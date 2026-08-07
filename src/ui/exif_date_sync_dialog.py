# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Dialogue "Synchroniser la date de création Windows avec la date EXIF".

Pour chaque photo/vidéo du catalogue dont la date EXIF existe et est cohérente,
remplace la date de création Windows (st_ctime) par la date EXIF.
Génère un rapport CSV dans APP_DATA_DIR.
"""

import csv
import ctypes
import ctypes.wintypes
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel,
    QProgressBar, QPushButton, QVBoxLayout, QHBoxLayout,
)

from src.core.app_dirs import APP_DATA_DIR
from src.core.i18n import translate

logger = logging.getLogger(__name__)

_MIN_YEAR = 1990
_TOLERANCE_SEC = 2   # delta en secondes en deçà duquel on considère les dates identiques


def _exif_date_is_coherent(dt: datetime) -> bool:
    now = datetime.now()
    return _MIN_YEAR <= dt.year <= now.year + 1


def _set_file_creation_time(path: str, dt: datetime) -> None:
    """Applique dt comme date de création Windows (SetFileTime / FILETIME)."""
    # Convertir datetime naïf (heure locale) en timestamp UTC → FILETIME
    ts = dt.timestamp()
    val = int((ts + 11644473600) * 10_000_000)

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime",  ctypes.wintypes.DWORD),
                    ("dwHighDateTime", ctypes.wintypes.DWORD)]

    ft = FILETIME(dwLowDateTime=val & 0xFFFFFFFF,
                  dwHighDateTime=(val >> 32) & 0xFFFFFFFF)

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(path, 0x40000000, 1, None, 3, 0x02000000, None)
    if handle in (-1, 0):
        raise OSError(f"Impossible d'ouvrir le fichier : {ctypes.get_last_error()}")
    try:
        if not kernel32.SetFileTime(handle, ctypes.byref(ft), None, None):
            raise OSError(f"SetFileTime a échoué : {ctypes.get_last_error()}")
    finally:
        kernel32.CloseHandle(handle)


# ------------------------------------------------------------------ thread

class _SyncThread(QThread):
    progress  = Signal(int, int, str)   # current, total, filename
    finished  = Signal(int, int, str)   # updated, skipped, csv_path

    def __init__(self, catalog, parent=None) -> None:
        super().__init__(parent)
        self._catalog   = catalog
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        self.setPriority(QThread.LowestPriority)

        photos   = self._catalog.get_all_photos()
        total    = len(photos)
        updated  = 0
        skipped  = 0

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = str(APP_DATA_DIR / f"exif_date_sync_{ts}.csv")
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow([
                translate("ExifDateSyncDialog", "Chemin"),
                translate("ExifDateSyncDialog", "Date création fichier"),
                translate("ExifDateSyncDialog", "Date EXIF"),
                translate("ExifDateSyncDialog", "Action"),
                translate("ExifDateSyncDialog", "Commentaire"),
            ])

            for i, photo in enumerate(photos):
                if self._stop_flag:
                    break

                self.progress.emit(i + 1, total, photo.filename)

                path = photo.path
                if not os.path.exists(path):
                    writer.writerow([path, "", "",
                                     translate("ExifDateSyncDialog", "ignoré"),
                                     translate("ExifDateSyncDialog", "fichier introuvable")])
                    skipped += 1
                    continue

                # Date EXIF
                dt_exif = photo.date_taken
                if dt_exif is None or not _exif_date_is_coherent(dt_exif):
                    writer.writerow([path, "", "",
                                     translate("ExifDateSyncDialog", "ignoré"),
                                     translate("ExifDateSyncDialog",
                                               "pas de date EXIF cohérente")])
                    skipped += 1
                    continue

                # Date de création Windows actuelle
                try:
                    st = os.stat(path)
                    ctime_current = st.st_ctime
                except OSError as e:
                    writer.writerow([path, "", dt_exif.isoformat(),
                                     translate("ExifDateSyncDialog", "erreur"), str(e)])
                    skipped += 1
                    continue

                ctime_dt = datetime.fromtimestamp(ctime_current)
                exif_ts  = dt_exif.timestamp()

                if abs(ctime_current - exif_ts) <= _TOLERANCE_SEC:
                    writer.writerow([
                        path,
                        ctime_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        dt_exif.strftime("%Y-%m-%d %H:%M:%S"),
                        translate("ExifDateSyncDialog", "ignoré"),
                        translate("ExifDateSyncDialog", "dates déjà identiques"),
                    ])
                    skipped += 1
                    continue

                try:
                    _set_file_creation_time(path, dt_exif)
                    writer.writerow([
                        path,
                        ctime_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        dt_exif.strftime("%Y-%m-%d %H:%M:%S"),
                        translate("ExifDateSyncDialog", "mis à jour"), "",
                    ])
                    updated += 1
                except Exception as e:
                    writer.writerow([
                        path,
                        ctime_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        dt_exif.strftime("%Y-%m-%d %H:%M:%S"),
                        translate("ExifDateSyncDialog", "erreur"), str(e),
                    ])
                    skipped += 1

        self.finished.emit(updated, skipped, csv_path)


# ------------------------------------------------------------------ dialogue

class ExifDateSyncDialog(QDialog):

    def __init__(self, catalog, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._thread: _SyncThread | None = None
        self._csv_path: str = ""

        self.setWindowTitle(translate("ExifDateSyncDialog", "Synchroniser les dates de création avec l'EXIF"))
        self.setMinimumWidth(520)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._setup_ui()

    # ------------------------------------------------------------------ ui

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 20)

        title = QLabel(translate("ExifDateSyncDialog", "Synchroniser la date de création Windows avec la date EXIF"))
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        layout.addWidget(title)

        # Explication
        info_frame = QFrame()
        info_frame.setFrameShape(QFrame.StyledPanel)
        info_frame.setStyleSheet(
            "QFrame { background: #252525; border: 1px solid #444; border-radius: 4px; }"
        )
        info_layout = QVBoxLayout(info_frame)
        info_layout.setContentsMargins(14, 12, 14, 12)
        info_layout.setSpacing(8)

        lbl_expl = QLabel(
            translate("ExifDateSyncDialog", "Lors d'un transfert depuis un appareil photo ou d'une copie de fichiers, "
            "Windows peut attribuer la date du jour comme date de création, "
            "écrasant la date réelle de prise de vue.\n\n"
            "Cette opération parcourt toutes les photos et vidéos du catalogue et, "
            "lorsqu'une date EXIF valide est présente et différente de la date de "
            "création Windows, elle remplace la date de création par la date EXIF.\n\n"
            "Un rapport CSV détaillant chaque fichier traité sera enregistré dans :")
        )
        lbl_expl.setWordWrap(True)
        lbl_expl.setStyleSheet("color: #ccc; font-size: 12px;")
        info_layout.addWidget(lbl_expl)

        lbl_dir = QLabel(str(APP_DATA_DIR))
        lbl_dir.setStyleSheet("color: #7ac; font-size: 11px; font-style: italic;")
        lbl_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info_layout.addWidget(lbl_dir)

        layout.addWidget(info_frame)

        # Avertissement
        lbl_warn = QLabel(
            translate("ExifDateSyncDialog", "⚠  Cette opération modifie les métadonnées système des fichiers originaux. "
            "Elle ne modifie pas le contenu des photos.")
        )
        lbl_warn.setWordWrap(True)
        lbl_warn.setStyleSheet("color: #e8a030; font-size: 11px;")
        layout.addWidget(lbl_warn)

        # Barre de progression (masquée au départ)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.hide()
        layout.addWidget(self._progress_bar)

        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 11px;")
        self._lbl_status.setAlignment(Qt.AlignCenter)
        self._lbl_status.hide()
        layout.addWidget(self._lbl_status)

        # Résultat (masqué au départ)
        self._lbl_result = QLabel("")
        self._lbl_result.setWordWrap(True)
        self._lbl_result.setAlignment(Qt.AlignCenter)
        self._lbl_result.setStyleSheet("color: #8dc87a; font-size: 12px; font-weight: bold;")
        self._lbl_result.hide()
        layout.addWidget(self._lbl_result)

        # Boutons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._btn_open_csv = QPushButton(translate("ExifDateSyncDialog", "Ouvrir le rapport CSV"))
        self._btn_open_csv.setStyleSheet(
            "QPushButton { background: #2a5a2a; color: white; border: none;"
            " border-radius: 3px; padding: 6px 16px; }"
            "QPushButton:hover { background: #3a6a3a; }"
        )
        self._btn_open_csv.clicked.connect(self._open_csv)
        self._btn_open_csv.hide()
        btn_layout.addWidget(self._btn_open_csv)

        btn_layout.addStretch()

        self._btn_start = QPushButton(translate("ExifDateSyncDialog", "Démarrer"))
        self._btn_start.setStyleSheet(
            "QPushButton { background: #2a5a8a; color: white; border: none;"
            " border-radius: 3px; padding: 6px 20px; font-weight: bold; }"
            "QPushButton:hover { background: #3a6a9a; }"
        )
        self._btn_start.clicked.connect(self._start)
        btn_layout.addWidget(self._btn_start)

        self._btn_close = QPushButton(translate("ExifDateSyncDialog", "Fermer"))
        self._btn_close.clicked.connect(self._on_close)
        btn_layout.addWidget(self._btn_close)

        layout.addLayout(btn_layout)

    # ------------------------------------------------------------------ actions

    def _start(self) -> None:
        self._btn_start.setEnabled(False)
        self._btn_start.setText(translate("ExifDateSyncDialog", "En cours…"))
        self._progress_bar.show()
        self._lbl_status.show()

        self._thread = _SyncThread(self._catalog, self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_finished)
        self._thread.start()

    def _on_progress(self, current: int, total: int, filename: str) -> None:
        pct = int(current * 100 / total) if total else 100
        self._progress_bar.setValue(pct)
        self._lbl_status.setText(f"{current} / {total}  —  {filename}")

    def _on_finished(self, updated: int, skipped: int, csv_path: str) -> None:
        self._csv_path = csv_path
        self._progress_bar.setValue(100)
        self._lbl_status.hide()

        self._lbl_result.setText(
            translate("ExifDateSyncDialog", "%n fichier(s) mis à jour", None, updated)
            + "  ·  "
            + translate("ExifDateSyncDialog", "%n ignoré(s) ou en erreur", None, skipped)
        )
        self._lbl_result.show()
        self._btn_open_csv.show()
        self._btn_start.hide()
        self._btn_close.setDefault(True)
        self._btn_close.setFocus()

    def _open_csv(self) -> None:
        if self._csv_path and os.path.exists(self._csv_path):
            os.startfile(self._csv_path)

    def _on_close(self) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)
        self.accept()

    def closeEvent(self, event) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)
        super().closeEvent(event)
