# Azure metrics → Dynatrace (near real time)

An Azure Function that forwards Azure platform metrics to Dynatrace within about 1–2 minutes. A timer trigger runs **every minute**, finds the resources you **tagged** in Azure, reads their metrics for the minute that is 2 minutes old from the [Azure Monitor Metrics Batch API](https://learn.microsoft.com/en-us/rest/api/monitor/metrics-batch/batch) and posts them to the Dynatrace Metrics API v2. No Event Hub, no data collection rules, no diagnostic settings.

```
tagged Azure resources ─► timer (every minute) ─► Resource Graph (find tags) ─► Metrics Batch API ─► Dynatrace /api/v2/metrics/ingest
```

- Metric keys: `cloud.azure.<provider_namespace>.<resource_type>.<MetricName>` plus a configurable suffix (`METRIC_KEY_SUFFIX`, default **`.nearrealtime`**), e.g. `cloud.azure.microsoft_keyvault.vaults.ServiceApiHit.nearrealtime`. The suffix keeps the series apart from the metrics of the native Dynatrace Azure integration.
- Attributes: `cloud.provider`, `azure.subscription`, `azure.resource.group`, `azure.resource.id`, `azure.resource.name`, `azure.resource.type`, `azure.location`, plus the metric's own dimensions (explicit config only).
- Counters (Azure *Total* / *Count* aggregation, e.g. hits, transactions, bytes) are sent as Dynatrace counters (`count,delta`), everything else as gauges (`gauge,min,max,sum,count`). Timestamps are the start of Azure's one-minute slot (UTC).

> **Prefer Azure to push?** The [event-hub-export](event-hub-export/README.md) folder contains the alternative solution: metrics exported by DCRs or diagnostic settings to an Event Hub, forwarded by an Event Hub-triggered Function. Use it for per-dimension series via DCR or for resources the batch API can't read. It is a separate Function app with its own deployment, tests and instructions.

## Set it up end to end

You need: an Azure subscription (Contributor to create things; **Owner / User Access Administrator once** for the role assignment in step 4), Azure CLI, [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) (`func`), and a Dynatrace API token with the **`metrics.ingest`** scope (add `storage:metrics:read` to verify with DQL below). Docs: [Flex Consumption plan](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan), [timer trigger](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-timer), [Metrics Batch API](https://learn.microsoft.com/en-us/rest/api/monitor/metrics-batch/batch).

**1. Variables**

```bash
az login && az account set --subscription <subscription id>
SUB=$(az account show --query id -o tsv)
RG=rg-dt-metrics; LOC=westeurope; APP=dt-metrics-poller-$RANDOM; ST=dtmetrics$RANDOM
DT_ENVIRONMENT=https://abc12345.live.dynatrace.com        # https:// required
DT_TOKEN=<token with metrics.ingest>
```

**2. Create the Function app** (Flex Consumption, Python) with a system-assigned managed identity

```bash
az provider register --namespace Microsoft.Web --wait
az group create -n $RG -l $LOC
az storage account create -g $RG -n $ST -l $LOC --sku Standard_LRS
az functionapp create -g $RG -n $APP --storage-account $ST --flexconsumption-location $LOC \
  --runtime python --runtime-version 3.11
PRINCIPAL=$(az functionapp identity assign -g $RG -n $APP --query principalId -o tsv)
```

**3. App settings and deploy the code** (for real deployments keep `DT_TOKEN` in Key Vault and use a Key Vault reference)

```bash
az functionapp config appsettings set -g $RG -n $APP --settings \
  DT_ENVIRONMENT=$DT_ENVIRONMENT DT_TOKEN=$DT_TOKEN \
  AZMON_TAG=dynatrace-metrics AZMON_SUBSCRIPTIONS=$SUB      # optional: METRIC_KEY_SUFFIX=.nearrealtime AZMON_DELAY_MINUTES=2
func azure functionapp publish $APP --python                # run from the repository root
```

**4. Let the Function read Azure Monitor** (a role assignment: ask an Owner / User Access Administrator): **Monitoring Reader on every polled subscription**. The batch API requires it at subscription level; Reader on the resources alone is not enough.

```bash
az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
  --role "Monitoring Reader" --scope /subscriptions/$SUB
```

**5. Tag what you want to monitor** (any resource type; the tag value selects the metrics, see below)

```bash
az tag update --resource-id <resource id> --operation merge --tags dynatrace-metrics=true
```

**6. Check that it works.** Resource Graph needs a few minutes to see a new tag and Azure a minute or two to publish metrics, so allow about 5–10 minutes, then:

- In the Function app (Portal → Monitor, or `func azure functionapp logstream $APP`), each run logs `polled hh:mm: forwarding N data points`. `401/403` from `metrics.monitor.azure.com` means the role from step 4 is missing or not effective yet (can take ~5 minutes); `400` from Dynatrace ingest quotes the rejected lines.
- In Dynatrace (Notebooks / DQL), swap in a metric of a resource you tagged:

  ```
  timeseries sum(cloud.azure.microsoft_keyvault.vaults.ServiceApiHit.nearrealtime), by:{azure.resource.id}, from:now()-30m
  ```

To stop everything: `az group delete -n $RG`.

## Run the poller locally (demos)

[scripts/poll_local.py](scripts/poll_local.py) runs the same `poll` handler on your machine at the start of every minute and sends the metrics into Dynatrace until you press Ctrl-C. It uses your `az login` (Reader, plus Monitoring Reader on the subscription), so no deployment or managed-identity role is needed. `DT_ENVIRONMENT` / `DT_TOKEN` come from the environment or the git-ignored `.env.local` (template: [.env.example](.env.example)).

```bash
pip install -r requirements.txt                 # once (a venv is a good idea)
az login
python scripts/poll_local.py                    # resources tagged dynatrace-metrics, every subscription you can read
python scripts/poll_local.py --tag demo --subscriptions <id> --delay-minutes 2   # other tag / one subscription / delay
```

Stop the deployed Function (or don't tag the same resources) while you do this, otherwise counters are sent twice.

## Settings

| Setting | Value |
|---|---|
| `DT_ENVIRONMENT` | Dynatrace environment URL including `https://`. |
| `DT_TOKEN` | API token with `metrics.ingest`. |
| `AZMON_TAG` | Tag name to look for. Setting it enables tag-based polling. |
| `AZMON_SUBSCRIPTIONS` | Optional, comma-separated subscription IDs to limit the search (default: all the identity can read). |
| `AZMON_DELAY_MINUTES` | Optional, default `2`: how old the polled minute is. `1` polls the minute that just ended and loses minutes Azure hasn't finished yet. |
| `METRIC_KEY_SUFFIX` | Optional, default `.nearrealtime`: appended to every metric key. |
| `AZMON_CONFIG` | Optional advanced override, see [Explicit configuration](#explicit-configuration-optional). Can be combined with `AZMON_TAG`. |

## How it works

The `poll` function ([function_app.py](function_app.py), helpers in [azmon.py](azmon.py)) is triggered by a timer **every minute**. Each run asks Azure Resource Graph for the tagged resources, then asks the [Metrics Batch API](https://learn.microsoft.com/en-us/rest/api/monitor/metrics-batch/batch) for the one-minute slot that is **2 minutes old** (`AZMON_DELAY_MINUTES`, so Azure has had time to finalise it), converts the data points to Dynatrace line protocol (key suffix `.nearrealtime`, `azure.*` attributes) and posts them. It needs no Event Hub and has no export setup lag, and it uses each resource's real region for `azure.location`. It does nothing until `AZMON_TAG` or `AZMON_CONFIG` is set.

### Choosing what to poll: just tag it

Set the app setting `AZMON_TAG` to a tag name, e.g. `dynatrace-metrics`, and then **tag the Azure resources you want in Dynatrace** with that tag. That's all: resources are found with Azure Resource Graph (across every subscription the Function's identity can read, new tags are picked up within ~10 minutes), and **all their per-minute metrics are collected automatically** from the metric definitions of their resource type (metrics that need a dimension filter or have no 1-minute grain are skipped).

```bash
# the Function looks for this tag (app setting AZMON_TAG, set in step 3); tag whatever should be monitored, any resource type
az tag update --resource-id <resource id> --operation merge --tags dynatrace-metrics=true
```

The tag value controls the metrics:

| Tag value | Effect |
|---|---|
| `true` (or `all`, `yes`, `on`, `1`, `*`, empty) | all per-minute metrics of the resource |
| `ServiceApiHit;ServiceApiLatency` (`,` or `;` separated) | only these metrics |
| `false`, `no`, `off`, `0`, `none` | resource is not polled (handy to switch a resource off without removing the tag) |

Tag *names* are matched exactly as written; the value matching above is case-insensitive. Tag a resource group or subscription? Azure tags don't inherit, so tag the resources themselves (an Azure Policy with the *Inherit a tag from the resource group* effect can do that for you).

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

**Azure permissions:** see step 4: Monitoring Reader on each polled subscription. Finding tagged resources only needs *Reader* (Azure Resource Graph). Locally the poller uses your `az login` instead, so it works with whatever access you have.

**Notes**
- **Counters vs gauges:** the poller reads each metric's definition from Azure. Metrics whose primary aggregation in Azure is **Total** (transactions, bytes in/out, ...) or **Count** (e.g. Key Vault `ServiceApiHit`, where every event is one sample) are counters and are sent as Dynatrace counters, `count,delta=<total of that minute>`; everything else (percentages, latencies, sizes, averages) is sent as a gauge summary (`gauge,min,max,sum,count`). So `ServiceApiHit` with 100 calls in a minute is a delta of 100 for that minute (sum 100 over a 1m interval); divide by 60 for a per-second rate. The [Event Hub export](event-hub-export/README.md) doesn't carry the metric definition and always sends gauges; its default suffix `.metricstream` differs from this one so the two never write to the same key.
- Don't poll resources that the [Event Hub solution](event-hub-export/README.md) also exports under the same suffix: you'd ingest the same points twice.
- Metric names are the Azure Monitor metric names (`ServiceApiHit`), as shown in the Azure portal and docs. Namespaces other than the resource type (custom / guest metrics) aren't supported by the batch API. A metric Azure rejects for a resource is logged once and skipped.
- The resource list is cached for 10 minutes, so new resources show up with that delay. A run that fails (Azure throttling, Dynatrace outage) loses that minute; there is no replay as with Event Hubs.
- Large estates: each run makes one batch call per (region, 50 resources) and metric set; the API is throttled, so keep `metrics` lists and resource types focused. The timer runs sequentially and has to finish within the minute.

## Real end-to-end test in Azure

[scripts/e2e-poller.sh](scripts/e2e-poller.sh) deploys the poller the way you would run it: a throw-away resource group with one Key Vault tagged `dynatrace-metrics=true` (so tag discovery is exercised too) and a **Function App (Flex Consumption) running the `poll` timer function with a managed identity**, configured through app settings (`AZMON_TAG`, `AZMON_SUBSCRIPTIONS`, `DT_*`). It generates Key Vault traffic and passes when, for 3 minutes that are at least 5 minutes old, the values in Dynatrace equal what Azure Monitor itself reports for the vault ([scripts/verify_values.py](scripts/verify_values.py)), for **both metric kinds**:

| Kind | Metric | Azure aggregation | Sent as | Checked |
|---|---|---|---|---|
| counter | `ServiceApiHit` | Count | `count,delta` | Dynatrace sum per minute = Azure count |
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

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests
```

The tests run the `poll` handler against local mocks of Azure Resource Graph, ARM, the metric definitions API, the Metrics Batch API and the Dynatrace ingest API: tag discovery and tag values, batching (50 resources / 20 metrics per call, per region), dimensions, counter / gauge classification, the 2-minute window, the key suffix, timestamps, error handling, and the latency / verification scripts. The mocks live in [tests/conftest.py](tests/conftest.py).

## Dynatrace API reference

[Metrics v2 – ingest](https://docs.dynatrace.com/docs/discover-dynatrace/references/dynatrace-api/environment-api/metric-v2/post-ingest-metrics) (limits: 1,000 lines or 1 MB per request; points more than about an hour old are rejected).

## Known limitations

- A poll that fails (Azure throttling, Dynatrace outage) loses that minute; there is no replay as with an Event Hub.
- Custom / guest metrics (namespaces other than the resource type) are not supported by the batch API.
- Metrics that need a dimension filter or have no 1-minute grain are skipped in tag mode; use `AZMON_CONFIG` for dimensions.
- Consider whether the native Dynatrace Azure integration already covers your needs; this Function is for near-real-time metrics and extra control.
