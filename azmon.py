"""Pull platform metrics from Azure Monitor (Metrics Batch API) and shape them like the DCR / diagnostic-settings
export records, so function_app.to_line() can turn them into Dynatrace line protocol.

Configuration (app setting AZMON_CONFIG, JSON list; one entry per resource type to poll):
  [{"subscription": "<id>",
    "resourceType": "Microsoft.KeyVault/vaults",
    "resourceGroup": "optional-rg-filter",
    "metrics": ["ServiceApiHit", "ServiceApiLatency"],
    "dimensions": ["ActivityName"],      # optional: split by these dimensions
    "top": 100}]                         # optional: max series per metric/resource when splitting (default 100)

Other settings: AZMON_DELAY_MINUTES (default 2), AZMON_ARM_URL, AZMON_METRICS_URL (template with {region}).
Azure auth: DefaultAzureCredential (managed identity in Azure, `az login` locally). The identity needs
Monitoring Reader on each polled subscription (required by the batch API).
"""
import datetime as dt
import json, logging, os, time
from collections import defaultdict

import requests

ARM_URL = os.environ.get("AZMON_ARM_URL", "https://management.azure.com").rstrip("/")
METRICS_URL = os.environ.get("AZMON_METRICS_URL", "https://{region}.metrics.monitor.azure.com")
ARM_SCOPE = "https://management.azure.com/.default"
METRICS_SCOPE = "https://metrics.monitor.azure.com/.default"
BATCH_API_VERSION = "2023-10-01"
ARM_API_VERSION = "2021-04-01"
MAX_RESOURCES_PER_BATCH = 50           # API limit: same subscription, region and resource type
RESOURCE_CACHE_SECONDS = 600
AGGREGATIONS = "average,count,maximum,minimum,total"

_credential = None
_resource_cache = {}                   # (subscription, type, resource group) -> (fetched at, [(resource id, location)])


def get_token(scope: str) -> str:
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential
        _credential = DefaultAzureCredential()
    return _credential.get_token(scope).token


def load_config(raw: str):
    cfg = json.loads(raw)
    if isinstance(cfg, dict):
        cfg = [cfg]
    for i, c in enumerate(cfg):
        for k in ("subscription", "resourceType", "metrics"):
            if not c.get(k):
                raise ValueError(f"AZMON_CONFIG[{i}] is missing '{k}'")
    return cfg


def target_window(now: dt.datetime = None, delay_minutes: int = 2):
    """The one-minute slot that is `delay_minutes` old: [start, start + 1 min)."""
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(second=0, microsecond=0)
    start = now - dt.timedelta(minutes=delay_minutes)
    return start, start + dt.timedelta(minutes=1)


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _request(method, url, scope, retries=2, **kw):
    """HTTP call with bearer token; backs off briefly on 429/529 (throttling)."""
    for attempt in range(retries + 1):
        r = requests.request(method, url, headers={"Authorization": f"Bearer {get_token(scope)}"}, timeout=30, **kw)
        if r.status_code in (429, 529) and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()


def list_resources(subscription, resource_type, resource_group=None):
    """[(resource id, location)] for a resource type, cached for a few minutes."""
    key = (subscription, resource_type.lower(), (resource_group or "").lower())
    hit = _resource_cache.get(key)
    if hit and time.time() - hit[0] < RESOURCE_CACHE_SECONDS:
        return hit[1]
    scope = f"/subscriptions/{subscription}" + (f"/resourceGroups/{resource_group}" if resource_group else "")
    url = f"{ARM_URL}{scope}/resources"
    params = {"$filter": f"resourceType eq '{resource_type}'", "api-version": ARM_API_VERSION}
    found = []
    while url:
        body = _request("GET", url, ARM_SCOPE, params=params)
        found += [(r["id"], r.get("location", "global")) for r in body.get("value", [])]
        url, params = body.get("nextLink"), None
    _resource_cache[key] = (time.time(), found)
    return found


def fetch_records(entry, start, end):
    """Query one config entry and return flat metric records (dicts) for the [start, end) minute."""
    resources = list_resources(entry["subscription"], entry["resourceType"], entry.get("resourceGroup"))
    by_region = defaultdict(list)
    for rid, location in resources:
        by_region[location.lower().replace(" ", "")].append(rid.lower())     # the batch API wants lower-case ids

    params = {
        "api-version": BATCH_API_VERSION,
        "metricnamespace": entry["resourceType"].lower(),
        "metricnames": ",".join(entry["metrics"]),
        "starttime": _fmt(start), "endtime": _fmt(end),
        "interval": "PT1M", "aggregation": AGGREGATIONS,
    }
    if entry.get("dimensions"):
        params["filter"] = " and ".join(f"{d} eq '*'" for d in entry["dimensions"])
        params["top"] = int(entry.get("top", 100))

    records = []
    for region, ids in by_region.items():
        url = METRICS_URL.format(region=region).rstrip("/") + f"/subscriptions/{entry['subscription']}/metrics:getBatch"
        for i in range(0, len(ids), MAX_RESOURCES_PER_BATCH):
            body = _request("POST", url, METRICS_SCOPE, params=params, json={"resourceids": ids[i:i + MAX_RESOURCES_PER_BATCH]})
            records += parse_batch_response(body)
    return records


def parse_batch_response(body):
    """values[].value[].timeseries[].data[] -> flat records with the same field names as the Event Hub exports."""
    records = []
    for res in body.get("values", []):
        for metric in res.get("value", []):
            if metric.get("errorCode", "Success") != "Success":
                logging.warning("metric %s on %s: %s %s", (metric.get("name") or {}).get("value"),
                                res.get("resourceid"), metric.get("errorCode"), metric.get("errorMessage"))
                continue
            for series in metric.get("timeseries", []):
                dims = {(m.get("name") or {}).get("value", ""): m.get("value")
                        for m in series.get("metadatavalues", [])}
                for point in series.get("data", []):
                    if "timeStamp" not in point:
                        continue
                    records.append({
                        "resourceId": res["resourceid"], "location": res.get("resourceregion"), "metricName": metric["name"]["value"],
                        "time": point["timeStamp"], "unit": metric.get("unit"), "timeGrain": "PT1M",
                        "count": point.get("count"), "total": point.get("total"),
                        "minimum": point.get("minimum"), "maximum": point.get("maximum"),
                        "average": point.get("average"), "dimension": dims,
                    })
    return records
