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
        self.setWindowTitle("Compteurs de reconnaissance faciale")
        self.setMinimumWidth(420)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        stats = face_db.get_recognition_counters()
        n_persons = len(catalog.get_persons())

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 16)

        layout.addWidget(_section("Reconnaissance faciale", [
            ("Personnes identifiées", n_persons),
            ("Visages identifiés", stats["identified_faces"]),
            ("Visages reconnus (analyse faciale)", stats["recognized_faces"]),
            ("Visages en attente de confirmation", stats["pending_faces"]),
            ("Visages inconnus", stats["unknown_faces"]),
        ]))

        layout.addWidget(_section("Import Picasa", [
            ("Visages importés", stats["picasa_total"]),
            ("Fusionnés avec la reconnaissance", stats["picasa_merged"]),
            ("En attente de reconnaissance", stats["picasa_placeholder"]),
        ]))

        layout.addWidget(_section("Totalité", [
            ("Visages détectés", stats["total_faces"]),
            ("Visages ignorés (taille)", stats["ignored_faces"]),
            ("Groupes (clusters)", stats["clusters"]),
        ]))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
