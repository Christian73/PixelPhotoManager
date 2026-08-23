# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""The "Display › Display order…" dialog: choice of the sort mode
(alphabetical/chronological) and of the direction (ascending/descending),
independently for the Folders panel and for the photo grid."""

from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QRadioButton, QVBoxLayout,
)

from src.core.config import Config
from src.core.i18n import translate

# The global dark theme does not define QRadioButton::indicator, which makes
# the selected button indistinguishable from the unselected one on a dark
# background (the same fix as people_panel.py::_RADIO_STYLE).
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
    """One group of two independent choices: mode (alpha/chrono) and
    direction (asc/desc). The two pairs of radio buttons share the same parent
    QGroupBox, which would make them mutually exclusive with one another by
    default (Qt automatically groups every QRadioButton of a same parent): one
    QButtonGroup per pair is needed to isolate them."""

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
    """Direction only (the mode is always chronological for this album, so no
    "mode" pair of buttons here, unlike _OrderSection)."""

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
    """Popup configuring the display order of the folders (sidebar), of the
    photo grid and of the special "Timeline" album (all the photos). The
    latter always stays sorted chronologically (an alphabetical sort makes no
    sense for an album called "Timeline") but has its own direction,
    independent of that of the standard photo grid."""

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
