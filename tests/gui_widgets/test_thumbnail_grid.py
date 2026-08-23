# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Isolated Qt widget tests (Layer 2, pytest-qt) for ThumbnailGrid - no
catalog and no real library: ThumbnailCache and the PhotoInfo objects are
entirely synthetic, instantiated in process."""
import io

from PIL import Image
from PySide6.QtCore import Qt

from src.core.models import EditInfo, PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache, edit_signature
from src.ui.thumbnail_grid import ThumbnailCell, ThumbnailGrid


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


def _jpeg_bytes(size=(32, 24)) -> bytes:
    """Valid JPEG bytes - what the thumbnail worker emits."""
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_grid(qtbot, tmp_path) -> ThumbnailGrid:
    cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
    grid = ThumbnailGrid(cache)
    qtbot.addWidget(grid)
    return grid


class TestSetPhotos:
    def test_set_photos_stores_all_photos(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        photos = [_photo(f"C:/lib/p{i}.jpg") for i in range(5)]
        grid.set_photos(photos)
        assert len(grid._photos) == 5
        assert grid.get_selected() == []

    def test_set_photos_clears_previous_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])
        grid._on_cell_clicked(p1, Qt.NoModifier)
        assert grid.get_selected() == [p1]

        grid.set_photos([p1, p2])
        assert grid.get_selected() == []


class TestEmptyMessage:
    """show_empty_message/clear_empty_message - used by MainWindow to
    signal a folder empty of catalogued photos but in fact containing
    a DVD copy (VIDEO_TS), with an action to open it externally."""

    def test_show_empty_message_displays_text_and_action(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        calls: list = []

        grid.show_empty_message("Copie de DVD détectée", "Open", lambda: calls.append(1))

        assert grid._empty_overlay.isVisible()
        assert grid._empty_label.text() == "Copie de DVD détectée"
        assert grid._empty_action_btn.isVisible()
        grid._empty_action_btn.click()
        assert calls == [1]

    def test_show_empty_message_without_action_hides_button(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)

        grid.show_empty_message("Dossier vide")

        assert grid._empty_overlay.isVisible()
        assert not grid._empty_action_btn.isVisible()

    def test_clear_empty_message_hides_overlay(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        grid.show_empty_message("Copie de DVD détectée", "Open", lambda: None)

        grid.clear_empty_message()

        assert not grid._empty_overlay.isVisible()

    def test_set_photos_clears_previous_empty_message(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        grid.show_empty_message("Copie de DVD détectée", "Open", lambda: None)

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert not grid._empty_overlay.isVisible()

    def test_second_action_replaces_first_connection(self, qtbot, tmp_path):
        """A second show_empty_message must not stack the connections of the
        clicked signal (otherwise a click triggers N callbacks after N calls)."""
        grid = _make_grid(qtbot, tmp_path)
        calls: list = []
        grid.show_empty_message("Message 1", "Open", lambda: calls.append("first"))
        grid.show_empty_message("Message 2", "Open", lambda: calls.append("second"))

        grid._empty_action_btn.click()

        assert calls == ["second"]


class TestCellClickSelection:
    def test_plain_click_selects_single_photo_and_emits_signal(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        with qtbot.waitSignal(grid.selection_changed, timeout=1000) as blocker:
            grid._on_cell_clicked(p1, Qt.NoModifier)
        assert blocker.args == [[p1]]
        assert grid.get_selected() == [p1]

    def test_ctrl_click_adds_to_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p2, Qt.ControlModifier)
        assert {p.path for p in grid.get_selected()} == {p1.path, p2.path}

    def test_ctrl_click_on_selected_photo_deselects_it(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1 = _photo("C:/lib/a.jpg")
        grid.set_photos([p1])

        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p1, Qt.ControlModifier)
        assert grid.get_selected() == []

    def test_plain_click_replaces_multi_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p2, Qt.ControlModifier)
        assert len(grid.get_selected()) == 2

        grid._on_cell_clicked(p1, Qt.NoModifier)
        assert grid.get_selected() == [p1]


class TestDuplicateBadgeForwarding:
    def test_cell_duplicate_clicked_forwards_to_grid_signal(self, qtbot, tmp_path):
        """Wiring regression: the duplicate badge of a cell must
        reach the `duplicate_clicked` signal of the grid itself
        (cf. _make_cell in thumbnail_grid.py)."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/dup.jpg", duplicate_group_id=3)
        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)

        with qtbot.waitSignal(grid.duplicate_clicked, timeout=1000) as blocker:
            cell.duplicate_clicked.emit(photo)
        assert blocker.args == [photo]


