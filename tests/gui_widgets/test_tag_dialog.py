# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Isolated Qt widget tests (Layer 2, pytest-qt) for tag_dialog -- TagEditDialog
is never exec()ed: its methods are driven directly (like _AssignDialog,
cf. test_people_panel.py). TagsPrepLoader is run synchronously through run()
(the standard QThread pattern of the project, cf. CLAUDE.md)."""
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

    def test_catalog_tag_absent_from_selection_is_shown_unchecked(self, qtbot):
        photo = _photo("C:/lib/a.jpg", tags=["vacances"])
        dlg = TagEditDialog([photo], all_tags=["vacances", "famille"])
        qtbot.addWidget(dlg)

        assert dlg._chips["famille"].checkState() == Qt.Unchecked


class TestTagChipClickCycle:
    def test_checked_chip_click_goes_directly_to_unchecked(self, qtbot):
        photo = _photo("C:/lib/a.jpg", tags=["vacances"])
        dlg = TagEditDialog([photo], all_tags=[])
        qtbot.addWidget(dlg)
        chip = dlg._chips["vacances"]
        assert chip.checkState() == Qt.Checked

        chip.nextCheckState()
        assert chip.checkState() == Qt.Unchecked

    def test_click_cycle_never_revisits_partially_checked(self, qtbot):
        photo = _photo("C:/lib/a.jpg", tags=["vacances"])
        dlg = TagEditDialog([photo], all_tags=[])
        qtbot.addWidget(dlg)
        chip = dlg._chips["vacances"]

        seen = set()
        for _ in range(4):
            chip.nextCheckState()
            seen.add(chip.checkState())

        assert Qt.PartiallyChecked not in seen

    def test_partial_chip_click_goes_directly_to_checked(self, qtbot):
        p1 = _photo("C:/lib/a.jpg", tags=["vacances"])
        p2 = _photo("C:/lib/b.jpg", tags=[])
        dlg = TagEditDialog([p1, p2], all_tags=[])
        qtbot.addWidget(dlg)
        chip = dlg._chips["vacances"]
        assert chip.checkState() == Qt.PartiallyChecked

        chip.nextCheckState()
        assert chip.checkState() == Qt.Checked


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

    def test_catalog_tag_never_present_and_left_unchecked_is_not_reported(self, qtbot):
        photo = _photo("C:/lib/a.jpg", tags=["vacances"])
        dlg = TagEditDialog([photo], all_tags=["vacances", "famille"])
        qtbot.addWidget(dlg)

        to_add, to_remove = dlg.result_add_remove()
        assert to_add == ["vacances"]
        assert to_remove == []

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
        loader.run()  # synchronous, cf. the coverage/QThread trap of CLAUDE.md

        assert received == [["plage", "vacances"]]
