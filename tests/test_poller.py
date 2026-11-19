"""End-to-end tests for the metrics poller: timer → Azure Monitor (mock Resource Graph / ARM / Metrics Batch API) → Dynatrace (mock).

The response bodies follow the Metrics Batch API reference (api-version 2023-10-01).
"""
import datetime as dt

import pytest
import requests

from helpers import parse

SUB = "a0a0a0a0-bbbb-cccc-dddd-e1e1e1e1e1e1"
KV = f"/subscriptions/{SUB}/resourceGroups/rg-1/providers/Microsoft.KeyVault/vaults"
KV_TYPE = "microsoft.keyvault/vaults"


def definition(name, grains=("PT1M",), dim_required=False, agg=None, dimensions=()):
    d = {"name": {"value": name, "localizedValue": name}, "isDimensionRequired": dim_required,
         "dimensions": [{"value": x, "localizedValue": x} for x in dimensions],
         "metricAvailabilities": [{"timeGrain": g, "retention": "P93D"} for g in grains]}
    if agg:
        d["primaryAggregationType"] = agg
    return d


def row(rid, rtype=KV_TYPE, value="true", location="westeurope", sub=SUB):
    """A Resource Graph result row: a resource carrying the tag."""
    return {"id": rid, "type": rtype, "location": location, "subscriptionId": sub, "v": value}


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


def answer_with_every_requested_metric(call):
    names = call["params"]["metricnames"].split(",")
    return {"values": [batch_value(rid, call["region"], {n: [({}, POINT)] for n in names}) for rid in call["resourceids"]]}


@pytest.fixture
def vaults(azure):
    """Two tagged vaults whose type has one plain metric and one with dimensions."""
    azure.graph_rows = [row(f"{KV}/vault-a"), row(f"{KV}/vault-b")]
    azure.definitions = {KV_TYPE: [definition("ServiceApiLatency", agg="Average"),
                                   definition("ServiceApiHit", agg="Count", dimensions=("ActivityName", "StatusCode"))]}
    azure.batch_response = answer_with_every_requested_metric
    return azure


def test_poll_forwards_metrics_to_dynatrace(dynatrace, vaults, load_poller):
    poll = load_poller(dynatrace.url)
    before = dt.datetime.now(dt.timezone.utc)
    poll(None)

    # the plain metric is requested for both vaults in one batch call per region, with the documented parameters
    call = next(c for c in vaults.batch_calls if "filter" not in c["params"])
    assert call["region"] == "westeurope" and call["subscription"] == SUB
    assert call["resourceids"] == [f"{KV}/vault-a".lower(), f"{KV}/vault-b".lower()]
    assert call["auth"] == "Bearer fake-token-for-metrics.monitor.azure.com"
    q = call["params"]
    assert q["api-version"] == "2023-10-01"
    assert q["metricnamespace"] == KV_TYPE
    assert q["metricnames"] == "ServiceApiLatency"
    assert q["interval"] == "PT1M" and q["aggregation"] == "average,count,maximum,minimum,total"

    # the requested slot is one minute long and two minutes old (to the minute)
    start = dt.datetime.strptime(q["starttime"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.strptime(q["endtime"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.timezone.utc)
    assert end - start == dt.timedelta(minutes=1) and start.second == 0
    age = before.replace(second=0, microsecond=0) - start
    assert age in (dt.timedelta(minutes=2), dt.timedelta(minutes=3))      # 3 only if a minute boundary passed

    # semantic attributes and key on the lines
    p = parse([l for l in dynatrace.lines if "ServiceApiLatency" in l and "vault-a" in l][0])
    assert p["key"] == "cloud.azure.microsoft_keyvault.vaults.ServiceApiLatency.nearrealtime"
    assert (p["min"], p["max"], p["sum"], p["count"]) == ("2.0", "4.0", "6.0", "2")
    assert p["dims"]["azure.resource.id"] == f"{KV}/vault-a".lower()
    assert p["dims"]["azure.resource.name"] == "vault-a"
    assert p["dims"]["azure.location"] == "westeurope"
    assert p["dims"]["azure.subscription"] == SUB
    assert p["dims"]["azure.resource.type"] == KV_TYPE


def test_all_dimensions_are_split_by_default(dynatrace, azure, load_poller):
    azure.graph_rows = [row(f"{KV}/vault-a")]
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit", agg="Count", dimensions=("ActivityName", "StatusCode")),
                                   definition("ServiceApiLatency", agg="Average")]}
    azure.batch_response = lambda call: {"values": [batch_value(
        call["resourceids"][0], "westeurope",
        {n: [({"ActivityName": "vaultget", "StatusCode": "200"} if "filter" in call["params"] else {}, POINT),
             *([({"ActivityName": "keycreate", "StatusCode": "200"}, POINT)] if "filter" in call["params"] else [])]
         for n in call["params"]["metricnames"].split(",")})]}
    load_poller(dynatrace.url)(None)

    by_filter = {c["params"].get("filter"): c["params"] for c in azure.batch_calls}
    assert set(by_filter) == {None, "ActivityName eq '*' and StatusCode eq '*'"}        # every dimension, nothing configured
    assert by_filter["ActivityName eq '*' and StatusCode eq '*'"]["metricnames"] == "ServiceApiHit"
    assert by_filter["ActivityName eq '*' and StatusCode eq '*'"]["top"] == "100"
    hits = [parse(l) for l in dynatrace.lines if "ServiceApiHit" in l]
    assert sorted(h["dims"]["activityname"] for h in hits) == ["keycreate", "vaultget"]
    assert all(h["dims"]["statuscode"] == "200" for h in hits)


def test_dimension_splitting_can_be_switched_off(dynatrace, azure, load_poller, monkeypatch):
    azure.graph_rows = [row(f"{KV}/vault-a")]
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit", agg="Count", dimensions=("ActivityName",)),
                                   definition("NeedsDimension", dim_required=True, dimensions=("Kind",))]}
    azure.batch_response = answer_with_every_requested_metric
    monkeypatch.setenv("AZMON_DIMENSIONS", "none")
    load_poller(dynatrace.url)(None)
    (call,) = azure.batch_calls
    assert "filter" not in call["params"] and call["params"]["metricnames"] == "ServiceApiHit"     # required-dimension metric skipped


