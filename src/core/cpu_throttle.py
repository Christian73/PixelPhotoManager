# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Limiting the CPU load of the permanent background tasks (duplicate
detection, face recognition).

Three complementary levers — the first two reduce the *number* of threads
that work, the third reduces the *throughput* of each one:

1. `throttled_worker_count()` — number of Python workers/subprocesses at ~15%
   of the available cores.
2. `lower_current_thread_priority()` / `lower_current_process_priority()` —
   minimum OS priority (IDLE), so that the foreground stays responsive.
   `limit_cv2_threads()` completes the two: without it, a single one of our
   "throttled" workers can saturate every core, since OpenCV parallelises its
   own calls internally over a thread pool we never created (hence neither
   counted nor lowered in priority).
3. `throttle_tick()` — duty cycle: each background thread sleeps periodically
   so as to consume only a configurable fraction of the CPU time. It is the
   only lever that really caps the consumption: a thread at IDLE priority
   still occupies 100% of an otherwise unused core (fan, battery) — priority
   only yields the way.
"""
import os
import threading
import time

THROTTLE_FRACTION = 0.15  # ~15% of the available cores

# Levels exposed in the settings: the fraction of time actually spent working
# (the rest is sleep). 1.0 = no throttling at all.
BACKGROUND_CPU_LEVELS: dict[str, float] = {
    "low": 0.25,
    "medium": 0.60,
    "max": 1.00,
}
# "low" by default: the background analyses (duplicates, faces) are permanent
# and have no deadline, whereas the responsiveness of the interface is noticed
# immediately. Whoever wants to go faster can raise the level in the settings —
# the opposite (discovering that a throttle needs to be applied) assumes one
# understands where the sluggishness comes from.
DEFAULT_BACKGROUND_CPU = "low"

# How long the user must stay idle before the throttle is lifted: if nobody is
# using the application, the background work may as well finish quickly.
IDLE_GRACE_SECONDS = 45.0

# Amount of accumulated work before a pause is considered. Too short and the
# cost of the time.sleep() calls dominates; too long and the pause becomes
# noticeable on how fast a cancellation reacts (cf. closeEvent).
_WORK_SLICE = 0.10
# The sleep is split up so as to stay interruptible (cancellation/shutdown).
_SLEEP_STEP = 0.05

_ratio_lock = threading.Lock()
_ratio: float | None = None  # None = not read from the configuration yet
_last_activity = time.monotonic()
_local = threading.local()


def throttled_worker_count(minimum: int = 1) -> int:
    """Number of workers/subprocesses matching ~15% of the available cores,
    never fewer than `minimum`."""
    return max(minimum, round((os.cpu_count() or 4) * THROTTLE_FRACTION))


def lower_current_thread_priority() -> None:
    """Windows: lowers the priority of the current OS thread to the minimum
    (THREAD_PRIORITY_IDLE) so as to let the foreground respond — the thread
    then runs almost only when the CPU is otherwise idle. To be used inside
    QThread.run() (secondary threads) and as the `initializer` of a
    ThreadPoolExecutor (each worker thread lowers itself when it starts).

    To be preferred over `QThread.setPriority(QThread.LowestPriority)`, which
    only goes down to THREAD_PRIORITY_LOWEST (-2) where IDLE (-15) places the
    thread below just about everything else on the system. Silent no-op if
    unavailable."""
    try:
        import ctypes
        THREAD_PRIORITY_IDLE = -15
        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, THREAD_PRIORITY_IDLE)
    except Exception:
        pass


def lower_current_process_priority() -> None:
    """Lowers the priority of the current process to the minimum through psutil
    (IDLE_PRIORITY_CLASS). To be used as the `initializer` of a
    ProcessPoolExecutor: each worker is a dedicated process with no UI, so
    lowering the whole process (unlike the main process, where it would
    penalise the UI too) is safe. Silent no-op if unavailable."""
    try:
        import psutil
        psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS)
    except Exception:
        pass


def limit_cv2_threads(n: int = 1) -> None:
    """Limits OpenCV's internal thread pool (`cv2.parallel_for_`) to `n`.

    Without this, `cv2.setNumThreads()` defaults to the number of cores (16 on
    a typical machine): each of our "throttled" workers can then, on a single
    call (`imdecode`, `resize`, `warpPerspective`, `knnMatch`,
    `detectAndCompute`…), spawn as many native threads, at NORMAL priority
    since we do not create them ourselves — the ceiling of
    `throttled_worker_count()` then caps nothing at all.

    Careful: the setting is **global to the process**, not to the calling
    thread. It therefore also applies to the interactive uses of OpenCV (video
    thumbnails, rotations on export, file repair); those are short one-shot
    operations where the internal parallelism brings next to nothing, unlike
    the background loops, which run continuously.
    Silent no-op if OpenCV is absent."""
    try:
        import cv2
        cv2.setNumThreads(max(1, int(n)))
    except Exception:
        pass


def init_background_process() -> None:
    """`initializer` to pass to every background ProcessPoolExecutor: lowers the
    priority of the worker process **and** caps OpenCV's internal pool.

    Must stay a MODULE-LEVEL function (not a lambda, not a method) so as to be
    picklable by multiprocessing on Windows (spawn) — the same constraint as
    `warmup_worker` in faces/detector.py.

    Replaces the direct use of `lower_current_process_priority` as the
    initializer: priority alone is not enough — a detection worker decoding and
    resizing a 24 Mpx photo spawns as many OpenCV threads as there are cores,
    at normal priority since they are born outside our control."""
    lower_current_process_priority()
    limit_cv2_threads(1)


# ── Cycle de service (duty cycle) ──────────────────────────────────────────────

def set_background_cpu_level(level: str) -> None:
    """Sets the throttling level of the background tasks (a key of
    BACKGROUND_CPU_LEVELS). Called at startup and then on every change in the
    settings — the value is kept in memory so that `throttle_tick()` stays
    cheap (it is called thousands of times per second)."""
    global _ratio
    with _ratio_lock:
        _ratio = BACKGROUND_CPU_LEVELS.get(level, BACKGROUND_CPU_LEVELS[DEFAULT_BACKGROUND_CPU])


def background_cpu_ratio() -> float:
    """Fraction of the CPU time allocated to the background tasks, read once
    from the configuration if `set_background_cpu_level()` has not been called
    already (the case of the tests and of scripts running outside the
    application)."""
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
    """Reports a user interaction (click, key, wheel). Called by the event
    filter of MainWindow; beyond IDLE_GRACE_SECONDS with no interaction, the
    throttle is lifted (nobody is watching, we may as well make progress)."""
    global _last_activity
    _last_activity = time.monotonic()


def user_is_idle() -> bool:
    return time.monotonic() - _last_activity >= IDLE_GRACE_SECONDS


def effective_cpu_ratio() -> float:
    """Effective fraction, once the user's activity state is taken into
    account."""
    if user_is_idle():
        return 1.0
    return background_cpu_ratio()


class DutyCycle:
    """Throughput regulator for a single thread: accumulates the working time
    elapsed between two `tick()` calls and sleeps to hold the requested ratio.

    One regulator per thread (cf. `throttle_tick()`) rather than a shared
    global counter: no lock on a very hot path, and the ratio composes
    naturally (N threads throttled to r consume ~N·r cores)."""

    def __init__(self, slice_s: float = _WORK_SLICE) -> None:
        self._slice = slice_s
        self._work_start = time.monotonic()

    def reset(self) -> None:
        self._work_start = time.monotonic()

    def tick(self, cancelled=None) -> None:
        """To be called on every unit of work (a photo, one row of the O(N²)
        loop, a batch of pairs). `cancelled`: an optional callable polled
        during the sleep so as to hand back control immediately."""
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
    """Duty cycle for the current thread (the regulator is created on the fly
    and kept in thread-local storage). To be called in every sustained
    background loop, including from the workers of a ThreadPoolExecutor —
    that is where the work happens, so that is where the pause must be taken."""
    duty = getattr(_local, "duty", None)
    if duty is None:
        duty = _local.duty = DutyCycle()
    duty.tick(cancelled)
