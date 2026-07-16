# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression du bug de production découvert en 2026-07 (voir
bugfix_signal_dict_int_keys_2026-07.md) : PySide6 mappe `Signal(dict)` sur
`QVariantMap`, qui exige des clés `str` côté C++. Avec des clés `int`
(exactement la forme utilisée par `DuplicateDetectorThread.finished`,
`{group_id: [path, ...]}`), la conversion cross-thread échoue silencieusement
(pas d'exception Python, juste un log Shiboken en stderr) et le slot reçoit un
dict vide — la détection de doublons rapportait alors toujours "aucun
doublon" malgré des groupes bien trouvés.

Appeler `_detect()` en synchrone (cf. test_duplicate_detector.py) ne peut PAS
détecter ce bug : il ne passe jamais par le marshalling Qt d'une connexion
cross-thread réelle. Seul un vrai `QThread.start()` + `Signal(object)` le
révèle — c'est tout l'objet de ce test."""
from PySide6.QtCore import QThread, Signal


class _Worker(QThread):
    done_object = Signal(object)

    def run(self) -> None:
        # Clés int : forme exacte qui cassait silencieusement Signal(dict).
        self.done_object.emit({1: ["a"], 2: ["b"]})


def test_signal_object_int_keyed_dict_survives_thread_boundary(qtbot):
    worker = _Worker()
    received: dict = {}
    worker.done_object.connect(received.update)

    with qtbot.waitSignal(worker.done_object, timeout=2000):
        worker.start()

    worker.wait()
    assert received == {1: ["a"], 2: ["b"]}


class _PartialResultsWorker(QThread):
    """Même forme que DuplicateDetectorThread.partial_results :
    Signal(object, object), clés int côté dict — la 2e émission de la même
    forme que `finished`, susceptible du même bug."""
    partial = Signal(object, object)

    def run(self) -> None:
        self.partial.emit({1: ["a"], 2: ["b"]}, ["corrupted.jpg"])


def test_signal_object_int_keyed_dict_survives_thread_boundary_partial_results(qtbot):
    worker = _PartialResultsWorker()
    received: dict = {}
    corrupted_received: list = []

    def _capture(groups, corrupted):
        received.update(groups)
        corrupted_received.extend(corrupted)

    worker.partial.connect(_capture)

    with qtbot.waitSignal(worker.partial, timeout=2000):
        worker.start()

    worker.wait()
    assert received == {1: ["a"], 2: ["b"]}
    assert corrupted_received == ["corrupted.jpg"]
