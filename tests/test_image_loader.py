# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/library/image_loader.py — point de décodage image unique
(RAW + HEIC + formats standards). rawpy est monkeypatché pour les scénarios
RAW (dispatch JPEG embarqué / bitmap embarqué / repli postprocess) plutôt que
de dépendre d'un vrai fichier .cr2 ; HEIC est testé avec un vrai fichier
généré via pillow_heif (aller-retour réel), gated par importorskip."""
import io
import sys

import numpy as np
import pytest
from PIL import Image

from src.library.image_loader import (
    RAW_EXT, is_raw_available, open_image, safe_temp_suffix,
)


def _jpeg_bytes(size=(8, 6), color=(120, 80, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class TestSafeTempSuffix:
    def test_raw_extension_forced_to_jpg(self):
        for ext in RAW_EXT:
            assert safe_temp_suffix(f"C:/photos/a{ext}") == ".jpg"

    def test_heic_forced_to_jpg(self):
        assert safe_temp_suffix("C:/photos/a.heic") == ".jpg"
        assert safe_temp_suffix("C:/photos/a.HEIF") == ".jpg"

    def test_normal_extension_preserved(self):
        assert safe_temp_suffix("C:/photos/a.png") == ".png"
        assert safe_temp_suffix("C:/photos/a.JPG") == ".jpg"

    def test_missing_extension_falls_back_to_jpg(self):
        assert safe_temp_suffix("C:/photos/noext") == ".jpg"


class TestOpenImageStandardFormats:
    def test_opens_regular_jpeg(self, tmp_path):
        path = tmp_path / "a.jpg"
        Image.new("RGB", (10, 5), (10, 20, 30)).save(path)

        with open_image(str(path)) as img:
            assert img.size == (10, 5)

    def test_raw_extension_without_rawpy_falls_back_to_pil(self, tmp_path, monkeypatch):
        """is_raw_available() False (rawpy absent) : open_image doit se rabattre
        sur Image.open standard plutôt que de lever — même si le contenu réel
        n'est pas un vrai CR2 (fichier JPEG renommé ici pour l'isoler du
        décodage RAW proprement dit, testé séparément ci-dessous)."""
        monkeypatch.setitem(sys.modules, "rawpy", None)
        assert is_raw_available() is False

        path = tmp_path / "a.cr2"
        Image.new("RGB", (10, 5), (10, 20, 30)).save(path, format="JPEG")

        with open_image(str(path)) as img:
            assert img.size == (10, 5)


class _FakeThumb:
    def __init__(self, fmt, data):
        self.format = fmt
        self.data = data


class _FakeRaw:
    def __init__(self, thumb=None, thumb_exc=None, postprocess_rgb=None):
        self._thumb = thumb
        self._thumb_exc = thumb_exc
        self._postprocess_rgb = postprocess_rgb

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_thumb(self):
        if self._thumb_exc is not None:
            raise self._thumb_exc
        return self._thumb

    def postprocess(self, half_size=False):
        return self._postprocess_rgb


class TestOpenImageRaw:
    """rawpy monkeypatché : dispatch selon le format d'aperçu embarqué."""

    def test_jpeg_thumbnail_is_used_when_available(self, tmp_path, monkeypatch):
        import rawpy

        jpeg_data = _jpeg_bytes(size=(12, 9))
        fake_raw = _FakeRaw(thumb=_FakeThumb(rawpy.ThumbFormat.JPEG, jpeg_data))
        monkeypatch.setattr(rawpy, "imread", lambda path: fake_raw)

        path = tmp_path / "a.cr2"
        path.write_bytes(b"not a real raw file")

        with open_image(str(path)) as img:
            assert img.size == (12, 9)

    def test_bitmap_thumbnail_is_used_when_available(self, tmp_path, monkeypatch):
        import rawpy

        arr = np.zeros((9, 12, 3), dtype=np.uint8)
        fake_raw = _FakeRaw(thumb=_FakeThumb(rawpy.ThumbFormat.BITMAP, arr))
        monkeypatch.setattr(rawpy, "imread", lambda path: fake_raw)

        path = tmp_path / "a.nef"
        path.write_bytes(b"not a real raw file")

        with open_image(str(path)) as img:
            assert img.size == (12, 9)

    def test_falls_back_to_postprocess_when_no_thumbnail(self, tmp_path, monkeypatch):
        import rawpy

        arr = np.zeros((6, 8, 3), dtype=np.uint8)
        fake_raw = _FakeRaw(
            thumb_exc=rawpy.LibRawNoThumbnailError("no thumb"),
            postprocess_rgb=arr,
        )
        monkeypatch.setattr(rawpy, "imread", lambda path: fake_raw)

        path = tmp_path / "a.arw"
        path.write_bytes(b"not a real raw file")

        with open_image(str(path)) as img:
            assert img.size == (8, 6)

    def test_falls_back_to_postprocess_on_unsupported_thumbnail(self, tmp_path, monkeypatch):
        import rawpy

        arr = np.zeros((4, 5, 3), dtype=np.uint8)
        fake_raw = _FakeRaw(
            thumb_exc=rawpy.LibRawUnsupportedThumbnailError("unsupported"),
            postprocess_rgb=arr,
        )
        monkeypatch.setattr(rawpy, "imread", lambda path: fake_raw)

        path = tmp_path / "a.dng"
        path.write_bytes(b"not a real raw file")

        with open_image(str(path)) as img:
            assert img.size == (5, 4)


class TestOpenImageHeic:
    def test_real_heic_round_trip(self, tmp_path):
        pillow_heif = pytest.importorskip("pillow_heif")

        path = tmp_path / "a.heic"
        heif_file = pillow_heif.from_pillow(Image.new("RGB", (16, 12), (200, 100, 50)))
        heif_file.save(str(path), quality=80)

        with open_image(str(path)) as img:
            assert img.size == (16, 12)


class TestIsRawAvailable:
    def test_true_when_rawpy_importable(self):
        assert is_raw_available() is True

    def test_false_when_rawpy_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rawpy", None)
        assert is_raw_available() is False
