# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
AdvancedSearchDialog — multi-criteria search (dates, camera, folder,
person, minimum rating, keywords, favorites, media type).

The person is NOT a criterion of Catalog.search_advanced(): catalog.db and
faces.db are two separate databases, with no JOIN possible between them (cf.
CLAUDE.md). get_person_id() returns the selected id separately; it is up to
the caller (MainWindow) to resolve face_db.get_photos_for_person(person_id)
and to intersect it with the result of search_advanced() in Python.
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

# (displayed label, media_type value) — the label is purely for display,
# the 2nd element is the data.
_MEDIA_TYPES = [
    (translate("AdvancedSearchDialog", "All"),   None),
    (translate("AdvancedSearchDialog", "Photos"), "image"),
    (translate("AdvancedSearchDialog", "Videos"), "video"),
]


class AdvancedSearchPrepLoader(QThread):
    """Preloads the camera/person/tag lists off the UI thread before the dialog
    is opened (the _AssignPrepLoader pattern of face_panel.py)."""

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
    """Advanced search form.

    get_criteria() returns a dict directly consumable by
    Catalog.search_advanced(); get_person_id() is resolved separately (cf. the
    module docstring)."""

    def __init__(
        self,
        cameras: list[str],
        persons: list[PersonInfo],
        all_tags: list[str],
        folders: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("AdvancedSearchDialog", "Advanced search"))
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
        self._chk_dates = QCheckBox(translate("AdvancedSearchDialog", "Filter by date"))
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
        form.addRow(translate("AdvancedSearchDialog", "Camera:"), self._camera_combo)

        # --- Personne ---
        self._person_combo = QComboBox()
        self._person_combo.addItem(translate("AdvancedSearchDialog", "(anyone)"), None)
        for p in self._persons:
            self._person_combo.addItem(p.name, p.id)
        form.addRow(translate("AdvancedSearchDialog", "Person:"), self._person_combo)

        # --- Folder ---
        self._folder_combo = QComboBox()
        self._folder_combo.setEditable(True)
        self._folder_combo.addItem("")
        self._folder_combo.addItems(folders)
        form.addRow(translate("AdvancedSearchDialog", "Folder:"), self._folder_combo)

        # --- Note min ---
        self._stars = _RatingStars()
        form.addRow(translate("AdvancedSearchDialog", "Min. rating:"), self._stars)

        # --- Keywords ---
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText(translate("AdvancedSearchDialog", "Add a keyword "
                                                                             "then press Enter…"))
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
        form.addRow(translate("AdvancedSearchDialog", "Keywords:"), self._wrap(tags_col))

        # --- Favorites / media type ---
        self._chk_favorites = QCheckBox(translate("AdvancedSearchDialog", "Favourites only"))
        form.addRow("", self._chk_favorites)

        self._media_combo = QComboBox()
        for label, _value in _MEDIA_TYPES:
            self._media_combo.addItem(label)
        form.addRow(translate("AdvancedSearchDialog", "Type:"), self._media_combo)

        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Ok).setText(translate("AdvancedSearchDialog", "Search"))
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
        """A dict ready for Catalog.search_advanced() — the person is not in it
        (resolved separately through get_person_id(), cf. the module
        docstring)."""
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
