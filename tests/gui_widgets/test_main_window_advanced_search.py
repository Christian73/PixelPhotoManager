# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste MainWindow._open_advanced_search / _continue_advanced_search /
_run_advanced_search (menu Fichier › Recherche avancée…, Ctrl+F, ou bouton
loupe de la sidebar), en méthode non liée contre un objet minimal — comme
test_main_window_tags.py. AdvancedSearchDialog est remplacé par un double de
test (jamais de vrai exec() bloquant) ; AdvancedSearchPrepLoader (vrai
QThread, parenté à un QWidget vivant le temps du test) est laissé réel pour
couvrir la plomberie cross-thread."""
import pytest
from PySide6.QtWidgets import QDialog, QWidget

from src.core.models import PhotoInfo
from src.library.catalog import Catalog
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


class _FakeFaceDb:
    def __init__(self, photos_by_person: dict | None = None):
        self._photos_by_person = photos_by_person or {}

    def get_photos_for_person(self, person_id: int) -> list:
        return self._photos_by_person.get(person_id, [])


class _FakeConfig:
    def __init__(self, folders: list | None = None):
        self._folders = folders or []

    def get_scan_folders(self) -> list:
        return self._folders


class _FakeGridNavBar:
    def __init__(self):
        self.hidden = False

    def hide(self) -> None:
        self.hidden = True


class _FakeGrid:
    def __init__(self):
        self.ribbon_mode = None
        self.date_overlay = None

    def set_ribbon_mode(self, value) -> None:
        self.ribbon_mode = value

    def set_date_overlay_visible(self, value) -> None:
        self.date_overlay = value


class _FakeAdvancedSearchDialog:
    """Double de test pour AdvancedSearchDialog : pas de vrai exec() bloquant,
    résultat piloté via l'attribut de classe _next_result
    (exec_result, criteria, person_id)."""

    _next_result = (QDialog.Accepted, {}, None)

    def __init__(self, cameras, persons, all_tags, folders, parent=None):
        self.cameras = cameras
        self.persons = persons
        self.all_tags = all_tags
        self.folders = folders

    def exec(self):
        return type(self)._next_result[0]

    def get_criteria(self) -> dict:
        return type(self)._next_result[1]

    def get_person_id(self):
        return type(self)._next_result[2]


class _FakeMainWindow(QWidget):
    _open_advanced_search = MainWindow._open_advanced_search
    _continue_advanced_search = MainWindow._continue_advanced_search
    _run_advanced_search = MainWindow._run_advanced_search
    show_grid = lambda self: None  # noqa: E731

    def __init__(self, catalog, face_db=None, folders=None):
        super().__init__()
        self._catalog = catalog
        self._face_db = face_db or _FakeFaceDb()
        self._config = _FakeConfig(folders)
        self._grid = _FakeGrid()
        self._grid_nav_bar = _FakeGridNavBar()
        self.queries: list = []

    def _start_photo_query(self, fn, context_key, album_id=None, folder_path=None) -> None:
        self.queries.append((fn, context_key))


@pytest.fixture(autouse=True)
def _patch_dialog(monkeypatch):
    monkeypatch.setattr(main_window_module, "AdvancedSearchDialog", _FakeAdvancedSearchDialog)


class TestRunAdvancedSearch:
    def test_no_person_returns_search_advanced_result(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_photo("C:/photos/a.jpg"))
        catalog.set_favorite(p1.id, True)

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        result = fake._run_advanced_search({"favorites_only": True}, None)

        assert [p.filename for p in result] == ["a.jpg"]

    def test_person_filter_intersects_with_sql_criteria(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        a = catalog.add_or_update_photo(_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_photo("C:/photos/b.jpg"))
        face_db = _FakeFaceDb({7: [a.path]})

        fake = _FakeMainWindow(catalog, face_db=face_db)
        qtbot.addWidget(fake)
        result = fake._run_advanced_search({}, 7)

        assert [p.filename for p in result] == ["a.jpg"]

    def test_person_with_no_matching_photos_yields_empty(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_photo("C:/photos/a.jpg"))
        face_db = _FakeFaceDb({7: []})

        fake = _FakeMainWindow(catalog, face_db=face_db)
        qtbot.addWidget(fake)
        result = fake._run_advanced_search({}, 7)

        assert result == []


class TestContinueAdvancedSearch:
    def test_accepted_dialog_starts_query_with_expected_context(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        _FakeAdvancedSearchDialog._next_result = (
            QDialog.Accepted, {"favorites_only": True}, None
        )

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._continue_advanced_search([], [], [])

        assert len(fake.queries) == 1
        fn, context_key = fake.queries[0]
        assert context_key == "Recherche avancée"
        assert fn() == []  # aucune photo en base, mais la fonction s'exécute sans lever

    def test_cancelled_dialog_does_not_start_query(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        _FakeAdvancedSearchDialog._next_result = (QDialog.Rejected, {}, None)

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._continue_advanced_search([], [], [])

        assert fake.queries == []

    def test_resets_grid_ribbon_and_date_overlay_and_hides_nav_bar(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        _FakeAdvancedSearchDialog._next_result = (QDialog.Accepted, {}, None)

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._continue_advanced_search([], [], [])

        assert fake._grid.ribbon_mode is False
        assert fake._grid.date_overlay is False
        assert fake._grid_nav_bar.hidden is True


class TestOpenAdvancedSearch:
    def test_starts_prep_thread_then_opens_dialog_and_queries(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_photo("C:/photos/a.jpg", camera_make="Canon", camera_model="EOS R5"))
        _FakeAdvancedSearchDialog._next_result = (QDialog.Accepted, {"camera": "Canon"}, None)

        fake = _FakeMainWindow(catalog)
        qtbot.addWidget(fake)
        fake._open_advanced_search()

        qtbot.waitUntil(lambda: len(fake.queries) == 1, timeout=2000)
        fn, context_key = fake.queries[0]
        assert context_key == "Recherche avancée"
        assert [p.filename for p in fn()] == ["a.jpg"]
