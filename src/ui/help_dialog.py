# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QTextBrowser, QDialogButtonBox,
)

from src.core.app_version import get_app_version
from src.core.update_checker import (
    UpdateCheckThread, STATUS_UPDATE_AVAILABLE, STATUS_UP_TO_DATE, STATUS_VERSION_UNKNOWN,
)

_STYLE = """
<style>
  body  { font-family: Segoe UI, Arial, sans-serif; font-size: 13px;
          color: #ddd; background: transparent; margin: 0; }
  h2    { color: #fff; font-size: 15px; margin: 14px 0 6px 0;
          border-bottom: 1px solid #555; padding-bottom: 4px; }
  h3    { color: #ccc; font-size: 13px; margin: 10px 0 4px 0; }
  p, li { margin: 3px 0; line-height: 1.5; color: #ccc; }
  ul    { margin: 4px 0 4px 16px; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 6px 0; }
  th    { text-align: left; color: #aaa; font-weight: normal;
          padding: 3px 8px; border-bottom: 1px solid #444; }
  td    { padding: 3px 8px; color: #ccc; vertical-align: top; }
  tr:nth-child(even) td { background: rgba(255,255,255,0.04); }
  kbd   { background: #3a3a3a; border: 1px solid #666; border-radius: 3px;
          padding: 1px 5px; font-size: 12px; color: #eee; white-space: nowrap; }
  .tip  { background: rgba(100,160,255,0.08); border-left: 3px solid #4a8fd4;
          padding: 5px 10px; margin: 8px 0; border-radius: 2px; }
</style>
"""

_TAB_OVERVIEW = _STYLE + """
<h2>PixelPhotoManager</h2>
<p>Gestionnaire de photos et vidéos non destructif pour Windows.
Toutes les retouches sont stockées séparément — l'original n'est jamais modifié.</p>

<h2>Organisation de l'interface</h2>
<ul>
  <li><b>Barre latérale gauche</b> — dossiers surveillés, albums, personnes identifiées, et en haut
      une case de filtrage instantané par nom de fichier, marque/modèle d'appareil, dossier ou
      personne (<kbd>F9</kbd> pour afficher/masquer la barre latérale).</li>
  <li><b>Grille centrale</b> — vignettes des photos et vidéos du contexte sélectionné.</li>
  <li><b>Visionneuse</b> — ouverte par double-clic sur une vignette ; panneau de retouches à gauche.</li>
  <li><b>Barre du bas</b> — nombre de fichiers, taille, curseur de zoom de la grille.</li>
</ul>

<h2>Modes d'affichage de la grille</h2>
<ul>
  <li><b>Mode grille</b> — vignettes uniformes, taille réglable avec le curseur en bas à droite.</li>
  <li><b>Mode chronologie</b> — ruban temporal : 5 rangées de tailles croissantes vers le centre.
    La photo centrale est la photo « courante ». Naviguez avec la molette, les flèches
    ou l'ascenseur à droite. Le diaporama démarrera depuis cette photo centrale.</li>
</ul>

<h2>Formats supportés</h2>
<p><b>Images :</b> JPEG, PNG, TIFF, WebP, BMP, GIF, HEIC, RAW (CR2, NEF, ARW, DNG)…<br>
<b>Vidéos :</b> MP4, MOV, AVI, MKV, WMV, WebM, M4V, 3GP, FLV, TS, MTS, MPG, MPEG.</p>

<h2>Où sont stockées vos données</h2>
<p>Toutes les données se trouvent dans <b>%LOCALAPPDATA%\PixelPhotoManager\</b></p>
<table>
  <tr><th>Fichier / Dossier</th><th>Contenu</th></tr>
  <tr><td><code>catalog.db</code></td><td>Index photos/vidéos (chemins, EXIF, métadonnées)</td></tr>
  <tr><td><code>thumbnails.db</code></td><td>Cache des vignettes générées</td></tr>
  <tr><td><code>edits.db</code></td><td>Toutes les retouches et leur historique</td></tr>
  <tr><td><code>faces.db</code></td><td>Visages détectés, embeddings, clusters et personnes identifiées</td></tr>
  <tr><td><code>config.json</code></td><td>Dossiers surveillés et préférences</td></tr>
  <tr><td><code>faces_backups\</code></td><td>Sauvegardes horodatées de la reconnaissance faciale (<code>visages_AAAAMMJJ_HHMMSS.zip</code>)</td></tr>
  <tr><td><code>problems_history.jsonl</code></td><td>Historique des fichiers corrompus détectés/réparés lors des analyses de doublons</td></tr>
</table>
<p class="tip">Vos fichiers originaux ne sont <b>jamais modifiés</b>. Supprimer <code>edits.db</code>
efface toutes les retouches ; supprimer <code>catalog.db</code> force une réindexation complète.</p>
"""

