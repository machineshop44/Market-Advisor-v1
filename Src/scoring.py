import os
import json
import time
import pandas as pd
import yfinance as yf
from datetime import datetime

# Fallback wrapper to hook into the main GUI's price feed
try:
    from market_data import fetch_current_price
except ImportError:
    def fetch_current_price(ticker): return 1.0

# =========================================================================
# BROKER FEE PROFILES
# Round-trip friction is baked into exit thresholds so a "win" is a real win.
# RH stocks ≈ $0 commission. RH crypto / CB Advanced take a real bite.
# =========================================================================
FEE_PROFILES = {
    "ROBINHOOD_STOCK": {
        "ttp_arm": 0.020,          # +2.0% arms trail (commission-free)
        "ttp_trail": 0.008,        # -0.8% trail
        "hard_stop": -0.035,       # -3.5%
        "time_30m_target": 0.012,  # +1.2%
        "time_60m_target": 0.010,  # +1.0%
        "stale_roi": -0.01,
    },
    "ROBINHOOD_CRYPTO": {
        "ttp_arm": 0.028,          # RH crypto spread/fees usually < CB Advanced
        "ttp_trail": 0.010,
        "hard_stop": -0.040,
        "time_30m_target": 0.018,
        "time_60m_target": 0.014,
        "stale_roi": -0.01,
    },
    "COINBASE": {
        # Tuned for ~0.6% each side ≈ ~1.2% round-trip Advanced retail take-rate
        "ttp_arm": 0.035,          # +3.5% arms trail → exit still clears fees
        "ttp_trail": 0.010,        # -1.0% trail → min exit ≈ +2.5% gross
        "hard_stop": -0.040,
        "time_30m_target": 0.020,  # +2.0%
        "time_60m_target": 0.015,  # +1.5%
        "stale_roi": -0.01,
    },
}

CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}

CRYPTO_COOLDOWN = 10 * 60   # 10 minutes after selling crypto
STOCK_COOLDOWN = 20 * 60    # 20 minutes after selling stocks

RSI_PERIOD = 14
RSI_CEILING = 60            # Over 60 = overbought, do not chase

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
_trend_cache = {}
_TREND_CACHE_TTL = 45  # seconds


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


def _get_trend_data(ticker, interval="5m", period="5d"):
    """Shared Yahoo chart math for BOTH brokers: (bullish, uptrend, rsi, has_volume)."""
    cache_key = (str(ticker).upper(), interval, period)
    cached = _trend_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _TREND_CACHE_TTL:
        return cached[1]

    result = (False, False, None, False)
    try:
        df = yf.Ticker(_safe_ticker(ticker)).history(period=period, interval=interval)
        if df.empty or len(df) < 20:
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
        is_bullish = (macd > sig) and (macd > 0)

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


def save_state():
    """Persist TTP/cooldown memory so restarts don't wipe trade state."""
    try:
        payload = {
            "portfolio": _portfolio_memory,
            "cooldown": _cooldown_memory,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"scoring save_state error: {e}")


def load_state():
    """Restore TTP/cooldown memory from disk."""
    global _portfolio_memory, _cooldown_memory
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        port = data.get("portfolio") or {}
        cool = data.get("cooldown") or {}
        for bid in ("ROBINHOOD", "COINBASE"):
            _portfolio_memory[bid] = port.get(bid, {})
            _cooldown_memory[bid] = cool.get(bid, {})
        return True
    except Exception as e:
        print(f"scoring load_state error: {e}")
        return False


def market_regime_ok(is_crypto=False):
    """
    Hard gate: skip NEW buys when the broad market (BTC or SPY) is in a 1H downtrend.
    Returns (ok: bool, reason: str).
    """
    proxy = "BTC-USD" if is_crypto else "SPY"
    _, uptrend, _, _ = _get_trend_data(proxy, interval="60m", period="5d" if is_crypto else "1mo")
    if not uptrend:
        return False, f"DO NOT BUY (Regime: {proxy} 1H Downtrend)"
    return True, ""


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
        save_state()
        return f"SELL (Hard Stop: {roi*100:.2f}%)"

    peak_roi = (highest - avg_cost) / avg_cost
    if peak_roi >= fees["ttp_arm"]:
        trail_trigger_price = highest * (1.0 - fees["ttp_trail"])
        if current_price <= trail_trigger_price:
            save_state()
            return f"SELL (TTP Triggered - Peak: +{peak_roi*100:.2f}%, Exit: +{roi*100:.2f}%)"
        save_state()
        return f"HOLD (TTP Armed - Peak: +{peak_roi*100:.2f}%)"

    if held_time_minutes >= 120 and roi < fees["stale_roi"]:
        save_state()
        return f"SELL (Stale > 2h, ROI: {roi*100:.2f}%)"
    elif held_time_minutes >= 60 and roi >= fees["time_60m_target"]:
        save_state()
        return f"SELL (Time-Stop > 1h, +{fees['time_60m_target']*100:.1f}% Target Hit)"
    elif held_time_minutes >= 30 and roi >= fees["time_30m_target"]:
        save_state()
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
