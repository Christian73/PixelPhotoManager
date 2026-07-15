# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import weakref
from datetime import datetime as _dt
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QRunnable, QThreadPool, QObject, Slot, QPoint, QRect, QTimer, QMimeData, QByteArray
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QDrag
from PySide6.QtWidgets import (
    QScrollArea, QScrollBar, QWidget, QLabel, QVBoxLayout, QSizePolicy,
    QMenu, QApplication,
)

from src.ui.loading_label import LoadingLabel
from src.core.models import PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache

logger = logging.getLogger(__name__)

_img_thumb_pool: "QThreadPool | None" = None

def _get_thumb_pool() -> QThreadPool:
    """Pool dédié aux vignettes image, limité à 4 threads.
    Évite la saturation RAM + I/O disque lors du décodage JPEG simultané."""
    global _img_thumb_pool
    if _img_thumb_pool is None:
        _img_thumb_pool = QThreadPool()
        _img_thumb_pool.setMaxThreadCount(4)
    return _img_thumb_pool


_video_thumb_pool: "QThreadPool | None" = None

def _get_video_thumb_pool() -> QThreadPool:
    """Pool dédié aux vignettes vidéo, limité à 2 threads.
    Évite la saturation I/O disque quand de nombreux cv2.VideoCapture s'ouvrent
    simultanément — ce qui ralentit aussi le thread UI par contention disque."""
    global _video_thumb_pool
    if _video_thumb_pool is None:
        _video_thumb_pool = QThreadPool()
        _video_thumb_pool.setMaxThreadCount(2)
    return _video_thumb_pool

_FR_MONTHS = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]

# Nombre de colonnes cibles par rangée (extrême, intermédiaire, centre, …).
# Les facteurs de taille sont l'inverse : moins de colonnes = cellules plus grandes.
# Intermédiaire = moyenne géométrique de extrême et centre → sqrt(37×4) ≈ 12.
_RIBBON_COLS_TARGET = (37, 12, 4, 12, 37)  # cibles indicatives (dépendent de la largeur)

# Facteurs de taille : proportionnels à 1/n_cols, normalisés pour que centre = 2.0
# 4/37 ≈ 0.108, ×2 → 0.216  |  4/12 ≈ 0.333, ×2 → 0.667  |  4/4 = 1.0, ×2 → 2.0
_RIBBON_FACTORS = (0.216, 0.667, 2.0, 0.667, 0.216)
_RIBBON_CENTER  = 2   # index de la rangée centrale
_RIBBON_SPACING = 6   # px entre les rangées

# Inertie du ruban (molette)
# Design : 1 pas immédiat par clic + impulsion de coasting accumulable.
# Clic lent  → 1 pas + ~2 pas de glissement  ≈ 3 total.
# Scroll rapide (10 clics) → 10 pas + ~17 de glissement ≈ 27 total.
_INERTIA_IMPULSE  = 0.20  # impulsion de coasting ajoutée par clic (en plus du pas immédiat)
_INERTIA_MAX_VEL  = 12.0  # vitesse maximale de coasting
_INERTIA_FRICTION = 0.85  # freinage par frame (≈16 ms)
_INERTIA_STOP     = 0.08  # seuil d'arrêt


class _ThumbSignals(QObject):
    # Utilise 'object' (PyObject) pour garantir le marshaling cross-thread en PySide6.
    ready = Signal(str, object)  # photo_path, jpeg_bytes (Python bytes)


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
            # Vérifier DB avant de relancer PIL — évite le décodage JPEG si déjà en cache
            if self._edit is None:
                data = self._cache.get_bytes(self._path)
            else:
                data = None
            if data is None:
                data = self._cache.generate(self._path, self._edit)
            if data:
                signals = self._signals_ref()
                if signals is not None:
                    signals.ready.emit(self._path, data)
        except Exception:
            logger.debug(f"Erreur génération vignette {self._path}", exc_info=True)


_DUP_BADGE_W = 22
_DUP_BADGE_H = 16


