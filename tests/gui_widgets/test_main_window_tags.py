# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste MainWindow._on_edit_tags_requested / _continue_edit_tags (édition des
mots-clés depuis le menu contextuel de la grille ou de la visionneuse), en
méthode non liée contre un objet minimal — comme test_main_window_dvd_folder.py.
TagEditDialog est remplacé par un double de test (jamais de vrai exec()
bloquant) ; TagsPrepLoader (vrai QThread, parenté à un QWidget vivant le temps
du test) est laissé réel pour couvrir la plomberie cross-thread."""
import pytest
from PySide6.QtWidgets import QDialog, QWidget

from src.core.models import PhotoInfo
from src.library.catalog import Catalog
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow


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

    def current_photo(self):
        return self._photo


class _FakeTagDialog:
    """Double de test pour TagEditDialog : pas de vrai exec() bloquant, résultat
    piloté via l'attribut de classe _next_result (exec_result, to_add, to_remove)."""

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

    def __init__(self, catalog, current_photo=None, exif_visible: bool = False):
        super().__init__()
        self._catalog = catalog
        self._viewer = _FakeViewer(current_photo)
        self._exif_panel = _FakeExifPanel(exif_visible)


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
        # instance distincte, même chemin : grille et visionneuse ne partagent
        # pas forcément le même objet PhotoInfo pour un chemin donné.
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

        fake._on_edit_tags_requested([])  # ne doit pas lever, ne démarre aucun thread

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
