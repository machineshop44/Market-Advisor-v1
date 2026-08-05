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
    # E*TRADE equities ≈ commission-free — profit-after-friction rails match RH stock.
    # (Kept as its own profile so we can tune ET independently later.)
    "ETRADE_STOCK": {
        "ttp_arm": 0.012,          # +1.2% arms trail
        "ttp_trail": 0.008,        # -0.8% trail
        "hard_stop": -0.035,       # -3.5%
        "time_30m_target": 0.008,  # +0.8%
        "time_60m_target": 0.006,  # +0.6%
        "time_green_min": 45,
        "time_green_roi": 0.006,   # +0.6% min green exit
        "stale_minutes": 180,
        "stale_roi": -0.015,
    },
}

CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}

CRYPTO_COOLDOWN = 10 * 60   # 10 minutes after selling crypto
STOCK_COOLDOWN = 20 * 60    # 20 minutes after selling stocks
HARD_STOP_COOLDOWN_MULT = 2.0  # hard-stop exits get a longer per-ticker lockout
LOSS_STREAK_WINDOW_SEC = 90 * 60
LOSS_STREAK_TRIGGER = 3        # hard stops in window → broker-wide new-buy pause
LOSS_STREAK_PAUSE_SEC = 45 * 60

RSI_PERIOD = 14
RSI_CEILING = 70            # 70 is standard; 60 was blocking too many real trends
ATR_PERIOD = 14
ATR_SIZING_MULT = 1.5       # size risk distance = max(fee hard-stop, ATR% * mult)
ATR_SIZING_CAP_MULT = 2.0   # never widen sizing stop beyond 2× fee hard-stop
_ATR_CACHE_TTL = 120

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
CASH_RESERVE_PCT = 0.12           # leave 12% buying power undeployed (overridden by target_bp_utilization)
DEFAULT_TARGET_BP_UTILIZATION = 0.88  # deploy most usable BP; idle cash does not earn
DEFAULT_SIZING_FOCUS_SLOTS = 6    # size as if filling next N tickets — not all max_open slots
MAX_CRYPTO_BOOK_FRAC = 0.40       # max crypto share on multi-asset brokers (RH); skipped on crypto-only (CB)
MAX_CLUSTER_POSITIONS = 2         # max open names in one correlation cluster
MAX_SINGLE_NAME_EQUITY_FRAC = 0.15  # soft cap: one name ≤ ~15% of equity
STOCK_LIMIT_FILL_TIMEOUT = 45     # cancel unfilled stock limits after N seconds
PRICE_STALE_SECONDS = 120         # reject buys if live quote older than this (when timestamped)
# Strong buy_rank_score may stretch slot/alloc aim (still hard-capped by risk $ / soft name / deployable)
CONVICTION_ALLOC_MULT_MAX = 1.50  # top-ranked setup → up to 1.5× slot/alloc aim
CONVICTION_SCORE_FLOOR = 65.0     # at/below → 1.0× (baseline)
CONVICTION_SCORE_CEIL = 100.0     # at/above → CONVICTION_ALLOC_MULT_MAX

# Gated scale-in (add-to-held) — optional average-in near support, not blind DCA
SCALE_IN_HISTORY_PERIOD = "6mo"
SCALE_IN_HISTORY_INTERVAL = "1d"
SCALE_IN_MIN_TOUCHES = 2
SCALE_IN_CACHE_TTL = 300  # seconds — daily support levels change slowly
_scale_in_support_cache = {}  # ticker -> (ts, levels_list)

# Risk Posture profiles — one UI choice that retunes concentration + exit patience together.
# Stops / cluster caps stay in force in every mode (Aggressive still has rails).
RISK_POSTURE_PROFILES = {
    "safer": {
        "label": "Safer",
        "hint": "More slots, lower util, tighter name exposure — BTC uptrend required for crypto",
        "require_crypto_regime": True,
        "target_bp_utilization_pct": 75.0,
        "sizing_focus_slots": 8,
        "max_open_positions": 10,
        "max_buys_per_cycle": 2,
        "max_single_name_equity_pct": 10.0,
        "conviction_alloc_mult_max": 1.25,
        "exit_roi_scale": 0.80,   # lower time-exit ROI targets → take profits sooner
        "exit_time_scale": 0.85,  # shorter green wait before banking
        "ttp_arm_scale": 0.85,    # arm trail earlier
        "allow_scale_in": False,
        "scale_in_size_frac": 0.40,
        "scale_in_max_adds": 1,
        "scale_in_roi_min": -0.04,   # deepest add (must be above hard stop)
        "scale_in_roi_max": -0.008,  # must be at least this underwater
        "scale_in_near_pct": 0.012,
        "scale_in_min_score": 55.0,
    },
    "balanced": {
        "label": "Balanced",
        "hint": "Focus 6, ~88% util — crypto uses each coin's trend without a BTC gate",
        "require_crypto_regime": False,
        "target_bp_utilization_pct": 88.0,
        "sizing_focus_slots": 6,
        "max_open_positions": 8,
        "max_buys_per_cycle": 2,
        "max_single_name_equity_pct": 15.0,
        "conviction_alloc_mult_max": 1.50,
        "exit_roi_scale": 1.0,
        "exit_time_scale": 1.0,
        "ttp_arm_scale": 1.0,
        "allow_scale_in": True,
        "scale_in_size_frac": 0.50,
        "scale_in_max_adds": 1,
        "scale_in_roi_min": -0.08,
        "scale_in_roi_max": -0.005,
        "scale_in_near_pct": 0.015,
        "scale_in_min_score": 45.0,
    },
    "aggressive": {
        "label": "Aggressive",
        "hint": "Fewer slots, higher util — crypto uses each coin's trend without a BTC gate",
        "require_crypto_regime": False,
        "target_bp_utilization_pct": 95.0,
        "sizing_focus_slots": 3,
        "max_open_positions": 5,
        "max_buys_per_cycle": 1,
        "max_single_name_equity_pct": 25.0,
        "conviction_alloc_mult_max": 1.75,
        "exit_roi_scale": 1.35,   # need more gain before time-exit
        "exit_time_scale": 1.30,  # wait longer before time-green
        "ttp_arm_scale": 1.25,    # arm trail later — let winners run
        "allow_scale_in": True,
        "scale_in_size_frac": 0.60,
        "scale_in_max_adds": 1,
        "scale_in_roi_min": -0.10,
        "scale_in_roi_max": -0.003,
        "scale_in_near_pct": 0.020,
        "scale_in_min_score": 40.0,
    },
}


