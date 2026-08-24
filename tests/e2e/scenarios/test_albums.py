# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: the three album creation paths and the single
deletion path. A single application launch, sequential steps (the order is
imposed by the plan: creation -> populating -> deletion):

1. The sidebar "+" (`Sidebar._create_album`, `sidebar.py:810`) ->
   `QInputDialog.getText(..., "New album", "Album name:")` ->
   `bus.emit("album.create_requested", ...)` -> `MainWindow._on_album_create`
   (main_window.py:1592) -> `Catalog.create_album` alone, with no photo -> one
   `albums` row created, 0 associated `album_photos` row.

2. Grid, multiple selection (click + Ctrl+click on 2 witness photos) -> context
   menu "Create a new album with the 2 selected photos…"
   (`thumbnail_grid.py:1234`, a dynamic label hence a substring search) ->
   `_on_create_album_with` (main_window.py:1650) ->
   `QInputDialog.getText(..., "New album", f"Name of the new album ({n}
   photo(s) selected):")` -> new album + `add_photos_to_album` -> checks that
   the 2 expected `album_photos` rows exist.

3. Grid, single selection of a 3rd witness photo (not in an album yet)
   -> context menu "Add this photo to an album…"
   (`thumbnail_grid.py:1232`) -> `_on_add_to_album` (main_window.py:1615):
   a `QDialog` with a `QListWidget` of the existing albums (label
   `f"{album.name}  ({album.photo_count} photo(s))"`, row 0 pre-selected
   by default) -> selects the album of step 2 by a substring of its name
   (the displayed photo count varies, only the name is stable) -> OK -> checks
   that a 3rd `album_photos` row appears for that album WITHOUT disturbing the 2
   rows of step 2. The "no existing album" case (an information QMessageBox,
   main_window.py:1618) is not reachable in this scenario since an album
   already exists since step 1 -- a documented gap, in the same spirit as the
   gaps already noted in the other scenarios of this folder.

4. Context menu of the sidebar Albums list (right click on the item of the
   album of step 2) -> "Delete the album…" (`sidebar.py:701`) ->
   a standard `QMessageBox.Yes/No` confirmation (NOT retextured, unlike
   "Delete the folder…" in test_folder_management.py) -> `click_yes`
   is enough -> checks that the `albums`/`album_photos` rows of that album
   disappear, but that the photos themselves (`photos` + the files on disk)
   stay intact."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_yes,
    find_dialog_button,
    find_thumbnail,
    query_one,
    right_click_element,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e

_EMPTY_ALBUM_NAME = "Album Vide E2E"
_POPULATED_ALBUM_NAME = "Album Peuplé E2E"


def _find_edit_near_text(window, text_substring: str, *, timeout: float = 10.0):
    """Locates the `QLineEdit` of a `QInputDialog` by vertical proximity with
    its label -- the same helper as test_folder_management.py (duplicated here
    rather than factored into conftest.py: each scenario keeps its own small
    local utilities in this folder, cf. the convention already established for
    `_find_edit_near_radio`/`_find_edit_near_text`)."""
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


