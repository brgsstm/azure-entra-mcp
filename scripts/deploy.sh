#!/usr/bin/env bash
# Build the image in ACR (no local Docker needed) and roll the Container App.
#
#   ./scripts/deploy.sh            # first run creates everything
#   ./scripts/deploy.sh            # later runs rebuild + roll a new revision
#
# Requires: az CLI (logged in), terraform, uv, run from the repo root.
set -euo pipefail

cd "$(dirname "$0")/.."
TF="terraform -chdir=infra"
TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"

# 0. Test against exactly what the image will install. --locked fails if uv.lock is
#    out of date with pyproject.toml; the contract tests catch FastMCP internals drift.
echo "==> Running tests"
uv run --locked pytest -q

$TF init -input=false >/dev/null

# 1. Registry must exist before we can build into it. On the first run this also
#    creates the resource group; on later runs it's a no-op.
#    Test for an empty value: with no state, newer Terraform exits 0 here.
if [ -z "$($TF output -raw acr_name 2>/dev/null)" ]; then
  echo "==> First run: creating the container registry"
  $TF apply -input=false -auto-approve \
    -target=azurerm_resource_group.this -target=azurerm_container_registry.this
fi
ACR=$($TF state show -no-color azurerm_container_registry.this | awk -F' = ' '/^ *name /{gsub(/"/,"",$2); print $2; exit}')

# 2. Build in ACR
echo "==> Building azure-entra-mcp:${TAG} in ${ACR}"
az acr build --registry "$ACR" --image "azure-entra-mcp:${TAG}" --image "azure-entra-mcp:latest" .

# 3. Apply everything with the new tag
echo "==> Applying infrastructure (image tag ${TAG})"
$TF apply -input=false -var "image_tag=${TAG}"

echo
echo "MCP server URL: $($TF output -raw mcp_url)"
