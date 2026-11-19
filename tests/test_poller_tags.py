"""Tag-based discovery for the poller: tag a resource, its per-minute metrics are found and forwarded automatically."""
import datetime as dt
import re

import pytest

from helpers import COUNTER, parse
from test_poller import (KV, KV_TYPE, POINT, SUB, answer_with_every_requested_metric, batch_value, definition, row)

STORAGE = f"/subscriptions/{SUB}/resourceGroups/rg-2/providers/Microsoft.Storage/storageAccounts"


@pytest.fixture
def tagged(azure):
    azure.graph_rows = [row(f"{KV}/vault-a"), row(f"{KV}/vault-b"),
                        row(f"{STORAGE}/stor1", "microsoft.storage/storageaccounts", location="northeurope")]
    azure.definitions = {
        KV_TYPE: [definition("ServiceApiHit"), definition("Availability"),
                  definition("NeedsDimension", dim_required=True),          # no dimensions known: cannot be asked for
                  definition("HourlyOnly", grains=("PT1H",))],
        "microsoft.storage/storageaccounts": [definition("Transactions"), definition("ServiceApiHit")],
    }
    azure.batch_response = answer_with_every_requested_metric
    return azure


def test_tagged_resources_are_found_and_all_usable_metrics_are_polled(dynatrace, tagged, load_poller):
    load_poller(dynatrace.url)(None)

    # one Resource Graph query that selects by that tag, across all subscriptions
    (graph,) = tagged.graph_calls
    assert "tags['dynatrace-nearrealtime']" in graph["query"] and "!in" in graph["query"]
    assert "subscriptions" not in graph

    # per resource type: every per-minute metric that can be requested
    calls = {c["params"]["metricnamespace"]: c for c in tagged.batch_calls}
    assert set(calls) == {KV_TYPE, "microsoft.storage/storageaccounts"}
    assert calls[KV_TYPE]["params"]["metricnames"] == "ServiceApiHit,Availability"
    assert calls["microsoft.storage/storageaccounts"]["params"]["metricnames"] == "Transactions,ServiceApiHit"
    assert calls[KV_TYPE]["region"] == "westeurope"
    assert calls["microsoft.storage/storageaccounts"]["region"] == "northeurope"

    keys = sorted(parse(l)["key"] for l in dynatrace.lines)
    assert keys == sorted(
        [f"cloud.azure.microsoft_keyvault.vaults.{m}.nearrealtime" for m in ("ServiceApiHit", "Availability")] * 2
        + [f"cloud.azure.microsoft_storage.storageaccounts.{m}.nearrealtime" for m in ("Transactions", "ServiceApiHit")])
    storage_line = parse([l for l in dynatrace.lines if "storageaccounts" in l][0])
    assert storage_line["dims"]["azure.location"] == "northeurope"
    assert storage_line["dims"]["azure.resource.name"] == "stor1"


