# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Regression of the production bug found in 2026-07 (see
bugfix_signal_dict_int_keys_2026-07.md): PySide6 maps `Signal(dict)` onto
`QVariantMap`, which requires `str` keys on the C++ side. With `int` keys
(exactly the form used by `DuplicateDetectorThread.finished`,
`{group_id: [path, ...]}`), the cross-thread conversion fails silently
(no Python exception, just a Shiboken log on stderr) and the slot receives an
empty dict -- duplicate detection then always reported "no
duplicate" despite groups being properly found.

Calling `_detect()` synchronously (cf. test_duplicate_detector.py) CANNOT
detect this bug: it never goes through the Qt marshalling of a real
cross-thread connection. Only a real `QThread.start()` + `Signal(object)`
reveals it -- that is the whole point of this test."""
from PySide6.QtCore import QThread, Signal


class _Worker(QThread):
    done_object = Signal(object)

    def run(self) -> None:
        # Int keys: the exact form that silently broke Signal(dict).
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
    """The same form as DuplicateDetectorThread.partial_results:
    Signal(object, object), int keys on the dict side -- the 2nd emission of the
    same form as `finished`, liable to the same bug."""
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
