# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Rendu Qt du calque d'annotations (dessin + texte).

Un seul chemin de rendu (``render_annotations``) sert à la fois l'aperçu live
dans le canvas (``src/ui/photo_viewer.py``) et l'export (``composite_annotations_pil``,
appelé depuis ``src/ui/main_window.py``) — évite de réimplémenter les courbes
et la résolution de polices une seconde fois côté PIL.
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
_DEFAULT_STROKE_WIDTH = 0.004   # fraction de min(largeur, hauteur)
_DEFAULT_FONT_SIZE = 0.04       # fraction de min(largeur, hauteur)


def _scale(target_w: float, target_h: float) -> float:
    return min(target_w, target_h)


def render_annotations(painter: QPainter, annotations: list, target_w: float, target_h: float,
                        background=None) -> None:
    """Peint ``annotations`` (coordonnées normalisées 0-1) sur ``painter``,
    à l'échelle de ``target_w``x``target_h`` — canvas écran ou QImage plein format.

    ``background`` (``QPixmap``/``QImage`` optionnel) sert de source de pixels pour le
    flou des formes rect/ellipse (flou de la photo sous la surface, pas de l'élément
    d'écriture) — ``None`` désactive le flou (aucune source à échantillonner)."""
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
    """Rectangle englobant (coordonnées écran, non tourné) d'un rect/ellipse — ``points``
    stocke les deux coins opposés normalisés, même convention que le type "line"."""
    pts = ann.get("points") or []
    if len(pts) < 2:
        return QRectF()
    x0, y0 = pts[0][0] * target_w, pts[0][1] * target_h
    x1, y1 = pts[1][0] * target_w, pts[1][1] * target_h
    return QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))


def _render_shape(painter: QPainter, ann: dict, target_w: float, target_h: float, background=None) -> None:
    """Rect/ellipse : la surface (intérieur) est régie par ``opacity``/``blur``, le contour
    par ``color``/``width`` — les deux sont indépendants. Ordre de peinture : (1) photo floutée
    sous la surface si ``blur`` > 0, (2) remplissage plein (alpha = ``opacity``, jamais composé
    avec ``painter.setOpacity`` pour qu'une opacité à 100% masque totalement la photo derrière),
    (3) contour toujours tracé à pleine opacité, par-dessus, indépendamment de opacity/blur."""
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
    """Peint, dans l'emprise de ``rect`` (découpée en rectangle ou ellipse), une version floutée
    de ``background`` (photo affichée — ``QPixmap`` du canvas ou ``QImage`` d'export) — pas de
    la forme elle-même. ``QGraphicsBlurEffect`` ne s'applique qu'à des items de scène, d'où le
    passage par un ``QGraphicsPixmapItem`` hors-écran, comme pour l'ancien flou (repris ici)."""
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
    """Rectangle englobant (coordonnées écran) d'une annotation — surbrillance de sélection."""
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
        # à l'intérieur d'un rectangle non rempli : distance au bord le plus proche
        return min(x - rect.left(), rect.right() - x, y - rect.top(), rect.bottom() - y)
    return math.hypot(dx, dy)


def _distance_to_annotation(ann: dict, x: float, y: float, target_w: float, target_h: float):
    angle = float(ann.get("angle", 0.0) or 0.0)
    if angle:
        # Ramène le point testé dans le repère local (non tourné) de l'annotation,
        # dans lequel points/pos sont stockés — même centre que render_annotations.
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
    """Retourne l'id de l'annotation la plus proche de (x,y) dans la tolérance ``tol_px``, sinon None."""
    best_id = None
    best_dist = tol_px
    for ann in annotations:
        d = _distance_to_annotation(ann, x, y, target_w, target_h)
        if d is not None and d < best_dist:
            best_dist = d
            best_id = ann.get("id")
    return best_id


def composite_annotations_pil(image: Image.Image, annotations: list) -> Image.Image:
    """Grave ``annotations`` dans ``image`` (PIL) — utilisé à l'export.

    Réutilise ``render_annotations`` (QPainter) plutôt qu'une seconde
    implémentation PIL/ImageDraw, pour un rendu identique à l'aperçu et
    éviter la résolution fragile des polices en chemins .ttf sous Pillow.
    """
    if not annotations:
        return image
    original_mode = image.mode if image.mode in ("RGB", "RGBA") else "RGB"
    # Constructeur-copie obligatoire : ImageQt(image) seul garde un pointeur
    # brut vers le buffer PIL, qui peut être libéré avant usage par QPainter.
    qimg = QImage(ImageQt(image.convert("RGBA")))
    painter = QPainter(qimg)
    render_annotations(painter, annotations, qimg.width(), qimg.height(), background=qimg.copy())
    painter.end()
    return fromqimage(qimg).convert(original_mode)
