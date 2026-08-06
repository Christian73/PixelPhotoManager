# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Genere les PDF de livraison a partir des Markdown a la racine du depot.

    .venv\\Scripts\\python.exe tools\\make_pdf.py                 # tous les documents
    .venv\\Scripts\\python.exe tools\\make_pdf.py DeliveryNote.md # un seul

Sortie : <nom du Markdown>_v<VERSION>.pdf a la racine (les .pdf sont ignores
par git, ce sont des livrables reconstructibles).

Chaine : Markdown -> HTML + feuille de style d'impression -> Chrome headless
`--print-to-pdf`. Chrome plutot qu'une bibliotheque Python (weasyprint, xhtml2pdf)
parce que c'est ce qui a produit le PDF de la v1.0.0 (moteur Skia/PDF) et que
la mise en page reste donc comparable d'une version a l'autre.
"""

import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
DOCUMENTS = ["Guide_Utilisateur.md", "DeliveryNote.md"]

# Chrome (ou Edge, meme moteur) : le premier chemin existant est utilise.
_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre"]

CSS = """
@page { size: A4; margin: 18mm 16mm 16mm 16mm; }

* { box-sizing: border-box; }
body {
  font-family: "Segoe UI", "Calibri", sans-serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: #1a1a1a;
  margin: 0;
}

h1 {
  font-size: 24pt; font-weight: 600; margin: 0 0 4pt 0; color: #123a5e;
  letter-spacing: -0.2pt;
}
h2 {
  font-size: 15pt; font-weight: 600; color: #123a5e;
  margin: 22pt 0 8pt 0; padding-bottom: 3pt;
  border-bottom: 1pt solid #c9d6e2;
  page-break-after: avoid; break-after: avoid;
}
h3 {
  font-size: 12pt; font-weight: 600; color: #1f5580;
  margin: 14pt 0 5pt 0;
  page-break-after: avoid; break-after: avoid;
}
/* Plus gras que le corps : sans ca un titre h4 (####, ex. "Recadrer") se lit
   plus leger qu'un simple paragraphe en gras juste en dessous. */
h4 {
  font-size: 11pt; font-weight: 700; color: #20415f;
  margin: 12pt 0 4pt 0;
  page-break-after: avoid; break-after: avoid;
}
p { margin: 0 0 7pt 0; }
ul, ol { margin: 0 0 7pt 0; padding-left: 18pt; }
li { margin: 0 0 3pt 0; }
li > ul, li > ol { margin-top: 3pt; }

