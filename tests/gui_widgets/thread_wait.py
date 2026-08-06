# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Attente d'un QThread déjà lancé par le code applicatif (helper de tests)."""


def wait_thread_done(qtbot, thread, timeout: int = 3000) -> None:
    """Attend la fin d'un QThread **déjà démarré**, puis la livraison de ses signaux.

    À utiliser à la place de ``qtbot.waitSignal(thread.un_signal)`` chaque fois
    que le thread est lancé par le code applicatif avant que le test ne puisse
    s'y brancher (``FacePanel.set_photo()`` démarre son ``_FacesDataLoader``
    puis rend la main) : si le thread émet pendant les quelques dizaines de
    microsecondes qui séparent le ``start()`` du branchement du blocker,
    l'émission est perdue et ``waitSignal`` **expire** (``TimeoutError``) au
    lieu de revenir immédiatement. Fenêtre mesurée sur ``_FacesDataLoader`` :
    ~1,5 ms — jamais atteinte sur une machine au repos, mais atteinte en
    pratique dans une suite complète (flake observé sous ``--cov``, où le
    thread applicatif n'est pas le seul en vol).

    Le sondage de ``isRunning()`` est sûr dans l'autre sens : le signal est émis
    (donc l'événement de la connexion queued est posté) **avant** la fin de
    ``run()``, donc avant que ``isRunning()`` ne repasse à ``False`` ; un tour
    d'event loop suffit ensuite à livrer le slot applicatif.
    """
    if thread is None:
        return

    def _done() -> bool:
        try:
            return not thread.isRunning()
        except RuntimeError:  # objet C++ déjà détruit (deleteLater)
            return True

    qtbot.waitUntil(_done, timeout=timeout)
    qtbot.wait(1)  # livraison des connexions queued restantes
