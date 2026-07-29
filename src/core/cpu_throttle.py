# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Limitation de la charge CPU des traitements de fond permanents (détection
de doublons, reconnaissance faciale) : réduction du nombre de workers/
subprocesses à ~15 % des cœurs disponibles, et abaissement de la priorité OS
des threads/process qui font le travail pour laisser le premier plan
répondre."""
import os

THROTTLE_FRACTION = 0.15  # ~15 % des cœurs disponibles


def throttled_worker_count(minimum: int = 1) -> int:
    """Nombre de workers/subprocesses correspondant à ~15 % des cœurs
    disponibles, jamais moins que `minimum`."""
    return max(minimum, round((os.cpu_count() or 4) * THROTTLE_FRACTION))


def lower_current_thread_priority() -> None:
    """Windows : abaisse la priorité du thread OS courant au minimum
    (THREAD_PRIORITY_IDLE) pour laisser le premier plan répondre — le thread
    ne s'exécute quasiment que quand le CPU est autrement inactif. À utiliser
    dans QThread.run() (threads secondaires) et comme `initializer` de
    ThreadPoolExecutor (chaque worker thread s'auto-abaisse à son démarrage).
    No-op silencieux si indisponible."""
    try:
        import ctypes
        THREAD_PRIORITY_IDLE = -15
        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, THREAD_PRIORITY_IDLE)
    except Exception:
        pass


def lower_current_process_priority() -> None:
    """Abaisse la priorité du process courant au minimum via psutil
    (IDLE_PRIORITY_CLASS). À utiliser comme `initializer` de
    ProcessPoolExecutor : chaque worker est un process dédié sans UI, donc
    abaisser tout le process (contrairement au process principal, où ça
    pénaliserait aussi l'UI) est sûr. No-op silencieux si indisponible."""
    try:
        import psutil
        psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS)
    except Exception:
        pass
