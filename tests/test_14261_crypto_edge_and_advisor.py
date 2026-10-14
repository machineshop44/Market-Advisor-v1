"""1.42.61 — 2-week market check: crypto score→edge recalibration, overnight + BTC-drop
entry blocks, advisor crypto approvals go to cloud, budget fallback skips sub-90 scores."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import desk_advisor_ai as dai
import scoring

ET = ZoneInfo("America/New_York")


def test_crypto_edge_slope_recalibrated():
    assert abs(scoring.estimated_signal_edge_pct(98, is_crypto=True) - 0.0232) < 1e-9
    assert abs(scoring.estimated_signal_edge_pct(98, is_crypto=False) - 0.029) < 1e-9
    assert abs(
        scoring.estimated_signal_edge_pct(98, is_crypto=True, settings={"crypto_edge_per_pt": 0.001})
        - 0.058
    ) < 1e-9


def test_typical_crypto_scores_now_fail_fee_gate(monkeypatch):
    monkeypatch.setattr(scoring, "realized_crypto_edge_mult", lambda *a, **k: (1.0, ""))
    monkeypatch.setattr(scoring, "_is_et_weekend", lambda now=None: False)
    for broker in ("COINBASE", "ROBINHOOD"):
        ok, why = scoring.new_entry_clears_fees_ok(broker, "ATOM-USD", 103, is_crypto=True)
        assert not ok and "Fee gate" in why
    ok, _ = scoring.new_entry_clears_fees_ok("ROBINHOOD", "ATOM-USD", 125, is_crypto=True)
    assert ok


@pytest.mark.real_crypto_gates
def test_overnight_block_window():
    ok, why = scoring.crypto_overnight_entry_ok({}, now=datetime(2026, 10, 14, 0, 42, tzinfo=ET))
    assert not ok and "overnight" in why
    ok, _ = scoring.crypto_overnight_entry_ok({}, now=datetime(2026, 10, 14, 6, 0, tzinfo=ET))
    assert ok
    ok, _ = scoring.crypto_overnight_entry_ok(
        {"crypto_overnight_block": False}, now=datetime(2026, 10, 14, 2, 0, tzinfo=ET),
    )
    assert ok


@pytest.mark.real_crypto_gates
def test_btc_drop_block():
    ok, why = scoring.crypto_btc_drop_ok({}, ret_fn=lambda: -0.0125)
    assert not ok and "BTC falling" in why
    assert scoring.crypto_btc_drop_ok({}, ret_fn=lambda: -0.004)[0]
    assert scoring.crypto_btc_drop_ok({}, ret_fn=lambda: None)[0]
    assert scoring.crypto_btc_drop_ok({"crypto_btc_drop_block_pct": 0}, ret_fn=lambda: -0.05)[0]


@pytest.mark.real_crypto_gates
def test_fee_gate_applies_btc_drop(monkeypatch):
    monkeypatch.setattr(scoring, "crypto_overnight_entry_ok", lambda settings=None, now=None: (True, ""))
    monkeypatch.setattr(scoring, "_btc_1h_return", lambda now=None: -0.02)
    ok, why = scoring.new_entry_clears_fees_ok("ROBINHOOD", "ADA-USD", 150, is_crypto=True)
    assert not ok and "BTC falling" in why
    ok, _ = scoring.new_entry_clears_fees_ok("ETRADE", "AMC", 98, is_crypto=False)
    assert ok


def _prop(tick, score, asset="cryptocurrency"):
    return {"ticker": tick, "score": score, "asset_type": asset, "broker": "Robinhood",
            "engine": "CRYPTO" if "crypto" in asset else "PENNY", "dollars": 12, "price": 1.0}


def test_crypto_never_takes_clear_local_shortcut(monkeypatch):
    monkeypatch.setattr(dai, "ai_configured", lambda s=None: True)
    monkeypatch.setattr(dai, "cloud_provider_chain", lambda s=None: [])
    out = dai.analyze_proposal(_prop("ATOM", 98), {}, {})
    assert out["source"] == "local_fallback"
    out = dai.analyze_proposal(_prop("AMC", 98, "stock"), {}, {})
    assert out["source"] == "local"


def test_fallback_skips_sub_90():
    out = dai._local_fallback_guard(
        {"verdict": dai.VERDICT_APPROVE, "source": "local_fallback", "brief": "x"},
        _prop("XRP", 87),
    )
    assert out["verdict"] == dai.VERDICT_SKIP and "87" in out["brief"]
    out = dai._local_fallback_guard(
        {"verdict": dai.VERDICT_APPROVE, "source": "local_fallback", "brief": "x"},
        _prop("XRP", 95),
    )
    assert out["verdict"] == dai.VERDICT_APPROVE
    out = dai._local_fallback_guard(
        {"verdict": dai.VERDICT_APPROVE, "source": "local", "brief": "x"}, _prop("XRP", 70),
    )
    assert out["verdict"] == dai.VERDICT_APPROVE
