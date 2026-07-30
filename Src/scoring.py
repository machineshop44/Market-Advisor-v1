import os
import json
import time
from datetime import datetime

# Lazy heavy deps — pandas/yfinance are only pulled when scoring actually runs
_yf = None


def _get_yf():
    global _yf
    if _yf is None:
        import yfinance as yf
        _yf = yf
    return _yf


# Fallback wrapper to hook into the main GUI's price feed
try:
    from market_data import fetch_current_price
except ImportError:
    def fetch_current_price(ticker): return 0.0

# =========================================================================
# BROKER FEE PROFILES
# Round-trip friction is baked into exit thresholds so a "win" is a real win.
# RH stocks ≈ $0 commission. RH crypto / CB Advanced take a real bite.
# Discipline: cut losers / bank winners same-session — no multi-day hope.
# =========================================================================
FEE_PROFILES = {
    "ROBINHOOD_STOCK": {
        "ttp_arm": 0.012,          # +1.2% arms trail (was 2.0% — too high for small RH tickets)
        "ttp_trail": 0.008,        # -0.8% trail
        "hard_stop": -0.035,       # -3.5% — clear disaster cut, not noise
        "time_30m_target": 0.008,  # +0.8%
        "time_60m_target": 0.006,  # +0.6%
        "time_green_min": 45,      # minutes
        "time_green_roi": 0.006,   # +0.6% — above typical RH stock spread crumbs
        "stale_minutes": 180,      # 3h same-session (2h/−1% was twitchy on META/TSLA)
        "stale_roi": -0.015,       # -1.5%
    },
    "ROBINHOOD_CRYPTO": {
        "ttp_arm": 0.018,          # +1.8% (was 2.8%)
        "ttp_trail": 0.010,
        "hard_stop": -0.040,       # -4.0% — crypto vol needs a wider disaster line
        "time_30m_target": 0.014,  # +1.4% — above typical RH crypto spread round-trip
        "time_60m_target": 0.010,  # +1.0%
        "time_green_min": 45,
        "time_green_roi": 0.010,   # +1.0% min green (spread is the real cost on RH crypto)
        "stale_minutes": 120,      # crypto stays tighter than equities
        "stale_roi": -0.012,       # -1.2%
    },
    "COINBASE": {
        # Advanced retail taker ≈ 0.60% each side → ~1.2% round-trip (+ slippage buffer)
        "ttp_arm": 0.022,          # +2.2% arms trail
        "ttp_trail": 0.010,        # -1.0% trail → exit still ~+1.2%+ once armed above peak
        "hard_stop": -0.040,
        "time_30m_target": 0.016,  # +1.6% clears ~1.2% fees + buffer
        "time_60m_target": 0.014,  # +1.4%
        "time_green_min": 45,
        "time_green_roi": 0.015,   # +1.5% — never take a CB "win" that fees wipe out
        "stale_minutes": 120,
        "stale_roi": -0.012,
    },
}

CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}

CRYPTO_COOLDOWN = 10 * 60   # 10 minutes after selling crypto
STOCK_COOLDOWN = 20 * 60    # 20 minutes after selling stocks

RSI_PERIOD = 14
RSI_CEILING = 70            # 70 is standard; 60 was blocking too many real trends

# Regime gate (baked-in — not a user toggle): multi-source BTC/SPY vs EMA20
# Sources: (1) Yahoo 1H closed bars  (2) broker live  (3) last-good cache
REGIME_EMA_BUFFER = 0.0015          # require close > EMA20 * 1.0015 (~0.15%)
REGIME_CONFIRM_BARS = 2             # consecutive 1H closes above buffered EMA
REGIME_LAST_GOOD_TTL = 25 * 60      # seconds — third source when live feeds gap (~25m)
REGIME_BROKER_MIN_HOURLY = 22       # broker-alone EMA needs ~EMA20 + confirm bars
REGIME_BROKER_SHORT_LOOKBACK = 90 * 60  # degraded: live vs ~1.5h-ago broker price
REGIME_BROKER_SAMPLE_GAP = 45       # min seconds between ring-buffer samples
REGIME_BROKER_RING_MAX = 72         # keep ~3d of hourly broker closes

