# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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
_CARD_SPACING  = 10
_COLS_MIN      = 2
_SIM_GROUP     = 0.72   # seuil pour regrouper deux clusters "même personne probable"
_BUILD_BATCH   = 10     # cartes créées par tick de l'event loop (évite de bloquer l'UI)
_PAGE_SIZE     = 200    # nombre de cartes rendues par page (pagination)
_UF_CHUNK      = 500    # lignes par bloc dans le produit matriciel de l'Union-Find
                        # RAM pic ≈ _UF_CHUNK × n × 4 octets  (500 × 50k × 4 = 100 Mo)
UNION_FIND_MAX = 80_000 # skip UF au-delà (temps > 2 min même en mode blocs)

# ------------------------------------------------------------------ modules extraits
# (2026-07) Cartes/sections, dialogue de fusion et threads déplacés dans leurs
# modules ; noms ré-exportés sous leurs noms historiques.
from src.ui.face_cluster_cards import (  # noqa: E402,F401
    _BTN_ACCEPT_STYLE, _BTN_OVL, _BTN_REJECT_STYLE, _ClusterCard, _SectionWidget,
)
from src.ui.face_merge_dialog import _MergePickerDialog, _MergeRow  # noqa: E402,F401
from src.ui.face_cluster_workers import (  # noqa: E402,F401
    _ClusterRefreshThread, _PersonsLoader,
    _compute_all_suggestions_bg, _compute_cluster_groups_bg, _compute_suggestion_bg,
)

