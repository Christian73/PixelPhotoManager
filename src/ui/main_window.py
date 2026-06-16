import ctypes
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFrame, QGroupBox,
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QRadioButton, QScrollBar, QSplitter, QStackedWidget, QStatusBar, QToolBar,
    QLineEdit, QSlider, QLabel, QPushButton,
    QFileDialog, QInputDialog, QMessageBox, QSizePolicy,
)

from src.core.config import Config
from src.core.event_bus import bus
from src.core.models import PhotoInfo, AlbumInfo, PersonInfo, EditInfo
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.library.folder_watcher import FolderWatcher
from src.library.scanner import LibraryScanner
from src.faces.face_database import FaceDatabase
from src.faces.face_indexer import FaceIndexThread, SingleFaceReindexThread, TFWarmUpThread
from src.faces.clusterer import ClusterThread
from src.processing.edit_database import EditDatabase
from src.ui.sidebar import Sidebar, _SPECIAL_ALL, _SPECIAL_FAV, _SPECIAL_VIDEOS
from src.ui.thumbnail_grid import ThumbnailGrid
from src.ui.photo_viewer import PhotoViewer
from src.ui.edit_panel import EditPanel, MarkedSlider
from src.ui.face_cluster_grid import FaceClusterGrid
from src.ui.person_cluster_view import PersonClusterView
from src.ui.face_panel import FacePanel
from src.ui.exif_panel import ExifPanel
from src.ui.people_panel import MergePersonsDialog, PeopleDialog
from src.ui.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

_THUMB_SIZES = [110, 180, 250, 350]


def _fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} Ko"
    return f"{size_bytes / (1024 * 1024):.1f} Mo"


# (label, max_total_pixels | None, jpeg_quality, size_hint)
_EXPORT_SIZES = [
    ("Taille maximale — résolution originale", None,      95, ""),
    ("Grande  (~4 Mpx)",                       4_000_000, 98, "600–1 600 Ko"),
    ("Moyenne (~2 Mpx)",                       2_000_000, 94, "320–800 Ko"),
    ("Petite  (~500 kpx)",                     500_000,   90, "75–300 Ko"),
]


class _ExportDialog(QDialog):
    _DEFAULT_DIR = Path.home() / "Pictures" / "PixelPhotoManager" / "Export"

    def __init__(self, photo_count: int, parent=None):
        super().__init__(parent)
        n = photo_count
        self.setWindowTitle(f"Exporter {n} photo{'s' if n > 1 else ''}")
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        # Dossier de destination
        grp_dir = QGroupBox("Dossier de destination")
        dir_layout = QHBoxLayout(grp_dir)
        self._dir_edit = QLineEdit(str(self._DEFAULT_DIR))
        dir_layout.addWidget(self._dir_edit)
        btn_browse = QPushButton("Parcourir…")
        btn_browse.setFixedWidth(90)
        btn_browse.clicked.connect(self._browse)
        dir_layout.addWidget(btn_browse)
        layout.addWidget(grp_dir)

        # Options de taille
        grp_size = QGroupBox("Taille d'export")
        grp_size.setStyleSheet("""
            QRadioButton::indicator {
                width: 13px; height: 13px;
                border-radius: 7px;
                border: 2px solid #888;
                background: transparent;
            }
            QRadioButton::indicator:checked {
                background: #7aabdb;
                border: 2px solid #7aabdb;
            }
            QRadioButton::indicator:unchecked:hover {
                border-color: #bbb;
            }
        """)
        size_layout = QVBoxLayout(grp_size)
        size_layout.setSpacing(6)
        self._size_radios: list[tuple[QRadioButton, int | None, int]] = []
        btn_group = QButtonGroup(self)   # groupe exclusif : un seul actif à la fois
        btn_group.setExclusive(True)
        for i, (label, max_px, quality, size_hint) in enumerate(_EXPORT_SIZES):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            rb = QRadioButton(label)
            rb.setChecked(i == 0)
            btn_group.addButton(rb)
            row_layout.addWidget(rb)

            if size_hint:
                lbl_info = QLabel(f"qualité {quality}  •  ≈ {size_hint}")
                lbl_info.setStyleSheet("color: #777; font-size: 10px;")
                row_layout.addWidget(lbl_info)

            row_layout.addStretch()
            size_layout.addWidget(row)
            self._size_radios.append((rb, max_px, quality))
        layout.addWidget(grp_size)

        # Boutons
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Exporter")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier d'export", self._dir_edit.text()
        )
        if folder:
            self._dir_edit.setText(folder)

    @property
    def export_dir(self) -> Path:
        return Path(self._dir_edit.text().strip())

    @property
    def size_preset(self) -> tuple:
        """Retourne (max_total_pixels | None, jpeg_quality)."""
        for rb, max_px, quality in self._size_radios:
            if rb.isChecked():
                return (max_px, quality)
        return (None, 95)  # fallback : taille maximale


