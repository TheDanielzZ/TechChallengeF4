#!/usr/bin/env python3
"""
Copia os dashboards do ToggleMaster (infra/observability/dashboards/ e, só para a VM,
infra/observability/dashboards-vm/) para o Grafana
da VM (tcf3...:3000), na pasta "ToggleMaster F4", e cria nele dois datasources que leem
o Prometheus e o Loki do cluster ATRAVÉS do Grafana do cluster (datasource proxy):

    Grafana da VM ──HTTP──► App Gateway /grafana/ ──► Grafana do AKS ──► Prometheus / Loki
                   token de service account (Viewer)        (rede interna do cluster)

Prometheus e Loki continuam sem endereço público; o Grafana da VM só tem um token de
LEITURA, que este script troca a cada execução (os anteriores são revogados).

Uso (WSL, com o kubectl apontando para o cluster):
    python3 infra/observability/scripts/sync-grafana-vm.py
    python3 infra/observability/scripts/sync-grafana-vm.py --dry-run   # só mostra o que faria
    python3 infra/observability/scripts/sync-grafana-vm.py --wait 900  # espera o Grafana do cluster subir

No pipeline de infra roda sozinho (etapa "12. Grafana da VM", IaC/scripts/06-grafana-vm.sh).

A senha do admin do Grafana da VM é pedida sem eco (ou VM_GRAFANA_PASSWORD).

Rodar de novo à mão só quando o pipeline não rodou (ambiente criado sem BOOTSTRAP)
ou depois de editar um dashboard no repo, para a cópia da VM acompanhar.

Variáveis opcionais: VM_GRAFANA (padrão http://tcf3.eastus.cloudapp.azure.com:3000; aceita
várias URLs separadas por vírgula — usa a primeira que responder), VM_GRAFANA_USER (admin),
FOLDER_UID (tm-f4), CLUSTER_GRAFANA (padrão: IP do Ingress do Grafana + /grafana).
"""
import base64
import getpass
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DASH_DIR = HERE.parent / "dashboards"          # os mesmos do Grafana do cluster (GitOps)
VM_DASH_DIR = HERE.parent / "dashboards-vm"    # só do Grafana da VM (ex.: VM de CI/CD via Azure Monitor)

VM_CANDIDATES = [u.strip().rstrip("/") for u in
                 os.environ.get("VM_GRAFANA", "http://tcf3.eastus.cloudapp.azure.com:3000").split(",") if u.strip()]
VM = VM_CANDIDATES[0]
VM_USER = os.environ.get("VM_GRAFANA_USER", "admin")
FOLDER_UID = os.environ.get("FOLDER_UID", "tm-f4")
FOLDER_TITLE = "ToggleMaster F4"
SA_NAME = "grafana-vm-reader"

# Datasources criados no Grafana da VM. src_uid = uid do datasource no Grafana do cluster.
DATASOURCES = {
    "datasource": {"uid": "aks-prometheus", "name": "Prometheus (AKS)", "type": "prometheus",
                   "src_uid": "prometheus", "jsonData": {"httpMethod": "GET", "timeInterval": "15s"}},
    "loki": {"uid": "aks-loki", "name": "Loki (AKS)", "type": "loki",
             "src_uid": "loki", "jsonData": {"maxLines": 1000}},
    # Opcional: alertas do Alertmanager do cluster na tela Alerting da VM (só leitura).
    "alertmanager": {"uid": "aks-alertmanager", "name": "Alertmanager (AKS)", "type": "alertmanager",
                     "src_uid": "alertmanager", "optional": True,
                     "jsonData": {"implementation": "prometheus", "handleGrafanaManagedAlerts": False}},
}

DRY = "--dry-run" in sys.argv
WAIT = int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 0


class Falha(Exception):
    pass


def info(msg):
    print(f"   {msg}")


def aviso(msg):
    print(f"   AVISO: {msg}")


def log(msg):
    print(f"\n=== {msg}")