_TAB_NAVIGATION = _STYLE + """
<h2>Parcourir la bibliothèque</h2>

<h3>Barre latérale — Dossiers</h3>
<ul>
  <li>Cliquez sur un dossier pour afficher son contenu dans la grille.</li>
  <li>Cliquez sur la flèche <b>▶</b> pour développer les sous-dossiers.</li>
  <li><b>Outils › Dossiers…</b> — gestion avancée : statut, nombre de fichiers, re-scan forcé.</li>
</ul>
<p><b>Clic droit</b> sur un dossier :</p>
<table>
  <tr><th>Action</th><th>Effet</th></tr>
  <tr><td><b>Scanner maintenant</b></td><td>Force un re-scan pour détecter les nouveaux fichiers</td></tr>
  <tr><td><b>Supprimer des dossiers surveillés</b></td><td>Retire le dossier de la bibliothèque (les fichiers restent sur le disque)</td></tr>
  <tr><td><b>Créer un sous-dossier…</b></td><td>Crée un sous-dossier sur le disque</td></tr>
  <tr><td><b>Renommer…</b></td><td>Renomme le dossier sur le disque et met à jour le catalogue</td></tr>
  <tr><td><b>Déplacer vers…</b></td><td>Déplace le dossier entier vers un autre emplacement</td></tr>
  <tr><td><b>Ouvrir dans l'Explorateur</b></td><td>Révèle le dossier dans l'Explorateur Windows</td></tr>
  <tr><td><b>Effacer le dossier…</b></td><td>Supprime le dossier et tout son contenu du disque (<b>irréversible</b>)</td></tr>
</table>

<h3>Barre latérale — Albums</h3>
<p>Albums spéciaux (intégrés, non modifiables) :</p>
<table>
  <tr><th>Album</th><th>Contenu</th></tr>
  <tr><td><b>★ Chronologie</b></td><td>Toutes les photos en mode ruban chronologique</td></tr>
  <tr><td><b>♡ Favoris</b></td><td>Photos marquées d'une étoile dans la visionneuse (<kbd>F</kbd>)</td></tr>
  <tr><td><b>▶ Vidéos</b></td><td>Toutes les vidéos de la bibliothèque</td></tr>
  <tr><td><b>🔍 Par nom de fichier</b></td><td>Photos dont le nom contient le texte saisi dans la zone de filtre de la sidebar</td></tr>
</table>
<ul>
  <li>Albums personnels — créés via le bouton <b>+</b> dans l'en-tête Albums.</li>
  <li>Cliquez sur un album pour en afficher le contenu dans la grille.</li>
  <li>Dans un album personnel, <kbd>Suppr</kbd> ou clic droit › <b>Retirer de l'album</b>
      retire la photo de l'album <b>sans toucher</b> au fichier ni au catalogue.</li>
</ul>

<h3>Barre latérale — Filtrage</h3>
<ul>
  <li>La zone de saisie en haut de la sidebar filtre simultanément les <b>dossiers</b>
      et les <b>personnes</b> en temps réel.</li>
  <li>L'album <b>🔍 Par nom de fichier</b> utilise ce même texte pour afficher dans la grille
      toutes les photos dont le nom de fichier correspond — saisissez un mot dans la zone,
      puis cliquez sur cet album.</li>
</ul>

<h3>Barre latérale — Personnes</h3>
<ul>
  <li>Affiche les personnes identifiées par l'analyse des visages.</li>
  <li>Un badge orange entre la vignette et le nom indique le nombre de suggestions en attente.</li>
  <li>Cliquez sur une personne pour voir ses visages confirmés et ses suggestions.</li>
</ul>
<p><b>Clic droit</b> sur une personne :</p>
<table>
  <tr><th>Action</th><th>Effet</th></tr>
  <tr><td><b>Renommer…</b></td><td>Modifie le nom dans toute la bibliothèque</td></tr>
  <tr><td><b>Fusionner avec…</b></td><td>Fusionne les visages de cette personne avec ceux d'une autre</td></tr>
  <tr><td><b>Effacer le nom…</b></td><td>Retire le nom — les visages redeviennent des groupes anonymes</td></tr>
</table>

<h3>Recherche</h3>
<ul>
  <li>Pas de barre de recherche dédiée : tout passe par la case de filtrage en haut de la
      barre latérale (voir « Barre latérale — Filtrage » ci-dessus).</li>
  <li>Le texte saisi filtre instantanément les dossiers et les personnes affichés dans la
      sidebar, et alimente aussi l'album <b>🔍 Par nom de fichier</b> (recherche sur le nom
      de fichier, la marque et le modèle d'appareil photo).</li>
  <li>Filtrage en temps réel dès la frappe. Utilisez le bouton <b>✕</b> de la case pour
      revenir à l'affichage complet.</li>
</ul>

<h3>Mode chronologie (ruban)</h3>
<ul>
  <li>5 rangées de tailles lenticulaires — la rangée centrale est la plus grande.</li>
  <li>Naviguez avec la <b>molette</b> (inertie), les <b>flèches clavier</b> ou l'<b>ascenseur</b> à droite.</li>
  <li>La photo au centre du ruban est celle qui sera utilisée comme point de départ du diaporama.</li>
</ul>

<h3>Navigation dans la visionneuse</h3>
<ul>
  <li><kbd>←</kbd> <kbd>↑</kbd> ou molette : photo précédente.</li>
  <li><kbd>→</kbd> <kbd>↓</kbd> ou molette : photo suivante.</li>
  <li>Double-clic : zoom 1:1 ↔ ajustement automatique.</li>
  <li><kbd>Ctrl</kbd>+Molette : zoom avant / arrière.</li>
  <li><kbd>Échap</kbd> ou <b>✕</b> : retour à la grille.</li>
  <li><kbd>F11</kbd> : plein écran.</li>
  <li><b>Clic droit</b> dans la visionneuse › <b>Afficher le dossier dans la grille</b> :
      retourne à la grille en affichant le dossier de la photo courante et en la sélectionnant.</li>
</ul>

<h3>Menu contextuel de la grille (clic droit sur une photo)</h3>
<table>
  <tr><th>Action</th><th>Effet</th></tr>
  <tr><td><b>Ouvrir</b></td><td>Ouvre la photo dans la visionneuse</td></tr>
  <tr><td><b>Marquer / Retirer des favoris</b></td><td>Ajoute ou retire la photo de l'album ♡ Favoris</td></tr>
  <tr><td><b>Renommer l'image</b></td><td>Renomme le fichier sur le disque (catalogue mis à jour automatiquement)</td></tr>
  <tr><td><b>Déplacer vers…</b></td><td>Déplace le fichier dans un autre dossier surveillé</td></tr>
  <tr><td><b>Enregistrer l'image traitée sur le disque</b></td><td>Exporte une copie avec toutes les retouches appliquées</td></tr>
  <tr><td><b>Révéler dans l'Explorateur</b></td><td>Ouvre le dossier contenant la photo dans l'Explorateur</td></tr>
  <tr><td><b>Retirer de l'album</b></td><td><i>(vue album uniquement)</i> Retire la ou les photos de l'album affiché — le fichier et la photo restent intacts</td></tr>
  <tr><td><b>Effacer le(s) fichier(s)…</b></td><td>Supprime le ou les fichiers du disque (<b>irréversible</b>, confirmation demandée)</td></tr>
</table>
<p class="tip"><b>Astuce :</b> Pour déplacer une ou plusieurs photos, vous pouvez aussi les
glisser-déposer directement vers un dossier dans la barre latérale.</p>

<h3>Supprimer des fichiers</h3>
<ul>
  <li>Dans la grille : sélectionnez puis appuyez sur <kbd>Suppr</kbd>.</li>
  <li>En mode chronologie sans sélection : <kbd>Suppr</kbd> efface la photo centrale.</li>
  <li>Dans la visionneuse : bouton 🗑 dans la barre d'outils.</li>
  <li>La suppression est <b>définitive</b> (pas de corbeille). Une confirmation est demandée.</li>
  <li>La suppression s'exécute en arrière-plan : l'interface reste utilisable et la barre
      de statut affiche la progression (« Suppression… n/total ») sur les grosses sélections.</li>
  <li><b>Dans un album</b> : <kbd>Suppr</kbd> (grille ou visionneuse) <b>retire</b> la photo
      de l'album au lieu de supprimer le fichier — le fichier et la photo restent intacts
      dans leur dossier. Pour supprimer réellement le fichier, passez par la vue de son dossier.</li>
</ul>

<h3>Déplacer des photos</h3>
<ul>
  <li>Sélectionnez une ou plusieurs photos dans la grille, puis glissez-les vers un dossier de la sidebar.</li>
  <li>Le fichier est déplacé sur le disque et le catalogue est mis à jour automatiquement.</li>
</ul>
"""

