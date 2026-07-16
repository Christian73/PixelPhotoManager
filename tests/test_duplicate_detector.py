# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste le vrai algorithme de détection de doublons (Tier 1 pHash + Tier 2
ORB/RANSAC) en appelant `_detect()` directement, sans passer par `.start()`
(pas de vrai thread ni de bus d'événements ici — voir
test_signal_object_cross_thread.py pour la régression spécifique au
franchissement du QThread)."""
import os
from pathlib import Path

from src.library.duplicate_detector import DuplicateDetectorThread, _load_gray, _merge
from tools.test_env.generate_library import build_library


class TestDetectRealLibrary:
    def _run(self, tmp_path, cache_db_path=None):
        manifest = build_library(tmp_path / "lib")
        # str, pas Path : c'est ce que le catalogue fournit en usage réel, et
        # _load_gray() (Tier 2) échoue silencieusement sur un objet Path
        # (`path.encode("ascii")` n'existe pas sur Path, capturé par un except
        # Exception large -> image traitée comme illisible).
        # cache_db_path pointe par défaut vers un fichier isolé dans tmp_path :
        # sans ça, ces tests toucheraient le vrai dedup_cache.db de la machine
        # (état partagé non-hermétique entre runs de pytest).
        thread = DuplicateDetectorThread(
            [str(p) for p in manifest.images],
            cache_db_path=cache_db_path or str(tmp_path / "dedup_cache.db"),
        )

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

    def test_burst_pair_not_grouped_despite_shared_background(self, tmp_path):
        """Même arrière-plan texturé partagé, sujet de premier plan différent
        (simule une rafale) : l'arrière-plan seul fournit assez d'inliers
        RANSAC pour dépasser _ORB_MIN_INLIERS, mais les photos ne se
        ressemblent pas réellement une fois recalées — ne doit pas être
        groupée (cf. _ORB_MAX_MEAN_DIFF)."""
        manifest, groups = self._run(tmp_path)
        a, b = (str(p) for p in manifest.burst_pair)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert not (a in members_by_path and b in members_by_path
                    and members_by_path[a] == members_by_path[b])

    def test_edited_duplicate_pair_still_grouped(self, tmp_path):
        """Retouche luminosité+contraste légitime : garde anti-régression
        pour la vérification post-hash du Tier 1 (cf. _HASH_PIXEL_MAX_DIFF) —
        doit rester groupée malgré le nouveau filtre pixel."""
        manifest, groups = self._run(tmp_path)
        a, b = (str(p) for p in manifest.edited_duplicate_pair)
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

    def test_file_disappearing_after_prefilter_does_not_crash(self, tmp_path, monkeypatch):
        """Simule la disparition d'un fichier entre le pré-filtre os.path.isfile()
        et l'ouverture réelle (PIL) dans _compute_fingerprint — le cas exact
        rapporté par l'utilisateur ("disparition d'un fichier durant le
        traitement"). Le pré-filtre n'est qu'une optimisation ; le vrai
        garde-fou est le try/except large autour de Image.open()."""
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        vanishing = paths[0]
        Path(vanishing).unlink()

        import src.library.duplicate_detector as dd
        real_isfile = dd.os.path.isfile

        def _lying_isfile(p):
            return True if p == vanishing else real_isfile(p)

        monkeypatch.setattr(dd.os.path, "isfile", _lying_isfile)

        thread = DuplicateDetectorThread(paths)
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()  # ne doit pas lever d'exception

        assert "groups" in received
        assert vanishing in thread.corrupted_paths
        members_by_path = {p: gid for gid, members in received["groups"].items() for p in members}
        assert vanishing not in members_by_path


class TestLoadGrayTiffBypassesCv2:
    def test_tiff_never_reaches_cv2_imread(self, tmp_path, monkeypatch):
        """Certains TIFF réels (tags de métadonnées exotiques) déclenchent un
        bug connu du décodeur libtiff d'OpenCV pouvant aller jusqu'à un
        abort() du process, non rattrapable par try/except (cf. rapport
        utilisateur : assertion "original_ptr == real_mat.data" dans
        loadsave.cpp). _load_gray doit donc ne jamais appeler cv2.imread pour
        un .tif/.tiff, quel que soit le chemin — vérifié ici en faisant
        échouer cv2.imread s'il est appelé."""
        import cv2
        from PIL import Image

        tif_path = tmp_path / "photo.tif"
        Image.new("RGB", (40, 30), color=(120, 80, 40)).save(tif_path)

        def _boom(*a, **kw):
            raise AssertionError("cv2.imread ne doit pas être appelé pour un TIFF")

        monkeypatch.setattr(cv2, "imread", _boom)

        img = _load_gray(str(tif_path), max_dim=800)
        assert img is not None
        assert img.shape[:2] == (30, 40)


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


