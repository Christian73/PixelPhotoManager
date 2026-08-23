# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of the session cache of person icons (Sidebar): refresh_persons must
only re-decode from the originals the covers absent from the cache, and
persons_thumbnails_ready must be emitted in every case (it gates the start of
duplicate detection, cf. main_window)."""
from src.core.models import PersonInfo
from src.ui.sidebar import Sidebar


def _person(pid: int, name: str, cover: str | None = None) -> PersonInfo:
    p = PersonInfo(id=pid, name=name)
    if cover:
        p.cover_path = cover
        p.cover_bbox = (0, 0, 50, 50)
    return p


def _make_sidebar(qtbot) -> Sidebar:
    sb = Sidebar()
    qtbot.addWidget(sb)
    return sb


class TestIconCache:
    def test_all_cached_emits_ready_without_loader(self, qtbot):
        sb = _make_sidebar(qtbot)
        persons = [_person(1, "Alice", "C:/photos/a.jpg")]
        # Pre-fills the cache as if the first load had taken place
        sb._icon_bytes_cache[Sidebar._icon_cache_key(persons[0])] = b"png"

        with qtbot.waitSignal(sb.persons_thumbnails_ready, timeout=1000):
            sb.refresh_persons(persons)

        # Everything came from the cache: no loader started
        assert sb._face_loader is None

    def test_no_persons_emits_ready(self, qtbot):
        sb = _make_sidebar(qtbot)
        with qtbot.waitSignal(sb.persons_thumbnails_ready, timeout=1000):
            sb.refresh_persons([])
        assert sb._face_loader is None

    def test_uncached_cover_starts_loader(self, qtbot):
        sb = _make_sidebar(qtbot)
        persons = [_person(1, "Alice", "C:/photos/a.jpg")]

        with qtbot.waitSignal(sb.persons_thumbnails_ready, timeout=2000):
            sb.refresh_persons(persons)

        # A loader was indeed created for the missing cover (the decoding
        # fails silently, the file does not exist -- only the flow matters to us
        # here: ready emitted at the end of the loader).
        assert sb._face_loader is not None
        sb._face_loader.wait(2000)   # thread really finished before teardown

    def test_icon_ready_feeds_cache(self, qtbot):
        sb = _make_sidebar(qtbot)
        persons = [_person(1, "Alice", "C:/photos/a.jpg")]
        # Wait for the loader to finish: destroying the Sidebar with a
        # _FaceIconLoader still in flight blocks the teardown of the test.
        with qtbot.waitSignal(sb.persons_thumbnails_ready, timeout=2000):
            sb.refresh_persons(persons)
        if sb._face_loader is not None:
            sb._face_loader.wait(2000)

        # a valid 1x1 PNG
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
            b"\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8"
            b"\xcf\xc0\x00\x00\x00\x03\x00\x01\x8e\xb1\xf3\xf4\x00\x00\x00\x00"
            b"IEND\xaeB`\x82"
        )
        sb._on_face_icon_ready(0, png)

        key = Sidebar._icon_cache_key(persons[0])
        assert sb._icon_bytes_cache.get(key) == png

    def test_stale_entries_pruned_on_refresh(self, qtbot):
        sb = _make_sidebar(qtbot)
        sb._icon_bytes_cache[("C:/photos/gone.jpg", (0, 0, 10, 10))] = b"old"
        persons = [_person(1, "Alice", "C:/photos/a.jpg")]
        sb._icon_bytes_cache[Sidebar._icon_cache_key(persons[0])] = b"png"

        sb.refresh_persons(persons)

        assert ("C:/photos/gone.jpg", (0, 0, 10, 10)) not in sb._icon_bytes_cache
        assert sb._icon_bytes_cache[Sidebar._icon_cache_key(persons[0])] == b"png"
