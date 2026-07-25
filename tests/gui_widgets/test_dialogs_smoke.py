# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de fumée pytest-qt des dialogues jusqu'ici à 0 % de couverture :
instanciation avec des bases temporaires, vérification du contenu affiché et
des signaux émis par les boutons principaux. Les popups modales bloquantes
(QMessageBox, QFileDialog) sont neutralisées par monkeypatch — aucune fenêtre
n'a besoin d'être réellement affichée."""
import os
from datetime import datetime, timedelta

import pytest
from PIL import Image
from PySide6.QtWidgets import QFileDialog, QLabel, QListWidget, QMessageBox, QPushButton

from src.core.config import Config
from src.core.models import PhotoInfo
from src.faces.face_database import FaceDatabase
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache


def _make_jpg(path, size=(64, 48)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (90, 90, 90)).save(str(path))
    return os.path.normpath(str(path))


@pytest.fixture
def config():
    """Config est un singleton sur APP_DATA_DIR (redirigé en temp par conftest) :
    on sauvegarde/restaure scan_folders pour isoler chaque test."""
    cfg = Config()
    saved = list(cfg._data.get("scan_folders", []))
    saved_picasa = cfg.get("picasa.import_done", False)
    yield cfg
    cfg._data["scan_folders"] = saved
    cfg.set("picasa.import_done", saved_picasa)


# ------------------------------------------------------------------ HelpDialog


class _InertSignal:
    def connect(self, *a, **k):
        pass

    def disconnect(self, *a, **k):
        pass


class _InertUpdateThread:
    """Pas un QThread : un vrai QThread sans parent auto-détruit par deleteLater
    pendant que son thread OS se termine encore déclenche un fail-fast Qt
    (0xC0000409) dès que l'event loop de pytest-qt traite la destruction."""

    def __init__(self, *a, **k):
        self.checked = _InertSignal()
        self.finished = _InertSignal()

    def start(self):
        pass

    def deleteLater(self):
        pass


class TestHelpDialog:
    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        import src.ui.help_dialog as hd
        monkeypatch.setattr(hd, "UpdateCheckThread", _InertUpdateThread)

    def test_tabs_created(self, qtbot):
        from src.ui.help_dialog import HelpDialog, _TABS
        dlg = HelpDialog()
        qtbot.addWidget(dlg)
        from PySide6.QtWidgets import QTabWidget
        tabs = dlg.findChild(QTabWidget)
        assert tabs.count() == len(_TABS)

    def test_named_tab_selected(self, qtbot):
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog(tab="Doublons")
        qtbot.addWidget(dlg)
        from PySide6.QtWidgets import QTabWidget
        tabs = dlg.findChild(QTabWidget)
        assert tabs.tabText(tabs.currentIndex()) == "Doublons"

    def test_version_check_states(self, qtbot):
        from src.ui import help_dialog as hd
        dlg = hd.HelpDialog()
        qtbot.addWidget(dlg)
        for status, needle in [
            (hd.STATUS_UPDATE_AVAILABLE, "nouvelle version"),
            (hd.STATUS_UP_TO_DATE, "dernière version"),
            (hd.STATUS_VERSION_UNKNOWN, "développement"),
            ("erreur_reseau", "Impossible de vérifier"),
        ]:
            dlg._on_version_checked(status, "9.9.9", "https://exemple/rel")
            assert needle in dlg._about_browser.toHtml()

    def test_close_disconnects(self, qtbot):
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog()
        qtbot.addWidget(dlg)
        dlg.close()   # closeEvent : déconnexion sans exception

    def test_search_finds_text_in_current_tab(self, qtbot):
        """"RANSAC" n'apparaît que dans doublons.html (cf. help_content/), même
        en comparaison insensible à la casse (find() par défaut) — chercher ce
        terme en étant déjà sur cet onglet ne doit pas en changer."""
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog(tab="Doublons")
        qtbot.addWidget(dlg)
        dlg._search_edit.setText("RANSAC")
        tabs = dlg._tabs
        assert tabs.tabText(tabs.currentIndex()) == "Doublons"
        assert tabs.currentWidget().textCursor().selectedText() == "RANSAC"

    def test_search_switches_to_tab_containing_match(self, qtbot):
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog()  # démarre sur "Vue d'ensemble"
        qtbot.addWidget(dlg)
        tabs = dlg._tabs
        assert tabs.tabText(tabs.currentIndex()) == "Vue d'ensemble"

        dlg._search_edit.setText("RANSAC")
        assert tabs.tabText(tabs.currentIndex()) == "Doublons"

    def test_search_not_found_flags_the_search_box(self, qtbot):
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog()
        qtbot.addWidget(dlg)
        dlg._search_edit.setText("zzz_terme_absent_de_toute_page_aide_zzz")
        assert dlg._search_edit.styleSheet() != ""

    def test_search_cleared_resets_style(self, qtbot):
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog()
        qtbot.addWidget(dlg)
        dlg._search_edit.setText("zzz_terme_absent_de_toute_page_aide_zzz")
        assert dlg._search_edit.styleSheet() != ""
        dlg._search_edit.setText("")
        assert dlg._search_edit.styleSheet() == ""


