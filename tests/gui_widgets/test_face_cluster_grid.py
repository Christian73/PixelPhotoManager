# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) for FaceClusterGrid -- construction in batches from
synthetic data (_on_data_ready called directly, the event loop pumped through
waitUntil), selection/action bar, section ejection, pagination, removal and
restoration from the cache. One real plumbing test (refresh() with
_ClusterRefreshThread) closes the loop. Dialogs never exec()ed."""
import sqlite3

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QPushButton

from src.core.models import FaceInfo, PersonInfo
from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
import src.ui.face_cluster_grid as fcg
from src.ui.face_cluster_grid import FaceClusterGrid, _ProgressPopup
from src.ui.people_panel import _AssignDialog, _face_bytes


def _make_photo(tmp_path, name="p.jpg") -> str:
    path = tmp_path / name
    if not path.exists():
        Image.new("RGB", (160, 120), color=(60, 70, 80)).save(path)
    return str(path)


def _raw_insert_face(db, photo_path, cluster_id, embedding=None) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            " cluster_id, embedding) VALUES (?, 10, 10, 60, 60, ?, ?)",
            (photo_path, cluster_id,
             _enc(embedding) if embedding is not None else None),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _data(face_counts, groups, labels=None, suggestions=None, reps=None,
          persons=None, p_embs=None):
    return {
        "face_counts": dict(face_counts),
        "groups_sorted": [list(g) for g in groups],
        "group_labels": dict(labels) if labels else {g[0]: ("", "") for g in groups},
        "suggestions": dict(suggestions) if suggestions else {},
        "representative_faces": dict(reps) if reps else {},
        "persons": list(persons) if persons else [],
        "person_cluster_embeddings": dict(p_embs) if p_embs else {},
        "is_partial": False,
    }


@pytest.fixture
def grid(qtbot, tmp_path):
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    g = FaceClusterGrid(face_db, catalog)
    qtbot.addWidget(g)
    return g


def _build(qtbot, g, data):
    g._on_data_ready(data)
    qtbot.waitUntil(
        lambda: g._rendered_count >= len(g._all_combined), timeout=3000
    )
    qtbot.wait(20)   # let the reflow singleShots through


# ---------------------------------------------------------------------------
# progress popup

class TestProgressPopup:
    def test_determinate_and_indeterminate(self, qtbot):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        popup = _ProgressPopup(parent)
        qtbot.addWidget(popup)

        popup.update_progress(3, 6, "Analyse…")
        assert popup._lbl_phase.text() == "Analyse…"
        assert popup._bar.value() == 50
        assert popup._lbl_pct.text() == "50 %"

        popup.update_progress(0, 0, "Attente…")
        assert popup._bar.minimum() == 0 and popup._bar.maximum() == 0
        assert popup._lbl_pct.text() == ""

        popup.center_on_parent()   # must not raise

    def test_cancel_button_emits_signal(self, qtbot):
        from PySide6.QtWidgets import QWidget
        parent = QWidget()
        qtbot.addWidget(parent)
        popup = _ProgressPopup(parent)
        qtbot.addWidget(popup)
        received = []
        popup.cancel_requested.connect(lambda: received.append(True))

        btn = next(b for b in popup.findChildren(QPushButton) if b.text() == "Cancel")
        btn.click()

        assert received == [True]

    def test_popup_is_draggable(self, qtbot):
        """Frameless window: dragging must move it (it used to be fixed)."""
        from PySide6.QtCore import QPoint, QPointF, QEvent
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QWidget

        parent = QWidget()
        qtbot.addWidget(parent)
        popup = _ProgressPopup(parent)
        qtbot.addWidget(popup)
        popup.move(100, 100)
        start = popup.pos()

        def _evt(kind, global_pt, button, buttons):
            local = QPointF(popup.mapFromGlobal(global_pt.toPoint()))
            return QMouseEvent(kind, local, global_pt, button, buttons,
                               Qt.KeyboardModifier.NoModifier)

        grab = QPointF(popup.frameGeometry().topLeft() + QPoint(40, 12))
        popup.mousePressEvent(_evt(QEvent.Type.MouseButtonPress, grab,
                                   Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        popup.mouseMoveEvent(_evt(QEvent.Type.MouseMove, grab + QPointF(60, 35),
                                  Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
        popup.mouseReleaseEvent(_evt(QEvent.Type.MouseButtonRelease, grab + QPointF(60, 35),
                                     Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))

        assert popup.pos() == start + QPoint(60, 35)
        assert popup._drag_offset is None

        # After release, a movement alone no longer moves the window.
        moved = popup.pos()
        popup.mouseMoveEvent(_evt(QEvent.Type.MouseMove, grab + QPointF(200, 200),
                                  Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
        assert popup.pos() == moved


class TestProgressCancel:
    """Cancel button of the popup -> FaceClusterGrid._on_progress_cancelled()."""

    def test_requests_interruption_and_resets_ui(self, qtbot, grid):
        class _FakeThread:
            def __init__(self):
                self.interrupted = False

            def isRunning(self):
                return True

            def cancel(self):
                self.interrupted = True

        fake_thread = _FakeThread()
        grid._refresh_thread = fake_thread
        grid._progress_popup = _ProgressPopup(grid)
        qtbot.addWidget(grid._progress_popup)
        grid._progress_widget.setVisible(True)

        grid._on_progress_cancelled()

        assert fake_thread.interrupted
        assert grid._progress_popup is None
        assert not grid._progress_widget.isVisible()
        assert grid._lbl_title.text() == "Analysis cancelled"


# ---------------------------------------------------------------------------
# construction from the data

class TestBuildFromData:
    def test_data_ready_none_shows_error(self, qtbot, grid):
        grid._on_data_ready(None)
        assert grid._lbl_title.text() == "Error while loading"

    def test_empty_data_shows_done_message(self, qtbot, grid):
        _build(qtbot, grid, _data({}, []))
        assert grid._lbl_title.text() == "No group to identify"

    def test_flat_groups_and_solos(self, qtbot, grid):
        data = _data(
            face_counts={1: 3, 2: 2, 9: 1},
            groups=[[1], [2], [9]],
        )
        _build(qtbot, grid, data)

        assert sorted(grid._cards.keys()) == [1, 2, 9]
        assert grid._lbl_title.text() == "2 group(s), 1 isolated face(s)"
        assert grid._cards[9]._is_solo
        assert grid._flat_section in grid._sections
        assert grid._solo_section in grid._sections
        assert grid._cached_data is data

    def test_multi_cluster_group_creates_section(self, qtbot, grid):
        data = _data(
            face_counts={1: 4, 2: 2},
            groups=[[1, 2]],
            labels={1: ("≈ Probably the same person  —  2 groups", "#7aabdb")},
        )
        _build(qtbot, grid, data)

        # 1 dedicated section (neither flat nor solo, which stay empty)
        dedicated = [s for s in grid._sections
                     if s is not grid._flat_section and s is not grid._solo_section]
        assert len(dedicated) == 1
        assert [c for c, _ in dedicated[0]._entries] == [1, 2]

    def test_singletons_with_same_suggestion_are_grouped(self, qtbot, grid):
        alice = PersonInfo(name="Alice", id=7)
        data = _data(
            face_counts={1: 2, 2: 3},
            groups=[[1], [2]],
            suggestions={
                1: (7, "≈ Alice (80 %)", "#7aabdb", 0.80),
                2: (7, "≈ Alice (75 %)", "#7aabdb", 0.75),
            },
            persons=[alice],
        )
        _build(qtbot, grid, data)

        dedicated = [s for s in grid._sections
                     if s is not grid._flat_section and s is not grid._solo_section]
        assert len(dedicated) == 1
        assert sorted(c for c, _ in dedicated[0]._entries) == [1, 2]
        # Label recomputed with the name and the best score
        label, color = data["group_labels"][2]   # root = cluster sorted by size (2 first)
        assert "Probably Alice (80 %)" in label
        assert color == "#7aabdb"

    def test_promoted_suggestions_emit_persons_updated(self, qtbot, grid):
        data = _data({1: 2}, [[1]])
        data["n_promoted"] = 2

        with qtbot.waitSignal(grid.persons_updated, timeout=1000):
            grid._on_data_ready(data)
        qtbot.waitUntil(
            lambda: grid._rendered_count >= len(grid._all_combined), timeout=3000
        )


# ---------------------------------------------------------------------------
# selection and action bar

class TestSelection:
    def _grid_with_cards(self, qtbot, grid):
        _build(qtbot, grid, _data(
            face_counts={1: 3, 2: 2, 9: 1},
            groups=[[1], [2], [9]],
        ))
        return grid

    def test_selection_shows_action_bar(self, qtbot, grid):
        g = self._grid_with_cards(qtbot, grid)

        g._on_card_selection_toggled(1, True)

        assert g._action_bar.isVisibleTo(g)
        assert g._lbl_selected.text() == "1 group(s) selected"
        assert g._anchor_id == 1

        g._on_card_selection_toggled(2, True)
        assert g._lbl_selected.text() == "2 group(s) selected"

    def test_solo_only_selection_label(self, qtbot, grid):
        g = self._grid_with_cards(qtbot, grid)
        g._cards[9]._is_selected = True
        g._cards[9].set_selected(True)

        g._on_card_selection_toggled(9, True)

        assert "isolated face(s) selected" in g._lbl_selected.text()

    def test_mixed_selection_label(self, qtbot, grid):
        g = self._grid_with_cards(qtbot, grid)
        g._on_card_selection_toggled(1, True)
        g._on_card_selection_toggled(9, True)

        assert "item(s) selected" in g._lbl_selected.text()

    def test_clear_selection_hides_bar(self, qtbot, grid):
        g = self._grid_with_cards(qtbot, grid)
        g._on_card_selection_toggled(1, True)

        g._clear_selection()

        assert g._selected_ids == set()
        assert not g._action_bar.isVisibleTo(g)
        assert g._anchor_id is None

    def test_range_select_between_anchor_and_target(self, qtbot, grid):
        g = self._grid_with_cards(qtbot, grid)
        # Anchor on card 1 (visual order: 1, 2 in flat, then 9 in solo)
        g._cards[1]._is_selected = True
        g._cards[1].set_selected(True)
        g._on_card_selection_toggled(1, True)

        g._on_range_select(9)

        assert g._selected_ids == {1, 2, 9}

    def test_range_select_without_anchor_toggles_single(self, qtbot, grid):
        g = self._grid_with_cards(qtbot, grid)
        assert g._anchor_id is None

        g._on_range_select(2)

        assert g._selected_ids == {2}


# ---------------------------------------------------------------------------
# card / section actions

class TestCardActions:
    def test_ignore_card_updates_db_and_emits(self, qtbot, grid, tmp_path):
        _raw_insert_face(grid._face_db, "C:/p.jpg", cluster_id=1)
        _build(qtbot, grid, _data({1: 1}, [[1]]))

        with qtbot.waitSignal(grid.cluster_ignored, timeout=1000) as blocker:
            grid._on_card_ignore_requested(1)

        assert blocker.args == [1]
        assert grid._face_db.get_unnamed_clusters() == []

    def test_ignore_selection_ignores_all_selected(self, qtbot, grid):
        for cid in (1, 2):
            _raw_insert_face(grid._face_db, f"C:/p{cid}.jpg", cluster_id=cid)
        _build(qtbot, grid, _data({1: 2, 2: 2}, [[1], [2]]))
        grid._on_card_selection_toggled(1, True)
        grid._on_card_selection_toggled(2, True)

        with qtbot.waitSignal(grid.clusters_ignored, timeout=1000) as blocker:
            grid._on_card_ignore_selection_requested()

        assert sorted(blocker.args[0]) == [1, 2]
        assert grid._face_db.get_unnamed_clusters() == []
        assert grid._selected_ids == set()

    def test_quick_accept_emits_clusters_assigned(self, qtbot, grid):
        _build(qtbot, grid, _data({1: 2}, [[1]]))

        with qtbot.waitSignal(grid.clusters_assigned, timeout=1000) as blocker:
            grid._on_card_quick_accept(1, 42)

        assert blocker.args == [[1], 42]

    def test_view_requested_solo_and_group(self, qtbot, grid, en_catalogue):
        _raw_insert_face(grid._face_db, "C:/p.jpg", cluster_id=2)
        _raw_insert_face(grid._face_db, "C:/p2.jpg", cluster_id=2)
        _build(qtbot, grid, _data({2: 2, 9: 1}, [[2], [9]]))

        with qtbot.waitSignal(grid.photos_requested, timeout=1000) as blocker:
            grid._on_card_view_requested(9)
        assert blocker.args == [9, "Isolated face"]

        with qtbot.waitSignal(grid.photos_requested, timeout=1000) as blocker:
            grid._on_card_view_requested(2)
        assert blocker.args == [2, "Group 2 — 2 faces"]

    def _patch_dialog(self, monkeypatch, *, ignored=False, new_person=False,
                      new_name="", person_id=None):
        monkeypatch.setattr(_AssignDialog, "exec", lambda self: QDialog.Accepted)
        monkeypatch.setattr(_AssignDialog, "is_ignored", lambda self: ignored)
        monkeypatch.setattr(_AssignDialog, "is_new_person", lambda self: new_person)
        monkeypatch.setattr(_AssignDialog, "new_name", lambda self: new_name)
        monkeypatch.setattr(_AssignDialog, "existing_person_id", lambda self: person_id)

    def test_show_assign_dialog_new_person(self, qtbot, grid, monkeypatch):
        self._patch_dialog(monkeypatch, new_person=True, new_name="Zoé")

        with qtbot.waitSignal(grid.cluster_named, timeout=1000) as blocker:
            grid._show_assign_dialog(5, None, [])

        assert blocker.args == [5, "Zoé"]

    def test_show_assign_dialog_existing_person(self, qtbot, grid, monkeypatch):
        self._patch_dialog(monkeypatch, person_id=42)

        with qtbot.waitSignal(grid.cluster_assigned, timeout=1000) as blocker:
            grid._show_assign_dialog(5, None, [])

        assert blocker.args == [5, 42]

    def test_show_assign_dialog_ignore(self, qtbot, grid, monkeypatch):
        _raw_insert_face(grid._face_db, "C:/p.jpg", cluster_id=5)
        self._patch_dialog(monkeypatch, ignored=True)

        with qtbot.waitSignal(grid.cluster_ignored, timeout=1000) as blocker:
            grid._show_assign_dialog(5, None, [])

        assert blocker.args == [5]
        assert grid._face_db.get_unnamed_clusters() == []

    def test_show_multi_assign_dialog(self, qtbot, grid, monkeypatch):
        self._patch_dialog(monkeypatch, person_id=42)

        with qtbot.waitSignal(grid.clusters_assigned, timeout=1000) as blocker:
            grid._show_multi_assign_dialog([3, 4], [], None)

        assert blocker.args == [[3, 4], 42]

    def test_section_accept_and_ignore(self, qtbot, grid):
        for cid in (1, 2):
            _raw_insert_face(grid._face_db, f"C:/p{cid}.jpg", cluster_id=cid)
        _build(qtbot, grid, _data({1: 2, 2: 2}, [[1], [2]]))

        with qtbot.waitSignal(grid.clusters_assigned, timeout=1000) as blocker:
            grid._on_section_accept([1, 2], 42)
        assert blocker.args == [[1, 2], 42]

        grid._on_section_ignore([1, 2])
        assert grid._face_db.get_unnamed_clusters() == []
        assert grid._cards == {}


# ---------------------------------------------------------------------------
# section ejection

class TestEjectFromSection:
    def test_eject_moves_card_to_flat_section(self, qtbot, grid):
        alice = PersonInfo(name="Alice", id=7)
        _build(qtbot, grid, _data(
            face_counts={1: 2, 2: 3},
            groups=[[1], [2]],
            suggestions={
                1: (7, "≈ Alice (80 %)", "#7aabdb", 0.80),
                2: (7, "≈ Alice (75 %)", "#7aabdb", 0.75),
            },
            persons=[alice],
        ))
        dedicated = [s for s in grid._sections
                     if s is not grid._flat_section and s is not grid._solo_section]
        assert len(dedicated) == 1

        grid._on_card_eject_from_section(1)

        # Card 1 now lives in the flat section
        assert any(c == 1 for c, _ in grid._flat_section._entries)
        remaining = [s for s in grid._sections
                     if s is not grid._flat_section and s is not grid._solo_section]
        assert all(1 not in [c for c, _ in s._entries] for s in remaining)
        assert grid._cached_data["suggestions"].get(1) is None
        assert [1] in grid._cached_data["groups_sorted"]


# ---------------------------------------------------------------------------
# remove / restore

class TestRemoveRestore:
    def test_remove_clusters_updates_cards_and_cache(self, qtbot, grid):
        _build(qtbot, grid, _data({1: 3, 2: 2, 9: 1}, [[1], [2], [9]]))
        grid._on_card_selection_toggled(1, True)

        grid.remove_clusters([1, 9])

        assert sorted(grid._cards.keys()) == [2]
        assert 1 not in grid._cached_data["face_counts"]
        assert [g for g in grid._cached_data["groups_sorted"]] == [[2]]
        assert grid._selected_ids == set()

    def test_restore_rebuilds_from_cache(self, qtbot, grid):
        _build(qtbot, grid, _data({1: 3, 2: 2}, [[1], [2]]))
        assert grid._cached_data is not None

        grid.restore()
        qtbot.waitUntil(
            lambda: grid._rendered_count >= len(grid._all_combined), timeout=3000
        )
        # Let the scroll restoration singleShot(30 ms) through while the grid is
        # still alive (otherwise RuntimeError in the next test)
        qtbot.wait(60)

        assert sorted(grid._cards.keys()) == [1, 2]

    def test_action_ignore_removes_selected(self, qtbot, grid):
        for cid in (1, 2):
            _raw_insert_face(grid._face_db, f"C:/p{cid}.jpg", cluster_id=cid)
        _build(qtbot, grid, _data({1: 2, 2: 2}, [[1], [2]]))
        grid._on_card_selection_toggled(1, True)

        grid._on_action_ignore()

        assert sorted(grid._cards.keys()) == [2]
        assert grid._face_db.get_unnamed_clusters() != []   # cluster 2 remains


# ---------------------------------------------------------------------------
# merge / associate -- regression: no reload of the analysis


class TestApplyMerge:
    """Associating groups must not restart the group analysis.

    Until now every merge went through refresh(), i.e. the Union-Find and the
    suggestions over the whole library behind a modal popup, just to redisplay
    what the user had done by hand.
    """

    def test_merging_groups_only_updates_the_counter(self, qtbot, grid, monkeypatch):
        _build(qtbot, grid, _data({1: 3, 2: 2}, [[1], [2]]))
        reloads = []
        monkeypatch.setattr(grid, "refresh", lambda: reloads.append(True))

        grid.apply_merge([2], 1)

        assert reloads == []
        assert sorted(grid._cards.keys()) == [1]
        assert grid._cached_data["face_counts"][1] == 5
        assert grid._cards[1]._lbl_count.text().startswith("5")

    def test_merging_isolated_faces_promotes_the_card(self, qtbot, grid, monkeypatch):
        """A solo card displays no counter: it has to be rebuilt as a group card."""
        _build(qtbot, grid, _data({1: 2, 8: 1, 9: 1}, [[1], [8], [9]]))
        assert grid._cards[8]._is_solo
        monkeypatch.setattr(grid, "refresh", lambda: pytest.fail("reloaded"))

        grid.apply_merge([9], 8)

        card = grid._cards[8]
        assert not card._is_solo
        assert card._lbl_count.text().startswith("2")
        assert any(c == 8 for c, _ in grid._flat_section._entries)
        assert 9 not in grid._cards
        assert ("group", [8]) in grid._all_combined

    def test_associate_merges_in_db_and_keeps_the_grid(self, qtbot, grid, monkeypatch):
        for cid, n in ((1, 2), (2, 1)):
            for i in range(n):
                _raw_insert_face(grid._face_db, f"C:/p{cid}_{i}.jpg", cluster_id=cid)
        _build(qtbot, grid, _data({1: 2, 2: 1}, [[1], [2]]))
        monkeypatch.setattr(grid, "refresh", lambda: pytest.fail("reloaded"))
        merged = []
        grid.cluster_merged.connect(lambda s, t: merged.append((s, t)))
        grid._on_card_selection_toggled(1, True)
        grid._on_card_selection_toggled(2, True)

        grid._on_card_associate_requested()

        # the biggest group absorbs the other, in the database and on screen
        assert dict(grid._face_db.get_unnamed_clusters()) == {1: 3}
        assert sorted(grid._cards.keys()) == [1]
        assert grid._cards[1]._lbl_count.text().startswith("3")
        assert merged and merged[0][1] == 1
        assert grid._selected_ids == set()

    def test_without_usable_cache_it_falls_back_on_refresh(self, qtbot, grid, monkeypatch):
        reloads = []
        monkeypatch.setattr(grid, "refresh", lambda: reloads.append(True))
        grid._cached_data = None

        grid.apply_merge([2], 1)

        assert reloads == [True]


# ---------------------------------------------------------------------------
# pagination

class TestPagination:
    def test_load_more_renders_next_page(self, qtbot, grid, monkeypatch):
        monkeypatch.setattr(fcg, "_PAGE_SIZE", 3)
        monkeypatch.setattr(fcg, "_BUILD_BATCH", 2)
        counts = {cid: 2 for cid in range(1, 6)}   # 5 multi-face groups
        # Not the _build helper: with pagination, the rendering stops at _PAGE_SIZE
        grid._on_data_ready(_data(counts, [[cid] for cid in range(1, 6)]))
        qtbot.waitUntil(lambda: grid._rendered_count >= 3, timeout=3000)
        qtbot.wait(20)

        assert grid._rendered_count == 3
        assert grid._load_more_btn is not None
        assert "(2 left)" in grid._load_more_btn.text()

        grid._load_more_btn.click()
        qtbot.waitUntil(lambda: grid._rendered_count >= 5, timeout=3000)
        qtbot.waitUntil(lambda: grid._load_more_btn is None, timeout=3000)

        assert sorted(grid._cards.keys()) == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# avatars

class TestAvatars:
    def test_avatar_ready_caches_and_applies(self, qtbot, grid, tmp_path):
        photo = _make_photo(tmp_path)
        _build(qtbot, grid, _data({1: 2}, [[1]]))
        face = FaceInfo(id=1, photo_path=photo,
                        bbox_x=10, bbox_y=10, bbox_w=60, bbox_h=60)
        data = _face_bytes(face, 130)

        grid._on_avatar_ready(1, data)

        assert grid._avatar_cache[1] == data

    def test_start_cluster_loader_real_thread(self, qtbot, grid, tmp_path):
        photo = _make_photo(tmp_path)
        _build(qtbot, grid, _data({1: 2}, [[1]]))
        face = FaceInfo(id=1, photo_path=photo,
                        bbox_x=10, bbox_y=10, bbox_w=60, bbox_h=60)

        grid._start_cluster_loader([(1, face)])
        qtbot.waitUntil(
            lambda: 1 in grid._avatar_cache and not grid._loader.isRunning(),
            timeout=3000,
        )

        assert 1 in grid._avatar_cache


# ---------------------------------------------------------------------------
# real plumbing (a genuine end-to-end refresh())

class TestRealRefresh:
    def test_refresh_builds_cards_from_db(self, qtbot, grid, tmp_path):
        photo = _make_photo(tmp_path)
        for cid in (1, 2):
            for k in range(2):
                _raw_insert_face(grid._face_db, photo, cluster_id=cid,
                                 embedding=[1.0, 0.0] if cid == 1 else [0.0, 1.0])

        grid.refresh()
        with qtbot.waitSignal(grid._refresh_thread.data_ready, timeout=5000):
            pass
        qtbot.waitUntil(
            lambda: grid._rendered_count >= len(grid._all_combined)
            and len(grid._cards) == 2,
            timeout=5000,
        )
        assert grid._progress_popup is None
        # The avatar loader starts through singleShot(0) AFTER the rendering: wait
        # until no child QThread is still running before the teardown (otherwise the
        # parent is destroyed with a live thread -> fail-fast 0xC0000409).
        from PySide6.QtCore import QThread as _QThread

        def _no_running_child_thread():
            try:
                return all(not t.isRunning() for t in grid.findChildren(_QThread))
            except RuntimeError:
                return True

        qtbot.waitUntil(_no_running_child_thread, timeout=5000)
        qtbot.wait(30)   # let the possible singleShot(0) start, then re-check
        qtbot.waitUntil(_no_running_child_thread, timeout=5000)

        assert sorted(grid._cards.keys()) == [1, 2]
        assert grid._cached_data is not None
