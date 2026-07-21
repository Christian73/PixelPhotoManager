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
from dataclasses import dataclass, field
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
    faces_db: Path
    face_photos: list[Path] = field(default_factory=list)


@pytest.fixture(scope="session")
def synthetic_library_master(tmp_path_factory) -> LibraryManifest:
    """Construit la bibliothèque synthétique une seule fois par session
    (génération procédurale : quelques secondes, formes ORB incluses)."""
    root = tmp_path_factory.mktemp("ppm_e2e_library_master")
    return build_library(root)


def _build_isolated_app(tmp_path: Path, synthetic_library_master: LibraryManifest, *,
                         extra_photo_files: tuple[Path, ...] = (),
                         extra_config: dict | None = None):
    """Copie la bibliothèque master dans un dossier de photos dédié au test
    (+ éventuels fichiers photo supplémentaires, ex. fixtures visages), lance
    l'application avec un `%LOCALAPPDATA%` isolé pointant dessus, et garantit
    la terminaison du processus même si le test échoue en cours de setup ou
    d'exécution. Partagé par `isolated_app` et `isolated_app_with_faces`."""
    photos_dir = tmp_path / "photos"
    shutil.copytree(synthetic_library_master.root, photos_dir)
    manifest = synthetic_library_master.rebased(photos_dir)

    rebased_extra_photos = []
    for src in extra_photo_files:
        dest = photos_dir / src.name
        shutil.copy2(src, dest)
        rebased_extra_photos.append(dest)

    app_data_dir = tmp_path / "app_data"
    app = launch_isolated.launch_app(app_data_dir, [str(photos_dir)], extra_config=extra_config)

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
            faces_db=app_data_dir / "PixelPhotoManager" / "faces.db",
            face_photos=rebased_extra_photos,
        )
    finally:
        _graceful_close(locals().get("window"), app)
        launch_isolated.terminate(app, window_pid=locals().get("window_pid"))


@pytest.fixture
def isolated_app(request, tmp_path, synthetic_library_master):
    """Instance isolée standard, sans photo de visage. Accepte une
    configuration de départ non par défaut via une paramétrisation indirecte :
    `@pytest.mark.parametrize("isolated_app", [{"ui.delete_no_confirm": False}], indirect=True)`."""
    extra_config = getattr(request, "param", None)
    yield from _build_isolated_app(tmp_path, synthetic_library_master, extra_config=extra_config)


_FACES_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "faces"


@pytest.fixture
def isolated_app_with_faces(tmp_path, synthetic_library_master):
    """Comme `isolated_app`, avec en plus les photos de visages de
    `tests/e2e/fixtures/faces/` (3 solo « Personne A », 3 solo « Personne B »,
    1 photo des deux ensemble) copiées dans le dossier de photos surveillé,
    pour les scénarios d'identification/fusion/reset de visages."""
    face_files = sorted(_FACES_FIXTURES_DIR.glob("*.jpg"))
    if not face_files:
        pytest.skip(f"Aucune fixture visage trouvée dans {_FACES_FIXTURES_DIR}")
    yield from _build_isolated_app(tmp_path, synthetic_library_master, extra_photo_files=tuple(face_files))


def _graceful_close(window, app, timeout: float = 20.0) -> None:
    """Tente une fermeture propre de l'application avant le terminate() de
    secours. Indispensable quand l'appli tourne sous coverage
    (PPM_E2E_COVERAGE=1, cf. launch_isolated) : TerminateProcess n'exécute
    jamais le hook atexit qui écrit les données de couverture. Best-effort :
    confirme l'éventuel avertissement « analyse en cours » puis attend la fin
    du processus ; en cas d'échec, terminate() reprend la main."""
    if window is None:
        return
    try:
        window.close()
    except Exception:
        return
    try:
        # closeEvent peut demander confirmation (ex. détection de doublons en
        # cours) — cliquer Oui si le bouton apparaît, sinon continuer.
        try:
            find_dialog_button(window, ["Oui", "Yes", "&Oui", "&Yes"], timeout=3.0).click_input()
        except Exception:
            pass
        app.process.wait(timeout=timeout)
    except Exception:
        pass


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
            # class_name="MainWindow" est nécessaire en plus du titre : un QMenu
            # contextuel ouvert plus tard dans le test expose lui aussi
            # Name="PixelPhotoManager" via UIA (popup top-level Qt, même
            # control_type="Window", mais class_name="QMenu" — confirmé par sonde
            # `Desktop(backend="uia").windows()` pendant qu'un menu était ouvert),
            # ce qui rend `app.window(title="PixelPhotoManager")` seul ambigu
            # (ElementAmbiguousError, 2 éléments) dès qu'un menu contextuel est
            # affiché.
            win = app.window(title="PixelPhotoManager", class_name="MainWindow")
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


