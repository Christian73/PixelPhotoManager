# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Dialogue affichant les compteurs de reconnaissance faciale (menu Visages › Compteurs…).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel, QVBoxLayout,
)
from src.core.i18n import translate


def _section(title: str, rows: list[tuple[str, int]]) -> QFrame:
    frame = QFrame()
    frame.setFrameShape(QFrame.StyledPanel)
    frame.setStyleSheet(
        "QFrame { background: #2a2a2a; border: 1px solid #444; border-radius: 4px; }"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.setSpacing(4)

    lbl_title = QLabel(title)
    font = QFont()
    font.setBold(True)
    lbl_title.setFont(font)
    lbl_title.setStyleSheet("color: #ddd; border: none;")
    layout.addWidget(lbl_title)

    for label, value in rows:
        lbl = QLabel(f"  {label} : {value}")
        lbl.setStyleSheet("color: #ccc; border: none;")
        layout.addWidget(lbl)

    return frame


class FaceCountersDialog(QDialog):
    """Résumé des compteurs de reconnaissance faciale et d'import Picasa."""

    def __init__(self, face_db, catalog, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("FaceCountersDialog", "Compteurs de reconnaissance faciale"))
        self.setMinimumWidth(420)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        stats = face_db.get_recognition_counters()
        n_persons = len(catalog.get_persons())

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 16)

        layout.addWidget(_section(translate("FaceCountersDialog", "Reconnaissance faciale"), [
            (translate("FaceCountersDialog", "Personnes identifiées"), n_persons),
            (translate("FaceCountersDialog", "Visages identifiés"), stats["identified_faces"]),
            (translate("FaceCountersDialog", "Visages reconnus (analyse faciale)"),
             stats["recognized_faces"]),
            (translate("FaceCountersDialog", "Visages en attente de confirmation"),
             stats["pending_faces"]),
            (translate("FaceCountersDialog", "Visages inconnus"), stats["unknown_faces"]),
        ]))

        layout.addWidget(_section(translate("FaceCountersDialog", "Import Picasa"), [
            (translate("FaceCountersDialog", "Visages importés"), stats["picasa_total"]),
            (translate("FaceCountersDialog", "Fusionnés avec la reconnaissance"),
             stats["picasa_merged"]),
            (translate("FaceCountersDialog", "En attente de reconnaissance"),
             stats["picasa_placeholder"]),
        ]))

        layout.addWidget(_section(translate("FaceCountersDialog", "Totalité"), [
            (translate("FaceCountersDialog", "Visages détectés"), stats["total_faces"]),
            (translate("FaceCountersDialog", "Visages ignorés (taille)"), stats["ignored_faces"]),
            (translate("FaceCountersDialog", "Groupes (clusters)"), stats["clusters"]),
        ]))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
