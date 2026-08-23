# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/library/catalog.py` in pure Python (sqlite3, without Qt): the three
automatic migrations at startup (each pre-seeded with the old raw
schema, without going through Catalog), the basic CRUD, the duplicate groups,
`cleanup_asset_dirs` and the counts."""
import os
import sqlite3
from datetime import datetime

from src.core.models import PhotoInfo
from src.library.catalog import Catalog


def _raw_query_all(db, sql, params=()):
    conn = sqlite3.connect(db._db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _make_photo(path: str, **kwargs) -> PhotoInfo:
    return PhotoInfo(path=path, **kwargs)


class TestMigrateNormalizePaths:
    def test_dedups_paths_after_normalization(self, tmp_path):
        """Two paths differing only by the separator ('/' vs '\')
        must merge into a single row after migration (the first one seen
        is kept)."""
        db_path = tmp_path / "catalog.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                filename TEXT,
                directory TEXT,
                date_taken TEXT, width INTEGER, height INTEGER,
                file_size INTEGER, file_mtime REAL,
                camera_make TEXT, camera_model TEXT, lens_model TEXT,
                iso INTEGER, exposure_time TEXT, aperture REAL, focal_length REAL,
                has_gps INTEGER DEFAULT 0, gps_lat REAL, gps_lon REAL,
                is_favorite INTEGER DEFAULT 0, tags TEXT,
                indexed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO photos (path, filename, directory) VALUES (?,?,?)",
            ("C:/Users/x/a.jpg", "a.jpg", "C:/Users/x"),
        )
        conn.execute(
            "INSERT INTO photos (path, filename, directory) VALUES (?,?,?)",
            (r"C:\Users\x\a.jpg", "a.jpg", r"C:\Users\x"),
        )
        conn.commit()
        conn.close()

        catalog = Catalog(db_path=db_path)

        rows = _raw_query_all(catalog, "SELECT path FROM photos")
        assert len(rows) == 1
        assert rows[0][0] == os.path.normpath("C:/Users/x/a.jpg")


class TestMigrateVideoFields:
    def _seed_pre_video_schema(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                filename TEXT,
                directory TEXT,
                date_taken TEXT, width INTEGER, height INTEGER,
                file_size INTEGER, file_mtime REAL,
                camera_make TEXT, camera_model TEXT, lens_model TEXT,
                iso INTEGER, exposure_time TEXT, aperture REAL, focal_length REAL,
                has_gps INTEGER DEFAULT 0, gps_lat REAL, gps_lon REAL,
                is_favorite INTEGER DEFAULT 0, tags TEXT,
                indexed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO photos (path, filename, directory) VALUES (?,?,?)",
            (os.path.normpath("C:/videos/holiday.mp4"), "holiday.mp4", os.path.normpath("C:/videos")),
        )
        conn.execute(
            "INSERT INTO photos (path, filename, directory) VALUES (?,?,?)",
            (os.path.normpath("C:/photos/beach.jpg"), "beach.jpg", os.path.normpath("C:/photos")),
        )
        conn.commit()
        conn.close()

    def test_adds_columns_and_retrofills_existing_videos(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        self._seed_pre_video_schema(db_path)

        catalog = Catalog(db_path=db_path)

        video = catalog.get_photo_by_path(os.path.normpath("C:/videos/holiday.mp4"))
        image = catalog.get_photo_by_path(os.path.normpath("C:/photos/beach.jpg"))
        assert video.media_type == "video"
        assert video.duration == 0.0
        assert image.media_type == "image"


class TestMigrateDuplicateFields:
    def test_adds_duplicate_group_id_column_without_crashing(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        # Schema after the video migration but before the duplicates migration.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                filename TEXT, directory TEXT,
                date_taken TEXT, width INTEGER, height INTEGER,
                file_size INTEGER, file_mtime REAL,
                camera_make TEXT, camera_model TEXT, lens_model TEXT,
                iso INTEGER, exposure_time TEXT, aperture REAL, focal_length REAL,
                has_gps INTEGER DEFAULT 0, gps_lat REAL, gps_lon REAL,
                is_favorite INTEGER DEFAULT 0, tags TEXT,
                indexed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                media_type TEXT DEFAULT 'image', duration REAL DEFAULT 0.0
            )
            """
        )
        conn.executemany(
            "INSERT INTO photos (path, filename, directory) VALUES (?,?,?)",
            [
                (os.path.normpath("C:/photos/a.jpg"), "a.jpg", os.path.normpath("C:/photos")),
                (os.path.normpath("C:/photos/b.jpg"), "b.jpg", os.path.normpath("C:/photos")),
            ],
        )
        conn.commit()
        conn.close()

        catalog = Catalog(db_path=db_path)

        # The column exists and set_duplicate_groups()/get_duplicate_groups() work
        # (two members: a group of 1 would be dissolved by the invariant, cf.
        # TestDuplicateGroups.test_set_duplicate_groups_dissolves_singletons).
        catalog.set_duplicate_groups({
            os.path.normpath("C:/photos/a.jpg"): 1,
            os.path.normpath("C:/photos/b.jpg"): 1,
        })
        groups = catalog.get_duplicate_groups()
        assert 1 in groups
        assert {p.filename for p in groups[1]} == {"a.jpg", "b.jpg"}

    def test_init_db_is_idempotent(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        Catalog(db_path=db_path)
        # Re-instantiating on the same DB runs every migration again: it must
        # not crash (the try/ALTER TABLE/except pattern already in place).
        Catalog(db_path=db_path)


class TestMigrateRatingField:
    def test_adds_rating_column_without_crashing(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        # Schema after the duplicates migration but before the rating migration.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                filename TEXT, directory TEXT,
                date_taken TEXT, width INTEGER, height INTEGER,
                file_size INTEGER, file_mtime REAL,
                camera_make TEXT, camera_model TEXT, lens_model TEXT,
                iso INTEGER, exposure_time TEXT, aperture REAL, focal_length REAL,
                has_gps INTEGER DEFAULT 0, gps_lat REAL, gps_lon REAL,
                is_favorite INTEGER DEFAULT 0, tags TEXT,
                indexed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                media_type TEXT DEFAULT 'image', duration REAL DEFAULT 0.0,
                duplicate_group_id INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO photos (path, filename, directory) VALUES (?,?,?)",
            (os.path.normpath("C:/photos/a.jpg"), "a.jpg", os.path.normpath("C:/photos")),
        )
        conn.commit()
        conn.close()

        catalog = Catalog(db_path=db_path)

        photo = catalog.get_photo_by_path(os.path.normpath("C:/photos/a.jpg"))
        assert photo.rating == 0
        catalog.set_rating(photo.id, 4)
        assert catalog.get_photo_by_path(os.path.normpath("C:/photos/a.jpg")).rating == 4


class TestPhotoCrud:
    def test_add_or_update_photo_inserts_then_updates(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = _make_photo("C:/photos/a.jpg", width=100, height=200)

        saved = catalog.add_or_update_photo(photo)
        assert saved.width == 100

        updated = _make_photo("C:/photos/a.jpg", width=999, height=200)
        saved_again = catalog.add_or_update_photo(updated)

        assert saved_again.width == 999
        assert len(catalog.get_all_photos()) == 1  # ON CONFLICT DO UPDATE, no duplicate

    def test_add_or_update_photo_preserves_favorite_across_rescan(self, tmp_path):
        """is_favorite is deliberately NOT in the ON CONFLICT DO UPDATE:
        a rescan (which rebuilds a fresh PhotoInfo, is_favorite=False by
        default) must never overwrite a favourite already marked by the user
        (cf. set_favorite(), the only path meant to modify it)."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.set_favorite(saved.id, True)

        rescanned = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg", width=50))

        assert rescanned.is_favorite is True

    def test_add_or_update_photo_preserves_rating_across_rescan(self, tmp_path):
        """rating is deliberately NOT in the ON CONFLICT DO UPDATE (the same
        reasoning as is_favorite): a rescan must never overwrite a
        rating already given by the user."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.set_rating(saved.id, 3)

        rescanned = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg", width=50))

        assert rescanned.rating == 3

    def test_add_or_update_photo_preserves_tags_across_rescan(self, tmp_path):
        """tags is deliberately no longer in the ON CONFLICT DO UPDATE (removed
        at the same time as the Keywords feature, the same reasoning as
        is_favorite/rating): a forced rescan must never erase the tags
        already entered by the user."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.set_tags(saved.id, ["vacances", "famille"])

        rescanned = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg", width=50))

        assert rescanned.tags == ["vacances", "famille"]

    def test_search_matches_filename_and_camera(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/sunset.jpg", camera_make="Canon"))
        catalog.add_or_update_photo(_make_photo("C:/photos/portrait.jpg", camera_make="Nikon"))

        by_filename = catalog.search("sunset")
        by_camera = catalog.search("Nikon")

        assert [p.filename for p in by_filename] == ["sunset.jpg"]
        assert [p.filename for p in by_camera] == ["portrait.jpg"]

    def test_delete_photo_removes_single_entry(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))

        catalog.delete_photo(os.path.normpath("C:/photos/a.jpg"))

        assert [p.filename for p in catalog.get_all_photos()] == ["b.jpg"]

    def test_delete_photos_batch(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            catalog.add_or_update_photo(_make_photo(f"C:/photos/{name}"))

        catalog.delete_photos([
            os.path.normpath("C:/photos/a.jpg"),
            os.path.normpath("C:/photos/c.jpg"),
        ])

        assert [p.filename for p in catalog.get_all_photos()] == ["b.jpg"]

    def test_rename_photo_updates_path_filename_directory(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/old.jpg"))

        changed = catalog.rename_photo(
            os.path.normpath("C:/photos/old.jpg"), r"C:\photos\renamed\new.jpg"
        )

        assert changed is True
        photo = catalog.get_photo_by_path(r"C:\photos\renamed\new.jpg")
        assert photo.filename == "new.jpg"
        assert photo.directory == str(__import__("pathlib").Path(r"C:\photos\renamed"))

    def test_rename_photo_returns_false_when_path_unknown(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        changed = catalog.rename_photo(os.path.normpath("C:/photos/missing.jpg"), "C:/photos/new.jpg")
        assert changed is False

    def test_update_paths_prefix_moves_subtree_only(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\old\sub\a.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\old\b.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\other\c.jpg"))  # outside the moved tree

        catalog.update_paths_prefix(r"C:\old", r"C:\new")

        paths = {p.filename: p.path for p in catalog.get_all_photos()}
        assert paths["a.jpg"] == r"C:\new\sub\a.jpg"
        assert paths["b.jpg"] == r"C:\new\b.jpg"
        assert paths["c.jpg"] == os.path.normpath(r"C:\other\c.jpg")


class TestDuplicateGroups:
    def test_set_get_clear_duplicate_groups(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))

        catalog.set_duplicate_groups({
            os.path.normpath("C:/photos/a.jpg"): 1,
            os.path.normpath("C:/photos/b.jpg"): 1,
        })

        groups = catalog.get_duplicate_groups()
        assert set(groups.keys()) == {1}
        assert {p.filename for p in groups[1]} == {"a.jpg", "b.jpg"}
        assert catalog.count_duplicate_groups() == 1

        catalog.clear_duplicate_groups()
        assert catalog.get_duplicate_groups() == {}
        assert catalog.count_duplicate_groups() == 0

    def test_repeated_set_duplicate_groups_after_single_clear(self, tmp_path):
        """Pattern used by the progressive duplicate scan (main_window.py):
        clear_duplicate_groups() a single time at the start, then several
        growing calls to set_duplicate_groups() as the scan goes - must
        leave no orphan row nor inconsistency."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/d.jpg"))

        catalog.clear_duplicate_groups()

        # Snapshot 1: a single group found so far
        catalog.set_duplicate_groups({
            os.path.normpath("C:/photos/a.jpg"): 1,
            os.path.normpath("C:/photos/b.jpg"): 1,
        })
        groups = catalog.get_duplicate_groups()
        assert set(groups.keys()) == {1}
        assert {p.filename for p in groups[1]} == {"a.jpg", "b.jpg"}

        # Snapshot 2: a second group appears, the first does not change
        catalog.set_duplicate_groups({
            os.path.normpath("C:/photos/a.jpg"): 1,
            os.path.normpath("C:/photos/b.jpg"): 1,
            os.path.normpath("C:/photos/c.jpg"): 2,
            os.path.normpath("C:/photos/d.jpg"): 2,
        })
        groups = catalog.get_duplicate_groups()
        assert set(groups.keys()) == {1, 2}
        assert {p.filename for p in groups[1]} == {"a.jpg", "b.jpg"}
        assert {p.filename for p in groups[2]} == {"c.jpg", "d.jpg"}
        assert catalog.count_duplicate_groups() == 2

    def test_ignore_duplicate_group_dissolves_only_that_group(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/d.jpg"))
        catalog.set_duplicate_groups({
            os.path.normpath("C:/photos/a.jpg"): 1,
            os.path.normpath("C:/photos/b.jpg"): 1,
            os.path.normpath("C:/photos/c.jpg"): 2,
            os.path.normpath("C:/photos/d.jpg"): 2,
        })

        catalog.ignore_duplicate_group(1)

        groups = catalog.get_duplicate_groups()
        assert set(groups.keys()) == {2}

    def test_get_duplicate_group_assignments_empty_when_nothing_grouped(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        assert catalog.get_duplicate_group_assignments() == {}

    def test_get_duplicate_group_assignments_reflects_set_duplicate_groups(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))

        a = os.path.normpath("C:/photos/a.jpg")
        b = os.path.normpath("C:/photos/b.jpg")
        catalog.set_duplicate_groups({a: 1, b: 1})

        assert catalog.get_duplicate_group_assignments() == {a: 1, b: 1}

    def test_set_duplicate_groups_none_clears_stale_assignment(self, tmp_path):
        """Technique used by _apply_duplicate_results (main_window.py) to
        erase the obsolete groups after an incremental pass:
        set_duplicate_groups({p: None for p in stale}). Here a 3rd member stays
        in the group to check that only the explicitly erased path
        loses its group (cf. the following test for the case where no valid
        member is left at all)."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))

        a = os.path.normpath("C:/photos/a.jpg")
        b = os.path.normpath("C:/photos/b.jpg")
        c = os.path.normpath("C:/photos/c.jpg")
        catalog.set_duplicate_groups({a: 1, b: 1, c: 1})
        assert catalog.get_duplicate_group_assignments() == {a: 1, b: 1, c: 1}

        catalog.set_duplicate_groups({a: None})

        assert catalog.get_duplicate_group_assignments() == {b: 1, c: 1}
        rows = _raw_query_all(catalog, "SELECT duplicate_group_id FROM photos WHERE path=?", (a,))
        assert rows == [(None,)]

    def test_set_duplicate_groups_dissolves_leftover_singleton(self, tmp_path):
        """Regression: a DuplicateDetectorThread running at the moment of a
        deletion may rewrite, through set_duplicate_groups(), the group of a
        member left alone (the other member disappeared from the `photos` table
        in the meantime - the UPDATE concerning it is a silent no-op). Without
        automatic dissolution here, that group of 1 stayed displayed until the
        next delete_photo(s)/restart (cf. dedup_singleton_groups_any_
        delete_path in memory)."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        # b.jpg is NOT inserted into the catalog: simulates its deletion
        # while the detection thread had already merged A and B.
        b = os.path.normpath("C:/photos/b.jpg")
        a = os.path.normpath("C:/photos/a.jpg")

        catalog.set_duplicate_groups({a: 1, b: 1})

        assert catalog.get_duplicate_group_assignments() == {}


class TestCleanupAssetDirs:
    def test_removes_entries_under_assets_subdirectories(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\photos\LR_assets\preview.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\photos\real.jpg"))

        deleted = catalog.cleanup_asset_dirs()

        assert deleted == [os.path.normpath(r"C:\photos\LR_assets\preview.jpg")]
        assert [p.filename for p in catalog.get_all_photos()] == ["real.jpg"]

    def test_updates_album_photo_count(self, tmp_path):
        # Non-regression: a photo in a *_assets folder and added to an
        # album left an orphan album_photos row after cleanup,
        # inflating AlbumInfo.photo_count beyond the photos really present.
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        asset_photo = catalog.add_or_update_photo(_make_photo(r"C:\photos\LR_assets\preview.jpg"))
        real_photo = catalog.add_or_update_photo(_make_photo(r"C:\photos\real.jpg"))
        catalog.add_photo_to_album(album.id, asset_photo.id)
        catalog.add_photo_to_album(album.id, real_photo.id)

        catalog.cleanup_asset_dirs()

        assert catalog.get_albums()[0].photo_count == 1


class TestCounts:
    def test_count_photos_in_folder_is_recursive(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\sub\b.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\other\c.jpg"))

        assert catalog.count_photos_in_folder(r"C:\lib") == 2
        assert catalog.count_photos_in_folder(r"C:\other") == 1

    def test_get_recursive_photo_counts_sums_subfolders_per_requested_root(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\sub\b.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\sub\deeper\c.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\other\d.jpg"))

        counts = catalog.get_recursive_photo_counts([r"C:\lib", r"C:\lib\sub", r"C:\other"])

        assert counts == {
            os.path.normpath(r"C:\lib"): 3,
            os.path.normpath(r"C:\lib\sub"): 2,
            os.path.normpath(r"C:\other"): 1,
        }

    def test_get_recursive_photo_counts_zero_for_folder_without_photos(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg"))

        counts = catalog.get_recursive_photo_counts([r"C:\empty"])

        assert counts == {os.path.normpath(r"C:\empty"): 0}

    def test_get_recursive_photo_counts_empty_input(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")

        assert catalog.get_recursive_photo_counts([]) == {}

    def test_get_recursive_photo_counts_many_folders_does_not_hit_sqlite_expression_limit(
        self, tmp_path
    ):
        # Non-regression: the original version built a WHERE with one
        # "directory=? OR directory LIKE ?" condition per requested folder - a
        # folder with several hundred subfolders (e.g. _populate_subfolders
        # of the sidebar called on a folder with 1500 children) exceeded the maximum
        # expression tree depth of SQLite (1000) and raised
        # "sqlite3.OperationalError: Expression tree is too large".
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        many_folders = [rf"C:\lib\sub{i}" for i in range(1500)]
        catalog.add_or_update_photo(_make_photo(rf"{many_folders[3]}\a.jpg"))

        counts = catalog.get_recursive_photo_counts(many_folders)

        assert counts[os.path.normpath(many_folders[3])] == 1
        assert counts[os.path.normpath(many_folders[0])] == 0

    def test_get_stats(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg", file_size=100))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\b.jpg", file_size=200))

        stats = catalog.get_stats()

        assert stats == {"total_photos": 2, "total_size": 300, "folders": 1}


class TestThreadLocalConnection:
    """_conn() caches the connection per (instance, thread) - the
    ThumbnailCache pattern generalised in 2026-07. These tests lock down the
    invariants of the refactor: reuse, inter-instance visibility (WAL),
    connection usable after a failed write (rollback guard), and
    absence of exception under concurrent read/write."""

    def test_same_thread_reuses_single_connection(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        conn1 = catalog._conn()
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        assert catalog._conn() is conn1

    def test_two_instances_same_path_see_each_others_writes(self, tmp_path):
        db_path = tmp_path / "catalog.db"
        cat1 = Catalog(db_path=db_path)
        cat2 = Catalog(db_path=db_path)

        p = cat1.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        assert cat2.get_photo_by_path(p.path) is not None
        assert cat1._conn() is not cat2._conn()

    def test_failed_write_leaves_connection_usable(self, tmp_path):
        """A write that fails must not leave the cached connection in the
        middle of a transaction (otherwise: "database is locked" for every
        subsequent write)."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        try:
            # non-existent photo_id + album_id None -> IntegrityError on the PK
            catalog.add_photos_to_album(None, [None])
        except sqlite3.IntegrityError:
            pass
        assert not catalog._conn().in_transaction

        # The connection stays fully usable
        p = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        assert p.id is not None

    def test_concurrent_reader_and_writer(self, tmp_path):
        """One writer (add_or_update_photo in a loop) and one reader
        (get_all_photos in a loop) on the same instance must raise
        no exception (WAL + Python lock)."""
        import threading as _threading

        catalog = Catalog(db_path=tmp_path / "catalog.db")
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(50):
                    catalog.add_or_update_photo(_make_photo(f"C:/photos/w{i}.jpg"))
            except Exception as e:   # pragma: no cover - the test failure is expected
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    catalog.get_all_photos()
            except Exception as e:   # pragma: no cover
                errors.append(e)

        threads = [_threading.Thread(target=writer), _threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(catalog.get_all_photos()) == 50


class TestIndexes:
    def test_query_indexes_exist(self, tmp_path):
        """The indexes that avoid the full scans (favourites, videos, duplicate
        groups) must be created at startup."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        names = {r[1] for r in _raw_query_all(catalog, "PRAGMA index_list('photos')")}
        assert "idx_photos_favorite" in names
        assert "idx_photos_media_type" in names
        assert "idx_photos_dup_group" in names


class TestPersonCrud:
    def test_create_get_person(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        created = catalog.create_person("Alice")

        assert created.name == "Alice"
        fetched = catalog.get_person(created.id)
        assert fetched.name == "Alice"
        assert fetched.id == created.id

    def test_get_person_missing_returns_none(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        assert catalog.get_person(999) is None

    def test_get_persons_ordered_by_name(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.create_person("Zoe")
        catalog.create_person("Alice")

        names = [p.name for p in catalog.get_persons()]

        assert names == ["Alice", "Zoe"]

    def test_rename_person(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        created = catalog.create_person("Alice")

        catalog.rename_person(created.id, "Alicia")

        assert catalog.get_person(created.id).name == "Alicia"

    def test_delete_person_removes_entry(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        created = catalog.create_person("Alice")

        catalog.delete_person(created.id)

        assert catalog.get_person(created.id) is None
        assert catalog.get_persons() == []


class TestAlbumCrud:
    def test_create_get_albums_with_photo_count(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_photo_to_album(album.id, photo.id)

        albums = catalog.get_albums()

        assert len(albums) == 1
        assert albums[0].name == "Vacances"
        assert albums[0].photo_count == 1

    def test_add_photo_to_album_no_duplicate(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        catalog.add_photo_to_album(album.id, photo.id)
        catalog.add_photo_to_album(album.id, photo.id)  # INSERT OR IGNORE

        assert len(catalog.get_photos_in_album(album.id)) == 1

    def test_get_photos_in_album(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))  # not in the album
        catalog.add_photo_to_album(album.id, p1.id)

        photos = catalog.get_photos_in_album(album.id)

        assert [p.filename for p in photos] == ["a.jpg"]

    def test_delete_album_removes_album_but_not_photos(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_photo_to_album(album.id, photo.id)

        catalog.delete_album(album.id)

        assert catalog.get_albums() == []
        assert len(catalog.get_all_photos()) == 1

    def test_delete_photo_updates_album_photo_count(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_photo_to_album(album.id, p1.id)
        catalog.add_photo_to_album(album.id, p2.id)

        catalog.delete_photo(p1.path)

        assert catalog.get_albums()[0].photo_count == 1

    def test_delete_photos_updates_album_photo_count(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_photo_to_album(album.id, p1.id)
        catalog.add_photo_to_album(album.id, p2.id)

        catalog.delete_photos([p1.path, p2.path])

        assert catalog.get_albums()[0].photo_count == 0

    def test_startup_purges_preexisting_orphaned_album_photos(self, tmp_path):
        # Non-regression: orphan album_photos rows (photo_id with no
        # corresponding photos row, e.g. created before the fix of
        # cleanup_asset_dirs) must be purged by the safety net at
        # startup, otherwise AlbumInfo.photo_count stays inflated indefinitely.
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        real_photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_photo_to_album(album.id, real_photo.id)
        conn = sqlite3.connect(catalog._db_path)
        conn.execute(
            "INSERT INTO album_photos (album_id, photo_id) VALUES (?, ?)",
            (album.id, real_photo.id + 999),
        )
        conn.commit()
        conn.close()
        assert catalog.get_albums()[0].photo_count == 2  # orphan counted before the purge

        catalog2 = Catalog(db_path=tmp_path / "catalog.db")

        assert catalog2.get_albums()[0].photo_count == 1

    def test_add_photos_to_album_batch(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_photo_to_album(album.id, p1.id)

        # p1 already present (ignored), p2 new -> a single effective addition
        added = catalog.add_photos_to_album(album.id, [p1.id, p2.id])

        assert added == 1
        assert catalog.get_albums()[0].photo_count == 2
        assert catalog.add_photos_to_album(album.id, []) == 0

    def test_remove_photos_from_album_batch(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        photos = [
            catalog.add_or_update_photo(_make_photo(f"C:/photos/{n}.jpg"))
            for n in "abc"
        ]
        catalog.add_photos_to_album(album.id, [p.id for p in photos])

        catalog.remove_photos_from_album(album.id, [photos[0].id, photos[2].id])

        remaining = {p.path for p in catalog.get_photos_in_album(album.id)}
        assert remaining == {photos[1].path}
        # The removed photos stay in the catalog
        assert catalog.get_photo_by_path(photos[0].path) is not None

    def test_remove_photo_from_album_keeps_photo_in_catalog(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        album = catalog.create_album("Vacances")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_photo_to_album(album.id, p1.id)
        catalog.add_photo_to_album(album.id, p2.id)

        catalog.remove_photo_from_album(album.id, p1.id)

        assert catalog.get_albums()[0].photo_count == 1
        remaining = {p.path for p in catalog.get_photos_in_album(album.id)}
        assert remaining == {p2.path}
        # The photo removed from the album stays in the catalog.
        assert catalog.get_photo_by_path(p1.path) is not None

    def test_get_favorites_only_returns_flagged_photos(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        fav = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_favorite(fav.id, True)

        favorites = catalog.get_favorites()

        assert [p.filename for p in favorites] == ["a.jpg"]

    def test_get_videos_only_returns_video_media_type(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/lib/a.jpg", media_type="image"))
        catalog.add_or_update_photo(_make_photo("C:/lib/b.mp4", media_type="video"))

        videos = catalog.get_videos()

        assert [p.filename for p in videos] == ["b.mp4"]


class TestRating:
    def test_set_rating_clamps_to_0_5_range(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        catalog.set_rating(photo.id, 3)
        assert catalog.get_photo_by_path(photo.path).rating == 3

        catalog.set_rating(photo.id, 99)
        assert catalog.get_photo_by_path(photo.path).rating == 5

        catalog.set_rating(photo.id, -7)
        assert catalog.get_photo_by_path(photo.path).rating == 0

    def test_set_rating_for_ids_applies_to_all(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photos = [
            catalog.add_or_update_photo(_make_photo(f"C:/photos/{n}.jpg"))
            for n in "abc"
        ]

        catalog.set_rating_for_ids([photos[0].id, photos[2].id], 5)

        ratings = {p.path: p.rating for p in catalog.get_all_photos()}
        assert ratings[photos[0].path] == 5
        assert ratings[photos[1].path] == 0
        assert ratings[photos[2].path] == 5

    def test_set_rating_for_ids_empty_list_is_noop(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        catalog.set_rating_for_ids([], 5)  # must not raise

        assert catalog.get_all_photos()[0].rating == 0

    def test_get_photos_min_rating_filters_and_orders(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        p3 = catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))
        catalog.set_rating(p1.id, 2)
        catalog.set_rating(p2.id, 5)
        catalog.set_rating(p3.id, 0)

        at_least_1 = catalog.get_photos_min_rating(1)
        at_least_3 = catalog.get_photos_min_rating(3)

        assert {p.filename for p in at_least_1} == {"a.jpg", "b.jpg"}
        assert [p.filename for p in at_least_3] == ["b.jpg"]

    def test_get_photos_min_rating_default_excludes_unrated(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        assert catalog.get_photos_min_rating() == []


class TestTags:
    def test_set_tags_round_trip(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        catalog.set_tags(photo.id, ["vacances", "plage"])

        assert catalog.get_photo_by_path(photo.path).tags == ["vacances", "plage"]

    def test_set_tags_strips_dedupes_and_rejects_empty_or_comma(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        catalog.set_tags(
            photo.id, [" vacances ", "vacances", "", "  ", "a,b", "plage"]
        )

        assert catalog.get_photo_by_path(photo.path).tags == ["vacances", "plage"]

    def test_set_tags_replaces_previous_list(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.set_tags(photo.id, ["vacances"])

        catalog.set_tags(photo.id, ["famille"])

        assert catalog.get_photo_by_path(photo.path).tags == ["famille"]

    def test_get_all_tags_deduplicated_and_sorted(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_tags(p1.id, ["plage", "vacances"])
        catalog.set_tags(p2.id, ["vacances", "famille"])

        assert catalog.get_all_tags() == ["famille", "plage", "vacances"]

    def test_add_tags_to_photos_unions_without_duplicating(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_tags(p1.id, ["vacances"])

        catalog.add_tags_to_photos([p1.id, p2.id], ["vacances", "plage"])

        assert catalog.get_photo_by_path(p1.path).tags == ["vacances", "plage"]
        assert catalog.get_photo_by_path(p2.path).tags == ["vacances", "plage"]

    def test_add_tags_to_photos_empty_list_is_noop(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        catalog.add_tags_to_photos([], ["vacances"])
        catalog.add_tags_to_photos([photo.id], [])

        assert catalog.get_photo_by_path(photo.path).tags == []

    def test_remove_tag_from_photos_keeps_other_tags(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_tags(p1.id, ["vacances", "plage"])
        catalog.set_tags(p2.id, ["vacances"])

        catalog.remove_tag_from_photos([p1.id, p2.id], "vacances")

        assert catalog.get_photo_by_path(p1.path).tags == ["plage"]
        assert catalog.get_photo_by_path(p2.path).tags == []

    def test_get_photos_by_tag_exact_match_not_substring(self, tmp_path):
        """'vacances' must not match 'vacances2024' (exact element
        match, not substring)."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_tags(p1.id, ["vacances"])
        catalog.set_tags(p2.id, ["vacances2024"])

        matches = catalog.get_photos_by_tag("vacances")

        assert [p.filename for p in matches] == ["a.jpg"]

    def test_get_photos_by_tag_matches_among_several_tags(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.set_tags(photo.id, ["famille", "vacances", "plage"])

        matches = catalog.get_photos_by_tag("vacances")

        assert [p.filename for p in matches] == ["a.jpg"]


class TestGetDistinctCameras:
    def test_returns_sorted_deduplicated_labels(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(
            _make_photo("C:/photos/a.jpg", camera_make="Canon", camera_model="EOS R5")
        )
        catalog.add_or_update_photo(
            _make_photo("C:/photos/b.jpg", camera_make="Canon", camera_model="EOS R5")
        )
        catalog.add_or_update_photo(
            _make_photo("C:/photos/c.jpg", camera_make="Fujifilm", camera_model="X100V")
        )

        assert catalog.get_distinct_cameras() == ["Canon EOS R5", "Fujifilm X100V"]

    def test_ignores_photos_without_camera_model(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        assert catalog.get_distinct_cameras() == []


class TestSearchAdvanced:
    def test_no_criteria_returns_everything(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))

        assert len(catalog.search_advanced({})) == 2

    def test_date_range_is_inclusive(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(
            _make_photo("C:/photos/a.jpg", date_taken=datetime(2024, 6, 15, 10, 0, 0))
        )
        catalog.add_or_update_photo(
            _make_photo("C:/photos/b.jpg", date_taken=datetime(2024, 1, 1, 10, 0, 0))
        )

        matches = catalog.search_advanced({"date_from": "2024-06-01", "date_to": "2024-06-30"})

        assert [p.filename for p in matches] == ["a.jpg"]

    def test_camera_filter_matches_make_and_model(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(
            _make_photo("C:/photos/a.jpg", camera_make="Canon", camera_model="EOS R5")
        )
        catalog.add_or_update_photo(
            _make_photo("C:/photos/b.jpg", camera_make="Fujifilm", camera_model="X100V")
        )

        matches = catalog.search_advanced({"camera": "Canon"})

        assert [p.filename for p in matches] == ["a.jpg"]

    def test_directory_filter_is_recursive(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/2024/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/2024/sub/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/2023/c.jpg"))

        matches = catalog.search_advanced({"directory": "C:/photos/2024"})

        assert {p.filename for p in matches} == {"a.jpg", "b.jpg"}

    def test_min_rating_filter(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_rating(p1.id, 4)
        catalog.set_rating(p2.id, 2)

        matches = catalog.search_advanced({"min_rating": 3})

        assert [p.filename for p in matches] == ["a.jpg"]

    def test_favorites_only_filter(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_favorite(p1.id, True)

        matches = catalog.search_advanced({"favorites_only": True})

        assert [p.filename for p in matches] == ["a.jpg"]

    def test_media_type_filter(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg", media_type="video"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg", media_type="image"))

        matches = catalog.search_advanced({"media_type": "video"})

        assert [p.filename for p in matches] == ["a.jpg"]

    def test_tags_filter_requires_all_listed_tags(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        p2 = catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.set_tags(p1.id, ["vacances", "plage"])
        catalog.set_tags(p2.id, ["vacances"])

        matches = catalog.search_advanced({"tags": ["vacances", "plage"]})

        assert [p.filename for p in matches] == ["a.jpg"]

    def test_combined_criteria(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = catalog.add_or_update_photo(
            _make_photo("C:/photos/a.jpg", camera_make="Canon", camera_model="EOS R5")
        )
        catalog.add_or_update_photo(
            _make_photo("C:/photos/b.jpg", camera_make="Canon", camera_model="EOS R5")
        )
        catalog.set_rating(p1.id, 5)

        matches = catalog.search_advanced({"camera": "Canon", "min_rating": 5})

        assert [p.filename for p in matches] == ["a.jpg"]


class TestIncrementalScanHelpers:
    def test_get_known_mtimes_recursive_under_folder(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg", file_mtime=111.0))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\sub\b.jpg", file_mtime=222.0))
        catalog.add_or_update_photo(_make_photo(r"C:\other\c.jpg", file_mtime=333.0))

        mtimes = catalog.get_known_mtimes(r"C:\lib")

        assert mtimes == {
            os.path.normpath(r"C:\lib\a.jpg"): 111.0,
            os.path.normpath(r"C:\lib\sub\b.jpg"): 222.0,
        }

    def test_get_known_mtimes_empty_folder(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        assert catalog.get_known_mtimes(r"C:\nowhere") == {}

    def test_get_all_paths_under_recursive(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\sub\b.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\other\c.jpg"))

        paths = catalog.get_all_paths_under(r"C:\lib")

        assert paths == {
            os.path.normpath(r"C:\lib\a.jpg"),
            os.path.normpath(r"C:\lib\sub\b.jpg"),
        }

    def test_move_photo_updates_path_directory_filename(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\old.jpg"))

        catalog.move_photo(r"C:\lib\old.jpg", r"C:\lib\renamed\new.jpg")

        photo = catalog.get_photo_by_path(r"C:\lib\renamed\new.jpg")
        assert photo is not None
        assert photo.filename == "new.jpg"
        assert photo.directory == str(__import__("pathlib").Path(r"C:\lib\renamed"))

    def test_get_duplicates_for_group(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))
        catalog.set_duplicate_groups({
            os.path.normpath("C:/photos/a.jpg"): 1,
            os.path.normpath("C:/photos/b.jpg"): 1,
        })

        group = catalog.get_duplicates_for_group(1)

        assert {p.filename for p in group} == {"a.jpg", "b.jpg"}

    def test_get_all_photo_paths_for_dedup_sorted(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))

        paths = catalog.get_all_photo_paths_for_dedup()

        assert paths == sorted(paths)
        assert len(paths) == 2

    def test_get_photos_by_paths_returns_matching_photos_only(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))

        photos = catalog.get_photos_by_paths([
            os.path.normpath("C:/photos/a.jpg"),
            os.path.normpath("C:/photos/c.jpg"),
        ])

        assert {p.filename for p in photos} == {"a.jpg", "c.jpg"}

    def test_get_photos_by_paths_empty_list_returns_empty(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        assert catalog.get_photos_by_paths([]) == []
