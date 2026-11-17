#!/usr/bin/env bash
# Real end-to-end test of the metrics poller: Key Vault traffic → Azure Monitor → poll() → Dynatrace → DQL check.
#
# Runs the poll handler on THIS machine once a minute (what the timer trigger does in Azure) using your `az login`,
# so no deployment and no role assignment is needed; it exercises the real Metrics Batch API and Dynatrace.
#
#   az login                         # interactive (MFA); then: az account set --subscription <id>
#   ./scripts/e2e-poller.sh up       # resource group + Key Vault + python venv (~2 min)
#   ./scripts/e2e-poller.sh run      # traffic + poll every minute until the vault's metrics are in Dynatrace
#   ./scripts/e2e-poller.sh latency [--minutes 15] [--run-poller] [--csv out.csv]
#   ./scripts/e2e-poller.sh latency --continuous [--run-poller]   # keeps running until Ctrl-C
#                                    # continuous traffic + per-minute latency report (Azure Monitor and Dynatrace)
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
MAX_WAIT_MIN="${MAX_WAIT_MIN:-15}"
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
  az keyvault create -g "$RG" -n "$KV" -l "$LOCATION" --enable-rbac-authorization false -o none

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

in_dynatrace() {   # prints the vault's resource id if its metric has arrived (DQL API)
  "$PYBIN" "$ROOT/scripts/dql.py" --field "azure.resource.id" \
    "timeseries s = sum(\`cloud.azure.microsoft_keyvault.vaults.ServiceApiHit.metricstream\`), by:{azure.resource.id}, filter: azure.resource.group == \"${RG,,}\", from: now()-1h | fieldsAdd total = arraySum(s) | filter total > 0 | fields azure.resource.id"
}

run() {
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  load_state
  PYBIN="$VENV/bin/python"; [ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python"

  log "Traffic + poll every minute (max ${MAX_WAIT_MIN} min). Azure needs ~2-3 min before the first minute is readable."
  local deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    traffic
    echo "$(date +%H:%M:%S) polling"
    "$PYBIN" "$ROOT/scripts/poll_once.py" "$SUB" "$RG" "Microsoft.KeyVault/vaults" "ServiceApiHit,ServiceApiLatency" || echo "poll failed (see above)"
    found="$(in_dynatrace || true)"
    if echo "$found" | grep -qi "/vaults/$KV\$"; then
      log "SUCCESS: metrics of $KV were read from Azure Monitor by the poller and arrived in Dynatrace"
      return 0
    fi
    echo "not in Dynatrace yet"
    sleep 55
  done

  log "TIMEOUT. Where to look:"
  cat <<EOF
  - 'poll failed' with 401/403 from metrics.monitor.azure.com: you need Monitoring Reader on the subscription
  - 'polled ...: forwarding 0 data points': Azure had no data yet for that minute; keep running, or raise MAX_WAIT_MIN
  - poll OK but nothing in Dynatrace: check DT_TOKEN scopes (metrics.ingest, storage:metrics:read)
EOF
  return 1
}

latency() {   # continuous traffic; reports how long each minute takes to appear in Azure Monitor and in Dynatrace
  : "${DT_ENVIRONMENT:?set DT_ENVIRONMENT}"; : "${DT_TOKEN:?set DT_TOKEN}"
  load_state
  PYBIN="$VENV/bin/python"; [ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python"
  "$PYBIN" "$ROOT/scripts/latency_probe.py" --sub "$SUB" --rg "$RG" --vault "$KV" "$@"
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
  latency) shift; latency "$@" ;;
  down) down ;;
  all) up; rc=0; run || rc=$?; down; exit $rc ;;
  *) sed -n '2,18p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
