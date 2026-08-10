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
# Profit-taking rails (TTP arm / time exits) must clear estimated RT fees
# + MIN_PROFIT_OVER_FEES_PCT — see enforce_min_profit_over_fees().
# Hard stops / stale (red) exits are deliberately exempt.
# Discipline: cut losers / bank winners same-session — no multi-day hope.
# =========================================================================
# Minimum net edge over estimated round-trip fees for discretionary profit exits.
MIN_PROFIT_OVER_FEES_PCT = 0.01  # 100 bps (1.0%)
# FinRL lesson (rules, not RL): NEW discretionary entries also clear RT + edge.
MIN_ENTRY_EDGE_OVER_FEES_PCT = 0.01  # 100 bps beyond RT before spraying tickets

# Flat time banks must clear TTP arm by this multiple so they cannot fire before trail arms.
FLAT_TIME_BANK_ARM_MULT = 1.25

FEE_PROFILES = {
    # Desk posture: ride uptrends — primary green exit is TTP trail-from-peak (turn-based).
    # Hierarchy: hard stop → TTP arm+trail → rare flat time banks (Aggressive only, after turn) → stale.
    # Do NOT jump out of a winner for a quick buck while price is still near the local high.
    # ATR may widen arm/trail/time rails together (see atr_adapt_exit_fees).
    "ROBINHOOD_STOCK": {
        # Est. RT ~0.20% → floor fee_rt+1% = +1.2% (rails sit above floor)
        "ttp_arm": 0.020,          # +2.0% arms trail — let winners develop
        "ttp_trail": 0.010,        # -1.0% from peak
        "hard_stop": -0.035,       # -3.5% — clear disaster cut, not noise
        "time_30m_target": 0.040,  # +4.0% rare flat bank (Aggressive + trend turned)
        "time_60m_target": 0.035,  # +3.5%
        "time_green_min": 90,      # minutes — patience before any flat green escape
        "time_green_roi": 0.030,   # +3.0% — well above arm; no ~1–2% jump-ship
        "stale_minutes": 180,      # 3h same-session (2h/−1% was twitchy on META/TSLA)
        "stale_roi": -0.015,       # -1.5%
    },
    "ROBINHOOD_CRYPTO": {
        # Est. one-way 0.95% (MM spread rebate ≈ exchange tier <$50K) → RT 1.90% → floor +2.90%
        # Slightly more patient than stocks (fees) but still rides uptrends via TTP.
        "ttp_arm": 0.035,          # +3.5% — trail primary over flat fee-floor TP
        "ttp_trail": 0.012,        # -1.2% from peak
        "hard_stop": -0.040,       # -4.0% — crypto vol needs a wider disaster line
        "time_30m_target": 0.055,  # +5.5%
        "time_60m_target": 0.050,  # +5.0%
        "time_green_min": 75,      # more patient than equities on flat green escape
        "time_green_roi": 0.045,   # +4.5% — no thin crypto "wins"
        "stale_minutes": 120,      # crypto stays tighter than equities on dead money
        "stale_roi": -0.012,       # -1.2%
    },
    "COINBASE": {
        # Intro 1: maker 0.60% / taker 1.20% — MA market exits use taker.
        # Taker 1.2% one-way → 2.4% RT → floor = +3.4%.
        # Higher tiers (Intro 2 / Advanced) can lower _FEE_ONE_WAY_PCT if volume rises.
        "ttp_arm": 0.040,          # +4.0% arms trail
        "ttp_trail": 0.012,        # -1.2% trail
        "hard_stop": -0.040,
        "time_30m_target": 0.060,  # +6.0%
        "time_60m_target": 0.055,  # +5.5%
        "time_green_min": 75,
        "time_green_roi": 0.050,   # +5.0% — never take a CB "win" that fees wipe out
        "stale_minutes": 120,
        "stale_roi": -0.012,
    },
    # E*TRADE STOCK/ETF (Andrew official schedule): $0 commission on stocks / options MF / ETFs.
    # Est. 0.10% one-way (SEC/TAF + spread friction) → 0.20% RT → floor fee_rt+1% = +1.2%.
    # Honest overestimate vs listed $0 — keeps discretionary exits ≥ ~1%+friction; no penny-takes.
    # Schedule also lists crypto 0.50% one-way — UNUSED here (no ETRADE_CRYPTO profile):
    # Market Advisor never places ET crypto (supports_crypto=False; equity orders only).
    # (Own profile so ET can diverge from RH later. OTC $6.95 / options $0.65 not modeled.)
    "ETRADE_STOCK": {
        "ttp_arm": 0.020,          # +2.0% arms trail (match RH stock desk rails)
        "ttp_trail": 0.010,        # -1.0% trail
        "hard_stop": -0.035,       # -3.5%
        "time_30m_target": 0.040,  # +4.0%
        "time_60m_target": 0.035,  # +3.5%
        "time_green_min": 90,
        "time_green_roi": 0.030,   # +3.0%
        "stale_minutes": 180,
        "stale_roi": -0.015,
    },
}

CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}

# Hold bias / less churn (FinRL: prefer no-trade; crypto OW fees ~0.95–1.2%)
CRYPTO_COOLDOWN = 20 * 60   # 20 minutes after selling crypto (was 10m)
STOCK_COOLDOWN = 20 * 60    # 20 minutes after selling stocks
CRYPTO_TRADE_LOCK_SEC = 600  # post-fill lock — longer for crypto
STOCK_TRADE_LOCK_SEC = 300   # equities stay at 5m
HARD_STOP_COOLDOWN_MULT = 2.0  # hard-stop exits get a longer per-ticker lockout
LOSS_STREAK_WINDOW_SEC = 90 * 60
LOSS_STREAK_TRIGGER = 3        # hard stops in window → broker-wide new-buy pause
LOSS_STREAK_PAUSE_SEC = 45 * 60

# Crypto entry bar: don't spray $5 tickets when edge ≪ ~2% RT
CRYPTO_MIN_SCORE_FOR_ENTRY = 55.0
CRYPTO_THIN_MIN_SCORE = 70.0       # near broker floor needs stronger conviction
CRYPTO_THIN_TICKET_MULT = 1.5      # ≤ 1.5× broker min = "thin"
# BTC 1H ATR% above this → pause NEW crypto buys (all postures; no liquidate)
CRYPTO_TURBULENCE_ATR_PCT = 0.025  # 2.5% — elevated chop, not a crash liquidate
SCALE_IN_REPEAT_COOLDOWN_SEC = 45 * 60  # mute repeat scale-in noise on same name

RSI_PERIOD = 14
RSI_CEILING = 70            # 70 is standard; 60 was blocking too many real trends
ATR_PERIOD = 14
ATR_SIZING_MULT = 1.5       # risk distance = max(fee hard-stop, ATR% * mult)
ATR_SIZING_CAP_MULT = 2.0   # never widen stop beyond 2× fee hard-stop (sizing + exits)
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

