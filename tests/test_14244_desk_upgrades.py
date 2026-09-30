"""1.42.44 — remaining Grok items: profit lock, liquidity gate, anchored stop, E*TRADE GTC
stops, Coinbase stop repair, journal excursions + round-trip analytics, Coinbase maker entries,
half-day calendar + lunch/crypto sizing, shadow entry quality, crypto cross-broker cluster."""
import os
import sys
import time
from datetime import date, datetime
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import analytics
import auto_cycle as ac
import market_calendar as mc
import profit_lock
import scoring

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = None


# ---------------------------------------------------------------- profit lock
@pytest.fixture
def lock_tmp(tmp_path):
    with mock.patch.object(profit_lock, "_state_path", return_value=str(tmp_path / "pl.json")):
        profit_lock._state.clear()
        profit_lock._loaded = True
        yield
        profit_lock._state.clear()


def test_profit_lock_trips_after_giveback(lock_tmp):
    s = {"profit_lock_enabled": True, "profit_lock_activate_pct": 1.5, "profit_lock_giveback_pct": 40}
    assert profit_lock.update("Robinhood", 10.0, 500.0, s) == (False, "")  # peak $10 ≥ $7.50
    assert profit_lock.update("Robinhood", 7.0, 500.0, s)[0] is False       # 30% giveback
    tripped, msg = profit_lock.update("Robinhood", 5.5, 500.0, s)            # 45% giveback
    assert tripped and "profit lock" in msg
    assert profit_lock.buys_paused("Robinhood")[0] is True
    assert profit_lock.buys_paused("Coinbase")[0] is False


def test_profit_lock_needs_activation(lock_tmp):
    s = {"profit_lock_activate_pct": 1.5, "profit_lock_giveback_pct": 40}
    profit_lock.update("Coinbase", 5.0, 1000.0, s)   # below $15 activation
    assert profit_lock.update("Coinbase", 0.0, 1000.0, s)[0] is False
    assert profit_lock.buys_paused("Coinbase")[0] is False


def test_profit_lock_disabled(lock_tmp):
    s = {"profit_lock_enabled": False}
    profit_lock.update("E*TRADE", 50.0, 500.0, s)
    assert profit_lock.update("E*TRADE", 0.0, 500.0, s)[0] is False


# ---------------------------------------------------------------- liquidity
def test_liquidity_gate_spread_price_volume():
    scoring.configure_entry_filters({"liquidity_gate_enabled": True})
    assert "Wide spread" in scoring.entry_liquidity_block("XYZ", is_crypto=False, bid_ask=(10.0, 10.2))
    assert "Wide spread" in scoring.entry_liquidity_block("WIF", is_crypto=True, bid_ask=(1.00, 1.02))
    assert scoring.entry_liquidity_block("WIF", is_crypto=True, bid_ask=(1.000, 1.005)) == ""
    assert "Sub-$1" in scoring.entry_liquidity_block("PENNY", is_crypto=False, price=0.5)
    with mock.patch.object(scoring, "stock_avg_dollar_volume", return_value=1e6):
        assert "Thin volume" in scoring.entry_liquidity_block("THIN", price=5.0)
    with mock.patch.object(scoring, "stock_avg_dollar_volume", return_value=None):
        assert scoring.entry_liquidity_block("UNKNOWN", price=5.0) == ""


def test_liquidity_gate_off():
    scoring.configure_entry_filters({"liquidity_gate_enabled": False})
    try:
        assert scoring.entry_liquidity_block("XYZ", bid_ask=(10.0, 12.0)) == ""
    finally:
        scoring.configure_entry_filters({"liquidity_gate_enabled": True})


def test_coinbase_movers_volume_floor_and_max_change():
    tickers = [
        {"product_id": "THIN-USD", "price_percentage_change_24h": "8", "approximate_quote_24h_volume": "100000"},
        {"product_id": "PUMP-USD", "price_percentage_change_24h": "60", "approximate_quote_24h_volume": "90000000"},
        {"product_id": "GOOD-USD", "price_percentage_change_24h": "9", "approximate_quote_24h_volume": "20000000"},
    ]
    got = ac.extract_coinbase_usd_movers({"products": tickers})
    names = [g if isinstance(g, str) else (g.get("ticker") or g.get("symbol")) for g in got]
    assert any("GOOD" in str(n) for n in names)
    assert not any("THIN" in str(n) for n in names)
    assert not any("PUMP" in str(n) for n in names)


