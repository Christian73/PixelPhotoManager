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

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

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
        btn_detect = QPushButton("Détecter les doublons…")
        btn_detect.setToolTip("Analyser toutes les photos de la bibliothèque et regrouper les doublons")
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

        self._lbl_empty = QLabel("Aucun groupe de doublons — lancez une détection.")
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet("color: #555; padding: 24px;")
        self._lbl_empty.setVisible(False)
        self._content_vbox.addWidget(self._lbl_empty)

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

    # ------------------------------------------------------------------ public

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
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        n = len(groups)
        self._lbl_title.setText(
            f"{n} groupe{'s' if n > 1 else ''} de doublons" if n else ""
        )
        self._lbl_empty.setVisible(n == 0)
        self._card_area.setVisible(n > 0)

        for group_id, photos in groups.items():
            if not photos:
                continue
            card = _DuplicateCard(group_id, photos, self._card_area)
            card.view_requested.connect(self.view_requested)
            card.ignore_requested.connect(self.group_ignored)
            pix = self._thumb_cache.get(photos[0].path)
            card.set_thumbnail(pix)
            self._cards[group_id] = card

        self._reflow()

    def remove_group(self, group_id: int) -> None:
        """Retire une carte de groupe sans recharger toute la grille."""
        card = self._cards.pop(group_id, None)
        if card is not None:
            card.setParent(None)
            card.deleteLater()
        n = len(self._cards)
        self._lbl_title.setText(
            f"{n} groupe{'s' if n > 1 else ''} de doublons" if n else ""
        )
        self._lbl_empty.setVisible(n == 0)
        self._card_area.setVisible(n > 0)
        self._reflow()
