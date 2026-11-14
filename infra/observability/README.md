# ToggleMaster — Fase 4: métricas e logs dos microsserviços (Prometheus + Loki + Grafana)

Pacote com a instrumentação de **métricas** dos 5 microsserviços e a **centralização de logs no Loki**,
ambas integradas ao Grafana via **OpenTelemetry Collector** (requisito obrigatório do PDF).

```
 auth / flag / targeting / evaluation / analytics
        │  OTLP/HTTP :4318  (métricas, a cada 15 s)
        ▼
 OTel Collector central (ns observability) ──exporter prometheus :8889──► Prometheus ──┐
        │                                       (ServiceMonitor)                             ├─► Grafana
        └── próxima etapa: traces → Datadog/New Relic                                        │   (dashboard + alertas)
                                                                                             │
 stdout dos contêineres ─► OTel Collector agente (DaemonSet, filelog) ──OTLP /otlp──► Loki ──┘
```

> **Integrado ao TCF4.** Este pacote (pasta `toggle-master-f4-metricas`) já está aplicado neste repositório.
> Na integração:
> - o `go.mod`/`go.sum` do `auth-service` ganhou o OpenTelemetry fixado em `v1.46` (a `v1.47` exige Go 1.26 e o CI usa 1.25);
> - o `evaluation-service` juntou estas métricas com os traces do APM em um `telemetry.go` só;
> - o Component `otel-env` passou a definir `OTEL_LOGS_EXPORTER=none` (logs só pelo agente, sem duplicar no Loki);
> - o Collector central ganhou um pipeline de `traces` explícito (`debug` até o exporter do New Relic entrar);
> - as 5 Applications da stack apontam para o TCF4, com os caminhos sob `infra/`, e já estão em
>   `infra/argocd/apps/` (stack ativa), com `retry` no sync automático;
> - entraram testes do `telemetry.go` nos dois serviços Go, porque sem eles a cobertura ficava abaixo do mínimo
>   do pipeline de testes unitários.
>
> Também foram verificados, além do que está listado abaixo: `go build`/`go test` dos serviços Go, `kustomize build`
> de todos os overlays e de `infra/observability`, `helm template` dos 4 charts nas versões fixadas e o
> evaluation-service rodando contra um Collector com esta configuração (séries `http_server_requests_total`,
> `evaluation_*_total` e `app_up` no exporter Prometheus).

## O que tem aqui

Os arquivos ficam nas pastas `app/` e `infra/` deste repositório, nos caminhos da tabela abaixo.

| Onde | O que |
|---|---|
| `app/app/*/telemetry.go` · `telemetry.py` | Setup OTel + middleware HTTP + métricas de negócio de cada serviço |
| `app/app/*/main.go`, `handlers.go`, `evaluator.go`, `servicebus.go`, `app.py` | Pontos de medição (mudanças pequenas e localizadas) |
| `app/app/{analytics,targeting}-service/Dockerfile` | Passam a copiar o `telemetry.py` (antes só copiavam `app.py`) |
| `app/app/*/requirements.txt` | 3 pacotes `opentelemetry-*==1.27.0` nos serviços Python |
| `app/app/flag-service/tests/test_telemetry.py` | 4 testes do `telemetry.py` |
| `app/scripts/validate-metrics.sh` | **Script de validação**: mostra por serviço se as métricas chegaram ao Prometheus |
| `infra/components/otel-env/` | Kustomize Component que injeta as variáveis `OTEL_*` nos Deployments (ligado nos 5 overlays `prod`) |
| `infra/observability/` | Values do Collector, ServiceMonitor, PrometheusRule (alertas), dashboard Grafana, datasource do Loki |
| `infra/observability/loki-values.yaml` | **Loki** (chart `grafana/loki`, SingleBinary, PVC 10 Gi, retenção 72 h, OTLP habilitado) |
| `infra/observability/otel-collector-agent-values.yaml` | **Coleta de logs**: Collector em DaemonSet (`filelog` + `k8sattributes`) → Loki |
| `infra/observability/datasource-loki.yaml` | Datasource do Loki no Grafana (uid `loki`) |
| `infra/observability/kube-prometheus-stack-values.yaml` | Values do Prometheus/Grafana (descoberta de ServiceMonitor, regras, dashboards e datasources) |
| `infra/argocd/apps/{kube-prometheus-stack,loki,otel-collector,otel-collector-agent,observability-config}.yaml` | 5 Applications do ArgoCD: os 4 charts Helm (multi-source, versão fixada) + a config |

## Métricas expostas

**HTTP — iguais nos 5 serviços** (labels: `service_name`, `method`, `route`, `status_code`, `status_class`):

