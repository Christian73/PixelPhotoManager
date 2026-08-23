# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Floating popup of the copies of a duplicate group (extracted from
main_window.py — opened by the ⧉ badge of the thumbnails and of the viewer)."""

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout,
)

from src.ui.ui_utils import fmt_size as _fmt_size
from src.core.i18n import translate


class _DuplicatesPopup(QFrame):
    """Floating popup listing every copy of a duplicate group (the original
    included). A Qt.Popup window: it closes automatically on a click outside
    it (like a menu), in addition to the "Close" button. Clicking a copy
    navigates directly (the navigate_requested signal) without closing the
    popup, so that several copies can be compared one after another.

    Draggable by click-and-drag (the title or the background of the popup): a
    popup without a title bar (Qt.Popup) would otherwise stay stuck where it
    opens, which can hide an important part of the compared photo."""

    navigate_requested = Signal(str)  # path of the target photo

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
        # The title is a child (QLabel): the mouse events reaching it do not
        # naturally bubble up to the parent QFrame, hence this filter to make it
        # draggable as well (cf. mousePressEvent/mouseMoveEvent below for the
        # rest of the popup).
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