def test_metrics_requiring_dimensions_are_collected_when_splitting(dynatrace, azure, load_poller):
    azure.graph_rows = [row(f"{KV}/vault-a")]
    azure.definitions = {KV_TYPE: [definition("NeedsDimension", dim_required=True, dimensions=("Kind",))]}
    azure.batch_response = answer_with_every_requested_metric
    load_poller(dynatrace.url)(None)
    (call,) = azure.batch_calls
    assert call["params"]["filter"] == "Kind eq '*'" and call["params"]["metricnames"] == "NeedsDimension"


def test_top_limit_is_configurable(dynatrace, azure, load_poller, monkeypatch):
    azure.graph_rows = [row(f"{KV}/vault-a")]
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit", dimensions=("ActivityName",))]}
    azure.batch_response = answer_with_every_requested_metric
    monkeypatch.setenv("AZMON_TOP", "25")
    load_poller(dynatrace.url)(None)
    assert azure.batch_calls[0]["params"]["top"] == "25"


def test_resources_are_grouped_by_region_and_batched_by_50(dynatrace, azure, load_poller):
    azure.graph_rows = ([row(f"{KV}/we-{i}") for i in range(120)]
                        + [row(f"{KV}/ne-{i}", location="North Europe") for i in range(3)])
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit", agg="Count")]}
    azure.batch_response = answer_with_every_requested_metric
    load_poller(dynatrace.url)(None)

    sizes = sorted((c["region"], len(c["resourceids"])) for c in azure.batch_calls)
    assert sizes == [("northeurope", 3), ("westeurope", 20), ("westeurope", 50), ("westeurope", 50)]
    assert len(dynatrace.lines) == 123
    ne = [parse(l) for l in dynatrace.lines if "/ne-0" in l][0]
    assert ne["dims"]["azure.location"] == "northeurope"            # per-resource region


def test_failed_metrics_are_skipped_others_forwarded(dynatrace, azure, load_poller):
    def response(call):
        v = batch_value(call["resourceids"][0], "westeurope",
                        {"ServiceApiHit": [({}, POINT)], "ServiceApiLatency": [({}, POINT)]})
        v["value"][1]["errorCode"] = "InvalidMetric"
        v["value"][1]["errorMessage"] = "metric not supported"
        return {"values": [v]}
    azure.graph_rows = [row(f"{KV}/vault-a")]
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit"), definition("ServiceApiLatency")]}
    azure.batch_response = response
    load_poller(dynatrace.url)(None)
    assert [parse(l)["key"].split(".")[-2] for l in dynatrace.lines] == ["ServiceApiHit"]