| Métrica Prometheus | Uso |
|---|---|
| `http_server_requests_total` | taxa de requisições e de erros (`status_class="5xx"`) |
| `http_server_request_duration_seconds_bucket` | latência p50/p95/p99 |
| `http_server_active_requests` | requisições em andamento |
| `app_up` | heartbeat: 1 por processo com telemetria ativa |

`/health` **não** entra na taxa (as sondas do kubelet poluiriam). `OTEL_METRICS_INCLUDE_HEALTH=true` reativa.

**Negócio, por serviço:**

| Serviço | Métricas |
|---|---|
| auth-service | `auth_key_validations_total{result=valid\|invalid\|db_error\|missing_header}`, `auth_keys_created_total`, `auth_db_query_duration_seconds{query}` |
| flag-service | `flag_operations_total{operation,outcome}`, `flag_operation_duration_seconds`, `flag_auth_validations_total{outcome}`, `flag_auth_validation_duration_seconds` |
| targeting-service | idem, com prefixo `targeting_` |
| evaluation-service | `evaluation_cache_requests_total{result=hit\|miss\|error}`, `evaluation_decisions_total{result}`, `evaluation_upstream_requests_total{upstream,outcome}`, `evaluation_upstream_duration_seconds{upstream}`, `evaluation_events_published_total{outcome}` |
| analytics-service | `analytics_messages_processed_total{outcome}`, `analytics_message_processing_duration_seconds`, `analytics_event_age_seconds` (idade do evento → detecta backlog), `analytics_worker_errors_total` |

## Como aplicar

1. ~~Copie `projeto/` por cima do projeto~~ — feito na integração ao TCF4.
2. ~~Rode `go mod tidy` em `auth-service` e `evaluation-service`~~ — feito, com o OpenTelemetry fixado em `v1.46`.
3. **Build/deploy**: o Jenkins gera as imagens novas e atualiza a tag nos overlays (fluxo atual). O
   Component `otel-env` entra junto e o ArgoCD reinicia os pods com as variáveis `OTEL_*`.
4. **Stack no cluster, via GitOps** (requisito do PDF: Helm charts no repositório GitOps): ~~mover as Applications
   para `infra/argocd/apps/`~~ — feito. O root app cria 5 Applications, em ordem de `sync-wave`. Como o App-of-Apps
   não espera um filho ficar saudável antes do próximo, cada uma tem `retry` no sync automático, e a
   `observability-config` usa `SkipDryRunOnMissingResource` (os CRDs do Prometheus podem chegar depois):

   | Wave | Application | Namespace |
   |---|---|---|
   | 0 | `kube-prometheus-stack` (Prometheus, Grafana, Alertmanager) | monitoring |
   | 1 | `loki` | logging |
   | 2 | `otel-collector` (métricas) e `otel-collector-agent` (logs) | observability |
   | 3 | `observability-config` (ServiceMonitor, alertas, datasource Loki, dashboard) | monitoring |

   Requer ArgoCD ≥ 2.6 (multi-source). As versões dos charts estão fixadas nos Applications — confirme se são as que
   você quer antes do commit. Sem GitOps (teste rápido), os comandos `helm upgrade --install` estão no cabeçalho de
   cada `*-values.yaml`.
5. **Valide**:
   ```bash
   ./app/scripts/validate-metrics.sh                                   # métricas + logs (port-forward automático)
   ./app/scripts/validate-metrics.sh --base-url http://<IP-GATEWAY> --api-key <chave>   # gera tráfego antes de medir
   ./app/scripts/validate-metrics.sh --skip-logs                       # só métricas
   ```
   Sai com código 1 se algum serviço não enviar métricas, se o Loki estiver inacessível ou se **nenhum** serviço
   tiver logs. Um serviço ocioso sem logs na última hora é só avisado.

## Logs (Loki)

- **Coleta:** o agente (DaemonSet) lê `/var/log/pods/*` de **todos** os contêineres do cluster, enriquece com
  `k8sattributes` e envia ao Loki pelo endpoint OTLP nativo (`/otlp`). Nenhuma mudança nas aplicações: elas já logam em stdout.
- **Labels no Loki** (mapeamento padrão do Loki 3): `k8s_namespace_name`, `k8s_container_name`, `k8s_deployment_name`,
  `service_name` (= nome do contêiner, igual ao `service_name` das métricas → o mesmo filtro "Serviço" do dashboard vale
  para métricas e logs). O **pod não é label** (alta cardinalidade): use `| k8s_pod_name="..."`.
- **Consultas úteis (Grafana → Explore → Loki):**
  ```logql
  {k8s_namespace_name="toggle-apps", k8s_container_name="auth-service"}
  {k8s_namespace_name="toggle-apps"} |~ "(?i)(error|erro|panic|fatal)"
  sum by (k8s_container_name) (rate({k8s_namespace_name="toggle-apps"}[1m]))
  ```
