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
.venv\Scripts\python.exe main.py

# Installer les dépendances
.venv\Scripts\pip.exe install -r requirements.txt

# Lancer les tests
.venv\Scripts\python.exe -m pytest tests/

# Lancer un test précis
.venv\Scripts\python.exe -m pytest tests/test_thumbnail_cache.py::TestThumbnailCache::test_lru_eviction -v

# Couverture (cliquet fail_under dans .coveragerc — relever, jamais baisser)
# COVERAGE_FILE dédié pour pouvoir combiner ensuite avec la couverture e2e (cf. plus bas).
# Piège vécu : un nom du style ".coverage.base" reste invisible en apparence, mais
# "coverage combine" sans argument (appelé juste après pour fusionner les fichiers par
# processus de l'étape e2e) fusionne PAR DÉFAUT tout fichier correspondant au motif
# "<COVERAGE_FILE>.*" — soit ".coverage.*" puisque COVERAGE_FILE vaut ".coverage" par défaut à
# ce moment-là. ".coverage.base" correspond à ce motif et se fait donc absorber une étape trop
# tôt (silencieusement — pas d'erreur avant l'étape finale, qui échoue avec "Couldn't combine
# from non-existent path"). D'où un nom qui NE COMMENCE PAS par ".coverage." pour les fichiers
# intermédiaires à préserver.
$env:COVERAGE_FILE='coverage_base.dat'; .venv\Scripts\python.exe -m pytest tests/ --cov=src
Remove-Item Env:\COVERAGE_FILE

# Scénarios e2e avec couverture du code UI (l'appli tourne sous coverage,
# fichiers .coverage.* écrits à la racine — fusionner avec coverage combine)
$env:PPM_E2E_COVERAGE='1'; .venv\Scripts\python.exe -m pytest tests/e2e -m e2e
.venv\Scripts\python.exe -m coverage combine; .venv\Scripts\python.exe -m coverage report
Copy-Item .coverage coverage_e2e.dat -Force  # préserve le résultat e2e-only sous un nom dédié

