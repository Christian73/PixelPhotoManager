# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests the display and the interaction of the person suggestions awaiting
verification in FacePanel (the faces panel of the viewer): suggested name +
green tick/red cross overlaid on the thumbnail to confirm/reject without going
through the full assignment dialog."""
import math
import sqlite3

from PIL import Image

from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
from src.ui.face_panel import FacePanel
from tests.gui_widgets.thread_wait import wait_thread_done


def _make_photo(path) -> None:
    Image.new("RGB", (200, 200), color=(128, 128, 128)).save(path)


def _raw_insert_face(
    db, photo_path, cluster_id=None, person_id=None,
    suggestion_person_id=None, suggestion_score=None, bbox=(10, 10, 50, 50),
    embedding=None,
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            "  cluster_id, person_id, suggestion_person_id, suggestion_score, embedding)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                photo_path, *bbox, cluster_id, person_id, suggestion_person_id,
                suggestion_score, _enc(embedding) if embedding is not None else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _tilted_embedding(angle_rad: float, dim: int = 8) -> list[float]:
    """Unit vector in the (e0, e1) plane, tilted by angle_rad with respect to
    e0 -- makes it possible to fabricate two embeddings at a controlled cosine
    similarity (cos(angle_rad)) without depending on a real face recognition
    model."""
    vec = [0.0] * dim
    vec[0] = math.cos(angle_rad)
    vec[1] = math.sin(angle_rad)
    return vec


def _make_panel(qtbot, tmp_path):
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    panel = FacePanel(face_db, catalog)
    qtbot.addWidget(panel)
    return panel, face_db, catalog


def _load_and_settle(qtbot, panel, photo_path):
    """set_photo() starts its loads in real QThreads; we wait for them to
    really finish rather than calling run() synchronously, so as to cover the
    real cross-thread wiring too (cf. the CLAUDE.md convention: keep a few real
    .start() calls per module for the plumbing).

    Polling (cf. wait_thread_done) and not waitSignal: the threads are already
    started when we get here, and a lost emission would make the blocker expire."""
    panel.set_photo(photo_path)
    wait_thread_done(qtbot, panel._data_loader, timeout=2000)
    wait_thread_done(qtbot, panel._loader, timeout=2000)


class TestPendingSuggestionDisplay:
    def test_shows_accept_reject_buttons_and_suggested_name(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        person = catalog.create_person("Uta Boltze")
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=5,
            suggestion_person_id=person.id, suggestion_score=0.653,
        )

        _load_and_settle(qtbot, panel, photo)

        item = panel._items[face_id]
        assert hasattr(item, "_btn_accept")
        assert hasattr(item, "_btn_reject")

    def test_no_buttons_for_face_without_suggestion(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        face_id = _raw_insert_face(face_db, photo, cluster_id=5)

        _load_and_settle(qtbot, panel, photo)

        item = panel._items[face_id]
        assert not hasattr(item, "_btn_accept")
        assert not hasattr(item, "_btn_reject")


class TestAcceptRejectSuggestion:
    def test_accept_assigns_person_and_clears_suggestion(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        person = catalog.create_person("Uta Boltze")
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=5,
            suggestion_person_id=person.id, suggestion_score=0.653,
        )
        _load_and_settle(qtbot, panel, photo)

        # The write (accept_cluster_suggestion) goes into a _DbWriteWorker;
        # person_assigned is emitted on the UI thread once the worker has finished,
        # hence after the commit -- we wait for that signal before reading the DB.
        with qtbot.waitSignal(panel.person_assigned, timeout=2000):
            panel._items[face_id]._btn_accept.click()

        conn = sqlite3.connect(face_db._db_path)
        try:
            row = conn.execute(
                "SELECT person_id, suggestion_person_id FROM faces WHERE id=?", (face_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row == (person.id, None)

    def test_reject_clears_suggestion_without_assigning(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        person = catalog.create_person("Uta Boltze")
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=5,
            suggestion_person_id=person.id, suggestion_score=0.653,
        )
        _load_and_settle(qtbot, panel, photo)

        panel._items[face_id]._btn_reject.click()
        wait_thread_done(qtbot, panel._data_loader, timeout=2000)

        conn = sqlite3.connect(face_db._db_path)
        try:
            row = conn.execute(
                "SELECT person_id, suggestion_person_id FROM faces WHERE id=?", (face_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row == (None, None)


class TestProbableMatchInformativeLabel:
    """Informative "≈ Probably/Maybe X" label for a face whose similarity to
    the centroid of a known person is computed on the fly (0.45 <= sim < 0.55,
    below the persisted suggestion threshold _SIM_SUGGEST) -- no ✓/✕ tick at
    that level of confidence, cf. an explicit user choice."""

    def _seed_person_face(self, face_db, catalog, tmp_path, name="Marc de Saint Roman"):
        person = catalog.create_person(name)
        person_photo = str(tmp_path / "person_ref.jpg")
        _make_photo(person_photo)
        _raw_insert_face(
            face_db, person_photo, cluster_id=None, person_id=person.id,
            embedding=_tilted_embedding(0.0),
        )
        return person

    def test_strong_match_shows_blue_probablement_label(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        person = self._seed_person_face(face_db, catalog, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        # cos(angle) ~= 0.60 >= _SIM_STRONG (0.50)
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=77,
            embedding=_tilted_embedding(math.acos(0.60)),
        )

        _load_and_settle(qtbot, panel, photo)

        item = panel._items[face_id]
        assert "Probably" in item._name_label.text()
        assert person.name in item._name_label.text()
        assert not hasattr(item, "_btn_accept")
        assert not hasattr(item, "_btn_reject")

    def test_weak_match_shows_gray_peut_etre_label(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        person = self._seed_person_face(face_db, catalog, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        # cos(angle) ~= 0.47: within [_SIM_WEAK=0.45, _SIM_STRONG=0.50)
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=77,
            embedding=_tilted_embedding(math.acos(0.47)),
        )

        _load_and_settle(qtbot, panel, photo)

        item = panel._items[face_id]
        assert "Maybe" in item._name_label.text()
        assert person.name in item._name_label.text()
        assert not hasattr(item, "_btn_accept")
        assert not hasattr(item, "_btn_reject")

    def test_below_threshold_shows_generic_group_label(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        self._seed_person_face(face_db, catalog, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        # cos(angle) ~= 0.30: below _SIM_WEAK (0.45), no informative label
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=77,
            embedding=_tilted_embedding(math.acos(0.30)),
        )

        _load_and_settle(qtbot, panel, photo)

        item = panel._items[face_id]
        assert "Groupe 77" in item._name_label.text()
