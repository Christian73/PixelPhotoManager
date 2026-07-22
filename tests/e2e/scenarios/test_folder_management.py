# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : cycle de vie complet d'un dossier surveillé — arbre
de la sidebar (créer/renommer/supprimer un sous-dossier) et
`FolderManagerDialog` (re-scanner, retirer). Un seul lancement d'application,
étapes séquentielles :

1. `Outils › Dossiers…` : bouton « ⟳ Re-scanner » sur le dossier racine. Pour
   donner un effet observable réel à ce re-scan forcé (pas seulement « l'appli
   ne plante pas ») plutôt qu'une simple absence de crash, un fichier image
   est copié à la main (pas de glissé-déposer simulé — simple `shutil.copy`,
   ce n'est pas un geste UI) dans un sous-dossier fraîchement créé AVANT ce
   re-scan : le re-scan forcé du dossier racine (récursif) est ce qui le fait
   apparaître au catalogue.

2. Arbre de la sidebar, menu contextuel d'un dossier (`sidebar.py:705-727`) :
   - « Créer un sous-dossier… » (`QInputDialog`) -> dossier créé sur disque.
   - « Renommer… » (`QInputDialog` pré-rempli) -> `MainWindow._on_folder_moved`
     (main_window.py:1487) appelle `Catalog.update_paths_prefix` ET
     `FaceDatabase.update_paths_prefix` : vérifié ici sur les deux tables
     (`photos.path`/`photos.directory` ET `indexed_photos.photo_path`), pas
     seulement côté catalogue — c'est le cas à plus forte valeur du dossier
     de dossiers, cf. le plan.
   - « Effacer le dossier… » : SEULE confirmation de suppression de
     l'application dont le bouton « Oui » est retexturé en « Supprimer »
     (`sidebar.py:799`, `box.button(QMessageBox.Yes).setText("Supprimer")`) —
     `click_yes()` ne le trouverait pas, d'où `find_dialog_button(...,
     ["Supprimer"], exact=True)` utilisé directement ici.
   - « Déplacer vers… » n'est PAS automatisé : `QFileDialog.getExistingDirectory`
     natif (sélecteur Shell Windows, pas une `QDialog` de l'appli), même écart
     déjà documenté pour « Ajouter un dossier… »/« Enregistrer à un autre
     emplacement… » dans les autres scénarios de ce dossier.

3. `FolderManagerDialog` › « Retirer » sur le dossier racine (dernier, car
   destructeur pour le reste du catalogue) : vérifie le contrat inverse de
   « Effacer le dossier… » — les fichiers restent intacts sur le disque, seules
   les lignes du catalogue disparaissent (`MainWindow._on_folder_removed`,
   main_window.py:1406, via `_purge_catalog_for_folder`)."""
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
    """Repère un `QTreeWidgetItem` de l'arbre de dossiers de la sidebar par
    son texte — aucun helper existant ne cible `control_type="TreeItem"`
    (`click_list_item` ne gère que les `QListWidget`).

    `exact=False` par défaut : `Sidebar.refresh_folders`/`_populate_subfolders`
    (sidebar.py) suffixent systématiquement le libellé d'un dossier avec son
    nombre de photos dès que `get_recursive_photo_counts` a tourné une fois
    (0 inclus, jamais `None` — catalog.py::get_recursive_photo_counts), donc
    le libellé n'est jamais le nom de dossier nu mais toujours "nom (N)"."""
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
    """Repère le `QLineEdit` d'un `QInputDialog` par proximité verticale avec
    son libellé (`control_type="Text"`) — même principe que
    `_find_edit_near_radio` dans test_save_options_and_settings.py, adapté ici
    à un `QLabel` plutôt qu'un `QRadioButton` : les `QMessageBox`/`QInputDialog`
    natifs ne sont pas des fenêtres UIA top-level distinctes (ce sont des
    descendants de la fenêtre principale, cf. conftest.py::find_dialog_button),
    donc plusieurs `Edit` (champ de filtre de la sidebar + champ du dialogue)
    coexistent dans l'arbre pendant que le dialogue est ouvert."""
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

    # ---- Déplier la racine (nécessaire pour que le nouveau sous-dossier soit
    #      peuplé après le prochain refresh_folders, cf. docstring du module) ----
    root_item = _find_tree_item(window, root_name, timeout=15.0)
    double_click_element(root_item)

    # ---- 2a. Créer un sous-dossier ----
    right_click_and_click_context_menu_item(
        lambda: _find_tree_item(window, root_name, timeout=15.0),
        window, "Créer un sous-dossier…", exact=True,
    )
    edit_new = _find_edit_near_text(window, "Nom du sous-dossier", timeout=10.0)
    edit_new.set_edit_text(_SUBFOLDER_NAME)
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    subfolder = root / _SUBFOLDER_NAME
    wait_for_condition(
        lambda: subfolder.is_dir(), timeout=15.0,
        message="le sous-dossier n'a pas été créé sur le disque",
    )

    # ---- Placer un fichier réel dans le nouveau sous-dossier (simple copie
    #      disque, pas un geste UI) pour donner un effet observable au re-scan ----
    extra_photo = subfolder / _EXTRA_PHOTO_NAME
    shutil.copy(manifest.control_photos[0], extra_photo)

    # ---- 1. Outils › Dossiers… : re-scan forcé de la racine ----
    # `invoke_button` (pas `find_dialog_button(...).click_input()`) pour tous
    # les boutons de FolderManagerDialog — cf. sa docstring dans conftest.py.
    click_menu_item(window, "Outils", "Dossiers…")
    # wait_gone=False : « Re-scanner » ne ferme rien lui-même (le dialogue
    # reste ouvert, seule la QMessageBox "Re-scan lancé" apparaît par-dessus).
    invoke_button(window, ["Re-scanner"], exact=False, timeout=10.0, wait_gone=False)
    invoke_button(window, ["OK"], exact=True, timeout=10.0)
    invoke_button(window, ["Fermer"], exact=True, timeout=10.0)

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

    # ---- 2b. Renommer le sous-dossier : vérifie catalog ET faces ----
    right_click_and_click_context_menu_item(
        lambda: _find_tree_item(window, _SUBFOLDER_NAME, timeout=15.0),
        window, "Renommer…", exact=True,
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

    # ---- 2c. Effacer le dossier (destructeur, bouton "Supprimer") ----
    right_click_and_click_context_menu_item(
        lambda: _find_tree_item(window, _RENAMED_NAME, timeout=15.0),
        window, "Effacer le dossier…", exact=True,
    )
    find_dialog_button(window, ["Supprimer"], exact=True, timeout=10.0).click_input()

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

    # ---- 3. FolderManagerDialog › Retirer sur la racine (dernier : purge tout) ----
    click_menu_item(window, "Outils", "Dossiers…")
    # wait_gone=False : « Retirer » ouvre une confirmation par-dessus sans se
    # fermer lui-même (même raison que « Re-scanner » ci-dessus).
    invoke_button(window, ["Retirer"], exact=True, timeout=10.0, wait_gone=False)
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
    invoke_button(window, ["Fermer"], exact=True, timeout=10.0)
    assert window.exists()
