# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression : alimentation de l'horodatage d'activité utilisateur et
application du niveau de bridage CPU au démarrage (MainWindow.eventFilter /
_apply_background_cpu_level), testées en méthodes non liées contre un objet
minimal — même convention que test_main_window_face_rotation.py.

Bug d'origine : `note_user_activity()` n'était appelé nulle part. `_last_activity`
restait donc figé à l'heure d'import du module, `user_is_idle()` renvoyait True en
permanence passé IDLE_GRACE_SECONDS, et `effective_cpu_ratio()` valait toujours
1.0 — le cycle de service ne bridait jamais rien, quel que soit le niveau choisi
dans les paramètres."""
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
    """Compte les appels à note_user_activity() sans toucher aux globales."""
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
        QEvent.Type.MouseMove,       # volontairement exclu : survol ≠ interaction,
        QEvent.Type.Timer,           # et le filtre voit *tous* les événements
        QEvent.Type.UpdateRequest,
    ])
    def test_other_events_are_ignored(self, qtbot, noted, event_type):
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        win.eventFilter(win, QEvent(event_type))

        assert noted == []

    def test_event_is_never_swallowed(self, qtbot, noted):
        """Le filtre est posé sur l'application entière : renvoyer True
        empêcherait l'événement d'atteindre le widget destinataire."""
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        assert win.eventFilter(win, QEvent(QEvent.Type.MouseButtonPress)) is False
        assert win.eventFilter(win, QEvent(QEvent.Type.Paint)) is False

    def test_activity_from_another_object_counts(self, qtbot, noted):
        """Dialogues modaux et visionneuse plein écran ne sont pas des enfants
        de la fenêtre principale — leurs événements passent quand même ici."""
        win = _FakeMainWindow()
        other = QWidget()
        qtbot.addWidget(win)
        qtbot.addWidget(other)

        win.eventFilter(other, QEvent(QEvent.Type.MouseButtonPress))

        assert len(noted) == 1

    def test_lifts_the_permanent_idle_state(self, qtbot, monkeypatch):
        """Le vrai note_user_activity() cette fois : sans le filtre, le bridage
        est définitivement levé passé le délai de grâce."""
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
    """Appliqué au tout début de __init__, avant le démarrage du moindre thread :
    sans ça, cpu_throttle lirait la configuration paresseusement au premier
    throttle_tick(), donc depuis un thread de fond, instanciant Config() hors du
    thread UI."""

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
