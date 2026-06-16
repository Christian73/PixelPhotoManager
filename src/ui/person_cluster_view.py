"""
PersonClusterView — grille des groupes de visages associés à une personne nommée.

Empilée dans MainWindow._stack à l'index 3.
Double-clic sur une carte → photos_requested(cluster_id, label).

Deux modes :
  "grouped" — une carte par groupe (comportement historique)
  "flat"    — une vignette par visage individuel (tous groupes confondus)
             Clic : sélection / Ctrl+clic : multi-sélection
             Clic-droit : réassigner les visages sélectionnés à une autre personne
             Double-clic : ouvrir la photo dans la visionneuse
"""

import logging
import os

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.core.models import FaceInfo, PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import _AssignDialog, _face_bytes, _load_edit_rotations

logger = logging.getLogger(__name__)

# Cartes groupées
_CARD_IMG = 130
_CARD_W   = 148
_CARD_GAP = 10
_COLS_MIN = 2

# Vignettes dégroupées
_THUMB_IMG = 80
_THUMB_W   = 90
_THUMB_GAP = 6

_TOGGLE_STYLE = (
    "QPushButton {"
    "  color: #aaa; border: 1px solid #444; background: #2a2a2a;"
    "  padding: 3px 14px; font-size: 11px;"
    "}"
    "QPushButton:checked {"
    "  color: #eee; background: #2d4a6a; border-color: #7aabdb;"
    "}"
    "QPushButton:hover:!checked { background: #333; }"
)

_MENU_STYLE = (
    "QMenu { background: #2a2a2a; color: #eee; border: 1px solid #555; }"
    "QMenu::item { padding: 6px 20px; }"
    "QMenu::item:selected { background: #3a4a5a; }"
)


