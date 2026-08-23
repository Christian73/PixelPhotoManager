# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialog showing the list of corrupted files deleted by the application
(cf. src/core/deleted_corrupted_files.py) — helps the user find them again in
the Windows recycle bin, or in an external backup if it has been emptied
since."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout,
)

from src.core.deleted_corrupted_files import deleted_corrupted_files
from src.core.i18n import translate


class DeletedCorruptedFilesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate("DeletedCorruptedFilesDialog", "Deleted corrupted files"))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        entries = list(reversed(deleted_corrupted_files.get_entries()))
        n = len(entries)
        lbl = QLabel(
            translate("DeletedCorruptedFilesDialog",
                      "%n corrupted file(s) deleted since the installation, by way of the "
                      "Windows recycle bin. This list is kept so that you can find them there "
                      "— or in an external backup if the recycle bin has since been emptied.", None, n)
        )
        lbl.setWordWrap(True)
        root.addWidget(lbl)

        list_widget = QListWidget()
        if entries:
            list_widget.addItems([f"{e.get('wall', '?')}   {e.get('path', '')}" for e in entries])
        root.addWidget(list_widget, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton(translate("DeletedCorruptedFilesDialog", "Close"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        self.resize(700, 420)
