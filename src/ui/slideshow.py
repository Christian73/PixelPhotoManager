# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Full-screen slideshow of the photos of the current album."""

import logging
import random

from PySide6.QtCore import Qt, QRectF, QSize, QTimer, QThread, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QWidget,
)

from src.core.models import PhotoInfo
from src.core.screensaver_guard import ScreensaverGuard, is_screensaver_command
from src.ui.photo_viewer import _build_pixmap
from src.core.i18n import translate

logger = logging.getLogger(__name__)

_INTERVAL_MS      = 5_000   # default delay between two photos (5 s)
_INTERVAL_MIN_MS  = 1_000   # minimum 1 s
_INTERVAL_MAX_MS  = 60_000  # maximum 60 s
_INTERVAL_STEP_MS = 1_000   # adjustment step: 1 s
_OVERLAY_TTL_MS   = 5_000   # delay before the bar is hidden automatically

_KB_FPS  = 30     # frame rate of the Ken Burns animation (frames/s)
_KB_ZOOM = 0.08   # max zoom amplitude (8 %)
_KB_PAN  = 0.55   # fraction of the available margin used for the pan

_BTN_STYLE = (
    "QPushButton { background: rgba(255,255,255,15); color: white; "
    "font-size: 18px; border: none; border-radius: 4px; padding: 6px 14px; }"
    "QPushButton:hover { background: rgba(255,255,255,35); color: #aad4ff; }"
    "QPushButton:disabled { color: #555; background: transparent; }"
)
_BTN_SMALL = (
    "QPushButton { background: rgba(255,255,255,15); color: white; "
    "font-size: 14px; border: none; border-radius: 4px; padding: 4px 10px; }"
    "QPushButton:hover { background: rgba(255,255,255,35); color: #aad4ff; }"
)


