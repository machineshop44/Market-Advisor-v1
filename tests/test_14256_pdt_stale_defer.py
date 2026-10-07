"""1.42.56 — same-day stale exits wait for the next session under PDT (RH AMC 10/7)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import auto_cycle
import pdt_guard
from activity_log_util import sell_fail_ttl_for_status


def _patch(monkeypatch, *, day_trade=True):
    monkeypatch.setattr(pdt_guard, "would_be_day_trade", lambda b, t, ts=None: day_trade)


def test_defers_same_day_stale_with_stop(monkeypatch):
    _patch(monkeypatch)
    defer, why = pdt_guard.defer_stale_day_trade(
        "Robinhood", "AMC", equity=66.0, settings={}, has_broker_stop=True,
    )
    assert defer and "PDT" in why


def test_no_defer_without_broker_stop(monkeypatch):
    _patch(monkeypatch)
    assert not pdt_guard.defer_stale_day_trade(
        "Robinhood", "AMC", equity=66.0, settings={}, has_broker_stop=False,
    )[0]


def test_no_defer_when_not_day_trade(monkeypatch):
    _patch(monkeypatch, day_trade=False)
    assert not pdt_guard.defer_stale_day_trade(
        "Robinhood", "F", equity=66.0, settings={}, has_broker_stop=True,
    )[0]


def test_no_defer_above_pdt_threshold(monkeypatch):
    _patch(monkeypatch)
    assert not pdt_guard.defer_stale_day_trade(
        "Robinhood", "AMC", equity=30000.0, settings={}, has_broker_stop=True,
    )[0]


def test_setting_off(monkeypatch):
    _patch(monkeypatch)
    assert not pdt_guard.defer_stale_day_trade(
        "Robinhood", "AMC", equity=66.0, settings={"pdt_defer_stale_day_trades": False},
        has_broker_stop=True,
    )[0]


def test_pdt_skip_backs_off_two_hours():
    st = "Skipped: PDT: stale exit deferred to next session — holding under the broker stop"
    assert auto_cycle.sell_status_should_backoff(st)
    assert sell_fail_ttl_for_status(st) >= 2 * 3600
