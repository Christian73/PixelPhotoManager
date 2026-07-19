# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Extension de test_face_database.py : méthodes non couvertes par le fichier
d'origine — comptages/embeddings, update_clusters (propagation, bruit,
libération Picasa), suggestions de clusters (set/clear/accept/resuggest,
find_similar_to_persons, isolate_and_suggest), faces représentatives et
centroïdes par lots, isolation/ajout manuel, recalculate_size_ignored,
save_picasa_annotations (placeholders, consommation par personne et par
chevauchement), maintenance (reset, delete/update paths, stats)."""
import os
import sqlite3

import pytest
from PIL import Image

from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase, _enc

# ------------------------------------------------------------------ helpers


def _base_vec(index: int, dim: int = 8) -> list[float]:
    v = [0.01] * dim
    v[index % dim] = 1.0
    return v


def _similar_vec(base: list[float], noise: float = 0.02, seed: int = 0) -> list[float]:
    import random
    rnd = random.Random(seed)
    return [b + rnd.uniform(-noise, noise) for b in base]


def _insert_face(
    db, photo_path, person_id=None, cluster_id=None,
    bbox=(0, 0, 50, 50), embedding=None, ignored=0, pinned=0,
    det_score=None, is_cover=0, suggestion_person_id=None,
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        blob = _enc(embedding) if embedding else None
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, embedding,"
            "  cluster_id, person_id, ignored, pinned, det_score, is_cover,"
            "  suggestion_person_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (photo_path, *bbox, blob, cluster_id, person_id, ignored, pinned,
             det_score, is_cover, suggestion_person_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _q1(db, sql, params=()):
    conn = sqlite3.connect(db._db_path)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _qall(db, sql, params=()):
    conn = sqlite3.connect(db._db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    return FaceDatabase(db_path=tmp_path / "faces.db")


# ------------------------------------------------------------------ counts & embeddings


class TestCountsAndEmbeddings:
    def test_count_embeddings_excludes_pinned(self, db):
        _insert_face(db, "a.jpg", embedding=_base_vec(0))
        _insert_face(db, "b.jpg", embedding=_base_vec(1), pinned=1)
        _insert_face(db, "c.jpg")  # sans embedding
        assert db.count_embeddings() == 1

    def test_count_identified_faces(self, db):
        _insert_face(db, "a.jpg", embedding=_base_vec(0), person_id=1)
        _insert_face(db, "b.jpg", embedding=_base_vec(1))
        assert db.count_identified_faces() == 1

    def test_get_all_embeddings_shape_and_filters(self, db):
        f1 = _insert_face(db, "a.jpg", embedding=_base_vec(0))
        _insert_face(db, "b.jpg", embedding=_base_vec(1), ignored=1)
        _insert_face(db, "c.jpg", embedding=_base_vec(2), pinned=1)
        f4 = _insert_face(db, "d.jpg", embedding=_base_vec(3), person_id=9)

        embs, ids = db.get_all_embeddings()
        assert embs.shape == (2, 8)
        assert set(ids) == {f1, f4}

        embs_u, ids_u = db.get_all_embeddings(only_unidentified=True)
        assert ids_u == [f1]

    def test_get_all_embeddings_empty(self, db):
        embs, ids = db.get_all_embeddings()
        assert embs.shape == (0, 0)
        assert ids == []


class TestUpdateClusters:
    def test_labels_applied_and_noise_null(self, db):
        f1 = _insert_face(db, "a.jpg", embedding=_base_vec(0))
        f2 = _insert_face(db, "b.jpg", embedding=_base_vec(0))
        f3 = _insert_face(db, "c.jpg", embedding=_base_vec(1))
        db.update_clusters([f1, f2, f3], [0, 0, -1])
        assert _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f1,))[0] == 0
        assert _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f2,))[0] == 0
        assert _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f3,))[0] is None

    def test_person_propagated_to_new_cluster_member(self, db):
        _insert_face(db, "a.jpg", embedding=_base_vec(0), person_id=5, cluster_id=7)
        f_new = _insert_face(db, "b.jpg", embedding=_base_vec(0))
        db.update_clusters([f_new], [7])
        assert _q1(db, "SELECT person_id FROM faces WHERE id=?", (f_new,))[0] == 5

    def test_orphaned_noise_face_loses_person(self, db):
        f = _insert_face(db, "a.jpg", embedding=_base_vec(0), person_id=5,
                         cluster_id=None)
        other = _insert_face(db, "b.jpg", embedding=_base_vec(1))
        db.update_clusters([other], [0])
        assert _q1(db, "SELECT person_id FROM faces WHERE id=?", (f,))[0] is None

    def test_empty_input_noop(self, db):
        db.update_clusters([], [])  # ne doit pas lever

    def test_progress_callback_called(self, db):
        f = _insert_face(db, "a.jpg", embedding=_base_vec(0))
        msgs: list[str] = []
        db.update_clusters([f], [0], progress_cb=msgs.append)
        assert len(msgs) == 1


# ------------------------------------------------------------------ suggestions


class TestClusterSuggestions:
    def _cluster_with_person(self, db):
        """cluster 1 non identifié (vec 0), personne 9 nommée (vec 0 aussi)."""
        _insert_face(db, "c1.jpg", cluster_id=1,
                     embedding=_similar_vec(_base_vec(0), seed=1))
        _insert_face(db, "c2.jpg", cluster_id=1,
                     embedding=_similar_vec(_base_vec(0), seed=2))
        _insert_face(db, "p1.jpg", person_id=9, cluster_id=100,
                     embedding=_base_vec(0))

    def test_set_and_get_suggestions(self, db):
        self._cluster_with_person(db)
        db.set_cluster_suggestions({1: (9, 0.8)})
        assert db.get_suggested_clusters_for_person(9) == [(1, 2, 0.8)]
        assert db.get_persons_pending_count() == {9: 1}

    def test_set_suggestions_idempotent(self, db):
        self._cluster_with_person(db)
        db.set_cluster_suggestions({1: (9, 0.8)})
        db.set_cluster_suggestions({1: (7, 0.9)})  # déjà suggéré → inchangé
        assert db.get_suggested_clusters_for_person(9) != []
        assert db.get_suggested_clusters_for_person(7) == []

    def test_clear_suggestion(self, db):
        self._cluster_with_person(db)
        db.set_cluster_suggestions({1: (9, 0.8)})
        db.clear_cluster_suggestion(1)
        assert db.get_suggested_clusters_for_person(9) == []

    def test_accept_suggestion_assigns_person(self, db):
        self._cluster_with_person(db)
        db.set_cluster_suggestions({1: (9, 0.8)})
        db.accept_cluster_suggestion(1)
        rows = _qall(db, "SELECT person_id FROM faces WHERE cluster_id=1")
        assert all(r[0] == 9 for r in rows)
        assert db.get_persons_pending_count() == {}

    def test_accept_without_suggestion_noop(self, db):
        self._cluster_with_person(db)
        db.accept_cluster_suggestion(1)  # aucune suggestion → return silencieux
        rows = _qall(db, "SELECT person_id FROM faces WHERE cluster_id=1")
        assert all(r[0] is None for r in rows)

    def test_resuggest_finds_best_person(self, db):
        self._cluster_with_person(db)
        db.resuggest_clusters([1])
        sugg = db.get_suggested_clusters_for_person(9)
        assert len(sugg) == 1
        assert sugg[0][0] == 1
        assert sugg[0][2] > 0.9

    def test_resuggest_excludes_person(self, db):
        self._cluster_with_person(db)
        db.resuggest_clusters([1], exclude_person_id=9)
        assert db.get_suggested_clusters_for_person(9) == []

    def test_resuggest_empty_input(self, db):
        db.resuggest_clusters([])  # ne doit pas lever

    def test_find_similar_to_persons(self, db):
        self._cluster_with_person(db)
        calls: list = []
        created, checked = db.find_similar_to_persons(
            progress_cb=lambda i, t: calls.append((i, t))
        )
        assert created == 1
        assert checked == 1
        assert calls == [(1, 1)]
        assert db.get_suggested_clusters_for_person(9)[0][0] == 1

    def test_find_similar_no_clusters(self, db):
        assert db.find_similar_to_persons() == (0, 0)

    def test_find_similar_no_persons(self, db):
        _insert_face(db, "c1.jpg", cluster_id=1, embedding=_base_vec(0))
        created, checked = db.find_similar_to_persons()
        assert created == 0
        assert checked == 1


# ------------------------------------------------------------------ representative faces


class TestRepresentativeFaces:
    def test_largest_bbox_wins(self, db):
        _insert_face(db, "small.jpg", cluster_id=1, bbox=(0, 0, 30, 30))
        _insert_face(db, "big.jpg", cluster_id=1, bbox=(0, 0, 90, 90))
        face = db.get_representative_face(cluster_id=1)
        assert face.photo_path == "big.jpg"

    def test_cover_overrides_size(self, db):
        f_small = _insert_face(db, "small.jpg", cluster_id=1, bbox=(0, 0, 30, 30))
        _insert_face(db, "big.jpg", cluster_id=1, bbox=(0, 0, 90, 90))
        db.set_cover_face(f_small)
        face = db.get_representative_face(cluster_id=1)
        assert face.photo_path == "small.jpg"

    def test_set_cover_clears_previous(self, db):
        f1 = _insert_face(db, "a.jpg", cluster_id=1, is_cover=1)
        f2 = _insert_face(db, "b.jpg", cluster_id=1)
        db.set_cover_face(f2)
        assert _q1(db, "SELECT is_cover FROM faces WHERE id=?", (f1,))[0] == 0
        assert _q1(db, "SELECT is_cover FROM faces WHERE id=?", (f2,))[0] == 1

    def test_set_cover_unclustered_noop(self, db):
        f = _insert_face(db, "a.jpg", cluster_id=None)
        db.set_cover_face(f)  # return silencieux
        assert _q1(db, "SELECT is_cover FROM faces WHERE id=?", (f,))[0] == 0

    def test_by_person(self, db):
        _insert_face(db, "p.jpg", person_id=4, bbox=(0, 0, 80, 80))
        face = db.get_representative_face(person_id=4)
        assert face.photo_path == "p.jpg"

    def test_missing_returns_none(self, db):
        assert db.get_representative_face(cluster_id=42) is None

    def test_get_face_by_id(self, db):
        f = _insert_face(db, "a.jpg", cluster_id=3, person_id=7, bbox=(1, 2, 3, 4))
        info = db.get_face_by_id(f)
        assert (info.bbox_x, info.bbox_y, info.bbox_w, info.bbox_h) == (1, 2, 3, 4)
        assert info.cluster_id == 3
        assert info.person_id == 7
        assert db.get_face_by_id(9999) is None

    def test_get_all_representative_faces(self, db):
        f_cover = _insert_face(db, "cov.jpg", cluster_id=1, bbox=(0, 0, 10, 10),
                               is_cover=1)
        _insert_face(db, "big1.jpg", cluster_id=1, bbox=(0, 0, 99, 99))
        _insert_face(db, "big2.jpg", cluster_id=2, bbox=(0, 0, 50, 50))
        _insert_face(db, "ign.jpg", cluster_id=3, ignored=1)
        result = db.get_all_representative_faces([1, 2, 3])
        assert result[1].id == f_cover          # cover prioritaire
        assert result[2].photo_path == "big2.jpg"
        assert 3 not in result                  # que des ignorés
        assert db.get_all_representative_faces([]) == {}


class TestCentroids:
    def test_representative_embedding_cluster_and_person(self, db):
        _insert_face(db, "a.jpg", cluster_id=1, embedding=[1.0] * 8)
        _insert_face(db, "b.jpg", cluster_id=1, embedding=[3.0] * 8)
        _insert_face(db, "c.jpg", person_id=5, embedding=[2.0] * 8)
        emb_c = db.get_representative_embedding(cluster_id=1)
        assert emb_c == pytest.approx([2.0] * 8)
        emb_p = db.get_representative_embedding(person_id=5)
        assert emb_p == pytest.approx([2.0] * 8)
        assert db.get_representative_embedding(cluster_id=99) is None

    def test_get_all_cluster_centroids(self, db):
        _insert_face(db, "a.jpg", cluster_id=1, embedding=[1.0] * 8)
        _insert_face(db, "b.jpg", cluster_id=1, embedding=[3.0] * 8)
        _insert_face(db, "c.jpg", cluster_id=2, embedding=[5.0] * 8)
        result = db.get_all_cluster_centroids([1, 2, 99])
        assert result[1] == pytest.approx([2.0] * 8)
        assert result[2] == pytest.approx([5.0] * 8)
        assert 99 not in result
        assert db.get_all_cluster_centroids([]) == {}

    def test_get_all_person_cluster_centroids(self, db):
        _insert_face(db, "a.jpg", person_id=5, cluster_id=1, embedding=[1.0] * 8)
        _insert_face(db, "b.jpg", person_id=5, cluster_id=2, embedding=[3.0] * 8)
        result = db.get_all_person_cluster_centroids([5])
        assert set(result[5].keys()) == {1, 2}
        assert result[5][1] == pytest.approx([1.0] * 8)
        assert db.get_all_person_cluster_centroids([]) == {}

    def test_get_cluster_person_and_persons(self, db):
        _insert_face(db, "a.jpg", cluster_id=1, person_id=5)
        _insert_face(db, "b.jpg", cluster_id=2)
        assert db.get_cluster_person(1) == 5
        assert db.get_cluster_person(2) is None
        assert db.get_cluster_persons([1, 2]) == {1: 5}
        assert db.get_cluster_persons([]) == {}


# ------------------------------------------------------------------ isolation & manual


class TestIsolation:
    def test_isolate_and_assign_face(self, db):
        f = _insert_face(db, "a.jpg", cluster_id=3, embedding=_base_vec(0))
        db.isolate_and_assign_face(f, 7)
        row = _q1(db, "SELECT cluster_id, pinned, person_id FROM faces WHERE id=?", (f,))
        assert row[0] < 0
        assert row[1] == 1
        assert row[2] == 7

    def test_isolated_clusters_are_unique(self, db):
        f1 = _insert_face(db, "a.jpg", cluster_id=3)
        f2 = _insert_face(db, "b.jpg", cluster_id=3)
        db.isolate_and_assign_face(f1, 7)
        db.isolate_and_assign_face(f2, 8)
        c1 = _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f1,))[0]
        c2 = _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f2,))[0]
        assert c1 != c2
        assert c1 < 0 and c2 < 0

    def test_add_manual_face(self, db):
        fid = db.add_manual_face("photo.jpg", (10, 20, 30, 40), person_id=7)
        row = _q1(
            db,
            "SELECT photo_path, bbox_x, bbox_w, embedding, cluster_id,"
            "       person_id, pinned, det_score FROM faces WHERE id=?",
            (fid,),
        )
        assert row[0] == "photo.jpg"
        assert (row[1], row[2]) == (10, 30)
        assert row[3] is None          # embedding NULL par construction
        assert row[4] < 0              # cluster isolé
        assert row[5] == 7
        assert row[6] == 1             # pinned
        assert row[7] == 1.0

    def test_isolate_and_suggest_creates_suggestion(self, db):
        # personne 1 = vec0, personne 2 = vec1
        _insert_face(db, "p1.jpg", person_id=1, cluster_id=10, embedding=_base_vec(0))
        _insert_face(db, "p2.jpg", person_id=2, cluster_id=11, embedding=_base_vec(1))
        # visage assigné à tort à la personne 1, mais ressemble à la personne 2
        f = _insert_face(db, "x.jpg", person_id=1, cluster_id=10,
                         embedding=_similar_vec(_base_vec(1), seed=3))

        db.isolate_and_suggest([f], exclude_person_id=1)

        row = _q1(
            db,
            "SELECT cluster_id, pinned, person_id, suggestion_person_id"
            " FROM faces WHERE id=?",
            (f,),
        )
        assert row[0] < 0
        assert row[1] == 1
        assert row[2] is None
        assert row[3] == 2   # suggéré à la personne 2, pas la 1 (exclue)

    def test_isolate_and_suggest_empty_input(self, db):
        db.isolate_and_suggest([])  # ne doit pas lever

    def test_unassign_person_from_face(self, db):
        f = _insert_face(db, "a.jpg", person_id=5, cluster_id=1)
        db.unassign_person_from_face(f)
        assert _q1(db, "SELECT person_id, cluster_id FROM faces WHERE id=?", (f,)) \
            == (None, 1)

    def test_get_ignored_faces_for_photo(self, db):
        _insert_face(db, "a.jpg", ignored=1, bbox=(1, 2, 3, 4))
        _insert_face(db, "a.jpg", ignored=0)
        faces = db.get_ignored_faces_for_photo("a.jpg")
        assert len(faces) == 1
        assert faces[0].bbox_w == 3


class TestAssignPersonToCluster:
    def test_all_faces_updated(self, db):
        _insert_face(db, "a.jpg", cluster_id=1)
        _insert_face(db, "b.jpg", cluster_id=1)
        _insert_face(db, "c.jpg", cluster_id=2)
        db.assign_person_to_cluster(1, 9)
        rows = _qall(db, "SELECT person_id FROM faces WHERE cluster_id=1")
        assert all(r[0] == 9 for r in rows)
        assert _q1(db, "SELECT person_id FROM faces WHERE cluster_id=2")[0] is None


# ------------------------------------------------------------------ enrichment


class TestEnrichment:
    def test_get_person_photo_count(self, db):
        _insert_face(db, "a.jpg", person_id=5, cluster_id=1)
        _insert_face(db, "a.jpg", person_id=5, cluster_id=1)  # même photo
        _insert_face(db, "b.jpg", person_id=5, cluster_id=1)
        _insert_face(db, "c.jpg", person_id=5, cluster_id=None)  # hors cluster
        assert db.get_person_photo_count(5) == 2

    def test_enrich_persons_photo_count(self, db):
        _insert_face(db, "a.jpg", person_id=5, cluster_id=1)
        persons = [PersonInfo(name="Alice", id=5), PersonInfo(name="Bob", id=6)]
        db.enrich_persons_photo_count(persons)
        assert persons[0].photo_count == 1
        assert persons[1].photo_count == 0
        db.enrich_persons_photo_count([])  # ne doit pas lever

    def test_enrich_persons_full(self, db):
        _insert_face(db, "small.jpg", person_id=5, cluster_id=1, bbox=(0, 0, 10, 10))
        _insert_face(db, "big.jpg", person_id=5, cluster_id=1, bbox=(4, 5, 90, 90))
        persons = [PersonInfo(name="Alice", id=5)]
        db.enrich_persons(persons)
        p = persons[0]
        assert p.photo_count == 2
        assert p.cover_path == "big.jpg"
        assert p.cover_bbox == (4, 5, 90, 90)
        assert p.pending_count == 0
        db.enrich_persons([])  # ne doit pas lever


# ------------------------------------------------------------------ recalculate_size_ignored


class TestRecalculateSizeIgnored:
    def test_faces_above_threshold_restored(self, db, tmp_path):
        photo = tmp_path / "photo.jpg"
        Image.new("RGB", (400, 300)).save(str(photo))
        path = str(photo)
        # premier plan 100×100 (≥ 60 = 20 % de 300) → seuil effectif = 100/4 = 25
        _insert_face(db, path, bbox=(0, 0, 100, 100), embedding=_base_vec(0),
                     det_score=0.9)
        f_mid = _insert_face(db, path, bbox=(0, 0, 40, 40), embedding=_base_vec(1),
                             det_score=0.9, ignored=1)
        f_tiny = _insert_face(db, path, bbox=(0, 0, 20, 20), embedding=_base_vec(2),
                              det_score=0.9, ignored=1)

        progress: list = []
        unignored, total = db.recalculate_size_ignored(
            progress_cb=lambda i, t: progress.append((i, t))
        )

        assert total == 1
        assert unignored == 1
        assert _q1(db, "SELECT ignored FROM faces WHERE id=?", (f_mid,))[0] == 0
        assert _q1(db, "SELECT ignored FROM faces WHERE id=?", (f_tiny,))[0] == 1
        assert progress[-1] == (1, 1)

    def test_low_score_faces_not_considered(self, db, tmp_path):
        photo = tmp_path / "photo.jpg"
        Image.new("RGB", (400, 300)).save(str(photo))
        _insert_face(db, str(photo), bbox=(0, 0, 100, 100),
                     embedding=_base_vec(0), det_score=0.3, ignored=1)
        unignored, total = db.recalculate_size_ignored()
        assert (unignored, total) == (0, 0)

    def test_missing_photo_skipped(self, db, tmp_path):
        _insert_face(db, str(tmp_path / "absente.jpg"), bbox=(0, 0, 100, 100),
                     embedding=_base_vec(0), det_score=0.9, ignored=1)
        unignored, total = db.recalculate_size_ignored()
        assert (unignored, total) == (0, 1)


# ------------------------------------------------------------------ picasa annotations


class TestSavePicasaAnnotations:
    def test_placeholder_created_when_photo_not_detected(self, db):
        db.save_picasa_annotations("a.jpg", [{"bbox": (1, 2, 30, 40), "person_id": 7}])
        rows = _qall(
            db,
            "SELECT bbox_x, bbox_w, person_id, embedding FROM faces"
            " WHERE photo_path='a.jpg'",
        )
        assert rows == [(1, 30, 7, None)]
        assert _q1(db, "SELECT consumed FROM picasa_annotations")[0] == 0

    def test_consumed_when_person_already_recognized(self, db):
        _insert_face(db, "a.jpg", person_id=7, embedding=_base_vec(0),
                     bbox=(200, 200, 50, 50))
        db.save_picasa_annotations("a.jpg", [{"bbox": (1, 2, 30, 40), "person_id": 7}])
        assert _q1(db, "SELECT consumed FROM picasa_annotations")[0] == 1
        # pas de placeholder ajouté
        n = _q1(db, "SELECT COUNT(*) FROM faces WHERE photo_path='a.jpg'")[0]
        assert n == 1

    def test_consumed_on_spatial_overlap(self, db):
        # face ArcFace non identifiée au même endroit → l'annotation lui est
        # appliquée par IoU et consommée, pas de placeholder
        f = _insert_face(db, "a.jpg", embedding=_base_vec(0), bbox=(10, 10, 50, 50))
        db.save_picasa_annotations(
            "a.jpg", [{"bbox": (12, 12, 48, 48), "person_id": 7}]
        )
        assert _q1(db, "SELECT consumed FROM picasa_annotations")[0] == 1
        assert _q1(db, "SELECT person_id FROM faces WHERE id=?", (f,))[0] == 7

    def test_reimport_does_not_duplicate_placeholders(self, db):
        ann = [{"bbox": (1, 2, 30, 40), "person_id": 7}]
        db.save_picasa_annotations("a.jpg", ann)
        db.save_picasa_annotations("a.jpg", ann)
        n = _q1(db, "SELECT COUNT(*) FROM faces WHERE photo_path='a.jpg'")[0]
        assert n == 1
        n_ann = _q1(db, "SELECT COUNT(*) FROM picasa_annotations")[0]
        assert n_ann == 1


# ------------------------------------------------------------------ maintenance


class TestMaintenance:
    def test_reset_clustering_keeps_identified(self, db):
        f_unnamed = _insert_face(db, "a.jpg", cluster_id=1)
        f_named = _insert_face(db, "b.jpg", cluster_id=2, person_id=5)
        f_pinned = _insert_face(db, "c.jpg", cluster_id=-1, pinned=1)
        db.reset_clustering()
        assert _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f_unnamed,))[0] is None
        assert _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f_named,))[0] == 2
        assert _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f_pinned,))[0] == -1

    def test_reset_index_clears_faces_keeps_annotations(self, db):
        _insert_face(db, "a.jpg", cluster_id=1)
        db.save_picasa_annotations("b.jpg", [{"bbox": (0, 0, 30, 30), "person_id": 7}])
        # forcer consumed=1 pour vérifier la réinitialisation
        conn = sqlite3.connect(db._db_path)
        conn.execute("UPDATE picasa_annotations SET consumed=1")
        conn.commit()
        conn.close()

        db.reset_index()

        assert _q1(db, "SELECT COUNT(*) FROM faces")[0] == 0
        assert _q1(db, "SELECT consumed FROM picasa_annotations")[0] == 0

    def test_delete_for_path(self, db):
        _insert_face(db, "a.jpg")
        db.save_picasa_annotations("a.jpg", [{"bbox": (0, 0, 30, 30), "person_id": 7}])
        _insert_face(db, "b.jpg")
        db.delete_for_path("a.jpg")
        assert _q1(db, "SELECT COUNT(*) FROM faces")[0] == 1
        assert _q1(db, "SELECT COUNT(*) FROM picasa_annotations")[0] == 0

    def test_delete_for_paths(self, db):
        _insert_face(db, "a.jpg")
        _insert_face(db, "b.jpg")
        _insert_face(db, "c.jpg")
        db.delete_for_paths(["a.jpg", "b.jpg"])
        assert _q1(db, "SELECT COUNT(*) FROM faces")[0] == 1
        db.delete_for_paths([])  # ne doit pas lever

    def test_update_path(self, db):
        _insert_face(db, os.path.normpath("d/old.jpg"))
        db.update_path("d/old.jpg", "d/new.jpg")
        assert _q1(db, "SELECT photo_path FROM faces")[0] == os.path.normpath("d/new.jpg")

    def test_update_paths_prefix(self, db):
        old = os.path.normpath("C:/photos/vacances")
        _insert_face(db, os.path.join(old, "a.jpg"))
        _insert_face(db, os.path.normpath("C:/photos/autres/b.jpg"))
        db.update_paths_prefix("C:/photos/vacances", "D:/archive/vacances")
        paths = {r[0] for r in _qall(db, "SELECT photo_path FROM faces")}
        assert os.path.normpath("D:/archive/vacances/a.jpg") in paths
        assert os.path.normpath("C:/photos/autres/b.jpg") in paths

    def test_get_stats(self, db):
        _insert_face(db, "a.jpg", cluster_id=1, person_id=5)
        _insert_face(db, "b.jpg", cluster_id=2)
        stats = db.get_stats()
        assert stats["total_faces"] == 2
        assert stats["named_persons"] == 1
        assert stats["clusters"] == 2

    def test_get_recognition_counters(self, db):
        _insert_face(db, "a.jpg", person_id=5, embedding=_base_vec(0), cluster_id=1)
        _insert_face(db, "b.jpg", embedding=_base_vec(1), cluster_id=2)
        _insert_face(db, "c.jpg", ignored=1)
        _insert_face(db, "d.jpg", suggestion_person_id=5, embedding=_base_vec(2))
        counters = db.get_recognition_counters()
        assert counters["total_faces"] == 4
        assert counters["ignored_faces"] == 1
        assert counters["identified_faces"] == 1
        assert counters["recognized_faces"] == 1
        assert counters["pending_faces"] == 1
        assert counters["unknown_faces"] == 1
        assert counters["clusters"] == 2

    def test_restore_orphaned_ignored_faces(self, db):
        # groupe (photo, personne) entièrement ignoré → le plus grand est restauré
        _insert_face(db, "a.jpg", person_id=5, bbox=(0, 0, 30, 30), ignored=1)
        f_big = _insert_face(db, "a.jpg", person_id=5, bbox=(0, 0, 90, 90), ignored=1)
        restored = db.restore_orphaned_ignored_faces()
        assert restored == 1
        assert _q1(db, "SELECT ignored FROM faces WHERE id=?", (f_big,))[0] == 0

    def test_assign_person_synthetic_clusters(self, db):
        f = _insert_face(db, "a.jpg", person_id=5, cluster_id=3,
                         embedding=_base_vec(0))
        n = db.assign_person_synthetic_clusters()
        assert n == 1
        assert _q1(db, "SELECT cluster_id FROM faces WHERE id=?", (f,))[0] \
            == 10_000_000 + 5

    def test_ignore_unignore_cluster(self, db):
        f = _insert_face(db, "a.jpg", cluster_id=1)
        db.ignore_cluster(1)
        assert _q1(db, "SELECT ignored FROM faces WHERE id=?", (f,))[0] == 1
        db.unignore_cluster(1)
        assert _q1(db, "SELECT ignored FROM faces WHERE id=?", (f,))[0] == 0

    def test_get_clusters_and_unnamed(self, db):
        _insert_face(db, "a.jpg", cluster_id=1)
        _insert_face(db, "b.jpg", cluster_id=1)
        _insert_face(db, "c.jpg", cluster_id=2, person_id=5)
        assert db.get_clusters() == [(1, 2), (2, 1)]
        assert db.get_unnamed_clusters() == [(1, 2)]
