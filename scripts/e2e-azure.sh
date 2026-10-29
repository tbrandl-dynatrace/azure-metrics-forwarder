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
#   ./scripts/e2e-azure.sh dcr       # (re)try only the DCR export, e.g. after an admin granted the role
#   ./scripts/e2e-azure.sh down      # delete everything
#   ./scripts/e2e-azure.sh all       # up + verify + down
#
# Needs: az CLI (logged in; Contributor is enough for everything except the DCR export, which needs a role
#        assignment (Owner / User Access Administrator) - without it the script tests diagnostic settings only),
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
# How the Function reads the Event Hub: "connection-string" (default, needs no role assignments) or "identity".
EH_AUTH="${EH_AUTH:-connection-string}"
PY="$(command -v python3 || command -v python)"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing required tool: $1" >&2; exit 1; }; }

save_state() {
  cat > "$STATE" <<EOF
PREFIX=$PREFIX
RG=$RG
NS_ID=${NS_ID:-}
KV_DCR=$KV_DCR
KV_DIAG=$KV_DIAG
FUNC=$FUNC
DCR_ENABLED=$DCR_ENABLED
EOF
}

load_state() { [ -f "$STATE" ] && . "$STATE" || { echo "no state file; run 'up' first" >&2; exit 1; }; }

