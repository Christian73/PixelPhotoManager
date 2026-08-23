# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: save options, the "do not ask again" deletion
confirmation, external applications and settings (video player), help/about.
A single application launch, sequential steps:

1. "Save the edited image to disk" (context menu of the grid -- the same code
   as the equivalent entry of the viewer, `save_requested.emit(photo)` in both
   cases, photo_viewer.py:717 and thumbnail_grid.py:1227 -- no need to repeat
   the test for both entries)
   -> `_SaveOptionsDialog`, default option "Overwrite the original file"
   with the backup checked by default -> checks the copy in
   `.tmp_originals` (naming `{stem}_{timestamp}{suffix}`,
   cf. main_window.py::_backup_original) AND that the file is really
   overwritten. The "Save to another location…" option opens a native
   `QFileDialog` with no editable fallback (unlike the path of the custom
   video player, cf. §3 below) -- a documented gap, not automated here,
   in the same spirit as the exclusion of the drag gestures from the plan.

2. The "Do not ask again" box of the deletion dialog
   (main_window.py:1822-1841, persists `ui.delete_no_confirm`): deletion
   of 3 successive witness photos -- the 1st confirmed without ticking the box
   (the dialog appears), the 2nd confirmed with the box ticked, the 3rd must
   trigger NO confirmation dialog at all.

3. "Tools › External applications…": an entry is pre-injected into the
   config at launch (an indirect way of configuring `isolated_app`, the only
   way to get a real executable path without going through the native selector
   of `_add()`, which has no editable fallback) -> checks that its icon
   appears in the viewer bar (a dedicated accessible name,
   `extapp::<name>`, added to photo_viewer.py for this e2e work) ->
   removal through the "Remove" button of the dialog -> the icon disappears.

4. "Tools › Settings › Video player": selecting the custom player and
   typing a dummy path into the dedicated `QLineEdit`
   (located by vertical proximity with the "Custom player:" `QRadioButton`,
   NOT by accessible name: a `QLineEdit` generally implements the UIA Text
   pattern, which takes precedence over the accessible name in
   `window_text()` -- cf. pywinauto `base_wrapper.window_text`, the same
   ambiguity that motivated the identification by elimination in
   `type_into_sidebar_filter`) -> checks the round trip in `config.json`
   (`video.player_path`), without trying to really launch the player
   (a fallback documented by the plan if launching the subprocess cannot be
   checked reliably).

5. Help (F1 -> the "Help…" menu) and "About": both open
   `HelpDialog` (help_dialog.py) without an error; closed through the
   standard "Close" button."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_list_item,
    click_menu_item,
    click_yes,
    find_by_accessible_name,
    find_checkbox,
    find_dialog_button,
    find_thumbnail,
    open_photo_in_viewer,
    query_one,
    right_click_element,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e

_EXTAPP_NAME = "Bloc-notes test"
_EXTAPP_PATH = r"C:\Windows\System32\notepad.exe"


def _click_radio(window, text: str, *, timeout: float = 10.0) -> None:
    """Ticks the `QRadioButton` carrying this text -- needed before typing
    text into the `QLineEdit` of the custom path: it is disabled
    (`setEnabled(False)`) as long as the "Custom player" radio button
    is not ticked (settings_dialog.py:210, `_on_radio_changed`), so
    `set_edit_text` fails with `ElementNotEnabled` if the radio button is not
    clicked first."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for r in window.descendants(control_type="RadioButton"):
                if r.window_text() == text:
                    r.click_input()
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"RadioButton {text!r} introuvable après {timeout}s ({last_exc})")


def _find_edit_near_radio(window, radio_text: str, *, timeout: float = 10.0):
    """Locates the QLineEdit of the custom video player path by vertical
    proximity with its neighbouring QRadioButton -- see the docstring of the
    module for the reason (window_text() is unreliable for a QLineEdit)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            radios = [r for r in window.descendants(control_type="RadioButton")
                      if r.window_text() == radio_text]
            edits = window.descendants(control_type="Edit")
            if radios and edits:
                r_rect = radios[0].rectangle()
                r_mid = (r_rect.top + r_rect.bottom) / 2
                return min(
                    edits,
                    key=lambda e: abs(((e.rectangle().top + e.rectangle().bottom) / 2) - r_mid),
                )
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"QLineEdit proche de {radio_text!r} introuvable après {timeout}s ({last_exc})")


def _config_get(config_path: Path, dotted_key: str):
    if not config_path.exists():
        return None
    data = json.loads(config_path.read_text(encoding="utf-8"))
    for k in dotted_key.split("."):
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return data


