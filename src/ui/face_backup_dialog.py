# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
FaceBackupDialog — sauvegarde et restauration de la reconnaissance faciale.

Format de sauvegarde : ZIP contenant
  • faces.db   — copie complète de la base des visages (détections, embeddings,
                  clusters, associations personnes)
  • persons.json — export de catalog.db::persons (id, name, created_at)

Stockage : APP_DATA_DIR / faces_backups / visages_YYYYMMDD_HHMMSS.zip
"""

import json
import logging
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from src.core.i18n import translate

logger = logging.getLogger(__name__)

_BACKUP_SUBDIR = "faces_backups"
_TS_FMT = "%Y%m%d_%H%M%S"
_MONTHS = [
    translate("FaceBackupDialog", "Jan"), translate("FaceBackupDialog", "Feb"),
    translate("FaceBackupDialog", "Mar"), translate("FaceBackupDialog", "Apr"),
    translate("FaceBackupDialog", "May"), translate("FaceBackupDialog", "Jun"),
    translate("FaceBackupDialog", "Jul"), translate("FaceBackupDialog", "Aug"),
    translate("FaceBackupDialog", "Sep"), translate("FaceBackupDialog", "Oct"),
    translate("FaceBackupDialog", "Nov"), translate("FaceBackupDialog", "Dec"),
]


# ------------------------------------------------------------------ helpers

def _backup_dir(app_data_dir: Path) -> Path:
    d = app_data_dir / _BACKUP_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_ts(zip_path: Path) -> str:
    """'visages_20260627_143210.zip' → '27 juin 2026 à 14:32'"""
    try:
        ts = zip_path.stem[len("visages_"):]
        dt = datetime.strptime(ts, _TS_FMT)
        return translate("FaceBackupDialog", "{month} {d}, {y} at {time}").format(
            d=dt.day, month=_MONTHS[dt.month - 1], y=dt.year, time=dt.strftime('%H:%M'))
    except Exception:
        return zip_path.stem


def _fmt_size(n: int) -> str:
    if n >= 1_000_000:
        return translate("Units", "{n} MB").format(n=f"{n / 1_000_000:.1f}")
    return translate("Units", "{n} kB").format(n=f"{n // 1_000}")


def list_backups(app_data_dir: Path) -> list[Path]:
    """Retourne les sauvegardes triées du plus récent au plus ancien."""
    d = _backup_dir(app_data_dir)
    return sorted(d.glob("visages_*.zip"), reverse=True)


# ------------------------------------------------------------------ core ops

def create_backup(
    faces_db_path: Path,
    catalog_db_path: Path,
    app_data_dir: Path,
) -> Path:
    """Crée une sauvegarde ZIP. Retourne le chemin du fichier créé."""
    bdir = _backup_dir(app_data_dir)
    ts = datetime.now().strftime(_TS_FMT)
    zip_path = bdir / f"visages_{ts}.zip"
    tmp_faces = bdir / f"_tmp_{ts}.db"
    try:
        # Hot-backup de faces.db via l'API de copie SQLite (thread-safe)
        src = sqlite3.connect(str(faces_db_path), check_same_thread=False)
        dst = sqlite3.connect(str(tmp_faces))
        src.backup(dst)
        dst.close()
        src.close()

        # Export des personnes depuis catalog.db
        persons_data: list[dict] = []
        try:
            cat = sqlite3.connect(str(catalog_db_path))
            rows = cat.execute(
                "SELECT id, name, created_at FROM persons"
            ).fetchall()
            cat.close()
            persons_data = [
                {"id": r[0], "name": r[1], "created_at": r[2]} for r in rows
            ]
        except Exception as exc:
            logger.warning("backup: lecture persons impossible: %s", exc)

        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(str(tmp_faces), "faces.db")
            zf.writestr(
                "persons.json",
                json.dumps(persons_data, ensure_ascii=False, indent=2),
            )
    finally:
        if tmp_faces.exists():
            tmp_faces.unlink()

    logger.info("Sauvegarde créée : %s", zip_path.name)
    return zip_path


def restore_backup(
    zip_path: Path,
    faces_db_path: Path,
    catalog_db_path: Path,
    app_data_dir: Path,
) -> None:
    """Restaure faces.db et les personnes à partir d'une sauvegarde ZIP."""
    bdir = _backup_dir(app_data_dir)
    tmp_dir = bdir / "_restore_tmp"
    tmp_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(tmp_dir))

        tmp_faces = tmp_dir / "faces.db"
        if tmp_faces.exists():
            src = sqlite3.connect(str(tmp_faces), check_same_thread=False)
            dst = sqlite3.connect(str(faces_db_path), check_same_thread=False)
            src.backup(dst)
            dst.close()
            src.close()
        else:
            raise FileNotFoundError("faces.db absent de la sauvegarde")

        persons_json = tmp_dir / "persons.json"
        if persons_json.exists():
            persons_data = json.loads(persons_json.read_text(encoding="utf-8"))
            cat = sqlite3.connect(str(catalog_db_path))
            for p in persons_data:
                cat.execute(
                    "INSERT OR REPLACE INTO persons (id, name, created_at)"
                    " VALUES (?, ?, ?)",
                    (p["id"], p["name"], p.get("created_at")),
                )
            cat.commit()
            cat.close()
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

    logger.info("Restauration effectuée depuis : %s", zip_path.name)


