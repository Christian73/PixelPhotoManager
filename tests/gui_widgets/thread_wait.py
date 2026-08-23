# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Waiting for a QThread already started by the application code (a test helper)."""


def wait_thread_done(qtbot, thread, timeout: int = 3000) -> None:
    """Waits for a QThread **already started** to finish, then for its signals to
    be delivered.

    To be used instead of ``qtbot.waitSignal(thread.a_signal)`` whenever the
    thread is started by the application code before the test can hook onto it
    (``FacePanel.set_photo()`` starts its ``_FacesDataLoader`` then returns): if
    the thread emits during the few tens of microseconds between the ``start()``
    and the connection of the blocker, the emission is lost and ``waitSignal``
    **times out** (``TimeoutError``) instead of returning immediately. Window
    measured on ``_FacesDataLoader``: ~1.5 ms -- never reached on an idle
    machine, but reached in practice in a complete suite (a flake observed under
    ``--cov``, where the application thread is not the only one in flight).

    Polling ``isRunning()`` is safe in the other direction: the signal is emitted
    (hence the event of the queued connection is posted) **before** the end of
    ``run()``, so before ``isRunning()`` goes back to ``False``; one turn of the
    event loop is then enough to deliver the application slot.
    """
    if thread is None:
        return

    def _done() -> bool:
        try:
            return not thread.isRunning()
        except RuntimeError:  # C++ object already destroyed (deleteLater)
            return True

    qtbot.waitUntil(_done, timeout=timeout)
    qtbot.wait(1)  # delivery of the remaining queued connections
