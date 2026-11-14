import os
import sys
import psycopg2
import requests
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from functools import wraps
import logging
import random
import time

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

import telemetry

# Configura o logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Carrega .env para desenvolvimento local
load_dotenv() 

app = Flask(__name__)

# --- Telemetria (traces -> APM, métricas -> Prometheus) ---
# No-op se OTEL_EXPORTER_OTLP_ENDPOINT não estiver definido. Precisa vir antes do
# pool de conexões: a instrumentação do psycopg2 só pega conexões abertas depois dela.
telemetry.init_telemetry("flag-service")
telemetry.instrument_flask(app)

_meter = telemetry.get_meter()
flag_operations = _meter.create_counter(
    "flag.operations", unit="{operation}",
    description="Operações CRUD por desfecho (success|not_found|conflict|client_error|error)")
flag_operation_duration = _meter.create_histogram(
    "flag.operation.duration", unit="s",
    description="Duração das operações CRUD")
auth_validations = _meter.create_counter(
    "flag.auth.validations", unit="{validation}",
    description="Validações de chave no auth-service (valid|invalid|missing_header|timeout|unavailable)")
auth_validation_duration = _meter.create_histogram(
    "flag.auth.validation.duration", unit="s",
    description="Latência da chamada ao auth-service /validate")


def _tracked(operation):
    return telemetry.tracked_operation(flag_operations, flag_operation_duration, operation)


# --- Configuração ---
DATABASE_URL = os.getenv("DATABASE_URL")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL")

if not DATABASE_URL or not AUTH_SERVICE_URL:
    log.critical("Erro: DATABASE_URL e AUTH_SERVICE_URL devem ser definidos.")
    sys.exit(1)

# --- Pool de Conexão com o Banco ---
# Inicializa o pool de conexões (Mín: 1, Máx: 5 conexões)
try:
    # RealDictCursor no pool, e não em cada conn.cursor(...): passado na chamada,
    # ele contorna a instrumentação do psycopg2 e a query não vira span no APM.
    pool = SimpleConnectionPool(1, 5, dsn=DATABASE_URL, cursor_factory=RealDictCursor)
    log.info("Pool de conexões com o PostgreSQL inicializado.")
except psycopg2.OperationalError as e:
    log.critical(f"Erro fatal ao conectar ao PostgreSQL: {e}")
    sys.exit(1)

# --- Middleware de Autenticação ---
def require_auth(f):
    """ Middleware para validar a chave de API contra o auth-service """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            auth_validations.add(1, {"outcome": "missing_header"})
            return jsonify({"error": "Authorization header obrigatório"}), 401

        started = time.perf_counter()
        try:
            # Chama o /validate do auth-service
            validate_url = f"{AUTH_SERVICE_URL}/validate"
            response = requests.get(validate_url, headers={"Authorization": auth_header}, timeout=3)

            if response.status_code != 200:
                auth_validations.add(1, {"outcome": "invalid"})
                log.warning(f"Falha na validação da chave (status: {response.status_code})")
                return jsonify({"error": "Chave de API inválida"}), 401

        except requests.exceptions.Timeout:
            auth_validations.add(1, {"outcome": "timeout"})
            log.error("Timeout ao conectar com o auth-service")
            return jsonify({"error": "Serviço de autenticação indisponível (timeout)"}), 504 # Gateway Timeout
        except requests.exceptions.RequestException as e:
            auth_validations.add(1, {"outcome": "unavailable"})
            log.error(f"Erro ao conectar com o auth-service: {e}")
            return jsonify({"error": "Serviço de autenticação indisponível"}), 503 # Service Unavailable
        finally:
            auth_validation_duration.record(time.perf_counter() - started)

        auth_validations.add(1, {"outcome": "valid"})
        # Se a chave for válida, continua para a rota
        return f(*args, **kwargs)
    return decorated

# --- Injeção de falha (caos) para a demonstração do incidente ---
# Liga uma falha que vive só no contêiner: um arquivo em /tmp, lido por todos os
# workers do gunicorn. Com ela ligada, toda rota (menos /health e a própria rota de
# caos) responde 500, como se o banco tivesse caído. O `kubectl rollout restart`
# do self-healer sobe um contêiner novo, sem o arquivo, e o serviço volta ao normal:
# é a falha que o restart cura. O /health continua 200 de propósito, para a liveness
# probe não reiniciar o pod antes do alerta (senão o self-healing não aparece).
CHAOS_FILE = os.getenv("CHAOS_FILE", "/tmp/togglemaster-chaos")
CHAOS_ROUTE = "/flags/_chaos"


class ChaosError(Exception):
    """Falha injetada de propósito pela rota de caos."""


def chaos_error_rate():
    """Fração das requisições que devem falhar (0 = caos desligado)."""
    try:
        with open(CHAOS_FILE, encoding="utf-8") as f:
            return min(max(float(f.read().strip() or 1), 0.0), 1.0)
    except (OSError, ValueError):
        return 0.0


@app.before_request
def inject_chaos():
    if request.path in ("/health", CHAOS_ROUTE):
        return None
    rate = chaos_error_rate()
    if rate > 0 and random.random() < rate:
        raise ChaosError("falha injetada: banco de dados indisponível (caos ligado)")
    return None


