import os
import logging
import subprocess

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMenu, QInputDialog,
)

from src.core.event_bus import bus
from src.core.models import AlbumInfo

logger = logging.getLogger(__name__)

_SPECIAL_ALL = "__all__"
_SPECIAL_FAV = "__favorites__"


class Sidebar(QWidget):
    folder_selected = Signal(str)
    album_selected = Signal(object)  # AlbumInfo | str (special key)
    scan_requested = Signal(str)
    folder_removed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Vertical)

        # --- Folder tree ---
        folder_widget = QWidget()
        fw_layout = QVBoxLayout(folder_widget)
        fw_layout.setContentsMargins(0, 0, 0, 0)
        fw_layout.setSpacing(0)

        folder_header = QLabel("  Dossiers")
        folder_header.setStyleSheet(
            "background: #2a2a2a; color: #ccc; padding: 4px; font-weight: bold;"
        )
        fw_layout.addWidget(folder_header)

        self._folder_tree = QTreeWidget()
        self._folder_tree.setHeaderHidden(True)
        self._folder_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._folder_tree.customContextMenuRequested.connect(self._folder_context_menu)
        self._folder_tree.itemClicked.connect(self._on_folder_clicked)
        fw_layout.addWidget(self._folder_tree)

        splitter.addWidget(folder_widget)

        # --- Albums list ---
        album_widget = QWidget()
        aw_layout = QVBoxLayout(album_widget)
        aw_layout.setContentsMargins(0, 0, 0, 0)
        aw_layout.setSpacing(0)

        album_header_bar = QHBoxLayout()
        album_header = QLabel("  Albums")
        album_header.setStyleSheet("color: #ccc; font-weight: bold;")
        album_header_bar.addWidget(album_header)
        album_header_bar.addStretch()
        btn_new_album = QPushButton("+")
        btn_new_album.setFixedWidth(24)
        btn_new_album.setToolTip("Créer un album")
        btn_new_album.clicked.connect(self._create_album)
        album_header_bar.addWidget(btn_new_album)

        header_container = QWidget()
        header_container.setStyleSheet("background: #2a2a2a;")
        header_container.setLayout(album_header_bar)
        aw_layout.addWidget(header_container)

        self._albums_list = QListWidget()
        self._albums_list.itemClicked.connect(self._on_album_clicked)
        aw_layout.addWidget(self._albums_list)

        splitter.addWidget(album_widget)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)

        self._albums: list[AlbumInfo] = []

        self._add_special_albums()

    def _add_special_albums(self) -> None:
        item_all = QListWidgetItem("★ Toutes les photos")
        item_all.setData(Qt.UserRole, _SPECIAL_ALL)
        self._albums_list.addItem(item_all)
        self._albums_list.setCurrentItem(item_all)

        item_fav = QListWidgetItem("♡ Favoris")
        item_fav.setData(Qt.UserRole, _SPECIAL_FAV)
        self._albums_list.addItem(item_fav)

    def refresh_folders(self, folders: list[str]) -> None:
        self._folder_tree.clear()
        for folder in folders:
            root_item = QTreeWidgetItem([os.path.basename(folder) or folder])
            root_item.setData(0, Qt.UserRole, folder)
            root_item.setToolTip(0, folder)
            self._folder_tree.addTopLevelItem(root_item)
            self._populate_subfolders(root_item, folder)
        self._folder_tree.expandAll()

    def _populate_subfolders(self, parent_item: QTreeWidgetItem, folder_path: str) -> None:
        try:
            entries = sorted(os.scandir(folder_path), key=lambda e: e.name.lower())
            for entry in entries:
                if entry.is_dir() and not entry.name.startswith("."):
                    child = QTreeWidgetItem([entry.name])
                    child.setData(0, Qt.UserRole, entry.path)
                    child.setToolTip(0, entry.path)
                    parent_item.addChild(child)
        except PermissionError:
            pass

    def refresh_albums(self, albums: list[AlbumInfo]) -> None:
        self._albums = albums
        # Remove existing album items (keep special ones at top)
        while self._albums_list.count() > 2:
            self._albums_list.takeItem(2)
        for album in albums:
            item = QListWidgetItem(f"📁 {album.name} ({album.photo_count})")
            item.setData(Qt.UserRole, album)
            self._albums_list.addItem(item)

    def _on_folder_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        path = item.data(0, Qt.UserRole)
        if path:
            bus.emit("library.folder_selected", folder=path)
            self.folder_selected.emit(path)

    def _on_album_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole)
        self.album_selected.emit(data)

    def _folder_context_menu(self, pos) -> None:
        item = self._folder_tree.itemAt(pos)
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Scanner maintenant", lambda: self.scan_requested.emit(path))
        menu.addAction(
            "Supprimer des dossiers surveillés",
            lambda: self.folder_removed.emit(path),
        )
        menu.addSeparator()
        menu.addAction(
            "Ouvrir dans l'Explorateur",
            lambda: subprocess.Popen(f'explorer "{path}"'),
        )
        menu.exec(self._folder_tree.mapToGlobal(pos))

    def _create_album(self) -> None:
        name, ok = QInputDialog.getText(self, "Nouvel album", "Nom de l'album :")
        if ok and name.strip():
            bus.emit("album.create_requested", name=name.strip())
