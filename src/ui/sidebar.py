# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import os
import logging
import shutil
import subprocess

from PySide6.QtCore import Signal, Qt, QRect, QSize, QUrl, QThread, QTimer, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QStyle, QStyleOptionViewItem, QStyledItemDelegate,
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QMenu, QInputDialog, QMessageBox, QFileDialog,
)

from src.core.event_bus import bus
from src.core.models import AlbumInfo, PersonInfo
from src.library.fs_utils import find_dvd_video_ts
from src.ui.people_panel import _face_bytes, _load_edit_rotations
from src.ui.ui_utils import install_menu_width_fix
from src.core.i18n import translate

logger = logging.getLogger(__name__)

_SPECIAL_ALL    = "__all__"
_SPECIAL_FAV    = "__favorites__"
_SPECIAL_VIDEOS = "__videos__"
_SPECIAL_RATED  = "__rated__"


_MIME_PHOTOS = 'application/x-pixelphoto-paths'


class _FolderTree(QTreeWidget):
    """QTreeWidget accepting the internal drops of photos from the grid."""
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


_SPECIAL_PERSON   = "__person__"    # prefix for the person context identifier
_SPECIAL_FILENAME = "__filename__"  # virtual album "By filename"
_SPECIAL_TAG = "__tag__"            # virtual album "By keyword" (header of the group)
_SPECIAL_TAG_ITEM_PREFIX = "__tag__:"  # prefix of the one-keyword-per-line sub-entries
_SPECIAL_RATED_ITEM_PREFIX = "__rated__:"  # prefix of the minimum-rating sub-entries (1 to 5)


class _BadgeButton(QPushButton):
    """QPushButton with a simple red dot in the top-right corner as long as the count is > 0."""

    _DOT_DIAMETER = 9

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

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        d = self._DOT_DIAMETER
        x = self.width() - d - 2
        y = 2

        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#d94f4f"))
        p.drawEllipse(x, y, d, d)
        p.end()


class _FaceIconLoader(QThread):
    """Loads the face crops in the background to avoid freezing the UI thread.

    Only receives the (row index, person) pairs whose icon is not already
    in the session cache of the Sidebar - the cached icons are set
    immediately by refresh_persons, without re-decoding the originals."""

    icon_ready = Signal(int, bytes)   # (index in the list, PNG bytes)

    def __init__(self, items: "list[tuple[int, object]]", parent=None) -> None:
        super().__init__(parent)
        self._items = items
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        from src.core.models import FaceInfo
        cover_paths = [p.cover_path for _, p in self._items if p.cover_path and p.cover_bbox]
        edit_rots = _load_edit_rotations(cover_paths)
        for i, person in self._items:
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


class _FolderTrashThread(QThread):
    """Sends a whole folder to the recycle bin off the UI thread."""

    done = Signal(str, str)   # (path, error message - empty on success)

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        try:
            from src.library.trash import move_to_trash
            move_to_trash(self._path)
            self.done.emit(self._path, "")
        except Exception as e:
            self.done.emit(self._path, str(e))


