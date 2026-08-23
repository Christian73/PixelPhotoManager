# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialogs for exporting and saving the processed image (extracted from
main_window.py). The names prefixed with an underscore are kept for the
history — implementation details of MainWindow."""

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QVBoxLayout, QWidget,
)
from src.core.i18n import translate

# (label, max_total_pixels | None, jpeg_quality, size_hint)
_EXPORT_SIZES = [
    (translate("ExportDialog", "Maximum size — original resolution"),
     None,      95, ""),
    (translate("ExportDialog", "Large  (~4 Mpx)"),
     4_000_000, 98, translate("ExportDialog", "600–1,600 KB")),
    (translate("ExportDialog", "Medium (~2 Mpx)"),
     2_000_000, 94, translate("ExportDialog", "320–800 KB")),
    (translate("ExportDialog", "Small  (~500 kpx)"),
     500_000,   90, translate("ExportDialog", "75–300 KB")),
]


class _ExportDialog(QDialog):
    _DEFAULT_DIR = Path.home() / "Pictures" / "PixelPhotoManager" / "Export"

    def __init__(self, photo_count: int, parent=None):
        super().__init__(parent)
        n = photo_count
        self.setWindowTitle(translate("ExportDialog", "Export %n photo(s)", None, n))
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        # Destination folder
        grp_dir = QGroupBox(translate("ExportDialog", "Destination folder"))
        dir_layout = QHBoxLayout(grp_dir)
        self._dir_edit = QLineEdit(str(self._DEFAULT_DIR))
        dir_layout.addWidget(self._dir_edit)
        btn_browse = QPushButton(translate("ExportDialog", "Browse…"))
        btn_browse.setFixedWidth(90)
        btn_browse.clicked.connect(self._browse)
        dir_layout.addWidget(btn_browse)
        layout.addWidget(grp_dir)

        # Size options
        grp_size = QGroupBox(translate("ExportDialog", "Export size"))
        grp_size.setStyleSheet("""
            QRadioButton::indicator {
                width: 13px; height: 13px;
                border-radius: 7px;
                border: 2px solid #888;
                background: transparent;
            }
            QRadioButton::indicator:checked {
                background: #7aabdb;
                border: 2px solid #7aabdb;
            }
            QRadioButton::indicator:unchecked:hover {
                border-color: #bbb;
            }
        """)
        size_layout = QVBoxLayout(grp_size)
        size_layout.setSpacing(6)
        self._size_radios: list[tuple[QRadioButton, int | None, int]] = []
        btn_group = QButtonGroup(self)   # an exclusive group: only one active at a time
        btn_group.setExclusive(True)
        for i, (label, max_px, quality, size_hint) in enumerate(_EXPORT_SIZES):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            rb = QRadioButton(label)
            rb.setChecked(i == 0)
            btn_group.addButton(rb)
            row_layout.addWidget(rb)

            if size_hint:
                lbl_info = QLabel(
                    translate("ExportDialog", "quality {q}  •  ≈ {hint}").format(
                        q=quality, hint=size_hint))
                lbl_info.setStyleSheet("color: #777; font-size: 10px;")
                row_layout.addWidget(lbl_info)

            row_layout.addStretch()
            size_layout.addWidget(row)
            self._size_radios.append((rb, max_px, quality))
        layout.addWidget(grp_size)

        # Buttons
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText(translate("ExportDialog", "Export"))
        btn_box.button(QDialogButtonBox.Cancel).setText(translate("ExportDialog", "Cancel"))
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, translate("ExportDialog", "Choose the export folder"), self._dir_edit.text()
        )
        if folder:
            self._dir_edit.setText(folder)

    @property
    def export_dir(self) -> Path:
        return Path(self._dir_edit.text().strip())

    @property
    def size_preset(self) -> tuple:
        """Returns (max_total_pixels | None, jpeg_quality)."""
        for rb, max_px, quality in self._size_radios:
            if rb.isChecked():
                return (max_px, quality)
        return (None, 95)  # fallback: maximum size


class _SaveOptionsDialog(QDialog):
    """Dialog for saving the processed image.

    Offers three actions:
    - Overwrite the original file (with the option of a backup in .tmp_originals)
    - Save to another location through the file explorer
    """

    def __init__(self, photo_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate("SaveOptionsDialog", "Save the edited image"))
        self.setMinimumWidth(480)
        self._photo_path = photo_path
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 18, 18, 18)

        # Header
        lbl_name = QLabel(f"<b>{Path(self._photo_path).name}</b>")
        lbl_name.setStyleSheet("font-size: 11px;")
        layout.addWidget(lbl_name)

        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #444;")
        layout.addWidget(sep)
        layout.addSpacing(4)

        # --- Option 1: overwrite ---
        self._rb_overwrite = QRadioButton(translate("SaveOptionsDialog", "Overwrite the "
                                                                         "original file"))
        self._rb_overwrite.setChecked(True)
        layout.addWidget(self._rb_overwrite)

        self._overwrite_details = QWidget()
        od_layout = QVBoxLayout(self._overwrite_details)
        od_layout.setContentsMargins(24, 2, 0, 4)
        od_layout.setSpacing(6)

        lbl_warn = QLabel(
            translate("SaveOptionsDialog", "⚠  This cannot be undone: the original file will "
                                           "be permanently\n    replaced by the processed "
                                           "version.")
        )
        lbl_warn.setStyleSheet("color: #e8a040; font-size: 10px;")
        od_layout.addWidget(lbl_warn)

        self._cb_backup = QCheckBox(
            translate("SaveOptionsDialog", "Copy the original into .tmp_originals before "
                                           "overwriting")
        )
        self._cb_backup.setChecked(True)
        self._cb_backup.setToolTip(
            translate("SaveOptionsDialog", "The original will be copied to:\n{path}").format(
                path=Path(self._photo_path).parent / '.tmp_originals')
        )
        od_layout.addWidget(self._cb_backup)

        layout.addWidget(self._overwrite_details)

        # --- Option 2 : enregistrer ailleurs ---
        self._rb_elsewhere = QRadioButton(translate("SaveOptionsDialog", "Save to another "
                                                                         "location…"))
        layout.addWidget(self._rb_elsewhere)

        layout.addSpacing(8)

        # Enable/disable the warning block according to the selected radio button
        self._rb_overwrite.toggled.connect(self._overwrite_details.setEnabled)

        # Buttons
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText(translate("SaveOptionsDialog", "Save"))
        btn_box.button(QDialogButtonBox.Cancel).setText(translate("SaveOptionsDialog", "Cancel"))
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # --- Results ---

    @property
    def overwrite(self) -> bool:
        return self._rb_overwrite.isChecked()

    @property
    def backup_before_overwrite(self) -> bool:
        return self._cb_backup.isChecked()
