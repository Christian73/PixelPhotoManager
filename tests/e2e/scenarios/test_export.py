# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: exporting an edited photo from the viewer.

The path exercised: double-click on a thumbnail -> viewer -> Brightness edit
(the same mechanics as test_edit_nondestructive.py) -> toolbar button
"⬆  Export" (main_window.py:816, ``_on_export_clicked``) -> ``_ExportDialog``
(modal, ``exec()``) -> the ``_dir_edit`` field (a QLineEdit whose default text
is ``Path.home()/Pictures/PixelPhotoManager/Export``!) is **explicitly
overwritten** towards an isolated folder under the ``tmp_path`` of the test --
never let this scenario write into the real Pictures folder of the user, cf. the
isolation principle of this whole test module -> "Export" (the OK button,
exact text, distinct from the toolbar button which carries the "⬆" glyph).

Checks on the produced `.jpg` file, not on the UI:
- it exists;
- it is measurably brighter than the original (delta of the mean luminance),
  proof that the non-destructive edit really was baked in on export
  (``_run_export``: ``ImageAdjuster.apply_all`` if ``edit.is_modified()``);
- its file dates (mtime) are those of the original
  (``preserve_file_dates``), not the creation date of the export."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from tests.e2e.conftest import double_click_element, open_photo_in_viewer, find_dialog_button, find_thumbnail, query_one, wait_for_condition

pytestmark = pytest.mark.e2e

_BRIGHTNESS_TARGET = 0.7


def _mean_luminance(path: Path) -> float:
    with Image.open(path) as img:
        return float(np.asarray(img.convert("L"), dtype=np.float64).mean())


def _set_export_dir(window, path: Path) -> None:
    """Overwrites the destination field of `_ExportDialog` (a QLineEdit whose
    default text points at the real Pictures folder of the user -- see the
    warning at the top of the file). Identified by its default content rather
    than by index, so as not to depend on the order of the QLineEdits in the
    UIA tree (the EXIF panel also displays QLineEdits while the viewer is
    open)."""
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


def test_export_bakes_in_edit_and_preserves_dates(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo = manifest.control_photos[0]

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    original_mtime = Path(photo).stat().st_mtime
    original_luminance = _mean_luminance(photo)

    open_photo_in_viewer(window, photo)

    find_dialog_button(window, ["Brightness"], exact=True, timeout=15.0).click_input()
    sliders = []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not sliders:
        sliders = window.descendants(control_type="Slider")
        time.sleep(0.3)
    assert sliders, "le slider de luminosité n'est pas apparu"
    sliders[0].set_value(int(_BRIGHTNESS_TARGET * 100))
    find_dialog_button(window, ["Apply"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: query_one(
            isolated_app.edits_db, "SELECT brightness FROM photo_edits WHERE photo_path=?", (str(photo),)
        ) is not None,
        timeout=20.0, message="la retouche préalable à l'export n'a pas été persistée",
    )

    # Isolated export folder: catalog_db = tmp_path/app_data/PixelPhotoManager/catalog.db
    export_dir = catalog_db.parents[2] / "export_out"

    # Toolbar button (the "⬆" glyph + text) -- a single button contains "Export" before the dialog opens.
    find_dialog_button(window, ["Export"], exact=False, timeout=10.0).click_input()
    _set_export_dir(window, export_dir)
    # OK button of the dialog: the exact text "Export" (no glyph), distinct from the toolbar button.
    find_dialog_button(window, ["Export"], exact=True, timeout=10.0).click_input()

    dest = export_dir / (Path(photo).stem + ".jpg")

    def _exported() -> bool:
        return dest.exists() and dest.stat().st_size > 0

    wait_for_condition(_exported, timeout=30.0, message=f"le fichier exporté {dest} n'est jamais apparu")

    exported_luminance = _mean_luminance(dest)
    assert exported_luminance > original_luminance + 5.0, (
        f"la retouche de luminosité (+{_BRIGHTNESS_TARGET}) ne semble pas incrustée à l'export "
        f"(original={original_luminance:.1f}, exporté={exported_luminance:.1f})"
    )

    exported_mtime = dest.stat().st_mtime
    assert abs(exported_mtime - original_mtime) < 5.0, (
        "preserve_file_dates() n'a pas reporté la date de l'original sur le fichier exporté "
        f"(original={original_mtime}, exporté={exported_mtime})"
    )