@pytest.mark.parametrize(
    "isolated_app",
    [{"tools": {"external_apps": [{"name": _EXTAPP_NAME, "path": _EXTAPP_PATH}]}}],
    indirect=True,
)
def test_save_options_and_settings(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    config_path = isolated_app.catalog_db.parent / "config.json"

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    # ---- 1. Save the edited image: overwrite + backup in .tmp_originals ----
    save_photo = manifest.burst_pair[0]
    save_path = Path(save_photo)
    backup_dir = save_path.parent / ".tmp_originals"

    thumb = find_thumbnail(window, str(save_photo), timeout=30.0)
    right_click_element(thumb)
    click_context_menu_item(window, "Save the edited image to disk\tCtrl+S", exact=True, timeout=10.0)
    find_dialog_button(window, ["Save"], exact=True, timeout=10.0).click_input()
    wait_for_condition(
        lambda: backup_dir.is_dir() and any(
            p.name.startswith(save_path.stem + "_") and p.suffix == save_path.suffix
            for p in backup_dir.iterdir()
        ),
        timeout=20.0, message="aucune sauvegarde n'a été créée dans .tmp_originals",
    )

    # ---- 2. Deletion confirmation + the "Do not ask again" box ----
    photo1, photo2, photo3 = manifest.control_photos[0], manifest.control_photos[1], manifest.control_photos[2]

    thumb1 = find_thumbnail(window, str(photo1), timeout=30.0)
    right_click_element(thumb1)
    click_context_menu_item(window, "Delete the file…\tDel", exact=True, timeout=10.0)
    click_yes(window)
    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo1),)) == 0,
        timeout=20.0, message="photo1 non supprimée (confirmation sans case cochée)",
    )
    assert _config_get(config_path, "ui.delete_no_confirm") in (None, False)

    thumb2 = find_thumbnail(window, str(photo2), timeout=15.0)
    right_click_element(thumb2)
    click_context_menu_item(window, "Delete the file…\tDel", exact=True, timeout=10.0)
    find_checkbox(window, "Do not ask again", timeout=10.0).click_input()
    click_yes(window)
    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo2),)) == 0,
        timeout=20.0, message="photo2 non supprimée (confirmation avec case cochée)",
    )
    wait_for_condition(
        lambda: _config_get(config_path, "ui.delete_no_confirm") is True,
        timeout=10.0, message="ui.delete_no_confirm n'a pas été persisté",
    )

    thumb3 = find_thumbnail(window, str(photo3), timeout=15.0)
    right_click_element(thumb3)
    click_context_menu_item(window, "Delete the file…\tDel", exact=True, timeout=10.0)
    with pytest.raises(LookupError):
        find_dialog_button(window, ["Oui", "Yes", "&Oui", "&Yes"], timeout=3.0)
    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo3),)) == 0,
        timeout=20.0, message="photo3 non supprimée automatiquement (ui.delete_no_confirm actif)",
    )

    # ---- 3. External applications: viewer icon + removal ----
    ext_photo = manifest.exact_duplicate_pair[0]
    open_photo_in_viewer(window, ext_photo)
    find_by_accessible_name(window, f"extapp::{_EXTAPP_NAME}", timeout=10.0)
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    click_menu_item(window, "Tools", "External applications…")
    click_list_item(window, _EXTAPP_NAME, exact=False, timeout=10.0)
    find_dialog_button(window, ["Remove"], exact=True, timeout=10.0).click_input()
    find_dialog_button(window, ["OK"], timeout=10.0).click_input()

    open_photo_in_viewer(window, ext_photo)
    with pytest.raises(LookupError):
        find_by_accessible_name(window, f"extapp::{_EXTAPP_NAME}", timeout=3.0)
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    # ---- 4. Settings: custom video player (config.json round trip) ----
    click_menu_item(window, "Tools", "Settings")
    click_list_item(window, "Video player", exact=True, timeout=10.0)
    _click_radio(window, "Custom player:", timeout=10.0)
    edit_path = _find_edit_near_radio(window, "Custom player:", timeout=10.0)
    edit_path.set_edit_text(r"C:\FakePlayer\player.exe")
    find_dialog_button(window, ["OK"], timeout=10.0).click_input()
    wait_for_condition(
        lambda: _config_get(config_path, "video.player_path") == r"C:\FakePlayer\player.exe",
        timeout=10.0, message="video.player_path n'a pas été persisté",
    )

    # ---- 5. Help / About ----
    click_menu_item(window, "Help", "Help…")
    find_dialog_button(window, ["Close"], exact=True, timeout=10.0).click_input()

    click_menu_item(window, "Help", "About")
    find_dialog_button(window, ["Close"], exact=True, timeout=10.0).click_input()
    assert window.exists()
