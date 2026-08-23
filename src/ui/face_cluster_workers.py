# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Background computations and threads of the face group grid
(extracted from face_cluster_grid.py): group/suggestion queries and QThread
loaders."""
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

from src.core.i18n import translate
from src.core.models import PersonInfo
from src.faces.face_database import FaceDatabase, _SIM_SUGGEST
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _cosine_sim, _SIM_WEAK, _SIM_STRONG,
)

logger = logging.getLogger(__name__)


class _AnalysisCancelled(Exception):
    """Raised from uf_progress() to interrupt the Union-Find in mid-block."""


_CARD_IMG     = 130
_CARD_W       = 148
_CARD_SPACING  = 10
_COLS_MIN      = 2
_SIM_GROUP     = 0.72   # threshold to group two "probably the same person" clusters
_BUILD_BATCH   = 10     # cards created per event loop tick (avoids blocking the UI)
_PAGE_SIZE     = 200    # number of cards rendered per page (pagination)
_UF_CHUNK      = 500    # rows per block in the matrix product of the Union-Find
                        # peak RAM ≈ _UF_CHUNK × n × 4 bytes  (500 × 50k × 4 = 100 MB)
UNION_FIND_MAX = 80_000 # skip the UF beyond that (> 2 min even in block mode)


def _compute_cluster_groups_bg(
    cluster_ids: list[int],
    embeddings: dict[int, list[float]],
    progress_cb=None,
) -> dict[int, list[int]]:
    """Union-Find by blocks: groups the clusters whose sim(centroid) ≥ _SIM_GROUP.

    Computed in blocks of _UF_CHUNK rows: at each iteration a block of rows is
    multiplied by every following row (upper triangle) through BLAS.
    Peak RAM ≈ _UF_CHUNK × n × 4 bytes instead of n² × 4 — scalable up to ~80k.
    progress_cb(chunk_start) is called at the beginning of each block; if it
    raises _AnalysisCancelled (a user cancellation, cf. FaceClusterGrid), the
    loop stops at that point — the merges already found in the previous blocks
    are kept and returned as they are (a partial but valid result: no merge is
    ever undone, only those not yet discovered are missing)."""
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
        try:
            import numpy as np
            if valid:
                ids_arr = [cid for cid, _ in valid]
                m = len(ids_arr)
                mat = np.array([e for _, e in valid], dtype=np.float32)  # (m, dim)
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                mat /= np.where(norms > 1e-8, norms, 1.0)

                for chunk_start in range(0, m, _UF_CHUNK):
                    if progress_cb is not None:
                        progress_cb(chunk_start)
                    if chunk_start + 1 >= m:
                        break
                    chunk_end = min(chunk_start + _UF_CHUNK, m)
                    # Current block vs every following row (upper triangle)
                    chunk = mat[chunk_start:chunk_end]     # (_UF_CHUNK, dim)
                    rest  = mat[chunk_start + 1:]          # (m - chunk_start - 1, dim)
                    sims  = chunk @ rest.T                 # (_UF_CHUNK, m - chunk_start - 1)
                    rows, cols = np.nonzero(sims >= _SIM_GROUP)
                    for r, c in zip(rows.tolist(), cols.tolist()):
                        i_abs = chunk_start + int(r)
                        j_abs = chunk_start + 1 + int(c)
                        if j_abs > i_abs:                  # upper triangle only
                            union(ids_arr[i_abs], ids_arr[j_abs])
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
    except _AnalysisCancelled:
        pass   # early stop: the merges already found are kept

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
    """Computes the best person suggestion for a cluster (scalar fallback)."""
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
) -> "dict[int, tuple[int | None, str, str, float]]":
    """Computes the suggestions for every cluster in a single matrix product.

    Returns {cluster_id: (person_id | None, label, color, score)}.
    Builds (n_clusters, dim) × (n_person_emb, dim)^T → the complete similarity
    matrix, then selects the maximum per row. Replaces the Python loop of N
    _compute_suggestion_bg calls."""
    result: dict = {cid: (None, "", "", 0.0) for cid in cluster_ids}

    if not persons or not person_cluster_embeddings:
        return result

    # Flat list of (person, embedding) for every known person
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
                result[cid] = (best_p.id, f"≈ {best_p.name} ({pct} %)", "#7aabdb", s)
            else:
                result[cid] = (best_p.id, f"~ {best_p.name} ({pct} %)", "#888", s)
    except ImportError:
        for cid in cluster_ids:
            pid, label, color = _compute_suggestion_bg(
                cid, cluster_embeddings, persons, person_cluster_embeddings
            )
            # Reconstruct score from label if possible
            s = 0.0
            if pid is not None and label:
                try:
                    s = int(label.split("(")[1].split("%")[0]) / 100.0
                except (IndexError, ValueError):
                    pass
            result[cid] = (pid, label, color, s)

    return result


# ------------------------------------------------------------------ card


class _ClusterRefreshThread(QThread):
    """
    Loading in two phases for a progressive display.

    Phase 1 — initial_ready (fast, < 1 s):
        2 SQL queries (face counts + representative faces).
        Emits a flat structure (1 group = 1 cluster) without suggestions.
        The cards can be displayed immediately.

    Phase 2 — data_ready (slow, O(n²)):
        Embeddings, Union-Find, suggestions.
        Emits the complete grouped structure with suggestions.
    """

    initial_ready = Signal(object)         # dict — displayed immediately
    data_ready    = Signal(object)         # dict | None — displayed after a heavy computation
    progress      = Signal(int, int, str)  # current step, total, message

    def __init__(self, face_db: "FaceDatabase", catalog, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._cancelled = False

    def cancel(self) -> None:
        """Requests the stop (the Cancel button of the popup). A plain flag, not
        QThread.requestInterruption(): isInterruptionRequested() only returns
        True if the thread was started through start() (d->running on the Qt
        side), which would break the tests that call run() synchronously (cf.
        CLAUDE.md, the coverage/QThread trap)."""
        self._cancelled = True

    def run(self) -> None:
        try:
            # ── Initial retrieval (fast) — N still unknown ─────────────────
            self.progress.emit(0, 0, translate(
                "FaceClusterWorkers", "Fetching the face groups…"))
            clusters = self._face_db.get_unnamed_clusters()

            _empty = {
                "face_counts": {}, "groups_sorted": [], "group_labels": {},
                "suggestions": {}, "representative_faces": {}, "persons": [],
                "person_cluster_embeddings": {}, "is_partial": False,
            }
            if not clusters:
                self.progress.emit(1, 1, translate(
                    "FaceClusterWorkers", "No group to analyse"))
                self.initial_ready.emit(_empty)
                self.data_ready.emit(_empty)
                return

            cluster_ids = [cid for cid, _ in clusters]
            face_counts = {cid: fc for cid, fc in clusters}
            n  = len(cluster_ids)

            # Total number of steps = 5 fixed + ≤100 Union-Find updates + 1 suggestions.
            # The suggestions are vectorised (1 matrix operation), hence a single step.
            n_uf_steps = min(n, 100)
            N          = 5 + n_uf_steps + 1
            step       = 0

            # ── Phase 1: flat structure, without suggestions ───────────────
            step += 1
            self.progress.emit(step, N, translate(
                "FaceClusterWorkers",
                "Loading representative faces (%n group(s))…", None, n))
            representative_faces = self._face_db.get_all_representative_faces(cluster_ids)
            flat_groups = [[cid] for cid in cluster_ids]   # already sorted DESC by face_count

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

            if self._cancelled:
                return

            # ── Phase 2: embeddings (non-isolated groups only, for the UF) ──
            # The Union-Find only runs on the clusters with face_count > 1 (the
            # isolated faces stay singletons). That reduces the size of the matrix
            # from ~68k to ~32k — the time divided by ~4.
            non_solo_ids = [cid for cid in cluster_ids if face_counts.get(cid, 0) > 1]
            n_ns = len(non_solo_ids)

            step += 1
            self.progress.emit(step, N, translate(
                "FaceClusterWorkers",
                "Computing vector representations (%n non-isolated group(s))…",
                None, n_ns))
            cluster_embeddings = self._face_db.get_all_cluster_centroids(cluster_ids)

            # Refine N now that the available embeddings are known
            non_solo_embeddings = {cid: cluster_embeddings[cid]
                                   for cid in non_solo_ids if cid in cluster_embeddings}
            m_emb      = len(non_solo_embeddings)
            # UF in blocks: number of blocks ≤ 100 for the progress bar
            n_uf_steps = min(max(m_emb // _UF_CHUNK, 1), 100) if m_emb else 0
            N          = step + 3 + n_uf_steps + 1

            if self._cancelled:
                return

            # ── Phase 2: known people ─────────────────────────────────────
            step += 1
            self.progress.emit(step, N, translate(
                "FaceClusterWorkers", "Fetching the known people…"))
            persons    = self._catalog.get_persons()
            np_        = len(persons)

            step += 1
            self.progress.emit(step, N, translate(
                "FaceClusterWorkers", "Analysing the known people (%n person(s))…",
                None, np_))
            self._face_db.enrich_persons(persons)
            person_ids = [p.id for p in persons]

            step += 1
            self.progress.emit(step, N, translate(
                "FaceClusterWorkers", "Vector representations of the people…"))
            person_cluster_embeddings = self._face_db.get_all_person_cluster_centroids(person_ids)

            # ── Phase 2: Union-Find by blocks, progress to the % ──────────
            _last_uf_pct = -1
            _n_blocks    = max(m_emb // _UF_CHUNK, 1) if m_emb else 1

            def uf_progress(chunk_start: int) -> None:
                nonlocal step, _last_uf_pct
                if self._cancelled:
                    raise _AnalysisCancelled()
                pct = chunk_start * 100 // m_emb if m_emb else 100
                if pct != _last_uf_pct:
                    _last_uf_pct = pct
                    step += 1
                    self.progress.emit(
                        step, N,
                        translate("FaceClusterWorkers",
                                  "Grouping similar faces… {pct} %"
                                  ).format(pct=pct)
                        + (translate("FaceClusterWorkers",
                                     "  ({groups} groups, blocks of {chunk})"
                                     ).format(groups=m_emb, chunk=_UF_CHUNK)
                           if pct == 0 else ""),
                    )

            if n_ns > UNION_FIND_MAX:
                # Too large even in block mode: skip the UF
                self.progress.emit(step + 1, step + 1, translate(
                    "FaceClusterWorkers",
                    "{groups} groups — grouping disabled (limit: {limit})"
                    ).format(groups=n_ns, limit=UNION_FIND_MAX))
                raw_groups: dict[int, list[int]] = {cid: [cid] for cid in cluster_ids}
            else:
                # _compute_cluster_groups_bg absorbs _AnalysisCancelled itself and
                # returns the merges already found: no try/except here, we always
                # continue with a result (complete or partial).
                raw_groups = _compute_cluster_groups_bg(
                    non_solo_ids, non_solo_embeddings, uf_progress
                )
                # Add the isolated faces as singletons
                for cid in cluster_ids:
                    if face_counts.get(cid, 0) == 1:
                        raw_groups[cid] = [cid]

            groups_sorted = sorted(
                raw_groups.values(),
                key=lambda g: (-len(g), -sum(face_counts.get(c, 0) for c in g)),
            )

            # No `if self._cancelled: return` here: beyond this point, what follows
            # (labels + suggestions) is fast (vectorised, no O(n²) loop) — better to
            # finish it and deliver a result (complete, or partial if the cancellation
            # interrupted the Union-Find above) than to throw everything away
            # silently.

            # ── Phase 2: labels of the multi-cluster groups (vectorised) ────
            step += 1
            n_multi = sum(1 for g in groups_sorted if len(g) > 1)
            self.progress.emit(step, N, translate(
                "FaceClusterWorkers", "Computing the group labels (%n group(s))…",
                None, n_multi))
            N += 1   # one step is added to the total for this phase

            try:
                import numpy as _np

                def _avg_sim_np(group: list[int]) -> float:
                    valid = [c for c in group if c in cluster_embeddings]
                    if len(valid) < 2:
                        return 0.0
                    mat = _np.array([cluster_embeddings[c] for c in valid], dtype=_np.float32)
                    norms = _np.linalg.norm(mat, axis=1, keepdims=True)
                    mat /= _np.where(norms > 1e-8, norms, 1.0)
                    sim_mat = mat @ mat.T
                    ng = len(valid)
                    ti, tj = _np.triu_indices(ng, k=1)
                    return float(sim_mat[ti, tj].mean()) if len(ti) > 0 else 0.0
            except ImportError:
                def _avg_sim_np(group: list[int]) -> float:  # type: ignore[misc]
                    sims = [
                        _cosine_sim(cluster_embeddings[ci], cluster_embeddings[cj])
                        for i, ci in enumerate(group)
                        for cj in group[i + 1:]
                        if ci in cluster_embeddings and cj in cluster_embeddings
                    ]
                    return sum(sims) / len(sims) if sims else 0.0

            group_labels: dict[int, tuple[str, str]] = {}
            for group in groups_sorted:
                root = group[0]
                if len(group) > 1:
                    avg_sim = _avg_sim_np(sorted(group, key=lambda c: -face_counts.get(c, 0)))
                    pct     = int(avg_sim * 100)
                    n_faces  = sum(face_counts.get(c, 0) for c in group)
                    n_groups = len(group)
                    label   = (
                        translate("FaceClusterWorkers",
                                  "≈ Probably the same person")
                        + "  —  "
                        + translate("FaceClusterWorkers", "%n group(s)",
                                    None, n_groups)
                        + ", "
                        + translate("FaceClusterWorkers", "%n face(s)",
                                    None, n_faces)
                        + "  "
                        + translate("FaceClusterWorkers", "(sim. {pct} %)"
                                    ).format(pct=pct)
                    )
                    color = "#7aabdb" if avg_sim >= _SIM_STRONG else "#aaa"
                    group_labels[root] = (label, color)
                else:
                    group_labels[root] = ("", "")

            # ── Phase 2: vectorised suggestions (1 matrix product) ─────────
            step += 1
            self.progress.emit(
                step, N,
                translate("FaceClusterWorkers", "Computing the identification suggestions…")
                + (("  —  " + translate("FaceClusterWorkers",
                                        "%n known person(s)", None, np_))
                   if np_ else ""),
            )
            suggestions = _compute_all_suggestions_bg(
                cluster_ids, cluster_embeddings, persons, person_cluster_embeddings
            )

            # Auto-promotion: the clusters whose score reaches _SIM_SUGGEST (the
            # "awaiting verification" threshold, not the display threshold _SIM_STRONG
            # which only governs the colour of the label) move to awaiting
            # verification (or are assigned directly beyond _SIM_AUTO_ASSIGN, cf.
            # set_cluster_suggestions) and disappear from the list to identify.
            strong = {
                cid: (pid, score)
                for cid, (pid, _label, _color, score) in suggestions.items()
                if pid is not None and score >= _SIM_SUGGEST
            }
            if strong:
                self._face_db.set_cluster_suggestions(strong)
                # Filter the promoted clusters out of the data structures
                promoted_set = set(strong)
                face_counts  = {c: v for c, v in face_counts.items()  if c not in promoted_set}
                suggestions  = {c: v for c, v in suggestions.items()  if c not in promoted_set}
                representative_faces = {
                    c: v for c, v in representative_faces.items() if c not in promoted_set
                }
                _filtered_groups = [
                    ([c for c in g if c not in promoted_set], g[0])
                    for g in groups_sorted
                ]
                groups_sorted = [ng for ng, _old_root in _filtered_groups if ng]
                # Reindex the labels on the new first element of each group: the old
                # root (the key of group_labels) may have been promoted and removed
                # from the group, without which the remaining group loses its header.
                group_labels  = {
                    ng[0]: group_labels.get(old_root, ("", ""))
                    for ng, old_root in _filtered_groups
                    if ng
                }

            self.data_ready.emit({
                "face_counts":               face_counts,
                "groups_sorted":             groups_sorted,
                "group_labels":              group_labels,
                "suggestions":               suggestions,
                "representative_faces":      representative_faces,
                "persons":                   persons,
                "person_cluster_embeddings": person_cluster_embeddings,
                "is_partial":                False,
                "n_promoted":                len(strong),
                "was_cancelled":             self._cancelled,
            })
        except Exception:
            logger.exception("_ClusterRefreshThread: erreur inattendue")
            self.data_ready.emit(None)



class _PersonsLoader(QThread):
    """Loads get_persons + enrich_persons off the UI thread before opening a dialog.

    For a multi-group selection, also computes the person suggestion by
    comparing the centroids of the clusters with the known centroids of the
    people.
    """

    ready = Signal(list, object)   # (persons: list[PersonInfo], suggested_person_id | None)

    def __init__(
        self, catalog, face_db, parent=None,
        cluster_ids: list | None = None,
        persons_snap: list | None = None,
        emb_snap: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self._catalog      = catalog
        self._face_db      = face_db
        self._cluster_ids  = cluster_ids or []
        self._persons_snap = persons_snap or []
        self._emb_snap     = emb_snap or {}

    def run(self) -> None:
        try:
            persons = self._catalog.get_persons()
            self._face_db.enrich_persons(persons)
            suggested_id = None
            if self._cluster_ids:
                # A single batch query for every selected cluster, instead of N
                # get_representative_embedding calls (N SQLite connections).
                cluster_centroids = self._face_db.get_all_cluster_centroids(self._cluster_ids)
                best_sim = 0.0
                for cid in self._cluster_ids:
                    c_emb = cluster_centroids.get(cid)
                    if not c_emb:
                        continue
                    for p in self._persons_snap:
                        for p_emb in self._emb_snap.get(p.id, {}).values():
                            sim = _cosine_sim(c_emb, p_emb)
                            if sim > best_sim:
                                best_sim, suggested_id = sim, p.id
                if best_sim < _SIM_WEAK:
                    suggested_id = None
            self.ready.emit(persons, suggested_id)
        except Exception:
            logger.exception("_PersonsLoader: erreur inattendue")
            self.ready.emit([], None)


# ------------------------------------------------------------------ progress popup


