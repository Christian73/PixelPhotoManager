# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
""""Frame" dialog of the editing panel.

Presents a gallery of previews of THE photo in progress, one per frame pattern,
so as to choose on the real thing rather than on a name. The previews are
rendered in a QThread (the "the UI never blocks" rule): the photo is decoded and
reduced only once, then each thumbnail is nothing more than a frame render
(~10 ms).

The thickness is adjustable for EVERY pattern: a carved frame only exists from a
certain width on (below ~8 % of the short side, the frieze fits in a few pixels).
Choosing a decorative pattern therefore raises the width to
``frames.DECOR_MIN_WIDTH`` — visibly, in the slider, and the gallery renders its
previews with the same rule: the thumbnail shows exactly what clicking on it
produces. The colours and the fill style, by contrast, stay specific to the
parametric frames (plain surround, simple, double).

The thumbnails concerned are re-rendered at every change, deferred so as not to
start one render per slider step.

The plain surround offers an optional second frame, painted on top of the photo
(a checkbox): it is the only SETTING of the dialog that covers part of the image,
hence the explicit activation. (The three foliage frames spill over the photo
too, but with no setting: that is part of the pattern, cf.
``frames.SPILL_FRAMES``.)
"""
import copy
import logging

from PySide6.QtCore import Qt, QSize, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QDialog, QDialogButtonBox,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from src.core.models import EditInfo
from src.processing.frames import (
    COLOR_STYLES, FRAME_TYPES, INNER_MOTIFS, INNER_ORNAMENT_MAX,
    INNER_ORNAMENT_MIN, INNER_RELIEFS, ORNAMENTED_MOTIFS, PARAMETRIC_FRAMES,
    QUICK_COLORS, STYLED_FRAMES, suggested_width,
)
from src.ui.edit_icons import _TOGGLE_BTN_STYLE
from src.ui.edit_sliders import EditSlider
from src.core.i18n import translate

logger = logging.getLogger(__name__)

# Side of the photo preview (the frame is added around it). Large enough for a
# carved frieze to stay legible in the gallery — below ~150 px, the acanthus
# leaves and the egg-and-dart shrink to an indistinct texture and the choice is
# made blind.
_TILE_PX = 150
_TILE_COLS = 4
_PREVIEW_DEBOUNCE_MS = 180

_SELECTED_TILE_STYLE = (
    "QToolButton { background: #1a2a3a; border: 1px solid #2080a0; border-radius: 4px; }"
)
_TILE_STYLE = "QToolButton { border: 1px solid #3a3a3a; border-radius: 4px; }"


