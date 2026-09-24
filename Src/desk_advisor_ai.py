"""
AI Desk Advisor — plain-English trade briefs + health digests for small-book traders.

Uses API keys on the trading PC. Preferred provider tries first; remaining keys failover
in order; local rules when the chain is exhausted.

Free-tier friendly: Gemini, Groq (console.groq.com), OpenRouter free models.
Paid optional: OpenAI, xAI Grok (console.x.ai). Cursor's bundled Grok chat is separate —
the desk cannot use Cursor's key.

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

# Free-first defaults; paid OpenAI/xAI still work when keys are present.
CLOUD_PROVIDERS = ("gemini", "groq", "openrouter", "openai", "xai")
_DEFAULT_FAILOVER_ORDER = "gemini,groq,openrouter,openai,xai"

_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
# Free-tier Groq: prefer Instant / GPT-OSS — llama-3.3-70b is often Enterprise-only (404).
_DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"
_GROQ_MODEL_FALLBACKS = (
    "llama-3.1-8b-instant",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
)
# OpenRouter :free catalog rotates — keep several current free ids.
_DEFAULT_OPENROUTER_MODEL = "google/gemma-4-26b-a4b-it:free"
_OPENROUTER_MODEL_FALLBACKS = (
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3.5-lightning:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
)
# Tool-capable default so Grok can web/X-search on top of our research pack.
_DEFAULT_XAI_MODEL = "grok-4.3"
_XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
_XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
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
_last_compat_model_used = ""

# Per-provider call budgets (override via settings caps)
_ai_budgets: dict[str, dict[str, Any]] = {}
# Legacy aliases kept for older tests that clear module globals directly
_ai_call_times: list[float] = []
_ai_day_key: str = ""
_ai_day_count: int = 0


def normalize_cloud_provider(provider: str | None) -> str:
    p = str(provider or "").strip().lower()
    if p == "grok":
        return "xai"
    return p if p in CLOUD_PROVIDERS else ""


def budget_defaults_for_source(source: str) -> tuple[int, int]:
    """(per_minute, per_day) sensible caps by provider."""
    src = normalize_cloud_provider(source) or str(source or "").strip().lower()
    if src == "xai":
        # Paid xAI — day trader desk needs many briefs; not free-Gemini 20/day.
        return 20, 400
    if src == "openai":
        return 10, 120
    if src == "groq":
        # Free plan often ~30 RPM / high RPD — stay conservative for JSON briefs.
        return 20, 400
    if src == "openrouter":
        # Free models vary; keep modest to avoid 429 storms.
        return 8, 80
    # Gemini free-tier caution
    return 4, 20


def reset_ai_budgets() -> None:
    """Test helper — clear all provider budget slots."""
    global _ai_call_times, _ai_day_key, _ai_day_count
    _ai_budgets.clear()
    _ai_call_times = []
    _ai_day_key = ""
    _ai_day_count = 0


def _budget_slot(provider: str | None) -> dict[str, Any]:
    p = normalize_cloud_provider(provider) or "default"
    slot = _ai_budgets.get(p)
    if slot is None:
        slot = {"times": [], "day_key": "", "day_count": 0}
        _ai_budgets[p] = slot
    return slot


def _sync_legacy_budget_globals(slot: dict[str, Any]) -> None:
    global _ai_call_times, _ai_day_key, _ai_day_count
    _ai_call_times = slot["times"]
    _ai_day_key = slot["day_key"]
    _ai_day_count = int(slot["day_count"] or 0)


def _ai_budget_limits(settings: dict | None, provider: str | None) -> tuple[int, int]:
    """Per-provider soft caps (failover skips exhausted providers). No shared UI knobs."""
    s = settings or {}
    src = normalize_cloud_provider(provider) or resolve_ai_source(s)
    per_min, per_day = budget_defaults_for_source(src)
    p = normalize_cloud_provider(provider)
    if p:
        try:
            if s.get(f"advisor_ai_max_per_minute_{p}") is not None:
                per_min = int(s.get(f"advisor_ai_max_per_minute_{p}"))
        except (TypeError, ValueError):
            pass
        try:
            if s.get(f"advisor_ai_max_per_day_{p}") is not None:
                per_day = int(s.get(f"advisor_ai_max_per_day_{p}"))
        except (TypeError, ValueError):
            pass
    per_min = max(1, min(120, per_min))
    per_day = max(1, min(5000, per_day))
    return per_min, per_day


def _ai_budget_ok(
    settings: dict | None, provider: str | None = None,
) -> tuple[bool, str]:
    """Enforce per-minute / per-day caps (per cloud provider)."""
    # Honor legacy tests that mutate module globals directly
    p = normalize_cloud_provider(provider) or resolve_ai_source(settings)
    slot = _budget_slot(p)
    if provider is None and _ai_call_times is not slot["times"]:
        # Tests may have cleared/replaced the legacy list — prefer it for default slot
        slot["times"] = _ai_call_times
        slot["day_key"] = _ai_day_key
        slot["day_count"] = _ai_day_count
    per_min, per_day = _ai_budget_limits(settings, p)
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    if day != slot["day_key"]:
        slot["day_key"] = day
        slot["day_count"] = 0
    if int(slot["day_count"] or 0) >= per_day:
        _sync_legacy_budget_globals(slot)
        return False, f"{p or 'ai'} daily budget ({per_day}/day) exhausted"
    times = slot["times"]
    while times and now - times[0] > 60.0:
        times.pop(0)
    if len(times) >= per_min:
        _sync_legacy_budget_globals(slot)
        return False, f"{p or 'ai'} per-minute budget ({per_min}/min)"
    _sync_legacy_budget_globals(slot)
    return True, ""


def _ai_budget_record(provider: str | None = None):
    p = normalize_cloud_provider(provider) or resolve_ai_source(None) or "default"
    slot = _budget_slot(p)
    if provider is None and _ai_call_times is not slot["times"]:
        slot["times"] = _ai_call_times
        slot["day_key"] = _ai_day_key
        slot["day_count"] = _ai_day_count
    slot["times"].append(time.time())
    slot["day_count"] = int(slot["day_count"] or 0) + 1
    _sync_legacy_budget_globals(slot)


def resolve_ai_source(settings: dict | None) -> str:
    """Preferred source: local | gemini | groq | openrouter | openai | xai."""
    s = settings or {}
    src = str(s.get("advisor_ai_source") or "").strip().lower()
    if src == "grok":
        return "xai"
    if src == "local" or src in CLOUD_PROVIDERS:
        return src
    if not bool(s.get("advisor_ai_enabled", True)):
        return "local"
    prov = str(s.get("advisor_ai_provider") or "gemini").strip().lower()
    if prov == "grok":
        prov = "xai"
    return prov if prov in CLOUD_PROVIDERS else "gemini"


def ai_enabled(settings: dict | None) -> bool:
    return resolve_ai_source(settings) != "local"


def parse_failover_order(settings: dict | None) -> list[str]:
    """Ordered cloud providers for failover (unique, valid only)."""
    s = settings or {}
    raw = str(s.get("advisor_ai_failover_order") or _DEFAULT_FAILOVER_ORDER)
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        p = normalize_cloud_provider(part)
        if p and p not in out:
            out.append(p)
    for p in CLOUD_PROVIDERS:
        if p not in out:
            out.append(p)
    return out


def resolve_api_key(settings: dict | None = None) -> str:
    """Legacy shared key (primary / old single-key installs)."""
    try:
        import credentials as cred

        return cred.resolve_advisor_api_key(settings)
    except Exception:
        return str((settings or {}).get("advisor_ai_api_key") or "").strip()


def resolve_provider_api_key(provider: str | None, settings: dict | None = None) -> str:
    """Per-provider key, then legacy shared key when provider is preferred source."""
    p = normalize_cloud_provider(provider)
    if not p:
        return ""
    s = settings or {}
    try:
        import credentials as cred

        key = cred.resolve_advisor_api_key_for_provider(p, s)
        if key:
            return key
    except Exception:
        pass
    key = str(s.get(f"advisor_ai_api_key_{p}") or "").strip()
    if key:
        return key
    if resolve_ai_source(s) == p:
        return resolve_api_key(s)
    return ""


def cloud_provider_chain(settings: dict | None) -> list[str]:
    """Providers to try, preferred first, only those with a key."""
    s = settings or {}
    if not ai_enabled(s):
        return []
    primary = resolve_ai_source(s)
    order = parse_failover_order(s)
    chain: list[str] = []
    if primary in CLOUD_PROVIDERS:
        chain.append(primary)
    failover_on = bool(s.get("advisor_ai_failover_enabled", True))
    if failover_on:
        for p in order:
            if p not in chain:
                chain.append(p)
    else:
        chain = [primary] if primary in CLOUD_PROVIDERS else []
    return [p for p in chain if resolve_provider_api_key(p, s)]


def ai_configured(settings: dict | None) -> bool:
    s = settings or {}
    if not ai_enabled(s):
        return False
    if cloud_provider_chain(s):
        return True
    # Single legacy key still counts when preferred is cloud
    return resolve_ai_source(s) in CLOUD_PROVIDERS and bool(resolve_api_key(s))


def _provider(settings: dict | None) -> str:
    src = resolve_ai_source(settings)
    if src in CLOUD_PROVIDERS:
        return src
    chain = cloud_provider_chain(settings)
    return chain[0] if chain else "gemini"


def _model(settings: dict | None, provider: str | None = None) -> str:
    """Per-provider default model. Optional advisor_ai_model_<provider> override only."""
    s = settings or {}
    p = normalize_cloud_provider(provider) or _provider(s)
    # Per-provider override only — never reuse a shared Gemini model id on Groq/OpenAI/etc.
    custom = str(s.get(f"advisor_ai_model_{p}") or "").strip()
    if custom:
        return custom
    if p == "openai":
        return _DEFAULT_OPENAI_MODEL
    if p == "xai":
        return _DEFAULT_XAI_MODEL
    if p == "groq":
        return _DEFAULT_GROQ_MODEL
    if p == "openrouter":
        return _DEFAULT_OPENROUTER_MODEL
    return _DEFAULT_GEMINI_MODEL


def _clamp_verdict(v: str) -> str:
    v = str(v or "").strip().lower()
    if v in (VERDICT_APPROVE, VERDICT_SKIP, VERDICT_WAIT):
        return v
    if v in ("buy", "yes", "go"):
        return VERDICT_APPROVE
    if v in ("no", "reject", "pass"):
        return VERDICT_SKIP
    return VERDICT_WAIT


# Re-ask window after skip/wait (minutes). Desk honors this before proposing again.
_RETRY_AFTER_MIN_DEFAULT_SKIP = 20
_RETRY_AFTER_MIN_DEFAULT_WAIT = 12
_RETRY_AFTER_MIN_FLOOR = 5
_RETRY_AFTER_MIN_CAP_SKIP = 180
_RETRY_AFTER_MIN_CAP_WAIT = 60


def clamp_retry_after_min(value, *, verdict: str = "") -> int:
    """Sanitize advisor-provided wait minutes before re-asking the same name."""
    v = str(verdict or "").strip().lower()
    if v == VERDICT_APPROVE:
        return 0
    try:
        mins = int(float(value))
    except (TypeError, ValueError):
        mins = 0
    if mins <= 0:
        mins = (
            _RETRY_AFTER_MIN_DEFAULT_WAIT
            if v == VERDICT_WAIT
            else _RETRY_AFTER_MIN_DEFAULT_SKIP
        )
    cap = _RETRY_AFTER_MIN_CAP_WAIT if v == VERDICT_WAIT else _RETRY_AFTER_MIN_CAP_SKIP
    return max(_RETRY_AFTER_MIN_FLOOR, min(int(mins), int(cap)))


def default_retry_after_min(
    *,
    verdict: str,
    reasons: list | None = None,
    proposal: dict | None = None,
) -> int:
    """Local heuristic when the model did not (or cannot) pick a wait window."""
    v = str(verdict or "").strip().lower()
    if v == VERDICT_APPROVE:
        return 0
    blob = " ".join(str(r) for r in (reasons or [])).lower()
    prop = proposal or {}
    if bool(prop.get("regime_caution")) or "regime" in blob or "downtrend" in blob:
        return 45  # SPY/BTC gates do not flip every pulse
    if "drawdown" in blob or "dd pause" in blob or "dd_pause" in blob:
        return 60
    if "halt" in blob or "offline" in blob or "reauth" in blob:
        return 15
    if "affordable" in blob or "eats almost" in blob or "above affordable" in blob:
        return 60
    if "weak" in blob or "score" in blob:
        return 20
    if v == VERDICT_WAIT:
        return _RETRY_AFTER_MIN_DEFAULT_WAIT
    return _RETRY_AFTER_MIN_DEFAULT_SKIP


def _with_retry_after(out: dict, proposal: dict | None = None, reasons: list | None = None) -> dict:
    """Ensure every analyze result carries retry_after_min for desk cooldowns."""
    if not isinstance(out, dict):
        return out
    verdict = _clamp_verdict(out.get("verdict"))
    raw = out.get("retry_after_min")
    if raw is None or raw == "":
        mins = default_retry_after_min(
            verdict=verdict, reasons=reasons, proposal=proposal,
        )
    else:
        mins = clamp_retry_after_min(raw, verdict=verdict)
        # If model returned junk that collapsed to default, still ok
    if verdict == VERDICT_APPROVE:
        out["retry_after_min"] = 0
    else:
        out["retry_after_min"] = int(mins)
    return out


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
    if not is_crypto:
        try:
            from crypto_symbols import KNOWN_CRYPTOS
            if tick in KNOWN_CRYPTOS:
                is_crypto = True
        except Exception:
            pass
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
    allow_regime = bool(ctx.get("allow_buys_when_regime_blocked"))
    asset_l = str(proposal.get("asset_type") or "").lower()
    engine_l = str(proposal.get("engine") or "").lower()
    try:
        from crypto_symbols import KNOWN_CRYPTOS as _KNOWN_CRYPTOS
    except Exception:
        _KNOWN_CRYPTOS = frozenset(
            {"BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "DOT", "AVAX", "LINK"}
        )
    is_crypto_prop = (
        "crypto" in asset_l
        or engine_l == "crypto"
        or tick in _KNOWN_CRYPTOS
    )

    if regime_caution and not allow_regime:
        reasons_skip.append("Regime: SPY/BTC gate blocked (override off)")

    for blk in ctx.get("blockers") or []:
        code = str(blk.get("code") or "")
        msg = str(blk.get("message") or code)
        if code in ("halt", "offline", "reauth", "dd_pause", "low_bp"):
            reasons_skip.append(msg)
        elif code == "regime_equity":
            # SPY gate applies to equities only — don't skip crypto on SPY disagree
            if is_crypto_prop:
                continue
            if regime_caution and allow_regime:
                reasons_ok.append(
                    f"SPY gate blocked scan — manual/AI override only ({msg})"
                )
            else:
                reasons_skip.append(msg)
        elif code == "regime_crypto":
            # BTC gate applies to crypto only
            if not is_crypto_prop:
                continue
            if regime_caution and allow_regime:
                reasons_ok.append(
                    f"BTC gate blocked scan — manual/AI override only ({msg})"
                )
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
        retry = default_retry_after_min(
            verdict=VERDICT_SKIP, reasons=reasons_skip, proposal=proposal,
        )
        return _with_retry_after(
            {
                "verdict": VERDICT_SKIP,
                "brief": brief[:420],
                "detail": "; ".join(reasons_skip),
                "source": "local",
                "ok": True,
                "retry_after_min": retry,
            },
            proposal=proposal,
            reasons=reasons_skip,
        )

    brief_parts = [f"{broker} {engine or 'scan'} wants ~${dollars:.0f} of {tick}"]
    if reasons_ok:
        brief_parts.append(reasons_ok[0])
    brief_parts.append(f"Posture {posture}. Desk will auto-apply if safety rails OK.")
    # Autopilot bar: ≥70 clear approve; 55–69 wait for cloud/human (was ≥55).
    if score >= 70:
        verdict = VERDICT_APPROVE
    elif score >= 55:
        verdict = VERDICT_WAIT
    else:
        verdict = VERDICT_WAIT
    return _with_retry_after(
        {
            "verdict": verdict,
            "brief": " ".join(brief_parts)[:420],
            "detail": (
                "; ".join(reasons_ok)
                or (
                    f"Score {score:.0f} needs ≥70 for local auto-approve"
                    if score < 70
                    else "No hard blockers from local rules."
                )
            ),
            "source": "local",
            "ok": True,
            "retry_after_min": default_retry_after_min(
                verdict=verdict, reasons=reasons_ok, proposal=proposal,
            ),
        },
        proposal=proposal,
        reasons=reasons_ok,
    )


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
    *,
    live_research: bool = False,
) -> str:
    ctx = context or {}
    tick = str(proposal.get("ticker") or "?").upper()
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
        "app_research_pack": research or {},
        "trader_note": (
            "Beginner auto-pilot desk. Small account. Explain simply. "
            "Your verdict may auto-execute under hard rails (DD/halt/min ticket). "
            "Be conservative: prefer skip/wait when unsure. Never override DD or halt. "
            "Prefer fundable ticket sizes for the book. "
            + (
                "You have live web_search and x_search tools — USE them for fresh news, "
                f"catalysts, tape, and sentiment on {tick} in addition to app_research_pack. "
                "Do not invent facts; cite one app fact and one live-search fact when possible. "
                if live_research
                else
                "Use app_research_pack (price action + desk fill history) — do not invent "
                "headlines or numbers that are not in the data. Cite one research fact in detail. "
            )
            + "CRITICAL: when verdict is skip or wait, set retry_after_min to how many minutes "
            f"the desk must wait before asking again about THIS exact BUY of {tick} "
            "(same ticker on this broker). Guidelines: regime/downtrend 30-60; weak score 15-25; "
            "temporary wait 8-15; structural unaffordable/halt 45-90; bad catalyst/news 45-120. "
            "approve → retry_after_min 0. Integer minutes only."
        ),
    }
    research_line = (
        "You may call web_search / x_search before deciding. "
        if live_research
        else "Live browsing is OFF for this call — use only the provided pack. "
    )
    return (
        "You are Desk Advisor for a beginner retail day-trader (auto-pilot). "
        + research_line
        + "Judge the trade using proposal + book + app_research_pack"
        + (" + your live search" if live_research else "")
        + ". "
        "Reply with ONLY JSON (no markdown): "
        '{"verdict":"approve|skip|wait","brief":"short plain-English reason",'
        '"detail":"one line why (cite app + live fact if searched)",'
        '"retry_after_min":0}'
        " — brief must be your decision text (never echo schema instructions). "
        "retry_after_min REQUIRED on skip/wait (minutes before re-asking this ticker); 0 on approve."
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


def _http_error_detail(err: Exception, *, provider: str = "") -> str:
    """Prefer API JSON error text over bare status lines."""
    resp = getattr(err, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    body = ""
    if resp is not None:
        try:
            data = resp.json()
            if isinstance(data, dict):
                err_obj = data.get("error")
                if isinstance(err_obj, dict):
                    body = str(
                        err_obj.get("message")
                        or err_obj.get("metadata", {}).get("raw")
                        or err_obj.get("code")
                        or ""
                    )
                elif err_obj:
                    body = str(err_obj)
                if not body:
                    body = str(data.get("message") or "")[:240]
        except Exception:
            try:
                body = str(resp.text or "")[:240]
            except Exception:
                body = ""
    base = body.strip() or str(err)[:240]
    p = str(provider or "").strip().lower()
    low = base.lower()
    # Avoid false positives from substring "rate" inside unrelated words.
    soft_limit = (
        status == 429
        or "rate limit" in low
        or "quota" in low
        or "too many requests" in low
        or "no credits" in low
    )
    if soft_limit:
        return f"{base} · wait / use next failover key"
    if status == 404 and p in ("groq", "openrouter", "openai", "xai"):
        return f"{base} · model unavailable on this key — trying fallbacks"
    if p == "openrouter" and (
        "provider returned error" in low or "no endpoints" in low
    ):
        return f"{base} · free model busy/offline — trying another"
    return base


def list_openrouter_free_models(api_key: str = "", timeout: float = 20.0) -> list[str]:
    """Live :free model ids from OpenRouter (catalog rotates often)."""
    cache_key = "openrouter_free"
    now = time.time()
    hit = _models_cache.get(cache_key)
    if hit and now - hit[0] < _MODELS_CACHE_TTL:
        return list(hit[1])
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/models", headers=headers, timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return list(_OPENROUTER_MODEL_FALLBACKS)
    ids: list[str] = []
    for m in data.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if mid.endswith(":free"):
            ids.append(mid)
    # Prefer compact instruction-ish free models first
    prefer_bits = ("gemma", "glm", "nemotron", "llama", "qwen", "deepseek", "mistral")
    ranked = sorted(
        ids,
        key=lambda x: (
            0 if any(b in x.lower() for b in prefer_bits) else 1,
            len(x),
            x,
        ),
    )
    out = ranked[:12] or list(_OPENROUTER_MODEL_FALLBACKS)
    _models_cache[cache_key] = (now, out)
    return out


def _openai_compatible_model_chain(
    provider: str, preferred: str | None = None, api_key: str = "",
) -> list[str]:
    p = normalize_cloud_provider(provider)
    if p == "groq":
        pool = list(_GROQ_MODEL_FALLBACKS)
    elif p == "openrouter":
        live = list_openrouter_free_models(api_key)
        pool = live + [m for m in _OPENROUTER_MODEL_FALLBACKS if m not in live]
    else:
        pool = []
    out: list[str] = []
    pref = str(preferred or "").strip()
    if pref:
        out.append(pref)
    for m in pool:
        if m and m not in out:
            out.append(m)
    return out or ([pref] if pref else [])


def _call_openai(
    api_key: str,
    model: str,
    prompt: str,
    timeout: float = 45.0,
    *,
    base_url: str = "https://api.openai.com/v1/chat/completions",
    extra_headers: dict | None = None,
    provider: str = "",
) -> str:
    url = str(base_url or "https://api.openai.com/v1/chat/completions")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        for k, v in extra_headers.items():
            if v:
                headers[str(k)] = str(v)
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
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(_http_error_detail(e, provider=provider)) from e
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("message", {}).get("content") or "")


def _call_openai_compatible_with_fallbacks(
    api_key: str,
    model: str,
    prompt: str,
    *,
    provider: str,
    base_url: str,
    timeout: float = 45.0,
    extra_headers: dict | None = None,
) -> str:
    """Try preferred model then provider fallbacks on 404 / busy free models."""
    global _last_compat_model_used
    last_err: Exception | None = None
    tried: list[str] = []
    chain = _openai_compatible_model_chain(provider, model, api_key=api_key)
    # Cap attempts so Test chain stays snappy
    max_try = 6 if provider == "openrouter" else 4
    for mid in chain[:max_try]:
        tried.append(mid)
        try:
            text = _call_openai(
                api_key,
                mid,
                prompt,
                timeout=timeout,
                base_url=base_url,
                extra_headers=extra_headers,
                provider=provider,
            )
            _last_compat_model_used = mid
            return text
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(x in msg for x in ("401", "403", "invalid api", "incorrect api", "unauthorized")):
                raise
            # OpenAI account empty credits — don't spam models
            if provider == "openai" and ("no credits" in msg or "billing" in msg):
                raise
            # Gemini soft quota — failover handles next provider
            if provider == "gemini" and any(
                x in msg for x in ("429", "quota", "rate limit", "too many")
            ):
                raise
            # Groq/OpenRouter: try next model on per-model rate limits / busy free endpoints
            if provider not in ("groq", "openrouter") and any(
                x in msg for x in ("429", "quota", "rate limit", "too many")
            ):
                raise
            retryable = any(
                x in msg
                for x in (
                    "404",
                    "not found",
                    "does not exist",
                    "no such model",
                    "model_not_found",
                    "no endpoints",
                    "unavailable",
                    "provider returned error",
                    "temporarily",
                    "capacity",
                    "timed out",
                    "502",
                    "503",
                    "429",
                    "rate limit",
                )
            )
            if not retryable:
                raise
            continue
    hint = f" Tried models: {', '.join(tried[:6])}."
    if last_err:
        raise RuntimeError(
            f"{_http_error_detail(last_err, provider=provider)}.{hint}"
        ) from last_err
    raise RuntimeError(f"No model worked for {provider}.{hint}")


def _call_groq(api_key: str, model: str, prompt: str, timeout: float = 45.0) -> str:
    return _call_openai_compatible_with_fallbacks(
        api_key,
        model,
        prompt,
        provider="groq",
        base_url=_GROQ_CHAT_URL,
        timeout=timeout,
    )


def _call_openrouter(api_key: str, model: str, prompt: str, timeout: float = 45.0) -> str:
    return _call_openai_compatible_with_fallbacks(
        api_key,
        model,
        prompt,
        provider="openrouter",
        base_url=_OPENROUTER_CHAT_URL,
        timeout=timeout,
        extra_headers={
            "HTTP-Referer": "https://github.com/market-advisor",
            "X-Title": "Market Advisor",
        },
    )


def _extract_responses_text(data: dict | None) -> str:
    """Pull assistant text from xAI / OpenAI Responses API payload."""
    if not isinstance(data, dict):
        return ""
    ot = data.get("output_text")
    if isinstance(ot, str) and ot.strip():
        return ot.strip()
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype == "message":
            for c in item.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("type") or "") in ("output_text", "text"):
                    t = str(c.get("text") or "").strip()
                    if t:
                        parts.append(t)
        elif itype in ("output_text", "text"):
            t = str(item.get("text") or "").strip()
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


def _call_xai(
    api_key: str,
    model: str,
    prompt: str,
    timeout: float = 90.0,
    *,
    live_research: bool = True,
) -> str:
    """
    Desk Advisor via xAI Grok.
    When live_research=True (default): Responses API + web_search + x_search so Grok
    can research the ticker beyond the app pack. Falls back to Chat Completions if
    Responses fails.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if live_research:
        body = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "You are Desk Advisor. Use web_search and/or x_search when helpful "
                        "for fresh catalyst/tape/news on the ticker. Then reply with JSON only "
                        "(no markdown fences): "
                        '{"verdict":"approve|skip|wait","brief":"...","detail":"...",'
                        '"retry_after_min":0}. On skip/wait, retry_after_min is required.'
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "tools": [
                {"type": "web_search"},
                {"type": "x_search"},
            ],
            "temperature": 0.2,
            "max_output_tokens": 900,
        }
        try:
            r = requests.post(
                _XAI_RESPONSES_URL, headers=headers, json=body, timeout=timeout,
            )
            r.raise_for_status()
            text = _extract_responses_text(r.json())
            if text.strip():
                return text
        except Exception:
            # Fall through to chat completions (no live tools)
            pass
    return _call_openai(
        api_key, model, prompt, timeout=min(timeout, 45.0), base_url=_XAI_CHAT_URL,
    )


