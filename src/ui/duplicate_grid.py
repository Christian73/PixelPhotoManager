# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
DuplicateGrid — grille des groupes de doublons détectés.

Contrairement à FaceClusterGrid, les groupes sont déjà calculés et stockés
(`duplicate_group_id` en base, cf. duplicate_detector.py) : pas de clustering
à recalculer ici, juste un chargement + affichage de cartes.

1 carte par groupe : vignette du 1er exemplaire, nombre d'exemplaires, bouton
✗ superposé pour ignorer (dissoudre) le groupe entier.
Double-clic : comparaison rapide (ouvre la visionneuse sur les photos du groupe).
"""

import logging

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot, QByteArray
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from src.ui.thumbnail_grid import _ThumbSignals, _ThumbWorker, _get_thumb_pool

logger = logging.getLogger(__name__)

_CARD_IMG     = 130
_CARD_W       = 148
_CARD_SPACING = 10
_COLS_MIN     = 2

_BTN_OVL = 22   # diamètre du bouton ✗ superposé sur la vignette
_BTN_REJECT_STYLE = (
    "QPushButton { background: rgba(170,30,30,215); color: white;"
    " border-radius: 11px; font-weight: bold; font-size: 13px; border: none; padding: 0; }"
    "QPushButton:hover { background: rgba(220,50,50,255); }"
)


# ------------------------------------------------------------------ load thread

class _DuplicateGroupLoadThread(QThread):
    """Charge tous les groupes de doublons depuis le catalogue en arrière-plan."""

    groups_ready = Signal(object)  # dict[int, list[PhotoInfo]]

    def __init__(self, catalog, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog

    def run(self) -> None:
        try:
            groups = self._catalog.get_duplicate_groups()
        except Exception:
            logger.exception("_DuplicateGroupLoadThread: erreur inattendue")
            groups = {}
        self.groups_ready.emit(groups)


# ------------------------------------------------------------------ card

class _DuplicateCard(QFrame):
    """Carte représentant un groupe de doublons.

    Double-clic → comparaison rapide dans la visionneuse.
    Clic sur ✗   → ignorer (dissoudre) le groupe entier.
    """

    view_requested   = Signal(int)  # group_id
    ignore_requested = Signal(int)  # group_id

    _STYLE = """
        QFrame {
            border: 2px solid #3a3a3a;
            border-radius: 6px;
            background: #252525;
        }
        QFrame:hover {
            border-color: #7aabdb;
            background: #2a3545;
        }
    """

    def __init__(self, group_id: int, photos: list, parent=None) -> None:
        super().__init__(parent)
        self._group_id = group_id
        self._cover_path = photos[0].path if photos else None
        self._cache = None
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_thumb_ready)
        self._worker: "_ThumbWorker | None" = None

        self.setFixedWidth(_CARD_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._STYLE)
        self.setToolTip("Double-clic : comparer dans la visionneuse — ✗ : ignorer ce groupe")

        col = QVBoxLayout(self)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(4)
        col.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self._lbl_img = QLabel()
        self._lbl_img.setFixedSize(_CARD_IMG, _CARD_IMG)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet("border: none; border-radius: 4px; background: #1a1a1a;")
        col.addWidget(self._lbl_img, alignment=Qt.AlignHCenter)

        n = len(photos)
        plural = "s" if n > 1 else ""
        lbl_count = QLabel(f"{n} exemplaire{plural}")
        lbl_count.setAlignment(Qt.AlignCenter)
        lbl_count.setStyleSheet("border: none; font-size: 11px; color: #aaa;")
        col.addWidget(lbl_count)

        btn_ignore = QPushButton("✗", self._lbl_img)
        btn_ignore.setGeometry(3, _CARD_IMG - _BTN_OVL - 3, _BTN_OVL, _BTN_OVL)
        btn_ignore.setStyleSheet(_BTN_REJECT_STYLE)
        btn_ignore.setCursor(Qt.PointingHandCursor)
        btn_ignore.setToolTip("Ignorer ce groupe de doublons")
        btn_ignore.clicked.connect(lambda: self.ignore_requested.emit(self._group_id))

    @property
    def group_id(self) -> int:
        return self._group_id

    def load_thumbnail(self, cache) -> None:
        """Charge la vignette via le pool partagé (thumbnail_grid._get_thumb_pool) —
        ne jamais décoder le JPEG sur le thread UI (cf. règle CLAUDE.md)."""
        self._cache = cache
        if not self._cover_path:
            self.set_thumbnail(None)
            return
        pixmap = cache.get_ram(self._cover_path)
        if pixmap:
            self.set_thumbnail(pixmap)
            return
        worker = _ThumbWorker(self._cover_path, cache, self._signals)
        self._worker = worker
        _get_thumb_pool().start(worker)

    @Slot(str, object)
    def _on_thumb_ready(self, path: str, data: object) -> None:
        if path != self._cover_path:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(QByteArray(data))
        if not pixmap.isNull():
            self._cache.store_pixmap(path, pixmap)
            self.set_thumbnail(pixmap)

    def set_thumbnail(self, pix: "QPixmap | None") -> None:
        if pix is None:
            self._lbl_img.setText("?")
            self._lbl_img.setStyleSheet(
                "border: none; border-radius: 4px; background: #1a1a1a; color: #555;"
            )
            return
        scaled = pix.scaled(
            _CARD_IMG, _CARD_IMG,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._lbl_img.setPixmap(scaled)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.view_requested.emit(self._group_id)
        super().mouseDoubleClickEvent(event)


# ------------------------------------------------------------------ grid

class DuplicateGrid(QWidget):
    """
    Zone principale affichant les groupes de doublons détectés.

    Signals
    -------
    back_requested()          — retourner à la grille de photos
    view_requested(group_id)  — comparaison rapide (ouvrir la visionneuse)
    group_ignored(group_id)   — ignorer (dissoudre) un groupe
    detect_requested()        — lancer une nouvelle détection de doublons
    """

    back_requested   = Signal()
    view_requested   = Signal(int)
    group_ignored    = Signal(int)
    detect_requested = Signal()

    def __init__(self, catalog, thumb_cache, parent=None) -> None:
        super().__init__(parent)
        self._catalog     = catalog
        self._thumb_cache = thumb_cache
        self._cards: dict[int, _DuplicateCard] = {}
        self._current_cols: int = _COLS_MIN
        self._load_thread: "_DuplicateGroupLoadThread | None" = None
        self._loaded = False
        self._scanning = False
        self._last_signature: dict[int, tuple] | None = None
        self._setup_ui()

    # ------------------------------------------------------------------ setup

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(6)

        bar = QHBoxLayout()
        btn_back = QPushButton("← Photos")
        btn_back.setToolTip("Retourner à la grille de photos")
        btn_back.clicked.connect(self.back_requested)
        bar.addWidget(btn_back)
        bar.addStretch()
        self._lbl_title = QLabel()
        self._lbl_title.setStyleSheet("font-weight: bold; color: #ccc; font-size: 13px;")
        bar.addWidget(self._lbl_title)
        bar.addStretch()
        btn_detect = QPushButton("Vérifier maintenant")
        btn_detect.setToolTip(
            "Force une vérification immédiate — l'analyse tourne aussi "
            "automatiquement en arrière-plan"
        )
        btn_detect.clicked.connect(self.detect_requested)
        bar.addWidget(btn_detect)
        root.addLayout(bar)

        self._content = QWidget()
        self._content_vbox = QVBoxLayout(self._content)
        self._content_vbox.setContentsMargins(0, 4, 0, 8)
        self._content_vbox.setSpacing(0)

        self._card_area = QWidget()
        self._card_area.setStyleSheet("background: transparent;")
        self._card_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._card_gl = QGridLayout(self._card_area)
        self._card_gl.setSpacing(_CARD_SPACING)
        self._card_gl.setContentsMargins(0, 0, 0, 0)
        self._content_vbox.addWidget(self._card_area)

        # Panneau vide/état — mutuellement exclusif avec _card_area, occupe tout
        # l'espace restant (stretch=1) et centre son contenu via l'alignement du
        # layout interne : centre bien à l'écran quel que soit le nombre de
        # cartes (0 ici) et la hauteur réelle du viewport.
        self._empty_panel = QWidget()
        empty_vbox = QVBoxLayout(self._empty_panel)
        empty_vbox.setAlignment(Qt.AlignCenter)
        empty_vbox.setSpacing(10)

        self._lbl_empty = QLabel("Aucun groupe de doublons.")
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet("color: #555; padding: 8px;")
        empty_vbox.addWidget(self._lbl_empty)

        self._btn_detect_empty = QPushButton("Vérifier maintenant")
        self._btn_detect_empty.setToolTip(
            "Force une vérification immédiate — l'analyse tourne aussi "
            "automatiquement en arrière-plan"
        )
        self._btn_detect_empty.clicked.connect(self.detect_requested)
        empty_vbox.addWidget(self._btn_detect_empty, alignment=Qt.AlignHCenter)

        self._lbl_scanning = QLabel("Recherche de doublons en cours…")
        self._lbl_scanning.setAlignment(Qt.AlignCenter)
        self._lbl_scanning.setStyleSheet("color: #aaa; font-size: 13px; padding: 8px;")
        empty_vbox.addWidget(self._lbl_scanning)

        self._scan_bar = QProgressBar()
        self._scan_bar.setFixedWidth(220)
        self._scan_bar.setFixedHeight(6)
        self._scan_bar.setTextVisible(False)
        self._scan_bar.setRange(0, 0)  # animation indéterminée (marquee)
        self._scan_bar.setStyleSheet(
            "QProgressBar { background: #1e1e2e; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #4a8fd4; border-radius: 3px; }"
        )
        empty_vbox.addWidget(self._scan_bar, alignment=Qt.AlignHCenter)

        self._empty_panel.setVisible(False)
        self._content_vbox.addWidget(self._empty_panel, 1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll)

    # ------------------------------------------------------------------ resize

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        available = self._scroll.viewport().width()
        cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))
        if cols != self._current_cols and self._cards:
            self._current_cols = cols
            self._reflow()

    def _reflow(self) -> None:
        while self._card_gl.count():
            self._card_gl.takeAt(0)
        for c in range(self._card_gl.columnCount() + self._current_cols + 1):
            self._card_gl.setColumnStretch(c, 0)
        self._card_gl.setColumnStretch(self._current_cols, 1)
        for i, card in enumerate(self._cards.values()):
            self._card_gl.addWidget(
                card, i // self._current_cols, i % self._current_cols,
                Qt.AlignLeft | Qt.AlignTop,
            )

    def _force_reflow(self) -> None:
        """Recalcule les colonnes depuis la largeur réelle et replace toutes les
        cartes. Appelé en différé après un chargement pour corriger le cas où
        le viewport n'était pas encore dimensionné au moment du premier affichage
        (ex: bascule depuis un QStackedWidget sans redimensionnement réel — cf.
        même pattern dans face_cluster_grid.py::_force_reflow)."""
        available = self._scroll.viewport().width()
        if available <= 0:
            QTimer.singleShot(50, self._force_reflow)  # viewport pas encore prêt
            return
        self._current_cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))
        self._reflow()

    # ------------------------------------------------------------------ empty/scanning state

    def _update_empty_state(self) -> None:
        empty = not self._cards
        self._card_area.setVisible(not empty)
        self._empty_panel.setVisible(empty)
        if empty:
            self._lbl_scanning.setVisible(self._scanning)
            self._scan_bar.setVisible(self._scanning)
            self._lbl_empty.setVisible(not self._scanning)
            self._btn_detect_empty.setVisible(not self._scanning)

    def set_scanning(self, scanning: bool) -> None:
        """Affiche/masque l'indicateur de recherche en cours (visible uniquement
        quand la grille est vide — cf. DuplicateDetectorThread côté main_window)."""
        if self._scanning == scanning:
            return
        self._scanning = scanning
        self._update_empty_state()

    # ------------------------------------------------------------------ public

    def ensure_loaded(self) -> None:
        """Affiche l'écran sans recharger si les données déjà en mémoire
        sont à jour (ex: simple retour depuis la visionneuse) ; ne
        recharge que lors du premier affichage ou après invalidate()."""
        if not self._loaded:
            self.refresh()

    def invalidate(self) -> None:
        """Marque les données courantes comme périmées : le prochain
        ensure_loaded() déclenchera un vrai rechargement."""
        self._loaded = False

    def refresh(self) -> None:
        """Recharge tous les groupes de doublons depuis le catalogue."""
        if self._load_thread is not None:
            if self._load_thread.isRunning():
                return
            self._load_thread.deleteLater()
            self._load_thread = None

        self._load_thread = _DuplicateGroupLoadThread(self._catalog, self)
        self._load_thread.groups_ready.connect(self._on_groups_ready)
        self._load_thread.start()

    def _on_groups_ready(self, groups: dict) -> None:
        signature = {
            group_id: tuple(sorted(p.path for p in photos))
            for group_id, photos in groups.items() if photos
        }
        if self._loaded and signature == self._last_signature:
            # Contenu identique à ce qui est déjà affiché (snapshot périodique
            # pendant un scan en arrière-plan, cf. _LIVE_SNAPSHOT_INTERVAL côté
            # duplicate_detector.py) — éviter de tout détruire/reconstruire
            # (vignettes incluses) pour ne pas provoquer de clignotement.
            return

        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        n = len(groups)
        self._lbl_title.setText(
            f"{n} groupe{'s' if n > 1 else ''} de doublons" if n else ""
        )

        for group_id, photos in groups.items():
            if not photos:
                continue
            card = _DuplicateCard(group_id, photos, self._card_area)
            card.view_requested.connect(self.view_requested)
            card.ignore_requested.connect(self.group_ignored)
            card.load_thumbnail(self._thumb_cache)
            self._cards[group_id] = card

        self._update_empty_state()
        self._loaded = True
        self._last_signature = signature
        QTimer.singleShot(0, self._force_reflow)

    def remove_group(self, group_id: int) -> None:
        """Retire une carte de groupe sans recharger toute la grille."""
        card = self._cards.pop(group_id, None)
        if card is not None:
            card.setParent(None)
            card.deleteLater()
        if self._last_signature is not None:
            self._last_signature.pop(group_id, None)
        n = len(self._cards)
        self._lbl_title.setText(
            f"{n} groupe{'s' if n > 1 else ''} de doublons" if n else ""
        )
        self._update_empty_state()
        self._reflow()