class _PersonsLoaderThread(QThread):
    """Charge les personnes existantes en arrière-plan avant d'ouvrir le dialogue de réallocation."""

    ready = Signal(list)   # list[PersonInfo]

    def __init__(self, catalog, face_db: FaceDatabase, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._face_db = face_db

    def run(self) -> None:
        try:
            persons = self._catalog.get_persons()
            self._face_db.enrich_persons(persons)
            self.ready.emit(persons)
        except Exception:
            logger.exception("_PersonsLoaderThread: erreur inattendue")
            self.ready.emit([])


class _CardLoader(QThread):
    """Charge les face crops PNG (mode groupé) en arrière-plan."""

    avatar_ready = Signal(int, bytes)   # cluster_id, PNG bytes

    def __init__(
        self,
        items: list[tuple[int, "FaceInfo"]],
        size: int = _CARD_IMG,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items = items
        self._size  = size

    def run(self) -> None:
        paths = list({face.photo_path for _, face in self._items})
        edit_rots = _load_edit_rotations(paths)
        for cluster_id, face in self._items:
            data = _face_bytes(face, self._size,
                               edit_rotation=edit_rots.get(face.photo_path, 0))
            if data:
                self.avatar_ready.emit(cluster_id, data)


class _FlatFaceLoader(QThread):
    """Charge les crops de visages individuels (mode dégroupé) en arrière-plan."""

    face_ready = Signal(int, bytes)   # face_id, PNG bytes

    def __init__(
        self,
        faces: list[FaceInfo],
        size: int = _THUMB_IMG,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._faces = faces
        self._size  = size

    def run(self) -> None:
        paths = list({f.photo_path for f in self._faces})
        edit_rots = _load_edit_rotations(paths)
        for face in self._faces:
            data = _face_bytes(face, self._size,
                               edit_rotation=edit_rots.get(face.photo_path, 0))
            if data:
                self.face_ready.emit(face.id, data)


class _PersonCard(QFrame):
    """
    Carte représentant un groupe de visages lié à une personne.
    Double-clic → afficher les photos du groupe.
    Clic droit → dé-associer ou réallouer à une autre personne.
    """

    double_clicked       = Signal(int)   # cluster_id
    dissociate_requested = Signal(int)   # cluster_id
    reassign_requested   = Signal(int)   # cluster_id

    _STYLE = (
        "QFrame { border: 2px solid #3a3a3a; border-radius: 6px; background: #252525; }"
        "QFrame:hover { border-color: #7aabdb; background: #2a3545; }"
    )
    _MENU_STYLE = (
        "QMenu { background: #2a2a2a; color: #eee; border: 1px solid #555; }"
        "QMenu::item { padding: 6px 20px; }"
        "QMenu::item:selected { background: #3a4a5a; }"
    )

    def __init__(self, cluster_id: int, face_count: int, parent=None) -> None:
        super().__init__(parent)
        self._cluster_id = cluster_id

        self.setFixedWidth(_CARD_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._STYLE)
        self.setToolTip("Double-clic : voir les photos de ce groupe")

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
        lbl = QLabel(f"{face_count} photo{plural}")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("border: none; font-size: 11px; color: #aaa;")
        col.addWidget(lbl)

    def set_avatar(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(
            _CARD_IMG, _CARD_IMG,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._lbl_img.setPixmap(scaled)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self._cluster_id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(self._MENU_STYLE)
        act_dissociate = menu.addAction("Dé-associer ce groupe de cette personne")
        act_reassign   = menu.addAction("Réallouer à une autre personne…")
        action = menu.exec(event.globalPos())
        if action == act_dissociate:
            self.dissociate_requested.emit(self._cluster_id)
        elif action == act_reassign:
            self.reassign_requested.emit(self._cluster_id)


class _FaceThumb(QFrame):
    """Vignette compacte d'un visage individuel (mode dégroupé)."""

    clicked                = Signal(int, bool)    # face_id, ctrl_held
    double_clicked         = Signal(str)           # photo_path
    context_menu_requested = Signal(int, object)  # face_id, QPoint global

    _STYLE_NORMAL = (
        "QFrame { border: 1px solid #3a3a3a; border-radius: 4px; background: #252525; }"
        "QFrame:hover { border-color: #7aabdb; background: #2a3545; }"
    )
    _STYLE_SELECTED = (
        "QFrame { border: 2px solid #7aabdb; border-radius: 4px; background: #1e3a5a; }"
        "QFrame:hover { border-color: #9fcbf5; background: #243f5a; }"
    )

    def __init__(self, face: FaceInfo, parent=None) -> None:
        super().__init__(parent)
        self._face_id    = face.id
        self._photo_path = face.photo_path
        self.setFixedSize(_THUMB_W, _THUMB_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._STYLE_NORMAL)
        self.setToolTip(os.path.basename(face.photo_path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        self._lbl_img = LoadingLabel("#1a1a1a")
        self._lbl_img.setFixedSize(_THUMB_IMG, _THUMB_IMG)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet("border: none; border-radius: 3px;")
        self._lbl_img.start_loading()
        layout.addWidget(self._lbl_img, alignment=Qt.AlignCenter)

    def set_image(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(
            _THUMB_IMG, _THUMB_IMG,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._lbl_img.setPixmap(scaled)

    def set_selected(self, selected: bool) -> None:
        self.setStyleSheet(
            self._STYLE_SELECTED if selected else self._STYLE_NORMAL
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            ctrl = bool(event.modifiers() & Qt.ControlModifier)
            self.clicked.emit(self._face_id, ctrl)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self._photo_path)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(self._face_id, event.globalPos())


class PersonClusterView(QWidget):
    """
    Vue des groupes de visages associés à une personne nommée.
    Empilée dans MainWindow._stack à l'index 3.
    """

    photos_requested   = Signal(int, str)   # cluster_id, label
    photo_requested    = Signal(str)        # photo_path — double-clic en mode dégroupé
    back_requested     = Signal()
    cluster_unassigned = Signal(int)        # cluster_id — groupe dé-associé
    cluster_named      = Signal(int, str)   # cluster_id, name
    cluster_assigned   = Signal(int, int)   # cluster_id, person_id
    faces_reassigned   = Signal()           # visages réassignés en mode dégroupé

    def __init__(self, face_db: FaceDatabase, catalog, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._person: PersonInfo | None = None
        self._mode: str = "grouped"          # "grouped" | "flat"

        # Mode groupé
        self._cards: dict[int, _PersonCard] = {}
        self._loader: _CardLoader | None = None

        # Mode dégroupé
        self._flat_cards: dict[int, _FaceThumb] = {}
        self._flat_loader: _FlatFaceLoader | None = None
        self._selection: set[int] = set()   # face_ids sélectionnés

        self._persons_loader: _PersonsLoaderThread | None = None
        self._build()

    @property
    def current_person(self) -> "PersonInfo | None":
        return self._person

    # ------------------------------------------------------------------ public

    def set_person(self, person: PersonInfo) -> None:
        if self._person is None or self._person.id != person.id:
            self._person = person
            self._refresh()
        else:
            self._lbl_title.setText(f"Groupes identifiés pour {person.name}")
            self._person = person

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Barre titre
        header = QWidget()
        header.setStyleSheet("background: #1e1e1e; border-bottom: 1px solid #333;")
        header.setFixedHeight(44)
        h = QHBoxLayout(header)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(8)

        btn_back = QPushButton("← Retour")
        btn_back.setStyleSheet(
            "QPushButton { color: #7aabdb; border: none; font-size: 12px; background: transparent; }"
            "QPushButton:hover { color: #9fcbf5; }"
        )
        btn_back.setCursor(Qt.PointingHandCursor)
        btn_back.setFixedHeight(28)
        btn_back.clicked.connect(self.back_requested)
        h.addWidget(btn_back)

        self._lbl_title = QLabel()
        self._lbl_title.setStyleSheet(
            "color: #eee; font-size: 13px; font-weight: bold; background: transparent;"
        )
        self._lbl_title.setAlignment(Qt.AlignCenter)
        h.addWidget(self._lbl_title, stretch=1)

        # Boutons toggle Groupé / Dégroupé
        self._btn_grouped = QPushButton("Groupé")
        self._btn_flat    = QPushButton("Dégroupé")
        for btn in (self._btn_grouped, self._btn_flat):
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.setStyleSheet(_TOGGLE_STYLE)

        self._btn_grouped.setChecked(True)

        self._toggle_group = QButtonGroup(self)
        self._toggle_group.setExclusive(True)
        self._toggle_group.addButton(self._btn_grouped, 0)
        self._toggle_group.addButton(self._btn_flat,    1)
        self._toggle_group.idClicked.connect(self._on_mode_changed)

        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.setSpacing(0)
        toggle_row.addWidget(self._btn_grouped)
        toggle_row.addWidget(self._btn_flat)
        h.addLayout(toggle_row)

        root.addWidget(header)

        # Zone de scroll
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: #1a1a1a; }")

        self._content = QWidget()
        self._content.setStyleSheet("background: #1a1a1a;")
        self._flow = QGridLayout(self._content)
        self._flow.setContentsMargins(16, 16, 16, 16)
        self._flow.setSpacing(_CARD_GAP)
        self._flow.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, stretch=1)

        self._lbl_empty = QLabel("Aucun groupe associé à cette personne.")
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet("color: #555; font-size: 13px;")
        self._lbl_empty.hide()
        root.addWidget(self._lbl_empty)

    # ------------------------------------------------------------------ mode

    @Slot(int)
    def _on_mode_changed(self, btn_id: int) -> None:
        new_mode = "grouped" if btn_id == 0 else "flat"
        if new_mode == self._mode:
            return
        self._mode = new_mode
        self._flow.setSpacing(_CARD_GAP if self._mode == "grouped" else _THUMB_GAP)
        self._refresh()

    # ------------------------------------------------------------------ refresh

    def _stop_loaders(self) -> None:
        for loader, sig in (
            (self._loader,      getattr(self._loader,      "avatar_ready", None)),
            (self._flat_loader, getattr(self._flat_loader, "face_ready",   None)),
        ):
            if loader is None:
                continue
            try:
                if sig is not None:
                    sig.disconnect()
                if loader.isRunning():
                    loader.finished.connect(loader.deleteLater)
                else:
                    loader.deleteLater()
            except RuntimeError:
                pass
        self._loader      = None
        self._flat_loader = None

    def _clear_grid(self) -> None:
        for i in range(self._flow.count() - 1, -1, -1):
            item = self._flow.takeAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._flat_cards.clear()
        self._selection.clear()

    def _refresh(self) -> None:
        if self._person is None:
            return
        self._lbl_title.setText(f"Groupes identifiés pour {self._person.name}")
        self._stop_loaders()
        self._clear_grid()
        if self._mode == "grouped":
            self._refresh_grouped()
        else:
            self._refresh_flat()

    def _refresh_grouped(self) -> None:
        clusters = self._face_db.get_clusters_for_person(self._person.id)
        if not clusters:
            self._scroll.hide()
            self._lbl_empty.setText("Aucun groupe associé à cette personne.")
            self._lbl_empty.show()
            return

        self._lbl_empty.hide()
        self._scroll.show()

        cluster_ids = [cid for cid, _ in clusters]
        rep_faces   = self._face_db.get_all_representative_faces(cluster_ids)
        cols        = self._compute_cols(_CARD_W, _CARD_GAP)

        for idx, (cluster_id, face_count) in enumerate(clusters):
            plural    = "s" if face_count > 1 else ""
            nav_label = f"Groupe {cluster_id} — {face_count} photo{plural}"
            card      = _PersonCard(cluster_id, face_count, self._content)
            card.double_clicked.connect(
                lambda cid=cluster_id, lbl=nav_label: self.photos_requested.emit(cid, lbl)
            )
            card.dissociate_requested.connect(self._on_dissociate)
            card.reassign_requested.connect(self._on_reassign)
            self._flow.addWidget(card, idx // cols, idx % cols)
            self._cards[cluster_id] = card

        avatar_items = [(cid, face) for cid, face in rep_faces.items() if cid in self._cards]
        if avatar_items:
            self._loader = _CardLoader(avatar_items, _CARD_IMG, self)
            self._loader.avatar_ready.connect(self._on_avatar_ready)
            self._loader.finished.connect(self._loader.deleteLater)
            self._loader.start()

    def _refresh_flat(self) -> None:
        faces = self._face_db.get_faces_for_person(self._person.id)
        if not faces:
            self._scroll.hide()
            self._lbl_empty.setText("Aucun visage associé à cette personne.")
            self._lbl_empty.show()
            return

        self._lbl_empty.hide()
        self._scroll.show()

        cols = self._compute_cols(_THUMB_W, _THUMB_GAP)
        for idx, face in enumerate(faces):
            thumb = _FaceThumb(face, self._content)
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.double_clicked.connect(self.photo_requested)
            thumb.context_menu_requested.connect(self._on_thumb_context_menu)
            self._flow.addWidget(thumb, idx // cols, idx % cols)
            self._flat_cards[face.id] = thumb

        self._flat_loader = _FlatFaceLoader(faces, _THUMB_IMG, self)
        self._flat_loader.face_ready.connect(self._on_face_ready)
        self._flat_loader.finished.connect(self._flat_loader.deleteLater)
        self._flat_loader.start()

    # ------------------------------------------------------------------ cols / reflow

    def _compute_cols(self, card_w: int, gap: int) -> int:
        w = self._scroll.width() or 600
        return max(_COLS_MIN, (w - 32) // (card_w + gap))

    def _reflow(self) -> None:
        if self._mode == "grouped":
            cards  = list(self._cards.values())
            card_w, gap = _CARD_W, _CARD_GAP
        else:
            cards  = list(self._flat_cards.values())
            card_w, gap = _THUMB_W, _THUMB_GAP
        if not cards:
            return
        cols = self._compute_cols(card_w, gap)
        for i, card in enumerate(cards):
            self._flow.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    # ------------------------------------------------------------------ slots loaders

    @Slot(int, bytes)
    def _on_avatar_ready(self, cluster_id: int, data: bytes) -> None:
        card = self._cards.get(cluster_id)
        if card:
            card.set_avatar(data)

    @Slot(int, bytes)
    def _on_face_ready(self, face_id: int, data: bytes) -> None:
        thumb = self._flat_cards.get(face_id)
        if thumb:
            thumb.set_image(data)

    # ------------------------------------------------------------------ sélection (mode flat)

    @Slot(int, bool)
    def _on_thumb_clicked(self, face_id: int, ctrl_held: bool) -> None:
        if ctrl_held:
            # Bascule dans/hors de la sélection
            if face_id in self._selection:
                self._selection.discard(face_id)
            else:
                self._selection.add(face_id)
        else:
            # Sélection exclusive
            self._selection = {face_id}
        self._apply_selection_style()

    def _apply_selection_style(self) -> None:
        for fid, thumb in self._flat_cards.items():
            thumb.set_selected(fid in self._selection)

    # ------------------------------------------------------------------ menu contextuel (mode flat)

    @Slot(int, object)
    def _on_thumb_context_menu(self, face_id: int, pos) -> None:
        # Si le visage cliqué n'est pas dans la sélection → sélection exclusive
        if face_id not in self._selection:
            self._selection = {face_id}
            self._apply_selection_style()

        n = len(self._selection)
        label = f"Réassigner {n} visage{'s' if n > 1 else ''} à une autre personne…"

        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLE)
        act_reassign = menu.addAction(label)
        if menu.exec(pos) == act_reassign:
            self._start_flat_reassign(list(self._selection))

    def _start_flat_reassign(self, face_ids: list[int]) -> None:
        if self._persons_loader is not None and self._persons_loader.isRunning():
            return
        self._persons_loader = _PersonsLoaderThread(self._catalog, self._face_db, self)
        self._persons_loader.ready.connect(
            lambda persons, fids=face_ids: self._show_flat_reassign_dialog(fids, persons)
        )
        self._persons_loader.finished.connect(self._persons_loader.deleteLater)
        self._persons_loader.start()

    def _show_flat_reassign_dialog(
        self, face_ids: list[int], persons: list[PersonInfo]
    ) -> None:
        current_pid   = self._person.id if self._person else None
        other_persons = [p for p in persons if p.id != current_pid]

        dlg = _AssignDialog(-1, other_persons, show_ignore=False, parent=self)
        n   = len(face_ids)
        dlg.setWindowTitle(
            f"Réassigner {n} visage{'s' if n > 1 else ''} à une autre personne"
        )
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_new_person():
            name = dlg.new_name()
            if not name:
                return
            new_person = self._catalog.create_person(name)
            person_id  = new_person.id
        else:
            person_id = dlg.existing_person_id()
            if person_id is None:
                return

        self._face_db.assign_person_to_faces(face_ids, person_id)
        self._remove_flat_thumbs(face_ids)
        self.faces_reassigned.emit()

    def _remove_flat_thumbs(self, face_ids: list[int]) -> None:
        for fid in face_ids:
            thumb = self._flat_cards.pop(fid, None)
            if thumb:
                self._flow.removeWidget(thumb)
                thumb.deleteLater()
        self._selection.clear()
        if not self._flat_cards:
            self._scroll.hide()
            self._lbl_empty.setText("Aucun visage associé à cette personne.")
            self._lbl_empty.show()
        else:
            self._reflow()

    # ------------------------------------------------------------------ context menu (mode groupé)

    @Slot(int)
    def _on_dissociate(self, cluster_id: int) -> None:
        if self._person is None:
            return
        self._face_db.unassign_person_from_cluster(self._person.id, cluster_id)
        self._remove_cluster_card(cluster_id)
        self.cluster_unassigned.emit(cluster_id)

    @Slot(int)
    def _on_reassign(self, cluster_id: int) -> None:
        if self._persons_loader is not None and self._persons_loader.isRunning():
            return
        self._persons_loader = _PersonsLoaderThread(self._catalog, self._face_db, self)
        self._persons_loader.ready.connect(
            lambda persons, cid=cluster_id: self._show_reassign_dialog(cid, persons)
        )
        self._persons_loader.finished.connect(self._persons_loader.deleteLater)
        self._persons_loader.start()

    def _show_reassign_dialog(self, cluster_id: int, persons: list[PersonInfo]) -> None:
        current_pid   = self._person.id if self._person else None
        other_persons = [p for p in persons if p.id != current_pid]
        dlg = _AssignDialog(cluster_id, other_persons, show_ignore=False, parent=self)
        dlg.setWindowTitle("Réallouer ce groupe à une autre personne")
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.is_new_person():
            name = dlg.new_name()
            self._remove_cluster_card(cluster_id)
            self.cluster_named.emit(cluster_id, name)
        elif dlg.existing_person_id() is not None:
            self._remove_cluster_card(cluster_id)
            self.cluster_assigned.emit(cluster_id, dlg.existing_person_id())

    def _remove_cluster_card(self, cluster_id: int) -> None:
        card = self._cards.pop(cluster_id, None)
        if card:
            self._flow.removeWidget(card)
            card.deleteLater()
        if not self._cards:
            self._scroll.hide()
            self._lbl_empty.setText("Aucun groupe associé à cette personne.")
            self._lbl_empty.show()
        else:
            self._reflow()
