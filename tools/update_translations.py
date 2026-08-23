# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Met a jour les catalogues de traduction (translations/*.ts puis *.qm).

    .venv\\Scripts\\python.exe tools\\update_translations.py            # .ts + .qm
    .venv\\Scripts\\python.exe tools\\update_translations.py --release  # .qm seuls

Etape 1 (lupdate) : re-extrait les chaines marquees translate() de tout le code
source vers translations/ppm_<code>.ts, en conservant les traductions deja
saisies et en marquant "vanished" celles dont la source a disparu.
Etape 2 (pluriels) : cf. `restore_numerus` ci-dessous.
Etape 3 (lrelease) : compile chaque .ts en .qm binaire, seul format lu a
l'execution (cf. src/core/i18n.py).

La langue SOURCE est l'anglais : les chaines sont ecrites en anglais dans le
code et toute chaine non traduite y retombe. L'anglais a lui aussi un
catalogue, mais pour les seuls pluriels : les chaines %n sont ecrites au neutre
dans le code (« %n face(s) ») et c'est ppm_en.ts qui porte « %n face » /
« %n faces ». Le reste y est vide et retombe sur la source.
"""

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TS_DIR = ROOT / "translations"

# Codes ISO -> locale complete ecrite dans l'en-tete du .ts (Qt Linguist
# l'utilise pour les regles de pluriel, qui different d'une locale a l'autre).
TARGETS = {"fr": "fr_FR", "en": "en_US", "de": "de_DE"}
SOURCE_LOCALE = "en_US"

#: Nombre de formes plurielles par langue cible. fr/en/de en ont deux
#: (le decoupage differe : le francais met 0 au singulier, pas l'anglais).
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
# Pluriels
# ---------------------------------------------------------------------------

def harvest_numerus(ts: Path) -> dict:
    """Releve les formes plurielles deja saisies : {(contexte, source): [formes]}."""
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
    """Remet en forme plurielle tout message dont la source contient `%n`.

    pyside6-lupdate ne marque `numerus="yes"` que si le 4e argument de
    translate() est un litteral entier -- ce qu'il n'est jamais dans du vrai
    code, ou le compte est une variable. Pire : si c'est une expression
    (len(x), obj.attr), lupdate SUPPRIME le message du catalogue, sans un mot
    (d'ou la regle : hisser le compte dans une variable locale, cf.
    tests/test_i18n.py). Et sur une passe suivante, lupdate re-aplatit un
    message pluriel deja traduit en n'en gardant que la 1re forme.

    On restaure donc ici, apres chaque lupdate, la structure plurielle et les
    formes relevees avant (harvest_numerus) -- ce fichier est le seul endroit
    qui sait qu'un `%n` vaut pluriel.
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
                # Rien de connu : recuperer ce que lupdate a laisse (texte plat
                # d'une passe precedente) plutot que de le perdre.
                flat = [f.text or "" for f in tr.findall("numerusform")]
                if not flat:
                    flat = [tr.text or ""]
                forms = flat
            forms = list(forms[:n_forms]) + [""] * max(0, n_forms - len(forms))

            # "vanished"/"obsolete" : source disparue du code, lupdate garde la
            # traduction en reserve. Ne pas ecraser ce statut par "unfinished",
            # qui la ferait ressortir comme un travail a faire.
            kept = tr.get("type") if tr.get("type") in ("vanished", "obsolete") else None
            msg.set("numerus", "yes")
            tr.clear()
            tr.text = None
            if kept:
                tr.set("type", kept)
            elif not all(f.strip() for f in forms):
                # UNE seule forme vide suffit a marquer le message inacheve :
                # lrelease compte « finished » des qu'il y a une <translation>,
                # et une forme vide rend une chaine VIDE a l'execution, pour le
                # seul `n` concerne. Quatre messages sont passes par ce trou.
                tr.set("type", "unfinished")
            for f in forms:
                node = ET.SubElement(tr, "numerusform")
                node.text = f
            fixed += 1

    tree.write(ts, encoding="utf-8", xml_declaration=True)
    # ElementTree ne reecrit pas la DOCTYPE ; lrelease et Linguist s'en passent,
    # mais le fichier reste plus proche de l'original avec elle.
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
