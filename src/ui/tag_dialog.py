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
from src.core.i18n import translate

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


class _TagChip(QCheckBox):
    """Case tristate au cycle de clic restreint à 2 états. Qt.PartiallyChecked
    n'est utilisé que comme état *initial* d'affichage (mot-clé présent sur
    une partie seulement de la sélection multi-photos) — sans cette
    surcharge, le cycle par défaut de QCheckBox.nextCheckState (Unchecked →
    PartiallyChecked → Checked → Unchecked → …) fait réapparaître l'état
    tiers après un second clic, ce qui ressemble à un 3e état indésirable
    même en sélection d'une seule photo."""

    def nextCheckState(self) -> None:
        if self.checkState() == Qt.Checked:
            self.setCheckState(Qt.Unchecked)
        else:
            self.setCheckState(Qt.Checked)


class TagEditDialog(QDialog):
    """Édite les tags d'une sélection de photos.

    Tous les mots-clés du catalogue apparaissent en chips (cases à cocher),
    pas seulement ceux déjà présents sur la sélection : coché si le mot-clé
    est sur *toutes* les photos sélectionnées, décoché s'il n'est sur
    aucune, état tiers (Qt.PartiallyChecked) s'il n'est que sur une partie —
    cliquer sur une chip tierce la fait basculer vers un état plein (cf.
    _TagChip.nextCheckState). Un champ avec autocomplétion permet d'ajouter
    un nouveau tag (existant dans le catalogue ou inédit)."""

    def __init__(
        self, photos: list[PhotoInfo], all_tags: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("TagEditDialog", "Mots-clés"))
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
            layout.addWidget(QLabel(
                translate("TagEditDialog", "%n photo(s) sélectionnée(s)", None, count)))

        self._input = QLineEdit()
        self._input.setPlaceholderText(translate("TagEditDialog", "Ajouter un mot-clé…"))
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

        tags_to_show = set(self._all_tags) | self._union_tags() if self._photos else set()
        for tag in sorted(tags_to_show):
            self._add_chip(tag, self._initial_state(tag))

        scroll_area = QScrollArea()
        scroll_area.setWidget(scroll_content)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setMaximumHeight(220)
        layout.addWidget(scroll_area)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _union_tags(self) -> set[str]:
        union: set[str] = set()
        for p in self._photos:
            union.update(p.tags)
        return union

    def _initial_state(self, tag: str) -> Qt.CheckState:
        present = sum(1 for p in self._photos if tag in p.tags)
        if present == 0:
            return Qt.Unchecked
        if present == len(self._photos):
            return Qt.Checked
        return Qt.PartiallyChecked

    def _add_chip(self, tag: str, state: Qt.CheckState) -> None:
        if tag in self._chips:
            self._chips[tag].setCheckState(Qt.Checked)
            return
        cb = _TagChip(tag)
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

    def _accept(self) -> None:
        """Le clic sur OK ne passe pas par returnPressed : sans ce commit
        explicite, un mot-clé encore tapé dans le champ (non validé par
        Entrée) était perdu silencieusement à la fermeture."""
        self._add_tag_from_input()
        self.accept()

    def result_add_remove(self) -> tuple[list[str], list[str]]:
        """Renvoie (tags_à_ajouter, tags_à_retirer) — les chips laissées à
        l'état tiers (non touchées par l'utilisateur) n'apparaissent dans
        aucune des deux listes. Un mot-clé du catalogue jamais présent sur la
        sélection et laissé décoché n'est pas non plus reporté en retrait
        (ce serait un retrait sans effet, mais ça éviterait quand même une
        écriture DB par mot-clé du catalogue à chaque validation)."""
        union = self._union_tags()
        to_add, to_remove = [], []
        for tag, cb in self._chips.items():
            state = cb.checkState()
            if state == Qt.Checked:
                to_add.append(tag)
            elif state == Qt.Unchecked and tag in union:
                to_remove.append(tag)
        return to_add, to_remove