class _SaveOptionsDialog(QDialog):
    """Dialogue de sauvegarde de l'image traitée.

    Propose trois actions :
    - Écraser le fichier original (avec option de sauvegarde dans .tmp_originals)
    - Enregistrer à un autre emplacement via l'explorateur
    """

    def __init__(self, photo_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Enregistrer l'image traitée")
        self.setMinimumWidth(480)
        self._photo_path = photo_path
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 18, 18, 18)

        # En-tête
        lbl_name = QLabel(f"<b>{Path(self._photo_path).name}</b>")
        lbl_name.setStyleSheet("font-size: 11px;")
        layout.addWidget(lbl_name)

        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #444;")
        layout.addWidget(sep)
        layout.addSpacing(4)

        # --- Option 1 : écraser ---
        self._rb_overwrite = QRadioButton("Écraser le fichier original")
        self._rb_overwrite.setChecked(True)
        layout.addWidget(self._rb_overwrite)

        self._overwrite_details = QWidget()
        od_layout = QVBoxLayout(self._overwrite_details)
        od_layout.setContentsMargins(24, 2, 0, 4)
        od_layout.setSpacing(6)

        lbl_warn = QLabel(
            "⚠  Cette action est irréversible : le fichier original sera définitivement\n"
            "    remplacé par la version traitée."
        )
        lbl_warn.setStyleSheet("color: #e8a040; font-size: 10px;")
        od_layout.addWidget(lbl_warn)

        self._cb_backup = QCheckBox(
            "Copier l'original dans .tmp_originals avant l'écrasement"
        )
        self._cb_backup.setChecked(True)
        self._cb_backup.setToolTip(
            f"L'original sera copié dans :\n"
            f"{Path(self._photo_path).parent / '.tmp_originals'}"
        )
        od_layout.addWidget(self._cb_backup)

        layout.addWidget(self._overwrite_details)

        # --- Option 2 : enregistrer ailleurs ---
        self._rb_elsewhere = QRadioButton("Enregistrer à un autre emplacement…")
        layout.addWidget(self._rb_elsewhere)

        layout.addSpacing(8)

        # Activer/désactiver le bloc d'avertissement selon la radio sélectionnée
        self._rb_overwrite.toggled.connect(self._overwrite_details.setEnabled)

        # Boutons
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Enregistrer")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # --- Résultats ---

    @property
    def overwrite(self) -> bool:
        return self._rb_overwrite.isChecked()

    @property
    def backup_before_overwrite(self) -> bool:
        return self._cb_backup.isChecked()


_PERSON_CTX_PREFIX = "__person__"


def _photo_sort_key(p: "PhotoInfo"):
    """Clé de tri : date_taken, puis file_mtime en fallback — ordre descendant."""
    if p.date_taken:
        return p.date_taken
    if p.file_mtime:
        return datetime.fromtimestamp(p.file_mtime)
    return datetime.min


class _CatalogLoadThread(QThread):
    """Charge get_all_photos() hors du thread UI et émet les résultats par lots."""

    batch_ready = Signal(list)  # list[PhotoInfo]

    def __init__(self, catalog: "Catalog", batch_size: int = 300):
        super().__init__()
        self._catalog = catalog
        self._batch_size = batch_size
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        photos = self._catalog.get_all_photos()
        for i in range(0, len(photos), self._batch_size):
            if self._stop:
                break
            self.batch_ready.emit(photos[i : i + self._batch_size])


class _PhotoQueryThread(QThread):
    """Exécute une requête catalog/face_db dans un thread secondaire."""

    photos_ready = Signal(list, str)   # list[PhotoInfo], context_key

    def __init__(self, fn, context_key: str, parent=None) -> None:
        super().__init__(parent)
        self._fn          = fn
        self._context_key = context_key

    def run(self) -> None:
        try:
            photos = self._fn()
            self.photos_ready.emit(photos, self._context_key)
        except Exception:
            self.photos_ready.emit([], self._context_key)


class _PersonsRefreshThread(QThread):
    """Charge get_persons + enrich_persons + get_unnamed_clusters hors du thread UI."""

    result_ready = Signal(list, int)   # persons, unnamed_cluster_count

    def __init__(self, catalog, face_db, parent=None) -> None:
        super().__init__(parent)
        self._catalog  = catalog
        self._face_db  = face_db

    def run(self) -> None:
        try:
            persons = self._catalog.get_persons()
            self._face_db.enrich_persons(persons)
            count = len(self._face_db.get_unnamed_clusters())
            self.result_ready.emit(persons, count)
        except Exception:
            self.result_ready.emit([], 0)


