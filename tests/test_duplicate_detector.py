# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste le vrai algorithme de détection de doublons (Tier 1 pHash + Tier 2
ORB/RANSAC) en appelant `_detect()` directement, sans passer par `.start()`
(pas de vrai thread ni de bus d'événements ici — voir
test_signal_object_cross_thread.py pour la régression spécifique au
franchissement du QThread)."""
from src.library.duplicate_detector import DuplicateDetectorThread, _merge
from tools.test_env.generate_library import build_library


class TestDetectRealLibrary:
    def _run(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        # str, pas Path : c'est ce que le catalogue fournit en usage réel, et
        # _load_gray() (Tier 2) échoue silencieusement sur un objet Path
        # (`path.encode("ascii")` n'existe pas sur Path, capturé par un except
        # Exception large -> image traitée comme illisible).
        thread = DuplicateDetectorThread([str(p) for p in manifest.images])

        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()

        assert "groups" in received, "finished n'a pas été émis"
        return manifest, received["groups"]

    def test_exact_duplicate_pair_grouped(self, tmp_path):
        manifest, groups = self._run(tmp_path)
        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert a in members_by_path and b in members_by_path
        assert members_by_path[a] == members_by_path[b]

    def test_resized_duplicate_pair_grouped(self, tmp_path):
        manifest, groups = self._run(tmp_path)
        a, b = (str(p) for p in manifest.resized_duplicate_pair)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert a in members_by_path and b in members_by_path
        assert members_by_path[a] == members_by_path[b]

    def test_crop_duplicate_pair_grouped_by_tier2(self, tmp_path):
        """La paire recadrée ne doit PAS matcher au Tier 1 (pHash) — c'est le
        Tier 2 (ORB/RANSAC) qui doit la grouper. Preuve que Tier 2 tourne bien."""
        manifest, groups = self._run(tmp_path)
        a, b = (str(p) for p in manifest.crop_duplicate_pair)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert a in members_by_path and b in members_by_path
        assert members_by_path[a] == members_by_path[b]

    def test_control_photos_not_grouped(self, tmp_path):
        manifest, groups = self._run(tmp_path)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        for control in manifest.control_photos:
            assert str(control) not in members_by_path

    def test_corrupted_file_reported_not_grouped(self, tmp_path):
        manifest, groups = self._run(tmp_path)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert str(manifest.corrupted_file) not in members_by_path

    def test_no_photos_emits_empty_dict(self, tmp_path):
        thread = DuplicateDetectorThread([])
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()
        assert received["groups"] == {}


class TestMergeUnionFind:
    def test_merge_two_new_paths_creates_group(self):
        group_of: dict = {}
        next_group = [1]
        _merge(group_of, "a", "b", next_group)
        assert group_of["a"] == group_of["b"] == 1
        assert next_group == [2]

    def test_merge_existing_group_with_new_path(self):
        group_of = {"a": 1, "b": 1}
        next_group = [2]
        _merge(group_of, "b", "c", next_group)
        assert group_of["c"] == 1
        assert next_group == [2]

    def test_merge_two_distinct_groups_unifies_to_lower_id(self):
        group_of = {"a": 1, "b": 2}
        next_group = [3]
        _merge(group_of, "a", "b", next_group)
        assert group_of["a"] == group_of["b"] == 1

    def test_merge_three_way_chain_ends_up_in_one_group(self):
        group_of: dict = {}
        next_group = [1]
        _merge(group_of, "a", "b", next_group)
        _merge(group_of, "c", "d", next_group)
        _merge(group_of, "b", "c", next_group)
        assert len({group_of["a"], group_of["b"], group_of["c"], group_of["d"]}) == 1
