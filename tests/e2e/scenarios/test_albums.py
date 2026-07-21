# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : les trois chemins de création d'album et le seul
chemin de suppression. Un seul lancement d'application, étapes séquentielles
(ordre imposé par le plan : création -> peuplement -> suppression) :

1. « + » de la sidebar (`Sidebar._create_album`, `sidebar.py:810`) ->
   `QInputDialog.getText(..., "Nouvel album", "Nom de l'album :")` ->
   `bus.emit("album.create_requested", ...)` -> `MainWindow._on_album_create`
   (main_window.py:1592) -> `Catalog.create_album` seul, sans photo -> une
   ligne `albums` créée, 0 ligne `album_photos` associée.

2. Grille, sélection multiple (clic + Ctrl+clic sur 2 photos témoin) -> menu
   contextuel « Créer un nouvel album avec les 2 photos sélectionnées… »
   (`thumbnail_grid.py:1234`, libellé dynamique donc recherche par
   sous-chaîne) -> `_on_create_album_with` (main_window.py:1650) ->
   `QInputDialog.getText(..., "Nouvel album", f"Nom du nouvel album ({n}
   photo(s) sélectionnée(s)) :")` -> nouvel album + `add_photos_to_album` ->
   vérifie que les 2 `album_photos` attendues existent.

3. Grille, sélection simple d'une 3e photo témoin (pas encore dans un album)
   -> menu contextuel « Ajouter cette photo à un album… »
   (`thumbnail_grid.py:1232`) -> `_on_add_to_album` (main_window.py:1615) :
   `QDialog` avec `QListWidget` des albums existants (libellé
   `f"{album.name}  ({album.photo_count} photo(s))"`, ligne 0 pré-sélectionnée
   par défaut) -> sélectionne l'album de l'étape 2 par sous-chaîne sur son nom
   (le compte de photos affiché varie, seul le nom est stable) -> OK -> vérifie
   qu'une 3e ligne `album_photos` apparaît pour cet album SANS perturber les 2
   lignes de l'étape 2. Le cas « aucun album existant » (QMessageBox
   d'information, main_window.py:1618) n'est pas atteignable dans ce
   scénario puisqu'un album existe déjà depuis l'étape 1 — écart documenté,
   même esprit que les gaps déjà notés dans les autres scénarios de ce
   dossier.

4. Menu contextuel de la liste Albums de la sidebar (clic droit sur l'item de
   l'album de l'étape 2) -> « Supprimer l'album… » (`sidebar.py:701`) ->
   confirmation standard `QMessageBox.Yes/No` (PAS retexturée, contrairement à
   « Effacer le dossier… » de test_folder_management.py) -> `click_yes`
   suffit -> vérifie que les lignes `albums`/`album_photos` de cet album
   disparaissent, mais que les photos elles-mêmes (`photos` + fichiers sur
   disque) restent intactes."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_yes,
    find_dialog_button,
    find_thumbnail,
    query_one,
    right_click_element,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e

_EMPTY_ALBUM_NAME = "Album Vide E2E"
_POPULATED_ALBUM_NAME = "Album Peuplé E2E"


def _find_edit_near_text(window, text_substring: str, *, timeout: float = 10.0):
    """Repère le `QLineEdit` d'un `QInputDialog` par proximité verticale avec
    son libellé — même helper que test_folder_management.py (dupliqué ici
    plutôt que factorisé dans conftest.py : chaque scénario garde ses petits
    utilitaires locaux dans ce dossier, cf. convention déjà établie pour
    `_find_edit_near_radio`/`_find_edit_near_text`)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            labels = [t for t in window.descendants(control_type="Text")
                      if text_substring in t.window_text()]
            edits = window.descendants(control_type="Edit")
            if labels and edits:
                l_rect = labels[0].rectangle()
                l_mid = (l_rect.top + l_rect.bottom) / 2
                return min(
                    edits,
                    key=lambda e: abs(((e.rectangle().top + e.rectangle().bottom) / 2) - l_mid),
                )
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"QLineEdit proche de {text_substring!r} introuvable après {timeout}s ({last_exc})")


def _find_list_item(window, text_substring: str, *, timeout: float = 10.0):
    """Repère l'élément UIA (`ListItem`) d'un item de `QListWidget` par
    sous-chaîne, sans cliquer dessus — nécessaire pour un clic droit
    (`right_click_element`), contrairement à `click_list_item` de conftest.py
    qui clique directement en gauche."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for item in window.descendants(control_type="ListItem"):
                if text_substring in item.window_text():
                    return item
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Élément de liste contenant {text_substring!r} introuvable après {timeout}s ({last_exc})")


