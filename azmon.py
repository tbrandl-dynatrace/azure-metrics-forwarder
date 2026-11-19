"""Pull platform metrics from Azure Monitor (Metrics Batch API) and shape them as flat records that
function_app.to_line() turns into Dynatrace line protocol.

What to poll is decided by an Azure tag (AZMON_TAG, e.g. "dynatrace-metrics"): every resource that carries it, of any
resource type, in any resource group, is polled. Resources are found with Azure Resource Graph in all subscriptions the
identity can read, or only in AZMON_SUBSCRIPTIONS (comma-separated, any number). All per-minute metrics of the
resource type are collected, split by ALL of their dimensions (from the metric definitions); AZMON_DIMENSIONS=none
aggregates over dimensions instead. The tag value can restrict the metrics: "true" / "all" = everything,
"ServiceApiHit;ServiceApiLatency" = only those, "false" / "no" / "off" / "0" = not polled.

Other settings: AZMON_DELAY_MINUTES (default 2), AZMON_TOP (max series per metric and resource when splitting, default
100), AZMON_PARALLELISM (default 8), AZMON_ARM_URL, AZMON_METRICS_URL (template with {region}).
Azure auth: DefaultAzureCredential (managed identity in Azure, `az login` locally). The identity needs
Monitoring Reader on each polled subscription (required by the batch API); Reader is enough to find resources.
"""
import datetime as dt
import logging, os, re, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

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
DEFAULT_TOP = 100                      # max series per metric and resource when splitting by dimensions
DEFINITION_CACHE_SECONDS = 3600
AGGREGATIONS = "average,count,maximum,minimum,total"
ALL_METRICS_VALUES = {"", "true", "yes", "on", "1", "all", "*", "y"}
TAG_OFF_VALUES = ("false", "no", "off", "0", "none")

_credential = None
_tagged_cache = {}                     # (tag, subscriptions) -> (fetched at, [row])
_definition_cache = {}                 # resource type -> (fetched at, [metric names])
_warned = set()                        # (metric, error code) already logged, so a broken metric doesn't flood the log


def get_token(scope: str) -> str:
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential
        _credential = DefaultAzureCredential()
    return _credential.get_token(scope).token


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


def metric_definitions(resource_id, resource_type):
    """Metric definitions of a resource type (cached): [{"name", "kind", "dimensions", "required", "per_minute"}].
    dimensions: names of the metric's dimensions; required: Azure only answers when they are split;
    per_minute: a 1-minute time grain is available.
    kind:   "count" for metrics Azure aggregates as a Total (bytes, transactions ... summed per minute, i.e. counters),
            "samples" for metrics whose primary aggregation is Count (e.g. Key Vault ServiceApiHit: every event is a
            sample, the number of samples per minute is the counter),
            "gauge" for everything else (Average / Minimum / Maximum: percentages, latencies, sizes)."""
    key = resource_type.lower()
    hit = _definition_cache.get(key)
    if hit and time.time() - hit[0] < DEFINITION_CACHE_SECONDS:
        return hit[1]
    body = _request("GET", f"{ARM_URL}{resource_id}/providers/Microsoft.Insights/metricDefinitions", ARM_SCOPE,
                    params={"api-version": DEFINITIONS_API_VERSION})
    defs = []
    for d in body.get("value", []):
        name = (d.get("name") or {}).get("value")
        if not name or any(x["name"] == name for x in defs):
            continue
        grains = [a.get("timeGrain") for a in d.get("metricAvailabilities") or []]
        defs.append({"name": name,
                     "kind": {"total": "count", "count": "samples"}.get(str(d.get("primaryAggregationType", "")).lower(), "gauge"),
                     "dimensions": [x.get("value") for x in d.get("dimensions") or [] if x.get("value")],
                     "required": bool(d.get("isDimensionRequired")),
                     "per_minute": not grains or "PT1M" in grains})
    _definition_cache[key] = (time.time(), defs)
    return defs


def metric_kinds(resource_id, resource_type):
    """{lower-case metric name: "count" | "samples" | "gauge"} for a resource type."""
    return {d["name"].lower(): d["kind"] for d in metric_definitions(resource_id, resource_type)}


