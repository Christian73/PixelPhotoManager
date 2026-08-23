# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Updates the translation catalogs (translations/*.ts then *.qm).

    .venv\\Scripts\\python.exe tools\\update_translations.py            # .ts + .qm
    .venv\\Scripts\\python.exe tools\\update_translations.py --release  # .qm only

Step 1 (lupdate): re-extracts the strings marked translate() from all the source
code into translations/ppm_<code>.ts, keeping the translations already
entered and marking "vanished" those whose source has disappeared.
Step 2 (plurals): cf. `restore_numerus` below.
Step 3 (lrelease): compiles each .ts into a binary .qm, the only format read at
runtime (cf. src/core/i18n.py).

The SOURCE language is English: the strings are written in English in the
code and any untranslated string falls back to it. English has a catalog
too, but for the plurals only: the %n strings are written neutrally
in the code ("%n face(s)") and it is ppm_en.ts that carries "%n face" /
"%n faces". The rest of it is empty and falls back to the source.
"""

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TS_DIR = ROOT / "translations"

# ISO codes -> full locale written into the header of the .ts (Qt Linguist
# uses it for the plural rules, which differ from one locale to another).
TARGETS = {"fr": "fr_FR", "en": "en_US", "de": "de_DE"}
SOURCE_LOCALE = "en_US"

#: Number of plural forms per target language. fr/en/de have two
#: (the split differs: French puts 0 in the singular, English does not).
PLURAL_FORMS = {"fr": 2, "en": 2, "de": 2}


def sources() -> list[str]:
    files = sorted(str(p) for p in (ROOT / "src").rglob("*.py"))
    files.append(str(ROOT / "main.py"))
    return files


def run(tool: str, args: list[str]) -> None:
    exe = ROOT / ".venv" / "Scripts" / f"pyside6-{tool}.exe"
    cmd = [str(exe) if exe.is_file() else f"pyside6-{tool}", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"pyside6-{tool} a echoue (code {proc.returncode}).")
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if tail:
        print("   " + tail[-1].strip())


# ---------------------------------------------------------------------------
# Plurals
# ---------------------------------------------------------------------------

def harvest_numerus(ts: Path) -> dict:
    """Harvests the plural forms already entered: {(context, source): [forms]}."""
    if not ts.is_file():
        return {}
    root = ET.parse(ts).getroot()
    out = {}
    for ctx in root.findall("context"):
        name = (ctx.findtext("name") or "").strip()
        for msg in ctx.findall("message"):
            src = msg.findtext("source") or ""
            tr = msg.find("translation")
            if tr is None:
                continue
            forms = [(f.text or "") for f in tr.findall("numerusform")]
            if forms and any(forms):
                out[(name, src)] = forms
    return out


def restore_numerus(ts: Path, saved: dict, n_forms: int) -> int:
    """Puts back into plural form every message whose source contains `%n`.

    pyside6-lupdate only marks `numerus="yes"` if the 4th argument of
    translate() is an integer literal -- which it never is in real
    code, where the count is a variable. Worse: if it is an expression
    (len(x), obj.attr), lupdate REMOVES the message from the catalog, without a word
    (hence the rule: hoist the count into a local variable, cf.
    tests/test_i18n.py). And on a following pass, lupdate re-flattens an
    already translated plural message keeping only its 1st form.

    So we restore here, after every lupdate, the plural structure and the
    forms harvested before (harvest_numerus) -- this file is the only place
    that knows a `%n` means plural.
    """
    if not ts.is_file():
        return 0
    ET.register_namespace("", "")
    tree = ET.parse(ts)
    root = tree.getroot()
    fixed = 0
    for ctx in root.findall("context"):
        name = (ctx.findtext("name") or "").strip()
        for msg in ctx.findall("message"):
            src = msg.findtext("source") or ""
            if "%n" not in src:
                continue
            forms = saved.get((name, src))
            tr = msg.find("translation")
            if tr is None:
                tr = ET.SubElement(msg, "translation")
            if forms is None:
                # Nothing known: recover what lupdate left (flat text
                # of a previous pass) rather than losing it.
                flat = [f.text or "" for f in tr.findall("numerusform")]
                if not flat:
                    flat = [tr.text or ""]
                forms = flat
            forms = list(forms[:n_forms]) + [""] * max(0, n_forms - len(forms))

            # "vanished"/"obsolete": source gone from the code, lupdate keeps the
            # translation in reserve. Do not overwrite that status with "unfinished",
            # which would make it show up again as work to be done.
            kept = tr.get("type") if tr.get("type") in ("vanished", "obsolete") else None
            msg.set("numerus", "yes")
            tr.clear()
            tr.text = None
            if kept:
                tr.set("type", kept)
            elif not all(f.strip() for f in forms):
                # ONE single empty form is enough to mark the message unfinished:
                # lrelease counts "finished" as soon as there is a <translation>,
                # and an empty form renders an EMPTY string at runtime, for the
                # only `n` concerned. Four messages went through that hole.
                tr.set("type", "unfinished")
            for f in forms:
                node = ET.SubElement(tr, "numerusform")
                node.text = f
            fixed += 1

    tree.write(ts, encoding="utf-8", xml_declaration=True)
    # ElementTree does not rewrite the DOCTYPE; lrelease and Linguist do without it,
    # but the file stays closer to the original with it.
    text = ts.read_text(encoding="utf-8")
    if "<!DOCTYPE TS>" not in text:
        text = re.sub(r"(<\?xml[^>]*\?>\s*)", r"\1<!DOCTYPE TS>\n", text, count=1)
        ts.write_text(text, encoding="utf-8")
    return fixed


def main(argv: list[str]) -> int:
    TS_DIR.mkdir(exist_ok=True)
    release_only = "--release" in argv[1:]

    if not release_only:
        files = sources()
        print(f"lupdate : {len(files)} fichiers source")
        for code, locale in TARGETS.items():
            ts = TS_DIR / f"ppm_{code}.ts"
            print(f" -> {ts.name}")
            saved = harvest_numerus(ts)
            run("lupdate", [*files, "-source-language", SOURCE_LOCALE,
                            "-target-language", locale, "-ts", str(ts)])
            n = restore_numerus(ts, saved, PLURAL_FORMS[code])
            print(f"   {n} message(s) pluriel(s) restaure(s)")

    for code in TARGETS:
        ts = TS_DIR / f"ppm_{code}.ts"
        if not ts.is_file():
            print(f" !! {ts.name} absent — lancez d'abord sans --release")
            continue
        qm = ts.with_suffix(".qm")
        print(f"lrelease : {ts.name} -> {qm.name}")
        run("lrelease", [str(ts), "-qm", str(qm)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
