import os
import logging
import shutil
import subprocess

from PySide6.QtCore import Signal, Qt, QRect, QSize, QUrl, QThread, QTimer, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QStyle, QStyleOptionViewItem, QStyledItemDelegate,
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QMenu, QInputDialog, QMessageBox, QFileDialog,
)

from src.core.event_bus import bus
from src.core.models import AlbumInfo, PersonInfo
from src.ui.people_panel import _face_bytes, _load_edit_rotations

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


_SPECIAL_PERSON   = "__person__"    # préfixe pour l'identifiant de contexte personne
_SPECIAL_FILENAME = "__filename__"  # album virtuel "Par nom de fichier"


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


class _FaceIconLoader(QThread):
    """Charge les crops de visages en arrière-plan pour éviter de freezer le thread UI."""

    icon_ready = Signal(int, bytes)   # (index dans la liste, PNG bytes)

    def __init__(self, persons: list, parent=None) -> None:
        super().__init__(parent)
        self._persons = persons
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        from src.core.models import FaceInfo
        cover_paths = [p.cover_path for p in self._persons if p.cover_path and p.cover_bbox]
        edit_rots = _load_edit_rotations(cover_paths)
        for i, person in enumerate(self._persons):
            if self._stop_flag:
                break
            if person.cover_path and person.cover_bbox:
                try:
                    face = FaceInfo(
                        photo_path=person.cover_path,
                        bbox_x=person.cover_bbox[0],
                        bbox_y=person.cover_bbox[1],
                        bbox_w=person.cover_bbox[2],
                        bbox_h=person.cover_bbox[3],
                        detected_rotation=person.cover_detected_rotation,
                    )
                    edit_rot = edit_rots.get(person.cover_path, 0)
                    data = _face_bytes(face, size=36, edit_rotation=edit_rot)
                    if data:
                        self.icon_ready.emit(i, data)
                except Exception:
                    pass


class _SingleFaceIconLoader(QThread):
    """Charge le crop d'un seul visage en arrière-plan pour mettre à jour une icône."""

    icon_ready = Signal(int, bytes)   # (index dans la liste, PNG bytes)

    def __init__(self, index: int, face, parent=None) -> None:
        super().__init__(parent)
        self._index = index
        self._face  = face

    def run(self) -> None:
        edit_rots = _load_edit_rotations([self._face.photo_path])
        data = _face_bytes(
            self._face, size=36,
            edit_rotation=edit_rots.get(self._face.photo_path, 0),
        )
        if data:
            self.icon_ready.emit(self._index, data)


