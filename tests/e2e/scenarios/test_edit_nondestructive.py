# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : retouche non destructive de bout en bout, dans la
vraie visionneuse.

Chemin exercé : double-clic sur une vignette (photo_activated) -> show_viewer
bascule _left_stack sur l'EditPanel réel -> clic sur le bouton de traitement
"Luminosité" (QToolButton, cf. edit_panel.py::_TREATMENTS/_make_treatment_button)
-> ouverture de LuminositeTreatmentDialog (QDialog non modal, dlg.show()) ->
glissé du QSlider interne (EditSlider -> MarkedSlider -> QSlider, seul slider
visible tant que "Fonctions avancées…" n'est pas coché) -> "Valider" ->
EditPanel._finish() persiste via EditDatabase.save() (table photo_edits,
colonne brightness) -> vérification directe sur edits.db, PAS sur l'UI (la
seule source de vérité pour la non-régression, cf. tests/e2e/conftest.py).

Puis Ctrl+Z (bouton "Annuler" de l'EditPanel, raccourci Ctrl+Z réel,
edit_panel.py:1514) -> re-vérification en base -> ré-ouverture de la même
photo (nouvelle instance logique de visionneuse, undo_stack rechargé depuis
la DB, cf. CLAUDE.md "Retouches non destructives") -> re-vérification de la
persistance de l'annulation."""
import pytest

from tests.e2e.conftest import double_click_element, open_photo_in_viewer, find_dialog_button, find_thumbnail, query_one, wait_for_condition

pytestmark = pytest.mark.e2e

_BRIGHTNESS_TARGET = 0.7  # slider scale=100 -> QSlider.setValue(70)


def _brightness(edits_db, photo_path) -> float | None:
    return query_one(edits_db, "SELECT brightness FROM photo_edits WHERE photo_path=?", (str(photo_path),))


def test_luminosity_edit_applies_persists_and_undoes(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    edits_db = isolated_app.edits_db
    photo = manifest.control_photos[0]

    # Le scan initial doit être terminé pour que la vignette existe dans la grille.
    wait_for_condition(
        lambda: query_one(
            isolated_app.catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo),)
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    assert _brightness(edits_db, photo) is None, "aucune retouche ne doit préexister sur la photo témoin"

    open_photo_in_viewer(window, photo)

    # Bouton de traitement "Luminosité" (QToolButton, texte exact) — descendant
    # de la fenêtre principale, apparaît une fois l'EditPanel affiché (_left_stack -> index 1).
    btn_luminosite = find_dialog_button(window, ["Luminosité"], exact=True, timeout=15.0)
    btn_luminosite.click_input()

    # LuminositeTreatmentDialog : un seul QSlider visible tant que "Fonctions
    # avancées…" n'est pas coché (le gamma slider est masqué par défaut).
    slider = _wait_for_slider(window)
    slider.set_value(int(_BRIGHTNESS_TARGET * 100))

    find_dialog_button(window, ["Valider"], exact=True, timeout=10.0).click_input()

    wait_for_condition(
        lambda: _brightness(edits_db, photo) is not None
        and abs(_brightness(edits_db, photo) - _BRIGHTNESS_TARGET) < 0.02,
        timeout=20.0,
        message="la retouche de luminosité n'a pas été persistée dans edits.db",
    )

    # Annuler (bouton undo de l'EditPanel). Libellé DYNAMIQUE depuis
    # l'évolution UI : « Annuler  <opération> » (ex. « Annuler  Luminosité »),
    # d'où la recherche non exacte — le « Annuler » du dialogue de traitement
    # est fermé à ce stade, pas d'ambiguïté.
    find_dialog_button(window, ["Annuler"], exact=False, timeout=10.0).click_input()

    # État vierge restauré : depuis l'évolution d'EditDatabase, une photo
    # revenue à l'état d'origine voit sa ligne photo_edits SUPPRIMÉE (et non
    # brightness=0) — l'absence de ligne est donc le succès attendu.
    wait_for_condition(
        lambda: _brightness(edits_db, photo) is None or abs(_brightness(edits_db, photo)) < 0.02,
        timeout=20.0,
        message="l'annulation (undo) n'a pas restauré la luminosité d'origine dans edits.db",
    )

    # Re-navigation (retour à la grille puis ré-ouverture) : la persistance de
    # l'annulation ne doit pas dépendre de l'état en mémoire de l'EditPanel.
    # Bouton "✕" de la visionneuse (PhotoViewer → closed → show_grid) — le
    # bouton "▦" de la barre de statut est caché en mode visionneuse.
    find_dialog_button(window, ["✕"], exact=True, timeout=10.0).click_input()
    open_photo_in_viewer(window, photo)
    b = _brightness(edits_db, photo)
    assert b is None or abs(b) < 0.02


def _wait_for_slider(window, timeout: float = 10.0):
    import time
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            sliders = window.descendants(control_type="Slider")
            if sliders:
                return sliders[0].wrapper_object() if hasattr(sliders[0], "wrapper_object") else sliders[0]
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Aucun QSlider trouvé dans le dialogue Luminosité après {timeout}s ({last_exc})")
