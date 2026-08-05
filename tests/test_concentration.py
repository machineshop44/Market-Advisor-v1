"""Crypto book cap vs crypto-only brokers (Coinbase) and multi-asset (Robinhood)."""
import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestCryptoBookCap(unittest.TestCase):
    def test_multi_asset_blocks_over_cap(self):
        """Robinhood-style: crypto may not exceed ~40% of that broker's equity."""
        from scoring import concentration_blocks_buy, MAX_CRYPTO_BOOK_FRAC

        equity = 33.0
        meta = [
            {"ticker": "SHIB", "value": 6.19, "is_crypto": True},
            {"ticker": "BONK", "value": 6.18, "is_crypto": True},
        ]
        # Holdings alone ~37.5%; +$6 proposed → ~56% > 40%
        blocked, reason = concentration_blocks_buy(
            "AVAX",
            held_tickers={"SHIB", "BONK"},
            holdings_meta=meta,
            portfolio_value=equity,
            proposed_dollars=6.0,
            is_crypto=True,
            crypto_only_broker=False,
        )
        self.assertTrue(blocked)
        self.assertIn("crypto book cap", reason)
        self.assertIn(f"{MAX_CRYPTO_BOOK_FRAC * 100:.0f}%", reason)

    def test_crypto_only_skips_book_cap(self):
        """Coinbase: book is crypto — BP util / cash reserve is the deploy rail."""
        from scoring import concentration_blocks_buy

        equity = 33.0
        meta = [
            {"ticker": "SHIB", "value": 6.19, "is_crypto": True},
            {"ticker": "BONK", "value": 6.18, "is_crypto": True},
        ]
        blocked, reason = concentration_blocks_buy(
            "AVAX",
            held_tickers={"SHIB", "BONK"},
            holdings_meta=meta,
            portfolio_value=equity,
            proposed_dollars=6.0,
            is_crypto=True,
            crypto_only_broker=True,
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_crypto_only_still_enforces_cluster_cap(self):
        """Cluster rails still apply on Coinbase (e.g. BTC_BETA)."""
        from scoring import concentration_blocks_buy

        blocked, reason = concentration_blocks_buy(
            "BTC",
            held_tickers={"ETH", "SOL"},
            holdings_meta=[
                {"ticker": "ETH", "value": 10.0, "is_crypto": True},
                {"ticker": "SOL", "value": 10.0, "is_crypto": True},
            ],
            portfolio_value=33.0,
            proposed_dollars=6.0,
            is_crypto=True,
            crypto_only_broker=True,
        )
        self.assertTrue(blocked)
        self.assertIn("cluster BTC_BETA full", reason)

    def test_rank_adjust_skips_crypto_penalty_on_crypto_only(self):
        from scoring import portfolio_buy_rank_adjust

        meta = [
            {"ticker": "SHIB", "value": 12.0, "is_crypto": True},
        ]
        # Multi-asset: high crypto share penalizes more crypto
        rh = portfolio_buy_rank_adjust(
            "AVAX", held_tickers={"SHIB"}, holdings_meta=meta,
            portfolio_value=33.0, is_crypto=True, crypto_only_broker=False,
        )
        cb = portfolio_buy_rank_adjust(
            "AVAX", held_tickers={"SHIB"}, holdings_meta=meta,
            portfolio_value=33.0, is_crypto=True, crypto_only_broker=True,
        )
        self.assertLess(rh, cb)


if __name__ == "__main__":
    unittest.main()
