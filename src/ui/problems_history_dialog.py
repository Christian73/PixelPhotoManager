# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialog affichant l'historique des problèmes rencontrés (ex. fichiers
corrompus détectés pendant une recherche de doublons)."""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from src.core.problems_history import problems_history
from src.core.i18n import translate


# ---------------------------------------------------------------------------
# Widget par entrée

class _ProblemRow(QWidget):
    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self._setup_ui(entry)

    def _setup_ui(self, entry: dict) -> None:
        self.setStyleSheet(
            "background: #252525; border-radius: 4px;"
            "border: 1px solid #333;"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)

        date_lbl = QLabel(entry.get("wall", "?"))
        date_lbl.setStyleSheet("color: #ddd; font-size: 12px; background: transparent; border: none;")
        top.addWidget(date_lbl)

        corrupted = entry.get("corrupted_count", 0)
        repaired = entry.get("repaired_count", 0)
        summary = QLabel(
            translate("ProblemRow", "%n corrupted file(s) found", None, corrupted)
            + ", "
            + translate("ProblemRow", "%n repaired", None, repaired)
        )
        summary.setStyleSheet("color: #888; font-size: 11px; background: transparent; border: none;")
        summary.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(summary, stretch=1)

        list_path = entry.get("list_path")
        btn_open = QPushButton(translate("ProblemRow", "Open the list…"))
        btn_open.setStyleSheet(
            "QPushButton { background: #2a5080; color: white; border: none;"
            " border-radius: 3px; padding: 3px 10px; }"
            "QPushButton:hover { background: #3a6090; }"
            "QPushButton:disabled { background: #333; color: #777; }"
        )
        has_list = bool(list_path) and os.path.isfile(list_path)
        btn_open.setEnabled(has_list)
        btn_open.setToolTip(list_path
                            or translate("ProblemRow", "No file attached"))
        if has_list:
            btn_open.clicked.connect(lambda: os.startfile(list_path))
        top.addWidget(btn_open)

        outer.addLayout(top)


# ---------------------------------------------------------------------------
# Dialog principal

class ProblemsHistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate("ProblemsHistoryDialog", "Problem history"))
        self.setMinimumWidth(560)
        self.setMinimumHeight(380)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 14)

        lbl = QLabel(translate("ProblemsHistoryDialog", "Problems encountered (corrupted files "
                                                        "found during analyses)"))
        lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #ccc;")
        lbl.setWordWrap(True)
        root.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("border: 1px solid #333;")

        container = QWidget()
        container.setStyleSheet("background: #1e1e1e;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        entries = list(reversed(problems_history.get_entries()))
        if not entries:
            empty = QLabel(translate("ProblemsHistoryDialog", "No problem recorded so far."))
            empty.setStyleSheet("color: #555; font-size: 11px; padding: 24px;")
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty)
        else:
            for entry in entries:
                layout.addWidget(_ProblemRow(entry, container))
        layout.addStretch()

        scroll.setWidget(container)
        root.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton(translate("ProblemsHistoryDialog", "Close"))
        btn_close.setFixedWidth(80)
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)
