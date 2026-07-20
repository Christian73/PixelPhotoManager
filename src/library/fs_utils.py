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
