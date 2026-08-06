# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Utilitaires système de fichiers partagés (scanner, watcher, dialogues)."""

import os


def is_hidden_path(path: str) -> bool:
    """True si le chemin est caché : attribut Windows « Caché » ou nom à
    préfixe point. Un chemin illisible (supprimé entre-temps, droits) n'est
    pas considéré caché."""
    if os.path.basename(path).startswith("."):
        return True
    try:
        return bool(os.stat(path).st_file_attributes & 0x2)
    except (AttributeError, OSError):
        return False


def find_dvd_video_ts(folder: str) -> "str | None":
    """Retourne le chemin du sous-dossier VIDEO_TS si folder est une copie de
    DVD (VIDEO_TS attendu en enfant direct, structure standard des copies de
    DVD), sinon None. Recherche insensible à la casse. Un dossier illisible
    ou inexistant renvoie None plutôt que de lever une exception."""
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_dir() and entry.name.upper() == "VIDEO_TS":
                    return entry.path
    except OSError:
        pass
    return None
