# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) pour PersonClusterView — sections confirmée / en attente,
sélection, acceptation/rejet de suggestions, réassignation. FaceDatabase et
Catalog réels semés en process ; loaders exécutés en run() synchrone ou
attendus via waitSignal (plomberie réelle), menus jamais exec()."""
import sqlite3

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from src.core.models import FaceInfo, PersonInfo
from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
from src.ui.people_panel import _AssignDialog, _face_bytes
from src.ui.person_cluster_view import (
    PersonClusterView, _FaceThumb, _FlatFaceLoader,
    _PersonsLoaderThread, _UnassignThread,
)


def _make_photo(tmp_path, name="p.jpg") -> str:
    path = tmp_path / name
    if not path.exists():
        Image.new("RGB", (160, 120), color=(80, 90, 100)).save(path)
    return str(path)


def _raw_insert_face(
    db, photo_path, cluster_id=None, person_id=None,
    suggestion_person_id=None, suggestion_score=None,
    bbox=(10, 10, 60, 60), embedding=None,
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, cluster_id,"
            "  person_id, suggestion_person_id, suggestion_score, embedding)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                photo_path, *bbox, cluster_id, person_id,
                suggestion_person_id, suggestion_score,
                _enc(embedding) if embedding is not None else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _face(photo_path, fid=1) -> FaceInfo:
    return FaceInfo(id=fid, photo_path=photo_path,
                    bbox_x=10, bbox_y=10, bbox_w=60, bbox_h=60)


@pytest.fixture
def env(tmp_path):
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    return face_db, catalog


def _wait_loaders(qtbot, view) -> None:
    """Attend la fin des _FlatFaceLoader réels démarrés par la vue (polling :
    waitSignal(finished) raterait une émission entre le check et le branchement)."""
    for attr in ("_flat_loader", "_pending_flat_loader"):
        loader = getattr(view, attr, None)
        if loader is not None:
            def _done(ld=loader):
                try:
                    return not ld.isRunning()
                except RuntimeError:
                    return True   # deleteLater déjà passé
            qtbot.waitUntil(_done, timeout=3000)


def _make_view(qtbot, env):
    face_db, catalog = env
    view = PersonClusterView(face_db, catalog)
    qtbot.addWidget(view)
    return view, face_db, catalog


# ---------------------------------------------------------------------------
# threads

class TestThreads:
    def test_persons_loader_success_and_error(self, qtbot, env):
        face_db, catalog = env
        catalog.create_person("Alice")
        t = _PersonsLoaderThread(catalog, face_db)
        results = []
        t.ready.connect(results.append)
        t.run()
        assert [p.name for p in results[0]] == ["Alice"]

        t_err = _PersonsLoaderThread(None, None)
        errors = []
        t_err.ready.connect(errors.append)
        t_err.run()
        assert errors == [[]]

    def test_unassign_thread_isolates_faces(self, qtbot, env):
        face_db, catalog = env
        alice = catalog.create_person("Alice")
        fid = _raw_insert_face(face_db, "C:/p.jpg", cluster_id=3,
                               person_id=alice.id, embedding=[1.0, 0.0])
        t = _UnassignThread(face_db, [fid], alice.id)
        done = []
        t.done.connect(lambda: done.append(1))

        t.run()

        assert done == [1]
        face = face_db.get_face_by_id(fid)
        assert face.person_id is None
        assert face.cluster_id < 0   # isolé dans un cluster négatif dédié

    def test_unassign_thread_error_still_emits_done(self, qtbot):
        t = _UnassignThread(None, [1], None)
        done = []
        t.done.connect(lambda: done.append(1))
        t.run()
        assert done == [1]

    def test_flat_face_loader_emits_bytes(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path)
        loader = _FlatFaceLoader([_face(photo, fid=7)], size=40)
        results = []
        loader.face_ready.connect(lambda fid, data: results.append(fid))

        loader.run()

        assert results == [7]


# ---------------------------------------------------------------------------
# vignette individuelle

class TestFaceThumb:
    def test_set_image_and_selection_styles(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path)
        thumb = _FaceThumb(_face(photo))
        qtbot.addWidget(thumb)

        thumb.set_image(_face_bytes(_face(photo), 40))
        assert thumb._lbl_img.pixmap() is not None

        thumb.set_selected(True)
        assert thumb.styleSheet() == thumb._STYLE_SELECTED
        thumb.set_selected(False)
        assert thumb.styleSheet() == thumb._STYLE_NORMAL

    def test_set_pending_shows_overlay_buttons(self, qtbot, tmp_path):
        thumb = _FaceThumb(_face(_make_photo(tmp_path)))
        qtbot.addWidget(thumb)
        thumb.show()
        qtbot.waitExposed(thumb)

        thumb.set_pending(True)
        assert thumb._btn_accept.isVisible()
        assert thumb._btn_reject.isVisible()
        assert thumb.styleSheet() == thumb._STYLE_PENDING

        thumb.set_pending(False)
        assert not thumb._btn_accept.isVisible()

    def test_click_and_double_click_signals(self, qtbot, tmp_path):
        photo = _make_photo(tmp_path)
        thumb = _FaceThumb(_face(photo, fid=9))
        qtbot.addWidget(thumb)
        thumb.show()
        qtbot.waitExposed(thumb)

        with qtbot.waitSignal(thumb.clicked, timeout=1000) as blocker:
            qtbot.mouseClick(thumb, Qt.LeftButton)
        assert blocker.args == [9, False, False]

        with qtbot.waitSignal(thumb.double_clicked, timeout=1000) as blocker:
            qtbot.mouseDClick(thumb, Qt.LeftButton)
        assert blocker.args == [photo]

    def test_overlay_buttons_emit_face_id(self, qtbot, tmp_path):
        thumb = _FaceThumb(_face(_make_photo(tmp_path), fid=4))
        qtbot.addWidget(thumb)
        thumb.set_pending(True)

        with qtbot.waitSignal(thumb.accept_clicked, timeout=1000) as blocker:
            thumb._btn_accept.click()
        assert blocker.args == [4]

        with qtbot.waitSignal(thumb.reject_clicked, timeout=1000) as blocker:
            thumb._btn_reject.click()
        assert blocker.args == [4]


# ---------------------------------------------------------------------------
# vue principale

class TestPersonClusterView:
    def _seed_confirmed(self, face_db, catalog, tmp_path, n=3):
        alice = catalog.create_person("Alice")
        photo = _make_photo(tmp_path)
        fids = [
            _raw_insert_face(face_db, photo, cluster_id=10,
                             person_id=alice.id, embedding=[1.0, 0.0])
            for _ in range(n)
        ]
        return alice, fids

    def test_empty_person_shows_placeholder(self, qtbot, env):
        view, face_db, catalog = _make_view(qtbot, env)
        alice = catalog.create_person("Alice")

        view.set_person(PersonInfo(name="Alice", id=alice.id))

        assert view._lbl_empty.isVisibleTo(view)
        assert view._flat_cards == {}

    def test_confirmed_faces_build_thumbs(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice, fids = self._seed_confirmed(face_db, catalog, tmp_path)

        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)

        assert sorted(view._flat_cards.keys()) == sorted(fids)
        assert view._lbl_title.text() == "Visages de Alice"
        assert not view._pending_section.isVisibleTo(view)

    def test_pending_suggestions_build_pending_section(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice = catalog.create_person("Alice")
        photo = _make_photo(tmp_path)
        for _ in range(2):
            _raw_insert_face(face_db, photo, cluster_id=5,
                             suggestion_person_id=alice.id,
                             suggestion_score=0.62, embedding=[1.0, 0.0])

        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)

        assert view._pending_section.isVisibleTo(view)
        assert len(view._pending_flat_cards) == 1        # 1 vignette par groupe
        assert set(view._pending_thumb_clusters.values()) == {5}

    def test_pending_accept_reject_by_face(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice = catalog.create_person("Alice")
        photo = _make_photo(tmp_path)
        _raw_insert_face(face_db, photo, cluster_id=5,
                         suggestion_person_id=alice.id,
                         suggestion_score=0.60, embedding=[1.0, 0.0])
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)
        thumb_fid = next(iter(view._pending_flat_cards))

        with qtbot.waitSignal(view.suggestion_accepted, timeout=1000) as blocker:
            view._on_pending_accept_by_face(thumb_fid)
        assert blocker.args == [5]

        with qtbot.waitSignal(view.suggestion_rejected, timeout=1000) as blocker:
            view._on_pending_reject_by_face(thumb_fid)
        assert blocker.args == [5]

    def test_accept_all_reject_all(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice = catalog.create_person("Alice")
        photo = _make_photo(tmp_path)
        for cid in (5, 6):
            _raw_insert_face(face_db, photo, cluster_id=cid,
                             suggestion_person_id=alice.id,
                             suggestion_score=0.60, embedding=[1.0, 0.0])
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)

        with qtbot.waitSignal(view.all_suggestions_accepted, timeout=1000) as blocker:
            view._on_accept_all()
        assert sorted(blocker.args[0]) == [5, 6]

        with qtbot.waitSignal(view.all_suggestions_rejected, timeout=1000) as blocker:
            view._on_reject_all()
        assert sorted(blocker.args[0]) == [5, 6]

    def test_remove_pending_cluster_hides_section_when_empty(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice = catalog.create_person("Alice")
        photo = _make_photo(tmp_path)
        _raw_insert_face(face_db, photo, cluster_id=5,
                         suggestion_person_id=alice.id,
                         suggestion_score=0.60, embedding=[1.0, 0.0])
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)
        assert view._pending_flat_cards

        view.remove_pending_cluster(5)

        assert view._pending_flat_cards == {}
        assert not view._pending_section.isVisibleTo(view)

    def test_clear_all_pending(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice = catalog.create_person("Alice")
        photo = _make_photo(tmp_path)
        for cid in (5, 6):
            _raw_insert_face(face_db, photo, cluster_id=cid,
                             suggestion_person_id=alice.id,
                             suggestion_score=0.60, embedding=[1.0, 0.0])
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)

        view.clear_all_pending()

        assert view._pending_flat_cards == {}
        assert view._pending_thumb_clusters == {}

    def test_accept_pending_cluster_moves_to_confirmed(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice = catalog.create_person("Alice")
        photo = _make_photo(tmp_path)
        fid = _raw_insert_face(face_db, photo, cluster_id=5,
                               suggestion_person_id=alice.id,
                               suggestion_score=0.60, embedding=[1.0, 0.0])
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)
        assert fid not in view._flat_cards

        view.accept_pending_cluster(5)

        assert fid in view._flat_cards
        assert view._pending_flat_cards == {}
        # laisser le loader démarré par accept_pending_cluster se terminer
        # (thread enfant vivant au teardown → fail-fast 0xC0000409)
        from PySide6.QtCore import QThread as _QThread
        qtbot.waitUntil(
            lambda: all(not t.isRunning() for t in view.findChildren(_QThread)),
            timeout=3000,
        )

    def test_selection_simple_ctrl_shift(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice, fids = self._seed_confirmed(face_db, catalog, tmp_path, n=4)
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)
        f0, f1, f2, f3 = view._flat_order

        view._on_thumb_clicked(f0, False, False)
        assert view._selection == {f0}

        view._on_thumb_clicked(f2, True, False)     # Ctrl : ajout
        assert view._selection == {f0, f2}

        view._on_thumb_clicked(f2, True, False)     # Ctrl : retrait
        assert view._selection == {f0}

        view._on_thumb_clicked(f0, False, False)
        view._on_thumb_clicked(f3, False, True)     # Shift : plage f0→f3
        assert view._selection == {f0, f1, f2, f3}

    def test_remove_flat_thumbs_to_empty_state(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice, fids = self._seed_confirmed(face_db, catalog, tmp_path, n=2)
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)

        view._remove_flat_thumbs(fids)

        assert view._flat_cards == {}
        assert view._lbl_empty.isVisibleTo(view)

    def test_flat_unassign_runs_thread_and_emits(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice, fids = self._seed_confirmed(face_db, catalog, tmp_path, n=2)
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)

        with qtbot.waitSignal(view.faces_reassigned, timeout=3000):
            view._flat_unassign([fids[0]])

        assert face_db.get_face_by_id(fids[0]).person_id is None
        assert fids[0] not in view._flat_cards

    def test_set_cover_face_emits_with_person(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice, fids = self._seed_confirmed(face_db, catalog, tmp_path, n=1)
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)

        with qtbot.waitSignal(view.cover_face_set, timeout=1000) as blocker:
            view._set_cover_face(fids[0])

        assert blocker.args[0] == alice.id
        assert blocker.args[1].id == fids[0]

    def test_reassign_dialog_moves_faces(self, qtbot, env, tmp_path, monkeypatch):
        view, face_db, catalog = _make_view(qtbot, env)
        alice, fids = self._seed_confirmed(face_db, catalog, tmp_path, n=2)
        boris = catalog.create_person("Boris")
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)
        monkeypatch.setattr(_AssignDialog, "exec", lambda self: QDialog.Accepted)
        monkeypatch.setattr(_AssignDialog, "is_new_person", lambda self: False)
        monkeypatch.setattr(_AssignDialog, "existing_person_id",
                            lambda self: boris.id)

        with qtbot.waitSignal(view.faces_reassigned, timeout=1000):
            view._show_flat_reassign_dialog([fids[0]], [
                PersonInfo(name="Alice", id=alice.id),
                PersonInfo(name="Boris", id=boris.id),
            ])

        assert face_db.get_face_by_id(fids[0]).person_id == boris.id
        assert fids[0] not in view._flat_cards

    def test_set_person_same_id_only_updates_title(self, qtbot, env, tmp_path):
        view, face_db, catalog = _make_view(qtbot, env)
        alice, _ = self._seed_confirmed(face_db, catalog, tmp_path, n=1)
        view.set_person(PersonInfo(name="Alice", id=alice.id))
        _wait_loaders(qtbot, view)
        cards_before = dict(view._flat_cards)

        view.set_person(PersonInfo(name="Alice Renommée", id=alice.id))

        assert view._lbl_title.text() == "Visages de Alice Renommée"
        assert view._flat_cards == cards_before   # pas de rebuild
