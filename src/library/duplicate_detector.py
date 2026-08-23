# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Detection of perceptual duplicates in the photo library.

Two detection tiers:
  Tier 1 — pHash (Hamming distance):
      Covers exact, resized and edited duplicates (colour, brightness).
      Fast: O(N²) comparisons of 64-bit hashes.

  Tier 2 — ORB + RANSAC (keypoint matching):
      Covers cropped duplicates (up to ~60 % of the area cropped away).
      Runs only on the photos not grouped by Tier 1.
      A prior area ratio filter avoids the impossible pairs.
"""
import bisect
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.core.cpu_throttle import (
    limit_cv2_threads,
    lower_current_thread_priority,
    throttle_tick,
    throttled_worker_count,
)
from src.core.i18n import active_language, translate
from src.library.dedup_cache import DedupCache
from src.library.image_loader import RAW_EXT

logger = logging.getLogger(__name__)

_PROGRESS_INTERVAL = 0.5  # seconds — throttle of the logs/signals in the O(N²) loops
_LIVE_SNAPSHOT_INTERVAL = 2.0  # seconds — throttle of the partial_results snapshots
# (renumbering + catalog write + UI refresh), more expensive than a plain
# progress signal, hence paced more slowly than _PROGRESS_INTERVAL.

# ── Tier 1 ─────────────────────────────────────────────────────────────────────
_HASH_THRESHOLD  = 10    # max Hamming distance (8 = exact/resize; 10 covers moderate edits)
_HASH_MICRO_SIZE     = 8    # thumbnail (px) for the post-hash check
_HASH_PIXEL_MAX_DIFF = 0.34  # mean deviation (normalised 8x8 thumbnail) beyond
# which a pHash-positive pair is rejected. Calibrated empirically: the worst
# plausible legitimate edit (a 5° rotation, under the pHash threshold) ~0.31;
# two real false positives observed (unrelated photos, hashes coinciding just
# at the threshold, a similar light/dark silhouette — e.g. sky+coast vs
# sky+cliff) ~0.375 and ~0.88. The first false positive (0.375) is close to
# the legitimate case (0.31): a narrow residual margin (~0.03 on each side).
# If a new false negative appears (a genuine edit not detected), this
# threshold must be raised and the ✕ button (Catalog.ignore_duplicate_group,
# persistent) relied on for the residual false positives rather than this
# threshold alone.

# ── Tier 2 ─────────────────────────────────────────────────────────────────────
_ORB_MIN_INLIERS = 40    # minimum RANSAC inliers to validate a match
_ORB_AREA_FACTOR = 6.0   # max area ratio between two photos to be candidates
_ORB_MAX_KP      = 300   # max ORB keypoints per image (speed vs recall)
_ORB_RATIO_TEST  = 0.75  # threshold of Lowe's ratio test
_ORB_LOAD_SIZE   = 800   # max dimension (px) to load an image in Tier 2
_ORB_GOOD_MIN    = 15    # matches after the ratio test required before running RANSAC
_ORB_MAX_MEAN_DIFF = 25.0  # pixel deviation (0-255) after registration by
# a homography, over the overlap area. Calibrated empirically: a genuine
# crop (the synthetic crop_duplicate_pair pair) gives ~14; two real false
# positives observed (a burst, a static and heavily textured background
# but a different subject) gave 38 and 42 — it is the only one of the
# signals tested (number of inliers, inliers/good ratio) that separates
# the two cases clearly.

_GRAY_CACHE_SIZE = 32  # Tier 2 working images kept decoded (cf. _GrayImageCache)

_VIDEO_EXT = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.webm',
              '.m4v', '.3gp', '.flv', '.ts', '.mts', '.mpg', '.mpeg', '.vob'}


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_gray(path: str, max_dim: int) -> "np.ndarray | None":
    """Loads in greyscale, downscaled if > max_dim. Returns None on error.

    cv2.imread rejects non-ASCII paths on Windows (cf. detector.py::
    _exif_corrected): PIL is used directly in that case, to avoid a cv2
    attempt doomed to fail (a console warning + a double decoding).

    TIFF is excluded from cv2.imread whatever the path: some TIFFs with
    exotic metadata tags (e.g. tag 50341/0xc4a5, observed in real use)
    trigger a known bug of OpenCV's libtiff decoder (the internal assertion
    "original_ptr == real_mat.data" in loadsave.cpp) that can end in an
    abort() of the process rather than a normally catchable Python cv2.error
    — a try/except does not protect against that case. PIL decodes those very
    files without trouble (already used without incident by Tier 1, which
    never goes through cv2)."""
    try:
        import numpy as np
        import cv2
        img = None
        is_tiff = Path(path).suffix.lower() in (".tif", ".tiff")
        if not is_tiff:
            try:
                path.encode("ascii")
            except UnicodeEncodeError:
                pass
            else:
                img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            from PIL import Image
            with Image.open(path) as pil:
                img = np.array(pil.convert("L"))
        if img is None:
            return None
        h, w = img.shape[:2]
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        return img
    except Exception:
        return None


class _GrayImageCache:
    """A thread-safe LRU of the Tier 2 working images (greyscale, downscaled
    to `_ORB_LOAD_SIZE`), loaded **on demand**.

    Those images only serve the final check of a pair (registration by
    homography then a pixel comparison, cf. `_ORB_MAX_MEAN_DIFF`), reached
    only by the rare pairs that have already passed Lowe's ratio test and the
    RANSAC inlier threshold. Keeping them all decoded in `desc_list` cost, on
    a library of 65,000 photos, one JPEG decoding per photo on every start
    (~80 s of CPU) and a memory footprint of several dozen GB — for an array
    of which only a handful of entries were used. They are reloaded from the
    original file through `_load_gray()`, exactly the function that served to
    compute the keypoints: the same dimensions, so the homography and the
    overlap mask stay valid."""

    def __init__(self, capacity: int = _GRAY_CACHE_SIZE) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, object]" = OrderedDict()

    def get(self, path: str):
        """The greyscale image, or None if the file is unreadable.
        Two threads may decode the same path simultaneously (the lock is not
        held during the decoding, which is long): of no consequence, the
        result is identical and the second overwrites the first."""
        with self._lock:
            img = self._items.get(path)
            if img is not None:
                self._items.move_to_end(path)
                return img
        img = _load_gray(path, _ORB_LOAD_SIZE)
        if img is None:
            return None
        with self._lock:
            self._items[path] = img
            self._items.move_to_end(path)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return img


def _merge(group_of: dict[str, int], path_a: str, path_b: str,
           next_group: list[int]) -> None:
    """Merges the groups of path_a and path_b in group_of (a naive union-find)."""
    ga = group_of.get(path_a)
    gb = group_of.get(path_b)
    if ga is None and gb is None:
        group_of[path_a] = next_group[0]
        group_of[path_b] = next_group[0]
        next_group[0] += 1
    elif ga is None:
        group_of[path_a] = gb
    elif gb is None:
        group_of[path_b] = ga
    elif ga != gb:
        old, new = max(ga, gb), min(ga, gb)
        for p in group_of:
            if group_of[p] == old:
                group_of[p] = new


def _dates_differ(dates: dict, path_a: str, path_b: str) -> bool:
    """True only if both photos have a known and different EXIF date — a
    burst (the same EXIF second, different sub-seconds) must be excluded
    from the duplicates even when the visual content is nearly identical,
    at the explicit request of the user. A date missing on one side or on
    both never blocks the merge (a fallback on the visual signal alone, as
    before this check was added)."""
    dt_a = dates.get(path_a)
    dt_b = dates.get(path_b)
    if dt_a is None or dt_b is None:
        return False
    return dt_a != dt_b


def _renumber(group_of: dict[str, int]) -> dict[int, list[str]]:
    """Renumbers the raw group_ids (1, 2, 3…) and excludes the singletons —
    usable on a final `group_of` as well as on a provisional snapshot taken
    during a scan (the groups only ever grow)."""
    raw: dict[int, list[str]] = {}
    for path, gid in group_of.items():
        raw.setdefault(gid, []).append(path)

    groups: dict[int, list[str]] = {}
    new_id = 1
    for members in raw.values():
        if len(members) >= 2:
            groups[new_id] = members
            new_id += 1
    return groups


# ── main thread ────────────────────────────────────────────────────────────────

class DuplicateDetectorThread(QThread):
    """Detects the duplicates in two passes (pHash + ORB/RANSAC)."""

    progress  = Signal(int, int, str)  # (courant, total, message)
    # object (not dict): PySide6 maps Signal(dict) onto QVariantMap, which requires
    # str keys on the C++ side — with int keys (group_id), the cross-thread
    # conversion fails silently (Shiboken logs an error on stderr, no Python
    # exception) and the slot receives an empty dict, suggesting "no duplicates".
    finished  = Signal(object)         # {group_id: [path, ...]}
    # A provisional snapshot during the scan, the same key constraint as above.
    partial_results = Signal(object, object)  # ({group_id: [path,...]}, [chemin_corrompu, ...])
    error     = Signal(str)
    cancelled = Signal()              # emitted once the thread has really stopped

    def __init__(self, photo_paths: list, seed_groups: dict[str, int] | None = None,
                 cache_db_path: str | None = None,
                 full_catalog_scan: bool = True, parent=None,
                 dates: dict | None = None):
        """seed_groups: {path: group_id} known at the time of the trigger
        (typically Catalog.get_duplicate_group_assignments()) — it seeds
        group_of so that the comparison stays genuinely incremental (cf.
        compared_tier1/compared_tier2 in dedup_cache.py). Careful: omitting
        seed_groups on a 2nd run over the same cache_db_path, already
        populated by a previous run, does NOT retrigger a full comparison —
        every pair will look "already compared" and no group will be
        (re)formed. Always pass the current seed_groups, including for a
        deliberate new full scan (in which case passing {} explicitly does
        not help either: purging compared_tier1/compared_tier2 through a new
        cache_db_path or a cache purge would be needed).

        dates: {path: datetime|None} — the capture date (EXIF, with
        sub-second precision when available; typically
        Catalog.get_photo_dates_for_dedup()). Two photos whose dates are both
        known and different are never merged as duplicates, even when the
        visual signals (pHash, ORB) agree — cf. _dates_differ(). A missing
        date blocks nothing (a fallback on the visual signal alone)."""
        super().__init__(parent)
        self._paths = photo_paths
        self._seed_groups = dict(seed_groups) if seed_groups else {}
        self._cache_db_path = cache_db_path
        self._full_catalog_scan = full_catalog_scan
        self._cancelled = False
        self._corrupted: set[str] = set()
        self._dates = dates or {}

    def cancel(self) -> None:
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        """Checks the cancellation request; emits `cancelled` exactly once at
        the effective stopping point (the O(N²) loops test it on every
        iteration, but the real stop only happens once)."""
        if self._cancelled:
            self.cancelled.emit()
            return True
        return False

    @property
    def corrupted_paths(self) -> list[str]:
        """Paths of the files whose loading failed during the scan (probably
        corrupted). Stable once the `finished` signal has been emitted."""
        return sorted(self._corrupted)

    def run(self) -> None:
        self.setPriority(QThread.LowestPriority)
        # The setPriority() above only goes down to THREAD_PRIORITY_LOWEST (-2):
        # not enough for the O(N²) loop of Tier 1, which is the heaviest
        # single-threaded part of the whole detection and runs precisely on
        # this very thread (the ThreadPoolExecutors, for their part, already
        # pass lower_current_thread_priority as their initializer).
        lower_current_thread_priority()
        # A process-wide setting (cf. the docstring): without it, each of our
        # "throttled" workers can on its own occupy all 16 cores through
        # OpenCV's internal pool.
        limit_cv2_threads(1)
        try:
            self._detect()
        except Exception as e:
            logger.exception("Erreur détection doublons")
            self.error.emit(str(e))

    # ── Tier 1: pHash ─────────────────────────────────────────────────────────

    def _detect(self) -> None:
        try:
            import imagehash
            import numpy as np
            from PIL import Image
        except ImportError as e:
            self.error.emit(
                f"Module manquant : {e}\n"
                "Installez imagehash et Pillow (pip install imagehash Pillow)."
            )
            return

        paths = [
            p for p in self._paths
            if os.path.isfile(p) and Path(p).suffix.lower() not in _VIDEO_EXT
            and Path(p).suffix.lower() not in RAW_EXT
        ]
        total = len(paths)
        if total == 0:
            self.finished.emit({})
            return

        # Each of the two phases counts for half of the progress bar.
        grand_total = total * 2

        cache = DedupCache(self._cache_db_path) if self._cache_db_path else DedupCache()
        cache.open()
        try:
            if self._full_catalog_scan:
                removed = cache.purge_missing(set(paths))
                if removed:
                    logger.info("dedup_cache : %d entrée(s) obsolète(s) purgée(s).", removed)

            # Phase 1: computing the pHash fingerprints + dimensions (used by Tier 2)
            hashes: list[tuple[str, object]] = []
            dims: dict[str, tuple[int, int]] = {}
            micro: dict[str, "np.ndarray"] = {}
            mtimes: dict[str, float] = {}
            n_workers = throttled_worker_count()

            # Reuse of the cache: a photo whose mtime has not changed since the
            # last scan does not need to be reopened/rehashed.
            cached_fp = cache.get_fingerprints(paths)
            to_compute: list[str] = []
            for path in paths:
                row = cached_fp.get(path)
                if row is not None:
                    mtime_cached, phash_hex, w, h, micro_blob = row
                    try:
                        current_mtime = os.path.getmtime(path)
                    except OSError:
                        current_mtime = None
                    if current_mtime is not None and abs(mtime_cached - current_mtime) < 1.0:
                        dims[path] = (w, h)
                        micro[path] = np.frombuffer(
                            micro_blob, dtype=np.float64
                        ).reshape(_HASH_MICRO_SIZE, _HASH_MICRO_SIZE)
                        mtimes[path] = current_mtime
                        hashes.append((path, imagehash.hex_to_hash(phash_hex)))
                        continue
                to_compute.append(path)

            cache_hits = total - len(to_compute)
            logger.info(
                "Tier 1 : %d/%d empreintes réutilisées du cache, %d à calculer sur %d cœur(s)…",
                cache_hits, total, len(to_compute), n_workers,
            )

            def _compute_fingerprint(path: str):
                # A pure function (no shared state written): each call decodes its own
                # file and returns its result, merged afterwards on this thread as the
                # completions come in — JPEG/PNG decoding (PIL) and the DCT computation
                # (imagehash, numpy) both release the GIL for the duration of the C
                # computation, so a thread pool really does exploit several cores here
                # (the same reasoning as for ORB/RANSAC in Tier 2, further down in this
                # file).
                try:
                    with Image.open(path) as img:
                        d = img.size
                        h = imagehash.phash(img)
                        arr = np.asarray(
                            img.convert("L").resize(
                                (_HASH_MICRO_SIZE, _HASH_MICRO_SIZE), Image.LANCZOS
                            ),
                            dtype=np.float64,
                        )
                    # Read after the successful open (not before) so that the persisted
                    # mtime matches the content actually fingerprinted.
                    mtime = os.path.getmtime(path)
                    arr -= arr.mean()
                    std = arr.std()
                    if std > 1e-6:
                        arr /= std
                    result = (path, h, d, arr, mtime, None)
                except Exception as exc:
                    result = (path, None, None, None, None, exc)
                # The duty cycle is taken here, in the worker that has just
                # consumed the CPU, and not on the consumer side
                # (`as_completed`): every future is submitted in advance, so
                # slowing the collection loop down would not slow the pool by
                # one iota.
                throttle_tick(lambda: self._cancelled)
                return result

            done = cache_hits
            if cache_hits:
                # An immediate signal: keeps the bar from looking frozen while
                # most of a large, already cached library is being skipped.
                self.progress.emit(done, grand_total, translate(
                    "DuplicateDetector", "Tier 1 — fingerprints {done}/{total} (cache)…"
                ).format(done=done, total=total))

            last_emit = time.monotonic()
            last_persist = last_emit
            pending_fp: list[tuple] = []
            try:
                executor = ThreadPoolExecutor(
                    max_workers=n_workers, initializer=lower_current_thread_priority
                )
                cancelled_mid_flight = False
                try:
                    futures = {}
                    for path in to_compute:
                        # A systematic log (not throttled) at submission time: if the
                        # processing hangs on a specific file (a corrupted image, a slow
                        # network volume…), this line stays the last one of the log — it
                        # identifies the offending file even under parallel execution.
                        logger.debug("Tier 1 empreinte (soumission) : %s", path)
                        futures[executor.submit(_compute_fingerprint, path)] = path

                    for future in as_completed(futures):
                        if self._is_cancelled():
                            cancelled_mid_flight = True
                            break

                        path = futures[future]
                        try:
                            path, h, d, arr, mtime, exc = future.result()
                        except Exception as e:
                            # _compute_fingerprint already catches every exception internally
                            # (returned in the tuple rather than raised): this safeguard should
                            # normally never fire, but an unexpected failure of one worker must
                            # not make the whole scan fail.
                            logger.warning("Worker Tier 1 en échec pour %s : %s", path, e)
                            h = d = arr = mtime = None
                            exc = e
                        done += 1
                        if exc is not None:
                            # A file deleted (by the user, while this scan is running)
                            # fails to open exactly like a corrupted file (a
                            # FileNotFoundError caught in the same except Exception above)
                            # — check that it still exists before classifying it as
                            # corrupted, otherwise it would stay reported (and offered for
                            # deletion/repair) for a file that no longer exists.
                            if os.path.exists(path):
                                logger.warning("Fichier illisible (Tier 1) : %s (%s)", path, exc)
                                self._corrupted.add(path)
                            else:
                                logger.debug(
                                    "Fichier disparu avant le calcul d'empreinte (Tier 1) : %s",
                                    path,
                                )
                        else:
                            dims[path] = d
                            micro[path] = arr
                            mtimes[path] = mtime
                            hashes.append((path, h))
                            pending_fp.append((path, mtime, str(h), d[0], d[1], arr.tobytes()))

                        now = time.monotonic()
                        if now - last_emit >= _PROGRESS_INTERVAL:
                            last_emit = now
                            self.progress.emit(done, grand_total, translate(
                                "DuplicateDetector", "Tier 1 — fingerprints {done}/{total}…"
                            ).format(done=done, total=total))
                            logger.info("Tier 1 : %d/%d empreintes calculées", done, total)
                        if pending_fp and now - last_persist >= _PROGRESS_INTERVAL:
                            last_persist = now
                            cache.store_fingerprints(pending_fp)
                            pending_fp = []
                finally:
                    # A Python thread cannot be killed cleanly from the outside (unlike a
                    # ProcessPoolExecutor, cf. FaceIndexThread), but
                    # shutdown(wait=False, cancel_futures=True) at least avoids waiting here
                    # for the ~n_workers tasks already running to finish (each one: a single,
                    # bounded image decoding) — without it, the implicit
                    # `with ThreadPoolExecutor` blocked on shutdown(wait=True) until ALL the
                    # remaining batch was done on every cancellation, one of the main
                    # contributors to the slow shutdown of the application while a duplicate
                    # detection is running (cf. MainWindow.closeEvent).
                    executor.shutdown(
                        wait=not cancelled_mid_flight, cancel_futures=cancelled_mid_flight
                    )
                if cancelled_mid_flight:
                    return
            finally:
                # Persists everything computed so far, cancellation included (the return
                # above) — that is what makes the scan resumable.
                if pending_fp:
                    cache.store_fingerprints(pending_fp)

            if self._is_cancelled():
                return

            # Phase 2: incremental grouping by Hamming distance — seeded with
            # the groups already known (self._seed_groups), then only the pairs
            # involving at least one "new" file (never compared during an earlier
            # full pass, or modified since) are evaluated. The old×old pairs are
            # not even iterated over: already checked during a previous pass and
            # unable to change as long as neither of the two files is modified.
            paths_set = set(paths)
            group_of: dict[str, int] = {
                p: gid for p, gid in self._seed_groups.items() if p in paths_set
            }
            next_group = [max(group_of.values(), default=0) + 1]

            compared_tier1 = cache.get_compared_tier1([p for p, _ in hashes])
            new_list: list[tuple[str, object]] = []
            old_list: list[tuple[str, object]] = []
            for path, h in hashes:
                cached_mtime = compared_tier1.get(path)
                if cached_mtime is not None and abs(cached_mtime - mtimes[path]) < 1.0:
                    old_list.append((path, h))
                else:
                    new_list.append((path, h))

            n = len(new_list)
            total_pairs = n * (n - 1) // 2 + n * len(old_list)
            logger.info(
                "Tier 1 : comparaison de %d empreinte(s) nouvelle(s)/modifiée(s) "
                "contre %d ancienne(s) (%d paire(s) à évaluer)…",
                n, len(old_list), total_pairs,
            )
            self.progress.emit(total, grand_total, translate(
                "DuplicateDetector", "Tier 1 — comparing fingerprints (0/{n})…"
            ).format(n=n))

            def _compare_pair(path_i, hash_i, path_j, hash_j) -> None:
                # The simplest and cheapest discriminant evaluated first (a dict
                # lookup + a comparison, no image access): two photos with known and
                # different EXIF dates are never duplicates, so there is no point
                # computing anything else for the pair.
                if _dates_differ(self._dates, path_i, path_j):
                    return
                try:
                    dist = hash_i - hash_j
                except Exception:
                    return
                if dist <= _HASH_THRESHOLD:
                    # A pHash can coincide by chance between two unrelated photos
                    # (a similar overall brightness distribution), especially when
                    # the distance falls just at the threshold — a backup check on
                    # a normalised 8x8 thumbnail (cf. _HASH_PIXEL_MAX_DIFF).
                    arr_i = micro.get(path_i)
                    arr_j = micro.get(path_j)
                    if arr_i is not None and arr_j is not None:
                        pixel_diff = float(np.abs(arr_i - arr_j).mean())
                        if pixel_diff > _HASH_PIXEL_MAX_DIFF:
                            return
                    _merge(group_of, path_i, path_j, next_group)

            # Incremental persistence of the completeness of the comparisons: rows
            # 0..i being processed in order, once row i is finished `new_list[0..i]`
            # has been compared with *all* the rest (the previous rows have each
            # covered their pairs with i). Without that milestoning, an interrupted
            # pass (closing the application) persisted nothing at all and started
            # from scratch on the next launch — that is, on a large library, an hour
            # of CPU replayed every session, indefinitely.
            #
            # The milestone deliberately stays one snapshot behind the real
            # progress: the groups formed are only persisted in the catalog
            # through `partial_results`, handled asynchronously on the UI thread.
            # Marking a row "compared" before its merges have been broadcast would
            # risk losing a group for good (the pair would never be re-evaluated).
            # By milestoning only up to the index of the *previous* snapshot, the
            # UI is left at least one full interval (_LIVE_SNAPSHOT_INTERVAL) to
            # have handled it.
            stored_upto = -1     # last index of new_list persisted
            snapshot_idx = -1    # index reached at the last partial_results

            def _checkpoint_tier1(upto: int) -> None:
                nonlocal stored_upto
                if upto <= stored_upto:
                    return
                cache.store_compared_tier1(
                    [(p, mtimes[p]) for p, _ in new_list[stored_upto + 1:upto + 1]]
                )
                stored_upto = upto

            last_emit = time.monotonic()
            last_snapshot = last_emit
            for i in range(n):
                if self._is_cancelled():
                    return
                path_i, hash_i = new_list[i]
                for j in range(i + 1, n):
                    path_j, hash_j = new_list[j]
                    _compare_pair(path_i, hash_i, path_j, hash_j)
                for path_j, hash_j in old_list:
                    _compare_pair(path_i, hash_i, path_j, hash_j)
                throttle_tick(lambda: self._cancelled)

                now = time.monotonic()
                if now - last_emit >= _PROGRESS_INTERVAL:
                    last_emit = now
                    n_groups = len({v for v in group_of.values()})
                    self.progress.emit(
                        total + int((i + 1) * total / n), grand_total,
                        translate(
                            "DuplicateDetector",
                            "Tier 1 — comparing fingerprints ({done}/{total}, {groups})…"
                        ).format(
                            done=i + 1, total=n,
                            groups=translate("DuplicateDetector", "%n group(s)",
                                             None, n_groups)),
                    )
                    logger.info("Tier 1 : %d/%d empreintes comparées (%d groupe(s) formés)",
                                i + 1, n, n_groups)
                if now - last_snapshot >= _LIVE_SNAPSHOT_INTERVAL:
                    last_snapshot = now
                    _checkpoint_tier1(snapshot_idx)
                    snapshot_idx = i
                    self.partial_results.emit(_renumber(group_of), sorted(self._corrupted))

            _checkpoint_tier1(n - 1)

            # ── Tier 2: ORB + RANSAC on the ungrouped photos ───────────────────────
            unmatched = [p for p in paths if p not in group_of]
            logger.info("Tier 1 : %d groupe(s). Tier 2 sur %d photos non groupées.",
                        len({v for v in group_of.values()}), len(unmatched))

            # An unconditional emission: makes the Tier 1 results visible (often
            # already the majority of the real duplicates) before the slower ORB
            # phase starts.
            self.partial_results.emit(_renumber(group_of), sorted(self._corrupted))

            self._detect_crops(unmatched, dims, group_of, next_group, total, grand_total, cache)

            # Final phase: renumbering (1, 2, 3…)
            self.finished.emit(_renumber(group_of))
        finally:
            # self._corrupted is, at this point (whatever the exit — normal or a
            # cancellation), the complete and up to date state: every corrupted
            # file is systematically retried on every pass (never cached in
            # fingerprints/orb_features), so a file repaired or deleted since the
            # last pass no longer appears in it. Persisted here (and not merely
            # kept in memory on the UI side) so as to survive a restart of the
            # application.
            cache.replace_corrupted_paths(self._corrupted)
            cache.close()

    # ── Tier 2: ORB + RANSAC ──────────────────────────────────────────────────

    def _detect_crops(
        self,
        unmatched: list[str],
        dims: dict[str, tuple[int, int]],
        group_of: dict[str, int],
        next_group: list[int],
        phase1_total: int,
        grand_total: int,
        cache: "DedupCache",
    ) -> None:
        """Detects cropped duplicates by ORB matching + RANSAC verification."""
        if len(unmatched) < 2:
            return

        try:
            import cv2
            import numpy as np
            from PIL import Image
        except ImportError:
            logger.warning("Tier 2 ignoré : opencv-python ou numpy non disponible")
            return

        n = len(unmatched)
        self.progress.emit(phase1_total, grand_total, translate(
            "DuplicateDetector", "Tier 2 — ORB extraction ({photos})…"
        ).format(photos=translate("DuplicateDetector", "%n photo(s)", None, n)))

        orb = cv2.ORB_create(nfeatures=_ORB_MAX_KP)
        gray_cache = _GrayImageCache()

        # ── Lazy selection: decide what to load, before loading ────────────────
        # A pair is only evaluated if at least one of its two members is new or
        # modified since the last full pass (the old×old pairs are skipped
        # further down) and the area ratio prefilter does not rule it out right
        # away. Both criteria depend on metadata alone (mtime, dimensions):
        # evaluating them *before* touching the features avoids rebuilding the
        # whole library on every start only to compare no pair at all — on
        # 65,000 photos, several GB read from the database, one JPEG decoding
        # and ~300 cv2.KeyPoint objects per photo, systematically, in the
        # nominal case where nothing has changed.
        orb_meta = cache.get_orb_meta(unmatched)
        compared_tier2 = cache.get_compared_tier2(unmatched)

        mtimes2: dict[str, float] = {}
        cached_paths: list[str] = []
        to_compute: list[str] = []
        old_paths_set: set[str] = set()
        for path in unmatched:
            try:
                current_mtime = os.path.getmtime(path)
            except OSError:
                continue  # gone since Tier 1
            mtimes2[path] = current_mtime
            meta = orb_meta.get(path)
            if meta is not None and abs(meta[0] - current_mtime) < 1.0:
                cached_paths.append(path)
            else:
                to_compute.append(path)
            cached_cmp = compared_tier2.get(path)
            if cached_cmp is not None and abs(cached_cmp - current_mtime) < 1.0:
                old_paths_set.add(path)

        new_paths_set = {p for p in mtimes2 if p not in old_paths_set}
        if not new_paths_set:
            logger.info(
                "Tier 2 : aucune photo nouvelle ou modifiée parmi les %d non groupées, "
                "aucune paire à évaluer.", n,
            )
            return

        # The area of each photo, known without loading anything: the real
        # dimensions recorded by Tier 1, or the ones memorised with the features.
        def _area_of(path: str) -> int:
            d = dims.get(path)
            if d is not None:
                return d[0] * d[1]
            meta = orb_meta.get(path)
            return meta[1] * meta[2] if meta is not None else 0

        areas = {path: _area_of(path) for path in mtimes2}

        # An old photo is only useful if its area falls inside the [a/F, a·F]
        # window of at least one new photo — otherwise the area ratio prefilter
        # of the comparison loop would rule out each of its pairs anyway. An
        # unknown area (0): the loop then disables the prefilter, so everything
        # becomes a candidate and nothing can be excluded.
        new_areas = sorted(areas[p] for p in new_paths_set)
        keep_everything = new_areas and new_areas[0] <= 0

        def _is_needed(path: str) -> bool:
            if keep_everything or path in new_paths_set:
                return True
            a = areas[path]
            if a <= 0:
                return True
            lo = bisect.bisect_left(new_areas, a / _ORB_AREA_FACTOR)
            hi = bisect.bisect_right(new_areas, a * _ORB_AREA_FACTOR)
            return lo < hi

        cached_paths = [p for p in cached_paths if _is_needed(p)]
        to_compute = [p for p in to_compute if _is_needed(p)]

        # ── Loading the features of the selected photos only ───────────────────
        desc_list: list[tuple[str, object, object, int]] = []  # (path, pts, des, area)
        cached_orb = cache.get_orb_descriptors(cached_paths)
        for path in cached_paths:
            row = cached_orb.get(path)
            if row is not None:
                _mtime, w, h, kp_blob, des_blob = row
                try:
                    des = np.frombuffer(des_blob, dtype=np.uint8).reshape(-1, 32)
                    pts = np.frombuffer(kp_blob, dtype=np.float32).reshape(-1, 2)
                except Exception as exc:
                    logger.debug("dedup_cache : ligne ORB corrompue pour %s (%s), recalcul.",
                                 path, exc)
                else:
                    if des.shape[0] > 0 and len(pts) == des.shape[0]:
                        desc_list.append((path, pts, des, w * h))
                        continue
            to_compute.append(path)

        cache_hits = len(desc_list)
        total_needed = cache_hits + len(to_compute)
        logger.info(
            "Tier 2 : %d/%d photos concernées par au moins une paire à évaluer "
            "(%d features réutilisées du cache, %d à calculer)…",
            total_needed, n, cache_hits, len(to_compute),
        )

        pending_orb: list[tuple] = []
        try:
            for i, path in enumerate(to_compute):
                if self._is_cancelled():
                    return
                idx = cache_hits + i
                if idx % 10 == 0:
                    self.progress.emit(
                        phase1_total
                        + idx * (grand_total - phase1_total) // (max(1, total_needed) * 2),
                        grand_total,
                        translate("DuplicateDetector",
                                  "Tier 2 — ORB descriptors {done}/{total}…"
                                  ).format(done=idx, total=total_needed),
                    )
                    if pending_orb:
                        cache.store_orb_features(pending_orb)
                        pending_orb = []
                try:
                    img = _load_gray(path, _ORB_LOAD_SIZE)
                    if img is None:
                        # Cf. the equivalent comment in Tier 1: a file deleted in the
                        # meantime fails to open like a corrupted file.
                        if os.path.exists(path):
                            logger.warning("Fichier illisible (Tier 2) : %s", path)
                            self._corrupted.add(path)
                        else:
                            logger.debug(
                                "Fichier disparu avant le calcul ORB (Tier 2) : %s", path
                            )
                        continue
                    mtime = os.path.getmtime(path)
                    kp, des = orb.detectAndCompute(img, None)
                    if des is None or len(kp) < 10:
                        continue
                    if path in dims:
                        w, h = dims[path]
                    else:
                        # Tier 1 could not open this file (Image.open failed): retry with
                        # PIL to get the real resolution rather than mixing a reduced area
                        # (_load_gray, 800 px max) with the real areas of the other photos
                        # — that would skew the _ORB_AREA_FACTOR prefilter sorted by area
                        # (a premature break becomes possible).
                        try:
                            with Image.open(path) as pil_img:
                                w, h = pil_img.size
                        except Exception:
                            w, h = img.shape[1], img.shape[0]
                    mtimes2[path] = mtime
                    # Only the coordinates of the keypoints are kept: the rest of the
                    # algorithm only reads `.pt` (cf. _compare_chunk), and an (N,2)
                    # float32 array advantageously replaces ~300 cv2.KeyPoint objects
                    # per photo — which the cache had to rebuild one by one on every
                    # start anyway.
                    pts = np.array([k.pt for k in kp], dtype=np.float32)
                    desc_list.append((path, pts, des, w * h))
                    pending_orb.append((path, mtime, w, h, pts.tobytes(), des.tobytes()))
                except Exception as exc:
                    logger.debug("ORB descripteur échoué %s : %s", os.path.basename(path), exc)
                throttle_tick(lambda: self._cancelled)
        finally:
            # Persists everything computed so far, cancellation included (the return
            # above) — that is what makes the scan resumable.
            if pending_orb:
                cache.store_orb_features(pending_orb)

        m = len(desc_list)
        if m < 2:
            return

        self.progress.emit(
            phase1_total + (grand_total - phase1_total) // 2,
            grand_total,
            translate("DuplicateDetector", "Tier 2 — ORB comparison ({photos})…"
                      ).format(photos=translate("DuplicateDetector",
                                                "%n photo(s)", None, m)),
        )

        # `old_paths_set` / `new_paths_set` were established above, over the set
        # of ungrouped photos and no longer over `desc_list` — the latter now only
        # contains the photos really involved in a pair to evaluate. Only the
        # paths actually present in desc_list can be marked "compared" at the end
        # of the pass.
        new_paths_set &= {path for path, *_ in desc_list}

        # Sorted by increasing area: speeds the prefilter up (similar photos close together)
        desc_list.sort(key=lambda x: x[3])

        pairs_checked = 0
        logger.info("Tier 2 : comparaison ORB de %d photos non groupées par le Tier 1.", m)

        # Comparing the pairs of one same row `i` in parallel: the cv2 calls
        # (knnMatch/findHomography) release the GIL, so a thread pool brings a real
        # gain without the serialisation cost of a process pool. Each task handles a
        # *chunk* of candidates rather than a single pair: a pair usually compares in
        # far less than a millisecond, and submitting one task per pair makes the
        # dispatch cost of the ThreadPoolExecutor dominate the gain of the
        # parallelism (measured: only ~10 % of gain instead of a gain close to the
        # number of cores). Splitting into `n_workers` chunks amortises that cost
        # while keeping every core busy. The merges (_merge) are still applied
        # sequentially on this thread once the results of the row are known — _merge
        # is order-independent within a row (every call shares path_i), so
        # parallelising never changes the final result (only, potentially, the
        # intermediate group ids, renumbered at the end of _detect() anyway).
        def _compare_chunk(items, des_i=None, pts_i=None, path_i=""):
            # A BFMatcher local to the worker rather than an instance shared
            # between the threads of the pool: the thread-safety of
            # cv2.BFMatcher.knnMatch() is not documented by OpenCV, and a shared
            # native object called concurrently is a plausible candidate for a hard
            # crash (a segfault), hence not catchable by a Python try/except. The
            # creation cost is negligible next to the work of the chunk (several ORB
            # comparisons).
            local_bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
            results = []
            for path_j, pts_j, des_j in items:
                # Reading the flag directly (not _is_cancelled(), which emits the
                # `cancelled` signal on every call): it allows interrupting a chunk
                # item by item rather than waiting for it to finish — a chunk can
                # hold dozens of candidates, each potentially costing several dozen
                # ms (knnMatch + RANSAC + warpPerspective + absdiff), and the
                # enclosing `with ThreadPoolExecutor(...)` waits (wait=True on
                # leaving the block) for the tasks already running to finish before
                # handing control back to closeEvent — without this safeguard,
                # closing the application during Tier 2 could stay blocked several
                # seconds longer than necessary.
                if self._cancelled:
                    break
                throttle_tick(lambda: self._cancelled)
                # The simplest and cheapest discriminant first (a dict lookup),
                # before the whole ORB pipeline (knnMatch + RANSAC +
                # warpPerspective + absdiff), by far the most expensive of the
                # entire detection — two photos with known and different EXIF
                # dates are never duplicates.
                if _dates_differ(self._dates, path_i, path_j):
                    results.append((path_j, False))
                    continue
                # The whole body of the comparison (not just knnMatch) is
                # protected: a degenerate geometry (e.g. a nearly singular
                # homography) can make any downstream cv2 call fail
                # (findHomography, warpPerspective, absdiff) — a pair in error
                # must neither crash the whole chunk nor cancel the entire scan
                # (cf. run(), which would otherwise turn that into an error
                # message for the user instead of a plain "no match" for that
                # pair).
                try:
                    raw_matches = local_bf.knnMatch(des_i, des_j, k=2)

                    good = [
                        m1 for pair in raw_matches if len(pair) == 2
                        for m1, m2 in (pair,)
                        if m1.distance < _ORB_RATIO_TEST * m2.distance
                    ]
                    if len(good) < _ORB_GOOD_MIN:
                        results.append((path_j, False))
                        continue

                    src_pts = pts_i[[m1.queryIdx for m1 in good]].reshape(-1, 1, 2)
                    dst_pts = pts_j[[m1.trainIdx for m1 in good]].reshape(-1, 1, 2)

                    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                    if mask is None:
                        results.append((path_j, False))
                        continue

                    inliers = int(mask.sum())
                    if inliers < _ORB_MIN_INLIERS:
                        results.append((path_j, False))
                        continue

                    # A rich, static background (e.g. a burst of photos) can on its
                    # own provide enough consistent inliers even when the real
                    # subject differs completely — the number of inliers is not
                    # enough to guarantee that the photos really do look alike. An
                    # extra check: register j onto i through the homography found
                    # and require the pixels to agree over the overlap area
                    # (cf. _ORB_MAX_MEAN_DIFF).
                    #
                    # The two working images are only decoded here, at the single
                    # place that needs them: reaching it means having already passed
                    # Lowe's ratio test *and* the RANSAC inlier threshold, which
                    # only genuine near-duplicates do — a tiny minority of the pairs
                    # evaluated (cf. _GrayImageCache).
                    img_i = gray_cache.get(path_i)
                    img_j = gray_cache.get(path_j)
                    if img_i is None or img_j is None:
                        results.append((path_j, False))
                        continue

                    h_i, w_i = img_i.shape[:2]
                    warped = cv2.warpPerspective(img_j, H, (w_i, h_i))
                    valid = cv2.warpPerspective(
                        np.full(img_j.shape, 255, dtype=np.uint8), H, (w_i, h_i)
                    ) > 0
                    if not valid.any():
                        results.append((path_j, False))
                        continue

                    mean_diff = float(cv2.absdiff(warped, img_i)[valid].mean())
                    results.append((path_j, mean_diff <= _ORB_MAX_MEAN_DIFF))
                except Exception as exc:
                    logger.debug("Tier 2 comparaison échouée %s ↔ %s : %s",
                                os.path.basename(path_i), os.path.basename(path_j), exc)
                    results.append((path_j, False))
            return results

        n_workers = throttled_worker_count()
        comparison_start = phase1_total + (grand_total - phase1_total) // 2
        last_emit = time.monotonic()
        last_snapshot = last_emit
        executor = ThreadPoolExecutor(
            max_workers=n_workers, initializer=lower_current_thread_priority
        )
        cancelled_mid_flight = False
        # Milestoning of the completeness of the comparisons, the same reasoning
        # as in Tier 1 (`_checkpoint_tier1`): row i handled, rows 0..i have
        # covered all of their pairs; only up to the index of the previous
        # snapshot is persisted, though, to give the UI time to have written the
        # corresponding groups into the catalog.
        stored_upto2 = -1
        snapshot_idx2 = -1

        def _checkpoint_tier2(upto: int) -> None:
            nonlocal stored_upto2
            if upto <= stored_upto2:
                return
            rows = [
                (path, mtimes2[path])
                for path, *_ in desc_list[stored_upto2 + 1:upto + 1]
                if path in new_paths_set
            ]
            cache.store_compared_tier2(rows)
            stored_upto2 = upto

        try:
            for i in range(m):
                if self._is_cancelled():
                    cancelled_mid_flight = True
                    break
                path_i, pts_i, des_i, area_i = desc_list[i]

                now = time.monotonic()
                if now - last_emit >= _PROGRESS_INTERVAL:
                    last_emit = now
                    current = comparison_start + int((i + 1) * (grand_total - comparison_start) / m)
                    self.progress.emit(
                        current, grand_total,
                        translate(
                            "DuplicateDetector",
                            "Tier 2 — ORB comparison ({done}/{total}, {pairs})…"
                        ).format(
                            done=i + 1, total=m,
                            pairs=translate("DuplicateDetector",
                                            "%n pair(s) checked",
                                            None, pairs_checked)),
                    )
                    logger.info("Tier 2 : %d/%d photos comparées (%d paire(s) vérifiée(s))",
                                i + 1, m, pairs_checked)
                if now - last_snapshot >= _LIVE_SNAPSHOT_INTERVAL:
                    last_snapshot = now
                    _checkpoint_tier2(snapshot_idx2)
                    snapshot_idx2 = i
                    self.partial_results.emit(_renumber(group_of), sorted(self._corrupted))

                candidates = []
                for j in range(i + 1, m):
                    path_j, pts_j, des_j, area_j = desc_list[j]

                    # Area ratio prefilter: early exit (sorted list — area_j ≥ area_i)
                    if area_i > 0 and area_j / area_i > _ORB_AREA_FACTOR:
                        break

                    # An old×old pair: already fully checked during an earlier full
                    # pass, and neither of the two has changed since.
                    if path_i in old_paths_set and path_j in old_paths_set:
                        continue

                    # No point rematching two photos already in the same group
                    gi, gj = group_of.get(path_i), group_of.get(path_j)
                    if gi is not None and gi == gj:
                        continue

                    candidates.append((path_j, pts_j, des_j))

                if not candidates:
                    continue

                pairs_checked += len(candidates)
                chunk_size = max(1, -(-len(candidates) // n_workers))  # ceil division
                chunks = [candidates[k:k + chunk_size] for k in range(0, len(candidates), chunk_size)]
                worker = partial(_compare_chunk, des_i=des_i, pts_i=pts_i, path_i=path_i)
                futures = [executor.submit(worker, chunk) for chunk in chunks]
                for future in futures:
                    if self._is_cancelled():
                        for f in futures:
                            f.cancel()
                        cancelled_mid_flight = True
                        break
                    try:
                        chunk_result = future.result()
                    except Exception as exc:
                        # _compare_chunk already protects each pair individually; this
                        # extra safeguard keeps an unexpected failure of a whole chunk
                        # (e.g. a MemoryError) from interrupting the entire scan for the
                        # remaining photos.
                        logger.warning("Tier 2 : lot de comparaison échoué pour %s : %s",
                                      os.path.basename(path_i), exc)
                        continue
                    for path_j, matched in chunk_result:
                        if matched:
                            logger.debug(
                                "Tier 2 crop détecté : %s ↔ %s",
                                os.path.basename(path_i), os.path.basename(path_j),
                            )
                            _merge(group_of, path_i, path_j, next_group)
                if cancelled_mid_flight:
                    break
        finally:
            # Cf. the equivalent comment in Tier 1: a Python thread cannot be killed
            # from the outside, but shutdown(wait=False, cancel_futures=True) avoids
            # waiting here for the tasks already running — each of them already exits
            # quickly through `if self._cancelled: break` in _compare_chunk (item by
            # item), so this non-blocking call is mostly an extra guarantee (e.g. if a
            # single cv2 call is running and takes longer than expected) rather than
            # the main gain, unlike in Tier 1.
            executor.shutdown(wait=not cancelled_mid_flight, cancel_futures=cancelled_mid_flight)
        if cancelled_mid_flight:
            return

        logger.info("Tier 2 : %d paire(s) vérifiées par ORB/RANSAC.", pairs_checked)
        _checkpoint_tier2(m - 1)


# ── HTML report ────────────────────────────────────────────────────────────────

def generate_html_report(groups: dict, output_path: str) -> None:
    """Generates an HTML report listing every duplicate group."""
    n_groups = len(groups)
    n_files  = sum(len(v) for v in groups.values())
    now      = datetime.now().strftime(
        translate("DuplicateReport", "%m/%d/%Y at %H:%M"))

    lines = [
        "<!DOCTYPE html>",
        # `lang` must follow the language of the content (screen readers, browser
        # hyphenation): it is a language code, not a translatable string.
        f"<html lang='{active_language()}'>",
        "<head>",
        "<meta charset='UTF-8'>",
        "<title>" + translate("DuplicateReport",
                              "Duplicates — PixelPhotoManager") + "</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;background:#1a1a1a;color:#ccc;"
        "     margin:0;padding:24px}",
        "h1{color:#fff;font-size:1.3em;margin:0 0 6px}",
        ".summary{background:#2b2b2b;border-radius:8px;padding:12px 20px;"
        "         margin-bottom:24px;font-size:.9em}",
        ".group{background:#242424;border:1px solid #333;border-radius:8px;"
        "       padding:14px 18px;margin-bottom:18px}",
        ".gtitle{color:#ffa040;font-weight:bold;margin-bottom:10px}",
        ".file{display:grid;grid-template-columns:1fr auto;gap:8px;"
        "      padding:6px 0;border-bottom:1px solid #2f2f2f}",
        ".file:last-child{border-bottom:none}",
        ".fname{color:#e0e0e0;font-size:.92em}",
        ".fpath{color:#666;font-size:.80em;word-break:break-all}",
        ".fsize{color:#888;font-size:.85em;white-space:nowrap;align-self:center}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>" + translate("DuplicateReport",
                           "Duplicate report — PixelPhotoManager") + "</h1>",
        "<div class='summary'>"
        + translate("DuplicateReport", "Generated on {date}").format(date=now)
        + " &nbsp;·&nbsp; "
        + translate("DuplicateReport", "<b>%n</b> duplicate group(s)",
                    None, n_groups)
        + " &nbsp;·&nbsp; "
        + translate("DuplicateReport", "<b>%n</b> file(s) affected",
                    None, n_files)
        + "</div>",
    ]

    for gid, members in groups.items():
        lines.append("<div class='group'>")
        n_members = len(members)
        lines.append(
            "<div class='gtitle'>"
            + translate("DuplicateReport", "Group&nbsp;#{id}").format(id=gid)
            + " — "
            + translate("DuplicateReport", "%n file(s)", None, n_members)
            + "</div>"
        )
        for path in members:
            fname = os.path.basename(path)
            fdir  = os.path.dirname(path)
            try:
                fsize = _fmt_size(os.path.getsize(path))
            except OSError:
                fsize = "—"
            lines.append(
                f"<div class='file'>"
                f"<div><div class='fname'>{fname}</div>"
                f"<div class='fpath'>{fdir}</div></div>"
                f"<div class='fsize'>{fsize}</div>"
                f"</div>"
            )
        lines.append("</div>")

    lines.append("</body></html>")
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")


def _fmt_size(n: int) -> str:
    # The complete label (number + unit) is translatable: English and German
    # write "kB/MB", not "Ko/Mo" (octets). Source and context must stay
    # literals on the spot (lupdate reads the code, it does not execute it):
    # no loop over a list of units here.
    if n < 1024:
        return translate("Units", "{n}&nbsp;B").format(n=n)
    n //= 1024
    if n < 1024:
        return translate("Units", "{n}&nbsp;kB").format(n=n)
    n //= 1024
    if n < 1024:
        return translate("Units", "{n}&nbsp;MB").format(n=n)
    n //= 1024
    if n < 1024:
        return translate("Units", "{n}&nbsp;GB").format(n=n)
    return translate("Units", "{n}&nbsp;TB").format(n=n // 1024)
