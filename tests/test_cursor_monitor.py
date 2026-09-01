"""Cursor monitor bridge + agent digest API."""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cursor_monitor as cm
import monitor


def _auth_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _bearer_header(token):
    return f"Bearer {token}"


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(port, timeout=4.0):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.4)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def test_build_agent_digest_flags_halt():
    digest = cm.build_agent_digest({
        "version": "1.39.0",
        "mode": "LIVE",
        "halted": True,
        "brokers": {},
        "recent_log": [],
    })
    assert digest["ok"] is True
    assert digest["halted"] is True
    assert any("halt" in x.lower() for x in digest["issues"])


def test_agent_digest_bearer_token():
    port = _free_port()
    monitor.stop_monitor()
    tok = cm.generate_token()
    monitor.start_monitor(
        host="127.0.0.1",
        port=port,
        cursor_agent_enabled=True,
        cursor_agent_token=tok,
    )
    assert _wait_ready(port)
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/agent/digest")
        req.add_header("Authorization", _bearer_header(tok))
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data.get("ok") is True
        assert "summary_text" in data
    finally:
        monitor.stop_monitor()


def test_agent_digest_rejects_bad_token():
    port = _free_port()
    monitor.stop_monitor()
    tok = cm.generate_token()
    monitor.start_monitor(
        host="127.0.0.1",
        port=port,
        cursor_agent_enabled=True,
        cursor_agent_token=tok,
    )
    assert _wait_ready(port)
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/agent/digest")
        req.add_header("Authorization", _bearer_header("wrong-token"))
        try:
            urllib.request.urlopen(req, timeout=2.0)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        monitor.stop_monitor()


def test_resolve_ai_source_legacy():
    import desk_advisor_ai as dai

    assert dai.resolve_ai_source({"advisor_ai_source": "openai"}) == "openai"
    assert dai.resolve_ai_source({"advisor_ai_enabled": False}) == "local"
    assert dai.resolve_ai_source({
        "advisor_ai_enabled": True,
        "advisor_ai_provider": "gemini",
    }) == "gemini"
