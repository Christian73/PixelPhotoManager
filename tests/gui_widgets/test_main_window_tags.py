# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests MainWindow._on_edit_tags_requested / _continue_edit_tags (editing the
keywords from the context menu of the grid or of the viewer), as an unbound
method against a minimal object -- like test_main_window_dvd_folder.py.
TagEditDialog is replaced by a test double (never a real blocking exec());
TagsPrepLoader (a real QThread, parented to a QWidget alive for the duration
of the test) is left real to cover the cross-thread plumbing."""
import pytest
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.core.models import PhotoInfo
from src.library.catalog import Catalog
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow
from src.ui.sidebar import _SPECIAL_ALL


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


class _FakeExifPanel:
    def __init__(self, visible: bool = False):
        self._visible = visible
        self.tags_set = None

    def isVisible(self) -> bool:
        return self._visible

    def set_tags(self, tags) -> None:
        self.tags_set = list(tags)


class _FakeViewer:
    def __init__(self, photo=None):
        self._photo = photo
        self.available_tags = None
        self.refresh_tags_calls = 0

    def current_photo(self):
        return self._photo

    def set_available_tags(self, tags) -> None:
        self.available_tags = list(tags)

    def refresh_tags(self) -> None:
        self.refresh_tags_calls += 1


class _FakeSidebar:
    def __init__(self):
        self.refreshed_tags = None
        self.selected = None

    def refresh_tags(self, tags) -> None:
        self.refreshed_tags = list(tags)

    def select_album_item(self, data) -> None:
        self.selected = data


class _FakeTagDialog:
    """Test double for TagEditDialog: no real blocking exec(), the result is
    driven through the _next_result class attribute (exec_result, to_add, to_remove)."""

    _next_result = (QDialog.Accepted, [], [])

    def __init__(self, photos, all_tags, parent=None):
        self.photos = photos
        self.all_tags = all_tags

    def exec(self):
        return type(self)._next_result[0]

    def result_add_remove(self):
        return type(self)._next_result[1], type(self)._next_result[2]


class _FakeMainWindow(QWidget):
    _on_edit_tags_requested = MainWindow._on_edit_tags_requested
    _continue_edit_tags = MainWindow._continue_edit_tags
    _on_tag_delete_requested = MainWindow._on_tag_delete_requested

    def __init__(self, catalog, current_photo=None, exif_visible: bool = False,
                 current_context: str = "Toutes les photos"):
        super().__init__()
        self._catalog = catalog
        self._viewer = _FakeViewer(current_photo)
        self._exif_panel = _FakeExifPanel(exif_visible)
        self._sidebar = _FakeSidebar()
        self._current_context = current_context
        self.show_all_photos_calls = 0

    def _show_all_photos(self) -> None:
        self.show_all_photos_calls += 1
        self._current_context = "Toutes les photos"


@pytest.fixture(autouse=True)
def _patch_dialog(monkeypatch):
    monkeypatch.setattr(main_window_module, "TagEditDialog", _FakeTagDialog)


class TestContinueEditTags:
    def test_applies_add_and_remove_to_catalog_and_memory(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vieux"]))
        photo = catalog.get_photo_by_path(saved.path)
        _FakeTagDialog._next_result = (QDialog.Accepted, ["nouveau"], ["vieux"])

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._continue_edit_tags([photo], [])

        assert catalog.get_photo_by_path(photo.path).tags == ["nouveau"]
        assert photo.tags == ["nouveau"]

    def test_cancel_leaves_catalog_untouched(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vieux"]))
        photo = catalog.get_photo_by_path(saved.path)
        _FakeTagDialog._next_result = (QDialog.Rejected, ["nouveau"], [])

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._continue_edit_tags([photo], [])

        assert catalog.get_photo_by_path(photo.path).tags == ["vieux"]

    def test_refreshes_exif_panel_when_visible_and_matching_current_photo(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg"))
        photo = catalog.get_photo_by_path(saved.path)
        # a distinct instance, the same path: the grid and the viewer do not
        # necessarily share the same PhotoInfo object for a given path.
        current = _photo(photo.path)
        _FakeTagDialog._next_result = (QDialog.Accepted, ["été"], [])

        fake = _FakeMainWindow(catalog, current_photo=current, exif_visible=True)
        qtbot.addWidget(fake)
        fake._continue_edit_tags([photo], [])

        assert current.tags == ["été"]
        assert fake._exif_panel.tags_set == ["été"]

    def test_exif_panel_untouched_when_not_visible(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg"))
        photo = catalog.get_photo_by_path(saved.path)
        current = _photo(photo.path)
        _FakeTagDialog._next_result = (QDialog.Accepted, ["été"], [])

        fake = _FakeMainWindow(catalog, current_photo=current, exif_visible=False)
        qtbot.addWidget(fake)
        fake._continue_edit_tags([photo], [])

        assert fake._exif_panel.tags_set is None

    def test_exif_panel_untouched_when_current_photo_not_in_selection(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg"))
        photo = catalog.get_photo_by_path(saved.path)
        current = _photo("C:/photos/other.jpg")
        _FakeTagDialog._next_result = (QDialog.Accepted, ["été"], [])

        fake = _FakeMainWindow(catalog, current_photo=current, exif_visible=True)
        qtbot.addWidget(fake)
        fake._continue_edit_tags([photo], [])

        assert fake._exif_panel.tags_set is None


class TestOnEditTagsRequested:
    def test_empty_selection_is_noop(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)

        fake._on_edit_tags_requested([])  # must not raise, starts no thread

    def test_starts_prep_thread_then_applies_dialog_result(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg"))
        photo = catalog.get_photo_by_path(saved.path)
        _FakeTagDialog._next_result = (QDialog.Accepted, ["plage"], [])

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._on_edit_tags_requested([photo])

        qtbot.waitUntil(
            lambda: catalog.get_photo_by_path(photo.path).tags == ["plage"], timeout=2000
        )


class TestOnTagDeleteRequested:
    def test_confirmed_removes_tag_from_catalog_and_refreshes_sidebar_and_viewer(
        self, qtbot, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vacances", "été"]))
        photo = catalog.get_photo_by_path(saved.path)

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._on_tag_delete_requested("vacances")

        assert catalog.get_photo_by_path(photo.path).tags == ["été"]
        assert fake._sidebar.refreshed_tags == ["été"]
        assert fake._viewer.available_tags == ["été"]

    def test_cancelled_leaves_catalog_untouched(self, qtbot, tmp_path, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vacances"]))
        photo = catalog.get_photo_by_path(saved.path)

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._on_tag_delete_requested("vacances")

        assert catalog.get_photo_by_path(photo.path).tags == ["vacances"]
        assert fake._sidebar.refreshed_tags is None

    def test_redirects_to_all_photos_when_viewing_deleted_tag_album(
        self, qtbot, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vacances"]))

        fake = _FakeMainWindow(catalog, current_context="Mot-clé : vacances")
        qtbot.addWidget(fake)
        fake._on_tag_delete_requested("vacances")

        assert fake._sidebar.selected == _SPECIAL_ALL
        assert fake.show_all_photos_calls == 1

    def test_does_not_redirect_when_viewing_a_different_context(
        self, qtbot, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vacances"]))

        fake = _FakeMainWindow(catalog, current_context="Mot-clé : été")
        qtbot.addWidget(fake)
        fake._on_tag_delete_requested("vacances")

        assert fake._sidebar.selected is None
        assert fake.show_all_photos_calls == 0

    def test_updates_current_photo_and_visible_exif_panel(self, qtbot, tmp_path, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vacances", "été"]))
        photo = catalog.get_photo_by_path(saved.path)
        current = _photo(photo.path, tags=["vacances", "été"])

        fake = _FakeMainWindow(catalog, current_photo=current, exif_visible=True)
        qtbot.addWidget(fake)
        fake._on_tag_delete_requested("vacances")

        assert current.tags == ["été"]
        assert fake._viewer.refresh_tags_calls == 1
        assert fake._exif_panel.tags_set == ["été"]

    def test_current_photo_without_tag_is_untouched(self, qtbot, tmp_path, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_photo("C:/photos/a.jpg", tags=["vacances"]))
        current = _photo("C:/photos/other.jpg", tags=["été"])

        fake = _FakeMainWindow(catalog, current_photo=current, exif_visible=True)
        qtbot.addWidget(fake)
        fake._on_tag_delete_requested("vacances")

        assert current.tags == ["été"]
        assert fake._viewer.refresh_tags_calls == 0
        assert fake._exif_panel.tags_set is None
