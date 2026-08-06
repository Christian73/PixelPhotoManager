# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Géométrie pure (sans Qt) pour le calque d'annotations.

Deux briques réutilisées par ``src/ui/annotation_renderer.py`` :
- conversion d'une polyligne de points de passage en segments de Bézier
  cubique approximant une spline Catmull-Rom centripète (outil "courbe") ;
- distance point-segment, utilisée pour le hit-test de sélection.
"""
import math

_EPS = 1e-6


def distance_point_to_segment(px: float, py: float, x1: float, y1: float,
                               x2: float, y2: float) -> float:
    """Distance du point (px,py) au segment [(x1,y1),(x2,y2)]."""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq < _EPS:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    proj_x, proj_y = x1 + t * dx, y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def catmull_rom_to_bezier_segments(points, alpha: float = 0.5):
    """Convertit une liste de points de passage en segments de Bézier cubique
    approximant une spline Catmull-Rom **centripète** (alpha=0.5) passant
    exactement par ``points``. Le centripète est préféré à l'uniforme car les
    points sont espacés irrégulièrement (clics utilisateur) — l'uniforme
    produit des boucles/rebroussements dans ce cas, pas le centripète.

    Retourne une liste de tuples ``(p0, cp1, cp2, p3)`` (points de départ,
    contrôle 1, contrôle 2, arrivée), un par segment entre deux points
    consécutifs de ``points``.
    """
    pts = [tuple(p) for p in points]
    if len(pts) < 2:
        return []
    if len(pts) == 2:
        p0, p1 = pts
        return [(p0, p0, p1, p1)]

    # Duplique les extrémités pour disposer d'un point "avant" et "après"
    # à chaque segment interne (nécessaire à la formule Catmull-Rom).
    padded = [pts[0]] + pts + [pts[-1]]
    return [
        _segment_to_bezier(padded[i - 1], padded[i], padded[i + 1], padded[i + 2], alpha)
        for i in range(1, len(padded) - 2)
    ]


def _vec(a, b):
    return (b[0] - a[0], b[1] - a[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _scaled(v, s):
    return (v[0] * s, v[1] * s)


def _knot_delta(pi, pj, alpha: float) -> float:
    return max(math.hypot(pj[0] - pi[0], pj[1] - pi[1]), _EPS) ** alpha


def _segment_to_bezier(p0, p1, p2, p3, alpha: float):
    t0 = 0.0
    t1 = t0 + _knot_delta(p0, p1, alpha)
    t2 = t1 + _knot_delta(p1, p2, alpha)
    t3 = t2 + _knot_delta(p2, p3, alpha)

    d10 = max(t1 - t0, _EPS)
    d20 = max(t2 - t0, _EPS)
    d21 = max(t2 - t1, _EPS)
    d31 = max(t3 - t1, _EPS)
    d32 = max(t3 - t2, _EPS)

    m1 = _scaled(
        _add(_add(_scaled(_vec(p0, p1), 1.0 / d10),
                  _scaled(_vec(p0, p2), -1.0 / d20)),
             _scaled(_vec(p1, p2), 1.0 / d21)),
        d21,
    )
    m2 = _scaled(
        _add(_add(_scaled(_vec(p1, p2), 1.0 / d21),
                  _scaled(_vec(p1, p3), -1.0 / d31)),
             _scaled(_vec(p2, p3), 1.0 / d32)),
        d21,
    )

    cp1 = _add(p1, _scaled(m1, 1.0 / 3.0))
    cp2 = _add(p2, _scaled(m2, -1.0 / 3.0))
    return (p1, cp1, cp2, p2)
