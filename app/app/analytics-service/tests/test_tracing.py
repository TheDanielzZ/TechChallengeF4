"""
Traces do consumo da fila: a gravação no Cosmos DB precisa aparecer no MESMO trace do
/evaluate que publicou o evento. Os spans vão para um exportador em memória.
"""

import pytest
from azure.cosmos import exceptions as cosmos_exceptions
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from conftest import MensagemFake, evento_valido

_EXPORTADOR = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTADOR))
trace.set_tracer_provider(_PROVIDER)

# O evaluation-service grava o traceparent nas ApplicationProperties da mensagem.
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


class MensagemComContexto(MensagemFake):
    def __init__(self, corpo, propriedades):
        super().__init__(corpo)
        self.application_properties = propriedades


@pytest.fixture
def spans():
    _EXPORTADOR.clear()
    yield _EXPORTADOR
    _EXPORTADOR.clear()


def _mensagem(**sobrescritas):
    import json

    # Chaves e valores em bytes: é como o SDK do Service Bus entrega as propriedades.
    return MensagemComContexto(json.dumps(evento_valido(**sobrescritas)), {b"traceparent": TRACEPARENT.encode()})


def _por_tipo(spans, kind):
    return [s for s in spans.get_finished_spans() if s.kind == kind]


def test_consumo_continua_o_trace_do_evaluate_e_grava_no_cosmos_como_filho(modulo, cosmos, spans):
    assert modulo.process_message(_mensagem()) is True

    (consumo,) = _por_tipo(spans, SpanKind.CONSUMER)
    (gravacao,) = _por_tipo(spans, SpanKind.CLIENT)

    assert f"{consumo.context.trace_id:032x}" == TRACE_ID
    assert f"{consumo.parent.span_id:016x}" == SPAN_ID
    assert gravacao.parent.span_id == consumo.context.span_id
    assert gravacao.attributes["db.system"] == "cosmosdb"
    assert consumo.attributes["toggle.outcome"] == "success"
    assert consumo.status.status_code != StatusCode.ERROR


def test_falha_no_cosmos_marca_erro_nos_dois_spans(modulo, cosmos, spans):
    cosmos.create_item.side_effect = cosmos_exceptions.CosmosHttpResponseError(message="throttled")

    assert modulo.process_message(_mensagem()) is False

    (consumo,) = _por_tipo(spans, SpanKind.CONSUMER)
    (gravacao,) = _por_tipo(spans, SpanKind.CLIENT)
    assert gravacao.status.status_code == StatusCode.ERROR
    assert consumo.status.status_code == StatusCode.ERROR
    assert consumo.attributes["toggle.outcome"] == "cosmos_error"


def test_mensagem_malformada_nao_e_erro_no_trace(modulo, cosmos, spans):
    mensagem = MensagemComContexto("isso não é json", {"traceparent": TRACEPARENT})

    assert modulo.process_message(mensagem) is True

    (consumo,) = _por_tipo(spans, SpanKind.CONSUMER)
    assert consumo.attributes["toggle.outcome"] == "discarded"
    assert consumo.status.status_code != StatusCode.ERROR
    assert _por_tipo(spans, SpanKind.CLIENT) == []


def test_mensagem_sem_propriedades_abre_trace_proprio(modulo, cosmos, spans, mensagem):
    assert modulo.process_message(mensagem.criar()) is True

    (consumo,) = _por_tipo(spans, SpanKind.CONSUMER)
    assert consumo.parent is None
