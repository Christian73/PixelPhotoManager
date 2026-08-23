# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/faces/detector.py` without running InsightFace: the
FaceAnalysis singleton is replaced by a fake whose `.get()` returns fabricated
faces. Covers the filtering rules documented in CLAUDE.md (det_score < 0.5,
embedding None, w/h < 20 px -- never to be changed), the rescaling of the
bboxes for large images, the _exif_corrected / _resized_for_detection context
managers, the resolution of the insightface root (dev vs frozen) and the
rotation strategy of detect_and_embed_auto."""
import os
import sys

import numpy as np
import pytest
from PIL import Image

from src.faces import detector


class _FakeFace:
    def __init__(self, bbox_xyxy, score=0.9, embedding=(0.5,) * 8):
        self.bbox = np.array(bbox_xyxy, dtype=np.float32)
        self.det_score = score
        self.embedding = (
            None if embedding is None else np.array(embedding, dtype=np.float32)
        )


class _FakeApp:
    def __init__(self, faces):
        self._faces = faces
        self.models = {}

    def get(self, img):
        return self._faces


def _make_jpg(path, size=(200, 150), orientation=None):
    img = Image.new("RGB", size, (80, 80, 80))
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        img.save(str(path), exif=exif)
    else:
        img.save(str(path))
    return str(path)


@pytest.fixture
def fake_app(monkeypatch):
    """Installs a fake FaceAnalysis; returns a setter for the faces."""
    holder = _FakeApp([])
    monkeypatch.setattr(detector, "_get_insight_app", lambda: holder)
    return holder


# ------------------------------------------------------------------ root & availability


class TestInsightfaceRoot:
    def test_dev_mode_uses_user_cache(self):
        assert detector._insightface_root() == os.path.expanduser("~/.insightface")

    def test_frozen_with_bundled_pack(self, tmp_path, monkeypatch):
        bundled = tmp_path / "insightface_root" / "models" / "buffalo_l"
        bundled.mkdir(parents=True)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert detector._insightface_root() == str(tmp_path / "insightface_root")

    def test_frozen_without_pack_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert detector._insightface_root() == os.path.expanduser("~/.insightface")


class TestIsAvailable:
    def test_available_in_venv(self):
        assert detector.is_available() is True


class TestRegisterNvidiaDllDirs:
    def test_does_not_raise(self):
        detector._register_nvidia_dll_dirs()


# ------------------------------------------------------------------ context managers


class TestExifCorrected:
    def test_plain_ascii_no_rotation_passthrough(self, tmp_path):
        p = _make_jpg(tmp_path / "plain.jpg")
        with detector._exif_corrected(p) as out:
            assert out == p

    def test_extra_rotation_creates_temp(self, tmp_path):
        p = _make_jpg(tmp_path / "rot.jpg", size=(200, 150))
        with detector._exif_corrected(p, extra_rotation=90) as out:
            assert out != p
            with Image.open(out) as img:
                assert img.size == (150, 200)   # dimensions swapped
            temp_used = out
        assert not os.path.exists(temp_used)   # temp cleaned up

    def test_exif_orientation_corrected(self, tmp_path):
        p = _make_jpg(tmp_path / "ori6.jpg", size=(200, 150), orientation=6)
        with detector._exif_corrected(p) as out:
            assert out != p
            with Image.open(out) as img:
                assert img.size == (150, 200)

    def test_non_ascii_path_gets_ascii_temp(self, tmp_path):
        p = _make_jpg(tmp_path / "été.jpg")
        with detector._exif_corrected(p) as out:
            assert out != p
            out.encode("ascii")   # must not raise

    def test_non_ascii_corrupt_file_raw_copy(self, tmp_path):
        p = tmp_path / "cassé.jpg"
        p.write_bytes(b"pas un jpeg")
        with detector._exif_corrected(str(p)) as out:
            assert out != str(p)
            out.encode("ascii")
            assert open(out, "rb").read() == b"pas un jpeg"

    def test_video_extracts_frame(self, tmp_path):
        import cv2
        vid = tmp_path / "clip.mp4"
        writer = cv2.VideoWriter(
            str(vid), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48)
        )
        for _ in range(10):
            writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
        writer.release()
        with detector._exif_corrected(str(vid)) as out:
            assert out != str(vid)
            assert out.endswith(".jpg")
            with Image.open(out) as img:
                assert img.size == (64, 48)


class TestResizedForDetection:
    def test_small_image_passthrough(self, tmp_path):
        p = _make_jpg(tmp_path / "small.jpg", size=(640, 480))
        with detector._resized_for_detection(p) as (out, scale):
            assert out == p
            assert scale == 1.0

    def test_large_image_resized(self, tmp_path):
        p = _make_jpg(tmp_path / "large.jpg", size=(3840, 1920))
        with detector._resized_for_detection(p) as (out, scale):
            assert out != p
            assert scale == pytest.approx(0.5)
            with Image.open(out) as img:
                assert img.size == (1920, 960)
            temp_used = out
        assert not os.path.exists(temp_used)

    def test_missing_file_passthrough(self, tmp_path):
        p = str(tmp_path / "absent.jpg")
        with detector._resized_for_detection(p) as (out, scale):
            assert out == p
            assert scale == 1.0


# ------------------------------------------------------------------ detect_and_embed


class TestDetectAndEmbed:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            detector.detect_and_embed(str(tmp_path / "absent.jpg"))

    def test_valid_face_returned(self, tmp_path, fake_app):
        p = _make_jpg(tmp_path / "a.jpg")
        fake_app._faces = [_FakeFace((10, 20, 60, 90))]
        result = detector.detect_and_embed(p)
        assert len(result) == 1
        assert result[0]["bbox"] == (10, 20, 50, 70)
        assert result[0]["det_score"] == pytest.approx(0.9)
        assert len(result[0]["embedding"]) == 8

    def test_low_score_filtered(self, tmp_path, fake_app):
        p = _make_jpg(tmp_path / "a.jpg")
        fake_app._faces = [_FakeFace((10, 20, 60, 90), score=0.4)]
        assert detector.detect_and_embed(p) == []

    def test_none_embedding_filtered(self, tmp_path, fake_app):
        p = _make_jpg(tmp_path / "a.jpg")
        fake_app._faces = [_FakeFace((10, 20, 60, 90), embedding=None)]
        assert detector.detect_and_embed(p) == []

    def test_tiny_face_filtered(self, tmp_path, fake_app):
        p = _make_jpg(tmp_path / "a.jpg")
        # 19x50 px: w < 20 -> excluded; 50x19: h < 20 -> excluded
        fake_app._faces = [
            _FakeFace((0, 0, 19, 50)),
            _FakeFace((0, 0, 50, 19)),
        ]
        assert detector.detect_and_embed(p) == []

    def test_exactly_20px_kept(self, tmp_path, fake_app):
        p = _make_jpg(tmp_path / "a.jpg")
        fake_app._faces = [_FakeFace((0, 0, 20, 20))]
        assert len(detector.detect_and_embed(p)) == 1

    def test_no_faces_returns_empty(self, tmp_path, fake_app):
        p = _make_jpg(tmp_path / "a.jpg")
        assert detector.detect_and_embed(p) == []

    def test_bbox_rescaled_for_large_image(self, tmp_path, fake_app):
        # 3840x1920 -> detection on 1920x960 (scale 0.5) -> bbox x2 on return
        p = _make_jpg(tmp_path / "large.jpg", size=(3840, 1920))
        fake_app._faces = [_FakeFace((100, 50, 200, 150))]
        result = detector.detect_and_embed(p)
        assert result[0]["bbox"] == (200, 100, 200, 200)

    def test_corrupt_image_returns_empty(self, tmp_path, fake_app):
        p = tmp_path / "corrompu.jpg"
        p.write_bytes(b"pas un jpeg")
        assert detector.detect_and_embed(str(p)) == []

    def test_app_exception_returns_empty(self, tmp_path, monkeypatch):
        p = _make_jpg(tmp_path / "a.jpg")

        def _boom():
            raise RuntimeError("CUDA out of memory")

        monkeypatch.setattr(detector, "_get_insight_app", _boom)
        assert detector.detect_and_embed(p) == []


# ------------------------------------------------------------------ detect_and_embed_auto


class TestDetectAndEmbedAuto:
    def test_stops_at_zero_when_found(self, tmp_path, monkeypatch):
        calls: list[int] = []

        def _detect(path, rotation=0):
            calls.append(rotation)
            return [{"bbox": (0, 0, 30, 30), "embedding": [0.1], "det_score": 0.9}]

        monkeypatch.setattr(detector, "detect_and_embed", _detect)
        result, rotation = detector.detect_and_embed_auto("photo.jpg")
        assert rotation == 0
        assert len(result) == 1
        assert calls == [0]   # never 90/180/270 if 0 degrees finds something

    def test_tries_rotations_when_zero_fails(self, tmp_path, monkeypatch):
        def _detect(path, rotation=0):
            if rotation == 180:
                return [{"bbox": (0, 0, 30, 30), "embedding": [0.1], "det_score": 0.9}]
            return []

        monkeypatch.setattr(detector, "detect_and_embed", _detect)
        result, rotation = detector.detect_and_embed_auto("photo.jpg")
        assert rotation == 180
        assert len(result) == 1

    def test_nothing_found_anywhere(self, monkeypatch):
        monkeypatch.setattr(
            detector, "detect_and_embed", lambda path, rotation=0: []
        )
        result, rotation = detector.detect_and_embed_auto("photo.jpg")
        assert result == []
        assert rotation == 0


# ------------------------------------------------------------------ warmup


class TestWarmup:
    def test_warmup_worker_with_fake_app(self, fake_app):
        detector.warmup_worker()   # empty models -> CPU backend, no exception

    def test_warmup_worker_raises_on_failure(self, monkeypatch):
        def _boom():
            raise OSError("modèle manquant")

        monkeypatch.setattr(detector, "_get_insight_app", _boom)
        with pytest.raises(OSError):
            detector.warmup_worker()
