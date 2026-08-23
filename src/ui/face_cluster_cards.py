# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Cards and sections of the face group grid (extracted from
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
_SIM_GROUP     = 0.72   # threshold to group two "probably the same person" clusters
_BUILD_BATCH   = 10     # cards created per event loop tick (avoids blocking the UI)
_PAGE_SIZE     = 200    # number of cards rendered per page (pagination)
_UF_CHUNK      = 500    # rows per block in the matrix product of the Union-Find
                        # RAM pic ≈ _UF_CHUNK × n × 4 octets  (500 × 50k × 4 = 100 Mo)
UNION_FIND_MAX = 80_000 # skip the UF beyond that (> 2 min even in block mode)


_BTN_OVL = 22   # diameter of the ✓/✗ buttons overlaid on the thumbnail (cf. PersonClusterView)
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


# ------------------------------------------------------------------ helpers (module-level, used by the thread)


class _ClusterCard(QFrame):
    """
    Card representing a group of faces.

    1 click        → toggled selection (selection_toggled)
    Shift+1 click  → selection extended from the anchor (range_select_requested)
    2 clicks       → open the photos (view_requested)
    Right click    → context menu (name / merge / ignore / associate if multi-selection)
    """

    selection_toggled    = Signal(int, bool)  # cluster_id, is_selected
    range_select_requested = Signal(int)      # cluster_id (Maj+clic)
    view_requested       = Signal(int)
    name_requested       = Signal(int)
    quick_accept_requested = Signal(int, int)   # cluster_id, person_id — accept the suggestion without a dialog
    merge_requested      = Signal(int)
    associate_requested  = Signal()           # merge every selected group together
    ignore_requested     = Signal(int)
    ignore_selection_requested = Signal()     # ignore every selected group/face
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
        # A direct reference to the set of cluster_ids selected in the parent
        # grid, to know on a right click whether a multi-selection is in progress
        # (showing "Associate") without having to replicate the state elsewhere.
        self._selected_ids_ref    = selected_ids_ref

        self.setFixedWidth(_CARD_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setAccessibleName(f"facecluster::{cluster_id}")
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._STYLE_NORMAL)
        if is_solo:
            self.setToolTip(translate("ClusterCard", "Click: select  —  Double-click: view the "
                                                     "photo  —  Right click: identify / ignore"))
        else:
            self.setToolTip(translate("ClusterCard", "Click: select  —  Double-click: view the "
                                                     "photos  —  Right click: identify / merge "
                                                     "/ ignore"))

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
            lbl_count = QLabel(translate("ClusterCard", "%n face(s)", None, face_count))
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

        # ✓/✗ buttons overlaid on the thumbnail, on every card (isolated, group or
        # with a suggestion) — the same actions as the context menu (Identify… /
        # Ignore), on the same visual principle as PersonClusterView.
        _y = _CARD_IMG - _BTN_OVL - 3
        btn_name = QPushButton("✓", self._lbl_img)
        btn_name.setGeometry(_CARD_IMG - _BTN_OVL - 3, _y, _BTN_OVL, _BTN_OVL)
        btn_name.setStyleSheet(_BTN_ACCEPT_STYLE)
        btn_name.setCursor(Qt.PointingHandCursor)
        btn_name.setToolTip(translate("ClusterCard", "Identify this face…") if is_solo
                            else translate("ClusterCard", "Identify this person…"))
        btn_name.clicked.connect(self._on_accept_clicked)

        btn_ignore = QPushButton("✗", self._lbl_img)
        btn_ignore.setGeometry(3, _y, _BTN_OVL, _BTN_OVL)
        btn_ignore.setStyleSheet(_BTN_REJECT_STYLE)
        btn_ignore.setCursor(Qt.PointingHandCursor)
        btn_ignore.setToolTip(translate("ClusterCard", "Ignore this face") if is_solo
                              else translate("ClusterCard", "Ignore this group"))
        btn_ignore.clicked.connect(lambda: self.ignore_requested.emit(self._cluster_id))

        if show_eject:
            btn_eject = QPushButton(translate("ClusterCard", "✕ Remove from the group"))
            btn_eject.setFixedHeight(18)
            btn_eject.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid #555;"
                " border-radius: 3px; color: #888; font-size: 9px; padding: 0 4px; }"
                "QPushButton:hover { border-color: #ba5d5d; color: #ba5d5d; }"
            )
            btn_eject.setToolTip(translate("ClusterCard", "Remove this group from the person "
                                                          "suggestion"))
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
        """Updates (or creates) the suggestion label without recreating the card."""
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
        # A suggestion is present: accept it directly without going through the
        # dialog. Otherwise: open the classic identification dialog.
        # `_suggested_person_id` may have been updated afterwards by
        # set_suggestion(), hence the read at click time rather than at card
        # construction time.
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
                    "ClusterCard", "Assign ({n} selected)").format(n=n_selected))
                menu.addSeparator()
            if self._is_solo:
                act_name = menu.addAction(translate("ClusterCard", "Identify this face…"))
                menu.addSeparator()
                act_ignore = menu.addAction(translate("ClusterCard", "Ignore these faces") if bulk
                                            else translate("ClusterCard", "Ignore this face"))
                act_merge  = None
            else:
                act_name   = menu.addAction(translate("ClusterCard", "Identify this person…"))
                act_merge  = None
                menu.addSeparator()
                act_ignore = menu.addAction(translate("ClusterCard", "Ignore these groups") if bulk
                                            else translate("ClusterCard", "Ignore this group"))
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
    """A group of visually similar clusters, with an optional header."""

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
                btn_accept = QPushButton(translate("SectionWidget", "Accept"))
                btn_accept.setFixedHeight(22)
                btn_accept.setStyleSheet(_bs)
                btn_accept.setToolTip(translate("SectionWidget", "Assign every group to the "
                                                                 "suggested person"))
                btn_accept.clicked.connect(
                    lambda: self.accept_requested.emit(
                        [c for c, _ in self._entries], self._suggested_person_id
                    )
                )
                hdr_row.addWidget(btn_accept)

            btn_assign = QPushButton(translate("SectionWidget", "Assign to…"))
            btn_assign.setFixedHeight(22)
            btn_assign.setStyleSheet(_bs)
            btn_assign.setToolTip(translate("SectionWidget", "Assign every group to another "
                                                             "person"))
            btn_assign.clicked.connect(
                lambda: self.assign_requested.emit([c for c, _ in self._entries])
            )
            hdr_row.addWidget(btn_assign)

            btn_ignore = QPushButton(translate("SectionWidget", "Ignore"))
            btn_ignore.setFixedHeight(22)
            btn_ignore.setStyleSheet(_bs)
            btn_ignore.setToolTip(translate("SectionWidget", "Ignore every group in this "
                                                             "section"))
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
        # Reset the stretches of the previous columns
        for c in range(self._card_gl.columnCount() + cols + 1):
            self._card_gl.setColumnStretch(c, 0)
        # A phantom column on the right: absorbs the free space → cards aligned left
        self._card_gl.setColumnStretch(cols, 1)
        for i, (_, card) in enumerate(self._entries):
            self._card_gl.addWidget(card, i // cols, i % cols, Qt.AlignLeft | Qt.AlignTop)


# ------------------------------------------------------------------ refresh thread


