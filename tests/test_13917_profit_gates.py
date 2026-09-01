"""1.39.17 — fee-clear entries + Growth DD tighten."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_new_entry_clears_fees_blocks_weak_score():
    from scoring import new_entry_clears_fees_ok, estimated_signal_edge_pct, min_entry_edge_pct

    # Very weak equity score cannot clear ~1% entry edge on RH stocks
    ok, why = new_entry_clears_fees_ok(
        "ROBINHOOD", "SOUN", score=42.0, is_crypto=False, asset_type="stock",
    )
    assert ok is False
    assert "Fee gate" in why

    # Strong score clears
    ok2, _ = new_entry_clears_fees_ok(
        "ROBINHOOD", "SOUN", score=85.0, is_crypto=False, asset_type="stock",
    )
    assert ok2 is True

    need = min_entry_edge_pct("ROBINHOOD", "SOUN", "stock")
    edge = estimated_signal_edge_pct(85.0, is_crypto=False)
    assert edge >= need


def test_crypto_new_entry_always_fee_gates():
    from scoring import crypto_new_entry_ok

    # Low score: fail hold bias or fee gate (either is fine — must not pass)
    ok, why = crypto_new_entry_ok(
        "COINBASE", "BONK", score=45.0, notional=25.0,
        skip_turbulence=True, equity=40.0,
    )
    assert ok is False
    assert why


def test_growth_peak_dd_tightened():
    from scoring import get_risk_posture_profile

    g = get_risk_posture_profile("growth")
    assert float(g["peak_dd_pause_pct"]) == 0.14
    assert int(g["max_buys_per_cycle"]) == 1
    assert float(g["day_dd_pause_pct"]) == 0.06
