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
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _cosine_sim, _SIM_WEAK, _SIM_STRONG,
)

logger = logging.getLogger(__name__)

_CARD_IMG     = 130
_CARD_W       = 148
_CARD_SPACING = 10
_COLS_MIN     = 2
_SIM_GROUP    = 0.72   # seuil pour regrouper deux clusters "même personne probable"
_BUILD_BATCH  = 10     # sections créées par tick de l'event loop (évite de bloquer l'UI)


# ------------------------------------------------------------------ helpers (module-level, utilisés par le thread)

def _compute_cluster_groups_bg(
    cluster_ids: list[int],
    embeddings: dict[int, list[float]],
    progress_cb=None,
) -> dict[int, list[int]]:
    """Union-Find vectorisé : regroupe les clusters dont sim(centroïde) ≥ _SIM_GROUP.

    Avec numpy disponible, construit une matrice normalisée une seule fois et
    calcule la similarité de chaque ligne avec toutes les suivantes via un produit
    matriciel BLAS — O(n²·dim) opérations mais sans allocation Python par paire.
    progress_cb(i) est appelé au début de chaque itération externe."""
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

    valid = [(cid, embeddings[cid]) for cid in cluster_ids if cid in embeddings]

    try:
        import numpy as np
        if valid:
            ids_arr = [cid for cid, _ in valid]
            m = len(ids_arr)
            mat = np.array([e for _, e in valid], dtype=np.float32)  # (m, dim)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            mat /= np.where(norms > 1e-8, norms, 1.0)              # normalisation in-place
            for i in range(m):
                if progress_cb is not None:
                    progress_cb(i)
                if i + 1 >= m:
                    break
                # Similarités avec toutes les lignes i+1..m-1 en un produit BLAS
                sims = mat[i] @ mat[i + 1:].T                       # ndarray (m-1-i,)
                for j_off in np.nonzero(sims >= _SIM_GROUP)[0]:
                    union(ids_arr[i], ids_arr[i + 1 + int(j_off)])
    except ImportError:
        ids = list(cluster_ids)
        for i, ci in enumerate(ids):
            if progress_cb is not None:
                progress_cb(i)
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
    """Calcule la meilleure suggestion de personne pour un cluster (fallback scalaire)."""
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


def _compute_all_suggestions_bg(
    cluster_ids: list[int],
    cluster_embeddings: dict[int, list[float]],
    persons: list,
    person_cluster_embeddings: dict[int, dict[int, list[float]]],
) -> "dict[int, tuple[int | None, str, str]]":
    """Calcule les suggestions pour tous les clusters en un seul produit matriciel.

    Construit (n_clusters, dim) × (n_person_emb, dim)^T → matrice de similarité
    complète, puis sélectionne le maximum par ligne. Remplace la boucle Python
    de N appels _compute_suggestion_bg."""
    result: dict = {cid: (None, "", "") for cid in cluster_ids}

    if not persons or not person_cluster_embeddings:
        return result

    # Liste plate (person, embedding) pour toutes les personnes connues
    person_emb_pairs: list = []
    for p in persons:
        for p_emb in person_cluster_embeddings.get(p.id, {}).values():
            person_emb_pairs.append((p, p_emb))
    if not person_emb_pairs:
        return result

    valid_c = [(cid, cluster_embeddings[cid]) for cid in cluster_ids if cid in cluster_embeddings]
    if not valid_c:
        return result

    try:
        import numpy as np
        cid_arr = [cid for cid, _ in valid_c]
        c_mat = np.array([e for _, e in valid_c], dtype=np.float32)     # (nc, dim)
        c_norms = np.linalg.norm(c_mat, axis=1, keepdims=True)
        c_mat /= np.where(c_norms > 1e-8, c_norms, 1.0)

        p_mat = np.array([e for _, e in person_emb_pairs], dtype=np.float32)  # (np, dim)
        p_norms = np.linalg.norm(p_mat, axis=1, keepdims=True)
        p_mat /= np.where(p_norms > 1e-8, p_norms, 1.0)

        sim_mat  = c_mat @ p_mat.T                                      # (nc, np_emb)
        best_idx = np.argmax(sim_mat, axis=1)                           # (nc,)
        best_sim = sim_mat[np.arange(len(cid_arr)), best_idx]           # (nc,)

        for k, cid in enumerate(cid_arr):
            s = float(best_sim[k])
            if s < _SIM_WEAK:
                continue
            best_p = person_emb_pairs[int(best_idx[k])][0]
            pct = int(s * 100)
            if s >= 0.82:
                result[cid] = (best_p.id, f"≈ {best_p.name} ({pct} %)", "#7aabdb")
            else:
                result[cid] = (best_p.id, f"~ {best_p.name} ({pct} %)", "#888")
    except ImportError:
        for cid in cluster_ids:
            result[cid] = _compute_suggestion_bg(
                cid, cluster_embeddings, persons, person_cluster_embeddings
            )

    return result


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
            outer.addLayout(hdr_row)

            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet(f"background: {color}; border: none;")
            sep.setFixedHeight(1)
            outer.addWidget(sep)

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

