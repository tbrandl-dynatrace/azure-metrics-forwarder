"""Unit tests for the pure parts of scripts/latency_probe.py (slot parsing, latency bookkeeping, percentiles)."""
import datetime as dt
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import latency_probe as lp

T0 = dt.datetime(2026, 11, 17, 8, 0, tzinfo=dt.timezone.utc)


def at(minute, second=0):
    return T0 + dt.timedelta(minutes=minute, seconds=second)


def test_dt_slots_from_timeseries_record():
    rec = {"timeframe": {"start": "2026-11-17T08:00:00.000000000Z", "end": "2026-11-17T08:05:00.000000000Z"},
           "interval": 60_000_000_000, "s": [None, 3.0, 0.0, 5.0, None]}
    assert lp.dt_slots(rec) == {at(1), at(3)}


def test_dt_slots_default_interval_when_missing():
    rec = {"timeframe": {"start": "2026-11-17T08:00:00Z"}, "s": [1, None, 2]}
    assert lp.dt_slots(rec) == {at(0), at(2)}


def test_azure_slots_ignore_empty_points():
    recs = [{"time": "2026-11-17T08:01:00Z", "total": 4}, {"time": "2026-11-17T08:02:00Z", "total": 0},
            {"time": "2026-11-17T08:03:00Z", "total": None}]
    assert lp.azure_slots(recs) == {at(1)}


def test_tracker_latency_is_measured_from_the_end_of_the_minute():
    t = lp.Tracker(at(1), at(3))
    assert t.wanted() == [at(1), at(2), at(3)]
    t.observe("azure", {at(1)}, at(2, 40))             # minute 1 ends at 08:02:00 -> visible after 40 s
    t.observe("dynatrace", {at(1)}, at(5, 10))         # ... and after 190 s in Dynatrace
    assert t.latency("azure", at(1)) == 40 and t.latency("dynatrace", at(1)) == 190
    assert t.latency("dynatrace", at(2)) is None


def test_first_sighting_wins_and_out_of_range_slots_are_ignored():
    t = lp.Tracker(at(1), at(2))
    assert t.observe("dynatrace", {at(0), at(1), at(9)}, at(4)) == [at(1)]
    assert t.observe("dynatrace", {at(1)}, at(7)) == []                  # already seen
    assert t.latency("dynatrace", at(1)) == (at(4) - at(2)).total_seconds()


def test_done_and_summary():
    t = lp.Tracker(at(1), at(2))
    t.observe("azure", {at(1), at(2)}, at(4))
    t.observe("dynatrace", {at(1)}, at(5))
    assert not t.done()
    assert t.summary()["missing"] == [at(2)]
    t.observe("dynatrace", {at(2)}, at(6))
    assert t.done()
    s = t.summary()
    assert s["missing"] == [] and s["dynatrace"]["n"] == 2 and s["pipeline"]["n"] == 2
    assert s["pipeline"]["p50"] == s["dynatrace"]["p50"] - s["azure"]["p50"]


def test_percentile():
    assert lp.percentile([], 50) is None
    assert lp.percentile([10], 95) == 10
    assert lp.percentile([1, 2, 3, 4], 50) == 2.5
    assert lp.percentile([0, 100], 95) == 95


def test_continuous_summary_only_counts_minutes_as_missing_after_the_grace_period():
    t = lp.Tracker(at(0), at(0))
    t.last_slot = at(5)                                   # continuous mode moves the window forward
    t.observe("dynatrace", {at(0), at(1), at(2)}, at(8))
    grace = dt.timedelta(minutes=3)
    # minute m ends at m+1 and counts as missing once that is more than 3 minutes ago
    assert t.summary(now=at(6), grace=grace)["missing"] == []
    assert t.summary(now=at(7), grace=grace)["missing"] == [at(3)]
    assert t.summary(now=at(8), grace=grace)["missing"] == [at(3), at(4)]
    assert t.summary()["missing"] == [at(3), at(4), at(5)]          # bounded run: everything unseen is missing


def test_summary_can_be_limited_to_the_last_n_minutes():
    t = lp.Tracker(at(0), at(3))
    for m in range(4):
        t.observe("dynatrace", {at(m)}, at(m + 1) + dt.timedelta(seconds=60 * (m + 1)))   # latency 60, 120, 180, 240 s
    assert t.summary()["dynatrace"]["n"] == 4
    last2 = t.summary(last_n=2)["dynatrace"]
    assert last2["n"] == 2 and last2["p50"] == 210
