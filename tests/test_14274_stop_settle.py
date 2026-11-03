"""1.42.74 — a fresh whole-share fill keeps a stop-repair gap when RH says not enough shares."""
import os
import sys
import time
from types import MethodType, SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import gui as gui_mod
import scoring


class _Broker:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.broker_id = "ROBINHOOD"
        self.is_connected = True
        self.supports_protective_stops = True

    def place_protective_stop(self, *args, **kwargs):
        self.calls += 1
        return self.replies.pop(0)


def _desk(broker, holding=None):
    s = SimpleNamespace(
        paper_mode=False,
        settings={"attach_protective_stops": True},
        brokers={"Robinhood": broker},
        logs=[],
        _protective_gaps={},
        _stop_repair_suppress_until={},
        _frac_stop_na_logged=set(),
    )
    s.log_event = s.logs.append
    s._note_protective_gap = MethodType(gui_mod.MarketAdvisorGUI._note_protective_gap, s)
    s._clear_protective_gap = MethodType(gui_mod.MarketAdvisorGUI._clear_protective_gap, s)
    s._suppress_stop_repair = MethodType(gui_mod.MarketAdvisorGUI._suppress_stop_repair, s)
    s._find_holding_for_stop_repair = lambda *_a, **_k: holding
    return s


def _patch(monkey):
    monkey.setattr(scoring, "get_stop_distance_pct", lambda *a, **k: 0.03)
    monkey.setattr(scoring, "get_trail_pct", lambda *a, **k: 0.008)
    monkey.setattr(scoring, "get_protective_order", lambda *a, **k: None)
    monkey.setattr(scoring, "set_protective_order", lambda *a, **k: None)
    monkey.setattr(scoring, "clear_protective_order", lambda *a, **k: None)
    monkey.setattr(gui_mod.time, "sleep", lambda *_a, **_k: None)


def test_fresh_fill_retries_then_keeps_a_repair_gap(monkeypatch):
    _patch(monkeypatch)
    fail = (False, None, "{'detail': 'Not enough shares to sell.'}")
    broker = _Broker([fail, fail, fail])
    s = _desk(broker)
    gui_mod.MarketAdvisorGUI._attach_protective_stop(
        s, "Robinhood", "AMC", "stock", 3.19, 15.95, settle_retry=True,
    )
    assert broker.calls == 3
    assert "Robinhood:AMC" in s._protective_gaps
    until = s._stop_repair_suppress_until["Robinhood:AMC"]
    assert until - time.time() < 180
    assert any("just filled" in line for line in s.logs)


def test_gone_position_still_clears_the_gap(monkeypatch):
    _patch(monkeypatch)
    fail = (False, None, "{'detail': 'Not enough shares to sell.'}")
    broker = _Broker([fail])
    s = _desk(broker, holding=None)
    gui_mod.MarketAdvisorGUI._attach_protective_stop(
        s, "Robinhood", "AMC", "stock", 3.19, 15.95, settle_retry=False,
    )
    assert broker.calls == 1
    assert "Robinhood:AMC" not in s._protective_gaps
    until = s._stop_repair_suppress_until["Robinhood:AMC"]
    assert until - time.time() > 600
    assert any("no shares" in line for line in s.logs)


def test_held_whole_shares_stay_repairable(monkeypatch):
    _patch(monkeypatch)
    fail = (False, None, "{'detail': 'Not enough shares to sell.'}")
    broker = _Broker([fail])
    s = _desk(broker, holding=(5.0, 3.19, "stock"))
    gui_mod.MarketAdvisorGUI._attach_protective_stop(
        s, "Robinhood", "AMC", "stock", 3.19, 15.95, settle_retry=False,
    )
    assert "Robinhood:AMC" in s._protective_gaps
    until = s._stop_repair_suppress_until["Robinhood:AMC"]
    assert until - time.time() < 180


def test_second_try_attaches_once_shares_release(monkeypatch):
    _patch(monkeypatch)
    seen = {}

    def _set(broker_id, ticker, info):
        seen["info"] = info

    monkeypatch.setattr(scoring, "set_protective_order", _set)
    fail = (False, None, "{'detail': 'Not enough shares to sell.'}")
    broker = _Broker([fail, (True, "ord-1", "stop placed")])
    s = _desk(broker)
    gui_mod.MarketAdvisorGUI._attach_protective_stop(
        s, "Robinhood", "AMC", "stock", 3.19, 15.95, settle_retry=True,
    )
    assert broker.calls == 2
    assert seen["info"]["order_id"] == "ord-1"
    assert any("Protective stop attached" in line for line in s.logs)
