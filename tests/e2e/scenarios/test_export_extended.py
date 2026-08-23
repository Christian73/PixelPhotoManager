# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : export depuis la grille (sélection multiple),
préréglages de taille et nommage anti-collision — complète
test_export.py (qui ne couvre que l'export mono-photo depuis la visionneuse,
préréglage par défaut « Taille maximale »).

`main_window.py::_on_export_clicked` n'a qu'UN SEUL bouton d'entrée
(« ⬆ Exporter » de la barre d'outils, main_window.py:391) : son comportement
bifurque simplement selon le mode actif (`self._stack.currentIndex()`) —
photo en cours si la visionneuse est ouverte, `self._grid.get_selected()`
sinon (contrairement à ce que le plan initial supposait, il n'existe PAS
d'entrée « Exporter » dédiée dans le menu contextuel de la grille — vérifié
par grep, aucune occurrence de « export » dans thumbnail_grid.py). Le second
« chemin d'entrée » à tester est donc la sélection multiple de la grille
(Ctrl+clic, `Qt.ControlModifier` géré à thumbnail_grid.py:1116), pas un
second bouton.

Les photos synthétiques font toutes 900×700 px (630 000 px) — sous les
seuils « Grande » (4 Mpx) et « Moyenne » (2 Mpx) de `_EXPORT_SIZES`
(export_dialogs.py), qui ne redimensionnent donc jamais cette bibliothèque
(`_run_export` ne réduit que si `w*h > max_pixels`). Seul le préréglage
« Petite (~500 kpx) » (500 000 px) déclenche réellement un redimensionnement
ici — utilisé en contraste avec « Moyenne » (aucun redimensionnement) pour
prouver que le préréglage sélectionné est bien pris en compte, plutôt que de
supposer un comportement par défaut.

Étapes séquentielles, un seul lancement :
1. Sélection multiple (clic + Ctrl+clic) de 2 photos témoin -> export
   préréglage « Petite » -> les deux fichiers produits sont redimensionnés
   sous 500 000 px (un fichier par photo, cf. `_run_export`'s boucle).
2. Export mono-photo (sélection simple, 3e photo témoin) préréglage
   « Moyenne » -> dimensions de sortie identiques à l'original (900×700),
   par contraste avec l'étape 1.
3. Ré-export de la MÊME photo, même dossier, préréglage par défaut
   (« Taille maximale ») -> collision de nom -> `_run_export`'s résolution
   `{stem}_{n}.jpg` (export_dialogs... non, main_window.py:2319-2325) :
   le fichier de l'étape 2 reste intact, un second fichier `_1` apparaît."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

from tests.e2e.conftest import (
    find_dialog_button,
    find_thumbnail,
    query_one,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e

_ORIGINAL_SIZE = (900, 700)


def _set_export_dir(window, path: Path) -> None:
    """Écrase le champ de destination de `_ExportDialog` — voir la même
    fonction dans test_export.py pour la justification (texte par défaut
    pointant vers le vrai dossier Images de l'utilisateur)."""
    deadline = time.monotonic() + 10.0
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for edit in window.descendants(control_type="Edit"):
                text = edit.window_text()
                if "PixelPhotoManager" in text and text.rstrip("\\/").endswith("Export"):
                    edit.set_edit_text(str(path))
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Champ de destination d'export introuvable après 10s ({last_exc})")


def _click_size_radio(window, label_substring: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for rb in window.descendants(control_type="RadioButton"):
                if label_substring in rb.window_text():
                    rb.click_input()
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Radio de taille contenant {label_substring!r} introuvable après {timeout}s ({last_exc})")


def test_export_extended(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo_a, photo_b, photo_c = manifest.control_photos

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_a),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    export_dir = catalog_db.parents[2] / "export_out_extended"

    # ---- 1. Sélection multiple (grille) + préréglage « Petite » ----
    find_thumbnail(window, str(photo_a), timeout=30.0).click_input()
    find_thumbnail(window, str(photo_b), timeout=15.0).click_input(pressed="control")

    find_dialog_button(window, ["Export"], exact=False, timeout=10.0).click_input()
    _click_size_radio(window, "Small", timeout=10.0)
    _set_export_dir(window, export_dir)
    find_dialog_button(window, ["Export"], exact=True, timeout=10.0).click_input()

    dest_a = export_dir / (Path(photo_a).stem + ".jpg")
    dest_b = export_dir / (Path(photo_b).stem + ".jpg")
    wait_for_condition(
        lambda: dest_a.exists() and dest_b.exists(),
        timeout=30.0, message="l'export multi-sélection n'a pas produit les deux fichiers attendus",
    )
    for dest in (dest_a, dest_b):
        with Image.open(dest) as img:
            w, h = img.size
        # Le préréglage cible ~500 000 px ; l'arrondi entier des dimensions
        # après mise à l'échelle (facteur non entier) peut légèrement dépasser
        # la cible exacte (ex. 802x624 = 500 448 depuis 900x700) — tolérance
        # de 2 % plutôt qu'un plafond strict, tout en prouvant qu'un
        # redimensionnement réel a eu lieu (loin de l'original 630 000 px).
        assert w * h <= 510_000, f"{dest} : {w}x{h} ({w * h} px) dépasse largement le préréglage « Petite » (~500 000 px)"
        assert (w, h) != _ORIGINAL_SIZE, f"{dest} : taille inchangée, le préréglage ne semble pas appliqué"

    # ---- 2. Export mono-photo, préréglage « Moyenne » (pas de redimensionnement) ----
    find_thumbnail(window, str(photo_c), timeout=15.0).click_input()  # clic seul : désélectionne a/b

    find_dialog_button(window, ["Export"], exact=False, timeout=10.0).click_input()
    _click_size_radio(window, "Average", timeout=10.0)
    _set_export_dir(window, export_dir)
    find_dialog_button(window, ["Export"], exact=True, timeout=10.0).click_input()

    dest_c = export_dir / (Path(photo_c).stem + ".jpg")
    wait_for_condition(
        lambda: dest_c.exists(), timeout=30.0, message=f"{dest_c} n'a jamais été produit (préréglage Moyenne)",
    )
    with Image.open(dest_c) as img:
        assert img.size == _ORIGINAL_SIZE, (
            f"préréglage « Moyenne » : taille inattendue {img.size}, "
            f"la bibliothèque synthétique ({_ORIGINAL_SIZE}) est sous le seuil de 2 Mpx "
            "et ne devrait jamais être redimensionnée"
        )
    first_export_size = dest_c.stat().st_size

    # ---- 3. Ré-export de la même photo, même dossier : nommage anti-collision ----
    find_dialog_button(window, ["Export"], exact=False, timeout=10.0).click_input()
    _set_export_dir(window, export_dir)
    find_dialog_button(window, ["Export"], exact=True, timeout=10.0).click_input()

    dest_c_collision = export_dir / (Path(photo_c).stem + "_1.jpg")
    wait_for_condition(
        lambda: dest_c_collision.exists(),
        timeout=30.0,
        message=f"le second export de {photo_c} n'a pas créé {dest_c_collision} (résolution de collision)",
    )
    assert dest_c.stat().st_size == first_export_size, (
        "le fichier de l'étape 2 a été écrasé par le second export (qualité JPEG "
        "différente entre les préréglages « Moyenne » et « Taille maximale », donc "
        "taille de fichier différente attendue) : la résolution de collision "
        "{stem}_1.jpg n'a pas été appliquée"
    )
