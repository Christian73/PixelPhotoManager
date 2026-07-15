# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Fixtures et utilitaires communs aux scénarios bout-en-bout (Layer 3).

Ces tests pilotent la vraie application (`main.py`) en sous-processus, avec
un `%LOCALAPPDATA%` isolé (voir `tools/test_env/launch_isolated.py`), contre
une bibliothèque synthétique reproductible (voir
`tools/test_env/generate_library.py`). Aucun test de ce dossier ne touche au
profil réel de l'utilisateur.

`pywinauto` est une dépendance optionnelle (`requirements-test-e2e.txt`,
Windows-only) : si elle est absente, les scénarios de `scenarios/` sont
retirés de la collecte (`collect_ignore_glob` ci-dessous) pour que
`pytest tests/` (Layers 1+2) continue de fonctionner sans elle."""
from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

try:
    import pywinauto  # noqa: F401
    _HAS_PYWINAUTO = True
except ImportError:
    _HAS_PYWINAUTO = False

collect_ignore_glob = [] if _HAS_PYWINAUTO else ["scenarios/*"]

from tools.test_env import launch_isolated
from tools.test_env.generate_library import LibraryManifest, build_library


@dataclass
class IsolatedApp:
    app: "launch_isolated.LaunchedApp"
    manifest: LibraryManifest
    window: object          # pywinauto.application.WindowSpecification
    window_pid: int
    catalog_db: Path
    edits_db: Path


@pytest.fixture(scope="session")
def synthetic_library_master(tmp_path_factory) -> LibraryManifest:
    """Construit la bibliothèque synthétique une seule fois par session
    (génération procédurale : quelques secondes, formes ORB incluses)."""
    root = tmp_path_factory.mktemp("ppm_e2e_library_master")
    return build_library(root)


@pytest.fixture
def isolated_app(tmp_path, synthetic_library_master):
    """Copie la bibliothèque master dans un dossier de photos dédié au test,
    lance l'application avec un `%LOCALAPPDATA%` isolé pointant dessus, et
    garantit la terminaison du processus même si le test échoue en cours de
    setup ou d'exécution."""
    photos_dir = tmp_path / "photos"
    shutil.copytree(synthetic_library_master.root, photos_dir)
    manifest = synthetic_library_master.rebased(photos_dir)

    app_data_dir = tmp_path / "app_data"
    app = launch_isolated.launch_app(app_data_dir, [str(photos_dir)])

    try:
        window_pid = _find_window_pid(app.launcher_pid)
        window = _connect_main_window(window_pid)
        yield IsolatedApp(
            app=app,
            manifest=manifest,
            window=window,
            window_pid=window_pid,
            catalog_db=app_data_dir / "PixelPhotoManager" / "catalog.db",
            edits_db=app_data_dir / "PixelPhotoManager" / "edits.db",
        )
    finally:
        launch_isolated.terminate(app, window_pid=locals().get("window_pid"))


def _find_window_pid(launcher_pid: int, timeout: float = 30.0) -> int:
    """Résout le PID propriétaire de la fenêtre principale UIA.

    Confirmé empiriquement sur ce poste : `subprocess.Popen([python_exe,
    "main.py"], ...).pid` (= `launcher_pid`) N'EST PAS le PID propriétaire de
    la fenêtre — `.venv\\Scripts\\python.exe` relance un vrai process enfant
    (`python.exe`) qui, lui, possède la fenêtre "PixelPhotoManager". Chercher
    par titre parmi `launcher_pid` ET ses descendants (`psutil`), jamais par
    `launcher_pid` seul (cf. bug initial : timeout systématique alors que
    l'appli tournait correctement — 18 fenêtres desktop trouvées, 0 pour
    `process=launcher_pid`)."""
    import psutil
    from pywinauto import findwindows

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        candidate_pids = {launcher_pid}
        try:
            candidate_pids |= {c.pid for c in psutil.Process(launcher_pid).children(recursive=True)}
        except psutil.NoSuchProcess:
            pass
        for pid in candidate_pids:
            try:
                handles = findwindows.find_windows(
                    process=pid, title="PixelPhotoManager", backend="uia"
                )
                if handles:
                    return pid
            except Exception as exc:
                last_exc = exc
        time.sleep(0.5)
    raise TimeoutError(
        f"Aucune fenêtre 'PixelPhotoManager' détectée pour le PID {launcher_pid} "
        f"ou ses descendants après {timeout}s ({last_exc})"
    )


def _connect_main_window(window_pid: int, timeout: float = 30.0):
    from pywinauto.application import Application

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            app = Application(backend="uia").connect(process=window_pid)
            win = app.window(title="PixelPhotoManager")
            win.wait("exists", timeout=5)
            return win
        except Exception as exc:
            last_exc = exc
            time.sleep(0.5)
    raise TimeoutError(
        f"Impossible de se connecter à la fenêtre principale (PID {window_pid}) : {last_exc}"
    )


