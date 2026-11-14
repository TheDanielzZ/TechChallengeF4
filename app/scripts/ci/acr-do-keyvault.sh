#!/bin/sh
# =============================================================================
# Imprime no stdout o nome do ACR do ambiente, lido do Key Vault [Infra CI].
#
# O Terraform grava o segredo `acr-name` nesse cofre a cada apply
# (IaC/modules/keyvault.tf, local.infra_secrets). Como o ACR e o cofre têm
# sufixo aleatório no nome e mudam quando o ambiente é recriado, os pipelines
# de CI leem o nome daqui em vez de depender de um valor fixo no Jenkinsfile.
#
# O cofre é achado pelo padrão do nome (kv-<projeto>-inf-<sufixo>) dentro do
# resource group do ambiente (rg-<projeto>-<ambiente>).
#
# Roda dentro da imagem mcr.microsoft.com/azure-cli. Variáveis esperadas:
#   AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   App Registration (credencial azure-service-principal)
#   ARM_TENANT_ID, ARM_SUBSCRIPTION_ID     credenciais arm-tenant-id e arm-subscription-id
#   RG_AMBIENTE                            ex.: rg-togglemaster-prod
#
# Tudo que não é o nome do ACR vai para o stderr, para o Jenkins poder usar
# o stdout direto (sh returnStdout).
# =============================================================================
set -eu

: "${AZURE_CLIENT_ID:?AZURE_CLIENT_ID não definido}"
: "${AZURE_CLIENT_SECRET:?AZURE_CLIENT_SECRET não definido}"
: "${ARM_TENANT_ID:?ARM_TENANT_ID não definido}"
: "${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID não definido}"
: "${RG_AMBIENTE:?RG_AMBIENTE não definido}"

az login --service-principal \
    --username "$AZURE_CLIENT_ID" --password "$AZURE_CLIENT_SECRET" \
    --tenant "$ARM_TENANT_ID" --output none >&2
az account set --subscription "$ARM_SUBSCRIPTION_ID" >&2

KV=$(az keyvault list --resource-group "$RG_AMBIENTE" --query "[].name" --output tsv | grep -e '-inf-' | head -n 1 || true)
if [ -z "$KV" ]; then
    echo "ERRO: nenhum Key Vault [Infra CI] (kv-*-inf-*) no resource group $RG_AMBIENTE." >&2
    echo "      O ambiente existe? Rode o pipeline de infra ou informe o parâmetro ACR_NAME." >&2
    exit 1
fi

ACR=$(az keyvault secret show --vault-name "$KV" --name acr-name --query value --output tsv)
case "$ACR" in
    '' | *[!A-Za-z0-9]*)
        echo "ERRO: segredo acr-name vazio ou inválido no Key Vault $KV: '$ACR'" >&2
        exit 1
        ;;
esac

echo "ACR lido do Key Vault $KV (segredo acr-name): $ACR" >&2
printf '%s\n' "$ACR"
