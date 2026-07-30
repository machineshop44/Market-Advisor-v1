import os
import sys
import time
import math
import json
import builtins
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_DOWN

from PyQt5.QtWidgets import (QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QTableWidget, QTableWidgetItem, QHeaderView, 
                             QPushButton, QMessageBox, QInputDialog, QLineEdit, 
                             QApplication, QStatusBar, QFrame, QCheckBox, QComboBox,
                             QDoubleSpinBox, QSpinBox, QTextEdit, QFileDialog, QDialog, QFormLayout, QGroupBox,
                             QSystemTrayIcon, QMenu, QAction, QScrollArea, QSizePolicy)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QEventLoop, QPoint, QSize
from PyQt5.QtGui import QPainter, QPen, QColor, QPalette, QPixmap, QPolygon, QIcon
import threading

import journal
import monitor
from broker import RobinhoodAdapter, CoinbaseAdapter
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
        "allocation_pct_crypto": 5.0,
        "allocation_pct_stock": 5.0,
        "min_trade_dollars": 5.0,
        "limit_offset_pct": 0.1,
        "daily_profit_target": 0.0,
        "daily_loss_limit": 8.0,
        "max_open_positions": 6,
        "max_buys_per_cycle": 2,
        "interval_crypto": 45,
        "interval_penny": 60,
        "interval_core": 300,
        "interval_portfolio": 45,
        "interval_balance_refresh": 60,
        "rh_email": "",
        "rh_password": "",
        "cb_api_key": "",
        "cb_api_secret": ""
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
            self.error_occurred.emit(str(e))