# ---------------------------------------------------------------- anchored stop
def test_anchored_stop_fixed_at_first_eval():
    mem = {}
    assert scoring.anchored_hard_stop(mem, -0.04) == -0.04
    assert scoring.anchored_hard_stop(mem, -0.07) == -0.04   # ATR widened — keep sized risk
    assert scoring.anchored_hard_stop(mem, -0.02) == -0.04   # ATR calmed — TTP does tightening
    assert mem["stop_roi"] == -0.04


# ---------------------------------------------------------------- E*TRADE stops
def test_etrade_price_ticks_and_stop_xml():
    from etrade_broker import build_equity_order_xml, etrade_price_str
    assert etrade_price_str(12.3456) == "12.34"
    assert etrade_price_str(0.123456) == "0.1234"
    xml = build_equity_order_xml(
        client_order_id="abc", symbol="AAPL", order_action="SELL", quantity=3,
        price_type="STOP", order_term="GOOD_UNTIL_CANCEL", stop_price=180.555,
    )
    assert "<priceType>STOP</priceType>" in xml
    assert "<stopPrice>180.55</stopPrice>" in xml
    assert "<orderTerm>GOOD_UNTIL_CANCEL</orderTerm>" in xml


def _connected_etrade():
    from etrade_broker import ETradeAdapter
    a = ETradeAdapter()
    a.is_connected = True
    a.account_id_key = "k"
    a.client = mock.Mock()
    a._orders_allowed = lambda: (True, "")
    return a


def test_etrade_protective_stop_floors_whole_shares():
    import etrade_broker as eb
    a = _connected_etrade()
    assert a.supports_protective_stops is True
    with mock.patch.object(eb, "_extract_preview_id", return_value=11), \
            mock.patch.object(eb, "_extract_order_id", return_value=99):
        ok, oid, msg = a.place_protective_stop("AAPL", "stock", 3.7, 200.0, 0.05)
    assert ok and oid == "99"
    place_xml = a.client.place_equity_order.call_args[0][1]
    assert "<quantity>3</quantity>" in place_xml
    assert "<stopPrice>190.00</stopPrice>" in place_xml


def test_etrade_protective_stop_fractional_na():
    a = _connected_etrade()
    ok, oid, msg = a.place_protective_stop("AAPL", "stock", 0.4, 200.0, 0.05)
    assert not ok and "fractional" in msg
    a.client.preview_equity_order.assert_not_called()


def test_health_expects_etrade_whole_and_coinbase_crypto():
    scoring._protective_orders = {b: {} for b in scoring._KNOWN_BROKER_IDS}
    health = scoring.protective_stop_health([
        {"broker_id": "ETRADE", "ticker": "AAPL", "value": 600, "shares": 3.5, "supports_protective": True},
        {"broker_id": "ETRADE", "ticker": "MSFT", "value": 200, "shares": 0.5, "supports_protective": True},
        {"broker_id": "COINBASE", "ticker": "SOL", "value": 40, "shares": 0.2, "is_crypto": True, "supports_protective": True},
        {"broker_id": "COINBASE", "ticker": "DOGE", "value": 2, "shares": 10, "is_crypto": True, "supports_protective": True},
        {"broker_id": "ROBINHOOD", "ticker": "BONK", "value": 20, "shares": 9e6, "is_crypto": True, "supports_protective": True},
    ])
    missing = {(m["broker_id"], m["ticker"]) for m in health["missing"]}
    assert ("ETRADE", "AAPL") in missing
    assert ("COINBASE", "SOL") in missing
    assert ("COINBASE", "DOGE") not in missing          # dust below CB stop minimum
    assert health["fractional_na_count"] == 1           # MSFT 0.5 sh
    assert health["crypto_na_count"] == 1               # RH crypto has no stop API


