# Delivery notes — PixelPhotoManager

Cumulative history since the project was created, most recent version first.

| Version | Date | Commits | Installer |
|---------|------|---------|-----------|
| 1.2.0 | 25 August 2026 | 43 | `PixelPhotoManager-1.2.0-x64.msi` |
| 1.1.0 | 6 August 2026 | 109 | `PixelPhotoManager-1.1.0-x64.msi` |
| 1.0.0 | 6 July 2026 | 113 (creation → 1.0.0) | `PixelPhotoManager-1.0.0-x64.msi` |

Versions 1.0.1 and 1.0.2 were never shipped (internal bumps); their contents are
included in 1.1.0.

---

## Version 1.2.0 — 25 August 2026

Previous version: **1.1.0** (6 August 2026). 43 commits.

A version with no new schema and almost no new screen: it makes the whole
product — application, built-in help and installer — speak **English, French
and German**, and hardens the test suite around what was until now the least
covered code.

### What's new

**Languages**
- The interface, the built-in help and the installer are available in
  **English, French and German**.
- **English is now the source language.** A string that has escaped
  translation therefore falls back to correct English rather than to French,
  which was unreadable for most users. About 1,235 strings are marked across
  53 modules.
- The language is a setting (**Settings › Language**), applied on restart.
- **Flag button** at the far right of the top bar: the language is the one
  setting a user must be able to reach *without being able to read the
  interface*, hence the deliberate duplication with the Settings entry. The
  flags are drawn, not emoji — no font shipped with Windows carries the
  regional-indicator flags.
- The standard Qt dialogs are translated too: without that, a German
  interface kept its "OK / Cancel" buttons in English.
- Built-in help translated page by page (9 pages per language), with a
  fallback to English per page: a page not yet translated no longer costs the
  whole help of that language.
- Units and date formats follow the language (`B/kB/MB` vs `o/Ko/Mo`, date
  order), instead of being fixed constants.
- A fresh installation starts **in the language the installer displayed**; an
  existing installation keeps the language its user chose.

**Installer**
- The MSI speaks the same three languages and **Windows picks the right one
  by itself**, with no bootstrapper and no option to tick: the French and
  German packages are embedded in the English one as language transforms, and
  Windows Installer matches them against the machine language. An unlisted
  language gets the English package.
- The licence is shown in the language of the installation.
- The **final screen offers to start the application** (ticked by default, on
  a fresh installation only). It starts as the user, never with the elevated
  token of the installation — otherwise the application would create its data
  folder in the administrator's profile and the first real start would find an
  empty library.

**Editing**
- The **Vignette** tool now opens at 50% intensity on a photo that has none.
  It used to open at zero, so on a strictly unchanged image: you had to move
  the slider to see the slightest effect. Cancelling still returns to no
  vignette, and an already adjusted vignette is not overwritten.

### Fixes

- **A re-indexing request could be lost for good.** After a forced rescan, the
  new photos stayed unindexed indefinitely if a face indexing pass was already
  running: the request was flagged as pending, and nothing consumed that flag
  afterwards. Found by an end-to-end scenario.
- The installer displayed **version 1.0** since 1.0: the number is painted into
  the welcome bitmap, which was only regenerated when it was missing. It is now
  rebuilt at every build from the real version.
- Four strings had escaped the whole translation campaign and stayed French,
  invisibly: the sidebar "Identify…" button, the folder-deletion confirmation
  button, the title of the duplicates report, and the "Del" key concatenated
  into the grid context menus.
- The "Mirror H / V" buttons were hard-coded, and the undo stack showed
  "Undo Miroir H" in the middle of an English or German interface.
- Graphics card memory ("8 Go") and thread journal sizes ("Ko / Mo") stayed
  French whatever the interface.
- The Editing help page described an **Apply** button that no longer exists and
  an export that has since been split into two distinct paths; the Duplicates
  page still presented detection as manual.
- The label and tooltip of "Deleted corrupted files…" announced a permanent
  deletion, when those files go to the Windows recycle bin like every other
  one.

### Installer and packaging

- Installer payload inventory regenerated: the CUDA libraries of
  `onnxruntime-gpu` (`cudart64_12`, `cublas64_12`, `cublasLt64_12`,
  `cufft64_11`) are gone, the HEIC (`libheif`, `libde265`) and RAW (`raw_r`)
  libraries added, and the OpenCV video library moved from 4.13 to 5.00.
- The packaging spec picks up the compiled catalogs and the help folders
  through globs: adding a language needs no change there.

### Automatic migrations on first start