def _reveal_sidebar_albums_tail(window) -> None:
    """La liste Albums de la sidebar (`Sidebar._albums_list`, un `QListWidget`)
    n'expose via UIA que les `ListItem` actuellement dans son viewport visible
    (même piège de virtualisation que la grille de vignettes, cf. le docstring
    de `find_thumbnail`) — un album ajouté après les 4 entrées spéciales
    (Chronologie/Favoris/Vidéos/Par nom de fichier) peut donc rester invisible
    à `_find_list_item` tant que la liste n'a pas été scrollée. On focus la
    liste via un item toujours présent puis on envoie {END} pour amener la fin
    de la liste dans le viewport."""
    for item in window.descendants(control_type="ListItem"):
        if "Chronologie" in item.window_text():
            item.click_input()
            break
    import pywinauto.keyboard as kb
    kb.send_keys("{END}")
    time.sleep(0.3)


def test_albums(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    photo_a, photo_b, photo_c = manifest.control_photos

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo_a),)) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    # ---- 1. « + » sidebar : album vide ----
    find_dialog_button(window, ["+"], exact=True, timeout=10.0).click_input()
    edit_new = _find_edit_near_text(window, "Nom de l'album", timeout=10.0)
    edit_new.set_edit_text(_EMPTY_ALBUM_NAME)
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_EMPTY_ALBUM_NAME,)) is not None,
        timeout=15.0, message="l'album vide n'a pas été créé au catalogue",
    )
    empty_album_id = query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_EMPTY_ALBUM_NAME,))
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (empty_album_id,)
    ) == 0, "l'album créé sans photo ne devrait avoir aucune ligne album_photos"

    # ---- 2. Sélection multiple grille -> "Créer un nouvel album avec…" ----
    find_thumbnail(window, str(photo_a), timeout=30.0).click_input()
    thumb_b = find_thumbnail(window, str(photo_b), timeout=15.0)
    thumb_b.click_input(pressed="control")
    right_click_element(thumb_b)
    click_context_menu_item(window, "Créer un nouvel album avec", exact=False, timeout=10.0)
    edit_populated = _find_edit_near_text(window, "Nom du nouvel album", timeout=10.0)
    edit_populated.set_edit_text(_POPULATED_ALBUM_NAME)
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_POPULATED_ALBUM_NAME,)) is not None,
        timeout=15.0, message="l'album peuplé n'a pas été créé au catalogue",
    )
    populated_album_id = query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_POPULATED_ALBUM_NAME,))
    photo_a_id = query_one(catalog_db, "SELECT id FROM photos WHERE path=?", (str(photo_a),))
    photo_b_id = query_one(catalog_db, "SELECT id FROM photos WHERE path=?", (str(photo_b),))
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (populated_album_id,)
        ) == 2,
        timeout=15.0, message="les 2 photos sélectionnées n'ont pas été ajoutées à l'album",
    )
    for pid in (photo_a_id, photo_b_id):
        assert query_one(
            catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=? AND photo_id=?",
            (populated_album_id, pid),
        ) == 1, f"photo {pid} absente de l'album peuplé"

    # ---- 3. Sélection simple d'une 3e photo -> "Ajouter … à un album…" ----
    thumb_c = find_thumbnail(window, str(photo_c), timeout=15.0)
    thumb_c.click_input()  # clic seul : désélectionne a/b
    right_click_element(thumb_c)
    click_context_menu_item(window, "à un album", exact=False, timeout=10.0)
    _find_list_item(window, _POPULATED_ALBUM_NAME, timeout=10.0).click_input()
    find_dialog_button(window, ["OK"], exact=True, timeout=10.0).click_input()

    photo_c_id = query_one(catalog_db, "SELECT id FROM photos WHERE path=?", (str(photo_c),))
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=? AND photo_id=?",
            (populated_album_id, photo_c_id),
        ) == 1,
        timeout=15.0, message="la 3e photo n'a pas été ajoutée à l'album via 'Ajouter à un album…'",
    )
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (populated_album_id,)
    ) == 3, "les 2 photos précédentes de l'album ont été perturbées par le 3e ajout"

    # ---- 4. Suppression de l'album peuplé (confirmation standard Oui/Non) ----
    _reveal_sidebar_albums_tail(window)
    right_click_element(_find_list_item(window, _POPULATED_ALBUM_NAME, timeout=10.0))
    click_context_menu_item(window, "Supprimer l'album…", exact=True, timeout=10.0)
    click_yes(window)

    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_POPULATED_ALBUM_NAME,)) is None,
        timeout=15.0, message="l'album n'a pas été supprimé du catalogue",
    )
    assert query_one(
        catalog_db, "SELECT COUNT(*) FROM album_photos WHERE album_id=?", (populated_album_id,)
    ) == 0, "les lignes album_photos de l'album supprimé n'ont pas été purgées"
    for path, pid in ((photo_a, photo_a_id), (photo_b, photo_b_id), (photo_c, photo_c_id)):
        assert query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE id=?", (pid,)) == 1, (
            f"la photo {path} a disparu du catalogue après suppression de l'album"
        )
        assert Path(path).exists(), f"le fichier {path} a été supprimé alors que seul l'album l'était"

    # l'album vide de l'étape 1 doit rester intact, non affecté par la suppression du 2e album
    assert query_one(catalog_db, "SELECT id FROM albums WHERE name=?", (_EMPTY_ALBUM_NAME,)) == empty_album_id
    assert window.exists()
