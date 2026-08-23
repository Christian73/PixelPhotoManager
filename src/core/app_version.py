# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import subprocess
import sys
import threading
from pathlib import Path

_FALLBACK_VERSION = "1.0"

_cache_lock = threading.Lock()
_cached_version: "str | None" = None


def get_app_version() -> str:
    """Version shown in "About": the latest git tag reachable from HEAD.

    Memoised: in dev mode the underlying `git describe` can take up to 2s
    (timeout below) — without a cache, the 4 call sites (startup, update-available
    popup, opening the help) would each rerun it. See also
    `warm_app_version_async()` to precompute that result outside the UI thread.
    """
    global _cached_version
    with _cache_lock:
        if _cached_version is None:
            _cached_version = _compute_app_version()
        return _cached_version


def warm_app_version_async() -> None:
    """Precomputes get_app_version() in a background thread, called early at
    startup so that the result is already cached when the UI needs it
    (rule: the UI never blocks)."""
    threading.Thread(target=get_app_version, daemon=True).start()


def _compute_app_version() -> str:
    """Version shown in "About": the latest git tag reachable from HEAD.

    In frozen mode (PyInstaller) the .git folder is not embedded in the bundle:
    we then read the VERSION file embedded at the root of the bundle (written by
    build.ps1 from the repository VERSION, cf. pixelphotomanager.spec), and only
    fall back on _FALLBACK_VERSION if that file is missing or unreadable.
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