# ------------------------------------------------------------------ threads

class _BackupThread(QThread):
    succeeded = Signal(Path)
    failed    = Signal(str)

    def __init__(self, faces_db, catalog_db, app_data_dir, parent=None) -> None:
        super().__init__(parent)
        self._faces_db    = faces_db
        self._catalog_db  = catalog_db
        self._app_data_dir = app_data_dir

    def run(self) -> None:
        try:
            path = create_backup(self._faces_db, self._catalog_db, self._app_data_dir)
            self.succeeded.emit(path)
        except Exception as exc:
            logger.exception("Échec de la sauvegarde")
            self.failed.emit(str(exc))


class _RestoreThread(QThread):
    succeeded = Signal()
    failed    = Signal(str)

    def __init__(self, zip_path, faces_db, catalog_db, app_data_dir, parent=None) -> None:
        super().__init__(parent)
        self._zip_path     = zip_path
        self._faces_db     = faces_db
        self._catalog_db   = catalog_db
        self._app_data_dir = app_data_dir

    def run(self) -> None:
        try:
            restore_backup(
                self._zip_path, self._faces_db,
                self._catalog_db, self._app_data_dir,
            )
            self.succeeded.emit()
        except Exception as exc:
            logger.exception("Échec de la restauration")
            self.failed.emit(str(exc))


# ------------------------------------------------------------------ dialog

_ROW_STYLE = (
    "QWidget#backup_row {"
    "  background: #252525; border: 1px solid #3a3a3a; border-radius: 4px;"
    "}"
)
_BTN_RESTORE = (
    "QPushButton { background: #1a3a5a; color: #7aabdb;"
    " border: 1px solid #2a5a8a; border-radius: 3px;"
    " padding: 3px 10px; font-size: 11px; }"
    "QPushButton:hover { background: #245080; color: #9fcbf5; }"
    "QPushButton:disabled { background: #1e1e1e; color: #444; border-color: #333; }"
)
_BTN_DELETE = (
    "QPushButton { background: transparent; color: #884444;"
    " border: 1px solid #553333; border-radius: 3px;"
    " padding: 3px 8px; font-size: 11px; }"
    "QPushButton:hover { background: #3a1515; color: #cc6666; border-color: #883333; }"
    "QPushButton:disabled { color: #333; border-color: #333; }"
)
_BTN_CREATE = (
    "QPushButton { background: #1a3a2a; color: #5abf7a;"
    " border: 1px solid #2a6a3a; border-radius: 4px;"
    " padding: 5px 16px; font-size: 12px; }"
    "QPushButton:hover { background: #245040; color: #7adf9a; }"
    "QPushButton:disabled { background: #1e1e1e; color: #444; border-color: #333; }"
)