def _pil_to_qimage(img) -> QImage:
    """PIL → QImage conversion usable outside the UI thread (a detached copy)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    # .copy(): QImage does not take ownership of the Python buffer, which would
    # be freed as soon as this function returns.
    return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()


class _TileLoader(QThread):
    """Renders the framed thumbnails, off the UI thread.

    ``base`` (a PIL image already reduced and edited, without a frame) is reused
    from one launch to the next: only the first render pays for decoding the
    file."""

    tile_ready = Signal(str, QImage)   # (pattern id, preview)
    base_ready = Signal(object)        # base PIL image, for the following renders

    def __init__(self, photo_path: str, edit: EditInfo, kinds: list,
                 base=None, parent=None) -> None:
        super().__init__(parent)
        self._photo_path = photo_path
        self._edit = copy.copy(edit)
        self._kinds = list(kinds)
        self._base = base
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _make_base(self):
        from PIL import Image, ImageOps
        from src.library.image_loader import open_image
        from src.processing.adjustments import ImageAdjuster

        with open_image(self._photo_path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((_TILE_PX * 3, _TILE_PX * 3), Image.LANCZOS)
            img = img.convert("RGB")
            base = copy.copy(self._edit)
            base.frame_type = "none"
            if base.is_modified():
                img = ImageAdjuster.apply_all(img, base)
            img.thumbnail((_TILE_PX, _TILE_PX), Image.LANCZOS)
            return img

    def run(self) -> None:
        from src.processing.frames import apply_frame
        try:
            base = self._base
            if base is None:
                base = self._make_base()
                self.base_ready.emit(base)
        except Exception as e:
            logger.error("Aperçu des cadres — image de base illisible (%s) : %s",
                         self._photo_path, e)
            return
        for kind in self._kinds:
            if self._cancelled:
                return
            try:
                if kind == "none":
                    img = base
                else:
                    e = copy.copy(self._edit)
                    e.frame_type = kind
                    # The same width raise as on selection: the preview must show
                    # what clicking on it produces.
                    e.frame_width = suggested_width(kind, e.frame_width)
                    img = apply_frame(base, e)
                self.tile_ready.emit(kind, _pil_to_qimage(img))
            except Exception as exc:
                logger.error("Aperçu du cadre %s impossible : %s", kind, exc)


class _ColorButton(QPushButton):
    """Clickable colour swatch (opens the colour picker)."""

    color_changed = Signal(str)

    def __init__(self, color: str, tooltip: str, parent=None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(30, 24)
        self.setToolTip(tooltip)
        self.setCursor(Qt.PointingHandCursor)
        self._refresh()
        self.clicked.connect(self._pick)

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"QPushButton {{ background: {self._color}; border: 1px solid #777;"
            f" border-radius: 3px; }}"
        )

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color
        self._refresh()

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, self.toolTip())
        if chosen.isValid():
            self.set_color(chosen.name())
            self.color_changed.emit(self._color)


class FrameDialog(QDialog):
    """Choice of a decorative frame and its settings, with live previews."""

    preview = Signal(object)   # live EditInfo

    def __init__(self, edit: EditInfo, photo_path: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self._edit = copy.copy(edit)
        self._photo_path = photo_path
        self._panel = None            # EditPanel — positioning, cf. showEvent
        self._tiles: dict[str, QToolButton] = {}
        self._loader: _TileLoader | None = None
        self._base_image = None
        self._pending_kinds: list[str] = []
        self._dirty_kinds: set[str] = set()

        self.setWindowTitle(translate("FrameDialog", "Frame"))
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(700)

        # A single render after a burst of slider movements.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._refresh_timer.timeout.connect(self._refresh_parametric_tiles)

        self._setup_ui()
        QTimer.singleShot(0, self._start_gallery)

    # ------------------------------------------------------------------ UI

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 10)

        hint = QLabel(translate("FrameDialog", "Pick a frame — it sits around the photo "
                                               "without covering the image (except the second "
                                               "frame of the flat surround, which you enable "
                                               "explicitly)."))
        hint.setStyleSheet("color: #999; font-size: 10px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ---- Gallery ----
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setSpacing(6)
        for idx, (kind, label) in enumerate(FRAME_TYPES):
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIconSize(QSize(_TILE_PX, _TILE_PX))
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setStyleSheet(_TILE_STYLE)
            btn.clicked.connect(lambda _checked=False, k=kind: self._select_kind(k))
            self._group.addButton(btn)
            self._tiles[kind] = btn
            grid.addWidget(btn, idx // _TILE_COLS, idx % _TILE_COLS)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_host)
        scroll.setMinimumHeight(_TILE_PX + 76)
        layout.addWidget(scroll, stretch=1)

        # ---- Settings of the parametric frames ----
        self._params = QGroupBox(translate("FrameDialog", "Frame settings"))
        pl = QVBoxLayout(self._params)
        pl.setContentsMargins(8, 10, 8, 8)
        pl.setSpacing(6)

        # Colours and fill style: reserved for the parametric frames
        # (a carved frame has the material of its pattern — gold, walnut, lacquer…).
        self._style_row_host = QWidget()
        style_row = QHBoxLayout(self._style_row_host)
        style_row.setContentsMargins(0, 0, 0, 0)
        style_row.setSpacing(6)
        style_row.addWidget(QLabel(translate("FrameDialog", "Colour:")))
        self._style_buttons: dict[str, QPushButton] = {}
        for style_id, style_label in COLOR_STYLES:
            btn = QPushButton(style_label)
            btn.setCheckable(True)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setChecked(self._edit.frame_style == style_id)
            btn.clicked.connect(lambda _checked=False, s=style_id: self._set_style(s))
            self._style_buttons[style_id] = btn
            style_row.addWidget(btn)

        self._btn_color = _ColorButton(self._edit.frame_color,
                                       translate("FrameDialog", "Main colour"))
        self._btn_color.color_changed.connect(
            lambda c: self._set_attr("frame_color", c, reload_tiles=True))
        style_row.addWidget(self._btn_color)
        self._btn_color2 = _ColorButton(
            self._edit.frame_color2,
            translate("FrameDialog", "Second colour (gradient, glitter flecks)"))
        self._btn_color2.color_changed.connect(
            lambda c: self._set_attr("frame_color2", c, reload_tiles=True))
        style_row.addWidget(self._btn_color2)
        # Black / white shortcuts: the two most common surrounds, without going
        # through the colour picker.
        for hex_value, label in QUICK_COLORS:
            btn = QPushButton(label)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setToolTip(translate("FrameDialog", "{color} surround"
                                     ).format(color=label.lower()))
            btn.clicked.connect(
                lambda _checked=False, h=hex_value: self._set_main_color(h))
            style_row.addWidget(btn)
        style_row.addStretch()
        pl.addWidget(self._style_row_host)

        # The widths are fractions of the short side of the photo, exposed as a
        # percentage (0.1 % of precision — 2 decimals on a fraction would only give
        # a step of 1 %, far too coarse).
        self._sl_width = EditSlider(translate("FrameDialog", "Outer frame"), 0.5, 25.0,
                                    self._edit.frame_width * 100.0, 1)
        self._sl_width.value_changed.connect(
            lambda v: self._set_attr("frame_width", v / 100.0, reload_tiles=True,
                                     tiles=[k for k, _ in FRAME_TYPES]))
        pl.addWidget(self._sl_width)

        # Second frame of the plain surround: optional (it encroaches on the photo,
        # which is not an acceptable default without an explicit gesture).
        self._chk_inner = QCheckBox(translate("FrameDialog", "Second frame over the photo"))
        self._chk_inner.setToolTip(
            translate("FrameDialog", "Adds an inner frame painted onto the image; a strip of "
                                     "the photo\nstays visible between the two frames.")
        )
        self._chk_inner.setChecked(bool(self._edit.frame_inner_enabled))
        self._chk_inner.toggled.connect(self._set_inner_enabled)
        pl.addWidget(self._chk_inner)

        # Gap + thickness: shared by the double frame and the second frame of the
        # plain surround (the same settings, with adapted labels).
        self._inner_rows = QWidget()
        dl = QVBoxLayout(self._inner_rows)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(6)

        self._sl_gap = EditSlider(translate("FrameDialog", "Gap"), 0.0, 15.0,
                                  self._edit.frame_gap * 100.0, 1)
        self._sl_gap.value_changed.connect(
            lambda v: self._set_attr("frame_gap", v / 100.0, reload_tiles=True))
        dl.addWidget(self._sl_gap)

        self._sl_inner = EditSlider(translate("FrameDialog", "Inner frame"), 0.0, 15.0,
                                    self._edit.frame_inner_width * 100.0, 1)
        self._sl_inner.value_changed.connect(
            lambda v: self._set_attr("frame_inner_width", v / 100.0, reload_tiles=True))
        dl.addWidget(self._sl_inner)

        # Ironwork of the second frame: motif, rendering (relief / flat fill) and
        # size of the ornaments. Settings specific to the plain surround.
        self._inner_motif_rows = QWidget()
        il = QVBoxLayout(self._inner_motif_rows)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(6)

        motif_row = QHBoxLayout()
        motif_row.setSpacing(6)
        motif_row.addWidget(QLabel(translate("FrameDialog", "Ironwork:")))
        self._motif_buttons: dict[str, QPushButton] = {}
        for motif_id, motif_label in INNER_MOTIFS:
            btn = QPushButton(motif_label)
            btn.setCheckable(True)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setChecked(self._edit.frame_inner_motif == motif_id)
            btn.clicked.connect(
                lambda _checked=False, m=motif_id: self._set_inner_motif(m))
            self._motif_buttons[motif_id] = btn
            motif_row.addWidget(btn)
        motif_row.addStretch()
        il.addLayout(motif_row)

        # Relief or flat fill: an ironwork setting, with no effect on the simple
        # line (rendered as a strict flat fill, cf. frames._draw_inner_overlay) —
        # the row is therefore hidden along with the ornaments slider.
        self._relief_row = QWidget()
        relief_row = QHBoxLayout(self._relief_row)
        relief_row.setContentsMargins(0, 0, 0, 0)
        relief_row.setSpacing(6)
        relief_row.addWidget(QLabel(translate("FrameDialog", "Rendering:")))
        self._relief_buttons: dict[bool, QPushButton] = {}
        for relief_value, relief_label in INNER_RELIEFS:
            btn = QPushButton(relief_label)
            btn.setCheckable(True)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setChecked(bool(self._edit.frame_inner_relief) == relief_value)
            btn.clicked.connect(
                lambda _checked=False, r=relief_value: self._set_inner_relief(r))
            self._relief_buttons[relief_value] = btn
            relief_row.addWidget(btn)
        relief_row.addStretch()
        il.addWidget(self._relief_row)

        # Scale of the ornaments, as a percentage (the internal scale of EditSlider
        # is hard-wired to 100: a factor of 0.4-2.5 is therefore set from 40 to 250 %).
        self._sl_ornament = EditSlider(translate("FrameDialog", "Ornaments"),
                                       INNER_ORNAMENT_MIN * 100.0,
                                       INNER_ORNAMENT_MAX * 100.0,
                                       self._edit.frame_inner_ornament * 100.0, 0)
        self._sl_ornament.value_changed.connect(
            lambda v: self._set_attr("frame_inner_ornament", v / 100.0, reload_tiles=True))
        il.addWidget(self._sl_ornament)

        self._double_rows = QWidget()          # colours specific to the double frame
        dcl = QVBoxLayout(self._double_rows)
        dcl.setContentsMargins(0, 0, 0, 0)
        dcl.setSpacing(6)

        inner_colors = QHBoxLayout()
        inner_colors.setSpacing(6)
        inner_colors.addWidget(QLabel(translate("FrameDialog", "Gap:")))
        self._btn_gap_color = _ColorButton(
            self._edit.frame_gap_color,
            translate("FrameDialog", "Gap colour"))
        self._btn_gap_color.color_changed.connect(
            lambda c: self._set_attr("frame_gap_color", c, reload_tiles=True))
        inner_colors.addWidget(self._btn_gap_color)
        inner_colors.addSpacing(12)
        inner_colors.addWidget(QLabel(translate("FrameDialog", "Inner frame:")))
        self._btn_inner_color = _ColorButton(
            self._edit.frame_inner_color,
            translate("FrameDialog", "Inner frame colour"))
        self._btn_inner_color.color_changed.connect(
            lambda c: self._set_attr("frame_inner_color", c, reload_tiles=True))
        inner_colors.addWidget(self._btn_inner_color)
        inner_colors.addStretch()
        dcl.addLayout(inner_colors)

        pl.addWidget(self._inner_rows)
        pl.addWidget(self._inner_motif_rows)
        pl.addWidget(self._double_rows)
        layout.addWidget(self._params)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText(translate("FrameDialog", "Apply"))
        btn_box.button(QDialogButtonBox.Cancel).setText(translate("FrameDialog", "Cancel"))
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        current = self._edit.frame_type if self._edit.frame_type in self._tiles else "none"
        self._tiles[current].setChecked(True)
        self._apply_tile_styles(current)
        self._update_params_visibility()

    def _apply_tile_styles(self, selected: str) -> None:
        for kind, btn in self._tiles.items():
            btn.setStyleSheet(_SELECTED_TILE_STYLE if kind == selected else _TILE_STYLE)

    # ------------------------------------------------------------------ previews

    def _start_gallery(self, kinds: list | None = None) -> None:
        """Starts (or restarts) the render of the requested thumbnails."""
        if not self._photo_path:
            return
        if self._loader is not None and self._loader.isRunning():
            # A more recent request wins: the previous one stops between two
            # thumbnails, and its end is what triggers this one.
            self._pending_kinds = kinds or [k for k, _ in FRAME_TYPES]
            self._loader.cancel()
            return
        loader = _TileLoader(self._photo_path, self._edit,
                             kinds or [k for k, _ in FRAME_TYPES],
                             base=self._base_image, parent=self)
        loader.base_ready.connect(self._on_base_ready)
        loader.tile_ready.connect(self._on_tile_ready)
        loader.finished.connect(self._on_loader_finished)
        self._loader = loader
        loader.start()

    def _on_loader_finished(self) -> None:
        self._loader = None
        if self._pending_kinds:
            kinds, self._pending_kinds = self._pending_kinds, []
            self._start_gallery(kinds)

    def _on_base_ready(self, image) -> None:
        self._base_image = image

    def _on_tile_ready(self, kind: str, image: QImage) -> None:
        btn = self._tiles.get(kind)
        if btn is None:
            return
        pix = QPixmap.fromImage(image)
        btn.setIcon(QIcon(pix))
        # The icon keeps its proportions: the framed thumbnail is larger than the
        # photo alone, so it is sized from what has been rendered.
        btn.setIconSize(pix.size())

    def _refresh_parametric_tiles(self) -> None:
        """Consumes the thumbnails marked to be re-rendered since the last setting."""
        kinds, self._dirty_kinds = sorted(self._dirty_kinds), set()
        if kinds:
            self._start_gallery(kinds)

    def _mark_dirty(self, kinds) -> None:
        self._dirty_kinds |= set(kinds)
        self._refresh_timer.start()

    # ------------------------------------------------------------------ settings

    def _select_kind(self, kind: str) -> None:
        self._edit.frame_type = kind
        width = suggested_width(kind, self._edit.frame_width)
        if width != self._edit.frame_width:
            # The slider follows: the setting stays the one displayed, never a silent
            # adjustment applied at render time.
            self._edit.frame_width = width
            self._sl_width.set_value(width * 100.0)
            self._mark_dirty(PARAMETRIC_FRAMES)
        self._apply_tile_styles(kind)
        self._update_params_visibility()
        self.preview.emit(copy.copy(self._edit))

    def _set_inner_enabled(self, enabled: bool) -> None:
        self._edit.frame_inner_enabled = bool(enabled)
        self._update_params_visibility()
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_inner_motif(self, motif: str) -> None:
        self._edit.frame_inner_motif = motif
        for m, btn in self._motif_buttons.items():
            btn.setChecked(m == motif)
        self._update_params_visibility()
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_inner_relief(self, relief: bool) -> None:
        self._edit.frame_inner_relief = bool(relief)
        for value, btn in self._relief_buttons.items():
            btn.setChecked(value == bool(relief))
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_main_color(self, hex_value: str) -> None:
        """Main colour imposed (black / white shortcuts)."""
        self._btn_color.set_color(hex_value)
        self._set_attr("frame_color", hex_value, reload_tiles=True)

    def _set_style(self, style: str) -> None:
        self._edit.frame_style = style
        for s, btn in self._style_buttons.items():
            btn.setChecked(s == style)
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_attr(self, attr: str, value, reload_tiles: bool = False, tiles=None) -> None:
        setattr(self._edit, attr, value)
        self.preview.emit(copy.copy(self._edit))
        if reload_tiles:
            # By default only the adjustable frames change appearance; the width,
            # by contrast, applies to every pattern.
            self._mark_dirty(PARAMETRIC_FRAMES if tiles is None else tiles)

    def _update_params_visibility(self) -> None:
        kind = self._edit.frame_type
        parametric = kind in PARAMETRIC_FRAMES
        # The thickness is adjustable for every pattern (a carved frame that is too
        # thin loses its decoration); only the colours are specific to the parametric ones.
        self._params.setVisible(kind != "none")
        self._style_row_host.setVisible(parametric)
        self._double_rows.setVisible(kind == "double")
        # "Plain surround" is a flat fill of a single colour: neither fill style
        # nor second colour.
        styled = kind in STYLED_FRAMES
        for btn in self._style_buttons.values():
            btn.setVisible(styled)
        self._btn_color2.setVisible(styled)
        self._sl_width.set_label(translate("FrameDialog", "Outer frame") if styled
                                 else translate("FrameDialog", "Thickness"))
        # The second frame is offered (and therefore adjustable) for the plain surround
        # only; for the double frame, the gap and the inner frame are part of the pattern.
        plain = kind == "plain"
        inner_on = plain and bool(self._edit.frame_inner_enabled)
        self._chk_inner.setVisible(plain)
        self._inner_rows.setVisible(kind == "double" or inner_on)
        self._sl_inner.set_label(translate("FrameDialog", "Second frame") if plain
                                 else translate("FrameDialog", "Inner frame"))
        # The ironwork only makes sense on the second frame; the simple line has
        # no ornament to size.
        self._inner_motif_rows.setVisible(inner_on)
        ornamented = inner_on and self._edit.frame_inner_motif in ORNAMENTED_MOTIFS
        self._relief_row.setVisible(ornamented)
        self._sl_ornament.setVisible(ornamented)
        self.adjustSize()
        QTimer.singleShot(0, self._reposition)

    # ------------------------------------------------------------------ misc

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            QTimer.singleShot(0, lambda: self.move(pos))

    def _reposition(self) -> None:
        if self._panel is not None:
            self.move(self._panel._compute_dialog_pos(self.width(), self.height()))

    def closeEvent(self, event) -> None:
        # Never leave a QThread running after the dialog is closed:
        # it would emit towards destroyed widgets.
        self._refresh_timer.stop()
        if self._loader is not None:
            self._pending_kinds = []
            self._loader.cancel()
            self._loader.wait(3000)
            self._loader = None
        super().closeEvent(event)

    def get_edit(self) -> EditInfo:
        return self._edit
