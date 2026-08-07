# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import copy
import logging
import math
import os

from PySide6.QtCore import Qt, Signal, Slot, QSize, QPoint, QTimer
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QFont, QPen, QIcon,
    QPolygon, QBrush, QLinearGradient, QRadialGradient, QPainterPath, QKeySequence,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QScrollArea, QGroupBox, QDialog,
    QDialogButtonBox, QToolButton, QGridLayout, QSizePolicy,
    QCheckBox, QStyle, QStyleOptionSlider,
    QButtonGroup, QColorDialog, QFontComboBox, QSpinBox, QDoubleSpinBox,
)

from src.core.i18n import translate
from src.core.models import PhotoInfo, EditInfo
from src.processing.adjustments import ImageAdjuster
from src.processing.edit_database import EditDatabase

logger = logging.getLogger(__name__)

_UNDO_MAX = 20

# Noms d'affichage pour les opérations stockées en DB (undo/redo persistant)
_OP_LABELS: dict[str, str] = {
    "rotation":           translate("EditPanel", "Rotation"),
    "flip_h":             translate("EditPanel", "Miroir H"),
    "flip_v":             translate("EditPanel", "Miroir V"),
    "crop":               translate("EditPanel", "Recadrage"),
    "red_eye":            translate("EditPanel", "Yeux rouges"),
    "red_eye_clear":      translate("EditPanel", "Effacer yeux rouges"),
    "annotation":         translate("EditPanel", "Annotation"),
    "annotation_delete":  translate("EditPanel", "Supprimer annotation"),
    "annotation_clear":   translate("EditPanel", "Effacer annotations"),
    "annotation_move":    translate("EditPanel", "Déplacer annotation"),
    "annotation_move_multi": translate("EditPanel", "Déplacer annotations"),
    "annotation_delete_multi": translate("EditPanel", "Supprimer annotations"),
    "annotation_resize":  translate("EditPanel", "Redimensionner annotation"),
    "annotation_style":   translate("EditPanel", "Modifier le style"),
    "annotation_group":   translate("EditPanel", "Grouper les annotations"),
    "annotation_ungroup": translate("EditPanel", "Dégrouper les annotations"),
    "undo":               translate("EditPanel", "Annuler"),
    "redo":               translate("EditPanel", "Rétablir"),
    "restore_all":        translate("EditPanel", "Remise en place des retouches"),
    "picasa_before":      translate("EditPanel", "Avant import"),
    "picasa_rotate":      translate("EditPanel", "Rotation"),
    "picasa_crop":        translate("EditPanel", "Recadrage"),
    "picasa_bw":          translate("EditPanel", "Noir et blanc"),
    "picasa_tilt":        translate("EditPanel", "Redressement"),
    "picasa_finetune2":   translate("EditPanel", "Réglages fins"),
    "picasa_fill":        translate("EditPanel", "Lumière"),
    "picasa_warmth":      translate("EditPanel", "Chaleur"),
    "picasa_lumi":        translate("EditPanel", "Luminosité"),
    "picasa_autolight":   translate("EditPanel", "Auto-éclairage"),
    "picasa_sat":         translate("EditPanel", "Saturation"),
    "picasa_anisotropic": translate("EditPanel", "Netteté"),
    "picasa_sharpen":     translate("EditPanel", "Netteté"),
    "picasa_softfocus":   translate("EditPanel", "Adoucissement"),
}


# Noms d'affichage des outils. Comme pour _OP_LABELS, la clé est le nom INTERNE
# de l'outil (1er élément de _TREATMENTS, cf. treatment_dialogs.py) : il reste en
# français en toutes langues parce qu'il sert d'identifiant — clé de
# _treatment_buttons, discriminant de _open_treatment, et nom d'opération
# persisté dans edits.db (historique undo/redo cross-session). Ne jamais
# traduire ces chaînes ailleurs qu'ici, sous peine de casser l'aiguillage.
_TOOL_LABELS: dict[str, str] = {
    "Luminosité": translate("EditPanel", "Luminosité"),
    "Contraste":  translate("EditPanel", "Contraste"),
    "Couleurs":   translate("EditPanel", "Couleurs"),
    "Vignette":   translate("EditPanel", "Vignette"),
    "Cadre":      translate("EditPanel", "Cadre"),
    "Redresser":  translate("EditPanel", "Redresser"),
}


def _tool_label(name: str) -> str:
    """Libellé affiché d'un outil, à partir de son nom interne."""
    return _TOOL_LABELS.get(name, name)


def _op_label(op: str) -> str:
    if op in _OP_LABELS:
        return _OP_LABELS[op]
    if op in _TOOL_LABELS:
        return _TOOL_LABELS[op]
    return op.replace("_", " ").capitalize()


# ------------------------------------------------------------------ icônes

# Icônes dessinées par code : regroupées dans edit_icons.py (2026-07).
from src.ui.edit_icons import (  # noqa: E402,F401
    _ANNOTATION_TOOL_BTN_STYLE, _ICON_SIZE, _base_pixmap,
    _icon_ann_curve,
    _icon_ann_ellipse,
    _icon_ann_line,
    _icon_ann_pen,
    _icon_ann_rect,
    _icon_ann_select,
    _icon_ann_text,
    _icon_brightness,
    _icon_contrast,
    _icon_crop,
    _icon_flip_h,
    _icon_flip_v,
    _icon_gamma,
    _icon_red_eye,
    _icon_saturation,
    _icon_straighten,
    _icon_vignette,
)



# ------------------------------------------------------------------ repères de curseur


# ------------------------------------------------------------------ classes extraites
# (2026-07) Curseurs et dialogues de traitement déplacés dans leurs modules ;
# ré-exportés ici sous leurs noms historiques (main_window, settings_dialog et
# les tests importent MarkedSlider/EditSlider depuis edit_panel).
from src.ui.edit_sliders import EditSlider, MarkedSlider, _Ruler  # noqa: E402,F401
from src.ui.treatment_dialogs import (  # noqa: E402,F401
    _ACTIVE_TOOL_STYLE, _TREATMENTS, CouleursTreatmentDialog, GammaCurveWidget,
    LuminositeTreatmentDialog, TreatmentDialog, VignetteTreatmentDialog,
)