# ------------------------------------------------------------------ FaceCountersDialog


class TestFaceCountersDialog:
    def test_counters_displayed(self, qtbot, tmp_path):
        from src.ui.face_counters_dialog import FaceCountersDialog
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        catalog.create_person("Alice")
        dlg = FaceCountersDialog(face_db, catalog)
        qtbot.addWidget(dlg)
        texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
        assert any("Personnes identifiées : 1" in t for t in texts)
        assert any("Visages détectés : 0" in t for t in texts)


# ------------------------------------------------------------------ ProblemsHistoryDialog


class TestProblemsHistoryDialog:
    def test_entries_shown(self, qtbot, tmp_path):
        from src.core.problems_history import problems_history
        from src.ui.problems_history_dialog import ProblemsHistoryDialog, _ProblemRow
        list_file = tmp_path / "echecs.txt"
        list_file.write_text("a.jpg\n", encoding="utf-8")
        problems_history.add_entry(3, 1, str(list_file))
        problems_history.add_entry(2, 2, None)

        dlg = ProblemsHistoryDialog()
        qtbot.addWidget(dlg)
        rows = dlg.findChildren(_ProblemRow)
        assert len(rows) >= 2
        # le bouton "Ouvrir la liste…" n'est actif que si le fichier existe
        states = sorted(
            b.isEnabled() for r in rows for b in r.findChildren(QPushButton)
        )
        assert True in states and False in states


# ------------------------------------------------------------------ DeletedCorruptedFilesDialog


class TestDeletedCorruptedFilesDialog:
    def test_list_populated(self, qtbot):
        from src.core.deleted_corrupted_files import deleted_corrupted_files
        from src.ui.deleted_corrupted_files_dialog import DeletedCorruptedFilesDialog
        deleted_corrupted_files.add_deleted(["C:\\photos\\cassée.jpg"])
        dlg = DeletedCorruptedFilesDialog()
        qtbot.addWidget(dlg)
        lst = dlg.findChild(QListWidget)
        assert lst.count() >= 1
        assert "cassée.jpg" in lst.item(0).text() or any(
            "cassée.jpg" in lst.item(i).text() for i in range(lst.count())
        )


# ------------------------------------------------------------------ IndexErrorsDialog


class TestIndexErrorsDialog:
    def test_empty_state(self, qtbot, tmp_path):
        from src.ui.index_errors_dialog import IndexErrorsDialog
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
        dlg = IndexErrorsDialog(face_db, cache)
        qtbot.addWidget(dlg)
        texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
        assert any("Aucune erreur" in t for t in texts)

    def test_rows_and_retry_signal(self, qtbot, tmp_path):
        from src.ui.index_errors_dialog import IndexErrorsDialog, _ErrorRow
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
        p1 = _make_jpg(tmp_path / "photos" / "a.jpg")
        p2 = _make_jpg(tmp_path / "photos" / "b.jpg")
        face_db.mark_index_error(p1, "timeout")
        face_db.mark_index_error(p2, "crash")

        dlg = IndexErrorsDialog(face_db, cache)
        qtbot.addWidget(dlg)
        rows = dlg.findChildren(_ErrorRow)
        assert len(rows) == 2

        requested: list[str] = []
        dlg.retry_requested.connect(requested.append)
        btn = rows[0].findChild(QPushButton)
        btn.click()
        assert requested and requested[0] in (p1, p2)


# ------------------------------------------------------------------ FolderManagerDialog


