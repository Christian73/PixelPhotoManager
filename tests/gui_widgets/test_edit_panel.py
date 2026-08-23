# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Isolated Qt widget tests (Layer 2, pytest-qt) for EditPanel/EditSlider.

`EditPanel.__init__` instantiates `EditDatabase()` with its default path
(no injectable `db_path=`) -- the redirection of `%LOCALAPPDATA%` by
`tests/conftest.py` (loaded before any test import) is what guarantees that
these tests never write into the real user profile."""
from PySide6.QtWidgets import QMessageBox

from src.core.models import PhotoInfo
from src.ui.edit_panel import VIGNETTE_DEFAULT_STRENGTH, EditPanel, EditSlider


def _photo(path: str) -> PhotoInfo:
    return PhotoInfo(path=path)


class TestEditSlider:
    def test_default_value(self, qtbot):
        slider = EditSlider("Luminosité", -1.0, 1.0, 0.25, 2)
        qtbot.addWidget(slider)
        assert slider.get_value() == 0.25

    def test_underlying_qslider_change_emits_value_changed(self, qtbot):
        slider = EditSlider("Luminosité", -1.0, 1.0, 0.0, 2)
        qtbot.addWidget(slider)

        with qtbot.waitSignal(slider.value_changed, timeout=1000) as blocker:
            slider._slider.setValue(50)  # scale=100 -> 0.50
        assert blocker.args == [0.5]
        assert slider.get_value() == 0.5

    def test_set_value_does_not_emit_signal(self, qtbot):
        """set_value() blocks the signals (cf. edit_panel.py): used to
        resynchronise the display without retriggering an edit cycle."""
        slider = EditSlider("Luminosité", -1.0, 1.0, 0.0, 2)
        qtbot.addWidget(slider)

        received = []
        slider.value_changed.connect(received.append)
        slider.set_value(0.8)
        assert received == []
        assert slider.get_value() == 0.8


class TestEditPanelUndoRedo:
    def _make_panel(self, qtbot) -> EditPanel:
        panel = EditPanel()
        qtbot.addWidget(panel)
        return panel

    def test_set_photo_starts_with_empty_undo_stack(self, qtbot):
        panel = self._make_panel(qtbot)
        panel.set_photo(_photo("C:/lib/photo1.jpg"))
        assert panel._undo_stack == []
        assert panel._redo_stack == []

    def test_push_undo_then_undo_restores_previous_edit_and_emits_signal(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        original = panel.get_edit()
        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4

        assert panel._undo_stack, "l'état pré-modification doit être empilé"

        with qtbot.waitSignal(panel.edits_changed, timeout=1000) as blocker:
            panel.undo()
        assert blocker.args == [original]
        assert panel._edit.brightness == original.brightness
        assert panel._redo_stack, "redo doit être disponible après un undo"

    def test_redo_after_undo_restores_modified_edit(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        modified = panel.get_edit()

        panel.undo()
        with qtbot.waitSignal(panel.edits_changed, timeout=1000) as blocker:
            panel.redo()
        assert blocker.args == [modified]
        assert panel._edit.brightness == 0.4

    def test_undo_persists_across_new_edit_panel_instance(self, qtbot, tmp_path):
        """Undo/redo persistent across sessions: the history is reloaded from the
        DB (`EditDatabase`) when a photo is opened, cf. CLAUDE.md."""
        photo = _photo(str(tmp_path / "photo1.jpg"))

        panel1 = self._make_panel(qtbot)
        panel1.set_photo(photo)
        panel1._checkpoint("Luminosité")
        panel1._push_undo("Luminosité")
        panel1._edit.brightness = 0.6
        panel1._save("Luminosité")

        panel2 = self._make_panel(qtbot)
        panel2.set_photo(photo)
        assert panel2._undo_stack, "l'historique doit être rechargé depuis la DB"


class TestRotationStepped:
    """Regression: `rotation_stepped` resynchronises the face index with the
    orientation actually displayed. It was only emitted by the rotate buttons --
    a Ctrl+Z (or a reset/restore) bringing the rotation back to 0 degrees left
    `indexed_photos.rotation` frozen on the old value, and re-detection ran
    forever in a stale orientation (real case: 2 faces found out of 8, even in
    forced detection)."""

    def _make_panel(self, qtbot, tmp_path) -> tuple[EditPanel, str]:
        panel = EditPanel()
        qtbot.addWidget(panel)
        path = str(tmp_path / "photo1.jpg")
        panel.set_photo(_photo(path))
        return panel, path

    def test_undo_of_rotation_emits_rotation_stepped(self, qtbot, tmp_path):
        panel, path = self._make_panel(qtbot, tmp_path)
        received: list = []
        panel.rotation_stepped.connect(lambda p, r: received.append((p, r)))

        panel._rotate_cw()               # 0 -> 90 degrees (emitted by the button)
        assert received == [(path, 90)]

        panel.undo()                     # 90 -> 0 degrees: must re-emit
        assert received == [(path, 90), (path, 0)]

    def test_redo_of_rotation_emits_rotation_stepped(self, qtbot, tmp_path):
        panel, path = self._make_panel(qtbot, tmp_path)
        panel._rotate_cw()
        panel.undo()
        received: list = []
        panel.rotation_stepped.connect(lambda p, r: received.append((p, r)))

        panel.redo()                     # 0 -> 90 degrees
        assert received == [(path, 90)]

    def test_reset_and_restore_all_emit_rotation_stepped(self, qtbot, tmp_path):
        panel, path = self._make_panel(qtbot, tmp_path)
        panel._rotate_cw()
        received: list = []
        panel.rotation_stepped.connect(lambda p, r: received.append((p, r)))

        panel.reset_all()                # 90 -> 0 degrees
        assert received == [(path, 0)]

        panel.restore_all()              # 0 -> 90 degrees
        assert received == [(path, 0), (path, 90)]

    def test_undo_without_rotation_change_is_silent(self, qtbot, tmp_path):
        """An undo that does not touch the rotation must not restart a
        (costly) detection: no emission."""
        panel, path = self._make_panel(qtbot, tmp_path)
        received: list = []
        panel.rotation_stepped.connect(lambda p, r: received.append((p, r)))

        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        panel.undo()

        assert received == []


class TestEditPanelContentMinWidth:
    """Regression: the 2-column grid of treatment buttons (Contraste,
    Vignette... in column 2) must never be cut off by the QScrollArea that hosts
    it. Real observed bug (not an artefact of the e2e automation): `QScrollArea`
    does not propagate the `minimumSizeHint()` of its content to its own (cf. the
    comment on `scroll.setMinimumWidth` in `edit_panel.py::_setup_ui`) -- without
    an explicit floor, a panel as narrow as the sidebar left column 2 partly
    outside the visible viewport: invisible and unreachable by click for a real
    user, not only for an automated test. `content_min_width()` is the contract
    that `main_window.py::_ensure_left_pane_min_width()` relies on to size the
    splitter; this test checks that contract directly, without depending on the
    splitter or on OS automation (unlike the e2e scenario
    `test_edit_treatments_extended.py`, which clicks through UIA Invoke -- hence
    blind to a visual geometry defect)."""

    def _make_panel(self, qtbot) -> EditPanel:
        panel = EditPanel()
        qtbot.addWidget(panel)
        panel.show()
        qtbot.waitExposed(panel)
        return panel

    def test_content_min_width_avoids_horizontal_clipping(self, qtbot):
        panel = self._make_panel(qtbot)
        panel.resize(panel.content_min_width(), 600)
        qtbot.wait(50)

        viewport_width = panel._scroll.viewport().width()
        inner_min_width = panel._scroll_inner.minimumSizeHint().width()
        assert viewport_width >= inner_min_width, (
            f"content_min_width() ({panel.content_min_width()}) est insuffisant : "
            f"le viewport ({viewport_width}px) reste plus étroit que le contenu "
            f"({inner_min_width}px) — la colonne 2 de boutons serait coupée"
        )

    def test_content_min_width_keeps_second_column_buttons_in_viewport(self, qtbot):
        panel = self._make_panel(qtbot)
        panel.resize(panel.content_min_width(), 600)
        qtbot.wait(50)

        viewport_width = panel._scroll.viewport().width()
        # Column 2 of the grid (odd idx in _TREATMENTS, cf. _setup_ui:
        # grid.addWidget(btn, idx // 2, idx % 2)) -- Contraste/Vignette in the
        # current order of _TREATMENTS.
        for name, btn in panel._treatment_buttons.items():
            right_edge = btn.geometry().right()
            assert right_edge <= viewport_width, (
                f"le bouton {name!r} déborde du viewport de la QScrollArea "
                f"({right_edge}px > {viewport_width}px) — colonne clippée"
            )


class TestEditPanelResetRestore:
    def _make_panel(self, qtbot) -> EditPanel:
        panel = EditPanel()
        qtbot.addWidget(panel)
        return panel

    def test_reset_all_does_not_prompt_for_confirmation(self, qtbot, tmp_path, monkeypatch):
        """Regression: reset_all() is reversible through restore_all(), so it must
        no longer show a confirmation popup (cf. QMessageBox.question removed)."""
        def _fail_if_called(*a, **k):
            raise AssertionError("reset_all() ne doit plus afficher de QMessageBox de confirmation")
        monkeypatch.setattr(QMessageBox, "question", _fail_if_called)
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._edit.brightness = 0.4
        panel.reset_all()
        assert not panel._edit.is_modified()

    def test_reset_all_then_restore_all_brings_back_the_edit(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        panel._save("brightness")
        modified = panel.get_edit()

        assert not panel._btn_restore.isEnabled()
        panel.reset_all()
        assert not panel._edit.is_modified()
        assert panel._btn_restore.isEnabled(), "reset_all() doit rendre le bouton restore disponible"

        with qtbot.waitSignal(panel.edits_changed, timeout=1000) as blocker:
            panel.restore_all()
        assert blocker.args == [modified]
        assert panel._edit.brightness == 0.4
        assert not panel._btn_restore.isEnabled(), "l'instantané est consommé après restauration"

        panel2 = self._make_panel(qtbot)
        panel2.set_photo(photo)
        assert panel2.get_edit().brightness == 0.4, "la restauration doit être persistée en DB"

    def test_reset_all_then_restore_all_keeps_step_by_step_undo(self, qtbot, tmp_path):
        """Regression: after reset_all() + restore_all(), the edits must be
        undoable one by one (the history used to be lost)."""
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        panel._save("brightness")

        panel._checkpoint("Contraste")
        panel._push_undo("Contraste")
        panel._edit.contrast = 0.2
        panel._save("contrast")

        panel.reset_all()
        panel.restore_all()

        assert panel._btn_undo.isEnabled(), "l'undo doit rester disponible après restauration"
        assert len(panel._undo_stack) == 2

        panel.undo()          # undoes the contrast
        assert panel._edit.contrast == 0.0
        assert panel._edit.brightness == 0.4
        panel.undo()          # undoes the brightness
        assert panel._edit.brightness == 0.0
        assert not panel._btn_undo.isEnabled()

    def test_restore_all_repopulates_persistent_history(self, qtbot, tmp_path):
        """The DB history erased by reset_all() is reinjected by restore_all()
        -> step-by-step undo survives a restart."""
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        panel._save("brightness")

        panel.reset_all()
        panel.restore_all()

        panel2 = self._make_panel(qtbot)
        panel2.set_photo(photo)          # reloads the history from the DB
        assert panel2.get_edit().brightness == 0.4
        assert panel2._undo_stack, "l'historique doit être rechargeable depuis la DB"
        panel2.undo()
        assert panel2.get_edit().brightness == 0.0

    def test_restore_all_without_prior_reset_is_a_noop(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._edit.brightness = 0.3
        panel.restore_all()
        assert panel._edit.brightness == 0.3

    def test_new_edit_after_reset_invalidates_restore_snapshot(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        panel._save("brightness")

        panel.reset_all()
        assert panel._btn_restore.isEnabled()

        panel._checkpoint("Rotation +90°")
        panel._push_undo("Rotation +90°")
        panel._edit.rotation = 90

        assert not panel._btn_restore.isEnabled(), (
            "une nouvelle retouche après reset_all() doit invalider l'instantané de restauration"
        )


class TestVignetteDefaultStrength:
    """The Vignette tool opens on a visible intensity (50 %) when the photo has
    none yet: at 0, opening it changed nothing in the image and the slider had to
    be moved to see the effect. The default value of `EditInfo` stays 0 -- it is
    really the opening of the tool that sets this starting point."""

    def _panel_with_photo(self, qtbot, tmp_path) -> EditPanel:
        panel = EditPanel()
        qtbot.addWidget(panel)
        panel.set_photo(_photo(str(tmp_path / "photo1.jpg")))
        return panel

    def test_opening_the_tool_sets_strength_to_the_default(self, qtbot, tmp_path):
        panel = self._panel_with_photo(qtbot, tmp_path)
        assert panel._edit.vignette_strength == 0.0

        panel._open_vignette_treatment()
        dlg = panel._active_vignette_dlg
        qtbot.addWidget(dlg)

        assert panel._edit.vignette_strength == VIGNETTE_DEFAULT_STRENGTH
        assert dlg._sl_strength.get_value() == VIGNETTE_DEFAULT_STRENGTH
        dlg.reject()

    def test_an_existing_vignette_is_not_overwritten(self, qtbot, tmp_path):
        panel = self._panel_with_photo(qtbot, tmp_path)
        panel._edit.vignette_strength = 0.2

        panel._open_vignette_treatment()
        dlg = panel._active_vignette_dlg
        qtbot.addWidget(dlg)

        assert panel._edit.vignette_strength == 0.2
        assert dlg._sl_strength.get_value() == 0.2
        dlg.reject()

    def test_cancelling_restores_the_absence_of_vignette(self, qtbot, tmp_path):
        panel = self._panel_with_photo(qtbot, tmp_path)

        panel._open_vignette_treatment()
        dlg = panel._active_vignette_dlg
        qtbot.addWidget(dlg)
        dlg.reject()

        assert panel._edit.vignette_strength == 0.0, (
            "annuler l'outil doit revenir à l'état d'avant ouverture, pas laisser "
            "le point de départ de 50 %"
        )
