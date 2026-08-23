# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Isolated Qt widget tests (Layer 2, pytest-qt) for treatment_dialogs.py.

Non-regression: when this module was extracted from edit_panel.py
(split N4), the import of `ImageAdjuster` (used by
`GammaCurveWidget.paintEvent`) had been left out, causing a
`NameError: name 'ImageAdjuster' is not defined` on every display of the
curve (the Colours dialog). `grab()` triggers paintEvent
synchronously; pytest-qt propagates the exceptions raised inside Qt
callbacks/events up to the test."""
from src.ui.treatment_dialogs import GammaCurveWidget


def test_gamma_curve_widget_paints_without_error(qtbot):
    w = GammaCurveWidget(points=[(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
    qtbot.addWidget(w)
    w.resize(260, 260)
    img = w.grab()  # triggers paintEvent
    assert not img.isNull()


def test_gamma_curve_widget_paints_with_histogram(qtbot):
    w = GammaCurveWidget(
        points=[(0.0, 0.0), (0.3, 0.5), (1.0, 1.0)],
        histogram=[i / 255.0 for i in range(256)],
    )
    qtbot.addWidget(w)
    w.resize(260, 260)
    img = w.grab()
    assert not img.isNull()
