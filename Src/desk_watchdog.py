"""
Desk watchdog — scan monitor status + log for snags before they become crashes.

Used by /api/agent/snags, Cursor MCP, and optional Discord early warnings.
"""
from __future__ import annotations

import re
import time
from typing import Any

SEV_CRITICAL = "critical"
SEV_WARN = "warn"
SEV_INFO = "info"

_LOG_PATTERNS: list[tuple[str, str, str, str]] = [
    (r"thread error", SEV_CRITICAL, "thread_error", "Background task failed — check activity log"),
    (r"buy batch error|buy execution error", SEV_CRITICAL, "buy_batch_error", "Buy batch crashed or rejected badly"),
    (r"cycle stall", SEV_WARN, "cycle_stall", "Trading cycle hung and was force-unlocked"),
    # ranked_then_stop handled specially in scan_log_snags (needs Buy batch done check)
    (r"0 actionable", SEV_INFO, "zero_actionable", "Signals found but none affordable for book size"),
    (r"unaffordable", SEV_INFO, "unaffordable", "Ticker unaffordable at current buying power"),
    (r"pausing new buys", SEV_WARN, "dd_pause_log", "Drawdown guard paused new buys"),
    (r"reauth|verifier required|token expired", SEV_CRITICAL, "reauth_log", "Broker needs manual re-authentication"),
    (r"publish_monitor_status failed", SEV_WARN, "monitor_publish_fail", "Companion status push failing"),
    (r"ui build error", SEV_CRITICAL, "ui_build_error", "UI thread error — app may be unstable"),
]


def _snag(
    code: str,
    severity: str,
    message: str,
    *,
    broker: str = "",
    hint: str = "",
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "broker": broker or "",
        "message": message[:320],
        "hint": hint[:240],
    }


def _ranked_then_stop_still_open(log_lines: list[str], ranked_line: str) -> bool:
    """True only if Ranked appears without a later Buy batch done/start for that broker."""
    try:
        idx = log_lines.index(ranked_line)
    except ValueError:
        return True
    broker = ""
    for b in ("Robinhood", "Coinbase", "E*TRADE"):
        if f"[{b}]" in ranked_line:
            broker = b
            break
    after = log_lines[idx + 1 :]
    for ln in after:
        low = ln.lower()
        if broker and f"[{broker}]" not in ln and broker.lower() not in low:
            # other brokers' lines don't clear this ranked snag
            if "buy batch" in low:
                continue
        if "buy batch done" in low or "buy batch start" in low:
            if not broker or f"[{broker}]" in ln:
                return False
        if "buy batch error" in low and (not broker or f"[{broker}]" in ln):
            return True
    return True


def scan_log_snags(log_lines: list | None, *, seen_codes: set | None = None) -> list[dict]:
    seen = seen_codes or set()
    out: list[dict] = []
    lines = [str(x) for x in (log_lines or []) if x][-40:]
    for ln in reversed(lines):
        low = ln.lower()
        if "ranked_then_stop" not in seen and re.search(r"ranked \d+ buys", low):
            if _ranked_then_stop_still_open(lines, ln):
                broker = ""
                for b in ("Robinhood", "Coinbase", "E*TRADE"):
                    if f"[{b}]" in ln:
                        broker = b
                        break
                out.append(_snag(
                    "ranked_then_stop",
                    SEV_WARN,
                    "Buys ranked — watch for silent stop after this line",
                    broker=broker,
                    hint=ln[-180:],
                ))
                seen.add("ranked_then_stop")
            else:
                seen.add("ranked_then_stop")
        for pattern, sev, code, msg in _LOG_PATTERNS:
            if code in seen:
                continue
            if re.search(pattern, low):
                broker = ""
                for b in ("Robinhood", "Coinbase", "E*TRADE", "E*TRADE"):
                    if f"[{b}]" in ln or f"[{b.upper()}]" in ln.upper():
                        broker = b.replace("E*TRADE", "E*TRADE")
                        break
                if "[E*TRADE]" in ln:
                    broker = "E*TRADE"
                out.append(_snag(code, sev, msg, broker=broker, hint=ln[-180:]))
                seen.add(code)
                break
    return out


