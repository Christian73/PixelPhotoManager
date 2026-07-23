# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import ctypes
import logging
import os
import shutil
import subprocess
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
    QMessageBox, QProgressBar, QSizePolicy, QMenu,
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
from src.library.fs_utils import find_dvd_video_ts
from src.core.app_version import get_app_version
from src.core.update_checker import UpdateCheckThread, STATUS_UPDATE_AVAILABLE
from src.faces.face_database import FaceDatabase
from src.faces.face_indexer import FaceIndexThread, SingleFaceReindexThread, RetryFaceIndexThread, ForceRedetectThread, TFWarmUpThread, SimilaritySearchThread
from src.faces.clusterer import ClusterThread
from src.processing.edit_database import EditDatabase
from src.ui.sidebar import (
    Sidebar, _SPECIAL_ALL, _SPECIAL_FAV, _SPECIAL_VIDEOS, _SPECIAL_RATED,
    _SPECIAL_FILENAME, _SPECIAL_TAG,
)
from src.ui.thumbnail_grid import ThumbnailGrid
from src.ui.photo_viewer import PhotoViewer
from src.ui.edit_panel import EditPanel, MarkedSlider
from src.ui.face_cluster_grid import FaceClusterGrid
from src.ui.person_cluster_view import PersonClusterView
from src.ui.duplicate_grid import DuplicateGrid
from src.ui.face_panel import FacePanel
from src.ui.exif_panel import ExifPanel
from src.ui.people_panel import MergePersonsDialog, PeopleDialog
from src.ui.tag_dialog import TagEditDialog, TagsPrepLoader
from src.ui.advanced_search_dialog import AdvancedSearchDialog, AdvancedSearchPrepLoader
from src.ui.settings_dialog import SettingsDialog
from src.ui.display_order_dialog import DisplayOrderDialog
from src.ui.face_backup_dialog import FaceBackupDialog

logger = logging.getLogger(__name__)

_THUMB_SIZES = [110, 180, 250, 350]

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

from src.ui.main_window_faces import _PERSON_CTX_PREFIX  # noqa: E402


def _photo_sort_key(p: "PhotoInfo"):
    """Clé de tri chronologique : date_taken, puis file_mtime en fallback."""
    if p.date_taken:
        return p.date_taken
    if p.file_mtime:
        return datetime.fromtimestamp(p.file_mtime)
    return datetime.min


def _photo_filename_sort_key(p: "PhotoInfo"):
    """Clé de tri alphabétique (nom de fichier, insensible à la casse)."""
    return (p.filename or "").lower()


# Contrôleurs par domaine (2026-07) : méthodes MainWindow déplacées par pans
# entiers — voir les modules pour le périmètre exact de chacun.
from src.ui.main_window_faces import FacesController  # noqa: E402
from src.ui.main_window_duplicates import DuplicatesController  # noqa: E402