# Pro risk stack — risk $ per trade is posture/settings tunable (see RISK_POSTURE_PROFILES)
RISK_PCT_PER_TRADE = 0.0075       # 0.75% of equity risked per trade (Balanced default)
DEFAULT_MAX_OPEN_RISK_PCT = 0.06  # soft book heat: sum of stop-risk $ ≤ 6% equity
CASH_RESERVE_PCT = 0.12           # leave 12% buying power undeployed (overridden by target_bp_utilization)
DEFAULT_TARGET_BP_UTILIZATION = 0.88  # deploy most usable BP; idle cash does not earn
DEFAULT_SIZING_FOCUS_SLOTS = 6    # size as if filling next N tickets — not all max_open slots
MAX_CRYPTO_BOOK_FRAC = 0.30       # max crypto share on multi-asset brokers (RH); ~$34 on a $115 book
# Soft prefer-equity during RTH (multi-asset only): boost stocks / penalize more crypto
RTH_EQUITY_RANK_BOOST = 12.0
RTH_CRYPTO_RANK_PENALTY = 10.0
SMALL_BOOK_EQUITY = 500.0         # below this, crypto soft penalties start earlier
MAX_CLUSTER_POSITIONS = 2         # max open names in one correlation cluster
MAX_SINGLE_NAME_EQUITY_FRAC = 0.15  # soft cap: one name ≤ ~15% of equity
# Fill-quality feedback (conservative; throttled)
FILL_FEEDBACK_WINDOW = 20         # look at last N fills with slippage
FILL_FEEDBACK_ADVERSE_MIN = 5     # adverse count before adjusting
FILL_FEEDBACK_ADVERSE_BPS = 5.0   # slippage_bps > this counts adverse
FILL_FEEDBACK_COOLDOWN_SEC = 1800  # don't thrash — 30 min between adjustments
FILL_FEEDBACK_OFFSET_BUMP = 0.05  # +0.05% limit offset when adverse
FILL_FEEDBACK_SIZE_MULT = 0.90    # shrink size 10% after adverse cluster
FILL_FEEDBACK_MAX_OFFSET_BUMP = 0.15
_fill_feedback_state = {
    "recent_slip_bps": [],        # newest last
    "last_adjust_ts": 0.0,
    "offset_bump_pct": 0.0,       # added to settings limit_offset_pct
    "size_mult": 1.0,
    "last_note": "",
}
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
# Scale-in ROI bands sit ABOVE the ~−3.5% stock hard stop (stop_floor ≈ −3.0%).
RISK_POSTURE_PROFILES = {
    "safer": {
        "label": "Safer",
        "hint": (
            "Diversified book, 25% cash buffer, rides winners via TTP trail "
            "(no flat early green takes), no averaging-down, "
            "no opportunity-swap — BTC uptrend + turbulence pause for crypto. Day DD pause ~3%."
        ),
        "require_crypto_regime": True,
        "target_bp_utilization_pct": 75.0,
        "sizing_focus_slots": 8,
        "max_open_positions": 10,
        "max_buys_per_cycle": 1,
        "risk_pct_per_trade": 0.50,       # % of equity risked to hard stop per new ticket
        "max_open_risk_pct": 4.0,         # book heat soft cap (% equity)
        "max_single_name_equity_pct": 10.0,
        "conviction_alloc_mult_max": 1.25,
        "exit_roi_scale": 1.0,
        "exit_time_scale": 1.15,  # slightly more patient green wait
        "ttp_arm_scale": 1.0,
        "allow_flat_time_banks": False,  # TTP trail only for green exits
        "allow_scale_in": False,
        "scale_in_size_frac": 0.40,
        "scale_in_max_adds": 1,
        "scale_in_roi_min": -0.022,  # shallow adds only if user enables
        "scale_in_roi_max": -0.012,
        "scale_in_near_pct": 0.012,
        "scale_in_min_score": 55.0,
        "day_dd_pause_pct": 0.03,
        "peak_dd_pause_pct": 0.08,
        "dd_pause_minutes": 60,
        "daily_loss_limit_equity_pct": 0.03,  # seeds $ daily_loss_limit from equity
    },
    "balanced": {
        "label": "Balanced",
        "hint": (
            "Focus 6 slots, ~88% util, scale-in near support, opportunity-swap on with "
            "fee gates — rides winners via TTP trail (no flat early green takes). "
            "BTC regime + turbulence pause for new crypto. Day DD pause ~5%."
        ),
        "require_crypto_regime": True,  # extended Safer BTC gate for live Balanced
        "target_bp_utilization_pct": 88.0,
        "sizing_focus_slots": 6,
        "max_open_positions": 8,
        "max_buys_per_cycle": 1,
        "risk_pct_per_trade": 0.75,
        "max_open_risk_pct": 6.0,
        "max_single_name_equity_pct": 15.0,
        "conviction_alloc_mult_max": 1.50,
        "exit_roi_scale": 1.0,
        "exit_time_scale": 1.0,
        "ttp_arm_scale": 1.0,
        "allow_flat_time_banks": False,  # TTP trail only for green exits
        "allow_scale_in": True,
        "scale_in_size_frac": 0.50,
        "scale_in_max_adds": 1,
        "scale_in_roi_min": -0.025,  # −2.5% … −1.0% underwater
        "scale_in_roi_max": -0.010,
        "scale_in_near_pct": 0.015,
        "scale_in_min_score": 48.0,
        "day_dd_pause_pct": 0.05,
        "peak_dd_pause_pct": 0.12,
        "dd_pause_minutes": 45,
        "daily_loss_limit_equity_pct": 0.05,
    },
    "aggressive": {
        "label": "Aggressive",
        "hint": (
            "Concentrated (≤20% name), high util, patient TTP exits, rare flat banks only "
            "after a local turn, deeper scale-in & rotates — still hard-stopped; "
            "BTC turbulence pauses new crypto (no full regime gate). Day DD pause ~8%."
        ),
        "require_crypto_regime": False,  # coin trend + turbulence only
        "target_bp_utilization_pct": 95.0,
        "sizing_focus_slots": 3,
        "max_open_positions": 5,
        "max_buys_per_cycle": 1,
        "risk_pct_per_trade": 1.00,
        "max_open_risk_pct": 10.0,
        "max_single_name_equity_pct": 20.0,  # soft-capped vs prior 25%
        "conviction_alloc_mult_max": 1.75,
        "exit_roi_scale": 1.35,   # need more gain before rare time-exit
        "exit_time_scale": 1.30,  # wait longer before time-green
        "ttp_arm_scale": 1.25,    # arm trail later — let winners run
        "allow_flat_time_banks": True,  # high-bar escape only after local turn
        "allow_scale_in": True,
        "scale_in_size_frac": 0.60,
        "scale_in_max_adds": 1,
        "scale_in_roi_min": -0.029,  # near stop_floor; wider than balanced
        "scale_in_roi_max": -0.005,
        "scale_in_near_pct": 0.020,
        "scale_in_min_score": 40.0,
        "day_dd_pause_pct": 0.08,
        "peak_dd_pause_pct": 0.15,
        "dd_pause_minutes": 30,
        "daily_loss_limit_equity_pct": 0.08,
    },
}


def normalize_risk_posture(name):
    key = str(name or "balanced").strip().lower()
    return key if key in RISK_POSTURE_PROFILES else "balanced"


def get_risk_posture_profile(name=None):
    """Return a copy of the posture profile dict (defaults to balanced)."""
    return dict(RISK_POSTURE_PROFILES[normalize_risk_posture(name)])


def crypto_regime_required(posture=None):
    """Safer + Balanced require the broad BTC regime gate for crypto entries."""
    return bool(get_risk_posture_profile(posture).get("require_crypto_regime", False))


def crypto_turbulence_ok():
    """
    FinRL-style turbulence pause: block NEW crypto buys when BTC 1H ATR% is elevated.
    Does NOT liquidate holdings — pause entries only. Fail-open when ATR unavailable
    (regime / score gates still apply).
    Returns (ok: bool, reason: str).
    """
    atr = _atr_pct("BTC")
    if atr is None or atr <= 0:
        return True, ""
    if float(atr) >= float(CRYPTO_TURBULENCE_ATR_PCT):
        return False, (
            f"DO NOT BUY (Turbulence: BTC ATR {float(atr)*100:.1f}% elevated — "
            f"pause new crypto)"
        )
    return True, ""


def entry_regime_ok(is_crypto=False, posture=None, *, allow_when_blocked=False):
    """
    Hard gate for NEW entries (scan + execute). Matches evaluate_* posture rules:
      - Equities: always require SPY regime unless allow_when_blocked
      - Crypto: BTC turbulence pause (all postures); BTC regime when
        posture.require_crypto_regime (Safer + Balanced)
    Returns (ok: bool, reason: str). Override setting defaults OFF for live.
    """
    if allow_when_blocked:
        return True, "override:allow_buys_when_regime_blocked"
    if is_crypto:
        tok, tw = crypto_turbulence_ok()
        if not tok:
            return False, tw
        if not crypto_regime_required(posture):
            return True, ""
    return market_regime_ok(is_crypto=bool(is_crypto))


def trade_lock_seconds(is_crypto=False):
    """Post-fill lock duration — crypto longer to cut OW-fee churn."""
    return int(CRYPTO_TRADE_LOCK_SEC if is_crypto else STOCK_TRADE_LOCK_SEC)


def min_entry_edge_pct(broker_id, ticker=None, asset_type=""):
    """Minimum expected edge for NEW discretionary buys/rotates: RT fees + buffer."""
    return float(estimate_round_trip_fee_pct(broker_id, ticker, asset_type)) + float(
        MIN_ENTRY_EDGE_OVER_FEES_PCT
    )


def net_roi_after_fees(gross_roi, broker_id, ticker=None, asset_type=""):
    """
    Net ROI fraction after estimated round-trip fees (FinRL reward = Δ equity − costs).
    Returns None when gross_roi is unknown.
    """
    if gross_roi is None:
        return None
    try:
        g = float(gross_roi)
    except (TypeError, ValueError):
        return None
    return g - float(estimate_round_trip_fee_pct(broker_id, ticker, asset_type))


def estimated_signal_edge_pct(score, *, is_crypto=False):
    """
    Map buy_rank score → rough expected edge for fee gates.
    Conservative: crypto needs more score points to clear ~2% RT.
    """
    try:
        sc = float(score or 0.0)
    except (TypeError, ValueError):
        sc = 0.0
    base = 40.0
    per_pt = 0.0010 if is_crypto else 0.0005
    return max(0.0, (sc - base) * per_pt)


def crypto_new_entry_ok(broker_id, ticker, score=0.0, notional=None, *, skip_turbulence=False):
    """
    FinRL hold-bias for NEW crypto buys (not scale-in / not protective).
    Raises the bar vs hold; blocks thin $5 tickets when edge ≪ RT+edge.
    Returns (ok: bool, reason: str).
    """
    if not skip_turbulence:
        tok, tw = crypto_turbulence_ok()
        if not tok:
            return False, tw
    try:
        sc = float(score or 0.0)
    except (TypeError, ValueError):
        sc = 0.0
    if sc < float(CRYPTO_MIN_SCORE_FOR_ENTRY):
        return False, (
            f"DO NOT BUY (Hold bias: score {sc:.0f} < "
            f"{CRYPTO_MIN_SCORE_FOR_ENTRY:.0f})"
        )
    need = min_entry_edge_pct(broker_id, ticker, "cryptocurrency")
    edge = estimated_signal_edge_pct(sc, is_crypto=True)
    floor = broker_min_notional(broker_id, is_crypto=True)
    try:
        notion = float(notional) if notional is not None else None
    except (TypeError, ValueError):
        notion = None
    if notion is not None and notion > 0 and notion <= floor * float(CRYPTO_THIN_TICKET_MULT):
        if sc < float(CRYPTO_THIN_MIN_SCORE):
            return False, (
                f"DO NOT BUY (Thin ${notion:.2f} ticket: score {sc:.0f} < "
                f"{CRYPTO_THIN_MIN_SCORE:.0f} — edge ≪ ~{need*100:.1f}% RT+edge)"
            )
        if edge + 1e-12 < need:
            return False, (
                f"DO NOT BUY (Thin ticket: est edge {edge*100:.2f}% < "
                f"need {need*100:.2f}% RT+edge)"
            )
    return True, ""