_TAB_SLIDESHOW = _STYLE + """
<h2>Diaporama</h2>
<p>Lancez le diaporama depuis <b>Affichage › Diaporama</b> ou avec <kbd>F5</kbd>.
Le diaporama s'ouvre en <b>plein écran</b> et parcourt les photos du contexte actuel.</p>

<h3>Point de départ</h3>
<table>
  <tr><th>Situation au lancement</th><th>Photo de départ</th></tr>
  <tr><td>Visionneuse ouverte</td><td>La photo actuellement affichée</td></tr>
  <tr><td>Mode chronologie (ruban)</td><td>La photo au <b>centre</b> du ruban</td></tr>
  <tr><td>Autre vue</td><td>La plus ancienne photo du dossier</td></tr>
</table>

<h3>Effet Ken Burns</h3>
<p>Chaque photo est animée d'un <b>léger zoom et d'un panoramique lent</b> :</p>
<ul>
  <li>Le zoom varie de 0 à 8 % sur toute la durée d'affichage.</li>
  <li>La direction est aléatoire, avec une préférence pour les mouvements <b>horizontaux
      et diagonaux</b> (les mouvements purement verticaux sont réduits).</li>
  <li>Les photos dont le rapport hauteur/largeur diffère de l'écran sont affichées avec des
      <b>marges noires</b> (letterbox / pillarbox) — elles ne sont jamais rognées.</li>
  <li>Pendant le chargement d'une nouvelle photo, l'animation de la photo précédente
      continue jusqu'à ce que la suivante soit prête.</li>
</ul>

<h3>Barre de contrôle</h3>
<p>La barre apparaît au mouvement de la souris et se masque après 5 secondes d'inactivité.</p>
<table>
  <tr><th>Contrôle</th><th>Effet</th></tr>
  <tr><td><b>◀ Précédente</b></td><td>Aller à la photo plus ancienne</td></tr>
  <tr><td><b>Suivante ▶</b></td><td>Aller à la photo plus récente</td></tr>
  <tr><td><b>−</b> / compteur / <b>+</b></td>
      <td>Réduire / afficher / augmenter l'intervalle d'affichage (1 s à 60 s, pas de 1 s)</td></tr>
  <tr><td><b>⏸ / ▶</b></td><td>Mettre en pause / reprendre le défilement automatique</td></tr>
  <tr><td><b>✕</b></td><td>Quitter le diaporama</td></tr>
</table>

<h3>Raccourcis clavier</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>←</kbd> ou <kbd>↑</kbd></td><td>Photo plus ancienne</td></tr>
  <tr><td><kbd>→</kbd> ou <kbd>↓</kbd></td><td>Photo plus récente</td></tr>
  <tr><td><kbd>Espace</kbd></td><td>Pause / Reprendre</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Quitter le diaporama</td></tr>
</table>

<p class="tip"><b>Astuce :</b> En mode chronologie, faites défiler le ruban jusqu'à la période
qui vous intéresse avant de lancer le diaporama — il démarrera depuis la photo au centre.</p>
"""

