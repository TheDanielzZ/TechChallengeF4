"""
telemetry.py — instrumentação OpenTelemetry dos serviços Python do ToggleMaster.

Fluxo:
  traces   app --OTLP/HTTP--> OTel Collector --> New Relic (APM)
  métricas app --OTLP/HTTP--> OTel Collector --(exporter prometheus)--> Prometheus --> Grafana
  logs     stdout (com trace_id) --> agente do Collector (filelog) --> Loki

Este arquivo é IDÊNTICO em flag-service, targeting-service e analytics-service.
Mantenha as três cópias sincronizadas.

Decisões de projeto
-------------------
* Sem OTEL_EXPORTER_OTLP_ENDPOINT (ou sem as libs opentelemetry instaladas) tudo vira
  no-op: testes unitários e execução local não precisam de Collector.
  OTEL_TRACES_EXPORTER=none / OTEL_METRICS_EXPORTER=none desligam um sinal só.
* gunicorn sobe N workers (processos). Cada worker importa o app e chama
  init_telemetry(), ganhando seus próprios providers e um `service.instance.id`
  único (hostname-pid). Assim os workers não sobrescrevem as séries uns dos outros e
  não é preciso o modo "multiprocess" do prometheus_client. No Grafana, agregue com
  `sum by (service_name)`.
* Métricas HTTP têm nomes/rótulos iguais nos 5 microsserviços (mesmas queries no Grafana):
    http.server.requests          -> http_server_requests_total{method,route,status_code,status_class}
    http.server.request.duration  -> http_server_request_duration_seconds_bucket{...}
    http.server.active_requests   -> http_server_active_requests
  Por isso as instrumentações automáticas (Flask, requests) ficam SÓ com os traces: as
  métricas delas teriam nomes parecidos com outros rótulos e duplicariam o dashboard.
* `route` usa o template da rota do Flask (ex.: /flags/<string:name>), nunca o path
  cru, para não explodir a cardinalidade.
* Traces: Flask (span de servidor, continuando o traceparent recebido), requests (span
  de cliente e propagação do traceparent para o auth-service) e psycopg2 (span por
  query). O psycopg2 só é rastreado em cursores criados SEM `cursor_factory` na chamada
  `conn.cursor()`: o RealDictCursor vai no pool (SimpleConnectionPool(...,
  cursor_factory=RealDictCursor)), senão a instrumentação é contornada em silêncio.
"""

import atexit
import contextlib
import logging
import os
import socket
import time
from functools import wraps

from flask import g, request

log = logging.getLogger("telemetry")

try:
    from opentelemetry import metrics as _metrics_api
    from opentelemetry import propagate as _propagate
    from opentelemetry import trace as _trace_api
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.metrics import NoOpMeterProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import SpanKind, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # libs não instaladas (ex.: ambiente de teste enxuto)
    _OTEL_AVAILABLE = False

METER_NAME = "togglemaster"
TRACER_NAME = "togglemaster"

LATENCY_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
# "Idade" de eventos em fila (analytics): de 1s a 1h.
AGE_BUCKETS = [1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600]
AGE_METRIC_NAME = "analytics.event.age"

# Rotas que nunca viram span (sondas do kubelet a cada ~10s por pod).
TRACE_EXCLUDED_URLS = "/health$"

_initialized = False
_tracing_enabled = False


# ---------------------------------------------------------------------------
# No-op (usado quando o OTel não está disponível)
# ---------------------------------------------------------------------------
class _NoopInstrument:
    def add(self, *args, **kwargs):
        pass

    def record(self, *args, **kwargs):
        pass


class _NoopMeter:
    def create_counter(self, *args, **kwargs):
        return _NoopInstrument()

    create_histogram = create_up_down_counter = create_counter


def get_meter():
    """Meter do OTel (ou no-op). Seguro chamar antes ou depois de init_telemetry()."""
    if _OTEL_AVAILABLE:
        return _metrics_api.get_meter(METER_NAME)
    return _NoopMeter()


# ---------------------------------------------------------------------------
# Inicialização dos providers
# ---------------------------------------------------------------------------
def _endpoint_configured():
    return any(
        os.getenv(v)
        for v in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        )
    )


def _signal_enabled(env_var):
    return os.getenv(env_var, "").lower() != "none"