class MainWindow(QMainWindow, FacesController, DuplicatesController):
    def __init__(
        self,
        config: Config,
        catalog: Catalog,
        thumb_cache: ThumbnailCache,
        scanner: LibraryScanner,
        face_db: FaceDatabase,
    ):
        super().__init__()
        self._config = config
        self._catalog = catalog
        self._thumb_cache = thumb_cache
        self._scanner = scanner
        self._face_db = face_db
        self._edit_db = EditDatabase()
        self._face_indexer: FaceIndexThread | None = None
        self._reindex_thread: SingleFaceReindexThread | None = None
        self._retry_face_thread: RetryFaceIndexThread | None = None
        self._duplicate_thread: DuplicateDetectorThread | None = None
        self._live_corrupted_paths: list[str] = []
        self._last_duplicate_check: datetime | None = None
        # (courant, total, message) de la passe de détection en cours, ou None
        # si aucune n'est en cours — alimente la barre de progression de
        # "État des doublons…" (cf. _show_duplicate_status_dialog).
        self._dup_progress: "tuple[int, int, str] | None" = None
        # Chemins ignorés via le bouton ✗ pendant le passage de détection en
        # cours — cf. _on_duplicate_group_ignored pour la raison d'être.
        self._duplicate_ignored_paths: set[str] = set()
        self._duplicates_popup: "_DuplicatesPopup | None" = None
        self._index_errors_dialog = None    # IndexErrorsDialog ouverte (ou None)
        self._force_redetect_thread: ForceRedetectThread | None = None
        self._cluster_thread: ClusterThread | None = None
        self._cluster_start_time: float | None = None
        self._warmup_thread = None          # TFWarmUpThread — pré-charge TF au démarrage
        self._reset_worker: _ResetWorkerThread | None = None
        self._slideshow_win = None
        self._face_index_pending: bool = False
        self._photo_query_thread: _PhotoQueryThread | None = None
        self._persons_refresh_thread: _PersonsRefreshThread | None = None
        self._dup_migration_thread: _DupMigrationThread | None = None
        self._delete_thread: _DeleteWorkerThread | None = None
        self._pending_deletes: list = []  # suppressions confirmées en attente (worker déjà occupé)
        self._scan_had_removals: bool = False
        # Garde manuelle (pas Qt.UniqueConnection : voir _on_scan_finished) —
        # une seule connexion du portillon persons_thumbnails_ready par appli.
        self._dup_gate_connected: bool = False
        # False tant que la liste des personnes de la sidebar n'a jamais été
        # peuplée : le premier _on_scan_finished doit toujours déclencher un
        # refresh, même si le scan n'a rien changé — c'est lui qui assure le
        # remplissage initial (aucun autre chemin ne le fait au démarrage).
        self._persons_loaded: bool = False
        self._from_person_cluster_view: bool = False
        self._viewer_back_target: str = "grid"  # "grid" | "person_cluster_view" | "duplicate_grid"
        # Filtre global de session (pas persisté) pour le calque d'annotations
        self._annotations_globally_visible: bool = True

        self._current_photos: list[PhotoInfo] = []
        self._current_paths: set[str] = set()
        self._current_photo_index: int = 0
        self._current_context: str = ""   # dossier ou album actif
        self._current_album_id: int | None = None   # id de l'album actif, sinon None
        self._pending_person_view_id: int | None = None
        self._catalog_loader: _CatalogLoadThread | None = None
        self._update_check_thread: UpdateCheckThread | None = None
        # Debounce du refresh du face panel après clustering (peut être déclenché
        # plusieurs fois par seconde pendant l'indexation) — délai de 3 s.
        self._face_panel_refresh_timer = QTimer()
        self._face_panel_refresh_timer.setSingleShot(True)
        self._face_panel_refresh_timer.setInterval(3000)

        self._setup_window()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()
        self._connect_bus()
        self._connect_scanner()
        self._setup_folder_watcher()

        # Déféré : laisse window.show() s'exécuter avant de charger la bibliothèque.
        QTimer.singleShot(0, self._load_library)
        _sw = self._config.get("ui.sidebar_width", 280)
        QTimer.singleShot(0, lambda: self._splitter.setSizes([_sw, max(1, self._splitter.width() - _sw)]))
        QTimer.singleShot(0, self._restore_splitter_states)
        # Migration des groupes de doublons + comptage pour le badge, en thread
        # (au premier lancement après upgrade elle charge tous les groupes —
        # exécutée en synchrone ici, elle retardait le premier affichage).
        self._dup_migration_thread = _DupMigrationThread(self._catalog, self)
        self._dup_migration_thread.done.connect(self._sidebar.update_duplicates_badge)
        self._dup_migration_thread.start()
        QTimer.singleShot(0, self._start_update_check)

    # ------------------------------------------------------------------ setup

    def _setup_window(self) -> None:
        self.setWindowTitle("PixelPhotoManager")
        self.setMinimumSize(900, 600)
        w = self._config.get("ui.window_width", 1200)
        h = self._config.get("ui.window_height", 800)
        self.resize(w, h)

    def _setup_menu(self) -> None:
        # Barre unifiée : icône | menus | spacer | boutons contextuels | export
        top_bar = QWidget()
        top_bar.setObjectName("top_bar")
        lay = QHBoxLayout(top_bar)
        lay.setContentsMargins(2, 0, 0, 0)
        lay.setSpacing(0)

        # --- Icône application (gauche) ---
        icon_path = Path(__file__).resolve().parent.parent.parent / "assets" / "cubic.png"
        if icon_path.exists():
            pix = QPixmap(str(icon_path)).scaled(
                48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            lbl_icon = QLabel()
            lbl_icon.setPixmap(pix)
            lbl_icon.setFixedSize(54, 54)
            lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_icon.setToolTip("PixelPhotoManager")
            lay.addWidget(lbl_icon)

        # --- Barre de menus ---
        mb = QMenuBar(top_bar)
        mb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        # Fichier
        m_file = mb.addMenu("Fichier")
        act_add = QAction("Ajouter un dossier…", self)
        act_add.triggered.connect(self.open_folder_dialog)
        m_file.addAction(act_add)
        m_file.addSeparator()
        act_advanced_search = QAction("Recherche avancée…", self)
        act_advanced_search.setShortcut(QKeySequence("Ctrl+F"))
        act_advanced_search.triggered.connect(self._open_advanced_search)
        m_file.addAction(act_advanced_search)
        m_file.addSeparator()
        act_quit = QAction("Quitter", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        # Affichage
        m_view = mb.addMenu("Affichage")
        act_sidebar = QAction("Afficher/masquer sidebar", self)
        act_sidebar.setShortcut(Qt.Key_F9)
        act_sidebar.triggered.connect(self.toggle_sidebar)
        m_view.addAction(act_sidebar)
        act_fs = QAction("Plein écran", self)
        act_fs.setShortcut(Qt.Key_F11)
        act_fs.triggered.connect(self._toggle_fullscreen)
        m_view.addAction(act_fs)
        m_view.addSeparator()
        act_slideshow = QAction("Diaporama", self)
        act_slideshow.setShortcut(Qt.Key_F5)
        act_slideshow.triggered.connect(self._start_slideshow)
        m_view.addAction(act_slideshow)
        m_view.addSeparator()
        act_order = QAction("Ordre d'affichage…", self)
        act_order.triggered.connect(self._open_display_order_dialog)
        m_view.addAction(act_order)

        # Outils
        m_tools = mb.addMenu("Outils")
        act_folders = QAction("Dossiers…", self)
        act_folders.setToolTip("Gérer les dossiers surveillés et forcer un re-scan")
        act_folders.triggered.connect(self._open_folder_manager)
        m_tools.addAction(act_folders)
        m_tools.addSeparator()
        act_dup_status = QAction("État des doublons…", self)
        act_dup_status.setToolTip("Afficher l'état actuel de la détection de doublons")
        act_dup_status.triggered.connect(self._show_duplicate_status_dialog)
        m_tools.addAction(act_dup_status)
        m_tools.addSeparator()
        act_corrupted = QAction("Fichiers corrompus…", self)
        act_corrupted.setToolTip(
            "Afficher les fichiers corrompus détectés par l'analyse des doublons"
        )
        act_corrupted.triggered.connect(self._show_corrupted_status_dialog)
        m_tools.addAction(act_corrupted)
        act_deleted_corrupted = QAction("Fichiers corrompus supprimés…", self)
        act_deleted_corrupted.setToolTip(
            "Afficher les fichiers corrompus supprimés définitivement "
            "(pour tenter de les retrouver dans une sauvegarde)"
        )
        act_deleted_corrupted.triggered.connect(self._open_deleted_corrupted_files_dialog)
        m_tools.addAction(act_deleted_corrupted)
        m_tools.addSeparator()
        act_exif_date_sync = QAction("Synchroniser dates de création avec l'EXIF…", self)
        act_exif_date_sync.setToolTip(
            "Remplace la date de création Windows par la date EXIF "
            "pour les fichiers où elles diffèrent"
        )
        act_exif_date_sync.triggered.connect(self._open_exif_date_sync)
        m_tools.addAction(act_exif_date_sync)
        m_tools.addSeparator()
        act_journal = QAction("Journal des threads…", self)
        act_journal.setToolTip("Afficher le journal d'activité des threads de fond")
        act_journal.triggered.connect(self._open_thread_journal)
        m_tools.addAction(act_journal)
        m_tools.addSeparator()
        act_problems = QAction("Historique des problèmes…", self)
        act_problems.setToolTip(
            "Afficher l'historique des fichiers corrompus détectés et réparés"
        )
        act_problems.triggered.connect(self._open_problems_history)
        m_tools.addAction(act_problems)
        m_tools.addSeparator()
        act_ext_apps = QAction("Applications externes…", self)
        act_ext_apps.setToolTip(
            "Configurer les applications tierces disponibles depuis la visionneuse"
        )
        act_ext_apps.triggered.connect(self._open_external_apps_dialog)
        m_tools.addAction(act_ext_apps)
        m_tools.addSeparator()
        act_settings = QAction("Paramètres", self)
        act_settings.triggered.connect(self._open_settings)
        m_tools.addAction(act_settings)

        # Visages
        m_faces = mb.addMenu("Visages")
        self._act_picasa = QAction("Importer depuis Picasa…", self)
        self._act_picasa.setEnabled(not self._config.get("picasa.import_done", False))
        self._act_picasa.triggered.connect(self._import_from_picasa)
        m_faces.addAction(self._act_picasa)
        m_faces.addSeparator()
        act_reindex = QAction("Réinitialiser et réindexer…", self)
        act_reindex.triggered.connect(self._reset_and_reindex_faces)
        m_faces.addAction(act_reindex)
        self._act_cluster_faces = QAction("Regrouper les visages…", self)
        self._act_cluster_faces.triggered.connect(self._start_clustering_with_confirm)
        m_faces.addAction(self._act_cluster_faces)
        m_faces.addSeparator()
        act_index_errors = QAction("Visualisation des erreurs…", self)
        act_index_errors.setToolTip(
            "Afficher les photos dont l'identification des visages a échoué "
            "(timeout/crash) et relancer le traitement fichier par fichier"
        )
        act_index_errors.triggered.connect(self._open_index_errors_dialog)
        m_faces.addAction(act_index_errors)
        m_faces.addSeparator()
        act_backup = QAction("Sauvegarder la reconnaissance…", self)
        act_backup.setToolTip(
            "Crée une sauvegarde de l'état actuel des visages, groupes et personnes"
        )
        act_backup.triggered.connect(self._backup_faces)
        m_faces.addAction(act_backup)
        act_manage_backups = QAction("Gérer les sauvegardes…", self)
        act_manage_backups.setToolTip(
            "Voir, restaurer ou supprimer les sauvegardes de reconnaissance faciale"
        )
        act_manage_backups.triggered.connect(self._manage_face_backups)
        m_faces.addAction(act_manage_backups)
        m_faces.addSeparator()
        act_face_counters = QAction("Compteurs…", self)
        act_face_counters.triggered.connect(self._show_face_counters)
        m_faces.addAction(act_face_counters)

        # Aide
        m_help = mb.addMenu("Aide")
        act_help = QAction("Aide…", self)
        act_help.setShortcut(QKeySequence("F1"))
        act_help.triggered.connect(self._show_help)
        m_help.addAction(act_help)
        m_help.addSeparator()
        act_about = QAction("À propos", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

        lay.addWidget(mb)

        # --- Spacer ---
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(spacer)

        # --- Boutons contextuels (masqués par défaut) ---
        self._btn_faces_toggle = QPushButton("Visages")
        self._btn_faces_toggle.setCheckable(True)
        self._btn_faces_toggle.setToolTip("Afficher / masquer les visages de la photo")
        self._btn_faces_toggle.toggled.connect(self._on_faces_toggle)
        self._btn_faces_toggle.setVisible(False)
        lay.addWidget(self._btn_faces_toggle)
        self._act_faces_toggle = self._btn_faces_toggle

        self._btn_exif_toggle = QPushButton("EXIF")
        self._btn_exif_toggle.setCheckable(True)
        self._btn_exif_toggle.setToolTip("Afficher / masquer les métadonnées EXIF")
        self._btn_exif_toggle.toggled.connect(self._on_exif_toggle)
        self._btn_exif_toggle.setVisible(False)
        lay.addWidget(self._btn_exif_toggle)
        self._act_exif_toggle = self._btn_exif_toggle

        self._btn_annotations_toggle = QPushButton("✏ Annotations")
        self._btn_annotations_toggle.setCheckable(True)
        # setChecked() avant connect() : évite de déclencher _on_annotations_toggle
        # ici, alors que self._viewer n'existe pas encore (_setup_central() pas encore appelé).
        self._btn_annotations_toggle.setChecked(True)   # actif par défaut
        self._btn_annotations_toggle.setToolTip("Afficher / masquer le calque d'annotations (dessin/texte)")
        self._btn_annotations_toggle.toggled.connect(self._on_annotations_toggle)
        self._btn_annotations_toggle.setVisible(False)
        lay.addWidget(self._btn_annotations_toggle)

        # --- Bouton Export ---
        self._btn_export = QPushButton("⬆  Exporter")
        self._btn_export.setToolTip(
            "Exporter la photo en cours (visionneuse) ou les photos sélectionnées (grille)"
        )
        self._btn_export.setStyleSheet(
            "QPushButton { background:#2a5a8a; color:white; border:none;"
            " border-radius:3px; padding:4px 14px; font-weight:bold; }"
            "QPushButton:hover { background:#3a6a9a; }"
            "QPushButton:pressed { background:#1a4a7a; }"
        )
        self._btn_export.clicked.connect(self._on_export_clicked)
        lay.addWidget(self._btn_export)

        margin = QWidget()
        margin.setFixedWidth(10)
        lay.addWidget(margin)

        # Fond noir, items de menu centrés verticalement dans la barre 54 px
        top_bar.setStyleSheet("""
            QWidget#top_bar {
                background: #000;
            }
            QWidget#top_bar > QWidget {
                background: transparent;
            }
            QMenuBar {
                background: transparent;
                color: #ddd;
                border: none;
            }
            QMenuBar::item {
                background: transparent;
                color: #ddd;
                padding: 18px 10px;
            }
            QMenuBar::item:selected {
                background: #2a2a2a;
                border-radius: 3px;
            }
            QMenuBar::item:pressed {
                background: #3a5a8a;
                border-radius: 3px;
            }
            QPushButton {
                background: #3a3a3a;
                color: #ddd;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 4px 10px;
            }
            QPushButton:hover  { background: #4a4a4a; }
            QPushButton:pressed { background: #2a2a2a; }
            QPushButton:checked { background: #3a5a8a; border-color: #5a8aba; }
        """)

        self.setMenuWidget(top_bar)

    def _setup_toolbar(self) -> None:
        pass  # Fusionné dans _setup_menu

    def _setup_central(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._splitter = QSplitter(Qt.Horizontal)
        root.addWidget(self._splitter)

        # Panneau gauche : sidebar (grille) ou edit panel (visionneuse)
        sidebar_w = self._config.get("ui.sidebar_width", 280)
        self._left_stack = QStackedWidget()
        self._left_stack.setMinimumWidth(160)
        self._splitter.addWidget(self._left_stack)
        self._splitter.setCollapsible(0, False)

        self._sidebar = Sidebar()
        self._sidebar.set_folder_count_provider(self._catalog.get_recursive_photo_counts)
        self._left_stack.addWidget(self._sidebar)   # index 0 — mode grille

        self._edit_panel = EditPanel()
        self._left_stack.addWidget(self._edit_panel)  # index 1 — mode visionneuse

        # Zone principale : grille ou visionneuse
        self._stack = QStackedWidget()
        self._splitter.addWidget(self._stack)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)

        # Index 0 — Grille photos (avec barre de contexte masquée par défaut)
        self._grid = ThumbnailGrid(self._thumb_cache)
        self._grid.photo_activated.connect(self._on_photo_activated)
        self._grid.selection_changed.connect(self._on_selection_changed)
        self._grid.rename_requested.connect(self._on_rename_requested)
        self._grid.move_requested.connect(self._on_move_requested)
        self._grid.delete_requested.connect(self._on_delete_requested)
        self._grid.remove_from_album_requested.connect(self._on_remove_from_album_requested)
        self._grid.save_requested.connect(self._on_save_requested)
        self._grid.duplicate_clicked.connect(self._on_duplicate_badge_clicked)
        self._grid.add_to_album_requested.connect(self._on_add_to_album)
        self._grid.create_album_with_requested.connect(self._on_create_album_with)
        self._grid.retry_face_index_requested.connect(self._on_retry_face_index_requested)
        self._grid.favorite_toggle_requested.connect(self._on_favorite_toggle_requested)
        self._grid.rating_change_requested.connect(self._on_rating_change_requested)
        self._grid.edit_tags_requested.connect(self._on_edit_tags_requested)

        self._grid_nav_bar = QWidget()
        self._grid_nav_bar.setStyleSheet("background: rgba(0,0,0,200);")
        _nav_layout = QHBoxLayout(self._grid_nav_bar)
        _nav_layout.setContentsMargins(8, 4, 8, 4)
        _btn_back_nav = QPushButton("←")
        _btn_back_nav.setToolTip("Retour à la page précédente")
        _btn_back_nav.setFixedWidth(32)
        _btn_back_nav.clicked.connect(self._on_back_nav_clicked)
        _nav_layout.addWidget(_btn_back_nav)
        self._lbl_grid_nav = QLabel("")
        self._lbl_grid_nav.setStyleSheet("color: #ccc;")
        _nav_layout.addWidget(self._lbl_grid_nav, stretch=1)
        self._grid_nav_bar.hide()

        _grid_container = QWidget()
        _grid_vbox = QVBoxLayout(_grid_container)
        _grid_vbox.setContentsMargins(0, 0, 0, 0)
        _grid_vbox.setSpacing(0)
        _grid_vbox.addWidget(self._grid_nav_bar)

        # Rangée grille + ascenseur ruban (affiché uniquement en mode chronologie)
        _grid_row = QWidget()
        _grid_hbox = QHBoxLayout(_grid_row)
        _grid_hbox.setContentsMargins(0, 0, 0, 0)
        _grid_hbox.setSpacing(0)
        _grid_hbox.addWidget(self._grid)
        self._ribbon_scroll = QScrollBar(Qt.Vertical)
        _grid_hbox.addWidget(self._ribbon_scroll)
        _grid_vbox.addWidget(_grid_row)

        self._grid.bind_ribbon_nav_bar(self._ribbon_scroll)
        self._stack.addWidget(_grid_container)

        # Index 1 — Visionneuse (avec panneau Visages rétractable à gauche)
        self._viewer = PhotoViewer(config=self._config, thumb_cache=self._thumb_cache)
        self._viewer.closed.connect(self._on_viewer_closed)
        self._viewer.navigate.connect(self._navigate_photo)
        self._viewer.zoom_changed.connect(self._on_viewer_zoom_changed)
        self._viewer.save_requested.connect(self._on_save_requested)
        self._viewer.rename_requested.connect(self._on_rename_requested)
        self._viewer.move_requested.connect(self._on_move_requested)
        self._viewer.delete_requested.connect(self._on_delete_requested)
        self._viewer.remove_from_album_requested.connect(self._on_remove_from_album_requested)
        self._viewer.force_redetect_requested.connect(self._on_force_redetect_requested)
        self._viewer.favorite_toggle_requested.connect(self._on_favorite_toggle_requested)
        self._viewer.rating_change_requested.connect(self._on_rating_change_requested)
        self._viewer.edit_tags_requested.connect(self._on_edit_tags_requested)
        self._viewer.folder_grid_requested.connect(
            lambda photo: self._navigate_to_photo_path(photo.path)
        )
        self._viewer.dup_badge_clicked.connect(self._on_duplicate_badge_clicked)
        self._edit_panel.edits_changed.connect(self._viewer.update_edit)
        self._edit_panel.crop_mode_requested.connect(self._viewer.enter_crop_mode)
        self._edit_panel.crop_confirm_requested.connect(self._viewer.confirm_crop)
        self._viewer.crop_mode_ended.connect(self._edit_panel.on_crop_mode_ended)
        self._edit_panel.grid_visibility_changed.connect(self._viewer.set_grid_visible)
        self._edit_panel.photo_saved.connect(self._on_photo_saved)
        self._edit_panel.rotation_stepped.connect(self._on_rotation_stepped)
        self._edit_panel.red_eye_mode_requested.connect(self._on_red_eye_mode_requested)
        self._edit_panel.wb_pick_requested.connect(self._on_wb_pick_requested)
        self._edit_panel.vignette_edit_mode.connect(self._on_vignette_edit_mode)
        self._edit_panel.annotation_mode_requested.connect(self._on_annotation_mode_requested)
        self._edit_panel.annotation_style_changed.connect(self._viewer.set_annotation_style)
        self._edit_panel.annotation_delete_selected_requested.connect(self._viewer.delete_selected_annotation)
        self._viewer.crop_ready.connect(self._edit_panel.apply_crop)
        self._viewer.red_eye_point_added.connect(self._edit_panel.on_red_eye_added)
        self._viewer.vignette_changed.connect(self._edit_panel.on_vignette_changed)
        self._viewer.pixel_sampled.connect(self._edit_panel.on_wb_pixel_received)
        self._viewer.annotation_added.connect(self._edit_panel.on_annotation_added)
        self._viewer.annotation_deleted.connect(self._edit_panel.on_annotation_deleted)
        self._viewer.annotation_deleted_multi.connect(self._edit_panel.on_annotation_deleted_multi)
        self._viewer.annotation_selection_changed.connect(self._edit_panel.on_annotation_selection_changed)
        self._viewer.annotation_moved.connect(self._edit_panel.on_annotation_moved)
        self._viewer.annotation_moved_multi.connect(self._edit_panel.on_annotation_moved_multi)
        self._viewer.annotation_resized.connect(self._edit_panel.on_annotation_resized)
        self._viewer.annotation_grouped.connect(self._edit_panel.on_annotation_grouped)

        self._face_panel = FacePanel(self._face_db, self._catalog, self)
        self._face_panel.face_highlighted.connect(self._on_face_highlighted)
        self._face_panel.all_faces_toggled.connect(self._on_all_faces_toggled)
        self._face_panel.person_assigned.connect(self._update_persons_counts)
        self._face_panel.cover_face_set.connect(self._on_cover_face_set)
        self._face_panel.person_cluster_requested.connect(
            self._on_face_panel_person_cluster_requested
        )
        self._face_panel.add_face_mode_requested.connect(self._on_add_face_mode_requested)
        self._viewer.face_context_menu_requested.connect(self._on_face_context_menu)
        self._viewer.face_bbox_ready.connect(self._face_panel.on_face_bbox_ready)
        self._viewer.face_add_mode_ended.connect(self._face_panel.reset_add_face_button)
        self._face_panel_refresh_timer.timeout.connect(self._face_panel.refresh)
        self._face_panel.hide()
        self._exif_panel = ExifPanel(self)
        self._exif_panel.photo_saved.connect(self._on_exif_photo_saved)
        self._exif_panel.hide()

        # Conteneur droit (face OU exif) — un seul visible à la fois
        self._right_panel = QWidget(self)
        _rp_layout = QVBoxLayout(self._right_panel)
        _rp_layout.setContentsMargins(0, 0, 0, 0)
        _rp_layout.setSpacing(0)
        _rp_layout.addWidget(self._face_panel)
        _rp_layout.addWidget(self._exif_panel)
        self._right_panel.hide()

        self._viewer_splitter = QSplitter(Qt.Horizontal)
        self._viewer_splitter.addWidget(self._viewer)
        self._viewer_splitter.addWidget(self._right_panel)
        self._viewer_splitter.setStretchFactor(0, 1)
        self._viewer_splitter.setStretchFactor(1, 0)
        self._viewer_splitter.setCollapsible(0, False)
        self._viewer_splitter.setCollapsible(1, False)
        self._stack.addWidget(self._viewer_splitter)

        # Index 2 — Grille des groupes de visages
        self._face_cluster_grid = FaceClusterGrid(
            self._face_db, self._catalog, self
        )
        self._face_cluster_grid.cluster_named.connect(self._on_cluster_named)
        self._face_cluster_grid.cluster_assigned.connect(self._on_cluster_assigned)
        self._face_cluster_grid.clusters_named.connect(self._on_clusters_named)
        self._face_cluster_grid.clusters_assigned.connect(self._on_clusters_assigned)
        self._face_cluster_grid.cluster_ignored.connect(self._on_cluster_ignored)
        self._face_cluster_grid.clusters_ignored.connect(self._on_clusters_ignored)
        self._face_cluster_grid.cluster_merged.connect(self._on_cluster_merged)
        self._face_cluster_grid.photos_requested.connect(self._on_cluster_photos_requested)
        self._face_cluster_grid.back_requested.connect(self.show_grid)
        self._face_cluster_grid.persons_updated.connect(self._update_persons_counts)
        self._stack.addWidget(self._face_cluster_grid)

        # Index 3 — Vue des groupes d'une personne nommée
        self._person_cluster_view = PersonClusterView(self._face_db, self._catalog, self)
        self._person_cluster_view.photos_requested.connect(
            self._on_person_cluster_photos_requested
        )
        self._person_cluster_view.photo_requested.connect(
            self._on_person_cluster_photo_requested
        )
        self._person_cluster_view.back_requested.connect(self._on_person_cluster_back)
        self._person_cluster_view.cluster_unassigned.connect(
            self._on_pcv_cluster_unassigned
        )
        self._person_cluster_view.cluster_named.connect(self._on_cluster_named)
        self._person_cluster_view.cluster_assigned.connect(self._on_cluster_assigned)
        self._person_cluster_view.faces_reassigned.connect(self._update_persons_counts)
        self._person_cluster_view.cover_face_set.connect(self._on_cover_face_set)
        self._person_cluster_view.suggestion_accepted.connect(self._on_suggestion_accepted)
        self._person_cluster_view.suggestion_rejected.connect(self._on_suggestion_rejected)
        self._person_cluster_view.all_suggestions_accepted.connect(self._on_all_suggestions_accepted)
        self._person_cluster_view.all_suggestions_rejected.connect(self._on_all_suggestions_rejected)
        self._person_cluster_view.add_to_album_requested.connect(self._on_add_to_album)
        self._person_cluster_view.create_album_with_requested.connect(self._on_create_album_with)
        self._stack.addWidget(self._person_cluster_view)

        # Index 4 — Grille des groupes de doublons
        self._duplicate_grid = DuplicateGrid(self._catalog, self._thumb_cache, self)
        self._duplicate_grid.back_requested.connect(self.show_grid)
        self._duplicate_grid.view_requested.connect(self._on_duplicate_group_view_requested)
        self._duplicate_grid.group_ignored.connect(self._on_duplicate_group_ignored)
        self._duplicate_grid.detect_requested.connect(self._start_duplicate_detection)
        self._stack.addWidget(self._duplicate_grid)

        # Connexions sidebar
        self._sidebar.folder_selected.connect(self._on_folder_selected)
        self._sidebar.album_selected.connect(self._on_album_selected)
        self._sidebar.album_delete_requested.connect(self._on_album_delete_requested)
        self._sidebar.scan_requested.connect(self._on_scan_requested)
        self._sidebar.folder_removed.connect(self._on_folder_removed)
        self._sidebar.folder_created.connect(self._on_folder_created)
        self._sidebar.folder_moved.connect(self._on_folder_moved)
        self._sidebar.folder_deleted.connect(self._on_folder_deleted)
        self._sidebar.photos_dropped.connect(self._on_photos_dropped)
        self._sidebar.person_selected.connect(self._on_person_selected)
        self._sidebar.identify_requested.connect(self.show_face_clusters)
        self._sidebar.duplicates_requested.connect(self.show_duplicate_grid)
        self._sidebar.advanced_search_requested.connect(self._open_advanced_search)
        self._sidebar.person_merge_requested.connect(self._on_person_merge_requested)
        self._sidebar.person_rename_requested.connect(self._on_person_rename_requested)
        self._sidebar.person_clear_requested.connect(self._on_person_clear_requested)
        self._sidebar.tree_state_changed.connect(
            lambda paths: self._config.set("ui.folder_tree_expanded", paths)
        )
        bus.on("album.create_requested", self._on_album_create)

    def _setup_statusbar(self) -> None:
        sb = self.statusBar()

        # Tiers gauche — actions en cours (scan, chargement…)
        self._lbl_action = QLabel("")
        self._lbl_action.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._lbl_action.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sb.addWidget(self._lbl_action, 1)

        # Barre de progression pour les opérations longues (cachée par défaut)
        self._sb_progress_bar = QProgressBar()
        self._sb_progress_bar.setFixedWidth(220)
        self._sb_progress_bar.setTextVisible(True)
        self._sb_progress_bar.setFormat("%v / %m photos")
        self._sb_progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #555; border-radius: 3px; "
            "               background: #2a2a2a; text-align: center; font-size: 11px; }"
            "QProgressBar::chunk { background: #2a5a9a; border-radius: 2px; }"
        )
        self._sb_progress_bar.hide()
        sb.addWidget(self._sb_progress_bar)

        # Compteur de fichiers corrompus détectés pendant un scan de doublons
        # (masqué par défaut) — cliquable pour afficher la liste courante.
        self._lbl_corrupted = QPushButton("")
        self._lbl_corrupted.setFlat(True)
        self._lbl_corrupted.setCursor(Qt.PointingHandCursor)
        self._lbl_corrupted.setStyleSheet("QPushButton { color: #d9822b; border: none; }")
        self._lbl_corrupted.hide()
        self._lbl_corrupted.clicked.connect(self._show_corrupted_list_dialog)
        sb.addWidget(self._lbl_corrupted)
        # Restaure l'état persisté (survit à un redémarrage — voir dedup_cache.py)
        # plutôt que d'attendre la fin du prochain scan pour le réafficher.
        self._update_corrupted_indicator(self._load_persisted_corrupted_paths())

        # Tiers centre — nom du fichier sélectionné et sa taille
        self._lbl_fileinfo = QLabel("")
        self._lbl_fileinfo.setAlignment(Qt.AlignCenter)
        self._lbl_fileinfo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sb.addWidget(self._lbl_fileinfo, 1)

        # --- Contrôles mode grille ---
        self._lbl_thumb_size = QLabel("Taille :")
        sb.addPermanentWidget(self._lbl_thumb_size)

        self._thumb_slider = MarkedSlider(
            Qt.Horizontal,
            fmt=lambda v: f"{_THUMB_SIZES[max(0, min(len(_THUMB_SIZES)-1, v))]}",
        )
        self._thumb_slider.setRange(0, len(_THUMB_SIZES) - 1)
        self._thumb_slider.setValue(1)
        self._thumb_slider.setFixedWidth(100)
        self._thumb_slider.valueChanged.connect(self._on_thumb_size_changed)
        sb.addPermanentWidget(self._thumb_slider)

        # --- Contrôles mode visionneuse (cachés par défaut) ---
        self._lbl_zoom = QLabel("Zoom :")
        self._lbl_zoom.hide()
        sb.addPermanentWidget(self._lbl_zoom)

        self._zoom_slider = MarkedSlider(Qt.Horizontal, fmt=lambda v: f"{v}%")
        self._zoom_slider.setRange(10, 400)    # 10 % à 400 %
        self._zoom_slider.setValue(100)
        self._zoom_slider.setFixedWidth(120)
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        self._zoom_slider.hide()
        sb.addPermanentWidget(self._zoom_slider)

        self._zoom_pct_label = QLabel("100%")
        self._zoom_pct_label.setFixedWidth(48)
        self._zoom_pct_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._zoom_pct_label.hide()
        sb.addPermanentWidget(self._zoom_pct_label)

        # --- Bouton retour grille (masqué en mode visionneuse) ---
        self._btn_grid_status = QPushButton("▦")
        self._btn_grid_status.setToolTip("Retour à la grille")
        self._btn_grid_status.setFixedWidth(28)
        self._btn_grid_status.clicked.connect(self.show_grid)
        sb.addPermanentWidget(self._btn_grid_status)

    # ------------------------------------------------------------------ bus

    def _connect_bus(self) -> None:
        bus.on("library.photo_selected", self._on_bus_photo_selected)

    def _connect_scanner(self) -> None:
        pass  # connections are per-thread, see _start_scan

    def _setup_folder_watcher(self) -> None:
        self._folder_watcher = FolderWatcher(self)
        self._folder_watcher.files_changed.connect(self._on_watcher_files_changed)
        self._folder_watcher.subfolder_added.connect(self._on_folder_created)

    # ------------------------------------------------------------------ mise à jour

    def _start_update_check(self) -> None:
        """Interroge la dernière release GitHub en arrière-plan (silencieux si à jour ou en erreur)."""
        self._update_check_thread = UpdateCheckThread(self)
        self._update_check_thread.checked.connect(self._on_update_checked)
        self._update_check_thread.start()

    def _on_update_checked(self, status: str, version: str, html_url: str) -> None:
        if status != STATUS_UPDATE_AVAILABLE:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Mise à jour disponible")
        box.setText(
            f"Une nouvelle version de Pixel Photo Manager est disponible : {version}\n"
            f"(version actuelle : {get_app_version()}).\n\n"
            "Pensez à lire les notes de version avant d'installer, pour connaître les "
            "nouvelles fonctionnalités et vérifier la compatibilité avec votre "
            "bibliothèque existante."
        )
        btn_open = box.addButton("Ouvrir la page de téléchargement", QMessageBox.AcceptRole)
        box.addButton("Plus tard", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is btn_open:
            QDesktopServices.openUrl(QUrl(html_url))

    # ------------------------------------------------------------------ library

    def _load_library(self) -> None:
        folders = self._config.get_scan_folders()
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)
        self._sidebar.set_folder_order(
            self._config.get("display_order.folder_mode", "alpha"),
            self._config.get("display_order.folder_dir", "asc"),
        )
        if folders:
            self._sidebar.set_tree_expanded_paths(
                self._config.get("ui.folder_tree_expanded", [])
            )
            self._sidebar.refresh_folders(folders)
            error_paths = self._face_db.get_error_paths()
            self._grid.set_index_error_paths(error_paths)
            self._restore_last_view(albums, folders)
            # Pré-charger InsightFace en parallèle du scan
            self._warmup_thread = TFWarmUpThread(self)
            self._warmup_thread.finished.connect(self._on_warmup_done)
            self._warmup_thread.start()
            self._start_scan(folders)
            self._folder_watcher.set_folders(folders)
        else:
            self._show_all_photos()
            self._sidebar.select_album_item(_SPECIAL_ALL)

    def _restore_last_view(self, albums: list, folders: list) -> None:
        """Restaure la dernière vue active depuis la config."""
        saved = self._config.get("ui.last_view", {})
        vtype = saved.get("type", "all") if saved else "all"

        if vtype == "folder":
            path = saved.get("value", "")
            if path and os.path.isdir(path):
                norm_path = os.path.normcase(path)
                is_under_scan = any(
                    norm_path == os.path.normcase(f)
                    or norm_path.startswith(os.path.normcase(f) + os.sep)
                    for f in folders
                )
                if is_under_scan:
                    self._on_folder_selected(path)
                    self._sidebar.select_folder_item(path)
                    return
        elif vtype == "favorites":
            self._on_album_selected(_SPECIAL_FAV)
            self._sidebar.select_album_item(_SPECIAL_FAV)
            return
        elif vtype == "videos":
            self._on_album_selected(_SPECIAL_VIDEOS)
            self._sidebar.select_album_item(_SPECIAL_VIDEOS)
            return
        elif vtype == "rated":
            self._on_album_selected(_SPECIAL_RATED)
            self._sidebar.select_album_item(_SPECIAL_RATED)
            return
        elif vtype == "album":
            album_id = saved.get("value")
            album = next((a for a in albums if a.id == album_id), None)
            if album:
                self._on_album_selected(album)
                self._sidebar.select_album_item(album)
                return
        elif vtype == "person":
            self._pending_person_view_id = saved.get("value")
            # Affichage intermédiaire en attendant le chargement des personnes

        self._show_all_photos()
        self._sidebar.select_album_item(_SPECIAL_ALL)

    def _cancel_grid_display_ops(self) -> None:
        """Annule tout chargement de photos pour la grille encore en vol.
        À appeler avant de démarrer un nouvel affichage (dossier/album/Toutes les
        photos) : sans ça, une requête précédente encore en cours peut se terminer
        après coup et écraser la vue actuelle avec des photos qui ne correspondent
        plus au contexte affiché."""
        if self._catalog_loader is not None:
            self._catalog_loader.stop()
            if self._catalog_loader.isRunning():
                self._catalog_loader.batch_ready.disconnect()
                self._catalog_loader.finished.connect(self._catalog_loader.deleteLater)
            else:
                self._catalog_loader.deleteLater()
            self._catalog_loader = None
        if self._photo_query_thread is not None:
            if self._photo_query_thread.isRunning():
                self._photo_query_thread.photos_ready.disconnect()
                self._photo_query_thread.finished.connect(self._photo_query_thread.deleteLater)
            else:
                self._photo_query_thread.deleteLater()
            self._photo_query_thread = None

    def _show_all_photos(self) -> None:
        self._cancel_grid_display_ops()

        self._current_photos = []
        self._current_paths = set()
        self._current_context = "Toutes les photos"
        self._current_album_id = None
        self._grid.set_album_context(None)
        self._grid.set_ribbon_mode(True)
        self._grid.set_date_overlay_visible(True)
        self._grid.set_photos([])
        self._grid_nav_bar.hide()
        self.show_grid()
        self._update_status()

        chrono_dir = self._config.get(
            "display_order.chrono_album_dir",
            self._config.get("display_order.grid_dir", "desc"),
        )
        ascending = chrono_dir == "asc"
        loader = _CatalogLoadThread(self._catalog, reverse=ascending, parent=self)
        loader.batch_ready.connect(self._on_catalog_batch)
        self._catalog_loader = loader
        loader.start()

    @Slot(list)
    def _on_catalog_batch(self, photos: list) -> None:
        if self._current_context != "Toutes les photos":
            return
        self._current_photos.extend(photos)
        self._current_paths.update(p.path for p in photos)
        self._grid.add_photos_batch(photos)
        self._update_status()

    def _start_scan(self, folders: list[str], force: bool = False) -> None:
        # Trace si ce scan a retiré des photos (fichiers disparus du disque) :
        # _on_scan_finished s'en sert pour ne rafraîchir albums/personnes que
        # si quelque chose a réellement changé.
        self._scan_had_removals = False
        thread = self._scanner.scan(folders, force=force)
        thread.photos_batch.connect(self._on_photos_batch)
        thread.photos_removed.connect(self._on_photos_removed)
        thread.finished.connect(self._on_scan_finished)
        thread.progress.connect(self._on_scan_progress)

    # ------------------------------------------------------------------ slots

    @Slot(list)
    def _on_photos_batch(self, photos: list) -> None:
        visible_new: list = []
        for photo in photos:
            visible = (
                self._current_context == "Toutes les photos"
                or os.path.normcase(photo.directory) == os.path.normcase(self._current_context)
            )
            if visible and photo.path not in self._current_paths:
                visible_new.append(photo)
                self._current_photos.append(photo)
                self._current_paths.add(photo.path)
        if visible_new:
            # Les photos découvertes pendant le scan arrivent dans l'ordre du
            # système de fichiers, pas trié : on re-trie pour respecter le
            # réglage "Ordre d'affichage" pendant le scan (pas seulement à la fin).
            self._current_photos = self._sort_photos_for_display(
                self._current_photos, self._current_context
            )
            self._grid.set_photos(self._current_photos)
            self._update_status()

    @Slot(list)
    def _on_photos_removed(self, paths: list[str]) -> None:
        """Retire de l'UI les photos dont le fichier a disparu du disque."""
        removed_set = set(paths)
        self._scan_had_removals = True
        self._current_photos = [p for p in self._current_photos
                                 if p.path not in removed_set]
        self._current_paths -= removed_set
        self._grid.remove_photos(paths)
        self._update_status()
        self._face_db.delete_for_paths(paths)
        logger.info("%d photo(s) retirée(s) du catalogue (fichiers absents)", len(paths))

    @Slot(int)
    def _on_scan_finished(self, total: int) -> None:
        self._lbl_action.setText("")
        self._update_status()
        # Ne rafraîchir albums et personnes que si le scan a réellement changé
        # quelque chose (nouvelles photos, ou fichiers disparus du disque) : un
        # rescan sans changement — cas fréquent d'un événement watcher sur un
        # simple attribut de fichier — ne doit rien coûter.
        if total or self._scan_had_removals:
            self._sidebar.refresh_albums(self._catalog.get_albums())
        # Rebuild complet uniquement si le scan a trouvé de nouvelles photos
        # (donc potentiellement de nouveaux visages/personnes) ; mise à jour
        # légère des compteurs sur simple suppression — évite de vider et
        # recharger toute la liste des personnes avec ses vignettes. Le tout
        # premier passage refresh toujours (remplissage initial de la liste,
        # cf. _persons_loaded) : update_persons_data bascule alors d'elle-même
        # sur un rebuild complet puisque la liste est encore vide.
        if total:
            self._refresh_persons()
        elif self._scan_had_removals or not self._persons_loaded:
            self._update_persons_counts()
        self._persons_loaded = True

        # Le scan ajoute les nouvelles photos dans l'ordre filesystem (non trié).
        # On re-trie la liste courante selon le réglage "Ordre d'affichage".
        # Applicable à "Toutes les photos" et aux vues dossier (les vues spéciales
        # comme Favoris, Vidéos ou Person ne reçoivent pas de photos via _on_photos_batch).
        # Inutile si le scan n'a rien ajouté : l'ordre courant est déjà bon.
        if total and self._current_photos \
                and not self._current_context.startswith(_PERSON_CTX_PREFIX):
            self._current_photos = self._sort_photos_for_display(
                self._current_photos, self._current_context
            )
            self._grid.set_photos(self._current_photos)

        if self._warmup_thread and self._warmup_thread.isRunning():
            self._lbl_action.setText("Initialisation de la reconnaissance faciale…")
            self._face_index_pending = True
        else:
            self._start_face_indexing()
        # Différer la détection des doublons jusqu'à ce que les vignettes des
        # visages des personnes connues (sidebar) soient chargées, pour ne pas
        # leur faire concurrence en CPU/E-S dès le démarrage de l'application.
        # Garde manuelle plutôt que Qt.UniqueConnection : sur cette connexion
        # précise (méthode @Slot() héritée d'un mixin non-QObject, connectée
        # avec Qt.UniqueConnection), PySide6 renvoie un QMetaObject.Connection
        # valide mais le slot n'est ensuite jamais invoqué — connexion fantôme
        # silencieuse. Sans Qt.UniqueConnection la connexion fonctionne
        # normalement ; la garde évite juste les connexions multiples si
        # _on_scan_finished est appelé plusieurs fois avant le premier tir.
        if not self._dup_gate_connected:
            self._dup_gate_connected = True
            self._sidebar.persons_thumbnails_ready.connect(
                self._on_persons_thumbnails_ready_start_duplicates
            )

    @Slot()
    def _on_warmup_done(self) -> None:
        if self._warmup_thread is not None:
            self._warmup_thread.deleteLater()
            self._warmup_thread = None
        if self._face_index_pending:
            self._face_index_pending = False
            self._lbl_action.setText("")
            self._start_face_indexing()

    # ------------------------------------------------------------------ settings

    def _open_thread_journal(self) -> None:
        from src.ui.thread_journal_dialog import ThreadJournalDialog
        dlg = ThreadJournalDialog(self)
        dlg.exec()

    def _open_problems_history(self) -> None:
        from src.ui.problems_history_dialog import ProblemsHistoryDialog
        ProblemsHistoryDialog(self).exec()

    def _open_deleted_corrupted_files_dialog(self) -> None:
        from src.ui.deleted_corrupted_files_dialog import DeletedCorruptedFilesDialog
        DeletedCorruptedFilesDialog(self).exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._config, self)
        dlg.recluster_needed.connect(self._run_clustering)
        dlg.exec()

    _MEDIA_SCOPE_LABELS = {"image": "Photo", "video": "Vidéo", "both": "Les deux"}

    def _open_external_apps_dialog(self) -> None:
        """Dialogue de configuration des applications externes accessibles depuis le viewer.

        Chaque application est taguée d'une portée média (photo / vidéo / les
        deux) qui détermine dans quel cas son icône apparaît dans la barre de
        la visionneuse (PhotoViewer.refresh_external_apps) — une entrée sans
        clé "media" (config antérieure à cette fonctionnalité) est traitée
        comme "both", donc reste visible partout comme avant."""
        apps: list = list(self._config.get("tools.external_apps", []))

        dlg = QDialog(self)
        dlg.setWindowTitle("Applications externes")
        dlg.setMinimumWidth(520)
        root = QVBoxLayout(dlg)

        root.addWidget(QLabel(
            "Applications disponibles via leur icône dans la barre de la visionneuse :"
        ))

        lst = QListWidget(dlg)
        for app in apps:
            label = self._MEDIA_SCOPE_LABELS.get(app.get("media", "both"), "Les deux")
            lst.addItem(f"{app['name']}   —   {app['path']}   [{label}]")

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Ajouter…")
        btn_del = QPushButton("Supprimer")
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_del)
        btn_row.addStretch()
        root.addWidget(lst)
        root.addLayout(btn_row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        root.addWidget(btns)

        def _add() -> None:
            path, _ = QFileDialog.getOpenFileName(
                dlg, "Choisir une application", "",
                "Exécutables (*.exe);;Tous les fichiers (*)"
            )
            if not path:
                return
            default_name = os.path.splitext(os.path.basename(path))[0]
            name, ok = QInputDialog.getText(
                dlg, "Nom de l'application",
                "Nom affiché dans l'infobulle :", text=default_name
            )
            if not (ok and name.strip()):
                return
            media_label, ok = QInputDialog.getItem(
                dlg, "Type de média",
                "Afficher l'icône de cette application pour :",
                ["Les deux", "Photo", "Vidéo"], 0, False
            )
            if not ok:
                return
            media = {"Photo": "image", "Vidéo": "video", "Les deux": "both"}[media_label]
            apps.append({"name": name.strip(), "path": path, "media": media})
            lst.addItem(f"{name.strip()}   —   {path}   [{media_label}]")

        def _del() -> None:
            row = lst.currentRow()
            if row >= 0:
                apps.pop(row)
                lst.takeItem(row)

        btn_add.clicked.connect(_add)
        btn_del.clicked.connect(_del)

        if dlg.exec() != QDialog.Accepted:
            return

        self._config.set("tools.external_apps", apps)
        self._viewer.refresh_external_apps()

    def _open_exif_date_sync(self) -> None:
        from src.ui.exif_date_sync_dialog import ExifDateSyncDialog
        dlg = ExifDateSyncDialog(self._catalog, self)
        dlg.exec()

    def _open_folder_manager(self) -> None:
        from src.ui.folder_manager_dialog import FolderManagerDialog
        dlg = FolderManagerDialog(self._config, self._catalog, self)
        dlg.rescan_requested.connect(self._on_folder_rescan_requested)
        dlg.folder_removed.connect(self._on_folder_removed)
        dlg.folder_added.connect(self._on_folder_added_from_manager)
        dlg.exec()

    def _on_folder_rescan_requested(self, folder: str) -> None:
        self._start_scan([folder], force=True)

    def _on_folder_added_from_manager(self, folder: str) -> None:
        self._config.add_scan_folder(folder)
        all_folders = self._config.get_scan_folders()
        self._sidebar.refresh_folders(all_folders)
        self._start_scan([folder])
        self._folder_watcher.set_folders(all_folders)
        self._maybe_prompt_picasa_for_new_folder(folder)

    def _on_back_nav_clicked(self) -> None:
        if self._from_person_cluster_view:
            self._from_person_cluster_view = False
            person = self._person_cluster_view.current_person
            if person:
                self.show_person_clusters(person)
                return
        if self._current_context.startswith(f"{_PERSON_CTX_PREFIX}cluster_"):
            self.show_face_clusters()
        else:
            self._grid_nav_bar.hide()
            self.show_grid()

    @Slot(int, str)
    def _on_scan_progress(self, percent: int, path: str) -> None:
        self._lbl_action.setText(f"Scan… {percent}%  —  {path}")

    @Slot(object)
    def _on_photo_activated(self, photo: PhotoInfo) -> None:
        self._current_photo_index = next(
            (i for i, p in enumerate(self._current_photos) if p.path == photo.path), 0
        )
        self.show_viewer(photo)

    @Slot(list)
    def _on_selection_changed(self, photos: list[PhotoInfo]) -> None:
        self._update_status(photos)

    def _on_red_eye_mode_requested(self, active: bool, radius: float) -> None:
        if active:
            self._viewer.enter_red_eye_mode(radius)
        else:
            self._viewer.exit_red_eye_mode()

    def _on_vignette_edit_mode(self, active: bool, edit) -> None:
        if active:
            self._viewer.enter_vignette_mode(edit)
        else:
            self._viewer.exit_vignette_mode()

    def _on_annotation_mode_requested(self, active: bool, tool: str) -> None:
        if active:
            self._viewer.enter_annotation_mode(tool)
        else:
            self._viewer.exit_annotation_mode()

    @Slot(bool)
    def _on_wb_pick_requested(self, start: bool) -> None:
        if start:
            self._viewer.start_color_pick()
        else:
            self._viewer.stop_color_pick()

    @Slot(bool)
    def _on_faces_toggle(self, checked: bool) -> None:
        if checked:
            self._btn_exif_toggle.setChecked(False)
            self._exif_panel.hide()
            self._face_panel.show()
            self._right_panel.show()
            photo = self._viewer.current_photo()
            if photo:
                self._face_panel.set_photo(photo.path)
        else:
            self._face_panel.hide()
            self._viewer.highlight_face(None)
            if not self._exif_panel.isVisible():
                self._right_panel.hide()

    def _on_exif_toggle(self, checked: bool) -> None:
        if checked:
            self._btn_faces_toggle.setChecked(False)
            self._face_panel.hide()
            self._exif_panel.show()
            self._right_panel.show()
            photo = self._viewer.current_photo()
            if photo:
                self._exif_panel.set_photo(photo.path)
                self._exif_panel.set_tags(photo.tags)
        else:
            self._exif_panel.hide()
            if not self._face_panel.isVisible():
                self._right_panel.hide()

    @Slot(bool)
    def _on_annotations_toggle(self, checked: bool) -> None:
        self._annotations_globally_visible = checked
        self._viewer.set_annotations_visible(checked)

    def _update_nav_arrows(self) -> None:
        n = len(self._current_photos)
        self._viewer.update_nav_arrows(
            has_prev=self._current_photo_index < n - 1,  # il y a des photos plus anciennes
            has_next=self._current_photo_index > 0,       # il y a des photos plus récentes
        )

    @Slot(int)
    def _navigate_photo(self, delta: int) -> None:
        if not self._current_photos:
            return
        new_index = max(0, min(self._current_photo_index + delta, len(self._current_photos) - 1))
        if new_index == self._current_photo_index:
            self._update_nav_arrows()
            return
        self._current_photo_index = new_index
        photo = self._current_photos[self._current_photo_index]
        is_video = photo.media_type == "video"
        self._viewer.set_photo(photo)
        if not is_video:
            self._edit_panel.set_photo(photo)
        self._left_stack.setCurrentIndex(0 if is_video else 1)
        if self._face_panel.isVisible():
            self._face_panel.set_photo(photo.path)
        if self._exif_panel.isVisible():
            self._exif_panel.set_photo(photo.path)
            self._exif_panel.set_tags(photo.tags)
        self._update_viewer_status(photo)
        self._update_nav_arrows()
        self._prefetch_viewer_neighbors()

    def _prefetch_viewer_neighbors(self) -> None:
        """Précharge l'image de base des photos voisines de celle affichée dans
        la visionneuse (les plus proches d'abord) : prev/next devient instantané."""
        idx = self._current_photo_index
        photos = self._current_photos
        neighbors = [
            photos[i]
            for i in (idx - 1, idx + 1, idx - 2, idx + 2)
            if 0 <= i < len(photos)
        ]
        if neighbors:
            self._viewer.prefetch(neighbors)

    @Slot(float)
    def _on_viewer_zoom_changed(self, zoom: float) -> None:
        pct = int(zoom * 100)
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(max(10, min(400, pct)))
        self._zoom_slider.blockSignals(False)
        self._zoom_pct_label.setText(f"{pct}%")

    @Slot(int)
    def _on_zoom_slider_changed(self, value: int) -> None:
        self._viewer.set_zoom(value / 100.0)
        self._zoom_pct_label.setText(f"{value}%")

    def _start_photo_query(
        self, fn, context_key: str, album_id: int | None = None, folder_path: str | None = None
    ) -> None:
        """Lance une requête photo en arrière-plan et met à jour la grille à l'arrivée.
        folder_path : chemin réel du dossier sélectionné (seulement pour la sidebar
        « Dossiers », distinct de context_key qui sert aussi de libellé d'affichage
        pour les autres vues) — permet de détecter une copie de DVD sans confondre
        avec un nom d'album qui coïnciderait par hasard avec un chemin du disque."""
        self._cancel_grid_display_ops()
        # Retour visuel immédiat au clic (l'indicateur ne s'affiche réellement
        # que si la requête dépasse 150 ms) ; masqué par grid.set_photos().
        self._grid.set_loading(True)
        # Paramètres de tri résolus ici (thread UI : lectures de Config),
        # tri exécuté dans le thread avec la requête.
        key_fn, reverse = self._sort_params_for_context(context_key)
        self._photo_query_thread = _PhotoQueryThread(
            fn, context_key, key_fn, reverse, self
        )
        self._photo_query_thread.photos_ready.connect(
            lambda photos, ctx, aid=album_id, fp=folder_path: self._on_photo_query_ready(photos, ctx, aid, fp)
        )
        self._photo_query_thread.start()

    def _on_photo_query_ready(
        self, photos: list, context_key: str, album_id: int | None = None, folder_path: str | None = None
    ) -> None:
        self._current_photos   = photos
        self._current_paths    = {p.path for p in photos}
        self._current_context  = context_key
        self._current_album_id = album_id
        self._grid.set_album_context(album_id)
        self._grid.set_photos(photos)
        if folder_path and not photos:
            video_ts = find_dvd_video_ts(folder_path)
            if video_ts:
                self._grid.show_empty_message(
                    "Ce dossier ne contient aucune photo cataloguée, mais semble être "
                    "une copie de DVD (dossier VIDEO_TS).",
                    "Ouvrir avec un lecteur externe",
                    lambda _checked=False, fp=folder_path: self._open_dvd_folder(fp),
                )
        self._update_status()

    def _open_dvd_folder(self, folder_path: str) -> None:
        """Ouvre un dossier « copie de DVD » dans une application externe déjà
        configurée par l'utilisateur (menu Outils › Applications externes…, même
        liste que celle utilisée par le viewer pour ouvrir une photo — cf.
        PhotoViewer._open_with). On lui passe le dossier lui-même (et non le
        sous-dossier VIDEO_TS) : VLC et la plupart des lecteurs détectent
        VIDEO_TS à l'intérieur d'un dossier passé en argument. Seules les
        applications taguées "vidéo" ou "les deux" sont proposées ici — une
        application taguée "photo" (ex. un éditeur d'images) n'a pas de sens
        pour ouvrir un dossier VIDEO_TS."""
        all_apps: list = list(self._config.get("tools.external_apps", []))
        apps = [a for a in all_apps if a.get("media", "both") != "image"]
        if not apps:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Aucune application externe configurée")
            if all_apps:
                box.setText(
                    "Aucune application externe configurée n'est compatible avec "
                    "la vidéo (toutes sont limitées aux photos). Configurez-en une "
                    "(ex. VLC) via le menu Outils › Applications externes… pour "
                    "pouvoir ouvrir ce dossier."
                )
            else:
                box.setText(
                    "Configurez d'abord une application externe (ex. VLC) via le menu "
                    "Outils › Applications externes… pour pouvoir ouvrir ce dossier."
                )
            btn_configure = box.addButton("Configurer…", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is btn_configure:
                self._open_external_apps_dialog()
            return

        if len(apps) == 1:
            self._launch_external_app(apps[0].get("path", ""), folder_path)
            return

        self._external_apps_menu(apps, folder_path).exec(self.cursor().pos())

    def _external_apps_menu(self, apps: list, target_path: str) -> QMenu:
        """Construit (sans l'afficher) le menu de choix d'application externe
        pour target_path — extrait de _open_dvd_folder pour rester testable
        sans passer par un QMenu.exec() modal."""
        menu = QMenu(self)
        for app in apps:
            name = app.get("name", "")
            path = app.get("path", "")
            menu.addAction(name, lambda _checked=False, p=path: self._launch_external_app(p, target_path))
        return menu

    def _launch_external_app(self, app_path: str, target_path: str) -> None:
        try:
            subprocess.Popen([app_path, target_path])
        except Exception as exc:
            logger.warning("Impossible de lancer '%s' : %s", app_path, exc)
            QMessageBox.warning(
                self,
                "Impossible de lancer l'application",
                f"Échec du lancement de :\n{app_path}\n\n{exc}",
            )

    def _sort_params_for_context(self, context: str) -> tuple:
        """Résout les paramètres de tri (key_fn, reverse) du réglage "Ordre
        d'affichage" pour un contexte donné. Doit être appelé sur le thread UI
        (lectures de Config) ; le tri lui-même peut ensuite s'exécuter dans un
        thread secondaire (_PhotoQueryThread). La vue "Toutes les photos"
        (Chronologie) reste toujours triée chronologiquement — un tri
        alphabétique n'a pas de sens pour un album qui s'appelle
        "Chronologie" — mais sa direction suit un réglage dédié
        (`display_order.chrono_album_dir`), indépendant de celui de la
        grille de photos standard (`display_order.grid_dir`)."""
        if context == "Toutes les photos":
            mode = "chrono"
            direction = self._config.get(
                "display_order.chrono_album_dir",
                self._config.get("display_order.grid_dir", "desc"),
            )
        else:
            mode = self._config.get("display_order.grid_mode", "chrono")
            direction = self._config.get("display_order.grid_dir", "desc")
        key_fn = _photo_sort_key if mode == "chrono" else _photo_filename_sort_key
        return key_fn, direction == "desc"

    def _sort_photos_for_display(self, photos: list, context: str) -> list:
        """Applique le réglage "Ordre d'affichage" à une liste de photos, sur
        le thread courant (voir _sort_params_for_context pour la version
        déportée dans _PhotoQueryThread)."""
        key_fn, reverse = self._sort_params_for_context(context)
        return sorted(photos, key=key_fn, reverse=reverse)

    def _open_display_order_dialog(self) -> None:
        dlg = DisplayOrderDialog(self._config, self)
        if dlg.exec() == QDialog.Accepted:
            dlg.save_to_config()
            self._apply_display_order()

    def _apply_display_order(self) -> None:
        """Réapplique le réglage "Ordre d'affichage" à l'arbre de dossiers et
        à la grille couramment affichée (appelé après modification via le
        dialogue, ou au chargement de la bibliothèque)."""
        self._sidebar.set_folder_order(
            self._config.get("display_order.folder_mode", "alpha"),
            self._config.get("display_order.folder_dir", "asc"),
        )
        self._sidebar.refresh_folders(self._config.get_scan_folders())
        if self._current_photos:
            self._current_photos = self._sort_photos_for_display(
                self._current_photos, self._current_context
            )
            self._grid.set_photos(self._current_photos)

    @Slot(str)
    def _on_folder_selected(self, folder: str) -> None:
        self._grid.set_ribbon_mode(False)
        self._grid.set_date_overlay_visible(False)
        self._grid_nav_bar.hide()
        self.show_grid()
        self._current_album_id = None
        self._start_photo_query(
            lambda: self._catalog.get_photos_in_folder(folder),
            folder,
            folder_path=folder,
        )

    @Slot(object)
    def _on_album_selected(self, data) -> None:
        if data == _SPECIAL_ALL:
            self._show_all_photos()
        elif data == _SPECIAL_FAV:
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(self._catalog.get_favorites, "Favoris")
        elif data == _SPECIAL_VIDEOS:
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(self._catalog.get_videos, "Vidéos")
        elif data == _SPECIAL_RATED:
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(
                lambda: self._catalog.get_photos_min_rating(1), "Notées"
            )
        elif data == _SPECIAL_FILENAME:
            query = self._sidebar.filter_text
            if not query:
                return
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(
                lambda q=query: self._catalog.search(q),
                f"Fichiers : {query}",
            )
        elif data == _SPECIAL_TAG:
            query = self._sidebar.filter_text
            if not query:
                return
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(
                lambda q=query: self._catalog.get_photos_by_tag(q),
                f"Mot-clé : {query}",
            )
        elif isinstance(data, AlbumInfo) and data.id:
            album_id   = data.id
            album_name = data.name
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(
                lambda: self._catalog.get_photos_in_album(album_id),
                album_name,
                album_id,
            )

    @Slot(str)
    def _on_scan_requested(self, folder: str) -> None:
        self._start_scan([folder])

    @Slot(str)
    def _on_watcher_files_changed(self, path: str) -> None:
        logger.debug("Watcher : changement détecté dans %s", path)
        self._start_scan([path])

    @Slot(str)
    def _on_folder_removed(self, folder: str) -> None:
        folder = os.path.normpath(folder)
        count = self._catalog.count_photos_in_folder(folder)
        if count:
            reply = QMessageBox.question(
                self, "Retirer le dossier",
                f"Retirer «{folder}» de la surveillance ?\n\n"
                f"<b>{count:,}</b> photo(s) seront supprimées du catalogue, ainsi que "
                "les vignettes et les visages associés. Les fichiers restent intacts sur le disque.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self._purge_catalog_for_folder(folder)
            self._grid.set_photos(self._current_photos)
            self._duplicate_grid.invalidate()
            self._sidebar.update_duplicates_badge(self._catalog.count_duplicate_groups())

        self._config.remove_scan_folder(folder)
        remaining = self._config.get_scan_folders()
        self._sidebar.refresh_folders(remaining)
        self._folder_watcher.set_folders(remaining)

    @Slot(list, str)
    def _on_photos_dropped(self, file_paths: list, dest_folder: str) -> None:
        """Déplace les fichiers glissés vers dest_folder et met à jour toutes les références."""
        # Déclarer les déplacements au watcher AVANT de toucher au disque :
        # toutes les références (catalogue, vignettes, visages, grille) sont
        # mises à jour ici même, le rescan que déclencherait sinon le watcher
        # serait purement redondant.
        self._folder_watcher.notify_self_deletions(file_paths)
        self._folder_watcher.notify_self_additions(
            [os.path.join(dest_folder, os.path.basename(p)) for p in file_paths]
        )
        moved_old: list[str] = []
        errors:    list[str] = []
        for src in file_paths:
            filename = os.path.basename(src)
            dst = os.path.normpath(os.path.join(dest_folder, filename))
            if os.path.normcase(dst) == os.path.normcase(src):
                continue
            if os.path.exists(dst):
                errors.append(f"{filename} : existe déjà dans la destination")
                continue
            try:
                shutil.move(src, dst)
            except Exception as e:
                errors.append(f"{filename} : {e}")
                continue
            try:
                self._catalog.move_photo(src, dst)
                self._edit_db.rename_photo(src, dst)
                self._thumb_cache.move_photo(src, dst)
                self._face_db.update_path(src, dst)
            except Exception as e:
                logger.error("Erreur mise à jour références %s → %s : %s", src, dst, e)
            moved_old.append(src)

        if moved_old:
            # Naviguer vers le dossier destination pour montrer les photos déplacées
            photos = self._sort_photos_for_display(
                self._catalog.get_photos_in_folder(dest_folder), dest_folder
            )
            self._current_photos = photos
            self._current_paths  = {p.path for p in photos}
            self._current_context = dest_folder
            self._grid.set_photos(photos)
            self._update_status()

        if errors:
            QMessageBox.warning(self, "Erreurs lors du déplacement",
                                "\n".join(errors))

    @Slot(str)
    def _on_folder_created(self, path: str) -> None:
        """Nouveau sous-dossier créé sur disque : rafraîchir l'arbre et scanner."""
        self._sidebar.refresh_folders(self._config.get_scan_folders())
        self._start_scan([path])

    @Slot(str, str)
    def _on_folder_moved(self, old_path: str, new_path: str) -> None:
        """Dossier renommé ou déplacé : mettre à jour catalogue, config et UI."""
        # Normaliser pour garantir la cohérence des chemins Windows
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        # Catalogue + visages
        self._catalog.update_paths_prefix(old_path, new_path)
        self._face_db.update_paths_prefix(old_path, new_path)
        # Config : remplacer les dossiers surveillés concernés
        for folder in list(self._config.get_scan_folders()):
            if folder == old_path or folder.startswith(old_path + os.sep):
                updated = new_path + folder[len(old_path):]
                self._config.remove_scan_folder(folder)
                self._config.add_scan_folder(updated)
        # Photos en mémoire
        n = len(old_path)
        for photo in self._current_photos:
            if photo.path == old_path or photo.path.startswith(old_path + os.sep):
                photo.path      = new_path + photo.path[n:]
                photo.directory = new_path + photo.directory[n:]
        # Contexte actif
        if self._current_context and (
            self._current_context == old_path
            or self._current_context.startswith(old_path + os.sep)
        ):
            self._current_context = new_path + self._current_context[n:]
        # Rafraîchir sidebar, grille et watcher
        updated_folders = self._config.get_scan_folders()
        self._sidebar.refresh_folders(updated_folders)
        self._folder_watcher.set_folders(updated_folders)
        self._grid.set_photos(self._current_photos)
        self._update_status()

    # ------------------------------------------------------------------ doublons

    @Slot(str)
    def _navigate_to_photo_path(self, path: str) -> None:
        results = self._catalog.get_photos_by_paths([path])
        if not results:
            QMessageBox.warning(self, "Photo introuvable",
                                f"La photo n'est plus dans la bibliothèque :\n{path}")
            return
        photo = results[0]
        folder = photo.directory
        self._on_folder_selected(folder)
        QTimer.singleShot(350, lambda: (
            self._grid.scroll_to_photo(path),
            self._grid.select_photo(path),
        ))

    def _purge_catalog_for_folder(self, folder: str) -> list[str]:
        """Supprime du catalogue, du cache de vignettes et de la base de visages
        toutes les photos d'un dossier (et de ses sous-dossiers).
        Retourne les chemins supprimés."""
        photos = self._catalog.get_photos_in_folder(folder)
        # Inclure les sous-dossiers via le catalogue
        all_paths = [p.path for p in photos
                     if p.path == folder or p.path.startswith(folder + os.sep)
                     or os.path.normpath(p.directory) == folder
                     or os.path.normpath(p.directory).startswith(folder + os.sep)]
        if all_paths:
            self._catalog.delete_photos(all_paths)
            for path in all_paths:
                self._thumb_cache.invalidate(path)
                self._face_db.delete_for_path(path)
            self._remove_persisted_corrupted_paths(all_paths)
            self._update_corrupted_indicator(
                [p for p in self._live_corrupted_paths if p not in set(all_paths)]
            )

            deleted_set = set(all_paths)
            self._current_photos = [p for p in self._current_photos
                                     if p.path not in deleted_set]
            self._current_paths -= deleted_set
        return all_paths

    @Slot(str)
    def _on_folder_deleted(self, folder: str) -> None:
        """Dossier supprimé du disque : nettoyer catalogue, caches et UI."""
        folder = os.path.normpath(folder)
        deleted_paths = self._purge_catalog_for_folder(folder)
        self._duplicate_grid.invalidate()
        self._sidebar.update_duplicates_badge(self._catalog.count_duplicate_groups())

        # Retirer de la config si c'était un dossier surveillé
        for watched in list(self._config.get_scan_folders()):
            if watched == folder or watched.startswith(folder + os.sep):
                self._config.remove_scan_folder(watched)

        # Si le contexte actif était dans le dossier supprimé, revenir à la grille vide
        if self._current_context and (
            self._current_context == folder
            or self._current_context.startswith(folder + os.sep)
        ):
            self._current_context = ""
            self.show_grid()
            self._grid.set_photos([])
        else:
            self._grid.remove_photos(deleted_paths)

        updated_folders = self._config.get_scan_folders()
        self._sidebar.refresh_folders(updated_folders)
        self._folder_watcher.set_folders(updated_folders)
        self._update_status()

    def _on_album_create(self, name: str) -> None:
        album = self._catalog.create_album(name)
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)

    @Slot(object)
    def _on_album_delete_requested(self, album: AlbumInfo) -> None:
        reply = QMessageBox.question(
            self, "Supprimer l'album",
            f"Supprimer l'album «{album.name}» ({album.photo_count} photo(s)) ?\n\n"
            "Les photos restent intactes dans le catalogue et sur le disque ; "
            "seul l'album est supprimé.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._catalog.delete_album(album.id)
        if self._current_context == album.name:
            self._sidebar.select_album_item(_SPECIAL_ALL)
            self._show_all_photos()
        self._sidebar.refresh_albums(self._catalog.get_albums())

    def _on_add_to_album(self, photos: list) -> None:
        albums = self._catalog.get_albums()
        if not albums:
            QMessageBox.information(
                self, "Ajouter à un album",
                "Aucun album existant.\nCréez d'abord un album via le panneau Albums."
            )
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Ajouter à un album")
        dlg.setMinimumWidth(320)
        layout = QVBoxLayout(dlg)
        n = len(photos)
        layout.addWidget(QLabel(f"Choisissez l'album pour {n} photo(s) :"))
        lst = QListWidget(dlg)
        for album in albums:
            lst.addItem(f"{album.name}  ({album.photo_count} photo(s))")
        lst.setCurrentRow(0)
        lst.itemDoubleClicked.connect(dlg.accept)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(lst)
        layout.addWidget(btns)
        if dlg.exec() != QDialog.Accepted or lst.currentRow() < 0:
            return
        album = albums[lst.currentRow()]
        added = self._catalog.add_photos_to_album(
            album.id, [p.id for p in photos if p.id is not None]
        )
        self._sidebar.refresh_albums(self._catalog.get_albums())
        self.statusBar().showMessage(
            f"{added} photo(s) ajoutée(s) à « {album.name} »", 4000
        )

    def _on_create_album_with(self, photos: list) -> None:
        n = len(photos)
        name, ok = QInputDialog.getText(
            self, "Nouvel album",
            f"Nom du nouvel album ({n} photo(s) sélectionnée(s)) :"
        )
        if not ok or not name.strip():
            return
        album = self._catalog.create_album(name.strip())
        added = self._catalog.add_photos_to_album(
            album.id, [p.id for p in photos if p.id is not None]
        )
        self._sidebar.refresh_albums(self._catalog.get_albums())
        self.statusBar().showMessage(
            f"Album « {name.strip()} » créé avec {added} photo(s)", 4000
        )

    def _on_bus_photo_selected(self, photo: PhotoInfo) -> None:
        pass

    @Slot(int)
    def _on_thumb_size_changed(self, idx: int) -> None:
        size = _THUMB_SIZES[idx]
        self._grid.set_thumbnail_size(size)
        self._config.set("thumbnail_size", size)

    # ------------------------------------------------------------------ public

    def show_grid(self) -> None:
        self._stack.setCurrentIndex(0)
        self._left_stack.setCurrentIndex(0)
        self._lbl_thumb_size.show()
        self._thumb_slider.show()
        self._lbl_zoom.hide()
        self._zoom_slider.hide()
        self._zoom_pct_label.hide()
        self._btn_grid_status.show()
        self._act_faces_toggle.setVisible(False)
        self._act_exif_toggle.setVisible(False)
        self._btn_annotations_toggle.setVisible(False)
        self._lbl_fileinfo.setText("")
        self._update_status()

    def _on_viewer_closed(self) -> None:
        """Bouton ← dans la visionneuse : retourne à l'écran d'origine."""
        target = self._viewer_back_target
        self._viewer_back_target = "grid"
        if target == "person_cluster_view":
            person = self._person_cluster_view.current_person
            if person:
                self.show_person_clusters(person)
                return
        elif target == "duplicate_grid":
            self.show_duplicate_grid()
            return
        self.show_grid()

    def show_viewer(self, photo: PhotoInfo) -> None:
        is_video = photo.media_type == "video"
        self._viewer.set_album_context(self._current_album_id)
        self._viewer.set_photo(photo)
        if not is_video:
            self._edit_panel.set_photo(photo)
        self._stack.setCurrentIndex(1)
        self._left_stack.setCurrentIndex(0 if is_video else 1)
        self._viewer.setFocus()
        self._lbl_thumb_size.hide()
        self._thumb_slider.hide()
        self._lbl_zoom.show()
        self._zoom_slider.show()
        self._zoom_pct_label.show()
        self._btn_grid_status.hide()
        self._act_faces_toggle.setVisible(True)
        self._act_exif_toggle.setVisible(True)
        self._btn_annotations_toggle.setVisible(True)
        # Un nouveau _Canvas ne connaît pas spontanément l'état de session.
        self._viewer.set_annotations_visible(self._annotations_globally_visible)
        if self._face_panel.isVisible():
            self._face_panel.set_photo(photo.path)
        if self._exif_panel.isVisible():
            self._exif_panel.set_photo(photo.path)
            self._exif_panel.set_tags(photo.tags)
        self._update_viewer_status(photo)
        self._update_nav_arrows()
        self._prefetch_viewer_neighbors()

    def toggle_sidebar(self) -> None:
        self._left_stack.setVisible(not self._left_stack.isVisible())

    def open_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir un dossier de photos", os.path.expanduser("~")
        )
        if folder:
            self._config.add_scan_folder(folder)
            all_folders = self._config.get_scan_folders()
            self._sidebar.refresh_folders(all_folders)
            self._start_scan([folder])
            self._folder_watcher.set_folders(all_folders)

    # ------------------------------------------------------------------ private

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _start_slideshow(self) -> None:
        if not self._current_photos:
            return
        from src.ui.slideshow import SlideshowWindow
        # Priorité : visionneuse ouverte → photo affichée
        #            mode chronologie    → photo au centre du ruban
        #            sinon              → photo la plus ancienne
        if self._viewer.isVisible() and 0 <= self._current_photo_index < len(self._current_photos):
            start_index = self._current_photo_index
        elif (ribbon_center := self._grid.center_photo_index()) is not None:
            start_index = ribbon_center
        else:
            start_index = len(self._current_photos) - 1
        self._slideshow_win = SlideshowWindow(
            photos=self._current_photos,
            start_index=start_index,
            edit_db=self._edit_db,
        )

    def _show_help(self) -> None:
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog(self)
        dlg.exec()

    def _show_about(self) -> None:
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog(self, tab="À propos")
        dlg.exec()

    @Slot(str, object)
    def _on_photo_saved(self, photo_path: str, edit) -> None:
        self._grid.refresh_photo(photo_path, edit)

    @Slot(str)
    def _on_exif_photo_saved(self, photo_path: str) -> None:
        """Mise à jour du catalogue après modification EXIF (date_taken peut avoir changé)."""
        # Fichier réécrit sur disque : oublier l'image de base en cache du viewer.
        self._viewer.invalidate_base_cache(photo_path)
        for photo in self._current_photos:
            if photo.path == photo_path:
                try:
                    from PIL import Image
                    with Image.open(photo_path) as img:
                        exif_ifd = img.getexif().get_ifd(0x8769)
                        dt_raw = exif_ifd.get(0x9003) or img.getexif().get(0x0132)
                        if dt_raw:
                            photo.date_taken = datetime.strptime(str(dt_raw), "%Y:%m:%d %H:%M:%S")
                except Exception:
                    pass
                self._catalog.add_or_update_photo(photo)
                break

    def _on_rotation_stepped(self, photo_path: str, rotation: int) -> None:
        """Re-détecte les visages après une rotation 90° de l'utilisateur."""
        from src.faces.detector import is_available
        if not is_available():
            return
        if self._reindex_thread and self._reindex_thread.isRunning():
            return
        if self._reindex_thread is not None:
            self._reindex_thread.deleteLater()
        self._reindex_thread = SingleFaceReindexThread(
            self._face_db, photo_path, rotation, self
        )
        self._reindex_thread.finished.connect(self._on_single_reindex_finished)
        self._reindex_thread.cluster_requested.connect(self._run_clustering)
        self._reindex_thread.start()

    @Slot(list)
    def _on_delete_requested(self, photos: list) -> None:
        if not photos:
            return

        if not self._config.get("ui.delete_no_confirm", False):
            n = len(photos)
            if n == 1:
                msg = (f"Envoyer « {photos[0].filename} » à la corbeille Windows ?\n\n"
                       f"Le fichier restera récupérable depuis la corbeille.")
            else:
                msg = (f"Envoyer les {n} fichiers sélectionnés à la corbeille Windows ?\n\n"
                       f"Ils resteront récupérables depuis la corbeille.")
            box = QMessageBox(QMessageBox.Warning, "Confirmer la suppression", msg,
                              QMessageBox.Yes | QMessageBox.Cancel, self)
            box.setDefaultButton(QMessageBox.Cancel)
            chk = QCheckBox("Ne plus demander de confirmation")
            box.setCheckBox(chk)
            if box.exec() != QMessageBox.Yes:
                return
            if chk.isChecked():
                self._config.set("ui.delete_no_confirm", True)

        # Un seul worker de suppression à la fois : deux workers entrelacés
        # rendraient l'épilogue (grille, groupes de doublons) incohérent. La
        # suppression est déjà confirmée à ce stade : la mettre en file plutôt
        # que de l'abandonner silencieusement (cf. _pending_deletes en mémoire —
        # un worker peut rester `isRunning()` plusieurs secondes, notamment sur
        # `FaceDatabase.delete_for_paths` en cas de contention SQLite passagère,
        # largement plus long que le message de statut furtif qui avertissait
        # l'utilisateur avant ce correctif).
        if self._delete_thread is not None and self._delete_thread.isRunning():
            self._pending_deletes.append(photos)
            self.statusBar().showMessage("Suppression mise en file d'attente…", 3000)
            return

        self._start_delete_worker(photos)

    def _start_delete_worker(self, photos: list) -> None:
        # Mémoriser l'état du viewer avant la suppression
        in_viewer = self._stack.currentIndex() == 1
        viewed_index = self._current_photo_index
        deleted_paths_set = {p.path for p in photos}

        # Index du premier fichier supprimé (pour recentrer le ruban après)
        first_deleted_idx = next(
            (i for i, p in enumerate(self._current_photos) if p.path in deleted_paths_set),
            None,
        )

        # Groupes de doublons concernés par cette suppression : si la suppression
        # fait passer un groupe sous 2 exemplaires, ce n'est plus un doublon.
        affected_groups = {p.duplicate_group_id for p in photos if p.duplicate_group_id is not None}

        # Déclarer les suppressions au watcher AVANT de toucher au disque : le
        # rescan qu'il déclencherait sinon (debounce 400 ms) referait en pur
        # gaspillage tout ce que l'épilogue ci-dessous met déjà à jour.
        self._folder_watcher.notify_self_deletions([p.path for p in photos])

        # Unlink + purge catalogue/vignettes/visages dans un thread : la boucle
        # synchrone gelait l'UI plusieurs secondes sur une multi-sélection.
        worker = _DeleteWorkerThread(
            [p.path for p in photos], self._catalog, self._thumb_cache,
            self._face_db, self,
        )
        self._delete_thread = worker
        worker.progress.connect(
            lambda done, total: self._lbl_action.setText(f"Suppression… {done}/{total}")
        )
        worker.finished_delete.connect(
            lambda deleted, errors: self._on_delete_finished(
                deleted, errors, in_viewer, viewed_index,
                first_deleted_idx, affected_groups,
            )
        )
        worker.start()

    def _on_delete_finished(self, deleted: list, errors: list, in_viewer: bool,
                            viewed_index: int, first_deleted_idx,
                            affected_groups: set) -> None:
        """Épilogue UI d'une suppression exécutée par _DeleteWorkerThread :
        mise à jour grille/albums/groupes de doublons et navigation voisin.
        Reste sur le thread UI — il touche _duplicate_ignored_paths et l'état
        des widgets."""
        self._lbl_action.setText("")
        deleted_paths_set = set(deleted)
        if deleted:
            self._grid.remove_photos(deleted)
            deleted_set = set(deleted)
            self._current_photos = [p for p in self._current_photos
                                    if p.path not in deleted_set]
            self._current_paths -= deleted_set
            self._update_status()
            # Rafraîchir immédiatement les compteurs d'albums : le watcher
            # n'émettra plus pour cette suppression (notify_self_deletions).
            self._sidebar.refresh_albums(self._catalog.get_albums())

            # Dissoudre les groupes de doublons devenus des singletons (ou vides)
            # suite à cette suppression : sinon la carte reste affichée dans
            # DuplicateGrid pour un groupe qui n'a plus lieu d'être.
            stale_groups = []
            for gid in affected_groups:
                remaining = self._catalog.get_duplicates_for_group(gid)
                if len(remaining) < 2:
                    # Même piège que le bouton ✗ (cf. _on_duplicate_group_ignored) :
                    # un passage de détection en cours peut encore fusionner ce
                    # groupe en mémoire depuis avant la suppression.
                    self._duplicate_ignored_paths |= {p.path for p in remaining}
                    self._catalog.ignore_duplicate_group(gid)
                    self._duplicate_grid.remove_group(gid)
                    stale_groups.append(gid)
            if stale_groups:
                for p in self._current_photos:
                    if p.duplicate_group_id in stale_groups:
                        p.duplicate_group_id = None
                self._sidebar.update_duplicates_badge(self._catalog.count_duplicate_groups())
                grid_assignments = {p.path: None for p in self._current_photos
                                    if p.duplicate_group_id is None}
                if grid_assignments:
                    self._grid.refresh_duplicate_status(grid_assignments)
                # Filet de sécurité : force un rechargement depuis le catalogue au
                # prochain affichage de la grille des doublons, même si
                # remove_group() a déjà mis les cartes à jour en mémoire.
                self._duplicate_grid.invalidate()

            # Si le viewer affichait une photo supprimée, naviguer vers le voisin
            if in_viewer and any(p in deleted_paths_set
                                 for p in [self._viewer.current_photo().path]
                                 if self._viewer.current_photo()):
                # Comparaison de doublons réduite à 0 ou 1 exemplaire : elle n'a
                # plus lieu d'être, retour automatique à la grille des doublons
                # plutôt que de continuer à afficher le seul exemplaire restant.
                if self._viewer_back_target == "duplicate_grid" and len(self._current_photos) <= 1:
                    self._viewer_back_target = "grid"
                    self.show_duplicate_grid()
                elif not self._current_photos:
                    self.show_grid()
                else:
                    new_index = min(viewed_index, len(self._current_photos) - 1)
                    self._current_photo_index = new_index
                    self.show_viewer(self._current_photos[new_index])
            elif not in_viewer and self._current_photos and first_deleted_idx is not None:
                neighbor_idx = min(first_deleted_idx, len(self._current_photos) - 1)
                neighbor_path = self._current_photos[neighbor_idx].path
                self._grid.scroll_to_photo(neighbor_path)
                self._grid.select_photo(neighbor_path)

        if errors:
            QMessageBox.warning(self, "Erreurs de suppression",
                                "Impossible de supprimer :\n" + "\n".join(errors))

        if self._pending_deletes:
            self._start_delete_worker(self._pending_deletes.pop(0))

    def _on_favorite_toggle_requested(self, photo: PhotoInfo) -> None:
        """Persiste la bascule favori demandée par la grille (menu contextuel)
        ou la visionneuse (bouton ★ / menu contextuel) — les deux émetteurs
        basculent photo.is_favorite avant d'émettre, ce handler n'a qu'à écrire
        l'état déjà à jour."""
        if photo.id is not None:
            self._catalog.set_favorite(photo.id, photo.is_favorite)

    def _on_rating_change_requested(self, photos: list, rating: int) -> None:
        """Persiste la note demandée par la grille (menu contextuel, éventuellement
        multi-sélection) ou la visionneuse (étoiles / clavier 0-5 / menu contextuel),
        puis rafraîchit les badges de la grille — la grille et la visionneuse ne
        partagent pas forcément la même instance de PhotoInfo pour un chemin donné."""
        ids = [p.id for p in photos if p.id is not None]
        if ids:
            self._catalog.set_rating_for_ids(ids, rating)
        self._grid.refresh_rating({p.path: rating for p in photos})

    def _on_edit_tags_requested(self, photos: list) -> None:
        """Ouvre le dialogue d'édition des mots-clés pour la sélection (menu
        contextuel grille ou visionneuse) — précharge la liste des tags déjà
        connus du catalogue dans un thread avant l'ouverture (pattern
        _AssignPrepLoader de face_panel.py), pour ne jamais bloquer l'UI le
        temps de la requête."""
        if not photos:
            return
        QApplication.setOverrideCursor(Qt.BusyCursor)
        t = TagsPrepLoader(self._catalog, self)
        t.ready.connect(lambda all_tags, photos=photos: self._continue_edit_tags(photos, all_tags))
        t.finished.connect(t.deleteLater)
        t.start()

    def _continue_edit_tags(self, photos: list, all_tags: list) -> None:
        QApplication.restoreOverrideCursor()
        dlg = TagEditDialog(photos, all_tags, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        to_add, to_remove = dlg.result_add_remove()
        ids = [p.id for p in photos if p.id is not None]
        if not ids:
            return
        if to_add:
            self._catalog.add_tags_to_photos(ids, to_add)
        for tag in to_remove:
            self._catalog.remove_tag_from_photos(ids, tag)

        for p in photos:
            merged = list(p.tags)
            for t in to_add:
                if t not in merged:
                    merged.append(t)
            p.tags = [t for t in merged if t not in to_remove]

        current = self._viewer.current_photo()
        if current is not None and self._exif_panel.isVisible():
            match = next((p for p in photos if p.path == current.path), None)
            if match is not None:
                current.tags = match.tags
                self._exif_panel.set_tags(current.tags)

    def _open_advanced_search(self) -> None:
        """Ouvre le dialogue de recherche avancée (menu Fichier › Recherche
        avancée…, Ctrl+F, ou bouton loupe de la sidebar) — précharge appareils/
        personnes/tags dans un thread avant l'ouverture (pattern
        _AssignPrepLoader de face_panel.py), pour ne jamais bloquer l'UI."""
        QApplication.setOverrideCursor(Qt.BusyCursor)
        t = AdvancedSearchPrepLoader(self._catalog, self)
        t.ready.connect(self._continue_advanced_search)
        t.finished.connect(t.deleteLater)
        t.start()

    def _continue_advanced_search(self, cameras: list, persons: list, all_tags: list) -> None:
        QApplication.restoreOverrideCursor()
        folders = self._config.get_scan_folders()
        dlg = AdvancedSearchDialog(cameras, persons, all_tags, folders, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        criteria = dlg.get_criteria()
        person_id = dlg.get_person_id()
        self._grid.set_ribbon_mode(False)
        self._grid.set_date_overlay_visible(False)
        self._grid_nav_bar.hide()
        self.show_grid()
        self._start_photo_query(
            lambda c=criteria, pid=person_id: self._run_advanced_search(c, pid),
            "Recherche avancée",
        )

    def _run_advanced_search(self, criteria: dict, person_id: "int | None") -> list:
        """Exécute la recherche avancée (sur le thread de _PhotoQueryThread) :
        critères SQL via Catalog.search_advanced(), puis intersection Python
        avec les photos de la personne sélectionnée — catalog.db et faces.db
        sont deux bases séparées, sans JOIN possible entre elles (cf.
        CLAUDE.md), d'où cette intersection côté appelant plutôt qu'en SQL."""
        photos = self._catalog.search_advanced(criteria)
        if person_id is not None:
            person_paths = set(self._face_db.get_photos_for_person(person_id))
            photos = [p for p in photos if p.path in person_paths]
        return photos

    def _on_remove_from_album_requested(self, photos: list) -> None:
        """Retire les photos de l'album affiché (touche Del / menu contextuel en
        contexte album, grille ou visionneuse) : ne touche ni au fichier disque
        ni à la photo elle-même, contrairement à _on_delete_requested."""
        if not photos or self._current_album_id is None:
            return

        album_id = self._current_album_id
        self._catalog.remove_photos_from_album(
            album_id, [p.id for p in photos if p.id is not None]
        )

        in_viewer = self._stack.currentIndex() == 1
        viewed_index = self._current_photo_index
        removed_set = {p.path for p in photos}
        current_viewed = self._viewer.current_photo()

        first_removed_idx = next(
            (i for i, p in enumerate(self._current_photos) if p.path in removed_set),
            None,
        )

        self._grid.remove_photos(list(removed_set))
        self._current_photos = [p for p in self._current_photos if p.path not in removed_set]
        self._current_paths -= removed_set
        self._update_status()
        self._sidebar.refresh_albums(self._catalog.get_albums())

        # Si la visionneuse affichait une photo retirée, naviguer vers le voisin
        # (même logique que _on_delete_requested).
        if in_viewer and current_viewed and current_viewed.path in removed_set:
            if not self._current_photos:
                self.show_grid()
            else:
                new_index = min(viewed_index, len(self._current_photos) - 1)
                self._current_photo_index = new_index
                self.show_viewer(self._current_photos[new_index])
        elif not in_viewer and self._current_photos and first_removed_idx is not None:
            neighbor_idx = min(first_removed_idx, len(self._current_photos) - 1)
            neighbor_path = self._current_photos[neighbor_idx].path
            self._grid.scroll_to_photo(neighbor_path)
            self._grid.select_photo(neighbor_path)

    def _update_viewer_status(self, photo: PhotoInfo) -> None:
        size_str = _fmt_size(photo.file_size)
        text = photo.filename
        if size_str:
            text += f"   —   {size_str}"
        self._lbl_fileinfo.setText(text)

    def _update_status(self, selection: list[PhotoInfo] | None = None) -> None:
        if selection is None:
            selection = self._grid.get_selected()

        n_sel = len(selection)
        n_total = len(self._current_photos)

        if n_sel == 1:
            photo = selection[0]
            size_str = _fmt_size(photo.file_size)
            text = photo.filename
            if size_str:
                text += f"   —   {size_str}"
            self._lbl_fileinfo.setText(text)
        elif n_sel > 1:
            self._lbl_fileinfo.setText(
                f"{n_sel} photos sélectionnées  —  {n_total} au total"
            )
        else:
            count_str = f"{n_total} photo{'s' if n_total != 1 else ''}"
            if self._current_context:
                self._lbl_fileinfo.setText(f"{self._current_context}  —  {count_str}")
            else:
                self._lbl_fileinfo.setText(count_str)

    @Slot(object)
    def _on_rename_requested(self, photo: PhotoInfo) -> None:
        old_p = Path(photo.path)
        new_stem, ok = QInputDialog.getText(
            self,
            "Renommer l'image",
            "Nouveau nom :",
            text=old_p.stem,
        )
        if not ok:
            return
        new_stem = new_stem.strip()
        if not new_stem:
            return

        forbidden = set('\\/:*?"<>|')
        if any(c in forbidden for c in new_stem):
            QMessageBox.warning(
                self, "Nom invalide",
                "Le nom ne peut pas contenir les caractères : \\ / : * ? \" < > |",
            )
            return

        new_p = old_p.parent / (new_stem + old_p.suffix)
        if new_p == old_p:
            return
        if new_p.exists():
            QMessageBox.warning(
                self, "Fichier existant",
                f"Un fichier nommé « {new_p.name} » existe déjà dans ce dossier.",
            )
            return

        try:
            old_p.rename(new_p)
        except OSError as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de renommer le fichier :\n{e}")
            return

        old_path_str = photo.path
        new_path_str = os.path.normpath(str(new_p))
        self._catalog.rename_photo(old_path_str, new_path_str)
        self._edit_db.rename_photo(old_path_str, new_path_str)
        self._face_db.update_path(old_path_str, new_path_str)
        self._grid.update_photo_path(old_path_str, new_path_str)

        for p in self._current_photos:
            if p.path == photo.path:
                p.path = new_path_str
                p.filename = new_p.name
                break

        self._viewer.refresh_name()
        self._update_status()

    @Slot(object)
    def _on_move_requested(self, photo: PhotoInfo) -> None:
        old_p = Path(photo.path)
        dest_dir = QFileDialog.getExistingDirectory(
            self,
            "Déplacer vers…",
            str(old_p.parent),
        )
        if not dest_dir:
            return
        dest_dir_p = Path(dest_dir)
        if dest_dir_p.resolve() == old_p.parent.resolve():
            return
        new_p = dest_dir_p / old_p.name
        if new_p.exists():
            QMessageBox.warning(
                self, "Fichier existant",
                f"Un fichier nommé « {old_p.name} » existe déjà dans ce dossier.",
            )
            return

        try:
            shutil.move(str(old_p), str(new_p))
        except OSError as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de déplacer le fichier :\n{e}")
            return

        old_path_str = photo.path
        new_path_str = os.path.normpath(str(new_p))
        self._catalog.rename_photo(old_path_str, new_path_str)
        self._edit_db.rename_photo(old_path_str, new_path_str)
        self._face_db.update_path(old_path_str, new_path_str)
        self._thumb_cache.invalidate(old_path_str)

        # La photo n'est plus dans le dossier courant : on la retire de la grille
        in_viewer = self._stack.currentIndex() == 1
        viewed_index = self._current_photo_index
        self._grid.remove_photos([old_path_str])
        self._current_photos = [p for p in self._current_photos if p.path != old_path_str]
        self._current_paths.discard(old_path_str)
        self._update_status()

        if in_viewer and self._viewer.current_photo() and self._viewer.current_photo().path == old_path_str:
            if not self._current_photos:
                self.show_grid()
            else:
                new_index = min(viewed_index, len(self._current_photos) - 1)
                self._current_photo_index = new_index
                self.show_viewer(self._current_photos[new_index])

    @Slot(object)
    def _on_save_requested(self, photo: PhotoInfo) -> None:
        dlg = _SaveOptionsDialog(photo.path, self)
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.overwrite:
            # Sauvegarde optionnelle de l'original avant écrasement
            if dlg.backup_before_overwrite:
                try:
                    self._backup_original(photo.path)
                except Exception as e:
                    logger.error("Échec sauvegarde original %s : %s",
                                 photo.path, e, exc_info=True)
                    answer = QMessageBox.warning(
                        self, "Échec de la sauvegarde",
                        f"Impossible de copier l'original dans .tmp_originals :\n{e}\n\n"
                        "Voulez-vous quand même écraser le fichier original ?",
                        QMessageBox.Yes | QMessageBox.Cancel,
                        QMessageBox.Cancel,
                    )
                    if answer != QMessageBox.Yes:
                        return
            dest = photo.path
        else:
            original = Path(photo.path)
            suggested = original.parent / (original.stem + "_retouché" + original.suffix)
            dest, _ = QFileDialog.getSaveFileName(
                self,
                "Enregistrer l'image traitée",
                str(suggested),
                "JPEG (*.jpg *.jpeg);;PNG (*.png);;Tous les fichiers (*)",
            )
            if not dest:
                return

        self._export_image(photo, dest)

    def _backup_original(self, photo_path: str) -> None:
        """Copie le fichier original dans .tmp_originals (dossier caché) avec horodatage."""
        original = Path(photo_path)
        backup_dir = original.parent / ".tmp_originals"
        backup_dir.mkdir(exist_ok=True)

        # Rendre le dossier caché sur Windows
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(backup_dir), 0x02)
        except Exception:
            pass  # non bloquant sur les systèmes non-Windows

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{original.stem}_{ts}{original.suffix}"
        shutil.copy2(photo_path, backup_dir / backup_name)
        logger.info("Original sauvegardé : %s", backup_dir / backup_name)

    def _export_image(self, photo: PhotoInfo, dest: str) -> None:
        """Exporte l'image traitée pleine résolution vers dest."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            from PIL import Image, ImageOps
            from src.processing.adjustments import ImageAdjuster
            from src.ui.annotation_renderer import composite_annotations_pil

            orig_stat = os.stat(photo.path)

            edit = self._edit_db.load(photo.path)
            with Image.open(photo.path) as img:
                img = ImageOps.exif_transpose(img)
                if edit.is_modified():
                    img = ImageAdjuster.apply_all(img, edit)
                if self._annotations_globally_visible and edit.annotations:
                    img = composite_annotations_pil(img, edit.annotations)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                if Path(dest).suffix.lower() == ".png":
                    img.save(dest, format="PNG")
                else:
                    if img.mode == "RGBA":
                        img = img.convert("RGB")
                    img.save(dest, format="JPEG", quality=95, subsampling=0)

            # Restaurer les dates du fichier original (atime, mtime et date de création)
            preserve_file_dates(orig_stat, dest)

            if os.path.normpath(dest) == os.path.normpath(photo.path):
                # Les retouches sont maintenant baked dans le fichier : supprimer l'edit
                # et rafraîchir l'UI pour éviter une double application au prochain chargement
                self._edit_db.delete(photo.path)
                self._thumb_cache.invalidate(photo.path)
                # Le fichier sur disque a changé : l'image de base en cache du
                # viewer ne correspond plus (elle montrerait la version sans
                # retouche alors qu'elles sont désormais baked dans le fichier).
                self._viewer.invalidate_base_cache(photo.path)
                self._viewer.update_edit(EditInfo())
                self._edit_panel.set_photo(photo)

            self._lbl_action.setText(f"Image sauvée : {Path(dest).name}")
            QTimer.singleShot(4000, lambda: self._lbl_action.setText(""))
        except Exception as e:
            logger.error("Erreur export image %s : %s", photo.path, e, exc_info=True)
            QMessageBox.critical(self, "Erreur d'export",
                                 f"Impossible de sauver l'image :\n{e}")
        finally:
            QApplication.restoreOverrideCursor()

    @Slot()
    def _on_export_clicked(self) -> None:
        if self._stack.currentIndex() == 1:   # mode visionneuse
            if not self._viewer._photo:
                return
            photos = [self._viewer._photo]
        else:                                  # mode grille
            photos = self._grid.get_selected()
            if not photos:
                QMessageBox.information(
                    self, "Exporter",
                    "Sélectionnez au moins une photo dans la grille avant d'exporter.",
                )
                return

        dlg = _ExportDialog(len(photos), self)
        if dlg.exec() != QDialog.Accepted:
            return

        self._run_export(photos, dlg.export_dir, *dlg.size_preset)

    def _run_export(
        self,
        photos: list,
        export_dir: Path,
        max_pixels: int | None,
        quality: int,
    ) -> None:
        """Exporte photos vers export_dir avec redimensionnement et qualité donnés."""
        from PIL import Image, ImageOps
        from src.processing.adjustments import ImageAdjuster
        from src.ui.annotation_renderer import composite_annotations_pil

        try:
            export_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de créer le dossier :\n{e}")
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        errors: list[str] = []
        try:
            for i, photo in enumerate(photos):
                self._lbl_action.setText(
                    f"Export {i + 1}/{len(photos)}  —  {photo.filename}"
                )
                QApplication.processEvents()
                try:
                    edit = self._edit_db.load(photo.path)
                    with Image.open(photo.path) as img:
                        img = ImageOps.exif_transpose(img)
                        if edit.is_modified():
                            img = ImageAdjuster.apply_all(img, edit)
                        # Redimensionnement si nécessaire
                        if max_pixels is not None:
                            w, h = img.size
                            if w * h > max_pixels:
                                scale = (max_pixels / (w * h)) ** 0.5
                                img = img.resize(
                                    (max(1, round(w * scale)), max(1, round(h * scale))),
                                    Image.LANCZOS,
                                )
                        if self._annotations_globally_visible and edit.annotations:
                            img = composite_annotations_pil(img, edit.annotations)
                        if img.mode not in ("RGB", "RGBA"):
                            img = img.convert("RGB")
                        if img.mode == "RGBA":
                            img = img.convert("RGB")
                        # Résolution du nom de fichier de destination (toujours en JPG)
                        dest = (export_dir / Path(photo.path).name).with_suffix(".jpg")
                        if dest.exists():
                            stem = dest.stem
                            n = 1
                            while dest.exists():
                                dest = export_dir / f"{stem}_{n}.jpg"
                                n += 1
                        orig_stat = os.stat(photo.path)
                        img.save(str(dest), format="JPEG",
                                 quality=quality, subsampling=0)
                        preserve_file_dates(orig_stat, str(dest))
                except Exception as e:
                    errors.append(f"{photo.filename} : {e}")
                    logger.error("Export %s : %s", photo.path, e, exc_info=True)
        finally:
            QApplication.restoreOverrideCursor()

        self._lbl_action.setText("")
        if errors:
            QMessageBox.warning(
                self, "Erreurs d'export",
                f"{len(errors)} fichier(s) non exporté(s) :\n" + "\n".join(errors),
            )
        else:
            n = len(photos)
            msg = (f"{n} photo{'s' if n > 1 else ''} "
                   f"exportée{'s' if n > 1 else ''}  →  {export_dir}")
            self._lbl_action.setText(msg)
            QTimer.singleShot(5000, lambda: self._lbl_action.setText(""))
            # PPM_SUPPRESS_EXPLORER=1 (posé par tools/test_env/launch_isolated.py) :
            # ouvrir l'Explorateur ici passerait devant la fenêtre de l'appli et
            # resterait ouvert après la fin du test (processus explorer.exe non
            # rattaché à l'appli, jamais fermé par terminate()), perturbant les
            # scénarios e2e suivants qui pilotent la fenêtre via UIA.
            if os.environ.get("PPM_SUPPRESS_EXPLORER") != "1":
                os.startfile(str(export_dir))

    def _restore_splitter_states(self) -> None:
        import base64
        from PySide6.QtCore import QByteArray

        def _apply(splitter, key: str) -> None:
            state_b64 = self._config.get(key, "")
            if state_b64:
                try:
                    splitter.restoreState(QByteArray(base64.b64decode(state_b64)))
                except Exception:
                    pass

        _apply(self._viewer_splitter, "ui.splitters.viewer")

        state_b64 = self._config.get("ui.splitters.sidebar_panels", "")
        if state_b64:
            self._sidebar.restore_splitter_state(state_b64)

        saved_person_id = self._config.get("ui.persons_list_selected_id", None)
        if saved_person_id is not None:
            self._sidebar.set_pending_person_id(int(saved_person_id))

    def _encode_view_state(self) -> dict:
        ctx = self._current_context
        if ctx == "Toutes les photos":
            return {"type": "all"}
        if ctx == "Favoris":
            return {"type": "favorites"}
        if ctx == "Vidéos":
            return {"type": "videos"}
        if ctx == "Notées":
            return {"type": "rated"}
        if ctx.startswith(f"{_PERSON_CTX_PREFIX}cluster_"):
            return {"type": "all"}   # vue transitoire, pas de restauration
        if ctx.startswith(_PERSON_CTX_PREFIX):
            try:
                return {"type": "person", "value": int(ctx[len(_PERSON_CTX_PREFIX):])}
            except ValueError:
                return {"type": "all"}
        if ctx.startswith("Fichiers : ") or ctx.startswith("Mot-clé : ") or ctx == "Recherche avancée":
            return {"type": "all"}   # filtre éphémère
        if ctx and os.path.isdir(ctx):
            return {"type": "folder", "value": ctx}
        if ctx:
            try:
                for album in self._catalog.get_albums():
                    if album.name == ctx:
                        return {"type": "album", "value": album.id}
            except Exception:
                pass
        return {"type": "all"}

    def closeEvent(self, event) -> None:
        self._config.set("ui.window_width", self.width())
        self._config.set("ui.window_height", self.height())
        self._config.set("ui.sidebar_width", self._left_stack.width())
        person_id = self._sidebar.get_selected_person_id()
        self._config.set("ui.persons_list_selected_id", person_id)
        self._config.set("ui.last_view", self._encode_view_state())
        import base64
        self._config.set(
            "ui.splitters.viewer",
            base64.b64encode(self._viewer_splitter.saveState().data()).decode(),
        )
        self._config.set(
            "ui.splitters.sidebar_panels",
            self._sidebar.save_splitter_state(),
        )
        # Confirmation AVANT tout signal d'arrêt : si l'utilisateur annule la
        # fermeture (ci-dessous), aucun thread d'arrière-plan ne doit avoir
        # été interrompu entre-temps.
        if self._cluster_thread and self._cluster_thread.isRunning():
            elapsed = int(time.monotonic() - self._cluster_start_time) if self._cluster_start_time else 0
            m, s = divmod(elapsed, 60)
            h, m = divmod(m, 60)
            if h:
                duree = f"{h}h{m:02d}min{s:02d}s"
            elif m:
                duree = f"{m}min{s:02d}s"
            else:
                duree = f"{s}s"
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Regroupement en cours")
            dlg.setIcon(QMessageBox.Icon.Warning)
            dlg.setText("<b>Un regroupement de visages est en cours.</b>")
            dlg.setInformativeText(
                f"Le regroupement tourne depuis <b>{duree}</b>.<br><br>"
                "Si vous fermez l'application maintenant, le calcul sera "
                "interrompu et <b>le résultat sera perdu</b>. "
                "Il faudra tout recommencer au prochain démarrage.<br><br>"
                "Voulez-vous quand même fermer l'application ?"
            )
            dlg.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            dlg.setDefaultButton(QMessageBox.StandardButton.No)
            dlg.button(QMessageBox.StandardButton.Yes).setText("Fermer quand même")
            dlg.button(QMessageBox.StandardButton.No).setText("Annuler")
            if dlg.exec() != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self._folder_watcher.set_folders([])

        # Signale l'arrêt à tous les threads d'arrière-plan avant d'attendre
        # quoi que ce soit : ils s'arrêtent ainsi en parallèle plutôt que
        # l'un après l'autre — l'ancienne attente séquentielle pouvait
        # cumuler plusieurs secondes par thread, jusqu'à une bonne minute
        # avec FaceIndexThread, qui pouvait rester bloqué jusqu'à
        # _DETECT_TIMEOUT/_WARMUP_TIMEOUT dans un appel bloquant sur son
        # subprocess avant même de remarquer la demande d'arrêt (corrigé
        # séparément : FaceIndexThread.stop() tue maintenant l'executor
        # tout de suite).
        self._scanner.request_stop()
        if self._face_indexer and self._face_indexer.isRunning():
            self._face_indexer.stop()
        if self._duplicate_thread and self._duplicate_thread.isRunning():
            self._duplicate_thread.cancel()

        # Masquer la fenêtre tout de suite : tout l'état utile est déjà
        # sauvegardé ci-dessus, donc rien n'empêche de rendre la fermeture
        # instantanée à l'écran pendant que les wait() ci-dessous (jusqu'à
        # ~10 s cumulés si un scan/détection est en cours) tournent en
        # arrière-plan, invisibles pour l'utilisateur.
        self.hide()

        self._scanner.wait_stopped(3000)
        if self._face_indexer and self._face_indexer.isRunning():
            self._face_indexer.wait(3000)
        if self._cluster_thread and self._cluster_thread.isRunning():
            self._cluster_thread.wait(500)
        if self._duplicate_thread and self._duplicate_thread.isRunning():
            self._duplicate_thread.wait(3000)
            if self._duplicate_thread.isRunning():
                # Un thread ORB peut rester bloqué au-delà du délai ci-dessus
                # (un seul appel cv2 en cours, ex. gros fichier sur un volume
                # réseau lent) malgré cancel() — un thread Python ne peut pas
                # être tué proprement de l'extérieur, et `sys.exit()`
                # attendrait quand même sa fin (atexit de ThreadPoolExecutor).
                # Sur demande explicite de l'utilisateur (l'appli mettait trop
                # de temps à se fermer) : on préfère tuer le process
                # immédiatement plutôt que de laisser l'appli traîner en
                # arrière-plan. Tout l'état utile (config, géométrie, dernière
                # vue) est déjà sauvegardé plus haut dans cette méthode.
                logger.warning(
                    "Détection de doublons : arrêt forcé du process, le "
                    "thread ne s'est pas arrêté à temps à la fermeture."
                )
                os._exit(0)
        if self._photo_query_thread and self._photo_query_thread.isRunning():
            self._photo_query_thread.wait(1000)
        if self._persons_refresh_thread and self._persons_refresh_thread.isRunning():
            self._persons_refresh_thread.wait(1000)
        if self._dup_migration_thread and self._dup_migration_thread.isRunning():
            self._dup_migration_thread.wait(2000)
        if self._delete_thread and self._delete_thread.isRunning():
            # Laisser la purge DB en lot se terminer : l'interrompre laisserait
            # des fichiers supprimés du disque mais encore présents au catalogue.
            self._delete_thread.wait(5000)
        # Fermer les connexions SQLite du thread UI (checkpoint du WAL) ;
        # celles des threads morts sont fermées par le GC.
        try:
            self._catalog.close()
            self._face_db.close()
        except Exception:
            pass
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        in_viewer = self._stack.currentIndex() == 1

        if key == Qt.Key_F9:
            self.toggle_sidebar()
        elif key == Qt.Key_A and modifiers == Qt.ControlModifier:
            if self._stack.currentIndex() == 0:
                self._grid.select_all()
        elif in_viewer and key == Qt.Key_Right and not self._viewer._canvas._crop_mode:
            self._navigate_photo(-1)   # plus récente
        elif in_viewer and key == Qt.Key_Left and not self._viewer._canvas._crop_mode:
            self._navigate_photo(1)    # plus ancienne
        else:
            super().keyPressEvent(event)
