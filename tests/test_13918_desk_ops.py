"""1.39.18 — partial TTP, peak DD confirm, engine P&L, sell parsing."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_ttp_partial_on_small_crypto():
    import scoring

    scoring._portfolio_memory.clear()
    scoring._portfolio_memory["COINBASE"] = {}
    broker = "COINBASE"
    ticker = "BONK"
    avg = 1.00
    arm = 0.035
    # Price at +4% — above typical crypto ttp_arm on small book
    live = avg * (1.0 + arm + 0.005)
    scoring._portfolio_memory[broker][ticker] = {
        "highest": live,
        "buy_time": __import__("time").time() - 3600,
        "last_eval": __import__("time").time(),
    }

    def fake_price(t):
        return live

    orig = scoring.fetch_current_price
    scoring.fetch_current_price = fake_price
    try:
        action = scoring.evaluate_holding(
            ticker,
            avg,
            broker_id=broker,
            asset_type="crypto",
            live_price=live,
            equity=120.0,
            holding_value=22.0,
        )
    finally:
        scoring.fetch_current_price = orig

    assert "SELL_PARTIAL" in action
    assert scoring._portfolio_memory[broker][ticker].get("ttp_partial_done") is True


def test_peak_dd_needs_confirm_reads():
    import scoring

    scoring._equity_dd["ROBINHOOD"] = {
        "day": scoring._local_day_key(),
        "day_open": 85.0,
        "peak": 100.0,
        "pause_until": 0.0,
        "pause_reason": "",
        "peak_dd_streak": 0,
    }
    posture = "growth"
    # -15% peak DD on growth (14% threshold); day P&L flat so only peak path fires
    for i in range(2):
        paused, _ = scoring.update_equity_drawdown("ROBINHOOD", 85.0, posture=posture)
        assert paused is False, f"read {i+1} should not pause yet"
    paused, msg = scoring.update_equity_drawdown("ROBINHOOD", 85.0, posture=posture)
    assert paused is True
    assert "Peak drawdown" in msg


def test_sell_fraction_and_portfolio_sells():
    import scoring
    from auto_cycle import portfolio_sells_from_scored

    partial, frac = scoring.sell_fraction_from_action(
        "SELL_PARTIAL (TTP Scale-Out 45% — Peak: +4.00%)"
    )
    assert partial is True
    assert abs(frac - 0.45) < 1e-9

    assets = [{"ticker": "BONK", "shares": 1000.0, "cost": 1.0, "type": "crypto"}]
    results = [(0, 1.05, "SELL_PARTIAL (TTP Scale-Out 45%)", "crypto", None)]
    sells = portfolio_sells_from_scored(assets, results, "Coinbase")
    assert len(sells) == 1
    assert sells[0]["sell_all"] is False
    assert abs(sells[0]["shares"] - 450.0) < 1e-6


def test_engine_pnl_breakdown():
    import analytics

    rows = [
        {
            "timestamp": "2026-08-30T10:00:00",
            "broker": "Coinbase",
            "side": "BUY",
            "ticker": "BONK",
            "price": 1.0,
            "qty": 10.0,
            "dollars": 10.0,
            "status": "Filled",
            "confirmed": True,
            "engine": "CRYPTO",
        },
        {
            "timestamp": "2026-08-30T11:00:00",
            "broker": "Coinbase",
            "side": "SELL",
            "ticker": "BONK",
            "price": 1.1,
            "qty": 10.0,
            "dollars": 11.0,
            "status": "Filled",
            "confirmed": True,
            "reason": "SELL (TTP Triggered)",
        },
    ]
    summary = analytics.summarize_fills(rows)
    by_eng = summary.get("by_engine") or {}
    assert "CRYPTO" in by_eng
    assert "PORTFOLIO" in by_eng
    line = analytics.format_engine_pnl_line(by_eng)
    assert "Engines:" in line