def normalize_risk_posture(name):
    key = str(name or "balanced").strip().lower()
    return key if key in RISK_POSTURE_PROFILES else "balanced"


def get_risk_posture_profile(name=None):
    """Return a copy of the posture profile dict (defaults to balanced)."""
    return dict(RISK_POSTURE_PROFILES[normalize_risk_posture(name)])


def crypto_regime_required(posture=None):
    """Only Safer posture requires the broad BTC regime gate for crypto entries."""
    return bool(get_risk_posture_profile(posture).get("require_crypto_regime", False))


def get_scale_in_params(posture=None, settings=None):
    """
    Merge Risk Posture scale-in defaults with optional settings overrides.
    settings.allow_scale_in (bool) wins when explicitly set; posture supplies the rest.
    """
    prof = get_risk_posture_profile(posture)
    s = settings or {}
    out = {
        "allow_scale_in": bool(prof.get("allow_scale_in", False)),
        "scale_in_size_frac": float(prof.get("scale_in_size_frac", 0.50)),
        "scale_in_max_adds": int(prof.get("scale_in_max_adds", 1)),
        "scale_in_roi_min": float(prof.get("scale_in_roi_min", -0.08)),
        "scale_in_roi_max": float(prof.get("scale_in_roi_max", -0.005)),
        "scale_in_near_pct": float(prof.get("scale_in_near_pct", 0.015)),
        "scale_in_min_score": float(prof.get("scale_in_min_score", 45.0)),
    }
    if "allow_scale_in" in s and s.get("allow_scale_in") is not None:
        out["allow_scale_in"] = bool(s.get("allow_scale_in"))
    for key, cast in (
        ("scale_in_size_frac", float),
        ("scale_in_max_adds", int),
        ("scale_in_roi_min", float),
        ("scale_in_roi_max", float),
        ("scale_in_near_pct", float),
        ("scale_in_min_score", float),
    ):
        if key in s and s.get(key) is not None:
            try:
                out[key] = cast(s.get(key))
            except (TypeError, ValueError):
                pass
    out["scale_in_size_frac"] = min(0.75, max(0.25, float(out["scale_in_size_frac"])))
    out["scale_in_max_adds"] = max(0, min(3, int(out["scale_in_max_adds"])))
    out["scale_in_near_pct"] = min(0.05, max(0.005, float(out["scale_in_near_pct"])))
    # Ensure roi_min (deeper loss) <= roi_max (shallower)
    if out["scale_in_roi_min"] > out["scale_in_roi_max"]:
        out["scale_in_roi_min"], out["scale_in_roi_max"] = (
            out["scale_in_roi_max"], out["scale_in_roi_min"]
        )
    return out


def apply_exit_posture(fees, exit_roi_scale=1.0, exit_time_scale=1.0, ttp_arm_scale=1.0):
    """
    Scale time-based take-profit / TTP-arm thresholds on a fee-profile copy.
    Hard stops and cluster rails are not relaxed here.
    """
    out = dict(fees or {})
    try:
        roi_s = max(0.50, min(2.0, float(exit_roi_scale)))
    except (TypeError, ValueError):
        roi_s = 1.0
    try:
        time_s = max(0.50, min(2.0, float(exit_time_scale)))
    except (TypeError, ValueError):
        time_s = 1.0
    try:
        arm_s = max(0.50, min(2.0, float(ttp_arm_scale)))
    except (TypeError, ValueError):
        arm_s = 1.0
    for key in ("time_green_roi", "time_30m_target", "time_60m_target"):
        if key in out and out[key] is not None:
            try:
                out[key] = float(out[key]) * roi_s
            except (TypeError, ValueError):
                pass
    if "time_green_min" in out and out["time_green_min"] is not None:
        try:
            out["time_green_min"] = max(15.0, float(out["time_green_min"]) * time_s)
        except (TypeError, ValueError):
            pass
    if "ttp_arm" in out and out["ttp_arm"] is not None:
        try:
            out["ttp_arm"] = float(out["ttp_arm"]) * arm_s
        except (TypeError, ValueError):
            pass
    return out


