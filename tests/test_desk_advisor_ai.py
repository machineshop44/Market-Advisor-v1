"""AI Desk Advisor — local rules + optional LLM briefs."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import advisor_queue as aq
import desk_advisor_ai as dai


def test_local_skip_expensive_share():
    prop = {
        "ticker": "MSFT",
        "broker": "E*TRADE",
        "dollars": 95.0,
        "price": 514.0,
        "score": 58.0,
        "engine": "BREAKOUT",
        "asset_type": "stock",
    }
    ctx = {"buying_power": 100.0, "equity": 100.0, "posture": "growth"}
    out = dai.local_analyze_proposal(prop, ctx)
    assert out["verdict"] == "skip"
    assert "MSFT" in out["brief"]
    assert out["source"] == "local"


def test_local_approve_reasonable_ticket():
    prop = {
        "ticker": "TLT",
        "broker": "E*TRADE",
        "dollars": 45.0,
        "price": 88.0,
        "score": 87.0,
        "engine": "CORE",
        "asset_type": "stock",
    }
    ctx = {"buying_power": 100.0, "equity": 100.0, "posture": "growth"}
    out = dai.local_analyze_proposal(prop, ctx)
    assert out["verdict"] in ("approve", "wait")
    assert "TLT" in out["brief"]


def test_analyze_without_api_key_uses_local():
    prop = {
        "ticker": "VOO",
        "broker": "E*TRADE",
        "dollars": 90.0,
        "price": 520.0,
        "score": 52.0,
        "engine": "CORE",
    }
    settings = {"advisor_ai_source": "local", "advisor_ai_api_key": ""}
    out = dai.analyze_proposal(prop, {"buying_power": 100.0}, settings)
    assert out["source"] == "local"
    assert out["verdict"] == "skip"


def test_patch_ai_on_proposal(tmp_path, monkeypatch):
    qfile = tmp_path / "advisor_queue.json"
    monkeypatch.setattr(aq, "QUEUE_FILE", str(qfile))
    prop = aq.propose(
        broker="Robinhood", ticker="AAPL", price=100.0, dollars=25.0, score=72.0,
    )
    patched = aq.patch_ai(prop["id"], {
        "verdict": "approve",
        "brief": "Looks fine for your book.",
        "detail": "test",
        "source": "local",
    })
    assert patched and patched.get("ai_verdict") == "approve"
    payload = aq.monitor_payload()
    assert payload["pending"][0]["ai_brief"] == "Looks fine for your book."
    assert payload["pending"][0]["ai_verdict"] == "approve"
    assert payload["pending"][0]["ai_pending"] is False


def test_desk_health_local_ok():
    out = dai.analyze_desk_health(
        ["[Robinhood] Heartbeat OK", "[CORE] Ranked 2 buys"],
        {"halted": False},
        None,
    )
    assert out["status"] == "ok"


def test_desk_health_local_warn_on_dd():
    out = dai.analyze_desk_health(
        ["[DD] E*TRADE pausing new buys — peak drawdown"],
        {"halted": False},
        None,
    )
    assert out["status"] == "warn"
    assert "drawdown" in out["brief"].lower() or "Drawdown" in out["brief"]


def test_pick_gemini_models_prefers_available_flash(monkeypatch):
    monkeypatch.setattr(
        dai,
        "list_gemini_models",
        lambda _k: ["gemini-3.6-flash", "gemini-embedding-001"],
    )
    order = dai._pick_gemini_models("k", "")
    assert order[0] == "gemini-3.6-flash"


def test_gemini_call_uses_header_not_query_key(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"brief":"ok"}'}]}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return FakeResp()

    monkeypatch.setattr(dai.requests, "post", fake_post)
    out = dai._call_gemini("AQ.test-key", "gemini-2.5-flash", "hi")
    assert "ok" in out
    assert "key=" not in captured["url"]
    assert captured["headers"].get("x-goog-api-key") == "AQ.test-key"


def test_ai_configured_requires_key():
    assert not dai.ai_configured({"advisor_ai_source": "gemini", "advisor_ai_api_key": ""})
    assert dai.ai_configured({"advisor_ai_source": "gemini", "advisor_ai_api_key": "x"})
    assert not dai.ai_configured({"advisor_ai_source": "local", "advisor_ai_api_key": "x"})


def test_format_advisor_summary_includes_ai():
    from auto_cycle import format_advisor_settings_summary

    s = format_advisor_settings_summary(
        advisor_on=True, remote_on=False, ai_on=False, ai_ready=False, ai_source="local",
    )
    assert "local briefs" in s
    s2 = format_advisor_settings_summary(
        advisor_on=True, remote_on=True, ai_on=True, ai_ready=True,
        ai_source="gemini", cursor_on=True,
    )
    assert "gemini API" in s2
    assert "Cursor on" in s2
