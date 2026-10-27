"""Dark Fusion theme (original styling)."""
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    base, window, text = QColor(43, 43, 43), QColor(53, 53, 55), QColor(220, 220, 220)
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, QColor(48, 48, 50))
    p.setColor(QPalette.ToolTipBase, QColor(30, 30, 30))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, window)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(255, 80, 80))
    p.setColor(QPalette.Highlight, QColor(52, 120, 198))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.Link, QColor(90, 160, 240))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(120, 120, 120))
    app.setPalette(p)
    app.setStyleSheet("""
        QDockWidget::title { background: #3a3a3d; padding: 4px; }
        QDockWidget { titlebar-close-icon: none; }
        QToolBar { background: #3a3a3d; border: none; spacing: 2px; }
        QToolButton { padding: 3px 5px; }
        QToolButton:checked { background: #2f5f94; border-radius: 3px; }
        QTabBar::tab { background: #3a3a3d; padding: 5px 14px; border: 1px solid #2a2a2a; }
        QTabBar::tab:selected { background: #2b2b2b; border-bottom-color: #3478c6; }
        QMenu::separator { height: 1px; background: #555; margin: 4px 8px; }
        QStatusBar { background: #3a3a3d; }
        QHeaderView::section { background: #3a3a3d; padding: 3px; border: 1px solid #2a2a2a; }
    """)