# Pro risk stack (baked-in — no Settings maze)
RISK_PCT_PER_TRADE = 0.0075       # 0.75% of equity risked per trade
CASH_RESERVE_PCT = 0.12           # leave 12% buying power undeployed
MAX_CRYPTO_BOOK_FRAC = 0.40       # max crypto share of portfolio value
MAX_CLUSTER_POSITIONS = 2         # max open names in one correlation cluster
STOCK_LIMIT_FILL_TIMEOUT = 45     # cancel unfilled stock limits after N seconds
PRICE_STALE_SECONDS = 120         # reject buys if live quote older than this (when timestamped)

# Practical concentration heuristics (maintainable, no quant library)
CORRELATION_CLUSTERS = {
    "MAG7": {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA"},
    "BTC_BETA": {"BTC", "ETH", "SOL", "IBIT", "FBTC", "BITO", "GBTC", "MSTR", "ETHE"},
    "SEMI": {"NVDA", "AMD", "AVGO", "TSM", "SMCI", "SOXL", "SOXX", "INTC"},
    "MEME_CRYPTO": {"DOGE", "SHIB", "PEPE", "BONK"},
}

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scoring_state.json")


def _resolve_fee_profile(broker_id, ticker=None, asset_type=""):
    """
    Return a *copy* of the fee profile for this broker+asset.
    Never returns the shared dict itself, so one broker cannot mutate another's thresholds.
    """
    bid = str(broker_id or "ROBINHOOD").upper()
    clean = str(ticker or "").replace("-USD", "").upper()
    is_crypto = (
        "CRYPTO" in str(asset_type).upper()
        or clean in CRYPTO_TICKERS
        or bid == "COINBASE"
    )
    if bid == "COINBASE":
        key = "COINBASE"
    elif is_crypto:
        key = "ROBINHOOD_CRYPTO"
    else:
        key = "ROBINHOOD_STOCK"
    return dict(FEE_PROFILES[key])  # copy — isolated per call


# =========================================================================
# IN-MEMORY STATE TRACKERS (MULTI-BROKER ISOLATED)
# =========================================================================
_portfolio_memory = {"ROBINHOOD": {}, "COINBASE": {}}
_cooldown_memory = {"ROBINHOOD": {}, "COINBASE": {}}
_protective_orders = {"ROBINHOOD": {}, "COINBASE": {}}  # ticker -> {order_id, kind, ...}
_trend_cache = {}
_TREND_CACHE_TTL = 45  # seconds

# Regime multi-source state (GUI registers connected broker adapters)
_regime_rh = None   # RobinhoodAdapter or None
_regime_cb = None   # CoinbaseAdapter or None
# proxy key -> {"ok": bool, "ts": float, "source": str}
_regime_last_good = {}
# ring key "SPY"|"BTC" -> [{"hour": int, "close": float}, ...]
_broker_hourly_closes = {"SPY": [], "BTC": []}
# recent ticks for short-trend + building hourly closes
_broker_price_samples = {"SPY": [], "BTC": []}
_broker_last_sample_ts = {"SPY": 0.0, "BTC": 0.0}


def register_regime_brokers(robinhood=None, coinbase=None):
    """GUI wires live adapters so regime can read SPY (RH) / BTC (CB then RH)."""
    global _regime_rh, _regime_cb
    if robinhood is not None:
        _regime_rh = robinhood
    if coinbase is not None:
        _regime_cb = coinbase


def _safe_ticker(ticker):
    """Ensures raw crypto tickers get the required suffix for Yahoo Finance data."""
    clean = str(ticker).upper()
    if clean in CRYPTO_TICKERS and not clean.endswith("-USD"):
        return f"{clean}-USD"
    return ticker


def _auto_detect_sales(broker_id):
    """
    Moves tickers from portfolio memory to cooldown if they haven't been
    evaluated as a holding in the last 3 minutes (GUI sold them).
    Isolated per broker so CB doesn't delete RH memory.
    """
    if broker_id not in _portfolio_memory: return
    if broker_id not in _cooldown_memory: _cooldown_memory[broker_id] = {}

    now = time.time()
    sold_tickers = []
    for ticker, data in _portfolio_memory[broker_id].items():
        if now - data['last_eval'] > 180:
            _cooldown_memory[broker_id][ticker] = {
                'sell_price': data['highest'],
                'sell_time': now
            }
            sold_tickers.append(ticker)

    for t in sold_tickers:
        del _portfolio_memory[broker_id][t]


def _calculate_macd(df, fast=12, slow=26, signal=9):
    if df.empty or len(df) < slow + signal:
        return None, None
    exp1 = df['Close'].ewm(span=fast, adjust=False).mean()
    exp2 = df['Close'].ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd.iloc[-1], sig.iloc[-1]


def _calculate_rsi(df, period=14):
    """Relative Strength Index using Wilder's Smoothing."""
    if df.empty or len(df) < period:
        return None
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]


