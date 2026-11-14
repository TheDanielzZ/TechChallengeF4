"""
Testes do telemetry.py (métricas HTTP e de operação).

Usam um "meter" falso que registra as medições: não precisam de Collector, de rede
nem das bibliotecas opentelemetry. O telemetry.py é idêntico nos 3 serviços Python;
estes testes ficam só no flag-service.
"""

import collections
from unittest.mock import patch

from flask import Flask, jsonify

import telemetry


class _Instrumento:
    def __init__(self, nome, registro):
        self.nome, self.registro = nome, registro

    def add(self, valor, atributos=None):
        self.registro.append((self.nome, valor, dict(atributos or {})))

    record = add


class _MeterFalso:
    def __init__(self):
        self.registro = []

    def create_counter(self, nome, **_):
        return _Instrumento(nome, self.registro)

    create_histogram = create_up_down_counter = create_counter


def _app_instrumentado(meter):
    with patch.object(telemetry, "get_meter", return_value=meter):
        app = Flask(__name__)
        telemetry.instrument_flask(app)
        ops = meter.create_counter("ops")
        dur = meter.create_histogram("op.duration")

    @app.route("/health")
    def health():
        return jsonify(ok=1)

    @app.route("/flags/<string:name>")
    @telemetry.tracked_operation(ops, dur, "get")
    def get(name):
        return (jsonify(error="x"), 404) if name == "nope" else jsonify(ok=1)

    @app.route("/flags", methods=["POST"])
    @telemetry.tracked_operation(ops, dur, "create")
    def create():
        return jsonify(error="dup"), 409

    @app.route("/boom")
    @telemetry.tracked_operation(ops, dur, "boom")
    def boom():
        raise RuntimeError("falha proposital")

    return app


def _requisicoes(meter):
    return [
        (a["method"], a["route"], a["status_code"], a["status_class"])
        for n, _, a in meter.registro
        if n == "http.server.requests"
    ]


def test_metricas_http_usam_template_da_rota_e_ignoram_health():
    meter = _MeterFalso()
    client = _app_instrumentado(meter).test_client()

    client.get("/health")
    client.get("/flags/abc")
    client.get("/flags/nope")
    client.post("/flags")
    client.get("/nao-existe")

    reqs = _requisicoes(meter)
    assert not any(r[1] == "/health" for r in reqs)
    assert ("GET", "/flags/<string:name>", "200", "2xx") in reqs
    assert ("GET", "/flags/<string:name>", "404", "4xx") in reqs
    assert ("POST", "/flags", "409", "4xx") in reqs
    assert ("GET", "unmatched", "404", "4xx") in reqs  # sem explosão de cardinalidade


def test_excecao_nao_tratada_vira_5xx_e_requisicoes_ativas_voltam_a_zero():
    meter = _MeterFalso()
    app = _app_instrumentado(meter)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    client = app.test_client()

    client.get("/boom")
    client.get("/flags/abc")

    assert ("GET", "/boom", "500", "5xx") in _requisicoes(meter)
    ativas = sum(v for n, v, _ in meter.registro if n == "http.server.active_requests")
    assert ativas == 0
    duracoes = [n for n, _, _ in meter.registro if n == "http.server.request.duration"]
    assert len(duracoes) == len(_requisicoes(meter))


def test_tracked_operation_classifica_desfecho_pelo_status():
    meter = _MeterFalso()
    client = _app_instrumentado(meter).test_client()

    client.get("/flags/abc")
    client.get("/flags/nope")
    client.post("/flags")
    client.get("/boom")

    desfechos = collections.Counter(
        (a["operation"], a["outcome"]) for n, _, a in meter.registro if n == "ops"
    )
    assert desfechos[("get", "success")] == 1
    assert desfechos[("get", "not_found")] == 1
    assert desfechos[("create", "conflict")] == 1
    assert desfechos[("boom", "error")] == 1


def test_sem_endpoint_otlp_a_telemetria_e_noop(monkeypatch=None):
    with patch.dict("os.environ", {}, clear=False) as env:
        env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        env.pop("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", None)
        assert telemetry.init_telemetry("qualquer") is False
