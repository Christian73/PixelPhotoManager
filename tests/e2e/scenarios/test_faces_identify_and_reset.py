# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : identification de visages, fusion de personnes,
ignorer/restaurer un visage, et réinitialisation du regroupement. Utilise
`isolated_app_with_faces` (7 photos : 3 "Personne A", 3 "Personne B", 1 photo
des deux ensemble — voir `tests/e2e/fixtures/faces/`).

`test_faces_identify_and_reset` : un seul lancement, étapes séquentielles :
1. Attente de la fin du scan + de l'indexation ArcFace + du premier
   regroupement HDBSCAN automatique.
2. Identification d'un visage isolé (nouvelle personne) depuis la
   visionneuse — `FacePanel` / `_AssignDialog` (people_panel.py:187) →
   `FaceDatabase.isolate_and_assign_face`.
3. Identification d'un groupe entier (nouvelle personne) depuis
   `FaceClusterGrid` (bouton ✓ superposé de `_ClusterCard`, nom accessible
   `facecluster::<id>` ajouté pour ce test — même principe que
   `_DuplicateCard`/`dupgroup::<id>`) → `FaceDatabase.assign_person_to_cluster`.
4. Fusion des deux personnes créées ci-dessus via le menu contextuel de la
   sidebar (« Fusionner avec… ») → `people_panel.py::MergePersonsDialog` →
   `FaceDatabase.merge_persons` + `Catalog.delete_person`.
5. Ignorer un visage (menu contextuel « Ignorer ce visage ») puis restaurer
   via `_IgnoredFacesDialog` (bouton « Restaurer » PUIS fermeture du
   dialogue — seul déclencheur réel de `unignore_face`, cf.
   `FacePanel._on_show_ignored`).
6. Réinitialisation « Réinitialiser les groupes uniquement » (rapide, non
   destructrice, RESET_CLUSTERING) — vérifie que `FaceDatabase.reset_clustering()`
   ne touche que les visages non identifiés/non épinglés : les personnes
   créées aux étapes 2-4 doivent survivre intactes.

Aucune hypothèse n'est faite sur la topologie exacte du regroupement HDBSCAN
(pas garanti reproductible sur des visages générés par IA) : chaque étape
interroge `faces.db` directement pour trouver un visage/cluster candidat
plutôt que de deviner un id ou un nombre de groupes.

