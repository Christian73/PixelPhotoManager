# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Registre persistant des fichiers corrompus supprimés définitivement (cf.
main_window.py::_offer_corrupted_delete), au format JSON Lines dans
%LOCALAPPDATA%\\PixelPhotoManager\\deleted_corrupted_files.jsonl — permet à
l'utilisateur de retrouver après coup la liste exacte des chemins supprimés
pour tenter de les récupérer depuis une sauvegarde externe.

API publique :
    deleted_corrupted_files.add_deleted(paths)
    deleted_corrupted_files.get_entries()
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from src.core.app_dirs import APP_DATA_DIR

_REGISTRY_PATH = APP_DATA_DIR / "deleted_corrupted_files.jsonl"


class _DeletedCorruptedFiles:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = _REGISTRY_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def add_deleted(self, paths: list[str]) -> None:
        """Ajoute une entrée par fichier supprimé. Append-only : l'historique
        complet est conservé, jamais purgé automatiquement — c'est le seul
        endroit où le chemin d'un fichier corrompu supprimé survit après sa
        disparition du catalogue."""
        if not paths:
            return
        now = time.time()
        wall = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        lines = "".join(
            json.dumps({"ts": round(now, 3), "wall": wall, "path": p}, ensure_ascii=False) + "\n"
            for p in paths
        )
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(lines)
            except OSError:
                pass

    def get_entries(self) -> list[dict]:
        """Retourne toutes les entrées, plus récente en dernier."""
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
deleted_corrupted_files = _DeletedCorruptedFiles()
