# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of src/processing/edit_database.py (EditDatabase): CRUD, history,
and migrations, in pure Python (sqlite3, no Qt).

Beware: EditDatabase is a singleton per database path (`_instances`,
cf. the class docstring). Without an explicit reset between tests, two tests
using the same tmp_path/"edits.db" would share the same instance and therefore
the same state -- every test here resets `EditDatabase._instances = {}` in
setup to start from a clean state, as the previous batch did for
`Config._instance`."""
import os
import sqlite3
import threading

from src.core.models import EditInfo
from src.processing.edit_database import EditDatabase


class BaseEditDatabaseTest:
    def setup_method(self):
        EditDatabase._instances = {}

    def teardown_method(self):
        EditDatabase._instances = {}

    def _make_db(self, tmp_path) -> EditDatabase:
        return EditDatabase(db_path=tmp_path / "edits.db")


class TestConnectionReuse(BaseEditDatabaseTest):
    def test_same_thread_reuses_single_connection(self, tmp_path):
        """_connect() caches the connection per thread (ThumbnailCache
        pattern): load() is called on every navigation in the viewer, a fresh
        connection per call cost more than the query itself."""
        db = self._make_db(tmp_path)
        conn1 = db._connect()
        db.load("C:/photos/a.jpg")
        db.load("C:/photos/b.jpg")
        assert db._connect() is conn1

    def test_wal_mode_enabled(self, tmp_path):
        db = self._make_db(tmp_path)
        mode = db._connect().execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


class TestHasEdits(BaseEditDatabaseTest):
    """has_edits() normalises its argument (os.path.normpath), like
    load()/save()/delete()/etc. -- fixed after noticing it did not: a caller
    passing a '/' path after a save() that stores it as '\\\\' (Windows) got a
    silent false negative. The only real caller (picasa_importer.py) already
    normalised it upstream out of caution, so no user-visible bug was
    triggered, but the trap stayed open for any future caller."""

    def test_has_edits_false_when_never_saved(self, tmp_path):
        db = self._make_db(tmp_path)
        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is False

    def test_has_edits_true_after_save(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.2))
        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is True

    def test_has_edits_false_after_unmodified_save(self, tmp_path):
        """save() removes the photo_edits row when the state is no longer modified."""
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.2))
        db.save("C:/photos/a.jpg", EditInfo())  # back to the neutral state
        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is False

    def test_has_edits_normalizes_forward_slashes_like_load_and_save(self, tmp_path):
        """A caller passing a '/' path after a save() in '\\\\' (Windows) must
        still get True (no more silent false negative)."""
        db = self._make_db(tmp_path)
        db.save(r"C:\photos\a.jpg", EditInfo(brightness=0.2))
        assert db.has_edits("C:/photos/a.jpg") is True


class TestDelete(BaseEditDatabaseTest):
    def test_delete_removes_current_state_and_history(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.2))

        db.delete("C:/photos/a.jpg")

        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is False
        assert db.get_history("C:/photos/a.jpg") == []

    def test_delete_unknown_photo_is_noop(self, tmp_path):
        db = self._make_db(tmp_path)
        db.delete("C:/photos/never_saved.jpg")  # must not raise


class TestAllEdits(BaseEditDatabaseTest):
    """all_edits() feeds the thumbnail grid (a single query for the whole
    library, rather than one load() per displayed photo). Its memory cache must
    be invalidated by every write, otherwise the grid would keep showing the
    state from before the edit."""

    def test_empty_when_nothing_saved(self, tmp_path):
        db = self._make_db(tmp_path)
        assert db.all_edits() == {}

    def test_returns_every_saved_edit_keyed_on_normalized_path(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(rotation=90))
        db.save("C:/photos/b.jpg", EditInfo(brightness=0.2))

        edits = db.all_edits()

        assert set(edits) == {os.path.normpath("C:/photos/a.jpg"),
                              os.path.normpath("C:/photos/b.jpg")}
        assert edits[os.path.normpath("C:/photos/a.jpg")].rotation == 90

    def test_save_invalidates_the_cache(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(rotation=90))
        db.all_edits()                                    # fills the cache

        db.save("C:/photos/a.jpg", EditInfo(rotation=180))

        assert db.all_edits()[os.path.normpath("C:/photos/a.jpg")].rotation == 180

    def test_delete_invalidates_the_cache(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(rotation=90))
        db.all_edits()

        db.delete("C:/photos/a.jpg")

        assert db.all_edits() == {}

    def test_rename_invalidates_the_cache(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/old.jpg", EditInfo(rotation=90))
        db.all_edits()

        db.rename_photo("C:/photos/old.jpg", "C:/photos/new.jpg")

        assert set(db.all_edits()) == {os.path.normpath("C:/photos/new.jpg")}

    def test_caller_cannot_corrupt_the_cache(self, tmp_path):
        """The returned dict is a copy: the grid reworks it (normalised keys,
        entries removed by refresh_photo) without that affecting the next call."""
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(rotation=90))

        db.all_edits().clear()

        assert len(db.all_edits()) == 1


class TestRenamePhoto(BaseEditDatabaseTest):
    def test_rename_propagates_to_current_state_and_history(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/old.jpg", EditInfo(brightness=0.3))

        db.rename_photo("C:/photos/old.jpg", "C:/photos/new.jpg")

        assert db.has_edits(os.path.normpath("C:/photos/old.jpg")) is False
        assert db.has_edits(os.path.normpath("C:/photos/new.jpg")) is True
        loaded = db.load("C:/photos/new.jpg")
        assert loaded.brightness == 0.3
        history = db.get_history("C:/photos/new.jpg")
        assert len(history) == 1


class TestPushHistoryAndGetHistory(BaseEditDatabaseTest):
    def test_push_history_does_not_change_current_state(self, tmp_path):
        db = self._make_db(tmp_path)
        db.push_history("C:/photos/a.jpg", EditInfo(brightness=0.1), operation="pre-crop")

        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is False  # photo_edits unchanged
        history = db.get_history("C:/photos/a.jpg")
        assert len(history) == 1
        edit, operation = history[0]
        assert operation == "pre-crop"
        assert edit.brightness == 0.1

    def test_get_history_ordered_oldest_to_newest(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.1), operation="op1")
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.2), operation="op2")

        history = db.get_history("C:/photos/a.jpg")

        assert [op for _, op in history] == ["op1", "op2"]

    def test_get_history_respects_limit(self, tmp_path):
        db = self._make_db(tmp_path)
        for i in range(5):
            db.save("C:/photos/a.jpg", EditInfo(brightness=i / 10.0), operation=f"op{i}")

        history = db.get_history("C:/photos/a.jpg", limit=2)

        assert [op for _, op in history] == ["op3", "op4"]

    def test_history_capped_at_history_limit(self, tmp_path):
        db = self._make_db(tmp_path)
        for i in range(55):
            db.save("C:/photos/a.jpg", EditInfo(brightness=(i % 90) / 100.0), operation=f"op{i}")

        conn = sqlite3.connect(db._db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM edit_history WHERE photo_path=?",
            (os.path.normpath("C:/photos/a.jpg"),),
        ).fetchone()[0]
        conn.close()

        assert count <= 50

    def test_get_history_empty_for_unknown_photo(self, tmp_path):
        db = self._make_db(tmp_path)
        assert db.get_history("C:/photos/never.jpg") == []

    def test_get_history_skips_corrupt_state_json(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.1), operation="ok")
        conn = sqlite3.connect(db._db_path)
        conn.execute(
            "INSERT INTO edit_history (photo_path, state_json, operation) VALUES (?, ?, ?)",
            ("C:\\photos\\a.jpg", "{not valid json", "broken"),
        )
        conn.commit()
        conn.close()

        history = db.get_history("C:/photos/a.jpg")

        assert [op for _, op in history] == ["ok"]


class TestSingletonAndReinit(BaseEditDatabaseTest):
    def test_same_path_returns_same_instance(self, tmp_path):
        db_path = tmp_path / "edits.db"
        db1 = EditDatabase(db_path=db_path)
        db2 = EditDatabase(db_path=db_path)
        assert db1 is db2

    def test_different_paths_return_different_instances(self, tmp_path):
        db1 = EditDatabase(db_path=tmp_path / "a.db")
        db2 = EditDatabase(db_path=tmp_path / "b.db")
        assert db1 is not db2

    def test_reinit_on_existing_db_runs_migrations_without_crashing(self, tmp_path):
        """Simulates a restart of the application on an already migrated
        database: the ALTER TABLEs of the successive migrations must all fail
        cleanly (column already present) without raising."""
        db_path = tmp_path / "edits.db"
        db1 = EditDatabase(db_path=db_path)
        db1.save("C:/photos/a.jpg", EditInfo(brightness=0.2))

        EditDatabase._instances = {}  # forces a new instance -> _init_db() replayed
        db2 = EditDatabase(db_path=db_path)

        assert db1 is not db2
        assert db2.load("C:/photos/a.jpg").brightness == 0.2


class TestFramePersistence(BaseEditDatabaseTest):
    """The 13 frame_* columns arrived through a migration: they must survive
    the DB round trip as well as the opening of an older database."""

    _FRAME = dict(
        frame_type="double", frame_width=0.07, frame_inner_width=0.02,
        frame_gap=0.03, frame_style="glitter", frame_color="#123456",
        frame_color2="#abcdef", frame_inner_color="#111111",
        frame_gap_color="#eeeeee", frame_inner_enabled=True,
        frame_inner_motif="scrolls", frame_inner_relief=False,
        frame_inner_ornament=1.7,
    )

    def test_round_trip_keeps_every_field(self, tmp_path):
        db = self._make_db(tmp_path)
        assert db.save("C:/photos/a.jpg", EditInfo(**self._FRAME))
        loaded = db.load("C:/photos/a.jpg")
        for attr, value in self._FRAME.items():
            assert getattr(loaded, attr) == value, attr

    def test_frame_is_seen_by_all_edits(self, tmp_path):
        """all_edits() feeds the thumbnail invalidation: a frame must appear
        there, otherwise the grid would keep a thumbnail with no frame."""
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(**self._FRAME))
        assert db.all_edits()[os.path.normpath("C:/photos/a.jpg")].frame_style == "glitter"

    def test_history_keeps_the_frame(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(**self._FRAME))
        history = db.get_history("C:/photos/a.jpg")
        assert history and history[-1][0].frame_type == "double"

    def test_pre_migration_database_is_upgraded(self, tmp_path):
        """Database created before the Frame feature: the missing columns are
        added at startup, without losing the existing edits."""
        from src.processing import edit_database as ed

        db_path = tmp_path / "edits.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(ed._CREATE_EDITS)
        conn.execute(ed._CREATE_HISTORY)
        # Every migration EXCEPT the frame one: the exact state of a database
        # from the previous version.
        for stmt in (ed._MIGRATE_STRAIGHTEN, *ed._MIGRATE_GAMMA_CURVE,
                     *ed._MIGRATE_COLOR_CHANNELS, ed._MIGRATE_RED_EYE,
                     *ed._MIGRATE_VIGNETTE, *ed._MIGRATE_VIGNETTE_V2,
                     ed._MIGRATE_ANNOTATIONS):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass   # column already in _CREATE_EDITS
        conn.execute("INSERT INTO photo_edits (photo_path, brightness) VALUES (?, ?)",
                     ("C:\\photos\\a.jpg", 0.4))
        conn.commit()
        conn.close()

        db = EditDatabase(db_path=db_path)
        loaded = db.load("C:/photos/a.jpg")
        assert loaded.brightness == 0.4          # existing edit preserved
        assert loaded.frame_type == "none"       # default, no inherited frame
        # Ironwork of the second frame: an existing row has no value at all
        # for those columns (NULL) -- reading must return the model defaults.
        assert loaded.frame_inner_motif == "line"
        assert loaded.frame_inner_relief is True
        assert loaded.frame_inner_ornament == 1.0

        db.save("C:/photos/a.jpg", EditInfo(brightness=0.4, **self._FRAME))
        reloaded = db.load("C:/photos/a.jpg")
        assert reloaded.frame_color == "#123456"
        assert reloaded.frame_inner_motif == "scrolls"
        assert reloaded.frame_inner_relief is False


class TestLoadExceptionPath:
    def test_load_on_unreadable_db_returns_empty_edit_info(self, tmp_path):
        EditDatabase._instances = {}
        db_path = tmp_path / "edits.db"
        db = EditDatabase(db_path=db_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.2))

        # Corrupt the file to force an exception on read. The cached connection
        # (per thread) keeps the original file open and would keep reading through
        # the WAL: we throw it away so that the next _connect() reopens the
        # corrupted file -- what is tested here really is the exception path of
        # load(), not corruption on the fly.
        db_path.write_bytes(b"not a sqlite database")
        db._tls = threading.local()

        loaded = db.load("C:/photos/a.jpg")

        assert loaded == EditInfo()
        EditDatabase._instances = {}
