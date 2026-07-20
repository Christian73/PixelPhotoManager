# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario phare : régression directe du bug de production corrigé en
2026-07 (`Signal(dict)` avec clés int perdant ses données au franchissement
du QThread, voir bugfix_signal_dict_int_keys_2026-07.md). Contrairement à
tests/test_duplicate_detector.py (Tier1/Tier2 en synchrone) et
tests/test_signal_object_cross_thread.py (marshalling Qt isolé), ce scénario
vérifie le chemin complet réel : détection AUTOMATIQUE après le scan (le
déclencheur de production depuis l'évolution « détection continue » —
l'ancien menu « Outils › Détecter les doublons… » n'existe plus) → thread →
catalogue. Repli si l'auto-détection tarde : « Outils › État des doublons… »
→ « Vérifier maintenant ».

Si `Signal(dict)` était réintroduit par erreur, CE scénario échouerait (plus
aucun groupe de doublons en base malgré une détection qui se termine sans
erreur) — c'est exactement la panne silencieuse observée en production."""
import time

import pytest

from tests.e2e.conftest import click_menu_item, find_dialog_button, query_one, wait_for_condition

pytestmark = pytest.mark.e2e


def _group_id(catalog_db, path) -> int | None:
    return query_one(catalog_db, "SELECT duplicate_group_id FROM photos WHERE path=?", (str(path),))


def test_duplicate_detection_groups_exact_resized_and_cropped_pairs(isolated_app):
    manifest = isolated_app.manifest
    catalog_db = isolated_app.catalog_db

    # Le scan automatique au démarrage doit être terminé avant de lancer la
    # détection (get_all_photo_paths_for_dedup() ne verrait sinon qu'une
    # bibliothèque partielle).
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    def _detection_done() -> bool:
        # Détection CONTINUE : les groupes arrivent par instantanés progressifs
        # (Tier 1 d'abord, Tier 2 recadrages ensuite) — attendre les 3 paires,
        # pas seulement la première, avant de vérifier les assertions.
        return all(
            _group_id(catalog_db, pair[0]) is not None
            for pair in (manifest.exact_duplicate_pair,
                         manifest.resized_duplicate_pair,
                         manifest.crop_duplicate_pair)
        )

    # La détection démarre automatiquement après le scan (production). La
    # chaîne de gating (vignettes personnes → migration → détection) peut
    # dépasser la minute sur machine chargée (indexation ONNX en parallèle) —
    # attente généreuse, puis repli « État des doublons… → Vérifier
    # maintenant » retenté deux fois (UIA lent sous charge).
    try:
        wait_for_condition(_detection_done, timeout=90.0, message="")
    except TimeoutError:
        for attempt in range(2):
            try:
                isolated_app.window.set_focus()
                click_menu_item(isolated_app.window, "Outils", "État des doublons…")
                find_dialog_button(
                    isolated_app.window, ["Vérifier maintenant"], timeout=15.0
                ).click_input()
                time.sleep(0.5)
                find_dialog_button(
                    isolated_app.window, ["Fermer"], timeout=15.0
                ).click_input()
                break
            except LookupError:
                if attempt == 1:
                    raise
                time.sleep(2.0)

    wait_for_condition(
        _detection_done, timeout=120.0,
        message="la détection de doublons ne s'est pas terminée (aucun group_id assigné)",
    )

    # Paire exacte (Tier 1)
    ga, gb = (_group_id(catalog_db, p) for p in manifest.exact_duplicate_pair)
    assert ga is not None and ga == gb

    # Paire redimensionnée (Tier 1)
    ga, gb = (_group_id(catalog_db, p) for p in manifest.resized_duplicate_pair)
    assert ga is not None and ga == gb

    # Paire recadrée (Tier 2 — ORB/RANSAC) : preuve que le Tier 2 tourne
    # ET que le résultat traverse bien le thread jusqu'au catalogue.
    ga, gb = (_group_id(catalog_db, p) for p in manifest.crop_duplicate_pair)
    assert ga is not None and ga == gb

    # Les photos témoin ne doivent jamais être groupées.
    for control in manifest.control_photos:
        assert _group_id(catalog_db, control) is None
