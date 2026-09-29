"""1.42.41 — anti-chase entry gate, runner trail, exit-reason labels."""
import os
import sys
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import auto_cycle as ac
import scoring


def test_chase_run_pct_from_closes():
    assert abs(scoring.chase_run_pct([100.0, 101.0, 102.0]) - 2.0) < 1e-9
    assert scoring.chase_run_pct([100.0]) is None


def test_chase_block_reason_threshold():
    assert "Chasing" in scoring.chase_block_reason(2.94, 1.5)
    assert scoring.chase_block_reason(1.49, 1.5) == ""
    assert scoring.chase_block_reason(-3.0, 1.5) == ""


def test_configure_entry_filters_clamps_and_toggles():
    scoring.configure_entry_filters({"anti_chase_enabled": False, "anti_chase_run_pct": 50})
    assert scoring._anti_chase_cfg["enabled"] is False
    assert scoring._anti_chase_cfg["run_pct"] == 10.0
    scoring.configure_entry_filters({})
    assert scoring._anti_chase_cfg["enabled"] is True
    assert scoring._anti_chase_cfg["run_pct"] == scoring.ANTI_CHASE_DEFAULT_RUN_PCT


def test_anti_chase_disabled_skips_fetch():
    scoring.configure_entry_filters({"anti_chase_enabled": False})
    try:
        with mock.patch.object(scoring, "_get_yf") as yf:
            assert scoring.anti_chase_block("FET", is_crypto=True) == ""
            yf.assert_not_called()
    finally:
        scoring.configure_entry_filters({})


def test_anti_chase_uses_cached_run():
    import time
    scoring.configure_entry_filters({})
    scoring._anti_chase_cache["TESTCHASE"] = (time.time(), 4.2)
    try:
        assert "Chasing" in scoring.anti_chase_block("TESTCHASE")
        scoring._anti_chase_cache["TESTCHASE"] = (time.time(), 0.3)
        assert scoring.anti_chase_block("TESTCHASE") == ""
        scoring._anti_chase_cache["TESTCHASE"] = (time.time(), None)
        assert scoring.anti_chase_block("TESTCHASE") == ""
    finally:
        scoring._anti_chase_cache.pop("TESTCHASE", None)


def test_explain_gate_mentions_pullback():
    why = scoring.explain_gate_from_recommendation(
        "DO NOT BUY (Chasing: +2.9% in 2h ≥ 1.5% — wait for a pullback)"
    )
    assert "pullback" in why.lower()


def test_runner_trail_widens_only_after_partial():
    assert scoring.runner_trail_pct(0.012, False) == 0.012
    assert abs(scoring.runner_trail_pct(0.012, True) - 0.018) < 1e-12


def test_exit_reason_label():
    assert ac.exit_reason_label("SELL (Hard Stop: -4.10%)") == "Hard Stop: -4.10%"
    assert ac.exit_reason_label(
        "SELL (TTP Runner trail - Peak: +6.00%, Exit: +4.20%)"
    ).startswith("TTP Runner trail")
    assert ac.exit_reason_label("HOLD (ROI: 1.00%)") == ""
    assert ac.exit_reason_label("") == ""
