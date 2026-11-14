import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from azure.servicebus import ServiceBusClient
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
from dotenv import load_dotenv
from flask import Flask, jsonify

import telemetry

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

load_dotenv()

# --- Telemetria (traces -> APM, métricas -> Prometheus) ---
# No-op se OTEL_EXPORTER_OTLP_ENDPOINT não estiver definido.
# Com gunicorn (4 workers) cada worker inicializa o seu próprio provider.
telemetry.init_telemetry("analytics-service")
_meter = telemetry.get_meter()
messages_processed = _meter.create_counter(
    "analytics.messages.processed", unit="{message}",
    description="Mensagens do Service Bus por desfecho (success|discarded|cosmos_error|error)")
message_processing_duration = _meter.create_histogram(
    "analytics.message.processing.duration", unit="s",
    description="Duração do processamento de uma mensagem (inclui gravação no Cosmos DB)")
event_age = _meter.create_histogram(
    telemetry.AGE_METRIC_NAME, unit="s",
    description="Idade do evento (agora - timestamp) ao ser processado; sobe quando há backlog na fila")
worker_errors = _meter.create_counter(
    "analytics.worker.errors", unit="{error}",
    description="Falhas no loop do worker do Service Bus (reconexões)")


def _observe_event_age(timestamp):
    """Registra há quanto tempo o evento foi gerado. Ignora timestamps ausentes/ilegíveis."""
    try:
        created = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        event_age.record(max((datetime.now(timezone.utc) - created).total_seconds(), 0.0))
    except (ValueError, TypeError):
        pass


# --- Configuração ---
SERVICE_BUS_CONNECTION_STRING = os.getenv("SERVICE_BUS_CONNECTION_STRING")
SERVICE_BUS_QUEUE_NAME = os.getenv("SERVICE_BUS_QUEUE_NAME", "togglemaster-events")
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_KEY = os.getenv("COSMOS_KEY")
COSMOS_DATABASE = os.getenv("COSMOS_DATABASE", "analyticsdb")
COSMOS_CONTAINER = os.getenv("COSMOS_CONTAINER", "analytics-events")

# Valida variáveis obrigatórias
missing = [
    name for name, val in [
        ("SERVICE_BUS_CONNECTION_STRING", SERVICE_BUS_CONNECTION_STRING),
        ("COSMOS_ENDPOINT", COSMOS_ENDPOINT),
        ("COSMOS_KEY", COSMOS_KEY),
    ] if not val
]
if missing:
    log.critical(f"Variáveis obrigatórias não definidas: {', '.join(missing)}")
    sys.exit(1)

# --- Clientes Azure ---
try:
    cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
    database = cosmos_client.get_database_client(COSMOS_DATABASE)
    container = database.get_container_client(COSMOS_CONTAINER)
    log.info(f"Cliente Cosmos DB inicializado. Database: {COSMOS_DATABASE}, Container: {COSMOS_CONTAINER}")
except Exception as e:
    log.critical(f"Erro ao inicializar Cosmos DB: {e}")
    sys.exit(1)


# --- Worker do Azure Service Bus ---

