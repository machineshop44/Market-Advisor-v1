"""1.39.15 — fully deployed, small-ticket exits, wait TTL, crash_log API."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_fully_deployed_blocker():
    import trader_context as tc

    ctx = tc.build_trader_context(
        "Coinbase",
        equity=30.0,
        buying_power=2.0,
        settings={"min_trade_dollars": 5.0, "target_bp_utilization_pct": 98.0},
        open_positions=1,
        armed=True,
        connected=True,
        supports_crypto=True,
        supports_equities=False,
        regime_crypto_ok=True,
    )
    codes = [b["code"] for b in ctx["blockers"]]
    assert "fully_deployed" in codes
    assert "low_bp" not in codes
    assert ctx["can_place_new_buy"] is False
    digest = tc.format_trader_digest({"Coinbase": ctx}, day_pnl=0.5)
    assert "DEPLOYED" in digest
    assert "Day P&L" in digest


def test_low_bp_empty_book_still_low_bp():
    import trader_context as tc

    ctx = tc.build_trader_context(
        "Robinhood",
        equity=40.0,
        buying_power=2.0,
        settings={"min_trade_dollars": 5.0},
        open_positions=0,
        armed=True,
        connected=True,
        supports_equities=True,
        supports_crypto=False,
        regime_equity_ok=True,
    )
    codes = [b["code"] for b in ctx["blockers"]]
    assert "low_bp" in codes
    assert "fully_deployed" not in codes


def test_small_ticket_exit_nudge():
    from scoring import apply_small_ticket_exit_nudge, resolve_exit_fees

    base = {
        "ttp_arm": 0.02,
        "ttp_trail": 0.01,
        "time_profit_roi": 0.015,
        "time_profit_min": 45,
        "time_stop_roi": 0.01,
        "hard_stop": -0.04,
    }
    nudged = apply_small_ticket_exit_nudge(
        base, "COINBASE", ticker="BONK", asset_type="crypto",
        equity=30.0, holding_value=22.0,
    )
    assert nudged["ttp_arm"] < base["ttp_arm"]
    assert nudged.get("small_ticket_exit_nudge")

    fees = resolve_exit_fees(
        "COINBASE", "BONK", "crypto",
        equity=30.0, holding_value=18.0,
    )
    assert float(fees.get("ttp_arm") or 0) > 0


def test_affordability_rank_boost():
    from scoring import affordability_rank_boost, buy_rank_score_for_book

    assert affordability_rank_boost(price=3.0, buying_power=50.0, is_crypto=False) > 0
    assert affordability_rank_boost(price=400.0, buying_power=50.0, is_crypto=False) < 0
    assert affordability_rank_boost(price=3.0, buying_power=50.0, is_crypto=True) == 0.0
    cheap = buy_rank_score_for_book("SOUN", is_crypto=False, price=8.0, buying_power=80.0)
    rich = buy_rank_score_for_book("MSFT", is_crypto=False, price=420.0, buying_power=80.0)
    # Soft nudge only — cheap name should not be crushed vs rich when both get scores
    assert isinstance(cheap, float) and isinstance(rich, float)


def test_wait_expire_and_auto_skip(tmp_path, monkeypatch):
    import advisor_queue as aq

    qfile = tmp_path / "advisor_queue.json"
    dfile = tmp_path / "advisor_decisions.jsonl"
    monkeypatch.setattr(aq, "QUEUE_FILE", str(qfile))
    monkeypatch.setattr(aq, "DECISIONS_FILE", str(dfile))

    prop = aq.propose(
        broker="Coinbase", ticker="BONK", price=0.01, dollars=20.0, score=70.0,
        ttl_sec=3600,
    )
    aq.patch_ai(prop["id"], {"verdict": "wait", "brief": "wait for setup", "source": "local"})
    patched = aq.get(prop["id"])
    assert patched and float(patched.get("expires_at") or 0) < time.time() + aq.WAIT_TTL_SEC + 5

    # Force wait TTL past
    with aq._lock:
        data = aq._load()
        for p in data["proposals"]:
            if p.get("id") == prop["id"]:
                p["ai_at"] = time.time() - aq.WAIT_TTL_SEC - 10
        aq._save(data)

    n = aq.expire_wait_verdicts()
    assert n == 1
    assert aq.list_pending() == []
    rows = aq.list_decisions(limit=5)
    assert any(r.get("action") == "wait_auto_skip" for r in rows)


def test_crash_log_read_tail(tmp_path, monkeypatch):
    import crash_log as cl

    path = tmp_path / "crash_log.txt"
    monkeypatch.setattr(cl, "CRASH_LOG_FILE", str(path))
    cl.write_crash("UNIT", "line-one\nline-two", also_stderr=False)
    payload = cl.read_tail(max_chars=5000, max_lines=50)
    assert payload["ok"] is True
    assert payload["exists"] is True
    assert "UNIT" in payload["text"]
    assert "line-two" in payload["text"]


def test_agent_crash_log_endpoint():
    import json
    import time
    import urllib.request
    import cursor_monitor as cm
    import monitor
    import crash_log as cl

    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    monitor.stop_monitor()
    tok = cm.generate_token()
    monitor.start_monitor(
        host="127.0.0.1",
        port=port,
        cursor_agent_enabled=True,
        cursor_agent_token=tok,
    )
    deadline = time.time() + 4.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.3)
            break
        except Exception:
            time.sleep(0.05)
    try:
        cl.write_crash("API_TEST", "from unit", also_stderr=False)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/agent/crash_log?lines=40"
        )
        req.add_header("Authorization", f"Bearer {tok}")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data.get("ok") is True
        assert "text" in data
    finally:
        monitor.stop_monitor()


def test_fully_deployed_idle_uses_cache_only():
    """Regression: idle message from caches only — never needs live broker APIs."""
    import trader_context as tc

    msg = tc.fully_deployed_idle_reason(
        buying_power=2.0, open_positions=1, min_ticket=5.0, utilization=0.98,
    )
    assert msg and "Fully deployed" in msg
    assert tc.fully_deployed_idle_reason(
        buying_power=2.0, open_positions=0, min_ticket=5.0, utilization=0.98,
    ) is None
    assert tc.fully_deployed_idle_reason(
        buying_power=80.0, open_positions=1, min_ticket=5.0, utilization=0.98,
    ) is None
