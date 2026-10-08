"""1.42.57 — advisor miss reasons from execute_skips, PDT park, last-slot reserve (10/8 SMCI/SNAP)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import pdt_guard
from activity_log_util import advisor_miss_park_spec, advisor_miss_reason


def _patch(monkeypatch, *, used=2, day_trade=True):
    monkeypatch.setattr(pdt_guard, "would_be_day_trade", lambda b, t, ts=None: day_trade)
    monkeypatch.setattr(pdt_guard, "_gate_day_trade_count", lambda b=None: used)


def test_miss_reason_falls_back_to_execute_skips():
    why = advisor_miss_reason(
        ["[Robinhood] Frac policy [SMCI]: sub-1 share - broker stop N/A, TTP only"],
        [],
        ["SMCI: Skipped: PDT entry guard - 2/3 used"],
    )
    assert "PDT entry guard" in why


def test_miss_reason_generic_without_skips():
    assert "did not fill" in advisor_miss_reason([], [], None)


def test_pdt_guard_parks_advisor_an_hour():
    spec = advisor_miss_park_spec("buy skipped — SMCI: PDT entry guard - 2/3 used")
    assert spec == (3600.0, "pdt_guard")


def test_reserve_blocks_small_gain_on_last_slot(monkeypatch):
    _patch(monkeypatch)
    hold, why = pdt_guard.reserve_last_day_trade(
        "Robinhood", "SNAP", equity=66.0, settings={}, roi_pct=0.61, has_broker_stop=True,
    )
    assert hold and "last day-trade slot" in why


def test_reserve_allows_big_gain(monkeypatch):
    _patch(monkeypatch)
    assert not pdt_guard.reserve_last_day_trade(
        "Robinhood", "SNAP", equity=66.0, settings={}, roi_pct=1.8, has_broker_stop=True,
    )[0]


def test_reserve_needs_broker_stop_and_roi(monkeypatch):
    _patch(monkeypatch)
    assert not pdt_guard.reserve_last_day_trade(
        "Robinhood", "SNAP", equity=66.0, settings={}, roi_pct=0.6, has_broker_stop=False,
    )[0]
    assert not pdt_guard.reserve_last_day_trade(
        "Robinhood", "SNAP", equity=66.0, settings={}, roi_pct=None, has_broker_stop=True,
    )[0]


def test_reserve_only_on_last_slot(monkeypatch):
    _patch(monkeypatch, used=1)
    assert not pdt_guard.reserve_last_day_trade(
        "Robinhood", "SNAP", equity=66.0, settings={}, roi_pct=0.6, has_broker_stop=True,
    )[0]


def test_reserve_off_above_threshold_or_setting(monkeypatch):
    _patch(monkeypatch)
    assert not pdt_guard.reserve_last_day_trade(
        "Robinhood", "SNAP", equity=30000.0, settings={}, roi_pct=0.6, has_broker_stop=True,
    )[0]
    assert not pdt_guard.reserve_last_day_trade(
        "Robinhood", "SNAP", equity=66.0, settings={"pdt_last_slot_min_roi_pct": 0},
        roi_pct=0.6, has_broker_stop=True,
    )[0]
