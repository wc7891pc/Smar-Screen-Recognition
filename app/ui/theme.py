"""深色主题与配色。"""
from __future__ import annotations

ACCENT = "#2dd4bf"
ACCENT_DARK = "#14b8a6"
BG = "#14161a"
PANEL = "#1c1f26"
PANEL_2 = "#23272f"
BORDER = "#2f3540"
TEXT = "#e7ebf2"
MUTED = "#8d97a8"
OK = "#34d399"
WARN = "#fbbf24"
ERR = "#f87171"
INFO = "#60a5fa"

QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
    font-size: 13px;
}}
QWidget {{
    background: {BG};
    color: {TEXT};
}}
QMainWindow, QDialog {{
    background: {BG};
}}
#Header {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
#HeaderTitle {{
    font-size: 17px;
    font-weight: 600;
    color: {TEXT};
}}
#HeaderSub {{
    color: {MUTED};
    font-size: 12px;
}}
#Card {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {PANEL};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    padding: 9px 20px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    color: {TEXT};
}}
QPushButton {{
    background: {PANEL_2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    background: #2b3038;
    border-color: #3c4452;
}}
QPushButton:pressed {{
    background: #1a1e24;
}}
QPushButton:disabled {{
    color: #5f6878;
    background: #1a1d22;
    border-color: #262b33;
}}
QPushButton#Primary {{
    background: {ACCENT_DARK};
    color: #06231f;
    border: none;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: #2ee0c8;
}}
QPushButton#Primary:disabled {{
    background: #1f4a45;
    color: #4d7a75;
}}
QPushButton#Danger {{
    background: #b91c1c;
    color: #fff;
    border: none;
    font-weight: 600;
}}
QPushButton#Danger:hover {{
    background: #dc2626;
}}
QPushButton#Danger:disabled {{
    background: #4c1d1d;
    color: #92676a;
}}
QPushButton#Ghost {{
    background: transparent;
    border: none;
    color: {MUTED};
    padding: 6px 8px;
}}
QPushButton#Ghost:hover {{
    color: {TEXT};
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {ACCENT_DARK};
    selection-color: #06231f;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox QAbstractItemView {{
    background: {PANEL_2};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DARK};
    selection-color: #06231f;
    outline: none;
}}
QCheckBox, QRadioButton {{
    spacing: 7px;
    color: {TEXT};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {PANEL_2};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: none;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QTableWidget, QListWidget, QTreeWidget {{
    background: {PANEL};
    alternate-background-color: #1f232a;
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: {BORDER};
    selection-background-color: #21343a;
    selection-color: {TEXT};
    outline: none;
}}
QTableWidget::item {{ padding: 5px 6px; }}
QHeaderView::section {{
    background: {PANEL_2};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 7px 6px;
    font-weight: 600;
}}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #3a4150; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #4a5468; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #3a4150; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    background: {PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    top: 2px;
    padding: 0 6px;
    color: {MUTED};
    font-weight: 600;
}}
QLabel {{ background: transparent; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Pill {{
    background: {PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 11px;
    padding: 3px 10px;
    color: {MUTED};
    font-size: 12px;
}}
QSlider::groove:horizontal {{
    height: 4px; background: #333a45; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 14px; height: 14px;
    margin: -6px 0; border-radius: 7px;
}}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QSplitter::handle:vertical {{ height: 10px; }}
QToolTip {{
    background: #2b3038;
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 5px 8px;
    border-radius: 6px;
}}
QMenu {{
    background: {PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: #2f3a45; }}
"""


from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication) -> None:
    app.setStyleSheet(QSS)
