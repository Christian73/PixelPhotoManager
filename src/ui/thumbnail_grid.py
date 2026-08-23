# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import weakref
from datetime import datetime as _dt
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QRunnable, QThreadPool, QObject, Slot, QPoint, QRect, QTimer, QMimeData, QByteArray
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QFontMetrics, QDrag
from PySide6.QtWidgets import (
    QScrollArea, QScrollBar, QWidget, QLabel, QVBoxLayout, QSizePolicy,
    QMenu, QApplication, QPushButton,
)

from src.ui.loading_label import LoadingLabel
from src.core.models import PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache, edit_signature
from src.ui.ui_utils import install_menu_width_fix
from src.core.i18n import translate

logger = logging.getLogger(__name__)

#: Displayed name of the Del key in the "Label\tKey" labels.
#: Separated from the label because the latter varies (singular/plural, album or not):
#: concatenated outside `translate()`, it stayed French in every language
#: ("Delete the file...\tSuppr"). photo_viewer.py can include it in its
#: translated literal, since its own is fixed.
_DEL_KEY = "\t" + translate("ThumbnailGrid", "Del")

_img_thumb_pool: "QThreadPool | None" = None

def _get_thumb_pool() -> QThreadPool:
    """Pool dedicated to the image thumbnails, limited to 4 threads.
    Avoids the RAM + disk I/O saturation during the simultaneous JPEG decoding."""
    global _img_thumb_pool
    if _img_thumb_pool is None:
        _img_thumb_pool = QThreadPool()
        _img_thumb_pool.setMaxThreadCount(4)
    return _img_thumb_pool


_video_thumb_pool: "QThreadPool | None" = None

def _get_video_thumb_pool() -> QThreadPool:
    """Pool dedicated to the video thumbnails, limited to 2 threads.
    Avoids the disk I/O saturation when many cv2.VideoCapture open
    simultaneously - which also slows the UI thread down through disk contention."""
    global _video_thumb_pool
    if _video_thumb_pool is None:
        _video_thumb_pool = QThreadPool()
        _video_thumb_pool.setMaxThreadCount(2)
    return _video_thumb_pool

_MONTHS = [
    translate("ThumbnailGrid", "January"), translate("ThumbnailGrid", "February"),
    translate("ThumbnailGrid", "March"), translate("ThumbnailGrid", "April"),
    translate("ThumbnailGrid", "May"), translate("ThumbnailGrid", "June"),
    translate("ThumbnailGrid", "July"), translate("ThumbnailGrid", "August"),
    translate("ThumbnailGrid", "September"), translate("ThumbnailGrid", "October"),
    translate("ThumbnailGrid", "November"), translate("ThumbnailGrid", "December"),
]

# Target number of columns per row (outer, intermediate, centre, ...).
# The size factors are the inverse: fewer columns = larger cells.
# Intermediate = geometric mean of outer and centre -> sqrt(37x4) ~ 12.
_RIBBON_COLS_TARGET = (37, 12, 4, 12, 37)  # indicative targets (depend on the width)

# Size factors: proportional to 1/n_cols, normalised so that centre = 2.0
# 4/37 ~ 0.108, x2 -> 0.216  |  4/12 ~ 0.333, x2 -> 0.667  |  4/4 = 1.0, x2 -> 2.0
_RIBBON_FACTORS = (0.216, 0.667, 2.0, 0.667, 0.216)
_RIBBON_CENTER  = 2   # index of the centre row
_RIBBON_SPACING = 6   # px between the rows

# Inertia of the ribbon (wheel)
# Design: 1 immediate step per click + a cumulative coasting impulse.
# Slow click  -> 1 step + ~2 steps of gliding  ~ 3 in total.
# Fast scroll (10 clicks) -> 10 steps + ~17 of gliding ~ 27 in total.
_INERTIA_IMPULSE  = 0.20  # coasting impulse added per click (on top of the immediate step)
_INERTIA_MAX_VEL  = 12.0  # maximum coasting speed
_INERTIA_FRICTION = 0.85  # braking per frame (~16 ms)
_INERTIA_STOP     = 0.08  # stopping threshold


class _ThumbSignals(QObject):
    # Uses 'object' (PyObject) to guarantee the cross-thread marshaling in PySide6.
    # 3rd argument: fingerprint of the edits applied to the thumbnail produced -
    # passed along rather than read back on the UI side, otherwise two edits made
    # in quick succession could file the pixmap of the first under the fingerprint of the second.
    ready = Signal(str, object, str)  # photo_path, jpeg_bytes (Python bytes), edit_sig


class _ThumbWorker(QRunnable):
    def __init__(self, photo_path: str, cache: ThumbnailCache, signals: _ThumbSignals,
                 edit=None):
        super().__init__()
        self._path = photo_path
        self._cache = cache
        self._edit = edit
        self._sig = edit_signature(edit)
        self._signals_ref = weakref.ref(signals)
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            # Check the DB before starting PIL again - avoids the JPEG decoding if already
            # in cache. The fingerprint guarantees that we do not reuse a thumbnail
            # produced with another edit state (nor the reverse: an edited
            # thumbnail already in cache is not regenerated for nothing).
            data = self._cache.get_bytes(self._path, self._sig)
            if data is None:
                data = self._cache.generate(self._path, self._edit)
            if data:
                signals = self._signals_ref()
                if signals is not None:
                    signals.ready.emit(self._path, data, self._sig)
        except Exception:
            logger.debug(f"Erreur génération vignette {self._path}", exc_info=True)


_DUP_BADGE_W = 22
_DUP_BADGE_H = 16
_RATING_BADGE_H = 16


