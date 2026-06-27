# Pipeline : détection, groupement et identification des visages

## Vue d'ensemble

```
Photo sur disque
      │
      ▼
┌─────────────┐     embedding 512-dim
│  Détection  │ ──────────────────────► Table faces (BD SQLite)
│  RetinaFace │                          person_id = NULL
│  + ArcFace  │                          cluster_id = NULL
└─────────────┘
                                              │
                                   Déclenchement manuel
                                   « Lancer le clustering »
                                              │
                                              ▼
                                   ┌────────────────────┐
                                   │    Clustering       │
                                   │  PCA 512→32 dims    │
                                   │  + HDBSCAN          │
                                   └────────────────────┘
                                              │
                                   cluster_id attribué
                                   (entier positif)
                                              │
                                              ▼
                                   ┌────────────────────┐
                                   │   Suggestions       │
                                   │  similarité cosinus │
                                   │  cluster ↔ personne │
                                   └────────────────────┘
                                              │
                                   suggestion_person_id
                                              │
                                              ▼
                                   ┌────────────────────┐
                                   │  Validation humaine │
                                   │  Accepter / Rejeter │
                                   └────────────────────┘
                                              │
                                   person_id attribué
```

---

## Phase 1 — Détection et embedding

**Fichier** : `src/faces/detector.py`

À chaque scan d'une photo :

```
Photo (JPEG/PNG/…)
       │
       ▼
  RetinaFace  ──► bounding box (x, y, w, h)  ◄── stocké dans faces.bbox_*
       │
       ▼
   ArcFace    ──► vecteur 512 flottants        ◄── stocké dans faces.embedding (blob)
```

Chaque visage détecté devient une ligne dans la table `faces` :

| Colonne            | Valeur initiale     |
|--------------------|---------------------|
| `photo_path`       | chemin de la photo  |
| `bbox_x/y/w/h`     | rectangle détecté   |
| `embedding`        | vecteur 512-dim     |
| `person_id`        | `NULL`              |
| `cluster_id`       | `NULL`              |
| `pinned`           | `0`                 |
| `ignored`          | `0`                 |
| `suggestion_person_id` | `NULL`          |

---

## Phase 2 — Clustering HDBSCAN

**Fichiers** : `src/faces/clusterer.py`, `src/faces/face_database.py`

Le clustering est déclenché manuellement depuis le menu Visages. Il tourne dans un **subprocess isolé** (via `multiprocessing.Process + Pipe`) pour ne pas bloquer l'UI.

### 2a. Pré-assignation des visages déjà identifiés

Avant HDBSCAN, les visages qui ont déjà un `person_id` reçoivent un `cluster_id` **synthétique** :

```
cluster_id = 10 000 000 + person_id
```

Cela les exclut du clustering et évite que HDBSCAN réutilise le même entier pour un groupe différent d'un run au suivant.

### 2b. Pipeline HDBSCAN sur les visages non identifiés

```
N visages sans person_id
        │
        ▼
Normalisation L2  (→ sphère unité)
        │
        ▼
PCA 512 → 32 dims  (>90 % variance conservée)
   + re-normalisation L2
        │
        ▼
HDBSCAN  (euclidien, boruvka_balltree)
   min_cluster_size = 2
   min_samples      = 1
        │
        ├─► label ≥ 0 → cluster_id = label  (groupe de ≥ 2 visages)
        │
        └─► label = -1 → singleton → cluster_id = max_label + 1, +2, +3…
                         (chaque singleton est son propre cluster)
```

### 2c. Sauvegarde et propagation

Après HDBSCAN, `update_clusters()` :

1. Remet `cluster_id = NULL` sur tous les visages non identifiés non pinnés (reset propre)
2. Écrit les nouveaux `cluster_id` en masse
3. **Propage le `person_id`** aux visages sans personne dans un cluster où d'autres visages ont déjà un `person_id` :

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

Résultat : un nouveau visage de la même personne, regroupé dans son cluster habituel, hérite automatiquement du `person_id`.

---

## Phase 3 — Calcul des suggestions

**Fichier** : `src/ui/face_cluster_grid.py`, fonction `_compute_all_suggestions_bg()`

Pour chaque cluster sans `person_id`, on calcule la similarité cosinus entre son centroïde et les centroïdes des personnes connues.

```
Clusters sans personne          Personnes connues
   (cluster_id > 0,               (person_id ≠ NULL)
    person_id = NULL)
         │                               │
         ▼                               ▼
   centroïde cluster C          centroïde personne P
   (moyenne des embeddings)     (moyenne par sous-cluster)
         │                               │
         └──────── similarité cosinus ───┘
                         │
               ┌─────────┴──────────┐
               │  score ≥ 0.82      │  score ∈ [0.50, 0.82)
               ▼                    ▼
         suggestion forte     suggestion faible
         « ≈ Prénom (85%) »   « ~ Prénom (67%) »
         couleur bleue        couleur grise
```

La similarité est calculée par **produit matriciel** en une seule passe :

```
(n_clusters, 512) × (n_embeddings_personnes, 512)ᵀ
          ─────────────────────────────────────────
                    matrice (n_clusters × n_pers)
```

Le résultat est stocké dans `faces.suggestion_person_id` et `faces.suggestion_score`.

### Seuils

