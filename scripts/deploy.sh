#!/usr/bin/env bash
# Deploys the poller as an Azure Function with default settings. Safe to run again (updates the code and settings).
#
#   az login
#   export DT_ENVIRONMENT=https://abc12345.live.dynatrace.com DT_TOKEN=<token with metrics.ingest>   # or put them in .env.local
#   ./scripts/deploy.sh
#
# Optional environment variables:
#   SUBSCRIPTIONS   comma-separated subscription ids to poll (default: the current az subscription)
#   RG, LOCATION    resource group (default rg-dt-metrics) and region (default westeurope)
#   APP             Function app name (default dt-metrics-poller-<hash of the subscription id>, stable across runs)
# Then tag the resources to monitor:  az tag update --resource-id <id> --operation merge --tags dynatrace-nearrealtime=true
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env.local ]; then set -a; . <(tr -d '\r' < .env.local); set +a; fi
: "${DT_ENVIRONMENT:?set DT_ENVIRONMENT (https://...)}"
: "${DT_TOKEN:?set DT_TOKEN (token with metrics.ingest)}"
command -v az >/dev/null || { echo "Azure CLI (az) is required"; exit 1; }
command -v func >/dev/null || { echo "Azure Functions Core Tools (func) are required"; exit 1; }

HOME_SUB=$(az account show --query id -o tsv)
SUBSCRIPTIONS=${SUBSCRIPTIONS:-$HOME_SUB}
RG=${RG:-rg-dt-metrics}
LOCATION=${LOCATION:-westeurope}
SUFFIX=$(printf %s "$HOME_SUB$RG" | cksum | cut -d' ' -f1)
APP=${APP:-dt-metrics-poller-$SUFFIX}
STORAGE=dtmetrics$SUFFIX

echo "Deploying $APP in $RG ($LOCATION); polling: $SUBSCRIPTIONS"
az provider register --namespace Microsoft.Web --wait
az group create -n "$RG" -l "$LOCATION" -o none
az storage account create -g "$RG" -n "$STORAGE" -l "$LOCATION" --sku Standard_LRS -o none
az functionapp show -g "$RG" -n "$APP" -o none 2>/dev/null || \
  az functionapp create -g "$RG" -n "$APP" --storage-account "$STORAGE" --flexconsumption-location "$LOCATION" \
    --runtime python --runtime-version 3.11 --disable-app-insights -o none
PRINCIPAL=$(az functionapp identity assign -g "$RG" -n "$APP" --query principalId -o tsv)

az functionapp config appsettings set -g "$RG" -n "$APP" -o none --settings \
  "DT_ENVIRONMENT=$DT_ENVIRONMENT" "DT_TOKEN=$DT_TOKEN" "AZMON_SUBSCRIPTIONS=$SUBSCRIPTIONS"
func azure functionapp publish "$APP" --python

for SUB in ${SUBSCRIPTIONS//,/ }; do
  az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
    --role "Monitoring Reader" --scope "/subscriptions/$SUB" -o none 2>/dev/null \
    && echo "Monitoring Reader granted on $SUB" \
    || echo "!! Could not assign Monitoring Reader on $SUB (needs Owner / User Access Administrator). Ask an admin to run:
   az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal --role \"Monitoring Reader\" --scope /subscriptions/$SUB"
done

echo
echo "Done. Now tag the resources to monitor (any type, any resource group):"
echo "  az tag update --resource-id <resource id> --operation merge --tags dynatrace-nearrealtime=true"
echo "Data appears in Dynatrace after ~5-10 minutes. The app has no Application Insights; to see its logs, enable it:"
echo "  az monitor app-insights component create -g $RG -a $APP -l $LOCATION && az functionapp config appsettings set -g $RG -n $APP --settings APPLICATIONINSIGHTS_CONNECTION_STRING=<connection string>"
