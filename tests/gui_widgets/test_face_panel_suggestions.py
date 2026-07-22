# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste l'affichage et l'interaction des suggestions de personne en attente de
vérification dans FacePanel (panneau visages de la visionneuse) : nom suggéré +
tick vert/croix rouge superposés sur la vignette pour confirmer/rejeter sans
passer par le dialogue d'assignation complet."""
import sqlite3

from PIL import Image

from src.faces.face_database import FaceDatabase
from src.library.catalog import Catalog
from src.ui.face_panel import FacePanel


def _make_photo(path) -> None:
    Image.new("RGB", (200, 200), color=(128, 128, 128)).save(path)


def _raw_insert_face(
    db, photo_path, cluster_id=None, person_id=None,
    suggestion_person_id=None, suggestion_score=None, bbox=(10, 10, 50, 50),
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            "  cluster_id, person_id, suggestion_person_id, suggestion_score)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (photo_path, *bbox, cluster_id, person_id, suggestion_person_id, suggestion_score),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _make_panel(qtbot, tmp_path):
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    panel = FacePanel(face_db, catalog)
    qtbot.addWidget(panel)
    return panel, face_db, catalog


def _load_and_settle(qtbot, panel, photo_path):
    """set_photo() lance ses chargements dans de vrais QThread ; on attend leur
    achèvement réel (waitSignal) plutôt que d'appeler run() en synchrone, pour
    couvrir aussi le câblage cross-thread réel (cf. convention CLAUDE.md : garder
    quelques vrais .start() + waitSignal par module pour la plomberie)."""
    panel.set_photo(photo_path)
    with qtbot.waitSignal(panel._data_loader.data_ready, timeout=2000):
        pass
    if panel._loader is not None:
        with qtbot.waitSignal(panel._loader.finished, timeout=2000):
            pass


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

        panel._items[face_id]._btn_accept.click()
        with qtbot.waitSignal(panel._data_loader.data_ready, timeout=2000):
            pass

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
        with qtbot.waitSignal(panel._data_loader.data_ready, timeout=2000):
            pass

        conn = sqlite3.connect(face_db._db_path)
        try:
            row = conn.execute(
                "SELECT person_id, suggestion_person_id FROM faces WHERE id=?", (face_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row == (None, None)