_TAB_EDITING = _STYLE + """
<h2>Retouches non destructives</h2>
<p>Les ajustements n'écrivent jamais dans le fichier d'origine. Ils sont stockés dans
une base SQLite séparée et appliqués à la volée à l'affichage et à l'export.</p>

<h3>Corrections tonales</h3>
<table>
  <tr><th>Correction</th><th>Plage</th><th>Description</th></tr>
  <tr><td><b>Luminosité</b></td><td>−1,00 à +1,00</td><td>Éclaircit ou assombrit l'image globalement</td></tr>
  <tr><td><b>Contraste</b></td><td>−1,00 à +1,00</td><td>Élargit ou réduit la plage tonale</td></tr>
  <tr><td><b>Saturation</b></td><td>−1,00 à +1,00</td><td>Renforce ou atténue les couleurs (−1 = N&amp;B)</td></tr>
  <tr><td><b>Gamma</b></td><td>0,10 à 3,00</td><td>Courbe de luminosité (1,0 = neutre)</td></tr>
</table>

<h3>Couleurs (Noir &amp; Blanc avec mixage de canaux)</h3>
<ul>
  <li>Cochez <b>Noir &amp; Blanc</b> et dosez les contributions Rouge/Vert/Bleu (−1 à +1).</li>
  <li>Exemple : Rouge +1, Bleu −1 → ciel foncé et peaux claires (filtre rouge argentique).</li>
</ul>

<h3>Vignette</h3>
<ul>
  <li>Ajoute un assombrissement (ou éclaircissement) progressif sur les bords de l'image.</li>
  <li><b>Intensité</b> — de 0,00 (aucun effet) à 1,00 (effet maximum).</li>
  <li><b>Couleur</b> — Noir (fondu sombre, style argentique) ou Blanc (fondu clair, style rétro).</li>
  <li>La forme et la position de la vignette sont réglables directement sur la photo :</li>
</ul>
<table>
  <tr><th>Poignée</th><th>Effet</th></tr>
  <tr><td>Cercle intérieur (pointillés)</td><td>Début du fondu</td></tr>
  <tr><td>Cercle extérieur</td><td>Fin du fondu</td></tr>
  <tr><td>Poignée ronde au sommet</td><td>Rotation de l'ellipse</td></tr>
  <tr><td>Croix centrale</td><td>Déplacer le centre de la vignette</td></tr>
</table>

<h3>Géométrie</h3>
<ul>
  <li><b>↺ / ↻</b> — rotation de 90° anti-horaire / horaire.</li>
  <li><b>Redresser</b> — corrige l'inclinaison de l'horizon (−10° à +10°), avec grille de référence.</li>
  <li><b>Recadrer</b> — sélection libre ou aux formats 10×15 / 13×18 (portrait ou paysage).
      La zone de recadrage est mémorisée entre les sessions.</li>
  <li><b>Miroir H / V</b> — symétrie horizontale ou verticale.</li>
</ul>

<h3>Yeux rouges</h3>
<ul>
  <li>Cliquez sur <b>Yeux rouges</b>, puis cliquez sur chaque œil affecté directement sur la photo.</li>
  <li><b>Taille</b> — rayon de la correction (0,5&nbsp;% à 8&nbsp;% de la plus petite dimension de l'image).</li>
  <li><b>Effacer tout</b> — supprime toutes les corrections posées. <b>Terminé</b>
      (ou <kbd>Échap</kbd>) — quitte le mode sans rien perdre.</li>
</ul>

<h3>Annotations</h3>
<p>Calque de dessin et de texte non destructif, superposé à la photo — indépendant des
corrections tonales et de la géométrie.</p>
<ul>
  <li><b>Outils</b> — stylo (trait libre), ligne, courbe (cliquez les points de passage,
      double-clic pour valider), rectangle, ellipse, texte, et sélection (pour modifier
      ou déplacer un élément déjà posé).</li>
  <li><b>Style</b> — couleur du trait/texte, épaisseur, couleur et opacité de remplissage
      des formes, flou de la photo sous la forme, police/taille/gras/italique pour le texte.</li>
  <li>Double-clic sur un texte existant (outil Sélection) : rouvre l'éditeur en place.</li>
  <li><b>Supprimer la sélection</b> — supprime l'élément sélectionné.
      <b>Effacer annotations</b> — supprime tout le calque de la photo.</li>
  <li>Bouton <b>✏ Annotations</b> en haut de la fenêtre (à côté d'EXIF) — affiche/masque le
      calque sans rien supprimer ; ce réglage n'est pas enregistré, il ne dure que la session.</li>
</ul>
<p class="tip">Les annotations sont incluses dans l'export et dans l'enregistrement de l'image
traitée, comme les autres retouches — sauf si le calque est masqué via le bouton
✏ Annotations au moment de l'export.</p>

<h3>Workflow</h3>
<ul>
  <li><b>Un seul outil actif à la fois</b> — sélectionner un nouvel outil (Recadrer, Yeux
      rouges, Annotations, Luminosité/Contraste/Couleurs/Vignette/Redresser…) valide
      automatiquement le travail en cours dans l'outil précédent, puis le referme.</li>
  <li>Double-clic sur un slider : remet la valeur à zéro.</li>
  <li>L'aperçu se met à jour en temps réel (≤ 60 ms).</li>
  <li>Cliquez <b>Appliquer</b> pour enregistrer.</li>
  <li><kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd> — undo/redo persistant
      entre les sessions (50 états max par photo).</li>
  <li><b>Réinitialiser</b> — revient à l'original sans supprimer l'historique.</li>
</ul>

<h3>Export</h3>
<p><b>Enregistrer une copie</b> — exporte l'image avec les retouches appliquées dans un nouveau
fichier JPEG. Quatre préréglages de taille sont proposés :</p>
<table>
  <tr><th>Préréglage</th><th>Résolution max.</th><th>Poids estimé</th><th>Usage typique</th></tr>
  <tr><td><b>Taille maximale</b></td><td>Résolution originale</td><td>variable</td>
      <td>Archivage, impression grand format, retouche externe</td></tr>
  <tr><td><b>Grande (~4 Mpx)</b></td><td>≈ 2 600 × 1 500 px</td><td>600–1 600 Ko</td>
      <td>Impression A4/A3, partage haute qualité</td></tr>
  <tr><td><b>Moyenne (~2 Mpx)</b></td><td>≈ 1 800 × 1 100 px</td><td>320–800 Ko</td>
      <td>Affichage écran, diaporamas, pièce jointe mail légère</td></tr>
  <tr><td><b>Petite (~500 kpx)</b></td><td>≈ 900 × 560 px</td><td>75–300 Ko</td>
      <td>Réseaux sociaux, aperçu web, messagerie mobile</td></tr>
</table>
<p class="tip"><b>Conseil :</b> Pour envoyer une photo par e-mail ou la poster sur un réseau
social, préférez <b>Moyenne</b> ou <b>Petite</b> — une photo en taille maximale peut dépasser
5 Mo et être refusée ou ralentir l'envoi. L'original sur le disque n'est jamais modifié.</p>

<h3>EXIF</h3>
<ul>
  <li>Touche <kbd>I</kbd> (ou bouton <b>[i]</b>) : affiche/masque le panneau EXIF dans la visionneuse.</li>
  <li><b>Outils › Synchroniser les dates EXIF</b> — corrige les dates en lot.</li>
</ul>
"""

