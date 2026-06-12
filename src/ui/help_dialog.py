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
</style>
"""

_TAB_OVERVIEW = _STYLE + """
<h2>PixelPhotoManager</h2>
<p>Gestionnaire de photos et vidéos non destructif pour Windows.
Toutes les retouches sont stockées séparément — l'original n'est jamais modifié.</p>

<h2>Organisation de l'interface</h2>
<ul>
  <li><b>Barre latérale gauche</b> — dossiers surveillés, albums, personnes identifiées.</li>
  <li><b>Grille centrale</b> — vignettes des photos et vidéos du contexte sélectionné.</li>
  <li><b>Visionneuse</b> — ouverte par double-clic sur une vignette ; panneau de retouches à gauche.</li>
  <li><b>Barre du bas</b> — nombre de fichiers, taille, zoom de la grille.</li>
</ul>

<h2>Modes d'affichage de la grille</h2>
<ul>
  <li><b>Mode grille</b> — vignettes uniformes, taille réglable avec le curseur en bas à droite.</li>
  <li><b>Mode chronologie</b> — ruban temporal : 5 rangées de tailles croissantes vers le centre.
    La photo centrale est la photo « courante ». Naviguez avec la molette, les flèches
    ou l'ascenseur à droite.</li>
</ul>

<h2>Formats supportés</h2>
<p>Images : JPEG, PNG, TIFF, WebP, BMP, GIF, HEIC…<br>
Vidéos : MP4, MOV, AVI, MKV, WMV, WebM, M4V, 3GP, FLV, TS, MTS, MPG, MPEG.</p>
"""

_TAB_NAVIGATION = _STYLE + """
<h2>Parcourir la bibliothèque</h2>

<h3>Barre latérale — Dossiers</h3>
<ul>
  <li>Cliquez sur un dossier pour afficher son contenu.</li>
  <li><b>Outils › Dossiers…</b> pour ajouter, supprimer ou forcer le re-scan d'un dossier.</li>
  <li>Les sous-dossiers cachés et <i>Originals</i> sont ignorés automatiquement.</li>
</ul>

<h3>Barre latérale — Albums</h3>
<ul>
  <li><b>Toutes les photos</b> — vue globale de toute la bibliothèque en mode chronologie.</li>
  <li><b>Favoris</b> — photos marquées d'une étoile (⭐) dans la visionneuse.</li>
  <li><b>Vidéos</b> — toutes les vidéos de la bibliothèque.</li>
</ul>

<h3>Barre latérale — Personnes</h3>
<ul>
  <li>Affiche les personnes identifiées via l'analyse des visages.</li>
  <li>Cliquez sur une personne pour voir toutes ses photos.</li>
</ul>

<h3>Recherche</h3>
<ul>
  <li>Saisissez un terme dans la barre de recherche en haut (<kbd>Ctrl</kbd>+<kbd>F</kbd>).</li>
  <li>La recherche porte sur le nom de fichier et les données EXIF.</li>
</ul>

<h3>Navigation dans la visionneuse</h3>
<ul>
  <li>Flèches <kbd>◀</kbd> <kbd>▶</kbd> ou molette : photo précédente / suivante.</li>
  <li><kbd>Échap</kbd> ou bouton ✕ : retour à la grille.</li>
  <li><kbd>F11</kbd> ou <b>Affichage › Plein écran</b> : basculer en plein écran.</li>
  <li>Double-clic sur la visionneuse : zoom 1:1 / ajustement automatique.</li>
</ul>

<h3>Supprimer des fichiers</h3>
<ul>
  <li>Dans la grille ou le mode chronologie : sélectionnez puis appuyez sur <kbd>Suppr</kbd>.</li>
  <li>En mode chronologie sans sélection : <kbd>Suppr</kbd> efface la photo centrale.</li>
  <li>Dans la visionneuse : bouton 🗑 dans la barre d'outils.</li>
  <li>Une confirmation est demandée (peut être désactivée dans Paramètres).</li>
</ul>
"""

_TAB_EDITING = _STYLE + """
<h2>Retouches non destructives</h2>
<p>Les ajustements n'écrivent jamais dans le fichier d'origine. Ils sont stockés dans
une base SQLite séparée et appliqués à la volée à l'affichage et à l'export.</p>

