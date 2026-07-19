# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/library/exif_reader.py` sur de vraies fixtures : images JPEG avec
EXIF complet écrit via piexif (dates + sous-secondes, appareil, objectif, ISO,
exposition, ouverture, focale, GPS N/S/E/W), correction d'orientation,
`ascii_safe_path` (passthrough, hardlink, repli copie), `preserve_file_dates`,
`_parse_subsec`, et `VideoMetadataReader` sur une vraie vidéo cv2."""
import os
from datetime import datetime

import piexif
import pytest
from PIL import Image

from src.library.exif_reader import (
    VIDEO_EXT,
    ExifReader,
    VideoMetadataReader,
    _parse_subsec,
    ascii_safe_path,
    preserve_file_dates,
)


# ------------------------------------------------------------------ helpers


def _save_jpg_with_exif(path, exif_dict, size=(64, 48)) -> None:
    img = Image.new("RGB", size, color=(90, 90, 90))
    img.save(str(path), exif=piexif.dump(exif_dict))


_FULL_EXIF = {
    "0th": {
        piexif.ImageIFD.Make: b"Canon ",
        piexif.ImageIFD.Model: b"EOS R5",
        piexif.ImageIFD.DateTime: b"2020:05:06 07:08:09",
    },
    "Exif": {
        piexif.ExifIFD.DateTimeOriginal: b"2021:02:03 04:05:06",
        piexif.ExifIFD.SubSecTimeOriginal: b"56",
        piexif.ExifIFD.ISOSpeedRatings: 400,
        piexif.ExifIFD.ExposureTime: (1, 250),
        piexif.ExifIFD.FNumber: (28, 10),
        piexif.ExifIFD.FocalLength: (50, 1),
        piexif.ExifIFD.LensModel: b"RF 50mm F1.8",
    },
    "GPS": {
        piexif.GPSIFD.GPSLatitudeRef: b"N",
        piexif.GPSIFD.GPSLatitude: ((48, 1), (51, 1), (2952, 100)),
        piexif.GPSIFD.GPSLongitudeRef: b"E",
        piexif.GPSIFD.GPSLongitude: ((2, 1), (21, 1), (300, 100)),
    },
}


# ------------------------------------------------------------------ _parse_subsec


class TestParseSubsec:
    def test_two_digits_are_hundredths(self):
        assert _parse_subsec("05") == 50000   # 0.05 s

    def test_three_digits_are_thousandths(self):
        assert _parse_subsec("563") == 563000

    def test_empty(self):
        assert _parse_subsec("") == 0

    def test_non_digit(self):
        assert _parse_subsec("abc") == 0

    def test_whitespace_stripped(self):
        assert _parse_subsec(" 5 ") == 500000

    def test_more_than_six_digits_truncated(self):
        assert _parse_subsec("1234567") == 123456


# ------------------------------------------------------------------ ascii_safe_path


class TestAsciiSafePath:
    def test_ascii_passthrough(self, tmp_path):
        p = tmp_path / "plain.jpg"
        p.write_bytes(b"data")
        with ascii_safe_path(str(p)) as safe:
            assert safe == str(p)

    def test_non_ascii_hardlink_and_cleanup(self, tmp_path):
        p = tmp_path / "vidéo_été.jpg"
        p.write_bytes(b"contenu")
        with ascii_safe_path(str(p)) as safe:
            assert safe != str(p)
            safe.encode("ascii")  # ne doit pas lever
            assert open(safe, "rb").read() == b"contenu"
            temp_used = safe
        assert not os.path.exists(temp_used)

    def test_copy_fallback_when_link_fails(self, tmp_path, monkeypatch):
        p = tmp_path / "élan.jpg"
        p.write_bytes(b"contenu2")

        def _fail_link(src, dst):
            raise OSError("volumes différents")

        monkeypatch.setattr(os, "link", _fail_link)
        with ascii_safe_path(str(p)) as safe:
            assert open(safe, "rb").read() == b"contenu2"
        assert not os.path.exists(safe)


# ------------------------------------------------------------------ preserve_file_dates


class TestPreserveFileDates:
    def test_mtime_copied(self, tmp_path):
        src = tmp_path / "src.jpg"
        dst = tmp_path / "dst.jpg"
        src.write_bytes(b"a")
        dst.write_bytes(b"b")
        os.utime(src, (1_600_000_000, 1_600_000_000))
        preserve_file_dates(os.stat(src), str(dst))
        assert os.stat(dst).st_mtime == pytest.approx(1_600_000_000, abs=2)


# ------------------------------------------------------------------ ExifReader


class TestExifReaderRead:
    def test_image_without_exif(self, tmp_path):
        p = tmp_path / "plain.jpg"
        Image.new("RGB", (64, 48)).save(str(p))
        r = ExifReader.read(str(p))
        assert (r["width"], r["height"]) == (64, 48)
        assert r["date_taken"] is None
        assert r["camera_make"] == ""
        assert r["has_gps"] is False

    def test_full_exif(self, tmp_path):
        p = tmp_path / "full.jpg"
        _save_jpg_with_exif(p, _FULL_EXIF)
        r = ExifReader.read(str(p))

        # DateTimeOriginal prioritaire sur DateTime, avec sous-secondes
        assert r["date_taken"] == datetime(2021, 2, 3, 4, 5, 6, 560000)
        assert r["camera_make"] == "Canon"       # strip()
        assert r["camera_model"] == "EOS R5"
        assert r["lens_model"] == "RF 50mm F1.8"
        assert r["iso"] == 400
        assert r["exposure_time"] == "1/250s"
        assert r["aperture"] == pytest.approx(2.8)
        assert r["focal_length"] == pytest.approx(50.0)
        assert r["has_gps"] is True
        # 48°51'29.52" N = 48.85820, 2°21'3.0" E = 2.35083
        assert r["gps_lat"] == pytest.approx(48.8582, abs=1e-4)
        assert r["gps_lon"] == pytest.approx(2.350833, abs=1e-4)

    def test_datetime_fallback_to_0th_ifd(self, tmp_path):
        p = tmp_path / "dt.jpg"
        _save_jpg_with_exif(
            p, {"0th": {piexif.ImageIFD.DateTime: b"2020:05:06 07:08:09"}}
        )
        r = ExifReader.read(str(p))
        assert r["date_taken"] == datetime(2020, 5, 6, 7, 8, 9)

    def test_invalid_date_string_ignored(self, tmp_path):
        p = tmp_path / "bad.jpg"
        _save_jpg_with_exif(
            p, {"0th": {piexif.ImageIFD.DateTime: b"pas une date"}}
        )
        r = ExifReader.read(str(p))
        assert r["date_taken"] is None

    def test_orientation_transposed_dimensions(self, tmp_path):
        p = tmp_path / "ori6.jpg"
        _save_jpg_with_exif(
            p, {"0th": {piexif.ImageIFD.Orientation: 6}}, size=(100, 50)
        )
        r = ExifReader.read(str(p))
        # orientation 6 (90° CW) : dimensions permutées
        assert (r["width"], r["height"]) == (50, 100)

    def test_gps_south_west_negative(self, tmp_path):
        p = tmp_path / "sw.jpg"
        _save_jpg_with_exif(p, {
            "GPS": {
                piexif.GPSIFD.GPSLatitudeRef: b"S",
                piexif.GPSIFD.GPSLatitude: ((33, 1), (52, 1), (0, 1)),
                piexif.GPSIFD.GPSLongitudeRef: b"W",
                piexif.GPSIFD.GPSLongitude: ((70, 1), (39, 1), (0, 1)),
            },
        })
        r = ExifReader.read(str(p))
        assert r["gps_lat"] == pytest.approx(-33.8667, abs=1e-3)
        assert r["gps_lon"] == pytest.approx(-70.65, abs=1e-3)

    def test_nonexistent_file_returns_defaults(self, tmp_path):
        r = ExifReader.read(str(tmp_path / "absent.jpg"))
        assert r["width"] == 0
        assert r["date_taken"] is None

    def test_corrupt_file_returns_defaults(self, tmp_path):
        p = tmp_path / "corrompu.jpg"
        p.write_bytes(b"pas un jpeg")
        r = ExifReader.read(str(p))
        assert r["width"] == 0

    def test_supported_extensions(self):
        assert ".jpg" in ExifReader.SUPPORTED
        assert ".png" in ExifReader.SUPPORTED
        assert ".mp4" not in ExifReader.SUPPORTED


class TestParseGpsDirect:
    def test_raw_tuples(self):
        # (num, denom) bruts : couvre la branche _to_float sans IFDRational
        gps_info = {
            1: "N",
            2: ((48, 1), (30, 1), (0, 1)),
            3: "E",
            4: ((2, 1), (0, 1), (0, 1)),
        }
        lat, lon = ExifReader._parse_gps(gps_info)
        assert lat == pytest.approx(48.5)
        assert lon == pytest.approx(2.0)

    def test_missing_coords_returns_none(self):
        assert ExifReader._parse_gps({1: "N"}) is None

    def test_malformed_returns_none(self):
        assert ExifReader._parse_gps({2: "garbage", 4: None}) is None


# ------------------------------------------------------------------ VideoMetadataReader


@pytest.fixture(scope="module")
def real_video(tmp_path_factory):
    """Vraie vidéo mp4v : 10 frames à 5 fps en 64×48 → durée 2 s."""
    import cv2
    import numpy as np

    path = tmp_path_factory.mktemp("videos") / "clip.mp4"
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48)
    )
    assert writer.isOpened()
    for _ in range(10):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    return path


class TestVideoMetadataReader:
    def test_real_video(self, real_video):
        r = VideoMetadataReader.read(str(real_video))
        assert (r["width"], r["height"]) == (64, 48)
        assert r["duration"] == pytest.approx(2.0, abs=0.1)
        assert isinstance(r["date_taken"], datetime)

    def test_non_ascii_video_path(self, real_video, tmp_path):
        import shutil
        p = tmp_path / "vidéo_été.mp4"
        shutil.copy2(real_video, p)
        r = VideoMetadataReader.read(str(p))
        assert (r["width"], r["height"]) == (64, 48)
        assert r["duration"] == pytest.approx(2.0, abs=0.1)

    def test_invalid_content(self, tmp_path):
        p = tmp_path / "fake.mp4"
        p.write_bytes(b"pas une video")
        r = VideoMetadataReader.read(str(p))
        assert r["width"] == 0
        assert r["duration"] == 0.0
        assert isinstance(r["date_taken"], datetime)  # mtime

    def test_missing_file(self, tmp_path):
        r = VideoMetadataReader.read(str(tmp_path / "absent.mp4"))
        assert r["date_taken"] is None
        assert r["width"] == 0


class TestVideoExt:
    def test_known_extensions(self):
        assert {".mp4", ".mov", ".mkv"} <= VIDEO_EXT
        assert len(VIDEO_EXT) == 13
