# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Treatment dialogs of the editing panel (extracted from
edit_panel.py): the generic TreatmentDialog, GammaCurveWidget and the
Brightness / Colours / Vignette dialogs."""
import copy
import logging
import math

from PySide6.QtCore import Qt, Signal, QSize, QPoint, QTimer
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QFont, QPen,
    QPolygon, QBrush, QLinearGradient, QPainterPath,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QDialog, QDialogButtonBox, QGridLayout, QSizePolicy,
    QCheckBox, QStyle, QStyleOptionSlider, QButtonGroup, QGroupBox,
)

from src.core.models import EditInfo
from src.processing.adjustments import ImageAdjuster

logger = logging.getLogger(__name__)

from src.ui.edit_sliders import EditSlider, MarkedSlider  # noqa: E402
from src.ui.edit_icons import (  # noqa: E402
    _icon_brightness, _icon_contrast, _icon_frame, _icon_saturation,
    _icon_vignette, _TOGGLE_BTN_STYLE,
)
from src.core.i18n import translate

class TreatmentDialog(QDialog):
    preview = Signal(object)  # live EditInfo

    def __init__(self, title: str, sliders_def: list, edit: EditInfo, parent=None):
        """
        sliders_def : list of (label, attr_name, min, max, decimals)
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(720)
        self._edit = copy.copy(edit)
        self._sliders: dict[str, EditSlider] = {}
        self._panel = None   # reference to EditPanel, set in showEvent

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        for label, attr, mn, mx, decimals in sliders_def:
            sl = EditSlider(label, mn, mx, getattr(edit, attr), decimals)
            self._sliders[attr] = sl
            sl.value_changed.connect(lambda v, a=attr: self._on_changed(a, v))
            layout.addWidget(sl)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText(translate("TreatmentDialog", "Apply"))
        btn_box.button(QDialogButtonBox.Cancel).setText(translate("TreatmentDialog", "Cancel"))
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._panel is not None:
            # The real dimensions are available here.
            # QTimer.singleShot(0) defers the move() until AFTER Windows has finished
            # any asynchronous repositioning (adjustPosition, WM_WINDOWPOSCHANGED…).
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            QTimer.singleShot(0, lambda: self.move(pos))

    def _on_changed(self, attr: str, value: float) -> None:
        setattr(self._edit, attr, value)
        self.preview.emit(copy.copy(self._edit))

    def get_edit(self) -> EditInfo:
        return self._edit


# ------------------------------------------------------------------ gamma curve


