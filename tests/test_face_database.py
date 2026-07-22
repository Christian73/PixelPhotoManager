# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/faces/face_database.py` en pur Python (sqlite3, sans InsightFace ni
Qt) : helpers d'encodage/similarité, régression du bug de fusion de personnes du
2026-07-04 (`merge_persons` perdait les `picasa_annotations` liées), seuils
d'auto-ignore proportionnels de `save_faces`, cache des centroïdes personne,
synchronisation Picasa/ArcFace, CRUD visages/clusters et suggestions par
similarité cosinus."""
import os
import sqlite3

import pytest
from PIL import Image

from src.faces.face_database import FaceDatabase, _centroid, _cosine_sim, _dec, _enc


def _make_image(path, size) -> None:
    Image.new("RGB", size, color=(128, 128, 128)).save(path)


def _base_vec(index: int, dim: int = 8) -> list[float]:
    """Vecteur quasi orthogonal aux autres index (0.01 de diaphonie) — permet de
    contrôler la similarité cosinus attendue entre "personnes" synthétiques."""
    v = [0.01] * dim
    v[index % dim] = 1.0
    return v


def _similar_vec(base: list[float], noise: float = 0.02, seed: int = 0) -> list[float]:
    import random
    rnd = random.Random(seed)
    return [b + rnd.uniform(-noise, noise) for b in base]


def _raw_insert_face(
    db, photo_path, person_id=None, cluster_id=None,
    bbox=(0, 0, 50, 50), embedding=None, ignored=0, pinned=0,
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        blob = _enc(embedding) if embedding else None
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            "  embedding, cluster_id, person_id, ignored, pinned)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (photo_path, *bbox, blob, cluster_id, person_id, ignored, pinned),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _raw_insert_picasa_annotation(
    db, photo_path, person_id, bbox=(0, 0, 50, 50), consumed=0
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO picasa_annotations"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, person_id, consumed)"
            " VALUES (?,?,?,?,?,?,?)",
            (photo_path, *bbox, person_id, consumed),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _raw_query_one(db, sql, params=()):
    conn = sqlite3.connect(db._db_path)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _raw_query_all(db, sql, params=()):
    conn = sqlite3.connect(db._db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TestThreadLocalConnection:
    def test_wal_mode_enabled(self, tmp_path):
        """FaceDatabase tournait sans WAL du tout (rollback-journal par
        défaut) : chaque écriture de l'indexeur bloquait les lectures UI."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        mode = db._conn().execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_same_thread_reuses_single_connection(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        conn1 = db._conn()
        db.get_persons_pending_count()
        assert db._conn() is conn1

    def test_two_instances_same_path_see_each_others_writes(self, tmp_path):
        db_path = tmp_path / "faces.db"
        db1 = FaceDatabase(db_path=db_path)
        db2 = FaceDatabase(db_path=db_path)
        _raw_insert_face(db1, "a.jpg")

        # db2 (autre connexion) voit l'écriture commitée via db1/_raw
        rows = db2.get_faces_for_photo("a.jpg")
        assert len(rows) == 1


