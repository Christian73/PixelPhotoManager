# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Suppression via la corbeille Windows — point unique pour toute l'application.

Règle : l'application n'efface JAMAIS définitivement un fichier utilisateur.
Toute suppression passe par move_to_trash() ; en cas d'échec (lecteur réseau
ou volume sans corbeille → TrashPermissionError), l'exception remonte à
l'appelant qui doit informer l'utilisateur que le fichier n'a PAS été
supprimé — surtout pas de repli unlink silencieux.

Les fichiers temporaires internes (tempfile, dossiers _restore_tmp…) ne sont
pas concernés : leur unlink direct reste légitime.
"""

import logging
import os

logger = logging.getLogger(__name__)


def move_to_trash(path: str) -> None:
    """Envoie un fichier ou un dossier (récursivement) à la corbeille Windows.

    Lève FileNotFoundError si le chemin n'existe pas, et laisse remonter
    TrashPermissionError / OSError si la corbeille est indisponible pour ce
    volume (lecteur réseau, clé USB configurée sans corbeille…).
    """
    from send2trash import send2trash

    norm = os.path.normpath(path)
    if not os.path.exists(norm):
        raise FileNotFoundError(norm)
    send2trash(norm)
    logger.debug("Envoyé à la corbeille : %s", norm)


def is_trash_available() -> bool:
    """True si le module send2trash est importable (toujours le cas en
    installation normale — sert de garde-fou aux tests et au packaging)."""
    import importlib.util
    return importlib.util.find_spec("send2trash") is not None
