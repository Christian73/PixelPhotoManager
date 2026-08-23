# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: the full life cycle of a watched folder -- sidebar
tree (create/rename/delete a subfolder) and `FolderManagerDialog` (rescan,
remove). A single application launch, sequential steps:

1. `Tools > Folders…`: the "Rescan" button on the root folder. To give this
   forced rescan a real observable effect (not just "the application does not
   crash"), an image file is copied by hand (no simulated drag and drop --
   a plain `shutil.copy`, this is not a UI gesture) into a freshly created
   subfolder BEFORE that rescan: the forced (recursive) rescan of the root
   folder is what makes it appear in the catalog.

2. Sidebar tree, context menu of a folder (`sidebar.py:705-727`):
   - "Create a subfolder…" (`QInputDialog`) -> folder created on disk.
   - "Rename…" (a pre-filled `QInputDialog`) -> `MainWindow._on_folder_moved`
     (main_window.py:1487) calls `Catalog.update_paths_prefix` AND
     `FaceDatabase.update_paths_prefix`: checked here on both tables
     (`photos.path`/`photos.directory` AND `indexed_photos.photo_path`), not
     only on the catalog side -- that is the highest-value case of the folder
     manager, cf. the plan.
   - "Delete the folder…": the ONLY deletion confirmation of the application
     whose "Yes" button is relabelled "Remove"
     (`sidebar.py:799`, `box.button(QMessageBox.Yes).setText("Remove")`) --
     `click_yes()` would not find it, hence `find_dialog_button(...,
     ["Remove"], exact=True)` used directly here.
   - "Move to…" is NOT automated: a native
     `QFileDialog.getExistingDirectory` (the Windows Shell picker, not a
     `QDialog` of the application), the same gap already documented for
     "Add a folder…"/"Save to another location…" in the other scenarios of
     this folder.

3. `FolderManagerDialog` > "Remove" on the root folder (last, because it is
   destructive for the rest of the catalog): checks the contract opposite to
   "Delete the folder…" -- the files stay intact on disk, only the catalog
   rows disappear (`MainWindow._on_folder_removed`, main_window.py:1406,
   through `_purge_catalog_for_folder`)."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_menu_item,
    double_click_element,
    find_dialog_button,
    invoke_button,
    query_one,
    right_click_and_click_context_menu_item,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e

_SUBFOLDER_NAME = "sous_dossier_e2e"
_RENAMED_NAME = "sous_dossier_renomme"
_EXTRA_PHOTO_NAME = "extra_photo_e2e.jpg"


def _find_tree_item(window, text: str, *, exact: bool = False, timeout: float = 15.0):
    """Locates a `QTreeWidgetItem` of the sidebar folder tree by its text --
    no existing helper targets `control_type="TreeItem"` (`click_list_item`
    only handles `QListWidget`).

    `exact=False` by default: `Sidebar.refresh_folders`/`_populate_subfolders`
    (sidebar.py) systematically suffix the label of a folder with its number
    of photos as soon as `get_recursive_photo_counts` has run once (0 included,
    never `None` -- catalog.py::get_recursive_photo_counts), so the label is
    never the bare folder name but always "name (N)"."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for item in window.descendants(control_type="TreeItem"):
                label = item.window_text()
                if (label == text) if exact else (text in label):
                    return item
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Élément d'arbre {text!r} introuvable après {timeout}s ({last_exc})")


def _find_edit_near_text(window, text_substring: str, *, timeout: float = 10.0):
    """Locates the `QLineEdit` of a `QInputDialog` by vertical proximity with
    its label (`control_type="Text"`) -- same principle as
    `_find_edit_near_radio` in test_save_options_and_settings.py, adapted here
    to a `QLabel` rather than a `QRadioButton`: the native
    `QMessageBox`/`QInputDialog` are not distinct top-level UIA windows (they
    are descendants of the main window, cf. conftest.py::find_dialog_button),
    so several `Edit` controls (the sidebar filter field + the dialog field)
    coexist in the tree while the dialog is open."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            labels = [t for t in window.descendants(control_type="Text")
                      if text_substring in t.window_text()]
            edits = window.descendants(control_type="Edit")
            if labels and edits:
                l_rect = labels[0].rectangle()
                l_mid = (l_rect.top + l_rect.bottom) / 2
                return min(
                    edits,
                    key=lambda e: abs(((e.rectangle().top + e.rectangle().bottom) / 2) - l_mid),
                )
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"QLineEdit proche de {text_substring!r} introuvable après {timeout}s ({last_exc})")


