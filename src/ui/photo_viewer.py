# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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
    QComboBox,
)

from src.core.models import PhotoInfo, EditInfo
from src.processing.edit_database import EditDatabase
from src.processing.adjustments import ImageAdjuster
from src.processing.annotation_geometry import catmull_rom_to_bezier_segments
from src.ui.annotation_renderer import (
    render_annotations, hit_test_annotations, annotation_screen_bounds,
)
from src.ui.ui_utils import install_menu_width_fix

logger = logging.getLogger(__name__)

# Maximum resolution for the on-screen display.
# The edits (rotation, crop, etc.) are applied to this reduced copy.
# The full-resolution original is only used for the final export.
# ------------------------------------------------------------------ extracted modules
# (2026-07) The pixmap pipeline and the canvas moved into their own modules;
# names re-exported (the slideshow imports _build_pixmap from photo_viewer).
from src.ui.viewer_pixmaps import (  # noqa: E402,F401
    _PREVIEW_MAX_PX, _apply_edit_to_base, _build_base_image, _build_pixmap,
    _build_video_base_image, _build_video_pixmap, _to_rgb,
)
from src.ui.viewer_canvas import (  # noqa: E402,F401
    _ANNOTATION_CORNER_CURSORS, _CORNER_CURSORS, _CROP_FORMAT_DATA,
    _EDGE_INDICES, _HANDLE_HIT, _Canvas, _InlineTextEdit, _make_rect_quad,
)
from src.core.i18n import translate

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
    """Icon representing the w/h ratio through a rectangle with the right proportions."""
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

# 4 corner handles: TL(0), TR(1), BR(2), BL(3)


class _RatingStars(QWidget):
    """5 clickable stars (viewer toolbar). Clicking the rating already shown
    removes it (rating → 0)."""

    rating_clicked = Signal(int)   # 0-5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rating = 0
        self._btns: list[QPushButton] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for i in range(1, 6):
            btn = QPushButton("☆")
            btn.setFlat(True)
            btn.setFixedWidth(20)
            btn.setStyleSheet(
                "QPushButton { color: #ccc; border: none; padding: 0; font-size: 14px; }"
                "QPushButton:hover { color: #ffd200; }"
            )
            btn.setToolTip(translate("PhotoViewer", "Rate %n star(s)", None, i))
            btn.clicked.connect(lambda _checked=False, n=i: self._on_star_clicked(n))
            layout.addWidget(btn)
            self._btns.append(btn)

    def _on_star_clicked(self, n: int) -> None:
        new_rating = 0 if n == self._rating else n
        self.set_rating(new_rating)
        self.rating_clicked.emit(new_rating)

    @property
    def rating(self) -> int:
        return self._rating

    def set_rating(self, rating: int) -> None:
        self._rating = max(0, min(5, int(rating)))
        for i, btn in enumerate(self._btns, start=1):
            btn.setText("★" if i <= self._rating else "☆")
            btn.setStyleSheet(
                "QPushButton { color: %s; border: none; padding: 0; font-size: 14px; }"
                "QPushButton:hover { color: #ffd200; }"
                % ("#ffd200" if i <= self._rating else "#ccc")
            )


