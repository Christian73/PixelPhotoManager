# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Building of the viewer pixmaps (viewer_pixmaps.py): the 1024 px base image,
applying the edits on top of it, and the video frames.

Two invariants dominate this module and are checked here again and again:
- the returned dimensions are those of the ORIGINAL image after EXIF correction,
  never those of the reduced copy -- they are the reference frame of the face
  bboxes;
- every function returns None on failure and never raises, since it is called
  from a thread whose only contract is "a pixmap or nothing".
"""
import io

import pytest
from PIL import Image
from PySide6.QtGui import QPixmap

from src.core.models import EditInfo, PhotoInfo
from src.ui import viewer_pixmaps as vp


def _jpeg(path, w, h, color=(200, 60, 30), exif=None):
    img = Image.new("RGB", (w, h), color)
    if exif is not None:
        img.save(str(path), exif=exif)
    else:
        img.save(str(path))
    return str(path)


def _photo(path) -> PhotoInfo:
    return PhotoInfo(path=str(path))


@pytest.fixture
def clip(tmp_path):
    """A real 20-frame video, encoded by cv2 -- the only way to exercise the
    VideoCapture path without shipping a binary fixture."""
    import cv2
    import numpy as np

    path = str(tmp_path / "clip.avi")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (64, 48))
    assert writer.isOpened(), "encodeur MJPG indisponible"
    for i in range(20):
        frame = np.zeros((48, 64, 3), np.uint8)
        frame[:, :] = (i * 10, 40, 200)
        writer.write(frame)
    writer.release()
    return path


class TestToRgb:
    def test_an_rgb_image_is_returned_untouched(self):
        img = Image.new("RGB", (4, 4), (1, 2, 3))
        assert vp._to_rgb(img) is img

    def test_rgba_is_flattened_on_white(self):
        img = Image.new("RGBA", (4, 4), (255, 0, 0, 0))     # fully transparent
        out = vp._to_rgb(img)
        assert out.mode == "RGB"
        assert out.getpixel((0, 0)) == (255, 255, 255)

    def test_rgba_keeps_its_opaque_pixels(self):
        img = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
        assert vp._to_rgb(img).getpixel((0, 0)) == (255, 0, 0)

    @pytest.mark.parametrize("mode", ["L", "P", "CMYK"])
    def test_the_other_modes_are_converted(self, mode):
        out = vp._to_rgb(Image.new(mode, (4, 4)))
        assert out.mode == "RGB"


class TestBuildBaseImage:
    def test_returns_decodable_jpeg_bytes(self, tmp_path):
        data, w, h = vp._build_base_image(_photo(_jpeg(tmp_path / "p.jpg", 200, 120)))
        assert (w, h) == (200, 120)
        assert Image.open(io.BytesIO(data)).size == (200, 120)

    def test_a_large_image_is_reduced_but_reports_its_original_size(self, tmp_path):
        """The reduction is a display detail; the announced dimensions stay those
        of the original, since the face bboxes are expressed in that frame."""
        data, w, h = vp._build_base_image(_photo(_jpeg(tmp_path / "big.jpg", 3000, 1500)))
        assert (w, h) == (3000, 1500)
        assert Image.open(io.BytesIO(data)).size == (vp._PREVIEW_MAX_PX, 512)

    def test_a_small_image_is_never_enlarged(self, tmp_path):
        data, _, _ = vp._build_base_image(_photo(_jpeg(tmp_path / "s.jpg", 80, 40)))
        assert Image.open(io.BytesIO(data)).size == (80, 40)

    def test_the_exif_orientation_is_applied(self, tmp_path):
        exif = Image.Exif()
        exif[274] = 6                       # 90 deg: the dimensions swap
        path = _jpeg(tmp_path / "rot.jpg", 200, 120, exif=exif)
        _, w, h = vp._build_base_image(_photo(path))
        assert (w, h) == (120, 200)

    def test_a_transparent_png_is_flattened(self, tmp_path):
        path = str(tmp_path / "a.png")
        Image.new("RGBA", (30, 30), (0, 0, 255, 0)).save(path)
        data, _, _ = vp._build_base_image(_photo(path))
        assert Image.open(io.BytesIO(data)).mode == "RGB"

    def test_a_missing_file_returns_none(self, tmp_path):
        assert vp._build_base_image(_photo(tmp_path / "ghost.jpg")) is None

    def test_a_video_goes_through_the_video_path(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(vp, "_build_video_base_image",
                            lambda p: seen.append(p) or (b"x", 1, 2))
        assert vp._build_base_image(_photo(tmp_path / "movie.MP4")) == (b"x", 1, 2)
        assert len(seen) == 1


class TestBuildPixmap:
    def test_returns_the_pixmap_and_the_original_size(self, qapp, tmp_path):
        pixmap, w, h = vp._build_pixmap(_photo(_jpeg(tmp_path / "p.jpg", 200, 120)), None)
        assert (w, h) == (200, 120)
        assert (pixmap.width(), pixmap.height()) == (200, 120)

    def test_a_large_image_is_reduced_to_the_preview_size(self, qapp, tmp_path):
        pixmap, w, h = vp._build_pixmap(_photo(_jpeg(tmp_path / "big.jpg", 2048, 1024)), None)
        assert (w, h) == (2048, 1024)
        assert pixmap.width() == vp._PREVIEW_MAX_PX

    def test_an_edit_is_applied_to_the_pixmap(self, qapp, tmp_path):
        photo = _photo(_jpeg(tmp_path / "p.jpg", 200, 120))
        pixmap, w, h = vp._build_pixmap(photo, EditInfo(rotation=90))
        assert (pixmap.width(), pixmap.height()) == (120, 200)
        assert (w, h) == (200, 120)      # the reference frame does not rotate

    def test_an_unmodified_edit_changes_nothing(self, qapp, tmp_path):
        photo = _photo(_jpeg(tmp_path / "p.jpg", 200, 120))
        pixmap, _, _ = vp._build_pixmap(photo, EditInfo())
        assert (pixmap.width(), pixmap.height()) == (200, 120)

    def test_a_corrupted_file_returns_none(self, qapp, tmp_path):
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not a jpeg at all")
        assert vp._build_pixmap(_photo(broken), None) is None

    def test_a_video_goes_through_the_video_path(self, qapp, tmp_path, monkeypatch):
        sentinel = (QPixmap(2, 2), 640, 480)
        monkeypatch.setattr(vp, "_build_video_pixmap", lambda p: sentinel)
        assert vp._build_pixmap(_photo(tmp_path / "movie.mkv"), None) is sentinel


class TestApplyEditToBase:
    @pytest.fixture
    def base(self, tmp_path):
        data, _, _ = vp._build_base_image(_photo(_jpeg(tmp_path / "p.jpg", 200, 120)))
        return data

    def test_without_edit_the_bytes_are_decoded_as_they_are(self, qapp, base):
        pixmap = vp._apply_edit_to_base(base, None)
        assert (pixmap.width(), pixmap.height()) == (200, 120)

    def test_without_edit_pil_is_never_involved(self, qapp, base, monkeypatch):
        """The navigation hot path: a direct JPEG decoding by Qt, without the
        decode + re-encode PIL round trip."""
        monkeypatch.setattr(Image, "open", lambda *a, **k: pytest.fail("PIL utilise"))
        assert vp._apply_edit_to_base(base, EditInfo()) is not None

    def test_a_modified_edit_is_applied(self, qapp, base):
        pixmap = vp._apply_edit_to_base(base, EditInfo(rotation=90))
        assert (pixmap.width(), pixmap.height()) == (120, 200)

    def test_a_black_and_white_edit_really_desaturates(self, qapp, base):
        pixmap = vp._apply_edit_to_base(base, EditInfo(bw=True))
        color = pixmap.toImage().pixelColor(100, 60)
        assert color.red() == color.green() == color.blue()

    def test_unusable_bytes_return_none(self, qapp):
        assert vp._apply_edit_to_base(b"garbage", None) is None

    def test_a_failure_of_the_adjustments_returns_none(self, qapp, base, monkeypatch):
        def _boom(*a, **k):
            raise ValueError("boom")

        monkeypatch.setattr(vp.ImageAdjuster, "apply_all", _boom)
        assert vp._apply_edit_to_base(base, EditInfo(rotation=90)) is None


@pytest.fixture
def hd_clip(tmp_path):
    """A video wider than _PREVIEW_MAX_PX, to exercise the reduction."""
    import cv2
    import numpy as np

    path = str(tmp_path / "hd.avi")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (2048, 512))
    assert writer.isOpened()
    for _ in range(4):
        writer.write(np.full((512, 2048, 3), 90, np.uint8))
    writer.release()
    return path


class TestVideoFrames:
    def test_the_base_image_of_a_video(self, clip):
        data, w, h = vp._build_video_base_image(clip)
        assert (w, h) == (64, 48)
        assert Image.open(io.BytesIO(data)).size == (64, 48)

    def test_the_pixmap_of_a_video(self, qapp, clip):
        pixmap, w, h = vp._build_video_pixmap(clip)
        assert (w, h) == (64, 48)
        assert (pixmap.width(), pixmap.height()) == (64, 48)

    def test_a_large_video_frame_is_reduced(self, qapp, hd_clip):
        data, w, h = vp._build_video_base_image(hd_clip)
        assert (w, h) == (2048, 512)     # announced: the real size of the video
        assert Image.open(io.BytesIO(data)).size == (vp._PREVIEW_MAX_PX, 256)

    def test_a_large_video_pixmap_is_reduced_too(self, qapp, hd_clip):
        pixmap, w, h = vp._build_video_pixmap(hd_clip)
        assert (w, h) == (2048, 512)
        assert (pixmap.width(), pixmap.height()) == (vp._PREVIEW_MAX_PX, 256)

    @pytest.mark.parametrize("build", ["_build_video_base_image", "_build_video_pixmap"])
    def test_an_opencv_failure_returns_none(self, qapp, clip, monkeypatch, build):
        """Decoding runs in a thread whose only contract is "a frame or nothing":
        an exception coming out of cv2 must never climb up to it."""
        import cv2

        def _boom(*a, **k):
            raise RuntimeError("cv2 en vrac")

        monkeypatch.setattr(cv2, "VideoCapture", _boom)
        assert getattr(vp, build)(clip) is None

    @pytest.mark.parametrize("build", ["_build_video_base_image", "_build_video_pixmap"])
    def test_a_file_that_is_not_a_video_returns_none(self, qapp, tmp_path, build):
        fake = tmp_path / "fake.avi"
        fake.write_bytes(b"this is not a video")
        assert getattr(vp, build)(str(fake)) is None

    @pytest.mark.parametrize("build", ["_build_video_base_image", "_build_video_pixmap"])
    def test_a_missing_video_returns_none(self, qapp, tmp_path, build):
        assert getattr(vp, build)(str(tmp_path / "ghost.avi")) is None

    def test_the_whole_chain_from_a_video_photo(self, qapp, clip):
        """_build_pixmap dispatches on the extension: a PhotoInfo pointing at a
        video comes back as a real decoded frame, not as None."""
        pixmap, w, h = vp._build_pixmap(_photo(clip), None)
        assert (w, h) == (64, 48)
        assert not pixmap.isNull()
