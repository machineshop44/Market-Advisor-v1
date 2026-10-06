"""1.42.55 — realized crypto edge feedback on the fee gate."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import scoring


def _sell(broker, ticker, net, asset="Ready (Crypto)"):
    return {"side": "SELL", "broker": broker, "ticker": ticker, "asset_type": asset, "roi_net": net}


def test_no_feedback_below_six_exits():
    rows = [_sell("Coinbase", "BCH-USD", -0.012)] * 5
    assert scoring.realized_crypto_edge_mult("COINBASE", rows=rows) == (1.0, "")


def test_losing_streak_doubles_need():
    rows = [_sell("Coinbase", "SOL-USD", -0.013)] * 6
    mult, tag = scoring.realized_crypto_edge_mult("COINBASE", rows=rows)
    assert mult == 2.0 and "realized" in tag


def test_mixed_negative_is_one_and_half():
    rows = [_sell("Coinbase", "XRP-USD", 0.01)] * 3 + [_sell("Coinbase", "XRP-USD", -0.02)] * 4
    mult, _ = scoring.realized_crypto_edge_mult("COINBASE", rows=rows)
    assert mult == 1.5


def test_positive_book_no_penalty():
    rows = [_sell("Coinbase", "BTC-USD", 0.02)] * 4 + [_sell("Coinbase", "BTC-USD", -0.01)] * 3
    assert scoring.realized_crypto_edge_mult("COINBASE", rows=rows)[0] == 1.0


def test_other_broker_and_equity_ignored():
    rows = [_sell("Robinhood", "SOL", -0.02)] * 6 + [_sell("Coinbase", "AMC", -0.02, "stock")] * 6
    assert scoring.realized_crypto_edge_mult("COINBASE", rows=rows)[0] == 1.0


def test_setting_off():
    rows = [_sell("Coinbase", "SOL-USD", -0.013)] * 8
    assert scoring.realized_crypto_edge_mult(
        "COINBASE", {"crypto_realized_edge_feedback": False}, rows=rows,
    )[0] == 1.0


def test_fee_gate_applies_feedback(monkeypatch):
    monkeypatch.setattr(scoring, "min_entry_edge_pct", lambda *a, **k: 0.0415)
    monkeypatch.setattr(scoring, "_is_et_weekend", lambda now=None: False)
    monkeypatch.setattr(scoring, "realized_crypto_edge_mult", lambda *a, **k: (1.0, ""))
    ok, _ = scoring.new_entry_clears_fees_ok("COINBASE", "BCH-USD", 98, is_crypto=True)
    assert ok
    monkeypatch.setattr(scoring, "realized_crypto_edge_mult", lambda *a, **k: (1.5, " realized"))
    ok, why = scoring.new_entry_clears_fees_ok("COINBASE", "BCH-USD", 98, is_crypto=True)
    assert not ok and "realized" in why
