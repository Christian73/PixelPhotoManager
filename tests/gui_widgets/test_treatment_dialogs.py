# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour treatment_dialogs.py.

Non-régression : lors de l'extraction de ce module depuis edit_panel.py
(découpage N4), l'import de `ImageAdjuster` (utilisé par
`GammaCurveWidget.paintEvent`) avait été omis, provoquant un
`NameError: name 'ImageAdjuster' is not defined` à chaque affichage de la
courbe (dialogue Couleurs). `grab()` déclenche paintEvent de façon
synchrone ; pytest-qt fait remonter les exceptions levées dans les
callbacks/événements Qt jusqu'au test."""
from src.ui.treatment_dialogs import GammaCurveWidget


def test_gamma_curve_widget_paints_without_error(qtbot):
    w = GammaCurveWidget(points=[(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
    qtbot.addWidget(w)
    w.resize(260, 260)
    img = w.grab()  # déclenche paintEvent
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