# Analyse combinée (base + e2e) : fusionne les deux séries préservées ci-dessus en un seul total
$env:COVERAGE_FILE='coverage_combined.dat'; .venv\Scripts\python.exe -m coverage combine --keep coverage_base.dat coverage_e2e.dat
.venv\Scripts\python.exe -m coverage report --data-file=coverage_combined.dat
Remove-Item Env:\COVERAGE_FILE

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
│                  theme.py                 : feuille de style sombre globale
│                    (`app_stylesheet(check_icon)`, posée par main() sur la
│                    QApplication). Tout contrôle dont l'indicateur est dessiné
│                    par le style — case à cocher, bouton radio… — doit y avoir
│                    ses règles `::indicator` : dès qu'une feuille de style
│                    applicative existe, Qt bascule sur QStyleSheetStyle et un
│                    sous-contrôle sans règle est rendu avec des couleurs par
│                    défaut invisibles sur fond #1e1e1e (cas vécu : pastille de
│                    QRadioButton cochée strictement identique au fond). Les
│                    copies locales de `_RADIO_STYLE` (display_order_dialog,
│                    people_panel, export_dialogs, reset_faces_dialog) datent
│                    d'avant et restent prioritaires — ne pas en créer de
│                    nouvelle. Test : tests/gui_widgets/test_theme.py mesure le
│                    contraste réel du rendu (grab()), pas la présence d'une
│                    chaîne.
│                  Découpage 2026-07 (les gros fichiers délèguent à des modules
│                  dédiés, noms historiques ré-exportés depuis le module d'origine) :
│                  - main_window.py  → background_workers.py (7 QThreads),
│                    export_dialogs.py, reset_faces_dialog.py, duplicates_popup.py,
│                    ui_utils.py (fmt_size, largeur des menus — cf. plus bas)
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
- `VIDEO_EXT` — ensemble des 14 extensions vidéo supportées : `.mp4 .mov .avi .mkv .wmv .webm .m4v .3gp .flv .ts .mts .mpg .mpeg .vob`
  (`.vob` = flux d'une copie de DVD, dossier `VIDEO_TS` — copie littérale dans
  `src/library/duplicate_detector.py::_VIDEO_EXT`, à maintenir en parallèle)
- `VideoMetadataReader.read(path)` — lit résolution/fps/durée via `cv2.VideoCapture`, date = `os.stat(path).st_mtime`

`src/core/models.py` — `PhotoInfo` dispose de deux champs supplémentaires :
- `media_type: str = "image"` — `"image"` ou `"video"`
- `duration: float = 0.0` — durée en secondes (vidéos uniquement)

`catalog.db` comporte les colonnes `media_type` et `duration` (migration automatique au démarrage via `_migrate_video_fields()`).

Le panneau de retouche est **ignoré pour les vidéos** : `main_window.show_viewer()` et `_navigate_photo()` vérifient `photo.media_type == "video"` et gardent `_left_stack` à l'index 0 (sidebar) au lieu de 1 (panneau retouche).

### Décodage image — RAW et HEIC/HEIF

`src/library/image_loader.py::open_image(path)` est le **point de décodage image
unique** du projet — tous les sites qui ouvraient auparavant un fichier via
`PIL.Image.open(path)` directement (thumbnail_cache, viewer_pixmaps, exif_reader,
faces/detector) doivent passer par lui à la place. Ne pas recréer un appel direct
`Image.open()` sur un chemin de fichier utilisateur (un `Image.open(io.BytesIO(...))`
sur des octets déjà décodés reste légitime, ex. `viewer_pixmaps._apply_edit_to_base`).

- `RAW_EXT = {.cr2 .nef .arw .dng .orf .rw2}`, `is_raw_available()` (import
  `rawpy` caché, coûteux). RAW décodé via l'aperçu JPEG embarqué par l'appareil
  (`rawpy.imread(path).extract_thumb()`, rapide, conserve l'EXIF d'origine) avec
  repli sur `raw.postprocess(half_size=True)` (dématriçage réduit, plus lent) si
  aucun aperçu exploitable — export d'un RAW = résolution de cet aperçu, limite
  documentée du produit.
- HEIC/HEIF : `register_heif_opener()` de `pillow-heif` enregistré au **niveau
  module** (pas dans une fonction) — les workers `ProcessPoolExecutor` (spawn,
  `faces/detector.py`) ré-importent les modules sans jamais passer par `main()`,
  un enregistrement paresseux ne s'exécuterait donc jamais pour eux. Une fois
  enregistré, `Image.open()` lit le HEIC de façon transparente partout (y
  compris hors de `image_loader`, ex. les fallbacks PIL de
  `duplicate_detector.py`) — aucune exclusion dédiée n'est nécessaire pour HEIC,
  contrairement au RAW ci-dessous.
- `safe_temp_suffix(path)` — à utiliser partout où un fichier temporaire est
  écrit via `PIL.Image.save()` à partir d'une image décodée depuis un chemin
  RAW ou HEIC : force `.jpg` (RAW n'est jamais ré-savable par PIL, HEIC pas
  forcément selon la version de `pillow-heif`).
- Détection de doublons (`duplicate_detector.py`) exclut `RAW_EXT` (import direct
  depuis `image_loader`, à ne pas dupliquer localement — contrairement à
  `_VIDEO_EXT`, historiquement dupliqué) du prélèvement Tier 1 : sans ça, ni
  `cv2.imread` ni le simple `PIL.Image.open` des fallbacks ne savent décoder un
  RAW, qui serait alors classé « corrompu » et proposé à la suppression.
- `faces/detector.py::_exif_corrected()` force la conversion en JPEG temporaire
  (`needs_format_conversion`) pour toute extension RAW/HEIC, **même si**
  l'orientation EXIF est déjà normale et le chemin ASCII — sans ce déclencheur
  inconditionnel, un RAW/HEIC correctement orienté (le cas le plus courant)
  passait tel quel à `cv2.imread()`, qui ne sait pas le décoder : 0 visage
  détecté en silence sur la quasi-totalité des photos RAW.
- `scanner.py::SUPPORTED_EXT` n'inclut `RAW_EXT` que si `is_raw_available()` ;
  `ExifReader.SUPPORTED` inclut `.heic`/`.heif` inconditionnellement (pillow-heif
  fait partie du cœur, pas une dépendance optionnelle isolée dans un plugin).

### Copies de DVD (dossiers VIDEO_TS/AUDIO_TS)

Un dossier « copie de DVD » (arborescence `VIDEO_TS`/`AUDIO_TS` standard) est parcouru
par le scanner comme n'importe quel autre dossier — pas d'exclusion dédiée. Les `.VOB`
(flux vidéo réels) sont cataloguées normalement via `VIDEO_EXT` ; les `.IFO`/`.BUP`
(métadonnées de navigation, pas du média) restent ignorés faute d'extension supportée.

`src/library/fs_utils.py::find_dvd_video_ts(folder)` détecte, purement côté filesystem
(un `os.scandir` des enfants directs, aucune persistance), si `folder` est une copie de
DVD (`VIDEO_TS` en enfant direct, insensible à la casse). Deux usages :
- `src/ui/sidebar.py::_mark_if_dvd_copy()` pose une icône disque sur le nœud de l'arbre —
  restreint aux dossiers **sans photo cataloguée** (`count` vide/nul) pour éviter un
  `os.scandir` de plus par dossier affiché ; devenu rare en pratique depuis que les
  `.VOB` sont cataloguées (le dossier n'a alors plus l'air vide), mais reste utile avant
  le premier scan d'un dossier nouvellement ajouté.
- `main_window.py::_on_photo_query_ready()` affiche un message dans la grille
  (`ThumbnailGrid.show_empty_message()`) avec un bouton « Ouvrir avec un lecteur
  externe » quand un dossier sélectionné ne contient aucune photo cataloguée mais est
  une copie de DVD — cas résiduel (DVD non encore scanné, ou copie incomplète sans
  `.VOB` exploitable). Réutilise `tools.external_apps` (même config que
  `PhotoViewer._open_with`) via `subprocess.Popen([app_path, folder_path])` ; ne
  propose que les applications de portée `"video"`/`"both"` (cf. ci-dessous),
  jamais celles limitées à `"image"`.

Chaque entrée de `tools.external_apps` (menu Outils › Applications externes…,
`main_window.py::_open_external_apps_dialog`) porte une portée média optionnelle
`"media"` : `"image"`, `"video"` ou `"both"` (absente = `"both"`, rétrocompatible avec
les configs antérieures à cette fonctionnalité). `PhotoViewer.refresh_external_apps()`
compare cette portée au `media_type` de la photo affichée pour ne montrer l'icône de
l'application dans la barre de la visionneuse que si elle est pertinente (ex. VLC en
`"video"` n'apparaît plus quand on visionne une photo fixe) ; le conteneur
`_ext_apps_container` est masqué entièrement si aucune application ne correspond.
`refresh_external_apps()` est appelé à chaque `set_photo()` (navigation) en plus du
changement de config, pour recalculer ce filtrage à chaque photo affichée.

### Retouches non destructives

Les retouches ne modifient jamais les fichiers originaux. Les ajustements sont stockés dans `%LOCALAPPDATA%\PixelPhotoManager\edits.db` (SQLite) et appliqués à la volée (affichage, export). L'original est toujours récupérable.

- `src/processing/edit_database.py` — `EditDatabase` : table `photo_edits` (état courant) + table `edit_history` (historique persistant, 50 entrées max par photo)
- L'historique est rechargé depuis la DB à l'ouverture d'une photo → undo/redo persistant entre sessions
- Le bouton **Appliquer** dans `EditPanel` déclenche `EditDatabase.save()`

**Piège largeur minimale** : la grille de boutons de traitement (2 colonnes —
Contraste, Vignette… en colonne 2) est hébergée dans une `QScrollArea`, qui ne
propage jamais le `minimumSizeHint()` de son widget interne vers le sien
(comportement Qt voulu, pour permettre un contenu plus grand que la vue). Sans
plancher explicite (`scroll.setMinimumWidth(...)` posé dans `_setup_ui`,
doublé par `EditPanel.content_min_width()` recalculé à la demande — le style
Qt applicatif n'est pleinement résolu qu'après le premier affichage, une
valeur figée à la construction sous-estime la largeur réelle des boutons une
fois stylés), rien n'empêche le splitter de comprimer le panneau sous cette
largeur : la colonne 2 devient invisible et inatteignable au clic,
silencieusement, pour un utilisateur réel (pas seulement un artefact
d'automation e2e). `main_window.py::_ensure_left_pane_min_width()` interroge
`content_min_width()` pour dimensionner le splitter — appelé aussi au
changement de page du `QStackedWidget` gauche, qui ne déclenche pas de
relayout de lui-même. Test de régression dédié (géométrie directe, sans
automation OS) : `tests/gui_widgets/test_edit_panel.py::TestEditPanelContentMinWidth`.
`content_min_width()` compte aussi la largeur de l'ascenseur vertical
(`_vertical_scrollbar_width()`) : le contenu du panneau dépasse toujours la
hauteur disponible, la barre est donc présente en pratique et mange d'autant le
viewport — ajouter un traitement à `_TREATMENTS` (donc une ligne de grille) a
suffi à faire ressortir la colonne 2 sans elle.

### Cadres décoratifs

`src/processing/frames.py` — 9 motifs (`FRAME_TYPES` : entourage uni, simple, double,
feuilles de vigne, roses, sculpture bois, métallique, reflets, fleurs), rendus
procéduralement en PIL/numpy, sans aucun fichier d'image externe. Réglages
(`PARAMETRIC_FRAMES` = plain/simple/double) : style de couleur (`COLOR_STYLES` : uni,
dégradé, pailleté), largeur extérieure, intervalle, largeur intérieure — toutes les
largeurs sont des **fractions du petit côté** de la photo (exposées en pourcentage dans
l'UI, `EditSlider` ayant une échelle interne figée à 100).

`plain` (« Entourage uni ») est le seul motif **sans relief** : aplat strict de
`frame_color` (raccourcis noir/blanc via `QUICK_COLORS` dans le dialogue), sans biseau
ni liseré — c'est ce qui le distingue de `simple`. Il est donc dans `PARAMETRIC_FRAMES`
(largeur + couleur réglables) mais **pas** dans `STYLED_FRAMES` = simple/double (les
seuls à exposer `COLOR_STYLES` et `frame_color2`). Ne pas lui appliquer `_bevel()` : le
noir demandé ne serait plus un vrai noir.

`plain` accepte en plus un **second cadre facultatif** (`frame_inner_enabled`, colonne
`edits.db` ajoutée par `_MIGRATE_FRAME`, désactivé par défaut) réutilisant `frame_gap` et
`frame_inner_width`. C'est la **seule** dérogation à l'invariant ci-dessous : il est peint
PAR-DESSUS la photo (`_draw_inner_overlay`, après le `paste`), à `frame_gap` du bord, la
bande d'image laissée visible entre les deux cadres étant l'effet recherché. Il n'entre
donc **pas** dans `border_px()`/`content_box()` (le canevas ne grandit que du cadre
extérieur) et la géométrie des outils interactifs reste celle de la photo entière —
`inner_overlay_px()` est un calcul d'affichage, jamais une donnée de géométrie.

Ce second cadre porte une **ferronnerie** (`INNER_MOTIFS` : `line` ligne simple,
`corners` volutes d'angle, `scrolls` rinceaux courants, `twist` barreau torsadé,
`studs` clous forgés — colonnes `frame_inner_motif`/`frame_inner_relief`/
`frame_inner_ornament`), rendue en relief léger ou en aplat strict et dimensionnée par
le curseur « Ornements » (facteur borné à `[INNER_ORNAMENT_MIN, INNER_ORNAMENT_MAX]`,
exposé en pourcentage). Trois règles à respecter :
- `line` est le **défaut** et reste un aplat strict : il ignore `frame_inner_relief` et
  est dessiné directement sur le canevas à pleine résolution (aucun flou de
  redimensionnement) — une base migrée doit rendre exactement le cadre d'avant la
  fonctionnalité. Relief et curseur ne concernent donc que `ORNAMENTED_MOTIFS`, et le
  dialogue masque les deux réglages pour `line`.
- Les ornements se développent **vers l'intérieur** depuis la ligne : ils restent dans
  la photo, laissent propre la bande de `frame_gap` et n'entrent jamais dans
  `border_px()`/`content_box()`. Leur calque (`_inner_motif_layer`) fait exactement la
  taille de la photo et est collé en `(border, border)` — c'est ce qui rend le
  débordement impossible par construction.
- Le calque est rendu à résolution de travail bornée × suréchantillonnage puis réduit
  une seule fois (même approche que `_ornament_layer`) : ~0,8 s sur un export
  6000 × 4000, contre 0,43 s pour `line`. Un échec de rendu est rattrapé par un simple
  anneau (`_draw_inner_overlay`), jamais par la perte du cadre.

**Invariant** : `apply_frame()` colle la photo **en dernier** sur un canevas agrandi —
le cadre ne recouvre jamais un pixel de l'image, il s'ajoute autour. Corollaire : le
pixmap affiché est plus grand que la photo, et toute coordonnée relative (recadrage,
yeux rouges, vignette, annotations, bbox de visage) se rapporte au **contenu**, pas au
pixmap. `viewer_canvas._img_rect()` retire donc la bordure (`_frame_border_px()` →
`frames.content_box()`, inverse exact de `border_px()` — un pixel d'écart décalerait
tous les outils). Ne jamais recalculer une position à partir de `self._pixmap.width()`
dans le canvas : passer par `_img_rect()`.

`ImageAdjuster.apply_all(image, edit, with_frame=True)` pose le cadre en dernier.
L'export (`main_window.py`) passe `with_frame=False` puis appelle `apply_frame()`
lui-même **après** `composite_annotations_pil()` — les annotations sont en coordonnées
de contenu, elles doivent être composées avant l'agrandissement.

UI : `src/ui/frame_dialog.py::FrameDialog` — galerie d'aperçus de la photo courante
(un par motif, rendus dans un `_TileLoader(QThread)` réutilisant une image de base
décodée une seule fois), réglages visibles seulement pour les motifs paramétriques
(style de remplissage et seconde couleur réservés à `STYLED_FRAMES` ; ferronnerie
réservée au second cadre de `plain`, relief et « Ornements » aux `ORNAMENTED_MOTIFS`), aperçu
temps réel via `preview` → `EditPanel._on_preview`. Le panneau ne modifie `self._edit`
qu'à la validation, pour que `_push_undo` empile bien l'état d'avant.

### Menus — largeur des popups et énumération des sous-menus

`src/ui/ui_utils.py` expose `install_menu_width_fix(menu_ou_barre)` : à l'ouverture
du popup (`aboutToShow`), la largeur nécessaire est recalculée
(`menu_required_width()`) et posée en `minimumWidth`. Sans ça, le style natif
Windows réserve la colonne du raccourci au plus juste et un libellé long passe
**sous** son raccourci (cas vécu : « Exporter la sélection vers un dossier… » +
`Ctrl+Shift+E`). Le calcul additionne le chrome de l'item — mesuré en interrogeant
le style lui-même (`sizeFromContents(CT_MenuItem)` sur un texte de largeur connue,
ce qui capte aussi le padding d'une feuille de style) — puis libellé + séparation +
raccourci. Il reste donc calé sur le `sizeHint` de Qt pour les menus sans
raccourci, qui ne sont pas élargis.

La séparation libellé ↔ raccourci est fixée par `_SHORTCUT_GAP_EM` (4 largeurs de
« M ») : c'est le seul réglage à toucher pour aérer ou resserrer la colonne des
raccourcis. Le raccourci étant aligné à droite du popup, toute largeur ajoutée là
se retrouve intégralement dans cet espace ; les menus sans raccourci n'en voient
rien. Test : `test_menu_width.py::TestMenuRequiredWidth::test_shortcut_column_is_aired`.

À brancher sur **chaque** menu susceptible d'afficher un raccourci — via
`QAction.setShortcut()` comme via la convention « Libellé\tTouche » des menus
contextuels : la barre de menus (`main_window.py`, un seul appel couvre ses menus
et leurs sous-menus) et chaque `QMenu(self)` contextuel. Les sous-menus sont
branchés à la volée à l'ouverture de leur parent, donc les menus reconstruits
dynamiquement (Noter, applications externes…) sont couverts sans appel dédié.
Ne pas remplacer `QMenu(self)` par une fabrique maison : plusieurs tests
substituent `QMenu` dans l'espace de noms du module pour intercepter `exec()`
(`tests/gui_widgets/test_album_mode_no_delete.py`).

**Piège PySide6 6.11** : `QAction.menu()` renvoie un wrapper dont la collecte
**détruit le QMenu C++** (sous-menu vidé, puis `RuntimeError: Internal C++ object
already deleted` au prochain accès). Énumérer les sous-menus d'un QMenu/QMenuBar
uniquement via `findChildren(QMenu, Qt.FindDirectChildrenOnly)`
(`ui_utils._submenus()`), jamais via `QAction.menu()`. Test :
`tests/gui_widgets/test_menu_width.py`.

### Cache vignettes à trois niveaux

`src/library/thumbnail_cache.py` — RAM LRU (500 entrées, ~50 Mo) → SQLite → génération à la demande dans un thread. Ne jamais générer de vignettes dans le thread UI.

Pour les vidéos, `generate()` délègue à `_generate_video_thumb()` : `cv2.VideoCapture` → seek à 10 % de la durée → frame BGR→RGB → PIL → JPEG.

### Gestionnaire de dossiers

`src/ui/folder_manager_dialog.py` — `FolderManagerDialog(QDialog)` — accessible via **Outils › Dossiers…**.

- Affiche tous les dossiers surveillés avec statut (✓/✗), nombre de fichiers, sous-dossiers ignorés (cachés, Originals).
- Signaux : `rescan_requested(str)`, `folder_removed(str)`, `folder_added(str)`.
- Le re-scan forcé passe par `LibraryScanner.scan(folders, force=True)` → `ScanThread(force=True)` → `known = {}` (bypass du cache mtime).
- `folder_removed` est traité par `MainWindow._on_folder_removed()` : confirmation (nombre de photos affecté) puis `_purge_catalog_for_folder()` supprime les photos du catalogue, les vignettes (`ThumbnailCache.invalidate`) et les visages/`indexed_photos` (`FaceDatabase.delete_for_path`) pour ce dossier. Les fichiers restent intacts sur le disque.

### Suppression — toujours via la corbeille Windows

`src/library/trash.py` est le **point unique** de suppression d'un fichier
utilisateur : `move_to_trash(path)` (wrapper `send2trash`, `os.path.normpath`,
lève `FileNotFoundError` si absent) et `is_trash_available()`. Règle absolue :
l'application n'efface **jamais** définitivement un fichier utilisateur — en cas
d'échec (lecteur réseau, volume sans corbeille → `TrashPermissionError`/`OSError`),
l'exception remonte à l'appelant, qui doit informer l'utilisateur que le fichier
n'a **pas** été supprimé (jamais de repli `unlink`/`rmtree` silencieux). Sites
concernés : `background_workers.py::_DeleteWorkerThread` (grille, visionneuse,
fichiers corrompus), `sidebar.py::_delete_folder` (suppression de dossier, dans
un QThread — un `rmtree` direct bloquerait le thread UI), `face_backup_dialog.py`
(suppression d'une archive de sauvegarde). Les fichiers **temporaires internes**
de l'application (tempfile, dossiers `_restore_tmp…`) restent en `unlink` direct —
non concernés par cette règle, ce ne sont pas des fichiers utilisateur.

### Détection de doublons — continue et incrémentale

`src/library/duplicate_detector.py` (`DuplicateDetectorThread`) se déclenche automatiquement après chaque scan (`MainWindow._on_scan_finished()` → `_start_duplicate_detection()`), sur le même principe que l'indexation des visages : pas de bouton manuel, pas de rapport de fin. Le menu **Outils › État des doublons…** (`MainWindow._show_duplicate_status_dialog()`) affiche un instantané en lecture seule (nombre de groupes/photos, dernière vérification, fichiers corrompus) avec un bouton **Vérifier maintenant** pour forcer une passe.

Deux niveaux (Tier 1 pHash, Tier 2 ORB+RANSAC pour les recadrages) — voir le docstring du module. La comparaison **par paires** (pas seulement le calcul pHash/ORB par fichier, déjà caché par mtime) est vraiment incrémentale grâce à deux tables `compared_tier1`/`compared_tier2` (`src/library/dedup_cache.py`) qui tracent quels chemins ont déjà été intégralement comparés au reste de la bibliothèque connue — seules les paires nouveau×ancien et nouveau×nouveau sont réévaluées, jamais ancien×ancien.

`DuplicateDetectorThread` prend un paramètre `seed_groups: dict[path, group_id]` (typiquement `Catalog.get_duplicate_group_assignments()`) pour amorcer `group_of` sans tout recomparer. **Piège** : relancer le thread sur un `cache_db_path` déjà peuplé **sans repasser `seed_groups`** fait que toutes les paires apparaissent comme « déjà comparées » et qu'aucun groupe n'est reformé — retour silencieux de `{}` au lieu d'une erreur. En usage réel (`main_window.py`), `seed_groups` est toujours récupéré frais avant chaque création de thread ; seul un nouveau test/script qui relance `_detect()` plusieurs fois sur le même cache doit y penser explicitement.

Conséquence de l'incrémentalité : `Catalog.ignore_duplicate_group()` (dissoudre un groupe, bouton ✕ de la grille des doublons) est maintenant **persistant** — un groupe ignoré n'est plus jamais recréé tant qu'aucun de ses membres ne change (ils sont déjà dans `compared_tier1`/`_tier2`, donc jamais recomparés entre eux). Un nouveau fichier correspondant à l'un d'eux reste détecté normalement (comparaison new×old).

### Visages — deux étages de filtrage par taille

`src/faces/detector.py::detect_and_embed()` exclut définitivement (visage jamais écrit en base) : `det_score < 0.5`, `embedding is None`, ou `w < 20 / h < 20` px. Ne pas y ajouter de seuil d'aire relatif à l'image — ça a déjà causé un bug (visages valides supprimés silencieusement, sans trace ni rattrapage possible).

`src/faces/face_database.py::save_faces()` marque ensuite `ignored=1` (visage conservé en base, masqué de l'UI/clustering, **récupérable**) selon un seuil proportionnel à la résolution de la photo et `_AUTO_IGNORE_MIN_SCORE` (0.65). Seuil de taille : un visage qualifie la photo de "premier plan" s'il atteint `_AUTO_IGNORE_MIN_SIDE_FG_RATIO` (20 % du plus petit côté de la photo, ou 2× le seuil de base si plus grand). Si au moins un visage premier plan est présent, tout visage plus petit que `_AUTO_IGNORE_FG_FRACTION` (1/4) du plus petit visage premier plan est ignoré. Sinon (aucun premier plan), seuil de base `_AUTO_IGNORE_MIN_SIDE_RATIO` = 3 % du plus petit côté. C'est le seul étage qui doit décider si un petit visage est bruit ou non — `FaceDatabase.recalculate_size_ignored()` implémente la même règle mais n'est actuellement rattachée à aucune entrée de menu (code orphelin, cf. `RevaluateSizeIgnoredThread` dans `face_indexer.py`).

### Visages — paliers de confiance de la reconnaissance (visage vs personne connue)

`src/faces/face_database.py` compare la similarité cosinus d'un visage (ou du centroïde
d'un groupe) aux centroïdes des personnes déjà nommées, à trois paliers croissants :
- `< 0.55` : aucune action automatique (visage non identifié).
- `_SIM_SUGGEST = 0.55` : suggestion enregistrée (`suggestion_person_id`/`suggestion_score`)
  → le groupe apparaît « en attente de vérification » chez la personne concernée, à
  confirmer manuellement.
- `_SIM_AUTO_ASSIGN = 0.70` : allocation automatique de la personne, **sans confirmation**
  (mêmes effets de bord que `accept_cluster_suggestion` : dédup, consommation des
  annotations Picasa en attente).

`set_cluster_suggestions()` est le point d'entrée unique qui applique cette bascule pour
les 4 producteurs de suggestions (`resuggest_clusters`, `find_similar_to_persons`,
`isolate_and_suggest`, l'auto-promotion de `face_cluster_workers.py`) — idempotent dans
les deux branches (`WHERE person_id IS NULL AND suggestion_person_id IS NULL`), un cluster
déjà assigné ou déjà suggéré n'est jamais réécrit par un appel ultérieur, quel que soit
le palier atteint.

`_SIM_STRONG = 0.50` (`src/ui/people_panel.py`) est un seuil **distinct**, purement
d'affichage (libellé bleu « Probablement X » vs gris « Peut-être X » à `_SIM_WEAK = 0.45`)
pour les visages qui n'ont pas encore atteint `_SIM_SUGGEST` — ne pas le confondre avec les
seuils ci-dessus ni avec `_SIM_GROUP` (0.72, seuil d'auto-groupement de clusters
*non identifiés* entre eux, sans rapport avec la correspondance à une personne connue).

### Visages — cache des centroïdes personne (popup d'assignation de nom)

`src/faces/face_database.py::get_all_person_centroids()` décode les embeddings (512D float32) de tous les visages identifiés pour calculer le centroïde de chaque personne — jusqu'à ~60k visages sur une grosse bibliothèque, plusieurs secondes en pur Python. Le résultat complet est mis en cache en mémoire (`self._person_centroid_cache`) et réutilisé tant qu'un fingerprint bon marché (`SELECT COUNT(*), SUM(person_id) FROM faces WHERE person_id IS NOT NULL`, quelques ms via `idx_faces_person`) n'a pas changé — le `SUM` est nécessaire en plus du `COUNT` pour détecter les réassignations (`merge_persons`) qui ne changent pas le nombre de lignes. Le décodage lui-même est vectorisé via `numpy.frombuffer` plutôt que `struct.unpack` (facteur ~10). `enrich_persons()` (photo_count + cover_path/cover_bbox + pending_count) est également coûteux (~1 s, dominé par une CTE avec fenêtrage pour la photo de couverture) ; `enrich_persons_photo_count()` en est une variante allégée (photo_count seul) à utiliser partout où la couverture n'est pas affichée, ex. la popup d'assignation.

Dans `src/ui/face_panel.py`, la popup d'assignation de nom (`_AssignDialog`) est préparée par `_AssignPrepLoader(QThread)` (get_persons + enrich_persons_photo_count + suggestion de personne par similarité cosinus) avant d'être ouverte, pour respecter la règle "l'UI ne bloque jamais" ci-dessous — `face_cluster_grid.py::_PersonsLoader` suit le même principe pour la vue en grille de groupes.

### Albums

`src/library/catalog.py::delete_album(album_id)` supprime un album (table `albums` + `album_photos`) sans toucher aux photos. Accessible via menu contextuel sur `Sidebar._albums_list` (`sidebar.py::_album_context_menu()`), qui exclut les 4 albums spéciaux (Chronologie/Favoris/Vidéos/Par nom de fichier) via `isinstance(item.data(Qt.UserRole), AlbumInfo)`.

### Règle de performance : l'UI ne bloque jamais

Toute opération > 50 ms passe dans un `QThread`. Les signaux PySide6 (`pyqtSignal`) sont le seul moyen de communiquer du thread secondaire vers l'UI.

Corollaire : **chaque action utilisateur a un retour visuel immédiat**, même quand le résultat réel arrive en asynchrone. Mécanismes en place (2026-07) :
- **Visionneuse** — `PhotoViewer._base_lru` : LRU (8 entrées) des images de base 1024 px, clé = chemin. `prefetch()` (appelé par `MainWindow._prefetch_viewer_neighbors()` après chaque navigation) précharge les voisines ±1/±2 → prev/next instantané. Sur cache froid, la vignette de la grille (`thumb_cache.get_ram`) sert de placeholder immédiat (flou bref, jamais d'écran noir). `_apply_edit_to_base` a un fast path sans retouche (décodage JPEG direct par Qt, sans aller-retour PIL). Le cache est invalidé quand le fichier change sur disque (`invalidate_base_cache` : export écrasant, réécriture EXIF).
- **Grille** — `ThumbnailGrid.set_loading(True)` au départ d'une requête photo (`_start_photo_query`) : indicateur « Chargement… » différé de 150 ms (pas de clignotement si la requête répond vite), masqué par `set_photos()`/`clear()`.
- **Panneau Visages** — curseur occupé pendant `_AssignPrepLoader` (préparation du dialogue d'assignation) ; après validation du dialogue, le libellé du/des visage(s) est mis à jour **optimistiquement** et l'écriture DB (assignation + dédup + consommation Picasa, potentiellement longue sur un gros groupe) part dans un `_DbWriteWorker` — le rafraîchissement complet (`person_assigned` + `set_photo`) n'a lieu qu'à la fin du worker. Le compte de « Visages ignorés » est calculé dans `_FacesDataLoader` (plus de requête DB sur le thread UI à chaque navigation).

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

**Ordre des colonnes de `photos`** (`catalog.py::_CREATE_PHOTOS`) : `_photo_from_row()`
unpacke la ligne **positionnellement** (`*rest` pour les colonnes ajoutées après
`media_type`/`duration`) — toute nouvelle colonne s'ajoute **en fin** de
`_CREATE_PHOTOS` (et de la migration `ALTER TABLE` correspondante), jamais au
milieu, sous peine de décaler silencieusement tous les champs suivants sur une
base migrée depuis une version antérieure.

**Pas de nouvelle colonne éditable par l'utilisateur dans le `DO UPDATE`** de
`add_or_update_photo()` (`ON CONFLICT(path) DO UPDATE SET ...`) : `tags` et
`rating` sont volontairement **absents** de cette clause (comme `is_favorite`
avant eux) — un re-scan forcé (`FolderManagerDialog` → `scan(force=True)`) doit
reconstruire les champs EXIF/fichier mais ne jamais écraser une donnée saisie par
l'utilisateur. Toute future colonne du même genre (éditable en dehors du scan)
suit ce même pattern : présente dans l'`INSERT`, absente du `DO UPDATE`.

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
| send2trash | Suppression via la corbeille Windows (`src/library/trash.py`) |
| pillow-heif | Décodage HEIC/HEIF (`src/library/image_loader.py`) |
| rawpy | Décodage RAW — CR2/NEF/ARW/DNG/ORF/RW2 (`src/library/image_loader.py`) |

Les dépendances IA (PyTorch, DeepFace, Real-ESRGAN…) sont **optionnelles** et commentées dans `requirements.txt`. Ne pas les imposer au cœur de l'application — les isoler dans des plugins.

`scikit-learn` et `hdbscan` (clustering des visages, `src/faces/clusterer.py`) sont en revanche des dépendances **non optionnelles** du cœur de l'application : ne jamais les ajouter à `excludes` dans `pixelphotomanager.spec`, sous peine de `ModuleNotFoundError: sklearn` uniquement dans l'exécutable packagé (le mode Python dev n'est pas affecté).

`insightface` doit figurer dans `_with_data` (liste `collect_all`) de `pixelphotomanager.spec` **ET** son dossier `data/objects/` doit en plus être copié explicitement à la racine du bundle sous le nom `objects` (`datas += [(str(Path(insightface.__file__).parent / "data" / "objects"), "objects")]`). Raison : `insightface/data/pickle_object.py::get_object()` résout le chemin différemment selon le mode :
- mode dev : `Path(__file__).parent / "objects"` → `insightface/data/objects/` (arborescence normale du package, ce que `collect_all()` seul reproduit dans l'exe figé sous `_internal/insightface/data/objects/`) ;
- mode figé (`sys.frozen`) : `sys._MEIPASS / "objects"` → un dossier **`objects` à la racine du bundle** (`_internal/objects/`), complètement différent de l'arborescence du package.

`collect_all("insightface")` seul ne suffit donc PAS : il place bien `meanshape_68.pkl` dans l'exe figé, mais au mauvais endroit (`_internal/insightface/data/objects/`), jamais consulté par le code en mode figé. Sans la copie supplémentaire vers `_internal/objects/`, `get_object('meanshape_68.pkl')` renvoie `None` en silence (juste un `print()`, invisible en mode `console=False`), et **chaque** visage détecté fait planter `InsightFace.get()` avec `AttributeError: 'NoneType' object has no attribute 'shape'` (dans `insightface/utils/transform.py::estimate_affine_matrix_3d23d`, appelé depuis `landmark.py::get()` pour le modèle `landmark_3d_68`, estimation de pose). Piège perfide : la détection réussit (bbox trouvée), seul ce post-traitement landmark/pose échoue, donc ça ressemble à un bug de détection alors que c'est un problème d'empaquetage de données — et une correction partielle (juste `collect_all`) ne change rien à l'erreur observée, ce qui peut faire croire à tort que le vrai problème est ailleurs.

Le pack de modèles `buffalo_l` (détection SCRFD + embedding ArcFace, ~340 Mo) est lui aussi embarqué dans le bundle, sous `insightface_root/models/buffalo_l` (`pixelphotomanager.spec`, source = `~/.insightface/models/buffalo_l` de la machine de build — il faut donc avoir lancé l'appli au moins une fois en mode dev pour l'avoir en cache localement avant de builder). `src/faces/detector.py::_insightface_root()` pointe `FaceAnalysis(root=...)` dessus en mode figé. Sans ça, `insightface` tente de télécharger le pack depuis GitHub au 1er lancement sur chaque poste — silencieux et invisible tant qu'il y a un accès Internet, mais **totalement bloquant sans accès à github.com** (pare-feu, poste isolé) : reconnaissance faciale inopérante à 100 % (0 visage détecté, quel que soit le nombre de photos), avec un nouveau essai de téléchargement complet à *chaque photo* puisque le modèle n'est jamais mis en cache.

`main.py` redirige `sys.stdout`/`sys.stderr` vers `os.devnull` au tout début s'ils valent `None` (cas d'un exe `console=False` : toute bibliothèque qui y écrit, comme `tqdm` utilisé par `insightface` pendant un téléchargement, plante avec `AttributeError: 'NoneType' object has no attribute 'write'`). Ce crash était particulièrement pernicieux avec le téléchargement du pack `buffalo_l` : la requête HTTP aboutissait bien (200 OK), mais `tqdm` plantait pendant l'écriture de la barre de progression, interrompant le flux **avant** l'écriture du fichier sur le disque — le modèle n'était donc jamais mis en cache, et le run suivant retentait un téléchargement complet, en boucle.

`pillow-heif` et `rawpy` (décodage HEIC/RAW, cf. `src/library/image_loader.py`)
figurent eux aussi dans `_with_data` (`collect_all`) de `pixelphotomanager.spec` —
jamais dans `excludes` — pour embarquer leurs bibliothèques natives (libheif,
libraw). Contrairement au pack `buffalo_l` d'insightface, aucune copie manuelle
supplémentaire n'est nécessaire : `collect_all()` seul suffit pour ces deux
packages (vérifié par un `collect_all()` à blanc : datas/binaries non vides pour
les deux).
