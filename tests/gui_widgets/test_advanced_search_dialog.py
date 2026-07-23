# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour advanced_search_dialog —
AdvancedSearchDialog n'est jamais exec() : on pilote ses widgets directement
(comme TagEditDialog, cf. test_tag_dialog.py). AdvancedSearchPrepLoader est
exécuté en synchrone via run() (pattern QThread standard du projet)."""
from src.core.models import PersonInfo
from src.library.catalog import Catalog
from src.ui.advanced_search_dialog import AdvancedSearchDialog, AdvancedSearchPrepLoader


class TestAdvancedSearchDialogCriteria:
    def test_no_input_produces_empty_criteria(self, qtbot):
        dlg = AdvancedSearchDialog(cameras=[], persons=[], all_tags=[], folders=[])
        qtbot.addWidget(dlg)

        assert dlg.get_criteria() == {}
        assert dlg.get_person_id() is None

    def test_dates_only_included_when_checkbox_checked(self, qtbot):
        dlg = AdvancedSearchDialog(cameras=[], persons=[], all_tags=[], folders=[])
        qtbot.addWidget(dlg)

        assert "date_from" not in dlg.get_criteria()

        dlg._chk_dates.setChecked(True)
        criteria = dlg.get_criteria()
        assert "date_from" in criteria
        assert "date_to" in criteria

    def test_camera_text_included_when_set(self, qtbot):
        dlg = AdvancedSearchDialog(
            cameras=["Canon EOS R5"], persons=[], all_tags=[], folders=[]
        )
        qtbot.addWidget(dlg)
        dlg._camera_combo.setCurrentText("Canon EOS R5")

        assert dlg.get_criteria()["camera"] == "Canon EOS R5"

    def test_directory_text_included_when_set(self, qtbot):
        dlg = AdvancedSearchDialog(
            cameras=[], persons=[], all_tags=[], folders=["C:/photos/2024"]
        )
        qtbot.addWidget(dlg)
        dlg._folder_combo.setCurrentText("C:/photos/2024")

        assert dlg.get_criteria()["directory"] == "C:/photos/2024"

    def test_min_rating_included_when_stars_clicked(self, qtbot):
        dlg = AdvancedSearchDialog(cameras=[], persons=[], all_tags=[], folders=[])
        qtbot.addWidget(dlg)
        dlg._stars.set_rating(3)

        assert dlg.get_criteria()["min_rating"] == 3

    def test_tags_added_via_input_are_included(self, qtbot):
        dlg = AdvancedSearchDialog(cameras=[], persons=[], all_tags=[], folders=[])
        qtbot.addWidget(dlg)
        dlg._tag_input.setText("vacances")
        dlg._add_tag_filter()
        dlg._tag_input.setText("plage")
        dlg._add_tag_filter()

        assert dlg.get_criteria()["tags"] == ["vacances", "plage"]

    def test_blank_or_duplicate_tag_input_is_ignored(self, qtbot):
        dlg = AdvancedSearchDialog(cameras=[], persons=[], all_tags=[], folders=[])
        qtbot.addWidget(dlg)
        dlg._tag_input.setText("vacances")
        dlg._add_tag_filter()
        dlg._tag_input.setText("   ")
        dlg._add_tag_filter()
        dlg._tag_input.setText("vacances")
        dlg._add_tag_filter()

        assert dlg.get_criteria()["tags"] == ["vacances"]

    def test_favorites_only_included_when_checked(self, qtbot):
        dlg = AdvancedSearchDialog(cameras=[], persons=[], all_tags=[], folders=[])
        qtbot.addWidget(dlg)
        dlg._chk_favorites.setChecked(True)

        assert dlg.get_criteria()["favorites_only"] is True

    def test_media_type_included_when_not_all(self, qtbot):
        dlg = AdvancedSearchDialog(cameras=[], persons=[], all_tags=[], folders=[])
        qtbot.addWidget(dlg)

        assert "media_type" not in dlg.get_criteria()

        dlg._media_combo.setCurrentIndex(2)  # Vidéos
        assert dlg.get_criteria()["media_type"] == "video"

    def test_person_selection_returned_by_get_person_id(self, qtbot):
        persons = [PersonInfo(name="Alice", id=1), PersonInfo(name="Bob", id=2)]
        dlg = AdvancedSearchDialog(cameras=[], persons=persons, all_tags=[], folders=[])
        qtbot.addWidget(dlg)

        dlg._person_combo.setCurrentIndex(2)  # Bob

        assert dlg.get_person_id() == 2


class TestAdvancedSearchPrepLoader:
    def test_run_emits_cameras_persons_tags(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        from src.core.models import PhotoInfo

        photo = catalog.add_or_update_photo(
            PhotoInfo(path="C:/lib/a.jpg", camera_make="Canon", camera_model="EOS R5")
        )
        catalog.set_tags(photo.id, ["vacances"])
        catalog.create_person("Alice")

        loader = AdvancedSearchPrepLoader(catalog)
        received = []
        loader.ready.connect(lambda cams, persons, tags: received.append((cams, persons, tags)))
        loader.run()  # synchrone, cf. CLAUDE.md piège coverage/QThread

        assert len(received) == 1
        cameras, persons, tags = received[0]
        assert cameras == ["Canon EOS R5"]
        assert [p.name for p in persons] == ["Alice"]
        assert tags == ["vacances"]
