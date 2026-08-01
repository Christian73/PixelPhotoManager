# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Limitation de la charge CPU des traitements de fond permanents (détection
de doublons, reconnaissance faciale).

Trois leviers complémentaires — les deux premiers réduisent le *nombre* de
threads qui travaillent, le troisième réduit le *débit* de chacun :

1. `throttled_worker_count()` — nombre de workers/subprocesses Python à ~15 %
   des cœurs disponibles.
2. `lower_current_thread_priority()` / `lower_current_process_priority()` —
   priorité OS minimale (IDLE), pour que le premier plan reste réactif.
   `limit_cv2_threads()` complète les deux : sans lui, un seul de nos workers
   « throttlés » peut saturer tous les cœurs, OpenCV parallélisant en interne
   ses propres appels sur un pool de threads que nous n'avons jamais créés
   (donc ni comptés, ni abaissés en priorité).
3. `throttle_tick()` — cycle de service (duty cycle) : chaque thread de fond
   s'endort périodiquement pour ne consommer qu'une fraction paramétrable du
   temps CPU. C'est le seul levier qui plafonne réellement la consommation :
   un thread en priorité IDLE occupe malgré tout 100 % d'un cœur inoccupé
   (ventilateur, batterie), la priorité ne fait que céder le passage.
"""
import os
import threading
import time

THROTTLE_FRACTION = 0.15  # ~15 % des cœurs disponibles

# Niveaux exposés dans les paramètres : fraction du temps réellement passée à
# travailler (le reste est du sommeil). 1.0 = aucun bridage.
BACKGROUND_CPU_LEVELS: dict[str, float] = {
    "low": 0.25,
    "medium": 0.60,
    "max": 1.00,
}
DEFAULT_BACKGROUND_CPU = "medium"

# Délai d'inactivité de l'utilisateur au-delà duquel le bridage est levé : si
# personne ne se sert de l'application, autant finir le travail de fond vite.
IDLE_GRACE_SECONDS = 45.0

# Durée de travail accumulée avant d'envisager une pause. Trop court, le coût
# des appels time.sleep() domine ; trop long, la pause devient perceptible sur
# la réactivité de l'annulation (cf. closeEvent).
_WORK_SLICE = 0.10
# Le sommeil est fractionné pour rester interruptible (annulation/fermeture).
_SLEEP_STEP = 0.05

_ratio_lock = threading.Lock()
_ratio: float | None = None  # None = pas encore lu depuis la configuration
_last_activity = time.monotonic()
_local = threading.local()


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

    À préférer à `QThread.setPriority(QThread.LowestPriority)`, qui ne descend
    qu'à THREAD_PRIORITY_LOWEST (-2) là où IDLE (-15) place le thread sous
    quasiment tout le reste du système. No-op silencieux si indisponible."""
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


def limit_cv2_threads(n: int = 1) -> None:
    """Limite le pool de threads interne d'OpenCV (`cv2.parallel_for_`) à `n`.

    Sans ça, `cv2.setNumThreads()` vaut par défaut le nombre de cœurs (16 sur
    une machine typique) : chacun de nos workers « throttlés » peut alors, sur
    un seul appel (`imdecode`, `resize`, `warpPerspective`, `knnMatch`,
    `detectAndCompute`…), déclencher autant de threads natifs, à priorité
    NORMALE puisque nous ne les créons pas nous-mêmes — le plafond de
    `throttled_worker_count()` ne plafonne alors plus rien du tout.

    Attention : le réglage est **global au process**, pas au thread appelant.
    Il s'applique donc aussi aux usages interactifs d'OpenCV (vignettes vidéo,
    rotations à l'export, réparation de fichiers) ; ceux-ci sont des opérations
    unitaires courtes où la parallélisation interne n'apporte quasiment rien,
    contrairement aux boucles de fond qui, elles, tournent en continu.
    No-op silencieux si OpenCV est absent."""
    try:
        import cv2
        cv2.setNumThreads(max(1, int(n)))
    except Exception:
        pass


