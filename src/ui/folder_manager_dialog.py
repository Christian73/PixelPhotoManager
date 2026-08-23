# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialog managing the folders watched by the scan."""

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from src.core.config import Config
from src.library.catalog import Catalog


# ---------------------------------------------------------------------------
# Helpers

# An implementation shared with the scanner and the watcher (cf. fs_utils):
# the exclusion rules shown here must stay identical to those of the scan.
from src.library.fs_utils import is_hidden_path as _is_hidden  # noqa: E402
from src.core.i18n import translate


def _find_subdirs(folder: str) -> list[tuple[str, bool, str]]:
    """
    Returns every direct subfolder: (name, is_excluded, reason).
    is_excluded=True  → folder ignored by the scan.
    is_excluded=False → folder included in the scan.
    """
    result = []
    try:
        for name in sorted(os.listdir(folder), key=str.lower):
            fullpath = os.path.join(folder, name)
            if not os.path.isdir(fullpath):
                continue
            if name == "Originals":
                result.append((name, True,
                               translate("FolderManagerDialog", "Picasa backup")))
            elif _is_hidden(fullpath):
                result.append((name, True,
                               translate("FolderManagerDialog", "hidden folder")))
            else:
                result.append((name, False, ""))
    except PermissionError:
        pass
    return result


# ---------------------------------------------------------------------------
# One widget per folder

class _FolderRow(QWidget):
    rescan_clicked = Signal(str)
    remove_clicked = Signal(str)

    def __init__(self, folder: str, photo_count: int, parent=None):
        super().__init__(parent)
        self._folder = folder
        self._subdir_panel: QWidget | None = None
        self._toggle_btn: QPushButton | None = None
        self._setup_ui(folder, photo_count)

    def _setup_ui(self, folder: str, photo_count: int) -> None:
        self.setStyleSheet(
            "background: #252525; border-radius: 4px;"
            "border: 1px solid #333;"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        # Main row
        top = QHBoxLayout()
        top.setSpacing(8)

        exists = os.path.isdir(folder)
        status_lbl = QLabel("✓" if exists else "✗")
        status_lbl.setStyleSheet(
            "color: #5c5; font-size: 14px; background: transparent; border: none;"
            if exists else
            "color: #c55; font-size: 14px; background: transparent; border: none;"
        )
        status_lbl.setFixedWidth(18)
        top.addWidget(status_lbl)

        path_lbl = QLabel(folder)
        path_lbl.setStyleSheet("color: #ddd; font-size: 12px; background: transparent; border: none;")
        path_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        path_lbl.setToolTip(folder)
        top.addWidget(path_lbl, stretch=1)

        count_lbl = QLabel(translate("FolderManagerDialog", "{n} files").format(
            n=f"{photo_count:,}".replace(",", " ")))
        count_lbl.setStyleSheet("color: #888; font-size: 11px; min-width: 90px; background: transparent; border: none;")
        count_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(count_lbl)

        if exists:
            btn_rescan = QPushButton(translate("FolderRow", "⟳  Rescan"))
            btn_rescan.setToolTip(
                translate("FolderRow", "Force a full rescan of this folder.\nEvery file is "
                                       "read again, even if unchanged since the last scan.")
            )
            btn_rescan.setStyleSheet(
                "QPushButton { background: #2a5080; color: white; border: none;"
                " border-radius: 3px; padding: 3px 10px; }"
                "QPushButton:hover { background: #3a6090; }"
            )
            btn_rescan.clicked.connect(lambda: self.rescan_clicked.emit(self._folder))
            top.addWidget(btn_rescan)

        btn_remove = QPushButton(translate("FolderRow", "Remove"))
        btn_remove.setToolTip(translate("FolderRow", "Stop watching this folder"))
        btn_remove.setStyleSheet(
            "QPushButton { background: #4a2222; color: #daa; border: none;"
            " border-radius: 3px; padding: 3px 10px; }"
            "QPushButton:hover { background: #6a3333; }"
        )
        btn_remove.clicked.connect(lambda: self.remove_clicked.emit(self._folder))
        top.addWidget(btn_remove)

        outer.addLayout(top)

        if not exists:
            warn = QLabel(translate("FolderRow", "  This folder cannot be found on the disk."))
            warn.setStyleSheet("color: #a66; font-size: 10px; background: transparent; border: none;")
            outer.addWidget(warn)
            return

        # Subfolders
        subdirs = _find_subdirs(folder)
        if not subdirs:
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333; margin-left: 20px; border: none; border-top: 1px solid #333;")
        outer.addWidget(sep)

        excluded = sum(1 for _, exc, _ in subdirs if exc)
        total = len(subdirs)
        label = "▶  " + translate("FolderManagerDialog", "%n subfolder(s)", None, total)
        if excluded:
            label += "  " + translate("FolderManagerDialog", "(%n excluded)", None, excluded)

        self._toggle_btn = QPushButton(label)
        self._toggle_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #888; border: none;"
            " font-size: 10px; padding: 2px 0px; text-align: left; }"
            "QPushButton:hover { color: #bbb; }"
        )
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle_subdirs)
        outer.addWidget(self._toggle_btn)

        # Subfolder panel (hidden by default)
        self._subdir_panel = QWidget()
        self._subdir_panel.setStyleSheet("background: transparent; border: none;")
        panel_layout = QVBoxLayout(self._subdir_panel)
        panel_layout.setContentsMargins(20, 2, 0, 2)
        panel_layout.setSpacing(2)

        for name, is_excluded, reason in subdirs:
            row_w = QHBoxLayout()
            row_w.setSpacing(6)
            row_w.setContentsMargins(0, 0, 0, 0)

            if is_excluded:
                icon = QLabel("✗")
                icon.setStyleSheet("color: #844; font-size: 11px; background: transparent; border: none;")
                name_lbl = QLabel(name)
                name_lbl.setStyleSheet("color: #666; font-size: 11px; background: transparent; border: none;")
                reason_lbl = QLabel(f"— {reason}")
                reason_lbl.setStyleSheet("color: #555; font-size: 10px; background: transparent; border: none;")
                row_w.addWidget(icon)
                row_w.addWidget(name_lbl)
                row_w.addWidget(reason_lbl)
            else:
                icon = QLabel("✓")
                icon.setStyleSheet("color: #484; font-size: 11px; background: transparent; border: none;")
                name_lbl = QLabel(name)
                name_lbl.setStyleSheet("color: #999; font-size: 11px; background: transparent; border: none;")
                row_w.addWidget(icon)
                row_w.addWidget(name_lbl)

            row_w.addStretch()
            panel_layout.addLayout(row_w)

        self._subdir_panel.setVisible(False)
        outer.addWidget(self._subdir_panel)

    def _toggle_subdirs(self) -> None:
        if self._subdir_panel is None or self._toggle_btn is None:
            return
        visible = not self._subdir_panel.isVisible()
        self._subdir_panel.setVisible(visible)
        # Updates the arrow of the button
        text = self._toggle_btn.text()
        if visible:
            self._toggle_btn.setText(text.replace("▶", "▼", 1))
        else:
            self._toggle_btn.setText(text.replace("▼", "▶", 1))
        # Forces the size of the parent widget to be recomputed
        self.adjustSize()
        if self.parent():
            self.parent().adjustSize()


