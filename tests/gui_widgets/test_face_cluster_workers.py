# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) for face_cluster_workers -- Union-Find of the similar
clusters, vectorised suggestions and loading threads. The QThreads are run
through a synchronous run() so that coverage traces the code (cf. CLAUDE.md);
FaceDatabase/Catalog are real, seeded in process."""
import math
import sqlite3

import pytest

from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase, _enc
from src.library.catalog import Catalog
from src.ui.face_cluster_workers import (
    _AnalysisCancelled, _ClusterRefreshThread, _PersonsLoader,
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
        # 1 and 2 identical (sim 1.0 >= 0.72), 3 orthogonal (sim 0)
        embs = {1: _emb(0.0), 2: _emb(0.0), 3: _emb(math.pi / 2)}

        groups = _compute_cluster_groups_bg([1, 2, 3], embs)

        merged = [sorted(g) for g in groups.values()]
        assert sorted(merged) == [[1, 2], [3]]

    def test_below_threshold_stays_separate(self):
        # sim ~ 0.5 < _SIM_GROUP (0.72)
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
        assert calls  # at least one block announced

    def test_empty_input(self):
        assert _compute_cluster_groups_bg([], {}) == {}

    def test_cancellation_absorbed_returns_partial_result(self):
        # uf_progress raises _AnalysisCancelled on the very first block (user
        # cancellation) -- the function swallows it internally and returns the merges
        # already found (here: none) instead of letting the exception propagate to the
        # caller, so that the thread can finish and deliver a partial result (cf. the
        # Cancel button).
        def cb(chunk_start):
            raise _AnalysisCancelled()

        groups = _compute_cluster_groups_bg(
            [1, 2], {1: _emb(0.0), 2: _emb(0.0)}, progress_cb=cb
        )

        assert sorted(groups.values()) == [[1], [2]]

    def test_partial_unions_preserved_when_cancelled_mid_computation(self):
        # 500 identical vectors (angle 0) followed by 100 vectors identical to each
        # other but orthogonal to the first 500. The first block (chunk_start=0) already
        # compares every i<500 against every j>i through BLAS (including the next 100):
        # the first 500 therefore all end up merged into a single group as of that
        # block. The last 100, on the other hand, are only compared with each other in
        # the second block (chunk_start=500) -- if the cancellation happens just before
        # that block, they must stay singletons: the merge already found for the first
        # 500 is never undone by the early stop.
        ids = list(range(600))
        embeddings = {cid: _emb(0.0) for cid in range(500)}
        embeddings.update({cid: _emb(math.pi / 2) for cid in range(500, 600)})

        def cb(chunk_start):
            if chunk_start >= 500:
                raise _AnalysisCancelled()

        groups = _compute_cluster_groups_bg(ids, embeddings, progress_cb=cb)

        merged = [g for g in groups.values() if len(g) > 1]
        assert len(merged) == 1
        assert sorted(merged[0]) == list(range(500))
        for cid in range(500, 600):
            assert groups[cid] == [cid]


# ---------------------------------------------------------------------------
# suggestions

class TestComputeSuggestion:
    def _persons_embs(self, sim_alice: float):
        persons = [_person(1, "Alice"), _person(2, "Boris")]
        # Boris on the e2 axis, orthogonal to the (e0, e1) plane where Alice and the
        # cluster live: his similarity with the cluster is always zero.
        boris = [0.0] * 8
        boris[2] = 1.0
        embs = {
            1: {99: _emb(0.0)},   # Alice on e0
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
            10: _emb(math.acos(0.90)),   # strong -> ~ blue
            11: _emb(math.acos(0.60)),   # medium -> ~ grey
            12: _emb(math.pi / 2),       # zero -> none
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
# _ClusterRefreshThread (synchronous run())

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
        assert any("No group to analyse" in m for m in col.progress)

    def test_two_phases_and_union_find_grouping(self, qtbot, tmp_path, en_catalogue):
        face_db, catalog = self._dbs(tmp_path)
        # Clusters 1 and 2: 2 faces each, identical centroids -> grouped.
        # Cluster 3: 2 faces, orthogonal -> separate.
        for cid, angle in [(1, 0.0), (2, 0.0), (3, math.pi / 2)]:
            for k in range(2):
                _raw_insert_face(face_db, f"C:/p{cid}_{k}.jpg",
                                 cluster_id=cid, embedding=_emb(angle))
        t = _ClusterRefreshThread(face_db, catalog)
        col = _Collector(t)

        t.run()

        # Phase 1: flat structure marked as partial
        assert col.initial[0]["is_partial"] is True
        assert sorted(g[0] for g in col.initial[0]["groups_sorted"]) == [1, 2, 3]
        # Phase 2: 1 and 2 merged, "same person" label
        final = col.final[0]
        assert final["is_partial"] is False
        merged = [sorted(g) for g in final["groups_sorted"]]
        assert [1, 2] in merged and [3] in merged
        root12 = next(g[0] for g in final["groups_sorted"] if len(g) == 2)
        label, color = final["group_labels"][root12]
        assert "the same person" in label and "2 groups" in label
        assert color == "#7aabdb"   # sim 100 % >= _SIM_STRONG

    def test_auto_promotion_filters_suggested_cluster(self, qtbot, tmp_path):
        face_db, catalog = self._dbs(tmp_path)
        alice = catalog.create_person("Alice")
        _raw_insert_face(face_db, "C:/ref.jpg", cluster_id=99,
                         person_id=alice.id, embedding=_emb(0.0))
        # Cluster 1: sim 0.60 with Alice -- >= _SIM_SUGGEST (0.55), < auto-assign
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
        # In the database: suggestion persisted, no assignment (score < 0.70)
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
        # 1 face alone (face_count == 1): excluded from the Union-Find, singleton
        _raw_insert_face(face_db, "C:/solo.jpg", cluster_id=5, embedding=_emb(0.0))
        t = _ClusterRefreshThread(face_db, catalog)
        col = _Collector(t)

        t.run()

        assert col.final[0]["groups_sorted"] == [[5]]
        assert col.final[0]["group_labels"][5] == ("", "")

    def test_cancel_stops_before_data_ready(self, qtbot, tmp_path):
        """Cancelling the popup calls cancel(): the thread must stop without ever
        emitting data_ready (cf. the Cancel button)."""
        face_db, catalog = self._dbs(tmp_path)
        for cid, angle in [(1, 0.0), (2, 0.0), (3, math.pi / 2)]:
            for k in range(2):
                _raw_insert_face(face_db, f"C:/p{cid}_{k}.jpg",
                                 cluster_id=cid, embedding=_emb(angle))
        t = _ClusterRefreshThread(face_db, catalog)
        col = _Collector(t)
        t.cancel()

        t.run()

        assert col.initial  # phase 1 already emitted before the first checkpoint
        assert col.final == []


# ---------------------------------------------------------------------------
# _PersonsLoader (synchronous run())

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