def _call_provider(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float = 45.0,
    *,
    live_research: bool = False,
) -> str:
    p = normalize_cloud_provider(provider) or str(provider or "").strip().lower()
    if p == "openai":
        return _call_openai(api_key, model, prompt, timeout=timeout)
    if p == "groq":
        return _call_groq(api_key, model, prompt, timeout=timeout)
    if p == "openrouter":
        return _call_openrouter(api_key, model, prompt, timeout=timeout)
    if p == "xai":
        return _call_xai(
            api_key, model, prompt,
            timeout=max(timeout, 90.0) if live_research else timeout,
            live_research=live_research,
        )
    return _call_gemini(api_key, model, prompt, timeout=timeout)


def _sanitize_ai_brief(brief: str, *, detail: str = "", verdict: str = "") -> str:
    """Drop schema-echo placeholders models sometimes return as the brief."""
    text = str(brief or "").strip()
    low = text.lower()
    junk = (
        "<=2 sentences",
        "≤2 sentences",
        "2 sentences plain",
        "plain english",
        "short plain-english reason",
        "one line why",
    )
    if (not text) or any(j in low for j in junk) or len(text) < 8:
        alt = str(detail or "").strip()
        if alt and not any(j in alt.lower() for j in junk):
            return alt[:420]
        v = str(verdict or "wait").strip().lower() or "wait"
        return f"Desk {v} — see detail."
    return text[:500]


