import io
import logging
import math
import os

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, Slot, QPoint, QRectF, QPointF, QSize
from PySide6.QtGui import (
    QDesktopServices, QPixmap, QPainter, QKeyEvent, QWheelEvent,
    QMouseEvent, QPen, QColor, QPainterPath, QPolygonF, QIcon,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy, QToolButton, QButtonGroup, QMenu,
)

from src.core.models import PhotoInfo, EditInfo
from src.processing.edit_database import EditDatabase
from src.processing.adjustments import ImageAdjuster

logger = logging.getLogger(__name__)

# Résolution maximale pour l'affichage à l'écran.
# Les retouches (rotation, recadrage, etc.) s'appliquent sur cette copie réduite.
# L'image originale pleine résolution n'est utilisée que pour l'export final.
_PREVIEW_MAX_PX = 1024


def _build_pixmap(photo: PhotoInfo, edit: EditInfo | None) -> "tuple[QPixmap, int, int] | None":
    """Retourne (pixmap, orig_w, orig_h) — dimensions de l'image EXIF-corrigée avant
    tout edit, à utiliser pour mapper les bbox de détection faciale."""
    from pathlib import Path as _Path
    from src.library.exif_reader import VIDEO_EXT
    if _Path(photo.path).suffix.lower() in VIDEO_EXT:
        return _build_video_pixmap(photo.path)
    try:
        from PIL import Image, ImageOps
        with Image.open(photo.path) as img:
            img = ImageOps.exif_transpose(img)
            orig_w, orig_h = img.size   # dimensions EXIF-corrigées (référence pour les bbox)
            if max(orig_w, orig_h) > _PREVIEW_MAX_PX:
                scale = _PREVIEW_MAX_PX / max(orig_w, orig_h)
                img = img.resize(
                    (round(orig_w * scale), round(orig_h * scale)),
                    Image.LANCZOS,
                )
            if edit and edit.is_modified():
                img = ImageAdjuster.apply_all(img, edit)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            pixmap = QPixmap()
            pixmap.loadFromData(buf.getvalue())
            return pixmap, orig_w, orig_h
    except Exception as e:
        logger.error(f"Erreur chargement photo {photo.path}: {e}")
        return None


def _build_video_base_image(video_path: str) -> "tuple[bytes, int, int] | None":
    """
    Extrait la première frame de la vidéo sans aucun seek.
    Retourne (jpeg_bytes, orig_w, orig_h).

    Utilise CAP_FFMPEG pour éviter les appels COM/DirectShow qui peuvent marshaler
    du travail sur le thread UI (STA) et provoquer des freezes.
    Ne lit jamais CAP_PROP_FRAME_COUNT ni cap.set(POS_FRAMES) : ces deux appels
    peuvent scanner ou décoder tout le fichier pour les formats sans index.
    """
    try:
        import cv2
        from PIL import Image

        cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return None
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        w, h = img.size
        if max(w, h) > _PREVIEW_MAX_PX:
            scale = _PREVIEW_MAX_PX / max(w, h)
            img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), orig_w or w, orig_h or h
    except Exception as e:
        logger.error("Erreur base vidéo %s: %s", video_path, e)
        return None


def _build_base_image(photo: PhotoInfo) -> "tuple[bytes, int, int] | None":
    """
    Charge l'image (ou la première frame vidéo), applique la correction EXIF et réduit
    à _PREVIEW_MAX_PX. Retourne (jpeg_bytes, orig_w, orig_h) SANS retouche.
    Résultat mis en cache dans PhotoViewer._base_cache : évite de relire le fichier
    complet à chaque mouvement de slider (preview de retouche).
    """
    from pathlib import Path as _Path
    from src.library.exif_reader import VIDEO_EXT
    if _Path(photo.path).suffix.lower() in VIDEO_EXT:
        return _build_video_base_image(photo.path)
    try:
        from PIL import Image, ImageOps
        with Image.open(photo.path) as img:
            img = ImageOps.exif_transpose(img)
            orig_w, orig_h = img.size
            if max(orig_w, orig_h) > _PREVIEW_MAX_PX:
                scale = _PREVIEW_MAX_PX / max(orig_w, orig_h)
                img = img.resize(
                    (round(orig_w * scale), round(orig_h * scale)), Image.LANCZOS
                )
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            return buf.getvalue(), orig_w, orig_h
    except Exception as e:
        logger.error("Erreur base image %s: %s", photo.path, e)
        return None


