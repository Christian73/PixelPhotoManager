# PixelPhotoManager — Document de Conception

**Version** : 1.2  
**Date** : 2026-06  
**Langage** : Python 3.11  
**Framework UI** : PySide6  
**Statut** : En développement actif

---

## 1. Vision du projet

PixelPhotoManager est un gestionnaire de photos desktop Windows conçu pour être **rapide, intuitif et extensible**. Son objectif est de redonner aux utilisateurs le plaisir de gérer leurs photos en local, sans cloud obligatoire, avec une interface fluide et des fonctionnalités intelligentes.

La philosophie centrale est **zéro friction**. L'application scanne vos dossiers, organise vos photos, reconnaît vos proches, et vous permet de les retoucher en quelques clics — sans formation préalable.

Ce qui distingue PixelPhotoManager des alternatives existantes est son architecture modulaire ouverte permettant à n'importe qui d'ajouter des fonctionnalités via des plugins Python simples, et l'intégration native des modèles d'IA modernes pour la reconnaissance faciale et la restauration de photos.

---

## 2. Philosophie de conception — Les principes fondateurs

Ces principes guident chaque décision de conception. Toute nouvelle fonctionnalité doit être évaluée à leur aune.

### 2.1 La rapidité avant tout

L'application scanne des milliers de photos en quelques secondes grâce à un indexage en arrière-plan non bloquant. Les vignettes s'affichent immédiatement même pendant le scan. La navigation entre photos est instantanée.

**Objectifs mesurables :**
- Démarrage de l'application : < 2 secondes
- Affichage de la grille après ouverture d'un dossier : < 500 ms (vignettes déjà en cache)
- Génération d'une vignette : < 100 ms
- Navigation entre photos (flèche suivante) : < 50 ms
- Résultats de recherche pendant la frappe : < 200 ms

Toute opération dépassant 100 ms s'exécute dans un thread secondaire. L'interface ne se fige jamais.

### 2.2 La découverte automatique — pas d'import destructif

PixelPhotoManager ne déplace pas les photos — il les découvre. Il scanne les dossiers configurés et les affiche tels quels, sans copier ni renommer quoi que ce soit. L'utilisateur retrouve ses photos exactement là où il les a rangées. C'est une différence fondamentale avec la plupart des gestionnaires modernes qui imposent un import.

**Principe :** les fichiers originaux sont intouchables. PixelPhotoManager est un outil de consultation et d'organisation, jamais de modification du système de fichiers (sauf en cas d'action explicite de l'utilisateur).

### 2.3 La reconnaissance faciale accessible

La reconnaissance faciale est accessible au grand public. Elle propose des groupes de visages similaires et demande simplement « qui est cette personne ? ». En quelques sessions de validation, PixelPhotoManager reconnaît tous les membres de la famille sur des milliers de photos.

### 2.4 Les retouches non destructives

PixelPhotoManager ne modifie jamais les photos originales. Tous les ajustements sont stockés dans la base de données et appliqués à la volée lors de l'affichage ou de l'export. L'original est toujours préservé et récupérable en un clic. L'information de modification est enregistrée dans un fichier caché dans le même dossier que la photo pour que si l'utilisateur modifie le nom ou l'organisation de ces répertoires, les modifications enregistrées restent valides.

### 2.5 L'interface épurée — la règle des 90/10

Une seule fenêtre, trois zones. Pas de menus complexes. Les outils les plus utilisés sont accessibles en un clic.

90 % des utilisateurs n'ont besoin que de 10 % des fonctionnalités. Ces 10 % doivent être immédiatement visibles. Les 90 % restants existent mais ne gênent pas.

### 2.6 La cohérence des interactions

Chaque action produit un retour visuel immédiat. Les états de chargement sont toujours indiqués. Les erreurs sont expliquées en français clair, avec une action corrective proposée.

---

## 3. Interface utilisateur — Spécification détaillée

### 3.1 Fenêtre principale — Layout général

```
┌─────────────────────────────────────────────────────────────────┐
│  [Logo]  Fichier  Affichage  Outils  Plugins  Aide    [Recherche]│  ← Barre de menus
├──────────┬──────────────────────────────────────────────────────┤
│          │  [Dossiers] [Personnes] [Albums] [Carte] [Timeline]  │  ← Onglets de vue
│ SIDEBAR  ├──────────────────────────────────────────────────────┤
│          │                                                      │
│ Arbre    │           GRILLE DE VIGNETTES                        │
│ des      │           (zone principale)                          │
│ dossiers │                                                      │
│          │  [photo1] [photo2] [photo3] [photo4] [photo5]        │
│ ─────── │  [photo6] [photo7] [photo8] [photo9] [photo10]       │
│          │                                                      │
│ Albums   │                                                      │
│ virtuels │                                                      │
│          │                                                      │
│          ├──────────────────────────────────────────────────────┤
│          │  234 photos · Trier: Date ▾ · Taille: ●──○   [⊞][≡] │  ← Barre d'état/contrôles
└──────────┴──────────────────────────────────────────────────────┘
```

**Dimensions par défaut :**
- Sidebar : 240 px (redimensionnable, masquable avec `F9`)
- Grille : espace restant
- Largeur minimale fenêtre : 900 px
- Hauteur minimale fenêtre : 600 px

### 3.2 Sidebar — Navigation

La sidebar contient trois sections séparées par des séparateurs glissables :

