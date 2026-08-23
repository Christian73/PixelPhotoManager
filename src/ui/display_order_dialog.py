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
from src.core.i18n import translate

# Le thème sombre global ne définit pas QRadioButton::indicator, ce qui rend
# le bouton sélectionné indiscernable du non-sélectionné sur fond foncé
# (même correctif que people_panel.py::_RADIO_STYLE).
_RADIO_STYLE = """
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
"""


class _OrderSection(QGroupBox):
    """Un groupe de deux choix indépendants : mode (alpha/chrono) et
    direction (asc/desc). Les deux paires de boutons radio partagent le même
    QGroupBox parent, ce qui les rendrait mutuellement exclusifs les uns aux
    autres par défaut (Qt groupe automatiquement tous les QRadioButton d'un
    même parent) : un QButtonGroup par paire est nécessaire pour les isoler."""

    def __init__(self, title: str, default_mode: str, default_dir: str, parent=None):
        super().__init__(title, parent)
        self.setStyleSheet(_RADIO_STYLE)
        v = QVBoxLayout(self)

        mode_row = QHBoxLayout()
        self._rb_alpha = QRadioButton(translate("OrderSection", "Alphabetical"))
        self._rb_chrono = QRadioButton(translate("OrderSection", "Chronological"))
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_alpha)
        self._mode_group.addButton(self._rb_chrono)
        mode_row.addWidget(self._rb_alpha)
        mode_row.addWidget(self._rb_chrono)
        v.addLayout(mode_row)

        dir_row = QHBoxLayout()
        self._rb_asc = QRadioButton(translate("OrderSection", "Ascending"))
        self._rb_desc = QRadioButton(translate("OrderSection", "Descending"))
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


class _ChronoAlbumSection(QGroupBox):
    """Direction seule (le mode est toujours chronologique pour cet album,
    donc pas de paire de boutons "mode" ici, contrairement à _OrderSection)."""

    def __init__(self, default_dir: str, parent=None):
        super().__init__(translate("ChronoAlbumSection",
                                   "“Timeline” album (all the photos)"), parent)
        self.setStyleSheet(_RADIO_STYLE)
        v = QVBoxLayout(self)

        dir_row = QHBoxLayout()
        self._rb_asc = QRadioButton(translate("ChronoAlbumSection", "Reverse chronological "
                                                                    "(oldest first)"))
        self._rb_desc = QRadioButton(translate("ChronoAlbumSection", "Chronological (newest "
                                                                     "first)"))
        self._dir_group = QButtonGroup(self)
        self._dir_group.addButton(self._rb_asc)
        self._dir_group.addButton(self._rb_desc)
        dir_row.addWidget(self._rb_desc)
        dir_row.addWidget(self._rb_asc)
        v.addLayout(dir_row)

        (self._rb_desc if default_dir == "desc" else self._rb_asc).setChecked(True)

    def direction(self) -> str:
        return "desc" if self._rb_desc.isChecked() else "asc"


class DisplayOrderDialog(QDialog):
    """Popup de configuration de l'ordre d'affichage des dossiers (sidebar),
    de la grille de photos et de l'album spécial "Chronologie" (toutes les
    photos). Ce dernier reste toujours trié chronologiquement (un tri
    alphabétique n'a pas de sens pour un album qui s'appelle "Chronologie")
    mais dispose de sa propre direction, indépendante de celle de la grille
    de photos standard."""

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle(translate("DisplayOrderDialog", "Display order"))

        layout = QVBoxLayout(self)
        self._folders = _OrderSection(
            translate("DisplayOrderDialog", "Folders"),
            config.get("display_order.folder_mode", "alpha"),
            config.get("display_order.folder_dir", "asc"),
        )
        self._grid = _OrderSection(
            translate("DisplayOrderDialog", "Photo grid"),
            config.get("display_order.grid_mode", "chrono"),
            config.get("display_order.grid_dir", "desc"),
        )
        self._chrono_album = _ChronoAlbumSection(
            config.get(
                "display_order.chrono_album_dir",
                config.get("display_order.grid_dir", "desc"),
            ),
        )
        layout.addWidget(self._folders)
        layout.addWidget(self._grid)
        layout.addWidget(self._chrono_album)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save_to_config(self) -> None:
        self._config.set("display_order.folder_mode", self._folders.mode())
        self._config.set("display_order.folder_dir", self._folders.direction())
        self._config.set("display_order.grid_mode", self._grid.mode())
        self._config.set("display_order.grid_dir", self._grid.direction())
        self._config.set("display_order.chrono_album_dir", self._chrono_album.direction())
        self._config.save()
