import os
import sys

# MUST run before QApplication — remaps taskbar away from pythonw.exe
APP_USER_MODEL_ID = "machineshop44.MarketAdvisor.1"
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox, QSplashScreen, QMenu, QAction

from version import APP_NAME, display_name, splash_subtitle, window_title, __version__ as APP_VERSION


def _icon_path():
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("app_icon.png", "app_icon.ico"):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


def _load_app_icon():
    path = _icon_path()
    if path:
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    return QIcon()


def _make_splash(app_icon):
    """Lightweight splash so the user sees something before heavy imports."""
    pm = QPixmap(420, 240)
    pm.fill(QColor("#0D3B2E"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # Soft panel behind icon
    p.setBrush(QColor("#124A3A"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(150, 22, 120, 120, 24, 24)
    if not app_icon.isNull():
        ico = app_icon.pixmap(88, 88)
        p.drawPixmap(166, 38, ico)
    p.setPen(QColor("#E8F5E9"))
    font = QFont()
    font.setPointSize(16)
    font.setBold(True)
    p.setFont(font)
    p.drawText(pm.rect().adjusted(0, 148, 0, 0), Qt.AlignHCenter | Qt.AlignTop, APP_NAME)
    font.setPointSize(10)
    font.setBold(False)
    p.setFont(font)
    p.setPen(QColor("#1F8A70"))
    p.drawText(pm.rect().adjusted(0, 180, 0, 0), Qt.AlignHCenter | Qt.AlignTop, f"{splash_subtitle()} · Starting…")
    p.end()
    splash = QSplashScreen(pm)
    splash.setWindowFlag(Qt.WindowStaysOnTopHint)
    return splash


def _show_boot_tray(app, icon):
    """Tray icon appears immediately so the user sees the app is launching."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QSystemTrayIcon(icon if not icon.isNull() else QIcon(), app)
    tray.setToolTip(f"{display_name()} — starting…")
    menu = QMenu()
    quit_act = QAction("Quit", menu)
    quit_act.triggered.connect(app.quit)
    menu.addAction(quit_act)
    tray.setContextMenu(menu)
    tray.show()
    tray.showMessage(
        display_name(),
        "Starting… window will open in a moment.",
        QSystemTrayIcon.Information,
        2500,
    )
    return tray


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("machineshop44")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    icon = _load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    # Instant feedback — tray + splash before any pandas/broker imports
    boot_tray = _show_boot_tray(app, icon)
    splash = _make_splash(icon)
    splash.show()
    splash.raise_()
    splash.showMessage("Loading libraries…", Qt.AlignBottom | Qt.AlignHCenter, QColor("#E8F5E9"))
    app.processEvents()

    splash.showMessage(f"Loading {APP_NAME}…", Qt.AlignBottom | Qt.AlignHCenter, QColor("#E8F5E9"))
    app.processEvents()
    from gui import MarketAdvisorGUI

    if not QSystemTrayIcon.isSystemTrayAvailable():
        splash.close()
        QMessageBox.critical(
            None,
            APP_NAME,
            "System tray is not available on this PC.\nThe window close button will exit the app.",
        )

    splash.showMessage("Building interface…", Qt.AlignBottom | Qt.AlignHCenter, QColor("#E8F5E9"))
    app.processEvents()
    window = MarketAdvisorGUI()
    if not icon.isNull():
        window.setWindowIcon(icon)
        QTimer.singleShot(0, lambda: window.setWindowIcon(icon))
        QTimer.singleShot(250, lambda: window.setWindowIcon(icon))

    # Hand off to the real in-window tray; drop the temporary boot tray
    if boot_tray is not None:
        boot_tray.hide()
        boot_tray.deleteLater()

    splash.finish(window)
    window.show()
    window.raise_()
    window.activateWindow()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