def _compute_luminosity_histogram(photo_path: str) -> list[float]:
    """Returns 256 normalised (log) values of the brightness histogram."""
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

        # brightness histogram (a semi-transparent silhouette)
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

        # grid
        p.setPen(QPen(QColor(55, 55, 75), 1))
        for i in range(1, 4):
            p.drawLine(x0 + i * w // 4, y0, x0 + i * w // 4, y0 + h)
            p.drawLine(x0, y0 + i * h // 4, x0 + w, y0 + i * h // 4)

        # identity diagonal
        p.setPen(QPen(QColor(75, 75, 100), 1, Qt.DashLine))
        p.drawLine(x0, y0 + h, x0 + w, y0)

        # interpolated curve
        lut = ImageAdjuster._curve_lut(self._points)
        p.setPen(QPen(QColor(140, 140, 255), 2))
        prev = None
        for k, yv in enumerate(lut):
            wx, wy = self._to_widget(k / 255.0, yv / 255.0)
            if prev:
                p.drawLine(prev[0], prev[1], wx, wy)
            prev = (wx, wy)

        # border
        p.setPen(QPen(QColor(75, 75, 100), 1))
        p.drawRect(x0, y0, w, h)

        # control points
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
                # avoid x values too close to the existing points
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
            # the endpoints stay fixed in x
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

    # -- public API

    def set_from_gamma(self, gamma: float) -> None:
        self._points = _gamma_to_curve_points(gamma)
        self.update()
        self.curve_changed.emit(list(self._points))

    def get_points(self) -> list:
        return list(self._points)

    def set_points(self, points: list) -> None:
        self._points = sorted(points, key=lambda pt: pt[0])
        self.update()


# ------------------------------------------------------------------ brightness dialog (+ advanced gamma)

class LuminositeTreatmentDialog(QDialog):
    preview = Signal(object)

    def __init__(self, edit: EditInfo, photo_path: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate("LuminositeTreatmentDialog", "Brightness"))
        self.setMinimumWidth(400)
        self._edit = copy.copy(edit)
        self._panel = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Main brightness slider
        self._sl_lum = EditSlider(translate("LuminositeTreatmentDialog", "Brightness"),
                                  -1.0, 1.0, edit.brightness, 2)
        self._sl_lum.value_changed.connect(lambda v: self._on_changed("brightness", v))
        layout.addWidget(self._sl_lum)

        # "Advanced functions…" checkbox (gamma)
        self._chk = QCheckBox(translate("LuminositeTreatmentDialog", "Advanced options…"))
        has_gamma = edit.gamma != 1.0 or edit.gamma_use_curve
        self._chk.setChecked(has_gamma)
        self._chk.toggled.connect(self._on_advanced_toggled)
        layout.addWidget(self._chk)

        # Gamma section (hidden if there is no gamma editing)
        self._adv = QWidget()
        adv_layout = QVBoxLayout(self._adv)
        adv_layout.setContentsMargins(0, 4, 0, 0)
        adv_layout.setSpacing(4)

        self._gamma_slider = EditSlider("Gamma", 0.1, 3.0, edit.gamma, 2)
        self._gamma_slider.value_changed.connect(self._on_gamma_changed)
        adv_layout.addWidget(self._gamma_slider)

        self._chk_curve = QCheckBox(translate("LuminositeTreatmentDialog", "Expert options…"))
        self._chk_curve.setChecked(edit.gamma_use_curve)
        self._chk_curve.toggled.connect(self._on_curve_toggled)
        adv_layout.addWidget(self._chk_curve)

        # Curve section (hidden by default)
        self._curve_section = QWidget()
        cs_layout = QVBoxLayout(self._curve_section)
        cs_layout.setContentsMargins(0, 0, 0, 0)
        cs_layout.setSpacing(4)

        lbl = QLabel(
            translate("LuminositeTreatmentDialog", "Click to add a point · Drag to move · "
                                                   "Right-click to remove")
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #999; font-size: 10px;")
        cs_layout.addWidget(lbl)

        histogram = _compute_luminosity_histogram(photo_path) if photo_path else []
        init_pts = edit.gamma_curve_points if edit.gamma_use_curve else _gamma_to_curve_points(edit.gamma)
        self._curve = GammaCurveWidget(points=init_pts, histogram=histogram)
        self._curve.curve_changed.connect(self._on_curve_changed)
        cs_layout.addWidget(self._curve)
        adv_layout.addWidget(self._curve_section)

        # Initial visibility
        self._gamma_slider.setVisible(not edit.gamma_use_curve)
        self._curve_section.setVisible(edit.gamma_use_curve)
        self._edit.gamma_curve_points = init_pts

        layout.addWidget(self._adv)
        self._adv.setVisible(has_gamma)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText(translate("LuminositeTreatmentDialog", "Apply"))
        btn_box.button(QDialogButtonBox.Cancel).setText(translate("LuminositeTreatmentDialog", "Cancel"))
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

    def _on_gamma_changed(self, value: float) -> None:
        self._edit.gamma = value
        self.preview.emit(copy.copy(self._edit))

    def _on_advanced_toggled(self, checked: bool) -> None:
        self._adv.setVisible(checked)
        self.adjustSize()
        QTimer.singleShot(0, self._reposition)

    def _on_curve_toggled(self, checked: bool) -> None:
        self._gamma_slider.setVisible(not checked)
        self._curve_section.setVisible(checked)
        self._edit.gamma_use_curve = checked
        if checked:
            self._curve.set_from_gamma(self._edit.gamma)
        self.adjustSize()
        QTimer.singleShot(0, self._reposition)
        self.preview.emit(copy.copy(self._edit))

    def _on_curve_changed(self, points: list) -> None:
        self._edit.gamma_curve_points = points
        self.preview.emit(copy.copy(self._edit))

    def _reposition(self) -> None:
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            self.move(pos)

    def get_edit(self) -> EditInfo:
        return self._edit


# ------------------------------------------------------------------ colours dialog


class CouleursTreatmentDialog(QDialog):
    preview          = Signal(object)  # EditInfo
    wb_pick_requested = Signal(bool)   # True = start the eyedropper, False = cancel

    def __init__(self, edit: EditInfo, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate("CouleursTreatmentDialog", "Colours"))
        self.setMinimumWidth(720)
        self._edit = copy.copy(edit)
        self._panel = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Global saturation slider
        self._sl_sat = EditSlider(translate("CouleursTreatmentDialog", "Saturation"),
                                  -1.0, 1.0, edit.saturation, 2)
        self._sl_sat.value_changed.connect(lambda v: self._on_changed("saturation", v))
        layout.addWidget(self._sl_sat)

        # Advanced checkbox
        self._chk = QCheckBox(translate("CouleursTreatmentDialog", "Advanced options…"))
        has_channel_edits = any(v != 0.0 for v in (edit.color_red, edit.color_green, edit.color_blue))
        self._chk.setChecked(has_channel_edits)
        layout.addWidget(self._chk)

        # RGB section (hidden by default)
        self._adv = QWidget()
        adv_layout = QVBoxLayout(self._adv)
        adv_layout.setContentsMargins(0, 4, 0, 0)
        adv_layout.setSpacing(4)

        lbl = QLabel(translate("CouleursTreatmentDialog", "Per-channel colour adjustment"))
        lbl.setStyleSheet("color: #999; font-size: 10px;")
        adv_layout.addWidget(lbl)

        # --- White balance eyedropper ---
        pip_row = QHBoxLayout()
        pip_row.setContentsMargins(0, 4, 0, 0)
        self._btn_pip = QPushButton(translate("CouleursTreatmentDialog", "⌖  White balance "
                                                                         "eyedropper"))
        self._btn_pip.setCheckable(True)
        self._btn_pip.setToolTip(
            translate("CouleursTreatmentDialog", "Click a neutral area (white or grey) in the "
                                                 "image\nto balance the R, G, B channels "
                                                 "automatically.")
        )
        pip_row.addWidget(self._btn_pip)
        self._lbl_pip_hint = QLabel(translate("CouleursTreatmentDialog", "→ Click a neutral "
                                                                         "point in the main "
                                                                         "image"))
        self._lbl_pip_hint.setStyleSheet("color: #7ab; font-size: 10px;")
        self._lbl_pip_hint.hide()
        pip_row.addWidget(self._lbl_pip_hint, stretch=1)
        adv_layout.addLayout(pip_row)

        # Feedback swatch (the picked colour)
        swatch_row = QHBoxLayout()
        self._wb_swatch_lbl = QLabel(translate("CouleursTreatmentDialog", "Sampled colour:"))
        self._wb_swatch_lbl.setStyleSheet("color: #888; font-size: 10px;")
        self._wb_swatch_lbl.hide()
        self._wb_swatch = QLabel()
        self._wb_swatch.setFixedSize(44, 16)
        self._wb_swatch.setStyleSheet("border: 1px solid #666;")
        self._wb_swatch.hide()
        swatch_row.addWidget(self._wb_swatch_lbl)
        swatch_row.addWidget(self._wb_swatch)
        swatch_row.addStretch()
        adv_layout.addLayout(swatch_row)

        self._btn_pip.toggled.connect(self._on_pip_toggled)

        # RGB sliders
        self._sl_r = EditSlider(translate("CouleursTreatmentDialog", "Red"),
                                -1.0, 1.0, edit.color_red,   2)
        self._sl_g = EditSlider(translate("CouleursTreatmentDialog", "Green"),
                                -1.0, 1.0, edit.color_green, 2)
        self._sl_b = EditSlider(translate("CouleursTreatmentDialog", "Blue"),
                                -1.0, 1.0, edit.color_blue,  2)
        for sl, attr in [
            (self._sl_r, "color_red"),
            (self._sl_g, "color_green"),
            (self._sl_b, "color_blue"),
        ]:
            sl.value_changed.connect(lambda v, a=attr: self._on_changed(a, v))
            adv_layout.addWidget(sl)

        self._adv.setVisible(has_channel_edits)
        self._chk.toggled.connect(self._adv.setVisible)
        self._chk.toggled.connect(lambda _: self._resize_and_reposition())
        layout.addWidget(self._adv)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText(translate("CouleursTreatmentDialog", "Apply"))
        btn_box.button(QDialogButtonBox.Cancel).setText(translate("CouleursTreatmentDialog", "Cancel"))
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _on_pip_toggled(self, checked: bool) -> None:
        self._lbl_pip_hint.setVisible(checked)
        self.wb_pick_requested.emit(checked)

    def apply_wb_pixel(self, r: int, g: int, b: int) -> None:
        """Applies the white balance correction from the pixel picked in the viewer."""
        if r == 0 and g == 0 and b == 0:
            return
        mean = (r + g + b) / 3.0
        def _corr(ch: int) -> float:
            return max(-1.0, min(1.0, mean / ch - 1.0)) if ch > 0 else 0.0
        cr, cg, cb = _corr(r), _corr(g), _corr(b)
        self._sl_r.set_value(cr)
        self._sl_g.set_value(cg)
        self._sl_b.set_value(cb)
        self._edit.color_red   = cr
        self._edit.color_green = cg
        self._edit.color_blue  = cb
        self.preview.emit(copy.copy(self._edit))
        # Disable the eyedropper button (without re-emitting the signal)
        self._btn_pip.blockSignals(True)
        self._btn_pip.setChecked(False)
        self._btn_pip.blockSignals(False)
        self._lbl_pip_hint.hide()
        # Feedback: swatch of the picked colour
        self._wb_swatch.setStyleSheet(f"background: rgb({r},{g},{b}); border: 1px solid #666;")
        self._wb_swatch.setToolTip(
            translate("CouleursTreatmentDialog", "Sampled pixel — R: {r}  G: {g}  B: {b}"
                      ).format(r=r, g=g, b=b))
        self._wb_swatch_lbl.show()
        self._wb_swatch.show()
        # Show the advanced section if it is hidden
        if not self._adv.isVisible():
            self._chk.setChecked(True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            QTimer.singleShot(0, lambda: self.move(pos))

    def _resize_and_reposition(self) -> None:
        self.adjustSize()
        QTimer.singleShot(0, self._reposition)

    def _reposition(self) -> None:
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            self.move(pos)

    def _on_changed(self, attr: str, value: float) -> None:
        setattr(self._edit, attr, value)
        self.preview.emit(copy.copy(self._edit))

    def get_edit(self) -> EditInfo:
        return self._edit




# Strength applied when the tool is opened on a photo that has no vignette
# yet (`vignette_strength` == 0): at 0, the tool opened on a strictly
# invisible effect and the slider had to be moved to see anything at all.
# The default value of `EditInfo.vignette_strength` stays 0 — a photo with no
# saved vignette must be displayed without one; it really is opening the tool
# that sets this starting point, cf. `edit_panel._open_vignette_treatment`
# (cancelling → back to 0).
VIGNETTE_DEFAULT_STRENGTH = 0.5


class VignetteTreatmentDialog(QDialog):
    preview = Signal(object)   # live EditInfo

    def __init__(self, edit: EditInfo, parent=None) -> None:
        super().__init__(parent)
        self._edit = copy.copy(edit)
        self._panel = None
        self.setWindowTitle(translate("VignetteTreatmentDialog", "Vignette"))
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(380)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 10)

        # ---- Strength ----
        self._sl_strength = EditSlider(translate("VignetteTreatmentDialog", "Strength"),
                                       0.0, 1.0, self._edit.vignette_strength, 2)
        self._sl_strength.value_changed.connect(lambda v: self._on_changed("vignette_strength", v))
        layout.addWidget(self._sl_strength)

        # ---- Colour ----
        color_grp = QGroupBox(translate("VignetteTreatmentDialog", "Colour"))
        color_row = QHBoxLayout(color_grp)
        color_row.setSpacing(6)

        self._btn_black = QPushButton(translate("VignetteTreatmentDialog", "Black"))
        self._btn_white = QPushButton(translate("VignetteTreatmentDialog", "White"))
        for btn in (self._btn_black, self._btn_white):
            btn.setCheckable(True)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            color_row.addWidget(btn)

        self._btn_black.setChecked(self._edit.vignette_color == "black")
        self._btn_white.setChecked(self._edit.vignette_color == "white")
        self._btn_black.clicked.connect(lambda: self._set_color("black"))
        self._btn_white.clicked.connect(lambda: self._set_color("white"))
        layout.addWidget(color_grp)

        # ---- Instructions ----
        hint = QLabel(
            translate("VignetteTreatmentDialog", "Drag the handles on the image:\n• Inner "
                                                 "circle (dotted) — where the fade starts\n• "
                                                 "Outer circle — where the fade ends\n• Round "
                                                 "handle at the top — rotate\n• Cross in the "
                                                 "middle — move")
        )
        hint.setStyleSheet("color: #999; font-size: 10px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ---- OK / Cancel buttons ----
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText(translate("VignetteTreatmentDialog", "Apply"))
        btn_box.button(QDialogButtonBox.Cancel).setText(translate("VignetteTreatmentDialog", "Cancel"))
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

    def _set_color(self, color: str) -> None:
        self._edit.vignette_color = color
        self._btn_black.setChecked(color == "black")
        self._btn_white.setChecked(color == "white")
        self.preview.emit(copy.copy(self._edit))

    def update_from_edit(self, edit: EditInfo) -> None:
        self._edit = copy.copy(edit)
        self._sl_strength.set_value(edit.vignette_strength)
        self._btn_black.setChecked(edit.vignette_color == "black")
        self._btn_white.setChecked(edit.vignette_color == "white")

    def get_edit(self) -> EditInfo:
        return self._edit


# ------------------------------------------------------------------ main panel

# (internal name, icon_fn, sliders_def)
# The 1st element is a KEY, not a label: it identifies the tool (the button
# dictionary, the dispatch of _open_treatment, the operation name persisted in
# edits.db). It therefore stays in French whatever the language — the display
# goes through edit_panel._tool_label(). The slider labels, by contrast, are
# nothing but display and are translated here.
_TREATMENTS: list[tuple] = [
    ("Luminosité", _icon_brightness,
     [(translate("EditPanel", "Brightness"), "brightness", -1.0, 1.0, 2)]),
    ("Contraste",  _icon_contrast,
     [(translate("EditPanel", "Contrast"),  "contrast",   -1.0, 1.0, 2)]),
    ("Couleurs",   _icon_saturation,
     [(translate("EditPanel", "Saturation"), "saturation", -1.0, 1.0, 2)]),
    ("Vignette",   _icon_vignette,   []),   # dedicated dialog — sliders_def ignored
    ("Cadre",      _icon_frame,      []),   # dedicated dialog — cf. frame_dialog.py
]

# The same highlight as the Annotations button: visible around the icon as
# long as the tool is active (interactive canvas mode or settings dialog open).
_ACTIVE_TOOL_STYLE = (
    "QToolButton { background: #1a2a3a; border: 1px solid #2080a0; border-radius: 4px; }"
)


