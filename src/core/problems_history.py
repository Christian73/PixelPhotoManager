# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
History of the problems encountered (e.g. corrupted files detected during a
duplicate search), recorded in JSON Lines format in
%LOCALAPPDATA%\PixelPhotoManager\problems_history.jsonl

Public API:
    problems_history.add_entry(corrupted_count, repaired_count, list_path)
    problems_history.get_entries()
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from src.core.app_dirs import APP_DATA_DIR

_HISTORY_PATH = APP_DATA_DIR / "problems_history.jsonl"


class _ProblemsHistory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = _HISTORY_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def add_entry(self, corrupted_count: int, repaired_count: int,
                  list_path: str | None) -> None:
        """Records the result of one detection/repair cycle for corrupted files.
        `list_path` points to the text file listing the files still failing
        (None if there is none, or if repaired_count already covers every
        corrupted file)."""
        now = time.time()
        entry = {
            "ts": round(now, 3),
            "wall": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            "corrupted_count": corrupted_count,
            "repaired_count": repaired_count,
            "still_failed_count": corrupted_count - repaired_count,
            "list_path": list_path,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass

    def get_entries(self) -> list[dict]:
        """Returns every entry, most recent last."""
        try:
            with self._lock:
                with open(self._path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            result = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return result
        except FileNotFoundError:
            return []


# Singleton global
problems_history = _ProblemsHistory()