def invoke_button(
    window, texts: list[str], exact: bool = False, *, timeout: float = 10.0,
    wait_gone: bool = True, gone_timeout: float = 10.0,
) -> None:
    """Clique un bouton via le pattern UIA Invoke plutôt qu'un clic souris
    simulé (`click_input`, SendInput à des coordonnées écran) — nécessaire
    pour les boutons de `FolderManagerDialog` (« Re-scanner », « Retirer »,
    « Fermer », et la `QMessageBox`/confirmation qui en découlent) : confirmé
    empiriquement (instrumentation temporaire côté appli, cf. session du
    2026-07-21) que `click_input()` n'y déclenche jamais le signal `clicked()`
    de Qt — ni exception, ni log, le bouton retrouvé par `find_dialog_button`
    a pourtant un rectangle et un état (`is_enabled`/`is_visible`) valides.
    `invoke()` (pattern `IUIAutomation::Invoke`, indépendant du focus/
    foreground OS) fonctionne à tir sûr sur ce même bouton. Cause probable :
    `FolderManagerDialog` s'ouvre de façon synchrone dans le slot d'une
    `QAction` de menu (`click_menu_item`), sans que la fenêtre native ait eu
    le temps de devenir réellement la fenêtre au premier plan avant que le
    clic simulé n'atteigne l'écran. Partout ailleurs dans la suite,
    `click_input()` reste fiable (menus, listes, vignettes, autres
    `QMessageBox` parentées directement à `MainWindow`) : ne migrer que ce qui
    est prouvé fragile plutôt que remplacer `click_input()` partout par
    précaution.

    Par défaut (`wait_gone=True`), attend après l'invocation que CE bouton
    précis (l'instance retournée par `find_dialog_button`, pas une nouvelle
    recherche par libellé) devienne invisible (dialogue fermé) avant de
    rendre la main — sans ça, un enchaînement `invoke_button(..., "OK")` puis
    `invoke_button(..., "Fermer")` peut invoquer « Fermer » sur la boîte de
    dialogue parente (`FolderManagerDialog`) alors que la `QMessageBox`
    enfant n'a pas fini de se fermer : comme `.invoke()` est indépendant du
    focus/z-order OS, il ne passe pas par le filtrage de modalité de Qt
    (`QApplication::notify`) et peut donc réussir à fermer le parent
    *pendant* que la boucle d'événements imbriquée de l'enfant tourne encore
    — orphelinant cette `QMessageBox` : elle reste affichée et modale au
    niveau OS (elle continue de capter tous les clics souris, d'où des sons
    système de « bip » sur les tentatives de clic suivantes en dehors
    d'elle), alors que son parent logique a déjà disparu. Observé en direct
    (popup bloquée à l'écran + bips système) sur `test_folder_management.py`
    avant ce correctif.
    Vérifier CETTE instance précise plutôt que refaire une recherche par
    libellé est nécessaire : des libellés génériques comme « Fermer »/« OK »
    coïncident avec des contrôles toujours présents ailleurs (ex. le bouton
    natif « Fermer » de la barre de titre de `MainWindow` elle-même) — une
    recherche par libellé après coup donne un faux « toujours ouvert »
    permanent (confirmé empiriquement : `window.descendants(...)` remontait
    `['Réduire', 'Agrandir', 'Fermer', ...]`, les boutons de la barre de
    titre de `MainWindow`, alors que `FolderManagerDialog` était déjà bel et
    bien fermé). Utiliser `wait_gone=False` pour les boutons dont
    l'invocation ne ferme pas de dialogue (« Re-scanner », « Retirer »)."""
    button = find_dialog_button(window, texts, exact=exact, timeout=timeout)
    button.invoke()
    if not wait_gone:
        return

    def _still_visible() -> bool:
        try:
            return bool(button.is_visible())
        except Exception:
            # Élément UIA devenu invalide/stale : le widget a disparu.
            return False

    deadline = time.monotonic() + gone_timeout
    while time.monotonic() < deadline:
        if not _still_visible():
            return
        time.sleep(0.2)
    raise LookupError(
        f"Le bouton {texts!r} est toujours visible {gone_timeout}s après invoke() "
        "— le dialogue ne s'est probablement pas fermé (modale orpheline ?)"
    )