- **Dashboard:** 3 painéis de logs (volume por serviço, linhas de erro, logs em tempo real com filtro de texto) mais um
  painel "Logs de erro em tempo real", útil na demo do incidente.

## Dashboard e alertas

Dashboard **"ToggleMaster — Visão geral"** (`uid: toggle-master-overview`, importado pelo sidecar do Grafana):
visão geral (serviços com telemetria, req/s, % 5xx, p95, pods, restarts, CPU/memória do cluster) ·
recursos por pod, CPU vs request (base do HPA) e réplicas · requisições por serviço (taxa, 5xx, p95, status, top rotas) ·
negócio (cache, dependências do evaluation, eventos, analytics, auth) · **logs do Loki em tempo real**, com filtro por
serviço e texto (e um painel só de erros). Anota no gráfico os alertas em *firing*.

Alertas (`prometheusrule-toggle.yaml`), todos com labels `service_name`/`deployment` para roteamento
(PagerDuty/OpsGenie/Slack) e para o self-healing saber qual Deployment reiniciar:
`ToggleHighHttp5xxRate` (> 5% por 2 min — o exemplo do PDF, vale para todos os serviços),
`AuthServiceDatabaseErrors`, `ToggleHighLatencyP95`, `EvaluationUpstreamFailing`, `AnalyticsProcessingFailing`, `AnalyticsQueueBacklog`.

## O que verifiquei e o que NÃO verifiquei

**Verificado aqui:** lógica do `telemetry.py` (4 testes com Flask real + meter falso: status, rotas, `/health`
ignorado, exceção → 5xx, gauge de ativas volta a 0, caminho no-op); o script de validação contra Prometheus e Loki
simulados (métricas OK, serviço sem métricas, Loki com logs, Loki vazio, Loki inacessível, `--skip-logs`); sintaxe de
todos os YAML/JSON; layout do dashboard sem sobreposição; sintaxe Python; balanceamento de chaves no Go; o patch
aplica limpo no original.

**Não verificado (sem Go, OTel, kustomize, helm nem cluster neste ambiente):** compilação do Go (por isso o passo 2),
`kustomize build`, `helm template` dos values e o fluxo ponta a ponta no cluster. Em especial **o pipeline de logs
nunca rodou de verdade**: os values do agente e do Loki seguem a documentação (Loki 3 + OTLP, presets do chart do
Collector), mas rode `helm template` e confira `kubectl -n observability logs ds/otel-collector-agent` no primeiro deploy. Vale rodar `go build ./... && go test ./...` e
`kustomize build infra/overlays/prod/auth-service` antes do commit.

## Pontos de atenção

- **auth-service devolve 401 (não 5xx) quando o PostgreSQL cai** — comportamento original do `/validate`. Um alerta
  só de 5xx não o enxerga; por isso existem a métrica `result="db_error"` e o alerta `AuthServiceDatabaseErrors`.
  Para a demo de incidente, derrubar o banco do **flag-service** dispara o alerta de 5xx normalmente.
- **Premissas a ajustar** se seu ambiente diferir: Prometheus no namespace `monitoring` e Service
  `kube-prometheus-stack-prometheus`; Loki no namespace `logging` com Service `loki` (o agente e o datasource apontam
  para `loki.logging.svc.cluster.local:3100`).
- Os nomes das métricas dependem do exporter Prometheus do Collector (`_total`, `_seconds`) — se trocar a versão
  do chart e as séries mudarem de nome, ajuste as queries.
- Python/gunicorn: cada worker é um processo com telemetria própria (`service_instance_id`); por isso `app_up` de um
  serviço com 4 workers vale 4. Nas queries sempre agregue com `sum by (service_name)`.
- O projeto roda em **AKS/Azure** (Jenkins + ACR), não EKS como cita o PDF; segui o que existe no repo.
- **Versões dos charts** (kube-prometheus-stack 65.1.1, loki 6.16.0, opentelemetry-collector 0.108.0) são versões que
  conheço como estáveis, **não** necessariamente as mais recentes. Se subir alguma, reconfira os values.
- **Loki em SingleBinary com disco local** serve para o laboratório/entrega; para produção use object storage (Azure Blob).
  Se o PVC encher, reduza `retention_period` ou aumente `singleBinary.persistence.size`.
- **Segurança:** `grafana.adminPassword: admin` em `kube-prometheus-stack-values.yaml` é só para lab; troque.
- Fora deste pacote (próximas etapas do PDF): traces/APM, PagerDuty/OpsGenie, ChatOps e o Self-Healing.
  O Collector central já está pronto para receber o pipeline de traces (comentário no `otel-collector-values.yaml`).

## Notificações, incidente e self-healing (requisito 4)

