# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Vérification de mise à jour : interroge la dernière release GitHub.

Dépôt public (Christian73/PixelPhotoManager) : l'API Releases est accessible
sans authentification. Utilisé au démarrage (notification silencieuse si à
jour ou en cas d'erreur, cf. main_window.py) et depuis la popup "À propos"
(qui affiche aussi les états "à jour" et "erreur", cf. help_dialog.py).
"""

import json
import logging
import urllib.request
from urllib.error import URLError

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

_RELEASES_API_URL = "https://api.github.com/repos/Christian73/PixelPhotoManager/releases/latest"
_TIMEOUT_S = 5

STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_ERROR = "error"                    # réseau/API indisponible
STATUS_VERSION_UNKNOWN = "version_unknown"  # version locale non comparable (mode dev, hash git)


def _parse_version(text: str) -> "tuple[int, ...] | None":
    """"v1.2.0" / "1.2.0" -> (1, 2, 0). None si illisible (ex. hash git en mode dev)."""
    text = text.strip().lstrip("vV")
    if not text:
        return None
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        return None


class UpdateCheckThread(QThread):
    """Interroge l'API GitHub Releases en arrière-plan (règle : l'UI ne bloque jamais).

    Émet toujours `checked`, avec l'un des trois statuts ci-dessus — contrairement
    à une simple notification "silencieuse si rien à signaler", la popup "À propos"
    a besoin de distinguer "à jour" de "vérification impossible".
    """

    checked = Signal(str, str, str)  # (status, latest_version, html_url)

    def __init__(self, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self._current_version = current_version

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                _RELEASES_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "PixelPhotoManager-UpdateChecker",
                },
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (URLError, TimeoutError, ValueError, OSError):
            logger.debug("UpdateCheckThread : vérification impossible (pas de réseau ?)", exc_info=True)
            self.checked.emit(STATUS_ERROR, "", "")
            return
        except Exception:
            logger.exception("UpdateCheckThread : erreur inattendue")
            self.checked.emit(STATUS_ERROR, "", "")
            return

        latest_tag = str(data.get("tag_name", ""))
        html_url = str(data.get("html_url", ""))
        latest = _parse_version(latest_tag)
        if latest is None or not html_url:
            # Réponse API inattendue (pas de tag/URL exploitable) — problème côté
            # GitHub, distinct d'une version locale non comparable.
            self.checked.emit(STATUS_ERROR, "", "")
            return

        version = latest_tag.lstrip("vV")
        current = _parse_version(self._current_version)
        if current is None:
            # Mode dev sur une branche où le tag n'est pas un ancêtre de HEAD :
            # get_app_version() retombe sur un hash git (ex. "17ab7a3-dirty"),
            # non comparable à un numéro sémantique — à ne pas confondre avec
            # une erreur réseau (l'appel à l'API a bien réussi).
            self.checked.emit(STATUS_VERSION_UNKNOWN, version, html_url)
            return

        if latest > current:
            self.checked.emit(STATUS_UPDATE_AVAILABLE, version, html_url)
        else:
            self.checked.emit(STATUS_UP_TO_DATE, version, html_url)
