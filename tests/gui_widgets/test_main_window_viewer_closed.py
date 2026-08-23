# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests MainWindow._on_viewer_closed: on the way back from the viewer to the
grid, the last displayed photo must become visible and selected again
in the grid (cf. ThumbnailGrid.scroll_to_photo / select_photo). An unbound
method against a minimal object, the same style as test_main_window_tags.py."""
from PySide6.QtWidgets import QWidget

from src.core.models import PhotoInfo
from src.ui.main_window import MainWindow


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


class _FakeViewer:
    def __init__(self, photo=None):
        self._photo = photo

    def current_photo(self):
        return self._photo


class _FakeGrid:
    def __init__(self):
        self.scrolled_to: list[str] = []
        self.selected: list[str] = []

    def scroll_to_photo(self, path: str) -> None:
        self.scrolled_to.append(path)

    def select_photo(self, path: str) -> None:
        self.selected.append(path)


class _FakeMainWindow(QWidget):
    _on_viewer_closed = MainWindow._on_viewer_closed

    def __init__(self, current_photo=None, back_target: str = "grid"):
        super().__init__()
        self._viewer = _FakeViewer(current_photo)
        self._grid = _FakeGrid()
        self._viewer_back_target = back_target
        self._person_cluster_view = None
        self.show_grid_calls = 0
        self.show_person_clusters_calls: list = []
        self.show_duplicate_grid_calls = 0

    def show_grid(self) -> None:
        self.show_grid_calls += 1

    def show_person_clusters(self, person) -> None:
        self.show_person_clusters_calls.append(person)

    def show_duplicate_grid(self) -> None:
        self.show_duplicate_grid_calls += 1


class TestViewerClosedRestoresGridPosition:
    def test_grid_target_scrolls_and_selects_last_photo(self, qtbot):
        photo = _photo("C:/lib/a.jpg")
        fake = _FakeMainWindow(current_photo=photo)
        qtbot.addWidget(fake)

        fake._on_viewer_closed()

        assert fake.show_grid_calls == 1
        assert fake._grid.scrolled_to == [photo.path]
        assert fake._grid.selected == [photo.path]

    def test_no_current_photo_does_not_touch_grid(self, qtbot):
        fake = _FakeMainWindow(current_photo=None)
        qtbot.addWidget(fake)

        fake._on_viewer_closed()

        assert fake.show_grid_calls == 1
        assert fake._grid.scrolled_to == []
        assert fake._grid.selected == []

    def test_resets_back_target_to_grid(self, qtbot):
        fake = _FakeMainWindow(current_photo=_photo("C:/lib/a.jpg"),
                                back_target="duplicate_grid")
        qtbot.addWidget(fake)

        fake._on_viewer_closed()

        assert fake._viewer_back_target == "grid"

    def test_duplicate_grid_target_does_not_touch_main_grid(self, qtbot):
        fake = _FakeMainWindow(current_photo=_photo("C:/lib/a.jpg"),
                                back_target="duplicate_grid")
        qtbot.addWidget(fake)

        fake._on_viewer_closed()

        assert fake.show_duplicate_grid_calls == 1
        assert fake.show_grid_calls == 0
        assert fake._grid.scrolled_to == []
        assert fake._grid.selected == []

    def test_person_cluster_view_target_does_not_touch_main_grid(self, qtbot):
        person = object()

        class _PersonView:
            current_person = person

        fake = _FakeMainWindow(current_photo=_photo("C:/lib/a.jpg"),
                                back_target="person_cluster_view")
        fake._person_cluster_view = _PersonView()
        qtbot.addWidget(fake)

        fake._on_viewer_closed()

        assert fake.show_person_clusters_calls == [person]
        assert fake.show_grid_calls == 0
        assert fake._grid.scrolled_to == []
        assert fake._grid.selected == []