def get_scale_in_params(posture=None, settings=None):
    """
    Merge Risk Posture scale-in defaults with optional settings overrides.
    settings.allow_scale_in (bool) wins when explicitly set; posture supplies ROI bands
    unless advanced_scale_in_override is True (Advanced dialog committed custom bands).
    """
    prof = get_risk_posture_profile(posture)
    s = settings or {}
    out = {
        "allow_scale_in": bool(prof.get("allow_scale_in", False)),
        "scale_in_size_frac": float(prof.get("scale_in_size_frac", 0.50)),
        "scale_in_max_adds": int(prof.get("scale_in_max_adds", 1)),
        "scale_in_roi_min": float(prof.get("scale_in_roi_min", -0.025)),
        "scale_in_roi_max": float(prof.get("scale_in_roi_max", -0.010)),
        "scale_in_near_pct": float(prof.get("scale_in_near_pct", 0.015)),
        "scale_in_min_score": float(prof.get("scale_in_min_score", 48.0)),
    }
    if "allow_scale_in" in s and s.get("allow_scale_in") is not None:
        out["allow_scale_in"] = bool(s.get("allow_scale_in"))
    if s.get("advanced_scale_in_override"):
        for key in (
            "scale_in_size_frac",
            "scale_in_max_adds",
            "scale_in_roi_min",
            "scale_in_roi_max",
            "scale_in_near_pct",
            "scale_in_min_score",
        ):
            if key in s and s.get(key) is not None:
                try:
                    out[key] = type(out[key])(s.get(key))
                except (TypeError, ValueError):
                    pass
    else:
        # Soft overrides for size / max adds only (common Advanced tweaks)
        if "scale_in_size_frac" in s and s.get("scale_in_size_frac") is not None:
            try:
                out["scale_in_size_frac"] = float(s.get("scale_in_size_frac"))
            except (TypeError, ValueError):
                pass
        if "scale_in_max_adds" in s and s.get("scale_in_max_adds") is not None:
            try:
                out["scale_in_max_adds"] = int(s.get("scale_in_max_adds"))
            except (TypeError, ValueError):
                pass
    out["scale_in_size_frac"] = max(0.25, min(0.75, float(out["scale_in_size_frac"])))
    out["scale_in_max_adds"] = max(0, min(3, int(out["scale_in_max_adds"])))
    out["scale_in_near_pct"] = min(0.05, max(0.005, float(out["scale_in_near_pct"])))
    if out["scale_in_roi_min"] > out["scale_in_roi_max"]:
        out["scale_in_roi_min"], out["scale_in_roi_max"] = (
            out["scale_in_roi_max"], out["scale_in_roi_min"]
        )
    return out


def apply_exit_posture(fees, exit_roi_scale=1.0, exit_time_scale=1.0, ttp_arm_scale=1.0):
    """
    Scale time-based take-profit / TTP-arm thresholds on a fee-profile copy.
    Hard stops and cluster rails are not relaxed here (ATR may widen them separately).
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


def atr_adapt_exit_fees(fees, ticker=None):
    """
    Widen hard_stop (and trail / arm / time-profit rails) toward ATR%, capped at 2× fee base.

    Time-profit targets scale with the same factor as ttp_arm so ATR volatility does
    not push the arm above flat +1–2% time banks (that caused ~fee-floor jump-ships).
    Returns a copy; no-op when ticker missing or ATR unavailable.
    """
    out = dict(fees or {})
    clean = str(ticker or "").replace("-USD", "").upper()
    if not clean:
        return out
    try:
        base_hs = abs(float(out.get("hard_stop") or -0.035))
    except (TypeError, ValueError):
        base_hs = 0.035
    if base_hs <= 0:
        return out
    atr = _atr_pct(clean)
    if atr is None or atr <= 0:
        return out
    widened = max(base_hs, float(atr) * ATR_SIZING_MULT)
    widened = min(widened, base_hs * ATR_SIZING_CAP_MULT)
    scale = widened / base_hs if base_hs > 0 else 1.0
    out["hard_stop"] = -widened
    for key in (
        "ttp_trail",
        "ttp_arm",
        "time_30m_target",
        "time_60m_target",
        "time_green_roi",
    ):
        if key in out and out[key] is not None:
            try:
                out[key] = float(out[key]) * scale
            except (TypeError, ValueError):
                pass
    return out


def min_profit_exit_roi_pct(broker_id, ticker=None, asset_type=""):
    """
    Minimum ROI for discretionary profit-taking / TTP arm / time-green sells:
    estimated round-trip fees + MIN_PROFIT_OVER_FEES_PCT.
    """
    return float(estimate_round_trip_fee_pct(broker_id, ticker, asset_type)) + float(
        MIN_PROFIT_OVER_FEES_PCT
    )


def enforce_min_profit_over_fees(fees, broker_id, ticker=None, asset_type=""):
    """
    Clamp profit-taking rails to ≥ fee_rt + MIN_PROFIT_OVER_FEES_PCT.
    Does not touch hard_stop, stale_roi, or ttp_trail (trail lock-in / protective exits).
    """
    out = dict(fees or {})
    floor = min_profit_exit_roi_pct(broker_id, ticker, asset_type)
    for key in ("time_green_roi", "time_30m_target", "time_60m_target", "ttp_arm"):
        if key in out and out[key] is not None:
            try:
                out[key] = max(float(out[key]), floor)
            except (TypeError, ValueError):
                pass
    return out


def ensure_flat_banks_above_ttp_arm(fees):
    """
    Keep flat time-bank ROI targets strictly above TTP arm so ~fee-floor / posture
    scaling cannot reintroduce jump-ships before the trail is allowed to arm.
    """
    out = dict(fees or {})
    try:
        arm = float(out.get("ttp_arm") or 0.0)
    except (TypeError, ValueError):
        arm = 0.0
    if arm <= 0:
        return out
    floor = arm * float(FLAT_TIME_BANK_ARM_MULT)
    for key in ("time_green_roi", "time_30m_target", "time_60m_target"):
        if key in out and out[key] is not None:
            try:
                out[key] = max(float(out[key]), floor)
            except (TypeError, ValueError):
                pass
    return out


def _still_riding_local_uptrend(current_price, highest, fees, ticker=None):
    """
    True while the local move has not turned — suppress flat green banks.

    Near position high (within TTP trail) counts as still riding. Slightly further
    off the high still rides if short-term price > EMA20 (when chart data available).
    """
    try:
        px = float(current_price or 0.0)
        hi = float(highest or 0.0)
        trail = float((fees or {}).get("ttp_trail") or 0.01)
    except (TypeError, ValueError):
        return False
    if px <= 0 or hi <= 0:
        return False
    trail = max(0.005, trail)
    # Within trail of local high — turn not confirmed; ride it
    if px >= hi * (1.0 - trail):
        return True
    # Soft band: EMA still up → do not flat-bank a climbing name
    soft = hi * (1.0 - trail * 1.75)
    if px >= soft and ticker:
        try:
            _, uptrend, _, _ = _get_trend_data(ticker, interval="5m", period="1d")
            if uptrend:
                return True
        except Exception:
            pass
    return False


def resolve_exit_fees(
    broker_id,
    ticker=None,
    asset_type="",
    exit_roi_scale=1.0,
    exit_time_scale=1.0,
    ttp_arm_scale=1.0,
):
    """Fee profile → ATR exit adapt → posture scales → fee floor → time banks above arm."""
    fees = atr_adapt_exit_fees(
        _resolve_fee_profile(broker_id, ticker, asset_type),
        ticker,
    )
    fees = apply_exit_posture(
        fees,
        exit_roi_scale=exit_roi_scale,
        exit_time_scale=exit_time_scale,
        ttp_arm_scale=ttp_arm_scale,
    )
    fees = enforce_min_profit_over_fees(fees, broker_id, ticker, asset_type)
    return ensure_flat_banks_above_ttp_arm(fees)


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
_scale_in_last_ts = {b: {} for b in _KNOWN_BROKER_IDS}  # ticker -> last scale-in attempt/fill ts
# broker -> {"events": [ts, ...], "pause_until": float}
_loss_streak = {b: {"events": [], "pause_until": 0.0} for b in _KNOWN_BROKER_IDS}
# Per-broker equity high-water + day open for drawdown pauses
_equity_dd = {
    b: {"day": "", "day_open": 0.0, "peak": 0.0, "pause_until": 0.0, "pause_reason": ""}
    for b in _KNOWN_BROKER_IDS
}
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
        if broker_id in _scale_in_last_ts:
            _scale_in_last_ts[broker_id].pop(t, None)


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


def _local_day_key():
    return datetime.now().strftime("%Y-%m-%d")


def update_equity_drawdown(broker_id, equity, posture=None, settings=None):
    """
    Track day-open + peak equity; pause new buys when day or peak drawdown
    exceeds posture thresholds. Returns (paused_now: bool, message: str).
    """
    broker_id = _normalize_broker_id(broker_id)
    try:
        eq = float(equity or 0.0)
    except (TypeError, ValueError):
        eq = 0.0
    if eq <= 0:
        return False, ""

    prof = get_risk_posture_profile(posture)
    s = settings or {}
    day_pct = float(s.get("day_dd_pause_pct", prof.get("day_dd_pause_pct", 0.05)) or 0.05)
    peak_pct = float(s.get("peak_dd_pause_pct", prof.get("peak_dd_pause_pct", 0.12)) or 0.12)
    pause_min = int(s.get("dd_pause_minutes", prof.get("dd_pause_minutes", 45)) or 45)
    day_pct = max(0.01, min(0.25, day_pct))
    peak_pct = max(0.02, min(0.40, peak_pct))
    pause_min = max(10, min(240, pause_min))

    if broker_id not in _equity_dd:
        _equity_dd[broker_id] = {
            "day": "", "day_open": 0.0, "peak": 0.0, "pause_until": 0.0, "pause_reason": ""
        }
    st = _equity_dd[broker_id]
    today = _local_day_key()
    if st.get("day") != today or float(st.get("day_open") or 0) <= 0:
        st["day"] = today
        st["day_open"] = eq
        st["peak"] = max(eq, float(st.get("peak") or 0.0))
        # New day clears prior pause unless still in the future from yesterday
        if float(st.get("pause_until") or 0) < time.time():
            st["pause_reason"] = ""
    else:
        st["peak"] = max(eq, float(st.get("peak") or 0.0))

    day_open = float(st["day_open"] or eq)
    peak = float(st["peak"] or eq)
    day_dd = (eq - day_open) / day_open if day_open > 0 else 0.0
    peak_dd = (eq - peak) / peak if peak > 0 else 0.0

    now = time.time()
    triggered = None
    if day_dd <= -day_pct:
        triggered = f"Day drawdown {day_dd*100:.1f}% ≤ −{day_pct*100:.0f}%"
    elif peak_dd <= -peak_pct:
        triggered = f"Peak drawdown {peak_dd*100:.1f}% ≤ −{peak_pct*100:.0f}%"

    if triggered and float(st.get("pause_until") or 0) < now:
        st["pause_until"] = now + pause_min * 60
        st["pause_reason"] = triggered
        save_state(force=True)
        return True, triggered

    save_state(force=False)
    return False, ""


def _drawdown_block(broker_id):
    """Return (allowed, reason) — False allowed means blocked."""
    broker_id = _normalize_broker_id(broker_id)
    st = _equity_dd.get(broker_id) or {}
    pause_until = float(st.get("pause_until") or 0.0)
    now = time.time()
    if pause_until > now:
        mins = int((pause_until - now) / 60) + 1
        why = st.get("pause_reason") or "Drawdown pause"
        return False, f"DO NOT BUY ({why}; {mins}m left)"
    return True, ""


def get_drawdown_status(broker_id):
    """Snapshot for UI / monitor."""
    broker_id = _normalize_broker_id(broker_id)
    st = dict(_equity_dd.get(broker_id) or {})
    now = time.time()
    st["paused"] = float(st.get("pause_until") or 0) > now
    return st


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
    if bid not in _scale_in_last_ts:
        _scale_in_last_ts[bid] = {}
    _scale_in_counts[bid][key] = get_scale_in_count(bid, key) + 1
    _scale_in_last_ts[bid][key] = time.time()
    save_state(force=True)
    return _scale_in_counts[bid][key]


def clear_scale_in_count(broker_id, ticker):
    bid = _normalize_broker_id(broker_id)
    key = str(ticker or "").replace("-USD", "").upper()
    if bid in _scale_in_counts:
        _scale_in_counts[bid].pop(key, None)
    if bid in _scale_in_last_ts:
        _scale_in_last_ts[bid].pop(key, None)


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

    allowed, reason = _drawdown_block(broker_id)
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
            "scale_in_last_ts": _scale_in_last_ts,
            "loss_streak": _loss_streak,
            "equity_dd": _equity_dd,
            "regime_last_good": _regime_last_good,
            "regime_broker_hourly": _broker_hourly_closes,
            "rotate_day_counts": _rotate_day_counts,
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
    global _scale_in_last_ts
    global _loss_streak, _regime_last_good, _broker_hourly_closes, _rotate_day_counts
    global _equity_dd
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        port = data.get("portfolio") or {}
        cool = data.get("cooldown") or {}
        prot = data.get("protective") or {}
        sic = data.get("scale_in_counts") or {}
        silt = data.get("scale_in_last_ts") or {}
        rdc = data.get("rotate_day_counts") or {}
        if isinstance(rdc, dict):
            _rotate_day_counts.clear()
            for k, v in rdc.items():
                try:
                    _rotate_day_counts[str(k)] = int(v)
                except (TypeError, ValueError):
                    pass
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
            raw_ts = silt.get(bid, {}) if isinstance(silt, dict) else {}
            _scale_in_last_ts[bid] = {}
            if isinstance(raw_ts, dict):
                for k, v in raw_ts.items():
                    try:
                        _scale_in_last_ts[bid][str(k).upper()] = float(v)
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
        edd = data.get("equity_dd") or {}
        if isinstance(edd, dict):
            for bid in _KNOWN_BROKER_IDS:
                row = edd.get(bid) or {}
                if not isinstance(row, dict):
                    continue
                try:
                    _equity_dd[bid] = {
                        "day": str(row.get("day") or ""),
                        "day_open": float(row.get("day_open") or 0.0),
                        "peak": float(row.get("peak") or 0.0),
                        "pause_until": float(row.get("pause_until") or 0.0),
                        "pause_reason": str(row.get("pause_reason") or ""),
                    }
                except (TypeError, ValueError):
                    pass
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
    When ticker is known, widen toward ATR (capped at 2× fee hard-stop) for both
    sizing and exits. ``for_sizing`` is kept for call-site clarity / backward compat.
    """
    fees = _resolve_fee_profile(broker_id, ticker, asset_type)
    base = abs(float(fees.get("hard_stop") or -0.035))
    if not ticker:
        return base
    # for_sizing unused for branching — exits and sizing share the same ATR widen rule
    _ = for_sizing
    atr = _atr_pct(ticker)
    if atr is None or atr <= 0:
        return base
    widened = max(base, float(atr) * ATR_SIZING_MULT)
    return min(widened, base * ATR_SIZING_CAP_MULT)


