"""AI Desk Advisor — local rules + optional LLM briefs."""
import os
import sys
import time

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


def test_research_pack_shape_and_prompt():
    prop = {
        "ticker": "BTC",
        "broker": "Coinbase",
        "dollars": 20.0,
        "price": 95000.0,
        "score": 90.0,
        "engine": "CRYPTO",
        "asset_type": "crypto",
        "is_crypto": True,
    }
    # Force empty cache path with a unique synthetic ticker to avoid network flake
    prop["ticker"] = "ZZZTEST"
    pack = dai.build_research_pack(prop, {"regime": {"label": "neutral"}})
    assert pack.get("ticker") == "ZZZTEST"
    assert "notes" in pack
    prompt = dai._proposal_prompt(prop, {"buying_power": 24.0}, research=pack)
    assert "app_research_pack" in prompt or "research" in prompt
    assert "ZZZTEST" in prompt
    assert "retry_after_min" in prompt
    live = dai._proposal_prompt(
        prop, {"buying_power": 24.0}, research=pack, live_research=True,
    )
    assert "web_search" in live and "x_search" in live
    assert "retry_after_min" in live


def test_extract_responses_text():
    import desk_advisor_ai as dai

    raw = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"verdict":"skip","retry_after_min":45}'}],
            }
        ]
    }
    assert "skip" in dai._extract_responses_text(raw)
    assert dai._extract_responses_text({"output_text": "hello"}) == "hello"


def test_xai_budget_defaults_lift_gemini_caps():
    import desk_advisor_ai as dai

    dai.reset_ai_budgets()
    ok, _ = dai._ai_budget_ok({
        "advisor_ai_source": "xai",
    }, provider="xai")
    assert ok is True
    assert dai.budget_defaults_for_source("xai") == (20, 400)
    dai.reset_ai_budgets()
    settings = {
        "advisor_ai_source": "gemini",
        "advisor_ai_max_per_minute_gemini": 2,
        "advisor_ai_max_per_day_gemini": 3,
    }
    assert dai._ai_budget_ok(settings, provider="gemini")[0] is True
    dai._ai_budget_record("gemini")
    dai._ai_budget_record("gemini")
    ok, why = dai._ai_budget_ok(settings, provider="gemini")
    assert ok is False
    assert "per-minute" in why
    dai._budget_slot("gemini")["times"].clear()
    dai._ai_budget_record("gemini")  # day count now 3
    ok2, why2 = dai._ai_budget_ok(settings, provider="gemini")
    assert ok2 is False
    assert "daily" in why2


def test_model_defaults_are_per_provider():
    assert dai._model({}, provider="gemini") == dai._DEFAULT_GEMINI_MODEL
    assert dai._model({"advisor_ai_model": "gemini-3-flash-preview"}, provider="groq") == (
        dai._DEFAULT_GROQ_MODEL
    )
    assert dai._model({"advisor_ai_model": "gemini-3-flash-preview"}, provider="openai") == (
        dai._DEFAULT_OPENAI_MODEL
    )
    assert dai._model(
        {"advisor_ai_model_openai": "gpt-4o"}, provider="openai"
    ) == "gpt-4o"
    assert dai._DEFAULT_GROQ_MODEL == "llama-3.1-8b-instant"
    assert ":free" in dai._DEFAULT_OPENROUTER_MODEL
    assert "llama-3.1-8b-instant" in dai._openai_compatible_model_chain("groq")
    assert any(
        x.endswith(":free") for x in dai._openai_compatible_model_chain("openrouter")
    )

def test_local_when_clear_skips_cloud(monkeypatch):
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("cloud should not be called")

    monkeypatch.setattr(dai, "_call_gemini", boom)
    settings = {
        "advisor_ai_source": "gemini",
        "advisor_ai_api_key": "fake-key",
        "advisor_ai_local_when_clear": True,
    }
    weak = {
        "ticker": "XYZ",
        "broker": "E*TRADE",
        "dollars": 10.0,
        "price": 5.0,
        "score": 40.0,
        "engine": "CORE",
        "asset_type": "stock",
    }
    out = dai.analyze_proposal(weak, {"buying_power": 100.0}, settings)
    assert called["n"] == 0
    assert out["source"] == "local"
    assert out["verdict"] == "skip"
    assert "cloud skipped" in str(out.get("detail") or "")


