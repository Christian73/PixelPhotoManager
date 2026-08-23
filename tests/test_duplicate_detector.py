# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests the real duplicate detection algorithm (Tier 1 pHash + Tier 2
ORB/RANSAC) by calling `_detect()` directly, without going through `.start()`
(no real thread and no event bus here - see
test_signal_object_cross_thread.py for the regression specific to
crossing the QThread)."""
import os
from pathlib import Path

from datetime import datetime

from src.library.duplicate_detector import DuplicateDetectorThread, _load_gray, _merge, _dates_differ
from tools.test_env.generate_library import build_library


class TestDetectRealLibrary:
    def _run(self, tmp_path, cache_db_path=None):
        manifest = build_library(tmp_path / "lib")
        # str, not Path: that is what the catalog provides in real use, and
        # _load_gray() (Tier 2) fails silently on a Path object
        # (`path.encode("ascii")` does not exist on Path, caught by a broad except
        # Exception -> image treated as unreadable).
        # cache_db_path points by default at an isolated file in tmp_path:
        # without that, these tests would touch the real dedup_cache.db of the machine
        # (non-hermetic shared state between pytest runs).
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
        """The cropped pair must NOT match at Tier 1 (pHash) - it is
        Tier 2 (ORB/RANSAC) that must group it. Proof that Tier 2 really runs."""
        manifest, groups = self._run(tmp_path)
        a, b = (str(p) for p in manifest.crop_duplicate_pair)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert a in members_by_path and b in members_by_path
        assert members_by_path[a] == members_by_path[b]

    def test_burst_pair_not_grouped_despite_shared_background(self, tmp_path):
        """Same shared textured background, different foreground subject
        (simulates a burst): the background alone provides enough RANSAC
        inliers to go beyond _ORB_MIN_INLIERS, but the photos do not
        really look alike once realigned - must not be
        grouped (cf. _ORB_MAX_MEAN_DIFF)."""
        manifest, groups = self._run(tmp_path)
        a, b = (str(p) for p in manifest.burst_pair)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert not (a in members_by_path and b in members_by_path
                    and members_by_path[a] == members_by_path[b])

    def test_edited_duplicate_pair_still_grouped(self, tmp_path):
        """Legitimate brightness+contrast retouch: anti-regression guard
        for the post-hash check of Tier 1 (cf. _HASH_PIXEL_MAX_DIFF) -
        must stay grouped despite the new pixel filter."""
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

    def test_corrupted_file_persisted_to_cache_db(self, tmp_path):
        """Complements TestCorruptedFilesPersistence (test_dedup_cache.py, which
        tests DedupCache in isolation): checks that a real run of
        _detect() really persists self._corrupted into corrupted_files (finally
        of _detect(), cf. duplicate_detector.py), not only in memory.
        manifest.corrupted_file is deliberately not in manifest.images
        (cf. generate_library.py): it must be added explicitly to the
        scanned paths, like test_corrupted_file_rediscovered_after_repair."""
        from src.library.dedup_cache import DedupCache

        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images] + [str(manifest.corrupted_file)]
        cache_db_path = str(tmp_path / "dedup_cache.db")

        thread = DuplicateDetectorThread(paths, cache_db_path=cache_db_path)
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()
        assert "groups" in received

        cache = DedupCache(cache_db_path)
        cache.open()
        try:
            assert cache.get_corrupted_paths() == [str(manifest.corrupted_file)]
        finally:
            cache.close()

    def test_no_photos_emits_empty_dict(self, tmp_path):
        thread = DuplicateDetectorThread([])
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()
        assert received["groups"] == {}

    def test_file_disappearing_after_prefilter_does_not_crash(self, tmp_path, monkeypatch):
        """Simulates the disappearance of a file between the os.path.isfile()
        pre-filter and the real opening (PIL) in _compute_fingerprint - the exact case
        reported by the user ("a file I delete ends up
        in the list of corrupted files"). Image.open() fails the
        same way (FileNotFoundError) for a file gone as for a
        really corrupted file: the guard re-checks os.path.exists()
        before classifying as corrupted, so a file gone is simply
        ignored, never offered for repair/deletion for nothing."""
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        vanishing = paths[0]
        Path(vanishing).unlink()

        import src.library.duplicate_detector as dd
        real_isfile = dd.os.path.isfile

        def _lying_isfile(p):
            return True if p == vanishing else real_isfile(p)

        monkeypatch.setattr(dd.os.path, "isfile", _lying_isfile)

        # isolated cache_db_path: without that, _detect() would write into the real
        # dedup_cache.db of the machine (and, since the persistence of
        # corrupted_files, would overwrite its real content entirely -
        # cf. DedupCache.replace_corrupted_paths).
        thread = DuplicateDetectorThread(paths, cache_db_path=str(tmp_path / "dedup_cache.db"))
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()  # must not raise an exception

        assert "groups" in received
        assert vanishing not in thread.corrupted_paths
        members_by_path = {p: gid for gid, members in received["groups"].items() for p in members}
        assert vanishing not in members_by_path


class TestDatesDiffer:
    def test_both_known_and_different(self):
        assert _dates_differ(
            {"a": datetime(2026, 1, 4, 12, 19, 15, 50000),
             "b": datetime(2026, 1, 4, 12, 19, 15, 630000)},
            "a", "b",
        )

    def test_both_known_and_equal(self):
        dt = datetime(2026, 1, 4, 12, 19, 15)
        assert not _dates_differ({"a": dt, "b": dt}, "a", "b")

    def test_one_missing_does_not_block(self):
        assert not _dates_differ({"a": datetime(2026, 1, 4, 12, 19, 15)}, "a", "b")

    def test_both_missing_does_not_block(self):
        assert not _dates_differ({}, "a", "b")


class TestDuplicateExifDateExclusion:
    """Checks the explicit rule of the user: two photos whose
    EXIF dates are both known and different must never
    be grouped as duplicates, even if pHash/ORB judge them identical
    (the case of a burst: near-identical content, different instants)."""

    def _run(self, tmp_path, dates=None):
        manifest = build_library(tmp_path / "lib")
        thread = DuplicateDetectorThread(
            [str(p) for p in manifest.images],
            cache_db_path=str(tmp_path / "dedup_cache.db"),
            dates=dates,
        )
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()
        assert "groups" in received
        return manifest, received["groups"]

    def test_tier1_pair_not_grouped_when_dates_differ(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        dates = {a: datetime(2026, 1, 4, 12, 19, 15), b: datetime(2026, 1, 4, 12, 19, 16)}
        thread = DuplicateDetectorThread(
            [str(p) for p in manifest.images],
            cache_db_path=str(tmp_path / "dedup_cache.db"),
            dates=dates,
        )
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()
        members_by_path = {p: gid for gid, members in received["groups"].items() for p in members}
        assert not (a in members_by_path and b in members_by_path
                    and members_by_path[a] == members_by_path[b])

    def test_tier1_pair_still_grouped_when_dates_equal(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        same = datetime(2026, 1, 4, 12, 19, 15)
        dates = {a: same, b: same}
        thread = DuplicateDetectorThread(
            [str(p) for p in manifest.images],
            cache_db_path=str(tmp_path / "dedup_cache.db"),
            dates=dates,
        )
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()
        members_by_path = {p: gid for gid, members in received["groups"].items() for p in members}
        assert a in members_by_path and b in members_by_path
        assert members_by_path[a] == members_by_path[b]

    def test_tier1_pair_still_grouped_when_dates_unknown(self, tmp_path):
        """Absence of dates (dates=None): behaviour unchanged compared
        to before this rule was added."""
        manifest, groups = self._run(tmp_path, dates=None)
        a, b = (str(p) for p in manifest.exact_duplicate_pair)
        members_by_path = {p: gid for gid, members in groups.items() for p in members}
        assert a in members_by_path and b in members_by_path
        assert members_by_path[a] == members_by_path[b]

    def test_tier2_crop_pair_not_grouped_when_dates_differ(self, tmp_path):
        manifest = build_library(tmp_path / "lib")
        a, b = (str(p) for p in manifest.crop_duplicate_pair)
        dates = {a: datetime(2026, 1, 4, 12, 19, 15), b: datetime(2026, 1, 4, 12, 19, 16)}
        thread = DuplicateDetectorThread(
            [str(p) for p in manifest.images],
            cache_db_path=str(tmp_path / "dedup_cache.db"),
            dates=dates,
        )
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()
        members_by_path = {p: gid for gid, members in received["groups"].items() for p in members}
        assert not (a in members_by_path and b in members_by_path
                    and members_by_path[a] == members_by_path[b])


class TestLoadGrayTiffBypassesCv2:
    def test_tiff_never_reaches_cv2_imread(self, tmp_path, monkeypatch):
        """Some real TIFFs (exotic metadata tags) trigger a
        known bug of the libtiff decoder of OpenCV that can go as far as an
        abort() of the process, not catchable by try/except (cf. user
        report: assertion "original_ptr == real_mat.data" in
        loadsave.cpp). _load_gray must therefore never call cv2.imread for
        a .tif/.tiff, whatever the path - checked here by making
        cv2.imread fail if it is called."""
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


class TestRawFilesNeverFlaggedCorrupted:
    """A .cr2 (neither PIL nor cv2.imread can decode it without rawpy - not
    monkeypatched here) must be excluded from the sampling of _detect() like the
    videos (_VIDEO_EXT), not classified "corrupted" and offered for deletion.
    In the style of test_tiff_never_reaches_cv2_imread: deliberately undecodable
    content, to check the exclusion upstream rather than the decoding."""

    def test_raw_file_excluded_from_corrupted_and_from_groups(self, tmp_path):
        from tools.test_env.generate_library import build_library

        manifest = build_library(tmp_path / "lib")
        raw_path = tmp_path / "lib" / "photo.cr2"
        raw_path.write_bytes(b"pas un vrai CR2")
        paths = [str(p) for p in manifest.images] + [str(raw_path)]

        thread = DuplicateDetectorThread(
            paths, cache_db_path=str(tmp_path / "dedup_cache.db"),
        )
        received = {}
        thread.finished.connect(lambda groups: received.update(groups=groups))
        thread._detect()

        assert "groups" in received
        members_by_path = {
            p: gid for gid, members in received["groups"].items() for p in members
        }
        assert str(raw_path) not in members_by_path
        assert str(raw_path) not in thread.corrupted_paths


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
    """The numeric group identifiers depend on the arrival order
    of the futures in the ThreadPoolExecutor (as_completed), not deterministic
    from one run to the next - including with no cache at all. Only the composition of the
    groups (which photos are together) is stable and comparable."""
    return {frozenset(members) for members in groups.values()}


class TestDedupCachePersistence:
    """Checks that the dedup_cache.db cache (Tier 1 pHash + Tier 2 ORB) makes a
    scan interruptible/resumable: a 2nd run on an already populated cache must
    not decode/recompute what has not changed, while staying correct
    in the face of photos added/removed/modified between two runs."""

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

        # seed_groups reflects the state of the catalog at the moment of the 2nd trigger
        # (that is the role of Catalog.get_duplicate_group_assignments() in
        # real use): without it, compared_tier1 (already populated by thread1)
        # would make every pair look "old" and no
        # comparison would be re-evaluated to (re)form the groups.
        seed_groups = {p: gid for gid, members in received1["groups"].items() for p in members}
        thread2 = DuplicateDetectorThread(paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        # Nothing changed between the two runs: Tier 1 must be 100% cache hit,
        # so Image.open() must not be called again for any photo.
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

        # cf. test_second_run_reuses_cached_fingerprints: seed_groups required
        # so that the 2nd run reforms the same groups despite compared_tier1/2
        # already populated by thread1.
        seed_groups = {p: gid for gid, members in received1["groups"].items() for p in members}
        thread2 = DuplicateDetectorThread(paths, seed_groups=seed_groups, cache_db_path=cache_db)
        received2 = {}
        thread2.finished.connect(lambda groups: received2.update(groups=groups))
        thread2._detect()

        # The cropped pair (Tier 2) must not be decoded again: that is
        # precisely the use case motivating the caching of the working
        # image, and not only of the keypoints/descriptors.
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
        # Without this throttle, a small test library is processed in far
        # less than _PROGRESS_INTERVAL (0.5s) and the progress signal would only be
        # emitted once right at the end - too late to cancel midway.
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

    def _cancel_during_comparison(self, tmp_path, monkeypatch, after_snapshots: int):
        """Starts a scan and cancels it during the *comparison loop* of Tier 1
        (and not during the computation of the fingerprints, already covered above).

        Both intervals are set to 0: on a test library, the
        loop finishes in far less than their real values and no
        snapshot would be emitted before the end. Returns (paths, cache
        database, last groups broadcast)."""
        import src.library.duplicate_detector as dd
        monkeypatch.setattr(dd, "_PROGRESS_INTERVAL", 0)
        monkeypatch.setattr(dd, "_LIVE_SNAPSHOT_INTERVAL", 0)

        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        thread = DuplicateDetectorThread(paths, cache_db_path=cache_db)
        state = {"snapshots": 0, "groups": {}}

        def _on_partial(groups, corrupted):
            state["snapshots"] += 1
            state["groups"] = groups
            if state["snapshots"] >= after_snapshots:
                thread.cancel()

        thread.partial_results.connect(_on_partial)
        thread._detect()

        assert state["snapshots"] >= after_snapshots, "annulation jamais déclenchée"
        return paths, cache_db, state["groups"]

    def test_cancellation_mid_comparison_checkpoints_compared_tier1(
        self, tmp_path, monkeypatch
    ):
        """Regression: `compared_tier1` was only written once the comparison
        loop was *entirely* finished. An interrupted pass (closing of
        the application) therefore persisted no comparison and started again from scratch
        at the next startup - on a large library, the same hour of CPU
        replayed at every session, indefinitely."""
        paths, cache_db, _ = self._cancel_during_comparison(
            tmp_path, monkeypatch, after_snapshots=3
        )

        import sqlite3
        conn = sqlite3.connect(cache_db)
        try:
            n_compared = conn.execute("SELECT COUNT(*) FROM compared_tier1").fetchone()[0]
        finally:
            conn.close()

        # Strictly between the two: "> 0" is the fix itself,
        # "< len(paths)" checks that we really cancelled along the way (otherwise
        # the test would also pass on a full scan, proving nothing).
        assert 0 < n_compared < len(paths)

    def test_resumed_scan_finds_the_same_groups(self, tmp_path, monkeypatch):
        """The resumption must not only be fast: it must reach the
        same result as an uninterrupted pass. The milestone is deliberately one
        snapshot behind the real progress, precisely so
        that no merge broadcast too late is lost."""
        paths, cache_db, partial_groups = self._cancel_during_comparison(
            tmp_path, monkeypatch, after_snapshots=3
        )

        # Resumption: seed_groups = what the catalog contains at this stage,
        # that is the last snapshot broadcast (cf. _on_partial in
        # main_window_duplicates.py).
        seed_groups = {p: gid for gid, members in partial_groups.items() for p in members}
        resumed = DuplicateDetectorThread(
            paths, seed_groups=seed_groups, cache_db_path=cache_db
        )
        got_resumed = {}
        resumed.finished.connect(lambda groups: got_resumed.update(groups=groups))
        resumed._detect()
        assert "groups" in got_resumed

        # Reference: the same library scanned in one go, with a fresh cache.
        reference = DuplicateDetectorThread(
            paths, cache_db_path=str(tmp_path / "dedup_cache_ref.db")
        )
        got_ref = {}
        reference.finished.connect(lambda groups: got_ref.update(groups=groups))
        reference._detect()

        assert _grouping_as_sets(got_resumed["groups"]) == _grouping_as_sets(got_ref["groups"])

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
        """End-to-end validation of the serialisation of the cv2.KeyPoint objects:
        on a 2nd run, the cropped pair is entirely served from the
        ORB cache (keypoints rebuilt + working image decoded again from the
        stored JPEG) - _compare_chunk must produce the same result as on
        freshly computed data."""
        manifest = build_library(tmp_path / "lib")
        paths = [str(p) for p in manifest.images]
        cache_db = str(tmp_path / "dedup_cache.db")

        # seed_groups required from the 2nd iteration on: otherwise compared_tier1/2
        # (already populated by the 1st) would make every pair look
        # "old" and no group would be reformed.
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
    """Checks that `partial_results` (provisional snapshots during the
    scan) is emitted before `finished`, and that each snapshot stays consistent
    with the final result - cf. duplicate_detector.py::_merge: the groups
    only ever grow, so a partial snapshot can never contradict
    the final result (paths together at an instant T can only
    stay together, never split)."""

    def test_partial_results_emitted_before_finished(self, tmp_path, monkeypatch):
        import src.library.duplicate_detector as dd
        # Without a zero throttle, a small test library is processed in
        # far less than _LIVE_SNAPSHOT_INTERVAL and no snapshot would be
        # emitted before the end.
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
                # Every member of a partial group must belong to the
                # same group in the final result (never split up afterwards).
                final_gids = {final_members_by_path.get(p) for p in members}
                assert len(final_gids) == 1 and None not in final_gids

    def test_partial_results_reports_corrupted_file_progressively(self, tmp_path, monkeypatch):
        import src.library.duplicate_detector as dd
        monkeypatch.setattr(dd, "_LIVE_SNAPSHOT_INTERVAL", 0)
        monkeypatch.setattr(dd, "_PROGRESS_INTERVAL", 0)

        manifest = build_library(tmp_path / "lib")
        # manifest.corrupted_file is deliberately not in manifest.images
        # (cf. tools/test_env/generate_library.py) - it must be added explicitly.
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
    """Checks the real incrementality of Phase 2 (pairwise comparison,
    cf. duplicate_detector.py::_detect): seed_groups seeds group_of, and
    only the pairs involving at least one new/modified file (never
    compared during an earlier full pass, or modified since) are
    evaluated - the old x old pairs are never iterated."""

    @staticmethod
    def _seed_from(groups: dict) -> dict[str, int]:
        return {p: gid for gid, members in groups.items() for p in members}

    def test_no_seed_groups_first_pass_behaves_as_before(self, tmp_path):
        """Regression: omitting seed_groups (a new parameter of the constructor)
        must produce exactly the same result as before its introduction."""
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

        # A single new file (b): n*(n-1)/2 + n*len(old_list) with n=1
        # -> exactly len(subset) new x old comparisons, no old x old pair
        # re-evaluated among the files already known.
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

        # Only the modified file becomes "new" again: compared against all
        # the others (already "old"), never among themselves.
        assert calls["n"] == len(all_paths) - 1

    def test_bridging_new_file_merges_two_seed_groups(self, tmp_path):
        """A new "bridge" file pHash-close to two distinct seed
        groups (already stable through compared_tier1) must merge them into a
        single one, through the new x old comparisons alone. Hamming
        distances (64-bit hash) checked independently:
        a<->bridge = 10 (<= _HASH_THRESHOLD=10), bridge<->b = 1,
        a<->b = 11 (no direct match without the bridge)."""
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
            # compared_tier1 pre-filled for everything except bridge: a1/a2/b1/b2
            # are already "old" (stable groups), bridge is the only
            # "new" one -> only its new x old comparisons run.
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

        # _renumber() excludes the groups reduced to a single member (a, now
        # alone in its seed group since b is no longer in photo_paths).
        members2 = self._seed_from(received2["groups"])
        assert a not in members2

    def test_corrupted_file_rediscovered_after_repair(self, tmp_path):
        """Non-regression: a corrupted file never writes a row into
        fingerprints/orb_features (write only on success), so
        it systematically falls back into to_compute at every pass,
        independently of the incrementality of the comparison - once
        repaired, it must normally rejoin its duplicate group."""
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
