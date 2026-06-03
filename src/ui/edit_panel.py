import copy
import logging
import math

from PySide6.QtCore import Qt, Signal, QSize, QPoint, QTimer
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QFont, QPen, QIcon,
    QPolygon, QBrush, QLinearGradient,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QScrollArea, QGroupBox, QDialog,
    QDialogButtonBox, QToolButton, QGridLayout, QSizePolicy,
    QToolBar,
)

from src.core.models import PhotoInfo, EditInfo
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


# ------------------------------------------------------------------ panneau principal

# (label, icône_fn, sliders_def)
_TREATMENTS: list[tuple] = [
    ("Luminosité",  _icon_brightness, [("Luminosité",  "brightness",      -1.0, 1.0, 2)]),
    ("Contraste",   _icon_contrast,   [("Contraste",   "contrast",        -1.0, 1.0, 2)]),
    ("Saturation",  _icon_saturation, [("Saturation",  "saturation",      -1.0, 1.0, 2)]),
    ("Gamma",       _icon_gamma,      [("Gamma",       "gamma",            0.1, 3.0, 2)]),
    ("Netteté",     _icon_sharpness,  [("Netteté",     "sharpness",        0.0, 1.0, 2)]),
    ("Débruitage",  _icon_noise,      [("Débruitage",  "noise_reduction",  0.0, 1.0, 2)]),
]


class EditPanel(QWidget):
    edits_changed       = Signal(object)  # EditInfo
    crop_mode_requested = Signal()

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
        btn_straighten.setToolTip("Corriger l'inclinaison de l'horizon (-45° à +45°)")
        btn_straighten.clicked.connect(
            lambda: self._open_treatment("Redresser", [("Angle (°)", "straighten", -45.0, 45.0, 1)])
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
        original = copy.copy(self._edit)
        dlg = TreatmentDialog(title, sliders_def, self._edit, parent=self)
        dlg.preview.connect(self._on_preview)
        dlg._panel = self

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

    def _rotate_ccw(self) -> None:
        self._push_undo()
        self._edit.rotation = (self._edit.rotation - 90) % 360
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("rotation")

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
