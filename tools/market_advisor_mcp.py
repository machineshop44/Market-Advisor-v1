#!/usr/bin/env python3
"""
MCP server — lets Cursor IDE poll your running Market Advisor desk.

Setup:
  pip install -r requirements-mcp.txt
  Enable "Cursor AI monitor" in Market Advisor Settings, copy MCP JSON, add to Cursor.

Env (set by MCP config):
  MARKET_ADVISOR_URL, MARKET_ADVISOR_TOKEN (preferred read-only token)
  or MARKET_ADVISOR_USER + MARKET_ADVISOR_PASS
  Optional: MARKET_ADVISOR_SETTINGS path to Src/settings.json
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "Missing MCP package. Run: pip install -r requirements-mcp.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

import cursor_monitor as cm

mcp = FastMCP("market-advisor")


@mcp.tool()
def get_desk_digest() -> str:
    """Plain-English desk health: brokers, advisor queue, issues, recent activity."""
    data = cm.fetch_digest()
    if not data.get("ok", True) and data.get("error"):
        return f"Market Advisor unreachable: {data.get('error')}"
    text = str(data.get("summary_text") or "").strip()
    if text:
        return text
    return json_dumps(data)


@mcp.tool()
def get_desk_json() -> str:
    """Full structured agent digest JSON from /api/agent/digest."""
    return json_dumps(cm.fetch_digest())


@mcp.tool()
def get_recent_log(lines: int = 20) -> str:
    """Last N lines from the desk activity log."""
    data = cm.fetch_recent_log(max_lines=max(1, min(int(lines), 80)))
    if data.get("error"):
        return f"Error: {data.get('error')}"
    rows = data.get("lines") or []
    return "\n".join(str(x) for x in rows) if rows else "(empty log)"


@mcp.tool()
def check_desk_snags() -> str:
    """Proactive snag scan: reauth, DD pause, zero BP, cycle stalls, log errors."""
    return cm.format_snag_report(cm.fetch_snags())


@mcp.tool()
def get_crash_log(lines: int = 80) -> str:
    """Tail of desk crash_log.txt — hard faults / uncaught exceptions (remote diagnose)."""
    return cm.format_crash_log(
        cm.fetch_crash_log(max_lines=max(10, min(int(lines), 400)))
    )

@mcp.tool()
def get_trader_context() -> str:
    """Per-broker desk context: BP, affordable share price, posture, regime, engines, blockers."""
    digest = cm.fetch_digest()
    trader = digest.get("trader") or {}
    by_broker = trader.get("by_broker") or {}
    if by_broker:
        try:
            from trader_context import format_trader_digest

            return format_trader_digest(by_broker)
        except Exception:
            pass
    if digest.get("error"):
        return f"Market Advisor unreachable: {digest.get('error')}"
    return "No trader context in desk snapshot (is the app running and connected?)."


@mcp.tool()
def remote_health_check() -> str:
    """Full remote check-in — digest plus snag list for early issue detection."""
    digest = cm.fetch_digest()
    snags = cm.fetch_snags()
    parts = []
    if digest.get("summary_text"):
        parts.append(str(digest.get("summary_text")))
    snag_text = cm.format_snag_report(snags)
    if snag_text and "No snags" not in snag_text:
        parts.append("")
        parts.append(snag_text)
    if digest.get("error"):
        return f"Unreachable: {digest.get('error')}"
    return "\n".join(parts) if parts else json_dumps({"digest": digest, "snags": snags})


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