def get_trail_pct(broker_id, ticker=None, asset_type=""):
    fees = atr_adapt_exit_fees(_resolve_fee_profile(broker_id, ticker, asset_type), ticker)
    return float(fees.get("ttp_trail") or 0.008)


def portfolio_heat_snapshot(broker_rows, settings=None, posture=None):
    """
    Approx open risk $ / % to hard stop, BP headroom, DD pause, and session $-loss room.

    broker_rows: iterable of {
      broker, broker_id, equity, buying_power, day_pnl, armed,
      holdings: [{ticker, value, asset_type}, ...]
    }
    """
    settings = settings or {}
    posture = posture or settings.get("risk_posture", "balanced")
    prof = get_risk_posture_profile(posture)
    try:
        util_pct = float(
            settings.get("target_bp_utilization_pct", prof.get("target_bp_utilization_pct", 88.0))
            or 88.0
        )
    except (TypeError, ValueError):
        util_pct = 88.0
    util = util_pct / 100.0 if util_pct > 1.0 else util_pct
    util = max(0.50, min(0.99, util))
    try:
        loss_limit = float(settings.get("daily_loss_limit", 0.0) or 0.0)
    except (TypeError, ValueError):
        loss_limit = 0.0

    by_broker = {}
    combined = {
        "open_risk_dollars": 0.0,
        "open_risk_pct": 0.0,
        "equity": 0.0,
        "buying_power": 0.0,
        "bp_headroom": 0.0,
        "day_pnl": 0.0,
        "dd_paused": False,
        "dd_reason": "",
        "loss_disarmed": False,
        "loss_room": 0.0 if loss_limit > 0 else None,
        "session_risk_used_pct": 0.0,
        "armed_any": False,
    }
    now = time.time()

    for row in broker_rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("broker") or "")
        bid = _normalize_broker_id(row.get("broker_id") or name)
        try:
            eq = float(row.get("equity") or 0.0)
            bp = float(row.get("buying_power") or 0.0)
            pnl = float(row.get("day_pnl") or 0.0)
        except (TypeError, ValueError):
            eq, bp, pnl = 0.0, 0.0, 0.0
        armed = bool(row.get("armed"))
        dd = get_drawdown_status(bid)
        dd_paused = bool(dd.get("paused"))
        dd_reason = str(dd.get("pause_reason") or "")
        mins_left = 0
        if dd_paused:
            mins_left = int(max(0.0, float(dd.get("pause_until") or 0.0) - now) / 60.0) + 1

        risk_dollars = 0.0
        for h in row.get("holdings") or []:
            if not isinstance(h, dict):
                continue
            t = str(h.get("ticker") or "").replace("-USD", "").upper()
            if not t:
                continue
            try:
                val = float(h.get("value") or 0.0)
            except (TypeError, ValueError):
                val = 0.0
            if val <= 0:
                try:
                    px = float(h.get("price") or h.get("live_price") or 0.0)
                    qty = float(h.get("shares") or h.get("qty") or 0.0)
                    val = abs(px * qty)
                except (TypeError, ValueError):
                    val = 0.0
            if val <= 0:
                continue
            asset_type = h.get("asset_type") or ""
            stop_d = get_stop_distance_pct(bid, ticker=t, asset_type=asset_type)
            risk_dollars += val * float(stop_d)

        bp_headroom = max(0.0, bp * util)
        loss_room = max(0.0, loss_limit + pnl) if loss_limit > 0 else None
        # $-loss path disarms; DD only pauses buys
        loss_hit = bool(loss_limit > 0 and pnl <= -loss_limit)
        used_pct = 0.0
        if loss_limit > 0:
            used_pct = min(100.0, abs(min(0.0, pnl)) / loss_limit * 100.0)

        snap = {
            "open_risk_dollars": risk_dollars,
            "open_risk_pct": (risk_dollars / eq * 100.0) if eq > 0 else 0.0,
            "equity": eq,
            "buying_power": bp,
            "bp_headroom": bp_headroom,
            "bp_util_target_pct": util * 100.0,
            "day_pnl": pnl,
            "dd_paused": dd_paused,
            "dd_reason": dd_reason,
            "dd_mins_left": mins_left,
            "armed": armed,
            "loss_limit": loss_limit,
            "loss_room": loss_room,
            "loss_hit": loss_hit,
            "loss_disarmed": bool(loss_hit and not armed),
            "session_risk_used_pct": used_pct,
        }
        by_broker[name or bid] = snap
        combined["open_risk_dollars"] += risk_dollars
        combined["equity"] += eq
        combined["buying_power"] += bp
        combined["bp_headroom"] += bp_headroom
        combined["day_pnl"] += pnl
        if dd_paused:
            combined["dd_paused"] = True
            if not combined["dd_reason"]:
                combined["dd_reason"] = dd_reason
        if snap["loss_disarmed"]:
            combined["loss_disarmed"] = True
        if armed:
            combined["armed_any"] = True
        if loss_room is not None:
            combined["loss_room"] = (combined["loss_room"] or 0.0) + loss_room

    if combined["equity"] > 0:
        combined["open_risk_pct"] = combined["open_risk_dollars"] / combined["equity"] * 100.0
    if loss_limit > 0 and by_broker:
        # Session meter: worst broker used-pct (or combined day loss vs per-broker limit)
        combined["session_risk_used_pct"] = max(
            float(b.get("session_risk_used_pct") or 0.0) for b in by_broker.values()
        )

    return {"combined": combined, "by_broker": by_broker}


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
                          existing_name_value=0.0, size_frac=1.0,
                          risk_pct_per_trade=None, open_risk_dollars=0.0,
                          max_open_risk_pct=None):
    """
    Full sizing math + skip diagnostics.

    Returns dict with trade (float), skip_reason (str|None), and intermediate
    caps (aim, soft_room, soft_cap, risk_size, deployable, already, …).

    Risk-$ path: notional ≤ (equity × risk_pct) / stop_distance, further capped by
    remaining book heat (max_open_risk_pct × equity − open_risk_dollars) / stop.

    When stop_distance is missing/invalid: fall back to util/slot aim only
    (no risk-$ cap) and set sizing_note accordingly — preserves Small-BP behavior.

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
        "risk_budget": 0.0,
        "deployable": 0.0,
        "already": 0.0,
        "name_limit": 0.0,
        "size_frac": 1.0,
        "min_dollars": float(min_dollars or 5.0),
        "equity": 0.0,
        "risk_pct": float(RISK_PCT_PER_TRADE),
        "sizing_mode": "risk_dollar",
        "sizing_note": "",
        "used_stop_fallback": False,
    }
    bp = float(buying_power or 0.0)
    eq = float(equity or 0.0)
    if eq <= 0:
        eq = bp
    out["equity"] = eq
    stop_d = float(stop_distance_pct or 0.0)
    min_d = float(min_dollars or 5.0)
    out["min_dollars"] = min_d
    if bp <= 0:
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

    # Risk % of equity — settings/profiles use percent points (0.75 = 0.75%);
    # bare fractions ≤ ~5% (e.g. 0.0075) also accepted for call-site compat.
    try:
        if risk_pct_per_trade is None:
            risk_pct = float(RISK_PCT_PER_TRADE)
        else:
            risk_pct = float(risk_pct_per_trade)
            if risk_pct > 0.05:
                risk_pct = risk_pct / 100.0
    except (TypeError, ValueError):
        risk_pct = float(RISK_PCT_PER_TRADE)
    risk_pct = min(0.05, max(0.001, risk_pct))
    out["risk_pct"] = risk_pct
    risk_budget = eq * risk_pct
    out["risk_budget"] = round(risk_budget, 2)

    try:
        if max_open_risk_pct is None:
            book_risk_pct = float(DEFAULT_MAX_OPEN_RISK_PCT)
        else:
            book_risk_pct = float(max_open_risk_pct)
            if book_risk_pct > 1.0:
                book_risk_pct = book_risk_pct / 100.0
    except (TypeError, ValueError):
        book_risk_pct = float(DEFAULT_MAX_OPEN_RISK_PCT)
    book_risk_pct = min(0.25, max(0.01, book_risk_pct))
    try:
        open_risk = max(0.0, float(open_risk_dollars or 0.0))
    except (TypeError, ValueError):
        open_risk = 0.0
    remaining_heat_dollars = max(0.0, eq * book_risk_pct - open_risk)

    stop_ok = stop_d > 1e-8
    if stop_ok:
        risk_size = risk_budget / stop_d
        if remaining_heat_dollars > 0 and stop_d > 0:
            heat_cap = remaining_heat_dollars / stop_d
            risk_size = min(risk_size, heat_cap)
        out["risk_size"] = round(risk_size, 2)
        out["sizing_mode"] = "risk_dollar"
        out["sizing_note"] = ""
        out["used_stop_fallback"] = False
    else:
        # Graceful degrade: util/slot aim without risk-$ cap (Small-BP / unknown stop)
        risk_size = deployable
        out["risk_size"] = round(risk_size, 2)
        out["sizing_mode"] = "util_fallback"
        out["sizing_note"] = "stop distance unknown — util/slot sizing (no risk-$ cap)"
        out["used_stop_fallback"] = True

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
                          existing_name_value=0.0, size_frac=1.0,
                          risk_pct_per_trade=None, open_risk_dollars=0.0,
                          max_open_risk_pct=None):
    """
    Buying-power-aware concentrated sizing:
      aim ≈ max(deployable / focus_slots, alloc% × deployable) × conviction × size_frac
    where focus_slots = min(remaining open capacity, sizing_focus_slots).

    Deploy most usable BP into fewer high-conviction tickets rather than spraying
    min clips across a large max_open book. Hard/soft caps (smallest wins): risk $
    (equity × risk_pct / stop), soft single-name equity frac, deployable BP,
    remaining book heat.

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
        risk_pct_per_trade=risk_pct_per_trade,
        open_risk_dollars=open_risk_dollars,
        max_open_risk_pct=max_open_risk_pct,
    )
    return float(detail.get("trade") or 0.0)