def test_regime_caution_skips_cloud_even_at_high_score(monkeypatch):
    """High-score regime_caution used to burn Gemini/Groq then local-skip."""
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("cloud should not be called for regime caution")

    monkeypatch.setattr(dai, "_call_gemini", boom)
    monkeypatch.setattr(dai, "_call_openai", boom)
    settings = {
        "advisor_ai_source": "gemini",
        "advisor_ai_api_key": "fake-key",
        "advisor_ai_local_when_clear": False,  # even with clear-skip off
        "allow_buys_when_regime_blocked": False,
    }
    prop = {
        "ticker": "PLUG",
        "broker": "Robinhood",
        "dollars": 25.0,
        "price": 16.5,
        "score": 116.0,
        "engine": "PENNY",
        "asset_type": "stock",
        "regime_caution": True,
    }
    ctx = {
        "buying_power": 200.0,
        "allow_buys_when_regime_blocked": False,
        "blockers": [
            {"code": "regime_equity", "message": "SPY sources disagree — blocked"}
        ],
    }
    out = dai.analyze_proposal(prop, ctx, settings)
    assert called["n"] == 0
    assert out["verdict"] == "skip"
    assert "regime" in str(out.get("brief") or "").lower()
    assert "cloud skipped" in str(out.get("detail") or "")


def test_crypto_ignores_spy_regime_blocker():
    prop = {
        "ticker": "SOL",
        "broker": "Robinhood",
        "dollars": 20.0,
        "price": 150.0,
        "score": 80.0,
        "engine": "CRYPTO",
        "asset_type": "Crypto",
    }
    ctx = {
        "buying_power": 200.0,
        "allow_buys_when_regime_blocked": False,
        "blockers": [
            {"code": "regime_equity", "message": "SPY sources disagree — blocked"},
        ],
    }
    out = dai.local_analyze_proposal(prop, ctx)
    assert out["verdict"] != "skip" or "SPY" not in str(out.get("brief") or "")


def test_equity_ignores_btc_regime_blocker():
    prop = {
        "ticker": "PLUG",
        "broker": "Robinhood",
        "dollars": 20.0,
        "price": 16.0,
        "score": 80.0,
        "engine": "PENNY",
        "asset_type": "Penny Stock",
    }
    ctx = {
        "buying_power": 200.0,
        "blockers": [
            {"code": "regime_crypto", "message": "BTC-USD 1H Downtrend"},
        ],
    }
    out = dai.local_analyze_proposal(prop, ctx)
    brief = str(out.get("brief") or "")
    assert "BTC" not in brief or out["verdict"] != "skip"


def test_budget_exhausted_falls_back_local(monkeypatch):
    dai.reset_ai_budgets()
    slot = dai._budget_slot("gemini")
    slot["day_key"] = time.strftime("%Y-%m-%d")
    slot["day_count"] = 99

    def boom(*_a, **_k):
        raise AssertionError("cloud should not be called")

    monkeypatch.setattr(dai, "_call_gemini", boom)
    settings = {
        "advisor_ai_source": "gemini",
        "advisor_ai_api_key": "fake-key",
        "advisor_ai_local_when_clear": False,
        "advisor_ai_max_per_day_gemini": 5,
        "advisor_ai_max_per_minute_gemini": 4,
        "advisor_ai_failover_enabled": False,
    }
    prop = {
        "ticker": "TLT",
        "broker": "E*TRADE",
        "dollars": 45.0,
        "price": 88.0,
        "score": 72.0,
        "engine": "CORE",
        "asset_type": "stock",
    }
    out = dai.analyze_proposal(prop, {"buying_power": 100.0}, settings)
    assert out["source"] == "local_fallback"
    blob = str(out.get("error") or "") + " " + str(out.get("detail") or "")
    assert "daily" in blob and "budget" in blob


