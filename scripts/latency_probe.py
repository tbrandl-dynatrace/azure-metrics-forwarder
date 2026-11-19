"""Continuously generate Key Vault metrics and measure how long each one-minute data point takes to show up,
first in Azure Monitor and then in Dynatrace.

  generator thread: calls the vault (~2/s) so every minute carries ServiceApiHit data
  observer loop:    every 15 s asks Azure Monitor (Metrics Batch API) and Dynatrace (DQL) which minutes have data
  report:           per minute, latency = first time the point was visible - end of that minute

The Dynatrace number is end to end: Azure's own availability delay plus the pipeline (poller, DCR / diagnostic
setting + Event Hub + Function). "pipeline" is the difference between the two. The probe is pipeline-agnostic: it
only looks at the metric in Dynatrace, so run it while whichever pipeline you want to measure is feeding the vault
(--run-poller runs the poll handler locally once a minute, like the timer trigger would).

  python scripts/latency_probe.py [--minutes 15] [--run-poller] [--csv out.csv]
  python scripts/latency_probe.py --continuous [--run-poller]     # keep running until Ctrl-C (for demos / long soaks)
Reads SUB / RG / KV from .e2e-poller-state.env (written by scripts/e2e-poller.sh up) unless given as options.
Needs DT_ENVIRONMENT / DT_TOKEN (environment or .env.local) and an `az login`.
"""
import argparse, csv, datetime as dt, json, os, statistics, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

SLOT = dt.timedelta(minutes=1)
OBSERVE_EVERY_S = 15
METRIC = "ServiceApiHit"
DT_KEY = "cloud.azure.microsoft_keyvault.vaults.ServiceApiHit" + os.environ.get("METRIC_KEY_SUFFIX", ".nearrealtime")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def dt_slots(record):
    """Minutes (start datetimes) that have a value > 0 in a DQL `timeseries ... interval:1m` record."""
    start = parse_ts(record["timeframe"]["start"])
    interval = record.get("interval")
    step = dt.timedelta(seconds=interval / 1e9) if isinstance(interval, (int, float)) and interval > 1e6 else SLOT
    return {start + i * step for i, v in enumerate(record.get("s") or []) if v}


def azure_slots(records):
    return {parse_ts(r["time"]) for r in records if (r.get("total") or 0) > 0}


class Tracker:
    """When was each minute first visible in Azure Monitor / Dynatrace?"""

    def __init__(self, first_slot, last_slot):
        self.first_slot, self.last_slot = first_slot, last_slot      # fully generated minutes only
        self.azure, self.dynatrace = {}, {}

    def wanted(self):
        t, out = self.first_slot, []
        while t <= self.last_slot:
            out.append(t)
            t += SLOT
        return out

    def observe(self, source, slots, now):
        seen = self.azure if source == "azure" else self.dynatrace
        new = [s for s in sorted(slots) if self.first_slot <= s <= self.last_slot and s not in seen]
        for s in new:
            seen[s] = now
        return new

    def latency(self, source, slot):
        seen = (self.azure if source == "azure" else self.dynatrace).get(slot)
        return None if seen is None else (seen - (slot + SLOT)).total_seconds()

    def done(self):
        return all(s in self.dynatrace for s in self.wanted())

    def rows(self, last_n=None):
        slots = self.wanted()[-last_n:] if last_n else self.wanted()
        return [(s, self.latency("azure", s), self.latency("dynatrace", s)) for s in slots]

    def summary(self, last_n=None, now=None, grace=dt.timedelta(0)):
        """Percentiles over the (last n) measured minutes. With `now`, a minute only counts as missing from
        Dynatrace once it ended more than `grace` ago (for continuous runs, where the newest minutes are still in flight)."""
        rows = self.rows(last_n)
        out = {}
        for name, vals in (("azure", [r[1] for r in rows]), ("dynatrace", [r[2] for r in rows]),
                           ("pipeline", [r[2] - r[1] for r in rows if r[1] is not None and r[2] is not None])):
            vals = [v for v in vals if v is not None]
            out[name] = {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95),
                         "max": max(vals) if vals else None}
        out["missing"] = [r[0] for r in rows if r[0] not in self.dynatrace
                          and (now is None or r[0] + SLOT + grace <= now)]
        return out


