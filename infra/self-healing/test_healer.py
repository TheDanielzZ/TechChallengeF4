"""
Testes do self-healer (só biblioteca padrão). Rodar a partir desta pasta:

    python -m unittest -v test_healer
"""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import healer


def alerta(deployment="flag-service", severity="critical", status="firing", alertname="ToggleHighHttp5xxRate"):
    return {"status": status, "labels": {
        "alertname": alertname, "severity": severity, "deployment": deployment, "service_name": deployment}}


class Relogio:
    def __init__(self):
        self.agora = 1000.0

    def __call__(self):
        return self.agora


class TestHealer(unittest.TestCase):
    def setUp(self):
        self.reiniciados, self.avisos, self.relogio = [], [], Relogio()
        self.healer = healer.Healer(
            restart=self.reiniciados.append,
            notify=self.avisos.append,
            allowed={"flag-service", "auth-service"},
            cooldown=600,
            clock=self.relogio,
        )

    def test_critical_firing_reinicia_e_avisa(self):
        resultado = self.healer.processar({"alerts": [alerta()]})

        self.assertEqual(self.reiniciados, ["flag-service"])
        self.assertEqual(resultado, [("flag-service", "reiniciado", "ToggleHighHttp5xxRate")])
        self.assertIn("rollout restart deployment/flag-service", self.avisos[0])

    def test_resolved_warning_e_fora_da_allowlist_sao_ignorados(self):
        self.healer.processar({"alerts": [
            alerta(status="resolved"),
            alerta(severity="warning"),
            alerta(deployment="kube-dns"),
            alerta(deployment=""),
        ]})

        self.assertEqual(self.reiniciados, [])
        self.assertEqual(self.avisos, [])

    def test_cooldown_evita_restart_em_loop(self):
        self.healer.processar({"alerts": [alerta()]})
        self.relogio.agora += 120  # Alertmanager reenviando o mesmo alerta
        segundo = self.healer.processar({"alerts": [alerta()]})
        self.relogio.agora += 600  # passou o cooldown
        self.healer.processar({"alerts": [alerta()]})

        self.assertEqual(segundo, [("flag-service", "ignorado: cooldown", "ToggleHighHttp5xxRate")])
        self.assertEqual(self.reiniciados, ["flag-service", "flag-service"])

    def test_dois_alertas_do_mesmo_servico_reiniciam_uma_vez(self):
        self.healer.processar({"alerts": [alerta(), alerta(alertname="EvaluationUpstreamFailing")]})

        self.assertEqual(self.reiniciados, ["flag-service"])

    def test_falha_no_restart_avisa_e_libera_nova_tentativa(self):
        def quebra(_):
            raise RuntimeError("403 Forbidden")

        h = healer.Healer(quebra, self.avisos.append, {"flag-service"}, 600, clock=self.relogio)
        resultado = h.processar({"alerts": [alerta()]})
        self.relogio.agora += 30
        h.processar({"alerts": [alerta()]})

        self.assertTrue(resultado[0][1].startswith("falhou"))
        self.assertEqual(len(self.avisos), 2, "cada tentativa que falha avisa no Discord")
        self.assertIn("Self-healing falhou", self.avisos[0])


class Resposta:
    def close(self):
        pass


def erro_http(codigo, cabecalhos=None, corpo=b""):
    import email.message
    import io

    msg = email.message.Message()
    for k, v in (cabecalhos or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://discord.test/webhook", codigo, "erro", msg, io.BytesIO(corpo))


class TestDiscord(unittest.TestCase):
    """notificar_discord: o 429 do Discord não pode engolir o aviso do restart."""

    def setUp(self):
        import tempfile

        arq = tempfile.NamedTemporaryFile("w", suffix=".url", delete=False)
        arq.write("https://discord.test/webhook\n")
        arq.close()
        self.addCleanup(lambda: __import__("os").remove(arq.name))
        self.original = healer.DISCORD_FILE
        healer.DISCORD_FILE = arq.name
        self.addCleanup(setattr, healer, "DISCORD_FILE", self.original)
        self.esperas = []

    def abrir_com(self, respostas):
        chamadas = []

        def abrir(req, timeout):
            chamadas.append(json.loads(req.data)["content"])
            r = respostas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        return abrir, chamadas

    def test_429_espera_retry_after_e_reenvia(self):
        abrir, chamadas = self.abrir_com([erro_http(429, {"Retry-After": "0.7"}), Resposta()])

        ok = healer.notificar_discord("restart", abrir=abrir, dormir=self.esperas.append)

        self.assertTrue(ok)
        self.assertEqual(chamadas, ["restart", "restart"])
        self.assertEqual(self.esperas, [0.7])

    def test_429_sem_cabecalho_usa_retry_after_do_corpo_com_limite(self):
        corpo = json.dumps({"retry_after": 30}).encode()
        abrir, _ = self.abrir_com([erro_http(429, corpo=corpo), Resposta()])

        healer.notificar_discord("restart", abrir=abrir, dormir=self.esperas.append)

        self.assertEqual(self.esperas, [healer.DISCORD_ESPERA_MAX])

    def test_429_persistente_desiste_depois_das_tentativas(self):
        abrir, chamadas = self.abrir_com([erro_http(429, {"Retry-After": "1"})] * healer.DISCORD_TENTATIVAS)

        ok = healer.notificar_discord("restart", abrir=abrir, dormir=self.esperas.append)

        self.assertFalse(ok)
        self.assertEqual(len(chamadas), healer.DISCORD_TENTATIVAS)
        self.assertEqual(len(self.esperas), healer.DISCORD_TENTATIVAS - 1)

    def test_outro_erro_http_nao_tenta_de_novo(self):
        abrir, chamadas = self.abrir_com([erro_http(404)])

        self.assertFalse(healer.notificar_discord("restart", abrir=abrir, dormir=self.esperas.append))
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(self.esperas, [])

    def test_sem_webhook_configurado_so_loga(self):
        healer.DISCORD_FILE = "/caminho/que/nao/existe"
        self.assertFalse(healer.notificar_discord("restart", abrir=None))


class TestHTTP(unittest.TestCase):
    """O webhook de verdade: o Alertmanager faz POST com o JSON e espera 200."""

    def setUp(self):
        self.reiniciados = []
        h = healer.Healer(self.reiniciados.append, lambda _: None, {"flag-service"}, 600)
        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), healer.criar_handler(h))
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"

    def tearDown(self):
        self.servidor.shutdown()

    def test_webhook_do_alertmanager(self):
        corpo = json.dumps({"version": "4", "status": "firing", "alerts": [alerta()]}).encode()
        req = urllib.request.Request(f"{self.base}/alertmanager", data=corpo, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            dados = json.loads(resp.read())

        self.assertEqual(resp.status, 200)
        self.assertEqual(dados["resultados"][0]["acao"], "reiniciado")
        self.assertEqual(self.reiniciados, ["flag-service"])

    def test_health(self):
        with urllib.request.urlopen(f"{self.base}/health") as resp:
            self.assertEqual(json.loads(resp.read())["status"], "ok")

    def test_json_invalido_devolve_400(self):
        req = urllib.request.Request(f"{self.base}/alertmanager", data=b"nao-e-json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as erro:
            urllib.request.urlopen(req)
        self.assertEqual(erro.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
