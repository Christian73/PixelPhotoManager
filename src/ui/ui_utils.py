# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Petits helpers d'affichage partagés entre widgets."""


def fmt_size(size_bytes: int) -> str:
    """Formate une taille fichier pour l'UI : « 512 Ko », « 3.2 Mo », "" si inconnue."""
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} Ko"
    return f"{size_bytes / (1024 * 1024):.1f} Mo"
