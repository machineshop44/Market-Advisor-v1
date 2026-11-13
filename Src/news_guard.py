"""
Equity news: entry veto (headlines / earnings), shadow catalyst tag, and News Radar discovery.

Breakouts radar only widens the micro scan — names still pass scoring, fee, spread and PDT.
High-impact mega-cap dumps (CEO exit / scandal) are a separate advisor-first lane:
discover_catalyst_megacaps() — never auto-buys; GUI proposes for cloud advisor approve.
Lookups fail open — an unreachable feed never blocks a buy.
"""
from __future__ import annotations

import concurrent.futures
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

VETO_PATTERNS = (
    (r"\b(public|secondary|registered direct|at-the-market|atm)\s+offering\b", "share offering"),
    (r"\bprices?\s+(?:\$[\d.]+\s+million\s+)?offering\b", "share offering"),
    (r"\boffering of\b.{0,40}\bshares\b", "share offering"),
    (r"\bdilut", "dilution"),
    (r"\breverse (stock )?split\b", "reverse split"),
    (r"\bgoing concern\b", "going-concern warning"),
    (r"\b(trading )?halt(ed|s)?\b", "trading halt"),
    (r"\bdelist", "delisting"),
    (r"\bbankruptcy|chapter 11\b", "bankruptcy"),
    (r"\b(sec|doj|ftc) (probe|investigation|charges|subpoena)\b", "regulator probe"),
    (r"\bclass action\b", "class action"),
    (r"\b(misses|missed|cuts|slashes|lowers) (estimates|guidance|outlook|forecast)\b", "guidance cut"),
    (r"\bdowngrade[sd]?\b", "downgrade"),
)
_VETO_RE = [(re.compile(p, re.I), tag) for p, tag in VETO_PATTERNS]

# Evaluate (advisor lane) — not the same as entry veto. CEO/scandal dumps can be buys.
CATALYST_EVAL_PATTERNS = (
    (
        r"\b(ceo|chief executive)\b.{0,60}\b(resign|resigns|resigned|steps? down|stepping down|"
        r"ousted|leaves?|leaving|exit|exits|fired)\b",
        "ceo_exit",
    ),
    (
        r"\b(resign|resigns|resigned|steps? down|stepping down|ousted)\b.{0,60}\b"
        r"(ceo|chief executive)\b",
        "ceo_exit",
    ),
    (r"\b(interim ceo|named? (?:an )?interim ceo|appoints? (?:a )?new ceo)\b", "ceo_change"),
    (r"\b(scandal|affair|unfaithful|misconduct)\b", "scandal"),
    (r"\b(plunges?|tumbles?|dives?|crashes?|sell-?off)\b.{0,40}\b(after|amid|on)\b", "headline_plunge"),
)
_CATALYST_EVAL_RE = [(re.compile(p, re.I), tag) for p, tag in CATALYST_EVAL_PATTERNS]

# CORE mega names — parked on small books; catalyst lane may still propose RH fractional.
MEGA_CAP_SYMBOLS = frozenset({
    "NVDA", "AAPL", "TSLA", "AMD", "MSFT", "META", "AMZN", "GOOGL", "GOOG",
    "NFLX", "AVGO", "ORCL", "CRM", "INTC", "QCOM", "MU", "AMAT", "COST", "JPM",
})

RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
TRENDING_URL = "https://query1.finance.yahoo.com/v1/finance/trending/US?count=25"
DISCOVERY_QUERIES = ("stocks to watch", "premarket movers", "stock market today")
CATALYST_DISCOVERY_QUERIES = (
    "CEO resigns", "CEO steps down", "stock plunges after", "stock market today",
)
CACHE_TTL_SEC = 30 * 60
CATALYST_CACHE_TTL_SEC = 15 * 60
_cache: dict[str, tuple[float, Optional[list[dict]]]] = {}
_cache_lock = threading.Lock()


