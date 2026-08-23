# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Canvas of the viewer (extracted from photo_viewer.py): zoom/pan display,
crop, red eyes, vignette, face addition and annotation layer
(_Canvas, ~2200 lines), with the in-place text editor (_InlineTextEdit)."""
import copy
import io
import logging
import math
import os
import uuid

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, Slot, QPoint, QRectF, QPointF, QSize, QFileInfo
from PySide6.QtGui import (
    QDesktopServices, QPixmap, QPainter, QKeyEvent, QWheelEvent,
    QMouseEvent, QPen, QBrush, QColor, QPainterPath, QPolygonF, QIcon, QFont, QTextCursor,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy, QToolButton, QButtonGroup, QMenu, QFileIconProvider, QTextEdit,
)

from src.core.models import PhotoInfo, EditInfo
from src.processing.edit_database import EditDatabase
from src.processing.adjustments import ImageAdjuster
from src.processing.annotation_geometry import catmull_rom_to_bezier_segments
from src.ui.ui_utils import install_menu_width_fix
from src.ui.annotation_renderer import (
    render_annotations, hit_test_annotations, annotation_screen_bounds,
)
from src.core.i18n import translate

logger = logging.getLogger(__name__)

# Maximum resolution for the on-screen display.
# The edits (rotation, crop, etc.) are applied to this reduced copy.

def _make_rect_quad(x0: float, y0: float, x1: float, y1: float) -> list:
    """Returns [TL, TR, BR, BL] for an axis-aligned rectangle."""
    return [QPointF(x0, y0), QPointF(x1, y0), QPointF(x1, y1), QPointF(x0, y1)]

# Crop formats: (label, tooltip, w/h ratio or None)
# The icon of each button visually shows the landscape/portrait orientation.
_CROP_FORMAT_DATA: list[tuple[str, str, float | None]] = [
    (translate("ViewerCanvas", "Free"),
     translate("ViewerCanvas", "Free form — any quadrilateral"),  None),
    ("10×15", translate("ViewerCanvas", "10×15 landscape  (3:2)"),  3 / 2),
    ("10×15", translate("ViewerCanvas", "10×15 portrait  (2:3)"),    2 / 3),
    ("13×18", translate("ViewerCanvas", "13×18 landscape  (18:13)"), 18 / 13),
    ("13×18", translate("ViewerCanvas", "13×18 portrait  (13:18)"),   13 / 18),
]

_CORNER_CURSORS = [
    Qt.SizeFDiagCursor,  # 0: TL
    Qt.SizeBDiagCursor,  # 1: TR
    Qt.SizeFDiagCursor,  # 2: BR
    Qt.SizeBDiagCursor,  # 3: BL
]
_HANDLE_HIT = 10   # pixels of tolerance to detect a corner handle
_EDGE_HIT   = 12   # pixels of tolerance to detect an edge handle
# Pairs of corners forming each edge: top, right, bottom, left
_EDGE_INDICES = [(0, 1), (1, 2), (2, 3), (3, 0)]

# Resize/rotation handles of a selected annotation
_ANNOTATION_HANDLE_HIT     = 10   # pixels of tolerance to detect a corner/rotation handle
_ANNOTATION_ROTATE_OFFSET  = 28   # pixels above the top corner for the rotation handle
_ANNOTATION_MIN_SIZE_PX    = 8.0  # minimum size (local width/height) during a resize
_ANNOTATION_CORNER_CURSORS = {
    'tl': Qt.SizeFDiagCursor, 'br': Qt.SizeFDiagCursor,
    'tr': Qt.SizeBDiagCursor, 'bl': Qt.SizeBDiagCursor,
}


class _InlineTextEdit(QTextEdit):
    """Floating text editor for the Text tool of the annotation layer.
    Enter (without Shift) validates, Escape cancels, losing the focus validates - no
    modal QInputDialog, so as to stay in direct manipulation on the canvas."""
    confirmed = Signal()
    cancelled = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            self.confirmed.emit()
            return
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.confirmed.emit()


class _Canvas(QWidget):
    zoom_changed                = Signal(float)
    wheel_navigate              = Signal(int)    # +/-1 photo
    crop_confirmed              = Signal(object) # tuple of 8 relative coords (x0,y0,...,x3,y3)
    context_menu_requested      = Signal(object) # global QPoint
    red_eye_point_added         = Signal(float, float)  # cx_norm, cy_norm (0-1)
    pixel_sampled               = Signal(int, int, int)  # R, G, B - white balance eyedropper
    face_context_menu_requested = Signal(object, object) # (FaceInfo, global QPoint)
    vignette_changed            = Signal(object) # EditInfo (geometry updated by a drag)
    face_add_confirmed          = Signal(object) # tuple (bbox_x,bbox_y,bbox_w,bbox_h) int
    annotation_added             = Signal(object)  # annotation dict added
    annotation_deleted           = Signal(str)     # id of the deleted annotation
    annotation_deleted_multi     = Signal(object)  # list[str] of deleted ids (batch deletion)
    annotation_selection_changed = Signal(object)  # list[str] of selected ids (may be empty)
    annotation_moved              = Signal(str, object)  # (id, up-to-date annotation dict)
    annotation_moved_multi        = Signal(object)  # dict[id, up-to-date annotation] (batch move)
    annotation_resized            = Signal(str, object)  # (id, up-to-date annotation dict)
    annotation_grouped            = Signal(object)  # dict[id, up-to-date annotation] (group/ungroup)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._drag_start: QPoint | None = None
        self._drag_offset_start = QPointF(0, 0)
        # Crop
        self._crop_mode   = False
        self._crop_quad:  list[QPointF] | None = None   # [TL, TR, BR, BL] screen coords
        self._crop_action: str | None   = None          # None | 'DRAWING' | 'MOVING' | 'RESIZING' | 'PANNING'
        self._crop_handle: int | None   = None          # index of the active corner (0-3)
        self._aspect_ratio: float | None = None         # locked width/height ratio (None = free)
        self._drag_ratio:   float | None = None         # effective ratio for the drag in progress
        self._crop_mouse_start:  QPointF | None = None
        self._crop_quad_start:   list[QPointF] | None = None
        self._crop_draw_start:   QPointF | None = None
        self._grid_visible = False
        # Highlighted face(s) - a single one (FacePanel click) or all of them ("All" button)
        self._highlighted_face  = None   # single FaceInfo
        self._highlighted_faces: list = []  # list for the "All" mode
        self._orig_w: int = 0
        self._orig_h: int = 0
        self._current_edit = None   # current EditInfo to transform the bboxes
        # Red-eye correction mode
        self._red_eye_mode: bool = False
        self._red_eye_radius: float = 0.03   # normalised radius (0-1) for the cursor
        self._red_eye_mouse: QPointF | None = None
        # White balance eyedropper mode
        self._wb_pick_mode: bool = False
        # Interactive vignette mode
        self._vignette_mode: bool = False
        self._vignette_edit = None           # current EditInfo of the vignette
        self._vignette_drag: str | None = None
        self._vignette_drag_start: "QPointF | None" = None
        self._vignette_edit_start = None     # copy of EditInfo at the start of the drag
        # Manual face addition mode (bbox not detected by InsightFace)
        self._face_add_mode: bool = False
        self._face_add_rect: "QRectF | None" = None     # screen, axis-aligned rectangle
        self._face_add_action: "str | None" = None      # None | 'DRAWING' | 'MOVING' | 'RESIZING'
        self._face_add_handle: "int | None" = None       # index of the active corner (0=TL,1=TR,2=BR,3=BL)
        self._face_add_draw_start: "QPointF | None" = None
        self._face_add_mouse_start: "QPointF | None" = None
        self._face_add_rect_start: "QRectF | None" = None
        # Annotation layer (drawing/text over the photo)
        self._annotation_mode: bool = False
        self._annotation_tool: str = "pen"   # "pen"|"line"|"curve"|"rect"|"ellipse"|"text"|"select"
        self._annotations_visible: bool = True
        self._annotations: list = []
        self._annotation_color: str = "#ffff0000"
        self._annotation_width: float = 0.006     # fraction of min(width, height)
        self._annotation_fill_color: str = "#ffff0000"   # rect/ellipse: background colour (alpha ignored, cf. opacity)
        self._annotation_opacity: float = 0.4             # rect/ellipse: opacity of the surface (0-1)
        self._annotation_blur: float = 0.0                 # rect/ellipse: blur, fraction of min(width, height)
        self._annotation_font_family: str = "Arial"
        self._annotation_font_size: float = 0.04  # fraction of min(width, height)
        self._annotation_bold: bool = False
        self._annotation_italic: bool = False
        self._annotation_selected_ids: set = set()            # multiple selection, "select" tool
        self._annotation_draft_type: "str | None" = None    # None|"pen"|"line"|"curve"|"rect"|"ellipse"
        self._annotation_draft_points: list = []             # screen points in progress
        self._annotation_hover_pos: "QPointF | None" = None  # preview of the next point (curve)
        self._annotation_text_editor: "_InlineTextEdit | None" = None
        self._annotation_text_pos: "QPointF | None" = None   # screen position of the open editor
        self._annotation_edit_id: "str | None" = None        # id of the text being edited (None = creation)
        # Dragging of the selected elements, "select" tool
        self._annotation_drag_ids: list = []
        self._annotation_drag_start: "QPointF | None" = None
        self._annotation_drag_origs: dict = {}                # id -> copy of the points/pos before the drag
        self._annotation_drag_moved: bool = False             # the anti-click threshold has been passed
        # Rectangular (marquee) selection in an empty area
        self._annotation_marquee_start: "QPointF | None" = None
        self._annotation_marquee_rect: "QRectF | None" = None
        # Resize/rotation through the handles of the selected element
        self._annotation_resize_handle: "str | None" = None   # 'tl'|'tr'|'br'|'bl'|'rotate'
        self._annotation_resize_id: "str | None" = None
        self._annotation_resize_orig: "dict | None" = None    # copy of the annotation before the drag
        self._annotation_resize_start: "QPointF | None" = None  # screen mouse position at the start
        self._annotation_resize_bbox0: "QRectF | None" = None   # local (unrotated) bbox at the start
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)

    # ------------------------------------------------------------------ colour eyedropper

    def start_color_pick(self) -> None:
        """Enables the eyedropper mode: the next left click -> pixel_sampled(r, g, b)."""
        self._wb_pick_mode = True
        self.setCursor(Qt.CrossCursor)

    def stop_color_pick(self) -> None:
        self._wb_pick_mode = False
        self.unsetCursor()

    # ------------------------------------------------------------------ zoom

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.zoom_fit()

    def zoom_fit(self) -> None:
        if not self._pixmap or self._pixmap.isNull():
            return
        cw, ch = self.width() or 800, self.height() or 600
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0:
            return
        self._zoom = min(cw / pw, ch / ph)
        self._center()
        self.zoom_changed.emit(self._zoom)
        self.update()

    def zoom_100(self) -> None:
        self._zoom = 1.0
        self._center()
        self.zoom_changed.emit(self._zoom)
        self.update()

    def set_zoom(self, factor: float) -> None:
        new_zoom = max(0.1, min(factor, 4.0))
        if self._pixmap and self._zoom > 0:
            # Zoom centred on the centre of the viewport: preserves the central visible point
            ratio = new_zoom / self._zoom
            cx, cy = self.width() / 2, self.height() / 2
            self._offset = QPointF(
                cx - (cx - self._offset.x()) * ratio,
                cy - (cy - self._offset.y()) * ratio,
            )
        self._zoom = new_zoom
        self.update()

    def _center(self) -> None:
        if not self._pixmap:
            return
        cw, ch = self.width(), self.height()
        pw = self._pixmap.width() * self._zoom
        ph = self._pixmap.height() * self._zoom
        self._offset = QPointF((cw - pw) / 2, (ch - ph) / 2)

    # ------------------------------------------------------------------ crop helpers

    def _frame_border_px(self) -> int:
        """Thickness of the decorative frame in the displayed pixmap (0 if there is no frame).

        The frame is part of the pixmap (laid down by ImageAdjuster.apply_all) but
        is NOT part of the image: every relative coordinate manipulated here
        (crop, annotations, vignette, face bbox) refers to the photo
        content alone, hence the removal of that border in _img_rect()."""
        if not self._pixmap:
            return 0
        edit = self._current_edit
        if edit is None or getattr(edit, "frame_type", "none") in (None, "", "none"):
            return 0
        from src.processing.frames import content_box
        x, _y, w, _h = content_box(edit, self._pixmap.width(), self._pixmap.height())
        return int(round(x)) if w > 0 else 0

    def _img_rect(self) -> QRectF:
        """Rect of the photo CONTENT on screen, in integer coordinates - identical to
        what drawPixmap really draws, minus the possible decorative frame."""
        if not self._pixmap:
            return QRectF()
        b = self._frame_border_px()
        return QRectF(
            int(self._offset.x() + b * self._zoom),
            int(self._offset.y() + b * self._zoom),
            int((self._pixmap.width()  - 2 * b) * self._zoom),
            int((self._pixmap.height() - 2 * b) * self._zoom),
        )

    def _handle_positions(self) -> dict[int, QPointF]:
        if not self._crop_quad:
            return {}
        return {i: QPointF(pt) for i, pt in enumerate(self._crop_quad)}

    def _hit_handle(self, pos: QPointF) -> int | None:
        for hid, hpos in self._handle_positions().items():
            if (abs(pos.x() - hpos.x()) <= _HANDLE_HIT and
                    abs(pos.y() - hpos.y()) <= _HANDLE_HIT):
                return hid
        return None

    def _hit_center(self, pos: QPointF) -> bool:
        if not self._crop_quad:
            return False
        cx = sum(pt.x() for pt in self._crop_quad) / 4
        cy = sum(pt.y() for pt in self._crop_quad) / 4
        xs = [pt.x() for pt in self._crop_quad]
        ys = [pt.y() for pt in self._crop_quad]
        half = max(16.0, min(max(xs) - min(xs), max(ys) - min(ys)) * 0.12)
        return abs(pos.x() - cx) <= half and abs(pos.y() - cy) <= half

    def _edge_handle_positions(self) -> dict[int, QPointF]:
        if not self._crop_quad:
            return {}
        result = {}
        for eid, (i, j) in enumerate(_EDGE_INDICES):
            a, b = self._crop_quad[i], self._crop_quad[j]
            result[eid] = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
        return result

    def _hit_edge_handle(self, pos: QPointF) -> int | None:
        for eid, mid in self._edge_handle_positions().items():
            if (abs(pos.x() - mid.x()) <= _EDGE_HIT and
                    abs(pos.y() - mid.y()) <= _EDGE_HIT):
                return eid
        return None

    def _edge_cursor(self, eid: int) -> Qt.CursorShape:
        i, j = _EDGE_INDICES[eid]
        a, b = self._crop_quad[i], self._crop_quad[j]
        return Qt.SizeVerCursor if abs(b.x() - a.x()) > abs(b.y() - a.y()) else Qt.SizeHorCursor

    def _update_cursor_for_pos(self, pos: QPointF) -> None:
        hid = self._hit_handle(pos)
        if hid is not None:
            self.setCursor(_CORNER_CURSORS[hid])
            return
        eid = self._hit_edge_handle(pos)
        if eid is not None:
            self.setCursor(self._edge_cursor(eid))
        elif self._hit_center(pos):
            self.setCursor(Qt.SizeAllCursor)
        elif self._crop_quad is not None and self._img_rect().contains(pos):
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    def _constrained_rect(self, s: QPointF, dx: float, dy: float,
                          r: float, ir: QRectF) -> list:
        """Rectangle from the starting point s, the delta (dx,dy) and the ratio r=w/h."""
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        if abs(dy) < 1 or abs(dx) / (abs(dy) + 1e-9) >= r:
            w = abs(dx)
            h = w / r
        else:
            h = abs(dy)
            w = h * r
        max_w = (ir.right() - s.x()) * sx if sx > 0 else (s.x() - ir.left())
        max_h = (ir.bottom() - s.y()) * sy if sy > 0 else (s.y() - ir.top())
        w = min(w, max_w)
        h = min(h, max_h)
        if w / r > h:
            w = h * r
        else:
            h = w / r
        x0, x1 = (s.x(), s.x() + w) if sx > 0 else (s.x() - w, s.x())
        y0, y1 = (s.y(), s.y() + h) if sy > 0 else (s.y() - h, s.y())
        return _make_rect_quad(x0, y0, x1, y1)

    def _apply_drag_corner(self, pos: QPointF) -> None:
        if self._crop_quad is None or self._crop_handle is None:
            return
        ir = self._img_rect()
        px = max(ir.left(), min(ir.right(),  pos.x()))
        py = max(ir.top(),  min(ir.bottom(), pos.y()))
        if self._drag_ratio is None:
            self._crop_quad[self._crop_handle] = QPointF(px, py)
            return
        # Locked format: the opposite corner is the anchor, we recreate the rectangle
        r   = self._drag_ratio
        opp = (self._crop_handle + 2) % 4
        fx  = self._crop_quad[opp].x()
        fy  = self._crop_quad[opp].y()
        dx, dy = px - fx, py - fy
        if abs(dx) < 1 and abs(dy) < 1:
            return
        self._crop_quad = self._constrained_rect(QPointF(fx, fy), dx, dy, r, ir)

    def _apply_drag_edge(self, pos: QPointF) -> None:
        """Moves the edge while preserving the orientation of the adjacent sides.
        Each corner slides along its adjacent side - lengthening/shortening only."""
        if self._crop_quad is None or self._crop_handle is None:
            return
        if not self._crop_quad_start or not self._crop_mouse_start:
            return
        ir  = self._img_rect()
        dx  = pos.x() - self._crop_mouse_start.x()
        dy  = pos.y() - self._crop_mouse_start.y()
        eid = self._crop_handle

        if self._drag_ratio is not None:
            # Locked format: rectangle, opposite edge anchored, ratio maintained
            r = self._drag_ratio
            xs = [pt.x() for pt in self._crop_quad_start]
            ys = [pt.y() for pt in self._crop_quad_start]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if eid == 0:            # top edge: y0 changes
                ny0 = max(ir.top(),    min(y1 - 1, y0 + dy))
                h   = y1 - ny0
                w   = min(h * r, ir.width())
                h   = w / r
                hx  = w / 2
                cx2 = max(ir.left() + hx, min(ir.right() - hx, cx))
                self._crop_quad = _make_rect_quad(cx2-hx, y1-h, cx2+hx, y1)
            elif eid == 2:          # bottom edge: y1 changes
                ny1 = max(y0 + 1,   min(ir.bottom(), y1 + dy))
                h   = ny1 - y0
                w   = min(h * r, ir.width())
                h   = w / r
                hx  = w / 2
                cx2 = max(ir.left() + hx, min(ir.right() - hx, cx))
                self._crop_quad = _make_rect_quad(cx2-hx, y0, cx2+hx, y0+h)
            elif eid == 1:          # right edge: x1 changes
                nx1 = max(x0 + 1,   min(ir.right(), x1 + dx))
                w   = nx1 - x0
                h   = min(w / r, ir.height())
                w   = h * r
                hy  = h / 2
                cy2 = max(ir.top() + hy, min(ir.bottom() - hy, cy))
                self._crop_quad = _make_rect_quad(x0, cy2-hy, x0+w, cy2+hy)
            elif eid == 3:          # left edge: x0 changes
                nx0 = max(ir.left(), min(x1 - 1, x0 + dx))
                w   = x1 - nx0
                h   = min(w / r, ir.height())
                w   = h * r
                hy  = h / 2
                cy2 = max(ir.top() + hy, min(ir.bottom() - hy, cy))
                self._crop_quad = _make_rect_quad(x1-w, cy2-hy, x1, cy2+hy)
            return

        i, j = _EDGE_INDICES[self._crop_handle]
        # Fixed adjacent corner for each end of the edge
        # In the TL(0)-TR(1)-BR(2)-BL(3) cycle, the neighbours of i (!=j) and j (!=i) are:
        i_adj = (i - 1) % 4
        j_adj = (j + 1) % 4

        q_i = self._crop_quad_start[i]       # starting position of corner i
        q_j = self._crop_quad_start[j]       # starting position of corner j
        p_i = self._crop_quad_start[i_adj]   # fixed adjacent corner of i
        p_j = self._crop_quad_start[j_adj]   # fixed adjacent corner of j

        # Normal to the edge
        ex, ey = q_j.x() - q_i.x(), q_j.y() - q_i.y()
        length = math.hypot(ex, ey)
        if length < 1:
            return
        nx, ny = -ey / length, ex / length

        # Projection of the mouse movement on the normal
        proj = dx * nx + dy * ny

        # For each corner: movement along the adjacent side
        # new_corner = q + proj * (q - p) / ((q - p) . n)
        # that is new_corner = q + proj * (sx, sy)
        dq_i_dot_n = (q_i.x() - p_i.x()) * nx + (q_i.y() - p_i.y()) * ny
        dq_j_dot_n = (q_j.x() - p_j.x()) * nx + (q_j.y() - p_j.y()) * ny
        if abs(dq_i_dot_n) < 1e-9 or abs(dq_j_dot_n) < 1e-9:
            return  # adjacent side almost parallel to the edge - degenerate case

        si_x = (q_i.x() - p_i.x()) / dq_i_dot_n
        si_y = (q_i.y() - p_i.y()) / dq_i_dot_n
        sj_x = (q_j.x() - p_j.x()) / dq_j_dot_n
        sj_y = (q_j.y() - p_j.y()) / dq_j_dot_n

        # Clamp proj so that the two new corners stay inside the image
        proj_min, proj_max = -1e9, 1e9
        for sx, sy, qx, qy in [(si_x, si_y, q_i.x(), q_i.y()),
                                 (sj_x, sj_y, q_j.x(), q_j.y())]:
            if abs(sx) > 1e-9:
                lo = (ir.left()  - qx) / sx
                hi = (ir.right() - qx) / sx
                if sx < 0:
                    lo, hi = hi, lo
                proj_min = max(proj_min, lo)
                proj_max = min(proj_max, hi)
            if abs(sy) > 1e-9:
                lo = (ir.top()    - qy) / sy
                hi = (ir.bottom() - qy) / sy
                if sy < 0:
                    lo, hi = hi, lo
                proj_min = max(proj_min, lo)
                proj_max = min(proj_max, hi)

        proj = max(proj_min, min(proj_max, proj))

        self._crop_quad[i] = QPointF(q_i.x() + proj * si_x, q_i.y() + proj * si_y)
        self._crop_quad[j] = QPointF(q_j.x() + proj * sj_x, q_j.y() + proj * sj_y)

    def _apply_move(self, pos: QPointF) -> None:
        if not self._crop_quad_start or not self._crop_mouse_start:
            return
        ir = self._img_rect()
        dx = pos.x() - self._crop_mouse_start.x()
        dy = pos.y() - self._crop_mouse_start.y()
        xs = [pt.x() for pt in self._crop_quad_start]
        ys = [pt.y() for pt in self._crop_quad_start]
        dx = max(ir.left() - min(xs), min(ir.right()  - max(xs), dx))
        dy = max(ir.top()  - min(ys), min(ir.bottom() - max(ys), dy))
        self._crop_quad = [QPointF(pt.x() + dx, pt.y() + dy) for pt in self._crop_quad_start]

    # ------------------------------------------------------------------ crop coords helpers

    def _crop_to_rel(self) -> tuple | None:
        """Returns the quad in relative coords (0-1): (x0,y0,x1,y1,x2,y2,x3,y3)."""
        if not self._crop_quad:
            return None
        ir = self._img_rect()
        if ir.width() == 0 or ir.height() == 0:
            return None
        result = []
        for pt in self._crop_quad:
            result.append((pt.x() - ir.x()) / ir.width())
            result.append((pt.y() - ir.y()) / ir.height())
        return tuple(result)

    def _crop_from_rel(self, rel: tuple) -> None:
        """Restores _crop_quad from relative coords.
        Accepts the old rectangular format (4 values x,y,w,h) or the new quad (8 values)."""
        ir = self._img_rect()
        if len(rel) == 4:
            x, y, w, h = rel
            self._crop_quad = [
                QPointF(ir.x() + x       * ir.width(), ir.y() + y       * ir.height()),  # TL
                QPointF(ir.x() + (x + w) * ir.width(), ir.y() + y       * ir.height()),  # TR
                QPointF(ir.x() + (x + w) * ir.width(), ir.y() + (y + h) * ir.height()),  # BR
                QPointF(ir.x() + x       * ir.width(), ir.y() + (y + h) * ir.height()),  # BL
            ]
        elif len(rel) == 8:
            it = iter(rel)
            self._crop_quad = [
                QPointF(ir.x() + next(it) * ir.width(), ir.y() + next(it) * ir.height())
                for _ in range(4)
            ]

    # ------------------------------------------------------------------ crop public

    def enter_crop(self, existing_crop: tuple | None = None) -> None:
        self._crop_mode        = True
        self._crop_action      = None
        self._crop_handle      = None
        self._crop_mouse_start = None
        self._crop_quad_start  = None
        self._crop_draw_start  = None
        if existing_crop:
            self._crop_from_rel(existing_crop)
        else:
            self._crop_quad = None
        self.setCursor(Qt.CrossCursor)
        self.update()

    def cancel_crop(self) -> None:
        self._crop_mode   = False
        self._crop_quad   = None
        self._crop_action = None
        self._crop_handle = None
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def set_aspect_ratio(self, ratio: float | None) -> None:
        self._aspect_ratio = ratio
        if ratio is not None and self._crop_quad:
            self._fit_rect_to_ratio(ratio)
        self.update()

    def _locked_ratio_from_quad(self) -> float | None:
        """Ratio locked for the drag in progress - uses _aspect_ratio as it is."""
        return self._aspect_ratio

    def _fit_rect_to_ratio(self, ratio: float) -> None:
        """Crops the quad to the given ratio while preserving the area (90 degree rotation on an
        orientation change, isometric resizing otherwise)."""
        if not self._crop_quad:
            return
        ir = self._img_rect()
        xs = [pt.x() for pt in self._crop_quad]
        ys = [pt.y() for pt in self._crop_quad]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        w  = max(xs) - min(xs)
        h  = max(ys) - min(ys)
        # New dimensions preserving the area: w_new*h_new = w*h and w_new/h_new = ratio
        area = w * h
        if area > 0:
            w = math.sqrt(area * ratio)
            h = math.sqrt(area / ratio)
        # Clamp to the image while re-enforcing the ratio
        w = min(w, ir.width())
        h = min(h, ir.height())
        if w / ratio > h:
            w = h * ratio
        else:
            h = w / ratio
        hx, hy = w / 2, h / 2
        cx = max(ir.left() + hx, min(ir.right()  - hx, cx))
        cy = max(ir.top()  + hy, min(ir.bottom() - hy, cy))
        self._crop_quad = _make_rect_quad(cx - hx, cy - hy, cx + hx, cy + hy)

    def confirm_crop(self) -> None:
        if not self._crop_mode or not self._crop_quad:
            self.cancel_crop()
            return
        rel = self._crop_to_rel()
        if rel is None:
            self.cancel_crop()
            return
        clamped = tuple(max(0.0, min(1.0, v)) for v in rel)
        self.cancel_crop()
        self.crop_confirmed.emit(clamped)

    # ------------------------------------------------------------------ paint

    def set_grid_visible(self, visible: bool) -> None:
        self._grid_visible = visible
        self.update()

    def _draw_grid(self, p: QPainter) -> None:
        ir = self._img_rect()
        if ir.width() < 1 or ir.height() < 1:
            return
        # Thin dotted lines: 10 regular divisions
        pen_dots = QPen(QColor(255, 255, 255, 255), 0.8, Qt.DotLine)
        p.setPen(pen_dots)
        for i in range(1, 10):
            t = i / 10
            p.drawLine(QPointF(ir.left() + t * ir.width(), ir.top()),
                       QPointF(ir.left() + t * ir.width(), ir.bottom()))
            p.drawLine(QPointF(ir.left(),  ir.top() + t * ir.height()),
                       QPointF(ir.right(), ir.top() + t * ir.height()))
        # Solid thirds lines (key alignment marks)
        p.setPen(QPen(QColor(255, 255, 255, 255), 1.2))
        for t in (1 / 3, 2 / 3):
            p.drawLine(QPointF(ir.left() + t * ir.width(), ir.top()),
                       QPointF(ir.left() + t * ir.width(), ir.bottom()))
            p.drawLine(QPointF(ir.left(),  ir.top() + t * ir.height()),
                       QPointF(ir.right(), ir.top() + t * ir.height()))

    def set_orig_size(self, w: int, h: int) -> None:
        """EXIF-corrected dimensions of the loaded image (the source of truth for the bboxes)."""
        self._orig_w = w
        self._orig_h = h

    def set_edit(self, edit) -> None:
        """Current edit to take into account for the bbox -> screen mapping."""
        self._current_edit = edit

    # ------------------------------------------------------------------ manual face addition

    def enter_face_add_mode(self) -> None:
        self._face_add_mode   = True
        self._face_add_rect   = None
        self._face_add_action = None
        self._face_add_handle = None
        self.setCursor(Qt.CrossCursor)
        self.update()

    def cancel_face_add_mode(self) -> None:
        self._face_add_mode   = False
        self._face_add_rect   = None
        self._face_add_action = None
        self._face_add_handle = None
        self.unsetCursor()
        self.update()

    def confirm_face_add(self) -> None:
        rect = self._face_add_rect
        self.cancel_face_add_mode()
        if rect is None or rect.width() < 8 or rect.height() < 8:
            return
        bbox = self._bbox_from_screen_rect(rect)
        if bbox is not None:
            self.face_add_confirmed.emit(bbox)

    def _face_add_handle_positions(self, rect: QRectF) -> dict[int, QPointF]:
        return {
            0: rect.topLeft(), 1: rect.topRight(),
            2: rect.bottomRight(), 3: rect.bottomLeft(),
        }

    def _face_add_hit_handle(self, pos: QPointF) -> "int | None":
        if self._face_add_rect is None:
            return None
        for idx, hpos in self._face_add_handle_positions(self._face_add_rect).items():
            if (abs(pos.x() - hpos.x()) <= _HANDLE_HIT and
                    abs(pos.y() - hpos.y()) <= _HANDLE_HIT):
                return idx
        return None

    def _apply_face_add_resize(self, pos: QPointF) -> None:
        if self._face_add_rect_start is None or self._face_add_handle is None:
            return
        r = self._face_add_rect_start
        ir = self._img_rect()
        x = max(ir.left(), min(ir.right(), pos.x()))
        y = max(ir.top(), min(ir.bottom(), pos.y()))
        if self._face_add_handle == 0:      # TL
            rect = QRectF(QPointF(x, y), r.bottomRight())
        elif self._face_add_handle == 1:    # TR
            rect = QRectF(QPointF(r.left(), y), QPointF(x, r.bottom()))
        elif self._face_add_handle == 2:    # BR
            rect = QRectF(r.topLeft(), QPointF(x, y))
        else:                               # BL
            rect = QRectF(QPointF(x, r.top()), QPointF(r.right(), y))
        self._face_add_rect = rect.normalized()

    def _apply_face_add_move(self, pos: QPointF) -> None:
        if self._face_add_rect_start is None or self._face_add_mouse_start is None:
            return
        ir = self._img_rect()
        r  = self._face_add_rect_start
        dx = pos.x() - self._face_add_mouse_start.x()
        dy = pos.y() - self._face_add_mouse_start.y()
        nx = max(ir.left(), min(ir.right()  - r.width(),  r.x() + dx))
        ny = max(ir.top(),  min(ir.bottom() - r.height(), r.y() + dy))
        self._face_add_rect = QRectF(nx, ny, r.width(), r.height())

    def _bbox_from_screen_rect(self, rect: QRectF) -> "tuple[int, int, int, int] | None":
        """Inverse of _face_screen_rect() for detected_rotation=0 - the only case
        relevant for a manually added face (embedding=NULL guarantees that
        detected_rotation will resolve to 0 on re-reading, cf. add_manual_face)."""
        if self._pixmap is None or self._orig_w == 0 or self._orig_h == 0:
            return None
        dw0, dh0 = float(self._orig_w), float(self._orig_h)

        # Dimensions after the rotation then the crop of the current edit (the same
        # geometric logic as _face_screen_rect, but only the sizes here).
        dw_postrot, dh_postrot = dw0, dh0
        edit = self._current_edit
        rot = 0
        cx_rel, cy_rel, cw_rel, ch_rel = 0.0, 0.0, 1.0, 1.0
        if edit is not None:
            rot = int(round(getattr(edit, "rotation", 0.0))) % 360
            if rot in (90, 270):
                dw_postrot, dh_postrot = dh0, dw0
            crop = getattr(edit, "crop", None)
            if crop and len(crop) == 4:
                cx_rel, cy_rel, cw_rel, ch_rel = crop
            elif crop and len(crop) == 8:
                xs = [crop[i] for i in range(0, 8, 2)]
                ys = [crop[i] for i in range(1, 8, 2)]
                cx_rel, cy_rel = min(xs), min(ys)
                cw_rel, ch_rel = max(xs) - cx_rel, max(ys) - cy_rel

        dw_crop = cw_rel * dw_postrot
        dh_crop = ch_rel * dh_postrot
        if dw_crop <= 0 or dh_crop <= 0 or self._zoom <= 0:
            return None

        # 1) screen -> image space (post-rotation, post-crop); _img_rect already
        #    excludes the decorative frame, which does not belong to the image.
        ir = self._img_rect()
        sx = ir.width()  / dw_crop
        sy = ir.height() / dh_crop
        if sx <= 0 or sy <= 0:
            return None
        bx = (rect.x() - ir.x()) / sx
        by = (rect.y() - ir.y()) / sy
        bw = rect.width()  / sx
        bh = rect.height() / sy

        # 2) undo crop -> post-rotation space
        bx += cx_rel * dw_postrot
        by += cy_rel * dh_postrot

        # 3) undo flip (still in post-rotation space)
        if edit is not None:
            if getattr(edit, "flip_h", False):
                bx = dw_postrot - bx - bw
            if getattr(edit, "flip_v", False):
                by = dh_postrot - by - bh

        # 4) undo edit rotation -> original EXIF-corrected space (dw0, dh0)
        if rot == 90:
            bx, by, bw, bh = by, dh0 - bx - bw, bh, bw
        elif rot == 180:
            bx, by, bw, bh = dw0 - bx - bw, dh0 - by - bh, bw, bh
        elif rot == 270:
            bx, by, bw, bh = dw0 - by - bh, bx, bh, bw

        bx = max(0.0, min(bx, dw0 - 1))
        by = max(0.0, min(by, dh0 - 1))
        bw = max(1.0, min(bw, dw0 - bx))
        bh = max(1.0, min(bh, dh0 - by))
        return int(round(bx)), int(round(by)), int(round(bw)), int(round(bh))

    # ------------------------------------------------------------------ interactive vignette

    def enter_vignette_mode(self, edit) -> None:
        self._vignette_mode = True
        self._vignette_edit = copy.copy(edit)
        self._vignette_drag = None
        self.update()

    def exit_vignette_mode(self) -> None:
        self._vignette_mode = False
        self._vignette_edit = None
        self._vignette_drag = None
        self.update()

    def update_vignette(self, edit) -> None:
        if self._vignette_mode:
            self._vignette_edit = copy.copy(edit)
            self.update()

    def _vignette_handle_positions(self) -> dict:
        if not self._vignette_edit or not self._pixmap:
            return {}
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            return {}
        e = self._vignette_edit
        cx_s  = ir.x() + e.vignette_cx * ir.width()
        cy_s  = ir.y() + e.vignette_cy * ir.height()
        rx1_s = e.vignette_rx1 * ir.width()  / 2.0
        ry1_s = e.vignette_ry1 * ir.height() / 2.0
        rx2_s = e.vignette_rx2 * ir.width()  / 2.0
        ry2_s = e.vignette_ry2 * ir.height() / 2.0
        rad   = math.radians(e.vignette_angle)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)

        def rot(ldx, ldy):
            return (ldx * cos_a - ldy * sin_a, ldx * sin_a + ldy * cos_a)

        def s(ldx, ldy):
            rdx, rdy = rot(ldx, ldy)
            return QPointF(cx_s + rdx, cy_s + rdy)

        return {
            'center':  QPointF(cx_s, cy_s),
            'inner_n': s(0,      -ry1_s),
            'inner_s': s(0,      +ry1_s),
            'inner_e': s(+rx1_s, 0),
            'inner_w': s(-rx1_s, 0),
            'outer_n': s(0,      -ry2_s),
            'outer_s': s(0,      +ry2_s),
            'outer_e': s(+rx2_s, 0),
            'outer_w': s(-rx2_s, 0),
            'rotate':  s(0,      -(ry2_s + 28)),
        }

    def _vignette_hit_test(self, pos: QPointF) -> "str | None":
        HIT_R = 12
        for name, hpos in self._vignette_handle_positions().items():
            if math.hypot(pos.x() - hpos.x(), pos.y() - hpos.y()) <= HIT_R:
                return name
        return None

    def _vignette_update_drag(self, pos: QPointF) -> None:
        if not self._vignette_drag or not self._vignette_edit_start or not self._vignette_drag_start:
            return
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            return

        e0  = self._vignette_edit_start
        dx  = pos.x() - self._vignette_drag_start.x()
        dy  = pos.y() - self._vignette_drag_start.y()
        rad = math.radians(e0.vignette_angle)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        handle = self._vignette_drag
        e = copy.copy(e0)

        if handle == 'center':
            e.vignette_cx = max(0.0, min(1.0, e0.vignette_cx + dx / ir.width()))
            e.vignette_cy = max(0.0, min(1.0, e0.vignette_cy + dy / ir.height()))
        elif handle == 'inner_n':
            proj = dx * sin_a + dy * (-cos_a)
            e.vignette_ry1 = max(0.05, e0.vignette_ry1 + proj / (ir.height() / 2.0))
        elif handle == 'inner_s':
            proj = dx * (-sin_a) + dy * cos_a
            e.vignette_ry1 = max(0.05, e0.vignette_ry1 + proj / (ir.height() / 2.0))
        elif handle == 'inner_e':
            proj = dx * cos_a + dy * sin_a
            e.vignette_rx1 = max(0.05, e0.vignette_rx1 + proj / (ir.width() / 2.0))
        elif handle == 'inner_w':
            proj = dx * (-cos_a) + dy * (-sin_a)
            e.vignette_rx1 = max(0.05, e0.vignette_rx1 + proj / (ir.width() / 2.0))
        elif handle == 'outer_n':
            proj = dx * sin_a + dy * (-cos_a)
            e.vignette_ry2 = max(0.05, e0.vignette_ry2 + proj / (ir.height() / 2.0))
        elif handle == 'outer_s':
            proj = dx * (-sin_a) + dy * cos_a
            e.vignette_ry2 = max(0.05, e0.vignette_ry2 + proj / (ir.height() / 2.0))
        elif handle == 'outer_e':
            proj = dx * cos_a + dy * sin_a
            e.vignette_rx2 = max(0.05, e0.vignette_rx2 + proj / (ir.width() / 2.0))
        elif handle == 'outer_w':
            proj = dx * (-cos_a) + dy * (-sin_a)
            e.vignette_rx2 = max(0.05, e0.vignette_rx2 + proj / (ir.width() / 2.0))
        elif handle == 'rotate':
            cx_s = ir.x() + e0.vignette_cx * ir.width()
            cy_s = ir.y() + e0.vignette_cy * ir.height()
            a0 = math.degrees(math.atan2(
                self._vignette_drag_start.y() - cy_s,
                self._vignette_drag_start.x() - cx_s))
            ac = math.degrees(math.atan2(pos.y() - cy_s, pos.x() - cx_s))
            e.vignette_angle = (e0.vignette_angle + ac - a0) % 360.0

        self._vignette_edit = e
        self.vignette_changed.emit(copy.copy(e))
        self.update()

    def _draw_vignette_overlay(self, p: QPainter) -> None:
        if not self._vignette_edit or not self._pixmap:
            return
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            return
        e = self._vignette_edit
        cx_s  = ir.x() + e.vignette_cx * ir.width()
        cy_s  = ir.y() + e.vignette_cy * ir.height()
        rx1_s = e.vignette_rx1 * ir.width()  / 2.0
        ry1_s = e.vignette_ry1 * ir.height() / 2.0
        rx2_s = e.vignette_rx2 * ir.width()  / 2.0
        ry2_s = e.vignette_ry2 * ir.height() / 2.0

        # Inner ellipse - yellow dots
        p.save()
        p.translate(cx_s, cy_s)
        p.rotate(e.vignette_angle)
        p.setPen(QPen(QColor(255, 200, 0, 210), 1.5, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(0, 0), rx1_s, ry1_s)
        p.restore()

        # Outer ellipse - solid yellow line
        p.save()
        p.translate(cx_s, cy_s)
        p.rotate(e.vignette_angle)
        p.setPen(QPen(QColor(255, 200, 0, 210), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(0, 0), rx2_s, ry2_s)
        p.restore()

        handles = self._vignette_handle_positions()

        # Rotation stem (outer_n -> rotate)
        outer_n = handles.get('outer_n')
        rot_h   = handles.get('rotate')
        if outer_n and rot_h:
            p.setPen(QPen(QColor(255, 200, 0, 130), 1, Qt.DotLine))
            p.drawLine(outer_n, rot_h)

        HS = 6
        for name, hpos in handles.items():
            active = (name == self._vignette_drag)
            fill   = QColor(255, 200, 0, 230 if active else 160)
            border = QPen(QColor(160, 120, 0, 230), 1.5)

            if name == 'center':
                p.setPen(QPen(QColor(255, 200, 0, 210), 1.5))
                p.setBrush(Qt.NoBrush)
                arm = 10
                p.drawLine(QPointF(hpos.x()-arm, hpos.y()), QPointF(hpos.x()+arm, hpos.y()))
                p.drawLine(QPointF(hpos.x(), hpos.y()-arm), QPointF(hpos.x(), hpos.y()+arm))
                p.drawEllipse(hpos, 4.0, 4.0)
            elif name == 'rotate':
                p.setPen(border)
                p.setBrush(fill)
                p.drawEllipse(hpos, HS + 1, HS + 1)
            elif name.startswith('inner_'):
                p.setPen(border)
                p.setBrush(fill)
                p.drawEllipse(hpos, HS, HS)
            else:  # outer_n/s/e/w
                p.setPen(border)
                p.setBrush(fill)
                p.drawRect(QRectF(hpos.x()-HS, hpos.y()-HS, HS*2, HS*2))

    # ------------------------------------------------------------------ red-eye mode

    def enter_red_eye_mode(self, radius: float = 0.03) -> None:
        self._red_eye_mode = True
        self._red_eye_radius = max(0.005, radius)
        self._red_eye_mouse = None
        self.setCursor(Qt.CrossCursor)
        self.update()

    def exit_red_eye_mode(self) -> None:
        self._red_eye_mode = False
        self._red_eye_mouse = None
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def set_red_eye_radius(self, radius: float) -> None:
        self._red_eye_radius = max(0.005, radius)
        self.update()

    def _red_eye_screen_radius(self) -> float:
        """Radius of the red-eye cursor in screen pixels."""
        if not self._pixmap:
            return 20.0
        ir = self._img_rect()   # radius relative to the photo content, frame excluded
        return self._red_eye_radius * min(ir.width(), ir.height())

    def _draw_red_eye_overlay(self, p: QPainter) -> None:
        if not self._red_eye_mouse or not self._pixmap:
            return
        r = self._red_eye_screen_radius()
        center = self._red_eye_mouse
        p.setPen(QPen(QColor(220, 60, 60, 200), 1.5))
        p.setBrush(QColor(220, 60, 60, 40))
        p.drawEllipse(center, r, r)
        # Crosshair
        p.setPen(QPen(QColor(220, 60, 60, 160), 1))
        arm = r * 0.6
        p.drawLine(QPointF(center.x() - arm, center.y()), QPointF(center.x() + arm, center.y()))
        p.drawLine(QPointF(center.x(), center.y() - arm), QPointF(center.x(), center.y() + arm))

    # ------------------------------------------------------------------ annotation layer

    def enter_annotation_mode(self, tool: str = "pen") -> None:
        self._annotation_mode = True
        self._annotation_tool = tool
        self._annotation_selected_ids = set()
        self.cancel_annotation_draft()
        self._cancel_annotation_drag()
        self._cancel_annotation_resize()
        self._cancel_annotation_marquee()
        self.setCursor(Qt.ArrowCursor if tool == "select" else Qt.CrossCursor)
        self.update()

    def exit_annotation_mode(self) -> None:
        self._annotation_mode = False
        self.cancel_annotation_draft()
        self._cancel_annotation_drag()
        self._cancel_annotation_resize()
        self._cancel_annotation_marquee()
        if self._annotation_text_editor is not None:
            self.cancel_text_edit()
        self._annotation_selected_ids = set()
        self.unsetCursor()
        self.update()

    def set_annotation_tool(self, tool: str) -> None:
        self._annotation_tool = tool
        self.cancel_annotation_draft()
        self._cancel_annotation_drag()
        self._cancel_annotation_resize()
        self._cancel_annotation_marquee()
        if self._annotation_text_editor is not None:
            self.cancel_text_edit()
        self.setCursor(Qt.ArrowCursor if tool == "select" else Qt.CrossCursor)
        self.update()

    def _cancel_annotation_drag(self) -> None:
        self._annotation_drag_ids   = []
        self._annotation_drag_start = None
        self._annotation_drag_origs = {}
        self._annotation_drag_moved = False

    def _cancel_annotation_marquee(self) -> None:
        self._annotation_marquee_start = None
        self._annotation_marquee_rect  = None

    def _set_annotation_selection(self, ids) -> None:
        self._annotation_selected_ids = set(ids)
        self.annotation_selection_changed.emit(sorted(self._annotation_selected_ids))

    def _cancel_annotation_resize(self) -> None:
        self._annotation_resize_handle = None
        self._annotation_resize_id     = None
        self._annotation_resize_orig   = None
        self._annotation_resize_start  = None
        self._annotation_resize_bbox0  = None

    def _update_annotation_drag(self, pos: QPointF) -> None:
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0 or self._annotation_drag_start is None:
            return
        dx_screen = pos.x() - self._annotation_drag_start.x()
        dy_screen = pos.y() - self._annotation_drag_start.y()
        if not self._annotation_drag_moved:
            if math.hypot(dx_screen, dy_screen) < 3.0:
                return
            self._annotation_drag_moved = True
        dx = dx_screen / ir.width()
        dy = dy_screen / ir.height()
        for ann_id in self._annotation_drag_ids:
            ann = self._find_annotation(ann_id)
            orig = self._annotation_drag_origs.get(ann_id)
            if ann is None or orig is None:
                continue
            if "points" in orig:
                ann["points"] = [[px + dx, py + dy] for px, py in orig["points"]]
            elif "pos" in orig:
                ox, oy = orig["pos"]
                ann["pos"] = [ox + dx, oy + dy]
        # forced repaint() (not update()): on Windows, a fast flow of
        # native WM_MOUSEMOVE can starve the asynchronous repaint queue of Qt,
        # which gave the impression that the original element stayed displayed
        # (a "ghost") until the click was released.
        self.repaint()

    def _finish_annotation_drag(self) -> None:
        ann_ids = list(self._annotation_drag_ids)
        moved   = self._annotation_drag_moved
        updated = {
            i: copy.deepcopy(self._find_annotation(i)) for i in ann_ids
            if self._find_annotation(i) is not None
        }
        self._cancel_annotation_drag()
        self.setCursor(Qt.ArrowCursor)
        if moved and updated:
            if len(updated) == 1:
                (ann_id, ann), = updated.items()
                self.annotation_moved.emit(ann_id, ann)
            else:
                self.annotation_moved_multi.emit(updated)
        self.update()

    def _finish_annotation_marquee(self, event: QMouseEvent) -> None:
        rect = self._annotation_marquee_rect
        self._cancel_annotation_marquee()
        if rect is None or (rect.width() < 3 and rect.height() < 3):
            self.update()
            return
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            self.update()
            return
        local_rect = QRectF(rect.x() - ir.x(), rect.y() - ir.y(), rect.width(), rect.height())
        hits = {
            a.get("id") for a in self._annotations
            if local_rect.intersects(annotation_screen_bounds(a, ir.width(), ir.height()))
        }
        if bool(event.modifiers() & Qt.ControlModifier):
            self._set_annotation_selection(self._annotation_selected_ids ^ hits)
        else:
            self._set_annotation_selection(hits)
        self.update()

    def _annotation_bbox_local(self, ann: dict) -> "QRectF | None":
        """Screen bbox of ``ann`` in its local unrotated frame (before ``angle``),
        origin (0,0) = corner of the displayed image. Serves as a fixed base for the handles:
        the centre of that bbox stays the rotation pivot whatever ``angle`` is."""
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            return None
        rect = annotation_screen_bounds(ann, ir.width(), ir.height())
        if rect.isEmpty():
            return None
        return rect

    def _annotation_handle_positions(self, ann: dict) -> dict:
        """Screen positions (widget frame) of the 4 corner handles + the rotation
        handle, rotated by ``ann['angle']`` around the centre of the local bbox -
        the same transformation as the one applied at render time (render_annotations)."""
        rect = self._annotation_bbox_local(ann)
        if rect is None:
            return {}
        ir = self._img_rect()
        rad = math.radians(float(ann.get("angle", 0.0) or 0.0))
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        cx, cy = rect.center().x(), rect.center().y()

        def rot(lx, ly):
            dx, dy = lx - cx, ly - cy
            return QPointF(ir.x() + cx + dx * cos_a - dy * sin_a,
                           ir.y() + cy + dx * sin_a + dy * cos_a)

        return {
            'tl':     rot(rect.left(),  rect.top()),
            'tr':     rot(rect.right(), rect.top()),
            'br':     rot(rect.right(), rect.bottom()),
            'bl':     rot(rect.left(),  rect.bottom()),
            'rotate': rot(cx, rect.top() - _ANNOTATION_ROTATE_OFFSET),
        }

    def _annotation_hit_handle(self, ann: dict, pos: QPointF) -> "str | None":
        for name, hpos in self._annotation_handle_positions(ann).items():
            if math.hypot(pos.x() - hpos.x(), pos.y() - hpos.y()) <= _ANNOTATION_HANDLE_HIT:
                return name
        return None

    def _start_annotation_resize(self, ann: dict, handle: str, pos: QPointF) -> None:
        bbox0 = self._annotation_bbox_local(ann)
        if bbox0 is None:
            return
        self._annotation_resize_handle = handle
        self._annotation_resize_id     = ann.get("id")
        self._annotation_resize_orig   = copy.deepcopy(ann)
        self._annotation_resize_start  = QPointF(pos)
        self._annotation_resize_bbox0  = QRectF(bbox0)
        cursor = Qt.ClosedHandCursor if handle == 'rotate' else _ANNOTATION_CORNER_CURSORS.get(handle, Qt.SizeFDiagCursor)
        self.setCursor(cursor)

    def _update_annotation_resize(self, pos: QPointF) -> None:
        ir = self._img_rect()
        orig  = self._annotation_resize_orig
        bbox0 = self._annotation_resize_bbox0
        ann   = self._find_annotation(self._annotation_resize_id) if self._annotation_resize_id else None
        if ir.width() <= 0 or ir.height() <= 0 or orig is None or bbox0 is None or ann is None:
            return
        handle = self._annotation_resize_handle
        angle0 = float(orig.get("angle", 0.0) or 0.0)
        rad = math.radians(angle0)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        cx0, cy0 = bbox0.center().x(), bbox0.center().y()

        # Mouse position brought back into the local unrotated frame (image origin, without the ir offset)
        lx, ly = pos.x() - ir.x(), pos.y() - ir.y()
        dx, dy = lx - cx0, ly - cy0
        local_x = cx0 + dx * cos_a + dy * sin_a
        local_y = cy0 - dx * sin_a + dy * cos_a

        if handle == 'rotate':
            sx = self._annotation_resize_start.x() - ir.x()
            sy = self._annotation_resize_start.y() - ir.y()
            a0 = math.degrees(math.atan2(sy - cy0, sx - cx0))
            ac = math.degrees(math.atan2(ly - cy0, lx - cx0))
            ann["angle"] = (angle0 + ac - a0) % 360.0
            self.update()
            return

        anchors = {
            'tl': (bbox0.right(), bbox0.bottom()),
            'tr': (bbox0.left(),  bbox0.bottom()),
            'br': (bbox0.left(),  bbox0.top()),
            'bl': (bbox0.right(), bbox0.top()),
        }
        ax, ay = anchors[handle]
        old_w = max(1e-6, bbox0.width())
        old_h = max(1e-6, bbox0.height())
        new_w = max(_ANNOTATION_MIN_SIZE_PX, abs(local_x - ax))
        new_h = max(_ANNOTATION_MIN_SIZE_PX, abs(local_y - ay))
        scale_x = new_w / old_w
        scale_y = new_h / old_h

        if ann.get("type") == "text":
            # Text has no independent width/height: a single scale
            # factor (distance to the anchor) governs font_size, a natural "diagonal" gesture.
            old_diag = math.hypot(old_w, old_h)
            new_diag = math.hypot(new_w, new_h)
            scale = new_diag / max(1e-6, old_diag)
            ann["font_size"] = max(0.005, min(1.0, float(orig.get("font_size", 0.04)) * scale))
        else:
            orig_pts = orig.get("points") or []
            new_pts = []
            for px, py in orig_pts:
                lx0 = px * ir.width()
                ly0 = py * ir.height()
                nlx = ax + (lx0 - ax) * scale_x
                nly = ay + (ly0 - ay) * scale_y
                new_pts.append([nlx / ir.width(), nly / ir.height()])
            ann["points"] = new_pts
            if "width" in orig:
                ann["width"] = max(0.0005, float(orig.get("width", 0.004)) * ((scale_x + scale_y) / 2))

        self.update()

    def _finish_annotation_resize(self) -> None:
        ann_id  = self._annotation_resize_id
        handle  = self._annotation_resize_handle
        ann     = self._find_annotation(ann_id) if ann_id is not None else None
        self._cancel_annotation_resize()
        self.setCursor(Qt.ArrowCursor)
        if handle is not None and ann is not None:
            self.annotation_resized.emit(ann_id, copy.deepcopy(ann))
        self.update()

    def set_annotation_style(self, color: str, width: float, font_family: str,
                              font_size: float, bold: bool, italic: bool,
                              fill_color: str = "#40ff0000", opacity: float = 1.0,
                              blur: float = 0.0) -> None:
        self._annotation_color = color
        self._annotation_width = width
        self._annotation_font_family = font_family
        self._annotation_font_size = font_size
        self._annotation_bold = bold
        self._annotation_italic = italic
        self._annotation_fill_color = fill_color
        self._annotation_opacity = opacity
        self._annotation_blur = blur

    def set_annotations(self, annotations: list) -> None:
        self._annotations = annotations if annotations is not None else []
        stale = {i for i in self._annotation_selected_ids if self._find_annotation(i) is None}
        if stale:
            self._annotation_selected_ids -= stale
        if any(self._find_annotation(i) is None for i in self._annotation_drag_ids):
            self._cancel_annotation_drag()
        if (self._annotation_resize_id is not None
                and self._find_annotation(self._annotation_resize_id) is None):
            self._cancel_annotation_resize()
        self.update()

    def set_annotations_visible(self, visible: bool) -> None:
        self._annotations_visible = visible
        self.update()

    def _find_annotation(self, ann_id: str) -> "dict | None":
        for ann in self._annotations:
            if ann.get("id") == ann_id:
                return ann
        return None

    def delete_selected_annotation(self) -> list:
        ids = list(self._annotation_selected_ids)
        if not ids:
            return []
        id_set = set(ids)
        self._annotations = [a for a in self._annotations if a.get("id") not in id_set]
        self._annotation_selected_ids = set()
        if id_set & set(self._annotation_drag_ids):
            self._cancel_annotation_drag()
        if self._annotation_resize_id in id_set:
            self._cancel_annotation_resize()
        if len(ids) == 1:
            self.annotation_deleted.emit(ids[0])
        else:
            self.annotation_deleted_multi.emit(ids)
        self.annotation_selection_changed.emit([])
        self.update()
        return ids

    def clear_all_annotations(self) -> None:
        self._annotations = []
        self._annotation_selected_ids = set()
        self._cancel_annotation_drag()
        self._cancel_annotation_resize()
        self.annotation_selection_changed.emit([])
        self.update()

    def group_selected_annotations(self) -> None:
        ids = list(self._annotation_selected_ids)
        if len(ids) < 2:
            return
        group_id = uuid.uuid4().hex
        updated = {}
        for ann_id in ids:
            ann = self._find_annotation(ann_id)
            if ann is not None:
                ann["group"] = group_id
                updated[ann_id] = copy.deepcopy(ann)
        if updated:
            self.annotation_grouped.emit(updated)
        self.update()

    def ungroup_selected_annotations(self) -> None:
        groups = {
            a.get("group") for a in self._annotations
            if a.get("id") in self._annotation_selected_ids and a.get("group")
        }
        if not groups:
            return
        updated = {}
        for ann in self._annotations:
            if ann.get("group") in groups:
                ann["group"] = None
                updated[ann.get("id")] = copy.deepcopy(ann)
        if updated:
            self.annotation_grouped.emit(updated)
        self.update()

    def cancel_annotation_draft(self) -> None:
        self._annotation_draft_points = []
        self._annotation_draft_type = None
        self._annotation_hover_pos = None
        self.update()

    def confirm_annotation_draft(self) -> None:
        pts = self._annotation_draft_points
        draft_type = self._annotation_draft_type
        self._annotation_draft_points = []
        self._annotation_draft_type = None
        self._annotation_hover_pos = None
        if not draft_type or len(pts) < 2:
            self.update()
            return
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            self.update()
            return
        norm_pts = [
            [max(0.0, min(1.0, (pt.x() - ir.x()) / ir.width())),
             max(0.0, min(1.0, (pt.y() - ir.y()) / ir.height()))]
            for pt in pts
        ]
        ann = {
            "id": str(uuid.uuid4()),
            "type": draft_type,
            "color": self._annotation_color,
            "width": self._annotation_width,
            "points": norm_pts,
        }
        if draft_type in ("rect", "ellipse"):
            ann["fill_color"] = self._annotation_fill_color
            ann["opacity"] = self._annotation_opacity
            ann["blur"] = self._annotation_blur
        self._annotations.append(ann)
        self.annotation_added.emit(ann)
        self.update()

    def _open_text_editor(self, pos: QPointF, ann: "dict | None" = None) -> None:
        editor = _InlineTextEdit(self)
        editor.setAcceptRichText(False)
        w, h = 220, 60
        x = min(max(0, int(pos.x())), max(0, self.width()  - w))
        y = min(max(0, int(pos.y())), max(0, self.height() - h))
        editor.setGeometry(x, y, w, h)
        ir = self._img_rect()
        scale = min(ir.width(), ir.height()) if ir.width() > 0 and ir.height() > 0 else 0
        font_family = ann.get("font_family", self._annotation_font_family) if ann else self._annotation_font_family
        font_size   = ann.get("font_size",   self._annotation_font_size)   if ann else self._annotation_font_size
        bold        = ann.get("bold",        self._annotation_bold)       if ann else self._annotation_bold
        italic      = ann.get("italic",      self._annotation_italic)     if ann else self._annotation_italic
        color_val   = ann.get("color",       self._annotation_color)      if ann else self._annotation_color
        px = max(8, round(float(font_size) * scale)) if scale else 16
        font = QFont(font_family or "Arial")
        font.setPixelSize(px)
        font.setBold(bool(bold))
        font.setItalic(bool(italic))
        editor.setFont(font)
        color = QColor(color_val)
        editor.setStyleSheet(
            f"QTextEdit {{ background: rgba(255,255,255,235); color: {color.name()}; "
            f"border: 1px solid #4a9fd4; }}"
        )
        if ann is not None:
            editor.setPlainText(ann.get("text", ""))
            editor.moveCursor(QTextCursor.End)
        editor.confirmed.connect(self._on_text_edit_confirmed)
        editor.cancelled.connect(self._on_text_edit_cancelled)
        editor.show()
        editor.setFocus(Qt.MouseFocusReason)
        self._annotation_text_editor = editor
        self._annotation_text_pos = pos
        self._annotation_edit_id = ann.get("id") if ann is not None else None

    def _open_text_editor_for_edit(self, ann: dict) -> None:
        """Double-click on an existing text (select tool): reopens the prefilled
        floating editor, in place of the annotation, for an in-place modification."""
        if self._annotation_text_editor is not None:
            return
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            return
        ann_pos = ann.get("pos", [0.0, 0.0])
        pos = QPointF(ir.x() + ann_pos[0] * ir.width(), ir.y() + ann_pos[1] * ir.height())
        self._open_text_editor(pos, ann=ann)

    def _on_text_edit_confirmed(self) -> None:
        editor = self._annotation_text_editor
        pos = self._annotation_text_pos
        edit_id = self._annotation_edit_id
        self._annotation_text_editor = None
        self._annotation_text_pos = None
        self._annotation_edit_id = None
        if editor is None:
            return
        text = editor.toPlainText().strip()
        editor.deleteLater()
        if edit_id is not None:
            ann = self._find_annotation(edit_id)
            if ann is None:
                self.update()
                return
            if not text:
                self._annotations = [a for a in self._annotations if a.get("id") != edit_id]
                self.annotation_deleted.emit(edit_id)
                self.update()
                return
            ann["text"] = text
            self.annotation_moved.emit(edit_id, copy.deepcopy(ann))
            self.update()
            return
        if not text or pos is None:
            self.update()
            return
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            self.update()
            return
        ann = {
            "id": str(uuid.uuid4()),
            "type": "text",
            "text": text,
            "color": self._annotation_color,
            "font_family": self._annotation_font_family,
            "font_size": self._annotation_font_size,
            "bold": self._annotation_bold,
            "italic": self._annotation_italic,
            "pos": [
                max(0.0, min(1.0, (pos.x() - ir.x()) / ir.width())),
                max(0.0, min(1.0, (pos.y() - ir.y()) / ir.height())),
            ],
        }
        self._annotations.append(ann)
        self.annotation_added.emit(ann)
        self.update()

    def cancel_text_edit(self) -> None:
        editor = self._annotation_text_editor
        self._annotation_text_editor = None
        self._annotation_text_pos = None
        self._annotation_edit_id = None
        if editor is not None:
            editor.deleteLater()
        self.update()

    def _on_text_edit_cancelled(self) -> None:
        self.cancel_text_edit()

    def _draw_annotation_overlay(self, p: QPainter) -> None:
        if not self._annotations:
            return
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            return
        # The blur samples the background assuming it covers exactly the
        # target area: with a frame, it must therefore be given the content alone.
        # (cropping only if an annotation really blurs - the copy has
        #  a cost, and paintEvent goes through here at every refresh)
        b = self._frame_border_px()
        background = self._pixmap
        if b > 0 and any(float(a.get("blur", 0.0) or 0.0) >= 0.5 for a in self._annotations):
            background = self._pixmap.copy(
                b, b, self._pixmap.width() - 2 * b, self._pixmap.height() - 2 * b)
        p.save()
        p.translate(ir.x(), ir.y())
        render_annotations(p, self._annotations, ir.width(), ir.height(), background=background)
        p.restore()

    def _draw_annotation_tool_overlay(self, p: QPainter) -> None:
        ir = self._img_rect()
        if ir.width() <= 0 or ir.height() <= 0:
            return
        for ann_id in self._annotation_selected_ids:
            ann = self._find_annotation(ann_id)
            if ann is None:
                continue
            p.save()
            p.translate(ir.x(), ir.y())
            rect = annotation_screen_bounds(ann, ir.width(), ir.height())
            angle = float(ann.get("angle", 0.0) or 0.0)
            if angle:
                center = rect.center()
                p.translate(center)
                p.rotate(angle)
                p.translate(-center)
            p.setPen(QPen(QColor(255, 210, 60), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)
            p.restore()
        if len(self._annotation_selected_ids) == 1 and self._annotation_tool == "select":
            ann = self._find_annotation(next(iter(self._annotation_selected_ids)))
            if ann is not None:
                self._draw_annotation_handles(p, ann)
        if self._annotation_marquee_rect is not None:
            p.save()
            p.setPen(QPen(QColor(80, 170, 255), 1, Qt.DashLine))
            p.setBrush(QColor(80, 170, 255, 40))
            p.drawRect(self._annotation_marquee_rect)
            p.restore()
        if self._annotation_draft_type in ("rect", "ellipse") and len(self._annotation_draft_points) >= 2:
            p.save()
            raw_width = self._annotation_width * min(ir.width(), ir.height())
            width_px = max(1.0, raw_width) if self._annotation_width > 0 else 0.0
            rect = QRectF(self._annotation_draft_points[0], self._annotation_draft_points[1]).normalized()
            opacity = max(0.0, min(1.0, self._annotation_opacity))
            if opacity > 0.0:
                fill = QColor(self._annotation_fill_color)
                fill.setAlpha(round(255 * opacity))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(fill))
                if self._annotation_draft_type == "ellipse":
                    p.drawEllipse(rect)
                else:
                    p.drawRect(rect)
            if width_px > 0:
                p.setPen(QPen(QColor(self._annotation_color), width_px))
                p.setBrush(Qt.NoBrush)
                if self._annotation_draft_type == "ellipse":
                    p.drawEllipse(rect)
                else:
                    p.drawRect(rect)
            p.restore()
        elif self._annotation_draft_type and self._annotation_draft_points:
            p.save()
            color = QColor(self._annotation_color)
            width_px = max(1.0, self._annotation_width * min(ir.width(), ir.height()))
            p.setPen(QPen(color, width_px, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            pts = list(self._annotation_draft_points)
            if self._annotation_draft_type == "curve" and self._annotation_hover_pos is not None:
                pts = pts + [self._annotation_hover_pos]
            if len(pts) == 1:
                p.setBrush(color)
                p.drawEllipse(pts[0], width_px / 2, width_px / 2)
            else:
                path = QPainterPath()
                path.moveTo(pts[0])
                if self._annotation_draft_type == "curve":
                    segs = catmull_rom_to_bezier_segments([(pt.x(), pt.y()) for pt in pts])
                    for _p0, cp1, cp2, p3 in segs:
                        path.cubicTo(QPointF(*cp1), QPointF(*cp2), QPointF(*p3))
                else:
                    for pt in pts[1:]:
                        path.lineTo(pt)
                p.drawPath(path)
            p.restore()

    def _draw_annotation_handles(self, p: QPainter, ann: dict) -> None:
        """Resize (corner) + rotation handles of a selected element,
        the same visual style as the vignette handles (_vignette_handle_positions)."""
        handles = self._annotation_handle_positions(ann)
        if not handles:
            return
        rotate_h = handles.get('rotate')
        tl, tr = handles.get('tl'), handles.get('tr')
        if rotate_h and tl and tr:
            top_mid = QPointF((tl.x() + tr.x()) / 2, (tl.y() + tr.y()) / 2)
            p.setPen(QPen(QColor(255, 210, 60, 160), 1, Qt.DotLine))
            p.drawLine(top_mid, rotate_h)
        HS = 5
        for name, hpos in handles.items():
            active = (name == self._annotation_resize_handle)
            fill   = QColor(255, 210, 60, 230 if active else 170)
            border = QPen(QColor(150, 110, 0, 230), 1.5)
            p.setPen(border)
            p.setBrush(fill)
            if name == 'rotate':
                p.drawEllipse(hpos, HS + 1, HS + 1)
            else:
                p.drawRect(QRectF(hpos.x() - HS, hpos.y() - HS, HS * 2, HS * 2))

    def set_highlighted_face(self, face) -> None:
        self._highlighted_face  = face
        self._highlighted_faces = []
        self.update()

    def set_highlighted_faces(self, faces: list) -> None:
        self._highlighted_faces = list(faces)
        self._highlighted_face  = None
        self.update()

    def _face_screen_rect(self, face=None) -> "QRectF | None":
        f = face if face is not None else self._highlighted_face
        if f is None or self._pixmap is None or self._orig_w == 0 or self._orig_h == 0:
            return None
        # bbox stored in the space of the image after an extra detected_rotation CW.
        # We bring it back into the display space (EXIF-corrected image, without the extra rotation).
        bx, by, bw, bh = float(f.bbox_x), float(f.bbox_y), float(f.bbox_w), float(f.bbox_h)
        r = getattr(f, "detected_rotation", 0) % 360
        dw, dh = float(self._orig_w), float(self._orig_h)
        if r == 90:
            bx, by, bw, bh = by, dh - bx - bw, bh, bw
        elif r == 180:
            bx, by, bw, bh = dw - bx - bw, dh - by - bh, bw, bh
        elif r == 270:
            bx, by, bw, bh = dw - by - bh, bx, bh, bw

        # Apply the geometric transformations of the edit (the same order as apply_all):
        # rotation CW -> straighten (ignored, small angle) -> flip -> crop
        edit = self._current_edit
        if edit is not None:
            rot = int(round(getattr(edit, "rotation", 0.0))) % 360
            if rot == 90:
                bx, by, bw, bh = dh - by - bh, bx, bh, bw
                dw, dh = dh, dw
            elif rot == 180:
                bx, by, bw, bh = dw - bx - bw, dh - by - bh, bw, bh
            elif rot == 270:
                bx, by, bw, bh = by, dw - bx - bw, bh, bw
                dw, dh = dh, dw

            if getattr(edit, "flip_h", False):
                bx = dw - bx - bw
            if getattr(edit, "flip_v", False):
                by = dh - by - bh

            crop = getattr(edit, "crop", None)
            if crop and len(crop) == 4:
                cx, cy, cw, ch = crop
                bx = bx - cx * dw
                by = by - cy * dh
                dw = cw * dw
                dh = ch * dh
            elif crop and len(crop) == 8:
                # TL,TR,BR,BL quad format (relative coords 0-1)
                xs = [crop[i] for i in range(0, 8, 2)]
                ys = [crop[i] for i in range(1, 8, 2)]
                cx, cy = min(xs), min(ys)
                cw, ch = max(xs) - cx, max(ys) - cy
                bx = bx - cx * dw
                by = by - cy * dh
                dw = cw * dw
                dh = ch * dh

        # Scaling to the screen footprint of the photo content (frame excluded)
        ir = self._img_rect()
        if dw <= 0 or dh <= 0 or ir.width() <= 0 or ir.height() <= 0:
            return None
        sx = ir.width()  / dw
        sy = ir.height() / dh
        return QRectF(ir.x() + bx * sx, ir.y() + by * sy, bw * sx, bh * sy)

    def _draw_one_face_rect(self, p: QPainter, rect: "QRectF") -> None:
        p.fillRect(rect, QColor(74, 159, 212, 45))
        p.setPen(QPen(QColor(74, 159, 212), 2.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(rect)
        p.setPen(QPen(QColor(130, 200, 255), 3))
        arm = min(rect.width(), rect.height()) * 0.18
        for cx, cy, dx, dy in [
            (rect.left(),  rect.top(),    1,  1),
            (rect.right(), rect.top(),   -1,  1),
            (rect.right(), rect.bottom(),-1, -1),
            (rect.left(),  rect.bottom(), 1, -1),
        ]:
            p.drawLine(QPointF(cx, cy), QPointF(cx + dx * arm, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * arm))

    def _draw_face_highlight(self, p: QPainter) -> None:
        faces = self._highlighted_faces or (
            [self._highlighted_face] if self._highlighted_face is not None else []
        )
        for face in faces:
            rect = self._face_screen_rect(face)
            if rect is not None:
                self._draw_one_face_rect(p, rect)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if self._pixmap and not self._pixmap.isNull():
            pw = int(self._pixmap.width() * self._zoom)
            ph = int(self._pixmap.height() * self._zoom)
            p.drawPixmap(int(self._offset.x()), int(self._offset.y()), pw, ph, self._pixmap)
            if self._annotations_visible:
                self._draw_annotation_overlay(p)
            if self._highlighted_face is not None or self._highlighted_faces:
                self._draw_face_highlight(p)
            if self._red_eye_mode:
                self._draw_red_eye_overlay(p)
            if self._grid_visible:
                self._draw_grid(p)
            if self._crop_mode:
                self._draw_crop_overlay(p)
            if self._vignette_mode:
                self._draw_vignette_overlay(p)
            if self._face_add_mode:
                self._draw_face_add_overlay(p)
            if self._annotation_mode:
                self._draw_annotation_tool_overlay(p)

    def _draw_face_add_overlay(self, p: QPainter) -> None:
        ir = self._img_rect()
        p.setPen(QPen(QColor(255, 200, 60, 160), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(ir)
        if self._face_add_rect is None:
            return
        rect = self._face_add_rect
        p.setPen(QPen(QColor(255, 200, 60), 2, Qt.DashLine))
        p.drawRect(rect)
        hs = 8
        p.setBrush(QColor(255, 200, 60))
        p.setPen(Qt.NoPen)
        for pt in (rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()):
            p.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))

    def _draw_crop_overlay(self, p: QPainter) -> None:
        ir = self._img_rect()

        # Crop mode border (an edging around the image)
        p.setPen(QPen(QColor(255, 255, 255, 120), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(ir)

        if not self._crop_quad:
            return

        tl, tr, br, bl = self._crop_quad
        poly = QPolygonF([tl, tr, br, bl])

        # Dark area outside the quadrilateral
        outer = QPainterPath()
        outer.addRect(ir)
        inner = QPainterPath()
        inner.addPolygon(poly)
        inner.closeSubpath()
        p.fillPath(outer.subtracted(inner), QColor(0, 0, 0, 155))

        # Dotted border of the quadrilateral
        p.setPen(QPen(QColor(255, 255, 255), 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(poly)

        # Grid of thirds (bilinear interpolation inside the quad)
        p.setPen(QPen(QColor(255, 255, 255, 80), 1))
        for t in (1 / 3, 2 / 3):
            top_pt  = tl + (tr - tl) * t
            bot_pt  = bl + (br - bl) * t
            left_pt = tl + (bl - tl) * t
            rgt_pt  = tr + (br - tr) * t
            p.drawLine(top_pt, bot_pt)
            p.drawLine(left_pt, rgt_pt)

        # 4 corner handles
        hs = 8
        p.setBrush(QColor(255, 255, 255))
        p.setPen(Qt.NoPen)
        for pt in self._crop_quad:
            p.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))

        # Edge handles ("sausages") - middle of each side
        for eid, (i, j) in enumerate(_EDGE_INDICES):
            a, b = self._crop_quad[i], self._crop_quad[j]
            mid = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
            dx, dy = b.x() - a.x(), b.y() - a.y()
            length = math.hypot(dx, dy)
            if length < 1:
                continue
            angle_deg = math.degrees(math.atan2(dy, dx))
            p.save()
            p.translate(mid)
            p.rotate(angle_deg)
            p.setBrush(QColor(255, 255, 255))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(-11, -5, 22, 10), 5, 5)
            p.restore()

        # Move indicator at the centre
        cx = sum(pt.x() for pt in self._crop_quad) / 4
        cy = sum(pt.y() for pt in self._crop_quad) / 4
        c = QPointF(cx, cy)
        p.setPen(QPen(QColor(255, 255, 255, 200), 1))
        p.setBrush(Qt.NoBrush)
        arm = 10
        p.drawLine(QPointF(c.x() - arm, c.y()), QPointF(c.x() + arm, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - arm), QPointF(c.x(), c.y() + arm))
        p.drawEllipse(c, 4.0, 4.0)

    # ------------------------------------------------------------------ events

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if self._red_eye_mode or self._face_add_mode or self._annotation_mode:
            return
        if self._crop_mode:
            # Zoom centred on the middle, preserving the crop quadrilateral
            factor = 1.15 if delta > 0 else 1 / 1.15
            new_zoom = self._zoom * factor
            if new_zoom < 0.1 or new_zoom > 4.0:
                return
            crop_rel = self._crop_to_rel()
            cx, cy = self.width() / 2.0, self.height() / 2.0
            self._offset = QPointF(
                cx - (cx - self._offset.x()) * factor,
                cy - (cy - self._offset.y()) * factor,
            )
            self._zoom = new_zoom
            self.zoom_changed.emit(self._zoom)
            if crop_rel:
                self._crop_from_rel(crop_rel)
            self.update()
        else:
            self.wheel_navigate.emit(1 if delta > 0 else -1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._annotation_mode:
            if event.button() == Qt.RightButton:
                return  # handled by contextMenuEvent (confirmation of the curve in progress)
            if event.button() != Qt.LeftButton:
                return
            pos = event.position()
            tool = self._annotation_tool
            if tool == "select":
                if len(self._annotation_selected_ids) == 1:
                    sel_ann = self._find_annotation(next(iter(self._annotation_selected_ids)))
                    if sel_ann is not None:
                        hid = self._annotation_hit_handle(sel_ann, pos)
                        if hid is not None:
                            self._start_annotation_resize(sel_ann, hid, pos)
                            self.update()
                            return
                ir = self._img_rect()
                if ir.width() <= 0 or ir.height() <= 0:
                    return
                ctrl = bool(event.modifiers() & Qt.ControlModifier)
                x, y = pos.x() - ir.x(), pos.y() - ir.y()
                hit_id = hit_test_annotations(self._annotations, x, y, ir.width(), ir.height(), tol_px=8.0)
                if hit_id is None:
                    # empty area: starts a rectangular (marquee) selection
                    if not ctrl:
                        self._set_annotation_selection(set())
                    self._annotation_marquee_start = QPointF(pos)
                    self._annotation_marquee_rect = QRectF(pos, pos)
                    self.update()
                    return
                hit_ann = self._find_annotation(hit_id)
                group = hit_ann.get("group") if hit_ann else None
                group_ids = (
                    {a.get("id") for a in self._annotations if a.get("group") == group}
                    if group else {hit_id}
                )
                if ctrl:
                    if group_ids <= self._annotation_selected_ids:
                        self._set_annotation_selection(self._annotation_selected_ids - group_ids)
                    else:
                        self._set_annotation_selection(self._annotation_selected_ids | group_ids)
                elif not (group_ids <= self._annotation_selected_ids):
                    self._set_annotation_selection(group_ids)
                if self._annotation_selected_ids:
                    self._annotation_drag_ids   = list(self._annotation_selected_ids)
                    self._annotation_drag_start = QPointF(pos)
                    self._annotation_drag_origs = {
                        i: copy.deepcopy(self._find_annotation(i)) for i in self._annotation_drag_ids
                        if self._find_annotation(i) is not None
                    }
                    self._annotation_drag_moved = False
                    self.setCursor(Qt.ClosedHandCursor)
                self.update()
                return
            if tool == "text":
                if self._annotation_text_editor is not None:
                    return
                if self._img_rect().contains(pos):
                    self._open_text_editor(pos)
                return
            if tool == "curve":
                if self._annotation_draft_type != "curve":
                    self._annotation_draft_type = "curve"
                    self._annotation_draft_points = []
                self._annotation_draft_points.append(pos)
                self.update()
                return
            if tool in ("pen", "line", "rect", "ellipse"):
                if not self._img_rect().contains(pos):
                    return
                self._annotation_draft_type = tool
                self._annotation_draft_points = [pos] if tool == "pen" else [pos, QPointF(pos)]
                self.update()
                return
            return
        if self._vignette_mode:
            if event.button() == Qt.LeftButton:
                pos = event.position()
                hit = self._vignette_hit_test(pos)
                if hit:
                    self._vignette_drag       = hit
                    self._vignette_drag_start = QPointF(pos)
                    self._vignette_edit_start = copy.copy(self._vignette_edit)
                    self.setCursor(Qt.ClosedHandCursor)
            return
        if self._wb_pick_mode:
            if event.button() == Qt.LeftButton and self._pixmap:
                pos = event.position()
                ir  = self._img_rect()
                if ir.contains(pos) and ir.width() > 0 and ir.height() > 0:
                    # Pixmap coordinates: the photo content starts after the
                    # decorative frame (b = 0 when there is none).
                    b  = self._frame_border_px()
                    cw = self._pixmap.width()  - 2 * b
                    ch = self._pixmap.height() - 2 * b
                    px = b + max(0, min(cw - 1, int((pos.x() - ir.x()) * cw / ir.width())))
                    py = b + max(0, min(ch - 1, int((pos.y() - ir.y()) * ch / ir.height())))
                    c = self._pixmap.toImage().pixelColor(px, py)
                    self.pixel_sampled.emit(c.red(), c.green(), c.blue())
            self._wb_pick_mode = False
            self.unsetCursor()
            return
        if self._red_eye_mode:
            if event.button() == Qt.LeftButton and self._pixmap:
                pos = event.position()
                ir = self._img_rect()
                if ir.contains(pos) and ir.width() > 0 and ir.height() > 0:
                    cx = (pos.x() - ir.x()) / ir.width()
                    cy = (pos.y() - ir.y()) / ir.height()
                    self.red_eye_point_added.emit(cx, cy)
            return
        if self._face_add_mode:
            if event.button() != Qt.LeftButton:
                return
            pos = event.position()
            ir  = self._img_rect()
            hid = self._face_add_hit_handle(pos) if self._face_add_rect is not None else None
            if hid is not None:
                self._face_add_action      = 'RESIZING'
                self._face_add_handle      = hid
                self._face_add_mouse_start = pos
                self._face_add_rect_start  = QRectF(self._face_add_rect)
            elif self._face_add_rect is not None and self._face_add_rect.contains(pos):
                self._face_add_action      = 'MOVING'
                self._face_add_mouse_start = pos
                self._face_add_rect_start  = QRectF(self._face_add_rect)
            elif ir.contains(pos):
                self._face_add_action     = 'DRAWING'
                self._face_add_draw_start = QPointF(pos)
                self._face_add_rect       = QRectF(pos, pos)
            self.update()
            return
        if self._crop_mode:
            if event.button() != Qt.LeftButton:
                return
            pos = event.position()
            ir  = self._img_rect()
            logger.debug(
                "crop press pos=(%.0f,%.0f) img_rect=(%.0f,%.0f,%.0f,%.0f) "
                "contains=%s crop_quad=%s",
                pos.x(), pos.y(),
                ir.x(), ir.y(), ir.width(), ir.height(),
                ir.contains(pos), self._crop_quad,
            )
            hid = self._hit_handle(pos)
            eid = self._hit_edge_handle(pos) if hid is None else None
            if hid is not None:
                self._crop_action      = 'RESIZING'
                self._crop_handle      = hid
                self._crop_mouse_start = pos
                self._crop_quad_start  = [QPointF(pt) for pt in self._crop_quad] if self._crop_quad else None
                self._drag_ratio       = self._locked_ratio_from_quad()
            elif eid is not None:
                self._crop_action      = 'RESIZING_EDGE'
                self._crop_handle      = eid
                self._crop_mouse_start = pos
                self._crop_quad_start  = [QPointF(pt) for pt in self._crop_quad] if self._crop_quad else None
                self._drag_ratio       = self._locked_ratio_from_quad()
            elif self._hit_center(pos):
                self._crop_action      = 'MOVING'
                self._crop_mouse_start = pos
                self._crop_quad_start  = [QPointF(pt) for pt in self._crop_quad] if self._crop_quad else None
                self._drag_ratio       = None
            elif ir.contains(pos):
                if self._crop_quad is not None:
                    # Area already defined -> pan
                    self._crop_action       = 'PANNING'
                    self._drag_start        = pos.toPoint()
                    self._drag_offset_start = QPointF(self._offset)
                    self._drag_ratio        = None
                    self.setCursor(Qt.ClosedHandCursor)
                else:
                    # First definition of the area
                    self._crop_action     = 'DRAWING'
                    self._crop_draw_start = QPointF(pos)
                    self._crop_quad       = [QPointF(pos) for _ in range(4)]
                    self._drag_ratio      = None  # determined at the first movement
                self.update()
        else:
            if event.button() == Qt.LeftButton:
                self._drag_start        = event.position().toPoint()
                self._drag_offset_start = QPointF(self._offset)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._annotation_mode:
            tool = self._annotation_tool
            if tool == "curve":
                self._annotation_hover_pos = pos if self._annotation_draft_points else None
                if self._annotation_draft_points:
                    self.update()
                return
            if tool == "pen" and self._annotation_draft_type == "pen":
                last = self._annotation_draft_points[-1]
                if math.hypot(pos.x() - last.x(), pos.y() - last.y()) >= 2.0:
                    ir = self._img_rect()
                    clamped = QPointF(
                        max(ir.left(), min(ir.right(), pos.x())),
                        max(ir.top(), min(ir.bottom(), pos.y())),
                    )
                    self._annotation_draft_points.append(clamped)
                    self.update()
                return
            if tool in ("line", "rect", "ellipse") and self._annotation_draft_type == tool:
                ir = self._img_rect()
                self._annotation_draft_points[1] = QPointF(
                    max(ir.left(), min(ir.right(), pos.x())),
                    max(ir.top(), min(ir.bottom(), pos.y())),
                )
                self.update()
                return
            if tool == "select" and self._annotation_resize_handle is not None:
                self._update_annotation_resize(pos)
                return
            if tool == "select" and self._annotation_drag_ids:
                self._update_annotation_drag(pos)
                return
            if tool == "select" and self._annotation_marquee_start is not None:
                self._annotation_marquee_rect = QRectF(self._annotation_marquee_start, pos).normalized()
                self.update()
                return
            if tool == "select":
                sel_ann = (self._find_annotation(next(iter(self._annotation_selected_ids)))
                           if len(self._annotation_selected_ids) == 1 else None)
                hid = self._annotation_hit_handle(sel_ann, pos) if sel_ann is not None else None
                if hid is not None:
                    cursor = Qt.PointingHandCursor if hid == 'rotate' else _ANNOTATION_CORNER_CURSORS.get(hid, Qt.SizeFDiagCursor)
                    self.setCursor(cursor)
                    return
                ir = self._img_rect()
                if ir.width() > 0 and ir.height() > 0:
                    x, y = pos.x() - ir.x(), pos.y() - ir.y()
                    hit = hit_test_annotations(self._annotations, x, y, ir.width(), ir.height(), tol_px=8.0)
                    self.setCursor(Qt.OpenHandCursor if hit else Qt.ArrowCursor)
            return
        if self._vignette_mode:
            if self._vignette_drag:
                self._vignette_update_drag(pos)
            else:
                # Update the cursor according to the proximity of the handles
                hit = self._vignette_hit_test(pos)
                self.setCursor(Qt.PointingHandCursor if hit else Qt.ArrowCursor)
            return
        if self._red_eye_mode:
            self._red_eye_mouse = pos
            self.update()
            return
        if self._face_add_mode:
            if self._face_add_action == 'DRAWING':
                ir = self._img_rect()
                s  = self._face_add_draw_start
                raw_x = max(ir.left(), min(ir.right(),  pos.x()))
                raw_y = max(ir.top(),  min(ir.bottom(), pos.y()))
                self._face_add_rect = QRectF(
                    QPointF(min(s.x(), raw_x), min(s.y(), raw_y)),
                    QPointF(max(s.x(), raw_x), max(s.y(), raw_y)),
                )
                self.update()
            elif self._face_add_action == 'RESIZING':
                self._apply_face_add_resize(pos)
                self.update()
            elif self._face_add_action == 'MOVING':
                self._apply_face_add_move(pos)
                self.update()
            else:
                hid = self._face_add_hit_handle(pos) if self._face_add_rect is not None else None
                if hid is not None:
                    self.setCursor([Qt.SizeFDiagCursor, Qt.SizeBDiagCursor,
                                     Qt.SizeFDiagCursor, Qt.SizeBDiagCursor][hid])
                elif self._face_add_rect is not None and self._face_add_rect.contains(pos):
                    self.setCursor(Qt.SizeAllCursor)
                else:
                    self.setCursor(Qt.CrossCursor)
            return
        if self._crop_mode:
            if self._crop_action == 'DRAWING':
                ir = self._img_rect()
                s  = self._crop_draw_start
                raw_x = max(ir.left(), min(ir.right(),  pos.x()))
                raw_y = max(ir.top(),  min(ir.bottom(), pos.y()))
                dx, dy = raw_x - s.x(), raw_y - s.y()
                if self._aspect_ratio is not None:
                    # Lock the ratio from the first significant movement on,
                    # respecting the explicitly chosen orientation (H or V).
                    if self._drag_ratio is None and (abs(dx) > 4 or abs(dy) > 4):
                        self._drag_ratio = self._aspect_ratio
                    if self._drag_ratio is not None:
                        self._crop_quad = self._constrained_rect(s, dx, dy,
                                                                  self._drag_ratio, ir)
                        self.update()
                        return
                self._crop_quad = _make_rect_quad(
                    min(s.x(), raw_x), min(s.y(), raw_y),
                    max(s.x(), raw_x), max(s.y(), raw_y),
                )
                self.update()
            elif self._crop_action == 'RESIZING':
                self._apply_drag_corner(pos)
                self.update()
            elif self._crop_action == 'RESIZING_EDGE':
                self._apply_drag_edge(pos)
                self.update()
            elif self._crop_action == 'MOVING':
                self._apply_move(pos)
                self.update()
            elif self._crop_action == 'PANNING':
                crop_rel = self._crop_to_rel()
                delta = pos.toPoint() - self._drag_start
                self._offset = self._drag_offset_start + QPointF(delta)
                if crop_rel:
                    self._crop_from_rel(crop_rel)
                self.update()
            else:
                # No action in progress - update the cursor
                self._update_cursor_for_pos(pos)
        else:
            if self._drag_start is not None:
                delta = event.position().toPoint() - self._drag_start
                self._offset = self._drag_offset_start + QPointF(delta)
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._annotation_mode:
            if event.button() == Qt.LeftButton and self._annotation_draft_type in ("pen", "line", "rect", "ellipse"):
                self.confirm_annotation_draft()
            if event.button() == Qt.LeftButton and self._annotation_resize_handle is not None:
                self._finish_annotation_resize()
            if event.button() == Qt.LeftButton and self._annotation_drag_ids:
                self._finish_annotation_drag()
            if event.button() == Qt.LeftButton and self._annotation_marquee_start is not None:
                self._finish_annotation_marquee(event)
            return
        if self._vignette_mode:
            if event.button() == Qt.LeftButton and self._vignette_drag:
                self._vignette_drag       = None
                self._vignette_drag_start = None
                self._vignette_edit_start = None
                pos = event.position()
                hit = self._vignette_hit_test(pos)
                self.setCursor(Qt.PointingHandCursor if hit else Qt.ArrowCursor)
            return
        if self._face_add_mode:
            if event.button() == Qt.LeftButton:
                self._face_add_action      = None
                self._face_add_handle      = None
                self._face_add_mouse_start = None
                self._face_add_rect_start  = None
                self._face_add_draw_start  = None
            return
        if self._crop_mode:
            if event.button() == Qt.LeftButton:
                self._drag_start       = None
                self._crop_action      = None
                self._crop_handle      = None
                self._crop_mouse_start = None
                self._crop_quad_start  = None
                self._crop_draw_start  = None
                self._drag_ratio       = None
                self._update_cursor_for_pos(event.position())
        elif event.button() == Qt.LeftButton:
            self._drag_start = None

    def contextMenuEvent(self, event) -> None:
        if self._annotation_mode:
            if self._annotation_tool == "curve" and self._annotation_draft_points:
                self.confirm_annotation_draft()
            elif self._annotation_selected_ids:
                menu = QMenu(self)
                install_menu_width_fix(menu)
                menu.addAction(translate("Canvas", "Delete\tDel"), self.delete_selected_annotation)
                if len(self._annotation_selected_ids) >= 2:
                    menu.addAction(translate("Canvas", "Group"), self.group_selected_annotations)
                if any(
                    a.get("group") for a in self._annotations
                    if a.get("id") in self._annotation_selected_ids
                ):
                    menu.addAction(translate("Canvas", "Ungroup"), self.ungroup_selected_annotations)
                menu.exec(event.globalPos())
            return
        if not self._crop_mode and not self._red_eye_mode and not self._face_add_mode:
            faces = self._highlighted_faces or (
                [self._highlighted_face] if self._highlighted_face is not None else []
            )
            for face in faces:
                rect = self._face_screen_rect(face)
                if rect is not None and rect.contains(QPointF(event.pos())):
                    self.face_context_menu_requested.emit(face, event.globalPos())
                    return
            self.context_menu_requested.emit(event.globalPos())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (self._annotation_mode and self._annotation_tool == "select"
                and event.button() == Qt.LeftButton):
            pos = event.position()
            ir = self._img_rect()
            if ir.width() > 0 and ir.height() > 0:
                x, y = pos.x() - ir.x(), pos.y() - ir.y()
                hit_id = hit_test_annotations(self._annotations, x, y, ir.width(), ir.height(), tol_px=8.0)
                hit_ann = self._find_annotation(hit_id) if hit_id is not None else None
                if hit_ann is not None and hit_ann.get("type") == "text":
                    self._open_text_editor_for_edit(hit_ann)
                    return
        if (self._annotation_mode and self._annotation_tool == "curve"
                and event.button() == Qt.LeftButton and self._annotation_draft_points):
            # The 2nd click of the double-click has already added an almost duplicate point
            # through mousePressEvent - we remove it before validating the stroke.
            if len(self._annotation_draft_points) > 1:
                self._annotation_draft_points.pop()
            self.confirm_annotation_draft()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._pixmap:
            crop_rel = self._crop_to_rel() if self._crop_mode else None
            self.zoom_fit()
            if crop_rel:
                self._crop_from_rel(crop_rel)
            if self._face_add_mode:
                # The screen rectangle does not survive a zoom/offset change.
                self._face_add_rect   = None
                self._face_add_action = None
            if self._annotation_mode:
                # The points in progress are in screen coordinates - a zoom/offset
                # change would invalidate them; the annotations already validated
                # (normalised coordinates) are not affected.
                self.cancel_annotation_draft()