**None.** No database schema changed in this version — `catalog.db`,
`faces.db`, `edits.db` and `thumbnails.db` are read exactly as 1.1.0 left them,
and going back to 1.1.0 requires nothing. The only new setting, the interface
language, is written to `config.json` at the first save.

### Quality

Two coverage campaigns on the least covered code: the MainWindow controllers
and the viewer canvas first (`main_window_duplicates.py` and
`main_window_faces.py` from 0 to 100%, `viewer_canvas.py` 28 → 93%,
`viewer_pixmaps.py` 40 → 97%), then the MainWindow slots themselves, tested
without ever instantiating the window (`main_window.py` 19 → 75% on the unit
run, 100% once the end-to-end runs are merged in). **Base coverage 85.4%**
(gating threshold raised from 80 to 85), **combined coverage 90.0%**, across
2,351 unit and interface test functions and 14 end-to-end scenarios.

Two lessons recorded in the process: the coverage history had until then been
logging *combined* figures against a threshold that only ever measured the
*base* run — the two are now logged separately — and three real traps of the
end-to-end harness were fixed, unrelated to the labels (a popup button living
in its own top-level window, a focus taken through a renamed list item, and a
"Delete" confirmation not to be confused with the "Remove" of the next step).

Comments and docstrings across the whole codebase were translated to English
at the same time as the strings, so that a French literal in `src/` is now, by
construction, a detectable defect.

---

## Version 1.1.0 — 6 August 2026

Previous version: **1.0.0** (6 July 2026). 109 commits.

### What's new

**Organisation and search**
- ★ 1-to-5 star ratings, with a "By rating" album collapsible by level.
- Editable keywords on photos: dialog, filter, drop-down list in the viewer,
  submenu and collapsible section in the sidebar.
- Multi-criteria advanced search dialog.
- Search bar in the Help / About window.
- Automatic update check through GitHub releases.

**Formats**
- Support for **RAW** photos (CR2, NEF, ARW, DNG, ORF, RW2) and **HEIC/HEIF**
  (iPhone).
- DVD copies: `VIDEO_TS` folders detected, `.VOB` files catalogued as videos,
  opening through an external player.
- The external-application icons in the viewer are filtered by media type (a
  video application no longer shows up on a still photo).

**Editing**
- **Decorative frames**: 13 computed patterns (plain surround, simple, double,
  gilded baroque, egg-and-dart, Greek key, art deco, carved wood, vine leaves,
  roses, flowers, metallic, reflections), a gallery of previews on the current
  photo, adjustable width. The plain surround accepts a second frame with
  ironwork (scrolls, running scrolls, twisted bar, studs). The foliage patterns
  spill over the photo in places, drop shadow included.
- Annotation layer and harmonisation of the editing tools.
- `Ctrl+S` shortcut to save the edited image.
- "Reset" can be reversed with "Restore", undo stack included.

**Duplicates**
- **Continuous, incremental** detection, started after every scan, with no
  button and no completion report; **Tools › Duplicate status…** gives a
  snapshot.
- Dedicated "Duplicates" grid, movable popup in the viewer, explicit empty
  state.
- Full handling of corrupted files: detection, repair, deletion, persistent
  history.
- Tier 1 and Tier 2 comparisons parallelised.

**Faces**
- ✓ / ✗ buttons overlaid on thumbnails to accept or reject a suggestion.
- **Faces › Search for similar faces…** entry, restarted automatically after
  each identification.
- Cancel button and movable popup while the groups are being analysed; partial
  clustering is kept.
- Revised confidence tiers: automatic assignment ≥ 70%, held for verification
  ≥ 55%, "Probably" / "Maybe" labels below that.
- Number of identified people shown in the sidebar.
- `Ctrl+A` selects every confirmed face of a person.

**Slideshow and viewer**
- No more screen saver or screen blanking during playback, pause included.
- "Photo n of N" counter in the navigation bar.
- Going back to the grid re-highlights the last photo shown and scrolls it into
  view.
- Folder of the photo shown in the grid.

**Deletion and data safety**
- Every file deletion now goes through the **Windows recycle bin**; on failure
  the user is told, never a silent permanent deletion.

**Performance**
- CPU throttle setting for background processing: **Settings › Performance**,
  three levels, "Frugal" by default, relaxed when the window is not in the
  foreground.
- IDLE system priority for background threads and processes.
- Immediate visual feedback in the grid, the viewer and face assignment.
- Similar-face search through a matrix product (11 M pairwise comparisons
  before, taking several minutes).
