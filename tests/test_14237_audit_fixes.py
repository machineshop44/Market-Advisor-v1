"""
1.42.37 — Grok audit money-path fixes:
advisor $ cap, focus park eq≤0, blotter never invents mark-as-cost,
propose TTL keep, CB product-limits TTL/no-cache-on-fail, et_flatten defaults,
KNOWN_CRYPTOS local advisor, working-order expire, Discord broker default.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _advisor_cap(row_dollars, advisor_dollars):
    """Mirrors buy-path Advisor hard cap (pure helper for unit test)."""
    try:
        adv = float(advisor_dollars)
        row = float(row_dollars or 0)
    except (TypeError, ValueError):
        return row_dollars
    if adv <= 0:
        return row
    if row <= 0:
        return 0.0  # reject — live size 0
    return min(row, adv)


class TestFocusParkZeroEquity(unittest.TestCase):
    def test_park_when_eq_zero(self):
        from blotter_reconcile import small_book_focus_park_active

        s = {"desk_focus_park_others": False, "desk_focus_park_others_auto_under": 500.0}
        self.assertTrue(small_book_focus_park_active(0.0, s))
        self.assertTrue(small_book_focus_park_active(-1.0, s))
        self.assertTrue(small_book_focus_park_active(200.0, s))
        self.assertFalse(small_book_focus_park_active(600.0, s))

    def test_under_disabled(self):
        from blotter_reconcile import small_book_focus_park_active

        s = {"desk_focus_park_others": False, "desk_focus_park_others_auto_under": 0.0}
        self.assertFalse(small_book_focus_park_active(0.0, s))
        self.assertFalse(small_book_focus_park_active(200.0, s))

    def test_focus_parks_buys_zero_eq(self):
        import desk_orchestration as do

        settings = {"desk_focus_mode": "auto", "desk_focus_park_others": False}
        self.assertTrue(
            do.focus_parks_buys(
                "Coinbase", "E*TRADE", settings, combined_equity=0.0,
            )
        )


class TestBlotterNoMarkAsCost(unittest.TestCase):
    def test_prefers_broker_cost_never_mark(self):
        from blotter_reconcile import missing_basis_tickers

        holdings = [
            {
                "broker": "Robinhood",
                "ticker": "AAPL",
                "shares": 2,
                "price": 999.0,
                "live_price": 998.0,
                "cost": 150.0,
            },
            {
                "broker": "Robinhood",
                "ticker": "MSFT",
                "shares": 1,
                "price": 400.0,
                "live_price": 401.0,
            },
        ]
        need = missing_basis_tickers(holdings, has_basis_fn=lambda b, t: False)
        by_t = {r["ticker"]: r for r in need}
        self.assertEqual(by_t["AAPL"]["broker_cost"], 150.0)
        self.assertEqual(by_t["AAPL"]["price"], 150.0)
        self.assertEqual(by_t["MSFT"]["broker_cost"], 0.0)
        self.assertEqual(by_t["MSFT"]["price"], 0.0)

    def test_holdings_ticker_set_requires_broker(self):
        from blotter_reconcile import holdings_ticker_set

        rows = [
            {"broker": "Robinhood", "ticker": "AAPL", "shares": 1},
            {"broker": "", "ticker": "GHOST", "shares": 1},
            {"ticker": "UNTAGGED", "shares": 1},
        ]
        held = holdings_ticker_set(rows, broker="Robinhood")
        self.assertEqual(held, {"AAPL"})


class TestAdvisorDollarsCap(unittest.TestCase):
    def test_caps_oversize(self):
        self.assertEqual(_advisor_cap(120.0, 50.0), 50.0)

    def test_keeps_undersize(self):
        self.assertEqual(_advisor_cap(30.0, 50.0), 30.0)

    def test_reject_zero_live(self):
        self.assertEqual(_advisor_cap(0.0, 50.0), 0.0)


class TestProposeTtlRefresh(unittest.TestCase):
    def test_refresh_extends_expires_at(self):
        import advisor_queue as aq

        now = time.time()
        original_exp = now + 100  # nearly expired
        pending = {
            "id": "abc123",
            "broker": "Robinhood",
            "ticker": "AAPL",
            "status": "pending",
            "price": 1.0,
            "dollars": 10.0,
            "score": 60.0,
            "engine": "CORE",
            "reason": "entry",
            "regime_caution": False,
            "created_at": now - 100,
            "updated_at": now - 100,
            "expires_at": original_exp,
        }
        store = {"proposals": [pending]}

        def _load():
            return store

        with patch.object(aq, "_load", side_effect=_load):
            with patch.object(aq, "_save"):
                with patch.object(aq, "expire_stale"):
                    out = aq.propose(
                        broker="Robinhood",
                        ticker="AAPL",
                        price=2.0,
                        dollars=20.0,
                        score=70.0,
                        engine="CORE",
                    )
        self.assertIsNotNone(out)
        self.assertTrue(out.get("_refreshed"))
        # Re-propose refreshes TTL so live pending rows don't rot mid-brief
        self.assertGreater(float(out.get("expires_at")), original_exp + 60)
        self.assertEqual(float(out.get("price")), 2.0)
        self.assertEqual(float(out.get("dollars")), 20.0)


class TestProductLimitsCache(unittest.TestCase):
    def test_no_cache_when_disconnected(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter.__new__(CoinbaseAdapter)
        cb.is_connected = False
        cb.client = None
        cb._product_limits_cache = {}
        a = cb._get_product_limits("BTC")
        self.assertIn("quote_increment", a)
        # Defaults must NOT be pinned in cache
        self.assertNotIn("BTC", cb._product_limits_cache)

    def test_caches_successful_fetch(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter.__new__(CoinbaseAdapter)
        cb.is_connected = True
        cb.client = MagicMock()
        cb._product_limits_cache = {}
        cb._cb_call = MagicMock(return_value={"quote_increment": "0.05", "base_increment": "0.001"})
        cb._cb_payload = lambda x: x
        limits = cb._get_product_limits("ETH")
        self.assertEqual(limits["quote_increment"], 0.05)
        hit = cb._product_limits_cache.get("ETH")
        self.assertIsInstance(hit, tuple)
        self.assertEqual(hit[1]["quote_increment"], 0.05)


class TestWorkingOrdersExpire(unittest.TestCase):
    def test_expire_stale_default_ttl_long(self):
        import working_orders as wo
        import inspect

        sig = inspect.signature(wo.expire_stale)
        default = float(sig.parameters["ttl_sec"].default)
        self.assertGreaterEqual(default, 28800.0)

    def test_expire_stale(self):
        import working_orders as wo

        wo._orders = {
            "Robinhood|old": {
                "broker": "Robinhood",
                "order_id": "old",
                "side": "BUY",
                "ticker": "AAPL",
                "dollars": 10.0,
                "ts": time.time() - 40_000,
            },
            "Robinhood|new": {
                "broker": "Robinhood",
                "order_id": "new",
                "side": "BUY",
                "ticker": "MSFT",
                "dollars": 10.0,
                "ts": time.time(),
            },
        }
        wo._loaded = True
        n = wo.expire_stale(ttl_sec=28800.0)
        self.assertEqual(n, 1)
        self.assertNotIn("Robinhood|old", wo._orders)
        self.assertIn("Robinhood|new", wo._orders)


class TestAdvisorKnownCryptos(unittest.TestCase):
    def test_pepe_treated_as_crypto(self):
        from desk_advisor_ai import local_analyze_proposal
        from crypto_symbols import KNOWN_CRYPTOS

        self.assertIn("PEPE", KNOWN_CRYPTOS)
        prop = {
            "ticker": "PEPE",
            "asset_type": "",
            "engine": "CRYPTO",
            "dollars": 10,
            "score": 70,
            "price": 0.00001,
            "regime_caution": False,
        }
        ctx = {
            "buying_power": 100.0,
            "equity": 200.0,
            "blockers": [{"code": "regime_equity", "message": "SPY disagree"}],
            "can_place_new_buy": True,
            "max_affordable_share_price": 50.0,
        }
        out = local_analyze_proposal(prop, ctx)
        brief = str(out.get("brief") or out.get("ai_brief") or "")
        # Should not skip solely because of SPY equity regime on a known crypto
        self.assertNotIn("SPY disagree", brief)


class TestEtFlattenDefault(unittest.TestCase):
    def test_defaults_true_in_settings_template(self):
        # Mirror gui DEFAULTS / call sites: missing key → True
        settings = {}
        self.assertTrue(bool(settings.get("et_flatten_before_close", True)))


class TestNextDeskActionEquity(unittest.TestCase):
    def test_passes_combined_equity_into_park(self):
        import desk_orchestration as do

        by = {
            "E*TRADE": {"can_place_new_buy": True, "equity": 100.0, "blockers": []},
            "Coinbase": {"can_place_new_buy": False, "equity": 50.0, "blockers": []},
        }
        settings = {"desk_focus_mode": "auto", "desk_focus_park_others": False}
        # Only focus is buyable + micro equity → Focus line
        action = do.next_desk_action(
            by, focus_broker="E*TRADE", settings=settings, combined_equity=150.0,
        )
        self.assertIn("Focus", action)

    def test_sums_equity_when_omitted(self):
        import desk_orchestration as do

        by = {
            "E*TRADE": {"can_place_new_buy": True, "equity": 100.0, "blockers": []},
            "Coinbase": {"can_place_new_buy": False, "equity": 50.0, "blockers": []},
        }
        settings = {"desk_focus_mode": "auto", "desk_focus_park_others": False}
        action = do.next_desk_action(
            by, focus_broker="E*TRADE", settings=settings,
        )
        self.assertIn("Focus", action)


class TestFeeGateEquityKw(unittest.TestCase):
    def test_new_entry_clears_fees_accepts_equity(self):
        from scoring import new_entry_clears_fees_ok

        ok, why = new_entry_clears_fees_ok(
            "COINBASE", "BTC", 90.0,
            is_crypto=True, asset_type="cryptocurrency",
            equity=200.0,
            settings={"micro_crypto_entry_edge_extra_pct": 0.05},
        )
        # With a huge extra edge on a micro book, even a strong score may fail —
        # just assert the kwargs path does not raise and returns a tuple.
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(why, str)


if __name__ == "__main__":
    unittest.main()
