"""
Boot / refresh blotter reconcile — broker holdings vs local protective + cost basis.

Pure helpers (no Qt). GUI applies the diffs and logs a throttled summary.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def _norm_ticker(t: Any) -> str:
    return str(t or "").replace("-USD", "").upper().strip()


def holdings_ticker_set(holdings: Iterable[dict] | None, *, broker: str = "") -> set[str]:
    out: set[str] = set()
    bn = str(broker or "")
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        if bn and str(h.get("broker") or h.get("broker_name") or "") not in ("", bn):
            if str(h.get("broker") or "") != bn:
                continue
        t = _norm_ticker(h.get("ticker"))
        if not t:
            continue
        try:
            qty = float(h.get("shares") or h.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 0:
            out.add(t)
    return out


def protective_stale_tickers(
    held: set[str],
    protective_rows: Iterable | None,
    *,
    broker_id: str = "",
) -> list[str]:
    """
    protective_rows: iterable of (broker_id, ticker) or (broker_id, ticker, info).
    Return tickers tracked for this broker_id that are no longer held.
    """
    bid = str(broker_id or "").upper().replace("*", "")
    stale: list[str] = []
    for row in protective_rows or []:
        if not row:
            continue
        try:
            b = row[0]
            t = row[1]
        except (TypeError, IndexError, KeyError):
            continue
        b_n = str(b or "").upper().replace("*", "")
        if bid:
            same = b_n == bid or (
                "ETRADE" in b_n and "ETRADE" in bid
            )
            if not same:
                continue
        tu = _norm_ticker(t)
        if tu and tu not in held:
            stale.append(tu)
    return sorted(set(stale))


def ghost_local_tickers(held: set[str], local_tickers: Iterable[str] | None) -> list[str]:
    """Local book symbols not present at the broker (ghosts to drop)."""
    ghosts = []
    for t in local_tickers or []:
        tu = _norm_ticker(t)
        if tu and tu not in held:
            ghosts.append(tu)
    return sorted(set(ghosts))


def missing_basis_tickers(
    holdings: Iterable[dict] | None,
    *,
    has_basis_fn,
    broker: str = "",
) -> list[dict]:
    """
    Holdings lacking cost basis. has_basis_fn(broker, ticker) -> bool.
    Returns [{ticker, shares, price}, ...].
    """
    need: list[dict] = []
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        b = str(h.get("broker") or broker or "")
        t = _norm_ticker(h.get("ticker"))
        if not t:
            continue
        try:
            if has_basis_fn(b, t):
                continue
        except Exception:
            pass
        try:
            shares = float(h.get("shares") or h.get("qty") or 0)
        except (TypeError, ValueError):
            shares = 0.0
        if shares <= 0:
            continue
        try:
            px = float(h.get("price") or h.get("live_price") or h.get("cost") or 0)
        except (TypeError, ValueError):
            px = 0.0
        need.append({"broker": b, "ticker": t, "shares": shares, "price": px})
    return need


def format_reconcile_summary(
    broker: str,
    *,
    ghosts: list[str] | None = None,
    stale_stops: list[str] | None = None,
    seeded_basis: list[str] | None = None,
) -> Optional[str]:
    g = list(ghosts or [])
    s = list(stale_stops or [])
    b = list(seeded_basis or [])
    if not (g or s or b):
        return None
    parts = []
    if g:
        parts.append(f"dropped {len(g)} ghost(s) ({', '.join(g[:6])}{'…' if len(g) > 6 else ''})")
    if s:
        parts.append(f"cleared {len(s)} stale stop(s) ({', '.join(s[:6])}{'…' if len(s) > 6 else ''})")
    if b:
        parts.append(f"seeded basis {len(b)} ({', '.join(b[:6])}{'…' if len(b) > 6 else ''})")
    return f"[{broker}] Blotter reconcile: " + "; ".join(parts)


def small_book_focus_park_active(
    combined_equity: float,
    settings: dict | None = None,
) -> bool:
    """True when micro combined equity should exclusive-park non-focus buys."""
    s = settings or {}
    try:
        under = float(s.get("desk_focus_park_others_auto_under", 500.0) or 500.0)
    except (TypeError, ValueError):
        under = 500.0
    if under <= 0:
        return False
    try:
        eq = float(combined_equity or 0)
    except (TypeError, ValueError):
        eq = 0.0
    # Manual exclusive park always wins
    if bool(s.get("desk_focus_park_others", False)):
        return True
    return eq > 0 and eq < under
