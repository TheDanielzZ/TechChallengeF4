# ToggleMaster — Tech Challenge Fase 4

Sistema de **feature flags** em microsserviços no **Azure Kubernetes Service**, agora com **observabilidade
total e resposta ativa**: telemetria padronizada em **OpenTelemetry**, métricas no **Prometheus**, logs no
**Loki**, visualização no **Grafana**, APM e trace distribuído no **New Relic**, incidentes no **PagerDuty**,
ChatOps no **Discord** e **self-healing** automático — tudo entregue pela mesma esteira de **Terraform +
Jenkins + Argo CD** da Fase 3.

**🔗 Links do Projeto**

- 🎬 **Vídeo de Demonstração:** [Link do Vídeo](#) <!-- TODO: link do vídeo da F4 -->
- 💻 **Repositório (monorepo):** [TCF4 - Observabilidade](https://dev.azure.com/Oh20Tony/FIAP%20-%20TechChallenge/_git/TCF4%20-%20Observabilidade)
  — aplicação (`app/`), Terraform (`IaC/`, [runbook](IaC/README.md)) e GitOps (`infra/`, [runbook](infra/README.md))
- 📊 **Stack de observabilidade:** [`infra/observability/README.md`](infra/observability/README.md) — métricas,
  logs, dashboards, alertas e self-healing
- 🛰️ **New Relic:** [`infra/observability/newrelic/README.md`](infra/observability/newrelic/README.md) —
  dashboards, condição de alerta e workflow do APM

---

## 📌 Visão Geral

Na Fase 3 o ToggleMaster chegou ao estado da arte na **entrega**: ambiente inteiro em Terraform, deploy por
GitOps e uma esteira DevSecOps com gate de vulnerabilidades. Faltava **enxergar o sistema rodando**.

O cenário do desafio deixa isso claro: o `evaluation-service` começou a falhar em silêncio, o HPA escalou
para compensar, a equipe levou 6 horas para ser avisada pelos usuários e mais 4 para achar a causa nos logs
espalhados pelos pods.

Na **Fase 4** o alvo passou a ser a **operação**. Quatro mudanças:

1. **Telemetria padronizada.** Os 5 serviços foram instrumentados com as bibliotecas do **OpenTelemetry** e
   mandam métricas e traces por OTLP para um **OTel Collector**, a peça central que roteia cada sinal para o
   backend certo. Nenhum serviço conhece Prometheus ou New Relic.
2. **Os três sinais centralizados.** Métricas no Prometheus, logs de **todos** os contêineres no Loki e
   traces no New Relic, com o `trace_id` presente nos logs — do log com erro se chega ao trace.
3. **Visibilidade de ponta a ponta.** Um `GET /evaluate` vira **um único trace** que atravessa os 5 serviços,
   o Redis, o PostgreSQL e o Service Bus, e o **Service Map** do New Relic mostra as dependências.
4. **Resposta ativa.** Um alerta crítico abre incidente no PagerDuty, avisa no Discord e aciona um
   **self-healer** que reinicia o Deployment afetado sem intervenção humana.

Requisitos técnicos cobertos:

- Monitoramento open source no Kubernetes: `Prometheus`, `Loki` e `Grafana`, instalados como **Helm charts
  pelo Argo CD** (GitOps), com dashboard customizado.
- `OpenTelemetry Collector` como ponto único de recepção, processamento e exportação de métricas e traces.
- Instrumentação de código com OpenTelemetry em Go e Python, **trace distribuído** e **Service Map** no
  `New Relic`.
- Alerta inteligente (`5xx > 5%`), incidente no `PagerDuty`, notificação no `Discord` e **self-healing** por
  Runbook Automation.
- Toda a base das Fases 1–3 mantida: 5 microsserviços em contêiner, Terraform, Jenkins com DevSecOps e
  Argo CD.

---

## 🗂️ Organização do Repositório

Na Fase 3 eram três repositórios. Na Fase 4 eles foram reunidos em um **monorepo**, cada um na sua pasta e
com o histórico preservado, porque a observabilidade atravessa as três camadas ao mesmo tempo: a
instrumentação mexe no código, a license key do New Relic e os segredos dos alertas passam pelo Terraform e
a stack é instalada pelo GitOps.

| Pasta | Origem na Fase 3 | Conteúdo | Quem consome |
| --- | --- | --- | --- |
| `app/` | `FIAP - ToggleMaster F3` | Código dos 5 serviços (com a instrumentação OTel), Dockerfiles, pipelines Jenkins, scripts | Jenkins |
| `IaC/` | `terraform - F3` | `bootstrap/`, `modules/`, `env/`, scripts de bootstrap do cluster, pipeline de infra | Jenkins / Terraform |
| `infra/` | `TCF3 - K8S` | `base/`, `overlays/`, `components/otel-env`, `observability/`, `self-healing/`, `argocd/` | Argo CD |

O commit de GitOps do CI agora cai no mesmo repositório que o Jenkins observa. Para não haver builds em
loop, o `app/Jenkinsfile` só considera alterações em `app/` (o commit de GitOps mexe só em `infra/`), e o
pipeline DevSecOps encerra como `NOT_BUILT` uma build automática cujo último commit é do próprio CI.

---

## 🏗️ Arquitetura da Solução

A base Azure da Fase 3 continua a mesma — AKS com AGIC, três PostgreSQL Flexible Server, Redis, Service Bus,
Cosmos DB, ACR e dois Key Vaults (detalhes em [`IaC/README.md`](IaC/README.md)). A Fase 4 acrescenta a stack
de observabilidade **dentro do cluster** e três serviços SaaS fora dele:

```
                        ┌──────────────────────────── AKS ─────────────────────────────┐
  cliente ─► App Gateway ─► toggle-apps: auth · flag · targeting · evaluation · analytics │
             (AGIC)     │        │ OTLP/HTTP :4318 (métricas + traces)    │ stdout (JSON │
             /grafana/ ─┼─┐      ▼                                        │  + trace_id) │
                        │ │  observability: OTel Collector (Deployment)   ▼              │
                        │ │        │  │                OTel Collector agente (DaemonSet) │
                        │ │        │  └─ :8889 ─► monitoring: Prometheus ◄─┐   │ filelog  │
                        │ │        │                 │      Alertmanager   │   ▼          │
                        │ └────────┼──► Grafana ◄────┤          │          │ logging: Loki│
                        │          │       ▲         └──────────┼──────────┘   │          │
                        │          │       └────────────────────┼──────────────┘          │
                        │          │                            ├─► self-healer ─► rollout restart
                        └──────────┼────────────────────────────┼──────────────────────────┘
                                   ▼ OTLP/gRPC                  ▼
                              New Relic (APM)        PagerDuty · Discord
                                   └──── workflow ───► PagerDuty · Discord
```

### Componentes novos

- **OpenTelemetry Collector (central):** Deployment no namespace `observability`, imagem `contrib`. Recebe
  OTLP (gRPC `:4317` e HTTP `:4318`) dos 5 serviços e exporta as **métricas** para o Prometheus (exporter
  `prometheus` na porta `8889`, raspado por um `ServiceMonitor`) e para o New Relic, e os **traces** para o
  New Relic (`otlp/newrelic`, `otlp.nr-data.net:4317`). Processors `memory_limiter` e `batch`.
- **OpenTelemetry Collector (agente):** DaemonSet que lê `/var/log/pods/*` de **todos** os contêineres do
  cluster (`filelog`), enriquece com `k8sattributes` (namespace, deployment, contêiner) e envia ao Loki pelo
  endpoint OTLP nativo. As aplicações não mudam nada para isso: continuam logando no stdout.
- **kube-prometheus-stack:** Prometheus (retenção de 3 dias), Alertmanager, Grafana, kube-state-metrics e
  node-exporter, no namespace `monitoring`. Os alvos do control plane e do kube-proxy ficam desligados — no
  AKS eles não são acessíveis e gerariam alertas `*Down` eternos.
- **Loki:** modo SingleBinary com PVC de 10 GiB, retenção de 72 h, OTLP habilitado, no namespace `logging`.
- **Grafana:** publicado no mesmo Application Gateway das APIs, em `http://<IP-do-gateway>/grafana/`. A senha
  do admin é gerada pelo Terraform e lida do Key Vault — nada de `admin/admin` exposto na internet.
- **self-healer:** serviço Python (só biblioteca padrão, script montado por ConfigMap na imagem
  `python:3.13-slim`) que recebe o webhook do Alertmanager e faz o `rollout restart` do Deployment do alerta.
- **New Relic** (APM), **PagerDuty** (incidentes) e **Discord** (ChatOps): SaaS, planos gratuitos.

> **Nota de região:** na Fase 3 os PostgreSQL ficavam em `eastus2`. Ao recriar o ambiente em 11/2026 o
> create passou a falhar lá com `SkuNotAvailable` — falta de capacidade para a subscription *Azure for
> Students*, que o `list-skus` não mostra. Os servidores foram para `centralus` (~20–30 ms do AKS em `eastus`,
> sem impacto para o projeto).

---

## 🔭 Arquitetura do OpenTelemetry

O Collector é o único destino que as aplicações conhecem. Trocar de APM, ligar outro backend ou desligar um
sinal é mudança de configuração do Collector, sem rebuild de imagem.

| Sinal | Origem | Caminho | Destino |
| --- | --- | --- | --- |
| **Traces** | SDK OTel nos 5 serviços | OTLP/HTTP → Collector central → `otlp/newrelic` | New Relic (APM, trace distribuído, Service Map) |
| **Métricas** | SDK OTel nos 5 serviços (HTTP + negócio) | OTLP/HTTP → Collector central → exporter `prometheus` `:8889` | Prometheus → Grafana e alertas; cópia no New Relic |
| **Logs** | stdout de todos os contêineres (JSON com `trace_id`) | arquivo do nó → Collector agente (`filelog` + `k8sattributes`) → OTLP | Loki → Grafana |

As variáveis `OTEL_*` chegam aos 5 Deployments por um único **Kustomize Component**
([`infra/components/otel-env`](infra/components/otel-env/kustomization.yaml)), ligado nos overlays `prod`:
endpoint do Collector, protocolo, intervalo de 15 s, `service.name` vindo do label `app` do Pod e os
atributos `k8s.pod.name`, `k8s.namespace.name` e `deployment.environment`.

Decisões que mantêm o desenho consistente:

| Regra | Por quê |
| --- | --- |
| Logs **não** vão por OTLP (`OTEL_LOGS_EXPORTER=none`) | O agente já lê o stdout; ligar os dois duplicaria as linhas no Loki |
| Instrumentações automáticas (`otelhttp`, Flask, requests) só geram **traces**, com MeterProvider no-op | As métricas HTTP próprias do projeto têm o mesmo nome (`http.server.request.duration`) com outros labels; sem isso dashboard e alertas contariam em dobro |
| Sem `OTEL_EXPORTER_OTLP_ENDPOINT`, a telemetria vira **no-op**; erro ao montar um sinal desliga só aquele sinal | Testes e execução local não precisam de Collector, e a telemetria nunca derruba o serviço |
| `/health` nunca vira span nem entra na taxa de requisições | As sondas do kubelet poluiriam o APM e as métricas |
| Propagação **W3C `traceparent`**, inclusive dentro da mensagem do Service Bus | É o que costura os serviços num trace só e forma as arestas do Service Map |
| Go fixado em OpenTelemetry `v1.46` / contrib `v0.71`; Python no SDK `1.27.0` com instrumentações `0.48b0` | A `v1.47` exige Go 1.26 e o CI usa `golang:1.25`; no Python é a dupla compatível |

---

## 🔄 Fluxo da Aplicação com Trace (Hot Path)

1. **Requisição externa:** o cliente chama `GET /evaluate` no IP público do Application Gateway, e o AGIC
   encaminha para um Pod do `evaluation-service`, que abre o **span raiz** do trace.
2. **Cache:** o `evaluation-service` consulta o Redis (span de cliente). Em *cache hit*, pula o passo 3 — o
   trace fica só com evaluation + Redis.
3. **Comunicação síncrona:** em *cache miss*, chama o `flag-service` e o `targeting-service` levando o
   `traceparent`. **Ambos** validam a API Key no `auth-service`, que consulta o PostgreSQL. Cada salto é um
   span filho no mesmo trace.
4. **Retorno ao cliente:** a decisão volta em JSON (`200 OK`).
5. **Mensageria assíncrona:** o evento é publicado no Service Bus por um span *producer*, com o `traceparent`
   e o `Diagnostic-Id` nas `ApplicationProperties` da mensagem.
6. **Consumo:** o `analytics-service` lê o contexto da mensagem e abre um span *consumer* **filho do trace
   do `/evaluate`**; a gravação no Cosmos DB é um span filho do consumo.

O trace de um `/evaluate` com *cache miss*:

```
evaluation-service  GET /evaluate                          Server
  evaluation-service  get / set (Redis)                    Client
  evaluation-service  GET flag-service                     Client
    flag-service        GET /flags/<string:name>           Server
      flag-service        GET (requests → auth)            Client
        auth-service        GET /validate                  Server
          auth-service        SELECT api_keys              Client  [postgresql]
      flag-service        SELECT (flags)                   Client  [postgresql]
  evaluation-service  GET targeting-service                Client
    targeting-service   GET /rules/<string:flag_name>      Server
      targeting-service   GET (requests → auth)            Client
        auth-service        GET /validate                  Server
          auth-service        SELECT api_keys              Client  [postgresql]
      targeting-service   SELECT (targeting_rules)         Client  [postgresql]
  evaluation-service  togglemasterqueue send               Producer
    analytics-service   togglemasterqueue process          Consumer
      analytics-service   create_item <container>          Client  [cosmosdb]
```

Validado no cluster: 20 chamadas a `/evaluate` geraram 20 eventos no Cosmos DB com **os mesmos `trace_id`**
do `evaluation-service` — o trace atravessa o Service Bus.

---

## 🧩 Instrumentação dos Serviços

| Serviço | Linguagem | O que vira span | Destaques |
| --- | --- | --- | --- |
| `evaluation-service` | Go | Servidor (`otelhttp`), Redis (`redisotel`), chamadas HTTP a flag/targeting, *producer* no Service Bus | As chamadas HTTP passaram a usar o `ctx` da requisição (antes saíam sem contexto e o trace quebraria); o evento sai com `context.WithoutCancel` para manter o trace depois da resposta; shutdown gracioso no SIGTERM envia a telemetria pendente |
| `auth-service` | Go | Servidor (`otelhttp`), queries no PostgreSQL (spans manuais) | O pgx v4 não tem instrumentação oficial; os spans levam `db.system`, `db.operation`, `db.sql.table` e a query parametrizada. Quando o banco cai o `/validate` responde **401**, então a falha marca erro no span da query **e** no da requisição — senão a queda seria invisível no APM |
| `flag-service` | Python (Flask) | Servidor (Flask), chamada ao auth (requests), PostgreSQL (psycopg2) | O `RealDictCursor` foi para o pool: passado em cada `conn.cursor(...)`, ele contornava a instrumentação **sem erro nenhum**. `skip_dep_check=True` por causa do `psycopg2-binary`. Rota `/flags/_chaos` para a demonstração do incidente |
| `targeting-service` | Python (Flask) | Igual ao flag-service | Mesmo `telemetry.py` (cópia idêntica nos 3 serviços Python) |
| `analytics-service` | Python | *Consumer* da fila e gravação no Cosmos DB (spans manuais) | Lê o `traceparent` das propriedades da mensagem (bytes ou str, com `Diagnostic-Id` de reserva). Falha no Cosmos marca erro; mensagem malformada, descartada, não conta como erro |

Todos os serviços logam no stdout em JSON com `trace_id` e `span_id`, e todos expõem as métricas HTTP
padronizadas (`http.server.requests`, `http.server.request.duration`, requisições ativas, `app.up`) mais
métricas de negócio próprias — cache hit/miss, decisões, chamadas a dependências, eventos publicados,
validações de chave, idade dos eventos consumidos. A lista completa está em
[`infra/observability/README.md`](infra/observability/README.md#métricas-expostas).

---

## 🛰️ APM e Distributed Tracing (New Relic)

Os traces e as métricas chegam ao New Relic pelo endpoint OTLP nativo, a partir do Collector — **sem agente
proprietário** nos serviços. O que fica no New Relic:

- **Service Map** com os 5 serviços e as arestas `evaluation → flag`, `evaluation → targeting`,
  `flag → auth`, `targeting → auth` e `evaluation → fila → analytics`, além das dependências externas
  (Redis, PostgreSQL, Cosmos DB).
- **Trace distribuído** de cada requisição, com os spans de servidor, cliente, *producer* e *consumer*.
- **Dashboard "ToggleMaster - APM"** (versionado em JSON no repositório), com três páginas: *Visão geral*
  (requisições, taxa de erro, p95, erros por rota e os últimos spans com erro com o `trace.id`),
  *Dependências e fila* e *Negócio*.
- **Condição de alerta NRQL** `ToggleMaster - Taxa de erro > 5% (spans OTel)` sobre os spans de servidor, e um
  **workflow** que notifica o PagerDuty e o canal `#alerts-newrelic` do Discord.

```sql
SELECT percentage(count(*), WHERE otel.status_code = 'ERROR') FROM Span WHERE span.kind = 'server' FACET service.name
```

A condição usa *gap filling* estático em `0`: quando o tráfego para junto com o erro, sem isso a issue nunca
fecharia. As condições padrão do New Relic (`apm.service.*`) não servem para serviços OpenTelemetry — a
contagem de erros delas fica sempre em zero — e ficam desligadas.

### Por que New Relic e não Datadog

- **OTLP nativo.** O New Relic recebe OTLP direto no endpoint de ingestão; o Collector padrão basta, com um
  exporter `otlp`. No Datadog o caminho é o agente próprio ou o exporter específico do Datadog — a
  instrumentação continuaria OTel, mas a ponta ficaria proprietária.
- **Plano gratuito sem prazo**, com 100 GB/mês de ingestão e APM, trace distribuído e Service Map incluídos.
  O plano gratuito do Datadog não inclui APM.
- **Nada amarrado ao fornecedor.** Como serviços e Collector falam só OpenTelemetry, trocar de APM é trocar o
  exporter no `otel-collector-values.yaml`.

---

## 📊 Métricas, Logs e Dashboards

A stack é instalada pelo Argo CD como **Applications multi-source** (Helm chart com versão fixada + values
deste repositório), em ordem de `sync-wave`:

| Wave | Application | Chart | Namespace |
| --- | --- | --- | --- |
| 0 | `kube-prometheus-stack` | `kube-prometheus-stack` 65.1.1 | `monitoring` |
| 1 | `loki` | `grafana/loki` 6.16.0 | `logging` |
| 2 | `otel-collector` e `otel-collector-agent` | `opentelemetry-collector` 0.108.0 | `observability` |
| 3 | `observability-config` (ServiceMonitor, regras, datasource, dashboards, AlertmanagerConfig) e `self-healing` | — | `monitoring` |

Como o App-of-Apps não espera um filho ficar saudável antes do próximo, cada Application tem `retry` no sync
automático, e a `observability-config` usa `SkipDryRunOnMissingResource` (os CRDs do Prometheus podem chegar
depois).

**Dashboards no Grafana** (importados pelo sidecar, a partir de ConfigMaps):

| Dashboard | Conteúdo |
| --- | --- |
| **ToggleMaster — Visão geral** | Serviços com telemetria, req/s, % de 5xx, p95, pods e restarts; CPU/memória por pod e CPU vs request (a base do HPA); requisições por serviço e por rota; métricas de negócio; **logs do Loki em tempo real** com filtro por serviço e texto, e um painel só de erros. Anota os alertas em *firing* nos gráficos |
| **ToggleMaster — Infraestrutura** | Nós, pods e recursos do AKS |

Um terceiro dashboard, da VM de CI/CD (Jenkins), usa o Azure Monitor e vive só no Grafana da VM
([`infra/observability/dashboards-vm/`](infra/observability/dashboards-vm/README.md)).

**Logs no Loki:** os labels são `k8s_namespace_name`, `k8s_container_name`, `k8s_deployment_name` e
`service_name` — o mesmo `service_name` das métricas, então um único filtro do dashboard vale para os dois.
O pod não é label (alta cardinalidade). Exemplos:

```logql
{k8s_namespace_name="toggle-apps", k8s_container_name="auth-service"}
{k8s_namespace_name="toggle-apps"} |~ "(?i)(error|erro|panic|fatal)"
```

O script [`app/scripts/validate-metrics.sh`](app/scripts/validate-metrics.sh) confere, serviço a serviço, se
as métricas chegaram ao Prometheus e os logs ao Loki (com `port-forward` automático).

---

## 🚨 Alertas, Incidentes e Self-Healing

### Regras de alerta

Todas em [`prometheusrule-toggle.yaml`](infra/observability/prometheusrule-toggle.yaml), com os labels
`service_name` e `deployment` — usados no roteamento e para o self-healer saber o que reiniciar.

| Alerta | Condição | Severidade |
| --- | --- | --- |
| `ToggleHighHttp5xxRate` | Taxa de 5xx > 5% por 2 min (qualquer serviço) | critical |
| `AuthServiceDatabaseErrors` | Qualquer validação de chave com `db_error` por 1 min | critical |
| `EvaluationUpstreamFailing` | Mais de 5% de falhas nas chamadas do evaluation a flag ou targeting por 3 min | critical |
| `ToggleHighLatencyP95` | p95 > 1 s por 5 min, com tráfego | warning |
| `AnalyticsProcessingFailing` | Mais de 5% das mensagens falhando por 5 min | warning |
| `AnalyticsQueueBacklog` | p95 da idade dos eventos consumidos > 5 min por 5 min (fila acumulando) | warning |

O `AuthServiceDatabaseErrors` existe porque o `auth-service` responde **401**, não 5xx, quando o banco cai —
um alerta só de 5xx não veria essa falha.

### Roteamento

```
PrometheusRule
   └─► Alertmanager (AlertmanagerConfig monitoring/toggle-alertas)
         ├─ critical de serviço ─► PagerDuty  (abre e resolve o incidente)
         │                       ├► Discord    (🔥 ALERTA / ✅ RESOLVIDO)
         │                       └► self-healer ─► rollout restart deployment/<deployment>
         │                                        └► Discord ("🔧 Self-healing ...")
         ├─ demais alertas (warning, regras do Kubernetes) ─► Discord
         └─ Watchdog / InfoInhibitor / info ─► descartados

New Relic (condição NRQL de taxa de erro) ─► workflow ─► PagerDuty + Discord #alerts-newrelic
```

O Alertmanager é a camada que aciona o self-healing; o New Relic é a segunda camada, que leva o link direto
para a issue e para os traces com erro.

**Contra alert fatigue:**

- um aviso por alerta e serviço (`groupBy: [alertname, service_name]`) — 5 pods com erro geram 1 mensagem;
- só os `critical` dos serviços acordam alguém e disparam a mitigação; os `warning` vão só para o Discord;
- um `critical` ativo silencia o `warning` do mesmo serviço (regra de inibição);
- regras padrão que nunca resolveriam no AKS (control plane, kube-proxy) foram desligadas na origem.

### Self-healer

[`infra/self-healing/`](infra/self-healing/) — Runbook Automation dentro do cluster, sem depender de serviço
externo:

- recebe o webhook do Alertmanager e, para cada alerta `firing` e `critical`, aplica o mesmo patch que o
  `kubectl rollout restart deployment/<deployment>` faz;
- só reinicia Deployments da **allowlist** (os 5 serviços) e só no namespace `toggle-apps`; o RBAC dá apenas
  `get`/`patch` em Deployments desse namespace;
- **cooldown de 10 min** por Deployment — o Alertmanager reenvia o alerta enquanto ele estiver ativo, e sem
  isso o pod seria reiniciado em loop;
- avisa no Discord o que fez, e espera e reenvia quando o Discord responde 429 (rate limit), o que acontece
  quando Alertmanager, New Relic e self-healer notificam ao mesmo tempo.

### Por que PagerDuty e não OpsGenie

- **O OpsGenie está em descontinuação.** A Atlassian encerrou a venda de novas contas e anunciou o fim do
  produto, migrando os clientes para o Jira Service Management. Começar um projeto nele agora seria construir
  sobre algo com data para acabar.
- **Integração nativa nas duas pontas.** O Alertmanager tem receiver `pagerduty` (Events API v2) e o New
  Relic tem o PagerDuty como destino de workflow — os dois abrem e resolvem o mesmo tipo de incidente sem
  código de cola.
- **Plano gratuito** suficiente para o time, com app móvel e escalonamento.

---

## 💥 Demonstração do Incidente (a "Prova Real")

A falha usada é uma que **um restart cura**: a rota de caos do `flag-service`. Ela grava um arquivo em
`/tmp` do contêiner, lido pelos 4 workers do gunicorn, e faz as requisições responderem 500 antes de tocar no
banco. O pod novo nasce sem o arquivo. O `/health` continua 200 de propósito, para a liveness probe não
"curar" o pod antes de o alerta disparar.

1. Gerar tráfego no `/evaluate`:
   `while true; do curl -s -o /dev/null "http://<IP>/evaluate?user_id=u$RANDOM&flag_name=<flag>"; sleep 0.3; done`
2. Ligar o caos: `curl -X POST http://<IP>/flags/_chaos -H "Authorization: Bearer <chave>"` — o
   `flag-service` passa a responder 500 e o `evaluation-service`, 502.
3. Em ~2–3 min: `ToggleHighHttp5xxRate` e `EvaluationUpstreamFailing` em *firing* no Grafana; incidente aberto
   no PagerDuty; mensagens no Discord; issue aberta no New Relic, com os spans de erro no trace.
4. O self-healer reinicia o `flag-service`: `kubectl -n monitoring logs deploy/self-healer` mostra
   `acao=reiniciado`, e `kubectl -n toggle-apps get pods -w` mostra o pod novo subindo.
5. As respostas voltam a 200 e os alertas chegam como **✅ RESOLVIDO** no Discord e no PagerDuty.

Para testar só o fluxo de notificação, sem provocar falha, há um alerta sintético em
[`infra/observability/README.md`](infra/observability/README.md#notificações-incidente-e-self-healing-requisito-4).

---

## ⚙️ Esteira de CI/CD

A esteira da Fase 3 continua a mesma — build, testes, lint, SAST, SCA, scan de imagem, Quality Gate antes do
push e commit da tag no GitOps; nenhum pipeline aplica manifesto no cluster. O que mudou na Fase 4:

- **Um repositório só.** Os pipelines clonam o TCF4 uma vez e usam as pastas; o pipeline de infra ficou com
  um único parâmetro `BRANCH`.
- **Segredos novos pelo mesmo caminho dos antigos.** A license key do New Relic, o webhook do Discord e a
  routing key do PagerDuty são credenciais *Secret text* do Jenkins que viram variáveis do Terraform, vão
  para o Key Vault e de lá para Secrets do Kubernetes no bootstrap.
- **Nome do ACR lido do Key Vault.** O ACR tem sufixo aleatório e muda quando o ambiente é recriado; o CI lê o
  nome do segredo `acr-name` do cofre `[Infra CI]` em vez de depender de um valor fixo no `Jenkinsfile`.

| Job no Jenkins | Script Path | Papel |
| --- | --- | --- |
| `togglemaster-cicd` (multibranch) | `app/Jenkinsfile` | CI/CD por serviço alterado em `app/` |
| `togglemaster-IaC` | `IaC/pipeline_infra.jenkinsfile` | Terraform ponta a ponta e bootstrap do cluster (Argo CD, Secrets — inclusive os da observabilidade —, migrations) |
| — | `app/pipeline_unit_tests.jenkinsfile` | Os 5 serviços em paralelo + gate de cobertura |
| — | `app/pipeline_devsecops.jenkinsfile` | Varredura de segurança completa e DefectDojo |

### Testes unitários

O código de telemetria entrou **com testes**: o gate de cobertura do pipeline reprovaria o build sem eles.
Os testes verificam, entre outras coisas, que o span de servidor continua o `traceparent` recebido, que o
span do banco é filho dele, que a falha do banco marca erro mesmo respondendo 401, que o consumo da fila
continua o trace do produtor e que o `/health` fica fora dos traces. Nenhum teste precisa de Collector: sem
endpoint a telemetria é no-op.

| Serviço | Linguagem | Cobertura F3 | Cobertura F4 | Mínimo |
| --- | --- | --- | --- | --- |
| `auth-service` | Go | 61,0 % | 76,2 % | 55 % |
| `evaluation-service` | Go | 66,5 % | 76,5 % | 60 % |
| `flag-service` | Python | 94 % | 95 % | 85 % |
| `targeting-service` | Python | 94 % | 94 % | 85 % |
| `analytics-service` | Python | 80 % | 85 % | 75 % |

O self-healer também tem testes (`infra/self-healing/test_healer.py`): allowlist, cooldown, alertas
`resolved` e não críticos ignorados, e o retry no 429 do Discord.

---

## 🔐 Segurança e Segredos

Os segredos da Fase 4 seguem a mesma regra da Fase 3: **nada sensível no Git**.

| Segredo | Origem | Caminho até o consumidor |
| --- | --- | --- |
| License key do New Relic | Credencial Jenkins `NEW_RELIC_LICENSE_KEY` | `TF_VAR_*` → Key Vault `[APP CI]` → `02-k8s-secrets.sh` → Secret `observability/newrelic-license` → env do Collector |
| Webhook do Discord | Credencial Jenkins `Discord` | `TF_VAR_*` → Key Vault → Secret `monitoring/alertmanager-notificacoes` → Alertmanager e self-healer |
| Routing key do PagerDuty | Credencial Jenkins `Pagerduty` | `TF_VAR_*` → Key Vault → Secret `monitoring/alertmanager-notificacoes` → Alertmanager |
| Senha do admin do Grafana | `random_password` do Terraform (ou valor fixado pelo time) | Key Vault → Secret `monitoring/grafana-admin` → Grafana |

- **Alertmanager sem segredo na config.** O Alertmanager do chart não aceita `webhook_url_file` no Discord.
  Por isso a configuração é um `AlertmanagerConfig`, em que `apiURL` e `routingKey` apontam para chaves do
  Secret, e o prometheus-operator injeta os valores na config gerada.
- **Self-healer com privilégio mínimo:** ServiceAccount própria, RBAC só de `get`/`patch` em Deployments do
  namespace das aplicações, sistema de arquivos somente leitura e allowlist explícita.
- **Grafana publicado com senha forte**, gerada pelo Terraform — o Grafana fica na mesma entrada pública das
  APIs.
- **O Collector sobe mesmo sem o Secret do New Relic** (`optional: true`): o export para o APM falha, mas
  Prometheus, Loki e alertas continuam funcionando.

---

## 📈 Escalabilidade

O HPA da Fase 3 continua: `evaluation-service` (50 % de CPU) e `analytics-service` (70 % de CPU), de 1 a 5
réplicas. O dashboard mostra agora **CPU vs request** e o número de réplicas lado a lado — exatamente a
visão que faltou no incidente do desafio, em que o HPA escalava para compensar uma falha que ninguém via.

A instrumentação tem custo: com o OpenTelemetry e 4 workers do gunicorn, os serviços Python passaram a usar
cerca de 180 MiB e eram mortos por `OOMKilled` com o limite antigo de 128 MiB. Os limites subiram para
192/384 MiB (`flag` e `targeting`) e 256/384 MiB (`analytics`).

O Collector central tem `memory_limiter` (80 % do limite de 512 MiB, com folga para picos): sob pressão ele
recusa dados em vez de ser morto, e os SDKs reenviam.

---

## 💰 Estimativa de Custos

A Fase 4 **não acrescenta recursos Azure cobrados à parte**: a stack de observabilidade roda nos nodes que o
AKS já tinha, e as ferramentas externas estão em planos gratuitos. A base continua a da Fase 3
(**R$ 1.948,63/mês** estimados, detalhados no README da Fase 3).

| Descrição | Serviço | Mensal (est.) | Observações |
| --- | --- | --- | --- |
| **Base do ambiente (Fase 3)** | AKS, App Gateway, PostgreSQL ×3, Redis, Service Bus, Cosmos DB, ACR | R$ 1.948,63 | Sem mudança de SKU |
| **Stack de observabilidade** | Prometheus, Loki, Grafana, OTel Collector, self-healer (no AKS) | R$ 0 adicional | Consome CPU/memória dos nodes existentes; pode levar o `apppool` ao 3º node pelo autoscaler (~R$ 240/mês enquanto ligado) |
| **Disco do Loki** | Managed Disk 10 GiB (PVC) | marginal | Retenção de 72 h |
| **APM** | New Relic Free | R$ 0 | 100 GB/mês de ingestão, 1 usuário completo |
| **Incidentes** | PagerDuty Free | R$ 0 | Até 5 usuários |
| **ChatOps** | Discord | R$ 0 | Webhooks de canal |

> O volume de ingestão do New Relic fica muito abaixo do limite gratuito: 5 serviços com tráfego de
> laboratório, `/health` fora dos traces e métricas a cada 15 s. Continua valendo a regra da Fase 3: uma
> subscription *Azure for Students* não sustenta o ambiente ligado o mês inteiro — rode `terraform destroy`
> entre as sessões de trabalho.

---

## 🚀 Como Subir o Ambiente

O caminho completo está no runbook [`IaC/README.md`](IaC/README.md). Além do que a Fase 3 já pedia:

1. **Contas externas:** criar a conta no New Relic (gerar a *License key* de ingestão), o serviço no PagerDuty
   (*Integration Key* da Events API v2) e o webhook do canal do Discord.
2. **Credenciais no Jenkins** (*Secret text*): `NEW_RELIC_LICENSE_KEY`, `Discord` e `Pagerduty`.
3. **Infraestrutura + bootstrap:** um build do `togglemaster-IaC` com `ACAO=apply` provisiona o ambiente,
   grava os segredos no Key Vault, cria os Secrets no cluster e instala o Argo CD.
4. **Imagens:** um build do `togglemaster-cicd` publica os 5 serviços no ACR e commita as tags em `infra/`.
5. **GitOps:** o root app (`infra/argocd/root-app.yaml`) cria as Applications dos serviços **e** da stack de
   observabilidade, em ordem de `sync-wave`. O resto é Git.
6. **Validação:** `app/scripts/validate-metrics.sh` (métricas e logs), Grafana em
   `http://<IP-do-gateway>/grafana/` (usuário `admin`, senha no segredo `grafana-admin-password` do Key Vault)
   e o Service Map no New Relic. Os dashboards do New Relic são importados dos JSON em
   [`infra/observability/newrelic/`](infra/observability/newrelic/README.md).

---

## 🎯 Conclusão

A Fase 2 resolveu o acoplamento da aplicação, a Fase 3 resolveu o acoplamento do processo e a Fase 4
resolveu a **cegueira da operação**.

O incidente do desafio — 6 horas até alguém perceber, 4 horas até achar a causa — teria hoje outro roteiro: o
alerta de 5xx dispara em 2 minutos, o PagerDuty acorda quem está de plantão, o Discord mostra o que está
acontecendo, o self-healer tenta o restart antes de qualquer pessoa abrir o terminal, e se o restart não
resolver o trace no New Relic aponta qual salto falhou e o `trace_id` leva aos logs daquele pod no Loki.

Tudo isso sem prender o projeto a um fornecedor: os serviços falam só OpenTelemetry, o Collector decide para
onde cada sinal vai, e a stack inteira é Helm chart com versão fixada no GitOps — sobe e é destruída junto com
o ambiente, como o resto da infraestrutura.

O que ficou de fora, conscientemente: **Loki com object storage** (Azure Blob) no lugar do disco local,
**Prometheus com retenção longa** (Thanos/Mimir), **tail sampling** no Collector para volumes de produção e
**KEDA** para escalar o `analytics-service` pela profundidade da fila — este último já listado como pendência
desde a Fase 3.

---

| Aluno | RM |
| --- | --- |
| Antony Matheus L Nascimento | 370340 |
| Daniel da Silva Junior | 370558 |
| Evandro Gomes da Silva | 373593 |
| Juan Rodrigues | 373074 |
| Gabriel Mota | 373164 |
