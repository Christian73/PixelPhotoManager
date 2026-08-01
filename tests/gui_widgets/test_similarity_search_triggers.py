# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression : ce qui relance la recherche de visages similaires
(main_window_faces.py::FacesController).

Signalement utilisateur — « plus aucun visage en attente de confirmation » :
_start_similarity_search() n'avait qu'un seul appelant, _on_clustering_finished()
sous la garde `n_clusters > 0`, elle-même atteignable seulement depuis
_on_face_indexing_finished quand `faces > 0`. Sur une bibliothèque entièrement
indexée, plus rien ne réalimentait donc la file de vérification, alors que
nommer une personne déplace son centroïde et rend proposables des groupes
jusque-là sous le seuil.

Comme test_face_indexing_requeue.py, les méthodes réelles de MainWindow sont
appelées en non lié contre un objet minimal ne portant que ce que lit le chemin
testé — pas de QMainWindow, pas de thread réel."""
import pytest

from src.ui.main_window import MainWindow


class _FakeThread:
    def __init__(self, running: bool = False):
        self._running = running

    def isRunning(self) -> bool:
        return self._running


class _FakeTimer:
    """QTimer à un coup : compte les (re)démarrages, sans event loop."""

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

    # --- collaborateurs remplacés -------------------------------------
    def _run_clustering(self):
        self.clustering_calls += 1

    def _start_similarity_search(self):
        self.similarity_starts += 1

    def _start_face_indexing(self):
        pass

    # --- dépendances de _on_clustering_finished ------------------------
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
        """Le timer est à un coup et relancé à chaque appel : nommer dix
        groupes d'affilée ne doit pas lancer dix recherches (chacune parcourt
        toute la bibliothèque)."""
        for _ in range(10):
            MainWindow._schedule_similarity_search(ctrl)

        assert ctrl.similarity_starts == 0, "rien ne part avant l'échéance du timer"
        assert ctrl._similarity_debounce.starts == 10  # QTimer.start() redémarre le délai


class TestIndexingFinished:
    def test_nothing_new_still_looks_for_similar_faces(self, ctrl):
        """Cœur du bug : sans nouveau visage, le regroupement n'est pas lancé —
        c'était jusqu'ici la fin du chemin, donc plus jamais de suggestion."""
        MainWindow._on_face_indexing_finished(ctrl, indexed=0, faces=0)

        assert ctrl.clustering_calls == 0
        assert ctrl._similarity_debounce.starts == 1

    def test_new_faces_delegate_to_clustering(self, ctrl):
        """Avec de nouveaux visages, c'est _on_clustering_finished qui enchaîne :
        pas de double déclenchement."""
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
        """`n_clusters == 0` ne veut pas dire « rien à comparer » : le
        regroupement rend la main sans rien faire dès que le nombre de visages
        non identifiés n'a pas bougé, alors que les groupes déjà formés restent
        à confronter aux personnes nommées entre-temps."""
        self._prepare(ctrl)

        MainWindow._on_clustering_finished(ctrl, n_clusters)

        assert ctrl.similarity_starts == 1


class TestManualEntry:
    """Visages › Rechercher des visages similaires… — même traitement, mais
    l'appelant est l'utilisateur : il doit avoir un retour."""

    def test_runs_immediately_and_cancels_the_pending_debounce(self, ctrl):
        ctrl._similarity_thread = _FakeThread(running=False)
        MainWindow._schedule_similarity_search(ctrl)

        MainWindow._start_similarity_search_manually(ctrl)

        assert ctrl._similarity_debounce.stops == 1, "sinon un second passage suit 30 s après"
        assert ctrl.similarity_starts == 1

    def test_no_thread_yet_still_runs(self, ctrl):
        """Aucune recherche n'a encore tourné : _similarity_thread n'existe pas
        (attribut créé par _start_similarity_search)."""
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

        assert shown == ["Recherche en cours"]
        assert ctrl.similarity_starts == 0
