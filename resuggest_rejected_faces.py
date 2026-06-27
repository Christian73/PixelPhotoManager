#!/usr/bin/env python3
"""
Recalcule les suggestions pour les faces isolées (pinned=1) dont la suggestion
a été rejetée ou n'a jamais abouti (suggestion_person_id IS NULL).

Ces faces sont invisibles dans l'application car elles ne peuvent plus apparaître
dans les clusters non-nommés (filtre pinned=0) ni dans les suggestions.

Ce script recalcule la meilleure personne pour chacun de ces clusters via
un produit matriciel numpy en une seule passe.
"""
import os
import sys
import struct
import sqlite3

sys.path.insert(0, os.path.dirname(__file__))

from src.core.app_dirs import APP_DATA_DIR

DB_PATH = str(APP_DATA_DIR / "faces.db")
SIM_SUGGEST = 0.50


def _dec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # 1. Faces isolées sans suggestion (rejetées ou sans correspondance)
    print("Lecture des faces isolées sans suggestion…")
    rows = conn.execute(
        "SELECT cluster_id, embedding FROM faces"
        " WHERE pinned=1"
        "   AND person_id IS NULL"
        "   AND suggestion_person_id IS NULL"
        "   AND ignored=0"
        "   AND embedding IS NOT NULL"
    ).fetchall()

    if not rows:
        print("Aucune face à re-suggérer.")
        conn.close()
        return

    # Regrouper les embeddings par cluster
    cid_to_embs: dict[int, list] = {}
    for cid, blob in rows:
        cid_to_embs.setdefault(cid, []).append(_dec(blob))

    print(f"{sum(len(v) for v in cid_to_embs.values())} face(s) dans {len(cid_to_embs)} cluster(s).")

    # 2. Charger tous les embeddings des personnes connues
    print("Chargement des embeddings des personnes connues…")
    pers_rows = conn.execute(
        "SELECT person_id, embedding FROM faces"
        " WHERE person_id IS NOT NULL AND embedding IS NOT NULL"
    ).fetchall()

    by_person: dict[int, list] = {}
    for pid, blob in pers_rows:
        by_person.setdefault(pid, []).append(_dec(blob))

    person_ids = list(by_person.keys())
    print(f"{len(person_ids)} personne(s) connue(s).")

    if not person_ids:
        print("Aucune personne connue — impossible de calculer des suggestions.")
        conn.close()
        return

    # 3. Calcul des centroids et produit matriciel numpy
    try:
        import numpy as np

        def centroid(embs):
            return np.mean(np.array(embs, dtype=np.float32), axis=0)

        p_mat = np.array(
            [centroid(by_person[pid]) for pid in person_ids], dtype=np.float32
        )
        norms = np.linalg.norm(p_mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        p_mat /= norms

        cluster_ids = list(cid_to_embs.keys())
        c_mat = np.array(
            [centroid(cid_to_embs[cid]) for cid in cluster_ids], dtype=np.float32
        )
        cn = np.linalg.norm(c_mat, axis=1, keepdims=True)
        cn[cn == 0] = 1.0
        c_mat /= cn

        sim = c_mat @ p_mat.T                   # (n_clusters, n_persons)
        best_idx = np.argmax(sim, axis=1)
        best_sim = sim[np.arange(len(cluster_ids)), best_idx]

        suggestions: dict[int, tuple[int, float]] = {}
        for k, cid in enumerate(cluster_ids):
            s = float(best_sim[k])
            if s >= SIM_SUGGEST:
                suggestions[cid] = (person_ids[int(best_idx[k])], s)

    except ImportError:
        print("numpy non disponible — calcul pur Python (plus lent)…")

        def dot(a, b):
            return sum(x * y for x, y in zip(a, b))

        def norm(a):
            return sum(x * x for x in a) ** 0.5

        def cosine(a, b):
            na, nb = norm(a), norm(b)
            return dot(a, b) / (na * nb) if na > 0 and nb > 0 else 0.0

        def centroid(embs):
            n, dim = len(embs), len(embs[0])
            return [sum(e[d] for e in embs) / n for d in range(dim)]

        person_centroids = {pid: centroid(by_person[pid]) for pid in person_ids}

        suggestions = {}
        for cid, face_embs in cid_to_embs.items():
            c = centroid(face_embs)
            best_s, best_pid = 0.0, None
            for pid, pc in person_centroids.items():
                s = cosine(c, pc)
                if s > best_s:
                    best_s, best_pid = s, pid
            if best_pid is not None and best_s >= SIM_SUGGEST:
                suggestions[cid] = (best_pid, best_s)

    print(f"{len(suggestions)} suggestion(s) calculée(s).")

    # 4. Écrire les suggestions
    if suggestions:
        conn.execute("BEGIN")
        for cid, (pid, score) in suggestions.items():
            conn.execute(
                "UPDATE faces SET suggestion_person_id=?, suggestion_score=?"
                " WHERE cluster_id=? AND suggestion_person_id IS NULL",
                (pid, score, cid),
            )
        conn.execute("COMMIT")

    conn.close()
    print("\nTerminé.")
    print(f"  {sum(len(v) for v in cid_to_embs.values())} face(s) traitées")
    print(f"  {len(suggestions)} suggestion(s) enregistrées")
    print("\nRelancez l'application pour voir les nouvelles suggestions.")


if __name__ == "__main__":
    main()
