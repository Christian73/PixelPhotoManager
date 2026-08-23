# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Language selector of the top bar (flags).

A deliberate duplicate of Settings › Language: the language is the only
setting a user must be able to find **without being able to read the
interface**. A menu entry named "Settings" does not meet that condition; a
permanently visible flag does. Both entry points write the same config key
(`ui.language`) — `refresh()` resynchronises the button after a visit to the
settings dialog.

The change only takes effect on restart (widgets build their labels once, cf.
`src/core/i18n.py`). The button nevertheless shows the chosen flag
**immediately**: what it shows is the user's choice, and the tooltip then says
explicitly that the interface is still in the previous language.
"""

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QMessageBox, QPushButton

from src.core import i18n
from src.core.i18n import translate
from src.ui.flag_icons import FLAG_HEIGHT, FLAG_WIDTH, flag_icon
from src.ui.ui_utils import install_menu_width_fix


class LanguageButton(QPushButton):
    """Flag button opening the list of the interface languages."""

    #: Emitted with the code selected when the user changes language.
    language_changed = Signal(str)

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        # Accessible name for the pywinauto automation (e2e) — the same
        # convention as settings::language::<code>: the button has no text at
        # all, and its tooltip is translated.
        self.setAccessibleName("toolbar::language")
        # Without `setIconSize`, QPushButton falls back to the default icon size
        # of the style (16 px): the flag would be rendered twice as small as the
        # thumbnail provided, however carefully it is drawn.
        self.setIconSize(QSize(FLAG_WIDTH, FLAG_HEIGHT))
        self.clicked.connect(self._show_menu)
        self.refresh()

    # ------------------------------------------------------------------ state

    def current_code(self) -> str:
        return i18n.current_language(self._config)

    def refresh(self) -> None:
        """Realigns the flag and the tooltip on the config (cf. the docstring)."""
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
            # The name of the language stays written in that language (cf. LANGUAGES):
            # translating it would make the list unreadable for whoever is looking for theirs.
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
