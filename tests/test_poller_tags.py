"""Tag-based discovery for the poller: tag a resource, its per-minute metrics are found and forwarded automatically."""
import pytest

from test_e2e import parse
from test_poller import CONFIG, KV, POINT, SUB, batch_value

STORAGE = f"/subscriptions/{SUB}/resourceGroups/rg-2/providers/Microsoft.Storage/storageAccounts"


def definition(name, grains=("PT1M",), dim_required=False):
    return {"name": {"value": name, "localizedValue": name}, "isDimensionRequired": dim_required,
            "metricAvailabilities": [{"timeGrain": g, "retention": "P93D"} for g in grains]}


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
