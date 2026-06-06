"""Panneau latéral affichant les métadonnées EXIF d'une photo."""

import logging
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Groupes et tags curated

_CURATED_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Appareil photo", [
        ("Make",       "Fabricant"),
        ("Model",      "Modèle"),
        ("LensModel",  "Objectif"),
        ("Software",   "Logiciel"),
    ]),
    ("Prise de vue", [
        ("DateTimeOriginal",       "Date"),
        ("ExposureTime",           "Exposition"),
        ("FNumber",                "Ouverture"),
        ("ISOSpeedRatings",        "ISO"),
        ("FocalLength",            "Focale"),
        ("FocalLengthIn35mmFilm",  "Focale éq. 35mm"),
        ("ExposureProgram",        "Programme"),
        ("MeteringMode",           "Mesure"),
        ("WhiteBalance",           "Balance des blancs"),
        ("Flash",                  "Flash"),
        ("ExposureMode",           "Mode expo."),
        ("ExposureBiasValue",      "Correction expo."),
    ]),
    ("Image", [
        ("PixelXDimension",  "Largeur"),
        ("PixelYDimension",  "Hauteur"),
        ("ColorSpace",       "Espace colorimétrique"),
        ("Orientation",      "Orientation"),
        ("ResolutionUnit",   "Unité résolution"),
        ("XResolution",      "Résolution X"),
        ("YResolution",      "Résolution Y"),
    ]),
    ("Auteur / Droits", [
        ("Artist",           "Artiste"),
        ("Copyright",        "Copyright"),
        ("ImageDescription", "Description"),
    ]),
]

# Tags ignorés dans la section "Autres" (binaires, redondants ou illisibles)
_SKIP_IN_EXTRA = {
    "GPSInfo", "MakerNote", "UserComment", "PrintImageMatching",
    "ApplicationNotes", "ExifVersion", "FlashPixVersion",
    "ComponentsConfiguration", "SceneType", "FileSource",
    "DateTimeDigitized", "DateTime", "SubsecTimeOriginal", "SubsecTime",
    "SubsecTimeDigitized", "CFAPattern", "InteroperabilityIFD",
    "Padding", "OffsetSchema", "ThumbnailOffset", "ThumbnailLength",
}
for _grp, _tags in _CURATED_GROUPS:
    for _t, _ in _tags:
        _SKIP_IN_EXTRA.add(_t)

# ---------------------------------------------------------------------------
# Formateurs de valeurs

_EXPOSURE_PROGRAMS = {
    0: "Non défini", 1: "Manuel", 2: "Programme auto",
    3: "Priorité ouverture", 4: "Priorité vitesse",
    5: "Créatif", 6: "Action", 7: "Portrait", 8: "Paysage",
}
_METERING_MODES = {
    0: "Inconnu", 1: "Moyenne", 2: "Centrale pondérée",
    3: "Point", 4: "Multi-point", 5: "Évaluative", 6: "Partielle",
}
_WHITE_BALANCE  = {0: "Auto", 1: "Manuel"}
_EXPOSURE_MODES = {0: "Auto", 1: "Manuel", 2: "Bracketing"}
_COLOR_SPACES   = {1: "sRGB", 65535: "Non calibré"}
_ORIENTATIONS   = {
    1: "Normal", 2: "Miroir H", 3: "180°", 4: "Miroir V",
    5: "Miroir H + 270°", 6: "90° horaire",
    7: "Miroir H + 90°",  8: "270° horaire",
}
_RESOLUTION_UNITS = {1: "Sans unité", 2: "dpi", 3: "dpc"}


def _fmt_exposure(val) -> str:
    try:
        f = float(val)
        if f >= 1:
            return f"{f:.1f} s"
        denom = round(1.0 / f)
        return f"1/{denom} s"
    except Exception:
        return str(val)


def _fmt_value(tag: str, val) -> str:
    """Convertit une valeur EXIF brute en chaîne lisible."""
    if val is None:
        return ""
    try:
        if tag == "ExposureTime":
            return _fmt_exposure(val)
        if tag == "FNumber":
            return f"f/{float(val):.1f}"
        if tag in ("FocalLength", "FocalLengthIn35mmFilm"):
            return f"{float(val):.0f} mm"
        if tag == "ISOSpeedRatings":
            return str(int(val) if not isinstance(val, tuple) else int(val[0]))
        if tag == "DateTimeOriginal":
            dt = datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S")
            return dt.strftime("%d/%m/%Y  %H:%M")
        if tag == "ExposureBiasValue":
            return f"{float(val):+.1f} EV"
        if tag == "ExposureProgram":
            return _EXPOSURE_PROGRAMS.get(int(val), str(val))
        if tag == "MeteringMode":
            return _METERING_MODES.get(int(val), str(val))
        if tag == "WhiteBalance":
            return _WHITE_BALANCE.get(int(val), str(val))
        if tag == "ExposureMode":
            return _EXPOSURE_MODES.get(int(val), str(val))
        if tag == "ColorSpace":
            return _COLOR_SPACES.get(int(val), str(val))
        if tag == "Orientation":
            return _ORIENTATIONS.get(int(val), str(val))
        if tag == "Flash":
            return "Déclenché" if (int(val) & 0x1) == 1 else "Non déclenché"
        if tag == "ResolutionUnit":
            return _RESOLUTION_UNITS.get(int(val), str(val))
        if tag in ("XResolution", "YResolution"):
            return f"{float(val):.0f}"
    except Exception:
        pass
    if isinstance(val, bytes):
        return "(données binaires)"
    return str(val).strip()


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} o"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} Ko"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} Mo"
    return f"{n / 1024 ** 3:.1f} Go"


