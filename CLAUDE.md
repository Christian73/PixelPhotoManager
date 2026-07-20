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

# Couverture (cliquet fail_under dans .coveragerc — relever, jamais baisser)
.venv\Scripts\python.exe -m pytest tests/ --cov=src

# Scénarios e2e avec couverture du code UI (l'appli tourne sous coverage,
# fichiers .coverage.* écrits à la racine — fusionner avec coverage combine)
$env:PPM_E2E_COVERAGE='1'; .venv\Scripts\python.exe -m pytest tests/e2e -m e2e
.venv\Scripts\python.exe -m coverage combine; .venv\Scripts\python.exe -m coverage report

# Packaging Windows (exécutable autonome)
.venv\Scripts\pyinstaller.exe pixelphotomanager.spec
```

### Règles de test

- **Tout bugfix s'accompagne de son test de non-régression** (cf. `test_signal_object_cross_thread.py`, `test_duplicate_detector.py::test_tiff_never_reaches_cv2_imread` pour le style attendu).
- **Piège coverage/QThread** : coverage.py ne trace pas le code exécuté dans un `QThread.start()` réel (thread natif Qt, hors `sys.settrace` en Python 3.11). Dans les tests, appeler `thread.run()` en synchrone (les signaux sont émis en connexion directe et le code est tracé) ; garder un ou deux vrais `.start()` + `qtbot.waitSignal` par module pour la plomberie cross-thread.
- **Piège QThread jetable dans un test** : un vrai `QThread` sans parent auto-détruit via `deleteLater` pendant que son thread OS se termine déclenche un fail-fast Qt (0xC0000409) dès que l'event loop de pytest-qt traite la destruction — utiliser un stub non-QThread (cf. `_InertUpdateThread` dans `test_dialogs_smoke.py`).
- Le cliquet `fail_under` (`.coveragerc`) s'applique à tout run `--cov` : le relever après chaque campagne qui augmente durablement la couverture.

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
│                  Découpage 2026-07 (les gros fichiers délèguent à des modules
│                  dédiés, noms historiques ré-exportés depuis le module d'origine) :
│                  - main_window.py  → background_workers.py (7 QThreads),
│                    export_dialogs.py, reset_faces_dialog.py, duplicates_popup.py,
│                    ui_utils.py (fmt_size)
│                  - photo_viewer.py → viewer_canvas.py (_Canvas + _InlineTextEdit),
│                    viewer_pixmaps.py (_build_pixmap & co., utilisé par slideshow)
│                  - edit_panel.py   → edit_sliders.py (MarkedSlider/EditSlider),
│                    treatment_dialogs.py (_TREATMENTS + dialogues), edit_icons.py
│                  Importer les classes depuis le module d'origine reste valide
│                  (ré-exports) ; le nouveau code doit importer le module dédié.
├── processing/    Retouches image (non destructives)
├── faces/         Détection, reconnaissance, clustering (+ import Picasa)
└── plugins/       Plugins intégrés (carte, restauration IA, etc.)
plugins/           Plugins utilisateur (externe à src/)
```

`src/library/fs_utils.py::is_hidden_path()` est l'unique implémentation du test
« chemin caché » (attribut Windows ou préfixe point) — scanner, folder_watcher
et folder_manager_dialog l'aliasent en `_is_hidden` ; ne pas recréer de copie.

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

### Détection de doublons — continue et incrémentale

`src/library/duplicate_detector.py` (`DuplicateDetectorThread`) se déclenche automatiquement après chaque scan (`MainWindow._on_scan_finished()` → `_start_duplicate_detection()`), sur le même principe que l'indexation des visages : pas de bouton manuel, pas de rapport de fin. Le menu **Outils › État des doublons…** (`MainWindow._show_duplicate_status_dialog()`) affiche un instantané en lecture seule (nombre de groupes/photos, dernière vérification, fichiers corrompus) avec un bouton **Vérifier maintenant** pour forcer une passe.

Deux niveaux (Tier 1 pHash, Tier 2 ORB+RANSAC pour les recadrages) — voir le docstring du module. La comparaison **par paires** (pas seulement le calcul pHash/ORB par fichier, déjà caché par mtime) est vraiment incrémentale grâce à deux tables `compared_tier1`/`compared_tier2` (`src/library/dedup_cache.py`) qui tracent quels chemins ont déjà été intégralement comparés au reste de la bibliothèque connue — seules les paires nouveau×ancien et nouveau×nouveau sont réévaluées, jamais ancien×ancien.

`DuplicateDetectorThread` prend un paramètre `seed_groups: dict[path, group_id]` (typiquement `Catalog.get_duplicate_group_assignments()`) pour amorcer `group_of` sans tout recomparer. **Piège** : relancer le thread sur un `cache_db_path` déjà peuplé **sans repasser `seed_groups`** fait que toutes les paires apparaissent comme « déjà comparées » et qu'aucun groupe n'est reformé — retour silencieux de `{}` au lieu d'une erreur. En usage réel (`main_window.py`), `seed_groups` est toujours récupéré frais avant chaque création de thread ; seul un nouveau test/script qui relance `_detect()` plusieurs fois sur le même cache doit y penser explicitement.

