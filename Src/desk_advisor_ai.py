"""
AI Desk Advisor — plain-English trade briefs + health digests for small-book traders.

Uses your API key (Gemini or OpenAI) from settings.json on the trading PC.
Falls back to local rules when AI is off or the call fails.

When ask-before-apply is on, the desk auto-applies approve/skip under hard rails
(DD pause, halt, min ticket) so beginners do not babysit every ticket.
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

# Free-tier Gemini budgets (override via settings)
_ai_call_times: list[float] = []
_ai_day_key: str = ""
_ai_day_count: int = 0


def _ai_budget_ok(settings: dict | None) -> tuple[bool, str]:
    """Enforce per-minute / per-day caps so free Gemini isn't burned in one burst."""
    global _ai_day_key, _ai_day_count
    s = settings or {}
    try:
        per_min = int(s.get("advisor_ai_max_per_minute") or 4)
    except (TypeError, ValueError):
        per_min = 4
    try:
        per_day = int(s.get("advisor_ai_max_per_day") or 20)
    except (TypeError, ValueError):
        per_day = 20
    per_min = max(1, min(60, per_min))
    per_day = max(1, min(500, per_day))
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    if day != _ai_day_key:
        _ai_day_key = day
        _ai_day_count = 0
    if _ai_day_count >= per_day:
        return False, f"daily AI budget ({per_day}/day) exhausted — local rules"
    while _ai_call_times and now - _ai_call_times[0] > 60.0:
        _ai_call_times.pop(0)
    if len(_ai_call_times) >= per_min:
        return False, f"per-minute AI budget ({per_min}/min) — local rules"
    return True, ""


def _ai_budget_record():
    global _ai_day_count
    _ai_call_times.append(time.time())
    _ai_day_count += 1


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


def resolve_api_key(settings: dict | None = None) -> str:
    """Prefer OS keyring; fall back to settings.json plaintext (legacy)."""
    try:
        import credentials as cred

        return cred.resolve_advisor_api_key(settings)
    except Exception:
        return str((settings or {}).get("advisor_ai_api_key") or "").strip()


def ai_configured(settings: dict | None) -> bool:
    s = settings or {}
    return ai_enabled(s) and bool(resolve_api_key(s))


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


_research_cache: dict[str, tuple[float, dict]] = {}
_RESEARCH_TTL_SEC = 120.0