# ---------------------------------------------------------------- excursions + round trips
def test_excursion_survives_close():
    bid = "ROBINHOOD"
    scoring._portfolio_memory[bid] = {
        "NVDA": {"highest": 110.0, "buy_time": time.time() - 600, "last_eval": time.time(),
                 "avg_cost": 100.0, "mae_roi": -0.02, "stop_roi": -0.04},
    }
    live = scoring.position_excursion(bid, "NVDA")
    assert live["mfe_roi"] == pytest.approx(0.10)
    with mock.patch.object(scoring, "save_state"):
        scoring.mark_position_closed(bid, "NVDA", 108.0)
    after = scoring.position_excursion(bid, "NVDA")
    assert after["mfe_roi"] == pytest.approx(0.10)
    assert after["mae_roi"] == pytest.approx(-0.02)
    assert after["stop_roi"] == pytest.approx(-0.04)


def _row(ts, side, ticker, price, qty, **kw):
    r = {"timestamp": ts, "broker": "Robinhood", "side": side, "ticker": ticker, "price": price,
         "qty": qty, "dollars": price * qty, "status": "Filled", "confirmed": True, "fee_est": 0.0}
    r.update(kw)
    return r


def test_round_trips_expectancy_r_and_buckets():
    rows = [
        _row("2026-09-28T10:05:00", "BUY", "AAA", 10.0, 10, initial_risk=5.0, score=72, rvol=2.0),
        _row("2026-09-28T10:40:00", "SELL", "AAA", 11.0, 10, reason="TTP trail", mfe_roi=0.12, mae_roi=-0.01),
        _row("2026-09-28T13:00:00", "BUY", "BBB", 20.0, 5, initial_risk=4.0, score=65, rvol=0.5),
        _row("2026-09-28T13:30:00", "SELL", "BBB", 19.2, 5, reason="Hard Stop"),
        _row("2026-09-28T14:00:00", "SELL", "CCC", 5.0, 1, status="Fail: rejected"),
        _row("2026-09-28T15:00:00", "BUY", "DDD", 5.0, 2),
    ]
    s = analytics.summarize_round_trips(rows)
    assert s["n"] == 2
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["net"] == pytest.approx(6.0)             # +10 − 4
    assert s["expectancy"] == pytest.approx(3.0)
    assert s["profit_factor"] == pytest.approx(2.5)
    assert s["avg_r"] == pytest.approx((2.0 + -1.0) / 2)
    assert set(s["by_exit"]) == {"ttp", "hard_stop"}
    assert "10:00" in s["by_hour"] and "13:00" in s["by_hour"]
    assert s["by_rvol"]["≥1.5x"]["n"] == 1 and s["by_rvol"]["<0.8x"]["n"] == 1
    assert s["failed_orders"] == {"Robinhood": 1}
    assert s["open_count"] == 1
    text = analytics.format_round_trip_report(s)
    assert "ROUND TRIPS" in text and "By exit reason" in text and "Failed orders" in text


def test_round_trips_partial_scale_out_then_exit_is_one_trip():
    rows = [
        _row("2026-09-28T10:00:00", "BUY", "AAA", 10.0, 10),
        _row("2026-09-28T10:30:00", "SELL", "AAA", 11.0, 5, reason="TTP SCALE-OUT"),
        _row("2026-09-28T11:00:00", "SELL", "AAA", 12.0, 5, reason="TTP trail"),
    ]
    s = analytics.summarize_round_trips(rows)
    assert s["n"] == 1
    assert s["net"] == pytest.approx(15.0)


# ---------------------------------------------------------------- Coinbase maker
def _cb():
    from broker import CoinbaseAdapter
    cb = CoinbaseAdapter()
    cb.is_connected = True
    cb.client = mock.Mock()
    cb._orders_allowed = lambda: (True, "")
    cb.maker_entries = True
    cb.maker_timeout_sec = 10
    cb.get_bid_ask = lambda t, a="": (1.2345, 1.2355)
    cb._get_product_limits = lambda t: {"quote_increment": 0.0001, "base_increment": 0.01}
    cb._cb_call = lambda fn, *a, **k: fn(*a, **k)
    cb._cb_payload = lambda res: res
    return cb


