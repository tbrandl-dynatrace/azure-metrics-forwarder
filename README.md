# Azure metrics forwarder (Event Hubs → Dynatrace)

Streams Azure platform metrics into Dynatrace using an Event Hub and an Azure Function. Metrics can be exported to the Event Hub in two ways, and the Function handles both:

- **Data collection rules (DCRs)** – the newer, recommended method
- **Diagnostic settings** – the classic method, still required for resource types that don't support DCR export yet

```
Azure resources ──► DCR (PlatformTelemetry)  ──┐
                                               ├─► Event Hub ─► Azure Function ─► Dynatrace Metrics API v2
Azure resources ──► Diagnostic setting (AllMetrics) ─┘
```

The Function ([function_app.py](function_app.py)) is triggered by the Event Hub, converts each metric record to Dynatrace line protocol (`gauge,min,max,sum,count`) and posts it to `/api/v2/metrics/ingest`.

- Metric keys follow the Dynatrace Azure convention `cloud.azure.<provider_namespace>.<resource_type>.<MetricName>` with a `-realtime` suffix (configurable via `KEY_SUFFIX`) so they don't clash with the metrics from the native Azure integration.
- Attributes set: `cloud.provider`, `azure.subscription`, `azure.resource.group`, `azure.resource.id`, `azure.resource.name`, `azure.resource.type`, `azure.location`, plus the metric's own dimensions (DCR export only).
- Records that aren't metrics (e.g. resource logs sent to the same hub by a diagnostic setting) are ignored.