class _ClusterRefreshThread(QThread):
    """
    Chargement en deux phases pour un affichage progressif.

    Phase 1 — initial_ready (rapide, < 1 s) :
        2 requêtes SQL (face counts + faces représentatives).
        Émet une structure plate (1 groupe = 1 cluster) sans suggestions.
        Les cartes peuvent être affichées immédiatement.

    Phase 2 — data_ready (lent, O(n²)) :
        Embeddings, Union-Find, suggestions.
        Émet la structure complète groupée avec suggestions.
    """

    initial_ready = Signal(object)         # dict — affiché immédiatement
    data_ready    = Signal(object)         # dict | None — affiché après calcul lourd
    progress      = Signal(int, int, str)  # étape courante, total, message

    def __init__(self, face_db: "FaceDatabase", catalog, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog

    def run(self) -> None:
        try:
            # ── Récupération initiale (rapide) — N encore inconnu ──────────
            self.progress.emit(0, 0, "Récupération des groupes de visages…")
            clusters = self._face_db.get_unnamed_clusters()

            _empty = {
                "face_counts": {}, "groups_sorted": [], "group_labels": {},
                "suggestions": {}, "representative_faces": {}, "persons": [],
                "person_cluster_embeddings": {}, "is_partial": False,
            }
            if not clusters:
                self.progress.emit(1, 1, "Aucun groupe à analyser")
                self.initial_ready.emit(_empty)
                self.data_ready.emit(_empty)
                return

            cluster_ids = [cid for cid, _ in clusters]
            face_counts = {cid: fc for cid, fc in clusters}
            n  = len(cluster_ids)
            s  = "s" if n > 1 else ""

            # Total d'étapes = 5 fixes + ≤100 mises à jour Union-Find + 1 suggestions.
            # Les suggestions sont vectorisées (1 opération matricielle) donc 1 seule étape.
            n_uf_steps = min(n, 100)
            N          = 5 + n_uf_steps + 1
            step       = 0

            # ── Phase 1 : structure plate, sans suggestion ─────────────────
            step += 1
            self.progress.emit(step, N, f"Chargement des visages représentatifs ({n} groupe{s})…")
            representative_faces = self._face_db.get_all_representative_faces(cluster_ids)
            flat_groups = [[cid] for cid in cluster_ids]   # déjà trié DESC par face_count
            self.initial_ready.emit({
                "face_counts":               face_counts,
                "groups_sorted":             flat_groups,
                "group_labels":              {},
                "suggestions":               {},
                "representative_faces":      representative_faces,
                "persons":                   [],
                "person_cluster_embeddings": {},
                "is_partial":                True,
            })

            # ── Phase 2 : embeddings clusters ─────────────────────────────
            step += 1
            self.progress.emit(step, N, f"Calcul des représentations vectorielles ({n} groupe{s})…")
            cluster_embeddings = self._face_db.get_all_cluster_centroids(cluster_ids)

            # Affiner N maintenant qu'on connaît le nombre de clusters avec embedding
            m_emb      = len(cluster_embeddings)
            n_uf_steps = min(m_emb, 100)
            N          = step + 3 + n_uf_steps + 1   # restants : 3 fixes + UF + suggestion

            # ── Phase 2 : personnes connues ───────────────────────────────
            step += 1
            self.progress.emit(step, N, "Récupération des personnes connues…")
            persons    = self._catalog.get_persons()
            np_        = len(persons)
            sp         = "s" if np_ > 1 else ""

            step += 1
            self.progress.emit(step, N, f"Analyse des personnes connues ({np_} personne{sp})…")
            self._face_db.enrich_persons(persons)
            person_ids = [p.id for p in persons]

            step += 1
            self.progress.emit(step, N, "Représentations vectorielles des personnes…")
            person_cluster_embeddings = self._face_db.get_all_person_cluster_centroids(person_ids)

            # ── Phase 2 : Union-Find vectorisé, progression au % près ─────
            _last_uf_pct = -1

            def uf_progress(i: int) -> None:
                nonlocal step, _last_uf_pct
                pct = i * 100 // m_emb if m_emb else 100
                if pct != _last_uf_pct:
                    _last_uf_pct = pct
                    step += 1
                    self.progress.emit(
                        step, N,
                        f"Regroupement des visages similaires… {pct} %",
                    )

            raw_groups    = _compute_cluster_groups_bg(cluster_ids, cluster_embeddings, uf_progress)
            groups_sorted = sorted(
                raw_groups.values(),
                key=lambda g: (-len(g), -sum(face_counts.get(c, 0) for c in g)),
            )

            group_labels: dict[int, tuple[str, str]] = {}
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
                    pct     = int(avg_sim * 100)
                    n_faces = sum(face_counts.get(c, 0) for c in group)
                    fp      = "s" if n_faces > 1 else ""
                    label   = (
                        f"≈ Probablement la même personne"
                        f"  —  {len(group)} groupes, {n_faces} visage{fp}"
                        f"  (sim. {pct} %)"
                    )
                    color = "#7aabdb" if avg_sim >= _SIM_STRONG else "#aaa"
                    group_labels[root] = (label, color)
                else:
                    group_labels[root] = ("", "")

            # ── Phase 2 : suggestions vectorisées (1 produit matriciel) ────
            step += 1
            self.progress.emit(
                step, N,
                "Calcul des suggestions d'identification…"
                + (f"  —  {np_} personne{sp} connue{sp}" if np_ else ""),
            )
            suggestions = _compute_all_suggestions_bg(
                cluster_ids, cluster_embeddings, persons, person_cluster_embeddings
            )

            self.data_ready.emit({
                "face_counts":               face_counts,
                "groups_sorted":             groups_sorted,
                "group_labels":              group_labels,
                "suggestions":               suggestions,
                "representative_faces":      representative_faces,
                "persons":                   persons,
                "person_cluster_embeddings": person_cluster_embeddings,
                "is_partial":                False,
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
        # Cache des bytes JPEG d'avatars : persiste entre la phase 1 et la phase 2
        # pour éviter de relire les fichiers lors du rebuild avec suggestions.
        self._avatar_cache: dict[int, bytes] = {}
        self._build_generation: int = 0   # annule les lots en file si un nouveau build démarre
        self._cached_data: dict | None = None  # dernières données complètes (phase 2)
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

        # Barre de progression (visible pendant le chargement, cachée ensuite)
        self._progress_widget = QWidget()
        self._progress_widget.setVisible(False)
        _pw_vbox = QVBoxLayout(self._progress_widget)
        _pw_vbox.setContentsMargins(0, 2, 0, 4)
        _pw_vbox.setSpacing(3)

        _pw_top = QHBoxLayout()
        _pw_top.setContentsMargins(0, 0, 0, 0)
        self._lbl_progress = QLabel("Initialisation…")
        self._lbl_progress.setStyleSheet("color: #aaa; font-size: 11px;")
        _pw_top.addWidget(self._lbl_progress)
        _pw_top.addStretch()
        self._lbl_progress_step = QLabel("")
        self._lbl_progress_step.setStyleSheet("color: #555; font-size: 11px;")
        _pw_top.addWidget(self._lbl_progress_step)
        _pw_vbox.addLayout(_pw_top)

        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(5)
        self._progress_bar.setRange(0, 6)
        self._progress_bar.setValue(0)
        self._progress_bar.setStyleSheet(
            "QProgressBar { background: #2a2a2a; border: none; border-radius: 2px; }"
            "QProgressBar::chunk { background: #4a8fd4; border-radius: 2px; }"
        )
        _pw_vbox.addWidget(self._progress_bar)
        root.addWidget(self._progress_widget)

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

    def _force_reflow(self) -> None:
        """Recalcule les colonnes depuis la largeur réelle et replace toutes les cartes.
        Appelé en différé après un build pour corriger le cas où le viewport
        n'était pas encore dimensionné au moment du calcul initial."""
        available = self._scroll.viewport().width()
        if available <= 0:
            QTimer.singleShot(50, self._force_reflow)  # viewport pas encore prêt
            return
        self._current_cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))
        self._reflow()

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

        self._build_generation += 1   # annule tout build par lots encore en file d'attente
        self._cards.clear()
        self._sections.clear()
        self._selected_ids.clear()
        self._action_bar.setVisible(False)

        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Afficher la barre de progression et réinitialiser
        self._progress_bar.setValue(0)
        self._lbl_progress.setText("Initialisation…")
        self._lbl_progress_step.setText("")
        self._progress_widget.setVisible(True)
        self._lbl_title.setText("Chargement…")

        self._refresh_thread = _ClusterRefreshThread(self._face_db, self._catalog, self)
        self._refresh_thread.initial_ready.connect(self._on_initial_ready)
        self._refresh_thread.data_ready.connect(self._on_data_ready)
        self._refresh_thread.progress.connect(self._on_progress)
        self._refresh_thread.start()

    def remove_clusters(self, cluster_ids: list[int]) -> None:
        """Retire les groupes donnés de l'affichage sans relancer les calculs.

        Supprime les cartes directement et met à jour _cached_data.
        Si un thread est en cours (Phase 2 non terminée), relance refresh()
        pour repartir sur des données fraîches du DB."""
        if not cluster_ids:
            return
        if (self._refresh_thread and self._refresh_thread.isRunning()) or self._cached_data is None:
            self.refresh()
            return

        cid_set = set(cluster_ids)

        # Cartes : retirer du registre et de l'avatar cache
        removed_from_sel = cid_set & self._selected_ids
        self._selected_ids -= removed_from_sel
        for cid in cid_set:
            card = self._cards.pop(cid, None)
            if card is not None:
                card.deleteLater()
            self._avatar_cache.pop(cid, None)

        # Sections : filtrer les entrées, reflow si encore peuplée, supprimer sinon
        sections_to_keep = []
        for section in self._sections:
            section._entries = [(c, w) for c, w in section._entries if c not in cid_set]
            if section._entries:
                section.reflow(self._current_cols)
                sections_to_keep.append(section)
            else:
                idx = self._content_vbox.indexOf(section)
                if idx >= 0:
                    self._content_vbox.takeAt(idx)
                section.deleteLater()
        self._sections = sections_to_keep

        # Cache : supprimer les clusters des dicts et reconstruire groups_sorted / group_labels
        data = self._cached_data
        for cid in cid_set:
            data["face_counts"].pop(cid, None)
            data["representative_faces"].pop(cid, None)
            data["suggestions"].pop(cid, None)
        new_groups: list[list[int]] = []
        new_labels: dict[int, tuple[str, str]] = {}
        for old_group in data.get("groups_sorted", []):
            old_root  = old_group[0]
            new_group = [c for c in old_group if c not in cid_set]
            if not new_group:
                continue
            new_root = new_group[0]
            new_groups.append(new_group)
            new_labels[new_root] = ("", "") if len(new_group) == 1 else data["group_labels"].get(old_root, ("", ""))
        data["groups_sorted"] = new_groups
        data["group_labels"]  = new_labels

        # Barre d'action
        if removed_from_sel:
            self._update_action_bar()

    def restore(self) -> None:
        """Restaure la grille depuis le cache sans relancer les calculs lourds.
        Appelé lors d'un retour de navigation (ex : retour depuis les photos d'un groupe).
        Si aucune donnée n'est en cache, déclenche un refresh() complet."""
        if self._cached_data is None:
            self.refresh()
            return

        # Arrêter un loader d'avatars en cours
        if self._loader and self._loader.isRunning():
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            self._loader = None

        self._progress_widget.setVisible(False)
        self._build_generation += 1
        self._cards.clear()
        self._sections.clear()
        self._selected_ids.clear()
        self._action_bar.setVisible(False)

        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._build_from_data(self._cached_data)

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, message: str) -> None:
        self._progress_bar.setRange(0, total)
        self._progress_bar.setValue(current)
        self._lbl_progress.setText(message)
        if total > 0:
            pct = current * 100 // total
            self._lbl_progress_step.setText(f"{pct} %")
        else:
            self._lbl_progress_step.setText("")

    @Slot(object)
    def _on_initial_ready(self, data: object) -> None:
        """Phase 1 : affiche immédiatement les cartes en liste plate (sans suggestions)."""
        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if data is None:
            return
        self._build_from_data(data)

    @Slot(object)
    def _on_data_ready(self, data: object) -> None:
        """Phase 2 : reconstruit la vue avec groupes similaires et suggestions."""
        self._progress_widget.setVisible(False)
        if data is None:
            self._lbl_title.setText("Erreur lors du chargement")
            return
        if data.get("is_partial"):
            return  # ne devrait pas arriver, mais garde-fou

        # Mémoriser les données finales pour restore()
        self._cached_data = data

        # Arrêter le loader d'avatars de la phase 1 (les bytes sont dans _avatar_cache)
        if self._loader and self._loader.isRunning():
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            self._loader = None

        # Vider et reconstruire
        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._sections.clear()
        self._selected_ids.clear()
        self._action_bar.setVisible(False)
        self._build_from_data(data)

    def _build_from_data(self, data: dict) -> None:
        """Lance la construction par lots (_BUILD_BATCH groupes par tick) pour ne pas
        bloquer l'UI lors de grandes bibliothèques.  Tout calcul lourd est déjà dans data.

        Stratégie de mise en page :
        • Groupes de 2+ clusters similaires → chacun sa propre _SectionWidget avec en-tête.
        • Clusters isolés (groupe de 1) → tous dans une unique section plate partagée,
          dont la grille est remplie sur plusieurs colonnes (corrige le bug 'colonne gauche').
        """
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

        is_partial = data.get("is_partial", False)
        plural = "s" if n > 1 else ""
        if is_partial:
            self._lbl_title.setText(f"{n} groupe{plural} — analyse en cours…")
        else:
            self._lbl_title.setText(f"{n} groupe{plural} de visages non identifié{plural}")

        available = self._scroll.viewport().width()
        if available > 0:
            self._current_cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))

        # Incrémenter la génération : tout callback _next encore en file d'attente
        # avec l'ancienne génération s'arrêtera immédiatement.
        self._build_generation += 1
        gen = self._build_generation
        avatar_items: list = []

        # Section plate partagée pour tous les clusters isolés.
        # Elle est construite de façon incrémentale au fil des lots, puis ajoutée
        # au vbox et reflowée une seule fois lors du dernier lot.
        flat_section = _SectionWidget("", "", self._content)

        def _add_card(cluster_id: int, target: _SectionWidget) -> None:
            fc = face_counts.get(cluster_id, 0)
            sugg_id, sugg_label, sugg_color = suggestions.get(cluster_id, (None, "", ""))
            card = _ClusterCard(
                cluster_id, fc, sugg_id, sugg_label, sugg_color,
                parent=target._card_area,   # évite les widgets orphelins entre lots
            )
            card.selection_toggled.connect(self._on_card_selection_toggled)
            card.view_requested.connect(self._on_card_view_requested)
            card.name_requested.connect(self._on_card_name_requested)
            card.merge_requested.connect(self._on_card_merge_requested)
            card.ignore_requested.connect(self._on_card_ignore_requested)
            target.add_card(cluster_id, card)
            self._cards[cluster_id] = card
            if cluster_id in self._avatar_cache:
                card.set_avatar(self._avatar_cache[cluster_id])
            else:
                rep = representative_faces.get(cluster_id)
                if rep:
                    avatar_items.append((cluster_id, rep))

        def _next(start: int) -> None:
            if gen != self._build_generation:
                return  # build annulé par refresh() ou _on_data_ready()
            end = min(start + _BUILD_BATCH, len(groups_sorted))
            for idx in range(start, end):
                group = groups_sorted[idx]
                root  = group[0]
                if len(group) == 1:
                    # Cluster isolé → section plate commune (plusieurs par ligne)
                    _add_card(root, flat_section)
                else:
                    # Groupe de clusters similaires → section dédiée avec en-tête
                    label, color  = group_labels.get(root, ("", ""))
                    group_by_size = sorted(group, key=lambda c: -face_counts.get(c, 0))
                    section = _SectionWidget(label, color, self._content)
                    for cluster_id in group_by_size:
                        _add_card(cluster_id, section)
                    section.reflow(self._current_cols)
                    self._content_vbox.addWidget(section)
                    self._sections.append(section)
            if end < len(groups_sorted):
                QTimer.singleShot(0, lambda: _next(end))
            else:
                # Dernier lot : finaliser la section plate puis le layout
                if flat_section._entries:
                    flat_section.reflow(self._current_cols)
                    self._content_vbox.addWidget(flat_section)
                    self._sections.append(flat_section)
                self._content_vbox.addStretch(1)
                if avatar_items:
                    QTimer.singleShot(
                        0, lambda items=avatar_items: self._start_cluster_loader(items)
                    )
                QTimer.singleShot(0, self._force_reflow)

        QTimer.singleShot(0, lambda: _next(0))

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
        self._avatar_cache[cluster_id] = data   # conservé pour la phase 2
        card = self._cards.get(cluster_id)
        if card:
            card.set_avatar(data)
