"""Desk watchdog snag detection."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import desk_watchdog as dw


def test_scan_halt_critical():
    r = dw.scan_snags({"halted": True, "brokers": {}, "recent_log": []})
    assert r["status"] == "critical"
    assert any(s["code"] == "panic_halt" for s in r["snags"])


def test_scan_dd_pause_warn():
    r = dw.scan_snags({
        "brokers": {
            "Robinhood": {"dd_pause": True, "dd_reason": "peak -22%", "connected": True},
        },
        "recent_log": [],
    })
    assert r["status"] == "warn"
    assert any(s["code"] == "dd_pause" for s in r["snags"])


def test_scan_log_thread_error():
    r = dw.scan_snags({
        "brokers": {},
        "recent_log": ["Thread Error in _bg_buy_batch: division by zero"],
    })
    assert any(s["code"] == "thread_error" for s in r["snags"])


def test_new_snags_for_alert():
    report = dw.scan_snags({
        "halted": True,
        "brokers": {},
        "recent_log": [],
    })
    new = dw.new_snags_for_alert(report, set(), min_severity=dw.SEV_WARN)
    assert len(new) >= 1
    keys = {dw.snag_alert_key(s) for s in new}
    again = dw.new_snags_for_alert(report, keys, min_severity=dw.SEV_WARN)
    assert again == []


def test_agent_snags_endpoint():
    import base64
    import json
    import time
    import urllib.request

    import cursor_monitor as cm
    import monitor

    port = 19877
    monitor.stop_monitor()
    tok = cm.generate_token()
    monitor.start_monitor(
        host="127.0.0.1", port=port, cursor_agent_enabled=True, cursor_agent_token=tok,
    )
    deadline = time.time() + 4
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.3)
            break
        except Exception:
            time.sleep(0.05)
    monitor.update_status({
        "halted": True,
        "brokers": {},
        "recent_log": [],
    })
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/agent/snags")
        req.add_header("Authorization", f"Bearer {tok}")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data.get("status") == "critical"
    finally:
        monitor.stop_monitor()
