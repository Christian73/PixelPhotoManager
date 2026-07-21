# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : actions UI sur les doublons, en complément de
test_duplicate_detection.py (qui ne vérifie que la détection elle-même, côté
base de données). Un seul lancement d'application, sur la même bibliothèque
synthétique (paires exacte/redimensionnée/recadrée) :

1. Grille des doublons (`DuplicateGrid`, badge "Dupliquées" de la sidebar) :
   double-clic sur la carte du groupe exact -> comparaison rapide dans la
   visionneuse ("1:1" comme marqueur d'ouverture) -> fermeture -> retour
   automatique à la grille des doublons (`_viewer_back_target`).
2. Bouton ✗ d'une carte (groupe redimensionné) -> dissolution persistante
   (`Catalog.ignore_duplicate_group`) -> vérifie que SEUL ce groupe est
   dissous, le groupe exact restant intact (même contrat que
   tests/test_catalog.py::test_ignore_duplicate_group_dissolves_only_that_group,
   ici prouvé de bout en bout depuis le clic UI).
3. Popup de doublons (`_DuplicatesPopup`) ouverte depuis le vrai bouton
   "⧉ Doublons" de la VISIONNEUSE (QPushButton réel, cf. photo_viewer.py:349)
   plutôt que le badge peint à la main de la grille (`ThumbnailCell.
   paintEvent`, non automatisable sans clic en coordonnées pixel brutes) :
   navigation vers un autre exemplaire du groupe recadré sans fermer la
   popup, puis fermeture explicite via "Fermer".
4. Suppression d'un exemplaire du groupe recadré -> effet de bord vérifié :
   l'exemplaire restant repasse à `duplicate_group_id=NULL` (moins de 2
   membres restants, cf. main_window.py::_on_delete_finished).
5. Dialogue "Outils › État des doublons…" testé directement (pas seulement
   comme repli de `wait_for_duplicate_detection`) : "Voir les groupes"
   navigue vers la grille des doublons, "Vérifier maintenant" relance une
   passe sans planter l'application, le bouton de fermeture standard
   ("Fermer") ferme le dialogue sans action.

Les cartes (`_DuplicateCard`) et leurs boutons ✗ portent un nom accessible
dédié (`dupgroup::<id>` / `dupgroup_ignore::<id>`, ajouté à duplicate_grid.py
pour ce chantier e2e, même convention que ThumbnailCell) car elles n'ont
sinon aucun texte UIA unique (tooltips identiques pour toutes les cartes)."""
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_menu_item,
    click_popup_list_item,
    click_yes,
    double_click_element,
    find_by_accessible_name,
    find_dialog_button,
    find_thumbnail,
    open_photo_in_viewer,
    query_one,
    right_click_element,
    wait_for_condition,
    wait_for_duplicate_detection,
)

pytestmark = pytest.mark.e2e


def _group_id(catalog_db, path) -> int | None:
    return query_one(catalog_db, "SELECT duplicate_group_id FROM photos WHERE path=?", (str(path),))


def test_duplicates_ui(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    wait_for_duplicate_detection(
        window, catalog_db,
        (manifest.exact_duplicate_pair, manifest.resized_duplicate_pair, manifest.crop_duplicate_pair),
        timeout=90.0,
    )

    group_exact = _group_id(catalog_db, manifest.exact_duplicate_pair[0])
    group_resized = _group_id(catalog_db, manifest.resized_duplicate_pair[0])
    group_crop = _group_id(catalog_db, manifest.crop_duplicate_pair[0])
    assert group_exact is not None and group_resized is not None and group_crop is not None

    # ---- 1. Grille des doublons : double-clic sur la carte du groupe exact ----
    find_dialog_button(window, ["Dupliquées"], exact=True, timeout=15.0).click_input()
    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0)  # marqueur : grille doublons active

    card_exact = find_by_accessible_name(window, f"dupgroup::{group_exact}", timeout=15.0)
    double_click_element(card_exact)
    find_dialog_button(window, ["1:1"], exact=True, timeout=15.0)  # marqueur : visionneuse ouverte
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()
    # Retour automatique à la grille des doublons (_viewer_back_target).
    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0)

    # ---- 2. Bouton ✗ : dissolution isolée au groupe redimensionné ----
    find_by_accessible_name(window, f"dupgroup_ignore::{group_resized}", timeout=15.0).click_input()
    wait_for_condition(
        lambda: all(_group_id(catalog_db, p) is None for p in manifest.resized_duplicate_pair),
        timeout=20.0, message="le groupe redimensionné n'a pas été dissous",
    )
    assert _group_id(catalog_db, manifest.exact_duplicate_pair[0]) == group_exact
    assert _group_id(catalog_db, manifest.exact_duplicate_pair[1]) == group_exact

    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0).click_input()

    # ---- 3. Popup de doublons depuis le vrai bouton de la visionneuse ----
    open_photo_in_viewer(window, manifest.crop_duplicate_pair[0])
    find_dialog_button(window, ["⧉ Doublons"], exact=True, timeout=10.0).click_input()
    other_name = Path(manifest.crop_duplicate_pair[1]).name
    click_popup_list_item("_DuplicatesPopup", other_name, exact=False, timeout=10.0)
    wait_for_condition(
        lambda: any(
            other_name in t.window_text()
            for t in window.descendants(control_type="Text")
        ),
        timeout=10.0,
        message="la navigation depuis la popup de doublons n'a pas changé la photo affichée",
    )
    find_dialog_button(window, ["Fermer"], exact=True, timeout=10.0).click_input()
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    # ---- 4. Suppression d'un exemplaire du groupe recadré : effet de bord ----
    thumb = find_thumbnail(window, manifest.crop_duplicate_pair[0], timeout=15.0)
    right_click_element(thumb)
    click_context_menu_item(window, "Effacer le fichier…", exact=True, timeout=10.0)
    click_yes(window)
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.crop_duplicate_pair[0]),),
        ) == 0,
        timeout=20.0, message="le fichier supprimé est toujours présent au catalogue",
    )
    wait_for_condition(
        lambda: _group_id(catalog_db, manifest.crop_duplicate_pair[1]) is None,
        timeout=20.0,
        message="le groupe recadré n'a pas été dissous après la suppression d'un exemplaire",
    )

    # ---- 5. Dialogue "État des doublons…" testé directement ----
    click_menu_item(window, "Outils", "État des doublons…")
    find_dialog_button(window, ["Voir les groupes"], exact=True, timeout=10.0).click_input()
    find_dialog_button(window, ["← Photos"], exact=True, timeout=10.0).click_input()

    click_menu_item(window, "Outils", "État des doublons…")
    find_dialog_button(window, ["Vérifier maintenant"], exact=True, timeout=10.0).click_input()
    assert isolated_app.app.process.poll() is None, (
        "l'application a quitté de manière inattendue au déclenchement manuel "
        "d'une nouvelle passe de détection"
    )

    click_menu_item(window, "Outils", "État des doublons…")
    find_dialog_button(window, ["Fermer"], exact=True, timeout=10.0).click_input()
    assert window.exists()
