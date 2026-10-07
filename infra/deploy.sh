#!/usr/bin/env bash
# Manual infrastructure deploy (docs/deployment.md §8).
#
# Use this instead of calling `az deployment group create` directly. CI/CD owns
# the running images and tags them with commit SHAs; infra/main.bicepparam only
# holds fallback tags. Deploying Bicep with those fallbacks would silently roll
# both apps back to old images, so this reads the tags actually running and
# passes them through.
#
# Extra arguments are forwarded as parameter overrides, e.g.:
#   infra/deploy.sh ollamaMinReplicas=0

set -euo pipefail

cd "$(dirname "$0")/.."

RG=${RG:-rg-ai-fiqh-learn}
ME=$(az ad signed-in-user show --query id -o tsv)

running_tag() {
  az containerapp show -n "$1" -g "$RG" \
    --query "properties.template.containers[0].image" -o tsv 2>/dev/null \
    | sed 's/.*://'
}

WEB_TAG=$(running_tag ai-fiqh-web || true)
OLLAMA_TAG=$(running_tag ai-fiqh-ollama || true)

overrides=()
[ -n "$WEB_TAG" ] && overrides+=("webImageTag=$WEB_TAG")
[ -n "$OLLAMA_TAG" ] && overrides+=("ollamaImageTag=$OLLAMA_TAG")

echo "Keeping running images: web=${WEB_TAG:-<not deployed — params file tag>} ollama=${OLLAMA_TAG:-<not deployed — params file tag>}"

# ${arr[@]+"${arr[@]}"} rather than "${arr[@]}": macOS ships bash 3.2, which
# treats an empty array as unbound under `set -u`.
az deployment group create \
  --name "ai-fiqh-$(date +%Y%m%d-%H%M%S)" \
  --resource-group "$RG" \
  --parameters infra/main.bicepparam \
  --parameters deployApps=true deployerPrincipalId="$ME" \
    ${overrides[@]+"${overrides[@]}"} "$@"
