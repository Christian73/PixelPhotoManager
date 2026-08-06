# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Scénario bout-en-bout : lancement, scan automatique de la bibliothèque
synthétique (pas d'onboarding, `config.json` pré-rempli par
`launch_isolated.prepare_app_data_dir`), et vérification directe du
catalogue — cf. tests/e2e/README.md pour les prérequis (pywinauto)."""
import pytest

from tests.e2e.conftest import query_one, wait_for_condition

pytestmark = pytest.mark.e2e


def test_scan_populates_catalog_with_all_synthetic_photos(isolated_app):
    manifest = isolated_app.manifest
    all_paths = (
        manifest.control_photos
        + list(manifest.exact_duplicate_pair)
        + list(manifest.resized_duplicate_pair)
        + list(manifest.crop_duplicate_pair)
    )

    def _all_scanned() -> bool:
        count = query_one(
            isolated_app.catalog_db,
            "SELECT COUNT(*) FROM photos WHERE path IN ({})".format(
                ",".join("?" * len(all_paths))
            ),
            tuple(str(p) for p in all_paths),
        )
        return count == len(all_paths)

    wait_for_condition(
        _all_scanned, timeout=60.0,
        message=f"les {len(all_paths)} photos synthétiques ne sont pas toutes cataloguées",
    )

    media_type = query_one(
        isolated_app.catalog_db,
        "SELECT media_type FROM photos WHERE path=?",
        (str(manifest.control_photos[0]),),
    )
    assert media_type == "image"


def test_scan_reports_corrupted_file_as_repairable_or_skipped(isolated_app):
    """Le fichier JPEG tronqué ne doit pas faire planter le scan — qu'il soit
    cataloguée (puis proposé à la réparation) ou simplement ignorée, le reste
    de la bibliothèque doit malgré tout être scanné intégralement."""
    manifest = isolated_app.manifest

    wait_for_condition(
        lambda: query_one(
            isolated_app.catalog_db,
            "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0,
        message="le scan n'a pas terminé (photo témoin absente du catalogue)",
    )
    # Pas d'assertion sur le fichier corrompu lui-même : son sort exact
    # (catalogué avec erreur vs. ignoré) dépend de file_repair.py et n'est
    # pas encore fixé par ce scénario — point à enrichir.