def init_background_process() -> None:
    """`initializer` à passer à tout ProcessPoolExecutor de fond : abaisse la
    priorité du process worker **et** plafonne le pool interne d'OpenCV.

    Doit rester une fonction MODULE-LEVEL (non-lambda, non-méthode) pour être
    picklable par multiprocessing sur Windows (spawn) — même contrainte que
    `warmup_worker` dans faces/detector.py.

    Remplace l'usage direct de `lower_current_process_priority` comme
    initializer : la priorité seule ne suffit pas, un worker de détection qui
    décode et redimensionne une photo de 24 Mpx déclenche autant de threads
    OpenCV que de cœurs, à priorité normale puisqu'ils naissent hors de notre
    contrôle."""
    lower_current_process_priority()
    limit_cv2_threads(1)


# ── Cycle de service (duty cycle) ──────────────────────────────────────────────

def set_background_cpu_level(level: str) -> None:
    """Fixe le niveau de bridage des traitements de fond (clé de
    BACKGROUND_CPU_LEVELS). Appelé au démarrage puis à chaque changement dans
    les paramètres — la valeur est gardée en mémoire pour que `throttle_tick()`
    reste bon marché (appelé des milliers de fois par seconde)."""
    global _ratio
    with _ratio_lock:
        _ratio = BACKGROUND_CPU_LEVELS.get(level, BACKGROUND_CPU_LEVELS[DEFAULT_BACKGROUND_CPU])


def background_cpu_ratio() -> float:
    """Fraction du temps CPU allouée aux traitements de fond, lue une seule
    fois depuis la configuration si `set_background_cpu_level()` n'a pas déjà
    été appelé (cas des tests et des scripts hors application)."""
    with _ratio_lock:
        if _ratio is not None:
            return _ratio
    try:
        from src.core.config import Config
        level = Config().get("performance.background_cpu", DEFAULT_BACKGROUND_CPU)
    except Exception:
        level = DEFAULT_BACKGROUND_CPU
    set_background_cpu_level(level)
    with _ratio_lock:
        return _ratio


def note_user_activity() -> None:
    """Signale une interaction utilisateur (clic, touche, molette). Appelé par
    le filtre d'événements de MainWindow ; au-delà de IDLE_GRACE_SECONDS sans
    interaction, le bridage est levé (personne ne regarde, autant avancer)."""
    global _last_activity
    _last_activity = time.monotonic()


def user_is_idle() -> bool:
    return time.monotonic() - _last_activity >= IDLE_GRACE_SECONDS


def effective_cpu_ratio() -> float:
    """Fraction effective, une fois pris en compte l'état d'activité de
    l'utilisateur."""
    if user_is_idle():
        return 1.0
    return background_cpu_ratio()


class DutyCycle:
    """Régulateur de débit d'un seul thread : accumule le temps de travail
    écoulé entre deux `tick()` et s'endort pour tenir le ratio demandé.

    Un régulateur par thread (cf. `throttle_tick()`) plutôt qu'un compteur
    global partagé : pas de verrou sur un chemin très chaud, et le ratio se
    compose naturellement (N threads bridés à r consomment ~N·r cœurs)."""

    def __init__(self, slice_s: float = _WORK_SLICE) -> None:
        self._slice = slice_s
        self._work_start = time.monotonic()

    def reset(self) -> None:
        self._work_start = time.monotonic()

    def tick(self, cancelled=None) -> None:
        """À appeler à chaque unité de travail (une photo, une ligne de la
        boucle O(N²), un lot de paires). `cancelled` : callable optionnel
        interrogé pendant le sommeil pour rendre la main immédiatement."""
        ratio = effective_cpu_ratio()
        now = time.monotonic()
        if ratio >= 1.0:
            self._work_start = now
            return
        worked = now - self._work_start
        if worked < self._slice:
            return
        remaining = worked * (1.0 - ratio) / ratio
        while remaining > 0:
            if cancelled is not None and cancelled():
                break
            time.sleep(min(_SLEEP_STEP, remaining))
            remaining -= _SLEEP_STEP
        self._work_start = time.monotonic()


def throttle_tick(cancelled=None) -> None:
    """Cycle de service pour le thread courant (régulateur créé à la volée et
    conservé en thread-local). À appeler dans toute boucle de fond soutenue,
    y compris depuis les workers d'un ThreadPoolExecutor — c'est là que le
    travail a lieu, donc c'est là que la pause doit être prise."""
    duty = getattr(_local, "duty", None)
    if duty is None:
        duty = _local.duty = DutyCycle()
    duty.tick(cancelled)
