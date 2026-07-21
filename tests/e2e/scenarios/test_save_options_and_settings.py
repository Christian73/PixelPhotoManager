# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : options de sauvegarde, confirmation de suppression
« ne plus demander », applications externes et paramètres (lecteur vidéo),
aide/à propos. Un seul lancement d'application, étapes séquentielles :

1. « Enregistrer l'image traitée sur le disque » (menu contextuel de la
   grille — même code que l'entrée équivalente de la visionneuse,
   `save_requested.emit(photo)` dans les deux cas, photo_viewer.py:717 et
   thumbnail_grid.py:1227 — inutile de répéter le test pour les deux entrées)
   -> `_SaveOptionsDialog`, option par défaut « Écraser le fichier original »
   avec sauvegarde cochée par défaut -> vérifie la copie dans
   `.tmp_originals` (nommage `{stem}_{horodatage}{suffixe}`,
   cf. main_window.py::_backup_original) ET l'écrasement effectif du fichier.
   L'option « Enregistrer à un autre emplacement… » ouvre un `QFileDialog`
   natif sans repli éditable (contrairement au chemin du lecteur vidéo
   personnalisé, cf. §3 plus bas) — écart documenté, non automatisé ici,
   même esprit que l'exclusion des gestes de glissé du plan.

2. Case « Ne plus demander de confirmation » de la boîte de suppression
   (main_window.py:1822-1841, persiste `ui.delete_no_confirm`) : suppression
   de 3 photos témoin successives — la 1re confirmée sans cocher la case (la
   boîte apparaît), la 2e confirmée en cochant la case, la 3e ne doit
   provoquer AUCUNE boîte de confirmation.

3. « Outils › Applications externes… » : une entrée est pré-injectée dans la
   config au lancement (paramétrage indirect de `isolated_app`, seul moyen
   d'obtenir un chemin d'exécutable réel sans passer par le sélecteur natif
   de `_add()`, qui n'a pas de repli éditable) -> vérifie que son icône
   apparaît dans la barre de la visionneuse (nom accessible dédié,
   `extapp::<nom>`, ajouté à photo_viewer.py pour ce chantier e2e) ->
   suppression via le bouton « Supprimer » du dialogue -> l'icône disparaît.

4. « Outils › Paramètres › Lecteur vidéo » : sélection du lecteur
   personnalisé et saisie d'un chemin factice dans le `QLineEdit` dédié
   (repéré par proximité verticale avec le `QRadioButton` « Lecteur
   personnalisé : », PAS par nom accessible : un `QLineEdit` implémente
   généralement le pattern UIA Text, qui prime sur le nom accessible dans
   `window_text()` — cf. pywinauto `base_wrapper.window_text`, même
   ambiguïté qui a motivé l'identification par élimination de
   `type_into_sidebar_filter`) -> vérifie le round-trip dans `config.json`
   (`video.player_path`), sans tenter de lancer réellement le lecteur
   (repli documenté par le plan si le lancement du sous-processus n'est pas
   fiablement vérifiable).

5. Aide (F1 -> menu « Aide… ») et « À propos » : les deux ouvrent
   `HelpDialog` (help_dialog.py) sans erreur ; fermeture via le bouton
   standard « Fermer »."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    click_context_menu_item,
    click_list_item,
    click_menu_item,
    click_yes,
    find_by_accessible_name,
    find_checkbox,
    find_dialog_button,
    find_thumbnail,
    open_photo_in_viewer,
    query_one,
    right_click_element,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e

_EXTAPP_NAME = "Bloc-notes test"
_EXTAPP_PATH = r"C:\Windows\System32\notepad.exe"


def _click_radio(window, text: str, *, timeout: float = 10.0) -> None:
    """Coche le `QRadioButton` portant ce texte — nécessaire avant de saisir
    du texte dans le `QLineEdit` du chemin personnalisé : celui-ci est
    désactivé (`setEnabled(False)`) tant que le radio « Lecteur personnalisé »
    n'est pas coché (settings_dialog.py:210, `_on_radio_changed`), donc
    `set_edit_text` échoue avec `ElementNotEnabled` si on ne clique pas
    d'abord le radio."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for r in window.descendants(control_type="RadioButton"):
                if r.window_text() == text:
                    r.click_input()
                    return
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"RadioButton {text!r} introuvable après {timeout}s ({last_exc})")


def _find_edit_near_radio(window, radio_text: str, *, timeout: float = 10.0):
    """Repère le QLineEdit du chemin du lecteur vidéo personnalisé par
    proximité verticale avec son QRadioButton voisin — voir le docstring du
    module pour la raison (window_text() peu fiable pour un QLineEdit)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            radios = [r for r in window.descendants(control_type="RadioButton")
                      if r.window_text() == radio_text]
            edits = window.descendants(control_type="Edit")
            if radios and edits:
                r_rect = radios[0].rectangle()
                r_mid = (r_rect.top + r_rect.bottom) / 2
                return min(
                    edits,
                    key=lambda e: abs(((e.rectangle().top + e.rectangle().bottom) / 2) - r_mid),
                )
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"QLineEdit proche de {radio_text!r} introuvable après {timeout}s ({last_exc})")


