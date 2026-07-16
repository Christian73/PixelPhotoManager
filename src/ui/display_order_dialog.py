# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialog "Affichage › Ordre d'affichage…" : choix du mode de tri
(alphabétique/chronologique) et de la direction (croissant/décroissant),
indépendamment pour le panneau Dossiers et pour la grille de photos."""

from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QRadioButton, QVBoxLayout,
)

from src.core.config import Config


class _OrderSection(QGroupBox):
    """Un groupe de deux choix indépendants : mode (alpha/chrono) et
    direction (asc/desc). Les deux paires de boutons radio partagent le même
    QGroupBox parent, ce qui les rendrait mutuellement exclusifs les uns aux
    autres par défaut (Qt groupe automatiquement tous les QRadioButton d'un
    même parent) : un QButtonGroup par paire est nécessaire pour les isoler."""

    def __init__(self, title: str, default_mode: str, default_dir: str, parent=None):
        super().__init__(title, parent)
        v = QVBoxLayout(self)

        mode_row = QHBoxLayout()
        self._rb_alpha = QRadioButton("Alphabétique")
        self._rb_chrono = QRadioButton("Chronologique")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_alpha)
        self._mode_group.addButton(self._rb_chrono)
        mode_row.addWidget(self._rb_alpha)
        mode_row.addWidget(self._rb_chrono)
        v.addLayout(mode_row)

        dir_row = QHBoxLayout()
        self._rb_asc = QRadioButton("Croissant")
        self._rb_desc = QRadioButton("Décroissant")
        self._dir_group = QButtonGroup(self)
        self._dir_group.addButton(self._rb_asc)
        self._dir_group.addButton(self._rb_desc)
        dir_row.addWidget(self._rb_asc)
        dir_row.addWidget(self._rb_desc)
        v.addLayout(dir_row)

        (self._rb_chrono if default_mode == "chrono" else self._rb_alpha).setChecked(True)
        (self._rb_desc if default_dir == "desc" else self._rb_asc).setChecked(True)

    def mode(self) -> str:
        return "chrono" if self._rb_chrono.isChecked() else "alpha"

    def direction(self) -> str:
        return "desc" if self._rb_desc.isChecked() else "asc"


class DisplayOrderDialog(QDialog):
    """Popup de configuration de l'ordre d'affichage des dossiers (sidebar)
    et de la grille de photos. La grille de "Chronologie de toutes les
    photos" reste toujours triée chronologiquement (seule la direction s'y
    applique) : ce cas particulier est géré côté MainWindow, pas ici."""

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Ordre d'affichage")

        layout = QVBoxLayout(self)
        self._folders = _OrderSection(
            "Dossiers",
            config.get("display_order.folder_mode", "alpha"),
            config.get("display_order.folder_dir", "asc"),
        )
        self._grid = _OrderSection(
            "Grille de photos",
            config.get("display_order.grid_mode", "chrono"),
            config.get("display_order.grid_dir", "desc"),
        )
        layout.addWidget(self._folders)
        layout.addWidget(self._grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save_to_config(self) -> None:
        self._config.set("display_order.folder_mode", self._folders.mode())
        self._config.set("display_order.folder_dir", self._folders.direction())
        self._config.set("display_order.grid_mode", self._grid.mode())
        self._config.set("display_order.grid_dir", self._grid.direction())
        self._config.save()
