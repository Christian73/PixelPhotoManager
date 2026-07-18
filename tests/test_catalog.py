# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/library/catalog.py` en pur Python (sqlite3, sans Qt) : les trois
migrations automatiques au démarrage (chacune pré-semée avec l'ancien schéma
brut, sans passer par Catalog), le CRUD de base, les groupes de doublons,
`cleanup_asset_dirs` et les comptages."""
import os
import sqlite3

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
        """Deux chemins qui ne diffèrent que par le séparateur ('/' vs '\\')
        doivent fusionner en une seule ligne après migration (le premier vu
        est conservé)."""
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
        # Schéma post-migration vidéo mais pré-migration doublons.
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

        # La colonne existe et set_duplicate_groups()/get_duplicate_groups() fonctionnent
        # (deux membres : un groupe de 1 serait dissous par l'invariant, cf.
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
        # Ré-instancier sur la même DB ré-exécute toutes les migrations : ne
        # doit pas planter (pattern try/ALTER TABLE/except déjà en place).
        Catalog(db_path=db_path)


class TestPhotoCrud:
    def test_add_or_update_photo_inserts_then_updates(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        photo = _make_photo("C:/photos/a.jpg", width=100, height=200)

        saved = catalog.add_or_update_photo(photo)
        assert saved.width == 100

        updated = _make_photo("C:/photos/a.jpg", width=999, height=200)
        saved_again = catalog.add_or_update_photo(updated)

        assert saved_again.width == 999
        assert len(catalog.get_all_photos()) == 1  # ON CONFLICT DO UPDATE, pas de doublon

    def test_add_or_update_photo_preserves_favorite_across_rescan(self, tmp_path):
        """is_favorite n'est volontairement PAS dans le ON CONFLICT DO UPDATE :
        un re-scan (qui reconstruit un PhotoInfo neuf, is_favorite=False par
        défaut) ne doit jamais écraser un favori déjà marqué par l'utilisateur
        (cf. set_favorite(), le seul chemin censé le modifier)."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        saved = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.set_favorite(saved.id, True)

        rescanned = catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg", width=50))

        assert rescanned.is_favorite is True

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
        catalog.add_or_update_photo(_make_photo(r"C:\other\c.jpg"))  # hors de l'arborescence déplacée

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
        """Pattern utilisé par le scan de doublons progressif (main_window.py) :
        clear_duplicate_groups() une seule fois au démarrage, puis plusieurs
        appels croissants à set_duplicate_groups() au fil du scan — ne doit
        laisser aucune ligne orpheline ni incohérence."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/c.jpg"))
        catalog.add_or_update_photo(_make_photo("C:/photos/d.jpg"))

        catalog.clear_duplicate_groups()

        # Instantané 1 : un seul groupe trouvé jusqu'ici
        catalog.set_duplicate_groups({
            os.path.normpath("C:/photos/a.jpg"): 1,
            os.path.normpath("C:/photos/b.jpg"): 1,
        })
        groups = catalog.get_duplicate_groups()
        assert set(groups.keys()) == {1}
        assert {p.filename for p in groups[1]} == {"a.jpg", "b.jpg"}

        # Instantané 2 : un second groupe apparaît, le premier ne change pas
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
        """Technique utilisée par _apply_duplicate_results (main_window.py) pour
        effacer les groupes obsolètes après une passe incrémentale :
        set_duplicate_groups({p: None for p in stale}). Ici un 3e membre reste
        dans le groupe pour vérifier que seul le chemin explicitement effacé
        perd son groupe (cf. test suivant pour le cas où plus aucun membre
        valide ne reste)."""
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
        """Régression : un DuplicateDetectorThread en cours au moment d'une
        suppression peut réécrire, via set_duplicate_groups(), le groupe d'un
        membre survivant seul (l'autre membre a disparu de la table `photos`
        entre-temps — l'UPDATE le concernant est un no-op silencieux). Sans
        dissolution automatique ici, ce groupe de 1 restait affiché jusqu'au
        prochain delete_photo(s)/redémarrage (cf. dedup_singleton_groups_any_
        delete_path en mémoire)."""
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo("C:/photos/a.jpg"))
        # b.jpg n'est PAS inséré dans le catalogue : simule sa suppression
        # pendant que le thread de détection avait déjà A et B fusionnés.
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


class TestCounts:
    def test_count_photos_in_folder_is_recursive(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\sub\b.jpg"))
        catalog.add_or_update_photo(_make_photo(r"C:\other\c.jpg"))

        assert catalog.count_photos_in_folder(r"C:\lib") == 2
        assert catalog.count_photos_in_folder(r"C:\other") == 1

    def test_get_stats(self, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.add_or_update_photo(_make_photo(r"C:\lib\a.jpg", file_size=100))
        catalog.add_or_update_photo(_make_photo(r"C:\lib\b.jpg", file_size=200))

        stats = catalog.get_stats()

        assert stats == {"total_photos": 2, "total_size": 300, "folders": 1}


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
        catalog.add_or_update_photo(_make_photo("C:/photos/b.jpg"))  # pas dans l'album
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
        # La photo retirée de l'album reste dans le catalogue.
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
