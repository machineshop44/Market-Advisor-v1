"""1.42.45 — working-order/stop blotter rows and the opt-in entry-quality rank adjustment."""
import os
import sys
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import scoring
import working_orders as wo


def test_blotter_rows_merge_working_and_stops():
    working = [
        {"broker": "Coinbase", "order_id": "old", "side": "BUY", "ticker": "SOL", "qty": 0.5,
         "dollars": 75.0, "status": "pending", "ts": 1000.0},
        {"broker": "E*TRADE", "order_id": "new", "side": "SELL", "ticker": "AAPL", "qty": 2,
         "dollars": 0.0, "status": "pending", "ts": 1540.0},
    ]
    stops = [
        ("ROBINHOOD", "NVDA", {"order_id": "rh1", "kind": "broker_stop", "stop_price": 170.0, "qty": 3}),
        ("ETRADE", "AAPL", {"order_id": "paper-AAPL-1", "kind": "virtual_stop", "stop_price": 190.0,
                            "qty": 2, "paper": True}),
    ]
    rows = wo.blotter_rows(working, stops, now=1600.0)
    assert [r["order_id"] for r in rows] == ["new", "old", "paper-AAPL-1", "rh1"]
    assert rows[0]["age_min"] == pytest.approx(1.0)
    assert rows[1]["price"] == pytest.approx(150.0)
    assert rows[1]["age_min"] == pytest.approx(10.0)
    assert rows[0]["price"] is None                    # no dollars → no implied price
    et_stop = rows[2]
    assert et_stop["broker"] == "E*TRADE" and et_stop["paper"] and et_stop["kind"] == "stop"
    assert rows[3]["broker"] == "Robinhood" and rows[3]["price"] == 170.0


def test_blotter_rows_empty():
    assert wo.blotter_rows([], []) == []


def test_quality_rank_adjust_bands():
    assert scoring.entry_quality_rank_adjust({}) == 0.0
    strong = {"rvol": 2.5, "vwap_stretch_pct": 0.5, "rs_pct": 1.0}
    assert scoring.entry_quality_rank_adjust(strong) == pytest.approx(15.0)   # 8+4+5 capped
    chase = {"rvol": 0.4, "vwap_stretch_pct": 4.0, "rs_pct": -1.0}
    assert scoring.entry_quality_rank_adjust(chase) == pytest.approx(-15.0)   # −6−12−5 capped
    assert scoring.entry_quality_rank_adjust({"vwap_stretch_pct": 2.5}) == -6.0
    assert scoring.entry_quality_rank_adjust({"vwap_stretch_pct": -2.0}) == -4.0
    assert scoring.entry_quality_rank_adjust({"rvol": 1.4, "rs_pct": 0.0}) == 4.0


def test_quality_rank_only_applies_when_enabled():
    q = {"rvol": 2.5, "vwap_stretch_pct": 0.5, "rs_pct": 1.0}
    with mock.patch.object(scoring, "_get_trend_data", return_value=(True, True, 50.0, True)), \
            mock.patch.object(scoring, "entry_quality_features", return_value=q):
        scoring.configure_entry_filters({"entry_quality_rank_enabled": False})
        base = scoring.buy_rank_score("AAPL", is_crypto=False)
        scoring.configure_entry_filters({"entry_quality_rank_enabled": True})
        try:
            boosted = scoring.buy_rank_score("AAPL", is_crypto=False)
        finally:
            scoring.configure_entry_filters({"entry_quality_rank_enabled": False})
    assert boosted - base == pytest.approx(15.0)


def test_pending_sell_ghost_implies_a_day_trade():
    import blotter_reconcile as br
    orders = [
        {"broker": "E*TRADE", "order_id": "b1", "side": "BUY", "ticker": "OKLO", "qty": 1},
        {"broker": "E*TRADE", "order_id": "s1", "side": "SELL", "ticker": "OKLO", "qty": 1},
        {"broker": "E*TRADE", "order_id": "s2", "side": "SELL", "ticker": "AMC", "qty": 4},
    ]
    rows = br.day_trades_implied_by_ghosts(["OKLO", "AMC", "SOUN"], orders)
    by = {r["ticker"]: r for r in rows}
    assert by["OKLO"]["record_buy"] is True
    assert "b1" in by["OKLO"]["order_ids"] and "s1" in by["OKLO"]["order_ids"]
    assert by["AMC"]["record_buy"] is False
    assert "SOUN" not in by


def test_equity_only_broker_skips_crypto_before_propose():
    import inspect
    import gui
    src = inspect.getsource(gui.MarketAdvisorGUI._bg_buy_batch)
    assert "broker has no crypto" in src
    assert "supports_crypto" in src
    rec = inspect.getsource(gui.MarketAdvisorGUI._maybe_blotter_reconcile)
    assert "book_is_live" in rec and "view_covers" in rec