def _grouping_as_sets(groups: dict) -> set:
    """Les identifiants numériques de groupe dépendent de l'ordre d'arrivée
    des futures dans le ThreadPoolExecutor (as_completed), non déterministe
    d'un run à l'autre — y compris sans aucun cache. Seule la composition des
    groupes (quelles photos sont ensemble) est stable et comparable."""
    return {frozenset(members) for members in groups.values()}


class TestDedupCachePersistence:
    """Vérifie que le cache dedup_cache.db (Tier 1 pHash + Tier 2 ORB) rend un
    scan interruptible/reprenable : un 2e run sur un cache déjà peuplé ne doit
    pas redécoder/recalculer ce qui n'a pas changé, tout en restant correct
    face aux photos ajoutées/supprimées/modifiées entre deux runs."""

    def test_second_run_reuses_cached_fingerprints(self, tmp_path, monkeypatch):
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        assert "groups" in received1

        import PIL.Image as PILImage
        real_open = PILImage.open
        opened: list = []

        def _spy_open(fp, *a, **kw):
            opened.append(fp)
            return real_open(fp, *a, **kw)

        monkeypatch.setattr(PILImage, "open", _spy_open)

        # seed_groups reflète l'état catalogue au moment du 2e déclenchement
        # (c'est le rôle de Catalog.get_duplicate_group_assignments() en
        # usage réel) : sans lui, compared_tier1 (déjà peuplée par thread1)
        # ferait considérer toutes les paires comme "anciennes" et aucune
        # comparaison ne serait ré-évaluée pour (re)former les groupes.
        seed_groups = {p: gid for gid, members in received1["groups"].items() for p in members}
        thread2 = DuplicateDetectorThread(paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        # Rien n'a changé entre les deux runs : Tier 1 doit être 100% cache hit,
        # donc Image.open() ne doit être rappelé pour aucune photo.
        assert opened == []
        assert _grouping_as_sets(received2["groups"]) == _grouping_as_sets(received1["groups"])

    def test_second_run_reuses_cached_orb_features(self, tmp_path, monkeypatch):
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        assert "groups" in received1

        import src.library.duplicate_detector as dd
        real_load_gray = dd._load_gray
        loaded: list = []

        def _spy_load_gray(path, max_dim):
            loaded.append(path)
            return real_load_gray(path, max_dim)

        monkeypatch.setattr(dd, "_load_gray", _spy_load_gray)

        # cf. test_second_run_reuses_cached_fingerprints : seed_groups requis
        # pour que le 2e run reforme les mêmes groupes malgré compared_tier1/2
        # déjà peuplées par thread1.
        seed_groups = {p: gid for gid, members in received1["groups"].items() for p in members}
        thread2 = DuplicateDetectorThread(paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        # La paire recadrée (Tier 2) ne doit pas être redécodée : c'est
        # précisément le cas d'usage motivant la mise en cache de l'image de
        # travail, pas seulement des points-clés/descripteurs.
        a, b = (str(p) for p in manifest.crop_duplicate_pair)
        assert a not in loaded
        assert b not in loaded
        assert _grouping_as_sets(received2["groups"]) == _grouping_as_sets(received1["groups"])

    def test_changed_mtime_triggers_recompute(self, tmp_path, monkeypatch):
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        thread1.finished.connect(lambda groups: None)
        thread1._detect()

        changed = str(manifest.control_photos[0])
        current = os.path.getmtime(changed)
        os.utime(changed, (current + 5, current + 5))

        import PIL.Image as PILImage
        real_open = PILImage.open
        opened: list = []

        def _spy_open(fp, *a, **kw):
            opened.append(fp)
            return real_open(fp, *a, **kw)

        monkeypatch.setattr(PILImage, "open", _spy_open)

        thread2 = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        assert changed in opened
        assert "groups" in received2

    def test_new_photo_added_between_runs_is_detected(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        all_paths = [str(p) for p in manifest.images]
        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        cache_db = str(tmp_path / "dedup_cache.db")

        subset = [p for p in all_paths if p != b]
        thread1 = DuplicateDetectorThread(subset, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        members1 = {p: gid for gid, members in received1["groups"].items() for p in members}
        assert a not in members1 or b not in members1

        thread2 = DuplicateDetectorThread(all_paths, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()
        members2 = {p: gid for gid, members in received2["groups"].items() for p in members}
        assert a in members2 and b in members2
        assert members2[a] == members2[b]

    def test_deleted_photo_purged_from_cache(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        all_paths = [str(p) for p in manifest.images]
        removed_path = str(manifest.control_photos[0])
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(all_paths, cache_db_path=cache_db)
        thread1.finished.connect(lambda groups: None)
        thread1._detect()

        import sqlite3
        conn = sqlite3.connect(cache_db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM fingerprints WHERE path=?", (removed_path,)
            ).fetchone()[0]
            assert n == 1
        finally:
            conn.close()

        Path(removed_path).unlink()
        remaining = [p for p in all_paths if p != removed_path]
        thread2 = DuplicateDetectorThread(remaining, cache_db_path=cache_db)
        thread2.finished.connect(lambda groups: None)
        thread2._detect()

        conn = sqlite3.connect(cache_db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM fingerprints WHERE path=?", (removed_path,)
            ).fetchone()[0]
            assert n == 0
        finally:
            conn.close()

    def test_cancellation_mid_scan_persists_partial_progress(self, tmp_path, monkeypatch):
        import src.library.duplicate_detector as dd
        # Sans ce throttle, une petite bibliothèque de test se traite en bien
        # moins de _PROGRESS_INTERVAL (0.5s) et le signal progress ne serait
        # émis qu'une seule fois tout à la fin — trop tard pour annuler à mi-course.
        monkeypatch.setattr(dd, "_PROGRESS_INTERVAL", 0)

        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        cancelled = {"v": False}
        thread.cancelled.connect(lambda: cancelled.__setitem__("v", True))

        def _on_progress(cur, total, msg):
            thread.cancel()

        thread.progress.connect(_on_progress)
        thread._detect()

        assert cancelled["v"]

        import sqlite3
        conn = sqlite3.connect(cache_db)
        try:
            n_fp = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        finally:
            conn.close()
        assert 0 < n_fp < len(paths)

    def test_full_catalog_scan_false_skips_purge(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        all_paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(all_paths, cache_db_path=cache_db)
        thread1.finished.connect(lambda groups: None)
        thread1._detect()

        a, b = (str(p) for p in manifest.crop_duplicate_pair)
        partial = [p for p in all_paths if p not in (a, b)]
        thread2 = DuplicateDetectorThread(
            partial, cache_db_path=cache_db, full_catalog_scan=False
        )
        thread2.finished.connect(lambda groups: None)
        thread2._detect()

        import sqlite3
        conn = sqlite3.connect(cache_db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM fingerprints WHERE path IN (?, ?)", (a, b)
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 2


class TestKeypointRoundtrip:
    def test_crop_pair_still_grouped_when_orb_cache_hit(self, tmp_path):
        """Validation de bout en bout de la sérialisation des cv2.KeyPoint :
        sur un 2e run, la paire recadrée est entièrement servie depuis le
        cache ORB (points-clés reconstruits + image de travail redécodée du
        JPEG stocké) — _compare_chunk doit produire le même résultat que sur
        des données fraîchement calculées."""
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        # seed_groups requis dès la 2e itération : sinon compared_tier1/2
        # (déjà peuplées par la 1re) feraient considérer toutes les paires
        # comme "anciennes" et aucun groupe ne serait reformé.
        received: dict = {}
        seed_groups: dict[str, int] = {}
        for _ in range(2):
            thread = DuplicateDetectorThread(paths, seed_groups=seed_groups, cache_db_path=cache_db)
            received.clear()
            thread.finished.connect(lambda groups: received.update(groups=groups))
            thread._detect()
            seed_groups = {p: gid for gid, members in received["groups"].items() for p in members}

        a, b = (str(p) for p in manifest.crop_duplicate_pair)
        members_by_path = {p: gid for gid, members in received["groups"].items() for p in members}
        assert a in members_by_path and b in members_by_path
        assert members_by_path[a] == members_by_path[b]


class TestPartialResultsSignal:
    """Vérifie que `partial_results` (instantanés provisoires pendant le
    scan) est émis avant `finished`, et que chaque instantané reste cohérent
    avec le résultat final — cf. duplicate_detector.py::_merge : les groupes
    ne font que croître, un instantané partiel ne peut donc jamais contredire
    le résultat final (des chemins ensemble à un instant T ne peuvent que
    rester ensemble, jamais se séparer)."""

    def test_partial_results_emitted_before_finished(self, tmp_path, monkeypatch):
        import src.library.duplicate_detector as dd
        # Sans throttle nul, une petite bibliothèque de test se traite en
        # bien moins de _LIVE_SNAPSHOT_INTERVAL et aucun instantané ne serait
        # émis avant la fin.
        monkeypatch.setattr(dd, "_LIVE_SNAPSHOT_INTERVAL", 0)
        monkeypatch.setattr(dd, "_PROGRESS_INTERVAL", 0)

        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        snapshots: list = []
        finished_received = {}
        thread.partial_results.connect(
            lambda groups, corrupted: snapshots.append((groups, corrupted))
        )
        thread.finished.connect(lambda groups: finished_received.update(groups=groups))
        thread._detect()

        assert snapshots, "partial_results n'a jamais été émis"
        assert "groups" in finished_received

        final_members_by_path = {
            p: gid for gid, members in finished_received["groups"].items() for p in members
        }
        for groups, corrupted in snapshots:
            assert isinstance(corrupted, list)
            for members in groups.values():
                # Tous les membres d'un groupe partiel doivent appartenir au
                # même groupe dans le résultat final (jamais éclaté ensuite).
                final_gids = {final_members_by_path.get(p) for p in members}
                assert len(final_gids) == 1 and None not in final_gids

    def test_partial_results_reports_corrupted_file_progressively(self, tmp_path, monkeypatch):
        import src.library.duplicate_detector as dd
        monkeypatch.setattr(dd, "_LIVE_SNAPSHOT_INTERVAL", 0)
        monkeypatch.setattr(dd, "_PROGRESS_INTERVAL", 0)

        manifest = build_library(tmp_path / "lib")
        # manifest.corrupted_file n'est volontairement pas dans manifest.images
        # (cf. tools/test_env/generate_library.py) — il faut l'ajouter explicitement.
        paths = [str(p) for p in manifest.images] + [str(manifest.corrupted_file)]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        snapshots: list = []
        thread.partial_results.connect(
            lambda groups, corrupted: snapshots.append((groups, corrupted))
        )
        thread.finished.connect(lambda groups: None)
        thread._detect()

        assert any(str(manifest.corrupted_file) in corrupted for _, corrupted in snapshots)


class TestIncrementalComparison:
    """Vérifie la vraie incrémentalité de la Phase 2 (comparaison par paires,
    cf. duplicate_detector.py::_detect) : seed_groups amorce group_of, et
    seules les paires impliquant au moins un fichier nouveau/modifié (jamais
    comparé lors d'une passe complète antérieure, ou modifié depuis) sont
    évaluées — les paires ancien×ancien ne sont jamais itérées."""

    @staticmethod
    def _seed_from(groups: dict) -> dict[str, int]:
        return {p: gid for gid, members in groups.items() for p in members}

    def test_no_seed_groups_first_pass_behaves_as_before(self, tmp_path):
        """Régression : omettre seed_groups (nouveau paramètre du constructeur)
        doit produire exactement le même résultat qu'avant son introduction."""
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        thread = DuplicateDetectorThread(paths, cache_db_path=str(tmp_path / "dedup_cache.db"))
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()

        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        members = self._seed_from(received["groups"])
        assert members[a] == members[b]

    def test_second_run_with_seed_evaluates_zero_pairs_when_nothing_changed(self, tmp_path, monkeypatch):
        import imagehash

        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        seed_groups = self._seed_from(received1["groups"])

        calls = {"n": 0}
        real_sub = imagehash.ImageHash.__sub__

        def _counting_sub(self_, other):
            calls["n"] += 1
            return real_sub(self_, other)

        monkeypatch.setattr(imagehash.ImageHash, "__sub__", _counting_sub)

        thread2 = DuplicateDetectorThread(paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        assert calls["n"] == 0
        assert _grouping_as_sets(received2["groups"]) == _grouping_as_sets(received1["groups"])

    def test_new_file_added_matches_via_new_x_old_without_recomparing_old_group(self, tmp_path, monkeypatch):
        import imagehash

        manifest = build_library(tmp_path / "lib")
        all_paths = [str(p) for p in manifest.images]
        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        cache_db = str(tmp_path / "dedup_cache.db")

        subset = [p for p in all_paths if p != b]
        thread1 = DuplicateDetectorThread(subset, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        seed_groups = self._seed_from(received1["groups"])

        calls = {"n": 0}
        real_sub = imagehash.ImageHash.__sub__

        def _counting_sub(self_, other):
            calls["n"] += 1
            return real_sub(self_, other)

        monkeypatch.setattr(imagehash.ImageHash, "__sub__", _counting_sub)

        thread2 = DuplicateDetectorThread(all_paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        # Un seul fichier nouveau (b) : n*(n-1)/2 + n*len(old_list) avec n=1
        # -> exactement len(subset) comparaisons nouveau×ancien, aucune paire
        # ancien×ancien ré-évaluée parmi les fichiers déjà connus.
        assert calls["n"] == len(subset)
        members2 = self._seed_from(received2["groups"])
        assert members2[a] == members2[b]

    def test_mtime_changed_file_becomes_new_again(self, tmp_path, monkeypatch):
        import imagehash

        manifest = build_library(tmp_path / "lib")
        all_paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(all_paths, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        seed_groups = self._seed_from(received1["groups"])

        changed = str(manifest.control_photos[0])
        current = os.path.getmtime(changed)
        os.utime(changed, (current + 5, current + 5))

        calls = {"n": 0}
        real_sub = imagehash.ImageHash.__sub__

        def _counting_sub(self_, other):
            calls["n"] += 1
            return real_sub(self_, other)

        monkeypatch.setattr(imagehash.ImageHash, "__sub__", _counting_sub)

        thread2 = DuplicateDetectorThread(all_paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        # Seul le fichier modifié redevient "nouveau" : comparé contre tous
        # les autres (déjà "anciens"), jamais entre eux.
        assert calls["n"] == len(all_paths) - 1

    def test_bridging_new_file_merges_two_seed_groups(self, tmp_path):
        """Un nouveau fichier "pont" pHash-proche de deux groupes seed
        distincts (déjà stables via compared_tier1) doit les fusionner en un
        seul, via les seules comparaisons nouveau×ancien. Distances de
        Hamming (hash 64 bits) vérifiées indépendamment :
        a<->pont = 10 (<= _HASH_THRESHOLD=10), pont<->b = 1,
        a<->b = 11 (pas de match direct sans le pont)."""
        from src.library.dedup_cache import DedupCache

        lib_dir = tmp_path / "bridge_lib"
        lib_dir.mkdir()
        a1 = lib_dir / "a1.jpg"
        a2 = lib_dir / "a2.jpg"
        b1 = lib_dir / "b1.jpg"
        b2 = lib_dir / "b2.jpg"
        bridge = lib_dir / "bridge.jpg"
        for f in (a1, a2, b1, b2, bridge):
            f.write_bytes(b"stub")

        import numpy as np
        micro = np.zeros((8, 8), dtype=np.float64).tobytes()
        cache_db = str(tmp_path / "dedup_cache.db")
        cache = DedupCache(cache_db)
        cache.open()
        try:
            cache.store_fingerprints([
                (str(a1), os.path.getmtime(a1), "0000000000000000", 64, 64, micro),
                (str(a2), os.path.getmtime(a2), "0000000000000000", 64, 64, micro),
                (str(b1), os.path.getmtime(b1), "00000000000007ff", 64, 64, micro),
                (str(b2), os.path.getmtime(b2), "00000000000007ff", 64, 64, micro),
                (str(bridge), os.path.getmtime(bridge), "00000000000003ff", 64, 64, micro),
            ])
            # compared_tier1 pré-rempli pour tout sauf bridge : a1/a2/b1/b2
            # sont déjà "anciens" (groupes stables), bridge est le seul
            # "nouveau" -> seules ses comparaisons nouveau×ancien s'exécutent.
            cache.store_compared_tier1([
                (str(a1), os.path.getmtime(a1)),
                (str(a2), os.path.getmtime(a2)),
                (str(b1), os.path.getmtime(b1)),
                (str(b2), os.path.getmtime(b2)),
            ])
        finally:
            cache.close()

        seed_groups = {str(a1): 1, str(a2): 1, str(b1): 2, str(b2): 2}
        thread = DuplicateDetectorThread(
            [str(a1), str(a2), str(b1), str(b2), str(bridge)],
            seed_groups=seed_groups,
            cache_db_path=cache_db,
        )
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()

        members = self._seed_from(received["groups"])
        assert len({members[str(a1)], members[str(a2)], members[str(b1)],
                    members[str(b2)], members[str(bridge)]}) == 1

    def test_group_shrinks_to_singleton_when_member_removed(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        all_paths = [str(p) for p in manifest.images]
        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(all_paths, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        seed_groups = self._seed_from(received1["groups"])
        assert seed_groups.get(a) == seed_groups.get(b)

        remaining = [p for p in all_paths if p != b]
        thread2 = DuplicateDetectorThread(remaining, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        # _renumber() exclut les groupes réduits à un seul membre (a, désormais
        # seul dans son groupe seed puisque b n'est plus dans photo_paths).
        members2 = self._seed_from(received2["groups"])
        assert a not in members2

    def test_corrupted_file_rediscovered_after_repair(self, tmp_path):
        """Non-régression : un fichier corrompu n'écrit jamais de ligne dans
        fingerprints/orb_features (écriture seulement en cas de succès), donc
        il retombe systématiquement dans to_compute à chaque passe,
        indépendamment de l'incrémentalité de la comparaison — une fois
        réparé, il doit rejoindre normalement son groupe de doublons."""
        import shutil

        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images] + [str(manifest.corrupted_file)]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread1 = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        received1 = {}
        thread1.finished.connect(lambda groups: received1.update(groups=groups))
        thread1._detect()
        assert str(manifest.corrupted_file) in thread1.corrupted_paths
        seed_groups = self._seed_from(received1["groups"])

        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        shutil.copy2(a, manifest.corrupted_file)

        thread2 = DuplicateDetectorThread(paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        assert str(manifest.corrupted_file) not in thread2.corrupted_paths
        members2 = self._seed_from(received2["groups"])
        assert members2.get(str(manifest.corrupted_file)) == members2.get(a)
