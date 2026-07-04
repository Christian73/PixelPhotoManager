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
- `folder_removed` est traité par `MainWindow._on_folder_removed()` : confirmation (nombre de photos affecté) puis `_purge_catalog_for_folder()` supprime les photos du catalogue, les vignettes (`ThumbnailCache.invalidate`) et les visages/`indexed_photos` (`FaceDatabase.delete_for_path`) pour ce dossier. Les fichiers restent intacts sur le disque.

### Visages — deux étages de filtrage par taille

`src/faces/detector.py::detect_and_embed()` exclut définitivement (visage jamais écrit en base) : `det_score < 0.5`, `embedding is None`, ou `w < 20 / h < 20` px. Ne pas y ajouter de seuil d'aire relatif à l'image — ça a déjà causé un bug (visages valides supprimés silencieusement, sans trace ni rattrapage possible).

`src/faces/face_database.py::save_faces()` marque ensuite `ignored=1` (visage conservé en base, masqué de l'UI/clustering, **récupérable**) selon un seuil proportionnel à la résolution de la photo et `_AUTO_IGNORE_MIN_SCORE` (0.65). Seuil de taille : un visage qualifie la photo de "premier plan" s'il atteint `_AUTO_IGNORE_MIN_SIDE_FG_RATIO` (20 % du plus petit côté de la photo, ou 2× le seuil de base si plus grand). Si au moins un visage premier plan est présent, tout visage plus petit que `_AUTO_IGNORE_FG_FRACTION` (1/4) du plus petit visage premier plan est ignoré. Sinon (aucun premier plan), seuil de base `_AUTO_IGNORE_MIN_SIDE_RATIO` = 3 % du plus petit côté. C'est le seul étage qui doit décider si un petit visage est bruit ou non — `FaceDatabase.recalculate_size_ignored()` implémente la même règle mais n'est actuellement rattachée à aucune entrée de menu (code orphelin, cf. `RevaluateSizeIgnoredThread` dans `face_indexer.py`).

### Albums

`src/library/catalog.py::delete_album(album_id)` supprime un album (table `albums` + `album_photos`) sans toucher aux photos. Accessible via menu contextuel sur `Sidebar._albums_list` (`sidebar.py::_album_context_menu()`), qui exclut les 4 albums spéciaux (Chronologie/Favoris/Vidéos/Par nom de fichier) via `isinstance(item.data(Qt.UserRole), AlbumInfo)`.

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

`scikit-learn` et `hdbscan` (clustering des visages, `src/faces/clusterer.py`) sont en revanche des dépendances **non optionnelles** du cœur de l'application : ne jamais les ajouter à `excludes` dans `pixelphotomanager.spec`, sous peine de `ModuleNotFoundError: sklearn` uniquement dans l'exécutable packagé (le mode Python dev n'est pas affecté).

`insightface` doit figurer dans `_with_data` (liste `collect_all`) de `pixelphotomanager.spec` **ET** son dossier `data/objects/` doit en plus être copié explicitement à la racine du bundle sous le nom `objects` (`datas += [(str(Path(insightface.__file__).parent / "data" / "objects"), "objects")]`). Raison : `insightface/data/pickle_object.py::get_object()` résout le chemin différemment selon le mode :
- mode dev : `Path(__file__).parent / "objects"` → `insightface/data/objects/` (arborescence normale du package, ce que `collect_all()` seul reproduit dans l'exe figé sous `_internal/insightface/data/objects/`) ;
- mode figé (`sys.frozen`) : `sys._MEIPASS / "objects"` → un dossier **`objects` à la racine du bundle** (`_internal/objects/`), complètement différent de l'arborescence du package.

`collect_all("insightface")` seul ne suffit donc PAS : il place bien `meanshape_68.pkl` dans l'exe figé, mais au mauvais endroit (`_internal/insightface/data/objects/`), jamais consulté par le code en mode figé. Sans la copie supplémentaire vers `_internal/objects/`, `get_object('meanshape_68.pkl')` renvoie `None` en silence (juste un `print()`, invisible en mode `console=False`), et **chaque** visage détecté fait planter `InsightFace.get()` avec `AttributeError: 'NoneType' object has no attribute 'shape'` (dans `insightface/utils/transform.py::estimate_affine_matrix_3d23d`, appelé depuis `landmark.py::get()` pour le modèle `landmark_3d_68`, estimation de pose). Piège perfide : la détection réussit (bbox trouvée), seul ce post-traitement landmark/pose échoue, donc ça ressemble à un bug de détection alors que c'est un problème d'empaquetage de données — et une correction partielle (juste `collect_all`) ne change rien à l'erreur observée, ce qui peut faire croire à tort que le vrai problème est ailleurs.

Le pack de modèles `buffalo_l` (détection SCRFD + embedding ArcFace, ~340 Mo) est lui aussi embarqué dans le bundle, sous `insightface_root/models/buffalo_l` (`pixelphotomanager.spec`, source = `~/.insightface/models/buffalo_l` de la machine de build — il faut donc avoir lancé l'appli au moins une fois en mode dev pour l'avoir en cache localement avant de builder). `src/faces/detector.py::_insightface_root()` pointe `FaceAnalysis(root=...)` dessus en mode figé. Sans ça, `insightface` tente de télécharger le pack depuis GitHub au 1er lancement sur chaque poste — silencieux et invisible tant qu'il y a un accès Internet, mais **totalement bloquant sans accès à github.com** (pare-feu, poste isolé) : reconnaissance faciale inopérante à 100 % (0 visage détecté, quel que soit le nombre de photos), avec un nouveau essai de téléchargement complet à *chaque photo* puisque le modèle n'est jamais mis en cache.

`main.py` redirige `sys.stdout`/`sys.stderr` vers `os.devnull` au tout début s'ils valent `None` (cas d'un exe `console=False` : toute bibliothèque qui y écrit, comme `tqdm` utilisé par `insightface` pendant un téléchargement, plante avec `AttributeError: 'NoneType' object has no attribute 'write'`). Ce crash était particulièrement pernicieux avec le téléchargement du pack `buffalo_l` : la requête HTTP aboutissait bien (200 OK), mais `tqdm` plantait pendant l'écriture de la barre de progression, interrompant le flux **avant** l'écriture du fichier sur le disque — le modèle n'était donc jamais mis en cache, et le run suivant retentait un téléchargement complet, en boucle.
