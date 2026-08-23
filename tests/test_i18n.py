# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de l'internationalisation (src/core/i18n.py + translations/*.ts).

L'essentiel de ce fichier ne teste pas du code applicatif mais les **conventions
d'écriture** des chaînes traduisibles, parce que les enfreindre ne casse rien de
visible : le programme tourne, les tests passent, et la chaîne est simplement
absente du catalogue — donc jamais traduite, en silence. Chaque classe ci-dessous
correspond à un piège réellement rencontré, documenté dans le docstring de
`src/core/i18n.py` et de `tools/update_translations.py`.
"""
import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.core.i18n import (DEFAULT_LANGUAGE, LANGUAGES, current_language,
                           normalize, set_language, translate)

ROOT = Path(__file__).resolve().parent.parent
TS_DIR = ROOT / "translations"


def _python_sources() -> list[Path]:
    return sorted((ROOT / "src").rglob("*.py")) + [ROOT / "main.py"]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _translate_calls():
    """(chemin, nœud Call) pour chaque appel `translate(...)` du code source."""
    for path in _python_sources():
        for node in ast.walk(_parse(path)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "translate"):
                yield path, node


def _where(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(ROOT)}:{node.lineno}"


# --------------------------------------------------------------------------
# Conventions d'écriture
# --------------------------------------------------------------------------

class TestMarkerConventions:
    def test_translate_is_qt_translate(self):
        """`pyside6-lupdate` reconnaît le nom `translate` tel quel — encore
        faut-il que ce soit bien la fonction de Qt qui traduise à l'exécution."""
        from PySide6.QtCore import QCoreApplication
        assert translate is QCoreApplication.translate

    def test_no_self_tr(self):
        """`self.tr()` résout son contexte sur la classe de l'*instance*, alors
        que lupdate l'extrait sous la classe qui écrit l'appel. Les deux
        divergent dès qu'il y a héritage (les mixins de MainWindow) : chaîne
        extraite sous un contexte, cherchée sous un autre, jamais traduite."""
        bad = []
        for path in _python_sources():
            for node in ast.walk(_parse(path)):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "tr"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"):
                    bad.append(_where(path, node))
        assert not bad, "utiliser translate(\"Contexte\", …) : " + ", ".join(bad)

    def test_translate_is_never_aliased(self):
        """`_t = lambda s: translate("Ctx", s)` compile, tourne, et ne produit
        aucune chaîne extractible : lupdate ne lit que des appels littéraux."""
        bad = []
        for path in _python_sources():
            for node in ast.walk(_parse(path)):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if isinstance(value, ast.Name) and value.id == "translate":
                    bad.append(_where(path, node))
        assert not bad, "ne pas ré-exporter translate sous un autre nom : " + ", ".join(bad)

    def test_context_and_source_are_literals(self):
        """Contexte et source doivent être des littéraux : lupdate lit le code,
        il ne l'exécute pas."""
        bad = []
        for path, node in _translate_calls():
            args = node.args
            if len(args) < 2:
                bad.append(f"{_where(path, node)} (moins de 2 arguments)")
                continue
            for arg in args[:2]:
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    bad.append(_where(path, node))
                    break
        assert not bad, "contexte/source non littéral : " + ", ".join(bad)


