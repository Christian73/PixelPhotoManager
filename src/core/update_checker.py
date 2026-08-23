# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Update check: queries the latest GitHub release.

Public repository (Christian73/PixelPhotoManager): the Releases API is
reachable without authentication. Used at startup (a silent notification if up
to date or on error, cf. main_window.py) and from the "About" popup (which also
shows the "up to date" and "error" states, cf. help_dialog.py).
"""

import json
import logging
import urllib.request
from urllib.error import URLError

from PySide6.QtCore import QThread, Signal

from src.core.app_version import get_app_version

logger = logging.getLogger(__name__)

_RELEASES_API_URL = "https://api.github.com/repos/Christian73/PixelPhotoManager/releases/latest"
_TIMEOUT_S = 5

STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_ERROR = "error"                    # network/API unavailable
STATUS_VERSION_UNKNOWN = "version_unknown"  # version locale non comparable (mode dev, hash git)


def _parse_version(text: str) -> "tuple[int, ...] | None":
    """"v1.2.0" / "1.2.0" -> (1, 2, 0). None if unreadable (e.g. a git hash in dev mode)."""
    text = text.strip().lstrip("vV")
    if not text:
        return None
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        return None


class UpdateCheckThread(QThread):
    """Queries the GitHub Releases API in the background (rule: the UI never blocks).

    Always emits `checked`, with one of the three statuses above — unlike a
    plain notification "silent if there is nothing to report", the "About" popup
    needs to tell "up to date" apart from "check impossible"."""

    checked = Signal(str, str, str)  # (status, latest_version, html_url)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        # get_app_version() runs `git describe` in dev mode (up to 2s) — computed
        # here rather than passed to the constructor so as to never block the UI thread.
        current_version = get_app_version()
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
            # Unexpected API response (no usable tag/URL) — a problem on the GitHub
            # side, distinct from a local version that cannot be compared.
            self.checked.emit(STATUS_ERROR, "", "")
            return

        version = latest_tag.lstrip("vV")
        current = _parse_version(current_version)
        if current is None:
            # Dev mode on a branch where the tag is not an ancestor of HEAD:
            # get_app_version() falls back on a git hash (e.g. "17ab7a3-dirty"), not
            # comparable to a semantic number — not to be confused with a network
            # error (the API call did succeed).
            self.checked.emit(STATUS_VERSION_UNKNOWN, version, html_url)
            return

        width = max(len(latest), len(current))
        latest_padded = latest + (0,) * (width - len(latest))
        current_padded = current + (0,) * (width - len(current))
        if latest_padded > current_padded:
            self.checked.emit(STATUS_UPDATE_AVAILABLE, version, html_url)
        else:
            self.checked.emit(STATUS_UP_TO_DATE, version, html_url)
