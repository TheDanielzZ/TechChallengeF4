"""
self-healer — Runbook Automation do ToggleMaster (requisito 4 do Tech Challenge).

Recebe o webhook do Alertmanager e tenta mitigar a falha sem intervenção humana:
para cada alerta firing com severity=critical, faz o equivalente a

    kubectl -n toggle-apps rollout restart deployment/<label deployment do alerta>

(o mesmo patch que o kubectl faz: anotação kubectl.kubernetes.io/restartedAt no
template do Pod) e avisa no Discord o que fez.

Proteções, para a automação não piorar um incidente:
  * só Deployments da allowlist (ALLOWED_DEPLOYMENTS) e só no namespace TARGET_NAMESPACE;
  * no máximo um restart por Deployment a cada COOLDOWN_SECONDS (o Alertmanager reenvia
    o alerta enquanto ele estiver ativo; sem isso o pod seria reiniciado em loop);
  * alertas resolved ou que não sejam critical são ignorados.

Só biblioteca padrão: roda na imagem python:3.13-slim com o script montado por ConfigMap,
sem build de imagem. Fala com a API do Kubernetes usando o token do ServiceAccount.
"""

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("self-healer")

TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "toggle-apps")
ALLOWED = {d.strip() for d in os.getenv("ALLOWED_DEPLOYMENTS", "").split(",") if d.strip()}
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "600"))
DISCORD_FILE = os.getenv("DISCORD_WEBHOOK_URL_FILE", "/etc/self-healer/discord-webhook-url")
PORT = int(os.getenv("PORT", "8080"))

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
USER_AGENT = "togglemaster-self-healer/1.0"  # o Discord recusa o User-Agent padrão do urllib


# ---------------------------------------------------------------------------
# Decisão (testável sem Kubernetes nem Discord)
# ---------------------------------------------------------------------------
class Healer:
    def __init__(self, restart, notify, allowed, cooldown, clock=time.monotonic):
        self._restart = restart
        self._notify = notify
        self._allowed = set(allowed)
        self._cooldown = cooldown
        self._clock = clock
        self._ultimo_restart = {}
        self._lock = threading.Lock()

    def processar(self, payload):
        """Trata um webhook do Alertmanager. Devolve uma lista de (deployment, ação, alerta)."""
        resultados = []
        for alerta in payload.get("alerts", []):
            labels = alerta.get("labels", {})
            nome = labels.get("deployment", "")
            alertname = labels.get("alertname", "?")

            if alerta.get("status") != "firing":
                continue
            if labels.get("severity") != "critical":
                resultados.append((nome, "ignorado: não é critical", alertname))
                continue
            if nome not in self._allowed:
                resultados.append((nome, "ignorado: fora da allowlist", alertname))
                continue

            with self._lock:
                agora = self._clock()
                anterior = self._ultimo_restart.get(nome)
                if anterior is not None and agora - anterior < self._cooldown:
                    resultados.append((nome, "ignorado: cooldown", alertname))
                    continue
                self._ultimo_restart[nome] = agora

            try:
                self._restart(nome)
                resultados.append((nome, "reiniciado", alertname))
                self._notify(
                    f"🔧 **Self-healing**: `kubectl rollout restart deployment/{nome}` executado "
                    f"automaticamente (namespace `{TARGET_NAMESPACE}`) em resposta ao alerta **{alertname}**. "
                    f"Próximo restart automático deste serviço só daqui a {self._cooldown // 60} min."
                )
            except Exception as exc:  # noqa: BLE001 — o webhook não pode cair por isso
                with self._lock:
                    self._ultimo_restart.pop(nome, None)  # deixa tentar de novo no próximo envio
                resultados.append((nome, f"falhou: {exc}", alertname))
                self._notify(f"⚠️ **Self-healing falhou** para `deployment/{nome}` ({alertname}): {exc}")

        for nome, acao, alertname in resultados:
            log.info("alerta=%s deployment=%s acao=%s", alertname, nome or "-", acao)
        return resultados


