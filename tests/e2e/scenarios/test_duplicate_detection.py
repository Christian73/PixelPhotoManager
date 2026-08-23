# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""The flagship scenario: a direct regression of the production bug fixed in
2026-07 (a `Signal(dict)` with int keys losing its data when crossing the
QThread, see bugfix_signal_dict_int_keys_2026-07.md). Unlike
tests/test_duplicate_detector.py (Tier1/Tier2 synchronously) and
tests/test_signal_object_cross_thread.py (isolated Qt marshalling), this
scenario checks the complete real path: AUTOMATIC detection after the scan (the
production trigger since the "continuous detection" evolution --
the former "Tools › Detect the duplicates…" menu no longer exists) -> thread ->
catalog. Fallback if the auto-detection is slow: "Tools › Duplicate status…"
-> "Check now".

If `Signal(dict)` were reintroduced by mistake, THIS scenario would fail (no
duplicate group at all in the database despite a detection that finishes without
an error) -- exactly the silent breakdown observed in production."""
import pytest

from tests.e2e.conftest import query_one, wait_for_condition, wait_for_duplicate_detection

pytestmark = pytest.mark.e2e


def _group_id(catalog_db, path) -> int | None:
    return query_one(catalog_db, "SELECT duplicate_group_id FROM photos WHERE path=?", (str(path),))


def test_duplicate_detection_groups_exact_resized_and_cropped_pairs(isolated_app):
    manifest = isolated_app.manifest
    catalog_db = isolated_app.catalog_db

    # The automatic scan at startup must have finished before starting the
    # detection (get_all_photo_paths_for_dedup() would otherwise only see a
    # partial library).
    wait_for_condition(
        lambda: query_one(
            catalog_db, "SELECT COUNT(*) FROM photos WHERE path=?",
            (str(manifest.control_photos[0]),),
        ) == 1,
        timeout=60.0, message="le scan initial n'a pas terminé",
    )

    # The detection starts automatically after the scan (in production), by
    # progressive snapshots (Tier 1 first, then Tier 2 for the crops) -- the
    # gating chain (person thumbnails -> migration -> detection) can exceed a
    # minute on a loaded machine (ONNX indexing running in parallel).
    wait_for_duplicate_detection(
        isolated_app.window, catalog_db,
        (manifest.exact_duplicate_pair, manifest.resized_duplicate_pair, manifest.crop_duplicate_pair),
        timeout=90.0,
    )

    # Exact pair (Tier 1)
    ga, gb = (_group_id(catalog_db, p) for p in manifest.exact_duplicate_pair)
    assert ga is not None and ga == gb

    # Resized pair (Tier 1)
    ga, gb = (_group_id(catalog_db, p) for p in manifest.resized_duplicate_pair)
    assert ga is not None and ga == gb

    # Cropped pair (Tier 2 -- ORB/RANSAC): proof that Tier 2 runs
    # AND that the result really crosses the thread down to the catalog.
    ga, gb = (_group_id(catalog_db, p) for p in manifest.crop_duplicate_pair)
    assert ga is not None and ga == gb

    # The witness photos must never be grouped.
    for control in manifest.control_photos:
        assert _group_id(catalog_db, control) is None
