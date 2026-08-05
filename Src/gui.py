import os
import sys
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
                             QLabel, QTableWidget, QTableWidgetItem, QHeaderView, 
                             QPushButton, QMessageBox, QInputDialog, QLineEdit, 
                             QApplication, QStatusBar, QFrame, QCheckBox, QComboBox,
                             QDoubleSpinBox, QSpinBox, QTextEdit, QFileDialog, QDialog, QDialogButtonBox,
                             QFormLayout, QGroupBox,
                             QSystemTrayIcon, QMenu, QAction, QScrollArea, QSizePolicy)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QEventLoop, QPoint, QSize
from PyQt5.QtGui import QPainter, QPen, QColor, QPalette, QPixmap, QPolygon, QIcon, QCursor
import threading

import journal
import monitor
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
KNOWN_CRYPTOS = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
BROKER_NAMES = ("Robinhood", "Coinbase", "E*TRADE")


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
    )
    return any(n in text for n in needles)


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
        "scale_in_max_adds": 1,
        "scale_in_size_frac": 0.50,
        "exit_roi_scale": 1.0,
        "exit_time_scale": 1.0,
        "ttp_arm_scale": 1.0,
        "limit_offset_pct": 0.1,
        "daily_profit_target": 0.0,
        "daily_loss_limit": 8.0,
        "max_open_positions": 8,
        "max_buys_per_cycle": 2,
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
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                defaults.update(data)
        except Exception:
            pass
            
    if defaults.get("interval_crypto", 30) < 30: defaults["interval_crypto"] = 30
    if defaults.get("interval_penny", 60) < 60: defaults["interval_penny"] = 60
    if defaults.get("interval_core", 300) < 120: defaults["interval_core"] = 300
    if defaults.get("interval_portfolio", 60) < 30: defaults["interval_portfolio"] = 30
    if defaults.get("interval_balance_refresh", 60) < 30: defaults["interval_balance_refresh"] = 30
        
    return defaults


