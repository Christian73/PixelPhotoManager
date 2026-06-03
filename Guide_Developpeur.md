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
8. [Système de plugins](#8-système-de-plugins)
9. [Schémas des bases de données](#9-schémas-des-bases-de-données)
10. [Modèle de threading](#10-modèle-de-threading)
11. [Normalisation des chemins Windows](#11-normalisation-des-chemins-windows)
12. [Packaging et distribution](#12-packaging-et-distribution)
13. [Patterns à suivre pour les évolutions](#13-patterns-à-suivre-pour-les-évolutions)

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
│   │   └── exif_reader.py         # Lecture EXIF via Pillow
│   │
│   ├── ui/                        # Interface PySide6
│   │   ├── main_window.py         # Fenêtre principale + orchestration
│   │   ├── sidebar.py             # Arborescence dossiers + albums
│   │   ├── thumbnail_grid.py      # Grille de vignettes
│   │   ├── photo_viewer.py        # Visionneuse + mode recadrage
│   │   └── edit_panel.py          # Panneau de retouche
│   │
│   ├── processing/                # Traitement image
│   │   ├── adjustments.py         # ImageAdjuster.apply_all()
│   │   ├── geometry.py            # Rotation, redressement, recadrage
│   │   └── edit_database.py       # Persistence des retouches (SQLite)
│   │
│   ├── faces/                     # Reconnaissance faciale (optionnel)
│   └── plugins/                   # Plugins intégrés (src/)
│
└── plugins/                       # Plugins utilisateur externes
```

**Données runtime** (non versionnées) :

```
%LOCALAPPDATA%\PixelPhotoManager\
├── catalog.db       # Index des photos
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
└───────────┬───────────┘
            │ appels directs
            ▼
┌───────────────────────────────────────────────────────────────┐
│                      Library / Processing                      │
│   Catalog (SQLite)    ThumbnailCache    LibraryScanner        │
│   EditDatabase        ImageAdjuster     ExifReader            │
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

Représente une photo dans le catalogue. **Le champ `path` est normalisé** (`os.path.normpath`) dans `__post_init__`.

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
```

**Migration au démarrage** : `_migrate_normalize_paths()` est appelée dans `_init_db()` à chaque démarrage. Elle normalise les séparateurs de chemins dans les données existantes (invariant de `os.path.normpath`).

---

### `scanner.py` — Scan de dossiers

```
LibraryScanner.scan(folders) → ScanThread (QThread)
```

**Algorithme du ScanThread :**

1. `os.walk(folder)` récursif → liste de tous les fichiers image (`os.path.normpath` appliqué).
2. `catalog.get_known_mtimes(folder)` → dict `{path: mtime}` des fichiers déjà indexés.
3. Pour chaque fichier : si `mtime` inchangé (±1 s) → skip. Sinon → lecture EXIF → `catalog.add_or_update_photo()` → `photo_discovered.emit(photo)`.

**Signaux émis** :

| Signal | Args | Fréquence |
|---|---|---|
| `photo_discovered` | `PhotoInfo` | Par photo nouvelle/modifiée |
| `progress` | `(int, str)` | Toutes les 50 photos |
| `finished` | `int` (total nouvelles) | Une fois |

> `LibraryScanner.scan()` appelle `stop()` sur le thread précédent avant d'en créer un nouveau. Ne pas appeler `stop()` manuellement sauf à la fermeture de l'application.

---

### `thumbnail_cache.py` — Cache vignettes

Architecture à **3 niveaux** :

```
get(path)
  ├─ 1. RAM dict (LRU, max 500 entrées)  → O(1), retour immédiat
  ├─ 2. SQLite thumbnails.db (clé = MD5(path), mtime check)
  └─ 3. Génération en QThreadPool (_ThumbWorker)
            └─ generate(path, edit=None) → PIL → JPEG → store RAM + DB
```

**Clé de cache** : `MD5(os.path.normpath(path))` — sensible à la casse et aux séparateurs. Toujours passer des chemins normalisés.

**API principale** :

```python
cache.get(photo_path)                  # QPixmap | None (niveaux 1 et 2 seulement)
cache.generate(photo_path, edit=None)  # Force la génération (thread secondaire)
cache.invalidate(photo_path)           # Supprime RAM + DB
cache.move_photo(old_path, new_path)   # Transfère l'entrée sans régénérer
```

**Règle** : ne jamais appeler `generate()` depuis le thread UI. Utiliser `_ThumbWorker` (QRunnable) via `QThreadPool.globalInstance()`.

---

### `exif_reader.py` — Lecture EXIF

```python
data = ExifReader.read(filepath)
# → dict avec : date_taken, width, height, camera_make, camera_model,
#               lens_model, iso, exposure_time, aperture, focal_length,
#               has_gps, gps_lat, gps_lon
```

Formats supportés : `.jpg .jpeg .png .tiff .tif .webp .bmp .gif`

La lecture GPS convertit les degrés/minutes/secondes en degrés décimaux.

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

La double vérification (contexte + déduplication) est essentielle : `photo_discovered` peut être émis pour des photos qui n'appartiennent pas au dossier affiché, ou pour des photos déjà présentes lors d'un re-scan.

**Flux d'un déplacement par glisser-déposer :**

```
Sidebar.photos_dropped.emit(file_paths, dest_folder)
  → MainWindow._on_photos_dropped(file_paths, dest_folder)
    → shutil.move(src, dst)                  # fichier disque
    → catalog.move_photo(src, dst)           # catalogue
    → edit_db.rename_photo(src, dst)         # retouches
    → thumb_cache.move_photo(src, dst)       # vignettes
    → catalog.get_photos_in_folder(dest)     # navigation auto
    → grid.set_photos(new_photos)
```

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

**Sélection** :
- `_selected: set[str]` contient les paths des photos sélectionnées.
- Click simple → remplace la sélection. Ctrl+click → toggle. Shift+click → plage.

---

### `photo_viewer.py` — Visionneuse

```
PhotoViewer (QWidget)
  ├─ _toolbar (QWidget) — nom fichier, favoris, zoom
  ├─ _Canvas (QWidget)  — rendu image + interactions
  └─ _navbar (QWidget)  — précédente/suivante + boutons recadrage
```

**`_build_pixmap(photo, edit)`** : charge l'image avec Pillow, applique `ImageOps.exif_transpose` (correction orientation EXIF), downscale à `_PREVIEW_MAX_PX = 1024 px`, puis applique `ImageAdjuster.apply_all(img, edit)`. Retourne un `QPixmap`.

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

### `edit_panel.py` — Panneau de retouche

**Structure UI** :

```
EditPanel
  ├─ Barre titre + boutons ↩ ↪ (undo/redo)
  └─ QScrollArea
       ├─ QGridLayout (6 boutons corrections : 2 colonnes)
       └─ QGroupBox "Géométrie"
            ├─ Rotation ↺ ↻
            ├─ Redresser | Recadrer
            └─ Miroir H | Miroir V
```

**`TreatmentDialog`** : dialogue modal avec un ou plusieurs `EditSlider`. L'aperçu est en temps réel via `preview.emit(EditInfo)` → `PhotoViewer.update_edit()`. Si l'utilisateur annule, l'`EditInfo` original est restauré.

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

## 8. Système de plugins

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

## 9. Schémas des bases de données

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
    indexed_at  TEXT DEFAULT CURRENT_TIMESTAMP
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
```

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

La clé est le **hash MD5 du chemin normalisé**. Si la vignette a été générée avec des retouches appliquées (`reload_with_edit`), `file_mtime` correspond au mtime du fichier à ce moment. La correspondance `abs(stored_mtime - current_mtime) < 1.0` invalide la vignette si le fichier a changé.

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

## 10. Modèle de threading

```
Thread UI (main)
  ├─ QApplication.exec()
  ├─ All Qt widgets
  └─ Signals/Slots cross-thread (Qt::QueuedConnection automatique)

Thread scan (ScanThread / QThread)
  ├─ os.walk + EXIF reading
  ├─ catalog.add_or_update_photo()   ← verrou threading.Lock
  └─ photo_discovered.emit()         → reçu dans le thread UI

ThreadPool (QThreadPool global)
  └─ _ThumbWorker (QRunnable) × N   ← génération vignettes
       ├─ PIL Image.open + resize
       ├─ thumbnail_cache.generate()  ← verrou threading.Lock
       └─ _ThumbSignals.ready.emit()  → reçu dans ThumbnailCell (UI)
```

**Règles** :
1. Ne jamais modifier un widget Qt depuis un thread secondaire.
2. Utiliser uniquement des signaux PySide6 pour communiquer vers l'UI depuis un thread.
3. Tous les accès SQLite passent par le verrou `threading.Lock` de chaque classe.
4. `_ThumbSignals` est un `QObject` distinct par worker, pas partagé, pour éviter les race conditions sur la destruction.

---

## 11. Normalisation des chemins Windows

### Le problème

Sur Windows, `QFileDialog` retourne des chemins avec `/` (`D:/Photos`). `os.path.join` et `os.walk` peuvent produire des chemins mixtes (`D:/Photos\SubFolder`). `str(Path(...))` normalise vers `\`. Ces incohérences cassent les comparaisons de chaînes dans SQLite (comparaison exacte).

### La solution

**`os.path.normpath`** est appliqué à tous les chemins aux points d'entrée :

| Point d'entrée | Où |
|---|---|
| Création de `PhotoInfo` | `PhotoInfo.__post_init__` |
| Scan de fichiers | `scanner.py` : `os.path.normpath(os.path.join(root, fname))` |
| Requêtes catalogue | `catalog.get_photos_in_folder()`, `get_known_mtimes()` |
| Écritures catalogue | `catalog.move_photo()` (old_path et new_path) |
| Écritures edits | `edit_database.py` : toutes les méthodes publiques |
| Glisser-déposer | `main_window._on_photos_dropped()` : `os.path.normpath(os.path.join(...))` |

**Migration** : `Catalog._migrate_normalize_paths()` et `EditDatabase._migrate_normalize_paths()` normalisent les données existantes au démarrage, avec gestion des doublons (UNIQUE constraint).

---

## 12. Packaging et distribution

### Prérequis

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
```

### Build

```powershell
.\build.ps1                    # Script complet avec nettoyage et résumé
# ou directement :
.venv\Scripts\python.exe -m PyInstaller pixelphotomanager.spec --clean --noconfirm
```

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

## 13. Patterns à suivre pour les évolutions

### Ajouter une retouche image

1. **`src/core/models.py`** — Ajouter le champ dans `EditInfo` avec valeur neutre par défaut.
2. **`src/processing/adjustments.py`** — Ajouter la méthode statique dans `ImageAdjuster` et l'appeler dans `apply_all()` à la bonne position dans l'ordre.
3. **`src/ui/edit_panel.py`** — Ajouter un tuple dans `_TREATMENTS` (label, icône, sliders_def).
4. **`src/processing/edit_database.py`** — Ajouter la colonne SQL via une migration `ALTER TABLE` dans `_init_db()` (pattern du `_MIGRATE_STRAIGHTEN` existant).

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
