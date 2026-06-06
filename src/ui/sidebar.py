import os
import logging
import shutil
import subprocess

from PySide6.QtCore import Signal, Qt, QRect, QSize, QUrl
from PySide6.QtGui import QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMenu, QInputDialog, QMessageBox, QFileDialog,
)

from src.core.event_bus import bus
from src.core.models import AlbumInfo, PersonInfo
from src.ui.people_panel import load_face_pixmap

logger = logging.getLogger(__name__)

_SPECIAL_ALL    = "__all__"
_SPECIAL_FAV    = "__favorites__"
_SPECIAL_VIDEOS = "__videos__"


_MIME_PHOTOS = 'application/x-pixelphoto-paths'


class _FolderTree(QTreeWidget):
    """QTreeWidget qui accepte les drops internes de photos depuis la grille."""
    files_dropped = Signal(list, str)   # (file_paths, dest_folder_path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(_MIME_PHOTOS):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(_MIME_PHOTOS):
            item = self.itemAt(event.position().toPoint())
            if item:
                self.setCurrentItem(item)
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(_MIME_PHOTOS):
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        if not item:
            event.ignore()
            return
        folder_path = item.data(0, Qt.UserRole)
        raw = event.mimeData().data(_MIME_PHOTOS)
        file_paths = [p for p in raw.data().decode('utf-8').split('\n') if p]
        if file_paths and folder_path:
            self.files_dropped.emit(file_paths, folder_path)
            event.acceptProposedAction()
        else:
            event.ignore()


_SPECIAL_PERSON = "__person__"   # préfixe pour l'identifiant de contexte personne


