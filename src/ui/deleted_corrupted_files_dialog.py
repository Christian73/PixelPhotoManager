# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialog affichant la liste des fichiers corrompus supprimés définitivement
(cf. src/core/deleted_corrupted_files.py) — sert à l'utilisateur pour tenter
de les retrouver dans une sauvegarde externe."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout,
)

from src.core.deleted_corrupted_files import deleted_corrupted_files


class DeletedCorruptedFilesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fichiers corrompus supprimés")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        entries = list(reversed(deleted_corrupted_files.get_entries()))
        n = len(entries)
        lbl = QLabel(
            f"{n} fichier{'s' if n != 1 else ''} corrompu{'s' if n != 1 else ''} "
            f"supprimé{'s' if n != 1 else ''} définitivement depuis l'installation. "
            "Conservés ici pour vous permettre de les rechercher dans une "
            "sauvegarde externe."
        )
        lbl.setWordWrap(True)
        root.addWidget(lbl)

        list_widget = QListWidget()
        if entries:
            list_widget.addItems([f"{e.get('wall', '?')}   {e.get('path', '')}" for e in entries])
        root.addWidget(list_widget, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("Fermer")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        self.resize(700, 420)
