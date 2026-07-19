# Tests bout-en-bout (Layer 3)

Ces scénarios pilotent la vraie application (`main.py`, sous-processus réel)
via [pywinauto](https://pywinauto.readthedocs.io/) (backend UIA), contre une
bibliothèque photo **synthétique et jetable** générée par
`tools/test_env/generate_library.py`, avec un `%LOCALAPPDATA%` **isolé** du
profil réel de l'utilisateur (voir `tools/test_env/launch_isolated.py`).

Aucun test de ce dossier ne touche au catalogue, à la config, aux vignettes ou
à la base de visages réels. Le dossier d'export par défaut de l'application
(`~/Pictures/PixelPhotoManager/Export`) est explicitement écrasé dans
`test_export.py` pour la même raison — ne jamais retirer cet override.

## Prérequis

Windows uniquement. `pywinauto` n'est **pas** dans `requirements.txt` (Layers
1+2, multiplateforme) : c'est une dépendance séparée et optionnelle.

```powershell
.venv\Scripts\pip.exe install -r requirements-test-e2e.txt
```

Sans `pywinauto` installé, `tests/e2e/conftest.py` retire automatiquement
`scenarios/*` de la collecte (`collect_ignore_glob`) — `pytest tests/` continue
de fonctionner normalement (Layers 1+2 seulement).

## Lancer les scénarios

```powershell
# Les 4 scénarios (lent : chaque test lance une vraie instance de l'appli,
# scan complet + indexation faciale InsightFace inclus — plusieurs minutes)
.venv\Scripts\python.exe -m pytest tests/e2e -m e2e -v

# Un seul scénario
.venv\Scripts\python.exe -m pytest tests/e2e/scenarios/test_duplicate_detection.py -m e2e -v
```

`pytest tests/` (commande par défaut documentée dans `CLAUDE.md`) **n'exécute
pas** ces scénarios : `pytest.ini` définit `addopts = -m "not e2e"`. Il faut
systématiquement `-m e2e` (ou `-m ""` pour tout inclure) pour les déclencher.

## Couverture de code des scénarios

`--cov` ne voit pas le code exécuté par l'application (sous-processus). Pour
que les scénarios créditent la couverture du code UI :

```powershell
$env:PPM_E2E_COVERAGE='1'
.venv\Scripts\python.exe -m pytest tests/e2e -m e2e
.venv\Scripts\python.exe -m coverage combine
.venv\Scripts\python.exe -m coverage report
```

`launch_isolated.py` lance alors l'appli via `coverage run` (un fichier
`.coverage.<hôte>.<pid>` par lancement, cf. `parallel = true` dans
`.coveragerc`), et `_graceful_close()` (conftest) ferme la fenêtre proprement
avant le `terminate()` de secours — un TerminateProcess n'exécuterait pas le
hook atexit qui écrit les données. Ordre de grandeur : les seuls 2 tests de
`test_scan_and_browse.py` créditent `main_window.py` à ~35 % et
`thumbnail_grid.py` à ~49 %.

## Ce que chaque scénario vérifie

