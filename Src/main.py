import sys
from PyQt5.QtWidgets import QApplication
from gui import MarketAdvisorGUI

def main():
    # Start the PyQt5 application
    app = QApplication(sys.argv)
    
    # Set a clean, modern visual style
    app.setStyle("Fusion")
    
    # Launch the main GUI
    window = MarketAdvisorGUI()
    window.show()
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()