Conséquence de l'incrémentalité : `Catalog.ignore_duplicate_group()` (dissoudre un groupe, bouton ✕ de la grille des doublons) est maintenant **persistant** — un groupe ignoré n'est plus jamais recréé tant qu'aucun de ses membres ne change (ils sont déjà dans `compared_tier1`/`_tier2`, donc jamais recomparés entre eux). Un nouveau fichier correspondant à l'un d'eux reste détecté normalement (comparaison new×old).

### Visages — deux étages de filtrage par taille

`src/faces/detector.py::detect_and_embed()` exclut définitivement (visage jamais écrit en base) : `det_score < 0.5`, `embedding is None`, ou `w < 20 / h < 20` px. Ne pas y ajouter de seuil d'aire relatif à l'image — ça a déjà causé un bug (visages valides supprimés silencieusement, sans trace ni rattrapage possible).

`src/faces/face_database.py::save_faces()` marque ensuite `ignored=1` (visage conservé en base, masqué de l'UI/clustering, **récupérable**) selon un seuil proportionnel à la résolution de la photo et `_AUTO_IGNORE_MIN_SCORE` (0.65). Seuil de taille : un visage qualifie la photo de "premier plan" s'il atteint `_AUTO_IGNORE_MIN_SIDE_FG_RATIO` (20 % du plus petit côté de la photo, ou 2× le seuil de base si plus grand). Si au moins un visage premier plan est présent, tout visage plus petit que `_AUTO_IGNORE_FG_FRACTION` (1/4) du plus petit visage premier plan est ignoré. Sinon (aucun premier plan), seuil de base `_AUTO_IGNORE_MIN_SIDE_RATIO` = 3 % du plus petit côté. C'est le seul étage qui doit décider si un petit visage est bruit ou non — `FaceDatabase.recalculate_size_ignored()` implémente la même règle mais n'est actuellement rattachée à aucune entrée de menu (code orphelin, cf. `RevaluateSizeIgnoredThread` dans `face_indexer.py`).

### Visages — cache des centroïdes personne (popup d'assignation de nom)

`src/faces/face_database.py::get_all_person_centroids()` décode les embeddings (512D float32) de tous les visages identifiés pour calculer le centroïde de chaque personne — jusqu'à ~60k visages sur une grosse bibliothèque, plusieurs secondes en pur Python. Le résultat complet est mis en cache en mémoire (`self._person_centroid_cache`) et réutilisé tant qu'un fingerprint bon marché (`SELECT COUNT(*), SUM(person_id) FROM faces WHERE person_id IS NOT NULL`, quelques ms via `idx_faces_person`) n'a pas changé — le `SUM` est nécessaire en plus du `COUNT` pour détecter les réassignations (`merge_persons`) qui ne changent pas le nombre de lignes. Le décodage lui-même est vectorisé via `numpy.frombuffer` plutôt que `struct.unpack` (facteur ~10). `enrich_persons()` (photo_count + cover_path/cover_bbox + pending_count) est également coûteux (~1 s, dominé par une CTE avec fenêtrage pour la photo de couverture) ; `enrich_persons_photo_count()` en est une variante allégée (photo_count seul) à utiliser partout où la couverture n'est pas affichée, ex. la popup d'assignation.

Dans `src/ui/face_panel.py`, la popup d'assignation de nom (`_AssignDialog`) est préparée par `_AssignPrepLoader(QThread)` (get_persons + enrich_persons_photo_count + suggestion de personne par similarité cosinus) avant d'être ouverte, pour respecter la règle "l'UI ne bloque jamais" ci-dessous — `face_cluster_grid.py::_PersonsLoader` suit le même principe pour la vue en grille de groupes.

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

**Pattern de connexion** (commun à `Catalog`, `FaceDatabase`, `ThumbnailCache`, `EditDatabase`) : connexion SQLite **par (instance, thread)**, mise en cache dans un `threading.local` porté par l'instance, PRAGMAs (`journal_mode=WAL`, `synchronous=NORMAL`, `cache_size`) posés une seule fois à la création. Ne jamais revenir au schéma « connexion neuve par appel ». Corollaire : les méthodes d'écriture ne ferment pas la connexion — en cas d'exception elles font `conn.rollback()` (garde `except BaseException: conn.rollback(); raise`) pour ne **jamais** laisser la connexion cachée dans une transaction ouverte, sinon toutes les écritures suivantes échouent en `database is locked`. Toute nouvelle méthode d'écriture doit reprendre cette garde.

**Migrations automatiques au démarrage** (pattern `try: ALTER TABLE ... except: pass`) :
- `_migrate_normalize_paths()` — normalise les séparateurs Windows
- `_migrate_video_fields()` — ajoute `media_type` et `duration` si absents
- `_migrate_face_tables()` — ajoute les tables de visages et annotations Picasa

Piège : l'index `idx_faces_suggestion` (faces.db) doit être créé **après** les migrations dans `_init_db` — la colonne `suggestion_person_id` n'existe pas dans `_CREATE_FACES`, seulement via `ALTER TABLE`.

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