class MarketAdvisorGUI(QMainWindow):
    _launch_discord_finished = pyqtSignal(bool, str)
    _log_line_ready = pyqtSignal(str)

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
        
        self.settings = load_settings()
        self.dark_mode = self.settings.get("dark_mode", False)
        self.paper_mode = self.settings.get("paper_mode", False)
        
        # Initialize Broker Adapters
        self.brokers = {
            "Robinhood": RobinhoodAdapter(),
            "Coinbase": CoinbaseAdapter()
        }
        try:
            from scoring import register_regime_brokers
            register_regime_brokers(
                robinhood=self.brokers["Robinhood"],
                coinbase=self.brokers["Coinbase"],
            )
        except Exception:
            pass
        self.active_broker_name = "Robinhood"
        self.view_mode = "All"  # Dropdown: All | Robinhood | Coinbase
        self.penny_tab_index = 3
        self.core_tab_index = 4
        self._last_balance_totals = {"Robinhood": {'p_val': 0.0, 'bp': 0.0}, "Coinbase": {'p_val': 0.0, 'bp': 0.0}}
        
        self.auto_trade_enabled = {"Robinhood": False, "Coinbase": False}
        self.task_queue = []
        self.is_processing_queue = False
        self._cycle_broker = None  # Broker locked for the in-flight auto-trade cycle
        self._queue_started_at = None
        self._stall_alerted = False
        self._reconnect_cooldown = {"Robinhood": 0.0, "Coinbase": 0.0}
        self._reconnect_fail_streak = {"Robinhood": 0, "Coinbase": 0}
        self._reconnect_in_flight = {"Robinhood": False, "Coinbase": False}
        self._holdings_count_cache = {"Robinhood": 0, "Coinbase": 0}
        self._balance_bad_streak = {"Robinhood": 0, "Coinbase": 0}
        self._balances_refresh_in_flight = False
        self._last_idle_balance_refresh = 0.0
        self.cost_basis_cache = {"Robinhood": {}, "Coinbase": {}}
        self._scoring_state_loaded = False
        self.last_crypto_time = {"Robinhood": 0, "Coinbase": 0}
        self.last_penny_time = {"Robinhood": 0, "Coinbase": 0}
        self.last_core_time = {"Robinhood": 0, "Coinbase": 0}
        self.last_port_time = {"Robinhood": 0, "Coinbase": 0}
        
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
        
        self.trade_locks = {}
        self._portfolio_fingerprint = ""
        self.sandbox_cash = {"Robinhood": 10000.00, "Coinbase": 10000.00}
        self.sandbox_holdings = {"Robinhood": {}, "Coinbase": {}}  # {ticker: {'shares':, 'cost':, 'type':}}
        
        # Track P&L Independently per broker
        self.session_starts = {
            "Robinhood": None,
            "Coinbase": None
        }
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

    def _finish_ui_build(self):
        """Deferred scanner/portfolio/settings tabs — window + tray already visible."""
        if getattr(self, "_trading_tabs_built", False):
            return
        self._trading_tabs_built = True
        self.build_portfolio_screen()
        self.build_crypto_screen()
        self.build_penny_screen()
        self.build_core_screen()
        self.build_activity_log_screen()
        self.build_settings_screen()

        self.penny_tab_index = 3
        self.core_tab_index = 4
        for i in range(self.tabs.count()):
            title = self.tabs.tabText(i)
            if "Breakout" in title:
                self.penny_tab_index = i
            elif "Core" in title:
                self.core_tab_index = i

        self._apply_view_mode_tabs()
        # Scale once deferred tabs exist (action buttons / section headers tagged)
        self._apply_ui_scale()
        QTimer.singleShot(0, lambda: self.director_timer.start(1000))

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
            self.log_event("System tray unavailable — close will exit the app.")
            return

        self.tray_icon = QSystemTrayIcon(self.app_icon, self)
        self.tray_icon.setToolTip(display_name())

        menu = QMenu()
        show_act = QAction(f"Open {APP_NAME}", self)
        show_act.triggered.connect(self.show_from_tray)
        menu.addAction(show_act)
        menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.quit_from_tray)
        menu.addAction(quit_act)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        # Double-click or single left-click restores the window
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self):
        self._force_quit = True
        self.close()

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
    #  GUI INPUT INTERCEPTOR FOR 2FA
    # ---------------------------------------------------------
    def _gui_input_prompt(self, prompt):
        code, ok = QInputDialog.getText(self, "2FA Required", f"{prompt}\n\nEnter the SMS code sent to your phone:")
        return code if ok else ""

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
        save_settings(self.settings)

    def _restore_session_baselines(self):
        today = str(datetime.now().date())
        if self.settings.get("pnl_baseline_date") != today:
            return
        rh = self.settings.get("pnl_baseline_rh")
        cb = self.settings.get("pnl_baseline_cb")
        if isinstance(rh, (int, float)) and rh > 0:
            self.session_starts["Robinhood"] = float(rh)
        if isinstance(cb, (int, float)) and cb > 0:
            self.session_starts["Coinbase"] = float(cb)

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
            return 0.0, 0.0
        return broker.get_account_balances()

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
        # Coinbase API has no reliable avg cost — overlay tracked buys; else seed live price
        cache = self.cost_basis_cache.get(broker_name, {})
        for a in assets:
            if a['ticker'] in cache and cache[a['ticker']] > 0:
                a['cost'] = cache[a['ticker']]
            elif broker_name == "Coinbase" and (not a.get('cost') or a['cost'] <= 0):
                live = broker.get_live_price(a['ticker']) if broker else 0.0
                a['cost'] = live if live and live > 0 else 0.0
                if a['cost'] > 0:
                    cache[a['ticker']] = a['cost']
        return assets

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
        any_down = False

        # Always show both brokers (armed or not) so a false loss-halt doesn't "erase" RH from Discord
        for name in ("Robinhood", "Coinbase"):
            connected = self.brokers[name].is_connected or self.paper_mode
            armed = bool(self.auto_trade_enabled.get(name))
            if not connected:
                any_down = True
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

        # Side color: red if any broker down or day down; green if day up; blue if flat
        if any_down or combined_pl < -0.001:
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
        for name in ("Robinhood", "Coinbase"):
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
            for n in ("Robinhood", "Coinbase")
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
            self.session_starts = {"Robinhood": None, "Coinbase": None}
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
                if self.paper_mode or broker.is_connected:
                    p_val, bp = self.get_broker_balances(name)
                    totals[name] = {
                        "p_val": float(p_val or 0.0),
                        "bp": float(bp or 0.0),
                        "ok": True,
                    }
                else:
                    # Disconnected — do not invent $0 equity (that fake-trips day-loss limits)
                    totals[name] = {"ok": False, "reason": "disconnected"}
            except Exception as e:
                totals[name] = {"ok": False, "reason": str(e)}
                try:
                    if sys.stdout is not None:
                        print(f"balance error [{name}]: {e}")
                except Exception:
                    pass
        return totals

    def _balance_reading_is_suspicious(self, broker_name, new_p, old_p, baseline):
        """
        True when a new equity print looks like a failed API read, not a real wipe.
        Classic bug: overnight RH glitch → $0 equity → Day P&L ≈ −entire account → false loss halt.
        """
        new_p = float(new_p or 0.0)
        old_p = float(old_p or 0.0)
        baseline = float(baseline or 0.0) if baseline else 0.0

        # Near-zero when we recently had real money
        if new_p <= 0.50 and old_p >= 5.0:
            return True
        if new_p <= 0.50 and baseline >= 5.0:
            return True

        # Sudden collapse vs last good reading (keep real large moves after 2 confirms)
        if old_p >= 20.0 and new_p < old_p * 0.35 and (old_p - new_p) >= 15.0:
            return True

        return False

    def _merge_balance_totals(self, incoming):
        """Keep last-good equity when a broker fetch fails or returns a suspicious wipe."""
        prev = getattr(self, "_last_balance_totals", {}) or {}
        if not hasattr(self, "_balance_bad_streak"):
            self._balance_bad_streak = {"Robinhood": 0, "Coinbase": 0}

        merged = {}
        trusted = {"Robinhood": False, "Coinbase": False}

        for name in ("Robinhood", "Coinbase"):
            raw = incoming.get(name) if isinstance(incoming, dict) else None
            old = prev.get(name) or {}
            old_p = float(old.get("p_val", 0.0) or 0.0)
            old_bp = float(old.get("bp", 0.0) or 0.0)
            baseline = self.session_starts.get(name)

            if not isinstance(raw, dict) or raw.get("ok") is False:
                reason = (raw or {}).get("reason", "fetch failed") if isinstance(raw, dict) else "missing"
                self._balance_bad_streak[name] = self._balance_bad_streak.get(name, 0) + 1
                self.log_event(f"[{name}] Balance fetch unreliable ({reason}) — keeping last good equity {format_money(old_p)}")
                merged[name] = {"p_val": old_p, "bp": old_bp}
                trusted[name] = False
                continue

            new_p = float(raw.get("p_val", 0.0) or 0.0)
            new_bp = float(raw.get("bp", 0.0) or 0.0)

            if self._balance_reading_is_suspicious(name, new_p, old_p, baseline):
                streak = self._balance_bad_streak.get(name, 0) + 1
                self._balance_bad_streak[name] = streak
                if streak < 2:
                    self.log_event(
                        f"[{name}] Ignoring suspicious equity {format_money(new_p)} "
                        f"(was {format_money(old_p)}; baseline {format_money(baseline or 0)}) "
                        f"— need another confirming read before trusting it"
                    )
                    merged[name] = {"p_val": old_p, "bp": old_bp}
                    trusted[name] = False
                    continue
                # Second consecutive collapse — accept (could be real)
                self.log_event(
                    f"[{name}] Equity collapse confirmed on 2nd read "
                    f"({format_money(old_p)} → {format_money(new_p)}); accepting"
                )

            self._balance_bad_streak[name] = 0
            merged[name] = {"p_val": new_p, "bp": new_bp}
            trusted[name] = True

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
        for broker_name in ("Robinhood", "Coinbase"):
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
        for broker_name in ["Robinhood", "Coinbase"]:
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
            for name in ("Robinhood", "Coinbase"):
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
                    "Robinhood": bool(self.auto_trade_enabled.get("Robinhood")),
                    "Coinbase": bool(self.auto_trade_enabled.get("Coinbase")),
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
        elif view == "Robinhood":
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
            "Robinhood equities (ET):\n"
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
            fname = getattr(target_func, '__name__', 'unknown')
            self.log_event(f"Thread Error in {fname}: {e}")
            self.set_working_state(False)
            if unlock_queue_on_error:
                self.send_discord_alert(f"🚨 Cycle thread error in {fname}: {e}")
                self.cycle_finished()

        task.result_ready.connect(on_success_callback)
        task.error_occurred.connect(_on_error)
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

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
        self.broker_dropdown.addItems(["All", "Robinhood", "Coinbase"])
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
        """Show/hide Breakouts + Core tabs (Coinbase has no equities)."""
        for idx in (getattr(self, 'penny_tab_index', 3), getattr(self, 'core_tab_index', 4)):
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
        # Coinbase-only: hide equity scanners. All / Robinhood: show them.
        self._set_stock_tabs_visible(self.view_mode != "Coinbase")

    def on_broker_switch(self, broker_name):
        self.view_mode = broker_name
        if broker_name in ("Robinhood", "Coinbase"):
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
            for name in ("Robinhood", "Coinbase"):
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
        ):
            lbl = getattr(self, name, None)
            if lbl is not None:
                lbl.setStyleSheet(
                    f"font-size: {ui_px(14)}px; font-weight: 600; padding: {ui_px(4)}px 2px;"
                )
                lbl.setMinimumHeight(ui_px(24))
        for name in ("home_rh_pl_lbl", "home_cb_pl_lbl"):
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

        # Connections Manager Section
        conn_group = QGroupBox("Broker Connection Manager")
        conn_layout = QFormLayout()

        # Robinhood Login Fields
        self.rh_status_lbl = QLabel("🔴 Disconnected")
        self.rh_email_input = QLineEdit(self.settings.get("rh_email", ""))
        self.rh_pass_input = QLineEdit(self.settings.get("rh_password", ""))
        self.rh_pass_input.setEchoMode(QLineEdit.Password)
        self.rh_connect_btn = QPushButton("Connect Robinhood")
        self.rh_connect_btn.clicked.connect(self.connect_robinhood)

        conn_layout.addRow("Robinhood Status:", self.rh_status_lbl)
        conn_layout.addRow("RH Email:", self.rh_email_input)
        conn_layout.addRow("RH Password:", self.rh_pass_input)
        conn_layout.addRow("", self.rh_connect_btn)

        # Coinbase CDP API Fields
        self.cb_status_lbl = QLabel("🔴 Disconnected")
        self.cb_key_input = QLineEdit(self.settings.get("cb_api_key", ""))
        self.cb_secret_input = QLineEdit(self.settings.get("cb_api_secret", ""))
        self.cb_secret_input.setEchoMode(QLineEdit.Password)
        self.cb_connect_btn = QPushButton("Connect Coinbase Advanced")
        self.cb_connect_btn.clicked.connect(self.connect_coinbase)

        conn_layout.addRow("Coinbase Status:", self.cb_status_lbl)
        conn_layout.addRow("CDP API Key:", self.cb_key_input)
        conn_layout.addRow("CDP API Secret:", self.cb_secret_input)
        conn_layout.addRow("", self.cb_connect_btn)

        conn_group.setLayout(conn_layout)
        layout.addWidget(conn_group)

        # Config Form
        form_layout = QVBoxLayout()

        discord_box = QHBoxLayout()
        discord_box.addWidget(QLabel("Discord Webhook URL:"))
        self.discord_input = QLineEdit(self.settings.get("discord_webhook", ""))
        discord_box.addWidget(self.discord_input)
        form_layout.addLayout(discord_box)

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

        alloc_box = QHBoxLayout()
        alloc_box.addWidget(QLabel("Stock/ETF Max Allocation % (risk ceiling):"))
        self.alloc_stock_spin = QDoubleSpinBox()
        self.alloc_stock_spin.setRange(0.5, 50.0)
        stock_default = self.settings.get("allocation_pct_stock", self.settings.get("allocation_pct", 5.0))
        self.alloc_stock_spin.setValue(stock_default)
        alloc_box.addWidget(self.alloc_stock_spin)
        alloc_box.addStretch()
        form_layout.addLayout(alloc_box)

        alloc_crypto_box = QHBoxLayout()
        alloc_crypto_box.addWidget(QLabel("Crypto Max Allocation % (risk ceiling):"))
        self.alloc_crypto_spin = QDoubleSpinBox()
        self.alloc_crypto_spin.setRange(0.5, 50.0)
        crypto_default = self.settings.get("allocation_pct_crypto", self.settings.get("allocation_pct", 5.0))
        self.alloc_crypto_spin.setValue(crypto_default)
        alloc_crypto_box.addWidget(self.alloc_crypto_spin)
        alloc_crypto_box.addStretch()
        form_layout.addLayout(alloc_crypto_box)

        alloc_hint = QLabel(
            "Primary size is risk-based (~0.75% equity / hard-stop distance). "
            "Allocation % caps notional; 12% cash reserve is always held back."
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
        self.max_pos_spin.setValue(int(self.settings.get("max_open_positions", 6)))
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

    def connect_robinhood(self):
        email = self.rh_email_input.text().strip()
        password = self.rh_pass_input.text().strip()
        
        # Patch input to elegantly handle 2FA SMS popups
        original_input = builtins.input
        builtins.input = self._gui_input_prompt
        
        try:
            success, msg = self.brokers["Robinhood"].login({'email': email, 'password': password})
            if success:
                self.rh_status_lbl.setText("🟢 Connected")
                self.rh_status_lbl.setStyleSheet("color: #00E676; font-weight: bold;")
                self.settings["rh_email"] = email
                self.settings["rh_password"] = password
                save_settings(self.settings)
                self.refresh_account_balances()
            else:
                QMessageBox.warning(self, "Connection Failed", f"Robinhood: {msg}")
        finally:
            builtins.input = original_input

    def connect_coinbase(self):
        key = self.cb_key_input.text().strip()
        secret = self.cb_secret_input.text().strip()
        success, msg = self.brokers["Coinbase"].login({'api_key': key, 'api_secret': secret})
        if success:
            self.cb_status_lbl.setText("🟢 Connected")
            self.cb_status_lbl.setStyleSheet("color: #00E676; font-weight: bold;")
            self.settings["cb_api_key"] = key
            self.settings["cb_api_secret"] = secret
            save_settings(self.settings)
            self.refresh_account_balances()
        else:
            QMessageBox.warning(self, "Connection Failed", f"Coinbase: {msg}")

    def run_startup_sequence(self):
        """Show UI immediately; connect brokers off the main thread (avoids freeze on open)."""
        self.log_event("Connecting brokers in background...")
        if hasattr(self, "rh_status_lbl"):
            self.rh_status_lbl.setText("🟡 Connecting…")
            self.rh_status_lbl.setStyleSheet("color: #FFD54F; font-weight: bold;")
        if hasattr(self, "cb_status_lbl"):
            self.cb_status_lbl.setText("🟡 Connecting…")
            self.cb_status_lbl.setStyleSheet("color: #FFD54F; font-weight: bold;")
        self.set_working_state(True, "Connecting brokers…")
        task = BackgroundTask(self._bg_startup_connect)
        task.result_ready.connect(self._on_startup_connected)
        task.error_occurred.connect(lambda e: self._on_startup_connected({"rh_ok": False, "cb_ok": False, "error": str(e)}))
        task.finished.connect(lambda: self.active_threads.remove(task) if task in self.active_threads else None)
        self.active_threads.append(task)
        task.start()

    def _bg_startup_connect(self):
        """Network logins only — never call QInputDialog from this thread."""
        result = {"rh_ok": False, "cb_ok": False, "rh_needs_password": False}
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
                })
                result["cb_ok"] = bool(ok)
            except Exception:
                result["cb_ok"] = False
        return result

    def _startup_rh_password_login(self):
        """Main-thread RH login so SMS 2FA dialogs stay safe."""
        original_input = builtins.input
        builtins.input = self._gui_input_prompt
        try:
            rh_ok, msg = self.brokers["Robinhood"].login({
                "email": self.settings.get("rh_email", ""),
                "password": self.settings.get("rh_password", ""),
                "store_session": True,
            })
            if rh_ok:
                self.rh_status_lbl.setText("🟢 Connected")
                self.rh_status_lbl.setStyleSheet("color: #00E676; font-weight: bold;")
                self.log_event("Robinhood connected (password / 2FA path).")
            else:
                self.rh_status_lbl.setText("🔴 Disconnected")
                self.rh_status_lbl.setStyleSheet("color: #FF5252; font-weight: bold;")
                self.log_event(f"Robinhood login failed: {msg}")
        finally:
            builtins.input = original_input

    def _on_startup_connected(self, result):
        result = result or {}
        if result.get("cb_ok"):
            self.cb_status_lbl.setText("🟢 Connected")
            self.cb_status_lbl.setStyleSheet("color: #00E676; font-weight: bold;")
            self.log_event("Coinbase connected.")
        elif self.settings.get("cb_api_key"):
            self.cb_status_lbl.setText("🔴 Disconnected")
            self.cb_status_lbl.setStyleSheet("color: #FF5252; font-weight: bold;")
            self.log_event("Coinbase login failed.")
        else:
            self.cb_status_lbl.setText("🔴 Disconnected")

        if result.get("rh_ok"):
            self.rh_status_lbl.setText("🟢 Connected")
            self.rh_status_lbl.setStyleSheet("color: #00E676; font-weight: bold;")
            self.log_event("Robinhood connected (saved session).")
        elif result.get("rh_needs_password"):
            self._startup_rh_password_login()
        else:
            self.rh_status_lbl.setText("🔴 Disconnected")
            self.rh_status_lbl.setStyleSheet("color: #FF5252; font-weight: bold;")

        self.set_working_state(False)
        if result.get("error"):
            self.log_event(f"Startup connect error: {result.get('error')}")
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
            self._set_engine_banner("Auto-Trader Armed")
        else:
            self.auto_trade_btn.setText("Auto-Trader: OFF")
            self.auto_trade_btn.setStyleSheet(top_bar_btn_style("#424242"))
            self.at_status_frame.setVisible(False)
            self._reset_autotrader_banner_style()

    def _disarm_broker(self, broker_name, notify_discord=False):
        self.auto_trade_enabled[broker_name] = False
        self.log_event(f"Auto-Trader disabled for {broker_name}.")
        self._update_autotrade_ui()
        if notify_discord:
            self.send_discord_alert(f"🛑 Auto-Trader **DISARMED** for **{broker_name}**.")

    def toggle_auto_trade(self):
        # If anything is currently running, this button acts as a single kill switch.
        if any(self.auto_trade_enabled.values()):
            was = [b for b, on in self.auto_trade_enabled.items() if on]
            for broker_name in list(self.auto_trade_enabled.keys()):
                self.auto_trade_enabled[broker_name] = False
            self.task_queue.clear()
            self._cycle_broker = None
            self.is_processing_queue = False
            self._queue_started_at = None
            self._stall_alerted = False
            self.log_event("Auto-Trader disabled for all brokers.")
            self._update_autotrade_ui()
            mode = "PAPER" if self.paper_mode else "LIVE"
            self.send_discord_alert(
                f"🛑 Auto-Trader **DISARMED** ({mode}) — stopped: {', '.join(was) or 'none'}."
            )
            return

        # Nothing running yet -> ask which broker(s) to arm.
        choices = ["Robinhood Only", "Coinbase Only", "Both Robinhood + Coinbase"]
        choice, ok = QInputDialog.getItem(self, "Select Broker(s)", "Run Auto-Trader on:", choices, 2, False)
        if not ok:
            return

        targets = []
        if choice == "Robinhood Only":
            targets = ["Robinhood"]
        elif choice == "Coinbase Only":
            targets = ["Coinbase"]
        else:
            targets = ["Robinhood", "Coinbase"]

        armed = []
        for broker_name in targets:
            if not self.brokers[broker_name].is_connected and not self.paper_mode:
                QMessageBox.warning(
                    self, "Broker Disconnected",
                    f"Skipping {broker_name}: please connect it in Settings first (or enable Paper Mode)."
                )
                continue
            if self.session_starts[broker_name] is None:
                # Prefer cached equity baseline; never lock in $0 before balances arrive
                if self.paper_mode:
                    self.session_starts[broker_name] = 10000.00
                else:
                    cached = float(
                        (self._last_balance_totals.get(broker_name) or {}).get("p_val", 0.0) or 0.0
                    )
                    if cached > 0:
                        self.session_starts[broker_name] = cached
                        self._persist_session_baselines()
                    # else leave None — first balance fetch will seed Day P&L baseline
            self.auto_trade_enabled[broker_name] = True
            # Force an immediate first pulse for this broker (don't wait a full interval)
            self.last_crypto_time[broker_name] = 0
            self.last_port_time[broker_name] = 0
            self.last_penny_time[broker_name] = 0
            self.last_core_time[broker_name] = 0
            armed.append(broker_name)

        if not armed:
            self.log_event("Auto-Trader arm failed — no eligible brokers.")
            self._update_autotrade_ui()
            return

        # Instant UI feedback — never block the click on live balance API calls
        self._update_autotrade_ui()
        self._set_engine_banner("🤖 ⚡ Auto-Trader Armed — spinning up…")
        QTimer.singleShot(0, self.director_tick)

        # Logs / Discord use cached buying power (refresh in background for accuracy)
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
        # Refresh balances off the UI thread; does not delay the armed state
        self.refresh_account_balances()

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
                task = (broker_name, "CRYPTO")
                if task not in self.task_queue: self.task_queue.append(task)
                self.last_crypto_time[broker_name] = now

            # Portfolio / sell checks: both brokers, 24/7 (crypto holdings don't care about equity hours)
            if now - self.last_port_time[broker_name] >= self.settings.get("interval_portfolio", 45):
                task = (broker_name, "PORTFOLIO")
                if task not in self.task_queue: self.task_queue.append(task)
                self.last_port_time[broker_name] = now

            # Stocks: Robinhood only — run in regular AND extended (not weekend/closed night)
            if broker_name != "Coinbase" and self.is_equity_session_active():
                if now - self.last_penny_time[broker_name] >= self.settings.get("interval_penny", 60):
                    task = (broker_name, "PENNY")
                    if task not in self.task_queue: self.task_queue.append(task)
                    self.last_penny_time[broker_name] = now

                if now - self.last_core_time[broker_name] >= self.settings.get("interval_core", 300):
                    task = (broker_name, "CORE")
                    if task not in self.task_queue: self.task_queue.append(task)
                    self.last_core_time[broker_name] = now

        self.process_queue()

        # Quiet idle balance poll — keeps top-bar equity/P&L fresh even when auto-trader is off
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
                    # Prefer saved session (no MFA). Password fallback is best-effort only.
                    ok, detail = self.brokers["Robinhood"].login({})
                    if ok:
                        return True, "session restored"
                    if email and password:
                        ok, detail = self.brokers["Robinhood"].login({
                            "email": email, "password": password, "store_session": True
                        })
                        return bool(ok), detail
                    return False, detail or "missing saved credentials"
                if broker_name == "Coinbase":
                    if cb_key and cb_secret:
                        ok, detail = self.brokers["Coinbase"].login({
                            "api_key": cb_key, "api_secret": cb_secret
                        })
                        return bool(ok), detail
                    return False, "missing saved API keys"
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
                self.log_event(f"[{broker_name}] Reconnected successfully.")
                self.send_discord_alert(f"✅ [{broker_name}] Session restored after drop.")
                self.refresh_account_balances()
            else:
                streak = self._reconnect_fail_streak.get(broker_name, 0) + 1
                self._reconnect_fail_streak[broker_name] = streak
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
        self._cycle_broker = broker_name
        self._set_trading_context(broker_name)
        self.log_event(f"[AUTO] Starting {task} cycle on {broker_name}")

        if task == "CRYPTO": self.run_crypto_cycle()
        elif task == "PENNY": self.run_penny_cycle()
        elif task == "CORE": self.run_core_cycle()
        elif task == "PORTFOLIO": self.run_portfolio_cycle()
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
        for a in sorted(assets, key=lambda x: (str(x.get('broker', '')), str(x.get('ticker', '')))):
            parts.append(f"{a.get('broker','')}:{a.get('ticker','')}:{float(a.get('shares') or 0):.8f}")
        return "|".join(parts)

    def _refresh_holdings_count_cache(self, assets=None):
        """Update cached holdings counts without hitting broker APIs on the UI thread."""
        cache = getattr(self, "_holdings_count_cache", None)
        if cache is None:
            self._holdings_count_cache = {"Robinhood": 0, "Coinbase": 0}
            cache = self._holdings_count_cache
        if assets is None:
            return cache
        counts = {"Robinhood": 0, "Coinbase": 0}
        for a in assets:
            name = a.get("broker") or ""
            if name in counts:
                counts[name] += 1
        # When view is a single broker, only refresh that broker's count
        if self.view_mode in ("Robinhood", "Coinbase"):
            cache[self.view_mode] = counts.get(self.view_mode, 0)
        else:
            cache.update(counts)
        return cache

    def _load_holdings_for_view(self):
        """Returns holdings for the current view_mode (All = both brokers tagged), with live prices."""
        if self.view_mode == "All":
            combined = []
            for name in ("Robinhood", "Coinbase"):
                for a in self.get_broker_holdings(name):
                    row = dict(a)
                    row["broker"] = name
                    combined.append(row)
            assets = combined
        else:
            name = self.view_mode if self.view_mode in self.brokers else self.active_broker_name
            assets = self.get_broker_holdings(name)
            for a in assets:
                a["broker"] = name

        for a in assets:
            broker_name = a.get("broker") or self.active_broker_name
            broker = self.brokers.get(broker_name)
            try:
                a["live_price"] = float(broker.get_live_price(a["ticker"]) if broker else 0.0) or 0.0
            except Exception:
                a["live_price"] = 0.0

        # Keep monitor counts fresh without UI-thread API calls
        if self.view_mode == "All":
            self._holdings_count_cache = {
                "Robinhood": sum(1 for a in assets if a.get("broker") == "Robinhood"),
                "Coinbase": sum(1 for a in assets if a.get("broker") == "Coinbase"),
            }
        elif self.view_mode in ("Robinhood", "Coinbase"):
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
            if self.view_mode in ("Robinhood", "Coinbase"):
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
        self.portfolio_table.setRowCount(len(assets))
        for row, a in enumerate(assets):
            broker_name = a.get('broker') or self.active_broker_name
            t_item = QTableWidgetItem(a['ticker'])
            t_item.setCheckState(Qt.Checked)
            t_item.setData(Qt.UserRole, a.get('type', ''))
            t_item.setData(Qt.UserRole + 1, broker_name)

            price = float(a.get("live_price") or 0.0)

            self.portfolio_table.setItem(row, 0, QTableWidgetItem(broker_name))
            self.portfolio_table.setItem(row, 1, t_item)
            self.portfolio_table.setItem(row, 2, QTableWidgetItem(format_quantity(a['shares'])))
            self.portfolio_table.setItem(row, 3, QTableWidgetItem(format_currency(a.get('cost') or 0)))
            self.portfolio_table.setItem(row, 4, QTableWidgetItem(format_currency(price)))
            self.portfolio_table.setItem(row, 5, QTableWidgetItem(format_currency(a['shares'] * price)))
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

    def calculate_order_sizing(self, current_bp, asset_type="", entry_price=0.0, equity=None):
        """
        Risk-based primary size: (equity * 0.75%) / hard_stop_distance.
        allocation_pct_* remains a hard ceiling; cash reserve applied inside calculate_risk_sizing.
        """
        from scoring import calculate_risk_sizing, get_stop_distance_pct
        is_crypto = "crypto" in str(asset_type).lower()
        if is_crypto:
            alloc_pct = self.settings.get("allocation_pct_crypto", self.settings.get("allocation_pct", 5.0)) / 100.0
        else:
            alloc_pct = self.settings.get("allocation_pct_stock", self.settings.get("allocation_pct", 5.0)) / 100.0
        min_dollars = self.settings.get("min_trade_dollars", 5.0)
        broker_id = getattr(self.cycle_broker, "broker_id", None) or self.cycle_broker_name.upper()
        stop_d = get_stop_distance_pct(broker_id, asset_type=asset_type)
        eq = float(equity) if equity is not None else None
        if eq is None:
            try:
                eq, _ = self.get_broker_balances(self.cycle_broker_name)
            except Exception:
                eq = float(current_bp or 0.0)
        return calculate_risk_sizing(
            eq, current_bp, stop_d, alloc_pct, min_dollars=min_dollars,
        )

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
            broker_id = broker_name.upper()
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
        broker_id = broker_name.upper()
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
            lambda: self._bg_execute_sell_batch(sell_list),
            lambda payload: self._on_sell_batch_done(payload, auto_mode=auto_mode, finish_cycle=False)
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
            trade_dollars = self.calculate_order_sizing(bp, sample_type, equity=equity)
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
        runner(
            lambda: self._bg_buy_batch(filtered, rank=(buy_candidates is None and auto_mode)),
            lambda payload: self._on_buy_batch_done(payload, auto_mode=auto_mode, table=table)
        )

    def _bg_buy_batch(self, candidates, rank=False):
        """Place buys on a worker thread (confirm_order sleeps stay off the UI)."""
        from scoring import concentration_blocks_buy
        broker_name = self.cycle_broker_name
        offset = self.settings.get("limit_offset_pct", 0.1) / 100.0
        session = self.get_equity_session_info()
        use_ext = session["use_ext"]
        market_hours = session["market_hours"]
        allow_fractional = session["fractional_ok"]
        max_positions = int(self.settings.get("max_open_positions", 6))
        max_buys = int(self.settings.get("max_buys_per_cycle", 2))
        notes = []
        ranked = list(candidates or [])

        if rank and len(ranked) > 1:
            with SuppressPrints():
                for c in ranked:
                    ticker = c.get("ticker") or ""
                    asset_type = c.get("asset_type") or ""
                    is_crypto = "crypto" in str(asset_type).lower() or ticker.upper() in KNOWN_CRYPTOS
                    try:
                        from scoring import buy_rank_score
                        c["score"] = float(buy_rank_score(ticker, is_crypto=is_crypto))
                    except Exception:
                        c["score"] = 0.0
            ranked.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            top = ", ".join(f"{c.get('ticker')}({float(c.get('score') or 0):.0f})" for c in ranked[:3])
            notes.append(f"[{broker_name}] Ranked {len(ranked)} buys — top: {top}")

        equity, bp = self.get_broker_balances(broker_name)
        holdings = self.get_broker_holdings(broker_name) or []
        held = {a["ticker"].upper() for a in holdings}
        open_count = len(held)
        # Refresh monitor cache from this bg holdings pull
        self._holdings_count_cache[broker_name] = open_count

        holdings_meta = []
        broker = self.brokers.get(broker_name)
        for a in holdings:
            t = a.get("ticker") or ""
            is_c = "crypto" in str(a.get("type") or "").lower() or str(t).upper() in KNOWN_CRYPTOS
            px = 0.0
            try:
                px = float(broker.get_live_price(t) if broker else 0.0) or 0.0
            except Exception:
                px = 0.0
            holdings_meta.append({
                "ticker": t,
                "value": float(a.get("shares") or 0) * px,
                "is_crypto": is_c,
            })

        fills = []
        buys_done = 0
        for c in ranked:
            ticker = c.get("ticker") or ""
            asset_type = c.get("asset_type") or ""
            price = float(c.get("price") or 0.0)
            is_crypto = "crypto" in str(asset_type).lower() or ticker.upper() in KNOWN_CRYPTOS
            if buys_done >= max_buys:
                notes.append(f"[{broker_name}] Buy cap reached ({max_buys}/cycle) — stopping this pulse")
                break
            if max_positions > 0 and open_count >= max_positions:
                notes.append(f"[{broker_name}] Max open positions ({max_positions}) — skipping further buys")
                break
            if ticker.upper() in held:
                notes.append(f"[{broker_name}] Skipped [{ticker}]: already holding")
                continue
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
            # Size first so crypto-book check can use proposed dollars
            row_dollars = self.calculate_order_sizing(bp, asset_type, entry_price=price, equity=equity)
            if row_dollars <= 0:
                notes.append(f"[{broker_name}] Skipping buys — buying power/risk size too low ({format_currency(bp)})")
                break
            blocked, reason = concentration_blocks_buy(
                ticker, held, holdings_meta=holdings_meta, portfolio_value=equity,
                proposed_dollars=row_dollars, is_crypto=is_crypto,
            )
            if blocked:
                notes.append(f"[{broker_name}] Skipped [{ticker}]: concentration — {reason}")
                continue
            status, spent = self.execute_buy_order(
                ticker, asset_type, price, row_dollars, offset, use_ext,
                market_hours=market_hours, allow_fractional=allow_fractional,
            )
            ok = "Fail" not in status and "Skipped" not in status
            if ok:
                held.add(ticker.upper())
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
            self.log_event(f"[{broker}] Execution [{ticker}]: {status}")
            self.send_discord_alert(f"BUY {ticker}: {status}", is_trade=True)
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
                    broker_id=broker_name.upper(),
                    asset_type=asset_type,
                    live_price=price
                )
                results.append((row, price, action, asset_type, None))
        flush_state()
        return results

    def _bg_score_opportunities(self, items):
        from scoring import evaluate_crypto_opportunity, evaluate_opportunity
        results = []
        # Stocks/ETFs always price+execute context via Robinhood; crypto uses the cycle broker
        rh = self.brokers.get("Robinhood")
        with SuppressPrints():
            for entry in items:
                row, ticker, shares, avg_cost, asset_type = entry[:5]
                is_crypto = "crypto" in str(asset_type).lower() or ticker.upper() in KNOWN_CRYPTOS
                is_penny = asset_type == "Penny Stock" or "mover" in str(asset_type).lower()
                if is_crypto:
                    broker = self.cycle_broker
                    broker_id = self.cycle_broker_name.upper()
                else:
                    broker = rh
                    broker_id = "ROBINHOOD"
                price = broker.get_live_price(ticker) if broker else 0.0
                if is_crypto:
                    action = evaluate_crypto_opportunity(ticker, broker_id=broker_id, live_price=price)
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
        table.setRowCount(len(opps))
        for row, item in enumerate(opps):
            t_item = QTableWidgetItem(item['symbol'])
            t_item.setCheckState(Qt.Checked)
            t_item.setData(Qt.UserRole, item['type'])
            table.setItem(row, 0, t_item)
            table.setItem(row, 1, QTableWidgetItem(item['type']))
            table.setItem(row, 2, QTableWidgetItem("Pending..."))
            table.setItem(row, 3, QTableWidgetItem("Pending..."))
            table.setItem(row, 4, QTableWidgetItem("Ready"))

    def _bg_scan_and_score(self, scan_func):
        """Discover tickers, score, and pre-rank BUY candidates in one background job."""
        opps = scan_func() or []
        if not opps:
            return [], [], []
        items = [(i, o['symbol'], 0.0, 0.0, o.get('type', '')) for i, o in enumerate(opps)]
        results = self._bg_score_opportunities(items)
        buy_candidates = []
        with SuppressPrints():
            for row, price, action, asset_type, err in results:
                action_u = str(action).upper()
                if "BUY" not in action_u or "DO NOT BUY" in action_u:
                    continue
                if row >= len(opps):
                    continue
                ticker = opps[row].get("symbol") or ""
                atype = asset_type or opps[row].get("type", "")
                is_crypto = "crypto" in str(atype).lower() or ticker.upper() in KNOWN_CRYPTOS
                try:
                    from scoring import buy_rank_score
                    score = float(buy_rank_score(ticker, is_crypto=is_crypto))
                except Exception:
                    score = 0.0
                buy_candidates.append({
                    "ticker": ticker,
                    "asset_type": atype,
                    "price": float(price or 0.0),
                    "score": score,
                    "table_row": row,
                })
        buy_candidates.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return opps, results, buy_candidates

    def _bg_portfolio_load_and_score(self, broker_name):
        """Load one broker's holdings and score them in one job. Returns (assets, results)."""
        assets = []
        for a in self.get_broker_holdings(broker_name) or []:
            row = dict(a)
            row['broker'] = broker_name
            assets.append(row)
        self._holdings_count_cache[broker_name] = len(assets)
        if not assets:
            return [], []
        items = [
            (i, a['ticker'], a.get('shares', 0.0), a.get('cost', 0.0), a.get('type', ''), broker_name)
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
            lambda b=broker: self._bg_portfolio_load_and_score(b),
            self._port_on_scored
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
            self._patch_portfolio_row_action(broker, a['ticker'], price, action)
            if "SELL" in str(action).upper():
                sell_list.append({
                    'broker': broker,
                    'ticker': a['ticker'],
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
                lambda: self._bg_execute_sell_batch(actionable),
                lambda res: self._on_sell_batch_done(res, auto_mode=True, finish_cycle=True)
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
            lambda: self._bg_execute_sell_batch(sell_list),
            lambda res: self._on_sell_batch_done(res, auto_mode=auto_mode, finish_cycle=auto_mode)
        )

    def run_crypto_cycle(self):
        broker = self.cycle_broker_name
        if self._is_broker_auto_trading(broker):
            self._set_engine_banner(f"🤖 🪙 [{broker}] CRYPTO — scan + score...", "#FFB300")
        self.set_working_state(True, f"Crypto scan+score ({broker})...")
        self.crypto_table.setRowCount(0)
        self.run_cycle_thread(
            lambda: self._bg_scan_and_score(self._bg_scan_crypto),
            self._crypto_on_scored
        )

    def _crypto_on_scored(self, payload):
        opps, results, buy_candidates = self._unpack_scan_payload(payload)
        self._apply_scored_opportunities(self.crypto_table, opps, results)
        buy_count = len(buy_candidates) if buy_candidates else self._count_buy_signals(results)
        self.log_event(f"[AUTO] [{self.cycle_broker_name}] CRYPTO scored — {buy_count} BUY signal(s)")
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
            lambda: self._bg_scan_and_score(self._bg_scan_penny),
            self._penny_on_scored
        )

    def _penny_on_scored(self, payload):
        opps, results, buy_candidates = self._unpack_scan_payload(payload)
        self._apply_scored_opportunities(self.penny_table, opps, results)
        buy_count = len(buy_candidates) if buy_candidates else self._count_buy_signals(results)
        self.log_event(f"[AUTO] [{self.cycle_broker_name}] BREAKOUT scored — {buy_count} BUY signal(s)")
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
            lambda: self._bg_scan_and_score(self._bg_scan_core),
            self._core_on_scored
        )

    def _core_on_scored(self, payload):
        opps, results, buy_candidates = self._unpack_scan_payload(payload)
        self._apply_scored_opportunities(self.core_table, opps, results)
        buy_count = len(buy_candidates) if buy_candidates else self._count_buy_signals(results)
        self.log_event(f"[AUTO] [{self.cycle_broker_name}] CORE scored — {buy_count} BUY signal(s)")
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
        """Normalize (opps, results[, buy_candidates]) from bg scan jobs."""
        if not payload:
            return [], [], []
        if isinstance(payload, (list, tuple)):
            if len(payload) >= 3:
                return payload[0] or [], payload[1] or [], payload[2] or []
            if len(payload) == 2:
                return payload[0] or [], payload[1] or [], []
        return [], [], []

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

    def toggle_dark_mode(self):
        self.dark_mode = not self.dark_mode
        self.settings["dark_mode"] = self.dark_mode
        save_settings(self.settings)
        self.dark_mode_btn.setText("Light" if self.dark_mode else "Dark")
        self.apply_theme()
        self._style_home_cards()
        self._reset_autotrader_banner_style()
        self.update_market_status()

    def save_custom_settings(self):
        self.settings["discord_webhook"] = self.discord_input.text().strip()
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
        save_settings(self.settings)
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
        QApplication.processEvents()

    def apply_theme(self):
        arrow = combo_arrow_path(self.dark_mode)
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
                QLineEdit, QDoubleSpinBox, QSpinBox, QTextEdit {{
                    background-color: #1A1D24; color: #E8EAED;
                    border: 1px solid #2A2F3A; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(8)}px;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
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
                QCheckBox {{ spacing: {ui_px(8)}px; }}
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
                QLineEdit, QDoubleSpinBox, QSpinBox, QTextEdit {{
                    background-color: #FFFFFF; color: #1A1A1A;
                    border: 1px solid #C5CAD3; border-radius: {ui_px(UI_RADIUS_INPUT)}px;
                    padding: {ui_px(5)}px {ui_px(8)}px;
                    selection-background-color: {accent}; selection-color: #FFFFFF;
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
