"""
Equity news: entry veto (headlines / earnings), shadow catalyst tag, and News Radar discovery.

News never triggers a buy by itself: the desk's penny names (SMCI, AMC, ACHR, SNAP, NIO)
gap on offerings / halts / going-concern headlines, and TV picks (Mad Money) revert after
the open pop. Discovery only widens the Breakouts scan — names still pass scoring, fee,
spread and PDT gates. Lookups fail open — an unreachable feed never blocks a buy.
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

RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
TRENDING_URL = "https://query1.finance.yahoo.com/v1/finance/trending/US?count=25"
DISCOVERY_QUERIES = ("stocks to watch", "premarket movers", "stock market today")
CACHE_TTL_SEC = 30 * 60
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


def discover_tickers(*, max_n: int = 8, timeout: float = 8.0, exclude=()) -> list[str]:
    """News-driven names to add to the Breakouts scan (cached 30m; [] on failure)."""
    now = time.time()
    if now - float(_discovery_cache["at"] or 0) < CACHE_TTL_SEC:
        return list(_discovery_cache["syms"])

    def _lookup():
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
        for q in DISCOVERY_QUERIES:
            try:
                for n in yf.Search(q, news_count=12).news or []:
                    related.append(n.get("relatedTickers") or [])
            except Exception:
                continue
        return merge_discovery(trending, related, exclude=exclude, max_n=max_n * 3)

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        syms = ex.submit(_lookup).result(timeout=timeout * 3)
    except Exception:
        syms = []
    finally:
        ex.shutdown(wait=False)
    _discovery_cache.update(at=now, syms=list(syms))
    return list(syms)


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
