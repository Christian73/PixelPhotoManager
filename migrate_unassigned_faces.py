# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
#!/usr/bin/env python3
"""
Migration: isolates the unassigned faces (person_id=NULL, pinned=0, cluster_id>0)
into individual negative clusters and computes suggestions for other
people in a single pass (one read of the embeddings, one matrix product).

Optimised version: O(1) DB read instead of O(N clusters).
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

    # 1. Unassigned faces not isolated yet
    print("Lecture des visages dé-associés…")
    rows = conn.execute(
        "SELECT id, cluster_id, embedding FROM faces"
        " WHERE person_id IS NULL"
        "   AND (pinned IS NULL OR pinned = 0)"
        "   AND cluster_id IS NOT NULL"
        "   AND cluster_id > 0"
        "   AND cluster_id < 10000000"
        "   AND ignored = 0"
        "   AND suggestion_person_id IS NULL"
        "   AND embedding IS NOT NULL"
    ).fetchall()

    if not rows:
        print("Aucun visage dé-associé à migrer.")
        conn.close()
        return

    print(f"{len(rows)} visage(s) à traiter.")

    # Group by original cluster to determine the dominant person
    by_cluster: dict[int, list[int]] = {}
    face_embs: dict[int, list[float]] = {}
    for face_id, cluster_id, blob in rows:
        by_cluster.setdefault(cluster_id, []).append(face_id)
        face_embs[face_id] = _dec(blob)

    print(f"Répartis dans {len(by_cluster)} cluster(s) d'origine.")

    # 2. Dominant person per original cluster (for the exclusion)
    dominant: dict[int, int | None] = {}
    for cluster_id in by_cluster:
        dom = conn.execute(
            "SELECT person_id FROM faces"
            " WHERE cluster_id=? AND person_id IS NOT NULL"
            " GROUP BY person_id ORDER BY COUNT(*) DESC LIMIT 1",
            (cluster_id,),
        ).fetchone()
        dominant[cluster_id] = dom[0] if dom else None

    # 3. Load ALL the embeddings of the known people in a single query
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

    # 4. Compute the centroids of the people (numpy if available, otherwise pure Python)
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
        USE_NUMPY = True
    except ImportError:
        USE_NUMPY = False
        def centroid(embs):
            n, dim = len(embs), len(embs[0])
            return [sum(e[d] for e in embs) / n for d in range(dim)]
        person_centroids = {pid: centroid(by_person[pid]) for pid in person_ids}

    # 5. Isolate every face in a single transaction
    print("Isolation des visages…")
    row = conn.execute(
        "SELECT MIN(cluster_id) FROM faces WHERE pinned=1"
    ).fetchone()
    next_cid = (min(row[0], 0) - 1) if row and row[0] is not None else -1

    face_to_cid: dict[int, int] = {}   # face_id -> new negative cluster_id
    face_to_excl: dict[int, int | None] = {}  # face_id -> person_id to exclude

    conn.execute("BEGIN")
    for cluster_id, face_ids in by_cluster.items():
        excl = dominant[cluster_id]
        for face_id in face_ids:
            cid = next_cid
            next_cid -= 1
            face_to_cid[face_id] = cid
            face_to_excl[face_id] = excl
            conn.execute(
                "UPDATE faces SET cluster_id=?, pinned=1, person_id=NULL,"
                " suggestion_person_id=NULL, suggestion_score=NULL WHERE id=?",
                (cid, face_id),
            )
    conn.execute("COMMIT")
    print(f"{len(face_to_cid)} visage(s) isolé(s).")

    # 6. Computation of the suggestions in a single matrix pass
    print("Calcul des suggestions…")
    suggestions: dict[int, tuple[int, float]] = {}  # new_cid -> (person_id, score)

    if USE_NUMPY:
        # Group the faces by excluded person so as to do one matrix product per group
        # (we exclude the dominant person of the original cluster)
        by_excl: dict[int | None, list[tuple[int, int]]] = {}
        for face_id, cid in face_to_cid.items():
            excl = face_to_excl[face_id]
            by_excl.setdefault(excl, []).append((face_id, cid))

        for excl_pid, group in by_excl.items():
            # Indices of the people to keep
            valid_idx = [
                i for i, pid in enumerate(person_ids) if pid != excl_pid
            ]
            if not valid_idx:
                continue
            p_sub = p_mat[valid_idx]                        # (n_pers, dim)
            p_sub_ids = [person_ids[i] for i in valid_idx]

            face_ids_g = [f for f, _ in group]
            cids_g     = [c for _, c in group]
            f_mat = np.array(
                [face_embs[fid] for fid in face_ids_g], dtype=np.float32
            )                                               # (n_faces, dim)
            fn = np.linalg.norm(f_mat, axis=1, keepdims=True)
            fn[fn == 0] = 1.0
            f_mat /= fn

            sim = f_mat @ p_sub.T                          # (n_faces, n_pers)
            best_idx = np.argmax(sim, axis=1)
            best_sim = sim[np.arange(len(face_ids_g)), best_idx]

            for k, cid in enumerate(cids_g):
                s = float(best_sim[k])
                if s >= SIM_SUGGEST:
                    suggestions[cid] = (p_sub_ids[int(best_idx[k])], s)
    else:
        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na  = sum(x * x for x in a) ** 0.5
            nb  = sum(x * x for x in b) ** 0.5
            return dot / (na * nb) if na > 0 and nb > 0 else 0.0

        for face_id, cid in face_to_cid.items():
            excl = face_to_excl[face_id]
            emb  = face_embs[face_id]
            best_sim, best_pid = 0.0, None
            for pid, cent in person_centroids.items():
                if pid == excl:
                    continue
                s = cosine(emb, cent)
                if s > best_sim:
                    best_sim, best_pid = s, pid
            if best_pid is not None and best_sim >= SIM_SUGGEST:
                suggestions[cid] = (best_pid, best_sim)

    print(f"{len(suggestions)} suggestion(s) calculée(s).")

    # 7. Write every suggestion in a single transaction
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
    print("\nMigration terminée.")
    print(f"  {len(face_to_cid)} visage(s) isolé(s)")
    print(f"  {len(suggestions)} suggestion(s) enregistrée(s)")
    print("\nRelancez l'application pour voir les nouvelles suggestions.")


if __name__ == "__main__":
    main()