# ---------------------------------------------------------------------------
# Widget

class ExifPanel(QWidget):
    """Panneau scrollable affichant les métadonnées EXIF d'une photo."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(260)
        self._setup_ui()

    # ------------------------------------------------------------------ UI

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QLabel("  Métadonnées EXIF")
        header.setStyleSheet(
            "background: #2a2a2a; color: #ccc; font-weight: bold;"
            "padding: 8px 0; border-bottom: 1px solid #444;"
        )
        header.setFixedHeight(36)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("border: none;")

        self._content = QWidget()
        self._content.setStyleSheet("background: #1e1e1e;")
        self._rows_layout = QVBoxLayout(self._content)
        self._rows_layout.setContentsMargins(0, 4, 0, 8)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch()

        scroll.setWidget(self._content)
        root.addWidget(scroll)

    # ------------------------------------------------------------------ API publique

    def set_photo(self, photo_path: str) -> None:
        self._clear()
        self._populate(photo_path)

    def clear(self) -> None:
        self._clear()

    # ------------------------------------------------------------------ private

    def _clear(self) -> None:
        while self._rows_layout.count() > 1:   # conserve le stretch final
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _populate(self, photo_path: str) -> None:
        from src.library.exif_reader import VIDEO_EXT
        if Path(photo_path).suffix.lower() in VIDEO_EXT:
            self._populate_video(photo_path)
            return
        try:
            from PIL import Image, ExifTags, ImageOps
            with Image.open(photo_path) as img:
                img = ImageOps.exif_transpose(img)
                width, height = img.size
                raw = img._getexif() if hasattr(img, "_getexif") else None
                exif: dict = {}
                if raw:
                    exif = {ExifTags.TAGS.get(k, str(k)): v for k, v in raw.items()}
        except Exception as e:
            logger.debug("ExifPanel: impossible de lire %s — %s", photo_path, e)
            self._add_row("", "Impossible de lire les métadonnées", error=True)
            return

        # Fichier
        try:
            stat = os.stat(photo_path)
            self._add_section("Fichier")
            self._add_row("Nom",        Path(photo_path).name)
            self._add_row("Dimensions", f"{width} × {height} px")
            self._add_row("Taille",     _fmt_size(stat.st_size))
        except OSError:
            pass

        # Groupes curated
        for group_name, tags in _CURATED_GROUPS:
            rows = [
                (label, _fmt_value(tag, exif[tag]))
                for tag, label in tags
                if tag in exif and _fmt_value(tag, exif[tag])
            ]
            if rows:
                self._add_section(group_name)
                for label, val in rows:
                    self._add_row(label, val)

        # GPS
        if "GPSInfo" in exif:
            try:
                from src.library.exif_reader import ExifReader
                coords = ExifReader._parse_gps(exif["GPSInfo"])
                if coords:
                    lat, lon = coords
                    self._add_section("GPS")
                    self._add_row("Latitude",  f"{lat:.6f}°")
                    self._add_row("Longitude", f"{lon:.6f}°")
            except Exception:
                pass

        # Tags supplémentaires
        extra = sorted(
            (tag, _fmt_value(tag, val))
            for tag, val in exif.items()
            if tag not in _SKIP_IN_EXTRA
            and not isinstance(val, bytes)
            and _fmt_value(tag, val)
        )
        if extra:
            self._add_section("Autres")
            for tag, val in extra:
                self._add_row(tag, val)

    def _populate_video(self, video_path: str) -> None:
        try:
            stat = os.stat(video_path)
            self._add_section("Fichier")
            self._add_row("Nom",    Path(video_path).name)
            self._add_row("Taille", _fmt_size(stat.st_size))
        except OSError:
            pass

        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                fc  = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                cap.release()
                self._add_section("Vidéo")
                if w and h:
                    self._add_row("Résolution", f"{w} × {h} px")
                if fps > 0:
                    self._add_row("Images/s", f"{fps:.2f}")
                if fps > 0 and fc > 0:
                    dur = fc / fps
                    m, s = divmod(int(dur), 60)
                    h2, m = divmod(m, 60)
                    if h2:
                        self._add_row("Durée", f"{h2}:{m:02d}:{s:02d}")
                    else:
                        self._add_row("Durée", f"{m}:{s:02d}")
        except Exception as e:
            logger.debug("ExifPanel vidéo %s: %s", video_path, e)

    def _add_section(self, title: str) -> None:
        lbl = QLabel(title.upper())
        lbl.setStyleSheet(
            "color: #666; font-size: 9px; font-weight: bold; letter-spacing: 1px;"
            "padding: 10px 10px 2px 10px; background: #1e1e1e;"
        )
        self._insert(lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2e2e2e; margin: 0 10px 2px 10px;")
        self._insert(sep)

    def _add_row(self, label: str, value: str, error: bool = False) -> None:
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(10, 2, 10, 2)
        hl.setSpacing(8)

        if label:
            lbl_key = QLabel(label)
            lbl_key.setStyleSheet("color: #777; font-size: 11px;")
            lbl_key.setFixedWidth(110)
            lbl_key.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            hl.addWidget(lbl_key)

        color = "#f99" if error else "#ddd"
        lbl_val = QLabel(value)
        lbl_val.setStyleSheet(f"color: {color}; font-size: 11px;")
        lbl_val.setWordWrap(True)
        lbl_val.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lbl_val.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        hl.addWidget(lbl_val, stretch=1)

        self._insert(row)

    def _insert(self, widget: QWidget) -> None:
        """Insère avant le stretch final."""
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, widget)