def test_points_without_data_are_ignored(dynatrace, azure, load_poller):
    azure.graph_rows = [row(f"{KV}/vault-a")]
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit")]}
    azure.batch_response = lambda call: {"values": [batch_value(call["resourceids"][0], "westeurope",
                                                                 {"ServiceApiHit": [({}, {})]})]}      # only timeStamp
    load_poller(dynatrace.url)(None)
    assert dynatrace.requests == []


def test_discovery_and_definitions_are_cached_between_polls(dynatrace, vaults, load_poller):
    poll = load_poller(dynatrace.url)
    poll(None)
    poll(None)
    assert len(vaults.graph_calls) == 1 and len(vaults.definition_calls) == 1
    assert len(vaults.batch_calls) == 4                               # 2 dimension groups x 2 polls


def test_polling_is_disabled_without_a_tag(dynatrace, azure, load_poller):
    load_poller(dynatrace.url, tag=None)(None)
    assert azure.graph_calls == [] and azure.batch_calls == [] and dynatrace.requests == []


def test_custom_delay(dynatrace, vaults, load_poller):
    poll = load_poller(dynatrace.url, delay=5)
    before = dt.datetime.now(dt.timezone.utc)
    poll(None)
    start = dt.datetime.strptime(vaults.batch_calls[0]["params"]["starttime"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.timezone.utc)
    assert before.replace(second=0, microsecond=0) - start in (dt.timedelta(minutes=5), dt.timedelta(minutes=6))


def test_dynatrace_errors_make_the_timer_run_fail(dynatrace, vaults, load_poller):
    dynatrace.status = 503
    with pytest.raises(requests.HTTPError):
        load_poller(dynatrace.url)(None)


def test_timer_trigger_is_registered_every_minute(load_poller, dynatrace):
    load_poller(dynatrace.url)
    import function_app
    triggers = [b for b in function_app.poll._function._bindings if b.type == "timerTrigger"]
    assert triggers and triggers[0].schedule == "0 * * * * *"


# --- several subscriptions at once ----------------------------------------------------------------------------------

SUB2 = "b1b1b1b1-bbbb-cccc-dddd-e2e2e2e2e2e2"
KV2 = f"/subscriptions/{SUB2}/resourceGroups/rg-9/providers/Microsoft.KeyVault/vaults"
STORAGE = f"/subscriptions/{SUB}/resourceGroups/rg-2/providers/Microsoft.Storage/storageAccounts"


def test_all_resource_types_resource_groups_and_subscriptions_are_polled_by_tag(dynatrace, azure, load_poller):
    azure.graph_rows = [row(f"{KV}/vault-a"), row(f"{KV2}/vault-z", sub=SUB2),
                        row(f"{STORAGE}/stor1", "microsoft.storage/storageaccounts", location="northeurope")]
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit")], "microsoft.storage/storageaccounts": [definition("Transactions")]}
    azure.batch_response = answer_with_every_requested_metric
    load_poller(dynatrace.url)(None)

    assert {(c["subscription"], c["params"]["metricnamespace"]) for c in azure.batch_calls} == {
        (SUB, KV_TYPE), (SUB2, KV_TYPE), (SUB, "microsoft.storage/storageaccounts")}
    assert len(dynatrace.lines) == 3
    assert "subscriptions" not in azure.graph_calls[0]               # one query over every subscription the identity reads


def test_several_subscriptions_can_be_listed(dynatrace, vaults, load_poller):
    load_poller(dynatrace.url, subscriptions=[SUB, SUB2])(None)
    assert vaults.graph_calls[0]["subscriptions"] == [SUB, SUB2]


def test_a_failing_subscription_does_not_stop_the_others(dynatrace, azure, load_poller):
    azure.graph_rows = [row(f"{KV}/vault-a"), row(f"{KV2}/vault-z", sub=SUB2)]
    azure.definitions = {KV_TYPE: [definition("ServiceApiHit")]}

    def response(call):
        if call["subscription"] == SUB2:
            return 403, {"error": {"code": "AuthorizationFailed", "message": "no Monitoring Reader"}}
        return answer_with_every_requested_metric(call)
    azure.batch_response = response
    load_poller(dynatrace.url)(None)
    assert [parse(l)["dims"]["azure.subscription"] for l in dynatrace.lines] == [SUB]


def test_the_run_fails_when_every_subscription_fails(dynatrace, vaults, load_poller):
    vaults.batch_response = lambda call: (403, {"error": {"code": "AuthorizationFailed"}})
    with pytest.raises(requests.HTTPError):
        load_poller(dynatrace.url)(None)
