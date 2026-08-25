# PixelPhotoManager

A non-destructive desktop photo manager for Windows, built with Python and PySide6.

![Python](https://img.shields.io/badge/Python-3.11-blue)
![PySide6](https://img.shields.io/badge/PySide6-6.x-green)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

- **Non-destructive editing** — original files are never modified; all adjustments are stored in a separate SQLite database and applied on the fly
- **Video support** — scan, index, and browse video files (MP4, MOV, AVI, MKV, WMV, WebM, M4V and more); thumbnails extracted from first frames, playback in the system player
- **Folder management** — watch multiple folders, create/rename/move subfolders from within the app, drag photos between folders; **Tools › Folders…** shows scan status and lets you force-rescan any folder
- **Fast thumbnail grid** — three-level cache (RAM LRU → SQLite → on-demand generation in background threads), four zoom levels
- **Full-screen viewer** — smooth zoom, pan, previous/next navigation (stops at first/last, no wrap-around)
- **Photo editing**
  - Tonal corrections: brightness, contrast, saturation, gamma, sharpness, noise reduction
  - Colour treatment: black & white with per-channel R/G/B mixing sliders
  - Geometry: ±90° rotation, straighten (−10° to +10°), horizontal/vertical flip
  - Crop with free-form or locked aspect ratios (10×15, 13×18 landscape/portrait)
- **Persistent undo/redo** — up to 50 edit states per photo, restored across sessions
- **EXIF panel** — toggle with `I` in the viewer; shows camera, lens, exposure, GPS coordinates
- **Map localization** — right-click a photo in the viewer → "Locate on the map" opens OpenStreetMap at the photo's GPS position
- **Slideshow** — **View › Slideshow** or `F5`; configurable interval
- **Face recognition** — detect, cluster and name people; import face annotations from Picasa
- **Albums & favorites** — organize photos across folders
- **Full-text search** — filter by filename or camera model
- **Plugin system** — extend the app without modifying core code

---

## Tech stack

| Library | Role |
|---|---|
| [PySide6](https://doc.qt.io/qtforpython/) | UI framework (Qt 6) |
| [Pillow](https://python-pillow.org/) | Image loading, processing, EXIF reading |
| [OpenCV](https://opencv.org/) | Advanced image operations, video thumbnail extraction |
| [piexif](https://github.com/hMatoba/Piexif) | EXIF metadata writing |
| [imagehash](https://github.com/JohannesBuchner/imagehash) | Perceptual hashing (duplicate detection) |
| [folium](https://python-visualization.github.io/folium/) | GPS map view |
| [reportlab](https://www.reportlab.com/) | PDF export |
| SQLite (stdlib) | Catalog, thumbnail cache, edit history |

---

## Getting started

**Requirements:** Python 3.11, Windows 10/11

```powershell
# Clone the repository
git clone https://github.com/Christian73/PixelPhotoManager.git
cd PixelPhotoManager

# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install dependencies
.venv\Scripts\pip.exe install -r requirements.txt

# Run the application
.venv\Scripts\python.exe main.py
```

On first launch, a setup dialog asks you to choose one or more photo folders to watch.

---

## Build a standalone Windows EXE

```powershell
.\build.ps1
```

This produces a self-contained folder in `dist\PixelPhotoManager\` (~350 MB) that runs on any Windows PC without Python installed. Zip the folder for distribution.

Requirements: PyInstaller must be installed in the venv (`pip install pyinstaller`).

---

## Project structure

```
src/
├── core/        Event bus, config, plugin manager, data models
├── library/     Folder scanner, SQLite catalog, thumbnail cache, EXIF/video reader
├── ui/          Main window, thumbnail grid, photo viewer, edit panel, sidebar,
│                folder manager dialog, EXIF panel
├── processing/  Non-destructive image adjustments, geometry, edit database
└── faces/       Face detection, clustering, Picasa import (optional, heavy dependencies)
plugins/         External user plugins (one folder per plugin)
```

Application data is stored in `%LOCALAPPDATA%\PixelPhotoManager\`:

| File | Contents |
|---|---|
| `catalog.db` | Photo index (paths, EXIF, metadata, media type) |
| `thumbnails.db` | Generated thumbnail cache (images and video frames) |
| `edits.db` | All edits and their history |
| `config.json` | Watched folders and UI preferences |

---

## Keyboard shortcuts

| Context | Key | Action |
|---|---|---|
| Grid | `Ctrl+F` | Search |
| Grid | `Ctrl+A` | Select all |
| Grid | `Del` | Delete selected |
| Grid | `F9` | Toggle sidebar |
| Grid | `F11` | Fullscreen |
| Grid/Viewer | `F5` | Start slideshow |
| Viewer | `← / →` | Previous / next photo |
| Viewer | `I` | Toggle EXIF panel |
| Viewer | `0` | Fit to window |
| Viewer | `1` | Zoom 100% |
| Viewer | `Esc` | Back to grid |
| Crop mode | `Enter` | Confirm crop |
| Crop mode | `Esc` | Cancel crop |
| Edit panel | `Ctrl+Z` | Undo |
| Edit panel | `Ctrl+Y` | Redo |

---

## Documentation

- [User guide](UserGuide.md) — full user guide
- [Developer guide](Guide_Developpeur.md) — architecture, database schemas, threading model, packaging
- [Design document](DocumentDeConception.md) — functional and technical specification
- [Plugin interface](InterfacePlugin.md) — plugin API reference

---

## Plugin development

A plugin is a folder inside `plugins/` containing a `plugin.json` manifest and a `plugin.py` file with a class that inherits from `BasePlugin`:

```python
from src.core.base_plugin import BasePlugin

class MyPlugin(BasePlugin):
    def activate(self) -> None:
        self.bus.on("library.photo_selected", self._on_photo)

    def deactivate(self) -> None:
        self.bus.off("library.photo_selected", self._on_photo)
```

See [Guide_Developpeur.md](Guide_Developpeur.md) for the full plugin API.

---

## License

MIT — see [LICENSE](LICENSE) for details.
