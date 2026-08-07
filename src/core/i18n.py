# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Internationalisation : français (langue source), anglais, allemand.

Le français est la langue des chaînes écrites dans le code : toute chaîne non
traduite retombe donc automatiquement sur un français correct. Il a malgré
tout son propre catalogue, mais pour un seul usage : les **pluriels**. Une
chaîne `%n` est écrite dans le code sous une forme neutre (« %n visage(s) »)
puisqu'un même littéral doit servir au singulier comme au pluriel ; c'est
`ppm_fr.qm` qui porte les deux formes réelles (« %n visage » / « %n visages »).
Les autres messages y restent vides et retombent sur la source.

Les catalogues `.ts` (sources, versionnés) et `.qm` (compilés, chargés à
l'exécution) vivent dans `translations/` à la racine du dépôt et sont
régénérés par `tools/update_translations.py`.

Deux traducteurs sont installés par langue :

- `ppm_<code>.qm` — les chaînes de l'application ;
- `qtbase_<code>.qm` — celles de Qt lui-même (boutons OK/Annuler des
  QMessageBox, sélecteur de fichiers, menu contextuel des champs de saisie).
  Sans lui, une interface allemande garde des dialogues standard en anglais.
  Il est charge aussi pour le francais, que Qt ne parle pas par defaut.

Le changement de langue prend effet au redémarrage : les widgets construisent
leurs libellés une seule fois, il n'y a pas de `retranslate_ui()`.

Marquage des chaînes — une seule forme dans tout le projet ::

    from src.core.i18n import translate
    ...
    translate("MainWindow", "Texte affiché")

`translate` est `QCoreApplication.translate` ; `pyside6-lupdate` reconnaît ce
nom tel quel, y compris importé d'ici (test : `tests/test_i18n.py`).

**Ne pas utiliser `self.tr()`** : PySide6 résout son contexte sur la classe de
*l'instance*, pas sur celle qui écrit l'appel, alors que `lupdate` l'extrait
sous la classe qui l'écrit. Les deux divergent dès qu'une classe est héritée —
et les mixins de `MainWindow` (`main_window_faces.py`,
`main_window_duplicates.py`) sont dans ce cas : extraction sous
« MainWindowFacesMixin », recherche à l'exécution sous « MainWindow », donc
libellé jamais traduit, en silence. Le contexte littéral de `translate()`
supprime la question : on y écrit le nom de la classe **d'exécution**
(« MainWindow » pour un mixin de MainWindow).
"""

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "fr"

#: Codes ISO 639-1 pris en charge -> nom de la langue dans cette langue
#: (un utilisateur qui a mis l'application dans une langue qu'il ne lit pas
#: doit pouvoir retrouver la sienne dans la liste).
LANGUAGES: dict[str, str] = {
    "fr": "Français",
    "en": "English",
    "de": "Deutsch",
}

CONFIG_KEY = "ui.language"

#: Marqueur de chaîne traduisible — cf. docstring du module.
#: Signature Qt : translate(context, source, disambiguation=None, n=-1).
translate = QCoreApplication.translate

# Les QTranslator doivent survivre a l'appel : Qt ne prend qu'une reference
# faible cote Python, un traducteur collecte cesse silencieusement de traduire.
_installed: list[QTranslator] = []

#: Langue réellement installée par `install()` (cf. `active_language`).
_active: str = DEFAULT_LANGUAGE


def app_translations_dir() -> Path:
    """Dossier des `.qm` de l'application."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", "")) / "translations"
    return Path(__file__).resolve().parent.parent.parent / "translations"


def _qt_translations_dirs() -> list[Path]:
    """Dossiers ou chercher les `.qm` de Qt (qtbase_*).

    QLibraryInfo suffit en mode dev. En mode figé il peut pointer vers le
    chemin de la machine de build : on ajoute donc l'emplacement réel dans le
    bundle.
    """
    dirs = [Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))]
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        dirs += [meipass / "PySide6" / "translations", meipass / "translations"]
    return dirs


def normalize(code: str | None) -> str:
    """Ramène n'importe quelle valeur de config à un code pris en charge."""
    if not code:
        return DEFAULT_LANGUAGE
    code = str(code).strip().replace("-", "_").split("_")[0].lower()
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


def system_language() -> str:
    """Langue du système si elle est prise en charge, sinon le français.

    Volontairement **pas** utilisée comme valeur par défaut : la langue de
    l'interface Windows ne dit pas la langue que l'utilisateur veut lire (cas
    vécu : Windows en anglais, application utilisée en français depuis
    toujours). Un tel réglage aurait basculé l'interface d'installations
    existantes sans que personne ne le demande. Exposée pour un éventuel
    « détecter automatiquement » explicite dans les paramètres.
    """
    for name in QLocale.system().uiLanguages():
        code = normalize(name)
        if code != DEFAULT_LANGUAGE or name.lower().startswith("fr"):
            return code
    return DEFAULT_LANGUAGE


def current_language(config) -> str:
    """Langue choisie par l'utilisateur, à défaut le français (cf. ci-dessus)."""
    return normalize(config.get(CONFIG_KEY))


def set_language(config, code: str) -> None:
    config.set(CONFIG_KEY, normalize(code))


def active_language() -> str:
    """Langue effectivement installée par `install()`.

    À utiliser pour tout contenu résolu par langue en dehors des catalogues Qt
    (l'aide intégrée, cf. `src/ui/help_dialog.py`) : la valeur de config peut
    déjà porter le prochain choix de l'utilisateur, alors que l'interface
    affichée est encore dans l'ancienne langue jusqu'au redémarrage.
    """
    return _active


def install(app: QCoreApplication, code: str) -> str:
    """Installe les traducteurs pour `code`. Renvoie la langue réellement posée.

    À appeler juste après la création de la QApplication, avant la
    construction du moindre widget : un libellé déjà affiché ne se retraduit
    pas.
    """
    global _active
    code = normalize(code)
    _active = code
    for tr in _installed:
        app.removeTranslator(tr)
    _installed.clear()

    qm = app_translations_dir() / f"ppm_{code}.qm"
    translator = QTranslator()
    if qm.is_file() and translator.load(str(qm)):
        app.installTranslator(translator)
        _installed.append(translator)
    elif code != DEFAULT_LANGUAGE:
        # Repli silencieux sur le français plutôt qu'une interface vide : les
        # chaînes sources restent affichées telles quelles.
        logger.warning("Catalogue de traduction introuvable ou illisible : %s", qm)

    _install_qt_base(app, code)
    return code


def _install_qt_base(app: QCoreApplication, code: str) -> None:
    for directory in _qt_translations_dirs():
        translator = QTranslator()
        if translator.load(f"qtbase_{code}", str(directory)):
            app.installTranslator(translator)
            _installed.append(translator)
            return
    logger.debug("qtbase_%s.qm introuvable — dialogues standard Qt en anglais", code)
