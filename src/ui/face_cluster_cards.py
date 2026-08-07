# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Cartes et sections de la grille de groupes de visages (extraites de
face_cluster_grid.py)."""
"""
FaceClusterGrid — grille des groupes de visages non identifiés.

Affichée dans la zone principale à la place de la grille de photos.
1 clic  : sélectionner / désélectionner un groupe (multi-sélection cumulative).
2 clics : ouvrir les photos du groupe.
Barre d'action (visible dès qu'1+ groupes sont sélectionnés) :
  • Voir les photos    (1 seul groupe sélectionné)
  • Associer à…       (ouvrir le dialogue d'assignation pour tous les groupes)
  • Ignorer           (ignorer tous les groupes sélectionnés)
  • ✕ Désélectionner  (vider la sélection)
"""

import logging

from PySide6.QtCore import Qt, QPoint, QRect, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMenu, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.ui_utils import install_menu_width_fix
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _cosine_sim, _SIM_WEAK, _SIM_STRONG,
)
from src.core.i18n import translate

logger = logging.getLogger(__name__)

_CARD_IMG     = 130
_CARD_W       = 148
_CARD_SPACING  = 10
_COLS_MIN      = 2
_SIM_GROUP     = 0.72   # seuil pour regrouper deux clusters "même personne probable"
_BUILD_BATCH   = 10     # cartes créées par tick de l'event loop (évite de bloquer l'UI)
_PAGE_SIZE     = 200    # nombre de cartes rendues par page (pagination)
_UF_CHUNK      = 500    # lignes par bloc dans le produit matriciel de l'Union-Find
                        # RAM pic ≈ _UF_CHUNK × n × 4 octets  (500 × 50k × 4 = 100 Mo)
UNION_FIND_MAX = 80_000 # skip UF au-delà (temps > 2 min même en mode blocs)


_BTN_OVL = 22   # diamètre des boutons ✓/✗ superposés sur la vignette (cf. PersonClusterView)
_BTN_ACCEPT_STYLE = (
    "QPushButton { background: rgba(30,150,50,215); color: white;"
    " border-radius: 11px; font-weight: bold; font-size: 13px; border: none; padding: 0; }"
    "QPushButton:hover { background: rgba(50,200,70,255); }"
)
_BTN_REJECT_STYLE = (
    "QPushButton { background: rgba(170,30,30,215); color: white;"
    " border-radius: 11px; font-weight: bold; font-size: 13px; border: none; padding: 0; }"
    "QPushButton:hover { background: rgba(220,50,50,255); }"
)


# ------------------------------------------------------------------ helpers (module-level, utilisés par le thread)


class _ClusterCard(QFrame):
    """
    Carte représentant un groupe de visages.

    1 clic         → sélection alternée (selection_toggled)
    Maj+1 clic     → sélection étendue depuis l'ancre (range_select_requested)
    2 clics        → ouvrir les photos (view_requested)
    Clic droit     → menu contextuel (nommer / fusionner / ignorer / associer si multi-sélection)
    """

    selection_toggled    = Signal(int, bool)  # cluster_id, is_selected
    range_select_requested = Signal(int)      # cluster_id (Maj+clic)
    view_requested       = Signal(int)
    name_requested       = Signal(int)
    quick_accept_requested = Signal(int, int)   # cluster_id, person_id — accepter la suggestion sans dialogue
    merge_requested      = Signal(int)
    associate_requested  = Signal()           # fusionner tous les groupes sélectionnés ensemble
    ignore_requested     = Signal(int)
    ignore_selection_requested = Signal()     # ignorer tous les groupes/visages sélectionnés
    eject_from_section_requested = Signal(int)  # cluster_id

    _STYLE_NORMAL = """
        QFrame {
            border: 2px solid #3a3a3a;
            border-radius: 6px;
            background: #252525;
        }
        QFrame:hover {
            border-color: #7aabdb;
            background: #2a3545;
        }
    """
    _STYLE_SELECTED = """
        QFrame {
            border: 2px solid #5a9fd4;
            border-radius: 6px;
            background: #1e3a5a;
        }
    """

    def __init__(
        self,
        cluster_id: int,
        face_count: int,
        suggested_person_id: int | None,
        suggestion_label: str,
        suggestion_color: str,
        is_solo: bool = False,
        show_eject: bool = False,
        selected_ids_ref: "set[int] | None" = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cluster_id          = cluster_id
        self._suggested_person_id = suggested_person_id
        self._is_solo             = is_solo
        self._is_selected         = False
        # Référence directe vers l'ensemble des cluster_id sélectionnés dans la
        # grille parente, pour savoir au clic droit si une multi-sélection est
        # en cours (afficher "Associer") sans devoir répliquer l'état ailleurs.
        self._selected_ids_ref    = selected_ids_ref

        self.setFixedWidth(_CARD_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setAccessibleName(f"facecluster::{cluster_id}")
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._STYLE_NORMAL)
        if is_solo:
            self.setToolTip(translate("ClusterCard", "Clic : sélectionner  —  Double-clic : voir la photo  —  Clic droit : identifier / ignorer"))
        else:
            self.setToolTip(translate("ClusterCard", "Clic : sélectionner  —  Double-clic : voir les photos  —  Clic droit : identifier / fusionner / ignorer"))

        col = QVBoxLayout(self)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(4)
        col.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self._lbl_img = LoadingLabel("#1a1a1a")
        self._lbl_img.setFixedSize(_CARD_IMG, _CARD_IMG)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet("border: none; border-radius: 4px;")
        self._lbl_img.start_loading()
        col.addWidget(self._lbl_img, alignment=Qt.AlignHCenter)

        if not is_solo:
            lbl_count = QLabel(translate("ClusterCard", "%n visage(s)", None, face_count))
            lbl_count.setAlignment(Qt.AlignCenter)
            lbl_count.setStyleSheet("border: none; font-size: 11px; color: #aaa;")
            col.addWidget(lbl_count)

        if suggestion_label:
            lbl_sugg = QLabel(suggestion_label)
            lbl_sugg.setAlignment(Qt.AlignCenter)
            lbl_sugg.setWordWrap(True)
            lbl_sugg.setStyleSheet(
                f"border: none; font-size: 10px; color: {suggestion_color};"
            )
            col.addWidget(lbl_sugg)

        # Boutons ✓/✗ superposés sur la vignette, sur chaque carte (isolée, groupe
        # ou avec suggestion) — mêmes actions que le menu contextuel (Identifier…
        # / Ignorer), sur le même principe visuel que PersonClusterView.
        _y = _CARD_IMG - _BTN_OVL - 3
        btn_name = QPushButton("✓", self._lbl_img)
        btn_name.setGeometry(_CARD_IMG - _BTN_OVL - 3, _y, _BTN_OVL, _BTN_OVL)
        btn_name.setStyleSheet(_BTN_ACCEPT_STYLE)
        btn_name.setCursor(Qt.PointingHandCursor)
        btn_name.setToolTip(translate("ClusterCard", "Identifier ce visage…") if is_solo
                            else translate("ClusterCard", "Identifier cette personne…"))
        btn_name.clicked.connect(self._on_accept_clicked)

        btn_ignore = QPushButton("✗", self._lbl_img)
        btn_ignore.setGeometry(3, _y, _BTN_OVL, _BTN_OVL)
        btn_ignore.setStyleSheet(_BTN_REJECT_STYLE)
        btn_ignore.setCursor(Qt.PointingHandCursor)
        btn_ignore.setToolTip(translate("ClusterCard", "Ignorer ce visage") if is_solo
                              else translate("ClusterCard", "Ignorer ce groupe"))
        btn_ignore.clicked.connect(lambda: self.ignore_requested.emit(self._cluster_id))

        if show_eject:
            btn_eject = QPushButton(translate("ClusterCard", "✕ Retirer du groupe"))
            btn_eject.setFixedHeight(18)
            btn_eject.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid #555;"
                " border-radius: 3px; color: #888; font-size: 9px; padding: 0 4px; }"
                "QPushButton:hover { border-color: #ba5d5d; color: #ba5d5d; }"
            )
            btn_eject.setToolTip(translate("ClusterCard", "Retirer ce groupe de la suggestion de personne"))
            btn_eject.clicked.connect(
                lambda: self.eject_from_section_requested.emit(self._cluster_id)
            )
            col.addWidget(btn_eject)

    def set_avatar(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(
            _CARD_IMG, _CARD_IMG,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._lbl_img.setPixmap(scaled)

    def set_suggestion(
        self,
        sugg_id: "int | None",
        label: str,
        color: str,
    ) -> None:
        """Met à jour (ou crée) le label de suggestion sans recréer la carte."""
        self._suggested_person_id = sugg_id
        if not hasattr(self, "_lbl_sugg"):
            self._lbl_sugg = QLabel()
            self._lbl_sugg.setAlignment(Qt.AlignCenter)
            self._lbl_sugg.setWordWrap(True)
            self.layout().addWidget(self._lbl_sugg)
        if label:
            self._lbl_sugg.setText(label)
            self._lbl_sugg.setStyleSheet(
                f"border: none; font-size: 10px; color: {color};"
            )
            self._lbl_sugg.setVisible(True)
        else:
            self._lbl_sugg.setVisible(False)

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self.setStyleSheet(self._STYLE_SELECTED if selected else self._STYLE_NORMAL)

    def _on_accept_clicked(self) -> None:
        # Suggestion présente : accepter directement sans passer par le dialogue.
        # Sinon : ouvrir le dialogue d'identification classique. `_suggested_person_id`
        # peut avoir été mis à jour après coup par set_suggestion(), d'où la lecture
        # au moment du clic plutôt qu'à la construction de la carte.
        if self._suggested_person_id is not None:
            self.quick_accept_requested.emit(self._cluster_id, self._suggested_person_id)
        else:
            self.name_requested.emit(self._cluster_id)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ShiftModifier:
                self.range_select_requested.emit(self._cluster_id)
            else:
                self._is_selected = not self._is_selected
                self.set_selected(self._is_selected)
                self.selection_toggled.emit(self._cluster_id, self._is_selected)
        elif event.button() == Qt.RightButton:
            n_selected = len(self._selected_ids_ref) if self._selected_ids_ref else 0
            bulk = self._is_selected and n_selected > 1
            menu = QMenu(self)
            install_menu_width_fix(menu)
            act_associate = None
            if bulk:
                act_associate = menu.addAction(translate(
                    "ClusterCard", "Associer ({n} sélectionnés)").format(n=n_selected))
                menu.addSeparator()
            if self._is_solo:
                act_name = menu.addAction(translate("ClusterCard", "Identifier ce visage…"))
                menu.addSeparator()
                act_ignore = menu.addAction(translate("ClusterCard", "Ignorer ces visages") if bulk
                                            else translate("ClusterCard", "Ignorer ce visage"))
                act_merge  = None
            else:
                act_name   = menu.addAction(translate("ClusterCard", "Identifier cette personne…"))
                act_merge  = None
                menu.addSeparator()
                act_ignore = menu.addAction(translate("ClusterCard", "Ignorer ces groupes") if bulk
                                            else translate("ClusterCard", "Ignorer ce groupe"))
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen == act_associate:
                self.associate_requested.emit()
            elif chosen == act_name:
                self.name_requested.emit(self._cluster_id)
            elif chosen == act_ignore:
                if bulk:
                    self.ignore_selection_requested.emit()
                else:
                    self.ignore_requested.emit(self._cluster_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.view_requested.emit(self._cluster_id)
        super().mouseDoubleClickEvent(event)


# ------------------------------------------------------------------ merge dialog


class _SectionWidget(QFrame):
    """Un groupe de clusters visuellement similaires, avec un en-tête optionnel."""

    accept_requested = Signal(list, int)  # cluster_ids, person_id
    assign_requested = Signal(list)       # cluster_ids
    ignore_requested = Signal(list)       # cluster_ids

    def __init__(
        self, label: str, color: str,
        suggested_person_id: "int | None" = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._suggested_person_id = suggested_person_id
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 10, 0, 4)
        outer.setSpacing(4)

        if label:
            hdr_row = QHBoxLayout()
            hdr_row.setSpacing(8)
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: bold; border: none;"
            )
            hdr_row.addWidget(lbl)
            hdr_row.addStretch()

            _bs = "QPushButton { padding: 1px 8px; font-size: 11px; }"
            if suggested_person_id is not None:
                btn_accept = QPushButton(translate("SectionWidget", "Accepter"))
                btn_accept.setFixedHeight(22)
                btn_accept.setStyleSheet(_bs)
                btn_accept.setToolTip(translate("SectionWidget", "Assigner tous les groupes à la personne suggérée"))
                btn_accept.clicked.connect(
                    lambda: self.accept_requested.emit(
                        [c for c, _ in self._entries], self._suggested_person_id
                    )
                )
                hdr_row.addWidget(btn_accept)

            btn_assign = QPushButton(translate("SectionWidget", "Associer à…"))
            btn_assign.setFixedHeight(22)
            btn_assign.setStyleSheet(_bs)
            btn_assign.setToolTip(translate("SectionWidget", "Assigner tous les groupes à une autre personne"))
            btn_assign.clicked.connect(
                lambda: self.assign_requested.emit([c for c, _ in self._entries])
            )
            hdr_row.addWidget(btn_assign)

            btn_ignore = QPushButton(translate("SectionWidget", "Ignorer"))
            btn_ignore.setFixedHeight(22)
            btn_ignore.setStyleSheet(_bs)
            btn_ignore.setToolTip(translate("SectionWidget", "Ignorer tous les groupes de cette section"))
            btn_ignore.clicked.connect(
                lambda: self.ignore_requested.emit([c for c, _ in self._entries])
            )
            hdr_row.addWidget(btn_ignore)

            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet(f"background: {color}; border: none;")
            sep.setFixedHeight(1)
            outer.addWidget(sep)

            outer.addLayout(hdr_row)

        self._card_area = QWidget()
        self._card_area.setStyleSheet("background: transparent;")
        self._card_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._card_gl = QGridLayout(self._card_area)
        self._card_gl.setSpacing(_CARD_SPACING)
        self._card_gl.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card_area)

        self._entries: list[tuple[int, "_ClusterCard"]] = []

    def add_card(self, cluster_id: int, card: "_ClusterCard") -> None:
        self._entries.append((cluster_id, card))

    def reflow(self, cols: int) -> None:
        while self._card_gl.count():
            self._card_gl.takeAt(0)
        # Réinitialiser les stretches des colonnes précédentes
        for c in range(self._card_gl.columnCount() + cols + 1):
            self._card_gl.setColumnStretch(c, 0)
        # Colonne fantôme à droite : absorbe l'espace libre → cartes alignées à gauche
        self._card_gl.setColumnStretch(cols, 1)
        for i, (_, card) in enumerate(self._entries):
            self._card_gl.addWidget(card, i // cols, i % cols, Qt.AlignLeft | Qt.AlignTop)


# ------------------------------------------------------------------ refresh thread