class ThumbnailCell(QWidget):
    double_clicked   = Signal(object)
    right_clicked    = Signal(object, object)
    clicked          = Signal(object, Qt.KeyboardModifier)
    drag_started     = Signal(object)
    duplicate_clicked = Signal(object)  # PhotoInfo - click on the duplicate badge

    def __init__(self, photo: PhotoInfo, cache: ThumbnailCache, size: int, parent=None,
                 edit=None):
        super().__init__(parent)
        self._photo = photo
        self._cache = cache
        # Edits in progress on this photo (None = none): the thumbnail must
        # reflect them (rotation, crop...), cf. ThumbnailGrid._edits.
        self._edit  = edit
        self._size  = size
        self._selected = False
        self._pixmap: QPixmap | None = None
        self._load_requested = False
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_thumb_ready)
        self._drag_start_pos: QPoint | None = None
        self._dup_badge_rect = None   # QRect in the coordinates of the widget
        self._worker: "_ThumbWorker | None" = None
        self._worker_pool: "QThreadPool | None" = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setFixedSize(self._size + 8, self._size + 8)
        self.setCursor(Qt.PointingHandCursor)
        # Accessible name (UIA/QAccessible): lets the end-to-end tests
        # (tests/e2e, pywinauto) target a precise thumbnail by photo path
        # rather than by guessed screen coordinates.
        self.setAccessibleName(f"thumb::{self._photo.path}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        self._img_label = LoadingLabel("#373737")
        self._img_label.setFixedSize(self._size, self._size)
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.start_loading()
        layout.addWidget(self._img_label)

    def load(self, priority: int = 0) -> None:
        if self._load_requested:
            return
        self._load_requested = True
        # Check only the RAM cache in the UI thread (non-blocking).
        # The DB check and the PIL generation are delegated to the worker.
        pixmap = self._cache.get_ram(self._photo.path, edit_signature(self._edit))
        if pixmap:
            self._set_pixmap(pixmap)
        else:
            worker = _ThumbWorker(self._photo.path, self._cache, self._signals,
                                  self._edit)
            from pathlib import Path as _P
            from src.library.exif_reader import VIDEO_EXT as _VE
            pool = (_get_video_thumb_pool()
                    if _P(self._photo.path).suffix.lower() in _VE
                    else _get_thumb_pool())
            self._worker = worker
            self._worker_pool = pool
            pool.start(worker, priority)

    def cancel_pending_load(self) -> None:
        """Removes the worker from the queue of the pool if it has not started yet.
        To be called when the cell goes out of view (scroll) before its deletion:
        without that, the worker decodes the thumbnail anyway for a photo that has become
        invisible, at the expense of the photos really displayed (cf. user
        request: the oldest display requests must be
        abandoned when they pile up)."""
        if self._worker is not None and self._worker_pool is not None:
            try:
                self._worker_pool.tryTake(self._worker)
            except RuntimeError:
                # The worker has AutoDelete=True: if it has already started/finished, Qt has
                # destroyed the underlying C++ object before we could remove it.
                pass
        self._worker = None
        self._worker_pool = None

    def reload_with_edit(self, edit) -> None:
        """Regenerates the thumbnail for a new edit state.

        No invalidate() of the cache: the stored entry carries the fingerprint of
        the old state, it therefore cannot be served again for the new one, and
        generate() will replace it (primary key = path)."""
        self._edit = edit
        worker = _ThumbWorker(self._photo.path, self._cache, self._signals, edit)
        _get_thumb_pool().start(worker)

    @Slot(str, object, str)
    def _on_thumb_ready(self, path: str, data: object, edit_sig: str) -> None:
        if path == self._photo.path:
            pixmap = QPixmap()
            pixmap.loadFromData(QByteArray(data))
            if not pixmap.isNull():
                self._cache.store_pixmap(path, pixmap, edit_sig)
                if edit_sig != edit_signature(self._edit):
                    # Result of a generation overtaken by a more
                    # recent edit: put in cache (it is valid for its fingerprint),
                    # but not displayed - the worker in progress has the last word.
                    return
                self._set_pixmap(pixmap)
            else:
                logger.warning("Pixmap null pour %s", path)

    def _set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        scaled = pixmap.scaled(self._size, self._size,
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if self._photo.media_type == "video":
            scaled = self._add_video_badge(scaled)
        if self._photo.duplicate_group_id is not None:
            # Compute the position of the badge in the coordinates of the widget
            # (_img_label starts at (4,4), pixmap centred in the label)
            pw = scaled.width()
            ph = scaled.height()
            lbl_ox = (self._size - pw) // 2
            lbl_oy = (self._size - ph) // 2
            bx = 4 + lbl_ox + pw - _DUP_BADGE_W - 2
            by = 4 + lbl_oy + 2
            self._dup_badge_rect = QRect(bx - 4, by - 4,
                                         _DUP_BADGE_W + 8, _DUP_BADGE_H + 8)
            scaled = self._add_duplicate_badge(scaled)
        else:
            self._dup_badge_rect = None
        if self._photo.rating > 0:
            scaled = self._add_rating_badge(scaled, self._photo.rating)
        self._img_label.setPixmap(scaled)

    def _add_duplicate_badge(self, pixmap: QPixmap) -> QPixmap:
        result = QPixmap(pixmap)
        p = QPainter(result)
        p.setRenderHint(QPainter.Antialiasing)
        x = result.width() - _DUP_BADGE_W - 2
        y = 2
        p.setBrush(QColor(255, 140, 0))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(x, y, _DUP_BADGE_W, _DUP_BADGE_H, 4, 4)
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(x, y, _DUP_BADGE_W, _DUP_BADGE_H), Qt.AlignCenter, "⧉")
        p.end()
        return result

    def _add_rating_badge(self, pixmap: QPixmap, rating: int) -> QPixmap:
        result = QPixmap(pixmap)
        p = QPainter(result)
        p.setRenderHint(QPainter.Antialiasing)
        text = "★" * rating
        f = QFont()
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        w = QFontMetrics(f).horizontalAdvance(text) + 8
        x, y = 2, result.height() - _RATING_BADGE_H - 2
        p.setBrush(QColor(0, 0, 0, 170))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(x, y, w, _RATING_BADGE_H, 4, 4)
        p.setPen(QColor(255, 210, 0))
        p.drawText(QRect(x, y, w, _RATING_BADGE_H), Qt.AlignCenter, text)
        p.end()
        return result

    def _add_video_badge(self, pixmap: QPixmap) -> QPixmap:
        result = QPixmap(pixmap)
        p = QPainter(result)
        p.setRenderHint(QPainter.Antialiasing)
        r = max(18, min(result.width(), result.height()) // 4)
        cx, cy = result.width() // 2, result.height() // 2
        p.setBrush(QColor(0, 0, 0, 150))
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPixelSize(max(14, r))
        p.setFont(f)
        p.drawText(QRect(cx - r, cy - r, 2 * r, 2 * r), Qt.AlignCenter, "▶")
        p.end()
        return result

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setStyleSheet(
            "background: rgba(70,130,180,120); border-radius:4px;" if selected else ""
        )

    def set_size(self, size: int) -> None:
        self._size = size
        self._load_requested = False          # allows a reload at the new size
        self.setFixedSize(size + 8, size + 8)
        self._img_label.setFixedSize(size, size)
        if self._pixmap:
            scaled = self._pixmap.scaled(size, size,
                                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._img_label.setPixmap(scaled)
        else:
            self._img_label.start_loading()

    @property
    def photo(self) -> PhotoInfo:
        return self._photo

    def mouseDoubleClickEvent(self, _event) -> None:
        self.double_clicked.emit(self._photo)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self.right_clicked.emit(self._photo, event.globalPosition().toPoint())
        elif event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            if (self._dup_badge_rect is not None
                    and self._dup_badge_rect.contains(pos)):
                self.duplicate_clicked.emit(self._photo)
                return
            self._drag_start_pos = pos
            self.clicked.emit(self._photo, QApplication.keyboardModifiers())
        super().mousePressEvent(event)

    def set_duplicate_group(self, group_id) -> None:
        self._photo.duplicate_group_id = group_id
        if self._pixmap is not None:
            self._set_pixmap(self._pixmap)

    def set_rating(self, rating: int) -> None:
        self._photo.rating = rating
        if self._pixmap is not None:
            self._set_pixmap(self._pixmap)

    def mouseMoveEvent(self, event) -> None:
        if (self._drag_start_pos is not None
                and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - self._drag_start_pos).manhattanLength()
                    >= QApplication.startDragDistance()):
            self._drag_start_pos = None
            self.drag_started.emit(self._photo)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.right_clicked.emit(self._photo, event.globalPos())


class _GridContainer(QWidget):
    """
    Virtual container for the uniform grid (scroll mode).
    In ribbon mode the height is managed by the parent QScrollArea.
    """
    layout_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total   = 0
        self._cell_w  = 188
        self._cell_h  = 188
        self._spacing = 6
        self._cols    = 1
        self._managed_height = True   # False in ribbon mode (size managed by Qt)

    def configure(self, total: int, cell_w: int, cell_h: int) -> None:
        self._total  = total
        self._cell_w = cell_w
        self._cell_h = cell_h
        self._recompute(check_cols=False)

    def set_total(self, total: int) -> None:
        self._total = total
        self._recompute(check_cols=False)

    def set_cell_width(self, w: int) -> None:
        self._cell_w = w
        self._recompute(check_cols=True)

    def _recompute(self, check_cols: bool = True) -> None:
        if not self._managed_height:
            return
        parent_w = (self.parentWidget().width()
                    if self.parentWidget() else self.width()) or 800
        old_cols = self._cols
        self._cols = max(1, (parent_w + self._spacing) // (self._cell_w + self._spacing))
        rows = max(1, (self._total + self._cols - 1) // self._cols) if self._total else 1
        h = rows * (self._cell_h + self._spacing) + self._spacing
        self.setFixedHeight(h)
        if check_cols and self._cols != old_cols:
            self.layout_changed.emit()

    def cell_rect(self, index: int) -> QRect:
        row = index // self._cols
        col = index % self._cols
        x = self._spacing + col * (self._cell_w + self._spacing)
        y = self._spacing + row * (self._cell_h + self._spacing)
        return QRect(x, y, self._cell_w, self._cell_h)

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def cell_h(self) -> int:
        return self._cell_h

    @property
    def spacing(self) -> int:
        return self._spacing

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._managed_height:
            # Ribbon mode: the container fills the viewport, we signal the change
            self.layout_changed.emit()
        else:
            self._recompute(check_cols=True)


class ThumbnailGrid(QScrollArea):
    """
    Virtual grid of thumbnails.

    Scroll mode (default)
    ─────────────────────
    A scrolling uniform grid. At the first display, only the visible part is
    instantiated (fast start). From the first movement in the grid on, a
    margin of one full screen is maintained above and below the visible
    area (cf. _visible_range/_buffer_active) to anticipate the rest of the
    scrolling. Supports 100,000+ photos.

    Ribbon mode (set_ribbon_mode(True))
    ───────────────────────────────────
    5 rows of lenticular sizes (outer.intermediate.CENTRE.intermediate.outer)
    occupy exactly the display area. The grid is fixed; the photos scroll
    as a ribbon through the wheel (up = shift right by 1 cell, down = shift left by 1 cell).
    The end of a row carries on at the beginning of the following row.
    """

    photo_activated           = Signal(object)
    selection_changed         = Signal(list)
    rename_requested          = Signal(object)
    move_requested            = Signal(object)
    delete_requested          = Signal(list)
    remove_from_album_requested = Signal(list)    # list[PhotoInfo] - remove from the displayed album
    save_requested            = Signal(object)
    duplicate_clicked         = Signal(object)   # PhotoInfo - duplicate badge clicked
    add_to_album_requested    = Signal(list)      # list[PhotoInfo] - add to an existing album
    create_album_with_requested = Signal(list)    # list[PhotoInfo] - create a new album
    retry_face_index_requested = Signal(object)   # PhotoInfo - retry the face identification
    favorite_toggle_requested = Signal(object)    # PhotoInfo - favourite toggle requested
    rating_change_requested  = Signal(list, int)  # list[PhotoInfo], rating 0-5 - rating change requested
    edit_tags_requested       = Signal(list)      # list[PhotoInfo] - keyword editing requested

    def __init__(self, cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self._cache       = cache
        self._thumb_size  = 180
        self._photos: list[PhotoInfo] = []
        # Path -> PhotoInfo index, maintained in parallel with _photos: avoids the
        # O(n) walks of the whole list at every selection change
        # (get_selected/select_photo are on the hot click/keyboard path).
        self._by_path: dict[str, PhotoInfo] = {}
        self._selected: set[str] = set()
        self._materialized: dict[int, ThumbnailCell] = {}
        # Album displayed (through set_album_context()), otherwise None: governs the
        # "Remove from the album" action of the context menu and the behaviour of the Del key.
        self._album_id: int | None = None
        # False as long as no scroll has happened since the last display: only
        # the visible part is prepared then. Switches to True at the 1st _on_scroll(),
        # which activates the margin of one screen above/below (cf. _visible_range).
        self._buffer_active = False
        # Paths (normpath) of the photos in face indexing error (timeout/crash,
        # not excluded) - governs the display of "Retry the face identification"
        # in the context menu. Updated by MainWindow.set_index_error_paths().
        self._index_error_paths: set[str] = set()
        # Edits in progress, by normalised path - the thumbnails must
        # reflect them (rotation, crop...). Reloaded in a single query at every
        # set_photos() through the provider set by MainWindow, and maintained one
        # by one by refresh_photo(): a virtualised grid has no cell
        # to refresh for a photo out of view at the time of the edit.
        self._edits: dict[str, object] = {}
        self._edit_provider = None

        # ── Ribbon mode ──
        self._ribbon_mode   = False
        self._ribbon_offset = 0
        # Metrics computed from the size of the container
        self._r_thumb: list[int] = []    # thumb size per row
        self._r_widget: list[int] = []   # widget size (thumb+8) per row
        self._r_cols: list[int]   = []   # nb of columns per row
        self._r_row_y: list[int]  = []   # Y of each row
        self._r_total: int        = 0    # total visible photos

        # Inertia (wheel): fractional speed + accumulation of the remainder
        self._inertia_vel  = 0.0
        self._inertia_frac = 0.0
        self._inertia_timer = QTimer(self)
        self._inertia_timer.setInterval(16)   # ~60 fps
        self._inertia_timer.timeout.connect(self._inertia_tick)

        self.setFocusPolicy(Qt.StrongFocus)

        self._container = _GridContainer()
        self._container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._container.layout_changed.connect(self._on_layout_changed)
        self.setWidget(self._container)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Timers for the scroll mode only
        self._placeholder_timer = QTimer(self)
        self._placeholder_timer.setSingleShot(True)
        self._placeholder_timer.setInterval(0)
        self._placeholder_timer.timeout.connect(self._update_placeholders)

        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(50)
        self._load_timer.timeout.connect(self._start_loading)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # Date indicator (chronological view)
        self._date_overlay_enabled = False
        self._date_label = QLabel("", self.viewport())
        self._date_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._date_label.setStyleSheet(
            "QLabel {"
            "  color: white;"
            "  font-size: 26px;"
            "  font-weight: bold;"
            "  background-color: rgba(0,0,0,170);"
            "  border-radius: 8px;"
            "  padding: 5px 18px;"
            "}"
        )
        self._date_label.hide()

        # Empty state with a message + action (e.g. a "DVD copy" folder with no
        # catalogued photo) - generic, with no knowledge of the use case:
        # the caller supplies the text and the callback of the button.
        self._empty_overlay = QWidget(self.viewport())
        self._empty_overlay.setStyleSheet(
            "QWidget { background-color: rgba(40,40,40,220); border-radius: 10px; }"
        )
        _empty_layout = QVBoxLayout(self._empty_overlay)
        _empty_layout.setContentsMargins(24, 20, 24, 20)
        _empty_layout.setSpacing(12)
        self._empty_label = QLabel("", self._empty_overlay)
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet("QLabel { color: white; font-size: 14px; }")
        self._empty_label.setMaximumWidth(320)
        _empty_layout.addWidget(self._empty_label)
        self._empty_action_btn = QPushButton("", self._empty_overlay)
        _empty_layout.addWidget(self._empty_action_btn)
        self._empty_overlay.hide()

        # "Loading..." indicator during a photo query (folder/album
        # selected): immediate visual feedback on a click in the sidebar. Deferred
        # by 150 ms so as not to flicker when the query answers quickly.
        self._loading_label = QLabel(translate("ThumbnailGrid", "Loading…"), self.viewport())
        self._loading_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._loading_label.setStyleSheet(
            "QLabel {"
            "  color: white;"
            "  font-size: 15px;"
            "  background-color: rgba(0,0,0,170);"
            "  border-radius: 8px;"
            "  padding: 8px 22px;"
            "}"
        )
        self._loading_label.hide()
        self._loading_delay_timer = QTimer(self)
        self._loading_delay_timer.setSingleShot(True)
        self._loading_delay_timer.setInterval(150)
        self._loading_delay_timer.timeout.connect(self._show_loading_label)

        # Fast navigation scrollbar (ribbon mode only)
        # The bar is supplied from the outside through bind_ribbon_nav_bar()
        # and placed in the parent layout - not as an overlay on the viewport.
        self._nav_bar: QScrollBar | None = None
        self._nav_bar_updating = False
        self._nav_scrolling = False   # True during the fast scroll through the scrollbar
        self._nav_settle_timer = QTimer(self)
        self._nav_settle_timer.setSingleShot(True)
        self._nav_settle_timer.setInterval(500)
        self._nav_settle_timer.timeout.connect(self._on_nav_settled)

    # ══════════════════════════════════════════════════════════════════ loading

    def set_loading(self, on: bool) -> None:
        """Displays (after 150 ms) or hides the "Loading..." indicator - called
        by main_window when a photo query starts/completes. set_photos()
        and clear() hide it automatically too."""
        if on:
            self._loading_delay_timer.start()
        else:
            self._loading_delay_timer.stop()
            self._loading_label.hide()

    def _show_loading_label(self) -> None:
        self._loading_label.adjustSize()
        vp = self.viewport().rect()
        x = max(0, (vp.width() - self._loading_label.width()) // 2)
        y = max(0, (vp.height() - self._loading_label.height()) // 2)
        self._loading_label.move(x, y)
        self._loading_label.show()
        self._loading_label.raise_()

    # ══════════════════════════════════════════════════════════════════ data

    def set_edit_provider(self, provider) -> None:
        """Provider of the edits in progress: callable() -> dict[path, EditInfo].

        Queried at every set_photos() rather than injected by the caller: the
        grid is repopulated from a dozen different places, none of
        which would have to know about the edits."""
        self._edit_provider = provider
        self._reload_edits()

    def _reload_edits(self) -> None:
        if self._edit_provider is None:
            return
        try:
            self._edits = {
                os.path.normpath(p): e for p, e in self._edit_provider().items()
            }
        except Exception:
            logger.debug("Erreur de lecture des retouches pour la grille", exc_info=True)

    def _edit_for(self, photo_path: str):
        return self._edits.get(os.path.normpath(photo_path))

    def set_photos(self, photos: list[PhotoInfo]) -> None:
        self.set_loading(False)
        self.clear_empty_message()
        self._selected.clear()
        self._cancel_pending_workers()
        self._dematerialize_all()
        self._reload_edits()
        self._photos = list(photos)
        self._by_path = {p.path: p for p in self._photos}
        if self._ribbon_mode:
            self._buffer_active = False
            self._ribbon_offset = 0
            QTimer.singleShot(0, self._ribbon_full_update)
        else:
            # configure() may reduce the height of the container and force a
            # clamping of the scrollbar (old position invalid in the
            # new, shorter list): that would emit valueChanged ->
            # _on_scroll() -> would reactivate the buffer margin even before the 1st
            # display. We block the signal for the duration of the resizing
            # to guarantee a "visible only" render at the first display.
            vbar = self.verticalScrollBar()
            blocked = vbar.blockSignals(True)
            self._container.configure(len(photos), self._thumb_size + 8, self._thumb_size + 8)
            vbar.blockSignals(blocked)
            self._buffer_active = False
            QTimer.singleShot(0, self._update_materialized)

    def add_photo(self, photo: PhotoInfo) -> None:
        self._photos.append(photo)
        self._by_path[photo.path] = photo
        if self._ribbon_mode:
            QTimer.singleShot(0, self._update_ribbon_cells)
        else:
            self._container.set_total(len(self._photos))
            QTimer.singleShot(0, self._update_materialized)

    def add_photos_batch(self, photos: list[PhotoInfo]) -> None:
        if not photos:
            return
        self._photos.extend(photos)
        self._by_path.update((p.path, p) for p in photos)
        if self._ribbon_mode:
            QTimer.singleShot(0, self._update_ribbon_cells)
        else:
            self._container.set_total(len(self._photos))
            QTimer.singleShot(0, self._update_materialized)

    def remove_photos(self, paths: list[str]) -> None:
        paths_set = set(paths)
        self._dematerialize_all()
        self._photos = [p for p in self._photos if p.path not in paths_set]
        for path in paths_set:
            self._by_path.pop(path, None)
        self._selected -= paths_set
        if self._ribbon_mode:
            self._clamp_ribbon_offset()
            QTimer.singleShot(0, self._update_ribbon_cells)
        else:
            self._container.set_total(len(self._photos))
            QTimer.singleShot(0, self._update_materialized)
        self.selection_changed.emit(self.get_selected())

    def scroll_to_photo(self, path: str) -> None:
        """Brings the thumbnail of path back into the visible area: centred in
        ribbon (chronological) mode, minimal just-sufficient scrolling otherwise."""
        idx = next((i for i, p in enumerate(self._photos) if p.path == path), None)
        if idx is None:
            return
        if self._ribbon_mode:
            if not self._r_cols:
                return
            self._ribbon_offset = idx - self._center_pos()
            self._clamp_ribbon_offset()
            self._update_ribbon_cells()
            self._update_date_overlay()
        else:
            rect = self._container.cell_rect(idx)
            vbar = self.verticalScrollBar()
            vp_h = max(1, self.viewport().height())
            spacing = self._container.spacing
            if rect.top() < vbar.value():
                vbar.setValue(rect.top() - spacing)
            elif rect.bottom() > vbar.value() + vp_h:
                vbar.setValue(rect.bottom() - vp_h + spacing)

    def refresh_photo(self, photo_path: str, edit) -> None:
        """Takes note of a new edit state for a photo.

        The table is updated in every case - it is what will make the
        thumbnail regenerate at the next materialisation of the cell if the
        photo is not on screen (a virtualised grid: at the moment when
        the user edits from the viewer, its cell most
        often does not exist)."""
        key = os.path.normpath(photo_path)
        if edit is not None and edit.is_modified():
            self._edits[key] = edit
        else:
            self._edits.pop(key, None)
        for cell in self._materialized.values():
            if os.path.normpath(cell.photo.path) == key:
                cell.reload_with_edit(edit)
                return

    def clear(self) -> None:
        self.set_loading(False)
        self.clear_empty_message()
        self._selected.clear()
        self._cancel_pending_workers()
        self._dematerialize_all()
        self._buffer_active = False
        self._photos.clear()
        self._by_path.clear()
        if not self._ribbon_mode:
            self._container.set_total(0)

    def update_photo_path(self, old_path: str, new_path: str) -> None:
        new_p = Path(new_path)
        photo = self._by_path.pop(old_path, None)
        if photo is not None:
            photo.path     = new_path
            photo.filename = new_p.name
            photo.directory = str(new_p.parent)
            self._by_path[new_path] = photo
        if old_path in self._selected:
            self._selected.discard(old_path)
            self._selected.add(new_path)

    # ══════════════════════════════════════════════════════════════════ selection

    def get_selected(self) -> list[PhotoInfo]:
        # O(selection) and not O(library) - called at every click/arrow.
        # The order is not that of the grid (order of the set): the
        # current consumers treat the result as a set.
        return [self._by_path[p] for p in self._selected if p in self._by_path]

    def select_all(self) -> None:
        self._selected = {p.path for p in self._photos}
        for cell in self._materialized.values():
            cell.set_selected(True)
        self.selection_changed.emit(self.get_selected())

    def select_photo(self, path: str) -> None:
        """Selects a single photo by path and emits selection_changed."""
        if path not in self._by_path:
            return
        self._clear_selection()
        self._selected.add(path)
        self._set_cell_selected(path, True)
        self.selection_changed.emit(self.get_selected())

    def set_thumbnail_size(self, size: int) -> None:
        self._thumb_size = size
        if self._ribbon_mode:
            return          # size determined by the viewport, not by this setting
        self._cancel_pending_workers()
        self._dematerialize_all()
        vbar = self.verticalScrollBar()
        blocked = vbar.blockSignals(True)
        self._container.configure(len(self._photos), size + 8, size + 8)
        vbar.blockSignals(blocked)
        self._buffer_active = False
        QTimer.singleShot(0, self._update_materialized)

    def bind_ribbon_nav_bar(self, bar: QScrollBar) -> None:
        """Binds the external QScrollBar governing the navigation of the ribbon.

        Must be called only once from main_window, before any navigation.
        """
        if self._nav_bar is not None:
            self._nav_bar.valueChanged.disconnect(self._on_nav_bar_scroll)
        self._nav_bar = bar
        self._nav_bar.setSingleStep(1)
        self._nav_bar.hide()
        self._nav_bar.valueChanged.connect(self._on_nav_bar_scroll)

    # ══════════════════════════════════════════════════════════════════ ribbon mode

    def set_ribbon_mode(self, enabled: bool) -> None:
        if self._ribbon_mode == enabled:
            return
        self._ribbon_mode = enabled
        self._cancel_pending_workers()
        self._dematerialize_all()
        self._ribbon_offset = 0

        if enabled:
            self._container._managed_height = False
            # Clear the setFixedHeight constraints inherited from the scroll mode
            # so that setWidgetResizable can resize the container freely.
            self._container.setMinimumHeight(0)
            self._container.setMaximumHeight(16777215)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            QTimer.singleShot(0, self._ribbon_full_update)
        else:
            self._inertia_timer.stop()
            self._inertia_vel  = 0.0
            self._inertia_frac = 0.0
            if self._nav_bar is not None:
                self._nav_bar.hide()
            self._container._managed_height = True
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self._container.configure(
                len(self._photos), self._thumb_size + 8, self._thumb_size + 8
            )
            QTimer.singleShot(0, self._update_materialized)

    def _clamp_ribbon_offset(self) -> None:
        """Adjusts ribbon_offset so that the 1st/last photo can be centred.

        Allowed range:
          min = -(position of the centre photo in the ribbon)
               -> first photo at the centre of the centre row
          max = (N-1) - center_pos
               -> last photo at the centre of the centre row
        The slots outside [0, N-1] simply stay empty.
        """
        if not self._r_cols or not self._photos:
            self._ribbon_offset = 0
            return
        center_pos = sum(self._r_cols[:_RIBBON_CENTER]) + self._r_cols[_RIBBON_CENTER] // 2
        lo = -center_pos
        hi = max(lo, len(self._photos) - 1 - center_pos)
        self._ribbon_offset = max(lo, min(self._ribbon_offset, hi))

    # ── Navigation scrollbar ───────────────────────────────────────────────────

    def _center_pos(self) -> int:
        """Index of the centre cell in the ribbon (centre row, middle column)."""
        if not self._r_cols:
            return 0
        return sum(self._r_cols[:_RIBBON_CENTER]) + self._r_cols[_RIBBON_CENTER] // 2

    def center_photo_index(self) -> int | None:
        """Returns the index of the photo at the centre of the ribbon, or None outside ribbon mode."""
        if not self._ribbon_mode or not self._photos:
            return None
        idx = self._ribbon_offset + self._center_pos()
        return idx if 0 <= idx < len(self._photos) else None

    def _update_nav_bar(self) -> None:
        """Shows/hides and synchronises the external scrollbar (called after a resize)."""
        if self._nav_bar is None:
            return
        if not self._ribbon_mode or not self._photos or not self._r_cols:
            self._nav_bar.hide()
            return
        self._nav_bar.show()
        self._sync_nav_bar()

    def _sync_nav_bar(self) -> None:
        """Updates the range and value of the scrollbar without repositioning it."""
        if self._nav_bar is None or not self._ribbon_mode or not self._r_cols or not self._photos:
            return
        center      = self._ribbon_offset + self._center_pos()
        max_val     = max(0, len(self._photos) - 1)
        page_step   = max(1, self._r_total)
        self._nav_bar_updating = True
        self._nav_bar.setRange(0, max_val)
        self._nav_bar.setPageStep(page_step)
        self._nav_bar.setValue(max(0, min(center, max_val)))
        self._nav_bar_updating = False

    @Slot(int)
    def _on_nav_bar_scroll(self, value: int) -> None:
        if self._nav_bar_updating or not self._r_cols:
            return
        self._ribbon_offset = value - self._center_pos()
        self._clamp_ribbon_offset()
        self._nav_bar_updating = True
        # During the fast scroll: empty the grid only once and only update
        # the date. The cells will be recreated after 500 ms of inactivity.
        if not self._nav_scrolling:
            self._nav_scrolling = True
            self._dematerialize_all()
        self._update_date_overlay()
        self._nav_bar_updating = False
        self._sync_nav_bar()
        self._nav_settle_timer.start()   # restarts the delay at every movement

    def _on_nav_settled(self) -> None:
        """Triggered 500 ms after the last movement of the navigation scrollbar."""
        self._nav_scrolling = False
        self._update_ribbon_cells()

    # ──────────────────────────────────────────────────────────────────────────

    def _ribbon_full_update(self) -> None:
        """Recomputes the metrics then places the cells again."""
        self._recompute_ribbon_layout()
        self._update_ribbon_cells()
        self._update_nav_bar()
        self._update_date_overlay()

    def _recompute_ribbon_layout(self) -> None:
        """Computes the sizes and row positions to fill the viewport exactly.

        Uses viewport().height()/width() and not container.height()/width():
        the container may keep an old setFixedHeight constraint inherited
        from the scroll mode, which would completely falsify the computation of base_size.
        """
        vp_h = max(100, self.viewport().height())
        vp_w = max(100, self.viewport().width())
        n    = len(_RIBBON_FACTORS)
        s    = _RIBBON_SPACING

        # base_size: solves sum(f*base + 8) + (n-1)*s = vp_h
        # -> base * sum_f = vp_h - n*8 - (n-1)*s
        sum_f = sum(_RIBBON_FACTORS)
        base  = max(20.0, (vp_h - n * 8 - (n - 1) * s) / sum_f)

        self._r_thumb  = [max(20, int(f * base)) for f in _RIBBON_FACTORS]
        self._r_widget = [ts + 8 for ts in self._r_thumb]
        self._r_cols   = [
            max(1, (vp_w + s) // (ws + s))
            for ws in self._r_widget
        ]

        # Y positions: rows stacked with no initial spacing, spacing between them
        self._r_row_y = []
        y = 0
        for i, ws in enumerate(self._r_widget):
            self._r_row_y.append(y)
            if i < n - 1:
                y += ws + s

        self._r_total = sum(self._r_cols)

    def _update_ribbon_cells(self) -> None:
        """Places the cells according to ribbon_offset without recomputing the metrics."""
        if not self._ribbon_mode or not self._r_thumb:
            return
        if not self._photos:
            self._dematerialize_all()
            return

        self._clamp_ribbon_offset()

        # Build the target: {photo_idx: (QRect, thumb_size)}
        vp_w = self.viewport().width()
        target: dict[int, tuple[QRect, int]] = {}
        rb_pos = 0
        for row_idx in range(len(_RIBBON_FACTORS)):
            n_cols = self._r_cols[row_idx]
            ws     = self._r_widget[row_idx]
            ts     = self._r_thumb[row_idx]
            y      = self._r_row_y[row_idx]
            # Horizontal centring of the centre row only
            if row_idx == _RIBBON_CENTER:
                row_w   = n_cols * ws + max(0, n_cols - 1) * _RIBBON_SPACING
                x_start = max(0, (vp_w - row_w) // 2)
            else:
                x_start = 0
            for col in range(n_cols):
                photo_idx = self._ribbon_offset + rb_pos
                if 0 <= photo_idx < len(self._photos):
                    x = x_start + col * (ws + _RIBBON_SPACING)
                    target[photo_idx] = (QRect(x, y, ws, ws), ts)
                rb_pos += 1

        # Delete the obsolete cells
        for idx in list(self._materialized.keys()):
            if idx not in target:
                cell = self._materialized.pop(idx)
                cell.cancel_pending_load()
                cell.hide()   # never setParent(None) on a visible widget (ghost window)
                cell.setParent(None)

        # Update or create the cells
        for photo_idx, (rect, ts) in target.items():
            photo = self._photos[photo_idx]
            if photo_idx in self._materialized:
                cell = self._materialized[photo_idx]
                if cell._size != ts:
                    cell.set_size(ts)
                    cell.load(priority=10)
                cell.setGeometry(rect)
            else:
                cell = self._make_cell(photo, ts)
                cell.setParent(self._container)
                cell.setGeometry(rect)
                if photo.path in self._selected:
                    cell.set_selected(True)
                cell.show()
                cell.load(priority=10)
                self._materialized[photo_idx] = cell

        self._sync_nav_bar()

    # ══════════════════════════════════════════════════════════════════ scroll mode

    def _visible_range(self) -> tuple[int, int]:
        scroll_y = self.verticalScrollBar().value()
        vp_h     = max(1, self.viewport().height())
        spacing  = self._container.spacing
        cols     = self._container.cols
        row_h    = self._thumb_size + 8 + spacing

        first_visible_row = max(0, (scroll_y - spacing) // row_h)
        last_visible_row  = (scroll_y + vp_h - spacing) // row_h

        if self._buffer_active:
            # Margin of one full screen above/below, activated only after
            # the 1st movement in the grid (cf. _on_scroll) - the very first
            # display only prepares the visible part for an immediate render.
            buffer_rows = max(1, -(-vp_h // row_h))  # ceil(vp_h / row_h)
            first_row = max(0, first_visible_row - buffer_rows)
            last_row  = last_visible_row + buffer_rows
        else:
            first_row = first_visible_row
            last_row  = last_visible_row

        first_idx = first_row * cols
        last_idx  = min(len(self._photos) - 1, (last_row + 1) * cols - 1)
        return first_idx, last_idx

    def _on_scroll(self) -> None:
        if self._ribbon_mode:
            return
        self._buffer_active = True
        self._update_date_overlay()
        if not self._placeholder_timer.isActive():
            self._placeholder_timer.start()
        self._load_timer.start()

    def _update_placeholders(self) -> None:
        if not self._photos or self._ribbon_mode:
            return
        first_idx, last_idx = self._visible_range()
        needed = set(range(first_idx, last_idx + 1))

        for i in list(self._materialized.keys()):
            if i not in needed:
                cell = self._materialized.pop(i)
                cell.cancel_pending_load()
                cell.hide()   # never setParent(None) on a visible widget (ghost window)
                cell.setParent(None)

        for i in needed:
            if i >= len(self._photos):
                break
            if i not in self._materialized:
                cell = self._make_cell(self._photos[i])
                rect = self._container.cell_rect(i)
                cell.setParent(self._container)
                cell.setGeometry(rect)
                if self._photos[i].path in self._selected:
                    cell.set_selected(True)
                cell.show()
                self._materialized[i] = cell

    def _start_loading(self) -> None:
        for cell in self._materialized.values():
            cell.load(priority=10)

    def _update_materialized(self) -> None:
        if not self._photos:
            self._date_label.hide()
            return
        first_idx, last_idx = self._visible_range()
        needed = set(range(first_idx, last_idx + 1))

        for i in list(self._materialized.keys()):
            if i not in needed:
                cell = self._materialized.pop(i)
                cell.cancel_pending_load()
                cell.hide()   # never setParent(None) on a visible widget (ghost window)
                cell.setParent(None)

        for i in needed:
            if i >= len(self._photos):
                break
            if i not in self._materialized:
                cell = self._make_cell(self._photos[i])
                rect = self._container.cell_rect(i)
                cell.setParent(self._container)
                cell.setGeometry(rect)
                if self._photos[i].path in self._selected:
                    cell.set_selected(True)
                cell.show()
                cell.load(priority=10)
                self._materialized[i] = cell

        self._update_date_overlay()

    # ══════════════════════════════════════════════════════════════════ common

    def _cancel_pending_workers(self) -> None:
        """Cancels the workers waiting in the pools (not started yet).
        To be called before any major reinitialisation of the grid to avoid
        dozens of workers reading the disk for invisible photos.
        The workers already running are not interrupted; their
        results will be stored in the RAM cache and remain reusable."""
        _get_thumb_pool().clear()
        _get_video_thumb_pool().clear()

    def _dematerialize_all(self) -> None:
        for cell in self._materialized.values():
            cell.cancel_pending_load()
            cell.hide()   # never setParent(None) on a visible widget (ghost window)
            cell.setParent(None)
        self._materialized.clear()

    def _on_layout_changed(self) -> None:
        self._dematerialize_all()
        if self._ribbon_mode:
            QTimer.singleShot(0, self._ribbon_full_update)
        else:
            QTimer.singleShot(0, self._update_materialized)

    # ══════════════════════════════════════════════════════════════════ date overlay

    def set_date_overlay_visible(self, enabled: bool) -> None:
        self._date_overlay_enabled = enabled
        if not enabled:
            self._date_label.hide()
        else:
            self._update_date_overlay()

    def _update_date_overlay(self) -> None:
        if not self._date_overlay_enabled or not self._photos:
            self._date_label.hide()
            return

        if self._ribbon_mode:
            if not self._r_cols:
                self._date_label.hide()
                return
            # Centre photo of the middle row
            center_start = sum(self._r_cols[:_RIBBON_CENTER])
            center_mid   = center_start + self._r_cols[_RIBBON_CENTER] // 2
            first_idx    = self._ribbon_offset + center_mid
        else:
            scroll_y  = self.verticalScrollBar().value()
            spacing   = self._container.spacing
            cols      = max(1, self._container.cols)
            row_h     = self._thumb_size + 8 + spacing
            first_row = max(0, (scroll_y - spacing) // row_h)
            first_idx = first_row * cols

        first_idx = max(0, min(first_idx, len(self._photos) - 1))
        photo = self._photos[first_idx]

        dt = photo.date_taken
        if dt is None and photo.file_mtime:
            try:
                dt = _dt.fromtimestamp(photo.file_mtime)
            except Exception:
                dt = None

        if dt is None:
            self._date_label.hide()
            return

        text = f"{dt.day} {_MONTHS[dt.month - 1]} {dt.year}"
        self._date_label.setText(text)
        self._date_label.adjustSize()
        self._date_label.move(12, 12)
        self._date_label.show()
        self._date_label.raise_()

    # ══════════════════════════════════════════════════════════════════ empty state

    def show_empty_message(self, text: str, action_label: str = None, action_callback=None) -> None:
        """Displays a centred message over the grid (e.g. a folder empty of
        photos but actually containing a DVD copy). Erased automatically
        by set_photos()/clear(); the caller asks for it again if the condition still
        holds after the next display."""
        self._empty_label.setText(text)
        if action_label and action_callback is not None:
            self._empty_action_btn.setText(action_label)
            try:
                self._empty_action_btn.clicked.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._empty_action_btn.clicked.connect(action_callback)
            self._empty_action_btn.show()
        else:
            self._empty_action_btn.hide()
        self._empty_overlay.adjustSize()
        self._reposition_empty_overlay()
        self._empty_overlay.show()
        self._empty_overlay.raise_()

    def clear_empty_message(self) -> None:
        self._empty_overlay.hide()

    def _reposition_empty_overlay(self) -> None:
        if not self._empty_overlay.isVisible():
            return
        size = self._empty_overlay.sizeHint()
        vp = self.viewport().rect()
        x = max(0, (vp.width() - size.width()) // 2)
        y = max(0, (vp.height() - size.height()) // 2)
        self._empty_overlay.setGeometry(x, y, size.width(), size.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_empty_overlay()

    # ══════════════════════════════════════════════════════════════════ wheel

    def wheelEvent(self, event) -> None:
        if not self._ribbon_mode:
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y()
        if delta > 0:
            # Immediate step (responsiveness) + coasting impulse for the fast scroll
            self._ribbon_offset -= 1
            self._inertia_vel = min(_INERTIA_MAX_VEL, self._inertia_vel + _INERTIA_IMPULSE)
        elif delta < 0:
            self._ribbon_offset += 1
            self._inertia_vel = max(-_INERTIA_MAX_VEL, self._inertia_vel - _INERTIA_IMPULSE)
        else:
            event.ignore()
            return

        self._clamp_ribbon_offset()
        self._update_ribbon_cells()
        self._update_date_overlay()

        if not self._inertia_timer.isActive():
            self._inertia_timer.start()
        event.accept()

    def _inertia_tick(self) -> None:
        """Tick of the inertia timer (~60 fps): applies the velocity and brakes it."""
        self._inertia_frac += self._inertia_vel
        steps = int(self._inertia_frac)          # truncates towards zero
        if steps:
            self._inertia_frac -= steps
            old = self._ribbon_offset
            self._ribbon_offset -= steps          # vel > 0 -> offset down -> photos towards the right
            self._clamp_ribbon_offset()
            if self._ribbon_offset != old:
                self._update_ribbon_cells()
                self._update_date_overlay()
            else:
                # End stop reached: cut the momentum
                self._inertia_vel  = 0.0
                self._inertia_frac = 0.0
                self._inertia_timer.stop()
                return

        # Braking
        self._inertia_vel *= _INERTIA_FRICTION
        if abs(self._inertia_vel) < _INERTIA_STOP:
            self._inertia_vel  = 0.0
            self._inertia_frac = 0.0
            self._inertia_timer.stop()

    # ══════════════════════════════════════════════════════════════════ factory

    def _make_cell(self, photo: PhotoInfo, size: int | None = None) -> ThumbnailCell:
        if size is None:
            size = self._thumb_size
        cell = ThumbnailCell(photo, self._cache, size, edit=self._edit_for(photo.path))
        cell.double_clicked.connect(self.photo_activated.emit)
        cell.right_clicked.connect(self._on_right_click)
        cell.clicked.connect(self._on_cell_clicked)
        cell.drag_started.connect(self._on_cell_drag_started)
        cell.duplicate_clicked.connect(self.duplicate_clicked.emit)
        return cell

    # ══════════════════════════════════════════════════════════════════ keyboard/mouse events

    @Slot(object, object)
    def _on_cell_clicked(self, photo: PhotoInfo, modifiers) -> None:
        path  = photo.path
        ctrl  = bool(modifiers & Qt.ControlModifier)
        shift = bool(modifiers & Qt.ShiftModifier)

        if ctrl:
            if path in self._selected:
                self._selected.discard(path)
                self._set_cell_selected(path, False)
            else:
                self._selected.add(path)
                self._set_cell_selected(path, True)
        elif shift:
            self._range_select(path)
        else:
            self._clear_selection()
            self._selected.add(path)
            self._set_cell_selected(path, True)

        self.selection_changed.emit(self.get_selected())
        if self._ribbon_mode:
            self.scroll_to_photo(path)

    def _set_cell_selected(self, path: str, selected: bool) -> None:
        for cell in self._materialized.values():
            if cell.photo.path == path:
                cell.set_selected(selected)
                return

    def _clear_selection(self) -> None:
        for cell in self._materialized.values():
            cell.set_selected(False)
        self._selected.clear()

    def _range_select(self, target_path: str) -> None:
        if not self._selected:
            self._selected.add(target_path)
            self._set_cell_selected(target_path, True)
            return
        paths = [p.path for p in self._photos]
        last = next((p for p in reversed(paths) if p in self._selected), None)
        if last is None:
            return
        lo = min(paths.index(last), paths.index(target_path))
        hi = max(paths.index(last), paths.index(target_path))
        for i in range(lo, hi + 1):
            self._selected.add(paths[i])
        for cell in self._materialized.values():
            cell.set_selected(cell.photo.path in self._selected)

    @Slot(object)
    def _on_cell_drag_started(self, photo: PhotoInfo) -> None:
        if photo.path in self._selected:
            photos = [p for p in self._photos if p.path in self._selected]
        else:
            photos = [photo]

        paths_bytes = '\n'.join(p.path for p in photos).encode('utf-8')
        mime = QMimeData()
        mime.setData('application/x-pixelphoto-paths', paths_bytes)

        drag = QDrag(self)
        drag.setMimeData(mime)

        for cell in self._materialized.values():
            if cell.photo.path == photos[0].path and cell._pixmap:
                px = cell._pixmap.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                drag.setPixmap(px)
                drag.setHotSpot(px.rect().center())
                break

        drag.exec(Qt.MoveAction)

    def set_album_context(self, album_id: int | None) -> None:
        """States whether the grid displays the content of an album (and which one), so as
        to offer "Remove from the album" and to point the Del key at it rather
        than at erasing the file permanently."""
        self._album_id = album_id

    def set_index_error_paths(self, paths) -> None:
        """Updates the set of photos in face indexing error
        (timeout/crash) - governs the "Retry the face identification" action
        of the context menu."""
        self._index_error_paths = {os.path.normpath(p) for p in paths}

    def refresh_duplicate_status(self, assignments: dict) -> None:
        """Updates the duplicate badges. assignments = {path: group_id | None}."""
        for photo in self._photos:
            if photo.path in assignments:
                photo.duplicate_group_id = assignments[photo.path]
        for cell in self._materialized.values():
            if cell.photo.path in assignments:
                cell.set_duplicate_group(assignments[cell.photo.path])

    def refresh_rating(self, ratings: dict) -> None:
        """Updates the rating badges. ratings = {path: rating (0-5)}."""
        for photo in self._photos:
            if photo.path in ratings:
                photo.rating = ratings[photo.path]
        for cell in self._materialized.values():
            if cell.photo.path in ratings:
                cell.set_rating(ratings[cell.photo.path])

    def _toggle_favorite_from_menu(self, photo: PhotoInfo) -> None:
        photo.is_favorite = not photo.is_favorite
        self.favorite_toggle_requested.emit(photo)

    def _emit_rating_change(self, photos: list[PhotoInfo], rating: int) -> None:
        self.rating_change_requested.emit(photos, rating)

    @Slot(object, object)
    def _on_right_click(self, photo: PhotoInfo, pos) -> None:
        # Effective selection: full selection if photo is already selected, else just this photo
        if photo.path in self._selected and len(self._selected) > 1:
            photos = self.get_selected()
        else:
            photos = [photo]

        menu = QMenu(self)
        install_menu_width_fix(menu)
        menu.addAction(translate("ThumbnailGrid", "Open"), lambda: self.photo_activated.emit(photo))
        menu.addSeparator()
        fav_label = (translate("ThumbnailGrid", "Remove from favourites") if photo.is_favorite
                     else translate("ThumbnailGrid", "Mark as favourite"))
        menu.addAction(fav_label, lambda: self._toggle_favorite_from_menu(photo))
        rating_menu = menu.addMenu(translate("ThumbnailGrid", "Rate"))
        for n in range(1, 6):
            rating_menu.addAction(
                "★" * n, lambda p=photos, n=n: self._emit_rating_change(p, n)
            )
        rating_menu.addSeparator()
        rating_menu.addAction(
            translate("ThumbnailGrid", "Clear the rating"), lambda p=photos: self._emit_rating_change(p, 0)
        )
        menu.addAction(translate("ThumbnailGrid", "Keywords…"), lambda p=photos: self.edit_tags_requested.emit(p))
        menu.addAction(translate("ThumbnailGrid", "Rename the image"), lambda: self.rename_requested.emit(photo))
        menu.addAction(translate("ThumbnailGrid", "Move to…"), lambda: self.move_requested.emit(photo))
        menu.addAction(translate("ThumbnailGrid", "Save the edited image to disk\tCtrl+S"),
                       lambda: self.save_requested.emit(photo))
        menu.addSeparator()
        n = len(photos)
        lbl = (translate("ThumbnailGrid", "the {n} selected photos").format(n=n)
               if n > 1 else translate("ThumbnailGrid", "this photo"))
        menu.addAction(translate("ThumbnailGrid", "Add {photos} to an album…").format(photos=lbl),
                       lambda p=photos: self.add_to_album_requested.emit(p))
        menu.addAction(translate("ThumbnailGrid", "Create a new album with {photos}…").format(photos=lbl),
                       lambda p=photos: self.create_album_with_requested.emit(p))
        menu.addSeparator()
        menu.addAction(translate("ThumbnailGrid", "Show in File Explorer"),
                       lambda: __import__('os').startfile(
                           __import__('os.path').path.dirname(photo.path)))
        if os.path.normpath(photo.path) in self._index_error_paths:
            menu.addSeparator()
            menu.addAction(translate("ThumbnailGrid", "Retry face identification"),
                           lambda: self.retry_face_index_requested.emit(photo))
        menu.addSeparator()
        if self._album_id is not None:
            # Album view: only the (non-destructive) removal is offered - never
            # the erasing of the file, even in multi-selection (the same rule as the
            # viewer and as the Del key, cf. _emit_delete_or_remove).
            rm_lbl = (translate("ThumbnailGrid", "Remove the photos from the album") if n > 1
                      else translate("ThumbnailGrid", "Remove from the album"))
            menu.addAction(rm_lbl + _DEL_KEY, lambda p=photos: self.remove_from_album_requested.emit(p))
        else:
            del_lbl = (translate("ThumbnailGrid", "Delete the files…") if n > 1
                       else translate("ThumbnailGrid", "Delete the file…"))
            menu.addAction(del_lbl + _DEL_KEY, lambda p=photos: self.delete_requested.emit(p))
        menu.exec(pos)

    def _emit_delete_or_remove(self, photos: list) -> None:
        """Del key: removes from the displayed album if there is one, otherwise erases
        the file(s) permanently."""
        if not photos:
            return
        if self._album_id is not None:
            self.remove_from_album_requested.emit(photos)
        else:
            self.delete_requested.emit(photos)

    def _emit_save_for_single(self, photos: list) -> None:
        """Ctrl+S: only makes sense for a single, unambiguous photo."""
        if len(photos) == 1:
            self.save_requested.emit(photos[0])

    def keyPressEvent(self, event) -> None:
        if self._ribbon_mode:
            key = event.key()
            if key == Qt.Key_Left:
                self._ribbon_offset -= 1
            elif key == Qt.Key_Right:
                self._ribbon_offset += 1
            elif key == Qt.Key_Up:
                self._ribbon_offset -= 3
            elif key == Qt.Key_Down:
                self._ribbon_offset += 3
            elif key == Qt.Key_Delete:
                selected = self.get_selected()
                if selected:
                    self._emit_delete_or_remove(selected)
                else:
                    center_idx = self._ribbon_offset + self._center_pos()
                    if 0 <= center_idx < len(self._photos):
                        self._emit_delete_or_remove([self._photos[center_idx]])
                event.accept()
                return
            elif key == Qt.Key_S and event.modifiers() == Qt.ControlModifier:
                selected = self.get_selected()
                if selected:
                    self._emit_save_for_single(selected)
                else:
                    center_idx = self._ribbon_offset + self._center_pos()
                    if 0 <= center_idx < len(self._photos):
                        self._emit_save_for_single([self._photos[center_idx]])
                event.accept()
                return
            else:
                super().keyPressEvent(event)
                return
            self._clamp_ribbon_offset()
            self._update_ribbon_cells()
            self._update_date_overlay()
            event.accept()
        elif event.key() == Qt.Key_Delete:
            selected = self.get_selected()
            if selected:
                self._emit_delete_or_remove(selected)
        elif event.key() == Qt.Key_S and event.modifiers() == Qt.ControlModifier:
            self._emit_save_for_single(self.get_selected())
        else:
            super().keyPressEvent(event)