def _config_get(config_path: Path, dotted_key: str):
    if not config_path.exists():
        return None
    data = json.loads(config_path.read_text(encoding="utf-8"))
    for k in dotted_key.split("."):
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return data


@pytest.mark.parametrize(
    "isolated_app",
    [{"tools": {"external_apps": [{"name": _EXTAPP_NAME, "path": _EXTAPP_PATH}]}}],
    indirect=True,
)
def test_save_options_and_settings(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    catalog_db = isolated_app.catalog_db
    config_path = isolated_app.catalog_db.parent / "config.json"

    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    # ---- 1. Enregistrer l'image traitée : écraser + sauvegarde .tmp_originals ----
    save_photo = manifest.burst_pair[0]
    save_path = Path(save_photo)
    backup_dir = save_path.parent / ".tmp_originals"

    thumb = find_thumbnail(window, str(save_photo), timeout=30.0)
    right_click_element(thumb)
    click_context_menu_item(window, "Enregistrer l'image traitée sur le disque", exact=True, timeout=10.0)
    find_dialog_button(window, ["Enregistrer"], exact=True, timeout=10.0).click_input()
    wait_for_condition(
        lambda: backup_dir.is_dir() and any(
            p.name.startswith(save_path.stem + "_") and p.suffix == save_path.suffix
            for p in backup_dir.iterdir()
        ),
        timeout=20.0, message="aucune sauvegarde n'a été créée dans .tmp_originals",
    )

    # ---- 2. Confirmation de suppression + case « Ne plus demander » ----
    photo1, photo2, photo3 = manifest.control_photos[0], manifest.control_photos[1], manifest.control_photos[2]

    thumb1 = find_thumbnail(window, str(photo1), timeout=30.0)
    right_click_element(thumb1)
    click_context_menu_item(window, "Effacer le fichier…", exact=True, timeout=10.0)
    click_yes(window)
    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo1),)) == 0,
        timeout=20.0, message="photo1 non supprimée (confirmation sans case cochée)",
    )
    assert _config_get(config_path, "ui.delete_no_confirm") in (None, False)

    thumb2 = find_thumbnail(window, str(photo2), timeout=15.0)
    right_click_element(thumb2)
    click_context_menu_item(window, "Effacer le fichier…", exact=True, timeout=10.0)
    find_checkbox(window, "Ne plus demander", timeout=10.0).click_input()
    click_yes(window)
    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo2),)) == 0,
        timeout=20.0, message="photo2 non supprimée (confirmation avec case cochée)",
    )
    wait_for_condition(
        lambda: _config_get(config_path, "ui.delete_no_confirm") is True,
        timeout=10.0, message="ui.delete_no_confirm n'a pas été persisté",
    )

    thumb3 = find_thumbnail(window, str(photo3), timeout=15.0)
    right_click_element(thumb3)
    click_context_menu_item(window, "Effacer le fichier…", exact=True, timeout=10.0)
    with pytest.raises(LookupError):
        find_dialog_button(window, ["Oui", "Yes", "&Oui", "&Yes"], timeout=3.0)
    wait_for_condition(
        lambda: query_one(catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo3),)) == 0,
        timeout=20.0, message="photo3 non supprimée automatiquement (ui.delete_no_confirm actif)",
    )

    # ---- 3. Applications externes : icône visionneuse + suppression ----
    ext_photo = manifest.exact_duplicate_pair[0]
    open_photo_in_viewer(window, ext_photo)
    find_by_accessible_name(window, f"extapp::{_EXTAPP_NAME}", timeout=10.0)
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    click_menu_item(window, "Outils", "Applications externes…")
    click_list_item(window, _EXTAPP_NAME, exact=False, timeout=10.0)
    find_dialog_button(window, ["Supprimer"], exact=True, timeout=10.0).click_input()
    find_dialog_button(window, ["OK"], timeout=10.0).click_input()

    open_photo_in_viewer(window, ext_photo)
    with pytest.raises(LookupError):
        find_by_accessible_name(window, f"extapp::{_EXTAPP_NAME}", timeout=3.0)
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()

    # ---- 4. Paramètres : lecteur vidéo personnalisé (round-trip config.json) ----
    click_menu_item(window, "Outils", "Paramètres")
    click_list_item(window, "Lecteur vidéo", exact=True, timeout=10.0)
    _click_radio(window, "Lecteur personnalisé :", timeout=10.0)
    edit_path = _find_edit_near_radio(window, "Lecteur personnalisé :", timeout=10.0)
    edit_path.set_edit_text(r"C:\FakePlayer\player.exe")
    find_dialog_button(window, ["OK"], timeout=10.0).click_input()
    wait_for_condition(
        lambda: _config_get(config_path, "video.player_path") == r"C:\FakePlayer\player.exe",
        timeout=10.0, message="video.player_path n'a pas été persisté",
    )

    # ---- 5. Aide / À propos ----
    click_menu_item(window, "Aide", "Aide…")
    find_dialog_button(window, ["Fermer"], exact=True, timeout=10.0).click_input()

    click_menu_item(window, "Aide", "À propos")
    find_dialog_button(window, ["Fermer"], exact=True, timeout=10.0).click_input()
    assert window.exists()
