import copy
import logging
import math
import os

from PySide6.QtCore import Qt, Signal, QSize, QPoint, QTimer
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QFont, QPen, QIcon,
    QPolygon, QBrush, QLinearGradient, QPainterPath,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QScrollArea, QGroupBox, QDialog,
    QDialogButtonBox, QToolButton, QGridLayout, QSizePolicy,
    QToolBar, QCheckBox,
)

from src.core.models import PhotoInfo, EditInfo
from src.processing.adjustments import ImageAdjuster
from src.processing.edit_database import EditDatabase

logger = logging.getLogger(__name__)

_UNDO_MAX = 20
_ICON_SIZE = 44


# ------------------------------------------------------------------ icônes

def _base_pixmap(size: int) -> tuple[QPixmap, QPainter]:
    px = QPixmap(size, size)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    return px, p


def _icon_brightness(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    c, r = size // 2, size // 4
    p.setBrush(QColor(255, 210, 60))
    p.setPen(QPen(QColor(255, 170, 0), 1))
    p.drawEllipse(c - r, c - r, r * 2, r * 2)
    p.setPen(QPen(QColor(255, 210, 60), 2))
    r1, r2 = r + 3, r + size // 5
    for i in range(8):
        a = math.radians(i * 45)
        p.drawLine(
            int(c + r1 * math.cos(a)), int(c + r1 * math.sin(a)),
            int(c + r2 * math.cos(a)), int(c + r2 * math.sin(a)),
        )
    p.end()
    return px


def _icon_contrast(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    c = size // 2
    r = int(size * 0.38)
    p.setBrush(QColor(30, 30, 30))
    p.setPen(Qt.NoPen)
    p.drawChord(c - r, c - r, r * 2, r * 2, 90 * 16, 180 * 16)
    p.setBrush(QColor(230, 230, 230))
    p.drawChord(c - r, c - r, r * 2, r * 2, 270 * 16, 180 * 16)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(140, 140, 140), 1))
    p.drawEllipse(c - r, c - r, r * 2, r * 2)
    p.end()
    return px