class EditPanel(QWidget):
    edits_changed           = Signal(object)       # EditInfo
    crop_mode_requested     = Signal()
    crop_confirm_requested  = Signal()             # un autre outil a été sélectionné pendant un recadrage en cours
    grid_visibility_changed = Signal(bool)
    photo_saved             = Signal(str, object)  # (photo_path, EditInfo) — uniquement lors d'un enregistrement réel
    rotation_stepped        = Signal(str, int)     # (photo_path, new_rotation_degrees) — émis uniquement pour les rotations 90°
    red_eye_mode_requested  = Signal(bool, float)  # (active, radius) — bascule le mode yeux rouges dans le canvas
    wb_pick_requested       = Signal(bool)         # True = démarrer la pipette, False = annuler
    vignette_edit_mode      = Signal(bool, object) # (active: bool, edit: EditInfo)
    annotation_mode_requested            = Signal(bool, str)   # (active, tool) — bascule le mode annotation dans le canvas
    annotation_style_changed             = Signal(str, float, str, float, bool, bool, str, float, float)
    # (color_argb, width, font_family, font_size, bold, italic, fill_color_argb, opacity, blur)
    annotation_delete_selected_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._photo: PhotoInfo | None = None
        self._edit = EditInfo()
        self._undo_stack: list[EditInfo] = []
        self._redo_stack: list[EditInfo] = []
        # path normalisé -> (état avant reset_all(), pile d'undo d'avant le reset)
        self._reset_snapshots: dict[str, tuple] = {}
        self._db = EditDatabase()
        self._red_eye_active = False
        self._annotation_active = False
        self._crop_active = False
        self._annotation_tool = "pen"
        self._annotation_color = QColor("#ffff0000")
        self._annotation_fill_color = QColor("#ffff0000")
        self._annotation_opacity = 0.4
        self._annotation_blur = 0.0
        self._annotation_selected_ids: set = set()
        self._active_color_dlg: "CouleursTreatmentDialog | None" = None
        self._active_vignette_dlg: "VignetteTreatmentDialog | None" = None
        self._active_frame_dlg: "QDialog | None" = None
        self._active_generic_dlg: "QDialog | None" = None    # Luminosité/Contraste/Redresser… non modal
        self._active_generic_dlg_title: "str | None" = None
        self._treatment_buttons: dict = {}   # nom de traitement -> QToolButton (surbrillance active/inactive)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # Barre titre
        self._title_label = QLabel(translate("EditPanel", "Retouche"))
        self._title_label.setStyleSheet("font-weight: bold;")
        root.addWidget(self._title_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(8)
        inner_layout.setContentsMargins(2, 2, 2, 2)

        # Grille de boutons de traitement
        lbl_corrections = QLabel(translate("EditPanel", "Corrections"))
        lbl_corrections.setStyleSheet("color: #aaa; font-size: 10px;")
        inner_layout.addWidget(lbl_corrections)

        grid = QGridLayout()
        grid.setSpacing(4)
        for idx, (name, icon_fn, sliders_def) in enumerate(_TREATMENTS):
            btn = self._make_treatment_button(name, icon_fn(), sliders_def)
            self._treatment_buttons[name] = btn
            grid.addWidget(btn, idx // 2, idx % 2)
        # Les deux boutons suivants poursuivent le remplissage de la grille :
        # leur position dépend du nombre de traitements (une case en dur
        # écraserait le dernier bouton dès qu'un traitement est ajouté).
        _next = len(_TREATMENTS)

        # Bouton Yeux rouges
        self._btn_red_eye = QToolButton()
        self._btn_red_eye.setText(translate("EditPanel", "Yeux rouges"))
        self._btn_red_eye.setIcon(QIcon(_icon_red_eye()))
        self._btn_red_eye.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._btn_red_eye.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self._btn_red_eye.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._btn_red_eye.setFixedHeight(_ICON_SIZE + 28)
        self._btn_red_eye.setToolTip(translate("EditPanel", "Corriger les yeux rouges — cliquez sur chaque œil"))
        self._btn_red_eye.setCheckable(True)
        self._btn_red_eye.clicked.connect(self._toggle_red_eye_mode)
        grid.addWidget(self._btn_red_eye, _next // 2, _next % 2)

        # Bouton Annotations
        self._btn_annotations = QToolButton()
        self._btn_annotations.setText(translate("EditPanel", "Annotations"))
        self._btn_annotations.setIcon(QIcon(_icon_ann_pen()))
        self._btn_annotations.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._btn_annotations.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self._btn_annotations.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._btn_annotations.setFixedHeight(_ICON_SIZE + 28)
        self._btn_annotations.setToolTip(translate("EditPanel", "Dessiner / écrire par-dessus la photo (calque séparé)"))
        self._btn_annotations.setCheckable(True)
        self._btn_annotations.clicked.connect(self._toggle_annotation_mode)
        grid.addWidget(self._btn_annotations, (_next + 1) // 2, (_next + 1) % 2)
        inner_layout.addLayout(grid)

        # Panneau de contrôle yeux rouges (masqué hors mode)
        self._red_eye_panel = QGroupBox(translate("EditPanel", "Correction yeux rouges"))
        re_layout = QVBoxLayout(self._red_eye_panel)
        re_layout.setContentsMargins(6, 8, 6, 6)
        re_layout.setSpacing(4)

        lbl_instr = QLabel(translate("EditPanel", "Cliquez sur chaque œil rouge dans la photo"))
        lbl_instr.setStyleSheet("color: #bbb; font-size: 10px;")
        lbl_instr.setAlignment(Qt.AlignCenter)
        lbl_instr.setWordWrap(True)
        re_layout.addWidget(lbl_instr)

        radius_row = QHBoxLayout()
        radius_row.addWidget(QLabel(translate("EditPanel", "Taille :")))
        self._red_eye_slider = MarkedSlider(Qt.Horizontal, fmt=lambda v: f"{v/10:.1f}%")
        self._red_eye_slider.setRange(5, 80)   # 0.5% – 8% de la plus petite dimension
        self._red_eye_slider.setValue(30)       # défaut : 3%
        self._red_eye_slider.setToolTip(translate("EditPanel", "Rayon de correction (% de l'image)"))
        self._red_eye_slider.valueChanged.connect(self._on_red_eye_radius_changed)
        radius_row.addWidget(self._red_eye_slider)
        re_layout.addLayout(radius_row)

        re_btns = QHBoxLayout()
        re_btns.setSpacing(4)
        btn_clear_re = QPushButton(translate("EditPanel", "Effacer tout"))
        btn_clear_re.setToolTip(translate("EditPanel", "Supprimer toutes les corrections yeux rouges"))
        btn_clear_re.clicked.connect(self._clear_red_eye)
        re_btns.addWidget(btn_clear_re)
        btn_done_re = QPushButton(translate("EditPanel", "Terminé"))
        btn_done_re.setToolTip(translate("EditPanel", "Quitter le mode yeux rouges  (Echap)"))
        btn_done_re.clicked.connect(self._done_red_eye)
        re_btns.addWidget(btn_done_re)
        re_layout.addLayout(re_btns)

        self._red_eye_panel.hide()
        inner_layout.addWidget(self._red_eye_panel)

        # Panneau de contrôle du calque d'annotations (masqué hors mode)
        self._annotation_panel = QGroupBox(translate("EditPanel", "Annotations"))
        an_layout = QVBoxLayout(self._annotation_panel)
        an_layout.setContentsMargins(6, 8, 6, 6)
        an_layout.setSpacing(4)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(4)
        self._annotation_tool_group = QButtonGroup(self)
        self._annotation_tool_group.setExclusive(True)
        self._annotation_tool_buttons: dict[str, QToolButton] = {}
        for tool, icon_fn, tip in [
            ("pen",     _icon_ann_pen,     translate("EditPanel", "Stylo — trait libre")),
            ("line",    _icon_ann_line,    translate("EditPanel", "Ligne droite")),
            ("curve",   _icon_ann_curve,   translate(
                "EditPanel",
                "Courbe — cliquez les points de passage, double-clic pour valider")),
            ("rect",    _icon_ann_rect,    translate("EditPanel", "Rectangle")),
            ("ellipse", _icon_ann_ellipse, translate("EditPanel", "Ellipse")),
            ("text",    _icon_ann_text,    translate("EditPanel", "Texte")),
            ("select",  _icon_ann_select,  translate(
                "EditPanel",
                "Sélection — cliquez un élément pour le sélectionner")),
        ]:
            btn = QToolButton()
            btn.setIcon(QIcon(icon_fn()))
            btn.setIconSize(QSize(24, 24))
            btn.setStyleSheet(_ANNOTATION_TOOL_BTN_STYLE)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda checked, t=tool: self._set_annotation_tool(t))
            self._annotation_tool_group.addButton(btn)
            self._annotation_tool_buttons[tool] = btn
            tools_row.addWidget(btn)
        self._annotation_tool_buttons["pen"].setChecked(True)
        an_layout.addLayout(tools_row)

        style_row = QHBoxLayout()
        style_row.setSpacing(4)
        self._btn_annotation_color = QPushButton()
        self._btn_annotation_color.setFixedSize(28, 28)
        self._btn_annotation_color.setToolTip(translate("EditPanel", "Couleur"))
        self._btn_annotation_color.clicked.connect(self._pick_annotation_color)
        style_row.addWidget(self._btn_annotation_color)
        style_row.addWidget(QLabel(translate("EditPanel", "Épaisseur")))
        self._annotation_width_spin = QDoubleSpinBox()
        self._annotation_width_spin.setRange(0.0, 4.0)   # % de la plus petite dimension — 0 = pas de contour
        self._annotation_width_spin.setSingleStep(0.1)
        self._annotation_width_spin.setDecimals(1)
        self._annotation_width_spin.setValue(0.6)         # défaut : 0.6%
        self._annotation_width_spin.setSuffix(" %")
        self._annotation_width_spin.setToolTip(translate("EditPanel", "Épaisseur du trait (% de l'image)"))
        self._annotation_width_spin.valueChanged.connect(self._on_annotation_style_changed)
        style_row.addWidget(self._annotation_width_spin, stretch=1)
        self._annotation_style_row = QWidget()
        self._annotation_style_row.setLayout(style_row)
        an_layout.addWidget(self._annotation_style_row)

        shape_row = QHBoxLayout()
        shape_row.setSpacing(14)
        self._btn_annotation_fill_color = QPushButton()
        self._btn_annotation_fill_color.setFixedSize(28, 28)
        self._btn_annotation_fill_color.setToolTip(translate("EditPanel", "Couleur de la surface"))
        self._btn_annotation_fill_color.clicked.connect(self._pick_annotation_fill_color)
        shape_row.addWidget(self._btn_annotation_fill_color)

        opacity_pair = QHBoxLayout()
        opacity_pair.setSpacing(4)
        opacity_pair.addWidget(QLabel(translate("EditPanel", "Opacité")))
        self._annotation_opacity_spin = QDoubleSpinBox()
        self._annotation_opacity_spin.setRange(0.0, 100.0)
        self._annotation_opacity_spin.setSingleStep(5.0)
        self._annotation_opacity_spin.setDecimals(0)
        self._annotation_opacity_spin.setValue(40.0)
        self._annotation_opacity_spin.setSuffix(" %")
        self._annotation_opacity_spin.setToolTip(
            translate("EditPanel", "Opacité de la surface — à 100 %, la photo derrière n'est plus visible"))
        self._annotation_opacity_spin.valueChanged.connect(self._on_annotation_style_changed)
        opacity_pair.addWidget(self._annotation_opacity_spin)
        shape_row.addLayout(opacity_pair)

        blur_pair = QHBoxLayout()
        blur_pair.setSpacing(4)
        blur_pair.addWidget(QLabel(translate("EditPanel", "Flou")))
        self._annotation_blur_spin = QDoubleSpinBox()
        self._annotation_blur_spin.setRange(0.0, 10.0)   # % de la plus petite dimension
        self._annotation_blur_spin.setSingleStep(0.5)
        self._annotation_blur_spin.setDecimals(1)
        self._annotation_blur_spin.setValue(0.0)
        self._annotation_blur_spin.setSuffix(" %")
        self._annotation_blur_spin.setToolTip(translate("EditPanel", "Flou de la photo sous la surface (% de l'image)"))
        self._annotation_blur_spin.valueChanged.connect(self._on_annotation_style_changed)
        blur_pair.addWidget(self._annotation_blur_spin)
        shape_row.addLayout(blur_pair)

        shape_row.addStretch(1)
        self._annotation_shape_row = QWidget()
        self._annotation_shape_row.setLayout(shape_row)
        an_layout.addWidget(self._annotation_shape_row)

        font_row = QHBoxLayout()
        font_row.setSpacing(4)
        self._annotation_font_combo = QFontComboBox()
        self._annotation_font_combo.setCurrentFont(QFont("Arial"))
        self._annotation_font_combo.currentFontChanged.connect(self._on_annotation_style_changed)
        font_row.addWidget(self._annotation_font_combo, stretch=1)
        self._annotation_font_size = QSpinBox()
        self._annotation_font_size.setRange(1, 20)   # % de la plus petite dimension
        self._annotation_font_size.setValue(4)
        self._annotation_font_size.setSuffix(" %")
        self._annotation_font_size.setToolTip(translate("EditPanel", "Taille du texte (% de l'image)"))
        self._annotation_font_size.valueChanged.connect(self._on_annotation_style_changed)
        font_row.addWidget(self._annotation_font_size)
        self._annotation_font_row = QWidget()
        self._annotation_font_row.setLayout(font_row)
        an_layout.addWidget(self._annotation_font_row)

        bi_row = QHBoxLayout()
        bi_row.setSpacing(4)
        self._btn_annotation_bold = QToolButton()
        self._btn_annotation_bold.setText("G")
        self._btn_annotation_bold.setStyleSheet(_ANNOTATION_TOOL_BTN_STYLE)
        self._btn_annotation_bold.setCheckable(True)
        self._btn_annotation_bold.setToolTip(translate("EditPanel", "Gras"))
        bold_font = QFont("Arial")
        bold_font.setBold(True)
        self._btn_annotation_bold.setFont(bold_font)
        self._btn_annotation_bold.clicked.connect(self._on_annotation_style_changed)
        bi_row.addWidget(self._btn_annotation_bold)
        self._btn_annotation_italic = QToolButton()
        self._btn_annotation_italic.setStyleSheet(_ANNOTATION_TOOL_BTN_STYLE)
        self._btn_annotation_italic.setText("I")
        self._btn_annotation_italic.setCheckable(True)
        self._btn_annotation_italic.setToolTip(translate("EditPanel", "Italique"))
        italic_font = QFont("Arial")
        italic_font.setItalic(True)
        self._btn_annotation_italic.setFont(italic_font)
        self._btn_annotation_italic.clicked.connect(self._on_annotation_style_changed)
        bi_row.addWidget(self._btn_annotation_italic)
        bi_row.addSpacing(10)
        bi_row.addWidget(QLabel(translate("EditPanel", "Couleur")))
        self._btn_annotation_text_color = QPushButton()
        self._btn_annotation_text_color.setFixedSize(24, 24)
        self._btn_annotation_text_color.setToolTip(translate("EditPanel", "Couleur du texte"))
        self._btn_annotation_text_color.clicked.connect(self._pick_annotation_color)
        bi_row.addWidget(self._btn_annotation_text_color)
        bi_row.addStretch()
        self._annotation_bi_row = QWidget()
        self._annotation_bi_row.setLayout(bi_row)
        an_layout.addWidget(self._annotation_bi_row)
        self._update_annotation_style_controls_visibility()

        self._btn_annotation_delete_sel = QPushButton(translate("EditPanel", "Supprimer la sélection"))
        self._btn_annotation_delete_sel.setEnabled(False)
        self._btn_annotation_delete_sel.setToolTip(translate("EditPanel", "Supprimer l'élément d'annotation sélectionné"))
        self._btn_annotation_delete_sel.clicked.connect(self.annotation_delete_selected_requested.emit)
        an_layout.addWidget(self._btn_annotation_delete_sel)

        an_btns = QHBoxLayout()
        an_btns.setSpacing(4)
        btn_clear_ann = QPushButton(translate("EditPanel", "Effacer tout"))
        btn_clear_ann.setToolTip(translate("EditPanel", "Supprimer toutes les annotations"))
        btn_clear_ann.clicked.connect(self._clear_all_annotations)
        an_btns.addWidget(btn_clear_ann)
        btn_done_ann = QPushButton(translate("EditPanel", "Terminé"))
        btn_done_ann.setToolTip(translate("EditPanel", "Quitter le mode annotation  (Echap)"))
        btn_done_ann.clicked.connect(self._done_annotation_mode)
        an_btns.addWidget(btn_done_ann)
        an_layout.addLayout(an_btns)

        self._update_annotation_color_swatch()
        self._update_annotation_fill_color_swatch()
        self._annotation_panel.hide()
        inner_layout.addWidget(self._annotation_panel)

        # Annuler / Rétablir
        undo_row = QHBoxLayout()
        undo_row.setSpacing(4)
        self._btn_undo = QPushButton(translate("EditPanel", "Annuler"))
        self._btn_undo.setEnabled(False)
        self._btn_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self._btn_undo.clicked.connect(self.undo)
        undo_row.addWidget(self._btn_undo)
        self._btn_redo = QPushButton(translate("EditPanel", "Rétablir"))
        self._btn_redo.setEnabled(False)
        self._btn_redo.setShortcut(QKeySequence("Ctrl+Y"))
        self._btn_redo.setStyleSheet("QPushButton:disabled { color: #555; }")
        self._btn_redo.clicked.connect(self.redo)
        undo_row.addWidget(self._btn_redo)
        inner_layout.addLayout(undo_row)

        # Réinitialiser toutes les retouches / Remettre toutes les retouches
        reset_row = QHBoxLayout()
        reset_row.setSpacing(4)
        self._btn_reset = QPushButton(translate("EditPanel", "Réinitialiser\ntoutes les retouches"))
        self._btn_reset.setEnabled(False)
        self._btn_reset.setToolTip(
            translate("EditPanel", "Supprime toutes les retouches et l'historique pour cette photo.\n"
            "Le fichier original sur disque n'est pas modifié.\n"
            "Réversible via « Remettre toutes les retouches ».")
        )
        self._btn_reset.setStyleSheet(
            "QPushButton { color: #c07070; }"
            "QPushButton:hover { color: #e08080; }"
            "QPushButton:disabled { color: #555; }"
        )
        self._btn_reset.clicked.connect(self.reset_all)
        reset_row.addWidget(self._btn_reset)
        self._btn_restore = QPushButton(translate("EditPanel", "Remettre\ntoutes les retouches"))
        self._btn_restore.setEnabled(False)
        self._btn_restore.setToolTip(
            translate("EditPanel", "Remet en place les retouches supprimées par le dernier\n"
            "« Réinitialiser toutes les retouches » sur cette photo.")
        )
        self._btn_restore.setStyleSheet("QPushButton:disabled { color: #555; }")
        self._btn_restore.clicked.connect(self.restore_all)
        reset_row.addWidget(self._btn_restore)
        inner_layout.addLayout(reset_row)

        # Géométrie (boutons directs)
        grp_geo = QGroupBox(translate("EditPanel", "Géométrie"))
        grp_geo.setLayout(QVBoxLayout())
        grp_geo.layout().setSpacing(4)
        grp_geo.layout().setContentsMargins(4, 8, 4, 4)

        row_rot = QHBoxLayout()
        for text, tip, slot in [
            ("↺  -90°", "Rotation 90° anti-horaire", self._rotate_ccw),
            ("↻  +90°", "Rotation 90° horaire",       self._rotate_cw),
        ]:
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row_rot.addWidget(btn)
        grp_geo.layout().addLayout(row_rot)

        # Redresser + Recadrer côte à côte
        row_sr = QHBoxLayout()
        row_sr.setSpacing(4)

        btn_straighten = QToolButton()
        btn_straighten.setText(translate("EditPanel", "Redresser"))
        btn_straighten.setIcon(QIcon(_icon_straighten()))
        btn_straighten.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        btn_straighten.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        btn_straighten.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn_straighten.setFixedHeight(_ICON_SIZE + 28)
        btn_straighten.setToolTip(translate("EditPanel", "Corriger l'inclinaison de l'horizon (-10° à +10°)"))
        btn_straighten.clicked.connect(
            lambda: self._open_treatment(
                "Redresser",
                [(translate("EditPanel", "Angle (°)"), "straighten", -10.0, 10.0, 1)])
        )
        self._treatment_buttons["Redresser"] = btn_straighten
        row_sr.addWidget(btn_straighten)

        self._btn_crop = QToolButton()
        self._btn_crop.setText(translate("EditPanel", "Recadrer"))
        self._btn_crop.setIcon(QIcon(_icon_crop()))
        self._btn_crop.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._btn_crop.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self._btn_crop.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._btn_crop.setFixedHeight(_ICON_SIZE + 28)
        self._btn_crop.setToolTip(translate("EditPanel", "Définir interactivement la zone de recadrage"))
        self._btn_crop.clicked.connect(self._on_crop_clicked)
        row_sr.addWidget(self._btn_crop)

        grp_geo.layout().addLayout(row_sr)

        row_flip = QHBoxLayout()
        row_flip.setSpacing(4)
        for icon_fn, label, tip, slot in [
            (_icon_flip_h, "Miroir H", "Miroir horizontal", self._flip_h),
            (_icon_flip_v, "Miroir V", "Miroir vertical",   self._flip_v),
        ]:
            btn = QToolButton()
            btn.setText(label)
            btn.setIcon(QIcon(icon_fn()))
            btn.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setFixedHeight(_ICON_SIZE + 28)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row_flip.addWidget(btn)
        grp_geo.layout().addLayout(row_flip)

        inner_layout.addWidget(grp_geo)
        inner_layout.addStretch()
        scroll.setWidget(inner)
        self._scroll = scroll
        self._scroll_inner = inner
        # QScrollArea ne propage jamais le minimumSizeHint() de son widget
        # interne vers le sien (comportement voulu pour permettre un contenu
        # plus grand que la vue) — sans ce plancher explicite, rien n'empêche
        # le splitter de comprimer le panneau sous la largeur requise par la
        # grille de boutons à 2 colonnes, rendant la 2e colonne (Contraste,
        # Vignette, Annotations…) invisible et inatteignable au clic. Valeur
        # posée ici à titre de filet de sécurité minimal ; le calcul faisant
        # foi pour le splitter est `content_min_width()`, interrogé à la
        # demande car le style Qt applicatif (`app.setStyleSheet` dans
        # main.py) n'est pleinement résolu qu'après le premier affichage —
        # une valeur figée ici, avant le show(), sous-estime la largeur
        # réelle des boutons une fois stylés.
        scroll.setMinimumWidth(inner.minimumSizeHint().width() + 2 * scroll.frameWidth() + 4)
        root.addWidget(scroll, stretch=1)

    def content_min_width(self) -> int:
        """Largeur minimale, recalculée à la demande, pour afficher la
        grille de boutons de traitement (2 colonnes) sans troncature ni
        recours à la scrollbar horizontale — cf. commentaire sur
        `scroll.setMinimumWidth` dans `_setup_ui` pour pourquoi ce calcul
        ne peut pas être figé une fois pour toutes à la construction."""
        margins = self.layout().contentsMargins()
        return (self._scroll_inner.minimumSizeHint().width()
                + 2 * self._scroll.frameWidth()
                + self._vertical_scrollbar_width()
                + margins.left() + margins.right() + 4)

    def _vertical_scrollbar_width(self) -> int:
        """Largeur prise par l'ascenseur vertical de la QScrollArea.

        Le contenu du panneau dépasse toujours la hauteur disponible : la barre
        verticale est présente en pratique et mange autant de largeur au
        viewport. Sans elle dans le calcul, la 2e colonne de boutons ressort de
        quelques pixels hors du viewport — exactement le défaut que
        content_min_width() est censé empêcher."""
        if self._scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff:
            return 0
        return self._scroll.verticalScrollBar().sizeHint().width()

    def _make_treatment_button(self, name: str, icon_px: QPixmap,
                                sliders_def: list) -> QToolButton:
        btn = QToolButton()
        btn.setText(_tool_label(name))
        btn.setIcon(QIcon(icon_px))
        btn.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setFixedHeight(_ICON_SIZE + 28)
        btn.clicked.connect(lambda: self._open_treatment(name, sliders_def))
        return btn

    # ------------------------------------------------------------------ dialogues

    def _compute_dialog_pos(self, dw: int, dh: int) -> QPoint:
        """Positionne le dialogue en bas-gauche de la zone image.
        Appelé depuis TreatmentDialog.showEvent — dw/dh sont les dimensions réelles.
        Ancres : _navbar et _toolbar du PhotoViewer (coordonnées exactes, évite
        de couvrir les boutons Précédente/Suivante)."""
        from PySide6.QtWidgets import QSplitter
        margin = 16

        # Naviguer vers PhotoViewer : self → _left_stack → _splitter → _stack → viewer
        splitter = self.parentWidget().parentWidget() if self.parentWidget() else None
        viewer = None
        if isinstance(splitter, QSplitter) and splitter.count() >= 2:
            stack = splitter.widget(1)
            if stack.count() >= 2:
                viewer = stack.widget(1)

        if viewer and hasattr(viewer, '_navbar') and hasattr(viewer, '_toolbar'):
            navbar = viewer._navbar
            vtb   = viewer._toolbar

            # Bas utilisable  = sommet de la barre prev/suivante, moins sa propre hauteur
            nav_tl    = navbar.mapToGlobal(QPoint(0, 0))
            bottom_y  = nav_tl.y() - navbar.height() - margin

            # Haut utilisable = bas de la toolbar du viewer
            top_y = vtb.mapToGlobal(QPoint(0, vtb.height())).y() + margin

            # Gauche/droite depuis la navbar (même largeur que le viewer)
            img_left  = nav_tl.x() + margin
            img_right = nav_tl.x() + navbar.width() - margin
        else:
            # Repli : coin supérieur-droit du panneau gauche
            tr       = self.mapToGlobal(QPoint(self.width(), 0))
            img_left  = tr.x() + margin
            img_right = tr.x() + self.window().width() - self.width() - margin
            bottom_y  = tr.y() + self.height() - margin
            top_y     = tr.y() + margin

        x = img_left
        y = bottom_y - dh
        x = min(x, img_right - dw)
        y = max(y, top_y)

        # Clamper aux limites de l'écran
        from PySide6.QtWidgets import QApplication
        screen = QApplication.screenAt(QPoint(x + dw // 2, y + dh // 2)) or QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            x = max(sg.left(), min(x, sg.right() - dw))
            y = max(sg.top(), min(y, sg.bottom() - dh))

        return QPoint(x, y)

    def _deactivate_other_tools(self, current: str) -> None:
        """Un seul outil d'édition actif à la fois : sélectionner un outil désactive
        celui actuellement en cours (mode canvas interactif ou dialogue de réglage
        non modal encore ouvert). ``current`` est le nom de l'outil qu'on active
        (ex. "Recadrer", "Yeux rouges", "Annotations", ou un titre de _TREATMENTS).
        La sortie équivaut à une validation (pas une annulation) : le travail en
        cours dans l'outil quitté est appliqué, jamais perdu silencieusement."""
        if current != "Yeux rouges" and self._red_eye_active:
            self._btn_red_eye.setChecked(False)
            self._toggle_red_eye_mode(False)
        if current != "Annotations" and self._annotation_active:
            self._btn_annotations.setChecked(False)
            self._toggle_annotation_mode(False)
        if current != "Recadrer" and self._crop_active:
            self.crop_confirm_requested.emit()
        if current != "Couleurs" and self._active_color_dlg is not None:
            self._active_color_dlg.accept()
        if current != "Vignette" and self._active_vignette_dlg is not None:
            self._active_vignette_dlg.accept()
        if current != "Cadre" and self._active_frame_dlg is not None:
            self._active_frame_dlg.accept()
        if current != self._active_generic_dlg_title and self._active_generic_dlg is not None:
            self._active_generic_dlg.accept()

    def _highlight_treatment_button(self, title: str, active: bool) -> None:
        """Même surbrillance que le bouton Annotations autour de l'icône, tant que
        l'outil ``title`` est actif (dialogue ouvert ou mode canvas en cours)."""
        btn = self._treatment_buttons.get(title)
        if btn is not None:
            btn.setStyleSheet(_ACTIVE_TOOL_STYLE if active else "")

    def _open_treatment(self, title: str, sliders_def: list) -> None:
        # Déjà ouvert : le ramener au premier plan plutôt que d'en ouvrir un second.
        if self._active_generic_dlg is not None and self._active_generic_dlg_title == title:
            self._active_generic_dlg.raise_()
            self._active_generic_dlg.activateWindow()
            return
        self._deactivate_other_tools(title)
        self._highlight_treatment_button(title, True)
        if title == "Luminosité":
            self._open_luminosite_treatment()
            return
        if title == "Couleurs":
            self._open_couleurs_treatment()
            return
        if title == "Vignette":
            self._open_vignette_treatment()
            return
        if title == "Cadre":
            self._open_frame_treatment()
            return

        original = copy.copy(self._edit)
        dlg = TreatmentDialog(_tool_label(title), sliders_def, self._edit, parent=self)
        dlg.preview.connect(self._on_preview)
        dlg._panel = self
        self._active_generic_dlg = dlg
        self._active_generic_dlg_title = title

        is_straighten = (title == "Redresser")
        if is_straighten:
            self.grid_visibility_changed.emit(True)

        def _finish(accepted: bool) -> None:
            self._active_generic_dlg = None
            self._active_generic_dlg_title = None
            if accepted:
                self._checkpoint(title)
                self._push_undo(title)
                new_edit = dlg.get_edit()
                for _, attr, *_ in sliders_def:
                    setattr(self._edit, attr, getattr(new_edit, attr))
                self.edits_changed.emit(copy.copy(self._edit))
                self._save(title)
            else:
                self._edit = original
                self.edits_changed.emit(copy.copy(self._edit))
            if is_straighten:
                self.grid_visibility_changed.emit(False)
            self._highlight_treatment_button(title, False)

        dlg.accepted.connect(lambda: _finish(True))
        dlg.rejected.connect(lambda: _finish(False))
        dlg.show()
        dlg.raise_()

    def _on_crop_clicked(self) -> None:
        self._deactivate_other_tools("Recadrer")
        self._crop_active = True
        self._btn_crop.setStyleSheet(_ACTIVE_TOOL_STYLE)
        self.crop_mode_requested.emit()

    def on_crop_mode_ended(self) -> None:
        """Reçu depuis la visionneuse quand le mode recadrage se termine
        (validation ou annulation) — retire la surbrillance du bouton."""
        self._crop_active = False
        self._btn_crop.setStyleSheet("")

    def _open_couleurs_treatment(self) -> None:
        # Si le dialogue est déjà ouvert, le ramener au premier plan
        if self._active_color_dlg is not None:
            self._active_color_dlg.raise_()
            self._active_color_dlg.activateWindow()
            return

        original = copy.copy(self._edit)
        dlg = CouleursTreatmentDialog(self._edit, parent=self)
        dlg.preview.connect(self._on_preview)
        dlg._panel = self
        self._active_color_dlg = dlg

        # Pipette : forward du signal vers main_window → visionneuse
        dlg.wb_pick_requested.connect(self.wb_pick_requested)

        def _on_accepted() -> None:
            self._checkpoint("Couleurs")
            self._push_undo("Couleurs")
            new_edit = dlg.get_edit()
            for attr in ("saturation", "color_red", "color_green", "color_blue"):
                setattr(self._edit, attr, getattr(new_edit, attr))
            self.edits_changed.emit(copy.copy(self._edit))
            self._save("Couleurs")

        def _on_rejected() -> None:
            self._edit = original
            self.edits_changed.emit(copy.copy(self._edit))

        def _cleanup() -> None:
            # Annuler le mode pipette si toujours actif
            self.wb_pick_requested.emit(False)
            self._active_color_dlg = None
            self._highlight_treatment_button("Couleurs", False)

        dlg.accepted.connect(_on_accepted)
        dlg.rejected.connect(_on_rejected)
        dlg.finished.connect(_cleanup)

        dlg.show()
        dlg.raise_()

    @Slot(int, int, int)
    def on_wb_pixel_received(self, r: int, g: int, b: int) -> None:
        """Appelé par main_window quand l'utilisateur a cliqué sur la visionneuse en mode pipette."""
        if self._active_color_dlg is not None:
            self._active_color_dlg.apply_wb_pixel(r, g, b)
            self._active_color_dlg.raise_()
            self._active_color_dlg.activateWindow()

    def _open_luminosite_treatment(self) -> None:
        original = copy.copy(self._edit)
        photo_path = self._photo.path if self._photo else None
        dlg = LuminositeTreatmentDialog(self._edit, photo_path=photo_path, parent=self)
        dlg.preview.connect(self._on_preview)
        dlg._panel = self
        self._active_generic_dlg = dlg
        self._active_generic_dlg_title = "Luminosité"

        def _finish(accepted: bool) -> None:
            self._active_generic_dlg = None
            self._active_generic_dlg_title = None
            if accepted:
                self._checkpoint("Luminosité")
                self._push_undo("Luminosité")
                new_edit = dlg.get_edit()
                self._edit.brightness = new_edit.brightness
                self._edit.gamma = new_edit.gamma
                self._edit.gamma_use_curve = new_edit.gamma_use_curve
                self._edit.gamma_curve_points = new_edit.gamma_curve_points
                self.edits_changed.emit(copy.copy(self._edit))
                self._save("Luminosité")
            else:
                self._edit = original
                self.edits_changed.emit(copy.copy(self._edit))
            self._highlight_treatment_button("Luminosité", False)

        dlg.accepted.connect(lambda: _finish(True))
        dlg.rejected.connect(lambda: _finish(False))
        dlg.show()
        dlg.raise_()

    def _open_vignette_treatment(self) -> None:
        if self._active_vignette_dlg is not None:
            self._active_vignette_dlg.raise_()
            self._active_vignette_dlg.activateWindow()
            return

        original = copy.copy(self._edit)
        dlg = VignetteTreatmentDialog(self._edit, parent=self)
        dlg.preview.connect(self._on_vignette_preview)
        dlg._panel = self
        self._active_vignette_dlg = dlg

        def _finish(accepted: bool) -> None:
            self._active_vignette_dlg = None
            self._highlight_treatment_button("Vignette", False)
            if accepted:
                # Pousser l'état AVANT ouverture (original), pas self._edit qui a
                # déjà été modifié par on_vignette_changed pendant le glissement.
                # Aussi sauvegarder dans la DB pour l'undo cross-session.
                if self._photo:
                    self._db.push_history(self._photo.path, original, "Vignette")
                self._undo_stack.append((original, "Vignette"))
                if len(self._undo_stack) > _UNDO_MAX:
                    self._undo_stack.pop(0)
                self._redo_stack.clear()
                self._update_undo_buttons()
                # La géométrie (cx/cy/rx/ry/angle) est déjà dans self._edit
                # via on_vignette_changed ; seuls force et couleur viennent du dialogue.
                new_edit = dlg.get_edit()
                self._edit.vignette_strength = new_edit.vignette_strength
                self._edit.vignette_color    = new_edit.vignette_color
                self.edits_changed.emit(copy.copy(self._edit))
                self._save("Vignette")
            else:
                self._edit = original
                self.edits_changed.emit(copy.copy(self._edit))
            self.vignette_edit_mode.emit(False, copy.copy(self._edit))

        dlg.accepted.connect(lambda: _finish(True))
        dlg.rejected.connect(lambda: _finish(False))

        self.vignette_edit_mode.emit(True, copy.copy(self._edit))
        dlg.show()
        dlg.raise_()

    # Attributs de cadre recopiés entre le dialogue et l'état du panneau.
    _FRAME_ATTRS = (
        "frame_type", "frame_width", "frame_inner_width", "frame_gap",
        "frame_style", "frame_color", "frame_color2", "frame_inner_color",
        "frame_gap_color", "frame_inner_enabled", "frame_inner_motif",
        "frame_inner_relief", "frame_inner_ornament",
    )

    def _open_frame_treatment(self) -> None:
        if self._active_frame_dlg is not None:
            self._active_frame_dlg.raise_()
            self._active_frame_dlg.activateWindow()
            return

        from src.ui.frame_dialog import FrameDialog

        original = copy.copy(self._edit)
        photo_path = self._photo.path if self._photo else None
        dlg = FrameDialog(self._edit, photo_path=photo_path, parent=self)
        # Aperçu en direct : l'EditInfo du dialogue (copie complète de l'état du
        # panneau) part telle quelle vers la visionneuse. self._edit n'est pas
        # touché avant validation, pour que _push_undo empile bien l'état d'avant.
        dlg.preview.connect(self._on_preview)
        dlg._panel = self
        self._active_frame_dlg = dlg

        def _finish(accepted: bool) -> None:
            self._active_frame_dlg = None
            self._highlight_treatment_button("Cadre", False)
            if accepted:
                self._checkpoint("Cadre")
                self._push_undo("Cadre")
                new_edit = dlg.get_edit()
                for attr in self._FRAME_ATTRS:
                    setattr(self._edit, attr, getattr(new_edit, attr))
                self.edits_changed.emit(copy.copy(self._edit))
                self._save("Cadre")
            else:
                self._edit = original
                self.edits_changed.emit(copy.copy(self._edit))

        dlg.accepted.connect(lambda: _finish(True))
        dlg.rejected.connect(lambda: _finish(False))
        dlg.show()
        dlg.raise_()

    def _on_vignette_preview(self, edit: EditInfo) -> None:
        """Mise à jour depuis le slider d'intensité ou bouton couleur du dialogue."""
        self._edit.vignette_strength = edit.vignette_strength
        self._edit.vignette_color    = edit.vignette_color
        self.edits_changed.emit(copy.copy(self._edit))

    @Slot(object)
    def on_vignette_changed(self, edit: EditInfo) -> None:
        """Appelé par main_window quand l'utilisateur a manipulé les poignées sur la visionneuse."""
        self._edit.vignette_cx    = edit.vignette_cx
        self._edit.vignette_cy    = edit.vignette_cy
        self._edit.vignette_rx1   = edit.vignette_rx1
        self._edit.vignette_ry1   = edit.vignette_ry1
        self._edit.vignette_rx2   = edit.vignette_rx2
        self._edit.vignette_ry2   = edit.vignette_ry2
        self._edit.vignette_angle = edit.vignette_angle
        if self._active_vignette_dlg is not None:
            self._active_vignette_dlg.update_from_edit(self._edit)
        self.edits_changed.emit(copy.copy(self._edit))

    def _on_preview(self, edit: EditInfo) -> None:
        self.edits_changed.emit(edit)

    # ------------------------------------------------------------------ public

    def set_photo(self, photo: PhotoInfo) -> None:
        self._deactivate_other_tools("")   # aucun outil ne doit rester actif au changement de photo
        self._photo = photo
        self._edit = self._db.load(photo.path)
        # get_history retourne aussi l'état courant (dernier enregistrement).
        # On le retire : la pile ne doit contenir que les états PRÉCÉDENTS.
        history = self._db.get_history(photo.path, limit=_UNDO_MAX + 1)
        if history:
            history.pop()
        self._undo_stack = history   # list[(EditInfo, op_label)]
        self._redo_stack.clear()
        self._title_label.setText(
            translate("EditPanel", "Retouche — {name}").format(name=photo.filename))
        self._update_undo_buttons()

    def get_edit(self) -> EditInfo:
        return copy.copy(self._edit)

    def undo(self) -> None:
        if not self._undo_stack:
            return
        before = self._edit.rotation
        prev_edit, op_label = self._undo_stack.pop()
        self._redo_stack.append((copy.copy(self._edit), op_label))
        self._edit = prev_edit
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("undo")
        self._update_undo_buttons()
        self._emit_rotation_if_changed(before)

    def redo(self) -> None:
        if not self._redo_stack:
            return
        before = self._edit.rotation
        prev_edit, op_label = self._redo_stack.pop()
        self._undo_stack.append((copy.copy(self._edit), op_label))
        self._edit = prev_edit
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("redo")
        self._update_undo_buttons()
        self._emit_rotation_if_changed(before)

    # ------------------------------------------------------------------ private

    def _emit_rotation_if_changed(self, before: float) -> None:
        """Émet rotation_stepped si la rotation vient de changer.

        Les boutons ↻/↺ émettent eux-mêmes ; ce helper couvre les chemins qui
        changent la rotation sans passer par eux (undo, redo, reset_all,
        restore_all). Sans ça, indexed_photos.rotation reste figé sur
        l'orientation de la dernière détection alors que la photo est revenue à
        une autre : la re-détection ne retrouve plus qu'une partie des visages
        et aucune action de l'UI ne permet d'en sortir."""
        if not self._photo:
            return
        after = int(self._edit.rotation) % 360
        if int(before) % 360 == after:
            return
        self.rotation_stepped.emit(self._photo.path, after)

    def _save(self, operation: str) -> None:
        if self._photo:
            if self._db.save(self._photo.path, self._edit, operation=operation):
                self.photo_saved.emit(self._photo.path, copy.copy(self._edit))

    def _checkpoint(self, op_label: str) -> None:
        """Sauvegarde l'état courant dans l'historique DB avant une opération.

        Permet l'undo cross-session : au prochain démarrage, cet état sera
        disponible dans la pile même si la session précédente n'a pas fait d'undo.
        """
        if self._photo:
            self._db.push_history(self._photo.path, self._edit, op_label)

    def _checkpoint_state(self, edit: EditInfo, op_label: str) -> None:
        """Comme _checkpoint(), mais pour un état arbitraire (réinjection d'historique)."""
        if self._photo:
            self._db.push_history(self._photo.path, edit, op_label)

    def _push_undo(self, op_label: str) -> None:
        self._undo_stack.append((copy.copy(self._edit), op_label))
        if len(self._undo_stack) > _UNDO_MAX:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        # une nouvelle retouche après un reset_all() invalide l'instantané de restauration
        # (sinon un futur restore_all() écraserait silencieusement cette retouche)
        if self._photo:
            self._reset_snapshots.pop(os.path.normpath(self._photo.path), None)
        self._update_undo_buttons()

    def _update_undo_buttons(self) -> None:
        if self._undo_stack:
            _, label = self._undo_stack[-1]
            self._btn_undo.setText(
                translate("EditPanel", "Annuler  {op}").format(op=_op_label(label)))
            self._btn_undo.setEnabled(True)
        else:
            self._btn_undo.setText(translate("EditPanel", "Annuler"))
            self._btn_undo.setEnabled(False)
        if self._redo_stack:
            _, label = self._redo_stack[-1]
            self._btn_redo.setText(
                translate("EditPanel", "Rétablir  {op}").format(op=_op_label(label)))
            self._btn_redo.setEnabled(True)
        else:
            self._btn_redo.setText(translate("EditPanel", "Rétablir"))
            self._btn_redo.setEnabled(False)
        self._btn_reset.setEnabled(self._edit.is_modified())
        can_restore = bool(self._photo) and os.path.normpath(self._photo.path) in self._reset_snapshots
        self._btn_restore.setEnabled(can_restore)

    def reset_all(self) -> None:
        """Supprime toutes les retouches et l'historique pour la photo courante.

        Pas de confirmation : l'action est réversible via restore_all()."""
        if not self._photo:
            return
        before = self._edit.rotation
        # L'historique est sauvegardé avec l'état : restore_all() doit rendre les
        # retouches ET la possibilité de les défaire une par une (sinon un
        # reset + restauration écrase définitivement l'historique de la photo).
        self._reset_snapshots[os.path.normpath(self._photo.path)] = (
            copy.copy(self._edit), list(self._undo_stack),
        )
        self._db.delete(self._photo.path)
        self._edit = EditInfo()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._update_undo_buttons()
        self.edits_changed.emit(copy.copy(self._edit))
        self.photo_saved.emit(self._photo.path, copy.copy(self._edit))
        self._emit_rotation_if_changed(before)

    def restore_all(self) -> None:
        """Remet en place les retouches supprimées par le dernier reset_all() sur cette photo."""
        if not self._photo:
            return
        snapshot = self._reset_snapshots.pop(os.path.normpath(self._photo.path), None)
        if snapshot is None:
            self._update_undo_buttons()
            return
        prev_edit, prev_history = snapshot
        before = self._edit.rotation
        self._edit = prev_edit
        self._undo_stack = list(prev_history)
        self._redo_stack.clear()
        # reset_all() a effacé edit_history en DB : on la réinsère avant _save()
        # (qui y empile l'état courant) pour que l'undo pas-à-pas reste possible,
        # y compris après redémarrage de l'application.
        for hist_edit, op_label in self._undo_stack:
            self._checkpoint_state(hist_edit, op_label)
        self._save("restore_all")
        self._update_undo_buttons()
        self.edits_changed.emit(copy.copy(self._edit))
        self._emit_rotation_if_changed(before)

    def _rotate_cw(self) -> None:
        self._checkpoint("Rotation +90°")
        self._push_undo("Rotation +90°")
        self._edit.rotation = (self._edit.rotation + 90) % 360
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("rotation")
        if self._photo:
            self.rotation_stepped.emit(self._photo.path, self._edit.rotation)

    def _rotate_ccw(self) -> None:
        self._checkpoint("Rotation −90°")
        self._push_undo("Rotation −90°")
        self._edit.rotation = (self._edit.rotation - 90) % 360
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("rotation")
        if self._photo:
            self.rotation_stepped.emit(self._photo.path, self._edit.rotation)

    def _flip_h(self) -> None:
        self._checkpoint("Miroir H")
        self._push_undo("Miroir H")
        self._edit.flip_h = not self._edit.flip_h
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("flip_h")

    def _flip_v(self) -> None:
        self._checkpoint("Miroir V")
        self._push_undo("Miroir V")
        self._edit.flip_v = not self._edit.flip_v
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("flip_v")

    def apply_crop(self, quad: tuple) -> None:
        self._checkpoint("Recadrage")
        self._push_undo("Recadrage")
        self._edit.crop = quad
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("crop")

    # ------------------------------------------------------------------ yeux rouges

    def _toggle_red_eye_mode(self, checked: bool) -> None:
        self._red_eye_active = checked
        if checked:
            self._deactivate_other_tools("Yeux rouges")
            self._btn_red_eye.setStyleSheet(_ACTIVE_TOOL_STYLE)
            self._red_eye_panel.show()
            radius = self._red_eye_slider.value() / 1000.0
            self.red_eye_mode_requested.emit(True, radius)
        else:
            self._btn_red_eye.setStyleSheet("")
            self._red_eye_panel.hide()
            self.red_eye_mode_requested.emit(False, 0.0)

    def _on_red_eye_radius_changed(self, value: int) -> None:
        if self._red_eye_active:
            self.red_eye_mode_requested.emit(True, value / 1000.0)

    def _clear_red_eye(self) -> None:
        if not self._edit.red_eye_regions:
            return
        self._checkpoint("Effacer yeux rouges")
        self._push_undo("Effacer yeux rouges")
        self._edit.red_eye_regions = []
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("red_eye_clear")

    def _done_red_eye(self) -> None:
        self._btn_red_eye.setChecked(False)
        self._toggle_red_eye_mode(False)

    def on_red_eye_added(self, cx: float, cy: float) -> None:
        """Reçu depuis le canvas quand l'utilisateur clique sur un œil rouge."""
        self._checkpoint("Yeux rouges")
        self._push_undo("Yeux rouges")
        radius = self._red_eye_slider.value() / 1000.0
        self._edit.red_eye_regions = list(self._edit.red_eye_regions)
        self._edit.red_eye_regions.append((cx, cy, radius))
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("red_eye")

    # ------------------------------------------------------------------ annotations

    def _toggle_annotation_mode(self, checked: bool) -> None:
        self._annotation_active = checked
        if checked:
            self._deactivate_other_tools("Annotations")
            self._btn_annotations.setStyleSheet(_ACTIVE_TOOL_STYLE)
            self._annotation_panel.show()
            self.annotation_mode_requested.emit(True, self._annotation_tool)
            self._emit_annotation_style()
        else:
            self._btn_annotations.setStyleSheet("")
            self._annotation_panel.hide()
            self._annotation_selected_ids = set()
            self._btn_annotation_delete_sel.setEnabled(False)
            self.annotation_mode_requested.emit(False, self._annotation_tool)
        self._update_annotation_style_controls_visibility()

    def _set_annotation_tool(self, tool: str) -> None:
        self._annotation_tool = tool
        if self._annotation_active:
            self.annotation_mode_requested.emit(True, tool)
        self._update_annotation_style_controls_visibility()

    def _annotation_active_kind(self) -> "str | None":
        """Détermine à quel groupe de contrôles de style le contexte courant se rapporte :
        'line' (trait : stylo/ligne/courbe), 'shape' (rectangle/ellipse), 'text',
        ou None si rien n'est pertinent (ex. outil Sélection sans élément sélectionné)."""
        if self._annotation_tool in ("pen", "line", "curve"):
            return "line"
        if self._annotation_tool in ("rect", "ellipse"):
            return "shape"
        if self._annotation_tool == "text":
            return "text"
        if self._annotation_tool == "select" and self._annotation_selected_ids:
            kinds = set()
            for ann_id in self._annotation_selected_ids:
                ann = next((a for a in self._edit.annotations if a.get("id") == ann_id), None)
                if ann is None:
                    continue
                if ann.get("type") == "text":
                    kinds.add("text")
                elif ann.get("type") in ("rect", "ellipse"):
                    kinds.add("shape")
                else:
                    kinds.add("line")
            if len(kinds) == 1:
                return next(iter(kinds))
        return None

    def _update_annotation_style_controls_visibility(self) -> None:
        kind = self._annotation_active_kind()
        self._annotation_style_row.setVisible(kind in ("line", "shape"))
        self._annotation_shape_row.setVisible(kind == "shape")
        self._annotation_font_row.setVisible(kind == "text")
        self._annotation_bi_row.setVisible(kind == "text")

    def _pick_annotation_color(self) -> None:
        color = QColorDialog.getColor(
            self._annotation_color, self,
            translate("EditPanel", "Couleur d'annotation"),
            QColorDialog.ShowAlphaChannel,
        )
        if color.isValid():
            self._annotation_color = color
            self._update_annotation_color_swatch()
            self._on_annotation_style_changed()

    def _update_annotation_color_swatch(self) -> None:
        style = (
            f"background-color: {self._annotation_color.name(QColor.HexArgb)}; "
            "border: 1px solid #888; border-radius: 3px;"
        )
        self._btn_annotation_color.setStyleSheet(style)
        self._btn_annotation_text_color.setStyleSheet(style)

    def _pick_annotation_fill_color(self) -> None:
        # Pas de canal alpha ici : l'opacité de la surface est régie exclusivement
        # par _annotation_opacity_spin, pour éviter deux contrôles de transparence
        # qui se composent silencieusement.
        color = QColorDialog.getColor(self._annotation_fill_color, self,
                                      translate("EditPanel", "Couleur de la surface"))
        if color.isValid():
            self._annotation_fill_color = color
            self._update_annotation_fill_color_swatch()
            self._on_annotation_style_changed()

    def _update_annotation_fill_color_swatch(self) -> None:
        self._btn_annotation_fill_color.setStyleSheet(
            f"background-color: {self._annotation_fill_color.name(QColor.HexArgb)}; "
            "border: 1px solid #888; border-radius: 3px;"
        )

    def _on_annotation_style_changed(self, *_args) -> None:
        if self._annotation_active:
            self._emit_annotation_style()

    def _emit_annotation_style(self) -> None:
        width = self._annotation_width_spin.value() / 100.0
        font_family = self._annotation_font_combo.currentFont().family()
        font_size = self._annotation_font_size.value() / 100.0
        bold = self._btn_annotation_bold.isChecked()
        italic = self._btn_annotation_italic.isChecked()
        color = self._annotation_color.name(QColor.HexArgb)
        fill_color = self._annotation_fill_color.name(QColor.HexArgb)
        opacity = self._annotation_opacity_spin.value() / 100.0
        blur = self._annotation_blur_spin.value() / 100.0
        self.annotation_style_changed.emit(color, width, font_family, font_size, bold, italic,
                                            fill_color, opacity, blur)
        if self._annotation_selected_ids:
            self._apply_style_to_selected(color, width, font_family, font_size, bold, italic,
                                           fill_color, opacity, blur)

    def _apply_style_to_selected(self, color: str, width: float, font_family: str,
                                  font_size: float, bold: bool, italic: bool,
                                  fill_color: str = "#ffff0000", opacity: float = 0.4,
                                  blur: float = 0.0) -> None:
        """Applique le style courant (couleur/épaisseur/police/fond) aux éléments sélectionnés,
        plutôt qu'au seul style par défaut des prochains éléments dessinés."""
        ids = self._annotation_selected_ids
        new_list = []
        any_updated = False
        for a in self._edit.annotations:
            if a.get("id") in ids:
                a = dict(a)
                a["color"] = color
                if a.get("type") == "text":
                    a["font_family"] = font_family
                    a["font_size"] = font_size
                    a["bold"] = bold
                    a["italic"] = italic
                elif a.get("type") in ("rect", "ellipse"):
                    a["width"] = width
                    a["fill_color"] = fill_color
                    a["opacity"] = opacity
                    a["blur"] = blur
                else:
                    a["width"] = width
                any_updated = True
            new_list.append(a)
        if not any_updated:
            return
        self._checkpoint("annotation_style")
        self._push_undo("annotation_style")
        self._edit.annotations = new_list
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_style")

    def _clear_all_annotations(self) -> None:
        if not self._edit.annotations:
            return
        self._checkpoint("Effacer annotations")
        self._push_undo("Effacer annotations")
        self._edit.annotations = []
        self._annotation_selected_ids = set()
        self._btn_annotation_delete_sel.setEnabled(False)
        self._update_annotation_style_controls_visibility()
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_clear")

    def _done_annotation_mode(self) -> None:
        self._btn_annotations.setChecked(False)
        self._toggle_annotation_mode(False)

    def on_annotation_added(self, annotation: dict) -> None:
        """Reçu depuis le canvas quand l'utilisateur valide un nouvel élément (trait/texte)."""
        self._checkpoint("Annotation")
        self._push_undo("Annotation")
        self._edit.annotations = list(self._edit.annotations) + [dict(annotation)]
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation")

    def on_annotation_deleted(self, annotation_id: str) -> None:
        """Reçu depuis le canvas quand l'utilisateur supprime l'élément sélectionné."""
        self._checkpoint("annotation_delete")
        self._push_undo("annotation_delete")
        self._edit.annotations = [a for a in self._edit.annotations if a.get("id") != annotation_id]
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_delete")

    def on_annotation_deleted_multi(self, annotation_ids) -> None:
        """Reçu depuis le canvas quand l'utilisateur supprime plusieurs éléments sélectionnés."""
        ids = set(annotation_ids or [])
        if not ids:
            return
        self._checkpoint("annotation_delete_multi")
        self._push_undo("annotation_delete_multi")
        self._edit.annotations = [a for a in self._edit.annotations if a.get("id") not in ids]
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_delete_multi")

    def on_annotation_selection_changed(self, annotation_ids) -> None:
        self._annotation_selected_ids = set(annotation_ids or [])
        self._btn_annotation_delete_sel.setEnabled(bool(self._annotation_selected_ids))
        if len(self._annotation_selected_ids) == 1:
            ann_id = next(iter(self._annotation_selected_ids))
            ann = next((a for a in self._edit.annotations if a.get("id") == ann_id), None)
            if ann is not None:
                self._load_style_into_controls(ann)
        self._update_annotation_style_controls_visibility()

    def _load_style_into_controls(self, ann: dict) -> None:
        """Reflète le style de l'élément sélectionné (couleur/épaisseur/police) dans les
        contrôles du panneau, sans déclencher de ré-application en cascade sur l'élément."""
        widgets = [
            self._annotation_width_spin, self._annotation_font_combo,
            self._annotation_font_size, self._btn_annotation_bold, self._btn_annotation_italic,
            self._annotation_opacity_spin, self._annotation_blur_spin,
        ]
        for w in widgets:
            w.blockSignals(True)
        try:
            self._annotation_color = QColor(ann.get("color") or self._annotation_color.name(QColor.HexArgb))
            self._update_annotation_color_swatch()
            if ann.get("type") == "text":
                self._annotation_font_combo.setCurrentFont(QFont(ann.get("font_family", "Arial")))
                self._annotation_font_size.setValue(round(ann.get("font_size", 0.04) * 100))
                self._btn_annotation_bold.setChecked(bool(ann.get("bold", False)))
                self._btn_annotation_italic.setChecked(bool(ann.get("italic", False)))
            elif ann.get("type") in ("rect", "ellipse"):
                self._annotation_width_spin.setValue(ann.get("width", 0.006) * 100)
                self._annotation_fill_color = QColor(
                    ann.get("fill_color") or self._annotation_fill_color.name(QColor.HexArgb)
                )
                self._update_annotation_fill_color_swatch()
                self._annotation_opacity_spin.setValue(ann.get("opacity", 1.0) * 100)
                self._annotation_blur_spin.setValue(ann.get("blur", 0.0) * 100)
            else:
                self._annotation_width_spin.setValue(ann.get("width", 0.006) * 100)
        finally:
            for w in widgets:
                w.blockSignals(False)

    def on_annotation_moved(self, annotation_id: str, updated: dict) -> None:
        """Reçu depuis le canvas quand l'utilisateur relâche la souris après avoir
        déplacé l'élément sélectionné (outil Sélection)."""
        self._checkpoint("annotation_move")
        self._push_undo("annotation_move")
        self._edit.annotations = [
            dict(updated) if a.get("id") == annotation_id else a for a in self._edit.annotations
        ]
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_move")

    def on_annotation_moved_multi(self, updated) -> None:
        """Reçu depuis le canvas quand l'utilisateur relâche la souris après avoir
        déplacé plusieurs éléments sélectionnés en une fois (outil Sélection)."""
        if not updated:
            return
        self._checkpoint("annotation_move_multi")
        self._push_undo("annotation_move_multi")
        self._edit.annotations = [
            dict(updated[a.get("id")]) if a.get("id") in updated else a for a in self._edit.annotations
        ]
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_move_multi")

    def on_annotation_resized(self, annotation_id: str, updated: dict) -> None:
        """Reçu depuis le canvas quand l'utilisateur relâche la souris après avoir
        redimensionné/tourné l'élément sélectionné via ses ancres (outil Sélection)."""
        self._checkpoint("Redimensionner annotation")
        self._push_undo("Redimensionner annotation")
        self._edit.annotations = [
            dict(updated) if a.get("id") == annotation_id else a for a in self._edit.annotations
        ]
        if self._annotation_selected_ids == {annotation_id}:
            self._load_style_into_controls(updated)
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_resize")

    def on_annotation_grouped(self, updated) -> None:
        """Reçu depuis le canvas après un Grouper/Dégrouper (menu contextuel)."""
        if not updated:
            return
        is_group = any(v.get("group") for v in updated.values())
        label = "annotation_group" if is_group else "annotation_ungroup"
        self._checkpoint(label)
        self._push_undo(label)
        self._edit.annotations = [
            dict(updated[a.get("id")]) if a.get("id") in updated else a for a in self._edit.annotations
        ]
        self.edits_changed.emit(copy.copy(self._edit))
        self._save("annotation_group" if is_group else "annotation_ungroup")
