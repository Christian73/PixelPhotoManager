# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialog listant les photos en erreur d'indexation faciale (timeout/crash),
avec une nouvelle tentative fichier par fichier (Outils > Visualisation des erreurs…)."""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from src.faces.face_database import FaceDatabase
from src.library.thumbnail_cache import ThumbnailCache

_ERROR_LABELS = {
    "timeout": "Timeout pendant la détection",
    "crash": "Crash du sous-processus de détection",
}


# ---------------------------------------------------------------------------
# Widget par photo en erreur

class _ErrorRow(QWidget):
    retry_clicked = Signal(str)

    def __init__(self, photo_path: str, error_type: str, thumb_cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self._path = photo_path
        self._setup_ui(photo_path, error_type, thumb_cache)

    def _setup_ui(self, photo_path: str, error_type: str, thumb_cache: ThumbnailCache) -> None:
        self.setStyleSheet(
            "background: #252525; border-radius: 4px; border: 1px solid #333;"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(10)

        thumb_lbl = QLabel()
        thumb_lbl.setFixedSize(56, 56)
        thumb_lbl.setAlignment(Qt.AlignCenter)
        pix = thumb_cache.get(photo_path)
        if pix is not None:
            thumb_lbl.setPixmap(
                pix.scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            thumb_lbl.setText("?")
            thumb_lbl.setStyleSheet(
                "color: #555; background: #1a1a1a; border-radius: 3px; border: none;"
            )
        row.addWidget(thumb_lbl)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)

        name_lbl = QLabel(os.path.basename(photo_path))
        name_lbl.setStyleSheet(
            "color: #ddd; font-size: 12px; font-weight: bold; background: transparent; border: none;"
        )
        info_col.addWidget(name_lbl)

        path_lbl = QLabel(photo_path)
        path_lbl.setStyleSheet("color: #888; font-size: 10px; background: transparent; border: none;")
        path_lbl.setToolTip(photo_path)
        info_col.addWidget(path_lbl)

        err_lbl = QLabel(_ERROR_LABELS.get(error_type, error_type))
        err_lbl.setStyleSheet("color: #c88; font-size: 10px; background: transparent; border: none;")
        info_col.addWidget(err_lbl)

        row.addLayout(info_col, stretch=1)

        btn_retry = QPushButton("⟳  Réessayer")
        btn_retry.setToolTip("Relancer l'identification des visages pour ce seul fichier")
        btn_retry.setStyleSheet(
            "QPushButton { background: #2a5080; color: white; border: none;"
            " border-radius: 3px; padding: 4px 12px; }"
            "QPushButton:hover { background: #3a6090; }"
        )
        btn_retry.clicked.connect(lambda: self.retry_clicked.emit(self._path))
        row.addWidget(btn_retry)


# ---------------------------------------------------------------------------
# Dialog principal

class IndexErrorsDialog(QDialog):
    retry_requested = Signal(str)   # chemin de la photo à ré-essayer

    def __init__(self, face_db: FaceDatabase, thumb_cache: ThumbnailCache, parent=None):
        super().__init__(parent)
        self._face_db = face_db
        self._thumb_cache = thumb_cache
        self.setWindowTitle("Visualisation des erreurs d'identification")
        self.setMinimumWidth(560)
        self.setMinimumHeight(420)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 14)

        lbl = QLabel("Photos en erreur d'identification faciale")
        lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #ccc;")
        root.addWidget(lbl)

        note = QLabel(
            "Ces fichiers ont provoqué un timeout ou un crash pendant la détection "
            "des visages. Ils sont automatiquement exclus des analyses tant que le "
            "problème persiste — relancez le traitement fichier par fichier."
        )
        note.setStyleSheet("color: #888; font-size: 10px;")
        note.setWordWrap(True)
        root.addWidget(note)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("border: 1px solid #333;")

        self._container = QWidget()
        self._container.setStyleSheet("background: #1e1e1e;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(6)
        self._layout.addStretch()

        self._scroll.setWidget(self._container)
        root.addWidget(self._scroll, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton("Fermer")
        btn_close.setFixedWidth(80)
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        self.refresh()

    def refresh(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        paths = self._face_db.get_error_paths()
        if not paths:
            empty = QLabel("Aucune erreur d'identification en attente.")
            empty.setStyleSheet("color: #555; font-size: 11px; padding: 24px;")
            empty.setAlignment(Qt.AlignCenter)
            self._layout.insertWidget(0, empty)
            return

        for i, path in enumerate(paths):
            info = self._face_db.get_index_error(path)
            error_type = info["error_type"] if info else "?"
            row = _ErrorRow(path, error_type, self._thumb_cache, self._container)
            row.retry_clicked.connect(self.retry_requested.emit)
            self._layout.insertWidget(i, row)
