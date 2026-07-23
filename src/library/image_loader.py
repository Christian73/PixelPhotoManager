# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Point de décodage image unique — formats standards, RAW et HEIC/HEIF.

register_heif_opener() est appelé au NIVEAU MODULE (pas dans une fonction) :
les workers ProcessPoolExecutor (spawn, cf. faces/detector.py) ré-importent ce
module sans jamais passer par main(), un enregistrement paresseux ne serait
donc jamais exécuté pour eux si on le déclenchait ailleurs.
"""
import io
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# CR2/NEF/ARW/DNG/ORF/RW2 : formats RAW supportés, décodage via rawpy.
RAW_EXT = {".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2"}

# Étendues que PIL ne peut pas ré-écrire telles quelles (Image.save échoue) —
# utilisé pour choisir un suffixe sûr quand on écrit un fichier temporaire à
# partir d'une image décodée depuis l'un de ces formats.
_UNSAVABLE_BY_PIL = RAW_EXT | {".heic", ".heif"}


def is_raw_available() -> bool:
    """True si rawpy est installé (import cache, coûteux à charger)."""
    try:
        import rawpy  # noqa: F401
    except ImportError:
        return False
    return True


def safe_temp_suffix(path: str) -> str:
    """Suffixe sûr pour un fichier temporaire destiné à recevoir une image via
    PIL Image.save() — RAW n'est jamais ré-savable par PIL, HEIC ne l'est pas
    forcément selon la version de pillow-heif installée : .jpg dans ces cas."""
    ext = Path(path).suffix.lower()
    if not ext or ext in _UNSAVABLE_BY_PIL:
        return ".jpg"
    return ext


def _open_raw(path: str) -> Image.Image:
    """Décode un RAW via son aperçu JPEG embarqué (rapide, conserve l'EXIF
    d'origine écrit par l'appareil) ; repli sur un dématriçage réduit
    (postprocess half_size) si le fichier n'a pas d'aperçu exploitable."""
    import rawpy

    with rawpy.imread(path) as raw:
        thumb = None
        try:
            thumb = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            thumb = None
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            return Image.open(io.BytesIO(thumb.data))
        if thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            return Image.fromarray(thumb.data)
        rgb = raw.postprocess(half_size=True)
        return Image.fromarray(rgb)


def open_image(path: str) -> Image.Image:
    """Ouvre `path` en image PIL, quel que soit le format — point de décodage
    unique du projet (thumbnails, visionneuse, EXIF, détection de visages).

    RAW (RAW_EXT) : via rawpy (cf. _open_raw). HEIC/HEIF et tous les autres
    formats : Image.open standard (HEIC transparent grâce à
    register_heif_opener() enregistré au chargement de ce module)."""
    ext = Path(path).suffix.lower()
    if ext in RAW_EXT and is_raw_available():
        return _open_raw(path)
    return Image.open(path)
