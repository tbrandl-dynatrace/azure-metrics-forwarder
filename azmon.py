"""Pull platform metrics from Azure Monitor (Metrics Batch API) and shape them like the DCR / diagnostic-settings
export records, so function_app.to_line() can turn them into Dynatrace line protocol.

Two ways to say what to poll (both can be used together):

1. Tags (simplest): set AZMON_TAG to a tag name, e.g. "dynatrace-metrics". Every resource that carries that tag is
   polled (found with Azure Resource Graph, in all subscriptions the identity can read, or only those in
   AZMON_SUBSCRIPTIONS), and all its per-minute metrics are collected automatically (from the metric definitions of
   its resource type). The tag value can restrict the metrics: "true" / "all" = everything, "ServiceApiHit;ServiceApiLatency"
   = only those, "false" / "no" / "off" / "0" = not polled.

2. Explicit config (app setting AZMON_CONFIG, JSON list; one entry per resource type):
     [{"subscription": "<id>", "resourceType": "Microsoft.KeyVault/vaults", "resourceGroup": "optional-rg-filter",
       "metrics": ["ServiceApiHit", "ServiceApiLatency"],
       "dimensions": ["ActivityName"],     # optional: split by these dimensions
       "top": 100}]                        # optional: max series per metric/resource when splitting (default 100)

Other settings: AZMON_DELAY_MINUTES (default 2), AZMON_ARM_URL, AZMON_METRICS_URL (template with {region}).
Azure auth: DefaultAzureCredential (managed identity in Azure, `az login` locally). The identity needs
Monitoring Reader on each polled subscription (required by the batch API); Reader is enough to find resources.
"""
import datetime as dt
import json, logging, os, re, time
from collections import defaultdict

import requests

ARM_URL = os.environ.get("AZMON_ARM_URL", "https://management.azure.com").rstrip("/")
METRICS_URL = os.environ.get("AZMON_METRICS_URL", "https://{region}.metrics.monitor.azure.com")
ARM_SCOPE = "https://management.azure.com/.default"
METRICS_SCOPE = "https://metrics.monitor.azure.com/.default"
BATCH_API_VERSION = "2023-10-01"
ARM_API_VERSION = "2021-04-01"
GRAPH_API_VERSION = "2022-10-01"
DEFINITIONS_API_VERSION = "2018-01-01"
MAX_RESOURCES_PER_BATCH = 50           # API limit: same subscription, region and resource type
METRICS_PER_CALL = 20                  # metrics per request (Azure Monitor limit)
RESOURCE_CACHE_SECONDS = 600
DEFINITION_CACHE_SECONDS = 3600
AGGREGATIONS = "average,count,maximum,minimum,total"
ALL_METRICS_VALUES = {"", "true", "yes", "on", "1", "all", "*", "y"}
TAG_OFF_VALUES = ("false", "no", "off", "0", "none")

_credential = None
_resource_cache = {}                   # (subscription, type, resource group) -> (fetched at, [(resource id, location)])
_tagged_cache = {}                     # (tag, subscriptions) -> (fetched at, [row])
_definition_cache = {}                 # resource type -> (fetched at, [metric names])
_warned = set()                        # (metric, error code) already logged, so a broken metric doesn't flood the log


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


# --- explicit config: all resources of a type ---------------------------------------------------------------------

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
    """Query one AZMON_CONFIG entry and return flat metric records (dicts) for the [start, end) minute."""
    resources = list_resources(entry["subscription"], entry["resourceType"], entry.get("resourceGroup"))
    return query_batch(entry["subscription"], entry["resourceType"], resources, entry["metrics"],
                       entry.get("dimensions"), int(entry.get("top", 100)), start, end)


# --- tag-based discovery -------------------------------------------------------------------------------------------

def discover_tagged(tag, subscriptions=None):
    """Resources carrying `tag` (not set to false/no/off/0/none), via Azure Resource Graph, cached for a few minutes.
    Returns rows {id, type, location, subscriptionId, v} where v is the tag value."""
    if not re.fullmatch(r"[\w .:/@+=-]+", tag or ""):
        raise ValueError(f"unsupported characters in tag name {tag!r}")
    key = (tag, tuple(sorted(subscriptions or [])))
    hit = _tagged_cache.get(key)
    if hit and time.time() - hit[0] < RESOURCE_CACHE_SECONDS:
        return hit[1]
    off = ", ".join(f"'{v}'" for v in TAG_OFF_VALUES)
    query = (f"resources | extend v = tostring(tags['{tag}']) "
             f"| where isnotempty(v) and tolower(v) !in ({off}) "
             "| project id, type, location, subscriptionId, v")
    rows, skip = [], None
    while True:
        options = {"resultFormat": "objectArray", "$top": 1000}
        if skip:
            options["$skipToken"] = skip
        body = {"query": query, "options": options}
        if subscriptions:
            body["subscriptions"] = list(subscriptions)
        res = _request("POST", f"{ARM_URL}/providers/Microsoft.ResourceGraph/resources", ARM_SCOPE,
                       params={"api-version": GRAPH_API_VERSION}, json=body)
        rows += res.get("data", [])
        skip = res.get("$skipToken")
        if not skip:
            break
    _tagged_cache[key] = (time.time(), rows)
    return rows


