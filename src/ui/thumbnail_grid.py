import logging
import weakref
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QRunnable, QThreadPool, QObject, Slot, QPoint, QRect, QTimer, QUrl, QMimeData
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QDrag
from PySide6.QtWidgets import (
    QScrollArea, QWidget, QLabel, QVBoxLayout, QSizePolicy,
    QMenu, QApplication,
)

from src.ui.loading_label import LoadingLabel

from src.core.models import PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache

logger = logging.getLogger(__name__)

_BUFFER_ROWS = 3


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
        self._load_requested = False
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_thumb_ready)
        self._drag_start_pos: QPoint | None = None
        self._setup_ui()

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
        if self._photo.media_type == "video":
            scaled = self._add_video_badge(scaled)
        self._img_label.setPixmap(scaled)

    def _add_video_badge(self, pixmap: QPixmap) -> QPixmap:
        result = QPixmap(pixmap)
        p = QPainter(result)
        p.setRenderHint(QPainter.Antialiasing)
        r = 12
        cx = result.width()  - r - 4
        cy = result.height() - r - 4
        p.setBrush(QColor(0, 0, 0, 160))
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPixelSize(13)
        p.setFont(f)
        p.drawText(QRect(cx - r, cy - r, 2 * r, 2 * r), Qt.AlignCenter, "▶")
        p.end()
        return result

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
            scaled = self._pixmap.scaled(size, size,
                                         Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
            self._img_label.setPixmap(scaled)
        else:
            self._img_label.start_loading()

    @property
    def photo(self) -> PhotoInfo:
        return self._photo

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
    """
    Conteneur virtuel : calcule la hauteur totale sans créer de widgets.
    ThumbnailGrid place directement ses cellules via cell_rect(index).
    """

    layout_changed = Signal()   # emis quand le nombre de colonnes change

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total = 0
        self._cell_w = 188
        self._cell_h = 188
        self._spacing = 6
        self._cols = 1

    def configure(self, total: int, cell_w: int, cell_h: int) -> None:
        self._total = total
        self._cell_w = cell_w
        self._cell_h = cell_h
        self._recompute(check_cols=False)

    def set_total(self, total: int) -> None:
        self._total = total
        self._recompute(check_cols=False)

    def set_cell_width(self, w: int) -> None:
        self._cell_w = w
        self._recompute(check_cols=True)

    def _recompute(self, check_cols: bool = True) -> None:
        parent_w = (self.parentWidget().width()
                    if self.parentWidget() else self.width()) or 800
        old_cols = self._cols
        self._cols = max(1, (parent_w + self._spacing) // (self._cell_w + self._spacing))
        rows = max(1, (self._total + self._cols - 1) // self._cols) if self._total else 1
        h = rows * (self._cell_h + self._spacing) + self._spacing
        self.setFixedHeight(h)
        if check_cols and self._cols != old_cols:
            self.layout_changed.emit()

    def cell_rect(self, index: int) -> QRect:
        row = index // self._cols
        col = index % self._cols
        x = self._spacing + col * (self._cell_w + self._spacing)
        y = self._spacing + row * (self._cell_h + self._spacing)
        return QRect(x, y, self._cell_w, self._cell_h)

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def cell_h(self) -> int:
        return self._cell_h

    @property
    def spacing(self) -> int:
        return self._spacing

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._recompute(check_cols=True)


class ThumbnailGrid(QScrollArea):
    """
    Grille virtuelle : seules les cellules visibles (± _BUFFER_ROWS rangées)
    sont instanciées comme QWidget. Le reste n'existe qu'en tant que PhotoInfo.
    Supporte 100 000+ photos sans bloquer le thread UI.
    """

    photo_activated   = Signal(object)  # PhotoInfo
    selection_changed = Signal(list)    # list[PhotoInfo]
    rename_requested  = Signal(object)  # PhotoInfo
    delete_requested  = Signal(list)    # list[PhotoInfo]
    save_requested    = Signal(object)  # PhotoInfo

    def __init__(self, cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self._cache = cache
        self._thumb_size = 180
        self._photos: list[PhotoInfo] = []           # toutes les photos (pas de widgets)
        self._selected: set[str] = set()
        self._materialized: dict[int, ThumbnailCell] = {}  # index → widget visible

        self.setFocusPolicy(Qt.StrongFocus)

        self._container = _GridContainer()
        self._container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._container.layout_changed.connect(self._on_layout_changed)
        self.setWidget(self._container)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Debounce scroll : rematerialise 50 ms après l'arrêt du défilement
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(50)
        self._update_timer.timeout.connect(self._update_materialized)
        self.verticalScrollBar().valueChanged.connect(self._update_timer.start)

    # ------------------------------------------------------------------ données

    def set_photos(self, photos: list[PhotoInfo]) -> None:
        self._selected.clear()
        self._dematerialize_all()
        self._photos = list(photos)
        self._container.configure(
            len(photos), self._thumb_size + 8, self._thumb_size + 8
        )
        QTimer.singleShot(0, self._update_materialized)

    def add_photo(self, photo: PhotoInfo) -> None:
        self._photos.append(photo)
        self._container.set_total(len(self._photos))
        QTimer.singleShot(0, self._update_materialized)

    def add_photos_batch(self, photos: list[PhotoInfo]) -> None:
        if not photos:
            return
        self._photos.extend(photos)
        self._container.set_total(len(self._photos))
        # Idempotent : appels rapides successifs → une seule matérialisation
        QTimer.singleShot(0, self._update_materialized)

    def remove_photos(self, paths: list[str]) -> None:
        paths_set = set(paths)
        self._dematerialize_all()
        self._photos = [p for p in self._photos if p.path not in paths_set]
        self._selected -= paths_set
        self._container.set_total(len(self._photos))
        QTimer.singleShot(0, self._update_materialized)
        self.selection_changed.emit(self.get_selected())

    def refresh_photo(self, photo_path: str, edit) -> None:
        for cell in self._materialized.values():
            if cell.photo.path == photo_path:
                cell.reload_with_edit(edit)
                return

    def clear(self) -> None:
        self._selected.clear()
        self._dematerialize_all()
        self._photos.clear()
        self._container.set_total(0)

    def update_photo_path(self, old_path: str, new_path: str) -> None:
        new_p = Path(new_path)
        for photo in self._photos:
            if photo.path == old_path:
                photo.path = new_path
                photo.filename = new_p.name
                photo.directory = str(new_p.parent)
                break
        if old_path in self._selected:
            self._selected.discard(old_path)
            self._selected.add(new_path)

    # ------------------------------------------------------------------ sélection

    def get_selected(self) -> list[PhotoInfo]:
        return [p for p in self._photos if p.path in self._selected]

    def select_all(self) -> None:
        self._selected = {p.path for p in self._photos}
        for cell in self._materialized.values():
            cell.set_selected(True)
        self.selection_changed.emit(self.get_selected())

    def set_thumbnail_size(self, size: int) -> None:
        self._thumb_size = size
        self._dematerialize_all()
        self._container.configure(len(self._photos), size + 8, size + 8)
        QTimer.singleShot(0, self._update_materialized)

    # ------------------------------------------------------------------ virtualisation

    def _visible_range(self) -> tuple[int, int]:
        scroll_y = self.verticalScrollBar().value()
        vp_h = max(1, self.viewport().height())
        spacing = self._container.spacing
        cols = self._container.cols
        row_h = self._thumb_size + 8 + spacing

        first_row = max(0, (scroll_y - spacing) // row_h - _BUFFER_ROWS)
        last_row = (scroll_y + vp_h - spacing) // row_h + _BUFFER_ROWS

        first_idx = first_row * cols
        last_idx = min(len(self._photos) - 1, (last_row + 1) * cols - 1)
        return first_idx, last_idx

    def _update_materialized(self) -> None:
        if not self._photos:
            return
        first_idx, last_idx = self._visible_range()
        needed = set(range(first_idx, last_idx + 1))

        # Dématérialise les cellules hors fenêtre
        for i in list(self._materialized.keys()):
            if i not in needed:
                self._materialized.pop(i).setParent(None)

        # Matérialise les cellules visibles manquantes
        for i in needed:
            if i >= len(self._photos):
                break
            if i not in self._materialized:
                cell = self._make_cell(self._photos[i])
                rect = self._container.cell_rect(i)
                cell.setParent(self._container)
                cell.setGeometry(rect)
                if self._photos[i].path in self._selected:
                    cell.set_selected(True)
                cell.show()
                cell.load(priority=10)
                self._materialized[i] = cell

    def _dematerialize_all(self) -> None:
        for cell in self._materialized.values():
            cell.setParent(None)
        self._materialized.clear()

    def _on_layout_changed(self) -> None:
        """Le nombre de colonnes a changé (redimensionnement) → repositionne tout."""
        self._dematerialize_all()
        QTimer.singleShot(0, self._update_materialized)

    # ------------------------------------------------------------------ fabrique + événements

    def _make_cell(self, photo: PhotoInfo) -> ThumbnailCell:
        cell = ThumbnailCell(photo, self._cache, self._thumb_size)
        cell.double_clicked.connect(self.photo_activated.emit)
        cell.right_clicked.connect(self._on_right_click)
        cell.clicked.connect(self._on_cell_clicked)
        cell.drag_started.connect(self._on_cell_drag_started)
        return cell

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
        for cell in self._materialized.values():
            if cell.photo.path == path:
                cell.set_selected(selected)
                return

    def _clear_selection(self) -> None:
        for cell in self._materialized.values():
            cell.set_selected(False)
        self._selected.clear()

    def _range_select(self, target_path: str) -> None:
        if not self._selected:
            self._selected.add(target_path)
            self._set_cell_selected(target_path, True)
            return
        paths = [p.path for p in self._photos]
        last = next((p for p in reversed(paths) if p in self._selected), None)
        if last is None:
            return
        lo = min(paths.index(last), paths.index(target_path))
        hi = max(paths.index(last), paths.index(target_path))
        for i in range(lo, hi + 1):
            self._selected.add(paths[i])
        for cell in self._materialized.values():
            cell.set_selected(cell.photo.path in self._selected)

    @Slot(object)
    def _on_cell_drag_started(self, photo: PhotoInfo) -> None:
        if photo.path in self._selected:
            photos = [p for p in self._photos if p.path in self._selected]
        else:
            photos = [photo]

        paths_bytes = '\n'.join(p.path for p in photos).encode('utf-8')
        mime = QMimeData()
        mime.setData('application/x-pixelphoto-paths', paths_bytes)

        drag = QDrag(self)
        drag.setMimeData(mime)

        for cell in self._materialized.values():
            if cell.photo.path == photos[0].path and cell._pixmap:
                px = cell._pixmap.scaled(72, 72, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
                drag.setPixmap(px)
                drag.setHotSpot(px.rect().center())
                break

        drag.exec(Qt.MoveAction)

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
