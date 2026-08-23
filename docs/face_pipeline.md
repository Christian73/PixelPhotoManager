# Pipeline: face detection, clustering and identification

## Overview

```
Photo on disk
      │
      ▼
┌─────────────┐     512-dim embedding
│  Detection  │ ──────────────────────► faces table (SQLite DB)
│  RetinaFace │                          person_id = NULL
│  + ArcFace  │                          cluster_id = NULL
└─────────────┘
                                              │
                                     Manual trigger
                                     "Run the clustering"
                                              │
                                              ▼
                                   ┌────────────────────┐
                                   │     Clustering     │
                                   │  PCA 512→32 dims   │
                                   │  + HDBSCAN         │
                                   └────────────────────┘
                                              │
                                     cluster_id assigned
                                     (positive integer)
                                              │
                                              ▼
                                   ┌────────────────────┐
                                   │    Suggestions     │
                                   │ cosine similarity  │
                                   │ cluster ↔ person   │
                                   └────────────────────┘
                                              │
                                     suggestion_person_id
                                              │
                                              ▼
                                   ┌────────────────────┐
                                   │ Human validation   │
                                   │  Accept / Reject   │
                                   └────────────────────┘
                                              │
                                      person_id assigned
```

---

## Phase 1 — Detection and embedding

**File**: `src/faces/detector.py`

Every time a photo is scanned:

```
Photo (JPEG/PNG/…)
       │
       ▼
  RetinaFace  ──► bounding box (x, y, w, h)  ◄── stored in faces.bbox_*
       │
       ▼
   ArcFace    ──► vector of 512 floats        ◄── stored in faces.embedding (blob)
```

Every detected face becomes a row in the `faces` table:

| Column             | Initial value       |
|--------------------|---------------------|
| `photo_path`       | path of the photo   |
| `bbox_x/y/w/h`     | detected rectangle  |
| `embedding`        | 512-dim vector      |
| `person_id`        | `NULL`              |
| `cluster_id`       | `NULL`              |
| `pinned`           | `0`                 |
| `ignored`          | `0`                 |
| `suggestion_person_id` | `NULL`          |

---

## Phase 2 — HDBSCAN clustering

**Files**: `src/faces/clusterer.py`, `src/faces/face_database.py`

Clustering is triggered manually from the Faces menu. It runs in an **isolated subprocess** (through `multiprocessing.Process + Pipe`) so that it never blocks the UI.

### 2a. Pre-assignment of already identified faces

Before HDBSCAN, faces that already have a `person_id` are given a **synthetic** `cluster_id`:

```
cluster_id = 10 000 000 + person_id
```

This excludes them from the clustering and prevents HDBSCAN from reusing the same integer for a different group from one run to the next.

### 2b. HDBSCAN pipeline on the unidentified faces

```
N faces without person_id
        │
        ▼
L2 normalisation  (→ unit sphere)
        │
        ▼
PCA 512 → 32 dims  (>90% of variance kept)
   + L2 re-normalisation
        │
        ▼
HDBSCAN  (euclidean, boruvka_balltree)
   min_cluster_size = 2
   min_samples      = 1
        │
        ├─► label ≥ 0 → cluster_id = label  (group of ≥ 2 faces)
        │
        └─► label = -1 → singleton → cluster_id = max_label + 1, +2, +3…
                         (each singleton is its own cluster)
```

### 2c. Saving and propagation

After HDBSCAN, `update_clusters()`:

1. Resets `cluster_id = NULL` on every unidentified, unpinned face (clean reset)
2. Writes the new `cluster_id` values in bulk
3. **Propagates the `person_id`** to faces without a person in a cluster where other faces already have a `person_id`:

```sql
UPDATE faces
SET person_id = (SELECT person_id FROM faces f2
                 WHERE f2.cluster_id = faces.cluster_id
                   AND f2.person_id IS NOT NULL LIMIT 1)
WHERE cluster_id IS NOT NULL
  AND person_id IS NULL
  AND EXISTS (SELECT 1 FROM faces f3
              WHERE f3.cluster_id = faces.cluster_id
                AND f3.person_id IS NOT NULL)
```

Result: a new face of the same person, grouped into its usual cluster, automatically inherits the `person_id`.

---

## Phase 3 — Computing the suggestions

**File**: `src/ui/face_cluster_grid.py`, function `_compute_all_suggestions_bg()`

For every cluster without a `person_id`, the cosine similarity between its centroid and the centroids of the known people is computed.

```
Clusters without a person       Known people
   (cluster_id > 0,               (person_id ≠ NULL)
    person_id = NULL)
         │                               │
         ▼                               ▼
   cluster centroid C            person centroid P
   (mean of the embeddings)      (mean per sub-cluster)
         │                               │
         └──────── cosine similarity ────┘
                         │
               ┌─────────┴──────────┐
               │  score ≥ 0.82      │  score ∈ [0.50, 0.82)
               ▼                    ▼
         strong suggestion    weak suggestion
         "≈ Name (85%)"       "~ Name (67%)"
         blue                 grey
```

The similarity is computed as a **matrix product** in a single pass:

```
(n_clusters, 512) × (n_person_embeddings, 512)ᵀ
          ─────────────────────────────────────────
                    matrix (n_clusters × n_persons)
```

The result is stored in `faces.suggestion_person_id` and `faces.suggestion_score`.

### Thresholds

| Threshold | Value | Meaning                              |
|-----------|-------|--------------------------------------|
| `_SIM_WEAK`    | 0.50 | minimum before offering a suggestion |
| `_SIM_STRONG`  | 0.82 | strong suggestion (blue)             |
| `_SIM_SUGGEST` | 0.50 | threshold after a manual unassignment |

---

## Phase 4 — Validation and identification

### 4a. Accepting a suggestion (whole cluster)

```
Click ✓ on the group card
        │
        ▼
assign_cluster_to_person(cluster_id, person_id)
   → UPDATE faces SET person_id=? WHERE cluster_id=?
   → deduplicates (a single face per photo)
```

### 4b. Identifying a single face (isolation + assignment)

```
Right-click → Identify
        │
        ▼
isolate_and_assign_face(face_id, person_id)
   → cluster_id = MIN(pinned cluster_id) - 1  (unique negative integer)
   → pinned = 1
   → person_id = the chosen person_id
```

That face is now **isolated**: neither the clustering nor the propagation will touch it again.

### 4c. Rejecting a suggestion

```
Click ✗ on the thumbnail (or the card button)
        │
        ▼  (immediate — UI)
PersonClusterView.remove_pending_cluster(cluster_id)
   → removes the thumbnail from the "Pending" section

        │
        ▼  (background — _ResuggestThread)
FaceDatabase.resuggest_clusters([cluster_id], exclude_person_id=current person)
   → suggestion_person_id = NULL  (rejection recorded)
   → loads the centroids of every other person
   → computes the cosine similarity against the cluster
   → if score ≥ 0.50 → records the new suggestion_person_id
                       → will show up in the "Pending" of the next person
```

The rejected face is **not lost**: it is immediately re-evaluated for the other
people. If the similarity is high enough, it will appear in the suggestions of
another person the next time that person is opened.

---

## Possible states of a face

```
                         ┌───────────────────────────────────────────────┐
                         │                  faces TABLE                   │
                         │  cluster_id  person_id  pinned  suggestion_*  │
                         └───────────────────────────────────────────────┘

  Just detected          │  NULL        NULL       0       NULL          │
  (not yet clustered)    │                                               │

  In a group             │  > 0         NULL       0       NULL          │
  (no suggestion)        │                                               │

  Pending suggestion     │  > 0         NULL       0       person_id = X │
                         │                                               │

  Identified (natural)   │  > 0         person_id  0       NULL          │
  (HDBSCAN cluster)      │                                               │

  Identified (synthetic) │  10M+pid     person_id  0       NULL          │
  (pre-clustering)       │                                               │

  Isolated + identified  │  < 0         person_id  1       NULL          │
  (assigned manually)    │                                               │

  Isolated, no person    │  < 0         NULL       1       person_id = X │
  (unassigned, awaiting  │                    or NULL if no suggestion   │
  a suggestion)          │                                               │

  Ignored                │  any         any        any     ignored=1     │
```

---

## Unassignment and reallocation

### Unassigning from the confirmed section

When the user **unassigns** a face from a person in `PersonClusterView`:

```
_flat_unassign(face_ids)
        │
        ▼  (thread _UnassignThread)
isolate_and_suggest(face_ids, exclude_person_id=current person)
        │
        ├─► cluster_id = unique negative integer, pinned=1, person_id=NULL
        │
        └─► cosine similarity vs every other person
                  │
                  ├─ score ≥ 0.50 → suggestion_person_id recorded
                  │                  → shows up in the "Pending" of the
                  │                    suggested person
                  │
                  └─ score < 0.50 → no suggestion (face still isolated)
```

The original person is **excluded** from the computation so that the face is not
immediately offered back to the same person.

### Rejecting a pending suggestion

When the user **rejects** a suggestion from the "Pending" section:

```
suggestion_rejected.emit(cluster_id)
        │
        ▼  (thread _ResuggestThread)
resuggest_clusters([cluster_id], exclude_person_id=current person)
        │
        ├─► suggestion_person_id = NULL (rejection recorded)
        │
        └─► cosine similarity vs every other person (current person excluded)
                  │
                  ├─ score ≥ 0.50 → new suggestion_person_id
                  │                  → visible in the next person concerned
                  │
                  └─ score < 0.50 → no suggestion (face still isolated, pinned=1)
```

The face stays **isolated** (pinned=1, cluster_id < 0) in both cases — only the
suggestion changes. It is never lost among the unnamed clusters.

---

## Summary of cluster_id values

| Range            | Meaning                                         |
|------------------|-------------------------------------------------|
| `NULL`           | Face detected, not clustered yet                |
| `1 … ~175,000`   | Real HDBSCAN group (≥ 2 faces)                  |
| `175,001 … 9.9M` | HDBSCAN singleton (1 face, unique)              |
| `10,000,000+`    | Synthetic cluster (already identified faces)    |
| `< 0`            | Face isolated manually (pinned=1)               |
