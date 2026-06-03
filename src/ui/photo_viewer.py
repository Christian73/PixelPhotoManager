import io
import logging
import math

from PySide6.QtCore import Qt, Signal, QPoint, QRectF, QPointF
from PySide6.QtGui import (
    QPixmap, QPainter, QKeyEvent, QWheelEvent,
    QMouseEvent, QPen, QColor, QPainterPath, QPolygonF,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy,
)

from src.core.models import PhotoInfo, EditInfo
from src.processing.edit_database import EditDatabase
from src.processing.adjustments import ImageAdjuster

logger = logging.getLogger(__name__)


def _build_pixmap(photo: PhotoInfo, edit: EditInfo | None) -> QPixmap | None:
    try:
        from PIL import Image, ImageOps
        with Image.open(photo.path) as img:
            img = ImageOps.exif_transpose(img)
            if edit and edit.is_modified():
                img = ImageAdjuster.apply_all(img, edit)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            pixmap = QPixmap()
            pixmap.loadFromData(buf.getvalue())
            return pixmap
    except Exception as e:
        logger.error(f"Erreur chargement photo {photo.path}: {e}")
        return None


# 4 poignées de coin : TL(0), TR(1), BR(2), BL(3)
_CORNER_CURSORS = [
    Qt.SizeFDiagCursor,  # 0: TL
    Qt.SizeBDiagCursor,  # 1: TR
    Qt.SizeFDiagCursor,  # 2: BR
    Qt.SizeBDiagCursor,  # 3: BL
]
_HANDLE_HIT = 10   # pixels de tolérance pour détecter une poignée de coin
_EDGE_HIT   = 12   # pixels de tolérance pour détecter une poignée d'arête
# Paires de coins formant chaque arête : haut, droite, bas, gauche
_EDGE_INDICES = [(0, 1), (1, 2), (2, 3), (3, 0)]


