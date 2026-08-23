# Plugin system architecture – Photo manager

**Version:** 1.0
**Status:** Architecture proposal

Target technologies:

* Python 3.11+
* PySide6
* OpenCV
* JSON

---

# 1. Purpose

The plugin system makes it possible to add features to the photo manager dynamically, without modifying the core of the application.

Examples:

* paper-photo detection;
* automatic extraction;
* face recognition;
* web export;
* renaming;
* colour processing;
* thumbnail generation;
* publishing.

Goals:

* simple architecture;
* extensibility;
* API stability;
* loose coupling;
* the future option of isolated execution.

---

# 2. Principles

## A plugin MAY

* add actions;
* add buttons to the toolbar;
* offer settings;
* process images;
* produce files;
* enrich metadata.

## A plugin MUST NOT

* modify the main window directly;
* access internal objects;
* write to arbitrary paths;
* block the graphical interface.

---

# 3. Overall architecture

```text
Application
    ↓
PluginManager
    ↓
Loaded plugins
    ↓
UI actions
    ↓
Processing
    ↓
Result
```

Structure:

```text
plugins/
├── plugin_name/
│   ├── manifest.json
│   ├── plugin.py
│   ├── icon.png
│   └── README.md
```

---

# 4. Plugin manifest

Every plugin must expose:

`manifest.json`

Example:

```json
{
  "id": "photo_export",
  "name": "Export Web",
  "version": "1.0.0",
  "author": "Author",
  "entrypoint": "plugin.py",
  "class": "ExportPlugin",
  "icon": "icon.png",
  "toolbar": true
}
```

## Fields

| Field      | Description       |
| ---------- | ----------------- |
| id         | unique identifier |
| name       | displayed name    |
| version    | version           |
| entrypoint | main module       |
| class      | plugin class      |
| icon       | icon              |
| toolbar    | toolbar entry     |

---

# 5. Mandatory Python interface

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

# 6. The `describe()` method

Example:

```python
{
 "name": "Export Web",

 "description":
 "Exports images",

 "toolbar_actions": [

   {
     "id":
       "export_web",

     "label":
       "Export",

     "tooltip":
       "Export image",

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

# 7. Toolbar

At startup:

```text
PluginManager
↓
read the manifest
↓
create a QAction
↓
insert into the toolbar
```

Cycle:

```text
Toolbar
↓
on_action()
↓
settings dialog
↓
process()
```

---

# 8. Context passed in

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

A plugin must never reach into the core directly.

---

# 9. User parameters

Declaration:

```json
{
  "quality": {

    "type": "int",

    "label": "Quality",

    "default": 85,

    "widget": "slider"
  }
}
```

Widgets generated automatically:

| Type   | Widget    |
| ------ | --------- |
| int    | Slider    |
| float  | Spinbox   |
| bool   | Checkbox  |
| string | Textbox   |
| enum   | Combobox  |
| path   | Picker    |

---

# 10. Advanced interface (optional)

Plugin:

```python
def create_settings_widget(
    self,
    parent,
    context
):
    return QWidget()
```

Allows:

* preview;
* validation;
* advanced interaction.

Reserved for complex plugins.

---

# 11. Processing

Input:

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

Output:

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

# 12. Supported results

Types:

* Image
* File
* Tag
* Person
* Export
* Album
* Log

Example:

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
User
↓
Toolbar
↓
Plugin
↓
Parameters
↓
Processing
↓
Result
↓
Refresh UI
```

---

# 14. Error handling

Return value:

```json
{
 "status":"error",

 "message":
   "Not possible"
}
```

Rules:

* 60 s timeout;
* exceptions caught;
* logging mandatory.

---

# 15. Security

Planned evolution:

```text
V1
→ plugin loaded directly

V2
→ separate process

V3
→ sandbox
```

Permissions:

* image reading;
* controlled writing.

---

# 16. Example plugins

* PhotoPaperExtractor
* FaceDetector
* WebExporter
* Resize
* DuplicateFinder
* RenameByDate
* ArchiveExporter

---

# 17. Future compatibility

Plan for:

```json
{
 "api_version":
 "1.0"
}
```

Rule:

```text
1.x compatible
2.x migration
```

---

# 18. Final recommendation

Keep:

* plugins simple;
* parameters declarative;
* the UI generated automatically;
* the API stable.

Principle:

> The plugin describes what it wants to do.
> The application decides how to run it.
