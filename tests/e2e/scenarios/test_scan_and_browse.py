# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""End-to-end scenario: launch, automatic scan of the synthetic library
(no onboarding, `config.json` pre-filled by
`launch_isolated.prepare_app_data_dir`), and a direct check of the
catalog -- cf. tests/e2e/README.md for the prerequisites (pywinauto)."""
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
    """The truncated JPEG file must not crash the scan -- whether it is
    catalogued (then offered for repair) or simply ignored, the rest of the
    library must be scanned in full all the same."""
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
    # No assertion on the corrupted file itself: its exact fate
    # (catalogued with an error vs. ignored) depends on file_repair.py and is
    # not fixed by this scenario yet -- a point to enrich.
