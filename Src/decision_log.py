"""
Thin decision-journal helpers for autotrader skips / rotates / scale-ins.

Keeps gui.py from growing more decision-row boilerplate while matching what
analytics.summarize_decisions / Reports already expect.
"""
from __future__ import annotations

from typing import Any, Callable, Optional


def build_decision_row(
    *,
    broker: str,
    ticker: str = "",
    action: str,
    reason: str = "",
    score: float | None = None,
    posture: str | None = None,
    engine: str | None = None,
    is_crypto: bool | None = None,
    regime_ok: bool | None = None,
    regime_why: str | None = None,
    open_count: int | None = None,
    max_open: int | None = None,
    bp: float | None = None,
    dollars: float | None = None,
    extra: dict | None = None,
) -> dict[str, Any]:
    """Build a decision_journal row (caller adds timestamp via journal.log_decision)."""
    row: dict[str, Any] = {
        "broker": broker,
        "ticker": ticker,
        "action": str(action or "").upper(),
        "reason": reason or "",
    }
    if score is not None:
        try:
            row["score"] = float(score)
        except (TypeError, ValueError):
            row["score"] = score
    if posture is not None:
        row["posture"] = posture
    if engine:
        row["engine"] = str(engine)
    if is_crypto is not None:
        row["is_crypto"] = bool(is_crypto)
    if regime_ok is not None:
        row["regime_ok"] = bool(regime_ok)
    if regime_why:
        row["regime_why"] = regime_why
    if open_count is not None:
        try:
            row["open_count"] = int(open_count)
        except (TypeError, ValueError):
            pass
    if max_open is not None:
        try:
            row["max_open"] = int(max_open)
        except (TypeError, ValueError):
            pass
    if bp is not None:
        try:
            row["bp"] = float(bp)
        except (TypeError, ValueError):
            pass
    if dollars is not None:
        try:
            row["dollars"] = float(dollars)
        except (TypeError, ValueError):
            pass
    if extra:
        for k, v in extra.items():
            if v is not None and k not in row:
                row[k] = v
    return row


def emit_decision(log_fn: Callable[..., Any], **kwargs) -> Optional[dict]:
    """
    Call gui-style _log_decision / journal writer with a normalized row.
    log_fn should accept **kwargs (same as TradingApp._log_decision).
    """
    row = build_decision_row(**kwargs)
    try:
        log_fn(**row)
        return row
    except Exception:
        return None


def emit_rotate_skip(
    log_fn: Callable[..., Any],
    *,
    broker: str,
    ticker: str,
    reason: str,
    score: float | None = None,
    posture: str | None = None,
    engine: str | None = None,
    is_crypto: bool | None = None,
    regime_ok: bool | None = None,
    open_count: int | None = None,
    max_open: int | None = None,
    block_reason: str | None = None,
) -> Optional[dict]:
    """Analytics action ROTATE_SKIP — rotate funding rejected."""
    why = str(reason or "no eligible funding name")
    extra = {}
    if block_reason:
        extra["block_reason"] = block_reason
    return emit_decision(
        log_fn,
        broker=broker,
        ticker=ticker,
        action="ROTATE_SKIP",
        reason=why if why.startswith("rotate:") else f"rotate:{why}",
        score=score,
        posture=posture,
        engine=engine,
        is_crypto=is_crypto,
        regime_ok=regime_ok,
        open_count=open_count,
        max_open=max_open,
        extra=extra or None,
    )


def emit_scale_in_skip(
    log_fn: Callable[..., Any],
    *,
    broker: str,
    ticker: str,
    reason: str,
    score: float | None = None,
    posture: str | None = None,
    engine: str | None = None,
    is_crypto: bool | None = None,
    regime_ok: bool | None = None,
    open_count: int | None = None,
    max_open: int | None = None,
) -> Optional[dict]:
    """Analytics action SCALE_IN_SKIP — held name failed scale-in gates."""
    why = str(reason or "blocked")
    return emit_decision(
        log_fn,
        broker=broker,
        ticker=ticker,
        action="SCALE_IN_SKIP",
        reason=why if why.startswith("scale_in:") else f"scale_in:{why}",
        score=score,
        posture=posture,
        engine=engine,
        is_crypto=is_crypto,
        regime_ok=regime_ok,
        open_count=open_count,
        max_open=max_open,
    )