def _find_list_item(window, text_substring: str, *, timeout: float = 10.0):
    """Locates the UIA element (`ListItem`) of a `QListWidget` item by
    substring, without clicking on it -- needed for a right click
    (`right_click_element`), unlike `click_list_item` in conftest.py which
    left-clicks directly."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for item in window.descendants(control_type="ListItem"):
                if text_substring in item.window_text():
                    return item
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Élément de liste contenant {text_substring!r} introuvable après {timeout}s ({last_exc})")


def _reveal_sidebar_albums_tail(window) -> None:
    """The sidebar Albums list (`Sidebar._albums_list`, a `QListWidget`) only
    exposes through UIA the `ListItem`s currently inside its visible viewport
    (the same virtualisation trap as the thumbnail grid, cf. the docstring of
    `find_thumbnail`) -- an album added after the 4 special entries
    (Timeline/Favorites/Videos/By filename) may therefore stay invisible to
    `_find_list_item` as long as the list has not been scrolled. We focus the
    list through an always present item then send {END} to bring the end of the
    list into the viewport."""
    for item in window.descendants(control_type="ListItem"):
        if "Timeline" in item.window_text():
            item.click_input()
            break
    import pywinauto.keyboard as kb
    kb.send_keys("{END}")
    time.sleep(0.3)


def test_albums(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo_a, photo_b, photo_c = manifest.control_photos

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_a),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    # ---- 1. sidebar "+": empty album ----
    find_dialog_button(window, ["+"], exact=True, timeout=10.0).click_input()
    edit_new = _find_edit_near_text(window, "Album name", timeout=10.0)
    edit_new.set_edit_text(_EMPTY_ALBUM_NAME)
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_EMPTY_ALBUM_NAME,)) is not None,
        timeout=15.0, message="l'album vide n'a pas été créé au catalogue",
    )
    empty_album_id = query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_EMPTY_ALBUM_NAME,))
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (empty_album_id,)
    ) == 0, "l'album créé sans photo ne devrait avoir aucune ligne album_photos"

    # ---- 2. Multiple selection in the grid -> "Create a new album with…" ----
    find_thumbnail(window, str(photo_a), timeout=30.0).click_input()
    thumb_b = find_thumbnail(window, str(photo_b), timeout=15.0)
    thumb_b.click_input(pressed="control")
    right_click_element(thumb_b)
    click_context_menu_item(window, "Create a new album with", exact=False, timeout=10.0)
    edit_populated = _find_edit_near_text(window, "Name of the new album", timeout=10.0)
    edit_populated.set_edit_text(_POPULATED_ALBUM_NAME)
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_POPULATED_ALBUM_NAME,)) is not None,
        timeout=15.0, message="l'album peuplé n'a pas été créé au catalogue",
    )
    populated_album_id = query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_POPULATED_ALBUM_NAME,))
    photo_a_id = query_one(catalog_db, "SELECT id FROM photos WHERE path=?", (str(photo_a),))
    photo_b_id = query_one(catalog_db, "SELECT id FROM photos WHERE path=?", (str(photo_b),))
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (populated_album_id,)
        ) == 2,
        timeout=15.0, message="les 2 photos sélectionnées n'ont pas été ajoutées à l'album",
    )
    for pid in (photo_a_id, photo_b_id):
        assert query_one(
            catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=? AND photo_id=?",
            (populated_album_id, pid),
        ) == 1, f"photo {pid} absente de l'album peuplé"

    # ---- 3. Single selection of a 3rd photo -> "Add … to an album…" ----
    thumb_c = find_thumbnail(window, str(photo_c), timeout=15.0)
    thumb_c.click_input()  # a plain click: deselects a/b
    right_click_element(thumb_c)
    click_context_menu_item(window, "to an album", exact=False, timeout=10.0)
    _find_list_item(window, _POPULATED_ALBUM_NAME, timeout=10.0).click_input()
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    photo_c_id = query_one(catalog_db, "SELECT id FROM photos WHERE path=?", (str(photo_c),))
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=? AND photo_id=?",
            (populated_album_id, photo_c_id),
        ) == 1,
        timeout=15.0, message="la 3e photo n'a pas été ajoutée à l'album via 'Ajouter à un album…'",
    )
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (populated_album_id,)
    ) == 3, "les 2 photos précédentes de l'album ont été perturbées par le 3e ajout"

    # ---- 4. Deleting the populated album (standard Yes/No confirmation) ----
    _reveal_sidebar_albums_tail(window)
    right_click_element(_find_list_item(window, _POPULATED_ALBUM_NAME, timeout=10.0))
    click_context_menu_item(window, "Delete the album…", exact=True, timeout=10.0)
    click_yes(window)

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_POPULATED_ALBUM_NAME,)) is None,
        timeout=15.0, message="l'album n'a pas été supprimé du catalogue",
    )
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (populated_album_id,)
    ) == 0, "les lignes album_photos de l'album supprimé n'ont pas été purgées"
    for path, pid in ((photo_a, photo_a_id), (photo_b, photo_b_id), (photo_c, photo_c_id)):
        assert query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE id=?", (pid,)) == 1, (
            f"la photo {path} a disparu du catalogue après suppression de l'album"
        )
        assert Path(path).exists(), f"le fichier {path} a été supprimé alors que seul l'album l'était"

    # the empty album of step 1 must stay intact, unaffected by the deletion of the 2nd album
    assert query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_EMPTY_ALBUM_NAME,)) == empty_album_id
    assert window.exists()
