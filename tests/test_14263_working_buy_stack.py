"""1.42.63 — 10/15–10/16: four GRAB tickets while the first was still working;
RH 'only 1 share' retried all morning; SHIB dust kept logging missing cost basis."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import activity_log_util as alu
import auto_cycle
import blotter_reconcile as br
import working_orders as wo


def test_open_working_buy_blocks_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(wo, "_state_path", lambda: str(tmp_path / "wo.json"))
    wo._orders = {}
    wo._loaded = False
    assert not wo.has_open_buy("Robinhood", "GRAB")
    wo.register(
        broker="Robinhood", order_id="abc", side="BUY", ticker="GRAB", dollars=15.0,
    )
    assert wo.has_open_buy("Robinhood", "GRAB")
    assert not wo.has_open_buy("Robinhood", "AMC")
    assert not wo.has_open_buy("Coinbase", "GRAB")


def test_one_share_cap_backs_off_for_hours():
    status = "Fail: {'detail': 'You can only purchase 1 share of GRAB.'}"
    assert auto_cycle.buy_status_should_backoff(status)
    assert alu.buy_fail_ttl_for_status(status) >= 6 * 3600
    assert alu.noisy_note_key("[Robinhood] Skip [GRAB] — working buy already open")


def test_dust_holding_carries_market_value():
    rows = br.missing_basis_tickers(
        [{"ticker": "SHIB", "shares": 13000, "price": 0.0001, "value": 1.30}],
        has_basis_fn=lambda b, t: False,
        broker="Robinhood",
    )
    assert rows and rows[0]["broker_cost"] == 0
    assert abs(rows[0]["market_value"] - 1.30) < 1e-9
