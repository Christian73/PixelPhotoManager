# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: UI actions on the duplicates, complementing
test_duplicate_detection.py (which only checks the detection itself, on the
database side). A single application launch, on the same synthetic library
(exact/resized/cropped pairs):

1. Duplicates grid (`DuplicateGrid`, the "Duplicates" badge of the sidebar):
   double-click on the card of the exact group -> quick comparison in the
   viewer ("1:1" as the marker that it opened) -> closing -> automatic return
   to the duplicates grid (`_viewer_back_target`).
2. The cross button of a card (resized group) -> persistent dissolution
   (`Catalog.ignore_duplicate_group`) -> checks that ONLY that group is
   dissolved, the exact group staying intact (the same contract as
   tests/test_catalog.py::test_ignore_duplicate_group_dissolves_only_that_group,
   proven here end to end from the UI click).
3. Duplicates popup (`_DuplicatesPopup`) opened from the real
   "⧉ Duplicates" button of the VIEWER (a real QPushButton, cf. photo_viewer.py:349)
   rather than the hand-painted badge of the grid (`ThumbnailCell.
   paintEvent`, not automatable without a click in raw pixel coordinates):
   navigation to another copy of the cropped group without closing the
   popup, then an explicit close through "Close".
4. Deleting one copy of the cropped group -> a checked side effect:
   the remaining copy goes back to `duplicate_group_id=NULL` (fewer than 2
   members left, cf. main_window.py::_on_delete_finished).
5. The "Tools › Duplicate status…" dialog tested directly (not only
   as a fallback of `wait_for_duplicate_detection`): "View the groups"
   navigates to the duplicates grid, "Check now" starts another
   pass without crashing the application, and the standard closing button
   ("Close") closes the dialog without an action.

The cards (`_DuplicateCard`) and their cross buttons carry a dedicated
accessible name (`dupgroup::<id>` / `dupgroup_ignore::<id>`, added to
duplicate_grid.py for this e2e work, the same convention as ThumbnailCell)
because they otherwise have no unique UIA text (identical tooltips for every
card)."""
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_menu_item,
    click_popup_button,
    click_popup_list_item,
    click_yes,
    double_click_element,
    find_by_accessible_name,
    find_dialog_button,
    find_thumbnail,
    open_photo_in_viewer,
    query_one,
    right_click_element,
    wait_for_condition,
    wait_for_duplicate_detection,
)

pytestmark = pytest.mark.e2e


def _group_id(catalog_db, path) -> int | None:
    return query_one(catalog_db, "SELECT duplicate_group_id FROM photos WHERE path=?", (str(path),))


def test_duplicates_ui(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    wait_for_duplicate_detection(
        window, catalog_db,
        (manifest.exact_duplicate_pair, manifest.resized_duplicate_pair, manifest.crop_duplicate_pair),
        timeout=90.0,
    )

    group_exact = _group_id(catalog_db, manifest.exact_duplicate_pair[0])
    group_resized = _group_id(catalog_db, manifest.resized_duplicate_pair[0])
    group_crop = _group_id(catalog_db, manifest.crop_duplicate_pair[0])
    assert group_exact is not None and group_resized is not None and group_crop is not None

    # ---- 1. Duplicates grid: double-click on the card of the exact group ----
    find_dialog_button(window, ["Duplicates"], exact=True, timeout=15.0).click_input()
    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0)  # marker: duplicates grid active

    card_exact = find_by_accessible_name(window, f"dupgroup::{group_exact}", timeout=15.0)
    double_click_element(card_exact)
    find_dialog_button(window, ["1:1"], exact=True, timeout=15.0)  # marker: viewer open
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()
    # Automatic return to the duplicates grid (_viewer_back_target).
    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0)

    # ---- 2. The cross button: dissolution isolated to the resized group ----
    find_by_accessible_name(window, f"dupgroup_ignore::{group_resized}", timeout=15.0).click_input()
    wait_for_condition(
        lambda: all(_group_id(catalog_db, p) is None for p in manifest.resized_duplicate_pair),
        timeout=20.0, message="le groupe redimensionné n'a pas été dissous",
    )
    assert _group_id(catalog_db, manifest.exact_duplicate_pair[0]) == group_exact
    assert _group_id(catalog_db, manifest.exact_duplicate_pair[1]) == group_exact

    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0).click_input()

    # ---- 3. Duplicates popup from the real button of the viewer ----
    open_photo_in_viewer(window, manifest.crop_duplicate_pair[0])
    find_dialog_button(window, ["⧉ Duplicates"], exact=True, timeout=10.0).click_input()
    other_name = Path(manifest.crop_duplicate_pair[1]).name
    click_popup_list_item("_DuplicatesPopup", other_name, exact=False, timeout=10.0)
    wait_for_condition(
        lambda: any(
            other_name in t.window_text()
            for t in window.descendants(control_type="Text")
        ),
        timeout=10.0,
        message="la navigation depuis la popup de doublons n'a pas changé la photo affichée",
    )
    # The "Close" button of the popup lives in the _DuplicatesPopup top-level
    # window (Qt.Popup), never among the descendants of the main window — same
    # trap as its list items just above.
    click_popup_button("_DuplicatesPopup", "Close", timeout=10.0)
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    # ---- 4. Deleting one copy of the cropped group: side effect ----
    thumb = find_thumbnail(window, manifest.crop_duplicate_pair[0], timeout=15.0)
    right_click_element(thumb)
    click_context_menu_item(window, "Delete the file…\tDel", exact=True, timeout=10.0)
    click_yes(window)
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.crop_duplicate_pair[0]),),
        ) == 0,
        timeout=20.0, message="le fichier supprimé est toujours présent au catalogue",
    )
    wait_for_condition(
        lambda: _group_id(catalog_db, manifest.crop_duplicate_pair[1]) is None,
        timeout=20.0,
        message="le groupe recadré n'a pas été dissous après la suppression d'un exemplaire",
    )

    # ---- 5. The "Duplicate status…" dialog tested directly ----
    click_menu_item(window, "Tools", "Duplicate status…")
    find_dialog_button(window, ["View the groups"], exact=True, timeout=10.0).click_input()
    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0).click_input()

    click_menu_item(window, "Tools", "Duplicate status…")
    find_dialog_button(window, ["Check now"], exact=True, timeout=10.0).click_input()
    assert isolated_app.app.process.poll() is None, (
        "l'application a quitté de manière inattendue au déclenchement manuel "
        "d'une nouvelle passe de détection"
    )

    click_menu_item(window, "Tools", "Duplicate status…")
    find_dialog_button(window, ["Close"], exact=True, timeout=10.0).click_input()
    assert window.exists()
