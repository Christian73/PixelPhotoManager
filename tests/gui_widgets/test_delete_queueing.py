# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Regression: _on_delete_requested/_start_delete_worker/_on_delete_finished
(main_window.py) queue a confirmed deletion if a deletion worker is already
running, instead of dropping it silently.

Found through test_save_options_and_settings.py (e2e): three deletions
triggered in a row may have a worker still `isRunning()` (e.g.
FaceDatabase.delete_for_paths blocked for several seconds by a transient
SQLite contention) at the moment the next deletion, already confirmed by
the user, is requested. Before this fix, _on_delete_requested returned
silently in that case (only a fleeting 3 s status message): the deletion was
lost. Here, the real methods of MainWindow are called unbound
(`MainWindow._method(fake, ...)`) against a minimal object carrying only the
attributes actually read by the tested path -- no complete QMainWindow
(dependencies too heavy: catalog, thumb_cache, face_db, sidebar, grid,
viewer, folder_watcher...)."""
from src.core.models import PhotoInfo
from src.ui.main_window import MainWindow


class _FakeConfig:
    def get(self, key, default=None):
        return True   # ui.delete_no_confirm: never a dialog to close here


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
    """Carries only the attributes read by _on_delete_requested (the queueing
    path) and by the end-of-run queue in _on_delete_finished."""

    def __init__(self, delete_thread=None):
        self._delete_thread = delete_thread
        self._pending_deletes: list = []
        self._config = _FakeConfig()
        self._lbl_action = _FakeLabel()
        self.started_with: list = []   # history of the calls to _start_delete_worker

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

        # Before the fix: silently ignored (started_with would stay
        # empty AND _pending_deletes would stay empty -> deletion lost).
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

        # A single worker started per epilogue; the second stays queued.
        assert fake.started_with == [first]
        assert fake._pending_deletes == [second]

    def test_on_delete_finished_noop_when_queue_empty(self):
        fake = _FakeMainWindow()

        MainWindow._on_delete_finished(
            fake, deleted=[], errors=[], in_viewer=False, viewed_index=0,
            first_deleted_idx=None, affected_groups=set(),
        )

        assert fake.started_with == []
