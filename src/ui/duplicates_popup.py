# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Popup flottante des exemplaires d'un groupe de doublons (extraite de
main_window.py — ouverte par le badge ⧉ des vignettes et de la visionneuse)."""

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout,
)

from src.ui.ui_utils import fmt_size as _fmt_size
from src.core.i18n import translate


class _DuplicatesPopup(QFrame):
    """Popup flottante listant tous les exemplaires d'un groupe de doublons
    (original inclus). Fenêtre de type Qt.Popup : se ferme automatiquement au
    clic en dehors d'elle (comme un menu), en plus du bouton « Fermer ».
    Cliquer sur un exemplaire navigue directement (signal navigate_requested)
    sans fermer la popup, pour permettre de comparer plusieurs exemplaires
    de suite.

    Déplaçable par cliquer-glisser (titre ou fond de la popup) : une popup
    sans barre de titre (Qt.Popup) reste sinon coincée là où elle s'ouvre,
    ce qui peut masquer une partie importante de la photo comparée."""

    navigate_requested = Signal(str)  # chemin de la photo cible

    def __init__(self, photo, others: list, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("duplicatesPopup")
        self.setStyleSheet(
            "#duplicatesPopup { background: #262626; border: 1px solid #555; border-radius: 6px; }"
            "QLabel { color: #ddd; }"
        )
        self.setMinimumWidth(440)
        self._drag_offset: QPoint | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        n_total = len(others) + 1
        title = QLabel(translate("DuplicatesPopup",
                                 "%n copy(ies) in this duplicate group:",
                                 None, n_total))
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        title.setWordWrap(True)
        title.setCursor(Qt.SizeAllCursor)
        title.setToolTip(translate("DuplicatesPopup", "Click and drag to move the window"))
        title.installEventFilter(self)
        layout.addWidget(title)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setMinimumHeight(140)
        self._list.setMaximumHeight(320)
        self._add_entry(photo, is_original=True)
        for p in others:
            self._add_entry(p, is_original=False)
        self._list.itemClicked.connect(self._on_navigate)
        layout.addWidget(self._list)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton(translate("DuplicatesPopup", "Close"))
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _add_entry(self, p, is_original: bool) -> None:
        size = _fmt_size(p.file_size) or "—"
        prefix = "★ Original — " if is_original else ""
        item = QListWidgetItem(f"{prefix}{p.filename}\n{p.directory}\n{size}")
        item.setData(Qt.UserRole, p.path)
        item.setToolTip(p.path)
        if is_original:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        self._list.addItem(item)

    def _on_navigate(self, item) -> None:
        path = item.data(Qt.UserRole)
        if path:
            self.navigate_requested.emit(path)

    def eventFilter(self, obj, event) -> bool:
        # Le titre est un enfant (QLabel) : les événements souris qui
        # l'atteignent ne remontent pas naturellement au QFrame parent, d'où
        # ce filtre pour le rendre lui aussi déplaçable (cf. mousePressEvent/
        # mouseMoveEvent ci-dessous pour le reste de la popup).
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            return True
        if event.type() == QEvent.MouseMove and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return True
        if event.type() == QEvent.MouseButtonRelease:
            self._drag_offset = None
            return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)