def init_telemetry(default_service_name):
    """Configura traces e métricas (OTLP/HTTP). Idempotente. Nunca derruba o serviço."""
    global _initialized
    if _initialized:
        return True

    if not _OTEL_AVAILABLE:
        log.warning("[telemetry] bibliotecas opentelemetry não instaladas: telemetria desativada")
        return False

    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true" or not _endpoint_configured():
        log.info("[telemetry] OTEL_EXPORTER_OTLP_ENDPOINT não definido: telemetria OTel desativada")
        return False

    try:
        # Resource.create() aplica os atributos passados POR CIMA do ambiente, então só
        # passamos service.name se o manifest K8s (OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES)
        # não tiver definido.
        attrs = {"service.instance.id": f"{socket.gethostname()}-{os.getpid()}"}
        if not os.getenv("OTEL_SERVICE_NAME") and "service.name=" not in os.getenv("OTEL_RESOURCE_ATTRIBUTES", ""):
            attrs["service.name"] = default_service_name
        resource = Resource.create(attrs)
    except Exception as exc:  # noqa: BLE001 — telemetria jamais pode derrubar o serviço
        log.error("[telemetry] falha ao montar o resource (seguindo sem telemetria): %s", exc)
        return False

    ativos = []
    if _signal_enabled("OTEL_METRICS_EXPORTER") and _init_metrics(resource):
        ativos.append("métricas")
    if _signal_enabled("OTEL_TRACES_EXPORTER") and _init_traces(resource):
        ativos.append("traces")

    _initialized = bool(ativos)
    if ativos:
        log.info("[telemetry] OTel ativo (service=%s, sinais=%s)", default_service_name, ", ".join(ativos))
    return _initialized


def _init_metrics(resource):
    try:
        interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "15000"))
        provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval_ms)
            ],
            views=[
                # Padrões PRECISOS: um padrão largo (ex.: "*age*" casa "messages") criaria
                # streams duplicados/incompatíveis em contadores.
                View(instrument_name="*.duration", aggregation=ExplicitBucketHistogramAggregation(LATENCY_BUCKETS)),
                View(instrument_name=AGE_METRIC_NAME, aggregation=ExplicitBucketHistogramAggregation(AGE_BUCKETS)),
            ],
        )
        _metrics_api.set_meter_provider(provider)
        atexit.register(_shutdown, provider)

        # Heartbeat: +1 por processo. Permite ver "serviço com telemetria ativa" no
        # Grafana mesmo sem tráfego (série cumulativa continua sendo exportada).
        get_meter().create_up_down_counter(
            "app.up", unit="{instance}", description="1 por instância do processo com telemetria ativa"
        ).add(1)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("[telemetry] falha ao inicializar métricas (seguindo sem elas): %s", exc)
        return False


def _init_traces(resource):
    global _tracing_enabled
    try:
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _trace_api.set_tracer_provider(provider)
        atexit.register(_shutdown, provider)
    except Exception as exc:  # noqa: BLE001
        log.error("[telemetry] falha ao inicializar traces (seguindo sem eles): %s", exc)
        return False

    _tracing_enabled = True
    instrument_libraries()
    install_log_correlation()
    return True


def _shutdown(provider):
    try:
        provider.shutdown()  # flush final
    except Exception:  # noqa: BLE001
        pass


def instrument_libraries():
    """
    Liga as instrumentações automáticas de cliente (só traces). Cada uma é opcional:
    o analytics-service, por exemplo, não usa requests nem psycopg2.
    Precisa rodar ANTES de o pool do psycopg2 abrir as conexões.
    """
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        if not RequestsInstrumentor().is_instrumented_by_opentelemetry:
            RequestsInstrumentor().instrument(meter_provider=NoOpMeterProvider())
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("[telemetry] instrumentação do requests desativada: %s", exc)

    try:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

        if not Psycopg2Instrumentor().is_instrumented_by_opentelemetry:
            # skip_dep_check: a imagem usa o pacote psycopg2-binary, e a checagem de
            # dependência procura o nome "psycopg2" e desligaria a instrumentação.
            Psycopg2Instrumentor().instrument(skip_dep_check=True)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("[telemetry] instrumentação do psycopg2 desativada: %s", exc)


# ---------------------------------------------------------------------------
# Logs: trace_id/span_id no formato do log
# ---------------------------------------------------------------------------
class TraceContextFilter(logging.Filter):
    """
    Preenche `record.otel` com " trace_id=... span_id=..." quando há um span ativo
    (e vazio fora de requisição), para pular do log no Loki para o trace no APM.
    """

    def filter(self, record):
        record.otel = ""
        if _OTEL_AVAILABLE:
            ctx = _trace_api.get_current_span().get_span_context()
            if ctx.is_valid:
                record.otel = f" trace_id={ctx.trace_id:032x} span_id={ctx.span_id:016x}"
        return True


def install_log_correlation():
    """Acrescenta %(otel)s ao formato dos handlers do logger raiz (idempotente)."""
    for handler in logging.getLogger().handlers:
        if any(isinstance(f, TraceContextFilter) for f in handler.filters):
            continue
        handler.addFilter(TraceContextFilter())
        atual = handler.formatter or logging.Formatter(logging.BASIC_FORMAT)
        handler.setFormatter(logging.Formatter((atual._fmt or "%(message)s") + "%(otel)s", atual.datefmt))


# ---------------------------------------------------------------------------
# Spans manuais (Service Bus e Cosmos DB, no analytics-service)
# ---------------------------------------------------------------------------
def _texto(valor):
    return valor.decode("utf-8", "replace") if isinstance(valor, (bytes, bytearray)) else str(valor)


