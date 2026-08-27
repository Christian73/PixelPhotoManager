# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) for FacePanel -- synchronous loaders, ignored faces
dialog, undo stack, ignore/unassign, complete assignment flows with a
monkeypatched _AssignDialog (never a real popup). Complements
test_face_panel_suggestions.py (display of the suggestions)."""
import sqlite3

import pytest
from PIL import Image
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QDialog, QMenu

from src.core.models import FaceInfo
from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
from src.ui import face_panel as face_panel_mod
from src.ui.face_panel import (
    FacePanel, _AssignPrepLoader, _FacePanelLoader, _FacesDataLoader,
    _IgnoredFacesDialog,
)
from src.ui.people_panel import _AssignDialog
from tests.gui_widgets.thread_wait import wait_thread_done


def _make_photo(tmp_path, name="p.jpg") -> str:
    path = tmp_path / name
    if not path.exists():
        Image.new("RGB", (200, 160), color=(70, 80, 90)).save(path)
    return str(path)


def _raw_insert_face(
    db, photo_path, cluster_id=None, person_id=None,
    suggestion_person_id=None, suggestion_score=None, ignored=0,
    bbox=(10, 10, 60, 60), embedding=None,
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, cluster_id,"
            "  person_id, suggestion_person_id, suggestion_score, ignored, embedding)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                photo_path, *bbox, cluster_id, person_id,
                suggestion_person_id, suggestion_score, ignored,
                _enc(embedding) if embedding is not None else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path):
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    return face_db, catalog


def _make_panel(qtbot, env):
    face_db, catalog = env
    panel = FacePanel(face_db, catalog)
    qtbot.addWidget(panel)
    return panel


def _wait_refresh(qtbot, panel):
    """Waits for the full reload of the panel (data + thumbnails) -- without
    which a _FacePanelLoader still alive at teardown triggers the Qt fail-fast
    0xC0000409 (cf. the QThread trap in CLAUDE.md).

    Polling (cf. wait_thread_done) rather than waitSignal: both threads are
    already started by the time we get here, and a lost emission would make the
    blocker time out."""
    wait_thread_done(qtbot, panel._data_loader)
    wait_thread_done(qtbot, panel._loader)


def _load(qtbot, panel, photo_path):
    panel.set_photo(photo_path)
    _wait_refresh(qtbot, panel)


def _ignored_flag(face_db, fid) -> bool:
    # get_face_by_id does not return the ignored column -- direct SQL read
    conn = sqlite3.connect(face_db._db_path)
    try:
        return bool(conn.execute(
            "SELECT ignored FROM faces WHERE id=?", (fid,)
        ).fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# synchronous loaders

class TestFacePanelLoader:
    def _face(self, photo, fid=1):
        return FaceInfo(id=fid, photo_path=photo,
                        bbox_x=10, bbox_y=10, bbox_w=60, bbox_h=60)

    def test_run_emits_png_per_face(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path)
        loader = _FacePanelLoader([(1, self._face(photo, 1)),
                                   (2, self._face(photo, 2))])
        results = []
        loader.ready.connect(lambda fid, data: results.append((fid, data[:4])))

        loader.run()

        assert [fid for fid, _ in results] == [1, 2]
        assert all(head == b"\x89PNG" for _, head in results)

    def test_stop_flag_interrupts(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path)
        loader = _FacePanelLoader([(i, self._face(photo, i)) for i in range(4)])
        results = []

        def _first(fid, data):
            results.append(fid)
            loader.stop()   # direct connection: the loop stops after the first one

        loader.ready.connect(_first)
        loader.run()

        assert results == [0]

    def test_video_photo_emits_nothing(self, qtbot):
        loader = _FacePanelLoader([(1, self._face("C:/clip.mp4"))])
        results = []
        loader.ready.connect(lambda *a: results.append(a))

        loader.run()

        assert results == []


class TestFacesDataLoader:
    def test_run_reports_faces_names_and_ignored_count(self, qtbot, env, tmp_path):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        alice = catalog.create_person("Alice")
        fid = _raw_insert_face(face_db, photo, cluster_id=1, person_id=alice.id)
        _raw_insert_face(face_db, photo, ignored=1)   # ignored face
        loader = _FacesDataLoader(face_db, catalog, photo)
        results = []
        loader.data_ready.connect(lambda *a: results.append(a))

        loader.run()

        path, faces, names, cluster_persons, probable, ignored_count, edit_rotation = results[0]
        assert [f.id for f in faces] == [fid]
        assert dict(names) == {alice.id: "Alice"}
        assert ignored_count == 1
        assert edit_rotation == 0

    def test_run_error_emits_empty(self, qtbot):
        loader = _FacesDataLoader(None, None, "C:/x.jpg")
        results = []
        loader.data_ready.connect(lambda *a: results.append(a))

        loader.run()

        assert results[0][1:] == ([], [], [], [], 0, 0)


class TestThumbnailCacheReuse:
    """A refresh of the panel on the same photo (after an identification, an
    ignore, etc.) must only re-decode the thumbnails whose geometry really
    changed -- not every face of the photo."""

    def test_second_load_same_photo_reuses_cached_thumbnails(self, qtbot, env, tmp_path):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        fid1 = _raw_insert_face(face_db, photo, bbox=(10, 10, 60, 60))
        fid2 = _raw_insert_face(face_db, photo, bbox=(90, 10, 60, 60))
        panel = _make_panel(qtbot, env)

        _load(qtbot, panel, photo)
        assert set(panel._thumb_cache) == {fid1, fid2}

        # Simulates the refresh triggered after an identification: same photo, no
        # bbox has changed -> nothing to re-decode, no loader created.
        panel.set_photo(photo)
        _wait_refresh(qtbot, panel)

        assert panel._loader is None

    def test_changed_bbox_forces_redecode_of_that_face_only(self, qtbot, env, tmp_path):
        """Simulates a re-indexing (rotation) that shifts the bbox of a single
        face under the same face_id: only its thumbnail goes through the loader
        again."""
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        fid1 = _raw_insert_face(face_db, photo, bbox=(10, 10, 60, 60))
        fid2 = _raw_insert_face(face_db, photo, bbox=(90, 10, 60, 60))
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        conn = sqlite3.connect(face_db._db_path)
        conn.execute("UPDATE faces SET bbox_x=200 WHERE id=?", (fid1,))
        conn.commit()
        conn.close()

        panel.set_photo(photo)
        wait_thread_done(qtbot, panel._data_loader)

        assert panel._loader is not None
        assert [fid for fid, _ in panel._loader._items] == [fid1]

        # Let the real loader finish before the end of the test (cf. the
        # disposable-QThread trap in CLAUDE.md -- a thread still alive when the
        # widget is destroyed triggers a Qt fail-fast).
        wait_thread_done(qtbot, panel._loader)


class TestAssignPrepLoader:
    def test_run_returns_persons_and_suggestion(self, qtbot, env, tmp_path):
        import math
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        alice = catalog.create_person("Alice")
        _raw_insert_face(face_db, photo, cluster_id=99, person_id=alice.id,
                         embedding=[1.0, 0.0])
        emb = [math.cos(math.acos(0.6)), math.sin(math.acos(0.6))]
        fid = _raw_insert_face(face_db, photo, cluster_id=1, embedding=emb)
        face = face_db.get_face_by_id(fid)
        loader = _AssignPrepLoader(catalog, face_db, face)
        results = []
        loader.ready.connect(lambda persons, sugg: results.append((persons, sugg)))

        loader.run()

        persons, suggested = results[0]
        assert [p.name for p in persons] == ["Alice"]
        assert suggested == alice.id

    def test_run_error_emits_empty(self, qtbot):
        loader = _AssignPrepLoader(None, None, None)
        results = []
        loader.ready.connect(lambda persons, sugg: results.append((persons, sugg)))

        loader.run()

        assert results == [([], None)]


# ---------------------------------------------------------------------------
# ignored faces dialog

class TestIgnoredFacesDialog:
    def test_rows_and_restore(self, qtbot, env, tmp_path, monkeypatch):
        face_db, _ = env
        photo = _make_photo(tmp_path)
        fid = _raw_insert_face(face_db, photo, ignored=1)
        # Thumbnails loaded synchronously: no live thread at teardown
        monkeypatch.setattr(_FacePanelLoader, "start", _FacePanelLoader.run)
        faces = face_db.get_ignored_faces_for_photo(photo)
        dlg = _IgnoredFacesDialog(faces, photo)
        qtbot.addWidget(dlg)

        assert dlg.restored_ids() == []
        assert fid in dlg._rows

        from PySide6.QtWidgets import QPushButton
        restore_btn = next(
            b for b in dlg.findChildren(QPushButton) if b.text() == "Restore"
        )
        restore_btn.click()

        assert dlg.restored_ids() == [fid]
        assert not restore_btn.isEnabled()
        assert restore_btn.text() == "Restored"


# ---------------------------------------------------------------------------
# FacePanel -- interactions

class TestRefreshWithoutBlanking:
    """A refresh of the same photo must not empty the panel while it loads.

    Confirming a suggestion ends on set_photo(current): clearing the items right
    away left the panel blank for the whole duration of _FacesDataLoader -- every
    face vanishing then coming back, with nothing on screen to explain it.
    """

    def test_the_faces_stay_on_screen_during_a_refresh(self, qtbot, env, tmp_path):
        face_db, _ = env
        photo = _make_photo(tmp_path)
        f1 = _raw_insert_face(face_db, photo, cluster_id=1)
        f2 = _raw_insert_face(face_db, photo, cluster_id=2)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)
        assert set(panel._items) == {f1, f2}

        panel.set_photo(photo)          # same photo: the loader has not answered yet
        assert set(panel._items) == {f1, f2}, "le panneau a été vidé pendant le chargement"

        _wait_refresh(qtbot, panel)
        assert set(panel._items) == {f1, f2}

    def test_navigating_to_another_photo_clears_at_once(self, qtbot, env, tmp_path):
        """The counterpart: those faces belong to ANOTHER photo, keeping them on
        screen would be showing something false."""
        face_db, _ = env
        photo = _make_photo(tmp_path)
        other = _make_photo(tmp_path, "other.jpg")
        _raw_insert_face(face_db, photo, cluster_id=1)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)
        assert panel._items

        panel.set_photo(other)
        assert panel._items == {}

        _wait_refresh(qtbot, panel)


class TestQuickIgnoreCross:
    """The quick-ignore cross is reserved for what is not settled yet.

    On a confirmed identification it brings nothing but the risk of a misclick
    on a face the user has just validated -- ignoring it stays reachable through
    the context menu.
    """

    def test_absent_on_a_confirmed_identification(self, qtbot, env, tmp_path):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        alice = catalog.create_person("Alice")
        fid = _raw_insert_face(face_db, photo, cluster_id=1, person_id=alice.id)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        assert not hasattr(panel._items[fid], "_btn_ignore")

    def test_absent_when_the_group_carries_the_person(self, qtbot, env, tmp_path):
        """The face has no person_id of its own yet (re-indexed after an
        assignment), but its cluster is named: the identification is settled."""
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        other = _make_photo(tmp_path, "other.jpg")
        alice = catalog.create_person("Alice")
        _raw_insert_face(face_db, other, cluster_id=7, person_id=alice.id)
        fid = _raw_insert_face(face_db, photo, cluster_id=7)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        assert not hasattr(panel._items[fid], "_btn_ignore")

    def test_present_on_an_unidentified_face(self, qtbot, env, tmp_path):
        face_db, _ = env
        photo = _make_photo(tmp_path)
        fid = _raw_insert_face(face_db, photo, cluster_id=1)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        assert hasattr(panel._items[fid], "_btn_ignore")

    def test_present_on_a_suggestion_awaiting_verification(self, qtbot, env, tmp_path):
        """A suggestion is precisely what is not confirmed: the triage crosses stay."""
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        alice = catalog.create_person("Alice")
        fid = _raw_insert_face(
            face_db, photo, cluster_id=1,
            suggestion_person_id=alice.id, suggestion_score=0.61,
        )
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        assert hasattr(panel._items[fid], "_btn_ignore")

    def test_the_group_name_is_displayed_not_the_group_number(self, qtbot, env, tmp_path):
        """Same setup: the name of the person carried by the cluster must be the
        one displayed.

        _on_faces_data_ready assigned self._cluster_persons then called _clear(),
        which empties that dict IN PLACE -- so the local variable, the same object,
        came out empty too and every such face fell through to the "Group {id}"
        branch.
        """
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        other = _make_photo(tmp_path, "other.jpg")
        alice = catalog.create_person("Alice")
        _raw_insert_face(face_db, other, cluster_id=7, person_id=alice.id)
        fid = _raw_insert_face(face_db, photo, cluster_id=7)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        assert panel._items[fid]._name_label.text() == "Alice"
        assert panel._cluster_persons == {7: alice.id}

    def test_the_context_menu_still_offers_it(self, qtbot, env, tmp_path, monkeypatch):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        alice = catalog.create_person("Alice")
        fid = _raw_insert_face(face_db, photo, cluster_id=1, person_id=alice.id)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        labels: list[str] = []

        # A QMenu subclass substituted in the module namespace, not a setattr on
        # the Shiboken class: a direct setattr does not intercept the native call
        # and the real menu opens on the desktop, blocking the run
        # (cf. the captured_menus fixture of test_album_mode_no_delete.py).
        class _CapturingMenu(QMenu):
            def exec(self, *a, **k):
                labels.extend(act.text() for act in self.actions())
                return None

        monkeypatch.setattr(face_panel_mod, "QMenu", _CapturingMenu)
        panel.show_face_context_menu(face_db.get_face_by_id(fid), QPoint(0, 0))

        assert any("Ignore this face" in label for label in labels)


class TestFacePanelInteractions:
    def test_undo_stack_push_and_undo(self, qtbot, env):
        panel = _make_panel(qtbot, env)
        calls = []
        assert not panel.can_undo()

        with qtbot.waitSignal(panel.undo_stack_changed, timeout=1000) as blocker:
            panel._push_undo("action", lambda: calls.append(1))
        assert blocker.args == [True]
        assert panel.can_undo()

        panel.undo()
        assert calls == [1]
        assert not panel.can_undo()

    def test_ignore_face_hides_it_and_undo_restores(self, qtbot, env, tmp_path):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        fid = _raw_insert_face(face_db, photo, cluster_id=1)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)
        assert fid in panel._items

        panel._on_ignore_requested(fid)
        _wait_refresh(qtbot, panel)

        assert _ignored_flag(face_db, fid)
        assert panel.can_undo()

        panel.undo()
        _wait_refresh(qtbot, panel)
        assert not _ignored_flag(face_db, fid)

    def test_unassign_face(self, qtbot, env, tmp_path):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        alice = catalog.create_person("Alice")
        fid = _raw_insert_face(face_db, photo, cluster_id=1, person_id=alice.id)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        panel._on_unassign_requested(fid)
        _wait_refresh(qtbot, panel)

        assert face_db.get_face_by_id(fid).person_id is None

    def test_item_click_selection_toggle(self, qtbot, env, tmp_path):
        face_db, _ = env
        photo = _make_photo(tmp_path)
        f1 = _raw_insert_face(face_db, photo, cluster_id=1)
        f2 = _raw_insert_face(face_db, photo, cluster_id=1)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        with qtbot.waitSignal(panel.face_highlighted, timeout=1000) as blocker:
            panel._on_item_clicked(f1)
        assert blocker.args[0].id == f1

        # clicking the same one again: deselection (None)
        with qtbot.waitSignal(panel.face_highlighted, timeout=1000) as blocker:
            panel._on_item_clicked(f1)
        assert blocker.args == [None]

        # selection of another face
        panel._on_item_clicked(f1)
        with qtbot.waitSignal(panel.face_highlighted, timeout=1000) as blocker:
            panel._on_item_clicked(f2)
        assert blocker.args[0].id == f2

    def test_tous_toggle_emits_all_faces_then_empty(self, qtbot, env, tmp_path):
        face_db, _ = env
        photo = _make_photo(tmp_path)
        _raw_insert_face(face_db, photo, cluster_id=1)
        _raw_insert_face(face_db, photo, cluster_id=1)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        with qtbot.waitSignal(panel.all_faces_toggled, timeout=1000) as blocker:
            panel._btn_tous.setChecked(True)
        assert len(blocker.args[0]) == 2

        with qtbot.waitSignal(panel.all_faces_toggled, timeout=1000) as blocker:
            panel._btn_tous.setChecked(False)
        assert blocker.args == [[]]

    def test_double_click_named_face_requests_person_view(self, qtbot, env, tmp_path):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        alice = catalog.create_person("Alice")
        fid = _raw_insert_face(face_db, photo, cluster_id=1, person_id=alice.id)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)

        with qtbot.waitSignal(panel.person_cluster_requested, timeout=1000) as blocker:
            panel._on_item_double_clicked(fid)

        assert blocker.args == [alice.id]

    def _patch_dialog(self, monkeypatch, *, new_person=False, new_name="",
                      person_id=None):
        monkeypatch.setattr(_AssignDialog, "exec", lambda self: QDialog.Accepted)
        monkeypatch.setattr(_AssignDialog, "is_new_person", lambda self: new_person)
        monkeypatch.setattr(_AssignDialog, "new_name", lambda self: new_name)
        monkeypatch.setattr(_AssignDialog, "existing_person_id", lambda self: person_id)

    def test_continue_identify_face_assigns_in_background(
        self, qtbot, env, tmp_path, monkeypatch
    ):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        boris = catalog.create_person("Boris")
        fid = _raw_insert_face(face_db, photo, cluster_id=1)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)
        self._patch_dialog(monkeypatch, person_id=boris.id)

        with qtbot.waitSignal(panel.person_assigned, timeout=3000):
            panel._continue_identify_face(fid, catalog.get_persons(), None)
        _wait_refresh(qtbot, panel)

        face = face_db.get_face_by_id(fid)
        assert face.person_id == boris.id
        assert face.cluster_id < 0        # isolated from its group
        assert panel.can_undo()

    def test_continue_assign_requested_assigns_whole_cluster(
        self, qtbot, env, tmp_path, monkeypatch
    ):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        boris = catalog.create_person("Boris")
        f1 = _raw_insert_face(face_db, photo, cluster_id=4)
        f2 = _raw_insert_face(face_db, photo, cluster_id=4)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)
        self._patch_dialog(monkeypatch, person_id=boris.id)
        face = panel._faces[f1]

        with qtbot.waitSignal(panel.person_assigned, timeout=3000):
            panel._continue_assign_requested(f1, face, catalog.get_persons(), None)
        _wait_refresh(qtbot, panel)

        assert face_db.get_face_by_id(f1).person_id == boris.id
        assert face_db.get_face_by_id(f2).person_id == boris.id

    def test_continue_bbox_ready_creates_manual_face(
        self, qtbot, env, tmp_path, monkeypatch
    ):
        face_db, catalog = env
        photo = _make_photo(tmp_path)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)
        self._patch_dialog(monkeypatch, new_person=True, new_name="Zoé")

        with qtbot.waitSignal(panel.person_assigned, timeout=3000):
            panel._continue_bbox_ready((20, 20, 50, 50), [])
        _wait_refresh(qtbot, panel)

        persons = catalog.get_persons()
        assert [p.name for p in persons] == ["Zoé"]
        faces = face_db.get_faces_for_photo(photo)
        assert len(faces) == 1 and faces[0].person_id == persons[0].id

    def test_show_ignored_restores_faces(self, qtbot, env, tmp_path, monkeypatch):
        face_db, _ = env
        photo = _make_photo(tmp_path)
        fid = _raw_insert_face(face_db, photo, ignored=1)
        panel = _make_panel(qtbot, env)
        _load(qtbot, panel, photo)
        assert panel._btn_ignored.isEnabled()

        # Dialog thumbnails loaded synchronously (no live thread at teardown)
        monkeypatch.setattr(_FacePanelLoader, "start", _FacePanelLoader.run)

        def _fake_exec(dlg_self):
            dlg_self._restored.append(fid)
            return QDialog.Accepted

        monkeypatch.setattr(_IgnoredFacesDialog, "exec", _fake_exec)

        panel._on_show_ignored()
        wait_thread_done(qtbot, panel._data_loader)

        assert not _ignored_flag(face_db, fid)
        assert panel.can_undo()