> Status: not yet tested end to end. Validate attribute names against your existing `cloud.azure.*` metrics before rolling out (see [Validate](#validate)).

## DCR or diagnostic settings?

| | DCR (metrics export) | Diagnostic settings |
|---|---|---|
| Latency | ~3 min end to end | Typically a few minutes; up to 15 min extra for idle resources (export backs off), and up to 90 min after first creating the setting |
| Dimensions | Yes | No – dimensional metrics are flattened and aggregated across dimension values |
| Filtering | By metric name | `AllMetrics` only |
| Scale | One DCR for many resources | One setting per resource (up to 5 per resource) |
| Resource type coverage | [Supported list](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/metrics-export-reference) | Required where DCR isn't supported yet |
| Access to Event Hub | DCR's managed identity needs `Azure Event Hubs Data Sender` | Shared access policy with `Manage`, `Send`, `Listen` |
| Record format | Flat JSON object per metric (`Average`, `Count`, … as strings) | `{"records": [ … ]}` envelope, lowercase keys, numeric values, upper-cased `resourceId` |

Prefer DCRs where the resource type is supported (dimensions, filtering, one rule for many resources). Use diagnostic settings for everything else. **Don't enable both for the same resource and metrics** – you'd get duplicate data (and `azure.resource.id` is lower-cased so both would write to the same series).

## Prerequisites

- Azure subscription with permission to create Event Hubs, Function apps, DCRs/diagnostic settings and role assignments
- Azure CLI and [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- A Dynatrace API token with the `metrics.ingest` scope

## Important constraints

- The monitored resource, the DCR / diagnostic setting destination and the Event Hub **must be in the same region** (for regional resources). You need one Event Hub namespace + export setup per region.
- Event Hubs behind a firewall/VNet must allow **trusted Microsoft services** (required for diagnostic settings).
- A compacted event hub can't be used for diagnostic settings.
- DCR: one destination type per DCR, at most 5 DCRs per resource, hourly-grain metrics are not exported.
- Diagnostic settings: at most 5 per resource; not all metrics are exportable (see the *Exportable* column in the [metrics index](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/metrics-index)).

## How to set it up

### 1. Create the Event Hub (Azure)

Docs: [Create an event hub](https://learn.microsoft.com/en-us/azure/event-hubs/event-hubs-create), [Stream Azure monitoring data to an event hub](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/stream-monitoring-data-event-hubs) (partition and retention recommendations: start with 4 partitions, ≥7 days retention, default consumer group)

```bash
az eventhubs namespace create -g <rg> -n <namespace> -l <region> --sku Standard
az eventhubs eventhub create  -g <rg> --namespace-name <namespace> -n <hub> --partition-count 4
```

### 2. Export metrics to the Event Hub (Azure)

Choose option A, option B, or both for different resource types.

#### Option A: data collection rule

Docs:
- [Metrics export using data collection rules](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/metrics-export-create)
- [DCR structure and sample JSON](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/metrics-export-structure)
- [Assign Azure roles to a managed identity](https://learn.microsoft.com/en-us/azure/role-based-access-control/role-assignments-portal-managed-identity)

1. Create the rule with `kind PlatformTelemetry`, a system-assigned identity and an Event Hubs destination. Use the sample JSON from the structure doc, listing only the metrics you need (`<resourceType>:<metricName>` streams, or `Metrics-Group-All`).

   ```bash
   az monitor data-collection rule create --name <dcr> -g <rg> --location <region> \
     --kind PlatformTelemetry --identity "{type:'SystemAssigned'}" --rule-file dcr.json
   ```

2. Let the DCR write to the Event Hub: assign **Azure Event Hubs Data Sender** to the DCR's managed identity (`principalId` from the create output).

   ```bash
   az role assignment create --assignee <dcr-principalId> \
     --role "Azure Event Hubs Data Sender" --scope <eventhub-namespace-resource-id>
   ```

3. Create a data collection rule association (DCRA) for each resource to monitor (see "Create a data collection rule association" in the first doc).

#### Option B: diagnostic settings

Docs:
- [Diagnostic settings in Azure Monitor](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/diagnostic-settings) (destinations, requirements, metrics limitations, latency)
- [Stream Azure monitoring data to an event hub – data formats](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/stream-monitoring-data-event-hubs#data-formats)
- [`az monitor diagnostic-settings create`](https://learn.microsoft.com/en-us/cli/azure/monitor/diagnostic-settings#az-monitor-diagnostic-settings-create)

Create one setting per resource that sends `AllMetrics` to the Event Hub. The authorization rule needs `Manage`, `Send` and `Listen` (e.g. `RootManageSharedAccessKey`), and you need `ListKey` on it to create the setting.

```bash
az monitor diagnostic-settings create --name metrics-to-eventhub \
  --resource <monitored-resource-id> \
  --metrics '[{"category": "AllMetrics", "enabled": true}]' \
  --event-hub-rule /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.EventHub/namespaces/<namespace>/authorizationrules/RootManageSharedAccessKey \
  --event-hub <hub>
```

To roll this out to many resources, use Azure Policy / Bicep / Terraform rather than creating settings by hand. If the same hub also receives resource logs, that's fine – the Function skips non-metric records.

### 3. Deploy the Function (Azure)

Docs:
- [Event Hubs trigger for Azure Functions](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-event-hubs-trigger)
- [Flex Consumption plan](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan)

Create a Python Function app (Flex Consumption recommended), then publish this repo:

```bash
func azure functionapp publish <function-app-name>
```

### 4. Configure app settings

| Setting | Value |
|---|---|
| `DT_ENVIRONMENT` | Dynatrace environment URL, e.g. `https://abc12345.live.dynatrace.com` |
| `DT_TOKEN` | API token with `metrics.ingest` (store in Key Vault and reference it) |
| `EH_NAME` | Event Hub name |
| `EH_CONN` | Event Hub connection string setting name, or use identity-based: `EH_CONN__fullyQualifiedNamespace=<namespace>.servicebus.windows.net` |
| `AZURE_LOCATION` | Region of the Event Hub / monitored resources, e.g. `westeurope`. Optional if the Function runs in that same region (falls back to the `REGION_NAME` variable Azure sets, normalized to `westeurope`), but set it explicitly to be safe. |

For identity-based access, grant the Function's managed identity **Azure Event Hubs Data Receiver** on the Event Hub. See [Event Hubs trigger – identity-based connections](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-event-hubs-trigger#identity-based-connections).

### Validate

- Allow 15–30 minutes after creating a DCR (up to 90 minutes for a new diagnostic setting) for the first data.
- Check Function invocations and failures (Application Insights), and Event Hub incoming messages.
- In Dynatrace, confirm the metrics exist, e.g.:
  ```
  timeseries avg(cloud.azure.microsoft_keyvault.vaults.ServiceApiLatency-realtime), by:{azure.resource.id}, from:now()-1h
  ```
- Compare against the existing `cloud.azure.*` metric's dimensions and adjust the `dims` dict in `to_line()` if your tenant uses different attribute names (e.g. `azure.resourceId`).
- DCR troubleshooting: [DCR monitoring](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-monitor).

## Dynatrace API reference

[Metrics v2 – ingest](https://docs.dynatrace.com/docs/discover-dynatrace/references/dynatrace-api/environment-api/metric-v2/post-ingest-metrics) (limits: 1,000 lines or 1 MB per request; points too far in the past are rejected).

## Known limitations

- `azure.location` is not part of either export format. It is taken from the `AZURE_LOCATION` app setting (or `REGION_NAME`), which is only correct because the Event Hub, export setting and monitored resources must all be in the same region. Deploy one Function (or one setting) per region; don't consume hubs from several regions in one Function.
- Metrics exported by diagnostic settings carry no dimensions, so they are aggregated across dimension values (an Azure limitation). Use a DCR if you need per-dimension series.
- `azure.resource.name` is taken from the `resourceId` as sent. Diagnostic settings upper-case parts of the ID, so name casing can differ between the two export methods for the same resource.
- Platform metrics at scale with many dimensions can create high data-point volumes. Filter metrics in the DCR.
- Consider whether the native Dynatrace Azure integration already covers your needs; this pipeline is for extra control over dimensions, filtering or multi-subscription pipelines.