_TAB_FACES = _STYLE + """
<h2>Détection et reconnaissance des visages</h2>

<p>Le pipeline de reconnaissance se déroule en trois étapes :</p>
<p style="text-align:center; font-size:13px;">
  <b>① Analyser</b> (automatique) &nbsp;→&nbsp; <b>② Regrouper</b> &nbsp;→&nbsp; <b>③ Identifier</b>
</p>
<p>Seules les étapes ② et ③ demandent une action de votre part.
Les options ci-dessous sont présentées dans l'ordre où elles apparaissent dans le
menu <b>Visages</b>. Lisez les descriptions avant d'en utiliser une : certaines sont longues.</p>

<hr/>

<h3>① Analyser les visages &nbsp;<span style="font-weight:normal;color:#888;">(automatique, aucune action requise)</span></h3>
<p><b>Ce que ça fait :</b> Dès qu'un scan de la bibliothèque détecte de nouvelles photos,
l'analyse démarre automatiquement en arrière-plan : InsightFace (modèle buffalo_l) détecte
les visages et calcule un vecteur ArcFace 512D par visage. À la fin, le regroupement
démarre lui aussi automatiquement. La progression s'affiche dans la barre de statut.</p>
<p><b>Périmètre :</b> Uniquement les photos <u>jamais encore analysées</u> (nouvelles photos
ajoutées à la bibliothèque). Les photos déjà traitées sont ignorées, même si elles
contiennent des visages marqués «&nbsp;ignoré&nbsp;».</p>
<p class="tip">Durée : de quelques secondes (nouvelle photo) à plusieurs heures
(première analyse d'une grande bibliothèque). L'application reste utilisable pendant ce temps.</p>

<hr/>

<h3>Import Picasa &nbsp;<span style="font-weight:normal;color:#888;">(Visages › Importer depuis Picasa…)</span></h3>
<ul>
  <li>Importe les annotations de visages depuis les fichiers <code>.picasa.ini</code>
      de Google Picasa : noms et régions de visages.</li>
  <li>Les noms sont associés automatiquement aux personnes existantes ou créent
      de nouvelles personnes.</li>
</ul>
<p class="tip"><b>À ne faire qu'une seule fois.</b> Un nouvel import ré-insère tous les noms
et annotations Picasa déjà connus, ce qui peut créer des doublons ou réintroduire
des associations supprimées. Un avertissement s'affiche si vous tentez de relancer l'import.</p>

<hr/>

<h3>Réinitialiser et réindexer &nbsp;<span style="font-weight:normal;color:#888;">(Visages › Réinitialiser et réindexer…)</span></h3>
<p>Deux options au choix, à utiliser avec précaution :</p>
<table>
  <tr><th>Option</th><th>Ce qui est effacé</th><th>Ce qui est conservé</th></tr>
  <tr>
    <td><b>Reset clustering</b></td>
    <td>Groupes HDBSCAN uniquement</td>
    <td>Embeddings, noms et associations personne ↔ visage</td>
  </tr>
  <tr>
    <td><b>Réindexation complète</b></td>
    <td>Tout (embeddings, groupes, noms, associations)</td>
    <td>— (table rase)</td>
  </tr>
</table>
<p class="tip"><b>La réindexation complète prend plusieurs heures</b> selon la taille
de la bibliothèque. Faites une sauvegarde avant.</p>

<hr/>

<h3>② Regrouper les visages &nbsp;<span style="font-weight:normal;color:#888;">(Visages › Regrouper les visages…)</span></h3>
<p><b>Ce que ça fait :</b> Regroupe par similarité les visages qui ont déjà été analysés,
à l'aide de l'algorithme HDBSCAN sur les vecteurs ArcFace. Les groupes sont ensuite
présentés dans <i>Identifier les personnes…</i></p>
<p><b>Périmètre :</b> Tous les visages avec un embedding stocké, <u>sauf</u> ceux déjà
identifiés (person_id assigné). Les associations validées sont conservées intactes.</p>
<p><b>Ce que ça ne fait PAS :</b></p>
<ul>
  <li>Ne relance pas la détection InsightFace.</li>
  <li>N'efface pas les noms ou associations déjà confirmés.</li>
  <li>Ne traite pas les visages marqués «&nbsp;ignoré&nbsp;».</li>
</ul>
<p class="tip">Durée : 15 à 30 minutes selon la taille de la bibliothèque.
À relancer après avoir ajusté la tolérance de similarité dans les Paramètres.</p>
<p class="tip">Dès que de nouveaux groupes sont formés, l'application compare
automatiquement le centroïde (embedding moyen) de chacun aux personnes déjà nommées.
Au-delà de 50&nbsp;% de similarité cosinus, une suggestion apparaît dans la vue de la
personne concernée, section «&nbsp;En attente de vérification&nbsp;» — voir l'étape
③ ci-dessous. Cette comparaison est silencieuse, ne relance jamais InsightFace et
n'associe jamais un visage sans validation de votre part.</p>

<hr/>

<h3>Visualisation des erreurs &nbsp;<span style="font-weight:normal;color:#888;">(Visages › Visualisation des erreurs…)</span></h3>
<p><b>Ce que ça fait :</b> Liste les photos pour lesquelles l'étape ① (analyse) a échoué
— timeout ou crash du sous-processus de détection — avec vignette, nom de fichier et
type d'erreur. Le bouton <b>⟳ Réessayer</b> relance l'analyse pour ce seul fichier ;
la ligne disparaît de la liste dès que le traitement réussit.</p>
<p><b>Périmètre :</b> Uniquement les photos en erreur. Tant qu'une photo reste en erreur,
elle est exclue des analyses automatiques suivantes (elle n'est pas retentée à chaque scan).</p>
<p class="tip">Ces photos sont aussi repérables directement dans la grille : un clic droit
sur une vignette en erreur propose l'option « Retenter l'identification des visages »
dans le menu contextuel.</p>

<hr/>

<h3>Sauvegarde et restauration</h3>
<ul>
  <li><b>Visages › Sauvegarder la reconnaissance…</b> — crée une sauvegarde horodatée
      de l'intégralité de <code>faces.db</code> (embeddings, groupes, personnes, annotations).</li>
  <li><b>Visages › Gérer les sauvegardes…</b> — liste, restaure ou supprime les sauvegardes.
      L'état courant est automatiquement sauvegardé avant toute restauration.</li>
</ul>

<hr/>

<h3>Compteurs &nbsp;<span style="font-weight:normal;color:#888;">(Visages › Compteurs…)</span></h3>
<p>Affiche un résumé chiffré de la reconnaissance faciale : nombre de personnes
identifiées, de visages identifiés / reconnus / en attente de confirmation / inconnus,
ainsi que les compteurs liés à l'import Picasa. Aucun traitement n'est lancé —
c'est un simple état des lieux.</p>

<hr/>

<h3>③ Identifier les personnes &nbsp;<span style="font-weight:normal;color:#888;">(bouton « Identifier… » dans la sidebar)</span></h3>
<p>Le bouton <b>Identifier…</b> de la barre latérale (avec un badge indiquant le nombre
de groupes en attente) ouvre la vue des groupes anonymes pour les nommer. Les suggestions
de noms (en bleu) indiquent une forte ressemblance avec une personne déjà nommée.
Attribuer le même nom à plusieurs groupes les fusionne dans la même personne.</p>
<p>Aucun traitement n'est lancé — c'est une navigation pure.</p>

<h3>Vue des visages d'une personne</h3>
<p>Cliquez sur une personne dans la sidebar pour ouvrir sa vue détaillée.
Elle affiche deux sections :</p>
<ul>
  <li><b>Visages confirmés</b> — visages déjà associés à cette personne.
    <ul>
      <li>Simple clic : sélectionner un visage.</li>
      <li><kbd>Ctrl</kbd>+clic : multi-sélection. <kbd>Shift</kbd>+clic : sélection en plage.</li>
      <li>Double-clic : ouvrir la photo correspondante dans la visionneuse.</li>
    </ul>
  </li>
</ul>
<p><b>Clic droit</b> sur un visage confirmé :</p>
<table>
  <tr><th>Action</th><th>Effet</th></tr>
  <tr><td><b>Réassigner</b></td><td>Déplace le ou les visages sélectionnés vers une autre personne</td></tr>
  <tr><td><b>Dé-associer</b></td><td>Retire le visage de la personne ; il est réévalué pour d'autres personnes</td></tr>
  <tr><td><b>Définir comme vignette principale</b></td><td>Utilise ce visage comme avatar dans la sidebar</td></tr>
</table>
<ul>
  <li><b>En attente de vérification</b> — groupes que le système suggère d'associer à cette personne.
    <ul>
      <li>Survolez une vignette pour faire apparaître <b>✓</b> (accepter) et <b>✗</b> (rejeter).</li>
      <li><b>Clic droit</b> : accepter ou rejeter le groupe entier.</li>
      <li>Boutons <b>Accepter toutes</b> / <b>Rejeter toutes</b> en en-tête de la section.</li>
    </ul>
  </li>
</ul>
<p class="tip">Un visage rejeté n'est jamais perdu : il reste isolé et continue d'être
candidat à une suggestion pour d'autres personnes.</p>

<h3>Gérer les personnes (sidebar)</h3>
<ul>
  <li>Les personnes nommées apparaissent dans la barre latérale sous <b>Personnes</b>.</li>
  <li>Un badge orange entre la vignette et le nom indique le nombre de suggestions en attente.</li>
  <li>Dans la visionneuse, le panneau <b>Visages</b> liste les visages de la photo ouverte.</li>
</ul>
<p><b>Clic droit</b> sur une personne dans la sidebar :</p>
<table>
  <tr><th>Action</th><th>Effet</th></tr>
  <tr><td><b>Renommer…</b></td><td>Modifie le nom dans toute la bibliothèque</td></tr>
  <tr><td><b>Fusionner avec…</b></td><td>Fusionne tous les visages de cette personne avec ceux d'une autre</td></tr>
  <tr><td><b>Effacer le nom…</b></td><td>Retire le nom — les visages redeviennent des groupes anonymes</td></tr>
</table>

<hr/>

<h3>Visages ignorés</h3>
<p>Un visage peut être ignoré de deux façons :</p>
<ul>
  <li><b>Automatiquement</b> lors de la détection : taille inférieure au seuil proportionnel
      ou score de confiance &lt; 0,65. Le visage est stocké mais exclu du clustering.</li>
  <li><b>Manuellement</b> via le bouton ✕ dans le panneau Visages (faux positif, doublure,
      arrière-plan). Le visage reste récupérable via <b>Visages ignorés…</b> (ci-dessous).</li>
</ul>
<p>Le bouton <b>Visages ignorés…</b> en bas du panneau Visages permet de voir et restaurer
les visages ignorés photo par photo.</p>

<hr/>
"""

