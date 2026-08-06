# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tentative de réparation de fichiers image corrompus (ex. JPEG avec une
erreur fatale libjpeg "Invalid SOS parameters for sequential JPEG") en
essayant des décodeurs plus tolérants que ceux utilisés pour la détection de
doublons, puis en ré-enregistrant une copie propre à la place de l'original.
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


def _decode_cv2_truncated(path: str):
    """Décodeur tolérant basé sur OpenCV/libjpeg-turbo — troisième
    implémentation indépendante de PIL et de Qt. Selon le point exact de la
    troncature, un décodeur peut récupérer plus de lignes qu'un autre : d'où
    l'intérêt de comparer plutôt que de se limiter à PIL+Qt. Retourne une
    image PIL RGB ou None."""
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
    """Beaucoup de JPEG « corrompus » sont en fait intacts à l'exception du
    marqueur de fin (EOI, 0xFFD9) manquant — cas typique d'un transfert de
    fichier interrompu en cours de copie. Si lui ajouter ce marqueur suffit à
    obtenir un décodage STRICT (non tolérant, donc sans ligne de secours
    remplie de gris/noir), la récupération est parfaite : aucun pixel perdu,
    contrairement aux décodeurs tolérants de `_try_repair_file` qui laissent
    la portion non décodée du fichier en données non définies. En cas d'autre
    corruption, le décodage strict échoue proprement (exception) et l'appelant
    retombe sur ces décodeurs tolérants. Retourne une image PIL RGB ou None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    if not data.startswith(b"\xff\xd8") or data.endswith(_JPEG_EOI):
        return None  # pas un JPEG, ou déjà terminé : la troncature finale n'est pas la cause

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
    """Heuristique de score pour comparer des décodages tolérants entre eux :
    un décodeur libjpeg-style s'arrête au point de corruption et remplit le
    reste de l'image avec une couleur unie (gris/noir) plutôt que de la
    tronquer réellement. En partant du bas, la première ligne dont l'écart-
    type des pixels dépasse le seuil marque la fin du contenu réel récupéré.
    Ne sert qu'à départager plusieurs décodages du même fichier, pas à
    valider une image saine (une vraie image peut avoir un bas uniforme)."""
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
    """Sauvegarde d'un décodage retenu comme réparation : sauvegarde de
    l'original puis ré-enregistrement à sa place, dates préservées."""
    try:
        _backup_before_repair(path)
        img.save(path, format="JPEG", quality=_JPEG_QUALITY, subsampling=0)
        preserve_file_dates(orig_stat, path)
        return True
    except Exception as exc:
        logger.warning("Échec de ré-enregistrement pour %s : %s", path, exc)
        return False


def _try_repair_file(path: str) -> bool:
    """Tente de ré-enregistrer une copie propre de `path`. Retourne True si
    la réparation a réussi (fichier sauvegardé puis remplacé), False sinon.

    Deux niveaux : (1) réparation sans perte si la seule anomalie est un
    marqueur de fin JPEG manquant ; (2) à défaut, comparaison de plusieurs
    décodeurs tolérants pour retenir celui qui a récupéré le plus de contenu
    réel (au lieu de s'arrêter au premier qui ne lève pas d'exception, qui
    peut être le moins bon des trois).

    Piège évité : un décodage STRICT après ajout de l'EOI peut malgré tout
    « réussir » sans lever d'exception sur un fichier tronqué en plein
    milieu — libjpeg traite certaines fins de flux entropique prématurées
    comme récupérables et remplit le reste avec du gris uni au lieu
    d'échouer. On ne fait donc confiance au résultat du niveau 1 comme
    « sans perte » que s'il n'a *aucune* ligne de filler détectable
    (`_usable_height` == hauteur totale) ; sinon il rejoint le lot de
    candidats comparés au niveau 2 comme les autres."""
    try:
        orig_stat = os.stat(path)
    except OSError:
        # Le fichier a disparu entre la détection et la tentative de
        # réparation (déplacé/supprimé manuellement, dossier réseau
        # débranché…) : rien à réparer, pas une erreur de décodage.
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
            try:
                ok = _try_repair_file(path)
            except Exception:
                # Un fichier imprévisible ne doit pas interrompre le reste du
                # lot et laisser la barre de progression bloquée indéfiniment.
                logger.exception("Échec inattendu de la réparation de %s", path)
                ok = False
            if ok:
                repaired += 1
            else:
                still_failed.append(path)
        self.finished.emit(repaired, still_failed)