def metric_plan(resource_id, resource_type, selection, split_dimensions=True):
    """{dimension names (tuple): [metric names]}: which metrics to request, each with the dimensions to split by.
    selection None = every per-minute metric of the type; otherwise just the listed names."""
    defs = {d["name"].lower(): d for d in metric_definitions(resource_id, resource_type)}
    if selection is None:
        names = [(d["name"], d) for d in defs.values() if d["per_minute"]]
    else:
        names = [(n, defs.get(n.lower())) for n in selection]
    plan = defaultdict(list)
    for name, d in names:
        dims = tuple(d["dimensions"]) if (d and split_dimensions) else ()
        if d and d["required"] and not dims:
            continue                                    # Azure rejects these without a dimension split
        plan[dims].append(name)
    return dict(plan)


def metrics_from_tag_value(value):
    """None = all metrics of the type; otherwise the explicit metric names listed in the tag value."""
    v = (value or "").strip()
    if v.lower() in ALL_METRICS_VALUES:
        return None
    return tuple(m.strip() for m in re.split(r"[;,]", v) if m.strip())


def fetch_tagged(tag, subscriptions, start, end, split_dimensions=True, top=DEFAULT_TOP, parallelism=8):
    """Poll every resource carrying `tag`, grouped by subscription / resource type / metric selection.
    Groups run in parallel; one failing group (e.g. no Monitoring Reader in one subscription) is logged and does not
    stop the others. Raises only when every group failed."""
    groups = defaultdict(list)
    for row in discover_tagged(tag, subscriptions):
        groups[(row["subscriptionId"], row["type"].lower(), metrics_from_tag_value(row.get("v")))].append(row)

    def poll_group(item):
        (sub, rtype, selection), rows = item
        kinds = metric_kinds(rows[0]["id"], rtype)
        resources = [(r["id"], r.get("location") or "global") for r in rows]
        records = []
        for dims, metrics in metric_plan(rows[0]["id"], rtype, selection, split_dimensions).items():
            records += query_batch(sub, rtype, resources, metrics, list(dims) or None, top, start, end, kinds)
        return records

    records, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
        for (key, _), result in zip(groups.items(), pool.map(_safely(poll_group), groups.items())):
            if isinstance(result, Exception):
                errors.append(result)
                logging.error("polling %s in subscription %s failed: %s", key[1], key[0], str(result)[:300])
            else:
                records += result
    if errors and len(errors) == len(groups):
        raise errors[0]
    return records


def _safely(fn):
    def wrapper(item):
        try:
            return fn(item)
        except Exception as e:
            return e
    return wrapper


def fetch_resource(resource_id, metrics, start, end):
    """Metrics (no dimension split) of one resource, for scripts and probes. Looks up the region itself."""
    m = re.match(r"^/subscriptions/([^/]+)/resourcegroups/[^/]+/providers/([^/]+)/([^/]+)/", resource_id + "/", re.I)
    sub, rtype = m[1], f"{m[2]}/{m[3]}"
    location = _request("GET", f"{ARM_URL}{resource_id}", ARM_SCOPE, params={"api-version": ARM_API_VERSION}).get("location", "global")
    return query_batch(sub, rtype, [(resource_id, location)], list(metrics), None, DEFAULT_TOP, start, end,
                       metric_kinds(resource_id, rtype))


# --- Metrics Batch API ---------------------------------------------------------------------------------------------

def query_batch(subscription, resource_type, resources, metrics, dimensions, top, start, end, kinds=None):
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
                records += parse_batch_response(body, kinds)
    return records


def parse_batch_response(body, kinds=None):
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
                        "kind": (kinds or {}).get(metric["name"]["value"].lower(), "gauge"),
                        "time": point["timeStamp"], "unit": metric.get("unit"), "timeGrain": "PT1M",
                        "count": point.get("count"), "total": point.get("total"),
                        "minimum": point.get("minimum"), "maximum": point.get("maximum"),
                        "average": point.get("average"), "dimension": dims,
                    })
    return records
