import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QStackedWidget, QStatusBar, QToolBar,
    QLineEdit, QSlider, QLabel, QPushButton,
    QFileDialog, QInputDialog, QMessageBox, QSizePolicy,
)

from src.core.config import Config
from src.core.event_bus import bus
from src.core.models import PhotoInfo, AlbumInfo
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.library.scanner import LibraryScanner
from src.processing.edit_database import EditDatabase
from src.ui.sidebar import Sidebar, _SPECIAL_ALL, _SPECIAL_FAV
from src.ui.thumbnail_grid import ThumbnailGrid
from src.ui.photo_viewer import PhotoViewer
from src.ui.edit_panel import EditPanel

logger = logging.getLogger(__name__)

_THUMB_SIZES = [110, 180, 250, 350]


def _fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} Ko"
    return f"{size_bytes / (1024 * 1024):.1f} Mo"


class MainWindow(QMainWindow):
    def __init__(
        self,
        config: Config,
        catalog: Catalog,
        thumb_cache: ThumbnailCache,
        scanner: LibraryScanner,
    ):
        super().__init__()
        self._config = config
        self._catalog = catalog
        self._thumb_cache = thumb_cache
        self._scanner = scanner
        self._edit_db = EditDatabase()

        self._current_photos: list[PhotoInfo] = []
        self._current_photo_index: int = 0
        self._current_context: str = ""   # dossier ou album actif
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._do_search)

        self._setup_window()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()
        self._connect_bus()
        self._connect_scanner()

        self._load_library()

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

        # Outils
        m_tools = mb.addMenu("Outils")
        act_settings = QAction("Paramètres", self)
        m_tools.addAction(act_settings)

        # Aide
        m_help = mb.addMenu("Aide")
        act_about = QAction("À propos", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    def _setup_toolbar(self) -> None:
        tb = QToolBar("Recherche")
        tb.setMovable(False)
        self.addToolBar(tb)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Rechercher… (Ctrl+F)")
        self._search_box.setMinimumWidth(260)
        self._search_box.textChanged.connect(self._on_search_text_changed)
        tb.addWidget(self._search_box)

        act_clear = QAction("✕", self)
        act_clear.triggered.connect(lambda: self._search_box.clear())
        tb.addAction(act_clear)

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
        self._left_stack.setFixedWidth(sidebar_w)
        self._splitter.addWidget(self._left_stack)

        self._sidebar = Sidebar()
        self._left_stack.addWidget(self._sidebar)   # index 0 — mode grille

        self._edit_panel = EditPanel()
        self._left_stack.addWidget(self._edit_panel)  # index 1 — mode visionneuse

        # Zone principale : grille ou visionneuse
        self._stack = QStackedWidget()
        self._splitter.addWidget(self._stack)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)

        # Index 0 — Grille
        self._grid = ThumbnailGrid(self._thumb_cache)
        self._grid.photo_activated.connect(self._on_photo_activated)
        self._grid.selection_changed.connect(self._on_selection_changed)
        self._grid.rename_requested.connect(self._on_rename_requested)
        self._stack.addWidget(self._grid)

        # Index 1 — Visionneuse seule (traitements dans le panneau gauche)
        self._viewer = PhotoViewer()
        self._viewer.closed.connect(self.show_grid)
        self._viewer.navigate.connect(self._navigate_photo)
        self._viewer.zoom_changed.connect(self._on_viewer_zoom_changed)
        self._edit_panel.edits_changed.connect(self._viewer.update_edit)
        self._edit_panel.crop_mode_requested.connect(self._viewer.enter_crop_mode)
        self._edit_panel.grid_visibility_changed.connect(self._viewer.set_grid_visible)
        self._viewer.crop_ready.connect(self._edit_panel.apply_crop)
        self._stack.addWidget(self._viewer)

        # Connexions sidebar
        self._sidebar.folder_selected.connect(self._on_folder_selected)
        self._sidebar.album_selected.connect(self._on_album_selected)
        self._sidebar.scan_requested.connect(self._on_scan_requested)
        self._sidebar.folder_removed.connect(self._on_folder_removed)

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

        self._thumb_slider = QSlider(Qt.Horizontal)
        self._thumb_slider.setRange(0, len(_THUMB_SIZES) - 1)
        self._thumb_slider.setValue(1)
        self._thumb_slider.setFixedWidth(100)
        self._thumb_slider.valueChanged.connect(self._on_thumb_size_changed)
        sb.addPermanentWidget(self._thumb_slider)

        # --- Contrôles mode visionneuse (cachés par défaut) ---
        self._lbl_zoom = QLabel("Zoom :")
        self._lbl_zoom.hide()
        sb.addPermanentWidget(self._lbl_zoom)

        self._zoom_slider = QSlider(Qt.Horizontal)
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

    # ------------------------------------------------------------------ library

    def _load_library(self) -> None:
        folders = self._config.get_scan_folders()
        if folders:
            self._sidebar.refresh_folders(folders)
            self._show_all_photos()
            self._start_scan(folders)
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)

    def _show_all_photos(self) -> None:
        photos = self._catalog.get_all_photos()
        self._current_photos = photos
        self._current_context = "Toutes les photos"
        self._grid.set_photos(photos)
        self._update_status()

    def _start_scan(self, folders: list[str]) -> None:
        thread = self._scanner.scan(folders)
        thread.photo_discovered.connect(self._on_photo_discovered)
        thread.finished.connect(self._on_scan_finished)
        thread.progress.connect(self._on_scan_progress)

    # ------------------------------------------------------------------ slots

    @Slot(object)
    def _on_photo_discovered(self, photo: PhotoInfo) -> None:
        self._grid.add_photo(photo)
        self._current_photos.append(photo)
        self._update_status()
        bus.emit("library.photo_discovered", photo=photo)

    @Slot(int)
    def _on_scan_finished(self, total: int) -> None:
        self._lbl_action.setText("")
        self._update_status()
        albums = self._catalog.get_albums()
        self._sidebar.refresh_albums(albums)

    @Slot(int, str)
    def _on_scan_progress(self, percent: int, path: str) -> None:
        self._lbl_action.setText(f"Scan… {percent}%  —  {os.path.basename(path)}")

    @Slot(object)
    def _on_photo_activated(self, photo: PhotoInfo) -> None:
        self._current_photo_index = next(
            (i for i, p in enumerate(self._current_photos) if p.path == photo.path), 0
        )
        self.show_viewer(photo)

    @Slot(list)
    def _on_selection_changed(self, photos: list[PhotoInfo]) -> None:
        self._update_status(photos)

    @Slot(int)
    def _navigate_photo(self, delta: int) -> None:
        if not self._current_photos:
            return
        self._current_photo_index = (self._current_photo_index + delta) % len(self._current_photos)
        photo = self._current_photos[self._current_photo_index]
        self._viewer.set_photo(photo)
        self._edit_panel.set_photo(photo)
        self._update_viewer_status(photo)

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

    @Slot(str)
    def _on_folder_selected(self, folder: str) -> None:
        photos = self._catalog.get_photos_in_folder(folder)
        self._current_photos = photos
        self._current_context = folder
        self._grid.set_photos(photos)
        self._update_status()

    @Slot(object)
    def _on_album_selected(self, data) -> None:
        if data == _SPECIAL_ALL:
            self._show_all_photos()
        elif data == _SPECIAL_FAV:
            photos = self._catalog.get_favorites()
            self._current_photos = photos
            self._current_context = "Favoris"
            self._grid.set_photos(photos)
            self._update_status()
        elif isinstance(data, AlbumInfo) and data.id:
            photos = self._catalog.get_photos_in_album(data.id)
            self._current_photos = photos
            self._current_context = data.name
            self._grid.set_photos(photos)
            self._update_status()

    @Slot(str)
    def _on_scan_requested(self, folder: str) -> None:
        self._start_scan([folder])

    @Slot(str)
    def _on_folder_removed(self, folder: str) -> None:
        self._config.remove_scan_folder(folder)
        self._sidebar.refresh_folders(self._config.get_scan_folders())

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
        photos = self._catalog.search(query)
        self._current_photos = photos
        self._grid.set_photos(photos)
        self._update_status(len(photos))

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
        self._lbl_fileinfo.setText("")
        self._update_status()

    def show_viewer(self, photo: PhotoInfo) -> None:
        self._viewer.set_photo(photo)
        self._edit_panel.set_photo(photo)
        self._stack.setCurrentIndex(1)
        self._left_stack.setCurrentIndex(1)
        self._viewer.setFocus()
        self._lbl_thumb_size.hide()
        self._thumb_slider.hide()
        self._lbl_zoom.show()
        self._zoom_slider.show()
        self._zoom_pct_label.show()
        self._btn_grid_status.hide()
        self._update_viewer_status(photo)

    def toggle_sidebar(self) -> None:
        self._left_stack.setVisible(not self._left_stack.isVisible())

    def open_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir un dossier de photos", os.path.expanduser("~")
        )
        if folder:
            self._config.add_scan_folder(folder)
            self._sidebar.refresh_folders(self._config.get_scan_folders())
            self._start_scan([folder])

    # ------------------------------------------------------------------ private

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "À propos de PixelPhotoManager",
            "PixelPhotoManager v1.0\n\nGestionnaire de photos non destructif.\nPySide6 · Pillow · SQLite",
        )

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

        new_path_str = str(new_p)
        self._catalog.rename_photo(photo.path, new_path_str)
        self._edit_db.rename_photo(photo.path, new_path_str)
        self._grid.update_photo_path(photo.path, new_path_str)

        for p in self._current_photos:
            if p.path == photo.path:
                p.path = new_path_str
                p.filename = new_p.name
                break

        self._update_status()

    def closeEvent(self, event) -> None:
        self._config.set("ui.window_width", self.width())
        self._config.set("ui.window_height", self.height())
        self._config.set("ui.sidebar_width", self._left_stack.width())
        self._scanner.stop()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key_F9:
            self.toggle_sidebar()
        elif key == Qt.Key_F and modifiers == Qt.ControlModifier:
            self._search_box.setFocus()
            self._search_box.selectAll()
        elif key == Qt.Key_A and modifiers == Qt.ControlModifier:
            if self._stack.currentIndex() == 0:
                self._grid.select_all()
        else:
            super().keyPressEvent(event)
