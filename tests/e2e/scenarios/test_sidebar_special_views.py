# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: the special views of the sidebar (Favourites, Videos,
search by file name), which proves along the way the two favourite fixes of
this same e2e work:
- `PhotoViewer._toggle_favorite` never persisted anything before the fix
  (an in-memory mutation only) -> `favorite_toggle_requested` (photo_viewer.py:134)
  -> `MainWindow._on_favorite_toggle_requested` -> `Catalog.set_favorite`.
- The context menu of the grid had a "Mark as favourite…" item with no
  callback at all (`menu.addAction(fav_label)` alone) -> `favorite_toggle_requested`
  (thumbnail_grid.py:420) added, the same `MainWindow` handler.

A single application launch, sequential steps on the synthetic library
(3 witness photos control_1/2/3.jpg, none of them a favourite to start
with):
1. Viewer: favourite toggle through the toolbar button (glyph
   "♡" -> "★") on control_1.jpg -> checked directly on catalog.db
   (`photos.is_favorite`), NOT on the UI. A UIA trap confirmed empirically
   (a `descendants(control_type=...)` dump while the viewer was open):
   `self._btn_fav` is a `QPushButton` but with `setCheckable(True)`
   (photo_viewer.py:200) -> the Qt accessibility bridge exposes it as
   `control_type="CheckBox"`, not `"Button"` -- invisible to
   `find_dialog_button`, `find_checkbox(window, "♡", ...)` is needed.
2. Sidebar "♡ Favourites" (_SPECIAL_FAV) -> the thumbnail of control_1.jpg must
   appear in the filtered grid.
3. Back to "★ Timeline of every photo" (_SPECIAL_ALL) so that
   control_2.jpg (never a favourite) is visible again, then a favourite toggle
   through the right-click context menu of the grid on control_2.jpg (a code
   path distinct from step 1) -> DB check -> another right click,
   "Remove from favourites" this time -> checks that the favourite goes back to 0
   (the two dynamic labels of the menu, cf. thumbnail_grid.py:1223, are
   exercised that way).
4. "▶ Videos" (_SPECIAL_VIDEOS): `manifest.video` may be absent (a missing
   encoder on the generation machine, cf. generate_library.py) -> a documented
   skip if that is the case, otherwise checks that the video appears and that
   `photos.media_type='video'`.
5. "🔍 By file name" (_SPECIAL_FILENAME): the text typed into the same
   filter field as the one used for folders/people (`Sidebar.
   filter_text`, NOT a separate search field, cf. sidebar.py:459-461) is
   read at the moment the special item is clicked (`itemClicked`, no need to
   press Enter) -> checks that only control_2.jpg (the pattern "control_2",
   unique among the 3 witnesses) appears. A UIA trap confirmed empirically
   (a `descendants()` dump right after the click: the status label stayed
   stuck on "Videos — 1 photo", proof that the click never reached
   `_on_album_selected`): this entry is the 4th of the sidebar Albums
   QListWidget, which only shows a limited number of items without scrolling --
   the same clipping/virtualisation trap as `_reveal_sidebar_albums_tail` in
   test_albums.py, here on the 4 special entries themselves rather than on
   an added album. The list must be given the focus (a click on an already
   visible item) then {END} to bring "🔍 By file name" into the viewport
   before it can be clicked."""
import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_list_item,
    find_checkbox,
    find_dialog_button,
    find_thumbnail,
    open_photo_in_viewer,
    query_one,
    right_click_element,
    type_into_sidebar_filter,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e


def _is_favorite(catalog_db, photo_path) -> int | None:
    return query_one(catalog_db, "SELECT is_favorite FROM photos WHERE path=?", (str(photo_path),))


def test_sidebar_special_views(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo_fav = manifest.control_photos[0]
    photo_other = manifest.control_photos[1]

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_fav),)) == 1
        and query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_other),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    assert _is_favorite(catalog_db, photo_fav) == 0
    assert _is_favorite(catalog_db, photo_other) == 0

    # ---- 1. Favourite toggle from the viewer (bugfix _toggle_favorite) ----
    open_photo_in_viewer(window, photo_fav)
    find_checkbox(window, "♡", timeout=10.0).click_input()
    wait_for_condition(
        lambda: _is_favorite(catalog_db, photo_fav) == 1,
        timeout=20.0, message="le favori (visionneuse) n'a pas été persisté",
    )
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    # ---- 2. "♡ Favourites" view: the favourite thumbnail must appear ----
    click_list_item(window, "♡ Favourites", exact=True, timeout=10.0)
    find_thumbnail(window, photo_fav, timeout=15.0)

    # ---- 3. Favourite toggle from the context menu of the grid (bugfix
    # dead menu stub): back to the full view so that control_2.jpg is
    # visible, then mark/unmark through a right click. ----
    click_list_item(window, "★ Timeline of every photo", exact=True, timeout=10.0)
    thumb_other = find_thumbnail(window, photo_other, timeout=15.0)
    right_click_element(thumb_other)
    click_context_menu_item(window, "Mark as favourite", exact=True, timeout=10.0)
    wait_for_condition(
        lambda: _is_favorite(catalog_db, photo_other) == 1,
        timeout=20.0, message="le favori (menu contextuel grille) n'a pas été persisté",
    )

    thumb_other = find_thumbnail(window, photo_other, timeout=15.0)
    right_click_element(thumb_other)
    click_context_menu_item(window, "Remove from favourites", exact=True, timeout=10.0)
    wait_for_condition(
        lambda: _is_favorite(catalog_db, photo_other) == 0,
        timeout=20.0, message="le retrait de favori (menu contextuel grille) n'a pas été persisté",
    )

    # ---- 4. "▶ Videos" view: may be absent on this machine ----
    if manifest.video is None:
        pytest.skip("Aucune vidéo synthétique générée sur cette machine (encodeur manquant)")
    click_list_item(window, "▶ Videos", exact=True, timeout=10.0)
    find_thumbnail(window, manifest.video, timeout=15.0)
    assert query_one(
        catalog_db, "SELECT media_type FROM photos WHERE path=?", (str(manifest.video),)
    ) == "video"

    # ---- 5. "🔍 By file name" view: the same filter field as
    # folders/people, read on clicking the special item. ----
    type_into_sidebar_filter(window, "control_2")
    # The sidebar Albums list (a QListWidget) only shows a limited number of
    # items without scrolling; "🔍 By file name" (the 4th special entry) may be
    # partly outside the visible viewport (the same virtualisation/clipping trap
    # as _reveal_sidebar_albums_tail in test_albums.py) -> we reveal it by
    # scrolling the list to the end with {END} after giving it the focus through
    # a click on an already visible item.
    import pywinauto.keyboard as kb
    click_list_item(window, "▶ Videos", exact=True, timeout=10.0)
    kb.send_keys("{END}")
    click_list_item(window, "🔍 By file name", exact=True, timeout=10.0)
    find_thumbnail(window, photo_other, timeout=15.0)
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM photos WHERE filename LIKE '%control_2%'"
    ) == 1