def _local_fallback_guard(out: dict, proposal: dict) -> dict:
    """Budget/cloud fallback must not auto-approve marginal regime overrides."""
    if out.get("source") != "local_fallback":
        return _with_retry_after(out, proposal=proposal)
    tick = str(proposal.get("ticker") or "?").upper()
    if bool(proposal.get("regime_caution")) and out.get("verdict") == VERDICT_APPROVE:
        out["verdict"] = VERDICT_WAIT
        out["brief"] = (
            f"Wait {tick}: regime caution — AI budget exhausted; "
            f"approve manually if you want to override SPY/BTC gate."
        )[:420]
        out["retry_after_min"] = default_retry_after_min(
            verdict=VERDICT_WAIT, proposal=proposal,
            reasons=["regime caution", "budget fallback"],
        )
    return _with_retry_after(out, proposal=proposal)


def analyze_proposal(
    proposal: dict,
    context: dict | None = None,
    settings: dict | None = None,
) -> dict:
    """Return {verdict, brief, detail, source, ok, retry_after_min, error?}."""
    if not ai_configured(settings):
        return local_analyze_proposal(proposal, context)

    local = local_analyze_proposal(proposal, context)
    score = float(proposal.get("score") or 0)
    allow_regime = bool(
        (context or {}).get("allow_buys_when_regime_blocked")
        or (settings or {}).get("allow_buys_when_regime_blocked")
    )
    detail_blob = (
        str(local.get("detail") or "") + " " + str(local.get("brief") or "")
    ).lower()
    regime_local_skip = local.get("verdict") == VERDICT_SKIP and (
        (bool(proposal.get("regime_caution")) and not allow_regime)
        or "regime" in detail_blob
        or "downtrend" in detail_blob
    )
    # Always skip cloud for clear regime skips — independent of local_when_clear
    if regime_local_skip:
        local["detail"] = (
            str(local.get("detail") or "")
            + " · cloud skipped (regime local skip)"
        ).strip(" ·")[:500]
        return local

    # Clear local cases skip the cloud call (saves free-tier quota)
    if bool((settings or {}).get("advisor_ai_local_when_clear", True)):
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

    research = build_research_pack(proposal, context)
    chain = cloud_provider_chain(settings)
    if not chain:
        out = local_analyze_proposal(proposal, context)
        out["source"] = "local_fallback"
        out["error"] = "no cloud API keys configured"
        return _local_fallback_guard(out, proposal)

    errors: list[str] = []
    for provider in chain:
        ok_budget, budget_why = _ai_budget_ok(settings, provider=provider)
        if not ok_budget:
            errors.append(budget_why)
            continue
        key = resolve_provider_api_key(provider, settings)
        if not key:
            continue
        model = _model(settings, provider=provider)
        live_research = provider == "xai" and bool(
            (settings or {}).get("advisor_ai_xai_live_research", True)
        )
        prompt = _proposal_prompt(
            proposal, context, research=research, live_research=live_research,
        )
        try:
            raw = _call_provider(
                provider, key, model, prompt, live_research=live_research,
            )
            _ai_budget_record(provider)
            parsed = _extract_json_object(raw)
            if not parsed:
                errors.append(f"{provider}: response not JSON")
                continue
            verdict = _clamp_verdict(parsed.get("verdict"))
            detail = str(parsed.get("detail") or "")[:500]
            brief = _sanitize_ai_brief(
                parsed.get("brief") or "", detail=detail, verdict=verdict,
            )
            if len(chain) > 1 and provider != chain[0]:
                detail = (
                    f"{detail} · failover:{provider}" if detail else f"failover:{provider}"
                )[:500]
            return _with_retry_after(
                {
                    "verdict": verdict,
                    "brief": brief,
                    "detail": detail,
                    "retry_after_min": parsed.get("retry_after_min"),
                    "source": provider,
                    "ok": True,
                    "research": research,
                    "live_research": bool(live_research),
                    "failover_chain": list(chain),
                },
                proposal=proposal,
            )
        except Exception as e:
            errors.append(f"{provider}: {str(e)[:120]}")
            continue

    out = local_analyze_proposal(proposal, context)
    out["source"] = "local_fallback"
    err = "; ".join(errors)[:200] if errors else "all cloud providers failed"
    out["error"] = err
    detail = str(out.get("detail") or "").strip()
    out["detail"] = (
        f"{detail} · cloud fail: {err}" if detail else f"cloud fail: {err}"
    )[:500]
    out["research"] = research
    out["failover_chain"] = list(chain)
    return _local_fallback_guard(out, proposal)


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
        for provider in cloud_provider_chain(settings):
            ok_budget, _why = _ai_budget_ok(settings, provider=provider)
            if not ok_budget:
                continue
            try:
                key = resolve_provider_api_key(provider, settings)
                if not key:
                    continue
                model = _model(settings, provider=provider)
                prompt = (
                    "Summarize this trading app issue for a beginner in ONE sentence. "
                    f"Issue context: {brief}. Recent log tail:\n" + "\n".join(lines[-8:])
                )
                raw = _call_provider(provider, key, model, prompt)
                _ai_budget_record(provider)
                if raw.strip():
                    return {
                        "status": "warn",
                        "brief": raw.strip()[:400],
                        "source": provider,
                        "ok": True,
                        "at": time.time(),
                    }
            except Exception:
                continue

    return {
        "status": "warn",
        "brief": brief[:400],
        "source": "local",
        "ok": True,
        "at": time.time(),
    }


