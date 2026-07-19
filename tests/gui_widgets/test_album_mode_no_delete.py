# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Règle « vue album = jamais d'effacement de fichier » : en mode album
utilisateur (set_album_context(id)), l'effacement définitif ne doit être
possible ni par le menu contextuel ni par la touche Suppr, dans la grille
(mode normal et ruban) comme dans la visionneuse — seule la dé-association
de l'album est proposée. Hors album, l'effacement redevient disponible.

Les menus contextuels sont capturés en remplaçant QMenu.exec (pas d'affichage
réel) ; les signaux delete_requested / remove_from_album_requested font foi."""
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QMenu

from src.core.models import PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache
from src.ui.photo_viewer import PhotoViewer
from src.ui.thumbnail_grid import ThumbnailGrid


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=str(path), **kw)


def _del_key() -> QKeyEvent:
    return QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier)


@pytest.fixture
def captured_menus(monkeypatch):
    """Remplace QMenu par une sous-classe dont exec() n'affiche rien et
    enregistre le menu construit. Le remplacement se fait dans l'espace de
    noms des modules utilisateurs (le setattr direct sur la classe Shiboken
    n'intercepte pas l'appel natif : « missing signature »)."""
    menus: list[QMenu] = []

    class _CapturingMenu(QMenu):
        def exec(self, *a, **k):
            menus.append(self)
            return None

    import src.ui.photo_viewer as pv
    import src.ui.thumbnail_grid as tg
    monkeypatch.setattr(pv, "QMenu", _CapturingMenu)
    monkeypatch.setattr(tg, "QMenu", _CapturingMenu)
    return menus


def _action_texts(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if a.text()]


def _trigger(menu: QMenu, text: str) -> None:
    for a in menu.actions():
        if a.text() == text:
            a.trigger()
            return
    raise AssertionError(f"Action {text!r} absente du menu : {_action_texts(menu)}")


class _SignalSpy:
    """Capture delete_requested / remove_from_album_requested d'un widget."""

    def __init__(self, widget):
        self.deleted: list[list] = []
        self.removed: list[list] = []
        widget.delete_requested.connect(self.deleted.append)
        widget.remove_from_album_requested.connect(self.removed.append)


# ══════════════════════════════════════════════════════════════ grille


@pytest.fixture
def grid(qtbot, tmp_path):
    cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
    g = ThumbnailGrid(cache)
    qtbot.addWidget(g)
    return g


class TestGridContextMenu:
    def test_album_mode_offers_remove_never_delete(self, grid, captured_menus):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        grid.set_album_context(42)

        grid._on_right_click(p, None)

        texts = _action_texts(captured_menus[0])
        assert "Retirer de l'album" in texts
        assert not any("Effacer" in t for t in texts)

    def test_album_mode_multiselection_offers_remove_never_delete(
        self, grid, captured_menus
    ):
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])
        grid.set_album_context(42)
        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p2, Qt.ControlModifier)

        grid._on_right_click(p1, None)

        texts = _action_texts(captured_menus[0])
        assert "Retirer les photos de l'album" in texts
        assert not any("Effacer" in t for t in texts)

    def test_no_album_offers_delete_never_remove(self, grid, captured_menus):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        grid.set_album_context(None)

        grid._on_right_click(p, None)

        texts = _action_texts(captured_menus[0])
        assert "Effacer le fichier…" in texts
        assert not any("album" in t and "Retirer" in t for t in texts)

    def test_album_remove_action_emits_remove_signal(self, grid, captured_menus):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        grid.set_album_context(42)
        spy = _SignalSpy(grid)

        grid._on_right_click(p, None)
        _trigger(captured_menus[0], "Retirer de l'album")

        assert spy.removed == [[p]]
        assert spy.deleted == []

    def test_no_album_delete_action_emits_delete_signal(self, grid, captured_menus):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        spy = _SignalSpy(grid)

        grid._on_right_click(p, None)
        _trigger(captured_menus[0], "Effacer le fichier…")

        assert spy.deleted == [[p]]
        assert spy.removed == []


