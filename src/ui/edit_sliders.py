# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Editing sliders (extracted from edit_panel.py): _Ruler (graduations),
MarkedSlider (a graduated QSlider + label) and EditSlider (a label + value
line), reused by the treatment dialogs, the main window and the settings."""
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
    QCheckBox, QStyle, QStyleOptionSlider, QButtonGroup,
)

from src.core.models import EditInfo
from src.core.i18n import translate

logger = logging.getLogger(__name__)


class _Ruler(QWidget):
    """Strip of marks (min / zero if within the range / max) under a QSlider."""
    _H = 14

    def __init__(self, slider: QSlider, fmt, parent=None):
        super().__init__(parent)
        self._slider = slider
        self._fmt = fmt
        self.setFixedHeight(self._H)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def _x_for(self, value: int) -> int:
        sl = self._slider
        opt = QStyleOptionSlider()
        sl.initStyleOption(opt)
        groove = sl.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, sl)
        handle = sl.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, sl)
        hw = handle.width() // 2
        avail = max(1, groove.width() - handle.width())
        pos = QStyle.sliderPositionFromValue(sl.minimum(), sl.maximum(), value, avail)
        return groove.x() + hw + pos

    def paintEvent(self, _event):
        sl = self._slider
        mn, mx = sl.minimum(), sl.maximum()
        marks: set[int] = {mn, mx}
        if mn < 0 < mx:
            marks.add(0)

        p = QPainter(self)
        font = QFont()
        font.setPixelSize(9)
        p.setFont(font)
        fm = p.fontMetrics()

        for val in sorted(marks):
            x = self._x_for(val)
            is_zero = (val == 0 and mn < 0 < mx)
            p.setPen(QColor(200, 200, 200) if is_zero else QColor(110, 110, 110))
            p.drawLine(x, 0, x, 5 if is_zero else 3)
            label = self._fmt(val)
            tw = fm.horizontalAdvance(label)
            lx = max(0, min(self.width() - tw, x - tw // 2))
            p.drawText(lx, self._H - 1, label)
        p.end()


class MarkedSlider(QWidget):
    """QSlider with value marks engraved underneath (min / zero if within the range / max)."""
    valueChanged = Signal(int)
    rangeChanged = Signal(int, int)

    def __init__(self, orientation: Qt.Orientation = Qt.Horizontal,
                 fmt=None, parent=None):
        super().__init__(parent)
        self._fmt = fmt or str
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        self._slider = QSlider(orientation)
        vbox.addWidget(self._slider)
        self._ruler = _Ruler(self._slider, self._fmt)
        vbox.addWidget(self._ruler)
        self._slider.valueChanged.connect(self.valueChanged)
        self._slider.rangeChanged.connect(self.rangeChanged)
        self._slider.rangeChanged.connect(lambda *_: self._ruler.update())

    # --- Proxy API QSlider ---
    def value(self) -> int:              return self._slider.value()
    def minimum(self) -> int:            return self._slider.minimum()
    def maximum(self) -> int:            return self._slider.maximum()
    def setValue(self, v: int):          self._slider.setValue(v)
    def setRange(self, a: int, b: int):  self._slider.setRange(a, b)
    def setMinimum(self, v: int):        self._slider.setMinimum(v)
    def setMaximum(self, v: int):        self._slider.setMaximum(v)
    def setSingleStep(self, v: int):     self._slider.setSingleStep(v)
    def setPageStep(self, v: int):       self._slider.setPageStep(v)
    def setTickPosition(self, v):        pass   # replaced by the ruler
    def setTickInterval(self, v: int):   pass   # idem

    def set_double_click_handler(self, handler) -> None:
        self._slider.mouseDoubleClickEvent = handler

    def blockSignals(self, b: bool) -> bool:
        self._slider.blockSignals(b)
        return super().blockSignals(b)


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

        self._name_lbl = QLabel(label)
        self._name_lbl.setFixedWidth(110)
        layout.addWidget(self._name_lbl)

        _fmt = lambda v, s=self._scale, d=self._decimals: f"{v / s:.{d}f}"
        self._slider = MarkedSlider(Qt.Horizontal, fmt=_fmt)
        self._slider.setRange(int(min_val * self._scale), int(max_val * self._scale))
        self._slider.setValue(int(default_val * self._scale))
        self._slider.valueChanged.connect(self._on_changed)
        layout.addWidget(self._slider, stretch=1)

        self._val_lbl = QLabel(self._fmt(default_val))
        self._val_lbl.setFixedWidth(46)
        self._val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._val_lbl)

        # Fine adjustment arrows (step = 1 unit at the last decimal place)
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
        btn_up.setToolTip(translate("EditSlider", "Increase by one step"))
        btn_up.clicked.connect(lambda: self._nudge(self._step_size))
        arrows.addWidget(btn_up)
        btn_dn = QPushButton("▼")
        btn_dn.setStyleSheet(_arrow_style)
        btn_dn.setToolTip(translate("EditSlider", "Decrease by one step"))
        btn_dn.clicked.connect(lambda: self._nudge(-self._step_size))
        arrows.addWidget(btn_dn)
        layout.addLayout(arrows)

        self._slider.set_double_click_handler(lambda _e: (
            self.set_value(self._default),
            self.value_changed.emit(self._default),
        ))

    def set_label(self, label: str) -> None:
        """Changes the label (one same slider can serve several settings)."""
        self._name_lbl.setText(label)

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


# ------------------------------------------------------------------ treatment dialog

