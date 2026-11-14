#!/bin/sh
# =============================================================================
# Parte 4.3 do README: criar os Secrets do Kubernetes a partir do
# Key Vault [APP CI].
#
# Os `secret.example.yaml` de ../../infra/base são modelos e ficam fora do
# kustomize de propósito — segredo não vai para o Git. A fonte de verdade é o
# cofre, preenchido pelo próprio Terraform.
#
# Idempotente: `create --dry-run=client | apply` atualiza o Secret existente em
# vez de falhar com AlreadyExists.
#
# Roda no container mcr.microsoft.com/azure-cli (precisa de az e kubectl).
# =============================================================================

. "$(dirname "$0")/_comum.sh"

carregar_ambiente
preparar_kubeconfig

log "Lendo segredos de ${KEY_VAULT_APP_NAME}"

garantir_namespace "$NAMESPACE_APPS"

# Aplica um Secret sem imprimir os valores. Todos os --from-literal chegam como
# argumentos posicionais, expandidos pelo shell e nunca ecoados.
aplicar_secret_em() {
    _ns="$1"
    _nome="$2"
    shift 2
    kubectl -n "$_ns" create secret generic "$_nome" "$@" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    info "secret ${_ns}/${_nome} aplicado"
}

aplicar_secret() {
    aplicar_secret_em "$NAMESPACE_APPS" "$@"
}

# O mapa abaixo é o mesmo da tabela "Secret no Key Vault -> Secret do
# Kubernetes" do README. Manter os dois em sincronia.

aplicar_secret auth-service-secret \
    --from-literal=DATABASE_URL="$(ler_segredo auth-service-database-url)" \
    --from-literal=MASTER_KEY="$(ler_segredo auth-master-key)"

aplicar_secret flag-service-secret \
    --from-literal=DATABASE_URL="$(ler_segredo flag-service-database-url)"

aplicar_secret targeting-service-secret \
    --from-literal=DATABASE_URL="$(ler_segredo targeting-service-database-url)"

aplicar_secret evaluation-service-secret \
    --from-literal=REDIS_URL="$(ler_segredo redis-url)" \
    --from-literal=SERVICE_API_KEY="$(ler_segredo evaluation-service-api-key)" \
    --from-literal=SERVICE_BUS_CONNECTION_STRING="$(ler_segredo servicebus-connection-string)"

aplicar_secret analytics-service-secret \
    --from-literal=SERVICE_BUS_CONNECTION_STRING="$(ler_segredo servicebus-connection-string)" \
    --from-literal=COSMOS_KEY="$(ler_segredo cosmos-key)"

# OTel Collector: license key do New Relic, lida pelo Collector como variável
# de ambiente (extraEnvs em infra/observability/otel-collector-values.yaml).
# Precisa existir antes de o Argo CD subir o Collector.
NAMESPACE_OBSERVABILITY="${NAMESPACE_OBSERVABILITY:-observability}"
garantir_namespace "$NAMESPACE_OBSERVABILITY"
aplicar_secret_em "$NAMESPACE_OBSERVABILITY" newrelic-license \
    --from-literal=NEW_RELIC_LICENSE_KEY="$(ler_segredo new-relic-license-key)"

# Grafana do kube-prometheus-stack: admin.existingSecret em
# infra/observability/kube-prometheus-stack-values.yaml. Sem este Secret o pod do
# Grafana fica em CreateContainerConfigError.
NAMESPACE_MONITORING="${NAMESPACE_MONITORING:-monitoring}"
garantir_namespace "$NAMESPACE_MONITORING"
aplicar_secret_em "$NAMESPACE_MONITORING" grafana-admin \
    --from-literal=admin-user=admin \
    --from-literal=admin-password="$(ler_segredo grafana-admin-password)"

# Alertmanager e self-healer: destinos das notificações. Montado como arquivos
# (alertmanager.alertmanagerSpec.secrets) em
# /etc/alertmanager/secrets/alertmanager-notificacoes/ e lido via webhook_url_file
# e routing_key_file — o valor nunca vai para o Git nem para a config renderizada.
aplicar_secret_em "$NAMESPACE_MONITORING" alertmanager-notificacoes \
    --from-literal=discord-webhook-url="$(ler_segredo discord-webhook-url)" \
    --from-literal=pagerduty-routing-key="$(ler_segredo pagerduty-routing-key)"

log 'Secrets do namespace toggle-apps'
kubectl -n "$NAMESPACE_APPS" get secrets \
    auth-service-secret flag-service-secret targeting-service-secret \
    evaluation-service-secret analytics-service-secret
kubectl -n "$NAMESPACE_OBSERVABILITY" get secret newrelic-license
kubectl -n "$NAMESPACE_MONITORING" get secret grafana-admin alertmanager-notificacoes