def save_settings(settings):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(settings, f, indent=4)
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
    """
    return (
        f"QPushButton {{ background-color: {bg}; color: {fg}; font-weight: 600; "
        f"border-radius: {ui_px(UI_RADIUS_BTN)}px; border: 1px solid rgba(0,0,0,40); "
        f"padding: {ui_px(7)}px {ui_px(14)}px; min-height: {ui_px(28)}px; }}"
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
        self.task_queue = []
        self.is_processing_queue = False
        self._cycle_broker = None  # Broker locked for the in-flight auto-trade cycle
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
        self._balances_refresh_in_flight = False
        self._last_idle_balance_refresh = 0.0
        self._startup_connect_finished = False
        self.cost_basis_cache = _blank_broker_map(lambda: {})
        self._scoring_state_loaded = False
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
        self._monitor_banner = "Application starting…"
        self._trading_tabs_built = False

        self.apply_theme()
        self.update_market_status()
        self.log_event("Application initialized. Verifying connections...")
        self.log_event(f"Version {APP_VERSION}" + (f" — {VERSION_NOTE}" if VERSION_NOTE else ""))
        self._setup_system_tray()  # tray visible immediately

        # Remaining tabs + startup connect after first paint
        QTimer.singleShot(0, self._finish_ui_build)

    def _fit_to_screen(self):
        """Open at a usable size that fits the available desktop; allow shrinking."""
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
        if hasattr(self, "_scale_timer"):
            self._scale_timer.start(90)

    def _on_scale_timer(self):
        new_scale = compute_ui_scale(self.width(), self.height())
        if abs(new_scale - getattr(self, "_ui_scale", 1.0)) < 0.025:
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
            self._top_bar_layout.setContentsMargins(ui_px(12), ui_px(8), ui_px(12), ui_px(8))
            self._top_bar_layout.setSpacing(ui_px(10))

        if hasattr(self, "broker_dropdown"):
            self.broker_dropdown.setFixedWidth(ui_px(120))
        if hasattr(self, "portfolio_val_lbl"):
            self.portfolio_val_lbl.setMinimumWidth(ui_px(130))
        if hasattr(self, "buying_power_lbl"):
            self.buying_power_lbl.setMinimumWidth(ui_px(150))
        if hasattr(self, "daily_profit_lbl"):
            self.daily_profit_lbl.setMinimumWidth(ui_px(120))
        for btn, mh, mw in (
            (getattr(self, "paper_mode_btn", None), 34, 108),
            (getattr(self, "dark_mode_btn", None), 34, 68),
            (getattr(self, "auto_trade_btn", None), 34, 138),
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
            self._home_layout.setContentsMargins(ui_px(18), ui_px(14), ui_px(18), ui_px(14))
            self._home_layout.setSpacing(ui_px(12))
        if hasattr(self, "_master_card_layout"):
            self._master_card_layout.setContentsMargins(ui_px(18), ui_px(18), ui_px(18), ui_px(16))
            self._master_card_layout.setSpacing(ui_px(6))
        if hasattr(self, "_rh_card_layout"):
            self._rh_card_layout.setContentsMargins(ui_px(16), ui_px(14), ui_px(16), ui_px(14))
            self._rh_card_layout.setSpacing(ui_px(28))
        if hasattr(self, "_cb_card_layout"):
            self._cb_card_layout.setContentsMargins(ui_px(16), ui_px(14), ui_px(16), ui_px(14))
            self._cb_card_layout.setSpacing(ui_px(28))
        if hasattr(self, "recent_trades_table"):
            self.recent_trades_table.setMinimumHeight(ui_px(140))
            polish_trades_header(self.recent_trades_table)

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
        self._trading_tabs_built = True
        self.build_portfolio_screen()
        self.build_crypto_screen()
        self.build_penny_screen()
        self.build_core_screen()
        self.build_ipo_screen()
        self.build_activity_log_screen()
        self.build_settings_screen()

        self.penny_tab_index = 3
        self.core_tab_index = 4
        self.ipo_tab_index = -1
        for i in range(self.tabs.count()):
            title = self.tabs.tabText(i)
            if "Breakout" in title:
                self.penny_tab_index = i
            elif "Core" in title:
                self.core_tab_index = i
            elif title == "IPOs":
                self.ipo_tab_index = i

        self._apply_view_mode_tabs()
        # Scale once deferred tabs exist (action buttons / section headers tagged)
        self._apply_ui_scale()
        QTimer.singleShot(0, lambda: self.director_timer.start(1000))
        # IPO calendar: first load shortly after UI settles; then every few hours
        QTimer.singleShot(8000, lambda: self.refresh_ipo_calendar(force=False))
        self._ipo_auto_timer = QTimer(self)
        self._ipo_auto_timer.timeout.connect(lambda: self.refresh_ipo_calendar(force=False))
        self._ipo_auto_timer.start(3 * 3600 * 1000)

        # Warm heavy libs in the background so the first score/scan doesn't hitch
        def _warm():
            try:
                import scoring  # noqa: F401
                import yfinance  # noqa: F401
                import robin_stocks.robinhood  # noqa: F401
            except Exception:
                pass
        threading.Thread(target=_warm, daemon=True).start()

        # Now that Settings/status labels exist, continue startup
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
    def is_locked(self, ticker):
        key = f"{self.cycle_broker_name}:{ticker}"
        if key in self.trade_locks:
            if time.time() - self.trade_locks[key] < 300: 
                return True
            else:
                del self.trade_locks[key]
        return False

    def set_lock(self, ticker):
        key = f"{self.cycle_broker_name}:{ticker}"
        self.trade_locks[key] = time.time()

    def _persist_session_baselines(self):
        """Save day-start equity so Day P&L survives app restarts."""
        self.settings["pnl_baseline_date"] = str(datetime.now().date())
        self.settings["pnl_baseline_rh"] = self.session_starts.get("Robinhood")
        self.settings["pnl_baseline_cb"] = self.session_starts.get("Coinbase")
        self.settings["pnl_baseline_et"] = self.session_starts.get("E*TRADE")
        save_settings(self.settings)

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
        # Coinbase API has no reliable avg cost — overlay tracked buys; else seed live price
        cache = self.cost_basis_cache.get(broker_name, {})
        for a in assets:
            if not isinstance(a, dict):
                continue
            ticker = a.get("ticker")
            if ticker in cache and cache[ticker] > 0:
                a['cost'] = cache[ticker]
            elif broker_name == "Coinbase" and (not a.get('cost') or a['cost'] <= 0):
                live = broker.get_live_price(ticker) if broker else 0.0
                a['cost'] = live if live and live > 0 else 0.0
                if a['cost'] > 0:
                    cache[ticker] = a['cost']
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

    def _journal_fill(self, side, ticker, asset_type, price, status, dollars=None, qty=None, order_id=None):
        confirmed = ("Filled" in status) or ("[PAPER]" in status)
        if "Pending" in status:
            confirmed = False
        try:
            journal.log_trade({
                "broker": self.cycle_broker_name,
                "side": side,
                "ticker": ticker,
                "asset_type": asset_type,
                "price": price,
                "dollars": dollars,
                "qty": qty,
                "status": status,
                "order_id": order_id,
                "confirmed": confirmed,
                "paper": self.paper_mode,
                "fee_profile": getattr(self.cycle_broker, "broker_id", ""),
            })
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
            self._journal_fill("BUY", ticker, asset_type, price, status, dollars=trade_dollars, qty=shares_bought)
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
        self._journal_fill("BUY", ticker, asset_type, price, status, dollars=spent, qty=(spent / price) if price and spent else None, order_id=order_id)
        filled_ok = (
            spent and spent > 0
            and "Fail" not in str(status)
            and "Skipped" not in str(status)
            and "Filled" in str(status)
        )
        if filled_ok:
            self._attach_protective_stop(broker_name, ticker, asset_type, price, spent)
        return status, spent

    def execute_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                           market_hours="regular_hours", allow_fractional=True):
        """Paper-mode-aware sell. Never calls the real broker API when self.paper_mode is True."""
        broker_name = self.cycle_broker_name
        if self.paper_mode:
            book = self.sandbox_holdings.setdefault(broker_name, {})
            pos = book.get(ticker)
            if not pos or pos['shares'] <= 0:
                return "Fail: No simulated position to sell"
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
            status = f"[PAPER] Sell Simulated ({format_currency(proceeds)})"
            self._journal_fill("SELL", ticker, asset_type, price, status, dollars=proceeds, qty=sell_qty)
            return status
        # Live: cancel protective first so reserved shares can sell
        self._cancel_protective_stop(broker_name, ticker, asset_type)
        result = self.cycle_broker.place_sell_order(
            ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
            market_hours=market_hours, allow_fractional=allow_fractional,
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
        self._journal_fill("SELL", ticker, asset_type, price, status, dollars=shares_val * price, qty=shares_val, order_id=order_id)
        return status

    def send_discord_alert(self, message, is_trade=False, embed=None, urgent=False):
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
                if embed:
                    body["embeds"] = [embed]
                    if message:
                        body["content"] = f"🤖 **MarketAdvisor [{tag}]**"
                else:
                    body["content"] = f"🤖 **MarketAdvisor [{tag}]**: {message}"
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
        cache = self.cost_basis_cache.get(broker_name) or {}
        entry = cache.get(ticker)
        if entry is None and ticker:
            entry = cache.get(str(ticker).upper())
        try:
            return float(entry or 0)
        except (TypeError, ValueError):
            return 0.0

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

        self.send_discord_alert(f"Heartbeat {clock}", embed=embed)
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
                self.home_et_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
                self.home_et_bp_lbl.setText(f"Buying Power: {format_money(bp)}")
                self.home_et_pl_lbl.setText(f"Day P&L: {pl_display}")
                self.home_et_pl_lbl.setStyleSheet(metric_label_style(color, 15))

        if hasattr(self, "home_master_pl_lbl"):
            cpl_str = format_money(abs(combined_pl))
            cpl_display = f"+{cpl_str}" if combined_pl >= 0 else f"-{cpl_str}"
            cpl_color = tc["success"] if combined_pl > 0.001 else (
                tc["danger"] if combined_pl < -0.001 else tc["neutral"]
            )
            self.home_master_pl_lbl.setText(f"Combined Day P&L: {cpl_display}")
            self.home_master_pl_lbl.setStyleSheet(metric_label_style(cpl_color, 20))

        if hasattr(self, "portfolio_val_lbl"):
            self._refresh_top_bar_from_cache()

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
                    self.home_et_val_lbl.setText(f"Portfolio: {format_money(p_val)}")
                    self.home_et_bp_lbl.setText(f"Buying Power: {format_money(bp)}")
                    self.home_et_pl_lbl.setText(f"Day P&L: {pl_display}")
                    self.home_et_pl_lbl.setStyleSheet(metric_label_style(color, 15))

            # Profit/loss limits only on trusted balance reads (never on a glitch $0)
            if self.auto_trade_enabled.get(broker_name) and trusted.get(broker_name):
                target_profit = self.settings.get("daily_profit_target", 0.0)
                if target_profit > 0 and pl_val >= target_profit:
                    msg = f"🎯 **[{broker_name}] Day Profit Target Reached!** Target: {format_currency(target_profit)} | Gain: {format_currency(pl_val)}. Disarming Auto-Trader."
                    self.log_event(msg)
                    self.send_discord_alert(msg, urgent=True)
                    self._disarm_broker(broker_name)

                loss_limit = self.settings.get("daily_loss_limit", 0.0)
                if loss_limit > 0 and pl_val <= -loss_limit:
                    msg = f"🚨 **[{broker_name}] MAX DAILY LOSS LIMIT HIT!** Limit: -{format_currency(loss_limit)} | Loss: -{pl_str}. EMERGENCY HALT DISARMING AUTO-TRADER."
                    self.log_event(msg)
                    self.send_discord_alert(msg, urgent=True)
                    self._disarm_broker(broker_name)

        if hasattr(self, 'home_master_pl_lbl'):
            cpl_str = format_money(abs(combined_pl))
            cpl_display = f"+{cpl_str}" if combined_pl >= 0 else f"-{cpl_str}"
            tc = theme_colors(self.dark_mode)
            cpl_color = tc["success"] if combined_pl > 0.001 else (
                tc["danger"] if combined_pl < -0.001 else tc["neutral"]
            )
            self.home_master_pl_lbl.setText(f"Combined Day P&L: {cpl_display}")
            self.home_master_pl_lbl.setStyleSheet(metric_label_style(cpl_color, 20))

        # Top bar reflects current view (All = combined)
        self._refresh_top_bar_from_cache()
        self.set_working_state(False)
        self.publish_monitor_status()

        # Launch Discord: first ping after balances, or upgrade an empty/$0 ping
        self._balances_fetched_once = True
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
        """Start or restart the local read-only status server from settings."""
        if not self.settings.get("monitor_enabled", True):
            monitor.stop_monitor()
            return
        host = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
        port = int(self.settings.get("monitor_port", 8791))
        user = self.settings.get("monitor_user", "") or ""
        pwd = self.settings.get("monitor_pass", "") or ""
        monitor.stop_monitor()
        ok, msg = monitor.start_monitor(host=host, port=port, username=user, password=pwd)
        self.log_event(msg if ok else f"Web monitor failed: {msg}")
        if ok:
            self.publish_monitor_status()

    def publish_monitor_status(self):
        """Push a read-only snapshot to the web monitor (safe to call often)."""
        try:
            totals = getattr(self, "_last_balance_totals", {}) or {}
            balances = {}
            combined_eq = combined_cash = combined_pnl = 0.0
            holdings_count = {}
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
            balances["combined"] = {
                "equity": combined_eq,
                "cash": combined_cash,
                "day_pnl": combined_pnl,
            }
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
            })
        except Exception:
            pass

    def _now_et(self):
        """US Eastern — Robinhood equity sessions are always quoted in ET."""
        return datetime.now(ZoneInfo("America/New_York"))

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
        is_crypto = "crypto" in str(asset_type or "").lower() or str(ticker).upper() in KNOWN_CRYPTOS
        if is_crypto:
            return None
        if not session.get("equity_tradeable"):
            return "equity markets closed"
        try:
            shares = float(shares_val or 0)
        except (TypeError, ValueError):
            shares = 0.0
        try:
            px = float(price or 0)
        except (TypeError, ValueError):
            px = 0.0
        # Sub-1 share positions need RH fractionals
        if 0 < shares < 1.0:
            if px > 0 and (shares * px) < 1.00:
                return "fractional notional under $1"
            if not session.get("fractional_ok"):
                return (
                    "fractional equity sells blocked until ~7am ET / regular hours "
                    "(after-hours fractionals end ~7:30pm ET)"
                )
            if session.get("label") != "REGULAR" and str(ticker).upper() in self._frac_ext_ineligible:
                return "ticker not eligible for extended-hours fractionals (waiting for regular open)"
        return None

    def _note_deferred_sell(self, broker, ticker, reason, session_label, notes):
        """Log a deferred sell once per ticker/reason for this session label."""
        key = (str(broker), str(ticker).upper(), str(reason)[:64])
        if self._sell_defer_log.get(key) == session_label:
            return
        self._sell_defer_log[key] = session_label
        notes.append(f"[{broker}] Deferring [{ticker}] — {reason}")

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
        """
        labels = {
            "pre_open": "Pre-open session check…",
            "open": "Open session check…",
            "pre_close": "Pre-close session check…",
        }
        self.log_event(labels.get(kind, f"Session boundary check ({kind})…"))

        broker = "Robinhood"
        if not self.auto_trade_enabled.get(broker):
            return
        if (
            not self.paper_mode
            and not self.brokers[broker].is_connected
        ):
            return

        # Prefer front of queue so sells/buys hit ASAP vs any pending CRYPTO pulse
        priority = []
        for task in ("PORTFOLIO", "CORE", "PENNY"):
            item = (broker, task)
            if item in self.task_queue:
                self.task_queue.remove(item)
            priority.append(item)
        self.task_queue = priority + self.task_queue

        self.last_port_time[broker] = now_ts
        self.last_core_time[broker] = now_ts
        self.last_penny_time[broker] = now_ts
        self._set_engine_banner(
            f"🤖 ⏰ [{broker}] {labels.get(kind, 'Session check')} — queued",
            "#00897B",
        )

    def _maybe_session_boundary_wakeup(self, now_ts):
        """
        Once per weekday (ET): equity cycles ~60s before 9:30 open, at/just after open,
        and ~60s before 16:00 close. Weekends skipped; no holiday calendar (weekday RTH baseline).
        """
        if not self.auto_trade_enabled.get("Robinhood"):
            return

        now_et = self._now_et()
        if now_et.weekday() >= 5:
            return

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
                self._send_cycle_error_discord(fname, summary)
                self.cycle_finished()

        task.result_ready.connect(on_success_callback)
        task.error_occurred.connect(_on_error)
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def _send_cycle_error_discord(self, fname, err):
        """Discord identical cycle errors at most once per 10 minutes (log always)."""
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
        self._full_log_lines = self._full_log_lines[-2000:]

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
        except Exception:
            pass

    def _append_log_line_ui(self, log_line):
        if not hasattr(self, "log_text_edit"):
            return
        if self._log_line_matches_filter(log_line, self._activity_log_filter()):
            self.log_text_edit.append(log_line)

    def _activity_log_filter(self):
        if hasattr(self, "log_filter_combo"):
            return self.log_filter_combo.currentText() or "All"
        return "All"

    def _log_line_matches_filter(self, line, filt):
        """All = everything; broker filters = lines that mention that broker."""
        if not filt or filt == "All":
            return True
        return filt in line

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
        if hasattr(self, "log_text_edit"):
            self.log_text_edit.clear()
        self.log_event("Activity log cleared.")

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
        top_bar.setFrameShape(QFrame.StyledPanel)

        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(ui_px(12), ui_px(8), ui_px(12), ui_px(8))
        layout.setSpacing(ui_px(10))
        self._top_bar_layout = layout

        self.broker_dropdown = QComboBox()
        self.broker_dropdown.setObjectName("brokerDropdown")
        self.broker_dropdown.addItems(["All", "Robinhood", "Coinbase", "E*TRADE"])
        self.broker_dropdown.setCurrentText("All")
        self.broker_dropdown.setFixedWidth(ui_px(120))
        self.broker_dropdown.setMaxVisibleItems(5)
        self.broker_dropdown.currentTextChanged.connect(self.on_broker_switch)

        self.portfolio_val_lbl = QLabel("Portfolio: $0.00")
        self.portfolio_val_lbl.setStyleSheet(metric_label_style(theme_colors(self.dark_mode)["accent"], 16))
        self.portfolio_val_lbl.setMinimumWidth(ui_px(130))

        self.buying_power_lbl = QLabel("Buying Power: $0.00")
        self.buying_power_lbl.setStyleSheet(metric_label_style(theme_colors(self.dark_mode)["success"], 16))
        self.buying_power_lbl.setMinimumWidth(ui_px(150))

        self.daily_profit_lbl = QLabel("Day P&L: …")
        self.daily_profit_lbl.setStyleSheet(metric_label_style(theme_colors(self.dark_mode)["neutral"], 16))
        self.daily_profit_lbl.setMinimumWidth(ui_px(120))

        self.paper_mode_btn = QPushButton("Mode: PAPER" if self.paper_mode else "Mode: LIVE")
        self.paper_mode_btn.setMinimumHeight(ui_px(34))
        self.paper_mode_btn.setMinimumWidth(ui_px(108))
        self.paper_mode_btn.setStyleSheet(
            top_bar_btn_style("#E65100") if self.paper_mode else top_bar_btn_style("#1B5E20")
        )
        self.paper_mode_btn.clicked.connect(self.toggle_paper_mode)

        self.dark_mode_btn = QPushButton("Light" if self.dark_mode else "Dark")
        self.dark_mode_btn.setMinimumHeight(ui_px(34))
        self.dark_mode_btn.setMinimumWidth(ui_px(68))
        self.dark_mode_btn.setToolTip("Toggle light / dark theme")
        self.dark_mode_btn.clicked.connect(self.toggle_dark_mode)

        self.auto_trade_btn = QPushButton("Auto-Trader: OFF")
        self.auto_trade_btn.setMinimumHeight(ui_px(34))
        self.auto_trade_btn.setMinimumWidth(ui_px(138))
        self.auto_trade_btn.setStyleSheet(top_bar_btn_style("#424242"))
        self.auto_trade_btn.setToolTip(
            "When OFF: open the broker picker to arm Auto-Trader. "
            "When ON: turn off all brokers, or choose Change brokers… to manage individually."
        )
        self.auto_trade_btn.clicked.connect(self.toggle_auto_trade)

        broker_lbl = QLabel("Broker")
        broker_lbl.setObjectName("brokerHint")
        broker_lbl.setStyleSheet(
            f"color: {theme_colors(self.dark_mode)['muted']}; font-size: {ui_px(13)}px; font-weight: 600;"
        )
        self.broker_hint_lbl = broker_lbl
        layout.addWidget(broker_lbl)
        layout.addWidget(self.broker_dropdown)
        layout.addSpacing(ui_px(4))
        layout.addWidget(self.portfolio_val_lbl)
        layout.addWidget(self.buying_power_lbl)
        layout.addWidget(self.daily_profit_lbl)
        layout.addStretch(1)
        layout.addWidget(self.paper_mode_btn)
        layout.addWidget(self.dark_mode_btn)
        layout.addWidget(self.auto_trade_btn)

        self.main_layout.addWidget(top_bar)

    def _set_stock_tabs_visible(self, visible):
        """Show/hide Breakouts + Core + IPOs tabs (Coinbase has no equities)."""
        for idx in (
            getattr(self, 'penny_tab_index', 3),
            getattr(self, 'core_tab_index', 4),
            getattr(self, 'ipo_tab_index', -1),
        ):
            if idx < 0 or idx >= self.tabs.count():
                continue
            # If user is sitting on a tab we're about to hide, bounce to Portfolio
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
        self.daily_profit_lbl.setStyleSheet(metric_label_style(color, 16))
        # Keep portfolio / cash accents readable after theme flips
        if hasattr(self, "portfolio_val_lbl"):
            # Preserve current text; only refresh color weight
            self.portfolio_val_lbl.setStyleSheet(metric_label_style(tc["accent"], 16))
        if hasattr(self, "buying_power_lbl"):
            self.buying_power_lbl.setStyleSheet(metric_label_style(tc["success"], 16))
        if hasattr(self, "broker_hint_lbl"):
            self.broker_hint_lbl.setStyleSheet(
                f"color: {tc['muted']}; font-size: {ui_px(13)}px; font-weight: 600;"
            )

    def _style_home_cards(self):
        """Theme-aware Home card surfaces (also re-run on dark/light toggle)."""
        if self.dark_mode:
            panel, line, mute = "#1A1D24", "#2A2F3A", "#9AA0A6"
            title_fg = "#E8EAED"
        else:
            panel, line, mute = "#FFFFFF", "#C5CAD3", "#3C4043"
            title_fg = "#1A1A1A"
        tc = theme_colors(self.dark_mode)
        # Extra padding-top so title + large net-worth font don't collide / clip
        master = (
            f"QGroupBox {{ font-size: {ui_px(15)}px; font-weight: 600; color: {title_fg}; "
            f"background-color: {panel}; border: 1px solid {tc['accent']}; "
            f"border-radius: {ui_px(UI_RADIUS_CARD)}px; margin-top: {ui_px(14)}px; "
            f"padding-top: {ui_px(20)}px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: {ui_px(16)}px; "
            f"padding: 0 {ui_px(6)}px; }}"
        )
        broker = (
            f"QGroupBox {{ font-size: {ui_px(13)}px; font-weight: 600; color: {title_fg}; "
            f"background-color: {panel}; border: 1px solid {line}; "
            f"border-radius: {ui_px(UI_RADIUS_CARD)}px; margin-top: {ui_px(12)}px; "
            f"padding-top: {ui_px(16)}px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: {ui_px(14)}px; "
            f"padding: 0 {ui_px(5)}px; color: {mute}; }}"
        )
        if hasattr(self, "master_card"):
            self.master_card.setStyleSheet(master)
        if hasattr(self, "rh_card"):
            self.rh_card.setStyleSheet(broker)
        if hasattr(self, "cb_card"):
            self.cb_card.setStyleSheet(broker)
        if hasattr(self, "et_card"):
            self.et_card.setStyleSheet(broker)
        if hasattr(self, "home_master_val_lbl"):
            self.home_master_val_lbl.setStyleSheet(metric_label_style(tc["accent"], 36))
            # Stylesheet fonts don't inflate sizeHint — pin height so $120 isn't clipped
            self.home_master_val_lbl.setMinimumHeight(ui_px(48))
        if hasattr(self, "home_master_bp_lbl"):
            self.home_master_bp_lbl.setStyleSheet(metric_label_style(tc["success"], 16))
            self.home_master_bp_lbl.setMinimumHeight(ui_px(24))
        if hasattr(self, "home_master_pl_lbl"):
            self.home_master_pl_lbl.setMinimumHeight(ui_px(26))
        for name in (
            "home_rh_val_lbl", "home_rh_bp_lbl",
            "home_cb_val_lbl", "home_cb_bp_lbl",
            "home_et_val_lbl", "home_et_bp_lbl",
        ):
            lbl = getattr(self, name, None)
            if lbl is not None:
                lbl.setStyleSheet(
                    f"font-size: {ui_px(14)}px; font-weight: 600; padding: {ui_px(4)}px 2px;"
                )
                lbl.setMinimumHeight(ui_px(24))
        for name in ("home_rh_pl_lbl", "home_cb_pl_lbl", "home_et_pl_lbl"):
            lbl = getattr(self, name, None)
            if lbl is not None:
                lbl.setMinimumHeight(ui_px(24))

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
        layout.setContentsMargins(ui_px(18), ui_px(14), ui_px(18), ui_px(14))
        layout.setSpacing(ui_px(12))
        self._home_layout = layout
        
        title = QLabel("Master Portfolio")
        title.setObjectName("homeTitle")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: {ui_px(20)}px; font-weight: 600; "
            f"margin: {ui_px(4)}px 0 {ui_px(8)}px 0;"
        )
        layout.addWidget(title)

        # Master Combined Banner
        self.master_card = QGroupBox("Net Worth")
        self.master_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        mc_layout = QVBoxLayout()
        mc_layout.setContentsMargins(ui_px(18), ui_px(14), ui_px(18), ui_px(14))
        mc_layout.setSpacing(ui_px(6))
        self._master_card_layout = mc_layout
        
        self.home_master_val_lbl = QLabel("$0.00")
        self.home_master_val_lbl.setAlignment(Qt.AlignCenter)
        self.home_master_val_lbl.setTextInteractionFlags(Qt.NoTextInteraction)
        self.home_master_val_lbl.setMinimumHeight(ui_px(48))
        
        self.home_master_bp_lbl = QLabel("Combined Liquid Cash: $0.00")
        self.home_master_bp_lbl.setAlignment(Qt.AlignCenter)
        self.home_master_bp_lbl.setMinimumHeight(ui_px(24))

        self.home_master_pl_lbl = QLabel("Combined Day P&L: $0.00")
        self.home_master_pl_lbl.setStyleSheet(metric_label_style(theme_colors(self.dark_mode)["neutral"], 18))
        self.home_master_pl_lbl.setAlignment(Qt.AlignCenter)
        self.home_master_pl_lbl.setMinimumHeight(ui_px(26))
        
        mc_layout.addWidget(self.home_master_val_lbl)
        mc_layout.addWidget(self.home_master_bp_lbl)
        mc_layout.addWidget(self.home_master_pl_lbl)
        self.master_card.setLayout(mc_layout)
        layout.addWidget(self.master_card)

        # Broker Line Items Container
        brokers_layout = QVBoxLayout()
        brokers_layout.setSpacing(ui_px(12))

        def _pack_broker_metrics(hbox, labels):
            """Keep portfolio / BP / P&L grouped — don't fling them to opposite edges on wide windows."""
            for lbl in labels:
                lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
                lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                lbl.setMinimumHeight(ui_px(24))
                hbox.addWidget(lbl)
            hbox.addStretch(1)

        # Robinhood Line Item
        self.rh_card = QGroupBox("Robinhood · Equities & Crypto")
        self.rh_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        rh_layout = QHBoxLayout()
        rh_layout.setContentsMargins(ui_px(16), ui_px(12), ui_px(16), ui_px(12))
        rh_layout.setSpacing(ui_px(28))
        self._rh_card_layout = rh_layout
        
        self.home_rh_val_lbl = QLabel("Portfolio: $0.00")
        self.home_rh_val_lbl.setObjectName("homeBrokerMetric")
        self.home_rh_val_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        self.home_rh_bp_lbl = QLabel("Buying Power: $0.00")
        self.home_rh_bp_lbl.setObjectName("homeBrokerMetric")
        self.home_rh_bp_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        self.home_rh_pl_lbl = QLabel("Day P&L: $0.00")
        self.home_rh_pl_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        _pack_broker_metrics(
            rh_layout,
            (self.home_rh_val_lbl, self.home_rh_bp_lbl, self.home_rh_pl_lbl),
        )
        self.rh_card.setLayout(rh_layout)
        brokers_layout.addWidget(self.rh_card)

        # Coinbase Line Item
        self.cb_card = QGroupBox("Coinbase Advanced · Crypto")
        self.cb_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        cb_layout = QHBoxLayout()
        cb_layout.setContentsMargins(ui_px(16), ui_px(12), ui_px(16), ui_px(12))
        cb_layout.setSpacing(ui_px(28))
        self._cb_card_layout = cb_layout
        
        self.home_cb_val_lbl = QLabel("Portfolio: $0.00")
        self.home_cb_val_lbl.setObjectName("homeBrokerMetric")
        self.home_cb_val_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        self.home_cb_bp_lbl = QLabel("Buying Power: $0.00")
        self.home_cb_bp_lbl.setObjectName("homeBrokerMetric")
        self.home_cb_bp_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        self.home_cb_pl_lbl = QLabel("Day P&L: $0.00")
        self.home_cb_pl_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        _pack_broker_metrics(
            cb_layout,
            (self.home_cb_val_lbl, self.home_cb_bp_lbl, self.home_cb_pl_lbl),
        )
        self.cb_card.setLayout(cb_layout)
        brokers_layout.addWidget(self.cb_card)

        # E*TRADE Line Item
        self.et_card = QGroupBox("E*TRADE · Equities & ETFs")
        self.et_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        et_layout = QHBoxLayout()
        et_layout.setContentsMargins(ui_px(16), ui_px(12), ui_px(16), ui_px(12))
        et_layout.setSpacing(ui_px(28))
        self._et_card_layout = et_layout

        self.home_et_val_lbl = QLabel("Portfolio: $0.00")
        self.home_et_val_lbl.setObjectName("homeBrokerMetric")
        self.home_et_val_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        self.home_et_bp_lbl = QLabel("Buying Power: $0.00")
        self.home_et_bp_lbl.setObjectName("homeBrokerMetric")
        self.home_et_bp_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        self.home_et_pl_lbl = QLabel("Day P&L: $0.00")
        self.home_et_pl_lbl.setStyleSheet(f"font-size: {ui_px(14)}px;")
        _pack_broker_metrics(
            et_layout,
            (self.home_et_val_lbl, self.home_et_bp_lbl, self.home_et_pl_lbl),
        )
        self.et_card.setLayout(et_layout)
        brokers_layout.addWidget(self.et_card)

        layout.addLayout(brokers_layout)

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
        self.tabs.addTab(tab, "Scanner: Crypto")

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
        self.tabs.addTab(tab, "Scanner: Breakouts")

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
        self.tabs.addTab(tab, "Scanner: Core")

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

        filter_lbl = QLabel("Show:")
        self.log_filter_combo = QComboBox()
        self.log_filter_combo.setObjectName("logFilterCombo")
        self.log_filter_combo.addItems(["All", "Robinhood", "Coinbase"])
        self.log_filter_combo.setFixedWidth(ui_px(130))
        self.log_filter_combo.setToolTip("Filter log lines by broker (All keeps app-wide messages too)")
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

        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.setObjectName("clearLogBtn")
        clear_log_btn.setFixedWidth(ui_px(100))
        clear_log_btn.clicked.connect(self._clear_activity_log)

        header_bar.addWidget(header)
        header_bar.addStretch()
        header_bar.addWidget(filter_lbl)
        header_bar.addWidget(self.log_filter_combo)
        header_bar.addWidget(copy_log_btn)
        header_bar.addWidget(save_log_btn)
        header_bar.addWidget(clear_log_btn)
        layout.addLayout(header_bar)

        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        layout.addWidget(self.log_text_edit)
        tab.setLayout(layout)
        self.tabs.addTab(tab, "Activity Log")

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
        brokers_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
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

        self._build_broker_login_dialogs()
        self._build_discord_webhook_dialog()

        # Config Form
        form_layout = QVBoxLayout()

        discord_wh_box = QHBoxLayout()
        discord_wh_box.setSpacing(ui_px(10))
        discord_wh_box.addWidget(QLabel("Discord Webhook:"))
        self.discord_webhook_status_lbl = QLabel("Not set")
        self.discord_webhook_status_lbl.setMinimumWidth(ui_px(140))
        discord_wh_box.addWidget(self.discord_webhook_status_lbl)
        discord_wh_box.addStretch(1)
        self.discord_webhook_btn = QPushButton("Webhook…")
        self.discord_webhook_btn.setToolTip("Edit Discord webhook URL (kept off the main Settings form)")
        self.discord_webhook_btn.clicked.connect(self._open_discord_webhook_dialog)
        discord_wh_box.addWidget(self.discord_webhook_btn)
        form_layout.addLayout(discord_wh_box)
        self._update_discord_webhook_status()

        discord_lvl_box = QHBoxLayout()
        discord_lvl_box.addWidget(QLabel("Discord Notification Level:"))
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
        discord_lvl_combo_box = QHBoxLayout()
        discord_lvl_combo_box.addWidget(self.discord_lvl_combo)
        discord_lvl_box.addLayout(discord_lvl_combo_box)
        form_layout.addLayout(discord_lvl_box)

        hb_box = QHBoxLayout()
        hb_box.addWidget(QLabel("Discord Heartbeat (once per hour):"))
        self.discord_hb_combo = QComboBox()
        self.discord_hb_combo.addItems([
            "Rolling (every hour from now)",
            "Align to :00 (top of hour)",
            "Align to :15",
            "Align to :30",
            "Align to :45",
        ])
        saved_hb = self.settings.get("discord_heartbeat_schedule", "Rolling (every hour from now)")
        # Soft-migrate old spammy labels
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
        form_layout.addLayout(hb_box)

        big_win_box = QHBoxLayout()
        big_win_box.addWidget(QLabel("Big-win Discord ROI % (fires under Important Only):"))
        self.discord_big_win_spin = QDoubleSpinBox()
        self.discord_big_win_spin.setRange(0.1, 100.0)
        self.discord_big_win_spin.setSingleStep(0.5)
        self.discord_big_win_spin.setDecimals(1)
        self.discord_big_win_spin.setValue(float(self.settings.get("discord_big_win_roi_pct", 1.5)))
        big_win_box.addWidget(self.discord_big_win_spin)
        big_win_box.addStretch()
        form_layout.addLayout(big_win_box)

        mon_box = QHBoxLayout()
        self.monitor_enabled_chk = QCheckBox("Web Monitor (read-only)")
        self.monitor_enabled_chk.setChecked(bool(self.settings.get("monitor_enabled", True)))
        mon_box.addWidget(self.monitor_enabled_chk)
        mon_box.addWidget(QLabel("Port:"))
        self.monitor_port_spin = QSpinBox()
        self.monitor_port_spin.setRange(1024, 65535)
        self.monitor_port_spin.setValue(int(self.settings.get("monitor_port", 8791)))
        mon_box.addWidget(self.monitor_port_spin)
        mon_box.addWidget(QLabel("User:"))
        self.monitor_user_input = QLineEdit(self.settings.get("monitor_user", ""))
        self.monitor_user_input.setPlaceholderText("optional")
        self.monitor_user_input.setMaximumWidth(120)
        mon_box.addWidget(self.monitor_user_input)
        mon_box.addWidget(QLabel("Pass:"))
        self.monitor_pass_input = QLineEdit(self.settings.get("monitor_pass", ""))
        self.monitor_pass_input.setEchoMode(QLineEdit.Password)
        self.monitor_pass_input.setPlaceholderText("optional")
        self.monitor_pass_input.setMaximumWidth(120)
        mon_box.addWidget(self.monitor_pass_input)
        mon_box.addStretch()
        form_layout.addLayout(mon_box)
        mon_hint = QLabel("Open http://127.0.0.1:<port>/ while the app runs. Use Tailscale for phone access.")
        mon_hint.setObjectName("settingsHint")
        mon_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(mon_hint)

        # Risk Posture — one control that retunes util / slots / name cap / exit patience
        from scoring import RISK_POSTURE_PROFILES, normalize_risk_posture, get_risk_posture_profile
        posture_box = QHBoxLayout()
        posture_box.addWidget(QLabel("Risk Posture:"))
        self.risk_posture_combo = QComboBox()
        self._risk_posture_keys = ["safer", "balanced", "aggressive"]
        for key in self._risk_posture_keys:
            prof = RISK_POSTURE_PROFILES[key]
            self.risk_posture_combo.addItem(prof["label"], key)
        saved_posture = normalize_risk_posture(self.settings.get("risk_posture", "balanced"))
        posture_idx = self._risk_posture_keys.index(saved_posture)
        self.risk_posture_combo.setCurrentIndex(posture_idx)
        self.risk_posture_combo.setToolTip(
            "Safer: diversify & bank sooner  ·  Balanced: current defaults  ·  "
            "Aggressive: concentrate into fewer larger tickets"
        )
        self.risk_posture_combo.currentIndexChanged.connect(self._on_risk_posture_changed)
        posture_box.addWidget(self.risk_posture_combo)
        posture_box.addStretch()
        form_layout.addLayout(posture_box)
        self.risk_posture_hint = QLabel(get_risk_posture_profile(saved_posture).get("hint", ""))
        self.risk_posture_hint.setObjectName("settingsHint")
        self.risk_posture_hint.setWordWrap(True)
        self.risk_posture_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(self.risk_posture_hint)

        scale_box = QHBoxLayout()
        self.allow_scale_in_chk = QCheckBox("Allow scale-in (add near support on held names)")
        # Default from settings; posture change retunes when user switches profile
        _si_default = get_risk_posture_profile(saved_posture).get("allow_scale_in", True)
        if "allow_scale_in" in self.settings and self.settings.get("allow_scale_in") is not None:
            _si_default = bool(self.settings.get("allow_scale_in"))
        self.allow_scale_in_chk.setChecked(bool(_si_default))
        self.allow_scale_in_chk.setToolTip(
            "When ON, already-held tickers may get a smaller add if price is near a "
            "6‑month revisit / cost-basis zone, ROI is in the posture add band, score "
            "is constructive, and max adds / name cap still bind. Not blind DCA."
        )
        scale_box.addWidget(self.allow_scale_in_chk)
        scale_box.addStretch()
        form_layout.addLayout(scale_box)
        self.scale_in_hint = QLabel(
            "Scale-in: average in only near support — Safer defaults OFF; "
            "Balanced/Aggressive ON with stricter/wider ROI bands."
        )
        self.scale_in_hint.setObjectName("settingsHint")
        self.scale_in_hint.setWordWrap(True)
        self.scale_in_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(self.scale_in_hint)

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

        alloc_hint = QLabel(
            "Risk Posture sets util / focus slots / max open / name cap / conviction stretch / "
            "exit patience / scale-in defaults together — you can still fine-tune the knobs after. "
            "Size deploys usable BP into Focus Slots (not every Max Open slot). Caps: ~0.75% equity "
            "risk per trade; cluster / hard-stop rails stay on in every posture. Min $ is a floor only."
        )
        alloc_hint.setObjectName("settingsHint")
        alloc_hint.setWordWrap(True)
        alloc_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(alloc_hint)

        # Keep legacy widget name used by older code paths
        self.alloc_spin = self.alloc_stock_spin

        min_dollar_box = QHBoxLayout()
        min_dollar_box.addWidget(QLabel("Minimum Order Threshold ($):"))
        self.min_dollar_spin = QDoubleSpinBox()
        self.min_dollar_spin.setRange(0.50, 500.0)
        self.min_dollar_spin.setValue(self.settings.get("min_trade_dollars", 5.0))
        min_dollar_box.addWidget(self.min_dollar_spin)
        min_dollar_box.addStretch()
        form_layout.addLayout(min_dollar_box)
        min_hint = QLabel(
            "Broker floor only (RH crypto needs ≥ $5). Does not force tiny trades when buying power is ample."
        )
        min_hint.setObjectName("settingsHint")
        min_hint.setWordWrap(True)
        min_hint.setStyleSheet(f"color: #888; font-size: {ui_px(11)}px;")
        form_layout.addWidget(min_hint)

        offset_box = QHBoxLayout()
        offset_box.addWidget(QLabel("Limit Order Buffer Offset %:"))
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(0.01, 5.0)
        self.offset_spin.setValue(self.settings.get("limit_offset_pct", 0.1))
        offset_box.addWidget(self.offset_spin)
        offset_box.addStretch()
        form_layout.addLayout(offset_box)

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
        
        layout.addWidget(QLabel("\nEngine Polling Intervals (Seconds):"))
        
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
        self.bal_spin.setToolTip("How often equity/cash refresh while the app is open (even if Auto-Trader is off)")
        bal_box.addWidget(self.bal_spin)
        bal_box.addStretch()
        form_layout.addLayout(bal_box)

        save_settings_btn = QPushButton("💾 Save Configuration")
        save_settings_btn.setProperty("uiBtnKind", "primary")
        save_settings_btn.setProperty("uiBtnExtra", "QPushButton { margin-top: 15px; }")
        save_settings_btn.setStyleSheet(action_btn_style("primary") + "QPushButton { margin-top: 15px; }")
        save_settings_btn.clicked.connect(self.save_custom_settings)
        form_layout.addWidget(save_settings_btn)

        ver_lbl = QLabel(
            f"{display_name()}"
            + (f"  ·  {VERSION_NOTE}" if VERSION_NOTE else "")
        )
        ver_lbl.setObjectName("settingsVersion")
        ver_lbl.setWordWrap(True)
        ver_lbl.setStyleSheet(
            f"color: #6B7280; font-size: {ui_px(12)}px; margin-top: {ui_px(18)}px;"
        )
        form_layout.addWidget(ver_lbl)

        layout.addLayout(form_layout)
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
        self.rh_pass_input = QLineEdit(self.settings.get("rh_password", ""))
        self.rh_pass_input.setEchoMode(QLineEdit.Password)
        self.rh_connect_btn = QPushButton("Connect Robinhood")
        self.rh_connect_btn.setProperty("uiBtnKind", "primary")
        self.rh_connect_btn.setStyleSheet(action_btn_style("primary"))
        self.rh_connect_btn.clicked.connect(self.connect_robinhood)
        rh_form.addRow("Status:", self.rh_dialog_status_lbl)
        rh_form.addRow("Email:", self.rh_email_input)
        rh_form.addRow("Password:", self.rh_pass_input)
        rh_form.addRow("", self.rh_connect_btn)

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
        self.cb_secret_input = QLineEdit(self.settings.get("cb_api_secret", ""))
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
        cb_form.addRow("Status:", self.cb_dialog_status_lbl)
        cb_form.addRow("CDP API Key:", self.cb_key_input)
        cb_form.addRow("CDP API Secret:", self.cb_secret_input)
        cb_form.addRow("", self.cb_live_trading_chk)
        cb_form.addRow("", self.cb_connect_btn)

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
        else:
            if _is_manual_auth_failure(msg):
                self._broker_manual_auth_needed["E*TRADE"] = True
            QMessageBox.warning(self, "Connection Failed", f"E*TRADE: {msg}")

    def disconnect_etrade(self):
        try:
            self.brokers["E*TRADE"].logout()
        except Exception:
            pass
        self._broker_manual_auth_needed["E*TRADE"] = True
        self._set_broker_status("E*TRADE", "🔴 Disconnected", "")
        self.log_event("[E*TRADE] Disconnected / token revoked.")

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
            self.settings["cb_api_secret"] = result.get("secret") or ""
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
            if self.settings.get("rh_email") and self.settings.get("rh_password"):
                result["rh_needs_password"] = True

        if self.settings.get("cb_api_key") and self.settings.get("cb_api_secret"):
            try:
                ok, _ = self.brokers["Coinbase"].login({
                    "api_key": self.settings["cb_api_key"],
                    "api_secret": self.settings["cb_api_secret"],
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
            else:
                self._set_broker_status("E*TRADE", "🔴 Disconnected")
                self._broker_manual_auth_needed["E*TRADE"] = False

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
        if active:
            self.auto_trade_btn.setText("Auto-Trader: ON")
            self.auto_trade_btn.setStyleSheet(top_bar_btn_style(UI_DANGER))
            self.at_status_frame.setVisible(True)
            self._set_engine_banner(f"Auto-Trader Armed — {', '.join(active)}")
        else:
            self.auto_trade_btn.setText("Auto-Trader: OFF")
            self.auto_trade_btn.setStyleSheet(top_bar_btn_style("#424242"))
            self.at_status_frame.setVisible(False)
            self._reset_autotrader_banner_style()

    def _disarm_broker(self, broker_name, notify_discord=False):
        self.auto_trade_enabled[broker_name] = False
        self.task_queue = [
            item for item in self.task_queue
            if not (isinstance(item, (tuple, list)) and item and item[0] == broker_name)
        ]
        self.log_event(f"Auto-Trader disabled for {broker_name}.")
        self._update_autotrade_ui()
        if notify_discord:
            self.send_discord_alert(f"🛑 Auto-Trader **DISARMED** for **{broker_name}**.")

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
        return f"{broker_name} ({cap_txt}) — {status}"

    def _broker_is_arm_eligible(self, broker_name, *, warn=False):
        """Return True if this broker can be armed right now."""
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
        self.send_discord_alert(
            f"⚔️ Auto-Trader **ARMED** ({mode}) on {', '.join(armed)}.\n"
            + "\n".join(bp_lines)
        )

    def toggle_auto_trade(self):
        """When OFF: open arm picker. When ON: confirm full-off or open manage picker."""
        currently_on = [b for b, on in self.auto_trade_enabled.items() if on]
        if currently_on:
            dlg = AutoTraderOffDialog(self, currently_on, dark_mode=self.dark_mode)
            if dlg.exec_() != QDialog.Accepted:
                return
            if dlg.choice == AutoTraderOffDialog.OFF_ALL:
                self._disarm_all_engines(was=currently_on)
                return
            if dlg.choice != AutoTraderOffDialog.MANAGE:
                return
            # Fall through to checkbox picker (manage)
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
            self._disarm_broker(broker_name, notify_discord=True)

        newly_armed = self._arm_broker_engines(to_arm, warn=True) if to_arm else []

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

        for broker_name, enabled in self.auto_trade_enabled.items():
            if not enabled: continue
            if not self.brokers[broker_name].is_connected and not self.paper_mode:
                self._try_reconnect_broker(broker_name)
                # Skip cycles while disconnected or reconnect still running
                if (
                    not self.brokers[broker_name].is_connected
                    or self._reconnect_in_flight.get(broker_name)
                ):
                    continue

            if now - self.last_crypto_time[broker_name] >= self.settings.get("interval_crypto", 45):
                if self._broker_supports(broker_name, "supports_crypto"):
                    task = (broker_name, "CRYPTO")
                    if task not in self.task_queue: self.task_queue.append(task)
                self.last_crypto_time[broker_name] = now

            # Portfolio / sell checks: all armed brokers, 24/7
            if now - self.last_port_time[broker_name] >= self.settings.get("interval_portfolio", 45):
                task = (broker_name, "PORTFOLIO")
                if task not in self.task_queue: self.task_queue.append(task)
                self.last_port_time[broker_name] = now

            # Equities: capability-driven (RH + E*TRADE; not Coinbase)
            if self._broker_supports(broker_name, "supports_equities") and self.is_equity_session_active():
                if now - self.last_penny_time[broker_name] >= self.settings.get("interval_penny", 60):
                    task = (broker_name, "PENNY")
                    if task not in self.task_queue: self.task_queue.append(task)
                    self.last_penny_time[broker_name] = now

                if now - self.last_core_time[broker_name] >= self.settings.get("interval_core", 300):
                    task = (broker_name, "CORE")
                    if task not in self.task_queue: self.task_queue.append(task)
                    self.last_core_time[broker_name] = now

        self.process_queue()

        # Quiet idle balance poll — keeps top-bar equity/P&L fresh even when auto-trader is off.
        # Skip until startup connect finishes so we don't Discord/$0-paint before brokers attach.
        if getattr(self, "_startup_connect_finished", False):
            bal_every = int(self.settings.get("interval_balance_refresh", 60) or 60)
            bal_every = max(30, bal_every)
            if now - getattr(self, "_last_idle_balance_refresh", 0.0) >= bal_every:
                self._last_idle_balance_refresh = now
                self.refresh_account_balances(quiet=True)

        # Throttle monitor publish (~every 3s) so the phone page stays fresh
        last_pub = getattr(self, "_monitor_last_publish", 0.0)
        if now - last_pub >= 3.0:
            self._monitor_last_publish = now
            self.publish_monitor_status()

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
                self.log_event(f"[{broker_name}] Reconnected successfully.")
                self.send_discord_alert(f"✅ [{broker_name}] Session restored after drop.")
                self.refresh_account_balances()
            else:
                streak = self._reconnect_fail_streak.get(broker_name, 0) + 1
                self._reconnect_fail_streak[broker_name] = streak
                if _is_manual_auth_failure(detail):
                    self._broker_manual_auth_needed[broker_name] = True
                    self.log_event(
                        f"[{broker_name}] Reconnect needs manual auth — pausing auto-retries. ({detail})"
                    )
                else:
                    self.log_event(f"[{broker_name}] Reconnect failed ({streak}x): {detail}")
                    if streak >= 2:
                        self.send_discord_alert(
                            f"🚨 [{broker_name}] Reconnect failed {streak}x — auto cycles paused until session restored. ({detail})"
                        )

        def _fail(err):
            self._reconnect_in_flight[broker_name] = False
            streak = self._reconnect_fail_streak.get(broker_name, 0) + 1
            self._reconnect_fail_streak[broker_name] = streak
            self.log_event(f"[{broker_name}] Reconnect error ({streak}x): {err}")

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
        self._cycle_broker = broker_name
        self._set_trading_context(broker_name)
        self.log_event(f"[AUTO] Starting {task} cycle on {broker_name}")

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
        parts = []
        safe = [a for a in (assets or []) if isinstance(a, dict)]
        for a in sorted(safe, key=lambda x: (str(x.get('broker', '')), str(x.get('ticker', '')))):
            parts.append(f"{a.get('broker','')}:{a.get('ticker','')}:{float(a.get('shares') or 0):.8f}")
        return "|".join(parts)

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
            self._refresh_holdings_count_cache(assets if self.view_mode == "All" else None)
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
            self.portfolio_table.setItem(row, 3, QTableWidgetItem(format_currency(a.get('cost') or 0)))
            self.portfolio_table.setItem(row, 4, QTableWidgetItem(format_currency(price)))
            shares = float(a.get("shares") or 0.0)
            self.portfolio_table.setItem(row, 5, QTableWidgetItem(format_currency(shares * price)))
            self.portfolio_table.setItem(row, 6, QTableWidgetItem("Pending..."))
            self.portfolio_table.setItem(row, 7, QTableWidgetItem("Not Traded"))
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
        Hard/soft caps inside risk_sizing_breakdown: risk $, soft equity name cap.
        min_trade_dollars is a floor/skip only — not the target size.
        existing_name_value / size_frac: gated scale-in — size_frac shrinks aim before
        caps; min floor still applies when remaining name room allows.

        Returns trade dollars, or (trade, detail_dict) when return_detail=True.
        """
        from scoring import risk_sizing_breakdown, get_stop_distance_pct
        is_crypto = "crypto" in str(asset_type).lower()
        if is_crypto:
            alloc_pct = self.settings.get("allocation_pct_crypto", self.settings.get("allocation_pct", 8.0)) / 100.0
        else:
            alloc_pct = self.settings.get("allocation_pct_stock", self.settings.get("allocation_pct", 5.0)) / 100.0
        min_dollars = self.settings.get("min_trade_dollars", 5.0)
        broker_id = getattr(self.cycle_broker, "broker_id", None) or self.cycle_broker_name.upper()
        stop_d = get_stop_distance_pct(
            broker_id, ticker=ticker, asset_type=asset_type, for_sizing=True,
        )
        eq = float(equity) if equity is not None else None
        if eq is None:
            try:
                eq, _ = self.get_broker_balances(self.cycle_broker_name)
            except Exception:
                eq = float(current_bp or 0.0)
        max_open = max_open_positions
        if max_open is None:
            max_open = int(self.settings.get("max_open_positions", 8))
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
            util = float(self.settings.get("target_bp_utilization_pct", 88.0))
        except (TypeError, ValueError):
            util = 88.0
        try:
            focus = int(self.settings.get("sizing_focus_slots", 6))
        except (TypeError, ValueError):
            focus = 6
        try:
            name_cap = float(self.settings.get("max_single_name_equity_pct", 15.0))
        except (TypeError, ValueError):
            name_cap = 15.0
        try:
            conv_max = float(self.settings.get("conviction_alloc_mult_max", 1.50))
        except (TypeError, ValueError):
            conv_max = 1.50
        detail = risk_sizing_breakdown(
            eq, current_bp, stop_d, alloc_pct, min_dollars=min_dollars,
            conviction_score=score, open_count=open_n, max_open_positions=max_open,
            target_bp_utilization=util, sizing_focus_slots=focus,
            soft_name_equity_frac=name_cap, conviction_mult_max=conv_max,
            existing_name_value=existing_name_value, size_frac=size_frac,
        )
        trade = float(detail.get("trade") or 0.0)
        if return_detail:
            return trade, detail
        return trade

    def _note_scale_in_skip(self, notes, broker_name, ticker, reason, throttle_sec=600):
        """
        Append a SCALE-IN skip note, throttling identical broker/ticker/reason spam.
        Re-emits after throttle_sec with a repeat count so the Activity Log stays readable.
        """
        reason = str(reason or "sizing blocked").strip()
        key = (str(broker_name or "").upper(), str(ticker or "").upper(), reason)
        now = time.time()
        if not hasattr(self, "_si_skip_throttle"):
            self._si_skip_throttle = {}
        prev = self._si_skip_throttle.get(key)
        if prev is not None:
            elapsed = now - float(prev.get("t") or 0.0)
            if elapsed < float(throttle_sec):
                prev["n"] = int(prev.get("n") or 1) + 1
                return
            n = int(prev.get("n") or 1)
            self._si_skip_throttle[key] = {"t": now, "n": 1}
            suffix = f" (repeated {n}× over last {int(elapsed // 60) or 1}m)" if n > 1 else ""
            notes.append(
                f"[{broker_name}] SCALE-IN skipped [{ticker}]: {reason}{suffix}"
            )
            return
        self._si_skip_throttle[key] = {"t": now, "n": 1}
        notes.append(f"[{broker_name}] SCALE-IN skipped [{ticker}]: {reason}")

    def _attach_protective_stop(self, broker_name, ticker, asset_type, price, spent):
        """After a successful buy: broker stop if live; virtual stop in paper. Software TTP remains backup."""
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
            else:
                clear_protective_order(broker_id, ticker)
                self.log_event(
                    f"[{broker_name}] Could not attach broker stop [{ticker}]: {msg} — software TTP remains"
                )
        except Exception as e:
            self.log_event(f"[{broker_name}] Protective stop error [{ticker}]: {e}")

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
            if self.is_locked(ticker):
                self.log_event(f"[{row_broker}] Skipped [{ticker}]: trade lock active")
                continue
            asset_type = ticker_item.data(Qt.UserRole) or ""
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
            })

        if not sell_list:
            return
        self.set_working_state(True, "Executing sells…")
        self.run_thread(
            self._bg_execute_sell_batch,
            lambda payload: self._on_sell_batch_done(payload, auto_mode=auto_mode, finish_cycle=False),
            sell_list,
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
            if self.is_locked(ticker):
                self.log_event(f"[{self.cycle_broker_name}] Skipped [{ticker}]: trade lock active")
                continue
            filtered.append(c)
        if not filtered:
            if auto_mode:
                self.set_working_state(False)
                self.cycle_finished()
            return

        if not auto_mode:
            sample_type = filtered[0].get("asset_type", "")
            equity, bp = self.get_broker_balances(self.cycle_broker_name)
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
        runner(
            self._bg_buy_batch,
            lambda payload: self._on_buy_batch_done(payload, auto_mode=auto_mode, table=table),
            filtered,
            rank,
        )

    def _bg_buy_batch(self, candidates, rank=False):
        """Place buys on a worker thread (confirm_order sleeps stay off the UI)."""
        from scoring import (
            concentration_blocks_buy, buy_rank_score_for_book, buy_rank_score,
            evaluate_scale_in, record_scale_in, get_scale_in_params,
        )
        broker_name = self.cycle_broker_name
        broker_id = getattr(self.brokers.get(broker_name), "broker_id", None) or str(broker_name).upper()
        offset = self.settings.get("limit_offset_pct", 0.1) / 100.0
        session = self.get_equity_session_info()
        use_ext = session["use_ext"]
        market_hours = session["market_hours"]
        allow_fractional = session["fractional_ok"]
        max_positions = int(self.settings.get("max_open_positions", 8))
        max_buys = int(self.settings.get("max_buys_per_cycle", 2))
        posture = self.settings.get("risk_posture", "balanced")
        si_params = get_scale_in_params(posture=posture, settings=self.settings)
        notes = []
        ranked = list(candidates or [])

        equity, bp = self.get_broker_balances(broker_name)
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
        if ranked:
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
                                signal_score=base, posture=posture, settings=self.settings,
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
                                ))
                                notes.append(
                                    f"[{broker_name}] SCALE-IN considered [{ticker}]: OK — {ev.get('reason')}"
                                )
                            else:
                                c["score"] = -1000.0
                                notes.append(
                                    f"[{broker_name}] SCALE-IN blocked [{ticker}]: {ev.get('reason')}"
                                )
                        else:
                            c["scale_in"] = False
                            c["score"] = float(buy_rank_score_for_book(
                                ticker, is_crypto=is_crypto, held_tickers=held,
                                holdings_meta=holdings_meta, portfolio_value=equity,
                                crypto_only_broker=crypto_only_broker,
                            ))
                    except Exception:
                        c["score"] = float(c.get("score") or 0.0)
                        c["scale_in"] = bool(c.get("scale_in"))
            ranked.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            # Drop names that cannot improve the book (already held without scale-in / cluster full)
            actionable = [c for c in ranked if float(c.get("score") or 0.0) > -500.0]
            if rank or len(ranked) > 1:
                top_src = actionable or ranked
                top = ", ".join(
                    f"{c.get('ticker')}({float(c.get('score') or 0):.0f}"
                    f"{'*SI' if c.get('scale_in') else ''})" for c in top_src[:3]
                )
                notes.append(f"[{broker_name}] Ranked {len(actionable)}/{len(ranked)} buys for book — top: {top}")
            ranked = actionable

        fills = []
        buys_done = 0
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
            # max_open blocks new slots only — scale-in keeps the same ticker
            if (not is_held) and max_positions > 0 and open_count >= max_positions:
                notes.append(f"[{broker_name}] Max open positions ({max_positions}) — skipping further buys")
                break

            # Fresh live price — reject missing/stale quotes (no buy on blind data)
            live = 0.0
            try:
                live = float(broker.get_live_price(ticker) if broker else 0.0) or 0.0
            except Exception:
                live = 0.0
            if live <= 0:
                notes.append(f"[{broker_name}] Skipped [{ticker}]: missing/stale live price")
                continue
            price = live

            scale_in = False
            scale_frac = 1.0
            existing_val = 0.0
            if is_held:
                meta = holdings_by_ticker.get(tu) or {}
                existing_val = float(meta.get("value") or 0.0)
                base = float(c.get("score") or 0.0)
                # Re-evaluate at live price (gates may have moved since rank)
                try:
                    sig = float(buy_rank_score(ticker, is_crypto=is_crypto))
                except Exception:
                    sig = base
                ev = evaluate_scale_in(
                    ticker, price, meta.get("avg_cost") or 0.0,
                    broker_id=broker_id, asset_type=asset_type, is_crypto=is_crypto,
                    signal_score=sig, posture=posture, settings=self.settings,
                    existing_name_value=existing_val, portfolio_value=equity,
                )
                if not ev.get("allowed"):
                    notes.append(
                        f"[{broker_name}] SCALE-IN blocked [{ticker}]: {ev.get('reason')}"
                    )
                    continue
                scale_in = True
                scale_frac = float(ev.get("size_frac") or si_params.get("scale_in_size_frac") or 0.5)

            row_dollars, size_detail = self.calculate_order_sizing(
                bp, asset_type, entry_price=price, equity=equity, score=c.get("score"),
                open_count=open_count, max_open_positions=max_positions,
                existing_name_value=existing_val if scale_in else 0.0,
                size_frac=scale_frac if scale_in else 1.0,
                return_detail=True, ticker=ticker,
            )
            if row_dollars <= 0:
                if scale_in:
                    why = (size_detail or {}).get("skip_reason") or "size too small / name cap"
                    self._note_scale_in_skip(notes, broker_name, ticker, why)
                    continue
                notes.append(f"[{broker_name}] Skipping buys — buying power/risk size too low ({format_currency(bp)})")
                break
            blocked, reason = concentration_blocks_buy(
                ticker, held, holdings_meta=holdings_meta, portfolio_value=equity,
                proposed_dollars=row_dollars, is_crypto=is_crypto,
                allow_held_scale_in=scale_in,
                crypto_only_broker=crypto_only_broker,
            )
            if blocked:
                notes.append(f"[{broker_name}] Skipped [{ticker}]: concentration — {reason}")
                continue
            if scale_in:
                notes.append(
                    f"[{broker_name}] SCALE-IN {ticker} … reason: {ev.get('reason')} "
                    f"… size ${row_dollars:.2f}"
                )
            status, spent = self.execute_buy_order(
                ticker, asset_type, price, row_dollars, offset, use_ext,
                market_hours=market_hours, allow_fractional=allow_fractional,
            )
            ok = "Fail" not in status and "Skipped" not in status
            if ok:
                if scale_in:
                    try:
                        record_scale_in(broker_id, ticker)
                    except Exception:
                        pass
                    # Allow future skip notes again if adds re-arm later
                    if hasattr(self, "_si_skip_throttle"):
                        prefix = (str(broker_name or "").upper(), str(ticker or "").upper())
                        for k in list(self._si_skip_throttle.keys()):
                            if k[:2] == prefix:
                                self._si_skip_throttle.pop(k, None)
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
        return {"fills": fills, "notes": notes, "buys_done": buys_done, "broker": broker_name}

    def _on_buy_batch_done(self, payload, auto_mode=False, table=None):
        payload = payload or {}
        broker = payload.get("broker") or self.cycle_broker_name
        for note in payload.get("notes") or []:
            self.log_event(note)
        for fill in payload.get("fills") or []:
            ticker = fill.get("ticker")
            status = fill.get("status") or ""
            if fill.get("ok"):
                self.set_lock(ticker)
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
        self.refresh_recent_trades()
        if auto_mode:
            self.refresh_account_balances()
            if buys_done > 0:
                self.manual_portfolio_reload(and_score=False, force=True)
            else:
                self.set_working_state(False)
            self.cycle_finished()
        else:
            self.set_working_state(False)
            if buys_done > 0:
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

                status = self.execute_sell_order(
                    ticker, asset_type, price, shares,
                    offset, use_ext,
                    market_hours=market_hours, allow_fractional=allow_fractional,
                )
                self._mark_frac_ext_ineligible(ticker, status)
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
                self.set_lock(ticker)
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
                if fill.get("ok") and self._is_big_win_roi(roi):
                    gain_pct = float(roi) * 100.0
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
                        f"🎉 BIG WIN SELL {ticker}: +{gain_pct:.1f}%{dollar_part} — {status}",
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
        from scoring import evaluate_holding, flush_state
        results = []
        with SuppressPrints():
            for row, ticker, shares, avg_cost, asset_type, *rest in items:
                broker_name = rest[0] if rest else self.cycle_broker_name
                broker = self.brokers.get(broker_name, self.cycle_broker)
                price = broker.get_live_price(ticker) if broker else 0.0
                if not price or price <= 0:
                    results.append((row, 0.0, "HOLD (Untradeable — no quote)", asset_type, None))
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
                action = evaluate_holding(
                    ticker, avg_cost,
                    broker_id=getattr(broker, "broker_id", None) or broker_name,
                    asset_type=asset_type,
                    live_price=price,
                    exit_roi_scale=float(self.settings.get("exit_roi_scale", 1.0) or 1.0),
                    exit_time_scale=float(self.settings.get("exit_time_scale", 1.0) or 1.0),
                    ttp_arm_scale=float(self.settings.get("ttp_arm_scale", 1.0) or 1.0),
                )
                results.append((row, price, action, asset_type, None))
        flush_state()
        return results

    def _bg_score_opportunities(self, items):
        from scoring import evaluate_crypto_opportunity, evaluate_opportunity
        results = []
        # Price/score in the cycle broker's context (E*TRADE equities use ET, not RH)
        cycle = self.cycle_broker
        cycle_id = getattr(cycle, "broker_id", None) or self.cycle_broker_name
        rh = self.brokers.get("Robinhood")
        with SuppressPrints():
            for entry in items:
                row, ticker, shares, avg_cost, asset_type = entry[:5]
                is_crypto = "crypto" in str(asset_type).lower() or ticker.upper() in KNOWN_CRYPTOS
                is_penny = asset_type == "Penny Stock" or "mover" in str(asset_type).lower()
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
                        posture=self.settings.get("risk_posture", "balanced"),
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
        posture = self.settings.get("risk_posture", "balanced")
        try:
            equity, _bp = self.get_broker_balances(broker_name)
        except Exception:
            equity = 0.0
        holdings = self.get_broker_holdings(broker_name) or []
        held = {
            (a.get("ticker") or "").upper()
            for a in holdings
            if isinstance(a, dict) and a.get("ticker")
        }
        broker = self.brokers.get(broker_name)
        crypto_only_broker = not bool(getattr(broker, "supports_equities", True))
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
                            signal_score=base, posture=posture, settings=self.settings,
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
        return sum(
            1 for _, _, action, _, _ in results
            if "BUY" in str(action).upper() and "DO NOT BUY" not in str(action).upper()
        )

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

        sell_list = []
        for row, price, action, asset_type, err in results:
            if row >= len(assets):
                continue
            a = assets[row]
            if not isinstance(a, dict):
                continue
            ticker = a.get("ticker") or ""
            if not ticker:
                continue
            self._patch_portfolio_row_action(broker, ticker, price, action)
            if "SELL" in str(action).upper():
                sell_list.append({
                    'broker': broker,
                    'ticker': ticker,
                    'shares': float(a.get('shares') or 0),
                    'price': float(price or 0),
                    'avg_cost': float(a.get('cost') or 0),
                    'type': a.get('type') or asset_type or '',
                })

        sell_n = len(sell_list)
        session = self._sync_equity_session_state()
        actionable = []
        deferred = []
        notes_tmp = []
        for item in sell_list:
            if str(item.get("broker") or broker) == "Robinhood":
                defer = self._rh_equity_sell_defer_reason(
                    item.get("ticker"), item.get("shares"), item.get("price"),
                    item.get("type"), session,
                )
                if defer:
                    deferred.append(str(item.get("ticker") or "?").upper())
                    self._note_deferred_sell(
                        "Robinhood", item.get("ticker"), defer,
                        session.get("label") or "UNKNOWN", notes_tmp,
                    )
                    continue
            actionable.append(item)

        if deferred:
            uniq = sorted(set(deferred))
            if notes_tmp:
                # First time these are deferred this session — announce once
                self.log_event(
                    f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s); "
                    f"deferring {len(uniq)} until tradable: {', '.join(uniq)}"
                )
                for n in notes_tmp:
                    self.log_event(n)
            elif actionable:
                self.log_event(
                    f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s) "
                    f"({len(actionable)} actionable, {len(uniq)} still deferred)"
                )
            # else: all still deferred — stay quiet this cycle
        else:
            self.log_event(f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s)")

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

    def _log_scan_buy_outcome(self, engine, results, buy_candidates, dropped=None):
        """
        Log BUY score count, and when signals exist but none are actionable for the book,
        explain the silent drop (already held / cluster full) so BP-idle cycles are clear.
        """
        broker = self.cycle_broker_name
        raw_buys = self._count_buy_signals(results)
        actionable = len(buy_candidates or [])
        if actionable:
            self.log_event(f"[AUTO] [{broker}] {engine} scored — {actionable} BUY signal(s)")
        else:
            self.log_event(f"[AUTO] [{broker}] {engine} scored — {raw_buys} BUY signal(s)")
        if raw_buys > 0 and actionable == 0 and self._is_broker_auto_trading():
            detail = ", ".join((dropped or [])[:8]) or "already held / cluster full"
            extra = f" (+{len(dropped) - 8} more)" if dropped and len(dropped) > 8 else ""
            self.log_event(
                f"[{broker}] {engine}: {raw_buys} BUY signal(s) but 0 actionable for book "
                f"— no orders. Dropped: {detail}{extra}"
            )

    def _crypto_on_scored(self, payload):
        opps, results, buy_candidates, dropped = self._unpack_scan_payload(payload)
        self._apply_scored_opportunities(self.crypto_table, opps, results)
        self._log_scan_buy_outcome("CRYPTO", results, buy_candidates, dropped)
        if buy_candidates and self._is_broker_auto_trading():
            if len(buy_candidates) > 1:
                top = ", ".join(
                    f"{c.get('ticker')}({float(c.get('score') or 0):.0f})" for c in buy_candidates[:3]
                )
                self.log_event(f"[{self.cycle_broker_name}] Ranked {len(buy_candidates)} buys — top: {top}")
            self._set_engine_banner(f"🤖 💰 [{self.cycle_broker_name}] CRYPTO — executing...", "#FFB300")
            self.execute_scanner_trades(self.crypto_table, auto_mode=True, buy_candidates=buy_candidates)
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
        self._log_scan_buy_outcome("BREAKOUT", results, buy_candidates, dropped)
        if buy_candidates and self._is_broker_auto_trading():
            if len(buy_candidates) > 1:
                top = ", ".join(
                    f"{c.get('ticker')}({float(c.get('score') or 0):.0f})" for c in buy_candidates[:3]
                )
                self.log_event(f"[{self.cycle_broker_name}] Ranked {len(buy_candidates)} buys — top: {top}")
            self._set_engine_banner(f"🤖 💰 [{self.cycle_broker_name}] BREAKOUT — executing...", "#E53935")
            self.execute_scanner_trades(self.penny_table, auto_mode=True, buy_candidates=buy_candidates)
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
        self._log_scan_buy_outcome("CORE", results, buy_candidates, dropped)
        if buy_candidates and self._is_broker_auto_trading():
            if len(buy_candidates) > 1:
                top = ", ".join(
                    f"{c.get('ticker')}({float(c.get('score') or 0):.0f})" for c in buy_candidates[:3]
                )
                self.log_event(f"[{self.cycle_broker_name}] Ranked {len(buy_candidates)} buys — top: {top}")
            self._set_engine_banner(f"[{self.cycle_broker_name}] CORE — executing...", UI_ACCENT)
            self.execute_scanner_trades(self.core_table, auto_mode=True, buy_candidates=buy_candidates)
        else:
            self.set_working_state(False)
            self.cycle_finished()

    def _unpack_scan_payload(self, payload):
        """Normalize (opps, results[, buy_candidates[, dropped]]) from bg scan jobs."""
        if not payload:
            return [], [], [], []
        if isinstance(payload, (list, tuple)):
            if len(payload) >= 4:
                return payload[0] or [], payload[1] or [], payload[2] or [], payload[3] or []
            if len(payload) >= 3:
                return payload[0] or [], payload[1] or [], payload[2] or [], []
            if len(payload) == 2:
                return payload[0] or [], payload[1] or [], [], []
        return [], [], [], []

    def _bg_scan_crypto(self):
        return [{'symbol': c, 'type': 'Crypto'} for c in ["BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"]]

    def _bg_scan_penny(self):
        discovered, seen = [], set()
        Overview = _get_overview_class()
        if Overview is not None:
            try:
                fs = Overview()
                fs.set_filter(filters_dict={'Price': 'Under $5', 'Current Volume': 'Over 2M'})
                df = fs.screener_view()
                if df is not None and not df.empty and 'Ticker' in df.columns:
                    for t in df['Ticker'].head(8).tolist():
                        sym = str(t).upper().strip()
                        # Skip Finviz junk / non-symbols (e.g. AABAT-style garbage)
                        if not sym.isalpha() or not (1 <= len(sym) <= 5):
                            continue
                        if sym.startswith("AA") and len(sym) == 5:
                            continue
                        discovered.append({'symbol': sym, 'type': 'Penny Stock'})
                        seen.add(sym)
            except Exception:
                pass
        if self.brokers["Robinhood"].is_connected:
            try:
                import robin_stocks.robinhood as rh
                for item in rh.markets.get_top_100()[:10]:
                    sym = item.get('symbol') or item.get('ticker')
                    if sym and sym not in seen:
                        discovered.append({'symbol': sym, 'type': 'RH Top Mover'})
                        seen.add(sym)
            except Exception:
                pass
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

    def _on_risk_posture_changed(self, _index=None):
        """Selecting a posture retunes related knobs; user can still fine-tune afterward."""
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
        # Apply profile values to dependent spinboxes (block signals not needed — no loops)
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
        # Exit scales live in settings (no dedicated spins) — stage for save
        self.settings["exit_roi_scale"] = float(prof["exit_roi_scale"])
        self.settings["exit_time_scale"] = float(prof["exit_time_scale"])
        self.settings["ttp_arm_scale"] = float(prof["ttp_arm_scale"])
        self.settings["risk_posture"] = key
        # Scale-in: posture supplies default; user can still toggle after
        if hasattr(self, "allow_scale_in_chk"):
            self.allow_scale_in_chk.setChecked(bool(prof.get("allow_scale_in", False)))
        self.settings["allow_scale_in"] = bool(prof.get("allow_scale_in", False))
        if "scale_in_size_frac" in prof:
            self.settings["scale_in_size_frac"] = float(prof["scale_in_size_frac"])
        if "scale_in_max_adds" in prof:
            self.settings["scale_in_max_adds"] = int(prof["scale_in_max_adds"])

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

    def save_custom_settings(self):
        # discord_webhook is owned by the Webhook… dialog (writes on its Save)
        self.settings["discord_alert_level"] = self.discord_lvl_combo.currentText()
        self.settings["discord_heartbeat_schedule"] = self.discord_hb_combo.currentText()
        self.settings["discord_big_win_roi_pct"] = self.discord_big_win_spin.value()
        self.settings["monitor_enabled"] = self.monitor_enabled_chk.isChecked()
        self.settings["monitor_port"] = int(self.monitor_port_spin.value())
        self.settings["monitor_host"] = self.settings.get("monitor_host", "127.0.0.1") or "127.0.0.1"
        self.settings["monitor_user"] = self.monitor_user_input.text().strip()
        self.settings["monitor_pass"] = self.monitor_pass_input.text()
        self.settings["allocation_pct_stock"] = self.alloc_stock_spin.value()
        self.settings["allocation_pct_crypto"] = self.alloc_crypto_spin.value()
        self.settings["allocation_pct"] = self.alloc_stock_spin.value()
        self.settings["min_trade_dollars"] = self.min_dollar_spin.value()
        if hasattr(self, "risk_posture_combo"):
            key = self.risk_posture_combo.currentData()
            self.settings["risk_posture"] = str(key or "balanced").lower()
        if hasattr(self, "allow_scale_in_chk"):
            self.settings["allow_scale_in"] = bool(self.allow_scale_in_chk.isChecked())
        self.settings["target_bp_utilization_pct"] = self.bp_util_spin.value()
        self.settings["sizing_focus_slots"] = int(self.sizing_focus_spin.value())
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
        self.settings["limit_offset_pct"] = self.offset_spin.value()
        self.settings["daily_profit_target"] = self.profit_spin.value()
        self.settings["daily_loss_limit"] = self.loss_spin.value()
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
        self._start_web_monitor()
        QMessageBox.information(self, "Settings Saved", "Configuration updated successfully!")

    def copy_log_to_clipboard(self):
        """Copy the currently visible (filtered) log; fall back to full buffer / disk."""
        text = ""
        if hasattr(self, "log_text_edit"):
            text = self.log_text_edit.toPlainText().strip()
        if not text:
            text = "\n".join(self._filtered_log_lines()).strip()
        if not text:
            try:
                if os.path.isfile(ACTIVITY_LOG_FILE):
                    with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read().strip()
            except Exception:
                text = ""
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
                with open(filename, 'w', encoding="utf-8") as f:
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
                    background-color: #1A1D24; border: 1px solid #2A2F3A;
                    border-radius: {ui_px(UI_RADIUS_FRAME)}px;
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
                    background-color: #FFFFFF; border: 1px solid #C5CAD3;
                    border-radius: {ui_px(UI_RADIUS_FRAME)}px;
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
