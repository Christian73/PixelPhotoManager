# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : export d'une photo retouchée depuis la visionneuse.

Chemin exercé : double-clic vignette -> visionneuse -> retouche Luminosité
(même mécanique que test_edit_nondestructive.py) -> bouton barre d'outils
"⬆  Exporter" (main_window.py:816, ``_on_export_clicked``) -> ``_ExportDialog``
(modal, ``exec()``) -> le champ ``_dir_edit`` (QLineEdit, texte par défaut =
``Path.home()/Pictures/PixelPhotoManager/Export`` !) est **explicitement
écrasé** vers un dossier isolé sous le ``tmp_path`` du test — ne jamais laisser
ce scénario écrire dans le vrai dossier Images de l'utilisateur, cf. le
principe d'isolation de tout ce module de tests -> "Exporter" (bouton OK,
texte exact, distinct du bouton de la barre d'outils qui porte le glyphe "⬆").

Vérifications sur le fichier `.jpg` produit, pas sur l'UI :
- il existe ;
- il est mesurablement plus clair que l'original (delta de luminance moyenne),
  preuve que la retouche non destructive a bien été incrustée à l'export
  (``_run_export`` : ``ImageAdjuster.apply_all`` si ``edit.is_modified()``) ;
- ses dates de fichier (mtime) reprennent celles de l'original
  (``preserve_file_dates``), pas la date de création de l'export."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from tests.e2e.conftest import double_click_element, open_photo_in_viewer, find_dialog_button, find_thumbnail, query_one, wait_for_condition

pytestmark = pytest.mark.e2e

_BRIGHTNESS_TARGET = 0.7


def _mean_luminance(path: Path) -> float:
    with Image.open(path) as img:
        return float(np.asarray(img.convert("L"), dtype=np.float64).mean())


def _set_export_dir(window, path: Path) -> None:
    """Écrase le champ de destination de `_ExportDialog` (QLineEdit dont le
    texte par défaut pointe vers le vrai dossier Images de l'utilisateur —
    voir l'avertissement en tête de fichier). Identifié par son contenu par
    défaut plutôt que par index, pour ne pas dépendre de l'ordre des
    QLineEdit dans l'arbre UIA (l'EXIF panel affiche aussi des QLineEdit
    pendant la visionneuse)."""
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


def test_export_bakes_in_edit_and_preserves_dates(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo = manifest.control_photos[0]

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    original_mtime = Path(photo).stat().st_mtime
    original_luminance = _mean_luminance(photo)

    open_photo_in_viewer(window, photo)

    find_dialog_button(window, ["Luminosité"], exact=True, timeout=15.0).click_input()
    sliders = []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not sliders:
        sliders = window.descendants(control_type="Slider")
        time.sleep(0.3)
    assert sliders, "le slider de luminosité n'est pas apparu"
    sliders[0].set_value(int(_BRIGHTNESS_TARGET * 100))
    find_dialog_button(window, ["Valider"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: query_one(
            isolated_app.edits_db, "SELECT brightness FROM photo_edits WHERE photo_path=?", (str(photo),)
        ) is not None,
        timeout=20.0, message="la retouche préalable à l'export n'a pas été persistée",
    )

    # Dossier d'export isolé : catalog_db = tmp_path/app_data/PixelPhotoManager/catalog.db
    export_dir = catalog_db.parents[2] / "export_out"

    # Bouton de la barre d'outils (glyphe "⬆" + texte) — un seul bouton contient "Exporter" avant l'ouverture du dialogue.
    find_dialog_button(window, ["Exporter"], exact=False, timeout=10.0).click_input()
    _set_export_dir(window, export_dir)
    # Bouton OK du dialogue : texte exact "Exporter" (sans glyphe), distinct du bouton barre d'outils.
    find_dialog_button(window, ["Exporter"], exact=True, timeout=10.0).click_input()

    dest = export_dir / (Path(photo).stem + ".jpg")

    def _exported() -> bool:
        return dest.exists() and dest.stat().st_size > 0

    wait_for_condition(_exported, timeout=30.0, message=f"le fichier exporté {dest} n'est jamais apparu")

    exported_luminance = _mean_luminance(dest)
    assert exported_luminance > original_luminance + 5.0, (
        f"la retouche de luminosité (+{_BRIGHTNESS_TARGET}) ne semble pas incrustée à l'export "
        f"(original={original_luminance:.1f}, exporté={exported_luminance:.1f})"
    )

    exported_mtime = dest.stat().st_mtime
    assert abs(exported_mtime - original_mtime) < 5.0, (
        "preserve_file_dates() n'a pas reporté la date de l'original sur le fichier exporté "
        f"(original={original_mtime}, exporté={exported_mtime})"
    )
