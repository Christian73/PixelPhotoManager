# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Contrôleur doublons & fichiers corrompus (extrait de MainWindow)."""
import ctypes
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFrame, QGroupBox,
    QMainWindow, QMenuBar, QWidget, QHBoxLayout, QVBoxLayout,
    QRadioButton, QScrollBar, QSplitter, QStackedWidget, QStatusBar, QToolBar,
    QLineEdit, QSlider, QLabel, QPushButton,
    QFileDialog, QInputDialog, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QSizePolicy,
)

from src.core.config import Config
from src.core.event_bus import bus
from src.core.models import PhotoInfo, AlbumInfo, PersonInfo, EditInfo
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.library.folder_watcher import FolderWatcher
from src.library.scanner import LibraryScanner
from src.library.duplicate_detector import DuplicateDetectorThread
from src.library.dedup_cache import DedupCache
from src.library.exif_reader import preserve_file_dates
from src.core.app_version import get_app_version
from src.core.update_checker import UpdateCheckThread, STATUS_UPDATE_AVAILABLE
from src.faces.face_database import FaceDatabase
from src.faces.face_indexer import FaceIndexThread, SingleFaceReindexThread, RetryFaceIndexThread, ForceRedetectThread, TFWarmUpThread, SimilaritySearchThread
from src.faces.clusterer import ClusterThread
from src.processing.edit_database import EditDatabase
from src.ui.sidebar import Sidebar, _SPECIAL_ALL, _SPECIAL_FAV, _SPECIAL_VIDEOS, _SPECIAL_FILENAME
from src.ui.thumbnail_grid import ThumbnailGrid
from src.ui.photo_viewer import PhotoViewer
from src.ui.edit_panel import EditPanel, MarkedSlider
from src.ui.face_cluster_grid import FaceClusterGrid
from src.ui.person_cluster_view import PersonClusterView
from src.ui.duplicate_grid import DuplicateGrid
from src.ui.face_panel import FacePanel
from src.ui.exif_panel import ExifPanel
from src.ui.people_panel import MergePersonsDialog, PeopleDialog
from src.ui.settings_dialog import SettingsDialog
from src.ui.display_order_dialog import DisplayOrderDialog
from src.ui.face_backup_dialog import FaceBackupDialog

logger = logging.getLogger(__name__)


# Classes extraites de ce fichier (2026-07) — importées sous leurs noms
# historiques : elles restent des détails d'implémentation de MainWindow.
from src.ui.ui_utils import fmt_size as _fmt_size  # noqa: E402
from src.ui.background_workers import (  # noqa: E402
    _CatalogLoadThread, _DeleteWorkerThread, _DupMigrationThread,
    _PersonsRefreshThread, _PhotoQueryThread, _ResetWorkerThread,
    _ResuggestThread,
)
from src.ui.export_dialogs import _ExportDialog, _SaveOptionsDialog  # noqa: E402
from src.ui.reset_faces_dialog import _ResetFacesDialog  # noqa: E402
from src.ui.duplicates_popup import _DuplicatesPopup  # noqa: E402
from src.core.i18n import translate





