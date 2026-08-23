# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Repair attempt for corrupted image files (e.g. a JPEG with a fatal
libjpeg error "Invalid SOS parameters for sequential JPEG") by trying
decoders more tolerant than the ones used for duplicate detection, then
saving a clean copy in place of the original.
"""
import ctypes
import io
import logging
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.library.exif_reader import preserve_file_dates

logger = logging.getLogger(__name__)

_JPEG_QUALITY = 95
_JPEG_EOI = b"\xff\xd9"


def _backup_before_repair(path: str) -> None:
    """Copies the original into .tmp_originals (a hidden folder) before the
    repair, the same convention as MainWindow._backup_original()."""
    import shutil

    original = Path(path)
    backup_dir = original.parent / ".tmp_originals"
    backup_dir.mkdir(exist_ok=True)

    try:
        ctypes.windll.kernel32.SetFileAttributesW(str(backup_dir), 0x02)
    except Exception:
        pass  # not blocking on non-Windows systems

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{original.stem}_{ts}{original.suffix}"
    shutil.copy2(path, backup_dir / backup_name)


def _decode_truncated_pil(path: str):
    """Attempts a PIL decoding tolerant of truncated files. Returns a PIL RGB
    image or None."""
    try:
        from PIL import Image, ImageFile

        prev = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(path) as img:
                img.load()
                return img.convert("RGB")
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = prev
    except Exception:
        return None


def _decode_qimage(path: str):
    """Attempts a decoding through Qt's JPEG codec (independent of a strict
    libjpeg). Returns a PIL RGB image or None."""
    try:
        from PySide6.QtGui import QImage
        from PIL.ImageQt import fromqimage

        qimg = QImage(path)
        if qimg.isNull():
            return None
        return fromqimage(qimg).convert("RGB")
    except Exception:
        return None


def _decode_cv2_truncated(path: str):
    """A tolerant decoder based on OpenCV/libjpeg-turbo — a third
    implementation independent of PIL and of Qt. Depending on the exact point
    of the truncation, one decoder may recover more rows than another: hence
    the value of comparing them rather than settling for PIL+Qt. Returns a
    PIL RGB image or None."""
    try:
        import cv2
        import numpy as np
        from PIL import Image

        data = np.fromfile(path, dtype=np.uint8)
        bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if bgr is None or bgr.size == 0:
            return None
        return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    except Exception:
        return None


def _decode_strict_with_eoi_fix(path: str):
    """Many "corrupted" JPEGs are in fact intact apart from a missing end
    marker (EOI, 0xFFD9) — the typical case of a file transfer interrupted
    mid-copy. If adding that marker is enough to obtain a STRICT decoding
    (not tolerant, hence with no fallback row filled with grey/black), the
    recovery is perfect: not a single pixel lost, unlike the tolerant
    decoders of `_try_repair_file`, which leave the undecoded portion of the
    file as undefined data. On any other corruption the strict decoding
    fails cleanly (an exception) and the caller falls back on those tolerant
    decoders. Returns a PIL RGB image or None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    if not data.startswith(b"\xff\xd8") or data.endswith(_JPEG_EOI):
        return None  # not a JPEG, or already complete: a final truncation is not the cause

    try:
        from PIL import Image, ImageFile

        prev = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            with Image.open(io.BytesIO(data + _JPEG_EOI)) as img:
                img.load()
                return img.convert("RGB")
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = prev
    except Exception:
        return None


def _usable_height(img) -> int:
    """A scoring heuristic to compare tolerant decodings with one another: a
    libjpeg-style decoder stops at the point of corruption and fills the rest
    of the image with a plain colour (grey/black) rather than truly
    truncating it. Starting from the bottom, the first row whose pixel
    standard deviation exceeds the threshold marks the end of the real
    content recovered. It only serves to pick between several decodings of
    the same file, never to validate a healthy image (a genuine image may
    well have a uniform bottom)."""
    import numpy as np

    arr = np.asarray(img)
    if arr.ndim == 2:
        arr = arr[:, :, None]
    height = arr.shape[0]
    row_std = arr.reshape(height, -1).astype(np.float32).std(axis=1)
    usable = 0
    for y in range(height - 1, -1, -1):
        if row_std[y] > 1.0:
            usable = y + 1
            break
    return usable


def _save_repaired(path: str, img, orig_stat: os.stat_result) -> bool:
    """Saves a decoding retained as the repair: back up the original, then
    save in its place, dates preserved."""
    try:
        _backup_before_repair(path)
        img.save(path, format="JPEG", quality=_JPEG_QUALITY, subsampling=0)
        preserve_file_dates(orig_stat, path)
        return True
    except Exception as exc:
        logger.warning("Échec de ré-enregistrement pour %s : %s", path, exc)
        return False


def _try_repair_file(path: str) -> bool:
    """Attempts to save a clean copy of `path`. Returns True if the repair
    succeeded (file backed up then replaced), False otherwise.

    Two levels: (1) a lossless repair if the only anomaly is a missing JPEG
    end marker; (2) failing that, a comparison of several tolerant decoders
    to keep the one that recovered the most real content (instead of
    stopping at the first one that does not raise, which may be the worst of
    the three).

    Trap avoided: a STRICT decoding after the EOI is added can nevertheless
    "succeed" without raising on a file truncated right in the middle —
    libjpeg treats some premature ends of the entropy stream as recoverable
    and fills the rest with plain grey instead of failing. The result of
    level 1 is therefore only trusted as "lossless" when it has *no*
    detectable filler row (`_usable_height` == the full height); otherwise it
    joins the pool of candidates compared at level 2 like the others."""
    try:
        orig_stat = os.stat(path)
    except OSError:
        # The file disappeared between the detection and the repair attempt
        # (moved/deleted by hand, network folder unplugged…): nothing to
        # repair, not a decoding error.
        return False

    candidates = []

    lossless = _decode_strict_with_eoi_fix(path)
    if lossless is not None and lossless.size[0] > 0 and lossless.size[1] > 0:
        if _usable_height(lossless) >= lossless.size[1]:
            return _save_repaired(path, lossless, orig_stat)
        candidates.append(lossless)

    for decode in (_decode_truncated_pil, _decode_qimage, _decode_cv2_truncated):
        img = decode(path)
        if img is not None and img.size[0] > 0 and img.size[1] > 0:
            candidates.append(img)

    if not candidates:
        return False
    best_img = max(candidates, key=_usable_height)
    return _save_repaired(path, best_img, orig_stat)


class FileRepairThread(QThread):
    """Attempts to repair a list of corrupted files in a separate thread (the
    UI must never block on those I/O operations)."""

    progress = Signal(int, int, str)   # (current, total, path being processed)
    finished = Signal(int, list)       # (number repaired, paths still failing)

    def __init__(self, paths: list[str], parent=None):
        super().__init__(parent)
        self._paths = paths
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        repaired = 0
        still_failed: list[str] = []
        total = len(self._paths)
        for i, path in enumerate(self._paths):
            if self._cancelled:
                break
            self.progress.emit(i, total, path)
            try:
                ok = _try_repair_file(path)
            except Exception:
                # An unpredictable file must not interrupt the rest of the batch
                # and leave the progress bar stuck indefinitely.
                logger.exception("Échec inattendu de la réparation de %s", path)
                ok = False
            if ok:
                repaired += 1
            else:
                still_failed.append(path)
        self.finished.emit(repaired, still_failed)
