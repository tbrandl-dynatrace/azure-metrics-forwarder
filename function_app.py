import logging, os, re, datetime as dt, requests, azure.functions as func

import azmon

app = func.FunctionApp()

DT_URL = os.environ["DT_ENVIRONMENT"].rstrip("/") + "/api/v2/metrics/ingest"
HEADERS = {"Authorization": f"Api-Token {os.environ['DT_TOKEN']}",
           "Content-Type": "text/plain; charset=utf-8"}
KEY_SUFFIX = os.environ.get("METRIC_KEY_SUFFIX", ".nearrealtime")   # appended to every metric key; keeps these series apart from the native metrics

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


def to_line(raw: dict):
    r = {k.lower(): v for k, v in raw.items()}
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
    location = r.get("location")                                # the resource's region, from Azure Resource Graph
    if location:
        dims["azure.location"] = re.sub(r"[^a-z0-9]", "", str(location).lower())
    dimension = r.get("dimension")                              # DCR export only; diag settings have no dimensions
    for k, v in (dimension if isinstance(dimension, dict) else {}).items():
        if v not in (None, ""):
            dims[dim_key(k)] = v

    ts = dt.datetime.strptime(r["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    dim_s = ",".join(f'{dim_key(k)}="{esc(v)}"' for k, v in dims.items())
    key = f'{metric_key(p, r["metricname"])},{dim_s}'
    if r.get("kind") in ("count", "samples"):
        # Azure aggregates this metric as a Total (or counts its samples) per minute, i.e. a counter: send the minute's
        # increase as a counter delta
        delta = float(r["total"]) if r["kind"] == "count" else count
        return f'{key} count,delta={delta} {int(ts.timestamp() * 1000)}'
    total = float(r["total"])
    if count != int(count):
        # Dynatrace needs an integer sample count; some metrics (e.g. SaturationShoebox) report fractional ones.
        # Keep the mean: one sample whose value is the average.
        avg = r.get("average")
        total = float(avg) if avg not in (None, "") else total / count
        count = 1
    return (f'{key} gauge,min={float(r["minimum"])},max={float(r["maximum"])},'
            f'sum={total},count={int(count)} {int(ts.timestamp() * 1000)}')


NEWLINE = chr(10)


def describe_rejection(resp, batch):
    """The ingest error with the rejected lines quoted (the API reports them by 1-based line number)."""
    try:
        invalid = resp.json()["error"]["invalidLines"]
        return NEWLINE.join(f'{x.get("error")} -> {batch[x["line"] - 1]}' for x in invalid)
    except Exception:
        return resp.text[:1500]


def post_lines(lines):
    for i in range(0, len(lines), 1000):                        # API limit: 1000 lines per request
        resp = requests.post(DT_URL, headers=HEADERS, data=NEWLINE.join(lines[i:i + 1000]), timeout=30)
        if not resp.ok:                                         # let the trigger retry the batch, but say why
            raise requests.HTTPError(f"{resp.status_code} from Dynatrace ingest: {describe_rejection(resp, lines[i:i + 1000])}",
                                     response=resp)


# Poll the Azure Monitor metrics API every minute for the minute that is
# AZMON_DELAY_MINUTES (default 2) old and forward it. Does nothing unless AZMON_TAG or AZMON_CONFIG is set (see azmon.py).
@app.timer_trigger(schedule="0 * * * * *", arg_name="timer", run_on_startup=False, use_monitor=False)
def poll(timer: func.TimerRequest) -> None:
    raw, tag = os.environ.get("AZMON_CONFIG"), os.environ.get("AZMON_TAG")
    if not raw and not tag:
        logging.info("neither AZMON_TAG nor AZMON_CONFIG is set; metrics polling is disabled")
        return
    start, end = azmon.target_window(delay_minutes=int(os.environ.get("AZMON_DELAY_MINUTES", "2")))
    records = []
    for entry in azmon.load_config(raw) if raw else []:
        records += azmon.fetch_records(entry, start, end)
    if tag:
        subscriptions = [s.strip() for s in os.environ.get("AZMON_SUBSCRIPTIONS", "").split(",") if s.strip()] or None
        records += azmon.fetch_tagged(tag, subscriptions, start, end)
    lines = []
    for r in records:
        line = to_line(r)
        if line:
            lines.append(line)
    logging.info("polled %s: forwarding %d data points", start.strftime("%H:%M"), len(lines))
    post_lines(lines)
