# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialogue Aide / À propos.

Le contenu des onglets vit dans src/ui/help_content/*.html (un fichier par
onglet + _style.html partagé) — extrait de ce module en 2026-07 pour que
l'aide soit éditable sans toucher au code. En mode figé (PyInstaller), le
dossier est embarqué sous _internal/help_content (cf. pixelphotomanager.spec,
entrée datas) et résolu via sys._MEIPASS."""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QTextBrowser, QDialogButtonBox,
)

from src.core.app_version import get_app_version
from src.core.update_checker import (
    UpdateCheckThread, STATUS_UPDATE_AVAILABLE, STATUS_UP_TO_DATE, STATUS_VERSION_UNKNOWN,
)

logger = logging.getLogger(__name__)

# (titre d'onglet, fichier dans help_content/)
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


def _content_dir() -> Path:
    """Dossier des fichiers d'aide — bundle PyInstaller ou arborescence source."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", "")) / "help_content"
    return Path(__file__).parent / "help_content"


def _load_tab_html(filename: str) -> str:
    """Contenu d'un onglet : <style> partagé + fichier de l'onglet, avec la
    version de l'application substituée. Un fichier manquant produit un
    message d'erreur affichable plutôt qu'un crash."""
    base = _content_dir()
    try:
        style = (base / "_style.html").read_text(encoding="utf-8")
        body = (base / filename).read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Aide : fichier introuvable %s (%s)", filename, exc)
        return f"<p>Contenu d'aide indisponible ({filename}).</p>"
    # _style.html contient déjà ses balises <style>…</style>
    return style + body.replace("__VERSION__", get_app_version())


_BROWSER_STYLE = """
QTextBrowser {
    background: #2b2b2b;
    border: none;
    padding: 8px;
}
"""

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
        self.setWindowTitle("Aide — PixelPhotoManager")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

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
                    '<span style="color:#888;">Vérification de la version…</span>',
                )
            browser.setHtml(html)
            browser.verticalScrollBar().setValue(0)
            tabs.addTab(browser, title)

        if tab is not None:
            for i, (title, _) in enumerate(_TABS):
                if title == tab:
                    tabs.setCurrentIndex(i)
                    break

        layout.addWidget(tabs)

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

    def _on_version_checked(self, status: str, version: str, html_url: str) -> None:
        if self._about_browser is None:
            return
        if status == STATUS_UPDATE_AVAILABLE:
            fragment = (
                '<span style="color:#e0a030;">⚠ Une nouvelle version est disponible : '
                f'<b>{version}</b> — <a href="{html_url}" style="color:#6aacf0;">'
                "ouvrir la page de téléchargement</a></span>"
            )
        elif status == STATUS_UP_TO_DATE:
            fragment = '<span style="color:#6abf6a;">✓ Vous disposez de la dernière version.</span>'
        elif status == STATUS_VERSION_UNKNOWN:
            fragment = (
                '<span style="color:#888;">Version locale non comparable (mode développement) — '
                f"dernière version publiée : <b>{version}</b>.</span>"
            )
        else:
            fragment = (
                '<span style="color:#888;">Impossible de vérifier la disponibilité '
                "d'une nouvelle version (pas de connexion ?).</span>"
            )
        scroll_pos = self._about_browser.verticalScrollBar().value()
        self._about_browser.setHtml(
            _load_tab_html("a_propos.html").replace("__VERSION_CHECK__", fragment)
        )
        self._about_browser.verticalScrollBar().setValue(scroll_pos)
