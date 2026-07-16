# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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
    QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFrame, QGroupBox,
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
    """Clé de tri chronologique : date_taken, puis file_mtime en fallback."""
    if p.date_taken:
        return p.date_taken
    if p.file_mtime:
        return datetime.fromtimestamp(p.file_mtime)
    return datetime.min


def _photo_filename_sort_key(p: "PhotoInfo"):
    """Clé de tri alphabétique (nom de fichier, insensible à la casse)."""
    return (p.filename or "").lower()


class _CatalogLoadThread(QThread):
    """Charge get_all_photos() hors du thread UI et émet les résultats par lots."""

    batch_ready = Signal(list)  # list[PhotoInfo]

    def __init__(self, catalog: "Catalog", batch_size: int = 300, reverse: bool = False, parent=None):
        super().__init__(parent)
        self._catalog = catalog
        self._batch_size = batch_size
        self._reverse = reverse
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        # get_all_photos() est trié chronologique descendant (SQL) ; "reverse"
        # inverse en ascendant pour suivre le réglage "Ordre d'affichage" —
        # la vue "Toutes les photos" reste toujours chronologique, seule la
        # direction est configurable (cf. MainWindow._sort_photos_for_display).
        photos = self._catalog.get_all_photos()
        if self._reverse:
            photos = list(reversed(photos))
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


