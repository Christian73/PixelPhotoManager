"""
PersonClusterView — grille des groupes de visages associés à une personne nommée.

Empilée dans MainWindow._stack à l'index 3.
Double-clic sur une carte → photos_requested(cluster_id, label).
"""

import logging

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.core.models import FaceInfo, PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import _AssignDialog, _face_bytes

logger = logging.getLogger(__name__)

_CARD_IMG = 130
_CARD_W   = 148
_CARD_GAP = 10
_COLS_MIN = 2


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
    """Charge les face crops PNG en arrière-plan."""

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
        for cluster_id, face in self._items:
            data = _face_bytes(face, self._size)
            if data:
                self.avatar_ready.emit(cluster_id, data)


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


class PersonClusterView(QWidget):
    """
    Vue des groupes de visages associés à une personne nommée.
    Empilée dans MainWindow._stack à l'index 3.
    """

    photos_requested = Signal(int, str)   # cluster_id, label
    back_requested   = Signal()
    cluster_unassigned = Signal(int)      # cluster_id — groupe dé-associé (DB déjà à jour)
    cluster_named      = Signal(int, str) # cluster_id, name — réallouer à une nouvelle personne
    cluster_assigned   = Signal(int, int) # cluster_id, person_id — réallouer à une personne existante

    def __init__(self, face_db: FaceDatabase, catalog, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._person: PersonInfo | None = None
        self._cards: dict[int, _PersonCard] = {}
        self._loader: _CardLoader | None = None
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
            # Même personne — on met juste à jour le titre au cas où elle a été renommée.
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

        # Spacer symétrique pour centrer le titre
        spacer = QWidget()
        spacer.setFixedWidth(btn_back.sizeHint().width())
        spacer.setStyleSheet("background: transparent;")
        h.addWidget(spacer)

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

    # ------------------------------------------------------------------ refresh

    def _refresh(self) -> None:
        if self._person is None:
            return

        self._lbl_title.setText(f"Groupes identifiés pour {self._person.name}")

        # Arrêter le loader précédent
        if self._loader is not None:
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
                if self._loader.isRunning():
                    self._loader.finished.connect(self._loader.deleteLater)
                else:
                    self._loader.deleteLater()
            except RuntimeError:
                pass  # C++ object already deleted by a previous deleteLater()
            self._loader = None

        # Vider la grille
        for i in range(self._flow.count() - 1, -1, -1):
            item = self._flow.takeAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        self._cards.clear()

        # Récupérer les clusters associés à cette personne
        clusters = self._face_db.get_clusters_for_person(self._person.id)
        if not clusters:
            self._scroll.hide()
            self._lbl_empty.show()
            return

        self._lbl_empty.hide()
        self._scroll.show()

        cluster_ids = [cid for cid, _ in clusters]
        rep_faces = self._face_db.get_all_representative_faces(cluster_ids)

        cols = self._compute_cols()
        for idx, (cluster_id, face_count) in enumerate(clusters):
            plural = "s" if face_count > 1 else ""
            nav_label = f"Groupe {cluster_id} — {face_count} photo{plural}"
            card = _PersonCard(cluster_id, face_count, self._content)
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

    def _compute_cols(self) -> int:
        w = self._scroll.width() or 600
        return max(_COLS_MIN, (w - 32) // (_CARD_W + _CARD_GAP))

    def _reflow(self) -> None:
        cards = list(self._cards.values())
        if not cards:
            return
        cols = self._compute_cols()
        for i, card in enumerate(cards):
            self._flow.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    @Slot(int, bytes)
    def _on_avatar_ready(self, cluster_id: int, data: bytes) -> None:
        card = self._cards.get(cluster_id)
        if card:
            card.set_avatar(data)

    # ------------------------------------------------------------------ context menu

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
        current_pid = self._person.id if self._person else None
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
            self._lbl_empty.show()
        else:
            self._reflow()
