from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QTextBrowser, QDialogButtonBox,
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
  <li><b>Barre de recherche</b> — en haut, recherche instantanée par nom de fichier ou appareil
      (<kbd>Ctrl</kbd>+<kbd>F</kbd>).</li>
  <li><b>Barre latérale gauche</b> — dossiers surveillés, albums, personnes identifiées
      (<kbd>F9</kbd> pour afficher/masquer).</li>
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
  <li><kbd>Ctrl</kbd>+<kbd>F</kbd> : mettre le focus sur la barre de recherche.</li>
  <li>Recherche sur le nom de fichier, la marque et le modèle d'appareil photo.</li>
  <li>Résultats en temps réel (délai 150 ms). Cliquez <b>✕</b> pour revenir à l'affichage normal.</li>
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
  <tr><td><b>Effacer le fichier…</b></td><td>Supprime le fichier du disque (<b>irréversible</b>, confirmation demandée)</td></tr>
</table>
<p class="tip"><b>Astuce :</b> Pour déplacer une ou plusieurs photos, vous pouvez aussi les
glisser-déposer directement vers un dossier dans la barre latérale.</p>

<h3>Supprimer des fichiers</h3>
<ul>
  <li>Dans la grille : sélectionnez puis appuyez sur <kbd>Suppr</kbd>.</li>
  <li>En mode chronologie sans sélection : <kbd>Suppr</kbd> efface la photo centrale.</li>
  <li>Dans la visionneuse : bouton 🗑 dans la barre d'outils.</li>
  <li>La suppression est <b>définitive</b> (pas de corbeille). Une confirmation est demandée.</li>
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

<h3>Workflow</h3>
<ul>
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

<h3>Analyser les visages</h3>
<ul>
  <li><b>Visages › Analyser les visages</b> — lance la détection en arrière-plan
      sur toutes les photos non encore traitées.</li>
  <li>L'analyse est progressive et non bloquante ; l'application reste utilisable.</li>
  <li>Les visages détectés sont regroupés automatiquement par ressemblance (<i>clustering</i>).</li>
</ul>

<h3>Identifier les personnes</h3>
<ul>
  <li><b>Visages › Identifier les personnes…</b> — ouvre la vue des groupes de visages.</li>
  <li>Cliquez sur un groupe pour lui attribuer un nom.</li>
  <li>Les suggestions de noms (en bleu) indiquent une ressemblance avec une personne déjà nommée.</li>
  <li>Attribuer le même nom à plusieurs groupes les fusionne automatiquement dans la même personne.</li>
</ul>

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

<h3>Regroupement et réinitialisation</h3>
<ul>
  <li><b>Visages › Regrouper les visages…</b> — relance le clustering sur les visages déjà
      analysés (rapide, sans réanalyse des photos). Utile après avoir ajusté la tolérance
      dans les Paramètres.</li>
  <li><b>Visages › Réinitialiser et réindexer…</b> — efface tous les visages et relance
      la détection depuis zéro. Toutes les associations sont perdues.</li>
</ul>

<h3>Sauvegarde et restauration</h3>
<ul>
  <li><b>Visages › Sauvegarder la reconnaissance…</b> — crée une sauvegarde horodatée.</li>
  <li><b>Visages › Gérer les sauvegardes…</b> — liste et restaure une sauvegarde
      (l'état courant est automatiquement sauvegardé avant la restauration).</li>
</ul>

<h3>Visages ignorés</h3>
<ul>
  <li>Un visage peut être marqué comme <i>ignoré</i> (faux positif, doublure, arrière-plan).</li>
  <li><b>Visages › Visages ignorés…</b> — liste et restaure les visages ignorés.</li>
</ul>

<h3>Import Picasa</h3>
<ul>
  <li><b>Visages › Importer depuis Picasa…</b> — importe les annotations de visages
      depuis les fichiers <code>.picasa.ini</code> de Google Picasa.</li>
  <li>Les noms et régions existants sont associés automatiquement aux personnes existantes.</li>
</ul>
<p class="tip"><b>À ne faire qu'une seule fois.</b> Un nouvel import ré-insère tous les noms
et annotations Picasa déjà connus, ce qui peut créer des doublons ou réintroduire
des associations que vous avez supprimées. Un avertissement vous sera affiché si vous
tentez de relancer l'import.</p>
"""

_TAB_SHORTCUTS = _STYLE + """
<h2>Raccourcis clavier</h2>

<h3>Global</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>F</kbd></td><td>Mettre le focus sur la barre de recherche</td></tr>
  <tr><td><kbd>F9</kbd></td><td>Afficher / masquer la sidebar</td></tr>
  <tr><td><kbd>F11</kbd></td><td>Basculer en plein écran</td></tr>
  <tr><td><kbd>F5</kbd></td><td>Lancer le diaporama</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Quitter la visionneuse / plein écran</td></tr>
</table>

<h3>Grille de photos</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>Suppr</kbd></td><td>Supprimer les photos sélectionnées (avec confirmation)</td></tr>
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
"""

_TAB_DUPES = _STYLE + """
<h2>Détection de doublons</h2>
<p>PixelPhotoManager repère les photos visuellement similaires même lorsqu'elles ont
été redimensionnées, légèrement retouchées ou recadrées.</p>

<h3>Lancer la détection</h3>
<ul>
  <li><b>Outils › Détecter les doublons…</b> — lance l'analyse en arrière-plan.</li>
  <li>La durée dépend de la taille de la bibliothèque ; l'application reste utilisable.</li>
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

<h3>Workflow recommandé</h3>
<ul>
  <li>Lancez la détection via <b>Outils › Détecter les doublons…</b>.</li>
  <li>Quand l'analyse est terminée, les badges <b>⧉</b> apparaissent sur les vignettes concernées.</li>
  <li>Cliquez sur un badge pour examiner le groupe de photos similaires.</li>
  <li>Sélectionnez les exemplaires à conserver / supprimer, puis appuyez sur <kbd>Suppr</kbd>.</li>
</ul>
<p class="tip">La suppression d'un doublon est <b>définitive</b> (pas de corbeille).
Vérifiez bien les photos avant de confirmer.</p>
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

_TABS = [
    ("Vue d'ensemble",  _TAB_OVERVIEW),
    ("Navigation",      _TAB_NAVIGATION),
    ("Diaporama",       _TAB_SLIDESHOW),
    ("Retouches",       _TAB_EDITING),
    ("Visages",         _TAB_FACES),
    ("Doublons",        _TAB_DUPES),
    ("Raccourcis",      _TAB_SHORTCUTS),
    ("Paramètres",      _TAB_SETTINGS),
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Aide — PixelPhotoManager")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        tabs = QTabWidget()
        tabs.setStyleSheet(_TABWIDGET_STYLE)
        for title, html in _TABS:
            browser = QTextBrowser()
            browser.setOpenExternalLinks(False)
            browser.setStyleSheet(_BROWSER_STYLE)
            browser.setHtml(html)
            browser.verticalScrollBar().setValue(0)
            tabs.addTab(browser, title)
        layout.addWidget(tabs)

        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.accept)
        layout.addWidget(btn_box)
