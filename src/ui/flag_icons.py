# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Flags of the interface languages, drawn by code.

No embedded image resource (the same principle as `edit_icons.py`) and above
all **no emoji**: the regional indicator pairs (U+1F1EB U+1F1F7…) are carried
by no font shipped with Windows — Segoe UI Emoji does not have the flags. They
therefore show as two boxed letters ("FR", "DE"), which is exactly what a
language selector must not be: a text to read for a user who, by definition,
does not read the displayed language.

The three flags are rendered in the same 3:2 format, the Union Jack included
(1:2 in reality): in a menu, three thumbnails of different sizes catch the eye
far more than the inexact proportion of one of them.

The drawing is done with supersampling (`_SS`) then downscaled once: the
diagonals of the Union Jack and the thin outlines are less than a pixel wide
at the final size, Qt's antialiasing alone would render them blurry.
"""

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

#: Default logical size of a flag thumbnail (3:2 format).
FLAG_WIDTH = 36
FLAG_HEIGHT = 24

#: Supersampling factor of the drawing.
_SS = 4

#: Outline: a mostly white flag (France) would otherwise disappear on a
#: light background, and the black of Germany blends into the black bar.
_BORDER = QColor(0, 0, 0, 110)


def _draw_fr(p: QPainter, w: float, h: float) -> None:
    p.fillRect(QRectF(0, 0, w / 3, h), QColor("#002654"))
    p.fillRect(QRectF(w / 3, 0, w / 3, h), QColor("#ffffff"))
    p.fillRect(QRectF(2 * w / 3, 0, w - 2 * w / 3, h), QColor("#ce1126"))


def _draw_de(p: QPainter, w: float, h: float) -> None:
    p.fillRect(QRectF(0, 0, w, h / 3), QColor("#000000"))
    p.fillRect(QRectF(0, h / 3, w, h / 3), QColor("#dd0000"))
    p.fillRect(QRectF(0, 2 * h / 3, w, h - 2 * h / 3), QColor("#ffce00"))


def _draw_en(p: QPainter, w: float, h: float) -> None:
    """Simplified Union Jack: saltires without counterchanging.

    The offset of the red bands of the diagonals (counterchanging) is not
    reproduced — invisible below ~64 px, and a centred saltire still reads as
    a Union Jack where a badly sampled counterchange looks like a smudge.
    """
    p.fillRect(QRectF(0, 0, w, h), QColor("#012169"))

    corners = ((QPointF(0, 0), QPointF(w, h)), (QPointF(w, 0), QPointF(0, h)))
    for color, ratio in ((QColor("#ffffff"), 0.30), (QColor("#c8102e"), 0.12)):
        p.setPen(QPen(color, h * ratio, Qt.SolidLine, Qt.FlatCap))
        for a, b in corners:
            p.drawLine(a, b)

    p.setPen(Qt.NoPen)
    for color, ratio in ((QColor("#ffffff"), 0.34), (QColor("#c8102e"), 0.20)):
        band = h * ratio
        p.setBrush(color)
        p.drawRect(QRectF(0, (h - band) / 2, w, band))
        p.drawRect(QRectF((w - band) / 2, 0, band, h))


#: A language code with no drawing falls back to English rather than to an
#: empty thumbnail (cf. `i18n.normalize`, which already folds the unknown codes).
_DRAWERS = {"en": _draw_en, "fr": _draw_fr, "de": _draw_de}

_cache: dict[tuple[str, int, int], QPixmap] = {}


def flag_pixmap(code: str, width: int = FLAG_WIDTH, height: int = FLAG_HEIGHT) -> QPixmap:
    """Thumbnail of the flag of `code`, memoised by (code, width, height)."""
    key = (code, width, height)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    big = QPixmap(width * _SS, height * _SS)
    big.fill(QColor(0, 0, 0, 0))
    p = QPainter(big)
    p.setRenderHint(QPainter.Antialiasing)
    w, h = float(width * _SS), float(height * _SS)
    _DRAWERS.get(code, _draw_en)(p, w, h)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(_BORDER, _SS))
    p.drawRect(QRectF(_SS / 2, _SS / 2, w - _SS, h - _SS))
    p.end()

    px = big.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    _cache[key] = px
    return px


def flag_icon(code: str, width: int = FLAG_WIDTH, height: int = FLAG_HEIGHT) -> QIcon:
    return QIcon(flag_pixmap(code, width, height))
