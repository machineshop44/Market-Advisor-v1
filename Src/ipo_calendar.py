"""
Upcoming IPO calendar — advisory research helper only.

Does NOT apply to Robinhood IPO Access. Cache + free sources (Yahoo via
yfinance, Nasdaq JSON fallback). Lightweight heuristic "consider" hints.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

CACHE_TTL_SEC = 3 * 3600  # 3 hours
LOOKAHEAD_DAYS = 75
YAHOO_IPO_CALENDAR_URL = "https://finance.yahoo.com/calendar/ipo"

_cache: Dict[str, Any] = {
    "fetched_at": 0.0,
    "ipos": [],
    "error": None,
    "source": "",
}

# Holding ticker → coarse themes (for overlap hints only)
_TICKER_THEMES: Dict[str, Set[str]] = {
    "AAPL": {"tech"}, "MSFT": {"tech"}, "GOOG": {"tech"}, "GOOGL": {"tech"},
    "AMZN": {"tech", "consumer"}, "META": {"tech"}, "NVDA": {"tech", "ai"},
    "AMD": {"tech", "ai"}, "AVGO": {"tech"}, "TSLA": {"auto", "tech"},
    "NFLX": {"tech", "consumer"}, "CRM": {"tech"}, "ORCL": {"tech"},
    "ADBE": {"tech"}, "INTC": {"tech"}, "QCOM": {"tech"},
    "QQQ": {"tech"}, "XLK": {"tech"}, "VGT": {"tech"}, "SMH": {"tech", "ai"},
    "SOXX": {"tech", "ai"}, "ARKK": {"tech", "ai"},
    "XBI": {"biotech"}, "IBB": {"biotech"}, "ARKG": {"biotech"},
    "JNJ": {"healthcare"}, "UNH": {"healthcare"}, "PFE": {"healthcare", "biotech"},
    "MRK": {"healthcare", "biotech"}, "LLY": {"healthcare", "biotech"},
    "XLV": {"healthcare"}, "IHI": {"healthcare"},
    "XLE": {"energy"}, "XOM": {"energy"}, "CVX": {"energy"}, "OXY": {"energy"},
    "XLF": {"finance"}, "JPM": {"finance"}, "BAC": {"finance"}, "GS": {"finance"},
    "V": {"finance"}, "MA": {"finance"},
    "XLI": {"industrial"}, "CAT": {"industrial"}, "GE": {"industrial"},
    "XLY": {"consumer"}, "XLP": {"consumer"}, "COST": {"consumer"}, "WMT": {"consumer"},
    "XLB": {"materials"}, "XLRE": {"realestate"}, "VNQ": {"realestate"},
    "XLU": {"utilities"}, "GLD": {"metals"}, "SLV": {"metals"}, "IAU": {"metals"},
    "BTC": {"crypto"}, "ETH": {"crypto"}, "GBTC": {"crypto"}, "IBIT": {"crypto"},
    "FBTC": {"crypto"}, "BITO": {"crypto"},
}

_NAME_THEME_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "biotech": (
        "bio", "therapeutics", "pharma", "pharmaceutical", "oncology", "gene",
        "medical", "health", "clinical", "life sciences", "diagnostics",
    ),
    "tech": (
        "software", "semiconductor", "cloud", "cyber", "digital", "data",
        "platform", "internet", "chip", "robotics", "saas",
    ),
    "ai": ("artificial intelligence", " machine learning", " ai ", "a.i."),
    "energy": ("energy", "oil", "gas", "solar", "renewable", "lithium", "battery"),
    "finance": (
        "bank", "bancorp", "capital", "financial", "fintech", "payment", "insurance",
    ),
    "consumer": (
        "retail", "consumer", "e-commerce", "apparel", "food", "restaurant",
        "grocery", "beverage", "fashion",
    ),
    "auto": ("auto", "vehicle", "ev ", "electric vehicle", "mobility"),
    "industrial": ("industrial", "manufacturing", "aerospace", "defense", "logistics"),
    "realestate": ("real estate", "reit", "property"),
    "crypto": ("crypto", "bitcoin", "blockchain", "digital asset"),
    "metals": ("mining", "gold", "silver", "copper", "rare earth"),
    "healthcare": ("health", "hospital", "medical device", "medtech"),
}

_SPAC_RE = re.compile(
    r"\b(acquisition|blank[\s-]?check|spac)\b|"
    r"\bacquisition corp|\bcorp\.?\s+(i{1,3}|iv|v|vi{0,3})\b",
    re.I,
)
_UNIT_WARRANT_RE = re.compile(r"\b(units?|warrants?)\b", re.I)
_FUND_PRODUCT_RE = re.compile(
    r"(?i)\b(etfs?|etns?|series trust|leverage shares|closed[- ]end funds?|funds?)\b",
)


def _ua_headers() -> Dict[str, str]:
    try:
        from version import user_agent
        ua = user_agent()
    except Exception:
        ua = "MarketAdvisor/1.0"
    return {
        "User-Agent": f"Mozilla/5.0 ({ua})",
        "Accept": "application/json,text/plain,*/*",
    }


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    try:
        import math
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return default
    except Exception:
        pass
    s = str(val).strip()
    if s.lower() in ("nan", "nat", "none", "null", ""):
        return default
    return s


def _parse_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    try:
        import math
        if isinstance(val, float) and math.isnan(val):
            return None
    except Exception:
        pass
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = _safe_str(val)
    if not s:
        return None
    # pandas Timestamp / ISO
    try:
        if hasattr(val, "to_pydatetime"):
            return val.to_pydatetime().date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(s[:26].replace("+00:00", "+0000"), fmt.replace("%z", "+0000") if "+0000" in s else fmt).date()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _format_price_range(lo: Any, hi: Any, offer: Any = None) -> str:
    def _num(v):
        try:
            if v is None:
                return None
            import math
            f = float(v)
            if math.isnan(f) or math.isinf(f) or f <= 0:
                return None
            return f
        except Exception:
            s = _safe_str(v).replace("$", "").replace(",", "")
            if not s:
                return None
            # Nasdaq sometimes gives "12.00-14.00"
            if "-" in s:
                return s
            try:
                f = float(s)
                return f if f > 0 else None
            except Exception:
                return s

    a, b, o = _num(lo), _num(hi), _num(offer)
    if isinstance(a, str) and "-" in a:
        return f"${a}" if not a.startswith("$") else a
    if a is not None and b is not None:
        return f"${a:g} – ${b:g}"
    if o is not None and not isinstance(o, str):
        return f"${o:g}"
    if isinstance(o, str):
        return o if o.startswith("$") else f"${o}"
    if a is not None:
        return f"${a:g}+"
    if b is not None:
        return f"up to ${b:g}"
    return "—"


def _is_spac(company: str, ticker: str = "") -> bool:
    name = company or ""
    if _SPAC_RE.search(name):
        return True
    t = (ticker or "").upper()
    # Common SPAC unit/warrant suffixes
    if t.endswith("U") or t.endswith("W") or t.endswith("R"):
        if "acquisition" in name.lower() or "capital" in name.lower():
            return True
    return False


def _is_unit_or_warrant(company: str, ticker: str = "") -> bool:
    if _UNIT_WARRANT_RE.search(company or ""):
        return True
    t = (ticker or "").upper()
    return bool(t) and (t.endswith("U") or t.endswith("W") or t.endswith("WT") or t.endswith("WS"))


def themes_from_holdings(tickers: List[str]) -> Set[str]:
    themes: Set[str] = set()
    for raw in tickers or []:
        t = str(raw or "").upper().replace("-USD", "").split("/")[0].strip()
        if not t:
            continue
        themes |= _TICKER_THEMES.get(t, set())
    return themes


def themes_from_company_name(company: str) -> Set[str]:
    text = f" {(company or '').lower()} "
    found: Set[str] = set()
    for theme, keys in _NAME_THEME_KEYWORDS.items():
        for k in keys:
            if k in text:
                found.add(theme)
                break
    return found


def _normalize_ipo(
    *,
    company: str,
    ticker: str,
    expected: Optional[date],
    exchange: str,
    price_range: str,
    status: str,
    source: str,
) -> Optional[Dict[str, Any]]:
    company = _safe_str(company)
    ticker = _safe_str(ticker).upper().replace(" ", "")
    if not company and not ticker:
        return None
    if expected is None:
        return None
    today = date.today()
    if expected < today - timedelta(days=2):
        return None
    if expected > today + timedelta(days=LOOKAHEAD_DAYS):
        return None
    spac = _is_spac(company, ticker)
    unit_w = _is_unit_or_warrant(company, ticker)
    fundish = bool(_FUND_PRODUCT_RE.search(company or ""))
    if ticker and not unit_w:
        link = f"https://finance.yahoo.com/quote/{ticker}"
    else:
        link = YAHOO_IPO_CALENDAR_URL
    return {
        "company": company or ticker,
        "ticker": ticker,
        "date": expected.isoformat(),
        "date_obj": expected,
        "exchange": _safe_str(exchange) or "—",
        "price_range": price_range or "—",
        "status": _safe_str(status) or "Expected",
        "is_spac": spac,
        "is_unit_warrant": unit_w,
        "is_fund_product": fundish,
        "source": source,
        "link": link,
        "themes": sorted(themes_from_company_name(company)),
    }


def _fetch_yahoo_yfinance() -> Tuple[List[Dict[str, Any]], str]:
    import yfinance as yf

    start = date.today()
    end = start + timedelta(days=LOOKAHEAD_DAYS)
    cal = yf.Calendars(start=start.isoformat(), end=end.isoformat())
    df = cal.get_ipo_info_calendar(limit=100)
    if df is None or getattr(df, "empty", True):
        return [], "Yahoo Finance (empty)"

    rows: List[Dict[str, Any]] = []
    # Symbol may be index
    reset = df.reset_index() if getattr(df.index, "name", None) or "Symbol" not in df.columns else df
    for _, row in reset.iterrows():
        ticker = _safe_str(row.get("Symbol") or row.get("symbol") or row.get("Ticker"))
        company = _safe_str(row.get("Company") or row.get("companyshortname"))
        exchange = _safe_str(row.get("Exchange") or row.get("exchange_short_name"))
        status = _safe_str(row.get("Action") or row.get("dealtype") or "Expected")
        expected = _parse_date(row.get("Date") or row.get("startdatetime"))
        price_range = _format_price_range(
            row.get("Price From") or row.get("pricefrom"),
            row.get("Price To") or row.get("priceto"),
            row.get("Price") or row.get("offerprice"),
        )
        item = _normalize_ipo(
            company=company,
            ticker=ticker,
            expected=expected,
            exchange=exchange,
            price_range=price_range,
            status=status,
            source="Yahoo Finance",
        )
        if item:
            rows.append(item)
    return rows, "Yahoo Finance"


def _nasdaq_http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_ua_headers())
    with urllib.request.urlopen(req, timeout=18) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_nasdaq_calendar() -> Tuple[List[Dict[str, Any]], str]:
    """Nasdaq public IPO calendar JSON — upcoming + recent filed as soft pipeline."""
    rows: List[Dict[str, Any]] = []
    months = []
    today = date.today()
    months.append(today.strftime("%Y-%m"))
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    months.append(nxt.strftime("%Y-%m"))

    for ym in months:
        payload = _nasdaq_http_json(f"https://api.nasdaq.com/api/ipo/calendar?date={ym}")
        data = (payload or {}).get("data") or {}
        for bucket, default_status in (
            ("upcoming", "Expected"),
            ("filed", "Filed"),
            ("priced", "Priced"),
        ):
            block = data.get(bucket) or {}
            items = block.get("rows") if isinstance(block, dict) else block
            if not items:
                continue
            for r in items:
                if not isinstance(r, dict):
                    continue
                company = _safe_str(r.get("companyName"))
                ticker = _safe_str(r.get("proposedTickerSymbol"))
                exchange = _safe_str(r.get("proposedExchange"))
                status = _safe_str(r.get("dealStatus")) or default_status
                raw_date = (
                    r.get("expectedPriceDate")
                    or r.get("pricedDate")
                    or r.get("filedDate")
                )
                expected = _parse_date(raw_date)
                # Filed without expected date → skip (not actionable calendar)
                if bucket == "filed" and not r.get("expectedPriceDate"):
                    continue
                price_range = _format_price_range(
                    None, None, r.get("proposedSharePrice")
                )
                if price_range == "—" and r.get("dollarValueOfSharesOffered"):
                    price_range = "—"
                item = _normalize_ipo(
                    company=company,
                    ticker=ticker,
                    expected=expected,
                    exchange=exchange,
                    price_range=price_range,
                    status=status,
                    source="Nasdaq",
                )
                if item:
                    rows.append(item)
    return rows, "Nasdaq IPO calendar"


def _dedupe(ipos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for ipo in ipos:
        key = (
            (ipo.get("ticker") or "").upper(),
            (ipo.get("company") or "").lower()[:48],
            ipo.get("date") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(ipo)
    out.sort(key=lambda x: (x.get("date") or "9999", x.get("company") or ""))
    return out


def score_ipo(
    ipo: Dict[str, Any],
    holding_themes: Optional[Set[str]] = None,
    regime_ok: bool = True,
) -> Dict[str, str]:
    """
    Pragmatic heuristic hint — not financial advice / not diligence.

    Returns keys: hint, note  (hint in {Worth a look, Watch, Skip / speculative, Caution})
    """
    holding_themes = holding_themes or set()
    ticker = _safe_str(ipo.get("ticker"))
    company = _safe_str(ipo.get("company"))
    notes: List[str] = []

    if not ticker:
        return {
            "hint": "Skip / speculative",
            "note": "No ticker yet — hard to research / RH Access usually needs a symbol.",
        }
    if ipo.get("is_unit_warrant"):
        return {
            "hint": "Skip / speculative",
            "note": "Unit/warrant listing — not a plain common-stock IPO Access candidate.",
        }
    if ipo.get("is_fund_product"):
        return {
            "hint": "Skip / speculative",
            "note": "Looks like a fund/ETF-style listing, not a classic operating-company IPO.",
        }
    if ipo.get("is_spac"):
        return {
            "hint": "Skip / speculative",
            "note": "Looks like a blank-check / SPAC — high structure risk.",
        }

    ipo_themes = set(ipo.get("themes") or themes_from_company_name(company))
    overlap = sorted(ipo_themes & holding_themes)
    if overlap:
        notes.append("Theme overlap with holdings: " + ", ".join(overlap))
        hint = "Worth a look"
    else:
        notes.append("No clear theme overlap with your holdings (heuristic).")
        hint = "Watch"

    pr = _safe_str(ipo.get("price_range"))
    if pr and pr != "—":
        notes.append(f"Range {pr}")
    else:
        notes.append("No price range published yet.")

    if not regime_ok:
        if hint == "Worth a look":
            hint = "Caution"
        notes.append("Broad equity regime soft — size any RH interest carefully.")

    notes.append("Heuristic only — not advice; apply manually in RH IPO Access if you choose.")
    return {"hint": hint, "note": " ".join(notes)}


def fetch_upcoming_ipos(
    force: bool = False,
    holding_tickers: Optional[List[str]] = None,
    regime_ok: bool = True,
) -> Dict[str, Any]:
    """
    Return cached or freshly fetched IPO list with consider hints.

    Shape: {ipos, error, source, fetched_at, from_cache}
    """
    now = time.time()
    if (
        not force
        and _cache["ipos"]
        and (now - float(_cache["fetched_at"] or 0)) < CACHE_TTL_SEC
        and not _cache.get("error")
    ):
        holding_themes = themes_from_holdings(holding_tickers or [])
        ipos = []
        for raw in _cache["ipos"]:
            item = dict(raw)
            scored = score_ipo(item, holding_themes, regime_ok=regime_ok)
            item["hint"] = scored["hint"]
            item["note"] = scored["note"]
            ipos.append(item)
        return {
            "ipos": ipos,
            "error": None,
            "source": _cache.get("source") or "",
            "fetched_at": _cache["fetched_at"],
            "from_cache": True,
        }

    errors: List[str] = []
    rows: List[Dict[str, Any]] = []
    source = ""

    try:
        rows, source = _fetch_yahoo_yfinance()
    except Exception as e:
        errors.append(f"Yahoo: {e}")

    if not rows:
        try:
            rows, source = _fetch_nasdaq_calendar()
        except Exception as e:
            errors.append(f"Nasdaq: {e}")

    rows = _dedupe(rows)
    err = None if rows else ("; ".join(errors) if errors else "No upcoming IPOs found.")

    # Strip non-JSON-friendly date_obj before caching
    cache_rows = []
    for r in rows:
        c = {k: v for k, v in r.items() if k != "date_obj"}
        cache_rows.append(c)

    _cache["fetched_at"] = now
    _cache["ipos"] = cache_rows
    _cache["error"] = err
    _cache["source"] = source

    holding_themes = themes_from_holdings(holding_tickers or [])
    scored_list = []
    for raw in cache_rows:
        item = dict(raw)
        scored = score_ipo(item, holding_themes, regime_ok=regime_ok)
        item["hint"] = scored["hint"]
        item["note"] = scored["note"]
        scored_list.append(item)

    return {
        "ipos": scored_list,
        "error": err,
        "source": source,
        "fetched_at": now,
        "from_cache": False,
    }


def cache_age_seconds() -> Optional[float]:
    ts = float(_cache.get("fetched_at") or 0)
    if ts <= 0:
        return None
    return max(0.0, time.time() - ts)


def format_fetched_at(ts: float) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"