def test_connection(settings: dict | None) -> dict:
    """Settings → Test chain — ping every provider that has a key."""
    if not ai_configured(settings):
        return {
            "ok": False,
            "error": "Enable AI and enter at least one API key (Gemini/Groq/OpenRouter/OpenAI/xAI).",
        }
    chain = cloud_provider_chain(settings)
    if not chain:
        return {"ok": False, "error": "No cloud API keys found for the failover chain."}
    prompt = 'Reply JSON only: {"verdict":"wait","brief":"AI connection OK","detail":"test"}'
    results: list[dict] = []
    for provider in chain:
        key = resolve_provider_api_key(provider, settings)
        if not key:
            continue
        model = _model(settings, provider=provider)
        entry: dict = {"provider": provider, "model": model, "ok": False}
        try:
            raw = _call_provider(provider, key, model, prompt, timeout=30.0)
            if provider == "gemini":
                used_model = _last_gemini_model_used or model
            elif provider in ("groq", "openrouter"):
                used_model = _last_compat_model_used or model
            else:
                used_model = model
            entry["model"] = used_model
            parsed = _extract_json_object(raw)
            entry["ok"] = True
            entry["message"] = (
                str(parsed.get("brief"))
                if parsed and parsed.get("brief")
                else "Connected (response received)."
            )
        except Exception as e:
            msg = str(e)[:320]
            low = msg.lower()
            if "quota" in low or "429" in low or "rate" in low:
                msg += " · temporary — wait a bit or rely on other keys"
            elif provider == "groq":
                msg += " · free key: console.groq.com"
            elif provider == "openrouter":
                msg += " · free catalog rotates at openrouter.ai/models"
            elif provider == "xai":
                msg += " · console.x.ai (Cursor Grok is separate)"
            entry["error"] = msg[:400]
        results.append(entry)

    if not results:
        return {"ok": False, "error": "No cloud API keys found for the failover chain."}

    ok_n = sum(1 for r in results if r.get("ok"))
    lines: list[str] = []
    for r in results:
        tag = "OK" if r.get("ok") else "FAIL"
        mid = str(r.get("model") or "default")
        if r.get("ok"):
            lines.append(f"[{tag}] {r['provider']} / {mid}: {r.get('message') or 'OK'}")
        else:
            lines.append(f"[{tag}] {r['provider']} / {mid}: {r.get('error') or 'failed'}")

    first_ok = next((r for r in results if r.get("ok")), None)
    summary = (
        f"{ok_n}/{len(results)} providers OK\n\n" + "\n".join(lines)
    )[:1200]
    return {
        "ok": ok_n > 0,
        "all_ok": ok_n == len(results),
        "message": summary,
        "provider": (first_ok or {}).get("provider") or results[0]["provider"],
        "model": (first_ok or {}).get("model") or results[0].get("model") or "",
        "failover_chain": list(chain),
        "results": results,
        "error": None if ok_n > 0 else summary,
    }