class TestNoGhostCellWindows:
    def test_dematerialize_leaves_no_visible_toplevel_cells(self, qtbot, tmp_path):
        """Preventive guard - the same root as the bug of the cards of
        DuplicateGrid (2026-07-19): setParent(None) on a widget still
        visible turns it into a displayable top-level window. Unlike the
        cards (referenced until the deleteLater), the detached cells
        are destroyed by the GC as soon as _materialized.clear(), so the ghost
        is not reproducible here without the fix - this test locks down
        the invariant "detached cell => hidden" without proving the regression."""
        from PySide6.QtWidgets import QApplication
        from src.ui.thumbnail_grid import ThumbnailCell

        grid = _make_grid(qtbot, tmp_path)
        grid.resize(640, 420)
        grid.show()
        qtbot.waitExposed(grid)
        grid.set_photos([_photo(f"C:/lib/p{i}.jpg") for i in range(12)])
        qtbot.wait(150)   # materialisation of the visible area
        assert grid._materialized   # precondition: cells exist

        grid.set_ribbon_mode(True)  # _dematerialize_all()

        # processEvents and not qtbot.wait: cf. TestNoGhostWindows in
        # test_duplicate_grid.py - an exec() would process the DeferredDelete
        # events and hide the ghost window.
        import time
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            QApplication.processEvents()

        ghosts = [
            w for w in QApplication.topLevelWidgets()
            if isinstance(w, ThumbnailCell) and w.isVisible()
        ]
        assert ghosts == []
        qtbot.wait(50)
        grid.close()


