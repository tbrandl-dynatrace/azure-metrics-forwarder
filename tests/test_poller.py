"""End-to-end tests for the metrics poller: timer → Azure Monitor (mock ARM + Metrics Batch API) → Dynatrace (mock).

The response bodies follow the Metrics Batch API reference (api-version 2023-10-01).
"""
import datetime as dt

import pytest
import requests

from test_e2e import parse

SUB = "a0a0a0a0-bbbb-cccc-dddd-e1e1e1e1e1e1"
KV = f"/subscriptions/{SUB}/resourceGroups/rg-1/providers/Microsoft.KeyVault/vaults"
CONFIG = [{"subscription": SUB, "resourceType": "Microsoft.KeyVault/vaults",
           "metrics": ["ServiceApiHit", "ServiceApiLatency"]}]


def batch_value(resource_id, region, metrics, ts="2026-11-17T08:10:00Z"):
    """One values[] element of a getBatch response. metrics: {name: (series list of (dims, datapoint))}"""
    return {
        "starttime": ts, "endtime": ts, "interval": "PT1M", "namespace": "microsoft.keyvault/vaults",
        "resourceregion": region, "resourceid": resource_id,
        "value": [{
            "id": f"{resource_id}/providers/Microsoft.Insights/metrics/{name}", "type": "Microsoft.Insights/metrics",
            "name": {"value": name, "localizedValue": name}, "unit": "Count", "errorCode": "Success",
            "timeseries": [{"metadatavalues": [{"name": {"value": k, "localizedValue": k}, "value": v}
                                               for k, v in dims.items()],
                            "data": [{"timeStamp": ts, **point}]} for dims, point in series],
        } for name, series in metrics.items()],
    }


POINT = {"average": 3.0, "count": 2.0, "maximum": 4.0, "minimum": 2.0, "total": 6.0}


def respond_with_hits(call):
    return {"values": [batch_value(rid, call["region"], {"ServiceApiHit": [({}, POINT)]}) for rid in call["resourceids"]]}


