# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Regression: the radio buttons were invisible (black on black) on the
pages of SettingsDialog.

Cause: as soon as an application style sheet exists, Qt switches to
QStyleSheetStyle and stops drawing the sub-controls natively. The dark theme
defined `QCheckBox::indicator` but not `QRadioButton::indicator`: the dot was
rendered in a near-black grey on the #1e1e1e background -- and once checked, in
a single tint identical to the background, hence literally invisible. The
earlier dialogs worked around the problem one by one (four local copies of
`_RADIO_STYLE`); the rule now lives in the global theme.

The rendering is really measured (grab() -> luminance contrast) rather than
through the presence of a string: that is the only way to observe that an
indicator *is seen*, and the "without the rules" test below confirms that they
are indeed what makes the difference."""
import pytest
from PySide6.QtWidgets import QRadioButton

from src.ui.theme import app_stylesheet

_BACKGROUND = 0xFF1E1E1E      # QWidget { background-color: #1e1e1e } of the theme
# Measurements observed: 18 / 0 before the fix (unchecked / checked), 89 / 132 after.
_VISIBLE = 50                 # beyond that, the indicator stands out clearly
_INVISIBLE = 25               # below that, it merges into the background

# Minimal sheet reproducing the cause: a dark background laid down by a
# style sheet, with no rule at all on the radio button indicator.
_LEGACY = "QMainWindow, QDialog, QWidget { background-color: #1e1e1e; color: #ddd; }"


def _luminance(argb: int) -> float:
    r, g, b = (argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF
    return 0.299 * r + 0.587 * g + 0.114 * b


def _indicator_contrast(checked: bool, stylesheet: str) -> float:
    """Maximum luminance difference between the rendering of a radio button
    *without a label* and the theme background: every non-background pixel comes
    from the indicator, so this difference measures its visibility exactly."""
    rb = QRadioButton("")
    rb.setStyleSheet(stylesheet)
    rb.setChecked(checked)
    rb.resize(rb.sizeHint())
    img = rb.grab().toImage()
    pixels = {
        img.pixel(x, y)
        for y in range(img.height())
        for x in range(img.width())
    }
    background = _luminance(_BACKGROUND)
    return max(abs(_luminance(p) - background) for p in pixels)


@pytest.fixture
def theme():
    # The check path only concerns QCheckBox; a dummy name is enough.
    return app_stylesheet("ppm_check.png")


class TestRadioIndicatorVisibility:
    @pytest.mark.parametrize("checked", [False, True])
    def test_indicator_is_visible_on_the_dark_background(self, qtbot, theme, checked):
        assert _indicator_contrast(checked, theme) >= _VISIBLE

    def test_checked_stands_out_more_than_unchecked(self, qtbot, theme):
        """The reported symptom: impossible to see which option is checked."""
        assert (_indicator_contrast(True, theme)
                > _indicator_contrast(False, theme))

    @pytest.mark.parametrize("checked", [False, True])
    def test_without_the_radio_rules_the_indicator_disappears(self, qtbot, checked):
        """Characterises the original bug. If this test were to fail, it would
        mean the diagnosis no longer holds and the fix has to be reconsidered."""
        assert _indicator_contrast(checked, _LEGACY) < _INVISIBLE


class TestAppStylesheet:
    def test_check_icon_path_is_interpolated(self):
        assert "image: url(C:/tmp/ppm_check.png)" in app_stylesheet("C:/tmp/ppm_check.png")

    @pytest.mark.parametrize("widget", ["QCheckBox", "QRadioButton"])
    def test_every_indicator_control_defines_its_states(self, widget, theme):
        """Every control whose indicator is drawn by the style must cover its
        states, on pain of reproducing the bug on another control."""
        for state in ("::indicator {", "::indicator:checked", "::indicator:unchecked"):
            assert f"{widget}{state}" in theme
