"""1.40 — desk orchestration, peak recovery, focus broker."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_resolve_focus_broker_picks_deployable():
    import desk_orchestration as do

    ctx = {
        "Robinhood": {"can_place_new_buy": False, "deployable_bp": 0},
        "Coinbase": {"can_place_new_buy": False, "deployable_bp": 2},
        "E*TRADE": {"can_place_new_buy": True, "deployable_bp": 92},
    }
    focus = do.resolve_focus_broker(ctx, {"desk_focus_mode": "auto"})
    assert focus == "E*TRADE"


def test_focus_parks_non_focus_broker():
    import desk_orchestration as do

    assert do.focus_parks_buys("Robinhood", "E*TRADE", {"desk_focus_mode": "auto"})
    assert not do.focus_parks_buys("E*TRADE", "E*TRADE", {"desk_focus_mode": "auto"})
    assert not do.focus_parks_buys("Robinhood", "E*TRADE", {"desk_focus_mode": "off"})


def test_deployable_open_count_excludes_otc():
    import desk_orchestration as do

    holdings = [
        {"ticker": "GOEVQ", "shares": 1.0, "price": 0.01},
        {"ticker": "BONK", "shares": 1000.0, "price": 0.02},
    ]
    n = do.deployable_open_count(holdings, broker_name="Robinhood")
    assert n == 1


def test_peak_recovery_cash_heavy():
    import scoring

    scoring._equity_dd["ROBINHOOD"] = {
        "day": scoring._local_day_key(),
        "day_open": 82.0,
        "peak": 110.0,
        "pause_until": 9999999999.0,
        "pause_reason": "Peak drawdown -25% ≤ −14%",
        "peak_dd_streak": 0,
    }
    ok, msg = scoring.maybe_recover_peak_for_cash_heavy_book(
        "ROBINHOOD", 82.0, 78.0, 3.0, settings={"peak_dd_cash_recovery_pct": 0.90},
    )
    assert ok is True
    assert "reset" in msg.lower()
    assert float(scoring._equity_dd["ROBINHOOD"]["peak"]) == 82.0


def test_profit_command_center_includes_action():
    import desk_orchestration as do

    ctx = {
        "E*TRADE": {"can_place_new_buy": True, "deployable_bp": 90, "blockers": []},
        "Coinbase": {
            "can_place_new_buy": False,
            "blockers": [{"code": "fully_deployed", "message": "deployed"}],
        },
    }
    txt = do.format_profit_command_center(
        {"net_after_fees": 1.5, "fee_drag_pct": 2.0, "net_win_rate": 0.5,
         "net_wins": 1, "net_losses": 1},
        ctx,
        focus_broker="E*TRADE",
    )
    assert "Next:" in txt
    assert "E*TRADE" in txt or "Scan" in txt