def find_checkbox(window, text: str, *, timeout: float = 10.0):
    """Cherche une `QCheckBox` parmi les descendants de `window` dont le texte
    contient `text` — analogue à `find_dialog_button` mais filtré sur
    `control_type="CheckBox"` (ex. « Fonctions avancées… », « Fonctions très
    avancées… » dans le dialogue Luminosité, ou « Ne plus demander » dans la
    confirmation de suppression)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for checkbox in window.descendants(control_type="CheckBox"):
                if text in checkbox.window_text():
                    return checkbox
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Case à cocher contenant {text!r} introuvable après {timeout}s ({last_exc})")


def click_yes(window) -> None:
    find_dialog_button(window, ["Oui", "Yes", "&Oui", "&Yes"]).click_input()


def click_no(window) -> None:
    find_dialog_button(window, ["Non", "No", "&Non", "&No"]).click_input()


def click_list_item(window, text: str, *, exact: bool = True, timeout: float = 10.0):
    """Clique un élément d'une QListWidget (ex. la liste Albums de la sidebar :
    Chronologie/Favoris/Vidéos/Par nom de fichier, ou un album nommé) — les
    `QListWidgetItem` se mappent sur `control_type="ListItem"` côté UIA."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for item in window.descendants(control_type="ListItem"):
                label = item.window_text()
                if (label == text) if exact else (text in label):
                    item.click_input()
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Élément de liste {text!r} introuvable après {timeout}s ({last_exc})")


def click_popup_list_item(class_name: str, text: str, *, exact: bool = False, timeout: float = 10.0) -> None:
    """Clique un élément d'une `QListWidget` hébergée dans une fenêtre top-level
    séparée de la fenêtre principale (ex. `_DuplicatesPopup`, widget `Qt.Popup`)
    — même piège que le `QMenu` contextuel documenté dans
    `click_context_menu_item` : confirmé empiriquement (probe
    `Desktop(backend="uia").windows()` pendant que la popup était ouverte)
    qu'elle est une fenêtre top-level distincte (`class_name="_DuplicatesPopup"`),
    malgré un nom accessible partagé "PixelPhotoManager" — ses `ListItem` ne
    sont donc jamais trouvés via `window.descendants(...)` (`click_list_item`),
    seulement via le Desktop."""
    from pywinauto import Desktop

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for w in Desktop(backend="uia").windows(class_name=class_name):
                for item in w.descendants(control_type="ListItem"):
                    label = item.window_text()
                    if (label == text) if exact else (text in label):
                        item.click_input()
                        return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(
        f"Élément de liste {text!r} introuvable dans la fenêtre {class_name!r} après {timeout}s ({last_exc})"
    )


def right_click_element(element) -> None:
    """Ouvre un menu contextuel Qt réel par clic droit (SendInput) — à la
    différence du double-clic (cf. `double_click_element`), un simple clic
    droit passe sans souci par `right_click_input()` de pywinauto."""
    element.right_click_input()


def click_context_menu_item(window, text: str, *, exact: bool = False, timeout: float = 10.0) -> None:
    """Clique un élément d'un `QMenu` contextuel déjà ouvert (typiquement après
    `right_click_element` sur une vignette).

    Confirmé empiriquement (probe `Desktop(backend="uia").windows()` pendant
    qu'un menu contextuel était ouvert) : un `QMenu` popup Qt est une fenêtre
    top-level **séparée** de la fenêtre principale (même s'il partage son titre
    UIA "PixelPhotoManager", cf. `_connect_main_window` ci-dessus), PAS un
    descendant de `window` — chercher ses `MenuItem` via `window.descendants(...)`
    ne trouve donc jamais que la barre de menus permanente (Fichier/Affichage/…),
    jamais les items d'un popup contextuel, même quand celui-ci s'est bien
    ouvert. Il faut retrouver la fenêtre `QMenu` elle-même via le Desktop."""
    from pywinauto import Desktop

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for w in Desktop(backend="uia").windows(class_name="QMenu"):
                for item in w.descendants(control_type="MenuItem"):
                    label = item.window_text()
                    if (label == text) if exact else (text in label):
                        item.click_input()
                        return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Élément de menu contextuel {text!r} introuvable après {timeout}s ({last_exc})")


