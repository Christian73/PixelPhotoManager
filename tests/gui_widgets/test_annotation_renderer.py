# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/ui/annotation_renderer.py` par rendu réel sur QImage : traits
(stylo/ligne/courbe), formes (rect/ellipse, remplissage/contour/flou), texte,
rotation, bornes d'annotation, hit-testing et composition PIL à l'export."""
import pytest
from PIL import Image
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtCore import Qt

from src.ui import annotation_renderer as ar


def _render(annotations, w=100, h=100, background=None) -> QImage:
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.black)
    painter = QPainter(img)
    ar.render_annotations(painter, annotations, w, h, background=background)
    painter.end()
    return img


def _has_color_near(img: QImage, x: int, y: int, min_red=100) -> bool:
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            px, py = x + dx, y + dy
            if 0 <= px < img.width() and 0 <= py < img.height():
                if img.pixelColor(px, py).red() >= min_red:
                    return True
    return False


def _pen(points, **kw):
    return {"id": kw.pop("id", 1), "type": "pen", "points": points,
            "color": "#ffff0000", **kw}


# ------------------------------------------------------------------ rendu


class TestRenderAnnotations:
    def test_empty_is_noop(self, qtbot):
        img = _render([])
        assert img.pixelColor(50, 50).red() == 0

    def test_pen_stroke_drawn(self, qtbot):
        img = _render([_pen([(0.1, 0.5), (0.9, 0.5)], width=0.05)])
        assert _has_color_near(img, 50, 50)          # milieu du trait
        assert not _has_color_near(img, 50, 10)      # loin du trait

    def test_single_point_stroke_ignored(self, qtbot):
        img = _render([_pen([(0.5, 0.5)])])
        assert not _has_color_near(img, 50, 50)

    def test_line_drawn(self, qtbot):
        ann = {"id": 1, "type": "line", "points": [(0.0, 0.0), (1.0, 1.0)],
               "color": "#ffff0000", "width": 0.05}
        img = _render([ann])
        assert _has_color_near(img, 50, 50)   # diagonale

    def test_curve_drawn(self, qtbot):
        ann = {"id": 1, "type": "curve",
               "points": [(0.1, 0.5), (0.5, 0.2), (0.9, 0.5)],
               "color": "#ffff0000", "width": 0.05}
        img = _render([ann])
        assert _has_color_near(img, 50, 22)

    def test_rect_filled(self, qtbot):
        ann = {"id": 1, "type": "rect", "points": [(0.2, 0.2), (0.8, 0.8)],
               "fill_color": "#ffff0000", "opacity": 1.0, "width": 0.0}
        img = _render([ann])
        assert _has_color_near(img, 50, 50)
        assert not _has_color_near(img, 5, 5)

    def test_rect_outline_only(self, qtbot):
        ann = {"id": 1, "type": "rect", "points": [(0.2, 0.2), (0.8, 0.8)],
               "color": "#ffff0000", "opacity": 0.0, "width": 0.04}
        img = _render([ann])
        assert _has_color_near(img, 20, 50)        # bord gauche
        assert not _has_color_near(img, 50, 50)    # intérieur vide

    def test_ellipse_filled(self, qtbot):
        ann = {"id": 1, "type": "ellipse", "points": [(0.1, 0.1), (0.9, 0.9)],
               "fill_color": "#ffff0000", "opacity": 1.0, "width": 0.0}
        img = _render([ann])
        assert _has_color_near(img, 50, 50)      # centre
        assert not _has_color_near(img, 12, 12)  # coin hors ellipse

    def test_shape_blur_with_background(self, qtbot):
        bg = QPixmap(100, 100)
        bg.fill(Qt.white)
        ann = {"id": 1, "type": "rect", "points": [(0.3, 0.3), (0.7, 0.7)],
               "opacity": 0.0, "blur": 0.1, "width": 0.0}
        img = _render([ann], background=bg)
        # la photo blanche floutée doit apparaître dans la zone de la forme
        assert img.pixelColor(50, 50).red() > 100

    def test_text_drawn(self, qtbot):
        ann = {"id": 1, "type": "text", "pos": [0.05, 0.2], "text": "Bonjour",
               "color": "#ffff0000", "font_size": 0.4}
        img = _render([ann])
        found = any(
            _has_color_near(img, x, y)
            for x in range(5, 95, 5) for y in range(20, 90, 5)
        )
        assert found

    def test_empty_text_ignored(self, qtbot):
        ann = {"id": 1, "type": "text", "pos": [0.1, 0.1], "text": ""}
        img = _render([ann])
        assert img.pixelColor(50, 50).red() == 0

    def test_rotated_annotation(self, qtbot):
        # trait horizontal tourné à 90° → devient vertical
        ann = _pen([(0.1, 0.5), (0.9, 0.5)], width=0.05, angle=90.0)
        img = _render([ann])
        assert _has_color_near(img, 50, 15)   # extrémité désormais verticale
        assert not _has_color_near(img, 10, 50)


# ------------------------------------------------------------------ bornes


class TestAnnotationScreenBounds:
    def test_stroke_bounds_with_padding(self, qtbot):
        ann = _pen([(0.2, 0.3), (0.6, 0.7)], width=0.1)
        r = ar.annotation_screen_bounds(ann, 100, 100)
        assert r.left() < 20 and r.right() > 60
        assert r.top() < 30 and r.bottom() > 70

    def test_text_bounds_nonempty(self, qtbot):
        ann = {"id": 1, "type": "text", "pos": [0.1, 0.1], "text": "abc",
               "font_size": 0.1}
        r = ar.annotation_screen_bounds(ann, 200, 200)
        assert r.width() > 0 and r.height() > 0

    def test_no_points_empty(self, qtbot):
        assert ar.annotation_screen_bounds({"type": "pen"}, 100, 100).isNull()


# ------------------------------------------------------------------ hit-test


class TestHitTest:
    def test_hit_pen_line(self, qtbot):
        anns = [_pen([(0.1, 0.5), (0.9, 0.5)], id=7)]
        assert ar.hit_test_annotations(anns, 50, 50, 100, 100) == 7

    def test_miss(self, qtbot):
        anns = [_pen([(0.1, 0.5), (0.9, 0.5)], id=7)]
        assert ar.hit_test_annotations(anns, 50, 10, 100, 100) is None

    def test_closest_wins(self, qtbot):
        anns = [
            _pen([(0.0, 0.4), (1.0, 0.4)], id=1),
            _pen([(0.0, 0.45), (1.0, 0.45)], id=2),
        ]
        assert ar.hit_test_annotations(anns, 50, 46, 100, 100) == 2

    def test_hit_filled_rect_interior(self, qtbot):
        anns = [{"id": 3, "type": "rect", "points": [(0.2, 0.2), (0.8, 0.8)],
                 "opacity": 1.0}]
        assert ar.hit_test_annotations(anns, 50, 50, 100, 100) == 3

    def test_unfilled_rect_interior_far_from_border_misses(self, qtbot):
        anns = [{"id": 3, "type": "rect", "points": [(0.1, 0.1), (0.9, 0.9)],
                 "opacity": 0.0, "blur": 0.0}]
        # centre à 40 px du bord le plus proche > tolérance 8 px
        assert ar.hit_test_annotations(anns, 50, 50, 100, 100) is None
        # près du bord → touché
        assert ar.hit_test_annotations(anns, 12, 50, 100, 100) == 3

    def test_hit_ellipse(self, qtbot):
        anns = [{"id": 4, "type": "ellipse", "points": [(0.2, 0.2), (0.8, 0.8)],
                 "opacity": 1.0}]
        assert ar.hit_test_annotations(anns, 50, 50, 100, 100) == 4
        assert ar.hit_test_annotations(anns, 2, 2, 100, 100) is None

    def test_hit_text(self, qtbot):
        anns = [{"id": 5, "type": "text", "pos": [0.1, 0.1], "text": "abc",
                 "font_size": 0.2}]
        r = ar.annotation_screen_bounds(anns[0], 200, 200)
        cx, cy = r.center().x(), r.center().y()
        assert ar.hit_test_annotations(anns, cx, cy, 200, 200) == 5

    def test_hit_rotated_rect(self, qtbot):
        # rect étroit horizontal tourné à 90° : le point au-dessus du centre touche
        anns = [{"id": 6, "type": "rect", "points": [(0.2, 0.45), (0.8, 0.55)],
                 "opacity": 1.0, "angle": 90.0}]
        assert ar.hit_test_annotations(anns, 50, 25, 100, 100) == 6
        # le point qui touchait la version non tournée ne touche plus
        assert ar.hit_test_annotations(anns, 25, 50, 100, 100) is None

    def test_single_point_distance(self, qtbot):
        anns = [_pen([(0.5, 0.5)], id=8)]
        assert ar.hit_test_annotations(anns, 51, 51, 100, 100) == 8


# ------------------------------------------------------------------ export PIL


class TestCompositePil:
    def test_empty_returns_same_object(self, qtbot):
        img = Image.new("RGB", (50, 50))
        assert ar.composite_annotations_pil(img, []) is img

    def test_annotations_burned_in(self, qtbot):
        img = Image.new("RGB", (100, 100), (0, 0, 0))
        ann = {"id": 1, "type": "rect", "points": [(0.2, 0.2), (0.8, 0.8)],
               "fill_color": "#ffff0000", "opacity": 1.0, "width": 0.0}
        out = ar.composite_annotations_pil(img, [ann])
        assert out.mode == "RGB"
        r, g, b = out.getpixel((50, 50))
        assert r > 200 and g < 50
        assert out.getpixel((5, 5)) == (0, 0, 0)

    def test_rgba_mode_preserved(self, qtbot):
        img = Image.new("RGBA", (60, 60), (0, 0, 0, 255))
        ann = _pen([(0.1, 0.5), (0.9, 0.5)], width=0.1)
        out = ar.composite_annotations_pil(img, [ann])
        assert out.mode == "RGBA"
