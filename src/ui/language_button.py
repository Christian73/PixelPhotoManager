# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Sélecteur de langue de la barre du haut (drapeaux).

Doublon assumé de Paramètres › Langue : la langue est le seul réglage qu'un
utilisateur doit pouvoir trouver **sans savoir lire l'interface**. Une entrée
de menu nommée « Settings » ne remplit pas cette condition ; un drapeau visible
en permanence, si. Les deux points d'entrée écrivent la même clé de config
(`ui.language`) — `refresh()` resynchronise le bouton après un passage par le
dialogue de paramètres.

Le changement ne prend effet qu'au redémarrage (les widgets construisent leurs
libellés une seule fois, cf. `src/core/i18n.py`). Le bouton affiche malgré tout
**immédiatement** le drapeau choisi : c'est le choix de l'utilisateur qu'il
montre, et l'infobulle dit alors explicitement que l'interface est encore dans
l'ancienne langue.
"""

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QMessageBox, QPushButton

from src.core import i18n
from src.core.i18n import translate
from src.ui.flag_icons import FLAG_HEIGHT, FLAG_WIDTH, flag_icon
from src.ui.ui_utils import install_menu_width_fix


class LanguageButton(QPushButton):
    """Bouton drapeau ouvrant la liste des langues d'interface."""

    #: Émis avec le code retenu quand l'utilisateur change de langue.
    language_changed = Signal(str)

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        # Nom accessible pour l'automatisation pywinauto (e2e) — même
        # convention que settings::language::<code> : le bouton n'a aucun
        # texte, et son infobulle est traduite.
        self.setAccessibleName("toolbar::language")
        # Sans `setIconSize`, QPushButton rabat l'icône sur la taille par défaut
        # du style (16 px) : le drapeau serait rendu deux fois plus petit que la
        # vignette fournie, quel que soit le soin mis à la dessiner.
        self.setIconSize(QSize(FLAG_WIDTH, FLAG_HEIGHT))
        self.clicked.connect(self._show_menu)
        self.refresh()

    # ------------------------------------------------------------------ état

    def current_code(self) -> str:
        return i18n.current_language(self._config)

    def refresh(self) -> None:
        """Recale le drapeau et l'infobulle sur la config (cf. docstring)."""
        code = self.current_code()
        self.setIcon(flag_icon(code))
        self.setText("")
        if code == i18n.active_language():
            tip = translate("LanguageButton", "Interface language: {language}")
        else:
            tip = translate(
                "LanguageButton",
                "Interface language: {language} — applied the next time "
                "PixelPhotoManager starts",
            )
        self.setToolTip(tip.format(language=i18n.LANGUAGES.get(code, code)))

    # ------------------------------------------------------------------ menu

    def _show_menu(self) -> None:
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        current = self.current_code()
        for code, name in i18n.LANGUAGES.items():
            # Le nom de la langue reste écrit dans cette langue (cf. LANGUAGES) :
            # le traduire rendrait la liste illisible pour qui cherche la sienne.
            act = QAction(flag_icon(code), name, menu)
            act.setCheckable(True)
            act.setChecked(code == current)
            act.triggered.connect(lambda _checked=False, c=code: self._select(c))
            group.addAction(act)
            menu.addAction(act)
        install_menu_width_fix(menu)
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _select(self, code: str) -> None:
        if code == self.current_code():
            return
        i18n.set_language(self._config, code)
        self.refresh()
        self.language_changed.emit(code)
        QMessageBox.information(
            self,
            translate("LanguageButton", "Language"),
            translate(
                "LanguageButton",
                "The new language will be applied the next time "
                "PixelPhotoManager starts.",
            ),
        )
