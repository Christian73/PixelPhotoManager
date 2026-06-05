import logging
import weakref
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QRunnable, QThreadPool, QObject, Slot, QSize, QPoint, QRect, QTimer, QUrl, QMimeData
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QDrag
from PySide6.QtWidgets import (
    QScrollArea, QWidget, QLabel, QVBoxLayout, QSizePolicy,
    QMenu, QApplication,
)

from src.ui.loading_label import LoadingLabel

from src.core.models import PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache

logger = logging.getLogger(__name__)


class _ThumbSignals(QObject):
    ready = Signal(str, QPixmap)  # photo_path, pixmap


class _ThumbWorker(QRunnable):
    def __init__(self, photo_path: str, cache: ThumbnailCache, signals: _ThumbSignals,
                 edit=None):
        super().__init__()
        self._path = photo_path
        self._cache = cache
        self._edit = edit
        self._signals_ref = weakref.ref(signals)
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            pixmap = self._cache.generate(self._path, self._edit)
            if pixmap:
                signals = self._signals_ref()
                if signals is not None:
                    signals.ready.emit(self._path, pixmap)
        except Exception:
            logger.debug(f"Erreur génération vignette {self._path}", exc_info=True)



class ThumbnailCell(QWidget):
    double_clicked = Signal(object)                # PhotoInfo
    right_clicked  = Signal(object, object)        # PhotoInfo, QPoint
    clicked        = Signal(object, Qt.KeyboardModifier)  # PhotoInfo, modifiers
    drag_started   = Signal(object)                # PhotoInfo

    def __init__(self, photo: PhotoInfo, cache: ThumbnailCache, size: int, parent=None):
        super().__init__(parent)
        self._photo = photo
        self._cache = cache
        self._size = size
        self._selected = False
        self._pixmap: QPixmap | None = None
        self._load_requested = False   # True dès que load() a été appelé
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_thumb_ready)
        self._drag_start_pos: QPoint | None = None
        self._setup_ui()
        # Pas d'appel à load() ici — ThumbnailGrid décide de la priorité

    def _setup_ui(self) -> None:
        self.setFixedSize(self._size + 8, self._size + 8)
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        self._img_label = LoadingLabel("#373737")
        self._img_label.setFixedSize(self._size, self._size)
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.start_loading()
        layout.addWidget(self._img_label)

    def load(self, priority: int = 0) -> None:
        """Démarre le chargement de la vignette. Sans effet si déjà demandé."""
        if self._load_requested:
            return
        self._load_requested = True
        pixmap = self._cache.get(self._photo.path)
        if pixmap:
            self._set_pixmap(pixmap)
        else:
            worker = _ThumbWorker(self._photo.path, self._cache, self._signals)
            QThreadPool.globalInstance().start(worker, priority)

    def reload_with_edit(self, edit) -> None:
        """Invalide le cache et régénère la vignette en appliquant les retouches."""
        self._cache.invalidate(self._photo.path)
        worker = _ThumbWorker(self._photo.path, self._cache, self._signals, edit)
        QThreadPool.globalInstance().start(worker)

    @Slot(str, QPixmap)
    def _on_thumb_ready(self, path: str, pixmap: QPixmap) -> None:
        if path == self._photo.path:
            self._set_pixmap(pixmap)

    def _set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        scaled = pixmap.scaled(
            self._size, self._size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._img_label.setPixmap(scaled)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setStyleSheet(
            "background: rgba(70,130,180,120); border-radius:4px;" if selected else ""
        )

    def set_size(self, size: int) -> None:
        self._size = size
        self.setFixedSize(size + 8, size + 8)
        self._img_label.setFixedSize(size, size)
        if self._pixmap:
            # Re-scale le pixmap déjà chargé — pas besoin de re-générer
            scaled = self._pixmap.scaled(size, size,
                                         Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
            self._img_label.setPixmap(scaled)
        else:
            self._img_label.start_loading()

    @property
    def photo(self) -> PhotoInfo:
        return self._photo

    # ------------------------------------------------------------------ events

    def mouseDoubleClickEvent(self, _event) -> None:
        self.double_clicked.emit(self._photo)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self.right_clicked.emit(self._photo, event.globalPosition().toPoint())
        elif event.button() == Qt.LeftButton:
            self._drag_start_pos = event.position().toPoint()
            self.clicked.emit(self._photo, QApplication.keyboardModifiers())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (self._drag_start_pos is not None
                and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - self._drag_start_pos).manhattanLength()
                    >= QApplication.startDragDistance()):
            self._drag_start_pos = None
            self.drag_started.emit(self._photo)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.right_clicked.emit(self._photo, event.globalPos())


class _GridContainer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cells: list[ThumbnailCell] = []
        self._cell_w = 188
        self._spacing = 6
        self._relayout_pending = False

    def set_cells(self, cells: list[ThumbnailCell]) -> None:
        for c in self._cells:
            c.setParent(None)
        self._cells = list(cells)
        for c in self._cells:
            c.setParent(self)
            c.show()
        self._relayout()

    def append_cell(self, cell: ThumbnailCell) -> None:
        self._cells.append(cell)
        cell.setParent(self)
        cell.show()
        self._relayout()

    def set_cell_width(self, w: int) -> None:
        self._cell_w = w
        self._relayout()

    def _relayout(self) -> None:
        container_w = self.parentWidget().width() if self.parentWidget() else self.width()
        container_w = container_w or 800

        cols = max(1, (container_w + self._spacing) // (self._cell_w + self._spacing))
        x, y = self._spacing, self._spacing
        col = 0
        max_y = self._spacing
        for cell in self._cells:
            cell.move(x, y)
            x += self._cell_w + self._spacing
            col += 1
            if col >= cols:
                col = 0
                x = self._spacing
                y += cell.height() + self._spacing
            max_y = max(max_y, y + cell.height())

        total_h = max_y + self._spacing if self._cells else self._spacing
        self.setMinimumHeight(total_h)
        # Éviter de déclencher resizeEvent en boucle
        if self.height() != total_h:
            self.setFixedHeight(total_h)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()


class ThumbnailGrid(QScrollArea):
    photo_activated   = Signal(object)  # PhotoInfo
    selection_changed = Signal(list)    # list[PhotoInfo]
    rename_requested  = Signal(object)  # PhotoInfo
    delete_requested  = Signal(list)    # list[PhotoInfo]
    save_requested    = Signal(object)  # PhotoInfo

    def __init__(self, cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self._cache = cache
        self._thumb_size = 180
        self._cells: list[ThumbnailCell] = []
        self._selected: set[str] = set()
        self.setFocusPolicy(Qt.StrongFocus)

        self._container = _GridContainer()
        self._container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setWidget(self._container)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Debounce scroll : re-priorise le chargement 50 ms après l'arrêt du défilement
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(50)
        self._scroll_timer.timeout.connect(self._schedule_loads)
        self.verticalScrollBar().valueChanged.connect(self._scroll_timer.start)

    def set_photos(self, photos: list[PhotoInfo]) -> None:
        self._selected.clear()
        cells = [self._make_cell(p) for p in photos]
        self._cells = cells
        self._container.set_cells(cells)
        # Différer d'un tick pour que _relayout() ait positionné les cellules
        QTimer.singleShot(0, self._schedule_loads)

    def add_photo(self, photo: PhotoInfo) -> None:
        cell = self._make_cell(photo)
        self._cells.append(cell)
        self._container.append_cell(cell)
        # Différer d'un tick pour que la cellule soit positionnée avant de vérifier la visibilité
        QTimer.singleShot(0, lambda c=cell: c.load(
            priority=10 if self._cell_is_visible(c) else 0
        ))

    def _cell_is_visible(self, cell: ThumbnailCell) -> bool:
        """Retourne True si la cellule est (au moins partiellement) dans le viewport."""
        try:
            top_left = cell.mapTo(self.viewport(), QPoint(0, 0))
            return self.viewport().rect().intersects(QRect(top_left, cell.size()))
        except RuntimeError:
            return False

    def _schedule_loads(self) -> None:
        """
        Parcourt toutes les cellules non encore chargées et les met en queue
        avec une priorité haute pour les cellules visibles, basse pour les autres.
        """
        for cell in self._cells:
            if cell._load_requested:
                continue
            priority = 10 if self._cell_is_visible(cell) else 0
            cell.load(priority)

    def _make_cell(self, photo: PhotoInfo) -> ThumbnailCell:
        cell = ThumbnailCell(photo, self._cache, self._thumb_size)
        cell.double_clicked.connect(self.photo_activated.emit)
        cell.right_clicked.connect(self._on_right_click)
        cell.clicked.connect(self._on_cell_clicked)
        cell.drag_started.connect(self._on_cell_drag_started)
        return cell

    @Slot(object)
    def _on_cell_drag_started(self, photo: PhotoInfo) -> None:
        """Lance un drag interne avec un type MIME applicatif (évite l'interception OS)."""
        if photo.path in self._selected:
            photos = [c.photo for c in self._cells if c.photo.path in self._selected]
        else:
            photos = [photo]

        # Type MIME interne : l'OS ne l'intercepte pas, pas de copie parasite
        paths_bytes = '\n'.join(p.path for p in photos).encode('utf-8')
        mime = QMimeData()
        mime.setData('application/x-pixelphoto-paths', paths_bytes)

        drag = QDrag(self)
        drag.setMimeData(mime)

        # Vignette de prévisualisation
        for cell in self._cells:
            if cell.photo.path == photos[0].path and cell._pixmap:
                px = cell._pixmap.scaled(72, 72, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
                drag.setPixmap(px)
                drag.setHotSpot(px.rect().center())
                break

        drag.exec(Qt.MoveAction)

    @Slot(object, object)
    def _on_cell_clicked(self, photo: PhotoInfo, modifiers) -> None:
        path = photo.path
        ctrl  = bool(modifiers & Qt.ControlModifier)
        shift = bool(modifiers & Qt.ShiftModifier)

        if ctrl:
            if path in self._selected:
                self._selected.discard(path)
                self._set_cell_selected(path, False)
            else:
                self._selected.add(path)
                self._set_cell_selected(path, True)
        elif shift:
            self._range_select(path)
        else:
            self._clear_selection()
            self._selected.add(path)
            self._set_cell_selected(path, True)

        self.selection_changed.emit(self.get_selected())

    def _set_cell_selected(self, path: str, selected: bool) -> None:
        for c in self._cells:
            if c.photo.path == path:
                c.set_selected(selected)
                return

    def _clear_selection(self) -> None:
        for c in self._cells:
            c.set_selected(False)
        self._selected.clear()

    def _range_select(self, target_path: str) -> None:
        if not self._selected:
            self._selected.add(target_path)
            self._set_cell_selected(target_path, True)
            return
        paths = [c.photo.path for c in self._cells]
        last = next((p for p in reversed(paths) if p in self._selected), None)
        if last is None:
            return
        lo = min(paths.index(last), paths.index(target_path))
        hi = max(paths.index(last), paths.index(target_path))
        for i, cell in enumerate(self._cells):
            if lo <= i <= hi:
                self._selected.add(cell.photo.path)
                cell.set_selected(True)

    @Slot(object, object)
    def _on_right_click(self, photo: PhotoInfo, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Ouvrir", lambda: self.photo_activated.emit(photo))
        menu.addSeparator()
        fav_label = "Retirer des favoris" if photo.is_favorite else "Marquer comme favori"
        menu.addAction(fav_label)
        menu.addAction("Informations EXIF")
        menu.addAction("Renommer l'image", lambda: self.rename_requested.emit(photo))
        menu.addAction("Enregistrer l'image traitée sur le disque",
                       lambda: self.save_requested.emit(photo))
        menu.addSeparator()
        menu.addAction("Révéler dans l'Explorateur",
                       lambda: __import__('os').startfile(
                           __import__('os.path').path.dirname(photo.path)))
        menu.addSeparator()
        menu.addAction("Effacer le fichier…",
                       lambda: self.delete_requested.emit([photo]))
        menu.exec(pos)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Delete:
            selected = self.get_selected()
            if selected:
                self.delete_requested.emit(selected)
        else:
            super().keyPressEvent(event)

    def remove_photos(self, paths: list[str]) -> None:
        """Retire les cellules correspondant aux chemins donnés de la grille."""
        paths_set = set(paths)
        remaining = [c for c in self._cells if c.photo.path not in paths_set]
        self._cells = remaining
        self._selected -= paths_set
        self._container.set_cells(remaining)
        self.selection_changed.emit(self.get_selected())

    def refresh_photo(self, photo_path: str, edit) -> None:
        """Régénère la vignette d'une photo après modification de ses retouches."""
        for cell in self._cells:
            if cell.photo.path == photo_path:
                cell.reload_with_edit(edit)
                return

    def set_thumbnail_size(self, size: int) -> None:
        self._thumb_size = size
        self._container.set_cell_width(size + 8)
        for cell in self._cells:
            cell.set_size(size)

    def get_selected(self) -> list[PhotoInfo]:
        return [c.photo for c in self._cells if c.photo.path in self._selected]

    def clear(self) -> None:
        self._selected.clear()
        self._cells.clear()
        self._container.set_cells([])

    def update_photo_path(self, old_path: str, new_path: str) -> None:
        """Met à jour le PhotoInfo d'une cellule après renommage."""
        new_p = Path(new_path)
        for cell in self._cells:
            if cell.photo.path == old_path:
                cell.photo.path = new_path
                cell.photo.filename = new_p.name
                cell.photo.directory = str(new_p.parent)
                break
        if old_path in self._selected:
            self._selected.discard(old_path)
            self._selected.add(new_path)

    def select_all(self) -> None:
        for cell in self._cells:
            self._selected.add(cell.photo.path)
            cell.set_selected(True)
        self.selection_changed.emit(self.get_selected())
