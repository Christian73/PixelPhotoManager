# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Calculs et threads d'arrière-plan de la grille de groupes de visages
(extraits de face_cluster_grid.py) : requêtes groupes/suggestions et
chargeurs QThread."""
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
from src.faces.face_database import FaceDatabase, _SIM_SUGGEST
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import (
    _AssignDialog, _AvatarLoader, _cosine_sim, _SIM_WEAK, _SIM_STRONG,
)

logger = logging.getLogger(__name__)


class _AnalysisCancelled(Exception):
    """Levée depuis uf_progress() pour interrompre le Union-Find en cours de bloc."""


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


def _compute_cluster_groups_bg(
    cluster_ids: list[int],
    embeddings: dict[int, list[float]],
    progress_cb=None,
) -> dict[int, list[int]]:
    """Union-Find par blocs : regroupe les clusters dont sim(centroïde) ≥ _SIM_GROUP.

    Calcul en blocs de _UF_CHUNK lignes : à chaque itération on multiplie un bloc
    de lignes par toutes les lignes suivantes (triangle supérieur) via BLAS.
    RAM pic ≈ _UF_CHUNK × n × 4 octets au lieu de n² × 4 — scalable jusqu'à ~80k.
    progress_cb(chunk_start) est appelé au début de chaque bloc ; s'il lève
    _AnalysisCancelled (annulation utilisateur, cf. FaceClusterGrid), la boucle
    s'arrête à ce point — les fusions déjà trouvées dans les blocs précédents
    sont conservées et renvoyées telles quelles (résultat partiel mais valide :
    aucune fusion n'est jamais défaite, seules celles pas encore découvertes
    manquent)."""
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
                    # Bloc courant vs toutes les lignes suivantes (triangle supérieur)
                    chunk = mat[chunk_start:chunk_end]     # (_UF_CHUNK, dim)
                    rest  = mat[chunk_start + 1:]          # (m - chunk_start - 1, dim)
                    sims  = chunk @ rest.T                 # (_UF_CHUNK, m - chunk_start - 1)
                    rows, cols = np.nonzero(sims >= _SIM_GROUP)
                    for r, c in zip(rows.tolist(), cols.tolist()):
                        i_abs = chunk_start + int(r)
                        j_abs = chunk_start + 1 + int(c)
                        if j_abs > i_abs:                  # triangle supérieur uniquement
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
        pass   # arrêt anticipé : on garde les fusions déjà trouvées

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
) -> "dict[int, tuple[int | None, str, str, float]]":
    """Calcule les suggestions pour tous les clusters en un seul produit matriciel.

    Retourne {cluster_id: (person_id | None, label, color, score)}.
    Construit (n_clusters, dim) × (n_person_emb, dim)^T → matrice de similarité
    complète, puis sélectionne le maximum par ligne. Remplace la boucle Python
    de N appels _compute_suggestion_bg."""
    result: dict = {cid: (None, "", "", 0.0) for cid in cluster_ids}

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
        self._cancelled = False

    def cancel(self) -> None:
        """Demande l'arrêt (bouton Annuler de la popup). Drapeau simple, pas
        QThread.requestInterruption() : isInterruptionRequested() ne renvoie
        True que si le thread a été démarré via start() (d->running côté Qt),
        ce qui casserait les tests qui appellent run() en synchrone (cf.
        CLAUDE.md, piège coverage/QThread)."""
        self._cancelled = True

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

            if self._cancelled:
                return

            # ── Phase 2 : embeddings (groupes non-isolés seulement pour UF) ─
            # L'Union-Find est lancé uniquement sur les clusters à face_count > 1
            # (les visages isolés restent des singletons). Cela réduit la taille
            # de la matrice de ~68k à ~32k — temps divisé par ~4.
            non_solo_ids = [cid for cid in cluster_ids if face_counts.get(cid, 0) > 1]
            n_ns = len(non_solo_ids)
            s_ns = "s" if n_ns > 1 else ""

            step += 1
            self.progress.emit(step, N,
                f"Calcul des représentations vectorielles ({n_ns} groupe{s_ns} non-isolé{s_ns})…")
            cluster_embeddings = self._face_db.get_all_cluster_centroids(cluster_ids)

            # Affiner N maintenant qu'on connaît les embeddings disponibles
            non_solo_embeddings = {cid: cluster_embeddings[cid]
                                   for cid in non_solo_ids if cid in cluster_embeddings}
            m_emb      = len(non_solo_embeddings)
            # UF en blocs : nombre de blocs ≤ 100 pour la barre de progression
            n_uf_steps = min(max(m_emb // _UF_CHUNK, 1), 100) if m_emb else 0
            N          = step + 3 + n_uf_steps + 1

            if self._cancelled:
                return

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

            # ── Phase 2 : Union-Find par blocs, progression au % près ─────
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
                        f"Regroupement des visages similaires… {pct} %"
                        + (f"  ({m_emb} groupes, blocs de {_UF_CHUNK})" if pct == 0 else ""),
                    )

            if n_ns > UNION_FIND_MAX:
                # Trop grand même en mode blocs : skip UF
                self.progress.emit(step + 1, step + 1,
                    f"{n_ns} groupes — regroupement désactivé (limite : {UNION_FIND_MAX})")
                raw_groups: dict[int, list[int]] = {cid: [cid] for cid in cluster_ids}
            else:
                # _compute_cluster_groups_bg absorbe elle-même _AnalysisCancelled
                # et renvoie les fusions déjà trouvées : pas de try/except ici,
                # on continue toujours avec un résultat (complet ou partiel).
                raw_groups = _compute_cluster_groups_bg(
                    non_solo_ids, non_solo_embeddings, uf_progress
                )
                # Ajouter les visages isolés comme singletons
                for cid in cluster_ids:
                    if face_counts.get(cid, 0) == 1:
                        raw_groups[cid] = [cid]

            groups_sorted = sorted(
                raw_groups.values(),
                key=lambda g: (-len(g), -sum(face_counts.get(c, 0) for c in g)),
            )

            # Pas de `if self._cancelled: return` ici : au-delà de ce point, la
            # suite (étiquettes + suggestions) est rapide (vectorisée, pas de
            # boucle O(n²)) — autant la finir et livrer un résultat (complet ou
            # partiel si l'annulation a interrompu l'Union-Find ci-dessus)
            # plutôt que de tout jeter silencieusement.

            # ── Phase 2 : étiquettes des groupes multi-clusters (vectorisé) ─
            step += 1
            n_multi = sum(1 for g in groups_sorted if len(g) > 1)
            self.progress.emit(step, N, f"Calcul des étiquettes de groupes ({n_multi} groupe{('s' if n_multi > 1 else '')})…")
            N += 1   # on ajoute une étape au total pour cette phase

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

            # Auto-promotion : les clusters dont le score atteint _SIM_SUGGEST (seuil
            # "en attente de vérification", pas le seuil d'affichage _SIM_STRONG qui
            # gouverne uniquement la couleur du libellé) passent en attente de
            # vérification (ou sont alloués directement au-delà de _SIM_AUTO_ASSIGN,
            # cf. set_cluster_suggestions) et disparaissent de la liste à identifier.
            strong = {
                cid: (pid, score)
                for cid, (pid, _label, _color, score) in suggestions.items()
                if pid is not None and score >= _SIM_SUGGEST
            }
            if strong:
                self._face_db.set_cluster_suggestions(strong)
                # Filtrer les clusters promus des structures de données
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
                # Réindexer les étiquettes sur le nouveau premier élément de chaque
                # groupe : l'ancien root (clé de group_labels) a pu être promu et
                # retiré du groupe, sans quoi le groupe restant perd son en-tête.
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
    """Charge get_persons + enrich_persons hors du thread UI avant d'ouvrir un dialogue.

    Pour la sélection multi-groupe, calcule aussi la suggestion de personne en
    comparant les centroïdes des clusters aux centroïdes connus des personnes.
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
                # Une seule requête batch pour tous les clusters sélectionnés,
                # au lieu de N appels get_representative_embedding (N connexions SQLite).
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


