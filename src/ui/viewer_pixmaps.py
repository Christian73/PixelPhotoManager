# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Loading and building of the viewer pixmaps (extracted from
photo_viewer.py): the 1024 px base image, applying the edits, video frames.
Also used by the slideshow (_build_pixmap)."""
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

# Maximum resolution for the on-screen display.
# The edits (rotation, crop, etc.) are applied on this reduced copy.

_PREVIEW_MAX_PX = 1024


def _to_rgb(img):
    """Converts a PIL image to RGB for JPEG saving.
    RGBA is flattened on a white background; the other modes (CMYK, P…) are converted directly."""
    if img.mode == "RGBA":
        from PIL import Image as _Image
        bg = _Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _build_pixmap(photo: PhotoInfo, edit: EditInfo | None) -> "tuple[QPixmap, int, int] | None":
    """Returns (pixmap, orig_w, orig_h) — the dimensions of the EXIF-corrected
    image before any edit, to be used to map the face detection bboxes."""
    from pathlib import Path as _Path
    from src.library.exif_reader import VIDEO_EXT
    if _Path(photo.path).suffix.lower() in VIDEO_EXT:
        return _build_video_pixmap(photo.path)
    try:
        from PIL import Image, ImageOps
        from src.library.image_loader import open_image
        with open_image(photo.path) as img:
            img = ImageOps.exif_transpose(img)
            orig_w, orig_h = img.size   # EXIF-corrected dimensions (the reference for the bboxes)
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
    Extracts the first frame of the video without any seek.
    Returns (jpeg_bytes, orig_w, orig_h).

    Uses CAP_FFMPEG to avoid the COM/DirectShow calls, which can marshal work
    onto the UI thread (STA) and cause freezes.
    Never reads CAP_PROP_FRAME_COUNT nor cap.set(POS_FRAMES): both calls can
    scan or decode the whole file for the formats without an index.
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
    Loads the image (or the first video frame), applies the EXIF correction
    and reduces it to _PREVIEW_MAX_PX. Returns (jpeg_bytes, orig_w, orig_h)
    WITHOUT any edit.
    The result is cached in PhotoViewer._base_lru: avoids re-reading the whole
    file at every slider movement (edit preview).
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
    Applies the edits on the cached base image (1024 px JPEG bytes).
    No disk read — replaces _build_pixmap for the previews.
    """
    try:
        if edit is None or not edit.is_modified():
            # No edit: direct JPEG decoding by Qt, without the PIL round trip
            # (decoding + re-encoding) — the hot path of the navigation.
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
    """Extracts a frame of the video to show it in the viewer."""
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