class _SingleFaceIconLoader(QThread):
    """Loads the crop of a single face in the background to update an icon."""

    icon_ready = Signal(int, bytes)   # (index in the list, PNG bytes)

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
    """Displays an orange badge with the number of suggestions between the thumbnail and the name."""

    _R = 9   # radius of the badge in px

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

        # 1. Background (selection, hover...)
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, widget)

        # 2. Icon
        icon_rect = style.subElementRect(QStyle.SE_ItemViewItemDecoration, opt, None)
        if not opt.icon.isNull() and icon_rect.isValid():
            mode = QIcon.Selected if (opt.state & QStyle.State_Selected) else QIcon.Normal
            opt.icon.paint(painter, icon_rect, Qt.AlignCenter, mode)

        # 3. Position of the badge (just after the icon)
        r = self._R
        bx = (icon_rect.right() + 3) if icon_rect.isValid() else (opt.rect.left() + 44)
        by = opt.rect.center().y() - r

        # 4. Text shifted after the badge
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
    album_delete_requested = Signal(object)  # AlbumInfo to delete
    tag_delete_requested = Signal(str)    # keyword to remove from every photo
    scan_requested     = Signal(str)
    folder_removed     = Signal(str)
    folder_created     = Signal(str)      # path of the newly created subfolder
    folder_moved       = Signal(str, str) # (old_path, new_path)
    folder_deleted     = Signal(str)      # folder deleted from the disk
    photos_dropped     = Signal(list, str) # (file_paths, dest_folder_path)
    duplicates_requested   = Signal()         # open the grid of the duplicate groups
    person_selected        = Signal(object)   # PersonInfo
    identify_requested     = Signal()         # open PeopleDialog
    person_merge_requested  = Signal(object)   # PersonInfo to merge
    person_rename_requested = Signal(object)  # PersonInfo to rename
    person_clear_requested  = Signal(object)  # PersonInfo whose name is being cleared
    tree_state_changed     = Signal(list)     # list[str] - expanded paths
    section_collapse_changed = Signal(str, bool)  # ("ratings"|"tags", collapsed?)
    persons_thumbnails_ready = Signal()       # face thumbnails of the known people loaded
    advanced_search_requested = Signal()      # magnifier button next to the filter

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded_paths: set[str] = set()
        self._restoring: bool = False
        self._face_loader: _FaceIconLoader | None = None
        self._pending_person_id: int | None = None
        # Session cache of the face icons (36 px, PNG) by
        # (cover_path, cover_bbox): refresh_persons otherwise re-decoded ALL
        # the covers from the original photos at every rebuild
        # (end of scan, renaming, assignment...) while nearly all of them
        # had not changed. Purged of its orphan entries at every rebuild.
        self._icon_bytes_cache: dict[tuple, bytes] = {}
        self._folder_order_mode: str = "alpha"   # "alpha" | "chrono"
        self._folder_order_dir: str = "asc"       # "asc" | "desc"
        self._folder_count_provider = None   # Callable[[list[str]], dict[str, int]] | None
        self._setup_ui()

    def set_folder_count_provider(self, provider) -> None:
        """Injects the recursive counting function (typically
        Catalog.get_recursive_photo_counts) used to display the number of
        photos/videos next to each folder of the tree. Sidebar therefore does not
        depend directly on Catalog - consistent with the fact that refresh_folders()
        already only receives paths, never a catalog object."""
        self._folder_count_provider = provider

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(0)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText(translate("Sidebar", "🔍  Filter folders, people "
                                                                 "and files…"))
        self._filter_box.setClearButtonEnabled(True)
        self._filter_box.setStyleSheet("padding: 4px 6px; background: #2a2a2a; color: #ddd; border: none; border-bottom: 1px solid #444;")
        self._filter_box.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_box)

        self._btn_advanced_search = QPushButton("🔎")
        self._btn_advanced_search.setFlat(True)
        self._btn_advanced_search.setFixedWidth(28)
        self._btn_advanced_search.setToolTip(translate("Sidebar", "Advanced search… (Ctrl+F)"))
        self._btn_advanced_search.setStyleSheet(
            "QPushButton { background: #2a2a2a; color: #ddd; border: none;"
            " border-bottom: 1px solid #444; font-size: 13px; }"
            "QPushButton:hover { background: #3a3a3a; }"
        )
        self._btn_advanced_search.clicked.connect(self.advanced_search_requested.emit)
        filter_row.addWidget(self._btn_advanced_search)

        layout.addLayout(filter_row)

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
        _lbl = QLabel(translate("Sidebar", "Folders"))
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        folder_header_bar.addWidget(_lbl)
        folder_header_bar.addStretch()
        self._btn_duplicates = _BadgeButton(translate("Sidebar", "Duplicates"))
        self._btn_duplicates.setToolTip(translate("Sidebar", "Browse the duplicate groups found"))
        self._btn_duplicates.clicked.connect(self.duplicates_requested)
        folder_header_bar.addWidget(self._btn_duplicates)
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
        _lbl = QLabel(translate("Sidebar", "Albums"))
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        album_header_bar.addWidget(_lbl)
        album_header_bar.addStretch()
        btn_new_album = QPushButton("+")
        btn_new_album.setFixedWidth(24)
        btn_new_album.setToolTip(translate("Sidebar", "Create an album"))
        btn_new_album.clicked.connect(self._create_album)
        album_header_bar.addWidget(btn_new_album)

        album_header_container = QWidget()
        album_header_container.setStyleSheet("background: #2a2a2a;")
        album_header_container.setLayout(album_header_bar)
        aw_layout.addWidget(album_header_container)

        self._albums_list = QListWidget()
        self._albums_list.itemClicked.connect(self._on_album_clicked)
        self._albums_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._albums_list.customContextMenuRequested.connect(self._album_context_menu)
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
        _lbl = QLabel(translate("Sidebar", "People"))
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        persons_header_bar.addWidget(_lbl)
        self._persons_count_lbl = QLabel("(0)")
        self._persons_count_lbl.setStyleSheet("color: #888;")
        persons_header_bar.addWidget(self._persons_count_lbl)
        persons_header_bar.addStretch()
        self._btn_identify = _BadgeButton(translate("Sidebar", "Identify…"))
        self._btn_identify.setToolTip(translate("Sidebar", "Name the face groups found"))
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
        self._tag_items_count = 0
        self._tags_collapsed = False
        self._tag_names_cache: list[str] = []
        self._rated_items_count = 0
        self._ratings_collapsed = False

        self._add_special_albums()

    # ── border position persistence ────────────────────────────────────────

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
        item_all = QListWidgetItem(translate("Sidebar", "★ Timeline of every photo"))
        item_all.setData(Qt.UserRole, _SPECIAL_ALL)
        self._albums_list.addItem(item_all)
        self._albums_list.setCurrentItem(item_all)

        item_fav = QListWidgetItem(translate("Sidebar", "♡ Favourites"))
        item_fav.setData(Qt.UserRole, _SPECIAL_FAV)
        self._albums_list.addItem(item_fav)

        item_vid = QListWidgetItem(translate("Sidebar", "▶ Videos"))
        item_vid.setData(Qt.UserRole, _SPECIAL_VIDEOS)
        self._albums_list.addItem(item_vid)

        item_rated = QListWidgetItem(self._rated_header_label())
        item_rated.setData(Qt.UserRole, _SPECIAL_RATED)
        item_rated.setToolTip(translate("Sidebar", "Click to fold/unfold the rating levels"))
        self._albums_list.addItem(item_rated)
        self._rated_header_item = item_rated
        self._render_rated_subitems()

        item_fn = QListWidgetItem(translate("Sidebar", "🔍 By file name"))
        item_fn.setData(Qt.UserRole, _SPECIAL_FILENAME)
        item_fn.setToolTip(translate("Sidebar", "Show the photos whose file name contains the "
                                                "filter text"))
        self._albums_list.addItem(item_fn)

        item_tag = QListWidgetItem(self._tag_header_label())
        item_tag.setData(Qt.UserRole, _SPECIAL_TAG)
        item_tag.setToolTip(translate("Sidebar", "Click to fold/unfold the list of existing "
                                                 "keywords"))
        self._albums_list.addItem(item_tag)
        self._tag_header_item = item_tag

    # ── live filtering ─────────────────────────────────────────────────────────

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
        """Hides/shows item according to q. Returns True if item or a descendant matches."""
        if item.data(0, Qt.UserRole) is None:
            return True  # lazy-load placeholder, do not touch
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

    # ── border position persistence ────────────────────────────────────────

    def set_section_collapsed_state(self, ratings_collapsed: bool, tags_collapsed: bool) -> None:
        """Restores the collapsed/expanded state of "By ratings"/"By keyword" memorised in
        the config (to be called before refresh_tags(), at startup)."""
        self._ratings_collapsed = ratings_collapsed
        self._tags_collapsed = tags_collapsed
        self._rated_header_item.setText(self._rated_header_label())
        self._tag_header_item.setText(self._tag_header_label())
        self._render_rated_subitems()
        self._render_tag_subitems()

    def set_tree_expanded_paths(self, paths: list[str]) -> None:
        """Initialises the memorised state from the config (to be called before refresh_folders)."""
        self._expanded_paths = set(paths)

    def set_folder_order(self, mode: str, direction: str) -> None:
        """Sets the sort order of the Folders panel (roots + subfolders).
        mode: "alpha" | "chrono" - direction: "asc" | "desc".
        Taken into account at the next refresh_folders()/expand."""
        self._folder_order_mode = mode
        self._folder_order_dir = direction

    def _sort_folder_paths(self, paths: list[str]) -> list[str]:
        reverse = self._folder_order_dir == "desc"
        if self._folder_order_mode == "chrono":
            def key(p):
                try:
                    return os.path.getmtime(p)
                except OSError:
                    return 0.0
            return sorted(paths, key=key, reverse=reverse)
        return sorted(paths, key=lambda p: (os.path.basename(p) or p).lower(), reverse=reverse)

    def _mark_if_dvd_copy(self, item: QTreeWidgetItem, path: str, count) -> None:
        """"DVD copy" badge (disc icon + tooltip) if path contains a
        VIDEO_TS subfolder. Restricted to the folders with no catalogued photo
        (count empty/zero): that is precisely the case where the folder would look
        empty without this indication, and it avoids one more os.scandir
        per displayed folder - cf. the comment of _populate_subfolders on
        the cost of a scandir per child on a network volume."""
        if count:
            return
        if find_dvd_video_ts(path) is None:
            return
        item.setIcon(0, self.style().standardIcon(QStyle.SP_DriveCDIcon))
        item.setToolTip(0, translate("Sidebar", "DVD copy (VIDEO_TS)"))

    def refresh_folders(self, folders: list[str]) -> None:
        self._folder_tree.clear()
        counts = self._folder_count_provider(list(folders)) if self._folder_count_provider else {}
        for folder in self._sort_folder_paths(list(folders)):
            label = os.path.basename(folder) or folder
            count = counts.get(os.path.normpath(folder))
            if count is not None:
                label = f"{label} ({count})"
            root_item = QTreeWidgetItem([label])
            root_item.setData(0, Qt.UserRole, folder)
            root_item.setToolTip(0, folder)
            self._mark_if_dvd_copy(root_item, folder, count)
            # Placeholder: makes the node expandable without touching the disk.
            # The subfolders are loaded on demand in _on_folder_expanded.
            root_item.addChild(QTreeWidgetItem([""]))
            self._folder_tree.addTopLevelItem(root_item)
            # No setExpanded() here: with >100 folders, _populate_subfolders
            # on each root blocks the UI (scandir x N folders).

        # Restore the memorised expansion state
        if self._expanded_paths:
            self._restoring = True
            try:
                for path in sorted(self._expanded_paths):
                    self._restore_expand(path)
            finally:
                self._restoring = False

    def _populate_subfolders(self, parent_item: QTreeWidgetItem, folder_path: str) -> None:
        """Adds the immediate subfolders of folder_path under parent_item.
        Each child systematically receives a placeholder (lazy loading on
        expansion) - the same principle as the root nodes in refresh_folders:
        checking whether it really has subfolders would cost one os.scandir
        per child (very slow on a network volume), for the sole benefit of
        hiding the chevron of the empty nodes. A node without a subfolder simply
        collapses at the first expansion."""
        try:
            dirs = [e for e in os.scandir(folder_path)
                    if e.is_dir() and not e.name.startswith(".")]
            reverse = self._folder_order_dir == "desc"
            if self._folder_order_mode == "chrono":
                def key(e):
                    try:
                        return e.stat().st_mtime
                    except OSError:
                        return 0.0
            else:
                def key(e):
                    return e.name.lower()
            sorted_dirs = sorted(dirs, key=key, reverse=reverse)
            counts = (
                self._folder_count_provider([e.path for e in sorted_dirs])
                if self._folder_count_provider else {}
            )
            for entry in sorted_dirs:
                label = entry.name
                count = counts.get(os.path.normpath(entry.path))
                if count is not None:
                    label = f"{label} ({count})"
                child = QTreeWidgetItem([label])
                child.setData(0, Qt.UserRole, entry.path)
                child.setToolTip(0, entry.path)
                self._mark_if_dvd_copy(child, entry.path, count)
                parent_item.addChild(child)
                # Placeholder -> makes the node expandable
                child.addChild(QTreeWidgetItem([""]))
        except PermissionError:
            pass

    def _on_folder_expanded(self, item: QTreeWidgetItem) -> None:
        """Lazy loading + memorising of the expansion state."""
        # Lazy loading if the node only has a placeholder without data
        if item.childCount() == 1 and item.child(0).data(0, Qt.UserRole) is None:
            folder_path = item.data(0, Qt.UserRole)
            if folder_path:
                item.removeChild(item.child(0))
                self._populate_subfolders(item, folder_path)
        # Memorise
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
        # Also delete every descendant (no longer visible)
        prefix = os.path.normcase(path + os.sep)
        self._expanded_paths = {
            p for p in self._expanded_paths
            if not os.path.normcase(p).startswith(prefix)
        }
        self.tree_state_changed.emit(list(self._expanded_paths))

    def _restore_expand(self, target: str) -> None:
        """Looks for target in the tree and expands the path to it."""
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
        """Recursively expands towards norm_target, loading the lazy nodes if needed."""
        item_path = item.data(0, Qt.UserRole)
        if not item_path:
            return
        norm_item = os.path.normcase(item_path)
        # Populate if the placeholder has not been loaded yet
        if item.childCount() == 1 and item.child(0).data(0, Qt.UserRole) is None:
            item.removeChild(item.child(0))
            self._populate_subfolders(item, item_path)
        item.setExpanded(True)
        if norm_item == norm_target:
            return
        # Go down to the child containing target
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
        # Remove existing album items (keep the 6 special ones at top: Chronologie,
        # Favoris, Videos, Par notes, Par nom de fichier, Par mot-cle - plus the
        # rating and keyword sub-entries already inserted right after their
        # respective headers).
        base = 6 + self._rated_items_count + self._tag_items_count
        while self._albums_list.count() > base:
            self._albums_list.takeItem(base)
        for album in albums:
            item = QListWidgetItem(f"📁 {album.name} ({album.photo_count})")  # album names: user data
            item.setData(Qt.UserRole, album)
            self._albums_list.addItem(item)

    def refresh_tags(self, tags: list[str]) -> None:
        """Rebuilds the sub-entries (one per existing keyword) under the
        "By keyword" header, just before the user albums. Selecting one
        of those sub-entries directly displays the photos carrying that keyword
        (cf. main_window._on_album_selected, prefix _SPECIAL_TAG_ITEM_PREFIX).
        If the section is collapsed (_tags_collapsed), the sub-entries are
        memorised in _tag_names_cache but not inserted in the list - they
        reappear as they are at the next expansion, without a new refresh_tags."""
        self._tag_names_cache = tags
        self._render_tag_subitems()

    def _tag_header_label(self) -> str:
        arrow = "▸" if self._tags_collapsed else "▾"
        return f"{arrow} 🏷 " + translate("Sidebar", "By keyword")

    def _render_tag_subitems(self) -> None:
        base = 6 + self._rated_items_count
        while self._tag_items_count > 0:
            self._albums_list.takeItem(base)
            self._tag_items_count -= 1
        if self._tags_collapsed:
            return
        for i, tag in enumerate(self._tag_names_cache):
            item = QListWidgetItem(f"      🏷 {tag}")
            item.setData(Qt.UserRole, _SPECIAL_TAG_ITEM_PREFIX + tag)
            self._albums_list.insertItem(base + i, item)
        self._tag_items_count = len(self._tag_names_cache)

    def _rated_header_label(self) -> str:
        arrow = "▸" if self._ratings_collapsed else "▾"
        return f"{arrow} ★ " + translate("Sidebar", "By rating")

    def _render_rated_subitems(self) -> None:
        base = 4
        while self._rated_items_count > 0:
            self._albums_list.takeItem(base)
            self._rated_items_count -= 1
        if self._ratings_collapsed:
            return
        for i, n in enumerate(range(5, 0, -1)):
            item = QListWidgetItem(f"      {'★' * n}{'☆' * (5 - n)}")
            item.setData(Qt.UserRole, _SPECIAL_RATED_ITEM_PREFIX + str(n))
            item.setToolTip(translate("Sidebar", "Photos rated %n star(s) or more", None, n))
            self._albums_list.insertItem(base + i, item)
        self._rated_items_count = 5

    def select_album_item(self, data) -> None:
        """Silently selects an album in the list (without emitting a signal)."""
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
        """Silently selects a folder in the tree (without emitting a signal)."""
        def _search(item: QTreeWidgetItem) -> bool:
            if item.data(0, Qt.UserRole) == path:
                self._albums_list.clearSelection()
                self._persons_list.clearSelection()
                self._folder_tree.blockSignals(True)
                self._folder_tree.setCurrentItem(item)
                self._folder_tree.blockSignals(False)
                # Deferred: on the first display, the geometry of the tree
                # (rows/scrollbar) is not resolved yet at the time of this
                # call (cf. _ensure_left_pane_min_width, the same layout trap).
                QTimer.singleShot(
                    0,
                    lambda it=item: self._folder_tree.scrollToItem(
                        it, QAbstractItemView.PositionAtCenter
                    ),
                )
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
        data = item.data(Qt.UserRole)
        if data == _SPECIAL_TAG:
            self._tags_collapsed = not self._tags_collapsed
            item.setText(self._tag_header_label())
            self._render_tag_subitems()
            self.section_collapse_changed.emit("tags", self._tags_collapsed)
        elif data == _SPECIAL_RATED:
            self._ratings_collapsed = not self._ratings_collapsed
            item.setText(self._rated_header_label())
            self._render_rated_subitems()
            self.section_collapse_changed.emit("ratings", self._ratings_collapsed)
        self._folder_tree.clearSelection()
        self._persons_list.clearSelection()
        self.album_selected.emit(data)

    def _album_context_menu(self, pos) -> None:
        item = self._albums_list.itemAt(pos)
        if not item:
            return
        data = item.data(Qt.UserRole)
        if isinstance(data, AlbumInfo):
            menu = QMenu(self)
            install_menu_width_fix(menu)
            menu.addAction(translate("Sidebar", "Delete the album…"),
                           lambda: self.album_delete_requested.emit(data))
            menu.exec(self._albums_list.mapToGlobal(pos))
        elif isinstance(data, str) and data.startswith(_SPECIAL_TAG_ITEM_PREFIX):
            tag = data[len(_SPECIAL_TAG_ITEM_PREFIX):]
            menu = QMenu(self)
            install_menu_width_fix(menu)
            menu.addAction(translate("Sidebar", "Delete this keyword…"),
                           lambda: self.tag_delete_requested.emit(tag))
            menu.exec(self._albums_list.mapToGlobal(pos))
        # other special albums (Timeline, Favorites, Videos...): no menu

    def _folder_context_menu(self, pos) -> None:
        item = self._folder_tree.itemAt(pos)
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        install_menu_width_fix(menu)
        menu.addAction(translate("Sidebar", "Scan now"), lambda: self.scan_requested.emit(path))
        menu.addAction(translate("Sidebar", "Stop watching this folder"),
                       lambda: self.folder_removed.emit(path))
        menu.addSeparator()
        menu.addAction(translate("Sidebar", "Create a subfolder…"),
                       lambda: self._create_subfolder(path))
        menu.addAction(translate("Sidebar", "Rename…"),
                       lambda: self._rename_folder(path))
        menu.addAction(translate("Sidebar", "Move to…"),
                       lambda: self._move_folder(path))
        menu.addSeparator()
        menu.addAction(translate("Sidebar", "Open in File Explorer"),
                       lambda p=path: subprocess.Popen(["explorer", p]))
        menu.addSeparator()
        menu.addAction(translate("Sidebar", "Delete the folder…"),
                       lambda: self._delete_folder(path))
        menu.exec(self._folder_tree.mapToGlobal(pos))

    def _create_subfolder(self, parent_path: str) -> None:
        name, ok = QInputDialog.getText(
            self, translate("Sidebar", "New subfolder"),
            translate("Sidebar", "Name of the subfolder inside “{parent}”:"
                      ).format(parent=os.path.basename(parent_path)),
        )
        if not ok or not name.strip():
            return
        new_path = os.path.join(parent_path, name.strip())
        try:
            os.makedirs(new_path, exist_ok=False)
        except FileExistsError:
            QMessageBox.warning(self, translate("Sidebar", "Folder already exists"),
                                translate("Sidebar", "“{name}” already exists in this folder.")
                                .format(name=name.strip()))
            return
        except Exception as e:
            QMessageBox.critical(self, translate("Sidebar", "Error"),
                                 translate("Sidebar", "Cannot create the folder:\n{error}")
                                 .format(error=e))
            return
        self.folder_created.emit(new_path)

    def _rename_folder(self, path: str) -> None:
        current_name = os.path.basename(path)
        new_name, ok = QInputDialog.getText(
            self, translate("Sidebar", "Rename the folder"), translate("Sidebar", "New name:"), text=current_name,
        )
        if not ok or not new_name.strip() or new_name.strip() == current_name:
            return
        new_path = os.path.join(os.path.dirname(path), new_name.strip())
        try:
            os.rename(path, new_path)
        except Exception as e:
            QMessageBox.critical(self, translate("Sidebar", "Error"),
                                 translate("Sidebar", "Cannot rename the folder:\n{error}")
                                 .format(error=e))
            return
        self.folder_moved.emit(path, new_path)

    def _move_folder(self, path: str) -> None:
        folder_name = os.path.basename(path)
        dst = QFileDialog.getExistingDirectory(
            self, translate("Sidebar", "Move “{name}” — choose the destination folder"
                            ).format(name=folder_name),
            os.path.dirname(path),
        )
        if not dst:
            return
        new_path = os.path.join(dst, folder_name)
        if os.path.normcase(new_path) == os.path.normcase(path):
            return  # the same location, nothing to do
        if os.path.exists(new_path):
            QMessageBox.warning(self, translate("Sidebar", "Folder already exists"),
                                translate("Sidebar", "“{path}” already exists.")
                                .format(path=new_path))
            return
        try:
            shutil.move(path, dst)
        except Exception as e:
            QMessageBox.critical(self, translate("Sidebar", "Error"),
                                 translate("Sidebar", "Cannot move the folder:\n{error}")
                                 .format(error=e))
            return
        self.folder_moved.emit(path, new_path)

    def _delete_folder(self, path: str) -> None:
        folder_name = os.path.basename(path)
        box = QMessageBox(
            QMessageBox.Warning,
            translate("Sidebar", "Confirm deletion"),
            translate("Sidebar",
                      "Send the folder “{name}” and everything in it to the Windows recycle "
                      "bin?\n\nThe folder will still be recoverable from the recycle bin."
                      ).format(name=folder_name),
            QMessageBox.Yes | QMessageBox.Cancel,
            self,
        )
        box.setDefaultButton(QMessageBox.Cancel)
        # "Delete" and not "Remove": the "Remove" button of the folder manager
        # only takes the folder out of the watch list, this one
        # sends it to the recycle bin with all its content.
        box.button(QMessageBox.Yes).setText(translate("Sidebar", "Delete"))
        if box.exec() != QMessageBox.Yes:
            return
        # Sending to the recycle bin in a thread: on a large folder (or a
        # slow volume) the operation goes far beyond the 50 ms of the
        # "the UI never blocks" rule - the old shutil.rmtree already violated it.
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self._trash_thread = _FolderTrashThread(path, self)
        self._trash_thread.done.connect(self._on_folder_trashed)
        self._trash_thread.finished.connect(self._trash_thread.deleteLater)
        self._trash_thread.start()

    @Slot(str, str)
    def _on_folder_trashed(self, path: str, error: str) -> None:
        QApplication.restoreOverrideCursor()
        if error:
            QMessageBox.critical(
                self, translate("Sidebar", "Error"),
                translate("Sidebar",
                          "Cannot send the folder to the recycle bin:\n{error}\n\nThe folder "
                          "was NOT deleted.").format(error=error),
            )
            return
        self.folder_deleted.emit(path)

    def _create_album(self) -> None:
        name, ok = QInputDialog.getText(self, translate("Sidebar", "New album"), translate("Sidebar", "Album "
                                                                                                         "name:"))
        if ok and name.strip():
            bus.emit("album.create_requested", name=name.strip())

    # ------------------------------------------------------------------ persons

    def set_pending_person_id(self, person_id: int | None) -> None:
        """Sets the person to select/scroll to at the next refresh_persons."""
        self._pending_person_id = person_id

    def get_selected_person_id(self) -> int | None:
        """Returns the id of the currently selected person, or None."""
        cur = self._persons_list.currentItem()
        if cur is not None:
            p = cur.data(Qt.UserRole)
            if isinstance(p, PersonInfo):
                return p.id
        return None

    def _cancel_face_loader(self) -> None:
        """Stops an icon loading in progress and releases the child Qt thread.
        To be called before any operation changing the row indices of the list
        (clear+rebuild, takeItem): the icon_ready(index, ...) signals in flight carry
        indices that would otherwise become associated with the wrong person."""
        if self._face_loader is None:
            return
        try:
            self._face_loader.icon_ready.disconnect(self._on_face_icon_ready)
        except RuntimeError:
            pass
        try:
            self._face_loader.finished.disconnect(self._on_face_loader_finished)
        except RuntimeError:
            pass
        if self._face_loader.isRunning():
            self._face_loader.stop()
            self._face_loader.finished.connect(self._face_loader.deleteLater)
        else:
            self._face_loader.deleteLater()
        self._face_loader = None

    def refresh_persons(self, persons: list[PersonInfo]) -> None:
        self._persons = persons
        self._persons_count_lbl.setText(f"({len(persons)})")
        # Memorise the currently selected person so as to restore them after the rebuild
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
            # Restoration at startup from the config
            selected_id = self._pending_person_id
            scroll_to_selected = True
        self._pending_person_id = None  # consumed in every case
        self._cancel_face_loader()
        self._persons_list.clear()
        to_load: list[tuple[int, PersonInfo]] = []
        for row, person in enumerate(persons):
            label = f"{person.name}  ({person.photo_count})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, person)
            self._persons_list.addItem(item)
            # Icon from the session cache if the cover has not changed,
            # otherwise to be loaded in the background (decoding of the original).
            cached = self._icon_bytes_cache.get(self._icon_cache_key(person))
            if cached is not None:
                pix = QPixmap()
                pix.loadFromData(cached)
                item.setIcon(QIcon(pix))
            elif person.cover_path and person.cover_bbox:
                to_load.append((row, person))
            if selected_id is not None and person.id == selected_id:
                self._persons_list.blockSignals(True)
                self._persons_list.setCurrentItem(item)
                if scroll_to_selected:
                    self._persons_list.scrollToItem(item)
                self._persons_list.blockSignals(False)
                selected_id = None  # found - no fallback needed
        # Person deleted/merged: silently select the closest neighbour
        # (does not apply to the restoration from the config - we prefer to select nothing)
        if selected_id is not None and not scroll_to_selected and self._persons_list.count() > 0:
            fallback_row = min(selected_row, self._persons_list.count() - 1)
            self._persons_list.blockSignals(True)
            self._persons_list.setCurrentRow(fallback_row)
            self._persons_list.blockSignals(False)
        # Reapply the current filter (the rebuild erases the masking of the items)
        self._filter_persons_list(self.filter_text.lower())
        # Purge of the orphan entries (person deleted/merged, cover changed)
        valid_keys = {self._icon_cache_key(p) for p in persons}
        self._icon_bytes_cache = {
            k: v for k, v in self._icon_bytes_cache.items() if k in valid_keys
        }
        # Load in the background only the icons absent from the cache.
        # persons_thumbnails_ready must be emitted in every case: it serves
        # as a gate to the start of the duplicate detection (main_window).
        if to_load:
            self._face_loader = _FaceIconLoader(to_load, self)
            self._face_loader.icon_ready.connect(self._on_face_icon_ready)
            self._face_loader.finished.connect(self._on_face_loader_finished)
            self._face_loader.start()
        else:
            self.persons_thumbnails_ready.emit()

    @staticmethod
    def _icon_cache_key(person) -> tuple:
        bbox = person.cover_bbox
        return (person.cover_path, tuple(bbox) if bbox else None)

    @Slot(int, bytes)
    def _on_face_icon_ready(self, index: int, data: bytes) -> None:
        if index < self._persons_list.count():
            item = self._persons_list.item(index)
            pix = QPixmap()
            pix.loadFromData(data)
            item.setIcon(QIcon(pix))
            # Feeds the session cache: the key is derived from the person
            # carried by the item (robust to the rebuilds between the emit and here).
            person = item.data(Qt.UserRole)
            if isinstance(person, PersonInfo):
                self._icon_bytes_cache[self._icon_cache_key(person)] = data

    @Slot()
    def _on_face_loader_finished(self) -> None:
        self.persons_thumbnails_ready.emit()

    def update_persons_data(self, persons: list) -> None:
        """Updates the counters and icons of the people without rebuilding the list.
        If the set of people has changed (addition/deletion), switches to refresh_persons."""
        self._persons = persons
        self._persons_count_lbl.setText(f"({len(persons)})")
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
        # takeItem() below shifts the row indices: an icon loading
        # still in flight would apply its icons to the wrong person (cf. _cancel_face_loader).
        self._cancel_face_loader()
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

    def remove_person(self, person_id: int) -> None:
        """Removes a person from the list (name cleared/deleted). No icon reload."""
        # takeItem() below shifts the row indices: an icon loading
        # still in flight would apply its icons to the wrong person (cf. _cancel_face_loader).
        self._cancel_face_loader()
        self._persons = [p for p in self._persons if p.id != person_id]
        for i in range(self._persons_list.count()):
            item = self._persons_list.item(i)
            p = item.data(Qt.UserRole)
            if isinstance(p, PersonInfo) and p.id == person_id:
                self._persons_list.takeItem(i)
                break

    def update_person_icon(self, person_id: int, face) -> None:
        """Immediate update of the icon of a person without rebuilding the whole list."""
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
        """Update the badge of the Identify button with the number of pending groups."""
        self._btn_identify.set_badge(count)

    def update_duplicates_badge(self, count: int) -> None:
        """Update the badge of the Duplicates button with the number of detected groups."""
        self._btn_duplicates.set_badge(count)

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
        install_menu_width_fix(menu)
        menu.addAction(translate("Sidebar", "Rename…"),
                       lambda: self.person_rename_requested.emit(person))
        menu.addAction(translate("Sidebar", "Merge with…"),
                       lambda: self.person_merge_requested.emit(person))
        menu.addSeparator()
        menu.addAction(translate("Sidebar", "Clear the name…"),
                       lambda: self.person_clear_requested.emit(person))
        menu.exec(self._persons_list.mapToGlobal(pos))
