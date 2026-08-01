# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Contrôleur visages & personnes (extrait de MainWindow)."""
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





# Sentinelle de contexte « vue personne » (préfixe des context_keys de grille)
_PERSON_CTX_PREFIX = "__person__"


class FacesController:
    """Contrôleur visages & personnes (extrait de MainWindow).

    Mixin de MainWindow — aucune instanciation autonome : les attributs
    (self._catalog, self._sidebar, …) sont créés par MainWindow.__init__."""

    def _open_index_errors_dialog(self) -> None:
        if self._index_errors_dialog is not None:
            self._index_errors_dialog.raise_()
            self._index_errors_dialog.activateWindow()
            return
        from src.ui.index_errors_dialog import IndexErrorsDialog
        dlg = IndexErrorsDialog(self._face_db, self._thumb_cache, self)
        dlg.retry_requested.connect(self._on_index_error_dialog_retry)
        dlg.finished.connect(self._on_index_errors_dialog_closed)
        self._index_errors_dialog = dlg
        dlg.show()

    def _on_index_errors_dialog_closed(self) -> None:
        self._index_errors_dialog = None

    def _on_index_error_dialog_retry(self, photo_path: str) -> None:
        photo = next((p for p in self._current_photos if p.path == photo_path), None)
        if photo is None:
            photo = PhotoInfo(path=photo_path, filename=os.path.basename(photo_path))
        self._on_retry_face_index_requested(photo)

    def _maybe_prompt_picasa_for_new_folder(self, folder: str) -> None:
        """Propose l'import Picasa scopé si le nouveau dossier contient des .picasa.ini."""
        from src.faces.picasa_importer import scan, PicasaImportThread

        n_contacts, n_photos, n_edits = scan([folder])
        if n_photos == 0 and n_edits == 0:
            return

        parts = []
        if n_photos:
            parts.append(f"{n_photos} photo(s) avec des visages identifiés")
        if n_edits:
            parts.append(f"{n_edits} photo(s) avec des retouches")
        details = " et ".join(parts)

        reply = QMessageBox.question(
            self, "Données Picasa détectées",
            f"Le dossier ajouté contient des données Picasa :\n{details}.\n\n"
            "Voulez-vous les importer pour ce dossier ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._folder_picasa_thread = PicasaImportThread(
            self._catalog, self._face_db, [folder], self._edit_db, self,
        )
        self._folder_picasa_thread.finished.connect(self._on_folder_picasa_import_finished)
        self._lbl_action.setText("Import Picasa du nouveau dossier en cours…")
        self._folder_picasa_thread.start()

    def _on_folder_picasa_import_finished(self, result) -> None:
        self._lbl_action.setText("")
        if result.edited_map:
            self._on_picasa_edits_imported(result.edited_map)
        parts = [
            f"{result.persons_created} personne(s) créée(s)",
            f"{result.faces_imported} annotation(s) de visage dans {result.photos_processed} photo(s)",
        ]
        if result.edits_imported:
            parts.append(f"{result.edits_imported} retouche(s) importée(s)")
        QMessageBox.information(
            self, "Import Picasa terminé", ", ".join(parts) + ".",
        )

    # ------------------------------------------------------------------ faces

    def _reset_and_reindex_faces(self) -> None:
        dlg = _ResetFacesDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        choice = dlg.choice

        # ── Arrêter proprement les threads en cours ──────────────────────────
        threads_to_wait: list[QThread] = []

        if self._face_indexer and self._face_indexer.isRunning():
            try:
                self._face_indexer.cluster_requested.disconnect(self._run_clustering)
            except RuntimeError:
                pass
            self._face_indexer.stop()
            threads_to_wait.append(self._face_indexer)

        if self._cluster_thread and self._cluster_thread.isRunning():
            threads_to_wait.append(self._cluster_thread)

        # ── Mise à jour UI immédiate ─────────────────────────────────────────
        msg = "Arrêt des analyses en cours…" if threads_to_wait else "Réinitialisation en cours…"
        self._lbl_action.setText(msg)

        # ── Worker hors UI : attend les threads + reset DB ───────────────────
        self._reset_worker = _ResetWorkerThread(
            self._face_db, choice, threads_to_wait, self
        )
        self._reset_worker.done.connect(self._on_reset_done)
        self._reset_worker.finished.connect(self._reset_worker.deleteLater)
        self._reset_worker.start()

    @Slot(int)
    def _on_reset_done(self, choice: int) -> None:
        self._face_cluster_grid.refresh()
        self._lbl_action.setText("")

        if choice != _ResetFacesDialog.RESET_CLUSTERING:
            # reset_index() a aussi vidé face_index_errors : les erreurs
            # de timeout/crash n'ont plus lieu d'être tant que le nouveau
            # passage d'indexation n'a pas eu lieu.
            self._grid.set_index_error_paths([])

        if choice == _ResetFacesDialog.RESET_CLUSTERING:
            msg = (
                "La réinitialisation des groupes est terminée.\n\n"
                "Le regroupement HDBSCAN va redémarrer."
            )
        else:
            msg = (
                "La réinitialisation complète est terminée.\n\n"
                "L'analyse des visages va redémarrer. Cette opération peut\n"
                "prendre plusieurs heures selon la taille de la bibliothèque."
            )
        QMessageBox.information(self, "Réinitialisation terminée", msg)

        if choice == _ResetFacesDialog.RESET_CLUSTERING:
            self._run_clustering()
        else:
            self._start_face_indexing()

    def _schedule_similarity_search(self) -> None:
        """Programme une recherche de similarité après une identification.

        Nommer ou assigner une personne déplace son centroïde : des groupes
        jusque-là sous le seuil de suggestion peuvent le franchir. C'est le seul
        événement, hors nouvelles photos, qui change le résultat de la recherche
        — sans ce déclencheur, une bibliothèque entièrement indexée ne produisait
        plus jamais de « visage en attente de vérification ».

        Le timer est à un coup et relancé à chaque appel : une série
        d'identifications ne provoque qu'un seul passage, 30 s après la dernière.
        """
        self._similarity_debounce.start()

    def _start_similarity_search_manually(self) -> None:
        """Entrée Visages › Rechercher des visages similaires… — même traitement,
        mais avec un retour explicite si un passage est déjà en cours (l'appelant
        est ici l'utilisateur, pas un enchaînement automatique)."""
        self._similarity_debounce.stop()
        if hasattr(self, "_similarity_thread") and self._similarity_thread.isRunning():
            QMessageBox.information(
                self,
                "Recherche en cours",
                "Une recherche de visages similaires est déjà en cours.\n"
                "Suivez sa progression dans la barre de statut.",
            )
            return
        self._start_similarity_search()

    def _start_similarity_search(self) -> None:
        """Compare les centroïdes des groupes non identifiés aux personnes nommées.

        Déclenché automatiquement à la fin de chaque regroupement, à la fin d'une
        passe d'indexation qui n'a rien trouvé de nouveau (sinon le regroupement
        s'en charge), et après une identification (cf.
        _schedule_similarity_search) — aucune interaction utilisateur, juste un
        message dans la barre de statut à la fin.
        """
        if hasattr(self, "_similarity_thread") and self._similarity_thread.isRunning():
            return

        self._sb_progress_bar.setRange(0, 0)
        self._sb_progress_bar.show()
        self._lbl_action.setText("Recherche de visages similaires…")
        self._similarity_thread = SimilaritySearchThread(self._face_db, self)
        self._similarity_thread.progress.connect(self._on_similarity_progress)
        self._similarity_thread.finished.connect(self._on_similarity_finished)
        self._similarity_thread.start()

    def _on_similarity_progress(self, current: int, total: int) -> None:
        self._sb_progress_bar.setRange(0, max(total, 1))
        self._sb_progress_bar.setValue(current)
        self._lbl_action.setText(f"Recherche similarité… {current} / {total} groupes")

    def _on_similarity_finished(self, made: int, total: int) -> None:
        self._sb_progress_bar.hide()
        self._sb_progress_bar.setValue(0)
        self._lbl_action.setText("")
        self.statusBar().showMessage(
            f"Recherche terminée : {made} suggestion(s) créée(s) sur {total} groupe(s) vérifiés.",
            8000,
        )
        if made > 0 and self._stack.currentWidget() is self._person_cluster_view:
            self._person_cluster_view.refresh()

    def _start_face_indexing(self) -> None:
        if self._face_indexer and self._face_indexer.isRunning():
            # Un scan (ex. re-scan forcé) vient d'ajouter/modifier des photos
            # pendant qu'une indexation précédente tourne encore : sans cette
            # garde, la demande est perdue silencieusement — aucun mécanisme
            # ne relance l'indexation pour les nouvelles photos, contrairement
            # au cas symétrique du warmup TF (_on_scan_finished/_on_warmup_done).
            # _on_face_indexing_finished relance dès que le run en cours se termine.
            self._face_index_pending = True
            return
        if self._face_indexer is not None:
            self._face_indexer.deleteLater()
        self._face_indexer = FaceIndexThread(self._face_db, self._catalog, self)
        self._face_indexer.progress.connect(self._on_face_progress)
        self._face_indexer.cluster_requested.connect(self._run_clustering)
        self._face_indexer.finished.connect(self._on_face_indexing_finished)
        self._face_indexer.unavailable.connect(self._on_face_unavailable)
        self._face_indexer.error.connect(self._on_face_index_error)
        self._face_indexer.start()

    def _import_from_picasa(self) -> None:
        from src.ui.picasa_import_dialog import PicasaImportDialog

        dlg = QMessageBox(self)
        dlg.setWindowTitle("Importer depuis Picasa")
        dlg.setIcon(QMessageBox.Icon.Information)
        dlg.setText("<b>Import des annotations de visages depuis Google Picasa</b>")
        dlg.setInformativeText(
            "<b>Ce que cette option fait :</b><br>"
            "• Parcourt les fichiers <code>.picasa.ini</code> de vos dossiers photos.<br>"
            "• Importe les noms et régions de visages annotés dans Picasa.<br>"
            "• Crée ou enrichit les personnes correspondantes dans PixelPhotoManager.<br><br>"
            "<b>Limitations et précautions :</b><br>"
            "• <b>À ne faire qu'une seule fois</b> — l'option sera grisée une fois "
            "l'import terminé.<br>"
            "• N'écrase pas les associations que vous avez faites manuellement.<br>"
            "• Les visages Picasa non appariés à une détection InsightFace créent "
            "des entrées sans embedding (non utilisables pour le clustering)."
        )
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.Ok)
        if dlg.exec() != QMessageBox.StandardButton.Ok:
            return

        dlg = PicasaImportDialog(
            self._config, self._catalog, self._face_db, self._edit_db, self,
            on_edits_imported=self._on_picasa_edits_imported,
        )
        dlg.exec()
        self._act_picasa.setEnabled(not self._config.get("picasa.import_done", False))

    def _backup_faces(self) -> None:
        """Crée immédiatement une sauvegarde et affiche le résultat."""
        from src.ui.face_backup_dialog import _BackupThread
        from src.core.app_dirs import APP_DATA_DIR
        from pathlib import Path
        from PySide6.QtWidgets import QMessageBox

        if hasattr(self, "_face_backup_thread") and self._face_backup_thread.isRunning():
            return
        self._face_backup_thread = _BackupThread(
            Path(self._face_db._db_path),
            Path(self._catalog._db_path),
            APP_DATA_DIR,
            self,
        )

        def _on_done(path):
            self._lbl_action.setText("")
            from src.ui.face_backup_dialog import _parse_ts
            QMessageBox.information(
                self, "Sauvegarde créée",
                f"Sauvegarde enregistrée :\n{_parse_ts(path)}\n\n"
                f"({path.name})",
            )

        def _on_err(msg):
            self._lbl_action.setText("")
            QMessageBox.critical(self, "Erreur de sauvegarde", msg)

        self._face_backup_thread.succeeded.connect(_on_done)
        self._face_backup_thread.failed.connect(_on_err)
        self._face_backup_thread.finished.connect(self._face_backup_thread.deleteLater)
        self._lbl_action.setText("Sauvegarde de la reconnaissance en cours…")
        self._face_backup_thread.start()

    def _manage_face_backups(self) -> None:
        """Ouvre le dialogue de gestion des sauvegardes de reconnaissance."""
        from src.core.app_dirs import APP_DATA_DIR
        from pathlib import Path
        dlg = FaceBackupDialog(
            APP_DATA_DIR,
            Path(self._face_db._db_path),
            Path(self._catalog._db_path),
            self,
        )
        dlg.restore_completed.connect(self._on_face_restore_completed)
        dlg.exec()

    @Slot()
    def _on_face_restore_completed(self) -> None:
        """Rafraîchit toute l'UI de reconnaissance après une restauration."""
        self._refresh_persons()
        if self._face_panel.isVisible():
            self._face_panel.refresh()

    def _on_picasa_edits_imported(self, edited_map: dict) -> None:
        for path, edit_info in edited_map.items():
            self._grid.refresh_photo(path, edit_info)

    def _show_face_counters(self) -> None:
        from src.ui.face_counters_dialog import FaceCountersDialog
        dlg = FaceCountersDialog(self._face_db, self._catalog, self)
        dlg.exec()

    @Slot(int, int)
    def _on_face_progress(self, current: int, total: int) -> None:
        if current == 0:
            self._lbl_action.setText("Initialisation de l'analyse des visages…")
        else:
            self._lbl_action.setText(f"Analyse visages… {current}/{total}")

    @Slot(int, int)
    def _on_face_indexing_finished(self, indexed: int, faces: int) -> None:
        self._lbl_action.setText("")
        if faces > 0:
            self._run_clustering()          # enchaîne lui-même sur la similarité
        else:
            # Rien de nouveau à regrouper, mais les personnes nommées depuis le
            # dernier passage ont pu rendre des groupes existants proposables :
            # sur une bibliothèque déjà entièrement indexée, c'est le seul point
            # d'entrée automatique qui reste.
            self._schedule_similarity_search()
        if self._face_index_pending:
            self._face_index_pending = False
            self._start_face_indexing()

    def _on_face_index_error(self, path: str, msg: str) -> None:
        """Timeout/crash pendant l'analyse automatique : la photo est déjà
        enregistrée dans face_index_errors (FaceIndexThread.mark_index_error)."""
        logger.warning("Visage non indexé %s: %s", path, msg)
        self._grid.set_index_error_paths(self._face_db.get_error_paths())
        if self._index_errors_dialog is not None:
            self._index_errors_dialog.refresh()

    def _start_clustering_with_confirm(self) -> None:
        """Affiche une explication du clustering, puis le lance si l'utilisateur confirme."""
        if self._cluster_thread and self._cluster_thread.isRunning():
            QMessageBox.information(
                self,
                "Regroupement en cours",
                "Un regroupement des visages est déjà en cours.\n"
                "Suivez sa progression dans la barre de statut.",
            )
            return

        n_total = self._face_db.count_embeddings()
        n_identified = self._face_db.count_identified_faces()
        n_unidentified = n_total - n_identified

        dlg = QMessageBox(self)
        dlg.setWindowTitle("Regrouper les visages")
        dlg.setIcon(QMessageBox.Icon.Information)
        dlg.setText("<b>Regroupement automatique des visages (clustering)</b>")
        dlg.setInformativeText(
            "Cette opération analyse les visages non encore identifiés et les regroupe "
            "automatiquement par similarité (algorithme HDBSCAN sur vecteurs ArcFace).<br><br>"
            f"<b>{n_unidentified:,}</b> visages non identifiés seront traités "
            f"({n_identified:,} visages déjà identifiés sont conservés intacts).<br><br>"
            "Les groupes obtenus apparaîtront dans <i>Identifier les personnes…</i> "
            "pour que vous puissiez nommer chaque groupe.<br><br>"
            "<b>Durée estimée : 15 à 30 minutes.</b> "
            "La progression s'affiche dans la barre de statut en bas de la fenêtre."
        )
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.Ok)
        dlg.button(QMessageBox.StandardButton.Ok).setText("Démarrer")
        dlg.button(QMessageBox.StandardButton.Cancel).setText("Annuler")
        if dlg.exec() != QMessageBox.StandardButton.Ok:
            return

        self._run_clustering()

    def _run_clustering(self) -> None:
        """Lance le clustering dans un thread séparé pour ne pas bloquer l'UI."""
        if self._cluster_thread and self._cluster_thread.isRunning():
            return   # un clustering est déjà en cours
        if self._cluster_thread is not None:
            self._cluster_thread.deleteLater()
        self._cluster_thread = ClusterThread(self._face_db, self)
        self._cluster_thread.progress.connect(self._lbl_action.setText)
        self._cluster_thread.finished.connect(self._on_clustering_finished)
        self._cluster_thread.error.connect(
            lambda msg: logger.warning("Clustering: %s", msg)
        )
        self._act_cluster_faces.setEnabled(False)
        self._act_cluster_faces.setText("Regroupement en cours…")
        self._cluster_start_time = time.monotonic()
        self._cluster_thread.start()

    @Slot(int)
    def _on_clustering_finished(self, n_clusters: int) -> None:
        self._cluster_start_time = None
        self._act_cluster_faces.setText("Regrouper les visages…")
        self._act_cluster_faces.setEnabled(True)
        self._refresh_persons()
        self._face_cluster_grid.refresh()
        if self._face_panel.isVisible():
            self._face_panel_refresh_timer.start()
        self._lbl_action.setText("")
        # Systématique, y compris quand n_clusters == 0 : le regroupement rend la
        # main sans rien faire dès que le nombre de visages non identifiés n'a pas
        # bougé (clusterer._run_clustering), alors que les groupes déjà formés,
        # eux, restent à comparer aux personnes nommées entre-temps.
        self._start_similarity_search()

    @Slot()
    def _on_face_unavailable(self) -> None:
        self._lbl_action.setText("")
        QMessageBox.information(
            self,
            "Reconnaissance faciale indisponible",
            "Le module insightface n'est pas installé.\n\n"
            "pip install insightface onnxruntime",
        )

    def _open_people_dialog(self) -> None:
        dlg = PeopleDialog(self._face_db, self._catalog, self)
        dlg.cluster_named.connect(self._on_cluster_named)
        dlg.cluster_assigned.connect(self._on_cluster_assigned)
        dlg.exec()

    def _refresh_face_panel_if_visible(self) -> None:
        if self._face_panel.isVisible():
            self._face_panel.refresh()

    @Slot(int, str)
    def _on_cluster_named(self, cluster_id: int, name: str) -> None:
        person = self._catalog.create_person(name)
        self._face_db.assign_person_to_cluster(cluster_id, person.id)
        self._refresh_persons()
        self._face_cluster_grid.remove_clusters([cluster_id])
        self._refresh_face_panel_if_visible()
        self._schedule_similarity_search()

    @Slot(int, int)
    def _on_cluster_assigned(self, cluster_id: int, person_id: int) -> None:
        self._face_db.assign_person_to_cluster(cluster_id, person_id)
        self._update_persons_counts()
        self._face_cluster_grid.remove_clusters([cluster_id])
        self._refresh_face_panel_if_visible()
        self._schedule_similarity_search()

    @Slot(list, str)
    def _on_clusters_named(self, cluster_ids: list, name: str) -> None:
        person = self._catalog.create_person(name)
        for cid in cluster_ids:
            self._face_db.assign_person_to_cluster(cid, person.id)
        self._refresh_persons()  # nouvelle personne créée → rebuild complet
        self._face_cluster_grid.remove_clusters(cluster_ids)
        self._refresh_face_panel_if_visible()
        self._schedule_similarity_search()

    @Slot(list, int)
    def _on_clusters_assigned(self, cluster_ids: list, person_id: int) -> None:
        for cid in cluster_ids:
            self._face_db.assign_person_to_cluster(cid, person_id)
        self._update_persons_counts()
        self._face_cluster_grid.remove_clusters(cluster_ids)
        self._refresh_face_panel_if_visible()
        self._schedule_similarity_search()

    @Slot(int)
    def _on_cluster_ignored(self, _cluster_id: int) -> None:
        self._face_cluster_grid.remove_clusters([_cluster_id])

    @Slot(list)
    def _on_clusters_ignored(self, cluster_ids: list) -> None:
        self._face_cluster_grid.remove_clusters(cluster_ids)

    @Slot(int, int)
    def _on_cluster_merged(self, _source: int, _target: int) -> None:
        self._face_cluster_grid.refresh()

    @Slot(int, str)
    def _on_cluster_photos_requested(self, cluster_id: int, label: str) -> None:
        """Clic simple sur un groupe : afficher ses photos dans la grille."""
        self._grid.set_ribbon_mode(False)
        self._grid.set_date_overlay_visible(False)
        self._start_photo_query(
            lambda: self._catalog.get_photos_by_paths(
                self._face_db.get_photos_for_cluster(cluster_id)
            ),
            f"{_PERSON_CTX_PREFIX}cluster_{cluster_id}",
        )
        self.show_grid()
        self._lbl_grid_nav.setText(label)
        self._grid_nav_bar.show()

    @Slot(object)
    def _on_person_merge_requested(self, source: PersonInfo) -> None:
        # enrich_persons lance une CTE sur toutes les faces nommées — peut durer
        # plusieurs secondes sur une grande base. On la déporte dans un thread.
        t = _PersonsRefreshThread(self._catalog, self._face_db, self)
        t.result_ready.connect(lambda persons, _: self._show_merge_dialog(source, persons))
        t.finished.connect(t.deleteLater)
        t.start()

    def _show_merge_dialog(self, source: PersonInfo, persons: list) -> None:
        dlg = MergePersonsDialog(source, persons, self)
        if dlg.exec() != QDialog.Accepted:
            return
        target_id = dlg.target_person_id()
        if target_id is None:
            return
        self._face_db.merge_persons(keep_id=target_id, remove_id=source.id)
        self._catalog.delete_person(source.id)
        if self._current_context == f"{_PERSON_CTX_PREFIX}{source.id}":
            paths = self._face_db.get_photos_for_person(target_id)
            photos = self._catalog.get_photos_by_paths(paths)
            self._current_photos = photos
            self._current_context = f"{_PERSON_CTX_PREFIX}{target_id}"
            self._grid.set_photos(photos)
            self._update_status()
        new_count = self._face_db.get_person_photo_count(target_id)
        self._sidebar.apply_person_merge(source.id, target_id, new_count)

    @Slot(object)
    def _on_person_rename_requested(self, person: PersonInfo) -> None:
        name, ok = QInputDialog.getText(
            self, "Renommer la personne", "Nouveau nom :", text=person.name
        )
        if ok and name.strip() and name.strip() != person.name:
            self._catalog.rename_person(person.id, name.strip())
            self._refresh_persons()

    @Slot(object)
    def _on_person_clear_requested(self, person: PersonInfo) -> None:
        """Supprime le nom d'une personne : désassocie toutes ses faces et efface l'entrée."""
        reply = QMessageBox.question(
            self,
            "Effacer le nom",
            f"Effacer « {person.name} » et supprimer toutes ses associations de visages ?\n\n"
            "Les visages retourneront dans leurs groupes anonymes.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        self._face_db.unassign_person(person.id)
        self._catalog.delete_person(person.id)
        if self._current_context == f"{_PERSON_CTX_PREFIX}{person.id}":
            self.show_grid()
        self._sidebar.remove_person(person.id)

    @Slot(object)
    def _on_person_selected(self, person: PersonInfo) -> None:
        self._grid_nav_bar.hide()
        self.show_person_clusters(person)

    @Slot(int, object)
    def _on_cover_face_set(self, person_id: int, face) -> None:
        self._sidebar.update_person_icon(person_id, face)

    def _refresh_persons(self) -> None:
        """Rebuild complet de la liste (personnes ajoutées/supprimées/renommées)."""
        if self._persons_refresh_thread and self._persons_refresh_thread.isRunning():
            return
        if self._persons_refresh_thread is not None:
            self._persons_refresh_thread.deleteLater()
        self._persons_refresh_thread = _PersonsRefreshThread(self._catalog, self._face_db, self)
        self._persons_refresh_thread.result_ready.connect(self._on_persons_refreshed)
        self._persons_refresh_thread.start()

    def _update_persons_counts(self) -> None:
        """Mise à jour légère : seuls les compteurs/couvertures modifiés sont rafraîchis."""
        if self._persons_refresh_thread and self._persons_refresh_thread.isRunning():
            return
        if self._persons_refresh_thread is not None:
            self._persons_refresh_thread.deleteLater()
        self._persons_refresh_thread = _PersonsRefreshThread(self._catalog, self._face_db, self)
        self._persons_refresh_thread.result_ready.connect(self._on_persons_counts_updated)
        self._persons_refresh_thread.start()

    @Slot(list, int)
    def _on_persons_refreshed(self, persons: list, count: int) -> None:
        self._sidebar.refresh_persons(persons)
        self._sidebar.update_cluster_badge(count)

        pending_id = self._pending_person_view_id
        if pending_id is not None and self._current_context == "Toutes les photos":
            self._pending_person_view_id = None
            person = next((p for p in persons if p.id == pending_id), None)
            if person:
                self._grid_nav_bar.hide()
                self.show_person_clusters(person)

    @Slot(list, int)
    def _on_persons_counts_updated(self, persons: list, count: int) -> None:
        self._sidebar.update_persons_data(persons)
        self._sidebar.update_cluster_badge(count)

    def _on_face_highlighted(self, face) -> None:
        self._viewer.highlight_face(face)

    def _on_all_faces_toggled(self, faces: list) -> None:
        self._viewer.set_all_highlighted_faces(faces)

    def _on_face_context_menu(self, face, gpos) -> None:
        self._face_panel.show_face_context_menu(face, gpos)

    @Slot(bool)
    def _on_add_face_mode_requested(self, enter: bool) -> None:
        """Bouton 'Ajouter une personne' du FacePanel — bascule le mode dessin
        de bboxe dans la visionneuse."""
        if enter:
            self._viewer.enter_face_add_mode()
        else:
            self._viewer.cancel_face_add_mode()

    @Slot(bool)
    def _on_face_panel_person_cluster_requested(self, person_id: int) -> None:
        """Double-clic sur un visage nommé dans le panneau → vue clusters de la personne."""
        person = self._catalog.get_person(person_id)
        if person is None:
            return
        self._face_db.enrich_persons([person])
        self.show_person_clusters(person)

    def show_face_clusters(self) -> None:
        self._face_cluster_grid.restore()
        self._stack.setCurrentIndex(2)
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

    def show_person_clusters(self, person: PersonInfo) -> None:
        """Affiche les groupes de visages d'une personne au lieu de ses photos."""
        self._person_cluster_view.set_person(person)
        self._stack.setCurrentIndex(3)
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

    def _on_person_cluster_photos_requested(self, cluster_id: int, label: str) -> None:
        """Double-clic sur une carte de groupe depuis PersonClusterView."""
        self._from_person_cluster_view = True
        self._on_cluster_photos_requested(cluster_id, label)

    def _on_person_cluster_photo_requested(self, path: str) -> None:
        """Double-clic sur une vignette en mode dégroupé → ouvrir la photo dans la visionneuse."""
        photo = self._catalog.get_photo_by_path(path)
        if photo is None:
            return
        # Charger toutes les photos de la personne pour permettre la navigation prev/next
        person = self._person_cluster_view.current_person
        if person:
            all_paths = self._face_db.get_photos_for_person(person.id)
            photos = self._catalog.get_photos_by_paths(all_paths)
        else:
            photos = [photo]
        self._current_photos = photos if photos else [photo]
        self._current_photo_index = next(
            (i for i, p in enumerate(self._current_photos) if p.path == path), 0
        )
        self._current_album_id = None
        self._viewer_back_target = "person_cluster_view"
        self.show_viewer(photo)

    def _on_person_cluster_back(self) -> None:
        """Bouton ← Retour dans PersonClusterView → retour à la grille principale."""
        self._grid_nav_bar.hide()
        self.show_grid()

    @Slot(int)
    def _on_pcv_cluster_unassigned(self, _cluster_id: int) -> None:
        """Groupe dé-associé depuis PersonClusterView (DB déjà à jour) → rafraîchir la sidebar."""
        self._update_persons_counts()
        self._refresh_face_panel_if_visible()

    @Slot(int)
    def _on_suggestion_accepted(self, cluster_id: int) -> None:
        """Suggestion confirmée : déplace les vignettes sans recharger toute la grille."""
        self._face_db.accept_cluster_suggestion(cluster_id)
        self._update_persons_counts()
        self._refresh_face_panel_if_visible()
        self._person_cluster_view.accept_pending_cluster(cluster_id)

    @Slot(int)
    def _on_suggestion_rejected(self, cluster_id: int) -> None:
        """Suggestion refusée : retire la vignette et recalcule la suggestion suivante."""
        person = self._person_cluster_view.current_person
        exclude_pid = person.id if person else None
        # UI immédiat
        self._person_cluster_view.remove_pending_cluster(cluster_id)
        # Vide la suggestion et recalcule la meilleure personne restante en arrière-plan
        t = _ResuggestThread(self._face_db, [cluster_id], exclude_pid, self)
        t.finished.connect(t.deleteLater)
        t.start()

    @Slot(list)
    def _on_all_suggestions_accepted(self, cluster_ids: list) -> None:
        """Toutes les suggestions confirmées d'un coup."""
        for cid in cluster_ids:
            self._face_db.accept_cluster_suggestion(cid)
        self._update_persons_counts()
        self._refresh_face_panel_if_visible()
        for cid in cluster_ids:
            self._person_cluster_view.accept_pending_cluster(cid)

    @Slot(list)
    def _on_all_suggestions_rejected(self, cluster_ids: list) -> None:
        """Toutes les suggestions refusées d'un coup."""
        person = self._person_cluster_view.current_person
        exclude_pid = person.id if person else None
        # UI immédiat
        self._person_cluster_view.clear_all_pending()
        # Recalcule les suggestions pour toutes les autres personnes en arrière-plan
        t = _ResuggestThread(self._face_db, list(cluster_ids), exclude_pid, self)
        t.finished.connect(t.deleteLater)
        t.start()

    def _on_single_reindex_finished(self, photo_path: str, _face_count: int) -> None:
        if self._face_panel.isVisible():
            self._face_panel.set_photo(photo_path)
        self._drain_pending_reindex()

    def _on_retry_face_index_requested(self, photo: PhotoInfo) -> None:
        """Menu contextuel "Retenter l'identification des visages" sur un fichier
        précédemment en erreur (timeout/crash)."""
        if self._retry_face_thread and self._retry_face_thread.isRunning():
            QMessageBox.information(
                self, "Tentative en cours",
                "Une autre tentative d'identification est déjà en cours.",
            )
            return
        if self._retry_face_thread is not None:
            self._retry_face_thread.deleteLater()
        self._retry_face_thread = RetryFaceIndexThread(self._face_db, photo.path, self)
        self._retry_face_thread.finished.connect(self._on_retry_face_index_finished)
        self._retry_face_thread.cluster_requested.connect(self._run_clustering)
        self._lbl_action.setText(f"Nouvelle tentative d'identification : {photo.filename}…")
        self._retry_face_thread.start()

    def _on_retry_face_index_finished(self, photo_path: str, success: bool, face_count: int) -> None:
        self._lbl_action.setText("")
        filename = os.path.basename(photo_path)

        if success:
            self._grid.set_index_error_paths(self._face_db.get_error_paths())
            if self._index_errors_dialog is not None:
                self._index_errors_dialog.refresh()
            if self._face_panel.isVisible():
                self._face_panel.set_photo(photo_path)
            QMessageBox.information(
                self, "Identification réussie",
                f"« {filename} » : {face_count} visage(s) détecté(s).",
            )
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Échec de l'identification")
        box.setText(
            f"L'identification des visages a de nouveau échoué pour « {filename} »."
        )
        box.setInformativeText(
            "Voulez-vous supprimer ce fichier, ou l'exclure définitivement du scan "
            "et de la reconnaissance faciale (il restera dans la photothèque) ?"
        )
        btn_delete  = box.addButton("Supprimer le fichier…", QMessageBox.DestructiveRole)
        btn_exclude = box.addButton("Exclure définitivement", QMessageBox.ActionRole)
        box.addButton("Laisser en erreur", QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is btn_delete:
            photo = next((p for p in self._current_photos if p.path == photo_path), None)
            if photo is None:
                photo = PhotoInfo(path=photo_path, filename=filename)
            self._on_delete_requested([photo])
        elif clicked is btn_exclude:
            self._face_db.set_index_excluded(photo_path, True)
            self._grid.set_index_error_paths(self._face_db.get_error_paths())

        if self._index_errors_dialog is not None:
            self._index_errors_dialog.refresh()

    def _on_force_redetect_requested(self, photo: PhotoInfo) -> None:
        """Menu contextuel de la visionneuse "Forcer une nouvelle détection sans
        limite de taille" : re-détecte les visages de la photo affichée sans le
        filtrage souple par taille (aucune face ne ressort ignored=1), en
        conservant les identifications déjà faites sur cette photo."""
        from src.faces.detector import is_available
        if not is_available():
            return
        if self._force_redetect_thread and self._force_redetect_thread.isRunning():
            QMessageBox.information(
                self, "Détection en cours",
                "Une nouvelle détection est déjà en cours sur cette photo.",
            )
            return
        if self._force_redetect_thread is not None:
            self._force_redetect_thread.deleteLater()
        self._force_redetect_thread = ForceRedetectThread(
            self._face_db, photo.path, self, edit_db=self._edit_db,
        )
        self._force_redetect_thread.finished.connect(self._on_force_redetect_finished)
        self._force_redetect_thread.cluster_requested.connect(self._run_clustering)
        self._lbl_action.setText(f"Nouvelle détection sans limite de taille : {photo.filename}…")
        self._force_redetect_thread.start()

    def _on_force_redetect_finished(self, photo_path: str, face_count: int) -> None:
        self._lbl_action.setText("")
        if self._face_panel.isVisible():
            self._face_panel.set_photo(photo_path)
        QMessageBox.information(
            self, "Détection terminée",
            f"« {os.path.basename(photo_path)} » : {face_count} visage(s) détecté(s), "
            "aucun ignoré par taille.",
        )

