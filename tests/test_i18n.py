# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of the internationalisation (src/core/i18n.py + translations/*.ts).

Most of this file does not test application code but the **writing
conventions** of the translatable strings, because breaking them breaks nothing
visible: the program runs, the tests pass, and the string is simply
absent from the catalog - hence never translated, silently. Each class below
corresponds to a trap really encountered, documented in the docstring of
`src/core/i18n.py` and of `tools/update_translations.py`.
"""
import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.core.i18n import (DEFAULT_LANGUAGE, LANGUAGES, current_language,
                           installer_language, normalize, set_language,
                           translate)

ROOT = Path(__file__).resolve().parent.parent
TS_DIR = ROOT / "translations"


def _python_sources() -> list[Path]:
    return sorted((ROOT / "src").rglob("*.py")) + [ROOT / "main.py"]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _translate_calls():
    """(path, Call node) for every `translate(...)` call of the source code."""
    for path in _python_sources():
        for node in ast.walk(_parse(path)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "translate"):
                yield path, node


def _where(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(ROOT)}:{node.lineno}"


# --------------------------------------------------------------------------
# Writing conventions
# --------------------------------------------------------------------------

class TestMarkerConventions:
    def test_translate_is_qt_translate(self):
        """`pyside6-lupdate` recognises the name `translate` as such - it still
        has to be the Qt function that translates at runtime."""
        from PySide6.QtCore import QCoreApplication
        assert translate is QCoreApplication.translate

    def test_no_self_tr(self):
        """`self.tr()` resolves its context on the class of the *instance*, whereas
        lupdate extracts it under the class that writes the call. The two
        diverge as soon as there is inheritance (the mixins of MainWindow): string
        extracted under one context, looked up under another, never translated."""
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
        """`_t = lambda s: translate("Ctx", s)` compiles, runs, and produces
        no extractable string: lupdate only reads literal calls."""
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
        """Context and source must be literals: lupdate reads the code,
        it does not execute it."""
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
    """The 4th argument of translate() - the costliest trap of the lot."""

    def test_count_argument_is_a_plain_name(self):
        """If the count is an expression (`len(x)`, `obj.attr`, `n + 1`),
        lupdate **removes the message from the catalog purely and simply**:
        no error, no trace, the string just becomes untranslatable.
        Hence the rule: hoist the count into a local variable first."""
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
        """A `%n` source with no 4th argument displays "%n" as such."""
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
        """Conversely, a count passed to a source with no `%n` shows
        nowhere - the number is lost."""
        bad = []
        for path, node in _translate_calls():
            if len(node.args) < 4:
                continue
            source = node.args[1]
            if isinstance(source, ast.Constant) and "%n" not in source.value:
                bad.append(_where(path, node))
        assert not bad, "compte passé à une chaîne sans %n : " + ", ".join(bad)


class TestInstallHappensBeforeUiImports:
    """Many labels are **module constants** (`_TREATMENTS`,
    `FRAME_TYPES`, `_TAB_LABELS`, EXIF tables...): their `translate()` is evaluated at
    import time, once. A module imported before `i18n.install()` therefore freezes
    the English source for the whole life of the process - the interface comes
    out half translated, without the slightest error. Hence the order imposed in
    `main()`, which these two tests lock down."""

    @staticmethod
    def _main_body() -> list[ast.stmt]:
        for node in ast.walk(_parse(ROOT / "main.py")):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                return node.body
        raise AssertionError("main() introuvable dans main.py")

    @staticmethod
    def _imports_translated_constants(module: str, _seen=None) -> bool:
        """True if importing `module` evaluates a translate(), directly or through
        one of its module-level imports. The transitivity counts as much as the rest:
        `src.ui.main_window` has no translated constant of its own, but
        imports `help_dialog`, `edit_panel`... which do."""
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

        # Only the module-level imports are followed: a deferred import
        # inside a function does not run at import time.
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
        """An import of `src.ui` at the top of `main.py` would run before even
        the QApplication: no translator is installed yet."""
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
        """In `main()`, every import of a module with translated constants must
        come after `i18n.install(...)`."""
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
# Catalogs
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
        """lupdate only marks `numerus="yes"` on an integer literal, and
        re-flattens the already translated plurals at every pass; it is
        `update_translations.restore_numerus()` that catches both. Without it,
        a plural message has only one form and the singular equals the plural."""
        bad = [f"{ctx} | {msg.findtext('source')}"
               for ctx, msg in _messages(code)
               if "%n" in (msg.findtext("source") or "")
               and msg.get("numerus") != "yes"]
        assert not bad, "message %n sans numerus : " + " ; ".join(bad)

    def test_numerus_messages_have_two_forms(self, code):
        """fr/en/de have two forms; a missing form comes out empty on
        display instead of the expected text."""
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
        """An **empty** plural form is the worst case: `lrelease` counts
        the message "finished" (it does have a `<translation>` with two forms) and
        nothing signals anything at all - but at runtime, the only `n`
        concerned renders an empty string. Four messages had gone through that
        hole, in all three languages at once."""
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
        """No unfinished message, except in the catalog of the SOURCE language:
        that one exists only to carry the plurals, all the rest of it is
        deliberately empty and falls back on the source written in the code."""
        if code == DEFAULT_LANGUAGE:
            pytest.skip(f"{code} est la langue source (catalogue de pluriels)")
        bad = []
        for ctx, msg in _messages(code):
            tr = msg.find("translation")
            if tr is None or tr.get("type") in ("vanished", "obsolete"):
                continue
            if msg.get("numerus") == "yes":
                continue          # covered by test_no_plural_form_is_empty
            if tr.get("type") == "unfinished" or not (tr.text or "").strip():
                bad.append(f"{ctx} | {msg.findtext('source')}")
        assert not bad, (f"{len(bad)} message(s) non traduit(s) en « {code} » — "
                         "ils s'afficheraient en anglais : " + " ; ".join(bad[:20]))


class TestSourcesAreInTheCatalogue:
    def test_every_plural_string_of_the_code_is_extracted(self):
        """A net against the two ways of losing a plural: forgetting to
        regenerate the catalogs, and the silent disappearance due to the 4th
        argument. Restricted to the `%n` strings, the only ones whose loss does not
        show (a simple string not extracted falls back on correct
        English, a plural not extracted displays "3 face(s)")."""
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
        """A config corrupted by hand must not produce a non-existent
        language, which would make the loading of the catalog fail."""
        config = _FakeConfig()
        set_language(config, "it")
        assert current_language(config) == DEFAULT_LANGUAGE


class TestInstallerLanguage:
    """Reading the language written in HKLM by the MSI installer.

    The value is produced by a localized string of `installer/product.wxs`, so
    it is carried by the same language transforms as the rest of the installer:
    what is read back here is the code of the language the installer really
    displayed.
    """

    @staticmethod
    def _registry(monkeypatch, value, *, key_exists=True):
        import winreg

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def open_key(root, path):
            assert root == winreg.HKEY_LOCAL_MACHINE
            if not key_exists:
                raise OSError(2, "not found")
            return _Key()

        monkeypatch.setattr(winreg, "OpenKey", open_key)
        monkeypatch.setattr(
            winreg, "QueryValueEx", lambda key, name: (value, winreg.REG_SZ)
        )

    def test_absent_key_gives_none(self, monkeypatch):
        """`None`, not `DEFAULT_LANGUAGE`.

        "No installer" (sources, portable copy) must stay distinguishable from
        "installer in English", otherwise the caller could not tell an absence
        from a deliberate choice.
        """
        self._registry(monkeypatch, None, key_exists=False)
        assert installer_language() is None

    @pytest.mark.parametrize("value,expected", [
        ("fr", "fr"), ("de", "de"), ("en", "en"),
        ("fr-FR", "fr"), ("de_DE", "de"), ("  EN  ", "en"),
    ])
    def test_supported_values(self, monkeypatch, value, expected):
        self._registry(monkeypatch, value)
        assert installer_language() == expected

    @pytest.mark.parametrize("value", ["", "it", "zz", None, 42])
    def test_unsupported_value_gives_none(self, monkeypatch, value):
        self._registry(monkeypatch, value)
        assert installer_language() is None

    def test_a_broken_registry_never_raises(self, monkeypatch):
        """A read failure must not stop the application from starting."""
        import winreg

        def boom(root, path):
            raise OSError(5, "access denied")

        monkeypatch.setattr(winreg, "OpenKey", boom)
        assert installer_language() is None


class TestRuntimePlurals:
    """Checks the whole chain: .qm loaded -> correct form displayed.

    It is the only test that proves that **English** really has a catalog, while
    it is the source language: without `ppm_en.qm`, `QCoreApplication.translate`
    falls back on the source and substitutes `%n` anyway - the user reads
    "3 photo(s)", that is a regression compared to the code before the i18n.
    """

    SOURCE = "%n photo(s)"

    def test_english_plurals_come_from_the_catalogue(self, qapp):
        from src.core import i18n
        i18n.install(qapp, "en")
        try:
            assert i18n.translate("MainWindow", self.SOURCE, None, 1) == "1 photo"
            assert i18n.translate("MainWindow", self.SOURCE, None, 3) == "3 photos"
            # 0 takes the plural in English (not in French).
            assert i18n.translate("MainWindow", self.SOURCE, None, 0) == "0 photos"
        finally:
            i18n.install(qapp, DEFAULT_LANGUAGE)

    def test_french_plurals_come_from_the_catalogue(self, qapp):
        from src.core import i18n
        i18n.install(qapp, "fr")
        try:
            assert i18n.translate("MainWindow", self.SOURCE, None, 1) == "1 photo"
            assert i18n.translate("MainWindow", self.SOURCE, None, 3) == "3 photos"
            # 0 takes the singular in French (not in English).
            assert i18n.translate("MainWindow", self.SOURCE, None, 0) == "0 photo"
        finally:
            i18n.install(qapp, DEFAULT_LANGUAGE)
