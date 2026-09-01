"""1.41 — profit guard, single-stack, RTH burst, fee-gated focus fast-path."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_profit_guard_trips_on_negative_net_and_high_drag():
    import desk_orchestration as do

    summary = {
        "net_after_fees": -2.50,
        "fee_drag_pct": 3.2,
        "net_wins": 1,
        "net_losses": 4,
    }
    settings = {"profit_guard_enabled": True, "profit_guard_min_closed": 3}
    tripped, why = do.profit_guard_tripped(summary, settings)
    assert tripped is True
    assert "fee drag" in why.lower()

    ok_summary = {"net_after_fees": 1.0, "fee_drag_pct": 3.2, "net_wins": 2, "net_losses": 1}
    tripped2, _ = do.profit_guard_tripped(ok_summary, settings)
    assert tripped2 is False


def test_profit_guard_acknowledgment():
    import desk_orchestration as do

    summary = {"net_after_fees": -5.0, "fee_drag_pct": 4.0, "net_wins": 0, "net_losses": 5}
    settings = {
        "profit_guard_enabled": True,
        "profit_guard_min_closed": 3,
        "profit_guard_ack_until": time.time() + 3600,
    }
    tripped, _ = do.profit_guard_tripped(summary, settings)
    assert tripped is False


def test_consolidated_stack_preview():
    import desk_orchestration as do

    ctx = {
        "E*TRADE": {"can_place_new_buy": True, "deployable_bp": 92, "buying_power": 92},
        "Robinhood": {"can_place_new_buy": True, "deployable_bp": 40, "buying_power": 40},
        "Coinbase": {"can_place_new_buy": True, "deployable_bp": 22, "buying_power": 22},
    }
    preview = do.consolidated_deployable_preview(ctx, focus_broker="E*TRADE")
    assert preview["combined"] == 154.0
    assert len(preview["fragmented"]) == 2
    banner = do.format_single_stack_banner(preview, money_fmt=lambda x: f"${x:.0f}")
    assert banner is not None
    assert "E*TRADE" in banner


def test_open_burst_prioritizes_focus_penny():
    import desk_orchestration as do

    tasks = do.session_boundary_tasks(
        "open", "E*TRADE", focus_broker="E*TRADE", focus_mode_on=True,
    )
    assert tasks[0] == "PORTFOLIO"
    assert tasks[1] == "PENNY"
    parked = do.session_boundary_tasks(
        "open", "Robinhood", focus_broker="E*TRADE", focus_mode_on=True,
    )
    assert parked == ("PORTFOLIO",)


def test_focus_advisor_requires_fee_clear():
    import desk_orchestration as do

    def _fee_clear(broker, ticker, score=0, is_crypto=False, asset_type=""):
        return score >= 80, "weak"

    assert do.focus_advisor_auto_clear(
        [{"ticker": "F", "score": 85, "asset_type": "stock"}],
        "E*TRADE",
        fee_clear_fn=_fee_clear,
    )
    assert not do.focus_advisor_auto_clear(
        [{"ticker": "F", "score": 40, "asset_type": "stock"}],
        "E*TRADE",
        fee_clear_fn=_fee_clear,
    )


def test_filter_otc_portfolio_items():
    import auto_cycle as ac

    items = [
        (0, "GOEVQ", 1.0, 0.01, "stock", "Robinhood"),
        (1, "F", 10.0, 12.0, "stock", "Robinhood"),
    ]
    kept, skipped = ac.filter_otc_portfolio_items(items, broker_name="Robinhood")
    assert skipped == ["GOEVQ"]
    assert len(kept) == 1
    assert kept[0][1] == "F"
