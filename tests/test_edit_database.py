# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/processing/edit_database.py (EditDatabase) : CRUD, historique,
et migrations, en pur Python (sqlite3, pas de Qt).

Attention : EditDatabase est un singleton par chemin de base (`_instances`,
cf. docstring de la classe). Sans reset explicite entre les tests, deux tests
utilisant le même tmp_path/"edits.db" partageraient la même instance et donc
le même état — chaque test ici réinitialise `EditDatabase._instances = {}`
en setup pour repartir d'un état propre, comme le lot précédent l'a fait pour
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
        """_connect() met la connexion en cache par thread (pattern
        ThumbnailCache) : load() est appelé à chaque navigation dans la
        visionneuse, une connexion neuve par appel coûtait plus cher que la
        requête elle-même."""
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
    """has_edits() normalise son argument (os.path.normpath), comme
    load()/save()/delete()/etc. — corrigé après avoir constaté que ce n'était
    pas le cas : un appelant passant un chemin '/' après un save() qui stocke
    en '\\\\' (Windows) obtenait un faux négatif silencieux. Le seul appelant
    réel (picasa_importer.py) normalisait déjà lui-même en amont par prudence,
    donc aucun bug utilisateur n'était déclenché, mais le piège restait ouvert
    pour tout futur appelant."""

    def test_has_edits_false_when_never_saved(self, tmp_path):
        db = self._make_db(tmp_path)
        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is False

    def test_has_edits_true_after_save(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.2))
        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is True

    def test_has_edits_false_after_unmodified_save(self, tmp_path):
        """save() supprime la ligne photo_edits si l'état n'est plus modifié."""
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(brightness=0.2))
        db.save("C:/photos/a.jpg", EditInfo())  # retour à l'état neutre
        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is False

    def test_has_edits_normalizes_forward_slashes_like_load_and_save(self, tmp_path):
        """Un appelant passant un chemin '/' après un save() en '\\\\' (Windows)
        doit tout de même obtenir True (plus de faux négatif silencieux)."""
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
        db.delete("C:/photos/never_saved.jpg")  # ne doit pas lever


class TestAllEdits(BaseEditDatabaseTest):
    """all_edits() alimente la grille de vignettes (une seule requête pour toute
    la bibliothèque, plutôt qu'un load() par photo affichée). Son cache mémoire
    doit être invalidé par toute écriture, sinon la grille continuerait
    d'afficher l'état d'avant la retouche."""

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
        db.all_edits()                                    # remplit le cache

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
        """Le dict retourné est une copie : la grille le remanie (clés
        normalisées, entrées retirées par refresh_photo) sans que ça affecte
        l'appel suivant."""
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

        assert db.has_edits(os.path.normpath("C:/photos/a.jpg")) is False  # photo_edits inchangée
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
        """Simule un redémarrage de l'appli sur une base déjà migrée : les
        ALTER TABLE des migrations successives doivent tous échouer proprement
        (colonne déjà présente) sans lever."""
        db_path = tmp_path / "edits.db"
        db1 = EditDatabase(db_path=db_path)
        db1.save("C:/photos/a.jpg", EditInfo(brightness=0.2))

        EditDatabase._instances = {}  # force une nouvelle instance -> _init_db() rejoué
        db2 = EditDatabase(db_path=db_path)

        assert db1 is not db2
        assert db2.load("C:/photos/a.jpg").brightness == 0.2


class TestFramePersistence(BaseEditDatabaseTest):
    """Les 13 colonnes frame_* sont arrivées par migration : elles doivent
    survivre à l'aller-retour DB comme à l'ouverture d'une base antérieure."""

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
        """all_edits() alimente l'invalidation des vignettes : un cadre doit y
        apparaître, sinon la grille garderait une vignette sans cadre."""
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(**self._FRAME))
        assert db.all_edits()[os.path.normpath("C:/photos/a.jpg")].frame_style == "glitter"

    def test_history_keeps_the_frame(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save("C:/photos/a.jpg", EditInfo(**self._FRAME))
        history = db.get_history("C:/photos/a.jpg")
        assert history and history[-1][0].frame_type == "double"

    def test_pre_migration_database_is_upgraded(self, tmp_path):
        """Base créée avant la fonctionnalité Cadre : les colonnes manquantes
        sont ajoutées au démarrage, sans perdre les retouches existantes."""
        from src.processing import edit_database as ed

        db_path = tmp_path / "edits.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(ed._CREATE_EDITS)
        conn.execute(ed._CREATE_HISTORY)
        # Toutes les migrations SAUF celle des cadres : l'état exact d'une base
        # de la version précédente.
        for stmt in (ed._MIGRATE_STRAIGHTEN, *ed._MIGRATE_GAMMA_CURVE,
                     *ed._MIGRATE_COLOR_CHANNELS, ed._MIGRATE_RED_EYE,
                     *ed._MIGRATE_VIGNETTE, *ed._MIGRATE_VIGNETTE_V2,
                     ed._MIGRATE_ANNOTATIONS):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass   # colonne déjà dans _CREATE_EDITS
        conn.execute("INSERT INTO photo_edits (photo_path, brightness) VALUES (?, ?)",
                     ("C:\\photos\\a.jpg", 0.4))
        conn.commit()
        conn.close()

        db = EditDatabase(db_path=db_path)
        loaded = db.load("C:/photos/a.jpg")
        assert loaded.brightness == 0.4          # retouche existante préservée
        assert loaded.frame_type == "none"       # défaut, pas de cadre hérité
        # Ferronnerie du second cadre : une ligne existante n'a aucune valeur
        # pour ces colonnes (NULL) — la lecture doit rendre les défauts du modèle.
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

        # Corrompt le fichier pour forcer une exception à la lecture. La
        # connexion en cache (par thread) garde le fichier d'origine ouvert et
        # continuerait de lire via la WAL : on la jette pour que le prochain
        # _connect() rouvre le fichier corrompu — c'est bien le chemin
        # d'exception de load() qui est testé ici, pas la corruption à chaud.
        db_path.write_bytes(b"not a sqlite database")
        db._tls = threading.local()

        loaded = db.load("C:/photos/a.jpg")

        assert loaded == EditInfo()
        EditDatabase._instances = {}
