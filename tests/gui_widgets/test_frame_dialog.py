# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Ironwork settings of the second frame in FrameDialog.

The dialog is built with no photo path: the gallery of previews
(`_TileLoader`, a QThread) is never started, only the controls are tested.
The visibilities are queried with `isVisibleTo()` -- the dialog itself is not
displayed, so `isVisible()` would be false everywhere.
"""
import pytest

from src.core.models import EditInfo
from src.processing import frames
from src.ui.frame_dialog import FrameDialog


@pytest.fixture
def dialog(qtbot):
    dlg = FrameDialog(EditInfo(frame_type="plain", frame_inner_enabled=True))
    qtbot.addWidget(dlg)
    return dlg


class TestIronworkVisibility:
    def test_hidden_for_other_frame_types(self, dialog):
        """The ironwork only exists on the second frame of the plain surround."""
        dialog._select_kind("double")
        assert not dialog._inner_motif_rows.isVisibleTo(dialog)

    def test_hidden_while_the_second_frame_is_off(self, dialog):
        dialog._set_inner_enabled(False)
        assert not dialog._inner_motif_rows.isVisibleTo(dialog)

    def test_shown_with_the_second_frame(self, dialog):
        assert dialog._inner_motif_rows.isVisibleTo(dialog)

    def test_relief_and_ornaments_are_reserved_to_ornamented_motifs(self, dialog):
        """The simple line is rendered as a strict flat fill and has no ornament
        to size: its two settings would stay without effect."""
        dialog._set_inner_motif("line")
        assert not dialog._relief_row.isVisibleTo(dialog)
        assert not dialog._sl_ornament.isVisibleTo(dialog)

        dialog._set_inner_motif("scrolls")
        assert dialog._relief_row.isVisibleTo(dialog)
        assert dialog._sl_ornament.isVisibleTo(dialog)


class TestIronworkControls:
    def test_every_motif_has_a_button(self, dialog):
        assert set(dialog._motif_buttons) == set(frames.INNER_MOTIF_LABELS)

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_clicking_a_motif_selects_it_alone(self, dialog, qtbot, motif):
        """A real click: `clicked` emits a bool that would clobber the default of
        a badly written positional lambda (a trap experienced elsewhere in the
        project)."""
        with qtbot.waitSignal(dialog.preview, timeout=500) as blocker:
            dialog._motif_buttons[motif].click()
        assert dialog._edit.frame_inner_motif == motif
        assert blocker.args[0].frame_inner_motif == motif
        checked = {m for m, b in dialog._motif_buttons.items() if b.isChecked()}
        assert checked == {motif}

    def test_relief_buttons_are_exclusive(self, dialog, qtbot):
        dialog._set_inner_motif("twist")
        with qtbot.waitSignal(dialog.preview, timeout=500) as blocker:
            dialog._relief_buttons[False].click()
        assert dialog._edit.frame_inner_relief is False
        assert blocker.args[0].frame_inner_relief is False
        assert dialog._relief_buttons[True].isChecked() is False

        dialog._relief_buttons[True].click()
        assert dialog._edit.frame_inner_relief is True
        assert dialog._relief_buttons[False].isChecked() is False

    def test_ornament_slider_is_a_percentage_of_the_scale_factor(self, dialog):
        """The internal scale of EditSlider is hard-wired to 100: the slider
        therefore exposes 40-250 % for a 0.4-2.5 factor."""
        raw = dialog._sl_ornament._slider
        assert raw.minimum() / 100.0 == pytest.approx(frames.INNER_ORNAMENT_MIN * 100.0)
        assert raw.maximum() / 100.0 == pytest.approx(frames.INNER_ORNAMENT_MAX * 100.0)
        assert dialog._sl_ornament.get_value() == pytest.approx(100.0)   # factor 1
        raw.setValue(17000)
        assert dialog._edit.frame_inner_ornament == pytest.approx(1.7)

    def test_settings_survive_validation(self, dialog):
        """`get_edit()` returns the EditInfo applied by the edit panel."""
        dialog._set_inner_motif("studs")
        dialog._set_inner_relief(False)
        dialog._sl_ornament._slider.setValue(6000)
        result = dialog.get_edit()
        assert result.frame_inner_motif == "studs"
        assert result.frame_inner_relief is False
        assert result.frame_inner_ornament == pytest.approx(0.6)
