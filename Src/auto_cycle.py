"""Autotrader cycle helpers — rank trail, scan unpack, idle/coach notes.

Pure functions kept out of gui.py so ops/unit tests can lock trail wording
without importing Qt. Trading behavior stays in gui; this module only formats,
filters, and throttles bookkeeping.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional


def count_buy_signals(results) -> int:
    """Count BUY actions in a scan score result list (excludes DO NOT BUY)."""
    return sum(
        1
        for row in (results or [])
        if len(row) >= 3
        and "BUY" in str(row[2]).upper()
        and "DO NOT BUY" not in str(row[2]).upper()
    )


def unpack_scan_payload(payload) -> tuple[list, list, list, list]:
    """Normalize (opps, results[, buy_candidates[, dropped]]) from bg scan jobs."""
    if not payload:
        return [], [], [], []
    if isinstance(payload, (list, tuple)):
        if len(payload) >= 4:
            return payload[0] or [], payload[1] or [], payload[2] or [], payload[3] or []
        if len(payload) >= 3:
            return payload[0] or [], payload[1] or [], payload[2] or [], []
        if len(payload) == 2:
            return payload[0] or [], payload[1] or [], [], []
    return [], [], [], []


def filter_actionable_ranked(ranked, *, floor: float = -500.0) -> list:
    """Drop names that cannot improve the book (score <= floor after rank)."""
    return [c for c in (ranked or []) if float(c.get("score") or 0.0) > float(floor)]


def format_top_ranked(candidates, *, top_n: int = 3) -> str:
    """e.g. 'ETH(70*SI), BTC(65)' for activity-log rank trails."""
    parts = []
    for c in list(candidates or [])[: max(1, int(top_n))]:
        ticker = c.get("ticker") or "?"
        score = float(c.get("score") or 0.0)
        si = "*SI" if c.get("scale_in") else ""
        parts.append(f"{ticker}({score:.0f}{si})")
    return ", ".join(parts)


def format_ranked_for_book_note(
    broker_name: str,
    actionable: list,
    ranked: list,
    *,
    top_n: int = 3,
) -> str:
    """Ranked N/M buys for book — top: … (matches historical gui trail)."""
    top_src = actionable or ranked or []
    top = format_top_ranked(top_src, top_n=top_n)
    return (
        f"[{broker_name}] Ranked {len(actionable)}/{len(ranked)} buys for book — top: {top}"
    )


def format_ranked_buys_note(
    broker_name: str,
    buy_candidates: list,
    *,
    top_n: int = 3,
) -> str:
    """Simpler trail used after CRYPTO/BREAKOUT/CORE score when candidates exist."""
    top = format_top_ranked(buy_candidates, top_n=top_n)
    return f"[{broker_name}] Ranked {len(buy_candidates or [])} buys — top: {top}"


def empty_after_rank_filter_note(broker_name: str, ranked_n: int) -> str:
    """When rank produced candidates but none passed actionable filter."""
    return (
        f"[{broker_name}] No buys executed after rank "
        f"(0/{ranked_n} actionable — held/scale-in/cluster filtered)"
    )


def should_append_empty_after_rank_filter(notes, ranked) -> bool:
    """True when actionable emptied and notes lack a scale-in / skip outcome already."""
    if not ranked:
        return False
    return not any(
        ("SCALE-IN skipped" in str(n) or "Skipped [" in str(n)) for n in (notes or [])
    )


def throttle_scan_drops(
    store: dict,
    broker,
    engine,
    dropped,
    *,
    now: float,
    cooldown_sec: float = 780,
) -> tuple[list[str], int]:
    """
    Once-per-ticker drop lines. Returns (visible_lines, suppressed_count).
    Mutates store in place. Skips still apply — this only gates Activity Log noise.
    """
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    visible: list[str] = []
    suppressed = 0
    for line in dropped or []:
        text = str(line or "").strip()
        if not text:
            continue
        ticker = text.split("(", 1)[0].strip().upper() or text[:24].upper()
        key = (str(broker), str(engine), ticker)
        prev = float(store.get(key) or 0.0)
        if now - prev < float(cooldown_sec):
            suppressed += 1
            continue
        store[key] = now
        visible.append(text)
    return visible, suppressed


def coach_drop_bucket(line: str) -> str:
    """Classify a scan-drop line into a coach tip bucket."""
    d = str(line or "").lower()
    if "regime" in d or "do not buy (regime" in d:
        return "regime"
    if "fee" in d and ("gate" in d or "clear" in d or "edge" in d):
        return "fee_gate"
    if "afford" in d or "whole share" in d or "bp <" in d:
        return "afford"
    if "frac" in d or "overnight" in d or "session" in d:
        return "session_frac"
    if "missing cost" in d:
        return "missing_cost"
    if "hard stop" in d:
        return "hard_stop"
    if "roi band" in d or "add roi" in d:
        return "roi_band"
    if "drawdown too deep" in d:
        return "drawdown"
    if "scale-in blocked" in d or "scale-in" in d:
        return "scale_in"
    if "cluster" in d or "already held" in d:
        return "held_cluster"
    return "other"


def dominant_coach_drop_bucket(dropped: Iterable) -> str:
    buckets = [coach_drop_bucket(line) for line in (dropped or [])]
    if not buckets:
        return "other"
    return Counter(buckets).most_common(1)[0][0]


def coach_tip_for_scan_drops(broker, engine, dropped) -> tuple[str, str]:
    """
    Match [COACH] wording to the dominant drop reason.
    Returns (throttle_key, tip_text).
    """
    dominant = dominant_coach_drop_bucket(dropped)
    tips = {
        "regime": (
            f"{broker}/{engine}: BUY signals blocked by SPY/BTC regime — "
            f"Growth keeps SPY/BTC gates for small books; Advisor can propose overrides."
        ),
        "fee_gate": (
            f"{broker}/{engine}: candidates failed fee/edge gate — "
            f"expected profit must clear round-trip friction before autosize buys."
        ),
        "afford": (
            f"{broker}/{engine}: signals exist but BP can't afford whole shares "
            f"or min ticket — consolidate stack or wait for a cheaper name."
        ),
        "session_frac": (
            f"{broker}/{engine}: session blocks fractional equity buys on RH — "
            f"wait for REGULAR hours or pick a lower-priced ticker."
        ),
        "missing_cost": (
            f"{broker}/{engine}: scale-in blocked — cost basis unknown on held names. "
            f"Reconnect or wait until RH reports avg cost; TTP/ROI stay gated."
        ),
        "hard_stop": (
            f"{broker}/{engine}: held names past hard stop — scale-in correctly blocked. "
            f"Wait for recovery or a portfolio exit; not a cluster/swap issue."
        ),
        "roi_band": (
            f"{broker}/{engine}: held names outside the add ROI band — scale-in gated "
            f"until price re-enters the band (not a new-entry / swap issue)."
        ),
        "drawdown": (
            f"{broker}/{engine}: held names too deep in drawdown for scale-in. "
            f"Adds stay blocked until ROI recovers into the add window."
        ),
        "scale_in": (
            f"{broker}/{engine}: BUY signals are on held names that failed scale-in gates "
            f"(not fresh book slots). Check ROI band / support / cost basis."
        ),
        "held_cluster": (
            f"{broker}/{engine}: signals exist but none fit the book (held/cluster). "
            f"Opportunity-swap may help on Balanced/Aggressive when BP is tight."
        ),
        "other": (
            f"{broker}/{engine}: BUY signals exist but none were actionable for the book."
        ),
    }
    tip = tips.get(dominant) or tips["other"]
    key = f"{broker}:{engine}:no_actionable:{dominant}"
    return key, tip


def format_no_actionable_scan_note(
    broker: str,
    engine: str,
    raw_buys: int,
    *,
    visible: list[str] | None = None,
    suppressed: int = 0,
    fallback: str = "already held / cluster full",
) -> Optional[str]:
    """
    Activity line when BUY signals exist but 0 are actionable.
    Returns None when all drop lines are still muted (stay quiet).
    """
    visible = list(visible or [])
    if visible:
        detail = ", ".join(visible[:8])
        more = f" (+{len(visible) - 8} more)" if len(visible) > 8 else ""
        mute = f" ({suppressed} muted)" if suppressed else ""
        return (
            f"[{broker}] {engine}: {raw_buys} BUY signal(s) but 0 actionable for book "
            f"— no orders. Dropped: {detail}{more}{mute}"
        )
    if suppressed:
        return None
    return (
        f"[{broker}] {engine}: {raw_buys} BUY signal(s) but 0 actionable for book "
        f"— no orders. Dropped: {fallback}"
    )


def scale_in_skip_note(
    store: dict,
    broker_name: str,
    ticker: str,
    reason: str,
    *,
    now: float,
    throttle_sec: float = 780,
) -> Optional[str]:
    """
    Throttle identical SCALE-IN skip Activity notes.
    Mutates store; returns note to append or None if suppressed this cycle.
    Always journals separately (caller).
    """
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    reason = str(reason or "sizing blocked").strip()
    key = (str(broker_name or "").upper(), str(ticker or "").upper(), reason)
    prev = store.get(key)
    if prev is not None:
        elapsed = now - float(prev.get("t") or 0.0)
        if elapsed < float(throttle_sec):
            prev["n"] = int(prev.get("n") or 1) + 1
            return None
        n = int(prev.get("n") or 1)
        store[key] = {"t": now, "n": 1}
        suffix = f" (repeated {n}× over last {int(elapsed // 60) or 1}m)" if n > 1 else ""
        return f"[{broker_name}] SCALE-IN skipped [{ticker}]: {reason}{suffix}"
    store[key] = {"t": now, "n": 1}
    return f"[{broker_name}] SCALE-IN skipped [{ticker}]: {reason}"


def clear_scale_in_skip_throttle(store: dict, broker_name: str, ticker: str) -> None:
    """Clear throttle keys for a ticker after a successful scale-in."""
    if not isinstance(store, dict):
        return
    prefix = (str(broker_name or "").upper(), str(ticker or "").upper())
    for k in list(store.keys()):
        if isinstance(k, tuple) and len(k) >= 2 and k[:2] == prefix:
            store.pop(k, None)


# --- Buy / rotate / portfolio cycle helpers (next gui.py extract wave) ---

DEFAULT_CRYPTO_TICKERS = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI",
})
try:
    from crypto_symbols import KNOWN_CRYPTOS as _KNOWN
    DEFAULT_CRYPTO_TICKERS = frozenset(DEFAULT_CRYPTO_TICKERS | set(_KNOWN))
except Exception:
    pass

CRYPTO_MOVER_BLOCKLIST = frozenset({
    "USDT", "USDC", "DAI", "USD", "EUR", "GBP", "PYUSD", "EURC",
})


def merge_crypto_scan_universe(
    curated: list[str] | None = None,
    movers: list[str] | None = None,
    *,
    max_movers: int = 8,
) -> list[dict]:
    """
    Curated crypto list plus optional live movers (deduped).
    Returns [{'symbol', 'type'}] with type Crypto or Crypto Mover.
    """
    core = [str(s).upper().replace("-USD", "") for s in (curated or list(DEFAULT_CRYPTO_TICKERS))]
    seen = set()
    out: list[dict] = []
    for sym in core:
        if not sym or sym in seen or sym in CRYPTO_MOVER_BLOCKLIST:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "type": "Crypto"})
    added = 0
    for raw in movers or []:
        if added >= max_movers:
            break
        sym = str(raw or "").upper().replace("-USD", "").strip()
        if not sym or sym in seen or sym in CRYPTO_MOVER_BLOCKLIST:
            continue
        if not sym.isalpha() or not (2 <= len(sym) <= 10):
            continue
        seen.add(sym)
        out.append({"symbol": sym, "type": "Crypto Mover"})
        added += 1
    return out


MOVER_MIN_QUOTE_VOLUME_24H = 5_000_000.0  # USD — thin books spread/slip past the fee model
MOVER_MAX_CHANGE_24H_PCT = 25.0            # beyond this the move is a chase, not a setup


def extract_coinbase_usd_movers(
    products_payload,
    *,
    limit: int = 8,
    min_quote_volume: float = MOVER_MIN_QUOTE_VOLUME_24H,
    max_change_pct: float = MOVER_MAX_CHANGE_24H_PCT,
) -> list[str]:
    """
    Rank Coinbase product dicts by 24h % change (USD quote only), requiring real
    24h quote volume and skipping blow-off moves.
    Accepts list[dict] or {'products': [...]}.
    """
    if isinstance(products_payload, dict):
        products = products_payload.get("products") or products_payload.get("Products") or []
    else:
        products = products_payload or []
    ranked = []
    for p in products:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("product_id") or p.get("id") or "")
        if not pid.endswith("-USD"):
            continue
        base = pid.replace("-USD", "").upper()
        if base in CRYPTO_MOVER_BLOCKLIST or base in DEFAULT_CRYPTO_TICKERS:
            continue
        status = str(p.get("status") or "").lower()
        if status and status not in ("online", "active", ""):
            continue
        try:
            chg = float(
                p.get("price_percentage_change_24h")
                or p.get("price_percentage_change_24H")
                or 0.0
            )
        except (TypeError, ValueError):
            chg = 0.0
        if chg <= 0 or chg > float(max_change_pct):
            continue
        vol_raw = p.get("approximate_quote_24h_volume")
        if vol_raw in (None, ""):
            try:
                vol_raw = float(p.get("volume_24h") or 0) * float(p.get("price") or 0)
            except (TypeError, ValueError):
                vol_raw = 0.0
        try:
            vol = float(vol_raw or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol < float(min_quote_volume):
            continue
        ranked.append((chg, base))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [b for _, b in ranked[: max(0, int(limit))]]


def throttled_buy_skip_note(
    store: dict,
    notes: list,
    broker_name: str,
    kind: str,
    message: str,
    *,
    now: float,
    cooldown_sec: float = 720,
) -> bool:
    """
    Append BP-too-low / rotate-capped notes once per broker+kind per cooldown.
    Mutates store and notes. Returns True when the note was appended.
    """
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = f"{broker_name}:{kind}"
    prev = float(store.get(key) or 0.0)
    if now - prev < float(cooldown_sec):
        return False
    store[key] = now
    notes.append(message)
    return True


def note_frac_buy_defer(
    store: dict,
    notes: list,
    broker_name: str,
    ticker: str,
    reason: str,
    session_label: str,
) -> bool:
    """Once per ticker/session: overnight/whole-share buy defer. Mutates store/notes."""
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = (str(broker_name), str(ticker).upper(), str(session_label or "?"))
    if store.get(key):
        return False
    store[key] = True
    notes.append(f"[{broker_name}] Deferring buy [{ticker}] — {reason}")
    return True


def note_deferred_sell(
    store: dict,
    notes: list,
    broker: str,
    ticker: str,
    reason: str,
    session_label: str,
) -> bool:
    """Log a deferred sell once per ticker/reason for this session label."""
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = (str(broker), str(ticker).upper(), str(reason)[:64])
    if store.get(key) == session_label:
        return False
    store[key] = session_label
    notes.append(f"[{broker}] Deferring [{ticker}] — {reason}")
    return True


def rh_equity_sell_defer_reason(
    ticker,
    shares_val,
    price,
    asset_type,
    session: dict,
    *,
    frac_ext_ineligible=None,
    known_cryptos: Optional[Iterable] = None,
) -> Optional[str]:
    """
    If this RH equity sell cannot succeed in the current session, return a short reason.
    Crypto always returns None (24/7). Pure policy — no Qt.
    """
    cryptos = set(known_cryptos) if known_cryptos is not None else set(DEFAULT_CRYPTO_TICKERS)
    is_crypto = (
        "crypto" in str(asset_type or "").lower()
        or str(ticker).upper() in cryptos
    )
    if is_crypto:
        return None
    if not (session or {}).get("equity_tradeable"):
        return "equity markets closed"
    try:
        shares = float(shares_val or 0)
    except (TypeError, ValueError):
        shares = 0.0
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        px = 0.0
    # Pure fractional (<1): defer overnight. Mixed lots (e.g. 2.99) proceed so the
    # broker can peel the whole-share floor and leave the remainder for ~7am.
    if 0 < shares < 1.0:
        if px > 0 and (shares * px) < 1.00:
            return "fractional notional under $1"
        if not (session or {}).get("fractional_ok"):
            return (
                "fractional equity sells blocked until ~7am ET / regular hours "
                "(after-hours fractionals end ~7:30pm ET)"
            )
        ineligible = frac_ext_ineligible or set()
        if (session or {}).get("label") != "REGULAR" and str(ticker).upper() in ineligible:
            return "ticker not eligible for extended-hours fractionals (waiting for regular open)"
    return None


def format_rotate_skip_note(broker_name: str, why: str) -> str:
    return f"[{broker_name}] [ROTATE] skipped — {why}"


def format_rotate_floor_clear_note(
    broker_name: str,
    candidate_ticker: str,
    *,
    bp: float,
    floor: float,
    label: str = "broker floor",
) -> str:
    """Andrew-visible trail when rotating to clear RH crypto / min ticket floor."""
    return (
        f"[{broker_name}] [ROTATE] freeing BP to clear {label} "
        f"(${float(bp):.2f} → ≥${float(floor):.2f}) for {candidate_ticker}…"
    )


def format_rotate_sell_note(
    broker_name: str,
    fund_ticker: str,
    candidate_ticker: str,
    *,
    roi: float = 0.0,
    fund_score: float = 0.0,
    candidate_score: float = 0.0,
    reason: str = "",
) -> str:
    return (
        f"[{broker_name}] [ROTATE] Sell {fund_ticker} "
        f"(roi {float(roi or 0) * 100:.2f}%, score {float(fund_score or 0):.0f}) "
        f"→ fund {candidate_ticker} "
        f"(score {float(candidate_score or 0):.0f}; {reason})"
    )


def format_rotate_sell_failed_note(broker_name: str, fund_ticker: str, status) -> str:
    return f"[{broker_name}] [ROTATE] Sell {fund_ticker} failed: {status}"


def rotate_would_trip_loss_guard(fund_roi, streak_count, max_losses) -> bool:
    """
    True when rotating out of a losing funder would be the Nth straight loss —
    the guard then pauses buys and the rotate's own candidate never fills.
    ``fund_roi`` is a fraction (−0.0137 = −1.37%).
    """
    try:
        roi = float(fund_roi or 0.0)
        n = int(streak_count or 0)
        cap = max(1, int(max_losses or 3))
    except (TypeError, ValueError):
        return False
    # Same loss line as record_exit_result: fill < avg × 0.995.
    return roi < -0.005 and n + 1 >= cap


def format_rotate_freed_note(
    broker_name: str,
    fund_ticker: str,
    proceeds_txt: str,
    bp_txt: str,
) -> str:
    return (
        f"[{broker_name}] [ROTATE] Freed ~{proceeds_txt} from {fund_ticker}; "
        f"BP now ~{bp_txt}"
    )


def format_scale_in_ok_note(ticker: str, reason: str) -> str:
    return f"SCALE-IN considered [{ticker}]: OK — {reason}"


def holdings_fingerprint(holdings) -> str:
    """Stable holdings signature (broker/ticker/shares) for cycle change detection."""
    parts = []
    safe = [a for a in (holdings or []) if isinstance(a, dict)]
    for a in sorted(safe, key=lambda x: (str(x.get("broker", "")), str(x.get("ticker", "")))):
        parts.append(
            f"{a.get('broker', '')}:{a.get('ticker', '')}:{float(a.get('shares') or 0):.8f}"
        )
    return "|".join(parts)


def partition_portfolio_sells(
    sell_list,
    *,
    broker_name: str,
    session: dict,
    sell_fail_should_skip,
    rh_defer_reason_fn=None,
    note_deferred_fn=None,
) -> tuple[list, list, list]:
    """
    Split scored SELLs into actionable vs deferred.
    Callbacks keep broker/session policy injectable (no Qt).
    Returns (actionable, deferred_tickers, defer_notes).
    """
    actionable: list = []
    deferred: list = []
    notes_tmp: list = []
    for item in sell_list or []:
        row_b = str(item.get("broker") or broker_name)
        tick = item.get("ticker")
        if sell_fail_should_skip(row_b, tick):
            deferred.append(str(tick or "?").upper())
            continue
        if row_b == "Robinhood" and callable(rh_defer_reason_fn):
            defer = rh_defer_reason_fn(
                tick, item.get("shares"), item.get("price"),
                item.get("type"), session,
            )
            if defer:
                deferred.append(str(tick or "?").upper())
                if callable(note_deferred_fn):
                    note_deferred_fn(
                        "Robinhood", tick, defer,
                        (session or {}).get("label") or "UNKNOWN", notes_tmp,
                    )
                continue
        actionable.append(item)
    return actionable, deferred, notes_tmp


def format_portfolio_scored_note(
    broker: str,
    sell_n: int,
    *,
    actionable_n: int = 0,
    deferred: list | None = None,
    first_defer_this_session: bool = False,
) -> Optional[str]:
    """PORTFOLIO scored Activity trail. None = stay quiet (all still deferred)."""
    deferred = list(deferred or [])
    uniq = sorted(set(deferred))
    if uniq:
        if first_defer_this_session:
            return (
                f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s); "
                f"deferring {len(uniq)} until tradable: {', '.join(uniq)}"
            )
        if actionable_n > 0:
            return (
                f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s) "
                f"({actionable_n} actionable, {len(uniq)} still deferred)"
            )
        return None
    return f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s)"


def format_cost_basis_display(cost, *, broker_name: str = "", unknown_label: str = "cost ?") -> str:
    """Portfolio Avg Cost cell: show cost ? when basis unknown (esp. Coinbase)."""
    try:
        c = float(cost or 0.0)
    except (TypeError, ValueError):
        c = 0.0
    if c > 0:
        return f"${c:,.2f}"
    if str(broker_name) == "Coinbase" or c <= 0:
        return unknown_label
    return f"${c:,.2f}"


def count_unknown_cost_holdings(assets, *, broker_name: str | None = None) -> int:
    """Count holdings with no usable avg cost (for Home CB honesty chip)."""
    n = 0
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        if broker_name and str(a.get("broker") or "") != broker_name:
            continue
        try:
            cost = float(a.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        if cost <= 0:
            n += 1
    return n


def etrade_home_env_chip(
    *,
    environment: str,
    live_trading: bool,
    buying_power: float,
    min_trade_dollars: float = 5.0,
) -> tuple[str, str, str]:
    """
    Home E*TRADE env chip: (label, tooltip, color_hex).
    Surfaces stop coverage and live $0 BP honesty (not sandbox-only).
    """
    env = str(environment or "sandbox").lower()
    try:
        bp_f = float(buying_power or 0.0)
    except (TypeError, ValueError):
        bp_f = 0.0
    try:
        min_d = float(min_trade_dollars or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    low_bp = bp_f < max(0.01, min_d)

    if env == "sandbox":
        chip = "Sandbox / no BP" if low_bp else "Sandbox · stops N/A"
        tip = (
            "Sandbox environment — paper/sandbox path until live credentials and funded BP. "
            "Protective stops N/A on E*TRADE (software TTP only). "
            "Repair stops skips E*TRADE by design."
            + (" Sandbox often returns $0 BP — not a live funded account; buy engines parked." if low_bp else "")
        )
        return chip, tip, "#F9A825"

    if live_trading:
        chip = "Live · orders ON · GTC stops"
        tip = (
            "Live environment with live order placement enabled. "
            "Whole-share positions get a GTC broker stop; fractional remainders stay on software TTP."
        )
        col = "#2E7D32"
    else:
        chip = "Live · orders OFF"
        tip = (
            "Live environment but live trading kill-switch is OFF (read-only). "
            "Enable in Settings after validation."
        )
        col = "#EF6C00"

    if low_bp:
        chip = f"{chip} · $0 BP"
        tip += (
            " Buying power is ~$0 — buy engines parked; verify funding / account "
            "selection before arming (no fake live fills)."
        )
        col = "#F9A825"
    return chip, tip, col


def affordability_prefer_whole_shares(
    broker_id,
    *,
    prefer_equity_rth: bool = False,
    settings=None,
) -> bool:
    """
    Whole-share affordability gate for filter_affordable_buy_candidates.
    E*TRADE small-BP books must not pass $300+ names as fractional — preview/order
    path is fragile and pre-RTH fractional uses REGULAR session only.
    """
    bid = str(broker_id or "").upper().replace("*", "").replace(" ", "")
    if "ETRADE" in bid or bid == "ET":
        return True
    prefer_whole = bool((settings or {}).get("prefer_whole_shares_for_stops", True))
    return bool(prefer_equity_rth and prefer_whole)


def filter_affordable_buy_candidates(
    candidates,
    *,
    buying_power,
    equity,
    broker_id,
    settings=None,
    prefer_whole_shares=False,
) -> tuple[list, list[str]]:
    """
    Pre-rank affordability filter — drop names that cannot meet min ticket / whole share.
    Returns (affordable, drop_lines for Activity/coach).
    """
    try:
        from scoring import buy_candidate_affordable
    except Exception:
        return list(candidates or []), []

    affordable: list = []
    dropped: list[str] = []
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        ticker = str(c.get("ticker") or "?")
        asset_type = str(c.get("asset_type") or "")
        is_crypto = (
            "crypto" in asset_type.lower()
            or ticker.upper() in DEFAULT_CRYPTO_TICKERS
        )
        ok, why = buy_candidate_affordable(
            buying_power=buying_power,
            price=c.get("price"),
            is_crypto=is_crypto,
            broker_id=broker_id,
            equity=equity,
            settings=settings,
            scale_in=bool(c.get("scale_in")),
            prefer_whole_shares=prefer_whole_shares,
        )
        if ok:
            affordable.append(c)
        else:
            dropped.append(f"{ticker} (unaffordable: {why})")
    return affordable, dropped


def prefer_fundable_buy_candidates(
    candidates,
    *,
    buying_power,
    equity,
    broker_id,
    broker_name="",
    settings=None,
) -> tuple[list, list[str]]:
    """
    Reorder affordable candidates so names that size to a real ticket come first.
    Demotes high scorers that still fail micro sizing (Activity demotion lines).
    """
    rows = [c for c in (candidates or []) if isinstance(c, dict)]
    if not rows:
        return [], []
    try:
        from scoring import (
            risk_sizing_breakdown,
            get_stop_distance_pct,
            posture_knobs_for_broker,
            effective_min_dollars,
            SMALL_BOOK_EQUITY,
        )
    except Exception:
        return rows, []

    try:
        eq = float(equity or 0.0)
    except (TypeError, ValueError):
        eq = 0.0
    if eq <= 0 or eq >= SMALL_BOOK_EQUITY:
        return rows, []

    knobs = posture_knobs_for_broker(broker_name or broker_id, settings, equity=eq)
    util = float(knobs.get("target_bp_utilization_pct", 88.0) or 88.0)
    if util > 1.0:
        util = util / 100.0
    demotions: list[str] = []
    fundable: list = []
    weak: list = []
    for c in rows:
        ticker = str(c.get("ticker") or "?")
        asset_type = str(c.get("asset_type") or "")
        is_crypto = (
            "crypto" in asset_type.lower()
            or ticker.upper() in DEFAULT_CRYPTO_TICKERS
        )
        min_d = effective_min_dollars(
            broker_id, eq, is_crypto, (settings or {}).get("min_trade_dollars", 5.0)
        )
        alloc_key = "allocation_pct_crypto" if is_crypto else "allocation_pct_stock"
        alloc = float((settings or {}).get(alloc_key, (settings or {}).get("allocation_pct", 8.0))) / 100.0
        stop_d = get_stop_distance_pct(broker_id, ticker=ticker, for_sizing=True)
        detail = risk_sizing_breakdown(
            eq,
            float(buying_power or 0),
            stop_d,
            alloc,
            min_dollars=min_d,
            conviction_score=float(c.get("score") or 0),
            target_bp_utilization=util,
            sizing_focus_slots=int(knobs.get("sizing_focus_slots", 6) or 6),
            soft_name_equity_frac=float(knobs.get("max_single_name_equity_pct", 15.0) or 15.0) / 100.0,
            risk_pct_per_trade=float(knobs.get("risk_pct_per_trade", 0.75) or 0.75),
            max_open_risk_pct=float(knobs.get("max_open_risk_pct", 6.0) or 6.0),
        )
        trade = float(detail.get("trade") or 0)
        if trade + 1e-9 >= min_d and not detail.get("skip_reason"):
            c = dict(c)
            c["_sized_dollars"] = trade
            fundable.append(c)
        else:
            why = detail.get("skip_reason") or "size too small"
            demotions.append(f"{ticker} demoted — micro deploy prefers fundable size ({why})")
            weak.append(c)
    # Keep demoted names after fundable so rank trail still shows them if needed
    return fundable + weak, demotions


def filter_otc_portfolio_items(
    items: list,
    *,
    broker_name: str = "",
    skip_otc: bool = True,
) -> tuple[list, list[str]]:
    """Drop OTC *Q rows from portfolio scoring when skip_otc — stops hopeless re-score spam."""
    if not skip_otc or not items:
        return list(items or []), []
    kept: list = []
    skipped: list[str] = []
    for item in items or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            kept.append(item)
            continue
        ticker = str(item[1] or "").upper()
        if is_bankruptcy_q_ticker(ticker):
            skipped.append(ticker)
            continue
        kept.append(item)
    return kept, skipped


def is_bankruptcy_q_ticker(ticker) -> bool:
    """GOEVQ-style OTC bankruptcy symbol — the Q is a 5th letter; IONQ / TQQQ are live."""
    t = str(ticker or "").upper().replace("-USD", "").strip()
    return len(t) == 5 and t.isalpha() and t.endswith("Q") and t not in DEFAULT_CRYPTO_TICKERS


def classify_locked_holding(holding: dict, *, broker_name: str = "") -> tuple[bool, str]:
    """
    Heuristic OTC/dust/untradeable capital — not deployable for rotates/sizing.
    GOEVQ-style *Q bags and sub-$1 dust are excluded from effective book BP.
    """
    if not isinstance(holding, dict):
        return False, ""
    reason = str(holding.get("locked_reason") or holding.get("untradeable_reason") or "").strip()
    if reason:
        return True, reason
    t = str(holding.get("ticker") or "").upper().replace("-USD", "")
    if not t:
        return False, ""
    asset_type = str(holding.get("asset_type") or holding.get("type") or "")
    is_crypto = (
        "crypto" in asset_type.lower()
        or t in DEFAULT_CRYPTO_TICKERS
        or str(broker_name) == "Coinbase"
    )
    try:
        shares = float(holding.get("shares") or holding.get("qty") or 0.0)
    except (TypeError, ValueError):
        shares = 0.0
    if shares <= 0:
        return False, ""
    try:
        px = float(
            holding.get("price")
            or holding.get("live_price")
            or holding.get("mark")
            or 0.0
        )
    except (TypeError, ValueError):
        px = 0.0
    try:
        val = float(holding.get("value") or 0.0)
    except (TypeError, ValueError):
        val = 0.0
    if val <= 0 and px > 0:
        val = abs(shares * px)

    if not is_crypto and is_bankruptcy_q_ticker(t):
        if px <= 0:
            return True, "OTC/delisted (*Q, no quote)"
        return True, "OTC/delisted (*Q)"

    if px <= 0:
        return True, "no quote"

    if is_crypto and val > 0 and val < 1.0:
        return True, "crypto dust (<$1)"

    if not is_crypto and shares < 1.0 and val > 0 and val < 1.0:
        return True, "dust fractional (<$1)"

    return False, ""


def effective_book_equity(equity, locked_value) -> float:
    """
    Deployable book equity for sizing / rank — total equity minus OTC/dust/no-quote bags.
    BP is unchanged (locked value sits in holdings, not cash).
    """
    try:
        eq = float(equity or 0.0)
    except (TypeError, ValueError):
        eq = 0.0
    try:
        locked = max(0.0, float(locked_value or 0.0))
    except (TypeError, ValueError):
        locked = 0.0
    return max(0.0, eq - locked)


def locked_value_from_holdings(holdings: Iterable, *, broker_name: str | None = None) -> float:
    """Lightweight locked total without broker network calls."""
    return float(locked_capital_summary(holdings).get("total_value") or 0.0)


def locked_capital_summary(holdings: Iterable) -> dict[str, Any]:
    """
    Aggregate untradeable/dust holdings for Home capital checklist.
    Returns {count, total_value, rows: [{broker, ticker, value, reason}, ...]}.
    """
    rows: list[dict] = []
    total = 0.0
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        broker = str(h.get("broker") or h.get("broker_name") or "")
        locked, reason = classify_locked_holding(h, broker_name=broker)
        if not locked:
            continue
        try:
            val = float(h.get("value") or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        if val <= 0:
            try:
                px = float(h.get("price") or h.get("live_price") or 0.0)
                qty = float(h.get("shares") or h.get("qty") or 0.0)
                val = abs(px * qty)
            except (TypeError, ValueError):
                val = 0.0
        total += max(0.0, val)
        rows.append({
            "broker": broker,
            "ticker": str(h.get("ticker") or "?"),
            "value": val,
            "reason": reason,
        })
    return {"count": len(rows), "total_value": total, "rows": rows}


REGIME_IDLE_COACH_SEC = 7200  # ~2 hours of zero BUY signals before regime-idle coach



def regime_idle_coach_tip(
    broker: str,
    engine: str,
    *,
    idle_sec: float,
    regime_reason: str = "",
    dd_paused: bool = False,
) -> tuple[str, str]:
    """
    [COACH] when an armed broker scans for ~1hr with zero raw BUY signals and regime blocks.
    Returns (throttle_key, message).
    """
    idle_min = max(1, int(float(idle_sec or 0) / 60))
    reason = str(regime_reason or "").strip()
    if dd_paused:
        tip = (
            f"{broker}/{engine}: no BUY signals for ~{idle_min}m — drawdown pause is active. "
            f"New buys stay gated until the pause clears or ROI recovers."
        )
        key = f"{broker}:{engine}:regime_idle:dd_pause"
    elif reason:
        short = reason.replace("DO NOT BUY (Regime:", "Regime:").strip(" )")
        tip = (
            f"{broker}/{engine}: no BUY signals for ~{idle_min}m — {short}. "
            f"Idle is expected in downtrends; enable allow_buys_when_regime_blocked in Settings "
            f"only if you accept the risk."
        )
        key = f"{broker}:{engine}:regime_idle:blocked"
    else:
        tip = (
            f"{broker}/{engine}: no BUY signals for ~{idle_min}m — market regime gate may be "
            f"blocking new entries. Check Activity Log for DO NOT BUY (Regime) marks."
        )
        key = f"{broker}:{engine}:regime_idle:unknown"
    return key, tip


DEFAULT_BROKER_NAMES = ("Robinhood", "Coinbase", "E*TRADE")

_BROKER_ID_MAP = {
    "Robinhood": "ROBINHOOD",
    "Coinbase": "COINBASE",
    "E*TRADE": "ETRADE",
}


def holdings_by_broker_from_assets(
    assets,
    broker_names: Iterable[str] | None = None,
) -> dict[str, list]:
    """Group portfolio snapshot rows by broker display name."""
    names = list(broker_names or DEFAULT_BROKER_NAMES)
    holdings_by: dict[str, list] = {n: [] for n in names}
    if not isinstance(assets, list):
        return holdings_by
    for a in assets:
        if not isinstance(a, dict):
            continue
        bname = str(a.get("broker") or a.get("broker_name") or "")
        if bname not in holdings_by:
            for n in names:
                if n.lower() in bname.lower():
                    bname = n
                    break
        if bname in holdings_by:
            holdings_by[bname].append(a)
    return holdings_by


def build_portfolio_heat_rows(
    totals: dict,
    holdings_by: dict,
    session_starts: dict,
    auto_trade_enabled: dict,
    broker_names: Iterable[str] | None = None,
) -> list[dict]:
    """Build per-broker rows for portfolio_heat_snapshot (effective equity net of locked)."""
    names = list(broker_names or DEFAULT_BROKER_NAMES)
    rows: list[dict] = []
    for name in names:
        try:
            p_val = float((totals.get(name) or {}).get("p_val", 0.0) or 0.0)
        except (TypeError, ValueError):
            p_val = 0.0
        try:
            bp = float((totals.get(name) or {}).get("bp", 0.0) or 0.0)
        except (TypeError, ValueError):
            bp = 0.0
        locked = locked_value_from_holdings(holdings_by.get(name) or [])
        eff_eq = effective_book_equity(p_val, locked)
        start = session_starts.get(name) if isinstance(session_starts, dict) else None
        try:
            pl = (p_val - float(start)) if start is not None and float(start) > 0 else 0.0
        except (TypeError, ValueError):
            pl = 0.0
        bid = _BROKER_ID_MAP.get(name, name)
        rows.append({
            "broker": name,
            "broker_id": bid,
            "equity": eff_eq,
            "raw_equity": p_val,
            "locked_value": locked,
            "buying_power": bp,
            "day_pnl": pl,
            "armed": bool((auto_trade_enabled or {}).get(name)),
            "holdings": holdings_by.get(name) or [],
        })
    return rows


def format_portfolio_heat_label(
    snap: dict,
    rows: list,
    *,
    money_fn,
    currency_fn,
) -> str:
    """Home heat strip one-liner from portfolio_heat_snapshot + broker rows."""
    c = snap.get("combined") or {}
    risk_d = float(c.get("open_risk_dollars") or 0)
    risk_p = float(c.get("open_risk_pct") or 0)
    head = float(c.get("bp_headroom") or 0)
    locked_total = sum(float(r.get("locked_value") or 0.0) for r in (rows or []))
    heat_txt = (
        f"Open risk ≈ {currency_fn(risk_d)} ({risk_p:.1f}% equity) · "
        f"BP headroom ≈ {currency_fn(head)} · "
        f"Day P&L {currency_fn(c.get('day_pnl') or 0)}"
    )
    if locked_total > 0.01:
        heat_txt += f" · Locked {money_fn(locked_total)} excluded from sizing"
    return heat_txt


def etrade_bp_label(bp: float, *, environment: str, min_trade_dollars: float = 5.0) -> tuple[str, str]:
    """Buying Power label + tooltip for Home ET row."""
    env = str(environment or "sandbox").lower()
    try:
        bp_f = float(bp or 0.0)
    except (TypeError, ValueError):
        bp_f = 0.0
    try:
        min_d = float(min_trade_dollars or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    low = bp_f < max(0.01, min_d)
    money = f"${bp_f:,.2f}"
    if env == "sandbox" and low:
        return (
            "Buying Power: $0 (sandbox stub)",
            "E*TRADE sandbox often returns $0 buying power even when connected. "
            "This is not a live funded account — do not arm expecting real BP.",
        )
    if env == "live" and low:
        return (
            f"Buying Power: {money}",
            "Live E*TRADE reports ~$0 buying power — verify funding, account picker, "
            "and that the selected account can trade. Stops remain N/A (TTP only).",
        )
    return (f"Buying Power: {money}", "")


# --- Portfolio cycle + monitor payload (gui.py extract wave 3) ---


def sell_status_should_backoff(status: str) -> bool:
    """
    True when a sell result should enter fail-backoff (suppress repeat attempts).
    Covers dust/min skips, overnight frac blocks, quote gaps, and API fails.
    """
    st = str(status or "")
    if "Fail" in st:
        return True
    if "Skipped" not in st:
        return False
    low = st.lower()
    if any(
        k in low
        for k in (
            "dust",
            "min",
            "too small",
            "otc",
            "delisted",
            "cannot trade",
            "no tradeable",
            "overnight",
            "late session",
            "fractional",
            "hours mismatch",
            "market hours mismatch",
            "no rh crypto quote",
            "soft-dead",
            "api gap",
        )
    ):
        return True
    return False


def buy_status_should_backoff(status: str) -> bool:
    """True when a buy error is transient server-side and should cool down per ticker."""
    low = str(status or "").lower()
    needles = (
        "http 500",
        "http 502",
        "http 503",
        "http 429",
        "failed (500)",
        "failed (502)",
        "failed (503)",
        "failed (429)",
        "status_code=500",
        "status_code=502",
        "status_code=503",
        "status_code=429",
        "rate limit",
        "too many requests",
        "temporar",
        "service unavailable",
        # RH crypto empty/None — today's FET/AVAX thrash; do not rotate into same name next pulse
        "empty response",
        "returned empty",
        "auth expired",
        "api unavailable",
        # Extended/RTH flag race — do not hammer LCID every 30s
        "hours mismatch",
        "market hours mismatch",
        "invalid product_id",
        "product_id",
        # RH lists no crypto pair (AMP 10/2–10/5: 94 ranked-then-failed pulses)
        "no rh crypto quote",
    )
    return any(n in low for n in needles)


def buy_order_is_working_unfilled(status: str) -> bool:
    """True when broker accepted the order but it is not confirmed filled yet."""
    st = str(status or "")
    low = st.lower()
    if "skip" in low and "cancel" in low:
        return False
    # Hard fails only — "left working" / pending fill must stay working.
    if "fail" in low and "left working" not in low and "pending fill" not in low:
        return False
    if "filled" in low and "pending fill" not in low:
        return False
    return (
        "pending fill" in low
        or "left working" in low
        or ("submitted" in low and "filled" not in low)
        or "pending/" in low
    )


def sold_qty_from_sell_status(status: str, fallback: float = 0.0) -> float:
    """Parse trailing (qty) from RH/ET sell status; else fallback."""
    import re
    st = str(status or "")
    m = re.search(r"\(([0-9]+(?:\.[0-9]+)?)\)\s*$", st)
    if m:
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            pass
    try:
        return float(fallback or 0)
    except (TypeError, ValueError):
        return 0.0


def sell_status_is_partial_peel(status: str, requested: float, sold: float) -> bool:
    """True when overnight peel sold whole floor but left a fractional remainder."""
    st = str(status or "").lower()
    if "partial peel" in st:
        return True
    try:
        req = float(requested or 0)
        got = float(sold or 0)
    except (TypeError, ValueError):
        return False
    # Require overnight peel shape: sold ≥1 whole shares, fractional remainder <1.
    # Plain short fills (e.g. 3→2) must NOT keep basis as a "peel".
    if req < 1.0 - 1e-9 or got < 1.0 - 1e-9:
        return False
    rem = req - got
    if rem <= 1e-4 or rem >= 1.0 - 1e-9:
        return False
    return abs(got - round(got)) <= 1e-4


def equity_session_size_mult(now_et=None, *, settings=None) -> tuple[float, str]:
    """
    Time-of-day equity ticket curve (joint-audit P1/P2).
      - First 30m RTH (9:30–10:00 ET): half-size
      - Last 30m RTH (15:30–16:00 ET; 12:30–13:00 on half days): no new equity entries
      - Lunch lull (11:30–13:30 ET): lunch_lull_size_mult (0.75)
      - Else: full size
    Crypto callers should use crypto_session_size_mult instead (24/7).
    """
    s = settings or {}
    if not bool(s.get("session_size_curve_enabled", True)):
        return 1.0, ""
    try:
        if now_et is None:
            from zoneinfo import ZoneInfo
            from datetime import datetime

            now_et = datetime.now(ZoneInfo("America/New_York"))
        sod = int(now_et.hour) * 3600 + int(now_et.minute) * 60 + int(now_et.second)
    except Exception:
        return 1.0, ""
    open_s = 9 * 3600 + 30 * 60
    close_s = 16 * 3600
    try:
        from market_calendar import regular_close_hour
        close_s = int(regular_close_hour(now_et.date()) * 3600)
    except Exception:
        pass
    if sod < open_s or sod >= close_s:
        return 1.0, ""  # extended/overnight handled by session gates elsewhere
    if sod < open_s + 30 * 60:
        return 0.5, "open half-size (first 30m RTH)"
    if sod >= close_s - 30 * 60:
        return 0.0, "last 30m RTH — no new equity entries"
    if 11 * 3600 + 30 * 60 <= sod < 13 * 3600 + 30 * 60:
        try:
            lull = float(s.get("lunch_lull_size_mult", 0.75))
        except (TypeError, ValueError):
            lull = 0.75
        lull = max(0.0, min(1.0, lull))
        if lull < 1.0 - 1e-9:
            return lull, f"lunch lull ×{lull:g} (11:30–13:30 ET)"
    return 1.0, ""


def crypto_session_size_mult(now_et=None, *, settings=None) -> tuple[float, str]:
    """
    Crypto ticket curve: thinner books overnight (20:00–02:00 ET) and on weekends
    (Fri 20:00 → Sun 20:00 ET) → crypto_off_hours_size_mult (0.75).
    """
    s = settings or {}
    if not bool(s.get("session_size_curve_enabled", True)):
        return 1.0, ""
    try:
        mult = float(s.get("crypto_off_hours_size_mult", 0.75))
    except (TypeError, ValueError):
        mult = 0.75
    mult = max(0.0, min(1.0, mult))
    if mult >= 1.0 - 1e-9:
        return 1.0, ""
    try:
        if now_et is None:
            from zoneinfo import ZoneInfo
            from datetime import datetime

            now_et = datetime.now(ZoneInfo("America/New_York"))
        wd = int(now_et.weekday())
        h = int(now_et.hour)
    except Exception:
        return 1.0, ""
    weekend = wd == 5 or (wd == 4 and h >= 20) or (wd == 6 and h < 20)
    if weekend:
        return mult, f"crypto weekend ×{mult:g}"
    if h >= 20 or h < 2:
        return mult, f"crypto overnight ×{mult:g} (20:00–02:00 ET)"
    return 1.0, ""


def heartbeat_should_skip(fingerprint, last_fingerprint, secs_since_last_post: float, settings=None) -> bool:
    """
    Skip an hourly heartbeat when broker status, whole-dollar equity/cash, armed set and
    market session match the last post — but never stay quiet past the max-quiet window.
    """
    s = settings or {}
    if not bool(s.get("discord_heartbeat_skip_unchanged", True)):
        return False
    if last_fingerprint is None or fingerprint != last_fingerprint:
        return False
    try:
        quiet_h = float(s.get("discord_heartbeat_max_quiet_hours", 4) or 4)
    except (TypeError, ValueError):
        quiet_h = 4.0
    return float(secs_since_last_post or 0) < max(1.0, quiet_h) * 3600.0 - 60.0


def interleave_tasks_by_broker(queue: list) -> list:
    """
    Round-robin by broker so one venue's PORTFOLIO/XML work cannot starve others
    (lightweight per-broker fairness — joint-audit P1).
    """
    if not queue or len(queue) < 2:
        return list(queue or [])
    buckets: dict = {}
    order = []
    for item in queue:
        try:
            broker = item[0]
        except (TypeError, IndexError):
            broker = "?"
        if broker not in buckets:
            order.append(broker)
            buckets[broker] = []
        buckets[broker].append(item)
    out = []
    while any(buckets.values()):
        for b in order:
            if buckets.get(b):
                out.append(buckets[b].pop(0))
    return out


def locked_broker_entry(raw) -> tuple[float, int]:
    """Normalize locked-by-broker cache values (float legacy or {value,count} dict)."""
    if isinstance(raw, dict):
        try:
            val = float(raw.get("total_value") or raw.get("value") or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        try:
            cnt = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        return max(0.0, val), max(0, cnt)
    try:
        return max(0.0, float(raw or 0.0)), 0
    except (TypeError, ValueError):
        return 0.0, 0


def build_monitor_locked_capital(
    locked_by: dict | None,
    heat_holdings_by_broker: dict | None,
    broker_names: Iterable[str],
    *,
    locked_summary: dict | None = None,
) -> dict[str, Any]:
    """
    Monitor/companion locked_capital payload: {total, count, by_broker: {name: {value, count}}}.
    Accepts legacy locked_by values that are plain floats per broker.
    """
    locked_by = locked_by if isinstance(locked_by, dict) else {}
    heat_holdings_by_broker = heat_holdings_by_broker if isinstance(heat_holdings_by_broker, dict) else {}
    summary_rows = list((locked_summary or {}).get("rows") or [])
    out: dict[str, Any] = {"total": 0.0, "count": 0, "by_broker": {}}
    for name in broker_names:
        val, cnt = locked_broker_entry(locked_by.get(name))
        if val <= 0 and cnt <= 0:
            holdings = heat_holdings_by_broker.get(name) or []
            val = locked_value_from_holdings(holdings, broker_name=name)
            if val > 0 and not cnt:
                cnt = sum(
                    1 for r in summary_rows
                    if isinstance(r, dict) and str(r.get("broker") or "") == name
                )
        out["by_broker"][name] = {"value": val, "count": cnt}
        out["total"] += val
        out["count"] += cnt
    return out


def exit_reason_label(action) -> str:
    """'SELL (Hard Stop: -4.10%)' → 'Hard Stop: -4.10%'; '' when not a sell action."""
    s = str(action or "").strip()
    if "SELL" not in s.upper():
        return ""
    if "(" in s and s.rstrip().endswith(")"):
        return s[s.index("(") + 1:-1].strip()[:120]
    return s[:120]


def portfolio_sells_from_scored(
    assets: Iterable,
    results: Iterable,
    broker: str,
) -> list[dict]:
    """Build sell_list entries from _bg_score_portfolio (row, price, action, ...) tuples."""
    from scoring import sell_fraction_from_action

    sells: list[dict] = []
    asset_list = list(assets or [])
    for row, price, action, asset_type, _err in results or []:
        if row >= len(asset_list):
            continue
        a = asset_list[row]
        if not isinstance(a, dict):
            continue
        ticker = a.get("ticker") or ""
        if not ticker:
            continue
        action_s = str(action or "")
        if "SELL" not in action_s.upper():
            continue
        partial, frac = sell_fraction_from_action(action_s)
        shares_total = float(a.get("shares") or 0)
        if partial and shares_total > 0:
            sell_shares = max(shares_total * frac, shares_total * 0.05)
            sell_shares = min(sell_shares, shares_total)
        else:
            sell_shares = shares_total
        sells.append({
            "broker": broker,
            "ticker": ticker,
            "shares": sell_shares,
            "price": float(price or 0),
            "avg_cost": float(a.get("cost") or 0),
            "type": a.get("type") or asset_type or "",
            "sell_all": not partial,
            "action": action_s,
        })
    return sells


def drop_locked_portfolio_sells(sell_list: Iterable, holdings: Iterable) -> tuple[list, list[str]]:
    """
    Remove locked/untradeable names from portfolio sell candidates before execution.
    Returns (filtered_list, dropped_tickers).
    """
    locked_tickers: set[str] = set()
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        is_locked, _reason = classify_locked_holding(
            h, broker_name=str(h.get("broker") or ""),
        )
        if is_locked:
            locked_tickers.add(str(h.get("ticker") or "").upper())
    if not locked_tickers:
        return list(sell_list or []), []
    kept: list = []
    dropped: list[str] = []
    for item in sell_list or []:
        tick = str(item.get("ticker") or "").upper()
        if tick in locked_tickers:
            dropped.append(tick)
        else:
            kept.append(item)
    return kept, dropped


def overnight_scorecard(
    *,
    protective_health: dict | None = None,
    reauth_needed: dict | None = None,
    session_label: str = "",
    et_equity_count: int = 0,
    et_flatten_enabled: bool = False,
    auto_armed: bool = False,
) -> dict:
    """
    Single trust grade for overnight / app-off exposure.
    Returns {grade, label, risks[], tip}.
    """
    ph = protective_health or {}
    missing = int(ph.get("missing_count") or len(ph.get("missing") or []) or 0)
    expected = int(ph.get("expected") or 0)
    ok_stops = bool(ph.get("ok", True)) and missing == 0
    reauth = reauth_needed or {}
    et_reauth = bool(reauth.get("E*TRADE") or reauth.get("ETRADE"))
    risks: list[str] = []
    score = 100
    if missing > 0:
        risks.append(f"{missing} RH stop(s) missing")
        score -= min(40, 15 * missing)
    if et_equity_count > 0:
        if et_flatten_enabled:
            risks.append(f"ET flatten ON ({et_equity_count} name(s))")
            score -= 5
        else:
            risks.append(f"ET naked overnight ({et_equity_count} equity)")
            score -= min(35, 10 + 5 * min(et_equity_count, 4))
    if et_reauth:
        risks.append("E*TRADE reauth needed")
        score -= 20
    rh_reauth = bool(reauth.get("Robinhood") or reauth.get("ROBINHOOD"))
    if rh_reauth:
        risks.append("Robinhood reauth needed")
        score -= 15
    cb_reauth = bool(reauth.get("Coinbase") or reauth.get("COINBASE"))
    if cb_reauth:
        risks.append("Coinbase reauth needed")
        score -= 10
    sess = str(session_label or "").upper()
    if sess in ("OVERNIGHT", "CLOSED", "WEEKEND", "HOLIDAY") and not auto_armed:
        risks.append("Auto-trader off — software TTP idle")
        score -= 15
    elif sess in ("OVERNIGHT",) and auto_armed:
        risks.append("Overnight session — RH stops matter if app stops")
        score -= 5
    score = max(0, min(100, score))
    if score >= 90:
        grade, label = "A", "Overnight: strong"
    elif score >= 75:
        grade, label = "B", "Overnight: OK"
    elif score >= 60:
        grade, label = "C", "Overnight: gaps"
    else:
        grade, label = "D", "Overnight: exposed"
    if not risks:
        tip = "RH stops healthy; ET flat or empty; companion can reauth."
    elif et_equity_count and not et_flatten_enabled:
        tip = "Enable ET flatten before close or hold with midnight reauth plan."
    elif missing > 0:
        tip = "Run Repair stops or pre-close pass before leaving desk."
    else:
        tip = "Review risks before overnight."
    return {
        "grade": grade,
        "label": label,
        "score": score,
        "risks": risks,
        "tip": tip,
        "stops_ok": ok_stops,
        "et_naked": et_equity_count,
    }


def capital_planner_snapshot(
    *,
    broker: str,
    ticker: str,
    price: float,
    score: float,
    equity: float,
    buying_power: float,
    holdings: list | None = None,
    posture: str = "balanced",
    broker_id: str = "ROBINHOOD",
    settings: dict | None = None,
) -> dict:
    """
    Dry-run BP + optional rotate funder for top radar candidate.
    """
    settings = settings or {}
    out: dict = {
        "broker": broker,
        "ticker": ticker,
        "deployable": 0.0,
        "aim": 0.0,
        "skip_reason": None,
        "rotates_used": 0,
        "rotates_cap": 0,
        "rotate_preview": None,
    }
    try:
        from scoring import (
            risk_sizing_breakdown,
            get_stop_distance_pct,
            pick_rotation_funding,
            opportunity_swap_params,
            rotation_allowed_today,
            rotates_today,
            posture_knobs_for_broker,
        )
        knobs = posture_knobs_for_broker(broker, settings)
        stop_d = get_stop_distance_pct(broker_id, ticker=ticker, for_sizing=True)
        is_crypto = str(ticker or "").upper() in {
            "BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI",
        }
        alloc_key = "allocation_pct_crypto" if is_crypto else "allocation_pct_stock"
        alloc = float(settings.get(alloc_key, settings.get("allocation_pct", 8.0))) / 100.0
        util = float(knobs.get("target_bp_utilization_pct", 88.0))
        if util > 1.0:
            util = util / 100.0
        detail = risk_sizing_breakdown(
            float(equity or 0),
            float(buying_power or 0),
            stop_d,
            alloc,
            min_dollars=float(settings.get("min_trade_dollars", 5.0) or 5.0),
            conviction_score=float(score or 0),
            target_bp_utilization=util,
            sizing_focus_slots=int(knobs.get("sizing_focus_slots", 6) or 6),
            soft_name_equity_frac=float(knobs.get("max_single_name_equity_pct", 15.0) or 15.0) / 100.0,
            risk_pct_per_trade=float(knobs.get("risk_pct_per_trade", 0.75) or 0.75),
            max_open_risk_pct=float(knobs.get("max_open_risk_pct", 6.0) or 6.0),
        )
        out["deployable"] = float(detail.get("deployable") or 0)
        out["aim"] = float(detail.get("trade") or detail.get("aim") or 0)
        out["skip_reason"] = detail.get("skip_reason")
        params = opportunity_swap_params(posture)
        out["rotates_cap"] = int(params.get("max_rotates_per_day") or 0)
        out["rotates_used"] = int(rotates_today(broker_id) or 0)
        ok_day, _ = rotation_allowed_today(broker_id, posture=posture)
        if ok_day and params.get("enabled") and holdings:
            fund = pick_rotation_funding(
                ticker,
                score,
                is_crypto,
                holdings,
                posture=posture,
                broker_id=broker_id,
                need_dollars=float(detail.get("min_dollars") or 5.0) if out["aim"] <= 0 else None,
                current_bp=float(buying_power or 0),
            )
            if fund:
                out["rotate_preview"] = {
                    "funder": fund.get("ticker"),
                    "roi": fund.get("roi"),
                    "value": fund.get("value"),
                    "reason": fund.get("reason"),
                }
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def buy_batch_candidates_pre_ranked(candidates) -> bool:
    """True when scan already attached scores — skip duplicate yfinance re-rank in buy batch."""
    rows = list(candidates or [])
    if not rows:
        return False
    for c in rows:
        if not isinstance(c, dict):
            return False
        if c.get("score") is None and not c.get("scale_in"):
            return False
    return True


def equity_buy_defer_reason(
    ticker,
    projected_shares,
    price,
    asset_type,
    session: dict,
    *,
    frac_ext_ineligible=None,
    known_cryptos: Optional[Iterable] = None,
    broker_name: str = "Robinhood",
) -> Optional[str]:
    """
    Defer equity BUY when the resulting position could not be sold in the
    *current* session (mirror of rh_equity_sell_defer_reason on projected size).

    Robinhood-only: RH blocks fractional equity sells overnight / late extended.
    During REGULAR (fractional_ok) allow fractionals — desk can exit same day.
    Do NOT always simulate overnight; that wrongly blocked RTH buys (e.g. ACHR).

    Whole-share projected size (≥1) is never deferred for session peel reasons —
    overnight peel can sell the floor and leave a fractional remainder.

    E*TRADE supports fractional equities and is not subject to that RH session rule —
    applying it there blocked live ET buys (e.g. SMCI) that sized under 1 share.
    """
    bn = str(broker_name or "").upper().replace("*", "").replace(" ", "")
    if "ETRADE" in bn or bn == "ET":
        return None
    if "COINBASE" in bn or bn == "CB":
        return None
    sess = dict(session or {})
    # Same-day exit OK — do not defer on a hypothetical overnight hold.
    label = str(sess.get("label") or "").upper()
    if sess.get("fractional_ok") or label == "REGULAR":
        return None
    # Whole shares: RH overnight peel can exit the floor; allow the buy.
    try:
        proj = float(projected_shares or 0)
    except (TypeError, ValueError):
        proj = 0.0
    if proj >= 1.0 - 1e-9:
        return None
    why = rh_equity_sell_defer_reason(
        ticker, projected_shares, price, asset_type, sess,
        frac_ext_ineligible=frac_ext_ineligible,
        known_cryptos=known_cryptos,
    )
    if why:
        if label == "OVERNIGHT":
            return f"would be stuck overnight — {why}"
        if label == "EXTENDED":
            return f"late extended exit risk — {why}"
        return f"session exit risk — {why}"
    return None


def order_session_flags(broker_name: str, session: dict | None) -> dict:
    """
    Broker-aware order flags derived from the shared equity session clock.

    RH owns fractional_ok (overnight / late-extended blocks).
    E*TRADE and Coinbase must NOT inherit RH fractional_ok — that bled into
    place_buy/place_sell and blocked ET sub-1 share tickets after 7:30pm ET
    even when equity engines were still in EXTENDED.
    Shared: use_ext / market_hours follow the ET calendar for equity venues.
    """
    sess = dict(session or {})
    bn = str(broker_name or "").upper().replace("*", "").replace(" ", "")
    use_ext = bool(sess.get("use_ext"))
    market_hours = str(sess.get("market_hours") or "regular_hours")
    if "COINBASE" in bn or bn == "CB":
        return {
            "allow_fractional": True,
            "use_ext": False,
            "market_hours": "regular_hours",
        }
    if "ETRADE" in bn or bn == "ET":
        # ET API equity XML is REGULAR-only until supports_extended_hours is proven.
        # Passing use_ext=True while still emitting REGULAR causes:
        #   "Extended hours and market hours mismatch"
        label = str(sess.get("label") or "").upper()
        return {
            "allow_fractional": True,
            "use_ext": False,
            "market_hours": "regular_hours",
            "et_equity_session_ok": label == "REGULAR",
            "session_label": label or "?",
        }
    # Robinhood (default)
    return {
        "allow_fractional": bool(sess.get("fractional_ok")),
        "use_ext": use_ext,
        "market_hours": market_hours,
    }


def etrade_equity_session_ok(session: dict | None = None, *, broker=None) -> tuple[bool, str]:
    """
    E*TRADE equities may only place when the shared clock is REGULAR, unless the
    adapter has proven supports_extended_hours.
    Returns (ok, reason_if_blocked).
    """
    if broker is not None and bool(getattr(broker, "supports_extended_hours", False)):
        return True, ""
    label = ""
    if isinstance(session, dict):
        label = str(session.get("label") or "").upper()
    if label == "REGULAR":
        return True, ""
    if not label:
        return False, "E*TRADE equity session unknown — waiting for REGULAR"
    return (
        False,
        f"E*TRADE equities idle in {label} (API REGULAR-only; "
        f"avoids Extended hours / market hours mismatch)",
    )


def etrade_entry_near_flatten_block(now_et=None, settings=None) -> tuple[bool, str]:
    """
    Block NEW E*TRADE equity entries in the last N minutes before the EOD flatten
    (close − 10m). AMC 10/2: breakout buy 15:17, flattened at market 15:50 for a
    loss + a PDT day-trade slot — a multi-hour breakout edge has no runway in 30m.
    Returns (blocked, reason).
    """
    s = settings or {}
    if not bool(s.get("et_flatten_before_close", True)):
        return False, ""
    try:
        window = float(s.get("et_no_entry_before_flatten_min", 60) or 0)
    except (TypeError, ValueError):
        window = 60.0
    if window <= 0:
        return False, ""
    if now_et is None:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            return False, ""
    try:
        from market_calendar import regular_close_hour
        close_h = float(regular_close_hour(now_et.date()))
    except Exception:
        close_h = 16.0
    flatten_h = close_h - 10.0 / 60.0
    now_h = now_et.hour + now_et.minute / 60.0 + now_et.second / 3600.0
    mins_left = (flatten_h - now_h) * 60.0
    if mins_left > window or now_h >= close_h:
        return False, ""
    return True, (
        f"EOD flatten in ~{max(0, int(mins_left))}m — last-{int(window)}m ET entries must "
        f"pass overnight-hold research"
    )


def etrade_reauth_quiet(now_et=None, settings=None) -> bool:
    """
    True when E*TRADE reauth alerts should stay off Discord: when tomorrow is not a
    session day and today's session is over (Fri after close, Sat). Sunday alerts stay
    on so Andrew can reauth ahead of Monday's open; holidays shift the same way.
    """
    s = settings or {}
    if not bool(s.get("etrade_reauth_quiet_off_days", True)):
        return False
    if now_et is None:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            return False
    from datetime import timedelta

    def _session(d) -> bool:
        try:
            from market_calendar import is_equity_session_day
            return bool(is_equity_session_day(d))
        except Exception:
            return d.weekday() < 5

    today = now_et.date()
    if _session(today + timedelta(days=1)):
        return False
    if not _session(today):
        return True
    return now_et.hour * 60 + now_et.minute >= 16 * 60


def equity_opening_range_block(now_et=None, settings=None) -> tuple[bool, str]:
    """
    Block NEW equity entries in the first N minutes of the regular session.
    AMC 10/5: 09:30 buy, TTP armed on a +3% opening tick and sold −0.92% at 09:33
    (a PDT day trade) before AMC ran +4.6% into midday.
    """
    s = settings or {}
    try:
        window = float(s.get("equity_open_no_entry_min", 15) or 0)
    except (TypeError, ValueError):
        window = 15.0
    if window <= 0:
        return False, ""
    if now_et is None:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            return False, ""
    try:
        from market_calendar import is_equity_session_day
        if not is_equity_session_day(now_et.date()):
            return False, ""
    except Exception:
        pass
    mins = (now_et.hour * 60 + now_et.minute + now_et.second / 60.0) - (9 * 60 + 30)
    if 0 <= mins < window:
        return True, (
            f"opening range — no new equity entries until {int(window)}m after the open "
            f"({int(window - mins)}m left)"
        )
    return False, ""


def et_eod_overnight_decision(
    ticker,
    *,
    price,
    avg_cost,
    has_broker_stop: bool,
    earnings_next: Optional[bool],
    settings=None,
    overnight_intent: bool = False,
) -> tuple[str, str]:
    """
    Selective EOD flatten for E*TRADE equities. Returns ("hold"|"flatten", reason).

    Hold overnight only winners riding a live GTC broker stop with no earnings
    before the next open — flattening winners burns PDT slots and caps gap-ups.
    earnings_next=None (lookup failed) is treated as no earnings: the stop still covers.
    """
    s = settings or {}
    mode = str(s.get("et_eod_flatten_mode", "smart") or "smart").lower()
    if mode == "all":
        return "flatten", "flatten-all mode"
    try:
        px = float(price or 0.0)
        cost = float(avg_cost or 0.0)
    except (TypeError, ValueError):
        px, cost = 0.0, 0.0
    if px <= 0 or cost <= 0:
        return "flatten", "no price/cost basis to judge"
    roi = (px - cost) / cost
    try:
        min_roi = float(s.get("et_overnight_hold_min_roi_pct", 0.5) or 0.0) / 100.0
    except (TypeError, ValueError):
        min_roi = 0.005
    if not has_broker_stop:
        return "flatten", f"no broker stop (ROI {roi*100:+.2f}%)"
    if earnings_next:
        return "flatten", f"earnings before next open (ROI {roi*100:+.2f}%)"
    if overnight_intent:
        # Bought late as a researched overnight hold — give it the night unless it broke down.
        try:
            max_loss = float(s.get("et_overnight_intent_max_loss_pct", 1.0) or 0.0) / 100.0
        except (TypeError, ValueError):
            max_loss = 0.01
        if roi >= -max_loss:
            return "hold", (
                f"ROI {roi*100:+.2f}% on GTC stop — holding as planned "
                f"(overnight-research entry or flatten would burn a PDT day trade)"
            )
        return "flatten", (
            f"overnight entry broke down (ROI {roi*100:+.2f}% < −{max_loss*100:.1f}%)"
        )
    if roi < min_roi:
        return "flatten", f"ROI {roi*100:+.2f}% < hold bar {min_roi*100:.1f}%"
    return "hold", f"winner ROI {roi*100:+.2f}% on GTC stop — riding overnight"


def equity_eod_action_for_holding(
    ticker,
    shares,
    price,
    asset_type,
    *,
    broker_name: str = "Robinhood",
    frac_ext_ineligible=None,
    known_cryptos: Optional[Iterable] = None,
) -> str:
    """
    Pre-close EOD action: keep | flatten | repair.
    ET equities without flatten setting are keep (warn handled in gui).
    """
    bn = str(broker_name or "").upper()
    if "ETRADE" in bn.replace("*", "").replace(" ", ""):
        return "keep"
    overnight = {
        "label": "OVERNIGHT",
        "fractional_ok": False,
        "equity_tradeable": True,
    }
    defer = rh_equity_sell_defer_reason(
        ticker, shares, price, asset_type, overnight,
        frac_ext_ineligible=frac_ext_ineligible,
        known_cryptos=known_cryptos,
    )
    if defer:
        return "flatten"
    if float(shares or 0) >= 1.0:
        return "repair"
    return "keep"


def crypto_held_across_brokers(holdings_by_broker: dict) -> dict:
    """Map crypto ticker -> set of broker names holding it (multi-venue safe)."""
    out: dict = {}
    for broker, rows in (holdings_by_broker or {}).items():
        for h in rows or []:
            if not isinstance(h, dict):
                continue
            t = str(h.get("ticker") or "").upper().replace("-USD", "")
            if not t:
                continue
            at = str(h.get("type") or h.get("asset_type") or "")
            is_c = "crypto" in at.lower() or t in DEFAULT_CRYPTO_TICKERS
            if not is_c:
                continue
            try:
                sh = float(h.get("shares") or 0)
            except (TypeError, ValueError):
                sh = 0.0
            if sh <= 0:
                continue
            out.setdefault(t, set()).add(str(broker))
    return out


def crypto_held_on_other_broker(ticker, broker_name, held_map: dict) -> Optional[str]:
    """Return another broker name if ticker is held elsewhere (multi-venue set-aware)."""
    clean = str(ticker or "").upper().replace("-USD", "")
    owners = (held_map or {}).get(clean)
    if owners is None:
        return None
    if isinstance(owners, str):
        return str(owners) if str(owners) != str(broker_name) else None
    try:
        others = [str(o) for o in owners if str(o) != str(broker_name)]
    except TypeError:
        return None
    return others[0] if others else None


def format_discord_settings_summary(*, webhook_set: bool, level: str) -> str:
    wh = "set" if webhook_set else "not set"
    lvl = str(level or "All Alerts").split("(")[0].strip()
    if "Disabled" in str(level or ""):
        return f"Webhook: {wh} · Disabled"
    return f"Webhook: {wh} · {lvl}"


def format_advisor_settings_summary(
    *,
    advisor_on: bool,
    remote_on: bool,
    ai_on: bool = False,
    ai_ready: bool = False,
    ai_source: str = "local",
    cursor_on: bool = False,
) -> str:
    a = "ON" if advisor_on else "OFF"
    r = "ON" if remote_on else "OFF"
    src = str(ai_source or "local").lower()
    if src == "local":
        brief = "local briefs"
    elif ai_ready:
        brief = f"{src} API"
    else:
        brief = f"{src} (no key)"
    cur = " · Cursor on" if cursor_on else ""
    return f"Advisor: {a} · {brief}{cur} · Remote: {r}"


def format_capital_planner_label(snap: dict, *, money_fn=None) -> str:
    money_fn = money_fn or (lambda x: f"${float(x):.2f}")
    if not snap or not snap.get("ticker"):
        return "Capital: —"
    tick = snap.get("ticker")
    dep = float(snap.get("deployable") or 0)
    aim = float(snap.get("aim") or 0)
    rot = snap.get("rotate_preview") or {}
    parts = [f"Capital [{tick}]: deploy {money_fn(dep)} · aim {money_fn(aim)}"]
    used = int(snap.get("rotates_used") or 0)
    cap = int(snap.get("rotates_cap") or 0)
    if cap:
        parts.append(f"rotates {used}/{cap}")
    if rot.get("funder"):
        parts.append(f"rotate via {rot.get('funder')}")
    elif snap.get("skip_reason"):
        parts.append(str(snap.get("skip_reason"))[:40])
    return " · ".join(parts)