class _KenBurnsWidget(QWidget):
    """Full-screen widget with a Ken Burns effect (slow zoom + pan)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self._pixmap:   QPixmap | None = None
        self._kb_start: tuple = (1.0, 0.5, 0.5)   # (zoom, cx, cy)
        self._kb_end:   tuple = (1.0, 0.5, 0.5)
        self._anim_t:   float = 0.0
        self._step:     float = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(1000 // _KB_FPS)
        self._timer.timeout.connect(self._tick)

    def set_pixmap(self, pixmap: QPixmap, duration_ms: int) -> None:
        self._pixmap  = pixmap
        self._anim_t  = 0.0
        self._step    = 1.0 / max(1, duration_ms * _KB_FPS // 1000)
        self._compute_kb()
        self._timer.start()
        self.update()

    def stop(self) -> None:
        self._timer.stop()

    def _compute_kb(self) -> None:
        if not self._pixmap:
            return
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0:
            return

        z_a = 1.0 + random.uniform(0.0, _KB_ZOOM)
        z_b = 1.0 + random.uniform(0.0, _KB_ZOOM)
        z_max = max(z_a, z_b)

        # Pan margin = the fraction of the pixmap not visible at max zoom.
        # Vertical component reduced to 35 % to favour
        # horizontal and diagonal movements.
        pan = 1.0 - 1.0 / z_max
        mx  = pan * _KB_PAN
        my  = pan * _KB_PAN * 0.35

        cx_a = 0.5 + random.uniform(-mx, mx)
        cy_a = 0.5 + random.uniform(-my, my)
        cx_b = 0.5 + random.uniform(-mx, mx)
        cy_b = 0.5 + random.uniform(-my, my)

        self._kb_start = (z_a, cx_a, cy_a)
        self._kb_end   = (z_b, cx_b, cy_b)

    def _tick(self) -> None:
        if self._anim_t >= 1.0:
            self._timer.stop()
            return
        self._anim_t = min(1.0, self._anim_t + self._step)
        self.update()

    def _src_rect(self, t: float) -> QRectF:
        if not self._pixmap:
            return QRectF()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        za, cxa, cya = self._kb_start
        zb, cxb, cyb = self._kb_end
        z  = za  + (zb  - za)  * t
        cx = cxa + (cxb - cxa) * t
        cy = cya + (cyb - cya) * t
        sw = pw / z
        sh = ph / z
        sx = cx * pw - sw / 2
        sy = cy * ph - sh / 2
        sx = max(0.0, min(sx, pw - sw))
        sy = max(0.0, min(sy, ph - sh))
        return QRectF(sx, sy, sw, sh)

    def _dst_rect(self) -> QRectF:
        """Destination rect centred in the widget (letterbox / pillarbox)."""
        if not self._pixmap:
            return QRectF(self.rect())
        pw, ph = self._pixmap.width(), self._pixmap.height()
        ww, wh = self.width(), self.height()
        scale  = min(ww / pw, wh / ph) if pw and ph else 1.0
        dw, dh = pw * scale, ph * scale
        return QRectF((ww - dw) / 2, (wh - dh) / 2, dw, dh)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self._pixmap:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(
                self._dst_rect(),
                self._pixmap,
                self._src_rect(self._anim_t),
            )


class _LoadThread(QThread):
    """Loads a photo (with its edits) in a secondary thread."""

    ready = Signal(int, object)   # (requested_index, QPixmap)

    def __init__(
        self,
        index: int,
        photo: PhotoInfo,
        target_size: QSize,
        edit_db=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index       = index
        self._photo       = photo
        self._target_size = target_size
        self._edit_db     = edit_db

    def run(self) -> None:
        edit = self._edit_db.load(self._photo.path) if self._edit_db else None
        result = _build_pixmap(self._photo, edit)
        if result:
            pixmap, *_ = result
            if self._target_size and not self._target_size.isEmpty():
                pixmap = pixmap.scaled(
                    self._target_size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            self.ready.emit(self._index, pixmap)


class SlideshowWindow(QWidget):
    """
    Full-screen slideshow window.

    Parameters
    ----------
    photos      : ordered list of PhotoInfo (index 0 = the newest)
    start_index : index of the starting photo in the list
    edit_db     : EditDatabase — to apply the non-destructive edits

    Navigation
    ----------
    ▶ / → : newer photo (index - 1)
    ◀ / ← : older photo (index + 1)

    Overlay controls (appear on mouse movement, hidden after 5 s)
    -----------------------------------------------------------------------
    ◀  older  |  [−][Xs][+]  |  ⏸/▶  |  ▶  newer  |  ✕

    As long as the window is open, the screen saver and the display sleep are
    inhibited (cf. `src/core/screensaver_guard.py`).
    """

    def __init__(
        self,
        photos: list[PhotoInfo],
        start_index: int = 0,
        edit_db=None,
        parent=None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self._photos    = photos
        self._index     = max(0, min(start_index, len(photos) - 1))
        self._edit_db   = edit_db
        self._playing   = True
        self._interval  = _INTERVAL_MS

        # Current thread + prefetch cache {index: QPixmap}
        self._load_thread:    _LoadThread | None = None
        self._preload_thread: _LoadThread | None = None
        self._preload_cache:  dict[int, QPixmap] = {}

        # Watching a slideshow without touching the keyboard or the mouse is
        # inactivity as far as Windows is concerned: without this guard, the screen
        # saver (or the monitor turning off) ends up covering the photos. Held for
        # the whole life of the window, pause included — a photo deliberately left
        # on screen must stay visible.
        self._screensaver = ScreensaverGuard()
        self._screensaver.inhibit()

        self.setWindowTitle(translate("SlideshowWindow", "Slideshow"))
        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)

        self._setup_ui()
        self._setup_timers()
        # Connections depending on both: _hide_timer is created in _setup_timers
        for _btn in self._overlay_btns:
            _btn.clicked.connect(self._hide_timer.start)
        self.showFullScreen()
        self._load_current()

    # ------------------------------------------------------------------ UI

    _OVERLAY_H = 90   # total height of the overlay strip (gradient included)

    def _setup_ui(self) -> None:
        # The Ken Burns widget covers the whole window — no root layout
        self._kb_widget = _KenBurnsWidget(self)
        self._kb_widget.setStyleSheet("background: black;")

        # Floating overlay positioned at the bottom through resizeEvent
        # Gradient: transparent at the top → dark at the bottom, for legibility
        self._overlay = QWidget(self)
        self._overlay.setStyleSheet(
            "background: qlineargradient("
            "x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(0,0,0,0), "
            "stop:0.35 rgba(0,0,0,160), "
            "stop:1 rgba(0,0,0,220));"
        )
        self._overlay.setMouseTracking(True)

        ol = QHBoxLayout(self._overlay)
        ol.setContentsMargins(24, 36, 24, 14)   # generous top margins for the gradient
        ol.setSpacing(8)

        # Position counter
        self._lbl_count = QLabel()
        self._lbl_count.setStyleSheet("color: #999; font-size: 12px; min-width: 56px;")
        ol.addWidget(self._lbl_count)

        ol.addStretch()

        # ◀ Older
        self._btn_prev = QPushButton(translate("SlideshowWindow", "◀  Previous"))
        self._btn_prev.setToolTip(translate("SlideshowWindow", "Older photo  (←)"))
        self._btn_prev.setStyleSheet(_BTN_STYLE)
        self._btn_prev.clicked.connect(self._go_older)
        ol.addWidget(self._btn_prev)

        ol.addSpacing(16)

        # Display time control: [−] [Xs] [+]
        btn_minus = QPushButton("−")
        btn_minus.setToolTip(translate("SlideshowWindow", "Shorten the display time"))
        btn_minus.setStyleSheet(_BTN_SMALL)
        btn_minus.setFixedWidth(36)
        btn_minus.clicked.connect(self._decrease_interval)
        ol.addWidget(btn_minus)

        self._lbl_interval = QLabel(self._fmt_interval())
        self._lbl_interval.setStyleSheet(
            "color: #ddd; font-size: 13px; min-width: 34px;"
        )
        self._lbl_interval.setAlignment(Qt.AlignCenter)
        ol.addWidget(self._lbl_interval)

        btn_plus = QPushButton("+")
        btn_plus.setToolTip(translate("SlideshowWindow", "Lengthen the display time"))
        btn_plus.setStyleSheet(_BTN_SMALL)
        btn_plus.setFixedWidth(36)
        btn_plus.clicked.connect(self._increase_interval)
        ol.addWidget(btn_plus)

        ol.addSpacing(16)

        # ⏸ / ▶ Play-pause
        self._btn_playpause = QPushButton("⏸")
        self._btn_playpause.setToolTip(translate("SlideshowWindow", "Pause / Resume  (Space)"))
        self._btn_playpause.setStyleSheet(_BTN_STYLE)
        self._btn_playpause.clicked.connect(self._toggle_play)
        ol.addWidget(self._btn_playpause)

        ol.addSpacing(16)

        # ▶ Newer
        self._btn_next = QPushButton(translate("SlideshowWindow", "Next  ▶"))
        self._btn_next.setToolTip(translate("SlideshowWindow", "Newer photo  (→)"))
        self._btn_next.setStyleSheet(_BTN_STYLE)
        self._btn_next.clicked.connect(self._go_newer)
        ol.addWidget(self._btn_next)

        ol.addStretch()

        # ✕ Close
        btn_close = QPushButton("✕")
        btn_close.setToolTip(translate("SlideshowWindow", "Leave the slideshow  (Esc)"))
        btn_close.setStyleSheet(_BTN_SMALL)
        btn_close.setFixedWidth(36)
        btn_close.clicked.connect(self.close)
        ol.addWidget(btn_close)

        # Buttons kept for connection in __init__ (after _setup_timers)
        self._overlay_btns = (
            self._btn_prev, btn_minus, btn_plus,
            self._btn_playpause, self._btn_next, btn_close,
        )

    def _setup_timers(self) -> None:
        self._advance_timer = QTimer(self)
        self._advance_timer.setInterval(self._interval)
        self._advance_timer.timeout.connect(self._advance)
        self._advance_timer.start()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_OVERLAY_TTL_MS)
        self._hide_timer.timeout.connect(self._overlay.hide)
        self._hide_timer.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        self._kb_widget.setGeometry(0, 0, w, h)
        self._overlay.setGeometry(0, h - self._OVERLAY_H, w, self._OVERLAY_H)

    # ------------------------------------------------------------------ loading

    def _screen_size(self) -> QSize:
        return self._kb_widget.size() or self.size()

    def _load_current(self) -> None:
        if not self._photos:
            return
        photo = self._photos[self._index]
        n = len(self._photos)

        self._lbl_count.setText(f"{n - self._index} / {n}")
        # ◀ (older) enabled if there are older photos left
        self._btn_prev.setEnabled(self._index < n - 1)
        # ▶ (newer) enabled if there are newer photos left
        self._btn_next.setEnabled(self._index > 0)

        # If the photo is already prefetched, display it immediately
        if self._index in self._preload_cache:
            pixmap = self._preload_cache.pop(self._index)
            self._kb_widget.set_pixmap(pixmap, self._interval)
            self._start_preload()
            return

        # The previous photo keeps animating during the load
        self._cancel_load(self._load_thread)
        t = _LoadThread(self._index, photo, self._screen_size(), self._edit_db)
        t.ready.connect(self._on_pixmap_ready)
        t.finished.connect(lambda th=t: self._clear_thread('load', th))
        t.start()
        self._load_thread = t

    def _cancel_load(self, thread: "_LoadThread | None") -> None:
        """Disconnects the ready signal of a running thread (its result will be ignored)."""
        if thread is None:
            return
        try:
            if thread.isRunning():
                try:
                    thread.ready.disconnect()
                except RuntimeError:
                    pass
        except RuntimeError:
            pass  # C++ object already destroyed — nothing to do

    def _clear_thread(self, kind: str, thread: "_LoadThread") -> None:
        """Called on finished: clears the Python reference then schedules the Qt destruction."""
        if kind == 'load' and self._load_thread is thread:
            self._load_thread = None
        elif kind == 'preload' and self._preload_thread is thread:
            self._preload_thread = None
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def _start_preload(self) -> None:
        """Prefetches the next photo in the direction of the automatic advance."""
        next_idx = self._index - 1   # automatic advance = towards the newest
        if not (0 <= next_idx < len(self._photos)):
            next_idx = self._index + 1
        if not (0 <= next_idx < len(self._photos)):
            return
        if next_idx in self._preload_cache:
            return
        self._cancel_load(self._preload_thread)
        photo = self._photos[next_idx]
        t = _LoadThread(next_idx, photo, self._screen_size(), self._edit_db)
        t.ready.connect(self._on_preload_ready)
        t.finished.connect(lambda th=t: self._clear_thread('preload', th))
        t.start()
        self._preload_thread = t

    def _on_pixmap_ready(self, index: int, pixmap: QPixmap) -> None:
        if index != self._index:
            return   # navigation in the meantime, ignore
        self._kb_widget.set_pixmap(pixmap, self._interval)
        self._start_preload()

    def _on_preload_ready(self, index: int, pixmap: QPixmap) -> None:
        self._preload_cache[index] = pixmap

    # ------------------------------------------------------------------ navigation

    def _advance(self) -> None:
        """Automatic advance towards the newer photo; pauses on the last one."""
        if self._index > 0:
            self._index -= 1
            self._load_current()
        else:
            self._set_playing(False)

    def _go_older(self) -> None:
        """◀ / ← : go to the older photo (index + 1)."""
        if self._index < len(self._photos) - 1:
            self._index += 1
            self._load_current()
            if self._playing:
                self._advance_timer.start()

    def _go_newer(self) -> None:
        """▶ / → : go to the newer photo (index - 1)."""
        if self._index > 0:
            self._index -= 1
            self._load_current()
            if self._playing:
                self._advance_timer.start()

    # ------------------------------------------------------------------ interval

    def _fmt_interval(self) -> str:
        s = self._interval // 1000
        return f"{s}s"

    def _decrease_interval(self) -> None:
        self._interval = max(_INTERVAL_MIN_MS, self._interval - _INTERVAL_STEP_MS)
        self._advance_timer.setInterval(self._interval)
        self._lbl_interval.setText(self._fmt_interval())

    def _increase_interval(self) -> None:
        self._interval = min(_INTERVAL_MAX_MS, self._interval + _INTERVAL_STEP_MS)
        self._advance_timer.setInterval(self._interval)
        self._lbl_interval.setText(self._fmt_interval())

    # ------------------------------------------------------------------ play/pause

    def _toggle_play(self) -> None:
        self._set_playing(not self._playing)

    def _set_playing(self, playing: bool) -> None:
        self._playing = playing
        self._btn_playpause.setText("⏸" if playing else "▶")
        self._btn_playpause.setToolTip(
            translate("Slideshow", "Pause  (Space)") if playing
            else translate("Slideshow", "Resume  (Space)")
        )
        if playing:
            self._advance_timer.start()
        else:
            self._advance_timer.stop()

    # ------------------------------------------------------------------ overlay

    def _show_overlay(self) -> None:
        self._overlay.show()
        self.setCursor(Qt.ArrowCursor)
        self._hide_timer.start()

    # ------------------------------------------------------------------ events

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._show_overlay()
        super().mouseMoveEvent(event)

    def nativeEvent(self, event_type, message):
        """Refuses the screen saver launch request addressed to the foreground
        window — a second lock, complementary to `ScreensaverGuard`
        (cf. `src/core/screensaver_guard.py`)."""
        if is_screensaver_command(event_type, message):
            return True, 0
        return super().nativeEvent(event_type, message)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            self.close()
        elif key == Qt.Key_Space:
            self._toggle_play()
            self._show_overlay()
        elif key in (Qt.Key_Left, Qt.Key_Up):
            self._go_older()
            self._show_overlay()
        elif key in (Qt.Key_Right, Qt.Key_Down):
            self._go_newer()
            self._show_overlay()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._screensaver.release()
        self._advance_timer.stop()
        self._hide_timer.stop()
        self._kb_widget.stop()
        # Disconnect the signals so as to ignore any result in progress
        self._cancel_load(self._load_thread)
        self._cancel_load(self._preload_thread)
        # Wait for the threads to finish (2 s each at most) before Qt destroys the widget
        for t in (self._load_thread, self._preload_thread):
            if t is not None:
                try:
                    t.wait(2000)
                except RuntimeError:
                    pass
        self._load_thread = None
        self._preload_thread = None
        super().closeEvent(event)
