# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: face identification, person merge, ignoring/restoring
a face, and reset of the grouping. Uses `isolated_app_with_faces` (7 photos:
3 "Personne A", 3 "Personne B", 1 photo of both together -- see
`tests/e2e/fixtures/faces/`).

`test_faces_identify_and_reset`: a single launch, sequential steps:
1. Wait for the end of the scan + of the ArcFace indexing + of the first
   automatic HDBSCAN grouping.
2. Identification of an isolated face (new person) from the viewer --
   `FacePanel` / `_AssignDialog` (people_panel.py:187) ->
   `FaceDatabase.isolate_and_assign_face`.
3. Identification of a whole group (new person) from `FaceClusterGrid`
   (the overlaid check button of `_ClusterCard`, accessible name
   `facecluster::<id>` added for this test -- same principle as
   `_DuplicateCard`/`dupgroup::<id>`) -> `FaceDatabase.assign_person_to_cluster`.
4. Merge of the two people created above through the context menu of the
   sidebar ("Merge with…") -> `people_panel.py::MergePersonsDialog` ->
   `FaceDatabase.merge_persons` + `Catalog.delete_person`.
5. Ignore a face ("Ignore this face" context menu) then restore it through
   `_IgnoredFacesDialog` (the "Restore" button THEN closing the dialog --
   the only real trigger of `unignore_face`, cf.
   `FacePanel._on_show_ignored`).
6. Reset "groups only" (fast, non-destructive, RESET_CLUSTERING) -- checks
   that `FaceDatabase.reset_clustering()` only touches the
   unidentified/unpinned faces: the people created at steps 2-4 must survive
   intact.

No assumption is made about the exact topology of the HDBSCAN grouping (not
guaranteed to be reproducible on AI-generated faces): every step queries
`faces.db` directly to find a candidate face/cluster rather than guessing an
id or a number of groups.

`test_faces_reset_full`: a separate isolated launch (the RESET_FULL mode is
destructive -- it completely erases `faces`/`indexed_photos` then restarts a
full indexing -- and it would corrupt the state needed by steps 2-5 above if
it shared the same launch)."""
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
    """Waits for the end of the scan + of the ArcFace indexing + of the first
    automatic HDBSCAN grouping (triggered with no interaction as soon as the
    indexing detects at least one face, cf. main_window_faces.py
    ::_on_face_indexing_finished) for the 7 photos of the fixture."""
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
    """Locates the "Nom de la personne…" `QLineEdit` of `_AssignDialog` by
    vertical proximity with the "Créer une nouvelle personne" radio button --
    same principle as `_find_edit_near_text` in test_folder_management.py
    (adapted to a `QRadioButton` rather than a `QLabel`): an empty `QLineEdit`
    does not reliably expose its placeholder text through UIA, and other
    `Edit` controls (sidebar filter, people search) coexist in the tree while
    the dialog is open."""
    anchor = find_radio_button(window, "Créer une nouvelle personne")
    a_rect = anchor.rectangle()
    a_mid = (a_rect.top + a_rect.bottom) / 2
    edits = window.descendants(control_type="Edit")
    return min(
        edits,
        key=lambda e: abs(((e.rectangle().top + e.rectangle().bottom) / 2) - a_mid),
    )


def _find_person_list_item(window, name_substring: str, *, timeout: float = 15.0):
    """Locates an item of `Sidebar._persons_list` by name substring (label
    "Name  (n)", cf. sidebar.py::refresh_persons) -- same principle as
    `_find_tree_item` in test_folder_management.py, adapted to
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

    # ---- 2. Identify an isolated face (new person) ----
    solo_face_id, solo_photo_path = _query_all(
        faces_db,
        "SELECT id, photo_path FROM faces WHERE ignored=0 ORDER BY id LIMIT 1",
    )[0]

    open_photo_in_viewer(window, solo_photo_path)
    # `_act_faces_toggle` (main_window.py) is a *checkable* QPushButton: Qt
    # exposes it to UIA as control_type="CheckBox", not "Button" -- find_dialog_button
    # (filtered on Button) never finds it, hence find_checkbox here.
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

    # ---- Go back to the grid (necessary: the sidebar switches to EditPanel
    #      during the viewer, hiding the "Identify…" button) ----
    # The "X" button of the viewer (PhotoViewer -> closed -> show_grid), same
    # pattern as test_edit_nondestructive.py -- no clickable "Chronologie"
    # in viewer mode. Retries the click if it had no effect: just after the
    # _AssignDialog is closed, the first click can arrive before the Qt focus /
    # event loop has settled on the viewer.
    texts: list[str] = []
    for _attempt in range(5):
        find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()
        time.sleep(1.0)
        texts = [b.window_text() for b in window.descendants(control_type="Button")]
        if "1:1" not in texts:
            break
    else:
        raise LookupError(f"le bouton '✕' est resté sans effet après 5 tentatives (boutons={texts!r})")

    # ---- 3. Identify a whole group (new person) ----
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

    # ---- 4. Merge the two people created above ----
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

    # ---- Go back to the photo grid ----
    # The "Identify" click of step 3 switched the main content to
    # FaceClusterGrid (main_window.py::show_face_clusters, through
    # Sidebar.identify_requested) -- that is not the thumbnail grid, so no
    # `thumb::<path>` exists in the UIA tree until we have gone back through its
    # "< Photos" button (back_requested -> show_grid).
    find_dialog_button(window, ["← Photos"], exact=True).click_input()

    # ---- 5. Ignore a face then restore it ----
    # Excludes solo_face_id: the same query as step 2 otherwise (identifying a
    # person does not touch `ignored`), which would reopen the same photo instead
    # of exercising a fresh face.
    ignore_face_id, ignore_photo_path = _query_all(
        faces_db,
        "SELECT id, photo_path FROM faces WHERE ignored=0 AND id!=? ORDER BY id LIMIT 1",
        (solo_face_id,),
    )[0]

    # The scroll position of the grid may have drifted since the last visit
    # (return from FaceClusterGrid at steps 3-4): the targeted thumbnail may be
    # outside the virtualised range (cf. scroll_grid_into_view).
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

    # ---- 6. Reset the groups only (fast, non-destructive) ----
    # (the "Faces" menu stays reachable whether we are in the grid or in the
    # viewer -- no need to come back to the grid explicitly here.)
    click_menu_item(window, "Faces", "Reset and reindex…")
    find_dialog_button(window, ["Confirm"], exact=True).click_input()
    find_dialog_button(window, ["OK"], exact=True).click_input()

    # The identified people survive the grouping reset (only the
    # unidentified/unpinned faces lose their cluster_id).
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

    # QMessageBox.information() is modal: it blocks the UI thread before
    # _start_face_indexing() (triggered right after it closes) starts again --
    # reset_index() has therefore already emptied faces/indexed_photos but the
    # re-indexing has not restarted yet, a safe window to check the exact
    # destructive effect before clicking OK.
    find_dialog_button(window, ["OK"], exact=True, timeout=60.0)
    assert query_one(faces_db, "SELECT COUNT(*) FROM faces") == 0
    assert query_one(faces_db, "SELECT COUNT(*) FROM indexed_photos") == 0
    find_dialog_button(window, ["OK"], exact=True).click_input()

    _wait_faces_ready(faces_db, face_photo_paths)
    assert window.exists()
