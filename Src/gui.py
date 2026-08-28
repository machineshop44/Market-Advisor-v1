import os
import sys
import re
import time
import math
import json
import builtins
import webbrowser
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_DOWN

from PyQt5.QtWidgets import (QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
                             QBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
                             QAbstractItemView,
                             QPushButton, QMessageBox, QInputDialog, QLineEdit, 
                             QApplication, QStatusBar, QFrame, QCheckBox, QComboBox,
                             QDoubleSpinBox, QSpinBox, QTextEdit, QFileDialog, QDialog, QDialogButtonBox,
                             QFormLayout, QGroupBox, QProgressBar, QStackedWidget,
                             QRadioButton, QButtonGroup,
                             QSystemTrayIcon, QMenu, QAction, QScrollArea, QSizePolicy)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QEventLoop, QPoint, QSize
from PyQt5.QtGui import QPainter, QPen, QColor, QPalette, QPixmap, QPolygon, QIcon, QCursor
import threading

import journal
import monitor
import companion_qr
from balance_guard import (
    balance_reading_is_suspicious,
    decide_suspicious_equity,
    is_near_zero_wipe,
    reference_equity,
    NEAR_ZERO_EQUITY,
)
from broker import RobinhoodAdapter, CoinbaseAdapter
from etrade_broker import ETradeAdapter
from version import (
    APP_NAME,
    APP_NAME_COMPACT,
    VERSION_NOTE,
    display_name,
    user_agent,
    window_title,
    __version__ as APP_VERSION,
)

# Heavy scoring/finviz libs load on first use (pandas/yfinance/robin are slow)
_FINVIZ_AVAILABLE = None
_Overview = None


def _get_overview_class():
    global _FINVIZ_AVAILABLE, _Overview
    if _FINVIZ_AVAILABLE is None:
        try:
            from finvizfinance.screener.overview import Overview as _Ov
            _Overview = _Ov
            _FINVIZ_AVAILABLE = True
        except ImportError:
            _Overview = None
            _FINVIZ_AVAILABLE = False
    return _Overview


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
ACTIVITY_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity_log.txt")
# UI + active file stay bounded; older lines archive indefinitely (never deleted by rotate).
from activity_log_util import (  # noqa: E402
    ACTIVITY_LOG_UI_MAX_LINES,
    ACTIVITY_LOG_DISK_MAX_BYTES,
    ACTIVITY_LOG_DISK_MAX_LINES,
    ACTIVITY_LOG_DISK_KEEP_LINES,
    ACTIVITY_LOG_DISK_TAIL_LINES,
    tail_activity_log_file as _tail_activity_log_file_impl,
    rotate_activity_log_if_needed as _rotate_activity_log_if_needed_impl,
    explain_no_buys_after_rank as _explain_no_buys_after_rank_impl,
    sell_fail_should_skip as _sell_fail_should_skip_impl,
    record_sell_fail_backoff as _record_sell_fail_backoff_impl,
)
import auto_cycle as _auto_cycle  # noqa: E402
import decision_log as _decision_log  # noqa: E402
ACTIVITY_LOG_UI_REBUILD_AT = 2200  # rebuild QTextEdit from buffer when over this
ACTIVITY_LOG_UI_REBUILD_EVERY = 500  # safety-net rebuild after N appends
ACTIVITY_LOG_DISK_CHECK_EVERY = 100  # rotate check every N disk appends
# Protective-stop repair throttles
_STOP_REPAIR_PASS_COOLDOWN_SEC = 180
_STOP_REPAIR_TICKER_COOLDOWN_SEC = 1800
_STOP_REPAIR_MAX_PER_PASS = 8
KNOWN_CRYPTOS = {
    "BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI",
    "FET", "AMP", "ADA", "DOT", "MATIC", "ATOM", "LTC", "BCH", "XRP", "NEAR", "AAVE",
}
BROKER_NAMES = ("Robinhood", "Coinbase", "E*TRADE")


def _tail_activity_log_file(path=None, max_lines=ACTIVITY_LOG_DISK_TAIL_LINES):
    """Return the last max_lines from the activity log without loading the whole file as one string."""
    return _tail_activity_log_file_impl(path or ACTIVITY_LOG_FILE, max_lines=max_lines)


def _rotate_activity_log_if_needed(path=None, force=False):
    """Bound the active log file; spill older lines into activity_log_archives/ (kept forever)."""
    return _rotate_activity_log_if_needed_impl(path or ACTIVITY_LOG_FILE, force=force)


def _blank_broker_map(default=None):
    """Per-broker dict seed for the three adapters."""
    if callable(default):
        return {n: default() for n in BROKER_NAMES}
    return {n: default for n in BROKER_NAMES}


def _is_manual_auth_failure(detail):
    """True when reconnect cannot succeed without user OAuth / credentials."""
    text = str(detail or "").lower()
    needles = (
        "no access token",
        "reauthorization required",
        "authorize in browser",
        "verification code",
        "missing e*trade consumer",
        "missing saved credentials",
        "missing saved api keys",
        "access token expired at midnight",
        "not authorized",
        "unauthorized",
        "reauth required",
        "token expired",
        "token revoked",
        "invalid_token",
        "access denied",
    )
    if any(n in text for n in needles):
        return True
    # HTTP auth failures from broker adapters (e.g. E*TRADE GET … failed (401): …)
    if re.search(r"\b(401|403)\b", text):
        return True
    if "(401)" in text or "(403)" in text:
        return True
    return False


class SuppressPrints:
    """Temporarily mutes sys.stdout, sys.stderr, and builtins.print.

    Safe under pythonw/tray (where stdout/stderr may be None) and nested use.
    """
    def __enter__(self):
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._original_print = builtins.print
        self._null_out = open(os.devnull, "w")
        self._null_err = open(os.devnull, "w")
        sys.stdout = self._null_out
        sys.stderr = self._null_err
        builtins.print = lambda *args, **kwargs: None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        builtins.print = self._original_print
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        for stream in (getattr(self, "_null_out", None), getattr(self, "_null_err", None)):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                pass
        return False


def load_settings():
    defaults = {
        "dark_mode": False,
        "paper_mode": False,
        "discord_webhook": "",
        "discord_alert_level": "All Alerts (Every Trade & Heartbeat)",
        "discord_heartbeat_schedule": "Rolling (every hour from now)",
        "discord_big_win_roi_pct": 1.5,
        "monitor_enabled": True,
        "monitor_host": "127.0.0.1",
        "monitor_port": 8791,
        "monitor_user": "",
        "monitor_pass": "",
        "monitor_controls_enabled": False,
        "monitor_https": True,
        "allocation_pct": 5.0,
        "allocation_pct_crypto": 8.0,
        "allocation_pct_stock": 5.0,
        "min_trade_dollars": 5.0,
        "risk_posture": "balanced",
        "target_bp_utilization_pct": 88.0,
        "sizing_focus_slots": 6,
        "max_single_name_equity_pct": 15.0,
        "conviction_alloc_mult_max": 1.50,
        "allow_scale_in": True,
        "allow_buys_when_regime_blocked": False,  # live default OFF — set True only to bypass SPY/BTC gate
        "scale_in_max_adds": 1,
        "scale_in_size_frac": 0.50,
        "advanced_scale_in_override": False,
        "day_dd_pause_pct": 0.05,
        "peak_dd_pause_pct": 0.12,
        "dd_pause_minutes": 45,
        "exit_roi_scale": 1.0,
        "exit_time_scale": 1.0,
        "ttp_arm_scale": 1.0,
        "allow_flat_time_banks": False,  # Safer/Balanced: TTP trail only
        "limit_offset_pct": 0.1,
        "use_limit_entries": True,
        "use_limit_exits": True,
        "attach_protective_stops": True,
        "et_flatten_before_close": False,
        "advisor_ask_before_apply": True,
        "risk_posture_by_broker": {},
        "risk_pct_per_trade": 0.75,
        "max_open_risk_pct": 6.0,
        "daily_profit_target": 0.0,
        "daily_loss_limit": 8.0,
        "max_open_positions": 8,
        "max_buys_per_cycle": 1,
        "interval_crypto": 45,
        "interval_penny": 60,
        "interval_core": 300,
        "interval_portfolio": 45,
        "interval_balance_refresh": 60,
        "rh_email": "",
        "rh_password": "",
        "cb_api_key": "",
        "cb_api_secret": "",
        "coinbase_live_trading": True,
        "etrade_environment": "sandbox",
        "etrade_consumer_key": "",
        "etrade_account_id_key": "",
        "etrade_token_expires_at": 0.0,
        "etrade_live_trading": False,
        "etrade_arm_intent": False,
        "webull_app_key": "",
        "webull_app_secret": "",
        "webull_env": "sandbox",
        "webull_endpoint": "api.sandbox.webull.com",
        "webull_live_trading": False,
        "prefer_whole_shares_for_stops": True,
        "allow_fractional_ttp_only": True,
        "shadow_guardrail_enabled": True,
        "shadow_adverse_rate_threshold": 0.55,
        "shadow_delta_net_threshold": -25.0,
        "onboarding_complete": False,
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                defaults.update(data)
                # Existing installs: don't force the wizard on upgrade
                if "onboarding_complete" not in data and "first_run_done" not in data:
                    defaults["onboarding_complete"] = True
                elif data.get("first_run_done") and not data.get("onboarding_complete"):
                    defaults["onboarding_complete"] = True
        except Exception:
            pass
            
    if defaults.get("interval_crypto", 30) < 30: defaults["interval_crypto"] = 30
    if defaults.get("interval_penny", 60) < 60: defaults["interval_penny"] = 60
    if defaults.get("interval_core", 300) < 120: defaults["interval_core"] = 300
    if defaults.get("interval_portfolio", 60) < 30: defaults["interval_portfolio"] = 30
    if defaults.get("interval_balance_refresh", 60) < 30: defaults["interval_balance_refresh"] = 30

    try:
        import credentials as cred_mod
        if cred_mod.migrate_settings_secrets(defaults):
            try:
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(cred_mod.scrub_settings_for_disk(defaults), f, indent=4)
            except Exception:
                pass
    except Exception:
        pass
        
    return defaults


def save_settings(settings):
    try:
        payload = dict(settings or {})
        try:
            import credentials as cred_mod
            payload = cred_mod.scrub_settings_for_disk(payload)
        except Exception:
            payload.pop("rh_password", None)
            payload.pop("cb_api_secret", None)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(payload, f, indent=4)
    except Exception as e:
        print(f"Error saving settings: {e}")


def format_currency(value):
    """Format asset prices — keeps extra precision for sub-$1 crypto."""
    try:
        val = float(value)
        if val == 0: return "$0.00"
        if 0 < abs(val) < 1.0:
            formatted = f"{val:.8f}".rstrip('0').rstrip('.')
            parts = formatted.split('.')
            if len(parts) == 1: return f"${formatted}.00"
            elif len(parts[1]) == 1: return f"${formatted}0"
            return f"${formatted}"
        return f"${val:,.2f}"
    except (ValueError, TypeError):
        return "$0.00"


def format_money(value):
    """Always 2-decimal money for balances / Day P&L (avoids -$0.44231471 noise)."""
    try:
        return f"${float(value):,.2f}"
    except (ValueError, TypeError):
        return "$0.00"


class SparklineWidget(QWidget):
    """Close-series sparkline for the Signal research tab."""

    def __init__(self, parent=None, *, min_h=36, max_h=48):
        super().__init__(parent)
        self._closes = []
        self.setMinimumHeight(ui_px(min_h))
        if max_h is None:
            self.setMaximumHeight(16777215)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        else:
            self.setMaximumHeight(ui_px(max_h))
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_closes(self, closes):
        try:
            self._closes = [float(x) for x in (closes or []) if x is not None]
        except Exception:
            self._closes = []
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(2, 4, -2, -4)
        p.fillRect(self.rect(), QColor("#F5F7F6"))
        if len(self._closes) < 2:
            p.setPen(QColor("#9E9E9E"))
            p.drawText(rect, Qt.AlignCenter, "No chart bars yet")
            return
        lo = min(self._closes)
        hi = max(self._closes)
        span = (hi - lo) or 1.0
        w = max(1, rect.width())
        h = max(1, rect.height())
        pts = []
        n = len(self._closes)
        for i, c in enumerate(self._closes):
            x = rect.left() + (i / (n - 1)) * w
            y = rect.bottom() - ((c - lo) / span) * h
            pts.append(QPoint(int(x), int(y)))
        up = self._closes[-1] >= self._closes[0]
        pen = QPen(QColor("#1F8A70" if up else "#C62828"), 2)
        p.setPen(pen)
        for i in range(1, len(pts)):
            p.drawLine(pts[i - 1], pts[i])


class FactorMeterRow(QWidget):
    """Labeled 0–100 meter strip (trend / RSI / volume / regime / score / RS)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(ui_px(6))
        self._bars = {}
        for key, label in (
            ("trend", "Trend"),
            ("rsi", "RSI"),
            ("volume", "Vol"),
            ("regime", "Regime"),
            ("score", "Score"),
            ("rs", "RS"),
        ):
            col = QVBoxLayout()
            col.setSpacing(1)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"font-size: {ui_px(10)}px; color: #666;")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(ui_px(8))
            bar.setMaximumWidth(ui_px(72))
            col.addWidget(lbl)
            col.addWidget(bar)
            host = QWidget()
            host.setLayout(col)
            lay.addWidget(host)
            self._bars[key] = bar
        lay.addStretch(1)

    def set_meters(self, meters: dict):
        meters = meters or {}
        for key, bar in self._bars.items():
            raw = meters.get(key)
            if raw is None:
                bar.setValue(0)
                bar.setStyleSheet("QProgressBar::chunk { background: #BDBDBD; }")
                continue
            try:
                v = int(max(0, min(100, float(raw))))
            except (TypeError, ValueError):
                v = 0
            bar.setValue(v)
            color = "#1F8A70" if v >= 60 else ("#F9A825" if v >= 40 else "#C62828")
            bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")


# Brand-aligned UI tokens (splash green #0D3B2E → teal accent, softer radii)
UI_ACCENT = "#1F8A70"
UI_ACCENT_HOVER = "#26A69A"
UI_SUCCESS = "#2E7D32"
UI_DANGER = "#C62828"
UI_RADIUS_BTN = 9
UI_RADIUS_CARD = 12
UI_RADIUS_INPUT = 8
UI_RADIUS_FRAME = 10
UI_ROW_HEIGHT = 34

# Design baseline ≈ default fitted window; fonts/padding scale from this.
UI_BASE_W = 1120
UI_BASE_H = 720
UI_SCALE_MIN = 0.82
UI_SCALE_MAX = 1.12
# Home 2-col split: below default launch floor (920) so default always splits;
# min window (760) still stacks gracefully.
HOME_SPLIT_MIN_W = 840
_UI_SCALE = 1.0


def ui_scale():
    return _UI_SCALE


def set_ui_scale(scale):
    global _UI_SCALE
    _UI_SCALE = max(UI_SCALE_MIN, min(UI_SCALE_MAX, float(scale)))


def compute_ui_scale(width, height):
    """Scale from window size without letting ultrawide inflate type forever."""
    if width <= 0 or height <= 0:
        return 1.0
    # Past design width, extra horizontal space should be empty margin — not bigger fonts
    capped_w = min(float(width), float(UI_BASE_W))
    sx = capped_w / UI_BASE_W
    sy = float(height) / UI_BASE_H
    return max(UI_SCALE_MIN, min(UI_SCALE_MAX, (sx * sy) ** 0.5))


def ui_px(base, minimum=1):
    """Scale a design-pixel value for the current window size."""
    return max(minimum, int(round(float(base) * _UI_SCALE)))


def cluster_heat_bar_style(dark_mode, *, full=False):
    """QProgressBar stylesheet for Cluster Heat meters (teal = room, warm = full)."""
    if full:
        chunk = "#E57373" if dark_mode else "#C62828"
        track = "#3A1F1F" if dark_mode else "#FFCDD2"
        text = "#FFCDD2" if dark_mode else "#B71C1C"
    else:
        chunk = UI_ACCENT if dark_mode else "#0F6B56"
        track = "#1A2E28" if dark_mode else "#E0F2F1"
        text = "#B2DFDB" if dark_mode else "#0F6B56"
    border = "#2A2F3A" if dark_mode else "#C5CAD3"
    return (
        f"QProgressBar {{ border: 1px solid {border}; "
        f"border-radius: {ui_px(4)}px; background-color: {track}; "
        f"text-align: center; color: {text}; font-size: {ui_px(10)}px; "
        f"font-weight: 600; min-height: {ui_px(12)}px; max-height: {ui_px(15)}px; }}"
        f"QProgressBar::chunk {{ background-color: {chunk}; border-radius: {ui_px(3)}px; }}"
    )


def polish_trades_header(table):
    """Compact columns + Status absorbs leftover width (avoids ultrawide stretch mess)."""
    if table is None:
        return
    hdr = table.horizontalHeader()
    for col in range(table.columnCount()):
        hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
    # Status is usually the long message column
    status_col = min(5, table.columnCount() - 1)
    if table.columnCount() > 0:
        hdr.setSectionResizeMode(status_col, QHeaderView.Stretch)


def top_bar_btn_style(bg, fg="white"):
    """
    Widget-level QSS replaces app theme rules for that button.
    Include padding/min-height so Mode/Auto-Trader don't shrink vs Refresh in light mode.
    Horizontal padding kept modest so metrics keep room at ~1200px with HALT.
    """
    return (
        f"QPushButton {{ background-color: {bg}; color: {fg}; font-weight: 600; "
        f"border-radius: {ui_px(UI_RADIUS_BTN)}px; border: 1px solid rgba(0,0,0,40); "
        f"padding: {ui_px(6)}px {ui_px(10)}px; min-height: {ui_px(28)}px; }}"
    )


def action_btn_style(kind="primary"):
    """Shared Scan / Score / Execute / Save button look."""
    colors = {
        "primary": UI_ACCENT,
        "success": UI_SUCCESS,
        "danger": UI_DANGER,
    }
    bg = colors.get(kind, UI_ACCENT)
    return (
        f"QPushButton {{ background-color: {bg}; color: white; font-weight: 600; "
        f"padding: {ui_px(10)}px {ui_px(14)}px; border-radius: {ui_px(UI_RADIUS_BTN)}px; border: none; }}"
        f"QPushButton:hover {{ background-color: {UI_ACCENT_HOVER if kind == 'primary' else bg}; }}"
    )


def section_header_style():
    return (
        f"font-size: {ui_px(15)}px; font-weight: 600; letter-spacing: 0.2px; "
        f"padding: {ui_px(6)}px 0 {ui_px(4)}px 0;"
    )


def metric_label_style(color, size=16):
    """Color/size for money metrics. Padding avoids stylesheet-font clipping in GroupBoxes."""
    fs = ui_px(size)
    pad = max(2, fs // 6)
    return (
        f"font-size: {fs}px; font-weight: 600; color: {color}; "
        f"background: transparent; padding: {pad}px 2px;"
    )


def top_bar_metric_style(color, size=16):
    """Top-bar money labels: extra right pad so QSS font doesn't clip the last digit."""
    fs = ui_px(size)
    pad_v = max(2, fs // 6)
    pad_r = max(6, fs // 2)
    return (
        f"font-size: {fs}px; font-weight: 600; color: {color}; "
        f"background: transparent; padding: {pad_v}px {pad_r}px {pad_v}px 2px;"
    )


def theme_colors(dark_mode):
    """Readable semantic colors for both themes (light tuned for contrast)."""
    if dark_mode:
        return {
            "accent": UI_ACCENT,
            "success": "#00E676",
            "danger": "#FF5252",
            "warn": "#FFB300",
            "muted": "#9AA0A6",
            "text": "#E8EAED",
            "neutral": "#B0B0B0",
        }
    return {
        "accent": "#0F6B56",
        "success": "#1B5E20",
        "danger": "#B71C1C",
        "warn": "#E65100",
        "muted": "#3C4043",
        "text": "#1A1A1A",
        "neutral": "#424242",
    }


def polish_table(table):
    """Taller rows, quieter chrome — call once when creating each table."""
    if table is None:
        return
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(ui_px(UI_ROW_HEIGHT))


class BrokerArmDialog(QDialog):
    """Checkbox picker to arm / update Auto-Trader per broker."""

    def __init__(self, parent, broker_rows, dark_mode=True, managing=False):
        """
        broker_rows: list of dicts with keys:
          name, label, enabled (checkbox enabled), checked (initial)
        """
        super().__init__(parent)
        self.setWindowTitle("Manage Armed Brokers" if managing else "Arm Auto-Trader")
        self.setModal(True)
        self.setMinimumWidth(ui_px(420))
        tc = theme_colors(dark_mode)
        if dark_mode:
            self.setStyleSheet(
                f"""
                QDialog {{ background-color: #151820; color: {tc['text']}; }}
                QLabel {{ color: {tc['text']}; background: transparent; }}
                QCheckBox {{ color: {tc['text']}; spacing: {ui_px(8)}px; }}
                QCheckBox::indicator {{
                    width: {ui_px(16)}px; height: {ui_px(16)}px;
                    border: 1px solid #3A4150; border-radius: 3px; background: #1A1D24;
                }}
                QCheckBox::indicator:checked {{
                    background-color: {UI_ACCENT}; border-color: {UI_ACCENT};
                }}
                QCheckBox::indicator:disabled {{
                    background-color: #22262E; border-color: #2A2F3A;
                }}
                QPushButton {{
                    background-color: #22262E; color: {tc['text']};
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_BTN)}px;
                    padding: {ui_px(7)}px {ui_px(14)}px; min-height: {ui_px(28)}px;
                }}
                QPushButton:hover {{ background-color: #2A303A; border-color: #3A4150; }}
                """
            )

        root = QVBoxLayout(self)
        root.setContentsMargins(ui_px(16), ui_px(14), ui_px(16), ui_px(14))
        root.setSpacing(ui_px(10))

        hint = QLabel(
            "Choose which brokers Auto-Trader should run on. "
            "Uncheck a broker to leave it disarmed (or disarm it if already on)."
            if managing else
            "Select one or more connected brokers to arm. Use Select All for every eligible broker."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        root.addWidget(hint)

        self._checks = {}
        for row in broker_rows:
            name = row["name"]
            chk = QCheckBox(row["label"])
            chk.setEnabled(bool(row.get("enabled", True)))
            chk.setChecked(bool(row.get("checked", False)) and chk.isEnabled())
            if not chk.isEnabled():
                chk.setToolTip(row.get("disabled_tip") or "Connect this broker in Settings (or enable Paper Mode).")
            self._checks[name] = chk
            root.addWidget(chk)

        sel_row = QHBoxLayout()
        sel_row.setSpacing(ui_px(8))
        select_all_btn = QPushButton("Select All")
        clear_btn = QPushButton("Clear")
        select_all_btn.clicked.connect(self._select_all_eligible)
        clear_btn.clicked.connect(self._clear_all)
        sel_row.addWidget(select_all_btn)
        sel_row.addWidget(clear_btn)
        sel_row.addStretch(1)
        root.addLayout(sel_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        if ok_btn:
            ok_btn.setText("OK")
            ok_btn.setProperty("uiBtnKind", "primary")
            ok_btn.setStyleSheet(action_btn_style("primary"))
        if cancel_btn:
            cancel_btn.setText("Cancel")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _select_all_eligible(self):
        for chk in self._checks.values():
            if chk.isEnabled():
                chk.setChecked(True)

    def _clear_all(self):
        for chk in self._checks.values():
            chk.setChecked(False)

    def selected_brokers(self):
        return [name for name, chk in self._checks.items() if chk.isChecked() and chk.isEnabled()]


class AutoTraderOffDialog(QDialog):
    """When Auto-Trader is ON: turn everything off, or open the broker picker."""

    OFF_ALL = 1
    MANAGE = 2

    def __init__(self, parent, armed_brokers, dark_mode=True):
        super().__init__(parent)
        self.setWindowTitle("Auto-Trader")
        self.setModal(True)
        self.setMinimumWidth(ui_px(420))
        self.choice = None
        tc = theme_colors(dark_mode)
        if dark_mode:
            self.setStyleSheet(
                f"""
                QDialog {{ background-color: #151820; color: {tc['text']}; }}
                QLabel {{ color: {tc['text']}; background: transparent; }}
                QPushButton {{
                    background-color: #22262E; color: {tc['text']};
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_BTN)}px;
                    padding: {ui_px(7)}px {ui_px(14)}px; min-height: {ui_px(28)}px;
                }}
                QPushButton:hover {{ background-color: #2A303A; border-color: #3A4150; }}
                """
            )

        root = QVBoxLayout(self)
        root.setContentsMargins(ui_px(16), ui_px(14), ui_px(16), ui_px(14))
        root.setSpacing(ui_px(10))

        title = QLabel("Turn off Auto-Trader for all brokers?")
        title.setStyleSheet(f"font-size: {ui_px(14)}px; font-weight: 600; color: {tc['text']};")
        root.addWidget(title)

        armed = ", ".join(armed_brokers) if armed_brokers else "none"
        hint = QLabel(
            f"Currently armed: {armed}.\n\n"
            "Turn off all disarms every broker immediately. "
            "Change brokers opens the picker to add or remove individual brokers."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        root.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(ui_px(8))
        cancel_btn = QPushButton("Cancel")
        manage_btn = QPushButton("Change brokers…")
        off_btn = QPushButton("Turn off all")
        off_btn.setProperty("uiBtnKind", "primary")
        off_btn.setStyleSheet(action_btn_style("danger"))
        cancel_btn.clicked.connect(self.reject)
        manage_btn.clicked.connect(self._choose_manage)
        off_btn.clicked.connect(self._choose_off_all)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(manage_btn)
        btn_row.addWidget(off_btn)
        root.addLayout(btn_row)
        off_btn.setDefault(True)
        off_btn.setFocus()

    def _choose_off_all(self):
        self.choice = self.OFF_ALL
        self.accept()

    def _choose_manage(self):
        self.choice = self.MANAGE
        self.accept()


class FirstRunWizardDialog(QDialog):
    """3-step getting-started: risk posture → brokers → Discord."""

    def __init__(self, parent, dark_mode=True):
        super().__init__(parent)
        self.setWindowTitle("Getting Started — Market Advisor")
        self.setModal(True)
        self.setMinimumWidth(ui_px(500))
        self.setMinimumHeight(ui_px(420))
        self.result_action = None  # "finish" | "skip"
        self._app = parent
        self._dark = dark_mode
        tc = theme_colors(dark_mode)
        if dark_mode:
            self.setStyleSheet(
                f"""
                QDialog {{ background-color: #151820; color: {tc['text']}; }}
                QLabel {{ color: {tc['text']}; background: transparent; }}
                QRadioButton {{ color: {tc['text']}; spacing: {ui_px(8)}px; }}
                QLineEdit, QComboBox {{
                    background-color: #1A1D24; color: {tc['text']};
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(8)}px; min-height: {ui_px(24)}px;
                }}
                QPushButton {{
                    background-color: #22262E; color: {tc['text']};
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_BTN)}px;
                    padding: {ui_px(7)}px {ui_px(14)}px; min-height: {ui_px(28)}px;
                }}
                QPushButton:hover {{ background-color: #2A303A; border-color: #3A4150; }}
                """
            )

        from scoring import RISK_POSTURE_PROFILES, normalize_risk_posture

        root = QVBoxLayout(self)
        root.setContentsMargins(ui_px(18), ui_px(16), ui_px(18), ui_px(14))
        root.setSpacing(ui_px(10))

        self.step_lbl = QLabel("Step 1 of 3 — Risk posture")
        self.step_lbl.setStyleSheet(
            f"font-size: {ui_px(13)}px; font-weight: 600; color: {tc['accent']};"
        )
        root.addWidget(self.step_lbl)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # --- Step 1: posture ---
        page1 = QWidget()
        p1 = QVBoxLayout(page1)
        p1.setSpacing(ui_px(8))
        intro1 = QLabel(
            "How hard should the auto-trader press? You can change this anytime in Settings."
        )
        intro1.setWordWrap(True)
        intro1.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        p1.addWidget(intro1)
        self.posture_group = QButtonGroup(self)
        self._posture_radios = {}
        saved = normalize_risk_posture(
            (getattr(parent, "settings", {}) or {}).get("risk_posture", "balanced")
        )
        self.posture_hint = QLabel("")
        self.posture_hint.setWordWrap(True)
        self.posture_hint.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(11)}px;")
        for key in ("safer", "balanced", "aggressive", "growth"):
            prof = RISK_POSTURE_PROFILES[key]
            rb = QRadioButton(prof["label"])
            rb.setProperty("postureKey", key)
            self.posture_group.addButton(rb)
            self._posture_radios[key] = rb
            p1.addWidget(rb)
            if key == saved:
                rb.setChecked(True)
        self.posture_group.buttonClicked.connect(self._sync_posture_hint)
        p1.addWidget(self.posture_hint)
        p1.addStretch(1)
        self.stack.addWidget(page1)
        self._sync_posture_hint()

        # --- Step 2: brokers ---
        page2 = QWidget()
        p2 = QVBoxLayout(page2)
        p2.setSpacing(ui_px(8))
        intro2 = QLabel(
            "Connect at least one broker when you're ready. Login dialogs are the same as Settings — "
            "you can skip and connect later."
        )
        intro2.setWordWrap(True)
        intro2.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        p2.addWidget(intro2)
        for label, slot in (
            ("Robinhood…", "_open_robinhood_login_dialog"),
            ("Coinbase…", "_open_coinbase_login_dialog"),
            ("E*TRADE…", "_open_etrade_login_dialog"),
        ):
            row = QHBoxLayout()
            name_lbl = QLabel(label.replace("…", ""))
            name_lbl.setMinimumWidth(ui_px(100))
            row.addWidget(name_lbl)
            btn = QPushButton("Connect…")
            btn.setToolTip(f"Open {label} login")
            fn = getattr(parent, slot, None)
            if callable(fn):
                btn.clicked.connect(fn)
            row.addWidget(btn)
            row.addStretch(1)
            p2.addLayout(row)
        tip2 = QLabel("Status updates after connect appear on Settings → Brokers.")
        tip2.setWordWrap(True)
        tip2.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(11)}px;")
        p2.addWidget(tip2)
        p2.addStretch(1)
        self.stack.addWidget(page2)

        # --- Step 3: Discord ---
        page3 = QWidget()
        p3 = QVBoxLayout(page3)
        p3.setSpacing(ui_px(8))
        intro3 = QLabel(
            "Optional: Discord webhook for trade alerts and hourly heartbeats."
        )
        intro3.setWordWrap(True)
        intro3.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        p3.addWidget(intro3)
        p3.addWidget(QLabel("Webhook URL:"))
        self.wiz_webhook = QLineEdit(
            str((getattr(parent, "settings", {}) or {}).get("discord_webhook", "") or "")
        )
        self.wiz_webhook.setPlaceholderText("https://discord.com/api/webhooks/…")
        self.wiz_webhook.setEchoMode(QLineEdit.Password)
        p3.addWidget(self.wiz_webhook)
        show_wh = QCheckBox("Show URL")
        show_wh.toggled.connect(
            lambda on: self.wiz_webhook.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password
            )
        )
        p3.addWidget(show_wh)
        lvl_row = QHBoxLayout()
        lvl_row.addWidget(QLabel("Alerts:"))
        self.wiz_alert_combo = QComboBox()
        self.wiz_alert_combo.addItems([
            "All Alerts (Every Trade & Heartbeat)",
            "Important Only (Critical Alerts & Hourly Heartbeat)",
            "Disabled Completely",
        ])
        saved_lvl = (getattr(parent, "settings", {}) or {}).get(
            "discord_alert_level", "All Alerts (Every Trade & Heartbeat)"
        )
        idx = self.wiz_alert_combo.findText(saved_lvl)
        if idx >= 0:
            self.wiz_alert_combo.setCurrentIndex(idx)
        lvl_row.addWidget(self.wiz_alert_combo, 1)
        p3.addLayout(lvl_row)
        test_row = QHBoxLayout()
        test_btn = QPushButton("Test webhook")
        test_btn.clicked.connect(self._test_webhook)
        test_row.addWidget(test_btn)
        test_row.addStretch(1)
        p3.addLayout(test_row)
        p3.addStretch(1)
        self.stack.addWidget(page3)

        # Nav
        nav = QHBoxLayout()
        nav.setSpacing(ui_px(8))
        self.skip_btn = QPushButton("Skip for now")
        self.skip_btn.setToolTip("Close and don't show this again (reopen from Settings)")
        self.back_btn = QPushButton("Back")
        self.next_btn = QPushButton("Next")
        self.next_btn.setProperty("uiBtnKind", "primary")
        self.next_btn.setStyleSheet(action_btn_style("primary"))
        self.skip_btn.clicked.connect(self._on_skip)
        self.back_btn.clicked.connect(self._on_back)
        self.next_btn.clicked.connect(self._on_next)
        nav.addWidget(self.skip_btn)
        nav.addStretch(1)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        root.addLayout(nav)
        self._update_nav()

    def _sync_posture_hint(self, *_args):
        from scoring import RISK_POSTURE_PROFILES
        key = self.selected_posture()
        hint = RISK_POSTURE_PROFILES.get(key, {}).get("hint", "")
        self.posture_hint.setText(hint)

    def selected_posture(self):
        for key, rb in self._posture_radios.items():
            if rb.isChecked():
                return key
        return "balanced"

    def _update_nav(self):
        i = self.stack.currentIndex()
        n = self.stack.count()
        titles = (
            "Step 1 of 3 — Risk posture",
            "Step 2 of 3 — Connect brokers",
            "Step 3 of 3 — Discord alerts",
        )
        self.step_lbl.setText(titles[i] if i < len(titles) else f"Step {i + 1} of {n}")
        self.back_btn.setEnabled(i > 0)
        self.next_btn.setText("Finish" if i >= n - 1 else "Next")

    def _on_back(self):
        i = self.stack.currentIndex()
        if i > 0:
            self.stack.setCurrentIndex(i - 1)
            self._update_nav()

    def _on_next(self):
        i = self.stack.currentIndex()
        if i >= self.stack.count() - 1:
            self.result_action = "finish"
            self.accept()
            return
        self.stack.setCurrentIndex(i + 1)
        self._update_nav()

    def _on_skip(self):
        self.result_action = "skip"
        self.accept()

    def _test_webhook(self):
        url = self.wiz_webhook.text().strip()
        if not url:
            QMessageBox.information(self, "Discord", "Paste a webhook URL first.")
            return
        app = self._app
        if app is None or not hasattr(app, "send_discord_alert"):
            return
        prev = (getattr(app, "settings", {}) or {}).get("discord_webhook", "")
        app.settings["discord_webhook"] = url
        try:
            app.send_discord_alert(
                "Test message from Market Advisor Getting Started — webhook OK.",
                urgent=True,
                prefix="[HEARTBEAT]",
            )
            QMessageBox.information(self, "Discord", "Test sent (check your channel).")
        finally:
            # Keep wizard URL; don't wipe if user was editing
            app.settings["discord_webhook"] = url or prev


def _write_combo_arrow_png(path, fill_hex):
    """Tiny triangle PNG so QComboBox::down-arrow stays visible on Windows Fusion."""
    pm = QPixmap(14, 10)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(fill_hex))
    painter.setPen(Qt.NoPen)
    painter.drawPolygon(QPolygon([QPoint(1, 2), QPoint(13, 2), QPoint(7, 9)]))
    painter.end()
    pm.save(path, "PNG")


def combo_arrow_path(dark_mode):
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "_combo_arrow_dark.png" if dark_mode else "_combo_arrow_light.png")
    fill = "#E0E0E0" if dark_mode else "#212121"
    try:
        _write_combo_arrow_png(path, fill)
    except Exception as e:
        print(f"combo arrow write failed: {e}")
    return path.replace("\\", "/")


def _write_spin_arrow_png(path, fill_hex, pointing_up):
    """Tiny triangle PNG so QSpinBox up/down arrows stay visible on Windows Fusion."""
    pm = QPixmap(12, 8)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(fill_hex))
    painter.setPen(Qt.NoPen)
    if pointing_up:
        painter.drawPolygon(QPolygon([QPoint(1, 7), QPoint(11, 7), QPoint(6, 1)]))
    else:
        painter.drawPolygon(QPolygon([QPoint(1, 1), QPoint(11, 1), QPoint(6, 7)]))
    painter.end()
    pm.save(path, "PNG")


def spin_arrow_paths(dark_mode):
    """Return (up_arrow_url, down_arrow_url) for themed QSpinBox / QDoubleSpinBox."""
    base = os.path.dirname(os.path.abspath(__file__))
    suffix = "dark" if dark_mode else "light"
    up_path = os.path.join(base, f"_spin_arrow_up_{suffix}.png")
    down_path = os.path.join(base, f"_spin_arrow_down_{suffix}.png")
    fill = "#E0E0E0" if dark_mode else "#212121"
    try:
        _write_spin_arrow_png(up_path, fill, pointing_up=True)
        _write_spin_arrow_png(down_path, fill, pointing_up=False)
    except Exception as e:
        print(f"spin arrow write failed: {e}")
    return up_path.replace("\\", "/"), down_path.replace("\\", "/")


def format_quantity(value):
    try:
        val = float(value)
        if val == 0: return "0"
        formatted = f"{val:.10f}".rstrip('0')
        if formatted.endswith('.'): formatted = formatted[:-1]
        return formatted
    except (ValueError, TypeError):
        return "0"


class CompactScrollArea(QScrollArea):
    """Scroll area that does not force the main window taller than the screen."""

    def sizeHint(self):
        return QSize(720, 420)

    def minimumSizeHint(self):
        return QSize(480, 240)


class WorkingSpinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._base_size = 20
        self.apply_scale()
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.rotate)
        self.is_spinning = False

    def apply_scale(self):
        s = ui_px(self._base_size)
        self.setFixedSize(s, s)

    def start(self):
        self.is_spinning = True
        self.timer.start(50)
        self.update()

    def stop(self):
        self.is_spinning = False
        self.timer.stop()
        self.update()

    def rotate(self):
        self.angle = (self.angle + 30) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height())
        inset = max(2, side // 10)
        arc = max(8, side - inset * 2)
        if self.is_spinning:
            pen = QPen(QColor("#007ACC"), max(2, side // 8))
            painter.setPen(pen)
            painter.drawArc(inset, inset, arc, arc, -self.angle * 16, 270 * 16)
        else:
            pen = QPen(QColor("#2E7D32"), max(2, side // 8))
            painter.setPen(pen)
            d = max(4, side // 3)
            o = (side - d) // 2
            painter.drawEllipse(o, o, d, d)


class BotActivityAnimator(QWidget):
    """
    Small banner animation of a bot combing through tickers/files.
    Modes: rest | armed | scan | score | execute
    """
    MODES = ("rest", "armed", "scan", "score", "execute")

    def __init__(self, parent=None):
        super().__init__(parent)
        # Compact — banner sits above tabs; tall animator was crushing Home cards
        self._base_w, self._base_h = 88, 32
        self.apply_scale()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.mode = "rest"
        self.frame = 0
        self.accent = QColor(UI_ACCENT)
        self.dark = True
        self._tickers = ["BTC", "ETH", "SOL", "SPY", "QQQ", "AVAX", "LINK", "NVDA", "AAPL", "DOGE"]
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(90)

    def apply_scale(self):
        self.setFixedSize(ui_px(self._base_w), ui_px(self._base_h))
        self.update()

    def set_dark(self, dark):
        self.dark = bool(dark)
        self.update()

    def set_mode(self, mode, accent_hex=None):
        mode = (mode or "rest").lower()
        if mode not in self.MODES:
            mode = "armed"
        self.mode = mode
        if accent_hex:
            self.accent = QColor(accent_hex)
        elif mode == "scan":
            self.accent = QColor("#FFB300")
        elif mode == "score":
            self.accent = QColor("#26A69A")
        elif mode == "execute":
            self.accent = QColor("#EF5350")
        elif mode == "armed":
            self.accent = QColor(UI_ACCENT)
        else:
            self.accent = QColor("#757575")
        self.update()

    def set_mode_from_banner(self, text, accent_hex=None):
        t = (text or "").upper()
        if "REST" in t or "AWAITING" in t or "💤" in (text or ""):
            self.set_mode("rest", accent_hex)
        elif "EXECUT" in t or "💰" in (text or ""):
            self.set_mode("execute", accent_hex)
        elif "SCOR" in t or "📈" in (text or ""):
            self.set_mode("score", accent_hex)
        elif "SCAN" in t or "LOAD" in t or "🪙" in (text or "") or "🚀" in (text or "") or "🏢" in (text or "") or "📊" in (text or ""):
            self.set_mode("scan", accent_hex)
        elif "ARM" in t or "⚡" in (text or ""):
            self.set_mode("armed", accent_hex)
        else:
            self.set_mode("armed", accent_hex)

    def _tick(self):
        if not self.isVisible():
            return
        self.frame = (self.frame + 1) % 240
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        ink = QColor("#E0E0E0" if self.dark else "#212121")
        mute = QColor("#9E9E9E" if self.dark else "#757575")
        paper = QColor("#2A2A2A" if self.dark else "#F5F5F5")
        paper_line = QColor("#555555" if self.dark else "#BDBDBD")

        scroll = (self.frame * (3 if self.mode in ("scan", "score") else 1)) % 28
        for i in range(4):
            y = 6 + i * 9 - (scroll % 9)
            if y < 2 or y > h - 6:
                continue
            p.setBrush(paper)
            p.setPen(QPen(paper_line, 1))
            p.drawRoundedRect(58, int(y), 56, 8, 2, 2)
            tix = self._tickers[(self.frame // 8 + i) % len(self._tickers)]
            p.setPen(mute)
            p.drawText(62, int(y) + 7, tix)

        if self.mode in ("scan", "score", "execute"):
            beam_y = 8 + (self.frame * 2) % 28
            beam = QColor(self.accent)
            beam.setAlpha(70 if self.mode != "execute" else 110)
            p.fillRect(56, beam_y, 60, 6, beam)

        bob = int(math.sin(self.frame / 5.0) * (2 if self.mode != "rest" else 1))
        if self.mode == "rest":
            bob = int(math.sin(self.frame / 12.0))
        rx, ry = 10, 10 + bob

        p.setBrush(self.accent if self.mode != "rest" else mute)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(rx + 6, ry, 22, 16, 4, 4)
        p.setPen(QPen(self.accent if self.mode != "rest" else mute, 2))
        p.drawLine(rx + 17, ry, rx + 17, ry - 5)
        p.setBrush(QColor("#FFD54F") if self.mode == "execute" else (self.accent if self.mode != "rest" else mute))
        p.setPen(Qt.NoPen)
        p.drawEllipse(rx + 14, ry - 9, 6, 6)

        eye = QColor("#121212" if self.dark else "#FFFFFF")
        p.setBrush(eye)
        if self.mode == "rest":
            p.drawRect(rx + 10, ry + 7, 5, 2)
            p.drawRect(rx + 19, ry + 7, 5, 2)
        else:
            blink = (self.frame % 40) < 2
            if blink:
                p.drawRect(rx + 10, ry + 7, 5, 2)
                p.drawRect(rx + 19, ry + 7, 5, 2)
            else:
                p.drawEllipse(rx + 10, ry + 5, 5, 5)
                p.drawEllipse(rx + 19, ry + 5, 5, 5)

        p.setBrush(QColor(self.accent).darker(120) if self.mode != "rest" else mute)
        p.drawRoundedRect(rx + 4, ry + 17, 26, 14, 3, 3)
        p.setPen(QPen(ink if self.mode != "rest" else mute, 2))
        reach = 8 + int(math.sin(self.frame / 4.0) * 4) if self.mode in ("scan", "score", "execute") else 4
        p.drawLine(rx + 30, ry + 22, rx + 30 + reach, ry + 18)

        if self.mode == "rest":
            p.setPen(mute)
            z = "." * (1 + (self.frame // 10) % 3)
            p.drawText(rx + 34, ry + 4, "z" + z)


class BackgroundTask(QThread):
    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, target_func, *args, **kwargs):
        super().__init__()
        self.target_func = target_func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            res = self.target_func(*self.args, **self.kwargs)
            self.result_ready.emit(res)
        except Exception as e:
            try:
                import traceback
                detail = f"{e}\n{traceback.format_exc()}"
            except Exception:
                detail = str(e)
            self.error_occurred.emit(detail)


class MarketAdvisorGUI(QMainWindow):
    _launch_discord_finished = pyqtSignal(bool, str)
    _log_line_ready = pyqtSignal(str)
    # Worker thread → main: request SMS/2FA without blocking the event loop
    _rh_sms_prompt = pyqtSignal(str)
    # Monitor HTTP thread → main: arm/disarm request object
    _monitor_control_req = pyqtSignal(object)
    # Monitor HTTP thread → main: E*TRADE OAuth start/complete from companion
    _monitor_etrade_oauth_req = pyqtSignal(object)
    _monitor_advisor_req = pyqtSignal(object)
    _monitor_eod_req = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(window_title())
        self._fit_to_screen()
        set_ui_scale(compute_ui_scale(self.width(), self.height()))
        self._ui_scale = ui_scale()
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.timeout.connect(self._on_scale_timer)
        self._launch_discord_finished.connect(self._on_launch_discord_finished)
        self._log_line_ready.connect(self._append_log_line_ui)
        self._rh_sms_prompt.connect(self._on_rh_sms_prompt)
        self._monitor_control_req.connect(self._on_monitor_control_req)
        self._monitor_etrade_oauth_req.connect(self._on_monitor_etrade_oauth_req)
        self._monitor_advisor_req.connect(self._on_monitor_advisor_req)
        self._monitor_eod_req.connect(self._on_monitor_eod_req)
        self._rh_sms_event = threading.Event()
        self._rh_sms_code = ""
        self._rh_sms_dialog = None
        self._rh_login_in_flight = False
        self._cb_login_in_flight = False

        self.settings = load_settings()
        self.dark_mode = self.settings.get("dark_mode", False)
        self.paper_mode = self.settings.get("paper_mode", False)
        
        # Initialize Broker Adapters
        self.brokers = {
            "Robinhood": RobinhoodAdapter(),
            "Coinbase": CoinbaseAdapter(),
            "E*TRADE": ETradeAdapter(),
        }
        try:
            from scoring import register_regime_brokers
            register_regime_brokers(
                robinhood=self.brokers["Robinhood"],
                coinbase=self.brokers["Coinbase"],
                etrade=self.brokers["E*TRADE"],
            )
        except Exception:
            pass
        self.active_broker_name = "Robinhood"
        self.view_mode = "All"  # Dropdown: All | Robinhood | Coinbase | E*TRADE
        self.penny_tab_index = 3
        self.core_tab_index = 4
        self.ipo_tab_index = -1
        self._ipo_refresh_in_flight = False
        self._last_balance_totals = _blank_broker_map(lambda: {'p_val': 0.0, 'bp': 0.0})
        
        self.auto_trade_enabled = _blank_broker_map(False)
        self._panic_halted = False
        self.task_queue = []
        self.is_processing_queue = False
        self._cycle_broker = None  # Broker locked for the in-flight auto-trade cycle
        self._cycle_task = None  # CRYPTO / CORE / PENNY / PORTFOLIO for decision journal
        self._queue_started_at = None
        self._stall_alerted = False
        self._reconnect_cooldown = _blank_broker_map(0.0)
        self._reconnect_fail_streak = _blank_broker_map(0)
        self._reconnect_in_flight = _blank_broker_map(False)
        self._holdings_count_cache = _blank_broker_map(0)
        self._balance_bad_streak = _blank_broker_map(0)
        # Last non-glitch equity per broker (survives rejected $0 reads)
        self._last_trusted_equity = _blank_broker_map(None)
        # Log "disconnected" / unreliable-zero skip once per stretch (avoids ~60s spam)
        self._balance_disconnected_warned = _blank_broker_map(False)
        self._balance_zero_glitch_warned = _blank_broker_map(False)
        # Rate-limit identical cycle Discord alerts: {key: (last_sent_ts, suppressed_count)}
        self._cycle_error_discord_cooldown = {}
        # Manual OAuth / missing token — do not auto-reconnect-loop
        self._broker_manual_auth_needed = _blank_broker_map(False)
        self._reauth_nudge_sent = _blank_broker_map(False)  # Discord once per reauth stretch
        self._stop_repair_ticker_cooldown = {}  # "Broker:TICKER" -> last attempt ts
        self._stop_repair_skip_logged = set()  # unsupported asset keys logged once
        self._last_stop_repair_pass = 0.0
        self._heat_holdings_by_broker = _blank_broker_map(lambda: [])
        self._coach_tip_keys = set()  # one [COACH] tip per skip-reason per cycle
        self._scan_idle_since = {}  # f"{broker}:{engine}" -> ts of last raw BUY signal
        self._buy_skip_throttle = {}  # (broker, kind) -> last log ts (BP/rotate spam)
        self._scan_drop_throttle = {}  # (broker, engine, ticker) -> last log ts
        self._cost_unknown_logged = set()  # "Broker:TICKER" cost-basis unknown once
        self._cost_seeded_logged = set()  # "Broker:TICKER:source" seeded-once log
        self._sell_fail_backoff = {}  # (broker, ticker) -> {reason, ts} hopeless sell defer
        self._sell_fail_backoff_ttl_sec = 1800  # 30m TTL; also clears when fail reason changes
        self._frac_buy_defer_log = {}  # (broker, ticker, session) -> True once-per-session
        self._frac_stop_na_logged = set()  # Broker:TICKER fractional stop N/A logged once
        self._balances_refresh_in_flight = False
        self._last_idle_balance_refresh = 0.0
        self._startup_connect_finished = False
        self.cost_basis_cache = _blank_broker_map(lambda: {})
        self._cost_basis_persist_ts = 0.0
        self._journal_basis_rows = None  # lazy journal snapshot for VWAP seed
        self._journal_basis_rows_ts = 0.0
        self._scoring_state_loaded = False
        self._restore_cost_basis_cache()
        self._merge_seed_cost_basis()
        self.last_crypto_time = _blank_broker_map(0)
        self.last_penny_time = _blank_broker_map(0)
        self.last_core_time = _blank_broker_map(0)
        self.last_port_time = _blank_broker_map(0)
        
        self.last_heartbeat_time = time.time()
        self._last_heartbeat_slot = None
        self._pending_launch_checkin = False
        self._launch_checkin_failsafe_armed = False
        self._launch_checkin_sent = False
        self._launch_checkin_was_empty = False
        self._launch_checkin_in_flight = False
        self._launch_checkin_retry_scheduled = False
        self._launch_checkin_upgrade_pending = False
        self._balances_fetched_once = False
        self._launch_failsafe_waits = 0
        self.current_trading_day = datetime.now().date()
        self._sell_defer_log = {}  # (broker, ticker, reason) -> session label last logged
        self._frac_ext_ineligible = set()  # RH tickers rejected for ext-hours fractionals
        self._last_equity_session_label = None
        # Weekday RTH boundary wake-ups (once each per ET calendar day)
        self._session_wakeup_fired = {
            "day": None, "pre_open": False, "open": False, "pre_close": False,
        }
        
        self.trade_locks = {}
        self._portfolio_fingerprint = ""
        self.sandbox_cash = _blank_broker_map(10000.00)
        self.sandbox_holdings = _blank_broker_map(lambda: {})  # {ticker: {'shares':, 'cost':, 'type':}}
        
        # Track P&L Independently per broker
        self.session_starts = _blank_broker_map(None)
        self._restore_session_baselines()
        
        self.active_threads = []
        
        central_widget = QWidget()
        self.main_layout = QVBoxLayout(central_widget)
        m = ui_px(10)
        self.main_layout.setContentsMargins(m, m, m, m)
        self.main_layout.setSpacing(m)
        self.setCentralWidget(central_widget)

        self.build_persistent_top_bar()
        self.build_auto_trader_banner()

        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs, 1)  # take leftover height so banner can't crush Home
        
        self.setup_status_bar()
        
        # Home only for first paint — rest of tabs fill in on the next event-loop tick
        self.build_home_screen()

        self.director_timer = QTimer(self)
        self.director_timer.timeout.connect(self.director_tick)

        self._recent_log_lines = []
        self._full_log_lines = []
        self._activity_log_disk_writes = 0
        self._log_ui_append_count = 0
        self._monitor_banner = "Application starting…"
        self._trading_tabs_built = False
        self._monitor_start_gen = 0

        self.apply_theme()
        self.update_market_status()
        self.log_event("Application initialized. Verifying connections...")
        self.log_event(f"Version {APP_VERSION}" + (f" — {VERSION_NOTE}" if VERSION_NOTE else ""))
        self._setup_system_tray()  # tray visible immediately

        # Warm heavy libs early so first score/scan (and load_state) don't hitch
        def _warm():
            try:
                import scoring  # noqa: F401
                import yfinance  # noqa: F401
                import robin_stocks.robinhood  # noqa: F401
            except Exception:
                pass
        threading.Thread(target=_warm, daemon=True).start()

        # Disk log rotate after first paint — force-read of a large file is not paint-critical
        QTimer.singleShot(400, lambda: _rotate_activity_log_if_needed(force=True))
        # Remaining tabs + startup connect after first paint
        QTimer.singleShot(0, self._finish_ui_build)

    def _fit_to_screen(self):
        """Open at a usable size that fits the available desktop; allow shrinking.

        Typical default: 920–1200 × 600–780 (often 1200×780 on a 1080p desk).
        Fallback when no screen: 1120×720. Minimum: 760×520.
        """
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            # Leave room for taskbar / window chrome
            target_w = min(1200, max(920, avail.width() - 48))
            target_h = min(780, max(600, avail.height() - 64))
            self.resize(target_w, target_h)
            self.setMinimumSize(760, 520)
            frame = self.frameGeometry()
            frame.moveCenter(avail.center())
            self.move(frame.topLeft())
        else:
            self.resize(1120, 720)
            self.setMinimumSize(760, 520)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Coalesce Home reflow + scale with the existing debounce (startup show/fit storms)
        if hasattr(self, "_scale_timer"):
            self._scale_timer.start(90)

    def _on_scale_timer(self):
        new_scale = compute_ui_scale(self.width(), self.height())
        if abs(new_scale - getattr(self, "_ui_scale", 1.0)) < 0.025:
            self._update_home_wide_layout()
            return
        set_ui_scale(new_scale)
        self._ui_scale = ui_scale()
        self._apply_ui_scale()

    def _apply_ui_scale(self):
        """Re-apply fonts, padding, and min sizes for the current UI scale."""
        m = ui_px(10)
        if hasattr(self, "main_layout"):
            self.main_layout.setContentsMargins(m, m, m, m)
            self.main_layout.setSpacing(m)

        if hasattr(self, "_top_bar_layout"):
            self._top_bar_layout.setContentsMargins(ui_px(10), ui_px(6), ui_px(10), ui_px(6))
            self._top_bar_layout.setSpacing(ui_px(8))
        if hasattr(self, "_top_bar_metrics_layout"):
            self._top_bar_metrics_layout.setSpacing(ui_px(8))

        if hasattr(self, "broker_dropdown"):
            self.broker_dropdown.setFixedWidth(ui_px(110))
        if hasattr(self, "portfolio_val_lbl"):
            self.portfolio_val_lbl.setMinimumWidth(ui_px(168))
        if hasattr(self, "buying_power_lbl"):
            self.buying_power_lbl.setMinimumWidth(ui_px(198))
        if hasattr(self, "daily_profit_lbl"):
            self.daily_profit_lbl.setMinimumWidth(ui_px(148))
        for btn, mh, mw in (
            (getattr(self, "paper_mode_btn", None), 34, 100),
            (getattr(self, "dark_mode_btn", None), 34, 60),
            (getattr(self, "auto_trade_btn", None), 34, 128),
            (getattr(self, "halt_all_btn", None), 34, 58),
        ):
            if btn is not None:
                btn.setMinimumHeight(ui_px(mh))
                btn.setMinimumWidth(ui_px(mw))

        if hasattr(self, "_at_banner_layout"):
            self._at_banner_layout.setContentsMargins(ui_px(10), ui_px(4), ui_px(10), ui_px(4))
            self._at_banner_layout.setSpacing(ui_px(8))
        if hasattr(self, "at_status_frame"):
            self.at_status_frame.setMaximumHeight(ui_px(48))

        if hasattr(self, "_home_layout"):
            self._home_layout.setContentsMargins(ui_px(18), ui_px(12), ui_px(18), ui_px(12))
            self._home_layout.setSpacing(ui_px(10))
        if hasattr(self, "_heat_card_layout"):
            self._heat_card_layout.setContentsMargins(ui_px(12), ui_px(6), ui_px(12), ui_px(6))
            self._heat_card_layout.setSpacing(ui_px(2))
        if hasattr(self, "_cluster_card_layout"):
            self._cluster_card_layout.setContentsMargins(ui_px(12), ui_px(6), ui_px(12), ui_px(6))
            self._cluster_card_layout.setSpacing(ui_px(4))
        if hasattr(self, "_cluster_tip_card_layout"):
            self._cluster_tip_card_layout.setContentsMargins(ui_px(12), ui_px(6), ui_px(12), ui_px(6))
            self._cluster_tip_card_layout.setSpacing(ui_px(4))
        if hasattr(self, "home_cluster_rows_lay"):
            self.home_cluster_rows_lay.setSpacing(ui_px(4))
        if hasattr(self, "_master_card_layout"):
            self._master_card_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
            self._master_card_layout.setSpacing(ui_px(4))
        if hasattr(self, "_home_nw_brokers_lay"):
            self._home_nw_brokers_lay.setSpacing(ui_px(12))
        if hasattr(self, "_home_brokers_layout"):
            self._home_brokers_layout.setSpacing(ui_px(8))
        if hasattr(self, "_home_risk_row"):
            self._home_risk_row.setSpacing(ui_px(12))
        elif hasattr(self, "_home_cluster_row"):
            self._home_cluster_row.setSpacing(ui_px(12))
        if hasattr(self, "_home_risk_right_lay"):
            self._home_risk_right_lay.setSpacing(ui_px(6))
        elif hasattr(self, "_home_risk_layout"):
            self._home_risk_layout.setSpacing(ui_px(6))
        if hasattr(self, "_rh_card_layout"):
            self._rh_card_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
            self._rh_card_layout.setSpacing(ui_px(20))
        if hasattr(self, "_cb_card_layout"):
            self._cb_card_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
            self._cb_card_layout.setSpacing(ui_px(20))
        if hasattr(self, "_et_card_layout"):
            self._et_card_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
            self._et_card_layout.setSpacing(ui_px(20))
        if hasattr(self, "recent_trades_table"):
            self.recent_trades_table.setMinimumHeight(ui_px(140))
            polish_trades_header(self.recent_trades_table)
        self._update_home_wide_layout()

        if hasattr(self, "_status_layout"):
            self._status_layout.setContentsMargins(ui_px(8), ui_px(4), ui_px(12), ui_px(4))
            self._status_layout.setSpacing(ui_px(10))
        if hasattr(self, "status_bar"):
            self.status_bar.setMinimumHeight(ui_px(28))
        if hasattr(self, "status_text"):
            self.status_text.setMinimumWidth(ui_px(200))
        if hasattr(self, "market_status_lbl"):
            self.market_status_lbl.setMinimumWidth(ui_px(200))
        if hasattr(self, "spinner") and hasattr(self.spinner, "apply_scale"):
            self.spinner.apply_scale()
        if hasattr(self, "bot_animator") and hasattr(self.bot_animator, "apply_scale"):
            self.bot_animator.apply_scale()

        # Theme QSS + metric/home/top-bar styles all read current ui_px()
        self.apply_theme()
        self._restyle_scaled_widgets()
        self._refresh_home_balance_labels()
        if hasattr(self, "paper_mode_btn"):
            self.paper_mode_btn.setStyleSheet(
                top_bar_btn_style("#E65100") if self.paper_mode else top_bar_btn_style("#1B5E20")
            )
        if hasattr(self, "auto_trade_btn"):
            active = any(self.auto_trade_enabled.values()) if hasattr(self, "auto_trade_enabled") else False
            self.auto_trade_btn.setStyleSheet(
                top_bar_btn_style(UI_DANGER) if active else top_bar_btn_style("#424242")
            )
            if hasattr(self, "at_status_lbl"):
                self._reset_autotrader_banner_style()

    def _restyle_scaled_widgets(self):
        """Refresh one-off stylesheets that are not covered by global theme QSS."""
        for lbl in self.findChildren(QLabel):
            if lbl.objectName() == "sectionHeader":
                lbl.setStyleSheet(section_header_style())
            elif lbl.objectName() == "homeTitle":
                lbl.setStyleSheet(
                    f"font-size: {ui_px(20)}px; font-weight: 600; "
                    f"margin: {ui_px(4)}px 0 {ui_px(8)}px 0;"
                )
            elif lbl.objectName() == "homeBrokerMetric":
                lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
            elif lbl.objectName() == "settingsHint":
                lbl.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
            elif lbl.objectName() == "settingsVersion":
                note = f"  ·  {VERSION_NOTE}" if VERSION_NOTE else ""
                lbl.setText(f"{display_name()}{note}")
                lbl.setStyleSheet(
                    f"color: #6B7280; font-size: {ui_px(12)}px; margin-top: {ui_px(18)}px;"
                )
            elif lbl.objectName() in ("ipoDisclaimer", "ipoTip"):
                lbl.setStyleSheet(
                    f"color: {theme_colors(self.dark_mode)['muted']}; "
                    f"font-size: {ui_px(12 if lbl.objectName() == 'ipoDisclaimer' else 11)}px;"
                )
            elif lbl.objectName() == "ipoStatus":
                lbl.setStyleSheet(f"font-size: {ui_px(12)}px;")

        for btn in self.findChildren(QPushButton):
            kind = btn.property("uiBtnKind")
            if kind:
                extra = btn.property("uiBtnExtra") or ""
                btn.setStyleSheet(action_btn_style(str(kind)) + (str(extra) if extra else ""))

        for combo in self.findChildren(QComboBox):
            if combo.objectName() == "logFilterCombo":
                combo.setFixedWidth(ui_px(130))

        for btn in self.findChildren(QPushButton):
            name = btn.objectName()
            if name == "copyLogBtn":
                btn.setFixedWidth(ui_px(100))
            elif name == "saveLogBtn":
                btn.setFixedWidth(ui_px(120))
            elif name == "clearLogBtn":
                btn.setFixedWidth(ui_px(100))
            elif name == "ipoRefreshBtn":
                btn.setFixedWidth(ui_px(130))
            elif name == "ipoYahooBtn":
                btn.setFixedWidth(ui_px(160))

    def _finish_ui_build(self):
        """Deferred scanner/portfolio/settings tabs — window + tray already visible."""
        if getattr(self, "_trading_tabs_built", False):
            return
        try:
            self.build_portfolio_screen()

            self.scanners_tabs = QTabWidget()
            self.scanners_tabs.setObjectName("scannersTabs")
            self.scanners_tabs.addTab(self.build_crypto_screen(), "Crypto")
            self.scanners_tabs.addTab(self.build_penny_screen(), "Breakouts")
            self.scanners_tabs.addTab(self.build_core_screen(), "Core")
            self.scanners_tabs.addTab(self.build_signal_screen(), "Signal")
            self.scanners_tab_index = self.tabs.addTab(self.scanners_tabs, "Scanners")
            self.penny_inner_index = 1
            self.core_inner_index = 2
            self.signal_inner_index = 3
            self._wire_scanner_signal_sources()

            self.build_ipo_screen()

            self.journal_tabs = QTabWidget()
            self.journal_tabs.setObjectName("journalTabs")
            self.journal_tabs.addTab(self.build_activity_log_screen(), "Activity")
            self.journal_tabs.addTab(self.build_execution_screen(), "Execution")
            self.journal_tabs.addTab(self.build_reports_screen(), "Reports")
            self.tabs.addTab(self.journal_tabs, "Journal")

            self.build_settings_screen()

            self.penny_tab_index = -1  # equity scanners live inside Scanners
            self.core_tab_index = -1
            self.ipo_tab_index = -1
            for i in range(self.tabs.count()):
                title = self.tabs.tabText(i)
                if title == "IPOs":
                    self.ipo_tab_index = i
                elif title == "Scanners":
                    self.scanners_tab_index = i

            self._apply_view_mode_tabs()
            self._trading_tabs_built = True
            # Yield one tick so Home can paint before polish / monitor / broker connect
            QTimer.singleShot(0, self._finish_ui_build_phase2)
        except Exception as e:
            import traceback

            self._trading_tabs_built = False
            tb = traceback.format_exc()
            self.log_event(f"Deferred UI build failed: {e}\n{tb}")
            QMessageBox.critical(
                self,
                "UI build error",
                f"Could not finish loading tabs (Settings/Scanners may be missing).\n\n{e}\n\n"
                "See Activity log for details. Restart the app to retry.",
            )

    def _finish_ui_build_phase2(self):
        """Light polish + startup connect after deferred tabs exist (avoids theme/monitor hitch)."""
        # Deferred tabs already built with current ui_px; inherit window QSS — skip full apply_theme
        self._restyle_scaled_widgets()
        self._update_home_wide_layout()
        try:
            self._refresh_home_desk_radar()
        except Exception:
            pass
        QTimer.singleShot(0, lambda: self.director_timer.start(1000))
        # IPO calendar: first load shortly after UI settles; then every few hours
        QTimer.singleShot(8000, lambda: self.refresh_ipo_calendar(force=False))
        self._ipo_auto_timer = QTimer(self)
        self._ipo_auto_timer.timeout.connect(lambda: self.refresh_ipo_calendar(force=False))
        self._ipo_auto_timer.start(3 * 3600 * 1000)
        self._post_show_init()

    def _post_show_init(self):
        """Runs right after first event-loop tick so the window appears sooner."""
        try:
            from scoring import load_state
            if load_state():
                self.log_event("Restored TTP/cooldown memory from disk")
                self._scoring_state_loaded = True
        except Exception as e:
            self.log_event(f"scoring load_state failed: {e}")
        # Monitor bind (esp. LAN 0.0.0.0 + TLS) is ~1s — off UI thread via _start_web_monitor
        self._start_web_monitor()
        # Arm launch Discord early so a hung broker login cannot skip it
        self._pending_launch_checkin = True
        self._launch_checkin_failsafe_armed = True
        self._launch_checkin_sent = False
        self._launch_checkin_upgrade_pending = False
        self._balances_fetched_once = False
        self._launch_failsafe_waits = 0
        self._launch_zero_balance_retry = False
        QTimer.singleShot(50, self.run_startup_sequence)
        # Prefer waiting for balances; failsafe only sends $0 as a last resort
        QTimer.singleShot(12000, self._launch_checkin_failsafe)
        # First-run wizard after window has settled (Skip/Cancel never blocks trading)
        QTimer.singleShot(1200, self._maybe_show_first_run_wizard)

    @property
    def current_broker(self):
        return self.brokers.get(self.active_broker_name, self.brokers["Robinhood"])

    @property
    def cycle_broker_name(self):
        """Broker for the current auto-cycle (falls back to dropdown selection for manual trades)."""
        return self._cycle_broker or self.active_broker_name

    @property
    def cycle_broker(self):
        return self.brokers.get(self.cycle_broker_name, self.brokers["Robinhood"])

    # ---------------------------------------------------------
    #  SYSTEM TRAY (Sonarr/Radarr-style background + restore)
    # ---------------------------------------------------------
    def _make_app_icon(self):
        """Load Market Advisor brand icon; fall back to a drawn candle mark."""
        base = os.path.dirname(os.path.abspath(__file__))
        for name in ("app_icon.ico", "app_icon.png"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                icon = QIcon(path)
                if not icon.isNull():
                    return icon
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#0D3B2E"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(2, 2, 60, 60, 14, 14)
        # trend
        p.setPen(QPen(QColor("#A5D6A7"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        pts = [QPoint(10, 48), QPoint(22, 40), QPoint(32, 44), QPoint(44, 26), QPoint(54, 18)]
        for i in range(len(pts) - 1):
            p.drawLine(pts[i], pts[i + 1])
        # mini candles
        p.setPen(Qt.NoPen)
        for cx, top, bot, color in ((20, 30, 42, "#F8FAF9"), (32, 22, 38, "#1F8A70"), (44, 14, 32, "#F8FAF9")):
            p.setBrush(QColor(color))
            p.drawRoundedRect(cx - 4, top, 8, bot - top, 1, 1)
            p.setPen(QPen(QColor(color), 1))
            p.drawLine(cx, top - 4, cx, bot + 4)
            p.setPen(Qt.NoPen)
        p.end()
        return QIcon(pm)

    def _setup_system_tray(self):
        self._force_quit = False
        self._tray_tip_shown = False
        self.app_icon = self._make_app_icon()
        self.setWindowIcon(self.app_icon)

        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            self._tray_menu = None
            self.log_event("System tray unavailable — close will exit the app.")
            return

        self.tray_icon = QSystemTrayIcon(self.app_icon, self)
        self.tray_icon.setToolTip(display_name())

        # Keep a strong, parented menu ref — unparented QMenu can GC on Windows
        # and right-click then shows nothing.
        self._tray_menu = QMenu(self)
        show_act = QAction(f"Open {APP_NAME}", self)
        show_act.triggered.connect(self.show_from_tray)
        self._tray_menu.addAction(show_act)
        self._tray_menu.addSeparator()
        quit_act = QAction("Exit", self)
        quit_act.triggered.connect(self.quit_from_tray)
        self._tray_menu.addAction(quit_act)

        self.tray_icon.setContextMenu(self._tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        # Left / double-click restores; Context is a fallback if OS ignores setContextMenu
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_from_tray()
        elif reason == QSystemTrayIcon.Context and getattr(self, "_tray_menu", None) is not None:
            self._tray_menu.popup(QCursor.pos())

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self):
        """Hard exit from tray — skip minimize-to-tray prompt."""
        self._force_quit = True
        # Unblock any RH worker waiting on SMS/2FA so threads can wind down
        try:
            self._rh_sms_code = ""
            if getattr(self, "_rh_sms_event", None) is not None:
                self._rh_sms_event.set()
            dlg = getattr(self, "_rh_sms_dialog", None)
            if dlg is not None:
                dlg.reject()
        except Exception:
            pass
        try:
            monitor.stop_monitor()
        except Exception:
            pass
        try:
            from scoring import flush_state
            flush_state()
        except Exception:
            pass
        if self.tray_icon:
            self.tray_icon.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        # X / Alt+F4 → ask: tray vs quit (tray keeps auto-trader + web monitor alive)
        if self.tray_icon and not self._force_quit:
            box = QMessageBox(self)
            box.setWindowTitle(APP_NAME)
            box.setIcon(QMessageBox.Question)
            box.setText(f"Close {APP_NAME}?")
            box.setInformativeText(
                "Minimize to tray keeps the auto-trader and web monitor running in the background.\n"
                "Quit fully stops the app."
            )
            tray_btn = box.addButton("Minimize to tray", QMessageBox.AcceptRole)
            quit_btn = box.addButton("Quit app", QMessageBox.DestructiveRole)
            cancel_btn = box.addButton("Cancel", QMessageBox.RejectRole)
            box.setDefaultButton(tray_btn)
            box.exec_()
            clicked = box.clickedButton()

            if clicked == cancel_btn or clicked is None:
                event.ignore()
                return

            if clicked == tray_btn:
                event.ignore()
                self.hide()
                if not self._tray_tip_shown:
                    self._tray_tip_shown = True
                    self.tray_icon.showMessage(
                        display_name(),
                        "Still running in the tray. Double-click the icon to open, or right-click → Quit.",
                        QSystemTrayIcon.Information,
                        4000,
                    )
                return

            # Quit app — fall through to full shutdown
            self._force_quit = True

        # Unblock RH worker waiting on SMS if we're actually exiting
        try:
            self._rh_sms_code = ""
            if getattr(self, "_rh_sms_event", None) is not None:
                self._rh_sms_event.set()
            dlg = getattr(self, "_rh_sms_dialog", None)
            if dlg is not None:
                dlg.reject()
        except Exception:
            pass

        try:
            monitor.stop_monitor()
        except Exception:
            pass
        try:
            from scoring import flush_state
            flush_state()
        except Exception:
            pass
        if self.tray_icon:
            self.tray_icon.hide()
        event.accept()
        QApplication.instance().quit()

    # ---------------------------------------------------------
    #  ROBINHOOD 2FA — worker waits; main thread shows non-blocking dialog
    # ---------------------------------------------------------
    def _worker_rh_input_prompt(self, prompt):
        """
        Called from the RH login worker thread (via builtins.input patch).
        Must not touch Qt widgets — emit to main and wait on a threading.Event.
        """
        self._rh_sms_code = ""
        self._rh_sms_event.clear()
        self._rh_sms_prompt.emit(str(prompt or "Enter the verification code"))
        if not self._rh_sms_event.wait(timeout=600):
            return ""
        return self._rh_sms_code or ""

    def _on_rh_sms_prompt(self, prompt):
        """Main thread: open SMS dialog with open() — never exec_/getText (keeps tray alive)."""
        self._set_broker_status(
            "Robinhood",
            "🟡 Waiting for Robinhood 2FA — check phone/SMS or authenticator…",
            "color: #FFD54F; font-weight: bold;",
        )
        self.set_working_state(
            True, "Waiting for Robinhood 2FA — check phone/SMS or authenticator…"
        )
        self.log_event(
            "Robinhood: waiting for 2FA — check your phone/SMS or authenticator app…"
        )

        old = getattr(self, "_rh_sms_dialog", None)
        if old is not None:
            try:
                old.finished.disconnect()
            except Exception:
                pass
            try:
                old.reject()
            except Exception:
                pass
            self._rh_sms_dialog = None

        dlg = QInputDialog(self)
        dlg.setWindowTitle("Robinhood 2FA")
        dlg.setLabelText(
            f"{prompt}\n\n"
            "Check your phone for Robinhood's SMS or authenticator code, then enter it here:"
        )
        dlg.setInputMode(QInputDialog.TextInput)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setModal(True)

        def _finished(result):
            try:
                if result == QDialog.Accepted:
                    self._rh_sms_code = (dlg.textValue() or "").strip()
                else:
                    self._rh_sms_code = ""
            except Exception:
                self._rh_sms_code = ""
            self._rh_sms_event.set()
            if getattr(self, "_rh_sms_dialog", None) is dlg:
                self._rh_sms_dialog = None
            dlg.deleteLater()

        dlg.finished.connect(_finished)
        self._rh_sms_dialog = dlg
        dlg.open()  # non-blocking; Qt event loop (tray Exit) keeps running

    # ---------------------------------------------------------
    #  SYNCHRONIZATION & DISCORD
    # ---------------------------------------------------------
    def is_locked(self, ticker, is_crypto=False):
        key = f"{self.cycle_broker_name}:{ticker}"
        if key not in self.trade_locks:
            return False
        crypto_flag = bool(is_crypto) or bool(self.trade_locks.get(f"{key}:crypto"))
        try:
            from scoring import trade_lock_seconds
            lock_sec = trade_lock_seconds(is_crypto=crypto_flag)
        except Exception:
            lock_sec = 600 if crypto_flag else 300
        if time.time() - self.trade_locks[key] < lock_sec:
            return True
        del self.trade_locks[key]
        self.trade_locks.pop(f"{key}:crypto", None)
        return False

    def set_lock(self, ticker, is_crypto=False):
        key = f"{self.cycle_broker_name}:{ticker}"
        self.trade_locks[key] = time.time()
        if is_crypto:
            self.trade_locks[f"{key}:crypto"] = 1
        else:
            self.trade_locks.pop(f"{key}:crypto", None)

    def _reset_day_pnl_baseline(self):
        """Re-anchor Day P&L to current portfolio values (survives via settings)."""
        totals = getattr(self, "_last_balance_totals", {}) or {}
        if not totals:
            QMessageBox.information(
                self, "Reset Day P&L",
                "Connect brokers and refresh balances first.",
            )
            return
        updated = []
        for name in BROKER_NAMES:
            p_val = float(totals.get(name, {}).get("p_val", 0.0) or 0.0)
            if p_val > 0:
                self.session_starts[name] = p_val
                updated.append(f"{name} {format_currency(p_val)}")
        if not updated:
            QMessageBox.information(
                self, "Reset Day P&L",
                "No positive portfolio values available to anchor.",
            )
            return
        self._persist_session_baselines()
        self.log_event(
            "📊 Day P&L baseline reset to current portfolio: " + "; ".join(updated)
        )
        self._refresh_home_balance_labels()
        if hasattr(self, "_refresh_portfolio_heat"):
            self._refresh_portfolio_heat()
        self.publish_monitor_status()

    def _persist_session_baselines(self):
        """Save day-start equity so Day P&L survives app restarts."""
        self.settings["pnl_baseline_date"] = str(datetime.now().date())
        self.settings["pnl_baseline_rh"] = self.session_starts.get("Robinhood")
        self.settings["pnl_baseline_cb"] = self.session_starts.get("Coinbase")
        self.settings["pnl_baseline_et"] = self.session_starts.get("E*TRADE")
        save_settings(self.settings)

    def _restore_cost_basis_cache(self):
        """Load last-known avg costs so crypto bags can trail-ride after restart."""
        try:
            import cost_basis as cb_mod
            raw = self.settings.get("cost_basis_cache") or {}
            restored = cb_mod.normalize_cache_map(raw)
            for broker, tickers in restored.items():
                bucket = self.cost_basis_cache.setdefault(broker, {})
                for t, c in tickers.items():
                    if c > 0 and float(bucket.get(t) or 0) <= 0:
                        bucket[t] = float(c)
        except Exception:
            pass

    def _merge_seed_cost_basis(self):
        """Optional seed.json → cost_basis_cache (Settings paste is preferred long-term)."""
        try:
            import cost_basis as cb_mod
            seeds = cb_mod.load_seed_file()
            for broker, tickers in (seeds or {}).items():
                bucket = self.cost_basis_cache.setdefault(broker, {})
                for t, c in tickers.items():
                    if float(c or 0) > 0 and float(bucket.get(t) or 0) <= 0:
                        bucket[t] = float(c)
        except Exception:
            pass

    def _persist_cost_basis_cache(self, *, force: bool = False):
        """Throttle-write last-known cost basis into settings (survives restart)."""
        now = time.time()
        last = float(getattr(self, "_cost_basis_persist_ts", 0.0) or 0.0)
        if not force and (now - last) < 20.0:
            return
        try:
            import cost_basis as cb_mod
            payload = cb_mod.cache_to_persistable(self.cost_basis_cache)
            self.settings["cost_basis_cache"] = payload
            save_settings(self.settings)
            self._cost_basis_persist_ts = now
        except Exception:
            pass

    def _journal_rows_for_basis(self, *, max_age_sec: float = 90.0):
        """Cached journal snapshot for inventory VWAP seeding."""
        now = time.time()
        rows = getattr(self, "_journal_basis_rows", None)
        ts = float(getattr(self, "_journal_basis_rows_ts", 0.0) or 0.0)
        if rows is not None and (now - ts) < max_age_sec:
            return rows
        try:
            rows = journal.read_since_days(days=-1, limit=8000)
        except Exception:
            rows = []
        self._journal_basis_rows = rows
        self._journal_basis_rows_ts = now
        return rows

    def _restore_session_baselines(self):
        today = str(datetime.now().date())
        if self.settings.get("pnl_baseline_date") != today:
            return
        rh = self.settings.get("pnl_baseline_rh")
        cb = self.settings.get("pnl_baseline_cb")
        et = self.settings.get("pnl_baseline_et")
        if isinstance(rh, (int, float)) and rh > 0:
            self.session_starts["Robinhood"] = float(rh)
        if isinstance(cb, (int, float)) and cb > 0:
            self.session_starts["Coinbase"] = float(cb)
        if isinstance(et, (int, float)) and et > 0:
            self.session_starts["E*TRADE"] = float(et)
        # Mirror into last-trusted so Home keep-paths don't paint $0 over a known baseline
        if not hasattr(self, "_last_trusted_equity"):
            self._last_trusted_equity = _blank_broker_map(None)
        for name in BROKER_NAMES:
            start = self.session_starts.get(name)
            try:
                if start is not None and float(start) > NEAR_ZERO_EQUITY:
                    if not self._last_trusted_equity.get(name):
                        self._last_trusted_equity[name] = float(start)
            except (TypeError, ValueError):
                pass

    def _broker_supports(self, broker_name, attr):
        b = self.brokers.get(broker_name)
        return bool(b and getattr(b, attr, False))

    def _iter_broker_names(self):
        return list(self.brokers.keys())

    # ---------------------------------------------------------
    #  PAPER TRADING ENGINE (routes here whenever self.paper_mode is True,
    #  so Auto-Trader / manual execution never touches the real broker API)
    # ---------------------------------------------------------
    def get_broker_balances(self, broker_name=None):
        """Returns (portfolio_value, buying_power) for either the sandbox or the real broker."""
        broker_name = broker_name or self.cycle_broker_name
        if self.paper_mode:
            cash = self.sandbox_cash.get(broker_name, 10000.0)
            holdings_val = 0.0
            for ticker, pos in self.sandbox_holdings.get(broker_name, {}).items():
                price = self.brokers[broker_name].get_live_price(ticker)
                holdings_val += pos['shares'] * price
            return (cash + holdings_val), cash
        broker = self.brokers.get(broker_name)
        if not broker or not broker.is_connected:
            # Prefer last-good cache over inventing a $0 wipe for sizing / UI
            cached = (getattr(self, "_last_balance_totals", {}) or {}).get(broker_name) or {}
            return float(cached.get("p_val") or 0.0), float(cached.get("bp") or 0.0)
        try:
            return broker.get_account_balances()
        except Exception:
            cached = (getattr(self, "_last_balance_totals", {}) or {}).get(broker_name) or {}
            if float(cached.get("p_val") or 0.0) > 0 or float(cached.get("bp") or 0.0) > 0:
                return float(cached.get("p_val") or 0.0), float(cached.get("bp") or 0.0)
            raise

    def _locked_capital_value(self, broker_name=None):
        """OTC/dust/no-quote notional for one broker (cached from Home refresh when possible)."""
        broker_name = broker_name or self.cycle_broker_name
        by_b = getattr(self, "_last_locked_by_broker", None) or {}
        if broker_name in by_b:
            val, _cnt = _auto_cycle.locked_broker_entry(by_b.get(broker_name))
            return val
        holdings = []
        for a in getattr(self, "_last_assets_snapshot", None) or []:
            if not isinstance(a, dict):
                continue
            if str(a.get("broker") or "") == broker_name:
                holdings.append(a)
        if not holdings:
            try:
                for a in self.get_broker_holdings(broker_name) or []:
                    row = dict(a)
                    row.setdefault("broker", broker_name)
                    holdings.append(row)
            except Exception:
                pass
        return _auto_cycle.locked_value_from_holdings(holdings)

    def get_effective_balances(self, broker_name=None):
        """(effective_equity, buying_power, locked_value) — equity net of locked bags."""
        broker_name = broker_name or self.cycle_broker_name
        p_val, bp = self.get_broker_balances(broker_name)
        locked = self._locked_capital_value(broker_name)
        eff = _auto_cycle.effective_book_equity(p_val, locked)
        return eff, bp, locked

    def get_broker_holdings(self, broker_name=None):
        """Returns a list of holding dicts for either the sandbox or the real broker."""
        broker_name = broker_name or self.cycle_broker_name
        if self.paper_mode:
            assets = []
            for ticker, pos in self.sandbox_holdings.get(broker_name, {}).items():
                if pos['shares'] > 0:
                    assets.append({'ticker': ticker, 'shares': pos['shares'], 'cost': pos['cost'], 'type': pos['type']})
            return assets
        broker = self.brokers.get(broker_name)
        if not broker or not broker.is_connected:
            return []
        assets = broker.get_current_holdings()
        # E*TRADE historically returned {SYM: {...}}; RH/CB return list[dict].
        if isinstance(assets, dict):
            normalized = []
            for sym, pos in assets.items():
                if not isinstance(pos, dict):
                    continue
                row = dict(pos)
                row.setdefault("ticker", str(sym).upper())
                normalized.append(row)
            assets = normalized
        elif not isinstance(assets, list):
            assets = list(assets) if assets else []
        # Prefer sane broker avg (RH cost_basis / CB portfolio breakdown); else
        # journal VWAP → tracked → last-known. Never invent live mark as cost.
        import cost_basis as cb_mod
        cache = self.cost_basis_cache.setdefault(broker_name, {})
        unknown_log = getattr(self, "_cost_unknown_logged", None)
        if unknown_log is None:
            self._cost_unknown_logged = set()
            unknown_log = self._cost_unknown_logged
        seeded_log = getattr(self, "_cost_seeded_logged", None)
        if seeded_log is None:
            self._cost_seeded_logged = set()
            seeded_log = self._cost_seeded_logged
        journal_rows = None
        cache_dirty = False
        for a in assets:
            if not isinstance(a, dict):
                continue
            ticker = a.get("ticker")
            if not ticker:
                continue
            tu = cb_mod.normalize_ticker(ticker)
            a["ticker"] = tu
            try:
                cost = float(a.get("cost") or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            cached = cb_mod.cache_lookup(self.cost_basis_cache, broker_name, tu)
            try:
                mark = float(a.get("price") or a.get("mark") or a.get("live_price") or 0.0)
            except (TypeError, ValueError):
                mark = 0.0
            # Lazy journal VWAP only when broker+cache can't supply a sane basis
            jvwap = 0.0
            if cb_mod.usable_cost(cost, mark) <= 0 and cb_mod.usable_cost(cached, mark) <= 0:
                if journal_rows is None:
                    journal_rows = self._journal_rows_for_basis()
                try:
                    jvwap = float(
                        cb_mod.inventory_vwap_from_journal(journal_rows, broker_name, tu) or 0.0
                    )
                except Exception:
                    jvwap = 0.0
            resolved, source = cb_mod.resolve_holding_cost(
                broker_cost=cost,
                tracked_cache=cached,
                journal_vwap=jvwap,
                last_known=cached,  # persisted last-known already merged into cache
                mark=mark,
            )
            if resolved > 0:
                a["cost"] = resolved
                cost = resolved
                prev = float(cache.get(tu) or 0.0)
                cache[tu] = resolved
                if abs(prev - resolved) > max(1e-8, resolved * 1e-6):
                    cache_dirty = True
                if source in ("broker", "journal_vwap", "tracked", "last_known"):
                    skey = f"{broker_name}:{tu}:{source}"
                    if skey not in seeded_log:
                        seeded_log.add(skey)
                        try:
                            self.log_event(
                                f"[{broker_name}] Cost basis seeded for {tu} via {source} "
                                f"@ {format_currency(resolved)} — TTP/ROI unlocked"
                            )
                        except Exception:
                            pass
            else:
                a["cost"] = 0.0
                cost = 0.0
            is_crypto = (
                "crypto" in str(a.get("type") or "").lower()
                or tu in KNOWN_CRYPTOS
                or broker_name == "Coinbase"
            )
            if cost <= 0 and is_crypto:
                ukey = f"{broker_name}:{tu}"
                if ukey not in unknown_log:
                    unknown_log.add(ukey)
                    try:
                        self.log_event(
                            f"[{broker_name}] Cost basis unknown for {tu} — "
                            f"TTP/scale-in/ROI gated until avg cost is available "
                            f"(broker avg / journal VWAP / tracked / last-known). "
                            f"Paste avg from the broker app in Settings → Cost basis "
                            f"(e.g. {broker_name}:{tu}=<avg>)"
                        )
                    except Exception:
                        pass
        if cache_dirty:
            try:
                self._persist_cost_basis_cache(force=True)
            except Exception:
                pass
        return [a for a in assets if isinstance(a, dict) and a.get("ticker")]

    def _record_buy_cost(self, broker_name, ticker, price, shares_bought):
        if shares_bought <= 0 or price <= 0:
            return
        cache = self.cost_basis_cache.setdefault(broker_name, {})
        prev_cost = cache.get(ticker, 0.0)
        # Approximate prior shares from sandbox/live holdings if available
        prior_shares = 0.0
        if self.paper_mode:
            prior_shares = self.sandbox_holdings.get(broker_name, {}).get(ticker, {}).get('shares', 0.0) - shares_bought
            prior_shares = max(0.0, prior_shares)
        if prior_shares > 0 and prev_cost > 0:
            cache[ticker] = ((prior_shares * prev_cost) + (shares_bought * price)) / (prior_shares + shares_bought)
        else:
            cache[ticker] = price
        tu = str(ticker).upper()
        if tu != ticker:
            cache[tu] = cache[ticker]
        try:
            self._persist_cost_basis_cache(force=False)
        except Exception:
            pass
        # Invalidate journal VWAP snapshot so next holdings pull sees this buy
        self._journal_basis_rows = None
        self._journal_basis_rows_ts = 0.0

    def _journal_fill(
        self,
        side,
        ticker,
        asset_type,
        price,
        status,
        dollars=None,
        qty=None,
        order_id=None,
        reason="",
        score=None,
        rotate_for=None,
        rotate_from=None,
        quote_price=None,
        limit_price=None,
        fill_price=None,
        offset_pct=None,
        fee_paid=None,
    ):
        confirmed = ("Filled" in status) or ("[PAPER]" in status)
        if "Pending" in status:
            confirmed = False
        broker_name = self.cycle_broker_name
        broker = self.brokers.get(broker_name)
        broker_id = getattr(broker, "broker_id", None) or str(broker_name).upper()
        # Prefer explicit fee_paid; else last fill fee stashed by confirm_order
        if fee_paid is None and broker is not None:
            try:
                fee_paid = getattr(broker, "_last_fill_fee", None)
                if hasattr(broker, "_last_fill_fee"):
                    broker._last_fill_fee = None
            except Exception:
                fee_paid = None
        fee_key = ""
        fee_est = None
        try:
            from scoring import fee_profile_key, estimate_fee_dollars
            fee_key = fee_profile_key(broker_id, ticker, asset_type)
            notion = dollars
            if notion is None and price and qty:
                notion = float(price) * float(qty)
            if notion:
                fee_est = estimate_fee_dollars(
                    notion, broker_id, ticker, asset_type, round_trip=False
                )
        except Exception:
            fee_key = str(broker_id or "")
        # Quote / limit / fill for slippage feedback
        try:
            q = float(quote_price) if quote_price is not None else float(price or 0)
        except (TypeError, ValueError):
            q = float(price or 0) if price else 0.0
        try:
            if limit_price is not None:
                lim = float(limit_price)
            elif offset_pct is not None and q > 0:
                off = float(offset_pct)
                if str(side).upper() == "BUY":
                    lim = q * (1.0 + off)
                else:
                    lim = q * (1.0 - off)
            else:
                lim = None
        except (TypeError, ValueError):
            lim = None
        try:
            if fill_price is not None:
                fp = float(fill_price)
            elif dollars and qty and float(qty) > 0:
                fp = float(dollars) / float(qty)
            else:
                fp = float(price or 0) if price else 0.0
        except (TypeError, ValueError):
            fp = float(price or 0) if price else 0.0
        slip_bps = None
        try:
            from scoring import compute_slippage_bps, note_fill_slippage
            slip_bps = compute_slippage_bps(side, q, fp)
            if confirmed and slip_bps is not None:
                fb_note = note_fill_slippage(slip_bps)
                if fb_note:
                    try:
                        self.log_event(f"[Exec] {fb_note}")
                    except Exception:
                        pass
        except Exception:
            slip_bps = None
        entry = {
            "broker": broker_name,
            "side": side,
            "ticker": ticker,
            "asset_type": asset_type,
            "price": price,
            "quote_price": round(q, 6) if q else None,
            "limit_price": round(lim, 6) if lim else None,
            "fill_price": round(fp, 6) if fp else None,
            "dollars": dollars,
            "qty": qty,
            "status": status,
            "order_id": order_id,
            "confirmed": confirmed,
            "paper": self.paper_mode,
            "fee_profile": fee_key,
            "reason": str(reason or ""),
        }
        if slip_bps is not None:
            entry["slippage_bps"] = round(float(slip_bps), 2)
        if fee_est is not None:
            entry["fee_est"] = round(float(fee_est), 4)
        # Prefer net-of-cost metrics on sells (FinRL reward = Δ equity − friction)
        if str(side).upper() == "SELL":
            try:
                from scoring import net_roi_after_fees, estimate_round_trip_fee_pct
                gross = self._sell_roi(broker_name, ticker, fp or price)
                if gross is not None:
                    entry["roi_gross"] = round(float(gross), 6)
                    net = net_roi_after_fees(gross, broker_id, ticker, asset_type)
                    if net is not None:
                        entry["roi_net"] = round(float(net), 6)
                    entry["fee_rt_pct"] = round(
                        float(estimate_round_trip_fee_pct(broker_id, ticker, asset_type)) * 100.0,
                        4,
                    )
            except Exception:
                pass
        try:
            if fee_paid is not None and float(fee_paid) >= 0:
                entry["fee_paid"] = round(float(fee_paid), 4)
        except (TypeError, ValueError):
            pass
        if score is not None:
            try:
                entry["score"] = float(score)
            except (TypeError, ValueError):
                pass
        if rotate_for:
            entry["rotate_for"] = rotate_for
        if rotate_from:
            entry["rotate_from"] = rotate_from
        try:
            journal.log_trade(entry)
        except Exception as e:
            try:
                if sys.stdout is not None:
                    print(f"Journal error: {e}")
            except Exception:
                pass
        # Only touch Qt timers from the GUI thread (orders may run on BackgroundTask)
        app = QApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            QTimer.singleShot(0, self.refresh_recent_trades)

    def execute_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
                          market_hours="regular_hours", allow_fractional=True):
        """Paper-mode-aware buy. Never calls the real broker API when self.paper_mode is True."""
        broker_name = self.cycle_broker_name
        offset_pct = self._effective_limit_offset(offset_pct, side="buy")
        if self.paper_mode:
            cash = self.sandbox_cash.get(broker_name, 0.0)
            if price <= 0 or trade_dollars < 1.0:
                return "Skipped: Invalid price/amount", 0.0
            if trade_dollars > cash:
                trade_dollars = cash
            if trade_dollars < 1.0:
                return "Fail: Insufficient sandbox cash", 0.0
            shares_bought = trade_dollars / price
            book = self.sandbox_holdings.setdefault(broker_name, {})
            pos = book.get(ticker, {'shares': 0.0, 'cost': price, 'type': asset_type})
            new_shares = pos['shares'] + shares_bought
            pos['cost'] = ((pos['shares'] * pos['cost']) + (shares_bought * price)) / new_shares if new_shares > 0 else price
            pos['shares'] = new_shares
            pos['type'] = asset_type
            book[ticker] = pos
            self.sandbox_cash[broker_name] = cash - trade_dollars
            self._record_buy_cost(broker_name, ticker, price, shares_bought)
            status = f"[PAPER] Buy Simulated ({format_currency(trade_dollars)})"
            self._journal_fill(
                "BUY", ticker, asset_type, price, status,
                dollars=trade_dollars, qty=shares_bought,
                quote_price=price, fill_price=price, offset_pct=offset_pct,
                **self._journal_kwargs(),
            )
            self._attach_protective_stop(broker_name, ticker, asset_type, price, trade_dollars)
            return status, trade_dollars
        result = self.cycle_broker.place_buy_order(
            ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
            market_hours=market_hours, allow_fractional=allow_fractional,
        )
        if isinstance(result, tuple) and len(result) >= 3:
            status, spent, order_id = result[0], result[1], result[2]
        else:
            status, spent = result[0], result[1]
            order_id = None
        if spent and spent > 0 and price > 0:
            self._record_buy_cost(broker_name, ticker, price, spent / price)
        fill_px = price
        try:
            qty_est = (spent / price) if price and spent else None
            if qty_est:
                fill_px = float(spent) / float(qty_est)
        except Exception:
            fill_px = price
        self._journal_fill(
            "BUY", ticker, asset_type, price, status,
            dollars=spent, qty=(spent / price) if price and spent else None,
            order_id=order_id, quote_price=price, fill_price=fill_px,
            offset_pct=offset_pct, **self._journal_kwargs(),
        )
        filled_ok = (
            spent and spent > 0
            and "Fail" not in str(status)
            and "Skipped" not in str(status)
            and "Filled" in str(status)
        )
        if filled_ok:
            self._attach_protective_stop(broker_name, ticker, asset_type, price, spent)
        return status, spent

    def _effective_limit_offset(self, offset_pct=None, *, side="buy"):
        """Settings limit offset + conservative fill-quality bump (fraction, not %).

        side='buy' respects use_limit_entries; side='sell' respects use_limit_exits.
        Returns 0 → brokers place market (or market-equivalent) orders.
        """
        toggle = "use_limit_entries" if str(side).lower() != "sell" else "use_limit_exits"
        if not bool(self.settings.get(toggle, True)):
            return 0.0
        try:
            if offset_pct is None:
                base = float(self.settings.get("limit_offset_pct", 0.1) or 0.1) / 100.0
            else:
                base = float(offset_pct)
                if base > 0.05:
                    base = base / 100.0
        except (TypeError, ValueError):
            base = 0.001
        bump = 0.0
        try:
            from scoring import get_execution_feedback
            bump = float(get_execution_feedback().get("offset_bump_pct") or 0.0) / 100.0
        except Exception:
            bump = 0.0
        try:
            sg = getattr(self, "_shadow_guard_active", None) or {}
            if sg.get("tighten") and bool(self.settings.get("shadow_guardrail_enabled", True)):
                bump += float(sg.get("offset_bump_pct") or 0.0) / 100.0
        except Exception:
            pass
        return max(0.0, min(0.05, base + bump))

    @staticmethod
    def _sell_force_market_reason(action_or_reason=""):
        """Hard/urgent exits skip limit preference — fill urgently via market."""
        t = str(action_or_reason or "").upper()
        return any(
            x in t
            for x in (
                "HARD STOP",
                "HARD_STOP",
                "MAX DAILY",
                "DD PAUSE",
                "PANIC",
                "FORCE FLATTEN",
                "EOD FLATTEN",
                "ET FLATTEN",
            )
        )

    def _journal_kwargs(self):
        meta = getattr(self, "_pending_journal_meta", None) or {}
        out = {}
        if meta.get("reason"):
            out["reason"] = meta["reason"]
        if meta.get("score") is not None:
            out["score"] = meta["score"]
        if meta.get("rotate_for"):
            out["rotate_for"] = meta["rotate_for"]
        if meta.get("rotate_from"):
            out["rotate_from"] = meta["rotate_from"]
        return out

    def execute_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                           market_hours="regular_hours", allow_fractional=True, sell_all=True,
                           sell_reason=""):
        """Paper-mode-aware sell. Never calls the real broker API when self.paper_mode is True.

        sell_all=True (default): full position exit — brokers use native close / live qty.
        sell_all=False: intentional partial — qty-based only.
        Hard-stop / flatten reasons force market (offset 0) even when limit exits are ON.
        """
        broker_name = self.cycle_broker_name
        meta = getattr(self, "_pending_journal_meta", None) or {}
        reason_blob = " ".join(
            str(x) for x in (sell_reason, meta.get("reason"), meta.get("action")) if x
        )
        if self._sell_force_market_reason(reason_blob):
            offset_pct = 0.0
        else:
            offset_pct = self._effective_limit_offset(offset_pct, side="sell")
        if self.paper_mode:
            book = self.sandbox_holdings.setdefault(broker_name, {})
            pos = book.get(ticker)
            if not pos or pos['shares'] <= 0:
                return "Fail: No simulated position to sell"
            if sell_all:
                sell_qty = float(pos['shares'])
            else:
                sell_qty = min(shares_val, pos['shares'])
            proceeds = sell_qty * price
            pos['shares'] -= sell_qty
            fully_exited = pos['shares'] <= 1e-9
            if fully_exited:
                del book[ticker]
                self.cost_basis_cache.get(broker_name, {}).pop(ticker, None)
                self._cancel_protective_stop(broker_name, ticker, asset_type)
            else:
                book[ticker] = pos
            self.sandbox_cash[broker_name] = self.sandbox_cash.get(broker_name, 0.0) + proceeds
            tag = "[PAPER] Sell-All Simulated" if sell_all else "[PAPER] Sell Simulated"
            status = f"{tag} ({format_currency(proceeds)})"
            self._journal_fill(
                "SELL", ticker, asset_type, price, status,
                dollars=proceeds, qty=sell_qty,
                quote_price=price, fill_price=price, offset_pct=offset_pct,
                **self._journal_kwargs(),
            )
            return status
        # Live: cancel protective first so reserved shares can sell
        self._cancel_protective_stop(broker_name, ticker, asset_type)
        result = self.cycle_broker.place_sell_order(
            ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
            market_hours=market_hours, allow_fractional=allow_fractional, sell_all=sell_all,
        )
        if isinstance(result, tuple):
            status = result[0]
            order_id = result[1] if len(result) > 1 else None
        else:
            status, order_id = result, None
        if "Fail" not in status and "Skipped" not in status:
            self.cost_basis_cache.get(broker_name, {}).pop(ticker, None)
        # Don't journal session/eligibility skips — those are deferred and would spam Recent Trades
        st_l = str(status).lower()
        if "Skipped" in str(status) and (
            "overnight" in st_l
            or "fractional" in st_l
            or "ext. hours" in st_l
            or "session" in st_l
        ):
            return status
        self._journal_fill(
            "SELL", ticker, asset_type, price, status,
            dollars=shares_val * price, qty=shares_val, order_id=order_id,
            quote_price=price, fill_price=price, offset_pct=offset_pct,
            **self._journal_kwargs(),
        )
        return status

    def send_discord_alert(self, message, is_trade=False, embed=None, urgent=False, prefix=None):
        webhook_url = self.settings.get("discord_webhook", "").strip()
        if not webhook_url:
            return

        alert_lvl = self.settings.get("discord_alert_level", "All Alerts (Every Trade & Heartbeat)")
        if alert_lvl == "Disabled Completely":
            return
        # Important Only = critical/urgent + heartbeat; suppress routine trade spam
        if (
            is_trade
            and not urgent
            and alert_lvl == "Important Only (Critical Alerts & Hourly Heartbeat)"
        ):
            return

        def _post():
            try:
                tag = self.cycle_broker_name if self._cycle_broker else "App"
                body = {"username": "MarketAdvisor"}
                pfx = f"{prefix} " if prefix else ""
                if embed:
                    body["embeds"] = [embed]
                    if message:
                        body["content"] = f"🤖 **MarketAdvisor [{tag}]** {pfx}".rstrip()
                else:
                    body["content"] = f"🤖 **MarketAdvisor [{tag}]**: {pfx}{message}"
                payload = json.dumps(body).encode('utf-8')
                req = urllib.request.Request(
                    webhook_url,
                    data=payload,
                    headers={'Content-Type': 'application/json', 'User-Agent': user_agent()},
                )
                urllib.request.urlopen(req, timeout=10)
                return "ok"
            except Exception as e:
                return f"error:{e}"

        def _done(res):
            if isinstance(res, str) and res.startswith("error:"):
                self.log_event(f"Discord webhook failed: {res[6:]}")

        self.run_thread(_post, _done)

    def _avg_cost_for(self, broker_name, ticker):
        """Look up tracked avg cost (cost_basis_cache stores plain floats per ticker)."""
        try:
            import cost_basis as cb_mod
            return float(cb_mod.cache_lookup(self.cost_basis_cache, broker_name, ticker) or 0.0)
        except Exception:
            cache = self.cost_basis_cache.get(broker_name) or {}
            entry = cache.get(ticker)
            if entry is None and ticker:
                entry = cache.get(str(ticker).replace("-USD", "").upper())
            try:
                return float(entry or 0)
            except (TypeError, ValueError):
                return 0.0

    def _apply_pasted_cost_basis(self):
        """Merge Settings paste lines into cost_basis_cache and persist."""
        import cost_basis as cb_mod
        text = ""
        if hasattr(self, "cost_basis_paste"):
            text = self.cost_basis_paste.toPlainText()
        parsed = cb_mod.parse_manual_basis_lines(text)
        if not parsed:
            QMessageBox.information(
                self,
                "Cost basis",
                "No valid lines found.\nUse: Coinbase:ETH=2450   or   Robinhood SHIB 0.000012",
            )
            return
        n = 0
        for broker, tickers in parsed.items():
            bucket = self.cost_basis_cache.setdefault(broker, {})
            for t, c in tickers.items():
                bucket[t] = float(c)
                n += 1
                # Allow re-log of seeded / clear unknown flag
                ukey = f"{broker}:{t}"
                getattr(self, "_cost_unknown_logged", set()).discard(ukey)
                for src in ("broker", "journal_vwap", "tracked", "last_known", "manual"):
                    getattr(self, "_cost_seeded_logged", set()).discard(f"{broker}:{t}:{src}")
                try:
                    self.log_event(
                        f"[{broker}] Cost basis seeded for {t} via manual "
                        f"@ {format_currency(c)} — TTP/ROI unlocked"
                    )
                except Exception:
                    pass
        try:
            self._persist_cost_basis_cache(force=True)
        except Exception:
            pass
        self.log_event(f"Applied {n} pasted avg cost(s) — refreshing holdings.")
        try:
            self.manual_portfolio_reload(and_score=False, force=True)
        except Exception:
            pass
        QMessageBox.information(self, "Cost basis", f"Saved {n} avg cost(s).")

    def _sell_roi(self, broker_name, ticker, price, avg_cost=None):
        """Return ROI fraction for a sell, or None if unknown."""
        try:
            px = float(price or 0)
        except (TypeError, ValueError):
            return None
        cost = avg_cost
        if cost is None:
            cost = self._avg_cost_for(broker_name, ticker)
        try:
            cost = float(cost or 0)
        except (TypeError, ValueError):
            return None
        if px <= 0 or cost <= 0:
            return None
        # Dust / bogus basis (e.g. RH cost_bases total ≈ $0.0007 ÷ qty) invents
        # mega-% Discord BIG WINs where ($) ≈ proceeds, not profit. Treat unknown.
        if cost < px * 0.01:  # implies ROI > ~9,900%
            return None
        return (px - cost) / cost

    def _is_big_win_roi(self, roi):
        """True when ROI meets Discord big-win threshold (settings %, default 2%)."""
        if roi is None:
            return False
        try:
            thr_pct = float(self.settings.get("discord_big_win_roi_pct", 1.5) or 0)
        except (TypeError, ValueError):
            thr_pct = 2.0
        if thr_pct <= 0:
            return False
        return float(roi) * 100.0 >= thr_pct

    def _heartbeat_align_minute(self, mode):
        """Return clock minute to align hourly ping, or None for rolling."""
        mode = str(mode or "")
        # Migrate old "every N minutes" labels → still once/hour at a mark
        if "Rolling" in mode:
            return None
        if ":15" in mode:
            return 15
        if ":45" in mode:
            return 45
        if ":30" in mode or "half" in mode.lower():
            return 30
        if ":00" in mode or "On the hour" in mode or "hour" in mode.lower():
            return 0
        return None

    def _heartbeat_is_due(self, now_ts):
        """True when Discord should phone home — always at most once per hour."""
        mode = self.settings.get("discord_heartbeat_schedule", "Rolling (every hour from now)")
        now_dt = datetime.fromtimestamp(now_ts)
        align = self._heartbeat_align_minute(mode)

        if align is None:
            return (now_ts - self.last_heartbeat_time) >= 3600

        slot_dt = now_dt.replace(minute=align, second=0, microsecond=0)
        if now_dt < slot_dt:
            slot_dt = slot_dt - timedelta(hours=1)

        slot_key = slot_dt.strftime("%Y-%m-%d %H:%M")
        if self._last_heartbeat_slot == slot_key:
            return False
        return now_dt >= slot_dt and self.last_heartbeat_time < slot_dt.timestamp()

    def _maybe_send_heartbeat(self, now_ts):
        if not any(self.auto_trade_enabled.values()):
            return
        if not self._heartbeat_is_due(now_ts):
            return

        webhook_url = self.settings.get("discord_webhook", "").strip()
        alert_lvl = self.settings.get("discord_alert_level", "All Alerts (Every Trade & Heartbeat)")
        if not webhook_url or alert_lvl == "Disabled Completely":
            return

        active = [b for b, on in self.auto_trade_enabled.items() if on]
        totals = getattr(self, "_last_balance_totals", {}) or {}
        fields = []
        combined_eq = combined_cash = combined_pl = 0.0

        # Always show both brokers (armed or not) so a false loss-halt doesn't "erase" RH from Discord
        for name in BROKER_NAMES:
            connected = self.brokers[name].is_connected or self.paper_mode
            armed = bool(self.auto_trade_enabled.get(name))
            p_val = float(totals.get(name, {}).get("p_val", 0.0) or 0.0)
            bp = float(totals.get(name, {}).get("bp", 0.0) or 0.0)
            start = self.session_starts.get(name)
            pl = (p_val - start) if start and start > 0 else 0.0
            combined_eq += p_val
            combined_cash += bp
            combined_pl += pl
            if not connected:
                status = "⚠️ Down"
            elif armed:
                status = "✅ Online · Armed"
            else:
                status = "⏸️ Online · Disarmed"
            pl_txt = f"+{format_money(pl)}" if pl >= 0 else format_money(pl)
            fields.append({
                "name": name,
                "value": f"{status}\nEquity **{format_money(p_val)}**\nCash **{format_money(bp)}**\nDay P&L **{pl_txt}**",
                "inline": True,
            })

        market = "Unknown"
        if hasattr(self, "market_status_lbl"):
            market = (self.market_status_lbl.text() or "").replace("Market: ", "").strip() or market
        mode_label = "📝 PAPER" if self.paper_mode else "🟢 LIVE"
        cpl = f"+{format_money(combined_pl)}" if combined_pl >= 0 else format_money(combined_pl)
        fields.append({
            "name": "Combined",
            "value": f"Equity **{format_money(combined_eq)}** · Cash **{format_money(combined_cash)}** · Day **{cpl}**",
            "inline": False,
        })
        fields.append({
            "name": "Session",
            "value": f"{mode_label} · Market **{market}** · Armed **{', '.join(active)}**",
            "inline": False,
        })

        # Side color: red if day down; green if day up; blue if flat (broker Down status ignored)
        if combined_pl < -0.001:
            color = 0xE74C3C
        elif combined_pl > 0.001:
            color = 0x2ECC71
        else:
            color = 0x3498DB

        now_dt = datetime.fromtimestamp(now_ts)
        clock = now_dt.strftime("%I:%M %p").lstrip("0")
        embed = {
            "title": f"📊 Hourly Check-in · {clock}",
            "description": "Auto-trader heartbeat — balances & day P&L by broker",
            "color": color,
            "fields": fields,
            "footer": {"text": f"{display_name()} · dual-broker telemetry"},
            "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }

        # Stamp only when we are actually posting (webhook present + alerts enabled)
        self.last_heartbeat_time = now_ts
        mode = self.settings.get("discord_heartbeat_schedule", "Rolling (every hour from now)")
        align = self._heartbeat_align_minute(mode)
        if align is not None:
            slot_dt = now_dt.replace(minute=align, second=0, microsecond=0)
            if now_dt < slot_dt:
                slot_dt = slot_dt - timedelta(hours=1)
            self._last_heartbeat_slot = slot_dt.strftime("%Y-%m-%d %H:%M")

        self.send_discord_alert(f"Heartbeat {clock}", embed=embed, prefix="[HEARTBEAT]")
        self.log_event(f"Discord heartbeat sent ({mode})")

    def _send_discord_launch_checkin(self, force=False):
        """Phone home once after startup. Uses a plain thread so Qt QThread quirks cannot drop it."""
        if getattr(self, "_launch_checkin_sent", False) and not force:
            return
        if getattr(self, "_launch_checkin_in_flight", False) and not force:
            return
        self._launch_checkin_in_flight = True
        self._pending_launch_checkin = False
        self._launch_checkin_failsafe_armed = False

        webhook_url = self.settings.get("discord_webhook", "").strip()
        if not webhook_url:
            self._launch_checkin_in_flight = False
            self._launch_checkin_sent = True  # nothing to send; don't retry forever
            self.log_event("Discord launch check-in skipped: no webhook URL.")
            return
        if self.settings.get("discord_alert_level", "") == "Disabled Completely":
            self._launch_checkin_in_flight = False
            self._launch_checkin_sent = True
            self.log_event("Discord launch check-in skipped: alerts disabled.")
            return

        totals = getattr(self, "_last_balance_totals", {}) or {}
        total_eq = 0.0
        lines = []
        for name in BROKER_NAMES:
            connected = self.brokers[name].is_connected or self.paper_mode
            p_val = float(totals.get(name, {}).get("p_val", 0.0) or 0.0)
            bp = float(totals.get(name, {}).get("bp", 0.0) or 0.0)
            total_eq += p_val
            status = "Online" if connected else "Offline"
            lines.append(f"**{name}**: {status} · Equity {format_money(p_val)} · Cash {format_money(bp)}")

        # Remember empty failsafe pings so a later balance fetch can upgrade Discord once
        self._launch_checkin_was_empty = total_eq <= 0.01

        mode_label = "PAPER" if self.paper_mode else "LIVE"
        clock = datetime.now().strftime("%I:%M %p").lstrip("0")
        prefix = "updated balances" if force else "online"
        content = (
            f"🤖 **Market Advisor {prefix}** — {clock} ({mode_label})\n"
            + "\n".join(lines)
            + "\nAuto-trader starts **off** until you arm it."
        )
        self.log_event("Sending Discord launch check-in…" + (" (balance update)" if force else ""))

        def worker():
            try:
                payload = json.dumps({
                    "username": "MarketAdvisor",
                    "content": content,
                }).encode("utf-8")
                req = urllib.request.Request(
                    webhook_url,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": user_agent()},
                )
                urllib.request.urlopen(req, timeout=15)
                self._launch_discord_finished.emit(True, "")
            except Exception as e:
                self._launch_discord_finished.emit(False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_launch_discord_finished(self, ok, err):
        self._launch_checkin_in_flight = False
        if ok:
            self._launch_checkin_sent = True
            self._pending_launch_checkin = False
            self.log_event("Discord launch check-in delivered.")
            # Balances may have arrived while the empty ping was in flight — upgrade now
            if self._launch_checkin_should_upgrade():
                self._launch_checkin_upgrade_pending = False
                self._launch_checkin_was_empty = False
                QTimer.singleShot(0, lambda: self._send_discord_launch_checkin(force=True))
        else:
            self.log_event(f"Discord launch check-in FAILED: {err}")
            # One automatic retry so a transient webhook blip does not kill the ping
            if not getattr(self, "_launch_checkin_retry_scheduled", False):
                self._launch_checkin_retry_scheduled = True
                self._pending_launch_checkin = True
                QTimer.singleShot(5000, self._send_discord_launch_checkin)

    def _launch_equity_total(self, totals=None):
        totals = totals if totals is not None else (getattr(self, "_last_balance_totals", {}) or {})
        return sum(
            float((totals.get(n) or {}).get("p_val", 0) or 0)
            for n in BROKER_NAMES
        )

    def _launch_checkin_should_upgrade(self):
        """True when an empty/$0 ping should be replaced with real balances."""
        if not getattr(self, "_launch_checkin_sent", False):
            return False
        if not (
            getattr(self, "_launch_checkin_was_empty", False)
            or getattr(self, "_launch_checkin_upgrade_pending", False)
        ):
            return False
        return self._launch_equity_total() > 0.01

    def refresh_account_balances(self, quiet=False):
        """Pull broker equity/cash. quiet=True skips the status-bar spinner (idle polls)."""
        if getattr(self, "_balances_refresh_in_flight", False):
            return
        today = datetime.now().date()
        if today > self.current_trading_day:
            self.current_trading_day = today
            self.session_starts = _blank_broker_map(None)
            self._persist_session_baselines()
            self.log_event("🌅 Midnight reached. Daily P&L Tracker reset for the new day.")

        self._balances_refresh_in_flight = True
        if not quiet:
            self.set_working_state(True, f"Fetching {self.active_broker_name} balances...")

        def _ok(totals):
            self._balances_refresh_in_flight = False
            self._on_all_balances_fetched(totals if isinstance(totals, dict) else {})

        def _fail(err):
            self._balances_refresh_in_flight = False
            self.log_event(f"Balance fetch error: {err}")
            # Keep last known totals — do not overwrite with zeros (corrupts Day P&L / Discord)
            if not quiet:
                self.set_working_state(False)
            if getattr(self, "_pending_launch_checkin", False) and not getattr(self, "_launch_checkin_sent", False):
                self._send_discord_launch_checkin()
            self.publish_monitor_status()

        task = BackgroundTask(self._bg_fetch_all_balances)
        task.result_ready.connect(_ok)
        task.error_occurred.connect(_fail)
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def _bg_fetch_all_balances(self):
        totals = {}
        for name, broker in self.brokers.items():
            try:
                if self.paper_mode:
                    p_val, bp = self.get_broker_balances(name)
                    totals[name] = {
                        "p_val": float(p_val or 0.0),
                        "bp": float(bp or 0.0),
                        "ok": True,
                        "holdings_count": int(
                            (getattr(self, "_holdings_count_cache", {}) or {}).get(name, 0) or 0
                        ),
                    }
                    continue
                if not broker.is_connected:
                    # Disconnected — do not invent $0 equity (that fake-trips day-loss limits)
                    totals[name] = {"ok": False, "reason": "disconnected"}
                    continue
                # Call adapter directly so API failures stay ok:False (not cache-masked).
                p_val, bp = broker.get_account_balances()
                p_val = float(p_val or 0.0)
                bp = float(bp or 0.0)
                holdings_count = int(
                    (getattr(self, "_holdings_count_cache", {}) or {}).get(name, 0) or 0
                )
                last_trusted = (getattr(self, "_last_trusted_equity", {}) or {}).get(name)
                baseline = (getattr(self, "session_starts", {}) or {}).get(name)
                prev_p = float(
                    ((getattr(self, "_last_balance_totals", {}) or {}).get(name) or {}).get(
                        "p_val", 0.0
                    )
                    or 0.0
                )
                if is_near_zero_wipe(p_val, prev_p, baseline, last_trusted):
                    try:
                        holdings = self.get_broker_holdings(name) or []
                        holdings_count = len(
                            {
                                (a.get("ticker") or "").upper()
                                for a in holdings
                                if isinstance(a, dict) and a.get("ticker")
                            }
                        )
                        self._holdings_count_cache[name] = holdings_count
                    except Exception:
                        pass
                    if holdings_count > 0:
                        totals[name] = {
                            "ok": False,
                            "reason": f"zero equity with {holdings_count} holding(s) still present",
                            "p_val": p_val,
                            "bp": bp,
                            "holdings_count": holdings_count,
                        }
                        continue
                    # Empty book + $0 while we had real equity: treat like a dead session,
                    # not a realized −100% day (avoids false MAX DAILY LOSS).
                    totals[name] = {
                        "ok": False,
                        "reason": "zero_equity_unreliable",
                        "p_val": p_val,
                        "bp": bp,
                        "holdings_count": 0,
                    }
                    continue
                totals[name] = {
                    "p_val": p_val,
                    "bp": bp,
                    "ok": True,
                    "holdings_count": holdings_count,
                }
            except Exception as e:
                totals[name] = {"ok": False, "reason": str(e)}
                try:
                    if sys.stdout is not None:
                        print(f"balance error [{name}]: {e}")
                except Exception:
                    pass
        return totals

    def _balance_reading_is_suspicious(self, broker_name, new_p, old_p, baseline):
        """True when a new equity print looks like a failed API read, not a real wipe."""
        last_trusted = (getattr(self, "_last_trusted_equity", {}) or {}).get(broker_name)
        loss_limit = float(self.settings.get("daily_loss_limit", 0.0) or 0.0)
        return balance_reading_is_suspicious(
            new_p, old_p, baseline, last_trusted, loss_limit=loss_limit
        )

    def _keep_equity_floor(self, broker_name, old_p=0.0, last_trusted=None):
        """Best non-zero equity to paint while rejecting a glitch wipe."""
        baseline = (getattr(self, "session_starts", {}) or {}).get(broker_name)
        if last_trusted is None:
            last_trusted = (getattr(self, "_last_trusted_equity", {}) or {}).get(broker_name)
        return float(reference_equity(old_p, baseline, last_trusted) or 0.0)

    def _merge_balance_totals(self, incoming):
        """Keep last-good equity when a broker fetch fails or returns a suspicious wipe."""
        prev = getattr(self, "_last_balance_totals", {}) or {}
        if not hasattr(self, "_balance_bad_streak"):
            self._balance_bad_streak = _blank_broker_map(0)
        if not hasattr(self, "_last_trusted_equity"):
            self._last_trusted_equity = _blank_broker_map(None)
        if not hasattr(self, "_balance_zero_glitch_warned"):
            self._balance_zero_glitch_warned = _blank_broker_map(False)

        merged = {}
        trusted = _blank_broker_map(False)

        for name in BROKER_NAMES:
            raw = incoming.get(name) if isinstance(incoming, dict) else None
            old = prev.get(name) or {}
            old_p = float(old.get("p_val", 0.0) or 0.0)
            old_bp = float(old.get("bp", 0.0) or 0.0)
            baseline = self.session_starts.get(name)
            last_trusted = self._last_trusted_equity.get(name)
            if last_trusted is None:
                # Prefer prior painted equity, else day baseline — never leave last_trusted empty
                # when we already know the account had money today.
                seed = old_p if old_p > NEAR_ZERO_EQUITY else None
                if seed is None:
                    try:
                        seed = float(baseline) if baseline and float(baseline) > NEAR_ZERO_EQUITY else None
                    except (TypeError, ValueError):
                        seed = None
                if seed is not None:
                    last_trusted = seed
                    self._last_trusted_equity[name] = seed

            if not isinstance(raw, dict) or raw.get("ok") is False:
                reason = (raw or {}).get("reason", "fetch failed") if isinstance(raw, dict) else "missing"
                self._balance_bad_streak[name] = self._balance_bad_streak.get(name, 0) + 1
                keep_p = self._keep_equity_floor(name, old_p=old_p, last_trusted=last_trusted)
                keep_bp = old_bp
                # No session / unreliable $0: keep last equity quietly (log once per stretch).
                if reason in ("disconnected", "zero_equity_unreliable") or str(reason).startswith(
                    "zero equity with"
                ):
                    warned_map = (
                        self._balance_disconnected_warned
                        if reason == "disconnected"
                        else self._balance_zero_glitch_warned
                    )
                    if not warned_map.get(name):
                        warned_map[name] = True
                        if reason == "disconnected":
                            self.log_event(
                                f"[{name}] Balance polling paused (disconnected) — "
                                f"keeping last good equity {format_money(keep_p)}"
                            )
                        else:
                            self.log_event(
                                f"[{name}] Unreliable $0 equity read ({reason}) — "
                                f"keeping last good equity {format_money(keep_p)} "
                                f"(not treating as day loss)"
                            )
                else:
                    self.log_event(
                        f"[{name}] Balance fetch unreliable ({reason}) — "
                        f"keeping last good equity {format_money(keep_p)}"
                    )
                    if _is_manual_auth_failure(reason):
                        self._handle_broker_auth_failure(
                            name, reason, source="balance_poll"
                        )
                merged[name] = {"p_val": keep_p, "bp": keep_bp}
                trusted[name] = False
                continue

            new_p = float(raw.get("p_val", 0.0) or 0.0)
            new_bp = float(raw.get("bp", 0.0) or 0.0)
            holdings_count = int(
                raw.get("holdings_count")
                if raw.get("holdings_count") is not None
                else (getattr(self, "_holdings_count_cache", {}) or {}).get(name, 0)
                or 0
            )

            loss_limit = float(self.settings.get("daily_loss_limit", 0.0) or 0.0)

            # Healthy non-zero read always wins — clear glitch state and repaint Home.
            if new_p > NEAR_ZERO_EQUITY:
                # Still run collapse / day-loss-trip guard for sudden drops from last trusted
                if self._balance_reading_is_suspicious(name, new_p, old_p, baseline):
                    decision = decide_suspicious_equity(
                        new_p,
                        old_p,
                        baseline,
                        last_trusted=last_trusted,
                        holdings_count=holdings_count,
                        bad_streak=self._balance_bad_streak.get(name, 0),
                        loss_limit=loss_limit,
                    )
                    self._balance_bad_streak[name] = int(decision.get("streak") or 0)
                    self.log_event(f"[{name}] {decision.get('reason')}")
                    if decision.get("action") != "accept":
                        keep_p = self._keep_equity_floor(name, old_p=old_p, last_trusted=last_trusted)
                        merged[name] = {"p_val": keep_p, "bp": old_bp}
                        trusted[name] = False
                        continue
                self._balance_bad_streak[name] = 0
                if getattr(self, "_balance_disconnected_warned", None) is not None:
                    self._balance_disconnected_warned[name] = False
                if getattr(self, "_balance_zero_glitch_warned", None) is not None:
                    self._balance_zero_glitch_warned[name] = False
                merged[name] = {"p_val": new_p, "bp": new_bp}
                trusted[name] = True
                self._last_trusted_equity[name] = new_p
                continue

            # Near-zero / zero path
            if self._balance_reading_is_suspicious(name, new_p, old_p, baseline):
                decision = decide_suspicious_equity(
                    new_p,
                    old_p,
                    baseline,
                    last_trusted=last_trusted,
                    holdings_count=holdings_count,
                    bad_streak=self._balance_bad_streak.get(name, 0),
                    loss_limit=loss_limit,
                )
                self._balance_bad_streak[name] = int(decision.get("streak") or 0)
                self.log_event(f"[{name}] {decision.get('reason')}")
                if decision.get("action") != "accept":
                    keep_p = self._keep_equity_floor(name, old_p=old_p, last_trusted=last_trusted)
                    merged[name] = {"p_val": keep_p, "bp": old_bp if old_bp > 0 else new_bp}
                    trusted[name] = False
                    continue

            self._balance_bad_streak[name] = 0
            if getattr(self, "_balance_disconnected_warned", None) is not None:
                self._balance_disconnected_warned[name] = False
            if getattr(self, "_balance_zero_glitch_warned", None) is not None:
                self._balance_zero_glitch_warned[name] = False
            merged[name] = {"p_val": new_p, "bp": new_bp}
            trusted[name] = True
            if new_p > NEAR_ZERO_EQUITY:
                self._last_trusted_equity[name] = new_p

        return merged, trusted

    def _refresh_home_balance_labels(self):
        """UI-only refresh of Home / top-bar money labels from cached totals (safe on resize)."""
        merged = getattr(self, "_last_balance_totals", None) or {}
        if not merged:
            return
        master_val = sum(float((d or {}).get("p_val", 0) or 0) for d in merged.values())
        master_bp = sum(float((d or {}).get("bp", 0) or 0) for d in merged.values())
        if hasattr(self, "home_master_val_lbl"):
            self.home_master_val_lbl.setText(format_money(master_val))
            self.home_master_bp_lbl.setText(f"Combined Liquid Cash: {format_money(master_bp)}")

        combined_pl = 0.0
        tc = theme_colors(self.dark_mode)
        for broker_name in BROKER_NAMES:
            p_val = float((merged.get(broker_name) or {}).get("p_val", 0.0) or 0.0)
            bp = float((merged.get(broker_name) or {}).get("bp", 0.0) or 0.0)
            pl_val = 0.0
            start = self.session_starts.get(broker_name)
            if start is not None and start > 0:
                pl_val = p_val - start
            combined_pl += pl_val
            pl_str = format_money(abs(pl_val))
            pl_display = f"+{pl_str}" if pl_val >= 0 else f"-{pl_str}"
            color = tc["success"] if pl_val > 0.001 else (
                tc["danger"] if pl_val < -0.001 else tc["neutral"]
            )
            if broker_name == "Robinhood" and hasattr(self, "home_rh_val_lbl"):
                self.home_rh_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
                self.home_rh_bp_lbl.setText(f"Buying Power: {format_money(bp)}")
                self.home_rh_pl_lbl.setText(f"Day P&L: {pl_display}")
                self.home_rh_pl_lbl.setStyleSheet(metric_label_style(color, 15))
            elif broker_name == "Coinbase" and hasattr(self, "home_cb_val_lbl"):
                self.home_cb_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
                self.home_cb_bp_lbl.setText(f"Buying Power: {format_money(bp)}")
                self.home_cb_pl_lbl.setText(f"Day P&L: {pl_display}")
                self.home_cb_pl_lbl.setStyleSheet(metric_label_style(color, 15))
            elif broker_name == "E*TRADE" and hasattr(self, "home_et_val_lbl"):
                self._update_etrade_home_metrics(p_val, bp, pl_display, color)

        if hasattr(self, "home_master_pl_lbl"):
            cpl_str = format_money(abs(combined_pl))
            cpl_display = f"+{cpl_str}" if combined_pl >= 0 else f"-{cpl_str}"
            cpl_color = tc["success"] if combined_pl > 0.001 else (
                tc["danger"] if combined_pl < -0.001 else tc["neutral"]
            )
            self.home_master_pl_lbl.setText(f"Combined Day P&L: {cpl_display}")
            self.home_master_pl_lbl.setStyleSheet(metric_label_style(cpl_color, 16))

        if hasattr(self, "portfolio_val_lbl"):
            self._refresh_top_bar_from_cache()

    def _update_etrade_home_metrics(self, p_val, bp, pl_display, color):
        """Home E*TRADE row: balances + Sandbox/Live + no-BP / stops-N/A chip."""
        if not hasattr(self, "home_et_val_lbl"):
            return
        et = self.brokers.get("E*TRADE")
        env = str(getattr(et, "environment", None) or self.settings.get("etrade_environment", "sandbox")).lower()
        live_ok = bool(getattr(et, "live_trading_enabled", False) or self.settings.get("etrade_live_trading", False))
        self.home_et_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
        bp_f = float(bp or 0.0)
        min_d = float(self.settings.get("min_trade_dollars", 5.0) or 5.0)
        bp_txt, bp_tip = _auto_cycle.etrade_bp_label(
            bp_f, environment=env, min_trade_dollars=min_d,
        )
        self.home_et_bp_lbl.setText(bp_txt)
        self.home_et_bp_lbl.setToolTip(bp_tip)
        self.home_et_pl_lbl.setText(f"Day P&L: {pl_display}")
        self.home_et_pl_lbl.setStyleSheet(metric_label_style(color, 15))
        if hasattr(self, "home_et_env_chip"):
            chip, tip, col = _auto_cycle.etrade_home_env_chip(
                environment=env,
                live_trading=live_ok,
                buying_power=bp_f,
                min_trade_dollars=min_d,
            )
            self.home_et_env_chip.setText(chip)
            self.home_et_env_chip.setToolTip(tip)
            self.home_et_env_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 700; color: {col}; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )

    def _on_all_balances_fetched(self, totals):
        merged, trusted = self._merge_balance_totals(totals if isinstance(totals, dict) else {})
        self._last_balance_totals = merged
        # Update Master Totals
        master_val = sum(float((d or {}).get("p_val", 0) or 0) for d in merged.values())
        master_bp = sum(float((d or {}).get("bp", 0) or 0) for d in merged.values())
        
        if hasattr(self, 'home_master_val_lbl'):
            self.home_master_val_lbl.setText(format_money(master_val))
            self.home_master_bp_lbl.setText(f"Combined Liquid Cash: {format_money(master_bp)}")

        combined_pl = 0.0

        # Process Each Broker
        for broker_name in BROKER_NAMES:
            p_val = float((merged.get(broker_name) or {}).get("p_val", 0.0) or 0.0)
            bp = float((merged.get(broker_name) or {}).get("bp", 0.0) or 0.0)
            
            # Session Init (persisted for the calendar day so restarts keep Day P&L)
            if self.session_starts[broker_name] is None and p_val > 0 and trusted.get(broker_name):
                self.session_starts[broker_name] = p_val
                self._persist_session_baselines()
                self.log_event(f"[{broker_name}] Baseline Equity set to: {format_currency(p_val)}")
            
            pl_val = 0.0
            if self.session_starts[broker_name] is not None and self.session_starts[broker_name] > 0:
                pl_val = p_val - self.session_starts[broker_name]
            combined_pl += pl_val
                
            pl_str = format_money(abs(pl_val))
            pl_display = f"+{pl_str}" if pl_val >= 0 else f"-{pl_str}"
            tc = theme_colors(self.dark_mode)
            color = tc["success"] if pl_val > 0.001 else (
                tc["danger"] if pl_val < -0.001 else tc["neutral"]
            )

            # Update Home Banners
            if hasattr(self, 'home_rh_val_lbl'):
                if broker_name == "Robinhood":
                    self.home_rh_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
                    self.home_rh_bp_lbl.setText(f"Buying Power: {format_money(bp)}")
                    self.home_rh_pl_lbl.setText(f"Day P&L: {pl_display}")
                    self.home_rh_pl_lbl.setStyleSheet(metric_label_style(color, 15))
                elif broker_name == "Coinbase":
                    self.home_cb_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
                    self.home_cb_bp_lbl.setText(f"Buying Power: {format_money(bp)}")
                    self.home_cb_pl_lbl.setText(f"Day P&L: {pl_display}")
                    self.home_cb_pl_lbl.setStyleSheet(metric_label_style(color, 15))
                elif broker_name == "E*TRADE" and hasattr(self, "home_et_val_lbl"):
                    self._update_etrade_home_metrics(p_val, bp, pl_display, color)

            # Profit/loss limits only on trusted balance reads (never on a glitch $0)
            if self.auto_trade_enabled.get(broker_name) and trusted.get(broker_name):
                try:
                    from scoring import update_equity_drawdown, posture_for_broker, posture_knobs_for_broker
                    bid = {
                        "Robinhood": "ROBINHOOD",
                        "Coinbase": "COINBASE",
                        "E*TRADE": "ETRADE",
                    }.get(broker_name, broker_name)
                    posture_b = posture_for_broker(broker_name, self.settings)
                    knobs_b = posture_knobs_for_broker(broker_name, self.settings)
                    triggered, dd_msg = update_equity_drawdown(
                        bid,
                        p_val,
                        posture=posture_b,
                        settings=self.settings,
                    )
                    if triggered and dd_msg:
                        mins = int(knobs_b.get("dd_pause_minutes") or 45)
                        self.log_event(
                            f"[DD] [{broker_name}] {dd_msg} — "
                            f"pausing new buys ({mins}m); auto-trader stays armed"
                        )
                        self.send_discord_alert(
                            f"[DD] **[{broker_name}]** {dd_msg} — "
                            f"new buys paused {mins}m (auto-trader still armed).",
                            urgent=True,
                            prefix="[RISK]",
                        )
                except Exception:
                    pass
                target_profit = self.settings.get("daily_profit_target", 0.0)
                if target_profit > 0 and pl_val >= target_profit:
                    msg = f"🎯 **[{broker_name}] Day Profit Target Reached!** Target: {format_currency(target_profit)} | Gain: {format_currency(pl_val)}. Disarming Auto-Trader."
                    self.log_event(msg)
                    self.send_discord_alert(msg, urgent=True, prefix="[RISK]")
                    self._disarm_broker(broker_name)

                loss_limit = self.settings.get("daily_loss_limit", 0.0)
                if loss_limit > 0 and pl_val <= -loss_limit:
                    msg = (
                        f"🚨 **[{broker_name}] MAX DAILY $-LOSS LIMIT HIT!** "
                        f"Limit: -{format_currency(loss_limit)} | Loss: -{pl_str}. "
                        f"DISARMING AUTO-TRADER ($-loss halt — not a DD pause)."
                    )
                    self.log_event(msg)
                    self.send_discord_alert(msg, urgent=True, prefix="[RISK]")
                    self._disarm_broker(broker_name)

        if hasattr(self, 'home_master_pl_lbl'):
            cpl_str = format_money(abs(combined_pl))
            cpl_display = f"+{cpl_str}" if combined_pl >= 0 else f"-{cpl_str}"
            tc = theme_colors(self.dark_mode)
            cpl_color = tc["success"] if combined_pl > 0.001 else (
                tc["danger"] if combined_pl < -0.001 else tc["neutral"]
            )
            self.home_master_pl_lbl.setText(f"Combined Day P&L: {cpl_display}")
            self.home_master_pl_lbl.setStyleSheet(metric_label_style(cpl_color, 16))

        # Top bar reflects current view (All = combined)
        self._refresh_top_bar_from_cache()
        self._refresh_portfolio_heat()
        self.set_working_state(False)
        self.publish_monitor_status()

        # Launch Discord: first ping after balances, or upgrade an empty/$0 ping
        self._balances_fetched_once = True
        self._maybe_coach_growth_posture()
        master_val = self._launch_equity_total(merged)
        in_flight = getattr(self, "_launch_checkin_in_flight", False)
        pending = getattr(self, "_pending_launch_checkin", False)
        sent = getattr(self, "_launch_checkin_sent", False)

        if pending and not sent:
            if in_flight:
                if master_val > 0.01:
                    self._launch_checkin_upgrade_pending = True
                    self._launch_checkin_was_empty = True
            elif master_val > 0.01:
                self._send_discord_launch_checkin()
            else:
                # Don't Discord $0 on the first fetch — retry once, else failsafe will send
                self.log_event("Balances still $0 — holding Discord launch ping…")
                if not getattr(self, "_launch_zero_balance_retry", False):
                    self._launch_zero_balance_retry = True
                    QTimer.singleShot(3500, self.refresh_account_balances)
        elif self._launch_checkin_should_upgrade() or (
            sent and getattr(self, "_launch_checkin_was_empty", False) and master_val > 0.01
        ):
            if in_flight:
                self._launch_checkin_upgrade_pending = True
            else:
                self._launch_checkin_upgrade_pending = False
                self._launch_checkin_was_empty = False
                self._send_discord_launch_checkin(force=True)

    def _start_web_monitor(self):
        """Start or restart the status server from settings (localhost or LAN/HTTPS).

        Bind + TLS wrap (especially on 0.0.0.0) can take ~1s — run off the UI thread.
        """
        if not self.settings.get("monitor_enabled", True):
            monitor.stop_monitor()
            return
        host = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
        port = int(self.settings.get("monitor_port", 8791))
        user = self.settings.get("monitor_user", "") or ""
        pwd = self.settings.get("monitor_pass", "") or ""
        remote = host not in ("127.0.0.1", "localhost", "::1")
        use_tls = bool(self.settings.get("monitor_https", True)) or remote
        controls = bool(self.settings.get("monitor_controls_enabled", False)) and bool(user.strip())
        # Handlers must be set on the UI thread before the server thread accepts requests
        monitor.set_control_handler(self._monitor_control_from_http)
        monitor.set_halt_handler(self._monitor_halt_from_http)
        monitor.set_advisor_handler(self._monitor_advisor_from_http)
        monitor.set_eod_handler(self._monitor_eod_from_http)
        monitor.set_etrade_oauth_handler(self._monitor_etrade_oauth_from_http)

        self._monitor_start_gen = getattr(self, "_monitor_start_gen", 0) + 1
        gen = self._monitor_start_gen
        start_kwargs = {
            "host": host,
            "port": port,
            "username": user,
            "password": pwd,
            "controls_enabled": controls,
            "use_tls": use_tls,
        }

        def _bg():
            monitor.stop_monitor()
            ok, msg = monitor.start_monitor(**start_kwargs)
            fp = monitor.get_cert_fingerprint() if ok else ""
            return {"ok": bool(ok), "msg": msg or "", "fp": fp or "", "gen": gen}

        task = BackgroundTask(_bg)
        task.result_ready.connect(self._on_web_monitor_started)
        task.error_occurred.connect(
            lambda e, g=gen: self._on_web_monitor_started(
                {"ok": False, "msg": str(e), "fp": "", "gen": g}
            )
        )
        task.finished.connect(
            lambda: self.active_threads.remove(task) if task in self.active_threads else None
        )
        self.active_threads.append(task)
        task.start()

    def _on_web_monitor_started(self, result):
        """UI follow-up after async monitor bind (ignore superseded restarts)."""
        result = result or {}
        if result.get("gen") != getattr(self, "_monitor_start_gen", 0):
            return
        ok = bool(result.get("ok"))
        msg = result.get("msg") or ""
        self.log_event(msg if ok else f"Web monitor failed: {msg}")
        if ok:
            self.publish_monitor_status()
            fp = result.get("fp") or monitor.get_cert_fingerprint()
            if fp and hasattr(self, "monitor_fp_lbl"):
                self.monitor_fp_lbl.setText(f"TLS fingerprint (paste into Android): {fp}")
            elif hasattr(self, "monitor_fp_lbl"):
                self.monitor_fp_lbl.setText(
                    "TLS fingerprint: (localhost HTTP — enable LAN + HTTPS for remote)"
                )
            self._refresh_companion_qr_button()
            if hasattr(self, "_update_companion_monitor_status"):
                self._update_companion_monitor_status()

    def _companion_setup_fields_from_settings(self, lan_ip=None):
        """Applied (saved) monitor fields + running/disk TLS fingerprint for QR."""
        host = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
        port = int(self.settings.get("monitor_port", 8791))
        user = self.settings.get("monitor_user", "") or ""
        pwd = self.settings.get("monitor_pass", "") or ""
        remote = host not in ("127.0.0.1", "localhost", "::1")
        use_https = bool(self.settings.get("monitor_https", True)) or remote
        fp = monitor.get_cert_fingerprint() or ""
        if not fp:
            try:
                from monitor_tls import read_fingerprint
                fp = read_fingerprint() or ""
            except Exception:
                fp = ""
        url = companion_qr.companion_base_url(host, port, use_https, lan_ip=lan_ip)
        return url, user, pwd, fp, use_https, host, port

    def _companion_setup_fields_from_ui(self):
        """Current monitor fields from Settings widgets (or saved settings as fallback)."""
        if hasattr(self, "monitor_bind_combo"):
            host = self.monitor_bind_combo.currentData() or "127.0.0.1"
            port = int(self.monitor_port_spin.value())
            user = self.monitor_user_input.text().strip()
            pwd = self.monitor_pass_input.text()
            remote = host not in ("127.0.0.1", "localhost", "::1")
            use_https = bool(self.monitor_https_chk.isChecked()) or remote
        else:
            host = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
            port = int(self.settings.get("monitor_port", 8791))
            user = self.settings.get("monitor_user", "") or ""
            pwd = self.settings.get("monitor_pass", "") or ""
            remote = host not in ("127.0.0.1", "localhost", "::1")
            use_https = bool(self.settings.get("monitor_https", True)) or remote
        fp = monitor.get_cert_fingerprint() or ""
        if not fp:
            try:
                from monitor_tls import read_fingerprint
                fp = read_fingerprint() or ""
            except Exception:
                fp = ""
        url = companion_qr.companion_base_url(host, port, use_https)
        return url, user, pwd, fp, use_https

    def _companion_monitor_widgets_dirty(self):
        """True when Companion dialog widgets differ from applied settings."""
        if not hasattr(self, "monitor_bind_combo"):
            return False
        host = self.monitor_bind_combo.currentData() or "127.0.0.1"
        port = int(self.monitor_port_spin.value())
        user = self.monitor_user_input.text().strip()
        pwd = self.monitor_pass_input.text()
        https = bool(self.monitor_https_chk.isChecked())
        enabled = bool(self.monitor_enabled_chk.isChecked())
        controls = bool(self.monitor_controls_chk.isChecked())
        saved_host = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
        remote = host not in ("127.0.0.1", "localhost", "::1")
        want_https = https or remote
        saved_remote = saved_host not in ("127.0.0.1", "localhost", "::1")
        saved_https = bool(self.settings.get("monitor_https", True)) or saved_remote
        return (
            enabled != bool(self.settings.get("monitor_enabled", True))
            or host != saved_host
            or port != int(self.settings.get("monitor_port", 8791))
            or user != (self.settings.get("monitor_user", "") or "")
            or pwd != (self.settings.get("monitor_pass", "") or "")
            or want_https != saved_https
            or controls != bool(self.settings.get("monitor_controls_enabled", False))
        )

    def _refresh_companion_qr_button(self):
        if not hasattr(self, "monitor_qr_btn"):
            return
        _url, _user, _pwd, fp, use_https, *_rest = self._companion_setup_fields_from_settings()
        ok = bool(use_https and fp)
        self.monitor_qr_btn.setEnabled(ok)
        if ok:
            self.monitor_qr_btn.setToolTip(
                "Encode applied monitor URL, user, password, and TLS fingerprint for the Android companion."
            )
        elif not use_https:
            self.monitor_qr_btn.setToolTip("Enable HTTPS (required for remote companion setup).")
        else:
            self.monitor_qr_btn.setToolTip(
                "TLS certificate not ready yet — Apply & restart monitor with HTTPS first."
            )

    def _open_companion_apk_folder(self):
        """Reveal built debug APK dir, else Google Drive apks folder."""
        import subprocess
        candidates = []
        try:
            src_dir = os.path.dirname(os.path.abspath(__file__))
            root = os.path.dirname(src_dir)
            candidates.append(
                os.path.join(root, "android", "app", "build", "outputs", "apk", "debug")
            )
            candidates.append(os.path.join(root, "android", "app", "build", "outputs", "apk"))
        except Exception:
            pass
        candidates.append(r"G:\My Drive\apks")
        target = next((p for p in candidates if p and os.path.isdir(p)), None)
        if not target:
            QMessageBox.information(
                self,
                "APK folder",
                "No companion APK folder found yet.\n\n"
                "Build with: android\\publish-apk-to-drive.ps1 -Build\n"
                "Or assembleDebug, then Drive path: G:\\My Drive\\apks",
            )
            return
        try:
            os.startfile(target)
        except Exception:
            try:
                subprocess.Popen(["explorer", target])
            except Exception as e:
                QMessageBox.warning(self, "APK folder", f"Could not open:\n{target}\n\n{e}")

    def _show_companion_setup_qr(self):
        """Show a QR dialog from applied settings (gate if Companion widgets are dirty)."""
        if self._companion_monitor_widgets_dirty():
            QMessageBox.information(
                self,
                "Setup QR",
                "Companion settings have unsaved changes. Click Apply & restart monitor first, "
                "then show the setup QR.",
            )
            return
        url, user, pwd, fp, use_https, host, port = self._companion_setup_fields_from_settings()
        if not use_https:
            QMessageBox.information(
                self,
                "Setup QR",
                "Enable HTTPS (and preferably Home Wi‑Fi + away bind) before generating a setup QR.",
            )
            return
        if not fp:
            QMessageBox.information(
                self,
                "Setup QR",
                "No TLS fingerprint yet. Apply & restart the web monitor with HTTPS so a "
                "certificate is created, then try again.",
            )
            return

        dark = bool(getattr(self, "dark_mode", True))
        dlg = QDialog(self)
        dlg.setWindowTitle("Companion setup QR")
        dlg.setModal(True)
        tc = theme_colors(dark)
        if dark:
            dlg.setStyleSheet(
                f"QDialog {{ background-color: #151820; color: {tc['text']}; }}"
                f"QLabel {{ color: {tc['text']}; }}"
            )
        root = QVBoxLayout(dlg)
        root.setContentsMargins(ui_px(16), ui_px(14), ui_px(16), ui_px(14))
        root.setSpacing(ui_px(10))

        note = QLabel("Scan with Market Advisor Companion (contains password).")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        root.addWidget(note)

        lan_combo = None
        bind_all = host in ("0.0.0.0", "::", "*")
        if bind_all:
            lan_row = QHBoxLayout()
            lan_row.addWidget(QLabel("Phone LAN IP:"))
            lan_combo = QComboBox()
            lan_combo.setEditable(True)
            for ip in companion_qr.list_lan_ips():
                lan_combo.addItem(ip)
            if lan_combo.count() == 0:
                lan_combo.addItem(companion_qr.detect_lan_ip())
            lan_combo.setCurrentIndex(0)
            lan_row.addWidget(lan_combo, 1)
            root.addLayout(lan_row)

        url_lbl = QLabel(url)
        url_lbl.setWordWrap(True)
        url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        url_lbl.setStyleSheet(f"color: {tc['text']}; font-size: {ui_px(11)}px;")
        root.addWidget(url_lbl)

        qr_lbl = QLabel()
        qr_lbl.setAlignment(Qt.AlignCenter)
        root.addWidget(qr_lbl)

        def _regen():
            lan = None
            if lan_combo is not None:
                lan = (lan_combo.currentText() or "").strip() or None
            u, u_user, u_pwd, u_fp, *_ = self._companion_setup_fields_from_settings(lan_ip=lan)
            try:
                payload = companion_qr.encode_setup_payload(u, u_user, u_pwd, u_fp)
                png = companion_qr.qr_png_bytes(payload)
            except Exception as e:
                QMessageBox.warning(self, "Setup QR", f"Could not build QR: {e}")
                return
            url_lbl.setText(u)
            pm = QPixmap()
            pm.loadFromData(png)
            qr_lbl.setPixmap(
                pm.scaled(ui_px(280), ui_px(280), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )

        if lan_combo is not None:
            lan_combo.currentTextChanged.connect(lambda *_: _regen())
            lan_combo.editTextChanged.connect(lambda *_: _regen())

        _regen()

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        close_btn = buttons.button(QDialogButtonBox.Close)
        if close_btn:
            close_btn.clicked.connect(dlg.accept)
        root.addWidget(buttons)
        dlg.exec_()

    def _monitor_control_from_http(self, broker, armed):
        """Called on the monitor HTTP thread; blocks until the GUI handles it."""
        req = {
            "broker": broker,
            "armed": bool(armed),
            "event": threading.Event(),
            "result": {"ok": False, "error": "timeout"},
        }
        self._monitor_control_req.emit(req)
        if not req["event"].wait(timeout=8.0):
            return {"ok": False, "error": "Timed out waiting for desktop app"}
        return req.get("result") or {"ok": False, "error": "No result"}

    def _broker_arm_reject_reason(self, broker):
        """Specific reason companion cannot arm this broker right now, or None if eligible."""
        if broker not in BROKER_NAMES:
            return f"Unknown broker: {broker}"
        if self.paper_mode:
            return None
        b = self.brokers.get(broker)
        if b is None:
            return f"Unknown broker: {broker}"
        if getattr(self, "_broker_manual_auth_needed", {}).get(broker):
            return f"Cannot arm {broker}: reauthorization needed — connect in Settings"
        if not getattr(b, "is_connected", False):
            return f"Cannot arm {broker}: disconnected — connect in Settings"
        if broker == "Coinbase" and not bool(self.settings.get("coinbase_live_trading", True)):
            return f"Cannot arm {broker}: live trading is off"
        if broker == "E*TRADE":
            if getattr(b, "environment", "sandbox") == "live" and not bool(
                self.settings.get("etrade_live_trading", False)
            ):
                return f"Cannot arm {broker}: live trading is off"
        return None

    def _on_monitor_control_req(self, req):
        """Main-thread handler for companion arm/disarm POSTs."""
        try:
            broker = str((req or {}).get("broker") or "")
            armed = bool((req or {}).get("armed"))
            if broker not in BROKER_NAMES:
                req["result"] = {"ok": False, "error": f"Unknown broker: {broker}"}
                return
            if not bool(self.settings.get("monitor_controls_enabled", False)):
                req["result"] = {"ok": False, "error": "Companion controls disabled"}
                return
            if armed:
                if self.auto_trade_enabled.get(broker):
                    req["result"] = {"ok": True, "broker": broker, "armed": True, "note": "already armed"}
                    self.log_event(f"[Companion] Arm {broker}: already armed")
                    return
                reason = self._broker_arm_reject_reason(broker)
                if reason:
                    req["result"] = {"ok": False, "error": reason}
                    self.log_event(f"[Companion] Arm {broker} REJECTED: {reason}")
                    return
                got = self._arm_broker_engines([broker], warn=False)
                if broker not in got:
                    fallback = self._broker_arm_reject_reason(broker) or (
                        f"Cannot arm {broker} (disconnected or live trading disabled)"
                    )
                    req["result"] = {"ok": False, "error": fallback}
                    self.log_event(f"[Companion] Arm {broker} REJECTED")
                    return
                self._log_armed_brokers(got)
                self._update_autotrade_ui()
                self.publish_monitor_status()
                self.log_event(f"[Companion] Auto-Trader ARMED for {broker}")
                req["result"] = {"ok": True, "broker": broker, "armed": True}
            else:
                was_on = bool(self.auto_trade_enabled.get(broker))
                self._disarm_broker(
                    broker,
                    notify_discord=was_on,
                    clear_arm_intent=(broker == "E*TRADE"),
                )
                self.publish_monitor_status()
                self.log_event(f"[Companion] Auto-Trader DISARMED for {broker}")
                req["result"] = {"ok": True, "broker": broker, "armed": False}
        except Exception as e:
            try:
                req["result"] = {"ok": False, "error": str(e)}
            except Exception:
                pass
            self.log_event(f"[Companion] Control error: {e}")
        finally:
            try:
                req["event"].set()
            except Exception:
                pass

    def _monitor_desk_radar_payload(self):
        try:
            import desk_radar
            return desk_radar.top_radar(8)
        except Exception:
            return getattr(self, "_last_desk_radar", None) or []

    def _monitor_advisor_payload(self):
        try:
            import advisor_queue as aq
            return aq.monitor_payload(limit=5)
        except Exception:
            return {"count": 0, "pending": []}

    def _monitor_signal_alert_payload(self):
        try:
            import desk_radar
            return desk_radar.latest_signal_alert()
        except Exception:
            return None

    def _monitor_etrade_oauth_from_http(self, action, verifier=""):
        """Called on the monitor HTTP thread; blocks until the GUI handles it."""
        req = {
            "action": str(action or ""),
            "verifier": str(verifier or "").strip(),
            "event": threading.Event(),
            "result": {"ok": False, "error": "timeout"},
        }
        self._monitor_etrade_oauth_req.emit(req)
        if not req["event"].wait(timeout=20.0):
            return {"ok": False, "error": "Timed out waiting for desktop app"}
        return req.get("result") or {"ok": False, "error": "No result"}

    def _companion_etrade_creds(self):
        """Build E*TRADE creds from saved settings (no Settings dialog widgets required)."""
        env = str(self.settings.get("etrade_environment", "sandbox") or "sandbox").lower()
        if env not in ("live", "sandbox"):
            env = "sandbox"
        key = (self.settings.get("etrade_consumer_key") or "").strip()
        if env == "live":
            key = (self.settings.get("etrade_prod_consumer_key_pending") or key or "").strip()
        secret = ""
        try:
            from etrade_broker import load_etrade_secret
            secret = (load_etrade_secret("consumer_secret", env) or "").strip()
        except Exception:
            secret = ""
        return {
            "environment": env,
            "consumer_key": key,
            "consumer_secret": secret,
            "live_trading_enabled": bool(self.settings.get("etrade_live_trading", False)),
            "account_id_key": self.settings.get("etrade_account_id_key", ""),
            "token_expires_at": self.settings.get("etrade_token_expires_at", 0),
        }

    def _on_monitor_etrade_oauth_req(self, req):
        """Main-thread handler for companion E*TRADE OAuth start/complete."""
        try:
            if not bool(self.settings.get("monitor_controls_enabled", False)):
                req["result"] = {"ok": False, "error": "Companion controls disabled"}
                return
            action = str((req or {}).get("action") or "").lower().strip()
            et = self.brokers.get("E*TRADE")
            if et is None:
                req["result"] = {"ok": False, "error": "E*TRADE broker unavailable"}
                return
            creds = self._companion_etrade_creds()
            if not creds.get("consumer_key") or not creds.get("consumer_secret"):
                req["result"] = {
                    "ok": False,
                    "error": "E*TRADE keys missing on desktop — complete Setup once in Settings",
                }
                return
            if action == "start":
                creds["start_oauth"] = True
                ok, msg = et.login(creds)
                if ok and str(msg).startswith("AUTH_URL::"):
                    url = str(msg).split("AUTH_URL::", 1)[1]
                    self.log_event("[Companion] E*TRADE OAuth started — authorize URL issued")
                    req["result"] = {"ok": True, "authorize_url": url}
                else:
                    req["result"] = {"ok": False, "error": str(msg or "Could not start OAuth")}
                return
            if action == "complete":
                verifier = str((req or {}).get("verifier") or "").strip()
                if not verifier:
                    req["result"] = {"ok": False, "error": "Missing verification code"}
                    return
                creds["verifier"] = verifier
                ok, msg = et.login(creds)
                if not ok:
                    if _is_manual_auth_failure(msg):
                        self._broker_manual_auth_needed["E*TRADE"] = True
                        self._update_autotrade_ui()
                        self.publish_monitor_status()
                    req["result"] = {"ok": False, "error": str(msg or "OAuth failed")}
                    return
                self._broker_manual_auth_needed["E*TRADE"] = False
                if hasattr(self, "_reauth_nudge_sent"):
                    self._reauth_nudge_sent["E*TRADE"] = False
                    self._reauth_nudge_sent["E*TRADE_SOON"] = False
                if hasattr(self, "_update_reauth_banner"):
                    self._update_reauth_banner()
                self.settings["etrade_environment"] = et.environment
                self.settings["etrade_account_id_key"] = et.account_id_key or ""
                self.settings["etrade_token_expires_at"] = float(et.token_expires_at or 0)
                try:
                    save_settings(self.settings)
                except Exception:
                    pass
                self._set_broker_status(
                    "E*TRADE", "🟢 Connected", "color: #00E676; font-weight: bold;"
                )
                self.log_event(
                    f"[Companion] E*TRADE reauth complete ({et.environment}) "
                    f"account={et.account_id_key}"
                )
                self.refresh_account_balances()
                self._update_autotrade_ui()
                self._maybe_restore_etrade_arm(source="companion_oauth")
                self.publish_monitor_status()
                req["result"] = {
                    "ok": True,
                    "armed": bool(self.auto_trade_enabled.get("E*TRADE")),
                    "environment": str(et.environment or ""),
                }
                return
            req["result"] = {"ok": False, "error": f"Unknown OAuth action: {action}"}
        except Exception as e:
            try:
                req["result"] = {"ok": False, "error": str(e)}
            except Exception:
                pass
            self.log_event(f"[Companion] E*TRADE OAuth error: {e}")
        finally:
            try:
                req["event"].set()
            except Exception:
                pass

    def publish_monitor_status(self):
        """Push a read-only snapshot to the web monitor (safe to call often)."""
        try:
            totals = getattr(self, "_last_balance_totals", {}) or {}
            balances = {}
            combined_eq = combined_cash = combined_pnl = 0.0
            holdings_count = {}
            brokers_snap = {}
            for name in BROKER_NAMES:
                p_val = float(totals.get(name, {}).get("p_val", 0.0) or 0.0)
                bp = float(totals.get(name, {}).get("bp", 0.0) or 0.0)
                start = self.session_starts.get(name)
                pl = (p_val - start) if start is not None and start > 0 else 0.0
                balances[name] = {"equity": p_val, "cash": bp, "day_pnl": pl}
                combined_eq += p_val
                combined_cash += bp
                combined_pnl += pl
                holdings_count[name] = int(
                    (getattr(self, "_holdings_count_cache", {}) or {}).get(name, 0) or 0
                )
                b = self.brokers.get(name)
                connected = bool(getattr(b, "is_connected", False)) or self.paper_mode
                live_trading = True
                if name == "Coinbase":
                    live_trading = bool(self.settings.get("coinbase_live_trading", True)) or self.paper_mode
                elif name == "E*TRADE":
                    et_live = bool(self.settings.get("etrade_live_trading", False))
                    env = getattr(b, "environment", "sandbox") if b else "sandbox"
                    live_trading = (env != "live" or et_live) or self.paper_mode
                reauth = bool(getattr(self, "_broker_manual_auth_needed", {}).get(name))
                armed = bool(self.auto_trade_enabled.get(name))
                dd_paused = False
                dd_reason = ""
                try:
                    from scoring import get_drawdown_status
                    bid = {
                        "Robinhood": "ROBINHOOD",
                        "Coinbase": "COINBASE",
                        "E*TRADE": "ETRADE",
                    }.get(name, name)
                    st = get_drawdown_status(bid)
                    dd_paused = bool(st.get("paused"))
                    dd_reason = str(st.get("pause_reason") or "")
                except Exception:
                    pass
                brokers_snap[name] = {
                    "connected": connected,
                    "live_trading": live_trading,
                    "reauth_needed": reauth,
                    "armed": armed,
                    "dd_pause": dd_paused,
                    "dd_reason": dd_reason,
                }
                if name == "E*TRADE":
                    brokers_snap[name]["environment"] = str(
                        getattr(b, "environment", None)
                        or self.settings.get("etrade_environment", "sandbox")
                    )
                    brokers_snap[name]["protective_stops"] = False
                    brokers_snap[name]["buying_power"] = bp
                    env_l = str(brokers_snap[name]["environment"]).lower()
                    min_d = float(self.settings.get("min_trade_dollars", 5.0) or 5.0)
                    low = bp < max(0.01, min_d)
                    brokers_snap[name]["sandbox_no_bp"] = env_l == "sandbox" and low
                    brokers_snap[name]["live_zero_bp"] = env_l == "live" and low
                    brokers_snap[name]["buy_engines_parked"] = (
                        brokers_snap[name]["sandbox_no_bp"]
                        or brokers_snap[name]["live_zero_bp"]
                    )
            balances["combined"] = {
                "equity": combined_eq,
                "cash": combined_cash,
                "day_pnl": combined_pnl,
            }
            locked_capital = _auto_cycle.build_monitor_locked_capital(
                getattr(self, "_last_locked_by_broker", None),
                getattr(self, "_heat_holdings_by_broker", None),
                BROKER_NAMES,
                locked_summary=getattr(self, "_last_locked_summary", None),
            )
            market = "Unknown"
            if hasattr(self, "market_status_lbl"):
                txt = self.market_status_lbl.text() or ""
                market = txt.replace("Market: ", "").strip() or market
            queue = []
            for item in list(getattr(self, "task_queue", []) or []):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    queue.append(f"{item[0]}:{item[1]}")
                else:
                    queue.append(str(item))
            trades = []
            for row in journal.read_recent(15):
                trades.append({
                    "timestamp": row.get("timestamp", ""),
                    "broker": row.get("broker", ""),
                    "side": row.get("side", ""),
                    "ticker": row.get("ticker", ""),
                    "status": row.get("status", ""),
                })
            monitor.update_status({
                "mode": "PAPER" if self.paper_mode else "LIVE",
                "market": market,
                "auto_trader": {
                    name: bool(self.auto_trade_enabled.get(name))
                    for name in BROKER_NAMES
                },
                "brokers": brokers_snap,
                "banner": (
                    self.at_status_lbl.text()
                    if hasattr(self, "at_status_lbl") and self.at_status_lbl.text().strip()
                    else getattr(self, "_monitor_banner", "—")
                ),
                "balances": balances,
                "queue": queue,
                "recent_trades": trades,
                "recent_log": list(getattr(self, "_recent_log_lines", []) or [])[-40:],
                "holdings_count": holdings_count,
                "cluster_heat": getattr(self, "_last_cluster_heat", []) or [],
                "protective_health": getattr(self, "_last_protective_health", {}) or {},
                "portfolio_heat": getattr(self, "_last_portfolio_heat", {}) or {},
                "locked_capital": locked_capital,
                "shadow_guard": getattr(self, "_last_shadow_guard", None)
                or getattr(self, "_shadow_guard_active", None)
                or {},
                "walk_forward": {
                    "journal": getattr(self, "_last_journal_walkforward", {}) or {},
                    "bar": getattr(self, "_last_bar_walkforward", {}) or {},
                },
                "frac_policy": {
                    "prefer_whole_shares": bool(
                        self.settings.get("prefer_whole_shares_for_stops", True)
                    ),
                    "allow_ttp_only": bool(
                        self.settings.get("allow_fractional_ttp_only", True)
                    ),
                    "last": getattr(self, "_last_frac_policy", None) or {},
                },
                "etrade": self._etrade_monitor_snapshot(),
                "halted": bool(getattr(self, "_panic_halted", False)),
                "armed_any": any(self.auto_trade_enabled.values()),
                "desk_radar": self._monitor_desk_radar_payload(),
                "signal_alert": self._monitor_signal_alert_payload(),
                "advisor": self._monitor_advisor_payload(),
                "overnight_scorecard": getattr(self, "_last_overnight_scorecard", {}) or {},
                "execution_quality": getattr(self, "_last_execution_quality", {}) or {},
                "capital_planner": getattr(self, "_last_capital_planner", {}) or {},
            })
        except Exception as e:
            now = time.time()
            last = float(getattr(self, "_monitor_publish_fail_log_at", 0.0) or 0.0)
            if now - last >= 60.0:
                self._monitor_publish_fail_log_at = now
                try:
                    self.log_event(f"[Companion] publish_monitor_status failed: {e}")
                except Exception:
                    pass

    def _now_et(self):
        """US Eastern — Robinhood equity sessions are always quoted in ET."""
        return datetime.now(ZoneInfo("America/New_York"))

    def _etrade_monitor_snapshot(self):
        """Compact E*TRADE env / BP stub for web monitor + companion."""
        et = self.brokers.get("E*TRADE")
        env = str(
            getattr(et, "environment", None) or self.settings.get("etrade_environment", "sandbox")
        ).lower()
        live_ok = bool(
            getattr(et, "live_trading_enabled", False)
            or self.settings.get("etrade_live_trading", False)
        )
        bp = 0.0
        try:
            totals = getattr(self, "_last_balance_totals", {}) or {}
            bp = float((totals.get("E*TRADE") or {}).get("bp") or 0)
        except Exception:
            bp = 0.0
        min_d = float(self.settings.get("min_trade_dollars", 5.0) or 5.0)
        low = bp < max(0.01, min_d)
        sandbox_no_bp = env == "sandbox" and low
        live_zero_bp = env == "live" and low
        try:
            note = _decision_log.etrade_path_honesty_note(
                environment=env,
                live_trading=live_ok,
                buying_power=bp,
                paper_mode=bool(self.paper_mode),
                min_trade_dollars=min_d,
            )
        except Exception:
            note = (
                "Sandbox / no BP stub"
                if sandbox_no_bp
                else ("Live · orders ON" if (env == "live" and live_ok) else (
                    "Live · orders OFF" if env == "live" else "Sandbox"
                ))
            )
        return {
            "environment": env,
            "live_trading": live_ok,
            "buying_power": bp,
            "sandbox_no_bp": sandbox_no_bp,
            "live_zero_bp": live_zero_bp,
            "buy_engines_parked": sandbox_no_bp or live_zero_bp,
            "protective_stops": False,
            "note": note,
        }

    def get_equity_session_info(self):
        """
        Robinhood equity session map (Eastern Time):
          REGULAR   9:30–16:00  — fractionals OK
          EXTENDED  7:00–9:30 and 16:00–20:00 — fractionals OK only until 19:30 after close
          OVERNIGHT 20:00–7:00 weekdays — whole shares only (no fractionals)
          WEEKEND / CLOSED — no equity trading
        """
        now = self._now_et()
        if now.weekday() >= 5:
            return {
                "label": "WEEKEND",
                "market_hours": "regular_hours",
                "use_ext": False,
                "fractional_ok": False,
                "equity_tradeable": False,
            }
        try:
            from market_calendar import is_nyse_holiday
            if is_nyse_holiday(now.date()):
                return {
                    "label": "HOLIDAY",
                    "market_hours": "regular_hours",
                    "use_ext": False,
                    "fractional_ok": False,
                    "equity_tradeable": False,
                }
        except Exception:
            pass

        t = now.hour + now.minute / 60.0

        # Regular market
        if 9.5 <= t < 16.0:
            return {
                "label": "REGULAR",
                "market_hours": "regular_hours",
                "use_ext": False,
                "fractional_ok": True,
                "equity_tradeable": True,
            }

        # Premarket extended (fractionals allowed)
        if 7.0 <= t < 9.5:
            return {
                "label": "EXTENDED",
                "market_hours": "extended_hours",
                "use_ext": True,
                "fractional_ok": True,
                "equity_tradeable": True,
            }

        # After-hours: fractionals until 7:30 PM ET; whole shares until 8:00 PM ET
        if 16.0 <= t < 19.5:
            return {
                "label": "EXTENDED",
                "market_hours": "extended_hours",
                "use_ext": True,
                "fractional_ok": True,
                "equity_tradeable": True,
            }
        if 19.5 <= t < 20.0:
            return {
                "label": "EXTENDED",
                "market_hours": "extended_hours",
                "use_ext": True,
                "fractional_ok": False,
                "equity_tradeable": True,
            }

        # Overnight / 24hr market window — RH: whole shares only
        if t >= 20.0 or t < 7.0:
            return {
                "label": "OVERNIGHT",
                "market_hours": "all_day_hours",
                "use_ext": True,
                "fractional_ok": False,
                "equity_tradeable": True,
            }

        return {
            "label": "CLOSED",
            "market_hours": "regular_hours",
            "use_ext": False,
            "fractional_ok": False,
            "equity_tradeable": False,
        }

    def _sync_equity_session_state(self, session=None):
        """Clear per-session defer bookkeeping when the equity window changes."""
        session = session or self.get_equity_session_info()
        label = session.get("label") or "UNKNOWN"
        prev = getattr(self, "_last_equity_session_label", None)
        if prev != label:
            self._last_equity_session_label = label
            self._sell_defer_log = {}
            if label == "REGULAR":
                # Extended-hours ineligible list only applies outside regular hours
                self._frac_ext_ineligible = set()
        return session

    def _rh_equity_sell_defer_reason(self, ticker, shares_val, price, asset_type, session):
        """
        If this RH equity sell cannot succeed in the current session, return a short reason.
        Crypto / Coinbase always return None (24/7).
        """
        return _auto_cycle.rh_equity_sell_defer_reason(
            ticker, shares_val, price, asset_type, session or {},
            frac_ext_ineligible=getattr(self, "_frac_ext_ineligible", None),
            known_cryptos=KNOWN_CRYPTOS,
        )

    def _note_deferred_sell(self, broker, ticker, reason, session_label, notes):
        """Log a deferred sell once per ticker/reason for this session label."""
        store = getattr(self, "_sell_defer_log", None)
        if store is None:
            self._sell_defer_log = {}
            store = self._sell_defer_log
        _auto_cycle.note_deferred_sell(
            store, notes, broker, ticker, reason, session_label,
        )

    def _mark_frac_ext_ineligible(self, ticker, status):
        """Remember tickers RH rejects for extended-hours fractionals."""
        st = str(status or "").lower()
        if "ext. hours fractional not eligible" in st or "ext. hours fractional rejected" in st:
            self._frac_ext_ineligible.add(str(ticker).upper())

    def is_extended_hours_active(self):
        """True outside regular 9:30–4 ET (extended or overnight)."""
        return self.get_equity_session_info()["label"] in ("EXTENDED", "OVERNIGHT", "WEEKEND")

    def is_equity_session_active(self):
        """
        Robinhood stock scanners/trades during RH extended+regular window (7am–8pm ET).
        Overnight whole-share trading is handled separately at order time.
        Crypto still runs 24/7 separately.
        """
        info = self.get_equity_session_info()
        return info["label"] in ("REGULAR", "EXTENDED")

    def _enqueue_session_boundary_cycles(self, kind, now_ts):
        """
        Priority equity pulse at RTH open/close boundaries.
        PORTFOLIO first (deferred sells), then CORE / BREAKOUT buys. Crypto unchanged.
        Runs for every armed equity broker (Robinhood + E*TRADE).
        """
        labels = {
            "pre_open": "Pre-open session check…",
            "open": "Open session check…",
            "pre_close": "Pre-close session check…",
        }
        self.log_event(labels.get(kind, f"Session boundary check ({kind})…"))

        if kind == "pre_close":
            try:
                self._run_eod_protective_pass()
            except Exception as e:
                self.log_event(f"[EOD] Protective pass error: {e}")

        equity_brokers = [
            b for b in BROKER_NAMES
            if self.auto_trade_enabled.get(b)
            and self._broker_supports(b, "supports_equities")
        ]
        if not equity_brokers:
            return

        now_ts = float(now_ts or time.time())
        priority = []
        for broker in equity_brokers:
            if (
                not self.paper_mode
                and not self.brokers[broker].is_connected
            ):
                continue
            idle_why = self._buy_engines_idle_reason(broker)
            if idle_why:
                self._throttled_log(
                    f"{broker}:buy_engines_idle",
                    f"[{broker}] {idle_why} — session-boundary buys skipped",
                    cooldown_sec=780,
                )
                tasks = ("PORTFOLIO",)
            elif kind == "pre_close":
                tasks = ("PORTFOLIO",)
            else:
                tasks = ("PORTFOLIO", "CORE", "PENNY")
            for task_name in tasks:
                item = (broker, task_name)
                if item in self.task_queue:
                    self.task_queue.remove(item)
                priority.append(item)
            self.last_port_time[broker] = now_ts
            if "CORE" in tasks:
                self.last_core_time[broker] = now_ts
                self.last_penny_time[broker] = now_ts

        if not priority:
            return
        self.task_queue = priority + self.task_queue
        who = ", ".join(dict.fromkeys(b for b, _ in priority))
        self._set_engine_banner(
            f"🤖 ⏰ [{who}] {labels.get(kind, 'Session check')} — queued",
            "#00897B",
        )

    def _maybe_session_boundary_wakeup(self, now_ts):
        """
        Once per equity session day (ET): cycles ~60s before 9:30 open, at/just after open,
        and ~60s before 16:00 close. Weekends and NYSE full holidays skipped.
        """
        equity_armed = any(
            self.auto_trade_enabled.get(b)
            and self._broker_supports(b, "supports_equities")
            for b in BROKER_NAMES
        )
        if not equity_armed:
            return

        now_et = self._now_et()
        if now_et.weekday() >= 5:
            return
        try:
            from market_calendar import is_equity_session_day
            if not is_equity_session_day(now_et.date()):
                return
        except Exception:
            pass

        day = now_et.date()
        fired = self._session_wakeup_fired
        if fired.get("day") != day:
            self._session_wakeup_fired = {
                "day": day, "pre_open": False, "open": False, "pre_close": False,
            }
            fired = self._session_wakeup_fired

        sod = now_et.hour * 3600 + now_et.minute * 60 + now_et.second
        open_s = 9 * 3600 + 30 * 60   # 09:30 ET
        close_s = 16 * 3600           # 16:00 ET

        # ~45s windows so the 1Hz director catches each once without spam
        if not fired["pre_open"] and (open_s - 60) <= sod < (open_s - 15):
            fired["pre_open"] = True
            self._enqueue_session_boundary_cycles("pre_open", now_ts)
        elif not fired["open"] and open_s <= sod < (open_s + 30):
            fired["open"] = True
            self._enqueue_session_boundary_cycles("open", now_ts)
        elif not fired["pre_close"] and (close_s - 60) <= sod < (close_s - 15):
            fired["pre_close"] = True
            self._enqueue_session_boundary_cycles("pre_close", now_ts)

    def update_market_status(self):
        """Broker-aware session label. Coinbase/crypto is 24/7; equities follow US hours."""
        if not hasattr(self, "market_status_lbl"):
            return

        tc = theme_colors(self.dark_mode)
        open_color = tc["success"]
        warn_color = tc["warn"]
        closed_color = tc["danger"]

        view = getattr(self, "view_mode", "All")
        info = self.get_equity_session_info()
        label = info["label"]

        if label == "WEEKEND":
            equity_text, equity_color = "WEEKEND", warn_color
        elif label == "HOLIDAY":
            equity_text, equity_color = "HOLIDAY", warn_color
        elif label == "REGULAR":
            equity_text, equity_color = "REGULAR", open_color
        elif label == "EXTENDED":
            if info["fractional_ok"]:
                equity_text, equity_color = "EXTENDED", warn_color
            else:
                equity_text, equity_color = "EXTENDED (whole only)", warn_color
        elif label == "OVERNIGHT":
            equity_text, equity_color = "OVERNIGHT (whole only)", warn_color
        else:
            equity_text, equity_color = "CLOSED", closed_color

        if view == "Coinbase":
            text, color = "Crypto: OPEN 24/7", open_color
        elif view in ("Robinhood", "E*TRADE"):
            text, color = f"Equities: {equity_text}", equity_color
        else:
            text = f"Equities: {equity_text}  ·  Crypto: 24/7"
            color = equity_color if label != "REGULAR" else open_color

        self.market_status_lbl.setText(text)
        self.market_status_lbl.setStyleSheet(
            f"font-size: {ui_px(14)}px; font-weight: 600; color: {color}; "
            f"padding: {ui_px(2)}px {ui_px(10)}px {ui_px(2)}px {ui_px(8)}px;"
        )
        self.market_status_lbl.setToolTip(
            "Robinhood / E*TRADE equities (ET):\n"
            "• Regular 9:30am–4pm — fractionals OK\n"
            "• Extended 7–9:30am & 4–8pm — fractionals until ~7:30pm after-hours\n"
            "• Overnight 8pm–7am — whole shares only (no fractionals)\n"
            "Coinbase crypto trades 24/7."
            if view != "Coinbase"
            else "Coinbase Advanced crypto markets are open 24/7."
        )
    def safe_delay(self, ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec_()

    def run_thread(self, target_func, on_success_callback, *args, unlock_queue_on_error=False):
        task = BackgroundTask(target_func, *args)

        def _on_error(e):
            fname = getattr(target_func, "__name__", "unknown")
            # Lambdas hide the real worker — unwrap common cycle wrappers
            if fname == "<lambda>":
                try:
                    closed = getattr(target_func, "__closure__", None) or ()
                    for cell in closed:
                        try:
                            val = cell.cell_contents
                        except Exception:
                            continue
                        if callable(val) and getattr(val, "__name__", "").startswith("_bg"):
                            fname = val.__name__
                            break
                except Exception:
                    pass
            err_text = str(e or "")
            summary = err_text.splitlines()[0] if err_text else "unknown error"
            self.log_event(f"Thread Error in {fname}: {summary}")
            for line in err_text.splitlines()[1:12]:
                if line.strip():
                    self.log_event(f"  {line}")
            self.set_working_state(False)
            if unlock_queue_on_error:
                if _is_manual_auth_failure(summary):
                    broker = getattr(self, "cycle_broker_name", None) or getattr(
                        self, "_cycle_broker", None
                    )
                    self._handle_broker_auth_failure(
                        broker, summary, source=f"cycle:{fname}"
                    )
                else:
                    self._send_cycle_error_discord(fname, summary)
                self.cycle_finished()

        task.result_ready.connect(on_success_callback)
        task.error_occurred.connect(_on_error)
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def _send_cycle_error_discord(self, fname, err):
        """Discord identical cycle errors at most once per 10 minutes (log always).

        Auth / 401 failures must not use this path — see _handle_broker_auth_failure.
        """
        if _is_manual_auth_failure(err):
            broker = getattr(self, "cycle_broker_name", None) or getattr(
                self, "_cycle_broker", None
            )
            self._handle_broker_auth_failure(broker, err, source=f"cycle:{fname}")
            return
        key = f"{self.cycle_broker_name}|{fname}|{err}"
        now = time.time()
        cool = getattr(self, "_cycle_error_discord_cooldown", None)
        if cool is None:
            self._cycle_error_discord_cooldown = {}
            cool = self._cycle_error_discord_cooldown
        prev = cool.get(key)
        if prev and (now - float(prev[0])) < 600:
            cool[key] = (prev[0], int(prev[1]) + 1)
            return
        suppressed = int(prev[1]) if prev else 0
        cool[key] = (now, 0)
        extra = f" (×{suppressed + 1} since last alert)" if suppressed else ""
        self.send_discord_alert(f"🚨 Cycle thread error in {fname}: {err}{extra}")

    def run_cycle_thread(self, target_func, on_success_callback, *args):
        """Background work for auto-trade cycles — unlocks the queue if the worker crashes."""
        self.run_thread(target_func, on_success_callback, *args, unlock_queue_on_error=True)

    def log_event(self, message):
        timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        log_line = f"{timestamp} {message}"

        if not hasattr(self, "_full_log_lines"):
            self._full_log_lines = []
        self._full_log_lines.append(log_line)
        self._full_log_lines = self._full_log_lines[-ACTIVITY_LOG_UI_MAX_LINES:]

        if not hasattr(self, "_recent_log_lines"):
            self._recent_log_lines = []
        self._recent_log_lines.append(log_line)
        self._recent_log_lines = self._recent_log_lines[-80:]

        # Always marshal UI updates via queued signal (safe from worker threads)
        self._log_line_ready.emit(log_line)
        try:
            if sys.stdout is not None:
                print(log_line)
        except Exception:
            pass
        try:
            with open(ACTIVITY_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
            n = getattr(self, "_activity_log_disk_writes", 0) + 1
            self._activity_log_disk_writes = n
            if n % ACTIVITY_LOG_DISK_CHECK_EVERY == 0:
                _rotate_activity_log_if_needed()
        except Exception:
            pass

    def _append_log_line_ui(self, log_line):
        if not hasattr(self, "log_text_edit"):
            return
        if not self._log_line_matches_filter(log_line, self._activity_log_filter()):
            return
        self.log_text_edit.append(log_line)
        n = getattr(self, "_log_ui_append_count", 0) + 1
        self._log_ui_append_count = n
        # Cap QTextEdit growth to match in-memory buffer (unbounded append freezes UI after long runs)
        try:
            too_many = self.log_text_edit.document().blockCount() > ACTIVITY_LOG_UI_REBUILD_AT
        except Exception:
            too_many = False
        if too_many or n >= ACTIVITY_LOG_UI_REBUILD_EVERY:
            self._log_ui_append_count = 0
            self._refresh_activity_log_view()

    def _activity_log_filter(self):
        if hasattr(self, "log_filter_combo"):
            return self.log_filter_combo.currentText() or "All"
        return "All"

    def _log_line_matches_filter(self, line, filt):
        """All = everything; broker / Companion filters = lines that mention that source."""
        if not filt or filt == "All":
            return True
        if filt == "Companion":
            return "[Companion]" in (line or "")
        if filt == "E*TRADE":
            return "E*TRADE" in (line or "") or "[E*TRADE]" in (line or "")
        return filt in (line or "")

    def _filtered_log_lines(self):
        lines = getattr(self, "_full_log_lines", None) or []
        filt = self._activity_log_filter()
        if filt == "All":
            return list(lines)
        return [ln for ln in lines if self._log_line_matches_filter(ln, filt)]

    def _refresh_activity_log_view(self):
        if not hasattr(self, "log_text_edit"):
            return
        # Preserve scroll-at-bottom behavior when following the live log
        bar = self.log_text_edit.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        text = "\n".join(self._filtered_log_lines())
        self.log_text_edit.setPlainText(text)
        if follow:
            bar.setValue(bar.maximum())

    def _on_log_filter_changed(self, _text=None):
        self._refresh_activity_log_view()

    def _clear_activity_log(self):
        self._full_log_lines = []
        self._recent_log_lines = []
        self._log_ui_append_count = 0
        if hasattr(self, "log_text_edit"):
            self.log_text_edit.clear()
        try:
            with open(ACTIVITY_LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
            self._activity_log_disk_writes = 0
        except Exception:
            pass
        self.log_event(
            "Activity log cleared (active file). Archives under activity_log_archives/ kept."
        )

    def _open_activity_log_folder(self):
        """Reveal activity_log.txt + archives folder in Explorer."""
        try:
            folder = os.path.dirname(os.path.abspath(ACTIVITY_LOG_FILE))
            arch = os.path.join(folder, "activity_log_archives")
            target = arch if os.path.isdir(arch) else folder
            os.makedirs(folder, exist_ok=True)
            os.startfile(target)
        except Exception as e:
            self.log_event(f"Could not open logs folder: {e}")

    # ---------------------------------------------------------
    #  UI BUILDING & SCREENS
    # ---------------------------------------------------------
    def build_auto_trader_banner(self):
        self.at_status_frame = QFrame()
        self.at_status_frame.setObjectName("autoTraderBanner")
        # Fixed vertical size so arming Auto-Trader never steals Home's layout room
        self.at_status_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.at_status_frame.setMaximumHeight(ui_px(48))
        at_layout = QHBoxLayout(self.at_status_frame)
        at_layout.setContentsMargins(ui_px(10), ui_px(4), ui_px(10), ui_px(4))
        at_layout.setSpacing(ui_px(8))
        self._at_banner_layout = at_layout

        self.bot_animator = BotActivityAnimator(self.at_status_frame)
        self.bot_animator.set_dark(self.dark_mode)

        self.at_status_lbl = QLabel("Auto-Trader Offline")
        self.at_status_lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.at_status_lbl.setWordWrap(False)  # wrapping doubled banner height and clipped Home
        self.at_status_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        at_layout.addWidget(self.bot_animator, 0)
        at_layout.addWidget(self.at_status_lbl, 1)
        self._reset_autotrader_banner_style()
        self.main_layout.addWidget(self.at_status_frame)
        self.at_status_frame.setVisible(False)

    def build_persistent_top_bar(self):
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        # NoFrame + transparent QSS — avoid StyledPanel's opaque grey panel fill.
        top_bar.setFrameShape(QFrame.NoFrame)
        top_bar.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(ui_px(10), ui_px(6), ui_px(10), ui_px(6))
        layout.setSpacing(ui_px(8))
        self._top_bar_layout = layout

        self.broker_dropdown = QComboBox()
        self.broker_dropdown.setObjectName("brokerDropdown")
        self.broker_dropdown.addItems(["All", "Robinhood", "Coinbase", "E*TRADE"])
        self.broker_dropdown.setCurrentText("All")
        self.broker_dropdown.setFixedWidth(ui_px(110))
        self.broker_dropdown.setMaxVisibleItems(5)
        self.broker_dropdown.currentTextChanged.connect(self.on_broker_switch)

        _metric_pol = QSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        tc = theme_colors(self.dark_mode)

        self.portfolio_val_lbl = QLabel("Portfolio: $0.00")
        self.portfolio_val_lbl.setStyleSheet(top_bar_metric_style(tc["accent"], 16))
        self.portfolio_val_lbl.setMinimumWidth(ui_px(168))
        self.portfolio_val_lbl.setSizePolicy(_metric_pol)
        self.portfolio_val_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.buying_power_lbl = QLabel("Buying Power: $0.00")
        self.buying_power_lbl.setStyleSheet(top_bar_metric_style(tc["success"], 16))
        self.buying_power_lbl.setMinimumWidth(ui_px(198))
        self.buying_power_lbl.setSizePolicy(_metric_pol)
        self.buying_power_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.daily_profit_lbl = QLabel("Day P&L: …")
        self.daily_profit_lbl.setStyleSheet(top_bar_metric_style(tc["neutral"], 16))
        self.daily_profit_lbl.setMinimumWidth(ui_px(148))
        self.daily_profit_lbl.setSizePolicy(_metric_pol)
        self.daily_profit_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        metrics_host = QWidget()
        metrics_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        metrics_lay = QHBoxLayout(metrics_host)
        metrics_lay.setContentsMargins(0, 0, 0, 0)
        metrics_lay.setSpacing(ui_px(8))
        self._top_bar_metrics_layout = metrics_lay
        metrics_lay.addWidget(self.portfolio_val_lbl)
        metrics_lay.addWidget(self.buying_power_lbl)
        metrics_lay.addWidget(self.daily_profit_lbl)
        metrics_lay.addStretch(1)

        self.paper_mode_btn = QPushButton("Mode: PAPER" if self.paper_mode else "Mode: LIVE")
        self.paper_mode_btn.setMinimumHeight(ui_px(34))
        self.paper_mode_btn.setMinimumWidth(ui_px(100))
        self.paper_mode_btn.setStyleSheet(
            top_bar_btn_style("#E65100") if self.paper_mode else top_bar_btn_style("#1B5E20")
        )
        self.paper_mode_btn.clicked.connect(self.toggle_paper_mode)

        self.dark_mode_btn = QPushButton("Light" if self.dark_mode else "Dark")
        self.dark_mode_btn.setMinimumHeight(ui_px(34))
        self.dark_mode_btn.setMinimumWidth(ui_px(60))
        self.dark_mode_btn.setToolTip("Toggle light / dark theme")
        self.dark_mode_btn.clicked.connect(self.toggle_dark_mode)

        self.auto_trade_btn = QPushButton("Auto-Trader: OFF")
        self.auto_trade_btn.setMinimumHeight(ui_px(34))
        self.auto_trade_btn.setMinimumWidth(ui_px(128))
        self.auto_trade_btn.setStyleSheet(top_bar_btn_style("#424242"))
        self.auto_trade_btn.setToolTip(
            "When OFF: open the broker picker to arm Auto-Trader. "
            "When ON: turn off all brokers, or choose Change brokers… to manage individually."
        )
        self.auto_trade_btn.clicked.connect(self.toggle_auto_trade)

        self.halt_all_btn = QPushButton("HALT")
        self.halt_all_btn.setMinimumHeight(ui_px(34))
        self.halt_all_btn.setMinimumWidth(ui_px(58))
        self.halt_all_btn.setStyleSheet(top_bar_btn_style("#B71C1C"))
        self.halt_all_btn.setToolTip(
            "Panic Halt All — disarm every broker, clear queues, urgent Discord. "
            "Does not place sells and does not cancel protective stops."
        )
        self.halt_all_btn.clicked.connect(self.panic_halt_all)
        # Visible only while Auto-Trader is armed (synced in _update_autotrade_ui).
        self.halt_all_btn.setVisible(False)

        broker_lbl = QLabel("Broker")
        broker_lbl.setObjectName("brokerHint")
        broker_lbl.setStyleSheet(
            f"color: {theme_colors(self.dark_mode)['muted']}; font-size: {ui_px(13)}px; font-weight: 600;"
        )
        self.broker_hint_lbl = broker_lbl
        layout.addWidget(broker_lbl)
        layout.addWidget(self.broker_dropdown)
        layout.addSpacing(ui_px(2))
        layout.addWidget(metrics_host, 1)
        layout.addWidget(self.paper_mode_btn)
        layout.addWidget(self.dark_mode_btn)
        layout.addWidget(self.auto_trade_btn)
        layout.addWidget(self.halt_all_btn)

        self.main_layout.addWidget(top_bar)

    def _set_stock_tabs_visible(self, visible):
        """Show/hide Breakouts + Core + IPOs (Coinbase has no equities)."""
        # Nested scanner tabs
        st = getattr(self, "scanners_tabs", None)
        if st is not None:
            for inner in (
                getattr(self, "penny_inner_index", 1),
                getattr(self, "core_inner_index", 2),
            ):
                if 0 <= inner < st.count():
                    try:
                        st.setTabVisible(inner, visible)
                    except Exception:
                        st.setTabEnabled(inner, visible)
            if not visible and st.currentIndex() in (
                getattr(self, "penny_inner_index", 1),
                getattr(self, "core_inner_index", 2),
            ):
                st.setCurrentIndex(0)  # Crypto
        # Top-level IPOs
        idx = getattr(self, "ipo_tab_index", -1)
        if 0 <= idx < self.tabs.count():
            if not visible and self.tabs.currentIndex() == idx:
                self.tabs.setCurrentIndex(1)
            try:
                self.tabs.setTabVisible(idx, visible)
            except Exception:
                self.tabs.setTabEnabled(idx, visible)

    def _apply_view_mode_tabs(self):
        # Coinbase-only: hide equity scanners. E*TRADE / All / Robinhood: show them.
        self._set_stock_tabs_visible(self.view_mode != "Coinbase")

    def on_broker_switch(self, broker_name):
        self.view_mode = broker_name
        if broker_name in self.brokers:
            self.active_broker_name = broker_name
        self.log_event(f"Switched view to: {broker_name}")
        self._apply_view_mode_tabs()
        self.update_market_status()
        self._refresh_top_bar_from_cache()
        self.refresh_account_balances()
        self.manual_portfolio_reload(and_score=True, force=True)

    def _refresh_top_bar_from_cache(self):
        totals = getattr(self, '_last_balance_totals', {})
        if self.view_mode == "All":
            p_val = sum(d.get('p_val', 0.0) for d in totals.values())
            bp = sum(d.get('bp', 0.0) for d in totals.values())
            pl_val = 0.0
            for name in BROKER_NAMES:
                start = self.session_starts.get(name)
                cur = totals.get(name, {}).get('p_val', 0.0)
                if start is not None and start > 0:
                    pl_val += cur - start
            self.portfolio_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
            self.buying_power_lbl.setText(f"Buying Power: {format_money(bp)}")
        else:
            name = self.view_mode
            p_val = totals.get(name, {}).get('p_val', 0.0)
            bp = totals.get(name, {}).get('bp', 0.0)
            start = self.session_starts.get(name)
            pl_val = (p_val - start) if start is not None and start > 0 else 0.0
            self.portfolio_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
            self.buying_power_lbl.setText(f"Buying Power: {format_money(bp)}")

        pl_str = format_money(abs(pl_val))
        pl_display = f"+{pl_str}" if pl_val >= 0 else f"-{pl_str}"
        tc = theme_colors(self.dark_mode)
        color = tc["success"] if pl_val > 0.001 else (
            tc["danger"] if pl_val < -0.001 else tc["neutral"]
        )
        self.daily_profit_lbl.setText(f"Day P&L: {pl_display}")
        self.daily_profit_lbl.setStyleSheet(top_bar_metric_style(color, 16))
        # Keep portfolio / cash accents readable after theme flips
        if hasattr(self, "portfolio_val_lbl"):
            self.portfolio_val_lbl.setStyleSheet(top_bar_metric_style(tc["accent"], 16))
        if hasattr(self, "buying_power_lbl"):
            self.buying_power_lbl.setStyleSheet(top_bar_metric_style(tc["success"], 16))
        if hasattr(self, "broker_hint_lbl"):
            self.broker_hint_lbl.setStyleSheet(
                f"color: {tc['muted']}; font-size: {ui_px(13)}px; font-weight: 600;"
            )

    def _update_home_wide_layout(self):
        """
        Responsive Home for default launch size (~920–1200×600–780) up to maximized.
        Split from HOME_SPLIT_MIN_W; only very narrow windows stack.
        """
        w = self.width()
        wide = w >= HOME_SPLIT_MIN_W
        self._home_layout_wide = wide

        nw_lay = getattr(self, "_home_nw_brokers_lay", None)
        if nw_lay is not None:
            nw_lay.setDirection(
                QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom
            )
            # Compact Net Worth + broader brokers (avoids empty NW slab at default)
            nw_lay.setStretch(0, 2 if wide else 1)
            nw_lay.setStretch(1, 5 if wide else 1)

        master = getattr(self, "master_card", None)
        if master is not None:
            if wide:
                # Tidy hero card — not half the row; stretch to match broker stack height
                master.setMaximumWidth(min(ui_px(360), max(ui_px(240), int(w * 0.30))))
                master.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            else:
                master.setMaximumWidth(16777215)
                master.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        brokers_col = getattr(self, "_home_brokers_col", None)
        if brokers_col is not None:
            # Preferred height from stacked broker cards drives the NW+brokers row
            brokers_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # Risk band under money row: Cluster | (Portfolio Heat + Theme caps) — stack under ~840px
        risk_row = getattr(self, "_home_risk_row", None) or getattr(
            self, "_home_cluster_row", None
        )
        if risk_row is not None:
            risk_row.setDirection(
                QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom
            )
            risk_row.setStretch(0, 2 if wide else 1)
            risk_row.setStretch(1, 3 if wide else 1)

        cluster_card = getattr(self, "cluster_card", None)
        if cluster_card is not None:
            if wide:
                # ~1/3 of window, hard-capped so bars never become pink slabs
                cap = min(ui_px(400), max(ui_px(260), int(w * 0.32)))
                cluster_card.setMaximumWidth(cap)
                cluster_card.setMinimumWidth(ui_px(200))
                cluster_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            else:
                cluster_card.setMaximumWidth(16777215)
                cluster_card.setMinimumWidth(0)
                cluster_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        heat_card = getattr(self, "heat_card", None)
        if heat_card is not None:
            heat_card.setMaximumWidth(16777215)
            heat_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        tip = getattr(self, "home_cluster_tip", None)
        tip_card = getattr(self, "home_cluster_tip_card", None)
        if tip is not None:
            tip.setMaximumWidth(16777215)
            tip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        if tip_card is not None:
            tip_card.setMaximumWidth(16777215)
            tip_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        right_col = getattr(self, "_home_risk_right_col", None)
        if right_col is not None:
            right_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

    def _style_home_cards(self):
        """Theme-aware Home card surfaces (also re-run on dark/light toggle)."""
        if self.dark_mode:
            panel, line, mute = "#1A1D24", "#2A2F3A", "#9AA0A6"
            title_fg = "#E8EAED"
        else:
            panel, line, mute = "#FFFFFF", "#C5CAD3", "#3C4043"
            title_fg = "#1A1A1A"
        tc = theme_colors(self.dark_mode)
        # Compact padding — Net Worth shares a row with brokers on wide screens
        master = (
            f"QGroupBox {{ font-size: {ui_px(14)}px; font-weight: 600; color: {title_fg}; "
            f"background-color: {panel}; border: 1px solid {tc['accent']}; "
            f"border-radius: {ui_px(UI_RADIUS_CARD)}px; margin-top: {ui_px(10)}px; "
            f"padding-top: {ui_px(14)}px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: {ui_px(14)}px; "
            f"padding: 0 {ui_px(6)}px; }}"
        )
        broker = (
            f"QGroupBox {{ font-size: {ui_px(12)}px; font-weight: 600; color: {title_fg}; "
            f"background-color: {panel}; border: 1px solid {line}; "
            f"border-radius: {ui_px(UI_RADIUS_CARD)}px; margin-top: {ui_px(8)}px; "
            f"padding-top: {ui_px(12)}px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: {ui_px(12)}px; "
            f"padding: 0 {ui_px(5)}px; color: {mute}; }}"
        )
        if hasattr(self, "master_card"):
            self.master_card.setStyleSheet(master)
        if hasattr(self, "heat_card"):
            self.heat_card.setStyleSheet(broker)
        if hasattr(self, "cluster_card"):
            self.cluster_card.setStyleSheet(broker)
        if hasattr(self, "home_cluster_tip_card"):
            self.home_cluster_tip_card.setStyleSheet(broker)
        if hasattr(self, "rh_card"):
            self.rh_card.setStyleSheet(broker)
        if hasattr(self, "cb_card"):
            self.cb_card.setStyleSheet(broker)
        if hasattr(self, "et_card"):
            self.et_card.setStyleSheet(broker)
        if hasattr(self, "home_master_val_lbl"):
            self.home_master_val_lbl.setStyleSheet(metric_label_style(tc["accent"], 32))
            # Stylesheet fonts don't inflate sizeHint — pin height so $120 isn't clipped
            self.home_master_val_lbl.setMinimumHeight(ui_px(40))
        if hasattr(self, "home_master_bp_lbl"):
            self.home_master_bp_lbl.setStyleSheet(metric_label_style(tc["success"], 14))
            self.home_master_bp_lbl.setMinimumHeight(ui_px(20))
        if hasattr(self, "home_master_pl_lbl"):
            self.home_master_pl_lbl.setMinimumHeight(ui_px(22))
        tip = getattr(self, "home_cluster_tip", None)
        if tip is not None:
            tip.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        for name in (
            "home_rh_val_lbl", "home_rh_bp_lbl",
            "home_cb_val_lbl", "home_cb_bp_lbl",
            "home_et_val_lbl", "home_et_bp_lbl",
        ):
            lbl = getattr(self, name, None)
            if lbl is not None:
                lbl.setStyleSheet(
                    f"font-size: {ui_px(13)}px; font-weight: 600; padding: {ui_px(2)}px 2px;"
                )
                lbl.setMinimumHeight(ui_px(20))
        for name in ("home_rh_pl_lbl", "home_cb_pl_lbl", "home_et_pl_lbl"):
            lbl = getattr(self, name, None)
            if lbl is not None:
                lbl.setMinimumHeight(ui_px(20))

    def build_home_screen(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scroll when Auto-Trader banner steals vertical room — never clip Net Worth / broker rows
        scroll = CompactScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(ui_px(18), ui_px(12), ui_px(18), ui_px(12))
        layout.setSpacing(ui_px(10))
        self._home_layout = layout
        
        title = QLabel("Master Portfolio")
        title.setObjectName("homeTitle")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: {ui_px(18)}px; font-weight: 600; "
            f"margin: {ui_px(2)}px 0 {ui_px(4)}px 0;"
        )
        layout.addWidget(title)

        # E*TRADE reauth / midnight banner (Home)
        self.reauth_banner = QFrame()
        self.reauth_banner.setObjectName("reauthBanner")
        reauth_lay = QHBoxLayout(self.reauth_banner)
        reauth_lay.setContentsMargins(ui_px(12), ui_px(8), ui_px(12), ui_px(8))
        reauth_lay.setSpacing(ui_px(10))
        self.reauth_banner_lbl = QLabel("")
        self.reauth_banner_lbl.setWordWrap(True)
        self.reauth_banner_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.reauth_btn = QPushButton("Reauth E*TRADE")
        self.reauth_btn.setMinimumHeight(ui_px(32))
        self.reauth_btn.setStyleSheet(top_bar_btn_style("#E65100"))
        self.reauth_btn.clicked.connect(self._open_etrade_login_dialog)
        reauth_lay.addWidget(self.reauth_banner_lbl, 1)
        reauth_lay.addWidget(self.reauth_btn, 0)
        self.reauth_banner.setVisible(False)
        layout.addWidget(self.reauth_banner)

        # Money first: Net Worth | Brokers — NW stretches to broker stack height when side-by-side
        nw_host = QWidget()
        nw_brokers = QHBoxLayout(nw_host)
        nw_brokers.setContentsMargins(0, 0, 0, 0)
        nw_brokers.setSpacing(ui_px(12))
        self._home_nw_brokers_lay = nw_brokers

        self.master_card = QGroupBox("Net Worth")
        self.master_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.master_card.setMaximumWidth(ui_px(340))
        mc_layout = QVBoxLayout()
        mc_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
        mc_layout.setSpacing(ui_px(4))
        self._master_card_layout = mc_layout
        
        self.home_master_val_lbl = QLabel("$0.00")
        self.home_master_val_lbl.setAlignment(Qt.AlignCenter)
        self.home_master_val_lbl.setTextInteractionFlags(Qt.NoTextInteraction)
        self.home_master_val_lbl.setMinimumHeight(ui_px(40))
        self.home_master_val_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        
        self.home_master_bp_lbl = QLabel("Combined Liquid Cash: $0.00")
        self.home_master_bp_lbl.setAlignment(Qt.AlignCenter)
        self.home_master_bp_lbl.setWordWrap(True)
        self.home_master_bp_lbl.setMinimumHeight(ui_px(20))
        self.home_master_bp_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.home_master_pl_lbl = QLabel("Combined Day P&L: $0.00")
        self.home_master_pl_lbl.setStyleSheet(
            metric_label_style(theme_colors(self.dark_mode)["neutral"], 16)
        )
        self.home_master_pl_lbl.setAlignment(Qt.AlignCenter)
        self.home_master_pl_lbl.setWordWrap(True)
        self.home_master_pl_lbl.setMinimumHeight(ui_px(22))
        self.home_master_pl_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        reset_pl_row = QHBoxLayout()
        reset_pl_row.addStretch(1)
        self.home_reset_pnl_btn = QPushButton("Reset Day P&L baseline")
        self.home_reset_pnl_btn.setFlat(True)
        self.home_reset_pnl_btn.setToolTip(
            "Set today's Day P&L start to current portfolio values (use after restart "
            "if combined P&L looks wrong). Does not affect autotrader risk rails."
        )
        self.home_reset_pnl_btn.clicked.connect(self._reset_day_pnl_baseline)
        reset_pl_row.addWidget(self.home_reset_pnl_btn)
        reset_pl_row.addStretch(1)
        
        # Vertically distribute total / cash / day P&L when card stretches to brokers
        mc_layout.addStretch(1)
        mc_layout.addWidget(self.home_master_val_lbl)
        mc_layout.addStretch(1)
        mc_layout.addWidget(self.home_master_bp_lbl)
        mc_layout.addWidget(self.home_master_pl_lbl)
        mc_layout.addLayout(reset_pl_row)
        mc_layout.addStretch(1)
        self.master_card.setLayout(mc_layout)

        brokers_col = QWidget()
        brokers_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._home_brokers_col = brokers_col
        brokers_layout = QVBoxLayout(brokers_col)
        brokers_layout.setContentsMargins(0, 0, 0, 0)
        brokers_layout.setSpacing(ui_px(8))
        self._home_brokers_layout = brokers_layout

        self.home_basis_nudge = QLabel("")
        self.home_basis_nudge.setWordWrap(True)
        self.home_basis_nudge.setVisible(False)
        self.home_basis_nudge.setStyleSheet(
            f"color: #F9A825; font-size: {ui_px(12)}px; font-weight: 600; "
            f"padding: {ui_px(4)}px {ui_px(8)}px;"
        )
        self.home_basis_nudge.setToolTip(
            "Holdings with unknown avg cost cannot use honest TTP/ROI/scale-in. "
            "Fix via Settings → Cost basis paste (e.g. Robinhood:SHIB=0.000012)."
        )
        brokers_layout.addWidget(self.home_basis_nudge)

        def _pack_broker_metrics(hbox, labels):
            """Keep portfolio / BP / P&L grouped — don't fling them to opposite edges on wide windows."""
            for lbl in labels:
                lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
                lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                lbl.setMinimumHeight(ui_px(20))
                hbox.addWidget(lbl)
            hbox.addStretch(1)

        self.rh_card = QGroupBox("Robinhood · Equities & Crypto")
        self.rh_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        rh_layout = QHBoxLayout()
        rh_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
        rh_layout.setSpacing(ui_px(20))
        self._rh_card_layout = rh_layout
        
        self.home_rh_val_lbl = QLabel("Portfolio: $0.00")
        self.home_rh_val_lbl.setObjectName("homeBrokerMetric")
        self.home_rh_val_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_rh_bp_lbl = QLabel("Buying Power: $0.00")
        self.home_rh_bp_lbl.setObjectName("homeBrokerMetric")
        self.home_rh_bp_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_rh_pl_lbl = QLabel("Day P&L: $0.00")
        self.home_rh_pl_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_rh_basis_chip = QLabel("Basis: —")
        self.home_rh_basis_chip.setObjectName("homeBrokerMetric")
        tc_rh = theme_colors(self.dark_mode)
        self.home_rh_basis_chip.setStyleSheet(
            f"font-size: {ui_px(11)}px; font-weight: 700; color: {tc_rh['muted']}; "
            f"padding: {ui_px(2)}px {ui_px(8)}px;"
        )
        self.home_rh_basis_chip.setToolTip(
            "Robinhood avg cost from broker when available; else journal / Settings paste / seed.json."
        )
        _pack_broker_metrics(
            rh_layout,
            (self.home_rh_val_lbl, self.home_rh_bp_lbl, self.home_rh_pl_lbl, self.home_rh_basis_chip),
        )
        self.rh_card.setLayout(rh_layout)
        brokers_layout.addWidget(self.rh_card)

        self.cb_card = QGroupBox("Coinbase Advanced · Crypto")
        self.cb_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        cb_layout = QHBoxLayout()
        cb_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
        cb_layout.setSpacing(ui_px(20))
        self._cb_card_layout = cb_layout
        
        self.home_cb_val_lbl = QLabel("Portfolio: $0.00")
        self.home_cb_val_lbl.setObjectName("homeBrokerMetric")
        self.home_cb_val_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_cb_bp_lbl = QLabel("Buying Power: $0.00")
        self.home_cb_bp_lbl.setObjectName("homeBrokerMetric")
        self.home_cb_bp_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_cb_pl_lbl = QLabel("Day P&L: $0.00")
        self.home_cb_pl_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_cb_basis_chip = QLabel("Basis: —")
        self.home_cb_basis_chip.setObjectName("homeBrokerMetric")
        tc_cb = theme_colors(self.dark_mode)
        self.home_cb_basis_chip.setStyleSheet(
            f"font-size: {ui_px(11)}px; font-weight: 700; color: {tc_cb['muted']}; "
            f"padding: {ui_px(2)}px {ui_px(8)}px;"
        )
        self.home_cb_basis_chip.setToolTip(
            "Coinbase avg entry from portfolio breakdown when available; "
            "else journal / tracked / Settings paste. Unknown basis gates TTP/scale-in/ROI."
        )
        _pack_broker_metrics(
            cb_layout,
            (self.home_cb_val_lbl, self.home_cb_bp_lbl, self.home_cb_pl_lbl, self.home_cb_basis_chip),
        )
        self.cb_card.setLayout(cb_layout)
        brokers_layout.addWidget(self.cb_card)

        self.et_card = QGroupBox("E*TRADE · Equities & ETFs")
        self.et_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        et_layout = QHBoxLayout()
        et_layout.setContentsMargins(ui_px(14), ui_px(10), ui_px(14), ui_px(10))
        et_layout.setSpacing(ui_px(20))
        self._et_card_layout = et_layout

        self.home_et_val_lbl = QLabel("Portfolio: $0.00")
        self.home_et_val_lbl.setObjectName("homeBrokerMetric")
        self.home_et_val_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_et_bp_lbl = QLabel("Buying Power: $0.00")
        self.home_et_bp_lbl.setObjectName("homeBrokerMetric")
        self.home_et_bp_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_et_pl_lbl = QLabel("Day P&L: $0.00")
        self.home_et_pl_lbl.setStyleSheet(f"font-size: {ui_px(13)}px;")
        self.home_et_env_chip = QLabel("Sandbox")
        self.home_et_env_chip.setObjectName("homeBrokerMetric")
        self.home_et_env_chip.setStyleSheet(
            f"font-size: {ui_px(11)}px; font-weight: 700; color: #F9A825; "
            f"padding: {ui_px(2)}px {ui_px(8)}px;"
        )
        self.home_et_env_chip.setToolTip(
            "E*TRADE environment + live-order gate. Stops N/A (software TTP). "
            "Sandbox $0 BP is a stub; live $0 BP means verify funding/account."
        )
        _pack_broker_metrics(
            et_layout,
            (self.home_et_val_lbl, self.home_et_bp_lbl, self.home_et_pl_lbl, self.home_et_env_chip),
        )
        self.et_card.setLayout(et_layout)
        brokers_layout.addWidget(self.et_card)

        # No AlignTop: Net Worth stretches to the broker stack's total height
        nw_brokers.addWidget(self.master_card, 2)
        nw_brokers.addWidget(brokers_col, 5)
        layout.addWidget(nw_host)

        # Risk second: Cluster Heat | Portfolio Heat + Theme caps (same right column)
        risk_host = QWidget()
        risk_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        risk_row = QHBoxLayout(risk_host)
        risk_row.setContentsMargins(0, 0, 0, 0)
        risk_row.setSpacing(ui_px(12))
        self._home_risk_row = risk_row
        self._home_cluster_row = risk_row  # alias for DPI / layout helpers

        self.cluster_card = QGroupBox("Cluster Heat")
        self.cluster_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.cluster_card.setMaximumWidth(ui_px(360))
        cluster_lay = QVBoxLayout()
        cluster_lay.setContentsMargins(ui_px(12), ui_px(6), ui_px(12), ui_px(6))
        cluster_lay.setSpacing(ui_px(4))
        self._cluster_card_layout = cluster_lay
        self.home_cluster_empty_lbl = QLabel("No holdings in tracked clusters…")
        self.home_cluster_empty_lbl.setWordWrap(True)
        self.home_cluster_empty_lbl.setStyleSheet(f"font-size: {ui_px(12)}px;")
        self.home_cluster_host = QWidget()
        self.home_cluster_rows_lay = QVBoxLayout(self.home_cluster_host)
        self.home_cluster_rows_lay.setContentsMargins(0, 0, 0, 0)
        self.home_cluster_rows_lay.setSpacing(ui_px(4))
        self.home_cluster_host.setVisible(False)
        cluster_lay.addWidget(self.home_cluster_empty_lbl)
        cluster_lay.addWidget(self.home_cluster_host)
        self.cluster_card.setLayout(cluster_lay)
        risk_row.addWidget(self.cluster_card, 2, Qt.AlignTop)

        # Right column: Portfolio Heat on top, Theme caps under
        right_col = QWidget()
        right_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._home_risk_right_col = right_col
        right_lay = QVBoxLayout(right_col)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(ui_px(6))
        self._home_risk_right_lay = right_lay
        self._home_risk_layout = right_lay  # alias for DPI spacing

        self.heat_card = QGroupBox("Portfolio Heat")
        self.heat_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        heat_lay = QVBoxLayout()
        heat_lay.setContentsMargins(ui_px(12), ui_px(6), ui_px(12), ui_px(6))
        heat_lay.setSpacing(ui_px(2))
        self._heat_card_layout = heat_lay
        self.home_heat_lbl = QLabel("Risk — · BP headroom — · Session —")
        self.home_heat_lbl.setWordWrap(True)
        self.home_heat_lbl.setStyleSheet(f"font-size: {ui_px(12)}px;")
        chip_row = QHBoxLayout()
        chip_row.setSpacing(ui_px(8))
        self.home_dd_chip = QLabel("DD: ok")
        self.home_loss_chip = QLabel("$-loss: ok")
        self.home_stops_chip = QLabel("Stops: —")
        self.home_overnight_chip = QLabel("Overnight: —")
        self.home_shadow_chip = QLabel("Shadow: —")
        self.home_fill_chip = QLabel("Fill: —")
        self.home_frac_chip = QLabel("Frac: —")
        self.home_locked_chip = QLabel("Locked: —")
        for chip in (
            self.home_dd_chip, self.home_loss_chip, self.home_stops_chip,
            self.home_overnight_chip, self.home_shadow_chip, self.home_fill_chip,
            self.home_frac_chip, self.home_locked_chip,
        ):
            chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; padding: {ui_px(2)}px {ui_px(8)}px;"
            )
            chip_row.addWidget(chip)
        self.home_repair_stops_btn = QPushButton("Repair stops")
        self.home_repair_stops_btn.setObjectName("repairStopsBtn")
        self.home_repair_stops_btn.setMinimumHeight(ui_px(26))
        self.home_repair_stops_btn.setToolTip(
            "Attach missing broker protective stops for open equity holdings "
            "(skips crypto / E*TRADE). Does not sell or flatten."
        )
        self.home_repair_stops_btn.clicked.connect(
            lambda: self._maybe_repair_protective_stops(force=True)
        )
        chip_row.addWidget(self.home_repair_stops_btn)
        chip_row.addStretch(1)
        self.home_session_meter_lbl = QLabel("Session risk: —")
        self.home_session_meter_lbl.setStyleSheet(f"font-size: {ui_px(11)}px;")
        chip_row.addWidget(self.home_session_meter_lbl)
        self.home_locked_chip.setToolTip(
            "OTC/delisted (*Q), dust, and no-quote holdings — not deployable capital "
            "(excluded from rotate/sizing honesty)."
        )
        heat_lay.addWidget(self.home_heat_lbl)
        self.home_radar_card = QGroupBox("Desk radar")
        radar_lay = QVBoxLayout()
        radar_lay.setContentsMargins(ui_px(10), ui_px(6), ui_px(10), ui_px(6))
        self.home_radar_lbl = QLabel("No scored signals yet — run a CRYPTO / BREAKOUT / CORE cycle.")
        self.home_radar_lbl.setWordWrap(True)
        self.home_radar_lbl.setStyleSheet(f"font-size: {ui_px(11)}px;")
        self.home_radar_lbl.setToolTip(
            "Top scored BUY candidates across engines (last ~6h). Feeds companion signal alerts."
        )
        radar_lay.addWidget(self.home_radar_lbl)
        self.home_capital_lbl = QLabel("Capital planner: —")
        self.home_capital_lbl.setWordWrap(True)
        self.home_capital_lbl.setStyleSheet(f"font-size: {ui_px(11)}px; color: #888;")
        self.home_capital_lbl.setToolTip(
            "Deployable BP, sizing aim, and rotate preview for top desk-radar BUY."
        )
        radar_lay.addWidget(self.home_capital_lbl)
        self.home_radar_card.setLayout(radar_lay)
        heat_lay.addWidget(self.home_radar_card)

        self.home_advisor_card = QGroupBox("Desk Advisor — pending")
        adv_lay = QVBoxLayout()
        adv_lay.setContentsMargins(ui_px(10), ui_px(6), ui_px(10), ui_px(6))
        self.home_advisor_lbl = QLabel("Ask-before-apply OFF — auto buys fire immediately.")
        self.home_advisor_lbl.setWordWrap(True)
        self.home_advisor_lbl.setStyleSheet(f"font-size: {ui_px(11)}px;")
        adv_btn_row = QHBoxLayout()
        self.home_advisor_approve_btn = QPushButton("Approve top")
        self.home_advisor_approve_btn.setToolTip("Execute the oldest pending proposal (live).")
        self.home_advisor_approve_btn.clicked.connect(self._advisor_approve_top)
        self.home_advisor_reject_btn = QPushButton("Reject all")
        self.home_advisor_reject_btn.clicked.connect(self._advisor_reject_all)
        adv_btn_row.addWidget(self.home_advisor_approve_btn)
        adv_btn_row.addWidget(self.home_advisor_reject_btn)
        adv_btn_row.addStretch(1)
        adv_lay.addWidget(self.home_advisor_lbl)
        adv_lay.addLayout(adv_btn_row)
        self.home_advisor_card.setLayout(adv_lay)
        heat_lay.addWidget(self.home_advisor_card)
        self.home_locked_nudge = QLabel("")
        self.home_locked_nudge.setWordWrap(True)
        self.home_locked_nudge.setVisible(False)
        self.home_locked_nudge.setStyleSheet(
            f"color: #EF6C00; font-size: {ui_px(11)}px; font-weight: 600; "
            f"padding: {ui_px(2)}px 0;"
        )
        heat_lay.addWidget(self.home_locked_nudge)
        heat_lay.addLayout(chip_row)
        self.heat_card.setLayout(heat_lay)
        right_lay.addWidget(self.heat_card)

        from scoring import MAX_CLUSTER_POSITIONS as _MAX_CL
        self.home_cluster_tip_card = QGroupBox("Theme caps")
        self.home_cluster_tip_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        tip_lay = QVBoxLayout()
        tip_lay.setContentsMargins(ui_px(12), ui_px(6), ui_px(12), ui_px(6))
        tip_lay.setSpacing(ui_px(4))
        self._cluster_tip_card_layout = tip_lay
        self.home_cluster_tip = QLabel(
            f"Max {_MAX_CL} open names per theme (MAG7, BTC_BETA, SEMI, MEME_CRYPTO). "
            "Full blocks new entries into that theme."
        )
        self.home_cluster_tip.setObjectName("settingsHint")
        self.home_cluster_tip.setWordWrap(True)
        self.home_cluster_tip.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.home_cluster_tip.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        self.home_cluster_tip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        tip_lay.addWidget(self.home_cluster_tip)
        tip_lay.addStretch(1)
        self.home_cluster_tip_card.setLayout(tip_lay)
        right_lay.addWidget(self.home_cluster_tip_card)

        risk_row.addWidget(right_col, 3, Qt.AlignTop)
        layout.addWidget(risk_host)

        journal_hdr = QLabel("Recent Trades")
        journal_hdr.setObjectName("sectionHeader")
        journal_hdr.setStyleSheet(section_header_style())
        layout.addWidget(journal_hdr)

        self.recent_trades_table = QTableWidget(0, 7)
        self.recent_trades_table.setHorizontalHeaderLabels(
            ["Time", "Broker", "Side", "Ticker", "Price", "Status", "Confirmed"]
        )
        polish_trades_header(self.recent_trades_table)
        self.recent_trades_table.setMinimumHeight(ui_px(140))
        self.recent_trades_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        polish_table(self.recent_trades_table)
        layout.addWidget(self.recent_trades_table, 1)

        self._style_home_cards()
        self._update_home_wide_layout()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        self.tabs.addTab(tab, "Home")
        QTimer.singleShot(0, self.refresh_recent_trades)

    def refresh_recent_trades(self):
        if not hasattr(self, 'recent_trades_table'):
            return
        rows = journal.read_recent(20)
        self.recent_trades_table.setRowCount(len(rows))
        for i, row in enumerate(reversed(rows)):
            ts = str(row.get("timestamp", ""))[-19:]
            vals = [
                ts,
                str(row.get("broker", "")),
                str(row.get("side", "")),
                str(row.get("ticker", "")),
                format_currency(float(row.get("price") or 0)),
                str(row.get("status", ""))[:48],
                "Yes" if row.get("confirmed") else "No",
            ]
            for col, text in enumerate(vals):
                self.recent_trades_table.setItem(i, col, QTableWidgetItem(text))

    def build_portfolio_screen(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        header = QLabel("Portfolio Holdings")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())
        layout.addWidget(header)

        select_bar = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.portfolio_table, Qt.Checked))
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.portfolio_table, Qt.Unchecked))
        refresh_holdings_btn = QPushButton("Reload Holdings")
        refresh_holdings_btn.clicked.connect(self.manual_portfolio_reload)
        
        select_bar.addWidget(select_all_btn)
        select_bar.addWidget(deselect_all_btn)
        select_bar.addStretch()
        select_bar.addWidget(refresh_holdings_btn)
        layout.addLayout(select_bar)

        self.portfolio_table = QTableWidget(0, 8)
        self.portfolio_table.setHorizontalHeaderLabels(["Broker", "Ticker", "Shares", "Avg Cost", "Current Price", "Total Value", "Portfolio Action", "Trade Status"])
        self.portfolio_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        polish_table(self.portfolio_table)
        layout.addWidget(self.portfolio_table)
        
        scoring_btn = QPushButton("Run Scoring (Selected Only)")
        scoring_btn.setProperty("uiBtnKind", "primary")
        scoring_btn.setStyleSheet(action_btn_style("primary"))
        scoring_btn.clicked.connect(self.manual_score_portfolio)
        layout.addWidget(scoring_btn)
        
        execute_btn = QPushButton("Execute Approved Trades (LIVE - Selected Items Only)")
        execute_btn.setProperty("uiBtnKind", "danger")
        execute_btn.setStyleSheet(action_btn_style("danger"))
        execute_btn.clicked.connect(lambda: self.execute_portfolio_trades(auto_mode=False))
        layout.addWidget(execute_btn)
        
        tab.setLayout(layout)
        self.tabs.addTab(tab, "Portfolio")

    def build_signal_screen(self):
        """Dedicated research tab — sparkline/factors without crowding scanner tables."""
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(ui_px(16), ui_px(12), ui_px(16), ui_px(12))
        layout.setSpacing(ui_px(8))

        header = QLabel("Signal research")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())
        layout.addWidget(header)

        hint = QLabel(
            "Pick a row on Crypto / Breakouts / Core, or type a ticker below. "
            "This tab keeps the sparkline roomy so scanner lists stay clean."
        )
        hint.setWordWrap(True)
        hint.setObjectName("settingsHint")
        hint.setStyleSheet(f"color: #666; font-size: {ui_px(11)}px;")
        layout.addWidget(hint)

        entry = QHBoxLayout()
        entry.addWidget(QLabel("Ticker:"))
        self.signal_ticker_edit = QLineEdit()
        self.signal_ticker_edit.setPlaceholderText("e.g. SOL or IWM")
        self.signal_ticker_edit.setMaximumWidth(ui_px(140))
        entry.addWidget(self.signal_ticker_edit)
        self.signal_crypto_chk = QCheckBox("Crypto")
        entry.addWidget(self.signal_crypto_chk)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("uiBtnKind", "primary")
        refresh_btn.setStyleSheet(action_btn_style("primary"))
        refresh_btn.clicked.connect(self._refresh_signal_panel_from_entry)
        entry.addWidget(refresh_btn)
        open_scan_btn = QPushButton("Open scanners…")
        open_scan_btn.clicked.connect(self._focus_scanners_crypto_tab)
        entry.addWidget(open_scan_btn)
        entry.addStretch(1)
        layout.addLayout(entry)

        self.signal_source_lbl = QLabel("Source: —")
        self.signal_source_lbl.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        layout.addWidget(self.signal_source_lbl)

        self.signal_head_lbl = QLabel("No ticker selected.")
        self.signal_head_lbl.setWordWrap(True)
        self.signal_head_lbl.setStyleSheet(
            f"color: #333; font-size: {ui_px(13)}px; font-weight: 600;"
        )
        layout.addWidget(self.signal_head_lbl)

        self.signal_spark = SparklineWidget(min_h=120, max_h=None)
        self.signal_spark.setMinimumHeight(ui_px(140))
        layout.addWidget(self.signal_spark, stretch=1)

        self.signal_meters = FactorMeterRow()
        layout.addWidget(self.signal_meters)

        self.signal_why_lbl = QLabel("")
        self.signal_why_lbl.setWordWrap(True)
        self.signal_why_lbl.setStyleSheet(f"color: #444; font-size: {ui_px(12)}px;")
        layout.addWidget(self.signal_why_lbl)

        self.signal_levels_lbl = QLabel("")
        self.signal_levels_lbl.setWordWrap(True)
        self.signal_levels_lbl.setStyleSheet(f"color: #666; font-size: {ui_px(11)}px;")
        layout.addWidget(self.signal_levels_lbl)

        btn_row = QHBoxLayout()
        tv_btn = QPushButton("TradingView")
        fv_btn = QPushButton("Finviz / Coinbase")
        broker_btn = QPushButton("Broker page")
        tv_btn.clicked.connect(lambda: self._open_signal_deep_link("tv"))
        fv_btn.clicked.connect(lambda: self._open_signal_deep_link("research"))
        broker_btn.clicked.connect(lambda: self._open_signal_deep_link("broker"))
        btn_row.addWidget(tv_btn)
        btn_row.addWidget(fv_btn)
        btn_row.addWidget(broker_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.signal_ticker_edit.returnPressed.connect(self._refresh_signal_panel_from_entry)
        self._signal_focus = {"ticker": "", "is_crypto": False, "rec": "", "source": ""}
        tab.setLayout(layout)
        return tab

    def _focus_scanners_crypto_tab(self):
        idx = getattr(self, "scanners_tab_index", -1)
        if 0 <= idx < self.tabs.count():
            self.tabs.setCurrentIndex(idx)
        st = getattr(self, "scanners_tabs", None)
        if st is not None:
            st.setCurrentIndex(0)

    def _wire_scanner_signal_sources(self):
        """Row select on scanner tables updates the dedicated Signal tab."""
        for table, is_crypto, source in (
            (getattr(self, "crypto_table", None), True, "Crypto"),
            (getattr(self, "penny_table", None), False, "Breakouts"),
            (getattr(self, "core_table", None), False, "Core"),
        ):
            if table is None:
                continue

            def _on_sel(_=None, t=table, crypto=is_crypto, src=source):
                self._signal_focus_from_table(t, is_crypto=crypto, source=src)

            table.itemSelectionChanged.connect(_on_sel)
            table.currentCellChanged.connect(
                lambda *_a, t=table, c=is_crypto, s=source: self._signal_focus_from_table(
                    t, is_crypto=c, source=s
                )
            )

    def _signal_focus_from_table(self, table, *, is_crypto=False, source=""):
        row = table.currentRow()
        if row < 0:
            return
        item = table.item(row, 0)
        ticker = (item.text() if item else "").strip().upper().replace("-USD", "")
        if not ticker:
            return
        atype_item = table.item(row, 1)
        atype = (atype_item.text() if atype_item else "") or ""
        crypto = is_crypto or "crypto" in atype.lower()
        rec_item = table.item(row, 3)
        rec = (rec_item.text() if rec_item else "") or ""
        self._signal_focus = {
            "ticker": ticker,
            "is_crypto": crypto,
            "rec": rec,
            "source": source or "Scanner",
            "asset_type": atype,
        }
        if hasattr(self, "signal_ticker_edit"):
            self.signal_ticker_edit.setText(ticker)
            self.signal_crypto_chk.setChecked(bool(crypto))
        self._refresh_signal_panel()

    def _refresh_signal_panel_from_entry(self):
        ticker = (self.signal_ticker_edit.text() or "").strip().upper().replace("-USD", "")
        if not ticker:
            return
        focus = getattr(self, "_signal_focus", None) or {}
        self._signal_focus = {
            "ticker": ticker,
            "is_crypto": bool(self.signal_crypto_chk.isChecked()),
            "rec": focus.get("rec") or "",
            "source": "Manual",
            "asset_type": focus.get("asset_type") or "",
        }
        self._refresh_signal_panel()

    def _refresh_signal_panel(self):
        from scoring import signal_research_bundle, explain_gate_from_recommendation
        focus = getattr(self, "_signal_focus", None) or {}
        ticker = str(focus.get("ticker") or "").upper()
        if not hasattr(self, "signal_head_lbl"):
            return
        if not ticker:
            self.signal_head_lbl.setText("No ticker selected.")
            self.signal_spark.set_closes([])
            self.signal_meters.set_meters({})
            self.signal_why_lbl.setText("")
            self.signal_levels_lbl.setText("")
            self.signal_source_lbl.setText("Source: —")
            return
        crypto = bool(focus.get("is_crypto"))
        src = focus.get("source") or "—"
        self.signal_source_lbl.setText(f"Source: {src}")
        try:
            f = signal_research_bundle(ticker, is_crypto=crypto)
        except Exception as e:
            self.signal_head_lbl.setText(f"{ticker}: research load failed ({e})")
            return
        bits = [ticker]
        if f.get("price"):
            bits.append(f"@ {format_currency(f['price'])}")
        if f.get("score") is not None:
            bits.append(f"score {f['score']:.0f}")
        if f.get("rs_pct") is not None:
            bits.append(f"RS vs {f.get('bench') or '—'} {f['rs_pct']:+.1f}%")
        self.signal_head_lbl.setText(" · ".join(bits))
        self.signal_spark.set_closes(f.get("closes") or [])
        self.signal_meters.set_meters(f.get("meters") or {})
        gate = explain_gate_from_recommendation(focus.get("rec") or "")
        if f.get("regime_ok") is False and f.get("regime_reason"):
            gate = f"{gate} · regime: {(f.get('regime_reason') or '')[:50]}"
        self.signal_why_lbl.setText(f"Why: {gate}")
        lvl_bits = []
        if f.get("stop_pct") is not None:
            if f.get("stop_price"):
                lvl_bits.append(
                    f"hard-stop ~{f['stop_pct']:.1f}% ({format_currency(f['stop_price'])})"
                )
            else:
                lvl_bits.append(f"hard-stop ~{f['stop_pct']:.1f}%")
        if f.get("support_hint"):
            lvl_bits.append(f"support≈{format_currency(f['support_hint'])}")
        if f.get("fee_edge_pct") is not None:
            lvl_bits.append(f"fee edge≈{f['fee_edge_pct']:.2f}%")
        if f.get("ticker_pct") is not None and f.get("bench_pct") is not None:
            lvl_bits.append(
                f"5d {f['ticker_pct']:+.1f}% vs {f.get('bench')} {f['bench_pct']:+.1f}%"
            )
        self.signal_levels_lbl.setText(" · ".join(lvl_bits) if lvl_bits else "Levels —")

    def _open_signal_deep_link(self, kind):
        focus = getattr(self, "_signal_focus", None) or {}
        ticker = str(focus.get("ticker") or "").upper()
        if not ticker and hasattr(self, "signal_ticker_edit"):
            ticker = (self.signal_ticker_edit.text() or "").strip().upper().replace("-USD", "")
        if not ticker:
            return
        crypto = bool(focus.get("is_crypto"))
        if hasattr(self, "signal_crypto_chk"):
            crypto = bool(self.signal_crypto_chk.isChecked())
        if kind == "tv":
            path = f"{ticker}USD" if crypto else ticker
            webbrowser.open(f"https://www.tradingview.com/chart/?symbol={path}")
        elif kind == "research":
            if crypto:
                webbrowser.open(f"https://www.coinbase.com/price/{ticker.lower()}")
            else:
                webbrowser.open(f"https://finviz.com/quote.ashx?t={ticker}")
        else:
            if crypto:
                webbrowser.open(f"https://www.coinbase.com/price/{ticker.lower()}")
            else:
                webbrowser.open(f"https://robinhood.com/stocks/{ticker}")

    def _export_fills_csv(self):
        try:
            import journal as journal_mod
            days = 7
            if hasattr(self, "reports_days_combo"):
                raw = self.reports_days_combo.currentData()
                days = 7 if raw is None else int(raw)
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Export fills CSV",
                f"market_advisor_fills_{datetime.now().strftime('%Y%m%d')}.csv",
                "CSV Files (*.csv)",
            )
            if not path:
                return
            n = journal_mod.export_fills_csv(path, days=days, limit=8000)
            QMessageBox.information(self, "Export", f"Wrote {n} fill row(s) to:\n{path}")
            self.log_event(f"[Reports] Exported {n} fills → {path}")
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))

    def build_crypto_screen(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        header = QLabel("Crypto Momentum")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())
        layout.addWidget(header)

        select_bar = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.crypto_table, Qt.Checked))
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.crypto_table, Qt.Unchecked))
        
        select_bar.addWidget(select_all_btn)
        select_bar.addWidget(deselect_all_btn)
        select_bar.addStretch()
        layout.addLayout(select_bar)

        self.crypto_table = QTableWidget(0, 5)
        self.crypto_table.setHorizontalHeaderLabels(["Ticker", "Asset Type", "Current Price", "Entry Recommendation", "Trade Status"])
        self.crypto_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        polish_table(self.crypto_table)
        layout.addWidget(self.crypto_table)
        
        scan_btn = QPushButton("Manual Scan: Crypto")
        scan_btn.setProperty("uiBtnKind", "success")
        scan_btn.setStyleSheet(action_btn_style("success"))
        scan_btn.clicked.connect(lambda: self.manual_scan_table(self.crypto_table, self._bg_scan_crypto))
        layout.addWidget(scan_btn)

        score_btn = QPushButton("Run Scoring (Selected Only)")
        score_btn.setProperty("uiBtnKind", "primary")
        score_btn.setStyleSheet(action_btn_style("primary"))
        score_btn.clicked.connect(lambda: self._manual_score_table(self.crypto_table))
        layout.addWidget(score_btn)

        execute_btn = QPushButton("Execute Crypto Trades (Selected Only)")
        execute_btn.setProperty("uiBtnKind", "danger")
        execute_btn.setStyleSheet(action_btn_style("danger"))
        execute_btn.clicked.connect(lambda: self.execute_scanner_trades(self.crypto_table, auto_mode=False))
        layout.addWidget(execute_btn)
        
        tab.setLayout(layout)
        return tab  # nested under Scanners

    def build_penny_screen(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        header = QLabel("Breakouts · Penny & Movers")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())
        layout.addWidget(header)

        select_bar = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.penny_table, Qt.Checked))
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.penny_table, Qt.Unchecked))
        
        select_bar.addWidget(select_all_btn)
        select_bar.addWidget(deselect_all_btn)
        select_bar.addStretch()
        layout.addLayout(select_bar)

        self.penny_table = QTableWidget(0, 5)
        self.penny_table.setHorizontalHeaderLabels(["Ticker", "Asset Type", "Current Price", "Entry Recommendation", "Trade Status"])
        self.penny_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        polish_table(self.penny_table)
        layout.addWidget(self.penny_table)
        
        scan_btn = QPushButton("Manual Scan: Breakouts")
        scan_btn.setProperty("uiBtnKind", "success")
        scan_btn.setStyleSheet(action_btn_style("success"))
        scan_btn.clicked.connect(lambda: self.manual_scan_table(self.penny_table, self._bg_scan_penny))
        layout.addWidget(scan_btn)

        score_btn = QPushButton("Run Scoring (Selected Only)")
        score_btn.setProperty("uiBtnKind", "primary")
        score_btn.setStyleSheet(action_btn_style("primary"))
        score_btn.clicked.connect(lambda: self._manual_score_table(self.penny_table))
        layout.addWidget(score_btn)

        execute_btn = QPushButton("Execute Breakout Trades (Selected Only)")
        execute_btn.setProperty("uiBtnKind", "danger")
        execute_btn.setStyleSheet(action_btn_style("danger"))
        execute_btn.clicked.connect(lambda: self.execute_scanner_trades(self.penny_table, auto_mode=False))
        layout.addWidget(execute_btn)
        
        tab.setLayout(layout)
        return tab  # nested under Scanners

    def build_core_screen(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        header = QLabel("Core · ETFs & Large Cap")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())
        layout.addWidget(header)

        select_bar = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.core_table, Qt.Checked))
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(lambda: self.toggle_all_rows(self.core_table, Qt.Unchecked))
        
        select_bar.addWidget(select_all_btn)
        select_bar.addWidget(deselect_all_btn)
        select_bar.addStretch()
        layout.addLayout(select_bar)

        self.core_table = QTableWidget(0, 5)
        self.core_table.setHorizontalHeaderLabels(["Ticker", "Asset Type", "Current Price", "Entry Recommendation", "Trade Status"])
        self.core_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        polish_table(self.core_table)
        layout.addWidget(self.core_table)
        
        scan_btn = QPushButton("Manual Scan: Core ETFs")
        scan_btn.setProperty("uiBtnKind", "success")
        scan_btn.setStyleSheet(action_btn_style("success"))
        scan_btn.clicked.connect(lambda: self.manual_scan_table(self.core_table, self._bg_scan_core))
        layout.addWidget(scan_btn)

        score_btn = QPushButton("Run Scoring (Selected Only)")
        score_btn.setProperty("uiBtnKind", "primary")
        score_btn.setStyleSheet(action_btn_style("primary"))
        score_btn.clicked.connect(lambda: self._manual_score_table(self.core_table))
        layout.addWidget(score_btn)

        execute_btn = QPushButton("Execute Core Trades (Selected Only)")
        execute_btn.setProperty("uiBtnKind", "danger")
        execute_btn.setStyleSheet(action_btn_style("danger"))
        execute_btn.clicked.connect(lambda: self.execute_scanner_trades(self.core_table, auto_mode=False))
        layout.addWidget(execute_btn)
        
        tab.setLayout(layout)
        return tab  # nested under Scanners

    def build_ipo_screen(self):
        """Advisory upcoming IPO calendar — research / RH IPO Access only (no auto-apply)."""
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(ui_px(8))

        header = QLabel("Upcoming IPOs")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())
        layout.addWidget(header)

        disclaimer = QLabel(
            "For research / RH IPO Access — bot does not apply for you. "
            "Hints are lightweight heuristics, not financial advice."
        )
        disclaimer.setObjectName("ipoDisclaimer")
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet(
            f"color: {theme_colors(self.dark_mode)['muted']}; font-size: {ui_px(12)}px;"
        )
        self.ipo_disclaimer_lbl = disclaimer
        layout.addWidget(disclaimer)

        bar = QHBoxLayout()
        self.ipo_status_lbl = QLabel("IPO calendar not loaded yet.")
        self.ipo_status_lbl.setObjectName("ipoStatus")
        self.ipo_status_lbl.setStyleSheet(f"font-size: {ui_px(12)}px;")
        refresh_btn = QPushButton("Refresh IPOs")
        refresh_btn.setObjectName("ipoRefreshBtn")
        refresh_btn.setProperty("uiBtnKind", "success")
        refresh_btn.setStyleSheet(action_btn_style("success"))
        refresh_btn.setFixedWidth(ui_px(130))
        refresh_btn.clicked.connect(lambda: self.refresh_ipo_calendar(force=True))
        self.ipo_refresh_btn = refresh_btn
        open_yf_btn = QPushButton("Yahoo IPO Calendar")
        open_yf_btn.setObjectName("ipoYahooBtn")
        open_yf_btn.setFixedWidth(ui_px(160))
        open_yf_btn.clicked.connect(
            lambda: webbrowser.open("https://finance.yahoo.com/calendar/ipo")
        )
        bar.addWidget(self.ipo_status_lbl, 1)
        bar.addWidget(open_yf_btn)
        bar.addWidget(refresh_btn)
        layout.addLayout(bar)

        self.ipo_table = QTableWidget(0, 8)
        self.ipo_table.setHorizontalHeaderLabels([
            "Company", "Ticker", "Expected", "Exchange", "Price Range",
            "Status", "Consider", "Notes",
        ])
        self.ipo_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ipo_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        for col in (1, 2, 3, 4, 5, 6):
            self.ipo_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        polish_table(self.ipo_table)
        self.ipo_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.ipo_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.ipo_table.cellDoubleClicked.connect(self._on_ipo_row_open)
        layout.addWidget(self.ipo_table, 1)

        tip = QLabel("Double-click a row to open the Yahoo quote / IPO calendar in your browser.")
        tip.setObjectName("ipoTip")
        tip.setStyleSheet(
            f"color: {theme_colors(self.dark_mode)['muted']}; font-size: {ui_px(11)}px;"
        )
        layout.addWidget(tip)

        tab.setLayout(layout)
        self.tabs.addTab(tab, "IPOs")

    def refresh_ipo_calendar(self, force=False):
        if getattr(self, "_ipo_refresh_in_flight", False):
            return
        if not hasattr(self, "ipo_table"):
            return
        self._ipo_refresh_in_flight = True
        if hasattr(self, "ipo_status_lbl"):
            self.ipo_status_lbl.setText("Loading upcoming IPOs…")
        if hasattr(self, "ipo_refresh_btn"):
            self.ipo_refresh_btn.setEnabled(False)

        holding_tickers = []
        try:
            for name in BROKER_NAMES:
                for a in self.get_broker_holdings(name) or []:
                    t = a.get("ticker")
                    if t:
                        holding_tickers.append(t)
        except Exception:
            pass

        regime_ok = True
        try:
            from scoring import market_regime_ok
            ok, _reason = market_regime_ok(is_crypto=False)
            regime_ok = bool(ok)
        except Exception:
            regime_ok = True

        def _bg():
            import ipo_calendar
            return ipo_calendar.fetch_upcoming_ipos(
                force=force,
                holding_tickers=holding_tickers,
                regime_ok=regime_ok,
            )

        task = BackgroundTask(_bg)
        task.result_ready.connect(self._on_ipo_calendar_loaded)
        task.error_occurred.connect(self._on_ipo_calendar_error)
        self.active_threads.append(task)
        task.start()

    def _on_ipo_calendar_error(self, message):
        self._ipo_refresh_in_flight = False
        if hasattr(self, "ipo_refresh_btn"):
            self.ipo_refresh_btn.setEnabled(True)
        if hasattr(self, "ipo_status_lbl"):
            self.ipo_status_lbl.setText(f"IPO calendar unavailable: {message}")
        self.log_event(f"IPO calendar error: {message}")

    def _on_ipo_calendar_loaded(self, result):
        self._ipo_refresh_in_flight = False
        if hasattr(self, "ipo_refresh_btn"):
            self.ipo_refresh_btn.setEnabled(True)
        result = result or {}
        ipos = result.get("ipos") or []
        err = result.get("error")
        source = result.get("source") or "—"
        fetched_at = result.get("fetched_at") or 0
        cached = "cache" if result.get("from_cache") else "live"
        try:
            import ipo_calendar
            when = ipo_calendar.format_fetched_at(fetched_at)
        except Exception:
            when = "—"

        if err and not ipos:
            if hasattr(self, "ipo_status_lbl"):
                self.ipo_status_lbl.setText(f"Could not load IPOs ({err}). Try Refresh.")
            self.log_event(f"IPO calendar failed: {err}")
            return

        self.ipo_table.setRowCount(len(ipos))
        self._ipo_row_links = []
        for row, ipo in enumerate(ipos):
            vals = [
                str(ipo.get("company") or ""),
                str(ipo.get("ticker") or "—"),
                str(ipo.get("date") or "—"),
                str(ipo.get("exchange") or "—"),
                str(ipo.get("price_range") or "—"),
                str(ipo.get("status") or "—"),
                str(ipo.get("hint") or "—"),
                str(ipo.get("note") or ""),
            ]
            self._ipo_row_links.append(ipo.get("link") or "")
            for col, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if col == 6:
                    self._apply_ipo_hint_color(item, text)
                self.ipo_table.setItem(row, col, item)

        extra = f" · {err}" if err else ""
        if hasattr(self, "ipo_status_lbl"):
            self.ipo_status_lbl.setText(
                f"{len(ipos)} upcoming · source: {source} ({cached}) · updated {when}{extra}"
            )
        if not result.get("from_cache"):
            self.log_event(f"IPO calendar refreshed: {len(ipos)} listings from {source}")

    def _apply_ipo_hint_color(self, item, hint):
        h = str(hint or "").lower()
        if "worth a look" in h:
            fg = QColor("#00E676" if self.dark_mode else "#2E7D32")
            bg = QColor("#003816" if self.dark_mode else "#E8F5E9")
        elif "skip" in h or "speculative" in h:
            fg = QColor("#FF8A80" if self.dark_mode else "#C62828")
            bg = QColor("#3A0B0B" if self.dark_mode else "#FFEBEE")
        elif "caution" in h or "watch" in h:
            fg = QColor("#FFD54F" if self.dark_mode else "#F57F17")
            bg = QColor("#332A00" if self.dark_mode else "#FFFDE7")
        else:
            return
        item.setForeground(fg)
        item.setBackground(bg)
        item.setData(Qt.ForegroundRole, fg)
        item.setData(Qt.BackgroundRole, bg)

    def _on_ipo_row_open(self, row, _col):
        links = getattr(self, "_ipo_row_links", [])
        url = links[row] if 0 <= row < len(links) else ""
        if not url:
            url = "https://finance.yahoo.com/calendar/ipo"
        try:
            webbrowser.open(url)
        except Exception as e:
            self.log_event(f"Could not open IPO link: {e}")

    def build_activity_log_screen(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        header_bar = QHBoxLayout()
        header = QLabel("Activity Log")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())

        tc = theme_colors(self.dark_mode)
        self.activity_log_hint = QLabel(
            f"Showing last {ACTIVITY_LOG_UI_MAX_LINES} lines · older → activity_log_archives/"
        )
        self.activity_log_hint.setObjectName("activityLogHint")
        self.activity_log_hint.setToolTip(
            "UI shows a recent window. The active activity_log.txt rolls when large; "
            "older lines are archived indefinitely under activity_log_archives/ "
            "(next to the log file). Clear Log clears the UI + active file only — not archives."
        )
        self.activity_log_hint.setStyleSheet(
            f"color: {tc['muted']}; font-size: {ui_px(11)}px;"
        )

        filter_lbl = QLabel("Show:")
        self.log_filter_combo = QComboBox()
        self.log_filter_combo.setObjectName("logFilterCombo")
        self.log_filter_combo.addItems(["All", "Robinhood", "Coinbase", "E*TRADE", "Companion"])
        self.log_filter_combo.setFixedWidth(ui_px(130))
        self.log_filter_combo.setToolTip("Filter log lines by broker or Companion (All keeps app-wide messages too)")
        self.log_filter_combo.currentTextChanged.connect(self._on_log_filter_changed)

        copy_log_btn = QPushButton("Copy Log")
        copy_log_btn.setObjectName("copyLogBtn")
        copy_log_btn.setFixedWidth(ui_px(100))
        copy_log_btn.setToolTip("Copy the currently visible (filtered) activity log to the clipboard")
        copy_log_btn.clicked.connect(self.copy_log_to_clipboard)

        save_log_btn = QPushButton("Save Log File")
        save_log_btn.setObjectName("saveLogBtn")
        save_log_btn.setFixedWidth(ui_px(120))
        save_log_btn.clicked.connect(self.save_log_to_file)

        open_logs_btn = QPushButton("Open Logs Folder")
        open_logs_btn.setObjectName("openLogsBtn")
        open_logs_btn.setFixedWidth(ui_px(130))
        open_logs_btn.setToolTip(
            "Open the folder with activity_log.txt and activity_log_archives/ "
            "(indefinite history)."
        )
        open_logs_btn.clicked.connect(self._open_activity_log_folder)

        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.setObjectName("clearLogBtn")
        clear_log_btn.setFixedWidth(ui_px(100))
        clear_log_btn.setToolTip(
            "Clear the UI and active activity_log.txt. Archived history under "
            "activity_log_archives/ is kept."
        )
        clear_log_btn.clicked.connect(self._clear_activity_log)

        header_bar.addWidget(header)
        header_bar.addWidget(self.activity_log_hint)
        header_bar.addStretch()
        header_bar.addWidget(filter_lbl)
        header_bar.addWidget(self.log_filter_combo)
        header_bar.addWidget(copy_log_btn)
        header_bar.addWidget(save_log_btn)
        header_bar.addWidget(open_logs_btn)
        header_bar.addWidget(clear_log_btn)
        layout.addLayout(header_bar)

        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        # Seed once from the capped in-memory buffer (not the multi-MB disk file)
        seed = "\n".join(self._filtered_log_lines())
        if seed:
            self.log_text_edit.setPlainText(seed)
        self._log_ui_append_count = 0
        layout.addWidget(self.log_text_edit)
        tab.setLayout(layout)
        return tab  # nested under Journal

    def build_execution_screen(self):
        """Fill quality + execution feedback loop (slippage, shadow guard)."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(ui_px(8))
        tc = theme_colors(self.dark_mode)

        hdr = QLabel("Execution quality")
        hdr.setObjectName("sectionHeader")
        hdr.setStyleSheet(section_header_style())
        layout.addWidget(hdr)

        hint = QLabel(
            "Live fill slippage vs quote, adverse-rate loop, and shadow guardrail state. "
            "Use with Reports walk-forward — same fee/TTP rails as live."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(11)}px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_execution_quality)
        export_btn = QPushButton("Export fills CSV…")
        export_btn.clicked.connect(self._export_fills_csv)
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(export_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.exec_summary_lbl = QLabel("—")
        self.exec_summary_lbl.setWordWrap(True)
        self.exec_summary_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.exec_summary_lbl)

        self.exec_feedback_lbl = QLabel("—")
        self.exec_feedback_lbl.setWordWrap(True)
        self.exec_feedback_lbl.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        layout.addWidget(self.exec_feedback_lbl)

        self.exec_fills_table = QTableWidget(0, 7)
        self.exec_fills_table.setHorizontalHeaderLabels([
            "Time", "Broker", "Side", "Ticker", "Slip bps", "Fee", "Status",
        ])
        self.exec_fills_table.horizontalHeader().setStretchLastSection(True)
        self.exec_fills_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        polish_table(self.exec_fills_table)
        layout.addWidget(self.exec_fills_table, 1)

        QTimer.singleShot(800, self.refresh_execution_quality)
        return tab

    def refresh_execution_quality(self):
        if not hasattr(self, "exec_summary_lbl"):
            return
        try:
            import analytics
            import journal as journal_mod
            from scoring import get_execution_feedback
            days = int(self.settings.get("reports_window_days", 7) or 7)
            rows = journal_mod.read_since_days(days=days, limit=4000)
            fq = analytics.summarize_fill_quality(rows)
            fb = get_execution_feedback()
            sg = getattr(self, "_last_shadow_guard", None) or getattr(self, "_shadow_guard_active", None) or {}
            avg = fq.get("avg_slippage_bps")
            avg_txt = f"{float(avg):+.1f} bps" if avg is not None else "n/a"
            ar = fq.get("adverse_rate")
            ar_txt = f"{float(ar)*100:.0f}%" if ar is not None else "n/a"
            self.exec_summary_lbl.setText(
                f"Samples {fq.get('samples', 0)} · avg slip {avg_txt} · "
                f"adverse {ar_txt} · missing slip {fq.get('missing_slippage', 0)}"
            )
            bump = float(fb.get("offset_bump_pct") or 0)
            sm = float(fb.get("size_mult") or 1.0)
            sg_line = ""
            if sg.get("present") or sg.get("tighten"):
                sg_line = (
                    f"Shadow: {sg.get('status') or '—'} · size×{float(sg.get('size_mult') or 1):.2f}"
                )
            self.exec_feedback_lbl.setText(
                f"Active loop: offset bump +{bump:.2f}% · size×{sm:.2f}"
                + (f" · {sg_line}" if sg_line else "")
                + f"\n{fq.get('note') or ''}"
            )
            fills = [r for r in rows if analytics._is_fill(r)][-40:]
            self.exec_fills_table.setRowCount(len(fills))
            for i, r in enumerate(reversed(fills)):
                slip = r.get("slippage_bps")
                if slip is None:
                    slip_txt = "—"
                else:
                    try:
                        slip_txt = f"{float(slip):+.1f}"
                    except (TypeError, ValueError):
                        slip_txt = "—"
                fee = r.get("fee_paid") or r.get("fee_est")
                vals = [
                    str(r.get("timestamp") or "")[:19],
                    str(r.get("broker") or ""),
                    str(r.get("side") or ""),
                    str(r.get("ticker") or ""),
                    slip_txt,
                    format_currency(fee) if fee else "—",
                    str(r.get("status") or "")[:48],
                ]
                for c, text in enumerate(vals):
                    self.exec_fills_table.setItem(i, c, QTableWidgetItem(text))
            self._last_execution_quality = {"fill_quality": fq, "feedback": fb, "shadow": sg}
        except Exception as e:
            self.exec_summary_lbl.setText(f"Execution quality error: {e}")

    def build_reports_screen(self):
        """Turnover / fee / P&L analytics + lite journal posture compare."""
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = CompactScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(ui_px(8))

        tc = theme_colors(self.dark_mode)
        header_bar = QHBoxLayout()
        header = QLabel("Reports")
        header.setObjectName("sectionHeader")
        header.setStyleSheet(section_header_style())
        header_bar.addWidget(header)
        header_bar.addStretch()
        header_bar.addWidget(QLabel("Window:"))
        self.reports_days_combo = QComboBox()
        self.reports_days_combo.addItem("Today", 1)
        self.reports_days_combo.addItem("7 days", 7)
        self.reports_days_combo.addItem("30 days", 30)
        self.reports_days_combo.addItem("All time", -1)
        # Prefer last-used window; default All time for lifetime P&L story
        pref = int(self.settings.get("reports_window_days", -1) or -1)
        idx = max(0, self.reports_days_combo.findData(pref))
        if idx < 0:
            idx = self.reports_days_combo.findData(-1)
        self.reports_days_combo.setCurrentIndex(max(0, idx))
        self.reports_days_combo.currentIndexChanged.connect(self.refresh_reports)
        header_bar.addWidget(self.reports_days_combo)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("uiBtnKind", "primary")
        refresh_btn.setStyleSheet(action_btn_style("primary"))
        refresh_btn.clicked.connect(self.refresh_reports)
        header_bar.addWidget(refresh_btn)
        export_btn = QPushButton("Export fills CSV…")
        export_btn.setToolTip("Export journal fills for the selected window (fee fields included).")
        export_btn.clicked.connect(self._export_fills_csv)
        header_bar.addWidget(export_btn)
        layout.addLayout(header_bar)

        hint = QLabel(
            "Hero = Net≈ (realized − fees) · fee drag · trade count. "
            "Fee confidence rises when broker invoice fields land on fills. "
            "Bar WF = multi-symbol OHLCV · fee-aware · holiday-filtered equities (not QuantConnect)."
        )
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(11)}px;")
        layout.addWidget(hint)

        self.reports_assumptions_lbl = QLabel("—")
        self.reports_assumptions_lbl.setWordWrap(True)
        self.reports_assumptions_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_assumptions_lbl.setStyleSheet(
            f"font-size: {ui_px(11)}px; color: {tc['warn']}; font-weight: 600;"
        )
        layout.addWidget(self.reports_assumptions_lbl)

        self.reports_et_honesty_lbl = QLabel("—")
        self.reports_et_honesty_lbl.setWordWrap(True)
        self.reports_et_honesty_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_et_honesty_lbl.setStyleSheet(
            f"font-size: {ui_px(12)}px; color: {tc['muted']};"
        )
        layout.addWidget(self.reports_et_honesty_lbl)

        self.reports_summary_lbl = QLabel("—")
        self.reports_summary_lbl.setWordWrap(True)
        self.reports_summary_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_summary_lbl.setStyleSheet(
            f"font-size: {ui_px(14)}px; font-weight: 600; color: {tc['text']};"
        )
        layout.addWidget(self.reports_summary_lbl)

        self.reports_fee_conf_lbl = QLabel("Fee confidence: —")
        self.reports_fee_conf_lbl.setWordWrap(True)
        self.reports_fee_conf_lbl.setStyleSheet(
            f"font-size: {ui_px(12)}px; color: {tc['muted']};"
        )
        layout.addWidget(self.reports_fee_conf_lbl)

        self.reports_broker_table = QTableWidget(0, 11)
        self.reports_broker_table.setHorizontalHeaderLabels([
            "Broker", "Buys", "Sells", "Rotates", "Turnover",
            "Est. Fees", "Fee drag %", "Gross WR", "Net WR",
            "Realized P&L", "Net P&L",
        ])
        self.reports_broker_table.horizontalHeader().setStretchLastSection(True)
        self.reports_broker_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        polish_table(self.reports_broker_table)
        layout.addWidget(self.reports_broker_table, 1)

        adv_hdr = QLabel("Advanced analytics")
        adv_hdr.setObjectName("sectionHeader")
        adv_hdr.setStyleSheet(
            f"font-size: {ui_px(12)}px; font-weight: 600; color: {tc['muted']};"
        )
        layout.addWidget(adv_hdr)

        wf_hdr = QLabel("Journal folds (fee-aware fill replay)")
        wf_hdr.setStyleSheet(f"font-size: {ui_px(12)}px; font-weight: 600; color: {tc['muted']};")
        layout.addWidget(wf_hdr)
        self.reports_walkforward_lbl = QLabel("—")
        self.reports_walkforward_lbl.setWordWrap(True)
        self.reports_walkforward_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_walkforward_lbl.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        layout.addWidget(self.reports_walkforward_lbl)

        bar_hdr = QLabel("Bar walk-forward (multi-symbol OHLCV · fee-aware · posture)")
        bar_hdr.setStyleSheet(f"font-size: {ui_px(12)}px; font-weight: 600; color: {tc['muted']};")
        layout.addWidget(bar_hdr)
        self.reports_bar_walkforward_lbl = QLabel("—")
        self.reports_bar_walkforward_lbl.setWordWrap(True)
        self.reports_bar_walkforward_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_bar_walkforward_lbl.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        layout.addWidget(self.reports_bar_walkforward_lbl)

        fq_hdr = QLabel("Fill quality · Paper vs Live · Shadow guard")
        fq_hdr.setStyleSheet(f"font-size: {ui_px(12)}px; font-weight: 600; color: {tc['muted']};")
        layout.addWidget(fq_hdr)
        self.reports_fill_quality_lbl = QLabel("—")
        self.reports_fill_quality_lbl.setWordWrap(True)
        self.reports_fill_quality_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_fill_quality_lbl.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        layout.addWidget(self.reports_fill_quality_lbl)

        dec_hdr = QLabel("Decision log (skips / buy rate)")
        dec_hdr.setStyleSheet(f"font-size: {ui_px(12)}px; font-weight: 600; color: {tc['muted']};")
        layout.addWidget(dec_hdr)
        self.reports_decisions_lbl = QLabel("—")
        self.reports_decisions_lbl.setWordWrap(True)
        self.reports_decisions_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_decisions_lbl.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        layout.addWidget(self.reports_decisions_lbl)

        compare_hdr = QLabel("Compare posture (fees + lite decision replay)")
        compare_hdr.setStyleSheet(f"font-size: {ui_px(12)}px; font-weight: 600; color: {tc['muted']};")
        layout.addWidget(compare_hdr)
        self.reports_posture_lbl = QLabel("—")
        self.reports_posture_lbl.setWordWrap(True)
        self.reports_posture_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reports_posture_lbl.setStyleSheet(f"color: {tc['muted']}; font-size: {ui_px(12)}px;")
        layout.addWidget(self.reports_posture_lbl)

        layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        # Journal analytics can hitch on large JSONL — wait until UI has settled
        QTimer.singleShot(2500, self.refresh_reports)
        return tab  # nested under Journal

    def refresh_reports(self):
        if not hasattr(self, "reports_summary_lbl"):
            return
        try:
            import analytics
            import journal as journal_mod
            days = 7
            if hasattr(self, "reports_days_combo"):
                raw = self.reports_days_combo.currentData()
                days = 7 if raw is None else int(raw)
                try:
                    self.settings["reports_window_days"] = days
                except Exception:
                    pass
            # days < 0 → All time (no date cutoff) via journal readers
            rows = journal_mod.read_since_days(days=days, limit=8000)
            summary = analytics.summarize_fills(rows)
            win_map = {1: "Today", 7: "7d", 30: "30d", -1: "All time"}
            win_lbl = win_map.get(days, "")
            self.reports_summary_lbl.setText(
                analytics.format_reports_hero(
                    summary, money_fmt=format_currency, window_label=win_lbl,
                )
            )
            if hasattr(self, "reports_et_honesty_lbl"):
                try:
                    snap = self._etrade_monitor_snapshot()
                    self.reports_et_honesty_lbl.setText(
                        snap.get("note")
                        or "E*TRADE: connect in Settings for sandbox/live path status."
                    )
                except Exception:
                    self.reports_et_honesty_lbl.setText("E*TRADE path: —")
            fee_conf = analytics.summarize_fee_confidence(rows)
            coach = analytics.fee_drag_coach(summary)
            fee_lines = [fee_conf.get("chip") or "Fee confidence: —"]
            if fee_conf.get("tip"):
                fee_lines.append(fee_conf["tip"])
            if coach:
                fee_lines.append(coach)
            if hasattr(self, "reports_fee_conf_lbl"):
                self.reports_fee_conf_lbl.setText("\n".join(fee_lines))
            by_b = summary.get("by_broker") or {}
            self.reports_broker_table.setRowCount(len(by_b))
            for i, (name, b) in enumerate(sorted(by_b.items())):
                turnover = float(b.get("buy_notional") or 0) + float(b.get("sell_notional") or 0)
                drag = float(b.get("fee_drag_pct") or 0.0)
                gross_wr = b.get("win_rate")
                net_wr = b.get("net_win_rate")
                net = float(b.get("net_after_fees") or (
                    float(b.get("realized_pnl") or 0) - float(b.get("fee_est") or 0)
                ))
                vals = [
                    name,
                    str(b.get("buys") or 0),
                    str(b.get("sells") or 0),
                    str(b.get("rotates") or 0),
                    format_currency(turnover),
                    format_currency(b.get("fee_est") or 0),
                    f"{drag:.2f}%",
                    f"{gross_wr * 100:.0f}%" if gross_wr is not None else "—",
                    f"{net_wr * 100:.0f}%" if net_wr is not None else "—",
                    format_currency(b.get("realized_pnl") or 0),
                    format_currency(net),
                ]
                for c, text in enumerate(vals):
                    self.reports_broker_table.setItem(i, c, QTableWidgetItem(text))

            drows = journal_mod.read_decisions_since_days(days=days, limit=8000)
            dsum = analytics.summarize_decisions(drows)
            if hasattr(self, "reports_decisions_lbl"):
                br = dsum.get("buy_rate")
                br_txt = f"{br*100:.0f}%" if br is not None else "—"
                top = dsum.get("top_reasons") or []
                top_txt = ", ".join(f"{t['reason']}×{t['count']}" for t in top[:5]) or "—"
                self.reports_decisions_lbl.setText(
                    f"Decisions {dsum.get('total', 0)} · Buys {dsum.get('buys', 0)} · "
                    f"Skips {dsum.get('skips', 0)} · Fails {dsum.get('fails', 0)} · "
                    f"Rotate skips {dsum.get('rotate_skips', 0)} · "
                    f"Scale-in skips {dsum.get('scale_in_skips', 0)} · "
                    f"Idle skips {dsum.get('idle_skips', 0)} · "
                    f"Buy rate {br_txt} · Regime-blocked marks {dsum.get('regime_blocked', 0)}\n"
                    f"Top skip reasons: {top_txt}"
                )

            cmp_ = analytics.compare_posture_fees(rows)
            replay = analytics.lite_posture_decision_replay(drows)
            lines = []
            for key, pdata in (cmp_.get("postures") or {}).items():
                r = (replay.get("postures") or {}).get(key) or {}
                lines.append(
                    f"{pdata.get('label', key)}: fees {format_currency(pdata.get('fee_est') or 0)} · "
                    f"net≈ {format_currency(pdata.get('net_after_fees') or 0)} · "
                    f"max open {pdata.get('max_open')} · "
                    f"max_open skips that would clear ≈ {r.get('would_clear_max_open', 0)}/"
                    f"{r.get('skips_seen', 0)}"
                )
            note = replay.get("note") or ""
            self.reports_posture_lbl.setText(
                ("\n".join(lines) if lines else "No data in window.")
                + (f"\n{note}" if note else "")
            )

            if hasattr(self, "reports_walkforward_lbl"):
                wf = analytics.walk_forward_fee_replay(rows, drows, n_folds=3)
                if hasattr(self, "reports_assumptions_lbl"):
                    assumptions = list(wf.get("assumptions") or [])
                    try:
                        bwf_pre = getattr(self, "_last_bar_walkforward", {}) or {}
                        assumptions.extend(list(bwf_pre.get("assumptions") or [])[:2])
                    except Exception:
                        pass
                    if assumptions:
                        self.reports_assumptions_lbl.setText(
                            "Backtest honesty: " + " · ".join(dict.fromkeys(assumptions))
                        )
                    else:
                        self.reports_assumptions_lbl.setText(
                            "Backtest honesty: journal fills only — not independent signal replay."
                        )
                wf_lines = []
                if wf.get("n_fills", 0) < 2:
                    wf_lines.append(wf.get("note") or "Not enough fills for journal folds.")
                else:
                    wf_lines.append(
                        f"{wf.get('note', '')} · OOS net sum "
                        f"{format_currency(wf.get('oos_net_sum') or 0)}"
                    )
                    for step in (wf.get("walk_forward") or [])[:4]:
                        oos = step.get("out_of_sample") or {}
                        wr = oos.get("win_rate")
                        wr_t = f"{wr*100:.0f}%" if wr is not None else "—"
                        wf_lines.append(
                            f"Step {step.get('step')}: OOS fold {step.get('oos_fold')} · "
                            f"fills {oos.get('n_fills', 0)} · "
                            f"net≈ {format_currency(oos.get('net_after_fees') or 0)} · "
                            f"WR {wr_t} · fee drag {float(oos.get('fee_drag_pct') or 0):.2f}%"
                        )
                    assumptions = wf.get("assumptions") or []
                    if assumptions:
                        wf_lines.append("Assumptions: " + " · ".join(assumptions[:3]))
                self.reports_walkforward_lbl.setText("\n".join(wf_lines))
                self._last_journal_walkforward = {
                    "note": wf.get("note"),
                    "oos_net_sum": wf.get("oos_net_sum"),
                    "oos_steps": wf.get("oos_steps"),
                    "n_folds": wf.get("n_folds"),
                }

            if hasattr(self, "reports_bar_walkforward_lbl"):
                try:
                    cache_key = (days, len(rows))
                    cached = getattr(self, "_bar_wf_cache", None)
                    now_ts = time.time()
                    if (
                        isinstance(cached, dict)
                        and cached.get("key") == cache_key
                        and (now_ts - float(cached.get("ts") or 0)) < 600
                    ):
                        bwf = cached.get("result") or {}
                    else:
                        bwf = analytics.bar_walk_forward_replay(
                            rows, n_folds=3, decision_rows=drows,
                        )
                        self._bar_wf_cache = {"key": cache_key, "ts": now_ts, "result": bwf}
                except Exception as be:
                    bwf = {"note": f"Bar walk-forward error: {be}", "n_trades": 0, "assumptions": []}
                self._last_bar_walkforward = {
                    "note": bwf.get("note"),
                    "oos_net_sum": bwf.get("oos_net_sum"),
                    "oos_steps": bwf.get("oos_steps"),
                    "n_trades": bwf.get("n_trades"),
                    "n_folds": bwf.get("n_folds"),
                }
                bar_lines = []
                if int(bwf.get("n_trades") or 0) < 2:
                    bar_lines.append(bwf.get("note") or "Not enough bar trades yet.")
                else:
                    bar_lines.append(
                        f"{bwf.get('note', '')} · OOS net sum "
                        f"{format_currency(bwf.get('oos_net_sum') or 0)}"
                    )
                    for step in (bwf.get("walk_forward") or [])[:4]:
                        oos = step.get("out_of_sample") or {}
                        wr = oos.get("win_rate")
                        wr_t = f"{wr*100:.0f}%" if wr is not None else "—"
                        bar_lines.append(
                            f"Step {step.get('step')}: OOS fold {step.get('oos_fold')} · "
                            f"trades {oos.get('n_trades', 0)} · "
                            f"net≈ {format_currency(oos.get('net_after_fees') or 0)} · WR {wr_t}"
                        )
                    assumptions = bwf.get("assumptions") or []
                    if assumptions:
                        bar_lines.append("Assumptions: " + " · ".join(assumptions[:5]))
                    fee_n = int(bwf.get("broker_fee_trades") or 0)
                    n_tr = int(bwf.get("n_trades") or 0)
                    if n_tr:
                        bar_lines.append(
                            f"Fee sources: {fee_n}/{n_tr} trades used broker invoice fields "
                            f"(rest Est. profile)."
                        )
                    overall = bwf.get("overall") or {}
                    if overall:
                        bar_lines.insert(
                            1,
                            f"All bar trades: net≈ {format_currency(overall.get('net_after_fees') or 0)} · "
                            f"fees {format_currency(overall.get('fee_est') or 0)} · "
                            f"wins {overall.get('wins', 0)}/{n_tr}",
                        )
                    syms = bwf.get("symbols") or []
                    if syms:
                        bar_lines.append(
                            "Symbols: " + ", ".join(syms[:12])
                            + (f" (+{len(syms)-12})" if len(syms) > 12 else "")
                        )
                    missing = bwf.get("missing_bars") or []
                    if missing:
                        bar_lines.append("Missing bars: " + ", ".join(missing[:8]))
                    pc = bwf.get("posture_compare") or {}
                    if pc.get("postures"):
                        bar_lines.append(
                            "Posture capacity (decision skips): see Compare posture strip above."
                        )
                self.reports_bar_walkforward_lbl.setText("\n".join(bar_lines))

            if hasattr(self, "reports_fill_quality_lbl"):
                fq = analytics.summarize_fill_quality(rows)
                shadow = analytics.compare_paper_live(rows)
                guard = analytics.evaluate_shadow_guardrail(
                    rows,
                    adverse_rate_threshold=float(
                        self.settings.get("shadow_adverse_rate_threshold", 0.55) or 0.55
                    ),
                    delta_net_threshold=float(
                        self.settings.get("shadow_delta_net_threshold", -25.0) or -25.0
                    ),
                )
                self._last_shadow_guard = guard
                self._apply_shadow_guardrail(guard)
                avg = fq.get("avg_slippage_bps")
                avg_t = f"{avg:.1f} bps" if avg is not None else "—"
                ar = fq.get("adverse_rate")
                ar_t = f"{ar*100:.0f}%" if ar is not None else "—"
                fq_lines = [
                    f"Fill quality: samples {fq.get('samples', 0)} · "
                    f"avg slippage {avg_t} · adverse rate {ar_t} · "
                    f"missing meta {fq.get('missing_slippage', 0)}"
                ]
                p = shadow.get("paper") or {}
                l = shadow.get("live") or {}
                if shadow.get("both_modes"):
                    pwr = p.get("win_rate")
                    lwr = l.get("win_rate")
                    fq_lines.append(
                        f"Paper: fills {p.get('fills', 0)} · "
                        f"net≈ {format_currency(p.get('net_after_fees') or 0)} · "
                        f"WR {f'{pwr*100:.0f}%' if pwr is not None else '—'}  |  "
                        f"Live: fills {l.get('fills', 0)} · "
                        f"net≈ {format_currency(l.get('net_after_fees') or 0)} · "
                        f"WR {f'{lwr*100:.0f}%' if lwr is not None else '—'}  ·  "
                        f"Δ(live−paper) {format_currency(shadow.get('delta_live_minus_paper_net') or 0)}"
                    )
                else:
                    fq_lines.append(shadow.get("note") or "")
                fq_lines.append(
                    f"Shadow guard: {guard.get('status', '—')} · {guard.get('tip', '')}"
                )
                if fq.get("note"):
                    fq_lines.append(fq["note"])
                self.reports_fill_quality_lbl.setText("\n".join(fq_lines))
        except Exception as e:
            self.reports_summary_lbl.setText(f"Reports error: {e}")

    def _apply_shadow_guardrail(self, guard):
        """Light live guard: tighten size/offset when paper↔live / fill quality is adverse."""
        if not bool(self.settings.get("shadow_guardrail_enabled", True)):
            self._shadow_guard_active = None
            if hasattr(self, "home_shadow_chip"):
                self.home_shadow_chip.setText("Shadow: off")
                self.home_shadow_chip.setToolTip("Shadow guardrail disabled in Settings.")
            return
        guard = guard or {}
        self._shadow_guard_active = guard if guard.get("tighten") else None
        if hasattr(self, "home_shadow_chip"):
            st = guard.get("status") or "—"
            if guard.get("tighten"):
                self.home_shadow_chip.setText(
                    f"Shadow: tighten ×{float(guard.get('size_mult') or 1):.2f}"
                )
                self.home_shadow_chip.setStyleSheet(
                    f"font-size: {ui_px(11)}px; font-weight: 600; color: #EF6C00; "
                    f"padding: {ui_px(2)}px {ui_px(8)}px;"
                )
            else:
                self.home_shadow_chip.setText(f"Shadow: {st}")
                self.home_shadow_chip.setStyleSheet(
                    f"font-size: {ui_px(11)}px; font-weight: 600; "
                    f"padding: {ui_px(2)}px {ui_px(8)}px;"
                )
            self.home_shadow_chip.setToolTip(str(guard.get("tip") or ""))
        if guard.get("tighten"):
            tip = guard.get("tip") or "Shadow guardrail tightening size/offset."
            self._coach_tip("shadow_guardrail", tip, cooldown_sec=1800)

    def build_settings_screen(self):
        tab = QWidget()
        scroll = CompactScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # Brokers — status summary here; credentials live in per-broker dialogs
        brokers_group = QGroupBox("Brokers")
        brokers_outer = QVBoxLayout()
        brokers_outer.setSpacing(ui_px(8))
        brokers_hint = QLabel("Open a broker to enter login credentials and connect.")
        brokers_hint.setObjectName("settingsHint")
        brokers_hint.setStyleSheet(
            f"color: {theme_colors(self.dark_mode)['muted']}; font-size: {ui_px(11)}px;"
        )
        brokers_outer.addWidget(brokers_hint)

        rh_row = QHBoxLayout()
        rh_row.setSpacing(ui_px(10))
        rh_name = QLabel("Robinhood")
        rh_name.setMinimumWidth(ui_px(100))
        rh_row.addWidget(rh_name)
        self.rh_status_lbl = QLabel("🔴 Disconnected")
        self.rh_status_lbl.setMinimumWidth(ui_px(130))
        rh_row.addWidget(self.rh_status_lbl)
        rh_row.addStretch(1)
        self.rh_manage_btn = QPushButton("Login…")
        self.rh_manage_btn.setToolTip("Email / password and Connect for Robinhood")
        self.rh_manage_btn.clicked.connect(self._open_robinhood_login_dialog)
        rh_row.addWidget(self.rh_manage_btn)
        brokers_outer.addLayout(rh_row)

        cb_row = QHBoxLayout()
        cb_row.setSpacing(ui_px(10))
        cb_name = QLabel("Coinbase")
        cb_name.setMinimumWidth(ui_px(100))
        cb_row.addWidget(cb_name)
        self.cb_status_lbl = QLabel("🔴 Disconnected")
        self.cb_status_lbl.setMinimumWidth(ui_px(130))
        cb_row.addWidget(self.cb_status_lbl)
        cb_row.addStretch(1)
        self.cb_manage_btn = QPushButton("Login…")
        self.cb_manage_btn.setToolTip("CDP API key / secret and Connect for Coinbase Advanced")
        self.cb_manage_btn.clicked.connect(self._open_coinbase_login_dialog)
        cb_row.addWidget(self.cb_manage_btn)
        brokers_outer.addLayout(cb_row)

        et_row = QHBoxLayout()
        et_row.setSpacing(ui_px(10))
        et_name = QLabel("E*TRADE")
        et_name.setMinimumWidth(ui_px(100))
        et_row.addWidget(et_name)
        self.et_status_lbl = QLabel("🔴 Disconnected")
        self.et_status_lbl.setMinimumWidth(ui_px(130))
        et_row.addWidget(self.et_status_lbl)
        et_row.addStretch(1)
        self.et_manage_btn = QPushButton("Login…")
        self.et_manage_btn.setToolTip("OAuth consumer key / authorize / account picker for E*TRADE")
        self.et_manage_btn.clicked.connect(self._open_etrade_login_dialog)
        et_row.addWidget(self.et_manage_btn)
        brokers_outer.addLayout(et_row)

        brokers_group.setLayout(brokers_outer)
        layout.addWidget(brokers_group)

        # Cost basis — manual paste for pre-app bags when broker/journal can't seed
        basis_group = QGroupBox("Cost basis")
        basis_outer = QVBoxLayout()
        basis_outer.setSpacing(ui_px(6))
        basis_hint = QLabel(
            "Pre-app bags need avg cost for TTP/ROI. Brokers are tried first "
            "(RH cost_basis / Coinbase portfolio entry). If still unknown, paste "
            "one line per bag from the broker app, then Apply."
        )
        basis_hint.setObjectName("settingsHint")
        basis_hint.setWordWrap(True)
        basis_hint.setStyleSheet(
            f"color: {theme_colors(self.dark_mode)['muted']}; font-size: {ui_px(11)}px;"
        )
        basis_outer.addWidget(basis_hint)
        self.cost_basis_paste = QTextEdit()
        self.cost_basis_paste.setPlaceholderText(
            "# examples\nCoinbase:ETH=2450.00\nRobinhood SHIB 0.000012\nCB FET 0.55"
        )
        self.cost_basis_paste.setMaximumHeight(ui_px(90))
        self.cost_basis_paste.setToolTip(
            "Format: Broker TICKER avg   or   Broker:TICKER=avg   (CB/RH aliases ok)"
        )
        basis_outer.addWidget(self.cost_basis_paste)
        basis_btn_row = QHBoxLayout()
        self.cost_basis_apply_btn = QPushButton("Apply pasted avg costs")
        self.cost_basis_apply_btn.setToolTip(
            "Merge into cost_basis_cache, persist, and unlock TTP for those tickers"
        )
        self.cost_basis_apply_btn.clicked.connect(self._apply_pasted_cost_basis)
        basis_btn_row.addWidget(self.cost_basis_apply_btn)
        basis_btn_row.addStretch(1)
        basis_outer.addLayout(basis_btn_row)
        basis_group.setLayout(basis_outer)
        layout.addWidget(basis_group)

        self._build_broker_login_dialogs()
        self._build_discord_webhook_dialog()

        # Getting Started — reopen the first-run wizard anytime
        gs_row = QHBoxLayout()
        gs_row.setSpacing(ui_px(10))
        self.getting_started_btn = QPushButton("Getting Started…")
        self.getting_started_btn.setToolTip(
            "Risk posture, broker connect, and Discord — same 3-step wizard as first launch"
        )
        self.getting_started_btn.clicked.connect(
            lambda: self._open_first_run_wizard(force=True)
        )
        gs_row.addWidget(self.getting_started_btn)
        gs_row.addStretch(1)
        layout.addLayout(gs_row)

        # --- Main Settings: Risk Posture first; fine-tunes under Advanced ---
        from scoring import RISK_POSTURE_PROFILES, normalize_risk_posture, get_risk_posture_profile

        posture_group = QGroupBox("Risk Posture")
        posture_outer = QVBoxLayout()
        posture_outer.setSpacing(ui_px(6))
        posture_intro = QLabel(
            "Pick how hard the auto-trader presses — concentration, cash buffer, "
            "exits, scale-in, and opportunity-swap. Fine-tunes live under Advanced."
        )
        posture_intro.setObjectName("settingsHint")
        posture_intro.setWordWrap(True)
        posture_intro.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        posture_outer.addWidget(posture_intro)

        posture_box = QHBoxLayout()
        posture_box.addWidget(QLabel("Mode:"))
        self.risk_posture_combo = QComboBox()
        self._risk_posture_keys = ["safer", "balanced", "aggressive", "growth"]
        for key in self._risk_posture_keys:
            prof = RISK_POSTURE_PROFILES[key]
            self.risk_posture_combo.addItem(prof["label"], key)
        saved_posture = normalize_risk_posture(self.settings.get("risk_posture", "balanced"))
        posture_idx = self._risk_posture_keys.index(saved_posture)
        self.risk_posture_combo.setCurrentIndex(posture_idx)
        self.risk_posture_combo.setToolTip(
            "Safer: diversify & bank sooner  ·  Balanced: default rails  ·  "
            "Aggressive: concentrate into fewer larger tickets  ·  "
            "Growth: small-book mode (~$50–$500) — faster takes, 2 buys/cycle"
        )
        self.risk_posture_combo.currentIndexChanged.connect(self._on_risk_posture_changed)
        posture_box.addWidget(self.risk_posture_combo)
        posture_box.addStretch()
        posture_outer.addLayout(posture_box)
        self.risk_posture_hint = QLabel(get_risk_posture_profile(saved_posture).get("hint", ""))
        self.risk_posture_hint.setObjectName("settingsHint")
        self.risk_posture_hint.setWordWrap(True)
        self.risk_posture_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        posture_outer.addWidget(self.risk_posture_hint)

        broker_posture_lbl = QLabel("Per-broker override (blank = use global Mode above):")
        broker_posture_lbl.setObjectName("settingsHint")
        broker_posture_lbl.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        posture_outer.addWidget(broker_posture_lbl)
        self._broker_posture_combos = {}
        by_broker = self.settings.get("risk_posture_by_broker") or {}
        for bname in BROKER_NAMES:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{bname}:"))
            combo = QComboBox()
            combo.addItem("(global)", "")
            for key in self._risk_posture_keys:
                combo.addItem(RISK_POSTURE_PROFILES[key]["label"], key)
            saved_b = normalize_risk_posture(by_broker.get(bname) or "")
            if by_broker.get(bname):
                idx = combo.findData(saved_b)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            row.addWidget(combo)
            row.addStretch()
            self._broker_posture_combos[bname] = combo
            posture_outer.addLayout(row)

        posture_group.setLayout(posture_outer)
        layout.addWidget(posture_group)

        # Desk Advisor & remote API — collapsed by default
        adv_toggle_row = QHBoxLayout()
        self.advisor_settings_summary_lbl = QLabel("")
        self.advisor_settings_summary_lbl.setObjectName("settingsHint")
        self.advisor_settings_summary_lbl.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        adv_toggle_row.addWidget(self.advisor_settings_summary_lbl, 1)
        self.advisor_settings_btn = QPushButton("Show Desk Advisor…")
        self.advisor_settings_btn.setToolTip(
            "Advisor ask-before-apply, phone approve/reject, arm/disarm, halt, EOD API"
        )
        self.advisor_settings_btn.clicked.connect(self._toggle_advisor_settings)
        adv_toggle_row.addWidget(self.advisor_settings_btn)
        layout.addLayout(adv_toggle_row)

        self.advisor_settings_group = QGroupBox("Desk Advisor & remote API")
        self.advisor_settings_group.setVisible(False)
        advisor_outer = QVBoxLayout()
        advisor_outer.setSpacing(ui_px(6))
        advisor_intro = QLabel(
            "Desk Advisor proposes BUYs for your approval. Companion Controls enables "
            "phone actions when the web monitor is on with user/pass set."
        )
        advisor_intro.setObjectName("settingsHint")
        advisor_intro.setWordWrap(True)
        advisor_intro.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        advisor_outer.addWidget(advisor_intro)

        advisor_row = QHBoxLayout()
        self.advisor_ask_chk = QCheckBox("Ask before applying Advisor changes (auto buys)")
        self.advisor_ask_chk.setChecked(bool(self.settings.get("advisor_ask_before_apply", True)))
        self.advisor_ask_chk.setToolTip(
            "When ON: scanner proposes BUYs on Home + companion; you approve before live orders. "
            "When OFF: armed auto-trader buys immediately (legacy behavior)."
        )
        advisor_row.addWidget(self.advisor_ask_chk)
        advisor_row.addStretch()
        advisor_outer.addLayout(advisor_row)

        remote_row = QHBoxLayout()
        self.monitor_controls_main_chk = QCheckBox(
            "Companion Controls — phone: approve/reject Advisor, arm/disarm, halt, EOD"
        )
        self.monitor_controls_main_chk.setChecked(
            bool(self.settings.get("monitor_controls_enabled", False))
        )
        remote_row.addWidget(self.monitor_controls_main_chk)
        remote_row.addStretch()
        advisor_outer.addLayout(remote_row)

        api_hint = QLabel(
            "Remote API (monitor ON + auth + controls ON): "
            "/api/advisor/approve · /api/halt · /api/eod/run"
        )
        api_hint.setObjectName("settingsHint")
        api_hint.setWordWrap(True)
        api_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        advisor_outer.addWidget(api_hint)

        self.advisor_settings_group.setLayout(advisor_outer)
        layout.addWidget(self.advisor_settings_group)
        self._update_advisor_settings_summary()

        # Companion quick link; Discord lives in its own main-page group below
        self._build_companion_monitor_dialog()
        companion_row = QHBoxLayout()
        companion_row.setSpacing(ui_px(10))
        companion_row.addWidget(QLabel("Web Monitor & Companion:"))
        self.companion_status_lbl = QLabel("—")
        self.companion_status_lbl.setMinimumWidth(ui_px(200))
        companion_row.addWidget(self.companion_status_lbl, 1)
        self.companion_cfg_btn = QPushButton("Companion…")
        self.companion_cfg_btn.setToolTip(
            "Bind, auth, HTTPS, TLS fingerprint, setup QR, and per-broker arm/disarm"
        )
        self.companion_cfg_btn.clicked.connect(self._open_companion_monitor_dialog)
        companion_row.addWidget(self.companion_cfg_btn)
        layout.addLayout(companion_row)
        self._update_companion_monitor_status()

        # Discord — collapsed by default (webhook in Webhook… dialog)
        discord_toggle_row = QHBoxLayout()
        self.discord_settings_summary_lbl = QLabel("")
        self.discord_settings_summary_lbl.setObjectName("settingsHint")
        self.discord_settings_summary_lbl.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        discord_toggle_row.addWidget(self.discord_settings_summary_lbl, 1)
        self.discord_settings_btn = QPushButton("Show Discord…")
        self.discord_settings_btn.clicked.connect(self._toggle_discord_settings)
        discord_toggle_row.addWidget(self.discord_settings_btn)
        layout.addLayout(discord_toggle_row)

        discord_group = QGroupBox("Discord")
        self.discord_settings_group = discord_group
        discord_group.setVisible(False)
        discord_outer = QVBoxLayout()
        discord_outer.setSpacing(ui_px(6))

        discord_wh_box = QHBoxLayout()
        discord_wh_box.setSpacing(ui_px(10))
        discord_wh_box.addWidget(QLabel("Webhook:"))
        self.discord_webhook_status_lbl = QLabel("Not set")
        self.discord_webhook_status_lbl.setMinimumWidth(ui_px(140))
        discord_wh_box.addWidget(self.discord_webhook_status_lbl)
        discord_wh_box.addStretch(1)
        self.discord_webhook_btn = QPushButton("Webhook…")
        self.discord_webhook_btn.setToolTip("Edit Discord webhook URL")
        self.discord_webhook_btn.clicked.connect(self._open_discord_webhook_dialog)
        discord_wh_box.addWidget(self.discord_webhook_btn)
        discord_outer.addLayout(discord_wh_box)
        self._update_discord_webhook_status()

        discord_lvl_box = QHBoxLayout()
        discord_lvl_box.addWidget(QLabel("Notification level:"))
        self.discord_lvl_combo = QComboBox()
        self.discord_lvl_combo.addItems([
            "All Alerts (Every Trade & Heartbeat)",
            "Important Only (Critical Alerts & Hourly Heartbeat)",
            "Disabled Completely"
        ])
        self.discord_lvl_combo.setToolTip(
            "Important Only: critical alerts, big-win sells, day profit/loss limits, "
            "and hourly heartbeat (routine trade fills are suppressed)."
        )
        saved_lvl = self.settings.get("discord_alert_level", "All Alerts (Every Trade & Heartbeat)")
        index = self.discord_lvl_combo.findText(saved_lvl)
        if index >= 0:
            self.discord_lvl_combo.setCurrentIndex(index)
        discord_lvl_box.addWidget(self.discord_lvl_combo)
        discord_outer.addLayout(discord_lvl_box)

        hb_box = QHBoxLayout()
        hb_box.addWidget(QLabel("Heartbeat (once per hour):"))
        self.discord_hb_combo = QComboBox()
        self.discord_hb_combo.addItems([
            "Rolling (every hour from now)",
            "Align to :00 (top of hour)",
            "Align to :15",
            "Align to :30",
            "Align to :45",
        ])
        saved_hb = self.settings.get("discord_heartbeat_schedule", "Rolling (every hour from now)")
        legacy = {
            "On the hour (:00)": "Align to :00 (top of hour)",
            "Every half hour (:00 / :30)": "Align to :30",
            "Every quarter hour (:00 / :15 / :30 / :45)": "Align to :00 (top of hour)",
        }
        saved_hb = legacy.get(saved_hb, saved_hb)
        hb_idx = self.discord_hb_combo.findText(saved_hb)
        if hb_idx >= 0:
            self.discord_hb_combo.setCurrentIndex(hb_idx)
        hb_box.addWidget(self.discord_hb_combo)
        hb_box.addStretch()
        discord_outer.addLayout(hb_box)

        big_win_box = QHBoxLayout()
        big_win_box.addWidget(QLabel("Big-win ROI % (Important Only):"))
        self.discord_big_win_spin = QDoubleSpinBox()
        self.discord_big_win_spin.setRange(0.1, 100.0)
        self.discord_big_win_spin.setSingleStep(0.5)
        self.discord_big_win_spin.setDecimals(1)
        self.discord_big_win_spin.setValue(float(self.settings.get("discord_big_win_roi_pct", 1.5)))
        big_win_box.addWidget(self.discord_big_win_spin)
        big_win_box.addStretch()
        discord_outer.addLayout(big_win_box)
        test_wh_row = QHBoxLayout()
        self.discord_test_btn = QPushButton("Test webhook")
        self.discord_test_btn.setToolTip("Send a one-shot test message to the saved Discord webhook")
        self.discord_test_btn.clicked.connect(self._test_discord_webhook)
        test_wh_row.addWidget(self.discord_test_btn)
        test_wh_row.addStretch()
        discord_outer.addLayout(test_wh_row)
        discord_group.setLayout(discord_outer)
        layout.addWidget(discord_group)
        self._update_discord_settings_summary()

        adv_toggle_row = QHBoxLayout()
        self.advanced_settings_btn = QPushButton("Show Advanced…")
        self.advanced_settings_btn.setToolTip(
            "Allocation floors, util/slots, day limits, scale-in, engine intervals"
        )
        self.advanced_settings_btn.clicked.connect(self._toggle_advanced_settings)
        adv_toggle_row.addWidget(self.advanced_settings_btn)
        adv_toggle_row.addStretch()
        layout.addLayout(adv_toggle_row)

        self.advanced_settings_group = QGroupBox("Advanced")
        self.advanced_settings_group.setVisible(False)
        form_layout = QVBoxLayout()
        form_layout.setSpacing(ui_px(6))

        scale_box = QHBoxLayout()
        self.allow_scale_in_chk = QCheckBox("Allow scale-in (add near support on held names)")
        _si_default = get_risk_posture_profile(saved_posture).get("allow_scale_in", True)
        if "allow_scale_in" in self.settings and self.settings.get("allow_scale_in") is not None:
            _si_default = bool(self.settings.get("allow_scale_in"))
        self.allow_scale_in_chk.setChecked(bool(_si_default))
        self.allow_scale_in_chk.setToolTip(
            "When ON, already-held tickers may get a smaller add near support if ROI "
            "is in the posture add band. Changing Risk Posture resets scale-in bands."
        )
        scale_box.addWidget(self.allow_scale_in_chk)
        scale_box.addStretch()
        form_layout.addLayout(scale_box)
        self.scale_in_hint = QLabel(
            "Scale-in bands come from Risk Posture (above hard stop). "
            "Save after Advanced edits to keep custom size/max-adds."
        )
        self.scale_in_hint.setObjectName("settingsHint")
        self.scale_in_hint.setWordWrap(True)
        self.scale_in_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(self.scale_in_hint)

        frac_box = QHBoxLayout()
        self.prefer_whole_shares_chk = QCheckBox("Prefer whole shares (RH stops eligible)")
        self.prefer_whole_shares_chk.setChecked(
            bool(self.settings.get("prefer_whole_shares_for_stops", True))
        )
        self.prefer_whole_shares_chk.setToolTip(
            "When ON, RH equity buys round down to whole shares when affordable so "
            "broker protective stops can attach. Small-BP sub-1 share still allowed "
            "if TTP-only fractionals are enabled below."
        )
        frac_box.addWidget(self.prefer_whole_shares_chk)
        self.allow_frac_ttp_chk = QCheckBox("Allow sub-1 share (TTP-only / stop N/A)")
        self.allow_frac_ttp_chk.setChecked(
            bool(self.settings.get("allow_fractional_ttp_only", True))
        )
        self.allow_frac_ttp_chk.setToolTip(
            "Default ON for Small-BP: allow fractional entries under 1 share with "
            "software TTP only (RH broker stops N/A). Turn OFF to require whole shares."
        )
        frac_box.addWidget(self.allow_frac_ttp_chk)
        frac_box.addStretch()
        form_layout.addLayout(frac_box)

        shadow_box = QHBoxLayout()
        self.shadow_guard_chk = QCheckBox("Paper↔live shadow guardrail (tighten size on adverse fills)")
        self.shadow_guard_chk.setChecked(bool(self.settings.get("shadow_guardrail_enabled", True)))
        self.shadow_guard_chk.setToolTip(
            "When recent adverse fill-quality or paper/live divergence exceeds thresholds, "
            "temporarily shrink size and bump limit offset. Status on Reports + Home."
        )
        shadow_box.addWidget(self.shadow_guard_chk)
        shadow_box.addStretch()
        form_layout.addLayout(shadow_box)

        sbp_row = QHBoxLayout()
        self.small_bp_preset_btn = QPushButton("Apply Growth preset (small book)")
        self.small_bp_preset_btn.setToolTip(
            "For ~$50–$500 books: Growth posture, 3 focus slots, 2 buys/cycle, "
            "wider DD pause, faster green takes"
        )
        self.small_bp_preset_btn.clicked.connect(self._apply_growth_preset)
        sbp_row.addWidget(self.small_bp_preset_btn)
        sbp_row.addStretch()
        form_layout.addLayout(sbp_row)

        alloc_box = QHBoxLayout()
        alloc_box.addWidget(QLabel("Stock/ETF Allocation % (baseline floor):"))
        self.alloc_stock_spin = QDoubleSpinBox()
        self.alloc_stock_spin.setRange(0.5, 50.0)
        stock_default = self.settings.get("allocation_pct_stock", self.settings.get("allocation_pct", 5.0))
        self.alloc_stock_spin.setValue(stock_default)
        alloc_box.addWidget(self.alloc_stock_spin)
        alloc_box.addStretch()
        form_layout.addLayout(alloc_box)

        alloc_crypto_box = QHBoxLayout()
        alloc_crypto_box.addWidget(QLabel("Crypto Allocation % (baseline floor):"))
        self.alloc_crypto_spin = QDoubleSpinBox()
        self.alloc_crypto_spin.setRange(0.5, 50.0)
        crypto_default = self.settings.get("allocation_pct_crypto", self.settings.get("allocation_pct", 8.0))
        self.alloc_crypto_spin.setValue(crypto_default)
        alloc_crypto_box.addWidget(self.alloc_crypto_spin)
        alloc_crypto_box.addStretch()
        form_layout.addLayout(alloc_crypto_box)

        util_box = QHBoxLayout()
        util_box.addWidget(QLabel("Target Buying-Power Utilization %:"))
        self.bp_util_spin = QDoubleSpinBox()
        self.bp_util_spin.setRange(50.0, 99.0)
        self.bp_util_spin.setSingleStep(1.0)
        self.bp_util_spin.setValue(float(self.settings.get("target_bp_utilization_pct", 88.0)))
        util_box.addWidget(self.bp_util_spin)
        util_box.addStretch()
        form_layout.addLayout(util_box)

        focus_box = QHBoxLayout()
        focus_box.addWidget(QLabel("Sizing Focus Slots (fewer = larger tickets):"))
        self.sizing_focus_spin = QSpinBox()
        self.sizing_focus_spin.setRange(1, 20)
        self.sizing_focus_spin.setValue(int(self.settings.get("sizing_focus_slots", 6)))
        focus_box.addWidget(self.sizing_focus_spin)
        focus_box.addStretch()
        form_layout.addLayout(focus_box)

        risk_pct_box = QHBoxLayout()
        risk_pct_box.addWidget(QLabel("Risk $ per trade (% equity to stop):"))
        self.risk_pct_spin = QDoubleSpinBox()
        self.risk_pct_spin.setRange(0.10, 3.0)
        self.risk_pct_spin.setSingleStep(0.05)
        self.risk_pct_spin.setDecimals(2)
        self.risk_pct_spin.setSuffix("%")
        self.risk_pct_spin.setValue(float(self.settings.get("risk_pct_per_trade", 0.75)))
        self.risk_pct_spin.setToolTip(
            "Notional ≈ (equity × this %) / stop distance. Capped by BP util, name soft-cap, book heat."
        )
        risk_pct_box.addWidget(self.risk_pct_spin)
        risk_pct_box.addStretch()
        form_layout.addLayout(risk_pct_box)

        book_risk_box = QHBoxLayout()
        book_risk_box.addWidget(QLabel("Max open book risk (% equity):"))
        self.max_open_risk_spin = QDoubleSpinBox()
        self.max_open_risk_spin.setRange(1.0, 20.0)
        self.max_open_risk_spin.setSingleStep(0.5)
        self.max_open_risk_spin.setDecimals(1)
        self.max_open_risk_spin.setSuffix("%")
        self.max_open_risk_spin.setValue(float(self.settings.get("max_open_risk_pct", 6.0)))
        book_risk_box.addWidget(self.max_open_risk_spin)
        book_risk_box.addStretch()
        form_layout.addLayout(book_risk_box)

        name_cap_box = QHBoxLayout()
        name_cap_box.addWidget(QLabel("Max Single-Name Equity % (soft cap):"))
        self.name_cap_spin = QDoubleSpinBox()
        self.name_cap_spin.setRange(5.0, 40.0)
        self.name_cap_spin.setSingleStep(1.0)
        self.name_cap_spin.setValue(float(self.settings.get("max_single_name_equity_pct", 15.0)))
        name_cap_box.addWidget(self.name_cap_spin)
        name_cap_box.addStretch()
        form_layout.addLayout(name_cap_box)

        conv_box = QHBoxLayout()
        conv_box.addWidget(QLabel("Conviction Stretch Max (high-score aim ×):"))
        self.conviction_mult_spin = QDoubleSpinBox()
        self.conviction_mult_spin.setRange(1.0, 2.5)
        self.conviction_mult_spin.setSingleStep(0.05)
        self.conviction_mult_spin.setDecimals(2)
        self.conviction_mult_spin.setValue(float(self.settings.get("conviction_alloc_mult_max", 1.50)))
        conv_box.addWidget(self.conviction_mult_spin)
        conv_box.addStretch()
        form_layout.addLayout(conv_box)

        dd_box = QHBoxLayout()
        dd_box.addWidget(QLabel("Day drawdown pause %:"))
        self.day_dd_spin = QDoubleSpinBox()
        self.day_dd_spin.setRange(1.0, 25.0)
        self.day_dd_spin.setSuffix("%")
        self.day_dd_spin.setDecimals(1)
        self.day_dd_spin.setValue(
            float(self.settings.get("day_dd_pause_pct", 0.05) or 0.05) * 100.0
        )
        dd_box.addWidget(self.day_dd_spin)
        dd_box.addWidget(QLabel("Peak DD %:"))
        self.peak_dd_spin = QDoubleSpinBox()
        self.peak_dd_spin.setRange(2.0, 40.0)
        self.peak_dd_spin.setSuffix("%")
        self.peak_dd_spin.setDecimals(1)
        self.peak_dd_spin.setValue(
            float(self.settings.get("peak_dd_pause_pct", 0.12) or 0.12) * 100.0
        )
        dd_box.addWidget(self.peak_dd_spin)
        dd_box.addWidget(QLabel("Pause min:"))
        self.dd_pause_spin = QSpinBox()
        self.dd_pause_spin.setRange(10, 240)
        self.dd_pause_spin.setValue(int(self.settings.get("dd_pause_minutes", 45) or 45))
        dd_box.addWidget(self.dd_pause_spin)
        dd_box.addStretch()
        form_layout.addLayout(dd_box)

        alloc_hint = QLabel(
            "Risk Posture retunes these knobs when you change Mode. "
            "Hard stops / cluster caps stay on. ~0.75% equity risk per trade. "
            "Day/peak DD pauses new buys without disarming ($-loss limit still disarms)."
        )
        alloc_hint.setObjectName("settingsHint")
        alloc_hint.setWordWrap(True)
        alloc_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(alloc_hint)

        self.alloc_spin = self.alloc_stock_spin

        min_dollar_box = QHBoxLayout()
        min_dollar_box.addWidget(QLabel("Minimum Order Threshold ($):"))
        self.min_dollar_spin = QDoubleSpinBox()
        self.min_dollar_spin.setRange(0.50, 500.0)
        self.min_dollar_spin.setValue(self.settings.get("min_trade_dollars", 5.0))
        min_dollar_box.addWidget(self.min_dollar_spin)
        min_dollar_box.addStretch()
        form_layout.addLayout(min_dollar_box)

        offset_box = QHBoxLayout()
        offset_box.addWidget(QLabel("Limit Order Buffer Offset %:"))
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(0.0, 5.0)
        self.offset_spin.setValue(self.settings.get("limit_offset_pct", 0.1))
        self.offset_spin.setToolTip(
            "Discretionary limit buffer (percent). RH & E*TRADE: limit at last×(1±buf). "
            "Coinbase Advanced: GTC limit buy when buffer > 0 (else market). "
            "Applies to Limit entry buys and/or Limit exit sells when those toggles are ON. "
            "Hard-stop / EOD flatten sells always use market."
        )
        offset_box.addWidget(self.offset_spin)
        offset_box.addStretch()
        form_layout.addLayout(offset_box)

        order_opts = QHBoxLayout()
        self.use_limit_entries_chk = QCheckBox("Limit entry buys")
        self.use_limit_entries_chk.setChecked(bool(self.settings.get("use_limit_entries", True)))
        self.use_limit_entries_chk.setToolTip(
            "When ON and buffer > 0: bot places limit buys (RH · ET · CB GTC). "
            "When OFF: market buys. Stop-limit *entries* are not used."
        )
        self.use_limit_exits_chk = QCheckBox("Limit exit sells")
        self.use_limit_exits_chk.setChecked(bool(self.settings.get("use_limit_exits", True)))
        self.use_limit_exits_chk.setToolTip(
            "When ON: discretionary sells (TTP / time / stale / rotate) prefer limit, "
            "then cancel→market on RH if unfilled. Hard stops and EOD flatten stay market."
        )
        self.attach_stops_chk = QCheckBox("Attach protective stops after buys")
        self.attach_stops_chk.setChecked(bool(self.settings.get("attach_protective_stops", True)))
        self.attach_stops_chk.setToolTip(
            "When ON: after equity fills, attach broker stops where supported (Robinhood). "
            "E*TRADE / Coinbase / crypto still use software TTP. Repair stops respects this too."
        )
        order_opts.addWidget(self.use_limit_entries_chk)
        order_opts.addWidget(self.use_limit_exits_chk)
        order_opts.addWidget(self.attach_stops_chk)
        order_opts.addStretch()
        form_layout.addLayout(order_opts)

        eod_opts = QHBoxLayout()
        self.et_flatten_close_chk = QCheckBox("Flatten E*TRADE equities before close")
        self.et_flatten_close_chk.setChecked(bool(self.settings.get("et_flatten_before_close", False)))
        self.et_flatten_close_chk.setToolTip(
            "Optional. At ~15:59 ET pre-close: market-sell ET equity holdings so you are not "
            "naked overnight if the app is off or midnight reauth fails. OFF by default — "
            "RH uses resting protective stops instead; crypto is 24/7."
        )
        eod_opts.addWidget(self.et_flatten_close_chk)
        eod_opts.addStretch()
        form_layout.addLayout(eod_opts)

        offset_hint = QLabel(
            "Limits = optional entry/exit preference. Protective stops = RH sell-side disaster rail. "
            "Pre-close: repair RH stops + warn ET overnight (optional flatten). "
            "Software TTP backs exits where broker stops are N/A."
        )
        offset_hint.setWordWrap(True)
        offset_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(offset_hint)

        def _sync_limit_spin_enabled(*_args):
            on = (
                self.use_limit_entries_chk.isChecked()
                or self.use_limit_exits_chk.isChecked()
            )
            self.offset_spin.setEnabled(on)

        self.use_limit_entries_chk.toggled.connect(_sync_limit_spin_enabled)
        self.use_limit_exits_chk.toggled.connect(_sync_limit_spin_enabled)
        _sync_limit_spin_enabled()

        profit_box = QHBoxLayout()
        profit_box.addWidget(QLabel("Daily Profit Target ($ [0 = Disabled]):"))
        self.profit_spin = QDoubleSpinBox()
        self.profit_spin.setRange(0.0, 10000.0)
        self.profit_spin.setSingleStep(5.0)
        self.profit_spin.setValue(self.settings.get("daily_profit_target", 0.0))
        profit_box.addWidget(self.profit_spin)
        profit_box.addStretch()
        form_layout.addLayout(profit_box)

        loss_box = QHBoxLayout()
        loss_box.addWidget(QLabel("Max Daily Loss Limit ($ [0 = Disabled]):"))
        self.loss_spin = QDoubleSpinBox()
        self.loss_spin.setRange(0.0, 10000.0)
        self.loss_spin.setSingleStep(5.0)
        self.loss_spin.setValue(self.settings.get("daily_loss_limit", 8.0))
        loss_box.addWidget(self.loss_spin)
        loss_box.addStretch()
        form_layout.addLayout(loss_box)

        max_pos_box = QHBoxLayout()
        max_pos_box.addWidget(QLabel("Max Open Positions per Broker (0 = unlimited):"))
        self.max_pos_spin = QSpinBox()
        self.max_pos_spin.setRange(0, 100)
        self.max_pos_spin.setValue(int(self.settings.get("max_open_positions", 8)))
        max_pos_box.addWidget(self.max_pos_spin)
        max_pos_box.addStretch()
        form_layout.addLayout(max_pos_box)

        max_buys_box = QHBoxLayout()
        max_buys_box.addWidget(QLabel("Max Buys per Auto Cycle:"))
        self.max_buys_spin = QSpinBox()
        self.max_buys_spin.setRange(1, 20)
        self.max_buys_spin.setValue(int(self.settings.get("max_buys_per_cycle", 2)))
        max_buys_box.addWidget(self.max_buys_spin)
        max_buys_box.addStretch()
        form_layout.addLayout(max_buys_box)

        form_layout.addWidget(QLabel("Engine Polling Intervals (Seconds):"))

        c_box = QHBoxLayout()
        c_box.addWidget(QLabel("Crypto Engine (Min 30s):"))
        self.c_spin = QSpinBox()
        self.c_spin.setRange(30, 3600)
        self.c_spin.setValue(self.settings.get("interval_crypto", 45))
        c_box.addWidget(self.c_spin)
        c_box.addStretch()
        form_layout.addLayout(c_box)

        p_box = QHBoxLayout()
        p_box.addWidget(QLabel("Breakout Engine (Min 60s):"))
        self.p_spin = QSpinBox()
        self.p_spin.setRange(60, 3600)
        self.p_spin.setValue(self.settings.get("interval_penny", 60))
        p_box.addWidget(self.p_spin)
        p_box.addStretch()
        form_layout.addLayout(p_box)

        core_box = QHBoxLayout()
        core_box.addWidget(QLabel("Core ETFs Engine (Min 120s):"))
        self.core_spin = QSpinBox()
        self.core_spin.setRange(120, 3600)
        self.core_spin.setValue(self.settings.get("interval_core", 300))
        core_box.addWidget(self.core_spin)
        core_box.addStretch()
        form_layout.addLayout(core_box)

        port_box = QHBoxLayout()
        port_box.addWidget(QLabel("Portfolio Engine (Min 30s):"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(30, 3600)
        self.port_spin.setValue(self.settings.get("interval_portfolio", 45))
        port_box.addWidget(self.port_spin)
        port_box.addStretch()
        form_layout.addLayout(port_box)

        bal_box = QHBoxLayout()
        bal_box.addWidget(QLabel("Balance Auto-Refresh (Min 30s):"))
        self.bal_spin = QSpinBox()
        self.bal_spin.setRange(30, 3600)
        self.bal_spin.setValue(int(self.settings.get("interval_balance_refresh", 60)))
        self.bal_spin.setToolTip("How often equity/cash refresh while the app is open")
        bal_box.addWidget(self.bal_spin)
        bal_box.addStretch()
        form_layout.addLayout(bal_box)

        self.advanced_settings_group.setLayout(form_layout)
        layout.addWidget(self.advanced_settings_group)

        save_settings_btn = QPushButton("Save Configuration")
        save_settings_btn.setProperty("uiBtnKind", "primary")
        save_settings_btn.setProperty("uiBtnExtra", "QPushButton { margin-top: 15px; }")
        save_settings_btn.setStyleSheet(action_btn_style("primary") + "QPushButton { margin-top: 15px; }")
        save_settings_btn.clicked.connect(self.save_custom_settings)
        layout.addWidget(save_settings_btn)

        ver_lbl = QLabel(
            f"{display_name()}"
            + (f"  ·  {VERSION_NOTE}" if VERSION_NOTE else "")
        )
        ver_lbl.setObjectName("settingsVersion")
        ver_lbl.setWordWrap(True)
        ver_lbl.setStyleSheet(
            f"color: #6B7280; font-size: {ui_px(12)}px; margin-top: {ui_px(18)}px;"
        )
        layout.addWidget(ver_lbl)

        layout.addStretch(1)
        scroll.setWidget(inner)

        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.tabs.addTab(tab, "Settings")

    def _build_broker_login_dialogs(self):
        """Credential fields live in per-broker dialogs so Settings stays trading-focused."""
        # Robinhood — non-modal show() so Connect never nests a blocking exec loop
        self._rh_login_dialog = QDialog(self)
        self._rh_login_dialog.setWindowTitle("Robinhood Login")
        self._rh_login_dialog.setWindowModality(Qt.WindowModal)
        self._rh_login_dialog.setModal(True)
        self._rh_login_dialog.setMinimumWidth(ui_px(380))
        rh_form = QFormLayout(self._rh_login_dialog)
        rh_form.setContentsMargins(ui_px(14), ui_px(12), ui_px(14), ui_px(12))
        rh_form.setSpacing(ui_px(8))
        self.rh_dialog_status_lbl = QLabel("🔴 Disconnected")
        self.rh_email_input = QLineEdit(self.settings.get("rh_email", ""))
        try:
            import credentials as cred_mod
            _rh_pwd = cred_mod.load_rh_password() or self.settings.get("rh_password", "")
        except Exception:
            _rh_pwd = self.settings.get("rh_password", "")
        self.rh_pass_input = QLineEdit(_rh_pwd)
        self.rh_pass_input.setEchoMode(QLineEdit.Password)
        self.rh_connect_btn = QPushButton("Connect Robinhood")
        self.rh_connect_btn.setProperty("uiBtnKind", "primary")
        self.rh_connect_btn.setStyleSheet(action_btn_style("primary"))
        self.rh_connect_btn.clicked.connect(self.connect_robinhood)
        self.rh_disconnect_btn = QPushButton("Disconnect")
        self.rh_disconnect_btn.setToolTip(
            "Log out and clear the saved Robinhood session so you can re-login / reauth."
        )
        self.rh_disconnect_btn.clicked.connect(self.disconnect_robinhood)
        rh_btn_row = QHBoxLayout()
        rh_btn_row.addWidget(self.rh_connect_btn)
        rh_btn_row.addWidget(self.rh_disconnect_btn)
        rh_form.addRow("Status:", self.rh_dialog_status_lbl)
        rh_form.addRow("Email:", self.rh_email_input)
        rh_form.addRow("Password:", self.rh_pass_input)
        rh_form.addRow("", rh_btn_row)

        # Coinbase Advanced (CDP)
        self._cb_login_dialog = QDialog(self)
        self._cb_login_dialog.setWindowTitle("Coinbase Advanced Login")
        self._cb_login_dialog.setModal(True)
        self._cb_login_dialog.setMinimumWidth(ui_px(420))
        cb_form = QFormLayout(self._cb_login_dialog)
        cb_form.setContentsMargins(ui_px(14), ui_px(12), ui_px(14), ui_px(12))
        cb_form.setSpacing(ui_px(8))
        self.cb_dialog_status_lbl = QLabel("🔴 Disconnected")
        self.cb_key_input = QLineEdit(self.settings.get("cb_api_key", ""))
        try:
            import credentials as cred_mod
            _cb_secret = cred_mod.load_cb_api_secret() or self.settings.get("cb_api_secret", "")
        except Exception:
            _cb_secret = self.settings.get("cb_api_secret", "")
        self.cb_secret_input = QLineEdit(_cb_secret)
        self.cb_secret_input.setEchoMode(QLineEdit.Password)
        self.cb_live_trading_chk = QCheckBox("Enable live order placement (kill switch)")
        self.cb_live_trading_chk.setChecked(bool(self.settings.get("coinbase_live_trading", True)))
        self.cb_live_trading_chk.setToolTip(
            "When unchecked, Coinbase stays connected for balances/holdings but will not place orders."
        )
        tc_cb = theme_colors(self.dark_mode)
        self.cb_live_trading_chk.setStyleSheet(
            f"QCheckBox {{ color: {tc_cb['text']}; spacing: {ui_px(8)}px; }}"
        )
        self.cb_connect_btn = QPushButton("Connect Coinbase Advanced")
        self.cb_connect_btn.setProperty("uiBtnKind", "primary")
        self.cb_connect_btn.setStyleSheet(action_btn_style("primary"))
        self.cb_connect_btn.clicked.connect(self.connect_coinbase)
        self.cb_disconnect_btn = QPushButton("Disconnect")
        self.cb_disconnect_btn.setToolTip(
            "Drop the live Coinbase session so you can reconnect / rotate API keys."
        )
        self.cb_disconnect_btn.clicked.connect(self.disconnect_coinbase)
        cb_btn_row = QHBoxLayout()
        cb_btn_row.addWidget(self.cb_connect_btn)
        cb_btn_row.addWidget(self.cb_disconnect_btn)
        self.cb_live_trading_chk.stateChanged.connect(self._on_cb_live_trading_toggled)
        cb_form.addRow("Status:", self.cb_dialog_status_lbl)
        cb_form.addRow("CDP API Key:", self.cb_key_input)
        cb_form.addRow("CDP API Secret:", self.cb_secret_input)
        cb_form.addRow("", self.cb_live_trading_chk)
        cb_form.addRow("", cb_btn_row)

        # E*TRADE OAuth
        self._et_login_dialog = QDialog(self)
        self._et_login_dialog.setWindowTitle("E*TRADE Login")
        self._et_login_dialog.setModal(True)
        self._et_login_dialog.setMinimumWidth(ui_px(460))
        et_form = QFormLayout(self._et_login_dialog)
        et_form.setContentsMargins(ui_px(14), ui_px(12), ui_px(14), ui_px(12))
        et_form.setSpacing(ui_px(8))
        self.et_dialog_status_lbl = QLabel("🔴 Disconnected")
        self.et_env_combo = QComboBox()
        self.et_env_combo.addItems(["sandbox", "live"])
        env_saved = self.settings.get("etrade_environment", "sandbox")
        idx = self.et_env_combo.findText(env_saved)
        if idx >= 0:
            self.et_env_combo.setCurrentIndex(idx)
        self.et_key_input = QLineEdit(self.settings.get("etrade_consumer_key", ""))
        self.et_secret_input = QLineEdit("")
        self.et_secret_input.setEchoMode(QLineEdit.Password)
        self.et_secret_input.setPlaceholderText("Stored in Windows Credential Manager")
        self.et_verifier_input = QLineEdit("")
        self.et_verifier_input.setPlaceholderText("Paste verification code after browser auth")
        self.et_account_combo = QComboBox()
        self.et_account_combo.setMinimumWidth(ui_px(280))
        self.et_live_trading_chk = QCheckBox("Enable live order placement (off until validated)")
        self.et_live_trading_chk.setChecked(bool(self.settings.get("etrade_live_trading", False)))
        self.et_live_trading_chk.setToolTip(
            "Sandbox-first guard. Leave unchecked until read-only live validation passes."
        )
        # Explicit contrast so the control stays readable in dark dialogs (Fusion).
        tc = theme_colors(self.dark_mode)
        self.et_live_trading_chk.setStyleSheet(
            f"QCheckBox {{ color: {tc['text']}; spacing: {ui_px(8)}px; }}"
        )
        et_btn_row = QHBoxLayout()
        self.et_auth_btn = QPushButton("Authorize in Browser")
        self.et_auth_btn.clicked.connect(self._etrade_start_oauth)
        self.et_connect_btn = QPushButton("Complete Connection")
        self.et_connect_btn.setProperty("uiBtnKind", "primary")
        self.et_connect_btn.setStyleSheet(action_btn_style("primary"))
        self.et_connect_btn.clicked.connect(self.connect_etrade)
        self.et_disconnect_btn = QPushButton("Disconnect")
        self.et_disconnect_btn.clicked.connect(self.disconnect_etrade)
        self.et_live_trading_chk.stateChanged.connect(self._on_et_live_trading_toggled)
        et_btn_row.addWidget(self.et_auth_btn)
        et_btn_row.addWidget(self.et_connect_btn)
        et_btn_row.addWidget(self.et_disconnect_btn)
        et_form.addRow("Status:", self.et_dialog_status_lbl)
        et_form.addRow("Environment:", self.et_env_combo)
        et_form.addRow("Consumer Key:", self.et_key_input)
        et_form.addRow("Consumer Secret:", self.et_secret_input)
        et_form.addRow("Verifier Code:", self.et_verifier_input)
        et_form.addRow("Account:", self.et_account_combo)
        et_form.addRow("", self.et_live_trading_chk)
        et_form.addRow("", et_btn_row)
        hint = QLabel(
            "1) Authorize in Browser → 2) paste the NEW code → 3) Complete Connection. "
            "Do not change Environment mid-flow. Tokens expire at midnight ET; "
            "secrets use Windows Credential Manager when available."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        et_form.addRow(hint)

    def _build_discord_webhook_dialog(self):
        """Webhook URL lives in a dialog so Settings does not expose it inline."""
        self._discord_webhook_dialog = QDialog(self)
        self._discord_webhook_dialog.setWindowTitle("Discord Webhook")
        self._discord_webhook_dialog.setModal(True)
        self._discord_webhook_dialog.setMinimumWidth(ui_px(460))
        form = QFormLayout(self._discord_webhook_dialog)
        form.setContentsMargins(ui_px(14), ui_px(12), ui_px(14), ui_px(12))
        form.setSpacing(ui_px(8))

        self.discord_input = QLineEdit(self.settings.get("discord_webhook", ""))
        self.discord_input.setPlaceholderText("https://discord.com/api/webhooks/…")
        self.discord_input.setEchoMode(QLineEdit.Password)
        form.addRow("Webhook URL:", self.discord_input)

        self.discord_webhook_show_chk = QCheckBox("Show URL")
        self.discord_webhook_show_chk.setToolTip("Reveal the webhook URL for paste / edit")
        self.discord_webhook_show_chk.toggled.connect(self._on_discord_webhook_show_toggled)
        form.addRow("", self.discord_webhook_show_chk)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._discord_webhook_dialog.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setProperty("uiBtnKind", "primary")
        save_btn.setStyleSheet(action_btn_style("primary"))
        save_btn.clicked.connect(self._save_discord_webhook_dialog)
        btn_row.addWidget(save_btn)
        form.addRow(btn_row)

    def _build_companion_monitor_dialog(self):
        """Web monitor + Android companion settings (keeps main Settings scannable)."""
        self._companion_monitor_dialog = QDialog(self)
        self._companion_monitor_dialog.setWindowTitle("Web Monitor & Companion")
        self._companion_monitor_dialog.setModal(True)
        self._companion_monitor_dialog.setMinimumWidth(ui_px(520))

        root = QVBoxLayout(self._companion_monitor_dialog)
        root.setContentsMargins(ui_px(14), ui_px(12), ui_px(14), ui_px(12))
        root.setSpacing(ui_px(8))

        mon_box = QHBoxLayout()
        self.monitor_enabled_chk = QCheckBox("Web Monitor")
        self.monitor_enabled_chk.setChecked(bool(self.settings.get("monitor_enabled", True)))
        mon_box.addWidget(self.monitor_enabled_chk)
        mon_box.addWidget(QLabel("Bind:"))
        self.monitor_bind_combo = QComboBox()
        self.monitor_bind_combo.addItem("This PC only (localhost)", "127.0.0.1")
        self.monitor_bind_combo.addItem("Home Wi‑Fi + away (all interfaces)", "0.0.0.0")
        saved_host = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
        self.monitor_bind_combo.setCurrentIndex(1 if saved_host == "0.0.0.0" else 0)
        mon_box.addWidget(self.monitor_bind_combo)
        mon_box.addWidget(QLabel("Port:"))
        self.monitor_port_spin = QSpinBox()
        self.monitor_port_spin.setRange(1024, 65535)
        self.monitor_port_spin.setValue(int(self.settings.get("monitor_port", 8791)))
        mon_box.addWidget(self.monitor_port_spin)
        mon_box.addStretch()
        root.addLayout(mon_box)

        mon_auth_box = QHBoxLayout()
        mon_auth_box.addWidget(QLabel("User:"))
        self.monitor_user_input = QLineEdit(self.settings.get("monitor_user", ""))
        self.monitor_user_input.setPlaceholderText("required for LAN / away")
        self.monitor_user_input.setMaximumWidth(ui_px(140))
        mon_auth_box.addWidget(self.monitor_user_input)
        mon_auth_box.addWidget(QLabel("Pass:"))
        self.monitor_pass_input = QLineEdit(self.settings.get("monitor_pass", ""))
        self.monitor_pass_input.setEchoMode(QLineEdit.Password)
        self.monitor_pass_input.setPlaceholderText("required for LAN / away")
        self.monitor_pass_input.setMaximumWidth(ui_px(140))
        mon_auth_box.addWidget(self.monitor_pass_input)
        self.monitor_https_chk = QCheckBox("HTTPS (encrypts user/pass)")
        self.monitor_https_chk.setChecked(bool(self.settings.get("monitor_https", True)))
        self.monitor_https_chk.setToolTip(
            "Always on for LAN/away bind. Encrypts credentials in transit. "
            "Failed logins lock out after 5 tries."
        )
        mon_auth_box.addWidget(self.monitor_https_chk)
        mon_auth_box.addStretch()
        root.addLayout(mon_auth_box)

        mon_ctrl_box = QHBoxLayout()
        self.monitor_controls_chk = QCheckBox(
            "Companion Controls (per-broker arm/disarm via phone — no trading)"
        )
        self.monitor_controls_chk.setChecked(bool(self.settings.get("monitor_controls_enabled", False)))
        mon_ctrl_box.addWidget(self.monitor_controls_chk)
        mon_ctrl_box.addStretch()
        root.addLayout(mon_ctrl_box)

        self.monitor_fp_lbl = QLabel("TLS fingerprint: (starts with monitor)")
        self.monitor_fp_lbl.setObjectName("settingsHint")
        self.monitor_fp_lbl.setWordWrap(True)
        self.monitor_fp_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.monitor_fp_lbl.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        root.addWidget(self.monitor_fp_lbl)

        mon_qr_box = QHBoxLayout()
        self.monitor_qr_btn = QPushButton("Show setup QR")
        self.monitor_qr_btn.setToolTip(
            "Encode applied monitor URL, user, password, and TLS fingerprint for the Android companion."
        )
        self.monitor_qr_btn.clicked.connect(self._show_companion_setup_qr)
        mon_qr_box.addWidget(self.monitor_qr_btn)
        self.monitor_clear_lockouts_btn = QPushButton("Clear lockouts")
        self.monitor_clear_lockouts_btn.setToolTip(
            "Clear failed-login lockouts so the phone can authenticate again."
        )
        self.monitor_clear_lockouts_btn.clicked.connect(self._clear_companion_auth_lockouts)
        mon_qr_box.addWidget(self.monitor_clear_lockouts_btn)
        open_apk_btn = QPushButton("Open companion APK folder")
        open_apk_btn.setToolTip(
            "Open android/app/build/outputs/apk/debug (or Google Drive apks if present)."
        )
        open_apk_btn.clicked.connect(self._open_companion_apk_folder)
        mon_qr_box.addWidget(open_apk_btn)
        mon_qr_box.addStretch()
        root.addLayout(mon_qr_box)
        self._refresh_companion_qr_button()

        mon_hint = QLabel(
            "Phone: set User/Pass, choose “Home Wi‑Fi + away”, allow port in Windows Firewall. "
            "Same Wi‑Fi → https://<pc-lan-ip>:<port>/  ·  Away → port-forward that port on your router "
            "to the PC, then https://<public-ip-or-ddns>:<port>/. "
            "Or use Show setup QR and scan in the Android companion. "
            "HTTPS encrypts login; wrong passwords lock out after 5 failures (~15 min). "
            "Use Apply & restart monitor to save companion fields and restart the server "
            "(no need to Save Configuration for other Settings)."
        )
        mon_hint.setObjectName("settingsHint")
        mon_hint.setWordWrap(True)
        mon_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        root.addWidget(mon_hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        apply_btn = QPushButton("Apply & restart monitor")
        apply_btn.setProperty("uiBtnKind", "primary")
        apply_btn.setStyleSheet(action_btn_style("primary"))
        apply_btn.setToolTip(
            "Copy these companion fields into settings, save, and restart the web monitor."
        )
        apply_btn.clicked.connect(self._apply_companion_monitor_settings)
        btn_row.addWidget(apply_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self._close_companion_monitor_dialog)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        for w in (
            self.monitor_enabled_chk,
            self.monitor_bind_combo,
            self.monitor_port_spin,
            self.monitor_user_input,
            self.monitor_pass_input,
            self.monitor_https_chk,
            self.monitor_controls_chk,
        ):
            if hasattr(w, "stateChanged"):
                w.stateChanged.connect(lambda *_: self._update_companion_monitor_status())
            elif hasattr(w, "currentIndexChanged"):
                w.currentIndexChanged.connect(lambda *_: self._update_companion_monitor_status())
            elif hasattr(w, "valueChanged"):
                w.valueChanged.connect(lambda *_: self._update_companion_monitor_status())
            elif hasattr(w, "textChanged"):
                w.textChanged.connect(lambda *_: self._update_companion_monitor_status())

    def _clear_companion_auth_lockouts(self):
        try:
            monitor.clear_auth_lockouts()
            self.log_event("[Companion] Auth lockouts cleared")
            QMessageBox.information(
                self,
                "Companion",
                "Auth lockouts cleared. The companion can authenticate again.",
            )
        except Exception as e:
            QMessageBox.warning(self, "Companion", f"Could not clear lockouts: {e}")

    def _apply_companion_monitor_settings(self):
        """Apply Companion dialog fields only, save, and restart the web monitor."""
        if not hasattr(self, "monitor_enabled_chk"):
            return
        self.settings["monitor_enabled"] = self.monitor_enabled_chk.isChecked()
        self.settings["monitor_port"] = int(self.monitor_port_spin.value())
        self.settings["monitor_host"] = (
            self.monitor_bind_combo.currentData() or "127.0.0.1"
        )
        self.settings["monitor_user"] = self.monitor_user_input.text().strip()
        self.settings["monitor_pass"] = self.monitor_pass_input.text()
        self.settings["monitor_https"] = bool(self.monitor_https_chk.isChecked())
        remote = self.settings["monitor_host"] not in ("127.0.0.1", "localhost", "::1")
        if remote:
            self.settings["monitor_https"] = True
            self.monitor_https_chk.setChecked(True)
        controls_wanted = bool(self.monitor_controls_chk.isChecked())
        if (remote or controls_wanted or self.settings.get("monitor_https")) and not self.settings["monitor_user"]:
            QMessageBox.warning(
                self,
                "Web Monitor",
                "LAN/away, HTTPS, and Companion Controls require a User and Password. "
                "Set them before enabling remote access.",
            )
            if remote:
                self.settings["monitor_host"] = "127.0.0.1"
                self.monitor_bind_combo.setCurrentIndex(0)
            controls_wanted = False
            self.monitor_controls_chk.setChecked(False)
        if controls_wanted and not self.settings["monitor_user"]:
            controls_wanted = False
        self.settings["monitor_controls_enabled"] = controls_wanted
        if hasattr(self, "monitor_controls_main_chk"):
            self.monitor_controls_main_chk.setChecked(controls_wanted)
        if self.settings["monitor_enabled"] and not self.settings["monitor_user"] and not remote:
            QMessageBox.information(
                self,
                "Web Monitor",
                "Tip: set a monitor User/Pass before enabling Home Wi‑Fi + away or the Android companion.",
            )
        save_settings(self.settings)
        self._start_web_monitor()
        self._update_companion_monitor_status()
        self._update_advisor_settings_summary()
        self.log_event("[Companion] Monitor settings applied and restarted")

    def _update_companion_monitor_status(self):
        if not hasattr(self, "companion_status_lbl"):
            return
        tc = theme_colors(self.dark_mode)
        runtime = {}
        try:
            runtime = monitor.describe_runtime() if hasattr(monitor, "describe_runtime") else {}
        except Exception:
            runtime = {}
        live_running = bool(runtime.get("running")) if runtime else bool(
            hasattr(monitor, "is_running") and monitor.is_running()
        )

        draft_enabled = (
            bool(self.monitor_enabled_chk.isChecked())
            if hasattr(self, "monitor_enabled_chk")
            else bool(self.settings.get("monitor_enabled", True))
        )
        if hasattr(self, "monitor_bind_combo"):
            host = self.monitor_bind_combo.currentData() or "127.0.0.1"
            port = int(self.monitor_port_spin.value())
            user = self.monitor_user_input.text().strip()
            controls = bool(self.monitor_controls_chk.isChecked())
            https = bool(self.monitor_https_chk.isChecked())
        else:
            host = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
            port = int(self.settings.get("monitor_port", 8791))
            user = (self.settings.get("monitor_user", "") or "").strip()
            controls = bool(self.settings.get("monitor_controls_enabled", False))
            https = bool(self.settings.get("monitor_https", True))
        remote = host not in ("127.0.0.1", "localhost", "::1")
        dirty = self._companion_monitor_widgets_dirty() if hasattr(self, "_companion_monitor_widgets_dirty") else False

        if not draft_enabled and not live_running:
            text = "Off"
            color = tc["muted"]
        else:
            bits = []
            if live_running:
                bits.append("live")
            elif draft_enabled:
                bits.append("draft")
            bits.append("LAN/away" if remote else "localhost")
            bits.append(f":{port}")
            live_tls = bool(runtime.get("tls")) if runtime else False
            bits.append("HTTPS" if (live_tls or https or remote) else "HTTP")
            if user or runtime.get("has_auth"):
                bits.append("auth")
            if controls or runtime.get("controls_enabled"):
                bits.append("controls")
            if dirty:
                bits.append("draft ≠ live")
            text = " · ".join(bits)
            if dirty:
                color = tc["warn"]
            elif live_running:
                color = tc["success"] if (not remote or user or runtime.get("has_auth")) else tc["warn"]
            else:
                color = tc["warn"]
        self.companion_status_lbl.setText(text)
        self.companion_status_lbl.setStyleSheet(
            f"color: {color}; font-weight: 600; font-size: {ui_px(12)}px;"
        )
        self._refresh_companion_qr_button()

    def _open_companion_monitor_dialog(self):
        if not hasattr(self, "_companion_monitor_dialog"):
            return
        self._update_companion_monitor_status()
        self._companion_monitor_dialog.exec_()

    def _close_companion_monitor_dialog(self):
        self._update_companion_monitor_status()
        if hasattr(self, "_companion_monitor_dialog"):
            self._companion_monitor_dialog.accept()

    def _on_discord_webhook_show_toggled(self, checked):
        if not hasattr(self, "discord_input"):
            return
        self.discord_input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

    def _update_discord_webhook_status(self):
        if not hasattr(self, "discord_webhook_status_lbl"):
            return
        configured = bool((self.settings.get("discord_webhook") or "").strip())
        tc = theme_colors(self.dark_mode)
        if configured:
            self.discord_webhook_status_lbl.setText("Webhook configured")
            self.discord_webhook_status_lbl.setStyleSheet(
                f"color: {tc['success']}; font-weight: 600; font-size: {ui_px(12)}px;"
            )
        else:
            self.discord_webhook_status_lbl.setText("Not set")
            self.discord_webhook_status_lbl.setStyleSheet(
                f"color: {tc['muted']}; font-size: {ui_px(12)}px;"
            )
        self._update_discord_settings_summary()

    def _open_discord_webhook_dialog(self):
        if not hasattr(self, "_discord_webhook_dialog"):
            return
        self.discord_input.setText(self.settings.get("discord_webhook", ""))
        if hasattr(self, "discord_webhook_show_chk"):
            self.discord_webhook_show_chk.setChecked(False)
        self.discord_input.setEchoMode(QLineEdit.Password)
        self._discord_webhook_dialog.exec_()

    def _save_discord_webhook_dialog(self):
        url = self.discord_input.text().strip()
        self.settings["discord_webhook"] = url
        save_settings(self.settings)
        self._update_discord_webhook_status()
        if hasattr(self, "_discord_webhook_dialog"):
            self._discord_webhook_dialog.accept()

    def _sync_broker_dialog_status(self, broker):
        """Mirror summary status into the matching login dialog label."""
        if broker == "Robinhood" and hasattr(self, "rh_dialog_status_lbl") and hasattr(self, "rh_status_lbl"):
            self.rh_dialog_status_lbl.setText(self.rh_status_lbl.text())
            self.rh_dialog_status_lbl.setStyleSheet(self.rh_status_lbl.styleSheet())
        elif broker == "Coinbase" and hasattr(self, "cb_dialog_status_lbl") and hasattr(self, "cb_status_lbl"):
            self.cb_dialog_status_lbl.setText(self.cb_status_lbl.text())
            self.cb_dialog_status_lbl.setStyleSheet(self.cb_status_lbl.styleSheet())
        elif broker == "E*TRADE" and hasattr(self, "et_dialog_status_lbl") and hasattr(self, "et_status_lbl"):
            self.et_dialog_status_lbl.setText(self.et_status_lbl.text())
            self.et_dialog_status_lbl.setStyleSheet(self.et_status_lbl.styleSheet())

    def _set_broker_status(self, broker, text, style=""):
        """Update Settings summary + dialog status for one broker."""
        if broker == "Robinhood" and hasattr(self, "rh_status_lbl"):
            self.rh_status_lbl.setText(text)
            self.rh_status_lbl.setStyleSheet(style)
            self._sync_broker_dialog_status("Robinhood")
        elif broker == "Coinbase" and hasattr(self, "cb_status_lbl"):
            self.cb_status_lbl.setText(text)
            self.cb_status_lbl.setStyleSheet(style)
            self._sync_broker_dialog_status("Coinbase")
        elif broker == "E*TRADE" and hasattr(self, "et_status_lbl"):
            self.et_status_lbl.setText(text)
            self.et_status_lbl.setStyleSheet(style)
            self._sync_broker_dialog_status("E*TRADE")

    def _open_robinhood_login_dialog(self):
        if not hasattr(self, "_rh_login_dialog"):
            return
        self._sync_broker_dialog_status("Robinhood")
        # show() — not exec_() — keeps the main event loop free (tray Exit stays usable)
        self._rh_login_dialog.show()
        self._rh_login_dialog.raise_()
        self._rh_login_dialog.activateWindow()

    def _open_coinbase_login_dialog(self):
        if not hasattr(self, "_cb_login_dialog"):
            return
        self._sync_broker_dialog_status("Coinbase")
        self._cb_login_dialog.show()
        self._cb_login_dialog.raise_()
        self._cb_login_dialog.activateWindow()

    def _open_etrade_login_dialog(self):
        if not hasattr(self, "_et_login_dialog"):
            return
        # Refresh non-secret fields from settings (secrets stay in Credential Manager)
        if hasattr(self, "et_key_input"):
            env = self.et_env_combo.currentText() if hasattr(self, "et_env_combo") else "sandbox"
            key = self.settings.get("etrade_consumer_key", "")
            if env == "live":
                key = self.settings.get("etrade_prod_consumer_key_pending") or key
            if key and not self.et_key_input.text().strip():
                self.et_key_input.setText(key)
            elif key:
                self.et_key_input.setText(key)
        if hasattr(self, "et_secret_input"):
            self.et_secret_input.clear()
            self.et_secret_input.setPlaceholderText("Leave blank if saved in Credential Manager")
        if hasattr(self, "et_env_combo"):
            env_saved = self.settings.get("etrade_environment", "sandbox")
            idx = self.et_env_combo.findText(env_saved)
            if idx >= 0:
                self.et_env_combo.setCurrentIndex(idx)
        self._sync_broker_dialog_status("E*TRADE")
        self._populate_etrade_account_combo()
        self._et_login_dialog.show()
        self._et_login_dialog.raise_()
        self._et_login_dialog.activateWindow()

    def _populate_etrade_account_combo(self):
        if not hasattr(self, "et_account_combo"):
            return
        combo = self.et_account_combo
        combo.clear()
        et = self.brokers.get("E*TRADE")
        choices = et.list_account_choices() if et and hasattr(et, "list_account_choices") else []
        saved = self.settings.get("etrade_account_id_key", "")
        sel_idx = 0
        for i, (key, label, is_ira) in enumerate(choices):
            combo.addItem(label, key)
            if key and key == saved:
                sel_idx = i
        if not choices and saved:
            combo.addItem(f"Saved · {saved}", saved)
        if combo.count():
            combo.setCurrentIndex(sel_idx)

    def _etrade_creds_base(self):
        env = self.et_env_combo.currentText() if hasattr(self, "et_env_combo") else "sandbox"
        key = self.et_key_input.text().strip() if hasattr(self, "et_key_input") else ""
        secret = self.et_secret_input.text().strip() if hasattr(self, "et_secret_input") else ""
        if not key:
            key = (self.settings.get("etrade_consumer_key") or "").strip()
            if env == "live":
                key = key or (self.settings.get("etrade_prod_consumer_key_pending") or "").strip()
        if not secret:
            try:
                from etrade_broker import load_etrade_secret
                secret = (load_etrade_secret("consumer_secret", env) or "").strip()
            except Exception:
                secret = ""
        return {
            "environment": env,
            "consumer_key": key,
            "consumer_secret": secret,
            "live_trading_enabled": bool(
                self.et_live_trading_chk.isChecked()
                if hasattr(self, "et_live_trading_chk")
                else self.settings.get("etrade_live_trading", False)
            ),
            "account_id_key": (
                self.et_account_combo.currentData()
                if hasattr(self, "et_account_combo") and self.et_account_combo.count()
                else self.settings.get("etrade_account_id_key", "")
            ),
            "token_expires_at": self.settings.get("etrade_token_expires_at", 0),
        }

    def _etrade_start_oauth(self):
        creds = self._etrade_creds_base()
        creds["start_oauth"] = True
        if not creds.get("consumer_key") and not creds.get("consumer_secret"):
            QMessageBox.warning(self, "E*TRADE", "Enter consumer key and secret first.")
            return
        if not creds.get("consumer_key"):
            QMessageBox.warning(self, "E*TRADE", "Enter the consumer key first.")
            return
        if not creds.get("consumer_secret"):
            QMessageBox.warning(
                self, "E*TRADE",
                "Consumer secret not found.\n\n"
                "Paste it in the Consumer Secret field once "
                "(it will be saved to Windows Credential Manager), then try again."
            )
            return
        try:
            from etrade_broker import store_etrade_secret
            if self.et_secret_input.text().strip():
                store_etrade_secret("consumer_secret", creds["environment"], creds["consumer_secret"])
        except Exception:
            pass
        ok, msg = self.brokers["E*TRADE"].login(creds)
        if ok and str(msg).startswith("AUTH_URL::"):
            url = str(msg).split("AUTH_URL::", 1)[1]
            webbrowser.open(url)
            self._set_broker_status("E*TRADE", "🟡 Authorize in browser…", "color: #FFD54F; font-weight: bold;")
            QMessageBox.information(
                self, "E*TRADE",
                "Browser opened for E*TRADE authorization.\n\n"
                "After approving, paste that verification code here and click "
                "Complete Connection (same dialog session).\n\n"
                "If you click Authorize again, use only the newest code."
            )
        else:
            QMessageBox.warning(self, "E*TRADE", f"Could not start OAuth: {msg}")

    def connect_etrade(self):
        creds = self._etrade_creds_base()
        verifier = self.et_verifier_input.text().strip() if hasattr(self, "et_verifier_input") else ""
        if verifier:
            creds["verifier"] = verifier
        if not creds.get("consumer_key"):
            QMessageBox.warning(self, "E*TRADE", "Enter the consumer key first.")
            return
        if not creds.get("consumer_secret"):
            QMessageBox.warning(
                self, "E*TRADE",
                "Consumer secret not found. Paste it in Consumer Secret once, then connect."
            )
            return
        try:
            from etrade_broker import store_etrade_secret
            if self.et_secret_input.text().strip():
                store_etrade_secret("consumer_secret", creds["environment"], creds["consumer_secret"])
        except Exception:
            pass
        ok, msg = self.brokers["E*TRADE"].login(creds)
        if ok:
            et = self.brokers["E*TRADE"]
            self._broker_manual_auth_needed["E*TRADE"] = False
            if hasattr(self, "_reauth_nudge_sent"):
                self._reauth_nudge_sent["E*TRADE"] = False
                self._reauth_nudge_sent["E*TRADE_SOON"] = False
            if hasattr(self, "_update_reauth_banner"):
                self._update_reauth_banner()
            self._set_broker_status("E*TRADE", "🟢 Connected", "color: #00E676; font-weight: bold;")
            self.settings["etrade_environment"] = et.environment
            self.settings["etrade_consumer_key"] = self.et_key_input.text().strip()
            self.settings["etrade_account_id_key"] = et.account_id_key or ""
            self.settings["etrade_token_expires_at"] = float(et.token_expires_at or 0)
            self.settings["etrade_live_trading"] = bool(self.et_live_trading_chk.isChecked())
            et.live_trading_enabled = bool(self.settings["etrade_live_trading"])
            save_settings(self.settings)
            self._populate_etrade_account_combo()
            if hasattr(self, "et_verifier_input"):
                self.et_verifier_input.clear()
            if hasattr(self, "et_secret_input"):
                self.et_secret_input.clear()
            self.refresh_account_balances()
            self.log_event(f"[E*TRADE] Connected ({et.environment}) account={et.account_id_key}")
            if str(et.environment).lower() != "live" and self.settings["etrade_live_trading"]:
                self.log_event(
                    "[E*TRADE] Note: Live order checkbox is ON but Environment is "
                    f"'{et.environment}' — switch to live + Complete Connection for real buys."
                )
            elif str(et.environment).lower() == "live" and not self.settings["etrade_live_trading"]:
                self.log_event(
                    "[E*TRADE] Live env connected read-only — enable Live order placement "
                    "to allow CORE/BREAKOUT buys."
                )
            self._update_autotrade_ui()
            self._maybe_restore_etrade_arm(source="connect")
        else:
            if _is_manual_auth_failure(msg):
                self._broker_manual_auth_needed["E*TRADE"] = True
                self._update_autotrade_ui()
            QMessageBox.warning(self, "Connection Failed", f"E*TRADE: {msg}")

    def disconnect_etrade(self):
        try:
            self.brokers["E*TRADE"].logout()
        except Exception:
            pass
        if self.auto_trade_enabled.get("E*TRADE"):
            self._disarm_broker("E*TRADE", notify_discord=True, clear_arm_intent=True)
        else:
            self._set_etrade_arm_intent(False)
        self._broker_manual_auth_needed["E*TRADE"] = True
        self._set_broker_status("E*TRADE", "🔴 Disconnected", "")
        self.log_event("[E*TRADE] Disconnected / token revoked.")
        self._update_autotrade_ui()

    def _on_et_live_trading_toggled(self, _state=None):
        """Persist live-order kill switch immediately (do not wait for Complete Connection)."""
        enabled = bool(
            self.et_live_trading_chk.isChecked()
            if hasattr(self, "et_live_trading_chk")
            else False
        )
        self.settings["etrade_live_trading"] = enabled
        et = self.brokers.get("E*TRADE")
        if et is not None:
            et.live_trading_enabled = enabled
        try:
            save_settings(self.settings)
        except Exception:
            pass
        env = str(
            getattr(et, "environment", None)
            or self.settings.get("etrade_environment", "sandbox")
        ).lower()
        if enabled and env != "live":
            self.log_event(
                "[E*TRADE] Live order placement ON — but Environment is still "
                f"'{env}'. Switch Environment to live and Complete Connection to place real orders."
            )
        else:
            self.log_event(
                f"[E*TRADE] Live order placement {'ON' if enabled else 'OFF'} "
                f"(env={env})."
            )
        self._update_autotrade_ui()

    def _on_cb_live_trading_toggled(self, _state=None):
        enabled = bool(
            self.cb_live_trading_chk.isChecked()
            if hasattr(self, "cb_live_trading_chk")
            else True
        )
        self.settings["coinbase_live_trading"] = enabled
        cb = self.brokers.get("Coinbase")
        if cb is not None:
            cb.live_trading_enabled = enabled
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self.log_event(f"[Coinbase] Live order placement {'ON' if enabled else 'OFF'}.")
        self._update_autotrade_ui()

    def disconnect_robinhood(self):
        """Log out + clear saved session pickle so Connect can reauth cleanly."""
        try:
            self.brokers["Robinhood"].logout()
        except Exception:
            pass
        # Clear saved session so next Connect is a real login (not stale pickle)
        try:
            from broker import robinhood_pickle_path
            path = robinhood_pickle_path()
            if path and os.path.isfile(path):
                os.remove(path)
                self.log_event(f"[Robinhood] Cleared saved session ({path}).")
        except Exception as e:
            self.log_event(f"[Robinhood] Could not clear session pickle: {e}")
        if self.auto_trade_enabled.get("Robinhood"):
            self._disarm_broker("Robinhood", notify_discord=True)
        self._broker_manual_auth_needed["Robinhood"] = True
        self._set_broker_status("Robinhood", "🔴 Disconnected", "color: #FF5252; font-weight: bold;")
        self.log_event("[Robinhood] Disconnected — use Connect to re-login / MFA.")
        self._update_autotrade_ui()

    def disconnect_coinbase(self):
        """Drop live CDP client; keys stay in Settings for reconnect."""
        try:
            self.brokers["Coinbase"].logout()
        except Exception:
            pass
        if self.auto_trade_enabled.get("Coinbase"):
            self._disarm_broker("Coinbase", notify_discord=True)
        self._broker_manual_auth_needed["Coinbase"] = True
        self._set_broker_status("Coinbase", "🔴 Disconnected", "color: #FF5252; font-weight: bold;")
        self.log_event("[Coinbase] Disconnected — use Connect to reauth (keys kept).")
        self._update_autotrade_ui()

    def connect_robinhood(self):
        """Start RH password/2FA login on a worker thread — never block the Qt UI thread."""
        if getattr(self, "_rh_login_in_flight", False):
            self.log_event("Robinhood: login already in progress.")
            return
        email = self.rh_email_input.text().strip()
        password = self.rh_pass_input.text().strip()
        if not email or not password:
            QMessageBox.warning(self, "Robinhood", "Enter email and password.")
            return

        self._rh_login_in_flight = True
        if hasattr(self, "rh_connect_btn"):
            self.rh_connect_btn.setEnabled(False)
        self._set_broker_status("Robinhood", "🟡 Connecting…", "color: #FFD54F; font-weight: bold;")
        self.set_working_state(True, "Connecting Robinhood…")
        self.log_event("Robinhood: connecting in background (UI stays responsive)…")

        def _bg():
            original_input = builtins.input
            builtins.input = self._worker_rh_input_prompt
            try:
                ok, msg = self.brokers["Robinhood"].login({
                    "email": email,
                    "password": password,
                    "store_session": True,
                })
                return {"ok": bool(ok), "msg": msg, "email": email, "password": password}
            finally:
                builtins.input = original_input

        task = BackgroundTask(_bg)
        task.result_ready.connect(
            lambda res: QTimer.singleShot(0, lambda: self._on_rh_connect_finished(res))
        )
        task.error_occurred.connect(
            lambda e: QTimer.singleShot(
                0,
                lambda: self._on_rh_connect_finished(
                    {"ok": False, "msg": str(e), "email": email, "password": password}
                ),
            )
        )
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def _on_rh_connect_finished(self, result):
        self._rh_login_in_flight = False
        if hasattr(self, "rh_connect_btn"):
            self.rh_connect_btn.setEnabled(True)
        result = result or {}
        ok = bool(result.get("ok"))
        msg = result.get("msg") or ""
        email = result.get("email") or ""
        password = result.get("password") or ""
        if ok:
            self._set_broker_status("Robinhood", "🟢 Connected", "color: #00E676; font-weight: bold;")
            self.settings["rh_email"] = email
            try:
                import credentials as cred_mod
                if password and cred_mod.store_rh_password(password):
                    self.settings["rh_password"] = ""
                elif password:
                    self.settings["rh_password"] = password
                else:
                    self.settings["rh_password"] = ""
            except Exception:
                self.settings["rh_password"] = password
            save_settings(self.settings)
            # Confirm robin_stocks wrote/kept ~/.tokens/robinhood.pickle for next launch
            try:
                from broker import robinhood_pickle_path
                pickle_ok = os.path.isfile(robinhood_pickle_path())
            except Exception:
                pickle_ok = False
            self.set_working_state(False, "Robinhood connected")
            if pickle_ok:
                self.log_event("Robinhood connected (password / 2FA path) — session saved for next launch.")
            else:
                self.log_event(
                    "Robinhood connected, but no session pickle found — next launch may require Connect again."
                )
            self.refresh_account_balances()
        else:
            self._set_broker_status("Robinhood", "🔴 Disconnected", "color: #FF5252; font-weight: bold;")
            self.set_working_state(False)
            self.log_event(f"Robinhood login failed: {msg}")
            QMessageBox.warning(self, "Connection Failed", f"Robinhood: {msg}")

    def connect_coinbase(self):
        """Coinbase CDP login on a worker thread (network must not freeze UI)."""
        if getattr(self, "_cb_login_in_flight", False):
            self.log_event("Coinbase: login already in progress.")
            return
        key = self.cb_key_input.text().strip()
        secret = self.cb_secret_input.text().strip()
        if not key or not secret:
            QMessageBox.warning(self, "Coinbase", "Enter API key and secret.")
            return

        self._cb_login_in_flight = True
        if hasattr(self, "cb_connect_btn"):
            self.cb_connect_btn.setEnabled(False)
        self._set_broker_status("Coinbase", "🟡 Connecting…", "color: #FFD54F; font-weight: bold;")
        self.set_working_state(True, "Connecting Coinbase…")

        def _bg():
            live = bool(
                self.cb_live_trading_chk.isChecked()
                if hasattr(self, "cb_live_trading_chk")
                else self.settings.get("coinbase_live_trading", True)
            )
            ok, msg = self.brokers["Coinbase"].login({
                "api_key": key,
                "api_secret": secret,
                "live_trading_enabled": live,
            })
            return {"ok": bool(ok), "msg": msg, "key": key, "secret": secret, "live": live}

        task = BackgroundTask(_bg)
        task.result_ready.connect(
            lambda res: QTimer.singleShot(0, lambda: self._on_cb_connect_finished(res))
        )
        task.error_occurred.connect(
            lambda e: QTimer.singleShot(
                0,
                lambda: self._on_cb_connect_finished(
                    {"ok": False, "msg": str(e), "key": key, "secret": secret}
                ),
            )
        )
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def _on_cb_connect_finished(self, result):
        self._cb_login_in_flight = False
        if hasattr(self, "cb_connect_btn"):
            self.cb_connect_btn.setEnabled(True)
        result = result or {}
        if result.get("ok"):
            self._set_broker_status("Coinbase", "🟢 Connected", "color: #00E676; font-weight: bold;")
            self.settings["cb_api_key"] = result.get("key") or ""
            secret = result.get("secret") or ""
            try:
                import credentials as cred_mod
                if secret and cred_mod.store_cb_api_secret(secret):
                    self.settings["cb_api_secret"] = ""
                elif secret:
                    self.settings["cb_api_secret"] = secret
                else:
                    self.settings["cb_api_secret"] = ""
            except Exception:
                self.settings["cb_api_secret"] = secret
            if "live" in result:
                self.settings["coinbase_live_trading"] = bool(result.get("live"))
            elif hasattr(self, "cb_live_trading_chk"):
                self.settings["coinbase_live_trading"] = bool(self.cb_live_trading_chk.isChecked())
            cb = self.brokers.get("Coinbase")
            if cb is not None:
                cb.live_trading_enabled = bool(self.settings.get("coinbase_live_trading", True))
            save_settings(self.settings)
            self.set_working_state(False, "Coinbase connected")
            self.log_event("Coinbase connected.")
            self.refresh_account_balances()
        else:
            self._set_broker_status("Coinbase", "🔴 Disconnected", "color: #FF5252; font-weight: bold;")
            self.set_working_state(False)
            QMessageBox.warning(self, "Connection Failed", f"Coinbase: {result.get('msg')}")

    def run_startup_sequence(self):
        """Show UI immediately; connect brokers off the main thread (avoids freeze on open)."""
        self.log_event("Connecting brokers in background...")
        self._set_broker_status("Robinhood", "🟡 Connecting…", "color: #FFD54F; font-weight: bold;")
        self._set_broker_status("Coinbase", "🟡 Connecting…", "color: #FFD54F; font-weight: bold;")
        self._set_broker_status("E*TRADE", "🟡 Connecting…", "color: #FFD54F; font-weight: bold;")
        self.set_working_state(True, "Connecting brokers…")
        task = BackgroundTask(self._bg_startup_connect)
        task.result_ready.connect(self._on_startup_connected)
        task.error_occurred.connect(
            lambda e: self._on_startup_connected(
                {"rh_ok": False, "cb_ok": False, "et_ok": False, "error": str(e)}
            )
        )
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def _bg_startup_connect(self):
        """Network logins only — never call QInputDialog from this thread."""
        result = {"rh_ok": False, "cb_ok": False, "et_ok": False, "rh_needs_password": False}
        # Prefer saved Robinhood session (fast, no 2FA UI)
        try:
            ok, _ = self.brokers["Robinhood"].login({})
            result["rh_ok"] = bool(ok)
        except Exception:
            result["rh_ok"] = False
        if not result["rh_ok"]:
            try:
                import credentials as cred_mod
                rh_pwd = cred_mod.load_rh_password() or self.settings.get("rh_password", "")
            except Exception:
                rh_pwd = self.settings.get("rh_password", "")
            if self.settings.get("rh_email") and rh_pwd:
                result["rh_needs_password"] = True

        cb_secret = ""
        try:
            import credentials as cred_mod
            cb_secret = cred_mod.load_cb_api_secret() or self.settings.get("cb_api_secret", "")
        except Exception:
            cb_secret = self.settings.get("cb_api_secret", "")
        if self.settings.get("cb_api_key") and cb_secret:
            try:
                ok, _ = self.brokers["Coinbase"].login({
                    "api_key": self.settings["cb_api_key"],
                    "api_secret": cb_secret,
                    "live_trading_enabled": bool(self.settings.get("coinbase_live_trading", True)),
                })
                result["cb_ok"] = bool(ok)
            except Exception:
                result["cb_ok"] = False

        # E*TRADE: restore same-day token quietly (no browser)
        if self.settings.get("etrade_consumer_key"):
            try:
                ok, msg = self.brokers["E*TRADE"].login({
                    "environment": self.settings.get("etrade_environment", "sandbox"),
                    "consumer_key": self.settings.get("etrade_consumer_key", ""),
                    "account_id_key": self.settings.get("etrade_account_id_key", ""),
                    "token_expires_at": self.settings.get("etrade_token_expires_at", 0),
                    "live_trading_enabled": bool(self.settings.get("etrade_live_trading", False)),
                })
                result["et_ok"] = bool(ok)
                result["et_msg"] = msg
            except Exception as e:
                result["et_ok"] = False
                result["et_msg"] = str(e)
        return result

    def _startup_rh_password_login(self):
        """
        Explicit password/2FA path — always async (same as Settings → Connect).
        Never call this from automatic startup.
        """
        if hasattr(self, "rh_email_input"):
            email = self.rh_email_input.text().strip() or self.settings.get("rh_email", "")
            try:
                import credentials as cred_mod
                password = (
                    self.rh_pass_input.text().strip()
                    or cred_mod.load_rh_password()
                    or self.settings.get("rh_password", "")
                )
            except Exception:
                password = self.rh_pass_input.text().strip() or self.settings.get("rh_password", "")
            self.rh_email_input.setText(email)
            self.rh_pass_input.setText(password)
        self.connect_robinhood()

    def _on_startup_connected(self, result):
        result = result or {}
        try:
            if result.get("cb_ok"):
                self._set_broker_status("Coinbase", "🟢 Connected", "color: #00E676; font-weight: bold;")
                self.log_event("Coinbase connected.")
            elif self.settings.get("cb_api_key"):
                self._set_broker_status("Coinbase", "🔴 Disconnected", "color: #FF5252; font-weight: bold;")
                self.log_event("Coinbase login failed.")
            else:
                self._set_broker_status("Coinbase", "🔴 Disconnected")

            if result.get("rh_ok"):
                self._set_broker_status("Robinhood", "🟢 Connected", "color: #00E676; font-weight: bold;")
                self.log_event("Robinhood connected (saved session).")
            elif result.get("rh_needs_password"):
                # Do NOT auto password/2FA here — that freezes the whole UI (and tray menu).
                # Restore already tried pickle on a background thread; only Connect when that failed.
                self._set_broker_status(
                    "Robinhood", "🔴 Sign-in needed", "color: #FF5252; font-weight: bold;"
                )
                self.log_event(
                    "Robinhood saved session missing or expired — open Settings → Connect Robinhood. "
                    "SMS 2FA only if Robinhood challenges; auto password-login is skipped so the UI stays responsive."
                )
            else:
                self._set_broker_status("Robinhood", "🔴 Disconnected", "color: #FF5252; font-weight: bold;")

            if result.get("et_ok"):
                self._set_broker_status("E*TRADE", "🟢 Connected", "color: #00E676; font-weight: bold;")
                self.log_event("E*TRADE connected (saved token).")
                self._broker_manual_auth_needed["E*TRADE"] = False
            if hasattr(self, "_reauth_nudge_sent"):
                self._reauth_nudge_sent["E*TRADE"] = False
                self._reauth_nudge_sent["E*TRADE_SOON"] = False
            if hasattr(self, "_update_reauth_banner"):
                self._update_reauth_banner()
                et = self.brokers.get("E*TRADE")
                if et and getattr(et, "token_expires_at", None):
                    self.settings["etrade_token_expires_at"] = float(et.token_expires_at)
                    self.settings["etrade_account_id_key"] = et.account_id_key or self.settings.get(
                        "etrade_account_id_key", ""
                    )
                    save_settings(self.settings)
            elif self.settings.get("etrade_consumer_key"):
                detail = result.get("et_msg") or "reauth required"
                self._set_broker_status("E*TRADE", "🔴 Reauth needed", "color: #FF5252; font-weight: bold;")
                self.log_event(f"E*TRADE not restored: {detail}")
                if _is_manual_auth_failure(detail):
                    self._broker_manual_auth_needed["E*TRADE"] = True
                    self._update_autotrade_ui()
            else:
                self._set_broker_status("E*TRADE", "🔴 Disconnected")
                self._broker_manual_auth_needed["E*TRADE"] = False
            if hasattr(self, "_reauth_nudge_sent"):
                self._reauth_nudge_sent["E*TRADE"] = False
                self._reauth_nudge_sent["E*TRADE_SOON"] = False
            if hasattr(self, "_update_reauth_banner"):
                self._update_reauth_banner()

            if result.get("error"):
                self.log_event(f"Startup connect error: {result.get('error')}")
        finally:
            self._startup_connect_finished = True
            self.set_working_state(False)
            # Balances + holdings; Discord launch ping is already armed from _post_show_init
            self.refresh_account_balances()
            self.manual_portfolio_reload(and_score=False, force=True)
            QTimer.singleShot(800, self._startup_score_portfolio_if_ready)

    def _launch_checkin_failsafe(self):
        if getattr(self, "_launch_checkin_sent", False):
            return
        if not getattr(self, "_pending_launch_checkin", False):
            return
        # Prefer real balances — wait rather than Discord $0 while connect/fetch is still going
        if not getattr(self, "_balances_fetched_once", False):
            waits = getattr(self, "_launch_failsafe_waits", 0)
            if waits < 4:
                self._launch_failsafe_waits = waits + 1
                self.log_event(
                    f"Launch check-in waiting for balances… (retry {self._launch_failsafe_waits}/4)"
                )
                QTimer.singleShot(5000, self._launch_checkin_failsafe)
                return
        self.log_event("Launch check-in failsafe — sending now (balances/connect still running).")
        self._send_discord_launch_checkin()

    def _startup_score_portfolio_if_ready(self):
        if hasattr(self, "portfolio_table") and self.portfolio_table.rowCount() > 0:
            self.manual_score_portfolio()

    # ---------------------------------------------------------
    #  STATE MACHINE: THE MASTER DIRECTOR
    # ---------------------------------------------------------
    def _reset_autotrader_banner_style(self):
        if not hasattr(self, 'at_status_frame'):
            return
        border = "#2A2F3A" if self.dark_mode else "#D8DCE3"
        self.at_status_frame.setMaximumHeight(ui_px(48))
        self.at_status_frame.setStyleSheet(
            f"QFrame#autoTraderBanner {{ background-color: transparent; border: 1px solid {border}; "
            f"border-radius: {ui_px(UI_RADIUS_FRAME)}px; padding: {ui_px(2)}px; }}"
        )
        if hasattr(self, 'at_status_lbl'):
            self.at_status_lbl.setStyleSheet(
                f"font-size: {ui_px(13)}px; font-weight: 600; background-color: transparent;"
            )
        if hasattr(self, 'bot_animator'):
            self.bot_animator.set_dark(self.dark_mode)

    def _set_engine_banner(self, text, accent_color=None):
        # Keep single-line; long status strings previously wrapped and crushed Home
        msg = str(text or "")
        if len(msg) > 110:
            msg = msg[:107] + "…"
        self.at_status_lbl.setText(msg)
        self.at_status_lbl.setToolTip(str(text or ""))
        self._monitor_banner = text
        if hasattr(self, 'bot_animator'):
            self.bot_animator.set_mode_from_banner(text, accent_color)
            self.bot_animator.set_dark(self.dark_mode)
        if accent_color:
            border = accent_color
            self.at_status_frame.setStyleSheet(
                f"QFrame#autoTraderBanner {{ background-color: transparent; border: 2px solid {border}; "
                f"border-radius: {ui_px(UI_RADIUS_FRAME)}px; padding: {ui_px(2)}px; }}"
            )
        else:
            self._reset_autotrader_banner_style()

    def _is_broker_auto_trading(self, broker_name=None):
        broker_name = broker_name or self.cycle_broker_name
        return self.auto_trade_enabled.get(broker_name, False)

    def _update_autotrade_ui(self):
        active = [b for b, on in self.auto_trade_enabled.items() if on]
        if hasattr(self, "halt_all_btn"):
            # Desktop HALT only while armed; companion/API halt stays available.
            self.halt_all_btn.setVisible(bool(active))
        if active:
            self.auto_trade_btn.setText("Auto-Trader: ON")
            self.auto_trade_btn.setStyleSheet(top_bar_btn_style(UI_DANGER))
            self.at_status_frame.setVisible(True)
            warn_brokers = []
            for name in active:
                if getattr(self, "_broker_manual_auth_needed", {}).get(name):
                    warn_brokers.append(f"{name} reauth")
                elif int(getattr(self, "_reconnect_fail_streak", {}).get(name, 0) or 0) >= 2:
                    warn_brokers.append(f"{name} reconnect")
                elif (
                    not self.paper_mode
                    and not getattr(self.brokers.get(name), "is_connected", False)
                ):
                    warn_brokers.append(f"{name} disconnected")
            if warn_brokers:
                msg = f"Auto-Trader paused — {', '.join(warn_brokers)} (still armed)"
                tip = (
                    f"{msg}\nConnect / reauthorize in Settings → Brokers, then cycles resume."
                )
                self._set_engine_banner(msg, theme_colors(self.dark_mode)["warn"])
                if hasattr(self, "at_status_lbl"):
                    self.at_status_lbl.setToolTip(tip)
            else:
                self._set_engine_banner(f"Auto-Trader Armed — {', '.join(active)}")
        else:
            self.auto_trade_btn.setText("Auto-Trader: OFF")
            self.auto_trade_btn.setStyleSheet(top_bar_btn_style("#424242"))
            self.at_status_frame.setVisible(False)
            self._reset_autotrader_banner_style()

    def _disarm_broker(self, broker_name, notify_discord=False, *, clear_arm_intent=False):
        if broker_name == "E*TRADE" and clear_arm_intent:
            self._set_etrade_arm_intent(False)
        self.auto_trade_enabled[broker_name] = False
        self.task_queue = [
            item for item in self.task_queue
            if not (isinstance(item, (tuple, list)) and item and item[0] == broker_name)
        ]
        self.log_event(f"Auto-Trader disabled for {broker_name}.")
        self._update_autotrade_ui()
        if notify_discord:
            self.send_discord_alert(
                f"🛑 Auto-Trader **DISARMED** for **{broker_name}**.",
                urgent=True,
                prefix="[RISK]",
            )

    def _set_etrade_arm_intent(self, want: bool):
        """Remember user wanted ET armed through midnight reauth / session drop."""
        prev = bool(self.settings.get("etrade_arm_intent", False))
        want = bool(want)
        if prev == want:
            return
        self.settings["etrade_arm_intent"] = want
        try:
            save_settings(self.settings)
        except Exception:
            pass

    def _maybe_restore_etrade_arm(self, *, source=""):
        """
        Re-arm E*TRADE after successful reconnect when user had it armed before
        midnight expiry or auth failure. Full OAuth still required at midnight ET —
        this only restores Auto-Trader once tokens are valid again.
        """
        if not bool(self.settings.get("etrade_arm_intent", False)):
            return
        if self.auto_trade_enabled.get("E*TRADE"):
            self._set_etrade_arm_intent(False)
            return
        et = self.brokers.get("E*TRADE")
        if not et or not getattr(et, "is_connected", False):
            return
        if getattr(self, "_broker_manual_auth_needed", {}).get("E*TRADE"):
            return
        env = str(getattr(et, "environment", None) or self.settings.get("etrade_environment", "")).lower()
        live_ok = bool(
            getattr(et, "live_trading_enabled", False)
            or self.settings.get("etrade_live_trading", False)
        )
        if env != "live" or not live_ok:
            self._throttled_log(
                "E*TRADE:arm_intent_wait_live",
                "[E*TRADE] Arm intent saved — enable Live env + Live orders, then re-arm "
                "or Complete Connection to resume CORE/BREAKOUT.",
                cooldown_sec=3600,
            )
            return
        armed = self._arm_broker_engines(["E*TRADE"], warn=False)
        if armed:
            self._set_etrade_arm_intent(False)
            src = f" ({source})" if source else ""
            self.log_event(
                f"[E*TRADE] Auto-Trader re-armed after reconnect{src} "
                "(restored from pre-midnight intent)."
            )
            self.send_discord_alert(
                "✅ [E*TRADE] Auto-Trader **re-armed** after reconnect.",
                urgent=False,
                prefix="[ET]",
            )
            self._set_engine_banner("🤖 ⚡ E*TRADE re-armed — spinning up…")
            QTimer.singleShot(0, self.director_tick)
            self._update_autotrade_ui()

    def _maybe_etrade_midnight_handling(self, now_ts=None):
        """
        At midnight ET: token dies — cannot silently renew into the next day.
        Persist arm intent so one OAuth reauth can restore trading without re-checking ET.
        """
        if self.paper_mode:
            return
        et = self.brokers.get("E*TRADE")
        if not et or not getattr(et, "client", None):
            return
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            now_et = datetime.fromtimestamp(float(now_ts or time.time()), ZoneInfo("America/New_York"))
        except Exception:
            return
        day_key = now_et.date().isoformat()
        handled = getattr(self, "_etrade_midnight_handled_day", None)
        if handled == day_key:
            return
        # First ~10 minutes after midnight ET — catch expiry once per day
        if not (now_et.hour == 0 and now_et.minute < 10):
            return
        exp = float(getattr(et, "token_expires_at", 0) or self.settings.get("etrade_token_expires_at", 0) or 0)
        if exp <= 0 or time.time() <= exp:
            return
        self._etrade_midnight_handled_day = day_key
        was_armed = bool(self.auto_trade_enabled.get("E*TRADE"))
        if was_armed:
            self._set_etrade_arm_intent(True)
        ok, msg = et.ensure_session()
        if ok:
            self.settings["etrade_token_expires_at"] = float(et.token_expires_at or 0)
            try:
                save_settings(self.settings)
            except Exception:
                pass
            self.log_event(f"[E*TRADE] Session renewed after midnight boundary ({msg}).")
            return
        if was_armed or bool(self.settings.get("etrade_arm_intent", False)):
            self.log_event(
                "[E*TRADE] Midnight ET — access token expired. "
                "Reauth in Settings (verifier required once). Auto-Trader will re-arm when connected."
            )
        if _is_manual_auth_failure(msg):
            self._handle_broker_auth_failure("E*TRADE", msg, source="midnight_et")

    def panic_halt_all(self):
        """Disarm every broker, clear queues, urgent Discord — Panic Halt All."""
        self.task_queue = []
        halted = []
        for name in BROKER_NAMES:
            if self.auto_trade_enabled.get(name):
                halted.append(name)
            self.auto_trade_enabled[name] = False
        self._set_etrade_arm_intent(False)
        self.log_event("[HALT] Panic Halt All — all brokers disarmed, queues cleared.")
        self._panic_halted = True
        self._update_autotrade_ui()
        self.publish_monitor_status()
        detail = ", ".join(halted) if halted else "none were armed"
        self.send_discord_alert(
            f"🚨 **PANIC HALT ALL** — auto-trader disarmed ({detail}). Queues cleared.",
            urgent=True,
            prefix="[RISK]",
        )
        if hasattr(self, "home_heat_lbl"):
            self._refresh_portfolio_heat()
        return {"ok": True, "halted": halted}

    def _monitor_halt_from_http(self):
        """Companion / monitor POST /api/halt."""
        try:
            return self.panic_halt_all()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _monitor_advisor_from_http(self, proposal_id, action):
        """Companion POST /api/advisor/approve — blocks until GUI handles."""
        req = {
            "id": str(proposal_id or ""),
            "action": str(action or "approve").lower().strip(),
            "event": threading.Event(),
            "result": {"ok": False, "error": "timeout"},
        }
        self._monitor_advisor_req.emit(req)
        if not req["event"].wait(timeout=12.0):
            return req.get("result") or {"ok": False, "error": "Timed out waiting for desktop app"}
        return req.get("result") or {"ok": False, "error": "No result"}

    def _on_monitor_advisor_req(self, req):
        try:
            action = str((req or {}).get("action") or "approve").lower()
            pid = str((req or {}).get("id") or "")
            if action == "approve" and not pid:
                import advisor_queue as aq
                pending = aq.list_pending(limit=1)
                if pending:
                    pid = str(pending[0].get("id") or "")
            if action in ("reject", "reject_all"):
                if action == "reject_all" or not pid:
                    import advisor_queue as aq
                    n = aq.reject_all()
                    self._refresh_advisor_card()
                    self.publish_monitor_status()
                    req["result"] = {"ok": True, "rejected": n}
                else:
                    req["result"] = self._advisor_reject_proposal(pid)
            else:
                req["result"] = self._advisor_apply_proposal(pid)
        except Exception as e:
            req["result"] = {"ok": False, "error": str(e)}
        finally:
            try:
                req["event"].set()
            except Exception:
                pass

    def _monitor_eod_from_http(self):
        """Companion POST /api/eod/run — pre-close protective checklist."""
        req = {
            "event": threading.Event(),
            "result": {"ok": False, "error": "timeout"},
        }
        self._monitor_eod_req.emit(req)
        if not req["event"].wait(timeout=30.0):
            return req.get("result") or {"ok": False, "error": "Timed out waiting for desktop app"}
        return req.get("result") or {"ok": False, "error": "No result"}

    def _on_monitor_eod_req(self, req):
        try:
            self._run_eod_protective_pass()
            req["result"] = {"ok": True, "note": "EOD protective pass queued"}
        except Exception as e:
            req["result"] = {"ok": False, "error": str(e)}
        finally:
            try:
                req["event"].set()
            except Exception:
                pass

    def _advisor_apply_proposal(self, proposal_id):
        import advisor_queue as aq
        prop = aq.claim(proposal_id)
        if not prop:
            return {"ok": False, "error": "Proposal not found or already resolved"}
        broker = str(prop.get("broker") or "")
        if broker not in BROKER_NAMES:
            aq.complete(proposal_id, False)
            return {"ok": False, "error": f"Unknown broker: {broker}"}
        self.active_broker_name = broker
        self._cycle_broker = broker
        candidate = {
            "ticker": prop.get("ticker"),
            "asset_type": prop.get("asset_type") or "",
            "price": float(prop.get("price") or 0),
            "score": float(prop.get("score") or 0),
            "_advisor_approved": True,
            "_advisor_proposal_id": proposal_id,
            "engine": prop.get("engine") or "",
        }
        self.set_working_state(True, f"Advisor BUY {prop.get('ticker')}…")
        try:
            self.run_thread(
                self._bg_buy_batch,
                lambda payload: self._on_advisor_buy_done(payload, proposal_id),
                [candidate],
                False,
                advisor_gate=False,
            )
        except Exception as e:
            aq.complete(proposal_id, False)
            return {"ok": False, "error": str(e)}
        self._refresh_advisor_card()
        self.publish_monitor_status()
        self.log_event(
            f"[Advisor] Approved BUY {prop.get('ticker')} on {broker} "
            f"~${float(prop.get('dollars') or 0):.2f} (executing)"
        )
        return {
            "ok": True,
            "id": proposal_id,
            "ticker": prop.get("ticker"),
            "broker": broker,
            "queued": True,
        }

    def _on_advisor_buy_done(self, payload, proposal_id):
        import advisor_queue as aq
        payload = payload or {}
        buys_done = int(payload.get("buys_done") or 0)
        aq.complete(proposal_id, ok=buys_done > 0)
        if buys_done <= 0:
            notes = payload.get("notes") or []
            why = notes[0] if notes else "buy did not fill — proposal restored to pending"
            self.log_event(f"[Advisor] Execute missed: {why}")
        self._on_buy_batch_done(payload, auto_mode=False, table=None)
        self._refresh_advisor_card()
        self.publish_monitor_status()

    def _advisor_reject_proposal(self, proposal_id):
        import advisor_queue as aq
        prop = aq.reject(proposal_id)
        if not prop:
            return {"ok": False, "error": "Proposal not found"}
        self._refresh_advisor_card()
        self.publish_monitor_status()
        self.log_event(f"[Advisor] Rejected {prop.get('ticker')} on {prop.get('broker')}")
        return {"ok": True, "id": proposal_id}

    def _advisor_approve_top(self):
        import advisor_queue as aq
        pending = aq.list_pending(limit=1)
        if not pending:
            QMessageBox.information(self, "Advisor", "No pending proposals.")
            return
        res = self._advisor_apply_proposal(pending[0].get("id"))
        if not res.get("ok"):
            QMessageBox.warning(self, "Advisor", res.get("error") or "Approve failed")

    def _advisor_reject_all(self):
        import advisor_queue as aq
        n = aq.reject_all()
        self._refresh_advisor_card()
        self.publish_monitor_status()
        self.log_event(f"[Advisor] Rejected all pending ({n})")

    def _refresh_advisor_card(self):
        if not hasattr(self, "home_advisor_lbl"):
            return
        import advisor_queue as aq
        on = bool(self.settings.get("advisor_ask_before_apply", True))
        self.home_advisor_card.setVisible(on)
        if not on:
            self.home_advisor_lbl.setText("Ask-before-apply OFF — auto buys fire immediately.")
            self.home_advisor_approve_btn.setEnabled(False)
            self.home_advisor_reject_btn.setEnabled(False)
            return
        pending = aq.list_pending(limit=6)
        self.home_advisor_approve_btn.setEnabled(bool(pending))
        self.home_advisor_reject_btn.setEnabled(bool(pending))
        if not pending:
            self.home_advisor_lbl.setText("No pending proposals — scanner will propose before live buys.")
            return
        lines = []
        for p in pending:
            lines.append(
                f"• {p.get('broker')} BUY {p.get('ticker')} "
                f"~${float(p.get('dollars') or 0):.2f} "
                f"({float(p.get('score') or 0):.0f} {p.get('engine') or ''})"
            )
        self.home_advisor_lbl.setText("\n".join(lines))

    def _coach_tip(self, reason_key, message, *, cooldown_sec=0):
        """One [COACH] tip per skip-reason key; optional time cooldown across cycles."""
        key = str(reason_key or "")[:120]
        if not key:
            return
        tips = getattr(self, "_coach_tip_keys", None)
        if tips is None:
            self._coach_tip_keys = set()
            tips = self._coach_tip_keys
        if key in tips:
            return
        if cooldown_sec and cooldown_sec > 0:
            store = getattr(self, "_buy_skip_throttle", None)
            if store is None:
                self._buy_skip_throttle = {}
                store = self._buy_skip_throttle
            now = time.time()
            prev = float(store.get(key) or 0.0)
            if now - prev < float(cooldown_sec):
                tips.add(key)  # suppress rest of this cycle too
                return
            store[key] = now
        tips.add(key)
        self.log_event(f"[COACH] {message}")

    def _reset_coach_tips(self):
        self._coach_tip_keys = set()

    def _throttled_buy_skip_note(self, notes, broker_name, kind, message, *, cooldown_sec=720):
        """Log BP-too-low / rotate-capped once per broker per cooldown (default 12 min)."""
        store = getattr(self, "_buy_skip_throttle", None)
        if store is None:
            self._buy_skip_throttle = {}
            store = self._buy_skip_throttle
        return _auto_cycle.throttled_buy_skip_note(
            store, notes, broker_name, kind, message,
            now=time.time(), cooldown_sec=cooldown_sec,
        )

    def _throttled_log(self, key, message, *, cooldown_sec=720):
        """Activity-log once per key per cooldown (director / idle paths)."""
        store = getattr(self, "_buy_skip_throttle", None)
        if store is None:
            self._buy_skip_throttle = {}
            store = self._buy_skip_throttle
        now = time.time()
        k = str(key or "")[:160]
        prev = float(store.get(k) or 0.0)
        if now - prev < float(cooldown_sec):
            return False
        store[k] = now
        self.log_event(message)
        return True

    def _etrade_sandbox_no_bp(self):
        """True when E*TRADE sandbox reports ~$0 BP (stub) — buy engines cannot fund."""
        if self.paper_mode:
            return False
        et = self.brokers.get("E*TRADE")
        if not et or not getattr(et, "is_connected", False):
            return False
        env = str(
            getattr(et, "environment", None)
            or self.settings.get("etrade_environment", "sandbox")
        ).lower()
        bp = float(
            (getattr(self, "_last_balance_totals", {}) or {})
            .get("E*TRADE", {})
            .get("bp", 0.0)
            or 0.0
        )
        if bp <= 0.0:
            try:
                bp = float(getattr(et, "buying_power", None) or bp or 0.0)
            except (TypeError, ValueError):
                pass
        min_d = float(self.settings.get("min_trade_dollars", 5.0) or 5.0)
        try:
            return _decision_log.etrade_sandbox_no_bp(
                paper_mode=False,
                connected=True,
                environment=env,
                buying_power=bp,
                min_trade_dollars=min_d,
            )
        except Exception:
            if env != "sandbox":
                return False
            return bp < max(0.01, min_d)

    def _buy_engines_idle_reason(self, broker_name):
        """
        When non-None, CRYPTO/PENNY/CORE should not run for this broker.
        PORTFOLIO (sells) still runs.
        """
        et = self.brokers.get("E*TRADE")
        env = str(
            getattr(et, "environment", None)
            or self.settings.get("etrade_environment", "sandbox")
        ).lower() if et else "sandbox"
        bp = float(
            (getattr(self, "_last_balance_totals", {}) or {})
            .get("E*TRADE", {})
            .get("bp", 0.0)
            or 0.0
        )
        if et and bp <= 0.0:
            try:
                bp = float(getattr(et, "buying_power", None) or bp or 0.0)
            except (TypeError, ValueError):
                pass
        min_d = float(self.settings.get("min_trade_dollars", 5.0) or 5.0)
        return _decision_log.buy_engines_idle_reason_for(
            broker_name,
            paper_mode=bool(self.paper_mode),
            etrade_connected=bool(et and getattr(et, "is_connected", False)),
            etrade_environment=env,
            etrade_buying_power=bp,
            min_trade_dollars=min_d,
        )

    def _throttle_scan_drops(self, broker, engine, dropped, *, cooldown_sec=780):
        """
        Once-per-ticker drop lines (~13 min). Returns (visible_lines, suppressed_count).
        Still skips correctly — this only gates Activity Log noise.
        """
        store = getattr(self, "_scan_drop_throttle", None)
        if store is None:
            self._scan_drop_throttle = {}
            store = self._scan_drop_throttle
        return _auto_cycle.throttle_scan_drops(
            store, broker, engine, dropped, now=time.time(), cooldown_sec=cooldown_sec,
        )

    def _coach_tip_for_scan_drops(self, broker, engine, dropped):
        """Match [COACH] wording to the dominant drop reason (not always held/cluster)."""
        key, tip = _auto_cycle.coach_tip_for_scan_drops(broker, engine, dropped)
        self._coach_tip(key, tip, cooldown_sec=780)

    def _note_frac_buy_defer(self, notes, broker_name, ticker, reason, session_label):
        """Once per ticker/session: overnight/whole-share buy defer."""
        store = getattr(self, "_frac_buy_defer_log", None)
        if store is None:
            self._frac_buy_defer_log = {}
            store = self._frac_buy_defer_log
        return _auto_cycle.note_frac_buy_defer(
            store, notes, broker_name, ticker, reason, session_label,
        )

    def _sell_fail_should_skip(self, broker, ticker):
        """True when this ticker already failed loudly and reason unchanged within TTL."""
        store = getattr(self, "_sell_fail_backoff", None) or {}
        ttl = float(getattr(self, "_sell_fail_backoff_ttl_sec", 1800) or 1800)
        return _sell_fail_should_skip_impl(store, broker, ticker, ttl_sec=ttl)

    def _record_sell_fail_backoff(self, broker, ticker, status, notes):
        """Fail loudly once, then suppress identical retries until reason changes / TTL."""
        store = getattr(self, "_sell_fail_backoff", None)
        if store is None:
            self._sell_fail_backoff = {}
            store = self._sell_fail_backoff
        ttl = float(getattr(self, "_sell_fail_backoff_ttl_sec", 1800) or 1800)
        already, note = _record_sell_fail_backoff_impl(
            store, broker, ticker, status, ttl_sec=ttl,
        )
        if already:
            return True
        if note:
            notes.append(note)
        return False

    def _clear_sell_fail_backoff(self, broker, ticker):
        store = getattr(self, "_sell_fail_backoff", None) or {}
        store.pop((str(broker), str(ticker).upper()), None)

    def _maybe_show_first_run_wizard(self):
        if bool(self.settings.get("onboarding_complete")):
            return
        self._open_first_run_wizard(force=False)

    def _open_first_run_wizard(self, *, force=False):
        """Show 3-step getting-started wizard (auto once, or Settings → Getting Started…)."""
        if not force and bool(self.settings.get("onboarding_complete")):
            return
        if not getattr(self, "_trading_tabs_built", False):
            QTimer.singleShot(400, lambda: self._open_first_run_wizard(force=force))
            return
        dlg = FirstRunWizardDialog(self, dark_mode=self.dark_mode)
        accepted = dlg.exec_() == QDialog.Accepted
        if accepted:
            if dlg.result_action == "finish":
                self._apply_wizard_finish(dlg)
            self.settings["onboarding_complete"] = True
            save_settings(self.settings)
            if dlg.result_action == "finish":
                try:
                    self.tabs.setCurrentIndex(0)
                except Exception:
                    pass
                self.log_event("Getting Started finished — settings saved.")
            else:
                self.log_event("Getting Started skipped.")
        elif not force:
            # First-run Cancel: don't nag every launch; reopen via Settings
            self.settings["onboarding_complete"] = True
            save_settings(self.settings)
            self.log_event("Getting Started dismissed.")

    def _apply_wizard_finish(self, dlg):
        """Persist posture + Discord from the wizard; brokers use existing login dialogs."""
        key = dlg.selected_posture()
        if hasattr(self, "risk_posture_combo") and hasattr(self, "_risk_posture_keys"):
            try:
                idx = self._risk_posture_keys.index(key)
                self.risk_posture_combo.blockSignals(True)
                self.risk_posture_combo.setCurrentIndex(idx)
                self.risk_posture_combo.blockSignals(False)
            except ValueError:
                pass
            self._on_risk_posture_changed()
        else:
            self.settings["risk_posture"] = key

        url = dlg.wiz_webhook.text().strip()
        self.settings["discord_webhook"] = url
        lvl = dlg.wiz_alert_combo.currentText()
        self.settings["discord_alert_level"] = lvl
        if hasattr(self, "discord_input"):
            self.discord_input.setText(url)
        if hasattr(self, "discord_lvl_combo"):
            i = self.discord_lvl_combo.findText(lvl)
            if i >= 0:
                self.discord_lvl_combo.setCurrentIndex(i)
        self._update_discord_webhook_status()
        save_settings(self.settings)

    def _test_discord_webhook(self):
        url = (self.settings.get("discord_webhook") or "").strip()
        if not url:
            QMessageBox.information(self, "Discord", "Set a webhook URL first (Webhook…).")
            return
        self.send_discord_alert(
            "Test message from Market Advisor Settings — webhook OK.",
            urgent=True,
            prefix="[HEARTBEAT]",
        )
        self.log_event("[Discord] Test webhook sent.")

    def _maybe_coach_growth_posture(self):
        """One-time tip when book is small and posture is not already Growth."""
        if getattr(self, "_growth_posture_coached", False):
            return
        try:
            from scoring import normalize_risk_posture, SMALL_BOOK_EQUITY
            posture = normalize_risk_posture(self.settings.get("risk_posture", "balanced"))
            if posture == "growth":
                self._growth_posture_coached = True
                return
            master = float(self._launch_equity_total() or 0.0)
            if master <= 0 or master >= float(SMALL_BOOK_EQUITY):
                return
            self._growth_posture_coached = True
            self._coach_tip(
                "growth_posture_small_book",
                f"Combined equity ~{format_currency(master)} — try Risk Posture → Growth "
                f"or Advanced → Apply Growth preset for larger tickets and wider DD rails.",
                cooldown_sec=86400,
            )
        except Exception:
            pass

    def _apply_growth_preset(self):
        """Growth posture + knobs tuned for small accounts (~$50–$500)."""
        from scoring import get_risk_posture_profile
        prof = get_risk_posture_profile("growth")
        if hasattr(self, "risk_posture_combo") and hasattr(self, "_risk_posture_keys"):
            try:
                idx = self._risk_posture_keys.index("growth")
                self.risk_posture_combo.blockSignals(True)
                self.risk_posture_combo.setCurrentIndex(idx)
                self.risk_posture_combo.blockSignals(False)
            except ValueError:
                pass
            self._on_risk_posture_changed()
        else:
            self.settings["risk_posture"] = "growth"
        if hasattr(self, "bp_util_spin"):
            self.bp_util_spin.setValue(float(prof.get("target_bp_utilization_pct", 92.0)))
        if hasattr(self, "sizing_focus_spin"):
            self.sizing_focus_spin.setValue(int(prof.get("sizing_focus_slots", 3)))
        if hasattr(self, "max_pos_spin"):
            self.max_pos_spin.setValue(int(prof.get("max_open_positions", 4)))
        if hasattr(self, "name_cap_spin"):
            self.name_cap_spin.setValue(float(prof.get("max_single_name_equity_pct", 25.0)))
        if hasattr(self, "max_buys_spin"):
            self.max_buys_spin.setValue(int(prof.get("max_buys_per_cycle", 2)))
        if hasattr(self, "day_dd_spin"):
            self.day_dd_spin.setValue(float(prof.get("day_dd_pause_pct", 0.10)) * 100.0)
        if hasattr(self, "peak_dd_spin"):
            self.peak_dd_spin.setValue(float(prof.get("peak_dd_pause_pct", 0.22)) * 100.0)
        if hasattr(self, "dd_pause_spin"):
            self.dd_pause_spin.setValue(int(prof.get("dd_pause_minutes", 20)))
        if hasattr(self, "allow_scale_in_chk"):
            self.allow_scale_in_chk.setChecked(bool(prof.get("allow_scale_in", True)))
        self.settings["advanced_scale_in_override"] = False
        self.log_event(
            "[COACH] Applied Growth preset (3 slots, 2 buys/cycle, peak DD 22%, faster green takes)."
        )
        QMessageBox.information(
            self,
            "Growth preset",
            "Applied Growth posture for small books. Click Save Configuration to persist.\n\n"
            "Tip: set per-broker override to Growth on Robinhood + Coinbase if global stays Balanced.",
        )

    def _apply_small_bp_preset(self):
        """Legacy alias — Growth preset replaced the old Safer-leaning Small-BP button."""
        self._apply_growth_preset()

    def _minutes_to_etrade_midnight_et(self):
        """Minutes until next midnight America/New_York (E*TRADE daily token)."""
        try:
            from datetime import datetime, timedelta
            try:
                from zoneinfo import ZoneInfo
                et = ZoneInfo("America/New_York")
            except Exception:
                et = None
            if et is not None:
                now = datetime.now(et)
            else:
                # Fallback: treat local as ET-ish
                now = datetime.now()
            nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return max(0, int((nxt - now).total_seconds() / 60))
        except Exception:
            return 9999

    def _handle_broker_auth_failure(self, broker_name, detail, *, source=""):
        """Mark needs-reauth, disarm only that broker, one Discord REAUTH alert (no cycle spam)."""
        broker_name = broker_name or getattr(self, "cycle_broker_name", None) or getattr(
            self, "_cycle_broker", None
        )
        if not broker_name or broker_name not in BROKER_NAMES:
            # Infer from error text when cycle context is missing
            text = str(detail or "").lower()
            if "e*trade" in text or "etrade" in text:
                broker_name = "E*TRADE"
            elif "robinhood" in text:
                broker_name = "Robinhood"
            elif "coinbase" in text:
                broker_name = "Coinbase"
            else:
                broker_name = "E*TRADE"
        detail_s = str(detail or "auth failure")[:240]
        src = f" ({source})" if source else ""
        already = bool(getattr(self, "_broker_manual_auth_needed", {}).get(broker_name))
        was_armed = bool(self.auto_trade_enabled.get(broker_name))

        if not hasattr(self, "_broker_manual_auth_needed"):
            self._broker_manual_auth_needed = _blank_broker_map(False)
        self._broker_manual_auth_needed[broker_name] = True

        if broker_name == "E*TRADE" and was_armed:
            self._set_etrade_arm_intent(True)

        broker = self.brokers.get(broker_name)
        if broker is not None:
            try:
                broker.is_connected = False
            except Exception:
                pass

        if broker_name == "E*TRADE":
            self._set_broker_status(
                "E*TRADE", "🔴 Reauth needed", "color: #FF5252; font-weight: bold;"
            )
        elif broker_name == "Robinhood":
            self._set_broker_status(
                "Robinhood", "🔴 Reauth needed", "color: #FF5252; font-weight: bold;"
            )
        elif broker_name == "Coinbase":
            self._set_broker_status(
                "Coinbase", "🔴 Reauth needed", "color: #FF5252; font-weight: bold;"
            )

        if was_armed:
            self._disarm_broker(broker_name, notify_discord=False)

        if not already or was_armed:
            self.log_event(
                f"[REAUTH] [{broker_name}] Auth failure{src} — auto-trader disarmed. ({detail_s})"
            )

        if not hasattr(self, "_reauth_nudge_sent"):
            self._reauth_nudge_sent = _blank_broker_map(False)
        if not self._reauth_nudge_sent.get(broker_name):
            self._reauth_nudge_sent[broker_name] = True
            self.send_discord_alert(
                f"{broker_name} needs reauthorization — auto-trader disarmed. "
                f"Open Home → Reauth / Settings → Brokers, then re-arm.",
                urgent=True,
                prefix="[REAUTH]",
            )

        if hasattr(self, "_update_reauth_banner"):
            self._update_reauth_banner()
        self._update_autotrade_ui()
        try:
            self.publish_monitor_status()
        except Exception:
            pass

    def _update_reauth_banner(self):
        """Show Home banner when E*TRADE needs reauth or <60m to midnight ET."""
        if not hasattr(self, "reauth_banner"):
            return
        need = bool(getattr(self, "_broker_manual_auth_needed", {}).get("E*TRADE"))
        mins = self._minutes_to_etrade_midnight_et()
        near = mins <= 60
        et = self.brokers.get("E*TRADE")
        connected = bool(getattr(et, "is_connected", False))
        # Show whenever reauth is needed (even if we marked disconnected), or near midnight while still connected
        show = need or (connected and near)
        self.reauth_banner.setVisible(show)
        if not show:
            return
        if need:
            self.reauth_banner_lbl.setText(
                "E*TRADE needs reauthorization — auto-trader disarmed until you reconnect."
            )
            # Discord + disarm owned by _handle_broker_auth_failure; banner only soft-disarms if still armed
            if self.auto_trade_enabled.get("E*TRADE"):
                self._disarm_broker("E*TRADE")
                self.log_event("[REAUTH] E*TRADE disarmed until reauthorization completes.")
            if not getattr(self, "_reauth_nudge_sent", {}).get("E*TRADE"):
                if not hasattr(self, "_reauth_nudge_sent"):
                    self._reauth_nudge_sent = _blank_broker_map(False)
                self._reauth_nudge_sent["E*TRADE"] = True
                self.send_discord_alert(
                    "E*TRADE needs reauthorization — auto-trader disarmed. "
                    "Open Home → Reauth E*TRADE (or Settings → Brokers).",
                    urgent=True,
                    prefix="[REAUTH]",
                )
        else:
            self.reauth_banner_lbl.setText(
                f"E*TRADE daily token expires in ~{mins}m (midnight ET). Reauth early to avoid a dead session."
            )
            if mins <= 30 and not getattr(self, "_reauth_nudge_sent", {}).get("E*TRADE_SOON"):
                if not hasattr(self, "_reauth_nudge_sent"):
                    self._reauth_nudge_sent = _blank_broker_map(False)
                self._reauth_nudge_sent["E*TRADE_SOON"] = True
                self.send_discord_alert(
                    f"E*TRADE token expires in ~{mins}m — consider reauth now.",
                    urgent=True,
                    prefix="[REAUTH]",
                )

    def _refresh_portfolio_heat(self):
        """Update Home heat strip + DD / $-loss chips from balances + open risk estimate."""
        if not hasattr(self, "home_heat_lbl"):
            return
        try:
            from scoring import portfolio_heat_snapshot
        except Exception:
            return
        totals = getattr(self, "_last_balance_totals", {}) or {}
        assets = getattr(self, "_last_assets_snapshot", None)
        holdings_by = _auto_cycle.holdings_by_broker_from_assets(assets, BROKER_NAMES)
        self._heat_holdings_by_broker = holdings_by

        rows = _auto_cycle.build_portfolio_heat_rows(
            totals,
            holdings_by,
            self.session_starts,
            self.auto_trade_enabled,
            BROKER_NAMES,
        )
        snap = portfolio_heat_snapshot(
            rows,
            settings=self.settings,
            posture=self.settings.get("risk_posture", "balanced"),
        )
        self._last_portfolio_heat = snap
        c = snap.get("combined") or {}
        risk_d = float(c.get("open_risk_dollars") or 0)
        risk_p = float(c.get("open_risk_pct") or 0)
        head = float(c.get("bp_headroom") or 0)
        room = c.get("loss_room")
        used = float(c.get("session_risk_used_pct") or 0)
        locked_total = sum(float(r.get("locked_value") or 0.0) for r in rows)
        heat_txt = _auto_cycle.format_portfolio_heat_label(
            snap, rows, money_fn=format_money, currency_fn=format_currency,
        )
        self.home_heat_lbl.setText(heat_txt)
        if c.get("dd_paused"):
            why = c.get("dd_reason") or "drawdown"
            mins = int(c.get("dd_mins_left") or 0)
            brokers = c.get("dd_brokers") or []
            peak_dd = float(c.get("peak_dd_worst_pct") or 0.0)
            eq = float(c.get("equity") or 0.0)
            small_book = 0 < eq < 500.0
            if brokers:
                btxt = ", ".join(str(b) for b in brokers[:2])
                self.home_dd_chip.setText(
                    f"DD: paused ({btxt}; {mins}m left)"
                )
            else:
                self.home_dd_chip.setText(f"DD: buys paused ({why}; {mins}m left)")
            tip = (
                f"{why}. New buys paused ~{mins}m. "
                f"Peak DD from session high: {peak_dd * 100:.1f}%."
            )
            if small_book:
                tip += " Small book (<$500): crypto min ticket $8 active."
            self.home_dd_chip.setToolTip(tip)
            self.home_dd_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; color: #E65100; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )
        else:
            eq = float(c.get("equity") or 0.0)
            peak_dd = float(c.get("peak_dd_worst_pct") or 0.0)
            small_book = 0 < eq < 500.0
            if small_book:
                self.home_dd_chip.setText("DD: ok · small-book hygiene")
                self.home_dd_chip.setToolTip(
                    f"Buys allowed. Book ~{format_money(eq)} — crypto min ticket $8, "
                    f"peak DD {peak_dd * 100:.1f}% from session high."
                )
            else:
                self.home_dd_chip.setText("DD: ok (buys allowed)")
                self.home_dd_chip.setToolTip(
                    f"Peak DD {peak_dd * 100:.1f}% from session high (pause triggers on posture thresholds)."
                )
            self.home_dd_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )
        self._refresh_locked_capital_chip()
        if c.get("loss_disarmed"):
            self.home_loss_chip.setText("$-loss: DISARMED")
            self.home_loss_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; color: #C62828; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )
        else:
            self.home_loss_chip.setText("$-loss: armed ok")
            self.home_loss_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )
        if room is None:
            self.home_session_meter_lbl.setText(f"Session risk: {used:.0f}% of $-limit (limit off or n/a)")
        else:
            self.home_session_meter_lbl.setText(
                f"Session risk: {used:.0f}% used · room {format_currency(room)}"
            )

        # Protective-stop health + cluster heat
        try:
            from scoring import (
                protective_stop_health, cluster_heat_snapshot, MAX_CLUSTER_POSITIONS,
            )
            prot_holdings = []
            all_tickers = []
            for name in BROKER_NAMES:
                b = self.brokers.get(name)
                bid = {"Robinhood": "ROBINHOOD", "Coinbase": "COINBASE", "E*TRADE": "ETRADE"}.get(name, name)
                supports = bool(getattr(b, "supports_protective_stops", False)) or self.paper_mode
                for h in holdings_by.get(name) or []:
                    t = str(h.get("ticker") or "").replace("-USD", "").upper()
                    if not t:
                        continue
                    all_tickers.append(t)
                    try:
                        val = float(h.get("value") or 0.0)
                    except (TypeError, ValueError):
                        val = 0.0
                    if val <= 0:
                        try:
                            val = abs(float(h.get("price") or h.get("live_price") or 0) * float(h.get("shares") or h.get("qty") or 0))
                        except (TypeError, ValueError):
                            val = 0.0
                    try:
                        shares = float(h.get("shares") or h.get("qty") or 0)
                    except (TypeError, ValueError):
                        shares = 0.0
                    is_c = (
                        "crypto" in str(h.get("type") or h.get("asset_type") or "").lower()
                        or t in KNOWN_CRYPTOS
                    )
                    prot_holdings.append({
                        "broker_id": bid,
                        "broker": name,
                        "ticker": t,
                        "value": val,
                        "shares": shares,
                        "is_crypto": is_c,
                        "supports_protective": supports if name != "E*TRADE" else False,
                    })
            health = protective_stop_health(prot_holdings, paper_mode=self.paper_mode)
            # Merge explicit attach gaps — exclude fractional/crypto N/A notes
            gaps = getattr(self, "_protective_gaps", {}) or {}
            frac_keys = set()
            for fn in (health.get("fractional_na") or []):
                bdisp = self._broker_display_from_id(fn.get("broker_id"))
                frac_keys.add(f"{bdisp}:{fn.get('ticker')}")
            actionable_gaps = {
                k: v for k, v in gaps.items()
                if k not in frac_keys and "fractional" not in str(v).lower()
                and "crypto" not in str(v).lower()
            }
            miss_n = int(health.get("missing_count") or 0) + len(actionable_gaps)
            frac_n = int(health.get("fractional_na_count") or 0)
            if hasattr(self, "home_stops_chip"):
                if miss_n <= 0 and frac_n <= 0:
                    self.home_stops_chip.setText(
                        f"Stops: {health.get('expected', 0)} ok"
                    )
                    self.home_stops_chip.setStyleSheet(
                        f"font-size: {ui_px(11)}px; font-weight: 600; "
                        f"padding: {ui_px(2)}px {ui_px(8)}px;"
                    )
                    self.home_stops_chip.setToolTip("Broker protective stops tracked.")
                elif miss_n <= 0 and frac_n > 0:
                    sample = [str(m.get("ticker") or "") for m in (health.get("fractional_na") or [])[:4]]
                    sample = [s for s in sample if s]
                    self.home_stops_chip.setText(
                        f"Stops: {frac_n} fractional N/A ({', '.join(sample) or '?'})"
                    )
                    self.home_stops_chip.setStyleSheet(
                        f"font-size: {ui_px(11)}px; font-weight: 600; color: #F9A825; "
                        f"padding: {ui_px(2)}px {ui_px(8)}px;"
                    )
                    self.home_stops_chip.setToolTip(
                        "Fractional equity — RH broker stop N/A; software TTP only. "
                        "Not counted as missing whole-share stops."
                    )
                else:
                    sample = []
                    for m in (health.get("missing") or [])[:3]:
                        sample.append(str(m.get("ticker") or ""))
                    sample += [k.split(":")[-1] for k in list(actionable_gaps.keys())[:3]]
                    sample = [s for s in sample if s][:4]
                    extra = f" · {frac_n} frac N/A" if frac_n else ""
                    self.home_stops_chip.setText(
                        f"Stops: {miss_n} missing ({', '.join(sample) or '?'}){extra}"
                    )
                    self.home_stops_chip.setStyleSheet(
                        f"font-size: {ui_px(11)}px; font-weight: 600; color: #C62828; "
                        f"padding: {ui_px(2)}px {ui_px(8)}px;"
                    )
                    self.home_stops_chip.setToolTip(
                        "Broker protective stop missing on whole-share equity — software TTP still runs. "
                        "Use Repair stops (or wait for auto-repair while armed). "
                        "Fractional positions use TTP only (not retryable)."
                    )
            if hasattr(self, "home_repair_stops_btn"):
                self.home_repair_stops_btn.setEnabled(miss_n > 0)
            # Fractional policy chip
            if hasattr(self, "home_frac_chip"):
                prefer = bool(self.settings.get("prefer_whole_shares_for_stops", True))
                ttp = bool(self.settings.get("allow_fractional_ttp_only", True))
                if prefer and ttp:
                    self.home_frac_chip.setText("Frac: whole+TTP")
                    tip = (
                        "Prefer whole shares when affordable (stops eligible). "
                        "Sub-1 share allowed with TTP-only (broker stop N/A)."
                    )
                elif prefer:
                    self.home_frac_chip.setText("Frac: whole only")
                    tip = "Whole shares required — sub-1 share entries blocked."
                else:
                    self.home_frac_chip.setText("Frac: allowed")
                    tip = "prefer_whole_shares off — fractional RH entries allowed (stops N/A on frac)."
                self.home_frac_chip.setToolTip(tip)
            clusters = cluster_heat_snapshot(all_tickers)
            if hasattr(self, "home_cluster_host"):
                self._render_cluster_heat(clusters)
            # Publish for companion / monitor — include Home chip's gap-adjusted missing count
            self._last_cluster_heat = clusters
            health_pub = dict(health) if isinstance(health, dict) else {}
            health_pub["missing_count"] = int(miss_n)
            self._last_protective_health = health_pub
            if hasattr(self, "home_overnight_chip"):
                et_count = 0
                for h in holdings_by.get("E*TRADE") or []:
                    if not isinstance(h, dict):
                        continue
                    t = str(h.get("ticker") or "").upper()
                    if not t:
                        continue
                    is_c = "crypto" in str(h.get("type") or "").lower() or t in KNOWN_CRYPTOS
                    if not is_c:
                        et_count += 1
                try:
                    sess = self.get_equity_session_info()
                    session_label = str(sess.get("label") or "")
                except Exception:
                    session_label = ""
                oc = _auto_cycle.overnight_scorecard(
                    protective_health=health_pub,
                    reauth_needed=getattr(self, "_broker_manual_auth_needed", {}),
                    session_label=session_label,
                    et_equity_count=et_count,
                    et_flatten_enabled=bool(self.settings.get("et_flatten_before_close", False)),
                    auto_armed=any(self.auto_trade_enabled.values()),
                )
                self._last_overnight_scorecard = oc
                grade = str(oc.get("grade") or "?")
                self.home_overnight_chip.setText(f"Overnight: {grade} — {oc.get('label') or ''}")
                color = "#2E7D32" if grade in ("A", "B") else ("#F9A825" if grade == "C" else "#C62828")
                self.home_overnight_chip.setStyleSheet(
                    f"font-size: {ui_px(11)}px; font-weight: 600; color: {color}; "
                    f"padding: {ui_px(2)}px {ui_px(8)}px;"
                )
                risks = oc.get("risks") or []
                tip = oc.get("tip") or ""
                self.home_overnight_chip.setToolTip(
                    (" · ".join(risks) if risks else "No overnight risks flagged.") + (f"\n{tip}" if tip else "")
                )
            self._refresh_basis_chips()
            self._refresh_fill_chip()
        except Exception:
            pass

        self._refresh_advisor_card()
        self._update_reauth_banner()

    def _refresh_fill_chip(self):
        """Home fill-slip / fee feedback chip (execution quality visibility)."""
        if not hasattr(self, "home_fill_chip"):
            return
        try:
            from scoring import get_execution_feedback
            fb = get_execution_feedback() or {}
        except Exception:
            fb = {}
        bump = float(fb.get("offset_bump_pct") or 0.0)
        size_m = float(fb.get("size_mult") or 1.0)
        n = int(fb.get("recent_count") or 0)
        note = str(fb.get("last_note") or "")
        avg_bps = None
        try:
            rows = journal.read_recent(40)
            slips = []
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                if r.get("slippage_bps") is None:
                    continue
                try:
                    slips.append(float(r["slippage_bps"]))
                except (TypeError, ValueError):
                    continue
            if slips:
                avg_bps = sum(slips) / len(slips)
                if n <= 0:
                    n = len(slips)
        except Exception:
            pass
        if avg_bps is None and n <= 0 and bump <= 0 and size_m >= 0.999:
            self.home_fill_chip.setText("Fill: —")
            self.home_fill_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; padding: {ui_px(2)}px {ui_px(8)}px;"
            )
            self.home_fill_chip.setToolTip(
                "Fill quality: avg slippage vs quote appears after confirmed fills. "
                "Adverse clusters temporarily bump limit offset / shrink size."
            )
            return
        avg_t = f"{avg_bps:.0f}bps" if avg_bps is not None else "—"
        adj = ""
        warn = bump > 0.001 or size_m < 0.999
        if warn:
            adj = f" · adj +{bump:.2f}%/×{size_m:.2f}"
        self.home_fill_chip.setText(f"Fill: {avg_t}{adj}" if n else f"Fill: {avg_t}")
        if warn:
            self.home_fill_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; color: #EF6C00; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )
        else:
            self.home_fill_chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; padding: {ui_px(2)}px {ui_px(8)}px;"
            )
        tip = (
            f"Recent fills with slip meta: {n}. Avg slippage {avg_t} (positive = adverse). "
            + (note or "No active fill-quality size/offset adjustment.")
        )
        self.home_fill_chip.setToolTip(tip)

    def _refresh_locked_capital_chip(self):
        """Home OTC/dust capital checklist — not deployable for rotates/sizing."""
        chip = getattr(self, "home_locked_chip", None)
        nudge = getattr(self, "home_locked_nudge", None)
        if chip is None:
            return
        tc = theme_colors(self.dark_mode)
        holdings: list = []
        assets = getattr(self, "_last_assets_snapshot", None) or []
        if assets:
            holdings = list(assets)
        else:
            for name in BROKER_NAMES:
                for h in (getattr(self, "_heat_holdings_by_broker", {}) or {}).get(name) or []:
                    row = dict(h) if isinstance(h, dict) else {}
                    row.setdefault("broker", name)
                    holdings.append(row)
        # Enrich with live dust/untradeable checks when broker connected
        enriched = []
        for h in holdings:
            if not isinstance(h, dict):
                continue
            row = dict(h)
            bname = str(row.get("broker") or "")
            broker = self.brokers.get(bname)
            ticker = row.get("ticker") or ""
            shares = row.get("shares") or row.get("qty") or 0
            px = row.get("price") or row.get("live_price") or 0
            asset_type = row.get("asset_type") or row.get("type") or ""
            if broker and not row.get("locked_reason"):
                try:
                    if float(px or 0) <= 0:
                        px = float(broker.get_live_price(ticker) or 0)
                        row["price"] = px
                except Exception:
                    pass
                try:
                    if bname == "Robinhood" and hasattr(broker, "_rh_equity_sellable"):
                        ok_inst, _, why_inst = broker._rh_equity_sellable(ticker)
                        if not ok_inst and why_inst:
                            row["locked_reason"] = str(why_inst)
                except Exception:
                    pass
                try:
                    is_dust, dust_reason = broker.position_is_dust(
                        ticker, shares, px, asset_type=asset_type,
                    )
                    if is_dust and dust_reason:
                        row["locked_reason"] = dust_reason
                except Exception:
                    pass
            enriched.append(row)
        try:
            summary = _auto_cycle.locked_capital_summary(enriched)
        except Exception:
            summary = {"count": 0, "total_value": 0.0, "rows": []}
        self._last_locked_summary = summary
        by_broker: dict[str, dict] = {}
        for row in summary.get("rows") or []:
            if not isinstance(row, dict):
                continue
            b = str(row.get("broker") or "")
            if not b:
                continue
            try:
                row_val = max(0.0, float(row.get("value") or 0.0))
            except (TypeError, ValueError):
                row_val = 0.0
            prev = by_broker.get(b) or {"value": 0.0, "count": 0}
            by_broker[b] = {
                "value": float(prev.get("value") or 0.0) + row_val,
                "count": int(prev.get("count") or 0) + 1,
            }
        self._last_locked_by_broker = by_broker
        n = int(summary.get("count") or 0)
        total = float(summary.get("total_value") or 0.0)
        rows = summary.get("rows") or []
        if n > 0:
            chip.setText(f"Locked: {n} (~{format_money(total)})")
            chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 700; color: #EF6C00; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )
            parts = [
                f"{r.get('broker', '?')}:{r.get('ticker', '?')} ({r.get('reason', '?')})"
                for r in rows[:6]
            ]
            sample = ", ".join(parts)
            more = f" (+{n - 6} more)" if n > 6 else ""
            if nudge is not None:
                nudge.setText(
                    f"⚠ ~{format_money(total)} locked in untradeable/dust "
                    f"({sample}{more}) — not deployable BP. Sell in broker app if possible."
                )
                nudge.setVisible(True)
        else:
            chip.setText("Locked: none")
            chip.setStyleSheet(
                f"font-size: {ui_px(11)}px; font-weight: 600; color: {tc['muted']}; "
                f"padding: {ui_px(2)}px {ui_px(8)}px;"
            )
            if nudge is not None:
                nudge.setVisible(False)

    def _refresh_basis_chips(self):
        """Home RH/CB chips + nudge banner for unknown cost basis (regrade P0 honesty)."""
        tc = theme_colors(self.dark_mode)
        assets = getattr(self, "_last_portfolio_assets", None) or []
        unknown_rows: list[tuple[str, str]] = []

        def _count_from_table(broker_name: str) -> int:
            n = 0
            pt = getattr(self, "portfolio_table", None)
            if pt is None:
                return 0
            for row in range(pt.rowCount()):
                b = pt.item(row, 0)
                c = pt.item(row, 3)
                t = pt.item(row, 1)
                if b and b.text() == broker_name and c and (
                    c.text() == "cost ?" or c.text() in ("$0.00", "0", "")
                ):
                    n += 1
                    if t:
                        unknown_rows.append((broker_name, t.text()))
            return n

        def _paint_chip(chip, broker_name: str):
            if chip is None:
                return
            unknown = 0
            try:
                if assets:
                    unknown = _auto_cycle.count_unknown_cost_holdings(
                        assets, broker_name=broker_name,
                    )
                    for a in assets:
                        if not isinstance(a, dict):
                            continue
                        if str(a.get("broker") or "") != broker_name:
                            continue
                        try:
                            cost = float(a.get("cost") or 0.0)
                        except (TypeError, ValueError):
                            cost = 0.0
                        if cost <= 0:
                            unknown_rows.append((broker_name, str(a.get("ticker") or "?")))
                else:
                    unknown = _count_from_table(broker_name)
            except Exception:
                unknown = 0
            if unknown > 0:
                chip.setText(f"Basis: {unknown} unknown")
                chip.setStyleSheet(
                    f"font-size: {ui_px(11)}px; font-weight: 700; color: #F9A825; "
                    f"padding: {ui_px(2)}px {ui_px(8)}px;"
                )
            else:
                chip.setText("Basis: tracked")
                chip.setStyleSheet(
                    f"font-size: {ui_px(11)}px; font-weight: 700; color: {tc['muted']}; "
                    f"padding: {ui_px(2)}px {ui_px(8)}px;"
                )

        _paint_chip(getattr(self, "home_rh_basis_chip", None), "Robinhood")
        _paint_chip(getattr(self, "home_cb_basis_chip", None), "Coinbase")

        nudge = getattr(self, "home_basis_nudge", None)
        if nudge is None:
            return
        uniq: list[str] = []
        seen = set()
        for b, t in unknown_rows:
            key = f"{b}:{t}"
            if key in seen:
                continue
            seen.add(key)
            uniq.append(f"{b}:{t}")
        if uniq:
            sample = ", ".join(uniq[:5])
            more = f" (+{len(uniq) - 5} more)" if len(uniq) > 5 else ""
            nudge.setText(
                f"⚠ {len(uniq)} holding(s) missing avg cost ({sample}{more}) — "
                f"TTP/scale-in gated. Settings → Cost basis paste "
                f"(e.g. Robinhood:SHIB=0.000012)."
            )
            nudge.setVisible(True)
        else:
            nudge.setVisible(False)

    def _refresh_cb_basis_chip(self):
        """Backward-compatible alias."""
        self._refresh_basis_chips()

    def _clear_layout_widgets(self, layout):
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _render_cluster_heat(self, clusters):
        """Paint Cluster Heat meters from scoring.cluster_heat_snapshot rows."""
        from scoring import MAX_CLUSTER_POSITIONS
        host = getattr(self, "home_cluster_host", None)
        rows_lay = getattr(self, "home_cluster_rows_lay", None)
        empty = getattr(self, "home_cluster_empty_lbl", None)
        if host is None or rows_lay is None:
            return
        self._clear_layout_widgets(rows_lay)
        active = [r for r in (clusters or []) if int(r.get("count") or 0) > 0 or r.get("full")]
        if empty is not None:
            if not active:
                empty.setText(
                    f"No holdings in tracked clusters… (cap {MAX_CLUSTER_POSITIONS}/theme)."
                )
                empty.setVisible(True)
            else:
                empty.setVisible(False)
        host.setVisible(bool(active))
        tc = theme_colors(self.dark_mode)
        for row in active:
            count = int(row.get("count") or 0)
            mx = max(1, int(row.get("max") or MAX_CLUSTER_POSITIONS))
            full = bool(row.get("full")) or count >= mx
            name = str(row.get("name") or "?")
            held = ", ".join(row.get("held") or []) or "—"

            wrap = QWidget()
            v = QVBoxLayout(wrap)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(ui_px(2))

            top = QHBoxLayout()
            top.setSpacing(ui_px(8))
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(
                f"font-size: {ui_px(12)}px; font-weight: 700; color: {tc['text']};"
            )
            top.addWidget(name_lbl)
            count_lbl = QLabel(f"{count}/{mx}")
            count_lbl.setStyleSheet(
                f"font-size: {ui_px(12)}px; font-weight: 600; color: {tc['muted']};"
            )
            top.addWidget(count_lbl)
            if full:
                badge = QLabel("FULL")
                badge.setStyleSheet(
                    f"font-size: {ui_px(10)}px; font-weight: 700; color: "
                    f"{'#FF8A80' if self.dark_mode else '#C62828'}; "
                    f"padding: 0 {ui_px(4)}px;"
                )
                top.addWidget(badge)
            else:
                room = QLabel("room left")
                room.setStyleSheet(
                    f"font-size: {ui_px(10)}px; font-weight: 600; color: "
                    f"{tc['accent']}; padding: 0 {ui_px(4)}px;"
                )
                top.addWidget(room)
            top.addStretch(1)
            v.addLayout(top)

            bar = QProgressBar()
            bar.setRange(0, mx)
            bar.setValue(min(count, mx))
            bar.setTextVisible(False)
            bar.setFixedHeight(ui_px(14))
            bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            bar.setStyleSheet(cluster_heat_bar_style(self.dark_mode, full=full))
            v.addWidget(bar)

            tickers = QLabel(held)
            tickers.setWordWrap(True)
            tickers.setTextInteractionFlags(Qt.TextSelectableByMouse)
            tickers.setStyleSheet(
                f"font-size: {ui_px(11)}px; color: {tc['muted']};"
            )
            v.addWidget(tickers)
            rows_lay.addWidget(wrap)

    def _broker_arm_capability_label(self, broker_name):
        b = self.brokers[broker_name]
        caps = []
        if getattr(b, "supports_equities", False):
            caps.append("equities")
        if getattr(b, "supports_crypto", False):
            caps.append("crypto")
        cap_txt = " + ".join(caps) or "n/a"
        connected = bool(getattr(b, "is_connected", False)) or self.paper_mode
        if self.paper_mode:
            status = "Paper"
        elif connected:
            status = "Connected"
        else:
            status = "Disconnected"
        extra = ""
        if broker_name == "E*TRADE" and connected and not self.paper_mode:
            env = str(
                getattr(b, "environment", None)
                or self.settings.get("etrade_environment", "sandbox")
            ).lower()
            live_ok = bool(
                getattr(b, "live_trading_enabled", False)
                or self.settings.get("etrade_live_trading", False)
            )
            bp = float(
                (getattr(self, "_last_balance_totals", {}) or {})
                .get("E*TRADE", {})
                .get("bp", 0.0)
                or 0.0
            )
            extra = f" · {env}"
            extra += " · orders ON" if live_ok else " · orders OFF"
            if bp > 0:
                extra += f" · BP ${bp:,.2f}"
            else:
                extra += " · BP $0"
        return f"{broker_name} ({cap_txt}) — {status}{extra}"

    def _broker_is_arm_eligible(self, broker_name, *, warn=False):
        """Return True if this broker can be armed right now."""
        if getattr(self, "_broker_manual_auth_needed", {}).get(broker_name):
            if warn:
                QMessageBox.warning(
                    self, "Reauthorization Needed",
                    f"Skipping {broker_name}: reauthorize / reconnect in Settings first."
                )
            return False
        if not self.brokers[broker_name].is_connected and not self.paper_mode:
            if warn:
                QMessageBox.warning(
                    self, "Broker Disconnected",
                    f"Skipping {broker_name}: please connect it in Settings first (or enable Paper Mode)."
                )
            return False
        if broker_name == "Coinbase" and not self.paper_mode:
            if not bool(self.settings.get("coinbase_live_trading", True)):
                if warn:
                    QMessageBox.warning(
                        self, "Coinbase Live Trading Disabled",
                        "Coinbase live order placement is off (kill switch). "
                        "Enable Live Trading in the Coinbase login dialog, or use Paper Mode."
                    )
                return False
        if broker_name == "E*TRADE" and not self.paper_mode:
            et = self.brokers["E*TRADE"]
            if getattr(et, "environment", "sandbox") == "live" and not bool(
                self.settings.get("etrade_live_trading", False)
            ):
                if warn:
                    QMessageBox.warning(
                        self, "E*TRADE Live Trading Disabled",
                        "E*TRADE live order placement is off (sandbox-first). "
                        "Connect in Sandbox, or enable Live Trading in the E*TRADE login dialog after validation."
                    )
                return False
            exp = getattr(et, "token_expires_at", None) or self.settings.get("etrade_token_expires_at") or 0
            try:
                exp = float(exp)
            except Exception:
                exp = 0.0
            if warn and exp and exp - time.time() < 3600:
                QMessageBox.information(
                    self, "E*TRADE Reauth Soon",
                    "E*TRADE access tokens expire at midnight ET. "
                    "You may need to reauthorize before the session ends."
                )
        return True

    def _seed_session_start_for_arm(self, broker_name):
        if self.session_starts[broker_name] is not None:
            return
        if self.paper_mode:
            self.session_starts[broker_name] = 10000.00
            return
        cached = float(
            (self._last_balance_totals.get(broker_name) or {}).get("p_val", 0.0) or 0.0
        )
        if cached > 0:
            self.session_starts[broker_name] = cached
            self._persist_session_baselines()

    def _arm_broker_engines(self, broker_names, *, warn=True):
        """Enable auto-trade for eligible brokers. Returns list successfully armed."""
        armed = []
        for broker_name in broker_names:
            if not self._broker_is_arm_eligible(broker_name, warn=warn):
                continue
            self._seed_session_start_for_arm(broker_name)
            self.auto_trade_enabled[broker_name] = True
            self._panic_halted = False
            # Force an immediate first pulse for this broker (don't wait a full interval)
            self.last_crypto_time[broker_name] = 0
            self.last_port_time[broker_name] = 0
            self.last_penny_time[broker_name] = 0
            self.last_core_time[broker_name] = 0
            armed.append(broker_name)
        return armed

    def _disarm_all_engines(self, was=None, *, notify_discord=True):
        was = list(was) if was is not None else [b for b, on in self.auto_trade_enabled.items() if on]
        for broker_name in list(self.auto_trade_enabled.keys()):
            self.auto_trade_enabled[broker_name] = False
        self.task_queue.clear()
        self._cycle_broker = None
        self.is_processing_queue = False
        self._queue_started_at = None
        self._stall_alerted = False
        stopped = ", ".join(was) if was else "none"
        mode = "PAPER" if self.paper_mode else "LIVE"
        self.log_event(f"Auto-Trader DISARMED ({mode}) for all brokers — stopped: {stopped}.")
        for broker_name in was:
            self.log_event(f"[{broker_name}] Auto-Trader disabled.")
        self._update_autotrade_ui()
        if notify_discord:
            self.send_discord_alert(
                f"🛑 Auto-Trader **DISARMED** ({mode}) — stopped: {stopped}."
            )

    def _log_armed_brokers(self, armed):
        totals = getattr(self, "_last_balance_totals", {}) or {}
        mode = "PAPER" if self.paper_mode else "LIVE"
        bp_lines = []
        self.log_event(f"Multi-Engine Auto-Trader ENABLED for {', '.join(armed)}")
        for broker_name in armed:
            if self.paper_mode:
                bp = float(self.sandbox_cash.get(broker_name, 10000.0) or 0.0)
            else:
                bp = float((totals.get(broker_name) or {}).get("bp", 0.0) or 0.0)
            bp_lines.append(f"**{broker_name}** cash {format_currency(bp)}")
            self.log_event(
                f"[{broker_name}] Auto-trade armed | Buying Power: {format_currency(bp)} | Paper={self.paper_mode}"
            )
            if (
                broker_name == "Coinbase"
                and bp < float(self.settings.get("min_trade_dollars", 5.0))
                and not self.paper_mode
            ):
                self.log_event(
                    f"[Coinbase] Note: buying power is {format_currency(bp)} — "
                    f"auto-BUY cannot place orders until you add USD/USDC (sells can still run)."
                )
            if broker_name == "E*TRADE" and self._buy_engines_idle_reason("E*TRADE"):
                why = self._buy_engines_idle_reason("E*TRADE")
                self.log_event(
                    f"[E*TRADE] {why} (CORE/BREAKOUT skipped); "
                    "PORTFOLIO sells still run."
                )
            if broker_name == "E*TRADE":
                et = self.brokers.get("E*TRADE")
                env = str(
                    getattr(et, "environment", None)
                    or self.settings.get("etrade_environment", "sandbox")
                ).lower()
                live_ok = bool(
                    getattr(et, "live_trading_enabled", False)
                    or self.settings.get("etrade_live_trading", False)
                )
                try:
                    note = _decision_log.etrade_path_honesty_note(
                        environment=env,
                        live_trading=live_ok,
                        buying_power=bp,
                        paper_mode=bool(self.paper_mode),
                        min_trade_dollars=float(self.settings.get("min_trade_dollars", 5.0) or 5.0),
                    )
                    self.log_event(f"[E*TRADE] {note}")
                except Exception:
                    self.log_event(
                        f"[E*TRADE] env={env} live_orders={'ON' if live_ok else 'OFF'} "
                        f"BP={format_currency(bp)} — equity buys only in REGULAR/EXTENDED ET."
                    )
        self.send_discord_alert(
            f"⚔️ Auto-Trader **ARMED** ({mode}) on {', '.join(armed)}.\n"
            + "\n".join(bp_lines)
        )

    def toggle_auto_trade(self):
        """Open per-broker arm picker (check to arm / uncheck to disarm, including re-arm)."""
        currently_on = [b for b, on in self.auto_trade_enabled.items() if on]
        # Manage picker directly so a single disarmed broker can be re-armed without
        # "Turn off all" first. Uncheck everyone + OK still disarms all.
        self._open_broker_arm_picker(currently_on)

    def _open_broker_arm_picker(self, currently_on=None):
        """Checkbox picker to arm, update, or disarm brokers independently."""
        currently_on = list(currently_on) if currently_on is not None else [
            b for b, on in self.auto_trade_enabled.items() if on
        ]
        managing = bool(currently_on)

        rows = []
        for name in BROKER_NAMES:
            eligible = self._broker_is_arm_eligible(name, warn=False)
            # Already-armed brokers stay selectable so they can be unchecked to disarm
            enabled = eligible or bool(self.auto_trade_enabled.get(name))
            tip = None
            if not enabled:
                tip = "Connect this broker in Settings first (or enable Paper Mode)."
                if name == "E*TRADE" and self.brokers[name].is_connected and not self.paper_mode:
                    tip = (
                        "E*TRADE live order placement is off. "
                        "Use Sandbox or enable Live Trading in the E*TRADE login dialog."
                    )
            rows.append({
                "name": name,
                "label": self._broker_arm_capability_label(name),
                "enabled": enabled,
                "checked": bool(self.auto_trade_enabled.get(name)) if managing else eligible,
                "disabled_tip": tip,
            })

        dlg = BrokerArmDialog(self, rows, dark_mode=self.dark_mode, managing=managing)
        if dlg.exec_() != QDialog.Accepted:
            return

        selected = dlg.selected_brokers()
        previously = set(currently_on)
        wanted = set(selected)

        to_disarm = [b for b in BROKER_NAMES if b in previously and b not in wanted]
        to_arm = [b for b in BROKER_NAMES if b in wanted and b not in previously]

        if not wanted:
            if previously:
                self._disarm_all_engines(was=currently_on)
            else:
                self.log_event("Auto-Trader arm cancelled — no brokers selected.")
            return

        for broker_name in to_disarm:
            self._disarm_broker(
                broker_name,
                notify_discord=True,
                clear_arm_intent=(broker_name == "E*TRADE"),
            )

        newly_armed = self._arm_broker_engines(to_arm, warn=True) if to_arm else []
        if "E*TRADE" in to_arm:
            self._set_etrade_arm_intent(False)

        # Keep previously armed brokers that remain selected
        still_armed = [b for b in BROKER_NAMES if self.auto_trade_enabled.get(b)]
        if not still_armed:
            self.log_event("Auto-Trader arm failed — no eligible brokers.")
            self._update_autotrade_ui()
            return

        self._update_autotrade_ui()
        if newly_armed:
            self._set_engine_banner("🤖 ⚡ Auto-Trader Armed — spinning up…")
            QTimer.singleShot(0, self.director_tick)
            self._log_armed_brokers(newly_armed)
            self.refresh_account_balances()
        elif to_disarm:
            remaining = ", ".join(still_armed)
            self.log_event(f"Multi-Engine Auto-Trader still ENABLED for {remaining}")
            mode = "PAPER" if self.paper_mode else "LIVE"
            self.send_discord_alert(
                f"⚔️ Auto-Trader **UPDATED** ({mode}) — still armed: {remaining}."
            )
        else:
            self.log_event(
                f"Multi-Engine Auto-Trader selection unchanged: {', '.join(still_armed)}"
            )

    def _set_trading_context(self, broker_name):
        """Locks trading to a broker for one auto cycle without yanking the user off All view."""
        # When user is on All, keep their active_broker as Robinhood for stock scanners;
        # cycle execution still uses _cycle_broker exclusively.
        if self.view_mode != "All":
            self.active_broker_name = broker_name
            if hasattr(self, 'broker_dropdown'):
                self.broker_dropdown.blockSignals(True)
                self.broker_dropdown.setCurrentText(broker_name)
                self.broker_dropdown.blockSignals(False)
                self.view_mode = broker_name
                self._apply_view_mode_tabs()
        # Never hide/show tabs mid-cycle based on auto context — view_mode owns that.

    def director_tick(self):
        now = time.time()
        self.update_market_status()

        # Stall watchdog: unlock queue if a cycle hangs too long
        if self.is_processing_queue and self._queue_started_at:
            stalled_for = now - self._queue_started_at
            if stalled_for >= 180 and not self._stall_alerted:
                self._stall_alerted = True
                msg = f"⚠️ Cycle stall detected ({int(stalled_for)}s). Forcing queue unlock."
                self.log_event(msg)
                self.send_discord_alert(msg)
                self._cycle_broker = None
                self.is_processing_queue = False
                self._queue_started_at = None
                self._stall_alerted = False

        self._maybe_send_heartbeat(now)

        # Equity RTH boundary wake-ups before interval scheduling (sets last_* so no double-queue)
        self._maybe_session_boundary_wakeup(now)
        self._maybe_etrade_midnight_handling(now)

        for broker_name, enabled in self.auto_trade_enabled.items():
            if not enabled:
                continue
            # Auth dead — do not enqueue more work (disarm should already have run)
            if getattr(self, "_broker_manual_auth_needed", {}).get(broker_name):
                continue
            if not self.brokers[broker_name].is_connected and not self.paper_mode:
                self._try_reconnect_broker(broker_name)
                # Skip cycles while disconnected or reconnect still running
                if (
                    not self.brokers[broker_name].is_connected
                    or self._reconnect_in_flight.get(broker_name)
                ):
                    continue

            if now - self.last_crypto_time[broker_name] >= self.settings.get("interval_crypto", 45):
                idle_why = self._buy_engines_idle_reason(broker_name)
                if idle_why:
                    self._throttled_log(
                        f"{broker_name}:buy_engines_idle",
                        f"[{broker_name}] {idle_why}",
                        cooldown_sec=780,
                    )
                elif self._broker_supports(broker_name, "supports_crypto"):
                    task = (broker_name, "CRYPTO")
                    if task not in self.task_queue: self.task_queue.append(task)
                self.last_crypto_time[broker_name] = now

            # Portfolio / sell checks: all armed brokers, 24/7
            if now - self.last_port_time[broker_name] >= self.settings.get("interval_portfolio", 45):
                task = (broker_name, "PORTFOLIO")
                if task not in self.task_queue: self.task_queue.append(task)
                self.last_port_time[broker_name] = now

            # Equities: capability-driven (RH + E*TRADE; not Coinbase)
            if self._broker_supports(broker_name, "supports_equities"):
                if not self.is_equity_session_active():
                    sess = self.get_equity_session_info().get("label", "?")
                    self._throttled_log(
                        f"{broker_name}:equity_session_wait",
                        f"[{broker_name}] Equity buy engines wait for REGULAR/EXTENDED "
                        f"(now {sess}) — PORTFOLIO sells still run 24/7.",
                        cooldown_sec=1800,
                    )
                else:
                    idle_why = self._buy_engines_idle_reason(broker_name)
                    if now - self.last_penny_time[broker_name] >= self.settings.get("interval_penny", 60):
                        if idle_why:
                            self._throttled_log(
                                f"{broker_name}:buy_engines_idle",
                                f"[{broker_name}] {idle_why}",
                                cooldown_sec=780,
                            )
                        else:
                            task = (broker_name, "PENNY")
                            if task not in self.task_queue: self.task_queue.append(task)
                        self.last_penny_time[broker_name] = now

                    if now - self.last_core_time[broker_name] >= self.settings.get("interval_core", 300):
                        if idle_why:
                            self._throttled_log(
                                f"{broker_name}:buy_engines_idle",
                                f"[{broker_name}] {idle_why}",
                                cooldown_sec=780,
                            )
                        else:
                            task = (broker_name, "CORE")
                            if task not in self.task_queue: self.task_queue.append(task)
                        self.last_core_time[broker_name] = now

        self.process_queue()

        # Quiet auto-repair of missing protective stops while any engine is armed
        if any(self.auto_trade_enabled.values()):
            self._maybe_repair_protective_stops(force=False)

        # Quiet idle balance poll — keeps top-bar equity/P&L fresh even when auto-trader is off.
        # Skip until startup connect finishes so we don't Discord/$0-paint before brokers attach.
        if getattr(self, "_startup_connect_finished", False):
            bal_every = int(self.settings.get("interval_balance_refresh", 60) or 60)
            bal_every = max(30, bal_every)
            if now - getattr(self, "_last_idle_balance_refresh", 0.0) >= bal_every:
                self._last_idle_balance_refresh = now
                self.refresh_account_balances(quiet=True)
            self._maybe_nudge_etrade_arm(now)

        # Throttle monitor publish (~every 3s) so the phone page stays fresh
        last_pub = getattr(self, "_monitor_last_publish", 0.0)
        if now - last_pub >= 3.0:
            self._monitor_last_publish = now
            self.publish_monitor_status()

    def _maybe_nudge_etrade_arm(self, now=None):
        """
        Once per stretch: if live ET is ready for real buys but not armed, say so.
        Live checkbox alone does not place orders — Auto-Trader must include E*TRADE.
        """
        if self.paper_mode or self.auto_trade_enabled.get("E*TRADE"):
            return
        et = self.brokers.get("E*TRADE")
        if not et or not getattr(et, "is_connected", False):
            return
        env = str(
            getattr(et, "environment", None)
            or self.settings.get("etrade_environment", "sandbox")
        ).lower()
        live_ok = bool(
            getattr(et, "live_trading_enabled", False)
            or self.settings.get("etrade_live_trading", False)
        )
        if env != "live" or not live_ok:
            return
        bp = float(
            (getattr(self, "_last_balance_totals", {}) or {})
            .get("E*TRADE", {})
            .get("bp", 0.0)
            or 0.0
        )
        min_d = float(self.settings.get("min_trade_dollars", 5.0) or 5.0)
        if bp < max(0.01, min_d):
            self._throttled_log(
                "E*TRADE:live_ready_zero_bp",
                f"[E*TRADE] Live · orders ON but BP {format_currency(bp)} — "
                "fund the account / pick the right account before arming buys.",
                cooldown_sec=1800,
            )
            return
        self._throttled_log(
            "E*TRADE:live_ready_not_armed",
            "[E*TRADE] Live · orders ON · funded — but Auto-Trader is not armed "
            "for E*TRADE. Open Auto-Trader and check E*TRADE to run CORE/BREAKOUT.",
            cooldown_sec=1800,
        )
    def _try_reconnect_broker(self, broker_name):
        """Kick off silent re-login on a worker thread (never block director_tick)."""
        now = time.time()
        if self._reconnect_in_flight.get(broker_name):
            return
        # Needs browser OAuth / missing creds — silent retry cannot succeed
        if getattr(self, "_broker_manual_auth_needed", {}).get(broker_name):
            return
        if now - self._reconnect_cooldown.get(broker_name, 0) < 90:
            return
        self._reconnect_cooldown[broker_name] = now
        self._reconnect_in_flight[broker_name] = True
        self.log_event(f"[{broker_name}] Session dropped — attempting reconnect...")

        email = self.settings.get("rh_email", "")
        password = self.settings.get("rh_password", "")
        cb_key = self.settings.get("cb_api_key", "")
        cb_secret = self.settings.get("cb_api_secret", "")

        def _bg():
            try:
                if broker_name == "Robinhood":
                    # Prefer saved session (no MFA). Never password/2FA on a worker thread.
                    ok, detail = self.brokers["Robinhood"].login({})
                    if ok:
                        return True, "session restored"
                    return False, detail or "saved session expired — Connect from Settings"
                if broker_name == "Coinbase":
                    if cb_key and cb_secret:
                        ok, detail = self.brokers["Coinbase"].login({
                            "api_key": cb_key,
                            "api_secret": cb_secret,
                            "live_trading_enabled": bool(
                                self.settings.get("coinbase_live_trading", True)
                            ),
                        })
                        return bool(ok), detail
                    return False, "missing saved API keys"
                if broker_name == "E*TRADE":
                    ok, detail = self.brokers["E*TRADE"].login({
                        "environment": self.settings.get("etrade_environment", "sandbox"),
                        "consumer_key": self.settings.get("etrade_consumer_key", ""),
                        "account_id_key": self.settings.get("etrade_account_id_key", ""),
                        "token_expires_at": self.settings.get("etrade_token_expires_at", 0),
                        "live_trading_enabled": bool(self.settings.get("etrade_live_trading", False)),
                    })
                    return bool(ok), detail
                return False, "unknown broker"
            except Exception as e:
                return False, str(e)

        def _done(result):
            self._reconnect_in_flight[broker_name] = False
            ok, detail = (False, "no result")
            if isinstance(result, (list, tuple)) and len(result) >= 2:
                ok, detail = bool(result[0]), result[1]
            if ok:
                self._reconnect_fail_streak[broker_name] = 0
                self._broker_manual_auth_needed[broker_name] = False
                if hasattr(self, "_reauth_nudge_sent"):
                    self._reauth_nudge_sent[broker_name] = False
                    if broker_name == "E*TRADE":
                        self._reauth_nudge_sent["E*TRADE_SOON"] = False
                self.log_event(f"[{broker_name}] Reconnected successfully.")
                self.send_discord_alert(f"✅ [{broker_name}] Session restored after drop.")
                self.refresh_account_balances()
                self._update_autotrade_ui()
                if hasattr(self, "_update_reauth_banner"):
                    self._update_reauth_banner()
                if broker_name == "E*TRADE":
                    et = self.brokers.get("E*TRADE")
                    if et and getattr(et, "token_expires_at", None):
                        self.settings["etrade_token_expires_at"] = float(et.token_expires_at)
                        try:
                            save_settings(self.settings)
                        except Exception:
                            pass
                    self._maybe_restore_etrade_arm(source="reconnect")
            else:
                streak = self._reconnect_fail_streak.get(broker_name, 0) + 1
                self._reconnect_fail_streak[broker_name] = streak
                if _is_manual_auth_failure(detail):
                    self._handle_broker_auth_failure(
                        broker_name, detail, source="reconnect"
                    )
                else:
                    self.log_event(f"[{broker_name}] Reconnect failed ({streak}x): {detail}")
                    if streak >= 2:
                        self.send_discord_alert(
                            f"🚨 [{broker_name}] Reconnect failed {streak}x — auto cycles paused until session restored. ({detail})"
                        )
                    self._update_autotrade_ui()

        def _fail(err):
            self._reconnect_in_flight[broker_name] = False
            streak = self._reconnect_fail_streak.get(broker_name, 0) + 1
            self._reconnect_fail_streak[broker_name] = streak
            self.log_event(f"[{broker_name}] Reconnect error ({streak}x): {err}")
            self._update_autotrade_ui()

        task = BackgroundTask(_bg)
        task.result_ready.connect(_done)
        task.error_occurred.connect(_fail)
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def process_queue(self):
        if self.is_processing_queue: return
        if not self.task_queue:
            if any(self.auto_trade_enabled.values()):
                self._set_engine_banner("🤖 💤 Engines resting — awaiting next pulse")
            self._cycle_broker = None
            return

        self.is_processing_queue = True
        self._queue_started_at = time.time()
        self._stall_alerted = False
        broker_name, task = self.task_queue.pop(0)
        if not self.auto_trade_enabled.get(broker_name):
            self.log_event(f"[AUTO] Skipping {task} on {broker_name} (disarmed)")
            self.cycle_finished()
            return
        if getattr(self, "_broker_manual_auth_needed", {}).get(broker_name):
            self.log_event(f"[AUTO] Skipping {task} on {broker_name} (reauth needed)")
            self.cycle_finished()
            return
        self._cycle_broker = broker_name
        self._cycle_task = task
        self._set_trading_context(broker_name)
        self.log_event(f"[AUTO] Starting {task} cycle on {broker_name}")

        # Sandbox/no-BP: skip buy engines already in queue; PORTFOLIO still runs
        if task in ("CRYPTO", "PENNY", "CORE"):
            idle_why = self._buy_engines_idle_reason(broker_name)
            if idle_why:
                logged = self._throttled_log(
                    f"{broker_name}:buy_engines_idle",
                    f"[{broker_name}] {idle_why} — skipping {task}",
                    cooldown_sec=780,
                )
                if logged:
                    self._journal_idle_skip(broker_name, idle_why, engine=task)
                self.cycle_finished()
                return

        if task == "CRYPTO":
            if not self._broker_supports(broker_name, "supports_crypto"):
                self.log_event(f"[AUTO] Skipping CRYPTO on {broker_name} (broker has no crypto)")
                self.cycle_finished()
                return
            self.run_crypto_cycle()
        elif task == "PENNY":
            if not self._broker_supports(broker_name, "supports_equities"):
                self.log_event(f"[AUTO] Skipping PENNY on {broker_name} (no equities)")
                self.cycle_finished()
                return
            self.run_penny_cycle()
        elif task == "CORE":
            if not self._broker_supports(broker_name, "supports_equities"):
                self.log_event(f"[AUTO] Skipping CORE on {broker_name} (no equities)")
                self.cycle_finished()
                return
            self.run_core_cycle()
        elif task == "PORTFOLIO":
            self.run_portfolio_cycle()
        else:
            self.log_event(f"[AUTO] Unknown task {task} — skipping")
            self.cycle_finished()

    def cycle_finished(self):
        finished_broker = self._cycle_broker
        self._cycle_broker = None
        self.is_processing_queue = False
        self._queue_started_at = None
        self._stall_alerted = False
        if finished_broker:
            self.log_event(f"[AUTO] Cycle finished for {finished_broker}")
        self.process_queue()

    # ---------------------------------------------------------
    #  TABLES & EXECUTION
    # ---------------------------------------------------------
    def _holdings_fingerprint(self, assets):
        """Stable signature of positions — ignores live price so quotes don't thrash the table."""
        return _auto_cycle.holdings_fingerprint(assets)

    def _refresh_holdings_count_cache(self, assets=None):
        """Update cached holdings counts without hitting broker APIs on the UI thread."""
        cache = getattr(self, "_holdings_count_cache", None)
        if cache is None:
            self._holdings_count_cache = _blank_broker_map(0)
            cache = self._holdings_count_cache
        if assets is None:
            return cache
        counts = _blank_broker_map(0)
        for a in assets:
            name = a.get("broker") or ""
            if name in counts:
                counts[name] += 1
        # When view is a single broker, only refresh that broker's count
        if self.view_mode in BROKER_NAMES:
            cache[self.view_mode] = counts.get(self.view_mode, 0)
        else:
            cache.update(counts)
        return cache

    def _load_holdings_for_view(self):
        """Returns holdings for the current view_mode (All = both brokers tagged), with live prices."""
        if self.view_mode == "All":
            combined = []
            for name in BROKER_NAMES:
                for a in self.get_broker_holdings(name):
                    if not isinstance(a, dict) or not a.get("ticker"):
                        continue
                    row = dict(a)
                    row["broker"] = name
                    combined.append(row)
            assets = combined
        else:
            name = self.view_mode if self.view_mode in self.brokers else self.active_broker_name
            assets = [
                a for a in (self.get_broker_holdings(name) or [])
                if isinstance(a, dict) and a.get("ticker")
            ]
            for a in assets:
                a["broker"] = name

        for a in assets:
            broker_name = a.get("broker") or self.active_broker_name
            broker = self.brokers.get(broker_name)
            try:
                a["live_price"] = float(broker.get_live_price(a.get("ticker")) if broker else 0.0) or 0.0
            except Exception:
                a["live_price"] = 0.0

        # Keep monitor counts fresh without UI-thread API calls
        if self.view_mode == "All":
            self._holdings_count_cache = {
                "Robinhood": sum(1 for a in assets if a.get("broker") == "Robinhood"),
                "Coinbase": sum(1 for a in assets if a.get("broker") == "Coinbase"),
                "E*TRADE": sum(1 for a in assets if a.get("broker") == "E*TRADE"),
            }
        elif self.view_mode in BROKER_NAMES:
            self._holdings_count_cache[self.view_mode] = len(assets)
        return assets

    def manual_portfolio_reload(self, and_score=False, force=False):
        """
        Reload portfolio UI. By default stays static unless holdings changed
        (ticker/shares fingerprint). Pass force=True for broker switch / startup / after fills.
        """
        self.set_working_state(True, "Loading portfolio holdings...")
        def _bg():
            return self._load_holdings_for_view()

        def _done(assets):
            assets = assets or []
            # Normalize for heat / protective health (broker_id + value)
            bid_map = {"Robinhood": "ROBINHOOD", "Coinbase": "COINBASE", "E*TRADE": "ETRADE"}
            norm = []
            for a in assets:
                if not isinstance(a, dict):
                    continue
                row = dict(a)
                bname = row.get("broker") or row.get("broker_name") or (
                    self.view_mode if self.view_mode in BROKER_NAMES else ""
                )
                if self.view_mode in BROKER_NAMES and not bname:
                    bname = self.view_mode
                row["broker"] = bname
                row["broker_id"] = bid_map.get(bname, str(bname).upper())
                if not row.get("value"):
                    try:
                        px = float(row.get("price") or row.get("live_price") or 0)
                        qty = float(row.get("shares") or row.get("qty") or 0)
                        row["value"] = abs(px * qty)
                    except (TypeError, ValueError):
                        row["value"] = 0.0
                norm.append(row)
            self._last_assets_snapshot = norm
            self._refresh_holdings_count_cache(norm if self.view_mode == "All" else None)
            if self.view_mode in BROKER_NAMES:
                self._holdings_count_cache[self.view_mode] = len(assets)
            fp = self._holdings_fingerprint(assets)
            changed = force or fp != self._portfolio_fingerprint or self.portfolio_table.rowCount() == 0
            if changed:
                self._portfolio_fingerprint = fp
                self._on_portfolio_loaded(assets)
                if and_score:
                    self.manual_score_portfolio()
                else:
                    self.set_working_state(False)
            else:
                # Positions unchanged — paint fresh prices without rebuilding rows
                self._paint_portfolio_prices(assets)
                if and_score and self.portfolio_table.rowCount() > 0:
                    self.manual_score_portfolio()
                else:
                    self.set_working_state(False)

        self.run_thread(_bg, _done)

    def _paint_portfolio_prices(self, assets):
        """Update price/value columns from bg-fetched live_price without full table rebuild."""
        by_key = {
            (str(a.get("broker") or ""), str(a.get("ticker") or "").upper()): a
            for a in (assets or [])
        }
        for row in range(self.portfolio_table.rowCount()):
            b_item = self.portfolio_table.item(row, 0)
            t_item = self.portfolio_table.item(row, 1)
            if not b_item or not t_item:
                continue
            a = by_key.get((b_item.text(), t_item.text().upper()))
            if not a:
                continue
            price = float(a.get("live_price") or 0.0)
            try:
                shares = float(self.portfolio_table.item(row, 2).text())
            except Exception:
                shares = float(a.get("shares") or 0.0)
            self.portfolio_table.setItem(row, 4, QTableWidgetItem(format_currency(price)))
            self.portfolio_table.setItem(row, 5, QTableWidgetItem(format_currency(shares * price)))

    def _on_portfolio_loaded(self, assets):
        assets = [a for a in (assets or []) if isinstance(a, dict) and a.get("ticker")]
        self.portfolio_table.setRowCount(len(assets))
        for row, a in enumerate(assets):
            broker_name = a.get('broker') or self.active_broker_name
            t_item = QTableWidgetItem(str(a.get('ticker') or ""))
            t_item.setCheckState(Qt.Checked)
            t_item.setData(Qt.UserRole, a.get('type', ''))
            t_item.setData(Qt.UserRole + 1, broker_name)

            price = float(a.get("live_price") or 0.0)

            self.portfolio_table.setItem(row, 0, QTableWidgetItem(broker_name))
            self.portfolio_table.setItem(row, 1, t_item)
            self.portfolio_table.setItem(row, 2, QTableWidgetItem(format_quantity(a.get('shares') or 0)))
            try:
                _cost = float(a.get("cost") or 0)
            except (TypeError, ValueError):
                _cost = 0.0
            cost_txt = (
                "cost ?" if _cost <= 0
                else format_currency(_cost)
            )
            cost_item = QTableWidgetItem(cost_txt)
            if _cost <= 0:
                cost_item.setToolTip(
                    "Cost basis unknown — TTP/scale-in/ROI gated until avg cost is available "
                    "(broker portfolio entry / journal / tracked / Settings → Cost basis paste)."
                )
            self.portfolio_table.setItem(row, 3, cost_item)
            self.portfolio_table.setItem(row, 4, QTableWidgetItem(format_currency(price)))
            shares = float(a.get("shares") or 0.0)
            self.portfolio_table.setItem(row, 5, QTableWidgetItem(format_currency(shares * price)))
            self.portfolio_table.setItem(row, 6, QTableWidgetItem("Pending..."))
            self.portfolio_table.setItem(row, 7, QTableWidgetItem("Not Traded"))
        self._last_portfolio_assets = list(assets)
        try:
            self._refresh_cb_basis_chip()
        except Exception:
            pass
        self.set_working_state(False)

    def _on_portfolio_loaded_and_scored(self, assets):
        self._on_portfolio_loaded(assets)
        self.manual_score_portfolio()

    def _patch_portfolio_row_action(self, broker_name, ticker, price, action):
        """Update a single row in the visible table without rebuilding it."""
        for row in range(self.portfolio_table.rowCount()):
            b_item = self.portfolio_table.item(row, 0)
            t_item = self.portfolio_table.item(row, 1)
            if not b_item or not t_item:
                continue
            if b_item.text() != broker_name or t_item.text().upper() != str(ticker).upper():
                continue
            if price:
                try:
                    shares = float(self.portfolio_table.item(row, 2).text())
                except Exception:
                    shares = 0.0
                self.portfolio_table.setItem(row, 4, QTableWidgetItem(format_currency(price)))
                self.portfolio_table.setItem(row, 5, QTableWidgetItem(format_currency(shares * price)))
            action_item = QTableWidgetItem(action)
            self.apply_color_formatting(action_item, action)
            self.portfolio_table.setItem(row, 6, action_item)
            return

    def manual_score_portfolio(self):
        items = self._gather_table_data_for_scoring(self.portfolio_table)
        if items:
            self.set_working_state(True, "Scoring portfolio...")
            self.run_thread(self._bg_score_portfolio, self._on_portfolio_scored, items)

    def _on_portfolio_scored(self, results):
        for row, price, action, asset_type, err in results:
            if row >= self.portfolio_table.rowCount(): continue
            if price:
                shares_item = self.portfolio_table.item(row, 2)
                try:
                    shares = float(shares_item.text()) if shares_item else 0.0
                except Exception:
                    shares = 0.0
                self.portfolio_table.setItem(row, 4, QTableWidgetItem(format_currency(price)))
                self.portfolio_table.setItem(row, 5, QTableWidgetItem(format_currency(shares * price)))
            action_item = QTableWidgetItem(action)
            self.apply_color_formatting(action_item, action)
            self.portfolio_table.setItem(row, 6, action_item)
        self.set_working_state(False)

    def calculate_order_sizing(self, current_bp, asset_type="", entry_price=0.0, equity=None,
                               score=None, open_count=None, max_open_positions=None,
                               existing_name_value=0.0, size_frac=1.0, return_detail=False,
                               ticker=None):
        """
        BP-aware concentrated size: deployable_BP / min(remaining capacity, focus_slots),
        floored by allocation_pct_* baseline; conviction may stretch the aim (posture max).
        Hard/soft caps inside risk_sizing_breakdown: risk $, soft equity name cap, book heat.
        min_trade_dollars is a floor/skip only — not the target size.
        existing_name_value / size_frac: gated scale-in — size_frac shrinks aim before
        caps; min floor still applies when remaining name room allows.

        Returns trade dollars, or (trade, detail_dict) when return_detail=True.
        """
        from scoring import (
            risk_sizing_breakdown, get_stop_distance_pct, get_execution_feedback,
            effective_min_dollars, posture_knobs_for_broker,
        )
        is_crypto = "crypto" in str(asset_type).lower()
        if is_crypto:
            alloc_pct = self.settings.get("allocation_pct_crypto", self.settings.get("allocation_pct", 8.0)) / 100.0
        else:
            alloc_pct = self.settings.get("allocation_pct_stock", self.settings.get("allocation_pct", 5.0)) / 100.0
        broker_id = getattr(self.cycle_broker, "broker_id", None) or self.cycle_broker_name.upper()
        knobs = posture_knobs_for_broker(self.cycle_broker_name, self.settings)
        eq = float(equity) if equity is not None else None
        if eq is None:
            try:
                eq, _, _ = self.get_effective_balances(self.cycle_broker_name)
            except Exception:
                try:
                    eq, _ = self.get_broker_balances(self.cycle_broker_name)
                except Exception:
                    eq = float(current_bp or 0.0)
        else:
            locked = self._locked_capital_value(self.cycle_broker_name)
            eq = _auto_cycle.effective_book_equity(eq, locked)
        min_dollars = effective_min_dollars(
            broker_id, eq, is_crypto, self.settings.get("min_trade_dollars", 5.0)
        )
        stop_d = get_stop_distance_pct(
            broker_id, ticker=ticker, asset_type=asset_type, for_sizing=True,
        )
        max_open = max_open_positions
        if max_open is None:
            max_open = int(knobs.get("max_open_positions", 8))
        open_n = open_count
        if open_n is None:
            try:
                holdings = self.get_broker_holdings(self.cycle_broker_name) or []
                open_n = len({
                    (a.get("ticker") or "").upper()
                    for a in holdings
                    if isinstance(a, dict) and a.get("ticker")
                })
            except Exception:
                open_n = 0
        try:
            util = float(knobs.get("target_bp_utilization_pct", 88.0))
        except (TypeError, ValueError):
            util = 88.0
        try:
            focus = int(knobs.get("sizing_focus_slots", 6))
        except (TypeError, ValueError):
            focus = 6
        try:
            name_cap = float(knobs.get("max_single_name_equity_pct", 15.0))
        except (TypeError, ValueError):
            name_cap = 15.0
        try:
            conv_max = float(knobs.get("conviction_alloc_mult_max", 1.50))
        except (TypeError, ValueError):
            conv_max = 1.50
        try:
            risk_pct = float(knobs.get("risk_pct_per_trade", 0.75))
        except (TypeError, ValueError):
            risk_pct = 0.75
        try:
            max_book_risk = float(knobs.get("max_open_risk_pct", 6.0))
        except (TypeError, ValueError):
            max_book_risk = 6.0
        open_risk = 0.0
        try:
            heat = getattr(self, "_last_portfolio_heat", None) or {}
            if isinstance(heat, dict):
                combined = heat.get("combined") if isinstance(heat.get("combined"), dict) else heat
                open_risk = float(combined.get("open_risk_dollars") or 0.0)
        except (TypeError, ValueError, AttributeError):
            open_risk = 0.0
        # Fill-quality size shrink (conservative)
        try:
            fb = get_execution_feedback()
            size_frac = float(size_frac or 1.0) * float(fb.get("size_mult") or 1.0)
        except Exception:
            pass
        # Paper↔live shadow guardrail (Reports-driven; light temporary tighten)
        try:
            sg = getattr(self, "_shadow_guard_active", None) or {}
            if sg.get("tighten") and bool(self.settings.get("shadow_guardrail_enabled", True)):
                size_frac = float(size_frac or 1.0) * float(sg.get("size_mult") or 1.0)
        except Exception:
            pass
        detail = risk_sizing_breakdown(
            eq, current_bp, stop_d, alloc_pct, min_dollars=min_dollars,
            conviction_score=score, open_count=open_n, max_open_positions=max_open,
            target_bp_utilization=util, sizing_focus_slots=focus,
            soft_name_equity_frac=name_cap, conviction_mult_max=conv_max,
            existing_name_value=existing_name_value, size_frac=size_frac,
            risk_pct_per_trade=risk_pct, open_risk_dollars=open_risk,
            max_open_risk_pct=max_book_risk,
        )
        if detail.get("sizing_note"):
            try:
                # Throttle identical fallback notes
                note_key = str(detail.get("sizing_note"))
                last = getattr(self, "_last_sizing_note", None)
                if note_key != last:
                    self._last_sizing_note = note_key
                    self.log_event(f"[Sizing] {note_key}")
            except Exception:
                pass
        trade = float(detail.get("trade") or 0.0)
        if return_detail:
            return trade, detail
        return trade

    def _note_scale_in_skip(self, notes, broker_name, ticker, reason, throttle_sec=780,
                            *, score=None, is_crypto=None, journal=True):
        """
        Append a SCALE-IN skip note, throttling identical broker/ticker/reason spam.
        Re-emits after throttle_sec (~13 min) with a repeat count so the Activity Log stays readable.
        Always journals SCALE_IN_SKIP when journal=True (analytics / posture replay).
        """
        reason = str(reason or "sizing blocked").strip()
        if journal:
            try:
                _decision_log.emit_scale_in_skip(
                    self._log_decision,
                    broker=broker_name,
                    ticker=ticker,
                    reason=reason,
                    score=score,
                    posture=__import__("scoring", fromlist=["posture_for_broker"]).posture_for_broker(
                        broker_name, self.settings
                    ),
                    engine=getattr(self, "_cycle_task", None),
                    is_crypto=is_crypto,
                )
            except Exception:
                try:
                    self._log_decision(
                        broker=broker_name, ticker=ticker, action="SCALE_IN_SKIP",
                        score=score, reason=f"scale_in:{reason}",
                        is_crypto=is_crypto,
                    )
                except Exception:
                    pass
        if not hasattr(self, "_si_skip_throttle"):
            self._si_skip_throttle = {}
        note = _auto_cycle.scale_in_skip_note(
            self._si_skip_throttle, broker_name, ticker, reason,
            now=time.time(), throttle_sec=throttle_sec,
        )
        if note:
            notes.append(note)

    def _journal_idle_skip(self, broker_name, reason, *, engine=None):
        """Decision-journal IDLE_SKIP (throttled with activity idle log key)."""
        try:
            bp = None
            try:
                bp = float(
                    (getattr(self, "_last_balance_totals", {}) or {})
                    .get(broker_name, {})
                    .get("bp", 0.0)
                    or 0.0
                )
            except (TypeError, ValueError):
                bp = None
            _decision_log.emit_idle_skip(
                self._log_decision,
                broker=broker_name,
                reason=reason,
                engine=engine or getattr(self, "_cycle_task", None),
                posture=__import__("scoring", fromlist=["posture_for_broker"]).posture_for_broker(
                    broker_name, self.settings
                ),
                bp=bp,
            )
        except Exception:
            try:
                self._log_decision(
                    broker=broker_name, ticker="", action="IDLE_SKIP",
                    reason=f"idle:{reason}",
                    posture=self.settings.get("risk_posture", "balanced"),
                    engine=engine or getattr(self, "_cycle_task", None),
                )
            except Exception:
                pass

    def _attach_protective_stop(self, broker_name, ticker, asset_type, price, spent):
        """After a successful buy: broker stop if live; virtual stop in paper. Software TTP remains backup."""
        if not bool(self.settings.get("attach_protective_stops", True)):
            return
        from scoring import (
            get_stop_distance_pct, get_trail_pct, get_protective_order,
            set_protective_order, clear_protective_order,
        )
        try:
            qty = (float(spent) / float(price)) if price and spent else 0.0
            if qty <= 0 or price <= 0:
                return
            broker_id = getattr(self.brokers.get(broker_name), "broker_id", None) or str(broker_name).upper()
            existing = get_protective_order(broker_id, ticker)
            if existing and existing.get("order_id"):
                self.log_event(f"[{broker_name}] Protective already tracked for {ticker} — skip duplicate")
                return
            stop_pct = get_stop_distance_pct(broker_id, ticker, asset_type)
            trail_pct = get_trail_pct(broker_id, ticker, asset_type)
            stop_px = float(price) * (1.0 - stop_pct)
            if self.paper_mode:
                set_protective_order(broker_id, ticker, {
                    "order_id": f"paper-{ticker}-{int(time.time())}",
                    "kind": "virtual_stop",
                    "stop_price": stop_px,
                    "qty": qty,
                    "paper": True,
                })
                self.log_event(
                    f"[{broker_name}] [PAPER] Virtual stop {ticker} @ {stop_px:.4f} "
                    f"(-{stop_pct*100:.1f}%); software TTP trail {trail_pct*100:.1f}%"
                )
                return
            broker = self.brokers.get(broker_name)
            if not broker or not broker.is_connected:
                self.log_event(f"[{broker_name}] No broker for protective stop on {ticker} — software TTP only")
                self._note_protective_gap(broker_name, ticker, "no broker connection")
                return
            if not getattr(broker, "supports_protective_stops", False):
                self.log_event(
                    f"[{broker_name}] Broker has no protective-stop API for {ticker} — software TTP only"
                )
                return
            is_crypto = "crypto" in str(asset_type).lower() or str(ticker).upper() in KNOWN_CRYPTOS
            if is_crypto:
                self.log_event(
                    f"[{broker_name}] Skip broker stop [{ticker}]: crypto — software TTP only"
                )
                return
            # RH rejects stops on fractional qty — mark N/A once, never treat as retryable gap
            try:
                from scoring import _qty_is_whole_shares
                whole = _qty_is_whole_shares(qty)
            except Exception:
                whole = abs(qty - round(qty)) < 1e-9 and qty >= 1.0
            if not whole:
                na_key = f"{broker_name}:{str(ticker).upper()}"
                logged = getattr(self, "_frac_stop_na_logged", None)
                if logged is None:
                    self._frac_stop_na_logged = set()
                    logged = self._frac_stop_na_logged
                if na_key not in logged:
                    logged.add(na_key)
                    self.log_event(
                        f"[{broker_name}] Fractional qty {qty:.4f} [{ticker}] — "
                        f"broker stop N/A, TTP only (will not retry attach)"
                    )
                # Do not put in actionable gaps — repair would hammer forever
                gaps = getattr(self, "_protective_gaps", None)
                if gaps is not None:
                    gaps.pop(na_key, None)
                return
            ok, oid, msg = broker.place_protective_stop(
                ticker, asset_type, qty, price, stop_pct, trail_pct=trail_pct,
            )
            if ok and oid:
                set_protective_order(broker_id, ticker, {
                    "order_id": oid,
                    "kind": "broker_stop",
                    "stop_price": stop_px,
                    "qty": qty,
                    "paper": False,
                })
                self.log_event(f"[{broker_name}] Protective stop attached [{ticker}]: {msg}")
                self._clear_protective_gap(broker_name, ticker)
            else:
                clear_protective_order(broker_id, ticker)
                self.log_event(
                    f"[{broker_name}] Could not attach broker stop [{ticker}]: {msg} — software TTP remains"
                )
                self._note_protective_gap(broker_name, ticker, msg or "attach failed")
        except Exception as e:
            self.log_event(f"[{broker_name}] Protective stop error [{ticker}]: {e}")
            self._note_protective_gap(broker_name, ticker, str(e))

    def _note_protective_gap(self, broker_name, ticker, detail=""):
        detail_s = str(detail or "missing")[:120]
        # Fractional / crypto N/A are not repairable gaps
        dl = detail_s.lower()
        if "fractional" in dl or ("crypto" in dl and "ttp only" in dl):
            na_key = f"{broker_name}:{str(ticker).upper()}"
            logged = getattr(self, "_frac_stop_na_logged", None)
            if logged is None:
                self._frac_stop_na_logged = set()
                logged = self._frac_stop_na_logged
            if na_key not in logged:
                logged.add(na_key)
                self.log_event(
                    f"[{broker_name}] Stop N/A [{ticker}]: {detail_s}"
                )
            return
        gaps = getattr(self, "_protective_gaps", None)
        if gaps is None:
            self._protective_gaps = {}
            gaps = self._protective_gaps
        key = f"{broker_name}:{str(ticker).upper()}"
        gaps[key] = detail_s
        if hasattr(self, "_refresh_portfolio_heat"):
            QTimer.singleShot(0, self._refresh_portfolio_heat)

    def _clear_protective_gap(self, broker_name, ticker):
        gaps = getattr(self, "_protective_gaps", None) or {}
        gaps.pop(f"{broker_name}:{str(ticker).upper()}", None)

    def _broker_display_from_id(self, broker_id):
        bid = str(broker_id or "").upper().replace("*", "")
        mapping = {
            "ROBINHOOD": "Robinhood",
            "COINBASE": "Coinbase",
            "ETRADE": "E*TRADE",
            "E*TRADE": "E*TRADE",
        }
        return mapping.get(bid) or mapping.get(str(broker_id or "").upper()) or str(broker_id or "")

    def _find_holding_for_stop_repair(self, broker_name, ticker):
        """Locate qty/price for a missing protective stop from recent snapshots."""
        tu = str(ticker or "").replace("-USD", "").upper()
        candidates = []
        snap = getattr(self, "_last_assets_snapshot", None) or []
        if isinstance(snap, list):
            candidates.extend(snap)
        by_broker = getattr(self, "_heat_holdings_by_broker", {}) or {}
        for row in by_broker.get(broker_name) or []:
            if isinstance(row, dict):
                candidates.append(row)
        for a in candidates:
            if not isinstance(a, dict):
                continue
            b = a.get("broker") or a.get("broker_name") or ""
            t = str(a.get("ticker") or "").replace("-USD", "").upper()
            if t != tu:
                continue
            if b in BROKER_NAMES and b != broker_name:
                continue
            try:
                qty = float(a.get("shares") or a.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                px = float(
                    a.get("live_price")
                    or a.get("price")
                    or a.get("cost")
                    or 0
                )
            except (TypeError, ValueError):
                px = 0.0
            if qty <= 0:
                continue
            if px <= 0:
                try:
                    broker = self.brokers.get(broker_name)
                    px = float(broker.get_live_price(tu) if broker else 0) or 0.0
                except Exception:
                    px = 0.0
            if px <= 0:
                continue
            asset_type = a.get("type") or a.get("asset_type") or "stock"
            return qty, px, asset_type
        return None

    def _run_eod_protective_pass(self):
        """
        ~15:59 ET pre-close checklist (equity only):
          1) Force RH protective-stop repair (whole shares).
          2) Flatten RH equities that cannot trade extended/overnight (stuck names).
          3) Warn / optional flatten for E*TRADE (no broker stop API).
        Crypto left alone (24/7 TTP while the app runs).
        """
        self.log_event("[EOD] Pre-close protective checklist…")
        try:
            self._maybe_repair_protective_stops(force=True)
        except Exception as e:
            self.log_event(f"[EOD] Stop repair error: {e}")

        flatten_rows = []
        flatten_rows.extend(self._eod_stuck_rh_equity_rows())

        et = self.brokers.get("E*TRADE")
        et_armed = bool(self.auto_trade_enabled.get("E*TRADE"))
        et_live = bool(et and (self.paper_mode or getattr(et, "is_connected", False)))
        et_equity_rows = []
        if et_live:
            holdings = []
            try:
                if self.paper_mode:
                    book = self.sandbox_holdings.get("E*TRADE") or {}
                    for t, pos in book.items():
                        holdings.append({
                            "ticker": t,
                            "shares": float((pos or {}).get("shares") or 0),
                            "type": (pos or {}).get("type") or "stock",
                            "price": float((pos or {}).get("cost") or 0),
                        })
                elif et:
                    holdings = list(et.get_current_holdings() or [])
            except Exception as e:
                self.log_event(f"[EOD] E*TRADE holdings read failed: {e}")
                holdings = []

            for h in holdings:
                if not isinstance(h, dict):
                    continue
                ticker = str(h.get("ticker") or "").replace("-USD", "").upper()
                if not ticker:
                    continue
                asset_type = str(h.get("type") or "")
                is_crypto = "crypto" in asset_type.lower() or ticker in KNOWN_CRYPTOS
                if is_crypto:
                    continue
                try:
                    shares = float(h.get("shares") or 0)
                except (TypeError, ValueError):
                    shares = 0.0
                if shares <= 0:
                    continue
                try:
                    price = float(h.get("price") or h.get("last") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                et_equity_rows.append({
                    "broker": "E*TRADE",
                    "ticker": ticker,
                    "shares": shares,
                    "price": price,
                    "avg_cost": float(h.get("cost") or h.get("avg_cost") or 0),
                    "type": asset_type or "stock",
                    "sell_all": True,
                    "action": "ET FLATTEN (EOD)",
                    "reason": "ET FLATTEN",
                })

            if et_equity_rows:
                names = ", ".join(r["ticker"] for r in et_equity_rows[:12])
                more = f" (+{len(et_equity_rows) - 12})" if len(et_equity_rows) > 12 else ""
                warn = (
                    f"[EOD] E*TRADE overnight risk: {len(et_equity_rows)} equity holding(s) "
                    f"({names}{more}) — no broker stop API; software TTP dies if the app is off "
                    f"or midnight reauth fails."
                )
                self.log_event(warn)
                try:
                    self.send_discord_alert(warn, urgent=True, prefix="EOD")
                except Exception:
                    pass

                if bool(self.settings.get("et_flatten_before_close", False)):
                    if et_armed:
                        flatten_rows.extend(et_equity_rows)
                    else:
                        self.log_event(
                            "[EOD] Flatten ON but E*TRADE is disarmed — warning only, not selling."
                        )
                else:
                    extra = "" if et_armed else " (ET auto-trader is disarmed — TTP idle if app is off.)"
                    self.log_event(
                        "[EOD] Flatten OFF — holding ET overnight. Enable "
                        "'Flatten E*TRADE equities before close' in Settings if you want auto flat."
                        + extra
                    )
        else:
            self.log_event("[EOD] E*TRADE not connected — overnight warn skipped.")

        if not flatten_rows:
            return

        rh_n = sum(1 for r in flatten_rows if r.get("broker") == "Robinhood")
        et_n = sum(1 for r in flatten_rows if r.get("broker") == "E*TRADE")
        parts = []
        if rh_n:
            parts.append(f"{rh_n} RH stuck")
        if et_n:
            parts.append(f"{et_n} ET")
        self.log_event(f"[EOD] Flattening {' + '.join(parts)} equity position(s) before close…")
        self.set_working_state(True, "EOD equity flatten…")
        self.run_thread(
            self._bg_execute_sell_batch,
            self._on_eod_flatten_done,
            flatten_rows,
        )

    def _eod_stuck_rh_equity_rows(self):
        """RH equities that cannot exit in extended/overnight — flatten before the bell."""
        rows = []
        if not bool(self.auto_trade_enabled.get("Robinhood")):
            return rows
        rh = self.brokers.get("Robinhood")
        if not rh or not (self.paper_mode or getattr(rh, "is_connected", False)):
            return rows
        holdings = []
        try:
            if self.paper_mode:
                book = self.sandbox_holdings.get("Robinhood") or {}
                for t, pos in book.items():
                    holdings.append({
                        "ticker": t,
                        "shares": float((pos or {}).get("shares") or 0),
                        "type": (pos or {}).get("type") or "stock",
                        "price": float((pos or {}).get("cost") or 0),
                    })
            else:
                holdings = list(rh.get_current_holdings() or [])
        except Exception:
            return rows
        for h in holdings:
            if not isinstance(h, dict):
                continue
            ticker = str(h.get("ticker") or "").replace("-USD", "").upper()
            if not ticker:
                continue
            asset_type = str(h.get("type") or "")
            is_crypto = "crypto" in asset_type.lower() or ticker in KNOWN_CRYPTOS
            if is_crypto:
                continue
            try:
                shares = float(h.get("shares") or 0)
            except (TypeError, ValueError):
                shares = 0.0
            if shares <= 0:
                continue
            try:
                price = float(h.get("price") or h.get("last") or h.get("cost") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                try:
                    price = float(rh.get_live_price(ticker) or 0)
                except Exception:
                    price = 0.0
            action = _auto_cycle.equity_eod_action_for_holding(
                ticker, shares, price, asset_type,
                broker_name="Robinhood",
                frac_ext_ineligible=getattr(self, "_frac_ext_ineligible", None),
                known_cryptos=KNOWN_CRYPTOS,
            )
            if action != "flatten":
                continue
            rows.append({
                "broker": "Robinhood",
                "ticker": ticker,
                "shares": shares,
                "price": price,
                "avg_cost": float(h.get("cost") or h.get("avg_cost") or 0),
                "type": asset_type or "stock",
                "sell_all": True,
                "action": "EOD FLATTEN (stuck)",
                "reason": "EOD stuck equity",
            })
        return rows

    def _on_eod_flatten_done(self, payload):
        payload = payload or {}
        for note in payload.get("notes") or []:
            self.log_event(note)
        n_ok = 0
        for fill in payload.get("fills") or []:
            st = str(fill.get("status") or "")
            self.log_event(f"[EOD] [{fill.get('ticker')}] flatten: {st}")
            if "Fail" not in st and "Skipped" not in st:
                n_ok += 1
        try:
            self.send_discord_alert(
                f"EOD ET flatten done — {n_ok}/{len(payload.get('fills') or [])} ok",
                is_trade=True,
                urgent=True,
            )
        except Exception:
            pass
        self.set_working_state(False)
        self.refresh_recent_trades()
        try:
            self.manual_portfolio_reload(and_score=False, force=True)
        except Exception:
            pass

    def _maybe_repair_protective_stops(self, force=False):
        """Attach missing broker protective stops (throttled). Never flattens positions."""
        if not bool(self.settings.get("attach_protective_stops", True)):
            return
        now = time.time()
        if not force:
            last = float(getattr(self, "_last_stop_repair_pass", 0.0) or 0.0)
            if now - last < _STOP_REPAIR_PASS_COOLDOWN_SEC:
                return
        health = getattr(self, "_last_protective_health", None) or {}
        missing = list(health.get("missing") or [])
        gaps = getattr(self, "_protective_gaps", {}) or {}
        for key in list(gaps.keys()):
            if ":" not in key:
                continue
            bname, tick = key.split(":", 1)
            bid = {"Robinhood": "ROBINHOOD", "Coinbase": "COINBASE", "E*TRADE": "ETRADE"}.get(
                bname, str(bname).upper()
            )
            missing.append({"broker_id": bid, "ticker": tick, "broker": bname})
        if not missing:
            if force:
                self.log_event("[STOPS] Repair: nothing missing.")
            return

        self._last_stop_repair_pass = now
        cool = getattr(self, "_stop_repair_ticker_cooldown", None)
        if cool is None:
            self._stop_repair_ticker_cooldown = {}
            cool = self._stop_repair_ticker_cooldown
        skip_logged = getattr(self, "_stop_repair_skip_logged", None)
        if skip_logged is None:
            self._stop_repair_skip_logged = set()
            skip_logged = self._stop_repair_skip_logged

        attempted = 0
        attached = 0
        skipped = 0
        seen = set()
        for item in missing:
            if attempted >= _STOP_REPAIR_MAX_PER_PASS:
                break
            bid = item.get("broker_id") or item.get("broker") or ""
            ticker = str(item.get("ticker") or "").replace("-USD", "").upper()
            if not ticker:
                continue
            broker_name = item.get("broker") or self._broker_display_from_id(bid)
            if broker_name not in BROKER_NAMES:
                broker_name = self._broker_display_from_id(bid)
            key = f"{broker_name}:{ticker}"
            if key in seen:
                continue
            seen.add(key)

            if not force:
                prev_ts = float(cool.get(key) or 0.0)
                if now - prev_ts < _STOP_REPAIR_TICKER_COOLDOWN_SEC:
                    continue

            broker = self.brokers.get(broker_name)
            is_crypto = ticker in KNOWN_CRYPTOS
            supports = bool(getattr(broker, "supports_protective_stops", False)) or self.paper_mode
            if broker_name == "E*TRADE" or not supports or is_crypto:
                skipped += 1
                if key not in skip_logged:
                    skip_logged.add(key)
                    why = (
                        "E*TRADE has no protective-stop API"
                        if broker_name == "E*TRADE"
                        else ("crypto has no broker stop API" if is_crypto else "broker stops unsupported")
                    )
                    self.log_event(f"[STOPS] Skip repair [{broker_name}] {ticker}: {why} — software TTP only")
                continue

            if not self.paper_mode and (not broker or not getattr(broker, "is_connected", False)):
                skipped += 1
                continue

            found = self._find_holding_for_stop_repair(broker_name, ticker)
            if not found:
                skipped += 1
                if force or key not in skip_logged:
                    skip_logged.add(key)
                    self.log_event(
                        f"[STOPS] Repair deferred [{broker_name}] {ticker}: holding qty/price unavailable"
                    )
                continue

            qty, px, asset_type = found
            if is_crypto or "crypto" in str(asset_type).lower():
                skipped += 1
                if key not in skip_logged:
                    skip_logged.add(key)
                    self.log_event(
                        f"[STOPS] Skip repair [{broker_name}] {ticker}: crypto — software TTP only"
                    )
                continue

            # Fractional equity — RH broker stop N/A; do not retry every repair pass
            try:
                from scoring import _qty_is_whole_shares
                whole = _qty_is_whole_shares(qty)
            except Exception:
                whole = abs(float(qty) - round(float(qty))) < 1e-9 and float(qty) >= 1.0
            if not whole:
                skipped += 1
                if key not in skip_logged:
                    skip_logged.add(key)
                    self.log_event(
                        f"[STOPS] Skip repair [{broker_name}] {ticker}: "
                        f"fractional qty {float(qty):.4f} — broker stop N/A, TTP only"
                    )
                self._clear_protective_gap(broker_name, ticker)
                continue

            cool[key] = now
            attempted += 1
            spent = float(qty) * float(px)
            self.log_event(
                f"[STOPS] Repair attaching [{broker_name}] {ticker} qty={qty:.4f} @ {px:.4f}"
            )
            before = None
            try:
                from scoring import get_protective_order
                broker_id = getattr(broker, "broker_id", None) or str(broker_name).upper()
                before = get_protective_order(broker_id, ticker)
            except Exception:
                before = None
            self._attach_protective_stop(broker_name, ticker, asset_type or "stock", px, spent)
            try:
                from scoring import get_protective_order
                broker_id = getattr(broker, "broker_id", None) or str(broker_name).upper()
                after = get_protective_order(broker_id, ticker)
                if after and after.get("order_id") and (
                    not before or before.get("order_id") != after.get("order_id")
                ):
                    attached += 1
                    self._clear_protective_gap(broker_name, ticker)
            except Exception:
                pass

        if attempted or force:
            self.log_event(
                f"[STOPS] Repair pass: attempted={attempted} attached={attached} skipped={skipped}"
            )
            if hasattr(self, "_refresh_portfolio_heat"):
                QTimer.singleShot(0, self._refresh_portfolio_heat)

    def _log_decision(self, **kwargs):
        """Append autotrader decision for later Reports / replay."""
        try:
            import journal as journal_mod
            from scoring import posture_for_broker
            row = dict(kwargs)
            row.setdefault("posture", posture_for_broker(self.cycle_broker_name, self.settings))
            if not row.get("engine"):
                task = getattr(self, "_cycle_task", None)
                if task:
                    row["engine"] = task
            if "regime_ok" not in row:
                try:
                    from scoring import market_regime_ok, uses_btc_regime
                    is_crypto = bool(row.get("is_crypto"))
                    use_btc = uses_btc_regime(row.get("ticker"), is_crypto)
                    ok, why = market_regime_ok(is_crypto=use_btc)
                    row["regime_ok"] = bool(ok)
                    if not ok:
                        row.setdefault("regime_why", why)
                except Exception:
                    pass
            if row.get("action"):
                row["action"] = str(row["action"]).upper()
            journal_mod.log_decision(row)
        except Exception:
            pass

    def _cancel_protective_stop(self, broker_name, ticker, asset_type=""):
        """Cancel orphan protective orders after a full exit."""
        from scoring import get_protective_order, clear_protective_order
        broker_id = getattr(self.brokers.get(broker_name), "broker_id", None) or str(broker_name).upper()
        info = get_protective_order(broker_id, ticker)
        if not info:
            return
        oid = info.get("order_id")
        is_crypto = "crypto" in str(asset_type).lower() or str(ticker).upper() in KNOWN_CRYPTOS
        if self.paper_mode or info.get("paper") or (oid and str(oid).startswith("paper-")):
            clear_protective_order(broker_id, ticker)
            self.log_event(f"[{broker_name}] [PAPER] Cleared virtual stop for {ticker}")
            return
        broker = self.brokers.get(broker_name)
        if broker and oid:
            try:
                ok, msg = broker.cancel_order(oid, is_crypto=is_crypto)
                self.log_event(
                    f"[{broker_name}] Cancel protective [{ticker}] "
                    f"{'OK' if ok else 'fail'}: {msg}"
                )
            except Exception as e:
                self.log_event(f"[{broker_name}] Cancel protective error [{ticker}]: {e}")
        clear_protective_order(broker_id, ticker)
    def execute_portfolio_trades(self, auto_mode=False):
        total_rows = self.portfolio_table.rowCount()
        if total_rows == 0: return

        sell_rows = []
        for row in range(total_rows):
            ticker_item = self.portfolio_table.item(row, 1)
            action_item = self.portfolio_table.item(row, 6)
            if ticker_item and ticker_item.checkState() == Qt.Checked and action_item:
                if "SELL" in action_item.text().upper():
                    sell_rows.append(row)

        if not sell_rows:
            if not auto_mode: QMessageBox.information(self, "No Selection", "No checked rows have a SELL action to execute.")
            return

        if not auto_mode:
            mode_tag = "[PAPER SIMULATION] " if self.paper_mode else "[LIVE MONEY] "
            summary = f"{mode_tag}Trades to Execute:\n\n"
            for row in sell_rows:
                broker = self.portfolio_table.item(row, 0).text() if self.portfolio_table.item(row, 0) else ""
                summary += f"• [{broker}] SELL {self.portfolio_table.item(row, 1).text()}\n"
            if QMessageBox.question(self, "Confirm Execution", summary, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return

        sell_list = []
        for row in sell_rows:
            ticker_item = self.portfolio_table.item(row, 1)
            ticker = ticker_item.text()
            row_broker = ticker_item.data(Qt.UserRole + 1) or (
                self.portfolio_table.item(row, 0).text() if self.portfolio_table.item(row, 0) else self.cycle_broker_name
            )
            asset_type = ticker_item.data(Qt.UserRole) or ""
            is_crypto = "crypto" in str(asset_type).lower() or str(ticker).upper() in KNOWN_CRYPTOS
            if self.is_locked(ticker, is_crypto=is_crypto):
                self.log_event(f"[{row_broker}] Skipped [{ticker}]: trade lock active")
                continue
            try:
                price = float(self.portfolio_table.item(row, 4).text().replace('$', '').replace(',', '') or 0.0)
            except Exception:
                price = 0.0
            try:
                shares_val = float(self.portfolio_table.item(row, 2).text() or 0.0)
            except Exception:
                shares_val = 0.0
            try:
                avg_cost = float(
                    self.portfolio_table.item(row, 3).text().replace('$', '').replace(',', '') or 0.0
                )
            except Exception:
                avg_cost = 0.0
            sell_list.append({
                "broker": row_broker,
                "ticker": ticker,
                "shares": shares_val,
                "price": price,
                "avg_cost": avg_cost,
                "type": asset_type,
                "table_row": row,
                "sell_all": True,
            })

        if not sell_list:
            return
        self.set_working_state(True, "Executing sells…")
        self.run_thread(
            self._bg_execute_sell_batch,
            lambda payload: self._on_sell_batch_done(payload, auto_mode=auto_mode, finish_cycle=False),
            sell_list,
        )

    def _affordable_scan_buy_candidates(self, buy_candidates):
        """Drop unaffordable scan BUYs before ranked log / buy batch (esp. E*TRADE $100 BP)."""
        if not buy_candidates:
            return [], []
        broker = self.cycle_broker_name
        broker_obj = self.brokers.get(broker)
        broker_id = getattr(broker_obj, "broker_id", None) or str(broker).upper()
        equity, bp, _ = self.get_effective_balances(broker)
        prefer_equity_rth = False
        try:
            prefer_equity_rth = str(self.get_equity_session_info().get("label") or "") == "REGULAR"
        except Exception:
            prefer_equity_rth = False
        prefer_whole = _auto_cycle.affordability_prefer_whole_shares(
            broker_id,
            prefer_equity_rth=prefer_equity_rth,
            settings=self.settings,
        )
        return _auto_cycle.filter_affordable_buy_candidates(
            buy_candidates,
            buying_power=bp,
            equity=equity,
            broker_id=broker_id,
            settings=self.settings,
            prefer_whole_shares=prefer_whole,
        )

    def execute_scanner_trades(self, table, auto_mode=False, buy_candidates=None):
        """Execute BUY rows. Auto mode prefers pre-ranked buy_candidates from the bg scan."""
        total_rows = table.rowCount()
        if total_rows == 0 and not buy_candidates:
            if auto_mode:
                self.set_working_state(False)
                self.cycle_finished()
            return

        # Manual trades from All view need an explicit broker target
        if not auto_mode and self.view_mode == "All" and not self._cycle_broker:
            is_crypto_table = (table is self.crypto_table)
            choices = ["Robinhood", "Coinbase"] if is_crypto_table else ["Robinhood"]
            choice, ok = QInputDialog.getItem(self, "Select Broker", "Execute these trades on:", choices, 0, False)
            if not ok:
                return
            self.active_broker_name = choice

        candidates = list(buy_candidates or [])
        if not candidates:
            for row in range(total_rows):
                ticker_item = table.item(row, 0)
                action_item = table.item(row, 3)
                if ticker_item and ticker_item.checkState() == Qt.Checked and action_item:
                    action = action_item.text().upper()
                    if "BUY" in action and "DO NOT BUY" not in action:
                        asset_type = table.item(row, 1).text() if table.item(row, 1) else ""
                        try:
                            price = float((table.item(row, 2).text() or "0").replace("$", "").replace(",", "") or 0)
                        except Exception:
                            price = 0.0
                        candidates.append({
                            "ticker": ticker_item.text(),
                            "asset_type": asset_type,
                            "price": price,
                            "score": 0.0,
                            "table_row": row,
                        })

        if not candidates:
            if not auto_mode:
                QMessageBox.information(self, "No Selection", "No checked rows have a BUY recommendation to execute.")
            return

        # Filter locks on UI thread
        filtered = []
        for c in candidates:
            ticker = c.get("ticker") or ""
            asset_type = c.get("asset_type") or ""
            is_crypto = "crypto" in str(asset_type).lower() or str(ticker).upper() in KNOWN_CRYPTOS
            if self.is_locked(ticker, is_crypto=is_crypto):
                self.log_event(f"[{self.cycle_broker_name}] Skipped [{ticker}]: trade lock active")
                continue
            filtered.append(c)
        if not filtered:
            if auto_mode:
                self.log_event(
                    f"[{self.cycle_broker_name}] No buys executed — "
                    f"{len(candidates)} candidate(s) trade-locked"
                )
                self.set_working_state(False)
                self.cycle_finished()
            return

        if not auto_mode:
            sample_type = filtered[0].get("asset_type", "")
            equity, bp, _locked = self.get_effective_balances(self.cycle_broker_name)
            try:
                _h = self.get_broker_holdings(self.cycle_broker_name) or []
                _open = len({(a.get("ticker") or "").upper() for a in _h if a.get("ticker")})
            except Exception:
                _open = 0
            trade_dollars = self.calculate_order_sizing(
                bp, sample_type, equity=equity, open_count=_open,
            )
            if trade_dollars <= 0:
                QMessageBox.warning(self, "Insufficient Funds", "Buying power is too low for the minimum order size.")
                return
            mode_tag = "[PAPER SIMULATION] " if self.paper_mode else "[LIVE MONEY] "
            summary = f"{mode_tag}Trades to Execute on {self.cycle_broker_name} (~{format_currency(trade_dollars)} each):\n\n"
            for c in filtered:
                summary += f"• BUY {c.get('ticker')}\n"
            if QMessageBox.question(self, "Confirm Execution", summary, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return

        self.set_working_state(True, "Executing buys…")
        runner = self.run_cycle_thread if auto_mode else self.run_thread
        rank = bool(buy_candidates is None and auto_mode)
        advisor_gate = auto_mode and bool(self.settings.get("advisor_ask_before_apply", True))
        runner(
            self._bg_buy_batch_safe,
            lambda payload: self._on_buy_batch_done(payload, auto_mode=auto_mode, table=table),
            filtered,
            rank,
            advisor_gate=advisor_gate,
        )

    def _bg_buy_batch(self, candidates, rank=False, advisor_gate=False):
        """Place buys on a worker thread (confirm_order sleeps stay off the UI)."""
        from scoring import (
            concentration_blocks_buy, buy_rank_score_for_book, buy_rank_score,
            evaluate_scale_in, record_scale_in, get_scale_in_params,
            opportunity_swap_enabled, pick_rotation_funding, mark_opportunity_swap_exit,
            last_rotation_reject_reason, record_rotation,
            entry_regime_ok, broker_min_notional, RH_CRYPTO_MIN_NOTIONAL,
            crypto_new_entry_ok, posture_for_broker, posture_knobs_for_broker,
            broker_has_posture_override, _drawdown_block,
        )
        broker_name = self.cycle_broker_name
        broker_id = getattr(self.brokers.get(broker_name), "broker_id", None) or str(broker_name).upper()
        offset = self.settings.get("limit_offset_pct", 0.1) / 100.0
        session = self.get_equity_session_info()
        use_ext = session["use_ext"]
        market_hours = session["market_hours"]
        allow_fractional = session["fractional_ok"]
        posture = posture_for_broker(broker_name, self.settings)
        knobs = posture_knobs_for_broker(broker_name, self.settings)
        max_positions = int(knobs.get("max_open_positions", 8) or 8)
        max_buys = int(knobs.get("max_buys_per_cycle", 1) or 1)
        si_overlay = None if broker_has_posture_override(broker_name, self.settings) else self.settings
        si_params = get_scale_in_params(posture=posture, settings=si_overlay)
        exit_roi_scale = float(knobs.get("exit_roi_scale") or 1.0)
        exit_time_scale = float(knobs.get("exit_time_scale") or 1.0)
        ttp_arm_scale = float(knobs.get("ttp_arm_scale") or 1.0)
        notes = []
        ranked = list(candidates or [])
        rotated_once = False
        orig_n = len(ranked)
        proposals_made = 0

        # Defense in depth: sandbox/$0 should skip before scoring noise, but also here
        idle_why = self._buy_engines_idle_reason(broker_name)
        if idle_why:
            try:
                self._journal_idle_skip(
                    broker_name, idle_why,
                    engine=getattr(self, "_cycle_task", None),
                )
            except Exception:
                pass
            return {
                "fills": [],
                "notes": [f"[{broker_name}] {idle_why} — skipping buy batch"],
                "buys_done": 0,
                "broker": broker_name,
            }

        dd_ok, dd_why = _drawdown_block(broker_id)
        if not dd_ok:
            notes.append(f"[{broker_name}] {dd_why} — skipping buy batch")
            return {
                "fills": [],
                "notes": notes,
                "buys_done": 0,
                "broker": broker_name,
            }

        equity, bp, _locked = self.get_effective_balances(broker_name)
        holdings = self.get_broker_holdings(broker_name) or []
        held = {
            (a.get("ticker") or "").upper()
            for a in holdings
            if isinstance(a, dict) and a.get("ticker")
        }
        open_count = len(held)
        # Refresh monitor cache from this bg holdings pull
        self._holdings_count_cache[broker_name] = open_count

        holdings_meta = []
        holdings_by_ticker = {}
        broker = self.brokers.get(broker_name)
        # Coinbase (crypto-only): skip equity-wide crypto book cap — BP util is the cash rail
        crypto_only_broker = not bool(getattr(broker, "supports_equities", True))
        crypto_held_map = {}
        try:
            by_b = {}
            for bname in ("Robinhood", "Coinbase"):
                by_b[bname] = self.get_broker_holdings(bname) or []
            crypto_held_map = _auto_cycle.crypto_held_across_brokers(by_b)
        except Exception:
            crypto_held_map = {}
        if broker_name == "E*TRADE" and broker and hasattr(broker, "prefetch_quotes"):
            try:
                broker.prefetch_quotes([
                    c.get("ticker") for c in (candidates or []) if c.get("ticker")
                ])
            except Exception:
                pass
        prefer_equity_rth = False
        if not crypto_only_broker:
            try:
                sess = self.get_equity_session_info()
                prefer_equity_rth = str(sess.get("label") or "") == "REGULAR"
            except Exception:
                prefer_equity_rth = False
        for a in holdings:
            if not isinstance(a, dict):
                continue
            t = a.get("ticker") or ""
            if not t:
                continue
            is_c = "crypto" in str(a.get("type") or "").lower() or str(t).upper() in KNOWN_CRYPTOS
            px = 0.0
            try:
                px = float(broker.get_live_price(t) if broker else 0.0) or 0.0
            except Exception:
                px = 0.0
            try:
                avg_cost = float(a.get("cost") or 0.0)
            except (TypeError, ValueError):
                avg_cost = 0.0
            if avg_cost <= 0:
                avg_cost = float(self._avg_cost_for(broker_name, t) or 0.0)
            meta = {
                "ticker": t,
                "value": float(a.get("shares") or 0) * px,
                "is_crypto": is_c,
                "avg_cost": avg_cost,
                "shares": float(a.get("shares") or 0),
            }
            holdings_meta.append(meta)
            holdings_by_ticker[str(t).upper()] = meta

        # Rank against *this* book; held names may qualify as gated scale-in
        pre_ranked = (not rank) and _auto_cycle.buy_batch_candidates_pre_ranked(ranked)
        if ranked and not pre_ranked:
            with SuppressPrints():
                for c in ranked:
                    ticker = c.get("ticker") or ""
                    asset_type = c.get("asset_type") or ""
                    is_crypto = "crypto" in str(asset_type).lower() or ticker.upper() in KNOWN_CRYPTOS
                    tu = ticker.upper()
                    try:
                        if tu in held:
                            meta = holdings_by_ticker.get(tu) or {}
                            live_px = float(c.get("price") or 0.0)
                            if live_px <= 0:
                                try:
                                    live_px = float(broker.get_live_price(ticker) if broker else 0.0) or 0.0
                                except Exception:
                                    live_px = 0.0
                            base = float(buy_rank_score(ticker, is_crypto=is_crypto))
                            ev = evaluate_scale_in(
                                ticker, live_px, meta.get("avg_cost") or 0.0,
                                broker_id=broker_id, asset_type=asset_type, is_crypto=is_crypto,
                                signal_score=base, posture=posture, settings=si_overlay,
                                existing_name_value=meta.get("value") or 0.0,
                                portfolio_value=equity,
                            )
                            c["scale_in"] = bool(ev.get("allowed"))
                            c["scale_in_eval"] = ev
                            if ev.get("allowed"):
                                c["score"] = float(buy_rank_score_for_book(
                                    ticker, is_crypto=is_crypto, held_tickers=held,
                                    holdings_meta=holdings_meta, portfolio_value=equity,
                                    scale_in_candidate=True,
                                    crypto_only_broker=crypto_only_broker,
                                    prefer_equity_rth=prefer_equity_rth,
                                ))
                                notes.append(
                                    f"[{broker_name}] "
                                    + _auto_cycle.format_scale_in_ok_note(
                                        ticker, ev.get("reason") or ""
                                    )
                                )
                            else:
                                c["score"] = -1000.0
                                self._note_scale_in_skip(
                                    notes, broker_name, ticker, ev.get("reason") or "blocked",
                                    score=base, is_crypto=is_crypto,
                                )
                        else:
                            c["scale_in"] = False
                            c["score"] = float(buy_rank_score_for_book(
                                ticker, is_crypto=is_crypto, held_tickers=held,
                                holdings_meta=holdings_meta, portfolio_value=equity,
                                crypto_only_broker=crypto_only_broker,
                                prefer_equity_rth=prefer_equity_rth,
                            ))
                    except Exception:
                        c["score"] = float(c.get("score") or 0.0)
                        c["scale_in"] = bool(c.get("scale_in"))
            ranked.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            prefer_whole = _auto_cycle.affordability_prefer_whole_shares(
                broker_id,
                prefer_equity_rth=prefer_equity_rth,
                settings=self.settings,
            )
            affordable, unaff = _auto_cycle.filter_affordable_buy_candidates(
                ranked,
                buying_power=bp,
                equity=equity,
                broker_id=broker_id,
                settings=self.settings,
                prefer_whole_shares=prefer_whole,
            )
            if unaff:
                sample = ", ".join(unaff[:5])
                more = f" (+{len(unaff) - 5})" if len(unaff) > 5 else ""
                self._throttled_log(
                    f"{broker_name}:unaffordable_pre_rank",
                    f"[{broker_name}] Unaffordable after rank — dropped: {sample}{more}",
                    cooldown_sec=780,
                )
            ranked = affordable
            # Drop names that cannot improve the book (already held without scale-in / cluster full)
            actionable = _auto_cycle.filter_actionable_ranked(ranked)
            # Always log Ranked (incl. single-signal BREAKOUT) so the trail is visible
            if rank or ranked:
                notes.append(
                    _auto_cycle.format_ranked_for_book_note(broker_name, actionable, ranked)
                )
            if not actionable and ranked:
                # Ranked → empty after filter: always say why (scale-in / cluster already noted)
                if _auto_cycle.should_append_empty_after_rank_filter(notes, ranked):
                    notes.append(
                        _auto_cycle.empty_after_rank_filter_note(broker_name, len(ranked))
                    )
            ranked = actionable

        elif ranked and pre_ranked:
            ranked.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            prefer_whole = _auto_cycle.affordability_prefer_whole_shares(
                broker_id,
                prefer_equity_rth=prefer_equity_rth,
                settings=self.settings,
            )
            affordable, unaff = _auto_cycle.filter_affordable_buy_candidates(
                ranked,
                buying_power=bp,
                equity=equity,
                broker_id=broker_id,
                settings=self.settings,
                prefer_whole_shares=prefer_whole,
            )
            if unaff:
                sample = ", ".join(unaff[:5])
                more = f" (+{len(unaff) - 5})" if len(unaff) > 5 else ""
                self._throttled_log(
                    f"{broker_name}:unaffordable_pre_rank",
                    f"[{broker_name}] Unaffordable after rank — dropped: {sample}{more}",
                    cooldown_sec=780,
                )
            ranked = affordable
            ranked = _auto_cycle.filter_actionable_ranked(ranked)

        fills = []
        buys_done = 0
        execute_skips = []  # always collect — even when individual notes are throttled

        def _holdings_for_picker():
            rows = []
            for meta in holdings_meta:
                t = meta.get("ticker") or ""
                tu2 = str(t).upper()
                m = holdings_by_ticker.get(tu2) or meta
                px = 0.0
                try:
                    px = float(broker.get_live_price(t) if broker else 0.0) or 0.0
                except Exception:
                    px = 0.0
                asset_type = "cryptocurrency" if m.get("is_crypto") else "stock"
                probe = {
                    "broker": broker_name,
                    "ticker": t,
                    "shares": float(m.get("shares") or 0.0),
                    "price": px,
                    "value": float(m.get("value") or 0.0),
                    "type": asset_type,
                }
                locked, _why = _auto_cycle.classify_locked_holding(
                    probe, broker_name=broker_name,
                )
                if locked:
                    continue
                rows.append({
                    "ticker": t,
                    "value": float(m.get("value") or 0.0),
                    "is_crypto": bool(m.get("is_crypto")),
                    "avg_cost": float(m.get("avg_cost") or 0.0),
                    "shares": float(m.get("shares") or 0.0),
                    "price": px,
                    "asset_type": asset_type,
                })
            return rows

        def _try_rotate_for(
            candidate_ticker,
            candidate_score,
            candidate_is_crypto,
            block_reason,
            *,
            need_dollars=None,
            floor_label=None,
        ):
            """Sell one eligible holding to fund candidate. Mutates held/bp/open_count. Max 1/cycle."""
            nonlocal bp, open_count, rotated_once, held, holdings_meta, holdings_by_ticker
            if advisor_gate:
                notes.append(
                    f"[{broker_name}] Rotate held until Advisor approve — "
                    "will not sell a funder for an unapproved BUY"
                )
                return False
            if rotated_once or not opportunity_swap_enabled(posture):
                return False
            try:
                need_d = float(need_dollars) if need_dollars is not None else None
            except (TypeError, ValueError):
                need_d = None
            # Desk-style: when clearing a broker min floor, announce before the sell
            if need_d is not None and float(bp) + 1e-9 < need_d:
                label = floor_label or (
                    "RH crypto floor" if candidate_is_crypto and "ROBIN" in str(broker_id).upper()
                    else "broker floor"
                )
                notes.append(
                    _auto_cycle.format_rotate_floor_clear_note(
                        broker_name, candidate_ticker,
                        bp=float(bp), floor=need_d, label=label,
                    )
                )
            fund = None
            with SuppressPrints():
                fund = pick_rotation_funding(
                    candidate_ticker,
                    candidate_score,
                    candidate_is_crypto,
                    _holdings_for_picker(),
                    posture=posture,
                    broker_id=broker_id,
                    block_reason=block_reason,
                    exit_roi_scale=exit_roi_scale,
                    exit_time_scale=exit_time_scale,
                    ttp_arm_scale=ttp_arm_scale,
                    need_dollars=need_d,
                    current_bp=float(bp),
                )
            if not fund:
                why = last_rotation_reject_reason() or "no eligible funding name"
                throttle_key = (
                    "rotate_floor_skip" if need_d is not None else "rotate_skip"
                )
                self._throttled_buy_skip_note(
                    notes, broker_name, throttle_key,
                    _auto_cycle.format_rotate_skip_note(broker_name, why),
                    cooldown_sec=720,
                )
                try:
                    _decision_log.emit_rotate_skip(
                        self._log_decision,
                        broker=broker_name,
                        ticker=candidate_ticker,
                        reason=why,
                        score=candidate_score,
                        posture=posture,
                        engine=getattr(self, "_cycle_task", None),
                        is_crypto=candidate_is_crypto,
                        open_count=open_count,
                        max_open=max_positions,
                        block_reason=block_reason,
                    )
                except Exception:
                    self._log_decision(
                        broker=broker_name, ticker=candidate_ticker, action="ROTATE_SKIP",
                        score=candidate_score, reason=f"rotate:{why}",
                        posture=posture, open_count=open_count, max_open=max_positions,
                        is_crypto=candidate_is_crypto,
                    )
                return False
            ft = fund.get("ticker") or ""
            ftu = str(ft).upper()
            meta = holdings_by_ticker.get(ftu) or {}
            shares = float(meta.get("shares") or fund.get("shares") or 0.0)
            price = float(fund.get("price") or 0.0)
            if price <= 0:
                try:
                    price = float(broker.get_live_price(ft) if broker else 0.0) or 0.0
                except Exception:
                    price = 0.0
            asset_type = fund.get("asset_type") or (
                "cryptocurrency" if fund.get("is_crypto") else "stock"
            )
            notes.append(
                _auto_cycle.format_rotate_sell_note(
                    broker_name, ft, candidate_ticker,
                    roi=float(fund.get("roi") or 0),
                    fund_score=float(fund.get("score") or 0),
                    candidate_score=float(candidate_score or 0),
                    reason=str(fund.get("reason") or ""),
                )
            )
            try:
                mark_opportunity_swap_exit(broker_id, ft)
            except Exception:
                pass
            # Journal via execute_sell — pass reason through thread-local on status path
            self._pending_journal_meta = {
                "reason": "ROTATE",
                "rotate_for": candidate_ticker,
                "score": candidate_score,
            }
            try:
                status = self.execute_sell_order(
                    ft, asset_type, price, shares,
                    offset, use_ext,
                    market_hours=market_hours, allow_fractional=allow_fractional,
                    sell_all=True,
                )
            finally:
                self._pending_journal_meta = None
            ok = "Fail" not in str(status) and "Skipped" not in str(status)
            fills.append({
                "ticker": ft,
                "status": status,
                "spent": 0.0,
                "ok": ok,
                "table_row": None,
                "scale_in": False,
                "rotate_sell": True,
                "rotate_for": candidate_ticker,
            })
            if not ok:
                notes.append(
                    _auto_cycle.format_rotate_sell_failed_note(broker_name, ft, status)
                )
                return False
            rotated_once = True
            try:
                record_rotation(broker_id)
            except Exception:
                pass
            proceeds = float(meta.get("value") or fund.get("value") or 0.0)
            if proceeds <= 0 and price > 0 and shares > 0:
                proceeds = price * shares
            bp = max(0.0, float(bp) + proceeds)
            held.discard(ftu)
            open_count = max(0, int(open_count) - 1)
            holdings_by_ticker.pop(ftu, None)
            holdings_meta[:] = [
                h for h in holdings_meta
                if str(h.get("ticker") or "").upper() != ftu
            ]
            notes.append(
                _auto_cycle.format_rotate_freed_note(
                    broker_name, ft, format_currency(proceeds), format_currency(bp),
                )
            )
            # If we rotated specifically to clear a floor and still short, don't pretend success
            if need_d is not None and float(bp) + 1e-9 < need_d:
                notes.append(
                    _auto_cycle.format_rotate_skip_note(
                        broker_name,
                        f"after rotate BP {format_currency(bp)} still below "
                        f"{floor_label or 'broker floor'} ${need_d:.2f}",
                    )
                )
                return False
            return True

        for c in ranked:
            ticker = c.get("ticker") or ""
            asset_type = c.get("asset_type") or ""
            price = float(c.get("price") or 0.0)
            is_crypto = "crypto" in str(asset_type).lower() or ticker.upper() in KNOWN_CRYPTOS
            tu = ticker.upper()
            is_held = tu in held
            if buys_done >= max_buys:
                notes.append(f"[{broker_name}] Buy cap reached ({max_buys}/cycle) — stopping this pulse")
                break

            # Fresh live price — reject missing/stale quotes (no buy on blind data)
            live = 0.0
            try:
                live = float(broker.get_live_price(ticker) if broker else 0.0) or 0.0
            except Exception:
                live = 0.0
            if live <= 0:
                notes.append(f"[{broker_name}] Skipped [{ticker}]: missing/stale live price")
                execute_skips.append(f"{ticker}: missing/stale live price")
                continue
            price = live

            cand_score = float(c.get("score") or 0.0)

            # Overnight / late session: RH blocks fractional equity buys — preflight early
            # (before regime) so we don't burn a cycle on regime-ok names we can't size.
            if (
                broker_name == "Robinhood"
                and (not is_crypto)
                and (not allow_fractional)
                and price > 0
            ):
                afford_whole = int(float(bp) / float(price)) if price > 0 else 0
                if afford_whole < 1:
                    reason = (
                        "overnight/late session — RH blocks fractional equity buys; "
                        f"BP {format_currency(bp)} < 1 share @ {price:.2f}"
                    )
                    self._note_frac_buy_defer(
                        notes, broker_name, ticker, reason, session.get("label") or "UNKNOWN",
                    )
                    execute_skips.append(f"{ticker}: session frac buy")
                    self._log_decision(
                        broker=broker_name, ticker=ticker, action="SKIP",
                        score=cand_score, reason="session_frac_buy",
                        posture=posture, open_count=open_count, max_open=max_positions,
                        is_crypto=False, regime_ok=True,
                    )
                    continue

            # Hard regime re-check at execute (scan may have been earlier when sources agreed)
            allow_regime_override = bool(self.settings.get("allow_buys_when_regime_blocked", False))
            regime_ok, regime_why = entry_regime_ok(
                is_crypto=is_crypto, posture=posture, allow_when_blocked=allow_regime_override,
                ticker=ticker,
            )
            if not regime_ok:
                why = regime_why or "regime blocked"
                notes.append(f"[{broker_name}] Regime blocked buy [{ticker}]: {why}")
                execute_skips.append(f"{ticker}: regime ({why})")
                self._log_decision(
                    broker=broker_name, ticker=ticker, action="SKIP",
                    score=cand_score, reason=f"regime:{why}",
                    posture=posture, open_count=open_count, max_open=max_positions,
                    is_crypto=is_crypto, regime_ok=False, regime_why=why,
                )
                continue

            # FinRL hold-bias: crypto new entries (not scale-in) must clear score bar
            # before we burn a cycle on sizing / rotate (thin-ticket check after size).
            if is_crypto and tu not in held:
                other = _auto_cycle.crypto_held_on_other_broker(
                    ticker, broker_name, crypto_held_map,
                )
                if other:
                    notes.append(
                        f"[{broker_name}] Skipped [{ticker}]: {other} already holds this coin"
                    )
                    execute_skips.append(f"{ticker}: held on {other}")
                    self._log_decision(
                        broker=broker_name, ticker=ticker, action="SKIP",
                        score=cand_score, reason=f"crypto_venue:{other}",
                        posture=posture, open_count=open_count, max_open=max_positions,
                        is_crypto=True, regime_ok=True,
                    )
                    continue
                ok_ce, why_ce = crypto_new_entry_ok(
                    broker_id, ticker, score=cand_score, notional=None, skip_turbulence=True,
                    equity=equity,
                )
                if not ok_ce:
                    notes.append(f"[{broker_name}] Hold bias skip [{ticker}]: {why_ce}")
                    execute_skips.append(f"{ticker}: hold bias ({why_ce})")
                    self._log_decision(
                        broker=broker_name, ticker=ticker, action="SKIP",
                        score=cand_score, reason=f"hold_bias:{why_ce}",
                        posture=posture, open_count=open_count, max_open=max_positions,
                        is_crypto=True, regime_ok=True,
                    )
                    continue

            bought = False
            for _attempt in range(2):
                is_held = tu in held
                # max_open blocks new slots only — scale-in keeps the same ticker
                if (not is_held) and max_positions > 0 and open_count >= max_positions:
                    if _attempt == 0 and _try_rotate_for(
                        ticker, cand_score, is_crypto, "max_open"
                    ):
                        continue
                    notes.append(
                        f"[{broker_name}] Max open positions ({max_positions}) — skipping further buys"
                    )
                    execute_skips.append(f"max open ({max_positions})")
                    self._coach_tip(
                        f"{broker_name}:max_open",
                        f"{broker_name}: book full at {max_positions} names — raise Max Open in Advanced, "
                        f"switch posture, or wait for exits/rotates.",
                    )
                    self._log_decision(
                        broker=broker_name, ticker=ticker, action="SKIP",
                        score=cand_score, reason="max_open",
                        posture=posture,
                        open_count=open_count, max_open=max_positions,
                        is_crypto=is_crypto,
                    )
                    break

                scale_in = False
                scale_frac = 1.0
                existing_val = 0.0
                if is_held:
                    meta = holdings_by_ticker.get(tu) or {}
                    existing_val = float(meta.get("value") or 0.0)
                    base = float(c.get("score") or 0.0)
                    try:
                        sig = float(buy_rank_score(ticker, is_crypto=is_crypto))
                    except Exception:
                        sig = base
                    ev = evaluate_scale_in(
                        ticker, price, meta.get("avg_cost") or 0.0,
                        broker_id=broker_id, asset_type=asset_type, is_crypto=is_crypto,
                        signal_score=sig, posture=posture, settings=si_overlay,
                        existing_name_value=existing_val, portfolio_value=equity,
                    )
                    if not ev.get("allowed"):
                        self._note_scale_in_skip(
                            notes, broker_name, ticker, ev.get("reason") or "blocked",
                            score=cand_score, is_crypto=is_crypto,
                        )
                        execute_skips.append(
                            f"{ticker}: scale-in ({ev.get('reason') or 'blocked'})"
                        )
                        break
                    scale_in = True
                    scale_frac = float(ev.get("size_frac") or si_params.get("scale_in_size_frac") or 0.5)

                row_dollars, size_detail = self.calculate_order_sizing(
                    bp, asset_type, entry_price=price, equity=equity, score=c.get("score"),
                    open_count=open_count, max_open_positions=max_positions,
                    existing_name_value=existing_val if scale_in else 0.0,
                    size_frac=scale_frac if scale_in else 1.0,
                    return_detail=True, ticker=ticker,
                )
                # Fractional risk policy (RH equity): prefer whole shares for broker stops
                if (
                    broker_name == "Robinhood"
                    and (not is_crypto)
                    and row_dollars > 0
                    and price > 0
                ):
                    try:
                        import analytics as _an
                        pol = _an.apply_fractional_share_policy(
                            row_dollars,
                            price,
                            prefer_whole_shares=bool(
                                self.settings.get("prefer_whole_shares_for_stops", True)
                            ),
                            allow_fractional_ttp_only=bool(
                                self.settings.get("allow_fractional_ttp_only", True)
                            ),
                            min_dollars=float(self.settings.get("min_trade_dollars", 5.0) or 5.0),
                        )
                        if pol.get("policy") == "skip":
                            notes.append(
                                f"[{broker_name}] Frac policy skip [{ticker}]: {pol.get('note')}"
                            )
                            execute_skips.append(f"{ticker}: frac policy")
                            self._log_decision(
                                broker=broker_name, ticker=ticker, action="SKIP",
                                score=cand_score, reason="frac_policy",
                                posture=posture, open_count=open_count, max_open=max_positions,
                                is_crypto=False, regime_ok=True,
                            )
                            break
                        if float(pol.get("trade_dollars") or 0) > 0:
                            row_dollars = float(pol["trade_dollars"])
                        if pol.get("note") and pol.get("policy") != "whole_shares":
                            notes.append(f"[{broker_name}] Frac policy [{ticker}]: {pol.get('note')}")
                        self._last_frac_policy = pol
                    except Exception:
                        pass
                if (
                    broker_name in ("Robinhood", "E*TRADE")
                    and (not is_crypto)
                    and row_dollars > 0
                    and price > 0
                ):
                    proj_shares = float(row_dollars) / float(price)
                    defer_buy = _auto_cycle.equity_buy_defer_reason(
                        ticker, proj_shares, price, asset_type, session,
                        frac_ext_ineligible=getattr(self, "_frac_ext_ineligible", None),
                        known_cryptos=KNOWN_CRYPTOS,
                    )
                    if defer_buy:
                        self._note_frac_buy_defer(
                            notes, broker_name, ticker, defer_buy,
                            session.get("label") or "UNKNOWN",
                        )
                        execute_skips.append(f"{ticker}: buy defer ({defer_buy})")
                        self._log_decision(
                            broker=broker_name, ticker=ticker, action="SKIP",
                            score=cand_score, reason="equity_buy_defer",
                            posture=posture, open_count=open_count, max_open=max_positions,
                            is_crypto=False, regime_ok=True,
                        )
                        break
                if row_dollars <= 0:
                    if scale_in:
                        why = (size_detail or {}).get("skip_reason") or "size too small / name cap"
                        self._note_scale_in_skip(
                            notes, broker_name, ticker, why,
                            score=cand_score, is_crypto=is_crypto,
                        )
                        execute_skips.append(f"{ticker}: scale-in size ({why})")
                        break
                    # Desk parity: when BP is under broker min ticket, rotate to clear
                    # floor (never lower the floor). Prefer one ticket ≥ floor.
                    min_ticket = float(self.settings.get("min_trade_dollars", 5.0) or 5.0)
                    try:
                        floor = float(broker_min_notional(broker_id, is_crypto=is_crypto) or min_ticket)
                    except Exception:
                        floor = max(min_ticket, float(RH_CRYPTO_MIN_NOTIONAL))
                    floor = max(floor, min_ticket)
                    under_floor = float(bp) + 1e-9 < floor
                    floor_label = (
                        "RH crypto floor" if (
                            is_crypto and "ROBIN" in str(broker_id).upper()
                        ) else "broker floor"
                    )
                    if _attempt == 0 and _try_rotate_for(
                        ticker, cand_score, is_crypto,
                        "rh_crypto_floor" if under_floor and is_crypto else "low_bp",
                        need_dollars=floor if under_floor else None,
                        floor_label=floor_label if under_floor else None,
                    ):
                        continue
                    execute_skips.append(
                        f"buying power/risk size too low ({format_currency(bp)})"
                        + (f" · under {floor_label} ${floor:.2f}" if under_floor else "")
                    )
                    self._throttled_buy_skip_note(
                        notes, broker_name,
                        "low_bp_floor" if under_floor else "low_bp",
                        f"[{broker_name}] Skipping buys — buying power/risk size too low "
                        f"({format_currency(bp)})"
                        + (
                            f"; no rotate cleared {floor_label} ≥${floor:.2f}"
                            if under_floor else ""
                        ),
                        cooldown_sec=720,
                    )
                    self._coach_tip(
                        f"{broker_name}:low_bp",
                        f"{broker_name}: BP {format_currency(bp)} can't fund min ticket / risk size — "
                        f"rotate may free cash on Balanced/Aggressive when a stronger buy "
                        f"beats a weaker hold (never places under ${floor:.2f}).",
                        cooldown_sec=720,
                    )
                    self._log_decision(
                        broker=broker_name, ticker=ticker, action="SKIP",
                        score=cand_score,
                        reason=("under_broker_floor" if under_floor else "low_bp"),
                        posture=posture,
                        open_count=open_count, max_open=max_positions,
                        is_crypto=is_crypto, bp=bp, regime_ok=True,
                    )
                    break

                # FinRL: don't spray thin crypto tickets when edge ≪ ~2% RT
                if is_crypto and (not scale_in):
                    ok_thin, why_thin = crypto_new_entry_ok(
                        broker_id, ticker, score=cand_score, notional=row_dollars,
                        skip_turbulence=True, equity=equity,
                    )
                    if not ok_thin:
                        notes.append(f"[{broker_name}] Thin-ticket skip [{ticker}]: {why_thin}")
                        execute_skips.append(f"{ticker}: thin ticket ({why_thin})")
                        self._log_decision(
                            broker=broker_name, ticker=ticker, action="SKIP",
                            score=cand_score, reason=f"thin_ticket:{why_thin}",
                            posture=posture, open_count=open_count, max_open=max_positions,
                            is_crypto=True, regime_ok=True,
                        )
                        break

                blocked, reason = concentration_blocks_buy(
                    ticker, held, holdings_meta=holdings_meta, portfolio_value=equity,
                    proposed_dollars=row_dollars, is_crypto=is_crypto,
                    allow_held_scale_in=scale_in,
                    crypto_only_broker=crypto_only_broker,
                )
                if blocked:
                    if (
                        _attempt == 0
                        and (not scale_in)
                        and _try_rotate_for(ticker, cand_score, is_crypto, reason)
                    ):
                        continue
                    notes.append(f"[{broker_name}] Skipped [{ticker}]: concentration — {reason}")
                    execute_skips.append(f"{ticker}: concentration ({reason})")
                    self._log_decision(
                        broker=broker_name, ticker=ticker, action="SKIP",
                        score=cand_score, reason=f"concentration:{reason}",
                        posture=posture,
                        open_count=open_count, max_open=max_positions,
                        is_crypto=is_crypto,
                    )
                    break

                if scale_in:
                    notes.append(
                        f"[{broker_name}] SCALE-IN {ticker} … reason: {ev.get('reason')} "
                        f"… size ${row_dollars:.2f}"
                    )
                # Never place under broker min — clamp aim up to floor when BP allows,
                # else rotate-to-clear (handled above when row_dollars<=0).
                try:
                    floor = float(broker_min_notional(broker_id, is_crypto=is_crypto) or 5.0)
                except Exception:
                    floor = float(RH_CRYPTO_MIN_NOTIONAL)
                floor = max(floor, float(self.settings.get("min_trade_dollars", 5.0) or 5.0))
                if row_dollars > 0 and row_dollars + 1e-9 < floor:
                    if float(bp) + 1e-9 >= floor:
                        row_dollars = floor  # concentrate into one ticket ≥ floor
                    else:
                        # Sizing returned a sub-floor stub — treat as under-floor rotate case
                        if _attempt == 0 and _try_rotate_for(
                            ticker, cand_score, is_crypto, "rh_crypto_floor",
                            need_dollars=floor,
                            floor_label=(
                                "RH crypto floor" if is_crypto and "ROBIN" in str(broker_id).upper()
                                else "broker floor"
                            ),
                        ):
                            continue
                        execute_skips.append(
                            f"{ticker}: sized ${row_dollars:.2f} under floor ${floor:.2f}"
                        )
                        self._throttled_buy_skip_note(
                            notes, broker_name, "under_floor_size",
                            f"[{broker_name}] Skip [{ticker}] — will not place under "
                            f"${floor:.2f} broker floor (sized ${row_dollars:.2f}, BP {format_currency(bp)})",
                            cooldown_sec=720,
                        )
                        self._log_decision(
                            broker=broker_name, ticker=ticker, action="SKIP",
                            score=cand_score, reason="under_broker_floor",
                            posture=posture, open_count=open_count, max_open=max_positions,
                            is_crypto=is_crypto, bp=bp, regime_ok=True,
                        )
                        break

                if advisor_gate and not c.get("_advisor_approved"):
                    import advisor_queue as aq
                    engine = str(getattr(self, "_cycle_task", None) or c.get("engine") or "")
                    prop = aq.propose(
                        broker=broker_name,
                        ticker=ticker,
                        asset_type=asset_type,
                        price=float(price or 0),
                        dollars=float(row_dollars or 0),
                        score=float(cand_score or 0),
                        engine=engine,
                        reason="scale_in" if scale_in else "entry",
                    )
                    if prop:
                        notes.append(
                            f"[Advisor] Proposed BUY {ticker} ~${row_dollars:.2f} "
                            f"({engine or 'scan'}) — approve on Home or companion"
                        )
                        proposals_made += 1
                        self._log_decision(
                            broker=broker_name, ticker=ticker, action="PROPOSE",
                            score=cand_score,
                            reason=("scale_in" if scale_in else "entry"),
                            posture=posture, open_count=open_count, max_open=max_positions,
                            is_crypto=is_crypto, regime_ok=True,
                            dollars=float(row_dollars or 0),
                        )
                    if proposals_made >= max_buys:
                        break
                    bought = True
                    break

                try:
                    status, spent = self.execute_buy_order(
                        ticker, asset_type, price, row_dollars, offset, use_ext,
                        market_hours=market_hours, allow_fractional=allow_fractional,
                    )
                except Exception as e:
                    status = f"Buy execution error: {e}"
                    spent = 0.0
                # If broker rejected for crypto floor and we haven't rotated yet, free BP once
                if (
                    (not scale_in)
                    and _attempt == 0
                    and (not rotated_once)
                    and "Below RH crypto floor" in str(status)
                ):
                    floor = float(RH_CRYPTO_MIN_NOTIONAL)
                    if _try_rotate_for(
                        ticker, cand_score, is_crypto, "rh_crypto_floor",
                        need_dollars=floor,
                        floor_label="RH crypto floor",
                    ):
                        continue
                ok = "Fail" not in status and "Skipped" not in status
                self._log_decision(
                    broker=broker_name, ticker=ticker,
                    action="BUY" if ok else "BUY_FAIL",
                    score=cand_score,
                    reason=("scale_in" if scale_in else "entry") + (f":{status}" if not ok else ""),
                    posture=posture,
                    dollars=float(spent or row_dollars or 0),
                    open_count=open_count, max_open=max_positions,
                    is_crypto=is_crypto, regime_ok=True,
                )
                if ok:
                    if scale_in:
                        try:
                            record_scale_in(broker_id, ticker)
                        except Exception:
                            pass
                        if hasattr(self, "_si_skip_throttle"):
                            _auto_cycle.clear_scale_in_skip_throttle(
                                self._si_skip_throttle, broker_name, ticker,
                            )
                        if tu in holdings_by_ticker and spent:
                            holdings_by_ticker[tu]["value"] = float(
                                holdings_by_ticker[tu].get("value") or 0
                            ) + float(spent)
                    else:
                        held.add(tu)
                        open_count += 1
                    buys_done += 1
                    if spent:
                        bp = max(0.0, bp - float(spent))
                        holdings_meta.append({
                            "ticker": ticker,
                            "value": float(spent),
                            "is_crypto": is_crypto,
                        })
                fills.append({
                    "ticker": ticker,
                    "status": status,
                    "spent": spent,
                    "ok": ok,
                    "table_row": c.get("table_row"),
                    "scale_in": scale_in,
                })
                bought = True
                break

            if (not bought) and (not is_held) and max_positions > 0 and open_count >= max_positions:
                # Max-open stop after failed rotate — don't keep scanning more names
                if any("Max open positions" in str(n) for n in notes[-3:]):
                    break

        # Never leave "Ranked N/N" (or a lone BUY signal) as the only trail when nothing bought.
        # Throttled BP/defer notes must not silence this — use execute_skips as the why.
        if buys_done == 0 and orig_n > 0:
            _explain_no_buys_after_rank_impl(
                notes, execute_skips,
                buys_done=buys_done, orig_n=orig_n, ranked_n=len(ranked),
                broker_name=broker_name,
            )
        return {"fills": fills, "notes": notes, "buys_done": buys_done, "broker": broker_name}

    def _on_buy_batch_done(self, payload, auto_mode=False, table=None):
        payload = payload or {}
        broker = payload.get("broker") or self.cycle_broker_name
        for note in payload.get("notes") or []:
            self.log_event(note)
        for fill in payload.get("fills") or []:
            ticker = fill.get("ticker")
            status = fill.get("status") or ""
            if fill.get("ok") and not fill.get("rotate_sell"):
                self.set_lock(ticker, is_crypto=bool(
                    "crypto" in str(fill.get("asset_type") or "").lower()
                    or str(ticker or "").upper() in KNOWN_CRYPTOS
                ))
            if fill.get("rotate_sell"):
                tag = "ROTATE-SELL "
                self.log_event(f"[{broker}] Execution [{ticker}]: {tag}{status}")
                if fill.get("ok"):
                    self.send_discord_alert(
                        f"ROTATE SELL {ticker} → fund {fill.get('rotate_for') or '?'}: {status}",
                        is_trade=True,
                    )
                continue
            tag = "SCALE-IN " if fill.get("scale_in") else ""
            self.log_event(f"[{broker}] Execution [{ticker}]: {tag}{status}")
            self.send_discord_alert(f"{'SCALE-IN' if fill.get('scale_in') else 'BUY'} {ticker}: {status}", is_trade=True)
            row = fill.get("table_row")
            if table is not None and row is not None and row < table.rowCount():
                try:
                    table.setItem(int(row), 4, QTableWidgetItem(status))
                except Exception:
                    pass
        buys_done = int(payload.get("buys_done") or 0)
        rotate_ok = any(
            f.get("rotate_sell") and f.get("ok") for f in (payload.get("fills") or [])
        )
        self.refresh_recent_trades()
        notes = payload.get("notes") or []
        if any("[Advisor] Proposed" in str(n) for n in notes):
            QTimer.singleShot(0, self._refresh_advisor_card)
            try:
                self.send_discord_alert(
                    "Advisor: BUY proposal(s) pending — approve on Home or companion",
                    urgent=True,
                    prefix="Advisor",
                )
            except Exception:
                pass
        if auto_mode:
            self.refresh_account_balances()
            if buys_done > 0 or rotate_ok:
                self.manual_portfolio_reload(and_score=False, force=True)
            else:
                self.set_working_state(False)
            self.cycle_finished()
        else:
            self.set_working_state(False)
            if buys_done > 0 or rotate_ok:
                self.manual_portfolio_reload(and_score=False, force=True)
            self.refresh_account_balances()

    def _bg_execute_sell_batch(self, sell_list):
        """Place sells on a worker thread."""
        offset = self.settings.get("limit_offset_pct", 0.1) / 100.0
        session = self._sync_equity_session_state()
        use_ext = session["use_ext"]
        market_hours = session["market_hours"]
        allow_fractional = session["fractional_ok"]
        equity_open = session["equity_tradeable"]
        session_label = session.get("label") or "UNKNOWN"
        prior = self._cycle_broker
        fills = []
        notes = []
        deferred = []
        try:
            for item in sell_list or []:
                ticker = item.get("ticker")
                row_broker = item.get("broker") or self.cycle_broker_name
                self._cycle_broker = row_broker
                asset_type = item.get("type", "")
                is_crypto = "crypto" in str(asset_type).lower() or str(ticker).upper() in KNOWN_CRYPTOS
                shares = item.get("shares") or 0.0
                price = item.get("price") or 0.0
                avg_cost = item.get("avg_cost")
                try:
                    avg_cost = float(avg_cost) if avg_cost is not None else 0.0
                except (TypeError, ValueError):
                    avg_cost = 0.0
                if avg_cost <= 0:
                    avg_cost = self._avg_cost_for(row_broker, ticker)
                # Dust RH basis: prefer tracked VWAP; else leave unknown for Discord ROI
                try:
                    px_chk = float(price or 0)
                except (TypeError, ValueError):
                    px_chk = 0.0
                if (
                    avg_cost > 0
                    and px_chk > 0
                    and avg_cost < px_chk * 0.01
                ):
                    tracked = float(self._avg_cost_for(row_broker, ticker) or 0.0)
                    if tracked > 0 and tracked >= px_chk * 0.01:
                        avg_cost = tracked
                    else:
                        avg_cost = 0.0

                if row_broker == "Robinhood" and not is_crypto:
                    if not equity_open:
                        self._note_deferred_sell(
                            row_broker, ticker, "equity markets closed", session_label, notes
                        )
                        deferred.append(str(ticker).upper())
                        continue
                    defer = self._rh_equity_sell_defer_reason(
                        ticker, shares, price, asset_type, session
                    )
                    if defer:
                        self._note_deferred_sell(row_broker, ticker, defer, session_label, notes)
                        deferred.append(str(ticker).upper())
                        continue

                # Hopeless sell backoff (e.g. BONK Fail / dust) — don't hammer every portfolio cycle
                if self._sell_fail_should_skip(row_broker, ticker):
                    deferred.append(str(ticker).upper())
                    continue

                status = self.execute_sell_order(
                    ticker, asset_type, price, shares,
                    offset, use_ext,
                    market_hours=market_hours, allow_fractional=allow_fractional,
                    sell_all=bool(item.get("sell_all", True)),
                    sell_reason=str(item.get("action") or item.get("reason") or ""),
                )
                self._mark_frac_ext_ineligible(ticker, status)
                st = str(status or "")
                if _auto_cycle.sell_status_should_backoff(st):
                    self._record_sell_fail_backoff(row_broker, ticker, st, notes)
                elif "Fail" not in st and "Skipped" not in st:
                    self._clear_sell_fail_backoff(row_broker, ticker)
                # If RH just told us this ticker can't frac in ext hours, don't keep retrying
                if (
                    row_broker == "Robinhood"
                    and not is_crypto
                    and float(shares or 0) < 1.0
                    and str(ticker).upper() in self._frac_ext_ineligible
                    and "Skipped" in str(status)
                ):
                    self._note_deferred_sell(
                        row_broker,
                        ticker,
                        "ticker not eligible for extended-hours fractionals (waiting for regular open)",
                        session_label,
                        notes,
                    )
                    # Still record one execution line this cycle (first rejection), then defer later
                ok = "Fail" not in status and "Skipped" not in status
                fills.append({
                    "ticker": ticker,
                    "broker": row_broker,
                    "status": status,
                    "ok": ok,
                    "skipped": "Skipped" in status,
                    "table_row": item.get("table_row"),
                    "price": price,
                    "avg_cost": avg_cost,
                    "shares": shares,
                })
        finally:
            self._cycle_broker = prior
        return {"fills": fills, "notes": notes, "deferred": deferred}

    def _on_sell_batch_done(self, payload, auto_mode=False, finish_cycle=False):
        payload = payload or {}
        for note in payload.get("notes") or []:
            self.log_event(note)
        for fill in payload.get("fills") or []:
            ticker = fill.get("ticker")
            broker = fill.get("broker") or self.cycle_broker_name
            status = fill.get("status") or ""
            if fill.get("ok"):
                self.set_lock(
                    ticker,
                    is_crypto="crypto" in str(fill.get("asset_type") or fill.get("type") or "").lower()
                    or str(ticker or "").upper() in KNOWN_CRYPTOS,
                )
                try:
                    from scoring import clear_scale_in_count
                    bid = getattr(self.brokers.get(broker), "broker_id", None) or str(broker).upper()
                    clear_scale_in_count(bid, ticker)
                except Exception:
                    pass
            self.log_event(f"[{broker}] Execution [{ticker}]: {status}")
            if not fill.get("skipped"):
                roi = None
                if fill.get("ok"):
                    roi = self._sell_roi(
                        broker, ticker, fill.get("price"), fill.get("avg_cost"),
                    )
                # Prefer net-of-fee ROI for BIG WIN gate (no fee-negative celebrations)
                win_roi = roi
                if roi is not None:
                    try:
                        from scoring import net_roi_after_fees
                        bid = getattr(self.brokers.get(broker), "broker_id", None) or str(broker).upper()
                        asset_type = fill.get("asset_type") or fill.get("type") or ""
                        net = net_roi_after_fees(roi, bid, ticker, asset_type)
                        if net is not None:
                            win_roi = net
                    except Exception:
                        pass
                if fill.get("ok") and self._is_big_win_roi(win_roi):
                    gain_pct = float(roi) * 100.0 if roi is not None else float(win_roi) * 100.0
                    net_part = ""
                    try:
                        if win_roi is not None and roi is not None and abs(win_roi - roi) > 1e-9:
                            net_part = f" net≈{float(win_roi)*100:.1f}%"
                    except Exception:
                        net_part = ""
                    dollar_part = ""
                    try:
                        shares = float(fill.get("shares") or 0)
                        px = float(fill.get("price") or 0)
                        cost = float(fill.get("avg_cost") or 0)
                        if shares > 0 and px > 0 and cost > 0:
                            dollar_part = f" ({format_currency((px - cost) * shares)})"
                    except (TypeError, ValueError):
                        dollar_part = ""
                    self.send_discord_alert(
                        f"🎉 BIG WIN SELL {ticker}: +{gain_pct:.1f}%{net_part}{dollar_part} — {status}",
                        is_trade=True,
                        urgent=True,
                    )
                else:
                    self.send_discord_alert(f"SELL {ticker}: {status}", is_trade=True)
            row = fill.get("table_row")
            if row is not None and hasattr(self, "portfolio_table") and row < self.portfolio_table.rowCount():
                try:
                    self.portfolio_table.setItem(int(row), 7, QTableWidgetItem(status))
                except Exception:
                    pass
        self.refresh_recent_trades()
        if auto_mode or finish_cycle:
            self.refresh_account_balances()
            self.manual_portfolio_reload(and_score=False, force=True)
        else:
            self.set_working_state(False)
            self.refresh_account_balances()
        if finish_cycle:
            self.cycle_finished()

    def _manual_score_table(self, table):
        items = self._gather_table_data_for_scoring(table)
        if items:
            self.set_working_state(True, "Scoring opportunities...")
            self.run_thread(self._bg_score_opportunities, lambda res: self._on_opportunities_scored(table, res), items)

    def _on_opportunities_scored(self, table, results):
        for row, price, action, asset_type, err in results:
            if row >= table.rowCount(): continue
            table.setItem(row, 2, QTableWidgetItem(format_currency(price)))
            action_item = QTableWidgetItem(action)
            self.apply_color_formatting(action_item, action)
            table.setItem(row, 3, action_item)
        self.set_working_state(False)

    def _gather_table_data_for_scoring(self, table):
        items = []
        is_portfolio = (table is self.portfolio_table)
        ticker_col = 1 if is_portfolio else 0
        for row in range(table.rowCount()):
            ticker_item = table.item(row, ticker_col)
            if not ticker_item or ticker_item.checkState() != Qt.Checked: continue
            ticker = ticker_item.text()
            asset_type = ticker_item.data(Qt.UserRole) or ""
            broker_name = ticker_item.data(Qt.UserRole + 1) or self.cycle_broker_name
            shares, cost = 0.0, 0.0
            if is_portfolio:
                try: shares = float(table.item(row, 2).text())
                except Exception: pass
                try: cost = float(table.item(row, 3).text().replace('$', '').replace(',', ''))
                except Exception: pass
            items.append((row, ticker, shares, cost, asset_type, broker_name))
        return items

    def _bg_score_portfolio(self, items):
        from scoring import (
            evaluate_holding,
            flush_state,
            posture_knobs_for_broker,
        )
        results = []
        with SuppressPrints():
            for row, ticker, shares, avg_cost, asset_type, *rest in items:
                broker_name = rest[0] if rest else self.cycle_broker_name
                broker = self.brokers.get(broker_name, self.cycle_broker)
                knobs = posture_knobs_for_broker(broker_name, self.settings)
                allow_flat_banks = bool(knobs.get("allow_flat_time_banks", False))
                price = broker.get_live_price(ticker) if broker else 0.0
                if not price or price <= 0:
                    tu = str(ticker or "").upper()
                    if tu.endswith("Q") and len(tu) >= 4:
                        msg = (
                            "HOLD (Untradeable — no quote; OTC/delisted *Q — "
                            "try Robinhood app)"
                        )
                    else:
                        msg = "HOLD (Untradeable — no quote)"
                    results.append((row, 0.0, msg, asset_type, None))
                    continue
                # Broker-aware dust: RH min qty / $1 fractional vs CB base+quote mins
                try:
                    is_dust, dust_reason = broker.position_is_dust(
                        ticker, shares, price, asset_type=asset_type
                    )
                except Exception:
                    is_dust, dust_reason = False, ""
                if is_dust:
                    results.append(
                        (row, price, f"HOLD (Dust — {dust_reason})", asset_type, None)
                    )
                    continue
                # Pre-flight RH instrument for known-dead OTC leftovers
                try:
                    if broker_name == "Robinhood" and hasattr(broker, "_rh_equity_sellable"):
                        ok_inst, _, why_inst = broker._rh_equity_sellable(ticker)
                        if not ok_inst:
                            results.append(
                                (row, price, f"HOLD (Untradeable — {why_inst})", asset_type, None)
                            )
                            continue
                except Exception:
                    pass
                action = evaluate_holding(
                    ticker, avg_cost,
                    broker_id=getattr(broker, "broker_id", None) or broker_name,
                    asset_type=asset_type,
                    live_price=price,
                    exit_roi_scale=float(knobs.get("exit_roi_scale", 1.0) or 1.0),
                    exit_time_scale=float(knobs.get("exit_time_scale", 1.0) or 1.0),
                    ttp_arm_scale=float(knobs.get("ttp_arm_scale", 1.0) or 1.0),
                    allow_flat_time_banks=allow_flat_banks,
                )
                results.append((row, price, action, asset_type, None))
        flush_state()
        return results

    def _bg_score_opportunities(self, items):
        from scoring import evaluate_crypto_opportunity, evaluate_opportunity, posture_for_broker
        results = []
        # Price/score in the cycle broker's context (E*TRADE equities use ET, not RH)
        cycle = self.cycle_broker
        cycle_id = getattr(cycle, "broker_id", None) or self.cycle_broker_name
        rh = self.brokers.get("Robinhood")
        posture = posture_for_broker(self.cycle_broker_name, self.settings)
        try:
            equity, _, _ = self.get_effective_balances(self.cycle_broker_name)
        except Exception:
            equity = 0.0
        with SuppressPrints():
            for entry in items:
                row, ticker, shares, avg_cost, asset_type = entry[:5]
                is_crypto = "crypto" in str(asset_type).lower() or ticker.upper() in KNOWN_CRYPTOS
                is_crypto_mover = "crypto mover" in str(asset_type).lower()
                is_penny = (
                    asset_type == "Penny Stock"
                    or ("mover" in str(asset_type).lower() and not is_crypto)
                )
                if is_crypto:
                    broker = cycle
                    broker_id = cycle_id
                else:
                    # Equity scanners historically used RH quotes; prefer cycle broker when it supports equities
                    if getattr(cycle, "supports_equities", False):
                        broker = cycle
                        broker_id = cycle_id
                    else:
                        broker = rh
                        broker_id = "ROBINHOOD"
                price = broker.get_live_price(ticker) if broker else 0.0
                if is_crypto:
                    action = evaluate_crypto_opportunity(
                        ticker,
                        broker_id=broker_id,
                        live_price=price,
                        posture=posture,
                        is_mover=is_crypto_mover,
                        equity=equity,
                    )
                else:
                    action = evaluate_opportunity(ticker, is_penny_stock=is_penny, broker_id=broker_id, live_price=price)
                results.append((row, price, action, asset_type, None))
        return results

    def toggle_all_rows(self, table, check_state):
        ticker_col = 1 if table is self.portfolio_table else 0
        for row in range(table.rowCount()):
            item = table.item(row, ticker_col)
            if item:
                item.setCheckState(check_state)

    def _populate_opp_table(self, table, opps):
        rows = [o for o in (opps or []) if isinstance(o, dict) and (o.get("symbol") or o.get("ticker"))]
        table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            sym = str(item.get("symbol") or item.get("ticker") or "")
            t_item = QTableWidgetItem(sym)
            t_item.setCheckState(Qt.Checked)
            t_item.setData(Qt.UserRole, item.get("type") or "")
            table.setItem(row, 0, t_item)
            table.setItem(row, 1, QTableWidgetItem(str(item.get("type") or "")))
            table.setItem(row, 2, QTableWidgetItem("Pending..."))
            table.setItem(row, 3, QTableWidgetItem("Pending..."))
            table.setItem(row, 4, QTableWidgetItem("Ready"))

    def _bg_scan_and_score(self, scan_func):
        """Discover tickers, score, and pre-rank BUY candidates in one background job."""
        from scoring import buy_rank_score_for_book, buy_rank_score, evaluate_scale_in
        from scoring import posture_for_broker, broker_has_posture_override
        opps = scan_func() or []
        if not opps:
            return [], [], [], []
        items = []
        for i, o in enumerate(opps):
            if not isinstance(o, dict):
                continue
            sym = o.get("symbol") or o.get("ticker")
            if not sym:
                continue
            items.append((i, sym, 0.0, 0.0, o.get("type", "")))
        if not items:
            return [], [], [], []
        results = self._bg_score_opportunities(items)

        broker_name = self.cycle_broker_name
        broker_id = getattr(self.brokers.get(broker_name), "broker_id", None) or str(broker_name).upper()
        posture = posture_for_broker(broker_name, self.settings)
        si_overlay = None if broker_has_posture_override(broker_name, self.settings) else self.settings
        try:
            equity, bp, _locked = self.get_effective_balances(broker_name)
        except Exception:
            equity, bp = 0.0, 0.0
        holdings = self.get_broker_holdings(broker_name) or []
        held = {
            (a.get("ticker") or "").upper()
            for a in holdings
            if isinstance(a, dict) and a.get("ticker")
        }
        broker = self.brokers.get(broker_name)
        crypto_only_broker = not bool(getattr(broker, "supports_equities", True))
        prefer_equity_rth = False
        if not crypto_only_broker:
            try:
                sess = self.get_equity_session_info()
                prefer_equity_rth = str(sess.get("label") or "") == "REGULAR"
            except Exception:
                prefer_equity_rth = False
        holdings_meta = []
        holdings_by_ticker = {}
        for a in holdings:
            if not isinstance(a, dict):
                continue
            t = a.get("ticker") or ""
            if not t:
                continue
            is_c = "crypto" in str(a.get("type") or "").lower() or str(t).upper() in KNOWN_CRYPTOS
            px = 0.0
            try:
                px = float(broker.get_live_price(t) if broker else 0.0) or 0.0
            except Exception:
                px = 0.0
            try:
                avg_cost = float(a.get("cost") or 0.0)
            except (TypeError, ValueError):
                avg_cost = 0.0
            if avg_cost <= 0:
                avg_cost = float(self._avg_cost_for(broker_name, t) or 0.0)
            meta = {
                "ticker": t,
                "value": float(a.get("shares") or 0) * px,
                "is_crypto": is_c,
                "avg_cost": avg_cost,
            }
            holdings_meta.append(meta)
            holdings_by_ticker[str(t).upper()] = meta

        buy_candidates = []
        dropped = []  # BUY signals filtered as already-held / cluster-full (not actionable)
        with SuppressPrints():
            for row, price, action, asset_type, err in results:
                action_u = str(action).upper()
                if "BUY" not in action_u or "DO NOT BUY" in action_u:
                    continue
                if row >= len(opps):
                    continue
                opp_row = opps[row]
                if not isinstance(opp_row, dict):
                    continue
                ticker = opp_row.get("symbol") or opp_row.get("ticker") or ""
                atype = asset_type or opp_row.get("type", "")
                is_crypto = "crypto" in str(atype).lower() or ticker.upper() in KNOWN_CRYPTOS
                tu = ticker.upper()
                live_px = float(price or 0.0)
                try:
                    if tu in held:
                        meta = holdings_by_ticker.get(tu) or {}
                        if live_px <= 0:
                            try:
                                live_px = float(broker.get_live_price(ticker) if broker else 0.0) or 0.0
                            except Exception:
                                live_px = float(price or 0.0)
                        base = float(buy_rank_score(ticker, is_crypto=is_crypto))
                        ev = evaluate_scale_in(
                            ticker, live_px, meta.get("avg_cost") or 0.0,
                            broker_id=broker_id, asset_type=atype, is_crypto=is_crypto,
                            signal_score=base, posture=posture, settings=si_overlay,
                            existing_name_value=meta.get("value") or 0.0,
                            portfolio_value=equity,
                        )
                        if not ev.get("allowed"):
                            dropped.append(f"{ticker} (scale-in blocked: {ev.get('reason')})")
                            continue
                        score = float(buy_rank_score_for_book(
                            ticker, is_crypto=is_crypto, held_tickers=held,
                            holdings_meta=holdings_meta, portfolio_value=equity,
                            scale_in_candidate=True,
                            crypto_only_broker=crypto_only_broker,
                            prefer_equity_rth=prefer_equity_rth,
                        ))
                        buy_candidates.append({
                            "ticker": ticker,
                            "asset_type": atype,
                            "price": live_px,
                            "score": score,
                            "table_row": row,
                            "scale_in": True,
                            "scale_in_reason": ev.get("reason"),
                        })
                        continue
                    score = float(buy_rank_score_for_book(
                        ticker, is_crypto=is_crypto, held_tickers=held,
                        holdings_meta=holdings_meta, portfolio_value=equity,
                        crypto_only_broker=crypto_only_broker,
                        prefer_equity_rth=prefer_equity_rth,
                    ))
                except Exception:
                    score = 0.0
                if score <= -500.0:
                    # cluster full — don't promote as a candidate
                    why = "already held / cluster full"
                    try:
                        from scoring import concentration_blocks_buy
                        blocked, reason = concentration_blocks_buy(
                            ticker, held, holdings_meta=holdings_meta,
                            portfolio_value=equity, is_crypto=is_crypto,
                            crypto_only_broker=crypto_only_broker,
                        )
                        if blocked and reason:
                            why = reason
                    except Exception:
                        pass
                    dropped.append(f"{ticker} ({why})")
                    continue
                buy_candidates.append({
                    "ticker": ticker,
                    "asset_type": atype,
                    "price": float(price or 0.0),
                    "score": score,
                    "table_row": row,
                    "scale_in": False,
                })
        prefer_whole = _auto_cycle.affordability_prefer_whole_shares(
            broker_id,
            prefer_equity_rth=prefer_equity_rth,
            settings=self.settings,
        )
        affordable, unaff = _auto_cycle.filter_affordable_buy_candidates(
            buy_candidates,
            buying_power=bp,
            equity=equity,
            broker_id=broker_id,
            settings=self.settings,
            prefer_whole_shares=prefer_whole,
        )
        if unaff:
            dropped.extend(unaff)
        buy_candidates = affordable
        buy_candidates.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return opps, results, buy_candidates, dropped

    def _bg_portfolio_load_and_score(self, broker_name):
        """Load one broker's holdings and score them in one job. Returns (assets, results)."""
        assets = []
        for a in self.get_broker_holdings(broker_name) or []:
            if not isinstance(a, dict) or not a.get("ticker"):
                continue
            row = dict(a)
            row['broker'] = broker_name
            assets.append(row)
        self._holdings_count_cache[broker_name] = len(assets)
        if not assets:
            return [], []
        items = [
            (i, a.get('ticker'), a.get('shares', 0.0), a.get('cost', 0.0), a.get('type', ''), broker_name)
            for i, a in enumerate(assets)
        ]
        results = self._bg_score_portfolio(items)
        # Attach live prices from scoring results for any UI paint
        for row, price, action, asset_type, err in results:
            if row < len(assets):
                assets[row]["live_price"] = float(price or 0.0)
        return assets, results

    def _apply_scored_opportunities(self, table, opps, results):
        self._populate_opp_table(table, opps)
        self._on_opportunities_scored(table, results)

    def _count_buy_signals(self, results):
        return _auto_cycle.count_buy_signals(results)

    def manual_scan_table(self, table, scan_bg_func):
        self.set_working_state(True, "Scanning...")
        table.setRowCount(0)
        self.run_thread(scan_bg_func, lambda opps: self._on_scan_complete(table, opps))

    def _on_scan_complete(self, table, opps):
        self._populate_opp_table(table, opps)
        self.set_working_state(False)

    def run_portfolio_cycle(self):
        """Load+score one broker's holdings in one job; execute sells separately."""
        broker = self.cycle_broker_name
        if self._is_broker_auto_trading(broker):
            self._set_engine_banner(f"🤖 📊 [{broker}] PORTFOLIO — load + score...", "#00897B")
        self.set_working_state(True, f"Scoring {broker} holdings...")
        self.run_cycle_thread(
            self._bg_portfolio_load_and_score,
            self._port_on_scored,
            broker,
        )

    def _port_on_scored(self, payload):
        broker = self.cycle_broker_name
        assets, results = payload if payload else ([], [])
        if not assets:
            self.log_event(f"[AUTO] [{broker}] Portfolio empty — no sells this cycle")
            self.set_working_state(False)
            self.cycle_finished()
            return

        for row, price, action, asset_type, err in results:
            if row >= len(assets):
                continue
            a = assets[row]
            if not isinstance(a, dict):
                continue
            ticker = a.get("ticker") or ""
            if ticker:
                self._patch_portfolio_row_action(broker, ticker, price, action)

        sell_list = _auto_cycle.portfolio_sells_from_scored(assets, results, broker)
        raw_sell_n = sum(
            1 for _row, _px, act, _at, _e in results
            if "SELL" in str(act or "").upper()
        )
        sell_list, locked_dropped = _auto_cycle.drop_locked_portfolio_sells(sell_list, assets)
        if locked_dropped:
            skip_key = f"{broker}:locked"
            skip_store = getattr(self, "_port_locked_skip_logged", None)
            if skip_store is None:
                self._port_locked_skip_logged = {}
                skip_store = self._port_locked_skip_logged
            tick_sig = ",".join(sorted(set(locked_dropped)))
            if skip_store.get(skip_key) != tick_sig:
                skip_store[skip_key] = tick_sig
                if sell_list:
                    self.log_event(
                        f"[AUTO] [{broker}] PORTFOLIO — skipped locked/untradeable: "
                        f"{tick_sig.replace(',', ', ')}"
                    )
                else:
                    self.log_event(
                        f"[AUTO] [{broker}] PORTFOLIO — {raw_sell_n} sell signal(s) on "
                        f"locked capital only ({tick_sig.replace(',', ', ')}); not executing"
                    )

        sell_n = len(sell_list) if sell_list else raw_sell_n
        session = self._sync_equity_session_state()

        def _note_defer(broker, tick, defer, label, notes_tmp):
            self._note_deferred_sell(broker, tick, defer, label, notes_tmp)

        actionable, deferred, notes_tmp = _auto_cycle.partition_portfolio_sells(
            sell_list,
            broker_name=broker,
            session=session,
            sell_fail_should_skip=self._sell_fail_should_skip,
            rh_defer_reason_fn=self._rh_equity_sell_defer_reason,
            note_deferred_fn=_note_defer,
        )

        trail = _auto_cycle.format_portfolio_scored_note(
            broker, sell_n,
            actionable_n=len(actionable),
            deferred=deferred,
            first_defer_this_session=bool(notes_tmp),
        )
        if trail:
            self.log_event(trail)
            for n in notes_tmp:
                self.log_event(n)

        if actionable and self._is_broker_auto_trading():
            self._set_engine_banner(f"🤖 💰 [{broker}] PORTFOLIO — executing...", "#00897B")
            self.run_cycle_thread(
                self._bg_execute_sell_batch,
                lambda res: self._on_sell_batch_done(res, auto_mode=True, finish_cycle=True),
                actionable,
            )
        else:
            self.set_working_state(False)
            self.cycle_finished()

    def _execute_sell_list(self, sell_list, auto_mode=False):
        """Compatibility wrapper — routes sells through the non-blocking batch path."""
        if not sell_list:
            return
        self.set_working_state(True, "Executing sells…")
        runner = self.run_cycle_thread if auto_mode else self.run_thread
        runner(
            self._bg_execute_sell_batch,
            lambda res: self._on_sell_batch_done(res, auto_mode=auto_mode, finish_cycle=auto_mode),
            sell_list,
        )

    def run_crypto_cycle(self):
        broker = self.cycle_broker_name
        if self._is_broker_auto_trading(broker):
            self._set_engine_banner(f"🤖 🪙 [{broker}] CRYPTO — scan + score...", "#FFB300")
        self.set_working_state(True, f"Crypto scan+score ({broker})...")
        self.crypto_table.setRowCount(0)
        self.run_cycle_thread(
            self._bg_scan_and_score,
            self._crypto_on_scored,
            self._bg_scan_crypto,
        )

    def _maybe_regime_idle_coach(self, broker, engine, raw_buys):
        """One [COACH] per ~1hr of zero raw BUY signals when regime blocks new entries."""
        if not self._is_broker_auto_trading(broker):
            return
        now = time.time()
        store = getattr(self, "_scan_idle_since", None)
        if store is None:
            self._scan_idle_since = {}
            store = self._scan_idle_since
        key = f"{broker}:{engine}"
        if int(raw_buys or 0) > 0:
            store[key] = now
            return
        since = store.get(key)
        if since is None:
            store[key] = now
            return
        idle_sec = now - float(since)
        if idle_sec < _auto_cycle.REGIME_IDLE_COACH_SEC:
            return
        is_crypto = str(engine or "").upper() in ("CRYPTO",)
        regime_ok = True
        regime_reason = ""
        dd_paused = False
        try:
            from scoring import market_regime_ok, get_drawdown_status
            regime_ok, regime_reason = market_regime_ok(is_crypto=is_crypto)
            bid = {
                "Robinhood": "ROBINHOOD",
                "Coinbase": "COINBASE",
                "E*TRADE": "ETRADE",
            }.get(broker, broker)
            dd_paused = bool(get_drawdown_status(bid).get("paused"))
        except Exception:
            regime_ok = True
        if regime_ok and not dd_paused:
            return
        tip_key, tip = _auto_cycle.regime_idle_coach_tip(
            broker, engine,
            idle_sec=idle_sec,
            regime_reason=regime_reason if not dd_paused else "",
            dd_paused=dd_paused,
        )
        self._coach_tip(tip_key, tip, cooldown_sec=1800)

    def _log_scan_buy_outcome(self, engine, results, buy_candidates, dropped=None):
        """
        Log BUY score count, and when signals exist but none are actionable for the book,
        explain the silent drop (scale-in / held / cluster) so BP-idle cycles are clear.
        Drop lines are throttled ~once per ticker per 13 min (same spirit as BP throttle).
        """
        broker = self.cycle_broker_name
        raw_buys = self._count_buy_signals(results)
        self._maybe_regime_idle_coach(broker, engine, raw_buys)
        actionable = len(buy_candidates or [])
        if actionable:
            self.log_event(f"[AUTO] [{broker}] {engine} scored — {actionable} BUY signal(s)")
            self._upsert_desk_radar(engine, buy_candidates)
        else:
            self.log_event(f"[AUTO] [{broker}] {engine} scored — {raw_buys} BUY signal(s)")
        if raw_buys > 0 and actionable == 0 and self._is_broker_auto_trading():
            visible, suppressed = self._throttle_scan_drops(
                broker, engine, dropped or [], cooldown_sec=780,
            )
            line = _auto_cycle.format_no_actionable_scan_note(
                broker, engine, raw_buys, visible=visible, suppressed=suppressed,
            )
            if line:
                self.log_event(line)
                self._coach_tip_for_scan_drops(broker, engine, dropped or visible)
            elif suppressed:
                # All drop lines still in cooldown — stay quiet (skips still applied)
                pass
            else:
                self._coach_tip_for_scan_drops(broker, engine, dropped or [])

    def _upsert_desk_radar(self, engine, buy_candidates):
        try:
            import desk_radar
            items = desk_radar.upsert_candidates(
                buy_candidates,
                engine=engine,
                broker=self.cycle_broker_name,
            )
            self._last_desk_radar = items
            self._refresh_home_desk_radar()
        except Exception:
            pass

    def _refresh_home_desk_radar(self):
        lbl = getattr(self, "home_radar_lbl", None)
        if lbl is None:
            return
        try:
            import desk_radar
            from scoring import posture_for_broker
            top = desk_radar.top_radar(6)
            if not top:
                lbl.setText("No scored signals yet — run a CRYPTO / BREAKOUT / CORE cycle.")
                if hasattr(self, "home_capital_lbl"):
                    self.home_capital_lbl.setText("Capital planner: —")
                return
            bits = []
            for it in top:
                age_m = max(0, int((time.time() - float(it.get("ts") or time.time())) // 60))
                bits.append(
                    f"{it.get('ticker')} {it.get('engine')} "
                    f"{float(it.get('score') or 0):.0f} ({age_m}m)"
                )
            lbl.setText(" · ".join(bits))
            if hasattr(self, "home_capital_lbl"):
                lead = top[0]
                broker = str(lead.get("broker") or self.cycle_broker_name or "Robinhood")
                tick = str(lead.get("ticker") or "")
                try:
                    eq, bp, _ = self.get_effective_balances(broker)
                    holdings = self.get_broker_holdings(broker) or []
                    posture = posture_for_broker(broker, self.settings)
                    bid = {"Robinhood": "ROBINHOOD", "Coinbase": "COINBASE", "E*TRADE": "ETRADE"}.get(
                        broker, broker.upper()
                    )
                    cap = _auto_cycle.capital_planner_snapshot(
                        broker=broker,
                        ticker=tick,
                        price=float(lead.get("price") or 0),
                        score=float(lead.get("score") or 0),
                        equity=float(eq or 0),
                        buying_power=float(bp or 0),
                        holdings=holdings,
                        posture=posture,
                        broker_id=bid,
                        settings=self.settings,
                    )
                    self._last_capital_planner = cap
                    self.home_capital_lbl.setText(
                        _auto_cycle.format_capital_planner_label(cap, money_fn=format_currency)
                    )
                except Exception as e:
                    self.home_capital_lbl.setText(f"Capital planner: unavailable ({e})")
        except Exception as e:
            lbl.setText(f"Radar unavailable ({e})")

    def _crypto_on_scored(self, payload):
        opps, results, buy_candidates, dropped = self._unpack_scan_payload(payload)
        self._apply_scored_opportunities(self.crypto_table, opps, results)
        if buy_candidates:
            affordable, unaff = self._affordable_scan_buy_candidates(buy_candidates)
            if unaff:
                dropped = list(dropped or []) + list(unaff)
            buy_candidates = affordable
        self._log_scan_buy_outcome("CRYPTO", results, buy_candidates, dropped)
        if buy_candidates and self._is_broker_auto_trading():
            self.log_event(
                _auto_cycle.format_ranked_buys_note(self.cycle_broker_name, buy_candidates)
            )
            self._try_execute_scan_buys(
                self.crypto_table, buy_candidates,
                engine_label="CRYPTO",
                banner_text=f"🤖 💰 [{self.cycle_broker_name}] CRYPTO — executing...",
                banner_color="#FFB300",
            )
        else:
            self.set_working_state(False)
            self.cycle_finished()

    def run_penny_cycle(self):
        broker = self.cycle_broker_name
        if self._is_broker_auto_trading(broker):
            self._set_engine_banner(f"🤖 🚀 [{broker}] BREAKOUT — scan + score...", "#E53935")
        self.set_working_state(True, "Breakout scan+score...")
        self.penny_table.setRowCount(0)
        self.run_cycle_thread(
            self._bg_scan_and_score,
            self._penny_on_scored,
            self._bg_scan_penny,
        )

    def _penny_on_scored(self, payload):
        opps, results, buy_candidates, dropped = self._unpack_scan_payload(payload)
        self._apply_scored_opportunities(self.penny_table, opps, results)
        if buy_candidates:
            affordable, unaff = self._affordable_scan_buy_candidates(buy_candidates)
            if unaff:
                dropped = list(dropped or []) + list(unaff)
            buy_candidates = affordable
        self._log_scan_buy_outcome("BREAKOUT", results, buy_candidates, dropped)
        if buy_candidates and self._is_broker_auto_trading():
            self.log_event(
                _auto_cycle.format_ranked_buys_note(self.cycle_broker_name, buy_candidates)
            )
            self._try_execute_scan_buys(
                self.penny_table, buy_candidates,
                engine_label="BREAKOUT",
                banner_text=f"🤖 💰 [{self.cycle_broker_name}] BREAKOUT — executing...",
                banner_color="#E53935",
            )
        else:
            self.set_working_state(False)
            self.cycle_finished()

    def run_core_cycle(self):
        broker = self.cycle_broker_name
        if self._is_broker_auto_trading(broker):
            self._set_engine_banner(f"[{broker}] CORE — scan + score...", UI_ACCENT)
        self.set_working_state(True, "Core scan+score...")
        self.core_table.setRowCount(0)
        self.run_cycle_thread(
            self._bg_scan_and_score,
            self._core_on_scored,
            self._bg_scan_core,
        )

    def _core_on_scored(self, payload):
        opps, results, buy_candidates, dropped = self._unpack_scan_payload(payload)
        self._apply_scored_opportunities(self.core_table, opps, results)
        if buy_candidates:
            affordable, unaff = self._affordable_scan_buy_candidates(buy_candidates)
            if unaff:
                dropped = list(dropped or []) + list(unaff)
            buy_candidates = affordable
        self._log_scan_buy_outcome("CORE", results, buy_candidates, dropped)
        if buy_candidates and self._is_broker_auto_trading():
            self.log_event(
                _auto_cycle.format_ranked_buys_note(self.cycle_broker_name, buy_candidates)
            )
            self._try_execute_scan_buys(
                self.core_table, buy_candidates,
                engine_label="CORE",
                banner_text=f"[{self.cycle_broker_name}] CORE — executing...",
                banner_color=UI_ACCENT,
            )
        else:
            self.set_working_state(False)
            self.cycle_finished()

    def _unpack_scan_payload(self, payload):
        """Normalize (opps, results[, buy_candidates[, dropped]]) from bg scan jobs."""
        return _auto_cycle.unpack_scan_payload(payload)

    def _bg_scan_crypto(self):
        movers = []
        # Coinbase Advanced: rank USD products by 24h % change
        cb = self.brokers.get("Coinbase")
        if cb and getattr(cb, "is_connected", False) and getattr(cb, "client", None):
            try:
                payload = cb._cb_payload(cb._cb_call(cb.client.get_products, product_type="SPOT", limit=250))
                movers.extend(_auto_cycle.extract_coinbase_usd_movers(payload, limit=8))
            except Exception:
                pass
        # Robinhood: top crypto movers when available
        rh = self.brokers.get("Robinhood")
        if rh and getattr(rh, "is_connected", False):
            try:
                import robin_stocks.robinhood as r
                for item in (r.markets.get_top_100() or [])[:15]:
                    sym = str(item.get("symbol") or item.get("ticker") or "").upper()
                    # Equity list — skip; RH crypto movers API varies by version
                    if not sym:
                        continue
                # Prefer dedicated crypto movers endpoint when present
                fn = getattr(getattr(r, "crypto", None), "get_crypto_currency_pairs", None)
                if callable(fn):
                    for pair in (fn() or [])[:40]:
                        if not isinstance(pair, dict):
                            continue
                        sym = str(pair.get("asset_currency", {}).get("code") or "").upper()
                        if sym and sym not in _auto_cycle.DEFAULT_CRYPTO_TICKERS:
                            movers.append(sym)
            except Exception:
                pass
        universe = _auto_cycle.merge_crypto_scan_universe(
            list(_auto_cycle.DEFAULT_CRYPTO_TICKERS),
            movers,
            max_movers=8,
        )
        for row in universe:
            sym = str(row.get("symbol") or "").upper()
            if sym:
                KNOWN_CRYPTOS.add(sym)
        return universe

    def _bg_scan_penny(self):
        finviz_syms, rh_syms, yahoo_syms = [], [], []
        Overview = _get_overview_class()
        if Overview is not None:
            try:
                fs = Overview()
                fs.set_filter(filters_dict={'Price': 'Under $5', 'Current Volume': 'Over 2M'})
                df = fs.screener_view()
                if df is not None and not df.empty and 'Ticker' in df.columns:
                    for t in df['Ticker'].head(10).tolist():
                        finviz_syms.append(str(t).upper().strip())
            except Exception:
                pass
        if self.brokers["Robinhood"].is_connected:
            try:
                import robin_stocks.robinhood as rh
                for item in rh.markets.get_top_100()[:12]:
                    sym = item.get('symbol') or item.get('ticker')
                    if sym:
                        rh_syms.append(str(sym).upper().strip())
            except Exception:
                pass
        # Yahoo day gainers (equities) — soft third source
        try:
            import yfinance as yf
            # curated high-beta names as gainers proxy when screener APIs fail
            probe = ["SOUN", "PLUG", "RKLB", "SMCI", "IONQ", "OKLO", "ACHR", "JOBY"]
            for sym in probe:
                try:
                    h = yf.Ticker(sym).history(period="5d", interval="1d")
                    if h is None or h.empty or len(h) < 2:
                        continue
                    chg = float(h["Close"].iloc[-1]) / float(h["Close"].iloc[-2]) - 1.0
                    if chg >= 0.03:
                        yahoo_syms.append(sym)
                except Exception:
                    continue
        except Exception:
            pass
        import desk_radar
        discovered = desk_radar.merge_breakout_universe(
            finviz_syms, rh_syms, yahoo_syms, max_total=16,
        )
        if not discovered:
            for s in ["SNDL", "SOUN", "PLUG", "TSLA", "AMD"]:
                discovered.append({'symbol': s, 'type': 'Penny Stock'})
        return discovered

    def _bg_scan_core(self):
        # Mega-caps + liquid ETFs (index, sector, thematic, bonds/metals). Curated — not the full ETF universe.
        return [{'symbol': s, 'type': 'Stock/ETF'} for s in [
            # Mega-cap stocks
            "NVDA", "AAPL", "TSLA", "AMD", "MSFT", "META", "AMZN", "GOOGL",
            # Broad / leveraged index ETFs
            "SPY", "QQQ", "IWM", "DIA", "TQQQ", "SQQQ", "SOXL", "SOXS",
            # Sector / thematic ETFs
            "XLK", "XLF", "XLE", "XLV", "SMH", "ARKK", "IBIT",
            # Bonds / metals / volatility hedges
            "TLT", "HYG", "GLD", "SLV", "UVXY",
        ]]

    def toggle_paper_mode(self):
        self.paper_mode = not self.paper_mode
        self.settings["paper_mode"] = self.paper_mode
        save_settings(self.settings)
        self.paper_mode_btn.setText("Mode: PAPER" if self.paper_mode else "Mode: LIVE")
        self.paper_mode_btn.setStyleSheet(
            top_bar_btn_style("#E65100") if self.paper_mode else top_bar_btn_style("#1B5E20")
        )

    def _toggle_advanced_settings(self):
        """Show/hide Advanced knobs under Risk Posture."""
        group = getattr(self, "advanced_settings_group", None)
        btn = getattr(self, "advanced_settings_btn", None)
        if group is None:
            return
        show = not group.isVisible()
        group.setVisible(show)
        if btn is not None:
            btn.setText("Hide Advanced" if show else "Show Advanced…")

    def _toggle_discord_settings(self):
        group = getattr(self, "discord_settings_group", None)
        btn = getattr(self, "discord_settings_btn", None)
        if group is None:
            return
        show = not group.isVisible()
        group.setVisible(show)
        if btn is not None:
            btn.setText("Hide Discord" if show else "Show Discord…")

    def _toggle_advisor_settings(self):
        group = getattr(self, "advisor_settings_group", None)
        btn = getattr(self, "advisor_settings_btn", None)
        if group is None:
            return
        show = not group.isVisible()
        group.setVisible(show)
        if btn is not None:
            btn.setText("Hide Desk Advisor" if show else "Show Desk Advisor…")

    def _update_discord_settings_summary(self):
        lbl = getattr(self, "discord_settings_summary_lbl", None)
        if lbl is None:
            return
        wh = bool(str(self.settings.get("discord_webhook") or "").strip())
        lvl = self.settings.get("discord_alert_level", "All Alerts (Every Trade & Heartbeat)")
        lbl.setText(_auto_cycle.format_discord_settings_summary(webhook_set=wh, level=str(lvl)))

    def _update_advisor_settings_summary(self):
        lbl = getattr(self, "advisor_settings_summary_lbl", None)
        if lbl is None:
            return
        advisor_on = bool(self.settings.get("advisor_ask_before_apply", True))
        remote_on = bool(self.settings.get("monitor_controls_enabled", False))
        lbl.setText(_auto_cycle.format_advisor_settings_summary(
            advisor_on=advisor_on, remote_on=remote_on,
        ))

    def _try_execute_scan_buys(self, table, buy_candidates, *, engine_label, banner_text, banner_color):
        """Gate scan BUY execution — idle BP, drawdown, and safe handoff to buy batch."""
        broker = self.cycle_broker_name
        idle = self._buy_engines_idle_reason(broker)
        if idle:
            self.log_event(
                f"[{broker}] {idle} — ranked {len(buy_candidates or [])} signal(s) not executed"
            )
            self.set_working_state(False)
            self.cycle_finished()
            return
        try:
            from scoring import _drawdown_block
            broker_id = getattr(self.brokers.get(broker), "broker_id", None) or str(broker).upper()
            dd_ok, dd_why = _drawdown_block(broker_id)
            if not dd_ok:
                self.log_event(f"[{broker}] {dd_why} — ranked buys not executed")
                self.set_working_state(False)
                self.cycle_finished()
                return
        except Exception:
            pass
        self._set_engine_banner(banner_text, banner_color)
        self.execute_scanner_trades(table, auto_mode=True, buy_candidates=buy_candidates)

    def _bg_buy_batch_safe(self, candidates, rank=False, advisor_gate=False):
        try:
            return self._bg_buy_batch(candidates, rank=rank, advisor_gate=advisor_gate)
        except Exception as e:
            import traceback
            broker = getattr(self, "cycle_broker_name", "?")
            tb = traceback.format_exc()
            return {
                "fills": [],
                "notes": [
                    f"[{broker}] Buy batch error: {e}",
                    tb[:1200],
                ],
                "buys_done": 0,
                "broker": broker,
            }

    def _on_risk_posture_changed(self, _index=None):
        """Selecting a posture retunes related knobs; Advanced can still fine-tune."""
        from scoring import get_risk_posture_profile, normalize_risk_posture
        if not hasattr(self, "risk_posture_combo"):
            return
        key = self.risk_posture_combo.currentData()
        if key is None:
            key = normalize_risk_posture(self.risk_posture_combo.currentText())
        else:
            key = normalize_risk_posture(key)
        prof = get_risk_posture_profile(key)
        if hasattr(self, "risk_posture_hint"):
            self.risk_posture_hint.setText(prof.get("hint", ""))
        if hasattr(self, "bp_util_spin"):
            self.bp_util_spin.setValue(float(prof["target_bp_utilization_pct"]))
        if hasattr(self, "sizing_focus_spin"):
            self.sizing_focus_spin.setValue(int(prof["sizing_focus_slots"]))
        if hasattr(self, "max_pos_spin"):
            self.max_pos_spin.setValue(int(prof["max_open_positions"]))
        if hasattr(self, "max_buys_spin"):
            self.max_buys_spin.setValue(int(prof["max_buys_per_cycle"]))
        if hasattr(self, "name_cap_spin"):
            self.name_cap_spin.setValue(float(prof["max_single_name_equity_pct"]))
        if hasattr(self, "conviction_mult_spin"):
            self.conviction_mult_spin.setValue(float(prof["conviction_alloc_mult_max"]))
        if hasattr(self, "risk_pct_spin") and "risk_pct_per_trade" in prof:
            self.risk_pct_spin.setValue(float(prof["risk_pct_per_trade"]))
            self.settings["risk_pct_per_trade"] = float(prof["risk_pct_per_trade"])
        if hasattr(self, "max_open_risk_spin") and "max_open_risk_pct" in prof:
            self.max_open_risk_spin.setValue(float(prof["max_open_risk_pct"]))
            self.settings["max_open_risk_pct"] = float(prof["max_open_risk_pct"])
        self.settings["exit_roi_scale"] = float(prof["exit_roi_scale"])
        self.settings["exit_time_scale"] = float(prof["exit_time_scale"])
        self.settings["ttp_arm_scale"] = float(prof["ttp_arm_scale"])
        self.settings["allow_flat_time_banks"] = bool(prof.get("allow_flat_time_banks", False))
        self.settings["risk_posture"] = key
        self.settings["advanced_scale_in_override"] = False
        if hasattr(self, "allow_scale_in_chk"):
            self.allow_scale_in_chk.setChecked(bool(prof.get("allow_scale_in", False)))
        self.settings["allow_scale_in"] = bool(prof.get("allow_scale_in", False))
        for k in (
            "scale_in_size_frac",
            "scale_in_max_adds",
            "scale_in_roi_min",
            "scale_in_roi_max",
            "scale_in_near_pct",
            "scale_in_min_score",
            "day_dd_pause_pct",
            "peak_dd_pause_pct",
            "dd_pause_minutes",
        ):
            if k in prof:
                self.settings[k] = prof[k]
        if hasattr(self, "day_dd_spin"):
            self.day_dd_spin.setValue(float(prof.get("day_dd_pause_pct", 0.05)) * 100.0)
        if hasattr(self, "peak_dd_spin"):
            self.peak_dd_spin.setValue(float(prof.get("peak_dd_pause_pct", 0.12)) * 100.0)
        if hasattr(self, "dd_pause_spin"):
            self.dd_pause_spin.setValue(int(prof.get("dd_pause_minutes", 45)))
        # Seed $ daily loss from equity × posture pct when we have a trusted book
        pct = float(prof.get("daily_loss_limit_equity_pct", 0.05) or 0.05)
        eq = 0.0
        for v in (getattr(self, "_last_trusted_equity", {}) or {}).values():
            try:
                eq += float(v or 0)
            except (TypeError, ValueError):
                pass
        if eq > 1.0 and hasattr(self, "loss_spin"):
            self.loss_spin.setValue(round(eq * pct, 2))
            self.settings["daily_loss_limit"] = round(eq * pct, 2)

    def toggle_dark_mode(self):
        self.dark_mode = not self.dark_mode
        self.settings["dark_mode"] = self.dark_mode
        save_settings(self.settings)
        self.dark_mode_btn.setText("Light" if self.dark_mode else "Dark")
        self.apply_theme()
        self._style_home_cards()
        self._reset_autotrader_banner_style()
        self.update_market_status()
        self._update_discord_webhook_status()
        if hasattr(self, "_last_cluster_heat") and hasattr(self, "home_cluster_host"):
            self._render_cluster_heat(self._last_cluster_heat)

    def save_custom_settings(self):
        # discord_webhook is owned by the Webhook… dialog (writes on its Save)
        self.settings["discord_alert_level"] = self.discord_lvl_combo.currentText()
        self.settings["discord_heartbeat_schedule"] = self.discord_hb_combo.currentText()
        self.settings["discord_big_win_roi_pct"] = self.discord_big_win_spin.value()
        self.settings["monitor_enabled"] = self.monitor_enabled_chk.isChecked()
        self.settings["monitor_port"] = int(self.monitor_port_spin.value())
        if hasattr(self, "monitor_bind_combo"):
            self.settings["monitor_host"] = (
                self.monitor_bind_combo.currentData() or "127.0.0.1"
            )
        else:
            self.settings["monitor_host"] = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
        self.settings["monitor_user"] = self.monitor_user_input.text().strip()
        self.settings["monitor_pass"] = self.monitor_pass_input.text()
        if hasattr(self, "monitor_https_chk"):
            self.settings["monitor_https"] = bool(self.monitor_https_chk.isChecked())
        else:
            self.settings["monitor_https"] = bool(self.settings.get("monitor_https", True))
        remote = self.settings["monitor_host"] not in ("127.0.0.1", "localhost", "::1")
        if remote:
            # LAN / port-forward always uses HTTPS
            self.settings["monitor_https"] = True
            if hasattr(self, "monitor_https_chk"):
                self.monitor_https_chk.setChecked(True)
        controls_wanted = bool(
            self.monitor_controls_chk.isChecked()
            if hasattr(self, "monitor_controls_chk")
            else self.settings.get("monitor_controls_enabled", False)
        )
        if (remote or controls_wanted or self.settings.get("monitor_https")) and not self.settings["monitor_user"]:
            QMessageBox.warning(
                self,
                "Web Monitor",
                "LAN/away, HTTPS, and Companion Controls require a User and Password. "
                "Set them before enabling remote access.",
            )
            if remote:
                # Fall back to localhost rather than opening an unauthenticated remote port
                self.settings["monitor_host"] = "127.0.0.1"
                if hasattr(self, "monitor_bind_combo"):
                    self.monitor_bind_combo.setCurrentIndex(0)
            controls_wanted = False
            if hasattr(self, "monitor_controls_chk"):
                self.monitor_controls_chk.setChecked(False)
        if controls_wanted and not self.settings["monitor_user"]:
            controls_wanted = False
        self.settings["monitor_controls_enabled"] = controls_wanted
        if self.settings["monitor_enabled"] and not self.settings["monitor_user"] and not remote:
            QMessageBox.information(
                self,
                "Web Monitor",
                "Tip: set a monitor User/Pass before enabling Home Wi‑Fi + away or the Android companion.",
            )
        self.settings["allocation_pct_stock"] = self.alloc_stock_spin.value()
        self.settings["allocation_pct_crypto"] = self.alloc_crypto_spin.value()
        self.settings["allocation_pct"] = self.alloc_stock_spin.value()
        self.settings["min_trade_dollars"] = self.min_dollar_spin.value()
        if hasattr(self, "risk_posture_combo"):
            key = self.risk_posture_combo.currentData()
            self.settings["risk_posture"] = str(key or "balanced").lower()
        if hasattr(self, "_broker_posture_combos"):
            by_b = {}
            for bname, combo in (self._broker_posture_combos or {}).items():
                val = combo.currentData()
                if val:
                    by_b[bname] = str(val)
            self.settings["risk_posture_by_broker"] = by_b
        if hasattr(self, "advisor_ask_chk"):
            self.settings["advisor_ask_before_apply"] = bool(self.advisor_ask_chk.isChecked())
        if hasattr(self, "monitor_controls_main_chk"):
            self.settings["monitor_controls_enabled"] = bool(
                self.monitor_controls_main_chk.isChecked()
            )
            if hasattr(self, "monitor_controls_chk"):
                self.monitor_controls_chk.setChecked(
                    bool(self.settings["monitor_controls_enabled"])
                )
        self._update_advisor_settings_summary()
        self._update_discord_settings_summary()
        if hasattr(self, "allow_scale_in_chk"):
            self.settings["allow_scale_in"] = bool(self.allow_scale_in_chk.isChecked())
        if hasattr(self, "prefer_whole_shares_chk"):
            self.settings["prefer_whole_shares_for_stops"] = bool(
                self.prefer_whole_shares_chk.isChecked()
            )
        if hasattr(self, "allow_frac_ttp_chk"):
            self.settings["allow_fractional_ttp_only"] = bool(self.allow_frac_ttp_chk.isChecked())
        if hasattr(self, "shadow_guard_chk"):
            self.settings["shadow_guardrail_enabled"] = bool(self.shadow_guard_chk.isChecked())
        self.settings["target_bp_utilization_pct"] = self.bp_util_spin.value()
        self.settings["sizing_focus_slots"] = int(self.sizing_focus_spin.value())
        if hasattr(self, "risk_pct_spin"):
            self.settings["risk_pct_per_trade"] = float(self.risk_pct_spin.value())
        if hasattr(self, "max_open_risk_spin"):
            self.settings["max_open_risk_pct"] = float(self.max_open_risk_spin.value())
        if hasattr(self, "name_cap_spin"):
            self.settings["max_single_name_equity_pct"] = float(self.name_cap_spin.value())
        if hasattr(self, "conviction_mult_spin"):
            self.settings["conviction_alloc_mult_max"] = float(self.conviction_mult_spin.value())
        # Exit scales have no dedicated spins — always derive from selected posture
        from scoring import get_risk_posture_profile, normalize_risk_posture
        _prof = get_risk_posture_profile(normalize_risk_posture(self.settings.get("risk_posture")))
        self.settings["exit_roi_scale"] = float(_prof["exit_roi_scale"])
        self.settings["exit_time_scale"] = float(_prof["exit_time_scale"])
        self.settings["ttp_arm_scale"] = float(_prof["ttp_arm_scale"])
        self.settings["allow_flat_time_banks"] = bool(
            _prof.get("allow_flat_time_banks", False)
        )
        self.settings["limit_offset_pct"] = self.offset_spin.value()
        if hasattr(self, "use_limit_entries_chk"):
            self.settings["use_limit_entries"] = bool(self.use_limit_entries_chk.isChecked())
        if hasattr(self, "use_limit_exits_chk"):
            self.settings["use_limit_exits"] = bool(self.use_limit_exits_chk.isChecked())
        if hasattr(self, "attach_stops_chk"):
            self.settings["attach_protective_stops"] = bool(self.attach_stops_chk.isChecked())
        if hasattr(self, "et_flatten_close_chk"):
            self.settings["et_flatten_before_close"] = bool(self.et_flatten_close_chk.isChecked())
        self.settings["daily_profit_target"] = self.profit_spin.value()
        self.settings["daily_loss_limit"] = self.loss_spin.value()
        if hasattr(self, "day_dd_spin"):
            self.settings["day_dd_pause_pct"] = float(self.day_dd_spin.value()) / 100.0
        if hasattr(self, "peak_dd_spin"):
            self.settings["peak_dd_pause_pct"] = float(self.peak_dd_spin.value()) / 100.0
        if hasattr(self, "dd_pause_spin"):
            self.settings["dd_pause_minutes"] = int(self.dd_pause_spin.value())
        self.settings["max_open_positions"] = self.max_pos_spin.value()
        self.settings["max_buys_per_cycle"] = self.max_buys_spin.value()
        self.settings["interval_crypto"] = self.c_spin.value()
        self.settings["interval_penny"] = self.p_spin.value()
        self.settings["interval_core"] = self.core_spin.value()
        self.settings["interval_portfolio"] = self.port_spin.value()
        if hasattr(self, "bal_spin"):
            self.settings["interval_balance_refresh"] = int(self.bal_spin.value())
        if hasattr(self, "cb_live_trading_chk"):
            self.settings["coinbase_live_trading"] = bool(self.cb_live_trading_chk.isChecked())
            cb = self.brokers.get("Coinbase")
            if cb is not None:
                cb.live_trading_enabled = bool(self.settings["coinbase_live_trading"])
        save_settings(self.settings)
        self._update_discord_webhook_status()
        self._update_companion_monitor_status()
        self._start_web_monitor()
        QMessageBox.information(self, "Settings Saved", "Configuration updated successfully!")

    def copy_log_to_clipboard(self):
        """Copy visible/filtered log only; disk fallback tails last N lines (never whole multi-MB file)."""
        text = ""
        if hasattr(self, "log_text_edit"):
            text = self.log_text_edit.toPlainText().strip()
        if not text:
            text = "\n".join(self._filtered_log_lines()).strip()
        if not text:
            text = _tail_activity_log_file(max_lines=ACTIVITY_LOG_DISK_TAIL_LINES).strip()
        if not text:
            QMessageBox.information(self, "Activity Log", "Nothing to copy yet.")
            return
        QApplication.clipboard().setText(text)
        self.log_event(f"Activity log copied to clipboard ({len(text.splitlines())} lines).")

    def save_log_to_file(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Save Log File", "activity_log.txt", "Text Files (*.txt);;All Files (*)")
        if filename:
            try:
                text = self.log_text_edit.toPlainText() if hasattr(self, "log_text_edit") else ""
                if not text:
                    text = "\n".join(self._filtered_log_lines())
                if not text:
                    text = _tail_activity_log_file(max_lines=ACTIVITY_LOG_DISK_TAIL_LINES)
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass

    def apply_color_formatting(self, item, text):
        """BUY=green, SELL/FAIL=red, HOLD/DO NOT BUY=amber. Uses brushes so theme QSS can't wipe it."""
        text_upper = str(text).upper()
        if "DO NOT BUY" in text_upper:
            fg = QColor("#FFD54F" if self.dark_mode else "#F57F17")
            bg = QColor("#332A00" if self.dark_mode else "#FFFDE7")
        elif "BUY" in text_upper:
            fg = QColor("#00E676" if self.dark_mode else "#2E7D32")
            bg = QColor("#003816" if self.dark_mode else "#E8F5E9")
        elif "SELL" in text_upper or "FAIL" in text_upper:
            fg = QColor("#FF5252" if self.dark_mode else "#C62828")
            bg = QColor("#3A0B0B" if self.dark_mode else "#FFEBEE")
        elif "HOLD" in text_upper or "SKIPPED" in text_upper or "PENDING" in text_upper:
            fg = QColor("#FFD54F" if self.dark_mode else "#F57F17")
            bg = QColor("#332A00" if self.dark_mode else "#FFFDE7")
        else:
            return
        item.setForeground(fg)
        item.setBackground(bg)
        item.setData(Qt.ForegroundRole, fg)
        item.setData(Qt.BackgroundRole, bg)

    def setup_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.setMinimumHeight(ui_px(28))
        status_widget = QWidget()
        layout = QHBoxLayout(status_widget)
        layout.setContentsMargins(ui_px(8), ui_px(4), ui_px(12), ui_px(4))
        layout.setSpacing(ui_px(10))
        self._status_layout = layout
        self.spinner = WorkingSpinner(self)
        self.status_text = QLabel("System Ready")
        self.status_text.setStyleSheet(
            f"font-size: {ui_px(14)}px; font-weight: 600; color: {theme_colors(self.dark_mode)['text']};"
        )
        self.status_text.setMinimumWidth(ui_px(200))
        layout.addWidget(self.spinner)
        layout.addWidget(self.status_text, 1)

        self.market_status_lbl = QLabel("…")
        self.market_status_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.market_status_lbl.setMinimumWidth(ui_px(200))
        layout.addWidget(self.market_status_lbl, 0)

        self.status_bar.addPermanentWidget(status_widget, 1)
        self.update_market_status()

    def set_working_state(self, is_working, message=""):
        tc = theme_colors(self.dark_mode)
        if is_working:
            self.spinner.start()
            self.status_text.setText(f"Working: {message}")
        else:
            self.spinner.stop()
            self.status_text.setText(message if message else "System Ready")
        self.status_text.setStyleSheet(
            f"font-size: {ui_px(14)}px; font-weight: 600; color: {tc['text']};"
        )
        # Avoid processEvents here — reentrancy during startup connect / theme paint
        # previously contributed to white/frozen windows when a modal blocked the loop.

    def apply_theme(self):
        arrow = combo_arrow_path(self.dark_mode)
        spin_up, spin_down = spin_arrow_paths(self.dark_mode)
        accent = theme_colors(self.dark_mode)["accent"]
        if self.dark_mode:
            qss = f"""
                QMainWindow, QWidget {{ background-color: #0F1115; color: #E8EAED; }}
                QLabel {{ background-color: transparent; }}
                QFrame {{ background-color: transparent; }}
                QFrame#topBar {{
                    background-color: transparent;
                    border: none;
                    border-bottom: 1px solid #2A2F3A;
                    border-radius: 0;
                }}
                QFrame#autoTraderBanner {{
                    background-color: transparent; border: 1px solid #2A2F3A;
                    border-radius: {ui_px(UI_RADIUS_FRAME)}px;
                }}
                QTabWidget::pane {{
                    border: 1px solid #2A2F3A; background-color: #0F1115;
                    border-radius: {ui_px(UI_RADIUS_INPUT)}px; top: -1px;
                }}
                QTabBar::tab {{
                    background-color: #151820; color: #9AA0A6;
                    padding: {ui_px(9)}px {ui_px(18)}px; margin-right: {ui_px(3)}px;
                    border: 1px solid #2A2F3A; border-bottom: none;
                    border-top-left-radius: {ui_px(8)}px; border-top-right-radius: {ui_px(8)}px;
                    min-width: {ui_px(88)}px;
                }}
                QTabBar::tab:selected {{
                    background-color: #1A1D24; color: #FFFFFF; font-weight: 600;
                    border-bottom: 2px solid {accent};
                }}
                QTabBar::tab:hover:!selected {{ background-color: #1C2129; color: #E8EAED; }}
                QTableWidget {{
                    background-color: #1A1D24; color: #E8EAED; gridline-color: #2A2F3A;
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    alternate-background-color: #151820; outline: 0;
                }}
                QHeaderView::section {{
                    background-color: #22262E; color: #9AA0A6; padding: {ui_px(8)}px {ui_px(6)}px;
                    border: none; border-bottom: 1px solid #2A2F3A;
                    font-weight: 600; font-size: {ui_px(11)}px;
                }}
                QTableCornerButton::section {{ background-color: #22262E; border: none; }}
                QPushButton {{
                    background-color: #22262E; color: #E8EAED;
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_BTN)}px;
                    padding: {ui_px(7)}px {ui_px(14)}px;
                }}
                QPushButton:hover {{ background-color: #2A303A; border-color: #3A4150; }}
                QPushButton:pressed {{ background-color: #1A1D24; }}
                QLineEdit, QTextEdit {{
                    background-color: #1A1D24; color: #E8EAED;
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(8)}px;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
                }}
                QDoubleSpinBox, QSpinBox {{
                    background-color: #1A1D24; color: #E8EAED;
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(28)}px {ui_px(5)}px {ui_px(8)}px;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
                }}
                QDoubleSpinBox::up-button, QSpinBox::up-button {{
                    subcontrol-origin: border; subcontrol-position: top right;
                    width: {ui_px(22)}px; border-left: 1px solid #2A2F3A;
                    background-color: #22262E;
                    border-top-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                }}
                QDoubleSpinBox::down-button, QSpinBox::down-button {{
                    subcontrol-origin: border; subcontrol-position: bottom right;
                    width: {ui_px(22)}px; border-left: 1px solid #2A2F3A;
                    background-color: #22262E;
                    border-bottom-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                }}
                QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
                QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{
                    background-color: #2A303A;
                }}
                QDoubleSpinBox::up-arrow, QSpinBox::up-arrow {{
                    image: url("{spin_up}"); width: {ui_px(10)}px; height: {ui_px(6)}px;
                }}
                QDoubleSpinBox::down-arrow, QSpinBox::down-arrow {{
                    image: url("{spin_down}"); width: {ui_px(10)}px; height: {ui_px(6)}px;
                }}
                QComboBox {{
                    background-color: #1A1D24; color: #E8EAED;
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(28)}px {ui_px(5)}px {ui_px(10)}px; font-weight: 600; min-height: {ui_px(24)}px;
                }}
                QComboBox:hover {{ border: 1px solid {UI_ACCENT_HOVER}; }}
                QComboBox::drop-down {{
                    subcontrol-origin: padding; subcontrol-position: top right;
                    width: {ui_px(26)}px; border-left: 1px solid #2A2F3A;
                    background-color: #22262E;
                    border-top-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    border-bottom-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                }}
                QComboBox::down-arrow {{
                    image: url("{arrow}"); width: {ui_px(12)}px; height: {ui_px(8)}px;
                }}
                QComboBox QAbstractItemView {{
                    background-color: #1A1D24; color: #E8EAED;
                    border: 1px solid #2A2F3A;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
                    outline: 0; border-radius: {ui_px(6)}px;
                }}
                QComboBox QAbstractItemView::item {{
                    background-color: #1A1D24; color: #E8EAED;
                    min-height: {ui_px(28)}px; padding: {ui_px(4)}px {ui_px(8)}px;
                }}
                QComboBox QAbstractItemView::item:selected {{
                    background-color: {accent}; color: #FFFFFF;
                }}
                QComboBox QAbstractItemView::item:hover {{
                    background-color: #22262E; color: #E8EAED;
                }}
                QGroupBox {{
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_CARD)}px;
                    margin-top: {ui_px(12)}px; font-weight: 600; background-color: #1A1D24;
                    padding-top: {ui_px(6)}px;
                }}
                QGroupBox::title {{ subcontrol-origin: margin; left: {ui_px(12)}px; padding: 0 {ui_px(4)}px; }}
                QStatusBar {{
                    color: #E8EAED; font-size: {ui_px(14)}px; min-height: {ui_px(28)}px;
                    padding: {ui_px(2)}px {ui_px(4)}px;
                }}
                QCheckBox {{
                    spacing: {ui_px(8)}px; color: #E8EAED;
                }}
                QCheckBox::indicator {{
                    width: {ui_px(16)}px; height: {ui_px(16)}px;
                    border: 1px solid #9AA0A6; border-radius: {ui_px(3)}px;
                    background-color: #1A1D24;
                }}
                QCheckBox::indicator:hover {{ border-color: #E8EAED; }}
                QCheckBox::indicator:checked {{
                    background-color: {accent}; border-color: {accent};
                }}
                QScrollBar:vertical {{
                    background: #0F1115; width: {ui_px(10)}px; margin: 0; border: none;
                }}
                QScrollBar::handle:vertical {{
                    background: #2A2F3A; border-radius: {ui_px(5)}px; min-height: {ui_px(24)}px;
                }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            """
        else:
            qss = f"""
                QMainWindow, QWidget {{ background-color: #F7F8FA; color: #1A1A1A; }}
                QLabel {{ background-color: transparent; color: #1A1A1A; }}
                QFrame {{ background-color: transparent; }}
                QFrame#topBar {{
                    background-color: transparent;
                    border: none;
                    border-bottom: 1px solid #C5CAD3;
                    border-radius: 0;
                }}
                QFrame#autoTraderBanner {{
                    background-color: transparent; border: 1px solid #C5CAD3;
                    border-radius: {ui_px(UI_RADIUS_FRAME)}px;
                }}
                QTabWidget::pane {{
                    border: 1px solid #C5CAD3; background-color: #FFFFFF;
                    border-radius: {ui_px(UI_RADIUS_INPUT)}px; top: -1px;
                }}
                QTabBar::tab {{
                    background-color: #E8EAED; color: #3C4043;
                    padding: {ui_px(9)}px {ui_px(18)}px; margin-right: {ui_px(3)}px;
                    border: 1px solid #C5CAD3; border-bottom: none;
                    border-top-left-radius: {ui_px(8)}px; border-top-right-radius: {ui_px(8)}px;
                    min-width: {ui_px(88)}px;
                }}
                QTabBar::tab:selected {{
                    background-color: #FFFFFF; color: #1A1A1A; font-weight: 600;
                    border-bottom: 2px solid {accent};
                }}
                QTabBar::tab:hover:!selected {{ background-color: #F0F2F5; color: #1A1A1A; }}
                QTableWidget {{
                    background-color: #FFFFFF; color: #1A1A1A; gridline-color: #E8EAED;
                    border: 1px solid #C5CAD3; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    alternate-background-color: #F7F8FA; outline: 0;
                }}
                QHeaderView::section {{
                    background-color: #EEF0F3; color: #3C4043; padding: {ui_px(8)}px {ui_px(6)}px;
                    border: none; border-bottom: 1px solid #C5CAD3;
                    font-weight: 600; font-size: {ui_px(12)}px;
                }}
                QTableCornerButton::section {{ background-color: #EEF0F3; border: none; }}
                QPushButton {{
                    background-color: #FFFFFF; color: #1A1A1A;
                    border: 1px solid #C5CAD3; border-radius: {ui_px(UI_RADIUS_BTN)}px;
                    padding: {ui_px(7)}px {ui_px(14)}px;
                }}
                QPushButton:hover {{ background-color: #F0F2F5; border-color: #9AA0A6; }}
                QPushButton:pressed {{ background-color: #E8EAED; }}
                QLineEdit, QTextEdit {{
                    background-color: #FFFFFF; color: #1A1A1A;
                    border: 1px solid #C5CAD3; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(8)}px;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
                }}
                QDoubleSpinBox, QSpinBox {{
                    background-color: #FFFFFF; color: #1A1A1A;
                    border: 1px solid #C5CAD3; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(28)}px {ui_px(5)}px {ui_px(8)}px;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
                }}
                QDoubleSpinBox::up-button, QSpinBox::up-button {{
                    subcontrol-origin: border; subcontrol-position: top right;
                    width: {ui_px(22)}px; border-left: 1px solid #C5CAD3;
                    background-color: #F0F2F5;
                    border-top-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                }}
                QDoubleSpinBox::down-button, QSpinBox::down-button {{
                    subcontrol-origin: border; subcontrol-position: bottom right;
                    width: {ui_px(22)}px; border-left: 1px solid #C5CAD3;
                    background-color: #F0F2F5;
                    border-bottom-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                }}
                QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
                QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{
                    background-color: #E8EAED;
                }}
                QDoubleSpinBox::up-arrow, QSpinBox::up-arrow {{
                    image: url("{spin_up}"); width: {ui_px(10)}px; height: {ui_px(6)}px;
                }}
                QDoubleSpinBox::down-arrow, QSpinBox::down-arrow {{
                    image: url("{spin_down}"); width: {ui_px(10)}px; height: {ui_px(6)}px;
                }}
                QComboBox {{
                    background-color: #FFFFFF; color: #1A1A1A;
                    border: 1px solid #C5CAD3; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(28)}px {ui_px(5)}px {ui_px(10)}px; font-weight: 600; min-height: {ui_px(24)}px;
                }}
                QComboBox:hover {{ border: 1px solid {accent}; }}
                QComboBox::drop-down {{
                    subcontrol-origin: padding; subcontrol-position: top right;
                    width: {ui_px(26)}px; border-left: 1px solid #C5CAD3;
                    background-color: #F0F2F5;
                    border-top-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    border-bottom-right-radius: {ui_px(UI_RADIUS_INPUT)}px;
                }}
                QComboBox::down-arrow {{
                    image: url("{arrow}"); width: {ui_px(12)}px; height: {ui_px(8)}px;
                }}
                QComboBox QAbstractItemView {{
                    background-color: #FFFFFF; color: #1A1A1A;
                    border: 1px solid #C5CAD3;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
                    outline: 0; border-radius: {ui_px(6)}px;
                }}
                QComboBox QAbstractItemView::item {{
                    background-color: #FFFFFF; color: #1A1A1A;
                    min-height: {ui_px(28)}px; padding: {ui_px(4)}px {ui_px(8)}px;
                }}
                QComboBox QAbstractItemView::item:selected {{
                    background-color: {accent}; color: #FFFFFF;
                }}
                QGroupBox {{
                    border: 1px solid #C5CAD3; border-radius: {ui_px(UI_RADIUS_CARD)}px;
                    margin-top: {ui_px(12)}px; font-weight: 600; background-color: #FFFFFF;
                    padding-top: {ui_px(6)}px; color: #1A1A1A;
                }}
                QGroupBox::title {{ subcontrol-origin: margin; left: {ui_px(12)}px; padding: 0 {ui_px(4)}px; }}
                QStatusBar {{
                    color: #1A1A1A; font-size: {ui_px(14)}px; min-height: {ui_px(28)}px;
                    padding: {ui_px(2)}px {ui_px(4)}px;
                }}
                QCheckBox {{ spacing: {ui_px(8)}px; color: #1A1A1A; }}
                QScrollBar:vertical {{
                    background: #F7F8FA; width: {ui_px(10)}px; margin: 0; border: none;
                }}
                QScrollBar::handle:vertical {{
                    background: #C5CAD3; border-radius: {ui_px(5)}px; min-height: {ui_px(24)}px;
                }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            """

        QApplication.instance().setStyleSheet(qss)
        if hasattr(self, 'at_status_frame'):
            self._reset_autotrader_banner_style()
        self._style_combo_popups()
        self._style_home_cards()
        if hasattr(self, "et_live_trading_chk"):
            tc_chk = theme_colors(self.dark_mode)
            self.et_live_trading_chk.setStyleSheet(
                f"QCheckBox {{ color: {tc_chk['text']}; spacing: {ui_px(8)}px; }}"
            )
        if hasattr(self, "cb_live_trading_chk"):
            tc_chk = theme_colors(self.dark_mode)
            self.cb_live_trading_chk.setStyleSheet(
                f"QCheckBox {{ color: {tc_chk['text']}; spacing: {ui_px(8)}px; }}"
            )
        if hasattr(self, "status_text"):
            tc = theme_colors(self.dark_mode)
            self.status_text.setStyleSheet(
                f"font-size: {ui_px(14)}px; font-weight: 600; color: {tc['text']};"
            )
        if hasattr(self, "portfolio_val_lbl"):
            self._refresh_top_bar_from_cache()
        self.update_market_status()
        table_bg = QColor("#1A1D24" if self.dark_mode else "#FFFFFF")
        table_fg = QColor("#E8EAED" if self.dark_mode else "#1A1A1A")
        for table in self.findChildren(QTableWidget):
            pal = table.palette()
            pal.setColor(QPalette.Base, table_bg)
            pal.setColor(QPalette.Text, table_fg)
            pal.setColor(QPalette.Window, table_bg)
            pal.setColor(QPalette.WindowText, table_fg)
            table.setPalette(pal)
            table.setAlternatingRowColors(True)
            table.setShowGrid(False)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(ui_px(UI_ROW_HEIGHT))
            if table.viewport():
                table.viewport().setPalette(pal)
                table.viewport().setAutoFillBackground(True)

    def _style_combo_popups(self):
        """Windows Fusion often ignores QSS on QComboBox popups — paint them explicitly."""
        tc = theme_colors(self.dark_mode)
        if self.dark_mode:
            bg, fg, sel_bg, sel_fg = QColor("#1A1D24"), QColor("#E8EAED"), QColor(tc["accent"]), QColor("#FFFFFF")
        else:
            bg, fg, sel_bg, sel_fg = QColor("#FFFFFF"), QColor("#1A1A1A"), QColor(tc["accent"]), QColor("#FFFFFF")

        for combo in self.findChildren(QComboBox):
            view = combo.view()
            if view is None:
                continue
            pal = view.palette()
            pal.setColor(QPalette.Base, bg)
            pal.setColor(QPalette.Text, fg)
            pal.setColor(QPalette.Window, bg)
            pal.setColor(QPalette.WindowText, fg)
            pal.setColor(QPalette.Highlight, sel_bg)
            pal.setColor(QPalette.HighlightedText, sel_fg)
            pal.setColor(QPalette.Button, bg)
            pal.setColor(QPalette.ButtonText, fg)
            view.setPalette(pal)
            view.setAutoFillBackground(True)
            combo.setPalette(pal)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    gui = MarketAdvisorGUI()
    gui.show()
    sys.exit(app.exec_())
