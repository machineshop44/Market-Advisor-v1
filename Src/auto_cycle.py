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


def extract_coinbase_usd_movers(products_payload, *, limit: int = 8) -> list[str]:
    """
    Rank Coinbase product dicts by 24h % change (USD quote only).
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
        if chg <= 0:
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
    Surfaces Stops N/A · TTP and live $0 BP honesty (not sandbox-only).
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
        chip = "Live · orders ON · stops N/A"
        tip = (
            "Live environment with live order placement enabled. "
            "Protective stops N/A on E*TRADE — software TTP only. Repair skips E*TRADE."
        )
        col = "#2E7D32"
    else:
        chip = "Live · orders OFF · stops N/A"
        tip = (
            "Live environment but live trading kill-switch is OFF (read-only). "
            "Enable in Settings after validation. Protective stops N/A (TTP only)."
        )
        col = "#EF6C00"

    if low_bp:
        chip = chip.replace(" · stops N/A", " · $0 BP · stops N/A")
        if "stops N/A" not in chip:
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

    if t.endswith("Q") and len(t) >= 4:
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


def locked_value_for_broker(summary: dict | None, broker_name: str) -> float:
    """Sum locked notional for one broker from locked_capital_summary rows."""
    if not isinstance(summary, dict):
        return 0.0
    want = str(broker_name or "").strip()
    total = 0.0
    for row in summary.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if want and str(row.get("broker") or "").strip() != want:
            continue
        try:
            total += max(0.0, float(row.get("value") or 0.0))
        except (TypeError, ValueError):
            pass
    return total


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


REGIME_IDLE_COACH_SEC = 3600  # ~1 hour of zero BUY signals


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
    Covers dust/min skips and OTC/delisted API blocks — not transient deferrals.
    """
    st = str(status or "")
    if "Fail" in st:
        return True
    if "Skipped" not in st:
        return False
    low = st.lower()
    if any(k in low for k in ("dust", "min", "too small", "otc", "delisted", "cannot trade", "no tradeable")):
        return True
    return False


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


def portfolio_sells_from_scored(
    assets: Iterable,
    results: Iterable,
    broker: str,
) -> list[dict]:
    """Build sell_list entries from _bg_score_portfolio (row, price, action, ...) tuples."""
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
        if "SELL" not in str(action or "").upper():
            continue
        sells.append({
            "broker": broker,
            "ticker": ticker,
            "shares": float(a.get("shares") or 0),
            "price": float(price or 0),
            "avg_cost": float(a.get("cost") or 0),
            "type": a.get("type") or asset_type or "",
            "sell_all": True,
            "action": str(action or ""),
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
) -> Optional[str]:
    """
    Defer equity BUY when the resulting position could not be sold overnight
    (mirror of rh_equity_sell_defer_reason on projected size).
    """
    overnight = {
        "label": "OVERNIGHT",
        "market_hours": "all_day_hours",
        "use_ext": True,
        "fractional_ok": False,
        "equity_tradeable": True,
    }
    why = rh_equity_sell_defer_reason(
        ticker, projected_shares, price, asset_type, overnight,
        frac_ext_ineligible=frac_ext_ineligible,
        known_cryptos=known_cryptos,
    )
    if why:
        return f"would be stuck overnight — {why}"
    ext = dict(session or {})
    if ext.get("label") == "EXTENDED" and not ext.get("fractional_ok"):
        why2 = rh_equity_sell_defer_reason(
            ticker, projected_shares, price, asset_type, ext,
            frac_ext_ineligible=frac_ext_ineligible,
            known_cryptos=known_cryptos,
        )
        if why2:
            return f"extended session exit risk — {why2}"
    return None


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
    """Map crypto ticker -> broker name holding it."""
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
            out[t] = str(broker)
    return out


def crypto_held_on_other_broker(ticker, broker_name, held_map: dict) -> Optional[str]:
    """Return broker name if ticker is held elsewhere."""
    clean = str(ticker or "").upper().replace("-USD", "")
    owner = (held_map or {}).get(clean)
    if owner and str(owner) != str(broker_name):
        return str(owner)
    return None


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