a { color: #1f5580; text-decoration: none; }

hr {
  border: none; border-top: 0.75pt solid #d8dee4;
  margin: 14pt 0;
}

code {
  font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
  font-size: 9pt;
  background: #f2f4f6;
  border: 0.5pt solid #e0e4e8;
  border-radius: 2pt;
  padding: 0.5pt 3pt;
}
/* Cascadia Mono en premier : Consolas n'a pas les glyphes de filets (U+2500...)
   des schemas ASCII du guide, Chrome leur substitue alors une police CJK de
   chasse double et la boite part de travers. */
pre {
  font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
  font-size: 8.5pt;
  line-height: 1.35;
  background: #f7f8fa;
  border: 0.5pt solid #dfe3e8;
  border-radius: 3pt;
  padding: 7pt 9pt;
  margin: 0 0 9pt 0;
  white-space: pre;
  page-break-inside: avoid; break-inside: avoid;
}
pre code { background: none; border: none; padding: 0; font-size: inherit; }

blockquote {
  margin: 0 0 9pt 0;
  padding: 6pt 10pt;
  border-left: 2.5pt solid #4a8fc4;
  background: #f4f8fb;
}
blockquote p:last-child { margin-bottom: 0; }

table {
  border-collapse: collapse;
  width: 100%;
  margin: 0 0 10pt 0;
  font-size: 9.5pt;
  page-break-inside: avoid; break-inside: avoid;
}
th, td {
  border: 0.5pt solid #ccd4dc;
  padding: 3.5pt 6pt;
  text-align: left;
  vertical-align: top;
}
th { background: #eef2f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }

.version { font-size: 10pt; color: #777; margin: 0 0 16pt 0; }
.toc-break { page-break-after: always; break-after: page; }
"""

_LIST_ITEM = re.compile(r"^\s{0,3}([-*+]|\d{1,9}[.)])\s+")


def loosen_lists(md_text: str) -> str:
    """Insere la ligne vide qu'exige Python-Markdown avant une liste.

    GitHub (CommonMark) accepte qu'une liste interrompe un paragraphe :
    "Definir la zone :" suivi directement de "- ..." s'y affiche bien en liste.
    Python-Markdown, lui, avale les puces dans le paragraphe precedent (14
    occurrences dans le guide). Correction faite ici, a la conversion, plutot
    que dans le .md — la source est valide et se lit correctement sur GitHub.
    """
    out: list[str] = []
    in_fence = False
    in_list = False
    for line in md_text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            in_list = False
        elif not in_fence:
            if not line.strip():
                in_list = False
            elif _LIST_ITEM.match(line):
                # Une liste deja engagee ne doit pas etre desserree : une ligne
                # vide entre deux puces la rendrait "loose" (Markdown enrobe
                # alors chaque item d'un <p>, ce qui aere le rendu).
                if not in_list and out and out[-1].strip():
                    out.append("")
                in_list = True
        out.append(line)
    return "\n".join(out)


def build_html(src: Path) -> str:
    body = markdown.markdown(
        loosen_lists(src.read_text(encoding="utf-8")),
        extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
        extension_configs={"toc": {"permalink": False}},
    )
    today = date.today()
    stamp = (f'<p class="version">Version {VERSION} — '
             f"{today.day} {_MOIS[today.month - 1]} {today.year}</p>")
    body = body.replace("</h1>", f"</h1>\n{stamp}", 1)
    # Table des matieres sur sa propre page (guide utilisateur uniquement — les
    # documents courts comme la note de livraison n'en ont pas).
    toc = body.find("Table des matières")
    if toc != -1:
        # Le saut va avant le titre qui SUIT la table des matieres — celle-ci
        # est elle-meme un <h2>, couper avant l'enverrait en page 2.
        after = re.search(r"<h2[ >]", body[toc:])
        if after:
            i = toc + after.start()
            body = body[:i] + '<div class="toc-break"></div>' + body[i:]
    title = f"{src.stem.replace('_', ' ')} — PixelPhotoManager {VERSION}"
    return (
        '<!DOCTYPE html>\n<html lang="fr"><head><meta charset="utf-8">'
        f"<title>{title}</title><style>{CSS}</style></head>"
        f"<body>\n{body}\n</body></html>"
    )


def find_browser() -> str:
    for path in _BROWSERS:
        if Path(path).is_file():
            return path
    found = shutil.which("chrome") or shutil.which("msedge")
    if not found:
        raise SystemExit(
            "Chrome ou Edge introuvable — necessaire pour le rendu PDF.\n"
            "Chemins essayes :\n  " + "\n  ".join(_BROWSERS)
        )
    return found


def render(src: Path, browser: str) -> Path:
    out = ROOT / f"{src.stem}_v{VERSION}.pdf"
    tmp_html = src.with_suffix(".pdf.html")
    tmp_html.write_text(build_html(src), encoding="utf-8")
    try:
        if out.exists():
            out.unlink()
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=10000",
                f"--print-to-pdf={out}",
                tmp_html.as_uri(),
            ],
            capture_output=True, text=True, timeout=300,
        )
    finally:
        tmp_html.unlink(missing_ok=True)
    if not out.exists():
        raise SystemExit(f"Le navigateur n'a pas produit {out.name}.")
    return out


def main(argv: list[str]) -> int:
    names = argv[1:] or DOCUMENTS
    browser = find_browser()
    for name in names:
        src = Path(name)
        if not src.is_absolute():
            src = ROOT / name
        if not src.is_file():
            print(f"{src} introuvable — ignore.")
            continue
        out = render(src, browser)
        print(f"{out}  ({out.stat().st_size / 1024:.0f} Ko)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
