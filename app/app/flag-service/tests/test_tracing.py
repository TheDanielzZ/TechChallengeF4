"""
Testes dos traces do telemetry.py (Flask, requests, logs e spans do Service Bus/Cosmos).

O telemetry.py é idêntico nos 3 serviços Python; estes testes ficam só no flag-service,
como os de métricas. Os spans vão para um exportador em memória — sem Collector nem rede
externa (o teste de propagação do requests sobe um HTTP server local).
"""

import atexit
import http.server
import logging
import threading

import pytest
import requests
from flask import Flask, jsonify
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

import telemetry

_EXPORTADOR = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTADOR))
trace.set_tracer_provider(_PROVIDER)

# traceparent publicado pelo evaluation-service (trace e span remotos fixos).
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


@pytest.fixture
def spans():
    _EXPORTADOR.clear()
    yield _EXPORTADOR
    _EXPORTADOR.clear()


def _hex_trace(span):
    return f"{span.context.trace_id:032x}"


# --- Service Bus / Cosmos DB --------------------------------------------------

@pytest.mark.parametrize("propriedades", [
    {"traceparent": TRACEPARENT},
    {b"traceparent": TRACEPARENT.encode()},  # o SDK do Service Bus pode entregar bytes
    {"Diagnostic-Id": TRACEPARENT},          # convenção dos SDKs Azure
])
def test_consumer_span_continua_o_trace_do_produtor(spans, propriedades):
    with telemetry.consumer_span("togglemasterqueue", propriedades):
        pass

    (span,) = spans.get_finished_spans()
    assert span.name == "togglemasterqueue process"
    assert span.kind == SpanKind.CONSUMER
    assert _hex_trace(span) == TRACE_ID
    assert f"{span.parent.span_id:016x}" == SPAN_ID
    assert span.attributes["messaging.system"] == "servicebus"
    assert span.attributes["messaging.destination.name"] == "togglemasterqueue"


def test_consumer_span_sem_contexto_abre_trace_novo(spans):
    with telemetry.consumer_span("fila", None):
        pass

    (span,) = spans.get_finished_spans()
    assert span.parent is None


def test_client_span_registra_excecao_e_marca_erro(spans):
    with pytest.raises(RuntimeError):
        with telemetry.client_span("create_item eventos", {"db.system": "cosmosdb"}):
            raise RuntimeError("cosmos fora do ar")

    (span,) = spans.get_finished_spans()
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["db.system"] == "cosmosdb"
    assert span.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in span.events)


def test_mark_span_error_sem_excecao(spans):
    with telemetry.consumer_span("fila", None) as span:
        telemetry.mark_span_error(span, "devolvida para a fila")
    telemetry.mark_span_error(None, "ignorado sem span")

    (finalizado,) = spans.get_finished_spans()
    assert finalizado.status.status_code == StatusCode.ERROR
    assert finalizado.status.description == "devolvida para a fila"


# --- Flask ----------------------------------------------------------------------

def test_flask_com_traces_continua_o_trace_e_ignora_health(spans, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_enabled", True)
    app = Flask(__name__)
    telemetry.instrument_flask(app)

    @app.route("/health")
    def health():
        return jsonify(ok=1)

    @app.route("/flags/<string:name>")
    def get_flag(name):
        return jsonify(name=name)

    client = app.test_client()
    client.get("/health")
    resposta = client.get("/flags/checkout-novo", headers={"traceparent": TRACEPARENT})
    assert resposta.status_code == 200

    (span,) = spans.get_finished_spans()  # o /health não gera span
    assert span.kind == SpanKind.SERVER
    assert span.attributes["http.route"] == "/flags/<string:name>"
    assert _hex_trace(span) == TRACE_ID
    assert f"{span.parent.span_id:016x}" == SPAN_ID


def test_flask_sem_traces_nao_cria_span(spans):
    app = Flask(__name__)
    telemetry.instrument_flask(app)  # _tracing_enabled é False fora do init

    @app.route("/flags")
    def flags():
        return jsonify([])

    app.test_client().get("/flags")
    assert spans.get_finished_spans() == ()


# --- requests (chamada ao auth-service) ------------------------------------------

class _GuardaHeaders(http.server.BaseHTTPRequestHandler):
    recebidos = []

    def do_GET(self):  # noqa: N802 — nome exigido pelo http.server
        _GuardaHeaders.recebidos.append(dict(self.headers))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_requests_propaga_traceparent_para_o_auth_service(spans):
    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    servidor = http.server.HTTPServer(("127.0.0.1", 0), _GuardaHeaders)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    _GuardaHeaders.recebidos.clear()

    telemetry.instrument_libraries()
    try:
        with trace.get_tracer("teste").start_as_current_span("requisicao") as pai:
            requests.get(f"http://127.0.0.1:{servidor.server_port}/validate", timeout=3)
    finally:
        RequestsInstrumentor().uninstrument()
        Psycopg2Instrumentor().uninstrument()
        servidor.shutdown()

    traceparent = _GuardaHeaders.recebidos[0].get("traceparent", "")
    assert f"{pai.get_span_context().trace_id:032x}" in traceparent
    cliente = [s for s in spans.get_finished_spans() if s.kind == SpanKind.CLIENT]
    assert len(cliente) == 1, "a chamada HTTP deveria virar um span de cliente"


# --- Logs ------------------------------------------------------------------------

def test_filtro_de_log_acrescenta_trace_id_so_dentro_de_span():
    filtro = telemetry.TraceContextFilter()
    registro = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)

    with trace.get_tracer("teste").start_as_current_span("requisicao") as span:
        filtro.filter(registro)
    assert f"trace_id={span.get_span_context().trace_id:032x}" in registro.otel

    filtro.filter(registro)
    assert registro.otel == ""


def test_install_log_correlation_e_idempotente():
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    raiz = logging.getLogger()
    raiz.addHandler(handler)
    try:
        telemetry.install_log_correlation()
        telemetry.install_log_correlation()
    finally:
        raiz.removeHandler(handler)

    assert handler.formatter._fmt == "%(levelname)s %(message)s%(otel)s"
    assert sum(isinstance(f, telemetry.TraceContextFilter) for f in handler.filters) == 1


# --- Inicialização ----------------------------------------------------------------

def test_init_com_traces_liga_provider_instrumentacoes_e_logs(monkeypatch):
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_tracing_enabled", False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")

    instalados = []
    monkeypatch.setattr(telemetry._trace_api, "set_tracer_provider", instalados.append)
    monkeypatch.setattr(telemetry, "instrument_libraries", lambda: instalados.append("libs"))
    monkeypatch.setattr(telemetry, "install_log_correlation", lambda: instalados.append("logs"))

    assert telemetry.init_telemetry("flag-service") is True
    assert telemetry._tracing_enabled is True
    provider, *resto = instalados
    assert isinstance(provider, TracerProvider)
    assert resto == ["libs", "logs"]
    provider.shutdown()
    # O init registrou o shutdown no atexit; no fim do pytest o stream de log já está
    # fechado e o segundo shutdown sujaria a saída com "Logging error".
    atexit.unregister(telemetry._shutdown)


def test_init_com_todos_os_sinais_desligados_e_noop(monkeypatch):
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_tracing_enabled", False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    assert telemetry.init_telemetry("flag-service") is False
    assert telemetry._tracing_enabled is False


def test_sdk_desligado_e_noop(monkeypatch):
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    assert telemetry.init_telemetry("flag-service") is False
