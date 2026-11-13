"""1.43.0 — a recycle must clear two round trips plus the funder's open loss."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import scoring
from scoring import buy_candidate_affordable, swap_cost_covered


def test_flat_funder_clears_when_gap_covers_two_round_trips():
    # RH stock RT is 0.20%. Two round trips = 0.40%. A 12-point gap at 0.12%/pt is 1.44%.
    ok, edge, need = swap_cost_covered(12, 0.0, 0.002, edge_per_pt=0.0012)
    assert ok
    assert edge == 12 * 0.0012
    assert abs(need - 0.004) < 1e-9


def test_open_loss_blocks_a_thin_gap():
    # -2% open loss + 0.40% two RT needs 2.40% edge. 12 points is only 1.44%.
    ok, edge, need = swap_cost_covered(12, -0.02, 0.002, edge_per_pt=0.0012)
    assert not ok
    assert edge < need
    ok2, _, _ = swap_cost_covered(25, -0.02, 0.002, edge_per_pt=0.0012)
    assert ok2


def test_picker_rejects_swap_that_misses_open_loss():
    scoring._portfolio_memory.setdefault("ROBINHOOD", {})
    scoring._portfolio_memory["ROBINHOOD"]["F"] = {
        "highest": 10.0,
        "buy_time": time.time() - 400 * 60,
        "last_eval": time.time(),
    }
    fund = scoring.pick_rotation_funding(
        "AVEX",
        95,
        False,
        [{
            "ticker": "F",
            "price": 9.80,
            "avg_cost": 10.0,
            "value": 40,
            "is_crypto": False,
            "shares": 4,
        }],
        posture="growth",
        broker_id="ROBINHOOD",
        block_reason="max_open",
        score_fn=lambda t, is_crypto=False: 80.0,
        skip_regime_check=True,
    )
    # Gap is 15 points → 1.80% edge. Open loss is 2%. Two stock round trips are 0.40%.
    assert fund is None
    assert "open loss" in scoring.last_rotation_reject_reason()


def test_picker_accepts_swap_when_gap_covers_loss():
    scoring._portfolio_memory.setdefault("ROBINHOOD", {})
    scoring._portfolio_memory["ROBINHOOD"]["F"] = {
        "highest": 10.0,
        "buy_time": time.time() - 400 * 60,
        "last_eval": time.time(),
    }
    fund = scoring.pick_rotation_funding(
        "AVEX",
        120,
        False,
        [{
            "ticker": "F",
            "price": 9.90,
            "avg_cost": 10.0,
            "value": 40,
            "is_crypto": False,
            "shares": 4,
        }],
        posture="growth",
        broker_id="ROBINHOOD",
        block_reason="max_open",
        score_fn=lambda t, is_crypto=False: 70.0,
        skip_regime_check=True,
    )
    # Gap 50 → 6% edge. Open loss 1%. This clears.
    assert fund is not None
    assert fund["ticker"] == "F"
    assert fund["swap_hurdle_ok"] is True


def test_winner_not_required_to_be_sold_when_ttp_path_skipped():
    # Positive ROI has zero open loss, so the hurdle is only the two round trips.
    ok, _, need = swap_cost_covered(12, 0.03, 0.002, edge_per_pt=0.0012)
    assert ok and need == 0.004


def test_unapproved_mega_still_needs_a_whole_share():
    settings = {"min_trade_dollars": 5.0, "target_bp_utilization_pct": 88.0}
    ok, why = buy_candidate_affordable(
        buying_power=50.0, price=180.0, is_crypto=False, broker_id="ROBINHOOD",
        equity=200.0, settings=settings, prefer_whole_shares=True, allow_fractional=False,
    )
    assert not ok and "whole share" in why


def test_tagged_fractional_clears_min_ticket_only():
    settings = {"min_trade_dollars": 5.0, "target_bp_utilization_pct": 88.0}
    ok, why = buy_candidate_affordable(
        buying_power=50.0, price=180.0, is_crypto=False, broker_id="ROBINHOOD",
        equity=200.0, settings=settings, prefer_whole_shares=True, allow_fractional=True,
    )
    assert ok, why
