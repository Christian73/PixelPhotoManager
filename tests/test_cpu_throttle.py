# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/core/cpu_throttle.py` — en particulier le cycle de service, seul
levier qui plafonne réellement la charge des traitements de fond (la priorité OS
ne fait que céder le passage : un thread IDLE occupe malgré tout 100 % d'un cœur
autrement inoccupé).

Le temps est entièrement simulé : `cpu_throttle.time` est remplacé par une
horloge factice dont `sleep()` avance le compteur au lieu d'attendre, ce qui rend
les durées de pause vérifiables exactement et sans ralentir la suite. Les
globales du module (`_ratio`, `_last_activity`, régulateur thread-local) sont
restaurées par la fixture — elles survivraient sinon d'un test à l'autre."""
import pytest

from src.core import cpu_throttle


class _FakeClock:
    """Horloge monotone contrôlée : `sleep()` n'attend rien, il avance le temps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.slept.append(duration)
        self.now += duration

    def advance(self, duration: float) -> None:
        self.now += duration

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


@pytest.fixture
def clock(monkeypatch):
    """Horloge factice + globales du module remises à zéro après le test."""
    fake = _FakeClock()
    monkeypatch.setattr(cpu_throttle, "time", fake)
    saved_ratio = cpu_throttle._ratio
    saved_activity = cpu_throttle._last_activity
    saved_local = cpu_throttle._local
    cpu_throttle._local = cpu_throttle.threading.local()
    # Après l'installation de l'horloge factice, pour que `_last_activity` soit
    # exprimé dans la même base de temps (sinon user_is_idle() est aléatoire).
    cpu_throttle.note_user_activity()
    yield fake
    cpu_throttle._ratio = saved_ratio
    cpu_throttle._last_activity = saved_activity
    cpu_throttle._local = saved_local


class TestBackgroundCpuLevel:
    @pytest.mark.parametrize("level, expected", [
        ("low", 0.25),
        ("medium", 0.60),
        ("max", 1.00),
    ])
    def test_known_levels(self, clock, level, expected):
        cpu_throttle.set_background_cpu_level(level)
        assert cpu_throttle.background_cpu_ratio() == pytest.approx(expected)

    def test_unknown_level_falls_back_to_default(self, clock):
        cpu_throttle.set_background_cpu_level("turbo")
        assert cpu_throttle.background_cpu_ratio() == pytest.approx(
            cpu_throttle.BACKGROUND_CPU_LEVELS[cpu_throttle.DEFAULT_BACKGROUND_CPU]
        )

    def test_read_from_config_when_never_set(self, clock, monkeypatch):
        """Cas des tests et scripts hors application : le ratio est lu une seule
        fois depuis la configuration, puis mémorisé."""
        cpu_throttle._ratio = None
        reads: list = []

        class _FakeConfig:
            def get(self, key, default=None):
                reads.append(key)
                return "low"

        monkeypatch.setitem(
            __import__("sys").modules, "src.core.config",
            type("_M", (), {"Config": _FakeConfig})(),
        )

        assert cpu_throttle.background_cpu_ratio() == pytest.approx(0.25)
        assert reads == ["performance.background_cpu"]
        # Deuxième appel : servi par le cache mémoire, pas de relecture.
        assert cpu_throttle.background_cpu_ratio() == pytest.approx(0.25)
        assert reads == ["performance.background_cpu"]

    def test_config_unreadable_falls_back_to_default(self, clock, monkeypatch):
        cpu_throttle._ratio = None

        class _BoomConfig:
            def __init__(self):
                raise OSError("config illisible")

        monkeypatch.setitem(
            __import__("sys").modules, "src.core.config",
            type("_M", (), {"Config": _BoomConfig})(),
        )

        assert cpu_throttle.background_cpu_ratio() == pytest.approx(
            cpu_throttle.BACKGROUND_CPU_LEVELS[cpu_throttle.DEFAULT_BACKGROUND_CPU]
        )


class TestUserActivity:
    def test_fresh_activity_is_not_idle(self, clock):
        assert cpu_throttle.user_is_idle() is False

    def test_idle_after_grace_period(self, clock):
        clock.advance(cpu_throttle.IDLE_GRACE_SECONDS + 1)
        assert cpu_throttle.user_is_idle() is True

    def test_activity_resets_the_countdown(self, clock):
        clock.advance(cpu_throttle.IDLE_GRACE_SECONDS + 1)
        cpu_throttle.note_user_activity()
        assert cpu_throttle.user_is_idle() is False

    def test_idle_lifts_the_throttle(self, clock):
        """Personne ne se sert de l'application : autant finir le travail vite."""
        cpu_throttle.set_background_cpu_level("low")
        assert cpu_throttle.effective_cpu_ratio() == pytest.approx(0.25)

        clock.advance(cpu_throttle.IDLE_GRACE_SECONDS + 1)

        assert cpu_throttle.effective_cpu_ratio() == pytest.approx(1.0)


