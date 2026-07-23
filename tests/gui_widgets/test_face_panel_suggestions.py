# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste l'affichage et l'interaction des suggestions de personne en attente de
vérification dans FacePanel (panneau visages de la visionneuse) : nom suggéré +
tick vert/croix rouge superposés sur la vignette pour confirmer/rejeter sans
passer par le dialogue d'assignation complet."""
import math
import sqlite3

from PIL import Image

from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
from src.ui.face_panel import FacePanel


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
    """Vecteur unitaire dans le plan (e0, e1), incliné de angle_rad par rapport à
    e0 — permet de fabriquer deux embeddings à une similarité cosinus contrôlée
    (cos(angle_rad)) sans dépendre d'un vrai modèle de reconnaissance faciale."""
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

        # L'écriture (accept_cluster_suggestion) part dans un _DbWriteWorker ;
        # person_assigned est émis sur le thread UI une fois le worker terminé,
        # donc après le commit — on attend ce signal avant de lire la DB.
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


class TestProbableMatchInformativeLabel:
    """Libellé informatif "≈ Probablement/Peut-être X" pour un visage dont la
    similarité au centroïde d'une personne connue est calculée à la volée
    (0.45 <= sim < 0.55, sous le seuil de suggestion persistée _SIM_SUGGEST) —
    pas de coche ✓/✕ à ce niveau de confiance, cf. choix utilisateur explicite."""

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
        assert "Probablement" in item._name_label.text()
        assert person.name in item._name_label.text()
        assert not hasattr(item, "_btn_accept")
        assert not hasattr(item, "_btn_reject")

    def test_weak_match_shows_gray_peut_etre_label(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        person = self._seed_person_face(face_db, catalog, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        # cos(angle) ~= 0.47 : dans [_SIM_WEAK=0.45, _SIM_STRONG=0.50)
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=77,
            embedding=_tilted_embedding(math.acos(0.47)),
        )

        _load_and_settle(qtbot, panel, photo)

        item = panel._items[face_id]
        assert "Peut-être" in item._name_label.text()
        assert person.name in item._name_label.text()
        assert not hasattr(item, "_btn_accept")
        assert not hasattr(item, "_btn_reject")

    def test_below_threshold_shows_generic_group_label(self, qtbot, tmp_path):
        panel, face_db, catalog = _make_panel(qtbot, tmp_path)
        self._seed_person_face(face_db, catalog, tmp_path)
        photo = str(tmp_path / "a.jpg")
        _make_photo(photo)
        # cos(angle) ~= 0.30 : sous _SIM_WEAK (0.45), aucun libellé informatif
        face_id = _raw_insert_face(
            face_db, photo, cluster_id=77,
            embedding=_tilted_embedding(math.acos(0.30)),
        )

        _load_and_settle(qtbot, panel, photo)

        item = panel._items[face_id]
        assert "Groupe 77" in item._name_label.text()
