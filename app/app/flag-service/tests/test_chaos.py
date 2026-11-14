"""
Testes da injeção de falha (rota /flags/_chaos), usada na demonstração do incidente:
liga o caos -> o serviço responde 500 -> alerta -> o self-healer reinicia o pod ->
o contêiner novo não tem o arquivo de caos e o serviço volta ao normal.
"""

from unittest.mock import MagicMock

import pytest

from conftest import AUTH_HEADERS

CHAOS = "/flags/_chaos"


@pytest.fixture(autouse=True)
def arquivo_de_caos(modulo, monkeypatch, tmp_path):
    """Cada teste usa o seu arquivo de caos (nada em /tmp de verdade)."""
    caminho = tmp_path / "chaos"
    monkeypatch.setattr(modulo, "CHAOS_FILE", str(caminho))
    return caminho


def test_sem_caos_as_rotas_funcionam(client, auth_ok, db):
    db.cursor.fetchall.return_value = [{"name": "demo"}]

    resposta = client.get("/flags", headers=AUTH_HEADERS)

    assert resposta.status_code == 200
    assert client.get(CHAOS, headers=AUTH_HEADERS).get_json() == {"chaos": False, "error_rate": 0.0}


def test_rota_de_caos_exige_autenticacao(client, arquivo_de_caos):
    assert client.post(CHAOS).status_code == 401
    assert client.delete(CHAOS).status_code == 401
    assert client.get(CHAOS).status_code == 401
    assert not arquivo_de_caos.exists()


def test_caos_ligado_faz_as_rotas_responderem_500(client, auth_ok, db, arquivo_de_caos):
    ligar = client.post(CHAOS, headers=AUTH_HEADERS)

    assert ligar.status_code == 200
    assert ligar.get_json() == {"chaos": True, "error_rate": 1.0}
    assert arquivo_de_caos.read_text() == "1.0"

    resposta = client.get("/flags/demo", headers=AUTH_HEADERS)
    assert resposta.status_code == 500
    assert "caos" in resposta.get_json()["details"]
    assert db.pool.getconn.call_count == 0, "a falha acontece antes de tocar no banco"


def test_caos_ligado_nao_derruba_health_nem_a_propria_rota(client, auth_ok):
    client.post(CHAOS, headers=AUTH_HEADERS)

    assert client.get("/health").status_code == 200
    assert client.get(CHAOS, headers=AUTH_HEADERS).get_json()["chaos"] is True


def test_restart_do_pod_cura_a_falha(client, auth_ok, db, arquivo_de_caos):
    client.post(CHAOS, headers=AUTH_HEADERS)
    assert client.get("/flags", headers=AUTH_HEADERS).status_code == 500

    arquivo_de_caos.unlink()  # contêiner novo depois do rollout restart: /tmp vazio

    assert client.get("/flags", headers=AUTH_HEADERS).status_code == 200


def test_delete_desliga_o_caos(client, auth_ok, arquivo_de_caos):
    client.post(CHAOS, headers=AUTH_HEADERS)

    resposta = client.delete(CHAOS, headers=AUTH_HEADERS)

    assert resposta.get_json() == {"chaos": False, "error_rate": 0.0}
    assert not arquivo_de_caos.exists()
    assert client.delete(CHAOS, headers=AUTH_HEADERS).status_code == 200, "desligar duas vezes não quebra"


def test_error_rate_parcial(client, auth_ok, db, modulo, monkeypatch):
    client.post(CHAOS, json={"error_rate": 0.25}, headers=AUTH_HEADERS)
    sorteios = iter([0.1, 0.9])
    monkeypatch.setattr(modulo.random, "random", lambda: next(sorteios))

    assert client.get("/flags", headers=AUTH_HEADERS).status_code == 500  # 0.1 < 0.25
    assert client.get("/flags", headers=AUTH_HEADERS).status_code == 200  # 0.9 >= 0.25


@pytest.mark.parametrize("corpo", [{"error_rate": 0}, {"error_rate": 1.5}, {"error_rate": "muito"}, {"error_rate": None}])
def test_error_rate_invalido_devolve_400(client, auth_ok, corpo, arquivo_de_caos):
    assert client.post(CHAOS, json=corpo, headers=AUTH_HEADERS).status_code == 400
    assert not arquivo_de_caos.exists()


@pytest.mark.parametrize("conteudo,esperado", [("", 1.0), ("0.5", 0.5), ("7", 1.0), ("-1", 0.0), ("lixo", 0.0)])
def test_leitura_do_arquivo_de_caos(modulo, arquivo_de_caos, conteudo, esperado):
    arquivo_de_caos.write_text(conteudo)

    assert modulo.chaos_error_rate() == esperado


def test_falha_injetada_marca_o_span_com_erro(client, auth_ok, modulo, monkeypatch):
    span = MagicMock(name="span")
    monkeypatch.setattr(modulo.trace, "get_current_span", lambda: span)
    client.post(CHAOS, headers=AUTH_HEADERS)

    client.get("/flags", headers=AUTH_HEADERS)

    span.record_exception.assert_called_once()
    assert span.set_status.call_args.args[0].status_code == modulo.StatusCode.ERROR
