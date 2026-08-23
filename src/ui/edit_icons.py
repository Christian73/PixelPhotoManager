# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Icons of the editing panel, drawn by code (no embedded image
resource) — extracted from edit_panel.py."""

import math

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QPolygon, QRadialGradient,
)

_ICON_SIZE = 44


def _base_pixmap(size: int) -> tuple[QPixmap, QPainter]:
    px = QPixmap(size, size)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    return px, p


def _icon_brightness(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    c, r = size // 2, size // 4
    p.setBrush(QColor(255, 210, 60))
    p.setPen(QPen(QColor(255, 170, 0), 1))
    p.drawEllipse(c - r, c - r, r * 2, r * 2)
    p.setPen(QPen(QColor(255, 210, 60), 2))
    r1, r2 = r + 3, r + size // 5
    for i in range(8):
        a = math.radians(i * 45)
        p.drawLine(
            int(c + r1 * math.cos(a)), int(c + r1 * math.sin(a)),
            int(c + r2 * math.cos(a)), int(c + r2 * math.sin(a)),
        )
    p.end()
    return px


def _icon_contrast(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    c = size // 2
    r = int(size * 0.38)
    p.setBrush(QColor(30, 30, 30))
    p.setPen(Qt.NoPen)
    p.drawChord(c - r, c - r, r * 2, r * 2, 90 * 16, 180 * 16)
    p.setBrush(QColor(230, 230, 230))
    p.drawChord(c - r, c - r, r * 2, r * 2, 270 * 16, 180 * 16)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(140, 140, 140), 1))
    p.drawEllipse(c - r, c - r, r * 2, r * 2)
    p.end()
    return px


def _icon_saturation(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    c, r = size // 2, size // 3
    for angle, col in [
        (210, QColor(80, 80, 220, 180)),
        (330, QColor(80, 200, 80, 180)),
        (90,  QColor(220, 60, 60, 180)),
    ]:
        rad = math.radians(angle)
        cx = int(c + r * 0.45 * math.cos(rad))
        cy = int(c + r * 0.45 * math.sin(rad))
        p.setBrush(col)
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r // 2, cy - r // 2, r, r)
    p.end()
    return px


def _icon_gamma(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    pad = size // 8
    w, h = size - 2 * pad, size - 2 * pad
    p.setPen(QPen(QColor(80, 80, 80), 1, Qt.DashLine))
    p.drawLine(pad, pad + h, pad + w, pad)
    p.setPen(QPen(QColor(160, 160, 255), 2))
    prev = None
    for i in range(w + 1):
        t = i / w
        y = h - int(h * (t ** 0.42))
        pt = (pad + i, pad + y)
        if prev:
            p.drawLine(prev[0], prev[1], pt[0], pt[1])
        prev = pt
    p.end()
    return px



def _icon_straighten(size: int = _ICON_SIZE) -> QPixmap:
    """A slightly tilted frame + a horizontal horizon line."""
    px, p = _base_pixmap(size)
    c = size // 2
    pad = size // 7
    # Reference horizon line (dotted)
    p.setPen(QPen(QColor(100, 180, 255), 1, Qt.DashLine))
    p.drawLine(pad, c, size - pad, c)
    # Tilted frame representing the image to straighten
    angle = math.radians(12)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    hw, hh = size // 2 - pad - 2, size // 3 - 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    rotated = [
        QPoint(int(c + x * cos_a - y * sin_a), int(c + x * sin_a + y * cos_a))
        for x, y in corners
    ]
    p.setPen(QPen(QColor(210, 210, 210), 2))
    p.setBrush(Qt.NoBrush)
    for i in range(4):
        p.drawLine(rotated[i], rotated[(i + 1) % 4])
    # Small correction arrow (an arc)
    p.setPen(QPen(QColor(100, 200, 100), 2))
    p.drawArc(c - 8, c + pad // 2, 16, 10, 0, 100 * 16)
    p.end()
    return px


def _icon_flip_h(size: int = _ICON_SIZE) -> QPixmap:
    """Two triangles pointing towards the central vertical axis."""
    px, p = _base_pixmap(size)
    c, pad = size // 2, size // 6
    h_half = size // 3
    # Left triangle → points to the right (towards the centre)
    tl = QPolygon([
        QPoint(pad, c - h_half),
        QPoint(c - 3, c),
        QPoint(pad, c + h_half),
    ])
    p.setBrush(QColor(90, 150, 255))
    p.setPen(Qt.NoPen)
    p.drawPolygon(tl)
    # Right triangle → points to the left (towards the centre)
    tr = QPolygon([
        QPoint(size - pad, c - h_half),
        QPoint(c + 3, c),
        QPoint(size - pad, c + h_half),
    ])
    p.drawPolygon(tr)
    # Axe central
    p.setPen(QPen(QColor(255, 255, 255), 2))
    p.drawLine(c, pad, c, size - pad)
    p.end()
    return px


def _icon_crop(size: int = _ICON_SIZE) -> QPixmap:
    """Crop rectangle with corner handles."""
    px, p = _base_pixmap(size)
    pad_out = size // 7
    pad_in  = size // 3
    # Image area (dotted outline)
    p.setPen(QPen(QColor(90, 90, 90), 1, Qt.DashLine))
    p.setBrush(Qt.NoBrush)
    p.drawRect(pad_out, pad_out, size - 2 * pad_out, size - 2 * pad_out)
    # Zone crop (contour blanc)
    p.setPen(QPen(QColor(200, 200, 200), 2))
    p.drawRect(pad_in, pad_in, size - 2 * pad_in, size - 2 * pad_in)
    # Corner handles
    hs = 4
    p.setBrush(QColor(200, 200, 200))
    p.setPen(Qt.NoPen)
    for hx, hy in [(pad_in, pad_in), (size - pad_in, pad_in),
                   (pad_in, size - pad_in), (size - pad_in, size - pad_in)]:
        p.drawRect(hx - hs, hy - hs, hs * 2, hs * 2)
    p.end()
    return px


def _icon_flip_v(size: int = _ICON_SIZE) -> QPixmap:
    """Two triangles pointing towards the central horizontal axis."""
    px, p = _base_pixmap(size)
    c, pad = size // 2, size // 6
    w_half = size // 3
    # Top triangle → points downwards (towards the centre)
    tt = QPolygon([
        QPoint(c - w_half, pad),
        QPoint(c, c - 3),
        QPoint(c + w_half, pad),
    ])
    p.setBrush(QColor(90, 200, 100))
    p.setPen(Qt.NoPen)
    p.drawPolygon(tt)
    # Bottom triangle → points upwards (towards the centre)
    tb = QPolygon([
        QPoint(c - w_half, size - pad),
        QPoint(c, c + 3),
        QPoint(c + w_half, size - pad),
    ])
    p.drawPolygon(tb)
    # Axe central
    p.setPen(QPen(QColor(255, 255, 255), 2))
    p.drawLine(pad, c, size - pad, c)
    p.end()
    return px


def _icon_frame(size: int = _ICON_SIZE) -> QPixmap:
    """A golden frame with a light mount around a bluish photo."""
    px, p = _base_pixmap(size)
    pad = max(1, size // 14)
    outer = size - 2 * pad
    band = max(3, size // 9)

    # Outer moulding (a bevelled golden gradient)
    grad = QLinearGradient(pad, pad, pad + outer, pad + outer)
    grad.setColorAt(0.0, QColor(238, 208, 128))
    grad.setColorAt(0.5, QColor(186, 142, 58))
    grad.setColorAt(1.0, QColor(120, 84, 26))
    p.setBrush(grad)
    p.setPen(QPen(QColor(90, 62, 18), 1))
    p.drawRect(pad, pad, outer, outer)

    # Passe-partout clair
    p.setBrush(QColor(238, 236, 228))
    p.setPen(QPen(QColor(170, 168, 158), 1))
    p.drawRect(pad + band, pad + band, outer - 2 * band, outer - 2 * band)

    # Photo
    inner = pad + band + max(2, size // 16)
    side = size - 2 * inner
    photo = QLinearGradient(inner, inner, inner, inner + side)
    photo.setColorAt(0.0, QColor(66, 120, 180))
    photo.setColorAt(1.0, QColor(150, 176, 120))
    p.setBrush(photo)
    p.setPen(QPen(QColor(40, 60, 90), 1))
    p.drawRect(inner, inner, side, side)

    p.end()
    return px


def _icon_vignette(size: int = _ICON_SIZE) -> QPixmap:
    """A grey square with a dark radial gradient at the corners — a vignette effect."""
    px, p = _base_pixmap(size)
    pad = size // 8
    c = size // 2
    bw = size - 2 * pad
    bh = size - 2 * pad

    # Mid-grey background photo
    p.setBrush(QColor(130, 130, 130))
    p.setPen(Qt.NoPen)
    p.drawRect(pad, pad, bw, bh)

    # Dark vignette through a radial gradient
    grad = QRadialGradient(c, c, int(size * 0.62))
    grad.setColorAt(0.35, QColor(0, 0, 0, 0))
    grad.setColorAt(1.00, QColor(0, 0, 0, 210))
    p.setBrush(grad)
    p.drawRect(pad, pad, bw, bh)

    # Reflet clair au centre
    grad2 = QRadialGradient(c, c, size // 7)
    grad2.setColorAt(0.0, QColor(220, 220, 220, 120))
    grad2.setColorAt(1.0, QColor(220, 220, 220, 0))
    p.setBrush(grad2)
    p.drawRect(pad, pad, bw, bh)

    p.end()
    return px



def _icon_red_eye(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    c, h = size // 2, size // 2
    eye_w = int(size * 0.7)
    eye_h = int(size * 0.32)
    p.setPen(QPen(QColor(200, 200, 200), 1.5))
    p.setBrush(QColor(60, 60, 60))
    path = QPainterPath()
    path.moveTo(c - eye_w // 2, h)
    path.quadTo(c, h - eye_h, c + eye_w // 2, h)
    path.quadTo(c, h + eye_h, c - eye_w // 2, h)
    p.drawPath(path)
    pr = int(size * 0.12)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(220, 40, 40))
    p.drawEllipse(c - pr, h - pr, pr * 2, pr * 2)
    p.end()
    return px


def _icon_ann_pen(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    p.setPen(QPen(QColor(220, 60, 60), 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    path = QPainterPath()
    path.moveTo(size * 0.15, size * 0.75)
    path.cubicTo(size * 0.3, size * 0.35, size * 0.4, size * 0.85, size * 0.55, size * 0.45)
    path.cubicTo(size * 0.65, size * 0.2, size * 0.75, size * 0.6, size * 0.85, size * 0.25)
    p.drawPath(path)
    p.end()
    return px


def _icon_ann_line(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    p.setPen(QPen(QColor(220, 60, 60), 2.5, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(int(size * 0.15), int(size * 0.8), int(size * 0.85), int(size * 0.2))
    p.end()
    return px


def _icon_ann_curve(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    p.setPen(QPen(QColor(220, 60, 60), 2.5, Qt.SolidLine, Qt.RoundCap))
    path = QPainterPath()
    path.moveTo(size * 0.15, size * 0.75)
    path.cubicTo(size * 0.15, size * 0.2, size * 0.85, size * 0.2, size * 0.85, size * 0.75)
    p.drawPath(path)
    p.end()
    return px


def _icon_ann_rect(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    p.setPen(QPen(QColor(220, 60, 60), 2.0, Qt.SolidLine))
    p.setBrush(QColor(220, 60, 60, 70))
    p.drawRect(int(size * 0.15), int(size * 0.25), int(size * 0.7), int(size * 0.5))
    p.end()
    return px


def _icon_ann_ellipse(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    p.setPen(QPen(QColor(220, 60, 60), 2.0, Qt.SolidLine))
    p.setBrush(QColor(220, 60, 60, 70))
    p.drawEllipse(int(size * 0.15), int(size * 0.2), int(size * 0.7), int(size * 0.6))
    p.end()
    return px


def _icon_ann_text(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    font = QFont("Arial", int(size * 0.55))
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor(230, 230, 230))
    p.drawText(px.rect(), int(Qt.AlignCenter), "T")
    p.end()
    return px


def _icon_ann_select(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    # Classic cursor arrow (mouse pointer)
    arrow = QPolygon([
        QPoint(int(size * 0.16), int(size * 0.10)),
        QPoint(int(size * 0.16), int(size * 0.78)),
        QPoint(int(size * 0.34), int(size * 0.61)),
        QPoint(int(size * 0.47), int(size * 0.88)),
        QPoint(int(size * 0.59), int(size * 0.82)),
        QPoint(int(size * 0.46), int(size * 0.55)),
        QPoint(int(size * 0.68), int(size * 0.52)),
    ])
    p.setPen(QPen(QColor(30, 30, 30), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(QColor(225, 225, 225))
    p.drawPolygon(arrow)
    p.end()
    return px


# ------------------------------------------------------------------ dialogue vignette

_TOGGLE_BTN_STYLE = """
    QPushButton {{
        background: #2e2e2e; color: #aaa;
        border: 1px solid #555; border-radius: 4px;
        padding: 4px 8px; font-size: 11px;
    }}
    QPushButton:hover   {{ background: #3a3a3a; color: #ddd; }}
    QPushButton:checked {{ background: #1a3a5a; color: #7ab; border-color: #4a9fd4; font-weight: bold; }}
"""

_ANNOTATION_TOOL_BTN_STYLE = """
    QToolButton {
        background: #2e2e2e; border: 1px solid #555; border-radius: 4px; padding: 3px;
    }
    QToolButton:hover    { background: #3a3a3a; border-color: #777; }
    QToolButton:checked  { background: #1a3a5a; border: 2px solid #4a9fd4; }
"""