class _ProgressPopup(QDialog):
    """Dialogue modal-less affiché pendant le calcul Union-Find."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setFixedWidth(380)

        self.setStyleSheet(
            "QDialog { background: #252535; border: 1px solid #445; border-radius: 8px; }"
            "QLabel  { color: #ddd; background: transparent; }"
        )

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(24, 20, 24, 20)
        vbox.setSpacing(12)

        lbl_title = QLabel("Analyse des groupes de visages")
        lbl_title.setStyleSheet(
            "font-weight: bold; font-size: 13px; color: #eee; background: transparent;"
        )
        vbox.addWidget(lbl_title)

        self._lbl_phase = QLabel("Initialisation…")
        self._lbl_phase.setStyleSheet(
            "font-size: 11px; color: #aaa; background: transparent;"
        )
        self._lbl_phase.setWordWrap(True)
        vbox.addWidget(self._lbl_phase)

        bar_row = QHBoxLayout()
        bar_row.setSpacing(8)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setStyleSheet(
            "QProgressBar { background: #1e1e2e; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #4a8fd4; border-radius: 3px; }"
        )
        bar_row.addWidget(self._bar)
        self._lbl_pct = QLabel("0 %")
        self._lbl_pct.setFixedWidth(38)
        self._lbl_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._lbl_pct.setStyleSheet("font-size: 11px; color: #888; background: transparent;")
        bar_row.addWidget(self._lbl_pct)
        vbox.addLayout(bar_row)

    def update_progress(self, current: int, total: int, message: str) -> None:
        self._lbl_phase.setText(message)
        if total > 0:
            pct = current * 100 // total
            self._bar.setRange(0, 100)
            self._bar.setValue(pct)
            self._lbl_pct.setText(f"{pct} %")
        else:
            self._bar.setRange(0, 0)   # animation indéterminée
            self._lbl_pct.setText("")

    def center_on_parent(self) -> None:
        p = self.parentWidget()
        if p is None:
            return
        self.adjustSize()
        center = p.mapToGlobal(p.rect().center())
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)


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
    clusters_ignored(cluster_ids)             — ignorer N groupes/visages sélectionnés
    cluster_merged(source_id, target_id)      — fusionner deux groupes
    back_requested()                          — retourner à la grille de photos
    photos_requested(cluster_id, label)       — afficher les photos d'un groupe
    """

    cluster_named      = Signal(int, str)
    cluster_assigned   = Signal(int, int)
    clusters_named     = Signal(list, str)    # list[int], name
    clusters_assigned  = Signal(list, int)    # list[int], person_id
    cluster_ignored    = Signal(int)
    clusters_ignored   = Signal(list)         # list[int]
    cluster_merged     = Signal(int, int)
    back_requested     = Signal()
    photos_requested   = Signal(int, str)
    persons_updated    = Signal()             # des suggestions ont été promu → rafraîchir la sidebar

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
        # Pagination
        self._all_combined:      list            = []
        self._rendered_count:    int             = 0
        self._flat_section:      "_SectionWidget | None" = None
        self._solo_section:      "_SectionWidget | None" = None
        self._load_more_btn:     "QPushButton | None"    = None
        self._pending_build_data: "dict | None"          = None
        # Mémorisation de la position de scroll pour restore()
        self._saved_scroll_pos:       int  = 0
        self._restore_scroll_on_build: bool = False
        # Ancre pour la sélection étendue Maj+clic
        self._anchor_id: "int | None" = None
        # Popup de progression (affiché pendant le calcul Union-Find)
        self._progress_popup: "_ProgressPopup | None" = None
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
        # Arrêter le loader d'avatars en cours et libérer le thread Qt enfant
        if self._loader is not None:
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            if self._loader.isRunning():
                self._loader.finished.connect(self._loader.deleteLater)
            else:
                self._loader.deleteLater()
            self._loader = None

        # Arrêter un refresh précédent encore en cours et libérer le thread Qt enfant
        if self._refresh_thread is not None:
            if self._refresh_thread.isRunning():
                try:
                    self._refresh_thread.data_ready.disconnect()
                    self._refresh_thread.initial_ready.disconnect()
                except RuntimeError:
                    pass
                self._refresh_thread.finished.connect(self._refresh_thread.deleteLater)
            else:
                self._refresh_thread.deleteLater()
            self._refresh_thread = None

        # Vider le cache d'avatars : les cluster_id changent à chaque re-clustering,
        # les anciennes entrées s'accumuleraient sans cette purge.
        self._avatar_cache.clear()

        self._build_generation += 1   # annule tout build par lots encore en file d'attente
        self._cards.clear()
        self._sections.clear()
        self._selected_ids.clear()
        self._action_bar.setVisible(False)
        self._anchor_id = None
        # Pagination : reset
        self._all_combined = []
        self._rendered_count = 0
        self._flat_section = None
        self._solo_section = None
        self._load_more_btn = None
        self._pending_build_data = None

        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Cacher la petite barre interne et ouvrir la popup de progression
        self._progress_widget.setVisible(False)
        self._lbl_title.setText("Chargement…")

        if self._progress_popup is not None:
            self._progress_popup.close()
            self._progress_popup = None
        self._progress_popup = _ProgressPopup(self)
        self._progress_popup.update_progress(0, 100, "Initialisation…")
        self._progress_popup.show()
        self._progress_popup.center_on_parent()

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

        # Pagination : retirer aussi des entrées non encore rendues
        self._all_combined = [
            (kind, [c for c in group if c not in cid_set])
            for kind, group in self._all_combined
        ]
        self._all_combined = [(k, g) for k, g in self._all_combined if g]
        # Mettre à jour le bouton si les données pendantes changent
        if self._load_more_btn is not None:
            remaining = len(self._all_combined) - self._rendered_count
            if remaining <= 0:
                self._content_vbox.removeWidget(self._load_more_btn)
                self._load_more_btn.deleteLater()
                self._load_more_btn = None
            else:
                next_n = min(_PAGE_SIZE, remaining)
                self._load_more_btn.setText(
                    f"Charger {next_n} de plus  "
                    f"({remaining} restant{'s' if remaining > 1 else ''})"
                )

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

        # Arrêter un loader d'avatars en cours et libérer le thread Qt enfant
        if self._loader is not None:
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            if self._loader.isRunning():
                self._loader.finished.connect(self._loader.deleteLater)
            else:
                self._loader.deleteLater()
            self._loader = None

        self._progress_widget.setVisible(False)
        self._build_generation += 1
        self._cards.clear()
        self._sections.clear()
        self._selected_ids.clear()
        self._action_bar.setVisible(False)
        self._anchor_id = None
        # Pagination : reset (le while loop suivant supprime load_more_btn du layout)
        self._all_combined = []
        self._rendered_count = 0
        self._flat_section = None
        self._solo_section = None
        self._load_more_btn = None
        self._pending_build_data = None

        while self._content_vbox.count():
            item = self._content_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._restore_scroll_on_build = True
        self._build_from_data(self._cached_data)

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, message: str) -> None:
        if self._progress_popup is not None:
            self._progress_popup.update_progress(current, total, message)
        else:
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
        """Phase 1 : affiche immédiatement les cartes en liste plate (sans suggestions).
        Si la popup de progression est visible, on attend la phase 2 pour afficher."""
        if self._progress_popup is not None:
            return   # la popup est ouverte — on saute la phase 1 et attend data_ready
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
        if self._progress_popup is not None:
            self._progress_popup.close()
            self._progress_popup = None
        self._progress_widget.setVisible(False)
        if data is None:
            self._lbl_title.setText("Erreur lors du chargement")
            return
        if data.get("is_partial"):
            return  # ne devrait pas arriver, mais garde-fou

        # Mémoriser les données finales pour restore()
        self._cached_data = data

        # Si des suggestions ont été promu en Phase 2, notifier la sidebar
        if data.get("n_promoted", 0) > 0:
            self.persons_updated.emit()

        # Arrêter le loader d'avatars de la phase 1 (les bytes sont dans _avatar_cache)
        if self._loader is not None:
            try:
                self._loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            if self._loader.isRunning():
                self._loader.finished.connect(self._loader.deleteLater)
            else:
                self._loader.deleteLater()
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
        # Pagination : reset (le while loop a purgé load_more_btn du layout)
        self._all_combined = []
        self._rendered_count = 0
        self._flat_section = None
        self._solo_section = None
        self._load_more_btn = None
        self._pending_build_data = None
        self._build_from_data(data)

    def _build_from_data(self, data: dict) -> None:
        """Construit la grille en lots (ne bloque pas l'UI).

        Affiche les _PAGE_SIZE premières cartes immédiatement, puis offre
        un bouton "Charger N de plus" pour les pages suivantes.

        Stratégie de mise en page :
        • Groupes de 2+ clusters similaires → section dédiée avec en-tête.
        • Clusters isolés avec face_count > 1 → section plate commune.
        • Clusters de 1 seul visage (visages isolés) → section "Visages isolés" en bas.
        """
        face_counts               = data["face_counts"]
        groups_sorted             = data["groups_sorted"]
        group_labels              = data["group_labels"]
        suggestions               = data["suggestions"]
        representative_faces      = data["representative_faces"]
        self._persons                   = data["persons"]
        self._person_cluster_embeddings = data["person_cluster_embeddings"]

        main_groups: list[list[int]] = []
        solo_ids:    list[int]       = []
        for group in groups_sorted:
            if len(group) == 1 and face_counts.get(group[0], 0) == 1:
                solo_ids.append(group[0])
            else:
                main_groups.append(group)

        n_groups = sum(len(g) for g in main_groups)
        n_solos  = len(solo_ids)
        n        = n_groups + n_solos

        if n == 0:
            self._lbl_title.setText("Aucun groupe à identifier")
            lbl = QLabel("Tous les groupes ont été identifiés ou ignorés.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #666; font-size: 13px;")
            self._content_vbox.addWidget(lbl)
            return

        is_partial = data.get("is_partial", False)
        parts = []
        if n_groups > 0:
            parts.append(f"{n_groups} groupe{'s' if n_groups > 1 else ''}")
        if n_solos > 0:
            parts.append(f"{n_solos} visage{'s isolés' if n_solos > 1 else ' isolé'}")
        suffix = " — analyse en cours…" if is_partial else ""
        self._lbl_title.setText(", ".join(parts) + suffix)

        available = self._scroll.viewport().width()
        if available > 0:
            self._current_cols = max(_COLS_MIN, available // (_CARD_W + _CARD_SPACING))

        self._build_generation += 1
        gen = self._build_generation

        # ── Regrouper les singletons partageant la même suggestion de personne ──
        if suggestions:
            persons_by_id = {p.id: p for p in (self._persons or [])}
            singleton_by_person: dict[int, list[int]] = {}
            singleton_scores: dict[int, float] = {}
            flat_singletons: list[int] = []
            rebuilt_main: list[list[int]] = []
            for g in main_groups:
                if len(g) > 1:
                    rebuilt_main.append(g)
                else:
                    cid = g[0]
                    pid, _, _, score = suggestions.get(cid, (None, "", "", 0.0))
                    if pid is not None and score >= _SIM_WEAK:
                        singleton_by_person.setdefault(pid, []).append(cid)
                        singleton_scores[cid] = score
                    else:
                        flat_singletons.append(cid)
            for pid, cids in sorted(singleton_by_person.items(),
                                    key=lambda kv: -sum(face_counts.get(c, 0) for c in kv[1])):
                if len(cids) >= 2:
                    cids = sorted(cids, key=lambda c: -face_counts.get(c, 0))
                    p = persons_by_id.get(pid)
                    p_name = p.name if p else f"Personne #{pid}"
                    n_f = sum(face_counts.get(c, 0) for c in cids)
                    fp = "s" if n_f > 1 else ""
                    best_score = max(singleton_scores.get(c, 0.0) for c in cids)
                    pct = round(best_score * 100)
                    group_labels[cids[0]] = (
                        f"≈ Probablement {p_name} ({pct} %)"
                        f"  —  {len(cids)} groupe{'s' if len(cids) > 1 else ''},"
                        f" {n_f} visage{fp}",
                        "#7aabdb",
                    )
                    rebuilt_main.append(cids)
                else:
                    flat_singletons.extend(cids)
            for cid in flat_singletons:
                rebuilt_main.append([cid])
            rebuilt_main.sort(key=lambda g: (-len(g), -sum(face_counts.get(c, 0) for c in g)))
            main_groups = rebuilt_main

        combined: list[tuple[str, list[int]]] = (
            [("group", g) for g in main_groups]
            + [("solo", [sid]) for sid in solo_ids]
        )
        self._all_combined      = combined
        self._rendered_count    = 0
        self._pending_build_data = data

        flat_section = _SectionWidget("", "", self._content)
        solo_section = _SectionWidget("Visages isolés", "#666", self._content)
        self._flat_section = flat_section
        self._solo_section = solo_section

        avatar_items: list = []

        def _add_card(cluster_id: int, target: _SectionWidget, is_solo: bool = False, eject: bool = False) -> None:
            fc = face_counts.get(cluster_id, 0)
            sugg_id, sugg_label, sugg_color, _ = suggestions.get(cluster_id, (None, "", "", 0.0))
            card = _ClusterCard(
                cluster_id, fc, sugg_id, sugg_label, sugg_color,
                is_solo=is_solo,
                show_eject=eject,
                selected_ids_ref=self._selected_ids,
                parent=target._card_area,
            )
            card.selection_toggled.connect(self._on_card_selection_toggled)
            card.range_select_requested.connect(self._on_range_select)
            card.view_requested.connect(self._on_card_view_requested)
            card.name_requested.connect(self._on_card_name_requested)
            card.quick_accept_requested.connect(self._on_card_quick_accept)
            card.merge_requested.connect(self._on_card_merge_requested)
            card.associate_requested.connect(self._on_card_associate_requested)
            card.ignore_requested.connect(self._on_card_ignore_requested)
            card.ignore_selection_requested.connect(self._on_card_ignore_selection_requested)
            card.eject_from_section_requested.connect(self._on_card_eject_from_section)
            target.add_card(cluster_id, card)
            self._cards[cluster_id] = card
            if cluster_id in self._avatar_cache:
                card.set_avatar(self._avatar_cache[cluster_id])
            else:
                rep = representative_faces.get(cluster_id)
                if rep:
                    avatar_items.append((cluster_id, rep))

        def _page_done(rendered_total: int) -> None:
            if gen != self._build_generation:
                return
            if flat_section._entries and self._content_vbox.indexOf(flat_section) < 0:
                flat_section.reflow(self._current_cols)
                self._content_vbox.addWidget(flat_section)
                self._sections.append(flat_section)
            elif flat_section._entries:
                flat_section.reflow(self._current_cols)
            if solo_section._entries and self._content_vbox.indexOf(solo_section) < 0:
                solo_section.reflow(self._current_cols)
                self._content_vbox.addWidget(solo_section)
                self._sections.append(solo_section)
            elif solo_section._entries:
                solo_section.reflow(self._current_cols)
            remaining = len(combined) - rendered_total
            if remaining > 0:
                next_n = min(_PAGE_SIZE, remaining)
                if self._load_more_btn is None:
                    btn = QPushButton()
                    btn.setStyleSheet(
                        "QPushButton { margin: 8px 40px; padding: 8px; "
                        "background: #2a2a2a; border: 1px solid #444; border-radius: 4px; "
                        "color: #aaa; font-size: 12px; }"
                        "QPushButton:hover { background: #333; color: #ddd; }"
                    )
                    btn.clicked.connect(self._load_more_cards)
                    self._load_more_btn = btn
                    self._content_vbox.addWidget(btn)
                self._load_more_btn.setText(
                    f"Charger {next_n} de plus  "
                    f"({remaining} restant{'s' if remaining > 1 else ''})"
                )
                if avatar_items:
                    QTimer.singleShot(
                        0, lambda items=list(avatar_items): self._start_cluster_loader(items)
                    )
                QTimer.singleShot(0, self._force_reflow)
            else:
                self._content_vbox.addStretch(1)
                if avatar_items:
                    QTimer.singleShot(
                        0, lambda items=list(avatar_items): self._start_cluster_loader(items)
                    )
                QTimer.singleShot(0, self._force_reflow)
            # Restaurer la position de scroll après retour de navigation
            if self._restore_scroll_on_build:
                self._restore_scroll_on_build = False
                _pos = self._saved_scroll_pos
                QTimer.singleShot(30, lambda: self._scroll.verticalScrollBar().setValue(_pos))

        def _next(start: int, page_rendered: int) -> None:
            if gen != self._build_generation:
                return
            remaining_in_page = _PAGE_SIZE - page_rendered
            if remaining_in_page <= 0:
                self._rendered_count = start
                _page_done(start)
                return
            batch = min(_BUILD_BATCH, remaining_in_page, len(combined) - start)
            end = start + batch
            for idx in range(start, end):
                kind, group = combined[idx]
                if kind == "solo":
                    _add_card(group[0], solo_section, is_solo=True)
                elif len(group) == 1:
                    _add_card(group[0], flat_section)
                else:
                    label, color  = group_labels.get(group[0], ("", ""))
                    group_by_size = sorted(group, key=lambda c: -face_counts.get(c, 0))
                    best_pid, best_score = None, 0.0
                    for cid in group:
                        pid, _, _, score = suggestions.get(cid, (None, "", "", 0.0))
                        if pid is not None and score > best_score:
                            best_pid, best_score = pid, score
                    section = _SectionWidget(label, color, suggested_person_id=best_pid, parent=self._content)
                    section.accept_requested.connect(self._on_section_accept)
                    section.assign_requested.connect(self._on_section_assign)
                    section.ignore_requested.connect(self._on_section_ignore)
                    for cluster_id in group_by_size:
                        _add_card(cluster_id, section, eject=(best_pid is not None))
                    section.reflow(self._current_cols)
                    self._content_vbox.addWidget(section)
                    self._sections.append(section)
            self._rendered_count = end
            new_page_rendered = page_rendered + batch
            if end >= len(combined) or new_page_rendered >= _PAGE_SIZE:
                _page_done(end)
            else:
                QTimer.singleShot(0, lambda: _next(end, new_page_rendered))

        QTimer.singleShot(0, lambda: _next(0, 0))

    # ------------------------------------------------------------------ sélection

    def _on_card_selection_toggled(self, cluster_id: int, selected: bool) -> None:
        if selected:
            self._selected_ids.add(cluster_id)
            self._anchor_id = cluster_id   # ancre pour Maj+clic
        else:
            self._selected_ids.discard(cluster_id)
        self._update_action_bar()

    def _update_action_bar(self) -> None:
        n = len(self._selected_ids)
        self._action_bar.setVisible(n > 0)
        if n == 0:
            return
        n_solos  = sum(1 for cid in self._selected_ids
                       if self._cards.get(cid) and self._cards[cid]._is_solo)
        n_groups = n - n_solos
        if n_solos > 0 and n_groups == 0:
            self._lbl_selected.setText(
                f"{n} visage{'s isolés' if n > 1 else ' isolé'} sélectionné{'s' if n > 1 else ''}"
            )
        elif n_solos == 0:
            plural = "s" if n > 1 else ""
            self._lbl_selected.setText(f"{n} groupe{plural} sélectionné{plural}")
        else:
            self._lbl_selected.setText(
                f"{n} élément{'s' if n > 1 else ''} sélectionné{'s' if n > 1 else ''}"
            )
        self._btn_action_view.setVisible(n == 1)

    def _clear_selection(self) -> None:
        for cid, card in self._cards.items():
            if cid in self._selected_ids:
                card.set_selected(False)
        self._selected_ids.clear()
        self._anchor_id = None
        self._action_bar.setVisible(False)

    # ------------------------------------------------------------------ slots cartes individuelles

    def _on_card_view_requested(self, cluster_id: int) -> None:
        self._saved_scroll_pos = self._scroll.verticalScrollBar().value()
        card = self._cards.get(cluster_id)
        if card and card._is_solo:
            self.photos_requested.emit(cluster_id, "Visage isolé")
        else:
            face_count = next(
                (fc for cid, fc in self._face_db.get_unnamed_clusters() if cid == cluster_id), 0
            )
            plural = "s" if face_count > 1 else ""
            group_label = "Isolé" if face_count == 1 else f"Groupe {cluster_id}"
            self.photos_requested.emit(cluster_id, f"{group_label} — {face_count} visage{plural}")

    def _on_card_ignore_requested(self, cluster_id: int) -> None:
        self._face_db.ignore_cluster(cluster_id)
        self.cluster_ignored.emit(cluster_id)

    def _on_card_ignore_selection_requested(self) -> None:
        """Ignore tous les groupes/visages isolés actuellement sélectionnés
        (menu contextuel « Ignorer ces groupes », visible seulement en
        multi-sélection — cf. _ClusterCard.mousePressEvent)."""
        cluster_ids = list(self._selected_ids)
        if not cluster_ids:
            return
        for cid in cluster_ids:
            self._face_db.ignore_cluster(cid)
        self._clear_selection()
        self.clusters_ignored.emit(cluster_ids)

    def _on_card_quick_accept(self, cluster_id: int, person_id: int) -> None:
        self.clusters_assigned.emit([cluster_id], person_id)

    def _on_card_eject_from_section(self, cluster_id: int) -> None:
        """Retire le cluster de sa section de suggestion et le place dans les groupes isolés."""
        self._face_db.clear_cluster_suggestion(cluster_id)

        # Supprimer l'ancienne carte
        old_card = self._cards.pop(cluster_id, None)
        if old_card is not None:
            old_card.deleteLater()
        self._avatar_cache.pop(cluster_id, None)
        self._selected_ids.discard(cluster_id)

        # Retirer de sa section — supprimer la section si elle devient vide
        sections_to_keep = []
        for section in self._sections:
            if section is self._flat_section or section is self._solo_section:
                sections_to_keep.append(section)
                continue
            if any(c == cluster_id for c, _ in section._entries):
                section._entries = [(c, w) for c, w in section._entries if c != cluster_id]
                if section._entries:
                    section.reflow(self._current_cols)
                    sections_to_keep.append(section)
                else:
                    idx = self._content_vbox.indexOf(section)
                    if idx >= 0:
                        self._content_vbox.takeAt(idx)
                    section.deleteLater()
            else:
                sections_to_keep.append(section)
        self._sections = sections_to_keep

        # Mettre à jour _cached_data
        if self._cached_data:
            self._cached_data["suggestions"].pop(cluster_id, None)
            old_group_labels = self._cached_data.get("group_labels", {})
            new_group_labels = dict(old_group_labels)
            new_groups = []
            for g in self._cached_data.get("groups_sorted", []):
                if cluster_id in g:
                    old_root = g[0]
                    new_g = [c for c in g if c != cluster_id]
                    if new_g:
                        new_groups.append(new_g)
                        # Le groupe restant garde son étiquette, réindexée sur son
                        # nouveau premier élément si le cluster éjecté était le root.
                        if new_g[0] != old_root:
                            new_group_labels[new_g[0]] = old_group_labels.get(old_root, ("", ""))
                    new_groups.append([cluster_id])
                else:
                    new_groups.append(g)
            self._cached_data["groups_sorted"] = new_groups
            new_group_labels[cluster_id] = ("", "")
            self._cached_data["group_labels"] = new_group_labels

        # Retirer de _all_combined (la carte sera ajoutée directement à flat_section)
        self._all_combined = [
            (k, [c for c in g if c != cluster_id])
            for k, g in self._all_combined
        ]
        self._all_combined = [(k, g) for k, g in self._all_combined if g]

        # Créer la nouvelle carte dans flat_section
        flat = self._flat_section
        data = self._pending_build_data
        if flat is not None and data is not None:
            fc = data["face_counts"].get(cluster_id, 0)
            rep = data["representative_faces"].get(cluster_id)
            new_card = _ClusterCard(
                cluster_id, fc, None, "", "",
                selected_ids_ref=self._selected_ids,
                parent=flat._card_area,
            )
            new_card.selection_toggled.connect(self._on_card_selection_toggled)
            new_card.range_select_requested.connect(self._on_range_select)
            new_card.view_requested.connect(self._on_card_view_requested)
            new_card.name_requested.connect(self._on_card_name_requested)
            new_card.quick_accept_requested.connect(self._on_card_quick_accept)
            new_card.merge_requested.connect(self._on_card_merge_requested)
            new_card.associate_requested.connect(self._on_card_associate_requested)
            new_card.ignore_requested.connect(self._on_card_ignore_requested)
            new_card.ignore_selection_requested.connect(self._on_card_ignore_selection_requested)
            flat.add_card(cluster_id, new_card)
            self._cards[cluster_id] = new_card
            if rep:
                QTimer.singleShot(
                    0, lambda r=rep, cid=cluster_id: self._start_cluster_loader([(cid, r)])
                )
            # S'assurer que flat_section est dans le layout
            if self._content_vbox.indexOf(flat) < 0:
                ref = None
                if self._solo_section and self._content_vbox.indexOf(self._solo_section) >= 0:
                    ref = self._solo_section
                elif self._load_more_btn:
                    ref = self._load_more_btn
                idx_ref = self._content_vbox.indexOf(ref) if ref else -1
                if idx_ref >= 0:
                    self._content_vbox.insertWidget(idx_ref, flat)
                else:
                    self._content_vbox.addWidget(flat)
                if flat not in self._sections:
                    self._sections.append(flat)
            flat.reflow(self._current_cols)

        self._update_action_bar()

    def _on_card_merge_requested(self, cluster_id: int) -> None:
        dlg = _MergePickerDialog(cluster_id, self._face_db, self)
        if dlg.exec() != QDialog.Accepted:
            return
        target_id = dlg.selected_cluster_id()
        if target_id is not None:
            self._face_db.merge_clusters(cluster_id, target_id)
            self.cluster_merged.emit(cluster_id, target_id)

    def _on_card_associate_requested(self) -> None:
        """Fusionne tous les groupes/visages isolés actuellement sélectionnés
        dans un même groupe (sans les assigner à une personne), pour pouvoir
        les identifier ensemble en une seule fois."""
        cluster_ids = list(self._selected_ids)
        if len(cluster_ids) < 2:
            return
        face_counts = (self._pending_build_data or {}).get("face_counts", {})
        target_id = max(cluster_ids, key=lambda cid: face_counts.get(cid, 0))
        for cid in cluster_ids:
            if cid != target_id:
                self._face_db.merge_clusters(cid, target_id)
        self._clear_selection()
        self.cluster_merged.emit(cluster_ids[0], target_id)

    def _on_card_name_requested(self, cluster_id: int) -> None:
        if cluster_id in self._selected_ids and len(self._selected_ids) > 1:
            self._start_assign_for_clusters(list(self._selected_ids))
            return
        card = self._cards.get(cluster_id)
        suggested_id = card._suggested_person_id if card else None
        t = _PersonsLoader(self._catalog, self._face_db, self)
        t.ready.connect(lambda persons, _: self._show_assign_dialog(cluster_id, suggested_id, persons))
        t.finished.connect(t.deleteLater)
        t.start()

    def _show_assign_dialog(self, cluster_id: int, suggested_id, persons: list) -> None:
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
        self._start_assign_for_clusters(cluster_ids)

    def _start_assign_for_clusters(self, cluster_ids: list) -> None:
        t = _PersonsLoader(
            self._catalog, self._face_db, self,
            cluster_ids=cluster_ids,
            persons_snap=list(self._persons),
            emb_snap=dict(self._person_cluster_embeddings),
        )
        t.ready.connect(
            lambda persons, suggested_id: self._show_multi_assign_dialog(
                cluster_ids, persons, suggested_id
            )
        )
        t.finished.connect(t.deleteLater)
        t.start()

    def _show_multi_assign_dialog(self, cluster_ids: list, persons: list, suggested_id) -> None:
        dlg = _AssignDialog(
            cluster_ids[0],
            persons,
            suggested_person_id=suggested_id,
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
        cluster_ids = list(self._selected_ids)
        for cid in cluster_ids:
            self._face_db.ignore_cluster(cid)
        self._anchor_id = None
        self.remove_clusters(cluster_ids)   # un seul reflow UI

    # ------------------------------------------------------------------ slots sections

    def _on_section_accept(self, cluster_ids: list, person_id: int) -> None:
        self.clusters_assigned.emit(cluster_ids, person_id)
        self._clear_selection()

    def _on_section_assign(self, cluster_ids: list) -> None:
        self._start_assign_for_clusters(cluster_ids)

    def _on_section_ignore(self, cluster_ids: list) -> None:
        for cid in cluster_ids:
            self._face_db.ignore_cluster(cid)
        self._anchor_id = None
        self.remove_clusters(cluster_ids)

    # ------------------------------------------------------------------ pagination

    def _load_more_cards(self) -> None:
        """Affiche la prochaine page de _PAGE_SIZE cartes depuis self._all_combined."""
        if not self._all_combined or self._pending_build_data is None:
            return
        start = self._rendered_count
        if start >= len(self._all_combined):
            return

        data                 = self._pending_build_data
        face_counts          = data["face_counts"]
        suggestions          = data["suggestions"]
        representative_faces = data["representative_faces"]
        group_labels         = data["group_labels"]
        gen                  = self._build_generation
        flat_section         = self._flat_section
        solo_section         = self._solo_section
        avatar_items: list   = []

        def _add_card(cluster_id: int, target: "_SectionWidget", is_solo: bool = False, eject: bool = False) -> None:
            if target is None:
                return
            fc = face_counts.get(cluster_id, 0)
            sugg_id, sugg_label, sugg_color, _ = suggestions.get(cluster_id, (None, "", "", 0.0))
            card = _ClusterCard(
                cluster_id, fc, sugg_id, sugg_label, sugg_color,
                is_solo=is_solo,
                show_eject=eject,
                selected_ids_ref=self._selected_ids,
                parent=target._card_area,
            )
            card.selection_toggled.connect(self._on_card_selection_toggled)
            card.range_select_requested.connect(self._on_range_select)
            card.view_requested.connect(self._on_card_view_requested)
            card.name_requested.connect(self._on_card_name_requested)
            card.quick_accept_requested.connect(self._on_card_quick_accept)
            card.merge_requested.connect(self._on_card_merge_requested)
            card.associate_requested.connect(self._on_card_associate_requested)
            card.ignore_requested.connect(self._on_card_ignore_requested)
            card.ignore_selection_requested.connect(self._on_card_ignore_selection_requested)
            card.eject_from_section_requested.connect(self._on_card_eject_from_section)
            target.add_card(cluster_id, card)
            self._cards[cluster_id] = card
            if cluster_id in self._avatar_cache:
                card.set_avatar(self._avatar_cache[cluster_id])
            else:
                rep = representative_faces.get(cluster_id)
                if rep:
                    avatar_items.append((cluster_id, rep))

        def _page_done_more(rendered_total: int) -> None:
            if gen != self._build_generation:
                return
            if flat_section and flat_section._entries:
                flat_section.reflow(self._current_cols)
            if solo_section and solo_section._entries:
                solo_section.reflow(self._current_cols)
            remaining = len(self._all_combined) - rendered_total
            if remaining > 0:
                next_n = min(_PAGE_SIZE, remaining)
                if self._load_more_btn:
                    self._load_more_btn.setText(
                        f"Charger {next_n} de plus  "
                        f"({remaining} restant{'s' if remaining > 1 else ''})"
                    )
            else:
                if self._load_more_btn is not None:
                    self._content_vbox.removeWidget(self._load_more_btn)
                    self._load_more_btn.deleteLater()
                    self._load_more_btn = None
                self._content_vbox.addStretch(1)
                QTimer.singleShot(0, self._force_reflow)
            if avatar_items:
                QTimer.singleShot(
                    0, lambda items=list(avatar_items): self._start_cluster_loader(items)
                )

        def _next_more(pos: int, page_rendered: int) -> None:
            if gen != self._build_generation:
                return
            remaining_in_page = _PAGE_SIZE - page_rendered
            if remaining_in_page <= 0:
                self._rendered_count = pos
                _page_done_more(pos)
                return
            batch = min(_BUILD_BATCH, remaining_in_page, len(self._all_combined) - pos)
            end = pos + batch
            for idx in range(pos, end):
                kind, group = self._all_combined[idx]
                if kind == "solo":
                    _add_card(group[0], solo_section, is_solo=True)
                elif len(group) == 1:
                    _add_card(group[0], flat_section)
                else:
                    label, color  = group_labels.get(group[0], ("", ""))
                    group_by_size = sorted(group, key=lambda c: -face_counts.get(c, 0))
                    best_pid, best_score = None, 0.0
                    for cid in group:
                        pid, _, _, score = suggestions.get(cid, (None, "", "", 0.0))
                        if pid is not None and score > best_score:
                            best_pid, best_score = pid, score
                    section = _SectionWidget(label, color, suggested_person_id=best_pid, parent=self._content)
                    section.accept_requested.connect(self._on_section_accept)
                    section.assign_requested.connect(self._on_section_assign)
                    section.ignore_requested.connect(self._on_section_ignore)
                    for cluster_id in group_by_size:
                        _add_card(cluster_id, section, eject=(best_pid is not None))
                    section.reflow(self._current_cols)
                    # Insérer avant flat/solo pour respecter l'ordre visuel
                    ref = None
                    if flat_section and self._content_vbox.indexOf(flat_section) >= 0:
                        ref = flat_section
                    elif solo_section and self._content_vbox.indexOf(solo_section) >= 0:
                        ref = solo_section
                    elif self._load_more_btn:
                        ref = self._load_more_btn
                    idx_ref = self._content_vbox.indexOf(ref) if ref else -1
                    if idx_ref >= 0:
                        self._content_vbox.insertWidget(idx_ref, section)
                    else:
                        self._content_vbox.addWidget(section)
                    self._sections.append(section)
            self._rendered_count = end
            new_page_rendered = page_rendered + batch
            if end >= len(self._all_combined) or new_page_rendered >= _PAGE_SIZE:
                _page_done_more(end)
            else:
                QTimer.singleShot(0, lambda: _next_more(end, new_page_rendered))

        QTimer.singleShot(0, lambda: _next_more(start, 0))

    # ------------------------------------------------------------------ sélection étendue (Maj+clic)

    def _get_ordered_card_ids(self) -> list[int]:
        """Retourne les cluster_ids dans l'ordre visuel (section par section, entrée par entrée)."""
        result: list[int] = []
        for section in self._sections:
            for cid, _ in section._entries:
                result.append(cid)
        return result

    def _on_range_select(self, cluster_id: int) -> None:
        """Sélectionne toutes les cartes entre l'ancre et cluster_id (inclus)."""
        ordered = self._get_ordered_card_ids()
        if not ordered:
            return

        anchor = self._anchor_id
        if anchor is None or anchor not in self._cards:
            # Pas d'ancre : comportement de clic normal
            card = self._cards.get(cluster_id)
            if card:
                card._is_selected = not card._is_selected
                card.set_selected(card._is_selected)
                self._on_card_selection_toggled(cluster_id, card._is_selected)
            return

        try:
            i_anchor  = ordered.index(anchor)
            i_clicked = ordered.index(cluster_id)
        except ValueError:
            return

        lo, hi = min(i_anchor, i_clicked), max(i_anchor, i_clicked)
        for cid in ordered[lo : hi + 1]:
            card = self._cards.get(cid)
            if card and not card._is_selected:
                card._is_selected = True
                card.set_selected(True)
                self._selected_ids.add(cid)

        self._update_action_bar()

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
