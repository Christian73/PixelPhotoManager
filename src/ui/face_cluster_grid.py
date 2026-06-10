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
    QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _cosine_sim, _SIM_WEAK, _SIM_STRONG,
)

logger = logging.getLogger(__name__)

_CARD_IMG     = 130
_CARD_W       = 148
_CARD_SPACING = 10
_COLS_MIN     = 2
_SIM_GROUP    = 0.72   # seuil pour regrouper deux clusters "même personne probable"


# ------------------------------------------------------------------ helpers (module-level, utilisés par le thread)

def _compute_cluster_groups_bg(
    cluster_ids: list[int],
    embeddings: dict[int, list[float]],
) -> dict[int, list[int]]:
    """Union-Find : regroupe les clusters dont sim(centroïde) ≥ _SIM_GROUP.
    Exécuté dans le thread de fond pour ne pas bloquer l'UI."""
    parent = {cid: cid for cid in cluster_ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    ids = list(cluster_ids)
    for i, ci in enumerate(ids):
        ei = embeddings.get(ci)
        if not ei:
            continue
        for cj in ids[i + 1:]:
            ej = embeddings.get(cj)
            if ej and _cosine_sim(ei, ej) >= _SIM_GROUP:
                union(ci, cj)

    groups: dict[int, list[int]] = {}
    for cid in cluster_ids:
        root = find(cid)
        groups.setdefault(root, []).append(cid)
    return groups


def _compute_suggestion_bg(
    cluster_id: int,
    cluster_embeddings: dict[int, list[float]],
    persons: list,
    person_cluster_embeddings: dict[int, dict[int, list[float]]],
) -> "tuple[int | None, str, str]":
    """Calcule la meilleure suggestion de personne pour un cluster.
    Exécuté dans le thread de fond."""
    if not person_cluster_embeddings:
        return None, "", ""
    c_emb = cluster_embeddings.get(cluster_id)
    if not c_emb:
        return None, "", ""

    best_sim, best_p = 0.0, None
    for p in persons:
        for p_emb in person_cluster_embeddings.get(p.id, {}).values():
            sim = _cosine_sim(c_emb, p_emb)
            if sim > best_sim:
                best_sim, best_p = sim, p

    if not best_p or best_sim < _SIM_WEAK:
        return None, "", ""

    pct = int(best_sim * 100)
    if best_sim >= 0.82:
        return best_p.id, f"≈ {best_p.name} ({pct} %)", "#7aabdb"
    return best_p.id, f"~ {best_p.name} ({pct} %)", "#888"


# ------------------------------------------------------------------ card

class _ClusterCard(QFrame):
    """
    Carte représentant un groupe de visages.

    1 clic  → sélection alternée (selection_toggled)
    2 clics → ouvrir le dialogue de nommage (name_requested)
    Clic droit → menu contextuel (nommer / fusionner / ignorer)
    """

    selection_toggled = Signal(int, bool)  # cluster_id, is_selected
    view_requested    = Signal(int)
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
        self.setToolTip("Clic : sélectionner  —  Double-clic : voir les photos  —  Clic droit : identifier / fusionner / ignorer")

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
            act_name   = menu.addAction("Identifier cette personne…")
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
            self.view_requested.emit(self._cluster_id)
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


# ------------------------------------------------------------------ section widget

class _SectionWidget(QFrame):
    """Un groupe de clusters visuellement similaires, avec un en-tête optionnel."""

    def __init__(self, label: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
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
            outer.addLayout(hdr_row)

            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet(f"background: {color}; border: none;")
            sep.setFixedHeight(1)
            outer.addWidget(sep)

        self._card_area = QWidget()
        self._card_area.setStyleSheet("background: transparent;")
        self._card_gl = QGridLayout(self._card_area)
        self._card_gl.setSpacing(_CARD_SPACING)
        self._card_gl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        outer.addWidget(self._card_area)

        self._entries: list[tuple[int, "_ClusterCard"]] = []

    def add_card(self, cluster_id: int, card: "_ClusterCard") -> None:
        self._entries.append((cluster_id, card))

    def reflow(self, cols: int) -> None:
        for i, (_, card) in enumerate(self._entries):
            self._card_gl.addWidget(card, i // cols, i % cols)


# ------------------------------------------------------------------ refresh thread

class _ClusterRefreshThread(QThread):
    """Charge et pré-calcule en arrière-plan tout ce dont _build_from_data a besoin.
    L'UI reçoit des données prêtes à l'emploi — aucun calcul lourd dans le thread UI."""

    data_ready = Signal(object)   # dict | None

    def __init__(self, face_db: "FaceDatabase", catalog, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog

    def run(self) -> None:
        try:
            clusters = self._face_db.get_unnamed_clusters()
            if not clusters:
                self.data_ready.emit({
                    "face_counts": {},
                    "groups_sorted": [],
                    "group_labels": {},
                    "suggestions": {},
                    "representative_faces": {},
                    "persons": [],
                    "person_cluster_embeddings": {},
                })
                return

            cluster_ids  = [cid for cid, _ in clusters]
            face_counts  = {cid: fc for cid, fc in clusters}

            # ── Embeddings ─────────────────────────────────────────────────
            cluster_embeddings = self._face_db.get_all_cluster_centroids(cluster_ids)

            persons = self._catalog.get_persons()
            self._face_db.enrich_persons(persons)
            person_ids = [p.id for p in persons]
            person_cluster_embeddings = self._face_db.get_all_person_cluster_centroids(person_ids)

            # ── Regroupement O(n²) — fait ici, pas dans l'UI ──────────────
            raw_groups = _compute_cluster_groups_bg(cluster_ids, cluster_embeddings)
            groups_sorted = sorted(
                raw_groups.values(),
                key=lambda g: (-len(g), -sum(face_counts.get(c, 0) for c in g)),
            )

            # ── Labels de section par groupe ───────────────────────────────
            group_labels: dict[int, tuple[str, str]] = {}  # root_cid → (label, color)
            for group in groups_sorted:
                root = group[0]
                if len(group) > 1:
                    group_by_size = sorted(group, key=lambda c: -face_counts.get(c, 0))
                    sims = [
                        _cosine_sim(cluster_embeddings[ci], cluster_embeddings[cj])
                        for i, ci in enumerate(group_by_size)
                        for cj in group_by_size[i + 1:]
                        if ci in cluster_embeddings and cj in cluster_embeddings
                    ]
                    avg_sim = sum(sims) / len(sims) if sims else 0.0
                    pct = int(avg_sim * 100)
                    n_faces = sum(face_counts.get(c, 0) for c in group)
                    fp = "s" if n_faces > 1 else ""
                    label = (
                        f"≈ Probablement la même personne"
                        f"  —  {len(group)} groupes, {n_faces} visage{fp}"
                        f"  (sim. {pct} %)"
                    )
                    color = "#7aabdb" if avg_sim >= _SIM_STRONG else "#aaa"
                    group_labels[root] = (label, color)
                else:
                    group_labels[root] = ("", "")

            # ── Suggestions par cluster ────────────────────────────────────
            suggestions: dict[int, tuple] = {
                cid: _compute_suggestion_bg(
                    cid, cluster_embeddings, persons, person_cluster_embeddings
                )
                for cid in cluster_ids
            }

            # ── Faces représentatives (1 requête batch) ────────────────────
            representative_faces = self._face_db.get_all_representative_faces(cluster_ids)

            self.data_ready.emit({
                "face_counts":               face_counts,
                "groups_sorted":             groups_sorted,
                "group_labels":              group_labels,
                "suggestions":               suggestions,
                "representative_faces":      representative_faces,
                "persons":                   persons,
                "person_cluster_embeddings": person_cluster_embeddings,
            })
        except Exception:
            logger.exception("_ClusterRefreshThread: erreur inattendue")
            self.data_ready.emit(None)


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
        self._cards:        dict[int, _ClusterCard] = {}
        self._sections:     list[_SectionWidget] = []
        self._current_cols: int = _COLS_MIN
        self._persons: list[PersonInfo] = []
        self._person_cluster_embeddings: dict[int, dict[int, list[float]]] = {}
        self._loader  = None
        self._refresh_thread: _ClusterRefreshThread | None = None
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

        # Barre d'action multi-sélection (cachée par défaut)
        self._action_bar = QFrame()
        self._action_bar.setStyleSheet(
            "QFrame { background: #1e3040; border-radius: 4px; }"
        )
        _ab_row = QHBoxLayout(self._action_bar)
        _ab_row.setContentsMargins(10, 4, 10, 4)
        _ab_row.setSpacing(8)

        self._lbl_selected = QLabel()
        self._lbl_selected.setStyleSheet(
            "color: #7aabdb; font-weight: bold; font-size: 12px;"
        )
        _ab_row.addWidget(self._lbl_selected)
        _ab_row.addStretch()

        self._btn_action_view = QPushButton("Voir les photos")
        self._btn_action_view.setFixedHeight(26)
        self._btn_action_view.clicked.connect(self._on_action_view)
        _ab_row.addWidget(self._btn_action_view)

        self._btn_action_assign = QPushButton("Associer à…")
        self._btn_action_assign.setFixedHeight(26)
        self._btn_action_assign.clicked.connect(self._on_action_assign)
        _ab_row.addWidget(self._btn_action_assign)

        self._btn_action_ignore = QPushButton("Ignorer")
        self._btn_action_ignore.setFixedHeight(26)
        self._btn_action_ignore.clicked.connect(self._on_action_ignore)
        _ab_row.addWidget(self._btn_action_ignore)

        btn_deselect = QPushButton("✕")
        btn_deselect.setFixedSize(26, 26)
        btn_deselect.setToolTip("Désélectionner tout")
        btn_deselect.clicked.connect(self._clear_selection)
        _ab_row.addWidget(btn_deselect)

        self._action_bar.setVisible(False)
        root.addWidget(self._action_bar)

        # Zone de défilement
        self._content = QWidget()
        self._content_vbox = QVBoxLayout(self._content)
        self._content_vbox.setContentsMargins(0, 0, 0, 8)
        self._content_vbox.setSpacing(0)
        self._content_vbox.setAlignment(Qt.AlignTop)

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
        if cols != self._current_cols and self._sections:
            self._current_cols = cols
            self._reflow()

    def _reflow(self) -> None:
        for section in self._sections:
            section.reflow(self._current_cols)

    # ------------------------------------------------------------------ public

    def refresh(self) -> None:
        """Lance le chargement en arrière-plan et affiche un indicateur immédiatement."""
        # Arrêter le loader d'avatars en cours
        if self._loader and self._loader.isRunning():
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            self._loader = None

        # Arrêter un refresh précédent encore en cours
        if self._refresh_thread and self._refresh_thread.isRunning():
            try:
                self._refresh_thread.data_ready.disconnect()
            except RuntimeError:
                pass

        self._cards.clear()
        self._sections.clear()
        self._selected_ids.clear()
        self._action_bar.setVisible(False)

        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Spinner pendant le chargement
        self._lbl_title.setText("Chargement…")
        lbl_loading = QLabel("Chargement des groupes de visages…")
        lbl_loading.setAlignment(Qt.AlignCenter)
        lbl_loading.setStyleSheet("color: #666; font-size: 13px;")
        self._content_vbox.addWidget(lbl_loading)

        self._refresh_thread = _ClusterRefreshThread(self._face_db, self._catalog, self)
        self._refresh_thread.data_ready.connect(self._on_refresh_data_ready)
        self._refresh_thread.start()

    @Slot(object)
    def _on_refresh_data_ready(self, data: object) -> None:
        # Vider le spinner
        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if data is None:
            self._lbl_title.setText("Erreur lors du chargement")
            return

        self._build_from_data(data)

    def _build_from_data(self, data: dict) -> None:
        """Construit les widgets depuis les données pré-calculées par le thread.
        Ne fait aucun calcul lourd — tout est déjà dans data."""
        face_counts:               dict = data["face_counts"]
        groups_sorted:             list = data["groups_sorted"]
        group_labels:              dict = data["group_labels"]
        suggestions:               dict = data["suggestions"]
        representative_faces:      dict = data["representative_faces"]
        self._persons                   = data["persons"]
        self._person_cluster_embeddings = data["person_cluster_embeddings"]

        n = sum(len(g) for g in groups_sorted)

        if n == 0:
            self._lbl_title.setText("Aucun groupe à identifier")
            lbl = QLabel("Tous les groupes ont été identifiés ou ignorés.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #666; font-size: 13px;")
            self._content_vbox.addWidget(lbl)
            return

        plural = "s" if n > 1 else ""
        self._lbl_title.setText(
            f"{n} groupe{plural} de visages non identifié{plural}"
        )

        available = self._scroll.viewport().width()
        if available > 0:
            self._current_cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))

        avatar_items = []

        for group in groups_sorted:
            root = group[0]
            label, color = group_labels.get(root, ("", ""))
            group_by_size = sorted(group, key=lambda c: -face_counts.get(c, 0))

            section = _SectionWidget(label, color, self._content)

            for cluster_id in group_by_size:
                fc = face_counts.get(cluster_id, 0)
                sugg_id, sugg_label, sugg_color = suggestions.get(cluster_id, (None, "", ""))
                card = _ClusterCard(cluster_id, fc, sugg_id, sugg_label, sugg_color)
                card.selection_toggled.connect(self._on_card_selection_toggled)
                card.view_requested.connect(self._on_card_view_requested)
                card.name_requested.connect(self._on_card_name_requested)
                card.merge_requested.connect(self._on_card_merge_requested)
                card.ignore_requested.connect(self._on_card_ignore_requested)
                section.add_card(cluster_id, card)
                self._cards[cluster_id] = card

                rep = representative_faces.get(cluster_id)
                if rep:
                    avatar_items.append((cluster_id, rep))

            section.reflow(self._current_cols)
            self._content_vbox.addWidget(section)
            self._sections.append(section)

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
        self._lbl_selected.setText(f"{n} groupe{plural} sélectionné{plural}")
        self._btn_action_view.setVisible(n == 1)

    def _clear_selection(self) -> None:
        for cid, card in self._cards.items():
            if cid in self._selected_ids:
                card.set_selected(False)
        self._selected_ids.clear()
        self._action_bar.setVisible(False)

    # ------------------------------------------------------------------ slots cartes individuelles

    def _on_card_view_requested(self, cluster_id: int) -> None:
        face_count = next(
            (fc for cid, fc in self._face_db.get_unnamed_clusters() if cid == cluster_id), 0
        )
        plural = "s" if face_count > 1 else ""
        self.photos_requested.emit(cluster_id, f"Groupe {cluster_id} — {face_count} visage{plural}")

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

    # ------------------------------------------------------------------ barre d'action

    def _on_action_view(self) -> None:
        if len(self._selected_ids) == 1:
            self._on_card_view_requested(next(iter(self._selected_ids)))

    def _on_action_assign(self) -> None:
        cluster_ids = list(self._selected_ids)
        if not cluster_ids:
            return
        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)
        # Suggestion : meilleur score parmi tous les groupes sélectionnés
        # comparés aux centroïdes par groupe de chaque personne connue.
        best_sim, best_pid = 0.0, None
        for cid in cluster_ids:
            c_emb = self._face_db.get_representative_embedding(cluster_id=cid)
            if not c_emb:
                continue
            for p in self._persons:
                for p_emb in self._person_cluster_embeddings.get(p.id, {}).values():
                    sim = _cosine_sim(c_emb, p_emb)
                    if sim > best_sim:
                        best_sim, best_pid = sim, p.id
        dlg = _AssignDialog(
            cluster_ids[0],
            persons,
            suggested_person_id=best_pid if best_sim >= _SIM_WEAK else None,
            show_ignore=False,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.is_new_person():
            self.clusters_named.emit(cluster_ids, dlg.new_name())
        else:
            self.clusters_assigned.emit(cluster_ids, dlg.existing_person_id())
        self._clear_selection()

    def _on_action_ignore(self) -> None:
        for cid in list(self._selected_ids):
            self._face_db.ignore_cluster(cid)
            self.cluster_ignored.emit(cid)
        self._clear_selection()

    # ------------------------------------------------------------------ internal

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