class TestFolderManagerDialog:
    def _dlg(self, qtbot, tmp_path, config, folders):
        from src.ui.folder_manager_dialog import FolderManagerDialog
        config._data["scan_folders"] = folders
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        dlg = FolderManagerDialog(config, catalog)
        qtbot.addWidget(dlg)
        return dlg

    def test_empty_state(self, qtbot, tmp_path, config):
        dlg = self._dlg(qtbot, tmp_path, config, [])
        texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
        assert any("Aucun dossier configuré" in t for t in texts)

    def test_rows_with_status_and_subdirs(self, qtbot, tmp_path, config):
        from src.ui.folder_manager_dialog import _FolderRow
        good = tmp_path / "bibli"
        (good / "Originals").mkdir(parents=True)
        (good / ".caché").mkdir()
        (good / "vacances").mkdir()
        missing = str(tmp_path / "disparu")

        dlg = self._dlg(qtbot, tmp_path, config, [str(good), missing])
        rows = dlg.findChildren(_FolderRow)
        assert len(rows) == 2

        row_good = next(r for r in rows if r._folder == str(good))
        assert row_good._toggle_btn is not None
        assert "3 sous-dossiers" in row_good._toggle_btn.text()
        assert "2 exclus" in row_good._toggle_btn.text()
        row_good._toggle_subdirs()
        # fenêtre non affichée : on vérifie l'état logique (non-caché + flèche),
        # pas isVisible() qui exige un parent visible
        assert not row_good._subdir_panel.isHidden()
        assert "▼" in row_good._toggle_btn.text()

        row_missing = next(r for r in rows if r._folder == missing)
        texts = [lbl.text() for lbl in row_missing.findChildren(QLabel)]
        assert any("introuvable" in t for t in texts)

    def test_rescan_signal(self, qtbot, tmp_path, config, monkeypatch):
        good = tmp_path / "bibli"
        good.mkdir()
        dlg = self._dlg(qtbot, tmp_path, config, [str(good)])
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        emitted: list[str] = []
        dlg.rescan_requested.connect(emitted.append)
        dlg._on_rescan(str(good))
        assert emitted == [str(good)]

    def test_remove_signal(self, qtbot, tmp_path, config):
        good = tmp_path / "bibli"
        good.mkdir()
        dlg = self._dlg(qtbot, tmp_path, config, [str(good)])
        emitted: list[str] = []
        dlg.folder_removed.connect(emitted.append)
        dlg._on_remove(str(good))
        assert emitted == [str(good)]

    def test_add_folder(self, qtbot, tmp_path, config, monkeypatch):
        dlg = self._dlg(qtbot, tmp_path, config, [])
        new_dir = str(tmp_path / "nouveau")
        os.makedirs(new_dir)
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", lambda *a, **k: new_dir
        )
        emitted: list[str] = []
        dlg.folder_added.connect(emitted.append)
        dlg._on_add()
        assert emitted == [new_dir]

    def test_add_cancelled(self, qtbot, tmp_path, config, monkeypatch):
        dlg = self._dlg(qtbot, tmp_path, config, [])
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
        emitted: list[str] = []
        dlg.folder_added.connect(emitted.append)
        dlg._on_add()
        assert emitted == []


# ------------------------------------------------------------------ PicasaImportDialog


class TestPicasaImportDialog:
    def _setup_picasa_folder(self, tmp_path):
        photos = tmp_path / "photos"
        _make_jpg(photos / "p1.jpg")
        (photos / "picasa.ini").write_text(
            "[Contacts2]\naa01=Alice;;\n"
            "[p1.jpg]\nfaces=rect64(1999199966663333),aa01\n",
            encoding="utf-8",
        )
        return photos

    def test_stats_displayed(self, qtbot, tmp_path, config, monkeypatch):
        from src.faces import picasa_importer as pi
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: None)
        from src.ui.picasa_import_dialog import PicasaImportDialog
        photos = self._setup_picasa_folder(tmp_path)
        config._data["scan_folders"] = [str(photos)]
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")

        dlg = PicasaImportDialog(config, catalog, face_db)
        qtbot.addWidget(dlg)
        texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
        assert any("1 personne(s)" in t for t in texts)
        assert any("1 photo(s) avec des visages" in t for t in texts)
        assert dlg._btn_import.isEnabled()

    def test_full_import_flow(self, qtbot, tmp_path, config, monkeypatch):
        from src.faces import picasa_importer as pi
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: None)
        from src.ui.picasa_import_dialog import PicasaImportDialog
        photos = self._setup_picasa_folder(tmp_path)
        config._data["scan_folders"] = [str(photos)]
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")

        dlg = PicasaImportDialog(config, catalog, face_db)
        qtbot.addWidget(dlg)
        dlg._on_import()
        # pas de waitSignal sur dlg._thread.finished : le thread peut émettre
        # avant l'abonnement (course classique) — attendre l'état final de l'UI
        qtbot.waitUntil(lambda: dlg._btn_skip.text() == "Fermer", timeout=15000)

        assert config.get("picasa.import_done") is True
        assert "personne(s) créée(s)" in dlg._lbl_status.text()
        assert {p.name for p in catalog.get_persons()} == {"Alice"}

    def test_skip_rejects(self, qtbot, tmp_path, config, monkeypatch):
        from src.faces import picasa_importer as pi
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: None)
        from src.ui.picasa_import_dialog import PicasaImportDialog
        config._data["scan_folders"] = []
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        dlg = PicasaImportDialog(config, catalog, face_db)
        qtbot.addWidget(dlg)
        dlg._on_skip()
        assert dlg.result() == 0   # rejected

    def test_check_and_prompt_already_done(self, qtbot, tmp_path, config):
        from src.ui.picasa_import_dialog import check_and_prompt
        config.set("picasa.import_done", True)
        assert check_and_prompt(config, None, None) is False

    def test_check_and_prompt_no_folders(self, qtbot, tmp_path, config, monkeypatch):
        from src.faces import picasa_importer as pi
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: None)
        from src.ui.picasa_import_dialog import check_and_prompt
        config.set("picasa.import_done", False)
        config._data["scan_folders"] = []
        assert check_and_prompt(config, None, None) is False

    def test_check_and_prompt_shows_dialog(self, qtbot, tmp_path, config, monkeypatch):
        from src.faces import picasa_importer as pi
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: None)
        from src.ui.picasa_import_dialog import PicasaImportDialog, check_and_prompt
        photos = self._setup_picasa_folder(tmp_path)
        config.set("picasa.import_done", False)
        config._data["scan_folders"] = [str(photos)]
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        face_db = FaceDatabase(db_path=tmp_path / "faces.db")
        monkeypatch.setattr(PicasaImportDialog, "exec", lambda self: 0)
        assert check_and_prompt(config, catalog, face_db) is True


