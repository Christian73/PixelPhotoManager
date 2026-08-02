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

logger = logging.getLogger(__name__)

_SPECIAL_ALL    = "__all__"
_SPECIAL_FAV    = "__favorites__"
_SPECIAL_VIDEOS = "__videos__"
_SPECIAL_RATED  = "__rated__"


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
_SPECIAL_TAG = "__tag__"            # album virtuel "Par mot-clé" (en-tête du groupe)
_SPECIAL_TAG_ITEM_PREFIX = "__tag__:"  # préfixe des sous-éléments un-mot-clé-par-ligne
_SPECIAL_RATED_ITEM_PREFIX = "__rated__:"  # préfixe des sous-éléments note minimale (1 à 5)


class _BadgeButton(QPushButton):
    """QPushButton avec un simple point rouge en coin supérieur droit tant que le compte est > 0."""

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
    """Charge les crops de visages en arrière-plan pour éviter de freezer le thread UI.

    Ne reçoit que les (index de ligne, personne) dont l'icône n'est pas déjà
    dans le cache session de la Sidebar — les icônes en cache sont posées
    immédiatement par refresh_persons, sans re-décoder les originaux."""

    icon_ready = Signal(int, bytes)   # (index dans la liste, PNG bytes)

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
    """Envoie un dossier entier à la corbeille hors du thread UI."""

    done = Signal(str, str)   # (path, message d'erreur — vide si succès)

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
    album_delete_requested = Signal(object)  # AlbumInfo à supprimer
    tag_delete_requested = Signal(str)    # mot-clé à retirer de toutes les photos
    scan_requested     = Signal(str)
    folder_removed     = Signal(str)
    folder_created     = Signal(str)      # chemin du nouveau sous-dossier créé
    folder_moved       = Signal(str, str) # (ancien_chemin, nouveau_chemin)
    folder_deleted     = Signal(str)      # dossier supprimé du disque
    photos_dropped     = Signal(list, str) # (file_paths, dest_folder_path)
    duplicates_requested   = Signal()         # ouvrir la grille des groupes de doublons
    person_selected        = Signal(object)   # PersonInfo
    identify_requested     = Signal()         # ouvrir PeopleDialog
    person_merge_requested  = Signal(object)   # PersonInfo à fusionner
    person_rename_requested = Signal(object)  # PersonInfo à renommer
    person_clear_requested  = Signal(object)  # PersonInfo dont on efface le nom
    tree_state_changed     = Signal(list)     # list[str] — chemins dépliés
    section_collapse_changed = Signal(str, bool)  # ("ratings"|"tags", replié ?)
    persons_thumbnails_ready = Signal()       # vignettes de visages des personnes connues chargées
    advanced_search_requested = Signal()      # bouton loupe à côté du filtre

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded_paths: set[str] = set()
        self._restoring: bool = False
        self._face_loader: _FaceIconLoader | None = None
        self._pending_person_id: int | None = None
        # Cache session des icônes de visage (36 px, PNG) par
        # (cover_path, cover_bbox) : refresh_persons re-décodait sinon TOUTES
        # les couvertures depuis les photos originales à chaque rebuild
        # (fin de scan, renommage, assignation…) alors que la quasi-totalité
        # n'a pas changé. Purgé des entrées orphelines à chaque rebuild.
        self._icon_bytes_cache: dict[tuple, bytes] = {}
        self._folder_order_mode: str = "alpha"   # "alpha" | "chrono"
        self._folder_order_dir: str = "asc"       # "asc" | "desc"
        self._folder_count_provider = None   # Callable[[list[str]], dict[str, int]] | None
        self._setup_ui()

    def set_folder_count_provider(self, provider) -> None:
        """Injecte la fonction de comptage récursif (typiquement
        Catalog.get_recursive_photo_counts) utilisée pour afficher le nombre de
        photos/vidéos à côté de chaque dossier de l'arbre. Sidebar ne dépend ainsi
        pas directement de Catalog — cohérent avec le fait que refresh_folders()
        ne reçoit déjà que des chemins, jamais d'objet catalogue."""
        self._folder_count_provider = provider

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(0)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText("🔍  Filtrer dossiers, personnes et fichiers…")
        self._filter_box.setClearButtonEnabled(True)
        self._filter_box.setStyleSheet("padding: 4px 6px; background: #2a2a2a; color: #ddd; border: none; border-bottom: 1px solid #444;")
        self._filter_box.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_box)

        self._btn_advanced_search = QPushButton("🔎")
        self._btn_advanced_search.setFlat(True)
        self._btn_advanced_search.setFixedWidth(28)
        self._btn_advanced_search.setToolTip("Recherche avancée… (Ctrl+F)")
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
        _lbl = QLabel("Dossiers")
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        folder_header_bar.addWidget(_lbl)
        folder_header_bar.addStretch()
        self._btn_duplicates = _BadgeButton("Dupliquées")
        self._btn_duplicates.setToolTip("Parcourir les groupes de doublons détectés")
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
        _lbl = QLabel("Personnes")
        _lbl.setStyleSheet("color: #ccc; font-weight: bold;")
        persons_header_bar.addWidget(_lbl)
        self._persons_count_lbl = QLabel("(0)")
        self._persons_count_lbl.setStyleSheet("color: #888;")
        persons_header_bar.addWidget(self._persons_count_lbl)
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
        self._tag_items_count = 0
        self._tags_collapsed = False
        self._tag_names_cache: list[str] = []
        self._rated_items_count = 0
        self._ratings_collapsed = False

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

        item_rated = QListWidgetItem(self._rated_header_label())
        item_rated.setData(Qt.UserRole, _SPECIAL_RATED)
        item_rated.setToolTip("Cliquer pour replier/déplier les niveaux de notation")
        self._albums_list.addItem(item_rated)
        self._rated_header_item = item_rated
        self._render_rated_subitems()

        item_fn = QListWidgetItem("🔍 Par nom de fichier")
        item_fn.setData(Qt.UserRole, _SPECIAL_FILENAME)
        item_fn.setToolTip("Afficher les photos dont le nom de fichier contient le texte du filtre")
        self._albums_list.addItem(item_fn)

        item_tag = QListWidgetItem(self._tag_header_label())
        item_tag.setData(Qt.UserRole, _SPECIAL_TAG)
        item_tag.setToolTip("Cliquer pour replier/déplier la liste des mots-clés existants")
        self._albums_list.addItem(item_tag)
        self._tag_header_item = item_tag

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

    def set_section_collapsed_state(self, ratings_collapsed: bool, tags_collapsed: bool) -> None:
        """Restaure l'état plié/déplié de "Par notes"/"Par mot-clé" mémorisé en
        config (appeler avant refresh_tags(), au démarrage)."""
        self._ratings_collapsed = ratings_collapsed
        self._tags_collapsed = tags_collapsed
        self._rated_header_item.setText(self._rated_header_label())
        self._tag_header_item.setText(self._tag_header_label())
        self._render_rated_subitems()
        self._render_tag_subitems()

    def set_tree_expanded_paths(self, paths: list[str]) -> None:
        """Initialise l'état mémorisé depuis la config (appeler avant refresh_folders)."""
        self._expanded_paths = set(paths)

    def set_folder_order(self, mode: str, direction: str) -> None:
        """Définit l'ordre de tri du panneau Dossiers (racines + sous-dossiers).
        mode: "alpha" | "chrono" — direction: "asc" | "desc".
        Pris en compte au prochain refresh_folders()/expand."""
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
        """Badge « copie de DVD » (icône disque + tooltip) si path contient un
        sous-dossier VIDEO_TS. Restreint aux dossiers sans photo cataloguée
        (count vide/nul) : c'est justement le cas où le dossier semblerait
        vide sans cette indication, et ça évite un os.scandir supplémentaire
        par dossier affiché — cf. le commentaire de _populate_subfolders sur
        le coût d'un scandir par enfant sur un volume réseau."""
        if count:
            return
        if find_dvd_video_ts(path) is None:
            return
        item.setIcon(0, self.style().standardIcon(QStyle.SP_DriveCDIcon))
        item.setToolTip(0, "Copie de DVD (VIDEO_TS)")

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

    def _populate_subfolders(self, parent_item: QTreeWidgetItem, folder_path: str) -> None:
        """Ajoute les sous-dossiers immédiats de folder_path sous parent_item.
        Chaque enfant reçoit systématiquement un placeholder (lazy loading à
        l'expansion) — même principe que les nœuds racine dans refresh_folders :
        vérifier s'il a réellement des sous-dossiers coûterait un os.scandir
        par enfant (très lent sur un volume réseau), pour seul bénéfice de
        masquer le chevron des nœuds vides. Un nœud sans sous-dossier se
        replie simplement à la première expansion."""
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
        # Remove existing album items (keep the 6 special ones at top : Chronologie,
        # Favoris, Vidéos, Par notes, Par nom de fichier, Par mot-clé — plus les
        # sous-éléments de notes et de mots-clés déjà insérés juste après leurs
        # en-têtes respectifs).
        base = 6 + self._rated_items_count + self._tag_items_count
        while self._albums_list.count() > base:
            self._albums_list.takeItem(base)
        for album in albums:
            item = QListWidgetItem(f"📁 {album.name} ({album.photo_count})")
            item.setData(Qt.UserRole, album)
            self._albums_list.addItem(item)

    def refresh_tags(self, tags: list[str]) -> None:
        """Reconstruit les sous-éléments (un par mot-clé existant) sous l'en-tête
        « Par mot-clé », juste avant les albums utilisateur. Sélectionner l'un
        de ces sous-éléments affiche directement les photos portant ce mot-clé
        (cf. main_window._on_album_selected, préfixe _SPECIAL_TAG_ITEM_PREFIX).
        Si la section est repliée (_tags_collapsed), les sous-éléments sont
        mémorisés dans _tag_names_cache mais pas insérés dans la liste — ils
        réapparaissent tels quels au prochain dépli, sans nouveau refresh_tags."""
        self._tag_names_cache = tags
        self._render_tag_subitems()

    def _tag_header_label(self) -> str:
        arrow = "▸" if self._tags_collapsed else "▾"
        return f"{arrow} 🏷 Par mot-clé"

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
        return f"{arrow} ★ Par notes"

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
            item.setToolTip(f"Photos notées {n} étoile(s) ou plus")
            self._albums_list.insertItem(base + i, item)
        self._rated_items_count = 5

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
                # Différé : au premier affichage, la géométrie de l'arbre
                # (lignes/scrollbar) n'est pas encore résolue au moment de cet
                # appel (cf. _ensure_left_pane_min_width, même piège de layout).
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
            menu.addAction("Supprimer l'album…",
                           lambda: self.album_delete_requested.emit(data))
            menu.exec(self._albums_list.mapToGlobal(pos))
        elif isinstance(data, str) and data.startswith(_SPECIAL_TAG_ITEM_PREFIX):
            tag = data[len(_SPECIAL_TAG_ITEM_PREFIX):]
            menu = QMenu(self)
            install_menu_width_fix(menu)
            menu.addAction("Supprimer ce mot-clé…",
                           lambda: self.tag_delete_requested.emit(tag))
            menu.exec(self._albums_list.mapToGlobal(pos))
        # autres albums spéciaux (Chronologie, Favoris, Vidéos…) : pas de menu

    def _folder_context_menu(self, pos) -> None:
        item = self._folder_tree.itemAt(pos)
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        install_menu_width_fix(menu)
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
                       lambda p=path: subprocess.Popen(["explorer", p]))
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
            f"Envoyer le dossier « {folder_name} » et tout son contenu "
            f"à la corbeille Windows ?\n\n"
            f"Le dossier restera récupérable depuis la corbeille.",
            QMessageBox.Yes | QMessageBox.Cancel,
            self,
        )
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("Supprimer")
        if box.exec() != QMessageBox.Yes:
            return
        # Mise à la corbeille dans un thread : sur un gros dossier (ou un
        # volume lent) l'opération dépasse largement les 50 ms de la règle
        # « l'UI ne bloque jamais » — l'ancien shutil.rmtree la violait déjà.
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
                self, "Erreur",
                f"Impossible d'envoyer le dossier à la corbeille :\n{error}\n\n"
                f"Le dossier n'a PAS été supprimé.",
            )
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

    def _cancel_face_loader(self) -> None:
        """Arrête un chargement d'icônes en cours et libère le thread Qt enfant.
        À appeler avant toute opération qui change les indices de ligne de la liste
        (clear+rebuild, takeItem) : les signaux icon_ready(index, ...) en vol portent
        des index qui deviendraient sinon associés à la mauvaise personne."""
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
        self._cancel_face_loader()
        self._persons_list.clear()
        to_load: list[tuple[int, PersonInfo]] = []
        for row, person in enumerate(persons):
            label = f"{person.name}  ({person.photo_count})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, person)
            self._persons_list.addItem(item)
            # Icône depuis le cache session si la couverture n'a pas changé,
            # sinon à charger en arrière-plan (décodage de l'original).
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
                selected_id = None  # trouvé — pas de fallback nécessaire
        # Personne supprimée/fusionnée : sélectionner silencieusement le voisin le plus proche
        # (ne s'applique pas à la restauration depuis la config — on préfère ne rien sélectionner)
        if selected_id is not None and not scroll_to_selected and self._persons_list.count() > 0:
            fallback_row = min(selected_row, self._persons_list.count() - 1)
            self._persons_list.blockSignals(True)
            self._persons_list.setCurrentRow(fallback_row)
            self._persons_list.blockSignals(False)
        # Réappliquer le filtre en cours (rebuild efface le masquage des items)
        self._filter_persons_list(self.filter_text.lower())
        # Purge des entrées orphelines (personne supprimée/fusionnée, couverture changée)
        valid_keys = {self._icon_cache_key(p) for p in persons}
        self._icon_bytes_cache = {
            k: v for k, v in self._icon_bytes_cache.items() if k in valid_keys
        }
        # Charger en arrière-plan uniquement les icônes absentes du cache.
        # persons_thumbnails_ready doit être émis dans tous les cas : il sert
        # de gate au démarrage de la détection de doublons (main_window).
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
            # Alimente le cache session : la clé est dérivée de la personne
            # portée par l'item (robuste aux rebuilds entre l'emit et ici).
            person = item.data(Qt.UserRole)
            if isinstance(person, PersonInfo):
                self._icon_bytes_cache[self._icon_cache_key(person)] = data

    @Slot()
    def _on_face_loader_finished(self) -> None:
        self.persons_thumbnails_ready.emit()

    def update_persons_data(self, persons: list) -> None:
        """Met à jour compteurs et icônes des personnes sans reconstruire la liste.
        Si l'ensemble des personnes a changé (ajout/suppression), bascule sur refresh_persons."""
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
        # takeItem() ci-dessous décale les index de ligne : un chargement d'icônes
        # encore en vol appliquerait ses icônes à la mauvaise personne (cf. _cancel_face_loader).
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
        """Retire une personne de la liste (nom effacé/supprimé). No icon reload."""
        # takeItem() ci-dessous décale les index de ligne : un chargement d'icônes
        # encore en vol appliquerait ses icônes à la mauvaise personne (cf. _cancel_face_loader).
        self._cancel_face_loader()
        self._persons = [p for p in self._persons if p.id != person_id]
        for i in range(self._persons_list.count()):
            item = self._persons_list.item(i)
            p = item.data(Qt.UserRole)
            if isinstance(p, PersonInfo) and p.id == person_id:
                self._persons_list.takeItem(i)
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

    def update_duplicates_badge(self, count: int) -> None:
        """Mettre à jour le badge du bouton Dupliquées avec le nombre de groupes détectés."""
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
        menu.addAction("Renommer…",
                       lambda: self.person_rename_requested.emit(person))
        menu.addAction("Fusionner avec…",
                       lambda: self.person_merge_requested.emit(person))
        menu.addSeparator()
        menu.addAction("Effacer le nom…",
                       lambda: self.person_clear_requested.emit(person))
        menu.exec(self._persons_list.mapToGlobal(pos))
