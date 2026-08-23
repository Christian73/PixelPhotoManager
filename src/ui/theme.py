# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Stylesheet of the global dark theme, applied by `main()` on the
QApplication.

Extracted from `main.py` so as to be checkable by tests without importing the
entry point (whose import reconfigures the logging of the process). The
`check_icon` parameter is the path of the tick drawn at runtime by main() — it
needs a live QGuiApplication, so it cannot be generated here.

Rule: every control whose indicator is *drawn by the style* (checkbox, radio
button…) must have its `::indicator` rules defined here. As soon as an
application stylesheet exists, Qt switches to QStyleSheetStyle and no longer
uses the native rendering of the indicator: a sub-control left without a rule
is rendered with the default colours, invisible on a dark background. That is
what happened to the QRadioButtons, absent from this sheet until now — hence
the local copies of `_RADIO_STYLE` in display_order_dialog.py,
people_panel.py, export_dialogs.py and reset_faces_dialog.py, which stay ahead
of the global rules (a widget stylesheet) and therefore keep their own
appearance."""

_BASE = """
    QToolTip {
        background-color: #2d2d2d;
        color: #eeeeee;
        border: 1px solid #666;
        padding: 4px 6px;
        border-radius: 3px;
    }
    QMainWindow, QDialog, QWidget {
        background-color: #1e1e1e;
        color: #ddd;
    }
    QMenuBar {
        background: #252525;
        color: #ddd;
    }
    QMenuBar::item:selected {
        background: #3a3a3a;
    }
    QMenu {
        background: #252525;
        color: #ddd;
    }
    QMenu::item:selected {
        background: #3a5a8a;
    }
    QMenu::item:disabled {
        color: #6a6a6a;
    }
    QMenu::item:disabled:selected {
        background: transparent;
    }
    QMenu::separator {
        height: 1px;
        background: #6a6a6a;
        margin: 6px 8px;
    }
    QToolBar {
        background: #252525;
        border: none;
        spacing: 4px;
        padding: 2px;
    }
    QTreeWidget, QListWidget {
        background: #252525;
        color: #ccc;
        border: none;
    }
    QTreeWidget::item:selected, QListWidget::item:selected {
        background: #3a5a8a;
    }
    QScrollArea {
        background: #1e1e1e;
        border: none;
    }
    QPushButton {
        background: #3a3a3a;
        color: #ddd;
        border: 1px solid #555;
        border-radius: 3px;
        padding: 4px 10px;
    }
    QPushButton:hover {
        background: #4a4a4a;
    }
    QPushButton:pressed {
        background: #2a2a2a;
    }
    QPushButton:checked {
        background: #3a5a8a;
    }
    QLineEdit {
        background: #2a2a2a;
        color: #ddd;
        border: 1px solid #555;
        border-radius: 3px;
        padding: 3px 6px;
    }
    QSlider::groove:horizontal {
        height: 4px;
        background: #555;
        border-radius: 2px;
    }
    QSlider::handle:horizontal {
        width: 12px;
        height: 12px;
        margin: -4px 0;
        background: #7aabdb;
        border-radius: 6px;
    }
    QGroupBox {
        color: #aaa;
        border: 1px solid #444;
        border-radius: 4px;
        margin-top: 8px;
        padding-top: 4px;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 8px;
        padding: 0 4px;
    }
    QStatusBar {
        background: #252525;
        color: #aaa;
    }
    QCheckBox {
        color: #ccc;
        spacing: 6px;
    }
    QCheckBox::indicator {
        width: 14px;
        height: 14px;
        border-radius: 2px;
    }
    QCheckBox::indicator:unchecked {
        border: 1px solid #777;
        background: #222232;
    }
    QCheckBox::indicator:unchecked:hover {
        border-color: #bbb;
        background: #2a2a3e;
    }
    QCheckBox::indicator:unchecked:disabled {
        border: 1px solid #444;
        background: #1a1a1a;
    }
    QCheckBox::indicator:checked:disabled {
        border: 1px solid #444;
        background: #1a3060;
    }
    QRadioButton {
        color: #ccc;
        spacing: 6px;
    }
    QRadioButton::indicator {
        width: 14px;
        height: 14px;
        border-radius: 9px;
        border: 2px solid #777;
        background: #222232;
    }
    QRadioButton::indicator:unchecked:hover {
        border-color: #bbb;
        background: #2a2a3e;
    }
    QRadioButton::indicator:checked {
        border: 2px solid #7aabdb;
        background: #2244bb;
    }
    QRadioButton::indicator:checked:hover {
        background: #3355cc;
    }
    QRadioButton::indicator:unchecked:disabled {
        border: 2px solid #444;
        background: #1a1a1a;
    }
    QRadioButton::indicator:checked:disabled {
        border: 2px solid #444;
        background: #1a3060;
    }
    QRadioButton:disabled {
        color: #666;
    }
    QSplitter::handle {
        background: #333;
    }
    QScrollBar:vertical {
        background: #1e1e1e;
        width: 14px;
        margin: 0;
    }
    QScrollBar::handle:vertical {
        background: #666;
        border-radius: 6px;
        min-height: 30px;
        margin: 2px 2px;
    }
    QScrollBar::handle:vertical:hover {
        background: #888;
    }
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {
        height: 0;
    }
    QScrollBar::add-page:vertical,
    QScrollBar::sub-page:vertical {
        background: none;
    }
"""


def app_stylesheet(check_icon: str) -> str:
    """Complete dark theme. `check_icon` = PNG path of the tick of the check
    boxes (drawn at runtime by main(), hence the late interpolation)."""
    return _BASE + f"""
    QCheckBox::indicator:checked {{
        border: 1px solid #5577ff;
        background: #2244bb;
        image: url({check_icon});
    }}
    QCheckBox::indicator:checked:hover {{
        background: #3355cc;
    }}
"""