def fmt(v):
    return "  -  " if v is None else f"{v:5.0f}s"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=15, help="how long to generate traffic (default 15; 0 = continuous)")
    ap.add_argument("--continuous", action="store_true",
                    help="run until Ctrl-C (same as --minutes 0): live rows as minutes arrive, rolling summary every few minutes")
    ap.add_argument("--window-minutes", type=int, default=60, help="continuous mode: how far back to query (default 60)")
    ap.add_argument("--summary-every", type=int, default=5, help="continuous mode: rolling summary interval in minutes (default 5)")
    ap.add_argument("--rate", type=float, default=2.0, help="vault calls per second (default 2)")
    ap.add_argument("--drain-minutes", type=int, default=10, help="extra time to wait for the last minutes (default 10)")
    ap.add_argument("--run-poller", action="store_true", help="run the poll handler locally once a minute (at :05)")
    ap.add_argument("--csv", help="write the per-minute results to this file")
    ap.add_argument("--sub"); ap.add_argument("--rg"); ap.add_argument("--vault")
    a = ap.parse_args()

    state = {}
    sp = os.path.join(ROOT, ".e2e-poller-state.env")
    if os.path.isfile(sp):
        state = dict(l.strip().split("=", 1) for l in open(sp, encoding="utf-8") if "=" in l)
    sub, rg, vault = a.sub or state.get("SUB"), a.rg or state.get("RG"), a.vault or state.get("KV")
    if not (sub and rg and vault):
        sys.exit("need --sub/--rg/--vault or a .e2e-poller-state.env (run scripts/e2e-poller.sh up)")

    import requests
    import azmon, dql
    rid = f"/subscriptions/{sub}/resourcegroups/{rg}/providers/microsoft.keyvault/vaults/{vault}".lower()
    vault_url = f"https://{vault}.vault.azure.net"
    stop = threading.Event()
    gen_errors, calls = [], [0]

    def vault_call(method, **kw):
        return requests.request(method, f"{vault_url}/secrets/latency-probe", params={"api-version": "7.4"},
                                headers={"Authorization": "Bearer " + azmon.get_token("https://vault.azure.net/.default")},
                                timeout=15, **kw)

    r = vault_call("PUT", json={"value": "probe"})
    if r.status_code >= 300:
        sys.exit(f"cannot write to vault {vault}: {r.status_code} {r.text[:300]}")

    gen_start = utcnow()

    def generate():
        interval = 1.0 / a.rate
        while not stop.is_set():
            t0 = time.time()
            try:
                vault_call("GET")
                calls[0] += 1
            except Exception as e:       # keep going; report at the end
                gen_errors.append(str(e)[:100])
            stop.wait(max(0, interval - (time.time() - t0)))

    threading.Thread(target=generate, daemon=True).start()

    if a.run_poller:
        os.environ.setdefault("AZMON_TAG", "dynatrace-metrics")      # discovery by tag, like a real deployment
        os.environ["AZMON_SUBSCRIPTIONS"] = sub
        import function_app
        poll = function_app.poll._function.get_user_function()

        def poller():
            while not stop.is_set():
                now = utcnow()
                stop.wait(((60 - now.second) % 60) + 5)       # next :05
                if stop.is_set():
                    break
                try:
                    poll(None)
                except Exception as e:
                    print(f"  poll failed: {str(e)[:150]}")
        threading.Thread(target=poller, daemon=True).start()

    continuous = a.continuous or a.minutes == 0
    grace = dt.timedelta(minutes=a.drain_minutes)
    first_slot = (gen_start + dt.timedelta(seconds=59)).replace(second=0, microsecond=0)    # first full minute
    if continuous:
        gen_end = deadline = None
        last_slot = first_slot - SLOT                       # moves forward as minutes complete
        lookback = a.window_minutes
    else:
        gen_end = gen_start + dt.timedelta(minutes=a.minutes)
        last_slot = gen_end.replace(second=0, microsecond=0) - SLOT                           # last full minute
        if last_slot < first_slot:
            sys.exit("--minutes is too short for a full minute of data")
        deadline = gen_end + grace
        lookback = a.minutes + a.drain_minutes + 2
    tracker = Tracker(first_slot, last_slot)
    entry = {"subscription": sub, "resourceGroup": rg, "resourceType": "Microsoft.KeyVault/vaults", "metrics": [METRIC]}
    dql_text = (f"timeseries s = sum(`{DT_KEY}`), by:{{azure.resource.id}}, filter: azure.resource.id == \"{rid}\", "
                f"interval: 1m, from: now()-{lookback + 3}m")

    if continuous:
        print(f"vault {vault}: generating ~{a.rate:g} calls/s until Ctrl-C; measuring every full minute from "
              f"{first_slot:%H:%M} UTC. Latency = first seen - end of the minute.")
    else:
        print(f"vault {vault}: generating ~{a.rate:g} calls/s for {a.minutes} min; measuring minutes "
              f"{first_slot:%H:%M}-{last_slot:%H:%M} UTC. Latency = first seen - end of the minute.")
    print(f"{'minute':>8} | {'azure':>6} | {'dynatrace':>9} | {'pipeline':>8}")
    next_summary = utcnow() + dt.timedelta(minutes=a.summary_every)
    try:
        while continuous or (utcnow() < deadline and not (utcnow() > gen_end and tracker.done())):
            if not continuous and utcnow() >= gen_end:
                stop.set()
            now = utcnow()
            if continuous:
                tracker.last_slot = now.replace(second=0, microsecond=0) - SLOT
            try:
                azure_new = tracker.observe("azure", azure_slots(azmon.fetch_records(entry, now - dt.timedelta(minutes=lookback), now)), now)
            except Exception as e:
                azure_new = []; print(f"  azure query failed: {str(e)[:150]}")
            try:
                dt_new = []
                for rec in dql.query(dql_text):
                    dt_new += tracker.observe("dynatrace", dt_slots(rec), utcnow())
            except Exception as e:
                dt_new = []; print(f"  dynatrace query failed: {str(e)[:150]}")
            for s in sorted(set(azure_new) | set(dt_new)):
                az, dy = tracker.latency("azure", s), tracker.latency("dynatrace", s)
                print(f"{s:%H:%M} UTC | {fmt(az)} | {fmt(dy):>9} | {fmt(dy - az if None not in (az, dy) else None):>8}", flush=True)
            if continuous and utcnow() >= next_summary:
                next_summary = utcnow() + dt.timedelta(minutes=a.summary_every)
                rs = tracker.summary(last_n=30, now=utcnow(), grace=grace)
                print(f"  -- last 30 min: azure p50 {fmt(rs['azure']['p50'])} p95 {fmt(rs['azure']['p95'])} | "
                      f"dynatrace p50 {fmt(rs['dynatrace']['p50'])} p95 {fmt(rs['dynatrace']['p95'])} | "
                      f"pipeline p50 {fmt(rs['pipeline']['p50'])} | {calls[0]} vault calls", flush=True)
            time.sleep(OBSERVE_EVERY_S)
    except KeyboardInterrupt:
        print("interrupted")
    stop.set()

    sm = tracker.summary(now=utcnow(), grace=grace) if continuous else tracker.summary()
    print(f"\n{calls[0]} vault calls, {len(gen_errors)} errors. Minutes measured: {len(tracker.wanted())}")
    for name in ("azure", "dynatrace", "pipeline"):
        v = sm[name]
        print(f"  {name:>9}: n={v['n']:>3}  p50={fmt(v['p50'])}  p95={fmt(v['p95'])}  max={fmt(v['max'])}")
    if sm["missing"]:
        print("  never arrived in Dynatrace:", ", ".join(f"{s:%H:%M}" for s in sm["missing"]))
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["minute_utc", "azure_latency_s", "dynatrace_latency_s"])
            for s, az, dy in tracker.rows():
                w.writerow([s.isoformat(), az, dy])
        print("written", a.csv)
    sys.exit(1 if sm["missing"] and not continuous else 0)


if __name__ == "__main__":
    main()
