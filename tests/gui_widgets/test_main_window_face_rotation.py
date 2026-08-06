# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression : coalescence des rotations 90° successives pour la re-détection
des visages (MainWindow._on_rotation_stepped / _drain_pending_reindex), testée
en méthode non liée contre un objet minimal — comme test_main_window_tags.py.

Bug d'origine : une rotation demandée pendant qu'une re-détection tournait déjà
était purement abandonnée. Deux clics rapides sur ↻ (la détection d'une photo
24 Mpx prend plusieurs secondes) laissaient `indexed_photos.rotation` figé sur
l'orientation *intermédiaire*, alors que la photo affichée était revenue à une
autre : la détection ne retrouvait plus qu'une partie des visages (2 sur 8 dans
le cas réel), et aucune action de l'UI ne permettait d'en sortir."""
import pytest
from PySide6.QtWidgets import QWidget

import src.faces.detector as detector_module
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow
from src.ui.main_window_faces import FacesController


class _FakeThread:
    def __init__(self, running: bool):
        self._running = running
        self.deleted = 0

    def isRunning(self) -> bool:
        return self._running

    def deleteLater(self) -> None:
        self.deleted += 1


class _FakeMainWindow(QWidget):
    _on_rotation_stepped   = MainWindow._on_rotation_stepped
    _drain_pending_reindex = MainWindow._drain_pending_reindex

    def __init__(self, running_thread: bool | None = None):
        super().__init__()
        self._reindex_thread = (
            None if running_thread is None else _FakeThread(running_thread)
        )
        self._pending_reindex = None
        self.started: list = []

    def _start_single_reindex(self, photo_path: str, rotation: int) -> None:
        self.started.append((photo_path, rotation))


@pytest.fixture(autouse=True)
def _insightface_available(monkeypatch):
    monkeypatch.setattr(detector_module, "is_available", lambda: True)


class TestRotationCoalescing:
    def test_starts_immediately_when_idle(self, qtbot):
        win = _FakeMainWindow()
        qtbot.addWidget(win)

        win._on_rotation_stepped("C:/lib/a.jpg", 90)

        assert win.started == [("C:/lib/a.jpg", 90)]
        assert win._pending_reindex is None

    def test_rotation_during_detection_is_queued_not_dropped(self, qtbot):
        win = _FakeMainWindow(running_thread=True)
        qtbot.addWidget(win)

        win._on_rotation_stepped("C:/lib/a.jpg", 180)

        assert win.started == []                                # rien relancé tout de suite
        assert win._pending_reindex == ("C:/lib/a.jpg", 180)    # …mais pas perdu

    def test_only_the_last_rotation_is_kept(self, qtbot):
        win = _FakeMainWindow(running_thread=True)
        qtbot.addWidget(win)

        win._on_rotation_stepped("C:/lib/a.jpg", 180)
        win._on_rotation_stepped("C:/lib/a.jpg", 270)
        win._on_rotation_stepped("C:/lib/a.jpg", 0)

        assert win._pending_reindex == ("C:/lib/a.jpg", 0)

    def test_drain_relaunches_last_rotation_once_thread_stopped(self, qtbot):
        win = _FakeMainWindow(running_thread=True)
        qtbot.addWidget(win)
        win._on_rotation_stepped("C:/lib/a.jpg", 90)
        win._on_rotation_stepped("C:/lib/a.jpg", 0)

        win._reindex_thread._running = False
        win._drain_pending_reindex()

        assert win.started == [("C:/lib/a.jpg", 0)]
        assert win._pending_reindex is None

    def test_drain_retries_while_thread_still_running(self, qtbot, monkeypatch):
        """`finished` est émis depuis run(), donc avant l'arrêt réel du QThread :
        le drain doit réessayer plus tard plutôt que deleteLater() un thread vivant
        (fail-fast Qt 0xC0000409)."""
        scheduled: list = []

        class _FakeQTimer:
            @staticmethod
            def singleShot(ms, fn):
                scheduled.append((ms, fn))

        monkeypatch.setattr(main_window_module, "QTimer", _FakeQTimer)

        win = _FakeMainWindow(running_thread=True)
        qtbot.addWidget(win)
        win._pending_reindex = ("C:/lib/a.jpg", 0)

        win._drain_pending_reindex()

        assert win.started == []
        assert win._pending_reindex == ("C:/lib/a.jpg", 0)   # toujours en attente
        assert len(scheduled) == 1 and scheduled[0][0] == 50

        # Le thread s'arrête : le rappel programmé relance bien la rotation.
        win._reindex_thread._running = False
        scheduled[0][1]()
        assert win.started == [("C:/lib/a.jpg", 0)]

    def test_drain_without_pending_is_a_noop(self, qtbot):
        win = _FakeMainWindow(running_thread=True)
        qtbot.addWidget(win)

        win._drain_pending_reindex()

        assert win.started == []

    def test_insightface_missing_does_not_queue(self, qtbot, monkeypatch):
        monkeypatch.setattr(detector_module, "is_available", lambda: False)
        win = _FakeMainWindow(running_thread=True)
        qtbot.addWidget(win)

        win._on_rotation_stepped("C:/lib/a.jpg", 90)

        assert win.started == [] and win._pending_reindex is None


class TestReindexFinishedDrains:
    """`_on_single_reindex_finished` doit vider la file d'attente — sans ça la
    rotation mémorisée ne serait jamais relancée."""

    def test_finished_calls_drain(self, qtbot):
        class _Host(QWidget):
            _on_single_reindex_finished = FacesController._on_single_reindex_finished

            def __init__(self):
                super().__init__()
                self.drained = 0
                self._face_panel = _HiddenPanel()

            def _drain_pending_reindex(self) -> None:
                self.drained += 1

        class _HiddenPanel:
            def isVisible(self) -> bool:
                return False

        host = _Host()
        qtbot.addWidget(host)
        host._on_single_reindex_finished("C:/lib/a.jpg", 3)

        assert host.drained == 1
