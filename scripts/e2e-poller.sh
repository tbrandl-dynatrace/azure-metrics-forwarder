#!/usr/bin/env bash
# Real end-to-end test of the metrics poller: Key Vault traffic → Azure Monitor → poll() → Dynatrace → DQL check.
#
# Runs the poll handler on THIS machine once a minute (what the timer trigger does in Azure) using your `az login`,
# so no deployment and no role assignment is needed; it exercises the real Metrics Batch API and Dynatrace.
#
#   az login                         # interactive (MFA); then: az account set --subscription <id>
#   ./scripts/e2e-poller.sh up       # resource group + Key Vault + python venv (~2 min)
#   ./scripts/e2e-poller.sh run      # traffic + poll every minute until counter AND gauge values in Dynatrace match Azure
#   ./scripts/e2e-poller.sh latency [--minutes 15] [--run-poller] [--csv out.csv]
#   ./scripts/e2e-poller.sh latency --continuous [--run-poller]   # keeps running until Ctrl-C
#                                    # continuous traffic + per-minute latency report (Azure Monitor and Dynatrace)
#   ./scripts/e2e-poller.sh tag      # tag a vault created before tag support (dynatrace-metrics=true)
#   ./scripts/e2e-poller.sh down     # delete everything
#   ./scripts/e2e-poller.sh all      # up + run + down
#
# DT_ENVIRONMENT / DT_TOKEN come from the environment or the git-ignored .env.local (see .env.example).
# Needs: az CLI (logged in; Reader is enough for ARM, the batch API wants Monitoring Reader on the subscription),
#        python3 with venv. Not yet run by its author; adjust az flags if your CLI version differs.

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

up() {
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  command -v az >/dev/null || { echo "az CLI missing" >&2; exit 1; }
  SUB="$(az account show --query id -o tsv)"
  P="dtpoll$RANDOM"; RG="rg-$P"; KV="$P-kv"
  printf 'SUB=%s\nRG=%s\nKV=%s\n' "$SUB" "$RG" "$KV" > "$STATE"   # saved first so 'down' always works

  log "Resource group + Key Vault ($RG, $LOCATION)"
  az provider register --namespace Microsoft.KeyVault --wait -o none
  az group create -n "$RG" -l "$LOCATION" -o none
  az keyvault create -g "$RG" -n "$KV" -l "$LOCATION" --enable-rbac-authorization false --tags dynatrace-metrics=true -o none

  log "Python environment"
  "$PY" -m venv "$VENV"
  PYBIN="$VENV/bin/python"; [ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python"
  "$PYBIN" -m pip install -q -r "$ROOT/requirements.txt"
  echo "Ready: vault $KV. Next: ./scripts/e2e-poller.sh run"
}

traffic() {   # Key Vault API calls produce the ServiceApiHit metric
  for i in 1 2 3 4 5; do
    az keyvault secret set --vault-name "$KV" --name probe --value "$RANDOM" -o none
    az keyvault secret show --vault-name "$KV" --name probe -o none
  done
}

run() {
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  load_state
  PYBIN="$VENV/bin/python"; [ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python"

  log "Traffic + poll every minute (max ${MAX_WAIT_MIN} min). Success = 3 minutes (each at least 5 minutes old) whose values in Dynatrace equal Azure's own: ServiceApiHit as counter, ServiceApiLatency as gauge."
  local deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    traffic
    echo "$(date +%H:%M:%S) polling"
    "$PYBIN" "$ROOT/scripts/poll_once.py" "$SUB" || echo "poll failed (see above)"
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
  - 'poll failed' with 401/403 from metrics.monitor.azure.com: you need Monitoring Reader on the subscription
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
  PYBIN="$VENV/bin/python"; [ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python"
  "$PYBIN" "$ROOT/scripts/latency_probe.py" --sub "$SUB" --rg "$RG" --vault "$KV" "$@"
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
  latency) shift; latency "$@" ;;
  down) down ;;
  all) up; rc=0; run || rc=$?; down; exit $rc ;;
  *) sed -n '2,19p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