def build_research_pack(proposal: dict, context: dict | None = None) -> dict:
    """
    Fresh facts for Gemini — not model web search. We gather; the model judges.
    Cached ~2 min per ticker so auto-apply bursts stay snappy.
    """
    tick = str(proposal.get("ticker") or "").replace("-USD", "").upper().strip()
    if not tick:
        return {"ok": False, "notes": ["no ticker"]}
    now = time.time()
    hit = _research_cache.get(tick)
    if hit and now - float(hit[0] or 0.0) < _RESEARCH_TTL_SEC:
        return dict(hit[1])

    ctx = context or {}
    is_crypto = "crypto" in str(proposal.get("asset_type") or "").lower() or bool(
        proposal.get("is_crypto")
    )
    yahoo = f"{tick}-USD" if is_crypto else tick
    pack: dict[str, Any] = {
        "ticker": tick,
        "yahoo_symbol": yahoo,
        "ok": True,
        "price_action": {},
        "book_history": {},
        "regime": ctx.get("regime"),
        "notes": [],
    }

    # Price / volume — short history only (worker thread; still keep it light)
    try:
        import yfinance as yf

        df = yf.Ticker(yahoo).history(period="5d", interval="1d")
        if df is not None and len(df) >= 2:
            closes = [float(x) for x in df["Close"].tolist() if x == x]
            vols = []
            if "Volume" in df.columns:
                vols = [float(x) for x in df["Volume"].tolist() if x == x]
            if len(closes) >= 2 and closes[-2] > 0:
                chg = (closes[-1] / closes[-2] - 1.0) * 100.0
                pack["price_action"]["last_close"] = round(closes[-1], 6)
                pack["price_action"]["day_chg_pct"] = round(chg, 2)
                pack["notes"].append(f"1d change {chg:+.1f}%")
            if len(closes) >= 3 and closes[0] > 0:
                chg5 = (closes[-1] / closes[0] - 1.0) * 100.0
                pack["price_action"]["chg_5d_pct"] = round(chg5, 2)
                pack["notes"].append(f"~5d change {chg5:+.1f}%")
            if len(vols) >= 2 and vols[-2] > 0:
                vratio = vols[-1] / vols[-2]
                pack["price_action"]["vol_vs_prior"] = round(vratio, 2)
                if vratio >= 1.5:
                    pack["notes"].append("volume elevated vs prior day")
                elif vratio <= 0.6:
                    pack["notes"].append("volume light vs prior day")
        else:
            pack["notes"].append("no recent daily bars")
    except Exception as e:
        pack["notes"].append(f"price fetch limited: {str(e)[:80]}")

    # Our own fills for this name — cheap "did we already get burned?" signal
    try:
        import journal as journal_mod

        rows = journal_mod.read_since_days(days=14, limit=1500)
        buys = sells = 0
        pnlish = 0.0
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").replace("-USD", "").upper()
            if t != tick:
                continue
            side = str(r.get("side") or r.get("action") or "").upper()
            if side.startswith("BUY") or side == "BUY":
                buys += 1
            elif side.startswith("SELL") or side == "SELL":
                sells += 1
            try:
                pnlish += float(r.get("pnl") or r.get("realized_pnl") or 0.0)
            except (TypeError, ValueError):
                pass
        pack["book_history"] = {
            "buys_14d": buys,
            "sells_14d": sells,
            "realized_pnl_hint": round(pnlish, 2),
        }
        if buys or sells:
            pack["notes"].append(
                f"desk traded {tick} {buys} buys / {sells} sells in 14d"
                + (f" · pnl hint ${pnlish:+.2f}" if abs(pnlish) > 0.01 else "")
            )
        else:
            pack["notes"].append(f"no desk fills on {tick} in 14d")
    except Exception:
        pack["notes"].append("journal history unavailable")

    # Proposal / scanner crumbs already computed locally
    try:
        score = float(proposal.get("score") or 0)
        pack["scanner_score"] = round(score, 1)
    except (TypeError, ValueError):
        pass
    if proposal.get("regime_caution"):
        pack["notes"].append("local regime caution on this name")

    _research_cache[tick] = (now, pack)
    return dict(pack)


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
    regime_caution = bool(proposal.get("regime_caution"))

    for blk in ctx.get("blockers") or []:
        code = str(blk.get("code") or "")
        msg = str(blk.get("message") or code)
        if code in ("halt", "offline", "reauth", "dd_pause", "low_bp"):
            reasons_skip.append(msg)
        elif code in ("regime_equity", "regime_crypto"):
            if regime_caution:
                reasons_ok.append(f"SPY/BTC gate blocked scan — approve to override ({msg})")
            else:
                reasons_skip.append(msg)

    if dd_pause and not any("drawdown" in r.lower() or "dd" in r.lower() for r in reasons_skip):
        reasons_skip.append(f"drawdown pause active ({ctx.get('dd_reason') or 'peak/day DD'})")
    if price > 0 and max_sh > 0 and price > max_sh and "crypto" not in str(proposal.get("asset_type") or "").lower():
        reasons_skip.append(f"${price:.2f}/share above affordable max ~${max_sh:.0f} on {broker}")
    if price > 0 and bp > 0 and price > bp * 0.95 and "crypto" not in str(proposal.get("asset_type") or "").lower():
        reasons_skip.append(f"1 share ~${price:.0f} eats almost all ${bp:.0f} BP on {broker}")
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
    brief_parts.append(f"Posture {posture}. Desk will auto-apply if safety rails OK.")
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


