# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) pour face_cluster_workers — Union-Find des clusters
similaires, suggestions vectorisées et threads de chargement. Les QThread sont
exécutés via run() synchrone pour que coverage trace le code (cf. CLAUDE.md) ;
FaceDatabase/Catalog sont réels, semés en process."""
import math
import sqlite3

import pytest

from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
from src.ui.face_cluster_workers import (
    _ClusterRefreshThread, _PersonsLoader,
    _compute_all_suggestions_bg, _compute_cluster_groups_bg,
    _compute_suggestion_bg,
)


def _emb(angle_rad: float, dim: int = 8) -> list[float]:
    vec = [0.0] * dim
    vec[0] = math.cos(angle_rad)
    vec[1] = math.sin(angle_rad)
    return vec


def _raw_insert_face(
    db, photo_path, cluster_id=None, person_id=None,
    bbox=(10, 10, 50, 50), embedding=None,
) -> int:
    conn = sqlite3.connect(db._db_path)
    try:
        cur = conn.execute(
            "INSERT INTO faces"
            " (photo_path, bbox_x, bbox_y, bbox_w, bbox_h,"
            "  cluster_id, person_id, embedding)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                photo_path, *bbox, cluster_id, person_id,
                _enc(embedding) if embedding is not None else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _person(pid, name) -> PersonInfo:
    return PersonInfo(name=name, id=pid)


# ---------------------------------------------------------------------------
# Union-Find

class TestComputeClusterGroups:
    def test_similar_clusters_are_grouped(self):
        # 1 et 2 identiques (sim 1.0 ≥ 0.72), 3 orthogonal (sim 0)
        embs = {1: _emb(0.0), 2: _emb(0.0), 3: _emb(math.pi / 2)}

        groups = _compute_cluster_groups_bg([1, 2, 3], embs)

        merged = [sorted(g) for g in groups.values()]
        assert sorted(merged) == [[1, 2], [3]]

    def test_below_threshold_stays_separate(self):
        # sim ≈ 0.5 < _SIM_GROUP (0.72)
        embs = {1: _emb(0.0), 2: _emb(math.acos(0.5))}

        groups = _compute_cluster_groups_bg([1, 2], embs)

        assert sorted(len(g) for g in groups.values()) == [1, 1]

    def test_cluster_without_embedding_is_singleton(self):
        groups = _compute_cluster_groups_bg([1, 2], {1: _emb(0.0)})

        assert sorted(sorted(g) for g in groups.values()) == [[1], [2]]

    def test_progress_callback_invoked(self):
        calls = []
        _compute_cluster_groups_bg(
            [1, 2], {1: _emb(0.0), 2: _emb(0.0)}, progress_cb=calls.append
        )
        assert calls  # au moins un bloc annoncé

    def test_empty_input(self):
        assert _compute_cluster_groups_bg([], {}) == {}


# ---------------------------------------------------------------------------
# suggestions

class TestComputeSuggestion:
    def _persons_embs(self, sim_alice: float):
        persons = [_person(1, "Alice"), _person(2, "Boris")]
        # Boris sur l'axe e2, orthogonal au plan (e0, e1) où vivent Alice et le
        # cluster : sa similarité avec le cluster est toujours nulle.
        boris = [0.0] * 8
        boris[2] = 1.0
        embs = {
            1: {99: _emb(0.0)},   # Alice sur e0
            2: {98: boris},
        }
        cluster_embs = {7: _emb(math.acos(sim_alice))}
        return persons, embs, cluster_embs

    def test_strong_match_blue_label(self):
        persons, p_embs, c_embs = self._persons_embs(0.90)

        pid, label, color = _compute_suggestion_bg(7, c_embs, persons, p_embs)

        assert pid == 1
        assert label == "≈ Alice (90 %)"
        assert color == "#7aabdb"

    def test_medium_match_gray_label(self):
        persons, p_embs, c_embs = self._persons_embs(0.60)

        pid, label, color = _compute_suggestion_bg(7, c_embs, persons, p_embs)

        assert pid == 1
        assert label.startswith("~ Alice")
        assert color == "#888"

    def test_below_weak_threshold_returns_none(self):
        persons, p_embs, c_embs = self._persons_embs(0.30)

        assert _compute_suggestion_bg(7, c_embs, persons, p_embs) == (None, "", "")

    def test_no_person_embeddings(self):
        assert _compute_suggestion_bg(7, {7: _emb(0.0)}, [], {}) == (None, "", "")

    def test_missing_cluster_embedding(self):
        persons, p_embs, _ = self._persons_embs(0.9)
        assert _compute_suggestion_bg(7, {}, persons, p_embs) == (None, "", "")


class TestComputeAllSuggestions:
    def test_vectorized_matches_expected_tiers(self):
        persons = [_person(1, "Alice")]
        p_embs = {1: {99: _emb(0.0)}}
        c_embs = {
            10: _emb(math.acos(0.90)),   # fort → ≈ bleu
            11: _emb(math.acos(0.60)),   # moyen → ~ gris
            12: _emb(math.pi / 2),       # nul → aucun
        }

        res = _compute_all_suggestions_bg([10, 11, 12], c_embs, persons, p_embs)

        pid, label, color, score = res[10]
        assert (pid, color) == (1, "#7aabdb")
        assert label == "≈ Alice (90 %)" and score == pytest.approx(0.90, abs=0.01)
        pid, label, color, score = res[11]
        assert (pid, color) == (1, "#888")
        assert label.startswith("~ Alice")
        assert res[12] == (None, "", "", 0.0)

    def test_no_persons_returns_empty_entries(self):
        res = _compute_all_suggestions_bg([1], {1: _emb(0.0)}, [], {})
        assert res == {1: (None, "", "", 0.0)}

    def test_cluster_without_embedding_kept_empty(self):
        persons = [_person(1, "Alice")]
        res = _compute_all_suggestions_bg([1], {}, persons, {1: {9: _emb(0.0)}})
        assert res == {1: (None, "", "", 0.0)}


# ---------------------------------------------------------------------------
# _ClusterRefreshThread (run() synchrone)

class _Collector:
    def __init__(self, thread):
        self.initial = []
        self.final = []
        self.progress = []
        thread.initial_ready.connect(self.initial.append)
        thread.data_ready.connect(self.final.append)
        thread.progress.connect(lambda a, b, msg: self.progress.append(msg))


class TestClusterRefreshThread:
    def _dbs(self, tmp_path):
        return (FaceDatabase(db_path=tmp_path / "faces.db"),
                Catalog(db_path=tmp_path / "catalog.db"))

    def test_empty_db_emits_empty_structures(self, qtbot, tmp_path):
        face_db, catalog = self._dbs(tmp_path)
        t = _ClusterRefreshThread(face_db, catalog)
        col = _Collector(t)

        t.run()

        assert col.initial[0]["groups_sorted"] == []
        assert col.final[0]["groups_sorted"] == []
        assert any("Aucun groupe" in m for m in col.progress)

    def test_two_phases_and_union_find_grouping(self, qtbot, tmp_path):
        face_db, catalog = self._dbs(tmp_path)
        # Clusters 1 et 2 : 2 visages chacun, centroïdes identiques → regroupés.
        # Cluster 3 : 2 visages, orthogonal → séparé.
        for cid, angle in [(1, 0.0), (2, 0.0), (3, math.pi / 2)]:
            for k in range(2):
                _raw_insert_face(face_db, f"C:/p{cid}_{k}.jpg",
                                 cluster_id=cid, embedding=_emb(angle))
        t = _ClusterRefreshThread(face_db, catalog)
        col = _Collector(t)

        t.run()

        # Phase 1 : structure plate marquée partielle
        assert col.initial[0]["is_partial"] is True
        assert sorted(g[0] for g in col.initial[0]["groups_sorted"]) == [1, 2, 3]
        # Phase 2 : 1 et 2 fusionnés, étiquette "même personne"
        final = col.final[0]
        assert final["is_partial"] is False
        merged = [sorted(g) for g in final["groups_sorted"]]
        assert [1, 2] in merged and [3] in merged
        root12 = next(g[0] for g in final["groups_sorted"] if len(g) == 2)
        label, color = final["group_labels"][root12]
        assert "même personne" in label and "2 groupes" in label
        assert color == "#7aabdb"   # sim 100 % ≥ _SIM_STRONG

    def test_auto_promotion_filters_suggested_cluster(self, qtbot, tmp_path):
        face_db, catalog = self._dbs(tmp_path)
        alice = catalog.create_person("Alice")
        _raw_insert_face(face_db, "C:/ref.jpg", cluster_id=99,
                         person_id=alice.id, embedding=_emb(0.0))
        # Cluster 1 : sim 0.60 avec Alice — ≥ _SIM_SUGGEST (0.55), < auto-assign
        for k in range(2):
            _raw_insert_face(face_db, f"C:/c1_{k}.jpg", cluster_id=1,
                             embedding=_emb(math.acos(0.60)))
        t = _ClusterRefreshThread(face_db, catalog)
        col = _Collector(t)

        t.run()

        final = col.final[0]
        assert final["n_promoted"] == 1
        assert 1 not in final["face_counts"]
        assert final["groups_sorted"] == []
        # En base : suggestion persistée, pas d'allocation (score < 0.70)
        conn = sqlite3.connect(face_db._db_path)
        try:
            rows = conn.execute(
                "SELECT person_id, suggestion_person_id FROM faces WHERE cluster_id=1"
            ).fetchall()
        finally:
            conn.close()
        assert rows and all(r == (None, alice.id) for r in rows)

    def test_isolated_cluster_stays_singleton(self, qtbot, tmp_path):
        face_db, catalog = self._dbs(tmp_path)
        # 1 visage seul (face_count == 1) : exclu de l'Union-Find, singleton
        _raw_insert_face(face_db, "C:/solo.jpg", cluster_id=5, embedding=_emb(0.0))
        t = _ClusterRefreshThread(face_db, catalog)
        col = _Collector(t)

        t.run()

        assert col.final[0]["groups_sorted"] == [[5]]
        assert col.final[0]["group_labels"][5] == ("", "")


# ---------------------------------------------------------------------------
# _PersonsLoader (run() synchrone)

class TestPersonsLoader:
    def test_loads_persons_and_suggests_best_match(self, qtbot, tmp_path):
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        alice = catalog.create_person("Alice")
        _raw_insert_face(face_db, "C:/c7.jpg", cluster_id=7,
                         embedding=_emb(math.acos(0.80)))
        persons_snap = [PersonInfo(name="Alice", id=alice.id)]
        emb_snap = {alice.id: {99: _emb(0.0)}}
        loader = _PersonsLoader(
            catalog, face_db, cluster_ids=[7],
            persons_snap=persons_snap, emb_snap=emb_snap,
        )
        results = []
        loader.ready.connect(lambda persons, sugg: results.append((persons, sugg)))

        loader.run()

        persons, suggested = results[0]
        assert [p.name for p in persons] == ["Alice"]
        assert suggested == alice.id

    def test_below_weak_threshold_no_suggestion(self, qtbot, tmp_path):
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        alice = catalog.create_person("Alice")
        _raw_insert_face(face_db, "C:/c7.jpg", cluster_id=7,
                         embedding=_emb(math.acos(0.20)))
        loader = _PersonsLoader(
            catalog, face_db, cluster_ids=[7],
            persons_snap=[PersonInfo(name="Alice", id=alice.id)],
            emb_snap={alice.id: {99: _emb(0.0)}},
        )
        results = []
        loader.ready.connect(lambda persons, sugg: results.append(sugg))

        loader.run()

        assert results == [None]

    def test_without_cluster_ids_returns_persons_only(self, qtbot, tmp_path):
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.create_person("Alice")
        loader = _PersonsLoader(catalog, face_db)
        results = []
        loader.ready.connect(lambda persons, sugg: results.append((persons, sugg)))

        loader.run()

        persons, suggested = results[0]
        assert len(persons) == 1 and suggested is None
