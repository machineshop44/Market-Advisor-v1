"""
AI Desk Advisor — plain-English trade briefs + health digests for small-book traders.

Uses your API key (Gemini or OpenAI) from settings.json on the trading PC.
Falls back to local rules when AI is off or the call fails. Never auto-buys.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

VERDICT_APPROVE = "approve"
VERDICT_SKIP = "skip"
VERDICT_WAIT = "wait"

_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
# Preference order when auto-picking from ListModels (newer flash first).
_GEMINI_MODEL_PREFER = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.6-flash",
    "gemini-3-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)

_models_cache: dict[str, tuple[float, list[str]]] = {}
_MODELS_CACHE_TTL = 300.0
_last_gemini_model_used = ""


def resolve_ai_source(settings: dict | None) -> str:
    """One of: local | gemini | openai (pick exactly one for trade briefs)."""
    s = settings or {}
    src = str(s.get("advisor_ai_source") or "").strip().lower()
    if src in ("local", "gemini", "openai"):
        return src
    if not bool(s.get("advisor_ai_enabled", True)):
        return "local"
    prov = str(s.get("advisor_ai_provider") or "gemini").strip().lower()
    return prov if prov in ("gemini", "openai") else "gemini"


def ai_enabled(settings: dict | None) -> bool:
    return resolve_ai_source(settings) != "local"


def ai_configured(settings: dict | None) -> bool:
    s = settings or {}
    return ai_enabled(s) and bool(str(s.get("advisor_ai_api_key") or "").strip())


def _provider(settings: dict | None) -> str:
    src = resolve_ai_source(settings)
    if src in ("gemini", "openai"):
        return src
    return "gemini"


def _model(settings: dict | None) -> str:
    s = settings or {}
    custom = str(s.get("advisor_ai_model") or "").strip()
    if custom:
        return custom
    return _DEFAULT_OPENAI_MODEL if _provider(s) == "openai" else _DEFAULT_GEMINI_MODEL


def _clamp_verdict(v: str) -> str:
    v = str(v or "").strip().lower()
    if v in (VERDICT_APPROVE, VERDICT_SKIP, VERDICT_WAIT):
        return v
    if v in ("buy", "yes", "go"):
        return VERDICT_APPROVE
    if v in ("no", "reject", "pass"):
        return VERDICT_SKIP
    return VERDICT_WAIT


def local_analyze_proposal(proposal: dict, context: dict | None = None) -> dict:
    """Rule-based brief when no API key — still useful for beginners."""
    ctx = context or {}
    tick = str(proposal.get("ticker") or "?").upper()
    broker = str(proposal.get("broker") or "?")
    dollars = float(proposal.get("dollars") or 0)
    price = float(proposal.get("price") or 0)
    score = float(proposal.get("score") or 0)
    engine = str(proposal.get("engine") or "")
    bp = float(ctx.get("buying_power") or 0)
    equity = float(ctx.get("equity") or 0)
    posture = str(ctx.get("posture") or "balanced")
    dd_pause = bool(ctx.get("dd_paused"))
    session = str(ctx.get("session") or "")
    max_sh = float(ctx.get("max_affordable_share_price") or 0)
    deploy = float(ctx.get("deployable_bp") or bp)

    reasons_skip = []
    reasons_ok = []

    for blk in ctx.get("blockers") or []:
        code = str(blk.get("code") or "")
        if code in ("halt", "offline", "reauth", "dd_pause", "low_bp", "regime_equity", "regime_crypto"):
            reasons_skip.append(str(blk.get("message") or code))

    if dd_pause and not any("drawdown" in r.lower() or "dd" in r.lower() for r in reasons_skip):
        reasons_skip.append(f"drawdown pause active ({ctx.get('dd_reason') or 'peak/day DD'})")
    if price > 0 and max_sh > 0 and price > max_sh and "crypto" not in str(proposal.get("asset_type") or "").lower():
        reasons_skip.append(f"${price:.2f}/share above affordable max ~${max_sh:.0f} on {broker}")
    if price > 0 and bp > 0 and price > bp * 0.95 and "crypto" not in str(proposal.get("asset_type") or "").lower():
        reasons_skip.append(f"1 share ~${price:.0f} eats almost all ${bp:.0f} BP on {broker}")
    if dollars > 0 and deploy > 0 and dollars > deploy * 0.92:
        reasons_skip.append(f"ticket ${dollars:.0f} is nearly all deployable BP (~${deploy:.0f})")
    if score < 50:
        reasons_skip.append(f"score {score:.0f} is weak for this book")
    elif score >= 70:
        reasons_ok.append(f"strong scanner score ({score:.0f})")
    if equity > 0 and equity < 500:
        reasons_ok.append("small-book mode — prefer fewer, larger tickets")
    if session and session not in ("REGULAR", "EXTENDED", "OVERNIGHT"):
        reasons_ok.append(f"session {session}")

    if reasons_skip:
        brief = (
            f"Skip {tick}: " + "; ".join(reasons_skip[:2])
            + (f". {engine} signal on {broker}." if engine else ".")
        )
        return {
            "verdict": VERDICT_SKIP,
            "brief": brief[:420],
            "detail": "; ".join(reasons_skip),
            "source": "local",
            "ok": True,
        }

    brief_parts = [f"{broker} {engine or 'scan'} wants ~${dollars:.0f} of {tick}"]
    if reasons_ok:
        brief_parts.append(reasons_ok[0])
    brief_parts.append(f"Posture {posture}. Tap Approve if you want the bot to place it.")
    return {
        "verdict": VERDICT_APPROVE if score >= 55 else VERDICT_WAIT,
        "brief": " ".join(brief_parts)[:420],
        "detail": "; ".join(reasons_ok) or "No hard blockers from local rules.",
        "source": "local",
        "ok": True,
    }


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _proposal_prompt(proposal: dict, context: dict | None) -> str:
    ctx = context or {}
    payload = {
        "proposal": proposal,
        "book": {
            "buying_power": ctx.get("buying_power"),
            "deployable_bp": ctx.get("deployable_bp"),
            "equity": ctx.get("equity"),
            "posture": ctx.get("posture"),
            "dd_paused": ctx.get("dd_paused"),
            "dd_reason": ctx.get("dd_reason"),
            "session": ctx.get("session"),
            "open_positions": ctx.get("open_positions"),
            "max_affordable_share_price": ctx.get("max_affordable_share_price"),
            "small_book": ctx.get("small_book"),
            "engines": ctx.get("engines"),
            "regime": ctx.get("regime"),
            "blockers": ctx.get("blockers"),
            "can_place_new_buy": ctx.get("can_place_new_buy"),
            "auto_ready": ctx.get("auto_ready"),
            "summary": ctx.get("summary"),
        },
        "trader_note": (
            "Beginner trader, small account (~$50–$175). Explain simply. "
            "Never promise profits. Prefer skip when sizing/regime is wrong."
        ),
    }
    return (
        "You are Desk Advisor for a small retail auto-trader. "
        "Reply with ONLY JSON: "
        '{"verdict":"approve|skip|wait","brief":"<=2 sentences plain English",'
        '"detail":"one line why"}'
        f"\n\nData:\n{json.dumps(payload, default=str)[:6000]}"
    )


def _gemini_headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "x-goog-api-key": api_key}


def list_gemini_models(api_key: str, timeout: float = 20.0) -> list[str]:
    """Models this API key can call for generateContent."""
    key = str(api_key or "").strip()
    if not key:
        return []
    cached = _models_cache.get(key)
    now = time.time()
    if cached and now - cached[0] < _MODELS_CACHE_TTL:
        return list(cached[1])

    headers = _gemini_headers(key)
    out: list[str] = []
    page_token = ""
    for _ in range(6):
        params: dict[str, Any] = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers=headers,
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("models") or []:
            methods = item.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = str(item.get("name") or "")
            short = name.rsplit("/", 1)[-1]
            if short and short not in out:
                out.append(short)
        page_token = str(data.get("nextPageToken") or "")
        if not page_token:
            break
    _models_cache[key] = (now, out)
    return out


def _pick_gemini_models(api_key: str, requested: str) -> list[str]:
    """Build try-order: explicit model (if any), then account-available flash models."""
    req = str(requested or "").strip()
    available: list[str] = []
    try:
        available = list_gemini_models(api_key)
    except Exception:
        available = []

    order: list[str] = []
    if req:
        order.append(req)
    if available:
        for m in _GEMINI_MODEL_PREFER:
            if m in available and m not in order:
                order.append(m)
        for m in available:
            if "flash" in m.lower() and m not in order:
                order.append(m)
        for m in available:
            if m not in order:
                order.append(m)
    else:
        for m in _GEMINI_MODEL_PREFER:
            if m not in order:
                order.append(m)
        if _DEFAULT_GEMINI_MODEL not in order:
            order.insert(0, _DEFAULT_GEMINI_MODEL)
    return order


def _gemini_http_error_detail(err: requests.HTTPError) -> str:
    try:
        body = err.response.json()
        msg = body.get("error", {}).get("message")
        if msg:
            return str(msg)[:200]
    except Exception:
        pass
    return str(err)[:200]


def _call_gemini(api_key: str, model: str, prompt: str, timeout: float = 45.0) -> str:
    global _last_gemini_model_used
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512},
    }
    headers = _gemini_headers(api_key)
    last_err: Exception | None = None
    tried: list[str] = []
    for m in _pick_gemini_models(api_key, model):
        tried.append(m)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{m}:generateContent"
        )
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            parts = (
                data.get("candidates") or [{}]
            )[0].get("content", {}).get("parts") or []
            _last_gemini_model_used = m
            return str(parts[0].get("text") or "") if parts else ""
        except requests.HTTPError as e:
            last_err = e
            code = getattr(e.response, "status_code", None)
            if code in (404, 400):
                continue
            raise RuntimeError(_gemini_http_error_detail(e)) from e
        except Exception as e:
            last_err = e
            raise
    hint = ""
    try:
        avail = list_gemini_models(api_key)[:6]
        if avail:
            hint = f" Models on your key: {', '.join(avail)}."
    except Exception:
        pass
    tried_txt = ", ".join(tried[:5])
    if last_err and isinstance(last_err, requests.HTTPError):
        raise RuntimeError(
            f"Gemini could not use any model (tried {tried_txt}). "
            f"{_gemini_http_error_detail(last_err)}.{hint}"
        ) from last_err
    if last_err:
        raise last_err
    raise RuntimeError(f"Gemini returned no response (tried {tried_txt}).{hint}")


def _call_openai(api_key: str, model: str, prompt: str, timeout: float = 45.0) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 512,
        "messages": [
            {"role": "system", "content": "Reply with JSON only. No markdown."},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("message", {}).get("content") or "")


def analyze_proposal(
    proposal: dict,
    context: dict | None = None,
    settings: dict | None = None,
) -> dict:
    """Return {verdict, brief, detail, source, ok, error?}."""
    if not ai_configured(settings):
        return local_analyze_proposal(proposal, context)

    key = str((settings or {}).get("advisor_ai_api_key") or "").strip()
    provider = _provider(settings)
    model = _model(settings)
    prompt = _proposal_prompt(proposal, context)
    try:
        raw = _call_openai(key, model, prompt) if provider == "openai" else _call_gemini(key, model, prompt)
        parsed = _extract_json_object(raw)
        if not parsed:
            out = local_analyze_proposal(proposal, context)
            out["source"] = "local_fallback"
            out["error"] = "AI response not JSON"
            return out
        return {
            "verdict": _clamp_verdict(parsed.get("verdict")),
            "brief": str(parsed.get("brief") or "")[:500],
            "detail": str(parsed.get("detail") or "")[:500],
            "source": provider,
            "ok": True,
        }
    except Exception as e:
        out = local_analyze_proposal(proposal, context)
        out["source"] = "local_fallback"
        out["error"] = str(e)[:200]
        return out


def analyze_desk_health(
    log_lines: list | None,
    snapshot: dict | None,
    settings: dict | None = None,
) -> dict:
    """Short desk health line for monitor API / optional AI digest."""
    lines = [str(x) for x in (log_lines or []) if x][-25:]
    snap = snapshot or {}
    issues = []
    for ln in reversed(lines):
        low = ln.lower()
        if "buy batch error" in low or "ui build error" in low:
            issues.append(ln[-160:])
            break
        if "[dd]" in low and "pausing new buys" in low:
            issues.append("Drawdown pause blocking new buys")
            break
        if "unaffordable" in low or "0 actionable" in low:
            issues.append("Signals exist but book cannot fund them")
            break
    if snap.get("halted"):
        issues.append("Panic halt is ON")
    if not issues:
        return {
            "status": "ok",
            "brief": "Desk running — no critical issues in recent log.",
            "source": "local",
            "ok": True,
            "at": time.time(),
        }

    brief = issues[0]
    if ai_configured(settings):
        try:
            key = str((settings or {}).get("advisor_ai_api_key") or "").strip()
            provider = _provider(settings)
            model = _model(settings)
            prompt = (
                "Summarize this trading app issue for a beginner in ONE sentence. "
                f"Issue context: {brief}. Recent log tail:\n" + "\n".join(lines[-8:])
            )
            raw = _call_openai(key, model, prompt) if provider == "openai" else _call_gemini(key, model, prompt)
            if raw.strip():
                return {
                    "status": "warn",
                    "brief": raw.strip()[:400],
                    "source": provider,
                    "ok": True,
                    "at": time.time(),
                }
        except Exception:
            pass

    return {
        "status": "warn",
        "brief": brief[:400],
        "source": "local",
        "ok": True,
        "at": time.time(),
    }


def test_connection(settings: dict | None) -> dict:
    """Settings → Test AI — lightweight ping."""
    if not ai_configured(settings):
        return {"ok": False, "error": "Enable AI and enter an API key first."}
    key = str((settings or {}).get("advisor_ai_api_key") or "").strip()
    provider = _provider(settings)
    model = _model(settings)
    prompt = 'Reply JSON only: {"verdict":"wait","brief":"AI connection OK","detail":"test"}'
    try:
        if provider == "openai":
            raw = _call_openai(key, model, prompt, timeout=30.0)
            used_model = model
        else:
            raw = _call_gemini(key, model, prompt, timeout=30.0)
            used_model = _last_gemini_model_used or model
        parsed = _extract_json_object(raw)
        if parsed and parsed.get("brief"):
            return {
                "ok": True,
                "message": str(parsed.get("brief")),
                "provider": provider,
                "model": used_model,
            }
        return {
            "ok": True,
            "message": "Connected (response received).",
            "provider": provider,
            "model": used_model,
        }
    except Exception as e:
        msg = str(e)[:320]
        if provider == "gemini" and "404" in msg:
            msg += " Leave Model blank to auto-pick from your Google account."
        return {"ok": False, "error": msg}