```
PrometheusRule (prometheusrule-toggle.yaml)
   └─► Alertmanager ── config global: AlertmanagerConfig monitoring/toggle-alertas
         ├─ critical de serviço (label service_name) ─► PagerDuty (abre e fecha o incidente)
         │                                           ├► Discord (🔥 ALERTA / ✅ RESOLVIDO)
         │                                           └► self-healer ─► rollout restart deployment/<label deployment>
         │                                                            └► Discord ("🔧 Self-healing ...")
         ├─ demais alertas (warning, regras do Kubernetes) ─► Discord
         └─ Watchdog / InfoInhibitor / info ─► descartados
```

| Peça | Onde |
| --- | --- |
| Rotas, receivers e inibições | `alertmanagerconfig-toggle.yaml`, ligado em `kube-prometheus-stack-values.yaml` (`alertmanagerSpec.alertmanagerConfiguration`) |
| Self-healer | `infra/self-healing/` (script Python em ConfigMap, RBAC só de `patch` em Deployments de `toggle-apps`), Application `infra/argocd/apps/self-healing.yaml` |
| Segredos | Credenciais *Secret text* `Discord` e `Pagerduty` do Jenkins → Key Vault (`discord-webhook-url`, `pagerduty-routing-key`) → `02-k8s-secrets.sh` → Secret `monitoring/alertmanager-notificacoes` |

**Por que um `AlertmanagerConfig` e não `alertmanager.config` nos values:** o Alertmanager 0.27 do chart não aceita
`webhook_url_file` no Discord, e a URL do webhook é segredo. No `AlertmanagerConfig`, `apiURL` (Discord) e `routingKey`
(PagerDuty) apontam para chaves do Secret e o prometheus-operator injeta os valores na config gerada. O
`alertmanagerConfigSelector` fica restrito a um label sem uso para o mesmo objeto não entrar também como sub-rota.

**Contra alert fatigue:** um aviso por alerta e serviço (`groupBy: [alertname, service_name]`); só os `critical` dos
serviços vão para o PagerDuty e para o self-healer; um `critical` silencia o `warning` do mesmo serviço; o self-healer
só reinicia Deployments da allowlist e no máximo 1 vez a cada 10 min por serviço.

**Validado num cluster k3s local** com o mesmo chart (65.1.1): o webhook de validação do operator aceitou o
`AlertmanagerConfig`, a config gerada traz a URL e a routing key injetadas, e um `ToggleHighHttp5xxRate` sintético
gerou a mensagem no Discord (num receptor falso), a chamada ao PagerDuty, o rollout restart do `flag-service` com o
aviso do self-healer e, ao resolver, o ✅ RESOLVIDO — sem nenhuma notificação com falha.

**Testar no cluster sem provocar falha** (dispara um alerta sintético; ele resolve sozinho em ~5 min):

```bash
kubectl -n monitoring port-forward svc/alertmanager-operated 9093:9093 &
curl -s -X POST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{
  "labels": {"alertname": "ToggleHighHttp5xxRate", "severity": "critical",
             "service_name": "flag-service", "deployment": "flag-service"},
  "annotations": {"summary": "flag-service: teste do fluxo de alertas",
                  "runbook": "kubectl -n toggle-apps rollout restart deployment/flag-service"}}]'
```

## Demonstração do incidente (a "prova real")

Falha que o restart cura: a rota de caos do flag-service (`app/app/flag-service/README.md`, seção "Injeção de falha").

1. Gere tráfego no `/evaluate` (que chama o flag-service) e, se quiser, no `/flags`. Por exemplo, em loop:
   `while true; do curl -s -o /dev/null "http://<IP>/evaluate?user_id=u$RANDOM&flag_name=<flag>"; sleep 0.3; done`
2. Ligue o caos: `curl -X POST http://<IP>/flags/_chaos -H "Authorization: Bearer <chave>"`.
   O flag-service passa a responder 500 e o evaluation-service, 502.
3. Em ~2–3 min: `ToggleHighHttp5xxRate` (flag-service e evaluation-service) e `EvaluationUpstreamFailing` em
   *firing* no Prometheus/Grafana; incidente no PagerDuty; mensagem no `#alerts-prometheus`. O New Relic abre a
   issue da condição "ToggleMaster - Taxa de erro > 5% (spans OTel)" e avisa no `#alerts-newrelic`
   (ver `newrelic/README.md`).
4. O self-healer reinicia o `flag-service`: `kubectl -n monitoring logs deploy/self-healer` mostra
   `alerta=ToggleHighHttp5xxRate deployment=flag-service acao=reiniciado`, e
   `kubectl -n toggle-apps get pods -w` mostra o pod novo subindo. O aviso do restart chega no Discord.
5. O pod novo nasce sem o caos: as respostas voltam a 200 e os alertas chegam como ✅ RESOLVIDO.

O self-healer espera e reenvia quando o Discord responde 429 (rate limit por webhook), o que acontece quando o
Alertmanager, o New Relic e o self-healer mandam mensagens ao mesmo tempo.
