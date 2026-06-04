"""
FaceClusterGrid — grille des groupes de visages non identifiés.

Affichée dans la zone principale à la place de la grille de photos.
Cliquer sur un groupe ouvre le dialogue de nommage.
"""

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _cosine_sim,
    _placeholder_pixmap, _SIM_WEAK,
)

logger = logging.getLogger(__name__)

_CARD_IMG     = 130   # taille de la vignette de visage
_CARD_W       = 148   # largeur totale de la carte
_CARD_SPACING = 10    # espacement entre cartes
_COLS_MIN     = 2     # minimum de colonnes


# ------------------------------------------------------------------ card

class _ClusterCard(QFrame):
    """Carte cliquable représentant un groupe de visages.

    Clic gauche  → photos_requested  (voir les photos du groupe)
    Clic droit   → name_requested    (ouvrir le dialogue de nommage)
    """

    photos_requested = Signal(int)   # cluster_id — clic gauche
    name_requested   = Signal(int)   # cluster_id — clic droit

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

        self.setFixedWidth(_CARD_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QFrame {
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                background: #252525;
            }
            QFrame:hover {
                border-color: #7aabdb;
                background: #2a3545;
            }
        """)

        col = QVBoxLayout(self)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(4)
        col.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        # Vignette (placeholder remplacé dès que l'avatar est chargé)
        self._lbl_img = QLabel()
        self._lbl_img.setFixedSize(_CARD_IMG, _CARD_IMG)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet(
            "border: none; background: #1a1a1a; border-radius: 4px;"
        )
        self._lbl_img.setPixmap(_placeholder_pixmap(_CARD_IMG))
        col.addWidget(self._lbl_img, alignment=Qt.AlignHCenter)

        # Nombre de visages
        plural = "s" if face_count > 1 else ""
        lbl_count = QLabel(f"{face_count} visage{plural}")
        lbl_count.setAlignment(Qt.AlignCenter)
        lbl_count.setStyleSheet("border: none; font-size: 11px; color: #aaa;")
        col.addWidget(lbl_count)

        # Suggestion de personne (optionnelle)
        if suggestion_label:
            lbl_sugg = QLabel(suggestion_label)
            lbl_sugg.setAlignment(Qt.AlignCenter)
            lbl_sugg.setWordWrap(True)
            lbl_sugg.setStyleSheet(
                f"border: none; font-size: 10px; color: {suggestion_color};"
            )
            col.addWidget(lbl_sugg)

        # Hint discret
        lbl_hint = QLabel("clic droit pour nommer")
        lbl_hint.setAlignment(Qt.AlignCenter)
        lbl_hint.setStyleSheet("border: none; font-size: 9px; color: #555;")
        col.addWidget(lbl_hint)

        self.setToolTip("Clic : voir les photos  —  Clic droit : nommer")

    def set_avatar(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(
            _CARD_IMG, _CARD_IMG,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._lbl_img.setPixmap(scaled)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.photos_requested.emit(self._cluster_id)
        elif event.button() == Qt.RightButton:
            self.name_requested.emit(self._cluster_id)
        super().mousePressEvent(event)


# ------------------------------------------------------------------ grid

class FaceClusterGrid(QWidget):
    """
    Zone principale affichant les groupes de visages non identifiés.

    Signals
    -------
    cluster_named(cluster_id, name)     — créer une nouvelle personne
    cluster_assigned(cluster_id, pid)   — assigner à une personne existante
    cluster_ignored(cluster_id)         — ignorer le groupe
    back_requested()                    — retourner à la grille de photos
    """

    cluster_named      = Signal(int, str)
    cluster_assigned   = Signal(int, int)
    cluster_ignored    = Signal(int)
    back_requested     = Signal()
    photos_requested   = Signal(int, str)  # cluster_id, label affiché dans la grille

    def __init__(
        self,
        face_db: FaceDatabase,
        catalog,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._cards:         dict[int, _ClusterCard] = {}
        self._ordered_cards: list[tuple[int, _ClusterCard]] = []
        self._current_cols:  int = _COLS_MIN
        self._persons: list[PersonInfo] = []
        self._person_embeddings: dict[int, list[float]] = {}
        self._loader  = None
        self._setup_ui()

    # ------------------------------------------------------------------ setup

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(8)

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
        # Différer le recalcul : le viewport n'a pas encore sa nouvelle taille
        # au moment où resizeEvent est appelé.
        QTimer.singleShot(0, self._reflow_if_needed)

    def _reflow_if_needed(self) -> None:
        available = self._scroll.viewport().width()
        cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))
        if cols != self._current_cols and self._ordered_cards:
            self._current_cols = cols
            self._reflow()

    def _reflow(self) -> None:
        """Redistribuer les cartes existantes sans les recréer."""
        for i, (_, card) in enumerate(self._ordered_cards):
            self._gl.addWidget(card, i // self._current_cols, i % self._current_cols)

    # ------------------------------------------------------------------ public

    def refresh(self) -> None:
        """Recharger les clusters depuis la base et reconstruire la grille."""
        if self._loader and self._loader.isRunning():
            # Déconnecter le signal sans bloquer le thread UI.
            # Le loader finit son image courante en arrière-plan mais ses
            # résultats atterrissent sur des cartes qui n'existent plus → ignorés.
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            self._loader = None

        # Vider la grille
        while self._gl.count():
            item = self._gl.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._ordered_cards.clear()

        clusters = self._face_db.get_unnamed_clusters()
        n = len(clusters)

        if not clusters:
            self._lbl_title.setText("Aucun groupe à identifier")
            lbl = QLabel("Tous les groupes ont été identifiés ou ignorés.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #666; font-size: 13px;")
            self._gl.addWidget(lbl, 0, 0, 1, _GRID_COLS)
            return

        plural = "s" if n > 1 else ""
        self._lbl_title.setText(
            f"{n} groupe{plural} de visages non identifié{plural}"
        )

        # Préparer les embeddings des personnes connues
        self._persons = self._catalog.get_persons()
        self._face_db.enrich_persons(self._persons)
        self._person_embeddings = {}
        for p in self._persons:
            emb = self._face_db.get_representative_embedding(person_id=p.id)
            if emb:
                self._person_embeddings[p.id] = emb

        # Calculer le nombre de colonnes d'après la largeur courante du viewport
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
            card.photos_requested.connect(self._on_card_photos_requested)
            card.name_requested.connect(self._on_card_name_requested)
            self._gl.addWidget(card, i // self._current_cols, i % self._current_cols)
            self._cards[cluster_id] = card
            self._ordered_cards.append((cluster_id, card))

            rep = self._face_db.get_representative_face(cluster_id=cluster_id)
            if rep:
                avatar_items.append((cluster_id, rep))

        if avatar_items:
            self._loader = _AvatarLoader(avatar_items, _CARD_IMG, self)
            self._loader.avatar_ready.connect(self._on_avatar_ready)
            self._loader.start()

    # ------------------------------------------------------------------ internal

    def _compute_suggestion(
        self, cluster_id: int
    ) -> tuple[int | None, str, str]:
        """Return (person_id | None, label, color) for the best matching person."""
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

    def _on_avatar_ready(self, cluster_id: int, data: bytes) -> None:
        card = self._cards.get(cluster_id)
        if card:
            card.set_avatar(data)

    def _on_card_photos_requested(self, cluster_id: int) -> None:
        """Clic simple : demander l'affichage des photos du groupe."""
        face_count = next(
            (fc for cid, fc in self._face_db.get_unnamed_clusters() if cid == cluster_id),
            0,
        )
        plural = "s" if face_count > 1 else ""
        label = f"Groupe {cluster_id} — {face_count} visage{plural}"
        self.photos_requested.emit(cluster_id, label)

    def _on_card_name_requested(self, cluster_id: int) -> None:
        """Double clic : ouvrir le dialogue de nommage."""
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
