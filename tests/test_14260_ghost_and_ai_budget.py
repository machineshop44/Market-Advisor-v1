"""1.42.60 — just-sold ghost rows, sell-all journal reset, AI JSON parsing and budget reserve."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import desk_advisor_ai as dai
from activity_log_util import drop_just_sold_rows
from cost_basis import inventory_vwap_from_journal


def test_just_sold_row_hidden_then_returns():
    rows = [{"ticker": "DOGE-USD"}, {"ticker": "ETH"}]
    exits = {("Coinbase", "DOGE"): 1000.0}
    assert [r["ticker"] for r in drop_just_sold_rows(rows, "Coinbase", exits, 1040.0)] == ["ETH"]
    assert len(drop_just_sold_rows(rows, "Coinbase", exits, 1400.0)) == 2
    assert len(drop_just_sold_rows(rows, "Robinhood", exits, 1040.0)) == 2


def _fill(side, qty, px, status="Filled"):
    return {"broker": "Coinbase", "ticker": "DOGE", "side": side, "qty": qty, "price": px,
            "status": status, "confirmed": True}


def test_sell_all_resets_journal_inventory():
    rows = [
        _fill("BUY", 100, 0.10),
        _fill("SELL", 60, 0.11, status="Coinbase Sell-All Filled (60)"),
        _fill("BUY", 58.6, 0.0859),
    ]
    assert abs(inventory_vwap_from_journal(rows, "Coinbase", "DOGE") - 0.0859) < 1e-9


def test_extract_json_fenced_two_objects_trailing_comma():
    raw = 'Sure!\n```json\n{"verdict": "approve", "detail": "ok",}\n```\n{"note": 1}'
    assert dai._extract_json_object(raw)["verdict"] == "approve"


def test_extract_json_truncated_keeps_verdict():
    raw = '{"verdict": "wait", "detail": "Spread is wide and momentum is fad'
    out = dai._extract_json_object(raw)
    assert out["verdict"] == "wait" and out["detail"].startswith("Spread")


def test_extract_json_think_block():
    raw = '<think>{"verdict": "skip"} maybe</think>{"verdict": "approve", "detail": "x"}'
    assert dai._extract_json_object(raw)["verdict"] == "approve"


def test_background_digest_reserves_half_budget():
    dai.reset_ai_budgets()
    slot = dai._budget_slot("gemini")
    slot["day_key"] = dai._budget_day()
    slot["day_count"] = 9
    assert dai._background_budget_ok({}, "gemini")
    slot["day_count"] = 10
    assert not dai._background_budget_ok({}, "gemini")
    dai.reset_ai_budgets()
