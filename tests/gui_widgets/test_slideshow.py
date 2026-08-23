# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/ui/slideshow.py` without a real display: showFullScreen is
neutralised by monkeypatch (no full-screen window during the tests). Covers the
Ken Burns widget (source/destination rectangles, animation), the loading thread
run synchronously, and the logic of SlideshowWindow (navigation, interval,
pause, overlay, keyboard, closing)."""
import os

import pytest
from PIL import Image
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QKeyEvent, QPixmap

from src.core.models import PhotoInfo
from src.ui.slideshow import (
    _INTERVAL_MAX_MS,
    _INTERVAL_MIN_MS,
    _INTERVAL_MS,
    _KenBurnsWidget,
    _LoadThread,
    SlideshowWindow,
)


def _photo(tmp_path, name, size=(64, 48)) -> PhotoInfo:
    p = tmp_path / name
    Image.new("RGB", size, (90, 120, 150)).save(str(p))
    return PhotoInfo(path=os.path.normpath(str(p)), file_size=1, file_mtime=1.0)


@pytest.fixture(autouse=True)
def _no_fullscreen(monkeypatch):
    monkeypatch.setattr(SlideshowWindow, "showFullScreen", lambda self: None)


def _key(key):
    return QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier)


# ------------------------------------------------------------------ Ken Burns


class TestKenBurnsWidget:
    def test_set_pixmap_starts_animation(self, qtbot):
        w = _KenBurnsWidget()
        qtbot.addWidget(w)
        w.resize(200, 200)
        w.set_pixmap(QPixmap(100, 50), duration_ms=1000)
        assert w._pixmap is not None
        assert w._timer.isActive()
        w.stop()
        assert not w._timer.isActive()

    def test_tick_advances_then_stops(self, qtbot):
        w = _KenBurnsWidget()
        qtbot.addWidget(w)
        w.set_pixmap(QPixmap(100, 50), duration_ms=100)
        t0 = w._anim_t
        w._tick()
        assert w._anim_t > t0
        w._anim_t = 1.0
        w._tick()
        assert not w._timer.isActive()

    def test_src_rect_stays_in_pixmap(self, qtbot):
        w = _KenBurnsWidget()
        qtbot.addWidget(w)
        w.set_pixmap(QPixmap(100, 50), duration_ms=1000)
        for t in (0.0, 0.5, 1.0):
            r = w._src_rect(t)
            assert r.left() >= 0 and r.top() >= 0
            assert r.right() <= 100.01 and r.bottom() <= 50.01
            assert r.width() > 0 and r.height() > 0

    def test_dst_rect_letterbox(self, qtbot):
        w = _KenBurnsWidget()
        qtbot.addWidget(w)
        w.resize(200, 200)
        w.set_pixmap(QPixmap(100, 50), duration_ms=1000)
        r = w._dst_rect()
        # 2:1 pixmap in a square widget -> full width, height 100, centred
        assert r.width() == pytest.approx(200)
        assert r.height() == pytest.approx(100)
        assert r.top() == pytest.approx(50)

    def test_empty_widget_rects(self, qtbot):
        w = _KenBurnsWidget()
        qtbot.addWidget(w)
        assert w._src_rect(0.0).isEmpty()
        assert w._dst_rect() == w.rect() or w._dst_rect().isValid()

    def test_paint_event_runs(self, qtbot):
        w = _KenBurnsWidget()
        qtbot.addWidget(w)
        w.resize(100, 100)
        w.set_pixmap(QPixmap(50, 50), duration_ms=500)
        img = w.grab()   # triggers paintEvent
        assert not img.isNull()


# ------------------------------------------------------------------ _LoadThread


class TestLoadThread:
    def test_loads_and_scales(self, qtbot, tmp_path):
        photo = _photo(tmp_path, "a.jpg", size=(64, 48))
        thread = _LoadThread(3, photo, QSize(32, 32))
        ready: list = []
        thread.ready.connect(lambda i, pix: ready.append((i, pix)))
        thread.run()   # synchronous
        assert len(ready) == 1
        idx, pix = ready[0]
        assert idx == 3
        assert pix.width() <= 32 and pix.height() <= 32

    def test_missing_file_no_signal(self, qtbot, tmp_path):
        photo = PhotoInfo(path=str(tmp_path / "absent.jpg"),
                          file_size=1, file_mtime=1.0)
        thread = _LoadThread(0, photo, QSize(32, 32))
        ready: list = []
        thread.ready.connect(lambda i, pix: ready.append(i))
        thread.run()
        assert ready == []


# ------------------------------------------------------------------ SlideshowWindow


class TestSlideshowWindowLogic:
    """Pure logic with an empty list (no loading thread started)."""

    def _win(self, qtbot):
        win = SlideshowWindow([])
        qtbot.addWidget(win)
        return win

    def test_interval_adjustment_bounds(self, qtbot):
        win = self._win(qtbot)
        assert win._fmt_interval() == f"{_INTERVAL_MS // 1000}s"
        win._interval = _INTERVAL_MIN_MS
        win._decrease_interval()
        assert win._interval == _INTERVAL_MIN_MS   # floor
        win._interval = _INTERVAL_MAX_MS
        win._increase_interval()
        assert win._interval == _INTERVAL_MAX_MS   # ceiling
        win._decrease_interval()
        assert win._interval == _INTERVAL_MAX_MS - 1000

    def test_toggle_play(self, qtbot):
        win = self._win(qtbot)
        assert win._playing is True
        win._toggle_play()
        assert win._playing is False
        assert not win._advance_timer.isActive()
        assert win._btn_playpause.text() == "▶"
        win._toggle_play()
        assert win._playing is True
        assert win._advance_timer.isActive()

    def test_advance_at_newest_pauses(self, qtbot):
        win = self._win(qtbot)
        win._index = 0
        win._advance()
        assert win._playing is False

    def test_navigation_while_paused_does_not_resume(self, qtbot):
        photos = [PhotoInfo(path=f"absent_{i}.jpg", file_size=1, file_mtime=1.0)
                  for i in range(3)]
        win = SlideshowWindow(photos, start_index=1)
        qtbot.addWidget(win)
        win._toggle_play()   # pause
        assert not win._advance_timer.isActive()

        win._go_older()
        assert win._playing is False
        assert not win._advance_timer.isActive()
        assert win._btn_playpause.text() == "▶"

        win._go_newer()
        assert win._playing is False
        assert not win._advance_timer.isActive()
        assert win._btn_playpause.text() == "▶"

    def test_show_overlay_restarts_hide_timer(self, qtbot):
        win = self._win(qtbot)
        win._overlay.hide()
        win._show_overlay()
        assert not win._overlay.isHidden()
        assert win._hide_timer.isActive()

    def test_key_escape_closes(self, qtbot):
        win = self._win(qtbot)
        win.keyPressEvent(_key(Qt.Key_Escape))
        qtbot.waitUntil(lambda: not win._advance_timer.isActive(), timeout=2000)

    def test_key_space_toggles(self, qtbot):
        win = self._win(qtbot)
        win.keyPressEvent(_key(Qt.Key_Space))
        assert win._playing is False

    def test_resize_positions_children(self, qtbot):
        from PySide6.QtGui import QResizeEvent
        win = self._win(qtbot)
        win.resize(800, 600)
        # hidden window: Qt defers resizeEvent until it is shown -- invoke it
        win.resizeEvent(QResizeEvent(QSize(800, 600), QSize(0, 0)))
        assert win._kb_widget.geometry().size().width() == 800
        assert win._overlay.geometry().top() == 600 - win._OVERLAY_H


class TestSlideshowWindowWithPhotos:
    def test_load_navigate_and_close(self, qtbot, tmp_path):
        photos = [_photo(tmp_path, f"p{i}.jpg") for i in range(3)]
        win = SlideshowWindow(photos, start_index=1)
        qtbot.addWidget(win)

        qtbot.waitUntil(lambda: win._kb_widget._pixmap is not None, timeout=10000)
        assert win._lbl_count.text() == "2 / 3"
        assert win._btn_prev.isEnabled()   # there is an older photo
        assert win._btn_next.isEnabled()   # and a more recent one

        win._go_newer()
        assert win._index == 0
        assert not win._btn_next.isEnabled()

        win._go_older()
        win._go_older()
        assert win._index == 2
        assert not win._btn_prev.isEnabled()

        win.close()   # closeEvent: timers and threads stopped without an exception

    def test_start_index_clamped(self, qtbot, tmp_path):
        photos = [_photo(tmp_path, "seule.jpg")]
        win = SlideshowWindow(photos, start_index=99)
        qtbot.addWidget(win)
        assert win._index == 0
        win.close()

    def test_screensaver_inhibited_until_close(self, qtbot, tmp_path, monkeypatch):
        """The slideshow inhibits the screensaver as long as it is open."""
        from src.core import screensaver_guard as sg
        calls: list[int] = []
        monkeypatch.setattr(sg, "_set_execution_state",
                            lambda flags: calls.append(flags) or True)

        win = SlideshowWindow([_photo(tmp_path, "p.jpg")])
        qtbot.addWidget(win)
        assert win._screensaver.active is True
        assert calls[0] & sg.ES_DISPLAY_REQUIRED

        win.close()
        assert win._screensaver.active is False
        assert calls[-1] == sg.ES_CONTINUOUS

    def test_native_screensaver_request_is_refused(self, qtbot, tmp_path):
        import ctypes.wintypes

        import shiboken6

        from src.core import screensaver_guard as sg

        win = SlideshowWindow([_photo(tmp_path, "p.jpg")])
        qtbot.addWidget(win)

        msg = ctypes.wintypes.MSG()
        msg.message = sg.WM_SYSCOMMAND
        msg.wParam = sg.SC_SCREENSAVE
        # QWidget.nativeEvent (called by super() on the "not handled" path)
        # refuses a raw integer address: pass a VoidPtr, like Qt.
        ptr = shiboken6.VoidPtr(ctypes.addressof(msg))
        handled, _ = win.nativeEvent(b"windows_generic_MSG", ptr)
        assert handled is True

        msg.wParam = 0xF010   # SC_MOVE: left to the normal handling
        handled, _ = win.nativeEvent(b"windows_generic_MSG", ptr)
        assert handled is False
        win.close()

    def test_preload_cache_used(self, qtbot, tmp_path):
        photos = [_photo(tmp_path, f"p{i}.jpg") for i in range(2)]
        win = SlideshowWindow(photos, start_index=1)
        qtbot.addWidget(win)
        # wait for the preloading of the next photo (index 0)
        qtbot.waitUntil(lambda: 0 in win._preload_cache, timeout=10000)
        win._go_newer()   # consumes the cache
        assert 0 not in win._preload_cache
        win.close()
