"""Mock tests for E*TRADE client helpers, adapter rounding, and capability routing."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestFractionalRounding(unittest.TestCase):
    def test_qty_for_notional(self):
        from etrade_broker import qty_for_notional, round_fractional_qty
        self.assertEqual(round_fractional_qty(1.23456), 1.234)
        self.assertEqual(qty_for_notional(5.0, 10.0), 0.5)
        self.assertEqual(qty_for_notional(5.0, 10.0, allow_fractional=False), 0.0)
        self.assertEqual(qty_for_notional(25.0, 10.0, allow_fractional=False), 2.0)


class TestOrderXml(unittest.TestCase):
    def test_preview_and_place_xml(self):
        from etrade_broker import build_equity_order_xml
        preview = build_equity_order_xml(
            client_order_id="abc123",
            symbol="SPY",
            order_action="BUY",
            quantity=0.5,
            price_type="MARKET",
        )
        self.assertIn("PreviewOrderRequest", preview)
        self.assertIn("<symbol>SPY</symbol>", preview)
        self.assertIn("<quantity>0.5</quantity>", preview)
        place = build_equity_order_xml(
            client_order_id="abc123",
            symbol="SPY",
            order_action="BUY",
            quantity=0.5,
            price_type="MARKET",
            preview_id=42,
        )
        self.assertIn("PlaceOrderRequest", place)
        self.assertIn("<previewId>42</previewId>", place)


class TestAccountPreferTaxable(unittest.TestCase):
    def test_prefer_brokerage_over_ira(self):
        from etrade_broker import prefer_taxable_account, label_account, _is_ira_account
        accounts = [
            {"accountIdKey": "ira1", "accountType": "ROTHIRA", "accountName": "Roth"},
            {"accountIdKey": "brk1", "accountType": "INDIVIDUAL", "accountName": "Brokerage", "accountMode": "CASH"},
        ]
        pref = prefer_taxable_account(accounts)
        self.assertEqual(pref["accountIdKey"], "brk1")
        self.assertTrue(_is_ira_account(accounts[0]))
        self.assertFalse(_is_ira_account(accounts[1]))
        self.assertIn("IRA", label_account(accounts[0]))


class TestLiveTradingGate(unittest.TestCase):
    def test_live_orders_blocked_by_default(self):
        from etrade_broker import ETradeAdapter
        et = ETradeAdapter()
        et.is_connected = True
        et.environment = "live"
        et.live_trading_enabled = False
        et.client = MagicMock()
        et.account_id_key = "x"
        status, spent, oid = et.place_buy_order("SPY", "stock", 100.0, 10.0, 0.0, False)
        self.assertIn("live trading is disabled", status.lower())
        self.assertEqual(spent, 0.0)
        self.assertIsNone(oid)

    def test_crypto_rejected(self):
        from etrade_broker import ETradeAdapter
        et = ETradeAdapter()
        et.is_connected = True
        et.environment = "sandbox"
        et.live_trading_enabled = True
        et.client = MagicMock()
        et.account_id_key = "x"
        status, spent, oid = et.place_buy_order("BTC", "crypto", 100.0, 10.0, 0.0, False)
        self.assertIn("crypto", status.lower())
        self.assertEqual(spent, 0.0)


class TestCapabilities(unittest.TestCase):
    def test_broker_capabilities(self):
        from broker import RobinhoodAdapter, CoinbaseAdapter
        from etrade_broker import ETradeAdapter
        rh = RobinhoodAdapter()
        cb = CoinbaseAdapter()
        et = ETradeAdapter()
        self.assertTrue(rh.supports_equities and rh.supports_crypto)
        self.assertTrue(cb.supports_crypto and not cb.supports_equities)
        self.assertTrue(et.supports_equities and not et.supports_crypto)
        self.assertTrue(et.requires_daily_reauth)
        self.assertEqual(et.min_equity_notional, 5.0)


class TestFeeProfile(unittest.TestCase):
    def test_etrade_stock_profile(self):
        from scoring import _resolve_fee_profile, FEE_PROFILES, _normalize_broker_id
        self.assertIn("ETRADE_STOCK", FEE_PROFILES)
        fees = _resolve_fee_profile("ETRADE", "SPY", "stock")
        self.assertAlmostEqual(fees["ttp_arm"], FEE_PROFILES["ETRADE_STOCK"]["ttp_arm"])
        # Display name from GUI BROKER_NAMES must map the same way
        self.assertEqual(_normalize_broker_id("E*TRADE"), "ETRADE")
        fees_disp = _resolve_fee_profile("E*TRADE", "AAPL", "Equity")
        self.assertEqual(fees_disp, FEE_PROFILES["ETRADE_STOCK"])
        # Commission-free equity rails ≈ RH stock; never Coinbase crypto thresholds
        self.assertEqual(FEE_PROFILES["ETRADE_STOCK"], FEE_PROFILES["ROBINHOOD_STOCK"])
        self.assertNotEqual(FEE_PROFILES["ETRADE_STOCK"]["ttp_arm"], FEE_PROFILES["COINBASE"]["ttp_arm"])
        # Crypto asset_type on E*TRADE still uses stock rails (ET has no crypto)
        fees_cryptoish = _resolve_fee_profile("ETRADE", "BTC", "crypto")
        self.assertEqual(fees_cryptoish, FEE_PROFILES["ETRADE_STOCK"])
        self.assertNotEqual(fees_cryptoish, FEE_PROFILES["COINBASE"])
        self.assertNotEqual(fees_cryptoish, FEE_PROFILES["ROBINHOOD_CRYPTO"])


class TestPreviewIdExtract(unittest.TestCase):
    def test_extract_nested(self):
        from etrade_broker import _extract_preview_id, _extract_order_id
        data = {"PreviewOrderResponse": {"PreviewIds": {"previewId": "99"}}}
        self.assertEqual(_extract_preview_id(data), 99)
        placed = {"PlaceOrderResponse": {"OrderIds": {"orderId": "555"}}}
        self.assertEqual(_extract_order_id(placed), "555")


class TestSchedulerCapabilityLogic(unittest.TestCase):
    """Mirror the director_tick capability gates without spinning up Qt."""

    def test_routing_matrix(self):
        from broker import RobinhoodAdapter, CoinbaseAdapter
        from etrade_broker import ETradeAdapter
        brokers = {
            "Robinhood": RobinhoodAdapter(),
            "Coinbase": CoinbaseAdapter(),
            "E*TRADE": ETradeAdapter(),
        }

        def should_crypto(name):
            return bool(getattr(brokers[name], "supports_crypto", False))

        def should_equity(name):
            return bool(getattr(brokers[name], "supports_equities", False))

        self.assertTrue(should_crypto("Robinhood"))
        self.assertTrue(should_crypto("Coinbase"))
        self.assertFalse(should_crypto("E*TRADE"))
        self.assertTrue(should_equity("Robinhood"))
        self.assertFalse(should_equity("Coinbase"))
        self.assertTrue(should_equity("E*TRADE"))


class TestOAuthUrlHelper(unittest.TestCase):
    def test_authorization_url(self):
        from etrade_client import ETradeClient
        with patch("etrade_client.requests"), patch("etrade_client.OAuth1"):
            # Construct without hitting network — OAuth1 may still be required
            try:
                client = ETradeClient("key", "secret", environment="sandbox")
            except ImportError:
                self.skipTest("requests-oauthlib not installed")
            client.request_token = "tok123"
            url = client.authorization_url()
            self.assertIn("authorize", url)
            self.assertIn("tok123", url)
            self.assertIn("key=", url)

    def test_access_token_puts_verifier_in_oauth_header(self):
        """E*TRADE requires oauth_verifier in Authorization header, not ?oauth_verifier=."""
        from etrade_client import ETradeClient

        try:
            client = ETradeClient("ck", "cs", environment="sandbox")
        except ImportError:
            self.skipTest("requests-oauthlib not installed")

        client.request_token = "rt"
        client.request_token_secret = "rts"
        captured = {}

        def _fake_get(url, auth=None, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["auth"] = auth
            # Materialize Authorization header the same way requests would
            from requests import Request
            prepared = Request("GET", url, auth=auth, params=params).prepare()
            captured["auth_header"] = prepared.headers.get("Authorization", "")
            captured["prepared_url"] = prepared.url
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "oauth_token=at&oauth_token_secret=ats"
            resp.content = resp.text.encode()
            return resp

        client.session.get = _fake_get
        client.get_access_token("L3H4M")
        self.assertNotIn("oauth_verifier", captured.get("url", ""))
        self.assertTrue(captured.get("params") in (None, {}))
        header = captured.get("auth_header") or ""
        if isinstance(header, bytes):
            header = header.decode()
        self.assertIn('oauth_verifier="L3H4M"', header)
        self.assertNotIn("oauth_verifier=", captured.get("prepared_url", ""))


class TestOAuthRequestTokenSurvivesLogin(unittest.TestCase):
    """Authorize then Complete must not lose request token when login() rebuilds the client."""

    def test_verifier_uses_pending_request_token(self):
        from etrade_broker import ETradeAdapter

        et = ETradeAdapter()
        mock_client = MagicMock()
        mock_client.request_token = None
        mock_client.request_token_secret = None
        mock_client.access_token = "at"
        mock_client.access_token_secret = "ats"
        mock_client.get_request_token.return_value = ("rt-abc", "rts-xyz")
        mock_client.authorization_url.return_value = "https://us.etrade.com/e/t/etws/authorize?key=k&token=rt-abc"
        mock_client.list_accounts.return_value = [
            {"accountIdKey": "acct1", "accountType": "INDIVIDUAL", "accountName": "Brokerage"}
        ]
        mock_client.get_balance.return_value = {}

        def _fake_get_access_token(verifier, request_token=None, request_token_secret=None):
            token = request_token or mock_client.request_token
            secret = request_token_secret or mock_client.request_token_secret
            if not token or not secret:
                raise Exception("Missing request token/secret for access_token exchange")
            mock_client.access_token = "at"
            mock_client.access_token_secret = "ats"
            return "at", "ats"

        mock_client.get_access_token.side_effect = _fake_get_access_token

        with patch("etrade_broker.ETradeClient", return_value=mock_client), \
             patch("etrade_broker.store_etrade_secret"), \
             patch("etrade_broker.load_etrade_secret", return_value=""), \
             patch("etrade_broker.clear_pending_oauth"):
            ok, msg = et.login({
                "environment": "sandbox",
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "start_oauth": True,
            })
            self.assertTrue(ok)
            self.assertTrue(str(msg).startswith("AUTH_URL::"))
            self.assertEqual(et._pending_request_token, "rt-abc")
            self.assertEqual(et._pending_request_token_secret, "rts-xyz")

            # Simulate Complete Connection: login() rebuilds client; pending must still apply.
            ok2, msg2 = et.login({
                "environment": "sandbox",
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "verifier": "NZTSI",
            })
            self.assertTrue(ok2, msg2)
            mock_client.get_access_token.assert_called()
            args, kwargs = mock_client.get_access_token.call_args
            self.assertEqual(args[0], "NZTSI")
            self.assertEqual(kwargs.get("request_token"), "rt-abc")
            self.assertEqual(kwargs.get("request_token_secret"), "rts-xyz")
            self.assertIsNone(et._pending_request_token)

    def test_verifier_without_pending_gives_clear_error(self):
        from etrade_broker import ETradeAdapter, _MISSING_REQUEST_TOKEN_MSG

        et = ETradeAdapter()
        mock_client = MagicMock()
        mock_client.request_token = None
        mock_client.request_token_secret = None

        with patch("etrade_broker.ETradeClient", return_value=mock_client), \
             patch("etrade_broker.store_etrade_secret"), \
             patch("etrade_broker.load_etrade_secret", return_value=""), \
             patch("etrade_broker.clear_pending_oauth"):
            ok, msg = et.login({
                "environment": "sandbox",
                "consumer_key": "ck",
                "consumer_secret": "cs",
                "verifier": "OLDCODE",
            })
            self.assertFalse(ok)
            self.assertIn("Authorize in Browser again", msg)
            self.assertEqual(msg, _MISSING_REQUEST_TOKEN_MSG)


if __name__ == "__main__":
    unittest.main()
