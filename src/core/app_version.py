# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import subprocess
import sys
from pathlib import Path

_FALLBACK_VERSION = "1.0"


def get_app_version() -> str:
    """Version affichée dans "À propos" : dernier tag git atteignable depuis HEAD.

    En mode figé (PyInstaller), le dossier .git n'est pas embarqué dans le bundle :
    on lit alors le fichier VERSION embarqué à la racine du bundle (écrit par
    build.ps1 à partir du VERSION du dépôt, cf. pixelphotomanager.spec), et on
    ne retombe sur _FALLBACK_VERSION que si ce fichier est absent/illisible.
    """
    if getattr(sys, "frozen", False):
        try:
            version_file = Path(getattr(sys, "_MEIPASS", "")) / "VERSION"
            text = version_file.read_text(encoding="utf-8").strip()
            if text:
                return text
        except Exception:
            pass
        return _FALLBACK_VERSION
    try:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return _FALLBACK_VERSION
