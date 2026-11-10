"""1.42.76 — RH whole-share sell that hits 'not enough shares' retries live fractional qty."""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import broker as broker_mod


def test_whole_share_not_enough_retries_fractional(monkeypatch):
    adapter = broker_mod.RobinhoodAdapter.__new__(broker_mod.RobinhoodAdapter)
    adapter.is_connected = True
    # First call (sell_all refresh) looks whole; retry after fail returns true fractional.
    lives = [1.0, 0.9968]
    adapter._live_sellable_qty = MagicMock(
        side_effect=lambda *_a, **_k: lives.pop(0) if lives else 0.9968
    )
    adapter._rh_equity_sellable = MagicMock(return_value=(True, "iid", ""))
    adapter.confirm_order = MagicMock(return_value=(True, "filled"))

    market_calls = []

    def _market(**kwargs):
        market_calls.append(kwargs)
        return {"detail": "Not enough shares to sell."}

    frac_calls = []

    def _frac(symbol, qty, **kwargs):
        frac_calls.append((symbol, qty))
        return {"id": "ord-frac", "state": "filled"}

    fake_r = SimpleNamespace(
        order_sell_market=_market,
        order_sell_fractional_by_quantity=_frac,
    )
    monkeypatch.setattr(broker_mod, "r", fake_r)

    status, oid = adapter.place_sell_order(
        "F", "stock", 12.29, 1.0, 0.0, False,
        market_hours="regular_hours", allow_fractional=True, sell_all=True,
    )
    assert market_calls, "should attempt whole-share market first"
    assert frac_calls and abs(frac_calls[0][1] - 0.9968) < 1e-9
    assert oid == "ord-frac"
    assert "Filled" in status
    assert "Fail" not in status
