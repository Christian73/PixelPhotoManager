# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Shared filesystem helpers (scanner, watcher, dialogs)."""

import os


def is_hidden_path(path: str) -> bool:
    """True if the path is hidden: the Windows "Hidden" attribute or a name
    with a dot prefix. An unreadable path (deleted in the meantime, rights)
    is not considered hidden."""
    if os.path.basename(path).startswith("."):
        return True
    try:
        return bool(os.stat(path).st_file_attributes & 0x2)
    except (AttributeError, OSError):
        return False


def find_dvd_video_ts(folder: str) -> "str | None":
    """Returns the path of the VIDEO_TS subfolder if folder is a DVD copy
    (VIDEO_TS expected as a direct child, the standard structure of DVD
    copies), None otherwise. Case-insensitive search. An unreadable or
    missing folder returns None rather than raising an exception."""
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_dir() and entry.name.upper() == "VIDEO_TS":
                    return entry.path
    except OSError:
        pass
    return None
