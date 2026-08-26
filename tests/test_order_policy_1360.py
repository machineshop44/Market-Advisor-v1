"""Order-policy helpers: limit toggles + hard-stop force-market."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_sell_force_market_reason():
    from gui import MarketAdvisorGUI

    assert MarketAdvisorGUI._sell_force_market_reason("SELL (Hard Stop: -3.60%)")
    assert MarketAdvisorGUI._sell_force_market_reason("ET FLATTEN (EOD)")
    assert not MarketAdvisorGUI._sell_force_market_reason(
        "SELL (TTP Triggered - Peak: +2.10%, Exit: +1.40%)"
    )
    assert not MarketAdvisorGUI._sell_force_market_reason("SELL (Stale > 2h)")


def test_portfolio_sells_carry_action():
    from auto_cycle import portfolio_sells_from_scored

    assets = [{"ticker": "AAA", "shares": 2.0, "cost": 10.0, "type": "stock"}]
    results = [(0, 9.5, "SELL (Hard Stop: -5.00%)", "stock", None)]
    sells = portfolio_sells_from_scored(assets, results, "Robinhood")
    assert len(sells) == 1
    assert "Hard Stop" in sells[0]["action"]