def _published_ts(item: dict) -> Optional[float]:
    for key in ("providerPublishTime", "pubTime"):
        v = item.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    raw = item.get("pubDate") or item.get("displayTime")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def normalize_items(raw) -> list[dict]:
    """yfinance .news (old flat or new {'content': {...}} shape) → [{'title', 'ts'}]."""
    out = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        body = it.get("content") if isinstance(it.get("content"), dict) else it
        title = str(body.get("title") or "").strip()
        if not title:
            continue
        out.append({"title": title, "ts": _published_ts(body)})
    return out


def parse_rss(text: str) -> list[dict]:
    """Yahoo headline RSS → [{'title', 'ts'}]."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    out = []
    try:
        root = ET.fromstring(text or "")
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        ts = None
        raw = item.findtext("pubDate")
        if raw:
            try:
                ts = parsedate_to_datetime(raw).timestamp()
            except (TypeError, ValueError):
                ts = None
        out.append({"title": title, "ts": ts})
    return out


def _us_symbol(sym) -> Optional[str]:
    s = str(sym or "").upper().strip()
    return s if s.isalpha() and 1 <= len(s) <= 5 else None


def merge_discovery(trending, related_lists, *, exclude=(), max_n: int = 8) -> list[str]:
    """Trending first, then names tagged in market headlines; US tickers only, deduped."""
    skip = {str(x).upper() for x in exclude or ()}
    out: list[str] = []
    for src in [trending or []] + list(related_lists or []):
        for raw in src or []:
            s = _us_symbol(raw)
            if s and s not in skip and s not in out:
                out.append(s)
            if len(out) >= max_n:
                return out
    return out


_discovery_cache: dict[str, Any] = {"at": 0.0, "syms": []}
_catalyst_cache: dict[str, Any] = {"at": 0.0, "rows": []}


def _yahoo_trending_and_related(
    queries, *, timeout: float = 8.0, exclude=(), max_n: int = 24,
) -> list[str]:
    """Shared Yahoo trending + Search relatedTickers pool (fail → [])."""
    import requests
    import yfinance as yf

    trending = []
    try:
        r = requests.get(TRENDING_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        res = (r.json().get("finance") or {}).get("result") or []
        trending = [q.get("symbol") for q in (res[0].get("quotes") if res else []) or []]
    except Exception:
        trending = []
    related = []
    for q in queries or ():
        try:
            for n in yf.Search(q, news_count=12).news or []:
                related.append(n.get("relatedTickers") or [])
        except Exception:
            continue
    return merge_discovery(trending, related, exclude=exclude, max_n=max_n)


def discover_tickers(*, max_n: int = 8, timeout: float = 8.0, exclude=()) -> list[str]:
    """News-driven micro names to add to the Breakouts scan (cached 30m; [] on failure)."""
    now = time.time()
    if now - float(_discovery_cache["at"] or 0) < CACHE_TTL_SEC:
        return list(_discovery_cache["syms"])

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        syms = ex.submit(
            _yahoo_trending_and_related,
            DISCOVERY_QUERIES,
            timeout=timeout,
            exclude=exclude,
            max_n=max_n * 3,
        ).result(timeout=timeout * 3)
    except Exception:
        syms = []
    finally:
        ex.shutdown(wait=False)
    _discovery_cache.update(at=now, syms=list(syms))
    return list(syms)


def headline_catalyst_eval(
    items, *, now: Optional[float] = None, hours: float = 48.0, aliases=None,
) -> tuple[bool, str, str]:
    """(matched, tag, title) for high-impact headlines the advisor lane may evaluate."""
    now = float(now if now is not None else time.time())
    for it in _recent(items, now, hours, aliases):
        title = str(it.get("title") or "")
        for rx, tag in _CATALYST_EVAL_RE:
            if rx.search(title):
                return True, tag, title[:120]
    return False, "", ""


def discover_catalyst_megacaps(
    *,
    max_n: int = 4,
    timeout: float = 8.0,
    exclude=(),
    mega_symbols=None,
    hours: float = 48.0,
    now: Optional[float] = None,
    fetch: Callable[..., Optional[list[dict]]] = None,
    name_fn: Optional[Callable[[str], str]] = None,
    pool: Optional[list[str]] = None,
    use_cache: bool = True,
) -> list[dict]:
    """
    Mega-cap names in the news pool with a fresh CEO/scandal/plunge headline.
    Returns [{symbol, tag, title}, ...] — empty on failure. Does not auto-buy.
    """
    ts_now = float(now if now is not None else time.time())
    if use_cache and pool is None and fetch is None:
        if ts_now - float(_catalyst_cache["at"] or 0) < CATALYST_CACHE_TTL_SEC:
            return [dict(r) for r in (_catalyst_cache.get("rows") or [])]

    mega = frozenset(mega_symbols or MEGA_CAP_SYMBOLS)
    fetch_fn = fetch or fetch_news
    name_f = name_fn if name_fn is not None else company_name

    if pool is not None:
        candidates = [s for s in pool if _us_symbol(s) and _us_symbol(s) in mega]
    else:
        try:
            raw_pool = _yahoo_trending_and_related(
                tuple(DISCOVERY_QUERIES) + tuple(CATALYST_DISCOVERY_QUERIES),
                timeout=timeout,
                exclude=exclude,
                max_n=max(24, max_n * 8),
            )
        except Exception:
            raw_pool = []
        # Prefer megas present in the pool; also keep any mega that trending returned.
        candidates = []
        for s in raw_pool:
            u = _us_symbol(s)
            if u and u in mega and u not in candidates:
                candidates.append(u)

    rows: list[dict] = []
    skip = {str(x).upper() for x in exclude or ()}
    for sym in candidates:
        if sym in skip:
            continue
        try:
            items = fetch_fn(sym)
        except Exception:
            items = None
        if not items:
            continue
        try:
            aliases = name_aliases(sym, name_f(sym) if name_f else "")
        except Exception:
            aliases = name_aliases(sym, "")
        ok, tag, title = headline_catalyst_eval(
            items, now=ts_now, hours=hours, aliases=aliases,
        )
        if not ok:
            continue
        # Dilution / halt / bankruptcy still never become catalyst buys.
        veto, _why = headline_veto(items, now=ts_now, hours=hours, aliases=aliases)
        if veto:
            continue
        rows.append({"symbol": sym, "tag": tag, "title": title})
        if len(rows) >= max_n:
            break

    if use_cache and pool is None and fetch is None:
        _catalyst_cache.update(at=ts_now, rows=[dict(r) for r in rows])
    return rows


_NAME_SUFFIX = re.compile(
    r"[,.]|\b(inc|corp|corporation|co|company|holdings?|technologies|technology|ltd|"
    r"limited|plc|group|class [a-c]|sa|nv|ag|se)\b",
    re.I,
)
_GENERIC_FIRST = {"super", "american", "first", "united", "general", "global", "new", "the",
                  "national", "international", "advanced", "applied", "digital"}


def name_aliases(ticker: str, company: str = "") -> list[str]:
    """Ticker plus company-name forms a headline would use ('Super Micro', 'Archer', 'SoFi')."""
    out = [str(ticker or "").upper()]
    core = " ".join(_NAME_SUFFIX.sub(" ", str(company or "")).split())
    words = core.split()
    if core:
        out.append(core)
    if words:
        first = words[0]
        if first.lower() in _GENERIC_FIRST and len(words) > 1:
            out.append(f"{words[0]} {words[1]}")
        elif len(first) >= 3:
            out.append(first)
    return [a for i, a in enumerate(out) if a and a not in out[:i]]


def _mentions(title: str, aliases) -> bool:
    if not aliases:
        return True
    return any(re.search(rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])", title) for a in aliases)


def _recent(items, now: float, hours: float, aliases=None) -> list[dict]:
    cutoff = now - hours * 3600.0
    return [
        i for i in items or []
        if i.get("ts") is not None and float(i["ts"]) >= cutoff and _mentions(i["title"], aliases)
    ]


def headline_veto(
    items, *, now: Optional[float] = None, hours: float = 24.0, aliases=None,
) -> tuple[bool, str]:
    """(True, why) when a recent headline naming the company is a known gap-down class."""
    now = float(now if now is not None else time.time())
    for it in _recent(items, now, hours, aliases):
        for rx, tag in _VETO_RE:
            if rx.search(it["title"]):
                return True, f"news veto ({tag}): {it['title'][:90]}"
    return False, ""


def has_fresh_catalyst(
    items, *, now: Optional[float] = None, hours: float = 24.0, aliases=None,
) -> bool:
    now = float(now if now is not None else time.time())
    return bool(_recent(items, now, hours, aliases))


_name_cache: dict[str, str] = {}


def company_name(ticker: str, *, timeout: float = 6.0) -> str:
    """Yahoo short name for alias matching (cached for the process; '' on failure)."""
    tu = str(ticker or "").upper()
    if tu in _name_cache:
        return _name_cache[tu]

    def _lookup():
        import yfinance as yf
        for q in yf.Search(tu, max_results=3, news_count=0).quotes or []:
            if str(q.get("symbol") or "").upper() == tu:
                return str(q.get("shortname") or q.get("longname") or "")
        return ""

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        name = ex.submit(_lookup).result(timeout=timeout)
        _name_cache[tu] = name
    except Exception:
        name = ""
    finally:
        ex.shutdown(wait=False)
    return name


def fetch_news(ticker: str, *, timeout: float = 6.0) -> Optional[list[dict]]:
    """Normalized headlines (cached 30m); None when the feed is unreachable."""
    tu = str(ticker or "").upper()
    if not tu:
        return None
    now = time.time()
    with _cache_lock:
        hit = _cache.get(tu)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1]

    def _lookup():
        # yfinance 1.5 Ticker.news returns [] — the Yahoo headline RSS still serves per-symbol.
        import requests
        r = requests.get(
            RSS_URL.format(sym=tu), headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout,
        )
        r.raise_for_status()
        return parse_rss(r.text)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        items = ex.submit(_lookup).result(timeout=timeout)
    except Exception:
        items = None
    finally:
        ex.shutdown(wait=False)
    with _cache_lock:
        _cache[tu] = (now, items)
    return items


def entry_news_check(
    ticker: str,
    *,
    settings: Optional[dict] = None,
    earnings_fn: Optional[Callable[[str], Optional[bool]]] = None,
    fetch: Callable[..., Optional[list[dict]]] = fetch_news,
    name_fn: Optional[Callable[[str], str]] = company_name,
    now: Optional[float] = None,
) -> tuple[Optional[str], Optional[bool]]:
    """
    (block_reason or None, fresh_catalyst or None) for an equity entry.
    entry_earnings_block / entry_news_veto gate the blocks; the catalyst tag is shadow-only.
    """
    s = settings or {}
    if bool(s.get("entry_earnings_block", True)) and earnings_fn is not None:
        try:
            if earnings_fn(ticker) is True:
                return "earnings before next open — no new entry", None
        except Exception:
            pass
    want_veto = bool(s.get("entry_news_veto", True))
    want_tag = bool(s.get("news_shadow_tag", True))
    if not (want_veto or want_tag):
        return None, None
    items = fetch(ticker)
    if items is None:
        return None, None
    try:
        hours = float(s.get("entry_news_veto_hours", 24) or 24)
    except (TypeError, ValueError):
        hours = 24.0
    # Symbol RSS carries market roundups ('Adobe downgraded…' on SOFI) — only headlines
    # naming this company count.
    aliases = name_aliases(ticker, name_fn(ticker) if name_fn else "")
    if want_veto:
        veto, why = headline_veto(items, now=now, hours=hours, aliases=aliases)
        if veto:
            return why, True
    return None, (
        has_fresh_catalyst(items, now=now, hours=hours, aliases=aliases) if want_tag else None
    )
