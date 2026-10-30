"""End-to-end tests: Event Hub events → Function handler → HTTP POST → (mock) Dynatrace metrics ingest.

The Function is invoked exactly as the Azure Functions host would (a list of events) and its real HTTP
calls are received by a local server that mimics POST /api/v2/metrics/ingest. Payloads are the examples
from the Microsoft docs for both export methods.

Put DT_ENVIRONMENT + DT_TOKEN (scopes: metrics.ingest, storage:metrics:read) in the git-ignored .env.local (see .env.example), or export
them, to also run the live test against a real Dynatrace tenant.
"""
import os, re, time, datetime as dt

import pytest
import requests

from conftest import FakeEvent, ROOT

import sys
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import dql

# --- sample payloads (from the Microsoft Learn docs) ---------------------------------------------------------

DCR_KEYVAULT = {   # metrics export via DCR: flat object, capitalised keys, string values, dimensions
    "Average": "31.5", "Count": "2", "Maximum": "52", "Minimum": "11", "Total": "63",
    "resourceId": "/subscriptions/a0a0a0a0-bbbb-cccc-dddd-e1e1e1e1e1e1/resourcegroups/rg-dcrs/providers/microsoft.keyvault/vaults/dcr-vault",
    "time": "2024-08-20T14:13:00.0000000Z", "unit": "MilliSeconds", "metricName": "ServiceApiLatency", "timeGrain": "PT1M",
    "dimension": {"ActivityName": "vaultget", "ActivityType": "vault", "StatusCode": "200", "StatusCodeClass": "2xx"},
    "EventProcessedUtcTime": "2024-08-22T13:49:17.1233030Z", "PartitionId": 0, "EventEnqueuedUtcTime": "2024-08-22T13:46:04.5570000Z",
}

DIAG_WEBAPP = {    # diagnostic settings: {"records": [...]} envelope, lowercase keys, numeric values, no dimensions
    "count": 2, "total": 0.217, "minimum": 0.042, "maximum": 0.175, "average": 0.1085,
    "resourceId": "/SUBSCRIPTIONS/a0a0a0a0-bbbb-cccc-dddd-e1e1e1e1e1e1/RESOURCEGROUPS/RG-001/PROVIDERS/MICROSOFT.WEB/SITES/SCALEABLEWEBAPP1",
    "time": "2023-04-18T09:03:00.0000000Z", "metricName": "CpuTime", "timeGrain": "PT1M",
}

RESOURCE_LOG = {   # a resource log that shares the hub with metrics; must be ignored
    "time": "2023-04-18T09:39:56.5027358Z", "category": "AuditEvent", "operationName": "VaultGet",
    "resourceId": "/SUBSCRIPTIONS/a0a0a0a0-bbbb-cccc-dddd-e1e1e1e1e1e1/RESOURCEGROUPS/RG-001/PROVIDERS/MICROSOFT.KEYVAULT/VAULTS/MYKEYVAULT",
}

LINE = re.compile(r'^(?P<key>[^, ]+),(?P<dims>.*) (?P<type>gauge),min=(?P<min>[^,]+),max=(?P<max>[^,]+),'
                  r'sum=(?P<sum>[^,]+),count=(?P<count>[^ ]+) (?P<ts>\d+)$')


def parse(line):
    m = LINE.match(line)
    assert m, f"not valid line protocol: {line}"
    dims = dict(re.findall(r'([a-z0-9_.-]+)="((?:[^"\\]|\\.)*)"', m["dims"]))
    return {**m.groupdict(), "dims": dims}


def wrap(*records):
    return [{"records": list(records)}]


# --- tests -----------------------------------------------------------------------------------------------------