| Seuil  | Valeur | Signification                        |
|--------|--------|--------------------------------------|
| `_SIM_WEAK`    | 0.50 | minimum pour proposer une suggestion |
| `_SIM_STRONG`  | 0.82 | suggestion forte (couleur bleue)     |
| `_SIM_SUGGEST` | 0.50 | seuil après dé-association manuelle  |

---

## Phase 4 — Validation et identification

### 4a. Accepter une suggestion (cluster entier)

```
Clic ✓ sur la carte de groupe
        │
        ▼
assign_cluster_to_person(cluster_id, person_id)
   → UPDATE faces SET person_id=? WHERE cluster_id=?
   → déduplique (une seule face par photo)
```

### 4b. Identifier un visage seul (isolation + assignation)

```
Clic-droit → Identifier
        │
        ▼
isolate_and_assign_face(face_id, person_id)
   → cluster_id = MIN(cluster_id_pinnés) - 1  (entier négatif unique)
   → pinned = 1
   → person_id = person_id choisi
```

Ce visage est désormais **isolé** : ni le clustering ni la propagation ne le toucheront plus.

### 4c. Rejeter une suggestion

```
Clic ✗ sur la vignette (ou bouton de la carte)
        │
        ▼  (immédiat — UI)
PersonClusterView.remove_pending_cluster(cluster_id)
   → retire la vignette de la section « En attente »

        │
        ▼  (arrière-plan — _ResuggestThread)
FaceDatabase.resuggest_clusters([cluster_id], exclude_person_id=personne_courante)
   → suggestion_person_id = NULL  (rejet enregistré)
   → charge les centroids de toutes les autres personnes
   → calcule la similarité cosinus contre le cluster
   → si score ≥ 0.50 → enregistre la nouvelle suggestion_person_id
                       → apparaîtra dans « En attente » de la prochaine personne
```

La face rejetée **n'est pas perdue** : elle est immédiatement réévaluée pour les
autres personnes. Si la similarité est suffisante, elle apparaîtra dans les
suggestions d'une autre personne lors de sa prochaine ouverture.

---

## États possibles d'un visage

```
                         ┌───────────────────────────────────────────────┐
                         │                   TABLE faces                  │
                         │  cluster_id  person_id  pinned  suggestion_*  │
                         └───────────────────────────────────────────────┘

  Juste détecté          │  NULL        NULL       0       NULL          │
  (pas encore clustérisé)│                                               │

  Dans un groupe         │  > 0         NULL       0       NULL          │
  (sans suggestion)      │                                               │

  Suggestion en attente  │  > 0         NULL       0       person_id = X │
                         │                                               │

  Identifié (naturel)    │  > 0         person_id  0       NULL          │
  (cluster HDBSCAN)      │                                               │

  Identifié (synthétique)│  10M+pid     person_id  0       NULL          │
  (pré-clustering)       │                                               │

  Isolé+identifié        │  < 0         person_id  1       NULL          │
  (assigné manuellement) │                                               │

  Isolé sans personne    │  < 0         NULL       1       person_id = X │
  (dé-associé, en        │                   ou NULL si aucune suggestion│
  attente de suggestion) │                                               │

  Ignoré                 │  quelconque  quelconque quelconque ignored=1  │
```

---

## Dé-association et réallocation

### Dé-association depuis la section confirmée

Lorsque l'utilisateur **dé-associe** un visage d'une personne depuis `PersonClusterView` :

```
_flat_unassign(face_ids)
        │
        ▼  (thread _UnassignThread)
isolate_and_suggest(face_ids, exclude_person_id=personne_courante)
        │
        ├─► cluster_id = entier négatif unique, pinned=1, person_id=NULL
        │
        └─► similarité cosinus vs toutes les autres personnes
                  │
                  ├─ score ≥ 0.50 → suggestion_person_id enregistré
                  │                  → apparaît dans « En attente »
                  │                    de la personne suggérée
                  │
                  └─ score < 0.50 → aucune suggestion (face toujours isolée)
```

La personne d'origine est **exclue** du calcul pour ne pas proposer immédiatement
de réassigner le visage à la même personne.

### Rejet d'une suggestion en attente

Lorsque l'utilisateur **rejette** une suggestion depuis la section « En attente » :

```
suggestion_rejected.emit(cluster_id)
        │
        ▼  (thread _ResuggestThread)
resuggest_clusters([cluster_id], exclude_person_id=personne_courante)
        │
        ├─► suggestion_person_id = NULL (rejet enregistré)
        │
        └─► similarité cosinus vs toutes les autres personnes (hors personne courante)
                  │
                  ├─ score ≥ 0.50 → nouvelle suggestion_person_id
                  │                  → visible dans la prochaine personne concernée
                  │
                  └─ score < 0.50 → aucune suggestion (face toujours isolée, pinned=1)
```

La face reste **isolée** (pinned=1, cluster_id < 0) dans les deux cas — seule la
suggestion change. Elle n'est jamais perdue dans les clusters sans nom.

---

## Résumé des cluster_id

| Plage            | Signification                                   |
|------------------|-------------------------------------------------|
| `NULL`           | Visage détecté, pas encore clustérisé           |
| `1 … ~175 000`   | Groupe HDBSCAN réel (≥ 2 visages)               |
| `175 001 … 9,9M` | Singleton HDBSCAN (1 visage, unique)            |
| `10 000 000+`    | Cluster synthétique (faces déjà identifiées)    |
| `< 0`            | Visage isolé manuellement (pinned=1)            |
