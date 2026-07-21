# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression : _on_delete_requested/_start_delete_worker/_on_delete_finished
(main_window.py) mettent en file une suppression confirmée si un worker de
suppression est déjà en cours, au lieu de l'abandonner silencieusement.

Découvert via test_save_options_and_settings.py (e2e) : trois suppressions
déclenchées coup sur coup peuvent avoir un worker encore `isRunning()` (ex.
FaceDatabase.delete_for_paths bloqué plusieurs secondes par une contention
SQLite passagère) au moment où la suppression suivante, déjà confirmée par
l'utilisateur, est demandée. Avant ce correctif, _on_delete_requested
retournait silencieusement dans ce cas (seul un message de statut furtif de
3 s) : la suppression était perdue. Ici, les méthodes réelles de MainWindow
sont appelées en non lié (`MainWindow._methode(fake, ...)`) contre un objet
minimal ne portant que les attributs effectivement lus par le chemin testé —
pas de QMainWindow complet (dépendances trop lourdes : catalog, thumb_cache,
face_db, sidebar, grid, viewer, folder_watcher...)."""
from src.core.models import PhotoInfo
from src.ui.main_window import MainWindow


class _FakeConfig:
    def get(self, key, default=None):
        return True   # ui.delete_no_confirm : jamais de dialogue à fermer ici


class _FakeThread:
    def __init__(self, running: bool):
        self._running = running

    def isRunning(self) -> bool:
        return self._running


class _FakeStatusBar:
    def showMessage(self, *a, **k):
        pass


class _FakeLabel:
    def setText(self, *a, **k):
        pass


class _FakeMainWindow:
    """Porte uniquement les attributs lus par _on_delete_requested (chemin
    file-d'attente) et par la queue de fin dans _on_delete_finished."""

    def __init__(self, delete_thread=None):
        self._delete_thread = delete_thread
        self._pending_deletes: list = []
        self._config = _FakeConfig()
        self._lbl_action = _FakeLabel()
        self.started_with: list = []   # historique des appels à _start_delete_worker

    def statusBar(self):
        return _FakeStatusBar()

    def _start_delete_worker(self, photos: list) -> None:
        self.started_with.append(photos)


class TestDeleteQueueing:
    def test_starts_worker_immediately_when_idle(self):
        fake = _FakeMainWindow(delete_thread=None)
        photos = [PhotoInfo(path="a.jpg")]

        MainWindow._on_delete_requested(fake, photos)

        assert fake.started_with == [photos]
        assert fake._pending_deletes == []

    def test_queues_instead_of_dropping_when_worker_running(self):
        fake = _FakeMainWindow(delete_thread=_FakeThread(running=True))
        photos = [PhotoInfo(path="b.jpg")]

        MainWindow._on_delete_requested(fake, photos)

        # Avant le correctif : silencieusement ignoré (started_with resterait
        # vide ET _pending_deletes resterait vide -> suppression perdue).
        assert fake.started_with == []
        assert fake._pending_deletes == [photos]

    def test_starts_worker_when_previous_thread_finished(self):
        fake = _FakeMainWindow(delete_thread=_FakeThread(running=False))
        photos = [PhotoInfo(path="c.jpg")]

        MainWindow._on_delete_requested(fake, photos)

        assert fake.started_with == [photos]
        assert fake._pending_deletes == []

    def test_on_delete_finished_drains_one_pending_request(self):
        fake = _FakeMainWindow()
        queued = [PhotoInfo(path="d.jpg")]
        fake._pending_deletes = [queued]

        MainWindow._on_delete_finished(
            fake, deleted=[], errors=[], in_viewer=False, viewed_index=0,
            first_deleted_idx=None, affected_groups=set(),
        )

        assert fake.started_with == [queued]
        assert fake._pending_deletes == []

    def test_on_delete_finished_drains_only_one_at_a_time(self):
        fake = _FakeMainWindow()
        first = [PhotoInfo(path="e.jpg")]
        second = [PhotoInfo(path="f.jpg")]
        fake._pending_deletes = [first, second]

        MainWindow._on_delete_finished(
            fake, deleted=[], errors=[], in_viewer=False, viewed_index=0,
            first_deleted_idx=None, affected_groups=set(),
        )

        # Un seul worker démarré par épilogue ; le second reste en file.
        assert fake.started_with == [first]
        assert fake._pending_deletes == [second]

    def test_on_delete_finished_noop_when_queue_empty(self):
        fake = _FakeMainWindow()

        MainWindow._on_delete_finished(
            fake, deleted=[], errors=[], in_viewer=False, viewed_index=0,
            first_deleted_idx=None, affected_groups=set(),
        )

        assert fake.started_with == []
