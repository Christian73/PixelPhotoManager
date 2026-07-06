# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Diaporama plein écran des photos de l'album courant."""

import logging
import random

from PySide6.QtCore import Qt, QRectF, QSize, QTimer, QThread, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QWidget,
)

from src.core.models import PhotoInfo
from src.ui.photo_viewer import _build_pixmap

logger = logging.getLogger(__name__)

_INTERVAL_MS      = 5_000   # durée par défaut entre deux photos (5 s)
_INTERVAL_MIN_MS  = 1_000   # minimum 1 s
_INTERVAL_MAX_MS  = 60_000  # maximum 60 s
_INTERVAL_STEP_MS = 1_000   # pas d'ajustement : 1 s
_OVERLAY_TTL_MS   = 5_000   # délai avant masquage automatique de la barre

_KB_FPS  = 30     # fréquence de l'animation Ken Burns (images/s)
_KB_ZOOM = 0.08   # amplitude max du zoom (8 %)
_KB_PAN  = 0.55   # fraction de la marge disponible utilisée pour le pan

_BTN_STYLE = (
    "QPushButton { background: rgba(255,255,255,15); color: white; "
    "font-size: 18px; border: none; border-radius: 4px; padding: 6px 14px; }"
    "QPushButton:hover { background: rgba(255,255,255,35); color: #aad4ff; }"
    "QPushButton:disabled { color: #555; background: transparent; }"
)
_BTN_SMALL = (
    "QPushButton { background: rgba(255,255,255,15); color: white; "
    "font-size: 14px; border: none; border-radius: 4px; padding: 4px 10px; }"
    "QPushButton:hover { background: rgba(255,255,255,35); color: #aad4ff; }"
)


