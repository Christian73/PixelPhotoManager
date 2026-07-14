# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tentative de réparation de fichiers image corrompus (ex. JPEG avec une
erreur fatale libjpeg "Invalid SOS parameters for sequential JPEG") en
essayant des décodeurs plus tolérants que ceux utilisés pour la détection de
doublons, puis en ré-enregistrant une copie propre à la place de l'original.
"""
import ctypes
import logging
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.library.exif_reader import preserve_file_dates

logger = logging.getLogger(__name__)

_JPEG_QUALITY = 95


def _backup_before_repair(path: str) -> None:
    """Copie l'original dans .tmp_originals (dossier caché) avant réparation,
    même convention que MainWindow._backup_original()."""
    import shutil

    original = Path(path)
    backup_dir = original.parent / ".tmp_originals"
    backup_dir.mkdir(exist_ok=True)

    try:
        ctypes.windll.kernel32.SetFileAttributesW(str(backup_dir), 0x02)
    except Exception:
        pass  # non bloquant sur les systèmes non-Windows

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{original.stem}_{ts}{original.suffix}"
    shutil.copy2(path, backup_dir / backup_name)


def _decode_truncated_pil(path: str):
    """Tente un décodage PIL tolérant aux fichiers tronqués. Retourne une
    image PIL RGB ou None."""
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
    """Tente un décodage via le codec JPEG de Qt (indépendant de libjpeg
    strict). Retourne une image PIL RGB ou None."""
    try:
        from PySide6.QtGui import QImage
        from PIL.ImageQt import fromqimage

        qimg = QImage(path)
        if qimg.isNull():
            return None
        return fromqimage(qimg).convert("RGB")
    except Exception:
        return None


def _try_repair_file(path: str) -> bool:
    """Tente de ré-enregistrer une copie propre de `path`. Retourne True si
    la réparation a réussi (fichier sauvegardé puis remplacé), False sinon."""
    for decode in (_decode_truncated_pil, _decode_qimage):
        img = decode(path)
        if img is None or img.size[0] == 0 or img.size[1] == 0:
            continue
        try:
            orig_stat = os.stat(path)
            _backup_before_repair(path)
            img.save(path, format="JPEG", quality=_JPEG_QUALITY, subsampling=0)
            preserve_file_dates(orig_stat, path)
            return True
        except Exception as exc:
            logger.warning("Échec de ré-enregistrement pour %s : %s", path, exc)
            continue
    return False


class FileRepairThread(QThread):
    """Tente de réparer une liste de fichiers corrompus dans un thread
    séparé (l'UI ne doit jamais bloquer sur ces opérations d'I/O)."""

    progress = Signal(int, int, str)   # (courant, total, chemin en cours)
    finished = Signal(int, list)       # (nb réparés, chemins toujours en échec)

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
            if _try_repair_file(path):
                repaired += 1
            else:
                still_failed.append(path)
        self.finished.emit(repaired, still_failed)