**Section Dossiers :**
- Arborescence fidèle à la structure Windows
- Icône de dossier modifiée si des photos non indexées sont détectées
- Clic sur un dossier = affiche son contenu dans la grille
- Clic droit = menu contextuel (Ajouter aux favoris, Scanner maintenant, Ouvrir dans l'Explorateur)
- Badge numérique indiquant le nombre de photos par dossier

**Section Albums :**
- Liste des albums virtuels créés par l'utilisateur
- Icône + pour créer un nouvel album
- Albums spéciaux automatiques : Récemment ajoutées, Favoris, Sans album
- Clic droit sur un album créé par l'utilisateur = suppression (après confirmation) ; les albums spéciaux ne sont pas supprimables. Les photos elles-mêmes ne sont jamais affectées.

**Section Personnes** (visible si reconnaissance faciale activée) :
- Vignette ronde + nom pour chaque personne reconnue
- Badge avec le nombre de photos
- Clic = filtre la grille sur cette personne

### 3.3 Grille de vignettes

**Comportement :**
- Taille des vignettes : ajustable via un slider en bas de fenêtre (4 tailles : petite 110px, normale 180px, grande 250px, très grande 350px)
- Scroll vertical infini avec chargement par lots de 50
- Sélection multiple : Ctrl+clic, Shift+clic, Ctrl+A
- Drag & drop pour glisser des photos vers un album dans la sidebar
- Double-clic = ouvrir la visionneuse
- Clic droit = menu contextuel

**Affichage sur la vignette :**
- Icône GPS (coin bas gauche) si la photo est géolocalisée
- Icône visage (coin bas droit) si des personnes sont reconnues
- Icône étoile (coin haut droit) si la photo est marquée favori
- Overlay de sélection bleu semi-transparent quand sélectionnée

**Menu contextuel (clic droit) :**
- Ouvrir
- Ouvrir dans la visionneuse
- Marquer comme favori / Retirer des favoris
- Ajouter à l'album... (sous-menu avec albums disponibles)
- Informations EXIF
- Retoucher
- Exporter...
- Révéler dans l'Explorateur
- ─────
- *(entrées ajoutées par les plugins actifs)*

### 3.4 Visionneuse plein écran

```
┌─────────────────────────────────────────────────────────────────┐
│ ← [Nom du fichier]                    [♡] [✏] [i] [↗] [✕]     │  ← Barre supérieure (auto-masquable)
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│                                                                 │
│                     PHOTO AFFICHÉE                              │
│                      (centré, fond noir)                        │
│                                                                 │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  ◀  [prev-3] [prev-2] [prev-1] [ PHOTO ] [next-1] [next-2]  ▶  │  ← Filmstrip (masquable F8)
└─────────────────────────────────────────────────────────────────┘
```

**Interactions :**
- Flèche gauche/droite : photo précédente/suivante (**s'arrête à la première/dernière, pas de boucle**)
- Molette : zoom centré sur la position du curseur
- Double-clic : zoom 100% / ajustement fenêtre
- Clic maintenu + glisser : panoramique quand zoomé
- Échap : retour à la grille
- F : marquer/retirer des favoris
- I : afficher/masquer le panneau EXIF
- F5 : démarrer le diaporama

**Support vidéo :**
- Les vidéos (`.mp4`, `.mov`, `.avi`, `.mkv`, etc.) s'affichent avec la première frame extraite.
- Le bouton **▶ Ouvrir la vidéo** ouvre le lecteur multimédia système.
- Le panneau de retouche est désactivé pour les vidéos.

**Panneau EXIF (touche `I` ou bouton `[i]`) :**
Panneau latéral togglé, exclusif avec le panneau Visages. Affiche :
- Appareil photo et objectif
- Date et heure
- ISO, vitesse d'obturation, ouverture, focale
- Résolution et taille de fichier
- Coordonnées GPS (si présentes)
- Pour les vidéos : résolution, fps, durée

**Menu contextuel (clic droit dans la visionneuse) :**
- Révéler dans l'Explorateur
- **Localiser sur la carte** — ouvre OpenStreetMap à la position GPS (grisé si pas de données GPS)

### 3.5 Panneau de retouche

Accessible depuis la visionneuse via le bouton `[✏]` ou depuis la grille via le menu contextuel.

```
┌──────────────────────────────────┐
│ ← Retouche : photo_001.jpg       │
├──────────────────────────────────┤
│ [Réinitialiser]  [Aperçu] [✓OK] │
├──────────────────────────────────┤
│ ▾ EXPOSITION                     │
│  Luminosité    ───●────  +12     │
│  Contraste     ──────●─  +34     │
│  Saturation    ────●───   -5     │
│  Gamma         ────●───  1.10    │
├──────────────────────────────────┤
│ ▾ COULEUR                        │
│  [Couleurs / N&B]                │
│   ☑ Noir & Blanc                 │
│   Rouge  ───●────  +0.30         │
│   Vert   ────●───   0.00         │
│   Bleu   ──────●─  -0.20         │
├──────────────────────────────────┤
│ ▾ DÉTAILS                        │
│  Netteté        ────●───   +20   │
│  Réduction bruit───●────    30   │
├──────────────────────────────────┤
│ ▾ GÉOMÉTRIE                      │
│  [Recadrer]   [Rotation libre]   │
│  [Redresser]  [Miroir H] [Mir V] │
├──────────────────────────────────┤
│ ▾ CORRECTION                     │
│  [Yeux rouges auto]              │
│  [Noir & Blanc]                  │
├──────────────────────────────────┤
│ ▾ PLUGINS                        │
│  (entrées des ProcessorPlugins   │
│   actifs s'insèrent ici)         │
└──────────────────────────────────┘
```

**Comportement des sliders :**
- Modification en temps réel avec aperçu instantané (< 100 ms pour ajustements basiques)
- Double-clic sur le slider = réinitialiser à la valeur par défaut
- Clic sur la valeur numérique = saisie directe au clavier
- Ctrl+Z / Ctrl+Y = undo/redo pas à pas

### 3.6 Raccourcis clavier — Référence complète

**Navigation globale :**

| Raccourci | Action |
|-----------|--------|
| `F9` | Afficher/masquer la sidebar |
| `F11` | Plein écran |
| `Ctrl+F` | Activer la recherche |
| `Ctrl+1` | Vue Dossiers |
| `Ctrl+2` | Vue Personnes |
| `Ctrl+3` | Vue Albums |
| `Ctrl+4` | Vue Carte |
| `Ctrl+5` | Vue Timeline |

**Grille de vignettes :**

| Raccourci | Action |
|-----------|--------|
| `Entrée` ou `Double-clic` | Ouvrir la visionneuse |
| `Ctrl+A` | Tout sélectionner |
| `Échap` | Désélectionner tout |
| `Suppr` | Supprimer la photo (confirmation requise) |
| `F2` | Renommer la photo |
| `Ctrl+C` | Copier le chemin du fichier |
| `+` / `-` | Agrandir / réduire les vignettes |
| `F5` | Démarrer le diaporama |

**Visionneuse :**

| Raccourci | Action |
|-----------|--------|
| `←` / `→` | Photo précédente / suivante (s'arrête aux extrémités) |
| `↑` / `↓` | Photo précédente / suivante (identique) |
| `+` / `-` ou molette | Zoom avant / arrière |
| `0` | Ajustement à la fenêtre |
| `1` | Zoom 100% |
| `F` | Marquer/démarquer favori |
| `I` | Afficher/masquer le panneau EXIF |
| `Espace` | Lecture/pause diaporama |
| `Échap` | Retour à la grille |

**Retouche :**

| Raccourci | Action |
|-----------|--------|
| `Ctrl+Z` | Annuler |
| `Ctrl+Y` | Rétablir |
| `Ctrl+Shift+R` | Réinitialiser toutes les retouches |
| `P` | Aperçu avant/après |
| `Ctrl+S` | Valider et fermer |
| `Échap` | Annuler et fermer |

### 3.7 Premier lancement — Onboarding

Au premier démarrage, une fenêtre d'accueil guide l'utilisateur en 3 étapes :

**Étape 1 — Choisir les dossiers à surveiller**
```
Bienvenue dans PixelPhotoManager !

Où sont vos photos ?

[✓] C:\Users\Vous\Images          [Modifier]
[ ] D:\Photos de famille          [+ Ajouter un dossier]
[ ] E:\Archives                   [+ Ajouter un dossier]

[Commencer le scan →]
```

**Étape 2 — Scan en cours (non bloquant)**
```
PixelPhotoManager découvre vos photos...

████████████░░░░  3 241 / ~5 000 photos trouvées

Dossier en cours : C:\Users\Vous\Images\2024\Vacances\

[Continuer en arrière-plan →]
```

L'utilisateur peut cliquer sur "Continuer en arrière-plan" pour commencer à naviguer immédiatement pendant que le scan se termine.

**Étape 3 — Paramétrage optionnel**
```
Voulez-vous activer la reconnaissance des personnes ?

○ Oui — PixelPhotoManager regroupera automatiquement les photos
        par personne (traitement en arrière-plan)
● Non  — Je l'activerai plus tard dans les paramètres

[Terminer la configuration ✓]
```

### 3.8 Barre de recherche — Comportement détaillé

La barre de recherche (`Ctrl+F`) est unifiée et accepte plusieurs syntaxes :

```
┌──────────────────────────────────────────────────────┐
│ 🔍  vacances 2023                           [✕] [▾] │
├──────────────────────────────────────────────────────┤
│ Résultats : 847 photos correspondantes               │
│ ─────────────────────────────────────────────────── │
│  Dossiers (3)                                        │
│   📁 2023/Vacances Bretagne  (234 photos)            │
│   📁 2023/Vacances Italie    (412 photos)            │
│   📁 2023/Noël               (201 photos)            │
│ ─────────────────────────────────────────────────── │
│  Personnes présentes                                 │
│   👤 Marie (234 photos)  👤 Lucas (189 photos)       │
└──────────────────────────────────────────────────────┘
```

**Syntaxe de recherche avancée (`[▾]`) :**

| Filtre | Syntaxe | Exemple |
|--------|---------|---------|
| Personne | `personne:prénom` | `personne:Marie` |
| Date exacte | `date:AAAA-MM-JJ` | `date:2023-07-14` |
| Plage de dates | `de:date à:date` | `de:2023-06 à:2023-08` |
| Lieu | `lieu:ville` | `lieu:Paris` |
| Appareil | `appareil:modèle` | `appareil:iPhone` |
| Tag | `tag:mot` | `tag:anniversaire` |
| Sans personne | `sans:personne` | — |
| Favoris | `favori:oui` | — |

**Résultats :** mis à jour en temps réel pendant la frappe, avec un délai de debounce de 150 ms.

---

## 4. Fonctionnalités cibles

### 4.1 Gestion de la bibliothèque

**Découverte automatique** — PixelPhotoManager scanne les dossiers configurés au démarrage et en arrière-plan. Aucun import manuel requis. Les photos et vidéos restent à leur emplacement d'origine.

**Support vidéo** — Les fichiers vidéo (`.mp4`, `.mov`, `.avi`, `.mkv`, `.wmv`, `.webm`, `.m4v`, `.3gp`, `.flv`, `.ts`, `.mts`, `.mpg`, `.mpeg`, `.vob`) sont indexés au même titre que les photos. Leurs vignettes sont extraites automatiquement via OpenCV. La lecture se fait dans le lecteur système via `QDesktopServices`.

**Gestionnaire de dossiers** — Le dialogue **Outils › Dossiers…** liste les dossiers surveillés avec leur statut, le nombre de fichiers indexés, et les sous-dossiers exclus du scan. Permet d'ajouter, retirer, ou forcer un re-scan complet de n'importe quel dossier. Le retrait d'un dossier surveillé (bouton ou menu contextuel « Supprimer des dossiers surveillés ») purge aussi le catalogue, les vignettes et les visages associés à ce dossier, après confirmation indiquant le nombre de photos concernées — les fichiers restent intacts sur le disque.

**Navigation par dossiers** — Arborescence fidèle à la structure du disque. L'utilisateur retrouve ses photos exactement comme dans l'explorateur Windows.

**Albums virtuels** — Collections organisées par thème, événement ou date, sans déplacer les fichiers. Un album est simplement une liste de chemins vers des photos existantes.

**Chronologie** — Vue par année / mois / jour avec regroupement automatique basé sur les dates EXIF.

**Recherche instantanée** — Recherche en temps réel par nom de fichier, date, lieu, personnes présentes, tags. Résultats affichés pendant la frappe.

**Détection des doublons** — Identification des photos identiques ou très similaires via hashing perceptuel, avec proposition de nettoyage.

### 4.2 Visionneuse

**Affichage haute qualité** — Rendu net avec zoom fluide de 10 % à 1600 %. Navigation au clavier (flèches) et à la molette.

**Diaporama** — Lancé via **Affichage › Diaporama** ou `F5`. Vitesse configurable (1–60 s), arrêt par Échap.
Implémenté dans `src/ui/slideshow.py` (`SlideshowWindow` + `_KenBurnsWidget`).
- **Point de départ** : visionneuse ouverte → photo affichée ; mode chronologie → photo centrale du ruban (`ThumbnailGrid.center_photo_index()`) ; sinon → plus ancienne photo.
- **Effet Ken Burns** : `_KenBurnsWidget` anime à 30 fps un rectangle source sur le pixmap (zoom 0–8 %, pan aléatoire à dominante horizontale/diagonale). Chargement en `KeepAspectRatio` : les marges noires (letterbox/pillarbox) sont préservées, l'image n'est jamais rognée.
- **Préchargement** : la photo suivante est chargée en arrière-plan (`_LoadThread`) pendant l'affichage de la courante.
- **Navigation** : ←/→ manuelle, Espace pause/reprendre, overlay auto-masquant après 5 s.

**Comparaison côte à côte** — Affichage de deux photos en parallèle pour comparer les versions.

**Informations EXIF** — Panneau togglé (`I`) affichant les métadonnées de la photo sélectionnée (appareil, objectif, ISO, vitesse, ouverture, GPS). Pour les vidéos : résolution, fps, durée.

**Géolocalisation depuis la visionneuse** — Menu contextuel › **Localiser sur la carte** ouvre OpenStreetMap à la position GPS de la photo.

### 4.3 Retouches basiques (non destructives)

Toutes les retouches sont stockées dans la base de données et appliquées à la volée. L'original n'est jamais modifié.

Les retouches disponibles sont les suivantes : ajustement de la luminosité, du contraste et de la saturation via des sliders en temps réel ; correction gamma ; recadrage libre ou selon des ratios prédéfinis (10×15, 13×18 paysage/portrait) ; rotation ±90° et redressement de l'horizon (−10° à +10°) ; miroir horizontal et vertical ; netteté et réduction du bruit ; conversion en noir et blanc avec mixage des canaux R/G/B par sliders indépendants.

### 4.4 Reconnaissance faciale

**Détection automatique** — Tous les visages de chaque photo sont détectés au scan via RetinaFace.

**Clustering automatique** — Les visages similaires sont regroupés automatiquement via DBSCAN et présentés à l'utilisateur pour validation.

**Identification** — L'utilisateur nomme un groupe, et tous les visages similaires futurs sont automatiquement associés à cette personne.

**Album par personne** — Chaque personne nommée génère automatiquement un album virtuel contenant toutes ses photos.

**Import de données existantes** — Les annotations Picasa sont importées via **Outils › Importer visages Picasa…**. Les fichiers `.picasa.ini` (présents dans chaque dossier de photos Picasa) contiennent les noms et positions de visages ; ils sont parsés et stockés dans `catalog.db` (table `picasa_annotations`). Les noms Picasa sont réutilisés comme étiquettes pour les clusters DBSCAN.

**Modèle IA** — ArcFace via DeepFace pour une précision de 99 %+ en reconnaissance faciale.

### 4.5 Géolocalisation

**Carte interactive** — Visualisation de toutes les photos géolocalisées sur une carte (OpenStreetMap, hors ligne possible).

**Localisation depuis la visionneuse** — Clic droit sur une photo › **Localiser sur la carte** ouvre directement OpenStreetMap dans le navigateur, centré sur la position GPS de la photo (option grisée si pas de données GPS).

**Regroupement géographique** — Les photos prises au même endroit sont regroupées automatiquement.

**Attribution manuelle** — Possibilité d'assigner un lieu à des photos sans GPS via une interface de glisser-déposer sur la carte.

### 4.6 Export et partage

Export vers JPEG, PNG, TIFF avec contrôle de la qualité et de la résolution. Redimensionnement par lot. Création de diaporamas PDF. Génération de planches contact. Watermarking automatique.

---

## 5. Architecture technique

### 5.1 Vue d'ensemble

```
PixelPhotoManager/
│
├── src/
│   ├── core/                    ← Noyau de l'application
│   │   ├── app.py               ← Point d'entrée, initialisation
│   │   ├── config.py            ← Configuration centralisée
│   │   ├── event_bus.py         ← Bus d'événements (pub/sub)
│   │   └── plugin_manager.py    ← Gestionnaire de plugins
│   │
│   ├── library/                 ← Gestion de la bibliothèque
│   │   ├── scanner.py           ← Scan des dossiers (thread, force rescan)
│   │   ├── catalog.py           ← Base de données catalogue
│   │   ├── thumbnail_cache.py   ← Cache des vignettes (images + vidéos)
│   │   └── exif_reader.py       ← ExifReader + VideoMetadataReader + VIDEO_EXT
│   │
│   ├── ui/                      ← Interface utilisateur
│   │   ├── main_window.py       ← Fenêtre principale
│   │   ├── thumbnail_grid.py    ← Grille de vignettes (badge ▶ vidéos)
│   │   ├── photo_viewer.py      ← Visionneuse + vidéos + carte
│   │   ├── sidebar.py           ← Panneau de navigation
│   │   ├── edit_panel.py        ← Panneau de retouche (N&B R/G/B)
│   │   ├── exif_panel.py        ← Panneau EXIF (toggle I)
│   │   └── folder_manager_dialog.py ← Gestion dossiers (Outils › Dossiers…)
│   │
│   ├── processing/              ← Traitements image
│   │   ├── adjustments.py       ← Luminosité, contraste, N&B mixage...
│   │   ├── geometry.py          ← Recadrage, rotation
│   │   └── edit_database.py     ← Persistence retouches (SQLite)
│   │
│   ├── faces/                   ← Reconnaissance faciale
│   │   ├── detector.py          ← Détection des visages
│   │   ├── recognizer.py        ← Identification (ArcFace/DeepFace)
│   │   ├── clusterer.py         ← Regroupement automatique (DBSCAN)
│   │   ├── face_panel.py        ← Panneau visages dans la visionneuse
│   │   └── picasa_importer.py   ← Import annotations Picasa (.picasa.ini)
│   │
│   └── plugins/                 ← Plugins intégrés
│       ├── restoration/         ← Restauration IA
│       ├── colorization/        ← Colorisation
│       └── map_view/            ← Vue carte
│
├── plugins/                     ← Plugins utilisateur (externe)
│   └── mon_plugin/
│       ├── plugin.py
│       └── plugin.json
│
├── tests/
├── docs/
│   └── plugin-api.md            ← Documentation API plugins
│
├── CLAUDE.md
└── requirements.txt
```

### 5.2 Le noyau — Bus d'événements

Le bus d'événements est la pièce centrale de l'architecture. Il découple complètement les composants entre eux et permet aux plugins de s'intégrer sans modifier le code existant.

```python
# src/core/event_bus.py

from typing import Callable, Any
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class EventBus:
    """
    Bus d'événements central de PixelPhotoManager.
    
    Permet la communication découplée entre tous les composants.
    Les plugins utilisent ce bus pour réagir aux événements
    de l'application et pour émettre leurs propres événements.
    
    Événements système disponibles :
    
    Bibliothèque :
        library.scan_started(dossier: str)
        library.photo_discovered(photo: PhotoInfo)
        library.scan_finished(total: int)
        library.photo_selected(photo: PhotoInfo)
        library.photos_deleted(photos: list[PhotoInfo])
    
    Traitements :
        processing.started(photo: PhotoInfo, processor: str)
        processing.progress(percent: int)
        processing.finished(photo: PhotoInfo, result: PIL.Image)
        processing.error(photo: PhotoInfo, error: str)
    
    Faces :
        faces.detected(photo: PhotoInfo, faces: list[FaceInfo])
        faces.person_named(person_id: int, name: str)
        faces.cluster_ready(cluster_id: int, faces: list[FaceInfo])
    
    UI :
        ui.theme_changed(theme: str)
        ui.view_changed(view: str)
    """

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._once_handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, handler: Callable) -> None:
        """
        Abonne un handler à un événement.
        
        Args:
            event: Nom de l'événement (ex: 'library.photo_selected')
            handler: Fonction appelée quand l'événement est émis
            
        Exemple:
            bus.on('library.photo_selected', self.on_photo_selected)
        """
        self._handlers[event].append(handler)

    def once(self, event: str, handler: Callable) -> None:
        """Abonne un handler qui ne sera appelé qu'une seule fois."""
        self._once_handlers[event].append(handler)

    def off(self, event: str, handler: Callable) -> None:
        """Désabonne un handler d'un événement."""
        if handler in self._handlers[event]:
            self._handlers[event].remove(handler)

    def emit(self, event: str, **kwargs) -> None:
        """
        Émet un événement avec des données associées.
        
        Args:
            event: Nom de l'événement
            **kwargs: Données associées à l'événement
            
        Exemple:
            bus.emit('library.photo_selected', photo=photo_info)
        """
        for handler in self._handlers.get(event, []):
            try:
                handler(**kwargs)
            except Exception as e:
                logger.error(f"Erreur handler {event}: {e}")

        # Handlers once
        for handler in self._once_handlers.pop(event, []):
            try:
                handler(**kwargs)
            except Exception as e:
                logger.error(f"Erreur handler once {event}: {e}")


# Instance globale
bus = EventBus()
```

### 5.3 La performance — Stratégie de cache en couches

La rapidité de l'application repose sur un cache intelligent des vignettes. PixelPhotoManager utilise une stratégie à trois niveaux.

```python
# src/library/thumbnail_cache.py

from pathlib import Path
from PIL import Image
import sqlite3
import hashlib
import io
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import QByteArray


class ThumbnailCache:
    """
    Cache de vignettes à trois niveaux pour des performances maximales.
    
    Niveau 1 — Mémoire RAM (LRU cache) :
        Les 500 dernières vignettes consultées restent en mémoire.
        Accès en microsecondes. Taille : ~50 Mo.
    
    Niveau 2 — Base SQLite (disque SSD) :
        Toutes les vignettes indexées. Accès en millisecondes.
        Invalidé automatiquement si le fichier source change.
    
    Niveau 3 — Génération à la demande :
        Si la vignette n'existe pas, elle est générée en arrière-plan
        et les deux niveaux précédents sont alimentés.
    """

    TAILLE_VIGNETTE = (220, 220)
    CACHE_RAM_MAX = 500

    def __init__(self, db_path: str):
        self._ram_cache: dict[str, QPixmap] = {}
        self._ram_order: list[str] = []
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thumbnails (
                photo_hash TEXT PRIMARY KEY,
                photo_path TEXT,
                file_mtime REAL,
                thumbnail_data BLOB,
                generated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def get(self, photo_path: str) -> QPixmap | None:
        """
        Retourne la vignette d'une photo.
        Cherche d'abord en RAM, puis SQLite, puis génère.
        """
        key = self._make_key(photo_path)

        # Niveau 1 — RAM
        if key in self._ram_cache:
            return self._ram_cache[key]

        # Niveau 2 — SQLite
        pixmap = self._get_from_db(photo_path, key)
        if pixmap:
            self._store_ram(key, pixmap)
            return pixmap

        # Niveau 3 — Génération (retourne None, génération async)
        return None

    def generate(self, photo_path: str) -> QPixmap | None:
        """Génère et stocke une vignette. Appelé dans un thread."""
        try:
            img = Image.open(photo_path)
            img.thumbnail(self.TAILLE_VIGNETTE, Image.LANCZOS)

            # Corriger l'orientation EXIF
            img = self._corriger_orientation(img)

            # Convertir en QPixmap via bytes
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            data = buf.getvalue()

            # Stocker en DB
            key = self._make_key(photo_path)
            mtime = Path(photo_path).stat().st_mtime
            conn = sqlite3.connect(self._db_path)
            conn.execute("""
                INSERT OR REPLACE INTO thumbnails
                (photo_hash, photo_path, file_mtime, thumbnail_data)
                VALUES (?, ?, ?, ?)
            """, (key, photo_path, mtime, data))
            conn.commit()
            conn.close()

            # Convertir en QPixmap
            pixmap = QPixmap()
            pixmap.loadFromData(QByteArray(data))
            self._store_ram(key, pixmap)
            return pixmap

        except Exception as e:
            return None

    def _get_from_db(self, photo_path: str, key: str) -> QPixmap | None:
        """Récupère une vignette depuis SQLite avec vérification fraîcheur."""
        try:
            mtime_actuel = Path(photo_path).stat().st_mtime
            conn = sqlite3.connect(self._db_path)
            row = conn.execute("""
                SELECT thumbnail_data, file_mtime FROM thumbnails
                WHERE photo_hash = ?
            """, (key,)).fetchone()
            conn.close()

            if row and abs(row[1] - mtime_actuel) < 1.0:
                pixmap = QPixmap()
                pixmap.loadFromData(QByteArray(row[0]))
                return pixmap
        except Exception:
            pass
        return None

    def _store_ram(self, key: str, pixmap: QPixmap) -> None:
        """Stocke en RAM avec éviction LRU."""
        if len(self._ram_order) >= self.CACHE_RAM_MAX:
            oldest = self._ram_order.pop(0)
            self._ram_cache.pop(oldest, None)
        self._ram_cache[key] = pixmap
        self._ram_order.append(key)

    def _make_key(self, photo_path: str) -> str:
        return hashlib.md5(photo_path.encode()).hexdigest()

    def _corriger_orientation(self, img: Image.Image) -> Image.Image:
        """Corrige l'orientation selon les données EXIF."""
        try:
            from PIL import ImageOps
            return ImageOps.exif_transpose(img)
        except Exception:
            return img
```

---

## 6. Interface Plugin — Spécification complète

C'est la pièce la plus importante de l'architecture. L'interface plugin est conçue pour être **si simple qu'un débutant Python peut écrire un plugin en 30 minutes**.

### 6.1 Anatomie d'un plugin

Un plugin PixelPhotoManager est un dossier Python contenant exactement deux fichiers obligatoires et des fichiers optionnels.

```
plugins/mon-plugin/
├── plugin.json       ← Obligatoire : métadonnées du plugin
├── plugin.py         ← Obligatoire : code du plugin
├── README.md         ← Recommandé : documentation
├── requirements.txt  ← Optionnel : dépendances supplémentaires
└── assets/           ← Optionnel : icônes, ressources
    └── icon.png
```

### 6.2 Fichier plugin.json

```json
{
    "id": "mon-plugin-unique-id",
    "name": "Mon Plugin",
    "version": "1.0.0",
    "description": "Description courte de ce que fait le plugin",
    "author": "Votre Nom",
    "email": "votre@email.com",
    "url": "https://github.com/vous/mon-plugin",
    "min_pixelphotomanager_version": "1.0.0",
    "type": "processor",
    "tags": ["retouche", "filtre"],
    "icon": "assets/icon.png",
    "settings_schema": {
        "intensite": {
            "type": "float",
            "label": "Intensité",
            "default": 0.5,
            "min": 0.0,
            "max": 1.0,
            "step": 0.01
        },
        "mode": {
            "type": "choice",
            "label": "Mode",
            "default": "auto",
            "choices": ["auto", "manuel", "avancé"]
        }
    }
}
```

### 6.3 Interface de base — BasePlugin

```python
# src/core/base_plugin.py

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.event_bus import EventBus
    from src.core.config import Config


class BasePlugin(ABC):
    """
    Classe de base pour tous les plugins PixelPhotoManager.
    
    Pour créer un plugin, héritez de cette classe et
    implémentez les méthodes marquées @abstractmethod.
    
    Les méthodes optionnelles peuvent être surchargées
    selon les besoins de votre plugin.
    
    Exemple minimal (plugin qui affiche un message) :
    
        class MonPlugin(BasePlugin):
            def activate(self):
                self.bus.on('library.photo_selected', self.on_photo)
            
            def deactivate(self):
                self.bus.off('library.photo_selected', self.on_photo)
            
            def on_photo(self, photo):
                print(f"Photo sélectionnée : {photo.path}")
    """

    def __init__(
        self,
        bus: 'EventBus',
        config: 'Config',
        settings: dict
    ):
        """
        Constructeur — ne pas surcharger.
        Utilisez activate() pour votre initialisation.
        """
        self.bus = bus
        self.config = config
        self.settings = settings
        self.enabled = False

    @abstractmethod
    def activate(self) -> None:
        """
        Appelé quand le plugin est activé.
        
        C'est ici que vous vous abonnez aux événements
        et initialisez vos ressources.
        
        Exemple :
            def activate(self):
                self.bus.on('library.photo_selected',
                            self.on_photo_selected)
                self.model = charger_mon_modele()
        """
        ...

    @abstractmethod
    def deactivate(self) -> None:
        """
        Appelé quand le plugin est désactivé.
        
        Libérez vos ressources et désabonnez-vous des événements.
        
        Exemple :
            def deactivate(self):
                self.bus.off('library.photo_selected',
                             self.on_photo_selected)
                del self.model
        """
        ...

    def get_menu_items(self) -> list[dict]:
        """
        Optionnel — Ajoute des entrées dans le menu principal.
        
        Retourne une liste de dictionnaires décrivant
        les entrées de menu à ajouter.
        
        Exemple :
            def get_menu_items(self):
                return [
                    {
                        'menu': 'Outils',
                        'label': 'Mon action',
                        'shortcut': 'Ctrl+M',
                        'callback': self.mon_action,
                        'icon': 'assets/icon.png'
                    }
                ]
        """
        return []

    def get_toolbar_items(self) -> list[dict]:
        """
        Optionnel — Ajoute des boutons dans la toolbar.
        
        Même format que get_menu_items().
        """
        return []

    def get_sidebar_widget(self):
        """
        Optionnel — Ajoute un panneau dans la sidebar.
        
        Retourne un QWidget ou None.
        
        Exemple :
            def get_sidebar_widget(self):
                widget = QWidget()
                layout = QVBoxLayout(widget)
                layout.addWidget(QLabel("Mon panneau"))
                return widget
        """
        return None

    def get_context_menu_items(
        self, photos: list
    ) -> list[dict]:
        """
        Optionnel — Ajoute des entrées dans le menu contextuel.
        
        photos est la liste des photos sélectionnées.
        
        Exemple :
            def get_context_menu_items(self, photos):
                if len(photos) == 1:
                    return [{'label': 'Traiter avec MonPlugin',
                             'callback': lambda: self.traiter(photos[0])}]
                return []
        """
        return []

    def on_settings_changed(self, settings: dict) -> None:
        """
        Optionnel — Appelé quand l'utilisateur modifie
        les paramètres du plugin dans les préférences.
        """
        self.settings = settings
```

### 6.4 Interface spécialisée — ProcessorPlugin

Pour les plugins qui appliquent un traitement sur les photos.

```python
# src/core/processor_plugin.py

from .base_plugin import BasePlugin
from PIL import Image
from abc import abstractmethod


class ProcessorPlugin(BasePlugin):
    """
    Plugin de traitement d'image.
    
    À utiliser pour les plugins qui transforment des photos :
    filtres, effets, restauration IA, colorisation, etc.
    
    PixelPhotoManager gère automatiquement :
    - L'affichage dans le panneau de retouche
    - La prévisualisation en temps réel
    - L'historique (undo/redo)
    - La sauvegarde non destructive
    
    Exemple complet — Plugin filtre sépia :
    
        class FiltreSepia(ProcessorPlugin):
            name = "Filtre Sépia"
            description = "Applique un effet sépia vintage"
            category = "Effets"
            icon = "assets/sepia.png"
            supports_preview = True
            
            def process(self, image, params):
                intensite = params.get('intensite', 0.8)
                img_gris = image.convert('L').convert('RGB')
                r, g, b = img_gris.split()
                r = r.point(lambda i: min(255, i * 1.1))
                g = g.point(lambda i: i * 0.9)
                b = b.point(lambda i: i * 0.7)
                sepia = Image.merge('RGB', (r, g, b))
                return Image.blend(image, sepia, intensite)
            
            def get_default_params(self):
                return {'intensite': 0.8}
            
            def activate(self): pass
            def deactivate(self): pass
    """

    # À définir dans chaque plugin
    name: str = ""
    description: str = ""
    category: str = "Général"
    icon: str = ""
    supports_preview: bool = True
    supports_batch: bool = True

    @abstractmethod
    def process(
        self,
        image: Image.Image,
        params: dict
    ) -> Image.Image:
        """
        Applique le traitement et retourne l'image résultante.
        
        Args:
            image: Image PIL en entrée (ne pas modifier l'original)
            params: Paramètres du traitement (depuis plugin.json)
        
        Returns:
            Image PIL résultante
        
        Notes:
            - Cette méthode est appelée dans un thread secondaire
            - Ne pas accéder à l'UI depuis cette méthode
            - Émettre des événements processing.progress si long
        """
        ...

    @abstractmethod
    def get_default_params(self) -> dict:
        """
        Retourne les paramètres par défaut.
        
        Doit correspondre au settings_schema de plugin.json.
        
        Exemple :
            def get_default_params(self):
                return {
                    'intensite': 0.5,
                    'mode': 'auto'
                }
        """
        ...

    def validate_params(self, params: dict) -> dict:
        """
        Optionnel — Valide et corrige les paramètres.
        Appelé avant process(). Par défaut, complète avec
        les valeurs par défaut manquantes.
        """
        defaults = self.get_default_params()
        return {**defaults, **params}

    def estimate_duration(
        self,
        image: Image.Image,
        params: dict
    ) -> float:
        """
        Optionnel — Estime la durée du traitement en secondes.
        Utilisé pour afficher une barre de progression adaptée.
        Par défaut : 1 seconde.
        """
        return 1.0
```

### 6.5 Interface spécialisée — ViewPlugin

Pour les plugins qui ajoutent de nouvelles vues à l'interface.

```python
# src/core/view_plugin.py

from .base_plugin import BasePlugin
from PyQt6.QtWidgets import QWidget
from abc import abstractmethod


class ViewPlugin(BasePlugin):
    """
    Plugin de vue — ajoute un nouvel onglet dans la sidebar.
    
    À utiliser pour : carte géographique, timeline avancée,
    statistiques, comparaison de photos, etc.
    
    Exemple — Plugin vue calendrier :
    
        class VueCalendrier(ViewPlugin):
            name = "Calendrier"
            icon = "assets/calendar.png"
            
            def create_widget(self, parent):
                widget = CalendrierWidget(parent)
                self.bus.on('library.photo_discovered',
                            widget.ajouter_photo)
                return widget
            
            def activate(self): pass
            def deactivate(self): pass
    """

    name: str = ""
    icon: str = ""
    position: str = "sidebar"  # "sidebar", "main", "bottom"

    @abstractmethod
    def create_widget(self, parent: QWidget) -> QWidget:
        """
        Crée et retourne le widget de la vue.
        
        Args:
            parent: Widget parent PyQt6
        
        Returns:
            QWidget représentant la vue
        """
        ...
```

### 6.6 Gestionnaire de plugins

```python
# src/core/plugin_manager.py

import importlib.util
import json
from pathlib import Path
from typing import Type
import logging

from .base_plugin import BasePlugin
from .event_bus import bus

logger = logging.getLogger(__name__)


class PluginManager:
    """
    Gestionnaire central des plugins.
    
    Découvre, charge, active et désactive les plugins
    automatiquement depuis le dossier plugins/.
    
    Utilisation :
        pm = PluginManager(config)
        pm.discover()          # Scanner les plugins disponibles
        pm.activate('mon-id')  # Activer un plugin
        pm.deactivate('mon-id')
        pm.list_available()    # Lister tous les plugins
    """

    PLUGIN_DIRS = [
        Path('plugins'),                          # Plugins utilisateur
        Path('src/plugins'),                      # Plugins intégrés
        Path.home() / '.pixelphotomanager' / 'plugins' # Plugins globaux
    ]

    def __init__(self, config):
        self.config = config
        self._available: dict[str, dict] = {}
        self._loaded: dict[str, BasePlugin] = {}

    def discover(self) -> list[dict]:
        """
        Scanne les dossiers de plugins et retourne la liste
        de tous les plugins disponibles avec leurs métadonnées.
        """
        self._available.clear()

        for plugin_dir in self.PLUGIN_DIRS:
            if not plugin_dir.exists():
                continue

            for item in plugin_dir.iterdir():
                if not item.is_dir():
                    continue

                manifest_path = item / 'plugin.json'
                if not manifest_path.exists():
                    continue

                try:
                    manifest = json.loads(
                        manifest_path.read_text(encoding='utf-8')
                    )
                    manifest['_path'] = str(item)
                    self._available[manifest['id']] = manifest
                    logger.info(f"Plugin découvert : {manifest['name']}")
                except Exception as e:
                    logger.error(f"Erreur lecture plugin {item}: {e}")

        return list(self._available.values())

    def activate(self, plugin_id: str) -> bool:
        """
        Active un plugin par son identifiant.
        
        Retourne True si l'activation a réussi, False sinon.
        """
        if plugin_id in self._loaded:
            return True  # Déjà actif

        if plugin_id not in self._available:
            logger.error(f"Plugin inconnu : {plugin_id}")
            return False

        manifest = self._available[plugin_id]
        plugin_path = Path(manifest['_path']) / 'plugin.py'

        try:
            # Charger dynamiquement le module Python
            spec = importlib.util.spec_from_file_location(
                f"plugin_{plugin_id}", plugin_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Trouver la classe du plugin
            plugin_class = self._find_plugin_class(module)
            if not plugin_class:
                raise ValueError("Aucune classe BasePlugin trouvée")

            # Instancier et activer
            settings = self.config.get_plugin_settings(plugin_id)
            instance = plugin_class(bus, self.config, settings)
            instance.activate()
            instance.enabled = True

            self._loaded[plugin_id] = instance
            bus.emit('plugin.activated', plugin_id=plugin_id)
            logger.info(f"Plugin activé : {manifest['name']}")
            return True

        except Exception as e:
            logger.error(f"Erreur activation plugin {plugin_id}: {e}")
            return False

    def deactivate(self, plugin_id: str) -> bool:
        """Désactive un plugin par son identifiant."""
        if plugin_id not in self._loaded:
            return True

        try:
            instance = self._loaded[plugin_id]
            instance.deactivate()
            instance.enabled = False
            del self._loaded[plugin_id]
            bus.emit('plugin.deactivated', plugin_id=plugin_id)
            return True
        except Exception as e:
            logger.error(f"Erreur désactivation {plugin_id}: {e}")
            return False

    def get_active_plugins(self) -> list[BasePlugin]:
        """Retourne la liste des plugins actuellement actifs."""
        return list(self._loaded.values())

    def list_available(self) -> list[dict]:
        """Retourne la liste de tous les plugins disponibles."""
        return [
            {**manifest, 'active': manifest['id'] in self._loaded}
            for manifest in self._available.values()
        ]

    def _find_plugin_class(
        self, module
    ) -> Type[BasePlugin] | None:
        """Trouve la classe plugin dans un module chargé."""
        for name in dir(module):
            obj = getattr(module, name)
            if (isinstance(obj, type)
                    and issubclass(obj, BasePlugin)
                    and obj is not BasePlugin):
                return obj
        return None
```

---

## 7. Exemple de plugin complet

Voici un exemple de plugin entier, documenté et fonctionnel, pour illustrer la simplicité de l'API.

```
plugins/watermark/
├── plugin.json
└── plugin.py
```

```json
{
    "id": "watermark-plugin",
    "name": "Filigrane",
    "version": "1.0.0",
    "description": "Ajoute un filigrane texte ou image sur les photos",
    "author": "Votre Nom",
    "type": "processor",
    "tags": ["export", "protection"],
    "settings_schema": {
        "texte": {
            "type": "string",
            "label": "Texte du filigrane",
            "default": "© Mon Nom 2026"
        },
        "opacite": {
            "type": "float",
            "label": "Opacité",
            "default": 0.5,
            "min": 0.1,
            "max": 1.0
        },
        "position": {
            "type": "choice",
            "label": "Position",
            "default": "bas-droite",
            "choices": [
                "bas-droite", "bas-gauche",
                "haut-droite", "haut-gauche",
                "centre"
            ]
        }
    }
}
```

```python
# plugins/watermark/plugin.py

from PIL import Image, ImageDraw, ImageFont
from src.core.processor_plugin import ProcessorPlugin


class WatermarkPlugin(ProcessorPlugin):
    """
    Plugin filigrane — ajoute un texte semi-transparent
    sur les photos lors de l'export.
    
    Ce plugin illustre l'utilisation de ProcessorPlugin.
    Il est appelé automatiquement lors de l'export si activé.
    """

    name = "Filigrane"
    description = "Ajoute un filigrane texte sur les photos"
    category = "Export"
    supports_preview = True
    supports_batch = True

    def activate(self) -> None:
        """Rien à initialiser pour ce plugin simple."""
        pass

    def deactivate(self) -> None:
        """Rien à libérer."""
        pass

    def process(
        self,
        image: Image.Image,
        params: dict
    ) -> Image.Image:
        """
        Ajoute le filigrane sur l'image.
        
        Args:
            image: Photo originale
            params: {'texte': str, 'opacite': float, 'position': str}
        
        Returns:
            Photo avec filigrane
        """
        texte = params.get('texte', '© PixelPhotoManager')
        opacite = params.get('opacite', 0.5)
        position = params.get('position', 'bas-droite')

        # Créer une couche transparente pour le filigrane
        watermark = Image.new('RGBA', image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(watermark)

        # Calculer la taille de police proportionnelle
        taille_police = max(20, image.width // 40)
        try:
            font = ImageFont.truetype("arial.ttf", taille_police)
        except Exception:
            font = ImageFont.load_default()

        # Calculer la position
        bbox = draw.textbbox((0, 0), texte, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        marge = 20

        positions = {
            'bas-droite':  (image.width - tw - marge,
                            image.height - th - marge),
            'bas-gauche':  (marge, image.height - th - marge),
            'haut-droite': (image.width - tw - marge, marge),
            'haut-gauche': (marge, marge),
            'centre':      ((image.width - tw) // 2,
                            (image.height - th) // 2),
        }
        x, y = positions.get(position, positions['bas-droite'])

        # Dessiner avec opacité
        alpha = int(255 * opacite)
        draw.text((x, y), texte, font=font,
                  fill=(255, 255, 255, alpha))

        # Fusionner avec l'image originale
        result = image.convert('RGBA')
        result = Image.alpha_composite(result, watermark)
        return result.convert('RGB')

    def get_default_params(self) -> dict:
        return {
            'texte': '© Mon Nom 2026',
            'opacite': 0.5,
            'position': 'bas-droite'
        }
```

---

## 8. Performance — Principes et stratégies

### 8.1 Règle d'or : l'UI ne bloque jamais

Toute opération de plus de 50 ms s'exécute dans un thread secondaire. L'interface reste toujours réactive.

```python
# Patron de base pour toute opération longue

from PyQt6.QtCore import QThread, pyqtSignal

class ScanThread(QThread):
    progress = pyqtSignal(int, str)   # percent, message
    finished = pyqtSignal(int)        # total photos

    def __init__(self, library, dossier):
        super().__init__()
        self.library = library
        self.dossier = dossier

    def run(self):
        def on_progress(path, percent):
            self.progress.emit(percent, path)

        total = self.library.scanner_dossier(
            self.dossier, callback=on_progress
        )
        self.finished.emit(total)
```

### 8.2 Chargement progressif de la grille

Les vignettes s'affichent au fur et à mesure qu'elles sont générées, par lots pour ne pas saturer l'UI.

```python
# Afficher les photos par lots de 50
# pendant que les vignettes sont générées en arrière-plan

BATCH_SIZE = 50

def charger_photos_progressivement(self, photos: list):
    for i in range(0, len(photos), BATCH_SIZE):
        lot = photos[i:i + BATCH_SIZE]
        self.grid_view.ajouter_photos(lot)
        # Laisser l'UI se rafraîchir entre chaque lot
        QApplication.processEvents()
```

### 8.3 Préchargement intelligent

Pendant que l'utilisateur consulte une photo, les photos adjacentes sont préchargées en arrière-plan.

```python
# Précharger les 3 photos suivantes et les 3 précédentes
PREFETCH_RANGE = 3

def on_photo_changed(self, index: int):
    indices_a_precharger = [
        i for i in range(
            index - PREFETCH_RANGE,
            index + PREFETCH_RANGE + 1
        )
        if 0 <= i < len(self.photos)
        and i != index
    ]
    for i in indices_a_precharger:
        self._prefetch_thread.enqueue(self.photos[i])
```

### 8.4 Objectifs de performance par opération

| Opération | Cible | Stratégie |
|-----------|-------|-----------|
| Démarrage application | < 2 s | Chargement lazy, splash screen |
| Ouverture d'un dossier (cache chaud) | < 500 ms | Cache vignettes SQLite + RAM |
| Génération d'une vignette | < 100 ms | Thread pool, Pillow LANCZOS |
| Navigation photo suivante | < 50 ms | Préchargement ±3 photos |
| Résultats de recherche | < 200 ms | Index SQLite FTS5 |
| Scan 10 000 photos | < 30 s | Scan multi-thread, indexation incrémentale |
| Ajustement slider retouche | < 100 ms | Preview basse résolution en temps réel |
| Export lot 100 photos | barre de progression | Thread dédié, pas de blocage UI |

---

## 9. Stratégie de tests

### 9.1 Trois couches, alignées sur le coût de chaque type de bug

La stratégie de tests reflète la même philosophie que le reste du projet (§2) : privilégier ce qui donne le plus de confiance pour le moins de friction. Trois couches, du moins au plus coûteux à écrire et exécuter :

| Couche | Cible | Ce qu'elle attrape |
|---|---|---|
| **Layer 1 — Unitaire** | Logique pure (détection de doublons, géométrie des retouches, base de données) | Régressions de calcul, migrations SQL cassées |
| **Layer 2 — Widgets Qt** | Un widget isolé (`pytest-qt`), sans lancer l'application | Bugs de rendu/état d'un composant, sans le coût d'un scénario complet |
| **Layer 3 — Bout-en-bout** | La vraie application pilotée via automation OS (`pywinauto`), scénario utilisateur complet | Régressions d'intégration entre couches (UI ↔ thread ↔ DB) invisibles aux deux couches précédentes |

Layer 3 existe précisément parce que plusieurs bugs critiques de ce projet (ex. la détection de doublons qui rapportait silencieusement « aucun doublon » à cause d'un `Signal(dict)` avec clés `int`, cf. historique du projet) ne se manifestaient qu'à l'intégration réelle — aucun test unitaire ne les aurait révélés, car chaque composant pris isolément se comportait correctement.

### 9.2 Isolation totale des données réelles de l'utilisateur

Principe non négociable : **aucun test ne touche jamais au profil réel de l'utilisateur** (`%LOCALAPPDATA%\PixelPhotoManager` — catalogue, vignettes, retouches, visages, configuration). Les tests Layer 1/2 redirigent cette variable d'environnement vers un dossier temporaire avant tout import de code applicatif ; les tests Layer 3 lancent une vraie instance de l'application en sous-processus, avec son propre profil isolé et une bibliothèque de photos synthétique jetable, jamais les photos réelles. Cette isolation permet en particulier de lancer la suite de tests pendant qu'une instance réelle de l'application tourne sur des données de production, sans aucun risque de collision — voir le Guide Développeur §15 pour le détail des mécanismes.

### 9.3 État de la couverture (2026-07)

La couverture de tests automatisés reste volontairement ciblée sur les zones à plus fort historique de régressions silencieuses (doublons, retouches non destructives) plutôt qu'exhaustive : ~9 % des lignes de `src/` sont couvertes par les Layers 1+2. La reconnaissance faciale (`src/faces/`) et la quasi-totalité de l'interface (`src/ui/`) ne sont exercées qu'indirectement par les 4 scénarios Layer 3, ce qui est une dette assumée plutôt qu'un oubli — ces zones nécessitent soit des données visages réalistes (coûteuses à synthétiser), soit une automation UI plus lourde que ce que Layer 1/2 permettent.

---

## 10. Plugins intégrés prévus

| Plugin | Type | Description | Dépendance |
|--------|------|-------------|------------|
| Restauration IA | Processor | HYPIR + SUPIR | PyTorch CUDA |
| Colorisation | Processor | DDColor | PyTorch |
| Super-résolution | Processor | Real-ESRGAN ×2/×4 | PyTorch |
| Réparation | Processor | IOPaint / LaMa | PyTorch |
| Amélioration visages | Processor | GFPGAN / CodeFormer | PyTorch |
| Carte géographique | View | OpenStreetMap offline | folium |
| Export PDF | Processor | Planche contact, diaporama | reportlab |
| Détection doublons | Tool | Hash perceptuel | imagehash |
| Import données legacy | Tool | Formats .ini + contacts.xml | — |
| Slideshow | View | Diaporama plein écran | — | ✓ Implémenté |

---

## 11. Roadmap

### Version 1.0 — Fondations (MVP) ✓ Livré
Scan et indexation des photos, grille de vignettes rapide, visionneuse avec zoom, retouches basiques non destructives (luminosité, contraste, saturation, gamma, netteté, débruitage, rotation, redressement, recadrage, miroir, N&B avec mixage R/G/B), undo/redo persistant, albums, favoris, recherche.

### Version 1.1 — Organisation ✓ Livré
Albums virtuels, diaporama, géolocalisation (carte depuis la visionneuse), reconnaissance faciale avec clustering DBSCAN et import Picasa, panneau EXIF toggle, support vidéo complet (13 extensions), gestionnaire de dossiers (Outils › Dossiers…).

### Version 1.2 — Intelligence (en cours)
Suggestions intelligentes d'albums, recherche par contenu visuel, détection automatique de scènes, chronologie avancée, détection des doublons.

### Version 2.0 — Extensions IA
Intégration des modèles de restauration (HYPIR, SUPIR, DDColor, Real-ESRGAN), marketplace de plugins, synchronisation optionnelle avec le cloud.

---

## 12. Stack technique résumée

| Composant | Technologie | Justification |
|-----------|-------------|---------------|
| Interface | PySide6 | Maturité, performance, richesse widgets |
| Base de données | SQLite | Embarqué, rapide, zéro config |
| Cache vignettes | SQLite + RAM LRU | Double niveau, invalidation automatique |
| Traitement image | Pillow + OpenCV | Complémentaires, bien maîtrisés ; OpenCV pour les vignettes vidéo |
| Reconnaissance faciale | DeepFace + ArcFace | 99%+ précision, open source |
| Détection visages | RetinaFace | Meilleur détecteur open source |
| Clustering | scikit-learn DBSCAN | Adapté aux données de visages |
| Threading | QThread | Intégré PySide6, signaux thread-safe |
| Bus d'événements | Custom pub/sub | Léger, découplé, extensible |
| Plugins | importlib dynamique | Standard Python, simple |
| GPS/Carte | GPSPhoto + folium | Lecture EXIF GPS + carte offline |
| Packaging | PyInstaller | Exécutable Windows autonome |
| Tests unitaire/widgets | pytest + pytest-qt | Layers 1+2, multiplateforme, rapide |
| Tests bout-en-bout | pywinauto (backend UIA) | Layer 3, pilotage réel de l'appli Windows |