class TestIndexes:
    def test_suggestion_index_exists(self, tmp_path):
        """idx_faces_suggestion évite un full scan de la table faces dans
        get_suggested_clusters_for_person / get_persons_pending_count. Créé
        après la migration qui ajoute la colonne (absente de _CREATE_FACES)."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        names = {r[1] for r in _raw_query_all(db, "PRAGMA index_list('faces')")}
        assert "idx_faces_suggestion" in names


class TestHelpers:
    def test_enc_dec_roundtrip(self):
        original = [0.5, -1.25, 3.0, 0.0]
        assert _dec(_enc(original)) == pytest.approx(original)

    def test_cosine_sim_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert _cosine_sim(v, v) == pytest.approx(1.0)

    def test_cosine_sim_orthogonal_vectors(self):
        assert _cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_centroid_is_mean(self):
        embs = [[1.0, 1.0], [3.0, 3.0]]
        assert _centroid(embs) == pytest.approx([2.0, 2.0])


class TestMergePersons:
    def test_merge_reassigns_faces_and_picasa_annotations(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        keep_id, remove_id = 100, 200
        _raw_insert_face(db, "p1.jpg", person_id=keep_id)
        face_remove = _raw_insert_face(db, "p2.jpg", person_id=remove_id)
        ann_id = _raw_insert_picasa_annotation(db, "p2.jpg", person_id=remove_id, consumed=1)

        db.merge_persons(keep_id, remove_id)

        assert _raw_query_one(db, "SELECT person_id FROM faces WHERE id=?", (face_remove,))[0] == keep_id
        assert _raw_query_one(db, "SELECT person_id FROM picasa_annotations WHERE id=?", (ann_id,))[0] == keep_id

    def test_merge_then_cleanup_orphan_person_ids_does_not_orphan_annotation(self, tmp_path):
        """Régression directe du bug 2026-07-04 : sans la réassignation des
        picasa_annotations dans merge_persons, cleanup_orphan_person_ids (appelé
        après suppression de remove_id du catalogue) supprimait silencieusement
        l'annotation restée sur l'ancien person_id."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        keep_id, remove_id = 154, 512
        _raw_insert_face(db, "photo.jpg", person_id=remove_id)
        ann_id = _raw_insert_picasa_annotation(db, "photo.jpg", person_id=remove_id, consumed=1)

        db.merge_persons(keep_id, remove_id)
        db.cleanup_orphan_person_ids(valid_person_ids={keep_id})

        row = _raw_query_one(db, "SELECT person_id FROM picasa_annotations WHERE id=?", (ann_id,))
        assert row is not None, "l'annotation a été supprimée comme orpheline"
        assert row[0] == keep_id

    def test_merge_dedups_faces_sharing_a_photo(self, tmp_path):
        """keep_id et remove_id peuvent chacun avoir un visage non-ignoré sur la
        même photo partagée — après fusion, un seul doit rester non-ignoré (le
        plus grand, cf. _dedup_in_transaction)."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        keep_id, remove_id = 1, 2
        small = _raw_insert_face(db, "shared.jpg", person_id=keep_id, bbox=(0, 0, 10, 10))
        big = _raw_insert_face(db, "shared.jpg", person_id=remove_id, bbox=(0, 0, 100, 100))

        db.merge_persons(keep_id, remove_id)

        ignored_small = _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (small,))[0]
        ignored_big = _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (big,))[0]
        assert (ignored_small, ignored_big) == (1, 0)


class TestSaveFacesAutoIgnore:
    def test_thresholds_with_foreground_face(self, tmp_path):
        """image 1000x600 : fg_qualify = max(44, 600*20%) = 120, un visage de
        130px qualifie donc la photo de "premier plan" ; le seuil effectif
        devient 130*25%=32.5 pour les autres visages de cette photo."""
        photo = tmp_path / "photo1.jpg"
        _make_image(photo, (1000, 600))
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.save_faces(str(photo), [
            {"bbox": (0, 0, 130, 130), "embedding": _base_vec(0), "det_score": 0.9},
            {"bbox": (200, 200, 40, 40), "embedding": _base_vec(1), "det_score": 0.9},
            {"bbox": (400, 400, 20, 20), "embedding": _base_vec(2), "det_score": 0.9},
        ])
        rows = dict(_raw_query_all(
            db, "SELECT bbox_w, ignored FROM faces WHERE photo_path=?",
            (os.path.normpath(str(photo)),),
        ))
        assert rows[130] == 0
        assert rows[40] == 0
        assert rows[20] == 1

    def test_low_det_score_ignored_even_if_large(self, tmp_path):
        photo = tmp_path / "photo2.jpg"
        _make_image(photo, (1000, 600))
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.save_faces(str(photo), [
            {"bbox": (0, 0, 200, 200), "embedding": _base_vec(0), "det_score": 0.4},
        ])
        row = _raw_query_one(
            db, "SELECT ignored FROM faces WHERE photo_path=?",
            (os.path.normpath(str(photo)),),
        )
        assert row[0] == 1

    def test_no_foreground_face_uses_base_threshold(self, tmp_path):
        """image 300x300 (aucun visage n'atteint fg_qualify) : seuil de base
        = max(22, 300*3%) = 22."""
        photo = tmp_path / "photo3.jpg"
        _make_image(photo, (300, 300))
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.save_faces(str(photo), [
            {"bbox": (0, 0, 25, 25), "embedding": _base_vec(0), "det_score": 0.9},
            {"bbox": (50, 50, 15, 15), "embedding": _base_vec(1), "det_score": 0.9},
        ])
        rows = dict(_raw_query_all(
            db, "SELECT bbox_w, ignored FROM faces WHERE photo_path=?",
            (os.path.normpath(str(photo)),),
        ))
        assert rows[25] == 0
        assert rows[15] == 1

    def test_missing_file_uses_fixed_fallback_threshold(self, tmp_path):
        """Si le fichier est illisible (ou absent), on retombe sur le seuil fixe
        _AUTO_IGNORE_MIN_SIDE (121 px), sans planter. Un visage par photo pour
        éviter qu'un visage au-dessus du seuil ne se qualifie lui-même comme
        "premier plan" et n'abaisse le seuil effectif pour l'autre."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")

        photo_small = str(tmp_path / "missing_small.jpg")
        db.save_faces(photo_small, [
            {"bbox": (0, 0, 100, 100), "embedding": _base_vec(0), "det_score": 0.9},
        ])
        row_small = _raw_query_one(
            db, "SELECT ignored FROM faces WHERE photo_path=?",
            (os.path.normpath(photo_small),),
        )
        assert row_small[0] == 1

        photo_big = str(tmp_path / "missing_big.jpg")
        db.save_faces(photo_big, [
            {"bbox": (0, 0, 150, 150), "embedding": _base_vec(0), "det_score": 0.9},
        ])
        row_big = _raw_query_one(
            db, "SELECT ignored FROM faces WHERE photo_path=?",
            (os.path.normpath(photo_big),),
        )
        assert row_big[0] == 0


