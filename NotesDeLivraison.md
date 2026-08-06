# Notes de livraison — PixelPhotoManager

Historique cumulatif depuis la création du projet, version la plus récente en tête.

| Version | Date | Commits | Installateur |
|---------|------|---------|--------------|
| 1.1.0 | 6 août 2026 | 109 | `PixelPhotoManager-1.1.0-x64.msi` |
| 1.0.0 | 6 juillet 2026 | 113 (création → 1.0.0) | `PixelPhotoManager-1.0.0-x64.msi` |

Les numéros 1.0.1 et 1.0.2 n'ont pas été livrés (bumps internes) ; leur contenu est
inclus dans la 1.1.0.

---

## Version 1.1.0 — 6 août 2026

Version précédente : **1.0.0** (6 juillet 2026). 109 commits.

### Nouveautés

**Organisation et recherche**
- Notation par étoiles ★ 1 à 5, avec album « Par notes » pliable par niveau.
- Mots-clés éditables sur les photos : dialogue, filtre, liste déroulante dans la
  visionneuse, sous-menu et section repliable dans le panneau latéral.
- Dialogue de recherche avancée multi-critères.
- Barre de recherche dans la fenêtre Aide / À propos.
- Vérification automatique des mises à jour via les publications GitHub.

**Formats**
- Prise en charge des photos **RAW** (CR2, NEF, ARW, DNG, ORF, RW2) et **HEIC/HEIF**
  (iPhone).
- Copies de DVD : dossiers `VIDEO_TS` détectés, fichiers `.VOB` catalogués comme
  vidéos, ouverture par un lecteur externe.
- Les icônes d'applications externes de la visionneuse sont filtrées selon le type de
  média (une application vidéo n'apparaît plus sur une photo fixe).

**Retouche**
- **Cadres décoratifs** : 13 motifs calculés (entourage uni, simple, double, baroque
  doré, oves et perles, grecque, art déco, sculpture bois, feuilles de vigne, roses,
  fleurs, métallique, reflets), galerie d'aperçus sur la photo en cours, largeur
  réglable. L'entourage uni accepte un second cadre avec ferronnerie (volutes, rinceaux,
  barreau torsadé, clous). Les motifs végétaux débordent par endroits sur la photo,
  ombre portée comprise.
- Calque d'annotations et harmonisation des outils d'édition.
- Raccourci `Ctrl+S` pour enregistrer l'image traitée.
- « Réinitialiser » est réversible par « Restaurer », pile d'annulation comprise.

**Doublons**
- Détection **continue et incrémentale**, lancée après chaque scan, sans bouton ni
  rapport de fin ; menu **Outils › État des doublons…** pour un instantané.
- Grille dédiée « Dupliquées », popup déplaçable dans la visionneuse, état vide
  explicite.
- Gestion complète des fichiers corrompus : détection, réparation, suppression,
  historique persistant.
- Comparaisons Tier 1 et Tier 2 parallélisées.

**Visages**
- Boutons ✓ / ✗ superposés aux vignettes pour accepter ou rejeter une suggestion.
- Entrée **Visages › Rechercher des visages similaires…**, relancée automatiquement
  après chaque identification.
- Bouton Annuler et popup déplaçable pendant l'analyse des groupes ; le regroupement
  partiel est conservé.
- Paliers de confiance revus : attribution automatique ≥ 70 %, mise en attente de
  vérification ≥ 55 %, libellés « Probablement » / « Peut-être » en dessous.
- Nombre de personnes identifiées affiché dans le panneau latéral.
- `Ctrl+A` sélectionne tous les visages confirmés d'une personne.

**Diaporama et visionneuse**
- Plus d'économiseur d'écran ni d'extinction de l'écran pendant la lecture, pause
  comprise.
- Compteur « photo n sur N » dans la barre de navigation.
- Le retour vers la grille remet la dernière photo affichée en surbrillance et à
  l'écran.
- Dossier de la photo affiché dans la grille.

**Suppression et sécurité des données**
- Toute suppression de fichier passe désormais par la **corbeille Windows** ; en cas
  d'échec, l'utilisateur est prévenu, jamais d'effacement définitif silencieux.

**Performances**
- Réglage du bridage CPU des traitements de fond : **Paramètres › Performances**, trois
  niveaux, défaut « Économe », relâché quand la fenêtre n'est pas au premier plan.
- Priorité système IDLE pour les threads et processus de fond.
- Retour visuel immédiat sur la grille, la visionneuse et l'assignation de visage.
- Recherche de visages similaires par produit matriciel (11 M de comparaisons couple par
  couple auparavant, plusieurs minutes).
- Panneau Visages : seules les vignettes dont le cadrage a changé sont redécodées.
- Rendu des vignettes de cadres 2,5× plus rapide.

### Corrections

- Boutons radio invisibles en thème sombre (pastille de la même teinte que le fond).
- Libellé de menu long passant sous son raccourci.
- Vignettes ne reflétant pas les retouches d'une photo hors du champ visible.
- Rotation perdue quand une re-détection de visages était déjà en cours.
- Suggestions de visages définitivement bloquées sur un groupe partiellement identifié.
- Détection de doublons repartant de zéro à chaque redémarrage ; groupes réduits à un
  seul exemplaire non dissous ; fichier supprimé pendant le scan classé « corrompu ».
