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
9. [Système de plugins](#9-système-de-plugins)
10. [Schémas des bases de données](#10-schémas-des-bases-de-données)
11. [Modèle de threading](#11-modèle-de-threading)
12. [Normalisation des chemins Windows](#12-normalisation-des-chemins-windows)
13. [Packaging et distribution](#13-packaging-et-distribution)
14. [Patterns à suivre pour les évolutions](#14-patterns-à-suivre-pour-les-évolutions)

---

## 1. Arborescence des sources

```
PixelPhotoManager/
│
├── main.py                        # Point d'entrée unique
├── pixelphotomanager.spec         # Spec PyInstaller (packaging)
├── build.ps1                      # Script de build PowerShell
├── requirements.txt               # Dépendances Python
├── Guide_Utilisateur.md
├── Guide_Developpeur.md
│
├── assets/
│   └── lutin_camera_icon_download.ico   # Icône de l'application
│
├── src/
│   ├── core/                      # Socle transversal
│   │   ├── app_dirs.py            # Chemin APP_DATA_DIR
│   │   ├── config.py              # Config singleton (JSON)
│   │   ├── event_bus.py           # Bus d'événements pub/sub
│   │   ├── models.py              # Dataclasses (PhotoInfo, EditInfo…)
│   │   ├── base_plugin.py         # Classe de base des plugins
│   │   ├── processor_plugin.py    # Sous-classe plugin de traitement
│   │   └── plugin_manager.py      # Chargement dynamique des plugins
│   │
│   ├── library/                   # Gestion de la bibliothèque
│   │   ├── catalog.py             # Catalogue SQLite (photos, albums)
│   │   ├── scanner.py             # Scan de dossiers en thread
│   │   ├── thumbnail_cache.py     # Cache vignettes 3 niveaux
│   │   └── exif_reader.py         # Lecture EXIF (ExifReader) + vidéo (VideoMetadataReader)
│   │
│   ├── ui/                        # Interface PySide6
│   │   ├── main_window.py         # Fenêtre principale + orchestration
│   │   ├── sidebar.py             # Arborescence dossiers + albums
│   │   ├── thumbnail_grid.py      # Grille de vignettes (badge ▶ pour vidéos)
│   │   ├── photo_viewer.py        # Visionneuse + mode recadrage + annotations + vidéos
│   │   ├── edit_panel.py          # Panneau de retouche (un seul outil actif à la fois)
│   │   ├── annotation_renderer.py # Rendu QPainter du calque d'annotations (aperçu + export)
│   │   ├── exif_panel.py          # Panneau EXIF (toggle avec touche I)
│   │   └── folder_manager_dialog.py  # Dialogue Outils › Dossiers…
│   │
│   ├── processing/                # Traitement image
│   │   ├── adjustments.py         # ImageAdjuster.apply_all()
│   │   ├── geometry.py            # Rotation, redressement, recadrage
│   │   ├── annotation_geometry.py # Géométrie des formes d'annotation (hit-test, bbox, redimensionnement)
│   │   └── edit_database.py       # Persistence des retouches (SQLite), inclut EditInfo.annotations
│   │
│   ├── faces/                     # Reconnaissance faciale (optionnel)
│   │   ├── detector.py            # Détection (RetinaFace / OpenCV)
│   │   ├── recognizer.py          # Embeddings (DeepFace / ArcFace)
│   │   ├── clusterer.py           # Clustering DBSCAN (scikit-learn)
│   │   ├── face_panel.py          # Panneau visages dans la visionneuse
│   │   └── picasa_importer.py     # Import annotations Picasa (.picasa.ini)
│   └── plugins/                   # Plugins intégrés (src/)
│
└── plugins/                       # Plugins utilisateur externes
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

# Lancer les tests
.venv\Scripts\python.exe -m pytest tests/

# Construire l'EXE
.\build.ps1
```

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

Interface d'accès à `catalog.db`. Toutes les méthodes sont **thread-safe** (verrou `threading.Lock` sur chaque opération, connexion créée et fermée à chaque appel).

```python
catalog = Catalog()   # utilise APP_DATA_DIR / "catalog.db"

catalog.add_or_update_photo(photo)          # INSERT OR REPLACE
catalog.get_photos_in_folder(folder)        # liste triée date desc
catalog.get_all_photos()
catalog.search(query)                       # filename, make, model
catalog.move_photo(old_path, new_path)
catalog.rename_photo(old_path, new_path)
catalog.delete_photo(path)
catalog.get_known_mtimes(folder)            # dict {path: mtime} pour le scanner
catalog.update_paths_prefix(old, new)       # renommage de dossier en masse
catalog.count_photos_in_folder(folder)      # int — compte récursif pour le FolderManagerDialog
```

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

---

## 6. Composants UI

### `main_window.py` — Orchestrateur principal

`MainWindow` est le **chef d'orchestre** : il instancie tous les composants UI, les relie aux composants library/processing, et répond aux signaux de la sidebar et de la grille.

**Attributs d'état clés** :

| Attribut | Type | Description |
|---|---|---|
| `_current_photos` | `list[PhotoInfo]` | Photos affichées dans la grille |
| `_current_context` | `str` | Dossier ou contexte actif (`"Toutes les photos"`, `"Favoris"`, un chemin, un nom d'album) |
| `_current_photo_index` | `int` | Index de la photo ouverte dans la visionneuse |

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

Quand un nœud possède des sous-dossiers, un **placeholder** (item vide sans `UserRole`) est ajouté pour rendre le nœud dépliable. À l'expansion (`itemExpanded`), le placeholder est remplacé par les vrais sous-dossiers (`_populate_subfolders`).

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

**`_build_pixmap(photo, edit)`** : charge l'image avec Pillow, applique `ImageOps.exif_transpose`, downscale à `_PREVIEW_MAX_PX = 1024 px`, puis applique `ImageAdjuster.apply_all(img, edit)`. Pour les vidéos, délègue à `_build_video_pixmap(path)` qui extrait une frame via `cv2`.

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

**`TreatmentDialog`** : dialogue modal avec un ou plusieurs `EditSlider`. L'aperçu est en temps réel via `preview.emit(EditInfo)` → `PhotoViewer.update_edit()`. Si l'utilisateur annule, l'`EditInfo` original est restauré.

**Couleurs (N&B)** : `TreatmentDialog` avec checkbox `bw` + trois `EditSlider` pour `bw_red`, `bw_green`, `bw_blue`. La checkbox active/désactive les sliders.

**Pile undo/redo** :

- `_undo_stack` et `_redo_stack` : listes d'`EditInfo` en mémoire (max 20).
- Chaque opération (`_push_undo`) sauvegarde également dans `edit_db` via `_save()`.
- À l'ouverture d'une photo, `get_history()` recharge jusqu'à 20 états depuis la DB → undo persistant entre sessions.

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

> Module optionnel — dépendances lourdes (DeepFace, RetinaFace, PyTorch, scikit-learn). L'application fonctionne sans ces packages ; les fonctionnalités de visages sont simplement désactivées.

### Architecture

```
faces/
├── detector.py        # Détection via RetinaFace (fallback : OpenCV Haar)
├── recognizer.py      # Embeddings facials via DeepFace/ArcFace
├── clusterer.py       # Clustering DBSCAN (scikit-learn)
├── face_panel.py      # Widget visionneuse : boîtes, noms, menu contextuel
└── picasa_importer.py # Import .picasa.ini → table picasa_annotations
```

### Import Picasa

`PicasaImporter` lit les fichiers `.picasa.ini` dans chaque dossier scanné. Il parse les sections `[contacts]` (mapping hash → nom) et les entrées `rect=` des photos (coordonnées de visage encodées en base64).

Les annotations sont stockées dans `catalog.db` dans la table `picasa_annotations`. La coexistence avec le moteur ArcFace est gérée : les noms Picasa peuvent être réutilisés comme étiquettes pour les clusters DBSCAN.

---

## 9. Système de plugins

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

## 10. Schémas des bases de données

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
    duration    REAL DEFAULT 0.0       -- durée en secondes (vidéos)
);

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

-- Annotations de visages importées depuis Picasa (.picasa.ini)
CREATE TABLE picasa_annotations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path  TEXT NOT NULL,
    person_name TEXT NOT NULL,
    rect_x1     REAL,   -- coordonnées relatives (0-1)
    rect_y1     REAL,
    rect_x2     REAL,
    rect_y2     REAL,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

> Les colonnes `media_type` et `duration` sont ajoutées par migration automatique au démarrage si elles n'existent pas (`_migrate_video_fields()`).

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

---

## 11. Modèle de threading

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
       ├─ thumbnail_cache.generate()  ← verrou threading.Lock
       └─ _ThumbSignals.ready.emit()  → reçu dans ThumbnailCell (UI)
```

**Règles** :
1. Ne jamais modifier un widget Qt depuis un thread secondaire.
2. Utiliser uniquement des signaux PySide6 pour communiquer vers l'UI depuis un thread.
3. Tous les accès SQLite passent par le verrou `threading.Lock` de chaque classe.
4. `_ThumbSignals` est un `QObject` distinct par worker, pas partagé, pour éviter les race conditions sur la destruction.

---

## 12. Normalisation des chemins Windows

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

## 13. Packaging et distribution

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

## 14. Patterns à suivre pour les évolutions

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
