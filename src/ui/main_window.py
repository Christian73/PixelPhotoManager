# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import ctypes
import logging
import os
import re
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
from src.core.cpu_throttle import (
    DEFAULT_BACKGROUND_CPU,
    note_user_activity,
    set_background_cpu_level,
)
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
    _SPECIAL_FILENAME, _SPECIAL_TAG, _SPECIAL_TAG_ITEM_PREFIX,
    _SPECIAL_RATED_ITEM_PREFIX,
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
from src.ui.language_button import LanguageButton
from src.ui.display_order_dialog import DisplayOrderDialog
from src.ui.face_backup_dialog import FaceBackupDialog

logger = logging.getLogger(__name__)

_THUMB_SIZES = [110, 180, 250, 350]

# Classes extracted from this file (2026-07) - imported under their
# historical names: they remain implementation details of MainWindow.
from src.ui.ui_utils import (  # noqa: E402
    fmt_size as _fmt_size, install_menu_width_fix,
)
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
    """Chronological sort key: date_taken, then file_mtime as a fallback."""
    if p.date_taken:
        return p.date_taken
    if p.file_mtime:
        return datetime.fromtimestamp(p.file_mtime)
    return datetime.min


def _photo_filename_sort_key(p: "PhotoInfo"):
    """Alphabetical sort key (file name, case-insensitive)."""
    return (p.filename or "").lower()


