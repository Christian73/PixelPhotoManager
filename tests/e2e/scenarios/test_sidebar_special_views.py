# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : vues spéciales de la sidebar (Favoris, Vidéos,
recherche par nom de fichier), qui prouve au passage les deux correctifs
favoris de ce même chantier e2e :
- `PhotoViewer._toggle_favorite` ne persistait jamais rien avant correctif
  (mutation en mémoire seule) -> `favorite_toggle_requested` (photo_viewer.py:134)
  -> `MainWindow._on_favorite_toggle_requested` -> `Catalog.set_favorite`.
- Le menu contextuel de la grille avait un item « Marquer comme favori… » sans
  aucun callback (`menu.addAction(fav_label)` seul) -> `favorite_toggle_requested`
  (thumbnail_grid.py:420) ajouté, même handler `MainWindow`.

Un seul lancement d'application, étapes séquentielles sur la bibliothèque
synthétique (3 photos témoin control_1/2/3.jpg, aucune n'est favorite au
départ) :
1. Visionneuse : bascule favori via le bouton barre d'outils (glyphe
   "♡" -> "★") sur control_1.jpg -> vérification directe sur catalog.db
   (`photos.is_favorite`), PAS sur l'UI. Piège UIA confirmé empiriquement
   (dump `descendants(control_type=...)` pendant que la visionneuse était
   ouverte) : `self._btn_fav` est un `QPushButton` mais `setCheckable(True)`
   (photo_viewer.py:200) -> le pont d'accessibilité Qt l'expose comme
   `control_type="CheckBox"`, pas `"Button"` — invisible à
   `find_dialog_button`, il faut `find_checkbox(window, "♡", ...)`.
2. Sidebar "♡ Favoris" (_SPECIAL_FAV) -> la vignette de control_1.jpg doit
   apparaître dans la grille filtrée.