class _Canvas(QWidget):
    zoom_changed    = Signal(float)
    wheel_navigate  = Signal(int)                  # ±1 photo
    crop_confirmed  = Signal(object)               # tuple 8 coords relatives (x0,y0,…,x3,y3)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._zoom = 1.0
        self._offset = QPointF(0, 0)
        self._drag_start: QPoint | None = None
        self._drag_offset_start = QPointF(0, 0)
        # Crop
        self._crop_mode   = False
        self._crop_quad:  list[QPointF] | None = None   # [TL, TR, BR, BL] coords écran
        self._crop_action: str | None   = None          # None | 'DRAWING' | 'MOVING' | 'RESIZING' | 'PANNING'
        self._crop_handle: int | None   = None          # index coin actif (0-3)
        self._crop_mouse_start:  QPointF | None = None
        self._crop_quad_start:   list[QPointF] | None = None
        self._crop_draw_start:   QPointF | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)

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
        self._zoom = max(0.1, min(factor, 4.0))
        self._center()
        self.update()

    def _center(self) -> None:
        if not self._pixmap:
            return
        cw, ch = self.width(), self.height()
        pw = self._pixmap.width() * self._zoom
        ph = self._pixmap.height() * self._zoom
        self._offset = QPointF((cw - pw) / 2, (ch - ph) / 2)

    # ------------------------------------------------------------------ crop helpers

    def _img_rect(self) -> QRectF:
        """Rect en coordonnées entières — identique à ce que drawPixmap dessine réellement."""
        if not self._pixmap:
            return QRectF()
        return QRectF(
            int(self._offset.x()), int(self._offset.y()),
            int(self._pixmap.width()  * self._zoom),
            int(self._pixmap.height() * self._zoom),
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

    def _apply_drag_corner(self, pos: QPointF) -> None:
        if self._crop_quad is None or self._crop_handle is None:
            return
        ir = self._img_rect()
        px = max(ir.left(), min(ir.right(),  pos.x()))
        py = max(ir.top(),  min(ir.bottom(), pos.y()))
        self._crop_quad[self._crop_handle] = QPointF(px, py)

    def _apply_drag_edge(self, pos: QPointF) -> None:
        """Déplace les deux coins de l'arête active de la même quantité (solidaire)."""
        if self._crop_quad is None or self._crop_handle is None:
            return
        if not self._crop_quad_start or not self._crop_mouse_start:
            return
        ir = self._img_rect()
        dx = pos.x() - self._crop_mouse_start.x()
        dy = pos.y() - self._crop_mouse_start.y()
        i, j = _EDGE_INDICES[self._crop_handle]
        for k in (i, j):
            px = max(ir.left(), min(ir.right(),  self._crop_quad_start[k].x() + dx))
            py = max(ir.top(),  min(ir.bottom(), self._crop_quad_start[k].y() + dy))
            self._crop_quad[k] = QPointF(px, py)

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
        """Renvoie le quad en coords relatives (0-1) : (x0,y0,x1,y1,x2,y2,x3,y3)."""
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
        """Restaure _crop_quad depuis des coords relatives.
        Accepte l'ancien format rectangulaire (4 valeurs x,y,w,h) ou le nouveau quad (8 valeurs)."""
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

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if self._pixmap and not self._pixmap.isNull():
            pw = int(self._pixmap.width() * self._zoom)
            ph = int(self._pixmap.height() * self._zoom)
            p.drawPixmap(int(self._offset.x()), int(self._offset.y()), pw, ph, self._pixmap)
            if self._crop_mode:
                self._draw_crop_overlay(p)

    def _draw_crop_overlay(self, p: QPainter) -> None:
        ir = self._img_rect()

        # Bordure de mode crop (liseré autour de l'image)
        p.setPen(QPen(QColor(255, 255, 255, 120), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(ir)

        if not self._crop_quad:
            return

        tl, tr, br, bl = self._crop_quad
        poly = QPolygonF([tl, tr, br, bl])

        # Zone sombre hors du quadrilatère
        outer = QPainterPath()
        outer.addRect(ir)
        inner = QPainterPath()
        inner.addPolygon(poly)
        inner.closeSubpath()
        p.fillPath(outer.subtracted(inner), QColor(0, 0, 0, 155))

        # Bordure pointillée du quadrilatère
        p.setPen(QPen(QColor(255, 255, 255), 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(poly)

        # Grille des tiers (interpolation bilinéaire dans le quad)
        p.setPen(QPen(QColor(255, 255, 255, 80), 1))
        for t in (1 / 3, 2 / 3):
            top_pt  = tl + (tr - tl) * t
            bot_pt  = bl + (br - bl) * t
            left_pt = tl + (bl - tl) * t
            rgt_pt  = tr + (br - tr) * t
            p.drawLine(top_pt, bot_pt)
            p.drawLine(left_pt, rgt_pt)

        # 4 poignées de coin
        hs = 8
        p.setBrush(QColor(255, 255, 255))
        p.setPen(Qt.NoPen)
        for pt in self._crop_quad:
            p.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))

        # Poignées d'arête ("saucisses") — milieu de chaque côté
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

        # Indicateur de déplacement au centre
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
        if self._crop_mode:
            # Zoom centré sur le milieu, en préservant le quadrilatère de crop
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
            self.wheel_navigate.emit(-1 if delta > 0 else 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
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
            elif eid is not None:
                self._crop_action      = 'RESIZING_EDGE'
                self._crop_handle      = eid
                self._crop_mouse_start = pos
                self._crop_quad_start  = [QPointF(pt) for pt in self._crop_quad] if self._crop_quad else None
            elif self._hit_center(pos):
                self._crop_action      = 'MOVING'
                self._crop_mouse_start = pos
                self._crop_quad_start  = [QPointF(pt) for pt in self._crop_quad] if self._crop_quad else None
            elif ir.contains(pos):
                if self._crop_quad is not None:
                    # Zone déjà définie → pan
                    self._crop_action       = 'PANNING'
                    self._drag_start        = pos.toPoint()
                    self._drag_offset_start = QPointF(self._offset)
                    self.setCursor(Qt.ClosedHandCursor)
                else:
                    # Première définition de la zone
                    self._crop_action     = 'DRAWING'
                    self._crop_draw_start = QPointF(pos)
                    self._crop_quad       = [QPointF(pos) for _ in range(4)]
                self.update()
        else:
            if event.button() == Qt.LeftButton:
                self._drag_start        = event.position().toPoint()
                self._drag_offset_start = QPointF(self._offset)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._crop_mode:
            if self._crop_action == 'DRAWING':
                ir = self._img_rect()
                clamped = QPointF(
                    max(ir.left(), min(ir.right(),  pos.x())),
                    max(ir.top(),  min(ir.bottom(), pos.y())),
                )
                s = self._crop_draw_start
                self._crop_quad = [
                    QPointF(min(s.x(), clamped.x()), min(s.y(), clamped.y())),  # TL
                    QPointF(max(s.x(), clamped.x()), min(s.y(), clamped.y())),  # TR
                    QPointF(max(s.x(), clamped.x()), max(s.y(), clamped.y())),  # BR
                    QPointF(min(s.x(), clamped.x()), max(s.y(), clamped.y())),  # BL
                ]
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
                # Pas d'action en cours — mettre à jour le curseur
                self._update_cursor_for_pos(pos)
        else:
            if self._drag_start is not None:
                delta = event.position().toPoint() - self._drag_start
                self._offset = self._drag_offset_start + QPointF(delta)
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._crop_mode:
            if event.button() == Qt.LeftButton:
                self._drag_start       = None
                self._crop_action      = None
                self._crop_handle      = None
                self._crop_mouse_start = None
                self._crop_quad_start  = None
                self._crop_draw_start  = None
                self._update_cursor_for_pos(event.position())
        elif event.button() == Qt.LeftButton:
            self._drag_start = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._pixmap:
            crop_rel = self._crop_to_rel() if self._crop_mode else None
            self.zoom_fit()
            if crop_rel:
                self._crop_from_rel(crop_rel)


class PhotoViewer(QWidget):
    closed     = Signal()
    navigate   = Signal(int)
    zoom_changed = Signal(float)
    crop_ready = Signal(object)   # tuple 8 coords relatives (x0,y0,…,x3,y3)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._photo: PhotoInfo | None = None
        self._edit: EditInfo | None = None
        self._db = EditDatabase()
        self._setup_ui()
        self.setFocusPolicy(Qt.StrongFocus)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- Toolbar permanente ----
        self._toolbar = QWidget()
        self._toolbar.setStyleSheet("background: rgba(0,0,0,200);")
        tb_layout = QHBoxLayout(self._toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4)

        self._btn_back = QPushButton("←")
        self._btn_back.setToolTip("Retour à la grille  (Echap)")
        self._btn_back.setFixedWidth(32)
        self._btn_back.clicked.connect(self.closed.emit)
        tb_layout.addWidget(self._btn_back)

        self._lbl_name = QLabel("")
        self._lbl_name.setStyleSheet("color: #ccc;")
        tb_layout.addWidget(self._lbl_name, stretch=1)

        self._btn_fav = QPushButton("♡")
        self._btn_fav.setToolTip("Marquer comme favori  (F)")
        self._btn_fav.setFixedWidth(32)
        self._btn_fav.setCheckable(True)
        self._btn_fav.clicked.connect(self._toggle_favorite)
        tb_layout.addWidget(self._btn_fav)

        self._btn_fit = QPushButton("⊡")
        self._btn_fit.setToolTip("Ajuster à la fenêtre  (0)")
        self._btn_fit.setFixedWidth(32)
        self._btn_fit.clicked.connect(self.zoom_fit)
        tb_layout.addWidget(self._btn_fit)

        self._btn_100 = QPushButton("1:1")
        self._btn_100.setToolTip("Zoom 100%  (1)")
        self._btn_100.setFixedWidth(36)
        self._btn_100.clicked.connect(self.zoom_100)
        tb_layout.addWidget(self._btn_100)

        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip("Fermer  (Echap)")
        self._btn_close.setFixedWidth(32)
        self._btn_close.clicked.connect(self.closed.emit)
        tb_layout.addWidget(self._btn_close)

        layout.addWidget(self._toolbar)

        # ---- Canvas ----
        self._canvas = _Canvas()
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._canvas.zoom_changed.connect(self.zoom_changed)
        self._canvas.wheel_navigate.connect(self.navigate)
        self._canvas.crop_confirmed.connect(self._on_crop_confirmed)
        layout.addWidget(self._canvas, stretch=1)

        # ---- Pied de page ----
        self._navbar = QWidget()
        self._navbar.setStyleSheet("background: rgba(0,0,0,200);")
        self._navbar.setFixedHeight(52)
        nav_layout = QHBoxLayout(self._navbar)
        nav_layout.setContentsMargins(16, 6, 16, 6)

        self._btn_prev = QPushButton("◀  Précédente")
        self._btn_prev.setFixedHeight(36)
        self._btn_prev.clicked.connect(lambda: self.navigate.emit(-1))
        nav_layout.addWidget(self._btn_prev)

        nav_layout.addStretch()

        self._btn_crop_confirm = QPushButton("✓  Confirmer le recadrage")
        self._btn_crop_confirm.setToolTip("Valider le recadrage  (Entrée)")
        self._btn_crop_confirm.setFixedHeight(36)
        self._btn_crop_confirm.setStyleSheet("background: #2a6a2a; color: white;")
        self._btn_crop_confirm.clicked.connect(self.confirm_crop)
        self._btn_crop_confirm.hide()
        nav_layout.addWidget(self._btn_crop_confirm)

        self._btn_crop_cancel = QPushButton("✕  Annuler")
        self._btn_crop_cancel.setToolTip("Annuler le recadrage  (Echap)")
        self._btn_crop_cancel.setFixedHeight(36)
        self._btn_crop_cancel.setStyleSheet("background: #6a2a2a; color: white;")
        self._btn_crop_cancel.clicked.connect(self.cancel_crop)
        self._btn_crop_cancel.hide()
        nav_layout.addWidget(self._btn_crop_cancel)

        nav_layout.addStretch()

        self._btn_next = QPushButton("Suivante  ▶")
        self._btn_next.setFixedHeight(36)
        self._btn_next.clicked.connect(lambda: self.navigate.emit(1))
        nav_layout.addWidget(self._btn_next)

        layout.addWidget(self._navbar)

    # ------------------------------------------------------------------ photo

    def set_photo(self, photo: PhotoInfo, edit: EditInfo | None = None) -> None:
        self._photo = photo
        self._edit = edit or self._db.load(photo.path)
        self._lbl_name.setText(photo.path)
        self._btn_fav.setChecked(photo.is_favorite)
        self._reload_pixmap()

    def _reload_pixmap(self) -> None:
        if not self._photo:
            return
        pixmap = _build_pixmap(self._photo, self._edit)
        self._canvas.set_pixmap(pixmap)

    def update_edit(self, edit: EditInfo) -> None:
        self._edit = edit
        self._reload_pixmap()

    # ------------------------------------------------------------------ zoom

    def zoom_fit(self) -> None:
        self._canvas.zoom_fit()

    def zoom_100(self) -> None:
        self._canvas.zoom_100()

    def set_zoom(self, factor: float) -> None:
        self._canvas.set_zoom(factor)

    @property
    def zoom(self) -> float:
        return self._canvas.zoom

    # ------------------------------------------------------------------ crop

    def enter_crop_mode(self) -> None:
        existing = self._edit.crop if self._edit else None
        self._canvas.enter_crop(existing)
        self._btn_prev.hide()
        self._btn_next.hide()
        self._btn_crop_confirm.show()
        self._btn_crop_cancel.show()

    def confirm_crop(self) -> None:
        self._canvas.confirm_crop()    # émet crop_confirmed → _on_crop_confirmed

    def cancel_crop(self) -> None:
        self._canvas.cancel_crop()
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()

    def _on_crop_confirmed(self, quad: tuple) -> None:
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self.crop_ready.emit(quad)

    # ------------------------------------------------------------------ misc

    def _toggle_favorite(self, checked: bool) -> None:
        if self._photo:
            self._photo.is_favorite = checked
            self._btn_fav.setText("★" if checked else "♡")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            if self._canvas._crop_mode:
                self.cancel_crop()
            else:
                self.closed.emit()
        elif key == Qt.Key_Return or key == Qt.Key_Enter:
            if self._canvas._crop_mode:
                self.confirm_crop()
        elif key in (Qt.Key_Left, Qt.Key_Up):
            if not self._canvas._crop_mode:
                self.navigate.emit(-1)
        elif key in (Qt.Key_Right, Qt.Key_Down):
            if not self._canvas._crop_mode:
                self.navigate.emit(1)
        elif key == Qt.Key_0:
            self.zoom_fit()
        elif key == Qt.Key_1:
            self.zoom_100()
        else:
            super().keyPressEvent(event)
