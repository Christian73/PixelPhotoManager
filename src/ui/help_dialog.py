# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialogue Aide / À propos.

Le contenu des onglets vit dans src/ui/help_content/<langue>/*.html (un fichier
par onglet + _style.html partagé) — extrait de ce module en 2026-07 pour que
l'aide soit éditable sans toucher au code, puis réparti par langue en 2026-08.
En mode figé (PyInstaller), le dossier est embarqué sous _internal/help_content
(cf. pixelphotomanager.spec, entrée datas) et résolu via sys._MEIPASS.

La résolution se fait **fichier par fichier** (_help_file), avec repli sur
l'anglais (`DEFAULT_LANGUAGE`) : une langue dont un seul onglet n'est pas encore
traduit affiche cet onglet en anglais au lieu de perdre toute son aide. C'est
aussi pourquoi `_style.html`, qui est du CSS pur et n'a donc pas de version par
langue, vit dans `en/` et n'existe que là."""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QTextBrowser, QDialogButtonBox, QLineEdit,
)

from src.core.app_version import get_app_version
from src.core.update_checker import (
    UpdateCheckThread, STATUS_UPDATE_AVAILABLE, STATUS_UP_TO_DATE, STATUS_VERSION_UNKNOWN,
)
from src.core.i18n import DEFAULT_LANGUAGE, active_language, translate

logger = logging.getLogger(__name__)

# (clé d'onglet, fichier dans help_content/)
# Le 1er élément est une CLÉ, pas un libellé : il sert d'identifiant d'onglet
# (paramètre `tab=` de HelpDialog, comparaisons internes). Il reste en français
# quelle que soit la langue — l'affichage passe par _TAB_LABELS.
_TABS = [
    ("Vue d'ensemble",  "vue_densemble.html"),
    ("Navigation",      "navigation.html"),
    ("Diaporama",       "diaporama.html"),
    ("Retouches",       "retouches.html"),
    ("Visages",         "visages.html"),
    ("Doublons",        "doublons.html"),
    ("Raccourcis",      "raccourcis.html"),
    ("Paramètres",      "parametres.html"),
    ("À propos",        "a_propos.html"),
]

#: Libellés affichés des onglets, indexés par leur clé (cf. _TABS).
_TAB_LABELS: dict[str, str] = {
    "Vue d'ensemble": translate("HelpDialog", "Overview"),
    "Navigation":     translate("HelpDialog", "Navigation"),
    "Diaporama":      translate("HelpDialog", "Slideshow"),
    "Retouches":      translate("HelpDialog", "Editing"),
    "Visages":        translate("HelpDialog", "Faces"),
    "Doublons":       translate("HelpDialog", "Duplicates"),
    "Raccourcis":     translate("HelpDialog", "Shortcuts"),
    "Paramètres":     translate("HelpDialog", "Settings"),
    "À propos":       translate("HelpDialog", "About"),
}


def _content_dir() -> Path:
    """Dossier des fichiers d'aide — bundle PyInstaller ou arborescence source."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", "")) / "help_content"
    return Path(__file__).parent / "help_content"


def _help_file(filename: str) -> Path:
    """Chemin du fichier d'aide dans la langue courante, avec repli sur
    l'anglais (page non encore traduite, ou langue inconnue)."""
    base = _content_dir()
    localized = base / active_language() / filename
    if localized.is_file():
        return localized
    return base / DEFAULT_LANGUAGE / filename


def _load_tab_html(filename: str) -> str:
    """Contenu d'un onglet : <style> partagé + fichier de l'onglet, avec la
    version de l'application substituée. Un fichier manquant produit un
    message d'erreur affichable plutôt qu'un crash."""
    try:
        style = _help_file("_style.html").read_text(encoding="utf-8")
        body = _help_file(filename).read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Aide : fichier introuvable %s (%s)", filename, exc)
        return "<p>" + translate(
            "HelpDialog", "Help content unavailable ({filename})."
        ).format(filename=filename) + "</p>"
    # _style.html contient déjà ses balises <style>…</style>
    return style + body.replace("__VERSION__", get_app_version())


_BROWSER_STYLE = """
QTextBrowser {
    background: #2b2b2b;
    border: none;
    padding: 8px;
}
"""

_SEARCH_NOT_FOUND_STYLE = "QLineEdit { background: #5a2a2a; color: #fff; }"

_TABWIDGET_STYLE = """
QTabWidget::pane {
    border: 1px solid #444;
    background: #2b2b2b;
}
QTabBar::tab {
    background: #2a2a2a;
    color: #bbb;
    padding: 5px 12px;
    border: 1px solid #444;
    border-bottom: none;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: #2a5a9a;
    color: #ffffff;
    font-weight: bold;
    border-color: #3a6ab0;
    border-bottom: 1px solid #2a5a9a;
}
QTabBar::tab:hover:!selected {
    background: #333;
    color: #eee;
}
"""


class HelpDialog(QDialog):
    def __init__(self, parent=None, tab: str | None = None):
        super().__init__(parent)
        # Sans ça, chaque ouverture d'Aide/À propos (dlg.exec() dans main_window.py)
        # laissait le QDialog et son QThread de vérification de version en vie
        # indéfiniment, parentés à MainWindow — fuite qui grossit à chaque ouverture.
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(translate("HelpDialog", "Help — PixelPhotoManager"))
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(translate("HelpDialog", "Search the help…  "
                                                                     "(Enter: next match)"))
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(lambda text: self._search(text, continue_search=False))
        self._search_edit.returnPressed.connect(
            lambda: self._search(self._search_edit.text(), continue_search=True)
        )
        layout.addWidget(self._search_edit)
        self._search_shortcut = QShortcut(QKeySequence.Find, self)
        self._search_shortcut.activated.connect(self._focus_search)

        tabs = QTabWidget()
        tabs.setStyleSheet(_TABWIDGET_STYLE)
        self._about_browser: QTextBrowser | None = None
        for title, filename in _TABS:
            browser = QTextBrowser()
            browser.setOpenExternalLinks(True)
            browser.setStyleSheet(_BROWSER_STYLE)
            html = _load_tab_html(filename)
            if title == "À propos":
                self._about_browser = browser
                html = html.replace(
                    "__VERSION_CHECK__",
                    '<span style="color:#888;">'
                    + translate("HelpDialog", "Checking the version…")
                    + '</span>',
                )
            browser.setHtml(html)
            browser.verticalScrollBar().setValue(0)
            tabs.addTab(browser, _TAB_LABELS.get(title, title))

        if tab is not None:
            for i, (title, _) in enumerate(_TABS):
                if title == tab:
                    tabs.setCurrentIndex(i)
                    break

        layout.addWidget(tabs)
        self._tabs = tabs

        # Pas de parent : WA_DeleteOnClose peut détruire ce dialogue avant que la
        # vérification réseau (jusqu'à 5s) ne se termine — un QThread parenté serait
        # alors détruit alors qu'il tourne encore. Il s'auto-nettoie via `finished`.
        self._update_check_thread = UpdateCheckThread()
        self._update_check_thread.checked.connect(self._on_version_checked)
        self._update_check_thread.finished.connect(self._update_check_thread.deleteLater)
        self._update_check_thread.start()

        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.accept)
        layout.addWidget(btn_box)

    def closeEvent(self, event) -> None:
        """Coupe le rappel vers ce dialogue (bientôt détruit via WA_DeleteOnClose)
        sans attendre la fin du thread de vérification, qui continue et se
        nettoie lui-même (cf. __init__)."""
        try:
            self._update_check_thread.checked.disconnect(self._on_version_checked)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

    def _focus_search(self) -> None:
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    @staticmethod
    def _search_current_tab_from(browser: QTextBrowser, text: str, *, from_top: bool) -> bool:
        if from_top:
            cursor = browser.textCursor()
            cursor.movePosition(QTextCursor.Start)
            browser.setTextCursor(cursor)
        return browser.find(text)

    def _search(self, text: str, *, continue_search: bool) -> None:
        """Cherche `text` dans l'onglet courant puis, s'il est absent,
        dans les onglets suivants (recherche circulaire, un seul onglet
        actif à la fois donc pas de vue "tous onglets" possible sans
        dupliquer le contenu). `continue_search=True` (Entrée) poursuit
        depuis la position courante ; toute autre frappe repart du début
        de l'onglet affiché — cohérent avec le comportement `Ctrl+F` d'un
        navigateur."""
        self._search_edit.setStyleSheet("")
        if not text:
            return
        tabs = self._tabs
        n = tabs.count()
        start_index = tabs.currentIndex()

        browser = tabs.widget(start_index)
        if self._search_current_tab_from(browser, text, from_top=not continue_search):
            return
        for offset in range(1, n):
            index = (start_index + offset) % n
            browser = tabs.widget(index)
            if self._search_current_tab_from(browser, text, from_top=True):
                tabs.setCurrentIndex(index)
                return
        # Rien trouvé dans aucun onglet en repartant du suivant : dernière
        # chance sur l'onglet de départ depuis son début (cas "Entrée" qui
        # vient de dépasser la dernière occurrence de cet onglet — la boucle
        # ci-dessus l'a sauté puisqu'elle commence à offset=1).
        if continue_search and self._search_current_tab_from(tabs.widget(start_index), text, from_top=True):
            return
        self._search_edit.setStyleSheet(_SEARCH_NOT_FOUND_STYLE)

    def _on_version_checked(self, status: str, version: str, html_url: str) -> None:
        if self._about_browser is None:
            return
        if status == STATUS_UPDATE_AVAILABLE:
            fragment = (
                '<span style="color:#e0a030;">'
                + translate("HelpDialog",
                            "⚠ A new version is available: <b>{version}</b> — <a "
                            "href=\"{url}\" style=\"color:#6aacf0;\">open the download page</a>"
                            ).format(version=version, url=html_url)
                + "</span>"
            )
        elif status == STATUS_UP_TO_DATE:
            fragment = ('<span style="color:#6abf6a;">'
                        + translate("HelpDialog", "✓ You have the latest version.")
                        + "</span>")
        elif status == STATUS_VERSION_UNKNOWN:
            fragment = (
                '<span style="color:#888;">'
                + translate("HelpDialog",
                            "Local version not comparable (development mode) — latest "
                            "published version: <b>{version}</b>."
                            ).format(version=version)
                + "</span>"
            )
        else:
            fragment = (
                '<span style="color:#888;">'
                + translate("HelpDialog",
                            "Could not check whether a new version is available (no "
                            "connection?).")
                + "</span>"
            )
        scroll_pos = self._about_browser.verticalScrollBar().value()
        self._about_browser.setHtml(
            _load_tab_html("a_propos.html").replace("__VERSION_CHECK__", fragment)
        )
        self._about_browser.verticalScrollBar().setValue(scroll_pos)