`test_faces_reset_full` : lancement isolé séparé (le mode RESET_FULL est
destructeur — efface entièrement `faces`/`indexed_photos` puis relance une
indexation complète — il corromprait l'état nécessaire aux étapes 2-5
ci-dessus s'il partageait le même lancement)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_menu_item,
    find_by_accessible_name,
    find_checkbox,
    find_dialog_button,
    find_radio_button,
    open_photo_in_viewer,
    query_one,
    right_click_and_click_context_menu_item,
    scroll_grid_into_view,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e


def _query_all(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _wait_faces_ready(faces_db: Path, photo_paths: list[str], *, timeout: float = 300.0) -> None:
    """Attend la fin du scan + de l'indexation ArcFace + du premier
    regroupement HDBSCAN automatique (déclenché sans interaction dès que
    l'indexation détecte au moins un visage, cf. main_window_faces.py
    ::_on_face_indexing_finished) pour les 7 photos de la fixture."""
    placeholders = ",".join("?" * len(photo_paths))
    wait_for_condition(
        lambda: query_one(
            faces_db,
            f"SELECT COUNT(*) FROM indexed_photos WHERE photo_path IN ({placeholders})",
            tuple(photo_paths),
        ) == len(photo_paths),
        timeout=timeout,
        message="l'indexation des visages n'a pas terminé pour les 7 photos de la fixture",
    )
    wait_for_condition(
        lambda: (query_one(faces_db, "SELECT COUNT(*) FROM faces") or 0) >= 2,
        timeout=30.0,
        message="moins de 2 visages détectés au total sur les 7 photos de la fixture",
    )
    wait_for_condition(
        lambda: query_one(
            faces_db,
            "SELECT COUNT(*) FROM faces"
            " WHERE cluster_id IS NULL AND person_id IS NULL AND ignored=0",
        ) == 0,
        timeout=timeout,
        message="le regroupement HDBSCAN automatique n'a pas terminé",
    )


def _find_name_input(window):
    """Repère le `QLineEdit` "Nom de la personne…" de `_AssignDialog` par
    proximité verticale avec le radio "Créer une nouvelle personne" — même
    principe que `_find_edit_near_text` dans test_folder_management.py
    (adapté à un `QRadioButton` plutôt qu'un `QLabel`) : un `QLineEdit` vide
    n'expose pas de façon fiable son texte de substitution via UIA, et
    d'autres `Edit` (filtre de la sidebar, recherche de personnes) coexistent
    dans l'arbre pendant que le dialogue est ouvert."""
    anchor = find_radio_button(window, "Créer une nouvelle personne")
    a_rect = anchor.rectangle()
    a_mid = (a_rect.top + a_rect.bottom) / 2
    edits = window.descendants(control_type="Edit")
    return min(
        edits,
        key=lambda e: abs(((e.rectangle().top + e.rectangle().bottom) / 2) - a_mid),
    )


def _find_person_list_item(window, name_substring: str, *, timeout: float = 15.0):
    """Repère un élément de `Sidebar._persons_list` par sous-chaîne de nom
    (libellé "Nom  (n)", cf. sidebar.py::refresh_persons) — même principe que
    `_find_tree_item` dans test_folder_management.py, adapté à
    `control_type="ListItem"`."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for item in window.descendants(control_type="ListItem"):
                if name_substring in item.window_text():
                    return item
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(
        f"Élément de la liste des personnes contenant {name_substring!r} "
        f"introuvable après {timeout}s ({last_exc})"
    )


def test_faces_identify_and_reset(isolated_app_with_faces):
    window = isolated_app_with_faces.window
    catalog_db = isolated_app_with_faces.catalog_db
    faces_db = isolated_app_with_faces.faces_db
    face_photo_paths = [str(p) for p in isolated_app_with_faces.face_photos]
    assert len(face_photo_paths) == 7

    _wait_faces_ready(faces_db, face_photo_paths)

    # ---- 2. Identifier un visage isolé (nouvelle personne) ----
    solo_face_id, solo_photo_path = _query_all(
        faces_db,
        "SELECT id, photo_path FROM faces WHERE ignored=0 ORDER BY id LIMIT 1",
    )[0]

    open_photo_in_viewer(window, solo_photo_path)
    # `_act_faces_toggle` (main_window.py) est un QPushButton *checkable* : Qt
    # l'expose à UIA sous control_type="CheckBox", pas "Button" — find_dialog_button
    # (filtré sur Button) ne le trouve jamais, d'où find_checkbox ici.
    find_checkbox(window, "Faces").click_input()
    right_click_and_click_context_menu_item(
        lambda: find_by_accessible_name(window, f"faceitem::{solo_face_id}"),
        window, "Identify this person…", exact=True,
    )
    _find_name_input(window).set_edit_text("TestPersonSolo")
    find_dialog_button(window, ["OK"], exact=True).click_input()

    wait_for_condition(
        lambda: query_one(
            faces_db, "SELECT person_id FROM faces WHERE id=?", (solo_face_id,)
        ) is not None,
        timeout=20.0, message="le visage isolé n'a pas reçu de person_id",
    )
    solo_person_id = query_one(
        catalog_db, "SELECT id FROM persons WHERE name=?", ("TestPersonSolo",)
    )
    assert solo_person_id is not None, "aucune ligne persons créée pour TestPersonSolo"
    assert query_one(
        faces_db, "SELECT person_id FROM faces WHERE id=?", (solo_face_id,)
    ) == solo_person_id

    # ---- Revenir à la grille (nécessaire : la sidebar bascule sur EditPanel
    #      pendant la visionneuse, masquant le bouton "Identifier…") ----
    # Bouton "✕" de la visionneuse (PhotoViewer → closed → show_grid), même
    # pattern que test_edit_nondestructive.py — pas de "Chronologie" cliquable
    # en mode visionneuse. Retente le clic si sans effet : juste après la
    # fermeture de l'_AssignDialog, le premier clic peut arriver avant que le
    # focus/la boucle d'évènements Qt ne se soit stabilisé sur la visionneuse.
    texts: list[str] = []
    for _attempt in range(5):
        find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()
        time.sleep(1.0)
        texts = [b.window_text() for b in window.descendants(control_type="Button")]
        if "1:1" not in texts:
            break
    else:
        raise LookupError(f"le bouton '✕' est resté sans effet après 5 tentatives (boutons={texts!r})")

    # ---- 3. Identifier un groupe entier (nouvelle personne) ----
    group_cluster_id = query_one(
        faces_db,
        "SELECT cluster_id FROM faces WHERE cluster_id IS NOT NULL"
        " AND person_id IS NULL LIMIT 1",
    )
    assert group_cluster_id is not None, (
        "aucun cluster restant à identifier après l'étape 2 — "
        "topologie de regroupement inattendue"
    )

    find_dialog_button(window, ["Identify"], exact=False).click_input()
    right_click_and_click_context_menu_item(
        lambda: find_by_accessible_name(window, f"facecluster::{group_cluster_id}"),
        window, "Identify this", exact=False,
    )
    _find_name_input(window).set_edit_text("TestPersonGroup")
    find_dialog_button(window, ["OK"], exact=True).click_input()

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT id FROM persons WHERE name=?", ("TestPersonGroup",)
        ) is not None,
        timeout=20.0, message="aucune ligne persons créée pour TestPersonGroup",
    )
    group_person_id = query_one(
        catalog_db, "SELECT id FROM persons WHERE name=?", ("TestPersonGroup",)
    )
    wait_for_condition(
        lambda: query_one(
            faces_db,
            "SELECT COUNT(*) FROM faces WHERE cluster_id=? AND"
            " (person_id IS NULL OR person_id!=?)",
            (group_cluster_id, group_person_id),
        ) == 0,
        timeout=20.0,
        message="tous les visages du cluster n'ont pas reçu le person_id du groupe",
    )

    # ---- 4. Fusionner les deux personnes créées ci-dessus ----
    solo_faces_before = query_one(
        faces_db, "SELECT COUNT(*) FROM faces WHERE person_id=?", (solo_person_id,)
    )
    group_faces_before = query_one(
        faces_db, "SELECT COUNT(*) FROM faces WHERE person_id=?", (group_person_id,)
    )

    right_click_and_click_context_menu_item(
        lambda: _find_person_list_item(window, "TestPersonSolo"),
        window, "Merge with…", exact=True,
    )
    find_dialog_button(window, ["OK"], exact=True).click_input()

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM persons WHERE id=?", (solo_person_id,)
        ) == 0,
        timeout=20.0, message="TestPersonSolo n'a pas été supprimé après la fusion",
    )
    assert query_one(
        faces_db, "SELECT COUNT(*) FROM faces WHERE person_id=?", (solo_person_id,)
    ) == 0
    assert query_one(
        faces_db, "SELECT COUNT(*) FROM faces WHERE person_id=?", (group_person_id,)
    ) == solo_faces_before + group_faces_before

    # ---- Revenir à la grille de photos ----
    # Le clic "Identifier" de l'étape 3 a basculé le contenu principal sur
    # FaceClusterGrid (main_window.py::show_face_clusters, via
    # Sidebar.identify_requested) — ce n'est pas la grille de vignettes, donc
    # aucun `thumb::<path>` n'existe dans l'arbre UIA tant qu'on n'est pas
    # revenu via son bouton "← Photos" (back_requested -> show_grid).
    find_dialog_button(window, ["← Photos"], exact=True).click_input()

    # ---- 5. Ignorer un visage puis le restaurer ----
    # Exclut solo_face_id : même requête que l'étape 2 sinon (identifier une
    # personne ne touche pas `ignored`), ce qui rouvrirait la même photo au
    # lieu d'exercer un visage frais.
    ignore_face_id, ignore_photo_path = _query_all(
        faces_db,
        "SELECT id, photo_path FROM faces WHERE ignored=0 AND id!=? ORDER BY id LIMIT 1",
        (solo_face_id,),
    )[0]

    # La position de scroll de la grille peut avoir dérivé depuis le dernier
    # passage (retour depuis FaceClusterGrid à l'étape 3-4) : la vignette
    # visée peut être hors de la plage virtualisée (cf. scroll_grid_into_view).
    scroll_grid_into_view(window, ignore_photo_path)
    open_photo_in_viewer(window, ignore_photo_path)
    right_click_and_click_context_menu_item(
        lambda: find_by_accessible_name(window, f"faceitem::{ignore_face_id}"),
        window, "Ignore this face", exact=True,
    )
    wait_for_condition(
        lambda: query_one(
            faces_db, "SELECT ignored FROM faces WHERE id=?", (ignore_face_id,)
        ) == 1,
        timeout=15.0, message="le visage n'a pas été marqué ignoré",
    )

    find_dialog_button(window, ["Ignored faces"], exact=False).click_input()
    find_dialog_button(window, ["Restore"], exact=True).click_input()
    find_dialog_button(window, ["Close", "Close"], exact=True).click_input()

    wait_for_condition(
        lambda: query_one(
            faces_db, "SELECT ignored FROM faces WHERE id=?", (ignore_face_id,)
        ) == 0,
        timeout=15.0,
        message="le visage n'a pas été restauré (unignore_face) après fermeture du dialogue",
    )

    # ---- 6. Réinitialiser les groupes uniquement (rapide, non destructeur) ----
    # (le menu "Faces" reste accessible qu'on soit dans la grille ou la
    # visionneuse — pas besoin de revenir explicitement à la grille ici.)
    click_menu_item(window, "Faces", "Reset and reindex…")
    find_dialog_button(window, ["Confirm"], exact=True).click_input()
    find_dialog_button(window, ["OK"], exact=True).click_input()

    # Les personnes identifiées survivent au reset de regroupement (seuls les
    # visages non identifiés/non épinglés perdent leur cluster_id).
    wait_for_condition(
        lambda: query_one(
            faces_db, "SELECT COUNT(*) FROM faces WHERE person_id=?", (group_person_id,)
        ) == solo_faces_before + group_faces_before,
        timeout=30.0,
        message="les visages identifiés n'ont pas survécu à la réinitialisation des groupes",
    )
    wait_for_condition(
        lambda: query_one(
            faces_db,
            "SELECT COUNT(*) FROM faces"
            " WHERE cluster_id IS NULL AND person_id IS NULL AND ignored=0",
        ) == 0,
        timeout=300.0,
        message="le re-regroupement HDBSCAN après reset n'a pas terminé",
    )
    assert window.exists()


def test_faces_reset_full(isolated_app_with_faces):
    window = isolated_app_with_faces.window
    faces_db = isolated_app_with_faces.faces_db
    face_photo_paths = [str(p) for p in isolated_app_with_faces.face_photos]

    _wait_faces_ready(faces_db, face_photo_paths)

    click_menu_item(window, "Faces", "Reset and reindex…")
    find_radio_button(window, "Full reset").click_input()
    find_dialog_button(window, ["Confirm"], exact=True).click_input()

    # QMessageBox.information() est modale : elle bloque le thread UI avant
    # que _start_face_indexing() (déclenché juste après sa fermeture) ne
    # reparte — reset_index() a donc déjà vidé faces/indexed_photos mais la
    # ré-indexation n'a pas encore recommencé, fenêtre sûre pour vérifier
    # l'effet destructif exact avant de cliquer OK.
    find_dialog_button(window, ["OK"], exact=True, timeout=60.0)
    assert query_one(faces_db, "SELECT COUNT(*) FROM faces") == 0
    assert query_one(faces_db, "SELECT COUNT(*) FROM indexed_photos") == 0
    find_dialog_button(window, ["OK"], exact=True).click_input()

    _wait_faces_ready(faces_db, face_photo_paths)
    assert window.exists()
