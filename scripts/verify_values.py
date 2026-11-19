"""Compare what the poller put into Dynatrace with what Azure Monitor itself reports for the test Key Vault.

Covers both metric kinds against real data:
  counter  ServiceApiHit      (Azure primary aggregation Count)   -> Dynatrace counter: sum per minute == Azure total
  gauge    ServiceApiLatency  (Azure primary aggregation Average) -> Dynatrace gauge:   min / max / avg == Azure min / max / average

  python scripts/verify_values.py <subscription> <resource group> <vault name>
Exit code: 0 = every comparable minute matches (at least 3), 2 = not enough data yet, 1 = mismatch or wrong metric kind,
3 = the comparison itself failed (query error).
Reads DT_ENVIRONMENT / DT_TOKEN from the environment; Azure auth is your `az login`.
"""
import datetime as dt
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

COUNTER_METRIC, GAUGE_METRIC = "ServiceApiHit", "ServiceApiLatency"
PREFIX = "cloud.azure.microsoft_keyvault.vaults."
SUFFIX = ".metricstream"
MIN_SLOTS = 3
SETTLE = dt.timedelta(minutes=5)       # Azure may still revise the newest minutes; only compare older ones
REL_TOL = 1e-3


def close(a, b, rel=REL_TOL):
    return a is not None and b is not None and abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def parse_ts(s):
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def dynatrace_by_slot(record):
    """DQL timeseries record (arrays c, mn, mx, a over 1m slots) -> {slot: {...}} for the slots that have data."""
    start = parse_ts(record["timeframe"]["start"])
    out = {}
    for i in range(len(record.get("c") or [])):
        vals = {k: (record.get(k) or [None] * (i + 1))[i] for k in ("c", "mn", "mx", "a")}
        if any(v is not None for v in vals.values()):
            out[start + dt.timedelta(minutes=i)] = vals
    return out


def azure_by_slot(records):
    """Poller-style records -> {slot: {metric name: record}}"""
    out = {}
    for r in records:
        out.setdefault(parse_ts(r["time"]), {})[r["metricName"]] = r
    return out


def compare(dt_slots, az_slots, now):
    """Returns (rows, mismatches); rows are (slot, description) for minutes old enough and present on both sides."""
    rows, bad = [], []
    for slot in sorted(set(dt_slots) & set(az_slots)):
        if slot + SETTLE > now:
            continue
        d, a = dt_slots[slot], az_slots[slot]
        hit, lat = a.get(COUNTER_METRIC), a.get(GAUGE_METRIC)
        if not hit or not lat or (hit.get("count") or 0) <= 0 or (lat.get("count") or 0) <= 0:
            continue
        problems = []
        if not close(d["c"], hit["count"], 1e-9):
            problems.append(f"counter sum {d['c']} != Azure count {hit['count']}")
        for key, azkey in (("mn", "minimum"), ("mx", "maximum"), ("a", "average")):
            if not close(d[key], lat[azkey]):
                problems.append(f"gauge {key} {d[key]} != Azure {azkey} {lat[azkey]}")
        rows.append((slot, f"{COUNTER_METRIC} {d['c']:g} (Azure {hit['count']:g}) | {GAUGE_METRIC} avg {d['a']:.4g} (Azure {lat['average']:.4g})"
                           + ("   MISMATCH: " + "; ".join(problems) if problems else "   ok")))
        bad += [(slot, p) for p in problems]
    return rows, bad


def main():
    sub, rg, vault = sys.argv[1:4]
    import azmon, dql
    rid = f"/subscriptions/{sub}/resourcegroups/{rg}/providers/microsoft.keyvault/vaults/{vault}".lower()
    now = dt.datetime.now(dt.timezone.utc)

    kinds = azmon.metric_kinds(f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.KeyVault/vaults/{vault}",
                               "Microsoft.KeyVault/vaults")
    expected = {COUNTER_METRIC.lower(): "samples", GAUGE_METRIC.lower(): "gauge"}
    wrong = {k: kinds.get(k) for k, v in expected.items() if kinds.get(k) != v}
    if wrong:
        print(f"unexpected Azure metric kinds (wanted {expected}): {wrong}")
        return 1

    cm, gm = PREFIX + COUNTER_METRIC + SUFFIX, PREFIX + GAUGE_METRIC + SUFFIX
    records = dql.query(
        f"timeseries c=sum(`{cm}`), mn=min(`{gm}`), mx=max(`{gm}`), a=avg(`{gm}`), by:{{azure.resource.id}}, "
        f"filter: azure.resource.id == \"{rid}\", interval: 1m, from: now()-45m")
    dt_slots = dynatrace_by_slot(records[0]) if records else {}

    entry = {"subscription": sub, "resourceGroup": rg, "resourceType": "Microsoft.KeyVault/vaults",
             "metrics": [COUNTER_METRIC, GAUGE_METRIC]}
    az_slots = azure_by_slot(azmon.fetch_records(entry, now - dt.timedelta(minutes=44), now))

    rows, bad = compare(dt_slots, az_slots, now)
    for slot, text in rows:
        print(f"{slot:%H:%M} UTC  {text}")
    print(f"{len(dt_slots)} minutes in Dynatrace, {len(az_slots)} in Azure, {len(rows)} comparable, {len(bad)} mismatches")
    if bad:
        return 1
    return 0 if len(rows) >= MIN_SLOTS else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:          # cannot compare right now (Azure / Dynatrace query failed): neither a pass nor a mismatch
        print(f"could not run the comparison: {str(e)[:300]}")
        sys.exit(3)