@contextlib.contextmanager
def consumer_span(queue_name, application_properties):
    """
    Span de consumo de uma mensagem do Service Bus. Continua o trace do produtor
    (evaluation-service) a partir do `traceparent` nas application_properties — o SDK
    pode entregar chaves e valores como bytes. Sem OTel, devolve None.
    """
    if not _OTEL_AVAILABLE:
        yield None
        return

    carrier = {_texto(k): _texto(v) for k, v in (application_properties or {}).items()}
    if "traceparent" not in carrier and "Diagnostic-Id" in carrier:
        carrier["traceparent"] = carrier["Diagnostic-Id"]  # convenção dos SDKs Azure

    with _trace_api.get_tracer(TRACER_NAME).start_as_current_span(
        f"{queue_name} process",
        context=_propagate.extract(carrier),
        kind=SpanKind.CONSUMER,
        attributes={
            "messaging.system": "servicebus",
            "messaging.destination.name": queue_name,
            "messaging.operation.type": "process",
        },
    ) as span:
        yield span


@contextlib.contextmanager
def client_span(name, attributes=None):
    """
    Span de cliente para dependências sem instrumentação automática (ex.: Cosmos DB).
    Exceção que atravessa o bloco fica registrada no span, que é marcado como erro.
    """
    if not _OTEL_AVAILABLE:
        yield None
        return

    with _trace_api.get_tracer(TRACER_NAME).start_as_current_span(
        name, kind=SpanKind.CLIENT, attributes=attributes or {}
    ) as span:
        yield span


def mark_span_error(span, description):
    """Marca o span como erro sem exceção (ex.: mensagem devolvida para a fila)."""
    if span is not None and _OTEL_AVAILABLE:
        span.set_status(StatusCode.ERROR, description)


# ---------------------------------------------------------------------------
# Métricas HTTP (e span de servidor) para Flask
# ---------------------------------------------------------------------------
_KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def instrument_flask(app, skip_paths=("/health",)):
    """
    Registra hooks no Flask que medem taxa, erros (via status) e latência e, com os
    traces ligados, cria o span de servidor de cada requisição.

    /health fica de fora por padrão: as sondas do kubelet (a cada ~10s por pod)
    poluiriam a taxa de requisições. OTEL_METRICS_INCLUDE_HEALTH=true reativa nas
    métricas; nos traces o /health fica sempre de fora.
    """
    if _tracing_enabled:
        _trace_flask(app)

    if os.getenv("OTEL_METRICS_INCLUDE_HEALTH", "").lower() == "true":
        skip_paths = ()

    meter = get_meter()
    requests_total = meter.create_counter(
        "http.server.requests", unit="{request}", description="Total de requisições HTTP recebidas"
    )
    duration = meter.create_histogram(
        "http.server.request.duration", unit="s", description="Duração das requisições HTTP"
    )
    active = meter.create_up_down_counter(
        "http.server.active_requests", unit="{request}", description="Requisições HTTP em andamento"
    )

    @app.before_request
    def _tm_start():
        if request.path in skip_paths:
            g._tm_skip = True
            return
        g._tm_skip = False
        g._tm_start = time.perf_counter()
        active.add(1)

    @app.after_request
    def _tm_record(response):
        if getattr(g, "_tm_skip", True) or not hasattr(g, "_tm_start"):
            return response
        method = request.method if request.method in _KNOWN_METHODS else "OTHER"
        route = request.url_rule.rule if request.url_rule is not None else "unmatched"
        status = response.status_code
        attrs = {
            "method": method,
            "route": route,
            "status_code": str(status),
            "status_class": f"{status // 100}xx",
        }
        requests_total.add(1, attrs)
        duration.record(time.perf_counter() - g._tm_start, attrs)
        g._tm_recorded = True
        return response

    @app.teardown_request
    def _tm_end(_exc):
        if getattr(g, "_tm_skip", True) or not hasattr(g, "_tm_start"):
            return
        active.add(-1)

    return app


def _trace_flask(app):
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor

        FlaskInstrumentor().instrument_app(
            app, excluded_urls=TRACE_EXCLUDED_URLS, meter_provider=NoOpMeterProvider()
        )
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("[telemetry] traces do Flask desativados: %s", exc)


# ---------------------------------------------------------------------------
# Métricas de operação (CRUD) — usadas por flag-service e targeting-service
# ---------------------------------------------------------------------------
def outcome_from_status(status):
    if status < 400:
        return "success"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status < 500:
        return "client_error"
    return "error"


def tracked_operation(counter, histogram, operation):
    """
    Decorator para handlers Flask: conta a operação por desfecho (derivado do status
    HTTP devolvido) e mede sua duração. Aplique ABAIXO de @require_auth para medir só
    o handler, sem o tempo da validação da chave.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            outcome = "error"
            try:
                rv = func(*args, **kwargs)
                if isinstance(rv, tuple) and len(rv) > 1 and isinstance(rv[1], int):
                    status = rv[1]
                else:
                    status = getattr(rv, "status_code", 200)
                outcome = outcome_from_status(status)
                return rv
            finally:
                counter.add(1, {"operation": operation, "outcome": outcome})
                histogram.record(time.perf_counter() - start, {"operation": operation})

        return wrapper

    return decorator