class TestFavoriteToggleFromMenu:
    def test_toggle_favorite_flips_state_and_emits_signal(self, qtbot, tmp_path):
        """Regression: the "Mark as favourite" context menu was
        wired to no callback (fav_label added with no action) - the action
        therefore did strictly nothing. This test would have failed before the
        fix since no signal was ever emitted."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/fav.jpg", is_favorite=False)
        grid.set_photos([photo])

        with qtbot.waitSignal(grid.favorite_toggle_requested, timeout=1000) as blocker:
            grid._toggle_favorite_from_menu(photo)
        assert blocker.args == [photo]
        assert photo.is_favorite is True

        with qtbot.waitSignal(grid.favorite_toggle_requested, timeout=1000):
            grid._toggle_favorite_from_menu(photo)
        assert photo.is_favorite is False


class TestRatingChangeFromMenu:
    def test_emit_rating_change_forwards_photos_and_rating(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        with qtbot.waitSignal(grid.rating_change_requested, timeout=1000) as blocker:
            grid._emit_rating_change([p1, p2], 4)
        assert blocker.args == [[p1, p2], 4]


class TestRefreshRating:
    def test_refresh_rating_updates_photo_and_cell(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])
        cell = grid._make_cell(photo)
        grid._materialized[0] = cell
        qtbot.addWidget(cell)

        grid.refresh_rating({photo.path: 3})

        assert photo.rating == 3
        assert cell.photo.rating == 3

    def test_refresh_rating_ignores_unrelated_paths(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])

        grid.refresh_rating({"C:/lib/other.jpg": 5})

        assert photo.rating == 0


class TestRatingBadge:
    def test_set_rating_redraws_pixmap_when_already_loaded(self, qtbot, tmp_path):
        from PySide6.QtGui import QPixmap

        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)
        cell._set_pixmap(QPixmap(40, 40))

        cell.set_rating(4)

        assert cell.photo.rating == 4
        assert cell._pixmap is not None  # badge redrawn without crashing

    def test_set_pixmap_with_rating_does_not_crash(self, qtbot, tmp_path):
        """_add_rating_badge must apply without error for each rating 1-5."""
        from PySide6.QtGui import QPixmap

        grid = _make_grid(qtbot, tmp_path)
        for n in range(1, 6):
            photo = _photo(f"C:/lib/r{n}.jpg", rating=n)
            cell = grid._make_cell(photo)
            qtbot.addWidget(cell)
            cell._set_pixmap(QPixmap(40, 40))
            assert cell._pixmap is not None


class TestRemovePhotos:
    def test_remove_photos_updates_list_and_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2, p3 = (_photo(f"C:/lib/{n}.jpg") for n in "abc")
        grid.set_photos([p1, p2, p3])
        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p2, Qt.ControlModifier)

        with qtbot.waitSignal(grid.selection_changed, timeout=1000):
            grid.remove_photos([p1.path])

        assert p1 not in grid._photos
        assert grid.get_selected() == [p2]


class TestScrollToPhoto:
    """scroll_to_photo in normal mode (outside the ribbon): returning from the viewer
    to the grid, the thumbnail of the last photo displayed must become
    visible again with no useless scrolling if it already is."""

    def test_offscreen_photo_scrolls_into_view(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.resize(300, 200)
        grid.show()
        qtbot.waitExposed(grid)
        grid.set_thumbnail_size(80)
        photos = [_photo(f"C:/lib/p{i}.jpg") for i in range(60)]
        grid.set_photos(photos)
        last = photos[-1]

        grid.scroll_to_photo(last.path)

        idx = len(photos) - 1
        rect = grid._container.cell_rect(idx)
        vbar = grid.verticalScrollBar()
        assert vbar.value() > 0
        assert rect.top() >= vbar.value()
        assert rect.bottom() <= vbar.value() + grid.viewport().height()

    def test_already_visible_photo_does_not_scroll(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.resize(300, 400)
        grid.show()
        qtbot.waitExposed(grid)
        photos = [_photo(f"C:/lib/p{i}.jpg") for i in range(3)]
        grid.set_photos(photos)

        grid.scroll_to_photo(photos[0].path)

        assert grid.verticalScrollBar().value() == 0

    def test_unknown_path_is_noop(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.set_photos([_photo("C:/lib/a.jpg")])

        grid.scroll_to_photo("C:/lib/missing.jpg")   # must not raise


class TestLoadingIndicator:
    """set_loading - immediate visual feedback when a photo query starts
    (folder/album click in the sidebar): the "Loading..." indicator only appears
    after 150 ms (no flicker on the quick queries) and is hidden
    automatically as soon as set_photos() delivers the result."""

    def test_indicator_appears_after_delay(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)

        grid.set_loading(True)
        assert not grid._loading_label.isVisible()   # deferred by 150 ms

        qtbot.waitUntil(lambda: grid._loading_label.isVisible(), timeout=2000)

    def test_fast_query_never_shows_indicator(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)

        grid.set_loading(True)
        grid.set_photos([_photo("C:/lib/a.jpg")])    # answer before the 150 ms

        assert not grid._loading_label.isVisible()
        assert not grid._loading_delay_timer.isActive()

    def test_set_photos_hides_visible_indicator(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        grid.set_loading(True)
        qtbot.waitUntil(lambda: grid._loading_label.isVisible(), timeout=2000)

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert not grid._loading_label.isVisible()


class TestEditedThumbnails:
    """A photo rotated/cropped in the viewer must appear retouched
    in the grid. Difficulty: the grid is virtualised - at the moment of the
    edit, the cell of the photo most often does not exist, so there is
    nothing to refresh. The state is memorised in grid._edits and passed to the
    cell at the moment it is (re)built."""

    def test_refresh_photo_records_edit_without_materialized_cell(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.set_photos([_photo("C:/lib/a.jpg")])
        grid._dematerialize_all()
        edit = EditInfo(rotation=90)

        grid.refresh_photo("C:/lib/a.jpg", edit)

        assert grid._edit_for("C:/lib/a.jpg") is edit

    def test_cell_built_later_receives_the_recorded_edit(self, qtbot, tmp_path):
        """The core of the fix: the cell created afterwards knows about the edit
        and will therefore ask for the retouched thumbnail, not the original one."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])
        edit = EditInfo(rotation=90)
        grid.refresh_photo(photo.path, edit)

        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)

        assert cell._edit is edit

    def test_reset_removes_the_recorded_edit(self, qtbot, tmp_path):
        """Cancellation of the edits: the next cell must start again from scratch."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])
        grid.refresh_photo(photo.path, EditInfo(rotation=90))

        grid.refresh_photo(photo.path, EditInfo())

        assert grid._edit_for(photo.path) is None
        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)
        assert cell._edit is None

    def test_edits_are_keyed_on_normalized_paths(self, qtbot, tmp_path):
        """The viewer and the catalog do not always deliver the path with
        the same separators - without normalisation, the edit would be recorded
        under a key that _edit_for() never finds again."""
        grid = _make_grid(qtbot, tmp_path)
        edit = EditInfo(rotation=90)

        grid.refresh_photo("C:/lib/sub/a.jpg", edit)

        assert grid._edit_for("C:\\lib\\sub\\a.jpg") is edit

    def test_set_photos_reloads_edits_from_provider(self, qtbot, tmp_path):
        """At startup as at every folder change, the edits already
        recorded in the database must be picked up - otherwise a photo retouched
        during a previous session would reappear unretouched."""
        grid = _make_grid(qtbot, tmp_path)
        edit = EditInfo(rotation=90)
        grid.set_edit_provider(lambda: {"C:\\lib\\a.jpg": edit})

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert grid._edit_for("C:/lib/a.jpg") is edit

    def test_provider_failure_does_not_break_the_grid(self, qtbot, tmp_path):
        """An unreadable edit database must not prevent displaying the
        photos (degradation: unretouched thumbnails)."""
        grid = _make_grid(qtbot, tmp_path)
        grid._edit_provider = lambda: (_ for _ in ()).throw(RuntimeError("db down"))

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert len(grid._photos) == 1
        assert grid._edit_for("C:/lib/a.jpg") is None


class TestCellEditSignature:
    """ThumbnailCell - the edit fingerprint accompanies the thumbnail from end to
    end (request to the cache, generation, caching, display)."""

    def _cell(self, qtbot, tmp_path, edit):
        cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
        cell = ThumbnailCell(_photo("C:/lib/a.jpg"), cache, 128, edit=edit)
        qtbot.addWidget(cell)
        return cell

    def test_ready_result_from_a_superseded_edit_is_not_displayed(self, qtbot, tmp_path):
        """The user chains two rotations: the result of the first must
        not overwrite the display of the second (they arrive in an order
        that is not guaranteed, two distinct workers)."""
        cell = self._cell(qtbot, tmp_path, EditInfo(rotation=180))
        data = _jpeg_bytes()
        # The emitted path is that of the worker, hence that of PhotoInfo - normalised
        # by __post_init__ (Windows separators). Passing the literal with "/" would make
        # the `path == self._photo.path` guard fail and the test would pass for the
        # wrong reasons (nothing stored, nothing displayed).
        path = cell._photo.path

        cell._on_thumb_ready(path, data, edit_signature(EditInfo(rotation=90)))

        assert cell._pixmap is None
        # ...but the stale result stays cached for its fingerprint
        assert cell._cache.get_ram(path, edit_signature(EditInfo(rotation=90)))

    def test_ready_result_for_the_current_edit_is_displayed(self, qtbot, tmp_path):
        cell = self._cell(qtbot, tmp_path, EditInfo(rotation=90))

        cell._on_thumb_ready(cell._photo.path, _jpeg_bytes(),
                             edit_signature(EditInfo(rotation=90)))

        assert cell._pixmap is not None

    def test_reload_with_edit_updates_the_cell_state(self, qtbot, tmp_path):
        cell = self._cell(qtbot, tmp_path, None)
        edit = EditInfo(rotation=90)

        cell.reload_with_edit(edit)

        assert cell._edit is edit
