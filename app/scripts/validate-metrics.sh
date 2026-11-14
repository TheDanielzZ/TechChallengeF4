#!/usr/bin/env bash
#
# Valida a observabilidade de cada microsserviço do ToggleMaster:
#   - MÉTRICAS: app -> OTel Collector -> Prometheus (instâncias, req/s, % 5xx, p95)
#   - LOGS:     contêiner -> OTel Collector (agente) -> Loki (linhas recebidas por serviço)
#
# Uso:
#   ./validate-metrics.sh                                   # port-forward automático no Prometheus
#   ./validate-metrics.sh --prometheus-url http://localhost:9090
#   ./validate-metrics.sh --base-url http://<IP-DO-GATEWAY> --api-key tm_key_xxx
#                                                           # gera tráfego de teste antes de medir
#
# Opções:
#   --prometheus-url URL   Usa este Prometheus (não faz port-forward)
#   --namespace NS         Namespace do Prometheus         (padrão: monitoring)
#   --prom-service SVC     Service do Prometheus           (padrão: kube-prometheus-stack-prometheus)
#   --context CTX          Contexto do kubectl             (padrão: o atual)
#   --base-url URL         URL pública (Ingress/App Gateway) para gerar tráfego
#   --api-key KEY          Chave de API válida (habilita /validate, /flags e /rules autenticados)
#   --loki-url URL         Usa este Loki (não faz port-forward)
#   --loki-namespace NS    Namespace do Loki               (padrão: logging)
#   --loki-service SVC     Service do Loki                 (padrão: loki)
#   --skip-logs            Não valida o Loki
#   --requests N           Nº de rodadas de tráfego        (padrão: 60)
#   --wait SEG             Espera após o tráfego p/ export + scrape (padrão: 35)
#
# Código de saída: 0 = todos os serviços com telemetria; 1 = algum serviço sem métricas.
# Requisitos: curl, python3 e (sem --prometheus-url / --loki-url) kubectl.

set -euo pipefail

PROM_URL=""
PROM_NS="monitoring"
PROM_SVC="kube-prometheus-stack-prometheus"
KUBE_CTX=""
BASE_URL=""
API_KEY=""
REQUESTS=60
WAIT=35
PF_PID=""
LOKI_URL=""
LOKI_NS="logging"
LOKI_SVC="loki"
SKIP_LOGS=0
LF_PID=""

SERVICES=(auth-service flag-service targeting-service evaluation-service analytics-service)

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --prometheus-url) PROM_URL="$2"; shift 2 ;;
    --namespace)      PROM_NS="$2"; shift 2 ;;
    --prom-service)   PROM_SVC="$2"; shift 2 ;;
    --context)        KUBE_CTX="$2"; shift 2 ;;
    --base-url)       BASE_URL="${2%/}"; shift 2 ;;
    --api-key)        API_KEY="$2"; shift 2 ;;
    --loki-url)       LOKI_URL="$2"; shift 2 ;;
    --loki-namespace) LOKI_NS="$2"; shift 2 ;;
    --loki-service)   LOKI_SVC="$2"; shift 2 ;;
    --skip-logs)      SKIP_LOGS=1; shift ;;
    --requests)       REQUESTS="$2"; shift 2 ;;
    --wait)           WAIT="$2"; shift 2 ;;
    -h|--help)        usage 0 ;;
    *) echo "Opção desconhecida: $1" >&2; usage 1 ;;
  esac
done

command -v curl    >/dev/null || { echo "ERRO: curl não encontrado" >&2; exit 2; }
command -v python3 >/dev/null || { echo "ERRO: python3 não encontrado" >&2; exit 2; }

