# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
TagEditDialog — editing the keywords of a selection of photos (1 or N).
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
    """Preloads the list of the existing tags (Catalog.get_all_tags) off the UI
    thread before the dialog is opened, like _AssignPrepLoader for the person
    assignment popup (face_panel.py)."""

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
    """A tristate box whose click cycle is restricted to 2 states.
    Qt.PartiallyChecked is only used as the *initial* display state (a keyword
    present on part only of the multi-photo selection) — without this
    override, the default cycle of QCheckBox.nextCheckState (Unchecked →
    PartiallyChecked → Checked → Unchecked → …) brings the third state back
    after a second click, which looks like an unwanted 3rd state even when a
    single photo is selected."""

    def nextCheckState(self) -> None:
        if self.checkState() == Qt.Checked:
            self.setCheckState(Qt.Unchecked)
        else:
            self.setCheckState(Qt.Checked)


class TagEditDialog(QDialog):
    """Edits the tags of a selection of photos.

    Every keyword of the catalog appears as a chip (a check box), not only
    those already present on the selection: checked if the keyword is on
    *every* selected photo, unchecked if it is on none, third state
    (Qt.PartiallyChecked) if it is only on part of them — clicking a chip in
    the third state switches it to a full state (cf. _TagChip.nextCheckState).
    A field with autocompletion allows a new tag to be added (either already
    in the catalog or brand new)."""

    def __init__(
        self, photos: list[PhotoInfo], all_tags: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("TagEditDialog", "Keywords"))
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
                translate("TagEditDialog", "%n photo(s) selected", None, count)))

        self._input = QLineEdit()
        self._input.setPlaceholderText(translate("TagEditDialog", "Add a keyword…"))
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
        """Clicking OK does not go through returnPressed: without this explicit
        commit, a keyword still typed in the field (not validated by Enter)
        was silently lost on closing."""
        self._add_tag_from_input()
        self.accept()

    def result_add_remove(self) -> tuple[list[str], list[str]]:
        """Returns (tags_to_add, tags_to_remove) — the chips left in the third
        state (untouched by the user) appear in neither list. A keyword of the
        catalog never present on the selection and left unchecked is not
        reported as a removal either (it would be a removal with no effect,
        and it would still cost one DB write per catalog keyword on every
        validation)."""
        union = self._union_tags()
        to_add, to_remove = [], []
        for tag, cb in self._chips.items():
            state = cb.checkState()
            if state == Qt.Checked:
                to_add.append(tag)
            elif state == Qt.Unchecked and tag in union:
                to_remove.append(tag)
        return to_add, to_remove
