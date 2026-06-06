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
        ("Make",             "Fabricant"),
        ("Model",            "Modèle"),
        ("SerialNumber",     "N° de série"),
        ("LensMake",         "Fabricant objectif"),
        ("LensModel",        "Objectif"),
        ("LensSpecification","Spéc. objectif"),
        ("LensSerialNumber", "N° série objectif"),
        ("Software",         "Logiciel"),
    ]),
    ("Prise de vue", [
        ("DateTimeOriginal",      "Date"),
        ("ExposureTime",          "Exposition"),
        ("FNumber",               "Ouverture"),
        ("MaxApertureValue",      "Ouverture max"),
        ("ISOSpeedRatings",       "ISO"),
        ("SensitivityType",       "Type ISO"),
        ("RecommendedExposureIndex", "Indice expo. recommandé"),
        ("FocalLength",           "Focale"),
        ("FocalLengthIn35mmFilm", "Focale éq. 35 mm"),
        ("DigitalZoomRatio",      "Zoom numérique"),
        ("ExposureProgram",       "Programme"),
        ("MeteringMode",          "Mesure"),
        ("ExposureMode",          "Mode expo."),
        ("ExposureBiasValue",     "Correction expo."),
        ("BrightnessValue",       "Valeur de luminosité"),
        ("WhiteBalance",          "Balance des blancs"),
        ("LightSource",           "Source lumière"),
        ("Flash",                 "Flash"),
        ("SceneCaptureType",      "Type de scène"),
        ("SceneType",             "Type scène brut"),
        ("SubjectDistance",       "Distance sujet (m)"),
        ("SubjectDistanceRange",  "Gamme distance sujet"),
        ("GainControl",           "Contrôle gain"),
        ("Contrast",              "Contraste"),
        ("Saturation",            "Saturation"),
        ("Sharpness",             "Netteté"),
        ("CustomRendered",        "Rendu personnalisé"),
    ]),
    ("Image", [
        ("PixelXDimension",  "Largeur (px)"),
        ("PixelYDimension",  "Hauteur (px)"),
        ("ColorSpace",       "Espace colorimétrique"),
        ("Orientation",      "Orientation"),
        ("ResolutionUnit",   "Unité résolution"),
        ("XResolution",      "Résolution X"),
        ("YResolution",      "Résolution Y"),
        ("Compression",      "Compression"),
        ("BitsPerSample",    "Bits par canal"),
        ("SamplesPerPixel",  "Canaux"),
    ]),
    ("Auteur / Droits", [
        ("Artist",           "Artiste"),
        ("Copyright",        "Copyright"),
        ("ImageDescription", "Description"),
        ("XPTitle",          "Titre (Windows)"),
        ("XPComment",        "Commentaire (Windows)"),
        ("XPAuthor",         "Auteur (Windows)"),
        ("XPKeywords",       "Mots-clés (Windows)"),
        ("XPSubject",        "Sujet (Windows)"),
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
    "ExifOffset", "GPSInfoIFDPointer", "InteroperabilityIFDPointer",
}
for _grp, _tags in _CURATED_GROUPS:
    for _t, _ in _tags:
        _SKIP_IN_EXTRA.add(_t)

# ---------------------------------------------------------------------------
# Tables de décodage

_EXPOSURE_PROGRAMS = {
    0: "Non défini", 1: "Manuel", 2: "Programme auto",
    3: "Priorité ouverture", 4: "Priorité vitesse",
    5: "Créatif (profondeur)", 6: "Action (vitesse)", 7: "Portrait", 8: "Paysage",
}
_METERING_MODES = {
    0: "Inconnu", 1: "Moyenne", 2: "Centrale pondérée",
    3: "Point", 4: "Multi-point", 5: "Évaluative", 6: "Partielle",
}
_WHITE_BALANCE  = {0: "Auto", 1: "Manuel"}
_EXPOSURE_MODES = {0: "Auto", 1: "Manuel", 2: "Bracketing"}
_COLOR_SPACES   = {1: "sRGB", 2: "Adobe RGB", 65535: "Non calibré"}
_ORIENTATIONS   = {
    1: "Normal", 2: "Miroir H", 3: "180°", 4: "Miroir V",
    5: "Miroir H + 270°", 6: "90° horaire",
    7: "Miroir H + 90°",  8: "270° horaire",
}
_RESOLUTION_UNITS = {1: "Sans unité", 2: "dpi", 3: "dpc"}
_SCENE_CAPTURE = {0: "Standard", 1: "Paysage", 2: "Portrait", 3: "Nuit"}
_LIGHT_SOURCES = {
    0: "Inconnu", 1: "Lumière du jour", 2: "Fluorescent",
    3: "Tungstène (ampoule)", 4: "Flash", 9: "Beau temps",
    10: "Nuageux", 11: "Ombre", 12: "Fluorescent lumière du jour (D)",
    13: "Fluorescent blanc (N)", 14: "Fluorescent blanc chaud (W)",
    15: "Fluorescent blanc froid", 17: "Lampe standard A",
    18: "Lampe standard B", 19: "Lampe standard C",
    20: "D55", 21: "D65", 22: "D75", 255: "Autre",
}
_GAIN_CONTROLS = {
    0: "Aucun", 1: "Faible gain +", 2: "Fort gain +",
    3: "Faible gain -", 4: "Fort gain -",
}
_PROCESS_VALUES = {0: "Normal", 1: "Doux", 2: "Fort"}
_SUBJECT_DIST_RANGES = {
    0: "Inconnu", 1: "Macro", 2: "Vue proche", 3: "Vue distante",
}
_SENSITIVITY_TYPES = {
    0: "Inconnu", 1: "SOS", 2: "REI", 3: "Sensibilité ISO standard",
    4: "SOS + REI", 5: "SOS + ISO", 6: "REI + ISO", 7: "SOS + REI + ISO",
}
_CUSTOM_RENDERED = {0: "Processus normal", 1: "Processus personnalisé"}
_FLASH_DECODE = {
    0x00: "Non déclenché",
    0x01: "Déclenché",
    0x05: "Déclenché, retour non détecté",
    0x07: "Déclenché, retour détecté",
    0x08: "On, non déclenché",
    0x09: "On, déclenché",
    0x0D: "On, retour non détecté",
    0x0F: "On, retour détecté",
    0x10: "Off, non déclenché",
    0x18: "Off, retour non détecté",
    0x19: "Off, déclenché",
    0x1D: "Off, retour non détecté",
    0x1F: "Off, retour détecté",
    0x20: "Auto, non déclenché",
    0x24: "Auto, non déclenché, réduction yeux rouges",
    0x25: "Auto, déclenché",
    0x27: "Auto, retour détecté",
    0x29: "Auto, déclenché, yeux rouges",
    0x2F: "Auto, retour détecté, yeux rouges",
    0x30: "Flash absent",
    0x41: "Déclenché, yeux rouges",
    0x45: "Déclenché, retour non détecté, yeux rouges",
    0x47: "Déclenché, retour détecté, yeux rouges",
    0x49: "On, déclenché, yeux rouges",
    0x4D: "On, retour non détecté, yeux rouges",
    0x4F: "On, retour détecté, yeux rouges",
    0x59: "Auto, déclenché, yeux rouges",
    0x5D: "Auto, retour non détecté, yeux rouges",
    0x5F: "Auto, retour détecté, yeux rouges",
}
_COMPRESSIONS = {
    1: "Non compressé", 2: "CCITT 1D", 3: "CCITT Groupe 3", 4: "CCITT Groupe 4",
    5: "LZW", 6: "JPEG (old)", 7: "JPEG", 8: "Deflate/ZIP",
    32773: "PackBits (Mac)", 34713: "Nikon NEF compressé",
}


# ---------------------------------------------------------------------------
# Formateurs

def _fmt_exposure(val) -> str:
    try:
        f = float(val)
        if f >= 1:
            return f"{f:.1f} s"
        denom = round(1.0 / f)
        return f"1/{denom} s"
    except Exception:
        return str(val)


def _fmt_apex(val) -> str:
    """Convertit une valeur APEX en f/ (pour MaxApertureValue)."""
    try:
        import math
        return f"f/{math.sqrt(2 ** float(val)):.1f}"
    except Exception:
        return str(val)


def _decode_xp_str(val) -> str:
    """Décode les champs XP* Windows (encodés en UTF-16LE bytes)."""
    try:
        if isinstance(val, bytes):
            return val.decode("utf-16-le").rstrip("\x00")
        return str(val)
    except Exception:
        return ""


def _fmt_lens_spec(val) -> str:
    """Formate LensSpecification [min_fl, max_fl, min_fn, max_fn]."""
    try:
        parts = [float(v) for v in val]
        if len(parts) == 4:
            fl = f"{parts[0]:.0f}–{parts[1]:.0f} mm" if parts[0] != parts[1] else f"{parts[0]:.0f} mm"
            fn = f"f/{parts[2]:.1f}–f/{parts[3]:.1f}" if parts[2] != parts[3] else f"f/{parts[2]:.1f}"
            return f"{fl}  {fn}"
    except Exception:
        pass
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
        if tag == "MaxApertureValue":
            return _fmt_apex(val)
        if tag in ("FocalLength",):
            return f"{float(val):.0f} mm"
        if tag == "FocalLengthIn35mmFilm":
            return f"{int(val)} mm"
        if tag == "ISOSpeedRatings":
            return str(int(val) if not isinstance(val, tuple) else int(val[0]))
        if tag == "RecommendedExposureIndex":
            return str(int(val))
        if tag == "DateTimeOriginal":
            dt = datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S")
            return dt.strftime("%d/%m/%Y  %H:%M:%S")
        if tag == "ExposureBiasValue":
            return f"{float(val):+.1f} EV"
        if tag == "BrightnessValue":
            return f"{float(val):.2f} EV"
        if tag == "DigitalZoomRatio":
            z = float(val)
            return "Pas de zoom" if z == 0 or z == 1 else f"×{z:.2f}"
        if tag == "SubjectDistance":
            f = float(val)
            return f"{f:.2f} m" if f < 9999 else "Infini"
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
            return _FLASH_DECODE.get(int(val), f"0x{int(val):02X}")
        if tag == "ResolutionUnit":
            return _RESOLUTION_UNITS.get(int(val), str(val))
        if tag in ("XResolution", "YResolution"):
            return f"{float(val):.0f}"
        if tag == "SceneCaptureType":
            return _SCENE_CAPTURE.get(int(val), str(val))
        if tag == "LightSource":
            return _LIGHT_SOURCES.get(int(val), str(val))
        if tag == "GainControl":
            return _GAIN_CONTROLS.get(int(val), str(val))
        if tag in ("Contrast", "Saturation", "Sharpness"):
            return _PROCESS_VALUES.get(int(val), str(val))
        if tag == "SubjectDistanceRange":
            return _SUBJECT_DIST_RANGES.get(int(val), str(val))
        if tag == "SensitivityType":
            return _SENSITIVITY_TYPES.get(int(val), str(val))
        if tag == "CustomRendered":
            return _CUSTOM_RENDERED.get(int(val), str(val))
        if tag == "Compression":
            return _COMPRESSIONS.get(int(val), str(val))
        if tag == "LensSpecification":
            return _fmt_lens_spec(val)
        if tag in ("XPTitle", "XPComment", "XPAuthor", "XPKeywords", "XPSubject"):
            s = _decode_xp_str(val)
            return s if s else ""
    except Exception:
        pass
    if isinstance(val, bytes):
        return "(données binaires)"
    if isinstance(val, (list, tuple)):
        return ", ".join(str(v) for v in val)
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
# Lecture EXIF robuste (JPEG + TIFF + WebP + PNG)

def _read_exif(img) -> dict:
    """
    Lit tous les tags EXIF via l'API publique Pillow (getexif + sub-IFD).
    Fonctionne pour JPEG, TIFF, WebP et PNG avec chunk EXIF.
    """
    from PIL import ExifTags
    exif: dict = {}
    try:
        exif_obj = img.getexif()
        if not exif_obj:
            return exif

        # IFD principal (Make, Model, Orientation, résolution…)
        for tag_id, val in exif_obj.items():
            tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
            exif[tag_name] = val

        # ExifIFD (0x8769) : exposition, objectif, ISO, flash…
        try:
            exif_ifd = exif_obj.get_ifd(0x8769)
            for tag_id, val in exif_ifd.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                exif.setdefault(tag_name, val)
        except Exception:
            pass

        # GPS IFD (0x8825)
        try:
            gps_ifd = exif_obj.get_ifd(0x8825)
            if gps_ifd:
                exif["GPSInfo"] = gps_ifd
        except Exception:
            pass

        # Interoperability IFD (0xa005) — optionnel
        try:
            interop = exif_obj.get_ifd(0xa005)
            for tag_id, val in interop.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                exif.setdefault(tag_name, val)
        except Exception:
            pass

    except Exception:
        pass
    return exif


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
        while self._rows_layout.count() > 1:
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
            from PIL import Image, ImageOps
            with Image.open(photo_path) as img:
                img_format = img.format or Path(photo_path).suffix.upper().lstrip(".")
                img_mode   = img.mode
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                width, height = img.size
                exif = _read_exif(img)
        except Exception as e:
            logger.debug("ExifPanel: impossible de lire %s — %s", photo_path, e)
            self._add_row("", "Impossible de lire les métadonnées", error=True)
            return

        # Fichier
        try:
            stat = os.stat(photo_path)
            self._add_section("Fichier")
            self._add_row("Nom",          Path(photo_path).name)
            self._add_row("Format",       img_format)
            self._add_row("Mode couleur", img_mode)
            self._add_row("Dimensions",   f"{width} × {height} px")
            self._add_row("Taille",       _fmt_size(stat.st_size))
            mtime = datetime.fromtimestamp(stat.st_mtime)
            self._add_row("Modifié",      mtime.strftime("%d/%m/%Y  %H:%M"))
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
                self._populate_gps(exif["GPSInfo"])
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

    def _populate_gps(self, gps_info) -> None:
        from PIL import ExifTags
        from src.library.exif_reader import ExifReader

        # Normalise : dict {tag_id: val} → dict {tag_name: val}
        if gps_info and isinstance(next(iter(gps_info)), int):
            gps_tags = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in gps_info.items()}
        else:
            gps_tags = gps_info  # déjà des noms

        coords = ExifReader._parse_gps(gps_info)
        if not coords:
            return

        lat, lon = coords
        self._add_section("GPS")
        self._add_row("Latitude",  f"{abs(lat):.6f}°  {'N' if lat >= 0 else 'S'}")
        self._add_row("Longitude", f"{abs(lon):.6f}°  {'E' if lon >= 0 else 'O'}")

        # Altitude
        alt = gps_tags.get("GPSAltitude")
        if alt is not None:
            try:
                alt_m = float(alt)
                alt_ref = gps_tags.get("GPSAltitudeRef", 0)
                if alt_ref == 1:
                    alt_m = -alt_m
                self._add_row("Altitude", f"{alt_m:.1f} m")
            except Exception:
                pass

        # Vitesse GPS
        speed = gps_tags.get("GPSSpeed")
        if speed is not None:
            try:
                spd = float(speed)
                ref  = gps_tags.get("GPSSpeedRef", "K")
                unit = {"K": "km/h", "M": "mph", "N": "nœuds"}.get(ref, ref)
                self._add_row("Vitesse GPS", f"{spd:.1f} {unit}")
            except Exception:
                pass

        # Direction de l'image
        direction = gps_tags.get("GPSImgDirection")
        if direction is not None:
            try:
                deg = float(direction)
                ref = gps_tags.get("GPSImgDirectionRef", "T")
                label = "Direction (magnétique)" if ref == "M" else "Direction (vraie)"
                self._add_row(label, f"{deg:.1f}°")
            except Exception:
                pass

        # Date/heure GPS
        gps_date = gps_tags.get("GPSDateStamp")
        gps_time = gps_tags.get("GPSTimeStamp")
        if gps_date and gps_time:
            try:
                h, m, s = (float(x) for x in gps_time)
                self._add_row("Date/heure GPS",
                              f"{gps_date}  {int(h):02d}:{int(m):02d}:{s:04.1f} UTC")
            except Exception:
                pass

        # DOP (précision)
        hdop = gps_tags.get("GPSDOP") or gps_tags.get("GPSHPositioningError")
        if hdop is not None:
            try:
                self._add_row("Précision GPS", f"±{float(hdop):.1f} m")
            except Exception:
                pass

    def _populate_video(self, video_path: str) -> None:
        try:
            stat = os.stat(video_path)
            self._add_section("Fichier")
            self._add_row("Nom",      Path(video_path).name)
            self._add_row("Format",   Path(video_path).suffix.upper().lstrip("."))
            self._add_row("Taille",   _fmt_size(stat.st_size))
            mtime = datetime.fromtimestamp(stat.st_mtime)
            self._add_row("Modifié", mtime.strftime("%d/%m/%Y  %H:%M"))
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
                codec_raw = int(cap.get(cv2.CAP_PROP_FOURCC))
                cap.release()
                self._add_section("Vidéo")
                if w and h:
                    self._add_row("Résolution", f"{w} × {h} px")
                if fps > 0:
                    self._add_row("Images/s", f"{fps:.3f}")
                if fps > 0 and fc > 0:
                    dur = fc / fps
                    hh, rem = divmod(int(dur), 3600)
                    mm, ss  = divmod(rem, 60)
                    if hh:
                        self._add_row("Durée", f"{hh}:{mm:02d}:{ss:02d}")
                    else:
                        self._add_row("Durée", f"{mm}:{ss:02d}")
                    self._add_row("Nb images", f"{int(fc):,}".replace(",", " "))
                if codec_raw:
                    codec = "".join(chr((codec_raw >> 8 * i) & 0xFF) for i in range(4)).strip()
                    if codec:
                        self._add_row("Codec", codec)
        except Exception as e:
            logger.debug("ExifPanel vidéo %s: %s", video_path, e)

    # ------------------------------------------------------------------ widgets

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
            lbl_key.setFixedWidth(120)
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
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, widget)
