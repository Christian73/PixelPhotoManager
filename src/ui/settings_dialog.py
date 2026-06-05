from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import Config

_DEFAULT_THRESHOLD_PCT = 40   # 0.40 cosine distance — valeur par défaut


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

        slider_row = QHBoxLayout()
        lbl_low = QLabel("Strict")
        lbl_low.setStyleSheet("color: #888; font-size: 10px;")
        slider_row.addWidget(lbl_low)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setMinimum(25)
        self._slider.setMaximum(70)
        self._slider.setSingleStep(5)
        self._slider.setPageStep(5)
        self._slider.setTickPosition(QSlider.TicksBelow)
        self._slider.setTickInterval(5)
        current = int(round(
            self._config.get("faces.cluster_threshold", _DEFAULT_THRESHOLD_PCT / 100.0) * 100
        ))
        current = max(25, min(70, current))
        self._initial_value = current
        self._slider.setValue(current)
        slider_row.addWidget(self._slider, stretch=1)

        lbl_high = QLabel("Large")
        lbl_high.setStyleSheet("color: #888; font-size: 10px;")
        slider_row.addWidget(lbl_high)

        layout.addLayout(slider_row)

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
        """Sauvegarde le paramètre. Retourne True si la valeur a changé."""
        new_value = self._slider.value()
        self._config.set("faces.cluster_threshold", new_value / 100.0)
        changed = new_value != self._initial_value
        self._initial_value = new_value
        return changed


class SettingsDialog(QDialog):
    recluster_needed = Signal()

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Paramètres")
        self.setMinimumSize(620, 380)
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
        self._category_list.setCurrentRow(0)
        root.addWidget(self._category_list)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        self._stack = QStackedWidget()
        self._page_faces = _FaceRecognitionPage(self._config)
        self._stack.addWidget(self._page_faces)
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
        changed = self._page_faces.apply()
        if changed:
            self.recluster_needed.emit()
        self.accept()
