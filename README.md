# Azure metrics forwarder (Event Hubs → Dynatrace)

Streams Azure platform metrics into Dynatrace using Azure Monitor data collection rules (DCRs), an Event Hub and an Azure Function.

```
Azure resources → DCR (PlatformTelemetry) → Event Hub → Azure Function → Dynatrace Metrics API v2
```

- The DCR exports platform metrics (with dimensions) to an Event Hub. Microsoft quotes ~3 min end-to-end latency (diagnostic settings: 6–10 min).
- The Function ([function_app.py](function_app.py)) is triggered by the Event Hub, converts each record to Dynatrace line protocol (`gauge,min,max,sum,count`) and posts it to `/api/v2/metrics/ingest`.
- Metric keys follow the Dynatrace Azure convention `cloud.azure.<provider_namespace>.<resource_type>.<MetricName>` with a `-realtime` suffix (configurable via `KEY_SUFFIX`) so they don't clash with the metrics from the native Azure integration.
- Attributes set: `cloud.provider`, `azure.subscription`, `azure.resource.group`, `azure.resource.id`, `azure.resource.name`, `azure.resource.type`, `azure.location`, plus the metric's own dimensions.

> Status: not yet tested end to end. Validate attribute names against your existing `cloud.azure.*` metrics before rolling out (see [Validate](#6-validate)).

## Prerequisites

- Azure subscription with permission to create Event Hubs, Function apps, DCRs and role assignments
- Azure CLI and [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- A Dynatrace API token with the `metrics.ingest` scope

## Important constraints

- The monitored resource, the DCR and the Event Hub **must be in the same region**. You need one Event Hub namespace + DCR set per region.
- One DCR can only have one destination type; a resource can have at most 5 DCRs.
- Hourly-grain metrics are not exported.
- Disable metric diagnostic settings on the same resources to avoid duplicate data.
- Check [supported resources and regions](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/metrics-export-reference).

## How to set it up

### 1. Create the Event Hub (Azure)

Docs: [Create an event hub](https://learn.microsoft.com/en-us/azure/event-hubs/event-hubs-create)

```bash
az eventhubs namespace create -g <rg> -n <namespace> -l <region> --sku Standard
az eventhubs eventhub create  -g <rg> --namespace-name <namespace> -n <hub> --partition-count 4
```

### 2. Create the DCR and associate it with your resources (Azure)

Docs:
- [Metrics export using data collection rules](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/metrics-export-create)
- [DCR structure and sample JSON](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/metrics-export-structure)

Create the rule with `kind PlatformTelemetry`, a system-assigned identity and an Event Hubs destination. Use the sample JSON from the structure doc, listing only the metrics you need (`<resourceType>:<metricName>` streams, or `Metrics-Group-All`).

```bash
az monitor data-collection rule create --name <dcr> -g <rg> --location <region> \
  --kind PlatformTelemetry --identity "{type:'SystemAssigned'}" --rule-file dcr.json
```

Then create a data collection rule association (DCRA) for each resource to monitor (see the same doc, "Create a data collection rule association").

### 3. Let the DCR write to the Event Hub (Azure)

Docs: [Assign Azure roles to a managed identity](https://learn.microsoft.com/en-us/azure/role-based-access-control/role-assignments-portal-managed-identity)

Assign **Azure Event Hubs Data Sender** on the Event Hub (namespace) to the DCR's managed identity (`principalId` from the create output).

```bash
az role assignment create --assignee <dcr-principalId> \
  --role "Azure Event Hubs Data Sender" --scope <eventhub-namespace-resource-id>
```

### 4. Deploy the Function (Azure)

Docs:
- [Event Hubs trigger for Azure Functions](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-event-hubs-trigger)
- [Flex Consumption plan](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan)

Create a Python Function app (Flex Consumption recommended), then publish this repo:

```bash
func azure functionapp publish <function-app-name>
```

### 5. Configure app settings

| Setting | Value |
|---|---|
| `DT_ENVIRONMENT` | Dynatrace environment URL, e.g. `https://abc12345.live.dynatrace.com` |
| `DT_TOKEN` | API token with `metrics.ingest` (store in Key Vault and reference it) |
| `EH_NAME` | Event Hub name |
| `AZURE_LOCATION` | Region of the Event Hub / DCR / monitored resources, e.g. `westeurope`. Optional if the Function runs in that same region (falls back to the `REGION_NAME` variable Azure sets, normalized to `westeurope`), but set it explicitly to be safe. |
| `EH_CONN` | Event Hub connection string setting name, or use identity-based: `EH_CONN__fullyQualifiedNamespace=<namespace>.servicebus.windows.net` |

For identity-based access, grant the Function's managed identity **Azure Event Hubs Data Receiver** on the Event Hub. See [Event Hubs trigger – identity-based connections](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-event-hubs-trigger#identity-based-connections).

### 6. Validate

- Allow 15–30 minutes after creating the DCR for the first data.
- Check Function invocations and failures (Application Insights), and Event Hub incoming messages.
- In Dynatrace, confirm the metrics exist, e.g.:
  ```
  timeseries avg(cloud.azure.microsoft_keyvault.vaults.ServiceApiLatency-realtime), by:{azure.resource.id}, from:now()-1h
  ```
- Compare against the existing `cloud.azure.*` metric's dimensions and adjust the `dims` dict in `to_line()` if your tenant uses different attribute names (e.g. `azure.resourceId`).
- Troubleshooting on the Azure side: [DCR monitoring](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-monitor).

## Dynatrace API reference

[Metrics v2 – ingest](https://docs.dynatrace.com/docs/discover-dynatrace/references/dynatrace-api/environment-api/metric-v2/post-ingest-metrics) (limits: 1,000 lines or 1 MB per request; points too far in the past are rejected).

## Known limitations

- `azure.location` is not part of the export. It is taken from the `AZURE_LOCATION` app setting (or `REGION_NAME`), which is only correct because the Event Hub, DCR and monitored resources must all be in the same region. Deploy one Function (or one setting) per region; don't consume hubs from several regions in one Function.
- Platform metrics at scale with many dimensions can create high data-point volumes. Filter metrics in the DCR.
- Consider whether the native Dynatrace Azure integration already covers your needs; this pipeline is for extra control over dimensions, filtering or multi-subscription pipelines.
