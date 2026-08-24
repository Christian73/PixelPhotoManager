# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: export from the grid (multiple selection), size
presets and anti-collision naming -- complements
test_export.py (which only covers the single-photo export from the viewer,
with the default "Maximum size" preset).

`main_window.py::_on_export_clicked` has only ONE entry button
("⬆ Export" of the toolbar, main_window.py:391): its behaviour simply
branches according to the active mode (`self._stack.currentIndex()`) --
the current photo if the viewer is open, `self._grid.get_selected()`
otherwise (contrary to what the initial plan assumed, there is NO dedicated
"Export" entry in the context menu of the grid -- checked by grep, no
occurrence of "export" in thumbnail_grid.py). The second "entry path" to
test is therefore the multiple selection of the grid (Ctrl+click,
`Qt.ControlModifier` handled at thumbnail_grid.py:1116), not a second button.

The synthetic photos are all 900x700 px (630,000 px) -- below the
"Large" (4 Mpx) and "Medium" (2 Mpx) thresholds of `_EXPORT_SIZES`
(export_dialogs.py), which therefore never resize this library
(`_run_export` only reduces if `w*h > max_pixels`). Only the
"Small (~500 kpx)" preset (500,000 px) really triggers a resizing
here -- used in contrast with "Medium" (no resizing at all) to
prove that the selected preset is really taken into account, rather than
assuming a default behaviour.

Sequential steps, a single launch:
1. Multiple selection (click + Ctrl+click) of 2 witness photos -> export
   with the "Small" preset -> both produced files are resized
   below 500,000 px (one file per photo, cf. the loop of `_run_export`).
2. Single-photo export (simple selection, the 3rd witness photo) with the
   "Medium" preset -> output dimensions identical to the original (900x700),
   in contrast with step 1.
3. Re-exporting the SAME photo, into the same folder, with the default preset
   ("Maximum size") -> name collision -> the `{stem}_{n}.jpg` resolution of
   `_run_export` (export_dialogs... no, main_window.py:2319-2325):
   the file of step 2 stays intact, a second file `_1` appears."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

from tests.e2e.conftest import (
    find_dialog_button,
    find_thumbnail,
    query_one,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e

_ORIGINAL_SIZE = (900, 700)


def _set_export_dir(window, path: Path) -> None:
    """Overwrites the destination field of `_ExportDialog` -- see the same
    function in test_export.py for the justification (the default text points
    at the real Pictures folder of the user)."""
    deadline = time.monotonic() + 10.0
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for edit in window.descendants(control_type="Edit"):
                text = edit.window_text()
                if "PixelPhotoManager" in text and text.rstrip("\\/").endswith("Export"):
                    edit.set_edit_text(str(path))
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Champ de destination d'export introuvable après 10s ({last_exc})")


def _click_size_radio(window, label_substring: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for rb in window.descendants(control_type="RadioButton"):
                if label_substring in rb.window_text():
                    rb.click_input()
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Radio de taille contenant {label_substring!r} introuvable après {timeout}s ({last_exc})")


def test_export_extended(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo_a, photo_b, photo_c = manifest.control_photos

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_a),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    export_dir = catalog_db.parents[2] / "export_out_extended"

    # ---- 1. Multiple selection (grid) + the "Small" preset ----
    find_thumbnail(window, str(photo_a), timeout=30.0).click_input()
    find_thumbnail(window, str(photo_b), timeout=15.0).click_input(pressed="control")

    find_dialog_button(window, ["Export"], exact=False, timeout=10.0).click_input()
    _click_size_radio(window, "Small", timeout=10.0)
    _set_export_dir(window, export_dir)
    find_dialog_button(window, ["Export"], exact=True, timeout=10.0).click_input()

    dest_a = export_dir / (Path(photo_a).stem + ".jpg")
    dest_b = export_dir / (Path(photo_b).stem + ".jpg")
    wait_for_condition(
        lambda: dest_a.exists() and dest_b.exists(),
        timeout=30.0, message="l'export multi-sélection n'a pas produit les deux fichiers attendus",
    )
    for dest in (dest_a, dest_b):
        with Image.open(dest) as img:
            w, h = img.size
        # The preset targets ~500,000 px; the integer rounding of the dimensions
        # after scaling (a non-integer factor) may slightly exceed the exact
        # target (e.g. 802x624 = 500,448 from 900x700) -- a 2 % tolerance
        # rather than a strict ceiling, while still proving that a real
        # resizing took place (far from the original 630,000 px).
        assert w * h <= 510_000, f"{dest} : {w}x{h} ({w * h} px) dépasse largement le préréglage « Petite » (~500 000 px)"
        assert (w, h) != _ORIGINAL_SIZE, f"{dest} : taille inchangée, le préréglage ne semble pas appliqué"

    # ---- 2. Single-photo export, "Medium" preset (no resizing) ----
    find_thumbnail(window, str(photo_c), timeout=15.0).click_input()  # a plain click: deselects a/b

    find_dialog_button(window, ["Export"], exact=False, timeout=10.0).click_input()
    _click_size_radio(window, "Medium", timeout=10.0)
    _set_export_dir(window, export_dir)
    find_dialog_button(window, ["Export"], exact=True, timeout=10.0).click_input()

    dest_c = export_dir / (Path(photo_c).stem + ".jpg")
    wait_for_condition(
        lambda: dest_c.exists(), timeout=30.0, message=f"{dest_c} n'a jamais été produit (préréglage Moyenne)",
    )
    with Image.open(dest_c) as img:
        assert img.size == _ORIGINAL_SIZE, (
            f"préréglage « Moyenne » : taille inattendue {img.size}, "
            f"la bibliothèque synthétique ({_ORIGINAL_SIZE}) est sous le seuil de 2 Mpx "
            "et ne devrait jamais être redimensionnée"
        )
    first_export_size = dest_c.stat().st_size

    # ---- 3. Re-exporting the same photo into the same folder: anti-collision naming ----
    find_dialog_button(window, ["Export"], exact=False, timeout=10.0).click_input()
    _set_export_dir(window, export_dir)
    find_dialog_button(window, ["Export"], exact=True, timeout=10.0).click_input()

    dest_c_collision = export_dir / (Path(photo_c).stem + "_1.jpg")
    wait_for_condition(
        lambda: dest_c_collision.exists(),
        timeout=30.0,
        message=f"le second export de {photo_c} n'a pas créé {dest_c_collision} (résolution de collision)",
    )
    assert dest_c.stat().st_size == first_export_size, (
        "le fichier de l'étape 2 a été écrasé par le second export (qualité JPEG "
        "différente entre les préréglages « Moyenne » et « Taille maximale », donc "
        "taille de fichier différente attendue) : la résolution de collision "
        "{stem}_1.jpg n'a pas été appliquée"
    )
