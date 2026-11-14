# flag-service (Python)

Este é o serviço de CRUD (Create, Read, Update, Delete) do projeto ToggleMaster. Ele é responsável por gerenciar as *definições* das feature flags.

**IMPORTANTE:** Este serviço é protegido e depende que o `auth-service` esteja rodando. Todas as requisições (exceto `/health`) exigem um header `Authorization: Bearer <sua-chave-api>`.

## 📦 Pré-requisitos (Local)

* [Python](https://www.python.org/) (versão 3.9 ou superior)
* [PostgreSQL](https://www.postgresql.org/download/) (rodando localmente ou em um contêiner Docker)
* O `auth-service` deve estar rodando (localmente na porta `8001`).

## 🚀 Rodando Localmente

1.  **Clone o repositório** e entre na pasta `flag-service`.

2.  **Prepare o Banco de Dados:**
    * Crie um banco de dados no seu PostgreSQL (ex: `flags_db`).
    * Execute o script `db/init.sql` para criar a tabela `flags`:
        ```bash
        psql -U seu_usuario -d flags_db -f db/init.sql
        ```

3.  **Configure as Variáveis de Ambiente:**
    Crie um arquivo chamado `.env` na raiz desta pasta (`flag-service/`) com o seguinte conteúdo:
    ```.env
    # String de conexão do seu banco de dados PostgreSQL
    DATABASE_URL="postgres://SEU_USUARIO:SUA_SENHA@localhost:5432/flags_db"
    
    # Porta que este serviço (flag-service) irá rodar
    PORT="8002"
    
    # URL do auth-service (que deve estar rodando na porta 8001)
    AUTH_SERVICE_URL="http://localhost:8001"
    ```

4.  **Instale as Dependências:**
    ```bash
    pip install -r requirements.txt
    ```

5.  **Inicie o Serviço:**
    ```bash
    gunicorn --bind 0.0.0.0:8002 app:app
    ```
    O servidor estará rodando em `http://localhost:8002`.

## 🧪 Testando os Endpoints

**Primeiro, você precisa de uma chave de API válida!**

1.  Vá até o terminal do `auth-service` (que deve estar rodando) e crie uma chave:
    ```bash
    curl -X POST http://localhost:8001/admin/keys \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer admin-secreto-123" \
    -d '{"name": "admin-para-flag-service"}'
    ```
2.  Copie a chave retornada (ex: `tm_key_...`). Vamos chamá-la de `SUA_CHAVE_API` no resto dos exemplos.

---

**Agora, teste o `flag-service`:**

**1. Verifique a Saúde (Health Check):**
```bash
curl http://localhost:8002/health
```

Saída esperada: `{"status":"ok"}`

**2. Tente Acessar um Endpoint Protegido (Sem Chave):**
```bash
curl http://localhost:8002/flags
```

Saída esperada: `{"error":"Authorization header obrigatório"}`

**3. Crie uma nova Flag (Com a Chave Correta):**
```bash
curl -X POST http://localhost:8002/flags \
-H "Content-Type: application/json" \
-H "Authorization: Bearer SUA_CHAVE_API" \
-d '{
    "name": "enable-new-dashboard",
    "description": "Ativa o novo dashboard para usuários",
    "is_enabled": true
}'
```
Saída esperada: (Um JSON com os dados da flag criada).

**4. Liste todas as Flags:**
```bash
curl http://localhost:8002/flags \
-H "Authorization: Bearer SUA_CHAVE_API"
```
Saída esperada: (Uma lista `[]` contendo a flag que você criou).

**5. Desative a Flag (PUT):**
```bash
curl -X PUT http://localhost:8002/flags/enable-new-dashboard \
-H "Content-Type: application/json" \
-H "Authorization: Bearer SUA_CHAVE_API" \
-d '{"is_enabled": false}'
```
Saída esperada: (O JSON da flag atualizada, com `"is_enabled": false`).

## 🔭 Observabilidade (OpenTelemetry)

A instrumentação fica no `telemetry.py`, idêntico nos 3 serviços Python (flag, targeting e analytics). Traces e métricas saem via **OTLP/HTTP** para o OTel Collector, que manda os traces para o APM (New Relic) e expõe as métricas para o Prometheus. Os logs saem no stdout com ` trace_id=... span_id=...` no fim da linha, e o agente do Collector os leva ao Loki.

Sem `OTEL_EXPORTER_OTLP_ENDPOINT` (ou com `OTEL_SDK_DISABLED=true`) a telemetria vira no-op. `OTEL_TRACES_EXPORTER=none` / `OTEL_METRICS_EXPORTER=none` desligam um sinal. No cluster as variáveis vêm do Component `infra/components/otel-env`. Com gunicorn, cada worker inicializa os seus providers.

| Sinal | O que é gerado |
| --- | --- |
| Traces | Span de servidor por requisição (`FlaskInstrumentor`, continuando o `traceparent` do evaluation-service; o `/health` fica de fora), span de cliente na chamada ao `auth-service /validate` (`RequestsInstrumentor`, que propaga o `traceparent`) e um span por query no PostgreSQL (`Psycopg2Instrumentor`) |
| Métricas | `http_server_requests_total`, `http_server_request_duration_seconds`, `http_server_active_requests`, `flag_operations_total{operation,outcome}`, `flag_operation_duration_seconds`, `flag_auth_validations_total{outcome}`, `flag_auth_validation_duration_seconds` e `app_up` |

As instrumentações automáticas ficam só com os traces (MeterProvider no-op): as métricas delas repetiriam as métricas HTTP acima com outros rótulos.

> O `RealDictCursor` é configurado no pool (`SimpleConnectionPool(..., cursor_factory=RealDictCursor)`) e os cursores são abertos com `conn.cursor()`. Passar `cursor_factory` em cada `conn.cursor(...)` contorna a instrumentação do psycopg2 e a query deixa de virar span.

## 💥 Injeção de falha (demonstração do incidente)

A rota `/flags/_chaos` liga uma falha proposital para a "prova real" do requisito 4 (alerta → incidente → self-healing). Exige a mesma chave de API das outras rotas.

| Método | Efeito |
| --- | --- |
| `POST /flags/_chaos` | Liga o caos. Corpo opcional `{"error_rate": 0.5}` (padrão `1.0`): essa fração das requisições responde **500** ("banco de dados indisponível"), antes de tocar no banco |
| `GET /flags/_chaos` | Mostra o estado (`{"chaos": true, "error_rate": 1.0}`) |
| `DELETE /flags/_chaos` | Desliga manualmente |

A falha vive num arquivo em `/tmp` do contêiner (`CHAOS_FILE`), lido pelos 4 workers do gunicorn. Por isso o `kubectl rollout restart` do self-healer **cura** a falha: o pod novo nasce sem o arquivo. O `/health` continua respondendo 200 de propósito, para a liveness probe não reiniciar o pod antes de o alerta disparar. O span da requisição fica com erro (com a exceção registrada) e o log sai em nível ERROR com o `trace_id`.

```bash
curl -X POST http://<IP-DO-GATEWAY>/flags/_chaos -H "Authorization: Bearer SUA_CHAVE_API"
```