_TAB_SHORTCUTS = _STYLE + """
<h2>Raccourcis clavier</h2>

<h3>Global</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>F9</kbd></td><td>Afficher / masquer la sidebar</td></tr>
  <tr><td><kbd>F11</kbd></td><td>Basculer en plein écran</td></tr>
  <tr><td><kbd>F5</kbd></td><td>Lancer le diaporama</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Quitter la visionneuse / plein écran</td></tr>
</table>

<h3>Grille de photos</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>Suppr</kbd></td><td>Supprimer les photos sélectionnées (avec confirmation) — dans un album : retirer de l'album, sans toucher aux fichiers</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>A</kbd></td><td>Tout sélectionner</td></tr>
  <tr><td>Double-clic</td><td>Ouvrir dans la visionneuse</td></tr>
</table>

<h3>Mode chronologie (ruban)</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>◀</kbd> <kbd>▶</kbd></td><td>Déplacer d'une photo</td></tr>
  <tr><td><kbd>▲</kbd> <kbd>▼</kbd></td><td>Déplacer de 3 photos</td></tr>
  <tr><td>Molette</td><td>Défilement avec inertie</td></tr>
  <tr><td>Ascenseur (droite)</td><td>Navigation rapide dans la chronologie</td></tr>
  <tr><td><kbd>Suppr</kbd></td><td>Supprimer la photo centrale (ou la sélection)</td></tr>
</table>

<h3>Visionneuse</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>◀</kbd> <kbd>▶</kbd> ou Molette</td><td>Photo précédente / suivante</td></tr>
  <tr><td><kbd>Ctrl</kbd>+Molette</td><td>Zoom avant / arrière</td></tr>
  <tr><td>Double-clic</td><td>Zoom 1:1 ↔ ajustement automatique</td></tr>
  <tr><td><kbd>0</kbd></td><td>Ajustement à la fenêtre (fit)</td></tr>
  <tr><td><kbd>1</kbd></td><td>Zoom 100 %</td></tr>
  <tr><td><kbd>I</kbd></td><td>Afficher / masquer le panneau EXIF</td></tr>
  <tr><td><kbd>F</kbd></td><td>Marquer / retirer des favoris</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Z</kbd></td><td>Annuler la dernière retouche</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Y</kbd></td><td>Rétablir la retouche annulée</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Retour à la grille</td></tr>
</table>

<h3>Diaporama</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>◀</kbd> <kbd>▲</kbd></td><td>Photo plus ancienne</td></tr>
  <tr><td><kbd>▶</kbd> <kbd>▼</kbd></td><td>Photo plus récente</td></tr>
  <tr><td><kbd>Espace</kbd></td><td>Pause / Reprendre</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Quitter le diaporama</td></tr>
</table>

<h3>Mode recadrage</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>Entrée</kbd></td><td>Confirmer le recadrage</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Annuler le recadrage</td></tr>
  <tr><td>Molette</td><td>Zoomer dans la visionneuse</td></tr>
</table>

<h3>Mode annotation</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>Suppr</kbd></td><td>Supprimer l'élément d'annotation sélectionné</td></tr>
  <tr><td><kbd>Entrée</kbd></td><td>Valider une courbe en cours de tracé</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Annuler le tracé en cours (le mode reste actif)</td></tr>
</table>
"""

