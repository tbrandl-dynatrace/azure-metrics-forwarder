"""Tag-based discovery for the poller: tag a resource, its per-minute metrics are found and forwarded automatically."""
import re

import pytest

from test_e2e import parse
from test_poller import CONFIG, KV, POINT, SUB, batch_value

STORAGE = f"/subscriptions/{SUB}/resourceGroups/rg-2/providers/Microsoft.Storage/storageAccounts"


def definition(name, grains=("PT1M",), dim_required=False, agg=None):
    d = {"name": {"value": name, "localizedValue": name}, "isDimensionRequired": dim_required,
         "metricAvailabilities": [{"timeGrain": g, "retention": "P93D"} for g in grains]}
    if agg:
        d["primaryAggregationType"] = agg
    return d


def row(rid, rtype, value="true", location="westeurope", sub=SUB):
    return {"id": rid, "type": rtype, "location": location, "subscriptionId": sub, "v": value}


def answer_with_every_requested_metric(call):
    names = call["params"]["metricnames"].split(",")
    return {"values": [batch_value(rid, call["region"], {n: [({}, POINT)] for n in names}) for rid in call["resourceids"]]}


@pytest.fixture
def tagged(azure):
    azure.graph_rows = [row(f"{KV}/vault-a", "microsoft.keyvault/vaults"),
                        row(f"{KV}/vault-b", "microsoft.keyvault/vaults"),
                        row(f"{STORAGE}/stor1", "microsoft.storage/storageaccounts", location="northeurope")]
    azure.definitions = {
        "microsoft.keyvault/vaults": [definition("ServiceApiHit"), definition("Availability"),
                                      definition("NeedsDimension", dim_required=True),
                                      definition("HourlyOnly", grains=("PT1H",))],
        "microsoft.storage/storageaccounts": [definition("Transactions"), definition("ServiceApiHit")],
    }
    azure.batch_response = answer_with_every_requested_metric
    return azure


def test_tagged_resources_are_found_and_all_usable_metrics_are_polled(dynatrace, tagged, load_poller):
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)

    # one Resource Graph query that selects by that tag
    (graph,) = tagged.graph_calls
    assert "tags['dynatrace-metrics']" in graph["query"] and "!in" in graph["query"]
    assert "subscriptions" not in graph                                    # all subscriptions the identity can see

    # per resource type: only metrics that are per-minute and need no dimension filter
    calls = {c["params"]["metricnamespace"]: c for c in tagged.batch_calls}
    assert set(calls) == {"microsoft.keyvault/vaults", "microsoft.storage/storageaccounts"}
    assert calls["microsoft.keyvault/vaults"]["params"]["metricnames"] == "ServiceApiHit,Availability"
    assert calls["microsoft.storage/storageaccounts"]["params"]["metricnames"] == "Transactions,ServiceApiHit"
    assert calls["microsoft.keyvault/vaults"]["region"] == "westeurope"
    assert calls["microsoft.storage/storageaccounts"]["region"] == "northeurope"
    assert "filter" not in calls["microsoft.keyvault/vaults"]["params"]

    keys = sorted(parse(l)["key"] for l in dynatrace.lines)
    assert keys == sorted(
        [f"cloud.azure.microsoft_keyvault.vaults.{m}.metricstream" for m in ("ServiceApiHit", "Availability")] * 2
        + [f"cloud.azure.microsoft_storage.storageaccounts.{m}.metricstream" for m in ("Transactions", "ServiceApiHit")])
    storage_line = parse([l for l in dynatrace.lines if "storageaccounts" in l][0])
    assert storage_line["dims"]["azure.location"] == "northeurope"
    assert storage_line["dims"]["azure.resource.name"] == "stor1"


def test_tag_value_can_restrict_the_metrics(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a", "microsoft.keyvault/vaults", value="ServiceApiHit; ServiceApiLatency"),
                         row(f"{KV}/vault-b", "microsoft.keyvault/vaults", value="true")]
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)

    by_ids = {tuple(c["resourceids"]): c["params"]["metricnames"] for c in tagged.batch_calls}
    assert by_ids == {(f"{KV}/vault-a".lower(),): "ServiceApiHit,ServiceApiLatency",
                      (f"{KV}/vault-b".lower(),): "ServiceApiHit,Availability"}


def test_metrics_are_requested_in_chunks_of_20(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a", "microsoft.keyvault/vaults")]
    tagged.definitions = {"microsoft.keyvault/vaults": [definition(f"M{i:02d}") for i in range(45)]}
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)

    sizes = [len(c["params"]["metricnames"].split(",")) for c in tagged.batch_calls]
    assert sizes == [20, 20, 5]
    assert len(dynatrace.lines) == 45


def test_discovery_and_definitions_are_cached_between_polls(dynatrace, tagged, load_poller):
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)
    poll(None)
    assert len(tagged.graph_calls) == 1
    assert len(tagged.definition_calls) == 2            # once per resource type, not per poll
    assert len(tagged.batch_calls) == 4                 # 2 types x 2 polls