def _icon_saturation(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    c, r = size // 2, size // 3
    for angle, col in [
        (210, QColor(80, 80, 220, 180)),
        (330, QColor(80, 200, 80, 180)),
        (90,  QColor(220, 60, 60, 180)),
    ]:
        rad = math.radians(angle)
        cx = int(c + r * 0.45 * math.cos(rad))
        cy = int(c + r * 0.45 * math.sin(rad))
        p.setBrush(col)
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r // 2, cy - r // 2, r, r)
    p.end()
    return px


def _icon_gamma(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    pad = size // 8
    w, h = size - 2 * pad, size - 2 * pad
    p.setPen(QPen(QColor(80, 80, 80), 1, Qt.DashLine))
    p.drawLine(pad, pad + h, pad + w, pad)
    p.setPen(QPen(QColor(160, 160, 255), 2))
    prev = None
    for i in range(w + 1):
        t = i / w
        y = h - int(h * (t ** 0.42))
        pt = (pad + i, pad + y)
        if prev:
            p.drawLine(prev[0], prev[1], pt[0], pt[1])
        prev = pt
    p.end()
    return px


def _icon_sharpness(size: int = _ICON_SIZE) -> QPixmap:
    px, p = _base_pixmap(size)
    mid = size // 2
    # Gauche : flou (cercles doux)
    p.setPen(Qt.NoPen)
    for cx, cy, r in [(mid // 2, mid // 2, 6), (mid // 3, mid * 3 // 4, 5),
                      (mid * 2 // 3, mid * 2 // 3, 4)]:
        p.setBrush(QColor(120, 120, 120, 80))
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
    # Droite : net (traits vifs)
    p.setPen(QPen(QColor(210, 210, 210), 1))
    for dy in range(0, size, size // 5):
        p.drawLine(mid + 4, dy, size - 4, dy)
    p.drawLine(mid + 4, 0, mid + 4, size)
    # Ligne de séparation
    p.setPen(QPen(QColor(100, 130, 255), 1))
    p.drawLine(mid, 2, mid, size - 2)
    p.end()
    return px


def _icon_noise(size: int = _ICON_SIZE) -> QPixmap:
    import random
    px, p = _base_pixmap(size)
    mid = size // 2
    rng = random.Random(7)
    p.setPen(QColor(0, 0, 0, 0))  # no pen
    # Gauche : bruit
    for _ in range(80):
        x = rng.randint(2, mid - 2)
        y = rng.randint(2, size - 2)
        v = rng.randint(40, 210)
        p.setPen(QColor(v, v, v))
        p.drawPoint(x, y)
    # Droite : lissé
    p.setPen(Qt.NoPen)
    for x in range(mid + 1, size - 1):
        p.setBrush(QColor(150, 150, 150, 160))
        p.drawRect(x, 2, 1, size - 4)
    # Séparation
    p.setPen(QPen(QColor(100, 220, 100), 1))
    p.drawLine(mid, 2, mid, size - 2)
    p.end()
    return px


def _icon_straighten(size: int = _ICON_SIZE) -> QPixmap:
    """Cadre légèrement incliné + ligne d'horizon horizontale."""
    px, p = _base_pixmap(size)
    c = size // 2
    pad = size // 7
    # Ligne d'horizon de référence (pointillés)
    p.setPen(QPen(QColor(100, 180, 255), 1, Qt.DashLine))
    p.drawLine(pad, c, size - pad, c)
    # Cadre incliné représentant l'image à redresser
    angle = math.radians(12)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    hw, hh = size // 2 - pad - 2, size // 3 - 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    rotated = [
        QPoint(int(c + x * cos_a - y * sin_a), int(c + x * sin_a + y * cos_a))
        for x, y in corners
    ]
    p.setPen(QPen(QColor(210, 210, 210), 2))
    p.setBrush(Qt.NoBrush)
    for i in range(4):
        p.drawLine(rotated[i], rotated[(i + 1) % 4])
    # Petite flèche de correction (arc)
    p.setPen(QPen(QColor(100, 200, 100), 2))
    p.drawArc(c - 8, c + pad // 2, 16, 10, 0, 100 * 16)
    p.end()
    return px


def _icon_flip_h(size: int = _ICON_SIZE) -> QPixmap:
    """Deux triangles pointant vers l'axe vertical central."""
    px, p = _base_pixmap(size)
    c, pad = size // 2, size // 6
    h_half = size // 3
    # Triangle gauche → pointe vers la droite (vers le centre)
    tl = QPolygon([
        QPoint(pad, c - h_half),
        QPoint(c - 3, c),
        QPoint(pad, c + h_half),
    ])
    p.setBrush(QColor(90, 150, 255))
    p.setPen(Qt.NoPen)
    p.drawPolygon(tl)
    # Triangle droit → pointe vers la gauche (vers le centre)
    tr = QPolygon([
        QPoint(size - pad, c - h_half),
        QPoint(c + 3, c),
        QPoint(size - pad, c + h_half),
    ])
    p.drawPolygon(tr)
    # Axe central
    p.setPen(QPen(QColor(255, 255, 255), 2))
    p.drawLine(c, pad, c, size - pad)
    p.end()
    return px


def _icon_crop(size: int = _ICON_SIZE) -> QPixmap:
    """Rectangle de recadrage avec poignées de coin."""
    px, p = _base_pixmap(size)
    pad_out = size // 7
    pad_in  = size // 3
    # Zone image (contour en pointillé)
    p.setPen(QPen(QColor(90, 90, 90), 1, Qt.DashLine))
    p.setBrush(Qt.NoBrush)
    p.drawRect(pad_out, pad_out, size - 2 * pad_out, size - 2 * pad_out)
    # Zone crop (contour blanc)
    p.setPen(QPen(QColor(200, 200, 200), 2))
    p.drawRect(pad_in, pad_in, size - 2 * pad_in, size - 2 * pad_in)
    # Poignées de coin
    hs = 4
    p.setBrush(QColor(200, 200, 200))
    p.setPen(Qt.NoPen)
    for hx, hy in [(pad_in, pad_in), (size - pad_in, pad_in),
                   (pad_in, size - pad_in), (size - pad_in, size - pad_in)]:
        p.drawRect(hx - hs, hy - hs, hs * 2, hs * 2)
    p.end()
    return px


def _icon_flip_v(size: int = _ICON_SIZE) -> QPixmap:
    """Deux triangles pointant vers l'axe horizontal central."""
    px, p = _base_pixmap(size)
    c, pad = size // 2, size // 6
    w_half = size // 3
    # Triangle haut → pointe vers le bas (vers le centre)
    tt = QPolygon([
        QPoint(c - w_half, pad),
        QPoint(c, c - 3),
        QPoint(c + w_half, pad),
    ])
    p.setBrush(QColor(90, 200, 100))
    p.setPen(Qt.NoPen)
    p.drawPolygon(tt)
    # Triangle bas → pointe vers le haut (vers le centre)
    tb = QPolygon([
        QPoint(c - w_half, size - pad),
        QPoint(c, c + 3),
        QPoint(c + w_half, size - pad),
    ])
    p.drawPolygon(tb)
    # Axe central
    p.setPen(QPen(QColor(255, 255, 255), 2))
    p.drawLine(pad, c, size - pad, c)
    p.end()
    return px


# ------------------------------------------------------------------ slider

class EditSlider(QWidget):
    value_changed = Signal(float)

    def __init__(self, label: str, min_val: float, max_val: float,
                 default_val: float, decimals: int = 2, parent=None):
        super().__init__(parent)
        self._min = min_val
        self._max = max_val
        self._default = default_val
        self._decimals = decimals
        self._scale = 100

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(label)
        lbl.setFixedWidth(110)
        layout.addWidget(lbl)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(int(min_val * self._scale), int(max_val * self._scale))
        self._slider.setValue(int(default_val * self._scale))
        self._slider.valueChanged.connect(self._on_changed)
        layout.addWidget(self._slider, stretch=1)

        self._val_lbl = QLabel(self._fmt(default_val))
        self._val_lbl.setFixedWidth(46)
        self._val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._val_lbl)

        # Flèches d'ajustement fin (pas = 1 unité au niveau de la dernière décimale)
        self._step_size = 10 ** (-self._decimals)
        arrows = QVBoxLayout()
        arrows.setContentsMargins(0, 0, 0, 0)
        arrows.setSpacing(1)
        _arrow_style = (
            "QPushButton { padding:0; font-size:8px; min-width:16px; max-width:16px;"
            " min-height:12px; max-height:12px; }"
        )
        btn_up = QPushButton("▲")
        btn_up.setStyleSheet(_arrow_style)
        btn_up.setToolTip("Augmenter d'un pas")
        btn_up.clicked.connect(lambda: self._nudge(self._step_size))
        arrows.addWidget(btn_up)
        btn_dn = QPushButton("▼")
        btn_dn.setStyleSheet(_arrow_style)
        btn_dn.setToolTip("Diminuer d'un pas")
        btn_dn.clicked.connect(lambda: self._nudge(-self._step_size))
        arrows.addWidget(btn_dn)
        layout.addLayout(arrows)

        self._slider.mouseDoubleClickEvent = lambda _e: (
            self.set_value(self._default),
            self.value_changed.emit(self._default),
        )

    def _fmt(self, v: float) -> str:
        return f"{v:.{self._decimals}f}"

    def _on_changed(self, raw: int) -> None:
        v = raw / self._scale
        self._val_lbl.setText(self._fmt(v))
        self.value_changed.emit(v)

    def get_value(self) -> float:
        return self._slider.value() / self._scale

    def set_value(self, v: float) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(int(v * self._scale))
        self._val_lbl.setText(self._fmt(v))
        self._slider.blockSignals(False)

    def _nudge(self, delta: float) -> None:
        new_val = max(self._min, min(self._max, self.get_value() + delta))
        self.set_value(new_val)
        self.value_changed.emit(new_val)


# ------------------------------------------------------------------ dialogue de traitement

class TreatmentDialog(QDialog):
    preview = Signal(object)  # EditInfo en temps réel

    def __init__(self, title: str, sliders_def: list, edit: EditInfo, parent=None):
        """
        sliders_def : list of (label, attr_name, min, max, decimals)
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(720)
        self._edit = copy.copy(edit)
        self._sliders: dict[str, EditSlider] = {}
        self._panel = None   # référence vers EditPanel, positionné dans showEvent

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        for label, attr, mn, mx, decimals in sliders_def:
            sl = EditSlider(label, mn, mx, getattr(edit, attr), decimals)
            self._sliders[attr] = sl
            sl.value_changed.connect(lambda v, a=attr: self._on_changed(a, v))
            layout.addWidget(sl)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Valider")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._panel is not None:
            # Dimensions réelles disponibles ici.
            # QTimer.singleShot(0) diffère le move() APRÈS que Windows ait fini
            # tout repositionnement asynchrone (adjustPosition, WM_WINDOWPOSCHANGED…).
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            QTimer.singleShot(0, lambda: self.move(pos))

    def _on_changed(self, attr: str, value: float) -> None:
        setattr(self._edit, attr, value)
        self.preview.emit(copy.copy(self._edit))

    def get_edit(self) -> EditInfo:
        return self._edit


# ------------------------------------------------------------------ courbe gamma


def _compute_luminosity_histogram(photo_path: str) -> list[float]:
    """Retourne 256 valeurs normalisées (log) de l'histogramme de luminosité."""
    try:
        from PIL import Image
        img = Image.open(photo_path)
        img.thumbnail((384, 384))
        hist = img.convert("L").histogram()  # 256 buckets
        max_val = max(hist) if hist else 1
        log_max = math.log(max_val + 1)
        return [math.log(v + 1) / log_max for v in hist]
    except Exception:
        return []


def _gamma_to_curve_points(gamma: float) -> list:
    result = []
    for v in [0.0, 0.25, 0.5, 0.75, 1.0]:
        out = v ** (1.0 / max(0.01, gamma))
        result.append((v, max(0.0, min(1.0, out))))
    return result


_CURVE_PAD = 22


class GammaCurveWidget(QWidget):
    curve_changed = Signal(list)

    def __init__(self, points=None, histogram=None, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._points: list[tuple[float, float]] = sorted(
            points or [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
        )
        self._histogram: list[float] = histogram or []
        self._drag_idx = -1
        self._hover_idx = -1
        self.setMouseTracking(True)

    # -- coords helpers

    def _chart(self):
        p = _CURVE_PAD
        return p, p, self.width() - 2 * p, self.height() - 2 * p

    def _to_widget(self, cx: float, cy: float) -> tuple[int, int]:
        x0, y0, w, h = self._chart()
        return int(x0 + cx * w), int(y0 + (1.0 - cy) * h)

    def _to_curve(self, px: int, py: int) -> tuple[float, float]:
        x0, y0, w, h = self._chart()
        cx = (px - x0) / max(w, 1)
        cy = 1.0 - (py - y0) / max(h, 1)
        return max(0.0, min(1.0, cx)), max(0.0, min(1.0, cy))

    def _hit(self, px: int, py: int, r: int = 9) -> int:
        for i, (cx, cy) in enumerate(self._points):
            wx, wy = self._to_widget(cx, cy)
            if abs(px - wx) <= r and abs(py - wy) <= r:
                return i
        return -1

    # -- painting

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x0, y0, w, h = self._chart()

        p.fillRect(self.rect(), QColor(22, 22, 32))
        p.fillRect(x0, y0, w, h, QColor(14, 14, 22))

        # histogramme de luminosité (silhouette semi-transparente)
        if self._histogram:
            hist_path = QPainterPath()
            hist_path.moveTo(x0, y0 + h)
            for k, v in enumerate(self._histogram):
                hx = x0 + k * w / 255.0
                hy = y0 + h - v * h * 0.92
                hist_path.lineTo(hx, hy)
            hist_path.lineTo(x0 + w, y0 + h)
            hist_path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(210, 210, 210, 38))
            p.drawPath(hist_path)

        # grille
        p.setPen(QPen(QColor(55, 55, 75), 1))
        for i in range(1, 4):
            p.drawLine(x0 + i * w // 4, y0, x0 + i * w // 4, y0 + h)
            p.drawLine(x0, y0 + i * h // 4, x0 + w, y0 + i * h // 4)

        # diagonale identité
        p.setPen(QPen(QColor(75, 75, 100), 1, Qt.DashLine))
        p.drawLine(x0, y0 + h, x0 + w, y0)

        # courbe interpolée
        lut = ImageAdjuster._curve_lut(self._points)
        p.setPen(QPen(QColor(140, 140, 255), 2))
        prev = None
        for k, yv in enumerate(lut):
            wx, wy = self._to_widget(k / 255.0, yv / 255.0)
            if prev:
                p.drawLine(prev[0], prev[1], wx, wy)
            prev = (wx, wy)

        # bordure
        p.setPen(QPen(QColor(75, 75, 100), 1))
        p.drawRect(x0, y0, w, h)

        # points de contrôle
        for i, (cx, cy) in enumerate(self._points):
            wx, wy = self._to_widget(cx, cy)
            r = 7 if (i == self._drag_idx or i == self._hover_idx) else 5
            p.setBrush(QColor(255, 255, 255) if (i == self._drag_idx or i == self._hover_idx)
                       else QColor(160, 160, 255))
            p.setPen(QPen(QColor(220, 220, 255), 1))
            p.drawEllipse(wx - r, wy - r, r * 2, r * 2)

        p.end()

    # -- interactions

    def mousePressEvent(self, event):
        px, py = event.x(), event.y()
        idx = self._hit(px, py)
        if event.button() == Qt.RightButton:
            if idx >= 0 and len(self._points) > 2:
                self._points.pop(idx)
                self._drag_idx = -1
                self.update()
                self.curve_changed.emit(list(self._points))
            return
        if idx >= 0:
            self._drag_idx = idx
        else:
            cx, cy = self._to_curve(px, py)
            x0, y0, w, h = self._chart()
            if x0 <= px <= x0 + w and y0 <= py <= y0 + h:
                # éviter les x trop proches des points existants
                if not any(abs(cx - p[0]) < 0.02 for p in self._points):
                    self._points.append((cx, cy))
                    self._points.sort(key=lambda pt: pt[0])
                    self._drag_idx = next(
                        i for i, pt in enumerate(self._points) if abs(pt[0] - cx) < 0.001
                    )
                    self.update()
                    self.curve_changed.emit(list(self._points))

    def mouseMoveEvent(self, event):
        px, py = event.x(), event.y()
        if self._drag_idx >= 0:
            cx, cy = self._to_curve(px, py)
            idx = self._drag_idx
            pts = self._points
            # les extrémités restent fixées en x
            if idx == 0:
                cx = 0.0
            elif idx == len(pts) - 1:
                cx = 1.0
            else:
                x_min = pts[idx - 1][0] + 0.01
                x_max = pts[idx + 1][0] - 0.01
                cx = max(x_min, min(x_max, cx))
            self._points[idx] = (cx, cy)
            self.update()
            self.curve_changed.emit(list(self._points))
        else:
            new_hover = self._hit(px, py)
            if new_hover != self._hover_idx:
                self._hover_idx = new_hover
                self.update()
            self.setCursor(Qt.SizeAllCursor if new_hover >= 0 else Qt.CrossCursor)

    def mouseReleaseEvent(self, event):
        self._drag_idx = -1

    # -- API publique

    def set_from_gamma(self, gamma: float) -> None:
        self._points = _gamma_to_curve_points(gamma)
        self.update()
        self.curve_changed.emit(list(self._points))

    def get_points(self) -> list:
        return list(self._points)

    def set_points(self, points: list) -> None:
        self._points = sorted(points, key=lambda pt: pt[0])
        self.update()


# ------------------------------------------------------------------ dialogue gamma avancé

class GammaTreatmentDialog(QDialog):
    preview = Signal(object)

    def __init__(self, edit: EditInfo, photo_path: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gamma")
        self.setMinimumWidth(320)
        self._edit = copy.copy(edit)
        self._panel = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Slider mode simple
        self._gamma_slider = EditSlider("Gamma", 0.1, 3.0, edit.gamma, 2)
        self._gamma_slider.value_changed.connect(self._on_gamma_changed)
        layout.addWidget(self._gamma_slider)

        self._chk = QCheckBox("Paramétrage avancé…")
        self._chk.setChecked(edit.gamma_use_curve)
        self._chk.toggled.connect(self._on_advanced_toggled)
        layout.addWidget(self._chk)

        # Section avancée (masquée par défaut)
        self._adv = QWidget()
        adv_layout = QVBoxLayout(self._adv)
        adv_layout.setContentsMargins(0, 0, 0, 0)
        adv_layout.setSpacing(4)

        lbl = QLabel(
            "Cliquer pour ajouter un point · Glisser pour déplacer · "
            "Clic droit pour supprimer"
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #999; font-size: 10px;")
        adv_layout.addWidget(lbl)

        histogram = _compute_luminosity_histogram(photo_path) if photo_path else []
        init_pts = edit.gamma_curve_points if edit.gamma_use_curve else _gamma_to_curve_points(edit.gamma)
        self._curve = GammaCurveWidget(points=init_pts, histogram=histogram)
        self._curve.curve_changed.connect(self._on_curve_changed)
        adv_layout.addWidget(self._curve)

        layout.addWidget(self._adv)

        # Appliquer visibilité initiale
        self._gamma_slider.setVisible(not edit.gamma_use_curve)
        self._adv.setVisible(edit.gamma_use_curve)
        self._edit.gamma_curve_points = init_pts

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Valider")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            QTimer.singleShot(0, lambda: self.move(pos))

    def _on_gamma_changed(self, value: float) -> None:
        self._edit.gamma = value
        self.preview.emit(copy.copy(self._edit))

    def _on_advanced_toggled(self, checked: bool) -> None:
        self._gamma_slider.setVisible(not checked)
        self._adv.setVisible(checked)
        self._edit.gamma_use_curve = checked
        if checked:
            self._curve.set_from_gamma(self._edit.gamma)
        self.adjustSize()
        QTimer.singleShot(0, self._reposition)
        self.preview.emit(copy.copy(self._edit))

    def _reposition(self) -> None:
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            self.move(pos)

    def _on_curve_changed(self, points: list) -> None:
        self._edit.gamma_curve_points = points
        self.preview.emit(copy.copy(self._edit))

    def get_edit(self) -> EditInfo:
        return self._edit


# ------------------------------------------------------------------ dialogue couleurs


class CouleursTreatmentDialog(QDialog):
    preview = Signal(object)  # EditInfo

    def __init__(self, edit: EditInfo, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Couleurs")
        self.setMinimumWidth(720)
        self._edit = copy.copy(edit)
        self._panel = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Curseur saturation globale
        self._sl_sat = EditSlider("Saturation", -1.0, 1.0, edit.saturation, 2)
        self._sl_sat.value_changed.connect(lambda v: self._on_changed("saturation", v))
        layout.addWidget(self._sl_sat)

        # Checkbox avancé
        chk = QCheckBox("Fonctions avancées…")
        has_channel_edits = any(v != 0.0 for v in (edit.color_red, edit.color_green, edit.color_blue))
        chk.setChecked(has_channel_edits)
        layout.addWidget(chk)

        # Section RVB (masquée par défaut)
        self._adv = QWidget()
        adv_layout = QVBoxLayout(self._adv)
        adv_layout.setContentsMargins(0, 4, 0, 0)
        adv_layout.setSpacing(4)

        lbl = QLabel("Réglage des couleurs indépendantes")
        lbl.setStyleSheet("color: #999; font-size: 10px;")
        adv_layout.addWidget(lbl)

        self._sl_r = EditSlider("Rouge",  -1.0, 1.0, edit.color_red,   2)
        self._sl_g = EditSlider("Vert",   -1.0, 1.0, edit.color_green, 2)
        self._sl_b = EditSlider("Bleu",   -1.0, 1.0, edit.color_blue,  2)
        for sl, attr in [
            (self._sl_r, "color_red"),
            (self._sl_g, "color_green"),
            (self._sl_b, "color_blue"),
        ]:
            sl.value_changed.connect(lambda v, a=attr: self._on_changed(a, v))
            adv_layout.addWidget(sl)

        self._adv.setVisible(has_channel_edits)
        chk.toggled.connect(self._adv.setVisible)
        chk.toggled.connect(lambda _: QTimer.singleShot(0, self.adjustSize))
        layout.addWidget(self._adv)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Valider")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            QTimer.singleShot(0, lambda: self.move(pos))

    def _on_changed(self, attr: str, value: float) -> None:
        setattr(self._edit, attr, value)
        self.preview.emit(copy.copy(self._edit))

    def get_edit(self) -> EditInfo:
        return self._edit


# ------------------------------------------------------------------ panneau principal

# (label, icône_fn, sliders_def)
_TREATMENTS: list[tuple] = [
    ("Luminosité",  _icon_brightness, [("Luminosité",  "brightness",      -1.0, 1.0, 2)]),
    ("Contraste",   _icon_contrast,   [("Contraste",   "contrast",        -1.0, 1.0, 2)]),
    ("Couleurs",    _icon_saturation, [("Saturation",  "saturation",      -1.0, 1.0, 2)]),
    ("Gamma",       _icon_gamma,      [("Gamma",       "gamma",            0.1, 3.0, 2)]),
    ("Netteté",     _icon_sharpness,  [("Netteté",     "sharpness",        0.0, 1.0, 2)]),
    ("Débruitage",  _icon_noise,      [("Débruitage",  "noise_reduction",  0.0, 1.0, 2)]),
]


class EditPanel(QWidget):
    edits_changed          = Signal(object)       # EditInfo
    crop_mode_requested    = Signal()
    grid_visibility_changed = Signal(bool)
    photo_saved            = Signal(str, object)  # (photo_path, EditInfo) — uniquement lors d'un enregistrement réel
    rotation_stepped       = Signal(str, int)     # (photo_path, new_rotation_degrees) — émis uniquement pour les rotations 90°

    def __init__(self, parent=None):
        super().__init__(parent)
        self._photo: PhotoInfo | None = None
        self._edit = EditInfo()
        self._undo_stack: list[EditInfo] = []
        self._redo_stack: list[EditInfo] = []
        self._db = EditDatabase()
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # Barre titre + undo/redo
        title_bar = QHBoxLayout()
        self._title_label = QLabel("Retouche")
        self._title_label.setStyleSheet("font-weight: bold;")
        title_bar.addWidget(self._title_label, stretch=1)

        btn_undo = QPushButton("↩")
        btn_undo.setToolTip("Annuler  (Ctrl+Z)")
        btn_undo.setFixedWidth(28)
        btn_undo.clicked.connect(self.undo)
        title_bar.addWidget(btn_undo)

        btn_redo = QPushButton("↪")
        btn_redo.setToolTip("Rétablir  (Ctrl+Y)")
        btn_redo.setFixedWidth(28)
        btn_redo.clicked.connect(self.redo)
        title_bar.addWidget(btn_redo)

        root.addLayout(title_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(8)
        inner_layout.setContentsMargins(2, 2, 2, 2)

        # Grille de boutons de traitement
        lbl_corrections = QLabel("Corrections")
        lbl_corrections.setStyleSheet("color: #aaa; font-size: 10px;")
        inner_layout.addWidget(lbl_corrections)

        grid = QGridLayout()
        grid.setSpacing(4)
        for idx, (name, icon_fn, sliders_def) in enumerate(_TREATMENTS):
            btn = self._make_treatment_button(name, icon_fn(), sliders_def)
            grid.addWidget(btn, idx // 2, idx % 2)
        inner_layout.addLayout(grid)

        # Géométrie (boutons directs)
        grp_geo = QGroupBox("Géométrie")
        grp_geo.setLayout(QVBoxLayout())
        grp_geo.layout().setSpacing(4)
        grp_geo.layout().setContentsMargins(4, 8, 4, 4)

        row_rot = QHBoxLayout()
        for text, tip, slot in [
            ("↺  -90°", "Rotation 90° anti-horaire", self._rotate_ccw),
            ("↻  +90°", "Rotation 90° horaire",       self._rotate_cw),
        ]:
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row_rot.addWidget(btn)
        grp_geo.layout().addLayout(row_rot)

        # Redresser + Recadrer côte à côte
        row_sr = QHBoxLayout()
        row_sr.setSpacing(4)

        btn_straighten = QToolButton()
        btn_straighten.setText("Redresser")
        btn_straighten.setIcon(QIcon(_icon_straighten()))
        btn_straighten.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        btn_straighten.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        btn_straighten.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn_straighten.setFixedHeight(_ICON_SIZE + 28)
        btn_straighten.setToolTip("Corriger l'inclinaison de l'horizon (-10° à +10°)")
        btn_straighten.clicked.connect(
            lambda: self._open_treatment("Redresser", [("Angle (°)", "straighten", -10.0, 10.0, 1)])
        )
        row_sr.addWidget(btn_straighten)

        self._btn_crop = QToolButton()
        self._btn_crop.setText("Recadrer")
        self._btn_crop.setIcon(QIcon(_icon_crop()))
        self._btn_crop.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._btn_crop.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self._btn_crop.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._btn_crop.setFixedHeight(_ICON_SIZE + 28)
        self._btn_crop.setToolTip("Définir interactivement la zone de recadrage")
        self._btn_crop.clicked.connect(self.crop_mode_requested.emit)
        row_sr.addWidget(self._btn_crop)

        grp_geo.layout().addLayout(row_sr)

        row_flip = QHBoxLayout()
        row_flip.setSpacing(4)
        for icon_fn, label, tip, slot in [
            (_icon_flip_h, "Miroir H", "Miroir horizontal", self._flip_h),
            (_icon_flip_v, "Miroir V", "Miroir vertical",   self._flip_v),
        ]:
            btn = QToolButton()
            btn.setText(label)
            btn.setIcon(QIcon(icon_fn()))
            btn.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setFixedHeight(_ICON_SIZE + 28)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row_flip.addWidget(btn)
        grp_geo.layout().addLayout(row_flip)

        inner_layout.addWidget(grp_geo)
        inner_layout.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

    def _make_treatment_button(self, name: str, icon_px: QPixmap,
                                sliders_def: list) -> QToolButton:
        btn = QToolButton()
        btn.setText(name)
        btn.setIcon(QIcon(icon_px))
        btn.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setFixedHeight(_ICON_SIZE + 28)
        btn.clicked.connect(lambda: self._open_treatment(name, sliders_def))
        return btn

    # ------------------------------------------------------------------ dialogues

    def _compute_dialog_pos(self, dw: int, dh: int) -> QPoint:
        """Positionne le dialogue en bas-gauche de la zone image.
        Appelé depuis TreatmentDialog.showEvent — dw/dh sont les dimensions réelles.
        Ancres : _navbar et _toolbar du PhotoViewer (coordonnées exactes, évite
        de couvrir les boutons Précédente/Suivante)."""
        from PySide6.QtWidgets import QSplitter
        margin = 16

        # Naviguer vers PhotoViewer : self → _left_stack → _splitter → _stack → viewer
        splitter = self.parentWidget().parentWidget() if self.parentWidget() else None
        viewer = None
        if isinstance(splitter, QSplitter) and splitter.count() >= 2:
            stack = splitter.widget(1)
            if stack.count() >= 2:
                viewer = stack.widget(1)

        if viewer and hasattr(viewer, '_navbar') and hasattr(viewer, '_toolbar'):
            navbar = viewer._navbar
            vtb   = viewer._toolbar

            # Bas utilisable  = sommet de la barre prev/suivante, moins sa propre hauteur
            nav_tl    = navbar.mapToGlobal(QPoint(0, 0))
            bottom_y  = nav_tl.y() - navbar.height() - margin

            # Haut utilisable = bas de la toolbar du viewer
            top_y = vtb.mapToGlobal(QPoint(0, vtb.height())).y() + margin

            # Gauche/droite depuis la navbar (même largeur que le viewer)
            img_left  = nav_tl.x() + margin
            img_right = nav_tl.x() + navbar.width() - margin
        else:
            # Repli : coin supérieur-droit du panneau gauche
            tr       = self.mapToGlobal(QPoint(self.width(), 0))
            img_left  = tr.x() + margin
            img_right = tr.x() + self.window().width() - self.width() - margin
            bottom_y  = tr.y() + self.height() - margin
            top_y     = tr.y() + margin

        x = img_left
        y = bottom_y - dh
        x = min(x, img_right - dw)
        y = max(y, top_y)
        return QPoint(x, y)

    def _open_treatment(self, title: str, sliders_def: list) -> None:
        if title == "Gamma":
            self._open_gamma_treatment()
            return
        if title == "Couleurs":
            self._open_couleurs_treatment()
            return

        original = copy.copy(self._edit)
        dlg = TreatmentDialog(title, sliders_def, self._edit, parent=self)
        dlg.preview.connect(self._on_preview)
        dlg._panel = self

        is_straighten = (title == "Redresser")
        if is_straighten:
            self.grid_visibility_changed.emit(True)

        if dlg.exec() == QDialog.Accepted:
            self._push_undo()
            new_edit = dlg.get_edit()
            for _, attr, *_ in sliders_def:
                setattr(self._edit, attr, getattr(new_edit, attr))
            self.edits_changed.emit(copy.copy(self._edit))
            self._save(title)
        else:
            self._edit = original
            self.edits_changed.emit(copy.copy(self._edit))

        if is_straighten:
            self.grid_visibility_changed.emit(False)

    def _open_couleurs_treatment(self) -> None:
        original = copy.copy(self._edit)
        dlg = CouleursTreatmentDialog(self._edit, parent=self)
        dlg.preview.connect(self._on_preview)
        dlg._panel = self

        if dlg.exec() == QDialog.Accepted:
            self._push_undo()
            new_edit = dlg.get_edit()
            for attr in ("saturation", "color_red", "color_green", "color_blue"):
                setattr(self._edit, attr, getattr(new_edit, attr))
            self.edits_changed.emit(copy.copy(self._edit))
            self._save("Couleurs")
        else:
            self._edit = original
            self.edits_changed.emit(copy.copy(self._edit))

    def _open_gamma_treatment(self) -> None:
        original = copy.copy(self._edit)
        photo_path = self._photo.path if self._photo else None
        dlg = GammaTreatmentDialog(self._edit, photo_path=photo_path, parent=self)
        dlg.preview.connect(self._on_preview)
        dlg._panel = self

        if dlg.exec() == QDialog.Accepted:
            self._push_undo()
            new_edit = dlg.get_edit()
            self._edit.gamma = new_edit.gamma
            self._edit.gamma_use_curve = new_edit.gamma_use_curve
            self._edit.gamma_curve_points = new_edit.gamma_curve_points
            self.edits_changed.emit(copy.copy(self._edit))
            self._save("Gamma")
        else:
            self._edit = original
            self.edits_changed.emit(copy.copy(self._edit))

    def _on_preview(self, edit: EditInfo) -> None:
        self.edits_changed.emit(edit)

    # ------------------------------------------------------------------ public

    def set_photo(self, photo: PhotoInfo) -> None:
        self._photo = photo
        self._edit = self._db.load(photo.path)
        self._undo_stack = self._db.get_history(photo.path, limit=_UNDO_MAX)
        self._redo_stack.clear()
        self._title_label.setText(f"Retouche — {photo.filename}")

    def get_edit(self) -> EditInfo:
        return copy.copy(self._edit)

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(copy.copy(self._edit))
        self._edit = self._undo_stack.pop()
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("undo")

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(copy.copy(self._edit))
        self._edit = self._redo_stack.pop()
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("redo")

    # ------------------------------------------------------------------ private

    def _save(self, operation: str) -> None:
        if self._photo:
            self._db.save(self._photo.path, self._edit, operation=operation)
            self.photo_saved.emit(self._photo.path, copy.copy(self._edit))

    def _push_undo(self) -> None:
        self._undo_stack.append(copy.copy(self._edit))
        if len(self._undo_stack) > _UNDO_MAX:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _rotate_cw(self) -> None:
        self._push_undo()
        self._edit.rotation = (self._edit.rotation + 90) % 360
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("rotation")
        if self._photo:
            self.rotation_stepped.emit(self._photo.path, self._edit.rotation)

    def _rotate_ccw(self) -> None:
        self._push_undo()
        self._edit.rotation = (self._edit.rotation - 90) % 360
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("rotation")
        if self._photo:
            self.rotation_stepped.emit(self._photo.path, self._edit.rotation)

    def _flip_h(self) -> None:
        self._push_undo()
        self._edit.flip_h = not self._edit.flip_h
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("flip_h")

    def _flip_v(self) -> None:
        self._push_undo()
        self._edit.flip_v = not self._edit.flip_v
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("flip_v")

    def apply_crop(self, quad: tuple) -> None:
        self._push_undo()
        self._edit.crop = quad
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("crop")