class TestPluralConventions:
    """Le 4e argument de translate() — le piège le plus coûteux du lot."""

    def test_count_argument_is_a_plain_name(self):
        """Si le compte est une expression (`len(x)`, `obj.attr`, `n + 1`),
        lupdate **retire purement et simplement le message du catalogue** :
        pas d'erreur, pas de trace, la chaîne devient juste intraduisible.
        D'où la règle : hisser le compte dans une variable locale d'abord."""
        bad = []
        for path, node in _translate_calls():
            if len(node.args) < 4:
                continue
            count = node.args[3]
            if isinstance(count, (ast.Name, ast.Constant)):
                continue
            bad.append(f"{_where(path, node)} ({ast.dump(count)[:40]}…)")
        assert not bad, (
            "hisser le compte dans une variable locale avant l'appel : "
            + ", ".join(bad))

    def test_plural_sources_declare_a_count(self):
        """Une source en `%n` sans 4e argument affiche « %n » tel quel."""
        bad = []
        for path, node in _translate_calls():
            source = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(source, ast.Constant)
                    and isinstance(source.value, str)
                    and "%n" in source.value):
                continue
            if len(node.args) < 4:
                bad.append(_where(path, node))
        assert not bad, "chaîne %n sans compte : " + ", ".join(bad)

    def test_count_argument_implies_plural_source(self):
        """Réciproquement, un compte passé à une source sans `%n` ne se voit
        nulle part — le nombre est perdu."""
        bad = []
        for path, node in _translate_calls():
            if len(node.args) < 4:
                continue
            source = node.args[1]
            if isinstance(source, ast.Constant) and "%n" not in source.value:
                bad.append(_where(path, node))
        assert not bad, "compte passé à une chaîne sans %n : " + ", ".join(bad)


