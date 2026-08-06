# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression : _start_face_indexing/_on_face_indexing_finished
(main_window_faces.py::FacesController) relancent l'indexation des visages
quand une nouvelle demande arrive alors qu'un FaceIndexThread tourne déjà, au
lieu de l'abandonner silencieusement.

Découvert via test_folder_management.py (e2e) : un re-scan forcé qui termine
pendant que l'indexation de visages du scan précédent tourne encore ne
déclenche jamais d'indexation pour les nouvelles photos — contrairement au cas
symétrique déjà géré (TFWarmUpThread encore actif, _face_index_pending
consommé par _on_warmup_done), _start_face_indexing() ne posait aucune trace
de la demande manquée : la photo restait indéfiniment non indexée tant
qu'aucun scan ultérieur ne retombait sur une fenêtre où l'indexeur était
inactif. Comme test_delete_queueing.py, les méthodes réelles de MainWindow
sont appelées en non lié contre un objet minimal ne portant que les attributs
lus par le chemin testé."""
from src.ui.main_window import MainWindow


class _FakeThread:
    def __init__(self, running: bool):
        self._running = running

    def isRunning(self) -> bool:
        return self._running

    def deleteLater(self):
        pass


class _FakeFaceIndexer(_FakeThread):
    """Trace les connexions/démarrage effectués par _start_face_indexing."""

    def __init__(self):
        super().__init__(running=False)
        self.started = False

    def progress(self):
        pass

    def connect(self, *a, **k):
        pass

    def deleteLater(self):
        pass

    def start(self):
        self.started = True
        self._running = True


class _FakeSignal:
    def connect(self, *a, **k):
        pass


class _FakeFaceIndexThread(_FakeThread):
    progress = _FakeSignal()
    cluster_requested = _FakeSignal()
    finished = _FakeSignal()
    unavailable = _FakeSignal()
    error = _FakeSignal()

    def __init__(self):
        super().__init__(running=False)

    def start(self):
        self._running = True


class _FakeLabel:
    def setText(self, *a, **k):
        pass


class _FakeMainWindow:
    """Porte uniquement les attributs lus/écrits par _start_face_indexing et
    _on_face_indexing_finished."""

    def __init__(self, face_indexer=None):
        self._face_indexer = face_indexer
        self._face_index_pending = False
        self._lbl_action = _FakeLabel()
        self._face_db = object()
        self._catalog = object()
        self.clustering_calls = 0
        self.similarity_calls = 0
        self.new_indexers: list = []

    def _run_clustering(self):
        self.clustering_calls += 1

    def _schedule_similarity_search(self):
        self.similarity_calls += 1

    def _on_face_progress(self, *a, **k):
        pass

    def _on_face_unavailable(self, *a, **k):
        pass

    def _on_face_index_error(self, *a, **k):
        pass

    def _start_face_indexing(self):
        MainWindow._start_face_indexing(self)

    def _on_face_indexing_finished(self, *a, **k):
        pass


def _patch_face_index_thread(monkeypatch, factory):
    monkeypatch.setattr("src.ui.main_window_faces.FaceIndexThread", factory)


class TestFaceIndexingRequeue:
    def test_starts_immediately_when_idle(self, monkeypatch):
        created: list = []

        def factory(face_db, catalog, parent):
            t = _FakeFaceIndexThread()
            created.append(t)
            return t

        _patch_face_index_thread(monkeypatch, factory)
        fake = _FakeMainWindow(face_indexer=None)

        MainWindow._start_face_indexing(fake)

        assert len(created) == 1
        assert created[0]._running is True
        assert fake._face_index_pending is False

    def test_marks_pending_instead_of_dropping_when_already_running(self, monkeypatch):
        def factory(face_db, catalog, parent):
            raise AssertionError("ne doit pas créer de nouveau thread pendant qu'un tourne déjà")

        _patch_face_index_thread(monkeypatch, factory)
        fake = _FakeMainWindow(face_indexer=_FakeThread(running=True))

        MainWindow._start_face_indexing(fake)

        # Avant le correctif : retour silencieux, _face_index_pending jamais
        # posé -> la nouvelle photo ne serait plus jamais indexée.
        assert fake._face_index_pending is True

    def test_finished_requeues_when_pending(self, monkeypatch):
        created: list = []

        def factory(face_db, catalog, parent):
            t = _FakeFaceIndexThread()
            created.append(t)
            return t

        _patch_face_index_thread(monkeypatch, factory)
        fake = _FakeMainWindow(face_indexer=_FakeThread(running=False))
        fake._face_index_pending = True

        MainWindow._on_face_indexing_finished(fake, indexed=14, faces=0)

        assert fake._face_index_pending is False
        assert len(created) == 1, "la demande mise en attente doit relancer un FaceIndexThread"

    def test_finished_noop_when_not_pending(self, monkeypatch):
        def factory(face_db, catalog, parent):
            raise AssertionError("aucune demande en attente : ne doit rien démarrer")

        _patch_face_index_thread(monkeypatch, factory)
        fake = _FakeMainWindow(face_indexer=None)

        MainWindow._on_face_indexing_finished(fake, indexed=14, faces=0)

        assert fake._face_index_pending is False
