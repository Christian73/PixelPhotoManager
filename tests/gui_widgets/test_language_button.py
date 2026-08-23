# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) du sélecteur de langue de la barre du haut.

Deux choses seulement méritent d'être verrouillées ici, et ce sont les deux qui
cassent en silence :

- **le drapeau se voit** — il est dessiné par code, donc rien n'échoue si le
  tracé produit un carré uniforme ; le test mesure les couleurs réellement
  rendues, comme `test_theme.py` mesure le contraste d'un indicateur ;
- **le clic écrit `ui.language`** — le bouton et Paramètres › Langue sont deux
  points d'entrée sur la même clé de config, et un `QMessageBox` s'ouvre au
  passage (neutralisé ici).
"""
import pytest

import src.core.config as config_module
from src.core import i18n
from src.core.config import Config
from src.ui import language_button as lb_module
from src.ui.flag_icons import flag_pixmap
from src.ui.language_button import LanguageButton


@pytest.fixture
def config(tmp_path, monkeypatch):
    Config._instance = None
    monkeypatch.setattr(config_module, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_module, "APP_DATA_DIR", tmp_path)
    yield Config()
    Config._instance = None


@pytest.fixture(autouse=True)
def _silence_messagebox(monkeypatch):
    """`_select()` informe du redémarrage — sans cela le test bloque dessus.

    Substitution dans l'espace de noms du module, et non `setattr` sur la
    classe : un `setattr` direct sur une classe Shiboken n'intercepte pas
    l'appel natif (même raison que `captured_menus` dans
    `test_album_mode_no_delete.py`)."""
    class _MuteMessageBox:
        @staticmethod
        def information(*a, **k):
            return None

    monkeypatch.setattr(lb_module, "QMessageBox", _MuteMessageBox)


def _colors(code):
    """Couleurs distinctes présentes dans la vignette (fond transparent exclu)."""
    img = flag_pixmap(code).toImage()
    return {
        img.pixel(x, y) & 0xFFFFFF
        for y in range(img.height()) for x in range(img.width())
        if (img.pixel(x, y) >> 24) & 0xFF
    }


class TestFlagIcons:
    @pytest.mark.parametrize("code", ["en", "fr", "de"])
    def test_flag_is_drawn_and_not_a_flat_square(self, qapp, code):
        # Un tracé raté (mauvais ordre de peinture, brosse oubliée) donne un
        # aplat : c'est le seul échec possible, il ne lève aucune exception.
        assert len(_colors(code)) > 3

    def test_each_language_has_its_own_flag(self, qapp):
        rendered = {c: flag_pixmap(c).toImage() for c in ("en", "fr", "de")}
        assert rendered["en"] != rendered["fr"] != rendered["de"]
        assert rendered["en"] != rendered["de"]

    def test_unknown_code_falls_back_to_english(self, qapp):
        assert flag_pixmap("xx").toImage() == flag_pixmap("en").toImage()

    def test_pixmap_is_cached(self, qapp):
        assert flag_pixmap("fr").cacheKey() == flag_pixmap("fr").cacheKey()


class TestLanguageButton:
    def test_shows_the_configured_language(self, qtbot, config):
        config.set(i18n.CONFIG_KEY, "de")
        btn = LanguageButton(config)
        qtbot.addWidget(btn)

        assert btn.current_code() == "de"
        assert not btn.icon().isNull()

    def test_selection_persists_the_language(self, qtbot, config):
        # Langue de départ posée explicitement : un test qui s'appuie sur le
        # défaut de `Config` teste ce défaut autant que le bouton.
        config.set(i18n.CONFIG_KEY, "en")
        btn = LanguageButton(config)
        qtbot.addWidget(btn)
        with qtbot.waitSignal(btn.language_changed) as sig:
            btn._select("fr")

        assert sig.args == ["fr"]
        assert config.get(i18n.CONFIG_KEY) == "fr"
        assert btn.current_code() == "fr"

    def test_selecting_the_current_language_is_a_no_op(self, qtbot, config):
        config.set(i18n.CONFIG_KEY, "fr")
        btn = LanguageButton(config)
        qtbot.addWidget(btn)
        with qtbot.assertNotEmitted(btn.language_changed):
            btn._select("fr")

    def test_tooltip_announces_the_pending_restart(self, qtbot, config):
        """Le drapeau change tout de suite, l'interface non : l'infobulle est le
        seul endroit qui porte encore l'information une fois le message fermé."""
        config.set(i18n.CONFIG_KEY, i18n.active_language())   # "en" hors catalogue
        btn = LanguageButton(config)
        qtbot.addWidget(btn)
        before = btn.toolTip()
        btn._select("de")

        assert "Deutsch" in btn.toolTip()
        # Le nom du produit n'apparaît que dans la variante « au prochain
        # démarrage » : comparer les longueurs ne dirait rien, « Français » est
        # plus long que « Deutsch ».
        assert "PixelPhotoManager" not in before
        assert "PixelPhotoManager" in btn.toolTip()

    def test_menu_lists_every_language_with_its_flag(self, qtbot, config, monkeypatch):
        config.set(i18n.CONFIG_KEY, "en")
        btn = LanguageButton(config)
        qtbot.addWidget(btn)
        captured = []

        class _CapturingMenu(lb_module.QMenu):
            def exec(self, *a, **k):
                captured.append(self)
                return None

        monkeypatch.setattr(lb_module, "QMenu", _CapturingMenu)
        btn._show_menu()

        menu, = captured
        actions = menu.actions()
        assert [a.text() for a in actions] == list(i18n.LANGUAGES.values())
        assert all(not a.icon().isNull() for a in actions)
        assert [a.isChecked() for a in actions] == [True, False, False]  # en
