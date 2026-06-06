"""Diaporama plein écran des photos de l'album courant."""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from src.core.models import PhotoInfo
from src.ui.photo_viewer import _build_pixmap

logger = logging.getLogger(__name__)

_INTERVAL_MS    = 5_000   # durée par défaut entre deux photos
_OVERLAY_TTL_MS = 3_000   # délai avant masquage automatique de la barre

_BTN_STYLE = (
    "QPushButton { background: transparent; color: white; "
    "font-size: 22px; border: none; padding: 2px 10px; }"
    "QPushButton:hover { color: #aad4ff; }"
    "QPushButton:disabled { color: #555; }"
)


class _LoadThread(QThread):
    """Charge une photo (avec retouches) dans un thread secondaire."""

    ready = Signal(object)   # QPixmap

    def __init__(self, photo: PhotoInfo, edit_db=None, parent=None) -> None:
        super().__init__(parent)
        self._photo  = photo
        self._edit_db = edit_db

    def run(self) -> None:
        edit = self._edit_db.load(self._photo.path) if self._edit_db else None
        pixmap = _build_pixmap(self._photo, edit)
        if pixmap:
            self.ready.emit(pixmap)


class SlideshowWindow(QWidget):
    """
    Fenêtre plein écran de diaporama.

    Paramètres
    ----------
    photos      : liste ordonnée de PhotoInfo à afficher
    start_index : index de la photo de départ dans la liste
    edit_db     : EditDatabase — pour appliquer les retouches non destructives
    """

    def __init__(
        self,
        photos: list[PhotoInfo],
        start_index: int = 0,
        edit_db=None,
        parent=None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self._photos  = photos
        self._index   = max(0, min(start_index, len(photos) - 1))
        self._edit_db = edit_db
        self._playing = True
        self._load_thread: _LoadThread | None = None

        self.setWindowTitle("Diaporama")
        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)

        self._setup_ui()
        self._setup_timers()
        self.showFullScreen()
        self._load_current()

    # ------------------------------------------------------------------ UI

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Zone photo
        self._lbl_photo = QLabel()
        self._lbl_photo.setAlignment(Qt.AlignCenter)
        self._lbl_photo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._lbl_photo.setStyleSheet("background: black;")
        self._lbl_photo.setMouseTracking(True)
        root.addWidget(self._lbl_photo, stretch=1)

        # Barre de contrôle (overlay bas)
        self._overlay = QWidget()
        self._overlay.setStyleSheet("background: rgba(0,0,0,180);")
        self._overlay.setMouseTracking(True)
        ol = QHBoxLayout(self._overlay)
        ol.setContentsMargins(20, 8, 20, 10)
        ol.setSpacing(10)

        self._lbl_title = QLabel()
        self._lbl_title.setStyleSheet("color: #ccc; font-size: 12px;")
        self._lbl_title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        ol.addWidget(self._lbl_title)

        self._btn_prev = QPushButton("◀")
        self._btn_prev.setToolTip("Photo précédente  (←)")
        self._btn_prev.setStyleSheet(_BTN_STYLE)
        self._btn_prev.clicked.connect(self._go_prev)
        ol.addWidget(self._btn_prev)

        self._btn_playpause = QPushButton("⏸")
        self._btn_playpause.setToolTip("Pause / Lecture  (Espace)")
        self._btn_playpause.setStyleSheet(_BTN_STYLE)
        self._btn_playpause.clicked.connect(self._toggle_play)
        ol.addWidget(self._btn_playpause)

        self._btn_next = QPushButton("▶")
        self._btn_next.setToolTip("Photo suivante  (→)")
        self._btn_next.setStyleSheet(_BTN_STYLE)
        self._btn_next.clicked.connect(self._go_next)
        ol.addWidget(self._btn_next)

        self._lbl_count = QLabel()
        self._lbl_count.setStyleSheet("color: #888; font-size: 12px; min-width: 64px;")
        self._lbl_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ol.addWidget(self._lbl_count)

        btn_close = QPushButton("✕")
        btn_close.setToolTip("Quitter le diaporama  (Échap)")
        btn_close.setStyleSheet(_BTN_STYLE)
        btn_close.clicked.connect(self.close)
        ol.addWidget(btn_close)

        root.addWidget(self._overlay)

    def _setup_timers(self) -> None:
        self._advance_timer = QTimer(self)
        self._advance_timer.setInterval(_INTERVAL_MS)
        self._advance_timer.timeout.connect(self._advance)
        self._advance_timer.start()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_OVERLAY_TTL_MS)
        self._hide_timer.timeout.connect(self._overlay.hide)
        self._hide_timer.start()

    # ------------------------------------------------------------------ chargement

    def _load_current(self) -> None:
        if not self._photos:
            return
        photo = self._photos[self._index]
        n = len(self._photos)

        self._lbl_title.setText(Path(photo.path).name)
        self._lbl_count.setText(f"{self._index + 1} / {n}")
        self._btn_prev.setEnabled(self._index > 0)
        self._btn_next.setEnabled(self._index < n - 1)

        if self._load_thread and self._load_thread.isRunning():
            self._load_thread.ready.disconnect()
            self._load_thread.quit()

        self._load_thread = _LoadThread(photo, self._edit_db, self)
        self._load_thread.ready.connect(self._on_pixmap_ready)
        self._load_thread.start()

    def _on_pixmap_ready(self, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._lbl_photo.setPixmap(scaled)

    # ------------------------------------------------------------------ navigation

    def _advance(self) -> None:
        """Passage automatique à la photo suivante ; pause à la dernière."""
        if self._index < len(self._photos) - 1:
            self._index += 1
            self._load_current()
        else:
            self._set_playing(False)

    def _go_prev(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._load_current()
            self._advance_timer.start()

    def _go_next(self) -> None:
        if self._index < len(self._photos) - 1:
            self._index += 1
            self._load_current()
            self._advance_timer.start()

    def _toggle_play(self) -> None:
        self._set_playing(not self._playing)

    def _set_playing(self, playing: bool) -> None:
        self._playing = playing
        self._btn_playpause.setText("⏸" if playing else "▶")
        if playing:
            self._advance_timer.start()
        else:
            self._advance_timer.stop()

    # ------------------------------------------------------------------ overlay

    def _show_overlay(self) -> None:
        self._overlay.show()
        self.setCursor(Qt.ArrowCursor)
        self._hide_timer.start()

    # ------------------------------------------------------------------ events

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._show_overlay()
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            self.close()
        elif key == Qt.Key_Space:
            self._toggle_play()
            self._show_overlay()
        elif key in (Qt.Key_Left, Qt.Key_Up):
            self._go_prev()
            self._show_overlay()
        elif key in (Qt.Key_Right, Qt.Key_Down):
            self._go_next()
            self._show_overlay()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._advance_timer.stop()
        self._hide_timer.stop()
        if self._load_thread and self._load_thread.isRunning():
            self._load_thread.ready.disconnect()
            self._load_thread.quit()
            self._load_thread.wait(500)
        super().closeEvent(event)
