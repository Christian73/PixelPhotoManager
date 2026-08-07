"""Panneau latéral affichant les métadonnées EXIF d'une photo."""

import io
import logging
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDateTime, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)
from src.core.i18n import translate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Groupes et tags curated

_CURATED_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (translate("ExifPanel", "Appareil photo"), [
        ("Make",             translate("ExifPanel", "Fabricant")),
        ("Model",            translate("ExifPanel", "Modèle")),
        ("SerialNumber",     translate("ExifPanel", "N° de série")),
        ("LensMake",         translate("ExifPanel", "Fabricant objectif")),
        ("LensModel",        translate("ExifPanel", "Objectif")),
        ("LensSpecification",translate("ExifPanel", "Spéc. objectif")),
        ("LensSerialNumber", translate("ExifPanel", "N° série objectif")),
        ("Software",         translate("ExifPanel", "Logiciel")),
    ]),
    (translate("ExifPanel", "Prise de vue"), [
        ("DateTimeOriginal",      translate("ExifPanel", "Date")),
        ("ExposureTime",          translate("ExifPanel", "Exposition")),
        ("FNumber",               translate("ExifPanel", "Ouverture")),
        ("MaxApertureValue",      translate("ExifPanel", "Ouverture max")),
        ("ISOSpeedRatings",       translate("ExifPanel", "ISO")),
        ("SensitivityType",       translate("ExifPanel", "Type ISO")),
        ("RecommendedExposureIndex", translate("ExifPanel", "Indice expo. recommandé")),
        ("FocalLength",           translate("ExifPanel", "Focale")),
        ("FocalLengthIn35mmFilm", translate("ExifPanel", "Focale éq. 35 mm")),
        ("DigitalZoomRatio",      translate("ExifPanel", "Zoom numérique")),
        ("ExposureProgram",       translate("ExifPanel", "Programme")),
        ("MeteringMode",          translate("ExifPanel", "Mesure")),
        ("ExposureMode",          translate("ExifPanel", "Mode expo.")),
        ("ExposureBiasValue",     translate("ExifPanel", "Correction expo.")),
        ("BrightnessValue",       translate("ExifPanel", "Valeur de luminosité")),
        ("WhiteBalance",          translate("ExifPanel", "Balance des blancs")),
        ("LightSource",           translate("ExifPanel", "Source lumière")),
        ("Flash",                 translate("ExifPanel", "Flash")),
        ("SceneCaptureType",      translate("ExifPanel", "Type de scène")),
        ("SceneType",             translate("ExifPanel", "Type scène brut")),
        ("SubjectDistance",       translate("ExifPanel", "Distance sujet (m)")),
        ("SubjectDistanceRange",  translate("ExifPanel", "Gamme distance sujet")),
        ("GainControl",           translate("ExifPanel", "Contrôle gain")),
        ("Contrast",              translate("ExifPanel", "Contraste")),
        ("Saturation",            translate("ExifPanel", "Saturation")),
        ("Sharpness",             translate("ExifPanel", "Netteté")),
        ("CustomRendered",        translate("ExifPanel", "Rendu personnalisé")),
    ]),
    (translate("ExifPanel", "Image"), [
        ("PixelXDimension",  translate("ExifPanel", "Largeur (px)")),
        ("PixelYDimension",  translate("ExifPanel", "Hauteur (px)")),
        ("ColorSpace",       translate("ExifPanel", "Espace colorimétrique")),
        ("Orientation",      translate("ExifPanel", "Orientation")),
        ("ResolutionUnit",   translate("ExifPanel", "Unité résolution")),
        ("XResolution",      translate("ExifPanel", "Résolution X")),
        ("YResolution",      translate("ExifPanel", "Résolution Y")),
        ("Compression",      translate("ExifPanel", "Compression")),
        ("BitsPerSample",    translate("ExifPanel", "Bits par canal")),
        ("SamplesPerPixel",  translate("ExifPanel", "Canaux")),
    ]),
    (translate("ExifPanel", "Auteur / Droits"), [
        ("Artist",           translate("ExifPanel", "Artiste")),
        ("Copyright",        translate("ExifPanel", "Copyright")),
        ("ImageDescription", translate("ExifPanel", "Description")),
        ("XPTitle",          translate("ExifPanel", "Titre (Windows)")),
        ("XPComment",        translate("ExifPanel", "Commentaire (Windows)")),
        ("XPAuthor",         translate("ExifPanel", "Auteur (Windows)")),
        ("XPKeywords",       translate("ExifPanel", "Mots-clés (Windows)")),
        ("XPSubject",        translate("ExifPanel", "Sujet (Windows)")),
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
    0: translate("ExifPanel", "Non défini"), 1: translate("ExifPanel", "Manuel"), 2: translate("ExifPanel", "Programme auto"),
    3: translate("ExifPanel", "Priorité ouverture"), 4: translate("ExifPanel", "Priorité vitesse"),
    5: translate("ExifPanel", "Créatif (profondeur)"), 6: translate("ExifPanel", "Action (vitesse)"), 7: translate("ExifPanel", "Portrait"), 8: translate("ExifPanel", "Paysage"),
}
_METERING_MODES = {
    0: translate("ExifPanel", "Inconnu"), 1: translate("ExifPanel", "Moyenne"), 2: translate("ExifPanel", "Centrale pondérée"),
    3: translate("ExifPanel", "Point"), 4: translate("ExifPanel", "Multi-point"), 5: translate("ExifPanel", "Évaluative"), 6: translate("ExifPanel", "Partielle"),
}
_WHITE_BALANCE  = {0: translate("ExifPanel", "Auto"), 1: translate("ExifPanel", "Manuel")}
_EXPOSURE_MODES = {0: translate("ExifPanel", "Auto"), 1: translate("ExifPanel", "Manuel"), 2: translate("ExifPanel", "Bracketing")}
_COLOR_SPACES   = {1: translate("ExifPanel", "sRGB"), 2: translate("ExifPanel", "Adobe RGB"), 65535: translate("ExifPanel", "Non calibré")}
_ORIENTATIONS   = {
    1: translate("ExifPanel", "Normal"), 2: translate("ExifPanel", "Miroir H"), 3: translate("ExifPanel", "180°"), 4: translate("ExifPanel", "Miroir V"),
    5: translate("ExifPanel", "Miroir H + 270°"), 6: translate("ExifPanel", "90° horaire"),
    7: translate("ExifPanel", "Miroir H + 90°"),  8: translate("ExifPanel", "270° horaire"),
}
_RESOLUTION_UNITS = {1: translate("ExifPanel", "Sans unité"), 2: translate("ExifPanel", "dpi"), 3: translate("ExifPanel", "dpc")}
_SCENE_CAPTURE = {0: translate("ExifPanel", "Standard"), 1: translate("ExifPanel", "Paysage"), 2: translate("ExifPanel", "Portrait"), 3: translate("ExifPanel", "Nuit")}
_LIGHT_SOURCES = {
    0: translate("ExifPanel", "Inconnu"), 1: translate("ExifPanel", "Lumière du jour"), 2: translate("ExifPanel", "Fluorescent"),
    3: translate("ExifPanel", "Tungstène (ampoule)"), 4: translate("ExifPanel", "Flash"), 9: translate("ExifPanel", "Beau temps"),
    10: translate("ExifPanel", "Nuageux"), 11: translate("ExifPanel", "Ombre"), 12: translate("ExifPanel", "Fluorescent lumière du jour (D)"),
    13: translate("ExifPanel", "Fluorescent blanc (N)"), 14: translate("ExifPanel", "Fluorescent blanc chaud (W)"),
    15: translate("ExifPanel", "Fluorescent blanc froid"), 17: translate("ExifPanel", "Lampe standard A"),
    18: translate("ExifPanel", "Lampe standard B"), 19: translate("ExifPanel", "Lampe standard C"),
    20: translate("ExifPanel", "D55"), 21: translate("ExifPanel", "D65"), 22: translate("ExifPanel", "D75"), 255: translate("ExifPanel", "Autre"),
}
_GAIN_CONTROLS = {
    0: translate("ExifPanel", "Aucun"), 1: translate("ExifPanel", "Faible gain +"), 2: translate("ExifPanel", "Fort gain +"),
    3: translate("ExifPanel", "Faible gain -"), 4: translate("ExifPanel", "Fort gain -"),
}
_PROCESS_VALUES = {0: translate("ExifPanel", "Normal"), 1: translate("ExifPanel", "Doux"), 2: translate("ExifPanel", "Fort")}
_SUBJECT_DIST_RANGES = {
    0: translate("ExifPanel", "Inconnu"), 1: translate("ExifPanel", "Macro"), 2: translate("ExifPanel", "Vue proche"), 3: translate("ExifPanel", "Vue distante"),
}
_SENSITIVITY_TYPES = {
    0: translate("ExifPanel", "Inconnu"), 1: translate("ExifPanel", "SOS"), 2: translate("ExifPanel", "REI"), 3: translate("ExifPanel", "Sensibilité ISO standard"),
    4: translate("ExifPanel", "SOS + REI"), 5: translate("ExifPanel", "SOS + ISO"), 6: translate("ExifPanel", "REI + ISO"), 7: translate("ExifPanel", "SOS + REI + ISO"),
}
_CUSTOM_RENDERED = {0: translate("ExifPanel", "Processus normal"), 1: translate("ExifPanel", "Processus personnalisé")}
_FLASH_DECODE = {
    0x00: translate("ExifPanel", "Non déclenché"),
    0x01: translate("ExifPanel", "Déclenché"),
    0x05: translate("ExifPanel", "Déclenché, retour non détecté"),
    0x07: translate("ExifPanel", "Déclenché, retour détecté"),
    0x08: translate("ExifPanel", "On, non déclenché"),
    0x09: translate("ExifPanel", "On, déclenché"),
    0x0D: translate("ExifPanel", "On, retour non détecté"),
    0x0F: translate("ExifPanel", "On, retour détecté"),
    0x10: translate("ExifPanel", "Off, non déclenché"),
    0x18: translate("ExifPanel", "Off, retour non détecté"),
    0x19: translate("ExifPanel", "Off, déclenché"),
    0x1D: translate("ExifPanel", "Off, retour non détecté"),
    0x1F: translate("ExifPanel", "Off, retour détecté"),
    0x20: translate("ExifPanel", "Auto, non déclenché"),
    0x24: translate("ExifPanel", "Auto, non déclenché, réduction yeux rouges"),
    0x25: translate("ExifPanel", "Auto, déclenché"),
    0x27: translate("ExifPanel", "Auto, retour détecté"),
    0x29: translate("ExifPanel", "Auto, déclenché, yeux rouges"),
    0x2F: translate("ExifPanel", "Auto, retour détecté, yeux rouges"),
    0x30: translate("ExifPanel", "Flash absent"),
    0x41: translate("ExifPanel", "Déclenché, yeux rouges"),
    0x45: translate("ExifPanel", "Déclenché, retour non détecté, yeux rouges"),
    0x47: translate("ExifPanel", "Déclenché, retour détecté, yeux rouges"),
    0x49: translate("ExifPanel", "On, déclenché, yeux rouges"),
    0x4D: translate("ExifPanel", "On, retour non détecté, yeux rouges"),
    0x4F: translate("ExifPanel", "On, retour détecté, yeux rouges"),
    0x59: translate("ExifPanel", "Auto, déclenché, yeux rouges"),
    0x5D: translate("ExifPanel", "Auto, retour non détecté, yeux rouges"),
    0x5F: translate("ExifPanel", "Auto, retour détecté, yeux rouges"),
}
_COMPRESSIONS = {
    1: translate("ExifPanel", "Non compressé"), 2: translate("ExifPanel", "CCITT 1D"), 3: translate("ExifPanel", "CCITT Groupe 3"), 4: translate("ExifPanel", "CCITT Groupe 4"),
    5: translate("ExifPanel", "LZW"), 6: translate("ExifPanel", "JPEG (old)"), 7: translate("ExifPanel", "JPEG"), 8: translate("ExifPanel", "Deflate/ZIP"),
    32773: translate("ExifPanel", "PackBits (Mac)"), 34713: translate("ExifPanel", "Nikon NEF compressé"),
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
            return (translate("ExifPanel", "Pas de zoom")
                    if z == 0 or z == 1 else f"×{z:.2f}")
        if tag == "SubjectDistance":
            f = float(val)
            return f"{f:.2f} m" if f < 9999 else translate("ExifPanel", "Infini")
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
        return translate("ExifPanel", "(données binaires)")
    if isinstance(val, (list, tuple)):
        return ", ".join(str(v) for v in val)
    return str(val).strip()


def _fmt_size(n: int) -> str:
    if n < 1024:
        return translate("ExifPanel", "{n} o").format(n=n)
    if n < 1024 ** 2:
        return translate("ExifPanel", "{n} Ko").format(n=f"{n / 1024:.1f}")
    if n < 1024 ** 3:
        return translate("ExifPanel", "{n} Mo").format(n=f"{n / 1024 ** 2:.1f}")
    return translate("ExifPanel", "{n} Go").format(n=f"{n / 1024 ** 3:.1f}")


def _set_file_dates(path: str, dt: datetime) -> None:
    """Met à jour mtime + date de création Windows (via l'API Win32 SetFileTime)."""
    ts = dt.timestamp()
    os.utime(path, (ts, ts))
    try:
        import ctypes
        import ctypes.wintypes
        _EPOCH = datetime(1601, 1, 1)
        delta_ns100 = int((dt - _EPOCH).total_seconds() * 10_000_000)
        low  = delta_ns100 & 0xFFFF_FFFF
        high = (delta_ns100 >> 32) & 0xFFFF_FFFF
        ft = ctypes.wintypes.FILETIME(low, high)
        handle = ctypes.windll.kernel32.CreateFileW(
            path, 0x4000_0000, 0, None, 3, 0x80, None,
        )
        if handle and handle != ctypes.c_void_p(-1).value:
            ctypes.windll.kernel32.SetFileTime(
                handle, ctypes.byref(ft), None, ctypes.byref(ft),
            )
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception as e:
        logger.debug("_set_file_dates Win32: %s", e)


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
# Dialogue d'édition EXIF

class ExifEditDialog(QDialog):
    """Dialogue permettant de modifier les métadonnées EXIF d'un fichier image."""

    def __init__(self, photo_path: str, parent=None) -> None:
        super().__init__(parent)
        self._photo_path = photo_path
        self._setup_ui()
        self._load_values()

    def _setup_ui(self) -> None:
        self.setWindowTitle(translate("ExifEditDialog", "Modifier les métadonnées EXIF"))
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        warn = QLabel(translate("ExifEditDialog", "⚠  Ces modifications écrivent directement dans le fichier image."))
        warn.setStyleSheet("color: #f0a800; font-size: 11px; padding: 4px 0;")
        layout.addWidget(warn)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._dt_edit = QDateTimeEdit()
        self._dt_edit.setDisplayFormat("dd/MM/yyyy  HH:mm:ss")
        self._dt_edit.setCalendarPopup(True)
        form.addRow(translate("ExifEditDialog", "Date de prise de vue :"), self._dt_edit)

        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText(translate("ExifEditDialog", "Description de l'image"))
        form.addRow(translate("ExifEditDialog", "Description :"), self._desc_edit)

        self._artist_edit = QLineEdit()
        self._artist_edit.setPlaceholderText(translate("ExifEditDialog", "Photographe / auteur"))
        form.addRow(translate("ExifEditDialog", "Artiste :"), self._artist_edit)

        self._copyright_edit = QLineEdit()
        self._copyright_edit.setPlaceholderText(translate("ExifEditDialog", "© Auteur 2024"))
        form.addRow(translate("ExifEditDialog", "Copyright :"), self._copyright_edit)

        layout.addLayout(form)

        self._cb_file_date = QCheckBox(
            translate("ExifEditDialog", "Mettre à jour aussi la date du fichier (mtime + date de création)")
        )
        self._cb_file_date.setChecked(True)
        layout.addWidget(self._cb_file_date)

        btns = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        btns.button(QDialogButtonBox.Save).setText(translate("ExifEditDialog", "Enregistrer"))
        btns.button(QDialogButtonBox.Cancel).setText(translate("ExifEditDialog", "Annuler"))
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _load_values(self) -> None:
        try:
            from PIL import Image
            with Image.open(self._photo_path) as img:
                exif_data = _read_exif(img)
        except Exception:
            exif_data = {}

        dt_str = exif_data.get("DateTimeOriginal") or exif_data.get("DateTime", "")
        if dt_str:
            try:
                dt = datetime.strptime(str(dt_str), "%Y:%m:%d %H:%M:%S")
                self._dt_edit.setDateTime(
                    QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
                )
            except Exception:
                self._dt_edit.setDateTime(QDateTime.currentDateTime())
        else:
            self._dt_edit.setDateTime(QDateTime.currentDateTime())

        self._desc_edit.setText(str(exif_data.get("ImageDescription") or "").strip())
        self._artist_edit.setText(str(exif_data.get("Artist") or "").strip())
        self._copyright_edit.setText(str(exif_data.get("Copyright") or "").strip())

    def _on_save(self) -> None:
        try:
            self._write_exif()
            self.accept()
        except Exception as e:
            QMessageBox.critical(
                self, translate("ExifEditDialog", "Erreur"),
                translate("ExifEditDialog", "Impossible d'écrire les métadonnées :\n{error}")
                .format(error=e))

    def _write_exif(self) -> None:
        qdt = self._dt_edit.dateTime()
        new_dt = datetime(
            qdt.date().year(), qdt.date().month(), qdt.date().day(),
            qdt.time().hour(), qdt.time().minute(), qdt.time().second(),
        )
        dt_str = new_dt.strftime("%Y:%m:%d %H:%M:%S")

        from PIL import Image
        with Image.open(self._photo_path) as img:
            fmt = (img.format or "JPEG").upper()
            exif = img.getexif()

            exif[0x0132] = dt_str                   # DateTime (IFD principal)
            exif_ifd = exif.get_ifd(0x8769)
            exif_ifd[0x9003] = dt_str               # DateTimeOriginal
            exif_ifd[0x9004] = dt_str               # DateTimeDigitized

            desc = self._desc_edit.text().strip()
            if desc:
                exif[0x010E] = desc
            elif 0x010E in exif:
                del exif[0x010E]

            artist = self._artist_edit.text().strip()
            if artist:
                exif[0x013B] = artist
            elif 0x013B in exif:
                del exif[0x013B]

            copyright_ = self._copyright_edit.text().strip()
            if copyright_:
                exif[0x8298] = copyright_
            elif 0x8298 in exif:
                del exif[0x8298]

            save_kwargs: dict = {"exif": exif.tobytes()}
            if fmt in ("JPEG", "JPG"):
                save_kwargs["quality"] = "keep"

            buf = io.BytesIO()
            img.save(buf, format=fmt, **save_kwargs)

        buf.seek(0)
        with open(self._photo_path, "wb") as f:
            f.write(buf.read())

        if self._cb_file_date.isChecked():
            _set_file_dates(self._photo_path, new_dt)


# ---------------------------------------------------------------------------
# Thread de chargement des données EXIF

class _ExifDataLoader(QThread):
    """Lit les métadonnées EXIF + infos fichier dans un thread secondaire."""

    data_ready = Signal(str, object)   # (photo_path, data_dict | None)

    def __init__(self, photo_path: str, parent=None) -> None:
        super().__init__(parent)
        self._path = photo_path

    def run(self) -> None:
        path = self._path
        try:
            from src.library.exif_reader import VIDEO_EXT
            if Path(path).suffix.lower() in VIDEO_EXT:
                self.data_ready.emit(path, self._load_video(path))
                return
            self.data_ready.emit(path, self._load_image(path))
        except Exception as e:
            logger.debug("_ExifDataLoader: %s — %s", path, e)
            self.data_ready.emit(path, None)

    @staticmethod
    def _load_image(path: str) -> dict:
        from PIL import Image, ImageOps
        with Image.open(path) as img:
            fmt    = img.format or Path(path).suffix.upper().lstrip(".")
            mode   = img.mode
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            w, h   = img.size
            exif   = _read_exif(img)
        stat = os.stat(path)
        return {
            "type": "image",
            "format": fmt, "mode": mode, "width": w, "height": h,
            "size": stat.st_size, "mtime": stat.st_mtime,
            "exif": exif,
        }

    @staticmethod
    def _load_video(path: str) -> dict:
        data: dict = {"type": "video"}
        stat = os.stat(path)
        data["size"]  = stat.st_size
        data["mtime"] = stat.st_mtime
        try:
            import cv2
            from src.library.exif_reader import ascii_safe_path
            with ascii_safe_path(path) as safe_path:
                cap = cv2.VideoCapture(safe_path)
                if cap.isOpened():
                    data["width"]   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    data["height"]  = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    data["fps"]     = cap.get(cv2.CAP_PROP_FPS)
                    fc              = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                    fps             = data["fps"]
                    data["duration"] = fc / fps if fps > 0 else 0.0
                    codec_raw       = int(cap.get(cv2.CAP_PROP_FOURCC))
                    data["codec"]   = "".join(chr((codec_raw >> (8 * i)) & 0xFF) for i in range(4))
                    cap.release()
        except Exception:
            pass
        return data


# ---------------------------------------------------------------------------
# Widget

class ExifPanel(QWidget):
    """Panneau scrollable affichant les métadonnées EXIF d'une photo."""

    photo_saved = Signal(str)  # émis après une sauvegarde EXIF réussie (path)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(260)
        self._current_path: str = ""
        self._is_video: bool = False
        self._loader: _ExifDataLoader | None = None
        self._setup_ui()

    # ------------------------------------------------------------------ UI

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QLabel(translate("ExifPanel", "  Métadonnées EXIF"))
        header.setStyleSheet(
            "background: #2a2a2a; color: #ccc; font-weight: bold;"
            "padding: 8px 0; border-bottom: 1px solid #444;"
        )
        header.setFixedHeight(36)
        root.addWidget(header)

        self._tags_label = QLabel()
        self._tags_label.setWordWrap(True)
        self._tags_label.setStyleSheet(
            "background: #24384a; color: #9cc4e4; padding: 6px 10px;"
            "border-bottom: 1px solid #444; font-size: 12px;"
        )
        self._tags_label.setVisible(False)
        root.addWidget(self._tags_label)

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
        root.addWidget(scroll, stretch=1)

        self._btn_edit = QPushButton(translate("ExifPanel", "✎  Modifier les métadonnées…"))
        self._btn_edit.setFixedHeight(42)
        self._btn_edit.setEnabled(False)
        self._btn_edit.setStyleSheet(
            "QPushButton {"
            "  background: #2d5a8e; color: #fff; font-size: 13px; border: none;"
            "}"
            "QPushButton:hover { background: #3a6eaa; }"
            "QPushButton:pressed { background: #1e4070; }"
            "QPushButton:disabled { background: #252525; color: #555; }"
        )
        self._btn_edit.clicked.connect(self._on_edit_clicked)
        root.addWidget(self._btn_edit)

    # ------------------------------------------------------------------ API publique

    def set_photo(self, photo_path: str) -> None:
        from src.library.exif_reader import VIDEO_EXT
        self._current_path = photo_path
        self._is_video = Path(photo_path).suffix.lower() in VIDEO_EXT
        self._btn_edit.setEnabled(not self._is_video)
        self._clear()
        # Annuler le chargement précédent si encore en cours
        if self._loader and self._loader.isRunning():
            self._loader.data_ready.disconnect()
        self._loader = _ExifDataLoader(photo_path, self)
        self._loader.data_ready.connect(self._on_data_ready)
        self._loader.start()

    @Slot(str, object)
    def _on_data_ready(self, path: str, data: object) -> None:
        if path != self._current_path:
            return  # navigation entre-temps → résultat obsolète
        if data is None:
            self._add_row("", translate(
                "ExifPanel", "Impossible de lire les métadonnées"), error=True)
            return
        if data.get("type") == "video":
            self._populate_from_video_data(data, path)
        else:
            self._populate_from_image_data(data, path)

    def set_tags(self, tags: list) -> None:
        """Affiche les mots-clés (données catalogue, pas EXIF fichier) en tête
        du panneau — alimenté par MainWindow, indépendamment du chargement
        asynchrone des métadonnées EXIF."""
        if tags:
            self._tags_label.setText("🏷  " + ", ".join(tags))
            self._tags_label.setVisible(True)
        else:
            self._tags_label.setVisible(False)

    def clear(self) -> None:
        self._current_path = ""
        self._is_video = False
        self._btn_edit.setEnabled(False)
        self._tags_label.setVisible(False)
        self._clear()

    # ------------------------------------------------------------------ private

    def _on_edit_clicked(self) -> None:
        if not self._current_path or self._is_video:
            return
        dlg = ExifEditDialog(self._current_path, self)
        if dlg.exec() == QDialog.Accepted:
            self._clear()
            # Recharger via le thread après édition
            self._loader = _ExifDataLoader(self._current_path, self)
            self._loader.data_ready.connect(self._on_data_ready)
            self._loader.start()
            self.photo_saved.emit(self._current_path)

    def _clear(self) -> None:
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _populate_from_image_data(self, data: dict, photo_path: str) -> None:
        self._add_section(translate("ExifPanel", "Fichier"))
        self._add_row(translate("ExifPanel", "Nom"),          Path(photo_path).name)
        self._add_row(translate("ExifPanel", "Format"),       data["format"])
        self._add_row(translate("ExifPanel", "Mode couleur"), data["mode"])
        self._add_row(translate("ExifPanel", "Dimensions"),
                      f"{data['width']} × {data['height']} px")
        self._add_row(translate("ExifPanel", "Taille"),       _fmt_size(data["size"]))
        self._add_row(translate("ExifPanel", "Modifié"),
                      datetime.fromtimestamp(data["mtime"]).strftime("%d/%m/%Y  %H:%M"))

        exif = data["exif"]
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

        if "GPSInfo" in exif:
            try:
                self._populate_gps(exif["GPSInfo"])
            except Exception:
                pass

        extra = sorted(
            (tag, _fmt_value(tag, val))
            for tag, val in exif.items()
            if tag not in _SKIP_IN_EXTRA
            and not isinstance(val, bytes)
            and _fmt_value(tag, val)
        )
        if extra:
            self._add_section(translate("ExifPanel", "Autres"))
            for tag, val in extra:
                self._add_row(tag, val)

    def _populate_from_video_data(self, data: dict, video_path: str) -> None:
        self._add_section(translate("ExifPanel", "Fichier"))
        self._add_row(translate("ExifPanel", "Nom"),
                      Path(video_path).name)
        self._add_row(translate("ExifPanel", "Format"),
                      Path(video_path).suffix.upper().lstrip("."))
        self._add_row(translate("ExifPanel", "Taille"), _fmt_size(data["size"]))
        self._add_row(translate("ExifPanel", "Modifié"),
                      datetime.fromtimestamp(data["mtime"]).strftime("%d/%m/%Y  %H:%M"))

        if data.get("width") and data.get("height"):
            self._add_section(translate("ExifPanel", "Vidéo"))
            self._add_row(translate("ExifPanel", "Résolution"),
                          f"{data['width']} × {data['height']} px")
            fps = data.get("fps", 0)
            if fps:
                self._add_row(translate("ExifPanel", "Images/s"), f"{fps:.3f}")
            dur = data.get("duration", 0)
            if dur:
                hh, rem = divmod(int(dur), 3600)
                mm, ss  = divmod(rem, 60)
                self._add_row(translate("ExifPanel", "Durée"),
                              f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm}:{ss:02d}")
            codec = data.get("codec", "").strip()
            if codec:
                self._add_row(translate("ExifPanel", "Codec"), codec)

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
        self._add_row(translate("ExifPanel", "Latitude"),
                      f"{abs(lat):.6f}°  {'N' if lat >= 0 else 'S'}")
        self._add_row(translate("ExifPanel", "Longitude"),
                      f"{abs(lon):.6f}°  {'E' if lon >= 0 else 'O'}")

        # Altitude
        alt = gps_tags.get("GPSAltitude")
        if alt is not None:
            try:
                alt_m = float(alt)
                alt_ref = gps_tags.get("GPSAltitudeRef", 0)
                if alt_ref == 1:
                    alt_m = -alt_m
                self._add_row(translate("ExifPanel", "Altitude"), f"{alt_m:.1f} m")
            except Exception:
                pass

        # Vitesse GPS
        speed = gps_tags.get("GPSSpeed")
        if speed is not None:
            try:
                spd = float(speed)
                ref  = gps_tags.get("GPSSpeedRef", "K")
                unit = {"K": translate("ExifPanel", "km/h"),
                        "M": translate("ExifPanel", "mph"),
                        "N": translate("ExifPanel", "nœuds")}.get(ref, ref)
                self._add_row(translate("ExifPanel", "Vitesse GPS"), f"{spd:.1f} {unit}")
            except Exception:
                pass

        # Direction de l'image
        direction = gps_tags.get("GPSImgDirection")
        if direction is not None:
            try:
                deg = float(direction)
                ref = gps_tags.get("GPSImgDirectionRef", "T")
                label = (translate("ExifPanel", "Direction (magnétique)") if ref == "M"
                         else translate("ExifPanel", "Direction (vraie)"))
                self._add_row(label, f"{deg:.1f}°")
            except Exception:
                pass

        # Date/heure GPS
        gps_date = gps_tags.get("GPSDateStamp")
        gps_time = gps_tags.get("GPSTimeStamp")
        if gps_date and gps_time:
            try:
                h, m, s = (float(x) for x in gps_time)
                self._add_row(translate("ExifPanel", "Date/heure GPS"),
                              f"{gps_date}  {int(h):02d}:{int(m):02d}:{s:04.1f} UTC")
            except Exception:
                pass

        # DOP (précision)
        hdop = gps_tags.get("GPSDOP") or gps_tags.get("GPSHPositioningError")
        if hdop is not None:
            try:
                self._add_row(translate("ExifPanel", "Précision GPS"),
                              f"±{float(hdop):.1f} m")
            except Exception:
                pass

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
