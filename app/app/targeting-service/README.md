# targeting-service (Python)

Este é o serviço de regras de segmentação (targeting) do projeto ToggleMaster. Ele é responsável por gerenciar regras complexas (ex: "50% dos usuários", "usuários do país X") para uma feature flag específica.

**IMPORTANTE:** Este serviço também é protegido e depende que o `auth-service` esteja rodando (ex: em `http://localhost:8001`).

## 📦 Pré-requisitos (Local)

* [Python](https://www.python.org/) (versão 3.9 ou superior)
* [PostgreSQL](https://www.postgresql.org/download/)
* O `auth-service` deve estar rodando.

## 🚀 Rodando Localmente

1.  **Clone o repositório** e entre na pasta `targeting-service`.

2.  **Prepare o Banco de Dados:**
    * Crie um banco de dados no seu PostgreSQL (ex: `targeting_db`).
    * Execute o script `db/init.sql` para criar a tabela `targeting_rules`:
        ```bash
        psql -U seu_usuario -d targeting_db -f db/init.sql
        ```

3.  **Configure as Variáveis de Ambiente:**
    Crie um arquivo chamado `.env` na raiz desta pasta (`targeting-service/`) com o seguinte conteúdo:
    ```.env
    # String de conexão do seu banco de dados PostgreSQL
    DATABASE_URL="postgres://SEU_USUARIO:SUA_SENHA@localhost:5432/targeting_db"
    
    # Porta que este serviço (targeting-service) irá rodar
    PORT="8003"
    
    # URL do auth-service (que deve estar rodando na porta 8001)
    AUTH_SERVICE_URL="http://localhost:8001"
    ```

4.  **Instale as Dependências:**
    ```bash
    pip install -r requirements.txt
    ```

5.  **Inicie o Serviço:**
    ```bash
    gunicorn --bind 0.0.0.0:8003 app:app
    ```
    O servidor estará rodando em `http://localhost:8003`.

## 🧪 Testando os Endpoints

Lembre-se de obter sua `SUA_CHAVE_API` no `auth-service` (veja o README do `flag-service`).

**1. Verifique a Saúde (Health Check):**
```bash
curl http://localhost:8003/health
```
Saída esperada: `{"status":"ok"}`

**2. Crie uma nova Regra de Segmentação:** Vamos criar uma regra para a flag enable-new-dashboard (que você criou no flag-service). Esta regra fará a flag aparecer para 50% dos usuários.
```bash
curl -X POST http://localhost:8003/rules \
-H "Content-Type: application/json" \
-H "Authorization: Bearer SUA_CHAVE_API" \
-d '{
    "flag_name": "enable-new-dashboard",
    "is_enabled": true,
    "rules": {
        "type": "PERCENTAGE",
        "value": 50
    }
}'
```
Saída esperada: (Um JSON com os dados da regra criada).

**3. Busque a Regra que você criou:**
```bash
curl http://localhost:8003/rules/enable-new-dashboard \
-H "Authorization: Bearer SUA_CHAVE_API"
```
Saída esperada: (O JSON da regra que você acabou de criar).

**4. Atualize a Regra (mude para 75%):**
```bash
curl -X PUT http://localhost:8003/rules/enable-new-dashboard \
-H "Content-Type: application/json" \
-H "Authorization: Bearer SUA_CHAVE_API" \
-d '{
    "rules": {
        "type": "PERCENTAGE",
        "value": 75
    }
}'
```
Saída esperada: (O JSON da regra atualizada, com `"value": 75`).

## 🔭 Observabilidade (OpenTelemetry)

A instrumentação fica no `telemetry.py`, idêntico nos 3 serviços Python (flag, targeting e analytics). Traces e métricas saem via **OTLP/HTTP** para o OTel Collector, que manda os traces para o APM (New Relic) e expõe as métricas para o Prometheus. Os logs saem no stdout com ` trace_id=... span_id=...` no fim da linha, e o agente do Collector os leva ao Loki.

Sem `OTEL_EXPORTER_OTLP_ENDPOINT` (ou com `OTEL_SDK_DISABLED=true`) a telemetria vira no-op. `OTEL_TRACES_EXPORTER=none` / `OTEL_METRICS_EXPORTER=none` desligam um sinal. No cluster as variáveis vêm do Component `infra/components/otel-env`. Com gunicorn, cada worker inicializa os seus providers.

| Sinal | O que é gerado |
| --- | --- |
| Traces | Span de servidor por requisição (`FlaskInstrumentor`, continuando o `traceparent` do evaluation-service; o `/health` fica de fora), span de cliente na chamada ao `auth-service /validate` (`RequestsInstrumentor`, que propaga o `traceparent`) e um span por query no PostgreSQL (`Psycopg2Instrumentor`) |
| Métricas | `http_server_requests_total`, `http_server_request_duration_seconds`, `http_server_active_requests`, `targeting_operations_total{operation,outcome}`, `targeting_operation_duration_seconds`, `targeting_auth_validations_total{outcome}`, `targeting_auth_validation_duration_seconds` e `app_up` |

As instrumentações automáticas ficam só com os traces (MeterProvider no-op): as métricas delas repetiriam as métricas HTTP acima com outros rótulos.

> O `RealDictCursor` é configurado no pool (`SimpleConnectionPool(..., cursor_factory=RealDictCursor)`) e os cursores são abertos com `conn.cursor()`. Passar `cursor_factory` em cada `conn.cursor(...)` contorna a instrumentação do psycopg2 e a query deixa de virar span.