| Fichier | Vérifie |
|---|---|
| `test_scan_and_browse.py` | Scan automatique au démarrage (pas d'onboarding, `config.json` pré-rempli) → toutes les photos synthétiques cataloguées, `media_type` correct. Fichier corrompu : le scan ne doit pas planter (sort exact non fixé — point à enrichir). |
| `test_duplicate_detection.py` | **Scénario phare** — régression directe du bug `Signal(dict)` avec clés int corrigé en 2026-07 (voir `bugfix_signal_dict_int_keys_2026-07.md`) : menu Outils › Détecter les doublons… → confirmation → thread réel → assertion sur `duplicate_group_id` en base. Couvre Tier 1 (pHash : paires exacte + redimensionnée) et Tier 2 (ORB/RANSAC : paire recadrée). |
| `test_edit_nondestructive.py` | Retouche Luminosité via la vraie UI (`LuminositeTreatmentDialog`) → persistance dans `edits.db` → Ctrl+Z (bouton "Annuler") → annulation persistée → re-navigation vers la même photo → confirmation que l'état persiste bien depuis la DB (pas seulement en mémoire). |
| `test_export.py` | Retouche + export → le fichier `.jpg` produit incruste bien la retouche (delta de luminance moyenne mesuré) et conserve les dates du fichier original (`preserve_file_dates`). |

## Mécanique de synchronisation : sonder les DB, pas l'UI

`ThumbnailGrid` est peinte à la main (aucun texte natif exploitable par UIA
pour ses cellules) et les opérations longues (scan, détection de doublons,
indexation faciale) n'ont pas de marqueur UI fiable à attendre. Le mécanisme
**principal** de synchronisation est donc `wait_for_condition()`
(`tests/e2e/conftest.py`) qui sonde directement `catalog.db`/`edits.db` via
`query_one()` — une vraie connexion `sqlite3.connect()` en lecture, en
parallèle du process applicatif qui écrit dans le même fichier.

`wait_for_log()` (tail de `logs/pixelphotomanager.log` depuis un offset
capturé via `log_offset()`) est fourni en repli pour un évènement qui n'a pas
de trace en base.

## Cibler des éléments UIA précis

- **Boutons / dialogues natifs** (`QMessageBox`, boutons de `QDialog`) :
  `find_dialog_button(window, texts, exact=False)`. Point établi
  empiriquement cette session : les `QDialog`/`QMessageBox` Qt sur Windows
  apparaissent comme des **descendants** de la fenêtre principale dans l'arbre
  UIA (fenêtres "owned"), jamais comme fenêtres top-level séparées — ne
  jamais utiliser `app.window(title=...)` pour les cibler, toujours
  `window.descendants(...)`.
- **Vignettes précises** : `find_thumbnail(window, photo_path)`, qui s'appuie
  sur le nom accessible `thumb::<chemin>` posé délibérément sur
  `ThumbnailCell` (`src/ui/thumbnail_grid.py::_setup_ui`) — seul moyen fiable
  de cibler une vignette précise dans la grille virtuelle peinte à la main.
- **Sliders** (`EditSlider` → `MarkedSlider` → `QSlider` réel) :
  `window.descendants(control_type="Slider")` + `.set_value(int(v * 100))`
  (l'échelle interne de `EditSlider` est toujours ×100).
- **Champs de texte** (ex. dossier de destination d'export) :
  `window.descendants(control_type="Edit")`, désambiguïsé par le **contenu**
  du champ plutôt que par position (plusieurs `QLineEdit` peuvent coexister
  à l'écran, ex. le panneau EXIF pendant la visionneuse).

## Ajouter un scénario

1. Nouveau fichier dans `scenarios/`, `pytestmark = pytest.mark.e2e` en tête.
2. Fixture `isolated_app` (function-scoped — un process applicatif frais et
   une copie dédiée de la bibliothèque synthétique par test, pour que les
   scénarios destructifs — suppression, déplacement — ne polluent pas les
   autres) donne accès à `.window` (pywinauto), `.manifest`
   (`LibraryManifest`), `.catalog_db`, `.edits_db`.
3. Toujours vérifier l'état via les DB (`query_one`/`wait_for_condition`)
   plutôt que via le texte affiché à l'écran — plus robuste, et c'est la
   vraie source de vérité de l'application.
4. Ne jamais cliquer "en aveugle" sur des coordonnées écran devinées : passer
   par `find_dialog_button`/`find_thumbnail`/`descendants(control_type=...)`.

## Limites connues / dette assumée

- Pas de couverture e2e dédiée à la reconnaissance faciale (InsightFace/
  HDBSCAN) au-delà de son déclenchement automatique après chaque scan
  (`_on_scan_finished` → `_start_face_indexing`, qui tourne "en fond" pendant
  les 4 scénarios sans être vérifié directement) — portée v1 volontairement
  limitée, l'indexation faciale complète est lente et lourde (modèles IA).
- Pas de scénario dédié Gestionnaire de dossiers / Albums — portée v1 centrée
  sur scan, doublons, retouche non destructive, export (cf. bug de régression
  découvert cette session). À enrichir.
- `test_scan_and_browse.py::test_scan_reports_corrupted_file_as_repairable_or_skipped`
  ne fixe pas le sort exact du fichier corrompu (catalogué avec erreur vs.
  ignoré) — dépend de `file_repair.py`, non encore spécifié par ce scénario.
- Flakiness connue : vol de focus par les dialogues natifs Windows si une
  autre fenêtre a le focus au moment du clic ; UIA peut mettre quelques
  centaines de ms à exposer un contrôle nouvellement affiché (d'où les boucles
  de retente dans tous les helpers `find_*` plutôt que des lookups directs).
