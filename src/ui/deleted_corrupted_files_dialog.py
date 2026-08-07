# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialog affichant la liste des fichiers corrompus supprimés par l'application
(cf. src/core/deleted_corrupted_files.py) — sert à l'utilisateur pour les
retrouver dans la corbeille Windows, ou dans une sauvegarde externe si elle a
depuis été vidée."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout,
)

from src.core.deleted_corrupted_files import deleted_corrupted_files
from src.core.i18n import translate


class DeletedCorruptedFilesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate("DeletedCorruptedFilesDialog", "Fichiers corrompus supprimés"))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        entries = list(reversed(deleted_corrupted_files.get_entries()))
        n = len(entries)
        lbl = QLabel(
            translate("DeletedCorruptedFilesDialog",
                      "%n fichier(s) corrompu(s) supprimé(s) depuis l'installation, "
                      "en passant par la corbeille Windows. Cette liste est conservée "
                      "pour vous permettre de les y retrouver — ou dans une sauvegarde "
                      "externe si la corbeille a depuis été vidée.", None, n)
        )
        lbl.setWordWrap(True)
        root.addWidget(lbl)

        list_widget = QListWidget()
        if entries:
            list_widget.addItems([f"{e.get('wall', '?')}   {e.get('path', '')}" for e in entries])
        root.addWidget(list_widget, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton(translate("DeletedCorruptedFilesDialog", "Fermer"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        self.resize(700, 420)
