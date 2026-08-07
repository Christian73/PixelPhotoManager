# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
AdvancedSearchDialog — recherche multi-critères (dates, appareil, dossier,
personne, note min, mots-clés, favoris, type média).

La personne n'est PAS un critère de Catalog.search_advanced() : catalog.db et
faces.db sont deux bases séparées, sans JOIN possible entre elles (cf.
CLAUDE.md). get_person_id() renvoie l'id sélectionné à part ; c'est à
l'appelant (MainWindow) de résoudre face_db.get_photos_for_person(person_id)
et d'intersecter avec le résultat de search_advanced() en Python.
"""

import logging

from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from src.core.models import PersonInfo
from src.ui.photo_viewer import _RatingStars
from src.core.i18n import translate

logger = logging.getLogger(__name__)

# (libellé affiché, valeur de media_type) — le libellé est purement d'affichage,
# c'est le 2e élément qui est la donnée.
_MEDIA_TYPES = [
    (translate("AdvancedSearchDialog", "Tous"),   None),
    (translate("AdvancedSearchDialog", "Photos"), "image"),
    (translate("AdvancedSearchDialog", "Vidéos"), "video"),
]


class AdvancedSearchPrepLoader(QThread):
    """Précharge les listes appareils/personnes/tags hors du thread UI avant
    l'ouverture du dialogue (pattern _AssignPrepLoader de face_panel.py)."""

    ready = Signal(list, list, list)   # cameras, persons, all_tags

    def __init__(self, catalog, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog

    def run(self) -> None:
        try:
            cameras = self._catalog.get_distinct_cameras()
            persons = self._catalog.get_persons()
            all_tags = self._catalog.get_all_tags()
            self.ready.emit(cameras, persons, all_tags)
        except Exception:
            logger.exception("[AdvancedSearchPrepLoader] exception during load")
            self.ready.emit([], [], [])


class AdvancedSearchDialog(QDialog):
    """Formulaire de recherche avancée.

    get_criteria() renvoie un dict directement consommable par
    Catalog.search_advanced() ; get_person_id() est résolu séparément (cf.
    docstring module)."""

    def __init__(
        self,
        cameras: list[str],
        persons: list[PersonInfo],
        all_tags: list[str],
        folders: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("AdvancedSearchDialog", "Recherche avancée"))
        self.setMinimumWidth(380)
        self._persons = persons
        self._all_tags = all_tags
        self._tag_filters: list[str] = []
        self._tags_summary: QLabel | None = None
        self._setup_ui(cameras, folders)

    def _setup_ui(self, cameras: list[str], folders: list[str]) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        # --- Dates ---
        self._chk_dates = QCheckBox(translate("AdvancedSearchDialog", "Filtrer par date"))
        self._chk_dates.toggled.connect(self._on_dates_toggled)
        date_row = QHBoxLayout()
        date_row.setContentsMargins(0, 0, 0, 0)
        self._date_from = QDateEdit(QDate.currentDate().addYears(-1))
        self._date_from.setCalendarPopup(True)
        self._date_from.setEnabled(False)
        self._date_to = QDateEdit(QDate.currentDate())
        self._date_to.setCalendarPopup(True)
        self._date_to.setEnabled(False)
        date_row.addWidget(self._date_from)
        date_row.addWidget(QLabel("→"))
        date_row.addWidget(self._date_to)
        form.addRow(self._chk_dates, self._wrap(date_row))

        # --- Appareil ---
        self._camera_combo = QComboBox()
        self._camera_combo.setEditable(True)
        self._camera_combo.addItem("")
        self._camera_combo.addItems(cameras)
        form.addRow(translate("AdvancedSearchDialog", "Appareil :"), self._camera_combo)

        # --- Personne ---
        self._person_combo = QComboBox()
        self._person_combo.addItem(translate("AdvancedSearchDialog", "(toute personne)"), None)
        for p in self._persons:
            self._person_combo.addItem(p.name, p.id)
        form.addRow(translate("AdvancedSearchDialog", "Personne :"), self._person_combo)

        # --- Dossier ---
        self._folder_combo = QComboBox()
        self._folder_combo.setEditable(True)
        self._folder_combo.addItem("")
        self._folder_combo.addItems(folders)
        form.addRow(translate("AdvancedSearchDialog", "Dossier :"), self._folder_combo)

        # --- Note min ---
        self._stars = _RatingStars()
        form.addRow(translate("AdvancedSearchDialog", "Note min. :"), self._stars)

        # --- Mots-clés ---
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText(translate("AdvancedSearchDialog", "Ajouter un mot-clé puis Entrée…"))
        completer = QCompleter(self._all_tags, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._tag_input.setCompleter(completer)
        self._tag_input.returnPressed.connect(self._add_tag_filter)
        tags_col = QVBoxLayout()
        tags_col.setContentsMargins(0, 0, 0, 0)
        tags_col.addWidget(self._tag_input)
        self._tags_summary = QLabel("")
        self._tags_summary.setWordWrap(True)
        self._tags_summary.setStyleSheet("color: #9cc4e4;")
        tags_col.addWidget(self._tags_summary)
        form.addRow(translate("AdvancedSearchDialog", "Mots-clés :"), self._wrap(tags_col))

        # --- Favoris / type média ---
        self._chk_favorites = QCheckBox(translate("AdvancedSearchDialog", "Favoris uniquement"))
        form.addRow("", self._chk_favorites)

        self._media_combo = QComboBox()
        for label, _value in _MEDIA_TYPES:
            self._media_combo.addItem(label)
        form.addRow(translate("AdvancedSearchDialog", "Type :"), self._media_combo)

        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Ok).setText(translate("AdvancedSearchDialog", "Rechercher"))
        root.addWidget(buttons)

    @staticmethod
    def _wrap(inner_layout) -> QWidget:
        w = QWidget()
        w.setLayout(inner_layout)
        return w

    def _on_dates_toggled(self, checked: bool) -> None:
        self._date_from.setEnabled(checked)
        self._date_to.setEnabled(checked)

    def _add_tag_filter(self) -> None:
        tag = self._tag_input.text().strip()
        if not tag or "," in tag or tag in self._tag_filters:
            self._tag_input.clear()
            return
        self._tag_filters.append(tag)
        self._tag_input.clear()
        self._tags_summary.setText(", ".join(self._tag_filters))

    def get_criteria(self) -> dict:
        """Dict prêt pour Catalog.search_advanced() — la personne n'y figure
        pas (résolue séparément via get_person_id(), cf. docstring module)."""
        criteria: dict = {}
        if self._chk_dates.isChecked():
            criteria["date_from"] = self._date_from.date().toString("yyyy-MM-dd")
            criteria["date_to"] = self._date_to.date().toString("yyyy-MM-dd")
        camera = self._camera_combo.currentText().strip()
        if camera:
            criteria["camera"] = camera
        directory = self._folder_combo.currentText().strip()
        if directory:
            criteria["directory"] = directory
        if self._stars.rating:
            criteria["min_rating"] = self._stars.rating
        if self._tag_filters:
            criteria["tags"] = list(self._tag_filters)
        if self._chk_favorites.isChecked():
            criteria["favorites_only"] = True
        media_value = _MEDIA_TYPES[self._media_combo.currentIndex()][1]
        if media_value:
            criteria["media_type"] = media_value
        return criteria

    def get_person_id(self) -> "int | None":
        return self._person_combo.currentData()
