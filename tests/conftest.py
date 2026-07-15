# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Garde-fou global : redirige %LOCALAPPDATA% vers un dossier temporaire de
session AVANT toute collecte/import de test.

`src/core/app_dirs.py::APP_DATA_DIR` est une constante de module calculée une
seule fois au premier import — la fixer ici, au niveau module de ce
conftest.py (chargé par pytest avant tout fichier tests/**/*.py), garantit
qu'aucun test (notamment Layer 2 : `EditPanel.__init__` instancie
`EditDatabase()` avec son chemin par défaut, sans point d'injection) ne peut
accidentellement lire/écrire dans le vrai %LOCALAPPDATA%\\PixelPhotoManager de
l'utilisateur.

Cette mutation ne porte que sur la variable d'environnement du *process
pytest en cours* (et de ses éventuels sous-processus) — jamais sur le profil
persistant réel de l'utilisateur.

Les tests Layer 1 (DB/logique) continuent d'utiliser `db_path=tmp_path/...`
en constructeur, qui ignore cette variable — ce garde-fou est une protection
supplémentaire, pas le mécanisme d'isolation principal pour ces tests-là.
"""
import atexit
import os
import shutil
import tempfile

_SESSION_LOCALAPPDATA = tempfile.mkdtemp(prefix="ppm_pytest_localappdata_")
os.environ["LOCALAPPDATA"] = _SESSION_LOCALAPPDATA


def _cleanup() -> None:
    shutil.rmtree(_SESSION_LOCALAPPDATA, ignore_errors=True)


atexit.register(_cleanup)