cleanup() {
  [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
  [ -n "$LF_PID" ] && kill "$LF_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Acesso ao Prometheus
# ---------------------------------------------------------------------------
if [ -z "$PROM_URL" ]; then
  command -v kubectl >/dev/null || { echo "ERRO: kubectl não encontrado (ou use --prometheus-url)" >&2; exit 2; }
  KCTX=(); [ -n "$KUBE_CTX" ] && KCTX=(--context "$KUBE_CTX")
  LOCAL_PORT=19090
  echo ">> port-forward svc/$PROM_SVC ($PROM_NS) -> localhost:$LOCAL_PORT"
  kubectl "${KCTX[@]}" -n "$PROM_NS" port-forward "svc/$PROM_SVC" "$LOCAL_PORT:9090" >/dev/null 2>&1 &
  PF_PID=$!
  PROM_URL="http://localhost:$LOCAL_PORT"
fi

for _ in $(seq 1 20); do
  curl -fsS "$PROM_URL/-/ready" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$PROM_URL/-/ready" >/dev/null 2>&1 || { echo "ERRO: Prometheus inacessível em $PROM_URL" >&2; exit 2; }

# prom_value '<promql>' -> imprime o valor do 1º resultado (ou vazio se não houver série)
prom_value() { api_value "$PROM_URL/api/v1/query" "$1"; }
# loki_value '<logql>' -> idem, no Loki (mesmo formato de resposta de instant query)
loki_value() { api_value "$LOKI_URL/loki/api/v1/query" "$1"; }

api_value() {
  curl -fsS -G --data-urlencode "query=$2" "$1" 2>/dev/null | python3 -c '
import sys, json
try:
    r = json.load(sys.stdin).get("data", {}).get("result", [])
    print(r[0]["value"][1] if r else "")
except Exception:
    print("")' || true
}

# ---------------------------------------------------------------------------
# Acesso ao Loki (opcional)
# ---------------------------------------------------------------------------
LOKI_OK=0
if [ "$SKIP_LOGS" -eq 0 ]; then
  if [ -z "$LOKI_URL" ]; then
    if command -v kubectl >/dev/null; then
      KCTX=(); [ -n "$KUBE_CTX" ] && KCTX=(--context "$KUBE_CTX")
      echo ">> port-forward svc/$LOKI_SVC ($LOKI_NS) -> localhost:13100"
      kubectl "${KCTX[@]}" -n "$LOKI_NS" port-forward "svc/$LOKI_SVC" 13100:3100 >/dev/null 2>&1 &
      LF_PID=$!
      LOKI_URL="http://localhost:13100"
    fi
  fi
  if [ -n "$LOKI_URL" ]; then
    for _ in $(seq 1 20); do
      curl -fsS "$LOKI_URL/ready" >/dev/null 2>&1 && { LOKI_OK=1; break; }
      sleep 1
    done
  fi
fi

# fmt <valor> <formato printf> [multiplicador] -> "-" se vazio/NaN
fmt() {
  local v="$1" f="$2" m="${3:-1}"
  if [ -z "$v" ] || [ "$v" = "NaN" ] || [ "$v" = "+Inf" ]; then echo "-"; return; fi
  awk -v v="$v" -v m="$m" "BEGIN{printf \"$f\", v*m}"
}

# ---------------------------------------------------------------------------
# Tráfego de teste (opcional)
# ---------------------------------------------------------------------------
if [ -n "$BASE_URL" ]; then
  echo ">> Gerando tráfego em $BASE_URL ($REQUESTS rodadas)..."
  declare -A CODES=()
  hit() { # hit <rótulo> <curl args...>
    local label="$1"; shift
    local code; code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$@" || true)
    CODES["$label $code"]=$(( ${CODES["$label $code"]:-0} + 1 ))
  }
  for i in $(seq 1 "$REQUESTS"); do
    hit "GET /evaluate      " "$BASE_URL/evaluate?user_id=user-$i&flag_name=smoke-test"
    hit "GET /validate (inv)" -H "Authorization: Bearer chave-invalida" "$BASE_URL/validate"
    if [ -n "$API_KEY" ]; then
      hit "GET /validate (ok) " -H "Authorization: Bearer $API_KEY" "$BASE_URL/validate"
      hit "GET /flags         " -H "Authorization: Bearer $API_KEY" "$BASE_URL/flags"
      hit "GET /rules/smoke   " -H "Authorization: Bearer $API_KEY" "$BASE_URL/rules/smoke-test"
    fi
  done
  # As chaves têm espaços ("GET /evaluate  200"): ler linha a linha, não com for/word-splitting.
  while IFS= read -r k; do printf '   %s -> %s vezes\n' "$k" "${CODES[$k]}"; done < <(printf '%s\n' "${!CODES[@]}" | sort)
  echo ">> Aguardando ${WAIT}s (export OTLP a cada 15s + scrape do Prometheus)..."
  sleep "$WAIT"
fi

# ---------------------------------------------------------------------------
# Pipeline: o Prometheus está raspando o OTel Collector?
# ---------------------------------------------------------------------------
echo
echo "== Pipeline =="
COLLECTOR_UP=$(prom_value 'max(up{job="otel-collector"})')
if [ "$COLLECTOR_UP" = "1" ]; then
  echo "  [OK]    Prometheus raspando o OTel Collector (up{job=\"otel-collector\"} = 1)"
else
  echo "  [FALTA] Prometheus não está raspando o OTel Collector."
  echo "          Verifique o ServiceMonitor 'otel-collector-metrics' (ns observability) e o label"
  echo "          release do kube-prometheus-stack. Alvos: $PROM_URL/targets"
fi

# ---------------------------------------------------------------------------
# Resumo por microsserviço
# ---------------------------------------------------------------------------
declare -A BUSINESS=(
  [auth-service]="auth_key_validations_total"
  [flag-service]="flag_operations_total"
  [targeting-service]="targeting_operations_total"
  [evaluation-service]="evaluation_cache_requests_total"
  [analytics-service]="analytics_messages_processed_total"
)

echo
echo "== Métricas por microsserviço =="
printf '  %-20s %-11s %-11s %-8s %-10s %-9s %s\n' 'SERVICO' 'INSTANCIAS' 'REQ/S' '5XX%' 'P95(ms)' 'STATUS' 'METRICA DE NEGOCIO'
printf '  %-20s %-11s %-11s %-8s %-10s %-9s %s\n' -------------------- ----------- ----------- -------- ---------- --------- ------------------
MISSING=0
for s in "${SERVICES[@]}"; do
  inst=$(prom_value "count(app_up{service_name=\"$s\"})")
  reqs=$(prom_value "sum(rate(http_server_requests_total{service_name=\"$s\"}[5m]))")
  errs=$(prom_value "100 * sum(rate(http_server_requests_total{service_name=\"$s\",status_class=\"5xx\"}[5m])) / sum(rate(http_server_requests_total{service_name=\"$s\"}[5m]))")
  p95=$(prom_value "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name=\"$s\"}[5m])))")
  metric="${BUSINESS[$s]}"
  biz=$(prom_value "count(${metric})")

  # 5xx: sem série 5xx mas com tráfego = 0%
  if [ -z "$errs" ] && [ -n "$reqs" ] && [ "$reqs" != "0" ]; then errs=0; fi

  if [ -n "$inst" ] && [ "$inst" != "0" ]; then status="OK"; else status="SEM DADOS"; MISSING=$((MISSING + 1)); fi
  if [ -n "$biz" ] && [ "$biz" != "0" ]; then bizs="ok  ($metric)"; else bizs="aguardando 1º evento ($metric)"; fi

  printf '  %-20s %-11s %-11s %-8s %-10s %-9s %s\n' "$s" \
    "$(fmt "$inst" '%d')" "$(fmt "$reqs" '%.3f')" "$(fmt "$errs" '%.2f')" "$(fmt "$p95" '%.1f' 1000)" "$status" "$bizs"
done

echo
echo "Legenda: INSTANCIAS = processos com telemetria ativa (Python/gunicorn conta 1 por worker)."
echo "         REQ/S exclui /health. analytics-service é um worker de fila: sem HTTP, veja a métrica de negócio."

# ---------------------------------------------------------------------------
# Logs no Loki
# ---------------------------------------------------------------------------
LOG_TOTAL=0
if [ "$SKIP_LOGS" -eq 1 ]; then
  echo
  echo "== Logs (Loki) == ignorado (--skip-logs)"
elif [ "$LOKI_OK" -ne 1 ]; then
  echo
  echo "== Logs (Loki) =="
  echo "  [FALTA] Loki inacessível (${LOKI_URL:-sem URL}). Verifique: kubectl -n $LOKI_NS get pods,svc  (ou use --loki-url / --skip-logs)"
  LOG_TOTAL=-1
else
  echo
  echo "== Logs (Loki) — últimas 1h =="
  printf '  %-20s %-14s %-10s %s\n' 'SERVICO' 'LINHAS(1h)' 'ERROS(1h)' 'STATUS'
  printf '  %-20s %-14s %-10s %s\n' -------------------- -------------- ---------- ---------
  for s in "${SERVICES[@]}"; do
    sel="{k8s_namespace_name=\"toggle-apps\", k8s_container_name=\"$s\"}"
    lines=$(loki_value "sum(count_over_time(${sel}[1h]))")
    errs=$(loki_value "sum(count_over_time(${sel} |~ \"(?i)(error|erro|panic|fatal)\" [1h]))")
    if [ -n "$lines" ] && [ "$lines" != "0" ]; then
      st="OK"; LOG_TOTAL=$((LOG_TOTAL + ${lines%.*}))
    else
      st="SEM LOGS"
    fi
    [ -z "$errs" ] && errs=0
    printf '  %-20s %-14s %-10s %s\n' "$s" "$(fmt "$lines" '%d')" "$(fmt "$errs" '%d')" "$st"
  done
  if [ "$LOG_TOTAL" -eq 0 ]; then
    echo
    echo "  [FALTA] O Loki está no ar, mas não recebeu nenhum log de toggle-apps. Checklist:"
    echo "    1. kubectl -n observability get ds otel-collector-agent        (1 pod por nó, todos Running?)"
    echo "    2. kubectl -n observability logs ds/otel-collector-agent       (erros de envio para o Loki?)"
    echo "    3. allow_structured_metadata: true no Loki (loki-values.yaml)"
  fi
fi

STATUS=0
if [ "$MISSING" -gt 0 ]; then
  echo
  echo "ATENÇÃO: $MISSING serviço(s) sem telemetria de métricas. Checklist:"
  echo "  1. A imagem nova (com a instrumentação) foi publicada e o ArgoCD sincronizou o overlay?"
  echo "  2. kubectl -n toggle-apps exec deploy/<svc> -- env | grep OTEL   (variáveis do Component otel-env)"
  echo "  3. kubectl -n observability logs deploy/otel-collector           (recebendo OTLP?)"
  STATUS=1
fi
# Logs: só reprovamos se o Loki está inacessível ou se NENHUM serviço tem logs (pipeline quebrado).
# Um serviço ocioso sem logs na última hora é apenas avisado.
if [ "$SKIP_LOGS" -eq 0 ] && [ "$LOG_TOTAL" -le 0 ]; then STATUS=1; fi

echo
if [ "$STATUS" -eq 0 ]; then
  if [ "$SKIP_LOGS" -eq 1 ]; then echo "Métricas chegando para todos os serviços."
  else echo "Métricas e logs chegando para todos os serviços."; fi
fi
exit "$STATUS"