# ---------------------------------------------------------------------------
# Kubernetes e Discord
# ---------------------------------------------------------------------------
def rollout_restart(nome):
    """PATCH equivalente ao `kubectl rollout restart`: muda uma anotação do template."""
    token = open(os.path.join(SA_DIR, "token"), encoding="utf-8").read().strip()
    contexto = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    corpo = {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": agora,
        "togglemaster/self-healed-at": agora,
    }}}}}
    req = urllib.request.Request(
        f"{K8S_API}/apis/apps/v1/namespaces/{TARGET_NAMESPACE}/deployments/{nome}",
        data=json.dumps(corpo).encode(),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/strategic-merge-patch+json",
        },
    )
    with urllib.request.urlopen(req, context=contexto, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"API do Kubernetes respondeu {resp.status}")


DISCORD_TENTATIVAS = 4
DISCORD_ESPERA_MAX = 5.0  # segundos por espera; o webhook do Alertmanager não pode demorar demais
_discord_lock = threading.Lock()  # um envio por vez: rajadas paralelas é o que gera o 429


def _espera_429(exc):
    """Quanto esperar depois de um 429: Retry-After (cabeçalho) ou retry_after (corpo JSON)."""
    espera = None
    try:
        espera = float(exc.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        pass
    if espera is None:
        try:
            espera = float(json.loads(exc.read() or b"{}").get("retry_after"))
        except (TypeError, ValueError, AttributeError, OSError):
            espera = 1.0
    return min(max(espera, 0.1), DISCORD_ESPERA_MAX)


def notificar_discord(texto, abrir=urllib.request.urlopen, dormir=time.sleep):
    """Mensagem simples no mesmo canal dos alertas. Sem webhook configurado, só loga.

    O Alertmanager e o New Relic usam o mesmo canal: quando um incidente dispara, as
    mensagens chegam juntas e o Discord responde 429 (rate limit por webhook). Nesse
    caso esperamos o tempo que ele pede e tentamos de novo, para o aviso do restart
    não se perder.
    """
    try:
        url = open(DISCORD_FILE, encoding="utf-8").read().strip()
    except OSError:
        log.warning("webhook do Discord não configurado (%s): %s", DISCORD_FILE, texto)
        return False
    dados = json.dumps({"content": texto}).encode()
    with _discord_lock:
        for tentativa in range(1, DISCORD_TENTATIVAS + 1):
            req = urllib.request.Request(
                url, data=dados, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                abrir(req, timeout=10).close()
                return True
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or tentativa == DISCORD_TENTATIVAS:
                    log.error("falha ao avisar no Discord (tentativa %d): %s", tentativa, exc)
                    return False
                espera = _espera_429(exc)
                log.warning("Discord respondeu 429; nova tentativa em %.1fs", espera)
                dormir(espera)
            except (urllib.error.URLError, OSError) as exc:
                log.error("falha ao avisar no Discord: %s", exc)
                return False
    return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def criar_handler(healer):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — nome exigido pelo http.server
            if self.path == "/health":
                self._responder(200, {"status": "ok"})
            else:
                self._responder(404, {"error": "não encontrado"})

        def do_POST(self):  # noqa: N802
            if self.path != "/alertmanager":
                self._responder(404, {"error": "não encontrado"})
                return
            try:
                tamanho = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(tamanho) or b"{}")
            except ValueError:
                self._responder(400, {"error": "JSON inválido"})
                return
            resultados = healer.processar(payload)
            self._responder(200, {"resultados": [
                {"deployment": d, "acao": a, "alerta": n} for d, a, n in resultados
            ]})

        def _responder(self, status, corpo):
            dados = json.dumps(corpo, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

        def log_message(self, formato, *args):
            if "/health" not in (args[0] if args else ""):
                log.info("%s %s", self.address_string(), formato % args)

    return Handler


def main():
    healer = Healer(rollout_restart, notificar_discord, ALLOWED, COOLDOWN_SECONDS)
    log.info("self-healer ouvindo em :%d (namespace=%s, allowlist=%s, cooldown=%ss)",
             PORT, TARGET_NAMESPACE, ",".join(sorted(ALLOWED)) or "-", COOLDOWN_SECONDS)
    ThreadingHTTPServer(("0.0.0.0", PORT), criar_handler(healer)).serve_forever()


if __name__ == "__main__":
    main()
