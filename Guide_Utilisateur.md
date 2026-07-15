# Guide utilisateur — PixelPhotoManager

> **PixelPhotoManager** est un gestionnaire de photos et vidéos non destructif pour Windows. Vos fichiers originaux ne sont jamais modifiés : toutes les retouches sont stockées séparément et appliquées à la volée à l'affichage.

---

## Table des matières

1. [Premiers pas](#1-premiers-pas)
2. [Interface générale](#2-interface-générale)
3. [Gérer vos dossiers](#3-gérer-vos-dossiers)
4. [La grille de photos](#4-la-grille-de-photos)
5. [Mode chronologie (vue en ruban)](#5-mode-chronologie-vue-en-ruban)
6. [Visualiser une photo ou une vidéo](#6-visualiser-une-photo-ou-une-vidéo)
7. [Retoucher une photo](#7-retoucher-une-photo)
8. [Albums et favoris](#8-albums-et-favoris)
9. [Recherche et filtrage](#9-recherche-et-filtrage)
10. [Déplacer des photos](#10-déplacer-des-photos)
11. [Enregistrer et exporter vos photos](#11-enregistrer-et-exporter-vos-photos)
12. [Diaporama](#12-diaporama)
13. [Reconnaissance faciale](#13-reconnaissance-faciale)
14. [Détection des doublons](#14-détection-des-doublons)
15. [Synchroniser les dates de création avec l'EXIF](#15-synchroniser-les-dates-de-création-avec-lexif)
16. [Autres outils](#16-autres-outils)
17. [Raccourcis clavier](#17-raccourcis-clavier)
18. [Où sont stockées vos données](#18-où-sont-stockées-vos-données)

---

## 1. Premiers pas

### Premier lancement

Au premier démarrage, une fenêtre d'accueil vous invite à choisir au moins un **dossier de photos** à surveiller.

1. Cliquez sur **+ Ajouter un dossier** et sélectionnez un dossier contenant vos photos.
2. Répétez l'opération si vous avez plusieurs dossiers.
3. Cliquez sur **Commencer →**.

L'application indexe immédiatement vos photos et vidéos en arrière-plan. La barre de statut en bas indique la progression du scan (`Scan… 42%  —  nom_du_fichier.jpg`).

### Ajouter un dossier ultérieurement

Menu **Fichier › Ajouter un dossier…** — sélectionnez n'importe quel dossier, il sera scanné automatiquement et ajouté à la sidebar.

### Quitter l'application

Menu **Fichier › Quitter** (**Ctrl + Q**).

---

## 2. Interface générale

```
┌─────────────────────────────────────────────────────────┐
│  [Fichier][Affichage][Outils][Visages][Aide]  [Export]   │
├──────────────────┬──────────────────────────────────────┤
│                  │                                       │
│   SIDEBAR        │   ZONE PRINCIPALE                     │
│                  │   (grille, ruban, ou visionneuse)      │
│   Dossiers       │                                       │
│   Albums         │                                       │
│                  │                                       │
├──────────────────┴──────────────────────────────────────┤
│  Barre de statut          [Taille ────────] [▦]         │
└─────────────────────────────────────────────────────────┘
```

| Zone | Rôle |
|---|---|
| **Barre de menus** | Fichier, Affichage, Outils, Visages, Aide — voir sections dédiées |
| **Bouton ⬆ Exporter** | Exporte la photo affichée (visionneuse) ou la sélection (grille) |
| **Sidebar** | Filtrage, navigation dans les dossiers et albums |
| **Zone principale** | Grille de vignettes, vue chronologie (ruban), vue Personnes, vue Doublons ou visionneuse plein écran |
| **Barre de statut** | Informations sur la sélection, progression du scan, curseur de taille des vignettes |

### Les cinq menus

| Menu | Contenu |
|---|---|
| **Fichier** | Ajouter un dossier…, Quitter |
| **Affichage** | Afficher/masquer sidebar (F9), Plein écran (F11), Diaporama (F5) |
| **Outils** | Dossiers…, Détecter les doublons…, Synchroniser dates de création avec l'EXIF…, Journal des threads…, Historique des problèmes…, Applications externes…, Paramètres |
| **Visages** | Importer depuis Picasa…, Réinitialiser et réindexer…, Regrouper les visages…, Visualisation des erreurs…, Sauvegarder la reconnaissance…, Gérer les sauvegardes…, Compteurs… |
| **Aide** | Aide… (F1), À propos |

### Masquer/afficher la sidebar

Appuyez sur **F9** ou allez dans **Affichage › Afficher/masquer sidebar**.
La sidebar peut aussi être redimensionnée en faisant glisser le séparateur vertical.

### Plein écran

**F11** bascule l'application en plein écran.

### Vérification des mises à jour

Au démarrage, l'application interroge en arrière-plan la page des releases GitHub du projet. Si une version plus récente est disponible, une popup s'affiche avec le numéro de version et un bouton **Ouvrir la page de téléchargement** ; elle rappelle de lire les notes de version avant d'installer, pour connaître les nouvelles fonctionnalités et vérifier la compatibilité avec votre bibliothèque existante. Si vous êtes déjà à jour, ou en cas d'erreur réseau, rien ne s'affiche.

L'onglet **À propos** (menu **Aide › À propos**) effectue la même vérification à chaque ouverture et affiche l'un des trois résultats : « ✓ Vous disposez de la dernière version », l'alerte de mise à jour disponible avec son lien, ou un message d'erreur si la vérification est impossible (pas de connexion).

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

### Gestionnaire de dossiers (Outils › Dossiers…)

Le menu **Outils › Dossiers…** ouvre un dialogue de gestion avancée des dossiers surveillés.

**Pour chaque dossier, le dialogue affiche :**
- Un indicateur ✓ (dossier trouvé sur le disque) ou ✗ (dossier introuvable)
- Le chemin complet du dossier
- Le nombre de fichiers indexés
- Les sous-dossiers exclus du scan (dossiers cachés, dossiers `Originals` de Picasa, dossiers `.tmp_*`) avec la raison de l'exclusion

**Actions disponibles :**
- **⟳ Re-scanner** — force le re-scan complet du dossier, même pour les fichiers non modifiés depuis le dernier scan. Utile pour détecter des modifications faites hors de l'application.
- **Retirer** — retire le dossier de la surveillance (les photos déjà indexées restent dans le catalogue ; seul le scan futur est désactivé).
- **＋ Ajouter un dossier…** — ajoute un nouveau dossier à surveiller.

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

### Vignettes vidéo

Les vidéos sont représentées dans la grille par une vignette extraite automatiquement (à environ 10 % de la durée de la vidéo), avec un badge **▶** superposé pour les distinguer des photos.

### Badge de doublons

Une photo faisant partie d'un groupe de doublons détecté (voir [section 14](#14-détection-des-doublons)) affiche un badge **⧉** orange sur sa vignette. Cliquer dessus ouvre une popup **« Doublons de cette photo »** listant les autres exemplaires (nom + dossier) ; un double-clic sur un élément de la liste navigue directement vers ce fichier.

### Ouvrir une photo ou une vidéo

**Double-clic** sur une vignette → ouvre la visionneuse.

### Menu contextuel de vignette (clic droit)

| Option | Effet |
|---|---|
| **Ouvrir** | Ouvre la photo dans la visionneuse |
| **Marquer comme favori / Retirer des favoris** | Gère l'état favori |
| **Renommer l'image** | Renomme le fichier sur le disque |
| **Déplacer vers…** | Déplace le fichier vers un autre dossier surveillé |
| **Enregistrer l'image traitée sur le disque** | Ouvre le dialogue d'enregistrement (voir [section 11](#11-enregistrer-et-exporter-vos-photos)) |
| **Ajouter {cette photo\|les N photos sélectionnées} à un album…** | Ajoute la photo (ou toute la sélection) à un album existant |
| **Créer un nouvel album avec {cette photo\|les N photos sélectionnées}…** | Crée un nouvel album à la volée avec la photo (ou la sélection) |
| **Révéler dans l'Explorateur** | Ouvre le dossier contenant la photo |
| **Retenter l'identification des visages** | *(affiché uniquement si la photo est en erreur de détection faciale)* relance l'analyse pour ce seul fichier |
| **Effacer le fichier…** | Supprime définitivement le fichier après confirmation |

> Le clic droit s'applique à **toute la sélection en cours** (Ctrl+Clic / Shift+Clic) pour Renommer/Déplacer/Ajouter à un album, pas seulement à la vignette cliquée.

### Supprimer des photos

Sélectionnez une ou plusieurs photos, puis appuyez sur la touche **Suppr** (ou clic droit › Effacer le fichier…). Une confirmation est demandée. La suppression est **définitive** (pas de corbeille).

### Renommer une photo

Clic droit sur la vignette › **Renommer l'image** — saisissez le nouveau nom sans extension. Le fichier est renommé sur le disque et le catalogue est mis à jour.

---

## 5. Mode chronologie (vue en ruban)

Cliquer sur **★ Chronologie de toutes les photos** dans la sidebar (album spécial, tout en haut de la liste Albums) affiche vos photos dans une **vue en ruban** plutôt qu'une grille classique.

### Principe

- Les photos sont réparties sur **cinq rangées** de tailles décroissantes vers le haut et le bas ; la **rangée centrale** (nettement plus grande) contient la « photo courante ».
- Un petit encart flottant affiche la **date** de la photo actuellement au centre du ruban.
- La photo centrale sert de point de départ si vous lancez un diaporama (**F5**) depuis cette vue, et c'est elle qui est supprimée par **Suppr** en l'absence d'autre sélection.

### Navigation

| Action | Résultat |
|---|---|
| **Molette** | Défilement avec inertie (glisse progressivement, comme un ruban physique) |
| **← / →** | Avance/recule d'une photo |
| **↑ / ↓** | Avance/recule de 3 photos |
| **Ascenseur vertical** (à droite de la vue) | Navigation directe, visible uniquement en mode chronologie |
| **Suppr** | Supprime la photo centrale (ou la sélection en cours) |

> Ce mode n'est actif que pour l'album **Chronologie de toutes les photos** ; les autres albums (Favoris, Vidéos, Par nom de fichier, dossiers, albums personnalisés) s'affichent en grille classique.

---

## 6. Visualiser une photo ou une vidéo

### Ouvrir la visionneuse

Double-cliquez sur une vignette dans la grille.

### Navigation entre photos

| Action | Résultat |
|---|---|
| **← / ↑** ou **◀ Précédente** | Photo précédente (s'arrête à la première photo) |
| **→ / ↓** ou **Suivante ▶** | Photo suivante (s'arrête à la dernière photo) |
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
| **Ctrl + Molette** | Zoom avant/arrière |
| Cliquer-glisser | Déplacer l'image dans la fenêtre |

> En **mode recadrage**, la molette de la souris sert à zoomer. En **mode normal**, elle passe à la photo suivante ou précédente.

### Panneau EXIF

Appuyez sur **I** (ou cliquez sur le bouton `[i]` dans la barre d'outils) pour afficher/masquer le panneau EXIF. Il est organisé en sections :

- **Fichier** : nom, format, mode couleur, dimensions, taille, date de modification.
- **Appareil photo** : fabricant, modèle, n° de série, objectif (fabricant/modèle/spécification/n° de série), logiciel.
- **Prise de vue** : date, exposition, ouverture (et ouverture maximale), ISO (+ type de sensibilité), focale (+ équivalent 35 mm), zoom numérique, programme d'exposition, mode de mesure, correction d'exposition, luminosité, balance des blancs, source lumineuse, flash (décodé en texte, ex. *« Déclenché, retour détecté »*), type de scène, distance au sujet, contraste/saturation/netteté, rendu personnalisé.
- **Image** : dimensions en pixels, espace colorimétrique, orientation, résolution, compression, bits par échantillon.
- **Auteur / Droits** : artiste, copyright, description, et champs Windows (titre, commentaire, auteur, mots-clés, sujet).
- **GPS** *(si présent)* : latitude/longitude, **altitude**, **vitesse GPS**, **direction de prise de vue** (vraie ou magnétique), **date/heure GPS**, **précision (DOP)**.
- **Vidéo** *(pour les vidéos)* : résolution, images/seconde, durée, codec.
- **Autres** : tous les autres tags EXIF présents, non couverts par les groupes ci-dessus.

#### Modifier les métadonnées EXIF

Le bouton **✎ Modifier les métadonnées…** en bas du panneau (désactivé pour les vidéos) ouvre un dialogue permettant de modifier directement le fichier :

- **Date de prise de vue** (calendrier)
- **Description**
- **Artiste**
- **Copyright**

Une case **« Mettre à jour aussi la date du fichier (mtime + date de création) »** (cochée par défaut) applique en plus la nouvelle date au fichier lui-même sur le disque.

> ⚠ Contrairement aux retouches d'image (non destructives), cette opération **écrit directement dans le fichier**.

### Visualiser une vidéo

Quand vous ouvrez une vidéo dans la visionneuse :
- La première image extraite est affichée.
- Un bouton **▶ Ouvrir la vidéo** apparaît dans la barre d'outils.
- Cliquez dessus pour lire la vidéo dans le lecteur configuré (voir **Paramètres**, [section 16](#16-autres-outils) — lecteur système par défaut ou lecteur personnalisé).
- Le panneau de retouche n'est **pas disponible** pour les vidéos.

### Localiser sur la carte

Si une photo contient des coordonnées GPS, faites un **clic droit** dans la visionneuse et choisissez **Localiser sur la carte**. OpenStreetMap s'ouvre dans le navigateur, centré sur le lieu de prise de vue.

> L'option est grisée si la photo ne contient pas de données GPS.

### Marquer comme favori

Cliquez sur le bouton **♡** dans la barre d'outils de la visionneuse (ou la touche **F**). L'étoile pleine **★** indique un favori actif.

### Applications externes

Si vous avez configuré des applications tierces (voir [section 16](#16-autres-outils)), leurs icônes apparaissent dans la barre d'outils de la visionneuse, à côté du bouton favori. Un clic lance l'application avec la photo courante en argument (info-bulle : « Ouvrir avec *nom* »).

### Menu contextuel dans la visionneuse (clic droit)

| Option | Effet |
|---|---|
| **Marquer comme favori / Retirer des favoris** | Gère l'état favori |
| **Renommer…** | Renomme le fichier sur le disque |
| **Déplacer vers…** | Déplace le fichier vers un autre dossier |
| **Enregistrer l'image traitée sur le disque** | Ouvre le dialogue d'enregistrement (voir [section 11](#11-enregistrer-et-exporter-vos-photos)) |
| **Révéler dans l'Explorateur** | Ouvre le dossier contenant la photo |
| **Afficher le dossier dans la grille** | Retourne à la grille de photos, en affichant le dossier contenant la photo courante et en la sélectionnant |
| **Localiser sur la carte** | Ouvre OpenStreetMap à la position GPS (grisé si pas de GPS) |
| **Forcer une nouvelle détection sans limite de taille** | Relance la détection de visages sur cette photo en ignorant le filtre de taille minimale, sans perdre les identifications déjà faites |
| **Effacer le fichier…** | Supprime définitivement le fichier après confirmation |

---

## 7. Retoucher une photo

### Accéder au panneau de retouche

Ouvrez une photo dans la visionneuse. Le panneau **Retouche** apparaît automatiquement à gauche.

> **Principe non destructif** : les retouches ne modifient jamais le fichier original. Elles sont stockées dans une base de données séparée et appliquées à la volée à l'affichage et à l'export. Vous pouvez toujours récupérer l'original.

> Le panneau de retouche n'est pas disponible pour les vidéos.

> **Un seul outil actif à la fois** : sélectionner un nouvel outil (Recadrer, Yeux rouges, Annotations, Luminosité, Contraste, Couleurs, Vignette, Redresser…) valide automatiquement le travail en cours dans l'outil précédent puis le referme — inutile de valider manuellement avant de changer d'outil.

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

### Couleurs (Noir & Blanc avec mixage de canaux)

Le traitement **Couleurs** convertit la photo en noir et blanc avec contrôle des contributions de chaque canal.

1. Cliquez sur **Couleurs** dans le panneau de retouche.
2. Cochez **Noir & Blanc** pour activer la conversion.
3. Ajustez les trois curseurs **Rouge**, **Vert**, **Bleu** (plage −1,00 à +1,00) pour doser la contribution de chaque canal dans les tons de gris.
4. Cliquez **Valider**.

> Un rouge à +1,00 avec bleu à −1,00 donne un résultat dramatique avec un ciel foncé et des peaux claires — l'équivalent d'un filtre rouge argentique.

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

### Yeux rouges

1. Cliquez sur **Yeux rouges** dans le panneau de retouche.
2. Cliquez directement sur chaque œil affecté dans la photo — un cercle de correction apparaît.
3. Ajustez le curseur **Taille** si besoin (0,5 % à 8 % de la plus petite dimension de l'image).
4. **Effacer tout** supprime toutes les corrections posées ; **Terminé** (ou **Échap**) quitte le mode sans rien perdre.

---

### Annotations

Calque de dessin et de texte **non destructif**, superposé à la photo — indépendant des corrections tonales et de la géométrie, et conservé séparément dans les retouches de la photo.

1. Cliquez sur **Annotations** dans le panneau de retouche.

**Outils disponibles :**

| Outil | Effet |
|---|---|
| Stylo | Trait libre à main levée |
| Ligne | Ligne droite entre deux points |
| Courbe | Cliquez les points de passage successifs, **double-clic** pour valider le tracé |
| Rectangle | Forme rectangulaire (contour et/ou remplissage) |
| Ellipse | Forme elliptique (contour et/ou remplissage) |
| Texte | Zone de texte éditable directement sur la photo |
| Sélection | Sélectionne un élément existant pour le déplacer, le redimensionner ou modifier son style |

**Style** (panneau qui s'adapte à l'outil sélectionné) :
- **Couleur** du trait ou du texte, **épaisseur** du trait (% de l'image).
- Pour les formes : **couleur de remplissage**, **opacité**, et **flou** de la photo sous la forme.
- Pour le texte : police, taille (% de l'image), **G** (gras), **I** (italique), couleur.

**Modifier ou supprimer :**
- Avec l'outil **Sélection**, cliquez un élément pour le sélectionner (poignées de redimensionnement), ou faites-le glisser pour le déplacer.
- **Double-clic** sur un texte existant : rouvre l'éditeur en place.
- **Supprimer la sélection** (ou touche **Suppr**) : supprime l'élément sélectionné. **Effacer annotations** : supprime tout le calque de la photo.

**Afficher / masquer le calque :**
Le bouton **✏ Annotations** en haut de la fenêtre (à côté du bouton EXIF, visible dès qu'une photo est ouverte) affiche ou masque le calque sans rien supprimer. Ce réglage n'est pas enregistré : il ne dure que le temps de la session en cours.

> Les annotations sont incluses dans l'export et dans l'enregistrement de l'image traitée sur le disque, comme les autres retouches — sauf si le calque est masqué via **✏ Annotations** au moment de l'export.

---

### Annuler / Rétablir

| Bouton | Raccourci | Effet |
|---|---|---|
| **↩** | Ctrl + Z | Annule la dernière retouche |
| **↪** | Ctrl + Y | Rétablit la retouche annulée |

L'historique conserve les **20 dernières actions** en mémoire, et jusqu'à **50 états** sont sauvegardés sur disque — ils sont restaurés à la prochaine ouverture de la photo.

---

## 8. Albums et favoris

### Favoris

- Dans la **grille** : clic droit sur une vignette › **Marquer comme favori**.
- Dans la **visionneuse** : cliquez sur **♡** (ou touche **F**).
- Pour voir tous vos favoris : cliquez sur **♡ Favoris** dans la section Albums de la sidebar.

### Albums spéciaux (toujours présents, non supprimables)

| Album | Icône | Effet |
|---|---|---|
| **Chronologie de toutes les photos** | ★ | Affiche toute la bibliothèque en [vue chronologie/ruban](#5-mode-chronologie-vue-en-ruban) |
| **Favoris** | ♡ | Photos marquées comme favorites |
| **Vidéos** | ▶ | Toutes les vidéos de la bibliothèque |
| **Par nom de fichier** | 🔍 | Résultat du texte tapé dans le filtre de la sidebar (voir [section 9](#9-recherche-et-filtrage)) |

### Créer un album personnalisé

**Depuis la sidebar :**
1. Cliquez sur le bouton **+** dans l'en-tête Albums de la sidebar.
2. Saisissez un nom.

**Depuis la grille (avec des photos déjà choisies) :**
Clic droit sur une vignette (ou une sélection) › **Créer un nouvel album avec…** — saisissez le nom du nouvel album ; il est créé et rempli immédiatement avec la ou les photos sélectionnées.

### Ajouter des photos à un album existant

Clic droit sur une vignette (dans la grille **ou** dans la vue Personnes) › **Ajouter à un album…** — une liste des albums existants (avec leur nombre de photos) s'affiche ; double-cliquez sur l'album cible ou sélectionnez-le puis validez.

> Si aucun album personnalisé n'existe encore, un message vous invite à en créer un d'abord via le panneau Albums.

### Supprimer un album

Clic droit sur un album personnalisé dans la sidebar › **Supprimer l'album…** — une confirmation précise le nombre de photos concernées et rappelle que **les photos restent intactes** dans le catalogue et sur le disque, seul l'album est supprimé. *(Les 4 albums spéciaux ne proposent pas cette option.)*

### Accéder à un album

Cliquez sur son nom dans la liste Albums de la sidebar.

---

## 9. Recherche et filtrage

Le champ de filtrage se trouve en haut de la **sidebar** (placeholder *« 🔍 Filtrer dossiers, personnes et fichiers… »*).

- Tapez un terme : il filtre **instantanément** (à chaque frappe, sans délai) l'arborescence des **dossiers** et la liste des **personnes** identifiées dans la sidebar.
- Le même texte alimente l'album spécial **🔍 Par nom de fichier**, qui recherche dans le **catalogue complet** par nom de fichier, marque d'appareil ou modèle d'appareil (une photo peut donc apparaître dans ce résultat même si le terme tapé correspond à son appareil photo plutôt qu'à son nom de fichier).
- Un bouton **✕** intégré au champ efface le filtre.

---

## 10. Déplacer des photos

### Glisser-déposer vers un dossier

1. Dans la grille, **cliquez sur une vignette** et maintenez le bouton de la souris.
2. Faites glisser vers un **dossier de la sidebar** (le dossier se met en surbrillance).
3. Relâchez la souris.

Pour déplacer **plusieurs photos** simultanément :
1. Sélectionnez-les avec Ctrl+Clic ou Shift+Clic.
2. Faites glisser l'une des photos sélectionnées vers le dossier destination.
   Toutes les photos de la sélection sont déplacées.

Vous pouvez aussi utiliser le clic droit › **Déplacer vers…** (grille ou visionneuse) pour choisir la destination sans glisser-déposer.

Après le déplacement :
- Le fichier est déplacé sur le disque.
- Le catalogue, les retouches et les vignettes sont mis à jour automatiquement.
- La grille bascule sur le **dossier de destination** pour confirmer le résultat.

> Si une photo avec ce nom existe déjà dans le dossier destination, le déplacement est annulé pour ce fichier et un message d'erreur s'affiche.

---

## 11. Enregistrer et exporter vos photos

Il existe deux façons de sortir une image retouchée de l'application, selon votre besoin.

### Enregistrer l'image traitée (un fichier à la fois)

Clic droit sur une vignette ou dans la visionneuse › **Enregistrer l'image traitée sur le disque**. Une boîte de dialogue propose deux options :

- **Écraser le fichier original** *(coché par défaut)* — un avertissement rappelle que l'action est irréversible. Une case **« Copier l'original dans .tmp_originals avant l'écrasement »** (cochée par défaut) permet de conserver une copie de sauvegarde horodatée du fichier d'origine dans un sous-dossier caché `.tmp_originals` avant remplacement.
- **Enregistrer à un autre emplacement…** — ouvre l'explorateur Windows avec un nom suggéré (`nomoriginal_retouché.jpg`), pour conserver l'original intact et créer une copie retouchée à côté.

Dans les deux cas :
- Le fichier de sortie est enregistré en pleine résolution (JPEG qualité 95, ou PNG selon l'extension choisie).
- **La date du fichier (création + modification) est reprise de l'original**, pas de la date d'enregistrement — le fichier de sortie garde donc la date de la prise de vue originale.
- Si vous avez choisi d'écraser l'original, les retouches enregistrées dans l'application sont supprimées (elles sont désormais « figées » dans le fichier) et la vignette/l'aperçu sont rafraîchis.

### Exporter plusieurs photos (bouton ⬆ Exporter)

Le bouton **⬆ Exporter** de la barre d'outils principale exporte soit la photo affichée dans la visionneuse, soit toute la sélection de la grille. Le dialogue **« Exporter N photo(s) »** propose :

- **Dossier de destination** — champ éditable + bouton **Parcourir…** (par défaut `Documents\Pictures\PixelPhotoManager\Export`).
- **Taille d'export** (choix exclusif) :

| Option | Résolution max. | Qualité JPEG | Taille estimée |
|---|---|---|---|
| Taille maximale — résolution originale | Aucune | 95 | — |
| Grande (~4 Mpx) | 4 000 000 px | 98 | 600–1 600 Ko |
| Moyenne (~2 Mpx) | 2 000 000 px | 94 | 320–800 Ko |
| Petite (~500 kpx) | 500 000 px | 90 | 75–300 Ko |

Toutes les photos exportées sont converties en **JPEG**, avec les retouches appliquées ; en cas de conflit de nom dans le dossier de destination, un suffixe numérique est ajouté automatiquement. Comme pour l'enregistrement, **la date du fichier original est reportée sur chaque fichier exporté**. Une fois l'export terminé, le dossier de destination s'ouvre automatiquement dans l'Explorateur.

---

## 12. Diaporama

Lancez un diaporama depuis **Affichage › Diaporama** ou avec la touche **F5**.

Le diaporama s'ouvre en **plein écran** et parcourt les photos du contexte actuel.

### Point de départ

| Situation au lancement | Photo de départ |
|---|---|
| Visionneuse ouverte | La photo actuellement affichée |
| Mode chronologie (ruban) | La photo au **centre** du ruban |
| Autre vue | La plus ancienne photo du dossier |

### Effet Ken Burns

Chaque photo est animée d'un **léger zoom et d'un panoramique lent** (effet Ken Burns) :
- Le zoom varie de 0 à 8 % sur toute la durée d'affichage.
- La direction du mouvement est aléatoire, avec une préférence pour les déplacements **horizontaux et diagonaux**.
- Les photos dont le rapport hauteur/largeur ne correspond pas à l'écran sont affichées avec des **marges noires** (letterbox / pillarbox) — elles ne sont jamais rognées.

### Contrôles (barre en bas, visible au mouvement de la souris)

| Contrôle | Effet |
|---|---|
| **◀ Précédente** | Aller à la photo plus ancienne |
| **Suivante ▶** | Aller à la photo plus récente |
| **−** / **+** | Réduire / augmenter l'intervalle d'affichage (1 s à 60 s, par pas de 1 s) |
| **⏸ / ▶** | Mettre en pause / reprendre le défilement automatique |
| **✕** | Quitter le diaporama |

La barre disparaît automatiquement après 5 secondes d'inactivité et réapparaît au moindre mouvement de souris.

### Raccourcis clavier

| Raccourci | Action |
|---|---|
| **←** ou **↑** | Photo plus ancienne |
| **→** ou **↓** | Photo plus récente |
| **Espace** | Pause / Reprendre |
| **Échap** | Quitter le diaporama |

---

## 13. Reconnaissance faciale

> La reconnaissance faciale nécessite des dépendances optionnelles (InsightField/buffalo_l, scikit-learn, hdbscan). Si elles ne sont pas installées, cette section n'est pas disponible.

### Panneau « Visages » de la visionneuse

Ouvrez une photo puis affichez le panneau **Visages** (à côté du panneau EXIF — les deux sont mutuellement exclusifs).

- **Bouton « Tous »** : encadre tous les visages détectés sur la photo.
- **Vignettes de visage** : une carte par visage détecté, triées visages nommés d'abord. Le nom affiché sous chaque vignette indique la personne, `« Groupe N »` (cluster non nommé), `« Séparé »` (isolé manuellement) ou `« Inconnu »`.
  - **Clic** : sélectionne/désélectionne le visage (surbrillance sur la photo).
  - **Double-clic** sur un visage nommé : ouvre la vue détaillée de cette personne.
  - **✕** sur la vignette : ignore ce visage (masqué de l'UI et du regroupement, récupérable).
  - **Clic droit** : **Identifier cette personne…**, **Identifier ce groupe…**, **Désallouer le groupe**, **Ignorer ce visage**.
- **➕ Ajouter une personne** : bascule en mode dessin manuel d'un rectangle sur la photo (pour un visage non détecté automatiquement), validé par **✓ Valider la position** (ou Entrée) / **✕ Annuler** (ou Échap), puis choix du nom.
- **Visages ignorés… (N)** : liste les visages ignorés de la photo, avec position/taille, et un bouton **Restaurer** par ligne.
- Chaque action (ajout, identification, ignorer, restaurer) est **annulable** via le bouton Undo général de la visionneuse.

### Vue « Personnes » — groupes non identifiés

Accessible via l'icône/le bouton dédié qui remplace la grille de photos.

- **Cartes de groupe** : vignette + nombre de visages (ou « Isolé » pour un visage seul), avec une **suggestion de personne** si le système en trouve une (ex. *« ≈ Nom (82 %) »*).
  - Clic : sélection multiple cumulative ; Maj+clic : sélection étendue.
  - Double-clic : ouvre les photos du groupe.
  - Clic droit : **Identifier cette personne…** / **Identifier ce groupe…**, **Ignorer ce visage/ce groupe**, et en multi-sélection **Associer (N sélectionnés)**.
  - **Boutons ✓ / ✗ superposés sur chaque vignette** (toutes les cartes, pas seulement celles avec suggestion) : **✓** accepte directement la suggestion en un clic si une suggestion est proposée, sinon ouvre le dialogue d'identification ; **✗** ignore le visage (carte isolée) ou tout le groupe.
- **Sections de suggestion** (« ≈ Probablement la même personne » / « ≈ Probablement *Nom* ») avec boutons d'en-tête **Accepter**, **Associer à…**, **Ignorer** pour toute la section d'un coup.
- **Section « Visages isolés »** en bas de page.
- **Barre d'action** (dès qu'un groupe est sélectionné) : **Voir les photos**, **Associer à…**, **Ignorer**, **✕** (tout désélectionner).
- **Fusionner le groupe N** : choisissez un autre groupe dans une liste illustrée puis validez.
- Pagination : **Charger N de plus (N restant(s))**.

### Vue détaillée d'une personne

Ouverte par double-clic sur une personne nommée.

- **Section « En attente de vérification »** (suggestions non confirmées) : boutons **✗ Rejeter toutes** / **✓ Accepter toutes**, ou par vignette **✓**/**✗**.
- **Section confirmée** : clic droit sur un ou plusieurs visages sélectionnés › **Réassigner à une autre personne…**, **Dé-associer de la personne**, **Utiliser ce visage comme vignette principale**, **Ajouter à un album…**, **Créer un nouvel album avec…**.

### Menu « Visages » (barre de menu)

| Option | Effet |
|---|---|
| **Importer depuis Picasa…** | Parcourt les fichiers `.picasa.ini` de vos dossiers et importe les noms/régions de visages définis dans Picasa ; propose une case **« Importer aussi les retouches Picasa (rotation, recadrage, luminosité…) »**. N'écrase jamais une association déjà faite manuellement. À faire une seule fois (le menu se grise ensuite). |
| **Réinitialiser et réindexer…** | Deux options : **« Réinitialiser les groupes uniquement — rapide »** (garde les visages détectés, refait juste le regroupement) ou **« Réinitialisation complète + réindexation — lente »** (efface tout et relance la détection, peut prendre plusieurs heures) |
| **Regrouper les visages…** | Relance le clustering des visages non identifiés (durée estimée affichée avant de démarrer) |
| **Visualisation des erreurs…** | Liste les photos dont la détection a échoué (timeout/crash), avec un bouton **⟳ Réessayer** par ligne |
| **Sauvegarder la reconnaissance…** | Crée immédiatement une sauvegarde (archive) de l'état actuel des visages, groupes et personnes |
| **Gérer les sauvegardes…** | Liste les sauvegardes existantes (date, taille) avec **Restaurer** ou **✕ Supprimer** par ligne, et **＋ Créer une sauvegarde** |
| **Compteurs…** | Statistiques : personnes/visages identifiés, en attente de confirmation, inconnus ; import Picasa (importés/fusionnés/en attente) ; totaux (détectés, ignorés par taille, groupes) |

### Depuis une photo

- Visionneuse, clic droit › **Forcer une nouvelle détection sans limite de taille** — relance la détection sur cette photo en ignorant le filtre de taille minimale, sans perdre les identifications déjà faites.
- Grille, clic droit sur une photo en erreur › **Retenter l'identification des visages**.

---

## 14. Détection des doublons

Menu **Outils › Détecter les doublons…** analyse l'ensemble de la bibliothèque (les vidéos sont exclues) pour repérer les photos en double, y compris des versions redimensionnées, retouchées (couleur/luminosité) ou recadrées.

### Fonctionnement

L'analyse se déroule en deux passes automatiques (aucune option à régler) :

1. **Empreinte perceptuelle (pHash)** — détecte les doublons exacts, redimensionnés ou légèrement retouchés.
2. **Points d'intérêt (ORB + RANSAC)** — appliqué uniquement aux photos non regroupées à l'étape 1, détecte en plus les **recadrages** (jusqu'à ~60 % de surface recadrée).

L'analyse tourne **en arrière-plan** : une barre de progression et un bouton **Annuler** apparaissent dans la barre de statut, en bas de la fenêtre, et indiquent l'étape en cours (« Tier 1 — empreintes… », « Tier 2 — comparaison ORB… »). Vous pouvez continuer à utiliser PixelPhotoManager normalement pendant ce temps — les résultats n'apparaissent qu'à la fin de l'analyse.

- Cliquer sur **Annuler** demande confirmation (« Voulez-vous vraiment interrompre la détection de doublons en cours ? Les résultats calculés jusqu'ici seront perdus. »).
- Si vous fermez l'application pendant qu'une analyse est en cours, un avertissement s'affiche (« Une détection de doublons est en cours… le résultat sera perdu ») avec le choix **Fermer quand même** ou **Annuler**.

### Résultat

- Un message final indique le nombre de groupes et de fichiers concernés.
- Les photos en double sont marquées d'un **badge ⧉ orange** dans la grille et la visionneuse (voir [section 4](#4-la-grille-de-photos)) — l'application ne supprime ni ne fusionne rien automatiquement, à vous de décider au cas par cas.
- Un **rapport HTML** (`duplicates_report.html`) est généré automatiquement ; un bouton **Ouvrir le rapport** permet de le consulter directement.

### Grille des groupes de doublons (bouton « Dupliquées » de la sidebar)

Le bouton **Dupliquées** de la sidebar (avec un badge indiquant le nombre de groupes détectés) ouvre une grille dédiée listant **tous** les groupes de doublons d'un coup — une carte par groupe, avec la vignette du premier exemplaire et le nombre d'exemplaires.

- **Double-clic** sur une carte : ouvre les exemplaires du groupe dans la visionneuse, pour une comparaison rapide (navigation précédent/suivant limitée aux membres du groupe).
- **✕** sur une carte : **dissout le groupe entier** (ne supprime aucun fichier) — la carte disparaît de la grille et le badge décrémente. Cette dissolution n'est **pas persistante** : relancer une détection complète peut reformer le même groupe si les photos sont toujours similaires.
- Bouton **Détecter les doublons…** en haut de la grille pour relancer une analyse sans repasser par le menu Outils.

### Fichiers corrompus détectés pendant l'analyse

Un fichier qui ne peut pas être lu pendant l'analyse (JPEG endommagé, copie interrompue…) n'est plus ignoré silencieusement : il est comptabilisé et signalé.

- Le message de bilan de fin d'analyse indique le nombre de fichiers illisibles rencontrés, avec un bouton **Réparer…**.
- Une confirmation est demandée avant toute tentative de réparation.
- La réparation essaie de ré-enregistrer une copie propre du fichier via un décodeur plus tolérant que celui utilisé pour l'analyse (PIL en mode tolérant aux troncatures, puis le codec JPEG de Qt). L'original est sauvegardé au préalable dans un dossier caché `.tmp_originals` à côté du fichier, et les dates Windows de modification **et** de création sont préservées à l'identique sur la copie réparée.
- Un second bilan indique le nombre de fichiers réparés. Ceux qui n'ont pas pu l'être (corruption trop importante pour les décodeurs disponibles) sont listés dans un fichier texte horodaté (`fichiers_corrompus_AAAAMMJJ_HHMMSS.txt`), dont l'emplacement reste accessible via **Outils › Historique des problèmes…** (voir [section 16](#16-autres-outils)).

---

## 15. Synchroniser les dates de création avec l'EXIF

Menu **Outils › Synchroniser dates de création avec l'EXIF…**

**Pourquoi :** lors d'un transfert ou d'une copie de fichiers, Windows attribue parfois la date du jour comme « date de création », en écrasant la date réelle de prise de vue contenue dans l'EXIF.

**Ce que fait l'outil :** parcourt tout le catalogue et, quand la date EXIF diffère de la date de création Windows (au-delà d'une tolérance de 2 secondes), **remplace la date de création du fichier** par la date EXIF. Les photos sans EXIF valide, ou dont le fichier est introuvable, sont simplement ignorées et comptabilisées comme telles.

> ⚠ Cette opération modifie les métadonnées système des fichiers originaux (dates), pas le contenu de l'image.

Un seul bouton **Démarrer** lance le traitement sur toute la bibliothèque, avec barre de progression. À la fin, un résumé (« N fichier(s) mis à jour · N ignoré(s) ou en erreur ») est affiché, avec un bouton **Ouvrir le rapport CSV** détaillant l'action prise pour chaque fichier.

---

## 16. Autres outils

### Journal des threads (Outils › Journal des threads…)

Outil de diagnostic destiné à surveiller les traitements en arrière-plan (scan, indexation de visages, clustering, vignettes…) — utile si l'application semble lente ou bloquée.

- **Bilan d'exécution** : statut global et par thread (✓ OK, ● LENT, ● TROP LONG, ✗ ERREUR, ● EN COURS).
- **Résumé par thread** : nombre d'exécutions, durée moyenne/max, erreurs.
- **Événements bruts** : journal détaillé filtrable (par thread, par mot-clé), avec bouton **▶ Temps réel** pour un rafraîchissement automatique.
- Boutons **Rapport de problèmes…** (diagnostic textuel copiable) et **Exporter CSV…**.
- **🗑 Vider** efface le journal (confirmation demandée).

### Historique des problèmes (Outils › Historique des problèmes…)

Conserve la trace de chaque analyse de doublons ayant rencontré des fichiers corrompus (voir [section 14](#14-détection-des-doublons)).

- Une ligne par analyse : date, nombre de fichiers corrompus détectés, nombre réparés.
- Bouton **Ouvrir la liste…** par ligne : ouvre le fichier texte listant les fichiers qui n'ont pas pu être réparés lors de cette analyse (désactivé si tous ont été réparés, ou si le fichier a depuis été supprimé).

### Applications externes (Outils › Applications externes…)

Permet d'ajouter des raccourcis vers des logiciels tiers (retoucheur externe, visionneuse RAW, etc.), accessibles ensuite sous forme d'icônes dans la barre d'outils de la visionneuse.

- **Ajouter…** : choisissez un exécutable (`*.exe`) puis un nom d'affichage (utilisé comme info-bulle).
- **Supprimer** : retire l'application sélectionnée de la liste.

Un clic sur l'icône correspondante dans la visionneuse ouvre l'application avec la photo courante en argument.

### Paramètres (Outils › Paramètres)

Dialogue à deux catégories :

- **Reconnaissance de visages** : curseur **« Tolérance de similarité »** (25 % à 70 %) — contrôle à quel point deux visages doivent se ressembler pour être placés dans le même groupe. Un indicateur textuel accompagne le curseur (groupes très stricts → très larges). Modifier ce réglage relance automatiquement le regroupement des visages à la fermeture du dialogue.
- **Lecteur vidéo** : choix entre **« Lecteur par défaut du système »** (application Windows associée aux fichiers vidéo) ou **« Lecteur personnalisé »** (chemin vers un exécutable, ex. VLC ou MPC-HC, via **Parcourir…**). Ce choix détermine ce qu'ouvre le bouton **▶ Ouvrir la vidéo** de la visionneuse.

---

## 17. Raccourcis clavier

### Général

| Raccourci | Action |
|---|---|
| **Ctrl + Q** | Quitter l'application |
| **F1** | Ouvrir l'aide |
| **F9** | Afficher/masquer la sidebar |
| **F11** | Plein écran |
| **F5** | Lancer le diaporama |

### Grille

| Raccourci | Action |
|---|---|
| **Ctrl + A** | Sélectionner toutes les photos |
| **Suppr** | Supprimer les photos sélectionnées (avec confirmation) |

### Mode chronologie (ruban)

| Raccourci | Action |
|---|---|
| **← / →** | Déplacer d'une photo |
| **↑ / ↓** | Déplacer de 3 photos |
| **Molette** | Défilement avec inertie |
| **Suppr** | Supprimer la photo centrale (ou la sélection) |

### Visionneuse

| Raccourci | Action |
|---|---|
| **← / ↑** | Photo précédente |
| **→ / ↓** | Photo suivante |
| **I** | Afficher/masquer le panneau EXIF |
| **0** | Ajuster à la fenêtre |
| **1** | Zoom 100 % |
| **Ctrl + Molette** | Zoom avant / arrière |
| **Échap** | Retour à la grille |
| **F** | Marquer/retirer des favoris |

### Diaporama

| Raccourci | Action |
|---|---|
| **← / ↑** | Photo plus ancienne |
| **→ / ↓** | Photo plus récente |
| **Espace** | Pause / Reprendre |
| **Échap** | Quitter le diaporama |

### Mode recadrage

| Raccourci | Action |
|---|---|
| **Entrée** | Confirmer le recadrage |
| **Échap** | Annuler le recadrage |
| **Molette** | Zoomer dans la visionneuse |

### Mode annotation

| Raccourci | Action |
|---|---|
| **Suppr** | Supprimer l'élément d'annotation sélectionné |
| **Entrée** | Valider une courbe en cours de tracé |
| **Échap** | Annuler le tracé en cours (le mode annotation reste actif) |

### Panneau de retouche

| Raccourci | Action |
|---|---|
| **Ctrl + Z** | Annuler la dernière retouche |
| **Ctrl + Y** | Rétablir |

---

## 18. Où sont stockées vos données

Toutes les données de l'application se trouvent dans :

```
%LOCALAPPDATA%\PixelPhotoManager\
```

Soit typiquement : `C:\Users\VotreNom\AppData\Local\PixelPhotoManager\`

| Fichier / dossier | Contenu |
|---|---|
| `catalog.db` | Index de toutes vos photos et vidéos (chemins, EXIF, métadonnées, albums) |
| `thumbnails.db` | Cache des vignettes générées (images et premières frames vidéo) |
| `edits.db` | Toutes vos retouches et leur historique |
| `faces.db` | Visages détectés, groupes/clusters, personnes identifiées |
| `config.json` | Dossiers surveillés et préférences de l'interface (dont réglages de Paramètres) |
| `logs\pixelphotomanager.log` | Journal de l'application (pour le dépannage) |
| `duplicates_report.html` | Dernier rapport de détection de doublons |
| `problems_history.jsonl` | Historique des fichiers corrompus détectés/réparés (**Outils › Historique des problèmes…**, voir [section 16](#16-autres-outils)) |
| `fichiers_corrompus_AAAAMMJJ_HHMMSS.txt` | Liste des fichiers corrompus non réparés lors d'une analyse de doublons |
| Rapport CSV de synchro EXIF | Généré à chaque exécution de l'outil de synchronisation des dates |
| Sauvegardes de reconnaissance faciale | Archives créées via **Visages › Sauvegarder la reconnaissance…** |

> **Vos photos et vidéos originales ne sont jamais modifiées** par les retouches ou la reconnaissance faciale. Vous pouvez supprimer `edits.db` pour effacer toutes les retouches et repartir de zéro, ou supprimer `catalog.db` pour forcer une réindexation complète.

Dans chaque dossier de photos, un sous-dossier caché **`.tmp_originals`** peut apparaître : il contient les copies de sauvegarde des fichiers écrasés via **Enregistrer l'image traitée sur le disque** (uniquement si vous avez coché l'option de sauvegarde), ou celles des fichiers corrompus avant tentative de réparation (voir [section 14](#14-détection-des-doublons)).

### Formats supportés

**Images :**
`.jpg` · `.jpeg` · `.png` · `.tiff` · `.tif` · `.webp` · `.bmp` · `.gif` · `.heic` · `.raw` · `.cr2` · `.nef` · `.arw` · `.dng`

**Vidéos :**
`.mp4` · `.mov` · `.avi` · `.mkv` · `.wmv` · `.webm` · `.m4v` · `.3gp` · `.flv` · `.ts` · `.mts` · `.mpg` · `.mpeg`

*(La disponibilité des formats RAW dépend des pilotes installés sur le système.)*

---

## Annexe — Résolution de problèmes courants

| Problème | Solution |
|---|---|
| Des photos n'apparaissent pas après les avoir copiées | Clic droit sur le dossier dans la sidebar › **Scanner maintenant**, ou utilisez **Outils › Dossiers…** › **⟳ Re-scanner** |
| La vignette d'une photo retouchée n'est pas à jour | Ouvrez la photo, les retouches sont appliquées automatiquement |
| L'application est lente au démarrage | Normal lors du premier scan d'une grande bibliothèque ; le scan suivant sera beaucoup plus rapide (seules les nouvelles photos sont analysées) |
| Un dossier déplacé manuellement (hors de l'application) n'est plus trouvé | Utilisez **Supprimer des dossiers surveillés** puis **Fichier › Ajouter un dossier…** pour le réenregistrer |
| Récupérer l'original d'une photo retouchée | Le fichier original n'a jamais été modifié — il suffit d'effacer les retouches via **Annuler** (↩) jusqu'à l'état initial |
| Récupérer un original écrasé par erreur | S'il a été sauvegardé (case cochée lors de l'enregistrement), il se trouve dans le sous-dossier caché `.tmp_originals` du dossier concerné |
| « Détecter les doublons… » signale des fichiers corrompus | Utilisez le bouton **Réparer…** proposé dans le bilan (voir [section 14](#14-détection-des-doublons)) ; l'original est toujours sauvegardé avant réparation dans `.tmp_originals` |
| La vignette d'une vidéo est noire | OpenCV n'a pas pu lire la vidéo — vérifiez que le codec est installé sur votre système |
| Une opération semble bloquée ou anormalement lente | Ouvrez **Outils › Journal des threads…** pour voir quel traitement de fond est en cours et son statut |
| « Détecter les doublons… » signale une erreur au démarrage | Les modules `imagehash` et `Pillow` sont requis ; sans OpenCV/numpy, seule la détection de recadrage (Tier 2) est indisponible, le reste continue de fonctionner |
| La reconnaissance faciale ne détecte plus rien | Vérifiez via **Visages › Visualisation des erreurs…** si les fichiers concernés sont en échec de détection (timeout/crash), et utilisez **⟳ Réessayer** |
