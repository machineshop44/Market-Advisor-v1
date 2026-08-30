"""
Trader context — everything a human day trader checks before clicking Buy.

Single snapshot for: buy batch, Advisor AI, monitor API, MCP, Home UI, watchdog.
"""
from __future__ import annotations

import time
from typing import Any

from scoring import (
    describe_posture_for_broker,
    entry_regime_ok,
    is_small_book,
    max_affordable_share_price,
    posture_knobs_for_broker,
    small_book_prefers_breakouts,
)


def _deployable_bp(buying_power: float, settings: dict | None) -> float:
    s = settings or {}
    try:
        util = float(s.get("target_bp_utilization_pct", 88.0))
        if util > 1.0:
            util = util / 100.0
    except (TypeError, ValueError):
        util = 0.88
    util = min(0.99, max(0.50, util))
    return max(0.0, float(buying_power or 0.0) * util)


def build_trader_context(
    broker_name: str,
    *,
    equity: float = 0.0,
    buying_power: float = 0.0,
    settings: dict | None = None,
    session_label: str = "",
    market_label: str = "",
    open_positions: int = 0,
    dd_paused: bool = False,
    dd_reason: str = "",
    dd_mins_left: int = 0,
    armed: bool = False,
    connected: bool = True,
    halted: bool = False,
    reauth_needed: bool = False,
    idle_reason: str = "",
    supports_crypto: bool = True,
    supports_equities: bool = True,
    advisor_gate: bool = False,
    regime_equity_ok: bool | None = None,
    regime_equity_reason: str = "",
    regime_crypto_ok: bool | None = None,
    regime_crypto_reason: str = "",
) -> dict[str, Any]:
    """Human-style desk snapshot for one broker."""
    s = settings or {}
    posture_info = describe_posture_for_broker(broker_name, s, equity=equity)
    posture = str(posture_info.get("effective") or "balanced")
    knobs = posture_knobs_for_broker(broker_name, s, equity=equity)
    max_open = int(knobs.get("max_open_positions") or 8)
    deploy = _deployable_bp(buying_power, s)
    min_ticket = float(s.get("min_trade_dollars", 5.0) or 5.0)
    max_share = max_affordable_share_price(buying_power, utilization=float(
        knobs.get("target_bp_utilization_pct") or s.get("target_bp_utilization_pct") or 88.0
    ))
    small = is_small_book(equity)
    breakouts_first = small_book_prefers_breakouts(equity, s)

    if regime_equity_ok is None and supports_equities:
        regime_equity_ok, regime_equity_reason = entry_regime_ok(
            is_crypto=False, posture=posture,
        )
    if regime_crypto_ok is None and supports_crypto:
        regime_crypto_ok, regime_crypto_reason = entry_regime_ok(
            is_crypto=True, posture=posture,
        )

    engines = {
        "crypto": {
            "enabled": supports_crypto,
            "recommended": supports_crypto and (small or broker_name == "Coinbase"),
        },
        "breakouts": {
            "enabled": supports_equities,
            "recommended": supports_equities and breakouts_first,
        },
        "core": {
            "enabled": supports_equities,
            "recommended": supports_equities and not breakouts_first,
            "parked": breakouts_first,
        },
    }

    blockers: list[dict[str, str]] = []
    if halted:
        blockers.append({"code": "halt", "message": "Panic halt — all brokers disarmed"})
    if not connected:
        blockers.append({"code": "offline", "message": f"{broker_name} disconnected"})
    if reauth_needed:
        blockers.append({"code": "reauth", "message": f"{broker_name} needs re-authentication"})
    if dd_paused:
        msg = dd_reason or "drawdown pause"
        if dd_mins_left > 0:
            msg += f" ({dd_mins_left}m left)"
        blockers.append({"code": "dd_pause", "message": msg})
    if idle_reason:
        blockers.append({"code": "idle", "message": idle_reason})
    if deploy + 1e-9 < min_ticket:
        blockers.append({
            "code": "low_bp",
            "message": f"Deployable ${deploy:.2f} below min ticket ${min_ticket:.2f}",
        })
    if supports_equities and regime_equity_ok is False and regime_equity_reason:
        blockers.append({
            "code": "regime_equity",
            "message": regime_equity_reason.replace("DO NOT BUY (", "").rstrip(")"),
        })
    if supports_crypto and regime_crypto_ok is False and regime_crypto_reason:
        blockers.append({
            "code": "regime_crypto",
            "message": regime_crypto_reason.replace("DO NOT BUY (", "").rstrip(")"),
        })
    if open_positions >= max_open > 0:
        blockers.append({
            "code": "max_positions",
            "message": f"At max open positions ({open_positions}/{max_open})",
        })

    if not armed:
        blockers.append({
            "code": "disarmed",
            "message": f"{broker_name} auto-trader disarmed",
        })

    hard_block = any(
        b["code"] in ("halt", "offline", "reauth", "dd_pause", "low_bp") for b in blockers
    )
    can_buy = (
        connected
        and not halted
        and not reauth_needed
        and not dd_paused
        and not hard_block
    )
    auto_ready = bool(can_buy and armed)

    afford_hint = (
        f"≤{max_share:.0f}/share whole"
        if max_share >= 1
        else f"need ≥${min_ticket:.0f} deployable"
    )
    engine_hint = "Breakouts + Crypto" if breakouts_first else "Breakouts + Core + Crypto"
    if breakouts_first and supports_equities:
        engine_hint += " (CORE parked — small book)"

    summary_parts = [
        f"{broker_name}: ${buying_power:.0f} BP · deploy ~${deploy:.0f}",
        f"afford {afford_hint}",
        f"posture {posture_info.get('label') or posture}",
    ]
    if session_label:
        summary_parts.append(f"session {session_label}")
    if blockers:
        summary_parts.append("block: " + blockers[0]["message"][:80])
    else:
        summary_parts.append(f"engines: {engine_hint}")
    if advisor_gate:
        summary_parts.append("advisor approve ON")

    return {
        "broker": broker_name,
        "at": time.time(),
        "equity": round(float(equity or 0), 2),
        "buying_power": round(float(buying_power or 0), 2),
        "deployable_bp": round(deploy, 2),
        "min_ticket": round(min_ticket, 2),
        "max_affordable_share_price": round(max_share, 2),
        "small_book": small,
        "posture": posture,
        "posture_label": posture_info.get("label") or posture,
        "posture_manual": posture_info.get("manual") or posture,
        "posture_auto_scaled": bool(posture_info.get("auto_scaled")),
        "equity_tier": posture_info.get("equity_tier") or "",
        "session": session_label,
        "market": market_label,
        "open_positions": int(open_positions),
        "max_open_positions": max_open,
        "armed": bool(armed),
        "connected": bool(connected),
        "halted": bool(halted),
        "advisor_gate": bool(advisor_gate),
        "engines": engines,
        "regime": {
            "equity_ok": regime_equity_ok,
            "equity_reason": regime_equity_reason or "",
            "crypto_ok": regime_crypto_ok,
            "crypto_reason": regime_crypto_reason or "",
        },
        "dd_paused": bool(dd_paused),
        "dd_reason": dd_reason or "",
        "dd_mins_left": int(dd_mins_left or 0),
        "blockers": blockers,
        "can_place_new_buy": bool(can_buy),
        "auto_ready": auto_ready,
        "summary": " · ".join(summary_parts)[:500],
    }