def process_message(message):
    """
    Processa uma mensagem da fila e insere no Cosmos DB.

    Padrão de confiabilidade (at-least-once delivery):
    - Só confirma (complete) APÓS gravar com sucesso no Cosmos DB
    - Se falhar antes do complete, o lock expira (1 min) e a mensagem
      volta para a fila automaticamente — nenhum dado é perdido

    Analogia de dados: é como o checkpoint do Spark Structured Streaming —
    você só avança o offset depois que o dado foi persistido com sucesso.

    Trace: o span de consumo continua o trace do /evaluate que publicou o evento
    (traceparent nas application_properties), então a gravação no Cosmos DB aparece
    no mesmo trace distribuído do evaluation-service.
    """
    started = time.perf_counter()
    outcome = "error"
    properties = getattr(message, "application_properties", None)
    with telemetry.consumer_span(SERVICE_BUS_QUEUE_NAME, properties) as span:
        try:
            body = json.loads(str(message))
            log.info(f"Processando: Flag={body.get('flag_name')}, User={body.get('user_id')}")
            _observe_event_age(body.get("timestamp"))

            # UUID garante unicidade do documento no Cosmos DB
            event_id = str(uuid.uuid4())

            # Cosmos DB usa JSON puro — muito mais natural que o DynamoDB
            # que exige tipos explícitos como {"S": "valor"}, {"BOOL": true}
            item = {
                "id": event_id,           # obrigatório no Cosmos DB
                "event_id": event_id,
                "user_id": body["user_id"],
                "flag_name": body["flag_name"],   # partition key — deve existir!
                "result": body["result"],
                "timestamp": body["timestamp"],
            }

            with telemetry.client_span(f"create_item {COSMOS_CONTAINER}", {
                "db.system": "cosmosdb",
                "db.name": COSMOS_DATABASE,
                "db.cosmosdb.container": COSMOS_CONTAINER,
                "db.operation": "create_item",
            }):
                container.create_item(body=item)
            log.info(f"Evento {event_id} (Flag: {body['flag_name']}) salvo no Cosmos DB.")

            # Retorna True = processou com sucesso = pode confirmar na fila
            outcome = "success"
            return True

        except json.JSONDecodeError:
            outcome = "discarded"
            log.error("Mensagem malformada (poison pill) — descartando")
            return True  # descarta para não travar a fila
        except cosmos_exceptions.CosmosHttpResponseError as e:
            outcome = "cosmos_error"
            log.error(f"Erro ao gravar no Cosmos DB: {e}")
            return False  # não confirma — mensagem volta para a fila
        except Exception as e:
            log.error(f"Erro inesperado: {e}")
            return False
        finally:
            messages_processed.add(1, {"outcome": outcome})
            message_processing_duration.record(time.perf_counter() - started)
            if span is not None:
                span.set_attribute("toggle.outcome", outcome)
            if outcome in ("cosmos_error", "error"):
                telemetry.mark_span_error(span, f"mensagem devolvida para a fila ({outcome})")


def service_bus_worker_loop():
    """
    Loop principal do worker.
    
    Diferença Service Bus vs SQS:
    - SQS: você recebe e precisa deletar explicitamente com ReceiptHandle
    - Service Bus: mensagem fica locked durante processamento.
      complete_message() = confirma sucesso (remove da fila)
      abandon_message()  = devolve para a fila (será reprocessada)
    """
    log.info("Iniciando worker do Azure Service Bus...")

    while True:
        try:
            with ServiceBusClient.from_connection_string(
                SERVICE_BUS_CONNECTION_STRING
            ) as client:
                with client.get_queue_receiver(
                    queue_name=SERVICE_BUS_QUEUE_NAME,
                    max_wait_time=20,  # long-polling: espera até 20s por mensagens
                ) as receiver:
                    log.info("Conectado ao Service Bus. Aguardando mensagens...")

                    for message in receiver:
                        success = process_message(message)

                        if success:
                            receiver.complete_message(message)
                            log.info("Mensagem confirmada (removida da fila).")
                        else:
                            receiver.abandon_message(message)
                            log.warning("Mensagem devolvida para a fila (será reprocessada).")

        except Exception as e:
            worker_errors.add(1)
            log.error(f"Erro no worker do Service Bus: {e}")
            log.info("Aguardando 10s antes de reconectar...")
            time.sleep(10)


# --- Flask (apenas health check para o Kubernetes) ---
app = Flask(__name__)
telemetry.instrument_flask(app)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def start_worker():
    worker_thread = threading.Thread(target=service_bus_worker_loop, daemon=True)
    worker_thread.start()
    log.info("Worker do Service Bus iniciado em background.")


start_worker()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8005))
    app.run(host="0.0.0.0", port=port, debug=False)
