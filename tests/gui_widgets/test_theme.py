# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression : les boutons radio étaient invisibles (noir sur noir) sur les
pages de SettingsDialog.

Cause : dès qu'une feuille de style applicative existe, Qt bascule sur
QStyleSheetStyle et cesse de dessiner nativement les sous-contrôles. Le thème
sombre définissait `QCheckBox::indicator` mais pas `QRadioButton::indicator` :
la pastille était rendue en gris quasi noir sur le fond #1e1e1e — et une fois
cochée, en une seule teinte identique au fond, donc littéralement invisible. Les
dialogues antérieurs contournaient le problème un par un (quatre copies locales
de `_RADIO_STYLE`) ; la règle est désormais dans le thème global.

Le rendu est mesuré réellement (grab() → contraste de luminance) plutôt que par
une présence de chaîne : c'est le seul moyen de constater qu'un indicateur *se
voit*, et le test « sans les règles » ci-dessous confirme que ce sont bien elles
qui font la différence."""
import pytest
from PySide6.QtWidgets import QRadioButton

from src.ui.theme import app_stylesheet

_BACKGROUND = 0xFF1E1E1E      # QWidget { background-color: #1e1e1e } du thème
# Mesures observées : 18 / 0 avant correctif (décoché / coché), 89 / 132 après.
_VISIBLE = 50                 # au-delà, l'indicateur se détache franchement
_INVISIBLE = 25               # en deçà, il se confond avec le fond

# Feuille minimale reproduisant la cause : un fond sombre posé par feuille de
# style, sans aucune règle sur l'indicateur du bouton radio.
_LEGACY = "QMainWindow, QDialog, QWidget { background-color: #1e1e1e; color: #ddd; }"


def _luminance(argb: int) -> float:
    r, g, b = (argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF
    return 0.299 * r + 0.587 * g + 0.114 * b


def _indicator_contrast(checked: bool, stylesheet: str) -> float:
    """Écart de luminance maximal entre le rendu d'un bouton radio *sans
    libellé* et le fond du thème : tout pixel non-fond provient de l'indicateur,
    donc cet écart mesure exactement sa visibilité."""
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
    # Le chemin de la coche ne concerne que les QCheckBox ; un nom bidon suffit.
    return app_stylesheet("ppm_check.png")


class TestRadioIndicatorVisibility:
    @pytest.mark.parametrize("checked", [False, True])
    def test_indicator_is_visible_on_the_dark_background(self, qtbot, theme, checked):
        assert _indicator_contrast(checked, theme) >= _VISIBLE

    def test_checked_stands_out_more_than_unchecked(self, qtbot, theme):
        """Le symptôme rapporté : impossible de voir quelle option est cochée."""
        assert (_indicator_contrast(True, theme)
                > _indicator_contrast(False, theme))

    @pytest.mark.parametrize("checked", [False, True])
    def test_without_the_radio_rules_the_indicator_disappears(self, qtbot, checked):
        """Caractérise le bug d'origine. Si ce test venait à échouer, c'est que
        le diagnostic n'est plus valable et que le correctif est à revoir."""
        assert _indicator_contrast(checked, _LEGACY) < _INVISIBLE


class TestAppStylesheet:
    def test_check_icon_path_is_interpolated(self):
        assert "image: url(C:/tmp/ppm_check.png)" in app_stylesheet("C:/tmp/ppm_check.png")

    @pytest.mark.parametrize("widget", ["QCheckBox", "QRadioButton"])
    def test_every_indicator_control_defines_its_states(self, widget, theme):
        """Tout contrôle dont l'indicateur est dessiné par le style doit couvrir
        ses états, sous peine de reproduire le bug sur un autre contrôle."""
        for state in ("::indicator {", "::indicator:checked", "::indicator:unchecked"):
            assert f"{widget}{state}" in theme