- Navigation manuelle relançant le défilement d'un diaporama en pause.
- Panneau de retouche compressible au point de rendre sa deuxième colonne inatteignable.
- Plantage du panneau latéral sur un dossier à plusieurs centaines de sous-dossiers.
- Fenêtres fantômes après avoir ignoré plusieurs groupes de doublons.
- Entrées `album_photos` orphelines purgées ; compteur photos / vidéos par dossier.
- La grille ne propose plus « Effacer le fichier » en vue album.
- Icônes de notation et de favori de la visionneuse invisibles ou incohérentes.

### Installateur et packaging

- MSI renommé **`PixelPhotoManager-X.Y.Z-x64.msi`** (auparavant
  `PixelPhotoManager-Setup-<version>.msi`).
- Script compagnon `Installer-avec-log.cmd` (journal `msiexec /L*v`) généré à côté du
  MSI.

### Migrations automatiques au premier démarrage

`edits.db` : 13 colonnes de cadre. `thumbnails.db` : empreinte des retouches
(`edit_sig`) pour régénérer les vignettes périmées. `faces.db` : purge des suggestions
résiduelles. Ces migrations ne sont pas réversibles vers la 1.0.0 — sauvegarder
`%LOCALAPPDATA%\PixelPhotoManager\` avant la mise à jour pour garder un retour arrière
possible.

### Qualité

1646 tests unitaires et d'interface, 14 scénarios de bout en bout (pywinauto),
couverture combinée 80,4 % (seuil de blocage relevé de 79 à 80 %). Modularisation des
gros modules (`main_window`, `photo_viewer`, `edit_panel`) en modules dédiés.

---

## Version 1.0.0 — 6 juillet 2026

Première version livrée. 113 commits depuis la création du projet (3 juin 2026).

### Socle

- Application desktop Windows en Python 3.11 / PySide6, bus d'événements central,
  système de plugins (traitement image et vues).
- Catalogue SQLite, cache de vignettes à trois niveaux (RAM, SQLite, génération en
  arrière-plan), données dans `%LOCALAPPDATA%\PixelPhotoManager\`.
- Scan de dossiers surveillés, nettoyage automatique des entrées périmées, gestionnaire
  de dossiers (**Outils › Dossiers…**).
- Licence MIT, README, guides utilisateur et développeur, aide intégrée.

### Bibliothèque et navigation

- Grille virtualisée : démarrage fluide avec 67 000 photos et plus de 1 000 dossiers.
- Panneau latéral : arborescence des dossiers, albums, favoris, chronologie.
- Albums : création depuis une sélection, suppression, retrait de photos.
- Visionneuse : panneau EXIF détaillé, GPS et carte, badge de doublon, menus
  contextuels alignés entre grille et visionneuse.
- Diaporama avec effet Ken Burns.
- Vidéo : 13 extensions prises en charge, vignettes extraites par OpenCV, album
  « Vidéos », lecteur externe configurable.

### Retouche et export

- Retouches **non destructives** stockées en SQLite, appliquées à l'affichage et à
  l'export, historique d'annulation persistant entre sessions.
- Recadrage, redressement avec grille d'alignement, miroirs, rotation, correction des
  yeux rouges.
- Enregistrement de l'image traitée, renommage depuis la grille, export avec préréglages
  de taille et de qualité, ouverture du dossier d'export à la fin.

### Reconnaissance faciale

- Détection et embedding par **InsightFace / buffalo_l** (après une première
  implémentation DeepFace), clustering HDBSCAN.
- Panneau Visages : identification, rotation, navigation, pile d'annulation, visages
  ignorés, filtre du panneau latéral, détection multi-rotation.
- Vue par personne, cartes de groupes, multi-sélection, association de plusieurs groupes
  en un clic droit, fusion et renommage de personnes.
- Import des annotations **Picasa** et coexistence avec les identifications ArcFace.
- Seuil de taille adaptatif, proportionnel à la résolution de la photo.

### Packaging

- Exécutable autonome PyInstaller, icône, écran de démarrage, barre unifiée menu +
  barre d'outils.
- Installateur MSI WiX (WixUI_InstallDir, bannière personnalisée), infrastructure de
  release.
- Correctifs d'empaquetage : `sklearn` / `hdbscan` absents de l'exécutable, pack de
  modèles `buffalo_l` embarqué (détection faciale inopérante sans accès Internet),
  `multiprocessing.freeze_support()`.

### Stabilité et performances

- Correction de saturations mémoire et de fuites (threads zombies, cache d'avatars,
  allocations d'embeddings), de blocages de l'interface (fusion de personnes, calcul des
  groupes, aperçus de retouche) et de gels sur les albums vidéo.
- Détection faciale sur les chemins non-ASCII, bridage CPU du scan et de l'indexation.
- Mise en place de la suite de tests (unitaires, interface, bout en bout) et de la
  mesure de couverture.
