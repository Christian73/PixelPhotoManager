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
  <tr><th>Fichier</th><th>Contenu</th></tr>
  <tr><td><code>catalog.db</code></td><td>Index photos/vidéos (chemins, EXIF, métadonnées)</td></tr>
  <tr><td><code>thumbnails.db</code></td><td>Cache des vignettes générées</td></tr>
  <tr><td><code>edits.db</code></td><td>Toutes les retouches et leur historique</td></tr>
  <tr><td><code>config.json</code></td><td>Dossiers surveillés et préférences</td></tr>
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
  <li><b>Clic droit</b> sur un dossier : Scanner, Supprimer, Renommer, Déplacer, Ouvrir dans l'Explorateur.</li>
  <li><b>Outils › Dossiers…</b> — gestion avancée : statut, nombre de fichiers, re-scan forcé.</li>
</ul>

<h3>Barre latérale — Albums</h3>
<ul>
  <li><b>Toutes les photos</b> — vue globale en mode chronologie.</li>
  <li><b>♡ Favoris</b> — photos marquées d'une étoile dans la visionneuse.</li>
  <li><b>Vidéos</b> — toutes les vidéos de la bibliothèque.</li>
  <li>Albums personnels — créés via le bouton <b>+</b> dans l'en-tête Albums.</li>
</ul>

<h3>Barre latérale — Personnes</h3>
<ul>
  <li>Affiche les personnes identifiées par l'analyse des visages.</li>
  <li>Cliquez sur une personne pour voir toutes ses photos.</li>
</ul>

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
  <tr><td><b>Saturation</b></td><td>−1,00 à +1,00</td><td>Renforce ou atténue les couleurs (−1 = N&B)</td></tr>
  <tr><td><b>Gamma</b></td><td>0,10 à 3,00</td><td>Courbe de luminosité (1,0 = neutre)</td></tr>
  <tr><td><b>Netteté</b></td><td>0,00 à 1,00</td><td>Accentue les contours</td></tr>
  <tr><td><b>Débruitage</b></td><td>0,00 à 1,00</td><td>Lisse le bruit numérique</td></tr>
</table>

<h3>Couleurs (Noir &amp; Blanc avec mixage de canaux)</h3>
<ul>
  <li>Cochez <b>Noir &amp; Blanc</b> et dosez les contributions Rouge/Vert/Bleu (−1 à +1).</li>
  <li>Exemple : Rouge +1, Bleu −1 → ciel foncé et peaux claires (filtre rouge argentique).</li>
</ul>

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
<ul>
  <li><b>Enregistrer une copie</b> — exporte l'image avec les retouches dans un nouveau fichier.</li>
</ul>

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
  <li>Attribuer le même nom à plusieurs groupes les fusionne automatiquement.</li>
</ul>

<h3>Gérer les personnes</h3>
<ul>
  <li>Les personnes nommées apparaissent dans la barre latérale sous <b>Personnes</b>.</li>
  <li>Cliquez sur une personne pour voir toutes ses photos.</li>
  <li>Dans la visionneuse, le panneau <b>Visages</b> liste les visages de la photo ouverte.</li>
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
  <li>Les noms et régions existants sont associés automatiquement.</li>
</ul>
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

_TABS = [
    ("Vue d'ensemble",  _TAB_OVERVIEW),
    ("Navigation",      _TAB_NAVIGATION),
    ("Diaporama",       _TAB_SLIDESHOW),
    ("Retouches",       _TAB_EDITING),
    ("Visages",         _TAB_FACES),
    ("Raccourcis",      _TAB_SHORTCUTS),
]

_BROWSER_STYLE = """
QTextBrowser {
    background: #2b2b2b;
    border: none;
    padding: 8px;
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