class TestDutyCycle:
    def test_no_sleep_below_the_work_slice(self, clock):
        cpu_throttle.set_background_cpu_level("low")
        duty = cpu_throttle.DutyCycle()

        clock.advance(cpu_throttle._WORK_SLICE / 2)
        duty.tick()

        assert clock.slept == []

    def test_sleeps_to_honour_the_ratio(self, clock):
        """r = 0.25 → pour 0,2 s travaillées, 0,6 s de pause (0,2 = 25 % de 0,8)."""
        cpu_throttle.set_background_cpu_level("low")
        duty = cpu_throttle.DutyCycle()

        clock.advance(0.2)
        duty.tick()

        assert clock.total_slept == pytest.approx(0.6, abs=1e-6)
        assert all(s <= cpu_throttle._SLEEP_STEP for s in clock.slept)

    def test_medium_sleeps_less_than_low(self, clock):
        cpu_throttle.set_background_cpu_level("medium")
        duty = cpu_throttle.DutyCycle()

        clock.advance(0.3)
        duty.tick()

        # r = 0.6 → 0.3 × 0.4 / 0.6 = 0.2 s
        assert clock.total_slept == pytest.approx(0.2, abs=1e-6)

    def test_max_level_never_sleeps(self, clock):
        cpu_throttle.set_background_cpu_level("max")
        duty = cpu_throttle.DutyCycle()

        clock.advance(10.0)
        duty.tick()

        assert clock.slept == []

    def test_cancellation_interrupts_the_sleep(self, clock):
        """Le sommeil est fractionné précisément pour rester interruptible :
        une fermeture d'application ne doit pas attendre la fin de la pause."""
        cpu_throttle.set_background_cpu_level("low")
        duty = cpu_throttle.DutyCycle()
        cancelled = False

        def _is_cancelled() -> bool:
            return cancelled

        clock.advance(0.2)
        cancelled = True
        duty.tick(_is_cancelled)

        assert clock.slept == []

    def test_cancellation_mid_sleep_stops_early(self, clock):
        cpu_throttle.set_background_cpu_level("low")
        duty = cpu_throttle.DutyCycle()
        calls = {"n": 0}

        def _is_cancelled() -> bool:
            calls["n"] += 1
            return calls["n"] > 3      # laisse passer 3 pas de sommeil

        clock.advance(0.2)
        duty.tick(_is_cancelled)

        assert len(clock.slept) == 3
        assert clock.total_slept == pytest.approx(3 * cpu_throttle._SLEEP_STEP)

    def test_work_start_resets_after_a_pause(self, clock):
        """Sans remise à zéro, le temps déjà « payé » serait recompté au tick
        suivant et la pause enflerait indéfiniment."""
        cpu_throttle.set_background_cpu_level("low")
        duty = cpu_throttle.DutyCycle()

        clock.advance(0.2)
        duty.tick()
        first = clock.total_slept
        clock.slept.clear()

        clock.advance(0.2)
        duty.tick()

        assert clock.total_slept == pytest.approx(first, abs=1e-6)

    def test_reset_discards_accumulated_work(self, clock):
        cpu_throttle.set_background_cpu_level("low")
        duty = cpu_throttle.DutyCycle()

        clock.advance(0.2)
        duty.reset()
        duty.tick()

        assert clock.slept == []


class TestThrottleTick:
    def test_regulator_is_reused_per_thread(self, clock):
        """Un régulateur par thread, conservé en thread-local : le temps de
        travail s'accumule d'un appel à l'autre au lieu de repartir de zéro."""
        cpu_throttle.set_background_cpu_level("low")

        clock.advance(0.2)
        cpu_throttle.throttle_tick()          # crée le régulateur, pas de pause
        assert clock.slept == []

        duty = cpu_throttle._local.duty
        clock.advance(0.2)
        cpu_throttle.throttle_tick()

        assert cpu_throttle._local.duty is duty
        assert clock.total_slept == pytest.approx(0.6, abs=1e-6)

    def test_forwards_the_cancellation_callback(self, clock):
        cpu_throttle.set_background_cpu_level("low")
        cpu_throttle.throttle_tick()

        clock.advance(0.2)
        cpu_throttle.throttle_tick(lambda: True)

        assert clock.slept == []


class TestProcessInitializer:
    def test_init_background_process_pulls_both_levers(self, monkeypatch):
        """La priorité seule ne suffit pas : un worker qui décode une photo de
        24 Mpx déclenche autant de threads OpenCV que de cœurs, à priorité
        normale puisqu'ils naissent hors de notre contrôle."""
        calls: list[str] = []
        monkeypatch.setattr(
            cpu_throttle, "lower_current_process_priority",
            lambda: calls.append("priority"),
        )
        monkeypatch.setattr(
            cpu_throttle, "limit_cv2_threads", lambda n=1: calls.append(f"cv2:{n}"),
        )

        cpu_throttle.init_background_process()

        assert calls == ["priority", "cv2:1"]

    def test_limit_cv2_threads_sets_opencv_pool(self, monkeypatch):
        cv2 = pytest.importorskip("cv2")
        saved = cv2.getNumThreads()
        try:
            cpu_throttle.limit_cv2_threads(1)
            assert cv2.getNumThreads() == 1
        finally:
            cv2.setNumThreads(saved)

    def test_limit_cv2_threads_is_silent_without_opencv(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "cv2", None)
        cpu_throttle.limit_cv2_threads(1)   # ne doit pas lever

    def test_lowering_priority_never_raises(self):
        """No-op silencieux hors Windows / sans psutil."""
        cpu_throttle.lower_current_thread_priority()
        cpu_throttle.lower_current_process_priority()


class TestWorkerCount:
    def test_never_below_the_minimum(self):
        assert cpu_throttle.throttled_worker_count(minimum=3) >= 3

    def test_roughly_the_configured_fraction(self, monkeypatch):
        monkeypatch.setattr(cpu_throttle.os, "cpu_count", lambda: 16)
        assert cpu_throttle.throttled_worker_count() == 2   # round(16 × 0.15)