class TestInstallHappensBeforeUiImports:
    """Beaucoup de libellés sont des **constantes de module** (`_TREATMENTS`,
    `FRAME_TYPES`, `_TAB_LABELS`, tables EXIF…) : leur `translate()` s'évalue à
    l'import, une seule fois. Un module importé avant `i18n.install()` fige donc
    la source anglaise pour toute la durée du processus — l'interface se
    retrouve à moitié traduite, sans la moindre erreur. D'où l'ordre imposé dans
    `main()`, que ces deux tests verrouillent."""

    @staticmethod
    def _main_body() -> list[ast.stmt]:
        for node in ast.walk(_parse(ROOT / "main.py")):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                return node.body
        raise AssertionError("main() introuvable dans main.py")

    @staticmethod
    def _imports_translated_constants(module: str, _seen=None) -> bool:
        """Vrai si importer `module` évalue un translate(), directement ou via
        un de ses imports de tête. La transitivité compte autant que le reste :
        `src.ui.main_window` n'a aucune constante traduite en propre, mais
        importe `help_dialog`, `edit_panel`… qui en ont."""
        _seen = _seen if _seen is not None else set()
        if module in _seen:
            return False
        _seen.add(module)

        rel = Path(module.replace(".", "/"))
        source = next((c for c in (ROOT / rel.with_suffix(".py"),
                                   ROOT / rel / "__init__.py") if c.is_file()), None)
        if source is None:
            return False

        tree = _parse(source)
        deferred = {n for f in ast.walk(tree)
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                    for n in ast.walk(f)}
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "translate" and n not in deferred
               for n in ast.walk(tree)):
            return True

        # Seuls les imports de tête de module sont suivis : un import différé
        # dans une fonction ne s'exécute pas à l'import.
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                children = [node.module]
            elif isinstance(node, ast.Import):
                children = [a.name for a in node.names]
            else:
                continue
            if any(c.startswith(("src.", "main"))
                   and TestInstallHappensBeforeUiImports._imports_translated_constants(
                       c, _seen)
                   for c in children):
                return True
        return False

    def test_no_ui_import_at_module_level(self):
        """Un import de `src.ui` en tête de `main.py` s'exécuterait avant même
        la QApplication : aucun traducteur n'est encore posé."""
        bad = []
        for node in _parse(ROOT / "main.py").body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src.ui"):
                bad.append(f"main.py:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Import):
                bad += [f"main.py:{node.lineno} import {a.name}"
                        for a in node.names if a.name.startswith("src.ui")]
        assert not bad, (
            "importer src.ui à l'intérieur de main(), après i18n.install() : "
            + ", ".join(bad))

    def test_install_precedes_every_translated_import(self):
        """Dans `main()`, tout import d'un module à constantes traduites doit
        venir après `i18n.install(...)`."""
        body = self._main_body()
        install_at = next(
            (i for i, stmt in enumerate(body)
             for n in ast.walk(stmt)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "install"),
            None)
        assert install_at is not None, "i18n.install() absent de main()"

        early = []
        for stmt in body[:install_at]:
            for node in ast.walk(stmt):
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                else:
                    continue
                early += [f"main.py:{node.lineno} {m}" for m in modules
                          if self._imports_translated_constants(m)]
        assert not early, (
            "module à libellés constants importé avant i18n.install() : "
            + ", ".join(early))


# --------------------------------------------------------------------------
# Catalogues
# --------------------------------------------------------------------------

def _messages(code: str):
    ts = TS_DIR / f"ppm_{code}.ts"
    root = ET.parse(ts).getroot()
    for ctx in root.findall("context"):
        name = (ctx.findtext("name") or "").strip()
        for msg in ctx.findall("message"):
            yield name, msg


@pytest.mark.parametrize("code", sorted(LANGUAGES))
class TestCatalogues:
    def test_ts_and_qm_exist(self, code):
        assert (TS_DIR / f"ppm_{code}.ts").is_file()
        assert (TS_DIR / f"ppm_{code}.qm").is_file(), (
            "lancer tools/update_translations.py")

    def test_every_percent_n_message_is_numerus(self, code):
        """lupdate ne marque `numerus="yes"` que sur un littéral entier, et
        ré-aplatit les pluriels déjà traduits à chaque passe ; c'est
        `update_translations.restore_numerus()` qui rattrape les deux. Sans lui,
        un message pluriel n'a qu'une forme et le singulier vaut le pluriel."""
        bad = [f"{ctx} | {msg.findtext('source')}"
               for ctx, msg in _messages(code)
               if "%n" in (msg.findtext("source") or "")
               and msg.get("numerus") != "yes"]
        assert not bad, "message %n sans numerus : " + " ; ".join(bad)

    def test_numerus_messages_have_two_forms(self, code):
        """fr/en/de ont deux formes ; une forme manquante sort vide à
        l'affichage au lieu du texte attendu."""
        bad = []
        for ctx, msg in _messages(code):
            if msg.get("numerus") != "yes":
                continue
            tr = msg.find("translation")
            if tr.get("type") in ("vanished", "obsolete"):
                continue
            if len(tr.findall("numerusform")) != 2:
                bad.append(f"{ctx} | {msg.findtext('source')}")
        assert not bad, "formes plurielles incomplètes : " + " ; ".join(bad)

    def test_no_plural_form_is_empty(self, code):
        """Une forme plurielle **vide** est le pire des cas : `lrelease` compte
        le message « finished » (il a bien une `<translation>` à deux formes) et
        rien ne signale quoi que ce soit — mais à l'exécution, le seul `n`
        concerné rend une chaîne vide. Quatre messages étaient passés par ce
        trou, dans les trois langues à la fois."""
        bad = []
        for ctx, msg in _messages(code):
            if msg.get("numerus") != "yes":
                continue
            tr = msg.find("translation")
            if tr.get("type") in ("vanished", "obsolete"):
                continue
            forms = [f.text or "" for f in tr.findall("numerusform")]
            if not all(f.strip() for f in forms):
                bad.append(f"{ctx} | {msg.findtext('source')} -> {forms}")
        assert not bad, ("forme plurielle vide — compléter le .ts puis relancer "
                         "tools/update_translations.py : " + " ; ".join(bad))

    def test_every_message_is_translated(self, code):
        """Aucun message inachevé, sauf dans le catalogue de la langue SOURCE :
        celui-ci n'existe que pour porter les pluriels, tout le reste y est
        volontairement vide et retombe sur la source écrite dans le code."""
        if code == DEFAULT_LANGUAGE:
            pytest.skip(f"{code} est la langue source (catalogue de pluriels)")
        bad = []
        for ctx, msg in _messages(code):
            tr = msg.find("translation")
            if tr is None or tr.get("type") in ("vanished", "obsolete"):
                continue
            if msg.get("numerus") == "yes":
                continue          # couvert par test_no_plural_form_is_empty
            if tr.get("type") == "unfinished" or not (tr.text or "").strip():
                bad.append(f"{ctx} | {msg.findtext('source')}")
        assert not bad, (f"{len(bad)} message(s) non traduit(s) en « {code} » — "
                         "ils s'afficheraient en anglais : " + " ; ".join(bad[:20]))


class TestSourcesAreInTheCatalogue:
    def test_every_plural_string_of_the_code_is_extracted(self):
        """Filet contre les deux façons de perdre un pluriel : l'oubli de
        régénérer les catalogues, et la disparition silencieuse due au 4e
        argument. Restreint aux chaînes `%n`, les seules dont la perte ne se
        voit pas (une chaîne simple non extraite retombe sur un anglais
        correct, un pluriel non extrait affiche « 3 face(s) »)."""
        catalogue = {(ctx, msg.findtext("source") or "")
                     for ctx, msg in _messages(DEFAULT_LANGUAGE)}
        bad = []
        for path, node in _translate_calls():
            if len(node.args) < 2:
                continue
            ctx_arg, src_arg = node.args[0], node.args[1]
            if not (isinstance(ctx_arg, ast.Constant)
                    and isinstance(src_arg, ast.Constant)):
                continue
            if "%n" not in src_arg.value:
                continue
            if (ctx_arg.value, src_arg.value) not in catalogue:
                bad.append(f"{_where(path, node)} | {src_arg.value[:40]}")
        assert not bad, (
            "absent des catalogues — relancer tools/update_translations.py : "
            + " ; ".join(bad))


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class TestNormalize:
    @pytest.mark.parametrize("value", [None, "", "es", "zz", "klingon", 42])
    def test_unsupported_falls_back_to_the_source_language(self, value):
        assert normalize(value) == DEFAULT_LANGUAGE

    @pytest.mark.parametrize("value,expected", [
        ("fr", "fr"), ("EN", "en"), ("de_DE", "de"), ("de-CH", "de"),
        ("  fr_FR  ", "fr"), ("en_US", "en"),
    ])
    def test_variants_are_reduced_to_the_base_code(self, value, expected):
        assert normalize(value) == expected


class _FakeConfig:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class TestLanguagePreference:
    def test_default_is_the_source_language_when_unset(self):
        assert current_language(_FakeConfig()) == DEFAULT_LANGUAGE

    def test_roundtrip(self):
        config = _FakeConfig()
        set_language(config, "de")
        assert current_language(config) == "de"

    def test_unsupported_value_is_normalised_on_write(self):
        """Une config corrompue à la main ne doit pas produire une langue
        inexistante, qui ferait échouer le chargement du catalogue."""
        config = _FakeConfig()
        set_language(config, "it")
        assert current_language(config) == DEFAULT_LANGUAGE


class TestRuntimePlurals:
    """Vérifie la chaîne complète : .qm chargé → forme correcte affichée.

    C'est le seul test qui prouve que l'**anglais** a bien un catalogue, alors
    qu'il est la langue source : sans `ppm_en.qm`, `QCoreApplication.translate`
    retombe sur la source et substitue quand même `%n` — l'utilisateur lit
    « 3 photo(s) », soit une régression par rapport au code d'avant l'i18n.
    """

    SOURCE = "%n photo(s)"

    def test_english_plurals_come_from_the_catalogue(self, qapp):
        from src.core import i18n
        i18n.install(qapp, "en")
        try:
            assert i18n.translate("MainWindow", self.SOURCE, None, 1) == "1 photo"
            assert i18n.translate("MainWindow", self.SOURCE, None, 3) == "3 photos"
            # 0 se dit au pluriel en anglais (pas en français).
            assert i18n.translate("MainWindow", self.SOURCE, None, 0) == "0 photos"
        finally:
            i18n.install(qapp, DEFAULT_LANGUAGE)

    def test_french_plurals_come_from_the_catalogue(self, qapp):
        from src.core import i18n
        i18n.install(qapp, "fr")
        try:
            assert i18n.translate("MainWindow", self.SOURCE, None, 1) == "1 photo"
            assert i18n.translate("MainWindow", self.SOURCE, None, 3) == "3 photos"
            # 0 se dit au singulier en français (pas en anglais).
            assert i18n.translate("MainWindow", self.SOURCE, None, 0) == "0 photo"
        finally:
            i18n.install(qapp, DEFAULT_LANGUAGE)