class DuplicatesController:
    """Contrôleur doublons & fichiers corrompus (extrait de MainWindow).

    Mixin de MainWindow — aucune instanciation autonome : les attributs
    (self._catalog, self._sidebar, …) sont créés par MainWindow.__init__."""

    @Slot()
    def _on_persons_thumbnails_ready_start_duplicates(self) -> None:
        try:
            self._sidebar.persons_thumbnails_ready.disconnect(
                self._on_persons_thumbnails_ready_start_duplicates
            )
        except (RuntimeError, TypeError):
            pass
        # La migration des groupes à dates conflictuelles (_DupMigrationThread)
        # doit être terminée avant d'amorcer seed_groups : en pratique elle
        # finit bien avant le chargement des vignettes de personnes, la garde
        # couvre le premier lancement après upgrade (migration longue).
        if self._dup_migration_thread is not None and self._dup_migration_thread.isRunning():
            self._dup_migration_thread.finished.connect(self._start_duplicate_detection)
            return
        self._start_duplicate_detection()

    def _start_duplicate_detection(self) -> None:
        if self._duplicate_thread and self._duplicate_thread.isRunning():
            return

        paths = self._catalog.get_all_photo_paths_for_dedup()
        if not paths:
            return

        seed_groups = self._catalog.get_duplicate_group_assignments()
        dates = self._catalog.get_photo_dates_for_dedup()
        # seed_groups reflète déjà tout ✗ cliqué avant ce lancement — repartir
        # d'un ensemble vide pour ce nouveau passage (cf. _on_duplicate_group_ignored).
        self._duplicate_ignored_paths = set()

        detector = DuplicateDetectorThread(
            paths, seed_groups=seed_groups, parent=self, dates=dates
        )
        self._duplicate_thread = detector
        self._dup_progress = (0, max(len(paths), 1) * 2,
                              translate("DuplicatesController", "Starting…"))

        def _on_partial(groups: dict, corrupted: list):
            self._update_corrupted_indicator(corrupted)
            self._apply_duplicate_results(groups, corrupted, seed_groups=seed_groups)

        def _on_progress(current: int, total: int, message: str):
            self._dup_progress = (current, total, message)

        def _on_done(groups: dict):
            self._duplicate_grid.set_scanning(False)
            self._last_duplicate_check = datetime.now()
            self._dup_progress = None
            self._apply_duplicate_results(groups, detector.corrupted_paths, seed_groups=seed_groups)

        def _on_error(msg: str):
            logger.warning("Détection de doublons : %s", msg)
            self._duplicate_grid.set_scanning(False)
            self._dup_progress = None

        def _on_cancelled():
            self._duplicate_grid.set_scanning(False)
            self._dup_progress = None

        detector.partial_results.connect(_on_partial)
        detector.progress.connect(_on_progress)
        detector.finished.connect(_on_done)
        detector.error.connect(_on_error)
        detector.cancelled.connect(_on_cancelled)
        self._duplicate_grid.set_scanning(True)
        detector.start()

    def _update_corrupted_indicator(self, corrupted_paths: list[str]) -> None:
        """Met à jour le compteur cliquable de fichiers corrompus dans la
        barre de statut, pendant un scan de doublons en cours."""
        self._live_corrupted_paths = list(corrupted_paths)
        n = len(self._live_corrupted_paths)
        if n:
            self._lbl_corrupted.setText("⚠ " + translate(
                "DuplicatesController", "%n corrupted file(s)", None, n))
            self._lbl_corrupted.show()
        else:
            self._lbl_corrupted.hide()

    def _load_persisted_corrupted_paths(self) -> list[str]:
        """Fichiers corrompus connus du dernier passage de détection de
        doublons, persistés dans dedup_cache.db pour survivre à un
        redémarrage de l'application (voir DedupCache.replace_corrupted_paths)."""
        cache = DedupCache()
        cache.open()
        try:
            return cache.get_corrupted_paths()
        finally:
            cache.close()

    def _remove_persisted_corrupted_paths(self, paths) -> None:
        """Retire des chemins précis de la liste persistée (réparation ou
        suppression manuelle réussie), sans attendre le prochain passage
        complet de détection de doublons pour que dedup_cache.db reflète
        l'état réel."""
        paths = list(paths)
        if not paths:
            return
        cache = DedupCache()
        cache.open()
        try:
            cache.remove_corrupted_paths(paths)
        finally:
            cache.close()

    def _show_corrupted_status_dialog(self) -> None:
        """Point d'entrée dédié (menu Outils › Fichiers corrompus…) : résumé
        + actions, plutôt que la liste détaillée (_show_corrupted_list_dialog,
        accessible ici via "Lister…" et déjà utilisée par l'indicateur de la
        barre de statut et "État des doublons…"). Pas de bouton "Supprimer…"
        ici (ni dans _show_corrupted_list_dialog) : l'utilisateur doit
        d'abord tenter une réparation, l'option de suppression n'étant
        proposée que pour les fichiers toujours en échec après coup, via
        _show_repair_result_dialog."""
        corrupted_paths = list(self._live_corrupted_paths)
        n = len(corrupted_paths)

        dlg = QDialog(self)
        dlg.setWindowTitle(translate("DuplicatesController", "Corrupted files"))
        v = QVBoxLayout(dlg)

        if n:
            v.addWidget(QLabel("⚠ " + translate(
                "DuplicatesController",
                "%n corrupted file(s) found by the duplicate analysis (probably unreadable).", None, n)))
        else:
            v.addWidget(QLabel(translate("DuplicatesController", "No corrupted file found.")))

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        btn_list = buttons.addButton(
            translate("DuplicatesController", "List…"), QDialogButtonBox.ActionRole)
        btn_list.setEnabled(bool(n))
        btn_list.clicked.connect(lambda: (dlg.accept(), self._show_corrupted_list_dialog()))
        btn_repair = buttons.addButton(
            translate("DuplicatesController", "Repair…"), QDialogButtonBox.ActionRole)
        btn_repair.setEnabled(bool(n))
        btn_repair.clicked.connect(lambda: (dlg.accept(), self._offer_corrupted_repair(corrupted_paths)))
        buttons.rejected.connect(dlg.reject)
        buttons.button(QDialogButtonBox.Close).setText(translate("DuplicatesController", "Close"))
        buttons.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        v.addWidget(buttons)

        dlg.exec()

    def _show_corrupted_list_dialog(self, _checked: bool = False) -> None:
        corrupted_paths = self._live_corrupted_paths
        if not corrupted_paths:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(translate("DuplicatesController", "Corrupted files"))
        v = QVBoxLayout(dlg)
        lbl_count = QLabel()
        v.addWidget(lbl_count)
        list_widget = QListWidget()
        list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        v.addWidget(list_widget)

        def _refresh(paths) -> None:
            list_widget.clear()
            list_widget.addItems(paths)
            n = len(paths)
            lbl_count.setText(translate(
                "DuplicatesController",
                "%n file(s) could not be read during the current analysis (probably corrupted):", None, n))
            btn_repair.setEnabled(bool(n))
            btn_delete.setEnabled(bool(n))

        def _target_paths() -> list:
            """Fichiers sélectionnés, ou tous si aucune sélection — permet de
            garder l'action « sur toute la liste » en un clic (comportement
            précédent) tout en offrant le ciblage d'une sélection."""
            selected = [item.text() for item in list_widget.selectedItems()]
            return selected if selected else [
                list_widget.item(i).text() for i in range(list_widget.count())
            ]

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        btn_repair = buttons.addButton(
            translate("DuplicatesController", "Repair…"), QDialogButtonBox.ActionRole)
        btn_repair.clicked.connect(
            lambda: self._offer_corrupted_repair(
                _target_paths(), on_done=lambda: _refresh(list(self._live_corrupted_paths))
            )
        )
        btn_delete = buttons.addButton(
            translate("DuplicatesController", "Delete…"), QDialogButtonBox.ActionRole)
        btn_delete.clicked.connect(
            lambda: (self._offer_corrupted_delete(_target_paths()),
                     _refresh(list(self._live_corrupted_paths)))
        )
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        buttons.button(QDialogButtonBox.Close).setText(translate("DuplicatesController", "Close"))
        buttons.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        v.addWidget(buttons)
        dlg.resize(600, 400)
        _refresh(list(corrupted_paths))
        dlg.exec()

    def _apply_duplicate_results(self, groups: dict, corrupted_paths=(),
                                  *, seed_groups: dict | None = None) -> None:
        """Applique un instantané (partiel ou final) de la détection de
        doublons : persiste les groupes en base, met à jour les PhotoInfo en
        mémoire et rafraîchit grille/visionneuse/sidebar. `seed_groups` est
        l'état {path: group_id} connu au lancement de cette passe — tout
        chemin qui y figurait mais n'apparaît plus dans `groups` (groupe
        dissous, réduit à un singleton, ou fichier retiré de la bibliothèque)
        voit son `duplicate_group_id` explicitement effacé."""
        assignments: dict[str, int] = {}
        for gid, members in groups.items():
            for path in members:
                assignments[path] = gid

        # Exclut tout chemin dissous via le bouton ✗ pendant ce passage : le
        # thread de détection peut encore les avoir fusionnés dans son état
        # interne (capturé avant l'ignore) — cf. _on_duplicate_group_ignored.
        if self._duplicate_ignored_paths:
            assignments = {p: gid for p, gid in assignments.items()
                           if p not in self._duplicate_ignored_paths}

        stale = (set(seed_groups) - set(assignments)) if seed_groups else set()

        self._catalog.set_duplicate_groups(assignments)
        if stale:
            self._catalog.set_duplicate_groups({p: None for p in stale})

        for photo in self._current_photos:
            if photo.path in assignments:
                photo.duplicate_group_id = assignments[photo.path]
            elif photo.path in stale:
                photo.duplicate_group_id = None

        ui_assignments = dict(assignments)
        ui_assignments.update({p: None for p in stale})
        self._grid.refresh_duplicate_status(ui_assignments)

        cp = self._viewer.current_photo()
        if cp and cp.path in ui_assignments:
            cp.duplicate_group_id = ui_assignments[cp.path]
            self._viewer._update_dup_badge()

        self._sidebar.update_duplicates_badge(len(groups))
        if self._stack.currentIndex() == 4:
            self._duplicate_grid.refresh()
        else:
            self._duplicate_grid.invalidate()

    def _show_duplicate_status_dialog(self) -> None:
        """État instantané (lecture seule) de la détection de doublons — la
        détection tourne en continu en arrière-plan, ce dialogue remplace
        l'ancien déclenchement manuel avec rapport de fin. Si une passe est en
        cours, une barre de progression (alimentée par self._dup_progress,
        mis à jour par le signal `progress` du thread — cf.
        _start_duplicate_detection) se met à jour en direct tant que le
        dialogue reste ouvert."""
        thread = self._duplicate_thread
        running = bool(thread and thread.isRunning())

        n_groups = self._catalog.count_duplicate_groups()
        n_photos = len(self._catalog.get_duplicate_group_assignments())

        dlg = QDialog(self)
        dlg.setWindowTitle(translate("DuplicatesController", "Duplicate status"))
        v = QVBoxLayout(dlg)

        v.addWidget(QLabel(
            translate("DuplicatesController", "%n duplicate group(s)", None, n_groups)
            + " ("
            + translate("DuplicatesController", "%n photo(s) affected", None, n_photos)
            + ")."))

        if running:
            status_text = translate("DuplicatesController", "Analysis running…")
        elif self._last_duplicate_check is not None:
            status_text = translate(
                "DuplicatesController", "Last check: {when}"
            ).format(when=self._last_duplicate_check.strftime(
                translate("DuplicatesController", "%m/%d/%Y %H:%M")))
        else:
            status_text = translate(
                "DuplicatesController", "Last check: never")
        lbl_status = QLabel(status_text)
        v.addWidget(lbl_status)

        progress_bar = None
        lbl_progress = None
        if running:
            progress_bar = QProgressBar()
            lbl_progress = QLabel()
            v.addWidget(progress_bar)
            v.addWidget(lbl_progress)

            def _set_progress(current: int, total: int, message: str) -> None:
                total = max(total, 1)
                current = min(max(current, 0), total)
                remaining = total - current
                progress_bar.setRange(0, total)
                progress_bar.setValue(current)
                prefix = f"{message} — " if message else ""
                lbl_progress.setText(
                    f"{prefix}{current}/{total} ("
                    + translate("DuplicatesController", "%n left", None, remaining)
                    + ")"
                )

            _set_progress(*(self._dup_progress
                            or (0, 1, translate("DuplicatesController", "Starting…"))))

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        btn_groups = buttons.addButton(
            translate("DuplicatesController", "View the groups"), QDialogButtonBox.ActionRole)
        btn_groups.clicked.connect(lambda: (dlg.accept(), self.show_duplicate_grid()))
        btn_check_now = buttons.addButton(
            translate("DuplicatesController", "Check now"),
            QDialogButtonBox.ActionRole)
        btn_check_now.setEnabled(not running)
        btn_check_now.clicked.connect(lambda: (dlg.accept(), self._start_duplicate_detection()))
        buttons.rejected.connect(dlg.reject)
        buttons.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        v.addWidget(buttons)

        if running and thread is not None:
            def _on_progress(current: int, total: int, message: str) -> None:
                _set_progress(current, total, message)

            def _on_terminal(*_args) -> None:
                # Émis par finished/error/cancelled : la passe en cours (celle
                # que ce dialogue affichait) s'est terminée pendant qu'il
                # restait ouvert — fige la barre à 100 % et réactive
                # "Vérifier maintenant" plutôt que de laisser un état figé.
                if progress_bar is not None:
                    progress_bar.setValue(progress_bar.maximum())
                if lbl_progress is not None:
                    lbl_progress.setText(translate("DuplicatesController", "Analysis finished."))
                lbl_status.setText(
                    translate("DuplicatesController", "Last check: {when}"
                              ).format(
                        when=self._last_duplicate_check.strftime(
                            translate("DuplicatesController", "%m/%d/%Y %H:%M")))
                    if self._last_duplicate_check
                    else translate("DuplicatesController", "Last check: never")
                )
                btn_check_now.setEnabled(True)

            thread.progress.connect(_on_progress)
            thread.finished.connect(_on_terminal)
            thread.error.connect(_on_terminal)
            thread.cancelled.connect(_on_terminal)

            def _cleanup(*_args) -> None:
                for signal, slot in (
                    (thread.progress, _on_progress),
                    (thread.finished, _on_terminal),
                    (thread.error, _on_terminal),
                    (thread.cancelled, _on_terminal),
                ):
                    try:
                        signal.disconnect(slot)
                    except (RuntimeError, TypeError):
                        pass

            dlg.finished.connect(_cleanup)

        dlg.exec()

    def _record_corrupted_files(self, corrupted_count: int, repaired_count: int,
                                 still_failed: list) -> "str | None":
        """Écrit la liste des fichiers toujours en échec (le cas échéant) et
        enregistre l'entrée dans l'historique des problèmes. Retourne le
        chemin du fichier texte créé, ou None si tout a été réparé."""
        from src.core.app_dirs import APP_DATA_DIR
        from src.core.problems_history import problems_history

        list_path = None
        if still_failed:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            list_path = str(APP_DATA_DIR / f"fichiers_corrompus_{ts}.txt")
            try:
                Path(list_path).write_text("\n".join(still_failed), encoding="utf-8")
            except OSError as e:
                logger.warning("Impossible d'écrire la liste des fichiers corrompus : %s", e)
                list_path = None
        problems_history.add_entry(corrupted_count, repaired_count, list_path)
        return list_path

    def _offer_corrupted_repair(self, corrupted_paths: list, on_done=None) -> None:
        n = len(corrupted_paths)
        reply = QMessageBox.question(
            self,
            translate("DuplicatesController", "Repair the corrupted files"),
            translate("DuplicatesController",
                      "%n file(s) appear to be corrupted.", None, n)
            + "\n\n"
            + translate(
                "DuplicatesController",
                "Attempt an automatic repair? PixelPhotoManager will try to re-save a clean "
                "copy of each file through a more forgiving decoder, keeping the Windows "
                "creation and modification dates. The original is backed up before anything is "
                "changed (hidden .tmp_originals folder next to the file)."),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            list_path = self._record_corrupted_files(n, 0, corrupted_paths)
            msg = translate("DuplicatesController", "The repair was not started.")
            if list_path:
                msg += "\n\n" + translate(
                    "DuplicatesController",
                    "The list of the %n file(s) is available under Tools › Problem history.", None, n)
            QMessageBox.information(self, translate("DuplicatesController", "Repair cancelled"), msg)
            return

        from PySide6.QtWidgets import QProgressDialog
        from src.library.file_repair import FileRepairThread

        progress = QProgressDialog(translate("DuplicatesController", "Repairing…"), translate("DuplicatesController", "Cancel"), 0, n, self)
        progress.setWindowTitle(translate("DuplicatesController", "Repairing corrupted files"))
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        thread = FileRepairThread(corrupted_paths, self)

        def _on_progress(cur, total, path):
            progress.setValue(cur)
            progress.setLabelText(translate(
                "DuplicatesController", "Repair {cur}/{total}:\n{name}"
            ).format(cur=cur + 1, total=total, name=os.path.basename(path)))

        def _on_finished(repaired_count, still_failed):
            progress.setValue(n)
            progress.close()
            self._record_corrupted_files(n, repaired_count, still_failed)

            still_failed_set = set(still_failed)
            repaired_paths = [p for p in corrupted_paths if p not in still_failed_set]
            self._remove_persisted_corrupted_paths(repaired_paths)
            self._update_corrupted_indicator(
                [p for p in self._live_corrupted_paths if p not in set(repaired_paths)]
            )

            # Le contenu du fichier a changé sur disque (nouvelle image
            # ré-enregistrée par file_repair.py) : sans ça, la grille garde
            # l'ancienne vignette (tronquée/corrompue) en cache jusqu'au
            # prochain redémarrage.
            if repaired_paths:
                self._thumb_cache.invalidate_many(repaired_paths)
                for path in repaired_paths:
                    self._grid.refresh_photo(path, None)

            self._show_repair_result_dialog(repaired_paths, list(still_failed))
            if on_done is not None:
                on_done()

        thread.progress.connect(_on_progress)
        thread.finished.connect(_on_finished)
        progress.canceled.connect(thread.cancel)

        thread.start()

    def _show_repair_result_dialog(self, repaired_paths: list[str],
                                     still_failed: list[str]) -> None:
        """Résumé d'un cycle de réparation : chemins réparés, chemins
        toujours en échec, et — pour ces derniers — un bouton pour les
        supprimer directement (réutilise _offer_corrupted_delete, qui
        enregistre les chemins supprimés dans deleted_corrupted_files.py
        pour qu'ils restent retrouvables plus tard dans une sauvegarde)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(translate("DuplicatesController", "Repair finished"))
        v = QVBoxLayout(dlg)

        n_repaired = len(repaired_paths)
        v.addWidget(QLabel(translate(
            "DuplicatesController", "%n file(s) repaired:", None, n_repaired)))
        if repaired_paths:
            list_repaired = QListWidget()
            list_repaired.addItems(repaired_paths)
            v.addWidget(list_repaired, stretch=1)

        n_failed = len(still_failed)
        if still_failed:
            v.addWidget(QLabel(translate(
                "DuplicatesController", "%n file(s) could not be repaired:",
                None, n_failed)))
            list_failed = QListWidget()
            list_failed.addItems(still_failed)
            v.addWidget(list_failed, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        if still_failed:
            btn_delete = buttons.addButton(
                translate("DuplicatesController", "Delete these files…"),
                QDialogButtonBox.ActionRole)
            btn_delete.clicked.connect(
                lambda: (dlg.accept(), self._offer_corrupted_delete(list(still_failed)))
            )
        buttons.rejected.connect(dlg.reject)
        buttons.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        v.addWidget(buttons)

        dlg.resize(560, 420)
        dlg.exec()

    def _offer_corrupted_delete(self, corrupted_paths: list) -> None:
        n = len(corrupted_paths)
        reply = QMessageBox.question(
            self,
            translate("DuplicatesController", "Delete the corrupted files"),
            translate("DuplicatesController",
                      "%n file(s) appear to be corrupted.", None, n)
            + "\n\n"
            + translate(
                "DuplicatesController",
                "Send %n file(s) to the Windows recycle bin?\n\nThey will still be recoverable "
                "from the recycle bin.", None, n),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return

        # Même pipeline que _on_delete_requested : worker partagé (garde de
        # réentrance commune), suppression absorbée par le watcher, épilogue UI
        # dans _on_corrupted_delete_finished.
        if self._delete_thread is not None and self._delete_thread.isRunning():
            self.statusBar().showMessage(translate("DuplicatesController", "A deletion is "
                                                                           "already running…"), 3000)
            return
        self._folder_watcher.notify_self_deletions(list(corrupted_paths))
        worker = _DeleteWorkerThread(
            list(corrupted_paths), self._catalog, self._thumb_cache,
            self._face_db, self,
        )
        self._delete_thread = worker
        worker.progress.connect(
            lambda done, total: self._lbl_action.setText(
                translate("MainWindow", "Deleting… {done}/{total}"
                          ).format(done=done, total=total))
        )
        worker.finished_delete.connect(self._on_corrupted_delete_finished)
        worker.start()

    def _on_corrupted_delete_finished(self, deleted: list, errors: list) -> None:
        """Épilogue UI de la suppression des fichiers corrompus (worker)."""
        self._lbl_action.setText("")
        if deleted:
            deleted_set = set(deleted)
            self._grid.remove_photos(deleted)
            self._current_photos = [p for p in self._current_photos if p.path not in deleted_set]
            self._current_paths -= deleted_set
            self._update_status()

            self._remove_persisted_corrupted_paths(deleted)
            remaining_corrupted = [p for p in self._live_corrupted_paths if p not in deleted_set]
            self._update_corrupted_indicator(remaining_corrupted)

            self._duplicate_grid.invalidate()
            self._sidebar.update_duplicates_badge(self._catalog.count_duplicate_groups())

            from src.core.deleted_corrupted_files import deleted_corrupted_files
            deleted_corrupted_files.add_deleted(deleted)

        if errors:
            QMessageBox.warning(self, translate("DuplicatesController", "Deletion errors"),
                                translate("DuplicatesController",
                                          "Could not delete:")
                                + "\n" + "\n".join(errors))
        elif deleted:
            n_deleted = len(deleted)
            QMessageBox.information(
                self, translate("DuplicatesController", "Deletion finished"),
                translate("DuplicatesController", "%n file(s) deleted.",
                          None, n_deleted),
            )

    @Slot(object)
    def _on_duplicate_badge_clicked(self, photo: PhotoInfo) -> None:
        if photo.duplicate_group_id is None:
            return
        duplicates = self._catalog.get_duplicates_for_group(photo.duplicate_group_id)
        others = [p for p in duplicates if p.path != photo.path]
        if not others:
            return

        if self._duplicates_popup is not None:
            self._duplicates_popup.close()

        dlg = _DuplicatesPopup(photo, others, self)
        dlg.navigate_requested.connect(
            lambda path: self._on_duplicate_popup_navigate(path, duplicates)
        )
        self._duplicates_popup = dlg
        dlg.adjustSize()
        center = self.geometry().center()
        dlg.move(center.x() - dlg.width() // 2, center.y() - dlg.height() // 2)
        dlg.show()

    def _on_duplicate_popup_navigate(self, path: str, group_photos: list) -> None:
        """Clic sur un exemplaire dans la popup de doublons. Si la visionneuse
        est déjà affichée, on y reste et on change simplement la photo montrée
        (comparaison rapide, même principe que _on_duplicate_group_view_requested) ;
        sinon on retombe sur la navigation classique dans la grille."""
        if self._stack.currentIndex() == 1:
            idx = next((i for i, p in enumerate(group_photos) if p.path == path), 0)
            self._current_photos = group_photos
            self._current_photo_index = idx
            self._current_album_id = None
            self.show_viewer(group_photos[idx])
        else:
            self._navigate_to_photo_path(path)

    def _on_duplicate_group_view_requested(self, group_id: int) -> None:
        """Double-clic sur une carte de DuplicateGrid : comparaison rapide dans la visionneuse."""
        photos = self._catalog.get_duplicates_for_group(group_id)
        if not photos:
            return
        self._current_photos = photos
        self._current_photo_index = 0
        self._current_album_id = None
        self._viewer_back_target = "duplicate_grid"
        self.show_viewer(photos[0])

    def _on_duplicate_group_ignored(self, group_id: int) -> None:
        """Bouton ✗ sur une carte de DuplicateGrid : dissout le groupe entier,
        persistant (cf. Catalog.ignore_duplicate_group). Piège corrigé ici :
        si un DuplicateDetectorThread tourne déjà, ce groupe peut être fusionné
        dans son group_of *en mémoire* depuis avant ce clic — son prochain
        instantané (partial_results, cadencé toutes les _LIVE_SNAPSHOT_INTERVAL
        secondes, ou finished) réécrirait alors bêtement ce même groupe en
        base via _apply_duplicate_results, le faisant réapparaître quelques
        secondes après sa dissolution. On mémorise donc les chemins concernés
        dans _duplicate_ignored_paths (vidé à chaque nouveau lancement dans
        _start_duplicate_detection) pour que _apply_duplicate_results les
        exclue de tout instantané du passage en cours."""
        ignored_paths = {p.path for p in self._catalog.get_duplicates_for_group(group_id)}
        self._duplicate_ignored_paths |= ignored_paths
        self._catalog.ignore_duplicate_group(group_id)
        self._duplicate_grid.remove_group(group_id)
        self._sidebar.update_duplicates_badge(self._catalog.count_duplicate_groups())
        for p in self._current_photos:
            if p.duplicate_group_id == group_id:
                p.duplicate_group_id = None
        grid_assignments = {p.path: p.duplicate_group_id for p in self._current_photos
                            if p.duplicate_group_id is None}
        if grid_assignments:
            self._grid.refresh_duplicate_status(grid_assignments)
        cp = self._viewer.current_photo()
        if cp and cp.duplicate_group_id == group_id:
            cp.duplicate_group_id = None
            self._viewer._update_dup_badge()

    def show_duplicate_grid(self) -> None:
        self._duplicate_grid.ensure_loaded()
        self._stack.setCurrentIndex(4)
        self._left_stack.setCurrentIndex(0)
        self._lbl_thumb_size.hide()
        self._thumb_slider.hide()
        self._lbl_zoom.hide()
        self._zoom_slider.hide()
        self._zoom_pct_label.hide()
        self._btn_grid_status.hide()
        self._act_faces_toggle.setVisible(False)
        self._act_exif_toggle.setVisible(False)
        self._btn_annotations_toggle.setVisible(False)
        self._lbl_fileinfo.setText("")
        self._lbl_action.setText("")

