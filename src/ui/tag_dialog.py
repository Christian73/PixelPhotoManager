# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
TagEditDialog — édition des mots-clés d'une sélection de photos (1 ou N).
"""

import logging

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QCompleter, QDialog, QDialogButtonBox,
    QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget,
)

from src.core.models import PhotoInfo

logger = logging.getLogger(__name__)


class TagsPrepLoader(QThread):
    """Précharge la liste des tags existants (Catalog.get_all_tags) hors du
    thread UI avant l'ouverture du dialogue, comme _AssignPrepLoader pour la
    popup d'assignation de personne (face_panel.py)."""

    ready = Signal(list)   # all_tags: list[str]

    def __init__(self, catalog, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog

    def run(self) -> None:
        try:
            self.ready.emit(self._catalog.get_all_tags())
        except Exception:
            logger.exception("[TagsPrepLoader] exception during load")
            self.ready.emit([])


class TagEditDialog(QDialog):
    """Édite les tags d'une sélection de photos.

    Les tags déjà présents sur la sélection apparaissent en chips (cases à
    cocher) : coché si le tag est sur *toutes* les photos sélectionnées, état
    tiers (Qt.PartiallyChecked) s'il n'est que sur une partie — cliquer sur une
    chip tierce la fait basculer vers un état plein (Qt et non tiers, cf.
    QCheckBox.nextCheckState). Un champ avec autocomplétion permet d'ajouter un
    nouveau tag (existant dans le catalogue ou inédit)."""

    def __init__(
        self, photos: list[PhotoInfo], all_tags: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mots-clés")
        self.setMinimumWidth(320)
        self._photos = photos
        self._all_tags = all_tags
        self._chips: dict[str, QCheckBox] = {}
        self._chips_layout: QVBoxLayout | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        count = len(self._photos)
        if count > 1:
            layout.addWidget(QLabel(f"{count} photos sélectionnées"))

        self._input = QLineEdit()
        self._input.setPlaceholderText("Ajouter un mot-clé…")
        self._input.setClearButtonEnabled(True)
        completer = QCompleter(self._all_tags, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._input.setCompleter(completer)
        self._input.returnPressed.connect(self._add_tag_from_input)
        layout.addWidget(self._input)

        scroll_content = QWidget()
        self._chips_layout = QVBoxLayout(scroll_content)
        self._chips_layout.setContentsMargins(4, 4, 4, 4)
        self._chips_layout.setSpacing(2)

        for tag in sorted(self._union_tags()):
            self._add_chip(tag, self._initial_state(tag))

        scroll_area = QScrollArea()
        scroll_area.setWidget(scroll_content)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setMaximumHeight(220)
        layout.addWidget(scroll_area)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _union_tags(self) -> set[str]:
        union: set[str] = set()
        for p in self._photos:
            union.update(p.tags)
        return union

    def _initial_state(self, tag: str) -> Qt.CheckState:
        on_all = all(tag in p.tags for p in self._photos)
        return Qt.Checked if on_all else Qt.PartiallyChecked

    def _add_chip(self, tag: str, state: Qt.CheckState) -> None:
        if tag in self._chips:
            self._chips[tag].setCheckState(Qt.Checked)
            return
        cb = QCheckBox(tag)
        cb.setTristate(True)
        cb.setCheckState(state)
        self._chips_layout.addWidget(cb)
        self._chips[tag] = cb

    def _add_tag_from_input(self) -> None:
        tag = self._input.text().strip()
        if not tag or "," in tag:
            return
        self._add_chip(tag, Qt.Checked)
        self._input.clear()

    def result_add_remove(self) -> tuple[list[str], list[str]]:
        """Renvoie (tags_à_ajouter, tags_à_retirer) — les chips laissées à
        l'état tiers (non touchées par l'utilisateur) n'apparaissent dans
        aucune des deux listes."""
        to_add, to_remove = [], []
        for tag, cb in self._chips.items():
            state = cb.checkState()
            if state == Qt.Checked:
                to_add.append(tag)
            elif state == Qt.Unchecked:
                to_remove.append(tag)
        return to_add, to_remove
