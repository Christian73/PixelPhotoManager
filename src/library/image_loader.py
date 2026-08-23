# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Single image decoding point — standard formats, RAW and HEIC/HEIF.

register_heif_opener() is called at MODULE LEVEL (not inside a function):
the ProcessPoolExecutor workers (spawn, cf. faces/detector.py) re-import this
module without ever going through main(), so a lazy registration triggered
somewhere else would never run for them.
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

# CR2/NEF/ARW/DNG/ORF/RW2: the supported RAW formats, decoded through rawpy.
RAW_EXT = {".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2"}

# Extensions PIL cannot write back as such (Image.save fails) — used to
# pick a safe suffix when writing a temporary file from an image decoded
# from one of these formats.
_UNSAVABLE_BY_PIL = RAW_EXT | {".heic", ".heif"}


def is_raw_available() -> bool:
    """True if rawpy is installed (a cached import, expensive to load)."""
    try:
        import rawpy  # noqa: F401
    except ImportError:
        return False
    return True


def safe_temp_suffix(path: str) -> str:
    """Safe suffix for a temporary file meant to receive an image through
    PIL Image.save() — RAW can never be saved back by PIL, and HEIC not
    necessarily depending on the installed pillow-heif version: .jpg in
    those cases."""
    ext = Path(path).suffix.lower()
    if not ext or ext in _UNSAVABLE_BY_PIL:
        return ".jpg"
    return ext


def _open_raw(path: str) -> Image.Image:
    """Decodes a RAW through its embedded JPEG preview (fast, keeps the
    original EXIF written by the camera); falls back on a reduced
    demosaicing (postprocess half_size) when the file has no usable preview."""
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
    """Opens `path` as a PIL image, whatever the format — the project's single
    decoding point (thumbnails, viewer, EXIF, face detection).

    RAW (RAW_EXT): through rawpy (cf. _open_raw). HEIC/HEIF and every other
    format: the standard Image.open (HEIC transparent thanks to
    register_heif_opener(), registered when this module is loaded)."""
    ext = Path(path).suffix.lower()
    if ext in RAW_EXT and is_raw_available():
        return _open_raw(path)
    return Image.open(path)
