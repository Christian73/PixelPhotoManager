"""
FaceClusterGrid — grille des groupes de visages non identifiés.

Affichée dans la zone principale à la place de la grille de photos.
1 clic  : sélectionner / désélectionner un groupe (multi-sélection cumulative).
2 clics : ouvrir le dialogue de nommage du groupe.
Barre d'action (visible dès qu'1+ groupes sont sélectionnés) :
  • Voir les photos    (1 seul groupe sélectionné)
  • Associer à…       (ouvrir le dialogue d'assignation pour tous les groupes)
  • Ignorer           (ignorer tous les groupes sélectionnés)
  • ✕ Désélectionner  (vider la sélection)
"""

import logging

from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _cosine_sim, _SIM_WEAK,
)

logger = logging.getLogger(__name__)

_CARD_IMG     = 130
_CARD_W       = 148
_CARD_SPACING = 10
_COLS_MIN     = 2


# ------------------------------------------------------------------ card

class _ClusterCard(QFrame):
    """
    Carte représentant un groupe de visages.

    1 clic  → sélection alternée (selection_toggled)
    2 clics → ouvrir le dialogue de nommage (name_requested)
    Clic droit → menu contextuel (nommer / fusionner / ignorer)
    """

    selection_toggled = Signal(int, bool)  # cluster_id, is_selected
    name_requested    = Signal(int)
    merge_requested   = Signal(int)
    ignore_requested  = Signal(int)

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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cluster_id          = cluster_id
        self._suggested_person_id = suggested_person_id
        self._is_selected         = False

        self.setFixedWidth(_CARD_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._STYLE_NORMAL)
        self.setToolTip("Clic : sélectionner  —  Double-clic : identifier  —  Clic droit : options")

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

        plural = "s" if face_count > 1 else ""
        lbl_count = QLabel(f"{face_count} visage{plural}")
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

    def set_avatar(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(
            _CARD_IMG, _CARD_IMG,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._lbl_img.setPixmap(scaled)

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self.setStyleSheet(self._STYLE_SELECTED if selected else self._STYLE_NORMAL)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._is_selected = not self._is_selected
            self.set_selected(self._is_selected)
            self.selection_toggled.emit(self._cluster_id, self._is_selected)
        elif event.button() == Qt.RightButton:
            menu = QMenu(self)
            act_name   = menu.addAction("Identifier ce groupe…")
            act_merge  = menu.addAction("Fusionner avec un autre groupe…")
            menu.addSeparator()
            act_ignore = menu.addAction("Ignorer ce groupe")
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen == act_name:
                self.name_requested.emit(self._cluster_id)
            elif chosen == act_merge:
                self.merge_requested.emit(self._cluster_id)
            elif chosen == act_ignore:
                self.ignore_requested.emit(self._cluster_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.name_requested.emit(self._cluster_id)
        super().mouseDoubleClickEvent(event)


# ------------------------------------------------------------------ merge dialog

class _MergeRow(QFrame):
    selected = Signal(int)

    def __init__(self, cluster_id: int, face_count: int, parent=None) -> None:
        super().__init__(parent)
        self._cluster_id  = cluster_id
        self._is_selected = False
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(54)
        self._apply_style()

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(10)

        self._lbl_avatar = LoadingLabel("#2a2a2a")
        self._lbl_avatar.setFixedSize(40, 40)
        self._lbl_avatar.setAlignment(Qt.AlignCenter)
        self._lbl_avatar.setStyleSheet("border-radius: 20px; border: none;")
        self._lbl_avatar.start_loading()
        row.addWidget(self._lbl_avatar)

        plural = "s" if face_count > 1 else ""
        lbl = QLabel(f"Groupe {cluster_id}  —  {face_count} visage{plural}")
        lbl.setStyleSheet("border: none; color: #ddd;")
        row.addWidget(lbl, stretch=1)

    def set_avatar(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(40, 40, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        self._lbl_avatar.setPixmap(scaled)

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self._apply_style()

    def _apply_style(self) -> None:
        if self._is_selected:
            self.setStyleSheet("QFrame { background: #1e3a5f; border-radius: 4px; }")
        else:
            self.setStyleSheet(
                "QFrame { background: transparent; border-radius: 4px; }"
                "QFrame:hover { background: #2a2a2a; }"
            )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.selected.emit(self._cluster_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.selected.emit(self._cluster_id)
        super().mouseDoubleClickEvent(event)


class _MergePickerDialog(QDialog):
    def __init__(self, source_cluster_id: int, face_db: FaceDatabase, parent=None) -> None:
        super().__init__(parent)
        self._source_id  = source_cluster_id
        self._face_db    = face_db
        self._target_id: int | None = None
        self._rows: dict[int, _MergeRow] = {}
        self._loader = None

        self.setWindowTitle(f"Fusionner le groupe {source_cluster_id}")
        self.setMinimumSize(340, 420)
        self._build()
        QTimer.singleShot(0, self._start_loader)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        lbl = QLabel(f"Fusionner le groupe {self._source_id} avec :")
        lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(lbl)

        content = QWidget()
        vbox = QVBoxLayout(content)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(2)
        vbox.setAlignment(Qt.AlignTop)

        for cid, count in self._face_db.get_unnamed_clusters():
            if cid == self._source_id:
                continue
            row = _MergeRow(cid, count)
            row.selected.connect(self._on_row_selected)
            vbox.addWidget(row)
            self._rows[cid] = row

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setStyleSheet("border: 1px solid #333;")
        layout.addWidget(scroll, stretch=1)

        if not self._rows:
            lbl_empty = QLabel("Aucun autre groupe disponible.")
            lbl_empty.setAlignment(Qt.AlignCenter)
            lbl_empty.setStyleSheet("color: #555;")
            vbox.addWidget(lbl_empty)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._btn_ok = buttons.button(QDialogButtonBox.Ok)
        self._btn_ok.setText("Fusionner")
        self._btn_ok.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_row_selected(self, cluster_id: int) -> None:
        for cid, row in self._rows.items():
            row.set_selected(cid == cluster_id)
        self._target_id = cluster_id
        self._btn_ok.setEnabled(True)

    def _start_loader(self) -> None:
        items = []
        for cid in self._rows:
            rep = self._face_db.get_representative_face(cluster_id=cid)
            if rep:
                items.append((cid, rep))
        if items:
            self._loader = _AvatarLoader(items, 40, self)
            self._loader.avatar_ready.connect(self._on_avatar_ready)
            self._loader.start()

    def _on_avatar_ready(self, cluster_id: int, data: bytes) -> None:
        row = self._rows.get(cluster_id)
        if row:
            row.set_avatar(data)

    def selected_cluster_id(self) -> int | None:
        return self._target_id


# ------------------------------------------------------------------ grid

class FaceClusterGrid(QWidget):
    """
    Zone principale affichant les groupes de visages non identifiés.

    Signals
    -------
    cluster_named(cluster_id, name)           — créer une personne et l'assigner
    cluster_assigned(cluster_id, pid)         — assigner à une personne existante
    clusters_named(cluster_ids, name)         — créer une personne pour N groupes
    clusters_assigned(cluster_ids, pid)       — assigner N groupes à une personne
    cluster_ignored(cluster_id)               — ignorer un groupe
    cluster_merged(source_id, target_id)      — fusionner deux groupes
    back_requested()                          — retourner à la grille de photos
    photos_requested(cluster_id, label)       — afficher les photos d'un groupe
    """

    cluster_named      = Signal(int, str)
    cluster_assigned   = Signal(int, int)
    clusters_named     = Signal(list, str)    # list[int], name
    clusters_assigned  = Signal(list, int)    # list[int], person_id
    cluster_ignored    = Signal(int)
    cluster_merged     = Signal(int, int)
    back_requested     = Signal()
    photos_requested   = Signal(int, str)

    def __init__(self, face_db: FaceDatabase, catalog, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._cards:         dict[int, _ClusterCard] = {}
        self._ordered_cards: list[tuple[int, _ClusterCard]] = []
        self._current_cols:  int = _COLS_MIN
        self._persons: list[PersonInfo] = []
        self._person_embeddings: dict[int, list[float]] = {}
        self._loader  = None
        self._selected_ids: set[int] = set()
        self._setup_ui()

    # ------------------------------------------------------------------ setup

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(6)

        # Barre de titre
        bar = QHBoxLayout()
        btn_back = QPushButton("← Photos")
        btn_back.setToolTip("Retourner à la grille de photos")
        btn_back.clicked.connect(self.back_requested)
        bar.addWidget(btn_back)
        bar.addStretch()
        self._lbl_title = QLabel()
        self._lbl_title.setStyleSheet("font-weight: bold; color: #ccc; font-size: 13px;")
        bar.addWidget(self._lbl_title)
        bar.addStretch()
        root.addLayout(bar)

        # Barre d'action (sélection multiple) — masquée par défaut
        self._action_bar = QFrame()
        self._action_bar.setFrameShape(QFrame.NoFrame)
        self._action_bar.setStyleSheet(
            "background: #1e3a5a; border-radius: 6px; padding: 2px;"
        )
        ab_layout = QHBoxLayout(self._action_bar)
        ab_layout.setContentsMargins(10, 6, 10, 6)
        ab_layout.setSpacing(8)

        self._lbl_selection = QLabel()
        self._lbl_selection.setStyleSheet("color: #aad4f5; font-size: 11px; font-weight: bold;")
        ab_layout.addWidget(self._lbl_selection)
        ab_layout.addStretch()

        self._btn_view_photos = QPushButton("Voir les photos")
        self._btn_view_photos.setStyleSheet(
            "QPushButton { background: #2a5070; color: #cce; border: none;"
            " border-radius: 3px; padding: 3px 10px; }"
            "QPushButton:hover { background: #3a6080; }"
        )
        self._btn_view_photos.clicked.connect(self._on_view_photos_selected)
        ab_layout.addWidget(self._btn_view_photos)

        self._btn_assign = QPushButton("Associer à une personne…")
        self._btn_assign.setStyleSheet(
            "QPushButton { background: #2a6040; color: #8da; border: none;"
            " border-radius: 3px; padding: 3px 10px; }"
            "QPushButton:hover { background: #3a7050; }"
        )
        self._btn_assign.clicked.connect(self._on_assign_selected)
        ab_layout.addWidget(self._btn_assign)

        self._btn_ignore_sel = QPushButton("Ignorer")
        self._btn_ignore_sel.setStyleSheet(
            "QPushButton { background: #503030; color: #daa; border: none;"
            " border-radius: 3px; padding: 3px 10px; }"
            "QPushButton:hover { background: #604040; }"
        )
        self._btn_ignore_sel.clicked.connect(self._on_ignore_selected)
        ab_layout.addWidget(self._btn_ignore_sel)

        self._btn_clear_sel = QPushButton("✕ Désélectionner")
        self._btn_clear_sel.setStyleSheet(
            "QPushButton { background: transparent; color: #777; border: none;"
            " border-radius: 3px; padding: 3px 10px; }"
            "QPushButton:hover { color: #aaa; }"
        )
        self._btn_clear_sel.clicked.connect(self._clear_selection)
        ab_layout.addWidget(self._btn_clear_sel)

        self._action_bar.setVisible(False)
        root.addWidget(self._action_bar)

        # Zone de défilement
        self._content = QWidget()
        self._gl = QGridLayout(self._content)
        self._gl.setSpacing(10)
        self._gl.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll)

    # ------------------------------------------------------------------ resize

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._reflow_if_needed)

    def _reflow_if_needed(self) -> None:
        available = self._scroll.viewport().width()
        cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))
        if cols != self._current_cols and self._ordered_cards:
            self._current_cols = cols
            self._reflow()

    def _reflow(self) -> None:
        for i, (_, card) in enumerate(self._ordered_cards):
            self._gl.addWidget(card, i // self._current_cols, i % self._current_cols)

    # ------------------------------------------------------------------ public

    def refresh(self) -> None:
        if self._loader and self._loader.isRunning():
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            self._loader = None

        while self._gl.count():
            item = self._gl.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._ordered_cards.clear()
        self._selected_ids.clear()
        self._action_bar.setVisible(False)

        clusters = self._face_db.get_unnamed_clusters()
        n = len(clusters)

        if not clusters:
            self._lbl_title.setText("Aucun groupe à identifier")
            lbl = QLabel("Tous les groupes ont été identifiés ou ignorés.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #666; font-size: 13px;")
            self._gl.addWidget(lbl, 0, 0, 1, _COLS_MIN)
            return

        plural = "s" if n > 1 else ""
        self._lbl_title.setText(
            f"{n} groupe{plural} de visages non identifié{plural}"
        )

        self._persons = self._catalog.get_persons()
        self._face_db.enrich_persons(self._persons)
        self._person_embeddings = {}
        for p in self._persons:
            emb = self._face_db.get_representative_embedding(person_id=p.id)
            if emb:
                self._person_embeddings[p.id] = emb

        available = self._scroll.viewport().width()
        if available > 0:
            self._current_cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))

        avatar_items = []
        for i, (cluster_id, face_count) in enumerate(clusters):
            sugg_id, sugg_label, sugg_color = self._compute_suggestion(cluster_id)

            card = _ClusterCard(
                cluster_id, face_count,
                sugg_id, sugg_label, sugg_color,
                self,
            )
            card.selection_toggled.connect(self._on_card_selection_toggled)
            card.name_requested.connect(self._on_card_name_requested)
            card.merge_requested.connect(self._on_card_merge_requested)
            card.ignore_requested.connect(self._on_card_ignore_requested)
            self._gl.addWidget(card, i // self._current_cols, i % self._current_cols)
            self._cards[cluster_id] = card
            self._ordered_cards.append((cluster_id, card))

            rep = self._face_db.get_representative_face(cluster_id=cluster_id)
            if rep:
                avatar_items.append((cluster_id, rep))

        if avatar_items:
            QTimer.singleShot(
                0, lambda items=avatar_items: self._start_cluster_loader(items)
            )

    # ------------------------------------------------------------------ sélection

    def _on_card_selection_toggled(self, cluster_id: int, selected: bool) -> None:
        if selected:
            self._selected_ids.add(cluster_id)
        else:
            self._selected_ids.discard(cluster_id)
        self._update_action_bar()

    def _update_action_bar(self) -> None:
        n = len(self._selected_ids)
        self._action_bar.setVisible(n > 0)
        if n == 0:
            return
        plural = "s" if n > 1 else ""
        self._lbl_selection.setText(f"{n} groupe{plural} sélectionné{plural}")
        self._btn_view_photos.setVisible(n == 1)

    def _clear_selection(self) -> None:
        for cid, card in self._cards.items():
            if cid in self._selected_ids:
                card.set_selected(False)
        self._selected_ids.clear()
        self._action_bar.setVisible(False)

    def _on_view_photos_selected(self) -> None:
        if len(self._selected_ids) != 1:
            return
        cluster_id = next(iter(self._selected_ids))
        face_count = next(
            (fc for cid, fc in self._face_db.get_unnamed_clusters() if cid == cluster_id), 0
        )
        plural = "s" if face_count > 1 else ""
        self.photos_requested.emit(cluster_id, f"Groupe {cluster_id} — {face_count} visage{plural}")

    def _on_assign_selected(self) -> None:
        ids = sorted(self._selected_ids)
        if not ids:
            return
        n = len(ids)
        label = (
            f"Identifier le groupe {ids[0]}"
            if n == 1
            else f"Identifier {n} groupes sélectionnés"
        )

        # Suggestion : personne la plus proche parmi les groupes sélectionnés
        best_sugg_id = None
        best_sugg_sim = 0.0
        for cid in ids:
            card = self._cards.get(cid)
            if card and card._suggested_person_id is not None:
                c_emb = self._face_db.get_representative_embedding(cluster_id=cid)
                if c_emb and card._suggested_person_id in self._person_embeddings:
                    p_emb = self._person_embeddings[card._suggested_person_id]
                    sim = _cosine_sim(c_emb, p_emb)
                    if sim > best_sugg_sim:
                        best_sugg_sim = sim
                        best_sugg_id = card._suggested_person_id

        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)

        dlg = _AssignDialog(
            ids[0], persons,
            suggested_person_id=best_sugg_id,
            parent=self,
        )
        dlg.setWindowTitle(label)
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_ignored():
            for cid in ids:
                self._face_db.ignore_cluster(cid)
                self.cluster_ignored.emit(cid)
        elif dlg.is_new_person():
            self.clusters_named.emit(ids, dlg.new_name())
        else:
            self.clusters_assigned.emit(ids, dlg.existing_person_id())

        self._clear_selection()

    def _on_ignore_selected(self) -> None:
        ids = sorted(self._selected_ids)
        for cid in ids:
            self._face_db.ignore_cluster(cid)
            self.cluster_ignored.emit(cid)
        self._clear_selection()

    # ------------------------------------------------------------------ slots cartes individuelles

    def _on_card_ignore_requested(self, cluster_id: int) -> None:
        self._face_db.ignore_cluster(cluster_id)
        self.cluster_ignored.emit(cluster_id)

    def _on_card_merge_requested(self, cluster_id: int) -> None:
        dlg = _MergePickerDialog(cluster_id, self._face_db, self)
        if dlg.exec() != QDialog.Accepted:
            return
        target_id = dlg.selected_cluster_id()
        if target_id is not None:
            self._face_db.merge_clusters(cluster_id, target_id)
            self.cluster_merged.emit(cluster_id, target_id)

    def _on_card_name_requested(self, cluster_id: int) -> None:
        card = self._cards.get(cluster_id)
        suggested_id = card._suggested_person_id if card else None

        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)

        dlg = _AssignDialog(
            cluster_id, persons,
            suggested_person_id=suggested_id,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_ignored():
            self._face_db.ignore_cluster(cluster_id)
            self.cluster_ignored.emit(cluster_id)
        elif dlg.is_new_person():
            self.cluster_named.emit(cluster_id, dlg.new_name())
        else:
            self.cluster_assigned.emit(cluster_id, dlg.existing_person_id())

    # ------------------------------------------------------------------ internal

    def _compute_suggestion(self, cluster_id: int) -> tuple[int | None, str, str]:
        if not self._person_embeddings:
            return None, "", ""
        c_emb = self._face_db.get_representative_embedding(cluster_id=cluster_id)
        if not c_emb:
            return None, "", ""

        best_sim, best_p = 0.0, None
        for p in self._persons:
            p_emb = self._person_embeddings.get(p.id)
            if p_emb:
                sim = _cosine_sim(c_emb, p_emb)
                if sim > best_sim:
                    best_sim, best_p = sim, p

        if not best_p or best_sim < _SIM_WEAK:
            return None, "", ""

        pct = int(best_sim * 100)
        if best_sim >= 0.82:
            return best_p.id, f"≈ {best_p.name} ({pct} %)", "#7aabdb"
        return best_p.id, f"~ {best_p.name} ({pct} %)", "#888"

    def _card_is_visible(self, card: _ClusterCard) -> bool:
        try:
            top_left = card.mapTo(self._scroll.viewport(), QPoint(0, 0))
            return self._scroll.viewport().rect().intersects(QRect(top_left, card.size()))
        except RuntimeError:
            return False

    def _start_cluster_loader(self, avatar_items: list) -> None:
        visible_ids = {
            cid for cid, card in self._cards.items()
            if self._card_is_visible(card)
        }
        avatar_items.sort(key=lambda item: 0 if item[0] in visible_ids else 1)
        self._loader = _AvatarLoader(avatar_items, _CARD_IMG, self)
        self._loader.avatar_ready.connect(self._on_avatar_ready)
        self._loader.start()

    def _on_avatar_ready(self, cluster_id: int, data: bytes) -> None:
        card = self._cards.get(cluster_id)
        if card:
            card.set_avatar(data)