class TestGridDeleteKey:
    def _select(self, grid, photos):
        grid._on_cell_clicked(photos[0], Qt.NoModifier)
        for p in photos[1:]:
            grid._on_cell_clicked(p, Qt.ControlModifier)

    def test_album_mode_del_removes_from_album(self, grid):
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])
        grid.set_album_context(7)
        self._select(grid, [p1, p2])
        spy = _SignalSpy(grid)

        grid.keyPressEvent(_del_key())

        assert len(spy.removed) == 1
        assert {p.path for p in spy.removed[0]} == {p1.path, p2.path}
        assert spy.deleted == []

    def test_no_album_del_deletes(self, grid):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        self._select(grid, [p])
        spy = _SignalSpy(grid)

        grid.keyPressEvent(_del_key())

        assert spy.deleted == [[p]]
        assert spy.removed == []

    def test_ribbon_album_mode_del_selection_removes_from_album(self, grid):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        grid.set_ribbon_mode(True)
        grid.set_album_context(7)
        self._select(grid, [p])
        spy = _SignalSpy(grid)

        grid.keyPressEvent(_del_key())

        assert spy.removed == [[p]]
        assert spy.deleted == []

    def test_ribbon_album_mode_del_center_photo_removes_from_album(self, grid):
        """Ruban sans sélection : Suppr vise la photo centrale — elle aussi doit
        être retirée de l'album, jamais effacée."""
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])
        grid.set_ribbon_mode(True)
        grid.set_album_context(7)
        spy = _SignalSpy(grid)

        grid.keyPressEvent(_del_key())

        assert len(spy.removed) == 1
        assert spy.deleted == []

    def test_ribbon_no_album_del_deletes(self, grid):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        grid.set_ribbon_mode(True)
        self._select(grid, [p])
        spy = _SignalSpy(grid)

        grid.keyPressEvent(_del_key())

        assert spy.deleted == [[p]]
        assert spy.removed == []


# ══════════════════════════════════════════════════════════════ visionneuse


@pytest.fixture
def viewer(qtbot, tmp_path):
    v = PhotoViewer()
    qtbot.addWidget(v)
    # photo réelle : _show_context_menu lit l'EXIF GPS via PIL
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 24)).save(str(img_path))
    v._photo = _photo(img_path)
    return v


class TestViewerContextMenu:
    def test_album_mode_offers_remove_never_delete(self, viewer, captured_menus):
        viewer.set_album_context(42)
        viewer._show_context_menu(None)
        texts = _action_texts(captured_menus[0])
        assert "Retirer de l'album" in texts
        assert not any("Effacer" in t for t in texts)

    def test_no_album_offers_delete_never_remove(self, viewer, captured_menus):
        viewer.set_album_context(None)
        viewer._show_context_menu(None)
        texts = _action_texts(captured_menus[0])
        assert "Effacer le fichier…" in texts
        assert "Retirer de l'album" not in texts

    def test_album_remove_action_emits_remove_signal(self, viewer, captured_menus):
        viewer.set_album_context(42)
        spy = _SignalSpy(viewer)
        viewer._show_context_menu(None)
        _trigger(captured_menus[0], "Retirer de l'album")
        assert spy.removed == [[viewer._photo]]
        assert spy.deleted == []

    def test_no_album_delete_action_emits_delete_signal(self, viewer, captured_menus):
        spy = _SignalSpy(viewer)
        viewer._show_context_menu(None)
        _trigger(captured_menus[0], "Effacer le fichier…")
        assert spy.deleted == [[viewer._photo]]
        assert spy.removed == []


class TestViewerDeleteKey:
    def test_album_mode_del_removes_from_album(self, viewer):
        viewer.set_album_context(42)
        spy = _SignalSpy(viewer)
        viewer.keyPressEvent(_del_key())
        assert spy.removed == [[viewer._photo]]
        assert spy.deleted == []

    def test_no_album_del_deletes(self, viewer):
        viewer.set_album_context(None)
        spy = _SignalSpy(viewer)
        viewer.keyPressEvent(_del_key())
        assert spy.deleted == [[viewer._photo]]
        assert spy.removed == []

    def test_del_ignored_without_photo(self, viewer):
        viewer._photo = None
        spy = _SignalSpy(viewer)
        viewer.keyPressEvent(_del_key())
        assert spy.deleted == []
        assert spy.removed == []