# ---------------------------------------------------------------------------
# Dialog principal

class FolderManagerDialog(QDialog):
    rescan_requested = Signal(str)   # path of the folder to rescan (forced)
    folder_removed   = Signal(str)   # folder removed
    folder_added     = Signal(str)   # folder added

    def __init__(self, config: Config, catalog: Catalog, parent=None):
        super().__init__(parent)
        self._config  = config
        self._catalog = catalog
        self.setWindowTitle(translate("FolderManagerDialog", "Watched folder management"))
        self.setMinimumWidth(640)
        self.setMinimumHeight(420)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 14)

        lbl = QLabel(translate("FolderManagerDialog", "Folders watched by the scan"))
        lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #ccc;")
        root.addWidget(lbl)

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

        note = QLabel(
            translate("FolderManagerDialog", "Exclusion rules: folders hidden by Windows "
                                             "(“Hidden” attribute or “.” prefix) and folders "
                                             "named “Originals” (Picasa backups).")
        )
        note.setStyleSheet("color: #555; font-size: 10px;")
        note.setWordWrap(True)
        root.addWidget(note)

        btn_row = QHBoxLayout()
        btn_add = QPushButton(translate("FolderManagerDialog", "＋  Add a folder…"))
        btn_add.setStyleSheet(
            "QPushButton { background: #2a3d2a; color: #8d8; border: none;"
            " border-radius: 3px; padding: 4px 12px; }"
            "QPushButton:hover { background: #3a4d3a; }"
        )
        btn_add.clicked.connect(self._on_add)
        btn_row.addWidget(btn_add)
        btn_row.addStretch()
        btn_close = QPushButton(translate("FolderManagerDialog", "Close"))
        btn_close.setFixedWidth(80)
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        self._refresh()

    def _refresh(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        folders = self._config.get_scan_folders()
        if not folders:
            empty = QLabel(translate("FolderManagerDialog", "No folder configured. Click “Add” "
                                                            "to get started."))
            empty.setStyleSheet("color: #555; font-size: 11px; padding: 24px;")
            empty.setAlignment(Qt.AlignCenter)
            self._layout.insertWidget(0, empty)
            return

        for i, folder in enumerate(folders):
            count = self._catalog.count_photos_in_folder(folder)
            row = _FolderRow(folder, count, self._container)
            row.rescan_clicked.connect(self._on_rescan)
            row.remove_clicked.connect(self._on_remove)
            self._layout.insertWidget(i, row)

    def _on_rescan(self, folder: str) -> None:
        self.rescan_requested.emit(folder)
        QMessageBox.information(
            self,
            translate("FolderManagerDialog", "Rescan started"),
            translate("FolderManagerDialog",
                      "The forced rescan of “{name}” is running.\nEvery file will be read "
                      "again, even the unchanged ones."
                      ).format(name=Path(folder).name),
        )

    def _on_remove(self, folder: str) -> None:
        # The confirmation (with the number of photos concerned) is handled on the
        # MainWindow._on_folder_removed side, which also performs the purge.
        self.folder_removed.emit(folder)
        self._refresh()

    def _on_add(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, translate("FolderManagerDialog", "Choose a folder to watch"), os.path.expanduser("~")
        )
        if folder:
            self.folder_added.emit(folder)
            self._refresh()