def scan_status_snags(status: dict | None) -> list[dict]:
    s = status or {}
    out: list[dict] = []

    if s.get("halted"):
        out.append(_snag(
            "panic_halt", SEV_CRITICAL,
            "Panic halt is ON — all brokers disarmed.",
            hint="Clear halt and re-arm when ready.",
        ))

    runtime = s.get("desk_runtime") or {}
    stall = int(runtime.get("stall_sec") or 0)
    if runtime.get("queue_processing") and stall >= 120:
        out.append(_snag(
            "cycle_slow", SEV_WARN,
            f"Cycle running {stall}s on {runtime.get('cycle_broker') or '?'} — may be stuck.",
            broker=str(runtime.get("cycle_broker") or ""),
            hint="Watchdog force-unlocks at 180s.",
        ))

    for name, b in (s.get("brokers") or {}).items():
        if not isinstance(b, dict):
            continue
        armed = bool(b.get("armed"))
        if b.get("reauth_needed"):
            out.append(_snag(
                "reauth", SEV_CRITICAL,
                f"{name} needs re-authentication.",
                broker=name,
                hint="Open Settings and reconnect the broker.",
            ))
        if not b.get("connected") and armed:
            out.append(_snag(
                "armed_offline", SEV_CRITICAL,
                f"{name} is armed but disconnected.",
                broker=name,
            ))
        if b.get("dd_pause"):
            reason = str(b.get("dd_reason") or "drawdown")
            out.append(_snag(
                "dd_pause", SEV_WARN,
                f"{name} drawdown pause — new buys blocked ({reason}).",
                broker=name,
            ))
        if b.get("buy_engines_parked") or b.get("sandbox_no_bp"):
            out.append(_snag(
                "zero_bp", SEV_WARN,
                f"{name} buy engines parked — insufficient buying power.",
                broker=name,
                hint="Fund account or wait for sells to free cash.",
            ))
        elif b.get("live_zero_bp"):
            out.append(_snag(
                "live_zero_bp", SEV_WARN,
                f"{name} live account shows ~$0 buying power.",
                broker=name,
            ))

    et = s.get("etrade") or {}
    if et.get("reauth_needed"):
        out.append(_snag("etrade_reauth", SEV_CRITICAL, "E*TRADE needs OAuth reauth.", broker="E*TRADE"))
    naked = int(et.get("naked_equity") or et.get("et_naked") or 0)
    if naked > 0:
        out.append(_snag(
            "etrade_naked", SEV_WARN,
            f"E*TRADE has {naked} equity position(s) without protective stops.",
            broker="E*TRADE",
        ))

    ph = s.get("protective_health") or {}
    missing = int(ph.get("missing_count") or 0)
    if missing > 0:
        out.append(_snag(
            "missing_stops", SEV_WARN,
            f"{missing} open position(s) missing protective stops.",
            hint=str(ph.get("detail") or "")[:120],
        ))

    heat = s.get("portfolio_heat") or {}
    if heat.get("dd_paused"):
        out.append(_snag(
            "portfolio_dd_pause", SEV_WARN,
            "Portfolio heat shows drawdown pause active.",
        ))

    sg = s.get("shadow_guard") or {}
    if sg.get("active") or sg.get("tripped"):
        out.append(_snag(
            "shadow_guard", SEV_INFO,
            "Shadow guardrail is limiting aggressive entries.",
        ))

    adv = s.get("advisor") or {}
    pending = adv.get("pending") or []
    if len(pending) >= 3:
        out.append(_snag(
            "advisor_backlog", SEV_INFO,
            f"{len(pending)} Advisor proposals waiting — approve or reject.",
        ))
    for p in pending:
        if p.get("ai_verdict") == "skip" and str(p.get("status") or "pending") == "pending":
            out.append(_snag(
                "advisor_ai_skip_pending",
                SEV_INFO,
                f"AI says skip but {p.get('ticker')} still pending on {p.get('broker')}.",
                broker=str(p.get("broker") or ""),
                hint="Enable auto-reject on skip or reject manually.",
            ))
            break

    oc = s.get("overnight_scorecard") or {}
    grade = str(oc.get("grade") or "").upper()
    if grade in ("D", "F"):
        out.append(_snag(
            "overnight_grade_low", SEV_WARN,
            f"Overnight scorecard grade {grade} — review risks before arming.",
            hint=str(oc.get("tip") or "")[:120],
        ))

    return out


def scan_snags(status: dict | None) -> dict:
    """Full snag report for remote check-in."""
    s = status or {}
    status_snags = scan_status_snags(s)
    log_snags = scan_log_snags(s.get("recent_log"))
    merged: list[dict] = []
    seen: set[str] = set()
    for item in status_snags + log_snags:
        key = f"{item.get('code')}|{item.get('broker')}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)

    sev_rank = {SEV_CRITICAL: 0, SEV_WARN: 1, SEV_INFO: 2}
    merged.sort(key=lambda x: (sev_rank.get(str(x.get("severity")), 9), x.get("code") or ""))

    counts = {SEV_CRITICAL: 0, SEV_WARN: 0, SEV_INFO: 0}
    for item in merged:
        sev = str(item.get("severity") or SEV_INFO)
        if sev in counts:
            counts[sev] += 1

    if counts[SEV_CRITICAL]:
        overall = "critical"
    elif counts[SEV_WARN]:
        overall = "warn"
    elif merged:
        overall = "info"
    else:
        overall = "ok"

    lines = []
    for item in merged[:8]:
        b = f" [{item['broker']}]" if item.get("broker") else ""
        lines.append(f"{item.get('severity', '?').upper()}{b}: {item.get('message')}")
    summary = "No snags detected." if not lines else " | ".join(lines[:4])

    return {
        "ok": True,
        "status": overall,
        "at": time.time(),
        "snags": merged,
        "counts": counts,
        "summary": summary[:500],
    }


def snag_alert_key(snag: dict) -> str:
    return f"{snag.get('code')}|{snag.get('broker')}|{snag.get('message', '')[:60]}"


def new_snags_for_alert(
    current: dict | None,
    previous_keys: set | None,
    *,
    min_severity: str = SEV_WARN,
) -> list[dict]:
    """Snags worth alerting that weren't in the previous key set."""
    rank = {SEV_CRITICAL: 0, SEV_WARN: 1, SEV_INFO: 2}
    min_rank = rank.get(min_severity, 1)
    prev = previous_keys or set()
    out = []
    for snag in (current or {}).get("snags") or []:
        sev = str(snag.get("severity") or SEV_INFO)
        if rank.get(sev, 9) > min_rank:
            continue
        key = snag_alert_key(snag)
        if key not in prev:
            out.append(snag)
    return out
