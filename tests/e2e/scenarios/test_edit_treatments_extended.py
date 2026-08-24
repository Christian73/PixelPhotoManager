# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: editing treatments not covered by
test_edit_nondestructive.py (which only tests Brightness + undo), chained
inside a single viewer on one witness photo.

Path exercised for each single-slider treatment (Contrast, Colours,
Straighten, Vignette): a real EditPanel button -> a real non-modal dialog ->
a real QSlider drag -> "Apply" -> persistence checked directly on edits.db
(never on the UI). Rotation/Mirror H/Mirror V: direct buttons, no dialog,
immediate persistence. Reset: immediate deletion of the photo_edits row
(without confirmation -- a reversible action through "Restore every edit",
cf. EditPanel.restore_all). Finally, the priority regression of the historical
NameError (commit 34d8c5e): GammaCurveWidget crashed on every render after a
file split that omitted an import -- reproduced here by really ticking the
two "Advanced options…" then "Expert options" check boxes of the Brightness
dialog (NOT by dragging a curve point: the bug happens in paintEvent, before
any interaction with the curve)."""
import pytest

from tests.e2e.conftest import (
    find_checkbox,
    find_dialog_button,
    invoke_button,
    open_photo_in_viewer,
    query_one,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e


def _row_exists(edits_db, photo_path) -> bool:
    return query_one(
        edits_db, "SELECT COUNT(*) FROM photo_edits WHERE photo_path=?", (str(photo_path),)
    ) == 1


def _column(edits_db, photo_path, column: str):
    return query_one(edits_db, f"SELECT {column} FROM photo_edits WHERE photo_path=?", (str(photo_path),))


def _set_slider(slider, value: float) -> None:
    """EditSlider exposes an internal QSlider scaled by x100 whatever the
    number of decimals displayed (cf. EditSlider._scale, edit_sliders.py) --
    the raw UIA value is therefore always value*100, including for Straighten
    (-10..10 degrees), whose display has only one decimal."""
    slider.set_value(int(round(value * 100)))


def _sliders(window):
    return window.descendants(control_type="Slider")


def _wait_for_n_sliders(window, n: int, timeout: float = 10.0):
    import time
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        last = _sliders(window)
        if len(last) >= n:
            return last
        time.sleep(0.3)
    raise LookupError(f"Seulement {len(last)} QSlider trouvé(s) après {timeout}s, {n} attendus")


def _slider_labeled(window, label_text: str, timeout: float = 10.0):
    """Identifies a QSlider through its neighbouring QLabel (same row,
    vertically aligned) rather than by position in window.descendants() --
    the UIA traversal order of the latter does not necessarily match the order
    in which the widgets were added to the layout (observed empirically: a
    Red/Green slider indexed positionally could receive the value meant for
    the other one)."""
    import time
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            labels = [t for t in window.descendants(control_type="Text") if t.window_text() == label_text]
            sliders = _sliders(window)
            if labels and sliders:
                lbl_rect = labels[0].rectangle()
                lbl_mid = (lbl_rect.top + lbl_rect.bottom) / 2
                for sl in sliders:
                    r = sl.rectangle()
                    if r.top <= lbl_mid <= r.bottom:
                        return sl
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Slider aligné avec le libellé {label_text!r} introuvable après {timeout}s ({last_exc})")


def test_edit_treatments_extended(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    edits_db = isolated_app.edits_db
    photo = manifest.control_photos[0]

    wait_for_condition(
        lambda: query_one(
            isolated_app.catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo),)
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    assert not _row_exists(edits_db, photo), "aucune retouche ne doit préexister sur la photo témoin"

    open_photo_in_viewer(window, photo)

    # ---- Contrast: generic dialog with a single slider ----
    # invoke_button (UIA Invoke pattern), not click_input(): this first click
    # immediately follows open_photo_in_viewer(), which has just moved
    # _left_stack onto the EditPanel synchronously (show_viewer()) -- the window
    # has not necessarily had the time to really become the OS foreground before
    # a simulated mouse click reaches the screen (same cause as the trap
    # documented on FolderManagerDialog, cf. the docstring of invoke_button):
    # confirmed empirically (temporary instrumentation) that click_input() here
    # never opens the dialog (no exception, no log), even though the button that
    # was found has a valid rectangle and state. The button stays visible after
    # the invocation (no dialog closing at this point) -> wait_gone=False.
    invoke_button(window, ["Contrast"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], 0.6)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "contrast") is not None
        and abs(_column(edits_db, photo, "contrast") - 0.6) < 0.02,
        timeout=20.0, message="le contraste n'a pas été persisté",
    )

    # ---- Colours: saturation + RGB (revealed by "Advanced options…") ----
    # invoke_button, same reason as for Contrast: this click immediately follows
    # the closing of the Contrast dialog (Apply), which gives the OS focus back to
    # the main window -- the same window of fragility as the very first opening
    # after open_photo_in_viewer (confirmed empirically: that same class of click
    # failed intermittently on Vignette during a later run of this test, cf. the
    # comment on Vignette further down).
    invoke_button(window, ["Colours"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], -0.3)   # saturation, always the first slider of the dialog
    find_checkbox(window, "Advanced options…", timeout=10.0).click_input()
    # Reveals the Red/Green/Blue sliders (CouleursTreatmentDialog,
    # treatment_dialogs.py:469-478) -- identified by their neighbouring label, not
    # by position (the order of window.descendants() does not reliably follow the
    # order in which they were added to the layout, cf. _slider_labeled).
    _wait_for_n_sliders(window, 4)
    sl_r = _slider_labeled(window, "Red")
    _set_slider(sl_r, 0.4)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "saturation") is not None
        and abs(_column(edits_db, photo, "saturation") - (-0.3)) < 0.02
        and abs(_column(edits_db, photo, "color_red") - 0.4) < 0.02,
        timeout=20.0, message="saturation/color_red n'ont pas été persistés",
    )

    # ---- Straighten: generic dialog, "Angle (deg)" slider ----
    # invoke_button: follows the closing of the Colours dialog (Apply), same
    # fragility as above.
    invoke_button(window, ["Straighten"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], 5.0)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "straighten") is not None
        and abs(_column(edits_db, photo, "straighten") - 5.0) < 0.15,
        timeout=20.0, message="le redressement n'a pas été persisté",
    )

    # ---- Vignette: dedicated dialog, "Intensity" slider only (never the
    # geometry handles on the canvas, out of scope) ----
    # invoke_button: follows the closing of the Straighten dialog (Apply), same
    # fragility as above -- this is precisely the click that failed
    # (intermittently, OS timing) during the initial diagnosis of this file.
    invoke_button(window, ["Vignette"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], 0.5)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "vignette_strength") is not None
        and abs(_column(edits_db, photo, "vignette_strength") - 0.5) < 0.02,
        timeout=20.0, message="l'intensité de vignette n'a pas été persistée",
    )

    # ---- Rotation / Mirror: direct buttons, immediate persistence ----
    # invoke_button for the rotation only: follows the closing of the Vignette
    # dialog (Apply), same fragility. The Mirror H/V/Reset buttons that follow
    # neither open nor close a window between them (the main window keeps the OS
    # foreground continuously): click_input stays reliable for them.
    invoke_button(window, ["↻", "+90°"], timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: _column(edits_db, photo, "rotation") == 90,
        timeout=20.0, message="la rotation +90° n'a pas été persistée",
    )
    # invoke_button for Mirror H/V/Reset: click_input turned out to be flaky on
    # fast sequences of direct buttons of the same kind (observed empirically on
    # Mirror V during the diagnosis of this file, with no window transition
    # identifiable as the cause -- general flakiness of SendInput on this
    # environment rather than an isolated structural trap).
    invoke_button(window, ["Mirror H"], exact=True, timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: _column(edits_db, photo, "flip_h") == 1,
        timeout=20.0, message="le miroir horizontal n'a pas été persisté",
    )
    invoke_button(window, ["Mirror V"], exact=True, timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: _column(edits_db, photo, "flip_v") == 1,
        timeout=20.0, message="le miroir vertical n'a pas été persisté",
    )

    # ---- Reset every edit: without confirmation (reversible), row deleted ----
    # Label on 2 lines (edit_panel.py): UIA returns the literal \n in window_text().
    invoke_button(window, ["Reset\nevery edit"], exact=True, timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: not _row_exists(edits_db, photo),
        timeout=20.0, message="la réinitialisation n'a pas supprimé la ligne photo_edits",
    )

    # ---- GammaCurveWidget regression (commit 34d8c5e): merely RENDERING the
    # widget crashed with a NameError before the fix -- reproduce the real
    # sequence of the two check boxes, not a drag of a point. ----
    invoke_button(window, ["Brightness"], exact=True, timeout=15.0, wait_gone=False)
    find_checkbox(window, "Advanced options…", timeout=10.0).click_input()
    find_checkbox(window, "Expert options", timeout=10.0).click_input()

    assert isolated_app.app.process.poll() is None, (
        "l'application a quitté de manière inattendue au rendu de GammaCurveWidget "
        "(régression du NameError historique, commit 34d8c5e)"
    )
    assert window.exists(), "la fenêtre principale n'a pas survécu au rendu de GammaCurveWidget"

    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "gamma_use_curve") == 1,
        timeout=20.0, message="gamma_use_curve n'a pas été persisté après validation de la courbe",
    )
