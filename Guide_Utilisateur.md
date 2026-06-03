# Guide utilisateur — PixelPhotoManager

> **PixelPhotoManager** est un gestionnaire de photos non destructif pour Windows. Vos fichiers originaux ne sont jamais modifiés : toutes les retouches sont stockées séparément et appliquées à la volée à l'affichage.

---

## Table des matières

1. [Premiers pas](#1-premiers-pas)
2. [Interface générale](#2-interface-générale)
3. [Gérer vos dossiers](#3-gérer-vos-dossiers)
4. [La grille de photos](#4-la-grille-de-photos)
5. [Visualiser une photo](#5-visualiser-une-photo)
6. [Retoucher une photo](#6-retoucher-une-photo)
7. [Albums et favoris](#7-albums-et-favoris)
8. [Recherche](#8-recherche)
9. [Déplacer des photos](#9-déplacer-des-photos)
10. [Raccourcis clavier](#10-raccourcis-clavier)
11. [Où sont stockées vos données](#11-où-sont-stockées-vos-données)

---

## 1. Premiers pas

### Premier lancement

Au premier démarrage, une fenêtre d'accueil vous invite à choisir au moins un **dossier de photos** à surveiller.

1. Cliquez sur **+ Ajouter un dossier** et sélectionnez un dossier contenant vos photos.
2. Répétez l'opération si vous avez plusieurs dossiers.
3. Cliquez sur **Commencer →**.

L'application indexe immédiatement vos photos en arrière-plan. La barre de statut en bas indique la progression du scan (`Scan… 42%  —  nom_du_fichier.jpg`).

### Ajouter un dossier ultérieurement

Menu **Fichier › Ajouter un dossier…** — sélectionnez n'importe quel dossier, il sera scanné automatiquement et ajouté à la sidebar.

---

## 2. Interface générale

```
┌─────────────────────────────────────────────────────────┐
│  [Barre de recherche]                      [Toolbar]    │
├──────────────────┬──────────────────────────────────────┤
│                  │                                       │
│   SIDEBAR        │   ZONE PRINCIPALE                     │
│                  │   (grille ou visionneuse)             │
│   Dossiers       │                                       │
│   Albums         │                                       │
│                  │                                       │
├──────────────────┴──────────────────────────────────────┤
│  Barre de statut          [Taille ────────] [▦]         │
└─────────────────────────────────────────────────────────┘
```

| Zone | Rôle |
|---|---|
| **Barre de recherche** | Recherche instantanée par nom de fichier ou appareil photo |
| **Sidebar** | Navigation dans les dossiers et albums |
| **Zone principale** | Grille de vignettes ou visionneuse plein écran |
| **Barre de statut** | Informations sur la sélection, progression du scan, curseur de taille des vignettes |

### Masquer/afficher la sidebar

Appuyez sur **F9** ou allez dans **Affichage › Afficher/masquer sidebar**.  
La sidebar peut aussi être redimensionnée en faisant glisser le séparateur vertical.

### Plein écran

**F11** bascule l'application en plein écran.

---

## 3. Gérer vos dossiers

### La sidebar — section Dossiers

La sidebar gauche affiche l'arborescence de vos dossiers surveillés. Cliquez sur la flèche `▶` d'un dossier pour voir ses sous-dossiers. Cliquez sur un dossier pour afficher son contenu dans la grille.

### Menu contextuel (clic droit sur un dossier)

| Option | Effet |
|---|---|
| **Scanner maintenant** | Relance l'indexation du dossier (utile après avoir ajouté des fichiers depuis l'extérieur) |
| **Supprimer des dossiers surveillés** | Retire le dossier de la surveillance (ne supprime rien sur le disque) |
| **Créer un sous-dossier…** | Crée un nouveau sous-dossier directement depuis l'application |
| **Renommer…** | Renomme le dossier sur le disque et met à jour le catalogue |
| **Déplacer vers…** | Déplace le dossier ailleurs sur le disque et met à jour toutes les références |
| **Ouvrir dans l'Explorateur** | Ouvre le dossier dans l'Explorateur Windows |

> **Note :** Renommer ou déplacer un dossier met automatiquement à jour les chemins dans le catalogue et les retouches associées. Aucune donnée n'est perdue.

### Scanner un dossier

Après avoir copié de nouvelles photos dans un dossier surveillé depuis l'extérieur de l'application, faites **clic droit › Scanner maintenant** sur ce dossier pour que les nouvelles photos apparaissent.

---

## 4. La grille de photos

### Naviguer dans la grille

- **Clic sur un dossier** dans la sidebar → affiche les photos de ce dossier.
- **Défilement** avec la molette de la souris pour parcourir les vignettes.

### Taille des vignettes

Le curseur **Taille** en bas à droite de la fenêtre permet de choisir parmi quatre tailles : petite, moyenne, grande, très grande.

### Sélectionner des photos

| Action | Résultat |
|---|---|
| Clic simple | Sélectionne la photo (désélectionne les autres) |
| **Ctrl + Clic** | Ajoute/retire la photo de la sélection |
| **Shift + Clic** | Sélectionne une plage de photos |
| **Ctrl + A** | Sélectionne toutes les photos du dossier |

La barre de statut affiche le nombre de photos sélectionnées et le total.

### Ouvrir une photo

**Double-clic** sur une vignette → ouvre la visionneuse.

### Menu contextuel de vignette (clic droit)

| Option | Effet |
|---|---|
| **Ouvrir** | Ouvre la photo dans la visionneuse |
| **Marquer comme favori / Retirer des favoris** | Gère l'état favori |
| **Informations EXIF** | (prévu) Affiche les métadonnées EXIF |
| **Renommer l'image** | Renomme le fichier sur le disque |
| **Révéler dans l'Explorateur** | Ouvre le dossier contenant la photo |
| **Effacer le fichier…** | Supprime définitivement le fichier après confirmation |

### Supprimer des photos

Sélectionnez une ou plusieurs photos, puis appuyez sur la touche **Suppr** (ou clic droit › Effacer le fichier…). Une confirmation est demandée. La suppression est **définitive** (pas de corbeille).

### Renommer une photo

Clic droit sur la vignette › **Renommer l'image** — saisissez le nouveau nom sans extension. Le fichier est renommé sur le disque et le catalogue est mis à jour.

---

## 5. Visualiser une photo

### Ouvrir la visionneuse

Double-cliquez sur une vignette dans la grille.

### Navigation entre photos

| Action | Résultat |
|---|---|
| **← / ↑** ou **◀ Précédente** | Photo précédente |
| **→ / ↓** ou **Suivante ▶** | Photo suivante |
| **Échap** ou **✕** | Retour à la grille |
| **▦** (barre de statut) | Retour à la grille |

### Zoom

| Action | Résultat |
|---|---|
| Curseur **Zoom** (barre de statut) | Ajuste le zoom de 10 % à 400 % |
| **0** | Ajuster à la fenêtre (fit) |
| **1** | Zoom 100 % (1 pixel photo = 1 pixel écran) |
| **⊡** (barre d'outils) | Ajuster à la fenêtre |
| **1:1** (barre d'outils) | Zoom 100 % |
| Cliquer-glisser | Déplacer l'image dans la fenêtre |

> En **mode recadrage**, la molette de la souris sert à zoomer. En **mode normal**, elle passe à la photo suivante ou précédente.

### Marquer comme favori

Cliquez sur le bouton **♡** dans la barre d'outils de la visionneuse. L'étoile pleine **★** indique un favori actif.

---

## 6. Retoucher une photo

### Accéder au panneau de retouche

Ouvrez une photo dans la visionneuse. Le panneau **Retouche** apparaît automatiquement à gauche.

> **Principe non destructif** : les retouches ne modifient jamais le fichier original. Elles sont stockées dans une base de données séparée et appliquées à la volée à l'affichage et à l'export. Vous pouvez toujours récupérer l'original.

---

### Corrections tonales

Cliquez sur l'un des six boutons de correction pour ouvrir son dialogue de réglage.

| Correction | Plage | Description |
|---|---|---|
| **Luminosité** | -1,00 à +1,00 | Éclaircit ou assombrit l'image |
| **Contraste** | -1,00 à +1,00 | Accentue ou réduit l'écart entre les tons clairs et foncés |
| **Saturation** | -1,00 à +1,00 | Intensifie ou désature les couleurs (−1 = noir et blanc) |
| **Gamma** | 0,10 à 3,00 | Ajuste la courbe de luminosité (1,0 = neutre) |
| **Netteté** | 0,00 à 1,00 | Accentue les contours |
| **Débruitage** | 0,00 à 1,00 | Lisse le bruit numérique |

**Dans chaque dialogue de correction :**
- Le **slider** principal règle la valeur.
- Les **flèches ▲ ▼** à droite permettent un ajustement fin au centième.
- **Double-clic sur le slider** remet la valeur à zéro.
- L'**aperçu** se met à jour en temps réel dans la visionneuse.
- **Valider** applique le réglage ; **Annuler** restaure la valeur précédente.

---

### Géométrie

#### Rotation

- **↺ -90°** : rotation de 90° dans le sens anti-horaire.
- **↻ +90°** : rotation de 90° dans le sens horaire.

#### Redresser

Corrige l'inclinaison de l'horizon. Plage : **-10° à +10°**.

1. Cliquez sur **Redresser**.
2. Une grille de référence s'affiche sur l'image.
3. Ajustez l'angle avec le slider jusqu'à aligner l'horizon avec les lignes de la grille.
4. Cliquez **Valider**.

#### Recadrer

1. Cliquez sur **Recadrer** dans le panneau de retouche.
2. La visionneuse passe en **mode recadrage** (l'image originale complète est affichée).

**Choisir un format :**

| Bouton | Format | Ratio |
|---|---|---|
| Libre | Quadrilatère quelconque | Aucun |
| 10×15 (horizontal) | Tirage photo standard paysage | 3:2 |
| 10×15 (vertical) | Tirage photo standard portrait | 2:3 |
| 13×18 (horizontal) | Grand tirage paysage | 18:13 |
| 13×18 (vertical) | Grand tirage portrait | 13:18 |

**Définir la zone de recadrage :**
- **Cliquer-glisser** sur l'image : dessine la zone de recadrage.
- **Poignées de coin** : redimensionnent la zone.
- **Poignées d'arête** (milieu de chaque côté) : déplacent un bord.
- **Cliquer-glisser au centre** : déplace la zone sans la redimensionner.
- **Molette** : zoome dans la visionneuse pour plus de précision.

**Valider ou annuler :**
- **✓ Confirmer le recadrage** (ou **Entrée**) : applique le recadrage.
- **✕ Annuler** (ou **Échap**) : annule sans modifier.

> Pour re-recadrer une photo déjà recadrée, cliquez à nouveau sur **Recadrer** : l'image originale complète est affichée avec la zone précédente déjà positionnée, prête à être modifiée.

#### Miroir

- **Miroir H** : retourne l'image horizontalement (symétrie gauche-droite).
- **Miroir V** : retourne l'image verticalement (symétrie haut-bas).

---

### Annuler / Rétablir

| Bouton | Raccourci | Effet |
|---|---|---|
| **↩** | Ctrl + Z | Annule la dernière retouche |
| **↪** | Ctrl + Y | Rétablit la retouche annulée |

L'historique conserve les **20 dernières actions** en mémoire, et jusqu'à **50 états** sont sauvegardés sur disque — ils sont restaurés à la prochaine ouverture de la photo.

---

## 7. Albums et favoris

### Favoris

- Dans la **grille** : clic droit sur une vignette › **Marquer comme favori**.
- Dans la **visionneuse** : cliquez sur **♡**.
- Pour voir tous vos favoris : cliquez sur **♡ Favoris** dans la section Albums de la sidebar.

### Albums

Les albums permettent de regrouper des photos issues de dossiers différents.

**Créer un album :**
1. Cliquez sur le bouton **+** dans l'en-tête Albums de la sidebar.
2. Saisissez un nom.

**Accéder à un album :**
Cliquez sur son nom dans la liste Albums.

> Pour ajouter des photos à un album, cette fonctionnalité est disponible via le menu contextuel des vignettes (clic droit).

### Toutes les photos

Cliquez sur **★ Toutes les photos** dans la sidebar pour afficher toutes les photos de tous les dossiers surveillés.

---

## 8. Recherche

La barre de **recherche** se trouve en haut de la fenêtre.

- **Ctrl + F** : active la barre de recherche.
- Tapez un terme pour filtrer par **nom de fichier**, **marque d'appareil** ou **modèle d'appareil**.
- La recherche est instantanée (délai de 150 ms après la frappe).
- Cliquez sur **✕** à droite de la barre ou effacez le texte pour revenir à l'affichage normal.

---

## 9. Déplacer des photos

### Glisser-déposer vers un dossier

1. Dans la grille, **cliquez sur une vignette** et maintenez le bouton de la souris.
2. Faites glisser vers un **dossier de la sidebar** (le dossier se met en surbrillance).
3. Relâchez la souris.

Pour déplacer **plusieurs photos** simultanément :
1. Sélectionnez-les avec Ctrl+Clic ou Shift+Clic.
2. Faites glisser l'une des photos sélectionnées vers le dossier destination.
   Toutes les photos de la sélection sont déplacées.

Après le déplacement :
- Le fichier est déplacé sur le disque.
- Le catalogue, les retouches et les vignettes sont mis à jour automatiquement.
- La grille bascule sur le **dossier de destination** pour confirmer le résultat.

> Si une photo avec ce nom existe déjà dans le dossier destination, le déplacement est annulé pour ce fichier et un message d'erreur s'affiche.

---

## 10. Raccourcis clavier

### Grille

| Raccourci | Action |
|---|---|
| **Ctrl + F** | Activer la recherche |
| **Ctrl + A** | Sélectionner toutes les photos |
| **Suppr** | Supprimer les photos sélectionnées (avec confirmation) |
| **F9** | Afficher/masquer la sidebar |
| **F11** | Plein écran |

### Visionneuse

| Raccourci | Action |
|---|---|
| **← / ↑** | Photo précédente |
| **→ / ↓** | Photo suivante |
| **0** | Ajuster à la fenêtre |
| **1** | Zoom 100 % |
| **Échap** | Retour à la grille |
| **F** | Marquer/retirer des favoris |

### Mode recadrage

| Raccourci | Action |
|---|---|
| **Entrée** | Confirmer le recadrage |
| **Échap** | Annuler le recadrage |
| **Molette** | Zoomer dans la visionneuse |

### Panneau de retouche

| Raccourci | Action |
|---|---|
| **Ctrl + Z** | Annuler la dernière retouche |
| **Ctrl + Y** | Rétablir |

---

## 11. Où sont stockées vos données

Toutes les données de l'application se trouvent dans :

```
%LOCALAPPDATA%\PixelPhotoManager\
```

Soit typiquement : `C:\Users\VotreNom\AppData\Local\PixelPhotoManager\`

| Fichier | Contenu |
|---|---|
| `catalog.db` | Index de toutes vos photos (chemins, EXIF, métadonnées) |
| `thumbnails.db` | Cache des vignettes générées |
| `edits.db` | Toutes vos retouches et leur historique |
| `config.json` | Dossiers surveillés et préférences de l'interface |
| `logs\pixelphotomanager.log` | Journal de l'application (pour le dépannage) |

> **Vos photos originales ne sont jamais modifiées.** Vous pouvez supprimer `edits.db` pour effacer toutes les retouches et repartir de zéro, ou supprimer `catalog.db` pour forcer une réindexation complète.

### Formats d'images supportés

`.jpg` · `.jpeg` · `.png` · `.tiff` · `.tif` · `.webp` · `.bmp` · `.gif` · `.heic` · `.raw` · `.cr2` · `.nef` · `.arw` · `.dng`

*(La disponibilité des formats RAW dépend des pilotes installés sur le système.)*

---

## Annexe — Résolution de problèmes courants

| Problème | Solution |
|---|---|
| Des photos n'apparaissent pas après les avoir copiées | Clic droit sur le dossier dans la sidebar › **Scanner maintenant** |
| La vignette d'une photo retouchée n'est pas à jour | Ouvrez la photo, les retouches sont appliquées automatiquement |
| L'application est lente au démarrage | Normal lors du premier scan d'une grande bibliothèque ; le scan suivant sera beaucoup plus rapide (seules les nouvelles photos sont analysées) |
| Un dossier déplacé manuellement (hors de l'application) n'est plus trouvé | Utilisez **Supprimer des dossiers surveillés** puis **Fichier › Ajouter un dossier…** pour le réenregistrer |
| Récupérer l'original d'une photo retouchée | Le fichier original n'a jamais été modifié — il suffit d'effacer les retouches via **Annuler** (↩) jusqu'à l'état initial |