def compute_slippage_bps(side, quote_price, fill_price):
    """Signed slippage in bps vs quote. Positive = adverse for the side."""
    try:
        q = float(quote_price or 0)
        f = float(fill_price or 0)
    except (TypeError, ValueError):
        return None
    if q <= 0 or f <= 0:
        return None
    s = str(side or "").upper()
    if s == "BUY":
        return (f - q) / q * 10000.0
    if s == "SELL":
        return (q - f) / q * 10000.0
    return None


def note_fill_slippage(slippage_bps):
    """
    Record a fill's slippage for the light execution feedback loop.
    Returns a short note string when an adjustment fires, else "".
    """
    global _fill_feedback_state
    st = _fill_feedback_state
    try:
        bps = float(slippage_bps)
    except (TypeError, ValueError):
        return ""
    recent = list(st.get("recent_slip_bps") or [])
    recent.append(bps)
    if len(recent) > FILL_FEEDBACK_WINDOW:
        recent = recent[-FILL_FEEDBACK_WINDOW:]
    st["recent_slip_bps"] = recent

    adverse = sum(1 for x in recent if x > FILL_FEEDBACK_ADVERSE_BPS)
    now = time.time()
    last = float(st.get("last_adjust_ts") or 0.0)
    note = ""
    if (
        adverse >= FILL_FEEDBACK_ADVERSE_MIN
        and (now - last) >= FILL_FEEDBACK_COOLDOWN_SEC
        and len(recent) >= FILL_FEEDBACK_ADVERSE_MIN
    ):
        bump = float(st.get("offset_bump_pct") or 0.0) + FILL_FEEDBACK_OFFSET_BUMP
        bump = min(FILL_FEEDBACK_MAX_OFFSET_BUMP, bump)
        st["offset_bump_pct"] = bump
        st["size_mult"] = min(1.0, max(0.70, float(st.get("size_mult") or 1.0) * FILL_FEEDBACK_SIZE_MULT))
        st["last_adjust_ts"] = now
        note = (
            f"fill-quality feedback: {adverse}/{len(recent)} adverse "
            f"(>{FILL_FEEDBACK_ADVERSE_BPS:.0f}bps) → offset +{bump:.2f}% · "
            f"size×{float(st['size_mult']):.2f}"
        )
        st["last_note"] = note
    return note


def get_execution_feedback():
    """Current conservative offset bump (% points) and size multiplier."""
    st = _fill_feedback_state
    return {
        "offset_bump_pct": float(st.get("offset_bump_pct") or 0.0),
        "size_mult": float(st.get("size_mult") or 1.0),
        "last_note": str(st.get("last_note") or ""),
        "recent_count": len(st.get("recent_slip_bps") or []),
    }