class TestSaveFacesForceNoLimit:
    def test_preserves_person_id_by_iou_reassociation(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        _make_image(photo, (1000, 600))
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.save_faces(str(photo), [
            {"bbox": (100, 100, 130, 130), "embedding": _base_vec(0), "det_score": 0.9},
        ])
        norm_path = os.path.normpath(str(photo))
        face_id = _raw_query_one(db, "SELECT id FROM faces WHERE photo_path=?", (norm_path,))[0]
        db.assign_person_to_face(face_id, person_id=42)

        db.save_faces(str(photo), [
            {"bbox": (105, 105, 130, 130), "embedding": _base_vec(0), "det_score": 0.9},
            {"bbox": (400, 400, 10, 10), "embedding": _base_vec(3), "det_score": 0.9},
        ], force_no_limit=True)

        rows = _raw_query_all(
            db, "SELECT bbox_x, person_id, ignored FROM faces WHERE photo_path=?",
            (norm_path,),
        )
        by_x = {x: (pid, ig) for x, pid, ig in rows}
        assert by_x[105][0] == 42, "person_id non transféré par réassociation IoU"
        assert by_x[400][1] == 0, "force_no_limit doit court-circuiter l'auto-ignore"


class TestGetAllPersonCentroids:
    def test_returns_average_embedding_per_person(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", person_id=1, embedding=[1.0, 0.0])
        _raw_insert_face(db, "b.jpg", person_id=1, embedding=[3.0, 0.0])
        _raw_insert_face(db, "c.jpg", person_id=2, embedding=[0.0, 5.0])

        centroids = db.get_all_person_centroids([1, 2])

        assert centroids[1] == pytest.approx([2.0, 0.0])
        assert centroids[2] == pytest.approx([0.0, 5.0])

    def test_cache_invalidated_after_reassignment(self, tmp_path):
        """Le cache est indexé sur un fingerprint (COUNT + SUM(person_id)) — une
        réassignation qui change la somme sans changer le nombre de lignes
        (ex. merge_persons) doit tout de même invalider le cache."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", person_id=1, embedding=[1.0, 0.0])
        _raw_insert_face(db, "b.jpg", person_id=2, embedding=[0.0, 4.0])

        first = db.get_all_person_centroids([1, 2])
        assert 2 in first

        db.merge_persons(keep_id=1, remove_id=2)

        second = db.get_all_person_centroids([1])
        assert second[1] == pytest.approx([0.5, 2.0])


class TestPicasaAnnotationSync:
    def test_apply_annotations_on_next_detection(self, tmp_path):
        """save_picasa_annotations avant toute détection ArcFace crée un
        placeholder (embedding NULL) ; une détection ultérieure recouvrant la
        même zone doit reprendre l'identification sur le vrai visage."""
        photo = tmp_path / "photo.jpg"
        _make_image(photo, (1000, 600))
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        norm_path = os.path.normpath(str(photo))

        db.save_picasa_annotations(str(photo), [{"bbox": (100, 100, 130, 130), "person_id": 7}])
        placeholder = _raw_query_one(
            db, "SELECT person_id, embedding FROM faces WHERE photo_path=?", (norm_path,)
        )
        assert placeholder == (7, None)

        db.save_faces(str(photo), [
            {"bbox": (105, 105, 130, 130), "embedding": _base_vec(0), "det_score": 0.9},
        ])

        rows = _raw_query_all(db, "SELECT person_id, embedding FROM faces WHERE photo_path=?", (norm_path,))
        assert any(pid == 7 and emb is not None for pid, emb in rows), (
            "l'annotation Picasa n'a pas été reprise par la nouvelle détection"
        )

    def test_unassign_face_releases_picasa_annotation_for_retry(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        photo = os.path.normpath(str(tmp_path / "photo.jpg"))
        face_id = _raw_insert_face(db, photo, person_id=7, embedding=_base_vec(0))
        ann_id = _raw_insert_picasa_annotation(db, photo, person_id=7, consumed=1)

        db.unassign_face(face_id)

        row = _raw_query_one(db, "SELECT consumed FROM picasa_annotations WHERE id=?", (ann_id,))
        assert row[0] == 0, "l'annotation reste bloquée à consumed=1, jamais retentée"

    def test_consume_matching_annotations_after_manual_assignment(self, tmp_path):
        """assign_person_to_face doit marquer consumed=1 une annotation Picasa en
        attente si elle chevauche spatialement le visage nouvellement identifié."""
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        photo = os.path.normpath(str(tmp_path / "photo.jpg"))
        face_id = _raw_insert_face(db, photo, bbox=(100, 100, 130, 130), embedding=_base_vec(0))
        ann_id = _raw_insert_picasa_annotation(db, photo, person_id=7, bbox=(100, 100, 130, 130), consumed=0)

        db.assign_person_to_face(face_id, person_id=7)

        row = _raw_query_one(db, "SELECT consumed FROM picasa_annotations WHERE id=?", (ann_id,))
        assert row[0] == 1


class TestFaceCrud:
    def test_isolate_face_gets_unique_negative_cluster(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f1 = _raw_insert_face(db, "a.jpg", cluster_id=5)
        f2 = _raw_insert_face(db, "b.jpg", cluster_id=5)

        db.isolate_face(f1)
        db.isolate_face(f2)

        c1 = _raw_query_one(db, "SELECT cluster_id, pinned, person_id FROM faces WHERE id=?", (f1,))
        c2 = _raw_query_one(db, "SELECT cluster_id FROM faces WHERE id=?", (f2,))
        assert c1[1] == 1 and c1[2] is None
        assert c1[0] < 0 and c2[0] < 0 and c1[0] != c2[0]

    def test_delete_face_hard_deletes(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f1 = _raw_insert_face(db, "a.jpg")

        db.delete_face(f1)

        assert _raw_query_one(db, "SELECT id FROM faces WHERE id=?", (f1,)) is None

    def test_ignore_unignore_face(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f1 = _raw_insert_face(db, "a.jpg")

        db.ignore_face(f1)
        assert _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (f1,))[0] == 1

        db.unignore_face(f1)
        assert _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (f1,))[0] == 0

    def test_ignore_unignore_cluster(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", cluster_id=9)
        _raw_insert_face(db, "b.jpg", cluster_id=9)

        db.ignore_cluster(9)
        assert all(r[0] == 1 for r in _raw_query_all(db, "SELECT ignored FROM faces WHERE cluster_id=9"))

        db.unignore_cluster(9)
        assert all(r[0] == 0 for r in _raw_query_all(db, "SELECT ignored FROM faces WHERE cluster_id=9"))

    def test_delete_for_paths_purges_all_tables(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        for name in ("a.jpg", "b.jpg", "keep.jpg"):
            _raw_insert_face(db, name)
            _raw_insert_picasa_annotation(db, name, person_id=1)
        conn = sqlite3.connect(db._db_path)
        try:
            for name in ("a.jpg", "b.jpg", "keep.jpg"):
                conn.execute(
                    "INSERT INTO indexed_photos (photo_path, indexed_at) VALUES (?, 0)",
                    (name,),
                )
                conn.execute(
                    "INSERT INTO face_index_errors (photo_path, error_type, last_attempt)"
                    " VALUES (?, 'corrupt', 0)",
                    (name,),
                )
            conn.commit()
        finally:
            conn.close()

        db.delete_for_paths(["a.jpg", "b.jpg"])

        for table in ("faces", "indexed_photos", "picasa_annotations", "face_index_errors"):
            paths = {r[0] for r in _raw_query_all(db, f"SELECT photo_path FROM {table}")}
            assert paths == {"keep.jpg"}, f"table {table}"

    def test_assign_person_to_faces_batch_dedups_shared_photo(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        small = _raw_insert_face(db, "shared.jpg", bbox=(0, 0, 10, 10))
        big = _raw_insert_face(db, "shared.jpg", bbox=(0, 0, 100, 100))

        db.assign_person_to_faces([small, big], person_id=1)

        ignored_small = _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (small,))[0]
        ignored_big = _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (big,))[0]
        assert (ignored_small, ignored_big) == (1, 0)


class TestUpdateClusters:
    def test_assigns_labels_and_propagates_person_id_within_cluster(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f1 = _raw_insert_face(db, "a.jpg", person_id=1)    # déjà identifié
        f2 = _raw_insert_face(db, "b.jpg")                 # à regrouper avec f1
        f3 = _raw_insert_face(db, "c.jpg")                 # cluster différent, non identifié

        db.update_clusters([f1, f2, f3], labels=[0, 0, 1])

        row2 = _raw_query_one(db, "SELECT cluster_id, person_id FROM faces WHERE id=?", (f2,))
        row3 = _raw_query_one(db, "SELECT person_id FROM faces WHERE id=?", (f3,))
        assert row2 == (0, 1), "f2 doit hériter du cluster_id et du person_id de f1"
        assert row3[0] is None


class TestSuggestions:
    def test_find_similar_to_persons_creates_suggestion_above_threshold(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        base = _base_vec(0)
        _raw_insert_face(db, "named.jpg", person_id=1, embedding=base)
        _raw_insert_face(db, "unnamed.jpg", cluster_id=5, embedding=_similar_vec(base, noise=4))

        created, checked = db.find_similar_to_persons()

        assert (created, checked) == (1, 1)
        row = _raw_query_one(db, "SELECT suggestion_person_id FROM faces WHERE cluster_id=5")
        assert row[0] == 1

    def test_accept_cluster_suggestion_assigns_person_and_clears_flag(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f = _raw_insert_face(db, "a.jpg", cluster_id=5)
        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "UPDATE faces SET suggestion_person_id=1, suggestion_score=0.8 WHERE id=?", (f,)
        )
        conn.commit()
        conn.close()

        db.accept_cluster_suggestion(5)

        row = _raw_query_one(db, "SELECT person_id, suggestion_person_id FROM faces WHERE id=?", (f,))
        assert row == (1, None)

    def test_resuggest_clusters_excludes_person(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        base = _base_vec(0)
        _raw_insert_face(db, "p1.jpg", person_id=1, embedding=base)
        _raw_insert_face(db, "p2.jpg", person_id=2, embedding=_similar_vec(base, noise=0.5, seed=1))
        _raw_insert_face(db, "unnamed.jpg", cluster_id=9, embedding=_similar_vec(base, noise=0.5, seed=2))

        db.resuggest_clusters([9], exclude_person_id=1)

        row = _raw_query_one(db, "SELECT suggestion_person_id FROM faces WHERE cluster_id=9")
        assert row[0] == 2, "la personne exclue ne doit jamais être suggérée"


class TestIndexErrorTracking:
    def test_get_paths_to_index_excludes_indexed_errored_and_videos(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "INSERT INTO indexed_photos (photo_path, indexed_at) VALUES (?,?)",
            (os.path.normpath("a.jpg"), 0.0),
        )
        conn.commit()
        conn.close()
        db.mark_index_error("b.jpg", "timeout")

        result = db.get_paths_to_index(["a.jpg", "b.jpg", "c.mp4", "d.jpg"])

        assert result == ["d.jpg"]

    def test_mark_index_error_then_get_index_error(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.mark_index_error("a.jpg", "timeout")

        info = db.get_index_error("a.jpg")

        assert info["error_type"] == "timeout"
        assert info["excluded"] is False

    def test_mark_index_error_updates_existing_row_instead_of_duplicating(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.mark_index_error("a.jpg", "timeout")
        db.mark_index_error("a.jpg", "crash")

        rows = _raw_query_all(
            db, "SELECT error_type FROM face_index_errors WHERE photo_path=?",
            (os.path.normpath("a.jpg"),),
        )
        assert len(rows) == 1
        assert rows[0][0] == "crash"

    def test_clear_index_error_removes_row(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.mark_index_error("a.jpg", "timeout")

        db.clear_index_error("a.jpg")

        assert db.get_index_error("a.jpg") is None

    def test_get_error_paths_excludes_excluded_by_default(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.mark_index_error("a.jpg", "timeout")
        db.mark_index_error("b.jpg", "timeout")
        db.set_index_excluded("b.jpg", True)

        assert db.get_error_paths() == [os.path.normpath("a.jpg")]
        assert set(db.get_error_paths(include_excluded=True)) == {
            os.path.normpath("a.jpg"), os.path.normpath("b.jpg"),
        }

    def test_set_index_excluded_creates_row_if_missing(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        db.set_index_excluded("a.jpg", True)

        info = db.get_index_error("a.jpg")
        assert info["excluded"] is True

    def test_get_indexed_rotation_default_zero_and_after_insert(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        assert db.get_indexed_rotation("never_indexed.jpg") == 0

        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "INSERT INTO indexed_photos (photo_path, indexed_at, rotation) VALUES (?,?,?)",
            (os.path.normpath("a.jpg"), 0.0, 90),
        )
        conn.commit()
        conn.close()

        assert db.get_indexed_rotation("a.jpg") == 90


class TestClusterGetters:
    def test_get_clusters_ordered_by_size_desc(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", cluster_id=1)
        _raw_insert_face(db, "b.jpg", cluster_id=2)
        _raw_insert_face(db, "c.jpg", cluster_id=2)
        _raw_insert_face(db, "d.jpg", cluster_id=2)

        assert db.get_clusters() == [(2, 3), (1, 1)]

    def test_get_unnamed_clusters_excludes_named_ignored_pinned_suggested(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "unnamed.jpg", cluster_id=1)
        _raw_insert_face(db, "named.jpg", cluster_id=2, person_id=5)
        _raw_insert_face(db, "ignored.jpg", cluster_id=3, ignored=1)
        _raw_insert_face(db, "pinned.jpg", cluster_id=4, pinned=1)
        suggested_id = _raw_insert_face(db, "suggested.jpg", cluster_id=6)
        conn = sqlite3.connect(db._db_path)
        conn.execute("UPDATE faces SET suggestion_person_id=9 WHERE id=?", (suggested_id,))
        conn.commit()
        conn.close()

        assert db.get_unnamed_clusters() == [(1, 1)]

    def test_get_faces_for_photo_returns_face_info(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", bbox=(1, 2, 3, 4), person_id=5)

        faces = db.get_faces_for_photo("a.jpg")

        assert len(faces) == 1
        assert (faces[0].bbox_x, faces[0].bbox_y, faces[0].bbox_w, faces[0].bbox_h) == (1, 2, 3, 4)
        assert faces[0].person_id == 5

    def test_get_faces_for_photo_returns_suggestion_fields(self, tmp_path):
        # Non-régression : get_faces_for_photo ignorait suggestion_person_id/score,
        # empêchant le panel de visages d'afficher les suggestions en attente.
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        face_id = _raw_insert_face(db, "a.jpg", cluster_id=5)
        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "UPDATE faces SET suggestion_person_id=7, suggestion_score=0.65 WHERE id=?",
            (face_id,),
        )
        conn.commit()
        conn.close()

        faces = db.get_faces_for_photo("a.jpg")

        assert faces[0].suggestion_person_id == 7
        assert faces[0].suggestion_score == pytest.approx(0.65)

    def test_get_photos_for_cluster_excludes_ignored(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "visible.jpg", cluster_id=1)
        _raw_insert_face(db, "hidden.jpg", cluster_id=1, ignored=1)

        assert db.get_photos_for_cluster(1) == [os.path.normpath("visible.jpg")]

    def test_get_clusters_for_person_ordered_by_photo_count(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", cluster_id=1, person_id=5)
        _raw_insert_face(db, "b.jpg", cluster_id=2, person_id=5)
        _raw_insert_face(db, "c.jpg", cluster_id=2, person_id=5)

        assert db.get_clusters_for_person(5) == [(2, 2), (1, 1)]

    def test_get_photos_for_person(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", person_id=5)
        _raw_insert_face(db, "b.jpg", person_id=5)
        _raw_insert_face(db, "c.jpg", person_id=6)

        assert set(db.get_photos_for_person(5)) == {
            os.path.normpath("a.jpg"), os.path.normpath("b.jpg"),
        }

    def test_get_faces_for_person(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", person_id=5, bbox=(0, 0, 10, 10))
        _raw_insert_face(db, "b.jpg", person_id=6)

        faces = db.get_faces_for_person(5)

        assert len(faces) == 1
        assert faces[0].photo_path == os.path.normpath("a.jpg")

    def test_get_faces_by_cluster(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", cluster_id=7)
        _raw_insert_face(db, "b.jpg", cluster_id=8)

        faces = db.get_faces_by_cluster(7)

        assert len(faces) == 1
        assert faces[0].photo_path == os.path.normpath("a.jpg")

    def test_merge_clusters_moves_all_faces(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f1 = _raw_insert_face(db, "a.jpg", cluster_id=1)
        f2 = _raw_insert_face(db, "b.jpg", cluster_id=2)

        db.merge_clusters(source_cluster_id=1, target_cluster_id=2)

        assert _raw_query_one(db, "SELECT cluster_id FROM faces WHERE id=?", (f1,))[0] == 2
        assert _raw_query_one(db, "SELECT cluster_id FROM faces WHERE id=?", (f2,))[0] == 2

    def test_unassign_person_from_cluster_clears_only_target_person(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f1 = _raw_insert_face(db, "a.jpg", cluster_id=1, person_id=5)
        f2 = _raw_insert_face(db, "b.jpg", cluster_id=1, person_id=6)

        db.unassign_person_from_cluster(person_id=5, cluster_id=1)

        assert _raw_query_one(db, "SELECT person_id FROM faces WHERE id=?", (f1,))[0] is None
        assert _raw_query_one(db, "SELECT person_id FROM faces WHERE id=?", (f2,))[0] == 6

    def test_unassign_person_clears_all_faces(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        f1 = _raw_insert_face(db, "a.jpg", person_id=5)
        f2 = _raw_insert_face(db, "b.jpg", person_id=5)

        db.unassign_person(5)

        assert _raw_query_one(db, "SELECT person_id FROM faces WHERE id=?", (f1,))[0] is None
        assert _raw_query_one(db, "SELECT person_id FROM faces WHERE id=?", (f2,))[0] is None

    def test_get_stats(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", cluster_id=1, person_id=5)
        _raw_insert_face(db, "b.jpg", cluster_id=2)
        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "INSERT INTO indexed_photos (photo_path, indexed_at) VALUES (?,?)",
            (os.path.normpath("a.jpg"), 0.0),
        )
        conn.commit()
        conn.close()

        stats = db.get_stats()

        assert stats == {
            "indexed_photos": 1, "total_faces": 2, "named_persons": 1, "clusters": 2,
        }

    def test_get_recognition_counters(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", person_id=1, embedding=_base_vec(0))          # identified + recognized
        _raw_insert_face(db, "b.jpg", person_id=1, ignored=1)                       # ignored, not identified
        pending_id = _raw_insert_face(db, "c.jpg", cluster_id=5)                    # pending suggestion
        _raw_insert_face(db, "d.jpg", embedding=_base_vec(1))                       # unknown
        conn = sqlite3.connect(db._db_path)
        conn.execute("UPDATE faces SET suggestion_person_id=2 WHERE id=?", (pending_id,))
        conn.commit()
        conn.close()
        _raw_insert_picasa_annotation(db, "a.jpg", person_id=1, consumed=1)
        _raw_insert_picasa_annotation(db, "e.jpg", person_id=3, consumed=0)

        counters = db.get_recognition_counters()

        assert counters["total_faces"] == 4
        assert counters["ignored_faces"] == 1
        assert counters["identified_faces"] == 1
        assert counters["recognized_faces"] == 1
        assert counters["pending_faces"] == 1
        assert counters["unknown_faces"] == 1
        assert counters["picasa_total"] == 2
        assert counters["picasa_merged"] == 1
        assert counters["picasa_placeholder"] == 1


class TestCleanupFamily:
    def test_cleanup_overlapping_placeholders_transfers_person_id_and_deletes(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        real_id = _raw_insert_face(db, "a.jpg", bbox=(100, 100, 50, 50), embedding=_base_vec(0))
        placeholder_id = _raw_insert_face(db, "a.jpg", bbox=(105, 105, 50, 50), person_id=7)

        deleted = db.cleanup_overlapping_placeholders()

        assert deleted == 1
        assert _raw_query_one(db, "SELECT id FROM faces WHERE id=?", (placeholder_id,)) is None
        assert _raw_query_one(db, "SELECT person_id FROM faces WHERE id=?", (real_id,))[0] == 7

    def test_cleanup_overlapping_placeholders_conflict_is_left_untouched(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", bbox=(100, 100, 50, 50), person_id=1, embedding=_base_vec(0))
        placeholder_id = _raw_insert_face(db, "a.jpg", bbox=(105, 105, 50, 50), person_id=2)

        deleted = db.cleanup_overlapping_placeholders()

        assert deleted == 0
        assert _raw_query_one(db, "SELECT id FROM faces WHERE id=?", (placeholder_id,)) is not None

    def test_restore_orphaned_ignored_faces_reactivates_largest(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        small = _raw_insert_face(db, "a.jpg", person_id=5, bbox=(0, 0, 10, 10), ignored=1)
        big = _raw_insert_face(db, "a.jpg", person_id=5, bbox=(0, 0, 100, 100), ignored=1)

        n = db.restore_orphaned_ignored_faces()

        assert n == 1
        assert _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (big,))[0] == 0
        assert _raw_query_one(db, "SELECT ignored FROM faces WHERE id=?", (small,))[0] == 1

    def test_cleanup_stale_placeholder_faces_removes_orphan(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        stale_id = _raw_insert_face(db, "a.jpg", person_id=99)
        _raw_insert_picasa_annotation(db, "a.jpg", person_id=1)

        n = db.cleanup_stale_placeholder_faces()

        assert n == 1
        assert _raw_query_one(db, "SELECT id FROM faces WHERE id=?", (stale_id,)) is None

    def test_cleanup_stale_placeholder_faces_keeps_matching_placeholder(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        kept_id = _raw_insert_face(db, "a.jpg", person_id=1)
        _raw_insert_picasa_annotation(db, "a.jpg", person_id=1)

        n = db.cleanup_stale_placeholder_faces()

        assert n == 0
        assert _raw_query_one(db, "SELECT id FROM faces WHERE id=?", (kept_id,)) is not None

    def test_assign_person_synthetic_clusters_migrates_identified_faces(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        identified = _raw_insert_face(db, "a.jpg", person_id=7, cluster_id=3)
        unidentified = _raw_insert_face(db, "b.jpg", cluster_id=3)

        n = db.assign_person_synthetic_clusters()

        assert n == 1
        assert _raw_query_one(db, "SELECT cluster_id FROM faces WHERE id=?", (identified,))[0] == 10_000_007
        assert _raw_query_one(db, "SELECT cluster_id FROM faces WHERE id=?", (unidentified,))[0] == 3


class TestZeroRowCommitDoesNotLeaveOpenTransaction:
    """Régression : restore_orphaned_ignored_faces / cleanup_stale_placeholder_faces /
    assign_person_synthetic_clusters / cleanup_orphan_person_ids ne committaient
    l'UPDATE/DELETE que si rowcount>0 (`if n: conn.commit()`). Un UPDATE/DELETE qui
    ne touche aucune ligne ouvre quand même une transaction (BEGIN implicite du
    module sqlite3 dès le premier DML) — sauter le commit laissait la connexion
    thread-local dans une transaction ouverte indéfiniment. Le verrou Python
    (_guard()) n'empêche pas ce bug : il protège l'exécution concurrente du code,
    pas la clôture de la transaction SQLite sous-jacente une fois le verrou relâché.

    Découvert via test_folder_management.py (e2e) : le clustering déclenché après
    un FaceIndexThread qui ne trouve aucun visage (n_identified=0, donc rowcount=0
    dans assign_person_synthetic_clusters) laissait la connexion du ClusterThread
    bloquée en transaction ouverte — le FaceIndexThread suivant (re-scan requeue)
    plantait alors sur son propre save_faces() avec
    `sqlite3.OperationalError: database is locked` après expiration du busy_timeout
    (5 s), cf. CLAUDE.md pattern de connexion."""

    def test_restore_orphaned_ignored_faces_zero_rows_commits(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")

        n = db.restore_orphaned_ignored_faces()

        assert n == 0
        assert db._conn().in_transaction is False

    def test_cleanup_stale_placeholder_faces_zero_rows_commits(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")

        n = db.cleanup_stale_placeholder_faces()

        assert n == 0
        assert db._conn().in_transaction is False

    def test_assign_person_synthetic_clusters_zero_rows_commits(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg")  # aucun person_id -> rowcount=0

        n = db.assign_person_synthetic_clusters()

        assert n == 0
        assert db._conn().in_transaction is False

    def test_cleanup_orphan_person_ids_zero_rows_commits(self, tmp_path):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        _raw_insert_face(db, "a.jpg", person_id=1)

        n_faces, n_ann = db.cleanup_orphan_person_ids(valid_person_ids={1})

        assert (n_faces, n_ann) == (0, 0)
        assert db._conn().in_transaction is False

    def test_zero_row_commit_does_not_block_a_second_thread_writer(self, tmp_path):
        """Reproduction directe du scénario e2e : un thread appelle une méthode à
        rowcount=0, un AUTRE thread (donc une AUTRE connexion sqlite thread-local)
        doit pouvoir écrire immédiatement après, sans attendre le busy_timeout."""
        import threading

        db = FaceDatabase(db_path=tmp_path / "faces.db")

        def _run_zero_row_method():
            db.assign_person_synthetic_clusters()

        t = threading.Thread(target=_run_zero_row_method)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()

        def _write_from_other_thread():
            _raw_insert_face(db, "b.jpg")

        writer = threading.Thread(target=_write_from_other_thread)
        writer.start()
        writer.join(timeout=5)
        assert not writer.is_alive(), (
            "l'écriture depuis un autre thread est restée bloquée : la connexion "
            "du premier thread a probablement laissé une transaction ouverte"
        )
        assert _raw_query_one(db, "SELECT COUNT(*) FROM faces WHERE photo_path=?", ("b.jpg",))[0] == 1
