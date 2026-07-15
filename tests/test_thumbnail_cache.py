# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests des méthodes non-Qt de src/library/thumbnail_cache.py (ThumbnailCache) :
generate(), get_bytes(), move_photo(), invalidate(), invalidate_many(), _key().

Volontairement hors scope : get()/get_ram()/store_pixmap()/_store_ram()/
_get_from_db(), qui créent des QPixmap et nécessitent un contexte Qt/QApplication
(cf. plan round 3, Phase E4)."""
import os
import sqlite3
import time

from PIL import Image

from src.core.models import EditInfo
from src.library.thumbnail_cache import ThumbnailCache


def _make_photo(tmp_path, name="a.jpg", size=(64, 48), color=(200, 50, 50)):
    path = tmp_path / name
    Image.new("RGB", size, color).save(path, format="JPEG")
    return str(path)


def _make_cache(tmp_path):
    return ThumbnailCache(db_path=tmp_path / "thumbnails.db")


class TestKey:
    def test_same_path_gives_same_key(self):
        assert ThumbnailCache._key("C:/photos/a.jpg") == ThumbnailCache._key("C:/photos/a.jpg")

    def test_different_paths_give_different_keys(self):
        assert ThumbnailCache._key("a.jpg") != ThumbnailCache._key("b.jpg")


class TestGenerate:
    def test_generate_returns_jpeg_bytes_and_persists_to_db(self, tmp_path):
        cache = _make_cache(tmp_path)
        photo = _make_photo(tmp_path)

        data = cache.generate(photo)

        assert data is not None
        assert data[:2] == b"\xff\xd8"  # marqueur JPEG
        with Image.open(__import__("io").BytesIO(data)) as thumb:
            assert thumb.size[0] <= cache.THUMB_SIZE[0]
            assert thumb.size[1] <= cache.THUMB_SIZE[1]

        conn = sqlite3.connect(cache._db_path)
        row = conn.execute(
            "SELECT thumbnail_data FROM thumbnails WHERE photo_hash=?",
            (ThumbnailCache._key(photo),),
        ).fetchone()
        conn.close()
        assert row is not None
        assert bytes(row[0]) == data

    def test_generate_returns_none_for_missing_file(self, tmp_path):
        cache = _make_cache(tmp_path)
        assert cache.generate(str(tmp_path / "does_not_exist.jpg")) is None

    def test_generate_applies_edit_when_modified(self, tmp_path):
        cache = _make_cache(tmp_path)
        photo = _make_photo(tmp_path, color=(128, 128, 128))

        data = cache.generate(photo, edit=EditInfo(brightness=0.9))

        assert data is not None

    def test_generate_video_extension_dispatches_to_video_thumb_and_fails_gracefully(self, tmp_path):
        """Un .mp4 illisible (pas une vraie vidéo) doit échouer proprement (None),
        sans lever — confirme juste le dispatch d'extension, pas le happy path cv2."""
        cache = _make_cache(tmp_path)
        fake_video = tmp_path / "clip.mp4"
        fake_video.write_bytes(b"not a real video")

        assert cache.generate(str(fake_video)) is None


class TestGetBytes:
    def test_returns_none_when_never_generated(self, tmp_path):
        cache = _make_cache(tmp_path)
        photo = _make_photo(tmp_path)
        assert cache.get_bytes(photo) is None

    def test_returns_bytes_after_generate(self, tmp_path):
        cache = _make_cache(tmp_path)
        photo = _make_photo(tmp_path)
        data = cache.generate(photo)

        assert cache.get_bytes(photo) == data

    def test_returns_none_when_file_mtime_changed_since_generation(self, tmp_path):
        cache = _make_cache(tmp_path)
        photo = _make_photo(tmp_path)
        cache.generate(photo)

        future = time.time() + 120
        os.utime(photo, (future, future))

        assert cache.get_bytes(photo) is None

    def test_returns_none_for_unknown_path(self, tmp_path):
        cache = _make_cache(tmp_path)
        assert cache.get_bytes(str(tmp_path / "never_generated.jpg")) is None


class TestMovePhoto:
    def test_move_transfers_db_row_to_new_path(self, tmp_path):
        cache = _make_cache(tmp_path)
        old_photo = _make_photo(tmp_path, name="old.jpg")
        data = cache.generate(old_photo)

        new_photo = tmp_path / "new.jpg"
        os.rename(old_photo, new_photo)
        cache.move_photo(old_photo, str(new_photo))

        assert cache.get_bytes(str(new_photo)) == data
        assert cache.get_bytes(old_photo) is None


class TestInvalidate:
    def test_invalidate_removes_db_row(self, tmp_path):
        cache = _make_cache(tmp_path)
        photo = _make_photo(tmp_path)
        cache.generate(photo)

        cache.invalidate(photo)

        assert cache.get_bytes(photo) is None

    def test_invalidate_unknown_path_is_noop(self, tmp_path):
        cache = _make_cache(tmp_path)
        cache.invalidate(str(tmp_path / "never.jpg"))  # ne doit pas lever


class TestInvalidateMany:
    def test_invalidate_many_removes_all_listed_paths(self, tmp_path):
        cache = _make_cache(tmp_path)
        photo_a = _make_photo(tmp_path, name="a.jpg")
        photo_b = _make_photo(tmp_path, name="b.jpg")
        cache.generate(photo_a)
        cache.generate(photo_b)

        cache.invalidate_many([photo_a, photo_b])

        assert cache.get_bytes(photo_a) is None
        assert cache.get_bytes(photo_b) is None

    def test_invalidate_many_with_empty_list_is_noop(self, tmp_path):
        cache = _make_cache(tmp_path)
        cache.invalidate_many([])  # ne doit pas lever
