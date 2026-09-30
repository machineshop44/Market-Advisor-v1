"""1.42.43 — 9/29–9/30 log audit: RH micro-price crypto collar, partial flag on fill,
E*TRADE fractional dust, readable E*TRADE errors, Discord content cap."""
import os
import sys
import time
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scoring
from activity_log_util import discord_safe_content
from broker import rh_crypto_collar_price
from etrade_client import etrade_error_summary


def test_bonk_sell_collar_stays_on_tick_and_below_bid():
    # robin_stocks rounded 0.00000371 → 0.000004 (above bid); RH rejected.
    assert rh_crypto_collar_price("0.00000371", "0.00000001", "sell") == "0.00000371"
    assert rh_crypto_collar_price("0.000003715", "0.00000001", "sell") == "0.00000371"
    assert rh_crypto_collar_price("0.000003715", "0.00000001", "buy") == "0.00000372"


def test_collar_without_tick_keeps_quote_precision():
    assert rh_crypto_collar_price("0.00000371", None, "sell") == "0.00000371"
    assert rh_crypto_collar_price("64123.45", "0.01", "sell") == "64123.45"
    assert rh_crypto_collar_price(None, "0.01", "sell") == ""


def test_trail_break_exits_full_position_before_retrying_partial():
    bid = "COINBASE"
    scoring._portfolio_memory[bid] = {
        "BONK": {"highest": 1.10, "buy_time": time.time() - 3600, "last_eval": time.time()},
    }
    scoring._broker_last_holding_eval[bid] = time.time()
    with mock.patch.object(scoring, "save_state"):
        action = scoring.evaluate_holding(
            "BONK", 1.0, broker_id=bid, asset_type="crypto", live_price=1.06,
            equity=120.0, holding_value=22.0,
        )
    assert action.startswith("SELL (TTP Triggered")


def test_etrade_error_summary_extracts_message():
    xml = "<Error>\n  <code>1037</code>\n  <message>Fractional shares not allowed.</message>\n</Error>"
    assert etrade_error_summary(xml) == "[1037] Fractional shares not allowed."
    assert "Unauthorized" in etrade_error_summary(
        "<html><head><title>HTTP Status 401 – Unauthorized</title></head></html>"
    )


def test_discord_content_strips_html_and_caps_length():
    msg = "[REAUTH] failed: <!doctype html><html><head><style>body{x}</style></head>" + "x" * 3000
    out = discord_safe_content(msg)
    assert len(out) <= 2000 and "<html" not in out and "body{x}" not in out
    assert discord_safe_content("edge 2.5% < need 3.6% and > 1") == "edge 2.5% < need 3.6% and > 1"


def test_etrade_sell_skips_fractional_dust():
    from etrade_broker import ETradeAdapter
    a = ETradeAdapter.__new__(ETradeAdapter)
    a.is_connected, a.client, a.account_id_key = True, object(), "k"
    a.supports_extended_hours = False
    with mock.patch.object(ETradeAdapter, "_reject_crypto", return_value=False), \
         mock.patch.object(ETradeAdapter, "_orders_allowed", return_value=(True, "")), \
         mock.patch.object(ETradeAdapter, "_regular_session_required", return_value=(True, "")):
        st, oid = a.place_sell_order("PLUG", "stock", 2.0, 0.25, 0.1, False)
    assert st.startswith("Skipped: Dust") and oid is None
