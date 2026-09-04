"""
Cursor IDE monitor bridge — read-only desk digest for MCP / agent polling.

Your Cursor API key (cursor_…) lives in the Cursor app (Dashboard → Integrations).
Market Advisor exposes a local read token + /api/agent/* for MCP tools on this desk.
"""
from __future__ import annotations

import json
import os
import secrets
import ssl
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TOKEN_BYTES = 24


def generate_token() -> str:
    return secrets.token_urlsafe(DEFAULT_TOKEN_BYTES)


def ensure_token(settings: dict | None) -> str:
    s = settings or {}
    try:
        import credentials as cred

        tok = cred.resolve_cursor_monitor_token(s)
    except Exception:
        tok = str(s.get("cursor_monitor_token") or "").strip()
    if tok:
        s["cursor_monitor_token"] = tok
        return tok
    tok = generate_token()
    try:
        import credentials as cred

        cred.persist_cursor_monitor_token(tok, s)
    except Exception:
        s["cursor_monitor_token"] = tok
    return tok


def monitor_base_url(settings: dict | None) -> str:
    s = settings or {}
    host = str(s.get("monitor_host") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(s.get("monitor_port") or 8791)
    https = bool(s.get("monitor_https", True))
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    scheme = "https" if https else "http"
    return f"{scheme}://{host}:{port}"


def _ssl_context(verify: bool):
    if verify:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_json(
    url: str,
    *,
    user: str = "",
    password: str = "",
    token: str = "",
    path: str = "/api/agent/digest",
    timeout: float = 12.0,
    verify_tls: bool = False,
) -> dict:
    full = url.rstrip("/") + path
    req = urllib.request.Request(full, method="GET")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    elif user:
        import base64

        raw = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {raw}")
    kwargs: dict[str, Any] = {"timeout": timeout}
    ctx = _ssl_context(verify_tls)
    if ctx is not None:
        kwargs["context"] = ctx
    with urllib.request.urlopen(req, **kwargs) as resp:
        return json.loads(resp.read().decode("utf-8"))


def connection_from_env() -> dict:
    """Env vars for tools/market_advisor_mcp.py."""
    settings_path = str(os.environ.get("MARKET_ADVISOR_SETTINGS") or "").strip()
    if settings_path and os.path.isfile(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
            return connection_from_settings(settings)
        except Exception:
            pass
    url = str(os.environ.get("MARKET_ADVISOR_URL") or "").strip()
    if not url:
        url = "https://127.0.0.1:8791"
    return {
        "url": url,
        "user": str(os.environ.get("MARKET_ADVISOR_USER") or "").strip(),
        "password": str(os.environ.get("MARKET_ADVISOR_PASS") or "").strip(),
        "token": str(os.environ.get("MARKET_ADVISOR_TOKEN") or "").strip(),
        "verify_tls": str(os.environ.get("MARKET_ADVISOR_VERIFY_TLS") or "").lower() in (
            "1", "true", "yes",
        ),
    }


def connection_from_settings(settings: dict | None) -> dict:
    s = settings or {}
    return {
        "url": monitor_base_url(s),
        "user": str(s.get("monitor_user") or "").strip(),
        "password": str(s.get("monitor_pass") or "").strip(),
        "token": str(s.get("cursor_monitor_token") or "").strip(),
        "verify_tls": False,
    }


def fetch_digest(conn: dict | None = None) -> dict:
    c = conn or connection_from_env()
    try:
        return fetch_json(
            c["url"],
            user=c.get("user") or "",
            password=c.get("password") or "",
            token=c.get("token") or "",
            path="/api/agent/digest",
            verify_tls=bool(c.get("verify_tls")),
        )
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body or e.reason}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_recent_log(conn: dict | None = None, *, max_lines: int = 25) -> dict:
    c = conn or connection_from_env()
    try:
        data = fetch_json(
            c["url"],
            user=c.get("user") or "",
            password=c.get("password") or "",
            token=c.get("token") or "",
            path=f"/api/agent/log?limit={int(max_lines)}",
            verify_tls=bool(c.get("verify_tls")),
        )
        return data
    except Exception as e:
        return {"ok": False, "error": str(e), "lines": []}


def _broker_lines(brokers: dict | None) -> list[str]:
    out = []
    for name, b in (brokers or {}).items():
        if not isinstance(b, dict):
            continue
        flags = []
        if b.get("dd_pause"):
            flags.append("DD pause")
        if b.get("reauth_needed"):
            flags.append("reauth")
        if not b.get("connected"):
            flags.append("offline")
        elif b.get("armed"):
            flags.append("armed")
        if b.get("buy_engines_parked"):
            flags.append("parked")
        flag_s = f" ({', '.join(flags)})" if flags else ""
        out.append(f"{name}{flag_s}")
    return out


def build_agent_digest(status: dict) -> dict:
    """Compact agent-friendly snapshot from full monitor status."""
    try:
        import desk_watchdog as dw
        snag_report = dw.scan_snags(status)
    except Exception:
        snag_report = {"status": "ok", "snags": [], "summary": ""}

    advisors = status.get("advisor") or {}
    pending = advisors.get("pending") or []
    desk_health = status.get("desk_health") or advisors.get("desk_health") or {}
    recent = list(status.get("recent_log") or [])[-20:]
    issues = []
    if status.get("halted"):
        issues.append("Panic halt is ON — all brokers disarmed.")
    for snag in (snag_report.get("snags") or [])[:5]:
        msg = str(snag.get("message") or "")
        if msg and msg not in issues:
            issues.append(msg)
    dh_brief = str(desk_health.get("brief") or "").strip()
    if dh_brief and str(desk_health.get("status") or "") != "ok":
        issues.append(dh_brief)
    for ln in reversed(recent):
        low = str(ln).lower()
        if "buy batch error" in low or "thread error" in low:
            issues.append(str(ln)[-200:])
            break
        if "pausing new buys" in low and "[dd]" in low:
            issues.append(str(ln)[-200:])
            break

    top_adv = pending[0] if pending else {}
    summary_parts = [
        f"{status.get('app') or 'Market Advisor'} v{status.get('version') or '?'}",
        f"mode={status.get('mode') or '?'} market={status.get('market') or '?'}",
    ]
    if status.get("banner"):
        summary_parts.append(f"banner: {status.get('banner')}")
    brokers = _broker_lines(status.get("brokers"))
    if brokers:
        summary_parts.append("brokers: " + "; ".join(brokers))
    if pending:
        summary_parts.append(
            f"advisor: {len(pending)} pending — top {top_adv.get('broker')} "
            f"{top_adv.get('ticker')} ~${float(top_adv.get('dollars') or 0):.0f}"
        )
        if top_adv.get("ai_brief"):
            summary_parts.append(f"AI: {top_adv.get('ai_verdict') or '?'} — {top_adv.get('ai_brief')}")
    trader = status.get("trader") or {}
    by_broker = trader.get("by_broker") or {}
    if by_broker:
        try:
            from trader_context import format_trader_digest

            day_pnl = None
            try:
                bals = status.get("balances") or {}
                comb = bals.get("combined") or {}
                if "day_pnl" in comb:
                    day_pnl = float(comb.get("day_pnl") or 0)
            except Exception:
                day_pnl = None
            td = format_trader_digest(by_broker, day_pnl=day_pnl)
            if td:
                summary_parts.append(td.replace("\n", " | "))
        except Exception:
            pass
    if issues:
        summary_parts.append("issues: " + " | ".join(issues[:3]))
    if snag_report.get("summary") and snag_report.get("status") not in ("ok", ""):
        summary_parts.append(f"watchdog: {snag_report.get('summary')}")

    return {
        "ok": True,
        "version": status.get("version"),
        "mode": status.get("mode"),
        "market": status.get("market"),
        "halted": bool(status.get("halted")),
        "banner": status.get("banner"),
        "desk_health": desk_health,
        "desk_snags": snag_report,
        "brokers": status.get("brokers") or {},
        "advisor_pending_count": int(advisors.get("count") or len(pending)),
        "advisor_top": top_adv,
        "trader": trader,
        "issues": issues,
        "recent_log_tail": recent[-12:],
        "summary_text": "\n".join(summary_parts),
    }


def fetch_snags(conn: dict | None = None) -> dict:
    c = conn or connection_from_env()
    try:
        return fetch_json(
            c["url"],
            user=c.get("user") or "",
            password=c.get("password") or "",
            token=c.get("token") or "",
            path="/api/agent/snags",
            verify_tls=bool(c.get("verify_tls")),
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "snags": []}


def fetch_crash_log(
    conn: dict | None = None,
    *,
    max_chars: int = 12000,
    max_lines: int = 120,
) -> dict:
    c = conn or connection_from_env()
    try:
        return fetch_json(
            c["url"],
            user=c.get("user") or "",
            password=c.get("password") or "",
            token=c.get("token") or "",
            path=(
                f"/api/agent/crash_log?chars={int(max_chars)}"
                f"&lines={int(max_lines)}"
            ),
            verify_tls=bool(c.get("verify_tls")),
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "text": ""}


def format_crash_log(payload: dict | None) -> str:
    p = payload or {}
    if p.get("error"):
        return f"Crash log unreachable: {p.get('error')}"
    head = "Crash log"
    if p.get("truncated"):
        head += " (truncated)"
    if p.get("exists") is False:
        return f"{head}: (empty — no hard faults recorded)"
    body = str(p.get("text") or "").strip()
    return f"{head}:\n{body}" if body else f"{head}: (empty)"


def format_snag_report(report: dict | None) -> str:
    r = report or {}
    if r.get("error"):
        return f"Desk unreachable: {r.get('error')}"
    lines = [f"Watchdog: {r.get('status', '?').upper()} — {r.get('summary', '')}"]
    for s in r.get("snags") or []:
        b = f" [{s.get('broker')}]" if s.get("broker") else ""
        lines.append(f"  • {s.get('severity', '?').upper()}{b}: {s.get('message')}")
        if s.get("hint"):
            lines.append(f"    → {s.get('hint')}")
    return "\n".join(lines) if len(lines) > 1 else lines[0]


def build_mcp_config_snippet(settings: dict | None) -> str:
    """JSON snippet user pastes into Cursor MCP config (project or user level)."""
    s = settings or {}
    conn = connection_from_settings(s)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script = os.path.join(root, "tools", "market_advisor_mcp.py")
    py = os.environ.get("MARKET_ADVISOR_PYTHON") or "py"
    cfg = {
        "mcpServers": {
            "market-advisor": {
                "command": py,
                "args": [script],
                "env": {
                    "MARKET_ADVISOR_URL": conn["url"],
                    "MARKET_ADVISOR_TOKEN": conn.get("token") or "",
                    "MARKET_ADVISOR_USER": conn.get("user") or "",
                    "MARKET_ADVISOR_PASS": conn.get("password") or "",
                    "MARKET_ADVISOR_VERIFY_TLS": "false",
                },
            }
        }
    }
    return json.dumps(cfg, indent=2)