class _BadgeButton(QPushButton):
    """QPushButton avec un badge circulaire rouge en coin supérieur droit."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self._badge: int = 0

    def set_badge(self, count: int) -> None:
        if count != self._badge:
            self._badge = count
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._badge <= 0:
            return

        label = str(self._badge) if self._badge < 100 else "99+"

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        font = QFont()
        font.setPixelSize(9)
        font.setBold(True)
        p.setFont(font)

        text_w = p.fontMetrics().horizontalAdvance(label)
        radius = max(8, text_w // 2 + 5)
        diameter = radius * 2
        x = self.width()  - diameter - 2
        y = 2

        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#d94f4f"))
        p.drawEllipse(x, y, diameter, diameter)

        p.setPen(QColor("white"))
        p.drawText(QRect(x, y, diameter, diameter), Qt.AlignCenter, label)
        p.end()


class Sidebar(QWidget):
    folder_selected    = Signal(str)
    album_selected     = Signal(object)   # AlbumInfo | str (special key)
    scan_requested     = Signal(str)
    folder_removed     = Signal(str)
    folder_created     = Signal(str)      # chemin du nouveau sous-dossier créé
    folder_moved       = Signal(str, str) # (ancien_chemin, nouveau_chemin)
    photos_dropped     = Signal(list, str) # (file_paths, dest_folder_path)
    person_selected        = Signal(object)   # PersonInfo
    identify_requested     = Signal()         # ouvrir PeopleDialog
    person_merge_requested = Signal(object)   # PersonInfo à fusionner
    person_rename_requested = Signal(object)  # PersonInfo à renommer

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

        self._folder_tree = _FolderTree()
        self._folder_tree.setHeaderHidden(True)
        self._folder_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._folder_tree.customContextMenuRequested.connect(self._folder_context_menu)
        self._folder_tree.itemClicked.connect(self._on_folder_clicked)
        self._folder_tree.itemExpanded.connect(self._on_folder_expanded)
        self._folder_tree.files_dropped.connect(self.photos_dropped)
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

        # --- Persons list ---
        persons_widget = QWidget()
        pw_layout = QVBoxLayout(persons_widget)
        pw_layout.setContentsMargins(0, 0, 0, 0)
        pw_layout.setSpacing(0)

        persons_header_bar = QHBoxLayout()
        persons_header = QLabel("  Personnes")
        persons_header.setStyleSheet("color: #ccc; font-weight: bold;")
        persons_header_bar.addWidget(persons_header)
        persons_header_bar.addStretch()
        self._btn_identify = _BadgeButton("Identifier…")
        self._btn_identify.setToolTip("Nommer les groupes de visages détectés")
        self._btn_identify.clicked.connect(self.identify_requested)
        persons_header_bar.addWidget(self._btn_identify)

        persons_header_container = QWidget()
        persons_header_container.setStyleSheet("background: #2a2a2a;")
        persons_header_container.setLayout(persons_header_bar)
        pw_layout.addWidget(persons_header_container)

        self._persons_list = QListWidget()
        self._persons_list.setIconSize(QSize(36, 36))
        self._persons_list.itemClicked.connect(self._on_person_clicked)
        self._persons_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._persons_list.customContextMenuRequested.connect(self._person_context_menu)
        pw_layout.addWidget(self._persons_list)

        splitter.addWidget(persons_widget)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)

        layout.addWidget(splitter)

        self._albums: list[AlbumInfo] = []
        self._persons: list[PersonInfo] = []

        self._add_special_albums()

    def _add_special_albums(self) -> None:
        item_all = QListWidgetItem("★ Toutes les photos")
        item_all.setData(Qt.UserRole, _SPECIAL_ALL)
        self._albums_list.addItem(item_all)
        self._albums_list.setCurrentItem(item_all)

        item_fav = QListWidgetItem("♡ Favoris")
        item_fav.setData(Qt.UserRole, _SPECIAL_FAV)
        self._albums_list.addItem(item_fav)

        item_vid = QListWidgetItem("▶ Vidéos")
        item_vid.setData(Qt.UserRole, _SPECIAL_VIDEOS)
        self._albums_list.addItem(item_vid)

    def refresh_folders(self, folders: list[str]) -> None:
        self._folder_tree.clear()
        for folder in folders:
            root_item = QTreeWidgetItem([os.path.basename(folder) or folder])
            root_item.setData(0, Qt.UserRole, folder)
            root_item.setToolTip(0, folder)
            # Placeholder : rend le nœud dépliable sans toucher le disque.
            # Les sous-dossiers sont chargés à la demande dans _on_folder_expanded.
            root_item.addChild(QTreeWidgetItem([""]))
            self._folder_tree.addTopLevelItem(root_item)
            # Pas de setExpanded() ici : avec >100 dossiers, _populate_subfolders
            # sur chaque racine bloque l'UI (scandir × N dossiers).

    def _has_subdirs(self, folder_path: str) -> bool:
        """Retourne True si folder_path contient au moins un sous-dossier visible."""
        try:
            for entry in os.scandir(folder_path):
                if entry.is_dir() and not entry.name.startswith("."):
                    return True
        except PermissionError:
            pass
        return False

    def _populate_subfolders(self, parent_item: QTreeWidgetItem, folder_path: str) -> None:
        """Ajoute les sous-dossiers immédiats de folder_path sous parent_item.
        Chaque enfant reçoit un placeholder s'il a lui-même des sous-dossiers,
        permettant le lazy loading à l'expansion."""
        try:
            entries = sorted(os.scandir(folder_path), key=lambda e: e.name.lower())
            for entry in entries:
                if entry.is_dir() and not entry.name.startswith("."):
                    child = QTreeWidgetItem([entry.name])
                    child.setData(0, Qt.UserRole, entry.path)
                    child.setToolTip(0, entry.path)
                    parent_item.addChild(child)
                    if self._has_subdirs(entry.path):
                        # Placeholder → rend le nœud dépliable
                        child.addChild(QTreeWidgetItem([""]))
        except PermissionError:
            pass

    def _on_folder_expanded(self, item: QTreeWidgetItem) -> None:
        """Lazy loading : remplace le placeholder par les vrais sous-dossiers."""
        if item.childCount() != 1:
            return
        placeholder = item.child(0)
        if placeholder.data(0, Qt.UserRole) is not None:
            return  # déjà chargé
        folder_path = item.data(0, Qt.UserRole)
        if not folder_path:
            return
        item.removeChild(placeholder)
        self._populate_subfolders(item, folder_path)

    def refresh_albums(self, albums: list[AlbumInfo]) -> None:
        self._albums = albums
        # Remove existing album items (keep the 3 special ones at top)
        while self._albums_list.count() > 3:
            self._albums_list.takeItem(3)
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
        menu.addAction("Supprimer des dossiers surveillés",
                       lambda: self.folder_removed.emit(path))
        menu.addSeparator()
        menu.addAction("Créer un sous-dossier…",
                       lambda: self._create_subfolder(path))
        menu.addAction("Renommer…",
                       lambda: self._rename_folder(path))
        menu.addAction("Déplacer vers…",
                       lambda: self._move_folder(path))
        menu.addSeparator()
        menu.addAction("Ouvrir dans l'Explorateur",
                       lambda: subprocess.Popen(f'explorer "{path}"'))
        menu.exec(self._folder_tree.mapToGlobal(pos))

    def _create_subfolder(self, parent_path: str) -> None:
        name, ok = QInputDialog.getText(
            self, "Nouveau sous-dossier",
            f"Nom du sous-dossier dans « {os.path.basename(parent_path)} » :",
        )
        if not ok or not name.strip():
            return
        new_path = os.path.join(parent_path, name.strip())
        try:
            os.makedirs(new_path, exist_ok=False)
        except FileExistsError:
            QMessageBox.warning(self, "Dossier existant",
                                f"« {name.strip()} » existe déjà dans ce dossier.")
            return
        except Exception as e:
            QMessageBox.critical(self, "Erreur",
                                 f"Impossible de créer le dossier :\n{e}")
            return
        self.folder_created.emit(new_path)

    def _rename_folder(self, path: str) -> None:
        current_name = os.path.basename(path)
        new_name, ok = QInputDialog.getText(
            self, "Renommer le dossier", "Nouveau nom :", text=current_name,
        )
        if not ok or not new_name.strip() or new_name.strip() == current_name:
            return
        new_path = os.path.join(os.path.dirname(path), new_name.strip())
        try:
            os.rename(path, new_path)
        except Exception as e:
            QMessageBox.critical(self, "Erreur",
                                 f"Impossible de renommer le dossier :\n{e}")
            return
        self.folder_moved.emit(path, new_path)

    def _move_folder(self, path: str) -> None:
        folder_name = os.path.basename(path)
        dst = QFileDialog.getExistingDirectory(
            self, f"Déplacer « {folder_name} » — choisir le dossier de destination",
            os.path.dirname(path),
        )
        if not dst:
            return
        new_path = os.path.join(dst, folder_name)
        if os.path.normcase(new_path) == os.path.normcase(path):
            return  # même emplacement, rien à faire
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Dossier existant",
                                f"« {new_path} » existe déjà.")
            return
        try:
            shutil.move(path, dst)
        except Exception as e:
            QMessageBox.critical(self, "Erreur",
                                 f"Impossible de déplacer le dossier :\n{e}")
            return
        self.folder_moved.emit(path, new_path)

    def _create_album(self) -> None:
        name, ok = QInputDialog.getText(self, "Nouvel album", "Nom de l'album :")
        if ok and name.strip():
            bus.emit("album.create_requested", name=name.strip())

    # ------------------------------------------------------------------ persons

    def refresh_persons(self, persons: list[PersonInfo]) -> None:
        self._persons = persons
        self._persons_list.clear()
        for person in persons:
            label = f"{person.name}  ({person.photo_count})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, person)
            if person.cover_path and person.cover_bbox:
                try:
                    from src.core.models import FaceInfo
                    face = FaceInfo(
                        photo_path=person.cover_path,
                        bbox_x=person.cover_bbox[0],
                        bbox_y=person.cover_bbox[1],
                        bbox_w=person.cover_bbox[2],
                        bbox_h=person.cover_bbox[3],
                    )
                    pix = load_face_pixmap(face, size=36)
                    item.setIcon(QIcon(pix))
                except Exception:
                    pass
            self._persons_list.addItem(item)

    def update_cluster_badge(self, count: int) -> None:
        """Mettre à jour le badge du bouton Identifier avec le nombre de groupes en attente."""
        self._btn_identify.set_badge(count)

    def _on_person_clicked(self, item: QListWidgetItem) -> None:
        person = item.data(Qt.UserRole)
        if isinstance(person, PersonInfo):
            self.person_selected.emit(person)

    def _person_context_menu(self, pos) -> None:
        item = self._persons_list.itemAt(pos)
        if not item:
            return
        person = item.data(Qt.UserRole)
        if not isinstance(person, PersonInfo):
            return
        menu = QMenu(self)
        menu.addAction("Renommer…",
                       lambda: self.person_rename_requested.emit(person))
        menu.addAction("Fusionner avec…",
                       lambda: self.person_merge_requested.emit(person))
        menu.exec(self._persons_list.mapToGlobal(pos))