def kubectl(*args, soft=False):
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if r.returncode != 0:
        if soft:
            raise Falha(f"kubectl {' '.join(args[:4])}: {r.stderr.strip()[:200]}")
        sys.exit(f"ERRO: kubectl {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def basic(user, pwd):
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


def call(base, auth, method, path, body=None, ok404=False, soft=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        msg = f"{method} {base}{path} -> HTTP {e.code}\n{e.read().decode(errors='replace')[:400]}"
    except (urllib.error.URLError, OSError) as e:
        msg = f"{method} {base}{path} -> {getattr(e, 'reason', e)}"
    if soft:
        raise Falha(msg)
    sys.exit(f"ERRO: {msg}")


def esperar(descricao, tentativa):
    """Repete tentativa() até ela devolver algo ou o --wait esgotar."""
    fim = time.time() + WAIT
    while True:
        try:
            r = tentativa()
            if r:
                return r
            motivo = "ainda não"
        except Falha as e:
            motivo = str(e).splitlines()[0]
        if time.time() >= fim:
            sys.exit(f"ERRO: {descricao}: {motivo}")
        info(f"aguardando {descricao} ({motivo}); nova tentativa em 20 s")
        time.sleep(20)


def prepare_dashboard(dash):
    """Remove o id local e fixa os datasources da VM como padrão das variáveis."""
    d = json.loads(json.dumps(dash))
    d.pop("id", None)
    for var in d.get("templating", {}).get("list", []):
        ds = DATASOURCES.get(var.get("name"))
        if ds and var.get("type") == "datasource":
            var["current"] = {"selected": True, "text": ds["name"], "value": ds["uid"]}
    return d


def main():
    dashboards = sorted(DASH_DIR.glob("*.json")) + sorted(VM_DASH_DIR.glob("*.json"))
    if not dashboards:
        sys.exit(f"ERRO: nenhum dashboard em {DASH_DIR}")

    global VM
    vpass = os.environ.get("VM_GRAFANA_PASSWORD") or getpass.getpass(f"Senha do '{VM_USER}' no Grafana da VM: ")
    vauth = basic(VM_USER, vpass)

    # --- Grafana do cluster (no pipeline ele ainda pode estar subindo) ---------
    log("Grafana do cluster")
    cluster = os.environ.get("CLUSTER_GRAFANA", "").rstrip("/")
    if not cluster:
        ip = esperar("IP do Ingress do Grafana do cluster", lambda: kubectl(
            "-n", "monitoring", "get", "ingress", "kube-prometheus-stack-grafana",
            "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}", soft=True))
        cluster = f"http://{ip}/grafana"
    cpass = base64.b64decode(esperar("Secret monitoring/grafana-admin", lambda: kubectl(
        "-n", "monitoring", "get", "secret", "grafana-admin",
        "-o", "jsonpath={.data.admin-password}", soft=True))).decode()
    cauth = basic("admin", cpass)
    h = esperar(f"{cluster}/api/health", lambda: call(cluster, cauth, "GET", "/api/health", soft=True))
    esperar("login no Grafana do cluster", lambda: call(cluster, cauth, "GET", "/api/user", soft=True))
    info(f"{cluster}  (Grafana {h.get('version', '?')}, banco {h.get('database', '?')})")

    # uid real dos datasources no cluster (fallback pelo tipo). O do Loki vem de um
    # ConfigMap da observability-config (sync-wave 3): pode chegar depois do Grafana.
    def datasources_prontos():
        src = call(cluster, cauth, "GET", "/api/datasources", soft=True)
        for ds in DATASOURCES.values():
            if any(s["uid"] == ds["src_uid"] for s in src):
                continue
            cand = [s for s in src if s["type"] == ds["type"]]
            if cand:
                ds["src_uid"] = cand[0]["uid"]
            elif not ds.get("optional"):
                return None
            else:
                ds["skip"] = True
        return True
    esperar("datasources Prometheus e Loki no Grafana do cluster", datasources_prontos)
    for ds in DATASOURCES.values():
        if ds.get("skip"):
            aviso(f"{ds['name']}: o Grafana do cluster não tem datasource {ds['type']} — pulado")
        else:
            info(f"{ds['name']} -> datasource '{ds['src_uid']}' do cluster")

    # --- Grafana da VM: primeira URL candidata que responder --------------------
    log("Grafana da VM")
    erros = []
    for base in VM_CANDIDATES:
        try:
            h = call(base, vauth, "GET", "/api/health", soft=True)
            call(base, vauth, "GET", "/api/user", soft=True)  # valida a senha
            VM = base
            break
        except Falha as e:
            erros.append(str(e).splitlines()[0])
    else:
        sys.exit("ERRO: nenhuma URL do Grafana da VM respondeu:\n  " + "\n  ".join(erros))
    info(f"{VM}  (Grafana {h.get('version', '?')}, banco {h.get('database', '?')})")

    if DRY:
        log("--dry-run: parando antes de qualquer alteração")
        for d in dashboards:
            info(f"importaria {d.name} na pasta {FOLDER_UID}")
        return

    # --- Service account de leitura no cluster ------------------------------
    log(f"Service account '{SA_NAME}' (Viewer) no Grafana do cluster")
    found = call(cluster, cauth, "GET", f"/api/serviceaccounts/search?query={SA_NAME}")
    sa = next((s for s in found.get("serviceAccounts", []) if s["name"] == SA_NAME), None)
    if sa is None:
        sa = call(cluster, cauth, "POST", "/api/serviceaccounts", {"name": SA_NAME, "role": "Viewer"})
        info("criado")
    else:
        info("já existia")
    old = call(cluster, cauth, "GET", f"/api/serviceaccounts/{sa['id']}/tokens")
    tok = call(cluster, cauth, "POST", f"/api/serviceaccounts/{sa['id']}/tokens",
               {"name": f"grafana-vm-{int(time.time())}"})["key"]
    for t in old:
        call(cluster, cauth, "DELETE", f"/api/serviceaccounts/{sa['id']}/tokens/{t['id']}")
    info(f"token novo emitido; {len(old)} anterior(es) revogado(s)")

    # --- Datasources no Grafana da VM ---------------------------------------
    log("Datasources no Grafana da VM")
    for ds in DATASOURCES.values():
        if ds.get("skip"):
            continue
        body = {
            "uid": ds["uid"], "name": ds["name"], "type": ds["type"], "access": "proxy",
            "url": f"{cluster}/api/datasources/proxy/uid/{ds['src_uid']}",
            "jsonData": {**ds["jsonData"], "httpHeaderName1": "Authorization"},
            "secureJsonData": {"httpHeaderValue1": f"Bearer {tok}"},
        }
        cur = call(VM, vauth, "GET", f"/api/datasources/uid/{ds['uid']}", ok404=True)
        if cur is None:
            call(VM, vauth, "POST", "/api/datasources", body)
            info(f"{ds['name']}: criado")
        else:
            body["id"] = cur["id"]
            call(VM, vauth, "PUT", f"/api/datasources/uid/{ds['uid']}", body)
            info(f"{ds['name']}: atualizado")

    # teste de ponta a ponta: uma consulta de verdade passando pelos dois Grafanas
    q = call(VM, vauth, "GET", "/api/datasources/proxy/uid/aks-prometheus/api/v1/query?query=count(up)")
    n = q.get("data", {}).get("result", [{}])[0].get("value", [None, "?"])[1]
    info(f"consulta de teste OK: o Prometheus do cluster respondeu count(up) = {n}")
    if not DATASOURCES["alertmanager"].get("skip"):
        a = call(VM, vauth, "GET", "/api/datasources/proxy/uid/aks-alertmanager/api/v2/alerts?active=true&silenced=false&inhibited=false")
        nomes = sorted({x.get("labels", {}).get("alertname", "?") for x in a if x.get("labels", {}).get("alertname") != "Watchdog"})
        info(f"Alertmanager do cluster respondeu: {len(nomes)} alerta(s) ativo(s) {nomes if nomes else ''}".rstrip())

    # --- Pasta e dashboards -----------------------------------------------------
    log(f"Dashboards na pasta '{FOLDER_UID}'")
    if call(VM, vauth, "GET", f"/api/folders/{FOLDER_UID}", ok404=True) is None:
        call(VM, vauth, "POST", "/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE})
        info(f"pasta '{FOLDER_TITLE}' criada")
    for path in dashboards:
        dash = prepare_dashboard(json.loads(path.read_text(encoding="utf-8")))
        r = call(VM, vauth, "POST", "/api/dashboards/db",
                 {"dashboard": dash, "folderUid": FOLDER_UID, "overwrite": True,
                  "message": "sync do repo TCF4 (infra/observability/dashboards)"})
        info(f"{dash.get('title')}: {VM}{r.get('url', '')}")

    log("Pronto")
    info("Pasta: /dashboards/f/" + FOLDER_UID)


if __name__ == "__main__":
    main()