class _ResetWorkerThread(QThread):
    """
    Attend l'arrêt des threads d'indexation/clustering en cours,
    effectue le reset DB demandé, puis émet done(choice).
    """

    done = Signal(int)   # choice : RESET_CLUSTERING ou RESET_FULL

    def __init__(
        self,
        face_db,
        choice: int,
        threads_to_wait: list,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._choice  = choice
        self._threads = threads_to_wait   # refs Python fortes → gardés en vie

    def run(self) -> None:
        for t in self._threads:
            try:
                if t.isRunning():
                    t.wait(10_000)   # 10 s max par thread
            except RuntimeError:
                pass   # objet C++ déjà supprimé
        if self._choice == 1:   # RESET_CLUSTERING
            self._face_db.reset_clustering()
        else:                    # RESET_FULL
            self._face_db.reset_index()
        self.done.emit(self._choice)


class _ResetFacesDialog(QDialog):
    """Dialogue de choix entre reset clustering seul et réinitialisation complète."""

    RESET_CLUSTERING = 1
    RESET_FULL       = 2

    _FRAME_BASE = (
        "QFrame#opt {"
        "  border: 2px solid #444; border-radius: 6px; background: #252525;"
        "}"
    )
    _FRAME_SEL = (
        "QFrame#opt {"
        "  border: 2px solid #4a9fd4; border-radius: 6px; background: #1a2f45;"
        "}"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Réinitialiser l'index des visages")
        self.setMinimumWidth(480)
        self.setStyleSheet(
            "QDialog { background: #1e1e1e; color: #ddd; }"
            "QRadioButton { color: #eee; font-size: 12px; font-weight: bold;"
            "  background: transparent; spacing: 8px; }"
            "QRadioButton::indicator { width: 15px; height: 15px; }"
            "QLabel { color: #aaa; font-size: 11px; background: transparent; }"
            "QDialogButtonBox QPushButton {"
            "  min-width: 90px; padding: 5px 12px;"
            "  background: #2a2a2a; color: #ddd;"
            "  border: 1px solid #555; border-radius: 4px;"
            "}"
            "QDialogButtonBox QPushButton:hover { background: #333; border-color: #888; }"
            "QDialogButtonBox QPushButton:default {"
            "  background: #1a3a5a; border-color: #4a9fd4; color: #fff;"
            "}"
        )
        self._choice = self.RESET_CLUSTERING
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        self._btn_group = QButtonGroup(self)

        self._rb_cluster = QRadioButton(
            "Réinitialiser les groupes uniquement  —  rapide"
        )
        self._rb_full = QRadioButton(
            "Réinitialisation complète + réindexation  —  lente"
        )
        self._btn_group.addButton(self._rb_cluster)
        self._btn_group.addButton(self._rb_full)

        self._frame_cluster = self._make_frame(
            self._rb_cluster,
            [
                "Les embeddings ArcFace (analyse des visages) sont conservés.",
                "Seuls les regroupements HDBSCAN sont effacés et recalculés.",
                "Les associations visage → personne (Picasa, identification manuelle)",
                "sont préservées et redistribuées dans les nouveaux groupes.",
                "⏱  Durée : quelques secondes.",
            ],
        )
        self._frame_full = self._make_frame(
            self._rb_full,
            [
                "Tout est effacé : embeddings, groupes, associations visage → personne.",
                "La détection ArcFace est relancée sur l'ensemble de la bibliothèque.",
                "Les personnes nommées sont conservées ; les annotations Picasa",
                "sont ré-appliquées automatiquement après re-détection.",
                "⏱  Durée : plusieurs heures selon la taille de la bibliothèque.",
            ],
        )
        root.addWidget(self._frame_cluster)
        root.addWidget(self._frame_full)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Confirmer")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

        self._rb_cluster.setChecked(True)
        self._frame_cluster.setStyleSheet(self._FRAME_SEL)
        self._btn_group.buttonToggled.connect(self._on_toggled)

    def _make_frame(self, rb: QRadioButton, lines: list[str]) -> QFrame:
        frame = QFrame()
        frame.setObjectName("opt")
        frame.setStyleSheet(self._FRAME_BASE)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        lay.addWidget(rb)
        for line in lines:
            lbl = QLabel(line)
            lbl.setIndent(23)
            lay.addWidget(lbl)
        return frame

    def _on_toggled(self, btn: QRadioButton, checked: bool) -> None:
        if not checked:
            return
        if btn is self._rb_cluster:
            self._choice = self.RESET_CLUSTERING
            self._frame_cluster.setStyleSheet(self._FRAME_SEL)
            self._frame_full.setStyleSheet(self._FRAME_BASE)
        else:
            self._choice = self.RESET_FULL
            self._frame_cluster.setStyleSheet(self._FRAME_BASE)
            self._frame_full.setStyleSheet(self._FRAME_SEL)

    @property
    def choice(self) -> int:
        return self._choice


class MainWindow(QMainWindow):
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
        self._cluster_thread: ClusterThread | None = None
        self._cluster_start_time: float | None = None
        self._warmup_thread = None          # TFWarmUpThread — pré-charge TF au démarrage
        self._reset_worker: _ResetWorkerThread | None = None
        self._face_index_pending: bool = False
        self._photo_query_thread: _PhotoQueryThread | None = None
        self._persons_refresh_thread: _PersonsRefreshThread | None = None
        self._from_person_cluster_view: bool = False

        self._current_photos: list[PhotoInfo] = []
        self._current_paths: set[str] = set()
        self._current_photo_index: int = 0
        self._current_context: str = ""   # dossier ou album actif
        self._catalog_loader: _CatalogLoadThread | None = None
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        # Debounce du refresh du face panel après clustering (peut être déclenché
        # plusieurs fois par seconde pendant l'indexation) — délai de 3 s.
        self._face_panel_refresh_timer = QTimer()
        self._face_panel_refresh_timer.setSingleShot(True)
        self._face_panel_refresh_timer.setInterval(3000)
        self._search_timer.timeout.connect(self._do_search)

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

    # ------------------------------------------------------------------ setup

    def _setup_window(self) -> None:
        self.setWindowTitle("PixelPhotoManager")
        self.setMinimumSize(900, 600)
        w = self._config.get("ui.window_width", 1200)
        h = self._config.get("ui.window_height", 800)
        self.resize(w, h)

    def _setup_menu(self) -> None:
        mb = self.menuBar()

        # Fichier
        m_file = mb.addMenu("Fichier")
        act_add = QAction("Ajouter un dossier…", self)
        act_add.triggered.connect(self.open_folder_dialog)
        m_file.addAction(act_add)
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

        # Outils
        m_tools = mb.addMenu("Outils")
        act_folders = QAction("Dossiers…", self)
        act_folders.setToolTip("Gérer les dossiers surveillés et forcer un re-scan")
        act_folders.triggered.connect(self._open_folder_manager)
        m_tools.addAction(act_folders)
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
        act_settings = QAction("Paramètres", self)
        act_settings.triggered.connect(self._open_settings)
        m_tools.addAction(act_settings)

        # Visages
        m_faces = mb.addMenu("Visages")
        self._act_index_faces = QAction("Analyser les visages", self)
        self._act_index_faces.triggered.connect(self._start_face_indexing)
        m_faces.addAction(self._act_index_faces)
        self._act_cluster_faces = QAction("Regrouper les visages…", self)
        self._act_cluster_faces.triggered.connect(self._start_clustering_with_confirm)
        m_faces.addAction(self._act_cluster_faces)
        m_faces.addSeparator()
        act_identify = QAction("Identifier les personnes…", self)
        act_identify.triggered.connect(self.show_face_clusters)
        m_faces.addAction(act_identify)
        m_faces.addSeparator()
        act_reindex = QAction("Réinitialiser et réindexer…", self)
        act_reindex.triggered.connect(self._reset_and_reindex_faces)
        m_faces.addAction(act_reindex)
        m_faces.addSeparator()
        act_picasa = QAction("Importer depuis Picasa…", self)
        act_picasa.triggered.connect(self._import_from_picasa)
        m_faces.addAction(act_picasa)

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

    def _setup_toolbar(self) -> None:
        tb = QToolBar("Recherche")
        tb.setMovable(False)
        self.addToolBar(tb)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Rechercher… (Ctrl+F)")
        self._search_box.setMinimumWidth(130)
        self._search_box.setMaximumWidth(260)
        self._search_box.textChanged.connect(self._on_search_text_changed)
        tb.addWidget(self._search_box)

        act_clear = QAction("✕", self)
        act_clear.setToolTip("")
        act_clear.triggered.connect(lambda: self._search_box.clear())
        tb.addAction(act_clear)

        # Espaceur flexible pour pousser le bouton Export à droite
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self._btn_faces_toggle = QPushButton("Visages")
        self._btn_faces_toggle.setCheckable(True)
        self._btn_faces_toggle.setToolTip("Afficher / masquer les visages de la photo")
        self._btn_faces_toggle.toggled.connect(self._on_faces_toggle)
        self._act_faces_toggle = tb.addWidget(self._btn_faces_toggle)
        self._act_faces_toggle.setVisible(False)

        self._btn_exif_toggle = QPushButton("EXIF")
        self._btn_exif_toggle.setCheckable(True)
        self._btn_exif_toggle.setToolTip("Afficher / masquer les métadonnées EXIF")
        self._btn_exif_toggle.toggled.connect(self._on_exif_toggle)
        self._act_exif_toggle = tb.addWidget(self._btn_exif_toggle)
        self._act_exif_toggle.setVisible(False)

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
        tb.addWidget(self._btn_export)

        margin = QWidget()
        margin.setFixedWidth(20)
        tb.addWidget(margin)

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
        self._grid.delete_requested.connect(self._on_delete_requested)
        self._grid.save_requested.connect(self._on_save_requested)

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
        self._viewer = PhotoViewer(config=self._config)
        self._viewer.closed.connect(self.show_grid)
        self._viewer.navigate.connect(self._navigate_photo)
        self._viewer.zoom_changed.connect(self._on_viewer_zoom_changed)
        self._viewer.save_requested.connect(self._on_save_requested)
        self._viewer.rename_requested.connect(self._on_rename_requested)
        self._viewer.delete_requested.connect(self._on_delete_requested)
        self._edit_panel.edits_changed.connect(self._viewer.update_edit)
        self._edit_panel.crop_mode_requested.connect(self._viewer.enter_crop_mode)
        self._edit_panel.grid_visibility_changed.connect(self._viewer.set_grid_visible)
        self._edit_panel.photo_saved.connect(self._on_photo_saved)
        self._edit_panel.rotation_stepped.connect(self._on_rotation_stepped)
        self._edit_panel.red_eye_mode_requested.connect(self._on_red_eye_mode_requested)
        self._edit_panel.wb_pick_requested.connect(self._on_wb_pick_requested)
        self._viewer.crop_ready.connect(self._edit_panel.apply_crop)
        self._viewer.red_eye_point_added.connect(self._edit_panel.on_red_eye_added)
        self._viewer.pixel_sampled.connect(self._edit_panel.on_wb_pixel_received)

        self._face_panel = FacePanel(self._face_db, self._catalog, self)
        self._face_panel.face_highlighted.connect(self._on_face_highlighted)
        self._face_panel.all_faces_toggled.connect(self._on_all_faces_toggled)
        self._face_panel.person_assigned.connect(self._refresh_persons)
        self._face_panel.person_cluster_requested.connect(
            self._on_face_panel_person_cluster_requested
        )
        self._viewer.face_context_menu_requested.connect(self._on_face_context_menu)
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
        self._face_cluster_grid.cluster_merged.connect(self._on_cluster_merged)
        self._face_cluster_grid.photos_requested.connect(self._on_cluster_photos_requested)
        self._face_cluster_grid.back_requested.connect(self.show_grid)
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
        self._person_cluster_view.faces_reassigned.connect(self._refresh_persons)
        self._stack.addWidget(self._person_cluster_view)

        # Connexions sidebar
        self._sidebar.folder_selected.connect(self._on_folder_selected)
        self._sidebar.album_selected.connect(self._on_album_selected)
        self._sidebar.scan_requested.connect(self._on_scan_requested)
        self._sidebar.folder_removed.connect(self._on_folder_removed)
        self._sidebar.folder_created.connect(self._on_folder_created)
        self._sidebar.folder_moved.connect(self._on_folder_moved)
        self._sidebar.photos_dropped.connect(self._on_photos_dropped)
        self._sidebar.person_selected.connect(self._on_person_selected)
        self._sidebar.identify_requested.connect(self.show_face_clusters)
        self._sidebar.person_merge_requested.connect(self._on_person_merge_requested)
        self._sidebar.person_rename_requested.connect(self._on_person_rename_requested)
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

    # ------------------------------------------------------------------ library

    def _load_library(self) -> None:
        folders = self._config.get_scan_folders()
        if folders:
            self._sidebar.set_tree_expanded_paths(
                self._config.get("ui.folder_tree_expanded", [])
            )
            self._sidebar.refresh_folders(folders)
            self._show_all_photos()
            # Pré-charger TF/DeepFace en parallèle du scan pour éliminer le freeze
            # de ~20 s lors du premier appel à detect_and_embed()
            if self._config.get("faces.auto_index", False):
                self._warmup_thread = TFWarmUpThread(self)
                self._warmup_thread.finished.connect(self._on_warmup_done)
                self._warmup_thread.start()
            self._start_scan(folders)
            self._folder_watcher.set_folders(folders)
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)

    def _show_all_photos(self) -> None:
        # Annule un chargement précédent si toujours actif.
        if self._catalog_loader is not None:
            self._catalog_loader.stop()
            self._catalog_loader.wait()
            self._catalog_loader = None

        self._current_photos = []
        self._current_paths = set()
        self._current_context = "Toutes les photos"
        self._grid.set_ribbon_mode(True)
        self._grid.set_date_overlay_visible(True)
        self._grid.set_photos([])
        self._grid_nav_bar.hide()
        self.show_grid()
        self._update_status()

        loader = _CatalogLoadThread(self._catalog)
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
            self._grid.add_photos_batch(visible_new)
            self._update_status()

    @Slot(list)
    def _on_photos_removed(self, paths: list[str]) -> None:
        """Retire de l'UI les photos dont le fichier a disparu du disque."""
        removed_set = set(paths)
        self._current_photos = [p for p in self._current_photos
                                 if p.path not in removed_set]
        self._current_paths -= removed_set
        self._grid.remove_photos(paths)
        self._update_status()
        for path in paths:
            self._face_db.delete_for_path(path)
        logger.info("%d photo(s) retirée(s) du catalogue (fichiers absents)", len(paths))

    @Slot(int)
    def _on_scan_finished(self, total: int) -> None:
        self._lbl_action.setText("")
        self._update_status()
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)
        self._refresh_persons()

        # Le scan ajoute les nouvelles photos dans l'ordre filesystem (non daté).
        # On re-trie la liste courante pour garantir : plus récent en haut.
        # Applicable à "Toutes les photos" et aux vues dossier (les vues spéciales
        # comme Favoris, Vidéos ou Person ne reçoivent pas de photos via _on_photos_batch).
        if self._current_photos and not self._current_context.startswith(_PERSON_CTX_PREFIX):
            self._current_photos.sort(key=_photo_sort_key, reverse=True)
            self._grid.set_photos(self._current_photos)

        if self._config.get("faces.auto_index", False):
            if self._warmup_thread and self._warmup_thread.isRunning():
                # Le pré-chargement TF n'est pas encore terminé — attendre
                self._lbl_action.setText("Initialisation de la reconnaissance faciale…")
                self._face_index_pending = True
            else:
                self._start_face_indexing()

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

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._config, self)
        dlg.recluster_needed.connect(self._run_clustering)
        dlg.exec()

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
        self._act_index_faces.setEnabled(False)
        self._act_index_faces.setText("Réinitialisation en cours…")
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
            self._act_index_faces.setText("Analyser les visages")
            self._act_index_faces.setEnabled(True)
            self._run_clustering()
        else:
            self._start_face_indexing()

    def _start_face_indexing(self) -> None:
        if self._face_indexer and self._face_indexer.isRunning():
            return
        if self._face_indexer is not None:
            self._face_indexer.deleteLater()
        self._face_indexer = FaceIndexThread(self._face_db, self._catalog, self)
        self._face_indexer.progress.connect(self._on_face_progress)
        self._face_indexer.cluster_requested.connect(self._run_clustering)
        self._face_indexer.finished.connect(self._on_face_indexing_finished)
        self._face_indexer.unavailable.connect(self._on_face_unavailable)
        self._face_indexer.error.connect(
            lambda path, msg: logger.warning("Visage non indexé %s: %s", path, msg)
        )
        self._config.set("faces.auto_index", True)
        self._act_index_faces.setText("Analyse en cours…")
        self._act_index_faces.setEnabled(False)
        self._face_indexer.start()

    def _import_from_picasa(self) -> None:
        from src.ui.picasa_import_dialog import PicasaImportDialog
        dlg = PicasaImportDialog(
            self._config, self._catalog, self._face_db, self._edit_db, self,
            on_edits_imported=self._on_picasa_edits_imported,
        )
        dlg.exec()

    def _on_picasa_edits_imported(self, edited_map: dict) -> None:
        for path, edit_info in edited_map.items():
            self._grid.refresh_photo(path, edit_info)

    @Slot(int, int)
    def _on_face_progress(self, current: int, total: int) -> None:
        if current == 0:
            self._lbl_action.setText("Initialisation de l'analyse des visages…")
        else:
            self._lbl_action.setText(f"Analyse visages… {current}/{total}")

    @Slot(int, int)
    def _on_face_indexing_finished(self, indexed: int, faces: int) -> None:
        self._lbl_action.setText("")
        self._act_index_faces.setText("Analyser les visages")
        self._act_index_faces.setEnabled(True)
        if faces > 0:
            self._run_clustering()

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
        if n_clusters > 0:
            self._lbl_action.setText(f"{n_clusters} groupe(s) de visages détecté(s)")
            QTimer.singleShot(4000, lambda: self._lbl_action.setText(""))
        self._refresh_persons()
        if self._face_panel.isVisible():
            self._face_panel_refresh_timer.start()

    @Slot()
    def _on_face_unavailable(self) -> None:
        self._lbl_action.setText("")
        self._act_index_faces.setText("Analyser les visages")
        self._act_index_faces.setEnabled(True)
        self._config.set("faces.auto_index", False)
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

    @Slot(int, int)
    def _on_cluster_assigned(self, cluster_id: int, person_id: int) -> None:
        self._face_db.assign_person_to_cluster(cluster_id, person_id)
        self._refresh_persons()
        self._face_cluster_grid.remove_clusters([cluster_id])
        self._refresh_face_panel_if_visible()

    @Slot(list, str)
    def _on_clusters_named(self, cluster_ids: list, name: str) -> None:
        person = self._catalog.create_person(name)
        for cid in cluster_ids:
            self._face_db.assign_person_to_cluster(cid, person.id)
        self._refresh_persons()
        self._face_cluster_grid.remove_clusters(cluster_ids)
        self._refresh_face_panel_if_visible()

    @Slot(list, int)
    def _on_clusters_assigned(self, cluster_ids: list, person_id: int) -> None:
        for cid in cluster_ids:
            self._face_db.assign_person_to_cluster(cid, person_id)
        self._refresh_persons()
        self._face_cluster_grid.remove_clusters(cluster_ids)
        self._refresh_face_panel_if_visible()

    @Slot(int)
    def _on_cluster_ignored(self, _cluster_id: int) -> None:
        self._face_cluster_grid.remove_clusters([_cluster_id])

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
        self._refresh_persons()

    @Slot(object)
    def _on_person_rename_requested(self, person: PersonInfo) -> None:
        name, ok = QInputDialog.getText(
            self, "Renommer la personne", "Nouveau nom :", text=person.name
        )
        if ok and name.strip() and name.strip() != person.name:
            self._catalog.rename_person(person.id, name.strip())
            self._refresh_persons()

    @Slot(object)
    def _on_person_selected(self, person: PersonInfo) -> None:
        self._grid_nav_bar.hide()
        self.show_person_clusters(person)

    def _refresh_persons(self) -> None:
        if self._persons_refresh_thread and self._persons_refresh_thread.isRunning():
            return  # un refresh est déjà en cours
        if self._persons_refresh_thread is not None:
            self._persons_refresh_thread.deleteLater()
        self._persons_refresh_thread = _PersonsRefreshThread(self._catalog, self._face_db, self)
        self._persons_refresh_thread.result_ready.connect(self._on_persons_refreshed)
        self._persons_refresh_thread.start()

    @Slot(list, int)
    def _on_persons_refreshed(self, persons: list, count: int) -> None:
        self._sidebar.refresh_persons(persons)
        self._sidebar.update_cluster_badge(count)

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

    def _on_face_highlighted(self, face) -> None:
        self._viewer.highlight_face(face)

    def _on_all_faces_toggled(self, faces: list) -> None:
        self._viewer.set_all_highlighted_faces(faces)

    def _on_face_context_menu(self, face, gpos) -> None:
        self._face_panel.show_face_context_menu(face, gpos)

    def _on_face_panel_person_cluster_requested(self, person_id: int) -> None:
        """Double-clic sur un visage nommé dans le panneau → vue clusters de la personne."""
        person = self._catalog.get_person(person_id)
        if person is None:
            return
        self._face_db.enrich_persons([person])
        self.show_person_clusters(person)

    def _on_red_eye_mode_requested(self, active: bool, radius: float) -> None:
        if active:
            self._viewer.enter_red_eye_mode(radius)
        else:
            self._viewer.exit_red_eye_mode()

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
        else:
            self._exif_panel.hide()
            if not self._face_panel.isVisible():
                self._right_panel.hide()

    def _update_nav_arrows(self) -> None:
        n = len(self._current_photos)
        self._viewer.update_nav_arrows(
            has_prev=self._current_photo_index > 0,
            has_next=self._current_photo_index < n - 1,
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
        self._update_viewer_status(photo)
        self._update_nav_arrows()

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

    def _start_photo_query(self, fn, context_key: str) -> None:
        """Lance une requête photo en arrière-plan et met à jour la grille à l'arrivée."""
        if self._photo_query_thread is not None:
            if self._photo_query_thread.isRunning():
                self._photo_query_thread.photos_ready.disconnect()
                self._photo_query_thread.finished.connect(self._photo_query_thread.deleteLater)
            else:
                self._photo_query_thread.deleteLater()
        self._photo_query_thread = _PhotoQueryThread(fn, context_key, self)
        self._photo_query_thread.photos_ready.connect(self._on_photo_query_ready)
        self._photo_query_thread.start()

    @Slot(list, str)
    def _on_photo_query_ready(self, photos: list, context_key: str) -> None:
        self._current_photos  = photos
        self._current_paths   = {p.path for p in photos}
        self._current_context = context_key
        self._grid.set_photos(photos)
        self._update_status()

    @Slot(str)
    def _on_folder_selected(self, folder: str) -> None:
        self._grid.set_ribbon_mode(False)
        self._grid.set_date_overlay_visible(False)
        self._grid_nav_bar.hide()
        self.show_grid()
        self._start_photo_query(
            lambda: self._catalog.get_photos_in_folder(folder),
            folder,
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
        self._config.remove_scan_folder(folder)
        remaining = self._config.get_scan_folders()
        self._sidebar.refresh_folders(remaining)
        self._folder_watcher.set_folders(remaining)

    @Slot(list, str)
    def _on_photos_dropped(self, file_paths: list, dest_folder: str) -> None:
        """Déplace les fichiers glissés vers dest_folder et met à jour toutes les références."""
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
            photos = self._catalog.get_photos_in_folder(dest_folder)
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

    def _on_album_create(self, name: str) -> None:
        album = self._catalog.create_album(name)
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)

    def _on_bus_photo_selected(self, photo: PhotoInfo) -> None:
        pass

    @Slot()
    def _on_search_text_changed(self) -> None:
        self._search_timer.start()

    @Slot()
    def _do_search(self) -> None:
        query = self._search_box.text().strip()
        if not query:
            self._show_all_photos()
            return
        self._start_photo_query(
            lambda: self._catalog.search(query),
            f"Recherche: {query}",
        )

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
        self._lbl_fileinfo.setText("")
        self._update_status()

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
        self._current_photos = [photo]
        self._current_photo_index = 0
        self.show_viewer(photo)

    def _on_person_cluster_back(self) -> None:
        """Bouton ← Retour dans PersonClusterView → retour à la grille principale."""
        self._grid_nav_bar.hide()
        self.show_grid()

    @Slot(int)
    def _on_pcv_cluster_unassigned(self, _cluster_id: int) -> None:
        """Groupe dé-associé depuis PersonClusterView (DB déjà à jour) → rafraîchir la sidebar."""
        self._refresh_persons()
        self._refresh_face_panel_if_visible()

    def show_viewer(self, photo: PhotoInfo) -> None:
        is_video = photo.media_type == "video"
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
        if self._face_panel.isVisible():
            self._face_panel.set_photo(photo.path)
        if self._exif_panel.isVisible():
            self._exif_panel.set_photo(photo.path)
        self._update_viewer_status(photo)
        self._update_nav_arrows()

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
        win = SlideshowWindow(
            photos=self._current_photos,
            start_index=self._current_photo_index,
            edit_db=self._edit_db,
            parent=self,
        )
        win.show()

    def _show_help(self) -> None:
        from src.ui.help_dialog import HelpDialog
        dlg = HelpDialog(self)
        dlg.exec()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "À propos de PixelPhotoManager",
            "PixelPhotoManager v1.0\n\nGestionnaire de photos non destructif.\nPySide6 · Pillow · SQLite",
        )

    @Slot(str, object)
    def _on_photo_saved(self, photo_path: str, edit) -> None:
        self._grid.refresh_photo(photo_path, edit)

    @Slot(str)
    def _on_exif_photo_saved(self, photo_path: str) -> None:
        """Mise à jour du catalogue après modification EXIF (date_taken peut avoir changé)."""
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

    def _on_single_reindex_finished(self, photo_path: str, _face_count: int) -> None:
        if self._face_panel.isVisible():
            self._face_panel.set_photo(photo_path)

    @Slot(list)
    def _on_delete_requested(self, photos: list) -> None:
        if not photos:
            return

        if not self._config.get("ui.delete_no_confirm", False):
            n = len(photos)
            if n == 1:
                msg = f"Supprimer définitivement « {photos[0].filename} » ?\n\nCette action est irréversible."
            else:
                msg = f"Supprimer définitivement {n} fichiers sélectionnés ?\n\nCette action est irréversible."
            box = QMessageBox(QMessageBox.Warning, "Confirmer la suppression", msg,
                              QMessageBox.Yes | QMessageBox.Cancel, self)
            box.setDefaultButton(QMessageBox.Cancel)
            chk = QCheckBox("Ne plus demander de confirmation")
            box.setCheckBox(chk)
            if box.exec() != QMessageBox.Yes:
                return
            if chk.isChecked():
                self._config.set("ui.delete_no_confirm", True)

        # Mémoriser l'état du viewer avant la suppression
        in_viewer = self._stack.currentIndex() == 1
        viewed_index = self._current_photo_index
        deleted_paths_set = {p.path for p in photos}

        # Index du premier fichier supprimé (pour recentrer le ruban après)
        first_deleted_idx = next(
            (i for i, p in enumerate(self._current_photos) if p.path in deleted_paths_set),
            None,
        )

        deleted: list[str] = []
        errors: list[str] = []
        for photo in photos:
            try:
                Path(photo.path).unlink(missing_ok=True)
                self._catalog.delete_photo(photo.path)
                self._thumb_cache.invalidate(photo.path)
                deleted.append(photo.path)
            except Exception as e:
                errors.append(f"{photo.filename}: {e}")
        if deleted:
            self._grid.remove_photos(deleted)
            deleted_set = set(deleted)
            self._current_photos = [p for p in self._current_photos
                                    if p.path not in deleted_set]
            self._current_paths -= deleted_set
            self._update_status()
            for path in deleted:
                self._face_db.delete_for_path(path)

            # Si le viewer affichait une photo supprimée, naviguer vers le voisin
            if in_viewer and any(p in deleted_paths_set
                                 for p in [self._viewer.current_photo().path]
                                 if self._viewer.current_photo()):
                if not self._current_photos:
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

    @staticmethod
    def _preserve_file_dates(src_stat, dst_path: str) -> None:
        """Copie atime, mtime et date de création (Windows) de src_stat vers dst_path."""
        os.utime(dst_path, (src_stat.st_atime, src_stat.st_mtime))
        try:
            import ctypes
            import ctypes.wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime",  ctypes.wintypes.DWORD),
                             ("dwHighDateTime", ctypes.wintypes.DWORD)]

            # Convertir timestamp Unix → FILETIME (100 ns depuis le 1er janvier 1601)
            val = int((src_stat.st_ctime + 11644473600) * 10_000_000)
            ft = FILETIME(dwLowDateTime=val & 0xFFFFFFFF,
                          dwHighDateTime=(val >> 32) & 0xFFFFFFFF)

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateFileW(
                dst_path, 0x40000000, 1, None, 3, 0x02000000, None
            )
            if handle not in (-1, 0):
                kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
                kernel32.CloseHandle(handle)
        except Exception:
            pass   # non-Windows ou droits insuffisants : mtime suffit

    def _export_image(self, photo: PhotoInfo, dest: str) -> None:
        """Exporte l'image traitée pleine résolution vers dest."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            from PIL import Image, ImageOps
            from src.processing.adjustments import ImageAdjuster

            orig_stat = os.stat(photo.path)

            edit = self._edit_db.load(photo.path)
            with Image.open(photo.path) as img:
                img = ImageOps.exif_transpose(img)
                if edit.is_modified():
                    img = ImageAdjuster.apply_all(img, edit)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                if Path(dest).suffix.lower() == ".png":
                    img.save(dest, format="PNG")
                else:
                    if img.mode == "RGBA":
                        img = img.convert("RGB")
                    img.save(dest, format="JPEG", quality=95, subsampling=0)

            # Restaurer les dates du fichier original (atime, mtime et date de création)
            self._preserve_file_dates(orig_stat, dest)

            if os.path.normpath(dest) == os.path.normpath(photo.path):
                # Les retouches sont maintenant baked dans le fichier : supprimer l'edit
                # et rafraîchir l'UI pour éviter une double application au prochain chargement
                self._edit_db.delete(photo.path)
                self._thumb_cache.invalidate(photo.path)
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
                        if img.mode not in ("RGB", "RGBA"):
                            img = img.convert("RGB")
                        # Résolution du nom de fichier de destination
                        dest = export_dir / Path(photo.path).name
                        if dest.exists():
                            stem, suffix = dest.stem, dest.suffix
                            n = 1
                            while dest.exists():
                                dest = export_dir / f"{stem}_{n}{suffix}"
                                n += 1
                        orig_stat = os.stat(photo.path)
                        if dest.suffix.lower() == ".png":
                            img.save(str(dest), format="PNG")
                        else:
                            if img.mode == "RGBA":
                                img = img.convert("RGB")
                            img.save(str(dest), format="JPEG",
                                     quality=quality, subsampling=0)
                        self._preserve_file_dates(orig_stat, str(dest))
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

    def closeEvent(self, event) -> None:
        self._config.set("ui.window_width", self.width())
        self._config.set("ui.window_height", self.height())
        self._config.set("ui.sidebar_width", self._left_stack.width())
        import base64
        self._config.set(
            "ui.splitters.viewer",
            base64.b64encode(self._viewer_splitter.saveState().data()).decode(),
        )
        self._config.set(
            "ui.splitters.sidebar_panels",
            self._sidebar.save_splitter_state(),
        )
        self._folder_watcher.set_folders([])
        self._scanner.stop()
        if self._face_indexer and self._face_indexer.isRunning():
            self._face_indexer.stop()
            self._face_indexer.wait(3000)
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
            self._cluster_thread.wait(500)
        if self._photo_query_thread and self._photo_query_thread.isRunning():
            self._photo_query_thread.wait(1000)
        if self._persons_refresh_thread and self._persons_refresh_thread.isRunning():
            self._persons_refresh_thread.wait(1000)
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        in_viewer = self._stack.currentIndex() == 1

        if key == Qt.Key_F9:
            self.toggle_sidebar()
        elif key == Qt.Key_F and modifiers == Qt.ControlModifier:
            self._search_box.setFocus()
            self._search_box.selectAll()
        elif key == Qt.Key_A and modifiers == Qt.ControlModifier:
            if self._stack.currentIndex() == 0:
                self._grid.select_all()
        elif in_viewer and key == Qt.Key_Right and not self._viewer._canvas._crop_mode:
            self._navigate_photo(-1)   # plus récente
        elif in_viewer and key == Qt.Key_Left and not self._viewer._canvas._crop_mode:
            self._navigate_photo(1)    # plus ancienne
        else:
            super().keyPressEvent(event)
