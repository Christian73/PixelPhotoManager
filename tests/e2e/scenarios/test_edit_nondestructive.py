# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: non-destructive editing from end to end, in the
real viewer.

The path exercised: double-click on a thumbnail (photo_activated) -> show_viewer
switches _left_stack to the real EditPanel -> click on the treatment button
"Brightness" (a QToolButton, cf. edit_panel.py::_TREATMENTS/_make_treatment_button)
-> LuminositeTreatmentDialog opens (a non-modal QDialog, dlg.show()) ->
drag of the internal QSlider (EditSlider -> MarkedSlider -> QSlider, the only
visible slider as long as "Advanced options…" is not ticked) -> "Apply" ->
EditPanel._finish() persists through EditDatabase.save() (the photo_edits table,
the brightness column) -> checked directly on edits.db, NOT on the UI (the
only source of truth for the non-regression, cf. tests/e2e/conftest.py).

Then Ctrl+Z (the "Undo" button of the EditPanel, the real Ctrl+Z shortcut,
edit_panel.py:1514) -> checked again in the database -> reopening the same
photo (a logically new viewer instance, undo_stack reloaded from the DB,
cf. CLAUDE.md "Non-destructive editing") -> the persistence of the undo
checked again."""
import pytest

from tests.e2e.conftest import double_click_element, open_photo_in_viewer, find_dialog_button, find_thumbnail, query_one, wait_for_condition

pytestmark = pytest.mark.e2e

_BRIGHTNESS_TARGET = 0.7  # slider scale=100 -> QSlider.setValue(70)


def _brightness(edits_db, photo_path) -> float | None:
    return query_one(edits_db, "SELECT brightness FROM photo_edits WHERE photo_path=?", (str(photo_path),))


def test_luminosity_edit_applies_persists_and_undoes(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    edits_db = isolated_app.edits_db
    photo = manifest.control_photos[0]

    # The initial scan must have finished for the thumbnail to exist in the grid.
    wait_for_condition(
        lambda: query_one(
            isolated_app.catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo),)
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    assert _brightness(edits_db, photo) is None, "aucune retouche ne doit préexister sur la photo témoin"

    open_photo_in_viewer(window, photo)

    # The "Brightness" treatment button (a QToolButton, exact text) -- a descendant
    # of the main window, appears once the EditPanel is displayed (_left_stack -> index 1).
    btn_luminosite = find_dialog_button(window, ["Brightness"], exact=True, timeout=15.0)
    btn_luminosite.click_input()

    # LuminositeTreatmentDialog: a single visible QSlider as long as "Advanced
    # options…" is not ticked (the gamma slider is hidden by default).
    slider = _wait_for_slider(window)
    slider.set_value(int(_BRIGHTNESS_TARGET * 100))

    find_dialog_button(window, ["Apply"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: _brightness(edits_db, photo) is not None
        and abs(_brightness(edits_db, photo) - _BRIGHTNESS_TARGET) < 0.02,
        timeout=20.0,
        message="la retouche de luminosité n'a pas été persistée dans edits.db",
    )

    # Undo (the undo button of the EditPanel). A DYNAMIC label since the
    # UI evolved: "Undo  <operation>" (e.g. "Undo  Brightness"),
    # hence the non-exact search -- the "Cancel" of the treatment dialog
    # is closed by that point, so there is no ambiguity.
    find_dialog_button(window, ["Undo"], exact=False, timeout=10.0).click_input()

    # Pristine state restored: since EditDatabase evolved, a photo
    # back to its original state has its photo_edits row DELETED (and not
    # brightness=0) -- the absence of a row is therefore the expected success.
    wait_for_condition(
        lambda: _brightness(edits_db, photo) is None or abs(_brightness(edits_db, photo)) < 0.02,
        timeout=20.0,
        message="l'annulation (undo) n'a pas restauré la luminosité d'origine dans edits.db",
    )

    # Re-navigation (back to the grid then reopening): the persistence of
    # the undo must not depend on the in-memory state of the EditPanel.
    # The "✕" button of the viewer (PhotoViewer -> closed -> show_grid) -- the
    # "▦" button of the status bar is hidden in viewer mode.
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()
    open_photo_in_viewer(window, photo)
    b = _brightness(edits_db, photo)
    assert b is None or abs(b) < 0.02


def _wait_for_slider(window, timeout: float = 10.0):
    import time
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            sliders = window.descendants(control_type="Slider")
            if sliders:
                return sliders[0].wrapper_object() if hasattr(sliders[0], "wrapper_object") else sliders[0]
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Aucun QSlider trouvé dans le dialogue Luminosité après {timeout}s ({last_exc})")
