#!/usr/bin/env bash
# Real end-to-end test: Azure → Event Hub → Function → Dynatrace.
#
# Creates a throw-away resource group containing
#   - an Event Hub namespace + hub
#   - this Function (Flex Consumption), deployed from this repo
#   - Key Vault "dcr":  metrics exported through a DATA COLLECTION RULE
#   - Key Vault "diag": metrics exported through a DIAGNOSTIC SETTING
# generates Key Vault traffic, then polls Dynatrace until the metrics from BOTH vaults have arrived.
#
# Usage:
#   cp .env.example .env.local   # git-ignored; set DT_ENVIRONMENT (classic API URL) and DT_TOKEN
#                                # (one token with metrics.ingest + storage:metrics:read) - or export them
#   az login && az account set --subscription <id>
#   ./scripts/e2e-azure.sh up        # provision + deploy (~10 min)
#   ./scripts/e2e-azure.sh verify    # generate traffic and wait for data in Dynatrace (typically 10–40 min,
#                                    # a new diagnostic setting can take up to 90 min)
#   ./scripts/e2e-azure.sh down      # delete everything
#   ./scripts/e2e-azure.sh all       # up + verify + down
#
# Needs: az CLI (logged in; Owner/User Access Administrator on the subscription for role assignments),
#        Azure Functions Core Tools (func), curl, python. Works in Git Bash / WSL / Linux / macOS.
# Not part of the Function itself; costs a few cents (Event Hubs Standard, storage, Key Vault operations).
# NOTE: this script has not been run by its author; expect to adjust az flags for your CLI version.

set -euo pipefail
export MSYS_NO_PATHCONV=1          # Git Bash on Windows would otherwise rewrite /subscriptions/... arguments

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.e2e-state.env"

# DT_ENVIRONMENT / DT_TOKEN: taken from the environment, else from the git-ignored .env.local (see .env.example)
if [ -f "$ROOT/.env.local" ]; then
  while IFS='=' read -r k v; do
    v="${v%$'\r'}"; k="${k//[[:space:]]/}"
    case "$k" in ''|\#*) continue ;; esac
    [ -n "$v" ] && [ -z "${!k:-}" ] && export "$k=$v"
  done < "$ROOT/.env.local"
fi

LOCATION="${LOCATION:-westeurope}"          # everything must be in one region (DCR / diagnostic setting requirement)
MAX_WAIT_MIN="${MAX_WAIT_MIN:-90}"
DCR_API_VERSION="${DCR_API_VERSION:-2023-03-11}"
PY="$(command -v python3 || command -v python)"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing required tool: $1" >&2; exit 1; }; }

load_state() { [ -f "$STATE" ] && . "$STATE" || { echo "no state file; run 'up' first" >&2; exit 1; }; }

# --------------------------------------------------------------------------------------------------------------
up() {
  need az; need func; need curl
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"

  PREFIX="dtfwd$RANDOM"
  RG="rg-$PREFIX"; NS="$PREFIX-ns"; HUB="metrics"; ST="${PREFIX}st"; FUNC="$PREFIX-func"
  KV_DCR="$PREFIX-dcr"; KV_DIAG="$PREFIX-diag"; DCR="$PREFIX-dcr-rule"
  SUB="$(az account show --query id -o tsv)"

  log "Registering resource providers"
  for p in Microsoft.Insights Microsoft.EventHub Microsoft.KeyVault Microsoft.Web Microsoft.Storage; do
    az provider register --namespace "$p" --wait -o none
  done

  log "Resource group + Event Hub ($RG, $LOCATION)"
  az group create -n "$RG" -l "$LOCATION" -o none
  az eventhubs namespace create -g "$RG" -n "$NS" -l "$LOCATION" --sku Standard -o none
  az eventhubs eventhub create -g "$RG" --namespace-name "$NS" -n "$HUB" --partition-count 2 -o none
  NS_ID="$(az eventhubs namespace show -g "$RG" -n "$NS" --query id -o tsv)"
  HUB_ID="$(az eventhubs eventhub show -g "$RG" --namespace-name "$NS" -n "$HUB" --query id -o tsv)"

  log "Two Key Vaults to generate metrics (one per export method)"
  for kv in "$KV_DCR" "$KV_DIAG"; do
    az keyvault create -g "$RG" -n "$kv" -l "$LOCATION" --enable-rbac-authorization false -o none
  done
  KV_DCR_ID="$(az keyvault show -n "$KV_DCR" --query id -o tsv)"
  KV_DIAG_ID="$(az keyvault show -n "$KV_DIAG" --query id -o tsv)"

  log "Function app (Flex Consumption) with managed identity"
  az storage account create -g "$RG" -n "$ST" -l "$LOCATION" --sku Standard_LRS -o none
  az functionapp create -g "$RG" -n "$FUNC" --storage-account "$ST" \
    --flexconsumption-location "$LOCATION" --runtime python --runtime-version 3.11 -o none
  FUNC_PRINCIPAL="$(az functionapp identity assign -g "$RG" -n "$FUNC" --query principalId -o tsv)"
  az role assignment create --assignee-object-id "$FUNC_PRINCIPAL" --assignee-principal-type ServicePrincipal \
    --role "Azure Event Hubs Data Receiver" --scope "$NS_ID" -o none
  # test only: for real deployments keep the token in Key Vault and use a Key Vault reference
  az functionapp config appsettings set -g "$RG" -n "$FUNC" -o none --settings \
    "DT_ENVIRONMENT=$DT_ENVIRONMENT" "DT_TOKEN=$DT_TOKEN" "EH_NAME=$HUB" \
    "EH_CONN__fullyQualifiedNamespace=$NS.servicebus.windows.net" "AZURE_LOCATION=$LOCATION"

  log "Deploying the Function code"
  (cd "$ROOT" && func azure functionapp publish "$FUNC" --python)

  log "Export method A: data collection rule (Key Vault '$KV_DCR')"
  DCR_URL="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Insights/dataCollectionRules/$DCR?api-version=$DCR_API_VERSION"
  DCR_PRINCIPAL="$(az rest --method put --url "$DCR_URL" --query identity.principalId -o tsv --body @- <<EOF
{
  "location": "$LOCATION",
  "kind": "PlatformTelemetry",
  "identity": { "type": "SystemAssigned" },
  "properties": {
    "dataSources": { "platformTelemetry": [ {
      "name": "keyvaultMetrics",
      "streams": [ "Microsoft.KeyVault/vaults:Metrics-Group-All" ] } ] },
    "destinations": { "eventHubsDirect": [ { "name": "eh", "eventHubResourceId": "$HUB_ID" } ] },
    "dataFlows": [ {
      "streams": [ "Microsoft.KeyVault/vaults:Metrics-Group-All" ],
      "destinations": [ "eh" ] } ]
  }
}
EOF
)"
  DCR_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Insights/dataCollectionRules/$DCR"
  az role assignment create --assignee-object-id "$DCR_PRINCIPAL" --assignee-principal-type ServicePrincipal \
    --role "Azure Event Hubs Data Sender" --scope "$NS_ID" -o none
  az monitor data-collection rule association create --name "$PREFIX-assoc" --rule-id "$DCR_ID" --resource "$KV_DCR_ID" -o none

  log "Export method B: diagnostic setting (Key Vault '$KV_DIAG')"
  az monitor diagnostic-settings create --name "$PREFIX-diag" --resource "$KV_DIAG_ID" \
    --metrics '[{"category":"AllMetrics","enabled":true}]' \
    --event-hub-rule "$NS_ID/authorizationrules/RootManageSharedAccessKey" --event-hub "$HUB" -o none

  cat > "$STATE" <<EOF
