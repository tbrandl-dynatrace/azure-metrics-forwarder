# Azure metrics forwarder (→ Dynatrace)

Forwards Azure platform metrics into Dynatrace with an Azure Function ([function_app.py](function_app.py)). There are two ways to get the metrics out of Azure; the Function contains both:

1. **Metrics poller (default, recommended)**: a timer-triggered function (`poll`) calls the Azure Monitor Metrics Batch API every minute for the minute that is 2 minutes old. You choose what to monitor by **tagging Azure resources**. No Event Hub, no DCR, no diagnostic settings, no export lag; counters are sent as counters and gauges as gauges.
2. **Event Hub export (alternative)**: metrics are exported by data collection rules or diagnostic settings to an Event Hub, and an Event Hub-triggered function (`run`) forwards them. Use it when you want Azure to push (per-dimension series via DCR, resources whose metrics the batch API can't read, no polling load).

```
Azure resources (tag dynatrace-metrics) ──► timer (1 min) ─► Metrics Batch API ─► poll() ─┐
                                                                                         ├─► Dynatrace Metrics API v2
Azure resources ─► DCR / diagnostic setting ─► Event Hub ─► run() ───────────────────────┘
```

Both functions convert each metric record to Dynatrace line protocol and post it to `/api/v2/metrics/ingest`.

- Metric keys follow the Dynatrace Azure convention `cloud.azure.<provider_namespace>.<resource_type>.<MetricName>` with a `.metricstream` suffix (configurable via `KEY_SUFFIX`) so they don't clash with the metrics from the native Azure integration.
- Attributes set: `cloud.provider`, `azure.subscription`, `azure.resource.group`, `azure.resource.id`, `azure.resource.name`, `azure.resource.type`, `azure.location`, plus the metric's own dimensions (DCR export and explicit poller config).
- Which function runs is decided by app settings: `poll` does nothing until `AZMON_TAG` / `AZMON_CONFIG` is set, and `run` is only registered when `EH_NAME` is set. A poller-only deployment therefore needs no Event Hub settings.

> Status: the poller and the exports are tested against mocks and partly on real Azure; validate attribute names against your existing `cloud.azure.*` metrics before rolling out.

## Deploy the poller (default)

1. **Create a Python Function app** (Flex Consumption recommended; docs: [Flex Consumption plan](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan)), enable its **system-assigned managed identity**, and publish this repo:

   ```bash
   az functionapp identity assign -g <rg> -n <function-app>
   func azure functionapp publish <function-app> --python
   ```

2. **App settings** (token: store it in Key Vault and use a Key Vault reference for real deployments):

   ```bash
   az functionapp config appsettings set -g <rg> -n <function-app> --settings      DT_ENVIRONMENT=https://abc12345.live.dynatrace.com DT_TOKEN=<token with metrics.ingest>      AZMON_TAG=dynatrace-metrics AZMON_SUBSCRIPTIONS=<subscription id[,id...]>
   ```

3. **Give the identity access** (a role assignment, so Owner / User Access Administrator has to do it): **Monitoring Reader on each polled subscription**; the batch API requires it at subscription level.

   ```bash
   az role assignment create --assignee-object-id <principalId> --assignee-principal-type ServicePrincipal      --role "Monitoring Reader" --scope /subscriptions/<subscription id>
   ```

4. **Tag the resources** you want in Dynatrace: `az tag update --resource-id <id> --operation merge --tags dynatrace-metrics=true`. Details below.

## Run the poller locally (demos)

[scripts/poll_local.py](scripts/poll_local.py) runs the same `poll` handler on your machine at the start of every minute and sends the metrics into Dynatrace until you press Ctrl-C. It uses your `az login` (Reader, plus Monitoring Reader on the subscription), so no deployment or role assignment for a managed identity is needed. `DT_ENVIRONMENT` / `DT_TOKEN` come from the environment or `.env.local`.

```bash
pip install -r requirements.txt                 # once (a venv is a good idea)
az login
python scripts/poll_local.py                    # resources tagged dynatrace-metrics, every subscription you can read
python scripts/poll_local.py --tag demo --subscriptions <id>   # other tag / only one subscription
```

Stop the deployed Function (or don't tag the same resources) while you do this, otherwise counters are sent twice.

## How the poller works

The `poll` function ([function_app.py](function_app.py), helpers in [azmon.py](azmon.py)), triggered by a timer **every minute**. Each run asks the [Azure Monitor Metrics Batch API](https://learn.microsoft.com/en-us/rest/api/monitor/metrics-batch/batch) for the one-minute slot that is **2 minutes old** (`AZMON_DELAY_MINUTES`, default 2, so Azure has had time to finalise it), converts the data points exactly like the Event Hub path does (same metric keys with `.metricstream`, same `azure.*` attributes) and posts them to Dynatrace.

```
timer (every minute) → ARM: list resources of the type → Metrics Batch API (per region, ≤50 resources/call) → Dynatrace
```

It needs no Event Hub and has no setup lag, and it gives you the real region per resource (`azure.location`). It is disabled until `AZMON_TAG` or `AZMON_CONFIG` is set.

### Choosing what to poll: just tag it

Set the app setting `AZMON_TAG` to a tag name, e.g. `dynatrace-metrics`, and then **tag the Azure resources you want in Dynatrace** with that tag. That's all: resources are found with Azure Resource Graph (across every subscription the Function's identity can read, new tags are picked up within ~10 minutes), and **all their per-minute metrics are collected automatically** from the metric definitions of their resource type (metrics that need a dimension filter or have no 1-minute grain are skipped).

```bash
# tell the poller which tag to look for (once)
az functionapp config appsettings set -g <rg> -n <function-app> --settings AZMON_TAG=dynatrace-metrics

# then tag whatever should be monitored, any resource type
az tag update --resource-id <resource id> --operation merge --tags dynatrace-metrics=true
```

The tag value controls the metrics:

| Tag value | Effect |
|---|---|
| `true` (or `all`, `yes`, `on`, `1`, `*`, empty) | all per-minute metrics of the resource |
| `ServiceApiHit;ServiceApiLatency` (`,` or `;` separated) | only these metrics |
| `false`, `no`, `off`, `0`, `none` | resource is not polled (handy to switch a resource off without removing the tag) |

Tag *names* are matched exactly as written; the value matching above is case-insensitive. Tag a resource group or subscription? Azure tags don't inherit, so tag the resources themselves (an Azure Policy with the *Inherit a tag from the resource group* effect can do that for you).

| Setting | Value |
|---|---|
| `AZMON_TAG` | Tag name to look for. Setting it enables tag-based polling. |
| `AZMON_SUBSCRIPTIONS` | Optional, comma-separated subscription IDs to limit the search (default: all the identity can read). |
| `AZMON_DELAY_MINUTES` | Optional, default `2`. |
| `AZMON_CONFIG` | Optional advanced override, see below. Can be combined with `AZMON_TAG`. |

**Cost / volume:** "all metrics" can be a lot of data points per resource (a VM has dozens of metrics, every minute). Restrict via the tag value for chatty resource types, or start with a few resources and check the ingest volume in Dynatrace.

### Explicit configuration (optional)

`AZMON_CONFIG` selects resources by type (and optionally resource group) and lets you split metrics by dimension, which the tag mode doesn't do:

```json
[{"subscription": "<subscription id>",
  "resourceType": "Microsoft.KeyVault/vaults",
  "resourceGroup": "optional resource group filter",
  "metrics": ["ServiceApiHit", "ServiceApiLatency"],
  "dimensions": ["ActivityName"],
  "top": 100}]
```

`dimensions` (optional) splits the metrics by those dimension names (`<dim> eq '*'`); `top` caps the series per metric and resource (default 100). Without `dimensions` values are aggregated across dimensions. Resources are all resources of that type in the subscription (or resource group).

**Azure permissions:** the Function's managed identity needs **Monitoring Reader on each polled subscription** (the batch API requires it at subscription level; having it on the resources alone is not enough). That is a role assignment, so someone with Owner / User Access Administrator has to grant it:

```bash
az functionapp identity assign -g <rg> -n <function-app>
az role assignment create --assignee-object-id <principalId> --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" --scope /subscriptions/<subscription id>
```

Finding tagged resources only needs *Reader* (Azure Resource Graph). Locally (`func start`) the Function uses your `az login` instead, so it works with whatever access you have.

**Notes**
- **Counters vs gauges:** the poller reads each metric's definition from Azure. Metrics whose primary aggregation in Azure is **Total** (transactions, bytes in/out, ...) or **Count** (e.g. Key Vault `ServiceApiHit`, where every event is one sample) are counters and are sent as Dynatrace counters, `count,delta=<total of that minute>`; everything else (percentages, latencies, sizes, averages) is sent as a gauge summary (`gauge,min,max,sum,count`). So `ServiceApiHit` with 100 calls in a minute is a delta of 100 for that minute (sum 100 over a 1m interval); divide by 60 for a per-second rate. The exports (DCR / diagnostic settings) don't carry the metric definition, so they always send gauge summaries: don't feed the same metric key from both the poller and an export, Dynatrace would see a gauge and a counter under one key.
- Don't poll resources that are also exported through a DCR or diagnostic setting: you'd ingest the same points twice.
- Metric names are the Azure Monitor metric names (`ServiceApiHit`), as in the exports. Namespaces other than the resource type (custom / guest metrics) aren't supported by the batch API. A metric Azure rejects for a resource is logged once and skipped.
- The resource list is cached for 10 minutes, so new resources show up with that delay. A run that fails (Azure throttling, Dynatrace outage) loses that minute; there is no replay as with Event Hubs.
- Large estates: each run makes one batch call per (region, 50 resources) and metric set; the API is throttled, so keep `metrics` lists and resource types focused. The timer runs sequentially and has to finish within the minute.

## Alternative: Event Hub export (DCR / diagnostic settings)

The Event Hub-triggered function `run` handles metrics exported to an Event Hub in two ways:

- **Data collection rules (DCRs)**: the newer method
- **Diagnostic settings**: the classic method, still required for resource types that don't support DCR export yet

`run` converts each record to line protocol (`gauge,min,max,sum,count`; the exports carry no metric definition, so counters can't be recognised) and posts it. Records that aren't metrics (e.g. resource logs sent to the same hub by a diagnostic setting) are ignored. Set `EH_NAME` (see step 4 below) to enable it.

### DCR or diagnostic settings?

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

### Prerequisites

- Azure subscription with permission to create Event Hubs, Function apps, DCRs/diagnostic settings and role assignments
- Azure CLI and [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- A Dynatrace API token with the `metrics.ingest` scope

### Important constraints

- The monitored resource, the DCR / diagnostic setting destination and the Event Hub **must be in the same region** (for regional resources). You need one Event Hub namespace + export setup per region.
- Event Hubs behind a firewall/VNet must allow **trusted Microsoft services** (required for diagnostic settings).
- A compacted event hub can't be used for diagnostic settings.
- DCR: one destination type per DCR, at most 5 DCRs per resource, hourly-grain metrics are not exported.
- Diagnostic settings: at most 5 per resource; not all metrics are exportable (see the *Exportable* column in the [metrics index](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/metrics-index)).

### How to set it up

#### 1. Create the Event Hub (Azure)

Docs: [Create an event hub](https://learn.microsoft.com/en-us/azure/event-hubs/event-hubs-create), [Stream Azure monitoring data to an event hub](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/stream-monitoring-data-event-hubs) (partition and retention recommendations: start with 4 partitions, ≥7 days retention, default consumer group)

```bash
az eventhubs namespace create -g <rg> -n <namespace> -l <region> --sku Standard
az eventhubs eventhub create  -g <rg> --namespace-name <namespace> -n <hub> --partition-count 4
```

#### 2. Export metrics to the Event Hub (Azure)

Choose option A, option B, or both for different resource types.

##### Option A: data collection rule

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

##### Option B: diagnostic settings

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

#### 3. Deploy the Function (Azure)

Docs:
- [Event Hubs trigger for Azure Functions](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-event-hubs-trigger)
- [Flex Consumption plan](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan)

Create a Python Function app (Flex Consumption recommended), or reuse the poller's, then publish this repo:

```bash
func azure functionapp publish <function-app-name>
```

#### 4. Configure app settings

| Setting | Value |
|---|---|
| `DT_ENVIRONMENT` | Dynatrace environment URL, e.g. `https://abc12345.live.dynatrace.com` |
| `DT_TOKEN` | API token with `metrics.ingest` (store in Key Vault and reference it) |
| `EH_NAME` | Event Hub name |
| `EH_CONN` | Event Hub connection string setting name, or use identity-based: `EH_CONN__fullyQualifiedNamespace=<namespace>.servicebus.windows.net` |
| `AZURE_LOCATION` | Region of the Event Hub / monitored resources, e.g. `westeurope`. Optional if the Function runs in that same region (falls back to the `REGION_NAME` variable Azure sets, normalized to `westeurope`), but set it explicitly to be safe. |

For identity-based access, grant the Function's managed identity **Azure Event Hubs Data Receiver** on the Event Hub. See [Event Hubs trigger – identity-based connections](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-event-hubs-trigger#identity-based-connections).

#### Validate

- Allow 15–30 minutes after creating a DCR (up to 90 minutes for a new diagnostic setting) for the first data.
- Check Function invocations and failures (Application Insights), and Event Hub incoming messages.
- In Dynatrace, confirm the metrics exist, e.g.:
  ```
  timeseries avg(cloud.azure.microsoft_keyvault.vaults.ServiceApiLatency.metricstream), by:{azure.resource.id}, from:now()-1h
  ```
- Compare against the existing `cloud.azure.*` metric's dimensions and adjust the `dims` dict in `to_line()` if your tenant uses different attribute names (e.g. `azure.resourceId`).
- DCR troubleshooting: [DCR monitoring](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-monitor).

## Testing

[tests/test_poller.py](tests/test_poller.py) and [tests/test_poller_tags.py](tests/test_poller_tags.py) do the same for the polling function (explicit config and tag discovery) against a mock of ARM and the Metrics Batch API (batching, regions, dimensions, error handling, the 2-minute window). [tests/test_e2e.py](tests/test_e2e.py) runs the Function handler the way the Functions host does (a batch of Event Hub events) and checks the real HTTP requests it sends to a local mock of the Dynatrace ingest API. It uses the example payloads from the Microsoft docs for both export methods and covers attributes and metric keys, DCR/diagnostic-settings parity, ignoring log records, 1,000-line batching, escaping, and error propagation (so Event Hubs retries).

```bash
pip install -r requirements-dev.txt
pytest tests
```

An optional live test ingests a probe metric into a real tenant and waits until it's queryable (it is skipped when no token is configured). Configure the tenant once in the git-ignored `.env.local` (template: [.env.example](.env.example)); the token needs `metrics.ingest` (ingest) and `storage:metrics:read` (verification queries via the **DQL query API**, `/platform/storage/query/v1/query:execute`). The query URL is derived from `DT_ENVIRONMENT` (e.g. `…sprint.dynatracelabs.com` → `…sprint.apps.dynatracelabs.com`) unless `DT_QUERY_ENVIRONMENT` is set:

```
DT_ENVIRONMENT=https://abc12345.live.dynatrace.com
DT_TOKEN=dt0....               # metrics.ingest + storage:metrics:read
# DT_QUERY_ENVIRONMENT=https://abc12345.apps.dynatrace.com   # optional override
```

```bash
pytest tests -k live
```

Exported environment variables take precedence over `.env.local`. The same file is read by `scripts/e2e-azure.sh`.

### Real end-to-end test of the Event Hub path

[scripts/e2e-azure.sh](scripts/e2e-azure.sh) provisions a throw-away resource group (Event Hub, this Function with `EH_NAME` set, and two Key Vaults — one exported through a DCR, one through a diagnostic setting), generates Key Vault traffic and polls Dynatrace (DQL API) until metrics from both vaults arrive, then tears everything down. Requires `az` (logged in), Azure Functions Core Tools and `python`. Contributor rights are enough to test the diagnostic-settings path; the DCR path additionally needs permission to create a role assignment (the DCR's identity must be granted *Azure Event Hubs Data Sender*). Without it the script says so and tests diagnostic settings only. The Function reads the Event Hub with a connection string by default; set `EH_AUTH=identity` to use its managed identity instead.

```bash
# DT_ENVIRONMENT / DT_TOKEN come from .env.local (see above) or the environment
./scripts/e2e-azure.sh up           # provision + deploy
./scripts/e2e-azure.sh verify       # typically 10–40 min; new diagnostic settings can take up to 90 min
./scripts/e2e-azure.sh down         # or: ./scripts/e2e-azure.sh all
```

The script hasn't been run yet; expect to adjust `az` flags for your CLI version.

### Real end-to-end test of the poller (default path)

[scripts/e2e-poller.sh](scripts/e2e-poller.sh) deploys the poller the way you would run it: a throw-away resource group with one Key Vault tagged `dynatrace-metrics=true` (so tag discovery is exercised too) and a **Function App (Flex Consumption) running the `poll` timer function with a managed identity**, configured through app settings (`AZMON_TAG`, `AZMON_SUBSCRIPTIONS`, `DT_*`). It generates Key Vault traffic and passes when, for 3 minutes that are at least 5 minutes old, the values in Dynatrace equal what Azure Monitor itself reports for the vault ([scripts/verify_values.py](scripts/verify_values.py)), for **both metric kinds**:

| Kind | Metric | Azure aggregation | Sent as | Checked |
|---|---|---|---|---|
| counter | `ServiceApiHit` | Total | `count,delta` | Dynatrace sum per minute = Azure total |
| gauge | `ServiceApiLatency` | Average | `gauge,min,max,sum,count` | Dynatrace min / max / avg = Azure minimum / maximum / average |

It also checks that Azure's metric definitions classify the two metrics that way. Needs `az`, Azure Functions Core Tools (`func`) and python3.

The Function's identity needs **Monitoring Reader on the subscription**. `up` tries to assign it; if you may not create role assignments it prints the command for an admin, and `./scripts/e2e-poller.sh role` retries afterwards. Without the role the deployed Function reads nothing; in that case `POLL_LOCAL=1` runs the same `poll` handler on your machine (with your own `az login`) so you can still test the code.

```bash
az login
./scripts/e2e-poller.sh up       # RG + Key Vault + Function App, deploy, role
./scripts/e2e-poller.sh run      # traffic, then compare Dynatrace with Azure until it matches
./scripts/e2e-poller.sh logs     # the deployed Function's log, if something is off
./scripts/e2e-poller.sh down     # or: ./scripts/e2e-poller.sh all
```

### Measuring latency

`./scripts/e2e-poller.sh latency` keeps calling the test Key Vault (~2 calls/s) so that every minute carries data, and every 15 s checks both Azure Monitor and Dynatrace for which minutes are visible. For each full minute it reports how long after the end of that minute the data point first appeared in Azure Monitor, in Dynatrace, and the difference (the pipeline's own share), plus p50 / p95 / max (`--csv` saves the rows). It only looks at the metric in Dynatrace, so it measures whichever pipeline is feeding the vault: by default the deployed poller Function (`POLL_LOCAL=1` runs the poller on your machine instead), or point a DCR / diagnostic setting at the vault.

```bash
./scripts/e2e-poller.sh up                                    # once
./scripts/e2e-poller.sh latency --minutes 15                  # ~25 min incl. draining
```

For demos or long soak tests, `--continuous` runs until you press Ctrl-C: it prints a row as each minute arrives, a rolling summary of the last 30 minutes every 5 minutes (`--summary-every`), and a final summary on exit. Traffic generation keeps going the whole time, and minutes only count as missing from Dynatrace after `--drain-minutes` (default 10).

```bash
./scripts/e2e-poller.sh latency --continuous
```

Expect the poller to be roughly the configured delay (2 min) plus Azure's own availability delay plus up to a minute of timer granularity.

These tests don't cover the Azure side (DCR / diagnostic setting → Event Hub delivery); verify that with the steps in [Validate](#validate).

## Dynatrace API reference

[Metrics v2 – ingest](https://docs.dynatrace.com/docs/discover-dynatrace/references/dynatrace-api/environment-api/metric-v2/post-ingest-metrics) (limits: 1,000 lines or 1 MB per request; points too far in the past are rejected).

## Known limitations

- `azure.location` is not part of either export format. It is taken from the `AZURE_LOCATION` app setting (or `REGION_NAME`), which is only correct because the Event Hub, export setting and monitored resources must all be in the same region. Deploy one Function (or one setting) per region; don't consume hubs from several regions in one Function.
- Metrics exported by diagnostic settings carry no dimensions, so they are aggregated across dimension values (an Azure limitation). Use a DCR if you need per-dimension series.
- `azure.resource.name` is taken from the `resourceId` as sent. Diagnostic settings upper-case parts of the ID, so name casing can differ between the two export methods for the same resource.
- Platform metrics at scale with many dimensions can create high data-point volumes. Filter metrics in the DCR.
- Consider whether the native Dynatrace Azure integration already covers your needs; this pipeline is for extra control over dimensions, filtering or multi-subscription pipelines.
