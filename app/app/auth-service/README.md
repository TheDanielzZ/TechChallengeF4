# auth-service (Go)

Este é o serviço de autenticação do projeto ToggleMaster. Ele é responsável por criar e validar chaves de API.

## 📦 Pré-requisitos (Local)

* [Go](https://go.dev/doc/install) (versão 1.21 ou superior)
* [PostgreSQL](https://www.postgresql.org/download/) (rodando localmente ou em um contêiner Docker)

## 🚀 Rodando Localmente

1.  **Clone o repositório** e entre na pasta `auth-service`.

2.  **Prepare o Banco de Dados:**
    * Crie um banco de dados no seu PostgreSQL (ex: `auth_db`).
    * Execute o script `db/init.sql` para criar a tabela `api_keys`:
        ```bash
        psql -U seu_usuario -d auth_db -f db/init.sql
        ```

3.  **Configure as Variáveis de Ambiente:**
    Crie um arquivo chamado `.env` na raiz desta pasta (`auth-service/`) com o seguinte conteúdo:
    ```.env
    # String de conexão do seu banco de dados PostgreSQL
    DATABASE_URL="postgres://SEU_USUARIO:SUA_SENHA@localhost:5432/auth_db"
    
    # Porta que o serviço irá rodar
    PORT="8001"
    
    # Chave mestra para criar novas chaves de API
    MASTER_KEY="admin-secreto-123"
    ```

4.  **Instale as Dependências:**
    ```bash
    go mod tidy
    ```

5.  **Inicie o Serviço:**
    ```bash
    go run .
    ```
    O servidor estará rodando em `http://localhost:8001`.

## 🧪 Testando os Endpoints

Você pode usar `curl` ou Postman.

**1. Verifique a Saúde (Health Check):**
```bash
curl http://localhost:8001/health
```

Saída esperada: `{"status":"ok"}`

**2. Crie uma nova Chave de API (requer a MASTER_KEY):**

```bash
curl -X POST http://localhost:8001/admin/keys \
-H "Content-Type: application/json" \
-H "Authorization: Bearer admin-secreto-123" \
-d '{"name": "meu-primeiro-servico"}'
``` 

Saída esperada (A SUA CHAVE SERÁ DIFERENTE):

```json
{
  "name": "meu-primeiro-servico",
  "key": "tm_key_a1b2c3d4...",
  "message": "Guarde esta chave com segurança! Você não poderá vê-la novamente."
}
```

**3. Valide a Chave que você acabou de criar: (Substitua tm_key_... pela chave exata que você recebeu no passo anterior)**

```bash
curl http://localhost:8001/validate \
-H "Authorization: Bearer tm_key_a1b2c3d4..."
```

Saída esperada: `{"message":"Chave válida"}`

**4. Teste uma Chave Inválida:**

```bash
curl http://localhost:8001/validate \
-H "Authorization: Bearer chave-errada-123"
```

Saída esperada: `Chave de API inválida ou inativa`
## 🔭 Observabilidade (OpenTelemetry)

O serviço é instrumentado com o SDK do OpenTelemetry para Go (tudo em `telemetry.go`) e envia **traces e métricas via OTLP/HTTP** para o OTel Collector, que manda os traces para o APM (New Relic) e expõe as métricas para o Prometheus. Os logs saem em JSON no stdout, com `trace_id`, e o agente do Collector os leva ao Loki.

| Sinal | O que é gerado |
| --- | --- |
| Traces | Span de servidor por requisição (`GET /validate`, `POST /admin/keys`; o `/health` fica de fora), continuando o `traceparent` enviado pelo flag-service e pelo targeting-service, e um span de cliente por query no PostgreSQL (`SELECT api_keys`, `INSERT api_keys`, com `db.system=postgresql` e a query parametrizada) |
| Métricas | `http_server_requests_total`, `http_server_request_duration_seconds`, `http_server_active_requests`, `auth_key_validations_total{result}`, `auth_keys_created_total`, `auth_db_query_duration_seconds{query}` e `app_up` |
| Logs | JSON no stdout com `trace_id`/`span_id` |

Quando o PostgreSQL falha, o `/validate` responde 401 (comportamento original), mas o span da query e o da requisição são marcados como erro, para a queda do banco aparecer na taxa de erro do APM. Chave inexistente não conta como erro.

A configuração usa as variáveis padrão do OpenTelemetry, injetadas no cluster pelo Component `infra/components/otel-env`. Sem `OTEL_EXPORTER_OTLP_ENDPOINT` (ou com `OTEL_SDK_DISABLED=true`) a telemetria vira no-op; `OTEL_TRACES_EXPORTER=none` / `OTEL_METRICS_EXPORTER=none` desligam um sinal.

> As versões do OpenTelemetry estão fixadas na linha `v1.46` / contrib `v0.71`, a última compatível com o `golang:1.25` usado no CI (a `v1.47` exige Go 1.26).