class _TagDropdown(QComboBox):
    """Always-present drop-down list of the catalog keywords (viewer
    toolbar) — not only those of the current photo.
    The keywords active on the displayed photo are sorted at the top of the
    list, in yellow (#ffd200) on a blue background (#2a5a8a, the colour of the
    Export button). Selecting an entry toggles that keyword on the current
    photo (added if absent, removed if already present); the box systematically
    goes back to its placeholder text after a selection (it lists every
    keyword, it does not "contain" a single one like a classic combo)."""

    tag_toggled = Signal(str, bool)  # (tag, added) — True = added, False = removed

    _ACTIVE_FG = "#ffd200"
    _ACTIVE_BG = "#2a5a8a"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(False)
        self.setToolTip(translate("TagDropdown", "No keyword"))
        self.setFixedWidth(150)
        # The usual CSS triangle (::down-arrow with bevelled borders) does not
        # draw here: this sub-control switches to "custom image" mode as soon as
        # it is styled, and without a valid image supplied it falls back on a
        # broken-image pictogram (a solid grey rectangle) rather than a real
        # arrow, whatever the active Qt style. Simpler and more reliable: the
        # arrow is a label overlaid on the right edge (positioned in resizeEvent)
        # and the native sub-control is reduced to zero.
        # WA_TransparentForMouseEvents lets the clicks pass through to the
        # QComboBox underneath (otherwise the arrow would catch the click and
        # prevent the list from opening).
        self._arrow_label = QLabel("▾", self)
        self._arrow_label.setStyleSheet("color: #ccc; background: transparent; font-size: 11px;")
        self._arrow_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._active: set[str] = set()
        self._apply_style()
        self._set_placeholder()
        self.activated.connect(self._on_activated)

    def _apply_style(self) -> None:
        # Seen through setPlaceholderText (currentIndex stays -1 permanently,
        # cf. the class docstring): the "color" of the QSS below does colour the
        # placeholder text, not only a classic selected text — hence the need to
        # regenerate the whole stylesheet to change its colour (a plain
        # QComboBox[prop] rule was no simpler here).
        color = self._ACTIVE_FG if len(self._active) == 1 else "#ccc"
        self.setStyleSheet(
            f"QComboBox {{ color: {color}; background: rgba(255,255,255,25);"
            " border: 1px solid rgba(255,255,255,60); border-radius: 8px;"
            " padding: 1px 18px 1px 8px; font-size: 11px; }"
            "QComboBox:hover { border: 1px solid rgba(255,255,255,90); }"
            "QComboBox::drop-down { width: 0; border: none; }"
            "QComboBox QAbstractItemView { outline: none; }"
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrow_label.adjustSize()
        margin = 8
        x = self.width() - self._arrow_label.width() - margin
        y = (self.height() - self._arrow_label.height()) // 2
        self._arrow_label.move(x, y)

    def _set_placeholder(self) -> None:
        self._apply_style()
        n = len(self._active)
        if n == 0:
            label = translate("PhotoViewer", "🏷 Keywords")
        elif n == 1:
            label = f"🏷 {next(iter(self._active))}"
        else:
            label = translate("PhotoViewer", "🏷 Keywords ({n})").format(n=n)
        self.setPlaceholderText(label)

    def set_tags(self, all_tags: list[str], active_tags: list[str]) -> None:
        self._active = set(active_tags)
        self.blockSignals(True)
        self.clear()
        ordered = sorted(self._active) + sorted(t for t in all_tags if t not in self._active)
        for tag in ordered:
            self.addItem(tag)
            idx = self.count() - 1
            self.setItemData(idx, tag, Qt.UserRole)
            if tag in self._active:
                self.setItemData(idx, QColor(self._ACTIVE_FG), Qt.ForegroundRole)
                self.setItemData(idx, QColor(self._ACTIVE_BG), Qt.BackgroundRole)
        self.setCurrentIndex(-1)
        self.blockSignals(False)
        self._set_placeholder()
        self.setToolTip(", ".join(sorted(self._active)) if self._active
                        else translate("PhotoViewer", "No keyword"))

    def _on_activated(self, index: int) -> None:
        tag = self.itemData(index, Qt.UserRole)
        self.setCurrentIndex(-1)
        if tag:
            self.tag_toggled.emit(tag, tag not in self._active)


# Number of base images (1024 px JPEG, ~300 kB each) kept in memory:
# the current photo + the prefetched neighbours → instant navigation in
# both directions without reading the original file again.
_BASE_LRU_MAX = 8


class _BaseLoader(QThread):
    """Loads _build_base_image in a secondary thread, for one or several photos
    (the current photo, or the prefetch of the neighbours for navigation).
    Needed for videos: cv2.VideoCapture may marshal COM calls on the UI thread
    (Windows STA) and cause freezes if called directly."""

    base_ready = Signal(str, object)   # (photo_path, tuple[bytes,int,int] | None)

    def __init__(self, photos: "list[PhotoInfo]", parent=None) -> None:
        super().__init__(parent)
        self._photos = list(photos)
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        for photo in self._photos:
            if self._stop_flag:
                break
            result = _build_base_image(photo)
            self.base_ready.emit(photo.path, result)


class PhotoViewer(QWidget):
    closed               = Signal()
    navigate             = Signal(int)
    zoom_changed         = Signal(float)
    crop_ready           = Signal(object)  # tuple of 8 relative coords (x0,y0,…,x3,y3)
    crop_mode_ended      = Signal()        # crop mode finished (validated or cancelled)
    save_requested       = Signal(object)  # PhotoInfo
    rename_requested     = Signal(object)  # PhotoInfo
    move_requested       = Signal(object)  # PhotoInfo
    delete_requested            = Signal(list)    # list[PhotoInfo]
    remove_from_album_requested = Signal(list)    # list[PhotoInfo] — removal from an album (non-destructive)
    dup_badge_clicked    = Signal(object)  # PhotoInfo — duplicate badge clicked
    red_eye_point_added         = Signal(float, float)  # cx_norm, cy_norm (0-1)
    pixel_sampled               = Signal(int, int, int)  # R, G, B — white balance eyedropper
    face_context_menu_requested = Signal(object, object)  # (FaceInfo, global QPoint)
    vignette_changed            = Signal(object)  # EditInfo (geometry after a drag)
    face_bbox_ready             = Signal(object)  # tuple (bbox_x,bbox_y,bbox_w,bbox_h) int
    face_add_mode_ended         = Signal()  # face add mode finished (validated or cancelled)
    force_redetect_requested    = Signal(object)  # PhotoInfo — context menu
    folder_grid_requested       = Signal(object)  # PhotoInfo — context menu: grid of the folder
    favorite_toggle_requested   = Signal(object)  # PhotoInfo — favourite toggle requested
    rating_change_requested     = Signal(list, int)  # list[PhotoInfo], rating 0-5 — rating change requested
    edit_tags_requested         = Signal(list)    # list[PhotoInfo] — keyword editing requested
    tag_toggle_requested        = Signal(object, str, bool)  # (PhotoInfo, tag, added) — entry clicked in the toolbar drop-down list
    annotation_added             = Signal(object)  # annotation dict added
    annotation_deleted           = Signal(str)     # id of the deleted annotation
    annotation_deleted_multi     = Signal(object)  # list[str] of deleted ids (batch deletion)
    annotation_selection_changed = Signal(object)  # list[str] of selected ids (may be empty)
    annotation_moved              = Signal(str, object)  # (id, up-to-date annotation dict)
    annotation_moved_multi        = Signal(object)  # dict[id, up-to-date annotation] (batch move)
    annotation_resized            = Signal(str, object)  # (id, up-to-date annotation dict)
    annotation_grouped            = Signal(object)  # dict[id, up-to-date annotation] (group/ungroup)

    def __init__(self, config=None, thumb_cache=None, parent=None):
        super().__init__(parent)
        self._config = config
        # Thumbnail cache of the grid (optional): serves as an immediate
        # placeholder while the base image loads — the user sees the photo
        # (blurry) straight away instead of a black screen.
        self._thumb_cache = thumb_cache
        self._photo: PhotoInfo | None = None
        self._edit: EditInfo | None = None
        self._db = EditDatabase()
        # Complete list of the catalog keywords (not only those of the current
        # photo) — feeds _tag_dropdown, cf. set_available_tags().
        self._all_tags: list[str] = []
        # LRU cache of the base images (1024px, without edits), key = path.
        # Current photo + prefetched neighbours: avoids reading the whole file
        # again at every slider preview AND at every prev/next navigation.
        from collections import OrderedDict
        self._base_lru: "OrderedDict[str, tuple[bytes, int, int]]" = OrderedDict()
        # Paths whose base image is being loaded (current or prefetched) —
        # avoids duplicate loads.
        self._loading_paths: set[str] = set()
        # Active loading threads (images and videos). The loading is asynchronous
        # so as not to block the UI thread, which is particularly critical for
        # videos (cv2.VideoCapture + COM). The results of the loads "overtaken" by
        # the navigation still feed the LRU.
        self._base_loaders: "list[_BaseLoader]" = []
        # Album displayed when the viewer was opened from its grid (or None):
        # determines whether the context menu offers "Remove from the album" and
        # whether the Del key removes from the album rather than erasing the file
        # — cf. ThumbnailGrid.set_album_context.
        self._album_id: int | None = None
        # Debounce for the edit previews: reload only 60 ms after the last slider
        # event (avoids memory overloads).
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(60)
        self._preview_timer.timeout.connect(self._reload_pixmap)
        self._setup_ui()
        self.refresh_external_apps()
        self.setFocusPolicy(Qt.StrongFocus)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- Permanent toolbar ----
        self._toolbar = QWidget()
        self._toolbar.setStyleSheet("background: rgba(0,0,0,200);")
        tb_layout = QHBoxLayout(self._toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4)

        self._btn_back = QPushButton("←")
        self._btn_back.setToolTip(translate("PhotoViewer", "Back to the grid  (Esc)"))
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
        self._btn_fav.setToolTip(translate("PhotoViewer", "Mark as favourite"))
        self._btn_fav.setFixedWidth(32)
        self._btn_fav.setCheckable(True)
        self._btn_fav.setStyleSheet(
            "QPushButton { color: #ccc; border: none; padding: 0; font-size: 16px; }"
            "QPushButton:checked { color: #ffd200; }"
            "QPushButton:hover { color: #ffd200; }"
        )
        self._btn_fav.clicked.connect(self._toggle_favorite)
        tb_layout.addWidget(self._btn_fav)

        self._rating_stars = _RatingStars()
        self._rating_stars.rating_clicked.connect(self._on_rating_clicked)
        tb_layout.addWidget(self._rating_stars)

        self._tag_dropdown = _TagDropdown()
        self._tag_dropdown.tag_toggled.connect(self._on_tag_dropdown_toggled)
        tb_layout.addWidget(self._tag_dropdown)

        # Container of the external application buttons (rebuilt by refresh_external_apps)
        self._ext_apps_container = QWidget()
        self._ext_apps_container.setStyleSheet("background: transparent;")
        self._ext_apps_layout = QHBoxLayout(self._ext_apps_container)
        self._ext_apps_layout.setContentsMargins(4, 0, 4, 0)
        self._ext_apps_layout.setSpacing(4)
        tb_layout.addWidget(self._ext_apps_container)

        self._btn_fit = QPushButton("⊡")
        self._btn_fit.setToolTip(translate("PhotoViewer", "Fit to window  (F)"))
        self._btn_fit.setFixedWidth(32)
        self._btn_fit.clicked.connect(self.zoom_fit)
        tb_layout.addWidget(self._btn_fit)

        self._btn_100 = QPushButton("1:1")
        self._btn_100.setToolTip(translate("PhotoViewer", "Zoom 100%  (Z)"))
        self._btn_100.setFixedWidth(36)
        self._btn_100.clicked.connect(self.zoom_100)
        tb_layout.addWidget(self._btn_100)

        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip(translate("PhotoViewer", "Close  (Esc)"))
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
        self._canvas.vignette_changed.connect(self.vignette_changed)
        self._canvas.face_add_confirmed.connect(self._on_face_add_confirmed)
        self._canvas.annotation_added.connect(self.annotation_added)
        self._canvas.annotation_deleted.connect(self.annotation_deleted)
        self._canvas.annotation_deleted_multi.connect(self.annotation_deleted_multi)
        self._canvas.annotation_selection_changed.connect(self.annotation_selection_changed)
        self._canvas.annotation_moved.connect(self.annotation_moved)
        self._canvas.annotation_moved_multi.connect(self.annotation_moved_multi)
        self._canvas.annotation_resized.connect(self.annotation_resized)
        self._canvas.annotation_grouped.connect(self.annotation_grouped)
        layout.addWidget(self._canvas, stretch=1)

        # ---- Footer ----
        self._navbar = QWidget()
        self._navbar.setStyleSheet("background: rgba(0,0,0,200);")
        self._navbar.setFixedHeight(52)
        nav_layout = QHBoxLayout(self._navbar)
        nav_layout.setContentsMargins(16, 6, 16, 6)

        self._btn_prev = QPushButton(translate("PhotoViewer", "◀  Older"))
        self._btn_prev.setFixedHeight(36)
        self._btn_prev.clicked.connect(lambda: self.navigate.emit(1))
        nav_layout.addWidget(self._btn_prev)

        nav_layout.addStretch()

        self._nav_position_label = QLabel("")
        self._nav_position_label.setStyleSheet("color: white; font-size: 13px;")
        nav_layout.addWidget(self._nav_position_label)

        # Crop format buttons (hidden outside crop mode)
        self._btn_play_video = QPushButton(translate("PhotoViewer", "▶  Open the video"))
        self._btn_play_video.setToolTip(translate("PhotoViewer", "Open in the default video "
                                                                 "player"))
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
        self._crop_format_btns[0].setChecked(True)  # "Free" by default
        self._crop_format_group.idClicked.connect(self._on_crop_format_changed)
        self._crop_format_widget.hide()
        nav_layout.addWidget(self._crop_format_widget)

        self._btn_crop_confirm = QPushButton(translate("PhotoViewer", "✓  Confirm the crop"))
        self._btn_crop_confirm.setToolTip(translate("PhotoViewer", "Apply the crop  (Enter)"))
        self._btn_crop_confirm.setFixedHeight(36)
        self._btn_crop_confirm.setStyleSheet("background: #2a6a2a; color: white;")
        self._btn_crop_confirm.clicked.connect(self.confirm_crop)
        self._btn_crop_confirm.hide()
        nav_layout.addWidget(self._btn_crop_confirm)

        self._btn_crop_cancel = QPushButton(translate("PhotoViewer", "✕  Cancel"))
        self._btn_crop_cancel.setToolTip(translate("PhotoViewer", "Cancel the crop  (Esc)"))
        self._btn_crop_cancel.setFixedHeight(36)
        self._btn_crop_cancel.setStyleSheet("background: #6a2a2a; color: white;")
        self._btn_crop_cancel.clicked.connect(self.cancel_crop)
        self._btn_crop_cancel.hide()
        nav_layout.addWidget(self._btn_crop_cancel)

        self._btn_face_confirm = QPushButton(translate("PhotoViewer", "✓  Confirm the position"))
        self._btn_face_confirm.setToolTip(translate("PhotoViewer", "Confirm the face position  "
                                                                   "(Enter)"))
        self._btn_face_confirm.setFixedHeight(36)
        self._btn_face_confirm.setStyleSheet("background: #2a6a2a; color: white;")
        self._btn_face_confirm.clicked.connect(self.confirm_face_add)
        self._btn_face_confirm.hide()
        nav_layout.addWidget(self._btn_face_confirm)

        self._btn_face_cancel = QPushButton(translate("PhotoViewer", "✕  Cancel"))
        self._btn_face_cancel.setToolTip(translate("PhotoViewer", "Cancel adding the face  "
                                                                  "(Esc)"))
        self._btn_face_cancel.setFixedHeight(36)
        self._btn_face_cancel.setStyleSheet("background: #6a2a2a; color: white;")
        self._btn_face_cancel.clicked.connect(self.cancel_face_add_mode)
        self._btn_face_cancel.hide()
        nav_layout.addWidget(self._btn_face_cancel)

        nav_layout.addStretch()

        self._btn_next = QPushButton(translate("PhotoViewer", "Newer  ▶"))
        self._btn_next.setFixedHeight(36)
        self._btn_next.clicked.connect(lambda: self.navigate.emit(-1))
        nav_layout.addWidget(self._btn_next)

        layout.addWidget(self._navbar)

        # ---- Duplicates badge (floating over the canvas) ----
        self._dup_badge = QPushButton(translate("PhotoViewer", "⧉ Duplicates"), self)
        self._dup_badge.setToolTip(translate("PhotoViewer", "This photo has duplicates — click "
                                                            "to see them"))
        self._dup_badge.setStyleSheet(
            "QPushButton{"
            "  background:rgba(255,140,0,210);color:white;border:none;"
            "  border-radius:5px;padding:3px 10px;"
            "  font-size:11px;font-weight:bold"
            "}"
            "QPushButton:hover{background:rgba(255,160,40,240)}"
        )
        self._dup_badge.setCursor(Qt.PointingHandCursor)
        self._dup_badge.setFixedHeight(24)
        self._dup_badge.hide()
        self._dup_badge.clicked.connect(
            lambda: self.dup_badge_clicked.emit(self._photo)
        )

    # ------------------------------------------------------------------ photo

    def current_photo(self) -> "PhotoInfo | None":
        return self._photo

    def refresh_tags(self) -> None:
        """Redraws the keyword drop-down list from `self._photo.tags` (without
        changing the complete list of the catalog keywords) — to be called
        after an external mutation of `photo.tags` that does not go through
        `set_photo()`. If the complete list may have changed (e.g. a new
        keyword created), prefer `set_available_tags()`."""
        if self._photo is not None:
            self._tag_dropdown.set_tags(self._all_tags, self._photo.tags)

    def set_available_tags(self, all_tags: list[str]) -> None:
        """Complete list of the keywords defined in the catalog — to be called
        again every time it changes (a new keyword created, the last photo of a
        keyword deleted…). Immediately restyles _tag_dropdown with the active
        keywords of the current photo."""
        self._all_tags = list(all_tags)
        self.refresh_tags()

    def set_album_context(self, album_id: int | None) -> None:
        """States whether the displayed photo comes from an album (and which one),
        so as to offer "Remove from the album" and to point the Del key at it
        rather than at erasing the file permanently."""
        self._album_id = album_id

    def set_photo(self, photo: PhotoInfo, edit: EditInfo | None = None) -> None:
        self._preview_timer.stop()
        # The loads in flight are not cancelled: their results feed the LRU
        # (useful if the user goes back); _on_base_ready only displays the result
        # matching the current photo.
        self._photo = photo
        is_video = photo.media_type == "video"
        self._edit = None if is_video else (edit or self._db.load(photo.path))
        self._lbl_name.setText(photo.path)
        self._btn_fav.setChecked(photo.is_favorite)
        self._rating_stars.set_rating(photo.rating)
        self._tag_dropdown.set_tags(self._all_tags, photo.tags)
        self._btn_play_video.setVisible(is_video)
        self.refresh_external_apps()
        self._canvas.set_highlighted_face(None)
        self._update_dup_badge()
        self._reload_pixmap()

    def _update_dup_badge(self) -> None:
        if self._photo and self._photo.duplicate_group_id is not None:
            self._dup_badge.show()
            self._dup_badge.raise_()
            self._reposition_dup_badge()
            # The geometry of the viewer may not be final at this moment (e.g. a
            # switch from the grid: the stack/splitter is only resized after this
            # call) — a deferred repositioning catches the computation up once the
            # final geometry is known, to avoid a badly placed badge.
            QTimer.singleShot(0, self._reposition_dup_badge)
        else:
            self._dup_badge.hide()

    def _reposition_dup_badge(self) -> None:
        if not self._dup_badge.isVisible():
            return
        self._dup_badge.adjustSize()
        margin = 12
        toolbar_h = self._toolbar.height()
        x = self.width() - self._dup_badge.width() - margin
        y = toolbar_h + margin
        self._dup_badge.move(x, y)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._dup_badge.isVisible():
            self._reposition_dup_badge()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dup_badge.isVisible():
            QTimer.singleShot(0, self._reposition_dup_badge)

    def highlight_face(self, face) -> None:
        """Frames a single face on the photo (called from main_window)."""
        self._canvas.set_highlighted_face(face)

    def set_all_highlighted_faces(self, faces: list) -> None:
        """Frames every face (the 'All' mode of the FacePanel)."""
        self._canvas.set_highlighted_faces(faces)

    def _reload_pixmap(self) -> None:
        if not self._photo:
            return
        self._canvas.set_edit(self._edit)
        self._canvas.set_annotations(self._edit.annotations if self._edit else [])

        cached = self._base_lru.get(self._photo.path)
        if cached is not None:
            # Warm cache: apply the edits and display immediately
            self._base_lru.move_to_end(self._photo.path)
            base_bytes, orig_w, orig_h = cached
            pixmap = _apply_edit_to_base(base_bytes, self._edit)
            self._canvas.set_orig_size(orig_w, orig_h)
            self._canvas.set_pixmap(pixmap)
            return

        # Cold cache: immediately display the grid thumbnail as a placeholder
        # (blurry but instant — visual feedback without a black screen), then
        # start loading the base image in the background.
        placeholder = (
            self._thumb_cache.get_ram(self._photo.path)
            if self._thumb_cache is not None else None
        )
        if placeholder is not None and not placeholder.isNull():
            self._canvas.set_orig_size(self._photo.width or 0, self._photo.height or 0)
            self._canvas.set_pixmap(placeholder)
        else:
            self._canvas.set_orig_size(0, 0)
            self._canvas.set_pixmap(None)
        self._start_base_loader([self._photo])

    def prefetch(self, photos: "list[PhotoInfo]") -> None:
        """Prefetches the base image of the given photos in the background (the
        neighbours of the displayed photo, supplied by main_window after every
        navigation): moving to the next/previous photo becomes instant. Skips
        what is already in cache or being loaded."""
        self._start_base_loader(photos)

    def _start_base_loader(self, photos: "list[PhotoInfo]") -> None:
        todo = [
            p for p in photos
            if p is not None
            and p.path not in self._base_lru
            and p.path not in self._loading_paths
        ]
        if not todo:
            return
        for p in todo:
            self._loading_paths.add(p.path)
        loader = _BaseLoader(todo, self)
        loader.base_ready.connect(self._on_base_ready)
        loader.finished.connect(loader.deleteLater)
        loader.finished.connect(self._reap_base_loaders)
        self._base_loaders.append(loader)
        loader.start()

    @Slot()
    def _reap_base_loaders(self) -> None:
        alive = []
        for t in self._base_loaders:
            try:
                if t.isRunning():
                    alive.append(t)
            except RuntimeError:
                pass  # C++ object already destroyed by deleteLater
        self._base_loaders = alive

    @Slot(str, object)
    def _on_base_ready(self, path: str, result: object) -> None:
        """Receives the result of the image/video base loading from _BaseLoader."""
        self._loading_paths.discard(path)
        if result is not None:
            self._base_lru[path] = result
            self._base_lru.move_to_end(path)
            while len(self._base_lru) > _BASE_LRU_MAX:
                self._base_lru.popitem(last=False)
        if self._photo is None or self._photo.path != path:
            return  # prefetch of a neighbour, or navigation in the meantime
        if result is None:
            return
        base_bytes, orig_w, orig_h = result
        pixmap = _apply_edit_to_base(base_bytes, self._edit)
        self._canvas.set_edit(self._edit)
        self._canvas.set_orig_size(orig_w, orig_h)
        self._canvas.set_pixmap(pixmap)

    def invalidate_base_cache(self, path: "str | None" = None) -> None:
        """Forgets the cached base image for a path (a file modified on disk), or
        the whole cache if path is None."""
        if path is None:
            self._base_lru.clear()
        else:
            self._base_lru.pop(path, None)

    def refresh_name(self) -> None:
        if self._photo:
            self._lbl_name.setText(self._photo.path)

    def update_edit(self, edit: EditInfo) -> None:
        self._edit = edit
        # The annotations are lightweight vector data, rendered in a separate
        # layer — no need to wait for the debounce of the raster pixmap.
        self._canvas.set_annotations(edit.annotations)
        # Debounce: defers the render by 60 ms to absorb slider bursts.
        # Avoids piling up 72 MB PIL images in memory.
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
        # If a crop is already applied, show the image without it so that the
        # user can reposition the area on the complete image.
        cached = self._base_lru.get(self._photo.path) if self._photo else None
        if existing and self._photo and self._edit and cached:
            edit_no_crop = EditInfo.from_dict({**self._edit.to_dict(), 'crop': None})
            base_bytes, _, _ = cached
            pixmap = _apply_edit_to_base(base_bytes, edit_no_crop)
            if pixmap:
                self._canvas.set_pixmap(pixmap)
        self._canvas.enter_crop(existing)
        idx = self._crop_format_group.checkedId()
        self._canvas.set_aspect_ratio(_CROP_FORMAT_DATA[idx][2] if idx >= 0 else None)
        self._btn_prev.hide()
        self._btn_next.hide()
        self._nav_position_label.hide()
        self._crop_format_widget.show()
        self._btn_crop_confirm.show()
        self._btn_crop_cancel.show()

    def _on_crop_format_changed(self, idx: int) -> None:
        self._canvas.set_aspect_ratio(_CROP_FORMAT_DATA[idx][2])

    def confirm_crop(self) -> None:
        self._canvas.confirm_crop()    # emits crop_confirmed → _on_crop_confirmed

    def update_nav_arrows(self, has_prev: bool, has_next: bool) -> None:
        self._btn_prev.setVisible(has_prev)
        self._btn_next.setVisible(has_next)

    def set_nav_position(self, current: int, total: int) -> None:
        self._nav_position_label.setText(f"{current} / {total}" if total > 0 else "")

    def cancel_crop(self) -> None:
        self._canvas.cancel_crop()
        self._crop_format_widget.hide()
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self._nav_position_label.show()
        # Restore the image with the crop applied (it was displayed without the crop)
        self._reload_pixmap()
        self.crop_mode_ended.emit()

    def _on_crop_confirmed(self, quad: tuple) -> None:
        self._crop_format_widget.hide()
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self._nav_position_label.show()
        self.crop_ready.emit(quad)
        self.crop_mode_ended.emit()

    # ------------------------------------------------------------------ manual face addition

    def enter_face_add_mode(self) -> None:
        self._canvas.enter_face_add_mode()
        self._btn_prev.hide()
        self._btn_next.hide()
        self._nav_position_label.hide()
        self._btn_face_confirm.show()
        self._btn_face_cancel.show()

    def confirm_face_add(self) -> None:
        self._canvas.confirm_face_add()   # emits face_add_confirmed → _on_face_add_confirmed if valid
        self._btn_face_confirm.hide()
        self._btn_face_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self._nav_position_label.show()
        self.face_add_mode_ended.emit()

    def cancel_face_add_mode(self) -> None:
        self._canvas.cancel_face_add_mode()
        self._btn_face_confirm.hide()
        self._btn_face_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self._nav_position_label.show()
        self.face_add_mode_ended.emit()

    def _on_face_add_confirmed(self, bbox: tuple) -> None:
        self.face_bbox_ready.emit(bbox)

    # ------------------------------------------------------------------ red-eye mode

    def enter_red_eye_mode(self, radius: float = 0.03) -> None:
        self._canvas.enter_red_eye_mode(radius)

    def exit_red_eye_mode(self) -> None:
        self._canvas.exit_red_eye_mode()

    def set_red_eye_radius(self, radius: float) -> None:
        self._canvas.set_red_eye_radius(radius)

    # ------------------------------------------------------------------ interactive vignette

    def enter_vignette_mode(self, edit) -> None:
        self._canvas.enter_vignette_mode(edit)

    def exit_vignette_mode(self) -> None:
        self._canvas.exit_vignette_mode()

    def update_vignette(self, edit) -> None:
        self._canvas.update_vignette(edit)

    # ------------------------------------------------------------------ colour eyedropper (white balance)

    def start_color_pick(self) -> None:
        """Enables the eyedropper mode: the next left click on the image → pixel_sampled(r, g, b)."""
        self._canvas.start_color_pick()

    def stop_color_pick(self) -> None:
        self._canvas.stop_color_pick()

    # ------------------------------------------------------------------ annotations (drawing/text)

    def enter_annotation_mode(self, tool: str = "pen") -> None:
        self._canvas.enter_annotation_mode(tool)

    def exit_annotation_mode(self) -> None:
        self._canvas.exit_annotation_mode()

    def set_annotation_tool(self, tool: str) -> None:
        self._canvas.set_annotation_tool(tool)

    def set_annotation_style(self, color: str, width: float, font_family: str,
                              font_size: float, bold: bool, italic: bool,
                              fill_color: str = "#40ff0000", opacity: float = 1.0,
                              blur: float = 0.0) -> None:
        self._canvas.set_annotation_style(color, width, font_family, font_size, bold, italic,
                                           fill_color, opacity, blur)

    def delete_selected_annotation(self) -> None:
        self._canvas.delete_selected_annotation()

    def clear_all_annotations(self) -> None:
        self._canvas.clear_all_annotations()

    def group_selected_annotations(self) -> None:
        self._canvas.group_selected_annotations()

    def ungroup_selected_annotations(self) -> None:
        self._canvas.ungroup_selected_annotations()

    def set_annotations_visible(self, visible: bool) -> None:
        self._canvas.set_annotations_visible(visible)

    # ------------------------------------------------------------------ misc

    def refresh_external_apps(self) -> None:
        """Rebuilds the external application buttons of the toolbar from the
        config, filtered by the media scope of each application ("image" /
        "video" / "both", absent = "both" for backward compatibility) compared
        with the media_type of the photo currently displayed — an application
        tagged "video" (e.g. VLC) only appears on a video, one tagged "image"
        only on a still. With no photo displayed (e.g. on the very first call,
        in __init__), no filtering is applied."""
        while self._ext_apps_layout.count():
            item = self._ext_apps_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._config:
            self._ext_apps_container.setVisible(False)
            return

        media_type = self._photo.media_type if self._photo else None
        _icon_provider = QFileIconProvider()
        shown = 0
        for app in self._config.get("tools.external_apps", []):
            name = app.get("name", "")
            path = app.get("path", "")
            scope = app.get("media", "both")
            if not path or not os.path.isfile(path):
                continue
            if media_type is not None and scope != "both" and scope != media_type:
                continue
            shown += 1
            btn = QToolButton()
            btn.setToolTip(translate("PhotoViewer", "Open with {app}").format(app=name))
            # Accessible name for the pywinauto automation (e2e) — the same
            # convention as ThumbnailCell/_DuplicateCard: this button has no text of
            # its own (icon only), hence no unique window_text().
            btn.setAccessibleName(f"extapp::{name}")
            btn.setFixedSize(32, 32)
            btn.setIcon(_icon_provider.icon(QFileInfo(path)))
            btn.setIconSize(QSize(22, 22))
            btn.setStyleSheet(
                "QToolButton { background: white; border: none; border-radius: 4px; }"
                "QToolButton:hover { background: #e0e8ff; }"
                "QToolButton:pressed { background: #c0d0ff; }"
            )
            btn.clicked.connect(lambda _checked=False, p=path: self._open_with(p))
            self._ext_apps_layout.addWidget(btn)

        self._ext_apps_container.setVisible(shown > 0)

    def _open_with(self, app_path: str) -> None:
        if not self._photo:
            return
        import subprocess
        try:
            subprocess.Popen([app_path, self._photo.path])
        except Exception as exc:
            logger.warning("Impossible de lancer '%s' : %s", app_path, exc)

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
        install_menu_width_fix(menu)

        fav_label = (translate("PhotoViewer", "Remove from favourites") if photo.is_favorite
                     else translate("PhotoViewer", "Mark as favourite"))
        menu.addAction(fav_label, self._toggle_fav_from_menu)
        menu.addAction(translate("PhotoViewer", "Keywords…"), lambda: self.edit_tags_requested.emit([photo]))
        menu.addAction(translate("PhotoViewer", "Rename…"), lambda: self.rename_requested.emit(photo))
        menu.addAction(translate("PhotoViewer", "Move to…"), lambda: self.move_requested.emit(photo))
        menu.addAction(translate("PhotoViewer", "Save the edited image to disk\tCtrl+S"),
                       lambda: self.save_requested.emit(photo))
        menu.addSeparator()
        menu.addAction(translate("PhotoViewer", "Show in File Explorer"),
                       lambda: os.startfile(os.path.dirname(photo.path)))
        menu.addAction(translate("PhotoViewer", "Show the folder in the grid"),
                       lambda: self.folder_grid_requested.emit(photo))
        menu.addSeparator()

        gps_coords = self._resolve_gps(photo)
        act_map = menu.addAction(translate("PhotoViewer", "Locate on the map"))
        act_map.setEnabled(gps_coords is not None)
        if gps_coords is not None:
            lat, lon = gps_coords
            act_map.triggered.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl(f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=15#map=15/{lat}/{lon}")
                )
            )

        menu.addSeparator()
        menu.addAction(translate("PhotoViewer", "Force a new detection with no size limit"),
                       lambda: self.force_redetect_requested.emit(photo))
        menu.addSeparator()
        if self._album_id is not None:
            menu.addAction(translate("PhotoViewer", "Remove from the album\tDel"),
                           lambda: self.remove_from_album_requested.emit([photo]))
        else:
            menu.addAction(translate("PhotoViewer", "Delete the file…\tDel"), lambda: self.delete_requested.emit([photo]))

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
            self._btn_fav.setText("♥" if checked else "♡")
            self.favorite_toggle_requested.emit(self._photo)

    def _set_rating(self, rating: int) -> None:
        if self._photo:
            self._photo.rating = rating
            self._rating_stars.set_rating(rating)
            self.rating_change_requested.emit([self._photo], rating)

    def _on_rating_clicked(self, rating: int) -> None:
        self._set_rating(rating)

    def _on_tag_dropdown_toggled(self, tag: str, added: bool) -> None:
        if self._photo is None:
            return
        if added:
            if tag not in self._photo.tags:
                self._photo.tags = self._photo.tags + [tag]
        else:
            self._photo.tags = [t for t in self._photo.tags if t != tag]
        self._tag_dropdown.set_tags(self._all_tags, self._photo.tags)
        self.tag_toggle_requested.emit(self._photo, tag, added)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            if self._canvas._crop_mode:
                self.cancel_crop()
            elif self._canvas._face_add_mode:
                self.cancel_face_add_mode()
            elif self._canvas._red_eye_mode:
                self.exit_red_eye_mode()
            elif self._canvas._annotation_mode:
                self._canvas.cancel_annotation_draft()
            else:
                self.closed.emit()
        elif key == Qt.Key_Return or key == Qt.Key_Enter:
            if self._canvas._crop_mode:
                self.confirm_crop()
            elif self._canvas._face_add_mode:
                self.confirm_face_add()
            elif self._canvas._annotation_mode and self._canvas._annotation_tool == "curve":
                self._canvas.confirm_annotation_draft()
        elif key in (Qt.Key_Right, Qt.Key_Up):
            if not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                    and not self._canvas._face_add_mode and not self._canvas._annotation_mode:
                self.navigate.emit(-1)   # newer (right/up = towards the top of the list)
        elif key in (Qt.Key_Left, Qt.Key_Down):
            if not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                    and not self._canvas._face_add_mode and not self._canvas._annotation_mode:
                self.navigate.emit(1)    # older (left/down = towards the bottom of the list)
        elif key == Qt.Key_Delete:
            if self._canvas._annotation_mode:
                self.delete_selected_annotation()
            else:
                photo = self.current_photo()
                if photo and not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                        and not self._canvas._face_add_mode:
                    if self._album_id is not None:
                        self.remove_from_album_requested.emit([photo])
                    else:
                        self.delete_requested.emit([photo])
        elif key == Qt.Key_S and event.modifiers() == Qt.ControlModifier:
            photo = self.current_photo()
            if photo and not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                    and not self._canvas._face_add_mode:
                self.save_requested.emit(photo)
        elif key == Qt.Key_F:
            self.zoom_fit()
        elif key == Qt.Key_Z:
            self.zoom_100()
        elif key in (Qt.Key_0, Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4, Qt.Key_5):
            if not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                    and not self._canvas._face_add_mode and not self._canvas._annotation_mode:
                self._set_rating(key - Qt.Key_0)
        else:
            super().keyPressEvent(event)
