# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""The "album view = never a file deletion" rule: in user album mode
(set_album_context(id)), permanent deletion must be possible neither through
the context menu nor through the Del key, in the grid (normal and filmstrip
modes) as well as in the viewer -- only removal from the album is offered.
Outside an album, deletion becomes available again.

The context menus are captured by replacing QMenu.exec (no real display);
the delete_requested / remove_from_album_requested signals are what counts."""
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
    """Replaces QMenu with a subclass whose exec() displays nothing and
    records the menu that was built. The replacement is done in the namespace
    of the using modules (a direct setattr on the Shiboken class does not
    intercept the native call: "missing signature")."""
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
    """Captures the delete_requested / remove_from_album_requested of a widget."""

    def __init__(self, widget):
        self.deleted: list[list] = []
        self.removed: list[list] = []
        widget.delete_requested.connect(self.deleted.append)
        widget.remove_from_album_requested.connect(self.removed.append)


# ══════════════════════════════════════════════════════════════ grid


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
        assert "Remove from the album\tDel" in texts
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
        assert "Remove the photos from the album\tDel" in texts
        assert not any("Effacer" in t for t in texts)

    def test_no_album_offers_delete_never_remove(self, grid, captured_menus):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        grid.set_album_context(None)

        grid._on_right_click(p, None)

        texts = _action_texts(captured_menus[0])
        assert "Delete the file…\tDel" in texts
        assert not any("album" in t and "Remove" in t for t in texts)

    def test_album_remove_action_emits_remove_signal(self, grid, captured_menus):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        grid.set_album_context(42)
        spy = _SignalSpy(grid)

        grid._on_right_click(p, None)
        _trigger(captured_menus[0], "Remove from the album\tDel")

        assert spy.removed == [[p]]
        assert spy.deleted == []

    def test_no_album_delete_action_emits_delete_signal(self, grid, captured_menus):
        p = _photo("C:/lib/a.jpg")
        grid.set_photos([p])
        spy = _SignalSpy(grid)

        grid._on_right_click(p, None)
        _trigger(captured_menus[0], "Delete the file…\tDel")

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
        """Filmstrip with no selection: Del aims at the central photo -- it too
        must be removed from the album, never deleted."""
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


# ══════════════════════════════════════════════════════════════ viewer


@pytest.fixture
def viewer(qtbot, tmp_path):
    v = PhotoViewer()
    qtbot.addWidget(v)
    # a real photo: _show_context_menu reads the GPS EXIF through PIL
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 24)).save(str(img_path))
    v._photo = _photo(img_path)
    return v


class TestViewerContextMenu:
    def test_album_mode_offers_remove_never_delete(self, viewer, captured_menus):
        viewer.set_album_context(42)
        viewer._show_context_menu(None)
        texts = _action_texts(captured_menus[0])
        assert "Remove from the album\tDel" in texts
        assert not any("Effacer" in t for t in texts)

    def test_no_album_offers_delete_never_remove(self, viewer, captured_menus):
        viewer.set_album_context(None)
        viewer._show_context_menu(None)
        texts = _action_texts(captured_menus[0])
        assert "Delete the file…\tDel" in texts
        assert "Remove from the album\tDel" not in texts

    def test_album_remove_action_emits_remove_signal(self, viewer, captured_menus):
        viewer.set_album_context(42)
        spy = _SignalSpy(viewer)
        viewer._show_context_menu(None)
        _trigger(captured_menus[0], "Remove from the album\tDel")
        assert spy.removed == [[viewer._photo]]
        assert spy.deleted == []

    def test_no_album_delete_action_emits_delete_signal(self, viewer, captured_menus):
        spy = _SignalSpy(viewer)
        viewer._show_context_menu(None)
        _trigger(captured_menus[0], "Delete the file…\tDel")
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
