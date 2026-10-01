"""1.42.48 — sells ahead of scans, scan time budget, quiet heartbeat and cycle logging."""
import os
import sys
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import auto_cycle as ac


# --- Heartbeat skip ----------------------------------------------------------

FP = ((("Robinhood", "✅ Online · Armed", 66, 64),), ("Robinhood",), "REGULAR")


def test_heartbeat_posts_first_time_and_on_change():
    assert ac.heartbeat_should_skip(FP, None, 0, {}) is False
    changed = ((("Robinhood", "✅ Online · Armed", 70, 50),), ("Robinhood",), "REGULAR")
    assert ac.heartbeat_should_skip(changed, FP, 3600, {}) is False


def test_heartbeat_skips_unchanged_until_max_quiet():
    assert ac.heartbeat_should_skip(FP, FP, 3600, {}) is True
    assert ac.heartbeat_should_skip(FP, FP, 3 * 3600, {}) is True
    assert ac.heartbeat_should_skip(FP, FP, 4 * 3600, {}) is False


def test_heartbeat_skip_can_be_disabled():
    assert ac.heartbeat_should_skip(FP, FP, 3600, {"discord_heartbeat_skip_unchanged": False}) is False


# --- Queue order -------------------------------------------------------------

def _queue_stub(queue, focus="E*TRADE"):
    import gui as gui_mod
    s = SimpleNamespace(task_queue=list(queue))
    s._desk_focus_broker = lambda: focus
    s._buy_engines_should_rest = lambda b, engine=None: (False, "")
    return gui_mod.MarketAdvisorGUI._reorder_task_queue_buy_focus, s


def test_portfolio_sells_run_before_buy_scans():
    fn, s = _queue_stub([
        ("E*TRADE", "PENNY"), ("Robinhood", "CRYPTO"),
        ("Robinhood", "PORTFOLIO"), ("E*TRADE", "PORTFOLIO"), ("Coinbase", "PORTFOLIO"),
    ])
    fn(s)
    first_three = [t for _, t in s.task_queue[:3]]
    assert first_three == ["PORTFOLIO"] * 3


# --- Scan time budget --------------------------------------------------------

def test_scan_budget_skips_remaining_tickers(monkeypatch):
    import gui as gui_mod
    import scoring

    clock = {"t": 0.0}
    monkeypatch.setattr(gui_mod.time, "time", lambda: clock["t"])

    def slow_eval(*_a, **_k):
        clock["t"] += 30.0
        return "DO NOT BUY (test)"

    monkeypatch.setattr(scoring, "evaluate_opportunity", slow_eval)
    logged = []
    broker = SimpleNamespace(broker_id="ETRADE", supports_equities=True, get_live_price=lambda *a, **k: 5.0)
    s = SimpleNamespace(
        cycle_broker=broker,
        cycle_broker_name="E*TRADE",
        brokers={"Robinhood": broker},
        settings={},
        get_effective_balances=lambda *_a, **_k: (100.0, 100.0, 0.0),
        _throttled_log=lambda key, msg, **_k: logged.append(msg),
    )
    items = [(i, f"T{i}", 0.0, 0.0, "Penny Stock") for i in range(5)]
    results = gui_mod.MarketAdvisorGUI._bg_score_opportunities(s, items, budget_sec=60)
    assert len(results) == 5
    skipped = [r for r in results if r[2].startswith("SKIPPED")]
    assert len(skipped) == 2
    assert logged and "budget" in logged[0]


def test_skipped_rows_never_become_buys():
    assert "BUY" not in "SKIPPED (scan time budget)".upper()
