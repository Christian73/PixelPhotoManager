# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Projet

**PixelPhotoManager** — Gestionnaire de photos desktop Windows. Python 3.11, PySide6. Voir `DocumentDeConception.md` pour la spécification complète.

---

## Environnement

```powershell
# Activer le VENV (toujours utiliser l'interpréteur du venv)
.venv\Scripts\Activate.ps1

# Lancer l'application
.venv\Scripts\python.exe src/core/app.py

# Installer les dépendances
.venv\Scripts\pip.exe install -r requirements.txt

# Lancer les tests
.venv\Scripts\python.exe -m pytest tests/

# Lancer un test précis
.venv\Scripts\python.exe -m pytest tests/test_thumbnail_cache.py::TestThumbnailCache::test_lru_eviction -v

# Packaging Windows (exécutable autonome)
.venv\Scripts\pyinstaller.exe pixelphotomanager.spec
```

---

## Architecture

```
src/
├── core/          Bus d'événements, config, gestionnaire de plugins
├── library/       Scanner de dossiers, catalogue SQLite, cache vignettes, EXIF
│                  exif_reader.py : ExifReader + VideoMetadataReader + VIDEO_EXT
├── ui/            Fenêtre principale, grille, visionneuse, sidebar, panneaux
│                  folder_manager_dialog.py : dialogue Outils › Dossiers…
│                  exif_panel.py            : panneau EXIF dans la visionneuse
├── processing/    Retouches image (non destructives)
├── faces/         Détection, reconnaissance, clustering (+ import Picasa)
└── plugins/       Plugins intégrés (carte, restauration IA, etc.)
plugins/           Plugins utilisateur (externe à src/)
```

### Bus d'événements — pièce centrale

`src/core/event_bus.py` expose une instance globale `bus`. Tous les composants communiquent exclusivement via ce bus. Ne jamais appeler directement une méthode d'un autre composant si un événement peut faire l'affaire.

```python
from src.core.event_bus import bus
bus.on('library.photo_selected', self.handler)
bus.emit('library.photo_selected', photo=photo_info)
```

Événements définis dans le docstring de `EventBus`. Tout nouvel événement doit y être documenté.

### Support vidéo

`src/library/exif_reader.py` expose :
- `VIDEO_EXT` — ensemble des 13 extensions vidéo supportées : `.mp4 .mov .avi .mkv .wmv .webm .m4v .3gp .flv .ts .mts .mpg .mpeg`
- `VideoMetadataReader.read(path)` — lit résolution/fps/durée via `cv2.VideoCapture`, date = `os.stat(path).st_mtime`

`src/core/models.py` — `PhotoInfo` dispose de deux champs supplémentaires :
- `media_type: str = "image"` — `"image"` ou `"video"`
- `duration: float = 0.0` — durée en secondes (vidéos uniquement)

`catalog.db` comporte les colonnes `media_type` et `duration` (migration automatique au démarrage via `_migrate_video_fields()`).

Le panneau de retouche est **ignoré pour les vidéos** : `main_window.show_viewer()` et `_navigate_photo()` vérifient `photo.media_type == "video"` et gardent `_left_stack` à l'index 0 (sidebar) au lieu de 1 (panneau retouche).

### Retouches non destructives

Les retouches ne modifient jamais les fichiers originaux. Les ajustements sont stockés dans `%LOCALAPPDATA%\PixelPhotoManager\edits.db` (SQLite) et appliqués à la volée (affichage, export). L'original est toujours récupérable.

- `src/processing/edit_database.py` — `EditDatabase` : table `photo_edits` (état courant) + table `edit_history` (historique persistant, 50 entrées max par photo)
- L'historique est rechargé depuis la DB à l'ouverture d'une photo → undo/redo persistant entre sessions
- Le bouton **Appliquer** dans `EditPanel` déclenche `EditDatabase.save()`

### Cache vignettes à trois niveaux

`src/library/thumbnail_cache.py` — RAM LRU (500 entrées, ~50 Mo) → SQLite → génération à la demande dans un thread. Ne jamais générer de vignettes dans le thread UI.

Pour les vidéos, `generate()` délègue à `_generate_video_thumb()` : `cv2.VideoCapture` → seek à 10 % de la durée → frame BGR→RGB → PIL → JPEG.

### Gestionnaire de dossiers

`src/ui/folder_manager_dialog.py` — `FolderManagerDialog(QDialog)` — accessible via **Outils › Dossiers…**.

- Affiche tous les dossiers surveillés avec statut (✓/✗), nombre de fichiers, sous-dossiers ignorés (cachés, Originals).
- Signaux : `rescan_requested(str)`, `folder_removed(str)`, `folder_added(str)`.
- Le re-scan forcé passe par `LibraryScanner.scan(folders, force=True)` → `ScanThread(force=True)` → `known = {}` (bypass du cache mtime).

### Règle de performance : l'UI ne bloque jamais

Toute opération > 50 ms passe dans un `QThread`. Les signaux PySide6 (`pyqtSignal`) sont le seul moyen de communiquer du thread secondaire vers l'UI.

---

## Système de plugins

Un plugin = un dossier dans `plugins/` avec `plugin.json` + `plugin.py`.

Trois classes de base dans `src/core/` :
- `BasePlugin` — tout plugin
- `ProcessorPlugin(BasePlugin)` — traitement image (implémente `process(image, params) -> Image`)
- `ViewPlugin(BasePlugin)` — nouvelle vue dans la sidebar (implémente `create_widget(parent) -> QWidget`)

Le `PluginManager` charge les plugins dynamiquement via `importlib`. Les plugins s'intègrent sans modifier le code existant — uniquement via le bus d'événements et les hooks de menu/sidebar.

---

## Base de données

SQLite embarqué, zéro configuration. Le catalogue est dans `%LOCALAPPDATA%\PixelPhotoManager\catalog.db`. Les vignettes ont leur propre base `thumbnails.db`. La configuration est dans `config.json` dans le même dossier. Le chemin de base est défini dans `src/core/app_dirs.py` (`APP_DATA_DIR`). Utiliser `sqlite3` standard, pas d'ORM.

**Migrations automatiques au démarrage** (pattern `try: ALTER TABLE ... except: pass`) :
- `_migrate_normalize_paths()` — normalise les séparateurs Windows
- `_migrate_video_fields()` — ajoute `media_type` et `duration` si absents
- `_migrate_face_tables()` — ajoute les tables de visages et annotations Picasa

---

## Dépendances notables

| Package | Usage |
|---------|-------|
| PySide6 | UI — utiliser `QThread` + signaux pour le threading |
| Pillow | Traitement image principal |
| opencv-python | Traitements avancés (détection, filtres, vignettes vidéo) |
| DeepFace + RetinaFace | Reconnaissance faciale (optionnel, lourd) |
| scikit-learn | Clustering DBSCAN pour les visages |
| imagehash | Détection de doublons perceptuels |
| folium | Carte OpenStreetMap |
| reportlab | Export PDF |

Les dépendances IA (PyTorch, DeepFace, Real-ESRGAN…) sont **optionnelles** et commentées dans `requirements.txt`. Ne pas les imposer au cœur de l'application — les isoler dans des plugins.
