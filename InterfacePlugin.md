
# Architecture du système de plugins – Gestionnaire de photos

**Version :** 1.0
**Statut :** Proposition d’architecture

Technologies cibles :

* Python 3.11+
* PySide6
* OpenCV
* JSON

---

# 1. Objectif

Le système de plugins permet d’ajouter dynamiquement des fonctionnalités au gestionnaire de photos sans modifier le cœur de l’application.

Exemples :

* détection de photos papier ;
* extraction automatique ;
* reconnaissance faciale ;
* export Web ;
* renommage ;
* traitement couleur ;
* génération de miniatures ;
* publication.

Objectifs :

* architecture simple ;
* extensibilité ;
* stabilité API ;
* faible couplage ;
* possibilité future d’exécution isolée.

---

# 2. Principes

## Le plugin PEUT

* ajouter des actions ;
* ajouter des boutons dans la barre d’outils ;
* proposer des réglages ;
* traiter des images ;
* produire des fichiers ;
* enrichir les métadonnées.

## Le plugin NE DOIT PAS

* modifier directement la fenêtre principale ;
* accéder aux objets internes ;
* écrire dans des chemins arbitraires ;
* bloquer l’interface graphique.

---

# 3. Architecture globale

```text
Application
    ↓
PluginManager
    ↓
Plugins chargés
    ↓
Actions UI
    ↓
Traitement
    ↓
Résultat
```

Structure :

```text
plugins/
├── plugin_name/
│   ├── manifest.json
│   ├── plugin.py
│   ├── icon.png
│   └── README.md
```

---

# 4. Manifest du plugin

Chaque plugin doit exposer :

`manifest.json`

Exemple :

```json
{
  "id": "photo_export",
  "name": "Export Web",
  "version": "1.0.0",
  "author": "Auteur",
  "entrypoint": "plugin.py",
  "class": "ExportPlugin",
  "icon": "icon.png",
  "toolbar": true
}
```

## Champs

| Champ      | Description        |
| ---------- | ------------------ |
| id         | identifiant unique |
| name       | nom affiché        |
| version    | version            |
| entrypoint | module principal   |
| class      | classe plugin      |
| icon       | icône              |
| toolbar    | ajout toolbar      |

---

# 5. Interface Python obligatoire

```python
class BasePlugin:

    def describe(self):
        pass

    def on_action(
        self,
        action_id,
        context
    ):
        pass

    def process(
        self,
        request
    ):
        pass
```

---

# 6. Méthode `describe()`

Exemple :

```python
{
 "name": "Export Web",

 "description":
 "Exporte des images",

 "toolbar_actions": [

   {
     "id":
       "export_web",

     "label":
       "Exporter",

     "tooltip":
       "Exporter image",

     "icon":
       "icon.png"
   }

 ],

 "parameters": {

   "max_pixels": {

     "type":"int",

     "default":1000000

   }

 }

}
```

---

# 7. Barre d’outils

Au démarrage :

```text
PluginManager
↓
lecture manifest
↓
création QAction
↓
insertion toolbar
```

Cycle :

```text
Toolbar
↓
on_action()
↓
dialog paramètres
↓
process()
```

---

# 8. Contexte transmis

```python
{
 "current_image":
   "path",

 "selected_images":
   [],

 "album":
   "...",

 "app_data":
   "...",

 "dry_run":
   False
}
```

Le plugin ne doit jamais accéder directement au cœur.

---

# 9. Paramètres utilisateur

Déclaration :

```json
{
  "quality": {

    "type": "int",

    "label": "Qualité",

    "default": 85,

    "widget": "slider"
  }
}
```

Widgets générés automatiquement :

| Type   | Widget    |
| ------ | --------- |
| int    | Slider    |
| float  | Spinbox   |
| bool   | Checkbox  |
| string | Textbox   |
| enum   | Combobox  |
| path   | Sélecteur |

---

# 10. Interface avancée (optionnelle)

Plugin :

```python
def create_settings_widget(
    self,
    parent,
    context
):
    return QWidget()
```

Permet :

* aperçu ;
* validation ;
* interaction avancée.

Réservé aux plugins complexes.

---

# 11. Traitement

Entrée :

```python
{
 "image_path":
   "...",

 "parameters":
   {},

 "temporary_dir":
   "..."
}
```

Sortie :

```python
{
 "status":"ok",

 "outputs":[

   {
     "type":"image",

     "path":"..."
   }

 ],

 "metadata":{

   "tags":[]
 },

 "messages":[]
}
```

---

# 12. Résultats supportés

Types :

* Image
* Fichier
* Tag
* Personne
* Export
* Album
* Log

Exemple :

```json
{
 "outputs":[

   {
     "type":"image",

     "path":"crop.jpg"
   }

 ]
}
```

---

# 13. Pipeline

```text
Utilisateur
↓
Toolbar
↓
Plugin
↓
Paramètres
↓
Traitement
↓
Résultat
↓
Refresh UI
```

---

# 14. Gestion des erreurs

Retour :

```json
{
 "status":"error",

 "message":
   "Impossible"
}
```

Règles :

* timeout 60 s ;
* exceptions capturées ;
* journal obligatoire.

---

# 15. Sécurité

Évolution prévue :

```text
V1
→ plugin chargé directement

V2
→ processus séparé

V3
→ sandbox
```

Permissions :

* lecture image ;
* écriture contrôlée.

---

# 16. Exemples de plugins

* PhotoPaperExtractor
* FaceDetector
* WebExporter
* Resize
* DuplicateFinder
* RenameByDate
* ArchiveExporter

---

# 17. Compatibilité future

Prévoir :

```json
{
 "api_version":
 "1.0"
}
```

Règle :

```text
1.x compatible
2.x migration
```

---

# 18. Recommandation finale

Conserver :

* plugins simples ;
* paramètres déclaratifs ;
* UI générée automatiquement ;
* API stable.

Principe :

> Le plugin décrit ce qu’il veut faire.
> L’application décide comment l’exécuter.