class ThumbnailCell(QWidget):
    double_clicked   = Signal(object)
    right_clicked    = Signal(object, object)
    clicked          = Signal(object, Qt.KeyboardModifier)
    drag_started     = Signal(object)
    duplicate_clicked = Signal(object)  # PhotoInfo — clic sur le badge de doublon

    def __init__(self, photo: PhotoInfo, cache: ThumbnailCache, size: int, parent=None):
        super().__init__(parent)
        self._photo = photo
        self._cache = cache
        self._size  = size
        self._selected = False
        self._pixmap: QPixmap | None = None
        self._load_requested = False
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_thumb_ready)
        self._drag_start_pos: QPoint | None = None
        self._dup_badge_rect = None   # QRect dans les coordonnées du widget
        self._worker: "_ThumbWorker | None" = None
        self._worker_pool: "QThreadPool | None" = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setFixedSize(self._size + 8, self._size + 8)
        self.setCursor(Qt.PointingHandCursor)
        # Nom accessible (UIA/QAccessible) : permet aux tests bout-en-bout
        # (tests/e2e, pywinauto) de cibler une vignette précise par chemin de
        # photo plutôt que par coordonnées écran devinées.
        self.setAccessibleName(f"thumb::{self._photo.path}")
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
        # Vérifier uniquement le cache RAM dans le thread UI (non bloquant).
        # La vérification DB et la génération PIL sont déléguées au worker.
        pixmap = self._cache.get_ram(self._photo.path)
        if pixmap:
            self._set_pixmap(pixmap)
        else:
            worker = _ThumbWorker(self._photo.path, self._cache, self._signals)
            from pathlib import Path as _P
            from src.library.exif_reader import VIDEO_EXT as _VE
            pool = (_get_video_thumb_pool()
                    if _P(self._photo.path).suffix.lower() in _VE
                    else _get_thumb_pool())
            self._worker = worker
            self._worker_pool = pool
            pool.start(worker, priority)

    def cancel_pending_load(self) -> None:
        """Retire le worker de la file du pool s'il n'a pas encore démarré.
        À appeler quand la cellule sort du champ (scroll) avant sa suppression :
        sans ça, le worker décode quand même la vignette pour une photo devenue
        invisible, au détriment des photos réellement affichées (cf. demande
        utilisateur : les requêtes d'affichage les plus anciennes doivent être
        abandonnées quand elles s'accumulent)."""
        if self._worker is not None and self._worker_pool is not None:
            try:
                self._worker_pool.tryTake(self._worker)
            except RuntimeError:
                # Le worker a AutoDelete=True : s'il a déjà démarré/terminé, Qt a
                # détruit l'objet C++ sous-jacent avant qu'on ait pu le retirer.
                pass
        self._worker = None
        self._worker_pool = None

    def reload_with_edit(self, edit) -> None:
        self._cache.invalidate(self._photo.path)
        worker = _ThumbWorker(self._photo.path, self._cache, self._signals, edit)
        _get_thumb_pool().start(worker)

    @Slot(str, object)
    def _on_thumb_ready(self, path: str, data: object) -> None:
        if path == self._photo.path:
            pixmap = QPixmap()
            pixmap.loadFromData(QByteArray(data))
            if not pixmap.isNull():
                self._cache.store_pixmap(path, pixmap)
                self._set_pixmap(pixmap)
            else:
                logger.warning("Pixmap null pour %s", path)

    def _set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        scaled = pixmap.scaled(self._size, self._size,
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if self._photo.media_type == "video":
            scaled = self._add_video_badge(scaled)
        if self._photo.duplicate_group_id is not None:
            # Calculer la position du badge dans les coordonnées du widget
            # (_img_label débute à (4,4), pixmap centré dans le label)
            pw = scaled.width()
            ph = scaled.height()
            lbl_ox = (self._size - pw) // 2
            lbl_oy = (self._size - ph) // 2
            bx = 4 + lbl_ox + pw - _DUP_BADGE_W - 2
            by = 4 + lbl_oy + 2
            self._dup_badge_rect = QRect(bx - 4, by - 4,
                                         _DUP_BADGE_W + 8, _DUP_BADGE_H + 8)
            scaled = self._add_duplicate_badge(scaled)
        else:
            self._dup_badge_rect = None
        self._img_label.setPixmap(scaled)

    def _add_duplicate_badge(self, pixmap: QPixmap) -> QPixmap:
        result = QPixmap(pixmap)
        p = QPainter(result)
        p.setRenderHint(QPainter.Antialiasing)
        x = result.width() - _DUP_BADGE_W - 2
        y = 2
        p.setBrush(QColor(255, 140, 0))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(x, y, _DUP_BADGE_W, _DUP_BADGE_H, 4, 4)
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(x, y, _DUP_BADGE_W, _DUP_BADGE_H), Qt.AlignCenter, "⧉")
        p.end()
        return result

    def _add_video_badge(self, pixmap: QPixmap) -> QPixmap:
        result = QPixmap(pixmap)
        p = QPainter(result)
        p.setRenderHint(QPainter.Antialiasing)
        r = max(18, min(result.width(), result.height()) // 4)
        cx, cy = result.width() // 2, result.height() // 2
        p.setBrush(QColor(0, 0, 0, 150))
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setPixelSize(max(14, r))
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
        self._load_requested = False          # permet un rechargement à la nouvelle taille
        self.setFixedSize(size + 8, size + 8)
        self._img_label.setFixedSize(size, size)
        if self._pixmap:
            scaled = self._pixmap.scaled(size, size,
                                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
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
            pos = event.position().toPoint()
            if (self._dup_badge_rect is not None
                    and self._dup_badge_rect.contains(pos)):
                self.duplicate_clicked.emit(self._photo)
                return
            self._drag_start_pos = pos
            self.clicked.emit(self._photo, QApplication.keyboardModifiers())
        super().mousePressEvent(event)

    def set_duplicate_group(self, group_id) -> None:
        self._photo.duplicate_group_id = group_id
        if self._pixmap is not None:
            self._set_pixmap(self._pixmap)

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
    Conteneur virtuel pour la grille uniforme (mode scroll).
    En mode ruban la hauteur est gérée par le QScrollArea parent.
    """
    layout_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total   = 0
        self._cell_w  = 188
        self._cell_h  = 188
        self._spacing = 6
        self._cols    = 1
        self._managed_height = True   # False en mode ruban (taille gérée par Qt)

    def configure(self, total: int, cell_w: int, cell_h: int) -> None:
        self._total  = total
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
        if not self._managed_height:
            return
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
        if not self._managed_height:
            # Mode ruban : le conteneur remplit le viewport, on signale le changement
            self.layout_changed.emit()
        else:
            self._recompute(check_cols=True)


class ThumbnailGrid(QScrollArea):
    """
    Grille virtuelle de vignettes.

    Mode scroll (par défaut)
    ───────────────────────
    Grille uniforme défilante. Au premier affichage, seule la partie visible est
    instanciée (départ rapide). Dès le premier déplacement dans la grille, une
    marge d'un écran plein est maintenue au-dessus et en dessous de la zone
    visible (cf. _visible_range/_buffer_active) pour anticiper la suite du
    défilement. Supporte 100 000+ photos.

    Mode ruban (set_ribbon_mode(True))
    ───────────────────────────────────
    5 rangées de tailles lenticulaires (extrême·intermédiaire·CENTRE·intermédiaire·extrême)
    occupent exactement la zone d'affichage. La grille est fixe ; les photos défilent
    en ruban via la molette (↑ = décalage droite d'1 case, ↓ = décalage gauche d'1 case).
    La fin d'une rangée enchaîne au début de la rangée suivante.
    """

    photo_activated           = Signal(object)
    selection_changed         = Signal(list)
    rename_requested          = Signal(object)
    move_requested            = Signal(object)
    delete_requested          = Signal(list)
    save_requested            = Signal(object)
    duplicate_clicked         = Signal(object)   # PhotoInfo — badge de doublon cliqué
    add_to_album_requested    = Signal(list)      # list[PhotoInfo] — ajouter à album existant
    create_album_with_requested = Signal(list)    # list[PhotoInfo] — créer nouvel album
    retry_face_index_requested = Signal(object)   # PhotoInfo — retenter l'identification des visages

    def __init__(self, cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self._cache       = cache
        self._thumb_size  = 180
        self._photos: list[PhotoInfo] = []
        self._selected: set[str] = set()
        self._materialized: dict[int, ThumbnailCell] = {}
        # False tant qu'aucun scroll n'a eu lieu depuis le dernier affichage : seule
        # la partie visible est alors préparée. Passe à True au 1er _on_scroll(),
        # ce qui active la marge d'un écran au-dessus/en dessous (cf. _visible_range).
        self._buffer_active = False
        # Chemins (normpath) des photos en erreur d'indexation faciale (timeout/crash,
        # non exclues) — pilote l'affichage de "Retenter l'identification des visages"
        # dans le menu contextuel. Mis à jour par MainWindow.set_index_error_paths().
        self._index_error_paths: set[str] = set()

        # ── Mode ruban ──
        self._ribbon_mode   = False
        self._ribbon_offset = 0
        # Métriques calculées depuis la taille du conteneur
        self._r_thumb: list[int] = []    # taille thumb par rangée
        self._r_widget: list[int] = []   # taille widget (thumb+8) par rangée
        self._r_cols: list[int]   = []   # nb colonnes par rangée
        self._r_row_y: list[int]  = []   # Y de chaque rangée
        self._r_total: int        = 0    # total photos visibles

        # Inertie (molette) : vitesse fractionnaire + accumulation de reste
        self._inertia_vel  = 0.0
        self._inertia_frac = 0.0
        self._inertia_timer = QTimer(self)
        self._inertia_timer.setInterval(16)   # ~60 fps
        self._inertia_timer.timeout.connect(self._inertia_tick)

        self.setFocusPolicy(Qt.StrongFocus)

        self._container = _GridContainer()
        self._container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._container.layout_changed.connect(self._on_layout_changed)
        self.setWidget(self._container)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Timers pour le mode scroll uniquement
        self._placeholder_timer = QTimer(self)
        self._placeholder_timer.setSingleShot(True)
        self._placeholder_timer.setInterval(0)
        self._placeholder_timer.timeout.connect(self._update_placeholders)

        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(50)
        self._load_timer.timeout.connect(self._start_loading)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # Indicateur de date (vue chronologique)
        self._date_overlay_enabled = False
        self._date_label = QLabel("", self.viewport())
        self._date_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._date_label.setStyleSheet(
            "QLabel {"
            "  color: white;"
            "  font-size: 26px;"
            "  font-weight: bold;"
            "  background-color: rgba(0,0,0,170);"
            "  border-radius: 8px;"
            "  padding: 5px 18px;"
            "}"
        )
        self._date_label.hide()

        # Ascenseur de navigation rapide (mode ruban uniquement)
        # La barre est fournie de l'extérieur via bind_ribbon_nav_bar()
        # et placée dans le layout parent — pas en overlay sur le viewport.
        self._nav_bar: QScrollBar | None = None
        self._nav_bar_updating = False
        self._nav_scrolling = False   # True pendant le scroll rapide via l'ascenseur
        self._nav_settle_timer = QTimer(self)
        self._nav_settle_timer.setSingleShot(True)
        self._nav_settle_timer.setInterval(500)
        self._nav_settle_timer.timeout.connect(self._on_nav_settled)

    # ══════════════════════════════════════════════════════════════════ données

    def set_photos(self, photos: list[PhotoInfo]) -> None:
        self._selected.clear()
        self._cancel_pending_workers()
        self._dematerialize_all()
        self._photos = list(photos)
        if self._ribbon_mode:
            self._buffer_active = False
            self._ribbon_offset = 0
            QTimer.singleShot(0, self._ribbon_full_update)
        else:
            # configure() peut réduire la hauteur du conteneur et forcer un
            # clampage de la scrollbar (ancienne position invalide dans la
            # nouvelle liste, plus courte) : cela émettrait valueChanged →
            # _on_scroll() → réactiverait la marge tampon avant même le 1er
            # affichage. On bloque le signal le temps du redimensionnement
            # pour garantir un rendu "visible uniquement" au premier affichage.
            vbar = self.verticalScrollBar()
            blocked = vbar.blockSignals(True)
            self._container.configure(len(photos), self._thumb_size + 8, self._thumb_size + 8)
            vbar.blockSignals(blocked)
            self._buffer_active = False
            QTimer.singleShot(0, self._update_materialized)

    def add_photo(self, photo: PhotoInfo) -> None:
        self._photos.append(photo)
        if self._ribbon_mode:
            QTimer.singleShot(0, self._update_ribbon_cells)
        else:
            self._container.set_total(len(self._photos))
            QTimer.singleShot(0, self._update_materialized)

    def add_photos_batch(self, photos: list[PhotoInfo]) -> None:
        if not photos:
            return
        self._photos.extend(photos)
        if self._ribbon_mode:
            QTimer.singleShot(0, self._update_ribbon_cells)
        else:
            self._container.set_total(len(self._photos))
            QTimer.singleShot(0, self._update_materialized)

    def remove_photos(self, paths: list[str]) -> None:
        paths_set = set(paths)
        self._dematerialize_all()
        self._photos = [p for p in self._photos if p.path not in paths_set]
        self._selected -= paths_set
        if self._ribbon_mode:
            self._clamp_ribbon_offset()
            QTimer.singleShot(0, self._update_ribbon_cells)
        else:
            self._container.set_total(len(self._photos))
            QTimer.singleShot(0, self._update_materialized)
        self.selection_changed.emit(self.get_selected())

    def scroll_to_photo(self, path: str) -> None:
        """Centre le ruban sur la photo path. Sans effet si pas en mode ruban."""
        if not self._ribbon_mode or not self._r_cols:
            return
        idx = next((i for i, p in enumerate(self._photos) if p.path == path), None)
        if idx is None:
            return
        self._ribbon_offset = idx - self._center_pos()
        self._clamp_ribbon_offset()
        self._update_ribbon_cells()
        self._update_date_overlay()

    def refresh_photo(self, photo_path: str, edit) -> None:
        for cell in self._materialized.values():
            if cell.photo.path == photo_path:
                cell.reload_with_edit(edit)
                return

    def clear(self) -> None:
        self._selected.clear()
        self._cancel_pending_workers()
        self._dematerialize_all()
        self._buffer_active = False
        self._photos.clear()
        if not self._ribbon_mode:
            self._container.set_total(0)

    def update_photo_path(self, old_path: str, new_path: str) -> None:
        new_p = Path(new_path)
        for photo in self._photos:
            if photo.path == old_path:
                photo.path     = new_path
                photo.filename = new_p.name
                photo.directory = str(new_p.parent)
                break
        if old_path in self._selected:
            self._selected.discard(old_path)
            self._selected.add(new_path)

    # ══════════════════════════════════════════════════════════════════ sélection

    def get_selected(self) -> list[PhotoInfo]:
        return [p for p in self._photos if p.path in self._selected]

    def select_all(self) -> None:
        self._selected = {p.path for p in self._photos}
        for cell in self._materialized.values():
            cell.set_selected(True)
        self.selection_changed.emit(self.get_selected())

    def select_photo(self, path: str) -> None:
        """Sélectionne une seule photo par chemin et émet selection_changed."""
        if not any(p.path == path for p in self._photos):
            return
        self._clear_selection()
        self._selected.add(path)
        self._set_cell_selected(path, True)
        self.selection_changed.emit(self.get_selected())

    def set_thumbnail_size(self, size: int) -> None:
        self._thumb_size = size
        if self._ribbon_mode:
            return          # taille déterminée par viewport, pas par ce réglage
        self._cancel_pending_workers()
        self._dematerialize_all()
        vbar = self.verticalScrollBar()
        blocked = vbar.blockSignals(True)
        self._container.configure(len(self._photos), size + 8, size + 8)
        vbar.blockSignals(blocked)
        self._buffer_active = False
        QTimer.singleShot(0, self._update_materialized)

    def bind_ribbon_nav_bar(self, bar: QScrollBar) -> None:
        """Associe la QScrollBar externe qui pilote la navigation du ruban.

        Doit être appelé une seule fois depuis main_window, avant toute navigation.
        """
        if self._nav_bar is not None:
            self._nav_bar.valueChanged.disconnect(self._on_nav_bar_scroll)
        self._nav_bar = bar
        self._nav_bar.setSingleStep(1)
        self._nav_bar.hide()
        self._nav_bar.valueChanged.connect(self._on_nav_bar_scroll)

    # ══════════════════════════════════════════════════════════════════ mode ruban

    def set_ribbon_mode(self, enabled: bool) -> None:
        if self._ribbon_mode == enabled:
            return
        self._ribbon_mode = enabled
        self._cancel_pending_workers()
        self._dematerialize_all()
        self._ribbon_offset = 0

        if enabled:
            self._container._managed_height = False
            # Effacer les contraintes setFixedHeight héritées du mode scroll
            # pour que setWidgetResizable puisse redimensionner librement le conteneur.
            self._container.setMinimumHeight(0)
            self._container.setMaximumHeight(16777215)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            QTimer.singleShot(0, self._ribbon_full_update)
        else:
            self._inertia_timer.stop()
            self._inertia_vel  = 0.0
            self._inertia_frac = 0.0
            if self._nav_bar is not None:
                self._nav_bar.hide()
            self._container._managed_height = True
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self._container.configure(
                len(self._photos), self._thumb_size + 8, self._thumb_size + 8
            )
            QTimer.singleShot(0, self._update_materialized)

    def _clamp_ribbon_offset(self) -> None:
        """Ajuste ribbon_offset pour que la 1re/dernière photo puisse être centrée.

        Plage autorisée :
          min = -(position de la photo centrale dans le ruban)
               → première photo au centre de la rangée centrale
          max = (N-1) - center_pos
               → dernière photo au centre de la rangée centrale
        Les slots hors [0, N-1] restent simplement vides.
        """
        if not self._r_cols or not self._photos:
            self._ribbon_offset = 0
            return
        center_pos = sum(self._r_cols[:_RIBBON_CENTER]) + self._r_cols[_RIBBON_CENTER] // 2
        lo = -center_pos
        hi = max(lo, len(self._photos) - 1 - center_pos)
        self._ribbon_offset = max(lo, min(self._ribbon_offset, hi))

    # ── Ascenseur de navigation ────────────────────────────────────────────────

    def _center_pos(self) -> int:
        """Index de la cellule centrale dans le ruban (rangée centrale, colonne médiane)."""
        if not self._r_cols:
            return 0
        return sum(self._r_cols[:_RIBBON_CENTER]) + self._r_cols[_RIBBON_CENTER] // 2

    def center_photo_index(self) -> int | None:
        """Retourne l'index de la photo au centre du ruban, ou None hors mode ruban."""
        if not self._ribbon_mode or not self._photos:
            return None
        idx = self._ribbon_offset + self._center_pos()
        return idx if 0 <= idx < len(self._photos) else None

    def _update_nav_bar(self) -> None:
        """Affiche/cache et synchronise l'ascenseur externe (appelé après resize)."""
        if self._nav_bar is None:
            return
        if not self._ribbon_mode or not self._photos or not self._r_cols:
            self._nav_bar.hide()
            return
        self._nav_bar.show()
        self._sync_nav_bar()

    def _sync_nav_bar(self) -> None:
        """Met à jour plage et valeur de l'ascenseur sans le repositionner."""
        if self._nav_bar is None or not self._ribbon_mode or not self._r_cols or not self._photos:
            return
        center      = self._ribbon_offset + self._center_pos()
        max_val     = max(0, len(self._photos) - 1)
        page_step   = max(1, self._r_total)
        self._nav_bar_updating = True
        self._nav_bar.setRange(0, max_val)
        self._nav_bar.setPageStep(page_step)
        self._nav_bar.setValue(max(0, min(center, max_val)))
        self._nav_bar_updating = False

    @Slot(int)
    def _on_nav_bar_scroll(self, value: int) -> None:
        if self._nav_bar_updating or not self._r_cols:
            return
        self._ribbon_offset = value - self._center_pos()
        self._clamp_ribbon_offset()
        self._nav_bar_updating = True
        # Pendant le scroll rapide : vider la grille une seule fois et ne mettre
        # à jour que la date. Les cellules seront recréées après 500 ms d'inactivité.
        if not self._nav_scrolling:
            self._nav_scrolling = True
            self._dematerialize_all()
        self._update_date_overlay()
        self._nav_bar_updating = False
        self._sync_nav_bar()
        self._nav_settle_timer.start()   # relance le délai à chaque mouvement

    def _on_nav_settled(self) -> None:
        """Déclenché 500 ms après le dernier mouvement de l'ascenseur de navigation."""
        self._nav_scrolling = False
        self._update_ribbon_cells()

    # ──────────────────────────────────────────────────────────────────────────

    def _ribbon_full_update(self) -> None:
        """Recalcule les métriques puis replace les cellules."""
        self._recompute_ribbon_layout()
        self._update_ribbon_cells()
        self._update_nav_bar()
        self._update_date_overlay()

    def _recompute_ribbon_layout(self) -> None:
        """Calcule les tailles et positions de rangées pour remplir exactement le viewport.

        Utilise viewport().height()/width() et non container.height()/width() :
        le conteneur peut garder une ancienne contrainte setFixedHeight héritée
        du mode scroll, ce qui fausserait totalement le calcul de base_size.
        """
        vp_h = max(100, self.viewport().height())
        vp_w = max(100, self.viewport().width())
        n    = len(_RIBBON_FACTORS)
        s    = _RIBBON_SPACING

        # base_size : résout sum(f*base + 8) + (n-1)*s = vp_h
        # → base * sum_f = vp_h - n*8 - (n-1)*s
        sum_f = sum(_RIBBON_FACTORS)
        base  = max(20.0, (vp_h - n * 8 - (n - 1) * s) / sum_f)

        self._r_thumb  = [max(20, int(f * base)) for f in _RIBBON_FACTORS]
        self._r_widget = [ts + 8 for ts in self._r_thumb]
        self._r_cols   = [
            max(1, (vp_w + s) // (ws + s))
            for ws in self._r_widget
        ]

        # Positions Y : rangées empilées sans espacement initial, espacement entre elles
        self._r_row_y = []
        y = 0
        for i, ws in enumerate(self._r_widget):
            self._r_row_y.append(y)
            if i < n - 1:
                y += ws + s

        self._r_total = sum(self._r_cols)

    def _update_ribbon_cells(self) -> None:
        """Place les cellules selon ribbon_offset sans recalculer les métriques."""
        if not self._ribbon_mode or not self._r_thumb:
            return
        if not self._photos:
            self._dematerialize_all()
            return

        self._clamp_ribbon_offset()

        # Construire la cible : {photo_idx: (QRect, thumb_size)}
        vp_w = self.viewport().width()
        target: dict[int, tuple[QRect, int]] = {}
        rb_pos = 0
        for row_idx in range(len(_RIBBON_FACTORS)):
            n_cols = self._r_cols[row_idx]
            ws     = self._r_widget[row_idx]
            ts     = self._r_thumb[row_idx]
            y      = self._r_row_y[row_idx]
            # Centrage horizontal de la rangée centrale uniquement
            if row_idx == _RIBBON_CENTER:
                row_w   = n_cols * ws + max(0, n_cols - 1) * _RIBBON_SPACING
                x_start = max(0, (vp_w - row_w) // 2)
            else:
                x_start = 0
            for col in range(n_cols):
                photo_idx = self._ribbon_offset + rb_pos
                if 0 <= photo_idx < len(self._photos):
                    x = x_start + col * (ws + _RIBBON_SPACING)
                    target[photo_idx] = (QRect(x, y, ws, ws), ts)
                rb_pos += 1

        # Supprimer les cellules obsolètes
        for idx in list(self._materialized.keys()):
            if idx not in target:
                cell = self._materialized.pop(idx)
                cell.cancel_pending_load()
                cell.setParent(None)

        # Mettre à jour ou créer les cellules
        for photo_idx, (rect, ts) in target.items():
            photo = self._photos[photo_idx]
            if photo_idx in self._materialized:
                cell = self._materialized[photo_idx]
                if cell._size != ts:
                    cell.set_size(ts)
                    cell.load(priority=10)
                cell.setGeometry(rect)
            else:
                cell = self._make_cell(photo, ts)
                cell.setParent(self._container)
                cell.setGeometry(rect)
                if photo.path in self._selected:
                    cell.set_selected(True)
                cell.show()
                cell.load(priority=10)
                self._materialized[photo_idx] = cell

        self._sync_nav_bar()

    # ══════════════════════════════════════════════════════════════════ mode scroll

    def _visible_range(self) -> tuple[int, int]:
        scroll_y = self.verticalScrollBar().value()
        vp_h     = max(1, self.viewport().height())
        spacing  = self._container.spacing
        cols     = self._container.cols
        row_h    = self._thumb_size + 8 + spacing

        first_visible_row = max(0, (scroll_y - spacing) // row_h)
        last_visible_row  = (scroll_y + vp_h - spacing) // row_h

        if self._buffer_active:
            # Marge d'un écran plein au-dessus/en dessous, activée seulement après
            # le 1er déplacement dans la grille (cf. _on_scroll) — le tout premier
            # affichage ne prépare que la partie visible pour un rendu immédiat.
            buffer_rows = max(1, -(-vp_h // row_h))  # ceil(vp_h / row_h)
            first_row = max(0, first_visible_row - buffer_rows)
            last_row  = last_visible_row + buffer_rows
        else:
            first_row = first_visible_row
            last_row  = last_visible_row

        first_idx = first_row * cols
        last_idx  = min(len(self._photos) - 1, (last_row + 1) * cols - 1)
        return first_idx, last_idx

    def _on_scroll(self) -> None:
        if self._ribbon_mode:
            return
        self._buffer_active = True
        self._update_date_overlay()
        if not self._placeholder_timer.isActive():
            self._placeholder_timer.start()
        self._load_timer.start()

    def _update_placeholders(self) -> None:
        if not self._photos or self._ribbon_mode:
            return
        first_idx, last_idx = self._visible_range()
        needed = set(range(first_idx, last_idx + 1))

        for i in list(self._materialized.keys()):
            if i not in needed:
                cell = self._materialized.pop(i)
                cell.cancel_pending_load()
                cell.setParent(None)

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
                self._materialized[i] = cell

    def _start_loading(self) -> None:
        for cell in self._materialized.values():
            cell.load(priority=10)

    def _update_materialized(self) -> None:
        if not self._photos:
            self._date_label.hide()
            return
        first_idx, last_idx = self._visible_range()
        needed = set(range(first_idx, last_idx + 1))

        for i in list(self._materialized.keys()):
            if i not in needed:
                cell = self._materialized.pop(i)
                cell.cancel_pending_load()
                cell.setParent(None)

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

        self._update_date_overlay()

    # ══════════════════════════════════════════════════════════════════ commun

    def _cancel_pending_workers(self) -> None:
        """Annule les workers en attente dans les pools (pas encore démarrés).
        À appeler avant toute réinitialisation majeure de la grille pour éviter
        que des dizaines de workers lisent le disque pour des photos invisibles.
        Les workers déjà en cours d'exécution ne sont pas interrompus ; leurs
        résultats seront stockés dans le cache RAM et réutilisables."""
        _get_thumb_pool().clear()
        _get_video_thumb_pool().clear()

    def _dematerialize_all(self) -> None:
        for cell in self._materialized.values():
            cell.cancel_pending_load()
            cell.setParent(None)
        self._materialized.clear()

    def _on_layout_changed(self) -> None:
        self._dematerialize_all()
        if self._ribbon_mode:
            QTimer.singleShot(0, self._ribbon_full_update)
        else:
            QTimer.singleShot(0, self._update_materialized)

    # ══════════════════════════════════════════════════════════════════ date overlay

    def set_date_overlay_visible(self, enabled: bool) -> None:
        self._date_overlay_enabled = enabled
        if not enabled:
            self._date_label.hide()
        else:
            self._update_date_overlay()

    def _update_date_overlay(self) -> None:
        if not self._date_overlay_enabled or not self._photos:
            self._date_label.hide()
            return

        if self._ribbon_mode:
            if not self._r_cols:
                self._date_label.hide()
                return
            # Photo centrale de la rangée du milieu
            center_start = sum(self._r_cols[:_RIBBON_CENTER])
            center_mid   = center_start + self._r_cols[_RIBBON_CENTER] // 2
            first_idx    = self._ribbon_offset + center_mid
        else:
            scroll_y  = self.verticalScrollBar().value()
            spacing   = self._container.spacing
            cols      = max(1, self._container.cols)
            row_h     = self._thumb_size + 8 + spacing
            first_row = max(0, (scroll_y - spacing) // row_h)
            first_idx = first_row * cols

        first_idx = max(0, min(first_idx, len(self._photos) - 1))
        photo = self._photos[first_idx]

        dt = photo.date_taken
        if dt is None and photo.file_mtime:
            try:
                dt = _dt.fromtimestamp(photo.file_mtime)
            except Exception:
                dt = None

        if dt is None:
            self._date_label.hide()
            return

        text = f"{dt.day} {_FR_MONTHS[dt.month - 1]} {dt.year}"
        self._date_label.setText(text)
        self._date_label.adjustSize()
        self._date_label.move(12, 12)
        self._date_label.show()
        self._date_label.raise_()

    # ══════════════════════════════════════════════════════════════════ molette

    def wheelEvent(self, event) -> None:
        if not self._ribbon_mode:
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y()
        if delta > 0:
            # Pas immédiat (réactivité) + impulsion de coasting pour le scroll rapide
            self._ribbon_offset -= 1
            self._inertia_vel = min(_INERTIA_MAX_VEL, self._inertia_vel + _INERTIA_IMPULSE)
        elif delta < 0:
            self._ribbon_offset += 1
            self._inertia_vel = max(-_INERTIA_MAX_VEL, self._inertia_vel - _INERTIA_IMPULSE)
        else:
            event.ignore()
            return

        self._clamp_ribbon_offset()
        self._update_ribbon_cells()
        self._update_date_overlay()

        if not self._inertia_timer.isActive():
            self._inertia_timer.start()
        event.accept()

    def _inertia_tick(self) -> None:
        """Tick du timer d'inertie (~60 fps) : applique la vélocité et la freine."""
        self._inertia_frac += self._inertia_vel
        steps = int(self._inertia_frac)          # tronque vers zéro
        if steps:
            self._inertia_frac -= steps
            old = self._ribbon_offset
            self._ribbon_offset -= steps          # vel > 0 → offset ↓ → photos vers droite
            self._clamp_ribbon_offset()
            if self._ribbon_offset != old:
                self._update_ribbon_cells()
                self._update_date_overlay()
            else:
                # Butée atteinte : couper l'élan
                self._inertia_vel  = 0.0
                self._inertia_frac = 0.0
                self._inertia_timer.stop()
                return

        # Freinage
        self._inertia_vel *= _INERTIA_FRICTION
        if abs(self._inertia_vel) < _INERTIA_STOP:
            self._inertia_vel  = 0.0
            self._inertia_frac = 0.0
            self._inertia_timer.stop()

    # ══════════════════════════════════════════════════════════════════ fabrique

    def _make_cell(self, photo: PhotoInfo, size: int | None = None) -> ThumbnailCell:
        if size is None:
            size = self._thumb_size
        cell = ThumbnailCell(photo, self._cache, size)
        cell.double_clicked.connect(self.photo_activated.emit)
        cell.right_clicked.connect(self._on_right_click)
        cell.clicked.connect(self._on_cell_clicked)
        cell.drag_started.connect(self._on_cell_drag_started)
        cell.duplicate_clicked.connect(self.duplicate_clicked.emit)
        return cell

    # ══════════════════════════════════════════════════════════════════ événements clavier/souris

    @Slot(object, object)
    def _on_cell_clicked(self, photo: PhotoInfo, modifiers) -> None:
        path  = photo.path
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
        if self._ribbon_mode:
            self.scroll_to_photo(path)

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
                px = cell._pixmap.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                drag.setPixmap(px)
                drag.setHotSpot(px.rect().center())
                break

        drag.exec(Qt.MoveAction)

    def set_index_error_paths(self, paths) -> None:
        """Met à jour l'ensemble des photos en erreur d'indexation faciale
        (timeout/crash) — pilote l'action "Retenter l'identification des visages"
        du menu contextuel."""
        self._index_error_paths = {os.path.normpath(p) for p in paths}

    def refresh_duplicate_status(self, assignments: dict) -> None:
        """Met à jour les badges de doublons. assignments = {path: group_id | None}."""
        for photo in self._photos:
            if photo.path in assignments:
                photo.duplicate_group_id = assignments[photo.path]
        for cell in self._materialized.values():
            if cell.photo.path in assignments:
                cell.set_duplicate_group(assignments[cell.photo.path])

    @Slot(object, object)
    def _on_right_click(self, photo: PhotoInfo, pos) -> None:
        # Effective selection: full selection if photo is already selected, else just this photo
        if photo.path in self._selected and len(self._selected) > 1:
            photos = self.get_selected()
        else:
            photos = [photo]

        menu = QMenu(self)
        menu.addAction("Ouvrir", lambda: self.photo_activated.emit(photo))
        menu.addSeparator()
        fav_label = "Retirer des favoris" if photo.is_favorite else "Marquer comme favori"
        menu.addAction(fav_label)
        menu.addAction("Renommer l'image", lambda: self.rename_requested.emit(photo))
        menu.addAction("Déplacer vers…", lambda: self.move_requested.emit(photo))
        menu.addAction("Enregistrer l'image traitée sur le disque",
                       lambda: self.save_requested.emit(photo))
        menu.addSeparator()
        n = len(photos)
        lbl = f"les {n} photos sélectionnées" if n > 1 else "cette photo"
        menu.addAction(f"Ajouter {lbl} à un album…",
                       lambda p=photos: self.add_to_album_requested.emit(p))
        menu.addAction(f"Créer un nouvel album avec {lbl}…",
                       lambda p=photos: self.create_album_with_requested.emit(p))
        menu.addSeparator()
        menu.addAction("Révéler dans l'Explorateur",
                       lambda: __import__('os').startfile(
                           __import__('os.path').path.dirname(photo.path)))
        if os.path.normpath(photo.path) in self._index_error_paths:
            menu.addSeparator()
            menu.addAction("Retenter l'identification des visages",
                           lambda: self.retry_face_index_requested.emit(photo))
        menu.addSeparator()
        menu.addAction("Effacer le fichier…",
                       lambda: self.delete_requested.emit([photo]))
        menu.exec(pos)

    def keyPressEvent(self, event) -> None:
        if self._ribbon_mode:
            key = event.key()
            if key == Qt.Key_Left:
                self._ribbon_offset -= 1
            elif key == Qt.Key_Right:
                self._ribbon_offset += 1
            elif key == Qt.Key_Up:
                self._ribbon_offset -= 3
            elif key == Qt.Key_Down:
                self._ribbon_offset += 3
            elif key == Qt.Key_Delete:
                selected = self.get_selected()
                if selected:
                    self.delete_requested.emit(selected)
                else:
                    center_idx = self._ribbon_offset + self._center_pos()
                    if 0 <= center_idx < len(self._photos):
                        self.delete_requested.emit([self._photos[center_idx]])
                event.accept()
                return
            else:
                super().keyPressEvent(event)
                return
            self._clamp_ribbon_offset()
            self._update_ribbon_cells()
            self._update_date_overlay()
            event.accept()
        elif event.key() == Qt.Key_Delete:
            selected = self.get_selected()
            if selected:
                self.delete_requested.emit(selected)
        else:
            super().keyPressEvent(event)
