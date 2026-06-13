"""
Journal d'activité des threads.

Chaque événement est enregistré au format JSON Lines dans
%LOCALAPPDATA%\PixelPhotoManager\thread_journal.jsonl

API publique :
    journal.start(thread, msg, **extra)  → retourne un token (float t0)
    journal.step(thread, msg, t0=None, **extra)
    journal.end(thread, msg, t0, **extra)
    journal.error(thread, msg, **extra)
    journal.get_entries(limit)
    journal.clear()
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from src.core.app_dirs import APP_DATA_DIR


def rss_mb() -> float:
    """Retourne la mémoire résidente (RSS) du processus en Mo. 0.0 si indisponible."""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1_048_576
    except Exception:
        return 0.0

_JOURNAL_PATH = APP_DATA_DIR / "thread_journal.jsonl"
_MAX_LINES    = 8_000   # rotation au-delà de cette limite
_KEEP_LINES   = 5_000   # lignes conservées après rotation


class _ThreadJournal:
    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._path  = _JOURNAL_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._line_count = self._count_lines()

    # ------------------------------------------------------------------ public

    def start(self, thread: str, msg: str, **extra) -> float:
        """Enregistre le démarrage d'un thread. Retourne t0 (perf_counter)."""
        t0 = time.perf_counter()
        self._write("START", thread, msg, elapsed_ms=None, **extra)
        return t0

    def step(self, thread: str, msg: str, t0: float | None = None, **extra) -> None:
        """Enregistre une étape intermédiaire."""
        elapsed = (time.perf_counter() - t0) * 1000 if t0 is not None else None
        self._write("STEP", thread, msg, elapsed_ms=elapsed, **extra)

    def end(self, thread: str, msg: str, t0: float, **extra) -> None:
        """Enregistre la fin d'un thread avec la durée totale."""
        elapsed = (time.perf_counter() - t0) * 1000
        self._write("END", thread, msg, elapsed_ms=round(elapsed, 1), **extra)

    def error(self, thread: str, msg: str, t0: float | None = None, **extra) -> None:
        """Enregistre une erreur."""
        elapsed = (time.perf_counter() - t0) * 1000 if t0 is not None else None
        self._write("ERROR", thread, msg, elapsed_ms=elapsed, **extra)

    def get_entries(self, limit: int = 2000) -> list[dict]:
        """Retourne les `limit` dernières entrées, plus récente en dernier."""
        try:
            with self._lock:
                with open(self._path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            result = []
            for line in lines[-limit:]:
                line = line.strip()
                if line:
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return result
        except FileNotFoundError:
            return []

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.write_text("", encoding="utf-8")
                self._line_count = 0
            except OSError:
                pass

    # ------------------------------------------------------------------ internal

    def _write(self, event: str, thread: str, msg: str,
               elapsed_ms: float | None, **extra) -> None:
        now  = time.time()
        tid  = threading.current_thread().ident or 0
        entry = {
            "ts":         round(now, 3),
            "wall":       datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "tid":        tid,
            "thread":     thread,
            "event":      event,
            "msg":        msg,
            "elapsed_ms": elapsed_ms,
            **extra,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
                self._line_count += 1
                if self._line_count > _MAX_LINES:
                    self._rotate()
            except OSError:
                pass

    def _rotate(self) -> None:
        """Garde les _KEEP_LINES dernières lignes."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(self._path, "w", encoding="utf-8") as f:
                f.writelines(lines[-_KEEP_LINES:])
            self._line_count = _KEEP_LINES
        except OSError:
            pass

    def _count_lines(self) -> int:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0


# Singleton global
journal = _ThreadJournal()
