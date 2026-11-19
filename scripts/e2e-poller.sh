#!/usr/bin/env bash
# Real end-to-end test of the poller, deployed as an Azure Function:
#   Key Vault traffic → Azure Monitor → timer-triggered Function (every minute) → Dynatrace → DQL check.
#
# `up` creates a throw-away resource group with a Key Vault tagged dynatrace-metrics=true and a Function App
# (Flex Consumption) running this repo's `poll` function with a managed identity. The identity needs
# Monitoring Reader on the subscription (a role assignment: Owner / User Access Administrator); if you may not
# create role assignments `up` prints the command for an admin, and you can run the poller on your machine
# in the meantime with POLL_LOCAL=1.
#
#   az login                         # interactive (MFA); then: az account set --subscription <id>
#   ./scripts/e2e-poller.sh up       # RG + Key Vault + Function App, deploy, role (~8 min)
#   ./scripts/e2e-poller.sh run      # traffic until counter AND gauge values in Dynatrace match Azure
#   ./scripts/e2e-poller.sh latency [--minutes 15] [--csv out.csv]
#   ./scripts/e2e-poller.sh latency --continuous                  # keeps running until Ctrl-C
#                                    # continuous traffic + per-minute latency report (Azure Monitor and Dynatrace)
#   ./scripts/e2e-poller.sh role     # (re)try the Monitoring Reader assignment, e.g. after an admin allowed it
#   ./scripts/e2e-poller.sh logs     # stream the deployed Function's log
#   ./scripts/e2e-poller.sh tag      # tag a vault created before tag support (dynatrace-metrics=true)
#   ./scripts/e2e-poller.sh down     # delete everything
#   ./scripts/e2e-poller.sh all      # up + run + down
#
# POLL_LOCAL=1 (for run / latency): run the poll handler on this machine once a minute with your own `az login`
# instead of relying on the deployed Function. Only for the case where the Function's identity has no Monitoring
# Reader yet (the deployed poller then reads nothing, so nothing is ingested twice). With a working deployed poller
# it would send the same minutes a second time, which doubles the counter deltas.
#
# DT_ENVIRONMENT / DT_TOKEN come from the environment or the git-ignored .env.local (see .env.example).
# Needs: az CLI (logged in; Contributor), Azure Functions Core Tools (func), python3 with venv.
# Not yet run by its author; adjust az flags if your CLI version differs.

set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.e2e-poller-state.env"
LOCATION="${LOCATION:-westeurope}"
MAX_WAIT_MIN="${MAX_WAIT_MIN:-25}"
PY="$(command -v python3 || command -v python)"
VENV="$ROOT/.venv-e2e"

if [ -f "$ROOT/.env.local" ]; then
  while IFS='=' read -r k v; do
    v="${v%$'\r'}"; k="${k//[[:space:]]/}"
    case "$k" in ''|\#*) continue ;; esac
    [ -n "$v" ] && [ -z "${!k:-}" ] && export "$k=$v"
  done < "$ROOT/.env.local"
fi

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
load_state() { [ -f "$STATE" ] && . "$STATE" || { echo "no state file; run 'up' first" >&2; exit 1; }; }