_TAB_DUPES = _STYLE + """
<h2>Détection de doublons</h2>
<p>PixelPhotoManager repère les photos visuellement similaires même lorsqu'elles ont
été redimensionnées, légèrement retouchées ou recadrées.</p>

<h3>Lancer la détection</h3>
<ul>
  <li><b>Outils › Détecter les doublons…</b> — lance l'analyse en arrière-plan.</li>
  <li>La durée dépend de la taille de la bibliothèque ; l'application reste utilisable.</li>
  <li>Une barre de progression et un bouton <b>Annuler</b> apparaissent dans la barre de statut
      pendant l'analyse ; les résultats n'apparaissent qu'à la fin.</li>
  <li>Cliquer sur <b>Annuler</b> demande confirmation — les résultats déjà calculés seraient perdus.</li>
  <li>Fermer l'application pendant une analyse en cours affiche un avertissement (le résultat
      serait perdu), avec le choix de fermer quand même ou d'annuler la fermeture.</li>
</ul>

<h3>Comment ça marche — deux passes</h3>
<table>
  <tr><th>Passe</th><th>Technique</th><th>Cas couverts</th></tr>
  <tr><td><b>1 — pHash</b></td><td>Empreinte perceptuelle (Hamming)</td>
      <td>Doublons exacts, redimensionnés, légèrement retouchés (couleur, luminosité)</td></tr>
  <tr><td><b>2 — ORB + RANSAC</b></td><td>Correspondance de points-clés</td>
      <td>Doublons recadrés (jusqu'à ~60 % de surface rognée) — uniquement pour les photos
          non groupées par la passe 1</td></tr>
</table>
<p class="tip"><b>Note :</b> Les vidéos ne sont pas analysées pour les doublons.</p>

<h3>Badge de doublon</h3>
<ul>
  <li>Les vignettes appartenant à un groupe de doublons affichent un badge <b>⧉</b>.</li>
  <li>Cliquer sur ce badge ouvre une vue listant toutes les photos du groupe côte à côte.</li>
</ul>

<h3>Grille des groupes de doublons &nbsp;<span style="font-weight:normal;color:#888;">(bouton « Dupliquées » de la sidebar)</span></h3>
<p>Le bouton <b>Dupliquées</b> de la sidebar (badge = nombre de groupes) ouvre une grille dédiée
listant tous les groupes de doublons d'un coup : une carte par groupe, vignette du premier
exemplaire, nombre d'exemplaires.</p>
<ul>
  <li><b>Double-clic</b> sur une carte : ouvre les exemplaires du groupe dans la visionneuse
      pour une comparaison rapide.</li>
  <li><b>✕</b> sur une carte : dissout le groupe entier (aucun fichier supprimé) — non
      persistant, une future détection complète peut reformer le même groupe.</li>
  <li>Bouton <b>Détecter les doublons…</b> en haut de la grille pour relancer une analyse.</li>
</ul>

<h3>Workflow recommandé</h3>
<ul>
  <li>Lancez la détection via <b>Outils › Détecter les doublons…</b>.</li>
  <li>Quand l'analyse est terminée, les badges <b>⧉</b> apparaissent sur les vignettes concernées.</li>
  <li>Cliquez sur un badge pour examiner le groupe de photos similaires.</li>
  <li>Sélectionnez les exemplaires à conserver / supprimer, puis appuyez sur <kbd>Suppr</kbd>.</li>
</ul>
<p class="tip">La suppression d'un doublon est <b>définitive</b> (pas de corbeille).
Vérifiez bien les photos avant de confirmer.</p>

<h3>Fichiers corrompus détectés pendant l'analyse</h3>
<p>Si un fichier ne peut pas être lu pendant l'analyse (JPEG endommagé, copie
interrompue…), il est signalé plutôt qu'ignoré silencieusement. Le bilan de fin
d'analyse indique alors le nombre de fichiers concernés, avec un bouton
<b>Réparer…</b>.</p>
<ul>
  <li>Une confirmation est demandée avant toute tentative de réparation.</li>
  <li>PixelPhotoManager essaie de ré-enregistrer une copie propre du fichier à
      l'aide d'un décodeur plus tolérant que celui utilisé pour l'analyse.</li>
  <li>L'original est toujours sauvegardé avant modification (dossier caché
      <code>.tmp_originals</code> à côté du fichier), et les dates Windows de
      modification et de création sont préservées à l'identique.</li>
  <li>Un second bilan indique le nombre de fichiers réparés. Les fichiers qui
      n'ont pas pu être réparés (corruption trop importante) sont listés dans
      un fichier texte horodaté.</li>
</ul>
<p class="tip"><b>Outils › Historique des problèmes…</b> conserve la trace de
chaque analyse ayant rencontré des fichiers corrompus (date, nombre détecté,
nombre réparé) avec un accès direct à la liste des fichiers non réparés.</p>
"""

_TAB_SETTINGS = _STYLE + """
<h2>Paramètres</h2>
<p>Accédez aux paramètres via <b>Outils › Paramètres</b>.</p>

<h3>Reconnaissance de visages — Tolérance de similarité</h3>
<p>Contrôle à quel point deux visages doivent se ressembler pour être placés dans le
même groupe lors du clustering. Plage : 25 % à 70 %. Valeur par défaut : <b>60 %</b>.</p>
<table>
  <tr><th>Plage</th><th>Comportement</th></tr>
  <tr><td>25–30 %</td><td>Groupes très stricts — peu d'erreurs de mélange, mais une même
      personne sous des angles ou éclairages très différents peut former plusieurs groupes</td></tr>
  <tr><td>31–40 %</td><td>Groupes équilibrés</td></tr>
  <tr><td>41–55 %</td><td>Groupes plus larges — regroupe davantage de variantes du même visage</td></tr>
  <tr><td>56–70 %</td><td>Groupes très larges — risque de mélanger des personnes différentes</td></tr>
</table>
<p class="tip">Si vous modifiez ce réglage, les groupes sont recalculés automatiquement
à la fermeture du dialogue. Les associations de personnes déjà nommées ne sont pas affectées.</p>

<h3>Lecteur vidéo</h3>
<p>Choisissez le lecteur utilisé par le bouton <b>▶ Ouvrir la vidéo</b> dans la visionneuse.</p>
<table>
  <tr><th>Option</th><th>Comportement</th></tr>
  <tr><td><b>Lecteur par défaut du système</b></td><td>Utilise l'application associée aux fichiers
      vidéo dans Windows (ex. Films &amp; TV)</td></tr>
  <tr><td><b>Lecteur personnalisé</b></td><td>Spécifiez le chemin vers un exécutable.
      Cliquez <b>Parcourir…</b> pour naviguer jusqu'à l'exécutable.
      Exemples : <code>C:\Program Files\VLC\vlc.exe</code>,
      <code>C:\Program Files\MPC-HC\mpc-hc64.exe</code></td></tr>
</table>
"""

