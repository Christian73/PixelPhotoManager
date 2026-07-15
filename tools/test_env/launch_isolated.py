# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Lance PixelPhotoManager en sous-processus avec un profil `%LOCALAPPDATA%`
isolé, jetable, distinct du profil réel de l'utilisateur.

`src/core/app_dirs.py::APP_DATA_DIR` est une constante de module calculée une
seule fois à l'import (`Path(os.environ.get("LOCALAPPDATA", ...)) / "PixelPhotoManager"`),
reprise telle quelle par 13 modules dépendants. Il n'existe ni variable d'env
dédiée ni argument CLI pour la surcharger — la seule façon supportée de
rediriger l'appli entière est de fixer `LOCALAPPDATA` dans le bloc
d'environnement d'un **sous-processus**, avant son premier import de code
applicatif. Ce module ne mute jamais la variable d'environnement du process
appelant.
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
    """Prépare un `%LOCALAPPDATA%\\PixelPhotoManager` isolé : un `config.json`
    minimal suffit — `Catalog`/`ThumbnailCache`/`FaceDatabase` s'auto-créent et
    s'auto-migrent au premier lancement, aucune base à pré-semer."""
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
    """Lance `main.py` en sous-processus, `LOCALAPPDATA` redirigé UNIQUEMENT
    dans le bloc d'environnement de cet enfant (jamais dans le process appelant)."""
    app_data_dir = Path(app_data_dir)
    repo_root = Path(repo_root) if repo_root else _REPO_ROOT
    python_exe = Path(python_exe) if python_exe else repo_root / ".venv" / "Scripts" / "python.exe"

    prepare_app_data_dir(app_data_dir, scan_folders, extra_config)

    child_env = {**os.environ, "LOCALAPPDATA": str(app_data_dir)}

    process = subprocess.Popen(
        [str(python_exe), "main.py"],
        cwd=str(repo_root),
        env=child_env,
    )

    # main.py ne redirige les logs vers %LOCALAPPDATA% qu'en mode figé
    # (PyInstaller, sys.frozen) ; en mode dev (ce lanceur), ils vont toujours
    # dans <repo>/logs/, indépendamment de LOCALAPPDATA.
    log_path = repo_root / "logs" / "pixelphotomanager.log"

    return LaunchedApp(
        process=process,
        launcher_pid=process.pid,
        app_data_dir=app_data_dir,
        log_path=log_path,
    )


def terminate(app: LaunchedApp, window_pid: int | None = None, *, timeout: float = 10.0) -> None:
    """Termine explicitement le(s) PID connu(s) de l'application isolée —
    jamais un `taskkill /IM` large. `window_pid` (résolu séparément, ex. via
    pywinauto) peut différer du PID lanceur `app.launcher_pid`.

    Inclut aussi tous les descendants (`psutil.children(recursive=True)`),
    capturés AVANT de terminer les parents : Windows ne tue jamais les enfants
    d'un process terminé (pas de cascade automatique comme sur POSIX), et
    InsightFace/scikit-learn démarrent des workers `multiprocessing` (procédé
    `spawn`, donc des `python.exe` indépendants) pendant l'indexation des
    visages. Sans ce ramassage explicite, chaque run e2e laissait de vrais
    processus orphelins (confirmé empiriquement : ~20 `python.exe`
    `--multiprocessing-fork` à parent mort après une poignée de runs), qui
    finissent par épuiser la mémoire/le fichier de pagination au point de
    faire échouer le chargement des modèles ONNX du run suivant."""
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