def test_dcr_event_reaches_dynatrace(dynatrace, load_function):
    run = load_function(dynatrace.url)
    run([FakeEvent(DCR_KEYVAULT)])

    assert len(dynatrace.requests) == 1
    req = dynatrace.requests[0]
    assert req["path"] == "/api/v2/metrics/ingest"
    assert req["headers"]["Authorization"] == "Api-Token test-token"
    assert req["headers"]["Content-Type"].startswith("text/plain")

    (line,) = req["lines"]
    p = parse(line)
    assert p["key"] == "cloud.azure.microsoft_keyvault.vaults.ServiceApiLatency.metricstream"
    assert (p["min"], p["max"], p["sum"], p["count"]) == ("11.0", "52.0", "63.0", "2")
    assert int(p["ts"]) == int(dt.datetime(2024, 8, 20, 14, 13, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert p["dims"] == {
        "cloud.provider": "azure",
        "azure.subscription": "a0a0a0a0-bbbb-cccc-dddd-e1e1e1e1e1e1",
        "azure.resource.group": "rg-dcrs",
        "azure.resource.id": DCR_KEYVAULT["resourceId"],
        "azure.resource.name": "dcr-vault",
        "azure.resource.type": "microsoft.keyvault/vaults",
        "azure.location": "westeurope",
        "activityname": "vaultget", "activitytype": "vault", "statuscode": "200", "statuscodeclass": "2xx",
    }


def test_diagnostic_settings_event_reaches_dynatrace(dynatrace, load_function):
    run = load_function(dynatrace.url)
    run([FakeEvent(wrap(DIAG_WEBAPP))])

    (line,) = dynatrace.lines
    p = parse(line)
    assert p["key"] == "cloud.azure.microsoft_web.sites.CpuTime.metricstream"
    assert (p["min"], p["max"], p["sum"], p["count"]) == ("0.042", "0.175", "0.217", "2")
    assert p["dims"]["azure.resource.id"] == DIAG_WEBAPP["resourceId"].lower()
    assert p["dims"]["azure.resource.group"] == "rg-001"
    assert p["dims"]["azure.resource.type"] == "microsoft.web/sites"
    assert p["dims"]["azure.location"] == "westeurope"
    assert set(p["dims"]) == {"cloud.provider", "azure.subscription", "azure.resource.group", "azure.resource.id",
                              "azure.resource.name", "azure.resource.type", "azure.location"}   # no dimensions


def test_both_export_methods_produce_the_same_series(dynatrace, load_function):
    """The same resource/metric exported by a DCR and by a diagnostic setting must land in the same series."""
    rid = "/subscriptions/a0a0a0a0-bbbb-cccc-dddd-e1e1e1e1e1e1/resourceGroups/rg-001/providers/Microsoft.Web/sites/webapp1"
    dcr = {"Count": "1", "Minimum": "1", "Maximum": "1", "Total": "1", "Average": "1", "resourceId": rid,
           "time": "2024-01-01T00:00:00.0000000Z", "metricName": "Requests", "timeGrain": "PT1M"}
    diag = {"count": 1, "minimum": 1, "maximum": 1, "total": 1, "average": 1, "resourceId": rid.upper(),
            "time": "2024-01-01T00:00:00.0000000Z", "metricName": "Requests", "timeGrain": "PT1M"}
    run = load_function(dynatrace.url)
    run([FakeEvent(dcr), FakeEvent(wrap(diag))])

    a, b = (parse(l) for l in dynatrace.lines)
    assert a["key"] == b["key"]
    for k in ("cloud.provider", "azure.subscription", "azure.resource.group", "azure.resource.id",
              "azure.resource.type", "azure.location"):
        assert a["dims"][k] == b["dims"][k], k


def test_non_metric_records_are_ignored(dynatrace, load_function):
    run = load_function(dynatrace.url)
    run([FakeEvent(wrap(RESOURCE_LOG, DIAG_WEBAPP, {"foo": "bar"}))])
    assert len(dynatrace.lines) == 1
    assert parse(dynatrace.lines[0])["key"].startswith("cloud.azure.microsoft_web.sites.CpuTime")


def test_no_metrics_means_no_request(dynatrace, load_function):
    run = load_function(dynatrace.url)
    run([FakeEvent(wrap(RESOURCE_LOG))])
    assert dynatrace.requests == []


def test_zero_count_samples_are_dropped(dynatrace, load_function):
    run = load_function(dynatrace.url)
    run([FakeEvent(wrap({**DIAG_WEBAPP, "count": 0}))])
    assert dynatrace.requests == []


def test_requests_are_batched_at_1000_lines(dynatrace, load_function):
    run = load_function(dynatrace.url)
    events = [FakeEvent(wrap(*[{**DIAG_WEBAPP, "metricName": f"Metric{i}"} for i in range(n, n + 500)]))
              for n in range(0, 2500, 500)]
    run(events)
    assert [len(r["lines"]) for r in dynatrace.requests] == [1000, 1000, 500]
    assert len(set(dynatrace.lines)) == 2500


def test_special_characters_are_escaped(dynatrace, load_function):
    rec = {**DCR_KEYVAULT, "dimension": {"Path": 'a "quoted" \\ value', "Empty": "", "Weird Key!": "x"}}
    run = load_function(dynatrace.url)
    run([FakeEvent(rec)])
    p = parse(dynatrace.lines[0])
    assert p["dims"]["path"] == 'a \\"quoted\\" \\\\ value'
    assert "empty" not in p["dims"]
    assert p["dims"]["weird_key_"] == "x"


@pytest.mark.parametrize("status", [400, 401, 500, 503])
def test_dynatrace_errors_propagate_so_event_hubs_retries(dynatrace, load_function, status):
    dynatrace.status = status
    run = load_function(dynatrace.url)
    with pytest.raises(requests.HTTPError):
        run([FakeEvent(DCR_KEYVAULT)])


def test_handler_annotation_is_accepted_by_the_python_worker(load_function):
    """The Functions Python worker rejects builtin generics (list[...]) for a cardinality=many Event Hub trigger
    ("FunctionLoadError: binding events has invalid non-type annotation"); it needs typing.List[...]."""
    import types, typing
    fn = load_function("http://127.0.0.1:1")
    annotation = typing.get_type_hints(fn)["events"]
    assert not isinstance(annotation, types.GenericAlias), "use typing.List[...], not list[...]"
    assert typing.get_origin(annotation) is list and typing.get_args(annotation)[0].__name__ == "EventHubEvent"


# --- live test against a real Dynatrace tenant (opt-in) --------------------------------------------------------

LIVE_ENV, LIVE_TOKEN = os.environ.get("DT_ENVIRONMENT"), os.environ.get("DT_TOKEN")


@pytest.mark.skipif(not (LIVE_ENV and LIVE_TOKEN), reason="set DT_ENVIRONMENT and DT_TOKEN (e.g. in .env.local) to run")
def test_live_metrics_arrive_in_dynatrace(load_function):
    run = load_function(LIVE_ENV, LIVE_TOKEN)
    name = f"e2e-{int(time.time())}"
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    record = {"count": 3, "total": 6, "minimum": 1, "maximum": 3, "average": 2,
              "resourceId": f"/SUBSCRIPTIONS/00000000-0000-0000-0000-000000000000/RESOURCEGROUPS/RG-E2E/PROVIDERS/MICROSOFT.E2ETEST/FORWARDER/{name}",
              "time": now.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"), "metricName": "E2EProbe", "timeGrain": "PT1M"}
    run([FakeEvent(wrap(record))])                       # raises if the ingest API rejects the request

    dql_text = ("timeseries s = sum(`cloud.azure.microsoft_e2etest.forwarder.E2EProbe.metricstream`), "
                f'filter: azure.resource.name == "{name}", from: now()-30m '
                "| fieldsAdd total = arraySum(s) | fields total")
    deadline = time.time() + 180
    while time.time() < deadline:                        # ingested metrics can take a short while to become queryable
        records = dql.query(dql_text)
        if records:
            assert records[0]["total"] == 6.0, records
            return
        time.sleep(10)
    pytest.fail("metric did not become queryable via DQL within 3 minutes")
