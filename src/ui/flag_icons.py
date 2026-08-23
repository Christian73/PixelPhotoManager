# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Drapeaux des langues d'interface, dessinés par code.

Aucune ressource image embarquée (même principe que `edit_icons.py`) et
surtout **aucun emoji** : les paires d'indicateurs régionaux (U+1F1EB U+1F1F7…)
ne sont portées par aucune police livrée avec Windows — Segoe UI Emoji n'a pas
les drapeaux. Elles s'affichent donc en deux lettres encadrées (« FR », « DE »),
ce qui est exactement ce qu'un sélecteur de langue ne doit pas être : un texte
à lire pour un utilisateur qui, par définition, ne lit pas la langue affichée.

Les trois drapeaux sont rendus au même format 3:2, y compris l'Union Jack
(1:2 dans la réalité) : dans un menu, trois vignettes de tailles différentes
sautent aux yeux bien plus que la proportion inexacte de l'une d'elles.

Le tracé se fait en supersampling (`_SS`) puis est réduit une fois : les
diagonales de l'Union Jack et les liserés font moins d'un pixel à la taille
finale, l'antialiasing de Qt seul les rendrait baveuses.
"""

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

#: Taille logique par défaut d'une vignette de drapeau (format 3:2).
FLAG_WIDTH = 36
FLAG_HEIGHT = 24

#: Facteur de suréchantillonnage du tracé.
_SS = 4

#: Contour : un drapeau à dominante blanche (France) disparaîtrait sinon sur
#: un fond clair, et le noir de l'Allemagne se fond dans la barre noire.
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
    """Union Jack simplifié : sautoirs non contrechargés.

    Le décalage des bandes rouges des diagonales (contrechargement) n'est pas
    reproduit — invisible en dessous de ~64 px, et un sautoir centré reste
    lisible comme Union Jack là où un contrechargement mal échantillonné
    ressemble à une bavure.
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


#: Un code de langue sans tracé retombe sur l'anglais plutôt que sur une
#: vignette vide (cf. `i18n.normalize`, qui rabat déjà les codes inconnus).
_DRAWERS = {"en": _draw_en, "fr": _draw_fr, "de": _draw_de}

_cache: dict[tuple[str, int, int], QPixmap] = {}


def flag_pixmap(code: str, width: int = FLAG_WIDTH, height: int = FLAG_HEIGHT) -> QPixmap:
    """Vignette du drapeau de `code`, mémorisée par (code, largeur, hauteur)."""
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