def click_menu_item(window, top_level_text: str, item_text: str, *, timeout: float = 10.0) -> None:
    """Ouvre un menu de la barre de menu principale puis clique un de ses
    éléments — remplace `window.menu_select("A->B")`, qui échoue sur cette
    appli avec un `AttributeError` (`menu_select()` cherche un
    `children(control_type="MenuBar")` ou un `descendants(control_type="Menu")`
    directement sur `window`, alors qu'ici le `MenuBar` est bien présent mais
    ses menus déroulants (`Outils`, etc.) sont peuplés paresseusement par Qt :
    les `MenuItem` du sous-menu (ex. "Détecter les doublons…") n'existent dans
    l'arbre UIA qu'une fois le menu de premier niveau réellement ouvert par un
    clic, jamais avant). Cible donc le `MenuItem` de premier niveau par texte,
    clique pour l'ouvrir, puis cherche le `MenuItem` voulu parmi les
    descendants nouvellement apparus."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    top_item = None
    while time.monotonic() < deadline and top_item is None:
        try:
            for item in window.descendants(control_type="MenuItem"):
                if item.window_text() == top_level_text:
                    top_item = item
                    break
        except Exception as exc:
            last_exc = exc
        if top_item is None:
            time.sleep(0.3)
    if top_item is None:
        raise LookupError(f"Menu {top_level_text!r} introuvable après {timeout}s ({last_exc})")
    top_item.click_input()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for item in window.descendants(control_type="MenuItem"):
                label = item.window_text()
                if item_text in label:
                    item.click_input()
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(
        f"Élément de menu {item_text!r} introuvable sous {top_level_text!r} après {timeout}s ({last_exc})"
    )


def find_dialog_button(window, texts: list[str], exact: bool = False, *, timeout: float = 10.0):
    """Cherche un bouton parmi les descendants de `window` (les QMessageBox
    natifs ne sont PAS des fenêtres top-level UIA distinctes — ce sont des
    descendants de la fenêtre principale) dont le texte correspond à l'une des
    variantes de `texts` (utile pour les libellés localisés : ex.
    `["Oui", "Yes"]`). Retente jusqu'à `timeout` : le dialogue peut apparaître
    avec un léger délai après l'action qui le déclenche."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for button in window.descendants(control_type="Button"):
                label = button.window_text()
                for text in texts:
                    if (label == text) if exact else (text.lower() in label.lower()):
                        return button
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Aucun bouton correspondant à {texts!r} trouvé après {timeout}s ({last_exc})")


def click_yes(window) -> None:
    find_dialog_button(window, ["Oui", "Yes", "&Oui", "&Yes"]).click_input()


def click_no(window) -> None:
    find_dialog_button(window, ["Non", "No", "&Non", "&No"]).click_input()


def wait_for_log(log_path: Path, pattern: str, *, since_offset: int = 0,
                  timeout: float = 60.0, poll: float = 0.5) -> str:
    """Attend qu'une ligne contenant `pattern` apparaisse dans `log_path` après
    `since_offset` (octets). Retourne la ligne trouvée. Lève `TimeoutError`
    sinon — capture les dernières lignes du log dans le message pour faciliter
    le diagnostic d'un scénario en échec."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            with open(log_path, encoding="utf-8", errors="replace") as f:
                f.seek(since_offset)
                lines = f.readlines()
            for line in lines:
                if pattern in line:
                    return line
        time.sleep(poll)
    tail = "".join(lines[-20:]) if log_path.exists() else "(fichier de log absent)"
    raise TimeoutError(
        f"Motif {pattern!r} non trouvé dans {log_path} après {timeout}s.\n"
        f"Dernières lignes lues :\n{tail}"
    )


def log_offset(log_path: Path) -> int:
    """Taille actuelle du log, à capturer avant de déclencher une opération
    pour que `wait_for_log` ne matche pas une ligne d'un run précédent."""
    return log_path.stat().st_size if log_path.exists() else 0


def find_thumbnail(window, photo_path: str, *, timeout: float = 15.0):
    """Retrouve l'élément UIA d'une vignette précise via son nom accessible
    `thumb::<chemin>` (cf. `ThumbnailCell._setup_ui` dans thumbnail_grid.py) —
    plus robuste que deviner des coordonnées écran dans la grille virtuelle.

    Confirmé empiriquement (probe UIA) : un `QWidget` avec `setAccessibleName`
    mais sans rôle explicite se mappe par le pont d'accessibilité Qt sur
    `control_type="Group"`, PAS `"Pane"` — la grille ne virtualise que les
    cellules hors de `_visible_range()` (+ marge d'un écran), donc une photo
    doit être scrollée dans le champ pour que sa cellule (et son nom
    accessible) existe dans l'arbre UIA."""
    name = f"thumb::{photo_path}"
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return window.child_window(title=name, control_type="Group").wrapper_object()
        except Exception as exc:
            last_exc = exc
            time.sleep(0.5)
    raise TimeoutError(f"Vignette {name!r} introuvable après {timeout}s : {last_exc}")


def wait_for_condition(predicate, *, timeout: float = 30.0, poll: float = 0.3,
                        message: str = "condition non remplie") -> None:
    """Attend qu'une fonction sans argument devienne vraie — utilisé pour
    sonder directement `catalog.db`/`edits.db` (fichiers SQLite réels), plus
    robuste qu'un texte affiché dans une grille custom peinte à la main."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    raise TimeoutError(f"Timeout ({timeout}s) : {message}")


def query_one(db_path: Path, sql: str, params: tuple = ()) -> object:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