# Controllers per domain (2026-07): MainWindow methods moved in whole
# blocks - see the modules for the exact perimeter of each one.
from src.ui.main_window_faces import FacesController  # noqa: E402
from src.ui.main_window_duplicates import DuplicatesController  # noqa: E402
from src.core.i18n import translate


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
        self._apply_background_cpu_level()
        self._catalog = catalog
        self._thumb_cache = thumb_cache
        self._scanner = scanner
        self._face_db = face_db
        self._edit_db = EditDatabase()
        self._face_indexer: FaceIndexThread | None = None
        self._reindex_thread: SingleFaceReindexThread | None = None
        # Last rotation requested while a re-detection was already running
        # (photo_path, rotation) - restarted by _drain_pending_reindex().
        self._pending_reindex: "tuple[str, int] | None" = None
        self._retry_face_thread: RetryFaceIndexThread | None = None
        self._duplicate_thread: DuplicateDetectorThread | None = None
        self._live_corrupted_paths: list[str] = []
        self._last_duplicate_check: datetime | None = None
        # (current, total, message) of the detection pass in progress, or None
        # if none is in progress - feeds the progress bar of
        # "Duplicate status..." (cf. _show_duplicate_status_dialog).
        self._dup_progress: "tuple[int, int, str] | None" = None
        # Paths ignored through the X button during the detection pass in
        # progress - cf. _on_duplicate_group_ignored for the reason why.
        self._duplicate_ignored_paths: set[str] = set()
        self._duplicates_popup: "_DuplicatesPopup | None" = None
        self._index_errors_dialog = None    # IndexErrorsDialog open (or None)
        self._force_redetect_thread: ForceRedetectThread | None = None
        self._cluster_thread: ClusterThread | None = None
        self._cluster_start_time: float | None = None
        self._warmup_thread = None          # TFWarmUpThread - preloads TF at startup
        self._reset_worker: _ResetWorkerThread | None = None
        self._slideshow_win = None
        self._face_index_pending: bool = False
        self._photo_query_thread: _PhotoQueryThread | None = None
        self._persons_refresh_thread: _PersonsRefreshThread | None = None
        self._dup_migration_thread: _DupMigrationThread | None = None
        self._delete_thread: _DeleteWorkerThread | None = None
        self._pending_deletes: list = []  # confirmed deletions pending (worker already busy)
        self._scan_had_removals: bool = False
        # Manual guard (not Qt.UniqueConnection: see _on_scan_finished) -
        # a single connection of the persons_thumbnails_ready gate per application.
        self._dup_gate_connected: bool = False
        # False as long as the list of people of the sidebar has never been
        # populated: the first _on_scan_finished must always trigger a
        # refresh, even if the scan changed nothing - it is what ensures the
        # initial filling (no other path does it at startup).
        self._persons_loaded: bool = False
        self._from_person_cluster_view: bool = False
        self._viewer_back_target: str = "grid"  # "grid" | "person_cluster_view" | "duplicate_grid"
        # Global session filter (not persisted) for the annotation layer
        self._annotations_globally_visible: bool = True

        self._current_photos: list[PhotoInfo] = []
        self._current_paths: set[str] = set()
        self._current_photo_index: int = 0
        self._current_context: str = ""   # active folder or album
        self._current_album_id: int | None = None   # id of the active album, otherwise None
        self._pending_person_view_id: int | None = None
        self._catalog_loader: _CatalogLoadThread | None = None
        self._update_check_thread: UpdateCheckThread | None = None
        # Debounce of the face panel refresh after clustering (may be triggered
        # several times per second during the indexing) - a delay of 3 s.
        self._face_panel_refresh_timer = QTimer()
        self._face_panel_refresh_timer.setSingleShot(True)
        self._face_panel_refresh_timer.setInterval(3000)
        # Debounce of the search for similar faces: every identification
        # moves the centroid of a person and may make new groups
        # proposable, but one often identifies in bursts - a single pass at the
        # end of the series is enough (cf. _schedule_similarity_search).
        self._similarity_debounce = QTimer()
        self._similarity_debounce.setSingleShot(True)
        self._similarity_debounce.setInterval(30000)
        self._similarity_debounce.timeout.connect(self._start_similarity_search)

        self._setup_window()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()
        self._connect_bus()
        self._connect_scanner()
        self._setup_folder_watcher()

        # Filter installed on the whole application (and not on `self`): it must
        # also see the events of the modal dialogs and of the full-screen
        # viewer, which are not children of the main window.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # Deferred: lets window.show() run before loading the library.
        QTimer.singleShot(0, self._load_library)
        _sw = self._config.get("ui.sidebar_width", 280)

        def _apply_initial_splitter_sizes() -> None:
            self._splitter.setSizes([_sw, max(1, self._splitter.width() - _sw)])
            # If the viewer has already been opened before this deferred timer
            # fires (e.g. e2e tests chaining quickly), the setSizes()
            # above would silently overwrite the adjustment already made by
            # _ensure_left_pane_min_width() on opening - force it again here.
            self._ensure_left_pane_min_width()

        QTimer.singleShot(0, _apply_initial_splitter_sizes)
        QTimer.singleShot(0, self._restore_splitter_states)
        # Migration of the duplicate groups + counting for the badge, in a thread
        # (at the first launch after an upgrade it loads every group -
        # run synchronously here, it delayed the first display).
        self._dup_migration_thread = _DupMigrationThread(self._catalog, self)
        self._dup_migration_thread.done.connect(self._sidebar.update_duplicates_badge)
        self._dup_migration_thread.start()
        QTimer.singleShot(0, self._start_update_check)

    # ---------------------------------------------------------- user activity

    def _apply_background_cpu_level(self) -> None:
        """Applies the CPU throttling level of the background treatments.

        Called at the very beginning of __init__, hence before the start of the least
        thread: without that, cpu_throttle would read the configuration lazily at the
        first `throttle_tick()`, that is from a background thread, which
        would instantiate Config() outside the UI thread."""
        set_background_cpu_level(
            self._config.get("performance.background_cpu", DEFAULT_BACKGROUND_CPU)
        )

    # Deliberately limited to the click, the key and the wheel (cf. the docstring
    # of note_user_activity): MouseMove would come through here hundreds of times per
    # second for a simple hover, while the filter is called for *every*
    # event of *every* object of the application.
    _ACTIVITY_EVENTS = frozenset({
        QEvent.Type.MouseButtonPress,
        QEvent.Type.KeyPress,
        QEvent.Type.Wheel,
    })

    def eventFilter(self, obj, event):
        """Feeds the user activity timestamp of cpu_throttle.

        Without this filter, `_last_activity` stays frozen at the import time of the module
        and `user_is_idle()` returns True permanently past IDLE_GRACE_SECONDS:
        `effective_cpu_ratio()` is then always 1.0 and the duty cycle never
        throttles anything, whatever the level chosen in the settings."""
        if event.type() in self._ACTIVITY_EVENTS:
            note_user_activity()
        # A purely passive filter: always False, the event must carry on
        # its way to its recipient. `return False` rather than
        # `super().eventFilter(...)` - the inheritance chain does not override
        # eventFilter (QObject returns False), and it is one call fewer on a
        # path crossed by *all* the events of the application.
        return False

    # ------------------------------------------------------------------ setup

    def _setup_window(self) -> None:
        self.setWindowTitle("PixelPhotoManager")
        self.setMinimumSize(900, 600)
        w = self._config.get("ui.window_width", 1200)
        h = self._config.get("ui.window_height", 800)
        self.resize(w, h)

    def _setup_menu(self) -> None:
        # Unified bar: icon | menus | spacer | contextual buttons | export
        top_bar = QWidget()
        top_bar.setObjectName("top_bar")
        lay = QHBoxLayout(top_bar)
        lay.setContentsMargins(2, 0, 0, 0)
        lay.setSpacing(0)

        # --- Application icon (left) ---
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

        # --- Menu bar ---
        mb = QMenuBar(top_bar)
        mb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        # File
        m_file = mb.addMenu(translate("MainWindow", "File"))
        act_add = QAction(translate("MainWindow", "Add a folder…"), self)
        act_add.triggered.connect(self.open_folder_dialog)
        m_file.addAction(act_add)
        m_file.addSeparator()
        act_advanced_search = QAction(translate("MainWindow", "Advanced search…"), self)
        act_advanced_search.setShortcut(QKeySequence("Ctrl+F"))
        act_advanced_search.triggered.connect(self._open_advanced_search)
        m_file.addAction(act_advanced_search)
        m_file.addSeparator()
        act_quit = QAction(translate("MainWindow", "Quit"), self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        # View
        m_view = mb.addMenu(translate("MainWindow", "View"))
        act_sidebar = QAction(translate("MainWindow", "Show/hide sidebar"), self)
        act_sidebar.setShortcut(Qt.Key_F9)
        act_sidebar.triggered.connect(self.toggle_sidebar)
        m_view.addAction(act_sidebar)
        act_fs = QAction(translate("MainWindow", "Full screen"), self)
        act_fs.setShortcut(Qt.Key_F11)
        act_fs.triggered.connect(self._toggle_fullscreen)
        m_view.addAction(act_fs)
        m_view.addSeparator()
        act_slideshow = QAction(translate("MainWindow", "Slideshow"), self)
        act_slideshow.setShortcut(Qt.Key_F5)
        act_slideshow.triggered.connect(self._start_slideshow)
        m_view.addAction(act_slideshow)
        m_view.addSeparator()
        act_order = QAction(translate("MainWindow", "Sort order…"), self)
        act_order.triggered.connect(self._open_display_order_dialog)
        m_view.addAction(act_order)

        # Tools
        m_tools = mb.addMenu(translate("MainWindow", "Tools"))
        act_folders = QAction(translate("MainWindow", "Folders…"), self)
        act_folders.setToolTip(translate("MainWindow", "Manage the watched folders and force a "
                                                       "rescan"))
        act_folders.triggered.connect(self._open_folder_manager)
        m_tools.addAction(act_folders)
        m_tools.addSeparator()
        act_dup_status = QAction(translate("MainWindow", "Duplicate status…"), self)
        act_dup_status.setToolTip(translate("MainWindow", "Show the current state of duplicate "
                                                          "detection"))
        act_dup_status.triggered.connect(self._show_duplicate_status_dialog)
        m_tools.addAction(act_dup_status)
        m_tools.addSeparator()
        act_corrupted = QAction(translate("MainWindow", "Corrupted files…"), self)
        act_corrupted.setToolTip(
            translate("MainWindow", "Show the corrupted files found by duplicate analysis")
        )
        act_corrupted.triggered.connect(self._show_corrupted_status_dialog)
        m_tools.addAction(act_corrupted)
        act_deleted_corrupted = QAction(translate("MainWindow", "Deleted corrupted files…"), self)
        act_deleted_corrupted.setToolTip(
            translate("MainWindow", "Show the corrupted files sent to the recycle bin (to find "
                                    "them there, or in a backup if it has been emptied)")
        )
        act_deleted_corrupted.triggered.connect(self._open_deleted_corrupted_files_dialog)
        m_tools.addAction(act_deleted_corrupted)
        m_tools.addSeparator()
        act_exif_date_sync = QAction(translate("MainWindow", "Sync creation dates with EXIF…"), self)
        act_exif_date_sync.setToolTip(
            translate("MainWindow", "Replaces the Windows creation date with the EXIF date "
                                    "wherever the two differ")
        )
        act_exif_date_sync.triggered.connect(self._open_exif_date_sync)
        m_tools.addAction(act_exif_date_sync)
        m_tools.addSeparator()
        act_journal = QAction(translate("MainWindow", "Thread journal…"), self)
        act_journal.setToolTip(translate("MainWindow", "Show the activity log of the "
                                                       "background threads"))
        act_journal.triggered.connect(self._open_thread_journal)
        m_tools.addAction(act_journal)
        m_tools.addSeparator()
        act_problems = QAction(translate("MainWindow", "Problem history…"), self)
        act_problems.setToolTip(
            translate("MainWindow", "Show the history of corrupted files found and repaired")
        )
        act_problems.triggered.connect(self._open_problems_history)
        m_tools.addAction(act_problems)
        m_tools.addSeparator()
        act_ext_apps = QAction(translate("MainWindow", "External applications…"), self)
        act_ext_apps.setToolTip(
            translate("MainWindow", "Set up the third-party applications available from the "
                                    "viewer")
        )
        act_ext_apps.triggered.connect(self._open_external_apps_dialog)
        m_tools.addAction(act_ext_apps)
        m_tools.addSeparator()
        act_settings = QAction(translate("MainWindow", "Settings"), self)
        act_settings.triggered.connect(self._open_settings)
        m_tools.addAction(act_settings)

        # Faces
        m_faces = mb.addMenu(translate("MainWindow", "Faces"))
        self._act_picasa = QAction(translate("MainWindow", "Import from Picasa…"), self)
        self._act_picasa.setEnabled(not self._config.get("picasa.import_done", False))
        self._act_picasa.triggered.connect(self._import_from_picasa)
        m_faces.addAction(self._act_picasa)
        m_faces.addSeparator()
        act_reindex = QAction(translate("MainWindow", "Reset and reindex…"), self)
        act_reindex.triggered.connect(self._reset_and_reindex_faces)
        m_faces.addAction(act_reindex)
        self._act_cluster_faces = QAction(translate("MainWindow", "Group faces…"), self)
        self._act_cluster_faces.triggered.connect(self._start_clustering_with_confirm)
        m_faces.addAction(self._act_cluster_faces)
        self._act_similarity = QAction(translate("MainWindow", "Find similar faces…"), self)
        self._act_similarity.setToolTip(
            translate("MainWindow", "Compares unidentified groups with people already named "
                                    "and offers the matches for review")
        )
        self._act_similarity.triggered.connect(self._start_similarity_search_manually)
        m_faces.addAction(self._act_similarity)
        m_faces.addSeparator()
        act_index_errors = QAction(translate("MainWindow", "Error review…"), self)
        act_index_errors.setToolTip(
            translate("MainWindow", "Show the photos whose face identification failed "
                                    "(timeout/crash) and reprocess them file by file")
        )
        act_index_errors.triggered.connect(self._open_index_errors_dialog)
        m_faces.addAction(act_index_errors)
        m_faces.addSeparator()
        act_backup = QAction(translate("MainWindow", "Back up recognition data…"), self)
        act_backup.setToolTip(
            translate("MainWindow", "Creates a backup of the current faces, groups and people")
        )
        act_backup.triggered.connect(self._backup_faces)
        m_faces.addAction(act_backup)
        act_manage_backups = QAction(translate("MainWindow", "Manage backups…"), self)
        act_manage_backups.setToolTip(
            translate("MainWindow", "View, restore or delete the face recognition backups")
        )
        act_manage_backups.triggered.connect(self._manage_face_backups)
        m_faces.addAction(act_manage_backups)
        m_faces.addSeparator()
        act_face_counters = QAction(translate("MainWindow", "Counters…"), self)
        act_face_counters.triggered.connect(self._show_face_counters)
        m_faces.addAction(act_face_counters)

        # Help
        m_help = mb.addMenu(translate("MainWindow", "Help"))
        act_help = QAction(translate("MainWindow", "Help…"), self)
        act_help.setShortcut(QKeySequence("F1"))
        act_help.triggered.connect(self._show_help)
        m_help.addAction(act_help)
        m_help.addSeparator()
        act_about = QAction(translate("MainWindow", "About"), self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

        # Widens the popups when the style lets the shortcut bite into the label.
        install_menu_width_fix(mb)

        lay.addWidget(mb)

        # --- Spacer ---
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(spacer)

        # --- Contextual buttons (hidden by default) ---
        # Yellow #ffd200 shared with the favourite heart and the rating stars
        # (PhotoViewer._btn_fav / _RatingStars): the same "active" colour code as
        # those buttons of the toolbar of the viewer.
        _toggle_active_style = (
            "QPushButton:checked { color: #ffd200; }"
        )

        self._btn_faces_toggle = QPushButton(translate("MainWindow", "Faces"))
        self._btn_faces_toggle.setCheckable(True)
        self._btn_faces_toggle.setToolTip(translate("MainWindow", "Show / hide the faces in "
                                                                  "the photo"))
        self._btn_faces_toggle.setStyleSheet(_toggle_active_style)
        self._btn_faces_toggle.toggled.connect(self._on_faces_toggle)
        self._btn_faces_toggle.setVisible(False)
        lay.addWidget(self._btn_faces_toggle)
        self._act_faces_toggle = self._btn_faces_toggle

        self._btn_exif_toggle = QPushButton("EXIF")
        self._btn_exif_toggle.setCheckable(True)
        self._btn_exif_toggle.setToolTip(translate("MainWindow", "Show / hide the EXIF metadata"))
        self._btn_exif_toggle.setStyleSheet(_toggle_active_style)
        self._btn_exif_toggle.toggled.connect(self._on_exif_toggle)
        self._btn_exif_toggle.setVisible(False)
        lay.addWidget(self._btn_exif_toggle)
        self._act_exif_toggle = self._btn_exif_toggle

        self._btn_annotations_toggle = QPushButton(translate("MainWindow", "✏ Annotations"))
        self._btn_annotations_toggle.setCheckable(True)
        self._btn_annotations_toggle.setStyleSheet(_toggle_active_style)
        # setChecked() before connect(): avoids triggering _on_annotations_toggle
        # here, while self._viewer does not exist yet (_setup_central() not called yet).
        self._btn_annotations_toggle.setChecked(True)   # active by default
        self._btn_annotations_toggle.setToolTip(translate("MainWindow", "Show / hide the "
                                                                        "annotation layer "
                                                                        "(drawing/text)"))
        self._btn_annotations_toggle.toggled.connect(self._on_annotations_toggle)
        self._btn_annotations_toggle.setVisible(False)
        lay.addWidget(self._btn_annotations_toggle)

        # --- Export button ---
        self._btn_export = QPushButton(translate("MainWindow", "⬆  Export"))
        self._btn_export.setToolTip(
            translate("MainWindow", "Export the current photo (viewer) or the selected photos "
                                    "(grid)")
        )
        self._btn_export.setStyleSheet(
            "QPushButton { background:#2a5a8a; color:white; border:none;"
            " border-radius:3px; padding:4px 14px; font-weight:bold; }"
            "QPushButton:hover { background:#3a6a9a; }"
            "QPushButton:pressed { background:#1a4a7a; }"
        )
        self._btn_export.clicked.connect(self._on_export_clicked)
        lay.addWidget(self._btn_export)

        # --- Language selector (flag) ---
        # At the far right, always visible: it is the only setting a
        # user must be able to reach without being able to read the interface
        # (cf. src/ui/language_button.py).
        # No fixed width: the button sizes itself on its icon and on the
        # padding of the stylesheet of the bar - a constant carved here
        # would clip the flag at the first change of either of them.
        self._btn_language = LanguageButton(self._config)
        lay.addWidget(self._btn_language)

        margin = QWidget()
        margin.setFixedWidth(10)
        lay.addWidget(margin)

        # Black background, menu items centred vertically in the 54 px bar
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
        pass  # Merged into _setup_menu

    def _setup_central(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._splitter = QSplitter(Qt.Horizontal)
        root.addWidget(self._splitter)

        # Left panel: sidebar (grid) or edit panel (viewer)
        sidebar_w = self._config.get("ui.sidebar_width", 280)
        self._left_stack = QStackedWidget()
        self._left_stack.setMinimumWidth(160)
        self._splitter.addWidget(self._left_stack)
        self._splitter.setCollapsible(0, False)

        self._sidebar = Sidebar()
        self._sidebar.set_folder_count_provider(self._catalog.get_recursive_photo_counts)
        self._left_stack.addWidget(self._sidebar)   # index 0 - grid mode

        self._edit_panel = EditPanel()
        self._left_stack.addWidget(self._edit_panel)  # index 1 - viewer mode

        # Main area: grid or viewer
        self._stack = QStackedWidget()
        self._splitter.addWidget(self._stack)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)

        # Index 0 - Photo grid (with the context bar hidden by default)
        self._grid = ThumbnailGrid(self._thumb_cache)
        # The thumbnails reflect the non-destructive edits (rotation,
        # crop...): the grid reads the table again at every content change.
        self._grid.set_edit_provider(self._edit_db.all_edits)
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
        _btn_back_nav.setToolTip(translate("MainWindow", "Back to the previous page"))
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

        # Grid row + ribbon scrollbar (displayed in chronology mode only)
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

        # Index 1 - Viewer (with a retractable Faces panel on the left)
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
        self._viewer.tag_toggle_requested.connect(self._on_viewer_tag_toggle_requested)
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
        # Identifying from the viewer also moves a person centroid:
        # the same (deferred) trigger as from the group view.
        self._face_panel.person_assigned.connect(self._schedule_similarity_search)
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

        # Right container (face OR exif) - only one visible at a time
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

        # Index 2 - Grid of the face groups
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

        # Index 3 - View of the groups of a named person
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

        # Index 4 - Grid of the duplicate groups
        self._duplicate_grid = DuplicateGrid(self._catalog, self._thumb_cache, self)
        self._duplicate_grid.back_requested.connect(self.show_grid)
        self._duplicate_grid.view_requested.connect(self._on_duplicate_group_view_requested)
        self._duplicate_grid.group_ignored.connect(self._on_duplicate_group_ignored)
        self._duplicate_grid.detect_requested.connect(self._start_duplicate_detection)
        self._stack.addWidget(self._duplicate_grid)

        # Sidebar connections
        self._sidebar.folder_selected.connect(self._on_folder_selected)
        self._sidebar.album_selected.connect(self._on_album_selected)
        self._sidebar.album_delete_requested.connect(self._on_album_delete_requested)
        self._sidebar.tag_delete_requested.connect(self._on_tag_delete_requested)
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
        self._sidebar.section_collapse_changed.connect(
            lambda key, collapsed: self._config.set(f"ui.{key}_collapsed", collapsed)
        )
        bus.on("album.create_requested", self._on_album_create)

    def _setup_statusbar(self) -> None:
        sb = self.statusBar()

        # Left third - actions in progress (scan, loading...)
        self._lbl_action = QLabel("")
        self._lbl_action.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._lbl_action.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sb.addWidget(self._lbl_action, 1)

        # Progress bar for the long operations (hidden by default)
        self._sb_progress_bar = QProgressBar()
        self._sb_progress_bar.setFixedWidth(220)
        self._sb_progress_bar.setTextVisible(True)
        self._sb_progress_bar.setFormat(translate("MainWindow", "%v / %m photos"))
        self._sb_progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #555; border-radius: 3px; "
            "               background: #2a2a2a; text-align: center; font-size: 11px; }"
            "QProgressBar::chunk { background: #2a5a9a; border-radius: 2px; }"
        )
        self._sb_progress_bar.hide()
        sb.addWidget(self._sb_progress_bar)

        # Counter of the corrupted files detected during a duplicate scan
        # (hidden by default) - clickable to display the current list.
        self._lbl_corrupted = QPushButton("")
        self._lbl_corrupted.setFlat(True)
        self._lbl_corrupted.setCursor(Qt.PointingHandCursor)
        self._lbl_corrupted.setStyleSheet("QPushButton { color: #d9822b; border: none; }")
        self._lbl_corrupted.hide()
        self._lbl_corrupted.clicked.connect(self._show_corrupted_list_dialog)
        sb.addWidget(self._lbl_corrupted)
        # Restores the persisted state (survives a restart - see dedup_cache.py)
        # rather than waiting for the end of the next scan to display it again.
        self._update_corrupted_indicator(self._load_persisted_corrupted_paths())

        # Centre third - name of the selected file and its size
        self._lbl_fileinfo = QLabel("")
        self._lbl_fileinfo.setAlignment(Qt.AlignCenter)
        self._lbl_fileinfo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sb.addWidget(self._lbl_fileinfo, 1)

        # --- Grid mode controls ---
        self._lbl_thumb_size = QLabel(translate("MainWindow", "Size:"))
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

        # --- Viewer mode controls (hidden by default) ---
        self._lbl_zoom = QLabel(translate("MainWindow", "Zoom:"))
        self._lbl_zoom.hide()
        sb.addPermanentWidget(self._lbl_zoom)

        self._zoom_slider = MarkedSlider(Qt.Horizontal, fmt=lambda v: f"{v}%")
        self._zoom_slider.setRange(10, 400)    # 10 % to 400 %
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

        # --- Back to grid button (hidden in viewer mode) ---
        self._btn_grid_status = QPushButton("▦")
        self._btn_grid_status.setToolTip(translate("MainWindow", "Back to the grid"))
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

    # ------------------------------------------------------------------ update

    def _start_update_check(self) -> None:
        """Queries the latest GitHub release in the background (silent if up to date or on error)."""
        self._update_check_thread = UpdateCheckThread(self)
        self._update_check_thread.checked.connect(self._on_update_checked)
        self._update_check_thread.start()

    def _on_update_checked(self, status: str, version: str, html_url: str) -> None:
        if status != STATUS_UPDATE_AVAILABLE:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(translate("MainWindow", "Update available"))
        box.setText(
            translate(
                "MainWindow",
                "A new version of Pixel Photo Manager is available: {new}\n(current version: "
                "{cur}).\n\nDo read the release notes before installing, to see what is new "
                "and to check compatibility with your existing library."
            ).format(new=version, cur=get_app_version())
        )
        btn_open = box.addButton(
            translate("MainWindow", "Open the download page"), QMessageBox.AcceptRole)
        box.addButton(translate("MainWindow", "Later"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is btn_open:
            QDesktopServices.openUrl(QUrl(html_url))

    # ------------------------------------------------------------------ library

    def _load_library(self) -> None:
        folders = self._config.get_scan_folders()
        self._sidebar.set_section_collapsed_state(
            self._config.get("ui.ratings_collapsed", False),
            self._config.get("ui.tags_collapsed", False),
        )
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)
        all_tags = self._catalog.get_all_tags()
        self._sidebar.refresh_tags(all_tags)
        self._viewer.set_available_tags(all_tags)
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
            # Preload InsightFace in parallel with the scan
            self._warmup_thread = TFWarmUpThread(self)
            self._warmup_thread.finished.connect(self._on_warmup_done)
            self._warmup_thread.start()
            self._start_scan(folders)
            self._folder_watcher.set_folders(folders)
        else:
            self._show_all_photos()
            self._sidebar.select_album_item(_SPECIAL_ALL)

    def _restore_last_view(self, albums: list, folders: list) -> None:
        """Restores the last active view from the config."""
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
            # Intermediate display while waiting for the people to load

        self._show_all_photos()
        self._sidebar.select_album_item(_SPECIAL_ALL)

    def _cancel_grid_display_ops(self) -> None:
        """Cancels any photo loading for the grid still in flight.
        To be called before starting a new display (folder/album/All the
        photos): without that, a previous query still in progress may finish
        afterwards and overwrite the current view with photos that no longer
        match the displayed context."""
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
        # Records whether this scan removed photos (files gone from the disk):
        # _on_scan_finished uses it to refresh the albums/people only
        # if something really changed.
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
            # The photos discovered during the scan arrive in the order of the
            # file system, unsorted: we sort them again to respect the
            # "Display order" setting during the scan (not only at the end).
            self._current_photos = self._sort_photos_for_display(
                self._current_photos, self._current_context
            )
            self._grid.set_photos(self._current_photos)
            self._update_status()

    @Slot(list)
    def _on_photos_removed(self, paths: list[str]) -> None:
        """Removes from the UI the photos whose file has disappeared from the disk."""
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
        # Refresh the albums and people only if the scan really changed
        # something (new photos, or files gone from the disk): a
        # rescan with no change - the frequent case of a watcher event on a
        # simple file attribute - must cost nothing.
        if total or self._scan_had_removals:
            self._sidebar.refresh_albums(self._catalog.get_albums())
        # Full rebuild only if the scan found new photos
        # (hence potentially new faces/people); a light update
        # of the counters on a simple deletion - avoids emptying and
        # reloading the whole list of people with its thumbnails. The very
        # first pass always refreshes (initial filling of the list,
        # cf. _persons_loaded): update_persons_data then switches on its own
        # to a full rebuild since the list is still empty.
        if total:
            self._refresh_persons()
        elif self._scan_had_removals or not self._persons_loaded:
            self._update_persons_counts()
        self._persons_loaded = True

        # The scan adds the new photos in filesystem (unsorted) order.
        # We sort the current list again according to the "Display order" setting.
        # Applicable to "All the photos" and to the folder views (the special views
        # such as Favorites, Videos or Person do not receive photos through _on_photos_batch).
        # Useless if the scan added nothing: the current order is already right.
        if total and self._current_photos \
                and not self._current_context.startswith(_PERSON_CTX_PREFIX):
            self._current_photos = self._sort_photos_for_display(
                self._current_photos, self._current_context
            )
            self._grid.set_photos(self._current_photos)

        if self._warmup_thread and self._warmup_thread.isRunning():
            self._lbl_action.setText(translate("MainWindow", "Initialising face recognition…"))
            self._face_index_pending = True
        else:
            self._start_face_indexing()
        # Defer the duplicate detection until the face thumbnails of
        # the known people (sidebar) are loaded, so as not to
        # compete with them for CPU/IO as soon as the application starts.
        # A manual guard rather than Qt.UniqueConnection: on this precise
        # connection (a @Slot() method inherited from a non-QObject mixin, connected
        # with Qt.UniqueConnection), PySide6 returns a valid
        # QMetaObject.Connection but the slot is then never invoked - a silent
        # ghost connection. Without Qt.UniqueConnection the connection works
        # normally; the guard just avoids the multiple connections if
        # _on_scan_finished is called several times before the first firing.
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
        # Both entry points write the same key: the flag of the
        # top bar would otherwise be out of date after a change made here.
        self._btn_language.refresh()

    #: Media scope of an external application: the value stored in the config
    #: ("image"/"video"/"both") is never translated - it is compared with
    #: PhotoInfo.media_type. Only its label is.
    _MEDIA_SCOPE_VALUES = ("both", "image", "video")

    @classmethod
    def _media_scope_label(cls, value: str) -> str:
        return {
            "image": translate("MainWindow", "Photo"),
            "video": translate("MainWindow", "Video"),
        }.get(value, translate("MainWindow", "Both"))

    def _open_external_apps_dialog(self) -> None:
        """Configuration dialog of the external applications reachable from the viewer.

        Each application is tagged with a media scope (photo / video / both)
        that determines in which case its icon appears in the bar of
        the viewer (PhotoViewer.refresh_external_apps) - an entry without a
        "media" key (a config predating this feature) is treated
        as "both", and therefore stays visible everywhere as before."""
        apps: list = list(self._config.get("tools.external_apps", []))

        dlg = QDialog(self)
        dlg.setWindowTitle(translate("MainWindow", "External applications"))
        dlg.setMinimumWidth(520)
        root = QVBoxLayout(dlg)

        root.addWidget(QLabel(
            translate("MainWindow", "Applications available from their icon in the viewer bar:")
        ))

        lst = QListWidget(dlg)
        for app in apps:
            label = self._media_scope_label(app.get("media", "both"))
            lst.addItem(f"{app['name']}   —   {app['path']}   [{label}]")

        btn_row = QHBoxLayout()
        btn_add = QPushButton(translate("MainWindow", "Add…"))
        btn_del = QPushButton(translate("MainWindow", "Remove"))
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
                dlg, translate("MainWindow", "Choose an application"), "",
                translate("MainWindow", "Executables (*.exe);;All files (*)")
            )
            if not path:
                return
            default_name = os.path.splitext(os.path.basename(path))[0]
            name, ok = QInputDialog.getText(
                dlg, translate("MainWindow", "Application name"),
                translate("MainWindow", "Name shown in the tooltip:"), text=default_name
            )
            if not (ok and name.strip()):
                return
            labels = [self._media_scope_label(v) for v in self._MEDIA_SCOPE_VALUES]
            media_label, ok = QInputDialog.getItem(
                dlg, translate("MainWindow", "Media type"),
                translate("MainWindow", "Show this application's icon for:"),
                labels, 0, False
            )
            if not ok:
                return
            media = self._MEDIA_SCOPE_VALUES[labels.index(media_label)]
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
        self._lbl_action.setText(
            translate("MainWindow", "Scanning… {percent}%  —  {path}"
                      ).format(percent=percent, path=path))

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
            has_prev=self._current_photo_index < n - 1,  # there are older photos
            has_next=self._current_photo_index > 0,       # there are newer photos
        )
        self._viewer.set_nav_position(self._current_photo_index + 1, n)

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
        self._ensure_left_pane_min_width()
        if self._face_panel.isVisible():
            self._face_panel.set_photo(photo.path)
        if self._exif_panel.isVisible():
            self._exif_panel.set_photo(photo.path)
            self._exif_panel.set_tags(photo.tags)
        self._update_viewer_status(photo)
        self._update_nav_arrows()
        self._prefetch_viewer_neighbors()

    def _prefetch_viewer_neighbors(self) -> None:
        """Preloads the base image of the photos neighbouring the one displayed in
        the viewer (the closest first): prev/next becomes instant."""
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
        """Starts a photo query in the background and updates the grid on arrival.
        folder_path: real path of the selected folder (only for the
        "Folders" sidebar, distinct from context_key which also serves as a display label
        for the other views) - makes it possible to detect a DVD copy without confusing it
        with an album name that would coincide by chance with a path on the disk."""
        self._cancel_grid_display_ops()
        # Immediate visual feedback on the click (the indicator only really appears
        # if the query goes beyond 150 ms); hidden by grid.set_photos().
        self._grid.set_loading(True)
        # Sort parameters resolved here (UI thread: Config reads),
        # sorting run in the thread with the query.
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
                    translate("MainWindow", "This folder holds no catalogued photo, but looks "
                                            "like a DVD copy (VIDEO_TS folder)."),
                    translate("MainWindow", "Open with an external player"),
                    lambda _checked=False, fp=folder_path: self._open_dvd_folder(fp),
                )
        self._update_status()

    def _open_dvd_folder(self, folder_path: str) -> None:
        """Opens a "DVD copy" folder in an external application already
        configured by the user (Tools > External applications... menu, the same
        list as the one used by the viewer to open a photo - cf.
        PhotoViewer._open_with). We pass it the folder itself (and not the
        VIDEO_TS subfolder): VLC and most players detect
        VIDEO_TS inside a folder passed as an argument. Only the
        applications tagged "video" or "both" are offered here - an
        application tagged "photo" (e.g. an image editor) makes no sense
        for opening a VIDEO_TS folder."""
        all_apps: list = list(self._config.get("tools.external_apps", []))
        apps = [a for a in all_apps if a.get("media", "both") != "image"]
        if not apps:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle(translate("MainWindow", "No external application set up"))
            if all_apps:
                box.setText(
                    translate("MainWindow", "None of the external applications set up handles "
                                            "video (they are all limited to photos). Set one "
                                            "up (VLC, for instance) from Tools › External "
                                            "applications… to open this folder.")
                )
            else:
                box.setText(
                    translate("MainWindow", "Set up an external application first (VLC, for "
                                            "instance) from Tools › External applications… to "
                                            "open this folder.")
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
        """Builds (without displaying it) the external application choice menu
        for target_path - extracted from _open_dvd_folder to stay testable
        without going through a modal QMenu.exec()."""
        menu = QMenu(self)
        install_menu_width_fix(menu)
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
                translate("MainWindow", "Could not start the application"),
                translate("MainWindow", "Failed to start:\n{path}\n\n{error}")
                .format(path=app_path, error=exc),
            )

    def _sort_params_for_context(self, context: str) -> tuple:
        """Resolves the sort parameters (key_fn, reverse) of the "Display
        order" setting for a given context. Must be called on the UI thread
        (Config reads); the sorting itself can then run in a
        secondary thread (_PhotoQueryThread). The "All the photos" view
        (Chronology) always stays sorted chronologically - an alphabetical
        sort makes no sense for an album called
        "Chronology" - but its direction follows a dedicated setting
        (`display_order.chrono_album_dir`), independent of the one of the
        standard photo grid (`display_order.grid_dir`)."""
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
        """Applies the "Display order" setting to a list of photos, on
        the current thread (see _sort_params_for_context for the version
        moved into _PhotoQueryThread)."""
        key_fn, reverse = self._sort_params_for_context(context)
        return sorted(photos, key=key_fn, reverse=reverse)

    def _open_display_order_dialog(self) -> None:
        dlg = DisplayOrderDialog(self._config, self)
        if dlg.exec() == QDialog.Accepted:
            dlg.save_to_config()
            self._apply_display_order()

    def _apply_display_order(self) -> None:
        """Reapplies the "Display order" setting to the folder tree and
        to the currently displayed grid (called after a modification through the
        dialog, or when the library is loaded)."""
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
                lambda: self._catalog.get_photos_min_rating(1), "Par notes"
            )
        elif isinstance(data, str) and data.startswith(_SPECIAL_RATED_ITEM_PREFIX):
            n = int(data[len(_SPECIAL_RATED_ITEM_PREFIX):])
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(
                lambda n=n: self._catalog.get_photos_min_rating(n),
                f"Par notes : {n}★ et plus",
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
        elif isinstance(data, str) and data.startswith(_SPECIAL_TAG_ITEM_PREFIX):
            tag = data[len(_SPECIAL_TAG_ITEM_PREFIX):]
            self._grid.set_ribbon_mode(False)
            self._grid.set_date_overlay_visible(False)
            self._grid_nav_bar.hide()
            self.show_grid()
            self._start_photo_query(
                lambda t=tag: self._catalog.get_photos_by_tag(t),
                f"Mot-clé : {tag}",
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
                self, translate("MainWindow", "Remove the folder"),
                translate(
                    "MainWindow",
                    "Stop watching “{folder}”?\n\n<b>{count}</b> photo(s) will be removed from "
                    "the catalogue, along with their thumbnails and faces. The files "
                    "themselves stay untouched on disk."
                ).format(folder=folder, count=f"{count:,}"),
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
        """Moves the dragged files to dest_folder and updates every reference."""
        # Declare the moves to the watcher BEFORE touching the disk:
        # every reference (catalog, thumbnails, faces, grid) is
        # updated right here, the rescan the watcher would otherwise trigger
        # would be purely redundant.
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
                errors.append(translate(
                    "MainWindow", "{name}: already exists at the destination"
                ).format(name=filename))
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
            # Navigate to the destination folder to show the moved photos
            photos = self._sort_photos_for_display(
                self._catalog.get_photos_in_folder(dest_folder), dest_folder
            )
            self._current_photos = photos
            self._current_paths  = {p.path for p in photos}
            self._current_context = dest_folder
            self._grid.set_photos(photos)
            self._update_status()

        if errors:
            QMessageBox.warning(self, translate("MainWindow", "Errors while moving"),
                                "\n".join(errors))

    @Slot(str)
    def _on_folder_created(self, path: str) -> None:
        """New subfolder created on disk: refresh the tree and scan."""
        self._sidebar.refresh_folders(self._config.get_scan_folders())
        self._start_scan([path])

    @Slot(str, str)
    def _on_folder_moved(self, old_path: str, new_path: str) -> None:
        """Folder renamed or moved: update the catalog, the config and the UI."""
        # Normalise to guarantee the consistency of the Windows paths
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        # Catalog + faces
        self._catalog.update_paths_prefix(old_path, new_path)
        self._face_db.update_paths_prefix(old_path, new_path)
        # Config: replace the watched folders concerned
        for folder in list(self._config.get_scan_folders()):
            if folder == old_path or folder.startswith(old_path + os.sep):
                updated = new_path + folder[len(old_path):]
                self._config.remove_scan_folder(folder)
                self._config.add_scan_folder(updated)
        # Photos in memory
        n = len(old_path)
        for photo in self._current_photos:
            if photo.path == old_path or photo.path.startswith(old_path + os.sep):
                photo.path      = new_path + photo.path[n:]
                photo.directory = new_path + photo.directory[n:]
        # Active context
        if self._current_context and (
            self._current_context == old_path
            or self._current_context.startswith(old_path + os.sep)
        ):
            self._current_context = new_path + self._current_context[n:]
        # Refresh the sidebar, the grid and the watcher
        updated_folders = self._config.get_scan_folders()
        self._sidebar.refresh_folders(updated_folders)
        self._folder_watcher.set_folders(updated_folders)
        self._grid.set_photos(self._current_photos)
        self._update_status()

    # ------------------------------------------------------------------ duplicates

    @Slot(str)
    def _navigate_to_photo_path(self, path: str) -> None:
        results = self._catalog.get_photos_by_paths([path])
        if not results:
            QMessageBox.warning(
                self, translate("MainWindow", "Photo not found"),
                translate("MainWindow", "The photo is no longer in the library:\n{path}")
                .format(path=path))
            return
        photo = results[0]
        folder = photo.directory
        self._on_folder_selected(folder)
        QTimer.singleShot(350, lambda: (
            self._grid.scroll_to_photo(path),
            self._grid.select_photo(path),
        ))

    def _purge_catalog_for_folder(self, folder: str) -> list[str]:
        """Deletes from the catalog, from the thumbnail cache and from the face database
        every photo of a folder (and of its subfolders).
        Returns the deleted paths."""
        photos = self._catalog.get_photos_in_folder(folder)
        # Include the subfolders through the catalog
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
        """Folder deleted from the disk: clean up the catalog, the caches and the UI."""
        folder = os.path.normpath(folder)
        deleted_paths = self._purge_catalog_for_folder(folder)
        self._duplicate_grid.invalidate()
        self._sidebar.update_duplicates_badge(self._catalog.count_duplicate_groups())

        # Remove from the config if it was a watched folder
        for watched in list(self._config.get_scan_folders()):
            if watched == folder or watched.startswith(folder + os.sep):
                self._config.remove_scan_folder(watched)

        # If the active context was in the deleted folder, go back to the empty grid
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
            self, translate("MainWindow", "Delete the album"),
            translate(
                "MainWindow",
                "Delete the album “{name}” ({count} photo(s))?\n\nThe photos stay untouched in "
                "the catalogue and on disk; only the album is deleted."
            ).format(name=album.name, count=album.photo_count),
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

    @Slot(str)
    def _on_tag_delete_requested(self, tag: str) -> None:
        photos = self._catalog.get_photos_by_tag(tag)
        reply = QMessageBox.question(
            self, translate("MainWindow", "Delete the keyword"),
            translate(
                "MainWindow",
                "Delete the keyword “{tag}” ({count} photo(s))?\n\nThe keyword will be removed "
                "from every photo that carries it. The photos and their other keywords stay "
                "untouched."
            ).format(tag=tag, count=len(photos)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        ids = [p.id for p in photos if p.id is not None]
        self._catalog.remove_tag_from_photos(ids, tag)
        for p in photos:
            p.tags = [t for t in p.tags if t != tag]

        all_tags = self._catalog.get_all_tags()
        self._sidebar.refresh_tags(all_tags)
        self._viewer.set_available_tags(all_tags)

        if self._current_context == f"Mot-clé : {tag}":
            self._sidebar.select_album_item(_SPECIAL_ALL)
            self._show_all_photos()

        current = self._viewer.current_photo()
        if current is not None and tag in current.tags:
            current.tags = [t for t in current.tags if t != tag]
            self._viewer.refresh_tags()
            if self._exif_panel.isVisible():
                self._exif_panel.set_tags(current.tags)

    def _on_add_to_album(self, photos: list) -> None:
        albums = self._catalog.get_albums()
        if not albums:
            QMessageBox.information(
                self, translate("MainWindow", "Add to an album"),
                translate("MainWindow", "No album yet.\nCreate one first from the Albums panel.")
            )
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(translate("MainWindow", "Add to an album"))
        dlg.setMinimumWidth(320)
        layout = QVBoxLayout(dlg)
        n = len(photos)
        layout.addWidget(QLabel(translate(
            "MainWindow", "Choose the album for {count} photo(s):").format(count=n)))
        lst = QListWidget(dlg)
        for album in albums:
            lst.addItem(translate(
                "MainWindow", "{name}  ({count} photo(s))"
            ).format(name=album.name, count=album.photo_count))
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
            translate("MainWindow", "{count} photo(s) added to “{name}”"
                      ).format(count=added, name=album.name), 4000
        )

    def _on_create_album_with(self, photos: list) -> None:
        n = len(photos)
        name, ok = QInputDialog.getText(
            self, translate("MainWindow", "New album"),
            translate("MainWindow", "Name of the new album ({count} photo(s) selected):"
                      ).format(count=n)
        )
        if not ok or not name.strip():
            return
        album = self._catalog.create_album(name.strip())
        added = self._catalog.add_photos_to_album(
            album.id, [p.id for p in photos if p.id is not None]
        )
        self._sidebar.refresh_albums(self._catalog.get_albums())
        self.statusBar().showMessage(
            translate("MainWindow", "Album “{name}” created with {count} photo(s)"
                      ).format(name=name.strip(), count=added), 4000
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
        """Back arrow in the viewer: goes back to the original screen."""
        target = self._viewer_back_target
        self._viewer_back_target = "grid"
        last_photo = self._viewer.current_photo()
        if target == "person_cluster_view":
            person = self._person_cluster_view.current_person
            if person:
                self.show_person_clusters(person)
                return
        elif target == "duplicate_grid":
            self.show_duplicate_grid()
            return
        self.show_grid()
        # Visual feedback: the last photo displayed in the viewer
        # becomes immediately locatable again in the grid (visible + selected),
        # centred in chronological view (ribbon mode).
        if last_photo is not None:
            self._grid.scroll_to_photo(last_photo.path)
            self._grid.select_photo(last_photo.path)

    def _ensure_left_pane_min_width(self) -> None:
        """QStackedWidget does not trigger a relayout of the QSplitter when its
        current page changes - without this call, if the page that has just
        become current (typically EditPanel) has a minimum width
        requirement greater than what the splitter has already allocated to it (e.g. still
        stuck on the smaller width of the sidebar), it stays
        squeezed below that minimum: its 2nd column of treatment buttons
        becomes invisible and unreachable by click, silently."""
        sizes = self._splitter.sizes()
        if len(sizes) != 2:
            return
        needed = max(self._left_stack.minimumSizeHint().width(), self._edit_panel.content_min_width())
        if sizes[0] >= needed:
            return
        delta = needed - sizes[0]
        self._splitter.setSizes([needed, max(1, sizes[1] - delta)])

    def show_viewer(self, photo: PhotoInfo) -> None:
        is_video = photo.media_type == "video"
        self._viewer.set_album_context(self._current_album_id)
        self._viewer.set_photo(photo)
        if not is_video:
            self._edit_panel.set_photo(photo)
        self._stack.setCurrentIndex(1)
        self._left_stack.setCurrentIndex(0 if is_video else 1)
        self._ensure_left_pane_min_width()
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
        # A new _Canvas does not spontaneously know the session state.
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
            self, translate("MainWindow", "Choose a photo folder"), os.path.expanduser("~")
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
        # Priority: viewer open      -> displayed photo
        #           chronology mode  -> photo at the centre of the ribbon
        #           otherwise        -> oldest photo
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
        """Update of the catalog after an EXIF modification (date_taken may have changed)."""
        # File rewritten on disk: forget the base image cached by the viewer.
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
        """Re-detects the faces after a 90 degree rotation by the user."""
        from src.faces.detector import is_available
        if not is_available():
            return
        if self._reindex_thread and self._reindex_thread.isRunning():
            # Detection already in progress (several seconds on a 24 Mpx photo):
            # memorise the LAST rotation requested and restart it at the end.
            # Dropping it left indexed_photos.rotation frozen on an
            # intermediate orientation (two quick clicks on the rotate button), and the
            # detection only found part of the faces again.
            self._pending_reindex = (photo_path, rotation)
            return
        self._pending_reindex = None
        self._start_single_reindex(photo_path, rotation)

    def _start_single_reindex(self, photo_path: str, rotation: int) -> None:
        if self._reindex_thread is not None:
            self._reindex_thread.deleteLater()
        self._reindex_thread = SingleFaceReindexThread(
            self._face_db, photo_path, rotation, self
        )
        self._reindex_thread.finished.connect(self._on_single_reindex_finished)
        self._reindex_thread.cluster_requested.connect(self._run_clustering)
        self._reindex_thread.start()

    def _drain_pending_reindex(self) -> None:
        """Restarts the last rotation requested while a detection was running.

        Called at the end of SingleFaceReindexThread. The `finished` signal is emitted
        from run(), hence before the thread is really finished: we
        try again as long as it is still running rather than deleteLater() a live
        QThread (Qt fail-fast)."""
        if self._pending_reindex is None:
            return
        if self._reindex_thread is not None and self._reindex_thread.isRunning():
            QTimer.singleShot(50, self._drain_pending_reindex)
            return
        pending, self._pending_reindex = self._pending_reindex, None
        self._start_single_reindex(*pending)

    @Slot(list)
    def _on_delete_requested(self, photos: list) -> None:
        if not photos:
            return

        if not self._config.get("ui.delete_no_confirm", False):
            n = len(photos)
            if n == 1:
                msg = translate(
                    "MainWindow",
                    "Send “{name}” to the Windows recycle bin?\n\nThe file will still be "
                    "recoverable from the recycle bin."
                ).format(name=photos[0].filename)
            else:
                msg = translate(
                    "MainWindow",
                    "Send the {count} selected files to the Windows recycle bin?\n\nThey will "
                    "still be recoverable from the recycle bin."
                ).format(count=n)
            box = QMessageBox(QMessageBox.Warning, translate("MainWindow", "Confirm deletion"), msg,
                              QMessageBox.Yes | QMessageBox.Cancel, self)
            box.setDefaultButton(QMessageBox.Cancel)
            chk = QCheckBox(translate("MainWindow", "Do not ask again"))
            box.setCheckBox(chk)
            if box.exec() != QMessageBox.Yes:
                return
            if chk.isChecked():
                self._config.set("ui.delete_no_confirm", True)

        # A single deletion worker at a time: two interleaved workers
        # would make the epilogue (grid, duplicate groups) inconsistent. The
        # deletion is already confirmed at this stage: queue it rather
        # than drop it silently (cf. _pending_deletes in memory -
        # a worker may stay `isRunning()` for several seconds, notably on
        # `FaceDatabase.delete_for_paths` in case of a passing SQLite contention,
        # far longer than the fleeting status message that warned
        # the user before this fix).
        if self._delete_thread is not None and self._delete_thread.isRunning():
            self._pending_deletes.append(photos)
            self.statusBar().showMessage(translate("MainWindow", "Deletion queued…"), 3000)
            return

        self._start_delete_worker(photos)

    def _start_delete_worker(self, photos: list) -> None:
        # Memorise the state of the viewer before the deletion
        in_viewer = self._stack.currentIndex() == 1
        viewed_index = self._current_photo_index
        deleted_paths_set = {p.path for p in photos}

        # Index of the first deleted file (to recentre the ribbon afterwards)
        first_deleted_idx = next(
            (i for i, p in enumerate(self._current_photos) if p.path in deleted_paths_set),
            None,
        )

        # Duplicate groups concerned by this deletion: if the deletion
        # brings a group below 2 copies, it is no longer a duplicate.
        affected_groups = {p.duplicate_group_id for p in photos if p.duplicate_group_id is not None}

        # Declare the deletions to the watcher BEFORE touching the disk: the
        # rescan it would otherwise trigger (400 ms debounce) would redo, in pure
        # waste, everything the epilogue below already updates.
        self._folder_watcher.notify_self_deletions([p.path for p in photos])

        # Unlink + purge of the catalog/thumbnails/faces in a thread: the
        # synchronous loop froze the UI for several seconds on a multi-selection.
        worker = _DeleteWorkerThread(
            [p.path for p in photos], self._catalog, self._thumb_cache,
            self._face_db, self,
        )
        self._delete_thread = worker
        worker.progress.connect(
            lambda done, total: self._lbl_action.setText(
                translate("MainWindow", "Deleting… {done}/{total}"
                          ).format(done=done, total=total))
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
        """UI epilogue of a deletion carried out by _DeleteWorkerThread:
        update of the grid/albums/duplicate groups and neighbour navigation.
        Stays on the UI thread - it touches _duplicate_ignored_paths and the state
        of the widgets."""
        self._lbl_action.setText("")
        deleted_paths_set = set(deleted)
        if deleted:
            self._grid.remove_photos(deleted)
            deleted_set = set(deleted)
            self._current_photos = [p for p in self._current_photos
                                    if p.path not in deleted_set]
            self._current_paths -= deleted_set
            self._update_status()
            # Refresh the album counters immediately: the watcher
            # will no longer emit for this deletion (notify_self_deletions).
            self._sidebar.refresh_albums(self._catalog.get_albums())
            self._sidebar.refresh_tags(self._catalog.get_all_tags())

            # Dissolve the duplicate groups that have become singletons (or empty)
            # following this deletion: otherwise the card stays displayed in
            # DuplicateGrid for a group that no longer has any reason to exist.
            stale_groups = []
            for gid in affected_groups:
                remaining = self._catalog.get_duplicates_for_group(gid)
                if len(remaining) < 2:
                    # The same trap as the X button (cf. _on_duplicate_group_ignored):
                    # a detection pass in progress may still merge this
                    # group in memory from before the deletion.
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
                # Safety net: forces a reload from the catalog at the
                # next display of the duplicates grid, even if
                # remove_group() has already updated the cards in memory.
                self._duplicate_grid.invalidate()

            # If the viewer displayed a deleted photo, navigate to the neighbour
            if in_viewer and any(p in deleted_paths_set
                                 for p in [self._viewer.current_photo().path]
                                 if self._viewer.current_photo()):
                # Duplicate comparison reduced to 0 or 1 copy: it no
                # longer has any reason to exist, automatic return to the duplicates grid
                # rather than carrying on displaying the single remaining copy.
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
            QMessageBox.warning(self, translate("MainWindow", "Deletion errors"),
                                translate("MainWindow", "Cannot delete:\n{details}")
                                .format(details="\n".join(errors)))

        if self._pending_deletes:
            self._start_delete_worker(self._pending_deletes.pop(0))

    def _on_favorite_toggle_requested(self, photo: PhotoInfo) -> None:
        """Persists the favourite toggle requested by the grid (context menu)
        or the viewer (star button / context menu) - both emitters
        toggle photo.is_favorite before emitting, this handler only has to write
        the already up-to-date state."""
        if photo.id is not None:
            self._catalog.set_favorite(photo.id, photo.is_favorite)

    def _on_rating_change_requested(self, photos: list, rating: int) -> None:
        """Persists the rating requested by the grid (context menu, possibly a
        multi-selection) or the viewer (stars / 0-5 keyboard / context menu),
        then refreshes the badges of the grid - the grid and the viewer do not
        necessarily share the same PhotoInfo instance for a given path."""
        ids = [p.id for p in photos if p.id is not None]
        if ids:
            self._catalog.set_rating_for_ids(ids, rating)
        self._grid.refresh_rating({p.path: rating for p in photos})

    def _on_edit_tags_requested(self, photos: list) -> None:
        """Opens the keyword editing dialog for the selection (grid or viewer
        context menu) - preloads the list of the tags already
        known to the catalog in a thread before the opening (the
        _AssignPrepLoader pattern of face_panel.py), so as never to block the UI for
        the duration of the query."""
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
        if to_add or to_remove:
            all_tags = self._catalog.get_all_tags()
            self._sidebar.refresh_tags(all_tags)
            self._viewer.set_available_tags(all_tags)

        for p in photos:
            merged = list(p.tags)
            for t in to_add:
                if t not in merged:
                    merged.append(t)
            p.tags = [t for t in merged if t not in to_remove]

        current = self._viewer.current_photo()
        if current is not None:
            match = next((p for p in photos if p.path == current.path), None)
            if match is not None:
                current.tags = match.tags
                self._viewer.refresh_tags()
                if self._exif_panel.isVisible():
                    self._exif_panel.set_tags(current.tags)

    def _on_viewer_tag_toggle_requested(self, photo: PhotoInfo, tag: str, added: bool) -> None:
        """Entry of the keyword drop-down list clicked in the toolbar
        of the viewer (PhotoViewer._tag_dropdown) - photo.tags has
        already been updated optimistically by the viewer itself before
        the signal was emitted."""
        if photo.id is None:
            return
        if added:
            self._catalog.add_tags_to_photos([photo.id], [tag])
        else:
            self._catalog.remove_tag_from_photos([photo.id], tag)
        self._sidebar.refresh_tags(self._catalog.get_all_tags())
        if self._exif_panel.isVisible():
            self._exif_panel.set_tags(photo.tags)

    def _open_advanced_search(self) -> None:
        """Opens the advanced search dialog (File > Advanced search...
        menu, Ctrl+F, or magnifier button of the sidebar) - preloads the cameras/
        people/tags in a thread before the opening (the
        _AssignPrepLoader pattern of face_panel.py), so as never to block the UI."""
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
        """Runs the advanced search (on the thread of _PhotoQueryThread):
        SQL criteria through Catalog.search_advanced(), then a Python intersection
        with the photos of the selected person - catalog.db and faces.db
        are two separate databases, with no JOIN possible between them (cf.
        CLAUDE.md), hence this intersection on the caller side rather than in SQL."""
        photos = self._catalog.search_advanced(criteria)
        if person_id is not None:
            person_paths = set(self._face_db.get_photos_for_person(person_id))
            photos = [p for p in photos if p.path in person_paths]
        return photos

    def _on_remove_from_album_requested(self, photos: list) -> None:
        """Removes the photos from the displayed album (Del key / context menu in
        an album context, grid or viewer): touches neither the file on disk
        nor the photo itself, unlike _on_delete_requested."""
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

        # If the viewer displayed a removed photo, navigate to the neighbour
        # (the same logic as _on_delete_requested).
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
            self._lbl_fileinfo.setText(translate(
                "MainWindow", "{sel} photos selected  —  {total} in total"
            ).format(sel=n_sel, total=n_total))
        else:
            count_str = translate("MainWindow", "%n photo(s)", None, n_total)
            if self._current_context:
                self._lbl_fileinfo.setText(
                    f"{self._context_label(self._current_context)}  —  {count_str}")
            else:
                self._lbl_fileinfo.setText(count_str)

    @Slot(object)
    def _on_rename_requested(self, photo: PhotoInfo) -> None:
        old_p = Path(photo.path)
        new_stem, ok = QInputDialog.getText(
            self,
            translate("MainWindow", "Rename the image"),
            translate("MainWindow", "New name:"),
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
                self, translate("MainWindow", "Invalid name"),
                translate("MainWindow", "The name cannot contain these characters: \\ / : * ? "
                                        "\" < > |"),
            )
            return

        new_p = old_p.parent / (new_stem + old_p.suffix)
        if new_p == old_p:
            return
        if new_p.exists():
            QMessageBox.warning(
                self, translate("MainWindow", "File already exists"),
                translate("MainWindow",
                          "A file named “{name}” already exists in this folder.")
                .format(name=new_p.name),
            )
            return

        try:
            old_p.rename(new_p)
        except OSError as e:
            QMessageBox.critical(
                self, translate("MainWindow", "Error"),
                translate("MainWindow", "Cannot rename the file:\n{error}")
                .format(error=e))
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
            translate("MainWindow", "Move to…"),
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
                self, translate("MainWindow", "File already exists"),
                translate("MainWindow",
                          "A file named “{name}” already exists in this folder.")
                .format(name=old_p.name),
            )
            return

        try:
            shutil.move(str(old_p), str(new_p))
        except OSError as e:
            QMessageBox.critical(
                self, translate("MainWindow", "Error"),
                translate("MainWindow", "Cannot move the file:\n{error}")
                .format(error=e))
            return

        old_path_str = photo.path
        new_path_str = os.path.normpath(str(new_p))
        self._catalog.rename_photo(old_path_str, new_path_str)
        self._edit_db.rename_photo(old_path_str, new_path_str)
        self._face_db.update_path(old_path_str, new_path_str)
        self._thumb_cache.invalidate(old_path_str)

        # The photo is no longer in the current folder: we remove it from the grid
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
            # Optional backup of the original before overwriting
            if dlg.backup_before_overwrite:
                try:
                    self._backup_original(photo.path)
                except Exception as e:
                    logger.error("Échec sauvegarde original %s : %s",
                                 photo.path, e, exc_info=True)
                    answer = QMessageBox.warning(
                        self, translate("MainWindow", "Save failed"),
                        translate(
                            "MainWindow",
                            "Cannot copy the original into .tmp_originals:\n{error}\n\nDo you "
                            "still want to overwrite the original file?")
                        .format(error=e),
                        QMessageBox.Yes | QMessageBox.Cancel,
                        QMessageBox.Cancel,
                    )
                    if answer != QMessageBox.Yes:
                        return
            dest = photo.path
        else:
            original = Path(photo.path)
            suggested = original.parent / (
                original.stem + translate("MainWindow", "_edited") + original.suffix)
            dest, _ = QFileDialog.getSaveFileName(
                self,
                translate("MainWindow", "Save the edited image"),
                str(suggested),
                translate("MainWindow", "JPEG (*.jpg *.jpeg);;PNG (*.png);;All files (*)"),
            )
            if not dest:
                return

        self._export_image(photo, dest)

    def _backup_original(self, photo_path: str) -> None:
        """Copies the original file into .tmp_originals (a hidden folder) with a timestamp."""
        original = Path(photo_path)
        backup_dir = original.parent / ".tmp_originals"
        backup_dir.mkdir(exist_ok=True)

        # Make the folder hidden on Windows
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(backup_dir), 0x02)
        except Exception:
            pass  # non-blocking on non-Windows systems

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{original.stem}_{ts}{original.suffix}"
        shutil.copy2(photo_path, backup_dir / backup_name)
        logger.info("Original sauvegardé : %s", backup_dir / backup_name)

    def _export_image(self, photo: PhotoInfo, dest: str) -> None:
        """Exports the processed full-resolution image to dest."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            from PIL import Image, ImageOps
            from src.processing.adjustments import ImageAdjuster
            from src.ui.annotation_renderer import composite_annotations_pil

            orig_stat = os.stat(photo.path)

            edit = self._edit_db.load(photo.path)
            with Image.open(photo.path) as img:
                img = ImageOps.exif_transpose(img)
                orig_w, orig_h = img.size
                if edit.is_modified():
                    # Frame excluded here: the annotations are in normalised
                    # coordinates of the PHOTO, they must be composited before
                    # the frame enlarges the image around it (cf. apply_all).
                    img = ImageAdjuster.apply_all(img, edit, with_frame=False)
                if self._annotations_globally_visible and edit.annotations:
                    img = composite_annotations_pil(img, edit.annotations)
                if edit.frame_type != "none":
                    from src.processing.frames import apply_frame
                    img = apply_frame(img, edit)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                if Path(dest).suffix.lower() == ".png":
                    img.save(dest, format="PNG")
                else:
                    if img.mode == "RGBA":
                        img = img.convert("RGB")
                    img.save(dest, format="JPEG", quality=95, subsampling=0)

            # Restore the dates of the original file (atime, mtime and creation date)
            preserve_file_dates(orig_stat, dest)

            if os.path.normpath(dest) == os.path.normpath(photo.path):
                # The edits are now baked into the file: delete the edit
                # and refresh the UI to avoid a double application at the next loading
                self._edit_db.delete(photo.path)
                self._thumb_cache.invalidate(photo.path)
                self._remap_face_bboxes_after_save(photo.path, edit, orig_w, orig_h)
                # The file on disk has changed: the base image cached by the
                # viewer no longer matches it (it would show the version without
                # the edits while they are now baked into the file).
                self._viewer.invalidate_base_cache(photo.path)
                self._viewer.update_edit(EditInfo())
                self._edit_panel.set_photo(photo)

            self._lbl_action.setText(translate(
                "MainWindow", "Image saved: {name}").format(name=Path(dest).name))
            QTimer.singleShot(4000, lambda: self._lbl_action.setText(""))
        except Exception as e:
            logger.error("Erreur export image %s : %s", photo.path, e, exc_info=True)
            QMessageBox.critical(self, translate("MainWindow", "Export error"),
                                 translate("MainWindow", "Cannot save the image:\n{error}")
                                 .format(error=e))
        finally:
            QApplication.restoreOverrideCursor()

    def _remap_face_bboxes_after_save(
        self, photo_path: str, edit: EditInfo, orig_w: int, orig_h: int,
    ) -> None:
        """After a save overwriting the original file: the crop and
        the possible rotation/straightening are now baked into the pixels,
        so the stored face bboxes (computed on the original image)
        no longer point at the right place - realign them in the new frame
        (cf. GeometryProcessor.transform_bboxes) or purge them if they have fallen out of
        frame (a crop excluding the face)."""
        if not (edit.rotation or edit.straighten or edit.flip_h or edit.flip_v or edit.crop):
            return
        from src.processing.geometry import GeometryProcessor

        faces = self._face_db.get_faces_for_photo(photo_path)
        if not faces:
            return

        by_detected_rotation: dict = {}
        for f in faces:
            by_detected_rotation.setdefault(f.detected_rotation % 360, []).append(f)

        updates: dict = {}
        deletions: list = []
        for det_rot, group in by_detected_rotation.items():
            size = (orig_h, orig_w) if det_rot in (90, 270) else (orig_w, orig_h)
            bboxes = [(f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h) for f in group]
            results, _final_size = GeometryProcessor.transform_bboxes(
                bboxes, size,
                rotation=edit.rotation, straighten=edit.straighten,
                flip_h=edit.flip_h, flip_v=edit.flip_v, crop=edit.crop,
                pre_rotation=det_rot,
            )
            for f, res in zip(group, results):
                if res is None:
                    deletions.append(f.id)
                else:
                    updates[f.id] = res

        if updates or deletions:
            self._face_db.remap_bboxes_after_save(photo_path, updates, deletions)

    @Slot()
    def _on_export_clicked(self) -> None:
        if self._stack.currentIndex() == 1:   # viewer mode
            if not self._viewer._photo:
                return
            photos = [self._viewer._photo]
        else:                                  # grid mode
            photos = self._grid.get_selected()
            if not photos:
                QMessageBox.information(
                    self, translate("MainWindow", "Export"),
                    translate("MainWindow", "Select at least one photo in the grid before "
                                            "exporting."),
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
        """Exports photos to export_dir with the given resizing and quality."""
        from PIL import Image, ImageOps
        from src.processing.adjustments import ImageAdjuster
        from src.ui.annotation_renderer import composite_annotations_pil

        try:
            export_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(
                self, translate("MainWindow", "Error"),
                translate("MainWindow", "Cannot create the folder:\n{error}")
                .format(error=e))
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        errors: list[str] = []
        try:
            for i, photo in enumerate(photos):
                self._lbl_action.setText(
                    translate("MainWindow", "Export {cur}/{total}  —  {name}"
                              ).format(cur=i + 1, total=len(photos), name=photo.filename)
                )
                QApplication.processEvents()
                try:
                    edit = self._edit_db.load(photo.path)
                    with Image.open(photo.path) as img:
                        img = ImageOps.exif_transpose(img)
                        if edit.is_modified():
                            # Frame laid down after the annotations, cf. _export_image.
                            img = ImageAdjuster.apply_all(img, edit, with_frame=False)
                        # Resizing if needed
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
                        if edit.frame_type != "none":
                            from src.processing.frames import apply_frame
                            img = apply_frame(img, edit)
                        if img.mode not in ("RGB", "RGBA"):
                            img = img.convert("RGB")
                        if img.mode == "RGBA":
                            img = img.convert("RGB")
                        # Resolution of the destination file name (always in JPG)
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
                    errors.append(f"{photo.filename} : {e}")  # system message, not translated
                    logger.error("Export %s : %s", photo.path, e, exc_info=True)
        finally:
            QApplication.restoreOverrideCursor()

        self._lbl_action.setText("")
        if errors:
            # The 4th argument of translate() must be a plain variable name,
            # otherwise lupdate removes the message from the catalog (cf. CLAUDE.md).
            n_errors = len(errors)
            QMessageBox.warning(
                self, translate("MainWindow", "Export errors"),
                translate("MainWindow", "%n file(s) not exported:", None, n_errors)
                + "\n" + "\n".join(errors),
            )
        else:
            n = len(photos)
            msg = (translate("MainWindow", "%n photo(s) exported", None, n)
                   + f"  →  {export_dir}")
            self._lbl_action.setText(msg)
            QTimer.singleShot(5000, lambda: self._lbl_action.setText(""))
            # PPM_SUPPRESS_EXPLORER=1 (set by tools/test_env/launch_isolated.py):
            # opening the Explorer here would come in front of the window of the application and
            # would stay open after the end of the test (an explorer.exe process not
            # attached to the application, never closed by terminate()), disturbing the
            # following e2e scenarios which drive the window through UIA.
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

    def _context_label(self, ctx: str) -> str:
        """Displayed label for a grid context.

        `_current_context` is an **internal key**: it is compared a little
        everywhere by == / startswith (cf. `_encode_view_state`) and must therefore
        stay in French whatever the language of the interface. The
        translation happens here, at the only place where the value is shown to
        the user (`_update_status`)."""
        exact = {
            "Toutes les photos": translate("MainWindow", "All photos"),
            "Favoris":           translate("MainWindow", "Favourites"),
            "Vidéos":            translate("MainWindow", "Videos"),
            "Par notes":         translate("MainWindow", "By rating"),
            "Recherche avancée": translate("MainWindow", "Advanced search"),
        }
        if ctx in exact:
            return exact[ctx]
        m = re.match(r"^Par notes : (\d+)★ et plus$", ctx)
        if m:
            return translate(
                "MainWindow", "By rating: {n}★ and above").format(n=m.group(1))
        for key, fmt in (
            ("Fichiers : ", translate("MainWindow", "Files: {query}")),
            ("Mot-clé : ",  translate("MainWindow", "Keyword: {query}")),
        ):
            if ctx.startswith(key):
                return fmt.format(query=ctx[len(key):])
        return ctx

    def _encode_view_state(self) -> dict:
        ctx = self._current_context
        if ctx == "Toutes les photos":
            return {"type": "all"}
        if ctx == "Favoris":
            return {"type": "favorites"}
        if ctx == "Vidéos":
            return {"type": "videos"}
        if ctx == "Par notes":
            return {"type": "rated"}
        if ctx.startswith(f"{_PERSON_CTX_PREFIX}cluster_"):
            return {"type": "all"}   # transient view, no restoration
        if ctx.startswith(_PERSON_CTX_PREFIX):
            try:
                return {"type": "person", "value": int(ctx[len(_PERSON_CTX_PREFIX):])}
            except ValueError:
                return {"type": "all"}
        if (
            ctx.startswith("Fichiers : ") or ctx.startswith("Mot-clé : ")
            or ctx.startswith("Par notes : ") or ctx == "Recherche avancée"
        ):
            return {"type": "all"}   # ephemeral filter
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
        # Confirmation BEFORE any stop signal: if the user cancels the
        # closing (below), no background thread must have
        # been interrupted in the meantime.
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
            dlg.setWindowTitle(translate("MainWindow", "Grouping in progress"))
            dlg.setIcon(QMessageBox.Icon.Warning)
            dlg.setText(translate("MainWindow", "<b>A face grouping is running.</b>"))
            dlg.setInformativeText(
                translate(
                    "MainWindow",
                    "Grouping has been running for <b>{duration}</b>.<br><br>If you close the "
                    "application now, the computation stops and <b>its result is lost</b>. It "
                    "will have to start over at the next launch.<br><br>Close the application "
                    "anyway?"
                ).format(duration=duree)
            )
            dlg.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            dlg.setDefaultButton(QMessageBox.StandardButton.No)
            dlg.button(QMessageBox.StandardButton.Yes).setText(translate("MainWindow", "Close "
                                                                                       "anyway"))
            dlg.button(QMessageBox.StandardButton.No).setText(translate("MainWindow", "Cancel"))
            if dlg.exec() != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self._folder_watcher.set_folders([])

        # Signals the stop to every background thread before waiting for
        # anything: they thus stop in parallel rather than
        # one after the other - the old sequential wait could
        # pile up several seconds per thread, up to a good minute
        # with FaceIndexThread, which could stay blocked up to
        # _DETECT_TIMEOUT/_WARMUP_TIMEOUT in a blocking call on its
        # subprocess before even noticing the stop request (fixed
        # separately: FaceIndexThread.stop() now kills the executor
        # straight away).
        self._scanner.request_stop()
        if self._face_indexer and self._face_indexer.isRunning():
            self._face_indexer.stop()
        if self._duplicate_thread and self._duplicate_thread.isRunning():
            self._duplicate_thread.cancel()

        # Hide the window straight away: all the useful state is already
        # saved above, so nothing prevents making the closing
        # instantaneous on screen while the wait() calls below (up to
        # ~10 s in total if a scan/detection is in progress) run in the
        # background, invisible to the user.
        self.hide()

        self._scanner.wait_stopped(3000)
        if self._face_indexer and self._face_indexer.isRunning():
            self._face_indexer.wait(3000)
        if self._cluster_thread and self._cluster_thread.isRunning():
            self._cluster_thread.wait(500)
        if self._duplicate_thread and self._duplicate_thread.isRunning():
            self._duplicate_thread.wait(3000)
            if self._duplicate_thread.isRunning():
                # An ORB thread may stay blocked beyond the delay above
                # (a single cv2 call in progress, e.g. a large file on a slow
                # network volume) despite cancel() - a Python thread cannot
                # be killed cleanly from the outside, and `sys.exit()`
                # would wait for its end anyway (atexit of ThreadPoolExecutor).
                # On an explicit request from the user (the application took too
                # long to close): we prefer to kill the process
                # immediately rather than letting the application linger in the
                # background. All the useful state (config, geometry, last
                # view) is already saved higher up in this method.
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
            # Let the batch DB purge finish: interrupting it would leave
            # files deleted from the disk but still present in the catalog.
            self._delete_thread.wait(5000)
        # Close the SQLite connections of the UI thread (WAL checkpoint);
        # those of the dead threads are closed by the GC.
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
            elif self._stack.currentWidget() is self._person_cluster_view:
                self._person_cluster_view.select_all()
        elif in_viewer and key == Qt.Key_Right and not self._viewer._canvas._crop_mode:
            self._navigate_photo(-1)   # newer
        elif in_viewer and key == Qt.Key_Left and not self._viewer._canvas._crop_mode:
            self._navigate_photo(1)    # older
        else:
            super().keyPressEvent(event)
