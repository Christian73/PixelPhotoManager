# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Le cadre décoratif fait partie du pixmap affiché mais PAS de l'image : tout
ce qui est exprimé en coordonnées relatives à la photo (recadrage, yeux rouges,
vignette, cadres de visages, annotations) doit continuer à viser le contenu, pas
le bandeau. Ces tests verrouillent ce décalage dans _Canvas.
"""
import pytest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPixmap

from src.core.models import EditInfo
from src.processing import frames
from src.ui.viewer_canvas import _Canvas


FRAMED = 240          # côté du pixmap encadré utilisé par les tests
EDIT = EditInfo(frame_type="simple", frame_width=0.10)


@pytest.fixture
def canvas(qtbot):
    c = _Canvas()
    qtbot.addWidget(c)
    c.resize(400, 400)
    c.set_pixmap(QPixmap(FRAMED, FRAMED))
    c._zoom = 1.0
    c._offset = QPointF(0, 0)
    return c


def _border() -> int:
    return int(frames.content_box(EDIT, FRAMED, FRAMED)[0])


class TestImgRect:
    def test_without_frame_covers_the_whole_pixmap(self, canvas):
        canvas.set_edit(EditInfo())
        ir = canvas._img_rect()
        assert (ir.x(), ir.y(), ir.width(), ir.height()) == (0, 0, FRAMED, FRAMED)

    def test_frame_is_excluded(self, canvas):
        canvas.set_edit(EDIT)
        b = _border()
        assert b > 0
        ir = canvas._img_rect()
        assert (ir.x(), ir.y()) == (b, b)
        assert (ir.width(), ir.height()) == (FRAMED - 2 * b, FRAMED - 2 * b)

    def test_zoom_and_pan_are_applied_to_the_content(self, canvas):
        canvas.set_edit(EDIT)
        canvas._zoom = 2.0
        canvas._offset = QPointF(30, 10)
        b = _border()
        ir = canvas._img_rect()
        assert (ir.x(), ir.y()) == (30 + 2 * b, 10 + 2 * b)
        assert ir.width() == (FRAMED - 2 * b) * 2

    def test_unknown_frame_type_falls_back_to_no_border(self, canvas):
        canvas.set_edit(EditInfo(frame_type="licorne"))
        assert canvas._img_rect().width() == FRAMED


class TestInteractiveCoordinates:
    def test_red_eye_click_at_content_center_is_relative_to_the_photo(self, canvas, qtbot):
        """Sans le retrait du cadre, un clic au centre visuel de la photo donnait
        des coordonnées décalées vers le bas/la droite."""
        canvas.set_edit(EDIT)
        canvas.enter_red_eye_mode()
        ir = canvas._img_rect()
        center = QPointF(ir.x() + ir.width() / 2, ir.y() + ir.height() / 2)
        with qtbot.waitSignal(canvas.red_eye_point_added, timeout=1000) as blocker:
            canvas.mousePressEvent(QMouseEvent(
                QMouseEvent.MouseButtonPress, center, canvas.mapToGlobal(center),
                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        cx, cy = blocker.args
        assert cx == pytest.approx(0.5, abs=0.01)
        assert cy == pytest.approx(0.5, abs=0.01)

    def test_red_eye_radius_follows_the_content_not_the_band(self, canvas):
        canvas.set_edit(EDIT)
        canvas.enter_red_eye_mode()
        canvas.set_red_eye_radius(0.1)
        b = _border()
        assert canvas._red_eye_screen_radius() == pytest.approx(0.1 * (FRAMED - 2 * b))

    def test_face_rect_is_placed_inside_the_content(self, canvas):
        canvas.set_edit(EDIT)
        canvas.set_orig_size(FRAMED, FRAMED)   # échelle 1:1 avec le contenu affiché

        class _Face:
            bbox_x, bbox_y, bbox_w, bbox_h = 0, 0, 10, 10
            detected_rotation = 0

        b = _border()
        rect = canvas._face_screen_rect(_Face())
        # Un visage en (0,0) de la photo se dessine au coin du CONTENU, pas du pixmap.
        assert (rect.x(), rect.y()) == (b, b)
        assert rect.width() < 10   # contenu réduit par rapport à l'original

    def test_bbox_from_screen_rect_inverts_face_screen_rect(self, canvas):
        canvas.set_edit(EDIT)
        canvas.set_orig_size(FRAMED, FRAMED)

        class _Face:
            bbox_x, bbox_y, bbox_w, bbox_h = 40, 30, 25, 20
            detected_rotation = 0

        rect = canvas._face_screen_rect(_Face())
        bbox = canvas._bbox_from_screen_rect(rect)
        assert bbox == pytest.approx((40, 30, 25, 20), abs=2)

    def test_existing_crop_is_restored_inside_the_content(self, canvas):
        """Un recadrage plein cadre enregistré en base se rouvre exactement sur
        la photo — pas sur le pixmap encadré, qui déborderait sur la moulure."""
        canvas.set_edit(EDIT)
        canvas.enter_crop((0.0, 0.0, 1.0, 1.0))
        b = _border()
        xs = [p.x() for p in canvas._crop_quad]
        ys = [p.y() for p in canvas._crop_quad]
        assert (min(xs), min(ys)) == (b, b)
        assert (max(xs), max(ys)) == (FRAMED - b, FRAMED - b)

    def test_crop_round_trip_is_stable(self, canvas):
        canvas.set_edit(EDIT)
        canvas.enter_crop((0.2, 0.1, 0.5, 0.6))
        rel = canvas._crop_to_rel()
        assert rel is not None
        assert list(rel) == pytest.approx(
            [0.2, 0.1, 0.7, 0.1, 0.7, 0.7, 0.2, 0.7], abs=0.02)
