# New Relic (APM)

Configuração do New Relic que não passa pelo cluster: dashboards, condição de alerta e workflow.
Os traces e as métricas chegam pelo OTel Collector (`otlp/newrelic` em `../otel-collector-values.yaml`).
Conta: `8684376` (endpoint US, `otlp.nr-data.net:4317`).

## Dashboards

| Arquivo | Dashboard |
|---|---|
| `dashboard-togglemaster-apm.json` | **ToggleMaster - APM**. Três páginas: *Visão geral* (requisições, taxa de erro, p95, throughput, erros por rota, últimos spans com erro e o `trace.id`), *Dependências e fila* (PostgreSQL, Redis, Cosmos DB, chamadas do evaluation para flag/targeting, Service Bus) e *Negócio* (cache hit, decisões, HTTP por classe, validações de chave, avaliações por flag) |
| `dashboard-apm-distributed-tracing-overview.json` | **APM & Distributed Tracing Overview** (Evandro). Uma página com filtro por serviço (variável `serviceName`) |

Para importar: **Dashboards → Import dashboard**, cole o JSON e clique em *Import dashboard*. Se for outra
conta, troque o `8684376` em `accountIds`.

Cuidados com os nomes dos atributos nos dados OpenTelemetry (conferidos no NRQL):

- erro do span: `otel.status_code = 'ERROR'`, com a mensagem em `error.group.message`;
- tipo do span: `span.kind` (`server`, `client`, `producer`, `consumer`);
- duração: `duration.ms`; trace: `trace.id`. Não existem `duration`, `traceId`, `statusMessage` nem
  `http.statusCode`: widgets com esses nomes aparecem vazios (é o caso de 3 widgets do dashboard do Evandro);
- métricas do app chegam com o nome OTel (`http.server.requests`, `evaluation.cache.requests` etc.),
  não com o nome do Prometheus (`http_server_requests_total`);
- os spans de **cliente** do evaluation para flag/targeting ficam com erro também no 404 (flag
  inexistente). Para taxa de erro, use só `span.kind = 'server'`.

## Alerta

Condição NRQL **ToggleMaster - Taxa de erro > 5% (spans OTel)**, na policy *Initial policy* (`8304670`):

```sql
SELECT percentage(count(*), WHERE otel.status_code = 'ERROR') FROM Span WHERE span.kind = 'server' FACET service.name
```

- Critical quando *above* `5` *for at least* 2 minutos; janela de 1 min, *event flow*, atraso de 2 min.
- **Gap filling: Static `0`**. Sem isso, quando o tráfego para junto com o erro a condição não recebe
  valor abaixo de 5% e a issue nunca fecha.
- As condições antigas da policy (`APM Error Rate/Response Time/Throughput Alerta`) usam métricas
  `apm.service.*`. A `apm.service.error.count` fica em 0 para serviços OpenTelemetry, então a de taxa de
  erro nunca dispararia; elas ficam desligadas.

O workflow **PageDutty NewRelic Alertas** filtra a policy `8304670` com prioridade Critical/High/Medium e
notifica o PagerDuty (serviço *NewRelic*) e o Discord (`#alerts-newrelic`, destino Webhook). O Discord
recusa o payload padrão do New Relic (exige `content` ou `embeds`); o template usado é:

```json
{
  "username": "New Relic APM",
  "embeds": [
    {
      "title": {{ json issueTitle }},
      "url": {{ json issuePageUrl }},
      "color": 15158332,
      "fields": [
        { "name": "Estado", "value": {{ json state }}, "inline": true },
        { "name": "Prioridade", "value": {{ json priority }}, "inline": true }
      ],
      "footer": { "text": "New Relic → Discord" }
    }
  ]
}
```

Os alertas do Prometheus (Alertmanager → PagerDuty, `#alerts-prometheus` e self-healer) continuam
sendo os que acionam o self-healing; o New Relic é a segunda camada, com o link direto para a issue
e os traces com erro.