def _proposal_prompt(
    proposal: dict,
    context: dict | None,
    research: dict | None = None,
) -> str:
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
        "research": research or {},
        "trader_note": (
            "Beginner auto-pilot desk. Small account. Explain simply. "
            "Your verdict may auto-execute under hard rails (DD/halt/min ticket). "
            "Be conservative: prefer skip/wait when unsure. Never override DD or halt. "
            "Prefer fundable ticket sizes for the book. "
            "Use the research block (price action + desk fill history) — do not invent "
            "headlines or numbers that are not in the data. Cite one research fact in detail."
        ),
    }
    return (
        "You are Desk Advisor for a beginner retail auto-trader (auto-pilot). "
        "You receive a live research pack gathered by the app (not your own browsing). "
        "Judge the trade using proposal + book + research. "
        "Reply with ONLY JSON: "
        '{"verdict":"approve|skip|wait","brief":"<=2 sentences plain English",'
        '"detail":"one line why (include one research fact)"}'
        f"\n\nData:\n{json.dumps(payload, default=str)[:7500]}"
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

    # Clear local cases skip the cloud call (saves free-tier quota)
    if bool((settings or {}).get("advisor_ai_local_when_clear", True)):
        local = local_analyze_proposal(proposal, context)
        score = float(proposal.get("score") or 0)
        if local.get("verdict") == VERDICT_SKIP and score < 55:
            local["detail"] = (
                str(local.get("detail") or "") + " · cloud skipped (clear local skip)"
            ).strip(" ·")[:500]
            return local
        if (
            local.get("verdict") == VERDICT_APPROVE
            and score >= 90
            and not bool(proposal.get("regime_caution"))
        ):
            local["detail"] = (
                str(local.get("detail") or "") + " · cloud skipped (clear local approve)"
            ).strip(" ·")[:500]
            return local

    ok_budget, budget_why = _ai_budget_ok(settings)
    if not ok_budget:
        out = local_analyze_proposal(proposal, context)
        out["source"] = "local_fallback"
        out["error"] = budget_why
        detail = str(out.get("detail") or "").strip()
        out["detail"] = (f"{detail} · {budget_why}" if detail else budget_why)[:500]
        return out

    key = resolve_api_key(settings)
    provider = _provider(settings)
    model = _model(settings)
    research = build_research_pack(proposal, context)
    prompt = _proposal_prompt(proposal, context, research=research)
    try:
        raw = _call_openai(key, model, prompt) if provider == "openai" else _call_gemini(key, model, prompt)
        _ai_budget_record()
        parsed = _extract_json_object(raw)
        if not parsed:
            out = local_analyze_proposal(proposal, context)
            out["source"] = "local_fallback"
            out["error"] = "AI response not JSON"
            detail = str(out.get("detail") or "").strip()
            out["detail"] = (
                f"{detail} · cloud fail: AI response not JSON"
                if detail else "cloud fail: AI response not JSON"
            )[:500]
            out["research"] = research
            return out
        return {
            "verdict": _clamp_verdict(parsed.get("verdict")),
            "brief": str(parsed.get("brief") or "")[:500],
            "detail": str(parsed.get("detail") or "")[:500],
            "source": provider,
            "ok": True,
            "research": research,
        }
    except Exception as e:
        out = local_analyze_proposal(proposal, context)
        out["source"] = "local_fallback"
        err = str(e)[:200]
        out["error"] = err
        # Keep local brief; append why cloud failed so Journal/Activity can show it
        detail = str(out.get("detail") or "").strip()
        out["detail"] = (f"{detail} · cloud fail: {err}" if detail else f"cloud fail: {err}")[:500]
        out["research"] = research
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
        ok_budget, _why = _ai_budget_ok(settings)
        if ok_budget:
            try:
                key = resolve_api_key(settings)
                provider = _provider(settings)
                model = _model(settings)
                prompt = (
                    "Summarize this trading app issue for a beginner in ONE sentence. "
                    f"Issue context: {brief}. Recent log tail:\n" + "\n".join(lines[-8:])
                )
                raw = (
                    _call_openai(key, model, prompt)
                    if provider == "openai"
                    else _call_gemini(key, model, prompt)
                )
                _ai_budget_record()
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
    key = resolve_api_key(settings)
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