def test_failover_skips_failed_provider(monkeypatch):
    dai.reset_ai_budgets()
    calls = []

    def fail_gemini(*_a, **_k):
        calls.append("gemini")
        raise RuntimeError("gemini down")

    def ok_openai(*_a, **_k):
        calls.append("openai")
        return '{"verdict":"wait","brief":"ok via openai","detail":"failover","retry_after_min":10}'

    monkeypatch.setattr(dai, "_call_gemini", fail_gemini)
    monkeypatch.setattr(dai, "_call_openai", ok_openai)
    settings = {
        "advisor_ai_source": "gemini",
        "advisor_ai_api_key_gemini": "g-key",
        "advisor_ai_api_key_openai": "o-key",
        "advisor_ai_failover_enabled": True,
        "advisor_ai_failover_order": "gemini,openai",
        "advisor_ai_local_when_clear": False,
    }
    prop = {
        "ticker": "SPY",
        "broker": "E*TRADE",
        "dollars": 50.0,
        "price": 400.0,
        "score": 70.0,
        "engine": "CORE",
        "asset_type": "stock",
    }
    out = dai.analyze_proposal(prop, {"buying_power": 200.0}, settings)
    assert calls == ["gemini", "openai"]
    assert out["source"] == "openai"
    assert out["ok"] is True
    assert "failover" in str(out.get("detail") or "")


def test_cloud_provider_chain_free_first():
    s = {
        "advisor_ai_source": "gemini",
        "advisor_ai_api_key_gemini": "g",
        "advisor_ai_api_key_groq": "q",
        "advisor_ai_api_key_openrouter": "r",
        "advisor_ai_failover_enabled": True,
    }
    assert dai.cloud_provider_chain(s) == ["gemini", "groq", "openrouter"]
    assert dai.budget_defaults_for_source("groq") == (20, 400)
    assert dai._model({"advisor_ai_source": "groq"}, provider="groq") == dai._DEFAULT_GROQ_MODEL
    assert "free" in dai._model({}, provider="openrouter")


def test_all_providers_exhausted_local(monkeypatch):
    dai.reset_ai_budgets()

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(dai, "_call_gemini", boom)
    monkeypatch.setattr(dai, "_call_openai", boom)
    settings = {
        "advisor_ai_source": "gemini",
        "advisor_ai_api_key_gemini": "g",
        "advisor_ai_api_key_openai": "o",
        "advisor_ai_failover_order": "gemini,openai",
        "advisor_ai_local_when_clear": False,
    }
    prop = {
        "ticker": "QQQ",
        "broker": "E*TRADE",
        "dollars": 40.0,
        "price": 350.0,
        "score": 68.0,
        "engine": "CORE",
        "asset_type": "stock",
    }
    out = dai.analyze_proposal(prop, {"buying_power": 100.0}, settings)
    assert out["source"] == "local_fallback"
    assert "gemini" in str(out.get("error") or "")
    assert "openai" in str(out.get("error") or "")
def test_connection_reports_every_keyed_provider(monkeypatch):
    calls = []

    def ok_gemini(*_a, **_k):
        calls.append("gemini")
        return '{"verdict":"wait","brief":"gemini ok","detail":"t"}'

    def fail_openai(*_a, **_k):
        calls.append("openai")
        raise RuntimeError("openai bad key")

    monkeypatch.setattr(dai, "_call_gemini", ok_gemini)
    monkeypatch.setattr(dai, "_call_openai", fail_openai)
    settings = {
        "advisor_ai_source": "gemini",
        "advisor_ai_api_key_gemini": "g",
        "advisor_ai_api_key_openai": "o",
        "advisor_ai_failover_order": "gemini,openai",
        "advisor_ai_failover_enabled": True,
    }
    res = dai.test_connection(settings)
    assert calls == ["gemini", "openai"]
    assert res["ok"] is True
    assert res["all_ok"] is False
    assert len(res["results"]) == 2
    assert res["results"][0]["ok"] is True
    assert res["results"][1]["ok"] is False
    assert "1/2" in res["message"]
    assert "[OK] gemini" in res["message"]
    assert "[FAIL] openai" in res["message"]
