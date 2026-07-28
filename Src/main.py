import sys
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox
from gui import MarketAdvisorGUI

def main():
    # Start the PyQt5 application
    app = QApplication(sys.argv)
    app.setApplicationName("Market Advisor")
    app.setQuitOnLastWindowClosed(False)  # stay alive when window is hidden to tray

    # Set a clean, modern visual style
    app.setStyle("Fusion")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            "Market Advisor",
            "System tray is not available on this PC.\nThe window close button will exit the app.",
        )

    # Launch the main GUI
    window = MarketAdvisorGUI()
    window.show()

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