def right_click_and_click_context_menu_item(
    get_element, window, text: str, *, exact: bool = False, timeout: float = 20.0,
    attempt_timeout: float = 3.0,
) -> None:
    """Combine `right_click_element` + `click_context_menu_item` en boucle de
    retenue du clic droit lui-même, pas seulement de la recherche du menu.

    Observé empiriquement sur l'arbre de dossiers de la sidebar
    (`test_folder_management.py`) : un clic droit isolé peut occasionnellement
    n'ouvrir aucun `QMenu` (rien trouvé même après plusieurs secondes de
    scrutation), en particulier juste après qu'une opération de fond (indexation
    visages, clustering) vient de se terminer et que la boucle d'événements Qt
    rattrape une rafale de signaux — le clic droit "se perd" plutôt que d'être
    simplement retardé. `get_element` doit renvoyer une référence *fraîche* de
    l'élément à chaque appel (p. ex. `lambda: _find_tree_item(window, name)`),
    pas un élément déjà résolu, pour re-cibler correctement si l'arbre a été
    reconstruit (`QTreeWidget.clear()` + repeuplement) entre deux tentatives."""
    from pywinauto import Desktop

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        element = get_element()
        element.right_click_input()
        attempt_deadline = time.monotonic() + min(attempt_timeout, deadline - time.monotonic())
        while time.monotonic() < attempt_deadline:
            try:
                for w in Desktop(backend="uia").windows(class_name="QMenu"):
                    for item in w.descendants(control_type="MenuItem"):
                        label = item.window_text()
                        if (label == text) if exact else (text in label):
                            item.click_input()
                            return
            except Exception as exc:
                last_exc = exc
            time.sleep(0.3)
    raise LookupError(f"Élément de menu contextuel {text!r} introuvable après {timeout}s ({last_exc})")


def type_into_sidebar_filter(window, text: str, *, timeout: float = 10.0) -> None:
    """Tape du texte dans le champ de filtre de la sidebar (`Sidebar._filter_box`,
    QLineEdit sans texte par défaut ni nom accessible — placeholder uniquement,
    cf. sidebar.py:273-277). Sur l'écran principal (grille, ni visionneuse ni
    dialogue ouverts), c'est le seul contrôle `Edit` présent, donc identifiable
    sans ambiguïté par élimination plutôt que par contenu (vide par défaut,
    contrairement au champ de destination d'export)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            edits = window.descendants(control_type="Edit")
            if len(edits) == 1:
                edits[0].set_edit_text(text)
                return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(
        f"Champ de filtre de la sidebar introuvable (un seul 'Edit' attendu) après {timeout}s ({last_exc})"
    )


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


def find_by_accessible_name(window, name: str, *, timeout: float = 15.0):
    """Retrouve un élément UIA par son nom accessible exact, sans filtrer sur
    `control_type` — contrairement à `find_thumbnail` (toujours `"Group"` pour
    un `QWidget` nommé), le rôle UIA d'un `QFrame` nommé (ex. `_DuplicateCard`,
    `dupgroup::<id>`) n'est pas garanti identique. `descendants()` reste rapide
    ici (arbre bien plus petit que la grille de vignettes virtualisée)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for el in window.descendants():
                if el.window_text() == name:
                    return el.wrapper_object() if hasattr(el, "wrapper_object") else el
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Élément de nom accessible {name!r} introuvable après {timeout}s ({last_exc})")


