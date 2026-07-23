# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour tag_dialog — TagEditDialog
n'est jamais exec() : on pilote ses méthodes directement (comme _AssignDialog,
cf. test_people_panel.py). TagsPrepLoader est exécuté en synchrone via run()
(pattern QThread standard du projet, cf. CLAUDE.md)."""
from PySide6.QtCore import Qt

from src.core.models import PhotoInfo
from src.library.catalog import Catalog
from src.ui.tag_dialog import TagEditDialog, TagsPrepLoader


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


class TestTagEditDialogChips:
    def test_single_photo_tags_are_fully_checked(self, qtbot):
        photo = _photo("C:/lib/a.jpg", tags=["vacances", "plage"])
        dlg = TagEditDialog([photo], all_tags=["vacances", "plage", "famille"])
        qtbot.addWidget(dlg)

        assert dlg._chips["vacances"].checkState() == Qt.Checked
        assert dlg._chips["plage"].checkState() == Qt.Checked

    def test_tag_on_some_but_not_all_photos_is_partially_checked(self, qtbot):
        p1 = _photo("C:/lib/a.jpg", tags=["vacances"])
        p2 = _photo("C:/lib/b.jpg", tags=[])
        dlg = TagEditDialog([p1, p2], all_tags=[])
        qtbot.addWidget(dlg)

        assert dlg._chips["vacances"].checkState() == Qt.PartiallyChecked

    def test_tag_on_all_photos_is_fully_checked(self, qtbot):
        p1 = _photo("C:/lib/a.jpg", tags=["vacances"])
        p2 = _photo("C:/lib/b.jpg", tags=["vacances"])
        dlg = TagEditDialog([p1, p2], all_tags=[])
        qtbot.addWidget(dlg)

        assert dlg._chips["vacances"].checkState() == Qt.Checked

    def test_no_photos_selected_produces_no_chips(self, qtbot):
        dlg = TagEditDialog([], all_tags=["vacances"])
        qtbot.addWidget(dlg)

        assert dlg._chips == {}


class TestTagEditDialogAddTag:
    def test_typing_and_returning_adds_a_checked_chip(self, qtbot):
        photo = _photo("C:/lib/a.jpg")
        dlg = TagEditDialog([photo], all_tags=[])
        qtbot.addWidget(dlg)

        dlg._input.setText("été")
        dlg._add_tag_from_input()

        assert dlg._chips["été"].checkState() == Qt.Checked
        assert dlg._input.text() == ""

    def test_adding_tag_already_present_as_partial_forces_it_checked(self, qtbot):
        p1 = _photo("C:/lib/a.jpg", tags=["vacances"])
        p2 = _photo("C:/lib/b.jpg", tags=[])
        dlg = TagEditDialog([p1, p2], all_tags=[])
        qtbot.addWidget(dlg)
        assert dlg._chips["vacances"].checkState() == Qt.PartiallyChecked

        dlg._input.setText("vacances")
        dlg._add_tag_from_input()

        assert dlg._chips["vacances"].checkState() == Qt.Checked

    def test_blank_or_comma_containing_input_is_ignored(self, qtbot):
        photo = _photo("C:/lib/a.jpg")
        dlg = TagEditDialog([photo], all_tags=[])
        qtbot.addWidget(dlg)

        dlg._input.setText("   ")
        dlg._add_tag_from_input()
        dlg._input.setText("a,b")
        dlg._add_tag_from_input()

        assert dlg._chips == {}


class TestTagEditDialogResult:
    def test_untouched_partial_chip_is_absent_from_add_and_remove(self, qtbot):
        p1 = _photo("C:/lib/a.jpg", tags=["vacances"])
        p2 = _photo("C:/lib/b.jpg", tags=[])
        dlg = TagEditDialog([p1, p2], all_tags=[])
        qtbot.addWidget(dlg)

        to_add, to_remove = dlg.result_add_remove()

        assert to_add == []
        assert to_remove == []

    def test_unchecking_a_checked_chip_marks_it_for_removal(self, qtbot):
        photo = _photo("C:/lib/a.jpg", tags=["vacances"])
        dlg = TagEditDialog([photo], all_tags=[])
        qtbot.addWidget(dlg)

        dlg._chips["vacances"].setCheckState(Qt.Unchecked)

        to_add, to_remove = dlg.result_add_remove()
        assert to_add == []
        assert to_remove == ["vacances"]

    def test_new_tag_is_returned_in_to_add(self, qtbot):
        photo = _photo("C:/lib/a.jpg")
        dlg = TagEditDialog([photo], all_tags=[])
        qtbot.addWidget(dlg)
        dlg._input.setText("été")
        dlg._add_tag_from_input()

        to_add, to_remove = dlg.result_add_remove()
        assert to_add == ["été"]
        assert to_remove == []


class TestTagsPrepLoader:
    def test_run_emits_catalog_tags(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = catalog.add_or_update_photo(_photo("C:/lib/a.jpg"))
        catalog.set_tags(photo.id, ["vacances", "plage"])

        loader = TagsPrepLoader(catalog)
        received = []
        loader.ready.connect(lambda tags: received.append(tags))
        loader.run()  # synchrone, cf. CLAUDE.md piège coverage/QThread

        assert received == [["plage", "vacances"]]