3. Retour à "★ Chronologie de toutes les photos" (_SPECIAL_ALL) pour que
   control_2.jpg (jamais favori) soit de nouveau visible, puis bascule favori
   via le menu contextuel clic-droit de la grille sur control_2.jpg (chemin de
   code distinct de l'étape 1) -> vérification DB -> nouveau clic-droit,
   "Retirer des favoris" cette fois -> vérification que le favori repasse à 0
   (les deux libellés dynamiques du menu, cf. thumbnail_grid.py:1223, sont
   ainsi exercés).
4. "▶ Vidéos" (_SPECIAL_VIDEOS) : `manifest.video` peut être absent (encodeur
   manquant sur la machine de génération, cf. generate_library.py) -> skip
   documenté si c'est le cas, sinon vérifie que la vidéo apparaît et que
   `photos.media_type='video'`.
5. "🔍 Par nom de fichier" (_SPECIAL_FILENAME) : le texte tapé dans le même
   champ de filtre que celui utilisé pour dossiers/personnes (`Sidebar.
   filter_text`, PAS un champ de recherche séparé, cf. sidebar.py:459-461) est
   lu au moment du clic sur l'item spécial (`itemClicked`, pas de la frappe
   d'Entrée nécessaire) -> vérifie que seule control_2.jpg (motif "control_2",
   unique parmi les 3 témoins) apparaît. Piège UIA confirmé empiriquement
   (dump `descendants()` juste après le clic : le libellé de statut restait
   bloqué sur "Vidéos — 1 photo", preuve que le clic n'atteignait jamais
   `_on_album_selected`) : cette entrée est la 4e de la QListWidget Albums de
   la sidebar, qui ne montre qu'un nombre limité d'items sans scroll — même
   piège de clipping/virtualisation que `_reveal_sidebar_albums_tail` dans
   test_albums.py, ici sur les 4 entrées spéciales elles-mêmes plutôt que sur
   un album ajouté. Il faut donner le focus à la liste (clic sur un item déjà
   visible) puis {END} pour amener "🔍 Par nom de fichier" dans le viewport
   avant de pouvoir cliquer dessus."""
import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_list_item,
    find_checkbox,
    find_dialog_button,
    find_thumbnail,
    open_photo_in_viewer,
    query_one,
    right_click_element,
    type_into_sidebar_filter,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e


def _is_favorite(catalog_db, photo_path) -> int | None:
    return query_one(catalog_db, "SELECT is_favorite FROM photos WHERE path=?", (str(photo_path),))


def test_sidebar_special_views(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo_fav = manifest.control_photos[0]
    photo_other = manifest.control_photos[1]

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_fav),)) == 1
        and query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_other),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    assert _is_favorite(catalog_db, photo_fav) == 0
    assert _is_favorite(catalog_db, photo_other) == 0

    # ---- 1. Bascule favori depuis la visionneuse (bugfix _toggle_favorite) ----
    open_photo_in_viewer(window, photo_fav)
    find_checkbox(window, "♡", timeout=10.0).click_input()
    wait_for_condition(
        lambda: _is_favorite(catalog_db, photo_fav) == 1,
        timeout=20.0, message="le favori (visionneuse) n'a pas été persisté",
    )
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    # ---- 2. Vue "♡ Favoris" : la vignette favorite doit apparaître ----
    click_list_item(window, "♡ Favoris", exact=True, timeout=10.0)
    find_thumbnail(window, photo_fav, timeout=15.0)

    # ---- 3. Bascule favori depuis le menu contextuel de la grille (bugfix
    # menu stub mort) : retour à la vue complète pour que control_2.jpg soit
    # visible, puis marquer/retirer via clic droit. ----
    click_list_item(window, "★ Chronologie de toutes les photos", exact=True, timeout=10.0)
    thumb_other = find_thumbnail(window, photo_other, timeout=15.0)
    right_click_element(thumb_other)
    click_context_menu_item(window, "Marquer comme favori", exact=True, timeout=10.0)
    wait_for_condition(
        lambda: _is_favorite(catalog_db, photo_other) == 1,
        timeout=20.0, message="le favori (menu contextuel grille) n'a pas été persisté",
    )

    thumb_other = find_thumbnail(window, photo_other, timeout=15.0)
    right_click_element(thumb_other)
    click_context_menu_item(window, "Retirer des favoris", exact=True, timeout=10.0)
    wait_for_condition(
        lambda: _is_favorite(catalog_db, photo_other) == 0,
        timeout=20.0, message="le retrait de favori (menu contextuel grille) n'a pas été persisté",
    )

    # ---- 4. Vue "▶ Vidéos" : peut être absente sur cette machine ----
    if manifest.video is None:
        pytest.skip("Aucune vidéo synthétique générée sur cette machine (encodeur manquant)")
    click_list_item(window, "▶ Vidéos", exact=True, timeout=10.0)
    find_thumbnail(window, manifest.video, timeout=15.0)
    assert query_one(
        catalog_db, "SELECT media_type FROM photos WHERE path=?", (str(manifest.video),)
    ) == "video"

    # ---- 5. Vue "🔍 Par nom de fichier" : même champ de filtre que
    # dossiers/personnes, lu au clic sur l'item spécial. ----
    type_into_sidebar_filter(window, "control_2")
    # La liste Albums de la sidebar (QListWidget) ne montre qu'un nombre limité
    # d'items sans scroll ; "🔍 Par nom de fichier" (4e entrée spéciale) peut
    # être partiellement hors du viewport visible (même piège de
    # virtualisation/clipping que _reveal_sidebar_albums_tail dans
    # test_albums.py) -> on la révèle en scrollant la liste en fin via {END}
    # après lui avoir donné le focus par un clic sur un item déjà visible.
    import pywinauto.keyboard as kb
    click_list_item(window, "▶ Vidéos", exact=True, timeout=10.0)
    kb.send_keys("{END}")
    click_list_item(window, "🔍 Par nom de fichier", exact=True, timeout=10.0)
    find_thumbnail(window, photo_other, timeout=15.0)
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM photos WHERE filename LIKE '%control_2%'"
    ) == 1
