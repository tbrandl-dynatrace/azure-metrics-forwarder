import os, re, json, datetime as dt, requests, azure.functions as func
from typing import List

app = func.FunctionApp()

DT_URL = os.environ["DT_ENVIRONMENT"].rstrip("/") + "/api/v2/metrics/ingest"
HEADERS = {"Authorization": f"Api-Token {os.environ['DT_TOKEN']}",
           "Content-Type": "text/plain; charset=utf-8"}
KEY_SUFFIX = ".metricstream"       # appended to every metric key to avoid clashing with the native metrics

# The Event Hub, the exporting DCR / diagnostic setting and the monitored resources share one region, so the
# location is constant per deployment.
# Set AZURE_LOCATION explicitly (e.g. "westeurope"); falls back to REGION_NAME (e.g. "West Europe") set by Azure Functions.
LOCATION = re.sub(r"[^a-z0-9]", "", (os.environ.get("AZURE_LOCATION") or os.environ.get("REGION_NAME", "")).lower())

RID = re.compile(r"^/subscriptions/(?P<sub>[^/]+)/resourcegroups/(?P<rg>[^/]+)/providers/(?P<ns>[^/]+)/(?P<rest>.+)$", re.I)


def parse_resource_id(rid: str):
    m = RID.match(rid)
    if not m:
        return None
    segs = m["rest"].split("/")                  # e.g. servers/s1/databases/db1
    types = segs[0::2]                           # resource type names: servers, databases
    ns = m["ns"].lower()                         # microsoft.sql
    return {
        "subscription": m["sub"].lower(),
        "resource_group": m["rg"].lower(),
        "namespace": ns,
        "type_path": [t.lower() for t in types],
        "arm_type": f'{ns}/{"/".join(t.lower() for t in types)}',
        "name": segs[-1],
    }


def metric_key(p, metric_name: str) -> str:
    # cloud.azure.<provider_namespace>.<resource_type...>.<MetricName>
    ns = p["namespace"].replace(".", "_")                       # microsoft.sql -> microsoft_sql
    name = re.sub(r"[^A-Za-z0-9_]", "", metric_name)            # "Percentage CPU" -> "PercentageCPU"
    return ".".join(["cloud.azure", ns, *p["type_path"], name]) + KEY_SUFFIX


def dim_key(k: str) -> str:
    k = re.sub(r"[^a-z0-9_.-]", "_", k.lower())
    return k if re.match(r"[a-z]", k) else "d_" + k


def esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def iter_records(body):
    """Yield flat metric records from either export format.

    DCR export:                  one flat JSON object per metric, e.g. {"Average": "1", "resourceId": ..., ...}
    Diagnostic settings export:  [{"records": [{"count": 2, "resourceId": ..., ...}, ...]}]
    Non-metric records (e.g. resource logs sent to the same hub) are dropped later in to_line().
    """
    if isinstance(body, list):
        for item in body:
            yield from iter_records(item)
    elif isinstance(body, dict):
        if isinstance(body.get("records"), list):
            yield from iter_records(body["records"])
        else:
            yield body


def to_line(raw: dict):
    r = {k.lower(): v for k, v in raw.items()}                  # DCR: "Count"/"resourceId", diag settings: "count"/"resourceId"
    required = ("resourceid", "metricname", "count", "minimum", "maximum", "total", "time")
    if any(r.get(k) in (None, "") for k in required):
        return None                                             # not a metric record
    p = parse_resource_id(r["resourceid"])
    if not p:
        return None
    count = float(r["count"])
    if count <= 0:
        return None

    dims = {                                                    # Dynatrace Azure semantic attributes
        "cloud.provider": "azure",
        "azure.subscription": p["subscription"],
        "azure.resource.group": p["resource_group"],
        "azure.resource.id": r["resourceid"].lower(),           # diag settings send the ID upper-cased
        "azure.resource.name": p["name"],
        "azure.resource.type": p["arm_type"],
    }
    if LOCATION:
        dims["azure.location"] = LOCATION
    dimension = r.get("dimension")                              # DCR export only; diag settings have no dimensions
    for k, v in (dimension if isinstance(dimension, dict) else {}).items():
        if v not in (None, ""):
            dims[dim_key(k)] = v

    ts = dt.datetime.strptime(r["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    dim_s = ",".join(f'{dim_key(k)}="{esc(v)}"' for k, v in dims.items())
    return (f'{metric_key(p, r["metricname"])},{dim_s} '
            f'gauge,min={float(r["minimum"])},max={float(r["maximum"])},'
            f'sum={float(r["total"])},count={count:g} {int(ts.timestamp() * 1000)}')


@app.event_hub_message_trigger(arg_name="events", event_hub_name="%EH_NAME%",
                               connection="EH_CONN", cardinality="many")
# typing.List, not builtin list[...]: the Python worker rejects the latter (FunctionLoadError)
def run(events: List[func.EventHubEvent]):
    lines = []
    for e in events:
        for r in iter_records(json.loads(e.get_body())):
            line = to_line(r)
            if line:
                lines.append(line)
    for i in range(0, len(lines), 1000):                        # API limit: 1000 lines per request
        resp = requests.post(DT_URL, headers=HEADERS, data="\n".join(lines[i:i + 1000]), timeout=30)
        resp.raise_for_status()                                 # let the trigger retry the batch
