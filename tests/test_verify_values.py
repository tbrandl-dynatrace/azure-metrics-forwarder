"""Unit tests for the comparison logic in scripts/verify_values.py (the real run needs Azure and Dynatrace)."""
import datetime as dt
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import verify_values as vv

T0 = dt.datetime(2026, 11, 17, 9, 0, tzinfo=dt.timezone.utc)
NOW = T0 + dt.timedelta(minutes=30)


def at(m):
    return T0 + dt.timedelta(minutes=m)


def azure(slot, hits=100.0, avg=12.5, mn=3.0, mx=40.0):
    iso = slot.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ServiceApiHit": {"metricName": "ServiceApiHit", "time": iso, "count": 100, "total": hits, "minimum": 1, "maximum": 1, "average": 1},
            "ServiceApiLatency": {"metricName": "ServiceApiLatency", "time": iso, "count": 100, "total": avg * 100, "minimum": mn, "maximum": mx, "average": avg}}


def dyn(hits=100.0, avg=12.5, mn=3.0, mx=40.0):
    return {"c": hits, "mn": mn, "mx": mx, "a": avg}


def test_dynatrace_record_is_split_into_slots():
    rec = {"timeframe": {"start": "2026-11-17T09:00:00.000000000Z"}, "c": [None, 5.0, 7.0],
           "mn": [None, 1.0, 2.0], "mx": [None, 9.0, 8.0], "a": [None, 4.0, 5.0]}
    assert vv.dynatrace_by_slot(rec) == {at(1): {"c": 5.0, "mn": 1.0, "mx": 9.0, "a": 4.0},
                                        at(2): {"c": 7.0, "mn": 2.0, "mx": 8.0, "a": 5.0}}


def test_matching_counter_and_gauge_values_pass():
    rows, bad = vv.compare({at(1): dyn(), at(2): dyn()}, {at(1): azure(at(1)), at(2): azure(at(2))}, NOW)
    assert len(rows) == 2 and bad == []


def test_counter_mismatch_is_reported():
    rows, bad = vv.compare({at(1): dyn(hits=2.0)}, {at(1): azure(at(1), hits=100.0)}, NOW)
    assert [p for _, p in bad] == ["counter sum 2.0 != Azure total 100.0"]


def test_gauge_mismatch_is_reported():
    _, bad = vv.compare({at(1): dyn(avg=1.0, mx=2.0)}, {at(1): azure(at(1))}, NOW)
    assert sorted(p.split(" ")[1] for _, p in bad) == ["a", "mx"]


def test_recent_minutes_and_unmatched_minutes_are_ignored():
    recent = NOW - dt.timedelta(minutes=2)
    rows, bad = vv.compare({recent: dyn(hits=1.0), at(1): dyn()}, {recent: azure(recent), at(3): azure(at(3))}, NOW)
    assert rows == [] and bad == []


def test_minutes_without_azure_samples_are_ignored():
    az = azure(at(1))
    az["ServiceApiHit"]["count"] = 0
    rows, bad = vv.compare({at(1): dyn(hits=0.0)}, {at(1): az}, NOW)
    assert rows == [] and bad == []


def test_close_uses_a_relative_tolerance():
    assert vv.close(1000.0, 1000.5) and not vv.close(1000.0, 1010.0)
    assert not vv.close(None, 1.0)
