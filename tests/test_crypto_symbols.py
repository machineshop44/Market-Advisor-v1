"""Shared crypto symbol set."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_symbols import KNOWN_CRYPTOS, is_known_crypto
from scoring import CRYPTO_TICKERS
from broker import KNOWN_CRYPTOS as BROKER_CRYPTOS, is_known_crypto as broker_is_crypto


def test_avax_is_known():
    assert is_known_crypto("AVAX")
    assert is_known_crypto("avax-usd")
    assert "AVAX" in KNOWN_CRYPTOS
    assert "AVAX" in CRYPTO_TICKERS
    assert "AVAX" in BROKER_CRYPTOS
    assert broker_is_crypto("AVAX")


def test_scoring_matches_canonical():
    assert set(CRYPTO_TICKERS) == set(KNOWN_CRYPTOS)