# ------------------------------------------------------------------ ExifDateSyncDialog


class TestExifDateSync:
    def test_sync_thread_synchronous(self, qtbot, tmp_path):
        from src.ui.exif_date_sync_dialog import _SyncThread
        catalog = Catalog(db_path=tmp_path / "catalog.db")

        # 1. fichier absent  2. pas de date EXIF  3. à mettre à jour
        p_missing = os.path.normpath(str(tmp_path / "absent.jpg"))
        catalog.add_or_update_photo(
            PhotoInfo(path=p_missing, file_size=1, file_mtime=1.0,
                      date_taken=datetime(2020, 5, 6))
        )
        p_nodate = _make_jpg(tmp_path / "sans_date.jpg")
        catalog.add_or_update_photo(
            PhotoInfo(path=p_nodate, file_size=1, file_mtime=1.0)
        )
        p_update = _make_jpg(tmp_path / "a_corriger.jpg")
        old_date = datetime.now() - timedelta(days=365 * 5)
        catalog.add_or_update_photo(
            PhotoInfo(path=p_update, file_size=1, file_mtime=1.0,
                      date_taken=old_date)
        )

        thread = _SyncThread(catalog)
        results: list = []
        thread.finished.connect(lambda u, s, c: results.append((u, s, c)))
        thread.run()

        updated, skipped, csv_path = results[0]
        assert updated == 1
        assert skipped == 2
        assert os.path.exists(csv_path)
        # la date de création Windows a bien été remplacée par la date EXIF
        assert abs(os.stat(p_update).st_ctime - old_date.timestamp()) < 3

    def test_dialog_flow(self, qtbot, tmp_path):
        from src.ui.exif_date_sync_dialog import ExifDateSyncDialog
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        dlg = ExifDateSyncDialog(catalog)
        qtbot.addWidget(dlg)
        assert dlg._btn_start.isEnabled()

        dlg._start()
        # cf. test_full_import_flow : attendre l'état final de l'UI, pas le
        # signal du thread (course si le thread finit avant l'abonnement)
        qtbot.waitUntil(lambda: dlg._lbl_result.isVisibleTo(dlg), timeout=15000)
        assert "0 fichier(s) mis à jour" in dlg._lbl_result.text()

    def test_open_csv_uses_startfile(self, qtbot, tmp_path, monkeypatch):
        from src.ui import exif_date_sync_dialog as mod
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        dlg = mod.ExifDateSyncDialog(catalog)
        qtbot.addWidget(dlg)
        csv = tmp_path / "rapport.csv"
        csv.write_text("x", encoding="utf-8")
        opened: list[str] = []
        monkeypatch.setattr(mod.os, "startfile", opened.append, raising=False)
        dlg._csv_path = str(csv)
        dlg._open_csv()
        assert opened == [str(csv)]
