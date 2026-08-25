"""Tests for monitor Basic Auth, HTTPS, and companion POST /api/auto."""
import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import monitor  # noqa: E402


def _auth_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _wait_ready(port, timeout=4.0, user=None, password=None, https=False):
    scheme = "https" if https else "http"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{scheme}://127.0.0.1:{port}/api/status")
            if user:
                req.add_header("Authorization", _auth_header(user, password or ""))
            kwargs = {"timeout": 0.4}
            if https:
                kwargs["context"] = _ssl_ctx()
            urllib.request.urlopen(req, **kwargs)
            return True
        except Exception:
            time.sleep(0.05)
    return False


def test_post_auto_requires_controls_and_auth():
    port = _free_port()
    monitor.stop_monitor()
    seen = []

    def handler(broker, armed):
        seen.append((broker, armed))
        return {"ok": True, "broker": broker, "armed": armed}

    monitor.set_control_handler(handler)
    ok, _ = monitor.start_monitor(
        host="127.0.0.1",
        port=port,
        username="phone",
        password="secret",
        controls_enabled=True,
        use_tls=True,
    )
    assert ok
    assert _wait_ready(port, user="phone", password="secret", https=True)

    req = urllib.request.Request(
        f"https://127.0.0.1:{port}/api/auto",
        data=json.dumps({"broker": "Robinhood", "armed": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, context=_ssl_ctx(), timeout=2)
        assert False, "expected 401"
    except urllib.error.HTTPError as e:
        assert e.code == 401

    req2 = urllib.request.Request(
        f"https://127.0.0.1:{port}/api/auto",
        data=json.dumps({"broker": "Coinbase", "armed": True}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": _auth_header("phone", "secret"),
        },
        method="POST",
    )
    with urllib.request.urlopen(req2, context=_ssl_ctx(), timeout=2) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body.get("ok") is True
    assert seen == [("Coinbase", True)]

    tls = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(f"https://127.0.0.1:{port}/api/tls"),
            context=_ssl_ctx(),
            timeout=2,
        ).read().decode("utf-8")
    )
    assert tls.get("tls") is True
    assert tls.get("fingerprint")
    monitor.stop_monitor()


def test_post_auto_batch_and_enabled_alias():
    port = _free_port()
    monitor.stop_monitor()
    seen = []

    def handler(broker, armed):
        seen.append((broker, armed))
        return {"ok": True, "broker": broker, "armed": armed}

    monitor.set_control_handler(handler)
    ok, _ = monitor.start_monitor(
        host="127.0.0.1",
        port=port,
        username="phone",
        password="secret",
        controls_enabled=True,
        use_tls=True,
    )
    assert ok
    assert _wait_ready(port, user="phone", password="secret", https=True)

    req = urllib.request.Request(
        f"https://127.0.0.1:{port}/api/auto",
        data=json.dumps({"broker": "Robinhood", "enabled": True}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": _auth_header("phone", "secret"),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=2) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body.get("ok") is True
    assert ("Robinhood", True) in seen

    req2 = urllib.request.Request(
        f"https://127.0.0.1:{port}/api/auto",
        data=json.dumps({
            "brokers": {"Coinbase": True, "E*TRADE": False},
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": _auth_header("phone", "secret"),
        },
        method="POST",
    )
    with urllib.request.urlopen(req2, context=_ssl_ctx(), timeout=2) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body.get("ok") is True
    assert "results" in body
    assert ("Coinbase", True) in seen
    assert ("E*TRADE", False) in seen
    monitor.stop_monitor()


def test_remote_bind_requires_auth():
    port = _free_port()
    monitor.stop_monitor()
    ok, msg = monitor.start_monitor(
        host="0.0.0.0",
        port=port,
        username="",
        password="",
        controls_enabled=False,
        use_tls=True,
    )
    assert not ok
    assert "requires User" in msg
    monitor.stop_monitor()


def test_post_auto_rejected_when_controls_off():
    port = _free_port()
    monitor.stop_monitor()
    monitor.set_control_handler(lambda b, a: {"ok": True})
    ok, _ = monitor.start_monitor(
        host="127.0.0.1",
        port=port,
        username="phone",
        password="secret",
        controls_enabled=False,
        use_tls=True,
    )
    assert ok
    assert _wait_ready(port, user="phone", password="secret", https=True)
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}/api/auto",
        data=json.dumps({"broker": "Robinhood", "armed": False}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": _auth_header("phone", "secret"),
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, context=_ssl_ctx(), timeout=2)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403
    monitor.stop_monitor()


def test_auth_lockout_includes_seconds_and_clear():
    port = _free_port()
    monitor.stop_monitor()
    monitor.clear_auth_lockouts()
    ok, _ = monitor.start_monitor(
        host="127.0.0.1",
        port=port,
        username="phone",
        password="secret",
        controls_enabled=True,
        use_tls=True,
    )
    assert ok
    assert _wait_ready(port, user="phone", password="secret", https=True)

    # Exhaust failures to trigger lockout
    for _ in range(monitor._AUTH_MAX_FAILS):
        req = urllib.request.Request(f"https://127.0.0.1:{port}/api/status")
        req.add_header("Authorization", _auth_header("phone", "wrong"))
        try:
            urllib.request.urlopen(req, context=_ssl_ctx(), timeout=2)
        except urllib.error.HTTPError as e:
            assert e.code == 401

    req = urllib.request.Request(f"https://127.0.0.1:{port}/api/status")
    req.add_header("Authorization", _auth_header("phone", "secret"))
    try:
        urllib.request.urlopen(req, context=_ssl_ctx(), timeout=2)
        assert False, "expected lockout 401"
    except urllib.error.HTTPError as e:
        assert e.code == 401
        retry = e.headers.get("Retry-After")
        body = json.loads(e.read().decode("utf-8"))
        assert body.get("lockout_seconds", 0) > 0
        assert retry is not None
        assert int(retry) == int(body["lockout_seconds"])

    monitor.clear_auth_lockouts()
    req2 = urllib.request.Request(f"https://127.0.0.1:{port}/api/status")
    req2.add_header("Authorization", _auth_header("phone", "secret"))
    with urllib.request.urlopen(req2, context=_ssl_ctx(), timeout=2) as resp:
        assert resp.status == 200
    assert monitor.is_running()
    monitor.stop_monitor()
    assert not monitor.is_running()


if __name__ == "__main__":
    test_post_auto_requires_controls_and_auth()
    test_post_auto_batch_and_enabled_alias()
    test_remote_bind_requires_auth()
    test_post_auto_rejected_when_controls_off()
    test_auth_lockout_includes_seconds_and_clear()
    print("ok")