def _apply_edit_to_base(base_bytes: bytes, edit: "EditInfo | None") -> "QPixmap | None":
    """
    Applique les retouches sur l'image de base en cache (bytes JPEG 1024px).
    Aucune lecture disque — remplace _build_pixmap pour les previews.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(base_bytes))
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
        logger.error("Erreur apply edit: %s", e)
        return None


def _build_video_pixmap(video_path: str) -> "tuple[QPixmap, int, int] | None":
    """Extrait une frame de la vidéo pour l'afficher dans la visionneuse."""
    try:
        import cv2
        from PIL import Image

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if frame_count > 10:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_count * 0.1))
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None

        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame)
        orig_w, orig_h = img.size
        if max(orig_w, orig_h) > _PREVIEW_MAX_PX:
            scale = _PREVIEW_MAX_PX / max(orig_w, orig_h)
            img = img.resize((round(orig_w * scale), round(orig_h * scale)), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        return pixmap, orig_w, orig_h
    except Exception as e:
        logger.error("Erreur chargement vidéo %s: %s", video_path, e)
        return None


def _make_rect_quad(x0: float, y0: float, x1: float, y1: float) -> list:
    """Retourne [TL, TR, BR, BL] pour un rectangle axis-aligned."""
    return [QPointF(x0, y0), QPointF(x1, y0), QPointF(x1, y1), QPointF(x0, y1)]

# Formats de recadrage : (libellé, tooltip, ratio w/h ou None)
# L'icône de chaque bouton montre visuellement l'orientation paysage/portrait.
_CROP_FORMAT_DATA: list[tuple[str, str, float | None]] = [
    ("Libre",  "Format libre — quadrilatère quelconque",  None),
    ("10×15",  "10×15 horizontal  (ratio 3:2)",           3 / 2),
    ("10×15",  "10×15 vertical  (ratio 2:3)",             2 / 3),
    ("13×18",  "13×18 horizontal  (ratio 18:13)",         18 / 13),
    ("13×18",  "13×18 vertical  (ratio 13:18)",           13 / 18),
]

_BTN_CROP_STYLE = """
QToolButton {
    background: rgba(50,50,50,180);
    border: 1px solid rgba(255,255,255,35);
    border-radius: 3px;
    padding: 1px 4px;
    color: #999;
    font-size: 9px;
}
QToolButton:checked {
    background: rgba(50,110,170,230);
    border: 1px solid rgba(110,170,230,200);
    color: white;
}
QToolButton:hover:!checked {
    background: rgba(70,70,70,200);
    border: 1px solid rgba(255,255,255,60);
    color: #ccc;
}
"""


def _fmt_icon(ratio: float | None, iw: int = 24, ih: int = 18) -> QPixmap:
    """Icône représentant le ratio w/h par un rectangle aux bonnes proportions."""
    px = QPixmap(iw, ih)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    mg = 2
    mw, mh = iw - 2 * mg, ih - 2 * mg
    if ratio is None:
        rw, rh = mw, mh
        p.setPen(QPen(QColor(170, 170, 170), 1.2, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
    elif ratio >= 1:
        rw = mw
        rh = max(4, round(mw / ratio))
        p.setPen(QPen(QColor(190, 190, 190), 1.2))
        p.setBrush(QColor(130, 130, 130, 55))
    else:
        rh = mh
        rw = max(4, round(mh * ratio))
        p.setPen(QPen(QColor(190, 190, 190), 1.2))
        p.setBrush(QColor(130, 130, 130, 55))
    rx = (iw - rw) // 2
    ry = (ih - rh) // 2
    p.drawRect(rx, ry, rw - 1, rh - 1)
    p.end()
    return px

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
    zoom_changed                = Signal(float)
    wheel_navigate              = Signal(int)    # ±1 photo
    crop_confirmed              = Signal(object) # tuple 8 coords relatives (x0,y0,…,x3,y3)
    context_menu_requested      = Signal(object) # QPoint global
    red_eye_point_added         = Signal(float, float)  # cx_norm, cy_norm (0-1)
    pixel_sampled               = Signal(int, int, int)  # R, G, B — pipette balance des blancs
    face_context_menu_requested = Signal(object, object) # (FaceInfo, QPoint global)

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
        self._aspect_ratio: float | None = None         # ratio largeur/hauteur verrouillé (None = libre)
        self._drag_ratio:   float | None = None         # ratio effectif pour le drag en cours
        self._crop_mouse_start:  QPointF | None = None
        self._crop_quad_start:   list[QPointF] | None = None
        self._crop_draw_start:   QPointF | None = None
        self._grid_visible = False
        # Visage(s) mis en surbrillance — un seul (FacePanel clic) ou tous (bouton "Tous")
        self._highlighted_face  = None   # FaceInfo unique
        self._highlighted_faces: list = []  # liste pour le mode "Tous"
        self._orig_w: int = 0
        self._orig_h: int = 0
        self._current_edit = None   # EditInfo courant pour transformer les bbox
        # Mode correction yeux rouges
        self._red_eye_mode: bool = False
        self._red_eye_radius: float = 0.03   # rayon normalisé (0-1) pour le curseur
        self._red_eye_mouse: QPointF | None = None
        # Mode pipette balance des blancs
        self._wb_pick_mode: bool = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)

    # ------------------------------------------------------------------ pipette couleur

    def start_color_pick(self) -> None:
        """Active le mode pipette : prochain clic gauche → pixel_sampled(r, g, b)."""
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
            # Zoom centré sur le centre du viewport : préserve le point visible central
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

    def _constrained_rect(self, s: QPointF, dx: float, dy: float,
                          r: float, ir: QRectF) -> list:
        """Rectangle à partir du point de départ s, delta (dx,dy) et ratio r=w/h."""
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
        # Format verrouillé : le coin opposé est l'ancre, on recrée le rectangle
        r   = self._drag_ratio
        opp = (self._crop_handle + 2) % 4
        fx  = self._crop_quad[opp].x()
        fy  = self._crop_quad[opp].y()
        dx, dy = px - fx, py - fy
        if abs(dx) < 1 and abs(dy) < 1:
            return
        self._crop_quad = self._constrained_rect(QPointF(fx, fy), dx, dy, r, ir)

    def _apply_drag_edge(self, pos: QPointF) -> None:
        """Déplace l'arête en conservant l'orientation des côtés adjacents.
        Chaque coin glisse le long de son côté adjacent — allongement/réduction seulement."""
        if self._crop_quad is None or self._crop_handle is None:
            return
        if not self._crop_quad_start or not self._crop_mouse_start:
            return
        ir  = self._img_rect()
        dx  = pos.x() - self._crop_mouse_start.x()
        dy  = pos.y() - self._crop_mouse_start.y()
        eid = self._crop_handle

        if self._drag_ratio is not None:
            # Format verrouillé : rectangle, bord opposé ancré, ratio maintenu
            r = self._drag_ratio
            xs = [pt.x() for pt in self._crop_quad_start]
            ys = [pt.y() for pt in self._crop_quad_start]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if eid == 0:            # bord haut : y0 change
                ny0 = max(ir.top(),    min(y1 - 1, y0 + dy))
                h   = y1 - ny0
                w   = min(h * r, ir.width())
                h   = w / r
                hx  = w / 2
                cx2 = max(ir.left() + hx, min(ir.right() - hx, cx))
                self._crop_quad = _make_rect_quad(cx2-hx, y1-h, cx2+hx, y1)
            elif eid == 2:          # bord bas : y1 change
                ny1 = max(y0 + 1,   min(ir.bottom(), y1 + dy))
                h   = ny1 - y0
                w   = min(h * r, ir.width())
                h   = w / r
                hx  = w / 2
                cx2 = max(ir.left() + hx, min(ir.right() - hx, cx))
                self._crop_quad = _make_rect_quad(cx2-hx, y0, cx2+hx, y0+h)
            elif eid == 1:          # bord droit : x1 change
                nx1 = max(x0 + 1,   min(ir.right(), x1 + dx))
                w   = nx1 - x0
                h   = min(w / r, ir.height())
                w   = h * r
                hy  = h / 2
                cy2 = max(ir.top() + hy, min(ir.bottom() - hy, cy))
                self._crop_quad = _make_rect_quad(x0, cy2-hy, x0+w, cy2+hy)
            elif eid == 3:          # bord gauche : x0 change
                nx0 = max(ir.left(), min(x1 - 1, x0 + dx))
                w   = x1 - nx0
                h   = min(w / r, ir.height())
                w   = h * r
                hy  = h / 2
                cy2 = max(ir.top() + hy, min(ir.bottom() - hy, cy))
                self._crop_quad = _make_rect_quad(x1-w, cy2-hy, x1, cy2+hy)
            return

        i, j = _EDGE_INDICES[self._crop_handle]
        # Coin adjacent fixe pour chaque extrémité de l'arête
        # Dans le cycle TL(0)-TR(1)-BR(2)-BL(3), les voisins de i (≠j) et j (≠i) sont :
        i_adj = (i - 1) % 4
        j_adj = (j + 1) % 4

        q_i = self._crop_quad_start[i]       # position de départ du coin i
        q_j = self._crop_quad_start[j]       # position de départ du coin j
        p_i = self._crop_quad_start[i_adj]   # coin adjacent fixe de i
        p_j = self._crop_quad_start[j_adj]   # coin adjacent fixe de j

        # Normale à l'arête
        ex, ey = q_j.x() - q_i.x(), q_j.y() - q_i.y()
        length = math.hypot(ex, ey)
        if length < 1:
            return
        nx, ny = -ey / length, ex / length

        # Projection du déplacement souris sur la normale
        proj = dx * nx + dy * ny

        # Pour chaque coin : déplacement le long du côté adjacent
        # new_corner = q + proj * (q - p) / ((q - p) · n)
        # soit new_corner = q + proj * (sx, sy)
        dq_i_dot_n = (q_i.x() - p_i.x()) * nx + (q_i.y() - p_i.y()) * ny
        dq_j_dot_n = (q_j.x() - p_j.x()) * nx + (q_j.y() - p_j.y()) * ny
        if abs(dq_i_dot_n) < 1e-9 or abs(dq_j_dot_n) < 1e-9:
            return  # côté adjacent quasi-parallèle à l'arête — cas dégénéré

        si_x = (q_i.x() - p_i.x()) / dq_i_dot_n
        si_y = (q_i.y() - p_i.y()) / dq_i_dot_n
        sj_x = (q_j.x() - p_j.x()) / dq_j_dot_n
        sj_y = (q_j.y() - p_j.y()) / dq_j_dot_n

        # Clamp proj pour que les deux nouveaux coins restent dans l'image
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

    def set_aspect_ratio(self, ratio: float | None) -> None:
        self._aspect_ratio = ratio
        if ratio is not None and self._crop_quad:
            self._fit_rect_to_ratio(ratio)
        self.update()

    def _locked_ratio_from_quad(self) -> float | None:
        """Ratio verrouillé pour le drag en cours — utilise _aspect_ratio tel quel."""
        return self._aspect_ratio

    def _fit_rect_to_ratio(self, ratio: float) -> None:
        """Recadre le quad au ratio donné en conservant l'aire (rotation 90° si changement
        d'orientation, redimensionnement isométrique sinon)."""
        if not self._crop_quad:
            return
        ir = self._img_rect()
        xs = [pt.x() for pt in self._crop_quad]
        ys = [pt.y() for pt in self._crop_quad]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        w  = max(xs) - min(xs)
        h  = max(ys) - min(ys)
        # Nouvelles dimensions qui conservent l'aire : w_new*h_new = w*h et w_new/h_new = ratio
        area = w * h
        if area > 0:
            w = math.sqrt(area * ratio)
            h = math.sqrt(area / ratio)
        # Clamper à l'image en re-enforçant le ratio
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
        # Lignes fines en pointillés : 10 divisions régulières
        pen_dots = QPen(QColor(255, 255, 255, 255), 0.8, Qt.DotLine)
        p.setPen(pen_dots)
        for i in range(1, 10):
            t = i / 10
            p.drawLine(QPointF(ir.left() + t * ir.width(), ir.top()),
                       QPointF(ir.left() + t * ir.width(), ir.bottom()))
            p.drawLine(QPointF(ir.left(),  ir.top() + t * ir.height()),
                       QPointF(ir.right(), ir.top() + t * ir.height()))
        # Lignes de tiers pleines (repères d'alignement clés)
        p.setPen(QPen(QColor(255, 255, 255, 255), 1.2))
        for t in (1 / 3, 2 / 3):
            p.drawLine(QPointF(ir.left() + t * ir.width(), ir.top()),
                       QPointF(ir.left() + t * ir.width(), ir.bottom()))
            p.drawLine(QPointF(ir.left(),  ir.top() + t * ir.height()),
                       QPointF(ir.right(), ir.top() + t * ir.height()))

    def set_orig_size(self, w: int, h: int) -> None:
        """Dimensions EXIF-corrigées de l'image chargée (source de vérité pour les bbox)."""
        self._orig_w = w
        self._orig_h = h

    def set_edit(self, edit) -> None:
        """Edit courant à prendre en compte pour le mapping bbox → écran."""
        self._current_edit = edit

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
        """Rayon du curseur yeux rouges en pixels écran."""
        if not self._pixmap:
            return 20.0
        return self._red_eye_radius * min(self._pixmap.width(), self._pixmap.height()) * self._zoom

    def _draw_red_eye_overlay(self, p: QPainter) -> None:
        if not self._red_eye_mouse or not self._pixmap:
            return
        r = self._red_eye_screen_radius()
        center = self._red_eye_mouse
        p.setPen(QPen(QColor(220, 60, 60, 200), 1.5))
        p.setBrush(QColor(220, 60, 60, 40))
        p.drawEllipse(center, r, r)
        # Réticule
        p.setPen(QPen(QColor(220, 60, 60, 160), 1))
        arm = r * 0.6
        p.drawLine(QPointF(center.x() - arm, center.y()), QPointF(center.x() + arm, center.y()))
        p.drawLine(QPointF(center.x(), center.y() - arm), QPointF(center.x(), center.y() + arm))

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
        # bbox stocké dans l'espace de l'image après detected_rotation CW supplémentaire.
        # On ramène dans l'espace d'affichage (image EXIF-corrigée, sans rotation extra).
        bx, by, bw, bh = float(f.bbox_x), float(f.bbox_y), float(f.bbox_w), float(f.bbox_h)
        r = getattr(f, "detected_rotation", 0) % 360
        dw, dh = float(self._orig_w), float(self._orig_h)
        if r == 90:
            bx, by, bw, bh = by, dh - bx - bw, bh, bw
        elif r == 180:
            bx, by, bw, bh = dw - bx - bw, dh - by - bh, bw, bh
        elif r == 270:
            bx, by, bw, bh = dw - by - bh, bx, bh, bw

        # Appliquer les transformations géométriques de l'edit (même ordre que apply_all) :
        # rotation CW → straighten (ignoré, petit angle) → flip → crop
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
                # Format quad TL,TR,BR,BL (coords relatives 0-1)
                xs = [crop[i] for i in range(0, 8, 2)]
                ys = [crop[i] for i in range(1, 8, 2)]
                cx, cy = min(xs), min(ys)
                cw, ch = max(xs) - cx, max(ys) - cy
                bx = bx - cx * dw
                by = by - cy * dh
                dw = cw * dw
                dh = ch * dh

        # Mise à l'échelle pixmap puis zoom
        sx = self._pixmap.width()  / dw
        sy = self._pixmap.height() / dh
        x = self._offset.x() + bx * sx * self._zoom
        y = self._offset.y() + by * sy * self._zoom
        w = bw * sx * self._zoom
        h = bh * sy * self._zoom
        return QRectF(x, y, w, h)

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
            if self._highlighted_face is not None or self._highlighted_faces:
                self._draw_face_highlight(p)
            if self._red_eye_mode:
                self._draw_red_eye_overlay(p)
            if self._grid_visible:
                self._draw_grid(p)
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
        if self._red_eye_mode:
            return
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
            self.wheel_navigate.emit(1 if delta > 0 else -1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._wb_pick_mode:
            if event.button() == Qt.LeftButton and self._pixmap:
                pos = event.position()
                ir  = self._img_rect()
                if ir.contains(pos) and ir.width() > 0 and ir.height() > 0:
                    px = max(0, min(self._pixmap.width()  - 1,
                                   int((pos.x() - ir.x()) * self._pixmap.width()  / ir.width())))
                    py = max(0, min(self._pixmap.height() - 1,
                                   int((pos.y() - ir.y()) * self._pixmap.height() / ir.height())))
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
                    # Zone déjà définie → pan
                    self._crop_action       = 'PANNING'
                    self._drag_start        = pos.toPoint()
                    self._drag_offset_start = QPointF(self._offset)
                    self._drag_ratio        = None
                    self.setCursor(Qt.ClosedHandCursor)
                else:
                    # Première définition de la zone
                    self._crop_action     = 'DRAWING'
                    self._crop_draw_start = QPointF(pos)
                    self._crop_quad       = [QPointF(pos) for _ in range(4)]
                    self._drag_ratio      = None  # déterminé au premier mouvement
                self.update()
        else:
            if event.button() == Qt.LeftButton:
                self._drag_start        = event.position().toPoint()
                self._drag_offset_start = QPointF(self._offset)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._red_eye_mode:
            self._red_eye_mouse = pos
            self.update()
            return
        if self._crop_mode:
            if self._crop_action == 'DRAWING':
                ir = self._img_rect()
                s  = self._crop_draw_start
                raw_x = max(ir.left(), min(ir.right(),  pos.x()))
                raw_y = max(ir.top(),  min(ir.bottom(), pos.y()))
                dx, dy = raw_x - s.x(), raw_y - s.y()
                if self._aspect_ratio is not None:
                    # Verrouiller le ratio dès le premier mouvement significatif,
                    # en respectant l'orientation explicitement choisie (H ou V).
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
                self._drag_ratio       = None
                self._update_cursor_for_pos(event.position())
        elif event.button() == Qt.LeftButton:
            self._drag_start = None

    def contextMenuEvent(self, event) -> None:
        if not self._crop_mode and not self._red_eye_mode:
            faces = self._highlighted_faces or (
                [self._highlighted_face] if self._highlighted_face is not None else []
            )
            for face in faces:
                rect = self._face_screen_rect(face)
                if rect is not None and rect.contains(QPointF(event.pos())):
                    self.face_context_menu_requested.emit(face, event.globalPos())
                    return
            self.context_menu_requested.emit(event.globalPos())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._pixmap:
            crop_rel = self._crop_to_rel() if self._crop_mode else None
            self.zoom_fit()
            if crop_rel:
                self._crop_from_rel(crop_rel)


class _BaseLoader(QThread):
    """Charge _build_base_image dans un thread secondaire.
    Nécessaire pour les vidéos : cv2.VideoCapture peut marshaler des appels COM
    sur le thread UI (STA Windows) et provoquer des freezes si appelé directement."""

    base_ready = Signal(str, object)   # (photo_path, tuple[bytes,int,int] | None)

    def __init__(self, photo: "PhotoInfo", parent=None) -> None:
        super().__init__(parent)
        self._photo = photo

    def run(self) -> None:
        result = _build_base_image(self._photo)
        self.base_ready.emit(self._photo.path, result)


class PhotoViewer(QWidget):
    closed               = Signal()
    navigate             = Signal(int)
    zoom_changed         = Signal(float)
    crop_ready           = Signal(object)  # tuple 8 coords relatives (x0,y0,…,x3,y3)
    save_requested       = Signal(object)  # PhotoInfo
    rename_requested     = Signal(object)  # PhotoInfo
    delete_requested            = Signal(list)    # list[PhotoInfo]
    red_eye_point_added         = Signal(float, float)  # cx_norm, cy_norm (0-1)
    pixel_sampled               = Signal(int, int, int)  # R, G, B — pipette balance des blancs
    face_context_menu_requested = Signal(object, object)  # (FaceInfo, QPoint global)

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self._config = config
        self._photo: PhotoInfo | None = None
        self._edit: EditInfo | None = None
        self._db = EditDatabase()
        # Cache de l'image de base (1024px, sans retouche) pour la photo courante.
        # Évite de relire le fichier complet à chaque preview de slider.
        self._base_cache: "tuple[bytes, int, int] | None" = None
        # Thread de chargement de l'image de base (images et vidéos).
        # Le chargement est asynchrone pour éviter de bloquer le thread UI,
        # particulièrement critique pour les vidéos (cv2.VideoCapture + COM).
        self._base_loader: "_BaseLoader | None" = None
        # Debounce pour les previews de retouche : on ne recharge que 60 ms
        # après le dernier événement slider (évite les surcharges mémoire).
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(60)
        self._preview_timer.timeout.connect(self._reload_pixmap)
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
        self._lbl_name.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self._lbl_name.setCursor(Qt.IBeamCursor)
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
        self._canvas.context_menu_requested.connect(self._show_context_menu)
        self._canvas.red_eye_point_added.connect(self.red_eye_point_added)
        self._canvas.pixel_sampled.connect(self.pixel_sampled)
        self._canvas.face_context_menu_requested.connect(self.face_context_menu_requested)
        layout.addWidget(self._canvas, stretch=1)

        # ---- Pied de page ----
        self._navbar = QWidget()
        self._navbar.setStyleSheet("background: rgba(0,0,0,200);")
        self._navbar.setFixedHeight(52)
        nav_layout = QHBoxLayout(self._navbar)
        nav_layout.setContentsMargins(16, 6, 16, 6)

        self._btn_prev = QPushButton("◀  Plus ancienne")
        self._btn_prev.setFixedHeight(36)
        self._btn_prev.clicked.connect(lambda: self.navigate.emit(1))
        nav_layout.addWidget(self._btn_prev)

        nav_layout.addStretch()

        # Boutons de format de recadrage (masqués hors mode crop)
        self._btn_play_video = QPushButton("▶  Ouvrir la vidéo")
        self._btn_play_video.setToolTip("Ouvrir dans le lecteur vidéo par défaut")
        self._btn_play_video.setFixedHeight(36)
        self._btn_play_video.setStyleSheet(
            "QPushButton { background:#2a6a2a; color:white; border:none;"
            " border-radius:3px; padding:4px 16px; font-size: 13px; }"
            "QPushButton:hover { background:#3a7a3a; }"
        )
        self._btn_play_video.clicked.connect(self._open_in_player)
        self._btn_play_video.hide()
        nav_layout.addWidget(self._btn_play_video)

        self._crop_format_widget = QWidget()
        self._crop_format_widget.setStyleSheet("background: transparent;")
        fmt_layout = QHBoxLayout(self._crop_format_widget)
        fmt_layout.setContentsMargins(0, 0, 8, 0)
        fmt_layout.setSpacing(4)
        self._crop_format_group = QButtonGroup(self)
        self._crop_format_group.setExclusive(True)
        self._crop_format_btns: list[QToolButton] = []
        for i, (label, tip, ratio) in enumerate(_CROP_FORMAT_DATA):
            btn = QToolButton()
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIcon(QIcon(_fmt_icon(ratio)))
            btn.setIconSize(QSize(24, 18))
            btn.setText(label)
            btn.setToolTip(tip)
            btn.setCheckable(True)
            btn.setFixedSize(54, 40)
            btn.setStyleSheet(_BTN_CROP_STYLE)
            fmt_layout.addWidget(btn)
            self._crop_format_group.addButton(btn, i)
            self._crop_format_btns.append(btn)
        self._crop_format_btns[0].setChecked(True)  # "Libre" par défaut
        self._crop_format_group.idClicked.connect(self._on_crop_format_changed)
        self._crop_format_widget.hide()
        nav_layout.addWidget(self._crop_format_widget)

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

        self._btn_next = QPushButton("Plus récente  ▶")
        self._btn_next.setFixedHeight(36)
        self._btn_next.clicked.connect(lambda: self.navigate.emit(-1))
        nav_layout.addWidget(self._btn_next)

        layout.addWidget(self._navbar)

    # ------------------------------------------------------------------ photo

    def current_photo(self) -> "PhotoInfo | None":
        return self._photo

    def set_photo(self, photo: PhotoInfo, edit: EditInfo | None = None) -> None:
        self._preview_timer.stop()
        # Annuler un chargement de base image en cours (navigation rapide)
        if self._base_loader and self._base_loader.isRunning():
            self._base_loader.base_ready.disconnect()
            self._base_loader.quit()
            self._base_loader = None
        self._base_cache = None  # invalide le cache pour la nouvelle photo
        self._photo = photo
        is_video = photo.media_type == "video"
        self._edit = None if is_video else (edit or self._db.load(photo.path))
        self._lbl_name.setText(photo.path)
        self._btn_fav.setChecked(photo.is_favorite)
        self._btn_play_video.setVisible(is_video)
        self._canvas.set_highlighted_face(None)
        self._reload_pixmap()

    def highlight_face(self, face) -> None:
        """Encadre un visage unique sur la photo (appelé depuis main_window)."""
        self._canvas.set_highlighted_face(face)

    def set_all_highlighted_faces(self, faces: list) -> None:
        """Encadre tous les visages (mode 'Tous' du FacePanel)."""
        self._canvas.set_highlighted_faces(faces)

    def _reload_pixmap(self) -> None:
        if not self._photo:
            return
        self._canvas.set_edit(self._edit)

        if self._base_cache is not None:
            # Cache chaud : appliquer les retouches et afficher immédiatement
            base_bytes, orig_w, orig_h = self._base_cache
            pixmap = _apply_edit_to_base(base_bytes, self._edit)
            self._canvas.set_orig_size(orig_w, orig_h)
            self._canvas.set_pixmap(pixmap)
            return

        # Cache froid : lancer le chargement en arrière-plan (image ou vidéo)
        # Afficher un état vide pendant le chargement
        self._canvas.set_orig_size(0, 0)
        self._canvas.set_pixmap(None)
        if self._base_loader and self._base_loader.isRunning():
            return  # déjà en cours pour cette photo
        self._base_loader = _BaseLoader(self._photo, self)
        self._base_loader.base_ready.connect(self._on_base_ready)
        self._base_loader.start()

    @Slot(str, object)
    def _on_base_ready(self, path: str, result: object) -> None:
        """Reçoit le résultat du chargement de base image/vidéo depuis _BaseLoader."""
        if self._photo is None or self._photo.path != path:
            return  # navigation entre-temps — résultat obsolète
        self._base_cache = result
        if result is None:
            return
        base_bytes, orig_w, orig_h = result
        pixmap = _apply_edit_to_base(base_bytes, self._edit)
        self._canvas.set_edit(self._edit)
        self._canvas.set_orig_size(orig_w, orig_h)
        self._canvas.set_pixmap(pixmap)

    def refresh_name(self) -> None:
        if self._photo:
            self._lbl_name.setText(self._photo.path)

    def update_edit(self, edit: EditInfo) -> None:
        self._edit = edit
        # Debounce : reporte le rendu de 60 ms pour absorber les rafales de
        # sliders. Évite d'accumuler des images PIL de 72 Mo en mémoire.
        self._preview_timer.stop()
        self._preview_timer.start()

    def set_grid_visible(self, visible: bool) -> None:
        self._canvas.set_grid_visible(visible)

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
        # Si un crop est déjà appliqué, afficher l'image sans ce crop pour que
        # l'utilisateur puisse repositionner la zone sur l'image complète.
        if existing and self._photo and self._edit and self._base_cache:
            edit_no_crop = EditInfo.from_dict({**self._edit.to_dict(), 'crop': None})
            base_bytes, _, _ = self._base_cache
            pixmap = _apply_edit_to_base(base_bytes, edit_no_crop)
            if pixmap:
                self._canvas.set_pixmap(pixmap)
        self._canvas.enter_crop(existing)
        idx = self._crop_format_group.checkedId()
        self._canvas.set_aspect_ratio(_CROP_FORMAT_DATA[idx][2] if idx >= 0 else None)
        self._btn_prev.hide()
        self._btn_next.hide()
        self._crop_format_widget.show()
        self._btn_crop_confirm.show()
        self._btn_crop_cancel.show()

    def _on_crop_format_changed(self, idx: int) -> None:
        self._canvas.set_aspect_ratio(_CROP_FORMAT_DATA[idx][2])

    def confirm_crop(self) -> None:
        self._canvas.confirm_crop()    # émet crop_confirmed → _on_crop_confirmed

    def update_nav_arrows(self, has_prev: bool, has_next: bool) -> None:
        self._btn_prev.setVisible(has_prev)
        self._btn_next.setVisible(has_next)

    def cancel_crop(self) -> None:
        self._canvas.cancel_crop()
        self._crop_format_widget.hide()
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        # Restaurer l'image avec le crop appliqué (on avait affiché l'image sans crop)
        self._reload_pixmap()

    def _on_crop_confirmed(self, quad: tuple) -> None:
        self._crop_format_widget.hide()
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self.crop_ready.emit(quad)

    # ------------------------------------------------------------------ red-eye mode

    def enter_red_eye_mode(self, radius: float = 0.03) -> None:
        self._canvas.enter_red_eye_mode(radius)

    def exit_red_eye_mode(self) -> None:
        self._canvas.exit_red_eye_mode()

    def set_red_eye_radius(self, radius: float) -> None:
        self._canvas.set_red_eye_radius(radius)

    # ------------------------------------------------------------------ pipette couleur (balance des blancs)

    def start_color_pick(self) -> None:
        """Active le mode pipette : prochain clic gauche sur l'image → pixel_sampled(r, g, b)."""
        self._canvas.start_color_pick()

    def stop_color_pick(self) -> None:
        self._canvas.stop_color_pick()

    # ------------------------------------------------------------------ misc

    def _open_in_player(self) -> None:
        if not self._photo:
            return
        player = self._config.get("video.player_path", "") if self._config else ""
        if player:
            import subprocess
            try:
                subprocess.Popen([player, self._photo.path])
                return
            except Exception as e:
                logger.warning("Lecteur vidéo introuvable (%s) : %s", player, e)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._photo.path))

    def _show_context_menu(self, pos) -> None:
        if not self._photo:
            return
        photo = self._photo
        menu = QMenu(self)

        fav_label = "Retirer des favoris" if photo.is_favorite else "Marquer comme favori"
        menu.addAction(fav_label, self._toggle_fav_from_menu)
        menu.addAction("Renommer…", lambda: self.rename_requested.emit(photo))
        menu.addAction("Enregistrer l'image traitée sur le disque",
                       lambda: self.save_requested.emit(photo))
        menu.addSeparator()
        menu.addAction("Révéler dans l'Explorateur",
                       lambda: os.startfile(os.path.dirname(photo.path)))
        menu.addSeparator()

        gps_coords = self._resolve_gps(photo)
        act_map = menu.addAction("Localiser sur la carte")
        act_map.setEnabled(gps_coords is not None)
        if gps_coords is not None:
            lat, lon = gps_coords
            act_map.triggered.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl(f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=15#map=15/{lat}/{lon}")
                )
            )

        menu.addSeparator()
        menu.addAction("Effacer le fichier…", lambda: self.delete_requested.emit([photo]))

        menu.exec(pos)

    def _resolve_gps(self, photo) -> "tuple[float, float] | None":
        """Return (lat, lon) from catalog cache or by reading EXIF directly."""
        if photo.has_gps and photo.gps_lat is not None and photo.gps_lon is not None:
            return photo.gps_lat, photo.gps_lon
        # Catalog field missing (indexed before GPS support or parse failure) — read live
        try:
            from PIL import Image
            from src.library.exif_reader import ExifReader
            with Image.open(photo.path) as img:
                gps_ifd = img.getexif().get_ifd(0x8825)
                if gps_ifd:
                    return ExifReader._parse_gps(gps_ifd)
        except Exception:
            pass
        return None

    def _toggle_fav_from_menu(self) -> None:
        if not self._photo:
            return
        new_state = not self._photo.is_favorite
        self._btn_fav.setChecked(new_state)
        self._toggle_favorite(new_state)

    def _toggle_favorite(self, checked: bool) -> None:
        if self._photo:
            self._photo.is_favorite = checked
            self._btn_fav.setText("★" if checked else "♡")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            if self._canvas._crop_mode:
                self.cancel_crop()
            elif self._canvas._red_eye_mode:
                self.exit_red_eye_mode()
            else:
                self.closed.emit()
        elif key == Qt.Key_Return or key == Qt.Key_Enter:
            if self._canvas._crop_mode:
                self.confirm_crop()
        elif key in (Qt.Key_Right, Qt.Key_Up):
            if not self._canvas._crop_mode and not self._canvas._red_eye_mode:
                self.navigate.emit(-1)   # plus récente (droite/haut = vers le haut de la liste)
        elif key in (Qt.Key_Left, Qt.Key_Down):
            if not self._canvas._crop_mode and not self._canvas._red_eye_mode:
                self.navigate.emit(1)    # plus ancienne (gauche/bas = vers le bas de la liste)
        elif key == Qt.Key_Delete:
            photo = self.current_photo()
            if photo and not self._canvas._crop_mode and not self._canvas._red_eye_mode:
                self.delete_requested.emit([photo])
        elif key == Qt.Key_0:
            self.zoom_fit()
        elif key == Qt.Key_1:
            self.zoom_100()
        else:
            super().keyPressEvent(event)
