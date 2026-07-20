# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialogue Visages › Réinitialiser et réindexer… (extrait de main_window.py)."""

from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFrame, QLabel, QRadioButton,
    QVBoxLayout,
)


class _ResetFacesDialog(QDialog):
    """Dialogue de choix entre reset clustering seul et réinitialisation complète."""

    RESET_CLUSTERING = 1
    RESET_FULL       = 2

    _FRAME_BASE = (
        "QFrame#opt {"
        "  border: 2px solid #444; border-radius: 6px; background: #252525;"
        "}"
    )
    _FRAME_SEL = (
        "QFrame#opt {"
        "  border: 2px solid #4a9fd4; border-radius: 6px; background: #1a2f45;"
        "}"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Réinitialiser l'index des visages")
        self.setMinimumWidth(480)
        self.setStyleSheet(
            "QDialog { background: #1e1e1e; color: #ddd; }"
            "QRadioButton { color: #eee; font-size: 12px; font-weight: bold;"
            "  background: transparent; spacing: 8px; }"
            "QRadioButton::indicator { width: 15px; height: 15px; }"
            "QLabel { color: #aaa; font-size: 11px; background: transparent; }"
            "QDialogButtonBox QPushButton {"
            "  min-width: 90px; padding: 5px 12px;"
            "  background: #2a2a2a; color: #ddd;"
            "  border: 1px solid #555; border-radius: 4px;"
            "}"
            "QDialogButtonBox QPushButton:hover { background: #333; border-color: #888; }"
            "QDialogButtonBox QPushButton:default {"
            "  background: #1a3a5a; border-color: #4a9fd4; color: #fff;"
            "}"
        )
        self._choice = self.RESET_CLUSTERING
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        self._btn_group = QButtonGroup(self)

        self._rb_cluster = QRadioButton(
            "Réinitialiser les groupes uniquement  —  rapide"
        )
        self._rb_full = QRadioButton(
            "Réinitialisation complète + réindexation  —  lente"
        )
        self._btn_group.addButton(self._rb_cluster)
        self._btn_group.addButton(self._rb_full)

        self._frame_cluster = self._make_frame(
            self._rb_cluster,
            [
                "Les embeddings ArcFace (analyse des visages) sont conservés.",
                "Seuls les regroupements HDBSCAN sont effacés et recalculés.",
                "Les associations visage → personne (Picasa, identification manuelle)",
                "sont préservées et redistribuées dans les nouveaux groupes.",
                "⏱  Durée : quelques secondes.",
            ],
        )
        self._frame_full = self._make_frame(
            self._rb_full,
            [
                "Tout est effacé : embeddings, groupes, associations visage → personne.",
                "La détection ArcFace est relancée sur l'ensemble de la bibliothèque.",
                "Les personnes nommées sont conservées ; les annotations Picasa",
                "sont ré-appliquées automatiquement après re-détection.",
                "⏱  Durée : plusieurs heures selon la taille de la bibliothèque.",
            ],
        )
        root.addWidget(self._frame_cluster)
        root.addWidget(self._frame_full)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Confirmer")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

        self._rb_cluster.setChecked(True)
        self._frame_cluster.setStyleSheet(self._FRAME_SEL)
        self._btn_group.buttonToggled.connect(self._on_toggled)

    def _make_frame(self, rb: QRadioButton, lines: list[str]) -> QFrame:
        frame = QFrame()
        frame.setObjectName("opt")
        frame.setStyleSheet(self._FRAME_BASE)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        lay.addWidget(rb)
        for line in lines:
            lbl = QLabel(line)
            lbl.setIndent(23)
            lay.addWidget(lbl)
        return frame

    def _on_toggled(self, btn: QRadioButton, checked: bool) -> None:
        if not checked:
            return
        if btn is self._rb_cluster:
            self._choice = self.RESET_CLUSTERING
            self._frame_cluster.setStyleSheet(self._FRAME_SEL)
            self._frame_full.setStyleSheet(self._FRAME_BASE)
        else:
            self._choice = self.RESET_FULL
            self._frame_cluster.setStyleSheet(self._FRAME_BASE)
            self._frame_full.setStyleSheet(self._FRAME_SEL)

    @property
    def choice(self) -> int:
        return self._choice