def test_maker_buy_posts_at_bid_post_only():
    cb = _cb()
    cb.client.limit_order_gtc_buy.return_value = {"success": True, "order_id": "m1"}
    cb._extract_order_id = lambda d: d.get("order_id")
    cb.confirm_order = lambda oid, is_crypto=True, timeout_sec=10: (True, "filled")
    cb._order_filled_value = lambda oid: (8.1, 9.99)
    status, spent, oid = cb.place_buy_order("WIF", "crypto", 1.235, 10.0, 0.0, False)
    kw = cb.client.limit_order_gtc_buy.call_args.kwargs
    assert kw["post_only"] is True
    assert kw["limit_price"] == "1.2345"
    assert "Filled" in status and "maker" in status and spent == pytest.approx(9.99) and oid == "m1"


def test_maker_unfilled_cancels_and_keeps_partial():
    cb = _cb()
    cb.client.limit_order_gtc_buy.return_value = {"success": True, "order_id": "m2"}
    cb._extract_order_id = lambda d: d.get("order_id")
    cb.confirm_order = lambda oid, is_crypto=True, timeout_sec=10: (False, "open")
    cb.cancel_order = mock.Mock(return_value=(True, "cancelled"))
    cb._order_filled_value = lambda oid: (0.0, 0.0)
    status, spent, oid = cb.place_buy_order("WIF", "crypto", 1.235, 10.0, 0.0, False)
    assert status.startswith("Skipped: Maker") and spent == 0.0 and oid is None
    cb._order_filled_value = lambda oid: (2.0, 2.47)
    status, spent, oid = cb.place_buy_order("WIF", "crypto", 1.235, 10.0, 0.0, False)
    assert "partial" in status and spent == pytest.approx(2.47)


def test_maker_post_only_reject_falls_back_to_taker():
    cb = _cb()
    cb.client.limit_order_gtc_buy.return_value = {"success": False, "error_response": "INVALID_LIMIT_PRICE_POST_ONLY"}
    cb.client.market_order_buy.return_value = {"success": True, "order_id": "t1"}
    cb._extract_order_id = lambda d: d.get("order_id")
    cb.confirm_order = lambda oid, is_crypto=True, timeout_sec=10: (True, "filled")
    status, spent, oid = cb.place_buy_order("WIF", "crypto", 1.235, 10.0, 0.0, False)
    cb.client.market_order_buy.assert_called_once()
    assert "market" in status and oid == "t1"


# ---------------------------------------------------------------- calendar + session sizing
def test_early_close_days():
    assert mc.early_close_time(date(2026, 11, 27)) == mc.EARLY_CLOSE   # day after Thanksgiving
    assert mc.early_close_time(date(2026, 12, 24)) == mc.EARLY_CLOSE   # Thursday
    assert mc.early_close_time(date(2025, 7, 3)) == mc.EARLY_CLOSE     # Thursday
    assert mc.early_close_time(date(2026, 7, 3)) is None               # Friday = observed holiday
    assert mc.early_close_time(date(2027, 12, 24)) is None             # Friday = observed holiday
    assert mc.early_close_time(date(2026, 9, 29)) is None
    assert mc.regular_close_hour(date(2026, 11, 27)) == 13.0
    assert mc.regular_close_hour(date(2026, 9, 29)) == 16.0


@pytest.mark.skipif(ET is None, reason="zoneinfo unavailable")
def test_equity_curve_half_day_and_lunch():
    m, why = ac.equity_session_size_mult(datetime(2026, 11, 27, 12, 45, tzinfo=ET))
    assert m == 0.0 and "last 30m" in why
    m, why = ac.equity_session_size_mult(datetime(2026, 9, 29, 12, 0, tzinfo=ET))
    assert m == pytest.approx(0.75) and "lunch" in why
    m, _ = ac.equity_session_size_mult(
        datetime(2026, 9, 29, 12, 0, tzinfo=ET), settings={"lunch_lull_size_mult": 1.0},
    )
    assert m == 1.0


@pytest.mark.skipif(ET is None, reason="zoneinfo unavailable")
def test_crypto_off_hours_mult():
    assert ac.crypto_session_size_mult(datetime(2026, 9, 29, 14, 0, tzinfo=ET))[0] == 1.0   # Tue day
    assert ac.crypto_session_size_mult(datetime(2026, 9, 29, 22, 0, tzinfo=ET))[0] == 0.75  # Tue night
    m, why = ac.crypto_session_size_mult(datetime(2026, 10, 3, 14, 0, tzinfo=ET))           # Saturday
    assert m == 0.75 and "weekend" in why
    assert ac.crypto_session_size_mult(
        datetime(2026, 10, 3, 14, 0, tzinfo=ET), settings={"crypto_off_hours_size_mult": 1.0},
    )[0] == 1.0