def wait_for_duplicate_detection(window, catalog_db, pairs, *, timeout: float = 90.0) -> None:
    """Attend que la détection continue de doublons ait assigné un
    `duplicate_group_id` au premier membre de chaque paire de `pairs` (liste de
    tuples `(photo_a, photo_b)`) ; si l'auto-détection (déclenchée après le scan,
    cf. CLAUDE.md « Détection de doublons — continue et incrémentale ») tarde,
    repli via Outils > État des doublons… > Vérifier maintenant (retenté deux
    fois, l'arbre UIA peut être lent à se peupler sous charge). Partagé par
    `test_duplicate_detection.py` et `test_duplicates_ui.py`."""
    def _done() -> bool:
        return all(
            query_one(
                catalog_db, "SELECT duplicate_group_id FROM photos WHERE path=?", (str(pair[0]),)
            ) is not None
            for pair in pairs
        )

    try:
        wait_for_condition(_done, timeout=timeout, message="")
    except TimeoutError:
        for attempt in range(2):
            try:
                window.set_focus()
                click_menu_item(window, "Outils", "État des doublons…")
                find_dialog_button(window, ["Vérifier maintenant"], timeout=15.0).click_input()
                time.sleep(0.5)
                find_dialog_button(window, ["Fermer"], timeout=15.0).click_input()
                break
            except LookupError:
                if attempt == 1:
                    raise
                time.sleep(2.0)

    wait_for_condition(
        _done, timeout=timeout + 30.0,
        message="la détection de doublons ne s'est pas terminée (aucun group_id assigné)",
    )


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


def double_click_element(element, *, gap_s: float = 0.10) -> None:
    """Double-clic réel sur un élément UIA, par SendInput brut.

    Ne PAS utiliser `element.double_click_input()` : pywinauto envoie ses deux
    clics à ~1 ms d'écart, trop rapprochés pour que Qt 6.11 synthétise un
    QEvent.MouseButtonDblClick (les deux arrivent comme deux Press simples) ;
    et deux `click_input()` successifs retombent au-delà des 500 ms de la
    fenêtre système à cause des Timings internes de pywinauto. Constaté
    empiriquement le 2026-07-20 : fenêtre valide ≈ 20-500 ms, d'où ce helper
    à ~100 ms."""
    import ctypes

    user32 = ctypes.windll.user32
    r = element.rectangle()
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    user32.SetCursorPos(cx, cy)
    time.sleep(0.08)

    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    for i in range(2):
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        if i == 0:
            time.sleep(gap_s)


def open_photo_in_viewer(window, photo_path, *, attempts: int = 4) -> None:
    """Ouvre une photo dans la visionneuse par double-clic sur sa vignette,
    avec vérification et re-tentative : pendant/juste après le scan initial la
    grille peut se réordonner entre la localisation de la vignette et le clic
    (cellules réassignées), le double-clic part alors dans le vide ou sur une
    autre cellule. Le marqueur de succès initial est le bouton « 1:1 » de la
    barre de la visionneuse (absent de la grille) — mais ça ne suffit pas :
    confirmé empiriquement (test_duplicates_ui.py, dump de
    `PhotoViewer._update_dup_badge`) qu'un double-clic peut ouvrir la
    visionneuse avec succès tout en affichant une AUTRE photo que celle visée
    (la grille avait réassigné entre-temps la cellule à la position cliquée,
    même piège de réordonnancement que documenté ci-dessus, mais qui aboutit
    ici sur un clic « réussi » du point de vue du seul marqueur « 1:1 »). On
    vérifie donc en plus que `PhotoViewer._lbl_name` (le `QLabel` du chemin
    complet, cf. photo_viewer.py:388) correspond bien au chemin demandé avant
    de considérer l'ouverture comme réussie."""
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            # premier plan obligatoire : le double-clic brut (SendInput) part
            # à une position écran — si une autre fenêtre recouvre l'appli,
            # c'est elle qui reçoit les clics.
            window.set_focus()
        except Exception:
            pass
        thumb = find_thumbnail(window, str(photo_path), timeout=30.0)
        double_click_element(thumb)
        try:
            find_dialog_button(window, ["1:1"], exact=True, timeout=4.0)
            wait_for_condition(
                lambda: any(
                    t.window_text() == str(photo_path)
                    for t in window.descendants(control_type="Text")
                ),
                timeout=4.0,
                message=f"la visionneuse affiche une autre photo que {photo_path}",
            )
            return
        except (LookupError, TimeoutError) as exc:
            last_exc = exc
            # La visionneuse peut être ouverte sur la mauvaise photo : la
            # refermer avant de retenter, sinon le prochain double-clic sur la
            # grille échoue silencieusement (la visionneuse reste affichée).
            try:
                find_dialog_button(window, ["✕"], exact=True, timeout=2.0).click_input()
            except LookupError:
                pass
            time.sleep(1.0)
    raise LookupError(
        f"La visionneuse ne s'est pas ouverte sur {photo_path} "
        f"après {attempts} double-clics ({last_exc})"
    )


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
