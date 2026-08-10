"""
Cost-basis seeding helpers — honesty-first for crypto / Coinbase bags.

Priority when broker avg is missing or dust:
  1. Broker avg when sane vs mark (RH / Coinbase portfolio breakdown)
  2. Journal inventory VWAP (confirmed buys − sells)
  3. Tracked / in-memory cache (fills since app start)
  4. Optional last-known persisted cache

Never invent live mark as avg cost for ROI / TTP / scale-in.
"""
from __future__ import annotations

from typing import Any

# Same dust threshold as scoring / Discord ROI honesty
COST_BASIS_DUST_FRAC = 0.01


def normalize_ticker(ticker: str | None) -> str:
    """ETH-USD / eth → ETH for journal ↔ holdings matching."""
    return str(ticker or "").replace("-USD", "").strip().upper()


def cost_is_dust(cost: float, mark: float, *, frac: float = COST_BASIS_DUST_FRAC) -> bool:
    try:
        c = float(cost or 0.0)
        m = float(mark or 0.0)
    except (TypeError, ValueError):
        return True
    if c <= 0:
        return True
    if m > 0 and c < m * float(frac):
        return True
    return False


def usable_cost(cost: float, mark: float = 0.0) -> float:
    """Return cost if sane, else 0.0."""
    try:
        c = float(cost or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if cost_is_dust(c, mark):
        return 0.0
    return c


def _fill_qty(row: dict) -> float:
    try:
        q = float(row.get("qty") or 0.0)
        if q > 0:
            return abs(q)
    except (TypeError, ValueError):
        pass
    try:
        px = float(row.get("price") or row.get("fill_price") or 0.0)
        dollars = float(row.get("dollars") or 0.0)
        if px > 0 and dollars:
            return abs(dollars) / px
    except (TypeError, ValueError):
        pass
    return 0.0


def _fill_price(row: dict) -> float:
    for key in ("fill_price", "price"):
        try:
            px = float(row.get(key) or 0.0)
            if px > 0:
                return px
        except (TypeError, ValueError):
            continue
    try:
        dollars = abs(float(row.get("dollars") or 0.0))
        qty = _fill_qty(row)
        if dollars > 0 and qty > 0:
            return dollars / qty
    except (TypeError, ValueError):
        pass
    return 0.0


def _is_confirmed_fill(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("confirmed") is False:
        return False
    status = str(row.get("status") or "")
    if "Fail" in status or "Skipped" in status or "Pending" in status:
        return False
    if row.get("confirmed") is True:
        return True
    return "Filled" in status or "[PAPER]" in status


def inventory_vwap_from_journal(
    rows: list[dict] | None,
    broker: str,
    ticker: str,
) -> float:
    """
    Running inventory average cost from confirmed journal fills for broker+ticker.
    Buys add; sells reduce share count (avg cost of remainder unchanged).
    Returns 0.0 when no residual long inventory with a known basis.
    """
    bwant = str(broker or "").strip()
    twant = normalize_ticker(ticker)
    if not bwant or not twant:
        return 0.0

    shares = 0.0
    cost_sum = 0.0
    for row in rows or []:
        if not _is_confirmed_fill(row):
            continue
        if str(row.get("broker") or "").strip() != bwant:
            continue
        t = normalize_ticker(row.get("ticker"))
        if t != twant:
            continue
        side = str(row.get("side") or "").upper()
        qty = _fill_qty(row)
        px = _fill_price(row)
        if qty <= 0 or px <= 0:
            continue
        if side == "BUY":
            cost_sum += qty * px
            shares += qty
        elif side == "SELL":
            if shares <= 1e-12:
                shares = 0.0
                cost_sum = 0.0
                continue
            take = min(qty, shares)
            avg = cost_sum / shares if shares > 0 else 0.0
            shares -= take
            cost_sum = avg * shares if shares > 1e-12 else 0.0
            if shares <= 1e-12:
                shares = 0.0
                cost_sum = 0.0

    if shares <= 1e-12 or cost_sum <= 0:
        return 0.0
    return cost_sum / shares


def resolve_holding_cost(
    *,
    broker_cost: float = 0.0,
    tracked_cache: float = 0.0,
    journal_vwap: float = 0.0,
    last_known: float = 0.0,
    mark: float = 0.0,
) -> tuple[float, str]:
    """
    Pick a usable avg cost + source label.

    When broker avg is sane → use it (official RH / Coinbase portfolio entry).
    When missing/dust:
      journal inventory VWAP → tracked buys cache → last-known → unknown.
    Never invent live mark as cost.
    """
    tracked = usable_cost(tracked_cache, mark)
    jvwap = usable_cost(journal_vwap, mark)
    broker = usable_cost(broker_cost, mark)
    known = usable_cost(last_known, mark)

    if broker > 0:
        return broker, "broker"
    if jvwap > 0:
        return jvwap, "journal_vwap"
    if tracked > 0:
        return tracked, "tracked"
    if known > 0:
        return known, "last_known"
    return 0.0, "unknown"


def parse_manual_basis_lines(text: str) -> dict[str, dict[str, float]]:
    """
    Parse Settings paste lines into {broker: {TICKER: cost}}.

    Accepted forms (one per line, # comments ok):
      Coinbase:ETH=1920.5
      Robinhood SHIB 0.000012
      CB DOGE = 0.08
    """
    aliases = {
        "CB": "Coinbase",
        "COINBASE": "Coinbase",
        "RH": "Robinhood",
        "ROBINHOOD": "Robinhood",
        "ET": "E*TRADE",
        "ETRADE": "E*TRADE",
        "E*TRADE": "E*TRADE",
    }
    out: dict[str, dict[str, float]] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.replace("=", " ").replace(":", " ")
        parts = [p for p in line.replace(",", " ").split() if p]
        if len(parts) < 3:
            continue
        broker_key = parts[0].strip().upper()
        broker = aliases.get(broker_key, parts[0].strip())
        if broker_key in aliases:
            broker = aliases[broker_key]
        # Allow "E*TRADE" split oddly — if first token is E and second looks like trade, skip
        ticker = normalize_ticker(parts[1])
        try:
            cost = float(parts[2].replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            continue
        if not ticker or cost <= 0:
            continue
        out.setdefault(broker, {})[ticker] = cost
    return out


def cache_lookup(cache: dict | None, broker: str, ticker: str) -> float:
    """Look up avg cost with ticker normalization."""
    if not isinstance(cache, dict):
        return 0.0
    bucket = cache.get(broker)
    if not isinstance(bucket, dict):
        return 0.0
    tu = normalize_ticker(ticker)
    for key in (tu, ticker, str(ticker or "").upper(), str(ticker or "")):
        try:
            v = float(bucket.get(key) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    return 0.0


def normalize_cache_map(raw: Any) -> dict[str, dict[str, float]]:
    """Coerce settings / disk payload into {broker: {ticker: float}}."""
    out: dict[str, dict[str, float]] = {}
    if not isinstance(raw, dict):
        return out
    for broker, tickers in raw.items():
        if not isinstance(tickers, dict):
            continue
        b = str(broker)
        bucket: dict[str, float] = {}
        for t, v in tickers.items():
            try:
                c = float(v)
            except (TypeError, ValueError):
                continue
            if c > 0:
                nt = str(t).replace("-USD", "").upper()
                bucket[nt] = c
                bucket[str(t).upper()] = c
                bucket[str(t)] = c
        if bucket:
            out[b] = bucket
    return out


def cache_to_persistable(cache: dict | None) -> dict[str, dict[str, float]]:
    """Strip zero/invalid and uppercase tickers for settings JSON."""
    out: dict[str, dict[str, float]] = {}
    for broker, tickers in (cache or {}).items():
        if not isinstance(tickers, dict):
            continue
        clean: dict[str, float] = {}
        for t, v in tickers.items():
            try:
                c = float(v)
            except (TypeError, ValueError):
                continue
            if c > 0:
                clean[str(t).upper()] = round(c, 8)
        if clean:
            out[str(broker)] = clean
    return out
