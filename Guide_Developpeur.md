# Guide développeur — PixelPhotoManager

> **Stack** : Python 3.11 · PySide6 6.x · Pillow · OpenCV · SQLite (stdlib)  
> **Plateforme cible** : Windows 10/11 (distribution via PyInstaller)

---

## Table des matières

1. [Arborescence des sources](#1-arborescence-des-sources)
2. [Démarrage du projet](#2-démarrage-du-projet)
3. [Architecture générale](#3-architecture-générale)
4. [Composants core](#4-composants-core)
5. [Composants library](#5-composants-library)
6. [Composants UI](#6-composants-ui)
7. [Composants processing](#7-composants-processing)
8. [Reconnaissance faciale](#8-reconnaissance-faciale)
9. [Détection de doublons](#9-détection-de-doublons)
10. [Système de plugins](#10-système-de-plugins)
11. [Schémas des bases de données](#11-schémas-des-bases-de-données)
12. [Modèle de threading](#12-modèle-de-threading)
13. [Normalisation des chemins Windows](#13-normalisation-des-chemins-windows)
14. [Packaging et distribution](#14-packaging-et-distribution)
15. [Patterns à suivre pour les évolutions](#15-patterns-à-suivre-pour-les-évolutions)
16. [Tests](#16-tests)

---

## 1. Arborescence des sources

```
PixelPhotoManager/
│
├── main.py                        # Point d'entrée unique
├── pixelphotomanager.spec         # Spec PyInstaller (packaging)
├── build.ps1                      # Script de build PowerShell
├── requirements.txt               # Dépendances Python (cœur applicatif)
├── requirements-test-e2e.txt      # Dépendances des tests bout-en-bout (pywinauto, Windows-only, optionnel)
├── pytest.ini                     # Config pytest : markers e2e/gui, addopts -m "not e2e"
├── Guide_Utilisateur.md
├── Guide_Developpeur.md
│
├── assets/
│   └── lutin_camera_icon_download.ico   # Icône de l'application
│
├── src/
│   ├── core/                      # Socle transversal
│   │   ├── app_dirs.py            # Chemin APP_DATA_DIR
│   │   ├── app_version.py         # Numéro de version (VERSION en mode figé, git describe en dev)
│   │   ├── config.py              # Config singleton (JSON)
│   │   ├── event_bus.py           # Bus d'événements pub/sub
│   │   ├── models.py              # Dataclasses (PhotoInfo, EditInfo…)
│   │   ├── cpu_throttle.py        # Limite CPU (~15% cœurs) + priorité OS abaissée (dedup/visages)
│   │   ├── thread_journal.py      # Journal JSONL des threads de fond (Outils › Journal des threads…)
│   │   ├── update_checker.py      # Vérification de version via l'API releases GitHub
│   │   ├── problems_history.py    # Historique des analyses de doublons avec fichiers corrompus
│   │   ├── deleted_corrupted_files.py  # Suivi des fichiers corrompus supprimés
│   │   ├── base_plugin.py         # Classe de base des plugins
│   │   ├── processor_plugin.py    # Sous-classe plugin de traitement
│   │   └── plugin_manager.py      # Chargement dynamique des plugins
│   │
│   ├── library/                   # Gestion de la bibliothèque
│   │   ├── catalog.py             # Catalogue SQLite (photos, albums, groupes de doublons)
│   │   ├── scanner.py             # Scan de dossiers en thread
│   │   ├── folder_watcher.py      # Surveillance disque (QFileSystemWatcher)
│   │   ├── fs_utils.py            # is_hidden_path() — test « chemin caché » partagé
│   │   ├── thumbnail_cache.py     # Cache vignettes 3 niveaux (verrou d'écriture dédié)
│   │   ├── exif_reader.py         # Lecture EXIF (ExifReader) + vidéo (VideoMetadataReader)
│   │   ├── duplicate_detector.py  # Détection de doublons (pHash Tier 1 + ORB/RANSAC Tier 2)
│   │   ├── dedup_cache.py         # Cache incrémental de la détection (dedup_cache.db)
│   │   └── file_repair.py         # Réparation des fichiers corrompus détectés pendant le scan doublons
│   │
│   ├── ui/                        # Interface PySide6
│   │   ├── main_window.py         # Fenêtre principale + orchestration (mixins Faces/Duplicates)
│   │   ├── main_window_faces.py   # FacesController (mixin) — indexation, clustering, personnes
│   │   ├── main_window_duplicates.py  # DuplicatesController (mixin) — détection, fichiers corrompus
│   │   ├── background_workers.py  # QThread transverses de MainWindow (chargement, suppression…)
│   │   ├── sidebar.py             # Arborescence dossiers + albums
│   │   ├── thumbnail_grid.py      # Grille de vignettes (badge ▶ pour vidéos)
│   │   ├── photo_viewer.py        # Visionneuse (délègue à viewer_canvas.py / viewer_pixmaps.py)
│   │   ├── viewer_canvas.py       # Canvas interactif (_Canvas) : recadrage, annotations, zoom
│   │   ├── viewer_pixmaps.py      # Pipeline pixmap (chargement image/vidéo + retouches)
│   │   ├── edit_panel.py          # Panneau de retouche (un seul outil actif à la fois)
│   │   ├── edit_sliders.py        # Widgets slider du panneau de retouche (MarkedSlider/EditSlider)
│   │   ├── edit_icons.py          # Icônes dessinées (QPixmap) des boutons de retouche
│   │   ├── treatment_dialogs.py   # Dialogues de correction (Luminosité, Couleurs, Vignette…)
│   │   ├── annotation_renderer.py # Rendu QPainter du calque d'annotations (aperçu + export)
│   │   ├── export_dialogs.py      # Dialogues Enregistrer/Exporter
│   │   ├── exif_panel.py          # Panneau EXIF (toggle avec touche I)
│   │   ├── exif_date_sync_dialog.py  # Outils › Synchroniser les dates EXIF…
│   │   ├── slideshow.py           # Diaporama plein écran (effet Ken Burns)
│   │   ├── display_order_dialog.py   # Affichage › Ordre d'affichage…
│   │   ├── folder_manager_dialog.py  # Dialogue Outils › Dossiers…
│   │   ├── duplicate_grid.py      # Grille des groupes de doublons (bouton « Dupliquées »)
│   │   ├── duplicates_popup.py    # Popup « Doublons de cette photo » depuis le badge ⧉
│   │   ├── index_errors_dialog.py    # Visages › Visualisation des erreurs…
│   │   ├── problems_history_dialog.py  # Outils › Historique des problèmes…
│   │   ├── deleted_corrupted_files_dialog.py  # Historique des fichiers corrompus supprimés
│   │   ├── thread_journal_dialog.py  # Outils › Journal des threads…
│   │   ├── settings_dialog.py     # Outils › Paramètres
│   │   ├── loading_label.py       # Indicateur de chargement générique
│   │   ├── ui_utils.py            # fmt_size() et utilitaires UI partagés
│   │   ├── help_dialog.py         # Dialogue Aide/À propos (contenu dans help_content/*.html)
│   │   ├── face_panel.py          # Panneau visages dans la visionneuse
│   │   ├── people_panel.py        # Vue « Personnes » (groupes non identifiés) + assignation
│   │   ├── person_cluster_view.py # Vue détaillée d'une personne (visages confirmés/en attente)
│   │   ├── face_cluster_grid.py   # Grille des groupes non identifiés
│   │   ├── face_cluster_cards.py  # Cartes de groupe (widgets de face_cluster_grid.py)
│   │   ├── face_cluster_workers.py   # Threads de rafraîchissement de la vue Personnes
│   │   ├── face_merge_dialog.py   # Fusion de groupes/personnes
│   │   ├── face_backup_dialog.py  # Visages › Sauvegarder/Gérer les sauvegardes…
│   │   ├── face_counters_dialog.py   # Visages › Compteurs…
│   │   ├── picasa_import_dialog.py   # Visages › Importer depuis Picasa…
│   │   └── reset_faces_dialog.py  # Visages › Réinitialiser et réindexer…
│   │
│   ├── processing/                # Traitement image
│   │   ├── adjustments.py         # ImageAdjuster.apply_all()
│   │   ├── geometry.py            # Rotation, redressement, recadrage
│   │   ├── annotation_geometry.py # Géométrie des formes d'annotation (hit-test, bbox, redimensionnement)
│   │   └── edit_database.py       # Persistence des retouches (SQLite), inclut EditInfo.annotations
│   │
│   ├── faces/                     # Reconnaissance faciale (optionnel)
│   │   ├── detector.py            # Détection + embedding via InsightFace (buffalo_l : SCRFD + ArcFace)
│   │   ├── clusterer.py           # Clustering HDBSCAN (scikit-learn/hdbscan) + purification des clusters
│   │   ├── face_database.py       # Base SQLite faces.db (visages, clusters, personnes, suggestions)
│   │   ├── face_indexer.py        # Threads d'indexation/réindexation/nouvelle tentative
│   │   ├── gpu_utils.py           # Détection GPU (CUDA/DirectML) pour l'inférence InsightFace
│   │   └── picasa_importer.py     # Import annotations + retouches Picasa (.picasa.ini)
│   │
│   └── plugins/                   # Plugins intégrés (src/)
│
├── plugins/                       # Plugins utilisateur externes
│
├── tests/                         # Layers 1+2 (voir §16 Tests)
│   ├── conftest.py                 # Isolation LOCALAPPDATA globale
│   ├── test_*.py                   # Layer 1 : logique pure
│   ├── gui_widgets/                 # Layer 2 : widgets Qt (pytest-qt)
│   └── e2e/                        # Layer 3 : bout-en-bout (pywinauto, voir e2e/README.md)
│       ├── conftest.py             # Fixture isolated_app
│       └── scenarios/              # Un fichier par scénario
│
└── tools/
    └── test_env/                  # Utilitaires Layer 3 : launch_isolated.py, generate_library.py
```

**Données runtime** (non versionnées) :

```
%LOCALAPPDATA%\PixelPhotoManager\
├── catalog.db       # Index des photos et vidéos
├── thumbnails.db    # Cache des vignettes
├── edits.db         # Retouches non destructives
├── config.json      # Configuration
└── logs/
    └── pixelphotomanager.log
```

---

## 2. Démarrage du projet

```powershell
# Créer l'environnement virtuel
python -m venv .venv

# Activer le venv
.venv\Scripts\Activate.ps1

# Installer les dépendances
.venv\Scripts\pip.exe install -r requirements.txt

# Lancer l'application
.venv\Scripts\python.exe main.py

# Lancer les tests (Layers 1+2 — unitaire + widgets Qt, multiplateforme, ~10 s)
.venv\Scripts\python.exe -m pytest tests/

# Optionnel : dépendances des tests bout-en-bout (Layer 3, Windows uniquement)
.venv\Scripts\pip.exe install -r requirements-test-e2e.txt
.venv\Scripts\python.exe -m pytest tests/e2e -m e2e

# Construire l'EXE
.\build.ps1
```

Voir [§16 Tests](#16-tests) pour le détail des trois couches, l'isolation des données et la mesure de couverture.

---

## 3. Architecture générale

### Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────────┐
│                          main.py                                │
│  QApplication · Config · Catalog · ThumbnailCache · Scanner    │
│                       MainWindow                                │
└───────────┬──────────────────────────────────────┬─────────────┘
            │ signaux PySide6                       │ bus.emit/on
            ▼                                       ▼
┌───────────────────────┐              ┌────────────────────────┐
│        UI Layer       │              │      Event Bus         │
│  Sidebar              │◄────────────►│  bus = EventBus()      │
│  ThumbnailGrid        │              │  (singleton global)    │
│  PhotoViewer          │              └────────────────────────┘
│  EditPanel            │
│  ExifPanel            │
│  FolderManagerDialog  │
└───────────┬───────────┘
            │ appels directs
            ▼
┌───────────────────────────────────────────────────────────────┐
│                      Library / Processing                      │
│   Catalog (SQLite)    ThumbnailCache    LibraryScanner        │
│   EditDatabase        ImageAdjuster     ExifReader            │
│   VideoMetadataReader                                         │
└───────────────────────────────────────────────────────────────┘
```

### Principes fondamentaux

| Principe | Implémentation |
|---|---|
| **Non destructif** | Les fichiers originaux ne sont jamais modifiés. Les retouches sont dans `edits.db`, appliquées à la volée. |
| **UI non bloquante** | Toute opération > 50 ms passe dans un `QThread`. Seuls les signaux PySide6 communiquent vers l'UI. |
| **Couplage faible** | Les composants communiquent via le bus d'événements. Les appels directs sont réservés aux relations parent → enfant établies. |
| **Cache à 3 niveaux** | RAM LRU → SQLite → génération à la demande (en thread). |
| **Chemins normalisés** | `os.path.normpath` systématique pour éviter les incohérences `/` vs `\` sur Windows. |

---

## 4. Composants core

### `app_dirs.py` — Chemins de données

```python
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PixelPhotoManager"
```

Unique source de vérité pour l'emplacement des bases de données et de la configuration. Toujours importer `APP_DATA_DIR` depuis ce module, ne jamais coder le chemin en dur.

---

### `config.py` — Configuration (Singleton)

`Config` est un **singleton** (via `__new__`). Une seule instance existe pour toute la durée de vie du processus.

```python
from src.core.config import Config
config = Config()          # retourne toujours la même instance
config.get("ui.theme")     # clés hiérarchiques séparées par "."
config.set("thumbnail_size", 250)   # sauvegarde immédiate dans config.json
```

**Clés de configuration** :

| Clé | Type | Défaut | Description |
|---|---|---|---|
| `scan_folders` | `list[str]` | `[]` | Dossiers surveillés |
| `thumbnail_size` | `int` | `180` | Taille des vignettes (px) |
| `sort_by` | `str` | `"date_taken"` | Champ de tri |
| `sort_desc` | `bool` | `True` | Tri décroissant |
| `ui.sidebar_width` | `int` | `240` | Largeur de la sidebar |
| `ui.theme` | `str` | `"dark"` | Thème de l'interface |
| `plugins.<id>` | `dict` | `{}` | Paramètres par plugin |

---

### `event_bus.py` — Bus d'événements

```python
from src.core.event_bus import bus   # singleton global

# S'abonner
bus.on("library.photo_selected", self.handler)
bus.once("scan.finished", callback)    # une seule fois

# Émettre
bus.emit("library.photo_selected", photo=photo_info)

# Se désabonner
bus.off("library.photo_selected", self.handler)
```

**Événements définis** (liste non exhaustive) :

| Événement | Kwargs | Emetteur |
|---|---|---|
| `library.folder_selected` | `folder: str` | Sidebar |
| `library.photo_selected` | `photo: PhotoInfo` | — |
| `library.photo_discovered` | `photo: PhotoInfo` | MainWindow |
| `album.create_requested` | `name: str` | Sidebar |
| `plugin.activated` | `plugin_id: str` | PluginManager |
| `plugin.deactivated` | `plugin_id: str` | PluginManager |

> **Règle** : tout nouvel événement doit être documenté dans le docstring de `EventBus`. Les handlers sont appelés de façon synchrone dans le thread émetteur. Ne pas émettre depuis un thread secondaire vers l'UI : utiliser les signaux PySide6.

---

### `cpu_throttle.py` — Limitation CPU des tâches de fond

Les traitements permanents (détection de doublons, indexation/clustering des visages) ne
doivent pas rendre l'application désagréable à utiliser pendant qu'ils tournent.

```python
from src.core.cpu_throttle import throttled_worker_count, lower_current_thread_priority, lower_current_process_priority

THROTTLE_FRACTION = 0.15  # ~15 % des cœurs disponibles

n = throttled_worker_count(minimum=1)          # nombre de workers à utiliser
executor = ThreadPoolExecutor(max_workers=n, initializer=lower_current_thread_priority)
pool = ProcessPoolExecutor(max_workers=n, initializer=lower_current_process_priority)
```

`lower_current_thread_priority()` (Windows `SetThreadPriority(THREAD_PRIORITY_BELOW_NORMAL)`) et
`lower_current_process_priority()` (via `psutil`, `BELOW_NORMAL_PRIORITY_CLASS`) s'utilisent comme
`initializer` d'un pool, pour que chaque worker démarre déjà à priorité réduite.

---

### `thread_journal.py` — Journal des threads de fond

Journal JSON-Lines (`%LOCALAPPDATA%\PixelPhotoManager\thread_journal.jsonl`, rotation à 8000
lignes → 5000 conservées) alimentant **Outils › Journal des threads…** (`thread_journal_dialog.py`) —
diagnostic si l'application semble lente ou bloquée (scan, indexation visages, clustering, vignettes…).

```python
from src.core.thread_journal import journal

t0 = journal.start("ScanThread", "scan dossier X")
journal.step("ScanThread", "1000 photos lues", t0=t0)
journal.end("ScanThread", "terminé", t0)      # ou journal.error(...) en cas d'exception
journal.get_entries(limit=2000)               # pour l'UI
journal.clear()
```

Chaque entrée : `{ts, wall, tid, thread, event, msg, elapsed_ms, **extra}` — `event` ∈
`START/STEP/END/ERROR`. Singleton global `journal`, thread-safe.

---

### `models.py` — Modèles de données

#### `PhotoInfo` (dataclass)

Représente une photo ou une vidéo dans le catalogue. **Le champ `path` est normalisé** (`os.path.normpath`) dans `__post_init__`.

```python
@dataclass
class PhotoInfo:
    path: str           # Chemin absolu normalisé (clé primaire)
    filename: str = ""  # Calculé depuis path si absent
    directory: str = "" # Calculé depuis path si absent
    date_taken: Optional[datetime] = None
    width: int = 0
    height: int = 0
    file_size: int = 0
    file_mtime: float = 0.0
    camera_make: str = ""
    camera_model: str = ""
    # ... EXIF, GPS, favoris, tags, id
    media_type: str = "image"   # "image" ou "video"
    duration: float = 0.0       # durée en secondes (vidéos uniquement)
```

#### `EditInfo` (dataclass)

État complet des retouches d'une photo. Sérialisable via `to_dict()` / `from_dict()`.

```python
@dataclass
class EditInfo:
    brightness: float = 0.0      # [-1.0, +1.0]
    contrast:   float = 0.0      # [-1.0, +1.0]
    saturation: float = 0.0      # [-1.0, +1.0]
    gamma:      float = 1.0      # [0.1, 3.0]
    sharpness:  float = 0.0      # [0.0, 1.0]
    noise_reduction: float = 0.0  # [0.0, 1.0]
    rotation:   float = 0.0      # Multiple de 90°
    straighten: float = 0.0      # [-10.0, +10.0] degrés
    flip_h:     bool = False
    flip_v:     bool = False
    crop:       Optional[tuple] = None  # 8 coords relatives (0-1)
    bw:         bool = False
    bw_red:     float = 0.0      # [-1.0, +1.0]
    bw_green:   float = 0.0
    bw_blue:    float = 0.0
```

Le champ `crop` stocke un tuple de 8 valeurs `(x0,y0, x1,y1, x2,y2, x3,y3)` — coordonnées relatives (0 à 1) des 4 coins du quadrilatère de recadrage, dans l'ordre TL·TR·BR·BL, relatives à l'image **originale** (avant tout autre traitement géométrique).

---

## 5. Composants library

### `catalog.py` — Catalogue SQLite

Interface d'accès à `catalog.db`. Toutes les méthodes sont **thread-safe** : verrou `threading.Lock` sur chaque opération, et **connexion SQLite par (instance, thread)** mise en cache dans un `threading.local` porté par l'instance (pattern partagé avec `ThumbnailCache`, `FaceDatabase` et `EditDatabase`). La connexion est créée une seule fois par thread avec ses PRAGMAs (`journal_mode=WAL`, `synchronous=NORMAL`, `cache_size=-2048`) — l'ancien schéma « connexion neuve + PRAGMAs à chaque appel » coûtait souvent plus cher que la requête elle-même.

**Invariant du pattern** : les méthodes d'écriture ne ferment plus la connexion ; en cas d'exception, une garde `except BaseException: conn.rollback(); raise` remplace le rollback implicite qu'assurait l'ancienne fermeture. Une connexion cachée ne doit **jamais** rester au milieu d'une transaction ouverte (toutes les écritures suivantes échoueraient en `database is locked`). `close()` ferme la connexion du thread courant (tests, `closeEvent`).

```python
catalog = Catalog()   # utilise APP_DATA_DIR / "catalog.db"

catalog.add_or_update_photo(photo)          # INSERT OR REPLACE
catalog.get_photos_in_folder(folder)        # liste triée date desc
catalog.get_all_photos()
catalog.search(query)                       # filename, make, model
catalog.move_photo(old_path, new_path)
catalog.rename_photo(old_path, new_path)
catalog.delete_photo(path)
catalog.delete_photos(paths)                # variante lot (albums nettoyés, groupes doublons dissous)
catalog.add_photos_to_album(album_id, ids)  # lot — retourne le nombre réellement ajouté
catalog.remove_photos_from_album(album_id, ids)  # lot
catalog.get_known_mtimes(folder)            # dict {path: mtime} pour le scanner
catalog.update_paths_prefix(old, new)       # renommage de dossier en masse
catalog.count_photos_in_folder(folder)      # int — compte récursif pour le FolderManagerDialog
```

**Index** : `idx_photos_directory`, `idx_photos_dup_group`, `idx_photos_favorite`, `idx_photos_media_type` — les vues Favoris/Vidéos ne scannent pas la table.

**Migrations au démarrage** (dans `_init_db()`) :
- `_migrate_normalize_paths()` — normalise les séparateurs de chemins dans les données existantes.
- `_migrate_video_fields()` — ajoute les colonnes `media_type` et `duration` si absentes (pattern `try: ALTER TABLE … except OperationalError: pass`).

---

### `scanner.py` — Scan de dossiers

```
LibraryScanner.scan(folders, force=False) → ScanThread (QThread)
```

**Paramètre `force`** : quand `True`, `ScanThread` initialise `known = {}` au lieu de charger les mtimes depuis le catalogue. Tous les fichiers sont relus, même si non modifiés. Utilisé par le bouton **Re-scanner** du `FolderManagerDialog`.

**Extensions supportées** : `SUPPORTED_EXT = ExifReader.SUPPORTED | VIDEO_EXT`  
(`VIDEO_EXT` = `.mp4 .mov .avi .mkv .wmv .webm .m4v .3gp .flv .ts .mts .mpg .mpeg`)

**Algorithme du ScanThread :**

1. `os.walk(folder)` récursif → liste de tous les fichiers image/vidéo (`os.path.normpath` appliqué).
   - Exclut les dossiers cachés (attribut Windows `0x2` ou préfixe `.`) et les dossiers `Originals`.
2. Si `force=False` : `catalog.get_known_mtimes(folder)` → dict `{path: mtime}` des fichiers déjà indexés.
3. Pour chaque fichier : si `mtime` inchangé (±1 s) → skip. Sinon → lecture EXIF/vidéo → `catalog.add_or_update_photo()` → `photo_discovered.emit(photo)`.
4. Nettoyage des fantômes : supprime du catalogue les entrées dont le fichier a disparu du disque (seulement si le scan n'a pas été interrompu).

**Signaux émis** :

| Signal | Args | Fréquence |
|---|---|---|
| `photo_discovered` | `PhotoInfo` | Par photo nouvelle/modifiée |
| `photos_removed` | `list[str]` | Une fois, si des fantômes ont été trouvés |
| `progress` | `(int, str)` | Toutes les 50 photos |
| `finished` | `int` (total nouvelles) | Une fois |

---

### `folder_watcher.py` — Surveillance disque

`FolderWatcher` surveille récursivement les dossiers racine via `QFileSystemWatcher` (snapshots par dossier, debounce 400 ms) et émet `files_changed(path)` / `subfolder_added(path)`. `MainWindow._on_watcher_files_changed` répond par un rescan du sous-arbre concerné.

**Absorption des changements auto-infligés** : quand l'application supprime ou déplace elle-même des fichiers (touche Suppr, drag & drop, fichiers corrompus), l'événement watcher qui suit ne fait que constater ce que l'UI a déjà traité — le rescan serait du pur gaspillage (re-walk du dossier + refresh albums/personnes + réarmement de la détection de doublons).

```python
watcher.notify_self_deletions(paths, ttl_s=10.0)  # AVANT de toucher au disque
watcher.notify_self_additions(paths, ttl_s=10.0)  # destinations d'un déplacement
```

Les noms déclarés (par dossier, avec deadline) sont soustraits des ensembles apparus/disparus dans `_process()` : si l'événement ne contient **que** des changements annoncés, `files_changed` n'est pas émis. Tout changement externe dans le même dossier (autre fichier ajouté/supprimé, y c. dans le même événement) émet toujours. Le TTL borne le cas où l'opération annoncée échoue finalement.

---

### `thumbnail_cache.py` — Cache vignettes

Architecture à **3 niveaux** :

```
get(path)
  ├─ 1. RAM dict (LRU, max 500 entrées)  → O(1), retour immédiat
  ├─ 2. SQLite thumbnails.db (clé = MD5(path), mtime check)
  └─ 3. Génération en QThreadPool (_ThumbWorker)
            ├─ image : PIL.Image.open + resize → JPEG → RAM + DB
            └─ vidéo : cv2.VideoCapture → seek 10% → BGR→RGB → PIL → JPEG → RAM + DB
```

**Clé de cache** : `MD5(os.path.normpath(path))` — sensible à la casse et aux séparateurs. Toujours passer des chemins normalisés.

**Vignettes vidéo** : `generate()` détecte l'extension dans `VIDEO_EXT` et délègue à `_generate_video_thumb()`. La frame est extraite à `frame_count * 0.1` (10 % de la durée). Si cv2 n'est pas disponible ou si la lecture échoue, une vignette noire est stockée.

**API principale** :

```python
cache.get(photo_path)                  # QPixmap | None (niveaux 1 et 2 seulement)
cache.generate(photo_path, edit=None)  # Force la génération (thread secondaire)
cache.invalidate(photo_path)           # Supprime RAM + DB
cache.invalidate_many(paths)           # Supprime plusieurs entrées
cache.move_photo(old_path, new_path)   # Transfère l'entrée sans régénérer
```

**Règle** : ne jamais appeler `generate()` depuis le thread UI. Utiliser `_ThumbWorker` (QRunnable) via `QThreadPool.globalInstance()`.

**Verrou d'écriture** : contrairement à `Catalog`/`FaceDatabase`, `ThumbnailCache` protège ses écritures SQLite par un `threading.Lock()` dédié — sans lui, les 4 (voire 6, avec l'indexation visages en parallèle) threads de génération de vignettes déclenchés en rafale pendant la navigation se disputaient la connexion et ralentissaient perceptiblement le défilement de la grille.

---

### `exif_reader.py` — Lecture EXIF et métadonnées vidéo

#### `ExifReader`

```python
data = ExifReader.read(filepath)
# → dict avec : date_taken, width, height, camera_make, camera_model,
#               lens_model, iso, exposure_time, aperture, focal_length,
#               has_gps, gps_lat, gps_lon
```

Formats supportés (`.jpg .jpeg .png .tiff .tif .webp .bmp .gif`). La lecture GPS convertit les degrés/minutes/secondes en degrés décimaux.

#### `VideoMetadataReader`

```python
data = VideoMetadataReader.read(filepath)
# → dict avec : date_taken (st_mtime), width, height, fps, duration (secondes)
```

Utilise `cv2.VideoCapture`. Si cv2 n'est pas disponible, retourne un dict avec des valeurs neutres. La date de prise de vue est `os.stat(path).st_mtime` (date de modification du fichier).

#### Constante `VIDEO_EXT`

```python
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm",
             ".m4v", ".3gp", ".flv", ".ts", ".mts", ".mpg", ".mpeg"}
```

Importée par `scanner.py` et `thumbnail_cache.py`. Toujours utiliser cette constante pour tester si un fichier est une vidéo.

#### `ascii_safe_path(path)` — context manager

```python
with ascii_safe_path(video_path) as safe_path:
    cap = cv2.VideoCapture(safe_path)
```

`cv2.VideoCapture`/`cv2.imread` rejettent les chemins non-ASCII sur Windows. Si `path` est déjà ASCII, retourné tel quel sans I/O ; sinon crée un hardlink temporaire vers un chemin ASCII (repli sur une copie si le hardlink échoue, ex. volume différent), supprimé en sortie de bloc. À utiliser systématiquement autour de tout appel `cv2.VideoCapture`/`cv2.imread`.

#### `preserve_file_dates(src_stat, dst_path)`

```python
orig_stat = os.stat(path)
# ... écriture d'une nouvelle version de `path` ...
preserve_file_dates(orig_stat, path)
```

Copie `atime`/`mtime` (`os.utime`) et la date de création Windows (`ctypes` + `kernel32.SetFileTime`) de `src_stat` vers `dst_path`. Utilisée partout où un fichier est ré-écrit sur place sans que l'utilisateur ne doive voir ses dates changer (export avec écrasement, réparation de fichier corrompu — voir `file_repair.py`).

---

## 6. Composants UI

### Découpage 2026-07 : fichiers volumineux → modules délégués

`main_window.py`, `photo_viewer.py` et `edit_panel.py` ont chacun dépassé une taille où un
seul fichier devenait difficile à naviguer. Le découpage extrait les classes annexes vers des
modules dédiés, tout en **ré-exportant les noms historiques** depuis le module d'origine (bloc
d'import `# noqa: E402` en tête de fichier) pour que le code et les tests existants continuent
d'importer depuis `main_window`/`photo_viewer`/`edit_panel` sans changement. **Le nouveau code
doit importer directement le module dédié**, pas le ré-export.

- `main_window.py` délègue à :
  - `background_workers.py` — `QThread` transverses : `_CatalogLoadThread`, `_PhotoQueryThread`,
    `_DeleteWorkerThread`, `_DupMigrationThread`, `_PersonsRefreshThread`, `_ResuggestThread`,
    `_ResetWorkerThread`.
  - `export_dialogs.py` (`_ExportDialog`, `_SaveOptionsDialog`), `reset_faces_dialog.py`
    (`_ResetFacesDialog`), `duplicates_popup.py` (`_DuplicatesPopup`), `ui_utils.py` (`fmt_size`).
  - `main_window_faces.py::FacesController` et `main_window_duplicates.py::DuplicatesController`
    — deux **mixins** apportant respectivement toutes les méthodes `_on_*`/`_show_*`/`_start_*`
    liées aux visages et aux doublons. `class MainWindow(QMainWindow, FacesController,
    DuplicatesController)` — `MainWindow` reste la classe unique instanciée ; les mixins
    n'existent que pour répartir les méthodes entre plusieurs fichiers.
- `photo_viewer.py` délègue à `viewer_canvas.py` (`_Canvas` — le widget interactif : recadrage,
  annotations, zoom/pan, `_InlineTextEdit`) et `viewer_pixmaps.py` (pipeline de construction du
  pixmap affiché : `_build_pixmap`, `_build_video_pixmap`, `_build_base_image`,
  `_apply_edit_to_base` — réutilisé tel quel par `slideshow.py`).
- `edit_panel.py` délègue à `edit_sliders.py` (`MarkedSlider`, `EditSlider`), `treatment_dialogs.py`
  (`TreatmentDialog` et ses sous-classes `LuminositeTreatmentDialog`, `CouleursTreatmentDialog`,
  `VignetteTreatmentDialog`, plus `_TREATMENTS`) et `edit_icons.py` (fonctions `_icon_*` dessinant
  chaque icône de bouton en `QPixmap`).

> **Piège rencontré pendant le découpage** : une classe définie *entre* deux méthodes d'une autre
> classe (mise en évidence uniquement à l'extraction) casse silencieusement l'héritage attendu —
> vérifier avec `pyflakes` après toute extraction de module.

---

### `main_window.py` — Orchestrateur principal

`MainWindow` est le **chef d'orchestre** : il instancie tous les composants UI, les relie aux composants library/processing, et répond aux signaux de la sidebar et de la grille. Il hérite aussi de `FacesController` et `DuplicatesController` (voir ci-dessus) pour les méthodes spécifiques aux visages et aux doublons.

**Attributs d'état clés** :

| Attribut | Type | Description |
|---|---|---|
| `_current_photos` | `list[PhotoInfo]` | Photos affichées dans la grille |
| `_current_context` | `str` | Dossier ou contexte actif (`"Toutes les photos"`, `"Favoris"`, un chemin, un nom d'album) |
| `_current_album_id` | `int \| None` | Id de l'album affiché (sinon `None`) — pilote « Retirer de l'album » et le comportement de la touche Suppr dans la grille et la visionneuse |
| `_current_photo_index` | `int` | Index de la photo ouverte dans la visionneuse |

**Flux d'une suppression (touche Suppr / « Effacer le(s) fichier(s)… ») :**

```
delete_requested (grille ou visionneuse)
  → _on_delete_requested(photos)
    → confirmation (thread UI), capture du contexte (viewer, groupes de doublons…)
    → _folder_watcher.notify_self_deletions(paths)   # pas de rescan redondant
    → _DeleteWorkerThread (QThread) :
        unlink par fichier (+ progress) puis, en lot :
        catalog.delete_photos / thumb_cache.invalidate_many / face_db.delete_for_paths
    → _on_delete_finished(...) (thread UI) : grille, albums, groupes de doublons,
      navigation vers le voisin, erreurs
```

Garde de réentrance : un seul `_delete_thread` à la fois (partagé avec la suppression des fichiers corrompus). En **contexte album** (`_current_album_id` non `None`), la touche Suppr émet `remove_from_album_requested` à la place : `Catalog.remove_photos_from_album` supprime uniquement le lien `album_photos`, fichiers et catalogue intacts.

**Flux d'un changement de dossier :**

```
Sidebar.folder_selected.emit(path)
  → MainWindow._on_folder_selected(path)
    → catalog.get_photos_in_folder(path)
    → grid.set_photos(photos)
    → _current_context = path
```

**Flux d'une photo découverte pendant le scan :**

```
ScanThread.photo_discovered.emit(photo)
  → MainWindow._on_photo_discovered(photo)
    → si photo.directory == _current_context (ou "Toutes les photos")
      → ET si photo.path pas déjà dans _current_photos
        → grid.add_photo(photo)
        → _current_photos.append(photo)
```

**Flux d'ouverture de la visionneuse :**

```
grid.photo_activated.emit(photo)
  → MainWindow.show_viewer(photo)
    → is_video = photo.media_type == "video"
    → si video : _left_stack.setCurrentIndex(0)  # sidebar visible
    → si image : _left_stack.setCurrentIndex(1)  # panneau retouche visible
                 _edit_panel.set_photo(photo)
    → _viewer.set_photo(photo, edit)
```

**Gestion du `FolderManagerDialog`** :
- `_open_folder_manager()` : crée et affiche le dialogue, connecte ses signaux.
- `_on_folder_rescan_requested(folder)` : appelle `_start_scan(force=True)` sur ce seul dossier.
- `_on_folder_added_from_manager(folder)` : ajoute le dossier à la config, lance le scan.

---

### `folder_manager_dialog.py` — Gestion des dossiers

`FolderManagerDialog(QDialog)` — ouvert via **Outils › Dossiers…**.

**Signaux** :
- `rescan_requested(str)` — chemin du dossier à re-scanner (force)
- `folder_removed(str)` — dossier retiré de la surveillance
- `folder_added(str)` — nouveau dossier ajouté

**`_FolderRow(QWidget)`** : widget par dossier, affiche statut (✓/✗), chemin, nombre de fichiers, sous-dossiers ignorés. Boutons **Re-scanner** et **Retirer**.

**`_find_ignored_subdirs(folder)`** : retourne les sous-dossiers directs exclus du scan avec leur raison :
- Dossiers cachés (attribut Windows `0x2` ou préfixe `.`) → `"dossier caché"`
- Dossiers nommés `Originals` → `"sauvegarde Picasa"`

---

### `sidebar.py` — Navigation

La sidebar est divisée en deux zones via un `QSplitter` vertical :
- **`_FolderTree`** (`QTreeWidget`) : arborescence avec lazy-loading des sous-dossiers.
- **`_albums_list`** (`QListWidget`) : liste des albums + entrées spéciales.

**Lazy-loading de l'arborescence** :

Chaque nœud reçoit systématiquement un **placeholder** (item vide sans `UserRole`) qui le rend dépliable. À l'expansion (`itemExpanded`), le placeholder est remplacé par les vrais sous-dossiers (`_populate_subfolders`). On ne vérifie **pas** si le dossier a réellement des sous-dossiers (ça coûterait un `os.scandir` par enfant — très lent sur un volume réseau) : un nœud sans sous-dossier se replie simplement à la première expansion.

**Cache session des icônes de personnes** :

`refresh_persons` (rebuild complet de la liste Personnes) ne re-décode plus toutes les vignettes de couverture depuis les photos originales : un cache en mémoire (`_icon_bytes_cache`, clé `(cover_path, cover_bbox)`) fournit instantanément les icônes inchangées, et seul le reste part dans `_FaceIconLoader` (QThread). Le signal `persons_thumbnails_ready` est émis dans **tous** les cas (immédiatement si tout vient du cache) — il sert de gate au démarrage de la détection de doublons dans `main_window.py`.

**Drag & drop** :

`_FolderTree` accepte le type MIME interne `application/x-pixelphoto-paths` (défini dans `thumbnail_grid.py`). Les fichiers systèmes (`text/uri-list`) sont ignorés pour éviter les interférences avec l'OS.

---

### `thumbnail_grid.py` — Grille de vignettes

```
ThumbnailGrid (QScrollArea)
  └─ _GridContainer (QWidget, layout manuel)
       └─ ThumbnailCell × N
            └─ QLabel (image) + _ThumbWorker (thread)
```

**`_GridContainer`** calcule le layout manuellement (`_relayout`) plutôt que d'utiliser un `QGridLayout` ou `QFlowLayout`, pour un contrôle précis du nombre de colonnes en fonction de la largeur du conteneur.

**`ThumbnailCell`** :
- Charge la vignette via `ThumbnailCache.get()` ; si absente, lance un `_ThumbWorker`.
- Le signal `ready` de `_ThumbSignals` communique le `QPixmap` généré vers l'UI (cross-thread safe).
- La référence aux signaux est une `weakref` pour éviter de retenir la cellule après sa destruction.
- Si `photo.media_type == "video"`, `_add_video_badge(pixmap)` superpose un cercle sombre avec le caractère `▶` via `QPainter`.

**Sélection** :
- `_selected: set[str]` contient les paths des photos sélectionnées.
- Click simple → remplace la sélection. Ctrl+click → toggle. Shift+click → plage.

---

### `photo_viewer.py` — Visionneuse

```
PhotoViewer (QWidget)
  ├─ _toolbar (QWidget) — nom fichier, favoris, zoom, bouton play vidéo
  ├─ _Canvas (QWidget)  — rendu image + interactions
  └─ _navbar (QWidget)  — précédente/suivante (s'arrête aux extrémités)
```

**`_build_pixmap(photo, edit)`** (dans `viewer_pixmaps.py`, ré-exporté par `photo_viewer.py` — `slideshow.py` l'importe directement depuis `photo_viewer`) : charge l'image avec Pillow, applique `ImageOps.exif_transpose`, downscale à `_PREVIEW_MAX_PX = 1024 px`, puis applique `ImageAdjuster.apply_all(img, edit)`. Pour les vidéos, délègue à `_build_video_pixmap(path)` qui extrait une frame via `cv2`. `_BaseLoader(QThread)` (défini directement dans `photo_viewer.py`) charge `_build_base_image()` hors du thread UI — nécessaire car `cv2.VideoCapture` peut déclencher des appels COM sur Windows pour certains codecs.

**Navigation sans boucle** : les boutons Précédente/Suivante sont désactivés (`setEnabled(False)`) quand on est à la première/dernière photo. Aucun wrap-around.

**Support vidéo** :
- `set_photo()` : si `photo.media_type == "video"`, affiche `_btn_play_video` et masque le panneau d'édition.
- `_open_in_player()` : `QDesktopServices.openUrl(QUrl.fromLocalFile(path))` — ouvre le lecteur système.
- `_build_video_pixmap()` : `cv2.VideoCapture` → seek 10% → frame BGR→RGB → PIL → `QPixmap`.

**Menu contextuel** :
```python
act_map = menu.addAction("Localiser sur la carte")
act_map.setEnabled(has_gps)   # grisé si pas de GPS
# si GPS disponible : QDesktopServices.openUrl(QUrl("https://www.openstreetmap.org/..."))
```

**Mode recadrage** :

```
EditPanel.crop_mode_requested.emit()
  → PhotoViewer.enter_crop_mode()
    → si edit.crop existant : recharger pixmap SANS crop (image originale)
    → _Canvas.enter_crop(existing_crop)

_Canvas.confirm_crop()
  → crop_confirmed.emit(quad_relative)
  → PhotoViewer._on_crop_confirmed(quad)
  → crop_ready.emit(quad)
  → EditPanel.apply_crop(quad)
    → edit.crop = quad
    → edits_changed.emit(edit)
    → PhotoViewer.update_edit(edit) → _reload_pixmap()
```

Le quadrilatère de recadrage est toujours stocké en **coordonnées relatives à l'image originale** (0–1). `_crop_to_rel()` / `_crop_from_rel()` convertissent entre coordonnées écran et relatives.

---

### `exif_panel.py` — Panneau EXIF

`ExifPanel(QWidget)` — affiché dans la visionneuse, togglé par la touche `I` ou un bouton toolbar.

- Affiche : résolution, taille du fichier, date, appareil photo, objectif, ISO, vitesse, ouverture, focale, coordonnées GPS.
- Pour les vidéos, affiche : résolution, fps, durée — via `cv2.VideoCapture` ou les champs `PhotoInfo`.
- **Exclusion mutuelle** avec le panneau Visages : ouvrir l'un ferme l'autre.

---

### `edit_panel.py` — Panneau de retouche

**Structure UI** :

```
EditPanel
  ├─ Barre titre + boutons ↩ ↪ (undo/redo)
  └─ QScrollArea
       ├─ QGridLayout (6 boutons corrections : 2 colonnes)
       │    Luminosité · Contraste · Saturation · Gamma · Netteté · Débruitage
       ├─ Bouton Couleurs (N&B + mixage R/G/B)
       └─ QGroupBox "Géométrie"
            ├─ Rotation ↺ ↻
            ├─ Redresser | Recadrer
            └─ Miroir H | Miroir V
```

**`TreatmentDialog`** (dans `treatment_dialogs.py`) : dialogue modal avec un ou plusieurs `EditSlider` (`edit_sliders.py`). L'aperçu est en temps réel via `preview.emit(EditInfo)` → `PhotoViewer.update_edit()`. Si l'utilisateur annule, l'`EditInfo` original est restauré.

**Couleurs (N&B)** : `TreatmentDialog` avec checkbox `bw` + trois `EditSlider` pour `bw_red`, `bw_green`, `bw_blue`. La checkbox active/désactive les sliders.

**Pile undo/redo** :

- `_undo_stack` et `_redo_stack` : listes d'`EditInfo` en mémoire (max 20).
- Chaque opération (`_push_undo`) sauvegarde également dans `edit_db` via `_save()`.
- À l'ouverture d'une photo, `get_history()` recharge jusqu'à 20 états depuis la DB → undo persistant entre sessions.

**`reset_all()` / `restore_all()`** : `reset_all()` supprime toutes les retouches sans confirmation
(l'ancien `QMessageBox.question` a été retiré) mais conserve un instantané dans
`self._reset_snapshots: dict[str, EditInfo]` (clé = chemin normalisé) avant de vider l'état.
`restore_all()` réapplique cet instantané. Le snapshot est invalidé (`.pop(...)`) dès qu'une
nouvelle retouche est poussée via `_push_undo()` après le reset, pour qu'un `restore_all()`
tardif n'écrase jamais silencieusement une retouche faite entre-temps.

**Ordre d'application des retouches** (dans `ImageAdjuster.apply_all`) :

```
1. Rotation (multiples de 90°)
2. Redressement (avec recadrage automatique des bords)
3. Miroir H / V
4. Recadrage (crop)
5. Conversion N&B (si activée)
6. Luminosité → Contraste → Saturation → Gamma → Netteté → Débruitage
```

Cet ordre est figé par conception : modifier l'ordre changerait le résultat visuel pour toutes les photos existantes.

---

### `thread_journal_dialog.py` — Journal des threads (Outils › Journal des threads…)

Lit `src/core/thread_journal.py::journal.get_entries()`. Onglets : bilan d'exécution
(`_CompteRenduPanel`) avec statut par thread (✓ OK / ● LENT / ● TROP LONG / ✗ ERREUR / ● EN COURS),
résumé (`_SummaryTable`), événements bruts filtrables (`_EventTable`, mode **▶ Temps réel**), et
un `_ProblemsReportDialog` générant un diagnostic texte copiable.

### `help_dialog.py` — Aide / À propos

`HelpDialog(QDialog)` charge le contenu de chaque onglet depuis `src/ui/help_content/*.html`
(un fichier par onglet + `_style.html` partagé) plutôt que de l'avoir en dur dans le code Python —
objectif : pouvoir corriger l'aide sans toucher au code. `_load_tab_html()` résout le dossier via
`sys._MEIPASS / "help_content"` en mode figé (PyInstaller, cf. §14 Packaging) ou
`Path(__file__).parent / "help_content"` en développement, et substitue `__VERSION__` /
`__VERSION_CHECK__`. **Toute modification du contenu d'aide passe par ces fichiers HTML, jamais
par du texte en dur dans `help_dialog.py`.**

Barre de recherche (`self._search_edit`, `HelpDialog._search()`) : cherche dans l'onglet
`QTextBrowser` courant via `QTextBrowser.find()` (Qt, insensible à la casse et **substring**, cf.
piège rencontré en test — "ORB" matche aussi "c**orb**eille") puis, si absent, dans les onglets
suivants par ordre circulaire à partir de l'onglet courant — bascule automatiquement dessus si
trouvé (`tabs.setCurrentIndex`). `continue_search=True` (Entrée, `returnPressed`) poursuit depuis
la position du curseur ; toute autre frappe (`textChanged`) repart du début de l'onglet affiché.
Raccourci `Ctrl+F` (`QShortcut(QKeySequence.Find, self)`, portée fenêtre — n'entre pas en conflit
avec le `Ctrl+F` global de la fenêtre principale car `HelpDialog` est modal) donne le focus au
champ. Aucun résultat dans aucun onglet → `_SEARCH_NOT_FOUND_STYLE` (fond rouge) sur le champ.

---

## 7. Composants processing

### `adjustments.py` — `ImageAdjuster`

Classe stateless de méthodes statiques. Opère sur des objets `PIL.Image`.

| Méthode | Implémentation PIL |
|---|---|
| `apply_brightness(v)` | `ImageEnhance.Brightness(factor = 1 + v)` |
| `apply_contrast(v)` | `ImageEnhance.Contrast(factor = 1 + v)` |
| `apply_saturation(v)` | `ImageEnhance.Color(factor = 1 + v)` |
| `apply_gamma(g)` | LUT 256 entrées : `(i/255)^(1/g) * 255` |
| `apply_sharpness(v)` | `ImageEnhance.Sharpness(factor = 1 + v)` |
| `apply_noise_reduction(v)` | `ImageFilter.GaussianBlur(radius = v * 2)` |
| `apply_bw(r, g, b)` | NumPy : pondération RGB → canal L, reconverti RGB |

### `geometry.py` — `GeometryProcessor`

Transformations géométriques via Pillow :
- `apply_rotation(img, angle)` : rotations en multiples de 90°.
- `apply_straighten_with_crop(img, angle_deg)` : rotation libre puis recadrage automatique pour éliminer les bords noirs.
- `apply_flip(img, flip_h, flip_v)` : `Image.FLIP_LEFT_RIGHT` / `Image.FLIP_TOP_BOTTOM`.
- `apply_crop(img, quad_tuple)` : recadrage par les 8 coordonnées relatives.

### `edit_database.py` — `EditDatabase`

Interface d'accès à `edits.db`. Thread-safe (verrou par opération).

```python
db = EditDatabase()
edit = db.load(photo_path)             # EditInfo (vierge si absent)
db.save(photo_path, edit, operation)   # INSERT OR REPLACE + historique
db.rename_photo(old_path, new_path)    # UPDATE les deux tables
db.delete(photo_path)                  # supprime état + historique
history = db.get_history(path, limit=20)  # list[EditInfo] ancien→récent
```

Tous les chemins sont normalisés avec `os.path.normpath` à l'entrée de chaque méthode.

---

## 8. Reconnaissance faciale

> Module optionnel — dépendances lourdes (InsightFace/ONNXRuntime, scikit-learn, hdbscan). `scikit-learn`
> et `hdbscan` sont cependant des dépendances **non optionnelles** du cœur applicatif (ne jamais les
> exclure du packaging PyInstaller, cf. §14) ; seul InsightFace lui-même (détection/embedding) est
> réellement facultatif — sans lui, les fonctionnalités de visages sont désactivées mais l'app démarre.

### Architecture

```
faces/
├── detector.py         # Détection + embedding via InsightFace (buffalo_l : SCRFD-10GF + ArcFace R100)
├── clusterer.py         # Clustering HDBSCAN + purification (scission des clusters impurs)
├── face_database.py     # Base SQLite faces.db — visages, clusters, personnes, suggestions
├── face_indexer.py      # Threads d'indexation en masse / réindexation / nouvelle tentative
├── gpu_utils.py          # Détection du backend GPU disponible pour l'inférence ONNXRuntime
└── picasa_importer.py    # Import annotations + retouches Picasa (.picasa.ini / contacts.xml)
```

Panneaux et dialogues UI (`src/ui/`) : `face_panel.py` (panneau visages de la visionneuse),
`people_panel.py` (vue « Personnes », groupes non identifiés + assignation), `person_cluster_view.py`
(vue détaillée d'une personne), `face_cluster_grid.py` + `face_cluster_cards.py` +
`face_cluster_workers.py` (grille des groupes non identifiés), `face_merge_dialog.py`,
`face_backup_dialog.py`, `face_counters_dialog.py`, `picasa_import_dialog.py`, `reset_faces_dialog.py`.
Orchestration côté `MainWindow` : mixin `main_window_faces.py::FacesController` (voir §6).

### `detector.py` — Détection et embedding

```python
from src.faces.detector import is_available, detect_and_embed

if is_available():
    faces = detect_and_embed(image_path, rotation=0)
    # → [{'bbox': (x, y, w, h), 'embedding': list[float] (512D), 'det_score': float}, ...]
```

`_get_insight_app()` maintient un singleton `FaceAnalysis` par process (coûteux à charger). Les
détections avec `det_score < 0.5`, `embedding is None`, ou `w < 20`/`h < 20` px sont **exclues
définitivement** (jamais écrites en base) — voir la mise en garde dans `CLAUDE.md` : ne pas y
ajouter de seuil d'aire relatif à l'image, ça a déjà supprimé silencieusement des visages valides
sans rattrapage possible.

### `face_database.py` — `FaceDatabase` (faces.db)

Même pattern de connexion que `Catalog` (thread-local, PRAGMAs WAL, garde
`except BaseException: conn.rollback(); raise` sur les écritures — voir §5/CLAUDE.md).

**Paliers de confiance de la reconnaissance** (comparaison cosinus visage/centroïde ↔ centroïde
personne, appliqués par `set_cluster_suggestions()`, point d'entrée unique pour les 4 producteurs
de suggestions) :

| Seuil | Valeur | Effet |
|---|---|---|
| `_SIM_SUGGEST` | 0.60 | Suggestion enregistrée (`suggestion_person_id`/`suggestion_score`) — à confirmer manuellement |
| `_SIM_AUTO_ASSIGN` | 0.70 | Allocation automatique de la personne, sans confirmation |
| `_SIM_STRONG` (`people_panel.py`, affichage seul) | 0.55 | Libellé bleu « Probablement X » vs gris « Peut-être X » (`_SIM_WEAK` = 0.50) |
| `_SIM_GROUP` (`people_panel.py`) | 0.72 | Auto-groupement de clusters *non identifiés* entre eux |

`set_cluster_suggestions()` est idempotent dans les deux branches (`WHERE person_id IS NULL AND
suggestion_person_id IS NULL`) : un cluster déjà assigné ou déjà suggéré n'est jamais réécrit.

**Filtrage par taille** (deux étages, à ne pas confondre) : `detect_and_embed()` exclut
définitivement (`w`/`h` < 20 px, cf. ci-dessus) ; `save_faces()` marque ensuite `ignored=1`
(conservé en base, masqué de l'UI/clustering, **récupérable**) selon un seuil proportionnel à la
résolution de la photo (`_AUTO_IGNORE_MIN_SIDE_RATIO=0.03`, `_AUTO_IGNORE_MIN_SIDE_FG_RATIO=0.20`,
`_AUTO_IGNORE_FG_FRACTION=0.25`, `_AUTO_IGNORE_MIN_SCORE=0.65`).

**Cache des centroïdes personne** : `get_all_person_centroids()` décode jusqu'à ~60k embeddings
(512D float32, `numpy.frombuffer` plutôt que `struct.unpack`, ~10× plus rapide) — coûteux sur une
grosse bibliothèque, donc mis en cache (`self._person_centroid_cache`) et invalidé seulement quand
un fingerprint bon marché (`SELECT COUNT(*), SUM(person_id) FROM faces WHERE person_id IS NOT NULL`)
change. `enrich_persons()` (photo_count + cover_path/cover_bbox + pending_count) est également
coûteux (~1 s) ; `enrich_persons_photo_count()` en est une variante allégée à préférer quand la
couverture n'est pas affichée.

**Méthodes principales** :

```python
db = FaceDatabase()
db.save_faces(photo_path, detections, rotation=0, force_no_limit=False)
db.get_all_person_centroids(person_ids) -> dict[int, list[float]]
db.set_cluster_suggestions(suggestions)              # {cluster_id: (person_id, score)}
db.merge_persons(keep_id, remove_id)                  # réassigne faces + picasa_annotations, dédup
db.delete_for_path(photo_path) / delete_for_paths(paths)
db.update_clusters(face_ids, labels, progress_cb=None)
db.resuggest_clusters(cluster_ids, exclude_person_id=None)
db.accept_cluster_suggestion(cluster_id)
db.reset_clustering()   # garde les visages détectés, refait juste le regroupement
db.reset_index()        # efface tout, à réindexer depuis zéro
```

### `clusterer.py` — Clustering HDBSCAN

`ClusterThread(QThread)` lance `_run_clustering()` dans un `_clustering_worker_proc` séparé
(timeout `_CLUSTER_TIMEOUT = 1800 s`). Réduction dimensionnelle PCA (`_PCA_DIMS = 32`) avant
HDBSCAN. `_purify_clusters()` scinde les clusters HDBSCAN mixtes via une liaison complète sur les
paires impures (`_PURITY_MIN_SIM = 0.60`, plafonné à `_PURITY_MAX_CLUSTER_N = 2000` pour rester
tractable). `reset_clustering_cache()` invalide le sentinel « dernier N traité » — **doit être
appelé après tout `reset_clustering()`/`reset_index()`**, sans quoi les visages restent bloqués
avec `cluster_id = NULL` (bug déjà rencontré, trouvé par un scénario e2e).

### Import Picasa

`PicasaImporter` (`picasa_importer.py`) lit les fichiers `.picasa.ini` (et `contacts.xml`) dans
chaque dossier scanné. Il parse les sections `[contacts]` (hash → nom) et les entrées `rect=`
(coordonnées de visage encodées en base64, `_decode_rect64` puis conversion vers coordonnées EXIF
via `_bbox_raw_to_exif`), et peut aussi convertir les édits Picasa (rotation, recadrage,
luminosité…) en `EditInfo` (`_picasa_to_edit_info`).

Les annotations en attente sont stockées dans **`faces.db`**, table `picasa_annotations` (et non
`catalog.db`), avec un flag `consumed` — `save_faces()` les consulte pour ré-appliquer une
identification Picasa dès qu'un visage correspondant (IoU ≥ `_IOU_THRESHOLD = 0.30`) est
(ré)indexé, sans jamais écraser une association déjà faite manuellement.

---

## 9. Détection de doublons

### Architecture

```
library/
├── duplicate_detector.py   # DuplicateDetectorThread — Tier 1 pHash + Tier 2 ORB/RANSAC
├── dedup_cache.py           # DedupCache — cache incrémental (dedup_cache.db)
└── file_repair.py           # FileRepairThread — réparation des fichiers corrompus détectés
```

Orchestration côté `MainWindow` : mixin `main_window_duplicates.py::DuplicatesController` (voir §6).
UI dédiée : `src/ui/duplicate_grid.py` (grille des groupes, bouton « Dupliquées » de la sidebar),
`src/ui/duplicates_popup.py` (popup « Doublons de cette photo » depuis le badge ⧉).

### `DuplicateDetectorThread` — les deux passes

```python
thread = DuplicateDetectorThread(
    photo_paths,
    seed_groups=catalog.get_duplicate_group_assignments(),  # {path: group_id} — TOUJOURS repasser
    cache_db_path=...,             # dedup_cache.db
    dates=catalog.get_photo_dates_for_dedup(),  # {path: datetime|None}
)
thread.progress.connect(...)        # (int, int, str)
thread.partial_results.connect(...)  # ({group_id: [paths]}, [corrupted_paths]) — snapshot live
thread.finished.connect(...)        # dict — object, PAS dict : QVariantMap exige des clés str
```

> **Piège** (déjà rencontré, cf. `CLAUDE.md`) : relancer le thread sur un `cache_db_path` déjà
> peuplé **sans repasser `seed_groups`** fait que toutes les paires apparaissent comme « déjà
> comparées » (`compared_tier1`/`compared_tier2`) → aucun groupe reformé, retour silencieux de
> `{}`. En usage réel `seed_groups` est toujours récupéré frais avant chaque création de thread ;
> seul un script relançant `_detect()` plusieurs fois sur le même cache doit y penser explicitement.

**Tier 1 — pHash** (`_HASH_THRESHOLD=10`, `_HASH_MICRO_SIZE=8`, `_HASH_PIXEL_MAX_DIFF=0.34`) :
empreinte perceptuelle + micro-vignette 8×8 normalisée par photo, calculées en parallèle
(`ThreadPoolExecutor(max_workers=throttled_worker_count())`, cf. §4 `cpu_throttle.py`), mises en
cache dans `DedupCache`, puis union-find incrémental (`_merge`) — les paires ancien×ancien déjà
dans `compared_tier1` ne sont jamais recomparées.

**Tier 2 — ORB + RANSAC** (`_ORB_MIN_INLIERS=40`, `_ORB_MAX_KP=300`, `_ORB_RATIO_TEST=0.75`,
`_ORB_LOAD_SIZE=800`) : uniquement sur les photos non groupées par le Tier 1, détecte les
recadrages via `knnMatch` + `cv2.findHomography(RANSAC)` puis vérification du diff pixel sur la
zone de recouvrement recalée.

**Exclusion par date EXIF** (`_dates_differ`) : deux photos dont les dates EXIF sont connues et
différentes ne sont **jamais** fusionnées, même visuellement identiques (rafale) — un groupe déjà
formé avant l'ajout de cette règle n'est pas défait rétroactivement.

### `dedup_cache.py` — `DedupCache` (dedup_cache.db)

Cache l'intégralité du travail coûteux pour rendre les passes suivantes incrémentales : empreintes
(`fingerprints`), features ORB (`orb_features`), paires déjà comparées (`compared_tier1`/`_tier2`)
et fichiers illisibles rencontrés (`corrupted_files`) — voir schéma complet en §11.

```python
cache = DedupCache(db_path)
cache.get_fingerprints(paths) / store_fingerprints(rows)
cache.get_compared_tier1(paths) / store_compared_tier1(rows)
cache.remove_compared(paths)      # invalide tier1+tier2 pour ces chemins (migration date EXIF)
cache.replace_corrupted_paths(paths) / get_corrupted_paths()
cache.purge_missing(keep_paths)   # nettoie toutes les tables des chemins disparus de la bibliothèque
```

### `file_repair.py` — Réparation des fichiers corrompus

`FileRepairThread(QThread)` essaie trois décodeurs tolérants dans l'ordre
(`_decode_truncated_pil`, `_decode_qimage`, `_decode_cv2_truncated`) plus un correctif
non-destructif du marqueur JPEG EOI (`_decode_strict_with_eoi_fix`), retient le meilleur candidat
via une heuristique d'écart-type par ligne (`_usable_height`), sauvegarde l'original dans
`.tmp_originals/` avant d'écraser (`_backup_before_repair`), puis restaure les dates Windows
(`preserve_file_dates`, cf. `exif_reader.py`).

### Persistance et dissolution des groupes

`Catalog` (`catalog.py`) porte la colonne `photos.duplicate_group_id`. `ignore_duplicate_group(group_id)`
(bouton ✕ de la grille) est **persistant** : un groupe ignoré n'est plus jamais recréé tant
qu'aucun de ses membres ne change, car ses paires restent dans `compared_tier1`/`_tier2`.
`_dissolve_singleton_duplicate_groups(conn)` s'auto-déclenche après toute mutation de groupe pour
éviter les groupes résiduels à un seul exemplaire — appelé depuis **tous** les chemins de
suppression (`delete_photo`/`delete_photos`), pas seulement le flux UI de la grille de doublons.

---

## 10. Système de plugins

### Structure d'un plugin

```
plugins/mon_plugin/
├── plugin.json     # Manifest
└── plugin.py       # Code
```

**`plugin.json`** :

```json
{
  "id": "mon_plugin",
  "name": "Mon Plugin",
  "version": "1.0.0",
  "description": "...",
  "author": "...",
  "entry_point": "plugin.py"
}
```

**`plugin.py`** — doit définir une classe héritant de `BasePlugin` :

```python
from src.core.base_plugin import BasePlugin

class MonPlugin(BasePlugin):
    def activate(self) -> None:
        self.bus.on("library.photo_selected", self._on_photo)

    def deactivate(self) -> None:
        self.bus.off("library.photo_selected", self._on_photo)

    def _on_photo(self, photo):
        ...
```

### Dossiers de recherche (dans l'ordre)

1. `plugins/` (racine du projet — plugins utilisateur)
2. `src/plugins/` (plugins intégrés)
3. `%LOCALAPPDATA%\PixelPhotoManager\plugins\` (plugins installés globalement)

### Chargement

```python
manager = PluginManager(config)
manager.discover()           # lit les plugin.json
manager.activate("mon_plugin")   # charge via importlib, appelle activate()
manager.deactivate("mon_plugin")
```

### Classes de base disponibles

| Classe | Module | Usage |
|---|---|---|
| `BasePlugin` | `src.core.base_plugin` | Plugin générique |
| `ProcessorPlugin` | `src.core.processor_plugin` | Traitement image (impl. `process(image, params)`) |

---

## 11. Schémas des bases de données

### `catalog.db`

```sql
CREATE TABLE photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT UNIQUE NOT NULL,   -- chemin absolu normalisé
    filename    TEXT,
    directory   TEXT,                  -- os.path.dirname(path)
    date_taken  TEXT,                  -- ISO 8601 ou NULL
    width       INTEGER,
    height      INTEGER,
    file_size   INTEGER,
    file_mtime  REAL,                  -- st_mtime (float Unix)
    camera_make TEXT,
    camera_model TEXT,
    lens_model  TEXT,
    iso         INTEGER,
    exposure_time TEXT,
    aperture    REAL,
    focal_length REAL,
    has_gps     INTEGER DEFAULT 0,
    gps_lat     REAL,
    gps_lon     REAL,
    is_favorite INTEGER DEFAULT 0,
    tags        TEXT,                  -- CSV "tag1,tag2"
    indexed_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    media_type  TEXT DEFAULT 'image',  -- 'image' ou 'video'
    duration    REAL DEFAULT 0.0,      -- durée en secondes (vidéos)
    duplicate_group_id INTEGER         -- NULL si non dupliquée, cf. §9
);
CREATE INDEX idx_photos_dup_group ON photos(duplicate_group_id);

CREATE TABLE albums (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE album_photos (
    album_id    INTEGER,
    photo_id    INTEGER,
    PRIMARY KEY (album_id, photo_id)
);

CREATE TABLE persons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
```

> Les colonnes `media_type`, `duration` et `duplicate_group_id` sont ajoutées par migration automatique au démarrage si elles n'existent pas (`_migrate_video_fields()`, migration doublons). **Note** : la table `persons` ci-dessus est la table de référence des personnes identifiées (id, name) — `faces.db` (ci-dessous) s'y réfère par `person_id` mais ne la duplique pas. Les visages, clusters et annotations Picasa (`picasa_annotations`), en revanche, vivent entièrement dans `faces.db`, jamais dans `catalog.db`.

### `thumbnails.db`

```sql
CREATE TABLE thumbnails (
    photo_hash      TEXT PRIMARY KEY,   -- MD5(os.path.normpath(path))
    photo_path      TEXT,               -- chemin pour référence humaine
    file_mtime      REAL,               -- mtime lors de la génération
    thumbnail_data  BLOB,               -- JPEG 220×220 max, qualité 85
    generated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
```

La clé est le **hash MD5 du chemin normalisé**. La correspondance `abs(stored_mtime - current_mtime) < 1.0` invalide la vignette si le fichier a changé.

### `edits.db`

```sql
CREATE TABLE photo_edits (
    photo_path      TEXT PRIMARY KEY,   -- chemin normalisé
    brightness      REAL DEFAULT 0.0,
    contrast        REAL DEFAULT 0.0,
    saturation      REAL DEFAULT 0.0,
    gamma           REAL DEFAULT 1.0,
    sharpness       REAL DEFAULT 0.0,
    noise_reduction REAL DEFAULT 0.0,
    rotation        REAL DEFAULT 0.0,
    straighten      REAL DEFAULT 0.0,
    flip_h          INTEGER DEFAULT 0,
    flip_v          INTEGER DEFAULT 0,
    crop            TEXT DEFAULT NULL,  -- JSON "[x0,y0,x1,y1,x2,y2,x3,y3]"
    bw              INTEGER DEFAULT 0,
    bw_red          REAL DEFAULT 0.0,
    bw_green        REAL DEFAULT 0.0,
    bw_blue         REAL DEFAULT 0.0,
    modified_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path  TEXT NOT NULL,
    state_json  TEXT NOT NULL,    -- JSON complet de l'EditInfo
    operation   TEXT NOT NULL,    -- "edit", "rotation", "crop", "undo", "redo"…
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_history_path ON edit_history(photo_path, id DESC);
```

L'historique est **limité à 50 entrées par photo** (nettoyage dans `EditDatabase.save()`). À l'ouverture d'une photo, les 20 entrées les plus récentes sont chargées en mémoire.

### `faces.db`

```sql
CREATE TABLE IF NOT EXISTS indexed_photos (
    photo_path TEXT PRIMARY KEY,
    indexed_at REAL NOT NULL,
    face_count INTEGER DEFAULT 0,
    rotation   INTEGER DEFAULT 0        -- migration
);

CREATE TABLE IF NOT EXISTS faces (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path           TEXT NOT NULL,
    bbox_x               INTEGER NOT NULL,
    bbox_y               INTEGER NOT NULL,
    bbox_w               INTEGER NOT NULL,
    bbox_h               INTEGER NOT NULL,
    embedding            BLOB,               -- 512D float32, cf. get_all_person_centroids()
    cluster_id           INTEGER,            -- NULL tant que non (re)clusterisé (HDBSCAN)
    person_id            INTEGER,            -- FK logique vers catalog.db::persons.id
    ignored              INTEGER DEFAULT 0,  -- migration — filtrage taille, cf. §8
    pinned                INTEGER DEFAULT 0,  -- migration — épinglé comme couverture
    is_cover              INTEGER DEFAULT 0,  -- migration
    suggestion_person_id INTEGER DEFAULT NULL,  -- migration — cf. _SIM_SUGGEST
    suggestion_score      REAL DEFAULT NULL,     -- migration
    det_score             REAL DEFAULT 1.0       -- migration — score de détection InsightFace
);
CREATE INDEX idx_faces_person ON faces(person_id);       -- fingerprint centroïdes, cf. §8
CREATE INDEX idx_faces_suggestion ON faces(suggestion_person_id);  -- créé APRÈS les migrations

-- Annotations en attente importées depuis Picasa (.picasa.ini / contacts.xml)
CREATE TABLE IF NOT EXISTS picasa_annotations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path TEXT NOT NULL,
    bbox_x     INTEGER NOT NULL,
    bbox_y     INTEGER NOT NULL,
    bbox_w     INTEGER NOT NULL,
    bbox_h     INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    consumed   INTEGER DEFAULT 0        -- consommée dès qu'un visage détecté matche (IoU ≥ 0.30)
);

CREATE TABLE IF NOT EXISTS face_index_errors (
    photo_path   TEXT PRIMARY KEY,
    error_type   TEXT NOT NULL,
    last_attempt REAL NOT NULL,
    excluded     INTEGER DEFAULT 0
);
```

> Piège de migration (cf. `CLAUDE.md`) : `idx_faces_suggestion` doit être créé **après** les
> migrations dans `_init_db` — `suggestion_person_id` n'existe pas dans le `CREATE TABLE` initial,
> seulement via `ALTER TABLE`.

### `dedup_cache.db`

```sql
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS fingerprints (       -- Tier 1 (pHash)
    path       TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL,
    phash_hex  TEXT NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    micro      BLOB NOT NULL                     -- micro-vignette 8×8 normalisée
);

CREATE TABLE IF NOT EXISTS orb_features (        -- Tier 2 (ORB + RANSAC)
    path         TEXT PRIMARY KEY,
    file_mtime   REAL NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    keypoints_xy BLOB NOT NULL,
    descriptors  BLOB NOT NULL,
    image_jpeg   BLOB NOT NULL                    -- image réduite (≤ 800px), pour le diff pixel final
);

CREATE TABLE IF NOT EXISTS compared_tier1 (      -- chemins déjà comparés à tout le reste (Tier 1)
    path       TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS compared_tier2 (      -- idem, Tier 2
    path       TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS corrupted_files (
    path        TEXT PRIMARY KEY,
    detected_at REAL NOT NULL
);
```

> C'est la présence dans `compared_tier1`/`compared_tier2` qui rend la détection de doublons
> incrémentale (cf. §9) : seules les paires nouveau×ancien et nouveau×nouveau sont réévaluées,
> jamais ancien×ancien.

---

## 12. Modèle de threading

```
Thread UI (main)
  ├─ QApplication.exec()
  ├─ All Qt widgets
  └─ Signals/Slots cross-thread (Qt::QueuedConnection automatique)

Thread scan (ScanThread / QThread)
  ├─ os.walk + EXIF/vidéo reading (ExifReader / VideoMetadataReader)
  ├─ catalog.add_or_update_photo()   ← verrou threading.Lock
  └─ photo_discovered.emit()         → reçu dans le thread UI

ThreadPool (QThreadPool global)
  └─ _ThumbWorker (QRunnable) × N   ← génération vignettes images et vidéos
       ├─ image : PIL.Image.open + resize
       ├─ vidéo : cv2.VideoCapture + frame extraction
       ├─ thumbnail_cache.generate()  ← verrou threading.Lock dédié (seule classe à en avoir un,
       │                                 cf. §5 — évite la contention SQLite constatée avec 4+2
       │                                 threads de vignettes actifs en parallèle de l'indexation
       │                                 visages)
       └─ _ThumbSignals.ready.emit()  → reçu dans ThumbnailCell (UI)

FaceIndexThread / SingleFaceReindexThread / ForceRedetectThread (QThread, face_indexer.py)
  ├─ detector.detect_and_embed()  ← InsightFace (CPU/GPU, cf. gpu_utils.py)
  └─ face_database.save_faces()   → faces.db, connexion thread-local

ClusterThread (QThread, clusterer.py)
  └─ _run_clustering() dans un process séparé (_clustering_worker_proc)
       ├─ ThreadPoolExecutor/ProcessPoolExecutor initialisés via cpu_throttle.py
       │    (throttled_worker_count() ≈ 15 % des cœurs, priorité OS abaissée)
       └─ HDBSCAN + purification → face_database.update_clusters()

DuplicateDetectorThread (QThread, duplicate_detector.py)
  ├─ Tier 1 pHash : ThreadPoolExecutor(max_workers=throttled_worker_count())
  ├─ Tier 2 ORB+RANSAC : idem, uniquement sur les photos non groupées au Tier 1
  ├─ dedup_cache (DedupCache)  ← connexion thread-local dédiée à ce thread
  └─ partial_results.emit() / finished.emit()  → reçus dans DuplicatesController (UI)
```

**Règles** :
1. Ne jamais modifier un widget Qt depuis un thread secondaire.
2. Utiliser uniquement des signaux PySide6 pour communiquer vers l'UI depuis un thread.
3. `Catalog`, `FaceDatabase`, `ThumbnailCache`, `EditDatabase` utilisent le pattern **connexion
   SQLite par (instance, thread)** (`threading.local`, PRAGMAs WAL posés une fois) plutôt qu'un
   verrou partagé — voir CLAUDE.md « Pattern de connexion ». Seul `ThumbnailCache` ajoute en plus
   un `threading.Lock()` explicite autour de ses écritures (cf. ci-dessus). Toute méthode
   d'écriture doit reprendre la garde `except BaseException: conn.rollback(); raise` pour ne
   jamais laisser la connexion cachée dans une transaction ouverte.
4. `_ThumbSignals` est un `QObject` distinct par worker, pas partagé, pour éviter les race conditions sur la destruction.
5. Les tâches de fond continues (indexation visages, clustering, détection de doublons) passent
   par `cpu_throttle.py` pour ne pas saturer la machine pendant que l'utilisateur travaille, et
   journalisent leur activité via `thread_journal.py` (cf. §4, `Outils › Journal des threads…`).

---

## 13. Normalisation des chemins Windows

### Le problème

Sur Windows, `QFileDialog` retourne des chemins avec `/` (`D:/Photos`). `os.path.join` et `os.walk` peuvent produire des chemins mixtes (`D:/Photos\SubFolder`). `str(Path(...))` normalise vers `\`. Ces incohérences cassent les comparaisons de chaînes dans SQLite (comparaison exacte).

### La solution

**`os.path.normpath`** est appliqué à tous les chemins aux points d'entrée :

| Point d'entrée | Où |
|---|---|
| Création de `PhotoInfo` | `PhotoInfo.__post_init__` |
| Scan de fichiers | `scanner.py` : `os.path.normpath(os.path.join(root, fname))` |
| Requêtes catalogue | `catalog.get_photos_in_folder()`, `get_known_mtimes()`, `count_photos_in_folder()` |
| Écritures catalogue | `catalog.move_photo()` (old_path et new_path) |
| Écritures edits | `edit_database.py` : toutes les méthodes publiques |
| Glisser-déposer | `main_window._on_photos_dropped()` : `os.path.normpath(os.path.join(...))` |

**Migration** : `Catalog._migrate_normalize_paths()` et `EditDatabase._migrate_normalize_paths()` normalisent les données existantes au démarrage, avec gestion des doublons (UNIQUE constraint).

---

## 14. Packaging et distribution

### Prérequis

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
```

### Build

```powershell
.\build.ps1                    # EXE + MSI (demande le numéro de version, tag git proposé par défaut)
.\build.ps1 -Version 1.1.0     # EXE + MSI, version fournie directement (pas de prompt)
.\build.ps1 -ExeOnly           # EXE uniquement
.\build.ps1 -MsiOnly           # MSI uniquement (EXE déjà construit, VERSION déjà à jour)
```

### Numéro de version

Le fichier `VERSION` à la racine du dépôt (non versionné, régénéré à chaque build) est la
source unique de vérité pour le numéro de version :

- `build.ps1` le (re)génère : sans `-Version`, il propose le dernier tag git du dépôt
  (`git tag --sort=-v:refname`, indépendant de la branche courante — pas `git describe`,
  qui échoue si le tag n'est pas un ancêtre de HEAD) comme valeur par défaut.
- `pixelphotomanager.spec` l'embarque dans l'exécutable (`datas`) ; `src/core/app_version.py::get_app_version()`
  le lit en mode figé (`sys._MEIPASS / "VERSION"`), et retombe sur `git describe` en mode développement.
- `installer\build_msi.ps1` le lit pour renseigner `Product/@Version` dans `installer/product.wxs`.

`src/core/update_checker.py::UpdateCheckThread` compare cette version à la dernière release
GitHub publiée (`api.github.com/repos/Christian73/PixelPhotoManager/releases/latest`, sans
authentification). Utilisé au démarrage (`main_window.py`, notification silencieuse sauf si
une mise à jour est disponible) et dans l'onglet **À propos** de l'aide (`help_dialog.py`,
qui affiche aussi les états « à jour » et « erreur »). Quatre statuts : `STATUS_UPDATE_AVAILABLE`,
`STATUS_UP_TO_DATE`, `STATUS_ERROR` (réseau/API), `STATUS_VERSION_UNKNOWN` (version locale non
sémantique, typiquement un hash git en mode développement — à ne pas confondre avec une erreur réseau).

### Sortie

```
dist\PixelPhotoManager\
├─ PixelPhotoManager.exe       # Exécutable principal
├─ _internal\                  # Runtime Python + DLLs Qt/Pillow/OpenCV
└─ ...
```

Taille typique : **300–450 Mo** (Qt + PySide6 représentent la majorité).

### Spec `pixelphotomanager.spec`

Points clés du spec :

```python
# collect_all : packages avec ressources intégrées
for pkg in ("PIL", "folium", "reportlab"):
    d, b, h = collect_all(pkg)
    ...

a = Analysis(
    ["main.py"],
    hooksconfig={
        "PySide6": {
            "qt_plugins": ["platforms", "imageformats", "styles", "iconengines"],
        },
    },
    excludes=["deepface", "torch", "tensorflow", "sklearn", "tkinter", ...],
)

exe = EXE(..., console=False, upx=False, ...)   # GUI, sans UPX
```

**`console=False`** : pas de fenêtre console (application graphique). Les erreurs sont dans le log (`%LOCALAPPDATA%\PixelPhotoManager\logs\`).

**`upx=False`** : UPX désactivé pour éviter les faux positifs antivirus.

**`collect_all("PIL")`** : indispensable pour inclure les plugins d'image Pillow (JPEG, PNG, TIFF, WebP) qui sont chargés dynamiquement.

### Logs en mode frozen

Dans `main.py`, la cible des logs est adaptée selon le mode d'exécution :

```python
if getattr(sys, "frozen", False):
    # EXE PyInstaller → %LOCALAPPDATA%\PixelPhotoManager\logs\
    _LOG_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home())) \
                / "PixelPhotoManager" / "logs" / "pixelphotomanager.log"
else:
    # Développement → logs/ à côté de main.py
    _LOG_PATH = Path(__file__).parent / "logs" / "pixelphotomanager.log"
```

### Checklist avant un build de distribution

- [ ] `requirements.txt` à jour et installé dans le venv
- [ ] Tester `main.py` en mode normal (pas de régression)
- [ ] Vérifier que l'icône `assets/lutin_camera_icon_download.ico` existe
- [ ] Lancer `.\build.ps1` et vérifier l'absence d'erreurs PyInstaller
- [ ] Tester `dist\PixelPhotoManager\PixelPhotoManager.exe` sur le poste de build
- [ ] Tester sur un PC **sans Python installé** (machine vierge ou VM)
- [ ] Vérifier que les logs apparaissent dans `%LOCALAPPDATA%\PixelPhotoManager\logs\`

---

## 15. Patterns à suivre pour les évolutions

### Ajouter une retouche image

1. **`src/core/models.py`** — Ajouter le champ dans `EditInfo` avec valeur neutre par défaut.
2. **`src/processing/adjustments.py`** — Ajouter la méthode statique dans `ImageAdjuster` et l'appeler dans `apply_all()` à la bonne position dans l'ordre.
3. **`src/ui/edit_panel.py`** — Ajouter un tuple dans `_TREATMENTS` (label, icône, sliders_def).
4. **`src/processing/edit_database.py`** — Ajouter la colonne SQL via une migration `ALTER TABLE` dans `_init_db()` (pattern du `_MIGRATE_STRAIGHTEN` existant).

### Ajouter un nouveau type de média

1. Ajouter l'extension dans une constante dédiée dans `exif_reader.py`.
2. Créer un reader de métadonnées similaire à `VideoMetadataReader`.
3. Mettre à jour `SUPPORTED_EXT` dans `scanner.py`.
4. Ajouter la génération de vignette dans `thumbnail_cache.py`.
5. Gérer l'affichage dans `photo_viewer.py` (pixmap + bouton d'action).
6. Ajouter la migration de colonne dans `catalog.py` si des champs spécifiques sont nécessaires.

### Ajouter un événement bus

1. Définir l'événement dans le docstring de `EventBus` : nom, kwargs, émetteur, consommateurs.
2. Émettre avec `bus.emit("module.action", **kwargs)`.
3. S'abonner avec `bus.on("module.action", handler)` dans `activate()`, se désabonner dans `deactivate()`.

### Ajouter une vue dans la sidebar

Implémenter `ViewPlugin` (sous-classe de `BasePlugin`) avec `create_widget(parent) → QWidget`, puis déclarer le plugin dans son `plugin.json`.

### Modifier le catalogue

1. Toujours utiliser `ON CONFLICT` ou migrations `ALTER TABLE` pour la rétrocompatibilité.
2. Ajouter la migration dans `Catalog._init_db()`.
3. Normaliser les nouveaux champs de type chemin avec `os.path.normpath`.
4. Respecter le verrou `self._lock` et le pattern `conn = self._conn(); try: ... finally: conn.close()`.

### Opérations longues

Toute opération > 50 ms doit passer dans un `QThread`. Pattern minimal :

```python
class MyThread(QThread):
    result = Signal(object)

    def run(self) -> None:
        data = ...long operation...
        self.result.emit(data)

thread = MyThread()
thread.result.connect(self._on_result)   # dans le thread UI
thread.start()
```

---

## 16. Tests

### 15.1 Trois couches

| Layer | Dossier | Cible | Dépendances | Vitesse |
|---|---|---|---|---|
| 1 — Unitaire | `tests/test_*.py` | Logique pure (DB, géométrie, doublons…), sans Qt | `requirements.txt` | ms |
| 2 — Widgets Qt | `tests/gui_widgets/` | Widgets isolés via `pytest-qt` (pas d'automation OS) | `requirements.txt` (`pytest-qt`) | ms–s |
| 3 — Bout-en-bout (e2e) | `tests/e2e/` | La vraie application (`main.py`) pilotée via `pywinauto`, scénario complet UI | `requirements-test-e2e.txt`, **Windows uniquement** | minutes |

```powershell
# Layers 1+2 (défaut — c'est ce que documente CLAUDE.md)
.venv\Scripts\python.exe -m pytest tests/

# Un test précis
.venv\Scripts\python.exe -m pytest tests/test_duplicate_detector.py -v

# Layer 3 — nécessite requirements-test-e2e.txt installé au préalable
.venv\Scripts\python.exe -m pytest tests/e2e -m e2e -v
```

`pytest.ini` définit `addopts = -m "not e2e"` : `pytest tests/` sans argument **n'exécute jamais** les scénarios e2e, il faut systématiquement `-m e2e` (ou `-m ""` pour tout inclure) pour les déclencher. Deux markers sont déclarés : `e2e` (lent, Windows-only, vole le focus) et `gui` (widget Qt via pytest-qt, rapide).

Si `pywinauto` n'est pas installé, `tests/e2e/conftest.py` retire automatiquement `tests/e2e/scenarios/*` de la collecte (`collect_ignore_glob`) — `pytest tests/` continue de fonctionner normalement sans que Layer 3 soit disponible. Voir **`tests/e2e/README.md`** pour le détail complet de Layer 3 (mécanique de synchronisation via sondage direct des DB, ciblage d'éléments UIA, comment ajouter un scénario, limites connues).

### 15.2 Isolation des données réelles

Aucun test ne doit jamais lire ni écrire dans le vrai `%LOCALAPPDATA%\PixelPhotoManager` de l'utilisateur (catalogue, vignettes, retouches, config). Deux mécanismes, un par couche :

- **Layers 1+2** : `tests/conftest.py` redirige la variable d'environnement `LOCALAPPDATA` vers un dossier temporaire de session, **avant tout import** de code applicatif (chargé par pytest avant tout fichier `tests/**/*.py`). Comme `src/core/app_dirs.py::APP_DATA_DIR` est une constante de module calculée une seule fois au premier import, cette redirection garantit qu'aucun composant (y compris ceux instanciés sans point d'injection explicite, ex. `EditPanel.__init__` → `EditDatabase()`) ne peut accidentellement toucher le profil réel. Cette mutation ne porte que sur le process `pytest` en cours, jamais sur le profil persistant de l'utilisateur. Les tests Layer 1 qui passent un `db_path=tmp_path/...` explicite au constructeur ajoutent une seconde couche d'isolation, indépendante de cette variable d'environnement.
- **Layer 3** : `tools/test_env/launch_isolated.py` lance `main.py` en sous-processus avec `LOCALAPPDATA` fixé (uniquement dans le bloc d'environnement de cet enfant) sur un dossier temporaire dédié au test, contre une bibliothèque photo **synthétique et jetable** (`tools/test_env/generate_library.py`) — jamais les vraies photos de l'utilisateur.

**Conséquence pratique** : les tests peuvent tourner sans risque pendant qu'une instance réelle de l'application est ouverte sur les données de production — aucun fichier (DB, config, vignettes) n'est partagé. Il n'existe pas de verrou "instance unique" dans le code, donc une deuxième instance (lancée par Layer 3) démarre sans conflit à côté de la réelle. Deux réserves cependant :
1. **Log partagé en mode dev** : en mode non-figé, `main.py` écrit toujours dans `<repo>/logs/pixelphotomanager.log`, indépendamment de `LOCALAPPDATA` (seul le mode `sys.frozen` redirige vers `%LOCALAPPDATA%\PixelPhotoManager\logs\`). Si l'instance réelle tourne aussi via `python main.py` pendant un run Layer 3, les deux processus écrivent dans le même fichier — lignes entrelacées, sans corruption de données ni impact sur les tests.
2. **`pywinauto` vole le focus réel** : `click_input()` envoie de vrais événements souris/clavier au niveau OS (voir `tests/e2e/README.md`, section limites connues). Éviter d'utiliser le clavier/la souris pendant l'exécution de scénarios Layer 3.

### 15.3 Mesurer la couverture

`pytest-cov` n'est pas dans `requirements.txt` (outil de développement, pas une dépendance applicative) :

```powershell
.venv\Scripts\pip.exe install pytest-cov
.venv\Scripts\python.exe -m pytest tests/ --cov=src --cov-report=term-missing
```

Ajouter `--cov-report=html` pour un rapport navigable ligne par ligne (`htmlcov/index.html`).

**État courant (2026-07)** : ~9 % de `src/` couvert par Layers 1+2. Bien couverts : `core/models.py`, `library/duplicate_detector.py` (68 %), `processing/edit_database.py` (71 %), `ui/edit_panel.py` (49 %), `ui/thumbnail_grid.py` (39 %). **Non couverts du tout** : tout `src/faces/` (détection, clustering, `face_database.py`, import Picasa), tout `src/core/` sauf `models.py`/`app_dirs.py` (bus d'événements, config, plugin manager), et la quasi-totalité de `src/ui/` (`main_window.py`, `photo_viewer.py`, `sidebar.py`, tous les dialogues). Ces zones ne sont exercées qu'indirectement via les 4 scénarios Layer 3 existants (voir `tests/e2e/README.md`).

### 15.4 Écrire un nouveau test

- **Logique pure sans Qt** → Layer 1, `tests/test_*.py`. Préférer un `db_path=tmp_path/...` explicite en plus de l'isolation `LOCALAPPDATA` du conftest.
- **Un widget Qt isolé** (comportement, signaux, rendu) → Layer 2, `tests/gui_widgets/`, via `pytest-qt` (fixture `qtbot`). Pas d'automation OS, pas de fenêtre visible.
- **Un scénario bout-en-bout impliquant plusieurs composants réels** (scan → catalogue → UI, ou toute régression déjà rencontrée en production) → Layer 3, `tests/e2e/scenarios/`. Suivre le guide « Ajouter un scénario » de `tests/e2e/README.md` : fixture `isolated_app`, toujours vérifier l'état via `catalog.db`/`edits.db` (`query_one`/`wait_for_condition`) plutôt que via le texte affiché à l'écran.
