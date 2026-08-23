# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Launches PixelPhotoManager in a subprocess with an isolated, disposable
`%LOCALAPPDATA%` profile, distinct from the real profile of the user.

`src/core/app_dirs.py::APP_DATA_DIR` is a module constant computed only
once at import time (`Path(os.environ.get("LOCALAPPDATA", ...)) / "PixelPhotoManager"`),
taken as-is by 13 dependent modules. There is neither a dedicated env variable
nor a CLI argument to override it -- the only supported way to
redirect the whole application is to set `LOCALAPPDATA` in the environment
block of a **subprocess**, before its first import of application
code. This module never mutates the environment variable of the calling
process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class LaunchedApp:
    process: subprocess.Popen
    launcher_pid: int
    app_data_dir: Path
    log_path: Path


def prepare_app_data_dir(
    app_data_dir: Path,
    scan_folders: list[str],
    extra_config: dict | None = None,
) -> None:
    """Prepares an isolated `%LOCALAPPDATA%\\PixelPhotoManager`: a minimal
    `config.json` is enough -- `Catalog`/`ThumbnailCache`/`FaceDatabase` create and
    migrate themselves on the first launch, no database to pre-seed."""
    app_data_dir = Path(app_data_dir)
    ppm_dir = app_data_dir / "PixelPhotoManager"
    ppm_dir.mkdir(parents=True, exist_ok=True)

    config = {"scan_folders": [str(p) for p in scan_folders]}
    if extra_config:
        config.update(extra_config)

    (ppm_dir / "config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def launch_app(
    app_data_dir: Path,
    scan_folders: list[str],
    *,
    extra_config: dict | None = None,
    python_exe: Path | None = None,
    repo_root: Path | None = None,
) -> LaunchedApp:
    """Launches `main.py` in a subprocess, `LOCALAPPDATA` redirected ONLY
    in the environment block of that child (never in the calling process)."""
    app_data_dir = Path(app_data_dir)
    repo_root = Path(repo_root) if repo_root else _REPO_ROOT
    python_exe = Path(python_exe) if python_exe else repo_root / ".venv" / "Scripts" / "python.exe"

    prepare_app_data_dir(app_data_dir, scan_folders, extra_config)

    # PPM_SUPPRESS_EXPLORER=1: the export normally opens the destination
    # folder in Explorer (main_window.py::_run_export) -- in e2e,
    # that window would come in front of the window driven by UIA and would stay
    # open after the end of the test (explorer.exe is not a child of the
    # application process, so it is never closed by terminate()), disturbing
    # the following scenarios.
    child_env = {
        **os.environ,
        "LOCALAPPDATA": str(app_data_dir),
        "PPM_SUPPRESS_EXPLORER": "1",
    }

    # PPM_E2E_COVERAGE=1: run the application under coverage.py so that the
    # e2e scenarios credit the coverage of the UI code. `parallel = true`
    # (.coveragerc) makes it write a distinct .coverage.<host>.<pid> file
    # in the cwd (the root of the repository), to be merged afterwards with
    # `coverage combine` before `coverage report`. Requires a CLEAN shutdown
    # of the application (cf. _graceful_close in tests/e2e/conftest.py):
    # a TerminateProcess does not run the atexit hook that writes the data.
    cmd = [str(python_exe), "main.py"]
    if os.environ.get("PPM_E2E_COVERAGE") == "1":
        cmd = [
            str(python_exe), "-m", "coverage", "run",
            "--rcfile", str(repo_root / ".coveragerc"),
            "main.py",
        ]

    process = subprocess.Popen(
        cmd,
        cwd=str(repo_root),
        env=child_env,
    )

    # main.py only redirects the logs towards %LOCALAPPDATA% in frozen mode
    # (PyInstaller, sys.frozen); in dev mode (this launcher), they always go
    # into <repo>/logs/, independently of LOCALAPPDATA.
    log_path = repo_root / "logs" / "pixelphotomanager.log"

    return LaunchedApp(
        process=process,
        launcher_pid=process.pid,
        app_data_dir=app_data_dir,
        log_path=log_path,
    )


def terminate(app: LaunchedApp, window_pid: int | None = None, *, timeout: float = 10.0) -> None:
    """Explicitly terminates the known PID(s) of the isolated application --
    never a broad `taskkill /IM`. `window_pid` (resolved separately, e.g. through
    pywinauto) may differ from the launcher PID `app.launcher_pid`.

    Also includes every descendant (`psutil.children(recursive=True)`),
    captured BEFORE terminating the parents: Windows never kills the children
    of a terminated process (no automatic cascade as on POSIX), and
    InsightFace/scikit-learn start `multiprocessing` workers (the `spawn`
    method, hence independent `python.exe` processes) during face
    indexing. Without that explicit collection, every e2e run left real
    orphan processes behind (confirmed empirically: ~20 `python.exe`
    `--multiprocessing-fork` with a dead parent after a handful of runs), which
    end up exhausting the memory/page file to the point of
    failing the loading of the ONNX models of the next run."""
    pids: set[int] = {app.launcher_pid}
    if window_pid is not None:
        pids.add(window_pid)
    for pid in list(pids):
        try:
            pids |= {c.pid for c in psutil.Process(pid).children(recursive=True)}
        except psutil.NoSuchProcess:
            pass

    for pid in pids:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue

    deadline = time.monotonic() + timeout
    for pid in pids:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except (psutil.TimeoutExpired, psutil.NoSuchProcess):
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass


def _main() -> None:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-data", type=Path,
        default=Path(tempfile.mkdtemp(prefix="ppm_isolated_")),
        help="Dossier isolé à utiliser comme LOCALAPPDATA (par défaut : temp jetable)",
    )
    parser.add_argument("--scan-folder", action="append", dest="scan_folders", default=[],
                         help="Dossier à surveiller (répétable)")
    args = parser.parse_args()

    if not args.scan_folders:
        print("Aucun --scan-folder fourni — l'onboarding s'affichera au démarrage.")

    app = launch_app(args.app_data, args.scan_folders)
    print(f"Lancé : PID={app.launcher_pid}, LOCALAPPDATA={app.app_data_dir}")
    print(f"Log : {app.log_path}")
    print("Ctrl+C pour terminer proprement le processus isolé.")
    try:
        app.process.wait()
    except KeyboardInterrupt:
        terminate(app)


if __name__ == "__main__":
    sys.exit(_main())