def _closed_bars(df):
    """Drop the in-progress forming bar when enough history exists (avoid look-ahead)."""
    if df is None or df.empty:
        return df
    if len(df) >= 3:
        return df.iloc[:-1]
    return df


def _get_trend_data(ticker, interval="5m", period="5d"):
    """Shared Yahoo chart math for BOTH brokers: (bullish, uptrend, rsi, has_volume)."""
    cache_key = (str(ticker).upper(), interval, period)
    cached = _trend_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _TREND_CACHE_TTL:
        return cached[1]

    result = (False, False, None, False)
    try:
        df = _get_yf().Ticker(_safe_ticker(ticker)).history(period=period, interval=interval)
        df = _closed_bars(df)
        if df is None or df.empty or len(df) < 20:
            _trend_cache[cache_key] = (time.time(), result)
            return result

        macd, sig = _calculate_macd(df)
        if macd is None or sig is None:
            _trend_cache[cache_key] = (time.time(), result)
            return result

        ema20 = df['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
        rsi = _calculate_rsi(df, RSI_PERIOD)

        current_vol = df['Volume'].iloc[-1]
        avg_vol_1h = df['Volume'].iloc[-13:-1].mean() if len(df) > 13 else current_vol
        has_volume = current_vol >= (avg_vol_1h * 0.8)

        is_uptrend = df['Close'].iloc[-1] > ema20
        # Crossover only — requiring macd > 0 blocked early (still-bullish) entries
        is_bullish = (macd > sig)

        result = (is_bullish, is_uptrend, rsi, has_volume)
    except Exception:
        result = (False, False, None, False)

    _trend_cache[cache_key] = (time.time(), result)
    return result


def _check_hysteresis(ticker, current_price, is_crypto, broker_id):
    """Per-broker re-entry lockout after a sell."""
    if broker_id not in _cooldown_memory: _cooldown_memory[broker_id] = {}
    _auto_detect_sales(broker_id)

    if ticker not in _cooldown_memory[broker_id]:
        return True, ""

    state = _cooldown_memory[broker_id][ticker]
    now = time.time()
    elapsed = now - state['sell_time']
    lockout_time = CRYPTO_COOLDOWN if is_crypto else STOCK_COOLDOWN

    if elapsed < lockout_time:
        return False, f"DO NOT BUY (Cooldown: {int((lockout_time - elapsed)/60)}m left)"

    if elapsed > (lockout_time * 4):
        return True, ""

    if current_price < (state['sell_price'] * 0.98):
        return True, ""

    return False, "DO NOT BUY (Waiting for Dip)"


_state_dirty = False
_last_state_save = 0.0
_STATE_SAVE_MIN_INTERVAL = 3.0  # seconds — avoid writing JSON on every HOLD eval


def save_state(force=False):
    """Persist TTP/cooldown memory so restarts don't wipe trade state.

    Debounced: rapid evaluate_holding calls mark dirty and write at most
    every few seconds unless force=True (SELL / shutdown flush).
    """
    global _state_dirty, _last_state_save
    _state_dirty = True
    now = time.time()
    if not force and (now - _last_state_save) < _STATE_SAVE_MIN_INTERVAL:
        return
    try:
        payload = {
            "portfolio": _portfolio_memory,
            "cooldown": _cooldown_memory,
            "protective": _protective_orders,
            "regime_last_good": _regime_last_good,
            "regime_broker_hourly": _broker_hourly_closes,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        _state_dirty = False
        _last_state_save = now
    except Exception as e:
        print(f"scoring save_state error: {e}")


def flush_state():
    """Write pending scoring memory immediately (end of portfolio pass / exit)."""
    global _state_dirty
    if _state_dirty:
        save_state(force=True)


def load_state():
    """Restore TTP/cooldown/protective/regime memory from disk."""
    global _portfolio_memory, _cooldown_memory, _protective_orders
    global _regime_last_good, _broker_hourly_closes
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        port = data.get("portfolio") or {}
        cool = data.get("cooldown") or {}
        prot = data.get("protective") or {}
        for bid in ("ROBINHOOD", "COINBASE"):
            _portfolio_memory[bid] = port.get(bid, {})
            _cooldown_memory[bid] = cool.get(bid, {})
            _protective_orders[bid] = prot.get(bid, {})
        lg = data.get("regime_last_good") or {}
        if isinstance(lg, dict):
            _regime_last_good = lg
        hourly = data.get("regime_broker_hourly") or {}
        if isinstance(hourly, dict):
            for key in ("SPY", "BTC"):
                rows = hourly.get(key) or []
                if isinstance(rows, list):
                    _broker_hourly_closes[key] = [
                        {"hour": int(r["hour"]), "close": float(r["close"])}
                        for r in rows
                        if isinstance(r, dict) and "hour" in r and "close" in r
                    ][-REGIME_BROKER_RING_MAX:]
        return True
    except Exception as e:
        print(f"scoring load_state error: {e}")
        return False


def get_stop_distance_pct(broker_id, ticker=None, asset_type=""):
    """Hard-stop distance as a positive fraction (matches FEE_PROFILES / broker stop)."""
    fees = _resolve_fee_profile(broker_id, ticker, asset_type)
    return abs(float(fees.get("hard_stop") or -0.035))


def get_trail_pct(broker_id, ticker=None, asset_type=""):
    fees = _resolve_fee_profile(broker_id, ticker, asset_type)
    return float(fees.get("ttp_trail") or 0.008)


def calculate_risk_sizing(equity, buying_power, stop_distance_pct, alloc_ceiling_pct, min_dollars=5.0):
    """
    Primary size = (equity * RISK_PCT) / stop_distance.
    Caps: cash reserve, allocation_pct ceiling, available buying power, min notional.
    """
    bp = float(buying_power or 0.0)
    eq = float(equity or 0.0)
    if eq <= 0:
        eq = bp
    stop_d = float(stop_distance_pct or 0.0)
    if bp <= 0 or stop_d <= 0:
        return 0.0
    deployable = max(0.0, bp * (1.0 - CASH_RESERVE_PCT))
    if deployable < 1.0:
        return 0.0
    risk_budget = eq * RISK_PCT_PER_TRADE
    risk_size = risk_budget / stop_d
    alloc_cap = deployable * max(0.0, float(alloc_ceiling_pct or 0.0))
    trade = min(risk_size, alloc_cap if alloc_cap > 0 else risk_size, deployable)
    trade = round(trade, 2)
    min_d = float(min_dollars or 5.0)
    if trade < min_d:
        if deployable >= min_d and risk_size >= min_d:
            trade = min_d
        else:
            return 0.0
    if trade > deployable:
        trade = round(deployable, 2)
    if trade < 1.0:
        return 0.0
    return trade


def concentration_blocks_buy(ticker, held_tickers, holdings_meta=None, portfolio_value=0.0,
                             proposed_dollars=0.0, is_crypto=False):
    """
    Portfolio concentration heuristics before a buy.
    holdings_meta: optional list of {ticker, value, is_crypto}
    Returns (blocked: bool, reason: str).
    """
    clean = str(ticker or "").replace("-USD", "").upper()
    held = {str(t).replace("-USD", "").upper() for t in (held_tickers or []) if t}
    if clean in held:
        return True, f"already holding {clean}"

    # Cluster caps
    for name, members in CORRELATION_CLUSTERS.items():
        if clean not in members:
            continue
        overlap = held & members
        if len(overlap) >= MAX_CLUSTER_POSITIONS:
            return True, f"cluster {name} full ({', '.join(sorted(overlap))})"

    # Crypto book fraction
    if is_crypto or clean in CRYPTO_TICKERS:
        meta = holdings_meta or []
        crypto_val = 0.0
        for h in meta:
            if h.get("is_crypto") or str(h.get("ticker", "")).upper() in CRYPTO_TICKERS:
                crypto_val += float(h.get("value") or 0.0)
        pv = float(portfolio_value or 0.0)
        if pv > 0:
            projected = (crypto_val + float(proposed_dollars or 0.0)) / pv
            if projected > MAX_CRYPTO_BOOK_FRAC:
                return True, f"crypto book cap ({projected*100:.0f}% > {MAX_CRYPTO_BOOK_FRAC*100:.0f}%)"

    return False, ""


def get_protective_order(broker_id, ticker):
    bid = str(broker_id or "ROBINHOOD").upper()
    if bid not in _protective_orders:
        _protective_orders[bid] = {}
    return _protective_orders[bid].get(str(ticker).upper())


def set_protective_order(broker_id, ticker, order_info):
    bid = str(broker_id or "ROBINHOOD").upper()
    if bid not in _protective_orders:
        _protective_orders[bid] = {}
    key = str(ticker).upper()
    if order_info:
        _protective_orders[bid][key] = order_info
    else:
        _protective_orders[bid].pop(key, None)
    save_state(force=True)


def clear_protective_order(broker_id, ticker):
    set_protective_order(broker_id, ticker, None)


def _regime_proxy_keys(is_crypto):
    """Yahoo symbol + broker ring key for the regime proxy."""
    if is_crypto:
        return "BTC-USD", "BTC"
    return "SPY", "SPY"


def _closes_above_ema(closes, confirm_bars=REGIME_CONFIRM_BARS, buffer=REGIME_EMA_BUFFER):
    """
    True if last confirm_bars closes sit above EMA20 * (1+buffer).
    closes: sequence of floats (oldest → newest). Needs len >= 20 + confirm_bars.
    """
    n = len(closes)
    need = 20 + confirm_bars
    if n < need:
        return False
    ema = float(closes[0])
    alpha = 2.0 / (20 + 1)
    emas = [ema]
    for c in closes[1:]:
        ema = alpha * float(c) + (1.0 - alpha) * ema
        emas.append(ema)
    thresh = 1.0 + buffer
    for i in range(-confirm_bars, 0):
        if float(closes[i]) <= float(emas[i]) * thresh:
            return False
    return True


def _yahoo_regime_vote(proxy, period):
    """
    Source 1 — Yahoo 1H closed bars vs EMA20.
    Returns (available, ok, last_ema20_or_none, detail).
    """
    try:
        df = _get_yf().Ticker(proxy).history(period=period, interval="60m")
        df = _closed_bars(df)
        need = 20 + REGIME_CONFIRM_BARS
        if df is None or df.empty or len(df) < need:
            return False, False, None, "yahoo empty"
        ema20 = df["Close"].ewm(span=20, adjust=False).mean()
        last_ema = float(ema20.iloc[-1])
        thresh = 1.0 + REGIME_EMA_BUFFER
        ok = True
        for i in range(-REGIME_CONFIRM_BARS, 0):
            if float(df["Close"].iloc[i]) <= float(ema20.iloc[i]) * thresh:
                ok = False
                break
        return True, ok, last_ema, "yahoo 1H"
    except Exception:
        return False, False, None, "yahoo error"


def _fetch_broker_regime_price(is_crypto):
    """
    Live proxy price from connected brokers only (no Yahoo fallback here —
    Yahoo is its own vote). BTC: Coinbase then RH crypto. SPY: Robinhood.
    """
    try:
        if is_crypto:
            for broker in (_regime_cb, _regime_rh):
                if broker is None or not getattr(broker, "is_connected", False):
                    continue
                p = float(broker.get_live_price("BTC", allow_yahoo_fallback=False) or 0.0)
                if p > 0:
                    return p
        else:
            if _regime_rh is not None and getattr(_regime_rh, "is_connected", False):
                p = float(_regime_rh.get_live_price("SPY", allow_yahoo_fallback=False) or 0.0)
                if p > 0:
                    return p
    except Exception:
        pass
    return 0.0


def _record_broker_regime_sample(ring_key, price):
    """Throttle-sample live broker price; roll completed clock-hours into ring closes."""
    if price <= 0:
        return
    now = time.time()
    last = _broker_last_sample_ts.get(ring_key, 0.0)
    if now - last < REGIME_BROKER_SAMPLE_GAP:
        return
    _broker_last_sample_ts[ring_key] = now
    samples = _broker_price_samples.setdefault(ring_key, [])
    samples.append({"t": now, "p": float(price)})
    cutoff = now - max(REGIME_BROKER_SHORT_LOOKBACK * 2, 4 * 3600)
    _broker_price_samples[ring_key] = [s for s in samples if s["t"] >= cutoff]

    hour = int(now // 3600)
    prior = hour - 1
    hourly = _broker_hourly_closes.setdefault(ring_key, [])
    if hourly and hourly[-1]["hour"] >= prior:
        return
    prior_ticks = [s for s in _broker_price_samples[ring_key] if int(s["t"] // 3600) == prior]
    if not prior_ticks:
        return
    close_px = float(prior_ticks[-1]["p"])
    if hourly and hourly[-1]["hour"] == prior:
        hourly[-1]["close"] = close_px
    else:
        hourly.append({"hour": prior, "close": close_px})
    _broker_hourly_closes[ring_key] = hourly[-REGIME_BROKER_RING_MAX:]


def _broker_regime_vote(ring_key, is_crypto, yahoo_ema=None):
    """
    Source 2 — broker live.
    Prefer: live vs Yahoo EMA20 (when Yahoo bars gave us an EMA).
    Else: EMA on broker hourly ring if long enough.
    Else: short lookback (live vs ~1.5h-ago sample) — degraded, documented in detail.
    Returns (available, ok, detail).
    """
    live = _fetch_broker_regime_price(is_crypto)
    if live <= 0:
        return False, False, "broker disconnected/no quote"
    _record_broker_regime_sample(ring_key, live)
    thresh = 1.0 + REGIME_EMA_BUFFER

    # Best broker path: live last vs Yahoo's EMA20 (single live print, not N closed bars)
    if yahoo_ema is not None and yahoo_ema > 0:
        ok = live > float(yahoo_ema) * thresh
        return True, ok, "broker live vs yahoo EMA"

    hourly = _broker_hourly_closes.get(ring_key) or []
    if len(hourly) >= REGIME_BROKER_MIN_HOURLY:
        closes = [float(h["close"]) for h in hourly]
        ok = _closes_above_ema(closes)
        return True, ok, "broker hourly EMA"

    samples = _broker_price_samples.get(ring_key) or []
    if len(samples) < 2:
        return False, False, "broker history thin"
    now = time.time()
    target = now - REGIME_BROKER_SHORT_LOOKBACK
    older = None
    for s in samples:
        if s["t"] <= target:
            older = s
        else:
            break
    if older is None:
        span = samples[-1]["t"] - samples[0]["t"]
        if span < REGIME_BROKER_SHORT_LOOKBACK * 0.75:
            return False, False, "broker history thin"
        older = samples[0]
    ok = live > float(older["p"]) * thresh
    return True, ok, "broker short-trend"


def _store_regime_last_good(proxy, ok, source):
    prev = _regime_last_good.get(proxy)
    flipped = (not isinstance(prev, dict)) or (bool(prev.get("ok")) != bool(ok))
    _regime_last_good[proxy] = {
        "ok": bool(ok),
        "ts": time.time(),
        "source": str(source),
    }
    # Force write on verdict flip; otherwise debounce with normal save cadence
    save_state(force=flipped)


def _last_good_regime_vote(proxy):
    """Source 3 — persisted last successful consensus within TTL."""
    lg = _regime_last_good.get(proxy)
    if not isinstance(lg, dict) or "ok" not in lg or "ts" not in lg:
        return False, False, "no last-good"
    age = time.time() - float(lg["ts"])
    if age > REGIME_LAST_GOOD_TTL:
        return False, False, "last-good expired"
    return True, bool(lg["ok"]), f"last-good {lg.get('source', '?')}"


def market_regime_ok(is_crypto=False):
    """
    Hard gate: skip NEW buys when the broad market (BTC or SPY) is in a 1H downtrend.

    Multi-source failover (fail-closed, no Settings maze):
      1) Yahoo — 1H closed bars vs EMA20 (+ buffer, N confirms)
      2) Broker live — CB/RH BTC or RH SPY; vs Yahoo EMA when available,
         else broker hourly ring / short lookback
      3) Last-good cache — last clear verdict within REGIME_LAST_GOOD_TTL

    Consensus: Yahoo+broker agree → that verdict; disagree → block (conservative).
    Yahoo alone OK if bars valid; broker alone OK if enough history; else last-good;
    else fail-closed. Never fail-open on total blackout.
    Returns (ok: bool, reason: str).
    """
    proxy, ring_key = _regime_proxy_keys(is_crypto)
    period = "5d" if is_crypto else "1mo"

    y_avail, y_ok, y_ema, _y_detail = _yahoo_regime_vote(proxy, period)
    b_avail, b_ok, _b_detail = _broker_regime_vote(ring_key, is_crypto, yahoo_ema=y_ema)

    if y_avail and b_avail:
        if y_ok and b_ok:
            _store_regime_last_good(proxy, True, "yahoo+broker")
            return True, ""
        if (not y_ok) and (not b_ok):
            _store_regime_last_good(proxy, False, "yahoo+broker")
            return False, f"DO NOT BUY (Regime: {proxy} 1H Downtrend)"
        # Disagree → fail closed; stamp last-good blocked so TTL cannot re-allow
        _store_regime_last_good(proxy, False, "disagree")
        return False, f"DO NOT BUY (Regime: {proxy} sources disagree — blocked)"

    if y_avail:
        _store_regime_last_good(proxy, y_ok, "yahoo")
        if y_ok:
            return True, ""
        return False, f"DO NOT BUY (Regime: {proxy} 1H Downtrend)"

    if b_avail:
        _store_regime_last_good(proxy, b_ok, "broker")
        if b_ok:
            return True, ""
        return False, f"DO NOT BUY (Regime: {proxy} broker trend down)"

    lg_avail, lg_ok, _lg_detail = _last_good_regime_vote(proxy)
    if lg_avail:
        if lg_ok:
            return True, ""
        return False, f"DO NOT BUY (Regime: {proxy} last-good downtrend)"

    return False, f"DO NOT BUY (Regime: {proxy} data unavailable)"


def buy_rank_score(ticker, is_crypto=True):
    """
    Higher = better candidate among tickers that already passed BUY filters.
    Prefers healthier RSI (not near ceiling) + volume + confirmed micro/macro.
    """
    interval = "5m" if is_crypto else "15m"
    period = "1d" if is_crypto else "5d"
    micro, macro, rsi, has_volume = _get_trend_data(ticker, interval=interval, period=period)
    score = 0.0
    if micro:
        score += 40
    if macro:
        score += 25
    if has_volume:
        score += 15
    if rsi is not None:
        # Sweet spot ~40–55; punish approaching overbought
        score += max(0.0, min(20.0, (RSI_CEILING - rsi)))
    return score


# =========================================================================
# PRIMARY EVALUATION ENGINES
# =========================================================================

def evaluate_holding(ticker, avg_cost, broker_id="ROBINHOOD", asset_type="", live_price=None):
    """
    Trailing take-profit / hard stop / time-stop.
    Fee thresholds change by broker so CB doesn't take thin RH-style exits.
    """
    current_price = float(live_price) if live_price and live_price > 0 else fetch_current_price(ticker)
    if current_price <= 0: return "HOLD (Awaiting Price)"

    # Coinbase (and some RH crypto) often has no avg cost — seed at live price so
    # TTP/time-stop can still manage the position from "now" instead of never selling.
    if avg_cost <= 0:
        avg_cost = current_price

    fees = _resolve_fee_profile(broker_id, ticker, asset_type)
    now = time.time()
    if broker_id not in _portfolio_memory: _portfolio_memory[broker_id] = {}

    _auto_detect_sales(broker_id)

    if ticker not in _portfolio_memory[broker_id]:
        _portfolio_memory[broker_id][ticker] = {'highest': current_price, 'buy_time': now, 'last_eval': now}
    else:
        _portfolio_memory[broker_id][ticker]['last_eval'] = now
        if current_price > _portfolio_memory[broker_id][ticker]['highest']:
            _portfolio_memory[broker_id][ticker]['highest'] = current_price

    highest = _portfolio_memory[broker_id][ticker]['highest']
    held_time_minutes = (now - _portfolio_memory[broker_id][ticker]['buy_time']) / 60.0
    roi = (current_price - avg_cost) / avg_cost

    if roi <= fees["hard_stop"]:
        save_state(force=True)
        return f"SELL (Hard Stop: {roi*100:.2f}%)"

    peak_roi = (highest - avg_cost) / avg_cost
    if peak_roi >= fees["ttp_arm"]:
        trail_trigger_price = highest * (1.0 - fees["ttp_trail"])
        if current_price <= trail_trigger_price:
            save_state(force=True)
            return f"SELL (TTP Triggered - Peak: +{peak_roi*100:.2f}%, Exit: +{roi*100:.2f}%)"
        save_state()
        return f"HOLD (TTP Armed - Peak: +{peak_roi*100:.2f}%)"

    # Green time-exit: bank modest winners that never reach TTP arm (was a profit dead-zone)
    green_min = float(fees.get("time_green_min", 45) or 45)
    green_roi = float(fees.get("time_green_roi", 0.005) or 0.005)
    if held_time_minutes >= green_min and roi >= green_roi:
        save_state(force=True)
        return f"SELL (Time-Green > {green_min:.0f}m, +{roi*100:.2f}%)"

    stale_min = float(fees.get("stale_minutes", 120) or 120)
    if held_time_minutes >= stale_min and roi < fees["stale_roi"]:
        save_state(force=True)
        hrs = stale_min / 60.0
        hrs_lbl = f"{hrs:.0f}h" if abs(hrs - round(hrs)) < 1e-9 else f"{hrs:.1f}h"
        return f"SELL (Stale > {hrs_lbl}, ROI: {roi*100:.2f}%)"
    elif held_time_minutes >= 60 and roi >= fees["time_60m_target"]:
        save_state(force=True)
        return f"SELL (Time-Stop > 1h, +{fees['time_60m_target']*100:.1f}% Target Hit)"
    elif held_time_minutes >= 30 and roi >= fees["time_30m_target"]:
        save_state(force=True)
        return f"SELL (Time-Stop > 30m, +{fees['time_30m_target']*100:.1f}% Target Hit)"

    save_state()
    return f"HOLD (ROI: {roi*100:.2f}%)"


def evaluate_crypto_opportunity(ticker, broker_id="ROBINHOOD", live_price=None):
    current_price = float(live_price) if live_price and live_price > 0 else fetch_current_price(ticker)
    if current_price <= 0: return "DO NOT BUY (Awaiting Price)"

    ok, reason = market_regime_ok(is_crypto=True)
    if not ok: return reason

    allowed, reason = _check_hysteresis(ticker, current_price, is_crypto=True, broker_id=broker_id)
    if not allowed: return reason

    _, macro_uptrend, _, _ = _get_trend_data(ticker, interval="60m", period="5d")
    if not macro_uptrend:
        return "DO NOT BUY (1H Macro Downtrend)"

    micro_bullish, _, rsi, has_volume = _get_trend_data(ticker, interval="5m", period="1d")

    if rsi is None: return "DO NOT BUY (Calculating RSI...)"
    if rsi >= RSI_CEILING: return f"DO NOT BUY (RSI Overbought: {rsi:.1f})"
    if not has_volume: return "DO NOT BUY (Low Volume Fakeout)"

    if micro_bullish:
        return f"BUY (MTF Confirmed | RSI: {rsi:.1f})"

    return "DO NOT BUY (Consolidating)"


def evaluate_opportunity(ticker, is_penny_stock=False, broker_id="ROBINHOOD", live_price=None):
    current_price = float(live_price) if live_price and live_price > 0 else fetch_current_price(ticker)
    if current_price <= 0: return "DO NOT BUY (Awaiting Price)"

    ok, reason = market_regime_ok(is_crypto=False)
    if not ok: return reason

    allowed, reason = _check_hysteresis(ticker, current_price, is_crypto=False, broker_id=broker_id)
    if not allowed: return reason

    interval = "15m" if not is_penny_stock else "5m"
    _, macro_uptrend, _, _ = _get_trend_data(ticker, interval="60m", period="1mo")

    if not macro_uptrend and not is_penny_stock:
        return "DO NOT BUY (1H Macro Downtrend)"

    micro_bullish, _, rsi, has_volume = _get_trend_data(ticker, interval=interval, period="5d")

    if rsi is None: return "DO NOT BUY (Calculating RSI...)"
    if rsi >= RSI_CEILING: return f"DO NOT BUY (RSI Overbought: {rsi:.1f})"
    if not has_volume: return "DO NOT BUY (Low Volume Fakeout)"

    if micro_bullish:
        return f"BUY (MTF Confirmed | RSI: {rsi:.1f})"

    return "DO NOT BUY (Consolidating)"
