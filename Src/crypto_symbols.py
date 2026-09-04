"""
Canonical crypto ticker set shared by scoring, brokers, and GUI.

Keep this list in sync when RH/CB add pairs we care about. Callers may still
treat asset_type containing "crypto" as authoritative for unknown symbols.
"""
from __future__ import annotations

KNOWN_CRYPTOS = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI",
    "FET", "AMP", "ADA", "DOT", "MATIC", "POL", "ATOM", "LTC", "BCH", "XRP", "NEAR", "AAVE",
})


def is_known_crypto(ticker: str | None) -> bool:
    clean = str(ticker or "").upper().replace("-USD", "").strip()
    return clean in KNOWN_CRYPTOS