def reset_execution_feedback():
    """Test helper — clear fill-quality feedback state."""
    global _fill_feedback_state
    _fill_feedback_state = {
        "recent_slip_bps": [],
        "last_adjust_ts": 0.0,
        "offset_bump_pct": 0.0,
        "size_mult": 1.0,
        "last_note": "",
    }


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
                              crypto_only_broker=False, prefer_equity_rth=False):
    """
    Soft ranking delta so new buys prefer names that fit the *current* book.
    Hard blocks stay in concentration_blocks_buy — this only reshuffles priority
    among tickers that already passed BUY filters (unheld / underweight themes first).

    scale_in_candidate: held name that passed evaluate_scale_in — mild penalty vs fresh
    entries instead of the hard -1000 bury.

    crypto_only_broker: skip crypto-share soft penalties (book is expected to be ~all crypto).
    prefer_equity_rth: multi-asset + regular hours — boost equities / soft-penalize crypto
    so a ~$115 RH book prefers stocks when the cash session is open.
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
    want_crypto = bool(is_crypto) or clean in CRYPTO_TICKERS
    if pv > 0:
        frac = crypto_val / pv
        # Small books: start soft-penalizing crypto earlier (book discipline)
        early = 0.85 if pv < SMALL_BOOK_EQUITY else 1.0
        if want_crypto:
            if frac >= MAX_CRYPTO_BOOK_FRAC * 0.75 * early:  # ≥~22.5% on small / 22.5%→30%*0.75
                delta -= 15.0
            elif frac >= MAX_CRYPTO_BOOK_FRAC * 0.5 * early:  # ≥15%
                delta -= 6.0
            elif frac < 0.08:
                delta += 8.0
        else:
            if frac >= MAX_CRYPTO_BOOK_FRAC * 0.75 * early:
                delta += 10.0

    if prefer_equity_rth:
        if want_crypto:
            delta -= float(RTH_CRYPTO_RANK_PENALTY)
        else:
            delta += float(RTH_EQUITY_RANK_BOOST)

    return delta


def get_protective_order(broker_id, ticker):
    bid = _normalize_broker_id(broker_id)
    if bid not in _protective_orders:
        _protective_orders[bid] = {}
    return _protective_orders[bid].get(str(ticker).upper())


def list_protective_orders(broker_id=None):
    """Return [(broker_id, ticker, info_dict), ...] for tracked protective stops."""
    out = []
    if broker_id:
        bids = [_normalize_broker_id(broker_id)]
    else:
        bids = list(_KNOWN_BROKER_IDS)
    for bid in bids:
        for ticker, info in (_protective_orders.get(bid) or {}).items():
            if info:
                out.append((bid, str(ticker).upper(), dict(info)))
    return out


def _qty_is_whole_shares(shares_val):
    """True when qty is an integer >= 1 (RH broker stops require whole shares)."""
    try:
        from decimal import Decimal
        d = Decimal(str(shares_val))
        return d >= 1 and d == d.to_integral_value()
    except Exception:
        return False


def protective_stop_health(holdings, *, paper_mode=False):
    """
    Compare open holdings that should have stops vs tracked protective orders.

    holdings: iterable of {
      broker_id|broker, ticker, value?, shares?, is_crypto?, supports_protective?
    }
    Brokers with supports_protective=False are skipped (E*TRADE).
    Crypto and fractional equity qty are N/A for broker stops (TTP only) — not "missing".
    Returns {ok, missing, fractional_na, crypto_na, tracked, expected, missing_count, ...}.
    """
    expected = []
    fractional_na = []
    crypto_na = []
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        t = str(h.get("ticker") or "").replace("-USD", "").upper()
        if not t:
            continue
        bid = _normalize_broker_id(h.get("broker_id") or h.get("broker") or "")
        if not bid:
            continue
        supports = h.get("supports_protective")
        if supports is False:
            continue
        # Default: ROBINHOOD / COINBASE expect stops; ETRADE does not
        if supports is None and bid == "ETRADE":
            continue
        is_crypto = bool(h.get("is_crypto")) or t in CRYPTO_TICKERS
        if is_crypto:
            # No broker stop API — software TTP only; do not count as missing
            crypto_na.append({"broker_id": bid, "ticker": t, "why": "crypto — TTP only"})
            continue
        try:
            val = float(h.get("value") or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        if val < 1.0 and not paper_mode:
            # skip dust
            continue
        shares = h.get("shares")
        if shares is None:
            shares = h.get("qty")
        try:
            shares_f = float(shares) if shares is not None else None
        except (TypeError, ValueError):
            shares_f = None
        # RH rejects stops on fractional qty — classify separately from true gaps
        if shares_f is not None and not _qty_is_whole_shares(shares_f):
            fractional_na.append({
                "broker_id": bid,
                "ticker": t,
                "shares": shares_f,
                "why": "fractional — broker stop N/A, TTP only",
            })
            continue
        expected.append((bid, t))

    tracked_set = {(b, t) for b, t, _ in list_protective_orders()}
    missing = [{"broker_id": b, "ticker": t} for b, t in expected if (b, t) not in tracked_set]
    return {
        "ok": len(missing) == 0,
        "expected": len(expected),
        "tracked": len(tracked_set),
        "missing_count": len(missing),
        "missing": missing[:12],
        "fractional_na_count": len(fractional_na),
        "fractional_na": fractional_na[:12],
        "crypto_na_count": len(crypto_na),
        "crypto_na": crypto_na[:12],
    }


def cluster_heat_snapshot(held_tickers):
    """
    Live correlation-cluster fill for UI.
    held_tickers: iterable of ticker strings (any broker combined or single book).
    Returns ordered list of {name, held, count, max, full, members}.
    """
    held = {str(t).replace("-USD", "").upper() for t in (held_tickers or []) if t}
    rows = []
    for name, members in CORRELATION_CLUSTERS.items():
        overlap = sorted(held & set(members))
        n = len(overlap)
        rows.append({
            "name": name,
            "held": overlap,
            "count": n,
            "max": int(MAX_CLUSTER_POSITIONS),
            "full": n >= MAX_CLUSTER_POSITIONS,
            "members": sorted(members),
        })
    # fullest first, then name
    rows.sort(key=lambda r: (-r["count"], r["name"]))
    return rows


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

    # Mute repeat scale-in noise (FinRL: fewer actions / prefer hold)
    bid = _normalize_broker_id(broker_id)
    last_ts = float((_scale_in_last_ts.get(bid) or {}).get(clean, 0) or 0)
    if last_ts > 0:
        elapsed = time.time() - last_ts
        if elapsed < float(SCALE_IN_REPEAT_COOLDOWN_SEC):
            mins = int((SCALE_IN_REPEAT_COOLDOWN_SEC - elapsed) / 60) + 1
            result["reason"] = f"scale-in cooldown ({mins}m left)"
            return result

    try:
        px = float(current_price or 0.0)
    except (TypeError, ValueError):
        px = 0.0
    cost = _usable_holding_cost(avg_cost, px)
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

    # Crypto scale-ins must still clear net-of-cost discipline on the add notional path
    if bool(is_crypto) or clean in CRYPTO_TICKERS:
        need = min_entry_edge_pct(broker_id, clean, asset_type or "cryptocurrency")
        try:
            sc_probe = float(signal_score) if signal_score is not None else None
        except (TypeError, ValueError):
            sc_probe = None
        if sc_probe is not None:
            edge = estimated_signal_edge_pct(sc_probe, is_crypto=True)
            if edge + 1e-12 < need * 0.75:
                result["reason"] = (
                    f"crypto add edge thin ({edge*100:.2f}% < "
                    f"~{need * 0.75 * 100:.1f}% of RT+edge need)"
                )
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
                            crypto_only_broker=False, prefer_equity_rth=False):
    """Signal quality + soft portfolio-fit adjustment for this book."""
    base = buy_rank_score(ticker, is_crypto=is_crypto)
    adj = portfolio_buy_rank_adjust(
        ticker, held_tickers, holdings_meta=holdings_meta,
        portfolio_value=portfolio_value, is_crypto=is_crypto,
        scale_in_candidate=scale_in_candidate,
        crypto_only_broker=crypto_only_broker,
        prefer_equity_rth=prefer_equity_rth,
    )
    return base + adj


# =========================================================================
# OPPORTUNITY SWAP (balanced + aggressive capital recycle)
# =========================================================================

def opportunity_swap_params(posture=None):
    """
    Guardrails for selling a weaker holding to fund a superior BUY.
    Safer posture: disabled. Returns a plain dict (safe to mutate by caller).
    """
    p = normalize_risk_posture(posture)
    if p == "safer":
        return {
            "enabled": False,
            "roi_floor": 0.0,
            "score_gap": 999.0,
            "min_hold_crypto_min": 90.0,
            "min_hold_equity_min": 90.0,
            "hard_stop_buffer": 0.005,
            "max_rotates_per_day": 0,
            "fee_buffer_pct": max(MIN_PROFIT_OVER_FEES_PCT, MIN_ENTRY_EDGE_OVER_FEES_PCT),
            "score_to_edge_pct": 0.0015,
            "freeze_on_regime_fail": True,
        }
    if p == "aggressive":
        return {
            "enabled": True,
            "roi_floor": -0.03,
            "score_gap": 10.0,
            "min_hold_crypto_min": 60.0,
            "min_hold_equity_min": 90.0,
            "hard_stop_buffer": 0.005,
            "max_rotates_per_day": 2,
            "fee_buffer_pct": max(MIN_PROFIT_OVER_FEES_PCT, 0.015),
            "score_to_edge_pct": 0.0012,  # need larger score gap to clear fees
            "freeze_on_regime_fail": True,
        }
    # balanced — tighter hold bias / fewer rotates (FinRL fewer actions)
    return {
        "enabled": True,
        "roi_floor": -0.015,
        "score_gap": 15.0,
        "min_hold_crypto_min": 90.0,
        "min_hold_equity_min": 90.0,
        "hard_stop_buffer": 0.005,
        "max_rotates_per_day": 1,
        "fee_buffer_pct": max(MIN_PROFIT_OVER_FEES_PCT, 0.015),
        "score_to_edge_pct": 0.0012,
        "freeze_on_regime_fail": True,
    }


def opportunity_swap_enabled(posture=None):
    return bool(opportunity_swap_params(posture).get("enabled"))


# Estimated one-way friction (spread + commission) for analytics / rotate fee gates.
# Round-trip ≈ 2x. Honest Est. for retail/self-directed use — not broker invoices.
# RH listed equities: $0 commission; tiny SEC/TAF + spread crumbs ≈ 10 bps one-way.
# E*TRADE equities (Andrew schedule): $0 commission stocks/ETFs; Est. still 10 bps one-way
#   for SEC/spread — not underestimating to penny-takes. No ETRADE_CRYPTO profile: this
#   autotrader is equity/ETF-only on ET (schedule crypto 0.50% unused; if ever wired →
#   1.0% RT / exit floor 2.0%).
# RH crypto: this app uses robin_stocks order_*_crypto_by_quantity (classic API).
#   MM routing (default / API v1): RH rebate ~$0.95/$100 embedded in spread ≈ 0.95% one-way.
#   Smart exchange routing (in-app opt-in): Andrew's tiers show <$50K = 0.95% one-way.
#   Prefer not underestimating small books → Est. 0.95% one-way either path.
# Coinbase Intro 1: maker 0.60% / taker 1.20% — MA uses market/taker → 1.2% one-way.
# Intro 2 (0.40%/0.80% @ $10K) / Advanced 1 (0.25%/0.50% @ $25K) — lower later if volume rises.
_FEE_ONE_WAY_PCT = {
    "ROBINHOOD_STOCK": 0.0010,   # listed $0 commission; 10 bps SEC/spread est.
    "ROBINHOOD_CRYPTO": 0.0095,  # Est. ~0.95% smart-exchange / MM rebate tier
    "COINBASE": 0.0120,          # Intro 1 taker (not maker 0.60%)
    "ETRADE_STOCK": 0.0010,      # listed $0 stocks/ETFs; 10 bps friction est.
}


def fee_profile_key(broker_id, ticker=None, asset_type=""):
    """Canonical FEE_PROFILES key for journaling / reports."""
    return _profile_key_for(broker_id, ticker, asset_type)


def _profile_key_for(broker_id, ticker=None, asset_type=""):
    raw = str(broker_id or "").strip().upper()
    if raw in _FEE_ONE_WAY_PCT:
        return raw
    bid = _normalize_broker_id(broker_id)
    clean = str(ticker or "").replace("-USD", "").upper()
    is_crypto = "crypto" in str(asset_type or "").lower() or clean in CRYPTO_TICKERS
    if bid == "COINBASE":
        return "COINBASE"
    if bid == "ETRADE":
        return "ETRADE_STOCK"
    if is_crypto:
        return "ROBINHOOD_CRYPTO"
    return "ROBINHOOD_STOCK"


def estimate_round_trip_fee_pct(broker_id, ticker=None, asset_type=""):
    """Estimated round-trip friction as a fraction of notional (e.g. 0.012 = 1.2%)."""
    key = _profile_key_for(broker_id, ticker, asset_type)
    one = float(_FEE_ONE_WAY_PCT.get(key, 0.002))
    return one * 2.0


def estimate_fee_dollars(notional, broker_id, ticker=None, asset_type="", *, round_trip=True):
    """Estimated fee $ for one-way or round-trip on notional."""
    try:
        n = abs(float(notional or 0.0))
    except (TypeError, ValueError):
        n = 0.0
    pct = estimate_round_trip_fee_pct(broker_id, ticker, asset_type)
    if not round_trip:
        pct = pct / 2.0
    return n * pct


# Per-broker daily rotate counters: { "ROBINHOOD|2026-08-05": 2 }
_rotate_day_counts = {}


def _rotate_day_key(broker_id, day=None):
    bid = _normalize_broker_id(broker_id)
    if day is None:
        day = datetime.now().date().isoformat()
    return f"{bid}|{day}"


def rotates_today(broker_id, day=None):
    return int(_rotate_day_counts.get(_rotate_day_key(broker_id, day), 0) or 0)


def record_rotation(broker_id, day=None):
    """Increment daily rotate counter (call after a successful rotate sell)."""
    key = _rotate_day_key(broker_id, day)
    _rotate_day_counts[key] = rotates_today(broker_id, day) + 1
    save_state(force=True)
    return _rotate_day_counts[key]


def rotation_allowed_today(broker_id, posture=None, day=None):
    params = opportunity_swap_params(posture)
    if not params.get("enabled"):
        return False, "safer posture"
    cap = int(params.get("max_rotates_per_day") or 0)
    used = rotates_today(broker_id, day)
    if used >= cap:
        return False, f"daily rotate cap ({used}/{cap})"
    return True, ""


def holding_opportunity_score(ticker, is_crypto=False, roi=0.0, score_fn=None):
    """
    Remaining opportunity proxy for a held name.
    Higher = stronger thesis / already working → worse funding victim.
    Lower = weaker remaining edge → preferred funding source.
    """
    clean = str(ticker or "").replace("-USD", "").upper()
    fn = score_fn or buy_rank_score
    try:
        base = float(fn(clean, is_crypto=bool(is_crypto)))
    except TypeError:
        base = float(fn(clean))
    except Exception:
        base = 0.0
    try:
        r = float(roi or 0.0)
    except (TypeError, ValueError):
        r = 0.0
    # Already-green names look stronger (harder to rotate out)
    return base + max(-20.0, min(30.0, r * 100.0))


def _holding_mem(broker_id, ticker):
    bid = _normalize_broker_id(broker_id)
    key = str(ticker or "").replace("-USD", "").upper()
    return (_portfolio_memory.get(bid) or {}).get(key) or {}


def holding_held_minutes(broker_id, ticker, now=None):
    """Minutes since buy_time in portfolio memory; None if unknown."""
    mem = _holding_mem(broker_id, ticker)
    bt = mem.get("buy_time")
    if not bt:
        return None
    try:
        tnow = float(now if now is not None else time.time())
        return max(0.0, (tnow - float(bt)) / 60.0)
    except (TypeError, ValueError):
        return None


# Cost < this fraction of live price ⇒ dust / bogus basis (same honesty as Discord _sell_roi).
_COST_BASIS_DUST_FRAC = 0.01


def _usable_holding_cost(avg_cost, current_price):
    """
    Return a usable avg cost, or 0.0 when unknown/dust.
    Dust RH cost_bases invent mega-ROI and must not arm TTP / profit exits.
    """
    try:
        cost = float(avg_cost or 0.0)
        px = float(current_price or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if cost <= 0:
        return 0.0
    if px > 0 and cost < px * _COST_BASIS_DUST_FRAC:
        return 0.0
    return cost


def is_ttp_armed_holding(broker_id, ticker, avg_cost, current_price, asset_type="",
                         exit_roi_scale=1.0, exit_time_scale=1.0, ttp_arm_scale=1.0):
    """True when trailing take-profit is armed (do not rotate out winners under trail)."""
    try:
        px = float(current_price or 0.0)
    except (TypeError, ValueError):
        return False
    cost = _usable_holding_cost(avg_cost, px)
    if cost <= 0 or px <= 0:
        return False
    fees = resolve_exit_fees(
        broker_id, ticker, asset_type,
        exit_roi_scale=exit_roi_scale,
        exit_time_scale=exit_time_scale,
        ttp_arm_scale=ttp_arm_scale,
    )
    mem = _holding_mem(broker_id, ticker)
    try:
        highest = float(mem.get("highest") or px)
    except (TypeError, ValueError):
        highest = px
    highest = max(highest, px)
    peak_roi = (highest - cost) / cost
    return peak_roi >= float(fees.get("ttp_arm") or 0.02)


def mark_opportunity_swap_exit(broker_id, ticker):
    """Tag memory so auto-detect cooldown does not treat this as a hard-stop loss."""
    bid = _normalize_broker_id(broker_id)
    key = str(ticker or "").replace("-USD", "").upper()
    if bid not in _portfolio_memory:
        _portfolio_memory[bid] = {}
    mem = _portfolio_memory[bid].setdefault(key, {"highest": 0.0, "buy_time": time.time(), "last_eval": time.time()})
    mem["exit_reason"] = "opportunity_swap"
    mem["last_eval"] = time.time()


def _parse_cluster_block(block_reason):
    """Extract cluster name from concentration reason like 'cluster BTC_BETA full (...)'."""
    text = str(block_reason or "")
    if "cluster " not in text.lower():
        return None
    try:
        # cluster NAME full
        part = text.split("cluster", 1)[1].strip()
        name = part.split()[0].strip()
        return name if name in CORRELATION_CLUSTERS else None
    except Exception:
        return None


_last_rotate_reject = ""

# Robinhood crypto notional floor (broker rejects under ~$5). Never lower this to "fit" BP.
RH_CRYPTO_MIN_NOTIONAL = 5.0


def broker_min_notional(broker_id, *, is_crypto: bool = False) -> float:
    """Hard broker ticket floor used for rotate-to-clear-BP (not a soft aim)."""
    bid = _normalize_broker_id(broker_id)
    if bid == "ROBINHOOD" and is_crypto:
        return float(RH_CRYPTO_MIN_NOTIONAL)
    if bid == "COINBASE":
        return float(RH_CRYPTO_MIN_NOTIONAL)  # CB Advanced also rejects tiny notionals
    return 5.0


def pick_rotation_funding(
    candidate_ticker,
    candidate_score,
    candidate_is_crypto,
    holdings,
    *,
    posture="balanced",
    broker_id="ROBINHOOD",
    block_reason="",
    now=None,
    score_fn=None,
    exit_roi_scale=1.0,
    exit_time_scale=1.0,
    ttp_arm_scale=1.0,
    skip_regime_check=False,
    need_dollars=None,
    current_bp=None,
):
    """
    Choose one held name to fully exit so we can fund candidate_ticker.
    Returns dict {ticker, score, roi, value, is_crypto, reason, ...} or None.

    When ``need_dollars`` is set (e.g. RH crypto floor $5 with BP under floor),
    prefer a funder whose proceeds clear the shortfall. Still respects ROI / TTP /
    fee / score-gap / daily rotate caps — never invents under-floor buys.
    """
    global _last_rotate_reject
    _last_rotate_reject = ""
    params = opportunity_swap_params(posture)
    if not params.get("enabled"):
        _last_rotate_reject = "safer posture"
        return None

    ok_day, day_why = rotation_allowed_today(broker_id, posture=posture)
    if not ok_day:
        _last_rotate_reject = day_why
        return None

    if params.get("freeze_on_regime_fail") and not skip_regime_check:
        try:
            regime_ok, regime_why = market_regime_ok(is_crypto=bool(candidate_is_crypto))
        except Exception:
            regime_ok, regime_why = False, "regime check failed"
        if not regime_ok:
            _last_rotate_reject = f"regime freeze ({regime_why or 'risk-off'})"
            return None

    cand = str(candidate_ticker or "").replace("-USD", "").upper()
    if not cand:
        return None
    try:
        cand_score = float(candidate_score or 0.0)
    except (TypeError, ValueError):
        cand_score = 0.0
    gap = float(params["score_gap"])
    roi_floor = float(params["roi_floor"])
    buf = float(params["hard_stop_buffer"])
    # Discretionary rotate edge must clear recycle fees + entry edge floor
    # (posture fee_buffer_pct is kept ≥ that floor; never grind pennies after friction).
    fee_buf = max(
        float(params.get("fee_buffer_pct") or 0.0),
        float(MIN_PROFIT_OVER_FEES_PCT),
        float(MIN_ENTRY_EDGE_OVER_FEES_PCT),
    )
    edge_per_pt = float(params.get("score_to_edge_pct") or 0.0015)
    tnow = float(now if now is not None else time.time())
    block = str(block_reason or "")
    cluster_name = _parse_cluster_block(block)
    cluster_members = set(CORRELATION_CLUSTERS.get(cluster_name) or ()) if cluster_name else set()
    need_cluster_slot = bool(cluster_members)

    try:
        need_d = float(need_dollars) if need_dollars is not None else None
    except (TypeError, ValueError):
        need_d = None
    try:
        bp_now = float(current_bp) if current_bp is not None else 0.0
    except (TypeError, ValueError):
        bp_now = 0.0
    shortfall = 0.0
    if need_d is not None and need_d > 0:
        shortfall = max(0.0, need_d - max(0.0, bp_now))

    candidates = []
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        t = str(h.get("ticker") or "").replace("-USD", "").upper()
        if not t or t == cand:
            continue
        is_c = bool(h.get("is_crypto")) or t in CRYPTO_TICKERS
        try:
            px = float(h.get("price") or 0.0)
        except (TypeError, ValueError):
            px = 0.0
        try:
            raw_cost = float(h.get("avg_cost") or 0.0)
        except (TypeError, ValueError):
            raw_cost = 0.0
        cost = _usable_holding_cost(raw_cost, px)
        try:
            val = float(h.get("value") or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        if cost > 0 and px > 0:
            roi = (px - cost) / cost
        else:
            try:
                roi = float(h.get("roi"))
            except (TypeError, ValueError):
                roi = 0.0

        asset_type = h.get("asset_type") or ("cryptocurrency" if is_c else "stock")
        hard_stop = -abs(float(get_stop_distance_pct(broker_id, ticker=t, asset_type=asset_type)))
        if roi <= hard_stop + buf:
            continue
        if roi < roi_floor:
            continue

        held_m = holding_held_minutes(broker_id, t, now=tnow)
        min_hold = float(params["min_hold_crypto_min"] if is_c else params["min_hold_equity_min"])
        if held_m is not None and held_m < min_hold:
            continue

        if is_ttp_armed_holding(
            broker_id, t, cost if cost > 0 else px, px if px > 0 else cost,
            asset_type=asset_type,
            exit_roi_scale=exit_roi_scale,
            exit_time_scale=exit_time_scale,
            ttp_arm_scale=ttp_arm_scale,
        ):
            continue

        if need_cluster_slot and t not in cluster_members:
            continue

        hold_score = holding_opportunity_score(t, is_crypto=is_c, roi=roi, score_fn=score_fn)
        score_delta = cand_score - hold_score
        if score_delta < gap:
            continue

        # Fee-aware: expected edge from score gap must beat round-trip + buffer
        # on the funding notional (sell + buy friction).
        rt_fee = estimate_round_trip_fee_pct(broker_id, t, asset_type)
        # Candidate buy also pays one-way; approximate full recycle as 1.5x RT on fund value
        # (sell fund + buy cand ≈ 1.5–2x one name RT). Use max of fund and cand profiles.
        rt_cand = estimate_round_trip_fee_pct(
            broker_id, cand, "cryptocurrency" if candidate_is_crypto else "stock"
        )
        recycle_fee = max(rt_fee, rt_cand) * 1.25
        edge_pct = float(score_delta) * edge_per_pt
        if edge_pct < recycle_fee + fee_buf:
            continue

        same_sleeve = (bool(candidate_is_crypto) == bool(is_c))
        clears_shortfall = True if shortfall <= 0 else (val + 1e-9 >= shortfall)
        candidates.append({
            "ticker": t,
            "score": hold_score,
            "roi": roi,
            "value": val,
            "is_crypto": is_c,
            "same_sleeve": same_sleeve,
            "clears_shortfall": clears_shortfall,
            "price": px,
            "avg_cost": cost,
            "shares": float(h.get("shares") or 0.0),
            "asset_type": asset_type,
            "edge_pct": edge_pct,
            "recycle_fee_pct": recycle_fee,
            "fee_est": estimate_fee_dollars(val, broker_id, t, asset_type, round_trip=True),
            "reason": (
                f"score gap {score_delta:.0f} "
                f"(cand {cand_score:.0f} vs hold {hold_score:.0f}, need +{gap:.0f}); "
                f"roi {roi*100:.2f}%; edge {edge_pct*100:.2f}% vs fees {recycle_fee*100:.2f}%"
            ),
        })

    if not candidates:
        _last_rotate_reject = "no eligible funding name (ROI/TTP/hold/fees/gap)"
        return None

    if shortfall > 0:
        clearing = [c for c in candidates if c.get("clears_shortfall")]
        if not clearing:
            _last_rotate_reject = (
                f"no funder frees enough for broker floor "
                f"(need ≥${shortfall:.2f} proceeds; BP ${bp_now:.2f})"
            )
            return None
        candidates = clearing

    candidates.sort(
        key=lambda x: (
            0 if x.get("same_sleeve") else 1,
            0 if x.get("clears_shortfall") else 1,
            float(x.get("score") or 0.0),
            float(x.get("roi") or 0.0),
            -float(x.get("value") or 0.0),
        )
    )
    return candidates[0]


def last_rotation_reject_reason():
    return str(_last_rotate_reject or "")


# =========================================================================
# PRIMARY EVALUATION ENGINES
# =========================================================================

def evaluate_holding(ticker, avg_cost, broker_id="ROBINHOOD", asset_type="", live_price=None,
                     exit_roi_scale=1.0, exit_time_scale=1.0, ttp_arm_scale=1.0,
                     allow_flat_time_banks=False):
    """
    Trailing take-profit / hard stop / time-stop.
    Fee thresholds change by broker so CB doesn't take thin RH-style exits.
    ATR may widen hard_stop / trail / arm / time rails (capped at 2×); posture scales time / TTP-arm.

    Primary green exit is peak-aware TTP trail (arm, then trail from local high).
    Flat Time-Green / Time-Stop are off for Safer/Balanced (allow_flat_time_banks=False).
    When enabled (Aggressive), they only fire after a local turn — never while still
    riding near the position high / short-term EMA up. Hard stop + stale (red) stay on.

    Unknown / dust cost basis: TTP and flat green exits stay gated (honesty). Hard stop
    and stale use a live-price reference so bags are not unmanaged forever.
    """
    broker_id = _normalize_broker_id(broker_id)
    current_price = float(live_price) if live_price and live_price > 0 else fetch_current_price(ticker)
    if current_price <= 0: return "HOLD (Awaiting Price)"

    # Coinbase / RH dust cost_bases: unknown basis — do not invent mega-ROI "wins".
    try:
        avg_cost = float(avg_cost or 0.0)
    except (TypeError, ValueError):
        avg_cost = 0.0
    avg_cost = _usable_holding_cost(avg_cost, current_price)
    unknown_basis = avg_cost <= 0
    if unknown_basis:
        avg_cost = current_price  # protective reference only

    fees = resolve_exit_fees(
        broker_id, ticker, asset_type,
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

    if unknown_basis:
        # Stale red management only — no TTP / time-green without a real basis
        stale_min = float(fees.get("stale_minutes", 120) or 120)
        if held_time_minutes >= stale_min and roi < fees["stale_roi"]:
            save_state(force=True)
            hrs = stale_min / 60.0
            hrs_lbl = f"{hrs:.0f}h" if abs(hrs - round(hrs)) < 1e-9 else f"{hrs:.1f}h"
            return f"SELL (Stale > {hrs_lbl}, Unknown Cost)"
        save_state()
        return "HOLD (Unknown Cost — TTP/ROI gated)"

    peak_roi = (highest - avg_cost) / avg_cost
    if peak_roi >= fees["ttp_arm"]:
        trail_trigger_price = highest * (1.0 - fees["ttp_trail"])
        if current_price <= trail_trigger_price:
            save_state(force=True)
            return f"SELL (TTP Triggered - Peak: +{peak_roi*100:.2f}%, Exit: +{roi*100:.2f}%)"
        save_state()
        return f"HOLD (TTP Armed - Peak: +{peak_roi*100:.2f}%)"

    # Flat time banks: Safer/Balanced off. Aggressive only after local turn (not still climbing).
    riding = _still_riding_local_uptrend(current_price, highest, fees, ticker=ticker)
    flat_ok = bool(allow_flat_time_banks) and not riding

    if flat_ok:
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
    elif flat_ok and held_time_minutes >= 60 and roi >= fees["time_60m_target"]:
        save_state(force=True)
        return f"SELL (Time-Stop > 1h, +{fees['time_60m_target']*100:.1f}% Target Hit)"
    elif flat_ok and held_time_minutes >= 30 and roi >= fees["time_30m_target"]:
        save_state(force=True)
        return f"SELL (Time-Stop > 30m, +{fees['time_30m_target']*100:.1f}% Target Hit)"

    save_state()
    return f"HOLD (ROI: {roi*100:.2f}%)"


def evaluate_crypto_opportunity(ticker, broker_id="ROBINHOOD", live_price=None, posture="balanced"):
    broker_id = _normalize_broker_id(broker_id)
    current_price = float(live_price) if live_price and live_price > 0 else fetch_current_price(ticker)
    if current_price <= 0: return "DO NOT BUY (Awaiting Price)"

    # Turbulence pause (all postures) + Safer/Balanced BTC regime gate
    tok, turbulence_why = crypto_turbulence_ok()
    if not tok:
        return turbulence_why
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
        # Hold bias: weak scores stay HOLD — prefer no-trade vs OW-fee churn
        try:
            sc = float(buy_rank_score(ticker, is_crypto=True))
        except Exception:
            sc = 0.0
        if sc < float(CRYPTO_MIN_SCORE_FOR_ENTRY):
            return (
                f"DO NOT BUY (Hold bias: score {sc:.0f} < "
                f"{CRYPTO_MIN_SCORE_FOR_ENTRY:.0f})"
            )
        return f"BUY (MTF Confirmed | RSI: {rsi:.1f} | Score: {sc:.0f})"

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
