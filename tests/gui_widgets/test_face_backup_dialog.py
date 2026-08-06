# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) pour face_backup_dialog — sauvegarde/restauration ZIP de la
reconnaissance faciale sur des FaceDatabase/Catalog réels en tmp_path, threads
exécutés en run() synchrone, dialogues QMessageBox monkeypatchés (jamais de
vraie popup en test)."""
import sqlite3
import zipfile
from pathlib import Path

from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from src.faces.face_database import FaceDatabase
from src.library.catalog import Catalog
from src.ui.face_backup_dialog import (
    FaceBackupDialog, _BackupThread, _RestoreThread,
    _backup_dir, _fmt_size, _parse_ts, create_backup, list_backups,
    restore_backup,
)


def _make_dbs(tmp_path):
    faces_db = tmp_path / "faces.db"
    catalog_db = tmp_path / "catalog.db"
    FaceDatabase(db_path=faces_db)
    catalog = Catalog(db_path=catalog_db)
    return faces_db, catalog_db, catalog


# ---------------------------------------------------------------------------
# helpers

class TestHelpers:
    def test_backup_dir_created(self, tmp_path):
        d = _backup_dir(tmp_path)
        assert d.is_dir() and d.name == "faces_backups"

    def test_parse_ts_valid(self):
        assert _parse_ts(Path("visages_20260627_143210.zip")) == "27 juin 2026 à 14:32"

    def test_parse_ts_invalid_falls_back_to_stem(self):
        assert _parse_ts(Path("visages_nimporte.zip")) == "visages_nimporte"

    def test_fmt_size(self):
        assert _fmt_size(500_000) == "500 Ko"
        assert _fmt_size(2_500_000) == "2.5 Mo"

    def test_list_backups_sorted_desc(self, tmp_path):
        d = _backup_dir(tmp_path)
        (d / "visages_20260101_000000.zip").write_bytes(b"a")
        (d / "visages_20260301_000000.zip").write_bytes(b"b")
        (d / "autre.zip").write_bytes(b"c")   # ignoré (pas le préfixe)

        names = [p.name for p in list_backups(tmp_path)]

        assert names == [
            "visages_20260301_000000.zip",
            "visages_20260101_000000.zip",
        ]


# ---------------------------------------------------------------------------
# create/restore

class TestCreateRestore:
    def test_create_backup_contains_faces_and_persons(self, tmp_path):
        faces_db, catalog_db, catalog = _make_dbs(tmp_path)
        catalog.create_person("Alice")
        catalog.create_person("Boris")

        zip_path = create_backup(faces_db, catalog_db, tmp_path)

        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            assert set(zf.namelist()) == {"faces.db", "persons.json"}
            persons = __import__("json").loads(zf.read("persons.json"))
        assert sorted(p["name"] for p in persons) == ["Alice", "Boris"]
        # Pas de fichier temporaire résiduel
        assert not list(_backup_dir(tmp_path).glob("_tmp_*.db"))

    def test_restore_backup_roundtrip(self, tmp_path):
        faces_db, catalog_db, catalog = _make_dbs(tmp_path)
        alice = catalog.create_person("Alice")
        conn = sqlite3.connect(faces_db)
        conn.execute(
            "INSERT INTO faces (photo_path, bbox_x, bbox_y, bbox_w, bbox_h, person_id)"
            " VALUES ('C:/p.jpg', 1, 2, 3, 4, ?)", (alice.id,)
        )
        conn.commit()
        conn.close()

        zip_path = create_backup(faces_db, catalog_db, tmp_path)

        # État postérieur : personne supprimée, visage effacé
        catalog.delete_person(alice.id)
        conn = sqlite3.connect(faces_db)
        conn.execute("DELETE FROM faces")
        conn.commit()
        conn.close()

        restore_backup(zip_path, faces_db, catalog_db, tmp_path)

        assert [p.name for p in catalog.get_persons()] == ["Alice"]
        conn = sqlite3.connect(faces_db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        finally:
            conn.close()
        assert n == 1
        # Dossier temporaire nettoyé
        assert not (_backup_dir(tmp_path) / "_restore_tmp").exists()

    def test_restore_without_faces_db_raises(self, tmp_path):
        faces_db, catalog_db, _ = _make_dbs(tmp_path)
        bad_zip = tmp_path / "visages_20260101_000000.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("persons.json", "[]")

        import pytest
        with pytest.raises(FileNotFoundError):
            restore_backup(bad_zip, faces_db, catalog_db, tmp_path)


# ---------------------------------------------------------------------------
# threads (run() synchrone)

class TestThreads:
    def test_backup_thread_success(self, qtbot, tmp_path):
        faces_db, catalog_db, _ = _make_dbs(tmp_path)
        t = _BackupThread(faces_db, catalog_db, tmp_path)
        done, errors = [], []
        t.succeeded.connect(done.append)
        t.failed.connect(errors.append)

        t.run()

        assert len(done) == 1 and done[0].exists()
        assert errors == []

    def test_backup_thread_failure(self, qtbot, tmp_path):
        # Dossier de sauvegarde impossible à créer (un FICHIER porte son nom)
        bad_root = tmp_path / "root"
        bad_root.mkdir()
        (bad_root / "faces_backups").write_text("bloque")
        t = _BackupThread(tmp_path / "faces.db", tmp_path / "cat.db", bad_root)
        errors = []
        t.failed.connect(errors.append)

        t.run()

        assert len(errors) == 1

    def test_restore_thread_success_and_failure(self, qtbot, tmp_path):
        faces_db, catalog_db, catalog = _make_dbs(tmp_path)
        catalog.create_person("Alice")
        zip_path = create_backup(faces_db, catalog_db, tmp_path)

        t = _RestoreThread(zip_path, faces_db, catalog_db, tmp_path)
        ok, errors = [], []
        t.succeeded.connect(lambda: ok.append(1))
        t.failed.connect(errors.append)
        t.run()
        assert ok == [1] and errors == []

        t2 = _RestoreThread(tmp_path / "absent.zip", faces_db, catalog_db, tmp_path)
        errors2 = []
        t2.failed.connect(errors2.append)
        t2.run()
        assert len(errors2) == 1


# ---------------------------------------------------------------------------
# dialogue

def _rows(dlg) -> list:
    return [
        dlg._list_vbox.itemAt(i).widget()
        for i in range(dlg._list_vbox.count())
        if dlg._list_vbox.itemAt(i).widget()
    ]


class TestFaceBackupDialog:
    def _make_dialog(self, qtbot, tmp_path):
        faces_db, catalog_db, catalog = _make_dbs(tmp_path)
        dlg = FaceBackupDialog(tmp_path, faces_db, catalog_db)
        qtbot.addWidget(dlg)
        return dlg, faces_db, catalog_db, catalog

    def test_empty_list_shows_placeholder(self, qtbot, tmp_path):
        dlg, *_ = self._make_dialog(qtbot, tmp_path)

        rows = _rows(dlg)
        assert len(rows) == 1
        assert "Aucune sauvegarde" in rows[0].text()

    def test_rows_built_for_existing_backups(self, qtbot, tmp_path):
        d = _backup_dir(tmp_path)
        (d / "visages_20260101_000000.zip").write_bytes(b"x" * 1500)
        (d / "visages_20260201_000000.zip").write_bytes(b"y" * 1500)

        dlg, *_ = self._make_dialog(qtbot, tmp_path)

        rows = _rows(dlg)
        assert len(rows) == 2
        dates = [r.findChildren(QLabel)[0].text() for r in rows]
        assert dates[0] == "1 févr. 2026 à 00:00"   # plus récent en premier

    def test_set_busy_disables_buttons(self, qtbot, tmp_path):
        d = _backup_dir(tmp_path)
        (d / "visages_20260101_000000.zip").write_bytes(b"x")
        dlg, *_ = self._make_dialog(qtbot, tmp_path)

        dlg._set_busy(True, "En cours…")
        assert not dlg._btn_create.isEnabled()
        assert all(
            not b.isEnabled()
            for r in _rows(dlg) for b in r.findChildren(QPushButton)
        )

        dlg._set_busy(False)
        assert dlg._btn_create.isEnabled()

    def test_on_create_real_thread_adds_row(self, qtbot, tmp_path):
        dlg, *_ = self._make_dialog(qtbot, tmp_path)

        dlg._on_create()
        with qtbot.waitSignal(dlg._thread.succeeded, timeout=5000):
            pass
        qtbot.waitUntil(
            lambda: dlg._lbl_status.text().startswith("Sauvegarde créée"),
            timeout=3000,
        )

        assert len(list_backups(tmp_path)) == 1
        assert dlg._btn_create.isEnabled()

    def test_on_delete_confirmed_removes_file_and_row(self, qtbot, tmp_path, monkeypatch):
        d = _backup_dir(tmp_path)
        zip_file = d / "visages_20260101_000000.zip"
        zip_file.write_bytes(b"x")
        dlg, *_ = self._make_dialog(qtbot, tmp_path)
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.Yes),
        )

        row = _rows(dlg)[0]
        dlg._on_delete(zip_file, row)

        assert not zip_file.exists()
        # La liste retombe sur le placeholder "Aucune sauvegarde"
        qtbot.waitUntil(
            lambda: any(
                isinstance(w, QLabel) and "Aucune sauvegarde" in w.text()
                for w in _rows(dlg)
            ),
            timeout=2000,
        )

    def test_on_delete_cancelled_keeps_file(self, qtbot, tmp_path, monkeypatch):
        d = _backup_dir(tmp_path)
        zip_file = d / "visages_20260101_000000.zip"
        zip_file.write_bytes(b"x")
        dlg, *_ = self._make_dialog(qtbot, tmp_path)
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.Cancel),
        )

        dlg._on_delete(zip_file, _rows(dlg)[0])

        assert zip_file.exists()
        assert len(_rows(dlg)) == 1

    def test_on_restore_cancelled_starts_nothing(self, qtbot, tmp_path, monkeypatch):
        d = _backup_dir(tmp_path)
        zip_file = d / "visages_20260101_000000.zip"
        zip_file.write_bytes(b"x")
        dlg, *_ = self._make_dialog(qtbot, tmp_path)
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.Cancel),
        )

        dlg._on_restore(zip_file)

        assert dlg._thread is None

    def test_on_restore_confirmed_emits_restore_completed(self, qtbot, tmp_path, monkeypatch):
        faces_db, catalog_db, catalog = _make_dbs(tmp_path)
        catalog.create_person("Alice")
        zip_path = create_backup(faces_db, catalog_db, tmp_path)
        dlg = FaceBackupDialog(tmp_path, faces_db, catalog_db)
        qtbot.addWidget(dlg)
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.Ok),
        )
        monkeypatch.setattr(
            QMessageBox, "information",
            staticmethod(lambda *a, **k: None),
        )

        with qtbot.waitSignal(dlg.restore_completed, timeout=5000):
            dlg._on_restore(zip_path)

        assert dlg.result() == 1   # accept() appelé après restauration

    def test_on_op_error_reenables_ui(self, qtbot, tmp_path, monkeypatch):
        dlg, *_ = self._make_dialog(qtbot, tmp_path)
        shown = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            staticmethod(lambda *a, **k: shown.append(a[2])),
        )
        dlg._set_busy(True, "x")

        dlg._on_op_error("disque plein")

        assert dlg._btn_create.isEnabled()
        assert shown and "disque plein" in shown[0]