- Faces panel: only the thumbnails whose framing changed are decoded again.
- Frame thumbnails render 2.5× faster.

### Fixes

- Radio buttons invisible in the dark theme (dot the same shade as the
  background).
- Long menu label running underneath its shortcut.
- Thumbnails not reflecting the edits of a photo outside the visible area.
- Rotation lost when a face re-detection was already running.
- Face suggestions permanently stuck on a partially identified group.
- Duplicate detection starting over at every restart; groups reduced to a single
  copy not dissolved; a file deleted during the scan classified as "corrupted".
- Manual navigation restarting the advance of a paused slideshow.
- Edit panel compressible to the point of making its second column
  unreachable.
- Sidebar crash on a folder with several hundred subfolders.
- Ghost windows after dismissing several duplicate groups.
- Orphan `album_photos` entries purged; photo/video counts per folder.
- The grid no longer offers "Delete the file" in album view.
- Rating and favourite icons in the viewer invisible or inconsistent.

### Installer and packaging

- MSI renamed **`PixelPhotoManager-X.Y.Z-x64.msi`** (previously
  `PixelPhotoManager-Setup-<version>.msi`).
- Companion script `Installer-avec-log.cmd` (`msiexec /L*v` log) generated next
  to the MSI.

### Automatic migrations on first start

`edits.db`: 13 frame columns. `thumbnails.db`: edit fingerprint (`edit_sig`) to
regenerate stale thumbnails. `faces.db`: purge of leftover suggestions. These
migrations cannot be rolled back to 1.0.0 — back up
`%LOCALAPPDATA%\PixelPhotoManager\` before upgrading to keep a way back.

### Quality

1,646 unit and interface tests, 14 end-to-end scenarios (pywinauto), 80.4%
combined coverage (gating threshold raised from 79 to 80%). The large modules
(`main_window`, `photo_viewer`, `edit_panel`) were split into dedicated modules.

---

## Version 1.0.0 — 6 July 2026

First shipped version. 113 commits since the project was created (3 June 2026).

### Foundations

- Windows desktop application in Python 3.11 / PySide6, central event bus,
  plugin system (image processing and views).
- SQLite catalog, three-level thumbnail cache (RAM, SQLite, background
  generation), data in `%LOCALAPPDATA%\PixelPhotoManager\`.
- Scanning of watched folders, automatic cleanup of stale entries, folder
  manager (**Tools › Folders…**).
- MIT licence, README, user and developer guides, built-in help.

### Library and browsing

- Virtualised grid: smooth startup with 67,000 photos and more than 1,000
  folders.
- Sidebar: folder tree, albums, favourites, timeline.
- Albums: creation from a selection, deletion, removal of photos.
- Viewer: detailed EXIF panel, GPS and map, duplicate badge, context menus
  aligned between grid and viewer.
- Slideshow with a Ken Burns effect.
- Video: 13 supported extensions, thumbnails extracted with OpenCV, "Videos"
  album, configurable external player.

### Editing and export

- **Non-destructive** edits stored in SQLite, applied on display and on export,
  undo history persistent across sessions.
- Cropping, straightening with an alignment grid, mirrors, rotation, red-eye
  correction.
- Saving the edited image, renaming from the grid, export with size and quality
  presets, opening the export folder when finished.

### Face recognition

- Detection and embedding with **InsightFace / buffalo_l** (after an initial
  DeepFace implementation), HDBSCAN clustering.
- Faces panel: identification, rotation, navigation, undo stack, ignored faces,
  sidebar filter, multi-rotation detection.
- Per-person view, group cards, multi-selection, association of several groups
  with one right-click, merging and renaming of people.
- Import of **Picasa** annotations and coexistence with ArcFace
  identifications.
- Adaptive size threshold, proportional to the resolution of the photo.

### Packaging

- Standalone PyInstaller executable, icon, splash screen, unified menu +
  toolbar.
- WiX MSI installer (WixUI_InstallDir, custom banner), release infrastructure.
- Packaging fixes: `sklearn` / `hdbscan` missing from the executable, `buffalo_l`
  model pack embedded (face detection inoperative without Internet access),
  `multiprocessing.freeze_support()`.

### Stability and performance

- Fixes for memory saturation and leaks (zombie threads, avatar cache, embedding
  allocations), for UI freezes (merging people, computing groups, edit previews)
  and for hangs on video albums.
- Face detection on non-ASCII paths, CPU throttling of the scan and of indexing.
- Test suite (unit, interface, end-to-end) and coverage measurement put in
  place.
