# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Regression: feeding the user activity timestamp and applying the CPU
throttling level at startup (MainWindow.eventFilter /
_apply_background_cpu_level), tested as unbound methods against a minimal
object -- the same convention as test_main_window_face_rotation.py.

Original bug: `note_user_activity()` was called nowhere. `_last_activity`
therefore stayed frozen at the module import time, `user_is_idle()` returned
True permanently past IDLE_GRACE_SECONDS, and `effective_cpu_ratio()` was always
1.0 -- the duty cycle never throttled anything, whatever the level chosen in the
settings."""
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QWidget

import src.ui.main_window as main_window_module
from src.core import cpu_throttle
from src.ui.main_window import MainWindow


class _FakeConfig:
    def __init__(self, values: dict | None = None):
        self._values = values or {}
        self.reads: list = []

    def get(self, key, default=None):
        self.reads.append(key)
        return self._values.get(key, default)


class _FakeMainWindow(QWidget):
    _ACTIVITY_EVENTS          = MainWindow._ACTIVITY_EVENTS
    eventFilter               = MainWindow.eventFilter
    _apply_background_cpu_level = MainWindow._apply_background_cpu_level

    def __init__(self, config=None):
        super().__init__()
        self._config = config or _FakeConfig()


@pytest.fixture
def noted(monkeypatch):
    """Counts the calls to note_user_activity() without touching the globals."""
    calls: list = []
    monkeypatch.setattr(
        main_window_module, "note_user_activity", lambda: calls.append(1),
    )
    return calls


class TestActivityEventFilter:
    def test_mouse_press_notes_activity(self, qtbot, noted):
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        win.eventFilter(win, QEvent(QEvent.Type.MouseButtonPress))

        assert len(noted) == 1

    def test_key_press_notes_activity(self, qtbot, noted):
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key_A, Qt.NoModifier))

        assert len(noted) == 1

    def test_wheel_notes_activity(self, qtbot, noted):
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        win.eventFilter(win, QEvent(QEvent.Type.Wheel))

        assert len(noted) == 1

    @pytest.mark.parametrize("event_type", [
        QEvent.Type.Paint,
        QEvent.Type.MouseMove,       # deliberately excluded: hovering != interacting,
        QEvent.Type.Timer,           # and the filter sees *every* event
        QEvent.Type.UpdateRequest,
    ])
    def test_other_events_are_ignored(self, qtbot, noted, event_type):
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        win.eventFilter(win, QEvent(event_type))

        assert noted == []

    def test_event_is_never_swallowed(self, qtbot, noted):
        """The filter is installed on the whole application: returning True would
        prevent the event from reaching the target widget."""
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        assert win.eventFilter(win, QEvent(QEvent.Type.MouseButtonPress)) is False
        assert win.eventFilter(win, QEvent(QEvent.Type.Paint)) is False

    def test_activity_from_another_object_counts(self, qtbot, noted):
        """Modal dialogs and the full-screen viewer are not children of the main
        window -- their events pass through here all the same."""
        win = _FakeMainWindow()
        other = QWidget()
        qtbot.addWidget(win)
        qtbot.addWidget(other)

        win.eventFilter(other, QEvent(QEvent.Type.MouseButtonPress))

        assert len(noted) == 1

    def test_lifts_the_permanent_idle_state(self, qtbot, monkeypatch):
        """The real note_user_activity() this time: without the filter, the
        throttling is lifted for good once the grace delay has passed."""
        saved_ratio, saved_activity = cpu_throttle._ratio, cpu_throttle._last_activity
        try:
            cpu_throttle.set_background_cpu_level("low")
            monkeypatch.setattr(
                cpu_throttle, "_last_activity",
                cpu_throttle.time.monotonic() - cpu_throttle.IDLE_GRACE_SECONDS - 1,
            )
            assert cpu_throttle.effective_cpu_ratio() == pytest.approx(1.0)

            win = _FakeMainWindow()
            qtbot.addWidget(win)
            win.eventFilter(win, QEvent(QEvent.Type.MouseButtonPress))

            assert cpu_throttle.effective_cpu_ratio() == pytest.approx(0.25)
        finally:
            cpu_throttle._ratio = saved_ratio
            cpu_throttle._last_activity = saved_activity


class TestBackgroundCpuLevelAtStartup:
    """Applied at the very beginning of __init__, before the slightest thread
    is started: without that, cpu_throttle would read the configuration lazily on
    the first throttle_tick(), hence from a background thread, instantiating
    Config() outside the UI thread."""

    @pytest.fixture(autouse=True)
    def _restore_ratio(self):
        saved = cpu_throttle._ratio
        yield
        cpu_throttle._ratio = saved

    def test_configured_level_is_applied(self, qtbot):
        win = _FakeMainWindow(_FakeConfig({"performance.background_cpu": "low"}))
        qtbot.addWidget(win)

        win._apply_background_cpu_level()

        assert cpu_throttle.background_cpu_ratio() == pytest.approx(0.25)

    def test_missing_key_uses_the_default_level(self, qtbot):
        config = _FakeConfig()
        win = _FakeMainWindow(config)
        qtbot.addWidget(win)

        win._apply_background_cpu_level()

        assert config.reads == ["performance.background_cpu"]
        assert cpu_throttle.background_cpu_ratio() == pytest.approx(
            cpu_throttle.BACKGROUND_CPU_LEVELS[cpu_throttle.DEFAULT_BACKGROUND_CPU]
        )

    def test_corrupted_level_does_not_raise(self, qtbot):
        win = _FakeMainWindow(_FakeConfig({"performance.background_cpu": 42}))
        qtbot.addWidget(win)

        win._apply_background_cpu_level()

        assert cpu_throttle.background_cpu_ratio() == pytest.approx(
            cpu_throttle.BACKGROUND_CPU_LEVELS[cpu_throttle.DEFAULT_BACKGROUND_CPU]
        )