class _KenBurnsWidget(QWidget):
    """Widget plein-écran avec effet Ken Burns (zoom + pan lent)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self._pixmap:   QPixmap | None = None
        self._kb_start: tuple = (1.0, 0.5, 0.5)   # (zoom, cx, cy)
        self._kb_end:   tuple = (1.0, 0.5, 0.5)
        self._anim_t:   float = 0.0
        self._step:     float = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(1000 // _KB_FPS)
        self._timer.timeout.connect(self._tick)

    def set_pixmap(self, pixmap: QPixmap, duration_ms: int) -> None:
        self._pixmap  = pixmap
        self._anim_t  = 0.0
        self._step    = 1.0 / max(1, duration_ms * _KB_FPS // 1000)
        self._compute_kb()
        self._timer.start()
        self.update()

    def stop(self) -> None:
        self._timer.stop()

    def _compute_kb(self) -> None:
        if not self._pixmap:
            return
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0:
            return

        z_a = 1.0 + random.uniform(0.0, _KB_ZOOM)
        z_b = 1.0 + random.uniform(0.0, _KB_ZOOM)
        z_max = max(z_a, z_b)

        # Marge de pan = fraction du pixmap non visible à zoom max.
        # Composante verticale réduite à 35 % pour favoriser
        # les mouvements horizontaux et diagonaux.
        pan = 1.0 - 1.0 / z_max
        mx  = pan * _KB_PAN
        my  = pan * _KB_PAN * 0.35

        cx_a = 0.5 + random.uniform(-mx, mx)
        cy_a = 0.5 + random.uniform(-my, my)
        cx_b = 0.5 + random.uniform(-mx, mx)
        cy_b = 0.5 + random.uniform(-my, my)

        self._kb_start = (z_a, cx_a, cy_a)
        self._kb_end   = (z_b, cx_b, cy_b)

    def _tick(self) -> None:
        if self._anim_t >= 1.0:
            self._timer.stop()
            return
        self._anim_t = min(1.0, self._anim_t + self._step)
        self.update()

    def _src_rect(self, t: float) -> QRectF:
        if not self._pixmap:
            return QRectF()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        za, cxa, cya = self._kb_start
        zb, cxb, cyb = self._kb_end
        z  = za  + (zb  - za)  * t
        cx = cxa + (cxb - cxa) * t
        cy = cya + (cyb - cya) * t
        sw = pw / z
        sh = ph / z
        sx = cx * pw - sw / 2
        sy = cy * ph - sh / 2
        sx = max(0.0, min(sx, pw - sw))
        sy = max(0.0, min(sy, ph - sh))
        return QRectF(sx, sy, sw, sh)

    def _dst_rect(self) -> QRectF:
        """Rect destination centré dans le widget (letterbox / pillarbox)."""
        if not self._pixmap:
            return QRectF(self.rect())
        pw, ph = self._pixmap.width(), self._pixmap.height()
        ww, wh = self.width(), self.height()
        scale  = min(ww / pw, wh / ph) if pw and ph else 1.0
        dw, dh = pw * scale, ph * scale
        return QRectF((ww - dw) / 2, (wh - dh) / 2, dw, dh)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self._pixmap:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(
                self._dst_rect(),
                self._pixmap,
                self._src_rect(self._anim_t),
            )


class _LoadThread(QThread):
    """Charge une photo (avec retouches) dans un thread secondaire."""

    ready = Signal(int, object)   # (index_demandé, QPixmap)

    def __init__(
        self,
        index: int,
        photo: PhotoInfo,
        target_size: QSize,
        edit_db=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index       = index
        self._photo       = photo
        self._target_size = target_size
        self._edit_db     = edit_db

    def run(self) -> None:
        edit = self._edit_db.load(self._photo.path) if self._edit_db else None
        result = _build_pixmap(self._photo, edit)
        if result:
            pixmap, *_ = result
            if self._target_size and not self._target_size.isEmpty():
                pixmap = pixmap.scaled(
                    self._target_size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            self.ready.emit(self._index, pixmap)


class SlideshowWindow(QWidget):
    """
    Fenêtre plein écran de diaporama.

    Paramètres
    ----------
    photos      : liste ordonnée de PhotoInfo (index 0 = plus récente)
    start_index : index de la photo de départ dans la liste
    edit_db     : EditDatabase — pour appliquer les retouches non destructives

    Navigation
    ----------
    ▶ / → : photo plus récente (index - 1)
    ◀ / ← : photo plus ancienne (index + 1)

    Contrôles overlay (apparaissent au mouvement souris, masqués après 5 s)
    -----------------------------------------------------------------------
    ◀  plus ancienne  |  [−][Xs][+]  |  ⏸/▶  |  ▶  plus récente  |  ✕
    """

    def __init__(
        self,
        photos: list[PhotoInfo],
        start_index: int = 0,
        edit_db=None,
        parent=None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self._photos    = photos
        self._index     = max(0, min(start_index, len(photos) - 1))
        self._edit_db   = edit_db
        self._playing   = True
        self._interval  = _INTERVAL_MS

        # Thread courant + cache de préchargement {index: QPixmap}
        self._load_thread:    _LoadThread | None = None
        self._preload_thread: _LoadThread | None = None
        self._preload_cache:  dict[int, QPixmap] = {}

        self.setWindowTitle("Diaporama")
        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)

        self._setup_ui()
        self._setup_timers()
        # Connexions dépendant des deux : _hide_timer créé dans _setup_timers
        for _btn in self._overlay_btns:
            _btn.clicked.connect(self._hide_timer.start)
        self.showFullScreen()
        self._load_current()

    # ------------------------------------------------------------------ UI

    _OVERLAY_H = 90   # hauteur totale de la bande overlay (gradient inclus)

    def _setup_ui(self) -> None:
        # Widget Ken Burns couvre toute la fenêtre — pas de layout racine
        self._kb_widget = _KenBurnsWidget(self)
        self._kb_widget.setStyleSheet("background: black;")

        # Overlay flottant positionné en bas via resizeEvent
        # Gradient : transparent en haut → sombre en bas pour lisibilité
        self._overlay = QWidget(self)
        self._overlay.setStyleSheet(
            "background: qlineargradient("
            "x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(0,0,0,0), "
            "stop:0.35 rgba(0,0,0,160), "
            "stop:1 rgba(0,0,0,220));"
        )
        self._overlay.setMouseTracking(True)

        ol = QHBoxLayout(self._overlay)
        ol.setContentsMargins(24, 36, 24, 14)   # marges hautes généreuses pour le gradient
        ol.setSpacing(8)

        # Compteur position
        self._lbl_count = QLabel()
        self._lbl_count.setStyleSheet("color: #999; font-size: 12px; min-width: 56px;")
        ol.addWidget(self._lbl_count)

        ol.addStretch()

        # ◀ Plus ancienne
        self._btn_prev = QPushButton("◀  Précédente")
        self._btn_prev.setToolTip("Photo plus ancienne  (←)")
        self._btn_prev.setStyleSheet(_BTN_STYLE)
        self._btn_prev.clicked.connect(self._go_older)
        ol.addWidget(self._btn_prev)

        ol.addSpacing(16)

        # Contrôle du temps d'affichage : [−] [Xs] [+]
        btn_minus = QPushButton("−")
        btn_minus.setToolTip("Réduire la durée d'affichage")
        btn_minus.setStyleSheet(_BTN_SMALL)
        btn_minus.setFixedWidth(36)
        btn_minus.clicked.connect(self._decrease_interval)
        ol.addWidget(btn_minus)

        self._lbl_interval = QLabel(self._fmt_interval())
        self._lbl_interval.setStyleSheet(
            "color: #ddd; font-size: 13px; min-width: 34px;"
        )
        self._lbl_interval.setAlignment(Qt.AlignCenter)
        ol.addWidget(self._lbl_interval)

        btn_plus = QPushButton("+")
        btn_plus.setToolTip("Augmenter la durée d'affichage")
        btn_plus.setStyleSheet(_BTN_SMALL)
        btn_plus.setFixedWidth(36)
        btn_plus.clicked.connect(self._increase_interval)
        ol.addWidget(btn_plus)

        ol.addSpacing(16)

        # ⏸ / ▶ Play-pause
        self._btn_playpause = QPushButton("⏸")
        self._btn_playpause.setToolTip("Pause / Reprendre  (Espace)")
        self._btn_playpause.setStyleSheet(_BTN_STYLE)
        self._btn_playpause.clicked.connect(self._toggle_play)
        ol.addWidget(self._btn_playpause)

        ol.addSpacing(16)

        # ▶ Plus récente
        self._btn_next = QPushButton("Suivante  ▶")
        self._btn_next.setToolTip("Photo plus récente  (→)")
        self._btn_next.setStyleSheet(_BTN_STYLE)
        self._btn_next.clicked.connect(self._go_newer)
        ol.addWidget(self._btn_next)

        ol.addStretch()

        # ✕ Fermer
        btn_close = QPushButton("✕")
        btn_close.setToolTip("Quitter le diaporama  (Échap)")
        btn_close.setStyleSheet(_BTN_SMALL)
        btn_close.setFixedWidth(36)
        btn_close.clicked.connect(self.close)
        ol.addWidget(btn_close)

        # Sauvegarde des boutons pour connexion dans __init__ (après _setup_timers)
        self._overlay_btns = (
            self._btn_prev, btn_minus, btn_plus,
            self._btn_playpause, self._btn_next, btn_close,
        )

    def _setup_timers(self) -> None:
        self._advance_timer = QTimer(self)
        self._advance_timer.setInterval(self._interval)
        self._advance_timer.timeout.connect(self._advance)
        self._advance_timer.start()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_OVERLAY_TTL_MS)
        self._hide_timer.timeout.connect(self._overlay.hide)
        self._hide_timer.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        self._kb_widget.setGeometry(0, 0, w, h)
        self._overlay.setGeometry(0, h - self._OVERLAY_H, w, self._OVERLAY_H)

    # ------------------------------------------------------------------ chargement

    def _screen_size(self) -> QSize:
        return self._kb_widget.size() or self.size()

    def _load_current(self) -> None:
        if not self._photos:
            return
        photo = self._photos[self._index]
        n = len(self._photos)

        self._lbl_count.setText(f"{n - self._index} / {n}")
        # ◀ (plus ancienne) actif s'il reste des photos plus anciennes
        self._btn_prev.setEnabled(self._index < n - 1)
        # ▶ (plus récente) actif s'il reste des photos plus récentes
        self._btn_next.setEnabled(self._index > 0)

        # Si la photo est déjà préchargée, affichage immédiat
        if self._index in self._preload_cache:
            pixmap = self._preload_cache.pop(self._index)
            self._kb_widget.set_pixmap(pixmap, self._interval)
            self._start_preload()
            return

        # La photo précédente continue d'animer pendant le chargement
        self._cancel_load(self._load_thread)
        t = _LoadThread(self._index, photo, self._screen_size(), self._edit_db)
        t.ready.connect(self._on_pixmap_ready)
        t.finished.connect(lambda th=t: self._clear_thread('load', th))
        t.start()
        self._load_thread = t

    def _cancel_load(self, thread: "_LoadThread | None") -> None:
        """Déconnecte le signal ready d'un thread en cours (résultat sera ignoré)."""
        if thread is None:
            return
        try:
            if thread.isRunning():
                try:
                    thread.ready.disconnect()
                except RuntimeError:
                    pass
        except RuntimeError:
            pass  # C++ object déjà détruit — rien à faire

    def _clear_thread(self, kind: str, thread: "_LoadThread") -> None:
        """Appelé sur finished : efface la référence Python puis planifie la destruction Qt."""
        if kind == 'load' and self._load_thread is thread:
            self._load_thread = None
        elif kind == 'preload' and self._preload_thread is thread:
            self._preload_thread = None
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def _start_preload(self) -> None:
        """Précharge la photo suivante dans la direction de l'avance automatique."""
        next_idx = self._index - 1   # avance automatique = vers le plus récent
        if not (0 <= next_idx < len(self._photos)):
            next_idx = self._index + 1
        if not (0 <= next_idx < len(self._photos)):
            return
        if next_idx in self._preload_cache:
            return
        self._cancel_load(self._preload_thread)
        photo = self._photos[next_idx]
        t = _LoadThread(next_idx, photo, self._screen_size(), self._edit_db)
        t.ready.connect(self._on_preload_ready)
        t.finished.connect(lambda th=t: self._clear_thread('preload', th))
        t.start()
        self._preload_thread = t

    def _on_pixmap_ready(self, index: int, pixmap: QPixmap) -> None:
        if index != self._index:
            return   # navigation entre-temps, ignorer
        self._kb_widget.set_pixmap(pixmap, self._interval)
        self._start_preload()

    def _on_preload_ready(self, index: int, pixmap: QPixmap) -> None:
        self._preload_cache[index] = pixmap

    # ------------------------------------------------------------------ navigation

    def _advance(self) -> None:
        """Avance automatique vers la photo plus récente ; pause à la dernière."""
        if self._index > 0:
            self._index -= 1
            self._load_current()
        else:
            self._set_playing(False)

    def _go_older(self) -> None:
        """◀ / ← : aller vers la photo plus ancienne (index + 1)."""
        if self._index < len(self._photos) - 1:
            self._index += 1
            self._load_current()
            self._advance_timer.start()

    def _go_newer(self) -> None:
        """▶ / → : aller vers la photo plus récente (index - 1)."""
        if self._index > 0:
            self._index -= 1
            self._load_current()
            self._advance_timer.start()

    # ------------------------------------------------------------------ intervalle

    def _fmt_interval(self) -> str:
        s = self._interval // 1000
        return f"{s}s"

    def _decrease_interval(self) -> None:
        self._interval = max(_INTERVAL_MIN_MS, self._interval - _INTERVAL_STEP_MS)
        self._advance_timer.setInterval(self._interval)
        self._lbl_interval.setText(self._fmt_interval())

    def _increase_interval(self) -> None:
        self._interval = min(_INTERVAL_MAX_MS, self._interval + _INTERVAL_STEP_MS)
        self._advance_timer.setInterval(self._interval)
        self._lbl_interval.setText(self._fmt_interval())

    # ------------------------------------------------------------------ play/pause

    def _toggle_play(self) -> None:
        self._set_playing(not self._playing)

    def _set_playing(self, playing: bool) -> None:
        self._playing = playing
        self._btn_playpause.setText("⏸" if playing else "▶")
        self._btn_playpause.setToolTip(
            "Mettre en pause  (Espace)" if playing else "Reprendre le défilement  (Espace)"
        )
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
            self._go_older()
            self._show_overlay()
        elif key in (Qt.Key_Right, Qt.Key_Down):
            self._go_newer()
            self._show_overlay()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._advance_timer.stop()
        self._hide_timer.stop()
        self._kb_widget.stop()
        # Déconnecter les signaux pour ignorer tout résultat en cours
        self._cancel_load(self._load_thread)
        self._cancel_load(self._preload_thread)
        # Attendre la fin des threads (max 2 s chacun) avant que Qt détruise le widget
        for t in (self._load_thread, self._preload_thread):
            if t is not None:
                try:
                    t.wait(2000)
                except RuntimeError:
                    pass
        self._load_thread = None
        self._preload_thread = None
        super().closeEvent(event)