def test_folder_management(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    faces_db = isolated_app.faces_db
    root = manifest.root
    root_name = root.name

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    # ---- Expand the root (necessary so that the new subfolder is populated
    #      after the next refresh_folders, cf. the module docstring) ----
    root_item = _find_tree_item(window, root_name, timeout=15.0)
    double_click_element(root_item)

    # ---- 2a. Create a subfolder ----
    right_click_and_click_context_menu_item(
        lambda: _find_tree_item(window, root_name, timeout=15.0),
        window, "Create a subfolder…", exact=True,
    )
    edit_new = _find_edit_near_text(window, "Nom du sous-dossier", timeout=10.0)
    edit_new.set_edit_text(_SUBFOLDER_NAME)
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    subfolder = root / _SUBFOLDER_NAME
    wait_for_condition(
        lambda: subfolder.is_dir(), timeout=15.0,
        message="le sous-dossier n'a pas été créé sur le disque",
    )

    # ---- Put a real file into the new subfolder (a plain disk copy, not a UI
    #      gesture) to give the rescan an observable effect ----
    extra_photo = subfolder / _EXTRA_PHOTO_NAME
    shutil.copy(manifest.control_photos[0], extra_photo)

    # ---- 1. Tools > Folders…: forced rescan of the root ----
    # `invoke_button` (not `find_dialog_button(...).click_input()`) for every
    # button of FolderManagerDialog -- cf. its docstring in conftest.py.
    click_menu_item(window, "Tools", "Folders…")
    # wait_gone=False: "Rescan" closes nothing by itself (the dialog stays
    # open, only the "Rescan started" QMessageBox appears on top of it).
    invoke_button(window, ["Rescan"], exact=False, timeout=10.0, wait_gone=False)
    invoke_button(window, ["OK"], exact=True, timeout=10.0)
    invoke_button(window, ["Close"], exact=True, timeout=10.0)

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(extra_photo),)
        ) == 1,
        timeout=30.0,
        message="le re-scan forcé n'a pas catalogué le fichier du nouveau sous-dossier",
    )
    wait_for_condition(
        lambda: query_one(
            faces_db, "SELECT COUNT(*) FROM indexed_photos WHERE photo_path=?", (str(extra_photo),)
        ) == 1,
        timeout=60.0,
        message="le fichier du nouveau sous-dossier n'a pas été indexé (visages)",
    )

    # ---- 2b. Rename the subfolder: checks the catalog AND the faces ----
    right_click_and_click_context_menu_item(
        lambda: _find_tree_item(window, _SUBFOLDER_NAME, timeout=15.0),
        window, "Rename…", exact=True,
    )
    edit_rename = _find_edit_near_text(window, "Nouveau nom", timeout=10.0)
    edit_rename.set_edit_text(_RENAMED_NAME)
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    renamed_folder = root / _RENAMED_NAME
    renamed_photo = renamed_folder / _EXTRA_PHOTO_NAME
    wait_for_condition(
        lambda: renamed_folder.is_dir() and not subfolder.exists(),
        timeout=15.0, message="le sous-dossier n'a pas été renommé sur le disque",
    )
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(renamed_photo),)
        ) == 1,
        timeout=15.0,
        message="Catalog.update_paths_prefix n'a pas mis à jour photos.path après renommage",
    )
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(extra_photo),)
    ) == 0, "l'ancien chemin est toujours présent au catalogue après renommage"
    wait_for_condition(
        lambda: query_one(
            faces_db, "SELECT COUNT(*) FROM indexed_photos WHERE photo_path=?", (str(renamed_photo),)
        ) == 1,
        timeout=15.0,
        message="FaceDatabase.update_paths_prefix n'a pas mis à jour indexed_photos.photo_path",
    )
    assert query_one(
        faces_db, "SELECT COUNT(*) FROM indexed_photos WHERE photo_path=?", (str(extra_photo),)
    ) == 0, "l'ancien chemin est toujours présent dans indexed_photos après renommage"

    # ---- 2c. Delete the folder (destructive, "Remove" button) ----
    right_click_and_click_context_menu_item(
        lambda: _find_tree_item(window, _RENAMED_NAME, timeout=15.0),
        window, "Delete the folder…", exact=True,
    )
    find_dialog_button(window, ["Remove"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: not renamed_folder.exists(), timeout=15.0,
        message="le dossier n'a pas été supprimé du disque",
    )
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(renamed_photo),)
        ) == 0,
        timeout=20.0,
        message="la photo du dossier supprimé est toujours présente au catalogue",
    )

    # ---- 3. FolderManagerDialog > Remove on the root (last: purges everything) ----
    click_menu_item(window, "Tools", "Folders…")
    # wait_gone=False: "Remove" opens a confirmation on top of itself without
    # closing (same reason as "Rescan" above).
    invoke_button(window, ["Remove"], exact=True, timeout=10.0, wait_gone=False)
    invoke_button(window, ["Oui", "Yes", "&Oui", "&Yes"], timeout=10.0)

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 0,
        timeout=20.0,
        message="les photos de la racine retirée sont toujours présentes au catalogue",
    )
    assert Path(manifest.control_photos[0]).exists(), (
        "le fichier a été supprimé du disque alors que « Retirer » doit "
        "seulement le retirer de la surveillance"
    )
    invoke_button(window, ["Close"], exact=True, timeout=10.0)
    assert window.exists()
