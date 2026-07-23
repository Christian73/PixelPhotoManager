# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Chargement et construction des pixmaps de la visionneuse (extraits de
photo_viewer.py) : image de base 1024 px, application des retouches, frames
vidéo. Aussi utilisés par le diaporama (_build_pixmap)."""
import copy
import io
import logging
import math
import os
import uuid

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, Slot, QPoint, QRectF, QPointF, QSize, QFileInfo
from PySide6.QtGui import (
    QDesktopServices, QPixmap, QPainter, QKeyEvent, QWheelEvent,
    QMouseEvent, QPen, QBrush, QColor, QPainterPath, QPolygonF, QIcon, QFont, QTextCursor,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy, QToolButton, QButtonGroup, QMenu, QFileIconProvider, QTextEdit,
)

from src.core.models import PhotoInfo, EditInfo
from src.processing.edit_database import EditDatabase
from src.processing.adjustments import ImageAdjuster
from src.processing.annotation_geometry import catmull_rom_to_bezier_segments
from src.ui.annotation_renderer import (
    render_annotations, hit_test_annotations, annotation_screen_bounds,
)

logger = logging.getLogger(__name__)

# Résolution maximale pour l'affichage à l'écran.
# Les retouches (rotation, recadrage, etc.) s'appliquent sur cette copie réduite.

_PREVIEW_MAX_PX = 1024


def _to_rgb(img):
    """Convertit une image PIL en RGB pour l'enregistrement JPEG.
    RGBA est aplati sur fond blanc ; les autres modes (CMYK, P…) sont convertis directement."""
    if img.mode == "RGBA":
        from PIL import Image as _Image
        bg = _Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _build_pixmap(photo: PhotoInfo, edit: EditInfo | None) -> "tuple[QPixmap, int, int] | None":
    """Retourne (pixmap, orig_w, orig_h) — dimensions de l'image EXIF-corrigée avant
    tout edit, à utiliser pour mapper les bbox de détection faciale."""
    from pathlib import Path as _Path
    from src.library.exif_reader import VIDEO_EXT
    if _Path(photo.path).suffix.lower() in VIDEO_EXT:
        return _build_video_pixmap(photo.path)
    try:
        from PIL import Image, ImageOps
        from src.library.image_loader import open_image
        with open_image(photo.path) as img:
            img = ImageOps.exif_transpose(img)
            orig_w, orig_h = img.size   # dimensions EXIF-corrigées (référence pour les bbox)
            if max(orig_w, orig_h) > _PREVIEW_MAX_PX:
                scale = _PREVIEW_MAX_PX / max(orig_w, orig_h)
                img = img.resize(
                    (round(orig_w * scale), round(orig_h * scale)),
                    Image.LANCZOS,
                )
            if edit and edit.is_modified():
                img = ImageAdjuster.apply_all(img, edit)
            img = _to_rgb(img)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            pixmap = QPixmap()
            pixmap.loadFromData(buf.getvalue())
            return pixmap, orig_w, orig_h
    except Exception as e:
        logger.error(f"Erreur chargement photo {photo.path}: {e}")
        return None


def _build_video_base_image(video_path: str) -> "tuple[bytes, int, int] | None":
    """
    Extrait la première frame de la vidéo sans aucun seek.
    Retourne (jpeg_bytes, orig_w, orig_h).

    Utilise CAP_FFMPEG pour éviter les appels COM/DirectShow qui peuvent marshaler
    du travail sur le thread UI (STA) et provoquer des freezes.
    Ne lit jamais CAP_PROP_FRAME_COUNT ni cap.set(POS_FRAMES) : ces deux appels
    peuvent scanner ou décoder tout le fichier pour les formats sans index.
    """
    try:
        import cv2
        from PIL import Image
        from src.library.exif_reader import ascii_safe_path

        with ascii_safe_path(video_path) as safe_path:
            cap = cv2.VideoCapture(safe_path, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                cap = cv2.VideoCapture(safe_path)
                if not cap.isOpened():
                    return None
            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            ret, frame = cap.read()
            cap.release()
        if not ret or frame is None:
            return None

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        w, h = img.size
        if max(w, h) > _PREVIEW_MAX_PX:
            scale = _PREVIEW_MAX_PX / max(w, h)
            img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), orig_w or w, orig_h or h
    except Exception as e:
        logger.error("Erreur base vidéo %s: %s", video_path, e)
        return None


def _build_base_image(photo: PhotoInfo) -> "tuple[bytes, int, int] | None":
    """
    Charge l'image (ou la première frame vidéo), applique la correction EXIF et réduit
    à _PREVIEW_MAX_PX. Retourne (jpeg_bytes, orig_w, orig_h) SANS retouche.
    Résultat mis en cache dans PhotoViewer._base_lru : évite de relire le fichier
    complet à chaque mouvement de slider (preview de retouche).
    """
    from pathlib import Path as _Path
    from src.library.exif_reader import VIDEO_EXT
    if _Path(photo.path).suffix.lower() in VIDEO_EXT:
        return _build_video_base_image(photo.path)
    try:
        from PIL import Image, ImageOps
        from src.library.image_loader import open_image
        with open_image(photo.path) as img:
            img = ImageOps.exif_transpose(img)
            orig_w, orig_h = img.size
            if max(orig_w, orig_h) > _PREVIEW_MAX_PX:
                scale = _PREVIEW_MAX_PX / max(orig_w, orig_h)
                img = img.resize(
                    (round(orig_w * scale), round(orig_h * scale)), Image.LANCZOS
                )
            img = _to_rgb(img)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            return buf.getvalue(), orig_w, orig_h
    except Exception as e:
        logger.error("Erreur base image %s: %s", photo.path, e)
        return None


def _apply_edit_to_base(base_bytes: bytes, edit: "EditInfo | None") -> "QPixmap | None":
    """
    Applique les retouches sur l'image de base en cache (bytes JPEG 1024px).
    Aucune lecture disque — remplace _build_pixmap pour les previews.
    """
    try:
        if edit is None or not edit.is_modified():
            # Aucune retouche : décodage JPEG direct par Qt, sans l'aller-retour
            # PIL (décodage + ré-encodage) — chemin chaud de la navigation.
            pixmap = QPixmap()
            pixmap.loadFromData(base_bytes)
            if not pixmap.isNull():
                return pixmap
        from PIL import Image
        img = Image.open(io.BytesIO(base_bytes))
        if edit and edit.is_modified():
            img = ImageAdjuster.apply_all(img, edit)
        img = _to_rgb(img)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        return pixmap
    except Exception as e:
        logger.error("Erreur apply edit: %s", e)
        return None


def _build_video_pixmap(video_path: str) -> "tuple[QPixmap, int, int] | None":
    """Extrait une frame de la vidéo pour l'afficher dans la visionneuse."""
    try:
        import cv2
        from PIL import Image
        from src.library.exif_reader import ascii_safe_path

        with ascii_safe_path(video_path) as safe_path:
            cap = cv2.VideoCapture(safe_path)
            if not cap.isOpened():
                return None
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if frame_count > 10:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_count * 0.1))
            ret, frame = cap.read()
            cap.release()
        if not ret:
            return None

        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame)
        orig_w, orig_h = img.size
        if max(orig_w, orig_h) > _PREVIEW_MAX_PX:
            scale = _PREVIEW_MAX_PX / max(orig_w, orig_h)
            img = img.resize((round(orig_w * scale), round(orig_h * scale)), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        return pixmap, orig_w, orig_h
    except Exception as e:
        logger.error("Erreur chargement vidéo %s: %s", video_path, e)
        return None


