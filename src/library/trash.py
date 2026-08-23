# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Deletion through the Windows recycle bin — the single point for the whole application.

Rule: the application NEVER permanently erases a user file. Every deletion
goes through move_to_trash(); on failure (network drive or volume with no
recycle bin → TrashPermissionError), the exception propagates to the caller,
which must tell the user that the file has NOT been deleted — never a silent
unlink fallback.

The internal temporary files (tempfile, _restore_tmp… folders) are not
concerned: their direct unlink stays legitimate.
"""

import logging
import os

logger = logging.getLogger(__name__)


def move_to_trash(path: str) -> None:
    """Sends a file or a folder (recursively) to the Windows recycle bin.

    Raises FileNotFoundError if the path does not exist, and lets
    TrashPermissionError / OSError propagate if the recycle bin is unavailable
    for that volume (network drive, USB key configured without one…).
    """
    from send2trash import send2trash

    norm = os.path.normpath(path)
    if not os.path.exists(norm):
        raise FileNotFoundError(norm)
    send2trash(norm)
    logger.debug("Envoyé à la corbeille : %s", norm)


def is_trash_available() -> bool:
    """True if the send2trash module is importable (always the case in a
    normal installation — a safety net for the tests and the packaging)."""
    import importlib.util
    return importlib.util.find_spec("send2trash") is not None