@app.errorhandler(ChaosError)
def chaos_failure(exc):
    span = trace.get_current_span()
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))
    log.error(f"Erro ao processar {request.method} {request.path}: {exc}")
    return jsonify({"error": "Erro interno do servidor", "details": str(exc)}), 500


@app.route(CHAOS_ROUTE, methods=['GET'])
@require_auth
def chaos_status():
    rate = chaos_error_rate()
    return jsonify({"chaos": rate > 0, "error_rate": rate})


@app.route(CHAOS_ROUTE, methods=['POST'])
@require_auth
def chaos_on():
    """ Liga a falha. Corpo opcional: {"error_rate": 0.0-1.0} (padrão 1.0 = tudo falha) """
    data = request.get_json(silent=True) or {}
    try:
        rate = float(data.get("error_rate", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "'error_rate' deve ser um número entre 0 e 1"}), 400
    if not 0 < rate <= 1:
        return jsonify({"error": "'error_rate' deve ser um número entre 0 e 1"}), 400
    with open(CHAOS_FILE, "w", encoding="utf-8") as f:
        f.write(str(rate))
    log.warning(f"CAOS LIGADO: {rate:.0%} das requisições vão responder 500 até o restart do pod.")
    return jsonify({"chaos": True, "error_rate": rate}), 200


@app.route(CHAOS_ROUTE, methods=['DELETE'])
@require_auth
def chaos_off():
    try:
        os.remove(CHAOS_FILE)
    except FileNotFoundError:
        pass
    log.warning("Caos desligado manualmente.")
    return jsonify({"chaos": False, "error_rate": 0.0}), 200

# --- Endpoints da API ---

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/flags', methods=['POST'])
@require_auth
@_tracked("create")
def create_flag():
    """ Cria uma nova definição de feature flag """
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({"error": "'name' é obrigatório"}), 400
    
    name = data['name']
    description = data.get('description', '')
    is_enabled = data.get('is_enabled', False)
    
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO flags (name, description, is_enabled, created_at, updated_at) "
            "VALUES (%s, %s, %s, NOW(), NOW()) RETURNING *",
            (name, description, is_enabled)
        )
        new_flag = cur.fetchone()
        conn.commit()
        log.info(f"Flag '{name}' criada com sucesso.")
        return jsonify(new_flag), 201
    except psycopg2.IntegrityError:
        if conn: conn.rollback()
        log.warning(f"Tentativa de criar flag duplicada: '{name}'")
        return jsonify({"error": f"Flag '{name}' já existe"}), 409
    except Exception as e:
        if conn: conn.rollback()
        log.error(f"Erro ao criar flag: {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)

@app.route('/flags', methods=['GET'])
@require_auth
@_tracked("list")
def get_flags():
    """ Lista todas as feature flags """
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM flags ORDER BY name")
        flags = cur.fetchall()
        return jsonify(flags)
    except Exception as e:
        log.error(f"Erro ao buscar flags: {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)

@app.route('/flags/<string:name>', methods=['GET'])
@require_auth
@_tracked("get")
def get_flag(name):
    """ Busca uma feature flag específica pelo nome """
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM flags WHERE name = %s", (name,))
        flag = cur.fetchone()
        if not flag:
            return jsonify({"error": "Flag não encontrada"}), 404
        return jsonify(flag)
    except Exception as e:
        log.error(f"Erro ao buscar flag '{name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)

@app.route('/flags/<string:name>', methods=['PUT'])
@require_auth
@_tracked("update")
def update_flag(name):
    """ Atualiza uma feature flag (descrição ou status 'is_enabled') """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Corpo da requisição obrigatório"}), 400

    fields = []
    values = []
    
    # Constrói a query dinamicamente
    if 'description' in data:
        fields.append("description = %s")
        values.append(data['description'])
    if 'is_enabled' in data:
        fields.append("is_enabled = %s")
        values.append(data['is_enabled'])
    
    if not fields:
        return jsonify({"error": "Pelo menos um campo ('description', 'is_enabled') é obrigatório"}), 400
    
    values.append(name) # Adiciona o 'name' para a cláusula WHERE
    
    query = f"UPDATE flags SET {', '.join(fields)} WHERE name = %s RETURNING *"
    
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(query, tuple(values))
        
        if cur.rowcount == 0:
            return jsonify({"error": "Flag não encontrada"}), 404
            
        updated_flag = cur.fetchone()
        conn.commit()
        log.info(f"Flag '{name}' atualizada com sucesso.")
        return jsonify(updated_flag), 200
    except Exception as e:
        if conn: conn.rollback()
        log.error(f"Erro ao atualizar flag '{name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)

@app.route('/flags/<string:name>', methods=['DELETE'])
@require_auth
@_tracked("delete")
def delete_flag(name):
    """ Deleta uma feature flag """
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("DELETE FROM flags WHERE name = %s", (name,))
        
        if cur.rowcount == 0:
            return jsonify({"error": "Flag não encontrada"}), 404
            
        conn.commit()
        log.info(f"Flag '{name}' deletada com sucesso.")
        return "", 204 # 204 No Content
    except Exception as e:
        if conn: conn.rollback()
        log.error(f"Erro ao deletar flag '{name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: pool.putconn(conn)

if __name__ == '__main__':
    port = int(os.getenv("PORT", 8002))
    app.run(host='0.0.0.0', port=port, debug=False)