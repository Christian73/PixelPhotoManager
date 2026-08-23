# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : traitements de retouche non couverts par
test_edit_nondestructive.py (qui ne teste que Luminosité/brightness + undo),
enchaînés dans une seule visionneuse sur une photo témoin.

Chemin exercé pour chaque traitement à curseur simple (Contraste, Couleurs,
Redresser, Vignette) : bouton de l'EditPanel réel -> dialogue non modal réel ->
glissé du QSlider réel -> "Apply" -> persistance vérifiée directement sur
edits.db (jamais l'UI). Rotation/Miroir H/Miroir V : boutons directs, pas de
dialogue, persistance immédiate. Réinitialiser : suppression immédiate de la
ligne photo_edits (sans confirmation — action réversible via « Remettre
toutes les retouches », cf. EditPanel.restore_all). Pour finir, régression prioritaire du
NameError historique (commit 34d8c5e) : GammaCurveWidget plantait à chaque
rendu après un découpage de fichier ayant omis un import — reproduit ici en
cochant réellement les deux cases « Fonctions avancées… » puis « Fonctions
très avancées… » du dialogue Luminosité (PAS en glissant un point de courbe,
le bug se produit au paintEvent, avant toute interaction avec la courbe)."""
import pytest

from tests.e2e.conftest import (
    find_checkbox,
    find_dialog_button,
    invoke_button,
    open_photo_in_viewer,
    query_one,
    wait_for_condition,
)

pytestmark = pytest.mark.e2e


def _row_exists(edits_db, photo_path) -> bool:
    return query_one(
        edits_db, "SELECT COUNT(*) FROM photo_edits WHERE photo_path=?", (str(photo_path),)
    ) == 1


def _column(edits_db, photo_path, column: str):
    return query_one(edits_db, f"SELECT {column} FROM photo_edits WHERE photo_path=?", (str(photo_path),))


def _set_slider(slider, value: float) -> None:
    """EditSlider expose un QSlider interne mis à l'échelle x100 quel que soit
    le nombre de décimales affichées (cf. EditSlider._scale, edit_sliders.py) —
    la valeur brute UIA est donc toujours value*100, y compris pour Redresser
    (-10..10°) dont l'affichage n'a qu'une décimale."""
    slider.set_value(int(round(value * 100)))


def _sliders(window):
    return window.descendants(control_type="Slider")


def _wait_for_n_sliders(window, n: int, timeout: float = 10.0):
    import time
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        last = _sliders(window)
        if len(last) >= n:
            return last
        time.sleep(0.3)
    raise LookupError(f"Seulement {len(last)} QSlider trouvé(s) après {timeout}s, {n} attendus")


def _slider_labeled(window, label_text: str, timeout: float = 10.0):
    """Identifie un QSlider par le QLabel voisin (même ligne, aligné
    verticalement) plutôt que par position dans window.descendants() —
    l'ordre de traversée UIA de ce dernier ne correspond pas forcément à
    l'ordre d'ajout au layout (constaté empiriquement : un slider Rouge/Vert
    indexé positionnellement pouvait recevoir la valeur destinée à l'autre)."""
    import time
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            labels = [t for t in window.descendants(control_type="Text") if t.window_text() == label_text]
            sliders = _sliders(window)
            if labels and sliders:
                lbl_rect = labels[0].rectangle()
                lbl_mid = (lbl_rect.top + lbl_rect.bottom) / 2
                for sl in sliders:
                    r = sl.rectangle()
                    if r.top <= lbl_mid <= r.bottom:
                        return sl
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise LookupError(f"Slider aligné avec le libellé {label_text!r} introuvable après {timeout}s ({last_exc})")


def test_edit_treatments_extended(isolated_app):
    manifest = isolated_app.manifest
    window = isolated_app.window
    edits_db = isolated_app.edits_db
    photo = manifest.control_photos[0]

    wait_for_condition(
        lambda: query_one(
            isolated_app.catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?", (str(photo),)
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )
    assert not _row_exists(edits_db, photo), "aucune retouche ne doit préexister sur la photo témoin"

    open_photo_in_viewer(window, photo)

    # ---- Contraste : dialogue générique à un seul slider ----
    # invoke_button (pattern UIA Invoke), pas click_input() : ce premier clic
    # suit immédiatement open_photo_in_viewer(), qui vient de faire passer
    # _left_stack sur l'EditPanel de façon synchrone (show_viewer()) — la
    # fenêtre n'a pas forcément eu le temps de devenir réellement le
    # premier-plan OS avant qu'un clic souris simulé n'atteigne l'écran (même
    # cause que le piège documenté sur FolderManagerDialog, cf. docstring
    # d'invoke_button) : confirmé empiriquement (instrumentation temporaire)
    # que click_input() ici n'ouvre jamais le dialogue (ni exception, ni log),
    # alors que le bouton retrouvé a un rectangle et un état valides. Le
    # bouton reste visible après invocation (pas de fermeture de dialogue à
    # cet endroit) → wait_gone=False.
    invoke_button(window, ["Contrast"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], 0.6)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "contrast") is not None
        and abs(_column(edits_db, photo, "contrast") - 0.6) < 0.02,
        timeout=20.0, message="le contraste n'a pas été persisté",
    )

    # ---- Couleurs : saturation + RVB (révélés par « Fonctions avancées… ») ----
    # invoke_button, même raison que pour Contraste : ce clic suit immédiatement
    # la fermeture du dialogue Contraste (Valider), qui rend le focus OS à la
    # fenêtre principale — même fenêtre de fragilité que la toute première
    # ouverture après open_photo_in_viewer (confirmé empiriquement : cette même
    # classe de clic a échoué de façon flottante sur Vignette lors d'une
    # exécution ultérieure de ce test, cf. commentaire sur Vignette plus bas).
    invoke_button(window, ["Colours"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], -0.3)   # saturation, toujours le 1er slider du dialogue
    find_checkbox(window, "Advanced options…", timeout=10.0).click_input()
    # Révèle les sliders Rouge/Vert/Bleu (CouleursTreatmentDialog,
    # treatment_dialogs.py:469-478) — identifiés par leur libellé voisin, pas
    # par position (l'ordre de window.descendants() ne suit pas fiablement
    # l'ordre d'ajout au layout, cf. _slider_labeled).
    _wait_for_n_sliders(window, 4)
    sl_r = _slider_labeled(window, "Red")
    _set_slider(sl_r, 0.4)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "saturation") is not None
        and abs(_column(edits_db, photo, "saturation") - (-0.3)) < 0.02
        and abs(_column(edits_db, photo, "color_red") - 0.4) < 0.02,
        timeout=20.0, message="saturation/color_red n'ont pas été persistés",
    )

    # ---- Redresser : dialogue générique, slider « Angle (°) » ----
    # invoke_button : suit la fermeture du dialogue Couleurs (Valider), même
    # fragilité que ci-dessus.
    invoke_button(window, ["Straighten"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], 5.0)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "straighten") is not None
        and abs(_column(edits_db, photo, "straighten") - 5.0) < 0.15,
        timeout=20.0, message="le redressement n'a pas été persisté",
    )

    # ---- Vignette : dialogue dédié, slider « Intensité » uniquement (jamais
    # les poignées de géométrie sur le canevas, hors périmètre) ----
    # invoke_button : suit la fermeture du dialogue Redresser (Valider), même
    # fragilité que ci-dessus — c'est précisément ce clic qui a échoué (par
    # intermittence, timing OS) lors du diagnostic initial de ce fichier.
    invoke_button(window, ["Vignette"], exact=True, timeout=15.0, wait_gone=False)
    sliders = _wait_for_n_sliders(window, 1)
    _set_slider(sliders[0], 0.5)
    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "vignette_strength") is not None
        and abs(_column(edits_db, photo, "vignette_strength") - 0.5) < 0.02,
        timeout=20.0, message="l'intensité de vignette n'a pas été persistée",
    )

    # ---- Rotation / Miroir : boutons directs, persistance immédiate ----
    # invoke_button pour la rotation seulement : suit la fermeture du dialogue
    # Vignette (Valider), même fragilité. Miroir H/V/Réinitialiser qui suivent
    # n'ouvrent ni ne ferment de fenêtre entre eux (la fenêtre principale garde
    # le premier-plan OS en continu) : click_input reste fiable pour eux.
    invoke_button(window, ["↻", "+90°"], timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: _column(edits_db, photo, "rotation") == 90,
        timeout=20.0, message="la rotation +90° n'a pas été persistée",
    )
    # invoke_button pour Miroir H/V/Réinitialiser : click_input s'est avéré
    # flottant sur des enchaînements rapides de boutons directs du même genre
    # (constaté empiriquement sur Miroir V lors du diagnostic de ce fichier,
    # sans transition de fenêtre identifiable comme cause — flakiness générale
    # de SendInput sur cet environnement plutôt qu'un piège structurel isolé).
    invoke_button(window, ["Mirror H"], exact=True, timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: _column(edits_db, photo, "flip_h") == 1,
        timeout=20.0, message="le miroir horizontal n'a pas été persisté",
    )
    invoke_button(window, ["Mirror V"], exact=True, timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: _column(edits_db, photo, "flip_v") == 1,
        timeout=20.0, message="le miroir vertical n'a pas été persisté",
    )

    # ---- Réinitialiser toutes les retouches : sans confirmation (réversible), ligne supprimée ----
    # Libellé sur 2 lignes (edit_panel.py) : UIA renvoie le \n littéral dans window_text().
    invoke_button(window, ["Réinitialiser\ntoutes les retouches"], exact=True, timeout=10.0, wait_gone=False)
    wait_for_condition(
        lambda: not _row_exists(edits_db, photo),
        timeout=20.0, message="la réinitialisation n'a pas supprimé la ligne photo_edits",
    )

    # ---- Régression GammaCurveWidget (commit 34d8c5e) : le simple RENDU du
    # widget plantait avec un NameError avant correctif — reproduire la
    # séquence réelle des deux cases à cocher, pas un glissé de point. ----
    invoke_button(window, ["Brightness"], exact=True, timeout=15.0, wait_gone=False)
    find_checkbox(window, "Advanced options…", timeout=10.0).click_input()
    find_checkbox(window, "Expert options", timeout=10.0).click_input()

    assert isolated_app.app.process.poll() is None, (
        "l'application a quitté de manière inattendue au rendu de GammaCurveWidget "
        "(régression du NameError historique, commit 34d8c5e)"
    )
    assert window.exists(), "la fenêtre principale n'a pas survécu au rendu de GammaCurveWidget"

    find_dialog_button(window, ["Apply"], exact=True, timeout=20.0).click_input()
    wait_for_condition(
        lambda: _column(edits_db, photo, "gamma_use_curve") == 1,
        timeout=20.0, message="gamma_use_curve n'a pas été persisté après validation de la courbe",
    )