class _ResuggestThread(QThread):
    """Recalcule les suggestions après le rejet d'un cluster, dans un thread secondaire."""

    def __init__(self, face_db, cluster_ids: list, exclude_pid, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._cluster_ids = cluster_ids
        self._exclude_pid = exclude_pid

    def run(self) -> None:
        self._face_db.resuggest_clusters(self._cluster_ids, self._exclude_pid)


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


class _DuplicatesPopup(QFrame):
    """Popup flottante listant tous les exemplaires d'un groupe de doublons
    (original inclus). Fenêtre de type Qt.Popup : se ferme automatiquement au
    clic en dehors d'elle (comme un menu), en plus du bouton « Fermer ».
    Cliquer sur un exemplaire navigue directement (signal navigate_requested)
    sans fermer la popup, pour permettre de comparer plusieurs exemplaires
    de suite.

    Déplaçable par cliquer-glisser (titre ou fond de la popup) : une popup
    sans barre de titre (Qt.Popup) reste sinon coincée là où elle s'ouvre,
    ce qui peut masquer une partie importante de la photo comparée."""

    navigate_requested = Signal(str)  # chemin de la photo cible

    def __init__(self, photo, others: list, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("duplicatesPopup")
        self.setStyleSheet(
            "#duplicatesPopup { background: #262626; border: 1px solid #555; border-radius: 6px; }"
            "QLabel { color: #ddd; }"
        )
        self.setMinimumWidth(440)
        self._drag_offset: QPoint | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        n = len(others)
        title = QLabel(f"{n + 1} exemplaire{'s' if n + 1 != 1 else ''} dans ce groupe de doublons :")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        title.setWordWrap(True)
        title.setCursor(Qt.SizeAllCursor)
        title.setToolTip("Cliquer-glisser pour déplacer la fenêtre")
        title.installEventFilter(self)
        layout.addWidget(title)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setMinimumHeight(140)
        self._list.setMaximumHeight(320)
        self._add_entry(photo, is_original=True)
        for p in others:
            self._add_entry(p, is_original=False)
        self._list.itemClicked.connect(self._on_navigate)
        layout.addWidget(self._list)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("Fermer")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _add_entry(self, p, is_original: bool) -> None:
        size = _fmt_size(p.file_size) or "—"
        prefix = "★ Original — " if is_original else ""
        item = QListWidgetItem(f"{prefix}{p.filename}\n{p.directory}\n{size}")
        item.setData(Qt.UserRole, p.path)
        item.setToolTip(p.path)
        if is_original:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        self._list.addItem(item)

    def _on_navigate(self, item) -> None:
        path = item.data(Qt.UserRole)
        if path:
            self.navigate_requested.emit(path)

    def eventFilter(self, obj, event) -> bool:
        # Le titre est un enfant (QLabel) : les événements souris qui
        # l'atteignent ne remontent pas naturellement au QFrame parent, d'où
        # ce filtre pour le rendre lui aussi déplaçable (cf. mousePressEvent/
        # mouseMoveEvent ci-dessous pour le reste de la popup).
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            return True
        if event.type() == QEvent.MouseMove and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return True
        if event.type() == QEvent.MouseButtonRelease:
            self._drag_offset = None
            return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


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
        self._retry_face_thread: RetryFaceIndexThread | None = None
        self._duplicate_thread: DuplicateDetectorThread | None = None
        self._live_corrupted_paths: list[str] = []
        self._last_duplicate_check: datetime | None = None
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
        self._from_person_cluster_view: bool = False
        self._viewer_back_target: str = "grid"  # "grid" | "person_cluster_view" | "duplicate_grid"
        # Filtre global de session (pas persisté) pour le calque d'annotations
        self._annotations_globally_visible: bool = True

        self._current_photos: list[PhotoInfo] = []
        self._current_paths: set[str] = set()
        self._current_photo_index: int = 0
        self._current_context: str = ""   # dossier ou album actif
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
        QTimer.singleShot(
            0, lambda: self._sidebar.update_duplicates_badge(self._catalog.count_duplicate_groups())
        )
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
        self._btn_undo = QPushButton("↩ Annuler")
        self._btn_undo.setToolTip("Annuler la dernière action sur les visages")
        self._btn_undo.setEnabled(False)
        self._btn_undo.clicked.connect(self._on_undo_clicked)
        self._btn_undo.setVisible(False)
        lay.addWidget(self._btn_undo)
        self._act_undo = self._btn_undo          # alias de compatibilité

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
        self._grid.save_requested.connect(self._on_save_requested)
        self._grid.duplicate_clicked.connect(self._on_duplicate_badge_clicked)
        self._grid.add_to_album_requested.connect(self._on_add_to_album)
        self._grid.create_album_with_requested.connect(self._on_create_album_with)
        self._grid.retry_face_index_requested.connect(self._on_retry_face_index_requested)

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
        self._viewer.closed.connect(self._on_viewer_closed)
        self._viewer.navigate.connect(self._navigate_photo)
        self._viewer.zoom_changed.connect(self._on_viewer_zoom_changed)
        self._viewer.save_requested.connect(self._on_save_requested)
        self._viewer.rename_requested.connect(self._on_rename_requested)
        self._viewer.move_requested.connect(self._on_move_requested)
        self._viewer.delete_requested.connect(self._on_delete_requested)
        self._viewer.force_redetect_requested.connect(self._on_force_redetect_requested)
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
        self._face_panel.undo_stack_changed.connect(self._on_face_undo_stack_changed)
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
        self._grid.set_ribbon_mode(True)
        self._grid.set_date_overlay_visible(True)
        self._grid.set_photos([])
        self._grid_nav_bar.hide()
        self.show_grid()
        self._update_status()

        ascending = self._config.get("display_order.grid_dir", "desc") == "asc"
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

        # Le scan ajoute les nouvelles photos dans l'ordre filesystem (non trié).
        # On re-trie la liste courante selon le réglage "Ordre d'affichage".
        # Applicable à "Toutes les photos" et aux vues dossier (les vues spéciales
        # comme Favoris, Vidéos ou Person ne reçoivent pas de photos via _on_photos_batch).
        if self._current_photos and not self._current_context.startswith(_PERSON_CTX_PREFIX):
            self._current_photos = self._sort_photos_for_display(
                self._current_photos, self._current_context
            )
            self._grid.set_photos(self._current_photos)

        if self._warmup_thread and self._warmup_thread.isRunning():
            self._lbl_action.setText("Initialisation de la reconnaissance faciale…")
            self._face_index_pending = True
        else:
            self._start_face_indexing()
        self._start_duplicate_detection()

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

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._config, self)
        dlg.recluster_needed.connect(self._run_clustering)
        dlg.exec()

    def _open_external_apps_dialog(self) -> None:
        """Dialogue de configuration des applications externes accessibles depuis le viewer."""
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
            lst.addItem(f"{app['name']}   —   {app['path']}")

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
            if ok and name.strip():
                apps.append({"name": name.strip(), "path": path})
                lst.addItem(f"{name.strip()}   —   {path}")

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

    def _on_folder_rescan_requested(self, folder: str) -> None:
        self._start_scan([folder], force=True)

    def _on_folder_added_from_manager(self, folder: str) -> None:
        self._config.add_scan_folder(folder)
        all_folders = self._config.get_scan_folders()
        self._sidebar.refresh_folders(all_folders)
        self._start_scan([folder])
        self._folder_watcher.set_folders(all_folders)
        self._maybe_prompt_picasa_for_new_folder(folder)

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

    def _start_similarity_search(self) -> None:
        """Compare les centroïdes des groupes non identifiés aux personnes nommées.

        Déclenché automatiquement par _on_clustering_finished() juste après
        qu'un regroupement a formé de nouveaux groupes — aucune interaction
        utilisateur, juste un message dans la barre de statut à la fin.
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
            self._run_clustering()

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
        if n_clusters > 0:
            # De nouveaux groupes viennent d'être formés : on compare aussitôt
            # leurs centroïdes aux personnes nommées, sans interaction utilisateur.
            self._start_similarity_search()
        else:
            self._lbl_action.setText("")

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

    @Slot(int, int)
    def _on_cluster_assigned(self, cluster_id: int, person_id: int) -> None:
        self._face_db.assign_person_to_cluster(cluster_id, person_id)
        self._update_persons_counts()
        self._face_cluster_grid.remove_clusters([cluster_id])
        self._refresh_face_panel_if_visible()

    @Slot(list, str)
    def _on_clusters_named(self, cluster_ids: list, name: str) -> None:
        person = self._catalog.create_person(name)
        for cid in cluster_ids:
            self._face_db.assign_person_to_cluster(cid, person.id)
        self._refresh_persons()  # nouvelle personne créée → rebuild complet
        self._face_cluster_grid.remove_clusters(cluster_ids)
        self._refresh_face_panel_if_visible()

    @Slot(list, int)
    def _on_clusters_assigned(self, cluster_ids: list, person_id: int) -> None:
        for cid in cluster_ids:
            self._face_db.assign_person_to_cluster(cid, person_id)
        self._update_persons_counts()
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

    @Slot(bool)
    def _on_add_face_mode_requested(self, enter: bool) -> None:
        """Bouton 'Ajouter une personne' du FacePanel — bascule le mode dessin
        de bboxe dans la visionneuse."""
        if enter:
            self._viewer.enter_face_add_mode()
        else:
            self._viewer.cancel_face_add_mode()

    @Slot(bool)
    def _on_face_undo_stack_changed(self, can_undo: bool) -> None:
        self._btn_undo.setEnabled(can_undo)
        if can_undo and self._face_panel._undo_stack:
            desc = self._face_panel._undo_stack[-1][0]
            self._btn_undo.setToolTip(f"Annuler : {desc}")
        else:
            self._btn_undo.setToolTip("Annuler la dernière action sur les visages")

    @Slot()
    def _on_undo_clicked(self) -> None:
        self._face_panel.undo()

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
        self._cancel_grid_display_ops()
        self._photo_query_thread = _PhotoQueryThread(fn, context_key, self)
        self._photo_query_thread.photos_ready.connect(self._on_photo_query_ready)
        self._photo_query_thread.start()

    @Slot(list, str)
    def _on_photo_query_ready(self, photos: list, context_key: str) -> None:
        photos = self._sort_photos_for_display(photos, context_key)
        self._current_photos  = photos
        self._current_paths   = {p.path for p in photos}
        self._current_context = context_key
        self._grid.set_photos(photos)
        self._update_status()

    def _sort_photos_for_display(self, photos: list, context: str) -> list:
        """Applique le réglage "Ordre d'affichage" (menu Affichage) à une liste
        de photos avant affichage dans la grille. La vue "Toutes les photos"
        (Chronologie) reste toujours triée chronologiquement — seule sa
        direction suit le réglage — car un tri alphabétique n'a pas de sens
        pour un album qui s'appelle "Chronologie"."""
        if context == "Toutes les photos":
            mode = "chrono"
        else:
            mode = self._config.get("display_order.grid_mode", "chrono")
        direction = self._config.get("display_order.grid_dir", "desc")
        key_fn = _photo_sort_key if mode == "chrono" else _photo_filename_sort_key
        return sorted(photos, key=key_fn, reverse=(direction == "desc"))

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

    def _start_duplicate_detection(self) -> None:
        if self._duplicate_thread and self._duplicate_thread.isRunning():
            return

        paths = self._catalog.get_all_photo_paths_for_dedup()
        if not paths:
            return

        seed_groups = self._catalog.get_duplicate_group_assignments()

        detector = DuplicateDetectorThread(paths, seed_groups=seed_groups, parent=self)
        self._duplicate_thread = detector

        def _on_partial(groups: dict, corrupted: list):
            self._update_corrupted_indicator(corrupted)
            self._apply_duplicate_results(groups, corrupted, seed_groups=seed_groups)

        def _on_done(groups: dict):
            self._duplicate_grid.set_scanning(False)
            self._last_duplicate_check = datetime.now()
            self._apply_duplicate_results(groups, detector.corrupted_paths, seed_groups=seed_groups)

        def _on_error(msg: str):
            logger.warning("Détection de doublons : %s", msg)
            self._duplicate_grid.set_scanning(False)

        def _on_cancelled():
            self._duplicate_grid.set_scanning(False)

        detector.partial_results.connect(_on_partial)
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
            self._lbl_corrupted.setText(f"⚠ {n} fichier{'s' if n != 1 else ''} corrompu{'s' if n != 1 else ''}")
            self._lbl_corrupted.show()
        else:
            self._lbl_corrupted.hide()

    def _show_corrupted_list_dialog(self, _checked: bool = False) -> None:
        corrupted_paths = self._live_corrupted_paths
        if not corrupted_paths:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Fichiers corrompus")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"{len(corrupted_paths)} fichier(s) n'ont pas pu être lu(s) "
                            "pendant l'analyse en cours (probablement corrompu(s)) :"))
        list_widget = QListWidget()
        list_widget.addItems(corrupted_paths)
        v.addWidget(list_widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        btn_repair = buttons.addButton("Réparer…", QDialogButtonBox.ActionRole)
        btn_repair.clicked.connect(lambda: self._offer_corrupted_repair(list(corrupted_paths)))
        btn_delete = buttons.addButton("Supprimer…", QDialogButtonBox.ActionRole)
        btn_delete.clicked.connect(lambda: self._offer_corrupted_delete(list(corrupted_paths)))
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        buttons.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        v.addWidget(buttons)
        dlg.resize(600, 400)
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
        l'ancien déclenchement manuel avec rapport de fin."""
        running = bool(self._duplicate_thread and self._duplicate_thread.isRunning())

        n_groups = self._catalog.count_duplicate_groups()
        n_photos = len(self._catalog.get_duplicate_group_assignments())
        n_corrupted = len(self._live_corrupted_paths)

        dlg = QDialog(self)
        dlg.setWindowTitle("État des doublons")
        v = QVBoxLayout(dlg)

        v.addWidget(QLabel(f"{n_groups} groupe{'s' if n_groups != 1 else ''} de doublons "
                            f"({n_photos} photo{'s' if n_photos != 1 else ''} concernée"
                            f"{'s' if n_photos != 1 else ''})."))

        if running:
            status_text = "Analyse en cours…"
        elif self._last_duplicate_check is not None:
            status_text = f"Dernière vérification : {self._last_duplicate_check:%d/%m/%Y %H:%M}"
        else:
            status_text = "Dernière vérification : jamais"
        v.addWidget(QLabel(status_text))

        if n_corrupted:
            corrupted_row = QHBoxLayout()
            corrupted_row.addWidget(QLabel(
                f"⚠ {n_corrupted} fichier{'s' if n_corrupted != 1 else ''} corrompu"
                f"{'s' if n_corrupted != 1 else ''}"
            ))
            btn_corrupted = QPushButton("Voir la liste…")
            btn_corrupted.clicked.connect(lambda: (dlg.accept(), self._show_corrupted_list_dialog()))
            corrupted_row.addWidget(btn_corrupted)
            v.addLayout(corrupted_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        btn_groups = buttons.addButton("Voir les groupes", QDialogButtonBox.ActionRole)
        btn_groups.clicked.connect(lambda: (dlg.accept(), self.show_duplicate_grid()))
        btn_check_now = buttons.addButton("Vérifier maintenant", QDialogButtonBox.ActionRole)
        btn_check_now.setEnabled(not running)
        btn_check_now.clicked.connect(lambda: (dlg.accept(), self._start_duplicate_detection()))
        buttons.rejected.connect(dlg.reject)
        buttons.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        v.addWidget(buttons)

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

    def _offer_corrupted_repair(self, corrupted_paths: list) -> None:
        n = len(corrupted_paths)
        reply = QMessageBox.question(
            self,
            "Réparer les fichiers corrompus",
            f"{n} fichier{'s' if n != 1 else ''} semble{'nt' if n != 1 else ''} corrompu"
            f"{'s' if n != 1 else ''}.\n\n"
            "Tenter une réparation automatique ? PixelPhotoManager va essayer de "
            "ré-enregistrer une copie propre de chaque fichier via un décodeur plus "
            "tolérant, en conservant les dates de création et de modification "
            "Windows. L'original est sauvegardé avant toute modification "
            "(dossier caché .tmp_originals à côté du fichier).",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            list_path = self._record_corrupted_files(n, 0, corrupted_paths)
            msg = "La réparation n'a pas été lancée."
            if list_path:
                msg += (f"\n\nLa liste des {n} fichier{'s' if n != 1 else ''} est "
                        "disponible via Outils › Historique des problèmes.")
            QMessageBox.information(self, "Réparation annulée", msg)
            return

        from PySide6.QtWidgets import QProgressDialog
        from src.library.file_repair import FileRepairThread

        progress = QProgressDialog("Réparation en cours…", "Annuler", 0, n, self)
        progress.setWindowTitle("Réparation des fichiers corrompus")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        thread = FileRepairThread(corrupted_paths, self)

        def _on_progress(cur, total, path):
            progress.setValue(cur)
            progress.setLabelText(f"Réparation {cur + 1}/{total} :\n{os.path.basename(path)}")

        def _on_finished(repaired_count, still_failed):
            progress.setValue(n)
            progress.close()
            list_path = self._record_corrupted_files(n, repaired_count, still_failed)
            msg = f"{repaired_count} fichier{'s' if repaired_count != 1 else ''} réparé{'s' if repaired_count != 1 else ''} sur {n}."
            if still_failed:
                msg += (f"\n\n{len(still_failed)} fichier(s) n'ont pas pu être réparés.")
                if list_path:
                    msg += "\nLa liste est disponible via Outils › Historique des problèmes."
            QMessageBox.information(self, "Réparation terminée", msg)

        thread.progress.connect(_on_progress)
        thread.finished.connect(_on_finished)
        progress.canceled.connect(thread.cancel)

        thread.start()

    def _offer_corrupted_delete(self, corrupted_paths: list) -> None:
        n = len(corrupted_paths)
        reply = QMessageBox.question(
            self,
            "Supprimer les fichiers corrompus",
            f"{n} fichier{'s' if n != 1 else ''} semble{'nt' if n != 1 else ''} corrompu"
            f"{'s' if n != 1 else ''}.\n\n"
            f"Supprimer définitivement {'ce fichier' if n == 1 else 'ces fichiers'} ?\n\n"
            "Cette action est irréversible.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return

        deleted: list[str] = []
        errors: list[str] = []
        for path in corrupted_paths:
            try:
                Path(path).unlink(missing_ok=True)
                self._catalog.delete_photo(path)
                self._thumb_cache.invalidate(path)
                self._face_db.delete_for_path(path)
                deleted.append(path)
            except Exception as e:
                errors.append(f"{os.path.basename(path)} : {e}")

        if deleted:
            deleted_set = set(deleted)
            self._grid.remove_photos(deleted)
            self._current_photos = [p for p in self._current_photos if p.path not in deleted_set]
            self._current_paths -= deleted_set
            self._update_status()

            remaining_corrupted = [p for p in self._live_corrupted_paths if p not in deleted_set]
            self._update_corrupted_indicator(remaining_corrupted)

        if errors:
            QMessageBox.warning(self, "Erreurs de suppression",
                                "Impossible de supprimer :\n" + "\n".join(errors))
        elif deleted:
            QMessageBox.information(
                self, "Suppression terminée",
                f"{len(deleted)} fichier{'s' if len(deleted) != 1 else ''} supprimé"
                f"{'s' if len(deleted) != 1 else ''}.",
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
        self._viewer_back_target = "duplicate_grid"
        self.show_viewer(photos[0])

    def _on_duplicate_group_ignored(self, group_id: int) -> None:
        """Bouton ✗ sur une carte de DuplicateGrid : dissout le groupe entier (non persistant)."""
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
        added = 0
        for photo in photos:
            if photo.id is not None:
                self._catalog.add_photo_to_album(album.id, photo.id)
                added += 1
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
        added = 0
        for photo in photos:
            if photo.id is not None:
                self._catalog.add_photo_to_album(album.id, photo.id)
                added += 1
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
        self._act_undo.setVisible(False)
        self._act_faces_toggle.setVisible(False)
        self._act_exif_toggle.setVisible(False)
        self._btn_annotations_toggle.setVisible(False)
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
        self._btn_annotations_toggle.setVisible(False)
        self._lbl_fileinfo.setText("")
        self._lbl_action.setText("")

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
        self._viewer_back_target = "person_cluster_view"
        self.show_viewer(photo)

    def _on_person_cluster_back(self) -> None:
        """Bouton ← Retour dans PersonClusterView → retour à la grille principale."""
        self._grid_nav_bar.hide()
        self.show_grid()

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
        self._act_undo.setVisible(True)
        self._act_faces_toggle.setVisible(True)
        self._act_exif_toggle.setVisible(True)
        self._btn_annotations_toggle.setVisible(True)
        # Un nouveau _Canvas ne connaît pas spontanément l'état de session.
        self._viewer.set_annotations_visible(self._annotations_globally_visible)
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
        self._force_redetect_thread = ForceRedetectThread(self._face_db, photo.path, self)
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

        # Groupes de doublons concernés par cette suppression : si la suppression
        # fait passer un groupe sous 2 exemplaires, ce n'est plus un doublon.
        affected_groups = {p.duplicate_group_id for p in photos if p.duplicate_group_id is not None}

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

            # Dissoudre les groupes de doublons devenus des singletons (ou vides)
            # suite à cette suppression : sinon la carte reste affichée dans
            # DuplicateGrid pour un groupe qui n'a plus lieu d'être.
            stale_groups = []
            for gid in affected_groups:
                if len(self._catalog.get_duplicates_for_group(gid)) < 2:
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
        if ctx.startswith(f"{_PERSON_CTX_PREFIX}cluster_"):
            return {"type": "all"}   # vue transitoire, pas de restauration
        if ctx.startswith(_PERSON_CTX_PREFIX):
            try:
                return {"type": "person", "value": int(ctx[len(_PERSON_CTX_PREFIX):])}
            except ValueError:
                return {"type": "all"}
        if ctx.startswith("Fichiers : "):
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
        if self._duplicate_thread and self._duplicate_thread.isRunning():
            self._duplicate_thread.cancel()
            self._duplicate_thread.wait(3000)
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
        elif key == Qt.Key_A and modifiers == Qt.ControlModifier:
            if self._stack.currentIndex() == 0:
                self._grid.select_all()
        elif in_viewer and key == Qt.Key_Right and not self._viewer._canvas._crop_mode:
            self._navigate_photo(-1)   # plus récente
        elif in_viewer and key == Qt.Key_Left and not self._viewer._canvas._crop_mode:
            self._navigate_photo(1)    # plus ancienne
        else:
            super().keyPressEvent(event)
