# Azure metrics → Dynatrace (near real time)

An Azure Function that forwards Azure platform metrics to Dynatrace within about 1–2 minutes. A timer runs **every minute**, finds the Azure resources you **tagged**, reads their metrics for the minute that is 2 minutes old from the [Azure Monitor Metrics Batch API](https://learn.microsoft.com/en-us/rest/api/monitor/metrics-batch/batch) and posts them to the Dynatrace Metrics API v2. No Event Hub, data collection rules or diagnostic settings.

```
tagged Azure resources ─► timer (every minute) ─► Resource Graph ─► Metrics Batch API ─► Dynatrace /api/v2/metrics/ingest
```

- **Selection:** tag a resource with `dynatrace-nearrealtime=true` (any resource type, resource group or subscription). All its per-minute metrics are collected, split by **all their dimensions**. Nothing else to configure.
- **Metric keys:** `cloud.azure.<namespace>.<type>.<Metric>` + suffix `.nearrealtime` (`METRIC_KEY_SUFFIX`), e.g. `cloud.azure.microsoft_keyvault.vaults.ServiceApiHit.nearrealtime`.
- **Attributes:** `cloud.provider`, `azure.subscription`, `azure.resource.group`, `azure.resource.id`, `azure.resource.name`, `azure.resource.type`, `azure.location` and the metric's dimensions (lower-case, e.g. `activityname`, `statuscode`).
- **Counters vs gauges:** metrics Azure aggregates as *Total* or *Count* (hits, transactions, bytes) are sent as counters (`count,delta`), the rest as gauges (`gauge,min,max,sum,count`). The timestamp is the start of Azure's one-minute slot (UTC).

> **Prefer Azure to push?** [event-hub-export](event-hub-export/README.md) is the alternative: DCRs or diagnostic settings export to an Event Hub, an Event Hub-triggered Function forwards. Separate Function app with its own docs.

## Set it up

You need: Azure CLI, [Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) (`func`), Contributor on the subscription (plus Owner / User Access Administrator once for step 4) and a Dynatrace token with `metrics.ingest` (add `storage:metrics:read` to check with DQL).

**1. Variables**

```bash
az login && az account set --subscription <subscription id>
SUBS="<subscription id 1>,<subscription id 2>"    # subscriptions to poll (one is fine)
RG=rg-dt-metrics; LOC=westeurope; APP=dt-metrics-poller-$RANDOM; ST=dtmetrics$RANDOM
DT_ENVIRONMENT=https://abc12345.live.dynatrace.com   # https:// required
DT_TOKEN=<token with metrics.ingest>
```

**2. Function app** (Flex Consumption, Python, managed identity)

```bash
az provider register --namespace Microsoft.Web --wait
az group create -n $RG -l $LOC
az storage account create -g $RG -n $ST -l $LOC --sku Standard_LRS
az functionapp create -g $RG -n $APP --storage-account $ST --flexconsumption-location $LOC \
  --runtime python --runtime-version 3.11
PRINCIPAL=$(az functionapp identity assign -g $RG -n $APP --query principalId -o tsv)
```

**3. Settings and code** (in production keep `DT_TOKEN` in Key Vault)

```bash
az functionapp config appsettings set -g $RG -n $APP --settings \
  DT_ENVIRONMENT=$DT_ENVIRONMENT DT_TOKEN=$DT_TOKEN AZMON_SUBSCRIPTIONS=$SUBS
func azure functionapp publish $APP --python        # from this folder
```

**4. Allow reading metrics:** *Monitoring Reader* on every polled subscription (the batch API needs it at subscription level).

```bash
for SUB in ${SUBS//,/ }; do
  az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal \
    --role "Monitoring Reader" --scope /subscriptions/$SUB
done
```

**5. Tag what to monitor**

```bash
az tag update --resource-id <resource id> --operation merge --tags dynatrace-nearrealtime=true
```

**6. Check** (allow 5–10 minutes: Resource Graph needs a few minutes to see a new tag). The Function logs `polled hh:mm: forwarding N data points` each minute (`func azure functionapp logstream $APP`). `401/403` from Azure means the role is missing or not active yet; `400` from Dynatrace quotes the rejected lines. In Dynatrace:

```
timeseries sum(cloud.azure.microsoft_keyvault.vaults.ServiceApiHit.nearrealtime), by:{azure.resource.id}, from:now()-30m
```

Clean up with `az group delete -n $RG`.

## Settings

| Setting | Meaning |
|---|---|
| `DT_ENVIRONMENT`, `DT_TOKEN` | Dynatrace URL (with `https://`) and token with `metrics.ingest`. Required. |
| `AZMON_TAG` | Tag name that selects resources. Default `dynatrace-nearrealtime`. |
| `AZMON_SUBSCRIPTIONS` | Comma-separated subscription IDs. Default: every subscription the identity can read. A failing subscription is logged and skipped. |
| `AZMON_DELAY_MINUTES` | Age of the polled minute, default `2`. `1` is faster but loses minutes Azure hasn't finished. |
| `METRIC_KEY_SUFFIX` | Appended to metric keys, default `.nearrealtime`. |
| `AZMON_DIMENSIONS` | `all` (default) splits by all dimensions; `none` aggregates instead. |
| `AZMON_TOP` | Max series per metric and resource when splitting, default `100`. |
| `AZMON_PARALLELISM` | Parallel (subscription, resource type) groups, default `8`. |

The tag value can restrict the metrics:

| Tag value | Effect |
|---|---|
| `true` (or `all`, `yes`, `on`, `1`, `*`, empty) | all per-minute metrics |
| `ServiceApiHit;ServiceApiLatency` (`,` or `;`) | only these |
| `false`, `no`, `off`, `0`, `none` | not polled |

Azure tags don't inherit, so tag the resources themselves (the *Inherit a tag from the resource group* Azure Policy can do that). "All metrics" can be a lot of data points: restrict chatty resource types via the tag value.

## Run locally (demos)

[scripts/poll_local.py](scripts/poll_local.py) runs the same handler every minute on your machine until Ctrl-C, using your `az login` (Reader + Monitoring Reader). `DT_ENVIRONMENT` / `DT_TOKEN` come from the environment or the git-ignored `.env.local` ([.env.example](.env.example)). Stop the deployed Function meanwhile, or counters are sent twice.

```bash
pip install -r requirements.txt
az login
python scripts/poll_local.py                                                    # default tag, all readable subscriptions
python scripts/poll_local.py --tag demo --subscriptions <id>,<id> --delay-minutes 2
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests
```

The tests run the handler against local mocks of Resource Graph, ARM, the metric definitions, the Batch API and Dynatrace ingest ([tests/conftest.py](tests/conftest.py)).

**Real end-to-end test in Azure:** [scripts/e2e-poller.sh](scripts/e2e-poller.sh) deploys a throw-away resource group with a tagged Key Vault and the Function app, then checks that the values in Dynatrace equal what Azure Monitor reports (counter `ServiceApiHit`, gauge `ServiceApiLatency`).

```bash
az login
./scripts/e2e-poller.sh up       # resource group, Key Vault, Function app, deploy, role (prints the command if you may not assign roles)
./scripts/e2e-poller.sh run      # traffic, then compare Dynatrace with Azure
./scripts/e2e-poller.sh logs     # the Function's log
./scripts/e2e-poller.sh down     # or: all
```

`POLL_LOCAL=1` runs the handler on your machine instead of the deployed Function. `./scripts/e2e-poller.sh latency --minutes 15` (or `--continuous`) measures how long after each minute the data shows up in Azure Monitor and in Dynatrace.

## Limitations

- A failed poll (Azure throttling, Dynatrace outage) loses that minute; there is no replay as with an Event Hub.
- Custom / guest metrics and metrics without a 1-minute grain aren't supported. Series beyond `AZMON_TOP` are dropped.
- New tags are picked up within ~10 minutes (the resource list is cached).
- Don't poll resources that [event-hub-export](event-hub-export/README.md) also exports under the same suffix: points would be ingested twice. Its default suffix `.metricstream` differs for that reason.
- The native Dynatrace Azure integration may already cover your needs; this Function is for near-real-time metrics and extra control.

[Dynatrace metrics ingest API](https://docs.dynatrace.com/docs/discover-dynatrace/references/dynatrace-api/environment-api/metric-v2/post-ingest-metrics): 1,000 lines or 1 MB per request, points older than about an hour are rejected.