def metric_names(resource_id, resource_type):
    """Names of the per-minute metrics of a resource type that can be queried without a dimension filter (cached)."""
    key = resource_type.lower()
    hit = _definition_cache.get(key)
    if hit and time.time() - hit[0] < DEFINITION_CACHE_SECONDS:
        return hit[1]
    body = _request("GET", f"{ARM_URL}{resource_id}/providers/Microsoft.Insights/metricDefinitions", ARM_SCOPE,
                    params={"api-version": DEFINITIONS_API_VERSION})
    names = []
    for d in body.get("value", []):
        name = (d.get("name") or {}).get("value")
        grains = [a.get("timeGrain") for a in d.get("metricAvailabilities") or []]
        if name and name not in names and not d.get("isDimensionRequired") and (not grains or "PT1M" in grains):
            names.append(name)
    _definition_cache[key] = (time.time(), names)
    return names


def metrics_from_tag_value(value):
    """None = all metrics of the type; otherwise the explicit metric names listed in the tag value."""
    v = (value or "").strip()
    if v.lower() in ALL_METRICS_VALUES:
        return None
    return tuple(m.strip() for m in re.split(r"[;,]", v) if m.strip())


def fetch_tagged(tag, subscriptions, start, end):
    """Poll every resource carrying `tag`, grouped by subscription / resource type / metric selection."""
    groups = defaultdict(list)
    for row in discover_tagged(tag, subscriptions):
        groups[(row["subscriptionId"], row["type"].lower(), metrics_from_tag_value(row.get("v")))].append(row)
    records = []
    for (sub, rtype, selection), rows in groups.items():
        metrics = list(selection) if selection else metric_names(rows[0]["id"], rtype)
        if metrics:
            records += query_batch(sub, rtype, [(r["id"], r.get("location") or "global") for r in rows],
                                   metrics, None, 100, start, end)
    return records


# --- Metrics Batch API ---------------------------------------------------------------------------------------------

def query_batch(subscription, resource_type, resources, metrics, dimensions, top, start, end):
    """Metrics for [(resource id, location)] of one type; batches by region, 50 resources and 20 metrics per call."""
    by_region = defaultdict(list)
    for rid, location in resources:
        by_region[location.lower().replace(" ", "")].append(rid.lower())     # the batch API wants lower-case ids

    params = {"api-version": BATCH_API_VERSION, "metricnamespace": resource_type.lower(),
              "starttime": _fmt(start), "endtime": _fmt(end), "interval": "PT1M", "aggregation": AGGREGATIONS}
    if dimensions:
        params["filter"] = " and ".join(f"{d} eq '*'" for d in dimensions)
        params["top"] = int(top)

    records = []
    for region, ids in by_region.items():
        url = METRICS_URL.format(region=region).rstrip("/") + f"/subscriptions/{subscription}/metrics:getBatch"
        for m in range(0, len(metrics), METRICS_PER_CALL):
            call_params = {**params, "metricnames": ",".join(metrics[m:m + METRICS_PER_CALL])}
            for i in range(0, len(ids), MAX_RESOURCES_PER_BATCH):
                body = _request("POST", url, METRICS_SCOPE, params=call_params,
                                json={"resourceids": ids[i:i + MAX_RESOURCES_PER_BATCH]})
                records += parse_batch_response(body)
    return records


def parse_batch_response(body):
    """values[].value[].timeseries[].data[] -> flat records with the same field names as the Event Hub exports."""
    records = []
    for res in body.get("values", []):
        for metric in res.get("value", []):
            if metric.get("errorCode", "Success") != "Success":
                name = (metric.get("name") or {}).get("value")
                if (name, metric.get("errorCode")) not in _warned:       # log each broken metric once, not every minute
                    _warned.add((name, metric.get("errorCode")))
                    logging.warning("metric %s on %s: %s %s", name, res.get("resourceid"),
                                    metric.get("errorCode"), metric.get("errorMessage"))
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
