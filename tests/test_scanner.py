# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/library/scanner.py` with real `ScanThread.start()` calls (qtbot)
on temporary trees: discovery of the supported files, exclusions (hidden,
Originals, *_assets, thumbnails), incremental scan by mtime, forced rescan,
purge of the ghost entries, video/corrupted files, the `_is_hidden` helper and
the `LibraryScanner` life cycle."""
import os
import subprocess

import pytest
from PIL import Image

from src.library.catalog import Catalog
from src.library.scanner import LibraryScanner, ScanThread, _is_hidden, SUPPORTED_EXT
from src.library.thumbnail_cache import ThumbnailCache


def _make_jpg(path, size=(32, 24)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=(100, 150, 200)).save(str(path))


@pytest.fixture
def env(tmp_path):
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
    photos = tmp_path / "photos"
    photos.mkdir()
    return catalog, cache, photos


def _run_scan(qtbot, catalog, cache, folders, force=False):
    """Runs a ScanThread synchronously (direct run(): the signals are emitted
    through direct connections, and coverage traces the code -- a real .start()
    in a native Qt thread escapes sys.settrace). The real cross-thread path is
    covered by TestLibraryScanner.test_scan_lifecycle."""
    thread = ScanThread(folders, catalog, cache, force=force)
    batches: list[list] = []
    removed: list[list] = []
    totals: list[int] = []
    thread.photos_batch.connect(lambda b: batches.append(list(b)))
    thread.photos_removed.connect(lambda r: removed.append(list(r)))
    thread.finished.connect(totals.append)
    thread.run()
    return batches, removed, totals[0]


class TestIsHidden:
    def test_dot_prefix(self, tmp_path):
        p = tmp_path / ".caché.jpg"
        p.write_bytes(b"x")
        assert _is_hidden(str(p)) is True

    def test_normal_file(self, tmp_path):
        p = tmp_path / "normal.jpg"
        p.write_bytes(b"x")
        assert _is_hidden(str(p)) is False

    def test_windows_hidden_attribute(self, tmp_path):
        p = tmp_path / "attrib_cache.jpg"
        p.write_bytes(b"x")
        subprocess.run(["attrib", "+h", str(p)], check=True, shell=True)
        assert _is_hidden(str(p)) is True

    def test_nonexistent_path(self, tmp_path):
        assert _is_hidden(str(tmp_path / "absent.jpg")) is False


class TestScanDiscovery:
    def test_supported_images_found(self, qtbot, env):
        catalog, cache, photos = env
        _make_jpg(photos / "a.jpg")
        _make_jpg(photos / "b.png")
        _make_jpg(photos / "sub" / "c.jpg")
        (photos / "notes.txt").write_text("pas une image")

        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])

        assert total == 3
        all_photos = [p for b in batches for p in b]
        assert len(all_photos) == 3
        assert all(p.media_type == "image" for p in all_photos)
        assert all(p.width == 32 and p.height == 24 for p in all_photos)
        assert len(catalog.get_all_paths_under(str(photos))) == 3

    def test_excluded_directories(self, qtbot, env):
        catalog, cache, photos = env
        _make_jpg(photos / "visible.jpg")
        _make_jpg(photos / "Originals" / "orig.jpg")
        _make_jpg(photos / "site_assets" / "asset.jpg")
        _make_jpg(photos / "Thumbnails" / "thumb.jpg")
        hidden_dir = photos / ".hidden"
        _make_jpg(hidden_dir / "h.jpg")

        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])

        assert total == 1
        paths = catalog.get_all_paths_under(str(photos))
        assert all("visible.jpg" in p for p in paths)

    def test_dvd_copy_vob_cataloged_as_video(self, qtbot, env):
        """A DVD copy (VIDEO_TS/AUDIO_TS) is walked like any other folder: the
        .VOB files are real videos (VIDEO_EXT), catalogued with
        media_type="video"; .IFO/.BUP (navigation metadata, not media) stay
        ignored for lack of a supported extension, with no dedicated folder
        exclusion."""
        catalog, cache, photos = env
        _make_jpg(photos / "visible.jpg")
        dvd = photos / "MonDVD"
        (dvd / "VIDEO_TS").mkdir(parents=True)
        (dvd / "VIDEO_TS" / "VTS_01_1.VOB").write_bytes(b"x")
        (dvd / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"x")
        (dvd / "AUDIO_TS").mkdir(parents=True)

        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])

        assert total == 2
        all_photos = [p for b in batches for p in b]
        vob = next(p for p in all_photos if p.path.lower().endswith(".vob"))
        assert vob.media_type == "video"
        paths = catalog.get_all_paths_under(str(photos))
        assert not any(p.lower().endswith(".ifo") for p in paths)

    def test_hidden_file_excluded(self, qtbot, env):
        catalog, cache, photos = env
        _make_jpg(photos / ".secret.jpg")
        _make_jpg(photos / "ok.jpg")
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 1

    def test_video_file_gets_video_media_type(self, qtbot, env):
        catalog, cache, photos = env
        # invalid content: VideoMetadataReader must fall back on width=0 and
        # date=mtime without crashing, and the scanner must type it as "video"
        (photos / "clip.mp4").write_bytes(b"pas une vraie video")
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 1
        video = batches[0][0]
        assert video.media_type == "video"
        assert video.duration == 0.0
        assert video.date_taken is not None  # mtime of the file

    def test_corrupt_image_still_indexed(self, qtbot, env):
        catalog, cache, photos = env
        (photos / "corrompue.jpg").write_bytes(b"pas un jpeg")
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 1
        assert batches[0][0].width == 0

    def test_raw_file_discovered_when_rawpy_available(self, qtbot, env):
        from src.library.image_loader import is_raw_available
        if not is_raw_available():
            pytest.skip("rawpy non installé")
        catalog, cache, photos = env
        (photos / "photo.cr2").write_bytes(b"pas un vrai CR2")
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 1
        assert batches[0][0].media_type == "image"

    def test_heic_file_discovered(self, qtbot, env):
        catalog, cache, photos = env
        (photos / "photo.heic").write_bytes(b"pas un vrai HEIC")
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 1

    def test_empty_folder(self, qtbot, env):
        catalog, cache, photos = env
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 0
        assert batches == []

    def test_batching_over_batch_size(self, qtbot, env):
        """55 files -> 2 photos_batch emissions (50 + 5)."""
        catalog, cache, photos = env
        for i in range(55):
            _make_jpg(photos / f"p{i:03d}.jpg", size=(4, 4))
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 55
        assert [len(b) for b in batches] == [50, 5]


class TestIncrementalScan:
    def test_unchanged_files_skipped(self, qtbot, env):
        catalog, cache, photos = env
        _make_jpg(photos / "a.jpg")
        _make_jpg(photos / "b.jpg")
        _run_scan(qtbot, catalog, cache, [str(photos)])

        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 0
        assert batches == []

    def test_force_rescans_everything(self, qtbot, env):
        catalog, cache, photos = env
        _make_jpg(photos / "a.jpg")
        _run_scan(qtbot, catalog, cache, [str(photos)])

        batches, removed, total = _run_scan(
            qtbot, catalog, cache, [str(photos)], force=True
        )
        assert total == 1

    def test_modified_file_rescanned(self, qtbot, env):
        catalog, cache, photos = env
        p = photos / "a.jpg"
        _make_jpg(p)
        _run_scan(qtbot, catalog, cache, [str(photos)])

        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 10))
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])
        assert total == 1

    def test_deleted_file_purged_from_catalog(self, qtbot, env):
        catalog, cache, photos = env
        keep = photos / "keep.jpg"
        gone = photos / "gone.jpg"
        _make_jpg(keep)
        _make_jpg(gone)
        _run_scan(qtbot, catalog, cache, [str(photos)])
        assert len(catalog.get_all_paths_under(str(photos))) == 2

        os.remove(gone)
        batches, removed, total = _run_scan(qtbot, catalog, cache, [str(photos)])

        assert len(removed) == 1
        assert removed[0] == [os.path.normpath(str(gone))]
        paths = catalog.get_all_paths_under(str(photos))
        assert paths == {os.path.normpath(str(keep))}


class TestStopFlag:
    def test_stopped_thread_does_no_removal(self, env):
        """Synchronous run() with stop_flag already raised: discovery
        interrupted, no purge of the ghost entries (a partial scan must not
        delete valid entries)."""
        catalog, cache, photos = env
        _make_jpg(photos / "a.jpg")
        # photo known to the catalog but absent from the scan (simulates a partial scan)
        from src.core.models import PhotoInfo
        ghost = os.path.normpath(str(photos / "ghost.jpg"))
        catalog.add_or_update_photo(PhotoInfo(path=ghost, file_size=1, file_mtime=1.0))

        thread = ScanThread([str(photos)], catalog, cache)
        removed: list = []
        totals: list[int] = []
        thread.photos_removed.connect(removed.append)
        thread.finished.connect(totals.append)
        thread.stop()
        thread.run()  # synchronous execution in this thread

        assert totals == [0]
        assert removed == []
        # the ghost entry has not been purged
        assert ghost in catalog.get_all_paths_under(str(photos))


class TestLibraryScanner:
    def test_scan_lifecycle(self, qtbot, env):
        catalog, cache, photos = env
        _make_jpg(photos / "a.jpg")
        scanner = LibraryScanner(catalog, cache)
        thread = scanner.scan([str(photos)])
        assert thread is not None
        qtbot.waitUntil(lambda: not scanner.is_scanning, timeout=15000)
        assert len(catalog.get_all_paths_under(str(photos))) == 1

    def test_stop_idempotent_without_thread(self, env):
        catalog, cache, photos = env
        scanner = LibraryScanner(catalog, cache)
        scanner.stop()          # no thread: must not raise
        scanner.request_stop()
        scanner.wait_stopped()
        assert scanner.is_scanning is False

    def test_new_scan_stops_previous(self, qtbot, env):
        catalog, cache, photos = env
        _make_jpg(photos / "a.jpg")
        scanner = LibraryScanner(catalog, cache)
        t1 = scanner.scan([str(photos)])
        t2 = scanner.scan([str(photos)])
        assert t2 is not t1
        qtbot.waitUntil(lambda: not scanner.is_scanning, timeout=15000)


class TestSupportedExt:
    def test_images_and_videos_merged(self):
        assert ".jpg" in SUPPORTED_EXT
        assert ".mp4" in SUPPORTED_EXT
        assert ".txt" not in SUPPORTED_EXT

    def test_heic_always_supported(self):
        assert ".heic" in SUPPORTED_EXT
        assert ".heif" in SUPPORTED_EXT

    def test_raw_supported_only_when_rawpy_available(self):
        from src.library.image_loader import is_raw_available, RAW_EXT
        if is_raw_available():
            assert RAW_EXT <= SUPPORTED_EXT
        else:
            assert not (RAW_EXT & SUPPORTED_EXT)
