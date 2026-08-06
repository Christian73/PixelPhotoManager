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
import pytest

from tests.e2e.conftest import query_one, wait_for_condition, wait_for_duplicate_detection

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

    # La détection démarre automatiquement après le scan (production), par
    # instantanés progressifs (Tier 1 d'abord, Tier 2 recadrages ensuite) — la
    # chaîne de gating (vignettes personnes → migration → détection) peut
    # dépasser la minute sur machine chargée (indexation ONNX en parallèle).
    wait_for_duplicate_detection(
        isolated_app.window, catalog_db,
        (manifest.exact_duplicate_pair, manifest.resized_duplicate_pair, manifest.crop_duplicate_pair),
        timeout=90.0,
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