pyenv() { PYBIN="$VENV/bin/python"; [ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python"; }

save_state() {
  printf 'SUB=%s
RG=%s
KV=%s
FUNC=%s
PRINCIPAL=%s
ROLE_OK=%s
' "$SUB" "$RG" "$KV" "${FUNC:-}" "${PRINCIPAL:-}" "${ROLE_OK:-0}" > "$STATE"
}

grant_role() {   # Monitoring Reader on the subscription for the Function's identity; sets ROLE_OK
  if az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal        --role "Monitoring Reader" --scope "/subscriptions/$SUB" -o none; then
    ROLE_OK=1
  else
    ROLE_OK=0
    cat <<MSG

WARNING: you are not allowed to create role assignments, so the Function cannot read Azure Monitor yet.
Ask someone with Owner / User Access Administrator to run:

  az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
    --role "Monitoring Reader" --scope /subscriptions/$SUB

then run ./scripts/e2e-poller.sh role (or just 'run'; the deployed poller picks it up within a few minutes).
Until then, you can test the poller code from this machine with: POLL_LOCAL=1 ./scripts/e2e-poller.sh run

MSG
  fi
  save_state
}

up() {
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  command -v az >/dev/null || { echo "az CLI missing" >&2; exit 1; }
  command -v func >/dev/null || { echo "Azure Functions Core Tools (func) missing" >&2; exit 1; }
  SUB="$(az account show --query id -o tsv)"
  P="dtpoll$RANDOM"; RG="rg-$P"; KV="$P-kv"; FUNC="$P-func"; ST="${P}st"; PRINCIPAL=""; ROLE_OK=0
  save_state                                                      # saved first so 'down' always works

  log "Registering resource providers"
  for p in Microsoft.KeyVault Microsoft.Web Microsoft.Storage; do az provider register --namespace "$p" --wait -o none; done

  log "Resource group + Key Vault ($RG, $LOCATION)"
  az group create -n "$RG" -l "$LOCATION" -o none
  az keyvault create -g "$RG" -n "$KV" -l "$LOCATION" --enable-rbac-authorization false --tags dynatrace-metrics=true -o none

  log "Function app (Flex Consumption) with managed identity"
  az storage account create -g "$RG" -n "$ST" -l "$LOCATION" --sku Standard_LRS -o none
  az functionapp create -g "$RG" -n "$FUNC" --storage-account "$ST"     --flexconsumption-location "$LOCATION" --runtime python --runtime-version 3.11 -o none
  PRINCIPAL="$(az functionapp identity assign -g "$RG" -n "$FUNC" --query principalId -o tsv)"
  save_state
  # test only: for real deployments keep the token in Key Vault and use a Key Vault reference
  az functionapp config appsettings set -g "$RG" -n "$FUNC" -o none --settings     "DT_ENVIRONMENT=$DT_ENVIRONMENT" "DT_TOKEN=$DT_TOKEN" "AZURE_LOCATION=$LOCATION"     "AZMON_TAG=dynatrace-metrics" "AZMON_SUBSCRIPTIONS=$SUB"

  log "Deploying the Function code"
  (cd "$ROOT" && func azure functionapp publish "$FUNC" --python)

  log "Monitoring Reader for the Function's identity"
  grant_role

  log "Python environment (for the verification scripts)"
  "$PY" -m venv "$VENV"
  pyenv
  "$PYBIN" -m pip install -q -r "$ROOT/requirements.txt"
  echo "Ready: vault $KV, function $FUNC. Next: ./scripts/e2e-poller.sh run"
}

role() { load_state; PRINCIPAL="${PRINCIPAL:?no identity in state; run up}"; grant_role; }

logs() { load_state; az webapp log tail -g "$RG" -n "$FUNC" 2>/dev/null || func azure functionapp logstream "$FUNC"; }

traffic() {   # Key Vault API calls produce the ServiceApiHit metric
  for i in 1 2 3 4 5; do
    az keyvault secret set --vault-name "$KV" --name probe --value "$RANDOM" -o none
    az keyvault secret show --vault-name "$KV" --name probe -o none
  done
}

run() {
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  load_state
  pyenv
  [ "${ROLE_OK:-0}" = 1 ] || [ -n "${POLL_LOCAL:-}" ] ||     echo "NOTE: the deployed Function has no Monitoring Reader yet (see 'up' output / './scripts/e2e-poller.sh role'); it will not ingest anything. Use POLL_LOCAL=1 to poll from this machine."

  log "Traffic, then compare Dynatrace with Azure every minute; the deployed Function polls on its own timer (max ${MAX_WAIT_MIN} min). Success = 3 minutes (each at least 5 minutes old) whose values in Dynatrace equal Azure's own: ServiceApiHit as counter, ServiceApiLatency as gauge."
  local deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    traffic
    if [ -n "${POLL_LOCAL:-}" ]; then
      echo "$(date +%H:%M:%S) polling locally"
      "$PYBIN" "$ROOT/scripts/poll_once.py" "$SUB" || echo "poll failed (see above)"
    fi
    rc=0
    "$PYBIN" "$ROOT/scripts/verify_values.py" "$SUB" "$RG" "$KV" || rc=$?
    if [ "$rc" = 0 ]; then
      log "SUCCESS: counter (ServiceApiHit) and gauge (ServiceApiLatency) read from Azure Monitor by the poller match Azure's own values in Dynatrace"
      return 0
    elif [ "$rc" = 1 ]; then
      log "FAILED: Dynatrace values differ from Azure (see MISMATCH lines above)"
      return 1
    fi
    [ "$rc" = 3 ] && echo "comparison could not run (see above); retrying"       || echo "not enough comparable minutes yet (need 3 that are at least 5 minutes old); continuing"
    sleep 55
  done

  log "TIMEOUT. Where to look:"
  cat <<EOF
  - ./scripts/e2e-poller.sh logs: the deployed Function's log. 401/403 from metrics.monitor.azure.com means the identity
    lacks Monitoring Reader on the subscription (./scripts/e2e-poller.sh role); a new role can take ~5 minutes to apply
  - the timer function not listed / not running: Portal -> the Function App -> Functions should show 'poll'
  - 'polled ...: forwarding 0 data points': Azure had no data yet for that minute, or Resource Graph has not indexed the
    new vault's tag yet (takes a few minutes); keep running, or raise MAX_WAIT_MIN
  - poll OK but nothing in Dynatrace: check DT_TOKEN scopes (metrics.ingest, storage:metrics:read)
  - 'not enough comparable minutes': the check needs 3 minutes older than 5 min with data on both sides; keep running
EOF
  return 1
}

latency() {   # continuous traffic; reports how long each minute takes to appear in Azure Monitor and in Dynatrace
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  load_state
  pyenv
  local extra=(); [ -n "${POLL_LOCAL:-}" ] && extra=(--run-poller)
  "$PYBIN" "$ROOT/scripts/latency_probe.py" --sub "$SUB" --rg "$RG" --vault "$KV" ${extra[@]+"${extra[@]}"} "$@"
}

tag() {   # (re)apply the discovery tag to the test vault, e.g. for a vault created before tag support
  load_state
  az tag update --resource-id "$(az keyvault show -n "$KV" --query id -o tsv)" --operation merge --tags dynatrace-metrics=true -o none
  echo "tagged $KV with dynatrace-metrics=true (Resource Graph needs a few minutes to see it)"
}

down() {
  load_state
  log "Deleting resource group $RG"
  az group delete -n "$RG" --yes --no-wait
  rm -f "$STATE"
  rm -rf "$VENV"
}

case "${1:-}" in
  up) up ;;
  run) run ;;
  tag) tag ;;
  role) role ;;
  logs) logs ;;
  latency) shift; latency "$@" ;;
  down) down ;;
  all) up; rc=0; run || rc=$?; down; exit $rc ;;
  *) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
