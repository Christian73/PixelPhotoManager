# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Pure geometry (Qt-free) for the annotation layer.

Two building blocks reused by ``src/ui/annotation_renderer.py``:
- conversion of a polyline of waypoints into cubic Bézier segments
  approximating a centripetal Catmull-Rom spline (the "curve" tool);
- point-to-segment distance, used for the selection hit-test.
"""
import math

_EPS = 1e-6


def distance_point_to_segment(px: float, py: float, x1: float, y1: float,
                               x2: float, y2: float) -> float:
    """Distance from the point (px,py) to the segment [(x1,y1),(x2,y2)]."""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq < _EPS:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    proj_x, proj_y = x1 + t * dx, y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def catmull_rom_to_bezier_segments(points, alpha: float = 0.5):
    """Converts a list of waypoints into cubic Bézier segments approximating
    a **centripetal** Catmull-Rom spline (alpha=0.5) passing exactly through
    ``points``. The centripetal variant is preferred over the uniform one
    because the points are irregularly spaced (user clicks) — the uniform one
    produces loops/cusps in that case, the centripetal one does not.

    Returns a list of ``(p0, cp1, cp2, p3)`` tuples (start point, control 1,
    control 2, end point), one per segment between two consecutive points of
    ``points``.
    """
    pts = [tuple(p) for p in points]
    if len(pts) < 2:
        return []
    if len(pts) == 2:
        p0, p1 = pts
        return [(p0, p0, p1, p1)]

    # Duplicate the endpoints so that every internal segment has a point
    # "before" and "after" (required by the Catmull-Rom formula).
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
