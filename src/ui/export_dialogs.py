# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialogues d'export et d'enregistrement de l'image traitée (extraits de
main_window.py). Les noms préfixés d'un underscore sont conservés pour
l'historique — détails d'implémentation de MainWindow."""

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QVBoxLayout, QWidget,
)

# (label, max_total_pixels | None, jpeg_quality, size_hint)
_EXPORT_SIZES = [
    ("Taille maximale — résolution originale", None,      95, ""),
    ("Grande  (~4 Mpx)",                       4_000_000, 98, "600–1 600 Ko"),
    ("Moyenne (~2 Mpx)",                       2_000_000, 94, "320–800 Ko"),
    ("Petite  (~500 kpx)",                     500_000,   90, "75–300 Ko"),
]


class _ExportDialog(QDialog):
    _DEFAULT_DIR = Path.home() / "Pictures" / "PixelPhotoManager" / "Export"

    def __init__(self, photo_count: int, parent=None):
        super().__init__(parent)
        n = photo_count
        self.setWindowTitle(f"Exporter {n} photo{'s' if n > 1 else ''}")
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        # Dossier de destination
        grp_dir = QGroupBox("Dossier de destination")
        dir_layout = QHBoxLayout(grp_dir)
        self._dir_edit = QLineEdit(str(self._DEFAULT_DIR))
        dir_layout.addWidget(self._dir_edit)
        btn_browse = QPushButton("Parcourir…")
        btn_browse.setFixedWidth(90)
        btn_browse.clicked.connect(self._browse)
        dir_layout.addWidget(btn_browse)
        layout.addWidget(grp_dir)

        # Options de taille
        grp_size = QGroupBox("Taille d'export")
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
        btn_group = QButtonGroup(self)   # groupe exclusif : un seul actif à la fois
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
                lbl_info = QLabel(f"qualité {quality}  •  ≈ {size_hint}")
                lbl_info.setStyleSheet("color: #777; font-size: 10px;")
                row_layout.addWidget(lbl_info)

            row_layout.addStretch()
            size_layout.addWidget(row)
            self._size_radios.append((rb, max_px, quality))
        layout.addWidget(grp_size)

        # Boutons
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Exporter")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier d'export", self._dir_edit.text()
        )
        if folder:
            self._dir_edit.setText(folder)

    @property
    def export_dir(self) -> Path:
        return Path(self._dir_edit.text().strip())

    @property
    def size_preset(self) -> tuple:
        """Retourne (max_total_pixels | None, jpeg_quality)."""
        for rb, max_px, quality in self._size_radios:
            if rb.isChecked():
                return (max_px, quality)
        return (None, 95)  # fallback : taille maximale


class _SaveOptionsDialog(QDialog):
    """Dialogue de sauvegarde de l'image traitée.

    Propose trois actions :
    - Écraser le fichier original (avec option de sauvegarde dans .tmp_originals)
    - Enregistrer à un autre emplacement via l'explorateur
    """

    def __init__(self, photo_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Enregistrer l'image traitée")
        self.setMinimumWidth(480)
        self._photo_path = photo_path
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 18, 18, 18)

        # En-tête
        lbl_name = QLabel(f"<b>{Path(self._photo_path).name}</b>")
        lbl_name.setStyleSheet("font-size: 11px;")
        layout.addWidget(lbl_name)

        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #444;")
        layout.addWidget(sep)
        layout.addSpacing(4)

        # --- Option 1 : écraser ---
        self._rb_overwrite = QRadioButton("Écraser le fichier original")
        self._rb_overwrite.setChecked(True)
        layout.addWidget(self._rb_overwrite)

        self._overwrite_details = QWidget()
        od_layout = QVBoxLayout(self._overwrite_details)
        od_layout.setContentsMargins(24, 2, 0, 4)
        od_layout.setSpacing(6)

        lbl_warn = QLabel(
            "⚠  Cette action est irréversible : le fichier original sera définitivement\n"
            "    remplacé par la version traitée."
        )
        lbl_warn.setStyleSheet("color: #e8a040; font-size: 10px;")
        od_layout.addWidget(lbl_warn)

        self._cb_backup = QCheckBox(
            "Copier l'original dans .tmp_originals avant l'écrasement"
        )
        self._cb_backup.setChecked(True)
        self._cb_backup.setToolTip(
            f"L'original sera copié dans :\n"
            f"{Path(self._photo_path).parent / '.tmp_originals'}"
        )
        od_layout.addWidget(self._cb_backup)

        layout.addWidget(self._overwrite_details)

        # --- Option 2 : enregistrer ailleurs ---
        self._rb_elsewhere = QRadioButton("Enregistrer à un autre emplacement…")
        layout.addWidget(self._rb_elsewhere)

        layout.addSpacing(8)

        # Activer/désactiver le bloc d'avertissement selon la radio sélectionnée
        self._rb_overwrite.toggled.connect(self._overwrite_details.setEnabled)

        # Boutons
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Enregistrer")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # --- Résultats ---

    @property
    def overwrite(self) -> bool:
        return self._rb_overwrite.isChecked()

    @property
    def backup_before_overwrite(self) -> bool:
        return self._cb_backup.isChecked()