_TAB_ABOUT = _STYLE + f"""
<h2>Pixel Photo Manager</h2>
<p style="color:#aaa;">Version {get_app_version()} &nbsp;·&nbsp; Windows x64</p>
<p>{{version_check}}</p>
<p>Copyright 2026 Christian Guyot<br>
Distribué sous les termes de l'<b>Apache License, Version 2.0</b>.<br>
<a href="http://www.apache.org/licenses/LICENSE-2.0" style="color:#6aacf0;">
www.apache.org/licenses/LICENSE-2.0</a></p>

<h2>Composants tiers</h2>
<table>
  <tr><th>Composant</th><th>Licence</th></tr>
  <tr><td><b>PySide6</b> (Qt for Python)</td><td>LGPLv3 / GPLv2</td></tr>
  <tr><td><b>Pillow</b></td><td>HPND</td></tr>
  <tr><td><b>OpenCV</b> (opencv-python)</td><td>Apache 2.0</td></tr>
  <tr><td><b>InsightFace</b></td><td>MIT</td></tr>
  <tr><td><b>ONNX Runtime</b></td><td>MIT</td></tr>
  <tr><td><b>scikit-learn</b></td><td>BSD 3-Clause</td></tr>
  <tr><td><b>HDBSCAN</b></td><td>BSD 3-Clause</td></tr>
  <tr><td><b>imagehash</b></td><td>BSD 2-Clause</td></tr>
  <tr><td><b>folium</b></td><td>MIT</td></tr>
  <tr><td><b>ReportLab</b></td><td>BSD</td></tr>
  <tr><td><b>psutil</b></td><td>BSD 3-Clause</td></tr>
  <tr><td><b>piexif</b></td><td>MIT</td></tr>
</table>
<p style="color:#888; font-size:12px; margin-top:12px;">
Le texte complet des licences tierces est disponible dans le fichier NOTICE
distribué avec le code source.</p>
"""

_TABS = [
    ("Vue d'ensemble",  _TAB_OVERVIEW),
    ("Navigation",      _TAB_NAVIGATION),
    ("Diaporama",       _TAB_SLIDESHOW),
    ("Retouches",       _TAB_EDITING),
    ("Visages",         _TAB_FACES),
    ("Doublons",        _TAB_DUPES),
    ("Raccourcis",      _TAB_SHORTCUTS),
    ("Paramètres",      _TAB_SETTINGS),
    ("À propos",        _TAB_ABOUT),
]

_BROWSER_STYLE = """
QTextBrowser {
    background: #2b2b2b;
    border: none;
    padding: 8px;
}
"""

_TABWIDGET_STYLE = """
QTabWidget::pane {
    border: 1px solid #444;
    background: #2b2b2b;
}
QTabBar::tab {
    background: #2a2a2a;
    color: #bbb;
    padding: 5px 12px;
    border: 1px solid #444;
    border-bottom: none;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: #2a5a9a;
    color: #ffffff;
    font-weight: bold;
    border-color: #3a6ab0;
    border-bottom: 1px solid #2a5a9a;
}
QTabBar::tab:hover:!selected {
    background: #333;
    color: #eee;
}
"""


class HelpDialog(QDialog):
    def __init__(self, parent=None, tab: str | None = None):
        super().__init__(parent)
        # Sans ça, chaque ouverture d'Aide/À propos (dlg.exec() dans main_window.py)
        # laissait le QDialog et son QThread de vérification de version en vie
        # indéfiniment, parentés à MainWindow — fuite qui grossit à chaque ouverture.
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle("Aide — PixelPhotoManager")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        tabs = QTabWidget()
        tabs.setStyleSheet(_TABWIDGET_STYLE)
        self._about_browser: QTextBrowser | None = None
        for title, html in _TABS:
            browser = QTextBrowser()
            browser.setOpenExternalLinks(True)
            browser.setStyleSheet(_BROWSER_STYLE)
            if title == "À propos":
                self._about_browser = browser
                html = html.replace(
                    "{version_check}",
                    '<span style="color:#888;">Vérification de la version…</span>',
                )
            browser.setHtml(html)
            browser.verticalScrollBar().setValue(0)
            tabs.addTab(browser, title)

        if tab is not None:
            for i, (title, _) in enumerate(_TABS):
                if title == tab:
                    tabs.setCurrentIndex(i)
                    break

        layout.addWidget(tabs)

        # Pas de parent : WA_DeleteOnClose peut détruire ce dialogue avant que la
        # vérification réseau (jusqu'à 5s) ne se termine — un QThread parenté serait
        # alors détruit alors qu'il tourne encore. Il s'auto-nettoie via `finished`.
        self._update_check_thread = UpdateCheckThread()
        self._update_check_thread.checked.connect(self._on_version_checked)
        self._update_check_thread.finished.connect(self._update_check_thread.deleteLater)
        self._update_check_thread.start()

        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.accept)
        layout.addWidget(btn_box)

    def closeEvent(self, event) -> None:
        """Coupe le rappel vers ce dialogue (bientôt détruit via WA_DeleteOnClose)
        sans attendre la fin du thread de vérification, qui continue et se
        nettoie lui-même (cf. __init__)."""
        try:
            self._update_check_thread.checked.disconnect(self._on_version_checked)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

    def _on_version_checked(self, status: str, version: str, html_url: str) -> None:
        if self._about_browser is None:
            return
        if status == STATUS_UPDATE_AVAILABLE:
            fragment = (
                '<span style="color:#e0a030;">⚠ Une nouvelle version est disponible : '
                f'<b>{version}</b> — <a href="{html_url}" style="color:#6aacf0;">'
                "ouvrir la page de téléchargement</a></span>"
            )
        elif status == STATUS_UP_TO_DATE:
            fragment = '<span style="color:#6abf6a;">✓ Vous disposez de la dernière version.</span>'
        elif status == STATUS_VERSION_UNKNOWN:
            fragment = (
                '<span style="color:#888;">Version locale non comparable (mode développement) — '
                f"dernière version publiée : <b>{version}</b>.</span>"
            )
        else:
            fragment = (
                '<span style="color:#888;">Impossible de vérifier la disponibilité '
                "d'une nouvelle version (pas de connexion ?).</span>"
            )
        scroll_pos = self._about_browser.verticalScrollBar().value()
        self._about_browser.setHtml(_TAB_ABOUT.replace("{version_check}", fragment))
        self._about_browser.verticalScrollBar().setValue(scroll_pos)