<h3>Ajustements disponibles</h3>
<ul>
  <li><b>Luminosité</b> — éclaircit ou assombrit l'image globalement.</li>
  <li><b>Contraste</b> — élargit ou réduit la plage tonale.</li>
  <li><b>Saturation</b> — renforce ou atténue les couleurs.</li>
  <li><b>Netteté</b> — accentue ou adoucit les contours.</li>
  <li><b>Teinte</b> — décale la teinte globale (virage colorimétrique).</li>
  <li><b>Recadrage</b> — sélection libre de la zone à conserver.</li>
  <li><b>Correction des yeux rouges</b> — détecte et corrige automatiquement.</li>
</ul>

<h3>Workflow</h3>
<ul>
  <li>Ajustez les curseurs : un aperçu en temps réel s'affiche (≤ 60 ms).</li>
  <li>Cliquez <b>Appliquer</b> pour enregistrer les modifications.</li>
  <li><b>Annuler / Rétablir</b> (<kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd>) :
      l'historique est persistant entre les sessions.</li>
  <li><b>Réinitialiser</b> : revient à l'image originale sans supprimer l'historique.</li>
</ul>

<h3>Export</h3>
<ul>
  <li><b>Enregistrer une copie</b> — exporte l'image avec les retouches appliquées
      dans un nouveau fichier, sans toucher à l'original.</li>
</ul>

<h3>EXIF</h3>
<ul>
  <li>Le panneau EXIF (onglet dans la visionneuse) affiche toutes les métadonnées.</li>
  <li><b>Outils › Synchroniser les dates EXIF</b> — corrige les dates de prise de vue
      en lot à partir des données EXIF ou du nom de fichier.</li>
</ul>
"""

_TAB_FACES = _STYLE + """
<h2>Détection et reconnaissance des visages</h2>

<h3>Analyser les visages</h3>
<ul>
  <li><b>Visages › Analyser les visages</b> — lance la détection en arrière-plan
      sur toutes les photos non encore traitées.</li>
  <li>L'analyse est progressive et non bloquante ; l'application reste utilisable.</li>
  <li>Les résultats sont enregistrés dans la base de données locale.</li>
</ul>

<h3>Identifier les personnes</h3>
<ul>
  <li><b>Visages › Identifier les personnes…</b> — ouvre la vue des groupes de visages.</li>
  <li>Les visages similaires sont regroupés automatiquement par <i>clustering</i>.</li>
  <li>Cliquez sur un groupe pour lui attribuer un nom.</li>
  <li>Les suggestions de noms s'affichent en bleu sous chaque groupe
      si un visage ressemble à une personne déjà nommée.</li>
</ul>

<h3>Gérer les personnes</h3>
<ul>
  <li>Une fois nommés, les groupes apparaissent dans la barre latérale sous <b>Personnes</b>.</li>
  <li>Cliquez sur une personne pour voir toutes ses photos.</li>
  <li>Fusionner plusieurs groupes en leur donnant le même nom.</li>
</ul>

<h3>Import Picasa</h3>
<ul>
  <li><b>Visages › Importer depuis Picasa…</b> — importe les annotations de visages
      depuis les fichiers <code>.picasa.ini</code> de Google Picasa.</li>
</ul>
"""

_TAB_SHORTCUTS = _STYLE + """
<h2>Raccourcis clavier</h2>

<h3>Global</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>F</kbd></td><td>Mettre le focus sur la barre de recherche</td></tr>
  <tr><td><kbd>F11</kbd></td><td>Basculer en plein écran</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Quitter la visionneuse / plein écran</td></tr>
</table>

<h3>Grille de photos</h3>
<table>
  <tr><th>Raccourci</th><th>Action</th></tr>
  <tr><td><kbd>Suppr</kbd></td><td>Supprimer les photos sélectionnées</td></tr>
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
  <tr><td><kbd>◀</kbd> <kbd>▶</kbd> ou molette</td><td>Photo précédente / suivante</td></tr>
  <tr><td><kbd>Ctrl</kbd>+Molette</td><td>Zoom avant / arrière</td></tr>
  <tr><td>Double-clic</td><td>Zoom 1:1 ↔ ajustement automatique</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Z</kbd></td><td>Annuler la dernière retouche</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Y</kbd></td><td>Rétablir la retouche annulée</td></tr>
  <tr><td><kbd>Échap</kbd></td><td>Retour à la grille</td></tr>
</table>
"""

_TABS = [
    ("Vue d'ensemble",  _TAB_OVERVIEW),
    ("Navigation",      _TAB_NAVIGATION),
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
        self.resize(720, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8,8)
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
