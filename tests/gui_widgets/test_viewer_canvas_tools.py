# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Interactive tools of the viewer canvas (_Canvas, viewer_canvas.py): zoom,
crop, manual face addition, vignette and annotation layer.

The whole file drives the widget through its REAL event handlers
(mousePressEvent/mouseMoveEvent/mouseReleaseEvent/wheelEvent, synthetic
QMouseEvent) rather than through the private helpers alone: a hit test that is
right but wired to the wrong branch of mousePressEvent is exactly the kind of
defect a direct call to `_apply_drag_corner()` would never see. No OS
automation: everything is pure geometry on a canvas whose zoom and offset are
frozen (cf. the `canvas` fixture), so the assertions are exact pixel values.

Complements test_viewer_canvas_frame.py, which locks down the OTHER half of the
same code: the offset introduced by a decorative frame between the pixmap and
the photo content. Here there is never a frame, so `_img_rect()` covers the
whole pixmap and the relative coordinates read directly."""

import pytest

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import (
    QColor, QContextMenuEvent, QImage, QMouseEvent, QPixmap, QResizeEvent,
    QWheelEvent,
)

from src.core.models import EditInfo
from src.ui.viewer_canvas import _Canvas


SIDE = 400   # side of the pixmap AND of the canvas -> zoom 1, offset 0


@pytest.fixture
def canvas(qtbot):
    """Canvas of 400x400 displaying a 400x400 pixmap at zoom 1, offset 0:
    `_img_rect()` is therefore exactly (0, 0, 400, 400) and a screen coordinate
    reads as a relative coordinate divided by 400."""
    c = _Canvas()
    qtbot.addWidget(c)
    c.resize(SIDE, SIDE)
    pm = QPixmap(SIDE, SIDE)
    pm.fill(QColor(10, 120, 200))
    c.set_pixmap(pm)
    # First rendering, discarded: a widget that has never been shown receives its
    # deferred QResizeEvent at the first render() -- which legitimately cancels
    # the drafts in progress (cf. resizeEvent). Absorbing it here keeps that
    # artefact of the harness out of the painting tests.
    c.render(QImage(1, 1, QImage.Format_ARGB32))
    c._zoom = 1.0
    c._offset = QPointF(0, 0)
    return c


def _render(canvas) -> QImage:
    """Real rendering of the widget (paintEvent) at scale 1, whatever the DPI of
    the machine -- `canvas.grab()` would return a pixmap in DEVICE pixels (500x500
    at 125%), where the assertions would no longer read as screen coordinates."""
    img = QImage(canvas.size(), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    canvas.render(img)
    return img


def _press(canvas, x, y, button=Qt.LeftButton, modifiers=Qt.NoModifier) -> None:
    pos = QPointF(x, y)
    canvas.mousePressEvent(QMouseEvent(
        QEvent.MouseButtonPress, pos, canvas.mapToGlobal(pos),
        button, button, modifiers))


def _move(canvas, x, y, buttons=Qt.LeftButton, modifiers=Qt.NoModifier) -> None:
    pos = QPointF(x, y)
    canvas.mouseMoveEvent(QMouseEvent(
        QEvent.MouseMove, pos, canvas.mapToGlobal(pos),
        Qt.NoButton, buttons, modifiers))


def _release(canvas, x, y, button=Qt.LeftButton, modifiers=Qt.NoModifier) -> None:
    pos = QPointF(x, y)
    canvas.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, pos, canvas.mapToGlobal(pos),
        button, Qt.NoButton, modifiers))


def _double_click(canvas, x, y) -> None:
    pos = QPointF(x, y)
    canvas.mouseDoubleClickEvent(QMouseEvent(
        QEvent.MouseButtonDblClick, pos, canvas.mapToGlobal(pos),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def _wheel(canvas, delta: int) -> None:
    pos = QPointF(SIDE / 2, SIDE / 2)
    canvas.wheelEvent(QWheelEvent(
        pos, canvas.mapToGlobal(pos), QPoint(0, 0), QPoint(0, delta),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))


def _drag(canvas, x0, y0, x1, y1, steps: int = 2, modifiers=Qt.NoModifier) -> None:
    """Complete press -> move(s) -> release sequence."""
    _press(canvas, x0, y0, modifiers=modifiers)
    for i in range(1, steps + 1):
        t = i / steps
        _move(canvas, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, modifiers=modifiers)
    _release(canvas, x1, y1, modifiers=modifiers)


def _bounds(quad) -> tuple:
    xs = [pt.x() for pt in quad]
    ys = [pt.y() for pt in quad]
    return min(xs), min(ys), max(xs), max(ys)


class TestZoom:
    def test_zoom_fit_scales_and_centres(self, canvas, qtbot):
        pm = QPixmap(800, 400)
        with qtbot.waitSignal(canvas.zoom_changed, timeout=1000) as blocker:
            canvas.set_pixmap(pm)
        assert canvas.zoom == pytest.approx(0.5)
        assert blocker.args == [pytest.approx(0.5)]
        # 800x400 at 0.5 -> 400x200 centred vertically in a 400x400 canvas
        assert (canvas._offset.x(), canvas._offset.y()) == (0, 100)

    def test_zoom_100_recentres(self, canvas):
        canvas.set_pixmap(QPixmap(800, 400))
        canvas.zoom_100()
        assert canvas.zoom == 1.0
        assert (canvas._offset.x(), canvas._offset.y()) == (-200, 0)

    def test_set_zoom_is_clamped(self, canvas):
        canvas.set_zoom(99.0)
        assert canvas.zoom == 4.0
        canvas.set_zoom(0.0001)
        assert canvas.zoom == 0.1

    def test_set_zoom_preserves_the_centre_of_the_viewport(self, canvas):
        """The point displayed at the centre of the widget must not move: that is
        what makes a zoom through the toolbar readable."""
        canvas._offset = QPointF(0, 0)
        canvas.set_zoom(2.0)
        # The pixel of the pixmap at the centre (200, 200) stays at (200, 200)
        cx = canvas._offset.x() + 200 * canvas.zoom
        assert cx == pytest.approx(200)

    def test_zoom_fit_ignores_an_absent_or_null_pixmap(self, canvas):
        canvas._zoom = 3.0
        canvas._pixmap = None
        canvas.zoom_fit()
        assert canvas.zoom == 3.0
        canvas._pixmap = QPixmap()
        canvas.zoom_fit()
        assert canvas.zoom == 3.0


class TestWheel:
    def test_wheel_navigates_between_photos(self, canvas, qtbot):
        with qtbot.waitSignal(canvas.wheel_navigate, timeout=1000) as blocker:
            _wheel(canvas, 120)
        assert blocker.args == [1]
        with qtbot.waitSignal(canvas.wheel_navigate, timeout=1000) as blocker:
            _wheel(canvas, -120)
        assert blocker.args == [-1]

    @pytest.mark.parametrize("enter", ["enter_red_eye_mode", "enter_face_add_mode",
                                       "enter_annotation_mode"])
    def test_wheel_is_inert_in_the_tool_modes(self, canvas, enter):
        """Navigating to the next photo in the middle of a drawing would lose the
        work in progress: the wheel does strictly nothing there."""
        getattr(canvas, enter)()
        received = []
        canvas.wheel_navigate.connect(received.append)
        canvas.zoom_changed.connect(received.append)
        _wheel(canvas, 120)
        assert received == []
        assert canvas.zoom == 1.0

    def test_wheel_zooms_in_crop_mode_and_keeps_the_relative_quad(self, canvas):
        canvas.enter_crop((0.25, 0.25, 0.75, 0.25, 0.75, 0.75, 0.25, 0.75))
        before = canvas._crop_to_rel()
        _wheel(canvas, 120)
        assert canvas.zoom == pytest.approx(1.15)
        assert list(canvas._crop_to_rel()) == pytest.approx(list(before), abs=0.02)

    def test_wheel_refuses_to_leave_the_zoom_range_in_crop_mode(self, canvas):
        canvas.enter_crop()
        canvas._zoom = 3.9
        _wheel(canvas, 120)          # 3.9 * 1.15 = 4.485 > 4.0
        assert canvas.zoom == 3.9


class TestCropDrawing:
    def test_drawing_from_scratch_builds_the_quad(self, canvas):
        canvas.enter_crop()
        assert canvas._crop_quad is None
        _press(canvas, 100, 100)
        assert canvas._crop_action == 'DRAWING'
        _move(canvas, 300, 250)
        assert _bounds(canvas._crop_quad) == (100, 100, 300, 250)
        _release(canvas, 300, 250)
        assert canvas._crop_action is None
        assert canvas._crop_draw_start is None

    def test_drawing_is_clamped_to_the_image(self, canvas):
        canvas.enter_crop()
        _press(canvas, 100, 100)
        _move(canvas, 900, -400)
        assert _bounds(canvas._crop_quad) == (100, 0, SIDE, 100)

    def test_drawing_outside_the_image_starts_nothing(self, canvas):
        """The canvas is bigger than the photo as soon as the zoom no longer fills
        it: a click on the grey background must not create a crop area."""
        canvas._zoom = 0.5           # the photo now occupies 200x200 at the origin
        canvas.enter_crop()
        _press(canvas, 350, 350)
        assert canvas._crop_action is None
        assert canvas._crop_quad is None

    def test_locked_ratio_applies_from_the_first_movement(self, canvas):
        canvas.enter_crop()
        canvas.set_aspect_ratio(2.0)
        _press(canvas, 100, 100)
        _move(canvas, 300, 250)
        x0, y0, x1, y1 = _bounds(canvas._crop_quad)
        assert (x1 - x0) / (y1 - y0) == pytest.approx(2.0)
        assert (x0, y0) == (100, 100)

    def test_a_movement_under_the_threshold_does_not_lock_the_ratio_yet(self, canvas):
        canvas.enter_crop()
        canvas.set_aspect_ratio(2.0)
        _press(canvas, 100, 100)
        _move(canvas, 102, 102)      # 2 px: below the 4 px threshold
        assert canvas._drag_ratio is None
        assert _bounds(canvas._crop_quad) == (100, 100, 102, 102)


class TestCropHandles:
    @pytest.fixture
    def cropping(self, canvas):
        """Crop mode with a square area of 200 px centred on the photo."""
        canvas.enter_crop((0.25, 0.25, 0.5, 0.5))     # legacy x,y,w,h format
        assert _bounds(canvas._crop_quad) == (100, 100, 300, 300)
        return canvas

    def test_corner_drag_moves_only_that_corner(self, cropping):
        _press(cropping, 100, 100)
        assert cropping._crop_action == 'RESIZING'
        assert cropping._crop_handle == 0
        _move(cropping, 60, 80)
        assert (cropping._crop_quad[0].x(), cropping._crop_quad[0].y()) == (60, 80)
        assert (cropping._crop_quad[2].x(), cropping._crop_quad[2].y()) == (300, 300)

    def test_corner_drag_is_clamped_to_the_image(self, cropping):
        _press(cropping, 300, 300)
        _move(cropping, 900, 900)
        assert (cropping._crop_quad[2].x(), cropping._crop_quad[2].y()) == (SIDE, SIDE)

    def test_corner_drag_with_a_locked_ratio_anchors_the_opposite_corner(self, cropping):
        cropping.set_aspect_ratio(2.0)
        opposite = QPointF(cropping._crop_quad[2])
        _press(cropping, cropping._crop_quad[0].x(), cropping._crop_quad[0].y())
        _move(cropping, 40, 40)
        x0, y0, x1, y1 = _bounds(cropping._crop_quad)
        assert (x1 - x0) / (y1 - y0) == pytest.approx(2.0)
        assert (x1, y1) == (opposite.x(), opposite.y())

    def test_edge_drag_moves_that_edge_only(self, cropping):
        _press(cropping, 200, 100)          # middle of the top edge
        assert cropping._crop_action == 'RESIZING_EDGE'
        assert cropping._crop_handle == 0
        _move(cropping, 200, 140)
        assert _bounds(cropping._crop_quad) == (100, 140, 300, 300)

    def test_edge_drag_is_clamped_to_the_image(self, cropping):
        _press(cropping, 300, 200)          # middle of the right edge
        _move(cropping, 900, 200)
        assert _bounds(cropping._crop_quad) == (100, 100, SIDE, 300)

    def test_edge_drag_with_a_locked_ratio_preserves_it(self, cropping):
        cropping.set_aspect_ratio(2.0)
        _, y0_before, _, y1_before = _bounds(cropping._crop_quad)
        mid = cropping._edge_handle_positions()[0]
        _press(cropping, mid.x(), mid.y())
        _move(cropping, mid.x(), mid.y() + 20)
        x0, y0, x1, y1 = _bounds(cropping._crop_quad)
        assert (x1 - x0) / (y1 - y0) == pytest.approx(2.0)
        assert y0 == pytest.approx(y0_before + 20)
        assert y1 == pytest.approx(y1_before)     # opposite edge anchored

    def test_centre_drag_translates_the_area(self, cropping):
        _press(cropping, 200, 200)
        assert cropping._crop_action == 'MOVING'
        _move(cropping, 250, 230)
        assert _bounds(cropping._crop_quad) == (150, 130, 350, 330)

    def test_centre_drag_never_leaves_the_image(self, cropping):
        _press(cropping, 200, 200)
        _move(cropping, 900, 900)
        assert _bounds(cropping._crop_quad) == (200, 200, SIDE, SIDE)

    def test_panning_keeps_the_area_glued_to_the_photo(self, cropping):
        before = cropping._crop_to_rel()
        _press(cropping, 350, 350)          # inside the photo, outside the area
        assert cropping._crop_action == 'PANNING'
        _move(cropping, 380, 380)
        assert (cropping._offset.x(), cropping._offset.y()) == (30, 30)
        assert list(cropping._crop_to_rel()) == pytest.approx(list(before), abs=0.01)

    def test_release_clears_every_drag_state(self, cropping):
        _press(cropping, 100, 100)
        _move(cropping, 60, 60)
        _release(cropping, 60, 60)
        assert cropping._crop_action is None
        assert cropping._crop_handle is None
        assert cropping._crop_quad_start is None
        assert cropping._drag_ratio is None

    def test_right_click_starts_no_drag(self, cropping):
        _press(cropping, 100, 100, button=Qt.RightButton)
        assert cropping._crop_action is None


class TestCropCursors:
    """The cursor is the only feedback telling which handle is under the mouse."""

    @pytest.fixture
    def cropping(self, canvas):
        canvas.enter_crop((0.25, 0.25, 0.5, 0.5))
        return canvas

    def test_corner(self, cropping):
        cropping._update_cursor_for_pos(QPointF(100, 100))
        assert cropping.cursor().shape() == Qt.SizeFDiagCursor
        cropping._update_cursor_for_pos(QPointF(300, 100))
        assert cropping.cursor().shape() == Qt.SizeBDiagCursor

    def test_edges_follow_their_orientation(self, cropping):
        cropping._update_cursor_for_pos(QPointF(200, 100))    # horizontal edge
        assert cropping.cursor().shape() == Qt.SizeVerCursor
        cropping._update_cursor_for_pos(QPointF(300, 200))    # vertical edge
        assert cropping.cursor().shape() == Qt.SizeHorCursor

    def test_centre_then_image_then_outside(self, cropping):
        cropping._update_cursor_for_pos(QPointF(200, 200))
        assert cropping.cursor().shape() == Qt.SizeAllCursor
        cropping._update_cursor_for_pos(QPointF(350, 350))
        assert cropping.cursor().shape() == Qt.OpenHandCursor
        cropping._zoom = 0.5                                  # photo reduced to 200x200
        cropping._update_cursor_for_pos(QPointF(390, 390))
        assert cropping.cursor().shape() == Qt.CrossCursor


class TestCropConfirmation:
    def test_confirm_emits_the_relative_quad_and_leaves_the_mode(self, canvas, qtbot):
        canvas.enter_crop((0.25, 0.25, 0.5, 0.5))
        with qtbot.waitSignal(canvas.crop_confirmed, timeout=1000) as blocker:
            canvas.confirm_crop()
        assert list(blocker.args[0]) == pytest.approx(
            [0.25, 0.25, 0.75, 0.25, 0.75, 0.75, 0.25, 0.75])
        assert canvas._crop_mode is False
        assert canvas._crop_quad is None

    def test_confirm_clamps_a_quad_that_overflows(self, canvas, qtbot):
        canvas.enter_crop()
        canvas._crop_quad = [QPointF(-40, -40), QPointF(500, -40),
                             QPointF(500, 500), QPointF(-40, 500)]
        with qtbot.waitSignal(canvas.crop_confirmed, timeout=1000) as blocker:
            canvas.confirm_crop()
        assert set(blocker.args[0]) == {0.0, 1.0}

    def test_confirm_without_an_area_only_cancels(self, canvas):
        received = []
        canvas.crop_confirmed.connect(received.append)
        canvas.enter_crop()
        canvas.confirm_crop()
        assert received == []
        assert canvas._crop_mode is False

    def test_cancel_forgets_the_area(self, canvas):
        canvas.enter_crop((0.1, 0.1, 0.8, 0.8))
        canvas.cancel_crop()
        assert canvas._crop_quad is None
        assert canvas._crop_mode is False

    def test_set_aspect_ratio_refits_an_existing_area(self, canvas):
        canvas.enter_crop((0.25, 0.25, 0.5, 0.5))     # square of 200 px
        canvas.set_aspect_ratio(2.0)
        x0, y0, x1, y1 = _bounds(canvas._crop_quad)
        assert (x1 - x0) / (y1 - y0) == pytest.approx(2.0)
        # Area preserved and centre unchanged
        assert (x1 - x0) * (y1 - y0) == pytest.approx(200 * 200, rel=0.01)
        assert ((x0 + x1) / 2, (y0 + y1) / 2) == pytest.approx((200, 200))

    def test_resize_of_the_widget_keeps_the_relative_area(self, canvas):
        canvas.enter_crop((0.25, 0.25, 0.5, 0.5))
        before = canvas._crop_to_rel()
        canvas.resize(600, 300)
        canvas.resizeEvent(QResizeEvent(QSize(600, 300), QSize(SIDE, SIDE)))
        assert list(canvas._crop_to_rel()) == pytest.approx(list(before), abs=0.01)


class _Face:
    """Minimal stand-in for FaceInfo: _face_screen_rect only reads the bbox and
    detected_rotation."""

    def __init__(self, x, y, w, h, rot=0):
        self.bbox_x, self.bbox_y, self.bbox_w, self.bbox_h = x, y, w, h
        self.detected_rotation = rot


class TestFaceAddMode:
    def test_drawing_then_confirming_emits_the_bbox(self, canvas, qtbot):
        canvas.set_orig_size(SIDE, SIDE)      # 1:1 between screen and photo
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 180)
        assert canvas._face_add_rect is not None
        with qtbot.waitSignal(canvas.face_add_confirmed, timeout=1000) as blocker:
            canvas.confirm_face_add()
        assert blocker.args[0] == (100, 100, 100, 80)
        assert canvas._face_add_mode is False

    def test_a_rectangle_too_small_to_be_a_face_is_dropped(self, canvas):
        canvas.set_orig_size(SIDE, SIDE)
        received = []
        canvas.face_add_confirmed.connect(received.append)
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 104, 104)     # 4 px: under the 8 px floor
        canvas.confirm_face_add()
        assert received == []
        assert canvas._face_add_mode is False

    def test_drawing_is_clamped_to_the_image(self, canvas):
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 900, 900)
        r = canvas._face_add_rect
        assert (r.right(), r.bottom()) == (SIDE, SIDE)

    def test_a_corner_handle_resizes_the_rectangle(self, canvas):
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 200)
        _press(canvas, 100, 100)              # TL handle
        assert canvas._face_add_action == 'RESIZING'
        _move(canvas, 60, 70)
        r = canvas._face_add_rect
        assert (r.left(), r.top(), r.right(), r.bottom()) == (60, 70, 200, 200)

    def test_dragging_the_inside_moves_the_rectangle(self, canvas):
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 200)
        _press(canvas, 150, 150)
        assert canvas._face_add_action == 'MOVING'
        _move(canvas, 170, 190)
        r = canvas._face_add_rect
        assert (r.left(), r.top(), r.width(), r.height()) == (120, 140, 100, 100)

    def test_the_moved_rectangle_stays_inside_the_image(self, canvas):
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 200)
        _press(canvas, 150, 150)
        _move(canvas, 900, 900)
        r = canvas._face_add_rect
        assert (r.right(), r.bottom()) == (SIDE, SIDE)

    def test_the_cursor_announces_the_handle_then_the_move(self, canvas):
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 200)
        _move(canvas, 100, 100, buttons=Qt.NoButton)
        assert canvas.cursor().shape() == Qt.SizeFDiagCursor
        _move(canvas, 150, 150, buttons=Qt.NoButton)
        assert canvas.cursor().shape() == Qt.SizeAllCursor
        _move(canvas, 350, 350, buttons=Qt.NoButton)
        assert canvas.cursor().shape() == Qt.CrossCursor

    def test_cancelling_forgets_everything(self, canvas):
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 200)
        canvas.cancel_face_add_mode()
        assert canvas._face_add_mode is False
        assert canvas._face_add_rect is None

    def test_a_widget_resize_drops_the_rectangle(self, canvas):
        """The rectangle is in screen coordinates: a zoom/offset change would leave
        it aiming at another part of the photo -- better to lose it than to lie."""
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 200)
        canvas.resize(600, 300)
        canvas.resizeEvent(QResizeEvent(QSize(600, 300), QSize(SIDE, SIDE)))
        assert canvas._face_add_rect is None
        assert canvas._face_add_action is None

    def test_the_bbox_is_unknown_without_the_original_dimensions(self, canvas):
        canvas.enter_face_add_mode()
        _drag(canvas, 100, 100, 200, 200)
        assert canvas._bbox_from_screen_rect(canvas._face_add_rect) is None

    @pytest.mark.parametrize("edit", [
        EditInfo(),
        EditInfo(rotation=90),
        EditInfo(rotation=180),
        EditInfo(rotation=270),
        EditInfo(flip_h=True),
        EditInfo(flip_v=True),
        EditInfo(crop=(0.1, 0.2, 0.6, 0.5)),
        EditInfo(rotation=90, flip_h=True, crop=(0.1, 0.1, 0.8, 0.8)),
    ])
    def test_bbox_from_screen_rect_inverts_face_screen_rect(self, canvas, edit):
        """The two functions must stay strict inverses of each other for every
        combination of rotation/flip/crop: a manually drawn face is stored through
        one and redisplayed through the other."""
        canvas.set_orig_size(400, 300)
        canvas.set_edit(edit)
        face = _Face(60, 40, 50, 45)
        rect = canvas._face_screen_rect(face)
        assert rect is not None
        assert canvas._bbox_from_screen_rect(rect) == pytest.approx((60, 40, 50, 45), abs=2)


class TestVignetteMode:
    @pytest.fixture
    def vignetting(self, canvas):
        canvas.enter_vignette_mode(EditInfo(vignette_strength=0.5))
        return canvas

    def test_the_handles_sit_on_the_two_ellipses(self, vignetting):
        h = vignetting._vignette_handle_positions()
        assert (h['center'].x(), h['center'].y()) == (200, 200)
        assert (h['inner_e'].x(), h['inner_e'].y()) == (280, 200)   # rx1 = 0.40
        assert (h['outer_n'].x(), h['outer_n'].y()) == (200, 40)    # ry2 = 0.80
        assert (h['rotate'].x(), h['rotate'].y()) == (200, 12)      # 28 px above outer_n

    def test_no_handle_without_an_edit_or_a_pixmap(self, canvas):
        assert canvas._vignette_handle_positions() == {}

    def test_dragging_the_centre_moves_the_vignette(self, vignetting, qtbot):
        _press(vignetting, 200, 200)
        assert vignetting._vignette_drag == 'center'
        with qtbot.waitSignal(vignetting.vignette_changed, timeout=1000) as blocker:
            _move(vignetting, 240, 230)
        assert blocker.args[0].vignette_cx == pytest.approx(0.6)
        assert blocker.args[0].vignette_cy == pytest.approx(0.575)

    def test_dragging_an_inner_handle_changes_the_inner_radius_only(self, vignetting):
        _press(vignetting, 280, 200)
        assert vignetting._vignette_drag == 'inner_e'
        _move(vignetting, 320, 200)
        e = vignetting._vignette_edit
        assert e.vignette_rx1 == pytest.approx(0.6)
        assert e.vignette_rx2 == pytest.approx(0.8)     # outer untouched

    def test_dragging_an_outer_handle_changes_the_outer_radius_only(self, vignetting):
        _press(vignetting, 200, 40)
        assert vignetting._vignette_drag == 'outer_n'
        _move(vignetting, 200, 20)
        e = vignetting._vignette_edit
        assert e.vignette_ry2 == pytest.approx(0.9)
        assert e.vignette_ry1 == pytest.approx(0.4)

    def test_a_radius_never_collapses_to_zero(self, vignetting):
        _press(vignetting, 280, 200)
        _move(vignetting, 0, 200)
        assert vignetting._vignette_edit.vignette_rx1 == pytest.approx(0.05)

    def test_the_rotation_handle_turns_the_ellipses(self, vignetting):
        _press(vignetting, 200, 12)
        assert vignetting._vignette_drag == 'rotate'
        _move(vignetting, 388, 200)          # from 12 o'clock to 3 o'clock
        assert vignetting._vignette_edit.vignette_angle == pytest.approx(90.0)

    def test_the_release_ends_the_drag(self, vignetting):
        _press(vignetting, 200, 200)
        _move(vignetting, 220, 220)
        _release(vignetting, 220, 220)
        assert vignetting._vignette_drag is None
        assert vignetting._vignette_edit_start is None

    def test_the_cursor_announces_a_grabbable_handle(self, vignetting):
        _move(vignetting, 280, 200, buttons=Qt.NoButton)
        assert vignetting.cursor().shape() == Qt.PointingHandCursor
        _move(vignetting, 150, 150, buttons=Qt.NoButton)
        assert vignetting.cursor().shape() == Qt.ArrowCursor

    def test_a_click_beside_the_handles_grabs_nothing(self, vignetting):
        _press(vignetting, 150, 150)
        assert vignetting._vignette_drag is None

    def test_update_vignette_only_applies_inside_the_mode(self, canvas):
        canvas.update_vignette(EditInfo(vignette_cx=0.1))
        assert canvas._vignette_edit is None
        canvas.enter_vignette_mode(EditInfo())
        canvas.update_vignette(EditInfo(vignette_cx=0.1))
        assert canvas._vignette_edit.vignette_cx == pytest.approx(0.1)

    def test_leaving_the_mode_forgets_the_geometry(self, vignetting):
        vignetting.exit_vignette_mode()
        assert vignetting._vignette_mode is False
        assert vignetting._vignette_edit is None


class TestRedEyeMode:
    def test_a_click_reports_the_relative_position(self, canvas, qtbot):
        canvas.enter_red_eye_mode(0.05)
        with qtbot.waitSignal(canvas.red_eye_point_added, timeout=1000) as blocker:
            _press(canvas, 300, 100)
        assert blocker.args == [pytest.approx(0.75), pytest.approx(0.25)]

    def test_a_click_outside_the_photo_reports_nothing(self, canvas):
        canvas._zoom = 0.5                    # the photo occupies 200x200
        canvas.enter_red_eye_mode()
        received = []
        canvas.red_eye_point_added.connect(lambda *a: received.append(a))
        _press(canvas, 350, 350)
        assert received == []

    def test_the_cursor_circle_follows_the_mouse(self, canvas):
        canvas.enter_red_eye_mode()
        _move(canvas, 120, 140, buttons=Qt.NoButton)
        assert (canvas._red_eye_mouse.x(), canvas._red_eye_mouse.y()) == (120, 140)
        canvas.exit_red_eye_mode()
        assert canvas._red_eye_mouse is None
        assert canvas._red_eye_mode is False

    def test_the_radius_has_a_floor(self, canvas):
        canvas.enter_red_eye_mode(0.0)
        assert canvas._red_eye_radius == pytest.approx(0.005)
        canvas.set_red_eye_radius(0.2)
        assert canvas._red_eye_screen_radius() == pytest.approx(0.2 * SIDE)


class TestColorPick:
    def test_a_click_samples_the_pixel_and_leaves_the_mode(self, canvas, qtbot):
        canvas.start_color_pick()
        assert canvas.cursor().shape() == Qt.CrossCursor
        with qtbot.waitSignal(canvas.pixel_sampled, timeout=1000) as blocker:
            _press(canvas, 200, 200)
        assert blocker.args == [10, 120, 200]
        assert canvas._wb_pick_mode is False

    def test_a_click_outside_the_photo_samples_nothing_but_still_leaves(self, canvas):
        canvas._zoom = 0.5
        canvas.start_color_pick()
        received = []
        canvas.pixel_sampled.connect(lambda *a: received.append(a))
        _press(canvas, 350, 350)
        assert received == []
        assert canvas._wb_pick_mode is False

    def test_stop_color_pick_disarms_it(self, canvas):
        canvas.start_color_pick()
        canvas.stop_color_pick()
        received = []
        canvas.pixel_sampled.connect(lambda *a: received.append(a))
        _press(canvas, 200, 200)
        assert received == []


class TestPanOutsideAnyMode:
    def test_dragging_moves_the_photo(self, canvas):
        _press(canvas, 200, 200)
        _move(canvas, 260, 240)
        assert (canvas._offset.x(), canvas._offset.y()) == (60, 40)
        _release(canvas, 260, 240)
        assert canvas._drag_start is None

    def test_a_move_without_a_press_does_not_move_the_photo(self, canvas):
        _move(canvas, 260, 240, buttons=Qt.NoButton)
        assert (canvas._offset.x(), canvas._offset.y()) == (0, 0)


# --------------------------------------------------------------------- annotations

def _shape(ann_id, x0, y0, x1, y1, kind="rect", **extra):
    """rect/ellipse annotation: `points` holds the two opposite corners, in
    relative coordinates -- x 0.1 therefore reads as 40 px on the 400 px canvas."""
    ann = {"id": ann_id, "type": kind, "color": "#ffff0000", "width": 0.004,
           "points": [[x0, y0], [x1, y1]], "opacity": 0.4, "blur": 0.0}
    ann.update(extra)
    return ann


def _text(ann_id, x, y, text="Bonjour", **extra):
    ann = {"id": ann_id, "type": "text", "text": text, "color": "#ffff0000",
           "font_family": "Arial", "font_size": 0.04, "bold": False,
           "italic": False, "pos": [x, y]}
    ann.update(extra)
    return ann


def _sel_bounds(canvas, ann_id):
    """Screen bbox of an annotation, as the canvas itself computes it."""
    from src.ui.annotation_renderer import annotation_screen_bounds
    return annotation_screen_bounds(canvas._find_annotation(ann_id), SIDE, SIDE)


class TestAnnotationDrafts:
    @pytest.mark.parametrize("tool", ["line", "rect", "ellipse"])
    def test_a_two_point_tool_normalises_its_draft(self, canvas, qtbot, tool):
        canvas.enter_annotation_mode(tool)
        _press(canvas, 100, 100)
        assert canvas._annotation_draft_type == tool
        _move(canvas, 300, 200)
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            _release(canvas, 300, 200)
        ann = blocker.args[0]
        assert ann["type"] == tool
        assert ann["points"] == [[0.25, 0.25], [0.75, 0.5]]
        assert canvas._annotations == [ann]
        assert canvas._annotation_draft_type is None

    def test_a_surface_tool_carries_its_fill(self, canvas, qtbot):
        canvas.set_annotation_style("#ff00ff00", 0.01, "Arial", 0.04, False, False,
                                     fill_color="#4000ff00", opacity=0.3, blur=2.0)
        canvas.enter_annotation_mode("rect")
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            _drag(canvas, 100, 100, 200, 200)
        ann = blocker.args[0]
        assert (ann["color"], ann["width"]) == ("#ff00ff00", 0.01)
        assert (ann["fill_color"], ann["opacity"], ann["blur"]) == ("#4000ff00", 0.3, 2.0)

    def test_a_stroke_tool_carries_no_fill(self, canvas, qtbot):
        canvas.enter_annotation_mode("line")
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            _drag(canvas, 100, 100, 200, 200)
        assert "fill_color" not in blocker.args[0]

    def test_the_pen_accumulates_the_points_of_the_stroke(self, canvas, qtbot):
        canvas.enter_annotation_mode("pen")
        _press(canvas, 100, 100)
        for x in (110, 120, 130):
            _move(canvas, x, 100)
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            _release(canvas, 130, 100)
        assert blocker.args[0]["points"] == [[0.25, 0.25], [0.275, 0.25],
                                              [0.3, 0.25], [0.325, 0.25]]

    def test_the_pen_ignores_a_micro_movement(self, canvas):
        """Below 2 px the point would add nothing but weight to the stroke."""
        canvas.enter_annotation_mode("pen")
        _press(canvas, 100, 100)
        _move(canvas, 101, 100)
        assert len(canvas._annotation_draft_points) == 1

    def test_the_curve_accumulates_one_point_per_click(self, canvas, qtbot):
        canvas.enter_annotation_mode("curve")
        _press(canvas, 100, 100)
        _press(canvas, 200, 150)
        _press(canvas, 300, 100)
        assert len(canvas._annotation_draft_points) == 3
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            canvas.confirm_annotation_draft()
        assert len(blocker.args[0]["points"]) == 3

    def test_the_double_click_closes_the_curve_without_its_duplicate_point(self, canvas, qtbot):
        """The 2nd click of the double-click has already gone through
        mousePressEvent: confirming as-is would leave a point on top of another."""
        canvas.enter_annotation_mode("curve")
        _press(canvas, 100, 100)
        _press(canvas, 200, 150)
        _press(canvas, 200, 150)
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            _double_click(canvas, 200, 150)
        assert blocker.args[0]["points"] == [[0.25, 0.25], [0.5, 0.375]]

    def test_a_hovering_move_previews_the_next_point_of_the_curve(self, canvas):
        canvas.enter_annotation_mode("curve")
        _move(canvas, 200, 200, buttons=Qt.NoButton)
        assert canvas._annotation_hover_pos is None   # no stroke started yet
        _press(canvas, 100, 100)
        _move(canvas, 200, 200, buttons=Qt.NoButton)
        assert (canvas._annotation_hover_pos.x(), canvas._annotation_hover_pos.y()) == (200, 200)

    def test_drawing_outside_the_photo_starts_nothing(self, canvas):
        canvas._zoom = 0.5                     # the photo occupies 200x200
        canvas.enter_annotation_mode("rect")
        _press(canvas, 350, 350)
        assert canvas._annotation_draft_type is None

    def test_a_single_point_is_not_an_annotation(self, canvas):
        received = []
        canvas.annotation_added.connect(received.append)
        canvas.enter_annotation_mode("curve")
        _press(canvas, 100, 100)
        canvas.confirm_annotation_draft()
        assert received == []
        assert canvas._annotations == []

    def test_the_confirmed_points_are_clamped_to_the_photo(self, canvas, qtbot):
        canvas.enter_annotation_mode("line")
        canvas._annotation_draft_type = "line"
        canvas._annotation_draft_points = [QPointF(-40, -40), QPointF(500, 500)]
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            canvas.confirm_annotation_draft()
        assert blocker.args[0]["points"] == [[0.0, 0.0], [1.0, 1.0]]

    def test_changing_tool_drops_the_draft_in_progress(self, canvas):
        canvas.enter_annotation_mode("curve")
        _press(canvas, 100, 100)
        canvas.set_annotation_tool("rect")
        assert canvas._annotation_draft_points == []
        assert canvas._annotation_tool == "rect"

    def test_leaving_the_mode_drops_everything_in_progress(self, canvas):
        canvas.enter_annotation_mode("curve")
        _press(canvas, 100, 100)
        canvas.exit_annotation_mode()
        assert canvas._annotation_mode is False
        assert canvas._annotation_draft_points == []
        assert canvas._annotation_selected_ids == set()


class TestAnnotationSelection:
    @pytest.fixture
    def selecting(self, canvas):
        """Two rectangles far apart: A at 40-80 px, B at 240-280 px."""
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2),
                                 _shape("B", 0.6, 0.6, 0.7, 0.7)])
        canvas.enter_annotation_mode("select")
        return canvas

    def test_a_click_on_an_annotation_selects_it(self, selecting, qtbot):
        with qtbot.waitSignal(selecting.annotation_selection_changed, timeout=1000) as blocker:
            _press(selecting, 60, 60)
        assert blocker.args[0] == ["A"]
        assert selecting._annotation_drag_ids == ["A"]

    def test_a_click_on_the_void_deselects_and_starts_a_marquee(self, selecting):
        _press(selecting, 60, 60)
        _release(selecting, 60, 60)
        _press(selecting, 150, 150)
        assert selecting._annotation_selected_ids == set()
        assert selecting._annotation_marquee_start is not None

    def test_the_marquee_selects_everything_it_touches(self, selecting, qtbot):
        _press(selecting, 20, 20)
        _move(selecting, 300, 300)
        assert selecting._annotation_marquee_rect is not None
        with qtbot.waitSignal(selecting.annotation_selection_changed, timeout=1000) as blocker:
            _release(selecting, 300, 300)
        assert blocker.args[0] == ["A", "B"]

    def test_a_marquee_too_small_selects_nothing(self, selecting):
        _press(selecting, 150, 150)
        _move(selecting, 151, 151)
        _release(selecting, 151, 151)
        assert selecting._annotation_selected_ids == set()
        assert selecting._annotation_marquee_rect is None

    def test_ctrl_marquee_toggles_the_existing_selection(self, selecting):
        _press(selecting, 60, 60)          # A selected
        _release(selecting, 60, 60)
        _press(selecting, 20, 20, modifiers=Qt.ControlModifier)
        _move(selecting, 300, 300, modifiers=Qt.ControlModifier)
        _release(selecting, 300, 300, modifiers=Qt.ControlModifier)
        assert selecting._annotation_selected_ids == {"B"}   # A toggled off

    def test_ctrl_click_adds_then_removes(self, selecting):
        _press(selecting, 60, 60)
        _release(selecting, 60, 60)
        _press(selecting, 260, 260, modifiers=Qt.ControlModifier)
        assert selecting._annotation_selected_ids == {"A", "B"}
        _release(selecting, 260, 260)
        _press(selecting, 260, 260, modifiers=Qt.ControlModifier)
        assert selecting._annotation_selected_ids == {"A"}

    def test_clicking_one_member_selects_the_whole_group(self, selecting, qtbot):
        _press(selecting, 60, 60)
        _release(selecting, 60, 60)
        _press(selecting, 260, 260, modifiers=Qt.ControlModifier)
        _release(selecting, 260, 260)
        with qtbot.waitSignal(selecting.annotation_grouped, timeout=1000) as blocker:
            selecting.group_selected_annotations()
        assert set(blocker.args[0]) == {"A", "B"}
        selecting._set_annotation_selection(set())
        _press(selecting, 60, 60)
        assert selecting._annotation_selected_ids == {"A", "B"}

    def test_grouping_needs_at_least_two(self, selecting):
        received = []
        selecting.annotation_grouped.connect(received.append)
        selecting._set_annotation_selection({"A"})
        selecting.group_selected_annotations()
        assert received == []

    def test_ungrouping_frees_every_member(self, selecting, qtbot):
        selecting._set_annotation_selection({"A", "B"})
        selecting.group_selected_annotations()
        selecting._set_annotation_selection({"A"})       # one member is enough
        with qtbot.waitSignal(selecting.annotation_grouped, timeout=1000):
            selecting.ungroup_selected_annotations()
        assert [a["group"] for a in selecting._annotations] == [None, None]

    def test_ungrouping_outside_a_group_does_nothing(self, selecting):
        received = []
        selecting.annotation_grouped.connect(received.append)
        selecting._set_annotation_selection({"A"})
        selecting.ungroup_selected_annotations()
        assert received == []

    def test_deleting_one_annotation(self, selecting, qtbot):
        selecting._set_annotation_selection({"A"})
        with qtbot.waitSignal(selecting.annotation_deleted, timeout=1000) as blocker:
            assert selecting.delete_selected_annotation() == ["A"]
        assert blocker.args[0] == "A"
        assert [a["id"] for a in selecting._annotations] == ["B"]

    def test_deleting_several_goes_through_the_batch_signal(self, selecting, qtbot):
        selecting._set_annotation_selection({"A", "B"})
        with qtbot.waitSignal(selecting.annotation_deleted_multi, timeout=1000) as blocker:
            selecting.delete_selected_annotation()
        assert sorted(blocker.args[0]) == ["A", "B"]
        assert selecting._annotations == []

    def test_deleting_without_a_selection_does_nothing(self, selecting):
        received = []
        selecting.annotation_deleted.connect(received.append)
        assert selecting.delete_selected_annotation() == []
        assert received == []

    def test_clear_all_empties_the_layer(self, selecting, qtbot):
        selecting._set_annotation_selection({"A"})
        with qtbot.waitSignal(selecting.annotation_selection_changed, timeout=1000) as blocker:
            selecting.clear_all_annotations()
        assert blocker.args[0] == []
        assert selecting._annotations == []

    def test_reloading_the_layer_drops_a_stale_selection(self, selecting):
        """Photo change: the ids of the previous photo must not survive."""
        selecting._set_annotation_selection({"A", "B"})
        selecting.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2)])
        assert selecting._annotation_selected_ids == {"A"}

    def test_reloading_the_layer_cancels_a_drag_on_a_vanished_annotation(self, selecting):
        _press(selecting, 60, 60)
        assert selecting._annotation_drag_ids == ["A"]
        selecting.set_annotations([_shape("B", 0.6, 0.6, 0.7, 0.7)])
        assert selecting._annotation_drag_ids == []


class TestAnnotationDrag:
    @pytest.fixture
    def dragging(self, canvas):
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2)])
        canvas.enter_annotation_mode("select")
        return canvas

    def test_dragging_translates_in_relative_coordinates(self, dragging, qtbot):
        _press(dragging, 60, 60)
        _move(dragging, 100, 90)
        assert dragging._annotations[0]["points"] == [
            pytest.approx([0.2, 0.175]), pytest.approx([0.3, 0.275])]
        with qtbot.waitSignal(dragging.annotation_moved, timeout=1000) as blocker:
            _release(dragging, 100, 90)
        assert blocker.args[0] == "A"
        assert blocker.args[1]["points"] == [pytest.approx([0.2, 0.175]),
                                              pytest.approx([0.3, 0.275])]

    def test_a_click_that_does_not_move_moves_nothing(self, dragging):
        """3 px anti-click threshold: selecting must not shift the annotation."""
        received = []
        dragging.annotation_moved.connect(lambda *a: received.append(a))
        _press(dragging, 60, 60)
        _move(dragging, 61, 61)
        _release(dragging, 61, 61)
        assert received == []
        assert dragging._annotations[0]["points"] == [[0.1, 0.1], [0.2, 0.2]]

    def test_dragging_several_goes_through_the_batch_signal(self, dragging, qtbot):
        dragging.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2),
                                   _shape("B", 0.6, 0.6, 0.7, 0.7)])
        dragging._set_annotation_selection({"A", "B"})
        _press(dragging, 60, 60)
        _move(dragging, 100, 60)
        with qtbot.waitSignal(dragging.annotation_moved_multi, timeout=1000) as blocker:
            _release(dragging, 100, 60)
        assert sorted(blocker.args[0]) == ["A", "B"]
        assert dragging._annotations[1]["points"][0] == pytest.approx([0.7, 0.6])

    def test_dragging_a_text_moves_its_anchor(self, dragging):
        dragging.set_annotations([_text("T", 0.1, 0.1)])
        dragging._set_annotation_selection({"T"})
        bounds = _sel_bounds(dragging, "T")
        _press(dragging, bounds.center().x(), bounds.center().y())
        _move(dragging, bounds.center().x() + 40, bounds.center().y())
        assert dragging._annotations[0]["pos"] == pytest.approx([0.2, 0.1])


class TestAnnotationResize:
    @pytest.fixture
    def resizing(self, canvas):
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2)])
        canvas.enter_annotation_mode("select")
        canvas._set_annotation_selection({"A"})
        return canvas

    def test_the_handles_frame_the_annotation(self, resizing):
        ann = resizing._find_annotation("A")
        h = resizing._annotation_handle_positions(ann)
        bounds = _sel_bounds(resizing, "A")
        assert (h['tl'].x(), h['tl'].y()) == (bounds.left(), bounds.top())
        assert (h['br'].x(), h['br'].y()) == (bounds.right(), bounds.bottom())
        assert h['rotate'].y() == pytest.approx(bounds.top() - 28)

    def test_dragging_a_corner_scales_from_the_opposite_one(self, resizing, qtbot):
        ann = resizing._find_annotation("A")
        br = resizing._annotation_handle_positions(ann)['br']
        left0, top0 = _sel_bounds(resizing, "A").left(), _sel_bounds(resizing, "A").top()
        _press(resizing, br.x(), br.y())
        assert resizing._annotation_resize_handle == 'br'
        _move(resizing, 130, br.y())
        after = _sel_bounds(resizing, "A")
        assert after.right() == pytest.approx(130, abs=2)
        assert (after.left(), after.top()) == pytest.approx((left0, top0), abs=0.5)
        with qtbot.waitSignal(resizing.annotation_resized, timeout=1000) as blocker:
            _release(resizing, 130, br.y())
        assert blocker.args[0] == "A"
        assert resizing._annotation_resize_handle is None

    def test_an_annotation_never_collapses_to_nothing(self, resizing):
        ann = resizing._find_annotation("A")
        br = resizing._annotation_handle_positions(ann)['br']
        _press(resizing, br.x(), br.y())
        _move(resizing, 0, 0)              # dragged onto its own anchor
        after = _sel_bounds(resizing, "A")
        assert after.width() >= 8
        assert after.height() >= 8

    def test_the_rotation_handle_turns_the_annotation(self, resizing):
        ann = resizing._find_annotation("A")
        h = resizing._annotation_handle_positions(ann)
        center = _sel_bounds(resizing, "A").center()
        _press(resizing, h['rotate'].x(), h['rotate'].y())
        assert resizing._annotation_resize_handle == 'rotate'
        _move(resizing, center.x() + 50, center.y())    # from 12 o'clock to 3 o'clock
        assert resizing._annotations[0]["angle"] == pytest.approx(90.0)

    def test_the_handles_follow_the_rotation(self, resizing):
        ann = resizing._find_annotation("A")
        ann["angle"] = 90.0
        h = resizing._annotation_handle_positions(ann)
        center = _sel_bounds(resizing, "A").center()
        # The top-left corner ends up at the top RIGHT after a quarter turn CW
        assert (h['tl'].x(), h['tl'].y()) == pytest.approx(
            (center.x() + (center.y() - _sel_bounds(resizing, "A").top()),
             center.y() - (center.x() - _sel_bounds(resizing, "A").left())), abs=0.01)

    def test_resizing_a_text_changes_its_size_not_its_anchor(self, resizing):
        resizing.set_annotations([_text("T", 0.1, 0.1)])
        resizing._set_annotation_selection({"T"})
        ann = resizing._find_annotation("T")
        br = resizing._annotation_handle_positions(ann)['br']
        _press(resizing, br.x(), br.y())
        _move(resizing, br.x() + 60, br.y() + 60)
        assert ann["font_size"] > 0.04
        assert ann["pos"] == [0.1, 0.1]

    def test_a_handle_is_only_grabbable_on_a_single_selection(self, resizing):
        resizing.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2),
                                   _shape("B", 0.6, 0.6, 0.7, 0.7)])
        resizing._set_annotation_selection({"A", "B"})
        ann = resizing._find_annotation("A")
        br = resizing._annotation_handle_positions(ann)['br']
        _press(resizing, br.x(), br.y())
        assert resizing._annotation_resize_handle is None

    def test_the_cursor_announces_the_handles(self, resizing):
        ann = resizing._find_annotation("A")
        h = resizing._annotation_handle_positions(ann)
        _move(resizing, h['br'].x(), h['br'].y(), buttons=Qt.NoButton)
        assert resizing.cursor().shape() == Qt.SizeFDiagCursor
        _move(resizing, h['rotate'].x(), h['rotate'].y(), buttons=Qt.NoButton)
        assert resizing.cursor().shape() == Qt.PointingHandCursor
        _move(resizing, 60, 60, buttons=Qt.NoButton)
        assert resizing.cursor().shape() == Qt.OpenHandCursor
        _move(resizing, 350, 350, buttons=Qt.NoButton)
        assert resizing.cursor().shape() == Qt.ArrowCursor


class TestAnnotationTextEditor:
    def test_a_click_opens_the_editor_and_enter_creates_the_text(self, canvas, qtbot):
        canvas.enter_annotation_mode("text")
        _press(canvas, 120, 160)
        editor = canvas._annotation_text_editor
        assert editor is not None
        editor.setPlainText("Bonjour")
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            editor.confirmed.emit()
        ann = blocker.args[0]
        assert ann["type"] == "text" and ann["text"] == "Bonjour"
        assert ann["pos"] == [pytest.approx(0.3), pytest.approx(0.4)]
        assert canvas._annotation_text_editor is None

    def test_an_empty_text_creates_nothing(self, canvas):
        received = []
        canvas.annotation_added.connect(received.append)
        canvas.enter_annotation_mode("text")
        _press(canvas, 120, 160)
        canvas._annotation_text_editor.confirmed.emit()
        assert received == []
        assert canvas._annotations == []

    def test_escape_cancels_the_capture(self, canvas):
        received = []
        canvas.annotation_added.connect(received.append)
        canvas.enter_annotation_mode("text")
        _press(canvas, 120, 160)
        editor = canvas._annotation_text_editor
        editor.setPlainText("Bonjour")
        editor.cancelled.emit()
        assert received == []
        assert canvas._annotation_text_editor is None

    def test_a_second_click_does_not_open_a_second_editor(self, canvas):
        canvas.enter_annotation_mode("text")
        _press(canvas, 120, 160)
        first = canvas._annotation_text_editor
        _press(canvas, 200, 200)
        assert canvas._annotation_text_editor is first

    def test_a_click_outside_the_photo_opens_nothing(self, canvas):
        canvas._zoom = 0.5
        canvas.enter_annotation_mode("text")
        _press(canvas, 350, 350)
        assert canvas._annotation_text_editor is None

    def test_the_double_click_reopens_an_existing_text(self, canvas, qtbot):
        canvas.set_annotations([_text("T", 0.1, 0.1)])
        canvas.enter_annotation_mode("select")
        bounds = _sel_bounds(canvas, "T")
        _double_click(canvas, bounds.center().x(), bounds.center().y())
        editor = canvas._annotation_text_editor
        assert editor is not None
        assert editor.toPlainText() == "Bonjour"
        editor.setPlainText("Bonsoir")
        with qtbot.waitSignal(canvas.annotation_moved, timeout=1000) as blocker:
            editor.confirmed.emit()
        assert blocker.args[0] == "T"
        assert canvas._annotations[0]["text"] == "Bonsoir"

    def test_emptying_an_existing_text_deletes_it(self, canvas, qtbot):
        canvas.set_annotations([_text("T", 0.1, 0.1)])
        canvas.enter_annotation_mode("select")
        bounds = _sel_bounds(canvas, "T")
        _double_click(canvas, bounds.center().x(), bounds.center().y())
        canvas._annotation_text_editor.setPlainText("   ")
        with qtbot.waitSignal(canvas.annotation_deleted, timeout=1000) as blocker:
            canvas._annotation_text_editor.confirmed.emit()
        assert blocker.args[0] == "T"
        assert canvas._annotations == []

    def test_a_double_click_beside_a_text_opens_nothing(self, canvas):
        canvas.set_annotations([_text("T", 0.1, 0.1)])
        canvas.enter_annotation_mode("select")
        _double_click(canvas, 350, 350)
        assert canvas._annotation_text_editor is None

    def test_enter_confirms_and_escape_cancels(self, canvas, qtbot):
        """The two keys really wired on _InlineTextEdit -- the editor has no
        button, they are the only way out."""
        from PySide6.QtGui import QKeyEvent
        canvas.enter_annotation_mode("text")
        _press(canvas, 120, 160)
        editor = canvas._annotation_text_editor
        with qtbot.waitSignal(editor.confirmed, timeout=1000):
            editor.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
        _press(canvas, 120, 160)
        editor = canvas._annotation_text_editor
        with qtbot.waitSignal(editor.cancelled, timeout=1000):
            editor.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))

    def test_shift_enter_stays_in_the_text(self, canvas):
        from PySide6.QtGui import QKeyEvent, QTextCursor
        canvas.enter_annotation_mode("text")
        _press(canvas, 120, 160)
        editor = canvas._annotation_text_editor
        received = []
        editor.confirmed.connect(lambda: received.append(1))
        editor.setPlainText("a")
        editor.moveCursor(QTextCursor.End)
        editor.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ShiftModifier))
        assert received == []
        assert "\n" in editor.toPlainText()


# --------------------------------------------------------------------- painting

class TestPainting:
    """Every overlay is checked by comparing the RENDERED image with the bare
    photo: a `paintEvent` that swallows an exception, or a branch never reached,
    would leave the two identical -- which a simple `canvas.grab()` with no
    assertion (a smoke test) would happily accept."""

    @pytest.fixture
    def baseline(self, canvas):
        return _render(canvas)

    def test_the_photo_alone_is_the_reference(self, canvas, baseline):
        assert _render(canvas) == baseline

    def test_an_empty_canvas_still_paints_its_background(self, qtbot):
        c = _Canvas()
        qtbot.addWidget(c)
        c.resize(80, 60)
        img = _render(c)
        assert img.pixelColor(40, 30) == QColor(30, 30, 30)

    def test_the_grid_of_thirds(self, canvas, baseline):
        canvas.set_grid_visible(True)
        assert _render(canvas) != baseline
        canvas.set_grid_visible(False)
        assert _render(canvas) == baseline

    def test_the_crop_overlay(self, canvas, baseline):
        canvas.enter_crop((0.25, 0.25, 0.5, 0.5))
        assert _render(canvas) != baseline
        # The inside of the area stays clear, the outside is darkened
        img = _render(canvas)
        assert img.pixelColor(200, 200) != img.pixelColor(20, 20)

    def test_the_crop_overlay_without_an_area_yet(self, canvas, baseline):
        canvas.enter_crop()
        assert _render(canvas) != baseline      # the edging around the photo

    def test_the_vignette_overlay(self, canvas, baseline):
        canvas.enter_vignette_mode(EditInfo(vignette_strength=0.5))
        assert _render(canvas) != baseline

    def test_the_vignette_overlay_while_dragging(self, canvas):
        canvas.enter_vignette_mode(EditInfo(vignette_strength=0.5))
        before = _render(canvas)
        _press(canvas, 280, 200)                  # inner_e grabbed -> handle highlighted
        assert _render(canvas) != before

    def test_the_face_add_overlay(self, canvas, baseline):
        canvas.enter_face_add_mode()
        assert _render(canvas) != baseline       # the edging alone
        empty = _render(canvas)
        _drag(canvas, 100, 100, 200, 200)
        assert _render(canvas) != empty

    def test_the_red_eye_cursor(self, canvas, baseline):
        canvas.enter_red_eye_mode(0.05)
        assert _render(canvas) == baseline       # nothing until the mouse enters
        _move(canvas, 200, 200, buttons=Qt.NoButton)
        assert _render(canvas) != baseline

    def test_the_highlighted_face(self, canvas, baseline):
        canvas.set_orig_size(SIDE, SIDE)
        canvas.set_highlighted_face(_Face(100, 100, 80, 80))
        assert _render(canvas) != baseline
        canvas.set_highlighted_face(None)
        assert _render(canvas) == baseline

    def test_several_highlighted_faces(self, canvas, baseline):
        canvas.set_orig_size(SIDE, SIDE)
        canvas.set_highlighted_faces([_Face(40, 40, 60, 60), _Face(240, 240, 60, 60)])
        assert _render(canvas) != baseline
        canvas.set_highlighted_faces([])
        assert _render(canvas) == baseline

    def test_the_annotations_and_their_visibility(self, canvas, baseline):
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.4, 0.4)])
        assert _render(canvas) != baseline
        canvas.set_annotations_visible(False)
        assert _render(canvas) == baseline
        canvas.set_annotations_visible(True)
        assert _render(canvas) != baseline

    def test_a_text_annotation(self, canvas, baseline):
        canvas.set_annotations([_text("T", 0.2, 0.2)])
        assert _render(canvas) != baseline

    def test_a_blurred_annotation(self, canvas, baseline):
        """The blur samples the photo underneath: the rendering path is different
        from a plain fill."""
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.5, 0.5, blur=6.0, opacity=0.0)])
        assert _render(canvas) != baseline

    def test_the_selection_frame_and_its_handles(self, canvas):
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.4, 0.4)])
        canvas.enter_annotation_mode("select")
        unselected = _render(canvas)
        canvas._set_annotation_selection({"A"})
        assert _render(canvas) != unselected

    def test_the_selection_frame_of_a_rotated_annotation(self, canvas):
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.4, 0.2)])
        canvas.enter_annotation_mode("select")
        canvas._set_annotation_selection({"A"})
        straight = _render(canvas)
        canvas._annotations[0]["angle"] = 30.0
        assert _render(canvas) != straight

    def test_the_marquee_rectangle(self, canvas):
        canvas.set_annotations([_shape("A", 0.6, 0.6, 0.7, 0.7)])
        canvas.enter_annotation_mode("select")
        before = _render(canvas)
        _press(canvas, 20, 20)
        _move(canvas, 200, 200)
        assert _render(canvas) != before

    @pytest.mark.parametrize("tool", ["pen", "line", "rect", "ellipse"])
    def test_the_draft_in_progress(self, canvas, baseline, tool):
        canvas.enter_annotation_mode(tool)
        _press(canvas, 100, 100)
        _move(canvas, 250, 220)
        assert _render(canvas) != baseline

    def test_the_curve_draft_and_its_preview(self, canvas, baseline):
        canvas.enter_annotation_mode("curve")
        _press(canvas, 100, 100)
        one_point = _render(canvas)
        assert one_point != baseline               # a dot marks the first click
        _move(canvas, 250, 220, buttons=Qt.NoButton)
        assert _render(canvas) != one_point      # + the preview of the next segment
        _press(canvas, 250, 220)
        _press(canvas, 320, 120)
        assert _render(canvas) != one_point

    def test_an_outlineless_draft_still_paints_its_fill(self, canvas, baseline):
        canvas.set_annotation_style("#ffff0000", 0.0, "Arial", 0.04, False, False,
                                     fill_color="#ff00ff00", opacity=1.0, blur=0.0)
        canvas.enter_annotation_mode("rect")
        _press(canvas, 100, 100)
        _move(canvas, 250, 220)
        assert _render(canvas).pixelColor(180, 160) == QColor("#ff00ff00")


class TestContextMenu:
    def _context(self, canvas, x, y):
        canvas.contextMenuEvent(QContextMenuEvent(
            QContextMenuEvent.Mouse, QPoint(x, y),
            canvas.mapToGlobal(QPoint(x, y))))

    def test_a_right_click_on_the_photo_asks_for_the_menu(self, canvas, qtbot):
        with qtbot.waitSignal(canvas.context_menu_requested, timeout=1000):
            self._context(canvas, 200, 200)

    def test_a_right_click_on_a_face_asks_for_ITS_menu(self, canvas, qtbot):
        canvas.set_orig_size(SIDE, SIDE)
        face = _Face(100, 100, 80, 80)
        canvas.set_highlighted_face(face)
        with qtbot.waitSignal(canvas.face_context_menu_requested, timeout=1000) as blocker:
            self._context(canvas, 140, 140)
        assert blocker.args[0] is face

    def test_beside_the_face_the_menu_is_the_generic_one(self, canvas, qtbot):
        canvas.set_orig_size(SIDE, SIDE)
        canvas.set_highlighted_face(_Face(100, 100, 80, 80))
        with qtbot.waitSignal(canvas.context_menu_requested, timeout=1000):
            self._context(canvas, 300, 300)

    @pytest.mark.parametrize("enter", [
        lambda c: c.enter_crop((0.1, 0.1, 0.8, 0.8)),
        lambda c: c.enter_red_eye_mode(),
        lambda c: c.enter_face_add_mode(),
    ])
    def test_no_menu_while_a_tool_is_active(self, canvas, enter):
        received = []
        canvas.context_menu_requested.connect(received.append)
        enter(canvas)
        self._context(canvas, 200, 200)
        assert received == []

    def test_the_menu_of_a_selected_annotation(self, canvas, monkeypatch):
        """The Delete/Group/Ungroup menu is built on the spot: only its labels
        and the number of entries are checked, exec() being intercepted."""
        built = []

        class _FakeMenu:
            def __init__(self, parent=None):
                self.actions_ = []
                built.append(self)

            def addAction(self, text, slot=None):
                self.actions_.append(text)

            def exec(self, _pos):
                self.executed = True

        monkeypatch.setattr("src.ui.viewer_canvas.QMenu", _FakeMenu)
        monkeypatch.setattr("src.ui.viewer_canvas.install_menu_width_fix", lambda m: None)

        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2),
                                 _shape("B", 0.6, 0.6, 0.7, 0.7)])
        canvas.enter_annotation_mode("select")
        canvas._set_annotation_selection({"A"})
        self._context(canvas, 60, 60)
        assert len(built) == 1
        assert built[0].actions_ == ["Delete\tDel"]

        canvas._set_annotation_selection({"A", "B"})
        self._context(canvas, 60, 60)
        assert built[1].actions_ == ["Delete\tDel", "Group"]

        canvas.group_selected_annotations()
        self._context(canvas, 60, 60)
        assert built[2].actions_ == ["Delete\tDel", "Group", "Ungroup"]

    def test_no_menu_without_a_selected_annotation(self, canvas, monkeypatch):
        built = []
        monkeypatch.setattr("src.ui.viewer_canvas.QMenu",
                            lambda parent=None: built.append(1))
        canvas.set_annotations([_shape("A", 0.1, 0.1, 0.2, 0.2)])
        canvas.enter_annotation_mode("select")
        self._context(canvas, 300, 300)
        assert built == []

    def test_the_right_click_closes_the_curve_in_progress(self, canvas, qtbot):
        canvas.enter_annotation_mode("curve")
        _press(canvas, 100, 100)
        _press(canvas, 200, 150)
        with qtbot.waitSignal(canvas.annotation_added, timeout=1000) as blocker:
            self._context(canvas, 200, 150)
        assert len(blocker.args[0]["points"]) == 2

    def test_the_right_click_starts_no_annotation(self, canvas):
        canvas.enter_annotation_mode("rect")
        _press(canvas, 100, 100, button=Qt.RightButton)
        assert canvas._annotation_draft_type is None