def build_from_monitor_status(status: dict | None, broker_name: str) -> dict[str, Any]:
    """Rebuild trader context from /api/status snapshot (remote MCP)."""
    st = status or {}
    bal = (st.get("balances") or {}).get(broker_name) or {}
    bro = (st.get("brokers") or {}).get(broker_name) or {}
    heat = st.get("portfolio_heat") or {}
    combined = heat.get("combined") or {}
    holdings = (st.get("holdings_count") or {}).get(broker_name, 0)
    session = str(st.get("market") or "")
    return build_trader_context(
        broker_name,
        equity=float(bal.get("equity") or 0),
        buying_power=float(bal.get("cash") or 0),
        settings={},
        session_label=session,
        market_label=session,
        open_positions=int(holdings or 0),
        dd_paused=bool(bro.get("dd_pause") or combined.get("dd_paused")),
        dd_reason=str(bro.get("dd_reason") or combined.get("dd_reason") or ""),
        dd_mins_left=int(combined.get("dd_mins_left") or 0),
        armed=bool(bro.get("armed")),
        connected=bool(bro.get("connected", True)),
        halted=bool(st.get("halted")),
        reauth_needed=bool(bro.get("reauth_needed")),
        supports_crypto=broker_name == "Coinbase",
        supports_equities=broker_name != "Coinbase",
        advisor_gate=bool((st.get("advisor") or {}).get("count", 0) >= 0),
    )


def format_trader_digest(by_broker: dict[str, dict]) -> str:
    lines = ["Trader desk context:"]
    for name, ctx in (by_broker or {}).items():
        if not isinstance(ctx, dict):
            continue
        flag = "CAN BUY" if ctx.get("can_place_new_buy") else "BLOCKED"
        lines.append(f"  [{flag}] {ctx.get('summary') or name}")
    return "\n".join(lines)
