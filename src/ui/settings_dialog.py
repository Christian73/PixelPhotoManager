# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import Config
from src.ui.edit_panel import MarkedSlider

_DEFAULT_THRESHOLD_PCT = 60


class _FaceRecognitionPage(QWidget):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._initial_value: int = 0
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel("Reconnaissance de visages")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #444;")
        layout.addWidget(sep)

        layout.addSpacing(4)

        lbl_title = QLabel("Tolérance de similarité")
        lbl_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl_title)

        lbl_desc = QLabel(
            "Contrôle à quel point deux visages doivent se ressembler pour être\n"
            "placés dans le même groupe. Une valeur plus élevée regroupe davantage\n"
            "de visages ensemble, mais risque de mélanger des personnes différentes."
        )
        lbl_desc.setStyleSheet("color: #aaa; font-size: 11px;")
        lbl_desc.setWordWrap(True)
        layout.addWidget(lbl_desc)

        self._slider = MarkedSlider(Qt.Horizontal, fmt=lambda v: f"{v}%")
        self._slider.setMinimum(25)
        self._slider.setMaximum(70)
        self._slider.setSingleStep(5)
        self._slider.setPageStep(5)
        current = int(round(
            self._config.get("faces.cluster_threshold", _DEFAULT_THRESHOLD_PCT / 100.0) * 100
        ))
        current = max(25, min(70, current))
        self._initial_value = current
        self._slider.setValue(current)
        layout.addWidget(self._slider)

        self._lbl_value = QLabel()
        self._lbl_value.setAlignment(Qt.AlignCenter)
        self._lbl_value.setStyleSheet("color: #ccc; font-size: 11px;")
        layout.addWidget(self._lbl_value)

        self._slider.valueChanged.connect(self._on_slider_changed)
        self._update_value_label(current)

        layout.addStretch()

        lbl_hint = QLabel(
            "Si vous modifiez ce paramètre, les groupes seront recalculés\n"
            "automatiquement lorsque vous fermez cette fenêtre."
        )
        lbl_hint.setStyleSheet("color: #777; font-size: 10px; font-style: italic;")
        lbl_hint.setWordWrap(True)
        layout.addWidget(lbl_hint)

    def _on_slider_changed(self, value: int) -> None:
        self._update_value_label(value)

    def _update_value_label(self, value: int) -> None:
        threshold = value / 100.0
        if value <= 30:
            hint = "— groupes très stricts"
        elif value <= 40:
            hint = "— groupes équilibrés"
        elif value <= 55:
            hint = "— groupes plus larges"
        else:
            hint = "— groupes très larges (peut mélanger des personnes différentes)"
        self._lbl_value.setText(f"{threshold:.2f}  {hint}")

    def apply(self) -> bool:
        new_value = self._slider.value()
        self._config.set("faces.cluster_threshold", new_value / 100.0)
        changed = new_value != self._initial_value
        self._initial_value = new_value
        return changed


class _VideoPlayerPage(QWidget):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel("Lecteur vidéo")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #444;")
        layout.addWidget(sep)

        layout.addSpacing(4)

        lbl_desc = QLabel(
            "Choisissez le lecteur utilisé par le bouton «▶ Ouvrir la vidéo»\n"
            "dans la visionneuse."
        )
        lbl_desc.setStyleSheet("color: #aaa; font-size: 11px;")
        lbl_desc.setWordWrap(True)
        layout.addWidget(lbl_desc)

        self._grp = QButtonGroup(self)

        self._rb_default = QRadioButton("Lecteur par défaut du système")
        self._grp.addButton(self._rb_default, 0)
        layout.addWidget(self._rb_default)

        lbl_default_hint = QLabel(
            "   Utilise l'application associée aux fichiers vidéo dans Windows."
        )
        lbl_default_hint.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(lbl_default_hint)

        layout.addSpacing(8)

        self._rb_custom = QRadioButton("Lecteur personnalisé :")
        self._grp.addButton(self._rb_custom, 1)
        layout.addWidget(self._rb_custom)

        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        path_row.setContentsMargins(20, 0, 0, 0)

        self._edit_path = QLineEdit()
        self._edit_path.setPlaceholderText("Chemin vers l'exécutable…")
        self._edit_path.textChanged.connect(lambda: self._rb_custom.setChecked(True))
        path_row.addWidget(self._edit_path, stretch=1)

        btn_browse = QPushButton("Parcourir…")
        btn_browse.setFixedWidth(90)
        btn_browse.clicked.connect(self._browse)
        path_row.addWidget(btn_browse)

        layout.addLayout(path_row)

        lbl_examples = QLabel(
            "   Exemples : C:\\Program Files\\VLC\\vlc.exe\n"
            "                    C:\\Program Files\\MPC-HC\\mpc-hc64.exe"
        )
        lbl_examples.setStyleSheet("color: #555; font-size: 10px; font-style: italic;")
        layout.addWidget(lbl_examples)

        layout.addStretch()

        # Restaurer la valeur sauvegardée
        saved = self._config.get("video.player_path", "")
        if saved:
            self._rb_custom.setChecked(True)
            self._edit_path.setText(saved)
        else:
            self._rb_default.setChecked(True)

        self._grp.idClicked.connect(self._on_radio_changed)
        self._on_radio_changed(self._grp.checkedId())

    def _on_radio_changed(self, btn_id: int) -> None:
        self._edit_path.setEnabled(btn_id == 1)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choisir le lecteur vidéo",
            self._edit_path.text() or "C:\\Program Files",
            "Exécutables (*.exe);;Tous les fichiers (*)",
        )
        if path:
            self._rb_custom.setChecked(True)
            self._edit_path.setText(path)

    def apply(self) -> None:
        if self._rb_custom.isChecked():
            self._config.set("video.player_path", self._edit_path.text().strip())
        else:
            self._config.set("video.player_path", "")


class SettingsDialog(QDialog):
    recluster_needed = Signal()

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Paramètres")
        self.setMinimumSize(620, 400)
        self._build()

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._category_list = QListWidget()
        self._category_list.setFixedWidth(170)
        self._category_list.setStyleSheet(
            "QListWidget { background: #1e1e1e; border: none; border-right: 1px solid #333; }"
            "QListWidget::item { padding: 10px 16px; color: #ccc; }"
            "QListWidget::item:selected { background: #2d2d2d; color: #fff; border-left: 3px solid #5599cc; }"
        )

        item_faces = QListWidgetItem("Reconnaissance\nde visages")
        item_faces.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._category_list.addItem(item_faces)

        item_video = QListWidgetItem("Lecteur vidéo")
        item_video.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._category_list.addItem(item_video)

        self._category_list.setCurrentRow(0)
        root.addWidget(self._category_list)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        self._stack = QStackedWidget()
        self._page_faces = _FaceRecognitionPage(self._config)
        self._stack.addWidget(self._page_faces)
        self._page_video = _VideoPlayerPage(self._config)
        self._stack.addWidget(self._page_video)
        right.addWidget(self._stack, stretch=1)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333;")
        right.addWidget(sep)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.setContentsMargins(12, 8, 12, 8)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)

        right_w = QWidget()
        right_w.setLayout(right)
        root.addWidget(right_w, stretch=1)

        self._category_list.currentRowChanged.connect(self._stack.setCurrentIndex)

    def _on_accept(self) -> None:
        self._page_video.apply()
        changed = self._page_faces.apply()
        if changed:
            self.recluster_needed.emit()
        self.accept()