def etrade_sandbox_no_bp(
    *,
    paper_mode: bool,
    connected: bool,
    environment: str,
    buying_power: float,
    min_trade_dollars: float = 5.0,
) -> bool:
    """True when E*TRADE sandbox reports ~$0 BP — buy engines should idle."""
    if paper_mode or not connected:
        return False
    env = str(environment or "sandbox").lower()
    if env != "sandbox":
        return False
    try:
        bp = float(buying_power or 0.0)
    except (TypeError, ValueError):
        bp = 0.0
    try:
        min_d = float(min_trade_dollars or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    return bp < max(0.01, min_d)


def etrade_live_zero_bp(
    *,
    paper_mode: bool,
    connected: bool,
    environment: str,
    buying_power: float,
    min_trade_dollars: float = 5.0,
) -> bool:
    """True when live E*TRADE reports ~$0 BP — park buys (no fake live fills)."""
    if paper_mode or not connected:
        return False
    env = str(environment or "sandbox").lower()
    if env != "live":
        return False
    try:
        bp = float(buying_power or 0.0)
    except (TypeError, ValueError):
        bp = 0.0
    try:
        min_d = float(min_trade_dollars or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    return bp < max(0.01, min_d)


def etrade_path_honesty_note(
    *,
    environment: str,
    live_trading: bool,
    buying_power: float,
    paper_mode: bool = False,
    min_trade_dollars: float = 5.0,
) -> str:
    """
    Home / Reports one-liner: sandbox vs live credentials / BP honesty.
    Never claims live fills when path is paper/sandbox/stub BP.
    """
    if paper_mode:
        return (
            "E*TRADE: app Paper Mode — fills are simulated; "
            "not live broker execution."
        )
    env = str(environment or "sandbox").lower()
    try:
        bp = float(buying_power or 0.0)
    except (TypeError, ValueError):
        bp = 0.0
    try:
        min_d = float(min_trade_dollars or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    low = bp < max(0.01, min_d)
    if env == "sandbox":
        if low:
            return (
                "E*TRADE: sandbox / $0 BP stub — paper path until live credentials "
                "and funded live BP. Buy engines parked; no live fills invented."
            )
        return (
            "E*TRADE: sandbox environment — not a live funded account. "
            "Switch to live + enable live trading only after validation."
        )
    if not live_trading:
        return (
            "E*TRADE: live credentials / read-only (orders OFF). "
            "Enable live order placement in Settings after validation."
            + (" Buying power ~$0 — verify funding before arming." if low else "")
        )
    if low:
        return (
            "E*TRADE: live · orders ON but ~$0 BP — buy engines parked until "
            "funded BP; verify account selection. Stops N/A (TTP only)."
        )
    return (
        "E*TRADE: live · orders ON — real path. Protective stops N/A (software TTP only)."
    )


def buy_engines_idle_reason_for(
    broker_name: str,
    *,
    paper_mode: bool,
    etrade_connected: bool = False,
    etrade_environment: str = "sandbox",
    etrade_buying_power: float = 0.0,
    min_trade_dollars: float = 5.0,
) -> str | None:
    """When non-None, CRYPTO/PENNY/CORE should not run (PORTFOLIO still may)."""
    if broker_name != "E*TRADE":
        return None
    if etrade_sandbox_no_bp(
        paper_mode=paper_mode,
        connected=etrade_connected,
        environment=etrade_environment,
        buying_power=etrade_buying_power,
        min_trade_dollars=min_trade_dollars,
    ):
        return "Sandbox/no BP — buy engines idle"
    if etrade_live_zero_bp(
        paper_mode=paper_mode,
        connected=etrade_connected,
        environment=etrade_environment,
        buying_power=etrade_buying_power,
        min_trade_dollars=min_trade_dollars,
    ):
        return "Live/$0 BP — buy engines idle"
    return None


def emit_idle_skip(
    log_fn: Callable[..., Any],
    *,
    broker: str,
    reason: str,
    engine: str | None = None,
    posture: str | None = None,
    open_count: int | None = None,
    max_open: int | None = None,
    bp: float | None = None,
) -> Optional[dict]:
    """
    Analytics action IDLE_SKIP — buy engines parked (e.g. ET sandbox/$0 BP).
    PORTFOLIO may still run; CRYPTO/PENNY/CORE do not.
    """
    why = str(reason or "buy engines idle")
    return emit_decision(
        log_fn,
        broker=broker,
        ticker="",
        action="IDLE_SKIP",
        reason=why if why.startswith("idle:") else f"idle:{why}",
        posture=posture,
        engine=engine,
        open_count=open_count,
        max_open=max_open,
        bp=bp,
    )
