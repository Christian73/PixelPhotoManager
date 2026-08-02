# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Dialogue « Cadre » du panneau de retouche.

Présente une galerie d'aperçus de LA photo en cours, un par motif de cadre, de
façon à choisir sur pièce plutôt que sur un nom. Les aperçus sont rendus dans un
QThread (règle « l'UI ne bloque jamais ») : la photo est décodée et réduite une
seule fois, puis chaque vignette n'est plus qu'un rendu de cadre (~10 ms).

Les cadres paramétriques (entourage uni, simple, double) ouvrent en plus une
section de réglages (style et couleurs, largeur extérieure, intervalle, largeur
intérieure) ; les vignettes concernées sont re-rendues à chaque modification, en
différé pour ne pas lancer un rendu par pas de curseur.

L'entourage uni propose un second cadre facultatif, peint par-dessus la photo
(case à cocher) : c'est le seul réglage du dialogue qui recouvre une partie de
l'image, d'où l'activation explicite.
"""
import copy
import logging

from PySide6.QtCore import Qt, QSize, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QDialog, QDialogButtonBox,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from src.core.models import EditInfo
from src.processing.frames import (
    COLOR_STYLES, FRAME_TYPES, INNER_MOTIFS, INNER_ORNAMENT_MAX,
    INNER_ORNAMENT_MIN, INNER_RELIEFS, ORNAMENTED_MOTIFS, PARAMETRIC_FRAMES,
    QUICK_COLORS, STYLED_FRAMES,
)
from src.ui.edit_icons import _TOGGLE_BTN_STYLE
from src.ui.edit_sliders import EditSlider

logger = logging.getLogger(__name__)

_TILE_PX = 132          # côté de l'aperçu photo (le cadre s'ajoute autour)
_TILE_COLS = 4
_PREVIEW_DEBOUNCE_MS = 180

_SELECTED_TILE_STYLE = (
    "QToolButton { background: #1a2a3a; border: 1px solid #2080a0; border-radius: 4px; }"
)
_TILE_STYLE = "QToolButton { border: 1px solid #3a3a3a; border-radius: 4px; }"


def _pil_to_qimage(img) -> QImage:
    """Conversion PIL → QImage utilisable hors du thread UI (copie détachée)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    # .copy() : QImage ne prend pas possession du buffer Python, qui serait
    # libéré dès le retour de cette fonction.
    return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()


class _TileLoader(QThread):
    """Rend les vignettes encadrées, hors thread UI.

    ``base`` (image PIL déjà réduite et retouchée, sans cadre) est réutilisée
    d'un lancement à l'autre : seul le premier rendu paie le décodage du
    fichier."""

    tile_ready = Signal(str, QImage)   # (identifiant du motif, aperçu)
    base_ready = Signal(object)        # image PIL de base, pour les rendus suivants

    def __init__(self, photo_path: str, edit: EditInfo, kinds: list,
                 base=None, parent=None) -> None:
        super().__init__(parent)
        self._photo_path = photo_path
        self._edit = copy.copy(edit)
        self._kinds = list(kinds)
        self._base = base
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _make_base(self):
        from PIL import Image, ImageOps
        from src.library.image_loader import open_image
        from src.processing.adjustments import ImageAdjuster

        with open_image(self._photo_path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((_TILE_PX * 3, _TILE_PX * 3), Image.LANCZOS)
            img = img.convert("RGB")
            base = copy.copy(self._edit)
            base.frame_type = "none"
            if base.is_modified():
                img = ImageAdjuster.apply_all(img, base)
            img.thumbnail((_TILE_PX, _TILE_PX), Image.LANCZOS)
            return img

    def run(self) -> None:
        from src.processing.frames import apply_frame
        try:
            base = self._base
            if base is None:
                base = self._make_base()
                self.base_ready.emit(base)
        except Exception as e:
            logger.error("Aperçu des cadres — image de base illisible (%s) : %s",
                         self._photo_path, e)
            return
        for kind in self._kinds:
            if self._cancelled:
                return
            try:
                if kind == "none":
                    img = base
                else:
                    e = copy.copy(self._edit)
                    e.frame_type = kind
                    img = apply_frame(base, e)
                self.tile_ready.emit(kind, _pil_to_qimage(img))
            except Exception as exc:
                logger.error("Aperçu du cadre %s impossible : %s", kind, exc)


class _ColorButton(QPushButton):
    """Pastille de couleur cliquable (ouvre le sélecteur de couleurs)."""

    color_changed = Signal(str)

    def __init__(self, color: str, tooltip: str, parent=None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(30, 24)
        self.setToolTip(tooltip)
        self.setCursor(Qt.PointingHandCursor)
        self._refresh()
        self.clicked.connect(self._pick)

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"QPushButton {{ background: {self._color}; border: 1px solid #777;"
            f" border-radius: 3px; }}"
        )

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color
        self._refresh()

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, self.toolTip())
        if chosen.isValid():
            self.set_color(chosen.name())
            self.color_changed.emit(self._color)


class FrameDialog(QDialog):
    """Choix d'un cadre décoratif et de ses réglages, avec aperçus en direct."""

    preview = Signal(object)   # EditInfo en temps réel

    def __init__(self, edit: EditInfo, photo_path: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self._edit = copy.copy(edit)
        self._photo_path = photo_path
        self._panel = None            # EditPanel — positionnement, cf. showEvent
        self._tiles: dict[str, QToolButton] = {}
        self._loader: _TileLoader | None = None
        self._base_image = None
        self._pending_kinds: list[str] = []

        self.setWindowTitle("Cadre")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(620)

        # Un seul rendu après une rafale de mouvements de curseur.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._refresh_timer.timeout.connect(self._refresh_parametric_tiles)

        self._setup_ui()
        QTimer.singleShot(0, self._start_gallery)

    # ------------------------------------------------------------------ UI

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 10)

        hint = QLabel("Choisissez un cadre — il se place autour de la photo, sans "
                      "recouvrir l'image (sauf le second cadre de l'entourage uni, "
                      "à activer explicitement).")
        hint.setStyleSheet("color: #999; font-size: 10px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ---- Galerie ----
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setSpacing(6)
        for idx, (kind, label) in enumerate(FRAME_TYPES):
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIconSize(QSize(_TILE_PX, _TILE_PX))
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setStyleSheet(_TILE_STYLE)
            btn.clicked.connect(lambda _checked=False, k=kind: self._select_kind(k))
            self._group.addButton(btn)
            self._tiles[kind] = btn
            grid.addWidget(btn, idx // _TILE_COLS, idx % _TILE_COLS)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_host)
        scroll.setMinimumHeight(_TILE_PX + 76)
        layout.addWidget(scroll, stretch=1)

        # ---- Réglages des cadres paramétriques ----
        self._params = QGroupBox("Réglages du cadre")
        pl = QVBoxLayout(self._params)
        pl.setContentsMargins(8, 10, 8, 8)
        pl.setSpacing(6)

        style_row = QHBoxLayout()
        style_row.setSpacing(6)
        style_row.addWidget(QLabel("Couleur :"))
        self._style_buttons: dict[str, QPushButton] = {}
        for style_id, style_label in COLOR_STYLES:
            btn = QPushButton(style_label)
            btn.setCheckable(True)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setChecked(self._edit.frame_style == style_id)
            btn.clicked.connect(lambda _checked=False, s=style_id: self._set_style(s))
            self._style_buttons[style_id] = btn
            style_row.addWidget(btn)

        self._btn_color = _ColorButton(self._edit.frame_color, "Couleur principale")
        self._btn_color.color_changed.connect(
            lambda c: self._set_attr("frame_color", c, reload_tiles=True))
        style_row.addWidget(self._btn_color)
        self._btn_color2 = _ColorButton(self._edit.frame_color2,
                                        "Seconde couleur (dégradé, éclats du pailleté)")
        self._btn_color2.color_changed.connect(
            lambda c: self._set_attr("frame_color2", c, reload_tiles=True))
        style_row.addWidget(self._btn_color2)
        # Raccourcis noir / blanc : les deux entourages les plus courants, sans
        # passer par le sélecteur de couleurs.
        for hex_value, label in QUICK_COLORS:
            btn = QPushButton(label)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setToolTip(f"Entourage {label.lower()}")
            btn.clicked.connect(
                lambda _checked=False, h=hex_value: self._set_main_color(h))
            style_row.addWidget(btn)
        style_row.addStretch()
        pl.addLayout(style_row)

        # Les largeurs sont des fractions du petit côté de la photo, exposées en
        # pourcentage (0,1 % de précision — 2 décimales sur une fraction ne
        # donneraient qu'un pas de 1 %, bien trop grossier).
        self._sl_width = EditSlider("Cadre extérieur", 0.5, 25.0,
                                    self._edit.frame_width * 100.0, 1)
        self._sl_width.value_changed.connect(
            lambda v: self._set_attr("frame_width", v / 100.0, reload_tiles=True))
        pl.addWidget(self._sl_width)

        # Second cadre de l'entourage uni : facultatif (il empiète sur la photo,
        # ce n'est pas un défaut acceptable sans un geste explicite).
        self._chk_inner = QCheckBox("Second cadre par-dessus la photo")
        self._chk_inner.setToolTip(
            "Ajoute un cadre intérieur peint sur l'image ; une bande de photo\n"
            "reste visible entre les deux cadres."
        )
        self._chk_inner.setChecked(bool(self._edit.frame_inner_enabled))
        self._chk_inner.toggled.connect(self._set_inner_enabled)
        pl.addWidget(self._chk_inner)

        # Intervalle + épaisseur : partagés par le cadre double et le second
        # cadre de l'entourage uni (mêmes réglages, libellés adaptés).
        self._inner_rows = QWidget()
        dl = QVBoxLayout(self._inner_rows)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(6)

        self._sl_gap = EditSlider("Intervalle", 0.0, 15.0,
                                  self._edit.frame_gap * 100.0, 1)
        self._sl_gap.value_changed.connect(
            lambda v: self._set_attr("frame_gap", v / 100.0, reload_tiles=True))
        dl.addWidget(self._sl_gap)

        self._sl_inner = EditSlider("Cadre intérieur", 0.0, 15.0,
                                    self._edit.frame_inner_width * 100.0, 1)
        self._sl_inner.value_changed.connect(
            lambda v: self._set_attr("frame_inner_width", v / 100.0, reload_tiles=True))
        dl.addWidget(self._sl_inner)

        # Ferronnerie du second cadre : motif, rendu (relief / aplat) et taille
        # des ornements. Réglages propres à l'entourage uni.
        self._inner_motif_rows = QWidget()
        il = QVBoxLayout(self._inner_motif_rows)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(6)

        motif_row = QHBoxLayout()
        motif_row.setSpacing(6)
        motif_row.addWidget(QLabel("Ferronnerie :"))
        self._motif_buttons: dict[str, QPushButton] = {}
        for motif_id, motif_label in INNER_MOTIFS:
            btn = QPushButton(motif_label)
            btn.setCheckable(True)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setChecked(self._edit.frame_inner_motif == motif_id)
            btn.clicked.connect(
                lambda _checked=False, m=motif_id: self._set_inner_motif(m))
            self._motif_buttons[motif_id] = btn
            motif_row.addWidget(btn)
        motif_row.addStretch()
        il.addLayout(motif_row)

        # Relief ou aplat : réglage de ferronnerie, sans effet sur la ligne
        # simple (rendue en aplat strict, cf. frames._draw_inner_overlay) — la
        # rangée est donc masquée avec le curseur d'ornements.
        self._relief_row = QWidget()
        relief_row = QHBoxLayout(self._relief_row)
        relief_row.setContentsMargins(0, 0, 0, 0)
        relief_row.setSpacing(6)
        relief_row.addWidget(QLabel("Rendu :"))
        self._relief_buttons: dict[bool, QPushButton] = {}
        for relief_value, relief_label in INNER_RELIEFS:
            btn = QPushButton(relief_label)
            btn.setCheckable(True)
            btn.setStyleSheet(_TOGGLE_BTN_STYLE)
            btn.setChecked(bool(self._edit.frame_inner_relief) == relief_value)
            btn.clicked.connect(
                lambda _checked=False, r=relief_value: self._set_inner_relief(r))
            self._relief_buttons[relief_value] = btn
            relief_row.addWidget(btn)
        relief_row.addStretch()
        il.addWidget(self._relief_row)

        # Échelle des ornements, en pourcentage (l'échelle interne d'EditSlider
        # est figée à 100 : un facteur 0,4-2,5 se règle donc de 40 à 250 %).
        self._sl_ornament = EditSlider("Ornements",
                                       INNER_ORNAMENT_MIN * 100.0,
                                       INNER_ORNAMENT_MAX * 100.0,
                                       self._edit.frame_inner_ornament * 100.0, 0)
        self._sl_ornament.value_changed.connect(
            lambda v: self._set_attr("frame_inner_ornament", v / 100.0, reload_tiles=True))
        il.addWidget(self._sl_ornament)

        self._double_rows = QWidget()          # couleurs propres au cadre double
        dcl = QVBoxLayout(self._double_rows)
        dcl.setContentsMargins(0, 0, 0, 0)
        dcl.setSpacing(6)

        inner_colors = QHBoxLayout()
        inner_colors.setSpacing(6)
        inner_colors.addWidget(QLabel("Intervalle :"))
        self._btn_gap_color = _ColorButton(self._edit.frame_gap_color,
                                           "Couleur de l'intervalle")
        self._btn_gap_color.color_changed.connect(
            lambda c: self._set_attr("frame_gap_color", c, reload_tiles=True))
        inner_colors.addWidget(self._btn_gap_color)
        inner_colors.addSpacing(12)
        inner_colors.addWidget(QLabel("Cadre intérieur :"))
        self._btn_inner_color = _ColorButton(self._edit.frame_inner_color,
                                             "Couleur du cadre intérieur")
        self._btn_inner_color.color_changed.connect(
            lambda c: self._set_attr("frame_inner_color", c, reload_tiles=True))
        inner_colors.addWidget(self._btn_inner_color)
        inner_colors.addStretch()
        dcl.addLayout(inner_colors)

        pl.addWidget(self._inner_rows)
        pl.addWidget(self._inner_motif_rows)
        pl.addWidget(self._double_rows)
        layout.addWidget(self._params)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Valider")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        current = self._edit.frame_type if self._edit.frame_type in self._tiles else "none"
        self._tiles[current].setChecked(True)
        self._apply_tile_styles(current)
        self._update_params_visibility()

    def _apply_tile_styles(self, selected: str) -> None:
        for kind, btn in self._tiles.items():
            btn.setStyleSheet(_SELECTED_TILE_STYLE if kind == selected else _TILE_STYLE)

    # ------------------------------------------------------------------ aperçus

    def _start_gallery(self, kinds: list | None = None) -> None:
        """Lance (ou relance) le rendu des vignettes demandées."""
        if not self._photo_path:
            return
        if self._loader is not None and self._loader.isRunning():
            # Une demande plus récente prime : la précédente s'arrête entre deux
            # vignettes, et c'est sa fin qui déclenchera celle-ci.
            self._pending_kinds = kinds or [k for k, _ in FRAME_TYPES]
            self._loader.cancel()
            return
        loader = _TileLoader(self._photo_path, self._edit,
                             kinds or [k for k, _ in FRAME_TYPES],
                             base=self._base_image, parent=self)
        loader.base_ready.connect(self._on_base_ready)
        loader.tile_ready.connect(self._on_tile_ready)
        loader.finished.connect(self._on_loader_finished)
        self._loader = loader
        loader.start()

    def _on_loader_finished(self) -> None:
        self._loader = None
        if self._pending_kinds:
            kinds, self._pending_kinds = self._pending_kinds, []
            self._start_gallery(kinds)

    def _on_base_ready(self, image) -> None:
        self._base_image = image

    def _on_tile_ready(self, kind: str, image: QImage) -> None:
        btn = self._tiles.get(kind)
        if btn is None:
            return
        pix = QPixmap.fromImage(image)
        btn.setIcon(QIcon(pix))
        # L'icône garde ses proportions : la vignette encadrée est plus grande
        # que la photo seule, on dimensionne donc d'après ce qui a été rendu.
        btn.setIconSize(pix.size())

    def _refresh_parametric_tiles(self) -> None:
        self._start_gallery([k for k in PARAMETRIC_FRAMES])

    # ------------------------------------------------------------------ réglages

    def _select_kind(self, kind: str) -> None:
        self._edit.frame_type = kind
        self._apply_tile_styles(kind)
        self._update_params_visibility()
        self.preview.emit(copy.copy(self._edit))

    def _set_inner_enabled(self, enabled: bool) -> None:
        self._edit.frame_inner_enabled = bool(enabled)
        self._update_params_visibility()
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_inner_motif(self, motif: str) -> None:
        self._edit.frame_inner_motif = motif
        for m, btn in self._motif_buttons.items():
            btn.setChecked(m == motif)
        self._update_params_visibility()
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_inner_relief(self, relief: bool) -> None:
        self._edit.frame_inner_relief = bool(relief)
        for value, btn in self._relief_buttons.items():
            btn.setChecked(value == bool(relief))
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_main_color(self, hex_value: str) -> None:
        """Couleur principale imposée (raccourcis noir / blanc)."""
        self._btn_color.set_color(hex_value)
        self._set_attr("frame_color", hex_value, reload_tiles=True)

    def _set_style(self, style: str) -> None:
        self._edit.frame_style = style
        for s, btn in self._style_buttons.items():
            btn.setChecked(s == style)
        self.preview.emit(copy.copy(self._edit))
        self._refresh_timer.start()

    def _set_attr(self, attr: str, value, reload_tiles: bool = False) -> None:
        setattr(self._edit, attr, value)
        self.preview.emit(copy.copy(self._edit))
        if reload_tiles:
            self._refresh_timer.start()

    def _update_params_visibility(self) -> None:
        kind = self._edit.frame_type
        parametric = kind in PARAMETRIC_FRAMES
        self._params.setVisible(parametric)
        self._double_rows.setVisible(kind == "double")
        # « Entourage uni » est un aplat d'une seule couleur : ni style de
        # remplissage, ni seconde couleur.
        styled = kind in STYLED_FRAMES
        for btn in self._style_buttons.values():
            btn.setVisible(styled)
        self._btn_color2.setVisible(styled)
        self._sl_width.set_label("Cadre extérieur" if styled else "Épaisseur")
        # Le second cadre est proposé (et donc réglable) pour le seul entourage uni ;
        # pour le cadre double, l'intervalle et le cadre intérieur font partie du motif.
        plain = kind == "plain"
        inner_on = plain and bool(self._edit.frame_inner_enabled)
        self._chk_inner.setVisible(plain)
        self._inner_rows.setVisible(kind == "double" or inner_on)
        self._sl_inner.set_label("Second cadre" if plain else "Cadre intérieur")
        # La ferronnerie n'a de sens que sur le second cadre ; la ligne simple
        # n'a aucun ornement à dimensionner.
        self._inner_motif_rows.setVisible(inner_on)
        ornamented = inner_on and self._edit.frame_inner_motif in ORNAMENTED_MOTIFS
        self._relief_row.setVisible(ornamented)
        self._sl_ornament.setVisible(ornamented)
        self.adjustSize()
        QTimer.singleShot(0, self._reposition)

    # ------------------------------------------------------------------ divers

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._panel is not None:
            pos = self._panel._compute_dialog_pos(self.width(), self.height())
            QTimer.singleShot(0, lambda: self.move(pos))

    def _reposition(self) -> None:
        if self._panel is not None:
            self.move(self._panel._compute_dialog_pos(self.width(), self.height()))

    def closeEvent(self, event) -> None:
        # Ne jamais laisser un QThread tourner après la fermeture du dialogue :
        # il émettrait vers des widgets détruits.
        self._refresh_timer.stop()
        if self._loader is not None:
            self._pending_kinds = []
            self._loader.cancel()
            self._loader.wait(3000)
            self._loader = None
        super().closeEvent(event)

    def get_edit(self) -> EditInfo:
        return self._edit