# --------------------------------------------------------------------------------------------------------------
# Export method A. Azure checks at creation time that the DCR's identity may already write to the Event Hub, so the
# identity (a user-assigned one; a system-assigned identity only exists after the DCR) must get its role first.
setup_dcr() {     # note: callers use '|| ...', which disables 'set -e' in here, hence the explicit '|| return 1'
  local SUB HUB_ID KV_DCR_ID UAMI UAMI_ID UAMI_PRINCIPAL DCR DCR_URL DCR_ID NS_ID BODY out i
  SUB="$(az account show --query id -o tsv)" || return 1
  DCR="$PREFIX-dcr-rule"; UAMI="$PREFIX-dcr-identity"
  NS_ID="$(az eventhubs namespace show -g "$RG" -n "$PREFIX-ns" --query id -o tsv)" || return 1
  HUB_ID="$(az eventhubs eventhub show -g "$RG" --namespace-name "$PREFIX-ns" -n metrics --query id -o tsv)" || return 1
  KV_DCR_ID="$(az keyvault show -n "$KV_DCR" --query id -o tsv)" || return 1

  log "Export method A: data collection rule (Key Vault '$KV_DCR')"
  az identity create -g "$RG" -n "$UAMI" -l "$LOCATION" -o none || return 1
  UAMI_ID="$(az identity show -g "$RG" -n "$UAMI" --query id -o tsv)" || return 1
  UAMI_PRINCIPAL="$(az identity show -g "$RG" -n "$UAMI" --query principalId -o tsv)" || return 1

  if ! az role assignment create --assignee-object-id "$UAMI_PRINCIPAL" --assignee-principal-type ServicePrincipal \
         --role "Azure Event Hubs Data Sender" --scope "$NS_ID" -o none; then
    DCR_ENABLED=0; save_state
    cat <<MSG

WARNING: you are not allowed to create role assignments, so the DCR export was skipped.
Continuing with the diagnostic-settings export only. To test the DCR path later, ask someone with
Owner / User Access Administrator to run:

  az role assignment create --assignee-object-id $UAMI_PRINCIPAL --assignee-principal-type ServicePrincipal \\
    --role "Azure Event Hubs Data Sender" --scope $NS_ID

and then run:  ./scripts/e2e-azure.sh dcr

MSG
    return 1
  fi

  DCR_URL="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Insights/dataCollectionRules/$DCR?api-version=$DCR_API_VERSION"
  BODY="$(cat <<JSON
{
  "location": "$LOCATION",
  "kind": "PlatformTelemetry",
  "identity": { "type": "UserAssigned", "userAssignedIdentities": { "$UAMI_ID": {} } },
  "properties": {
    "dataSources": { "platformTelemetry": [ {
      "name": "keyvaultMetrics",
      "streams": [ "Microsoft.KeyVault/vaults:Metrics-Group-All" ] } ] },
    "destinations": { "eventHubs": [ { "name": "eh", "eventHubResourceId": "$HUB_ID" } ] },
    "dataFlows": [ {
      "streams": [ "Microsoft.KeyVault/vaults:Metrics-Group-All" ],
      "destinations": [ "eh" ] } ]
  }
}
JSON
)"
  for i in 1 2 3 4 5 6; do          # the new role assignment can take a minute to become effective
    if out="$(printf '%s' "$BODY" | az rest --method put --url "$DCR_URL" --body @- -o none 2>&1)"; then break; fi
    if echo "$out" | grep -q MissingPermissionsInTheManagedIdentity && [ "$i" -lt 6 ]; then
      echo "role assignment not effective yet, retrying in 30 s ($i/6)..."; sleep 30
    else
      echo "$out" >&2; return 1
    fi
  done
  DCR_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Insights/dataCollectionRules/$DCR"
  az monitor data-collection rule association create --name "$PREFIX-assoc" --rule-id "$DCR_ID" --resource "$KV_DCR_ID" -o none || return 1
  DCR_ENABLED=1; save_state
}

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

  DCR_ENABLED=1
  log "Resource group + Event Hub ($RG, $LOCATION)"
  az group create -n "$RG" -l "$LOCATION" -o none
  save_state                                     # so that 'down' works even if a later step fails
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
  # test only: for real deployments keep the token in Key Vault and use a Key Vault reference
  az functionapp config appsettings set -g "$RG" -n "$FUNC" -o none --settings \
    "DT_ENVIRONMENT=$DT_ENVIRONMENT" "DT_TOKEN=$DT_TOKEN" "EH_NAME=$HUB" "AZURE_LOCATION=$LOCATION"
  if [ "$EH_AUTH" = "identity" ]; then          # needs permission to create role assignments
    FUNC_PRINCIPAL="$(az functionapp identity assign -g "$RG" -n "$FUNC" --query principalId -o tsv)"
    az role assignment create --assignee-object-id "$FUNC_PRINCIPAL" --assignee-principal-type ServicePrincipal \
      --role "Azure Event Hubs Data Receiver" --scope "$NS_ID" -o none
    az functionapp config appsettings set -g "$RG" -n "$FUNC" -o none --settings \
      "EH_CONN__fullyQualifiedNamespace=$NS.servicebus.windows.net"
  else                                          # no RBAC needed: shared access key of the namespace
    EH_CS="$(az eventhubs namespace authorization-rule keys list -g "$RG" --namespace-name "$NS" \
      --name RootManageSharedAccessKey --query primaryConnectionString -o tsv)"
    az functionapp config appsettings set -g "$RG" -n "$FUNC" -o none --settings "EH_CONN=$EH_CS"
  fi

  log "Deploying the Function code"
  (cd "$ROOT" && func azure functionapp publish "$FUNC" --python)

  log "Export method B: diagnostic setting (Key Vault '$KV_DIAG')"
  az monitor diagnostic-settings create --name "$PREFIX-diag" --resource "$KV_DIAG_ID" \
    --metrics '[{"category":"AllMetrics","enabled":true}]' \
    --event-hub-rule "$NS_ID/authorizationrules/RootManageSharedAccessKey" --event-hub "$HUB" -o none


  # needs a role assignment; on any failure the script continues with diagnostic settings only
  setup_dcr || { DCR_ENABLED=0; save_state; echo "DCR export not set up (see above); continuing without it. Retry later with: ./scripts/e2e-azure.sh dcr"; }

  save_state
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
  [ "${DCR_ENABLED:-1}" = 0 ] && { dcr_ok=1; echo "DCR path skipped (no role-assignment permission); testing diagnostic settings only"; }
  while [ "$(date +%s)" -lt "$deadline" ]; do
    generate_traffic
    local found; found="$(query_dynatrace || true)"
    echo "$found" | grep -qi "/vaults/$KV_DCR\$"  && dcr_ok=1
    echo "$found" | grep -qi "/vaults/$KV_DIAG\$" && diag_ok=1
    echo "$(date +%H:%M:%S)  DCR export: $([ $dcr_ok = 1 ] && echo RECEIVED || echo waiting)   diagnostic setting export: $([ $diag_ok = 1 ] && echo RECEIVED || echo waiting)"
    [ $dcr_ok = 1 ] && [ $diag_ok = 1 ] && { log "SUCCESS: all enabled export methods delivered metrics to Dynatrace"; return 0; }
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
  dcr) need az; load_state; setup_dcr ;;
  all) up; rc=0; verify || rc=$?; down; exit $rc ;;
  *) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