def test_poll_forwards_metrics_to_dynatrace(dynatrace, azure, load_poller):
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"},
                       {"id": f"{KV}/vault-b", "location": "westeurope"}]
    azure.batch_response = respond_with_hits
    poll = load_poller(dynatrace.url, CONFIG)
    before = dt.datetime.now(dt.timezone.utc)
    poll(None)

    # one batch call for the region, with the documented parameters
    (call,) = azure.batch_calls
    assert call["region"] == "westeurope" and call["subscription"] == SUB
    assert call["resourceids"] == [f"{KV}/vault-a".lower(), f"{KV}/vault-b".lower()]
    assert call["auth"] == "Bearer fake-token-for-metrics.monitor.azure.com"
    q = call["params"]
    assert q["api-version"] == "2023-10-01"
    assert q["metricnamespace"] == "microsoft.keyvault/vaults"
    assert q["metricnames"] == "ServiceApiHit,ServiceApiLatency"
    assert q["interval"] == "PT1M" and q["aggregation"] == "average,count,maximum,minimum,total"

    # the requested slot is one minute long and two minutes old (to the minute)
    start = dt.datetime.strptime(q["starttime"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.strptime(q["endtime"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.timezone.utc)
    assert end - start == dt.timedelta(minutes=1) and start.second == 0
    age = before.replace(second=0, microsecond=0) - start
    assert age in (dt.timedelta(minutes=2), dt.timedelta(minutes=3))      # 3 only if a minute boundary passed

    # both vaults arrive in Dynatrace with the suffix and semantic attributes
    assert len(dynatrace.lines) == 2
    p = parse(dynatrace.lines[0])
    assert p["key"] == "cloud.azure.microsoft_keyvault.vaults.ServiceApiHit.metricstream"
    assert (p["min"], p["max"], p["sum"], p["count"]) == ("2.0", "4.0", "6.0", "2")
    assert p["dims"]["azure.resource.id"] == f"{KV}/vault-a".lower()
    assert p["dims"]["azure.resource.name"] == "vault-a"
    assert p["dims"]["azure.location"] == "westeurope"
    assert p["dims"]["azure.subscription"] == SUB
    assert p["dims"]["azure.resource.type"] == "microsoft.keyvault/vaults"


def test_dimensions_are_split_and_forwarded(dynatrace, azure, load_poller):
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"}]
    azure.batch_response = lambda call: {"values": [batch_value(
        call["resourceids"][0], "westeurope",
        {"ServiceApiHit": [({"ActivityName": "vaultget"}, POINT), ({"ActivityName": "keycreate"}, POINT)]})]}
    poll = load_poller(dynatrace.url, [{**CONFIG[0], "metrics": ["ServiceApiHit"], "dimensions": ["ActivityName"]}])
    poll(None)

    assert azure.batch_calls[0]["params"]["filter"] == "ActivityName eq '*'"
    assert azure.batch_calls[0]["params"]["top"] == "100"
    assert sorted(parse(l)["dims"]["activityname"] for l in dynatrace.lines) == ["keycreate", "vaultget"]


def test_resources_are_grouped_by_region_and_batched_by_50(dynatrace, azure, load_poller):
    azure.resources = ([{"id": f"{KV}/we-{i}", "location": "westeurope"} for i in range(120)]
                       + [{"id": f"{KV}/ne-{i}", "location": "North Europe"} for i in range(3)])
    azure.batch_response = respond_with_hits
    poll = load_poller(dynatrace.url, CONFIG)
    poll(None)

    sizes = sorted((c["region"], len(c["resourceids"])) for c in azure.batch_calls)
    assert sizes == [("northeurope", 3), ("westeurope", 20), ("westeurope", 50), ("westeurope", 50)]
    assert len(dynatrace.lines) == 123
    ne = [parse(l) for l in dynatrace.lines if "/ne-0" in l][0]
    assert ne["dims"]["azure.location"] == "northeurope"            # per-resource region, not AZURE_LOCATION


def test_failed_metrics_are_skipped_others_forwarded(dynatrace, azure, load_poller):
    def response(call):
        v = batch_value(call["resourceids"][0], "westeurope",
                        {"ServiceApiHit": [({}, POINT)], "ServiceApiLatency": [({}, POINT)]})
        v["value"][1]["errorCode"] = "InvalidMetric"
        v["value"][1]["errorMessage"] = "metric not supported"
        return {"values": [v]}
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"}]
    azure.batch_response = response
    poll = load_poller(dynatrace.url, CONFIG)
    poll(None)
    assert [parse(l)["key"].split(".")[-2] for l in dynatrace.lines] == ["ServiceApiHit"]


def test_points_without_data_are_ignored(dynatrace, azure, load_poller):
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"}]
    azure.batch_response = lambda call: {"values": [batch_value(call["resourceids"][0], "westeurope",
                                                                 {"ServiceApiHit": [({}, {})]})]}      # only timeStamp
    poll = load_poller(dynatrace.url, CONFIG)
    poll(None)
    assert dynatrace.requests == []


def test_resource_list_is_cached_between_polls(dynatrace, azure, load_poller):
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"}]
    azure.batch_response = respond_with_hits
    poll = load_poller(dynatrace.url, CONFIG)
    poll(None)
    poll(None)
    assert azure.arm_calls == 1 and len(azure.batch_calls) == 2


def test_polling_is_disabled_without_config(dynatrace, azure, load_poller):
    poll = load_poller(dynatrace.url, None)
    poll(None)
    assert azure.arm_calls == 0 and azure.batch_calls == [] and dynatrace.requests == []


def test_custom_delay(dynatrace, azure, load_poller):
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"}]
    azure.batch_response = respond_with_hits
    poll = load_poller(dynatrace.url, CONFIG, delay=5)
    before = dt.datetime.now(dt.timezone.utc)
    poll(None)
    start = dt.datetime.strptime(azure.batch_calls[0]["params"]["starttime"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.timezone.utc)
    assert before.replace(second=0, microsecond=0) - start in (dt.timedelta(minutes=5), dt.timedelta(minutes=6))


def test_dynatrace_errors_make_the_timer_run_fail(dynatrace, azure, load_poller):
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"}]
    azure.batch_response = respond_with_hits
    dynatrace.status = 503
    poll = load_poller(dynatrace.url, CONFIG)
    with pytest.raises(requests.HTTPError):
        poll(None)


def test_timer_trigger_is_registered_every_minute(load_poller, dynatrace):
    load_poller(dynatrace.url, CONFIG)
    import function_app
    triggers = [b for b in function_app.poll._function._bindings if b.type == "timerTrigger"]
    assert triggers and triggers[0].schedule == "0 * * * * *"