class _PendingBadgeDelegate(QStyledItemDelegate):
    """Affiche un badge orange avec le nombre de suggestions entre la vignette et le nom."""

    _R = 9   # rayon du badge en px

    def paint(self, painter, option, index) -> None:
        person = index.data(Qt.UserRole)
        pending = isinstance(person, PersonInfo) and getattr(person, "pending_count", 0) > 0

        if not pending:
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = self.parent()
        style = widget.style() if widget else QApplication.style()

        # 1. Fond (sélection, survol…)
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, widget)

        # 2. Icône
        icon_rect = style.subElementRect(QStyle.SE_ItemViewItemDecoration, opt, None)
        if not opt.icon.isNull() and icon_rect.isValid():
            mode = QIcon.Selected if (opt.state & QStyle.State_Selected) else QIcon.Normal
            opt.icon.paint(painter, icon_rect, Qt.AlignCenter, mode)

        # 3. Position du badge (juste après l'icône)
        r = self._R
        bx = (icon_rect.right() + 3) if icon_rect.isValid() else (opt.rect.left() + 44)
        by = opt.rect.center().y() - r

        # 4. Texte décalé après le badge
        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, None)
        text_rect.setLeft(bx + r * 2 + 5)
        color = (opt.palette.color(QPalette.HighlightedText)
                 if (opt.state & QStyle.State_Selected)
                 else opt.palette.color(QPalette.Text))
        painter.save()
        painter.setPen(color)
        elided = opt.fontMetrics.elidedText(opt.text, opt.textElideMode, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()

        # 5. Badge
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor("#e8a040"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(bx, by, r * 2, r * 2)
        count = person.pending_count
        label = str(count) if count < 100 else "99+"
        font = QFont()
        font.setPixelSize(r)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#1a1a1a"))
        painter.drawText(bx, by, r * 2, r * 2, Qt.AlignCenter, label)
        painter.restore()


class Sidebar(QWidget):
    folder_selected    = Signal(str)
    album_selected     = Signal(object)   # AlbumInfo | str (special key)
    scan_requested     = Signal(str)
    folder_removed     = Signal(str)
    folder_created     = Signal(str)      # chemin du nouveau sous-dossier créé
    folder_moved       = Signal(str, str) # (ancien_chemin, nouveau_chemin)
    folder_deleted     = Signal(str)      # dossier supprimé du disque
    photos_dropped     = Signal(list, str) # (file_paths, dest_folder_path)
    person_selected        = Signal(object)   # PersonInfo
    identify_requested     = Signal()         # ouvrir PeopleDialog
    person_merge_requested  = Signal(object)   # PersonInfo à fusionner
    person_rename_requested = Signal(object)  # PersonInfo à renommer
    person_clear_requested  = Signal(object)  # PersonInfo dont on efface le nom
    tree_state_changed     = Signal(list)     # list[str] — chemins dépliés

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded_paths: set[str] = set()
        self._restoring: bool = False
        self._face_loader: _FaceIconLoader | None = None
        self._pending_person_id: int | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText("🔍  Filtrer dossiers, personnes et fichiers…")
        self._filter_box.setClearButtonEnabled(True)
        self._filter_box.setStyleSheet("padding: 4px 6px; background: #2a2a2a; color: #ddd; border: none; border-bottom: 1px solid #444;")
        self._filter_box.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter_box)

        self._splitter = QSplitter(Qt.Vertical)

        # --- Folder tree ---
        folder_widget = QWidget()
        fw_layout = QVBoxLayout(folder_widget)
        fw_layout.setContentsMargins(0, 0, 0, 0)
        fw_layout.setSpacing(0)

        folder_header_bar = QHBoxLayout()
        folder_header_bar.setContentsMargins(8, 3, 4, 3)
        folder_header_bar.setSpacing(4)
        self._folder_arrow = QLabel("▾")
        self._folder_arrow.setStyleSheet("color: #888;")
        self._folder_arrow.setFixedWidth(10)
        folder_header_bar.addWidget(self._folder_arrow)
        _lbl = QLabel("Dossiers")
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        folder_header_bar.addWidget(_lbl)
        folder_header_bar.addStretch()
        folder_header_container = QWidget()
        folder_header_container.setStyleSheet("background: #2a2a2a;")
        folder_header_container.setLayout(folder_header_bar)
        fw_layout.addWidget(folder_header_container)

        self._folder_tree = _FolderTree()
        self._folder_tree.setHeaderHidden(True)
        self._folder_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._folder_tree.customContextMenuRequested.connect(self._folder_context_menu)
        self._folder_tree.itemClicked.connect(self._on_folder_clicked)
        self._folder_tree.itemExpanded.connect(self._on_folder_expanded)
        self._folder_tree.itemCollapsed.connect(self._on_folder_collapsed)
        self._folder_tree.files_dropped.connect(self.photos_dropped)
        fw_layout.addWidget(self._folder_tree)

        self._splitter.addWidget(folder_widget)

        # --- Albums list ---
        album_widget = QWidget()
        aw_layout = QVBoxLayout(album_widget)
        aw_layout.setContentsMargins(0, 0, 0, 0)
        aw_layout.setSpacing(0)

        album_header_bar = QHBoxLayout()
        album_header_bar.setContentsMargins(8, 3, 4, 3)
        album_header_bar.setSpacing(4)
        self._album_arrow = QLabel("▾")
        self._album_arrow.setStyleSheet("color: #888;")
        self._album_arrow.setFixedWidth(10)
        album_header_bar.addWidget(self._album_arrow)
        _lbl = QLabel("Albums")
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        album_header_bar.addWidget(_lbl)
        album_header_bar.addStretch()
        btn_new_album = QPushButton("+")
        btn_new_album.setFixedWidth(24)
        btn_new_album.setToolTip("Créer un album")
        btn_new_album.clicked.connect(self._create_album)
        album_header_bar.addWidget(btn_new_album)

        album_header_container = QWidget()
        album_header_container.setStyleSheet("background: #2a2a2a;")
        album_header_container.setLayout(album_header_bar)
        aw_layout.addWidget(album_header_container)

        self._albums_list = QListWidget()
        self._albums_list.itemClicked.connect(self._on_album_clicked)
        aw_layout.addWidget(self._albums_list)

        self._splitter.addWidget(album_widget)

        # --- Persons list ---
        persons_widget = QWidget()
        pw_layout = QVBoxLayout(persons_widget)
        pw_layout.setContentsMargins(0, 0, 0, 0)
        pw_layout.setSpacing(0)

        persons_header_bar = QHBoxLayout()
        persons_header_bar.setContentsMargins(8, 3, 4, 3)
        persons_header_bar.setSpacing(4)
        self._persons_arrow = QLabel("▾")
        self._persons_arrow.setStyleSheet("color: #888;")
        self._persons_arrow.setFixedWidth(10)
        persons_header_bar.addWidget(self._persons_arrow)
        _lbl = QLabel("Personnes")
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        persons_header_bar.addWidget(_lbl)
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
        self._persons_list.setItemDelegate(_PendingBadgeDelegate(self._persons_list))
        self._persons_list.itemClicked.connect(self._on_person_clicked)
        self._persons_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._persons_list.customContextMenuRequested.connect(self._person_context_menu)
        pw_layout.addWidget(self._persons_list)

        self._splitter.addWidget(persons_widget)

        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 1)
        for i in range(3):
            self._splitter.setCollapsible(i, False)
        folder_widget.setMinimumHeight(26)
        album_widget.setMinimumHeight(26)
        persons_widget.setMinimumHeight(26)
        self._splitter.splitterMoved.connect(self._update_section_arrows)

        layout.addWidget(self._splitter)

        self._albums: list[AlbumInfo] = []
        self._persons: list[PersonInfo] = []

        self._add_special_albums()

    # ── persistance des positions de bordures ──────────────────────────────

    def save_splitter_state(self) -> str:
        import base64
        return base64.b64encode(self._splitter.saveState().data()).decode()

    def restore_splitter_state(self, state_b64: str) -> None:
        import base64
        from PySide6.QtCore import QByteArray
        if state_b64:
            self._splitter.restoreState(QByteArray(base64.b64decode(state_b64)))
        QTimer.singleShot(0, self._update_section_arrows)

    def _update_section_arrows(self) -> None:
        sizes = self._splitter.sizes()
        for i, arrow_lbl in enumerate((self._folder_arrow, self._album_arrow, self._persons_arrow)):
            if i < len(sizes):
                arrow_lbl.setText("▸" if sizes[i] < 50 else "▾")

    def _add_special_albums(self) -> None:
        item_all = QListWidgetItem("★ Chronologie de toutes les photos")
        item_all.setData(Qt.UserRole, _SPECIAL_ALL)
        self._albums_list.addItem(item_all)
        self._albums_list.setCurrentItem(item_all)

        item_fav = QListWidgetItem("♡ Favoris")
        item_fav.setData(Qt.UserRole, _SPECIAL_FAV)
        self._albums_list.addItem(item_fav)

        item_vid = QListWidgetItem("▶ Vidéos")
        item_vid.setData(Qt.UserRole, _SPECIAL_VIDEOS)
        self._albums_list.addItem(item_vid)

        item_fn = QListWidgetItem("🔍 Par nom de fichier")
        item_fn.setData(Qt.UserRole, _SPECIAL_FILENAME)
        item_fn.setToolTip("Afficher les photos dont le nom de fichier contient le texte du filtre")
        self._albums_list.addItem(item_fn)

    # ── filtrage live ──────────────────────────────────────────────────────────

    @Slot(str)
    def _apply_filter(self, text: str) -> None:
        q = text.strip().lower()
        self._filter_folder_tree(q)
        self._filter_persons_list(q)

    @property
    def filter_text(self) -> str:
        return self._filter_box.text().strip()

    def _filter_folder_tree(self, q: str) -> None:
        for i in range(self._folder_tree.topLevelItemCount()):
            self._filter_tree_item(self._folder_tree.topLevelItem(i), q)

    def _filter_tree_item(self, item: QTreeWidgetItem, q: str) -> bool:
        """Cache/montre item selon q. Retourne True si item ou un descendant matche."""
        if item.data(0, Qt.UserRole) is None:
            return True  # placeholder lazy-load, ne pas toucher
        self_match = not q or q in item.text(0).lower()
        child_match = False
        for i in range(item.childCount()):
            child = item.child(i)
            if child.data(0, Qt.UserRole) is not None:
                if self._filter_tree_item(child, q):
                    child_match = True
        visible = self_match or child_match
        item.setHidden(not visible)
        return visible

    def _filter_persons_list(self, q: str) -> None:
        for i in range(self._persons_list.count()):
            item = self._persons_list.item(i)
            person = item.data(Qt.UserRole)
            name = person.name if isinstance(person, PersonInfo) else item.text()
            item.setHidden(bool(q) and q not in name.lower())

    # ── persistance des positions de bordures ──────────────────────────────

    def set_tree_expanded_paths(self, paths: list[str]) -> None:
        """Initialise l'état mémorisé depuis la config (appeler avant refresh_folders)."""
        self._expanded_paths = set(paths)

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

        # Restaurer l'état d'expansion mémorisé
        if self._expanded_paths:
            self._restoring = True
            try:
                for path in sorted(self._expanded_paths):
                    self._restore_expand(path)
            finally:
                self._restoring = False

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
            entries = sorted(os.scandir(folder_path), key=lambda e: e.name.lower(), reverse=True)
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
        """Lazy loading + mémorisation de l'état d'expansion."""
        # Lazy loading si le nœud n'a qu'un placeholder sans données
        if item.childCount() == 1 and item.child(0).data(0, Qt.UserRole) is None:
            folder_path = item.data(0, Qt.UserRole)
            if folder_path:
                item.removeChild(item.child(0))
                self._populate_subfolders(item, folder_path)
        # Mémoriser
        if not self._restoring:
            path = item.data(0, Qt.UserRole)
            if path:
                self._expanded_paths.add(path)
                self.tree_state_changed.emit(list(self._expanded_paths))

    def _on_folder_collapsed(self, item: QTreeWidgetItem) -> None:
        if self._restoring:
            return
        path = item.data(0, Qt.UserRole)
        if not path:
            return
        self._expanded_paths.discard(path)
        # Supprimer aussi tous les descendants (plus visibles)
        prefix = os.path.normcase(path + os.sep)
        self._expanded_paths = {
            p for p in self._expanded_paths
            if not os.path.normcase(p).startswith(prefix)
        }
        self.tree_state_changed.emit(list(self._expanded_paths))

    def _restore_expand(self, target: str) -> None:
        """Cherche target dans l'arbre et déplie le chemin vers lui."""
        norm_target = os.path.normcase(target)
        for i in range(self._folder_tree.topLevelItemCount()):
            root = self._folder_tree.topLevelItem(i)
            root_path = root.data(0, Qt.UserRole)
            if not root_path:
                continue
            norm_root = os.path.normcase(root_path)
            if norm_target == norm_root or norm_target.startswith(norm_root + os.sep):
                self._expand_toward(root, norm_target)
                return

    def _expand_toward(self, item: QTreeWidgetItem, norm_target: str) -> None:
        """Déplie récursivement vers norm_target, en chargeant les nœuds lazy si besoin."""
        item_path = item.data(0, Qt.UserRole)
        if not item_path:
            return
        norm_item = os.path.normcase(item_path)
        # Peupler si placeholder non encore chargé
        if item.childCount() == 1 and item.child(0).data(0, Qt.UserRole) is None:
            item.removeChild(item.child(0))
            self._populate_subfolders(item, item_path)
        item.setExpanded(True)
        if norm_item == norm_target:
            return
        # Descendre vers l'enfant qui contient target
        for j in range(item.childCount()):
            child = item.child(j)
            child_path = child.data(0, Qt.UserRole)
            if not child_path:
                continue
            norm_child = os.path.normcase(child_path)
            if norm_target == norm_child or norm_target.startswith(norm_child + os.sep):
                self._expand_toward(child, norm_target)
                return

    def refresh_albums(self, albums: list[AlbumInfo]) -> None:
        self._albums = albums
        # Remove existing album items (keep the 4 special ones at top)
        while self._albums_list.count() > 4:
            self._albums_list.takeItem(4)
        for album in albums:
            item = QListWidgetItem(f"📁 {album.name} ({album.photo_count})")
            item.setData(Qt.UserRole, album)
            self._albums_list.addItem(item)

    def select_album_item(self, data) -> None:
        """Sélectionne silencieusement un album dans la liste (sans émettre de signal)."""
        for i in range(self._albums_list.count()):
            item = self._albums_list.item(i)
            item_data = item.data(Qt.UserRole)
            match = (item_data == data) if isinstance(data, str) else (
                isinstance(item_data, AlbumInfo) and isinstance(data, AlbumInfo)
                and item_data.id == data.id
            )
            if match:
                self._folder_tree.clearSelection()
                self._persons_list.clearSelection()
                self._albums_list.blockSignals(True)
                self._albums_list.setCurrentItem(item)
                self._albums_list.blockSignals(False)
                return

    def select_folder_item(self, path: str) -> None:
        """Sélectionne silencieusement un dossier dans l'arbre (sans émettre de signal)."""
        def _search(item: QTreeWidgetItem) -> bool:
            if item.data(0, Qt.UserRole) == path:
                self._albums_list.clearSelection()
                self._persons_list.clearSelection()
                self._folder_tree.blockSignals(True)
                self._folder_tree.setCurrentItem(item)
                self._folder_tree.blockSignals(False)
                return True
            for i in range(item.childCount()):
                if _search(item.child(i)):
                    return True
            return False

        for i in range(self._folder_tree.topLevelItemCount()):
            if _search(self._folder_tree.topLevelItem(i)):
                return

    def _on_folder_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        self._albums_list.clearSelection()
        self._persons_list.clearSelection()
        path = item.data(0, Qt.UserRole)
        if path:
            bus.emit("library.folder_selected", folder=path)
            self.folder_selected.emit(path)

    def _on_album_clicked(self, item: QListWidgetItem) -> None:
        self._folder_tree.clearSelection()
        self._persons_list.clearSelection()
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
        menu.addSeparator()
        menu.addAction("Effacer le dossier…",
                       lambda: self._delete_folder(path))
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

    def _delete_folder(self, path: str) -> None:
        folder_name = os.path.basename(path)
        box = QMessageBox(
            QMessageBox.Warning,
            "Confirmer la suppression",
            f"Supprimer définitivement le dossier « {folder_name} » et tout son contenu ?\n\n"
            f"Cette action est irréversible. Tous les fichiers seront supprimés du disque.",
            QMessageBox.Yes | QMessageBox.Cancel,
            self,
        )
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("Supprimer")
        if box.exec() != QMessageBox.Yes:
            return
        try:
            shutil.rmtree(path)
        except Exception as e:
            QMessageBox.critical(self, "Erreur",
                                 f"Impossible de supprimer le dossier :\n{e}")
            return
        self.folder_deleted.emit(path)

    def _create_album(self) -> None:
        name, ok = QInputDialog.getText(self, "Nouvel album", "Nom de l'album :")
        if ok and name.strip():
            bus.emit("album.create_requested", name=name.strip())

    # ------------------------------------------------------------------ persons

    def set_pending_person_id(self, person_id: int | None) -> None:
        """Définit la personne à sélectionner/scroller lors du prochain refresh_persons."""
        self._pending_person_id = person_id

    def get_selected_person_id(self) -> int | None:
        """Retourne l'id de la personne actuellement sélectionnée, ou None."""
        cur = self._persons_list.currentItem()
        if cur is not None:
            p = cur.data(Qt.UserRole)
            if isinstance(p, PersonInfo):
                return p.id
        return None

    def refresh_persons(self, persons: list[PersonInfo]) -> None:
        self._persons = persons
        # Mémoriser la personne actuellement sélectionnée pour la restaurer après rebuild
        selected_id: int | None = None
        selected_row: int = -1
        scroll_to_selected: bool = False
        cur = self._persons_list.currentItem()
        if cur is not None:
            p = cur.data(Qt.UserRole)
            if isinstance(p, PersonInfo):
                selected_id = p.id
                selected_row = self._persons_list.row(cur)
        elif self._pending_person_id is not None:
            # Restauration au démarrage depuis la config
            selected_id = self._pending_person_id
            scroll_to_selected = True
        self._pending_person_id = None  # consommé dans tous les cas
        # Arrêter un chargement précédent et libérer le thread Qt enfant
        if self._face_loader is not None:
            try:
                self._face_loader.icon_ready.disconnect(self._on_face_icon_ready)
            except RuntimeError:
                pass
            if self._face_loader.isRunning():
                self._face_loader.stop()
                self._face_loader.finished.connect(self._face_loader.deleteLater)
            else:
                self._face_loader.deleteLater()
            self._face_loader = None
        self._persons_list.clear()
        for person in persons:
            label = f"{person.name}  ({person.photo_count})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, person)
            self._persons_list.addItem(item)
            if selected_id is not None and person.id == selected_id:
                self._persons_list.blockSignals(True)
                self._persons_list.setCurrentItem(item)
                if scroll_to_selected:
                    self._persons_list.scrollToItem(item)
                self._persons_list.blockSignals(False)
                selected_id = None  # trouvé — pas de fallback nécessaire
        # Personne supprimée/fusionnée : sélectionner silencieusement le voisin le plus proche
        # (ne s'applique pas à la restauration depuis la config — on préfère ne rien sélectionner)
        if selected_id is not None and not scroll_to_selected and self._persons_list.count() > 0:
            fallback_row = min(selected_row, self._persons_list.count() - 1)
            self._persons_list.blockSignals(True)
            self._persons_list.setCurrentRow(fallback_row)
            self._persons_list.blockSignals(False)
        # Charger les icônes de visage en arrière-plan
        if persons:
            self._face_loader = _FaceIconLoader(persons, self)
            self._face_loader.icon_ready.connect(self._on_face_icon_ready)
            self._face_loader.start()

    @Slot(int, bytes)
    def _on_face_icon_ready(self, index: int, data: bytes) -> None:
        if index < self._persons_list.count():
            pix = QPixmap()
            pix.loadFromData(data)
            self._persons_list.item(index).setIcon(QIcon(pix))

    def update_persons_data(self, persons: list) -> None:
        """Met à jour compteurs et icônes des personnes sans reconstruire la liste.
        Si l'ensemble des personnes a changé (ajout/suppression), bascule sur refresh_persons."""
        self._persons = persons
        new_by_id = {p.id: p for p in persons if p.id is not None}

        current_by_id: dict = {}
        for i in range(self._persons_list.count()):
            item = self._persons_list.item(i)
            p = item.data(Qt.UserRole)
            if hasattr(p, "id") and p.id is not None:
                current_by_id[p.id] = (i, p)

        if set(new_by_id.keys()) != set(current_by_id.keys()):
            self.refresh_persons(persons)
            return

        from src.core.models import FaceInfo
        for person in persons:
            if person.id is None:
                continue
            entry = current_by_id.get(person.id)
            if entry is None:
                continue
            row, old_person = entry
            item = self._persons_list.item(row)

            new_label = f"{person.name}  ({person.photo_count})"
            if item.text() != new_label:
                item.setText(new_label)
                item.setData(Qt.UserRole, person)

            if (person.cover_path != old_person.cover_path
                    or person.cover_bbox != old_person.cover_bbox):
                if person.cover_path and person.cover_bbox:
                    face = FaceInfo(
                        photo_path=person.cover_path,
                        bbox_x=person.cover_bbox[0], bbox_y=person.cover_bbox[1],
                        bbox_w=person.cover_bbox[2], bbox_h=person.cover_bbox[3],
                        detected_rotation=getattr(person, "cover_detected_rotation", 0),
                    )
                    loader = _SingleFaceIconLoader(row, face, self)
                    loader.icon_ready.connect(self._on_face_icon_ready)
                    loader.finished.connect(loader.deleteLater)
                    loader.start()

    def apply_person_merge(self, source_id: int, target_id: int, new_count: int) -> None:
        """Remove source from list and update target count after a merge. No icon reload."""
        self._persons = [p for p in self._persons if p.id != source_id]
        for p in self._persons:
            if p.id == target_id:
                p.photo_count = new_count

        source_was_selected = False
        cur = self._persons_list.currentItem()
        if cur is not None:
            p = cur.data(Qt.UserRole)
            if isinstance(p, PersonInfo) and p.id == source_id:
                source_was_selected = True

        for i in range(self._persons_list.count()):
            item = self._persons_list.item(i)
            p = item.data(Qt.UserRole)
            if isinstance(p, PersonInfo) and p.id == source_id:
                self._persons_list.takeItem(i)
                break

        for i in range(self._persons_list.count()):
            item = self._persons_list.item(i)
            p = item.data(Qt.UserRole)
            if isinstance(p, PersonInfo) and p.id == target_id:
                p.photo_count = new_count
                item.setText(f"{p.name}  ({new_count})")
                item.setData(Qt.UserRole, p)
                if source_was_selected:
                    self._persons_list.blockSignals(True)
                    self._persons_list.setCurrentItem(item)
                    self._persons_list.blockSignals(False)
                break

    def update_person_icon(self, person_id: int, face) -> None:
        """Mise à jour immédiate de l'icône d'une personne sans reconstruire toute la liste."""
        for i in range(self._persons_list.count()):
            item = self._persons_list.item(i)
            p = item.data(Qt.UserRole)
            if isinstance(p, PersonInfo) and p.id == person_id:
                loader = _SingleFaceIconLoader(i, face, self)
                loader.icon_ready.connect(self._on_face_icon_ready)
                loader.finished.connect(loader.deleteLater)
                loader.start()
                break

    def update_cluster_badge(self, count: int) -> None:
        """Mettre à jour le badge du bouton Identifier avec le nombre de groupes en attente."""
        self._btn_identify.set_badge(count)

    def _on_person_clicked(self, item: QListWidgetItem) -> None:
        self._folder_tree.clearSelection()
        self._albums_list.clearSelection()
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
        menu.addSeparator()
        menu.addAction("Effacer le nom…",
                       lambda: self.person_clear_requested.emit(person))
        menu.exec(self._persons_list.mapToGlobal(pos))
