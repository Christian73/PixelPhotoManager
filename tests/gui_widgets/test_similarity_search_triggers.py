# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Regression: what restarts the search for similar faces
(main_window_faces.py::FacesController).

User report -- "no more faces awaiting confirmation":
_start_similarity_search() had a single caller, _on_clustering_finished()
under the `n_clusters > 0` guard, itself only reachable from
_on_face_indexing_finished when `faces > 0`. On a fully indexed library,
nothing refilled the verification queue any more, even though naming a person
moves their centroid and makes groups that were below the threshold
suggestible.

As in test_face_indexing_requeue.py, the real methods of MainWindow are called
unbound against a minimal object carrying only what the tested path reads --
no QMainWindow, no real thread."""
import pytest

from src.ui.main_window import MainWindow


class _FakeThread:
    def __init__(self, running: bool = False):
        self._running = running

    def isRunning(self) -> bool:
        return self._running


class _FakeTimer:
    """One-shot QTimer: counts the (re)starts, without an event loop."""

    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1


class _FakeLabel:
    def __init__(self):
        self.text = None

    def setText(self, value):
        self.text = value


class _FakeAction:
    def setEnabled(self, *a):
        pass

    def setText(self, *a):
        pass


class _FakeController:
    def __init__(self):
        self._similarity_debounce = _FakeTimer()
        self._lbl_action = _FakeLabel()
        self._face_index_pending = False
        self._face_indexer = None
        self.clustering_calls = 0
        self.similarity_starts = 0
        self.info_boxes: list = []

    # --- collaborators replaced ---------------------------------------
    def _run_clustering(self):
        self.clustering_calls += 1

    def _start_similarity_search(self):
        self.similarity_starts += 1

    def _start_face_indexing(self):
        pass

    def _schedule_similarity_search(self):
        """Called by the code under test (`_on_face_indexing_finished`): we
        delegate to the real implementation, which is the one that must arm the
        timer."""
        MainWindow._schedule_similarity_search(self)

    # --- dependencies of _on_clustering_finished -----------------------
    def _refresh_persons(self):
        pass


@pytest.fixture
def ctrl():
    return _FakeController()


class TestScheduling:
    def test_identification_schedules_a_search(self, ctrl):
        MainWindow._schedule_similarity_search(ctrl)

        assert ctrl._similarity_debounce.starts == 1

    def test_a_burst_of_identifications_coalesces(self, ctrl):
        """The timer is one-shot and restarted on every call: naming ten groups
        in a row must not launch ten searches (each of them walks the whole
        library)."""
        for _ in range(10):
            MainWindow._schedule_similarity_search(ctrl)

        assert ctrl.similarity_starts == 0, "rien ne part avant l'échéance du timer"
        assert ctrl._similarity_debounce.starts == 10  # QTimer.start() restarts the delay


class TestIndexingFinished:
    def test_nothing_new_still_looks_for_similar_faces(self, ctrl):
        """The heart of the bug: without a new face, the grouping is not started --
        that was, until now, the end of the path, hence never a suggestion again."""
        MainWindow._on_face_indexing_finished(ctrl, indexed=0, faces=0)

        assert ctrl.clustering_calls == 0
        assert ctrl._similarity_debounce.starts == 1

    def test_new_faces_delegate_to_clustering(self, ctrl):
        """With new faces, _on_clustering_finished is the one that carries on:
        no double trigger."""
        MainWindow._on_face_indexing_finished(ctrl, indexed=12, faces=30)

        assert ctrl.clustering_calls == 1
        assert ctrl._similarity_debounce.starts == 0


class TestClusteringFinished:
    def _prepare(self, ctrl):
        ctrl._cluster_start_time = 123.0
        ctrl._act_cluster_faces = _FakeAction()
        ctrl._face_cluster_grid = type("G", (), {"refresh": lambda self: None})()
        ctrl._face_panel = type("P", (), {"isVisible": lambda self: False})()

    @pytest.mark.parametrize("n_clusters", [0, 7])
    def test_search_runs_whatever_the_cluster_count(self, ctrl, n_clusters):
        """`n_clusters == 0` does not mean "nothing to compare": the grouping
        returns without doing anything as soon as the number of unidentified
        faces has not moved, while the groups already formed still have to be
        confronted with the people named in the meantime."""
        self._prepare(ctrl)

        MainWindow._on_clustering_finished(ctrl, n_clusters)

        assert ctrl.similarity_starts == 1


class TestManualEntry:
    """Faces > Search for similar faces… -- same processing, but the caller
    is the user: they must get some feedback."""

    def test_runs_immediately_and_cancels_the_pending_debounce(self, ctrl):
        ctrl._similarity_thread = _FakeThread(running=False)
        MainWindow._schedule_similarity_search(ctrl)

        MainWindow._start_similarity_search_manually(ctrl)

        assert ctrl._similarity_debounce.stops == 1, "sinon un second passage suit 30 s après"
        assert ctrl.similarity_starts == 1

    def test_no_thread_yet_still_runs(self, ctrl):
        """No search has run yet: _similarity_thread does not exist (an attribute
        created by _start_similarity_search)."""
        assert not hasattr(ctrl, "_similarity_thread")

        MainWindow._start_similarity_search_manually(ctrl)

        assert ctrl.similarity_starts == 1

    def test_already_running_tells_the_user(self, ctrl, monkeypatch):
        ctrl._similarity_thread = _FakeThread(running=True)
        shown: list = []
        monkeypatch.setattr(
            "src.ui.main_window_faces.QMessageBox.information",
            staticmethod(lambda parent, title, text: shown.append(title)),
        )

        MainWindow._start_similarity_search_manually(ctrl)

        assert shown == ["Search running"]
        assert ctrl.similarity_starts == 0