PREFIX=$PREFIX
RG=$RG
NS_ID=$NS_ID
KV_DCR=$KV_DCR
KV_DIAG=$KV_DIAG
FUNC=$FUNC
EOF
  log "Waiting 60 s for role assignments to propagate"
  sleep 60
  echo "Provisioned. State saved in $STATE. Next: ./scripts/e2e-azure.sh verify"
}

# --------------------------------------------------------------------------------------------------------------
generate_traffic() {   # Key Vault API hits produce ServiceApiHit / ServiceApiLatency metrics
  for kv in "$KV_DCR" "$KV_DIAG"; do
    for i in 1 2 3 4 5; do
      az keyvault secret set --vault-name "$kv" --name probe --value "$RANDOM" -o none
      az keyvault secret show --vault-name "$kv" --name probe -o none
    done
  done
}

query_dynatrace() {    # prints the resource ids (lower-case) that currently have data for the probe metric (DQL API)
  "$PY" "$ROOT/scripts/dql.py" --field "azure.resource.id" \
    "timeseries s = sum(\`cloud.azure.microsoft_keyvault.vaults.ServiceApiHit-realtime\`), by:{azure.resource.id}, filter: azure.resource.group == \"${RG,,}\", from: now()-3h | fieldsAdd total = arraySum(s) | filter total > 0 | fields azure.resource.id"
}

verify() {
  need az
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  load_state

  log "Generating Key Vault traffic and polling Dynatrace (max ${MAX_WAIT_MIN} min)"
  local deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 )) dcr_ok=0 diag_ok=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    generate_traffic
    local found; found="$(query_dynatrace || true)"
    echo "$found" | grep -qi "/vaults/$KV_DCR\$"  && dcr_ok=1
    echo "$found" | grep -qi "/vaults/$KV_DIAG\$" && diag_ok=1
    echo "$(date +%H:%M:%S)  DCR export: $([ $dcr_ok = 1 ] && echo RECEIVED || echo waiting)   diagnostic setting export: $([ $diag_ok = 1 ] && echo RECEIVED || echo waiting)"
    [ $dcr_ok = 1 ] && [ $diag_ok = 1 ] && { log "SUCCESS: both export methods delivered metrics to Dynatrace"; return 0; }
    sleep 60
  done

  log "TIMEOUT: not everything arrived. Where to look:"
  cat <<EOF
  1. Event Hub received data?   az monitor metrics list --resource "$NS_ID" --metric IncomingMessages --interval PT5M --offset 2h -o table
  2. Function ran / failed?     Portal → $FUNC → Monitor, or: az monitor app-insights query ... (traces / exceptions)
  3. Dynatrace rejected lines?  Function failures with HTTP 400/401 show the ingest API error; check the token scopes
  4. DCR: $DCR_API_VERSION association present? az monitor data-collection rule association list --resource <vault id>
  5. Diagnostic settings can take up to 90 min after creation; increase MAX_WAIT_MIN
EOF
  return 1
}

# --------------------------------------------------------------------------------------------------------------
down() {
  need az
  load_state
  log "Deleting resource group $RG"
  az group delete -n "$RG" --yes --no-wait
  # Key Vaults are soft-deleted; names are random so purging is optional:
  #   az keyvault purge --name <name>
  rm -f "$STATE"
  echo "Deletion started (runs in the background)."
}

case "${1:-}" in
  up) up ;;
  verify) verify ;;
  down) down ;;
  all) up; rc=0; verify || rc=$?; down; exit $rc ;;
  *) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