# Practical concentration heuristics (maintainable, no quant library)
CORRELATION_CLUSTERS = {
    "MAG7": {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA"},
    "BTC_BETA": {"BTC", "ETH", "SOL", "IBIT", "FBTC", "BITO", "GBTC", "MSTR", "ETHE"},
    "SEMI": {"NVDA", "AMD", "AVGO", "TSM", "SMCI", "SOXL", "SOXX", "INTC"},
    "MEME_CRYPTO": {"DOGE", "SHIB", "PEPE", "BONK"},
}

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scoring_state.json")


def _normalize_broker_id(broker_id):
    """Map display names / aliases to canonical fee-profile broker ids."""
    bid = str(broker_id or "ROBINHOOD").strip().upper()
    aliases = {
        "E*TRADE": "ETRADE",
        "ETRADE": "ETRADE",
        "ROBINHOOD": "ROBINHOOD",
        "COINBASE": "COINBASE",
        "COINBASE ADVANCED": "COINBASE",
    }
    return aliases.get(bid, bid.replace("*", "").replace(" ", "_"))


def _resolve_fee_profile(broker_id, ticker=None, asset_type=""):
    """
    Return a *copy* of the fee profile for this broker+asset.
    Never returns the shared dict itself, so one broker cannot mutate another's thresholds.
    """
    bid = _normalize_broker_id(broker_id)
    clean = str(ticker or "").replace("-USD", "").upper()
    is_crypto = (
        "CRYPTO" in str(asset_type).upper()
        or clean in CRYPTO_TICKERS
        or bid == "COINBASE"
    )
    if bid == "COINBASE":
        key = "COINBASE"
    elif bid == "ETRADE":
        # Commission-free equities — same profit-after-friction rails as RH stock
        key = "ETRADE_STOCK"
    elif is_crypto:
        key = "ROBINHOOD_CRYPTO"
    else:
        key = "ROBINHOOD_STOCK"
    return dict(FEE_PROFILES[key])  # copy — isolated per call


# =========================================================================
# IN-MEMORY STATE TRACKERS (MULTI-BROKER ISOLATED)
# =========================================================================
_KNOWN_BROKER_IDS = ("ROBINHOOD", "COINBASE", "ETRADE")
_portfolio_memory = {b: {} for b in _KNOWN_BROKER_IDS}
_cooldown_memory = {b: {} for b in _KNOWN_BROKER_IDS}
_protective_orders = {b: {} for b in _KNOWN_BROKER_IDS}  # ticker -> {order_id, kind, ...}
_scale_in_counts = {b: {} for b in _KNOWN_BROKER_IDS}  # ticker -> adds already taken
# broker -> {"events": [ts, ...], "pause_until": float}
_loss_streak = {b: {"events": [], "pause_until": 0.0} for b in _KNOWN_BROKER_IDS}
_trend_cache = {}
_TREND_CACHE_TTL = 45  # seconds
_atr_cache = {}  # ticker -> (ts, atr_pct or None)

# Regime multi-source state (GUI registers connected broker adapters)
_regime_rh = None   # RobinhoodAdapter or None
_regime_cb = None   # CoinbaseAdapter or None
_regime_et = None   # ETradeAdapter — optional SPY fallback
# proxy key -> {"ok": bool, "ts": float, "source": str}
_regime_last_good = {}
# ring key "SPY"|"BTC" -> [{"hour": int, "close": float}, ...]
_broker_hourly_closes = {"SPY": [], "BTC": []}
# recent ticks for short-trend + building hourly closes
_broker_price_samples = {"SPY": [], "BTC": []}
_broker_last_sample_ts = {"SPY": 0.0, "BTC": 0.0}


def register_regime_brokers(robinhood=None, coinbase=None, etrade=None):
    """GUI wires live adapters so regime can read SPY (RH/ET) / BTC (CB then RH)."""
    global _regime_rh, _regime_cb, _regime_et
    if robinhood is not None:
        _regime_rh = robinhood
    if coinbase is not None:
        _regime_cb = coinbase
    if etrade is not None:
        _regime_et = etrade


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
    broker_id = _normalize_broker_id(broker_id)
    if broker_id not in _portfolio_memory: return
    if broker_id not in _cooldown_memory: _cooldown_memory[broker_id] = {}

    now = time.time()
    sold_tickers = []
    for ticker, data in _portfolio_memory[broker_id].items():
        if now - data['last_eval'] > 180:
            reason = data.get("exit_reason") or ""
            # Avoid double-counting streak if evaluate_holding already recorded hard_stop
            already = bool(data.get("loss_recorded"))
            _apply_cooldown(
                broker_id, ticker,
                sell_price=data.get("highest") or 0.0,
                reason=reason,
                record_streak=(reason == "hard_stop" and not already),
            )
            sold_tickers.append(ticker)

    for t in sold_tickers:
        del _portfolio_memory[broker_id][t]
        if broker_id in _scale_in_counts:
            _scale_in_counts[broker_id].pop(t, None)


def _apply_cooldown(broker_id, ticker, sell_price, reason="", record_streak=False):
    """Write per-ticker cooldown; optionally bump hard-stop loss streak."""
    broker_id = _normalize_broker_id(broker_id)
    if broker_id not in _cooldown_memory:
        _cooldown_memory[broker_id] = {}
    key = str(ticker or "").replace("-USD", "").upper()
    _cooldown_memory[broker_id][key] = {
        "sell_price": float(sell_price or 0.0),
        "sell_time": time.time(),
        "reason": str(reason or ""),
    }
    if record_streak and reason == "hard_stop":
        _record_hard_stop_streak(broker_id)


def _record_hard_stop_streak(broker_id):
    """Track clustered hard stops; pause new buys after LOSS_STREAK_TRIGGER hits."""
    broker_id = _normalize_broker_id(broker_id)
    if broker_id not in _loss_streak:
        _loss_streak[broker_id] = {"events": [], "pause_until": 0.0}
    now = time.time()
    state = _loss_streak[broker_id]
    events = [t for t in (state.get("events") or []) if now - float(t) <= LOSS_STREAK_WINDOW_SEC]
    events.append(now)
    state["events"] = events[-12:]
    if len(events) >= LOSS_STREAK_TRIGGER:
        state["pause_until"] = max(float(state.get("pause_until") or 0.0), now + LOSS_STREAK_PAUSE_SEC)
        # Reset window so one pause does not instantly re-trigger
        state["events"] = []
    save_state(force=True)


def _loss_streak_block(broker_id):
    """Return (blocked, reason) when broker is in a loss-streak pause."""
    broker_id = _normalize_broker_id(broker_id)
    state = _loss_streak.get(broker_id) or {}
    pause_until = float(state.get("pause_until") or 0.0)
    now = time.time()
    if pause_until > now:
        mins = int((pause_until - now) / 60) + 1
        return False, f"DO NOT BUY (Loss-streak pause: {mins}m left)"
    return True, ""


def get_scale_in_count(broker_id, ticker):
    bid = _normalize_broker_id(broker_id)
    key = str(ticker or "").replace("-USD", "").upper()
    return int((_scale_in_counts.get(bid) or {}).get(key, 0) or 0)


def record_scale_in(broker_id, ticker):
    """Increment add count after a successful scale-in fill."""
    bid = _normalize_broker_id(broker_id)
    key = str(ticker or "").replace("-USD", "").upper()
    if bid not in _scale_in_counts:
        _scale_in_counts[bid] = {}
    _scale_in_counts[bid][key] = get_scale_in_count(bid, key) + 1
    save_state(force=True)
    return _scale_in_counts[bid][key]


def clear_scale_in_count(broker_id, ticker):
    bid = _normalize_broker_id(broker_id)
    key = str(ticker or "").replace("-USD", "").upper()
    if bid in _scale_in_counts:
        _scale_in_counts[bid].pop(key, None)


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
    broker_id = _normalize_broker_id(broker_id)
    if broker_id not in _cooldown_memory: _cooldown_memory[broker_id] = {}
    _auto_detect_sales(broker_id)

    allowed, reason = _loss_streak_block(broker_id)
    if not allowed:
        return False, reason

    key = str(ticker or "").replace("-USD", "").upper()
    if key not in _cooldown_memory[broker_id] and ticker not in _cooldown_memory[broker_id]:
        return True, ""

    state = _cooldown_memory[broker_id].get(key) or _cooldown_memory[broker_id].get(ticker)
    if not state:
        return True, ""

    now = time.time()
    elapsed = now - state['sell_time']
    lockout_time = CRYPTO_COOLDOWN if is_crypto else STOCK_COOLDOWN
    if str(state.get("reason") or "") == "hard_stop":
        lockout_time = int(lockout_time * HARD_STOP_COOLDOWN_MULT)

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
            "scale_in_counts": _scale_in_counts,
            "loss_streak": _loss_streak,
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
    global _portfolio_memory, _cooldown_memory, _protective_orders, _scale_in_counts
    global _loss_streak, _regime_last_good, _broker_hourly_closes
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        port = data.get("portfolio") or {}
        cool = data.get("cooldown") or {}
        prot = data.get("protective") or {}
        sic = data.get("scale_in_counts") or {}
        for bid in _KNOWN_BROKER_IDS:
            _portfolio_memory[bid] = port.get(bid, {})
            _cooldown_memory[bid] = cool.get(bid, {})
            _protective_orders[bid] = prot.get(bid, {})
            raw = sic.get(bid, {}) if isinstance(sic, dict) else {}
            _scale_in_counts[bid] = {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        _scale_in_counts[bid][str(k).upper()] = int(v)
                    except (TypeError, ValueError):
                        pass
        ls = data.get("loss_streak") or {}
        if isinstance(ls, dict):
            for bid in _KNOWN_BROKER_IDS:
                row = ls.get(bid) or {}
                if not isinstance(row, dict):
                    continue
                events = []
                for t in row.get("events") or []:
                    try:
                        events.append(float(t))
                    except (TypeError, ValueError):
                        pass
                try:
                    pause_until = float(row.get("pause_until") or 0.0)
                except (TypeError, ValueError):
                    pause_until = 0.0
                _loss_streak[bid] = {"events": events[-12:], "pause_until": pause_until}
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


def _atr_pct(ticker, period=ATR_PERIOD):
    """ATR(period) / last close on 60m bars — None when history is thin."""
    key = str(ticker or "").upper()
    cached = _atr_cache.get(key)
    if cached and (time.time() - cached[0]) < _ATR_CACHE_TTL:
        return cached[1]
    atr_frac = None
    try:
        df = _get_yf().Ticker(_safe_ticker(ticker)).history(period="10d", interval="60m")
        df = _closed_bars(df)
        if df is not None and not df.empty and len(df) >= period + 2:
            high = df["High"]
            low = df["Low"]
            close = df["Close"]
            prev_close = close.shift(1)
            tr = (high - low).to_frame("hl")
            tr["hc"] = (high - prev_close).abs()
            tr["lc"] = (low - prev_close).abs()
            true_range = tr.max(axis=1)
            atr = true_range.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
            last = float(close.iloc[-1] or 0.0)
            if last > 0 and atr == atr:  # not NaN
                atr_frac = max(0.0, float(atr) / last)
    except Exception:
        atr_frac = None
    _atr_cache[key] = (time.time(), atr_frac)
    return atr_frac


def get_stop_distance_pct(broker_id, ticker=None, asset_type="", *, for_sizing=False):
    """
    Hard-stop distance as a positive fraction (matches FEE_PROFILES / broker stop).
    When for_sizing=True and ticker is known, widen toward ATR so high-vol names
    get smaller dollar size (risk $ / wider stop) without changing the exit hard-stop.
    """
    fees = _resolve_fee_profile(broker_id, ticker, asset_type)
    base = abs(float(fees.get("hard_stop") or -0.035))
    if not for_sizing or not ticker:
        return base
    atr = _atr_pct(ticker)
    if atr is None or atr <= 0:
        return base
    widened = max(base, float(atr) * ATR_SIZING_MULT)
    return min(widened, base * ATR_SIZING_CAP_MULT)


def get_trail_pct(broker_id, ticker=None, asset_type=""):
    fees = _resolve_fee_profile(broker_id, ticker, asset_type)
    return float(fees.get("ttp_trail") or 0.008)


def conviction_alloc_multiplier(score=None, mult_max=None):
    """
    Map buy_rank_score → stretch factor in [1.0, mult_max].
    Missing/weak score keeps baseline (1.0). Strong rank may use more of BP under the
    same hard risk $, soft name-cap, and utilization caps — min_trade is never the target.
    """
    try:
        cap = float(mult_max) if mult_max is not None else float(CONVICTION_ALLOC_MULT_MAX)
    except (TypeError, ValueError):
        cap = float(CONVICTION_ALLOC_MULT_MAX)
    cap = max(1.0, min(2.5, cap))
    try:
        s = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        s = 0.0
    if s <= CONVICTION_SCORE_FLOOR:
        return 1.0
    span = max(1e-9, CONVICTION_SCORE_CEIL - CONVICTION_SCORE_FLOOR)
    t = min(1.0, (s - CONVICTION_SCORE_FLOOR) / span)
    return 1.0 + t * (cap - 1.0)


def risk_sizing_breakdown(equity, buying_power, stop_distance_pct, alloc_ceiling_pct,
                          min_dollars=5.0, conviction_score=None,
                          open_count=None, max_open_positions=None,
                          target_bp_utilization=None, sizing_focus_slots=None,
                          soft_name_equity_frac=None, conviction_mult_max=None,
                          existing_name_value=0.0, size_frac=1.0):
    """
    Full sizing math + skip diagnostics.

    Returns dict with trade (float), skip_reason (str|None), and intermediate
    caps (aim, soft_room, soft_cap, risk_size, deployable, already, …).

    size_frac: scale-in multiplier applied to *aim* before hard/soft caps — not
    after. That way a 50% add on a small account can still lift to min_dollars
    when remaining name room and BP allow it (old path: full size × frac often
    fell under min and skipped despite usable soft-cap room).
    """
    out = {
        "trade": 0.0,
        "skip_reason": None,
        "aim": 0.0,
        "aim_raw": 0.0,
        "soft_cap": 0.0,
        "soft_room": 0.0,
        "risk_size": 0.0,
        "deployable": 0.0,
        "already": 0.0,
        "name_limit": 0.0,
        "size_frac": 1.0,
        "min_dollars": float(min_dollars or 5.0),
        "equity": 0.0,
    }
    bp = float(buying_power or 0.0)
    eq = float(equity or 0.0)
    if eq <= 0:
        eq = bp
    out["equity"] = eq
    stop_d = float(stop_distance_pct or 0.0)
    min_d = float(min_dollars or 5.0)
    out["min_dollars"] = min_d
    if bp <= 0 or stop_d <= 0:
        out["skip_reason"] = "no buying power / invalid stop"
        return out

    try:
        if target_bp_utilization is None:
            util = float(DEFAULT_TARGET_BP_UTILIZATION)
        else:
            util = float(target_bp_utilization)
            if util > 1.0:
                util = util / 100.0
    except (TypeError, ValueError):
        util = float(DEFAULT_TARGET_BP_UTILIZATION)
    util = min(0.99, max(0.50, util))
    deployable = max(0.0, bp * util)
    out["deployable"] = round(deployable, 2)
    if deployable < 1.0:
        out["skip_reason"] = f"deployable ${deployable:.2f} too low"
        return out

    risk_budget = eq * RISK_PCT_PER_TRADE
    risk_size = risk_budget / stop_d
    out["risk_size"] = round(risk_size, 2)
    alloc_pct = max(0.0, float(alloc_ceiling_pct or 0.0))
    alloc_base = deployable * alloc_pct
    mult = conviction_alloc_multiplier(conviction_score, mult_max=conviction_mult_max)

    try:
        max_open = int(max_open_positions) if max_open_positions is not None else 0
    except (TypeError, ValueError):
        max_open = 0
    try:
        open_n = max(0, int(open_count or 0))
    except (TypeError, ValueError):
        open_n = 0
    try:
        focus = int(sizing_focus_slots) if sizing_focus_slots is not None else int(DEFAULT_SIZING_FOCUS_SLOTS)
    except (TypeError, ValueError):
        focus = int(DEFAULT_SIZING_FOCUS_SLOTS)
    focus = max(1, min(20, focus))

    if max_open > 0:
        remaining_slots = max(1, max_open - open_n)
        slots_for_sizing = max(1, min(remaining_slots, focus))
    else:
        slots_for_sizing = focus
    slot_target = deployable / float(slots_for_sizing)
    aim_raw = max(slot_target, alloc_base) * mult
    out["aim_raw"] = round(aim_raw, 2)

    try:
        frac = float(size_frac)
    except (TypeError, ValueError):
        frac = 1.0
    if frac < 0.99:
        frac = max(0.25, min(0.75, frac))
    else:
        frac = 1.0
    out["size_frac"] = frac
    aim = aim_raw * frac
    out["aim"] = round(aim, 2)

    try:
        if soft_name_equity_frac is None:
            name_frac = float(MAX_SINGLE_NAME_EQUITY_FRAC)
        else:
            name_frac = float(soft_name_equity_frac)
            if name_frac > 1.0:
                name_frac = name_frac / 100.0
    except (TypeError, ValueError):
        name_frac = float(MAX_SINGLE_NAME_EQUITY_FRAC)
    name_frac = min(0.40, max(0.05, name_frac))
    soft_cap = eq * name_frac
    try:
        already = max(0.0, float(existing_name_value or 0.0))
    except (TypeError, ValueError):
        already = 0.0
    soft_room = max(0.0, soft_cap - already)
    name_limit = soft_room if already > 0 else soft_cap
    out["soft_cap"] = round(soft_cap, 2)
    out["soft_room"] = round(soft_room, 2)
    out["already"] = round(already, 2)
    out["name_limit"] = round(name_limit, 2)

    trade = min(aim, risk_size, name_limit, deployable)
    trade = round(trade, 2)

    if trade < min_d:
        if deployable >= min_d and risk_size >= min_d and name_limit >= min_d:
            # Floor to min when caps still allow a ticket (incl. scale-in with room)
            trade = min_d
        else:
            if already > 0 and soft_room < min_d:
                if soft_room <= 0.01:
                    out["skip_reason"] = (
                        f"name soft cap full "
                        f"(${already:.2f} / ${soft_cap:.2f} cap)"
                    )
                else:
                    out["skip_reason"] = (
                        f"remaining name room ${soft_room:.2f} < min ${min_d:.2f}"
                    )
            elif deployable < min_d:
                out["skip_reason"] = (
                    f"deployable ${deployable:.2f} < min ${min_d:.2f}"
                )
            elif risk_size < min_d:
                out["skip_reason"] = (
                    f"risk size ${risk_size:.2f} < min ${min_d:.2f}"
                )
            else:
                out["skip_reason"] = (
                    f"sized add ${trade:.2f} < min ${min_d:.2f}"
                )
            return out

    if trade > deployable:
        trade = round(deployable, 2)
    if trade < 1.0:
        out["skip_reason"] = f"sized add ${trade:.2f} too small"
        return out
    out["trade"] = trade
    out["skip_reason"] = None
    return out


def calculate_risk_sizing(equity, buying_power, stop_distance_pct, alloc_ceiling_pct,
                          min_dollars=5.0, conviction_score=None,
                          open_count=None, max_open_positions=None,
                          target_bp_utilization=None, sizing_focus_slots=None,
                          soft_name_equity_frac=None, conviction_mult_max=None,
                          existing_name_value=0.0, size_frac=1.0):
    """
    Buying-power-aware concentrated sizing:
      aim ≈ max(deployable / focus_slots, alloc% × deployable) × conviction × size_frac
    where focus_slots = min(remaining open capacity, sizing_focus_slots).

    Deploy most usable BP into fewer high-conviction tickets rather than spraying
    min clips across a large max_open book. Hard/soft caps (smallest wins): risk $
    (equity × RISK_PCT / stop), soft single-name equity frac, deployable BP.

    existing_name_value: already-held notional for this ticker (scale-in) — soft cap
    applies to total name exposure, so only remaining room is usable.
    size_frac: applied to aim before caps (scale-in); min_dollars may still floor
    the ticket when remaining name room / BP / risk allow it.

    min_dollars is a hard floor / skip threshold only — never the intended size when
    risk budget and buying power support a larger notional.
    """
    detail = risk_sizing_breakdown(
        equity, buying_power, stop_distance_pct, alloc_ceiling_pct,
        min_dollars=min_dollars, conviction_score=conviction_score,
        open_count=open_count, max_open_positions=max_open_positions,
        target_bp_utilization=target_bp_utilization,
        sizing_focus_slots=sizing_focus_slots,
        soft_name_equity_frac=soft_name_equity_frac,
        conviction_mult_max=conviction_mult_max,
        existing_name_value=existing_name_value,
        size_frac=size_frac,
    )
    return float(detail.get("trade") or 0.0)


def concentration_blocks_buy(ticker, held_tickers, holdings_meta=None, portfolio_value=0.0,
                             proposed_dollars=0.0, is_crypto=False, allow_held_scale_in=False,
                             crypto_only_broker=False):
    """
    Portfolio concentration heuristics before a buy.
    holdings_meta: optional list of {ticker, value, is_crypto}
    Returns (blocked: bool, reason: str).

    allow_held_scale_in: when True, skip the hard "already holding" block so a gated
    scale-in can proceed; cluster/crypto-book rails still apply for *new* names.
    Same-ticker adds do not consume a cluster slot (already counted).

    crypto_only_broker: Coinbase (and any broker with supports_equities=False). The book
    *is* crypto — do not apply MAX_CRYPTO_BOOK_FRAC (cash reserve / BP util is the rail).
    Multi-asset brokers (Robinhood) keep the equity-wide crypto book cap.
    """
    clean = str(ticker or "").replace("-USD", "").upper()
    held = {str(t).replace("-USD", "").upper() for t in (held_tickers or []) if t}
    already_held = clean in held
    if already_held and not allow_held_scale_in:
        return True, f"already holding {clean}"

    # Cluster caps — only for new entries into a theme (scale-in already owns the slot)
    if not already_held:
        for name, members in CORRELATION_CLUSTERS.items():
            if clean not in members:
                continue
            overlap = held & members
            if len(overlap) >= MAX_CLUSTER_POSITIONS:
                return True, f"cluster {name} full ({', '.join(sorted(overlap))})"

    # Crypto book fraction — multi-asset brokers only (RH stocks+crypto)
    if not crypto_only_broker and (is_crypto or clean in CRYPTO_TICKERS):
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


def portfolio_buy_rank_adjust(ticker, held_tickers, holdings_meta=None, portfolio_value=0.0,
                              is_crypto=False, scale_in_candidate=False,
                              crypto_only_broker=False):
    """
    Soft ranking delta so new buys prefer names that fit the *current* book.
    Hard blocks stay in concentration_blocks_buy — this only reshuffles priority
    among tickers that already passed BUY filters (unheld / underweight themes first).

    scale_in_candidate: held name that passed evaluate_scale_in — mild penalty vs fresh
    entries instead of the hard -1000 bury.

    crypto_only_broker: skip crypto-share soft penalties (book is expected to be ~all crypto).
    """
    clean = str(ticker or "").replace("-USD", "").upper()
    held = {str(t).replace("-USD", "").upper() for t in (held_tickers or []) if t}
    delta = 0.0

    if clean in held:
        if scale_in_candidate:
            return -15.0  # prefer fresh tickets when scores are close
        return -1000.0

    for _name, members in CORRELATION_CLUSTERS.items():
        if clean not in members:
            continue
        overlap = held & members
        n = len(overlap)
        if n >= MAX_CLUSTER_POSITIONS:
            return -1000.0
        if n == 0:
            delta += 10.0
        else:
            # One slot already used — still allowed, prefer a fresher theme when scores are close
            delta -= 12.0

    if crypto_only_broker:
        return delta

    meta = holdings_meta or []
    crypto_val = 0.0
    for h in meta:
        if h.get("is_crypto") or str(h.get("ticker", "")).upper() in CRYPTO_TICKERS:
            crypto_val += float(h.get("value") or 0.0)
    pv = float(portfolio_value or 0.0)
    if pv > 0:
        frac = crypto_val / pv
        want_crypto = bool(is_crypto) or clean in CRYPTO_TICKERS
        if want_crypto:
            if frac >= MAX_CRYPTO_BOOK_FRAC * 0.75:  # ≥30%
                delta -= 15.0
            elif frac >= MAX_CRYPTO_BOOK_FRAC * 0.5:  # ≥20%
                delta -= 6.0
            elif frac < 0.10:
                delta += 8.0
        else:
            if frac >= MAX_CRYPTO_BOOK_FRAC * 0.75:
                delta += 10.0

    return delta


def get_protective_order(broker_id, ticker):
    bid = _normalize_broker_id(broker_id)
    if bid not in _protective_orders:
        _protective_orders[bid] = {}
    return _protective_orders[bid].get(str(ticker).upper())


def set_protective_order(broker_id, ticker, order_info):
    bid = _normalize_broker_id(broker_id)
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
            for broker in (_regime_rh, _regime_et):
                if broker is None or not getattr(broker, "is_connected", False):
                    continue
                p = float(broker.get_live_price("SPY", allow_yahoo_fallback=False) or 0.0)
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


def _swing_low_levels(lows, cluster_pct=0.015):
    """
    Local minima clustered into revisit levels.
    Returns list of (level, touch_count) sorted by touches desc.
    """
    n = len(lows)
    if n < 5:
        return []
    swings = []
    for i in range(2, n - 2):
        w = lows[i]
        if w <= lows[i - 1] and w <= lows[i - 2] and w <= lows[i + 1] and w <= lows[i + 2]:
            swings.append(float(w))
    if not swings:
        return []
    swings.sort()
    clusters = []  # [level_sum, count]
    for px in swings:
        placed = False
        for c in clusters:
            lvl = c[0] / c[1]
            if lvl > 0 and abs(px - lvl) / lvl <= cluster_pct:
                c[0] += px
                c[1] += 1
                placed = True
                break
        if not placed:
            clusters.append([px, 1])
    levels = [(c[0] / c[1], c[1]) for c in clusters]
    levels.sort(key=lambda x: (-x[1], x[0]))
    return levels


def _price_near_level(price, level, near_pct):
    """True if price is at/near level, allowing a wider approach from below."""
    try:
        px = float(price)
        lv = float(level)
    except (TypeError, ValueError):
        return False
    if px <= 0 or lv <= 0:
        return False
    dist = (px - lv) / lv
    # Slightly above → within near_pct; below → up to ~2× near_pct (approaching support)
    return (-near_pct * 2.0) <= dist <= near_pct


def _load_daily_lows(ticker, period=SCALE_IN_HISTORY_PERIOD):
    """Yahoo daily lows for support detection (cached). Returns list of floats or []."""
    key = str(ticker or "").upper()
    cached = _scale_in_support_cache.get(key)
    if cached and (time.time() - cached[0]) < SCALE_IN_CACHE_TTL:
        return cached[1]
    lows = []
    try:
        df = _get_yf().Ticker(_safe_ticker(ticker)).history(
            period=period, interval=SCALE_IN_HISTORY_INTERVAL
        )
        df = _closed_bars(df)
        if df is not None and not df.empty and "Low" in df.columns:
            lows = [float(x) for x in df["Low"].tolist() if float(x) > 0]
    except Exception:
        lows = []
    _scale_in_support_cache[key] = (time.time(), lows)
    return lows


def find_support_revisit(ticker, current_price, near_pct=0.015, min_touches=SCALE_IN_MIN_TOUCHES,
                         avg_cost=None):
    """
    Detect whether price is near a multi-touch revisit zone and/or cost basis.
    Returns (ok: bool, level: float|None, detail: str).
    """
    try:
        px = float(current_price or 0.0)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return False, None, "no price"

    # Cost-basis zone (average-in near entry)
    try:
        cost = float(avg_cost) if avg_cost is not None else 0.0
    except (TypeError, ValueError):
        cost = 0.0
    if cost > 0 and _price_near_level(px, cost, near_pct):
        return True, cost, f"near cost basis ${cost:.2f}"

    lows = _load_daily_lows(ticker)
    if len(lows) < 20:
        # Fall back: recent swing low from whatever we have
        if len(lows) >= 5:
            recent = min(lows[-20:]) if len(lows) >= 5 else min(lows)
            if _price_near_level(px, recent, near_pct):
                return True, recent, f"near recent swing low ${recent:.2f}"
        if cost > 0:
            return False, None, "no 6m support history"
        return False, None, "no support history"

    levels = _swing_low_levels(lows, cluster_pct=max(near_pct, 0.012))
    for level, touches in levels:
        if touches < min_touches:
            continue
        if _price_near_level(px, level, near_pct):
            return True, level, f"near {SCALE_IN_HISTORY_PERIOD} revisit ${level:.2f} ({touches}x)"

    # Single recent swing low still counts as mean-reversion character
    recent_low = min(lows[-40:]) if len(lows) >= 10 else min(lows)
    if _price_near_level(px, recent_low, near_pct):
        return True, recent_low, f"near prior swing low ${recent_low:.2f}"

    return False, None, "not near support/cost zone"


def evaluate_scale_in(ticker, current_price, avg_cost, broker_id="ROBINHOOD",
                      asset_type="", is_crypto=False, signal_score=None,
                      posture=None, settings=None, existing_name_value=0.0,
                      portfolio_value=0.0):
    """
    Gate whether an already-held name may receive an add (scale-in).
    Returns dict: allowed, reason, support_level, roi, adds_used, size_frac, score, detail.
    """
    clean = str(ticker or "").replace("-USD", "").upper()
    params = get_scale_in_params(posture=posture, settings=settings)
    result = {
        "allowed": False,
        "reason": "",
        "support_level": None,
        "roi": None,
        "adds_used": get_scale_in_count(broker_id, clean),
        "size_frac": float(params["scale_in_size_frac"]),
        "score": None,
        "detail": "",
        "params": params,
    }

    if not params["allow_scale_in"]:
        result["reason"] = "scale-in disabled"
        return result

    max_adds = int(params["scale_in_max_adds"])
    if max_adds <= 0:
        result["reason"] = "max adds = 0"
        return result
    if result["adds_used"] >= max_adds:
        result["reason"] = f"max adds reached ({result['adds_used']}/{max_adds})"
        return result

    try:
        px = float(current_price or 0.0)
        cost = float(avg_cost or 0.0)
    except (TypeError, ValueError):
        px, cost = 0.0, 0.0
    if px <= 0:
        result["reason"] = "missing price"
        return result
    if cost <= 0:
        result["reason"] = "missing cost basis"
        return result

    roi = (px - cost) / cost
    result["roi"] = roi

    # Never add into/through the hard-stop zone
    hard_stop = -abs(float(get_stop_distance_pct(broker_id, ticker=clean, asset_type=asset_type)))
    # Stay a small buffer above the stop so we don't average into a freefall cut
    stop_floor = hard_stop + 0.005
    roi_min = max(float(params["scale_in_roi_min"]), stop_floor)
    roi_max = float(params["scale_in_roi_max"])
    if roi <= hard_stop:
        result["reason"] = f"past hard stop ({roi*100:.2f}% <= {hard_stop*100:.1f}%)"
        return result
    if roi < roi_min:
        result["reason"] = f"drawdown too deep for add ({roi*100:.2f}% < {roi_min*100:.1f}%)"
        return result
    if roi > roi_max:
        result["reason"] = f"not in add ROI band ({roi*100:.2f}% > {roi_max*100:.1f}%)"
        return result

    near_pct = float(params["scale_in_near_pct"])
    ok_sup, level, detail = find_support_revisit(
        clean, px, near_pct=near_pct, avg_cost=cost,
    )
    result["support_level"] = level
    result["detail"] = detail
    if not ok_sup:
        result["reason"] = f"not near support ({detail})"
        return result

    # Constructive score (dedicated path uses same rank components)
    try:
        score = float(signal_score) if signal_score is not None else float(
            buy_rank_score(clean, is_crypto=bool(is_crypto) or clean in CRYPTO_TICKERS)
        )
    except (TypeError, ValueError):
        score = 0.0
    result["score"] = score
    min_score = float(params["scale_in_min_score"])
    if score < min_score:
        result["reason"] = f"score too weak ({score:.0f} < {min_score:.0f})"
        return result

    # Soft name cap: need some room left
    try:
        pv = float(portfolio_value or 0.0)
        already = float(existing_name_value or 0.0)
        name_pct = float((settings or {}).get("max_single_name_equity_pct") or 0) if settings else 0.0
        if name_pct <= 0:
            name_pct = float(get_risk_posture_profile(posture).get("max_single_name_equity_pct", 15.0))
        if name_pct > 1.0:
            name_pct = name_pct / 100.0
        if pv > 0 and already > 0 and already >= pv * name_pct:
            result["reason"] = "single-name soft cap full"
            return result
    except (TypeError, ValueError):
        pass

    result["allowed"] = True
    result["reason"] = detail
    return result


def scale_in_allowed(ticker, current_price, avg_cost, **kwargs):
    """Convenience bool wrapper around evaluate_scale_in."""
    ev = evaluate_scale_in(ticker, current_price, avg_cost, **kwargs)
    return bool(ev.get("allowed")), str(ev.get("reason") or ""), ev


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


def buy_rank_score_for_book(ticker, is_crypto=True, held_tickers=None, holdings_meta=None,
                            portfolio_value=0.0, scale_in_candidate=False,
                            crypto_only_broker=False):
    """Signal quality + soft portfolio-fit adjustment for this book."""
    base = buy_rank_score(ticker, is_crypto=is_crypto)
    adj = portfolio_buy_rank_adjust(
        ticker, held_tickers, holdings_meta=holdings_meta,
        portfolio_value=portfolio_value, is_crypto=is_crypto,
        scale_in_candidate=scale_in_candidate,
        crypto_only_broker=crypto_only_broker,
    )
    return base + adj


# =========================================================================
# PRIMARY EVALUATION ENGINES
# =========================================================================

def evaluate_holding(ticker, avg_cost, broker_id="ROBINHOOD", asset_type="", live_price=None,
                     exit_roi_scale=1.0, exit_time_scale=1.0, ttp_arm_scale=1.0):
    """
    Trailing take-profit / hard stop / time-stop.
    Fee thresholds change by broker so CB doesn't take thin RH-style exits.
    Optional posture scales adjust time-exit / TTP-arm patience (hard stops unchanged).
    """
    broker_id = _normalize_broker_id(broker_id)
    current_price = float(live_price) if live_price and live_price > 0 else fetch_current_price(ticker)
    if current_price <= 0: return "HOLD (Awaiting Price)"

    # Coinbase (and some RH crypto) often has no avg cost — seed at live price so
    # TTP/time-stop can still manage the position from "now" instead of never selling.
    if avg_cost <= 0:
        avg_cost = current_price

    fees = apply_exit_posture(
        _resolve_fee_profile(broker_id, ticker, asset_type),
        exit_roi_scale=exit_roi_scale,
        exit_time_scale=exit_time_scale,
        ttp_arm_scale=ttp_arm_scale,
    )
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
        mem = _portfolio_memory[broker_id][ticker]
        mem["exit_reason"] = "hard_stop"
        if not mem.get("loss_recorded"):
            _apply_cooldown(
                broker_id, ticker,
                sell_price=current_price,
                reason="hard_stop",
                record_streak=True,
            )
            mem["loss_recorded"] = True
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


def evaluate_crypto_opportunity(ticker, broker_id="ROBINHOOD", live_price=None, posture="balanced"):
    broker_id = _normalize_broker_id(broker_id)
    current_price = float(live_price) if live_price and live_price > 0 else fetch_current_price(ticker)
    if current_price <= 0: return "DO NOT BUY (Awaiting Price)"

    # Safer requires broad BTC risk-on confirmation. Balanced/Aggressive deliberately
    # bypass only this broad gate; all ticker-specific entry gates below still apply.
    if crypto_regime_required(posture):
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
    broker_id = _normalize_broker_id(broker_id)
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