def test_subscriptions_can_be_limited(dynatrace, tagged, load_poller):
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics", subscriptions=[SUB, "other-sub"])
    poll(None)
    assert tagged.graph_calls[0]["subscriptions"] == [SUB, "other-sub"]


def test_tag_and_explicit_config_work_together(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{STORAGE}/stor1", "microsoft.storage/storageaccounts")]
    tagged.resources = [{"id": f"{KV}/vault-x", "location": "westeurope"}]
    poll = load_poller(dynatrace.url, CONFIG, tag="dynatrace-metrics")
    poll(None)
    namespaces = sorted(c["params"]["metricnamespace"] for c in tagged.batch_calls)
    assert namespaces == ["microsoft.keyvault/vaults", "microsoft.storage/storageaccounts"]


def test_nothing_tagged_means_no_metric_calls(dynatrace, tagged, load_poller):
    tagged.graph_rows = []
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)
    assert tagged.batch_calls == [] and dynatrace.requests == []


def test_unsafe_tag_names_are_rejected(dynatrace, tagged, load_poller):
    poll = load_poller(dynatrace.url, None, tag="x'] | take 1 //")
    with pytest.raises(ValueError):
        poll(None)
    assert tagged.graph_calls == []


def test_metrics_failing_for_a_resource_are_skipped(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a", "microsoft.keyvault/vaults")]

    def response(call):
        v = batch_value(call["resourceids"][0], "westeurope", {"ServiceApiHit": [({}, POINT)], "Availability": [({}, POINT)]})
        v["value"][1]["errorCode"] = "BadRequest"
        return {"values": [v]}
    tagged.batch_response = response
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)
    assert [parse(l)["key"].split(".")[-2] for l in dynatrace.lines] == ["ServiceApiHit"]


COUNTER = re.compile(r'^(?P<key>[^, ]+),(?P<dims>.*) count,delta=(?P<delta>[^ ]+) (?P<ts>\d+)$')


def test_total_metrics_are_sent_as_counters_and_the_rest_as_gauges(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a", "microsoft.keyvault/vaults")]
    tagged.definitions = {"microsoft.keyvault/vaults": [definition("ServiceApiHit", agg="Total"),
                                                         definition("Availability", agg="Average"),
                                                         definition("ServiceApiLatency", agg="Average")]}
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)

    by_metric = {l.split(",")[0].split(".")[-2]: l for l in dynatrace.lines}
    hit = COUNTER.match(by_metric["ServiceApiHit"])
    assert hit, by_metric["ServiceApiHit"]
    assert hit["delta"] == "6.0"                                             # the minute's total, not min/max/count
    assert "gauge" not in by_metric["ServiceApiHit"]
    assert parse(by_metric["Availability"])["type"] == "gauge"               # gauge summary as before
    assert parse(by_metric["ServiceApiLatency"])["sum"] == "6.0"
    assert 'azure.resource.name="vault-a"' in by_metric["ServiceApiHit"]


def test_explicit_config_classifies_metrics_too(dynatrace, azure, load_poller):
    azure.resources = [{"id": f"{KV}/vault-a", "location": "westeurope"}]
    azure.definitions = {"microsoft.keyvault/vaults": [definition("ServiceApiHit", agg="Total"),
                                                       definition("ServiceApiLatency", agg="Average")]}
    azure.batch_response = answer_with_every_requested_metric
    poll = load_poller(dynatrace.url, CONFIG)
    poll(None)
    kinds = {l.split(",")[0].split(".")[-2]: ("count" if " count,delta=" in l else "gauge") for l in dynatrace.lines}
    assert kinds == {"ServiceApiHit": "count", "ServiceApiLatency": "gauge"}


def test_metrics_without_definition_info_stay_gauges(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a", "microsoft.keyvault/vaults", value="ServiceApiHit")]
    tagged.definitions = {}                                                  # nothing known about the type
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    poll(None)
    assert [parse(l)["type"] for l in dynatrace.lines] == ["gauge"]


def test_fractional_sample_counts_are_sent_as_one_sample_with_the_average():
    import function_app
    base = {"resourceId": f"{KV}/vault-a", "metricName": "SaturationShoebox", "time": "2026-11-17T09:00:00Z",
            "minimum": 0.1, "maximum": 0.3, "average": 0.2}
    line = function_app.to_line({**base, "count": 0.0001, "total": 0.00002})
    assert "count=1 " in line and "sum=0.2," in line
    whole = function_app.to_line({**base, "count": 4, "total": 0.8})
    assert "sum=0.8," in whole and "count=4 " in whole


def test_event_hub_function_is_only_registered_with_a_hub_name(dynatrace, load_poller, monkeypatch):
    import sys, importlib
    monkeypatch.delenv("EH_NAME", raising=False)
    poll = load_poller(dynatrace.url, None, tag="dynatrace-metrics")
    module = sys.modules["function_app"]
    assert not hasattr(module, "run") and callable(poll)                      # poller-only deployment

    monkeypatch.setenv("EH_NAME", "metrics")
    sys.modules.pop("function_app")
    assert hasattr(importlib.import_module("function_app"), "run")