class FaceBackupDialog(QDialog):
    """
    Dialogue de gestion des sauvegardes de reconnaissance faciale.

    Signal
    ------
    restore_completed
        Émis après une restauration réussie pour que MainWindow rafraîchisse l'UI.
    """

    restore_completed = Signal()

    def __init__(
        self,
        app_data_dir: Path,
        faces_db_path: Path,
        catalog_db_path: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._app_data_dir  = app_data_dir
        self._faces_db_path = faces_db_path
        self._catalog_db_path = catalog_db_path
        self._thread: QThread | None = None

        self.setWindowTitle(translate("FaceBackupDialog", "Backups — Face recognition"))
        self.setMinimumSize(500, 340)
        self.setStyleSheet("background: #1e1e1e; color: #ccc;")
        self._build_ui()
        self._refresh_list()

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # Info
        lbl_info = QLabel(
            translate("FaceBackupDialog", "Each backup captures the complete state of the "
                                          "recognition: detected faces, groups, person "
                                          "assignments.")
        )
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(lbl_info)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: #333; border: none;")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        # Liste scrollable
        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_vbox = QVBoxLayout(self._list_container)
        self._list_vbox.setContentsMargins(0, 0, 0, 0)
        self._list_vbox.setSpacing(6)
        self._list_vbox.setAlignment(Qt.AlignTop)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._list_container)
        scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #333; border-radius: 4px; background: #1a1a1a; }"
        )
        scroll.setMinimumHeight(180)
        root.addWidget(scroll, stretch=1)

        # Barre du bas
        bot = QHBoxLayout()
        bot.setSpacing(10)

        self._btn_create = QPushButton(translate("FaceBackupDialog", "＋  Create a backup"))
        self._btn_create.setStyleSheet(_BTN_CREATE)
        self._btn_create.setCursor(Qt.PointingHandCursor)
        self._btn_create.clicked.connect(self._on_create)
        bot.addWidget(self._btn_create)

        bot.addStretch()

        btn_close = QPushButton(translate("FaceBackupDialog", "Close"))
        btn_close.setStyleSheet(
            "QPushButton { background: #2a2a2a; color: #aaa;"
            " border: 1px solid #444; border-radius: 4px; padding: 5px 16px; }"
            "QPushButton:hover { background: #333; color: #ddd; }"
        )
        btn_close.clicked.connect(self.accept)
        bot.addWidget(btn_close)

        root.addLayout(bot)

        # Label de statut (progression)
        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color: #7aabdb; font-size: 11px;")
        self._lbl_status.setAlignment(Qt.AlignCenter)
        self._lbl_status.hide()
        root.addWidget(self._lbl_status)

    # ---------------------------------------------------------------- list

    def _refresh_list(self) -> None:
        while self._list_vbox.count():
            item = self._list_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        backups = list_backups(self._app_data_dir)
        if not backups:
            lbl = QLabel(translate("FaceBackupDialog", "No backup available."))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #555; font-size: 12px; padding: 20px;")
            self._list_vbox.addWidget(lbl)
            return

        for zip_path in backups:
            row = self._make_row(zip_path)
            self._list_vbox.addWidget(row)

    def _make_row(self, zip_path: Path) -> QWidget:
        row = QWidget()
        row.setObjectName("backup_row")
        row.setStyleSheet(_ROW_STYLE)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        h = QHBoxLayout(row)
        h.setContentsMargins(10, 7, 8, 7)
        h.setSpacing(10)

        lbl_date = QLabel(_parse_ts(zip_path))
        lbl_date.setStyleSheet("color: #ccc; font-size: 12px; background: transparent;")
        h.addWidget(lbl_date, stretch=1)

        size = zip_path.stat().st_size
        lbl_size = QLabel(_fmt_size(size))
        lbl_size.setStyleSheet("color: #666; font-size: 11px; background: transparent;")
        lbl_size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lbl_size.setFixedWidth(56)
        h.addWidget(lbl_size)

        btn_restore = QPushButton(translate("FaceBackupDialog", "Restore"))
        btn_restore.setStyleSheet(_BTN_RESTORE)
        btn_restore.setCursor(Qt.PointingHandCursor)
        btn_restore.setFixedWidth(80)
        btn_restore.clicked.connect(lambda _=False, p=zip_path: self._on_restore(p))
        h.addWidget(btn_restore)

        btn_del = QPushButton("✕")
        btn_del.setStyleSheet(_BTN_DELETE)
        btn_del.setCursor(Qt.PointingHandCursor)
        btn_del.setFixedWidth(28)
        btn_del.setToolTip(translate("FaceBackupDialog", "Delete this backup"))
        btn_del.clicked.connect(lambda _=False, p=zip_path, r=row: self._on_delete(p, r))
        h.addWidget(btn_del)

        return row

    # ---------------------------------------------------------------- actions

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        self._btn_create.setEnabled(not busy)
        self._lbl_status.setText(msg)
        self._lbl_status.setVisible(busy)
        # Désactiver tous les boutons Restaurer/Supprimer pendant l'opération
        for i in range(self._list_vbox.count()):
            w = self._list_vbox.itemAt(i).widget()
            if w:
                for btn in w.findChildren(QPushButton):
                    btn.setEnabled(not busy)

    def _on_create(self) -> None:
        self._set_busy(True, "Sauvegarde en cours…")
        self._thread = _BackupThread(
            self._faces_db_path,
            self._catalog_db_path,
            self._app_data_dir,
            self,
        )
        self._thread.succeeded.connect(self._on_backup_done)
        self._thread.failed.connect(self._on_op_error)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot(Path)
    def _on_backup_done(self, path: Path) -> None:
        self._set_busy(False)
        self._refresh_list()
        self._lbl_status.setText(
            translate("FaceBackupDialog", "Backup created: {name}").format(name=path.name))
        self._lbl_status.show()

    def _on_restore(self, zip_path: Path) -> None:
        date_str = _parse_ts(zip_path)
        reply = QMessageBox.question(
            self,
            translate("FaceBackupDialog", "Restore the backup"),
            translate("FaceBackupDialog",
                      "Restore face recognition to this point:\n\n  {date}\n\nThe current "
                      "state will be replaced. This cannot be undone\n(you may want to back up "
                      "the current state first)."
                      ).format(date=date_str),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Ok:
            return

        self._set_busy(True, "Restauration en cours…")
        self._thread = _RestoreThread(
            zip_path,
            self._faces_db_path,
            self._catalog_db_path,
            self._app_data_dir,
            self,
        )
        self._thread.succeeded.connect(self._on_restore_done)
        self._thread.failed.connect(self._on_op_error)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot()
    def _on_restore_done(self) -> None:
        self._set_busy(False)
        self.restore_completed.emit()
        QMessageBox.information(
            self,
            translate("FaceBackupDialog", "Restore successful"),
            translate("FaceBackupDialog", "Face recognition has been restored.\nThe list of "
                                          "people and the assignments are up to date."),
        )
        self.accept()

    def _on_delete(self, zip_path: Path, row: QWidget) -> None:
        reply = QMessageBox.question(
            self,
            translate("FaceBackupDialog", "Delete the backup"),
            translate("FaceBackupDialog", "Send to the recycle bin:\n\n  {name}?"
                      ).format(name=_parse_ts(zip_path)),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from src.library.trash import move_to_trash
            move_to_trash(str(zip_path))
        except Exception as exc:
            QMessageBox.warning(
                self, translate("FaceBackupDialog", "Error"),
                translate("FaceBackupDialog", "Cannot delete:\n{error}")
                .format(error=exc))
            return
        self._list_vbox.removeWidget(row)
        row.deleteLater()
        # Afficher "Aucune sauvegarde" si la liste est vide
        if self._list_vbox.count() == 0:
            self._refresh_list()

    @Slot(str)
    def _on_op_error(self, msg: str) -> None:
        self._set_busy(False)
        QMessageBox.critical(
            self, translate("FaceBackupDialog", "Error"),
            translate("FaceBackupDialog", "The operation failed:\n\n{error}").format(error=msg))
