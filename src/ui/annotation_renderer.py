# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Qt rendering of the annotation layer (drawing + text).

A single rendering path (``render_annotations``) serves both the live preview
in the canvas (``src/ui/photo_viewer.py``) and the export
(``composite_annotations_pil``, called from ``src/ui/main_window.py``) — this
avoids reimplementing the curves and the font resolution a second time on the
PIL side.
"""
import math

from PIL import Image
from PIL.ImageQt import ImageQt, fromqimage
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene

from src.processing.annotation_geometry import (
    catmull_rom_to_bezier_segments,
    distance_point_to_segment,
)

_MIN_STROKE_PX = 1.0
_DEFAULT_STROKE_WIDTH = 0.004   # fraction of min(width, height)
_DEFAULT_FONT_SIZE = 0.04       # fraction of min(width, height)


def _scale(target_w: float, target_h: float) -> float:
    return min(target_w, target_h)


def render_annotations(painter: QPainter, annotations: list, target_w: float, target_h: float,
                        background=None) -> None:
    """Paints ``annotations`` (normalised 0-1 coordinates) onto ``painter``,
    at the scale of ``target_w``x``target_h`` — the screen canvas or a
    full-size QImage.

    ``background`` (an optional ``QPixmap``/``QImage``) serves as the pixel source for the
    blur of the rect/ellipse shapes (a blur of the photo under the surface, not of the
    drawing element) — ``None`` disables the blur (no source to sample)."""
    if not annotations:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    for ann in annotations:
        painter.save()
        angle = float(ann.get("angle", 0.0) or 0.0)
        if angle:
            center = annotation_screen_bounds(ann, target_w, target_h).center()
            painter.translate(center)
            painter.rotate(angle)
            painter.translate(-center)
        if ann.get("type") == "text":
            _render_text(painter, ann, target_w, target_h)
        elif ann.get("type") in ("rect", "ellipse"):
            _render_shape(painter, ann, target_w, target_h, background)
        else:
            _render_stroke(painter, ann, target_w, target_h)
        painter.restore()
    painter.restore()


def _render_stroke(painter: QPainter, ann: dict, target_w: float, target_h: float) -> None:
    pts = ann.get("points") or []
    if len(pts) < 2:
        return
    scale = _scale(target_w, target_h)
    width_px = max(_MIN_STROKE_PX, float(ann.get("width", _DEFAULT_STROKE_WIDTH)) * scale)
    color = QColor(ann.get("color", "#ffff0000"))
    painter.setPen(QPen(color, width_px, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)

    screen_pts = [QPointF(x * target_w, y * target_h) for x, y in pts]
    path = QPainterPath()
    path.moveTo(screen_pts[0])
    if ann.get("type") == "curve" and len(screen_pts) >= 2:
        segments = catmull_rom_to_bezier_segments([(p.x(), p.y()) for p in screen_pts])
        for _p0, cp1, cp2, p3 in segments:
            path.cubicTo(QPointF(*cp1), QPointF(*cp2), QPointF(*p3))
    else:  # pen, line
        for p in screen_pts[1:]:
            path.lineTo(p)
    painter.drawPath(path)


def _shape_local_rect(ann: dict, target_w: float, target_h: float) -> QRectF:
    """Bounding rectangle (screen coordinates, unrotated) of a rect/ellipse — ``points``
    stores the two opposite normalised corners, the same convention as the "line" type."""
    pts = ann.get("points") or []
    if len(pts) < 2:
        return QRectF()
    x0, y0 = pts[0][0] * target_w, pts[0][1] * target_h
    x1, y1 = pts[1][0] * target_w, pts[1][1] * target_h
    return QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))


def _render_shape(painter: QPainter, ann: dict, target_w: float, target_h: float, background=None) -> None:
    """Rect/ellipse: the surface (the inside) is governed by ``opacity``/``blur``, the outline
    by ``color``/``width`` — the two are independent. Painting order: (1) the blurred photo
    under the surface if ``blur`` > 0, (2) a solid fill (alpha = ``opacity``, never composed
    with ``painter.setOpacity`` so that a 100% opacity hides the photo behind it completely),
    (3) the outline always drawn at full opacity, on top, independently of opacity/blur."""
    rect = _shape_local_rect(ann, target_w, target_h)
    if rect.isEmpty():
        return
    scale = _scale(target_w, target_h)
    raw_width = float(ann.get("width", _DEFAULT_STROKE_WIDTH))
    stroke_w = max(_MIN_STROKE_PX, raw_width * scale) if raw_width > 0 else 0.0
    stroke_color = QColor(ann.get("color", "#ffff0000"))
    fill_color = QColor(ann.get("fill_color", "#ffff0000"))
    opacity = max(0.0, min(1.0, float(ann.get("opacity", 1.0))))
    blur_px = max(0.0, float(ann.get("blur", 0.0))) * scale
    is_ellipse = ann.get("type") == "ellipse"

    painter.save()
    painter.setOpacity(1.0)

    if blur_px >= 0.5 and background is not None:
        _draw_blurred_background(painter, rect, blur_px, is_ellipse, background, target_w, target_h)

    if opacity > 0.0:
        fill_color.setAlpha(round(255 * opacity))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(fill_color))
        if is_ellipse:
            painter.drawEllipse(rect)
        else:
            painter.drawRect(rect)

    if stroke_w > 0:
        painter.setPen(QPen(stroke_color, stroke_w))
        painter.setBrush(Qt.NoBrush)
        if is_ellipse:
            painter.drawEllipse(rect)
        else:
            painter.drawRect(rect)

    painter.restore()


def _draw_blurred_background(painter: QPainter, rect: QRectF, blur_px: float, is_ellipse: bool,
                              background, target_w: float, target_h: float) -> None:
    """Paints, within the footprint of ``rect`` (clipped to a rectangle or an ellipse), a blurred
    version of ``background`` (the displayed photo — the ``QPixmap`` of the canvas or the export
    ``QImage``) — not of the shape itself. ``QGraphicsBlurEffect`` only applies to scene items,
    hence going through an off-screen ``QGraphicsPixmapItem``, as for the former blur (reused here)."""
    bg_img = background.toImage() if isinstance(background, QPixmap) else background
    if bg_img is None or bg_img.isNull():
        return
    bg_w, bg_h = bg_img.width(), bg_img.height()
    if bg_w <= 0 or bg_h <= 0 or target_w <= 0 or target_h <= 0:
        return
    sx, sy = bg_w / target_w, bg_h / target_h

    pad = blur_px * 2.0
    clip_rect = rect.adjusted(-pad, -pad, pad, pad).intersected(QRectF(0, 0, target_w, target_h))
    if clip_rect.isEmpty():
        return

    src_rect = QRectF(clip_rect.x() * sx, clip_rect.y() * sy,
                       clip_rect.width() * sx, clip_rect.height() * sy).toAlignedRect()
    src_rect = src_rect.intersected(bg_img.rect())
    if src_rect.isEmpty():
        return

    w = max(1, int(math.ceil(clip_rect.width())))
    h = max(1, int(math.ceil(clip_rect.height())))
    cropped = bg_img.copy(src_rect)
    scaled = cropped.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(scaled))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(blur_px)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    out = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    op = QPainter(out)
    op.setRenderHint(QPainter.Antialiasing, True)
    scene.render(op, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
    op.end()

    painter.save()
    clip_path = QPainterPath()
    if is_ellipse:
        clip_path.addEllipse(rect)
    else:
        clip_path.addRect(rect)
    painter.setClipPath(clip_path)
    painter.drawImage(clip_rect.topLeft(), out)
    painter.restore()


def _font_for(ann: dict, target_w: float, target_h: float) -> QFont:
    font = QFont(ann.get("font_family") or "Arial")
    size_px = max(4, round(float(ann.get("font_size", _DEFAULT_FONT_SIZE)) * _scale(target_w, target_h)))
    font.setPixelSize(size_px)
    font.setBold(bool(ann.get("bold", False)))
    font.setItalic(bool(ann.get("italic", False)))
    return font


def _text_rect(ann: dict, target_w: float, target_h: float) -> QRectF:
    font = _font_for(ann, target_w, target_h)
    fm = QFontMetricsF(font)
    pos = ann.get("pos") or [0.0, 0.0]
    x, y = pos[0] * target_w, pos[1] * target_h
    text = ann.get("text", "")
    bound = QRectF(x, y, max(target_w - x, 10.0), max(target_h - y, 10.0))
    return fm.boundingRect(bound, int(Qt.TextWordWrap), text)


def _render_text(painter: QPainter, ann: dict, target_w: float, target_h: float) -> None:
    text = ann.get("text", "")
    if not text:
        return
    font = _font_for(ann, target_w, target_h)
    painter.setFont(font)
    painter.setPen(QColor(ann.get("color", "#ffffffff")))
    painter.drawText(_text_rect(ann, target_w, target_h), int(Qt.TextWordWrap), text)


def annotation_screen_bounds(ann: dict, target_w: float, target_h: float) -> QRectF:
    """Bounding rectangle (screen coordinates) of an annotation — selection highlight."""
    if ann.get("type") == "text":
        return _text_rect(ann, target_w, target_h)
    pts = ann.get("points") or []
    if not pts:
        return QRectF()
    xs = [p[0] * target_w for p in pts]
    ys = [p[1] * target_h for p in pts]
    pad = max(_MIN_STROKE_PX, float(ann.get("width", _DEFAULT_STROKE_WIDTH)) * _scale(target_w, target_h)) / 2
    return QRectF(min(xs) - pad, min(ys) - pad,
                  max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)


def _distance_to_shape(ann: dict, x: float, y: float, target_w: float, target_h: float):
    rect = _shape_local_rect(ann, target_w, target_h)
    if rect.isEmpty():
        return None
    filled = float(ann.get("opacity", 1.0)) > 0.0 or float(ann.get("blur", 0.0)) > 0.0
    if ann.get("type") == "ellipse":
        cx, cy = rect.center().x(), rect.center().y()
        rx, ry = rect.width() / 2, rect.height() / 2
        if rx <= 0 or ry <= 0:
            return None
        nx, ny = (x - cx) / rx, (y - cy) / ry
        if filled and (nx * nx + ny * ny) <= 1.0:
            return 0.0
        d = math.hypot(nx, ny)
        if d == 0:
            return min(rx, ry)
        border_x, border_y = cx + nx / d * rx, cy + ny / d * ry
        return math.hypot(x - border_x, y - border_y)
    if filled and rect.contains(QPointF(x, y)):
        return 0.0
    dx = max(rect.left() - x, 0.0, x - rect.right())
    dy = max(rect.top() - y, 0.0, y - rect.bottom())
    if dx == 0 and dy == 0:
        # inside an unfilled rectangle: distance to the nearest edge
        return min(x - rect.left(), rect.right() - x, y - rect.top(), rect.bottom() - y)
    return math.hypot(dx, dy)


def _distance_to_annotation(ann: dict, x: float, y: float, target_w: float, target_h: float):
    angle = float(ann.get("angle", 0.0) or 0.0)
    if angle:
        # Brings the tested point back into the local (unrotated) frame of the
        # annotation, in which points/pos are stored — the same centre as
        # render_annotations.
        center = annotation_screen_bounds(ann, target_w, target_h).center()
        rad = math.radians(-angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        dx, dy = x - center.x(), y - center.y()
        x = center.x() + dx * cos_a - dy * sin_a
        y = center.y() + dx * sin_a + dy * cos_a
    if ann.get("type") == "text":
        rect = _text_rect(ann, target_w, target_h)
        if rect.contains(QPointF(x, y)):
            return 0.0
        dx = max(rect.left() - x, 0.0, x - rect.right())
        dy = max(rect.top() - y, 0.0, y - rect.bottom())
        return math.hypot(dx, dy)
    if ann.get("type") in ("rect", "ellipse"):
        return _distance_to_shape(ann, x, y, target_w, target_h)
    pts = ann.get("points") or []
    if not pts:
        return None
    screen_pts = [(p[0] * target_w, p[1] * target_h) for p in pts]
    if len(screen_pts) == 1:
        return math.hypot(x - screen_pts[0][0], y - screen_pts[0][1])
    return min(
        distance_point_to_segment(x, y, x1, y1, x2, y2)
        for (x1, y1), (x2, y2) in zip(screen_pts, screen_pts[1:])
    )


def hit_test_annotations(annotations: list, x: float, y: float, target_w: float, target_h: float,
                          tol_px: float = 8.0):
    """Returns the id of the annotation nearest to (x,y) within the ``tol_px`` tolerance, otherwise None."""
    best_id = None
    best_dist = tol_px
    for ann in annotations:
        d = _distance_to_annotation(ann, x, y, target_w, target_h)
        if d is not None and d < best_dist:
            best_dist = d
            best_id = ann.get("id")
    return best_id


def composite_annotations_pil(image: Image.Image, annotations: list) -> Image.Image:
    """Engraves ``annotations`` into ``image`` (PIL) — used at export time.

    Reuses ``render_annotations`` (QPainter) rather than a second PIL/ImageDraw
    implementation, for a rendering identical to the preview and to avoid the
    fragile resolution of fonts into .ttf paths under Pillow.
    """
    if not annotations:
        return image
    original_mode = image.mode if image.mode in ("RGB", "RGBA") else "RGB"
    # A copy constructor is mandatory: ImageQt(image) alone keeps a raw
    # pointer to the PIL buffer, which may be freed before QPainter uses it.
    qimg = QImage(ImageQt(image.convert("RGBA")))
    painter = QPainter(qimg)
    render_annotations(painter, annotations, qimg.width(), qimg.height(), background=qimg.copy())
    painter.end()
    return fromqimage(qimg).convert(original_mode)
