# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Fixtures and utilities shared by the end-to-end scenarios (Layer 3).

These tests drive the real application (`main.py`) in a subprocess, with
an isolated `%LOCALAPPDATA%` (see `tools/test_env/launch_isolated.py`), against
a reproducible synthetic library (see
`tools/test_env/generate_library.py`). No test of this folder touches the
real profile of the user.

`pywinauto` is an optional dependency (`requirements-test-e2e.txt`,
Windows-only): if it is absent, the scenarios of `scenarios/` are
removed from the collection (`collect_ignore_glob` below) so that
`pytest tests/` (Layers 1+2) keeps working without it."""
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
    """Builds the synthetic library only once per session
    (procedural generation: a few seconds, ORB shapes included)."""
    root = tmp_path_factory.mktemp("ppm_e2e_library_master")
    return build_library(root)


def _build_isolated_app(tmp_path: Path, synthetic_library_master: LibraryManifest, *,
                         extra_photo_files: tuple[Path, ...] = (),
                         extra_config: dict | None = None):
    """Copies the master library into a photo folder dedicated to the test
    (+ any additional photo files, e.g. face fixtures), starts
    the application with an isolated `%LOCALAPPDATA%` pointing at it, and guarantees
    the termination of the process even if the test fails during setup or
    execution. Shared by `isolated_app` and `isolated_app_with_faces`."""
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
    """Standard isolated instance, with no face photo. Accepts a
    non-default starting configuration through an indirect parametrisation:
    `@pytest.mark.parametrize("isolated_app", [{"ui.delete_no_confirm": False}], indirect=True)`."""
    extra_config = getattr(request, "param", None)
    yield from _build_isolated_app(tmp_path, synthetic_library_master, extra_config=extra_config)


_FACES_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "faces"


@pytest.fixture
def isolated_app_with_faces(tmp_path, synthetic_library_master):
    """Like `isolated_app`, with in addition the face photos of
    `tests/e2e/fixtures/faces/` (3 solo "Personne A", 3 solo "Personne B",
    1 photo of both together) copied into the watched photo folder,
    for the face identification/merge/reset scenarios."""
    face_files = sorted(_FACES_FIXTURES_DIR.glob("*.jpg"))
    if not face_files:
        pytest.skip(f"Aucune fixture visage trouvée dans {_FACES_FIXTURES_DIR}")
    yield from _build_isolated_app(tmp_path, synthetic_library_master, extra_photo_files=tuple(face_files))


def _graceful_close(window, app, timeout: float = 20.0) -> None:
    """Attempts a clean closing of the application before the fallback
    terminate(). Indispensable when the application runs under coverage
    (PPM_E2E_COVERAGE=1, cf. launch_isolated): TerminateProcess never
    runs the atexit hook that writes the coverage data. Best-effort:
    confirms the possible "analysis in progress" warning then waits for the end
    of the process; on failure, terminate() takes over."""
    if window is None:
        return
    try:
        window.close()
    except Exception:
        return
    try:
        # closeEvent may ask for confirmation (e.g. duplicate detection in
        # progress) - click Yes if the button appears, otherwise carry on.
        try:
            find_dialog_button(window, ["Oui", "Yes", "&Oui", "&Yes"], timeout=3.0).click_input()
        except Exception:
            pass
        app.process.wait(timeout=timeout)
    except Exception:
        pass


def _find_window_pid(launcher_pid: int, timeout: float = 30.0) -> int:
    """Resolves the PID owning the main UIA window.

    Confirmed empirically on this machine: `subprocess.Popen([python_exe,
    "main.py"], ...).pid` (= `launcher_pid`) IS NOT the PID owning
    the window - `.venv\\Scripts\\python.exe` restarts a real child process
    (`python.exe`) which, for its part, owns the "PixelPhotoManager" window. Search
    by title among `launcher_pid` AND its descendants (`psutil`), never by
    `launcher_pid` alone (cf. the initial bug: systematic timeout while
    the application was running correctly - 18 desktop windows found, 0 for
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
            # class_name="MainWindow" is necessary on top of the title: a context
            # QMenu opened later in the test also exposes
            # Name="PixelPhotoManager" through UIA (top-level Qt popup, same
            # control_type="Window", but class_name="QMenu" - confirmed by a
            # `Desktop(backend="uia").windows()` probe while a menu was open),
            # which makes `app.window(title="PixelPhotoManager")` alone ambiguous
            # (ElementAmbiguousError, 2 elements) as soon as a context menu is
            # displayed.
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
    """Opens a menu of the main menu bar then clicks one of its
    items - replaces `window.menu_select("A->B")`, which fails on this
    application with an `AttributeError` (`menu_select()` looks for a
    `children(control_type="MenuBar")` or a `descendants(control_type="Menu")`
    directly on `window`, whereas here the `MenuBar` is indeed present but
    its drop-down menus (`Tools`, etc.) are populated lazily by Qt:
    the `MenuItem` objects of the submenu (e.g. "Detect the duplicates...") only exist in
    the UIA tree once the top-level menu has really been opened by a
    click, never before). So it targets the top-level `MenuItem` by text,
    clicks to open it, then looks for the wanted `MenuItem` among the
    newly appeared descendants."""
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
    """Looks for a button among the descendants of `window` (the native
    QMessageBox objects are NOT distinct top-level UIA windows - they are
    descendants of the main window) whose text matches one of the
    variants of `texts` (useful for localised labels: e.g.
    `["Oui", "Yes"]`). Retries until `timeout`: the dialog may appear
    with a slight delay after the action that triggers it."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    last_labels: list[str] = []
    while time.monotonic() < deadline:
        try:
            last_labels = []
            for button in window.descendants(control_type="Button"):
                label = button.window_text()
                last_labels.append(label)
                for text in texts:
                    if (label == text) if exact else (text.lower() in label.lower()):
                        return button
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(
        f"Aucun bouton correspondant à {texts!r} trouvé après {timeout}s ({last_exc}) — "
        f"boutons vus au dernier essai : {last_labels!r}"
    )


def invoke_button(
    window, texts: list[str], exact: bool = False, *, timeout: float = 10.0,
    wait_gone: bool = True, gone_timeout: float = 10.0,
) -> None:
    """Clicks a button through the UIA Invoke pattern rather than a simulated
    mouse click (`click_input`, SendInput at screen coordinates) - necessary
    for the buttons of `FolderManagerDialog` ("Rescan", "Remove",
    "Close", and the `QMessageBox`/confirmation that follow from them): confirmed
    empirically (temporary instrumentation on the application side, cf. the session of
    2026-07-21) that `click_input()` never triggers the `clicked()` signal
    of Qt there - no exception, no log, yet the button found by `find_dialog_button`
    does have a valid rectangle and state (`is_enabled`/`is_visible`).
    `invoke()` (the `IUIAutomation::Invoke` pattern, independent of the OS focus/
    foreground) works every time on that same button. Probable cause:
    `FolderManagerDialog` opens synchronously in the slot of a
    menu `QAction` (`click_menu_item`), without the native window having had
    the time to really become the foreground window before the
    simulated click reaches the screen. Everywhere else in the suite,
    `click_input()` stays reliable (menus, lists, thumbnails, other
    `QMessageBox` parented directly to `MainWindow`): only migrate what
    is proven fragile rather than replacing `click_input()` everywhere as a
    precaution.

    By default (`wait_gone=True`), waits after the invocation for THIS
    precise button (the instance returned by `find_dialog_button`, not a new
    search by label) to become invisible (dialog closed) before
    handing back - without that, a sequence `invoke_button(..., "OK")` then
    `invoke_button(..., "Close")` may invoke "Close" on the parent
    dialog box (`FolderManagerDialog`) while the child `QMessageBox`
    has not finished closing: since `.invoke()` is independent of the
    OS focus/z-order, it does not go through the modality filtering of Qt
    (`QApplication::notify`) and may therefore succeed in closing the parent
    *while* the nested event loop of the child is still running
    - orphaning that `QMessageBox`: it stays displayed and modal at the
    OS level (it keeps capturing every mouse click, hence system
    "beep" sounds on the following click attempts outside
    it), while its logical parent has already disappeared. Observed live
    (popup stuck on screen + system beeps) on `test_folder_management.py`
    before this fix.
    Checking THAT precise instance rather than doing a new search by
    label is necessary: generic labels such as "Close"/"OK"
    coincide with controls always present elsewhere (e.g. the native
    "Close" button of the title bar of `MainWindow` itself) - a
    search by label after the fact gives a permanent false "still open"
    (confirmed empirically: `window.descendants(...)` returned
    `['Minimise', 'Maximise', 'Close', ...]`, the buttons of the title
    bar of `MainWindow`, while `FolderManagerDialog` was already well and
    truly closed). Use `wait_gone=False` for the buttons whose
    invocation does not close a dialog ("Rescan", "Remove")."""
    button = find_dialog_button(window, texts, exact=exact, timeout=timeout)
    button.invoke()
    if not wait_gone:
        return

    def _still_visible() -> bool:
        try:
            return bool(button.is_visible())
        except Exception:
            # UIA element gone invalid/stale: the widget has disappeared.
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
    """Looks for a `QCheckBox` among the descendants of `window` whose text
    contains `text` - analogous to `find_dialog_button` but filtered on
    `control_type="CheckBox"` (e.g. "Advanced functions...", "Very
    advanced functions..." in the Brightness dialog, or "Do not ask again" in the
    deletion confirmation)."""
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


def find_radio_button(window, text: str, *, timeout: float = 10.0):
    """Looks for a `QRadioButton` among the descendants of `window` whose
    text contains `text` - analogous to `find_checkbox` but filtered on
    `control_type="RadioButton"` (e.g. choosing an existing person in
    `_AssignDialog`/`MergePersonsDialog`/`_ResetFacesDialog`, whose
    labels include a variable photo count, hence a search by
    substring rather than by exact equality)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for radio in window.descendants(control_type="RadioButton"):
                if text in radio.window_text():
                    return radio
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Bouton radio contenant {text!r} introuvable après {timeout}s ({last_exc})")


def click_yes(window) -> None:
    find_dialog_button(window, ["Oui", "Yes", "&Oui", "&Yes"]).click_input()


def click_no(window) -> None:
    find_dialog_button(window, ["Non", "No", "&Non", "&No"]).click_input()


def click_list_item(window, text: str, *, exact: bool = True, timeout: float = 10.0):
    """Clicks an item of a QListWidget (e.g. the Albums list of the sidebar:
    Chronology/Favorites/Videos/By filename, or a named album) - the
    `QListWidgetItem` objects map onto `control_type="ListItem"` on the UIA side."""
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
    """Clicks an item of a `QListWidget` hosted in a top-level window
    separate from the main window (e.g. `_DuplicatesPopup`, a `Qt.Popup` widget)
    - the same trap as the context `QMenu` documented in
    `click_context_menu_item`: confirmed empirically (a
    `Desktop(backend="uia").windows()` probe while the popup was open)
    that it is a distinct top-level window (`class_name="_DuplicatesPopup"`),
    despite a shared accessible name "PixelPhotoManager" - its `ListItem`
    objects are therefore never found through `window.descendants(...)` (`click_list_item`),
    only through the Desktop."""
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
    """Opens a real Qt context menu by right click (SendInput) - unlike
    the double click (cf. `double_click_element`), a simple right
    click goes through `right_click_input()` of pywinauto without trouble."""
    element.right_click_input()


def click_context_menu_item(window, text: str, *, exact: bool = False, timeout: float = 10.0) -> None:
    """Clicks an item of a context `QMenu` already open (typically after
    `right_click_element` on a thumbnail).

    Confirmed empirically (a `Desktop(backend="uia").windows()` probe while
    a context menu was open): a Qt `QMenu` popup is a top-level window
    **separate** from the main window (even if it shares its
    UIA title "PixelPhotoManager", cf. `_connect_main_window` above), NOT a
    descendant of `window` - looking for its `MenuItem` objects through `window.descendants(...)`
    therefore only ever finds the permanent menu bar (File/View/...),
    never the items of a context popup, even when that one did
    open. The `QMenu` window itself must be found through the Desktop."""
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
    """Combines `right_click_element` + `click_context_menu_item` in a retry
    loop of the right click itself, not only of the search for the menu.

    Observed empirically on the folder tree of the sidebar
    (`test_folder_management.py`): an isolated right click may occasionally
    open no `QMenu` at all (nothing found even after several seconds of
    scanning), in particular just after a background operation (face
    indexing, clustering) has just finished and the Qt event loop
    catches up with a burst of signals - the right click "gets lost" rather than being
    simply delayed. `get_element` must return a *fresh* reference of
    the element at every call (e.g. `lambda: _find_tree_item(window, name)`),
    not an already resolved element, so as to re-target correctly if the tree has been
    rebuilt (`QTreeWidget.clear()` + repopulation) between two attempts."""
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
    """Types text into the filter field of the sidebar (`Sidebar._filter_box`,
    a QLineEdit with no default text nor accessible name - placeholder only,
    cf. sidebar.py:273-277). On the main screen (grid, neither viewer nor
    dialog open), it is the only `Edit` control present, hence identifiable
    unambiguously by elimination rather than by content (empty by default,
    unlike the export destination field)."""
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
    """Waits for a line containing `pattern` to appear in `log_path` after
    `since_offset` (bytes). Returns the line found. Raises `TimeoutError`
    otherwise - captures the last lines of the log in the message to make
    the diagnosis of a failing scenario easier."""
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
    """Current size of the log, to be captured before triggering an operation
    so that `wait_for_log` does not match a line of a previous run."""
    return log_path.stat().st_size if log_path.exists() else 0


def find_by_accessible_name(window, name: str, *, timeout: float = 15.0):
    """Finds a UIA element by its exact accessible name, without filtering on
    `control_type` - unlike `find_thumbnail` (always `"Group"` for
    a named `QWidget`), the UIA role of a named `QFrame` (e.g. `_DuplicateCard`,
    `dupgroup::<id>`) is not guaranteed to be identical. `descendants()` stays fast
    here (a tree far smaller than the virtualised thumbnail grid)."""
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
    """Waits for the continuous duplicate detection to have assigned a
    `duplicate_group_id` to the first member of each pair of `pairs` (a list of
    `(photo_a, photo_b)` tuples); if the auto-detection (triggered after the scan,
    cf. CLAUDE.md "Duplicate detection - continuous and incremental") is slow,
    falls back on Tools > Duplicate status... > Check now (retried twice,
    the UIA tree can be slow to populate under load). Shared by
    `test_duplicate_detection.py` and `test_duplicates_ui.py`."""
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
                click_menu_item(window, "Tools", "Duplicate status…")
                find_dialog_button(window, ["Check now"], timeout=15.0).click_input()
                time.sleep(0.5)
                find_dialog_button(window, ["Close"], timeout=15.0).click_input()
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
    """Finds the UIA element of a precise thumbnail through its accessible name
    `thumb::<path>` (cf. `ThumbnailCell._setup_ui` in thumbnail_grid.py) -
    more robust than guessing screen coordinates in the virtual grid.

    Confirmed empirically (UIA probe): a `QWidget` with `setAccessibleName`
    but with no explicit role maps through the Qt accessibility bridge onto
    `control_type="Group"`, NOT `"Pane"` - the grid only virtualises the
    cells outside `_visible_range()` (+ one screen of margin), so a photo
    must be scrolled into view for its cell (and its accessible
    name) to exist in the UIA tree."""
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


def scroll_grid_into_view(window, photo_path: str, *, max_attempts: int = 30) -> None:
    """Scrolls the grid (real wheel, `wheel_mouse_input`) until
    the thumbnail of `photo_path` exists in the UIA tree.

    Necessary after any navigation that may have left the scroll position
    far from the top of the grid (e.g. returning from `FaceClusterGrid` through its
    "<- Photos" button): `ThumbnailGrid` only materialises the cells of
    `_visible_range()` (+ one screen of margin, cf. the docstring of `find_thumbnail`)
    - a previously visible thumbnail may have gone out of view without a
    manual scroll bringing it back. Wheel rather than keyboard keys (Page
    Down/End): no reliable assumption about which widget has the keyboard focus at
    the moment of the call, whereas the wheel only follows the position of
    the cursor."""
    try:
        find_thumbnail(window, photo_path, timeout=1.0)
        return
    except TimeoutError:
        pass
    rect = window.rectangle()
    # the coords of wheel_mouse_input()/click_input() are relative to the client of
    # `window` (passed to client_to_screen() internally): NOT absolute screen
    # coordinates, despite `window.rectangle()` which, for its part, returns
    # screen coordinates.
    coords = (int((rect.right - rect.left) * 0.65), int((rect.bottom - rect.top) * 0.5))
    for _ in range(max_attempts):
        window.wheel_mouse_input(coords=coords, wheel_dist=-3)
        time.sleep(0.3)
        try:
            find_thumbnail(window, photo_path, timeout=1.0)
            return
        except TimeoutError:
            continue
    raise TimeoutError(
        f"Vignette 'thumb::{photo_path}' introuvable après {max_attempts} "
        "molettes de défilement"
    )


def double_click_element(element, *, gap_s: float = 0.10) -> None:
    """Real double click on a UIA element, through raw SendInput.

    Do NOT use `element.double_click_input()`: pywinauto sends its two
    clicks ~1 ms apart, too close together for Qt 6.11 to synthesise a
    QEvent.MouseButtonDblClick (both arrive as two simple Presses);
    and two successive `click_input()` fall beyond the 500 ms of the
    system window because of the internal Timings of pywinauto. Established
    empirically on 2026-07-20: valid window ~ 20-500 ms, hence this helper
    at ~100 ms."""
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
    """Opens a photo in the viewer through the "Open" context menu
    of its thumbnail (a single right click), NOT a double click.

    A double click requires two distinct clicks at the same screen position,
    ~100 ms apart (cf. `double_click_element`): if the grid
    reorders between the two - during/just after the initial scan, after a
    return from `FaceClusterGrid`, or even under simple high background load
    that delays the processing of the 2nd click - that one may land on
    ANOTHER cell than the first. Confirmed empirically (test_duplicates_ui.py,
    then deterministically and reproducibly in
    test_faces_identify_and_reset.py, including with a position stability
    wait added before the click): the double click may
    "succeed" ("1:1" button present) while displaying a photo
    different from the intended one - not a rare case, a stable failure mode.
    The "Open" context menu (`thumbnail_grid.py::_on_right_click`,
    `menu.addAction("Open", lambda: self.photo_activated.emit(photo))`)
    only needs a single click to land on the right cell, and
    captures the clicked `PhotoInfo` in the closure of the callback as soon as
    the menu opens - no reassignment window between two clicks.
    `right_click_and_click_context_menu_item` already retries the whole sequence
    right click + menu search if needed (cf. its own docstring).
    Still checks `PhotoViewer._lbl_name` (full path displayed,
    photo_viewer.py:388) afterwards, as defence in depth."""
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            window.set_focus()
        except Exception:
            pass
        try:
            right_click_and_click_context_menu_item(
                lambda: find_thumbnail(window, str(photo_path), timeout=30.0),
                window, "Open", exact=True, timeout=10.0,
            )
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
            # The viewer may be opened on the wrong photo: close it
            # again before retrying, otherwise the next opening
            # fails silently (the viewer stays displayed).
            try:
                find_dialog_button(window, ["✕"], exact=True, timeout=2.0).click_input()
            except LookupError:
                pass
            time.sleep(1.0)
    raise LookupError(
        f"La visionneuse ne s'est pas ouverte sur {photo_path} "
        f"après {attempts} tentatives ({last_exc})"
    )


def wait_for_condition(predicate, *, timeout: float = 30.0, poll: float = 0.3,
                        message: str = "condition non remplie") -> None:
    """Waits for a function with no argument to become true - used to
    probe `catalog.db`/`edits.db` directly (real SQLite files), more
    robust than a text displayed in a custom grid painted by hand."""
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