def test_tag_value_can_restrict_the_metrics(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a", value="ServiceApiHit; ServiceApiLatency"), row(f"{KV}/vault-b")]
    load_poller(dynatrace.url)(None)

    by_ids = {tuple(c["resourceids"]): c["params"]["metricnames"] for c in tagged.batch_calls}
    assert by_ids == {(f"{KV}/vault-a".lower(),): "ServiceApiHit,ServiceApiLatency",
                      (f"{KV}/vault-b".lower(),): "ServiceApiHit,Availability"}


def test_metrics_are_requested_in_chunks_of_20(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a")]
    tagged.definitions = {KV_TYPE: [definition(f"M{i:02d}") for i in range(45)]}
    load_poller(dynatrace.url)(None)

    assert [len(c["params"]["metricnames"].split(",")) for c in tagged.batch_calls] == [20, 20, 5]
    assert len(dynatrace.lines) == 45


def test_nothing_tagged_means_no_metric_calls(dynatrace, tagged, load_poller):
    tagged.graph_rows = []
    load_poller(dynatrace.url)(None)
    assert tagged.batch_calls == [] and dynatrace.requests == []


def test_unsafe_tag_names_are_rejected(dynatrace, tagged, load_poller):
    poll = load_poller(dynatrace.url, tag="x'] | take 1 //")
    with pytest.raises(ValueError):
        poll(None)
    assert tagged.graph_calls == []


def test_metrics_failing_for_a_resource_are_skipped(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a")]
    tagged.definitions = {KV_TYPE: [definition("ServiceApiHit"), definition("Availability")]}

    def response(call):
        v = batch_value(call["resourceids"][0], "westeurope", {"ServiceApiHit": [({}, POINT)], "Availability": [({}, POINT)]})
        v["value"][1]["errorCode"] = "BadRequest"
        return {"values": [v]}
    tagged.batch_response = response
    load_poller(dynatrace.url)(None)
    assert [parse(l)["key"].split(".")[-2] for l in dynatrace.lines] == ["ServiceApiHit"]


# --- counters and gauges -----------------------------------------------------------------------------------------------

def test_total_metrics_are_sent_as_counters_and_the_rest_as_gauges(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a")]
    tagged.definitions = {KV_TYPE: [definition("ServiceApiHit", agg="Total"),
                                    definition("Availability", agg="Average"),
                                    definition("ServiceApiLatency", agg="Average")]}
    load_poller(dynatrace.url)(None)

    by_metric = {l.split(",")[0].split(".")[-2]: l for l in dynatrace.lines}
    hit = COUNTER.match(by_metric["ServiceApiHit"])
    assert hit, by_metric["ServiceApiHit"]
    assert hit["delta"] == "6.0"                                             # the minute's total, not min/max/count
    assert "gauge" not in by_metric["ServiceApiHit"]
    assert parse(by_metric["Availability"])["type"] == "gauge"
    assert parse(by_metric["ServiceApiLatency"])["sum"] == "6.0"
    assert 'azure.resource.name="vault-a"' in by_metric["ServiceApiHit"]


def test_count_aggregated_metrics_are_counters_sending_the_sample_count(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a")]
    tagged.definitions = {KV_TYPE: [definition("ServiceApiHit", agg="Count")]}
    load_poller(dynatrace.url)(None)
    (line,) = dynatrace.lines
    assert " count,delta=" in line and "gauge" not in line
    assert COUNTER.match(line)["delta"] == str(float(POINT["count"]))


def test_metrics_without_definition_info_stay_gauges(dynatrace, tagged, load_poller):
    tagged.graph_rows = [row(f"{KV}/vault-a", value="ServiceApiHit")]
    tagged.definitions = {}                                                  # nothing known about the type
    load_poller(dynatrace.url)(None)
    assert [parse(l)["type"] for l in dynatrace.lines] == ["gauge"]


def test_fractional_sample_counts_are_sent_as_one_sample_with_the_average():
    import function_app
    base = {"resourceId": f"{KV}/vault-a", "metricName": "SaturationShoebox", "time": "2026-11-17T09:00:00Z",
            "minimum": 0.1, "maximum": 0.3, "average": 0.2}
    line = function_app.to_line({**base, "count": 0.0001, "total": 0.00002})
    assert "count=1 " in line and "sum=0.2," in line
    whole = function_app.to_line({**base, "count": 4, "total": 0.8})
    assert "sum=0.8," in whole and "count=4 " in whole


def test_timestamp_is_the_start_of_azures_one_minute_slot_for_counters_and_gauges():
    """Azure labels a 1-minute data point with the start of the minute; Dynatrace gets exactly that instant in ms (UTC)."""
    import function_app
    expected = int(dt.datetime(2026, 11, 17, 9, 15, tzinfo=dt.timezone.utc).timestamp() * 1000)
    base = {"resourceId": f"{KV}/vault-a", "time": "2026-11-17T09:15:00Z", "count": 3, "total": 3.0,
            "minimum": 1.0, "maximum": 1.0, "average": 1.0}
    for kind in ("count", "samples", "gauge"):
        line = function_app.to_line({**base, "metricName": "M", "kind": kind})
        assert line.endswith(f" {expected}"), (kind, line)


# --- metric key suffix -------------------------------------------------------------------------------------------------

def test_metric_key_suffix_is_configurable(dynatrace, tagged, load_poller, monkeypatch):
    tagged.graph_rows = [row(f"{KV}/vault-a")]
    tagged.definitions = {KV_TYPE: [definition("ServiceApiLatency", agg="Average")]}
    monkeypatch.setenv("METRIC_KEY_SUFFIX", ".demo")
    load_poller(dynatrace.url)(None)
    assert parse(dynatrace.lines[0])["key"] == "cloud.azure.microsoft_keyvault.vaults.ServiceApiLatency.demo"


def test_default_metric_key_suffix_is_nearrealtime(dynatrace, tagged, load_poller, monkeypatch):
    tagged.graph_rows = [row(f"{KV}/vault-a")]
    tagged.definitions = {KV_TYPE: [definition("ServiceApiLatency", agg="Average")]}
    monkeypatch.delenv("METRIC_KEY_SUFFIX", raising=False)
    load_poller(dynatrace.url)(None)
    assert parse(dynatrace.lines[0])["key"].endswith(".ServiceApiLatency.nearrealtime")
