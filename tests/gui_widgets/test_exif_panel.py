# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour ExifPanel — fichiers
image/vidéo synthétiques créés en process (Pillow/cv2), pas de bibliothèque
réelle. Les loaders QThread sont appelés en synchrone (méthodes statiques ou
run()) pour que coverage trace le code (cf. convention CLAUDE.md)."""
import os
from datetime import datetime

import pytest
from PIL import Image
from PySide6.QtWidgets import QDialog, QLabel

from src.ui.exif_panel import (
    ExifEditDialog, ExifPanel, _ExifDataLoader,
    _decode_xp_str, _fmt_apex, _fmt_exposure, _fmt_lens_spec, _fmt_size,
    _fmt_value, _read_exif, _set_file_dates,
)


# ---------------------------------------------------------------------------
# fabriques

def _make_jpeg_with_exif(path) -> str:
    """JPEG 80×60 avec un jeu de tags EXIF représentatif (IFD principal + ExifIFD)."""
    img = Image.new("RGB", (80, 60), color=(90, 120, 150))
    exif = Image.Exif()
    exif[0x010F] = "PixelCam"                      # Make
    exif[0x0110] = "PC-1000"                       # Model
    exif[0x0132] = "2024:06:15 10:30:00"           # DateTime
    exif[0x013B] = "Jean Testeur"                  # Artist
    exif[0x010E] = "Photo de test"                 # ImageDescription
    exif[0x0112] = 1                               # Orientation
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = "2024:06:15 10:30:00"            # DateTimeOriginal
    ifd[0x829A] = 0.005                            # ExposureTime (1/200)
    ifd[0x829D] = 2.8                              # FNumber
    ifd[0x8827] = 200                              # ISOSpeedRatings
    ifd[0x920A] = 35.0                             # FocalLength
    ifd[0x9209] = 16                               # Flash (Off, non déclenché)
    img.save(path, format="JPEG", exif=exif)
    return str(path)


def _make_plain_jpeg(path) -> str:
    Image.new("RGB", (40, 30), color=(10, 20, 30)).save(path, format="JPEG")
    return str(path)


def _make_avi(path, w=64, h=48, frames=5, fps=10.0) -> str:
    import cv2
    import numpy as np
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    for _ in range(frames):
        vw.write(np.zeros((h, w, 3), dtype=np.uint8))
    vw.release()
    return str(path)


def _panel_texts(panel: ExifPanel) -> list[str]:
    """Textes de tous les QLabel du contenu du panneau (sections + lignes)."""
    return [
        lbl.text()
        for lbl in panel._content.findChildren(QLabel)
        if lbl.text()
    ]


# ---------------------------------------------------------------------------
# formateurs purs

class TestFormatters:
    @pytest.mark.parametrize("val, expected", [
        (0.005, "1/200 s"),
        (2.0, "2.0 s"),
        ("bad", "bad"),
    ])
    def test_fmt_exposure(self, val, expected):
        assert _fmt_exposure(val) == expected

    def test_fmt_apex_converts_to_fnumber(self):
        # APEX 2 → f/2.0 (sqrt(2^2))
        assert _fmt_apex(2) == "f/2.0"
        assert _fmt_apex("bad") == "bad"

    def test_decode_xp_str_utf16le(self):
        assert _decode_xp_str("Titre\u00e9".encode("utf-16-le")) == "Titreé"
        assert _decode_xp_str("déjà str") == "déjà str"

    def test_fmt_lens_spec_range_and_fixed(self):
        assert _fmt_lens_spec([18.0, 55.0, 3.5, 5.6]) == "18–55 mm  f/3.5–f/5.6"
        assert _fmt_lens_spec([50.0, 50.0, 1.8, 1.8]) == "50 mm  f/1.8"
        assert _fmt_lens_spec("garbage") == "garbage"

    @pytest.mark.parametrize("tag, val, expected", [
        ("FNumber", 2.8, "f/2.8"),
        ("FocalLength", 35.0, "35 mm"),
        ("FocalLengthIn35mmFilm", 52, "52 mm"),
        ("ISOSpeedRatings", (200, 0), "200"),
        ("ISOSpeedRatings", 400, "400"),
        ("DateTimeOriginal", "2024:06:15 10:30:00", "15/06/2024  10:30:00"),
        ("ExposureBiasValue", -0.7, "-0.7 EV"),
        ("DigitalZoomRatio", 0, "Pas de zoom"),
        ("DigitalZoomRatio", 2.5, "×2.50"),
        ("SubjectDistance", 3.2, "3.20 m"),
        ("SubjectDistance", 100000, "Infini"),
        ("ExposureProgram", 2, "Programme auto"),
        ("MeteringMode", 5, "Évaluative"),
        ("WhiteBalance", 0, "Auto"),
        ("ExposureMode", 1, "Manuel"),
        ("ColorSpace", 1, "sRGB"),
        ("Orientation", 6, "90° horaire"),
        ("Flash", 0x10, "Off, non déclenché"),
        ("Flash", 0x63, "0x63"),
        ("ResolutionUnit", 2, "dpi"),
        ("XResolution", 300.0, "300"),
        ("SceneCaptureType", 1, "Paysage"),
        ("LightSource", 1, "Lumière du jour"),
        ("GainControl", 2, "Fort gain +"),
        ("Contrast", 1, "Doux"),
        ("SubjectDistanceRange", 2, "Vue proche"),
        ("SensitivityType", 2, "REI"),
        ("CustomRendered", 0, "Processus normal"),
        ("Compression", 7, "JPEG"),
        ("XPTitle", "T".encode("utf-16-le"), "T"),
        ("Autre", None, ""),
        ("Autre", b"\x00\x01", "(données binaires)"),
        ("Autre", [1, 2], "1, 2"),
        ("Autre", "  brut  ", "brut"),
    ])
    def test_fmt_value(self, tag, val, expected):
        assert _fmt_value(tag, val) == expected

    def test_fmt_size_units(self):
        assert _fmt_size(512) == "512 o"
        assert _fmt_size(2048) == "2.0 Ko"
        assert _fmt_size(3 * 1024 ** 2) == "3.0 Mo"
        assert _fmt_size(5 * 1024 ** 3) == "5.0 Go"


class TestSetFileDates:
    def test_sets_mtime(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("data")
        dt = datetime(2020, 1, 2, 3, 4, 5)

        _set_file_dates(str(f), dt)

        # La branche Win32 (SetFileTime) interprète dt comme UTC alors
        # qu'os.utime l'interprète en heure locale : selon la branche qui a
        # gagné, le mtime vaut l'une ou l'autre lecture (écart = décalage de
        # fuseau). Comportement historique de l'application — le test accepte
        # les deux interprétations.
        from datetime import timezone
        local_ts = dt.timestamp()
        utc_ts = dt.replace(tzinfo=timezone.utc).timestamp()
        mtime = os.stat(f).st_mtime
        assert any(abs(mtime - ts) < 2 for ts in (local_ts, utc_ts))


# ---------------------------------------------------------------------------
# lecture EXIF

class TestReadExif:
    def test_reads_main_and_sub_ifd(self, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        with Image.open(path) as img:
            exif = _read_exif(img)

        assert exif["Make"] == "PixelCam"
        assert exif["Model"] == "PC-1000"
        assert exif["DateTimeOriginal"] == "2024:06:15 10:30:00"
        assert int(exif["ISOSpeedRatings"]) == 200

    def test_image_without_exif_returns_empty(self, tmp_path):
        path = _make_plain_jpeg(tmp_path / "p.jpg")
        with Image.open(path) as img:
            assert _read_exif(img) == {}


# ---------------------------------------------------------------------------
# loader (appel synchrone des méthodes statiques → tracé par coverage)

class TestExifDataLoader:
    def test_load_image_returns_metadata(self, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")

        data = _ExifDataLoader._load_image(path)

        assert data["type"] == "image"
        assert data["format"] == "JPEG"
        assert (data["width"], data["height"]) == (80, 60)
        assert data["size"] > 0
        assert data["exif"]["Make"] == "PixelCam"

    def test_load_video_reads_dimensions_fps_duration(self, tmp_path):
        path = _make_avi(tmp_path / "v.avi")

        data = _ExifDataLoader._load_video(path)

        assert data["type"] == "video"
        assert (data["width"], data["height"]) == (64, 48)
        assert data["fps"] == pytest.approx(10.0, abs=0.5)
        assert data["duration"] == pytest.approx(0.5, abs=0.2)
        assert data["codec"].upper() == "MJPG"

    def test_load_video_unreadable_falls_back_to_file_info(self, tmp_path):
        path = tmp_path / "fake.avi"
        path.write_bytes(b"pas une vraie video")

        data = _ExifDataLoader._load_video(str(path))

        assert data["type"] == "video"
        assert data["size"] > 0
        assert "width" not in data

    def test_run_emits_none_for_missing_file(self, qtbot):
        loader = _ExifDataLoader("C:/nulle/part/x.jpg")
        results = []
        loader.data_ready.connect(lambda p, d: results.append((p, d)))

        loader.run()   # synchrone : tracé par coverage

        assert results == [("C:/nulle/part/x.jpg", None)]

    def test_run_routes_video_extension(self, qtbot, tmp_path):
        path = _make_avi(tmp_path / "v.avi")
        loader = _ExifDataLoader(path)
        results = []
        loader.data_ready.connect(lambda p, d: results.append(d))

        loader.run()

        assert results[0]["type"] == "video"


# ---------------------------------------------------------------------------
# panneau

class TestExifPanelPopulation:
    def test_image_data_populates_sections_and_rows(self, qtbot, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        panel = ExifPanel()
        qtbot.addWidget(panel)
        panel._current_path = path

        data = _ExifDataLoader._load_image(path)
        panel._on_data_ready(path, data)

        texts = _panel_texts(panel)
        assert "FICHIER" in texts
        assert "APPAREIL PHOTO" in texts
        assert "PixelCam" in texts
        assert "PRISE DE VUE" in texts
        assert "15/06/2024  10:30:00" in texts
        assert "f/2.8" in texts
        assert "80 × 60 px" in texts

    def test_video_data_populates_video_section(self, qtbot, tmp_path):
        path = _make_avi(tmp_path / "v.avi")
        panel = ExifPanel()
        qtbot.addWidget(panel)
        panel._current_path = path

        data = _ExifDataLoader._load_video(path)
        panel._on_data_ready(path, data)

        texts = _panel_texts(panel)
        assert "VIDÉO" in texts
        assert "64 × 48 px" in texts
        assert any(t.startswith("0:00") for t in texts)   # durée < 1 min

    def test_video_without_stream_shows_file_section_only(self, qtbot, tmp_path):
        path = tmp_path / "fake.avi"
        path.write_bytes(b"xxx")
        panel = ExifPanel()
        qtbot.addWidget(panel)
        panel._current_path = str(path)

        panel._on_data_ready(str(path), _ExifDataLoader._load_video(str(path)))

        texts = _panel_texts(panel)
        assert "FICHIER" in texts
        assert "VIDÉO" not in texts

    def test_none_data_shows_error_row(self, qtbot):
        panel = ExifPanel()
        qtbot.addWidget(panel)
        panel._current_path = "C:/x.jpg"

        panel._on_data_ready("C:/x.jpg", None)

        assert "Impossible de lire les métadonnées" in _panel_texts(panel)

    def test_stale_result_is_ignored(self, qtbot, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        panel = ExifPanel()
        qtbot.addWidget(panel)
        panel._current_path = "C:/autre/photo.jpg"   # navigation entre-temps

        panel._on_data_ready(path, _ExifDataLoader._load_image(path))

        assert _panel_texts(panel) == []

    def test_set_photo_real_thread_populates(self, qtbot, tmp_path):
        """Plomberie cross-thread réelle (un vrai .start() par module, cf. CLAUDE.md)."""
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        panel = ExifPanel()
        qtbot.addWidget(panel)

        panel.set_photo(path)
        with qtbot.waitSignal(panel._loader.data_ready, timeout=3000):
            pass

        assert panel._btn_edit.isEnabled()
        assert "PixelCam" in _panel_texts(panel)

    def test_set_photo_video_disables_edit_button(self, qtbot, tmp_path):
        path = _make_avi(tmp_path / "v.avi")
        panel = ExifPanel()
        qtbot.addWidget(panel)

        panel.set_photo(path)
        with qtbot.waitSignal(panel._loader.data_ready, timeout=3000):
            pass

        assert panel._is_video
        assert not panel._btn_edit.isEnabled()

    def test_clear_resets_state(self, qtbot, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        panel = ExifPanel()
        qtbot.addWidget(panel)
        panel._current_path = path
        panel._on_data_ready(path, _ExifDataLoader._load_image(path))

        panel.clear()

        assert panel._current_path == ""
        assert not panel._btn_edit.isEnabled()
        # Les lignes sont détruites via deleteLater : attendre le passage de
        # l'event loop (cf. convention : processEvents, pas d'assertion immédiate)
        qtbot.waitUntil(lambda: _panel_texts(panel) == [], timeout=2000)


class TestExifPanelGps:
    def _gps_ifd(self, extra: dict | None = None):
        # Clés entières GPSTAGS : 1/2 latitude, 3/4 longitude
        ifd = {
            1: "N", 2: (48.0, 51.0, 24.0),
            3: "E", 4: (2.0, 17.0, 40.0),
        }
        if extra:
            ifd.update(extra)
        return ifd

    def test_gps_section_with_coordinates(self, qtbot):
        panel = ExifPanel()
        qtbot.addWidget(panel)

        panel._populate_gps(self._gps_ifd())

        texts = _panel_texts(panel)
        assert "GPS" in texts
        assert any(t.startswith("48.856") and t.endswith("N") for t in texts)
        assert any(t.startswith("2.294") and t.endswith("E") for t in texts)

    def test_gps_altitude_speed_direction_datetime_dop(self, qtbot):
        panel = ExifPanel()
        qtbot.addWidget(panel)
        ifd = self._gps_ifd({
            5: 1,                       # GPSAltitudeRef : sous le niveau de la mer
            6: 35.5,                    # GPSAltitude
            12: "K", 13: 42.0,          # GPSSpeedRef / GPSSpeed
            16: "M", 17: 123.4,         # GPSImgDirectionRef / GPSImgDirection
            29: "2024:06:15",           # GPSDateStamp
            7: (10.0, 30.0, 15.5),      # GPSTimeStamp
            11: 4.2,                    # GPSDOP
        })

        panel._populate_gps(ifd)

        texts = _panel_texts(panel)
        assert "-35.5 m" in texts
        assert "42.0 km/h" in texts
        assert any("magnétique" in t for t in texts)
        assert "2024:06:15  10:30:15.5 UTC" in texts
        assert "±4.2 m" in texts

    def test_gps_without_coordinates_adds_nothing(self, qtbot):
        panel = ExifPanel()
        qtbot.addWidget(panel)

        panel._populate_gps({5: 0, 6: 12.0})   # altitude seule, pas de lat/lon

        assert _panel_texts(panel) == []


# ---------------------------------------------------------------------------
# dialogue d'édition

class TestExifEditDialog:
    def test_load_values_prefills_fields(self, qtbot, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")

        dlg = ExifEditDialog(path)
        qtbot.addWidget(dlg)

        assert dlg._dt_edit.dateTime().toString("yyyy:MM:dd HH:mm:ss") == "2024:06:15 10:30:00"
        assert dlg._desc_edit.text() == "Photo de test"
        assert dlg._artist_edit.text() == "Jean Testeur"

    def test_load_values_without_exif_defaults_to_now(self, qtbot, tmp_path):
        path = _make_plain_jpeg(tmp_path / "p.jpg")

        dlg = ExifEditDialog(path)
        qtbot.addWidget(dlg)

        assert dlg._dt_edit.dateTime().date().year() >= 2026
        assert dlg._desc_edit.text() == ""

    def test_write_exif_updates_file(self, qtbot, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        dlg = ExifEditDialog(path)
        qtbot.addWidget(dlg)
        from PySide6.QtCore import QDateTime
        dlg._dt_edit.setDateTime(QDateTime(2021, 3, 4, 5, 6, 7))
        dlg._desc_edit.setText("Nouvelle description")
        dlg._artist_edit.setText("")             # efface Artist
        # ASCII uniquement : l'encodeur EXIF de Pillow remplace les caractères
        # non-ASCII par "?" dans les tags texte de l'IFD principal.
        dlg._copyright_edit.setText("(c) Test")
        dlg._cb_file_date.setChecked(True)

        dlg._write_exif()

        with Image.open(path) as img:
            exif = _read_exif(img)
        assert exif["DateTimeOriginal"] == "2021:03:04 05:06:07"
        assert exif["ImageDescription"] == "Nouvelle description"
        assert exif["Copyright"] == "(c) Test"
        assert "Artist" not in exif or not str(exif["Artist"]).strip()
        from datetime import timezone
        dt = datetime(2021, 3, 4, 5, 6, 7)
        mtime = os.stat(path).st_mtime
        assert any(
            abs(mtime - ts) < 2
            for ts in (dt.timestamp(), dt.replace(tzinfo=timezone.utc).timestamp())
        )

    def test_on_save_accepts_dialog(self, qtbot, tmp_path):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        dlg = ExifEditDialog(path)
        qtbot.addWidget(dlg)

        dlg._on_save()

        assert dlg.result() == QDialog.Accepted

    def test_edit_clicked_reloads_and_emits_photo_saved(self, qtbot, tmp_path, monkeypatch):
        path = _make_jpeg_with_exif(tmp_path / "e.jpg")
        panel = ExifPanel()
        qtbot.addWidget(panel)
        panel._current_path = path
        panel._is_video = False
        monkeypatch.setattr(ExifEditDialog, "exec",
                            lambda self: QDialog.Accepted)

        with qtbot.waitSignal(panel.photo_saved, timeout=3000) as blocker:
            panel._on_edit_clicked()

        assert blocker.args == [path]
        with qtbot.waitSignal(panel._loader.data_ready, timeout=3000):
            pass   # laisse le rechargement réel se terminer avant le teardown