# ---------------------------------------------------------------- shadow entry quality
def test_bar_quality_features_rvol_vwap_ret():
    import pandas as pd
    idx = pd.date_range("2026-09-28 09:30", periods=30, freq="15min")
    idx = idx.append(pd.date_range("2026-09-29 09:30", periods=30, freq="15min"))
    n = len(idx)
    df = pd.DataFrame({
        "Open": [10.0] * n, "High": [10.2] * n, "Low": [9.8] * n,
        "Close": [10.0] * (n - 1) + [10.5], "Volume": [1000.0] * (n - 1) + [3000.0],
    }, index=idx)
    f = scoring._bar_quality_features(df)
    assert f["rvol"] == pytest.approx(3.0)
    assert f["vwap_stretch_pct"] > 0
    assert f["ret_pct"] == pytest.approx(5.0)


def test_entry_quality_rs_vs_benchmark_cache_only():
    key = lambda s: (s, "15m", "5d")
    scoring._quality_cache[key("AAPL")] = (time.time(), {"ret_pct": 2.0, "rvol": 1.2})
    scoring._quality_cache[key("SPY")] = (time.time(), {"ret_pct": 0.5})
    try:
        with mock.patch.object(scoring, "_get_trend_data") as fetch:
            q = scoring.entry_quality_features("AAPL", is_crypto=False)
            fetch.assert_not_called()
        assert q["rs_pct"] == pytest.approx(1.5)
        assert scoring.entry_quality_features("NOPE", is_crypto=False) == {}
    finally:
        scoring._quality_cache.pop(key("AAPL"), None)
        scoring._quality_cache.pop(key("SPY"), None)


# ---------------------------------------------------------------- crypto cluster
def test_crypto_cluster_caps_across_brokers():
    old = scoring.MAX_CRYPTO_CLUSTER_POSITIONS
    scoring.MAX_CRYPTO_CLUSTER_POSITIONS = 3
    try:
        scoring.set_cross_broker_crypto({"Robinhood": {"BONK", "SHIB"}, "Coinbase": {"SOL"}})
        blocked, why = scoring.concentration_blocks_buy(
            "WIF", {"SOL"}, is_crypto=True, crypto_only_broker=True, broker_id="Coinbase",
        )
        assert blocked and "cluster CRYPTO full" in why
        # add to a name this broker already holds is not a new cluster slot
        assert scoring.crypto_cluster_block("SOL", "Coinbase", {"SOL"}) == (False, "")
        # equities are unaffected
        blocked, _ = scoring.concentration_blocks_buy(
            "F", {"BONK", "SHIB"}, is_crypto=False, broker_id="Robinhood",
        )
        assert not blocked
        # without broker_id the cross-broker rail is skipped (legacy callers)
        blocked, _ = scoring.concentration_blocks_buy("WIF", {"SOL"}, is_crypto=True, crypto_only_broker=True)
        assert not blocked
        rows = scoring.cluster_heat_snapshot([])
        crypto_row = next(r for r in rows if r["name"] == "CRYPTO")
        assert crypto_row["count"] == 3 and crypto_row["full"]
    finally:
        scoring.MAX_CRYPTO_CLUSTER_POSITIONS = old
        scoring.set_cross_broker_crypto({})


def test_crypto_cluster_ignores_own_dust():
    old = scoring.MAX_CRYPTO_CLUSTER_POSITIONS
    scoring.MAX_CRYPTO_CLUSTER_POSITIONS = 2
    try:
        scoring.set_cross_broker_crypto({"Robinhood": {"BONK"}})
        blocked, _ = scoring.concentration_blocks_buy(
            "WIF", {"PEPE"}, holdings_meta=[{"ticker": "PEPE", "value": 0.4, "is_crypto": True}],
            is_crypto=True, crypto_only_broker=True, broker_id="Coinbase",
        )
        assert not blocked
    finally:
        scoring.MAX_CRYPTO_CLUSTER_POSITIONS = old
        scoring.set_cross_broker_crypto({})
