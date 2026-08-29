# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project

**PixelPhotoManager** — a Windows desktop photo manager. Python 3.11, PySide6. See `DocumentDeConception.md` for the full specification.

---

## Environment

```powershell
# Activate the VENV (always use the venv interpreter)
.venv\Scripts\Activate.ps1

# Run the application
.venv\Scripts\python.exe main.py

# Install the dependencies
.venv\Scripts\pip.exe install -r requirements.txt

# Run the tests
.venv\Scripts\python.exe -m pytest tests/

# Run one specific test
.venv\Scripts\python.exe -m pytest tests/test_thumbnail_cache.py::TestThumbnailCache::test_lru_eviction -v

# Coverage (fail_under ratchet in .coveragerc — raise it, never lower it)
# A dedicated COVERAGE_FILE, so that it can later be combined with the e2e coverage (see below).
# A trap learned the hard way: a name such as ".coverage.base" looks harmless, but
# "coverage combine" with no argument (called right afterwards to merge the per-process files
# of the e2e step) merges BY DEFAULT every file matching the pattern "<COVERAGE_FILE>.*" —
# that is ".coverage.*", since COVERAGE_FILE is ".coverage" by default at that point.
# ".coverage.base" matches that pattern and is therefore swallowed one step too early
# (silently — no error until the final step, which fails with "Couldn't combine from
# non-existent path"). Hence a name that does NOT start with ".coverage." for the
# intermediate files that must be preserved.
$env:COVERAGE_FILE='coverage_base.dat'; .venv\Scripts\python.exe -m pytest tests/ --cov=src
Remove-Item Env:\COVERAGE_FILE

# e2e scenarios with coverage of the UI code (the application runs under coverage,
# .coverage.* files written at the root — merge them with coverage combine)
$env:PPM_E2E_COVERAGE='1'; .venv\Scripts\python.exe -m pytest tests/e2e -m e2e
.venv\Scripts\python.exe -m coverage combine; .venv\Scripts\python.exe -m coverage report
Copy-Item .coverage coverage_e2e.dat -Force  # preserves the e2e-only result under a dedicated name

# Combined analysis (base + e2e): merges the two preserved series above into a single total
$env:COVERAGE_FILE='coverage_combined.dat'; .venv\Scripts\python.exe -m coverage combine --keep coverage_base.dat coverage_e2e.dat
.venv\Scripts\python.exe -m coverage report --data-file=coverage_combined.dat
Remove-Item Env:\COVERAGE_FILE

# Windows packaging (standalone executable)
.venv\Scripts\pyinstaller.exe pixelphotomanager.spec
```

### Testing rules

- **Every bugfix comes with its regression test** (see `test_signal_object_cross_thread.py`, `test_duplicate_detector.py::test_tiff_never_reaches_cv2_imread` for the expected style).
- **coverage/QThread trap**: coverage.py does not trace code executed inside a real `QThread.start()` (a native Qt thread, outside `sys.settrace` in Python 3.11). In tests, call `thread.run()` synchronously (signals are emitted through direct connections and the code is traced); keep one or two real `.start()` + `qtbot.waitSignal` per module for the cross-thread plumbing.
- **Disposable-QThread trap**: a real `QThread` with no parent self-destructing through `deleteLater` while its OS thread is terminating triggers a Qt fail-fast (0xC0000409) as soon as pytest-qt's event loop processes the destruction — use a non-QThread stub (see `_InertUpdateThread` in `test_dialogs_smoke.py`).
- The `fail_under` ratchet (`.coveragerc`) applies to every `--cov` run: raise it after each campaign that durably increases coverage.

---

## Architecture

```
src/
├── core/          Event bus, config, plugin manager
├── library/       Folder scanner, SQLite catalog, thumbnail cache, EXIF
│                  exif_reader.py : ExifReader + VideoMetadataReader + VIDEO_EXT
├── ui/            Main window, grid, viewer, sidebar, panels
│                  folder_manager_dialog.py : the Tools › Folders… dialog
│                  exif_panel.py            : EXIF panel in the viewer
│                  theme.py                 : global dark stylesheet
│                    (`app_stylesheet(check_icon)`, applied by main() to the
│                    QApplication). Every control whose indicator is drawn by
│                    the style — checkbox, radio button… — must have its
│                    `::indicator` rules there: as soon as an application
│                    stylesheet exists, Qt switches to QStyleSheetStyle and a
│                    sub-control with no rule is rendered with default colours
│                    invisible against the #1e1e1e background (real case: the
│                    dot of a checked QRadioButton strictly identical to the
│                    background). The local copies of `_RADIO_STYLE`
│                    (display_order_dialog, people_panel, export_dialogs,
│                    reset_faces_dialog) predate this and still take
│                    precedence — do not create a new one. Test:
│                    tests/gui_widgets/test_theme.py measures the real
│                    contrast of the rendering (grab()), not the presence of
│                    a string.
│                  2026-07 split (the large files delegate to dedicated
│                  modules; the historical names are re-exported from the
│                  original module):
│                  - main_window.py  → background_workers.py (7 QThreads),
│                    export_dialogs.py, reset_faces_dialog.py, duplicates_popup.py,
│                    ui_utils.py (fmt_size, menu widths — see below)
│                  - photo_viewer.py → viewer_canvas.py (_Canvas + _InlineTextEdit),
│                    viewer_pixmaps.py (_build_pixmap & co., used by slideshow)
│                  - edit_panel.py   → edit_sliders.py (MarkedSlider/EditSlider),
│                    treatment_dialogs.py (_TREATMENTS + dialogs), edit_icons.py
│                  Importing the classes from the original module stays valid
│                  (re-exports); new code must import the dedicated module.
├── processing/    Image editing (non-destructive)
├── faces/         Detection, recognition, clustering (+ Picasa import)
└── plugins/       Built-in plugins (map, AI restoration, etc.)
plugins/           User plugins (outside src/)
```

`src/library/fs_utils.py::is_hidden_path()` is the single implementation of the
"hidden path" test (Windows attribute or dot prefix) — the scanner, folder_watcher
and folder_manager_dialog alias it as `_is_hidden`; do not create another copy.

### Event bus — the central piece

`src/core/event_bus.py` exposes a global `bus` instance. Every component communicates exclusively through this bus. Never call a method of another component directly if an event can do the job.

```python
from src.core.event_bus import bus
bus.on('library.photo_selected', self.handler)
bus.emit('library.photo_selected', photo=photo_info)
```

Events are defined in the docstring of `EventBus`. Every new event must be documented there.

### Video support

`src/library/exif_reader.py` exposes:
- `VIDEO_EXT` — the set of the 14 supported video extensions: `.mp4 .mov .avi .mkv .wmv .webm .m4v .3gp .flv .ts .mts .mpg .mpeg .vob`
  (`.vob` = the stream of a DVD copy, `VIDEO_TS` folder — literally copied into
  `src/library/duplicate_detector.py::_VIDEO_EXT`, to be kept in sync)
- `VideoMetadataReader.read(path)` — reads resolution/fps/duration through `cv2.VideoCapture`, date = `os.stat(path).st_mtime`

`src/core/models.py` — `PhotoInfo` has two extra fields:
- `media_type: str = "image"` — `"image"` or `"video"`
- `duration: float = 0.0` — duration in seconds (videos only)

`catalog.db` has the `media_type` and `duration` columns (migrated automatically at startup by `_migrate_video_fields()`).

The edit panel is **skipped for videos**: `main_window.show_viewer()` and `_navigate_photo()` check `photo.media_type == "video"` and keep `_left_stack` at index 0 (sidebar) instead of 1 (edit panel).

### Image decoding — RAW and HEIC/HEIF

`src/library/image_loader.py::open_image(path)` is the project's **single image
decoding point** — every site that used to open a file directly through
`PIL.Image.open(path)` (thumbnail_cache, viewer_pixmaps, exif_reader,
faces/detector) must go through it instead. Do not reintroduce a direct
`Image.open()` call on a user file path (an `Image.open(io.BytesIO(...))` on
already-decoded bytes remains legitimate, e.g. `viewer_pixmaps._apply_edit_to_base`).

- `RAW_EXT = {.cr2 .nef .arw .dng .orf .rw2}`, `is_raw_available()` (a hidden,
  expensive `rawpy` import). RAW is decoded through the JPEG preview embedded by
  the camera (`rawpy.imread(path).extract_thumb()`, fast, keeps the original
  EXIF) with a fallback on `raw.postprocess(half_size=True)` (reduced
  demosaicing, slower) when no usable preview exists — exporting a RAW therefore
  yields the resolution of that preview, a documented product limitation.
- HEIC/HEIF: `register_heif_opener()` from `pillow-heif` is registered at
  **module level** (not inside a function) — the `ProcessPoolExecutor` workers
  (spawn, `faces/detector.py`) re-import the modules without ever going through
  `main()`, so a lazy registration would never run for them. Once registered,
  `Image.open()` reads HEIC transparently everywhere (including outside
  `image_loader`, e.g. the PIL fallbacks of `duplicate_detector.py`) — no
  dedicated exclusion is needed for HEIC, unlike RAW below.
- `safe_temp_suffix(path)` — to be used everywhere a temporary file is written
  through `PIL.Image.save()` from an image decoded from a RAW or HEIC path: it
  forces `.jpg` (RAW can never be saved back by PIL, HEIC not necessarily
  depending on the `pillow-heif` version).
- Duplicate detection (`duplicate_detector.py`) excludes `RAW_EXT` (imported
  directly from `image_loader`, not to be duplicated locally — unlike
  `_VIDEO_EXT`, historically duplicated) from the Tier 1 sampling: without that,
  neither `cv2.imread` nor the plain `PIL.Image.open` of the fallbacks can decode
  a RAW, which would then be classified as "corrupted" and offered for deletion.
- `faces/detector.py::_exif_corrected()` forces conversion to a temporary JPEG
  (`needs_format_conversion`) for every RAW/HEIC extension, **even when** the
  EXIF orientation is already normal and the path is ASCII — without that
  unconditional trigger, a correctly oriented RAW/HEIC (by far the most common
  case) was passed as-is to `cv2.imread()`, which cannot decode it: 0 faces
  detected, silently, on nearly every RAW photo.
- `scanner.py::SUPPORTED_EXT` includes `RAW_EXT` only if `is_raw_available()`;
  `ExifReader.SUPPORTED` includes `.heic`/`.heif` unconditionally (pillow-heif is
  part of the core, not an optional dependency isolated in a plugin).

### DVD copies (VIDEO_TS/AUDIO_TS folders)

A "DVD copy" folder (standard `VIDEO_TS`/`AUDIO_TS` tree) is walked by the scanner
like any other folder — no dedicated exclusion. The `.VOB` files (the real video
streams) are catalogued normally through `VIDEO_EXT`; the `.IFO`/`.BUP` files
(navigation metadata, not media) stay ignored for lack of a supported extension.

`src/library/fs_utils.py::find_dvd_video_ts(folder)` detects, purely at the filesystem
level (one `os.scandir` of the direct children, no persistence), whether `folder` is a
DVD copy (`VIDEO_TS` as a direct child, case-insensitive). Two uses:
- `src/ui/sidebar.py::_mark_if_dvd_copy()` puts a disc icon on the tree node —
  restricted to folders **with no catalogued photo** (`count` empty/zero) to avoid one
  more `os.scandir` per displayed folder; rare in practice now that `.VOB` files are
  catalogued (the folder no longer looks empty), but still useful before the first scan
  of a newly added folder.
- `main_window.py::_on_photo_query_ready()` shows a message in the grid
  (`ThumbnailGrid.show_empty_message()`) with an "Open with an external player" button
  when a selected folder contains no catalogued photo but is a DVD copy — the residual
  case (DVD not yet scanned, or an incomplete copy with no usable `.VOB`). It reuses
  `tools.external_apps` (the same config as `PhotoViewer._open_with`) through
  `subprocess.Popen([app_path, folder_path])`; it only offers applications whose scope
  is `"video"`/`"both"` (see below), never those limited to `"image"`.

Each entry of `tools.external_apps` (Tools › External applications… menu,
`main_window.py::_open_external_apps_dialog`) carries an optional media scope
`"media"`: `"image"`, `"video"` or `"both"` (absent = `"both"`, backward-compatible with
configs predating this feature). `PhotoViewer.refresh_external_apps()` compares that
scope with the `media_type` of the displayed photo so that the application's icon shows
in the viewer bar only when relevant (e.g. VLC as `"video"` no longer appears when
viewing a still photo); the `_ext_apps_container` container is hidden entirely if no
application matches. `refresh_external_apps()` is called on every `set_photo()`
(navigation) in addition to config changes, to recompute that filtering for each
displayed photo.

### Non-destructive editing

Edits never modify the original files. Adjustments are stored in `%LOCALAPPDATA%\PixelPhotoManager\edits.db` (SQLite) and applied on the fly (display, export). The original is always recoverable.

- `src/processing/edit_database.py` — `EditDatabase`: the `photo_edits` table (current state) + the `edit_history` table (persistent history, 50 entries max per photo)
- The history is reloaded from the DB when a photo is opened → undo/redo persists across sessions
- The **Apply** button in `EditPanel` triggers `EditDatabase.save()`

**Minimum-width trap**: the grid of treatment buttons (2 columns — Contrast,
Vignette… in column 2) lives inside a `QScrollArea`, which never propagates the
`minimumSizeHint()` of its inner widget to its own (intended Qt behaviour, so
that content can be larger than the view). Without an explicit floor
(`scroll.setMinimumWidth(...)` set in `_setup_ui`, backed by
`EditPanel.content_min_width()` recomputed on demand — the application Qt style
is only fully resolved after the first display, so a value frozen at
construction time underestimates the real width of the buttons once styled),
nothing prevents the splitter from squeezing the panel below that width: column
2 becomes invisible and unreachable by click, silently, for a real user (not
just as an e2e automation artefact). `main_window.py::_ensure_left_pane_min_width()`
queries `content_min_width()` to size the splitter — it is also called when the
left `QStackedWidget` changes page, which does not trigger a relayout on its
own. Dedicated regression test (direct geometry, no OS automation):
`tests/gui_widgets/test_edit_panel.py::TestEditPanelContentMinWidth`.
`content_min_width()` also counts the width of the vertical scrollbar
(`_vertical_scrollbar_width()`): the panel's content always exceeds the
available height, so the bar is present in practice and eats into the viewport
by that much — adding a treatment to `_TREATMENTS` (hence a grid row) was enough
to push column 2 out without it.

### Decorative frames

`src/processing/frames.py` — 13 patterns (`FRAME_TYPES`: plain surround, simple, double,
gilded baroque, egg-and-dart, Greek key, art deco, carved wood, vine leaves, roses,
flowers, metallic, reflections), rendered procedurally in PIL/numpy, with no external
image file whatsoever. Settings (`PARAMETRIC_FRAMES` = plain/simple/double): colour style
(`COLOR_STYLES`: solid, gradient, glitter), outer width, gap, inner width — every width is
a **fraction of the short side** of the photo (exposed as a percentage in the UI, since
`EditSlider` has an internal scale hard-wired to 100).

**Relief engine** (the 10 patterns outside `PARAMETRIC_FRAMES`) — what makes a frame look
carved is not the drawing but the light: an ornament laid down in flat colour stays flat
whatever its outline. Rendering therefore happens in three stages, on a height map
(`float32`), never by painting coloured polygons:
1. **Moulding profile** — `_PROFILE_SEGMENTS`/`_PROFILE_LUTS` (`ogee`, `cove`, `flat`,
   `bevel`, `round`, `steps`, `scoop`, `field`): a cross-section sampled into a 1-D LUT,
   interpolated by `np.interp` over `t = distance_to_edge / border`. The LUTs must stay
   **increasing in `t` over [0, 1]** — `np.interp` requires it. `field` is a flat field
   between two beads: use it when the carving must carry the relief on its own (the three
   foliage frames), since a hollowed moulding otherwise competes with the ornaments that
   cover it.
2. **Carved ornaments** — `_Carver` (`dome`/`flat`/`disc`/`ridge`/`groove`) writes into the
   height map, plus an optional RGBA layer for the patterns that are genuinely painted
   (porcelain, roses). The `edge` parameter carves the carver's outline groove: without it,
   two neighbouring motifs merge into soft mush (a real case on the baroque acanthus).
3. **Lighting** — `_shade_relief()`: normals through `np.gradient`, Lambertian diffuse +
   Blinn-Phong specular (`_LIGHT`/`_HALF`, light **from the top left** — reversing it would
   flip the perception of every frame at once), approximate cavity occlusion through a
   blurred height difference, patina in the hollows, wear on the gilding. Materials in
   `_MATERIALS` (gold/silver/bronze/walnut/lacquer/carmine/porcelain/paint);
   `_DECOR[kind] = (profile, material, amplitude)` — a pattern added to `FRAME_TYPES`
   without a `_DECOR` entry would silently fall back to a flat grey.

**Covering foliage frames** (`vine`, `roses`, `flowers`) — patterns that fill the band from
edge to edge instead of forming a spaced frieze. The recipe, in this order: the `field`
profile; motifs at the scale of the band (a leaf or corolla ≈ half its width); a **carpet
of foliage** (`_carve_foliage`) underneath; rows of flowers in a staggered layout with
random jitter on top (two aligned rows produce an "88" pattern); and finally a tint
specific to each pattern. Two rules learned from failed iterations:
- **Value survives downscaling, relief does not.** An ornament in a single colour becomes a
  blob again in a thumbnail, however well carved — hence the per-petal-ring tints
  (`_carve_rose`), the per-leaf tints within a tuft, and the three-value bronze patina of
  `_carve_vine` (the band stays uniform, only the motifs are tinted).
- **A fan of leaves covers a sector, not a disc.** Tufts must aim **across** the band
  (± the normal, with jitter); oriented at random they open a diagonal grid of gaps.

`_detail_steps(span, full, minimum)` caps the number of vertices of an outline at one per
~2.5 px (`_petal_polygon`, `_cup_polygon`, `_vine_leaf_polygon`, `_circle_polygon`,
`_ellipse_polygon`); `_Carver.dome` falls back to 2 nested outlines below a 12 px
footprint. Without these two caps, a 240 px thumbnail pays exactly the same vertex cost as
a 6000 × 4000 export — the number of motifs does not depend on resolution (their spacing is
a fraction of the band width). They divide the cost of a `roses`/`flowers` thumbnail by
~2.5 (0.21 s → 0.08 s), which matters: the width slider dirties all thirteen gallery
thumbnails at once.

`DECOR_MIN_WIDTH` (0.08) + `suggested_width(kind, current)`: a carved frame does not exist
below a certain thickness (at 5%, the default width, the ornaments are illegible). Choosing
a decorative pattern therefore **visibly raises the width slider**
(`FrameDialog._select_kind`) rather than applying a hidden floor at render time —
otherwise the slider would lie about what is displayed. The width slider is thus exposed
for **every** pattern, and `_TileLoader` applies `suggested_width` to each thumbnail: the
thumbnail shows exactly what clicking it produces.

`plain` ("Plain surround") is the only pattern **without relief**: a strict flat fill of
`frame_color` (black/white shortcuts through `QUICK_COLORS` in the dialog), with no bevel
and no fillet — that is what distinguishes it from `simple`. It is therefore in
`PARAMETRIC_FRAMES` (adjustable width + colour) but **not** in `STYLED_FRAMES` =
simple/double (the only ones exposing `COLOR_STYLES` and `frame_color2`). Never route it
through the relief engine: the requested black would no longer be a true black (dedicated
test `TestPlainFrame::test_band_is_exactly_the_requested_color`).

`plain` additionally accepts an **optional second frame** (`frame_inner_enabled`, an
`edits.db` column added by `_MIGRATE_FRAME`, off by default) reusing `frame_gap` and
`frame_inner_width`. This is the **first** of the two exceptions to the invariant below
(the other being the spills of `SPILL_FRAMES`, further down): it is painted ON TOP of the
photo (`_draw_inner_overlay`, after the `paste`), at `frame_gap` from the edge, the strip
of image left visible between the two frames being the intended effect. It therefore does
**not** enter `border_px()`/`content_box()` (the canvas only grows by the outer frame) and
the geometry of the interactive tools stays that of the whole photo —
`inner_overlay_px()` is a display computation, never a geometry input.

That second frame carries **ironwork** (`INNER_MOTIFS`: `line` a plain line, `corners`
corner scrolls, `scrolls` running scrolls, `twist` a twisted bar, `studs` forged studs —
columns `frame_inner_motif`/`frame_inner_relief`/`frame_inner_ornament`), rendered in light
relief or as a strict flat fill and sized by the "Ornaments" slider (a factor clamped to
`[INNER_ORNAMENT_MIN, INNER_ORNAMENT_MAX]`, exposed as a percentage). Three rules to
respect:
- `line` is the **default** and stays a strict flat fill: it ignores `frame_inner_relief`
  and is drawn directly on the canvas at full resolution (no resampling blur) — a migrated
  database must render exactly the frame from before the feature. Relief and slider
  therefore only concern `ORNAMENTED_MOTIFS`, and the dialog hides both settings for
  `line`.
- The ornaments grow **inwards** from the line: they stay inside the photo, leave the
  `frame_gap` strip clean and never enter `border_px()`/`content_box()`. Their layer
  (`_inner_motif_layer`) is exactly the size of the photo and is pasted at
  `(border, border)` — that is what makes overflow impossible by construction.
- The layer is rendered at a bounded working resolution (`_WORK_MAX`) × supersampling
  (`_SS`) then downscaled once, like the band: ~0.8 s on a 6000 × 4000 export, against
  0.43 s for `line`. A rendering failure is caught and replaced by a plain ring
  (`_draw_inner_overlay`), never by losing the frame.

**Spills** (`SPILL_FRAMES` = vine/roses/flowers) — a few patterns go OVER the photo, so that
the frame reads as a sculpture overhanging the image rather than a frieze glued to the
edge. This is the second and last exception to the invariant below, purely a display
matter as well: the layer (`_spill_array`/`_render_spill`, `_SPILLERS[kind]`) is pasted
**after** the photo in `apply_frame()`, and enters neither `border_px()` nor
`content_box()`. Four points:
- What tells a sculpture from a sticker is not the motif: it is the **drop shadow** on the
  image (`_SPILL_SHADOW`, offset along the axis of `_LIGHT`) and the fact that every motif
  stays **attached** to the band. `_spill_stem` draws that attachment in three points from
  UNDER the edge — a straight, long stem crosses the ornaments of the band and reads as a
  pin stuck into the frame.
- "Sometimes" is a requirement, not an approximation: a spill at regular intervals is a
  frieze again. Hence the per-site draw (`_SPILL_SKIP`) and the spacing of several band
  widths (`_SPILL_SPACING`) in `_spill_sites`/`_spill_corners`, which place the anchor
  points astride the inner edge.
- The silhouette that carries the shadow comes from a **third channel** of the `_Carver`
  (the `mdraw` mask), fed only by the passes that ADD material (`dome`/`flat`/`ridge`):
  writing `groove` into it would leave the furrows trailing as black scratches on the
  photo.
- Isolated on the image, an ornament forgives nothing: a domed vine leaf reads as a
  starfish made of modelling clay (hence a distinctly flat relief, with the outline and
  veins carrying the drawing on their own), and a shiny tendril repeated along the edge
  reads as a keyring loop (hence its restriction to the corners). An isolated motif must be
  a tuft — foliage + flower/cluster — never a single ornament.
- Cost: ~1.2 s more on a 6000 × 4000 export, 0.06–0.12 s per gallery thumbnail. Tests:
  `tests/test_frames.py::TestSpill` (real spill, centre of the photo untouched, geometry
  unchanged, deterministic rendering, failure without losing the frame).

**Invariant**: `apply_frame()` pastes the photo **last** onto an enlarged canvas — the
frame never covers a single pixel of the image, it is added around it (apart from the two
display exceptions above, which have no effect on geometry). Corollary: the displayed
pixmap is larger than the photo, and every relative coordinate (crop, red-eye, vignette,
annotations, face bbox) refers to the **content**, not to the pixmap.
`viewer_canvas._img_rect()` therefore removes the border (`_frame_border_px()` →
`frames.content_box()`, the exact inverse of `border_px()` — one pixel off would shift
every tool). Never recompute a position from `self._pixmap.width()` in the canvas: go
through `_img_rect()`.

`ImageAdjuster.apply_all(image, edit, with_frame=True)` applies the frame last.
The export (`main_window.py`) passes `with_frame=False` then calls `apply_frame()`
itself **after** `composite_annotations_pil()` — annotations are in content
coordinates, so they must be composited before the enlargement.

UI: `src/ui/frame_dialog.py::FrameDialog` — a gallery of previews of the current photo
(one per pattern, rendered in a `_TileLoader(QThread)` reusing a base image decoded only
once), width adjustable for **every** pattern, the other settings reserved for the
parametric ones (fill style and second colour for `STYLED_FRAMES`, in `_style_row_host`;
ironwork reserved for the second frame of `plain`, relief and "Ornaments" for
`ORNAMENTED_MOTIFS`), live preview through `preview` → `EditPanel._on_preview`. The panel
only modifies `self._edit` on validation, so that `_push_undo` really stacks the previous
state. Re-rendering the thumbnails is deferred and **targeted** (`_mark_dirty(kinds)` →
`_dirty_kinds` → `_refresh_timer`): only the width slider dirties all 13 thumbnails, the
other settings dirty just three.

### Menus — popup width and enumerating submenus

`src/ui/ui_utils.py` exposes `install_menu_width_fix(menu_or_bar)`: when the popup
opens (`aboutToShow`), the required width is recomputed (`menu_required_width()`)
and set as `minimumWidth`. Without it, the native Windows style reserves the
shortcut column as tightly as possible and a long label ends up **underneath** its
shortcut (a real case: "Export the selection to a folder…" + `Ctrl+Shift+E`). The
computation adds up the item chrome — measured by asking the style itself
(`sizeFromContents(CT_MenuItem)` on a text of known width, which also captures the
padding of a stylesheet) — then label + separation + shortcut. It therefore stays
aligned with Qt's `sizeHint` for menus without shortcuts, which are not widened.

The label ↔ shortcut separation is set by `_SHORTCUT_GAP_EM` (4 widths of an "M"):
that is the only knob to touch to loosen or tighten the shortcut column. Since the
shortcut is right-aligned in the popup, any width added there ends up entirely in
that gap; menus without shortcuts see none of it. Test:
`test_menu_width.py::TestMenuRequiredWidth::test_shortcut_column_is_aired`.

To be wired on **every** menu that may display a shortcut — through
`QAction.setShortcut()` as well as through the "Label\tKey" convention of context
menus: the menu bar (`main_window.py`, a single call covers its menus and their
submenus) and each contextual `QMenu(self)`. Submenus are wired on the fly when
their parent opens, so dynamically rebuilt menus (Rate, external applications…)
are covered without a dedicated call. Do not replace `QMenu(self)` with a
home-made factory: several tests substitute `QMenu` in the module namespace to
intercept `exec()` (`tests/gui_widgets/test_album_mode_no_delete.py`).

**PySide6 6.11 trap**: `QAction.menu()` returns a wrapper whose collection
**destroys the C++ QMenu** (empty submenu, then `RuntimeError: Internal C++ object
already deleted` on the next access). Enumerate the submenus of a QMenu/QMenuBar
only through `findChildren(QMenu, Qt.FindDirectChildrenOnly)`
(`ui_utils._submenus()`), never through `QAction.menu()`. Test:
`tests/gui_widgets/test_menu_width.py`.

### Three-level thumbnail cache

`src/library/thumbnail_cache.py` — RAM LRU (500 entries, ~50 MB) → SQLite → on-demand generation in a thread. Never generate thumbnails in the UI thread.

For videos, `generate()` delegates to `_generate_video_thumb()`: `cv2.VideoCapture` → seek to 10% of the duration → BGR→RGB frame → PIL → JPEG.

### Folder manager

`src/ui/folder_manager_dialog.py` — `FolderManagerDialog(QDialog)` — reachable through **Tools › Folders…**.

- Shows every watched folder with its status (✓/✗), file count, and skipped subfolders (hidden, Originals).
- Signals: `rescan_requested(str)`, `folder_removed(str)`, `folder_added(str)`.
- A forced rescan goes through `LibraryScanner.scan(folders, force=True)` → `ScanThread(force=True)` → `known = {}` (bypassing the mtime cache).
- `folder_removed` is handled by `MainWindow._on_folder_removed()`: a confirmation (with the number of photos affected), then `_purge_catalog_for_folder()` removes the photos from the catalog, the thumbnails (`ThumbnailCache.invalidate`) and the faces/`indexed_photos` (`FaceDatabase.delete_for_path`) for that folder. The files themselves are left untouched on disk.

### Deletion — always through the Windows recycle bin

`src/library/trash.py` is the **single point** of deletion for a user file:
`move_to_trash(path)` (a `send2trash` wrapper, `os.path.normpath`, raises
`FileNotFoundError` if absent) and `is_trash_available()`. Absolute rule: the
application **never** permanently erases a user file — on failure (network drive,
volume with no recycle bin → `TrashPermissionError`/`OSError`), the exception
propagates to the caller, which must tell the user that the file has **not** been
deleted (never a silent `unlink`/`rmtree` fallback). Sites concerned:
`background_workers.py::_DeleteWorkerThread` (grid, viewer, corrupted files),
`sidebar.py::_delete_folder` (folder deletion, inside a QThread — a direct
`rmtree` would block the UI thread), `face_backup_dialog.py` (deleting a backup
archive). The application's **internal temporary files** (tempfile,
`_restore_tmp…` folders) stay on a direct `unlink` — they are not covered by this
rule, they are not user files.

### Duplicate detection — continuous and incremental

`src/library/duplicate_detector.py` (`DuplicateDetectorThread`) starts automatically after every scan (`MainWindow._on_scan_finished()` → `_start_duplicate_detection()`), on the same principle as face indexing: no manual button, no completion report. The **Tools › Duplicate status…** menu (`MainWindow._show_duplicate_status_dialog()`) shows a read-only snapshot (number of groups/photos, last check, corrupted files) with a **Check now** button to force a pass.

Two tiers (Tier 1 pHash, Tier 2 ORB+RANSAC for crops) — see the module docstring. The **pairwise** comparison (not just the per-file pHash/ORB computation, already cached by mtime) is genuinely incremental thanks to two tables `compared_tier1`/`compared_tier2` (`src/library/dedup_cache.py`) that track which paths have already been fully compared with the rest of the known library — only new×old and new×new pairs are re-evaluated, never old×old.

`DuplicateDetectorThread` takes a `seed_groups: dict[path, group_id]` parameter (typically `Catalog.get_duplicate_group_assignments()`) to seed `group_of` without recomparing everything. **Trap**: restarting the thread on an already-populated `cache_db_path` **without passing `seed_groups` again** makes every pair look "already compared" and no group is reformed — a silent return of `{}` instead of an error. In real use (`main_window.py`), `seed_groups` is always fetched fresh before each thread is created; only a new test/script that re-runs `_detect()` several times on the same cache needs to think about it explicitly.

A consequence of the incrementality: `Catalog.ignore_duplicate_group()` (dissolving a group, the ✕ button of the duplicates grid) is now **persistent** — an ignored group is never recreated as long as none of its members changes (they are already in `compared_tier1`/`_tier2`, so never compared with each other again). A new file matching one of them is still detected normally (new×old comparison).

### Faces — two stages of size filtering

`src/faces/detector.py::detect_and_embed()` excludes definitively (the face is never written to the database): `det_score < 0.5`, `embedding is None`, or `w < 20 / h < 20` px. Do not add an area threshold relative to the image there — that has already caused a bug (valid faces silently removed, with no trace and no way to recover them).

`src/faces/face_database.py::save_faces()` then marks `ignored=1` (the face is kept in the database, hidden from the UI/clustering, and **recoverable**) according to a threshold proportional to the resolution of the photo and `_AUTO_IGNORE_MIN_SCORE` (0.65). Size threshold: a face qualifies the photo as "foreground" if it reaches `_AUTO_IGNORE_MIN_SIDE_FG_RATIO` (20% of the short side of the photo, or 2× the base threshold if larger). If at least one foreground face is present, every face smaller than `_AUTO_IGNORE_FG_FRACTION` (1/4) of the smallest foreground face is ignored. Otherwise (no foreground at all), the base threshold `_AUTO_IGNORE_MIN_SIDE_RATIO` = 3% of the short side applies. This is the only stage that should decide whether a small face is noise — `FaceDatabase.recalculate_size_ignored()` implements the same rule but is currently not wired to any menu entry (orphan code, cf. `RevaluateSizeIgnoredThread` in `face_indexer.py`).

### Faces — recognition confidence tiers (face vs known person)

`src/faces/face_database.py` compares the cosine similarity of a face (or of a group's
centroid) with the centroids of the already named people, at three increasing tiers:
- `< 0.55`: no automatic action (unidentified face).
- `_SIM_SUGGEST = 0.55`: a suggestion is recorded (`suggestion_person_id`/`suggestion_score`)
  → the group appears as "awaiting verification" under the person concerned, to be
  confirmed manually.
- `_SIM_AUTO_ASSIGN = 0.70`: the person is assigned automatically, **without confirmation**
  (with the same side effects as `accept_cluster_suggestion`: deduplication, consumption of
  the pending Picasa annotations).

`set_cluster_suggestions()` is the single entry point applying that switch for the 4
producers of suggestions (`resuggest_clusters`, `find_similar_to_persons`,
`isolate_and_suggest`, and the auto-promotion of `face_cluster_workers.py`) — idempotent in
both branches (`WHERE person_id IS NULL AND suggestion_person_id IS NULL`), so a cluster
already assigned or already suggested is never rewritten by a later call, whatever tier is
reached.

`_SIM_STRONG = 0.50` (`src/ui/people_panel.py`) is a **separate**, purely cosmetic
threshold (blue "Probably X" label vs grey "Maybe X" at `_SIM_WEAK = 0.45`) for the faces
that have not reached `_SIM_SUGGEST` yet — do not confuse it with the thresholds above, nor
with `_SIM_GROUP` (0.72, the auto-grouping threshold between *unidentified* clusters,
unrelated to matching a known person).

**The badge and the panel it points to must filter the same rows.** The orange
"awaiting verification" badge of the sidebar (`_PendingBadgeDelegate`, fed by
`get_persons_pending_count()`) and the pending section of `PersonClusterView`
(fed by `get_suggested_clusters_for_person()`) are two separate queries over
`faces`: any difference between their `WHERE` clauses surfaces as a badge
counting a suggestion the panel does not display — the user has no way to make
it go away. Both therefore filter `person_id IS NULL AND ignored=0`. In the same
spirit, `ignore_face()`/`ignore_cluster()` **clear the suggestion** while raising
`ignored`: ignoring is the very decision the suggestion was waiting for, and a
suggestion left behind blocks the face for good, since every producer filters on
`suggestion_person_id IS NULL` (it would never be suggested nor regrouped again,
even after being un-ignored). A migration in `_init_db` purges the rows already
in that state, next to the one purging the suggestions left on identified faces.

The overlaid buttons of `_FaceItem` (`src/ui/face_panel.py`, the faces panel of the
viewer) follow that same "what is not settled yet" logic: the ✓/✕ pair of a suggestion
(`suggestion=True`) and the quick-ignore ✕ at the bottom right (`confirmed=False`) are
triage shortcuts. A **confirmed** identification — the face carries a known
`person_id`, or its cluster is mapped to a named person — gets **no cross**: it would
only offer a misclick on a face the user has just validated. Ignoring it stays
available through the context menu (`show_face_context_menu`), where the entry is
deliberately unconditional. Tests:
`tests/gui_widgets/test_face_panel_flows.py::TestQuickIgnoreCross`.

### Faces — person centroid cache (name assignment popup)

`src/faces/face_database.py::get_all_person_centroids()` decodes the embeddings (512D float32) of every identified face to compute each person's centroid — up to ~60k faces on a large library, several seconds in pure Python. The result is cached in memory (`self._person_centroid_cache`) and refreshed **person by person**: the fingerprint is a `SELECT person_id, COUNT(*), SUM(id) … GROUP BY person_id` (a few ms through `idx_faces_person`, which covers it since `id` is the rowid), and only the people whose fingerprint moved get their embeddings decoded again — the others keep the very same list object. `SUM(id)` (the face ids) and not `SUM(person_id)`: a face moving from one person to another (`merge_persons`, a reassignment) changes the count of neither side, but does change the sum of their face ids on both. `len(fps)` is compared too, to evict a person who has lost all their faces while nothing else moved. A **single global** fingerprint (the scheme up to 2026-08) was invalidated by any identification whatsoever, so confirming one suggestion re-decoded the whole library to recompute centroids that had not moved — and that cost was paid in front of the user, since the faces panel reloads right after an identification and its loading thread calls this method. The decoding itself is vectorised through `numpy.frombuffer` rather than `struct.unpack` (a factor of ~10). `enrich_persons()` (photo_count + cover_path/cover_bbox + pending_count) is expensive too (~1 s, dominated by a CTE with a window function for the cover photo); `enrich_persons_photo_count()` is a lighter variant (photo_count only) to be used everywhere the cover is not displayed, e.g. the assignment popup.

`FacePanel.set_photo()` only empties the panel (`_clear()`) when the photo really
changes. A refresh of the **same** photo — confirmed suggestion, ignore, unassign,
undo — keeps the faces on screen until `_on_faces_data_ready` clears and rebuilds them
in a single slot, so the swap costs no repaint. Clearing on the spot instead left the
panel blank for the whole duration of `_FacesDataLoader`: every face vanishing then
coming back, with nothing on screen to explain it. Test:
`tests/gui_widgets/test_face_panel_flows.py::TestRefreshWithoutBlanking`.

`_on_faces_data_ready` calls `_clear()` **before** assigning `self._cluster_persons`,
and the order is load-bearing: `_clear()` empties that dict **in place**, so assigning
first wiped the freshly built dict — and the local variable with it, being the same
object. Every face whose person comes from its cluster (re-indexed after the
assignment, `person_id` not yet propagated) then fell through to the "Group {id}"
branch: the name was lost, and with it the "confirmed identification" status. Test:
`TestQuickIgnoreCross::test_the_group_name_is_displayed_not_the_group_number`.

In `src/ui/face_panel.py`, the name assignment popup (`_AssignDialog`) is prepared by `_AssignPrepLoader(QThread)` (get_persons + enrich_persons_photo_count + person suggestion by cosine similarity) before being opened, to honour the "the UI never blocks" rule below — `face_cluster_grid.py::_PersonsLoader` follows the same principle for the group grid view.

### Faces — the identification grid updates itself, it never reloads

`src/ui/face_cluster_grid.py::FaceClusterGrid.refresh()` restarts
`_ClusterRefreshThread`: Union-Find over every unidentified cluster + suggestions,
i.e. several seconds behind a modal progress popup on a real library. It is the entry
point for *new data* (end of clustering, reset), **never** the way to reflect an action
the user has just performed — the display is already known at that point. Three local
paths do that instead, and any new action must follow one of them:

- `remove_clusters(ids)` — the groups disappear (identified, ignored).
- `apply_merge(source_ids, target_id)` — groups merged ("Associate", merge dialog): the
  source cards go away, the target's counter grows (`_ClusterCard.set_face_count`). An
  isolated face that absorbs other groups is no longer isolated: its card carries no
  counter, so it is **rebuilt** in the flat section (`_promote_solo_card`) and its entry
  in `_all_combined` switches from `"solo"` to `"group"` for the not-yet-rendered case
  (pagination).
- `restore()` — coming back from another view, straight from `_cached_data`.

All three fall back on `refresh()` under the same condition — Phase 2 still running or
no `_cached_data` — since there is nothing to patch locally then.

Corollary in the controller: `main_window_faces.py::_on_cluster_merged()` must **not**
refresh the grid (the grid did it), only the sidebar badge. An emptied `_flat_section`
/ `_solo_section` is hidden, never deleted (`_retire_section`): the pagination keeps a
reference to both and may refill them (`_ensure_section_in_layout`).

### Albums

`src/library/catalog.py::delete_album(album_id)` deletes an album (the `albums` + `album_photos` tables) without touching the photos. Reachable through a context menu on `Sidebar._albums_list` (`sidebar.py::_album_context_menu()`), which excludes the 4 special albums (Timeline/Favorites/Videos/By filename) through `isinstance(item.data(Qt.UserRole), AlbumInfo)`.

### Performance rule: the UI never blocks

Every operation over 50 ms goes into a `QThread`. PySide6 signals (`pyqtSignal`) are the only way to communicate from a secondary thread to the UI.

Corollary: **every user action gets immediate visual feedback**, even when the real result arrives asynchronously. Mechanisms in place (2026-07):
- **Viewer** — `PhotoViewer._base_lru`: an LRU (8 entries) of the 1024 px base images, keyed by path. `prefetch()` (called by `MainWindow._prefetch_viewer_neighbors()` after every navigation) preloads the ±1/±2 neighbours → instant prev/next. On a cold cache, the grid thumbnail (`thumb_cache.get_ram`) serves as an immediate placeholder (briefly blurry, never a black screen). `_apply_edit_to_base` has a fast path with no edit (direct JPEG decoding by Qt, without a PIL round-trip). The cache is invalidated when the file changes on disk (`invalidate_base_cache`: overwriting export, EXIF rewrite).
- **Grid** — `ThumbnailGrid.set_loading(True)` when a photo query starts (`_start_photo_query`): a "Loading…" indicator deferred by 150 ms (no flicker if the query answers quickly), hidden by `set_photos()`/`clear()`.
- **Faces panel** — busy cursor during `_AssignPrepLoader` (preparing the assignment dialog); after the dialog is validated, the label of the face(s) is updated **optimistically** and the DB write (assignment + deduplication + Picasa consumption, potentially long on a large group) goes into a `_DbWriteWorker` — the full refresh (`person_assigned` + `set_photo`) only happens when the worker finishes. The "Ignored faces" count is computed inside `_FacesDataLoader` (no more DB query on the UI thread at every navigation).

---

## Internationalisation (English, French, German)

`src/core/i18n.py` — English is the **source language** (`DEFAULT_LANGUAGE = "en"`):
strings are written in English in the code and any untranslated string falls back to
it automatically. It was French until 2026-08; the switch was made so that the
fallback of a forgotten message stays readable for any user, not just for French
speakers.
The language is a config setting (`ui.language`) applied **on restart**: widgets build
their labels once, there is no `retranslate_ui()`. Its default in
`config._DEFAULTS` is `"en"` too — a fresh install therefore starts in the source
language, the only one where no message can be missing. It stayed `"fr"` for a while
after the source-language switch, which left the e2e suite asserting English labels
against a French interface.

**Two entry points write that key**, deliberately: Settings › Language, and the flag
button at the far right of the top bar (`src/ui/language_button.py`, flags drawn by
`src/ui/flag_icons.py`). The duplication is the point — the language is the one
setting a user must be able to reach *without being able to read the interface*, so
it cannot live only behind a menu entry named "Settings". `MainWindow._open_settings()`
calls `LanguageButton.refresh()` on return so the flag never lags behind the config.
No emoji: no font shipped with Windows carries the regional-indicator flags, which
would render as a boxed "FR" — the exact failure mode the button exists to avoid.

`install()` installs two translators, `ppm_<code>.qm` (the application) and
`qtbase_<code>.qm` (the standard Qt dialogs — without it, a German interface keeps
"OK/Cancel" buttons in English).

Catalogs live in `translations/`: `.ts` versioned, `.qm` compiled, regenerated by
`tools/update_translations.py` (lupdate → plural post-processing → lrelease). The
PyInstaller `.spec` embeds `translations/ppm_*.qm` through a glob — a new language
code needs no change there.

**A single marking form in the whole project**:

```python
from src.core.i18n import translate
translate("MainWindow", "Displayed text")
```

Three prohibitions, all checked by `tests/test_i18n.py` — what they have in common
is that they break **nothing** visible: the program runs, the string is simply
absent from the catalog and therefore never translated.

- **Never `self.tr()`.** PySide6 resolves its context on the class of the
  *instance*; lupdate extracts it under the class that *writes* the call. The two
  diverge as soon as inheritance is involved — the `MainWindow` mixins
  (`main_window_faces.py`, `main_window_duplicates.py`) are in that case. A literal
  context removes the question: write the name of the **runtime** class there
  ("MainWindow" for a MainWindow mixin).
- **Never an alias.** `_t = lambda s: translate("Ctx", s)` compiles and runs, and
  produces zero extractable string: lupdate reads the code, it does not execute it.
  Context and source must be literals on the spot.
- **The 4th argument (the count) must be a plain variable name.** If it is an
  expression (`len(faces)`, `result.persons_created`, `n + 1`), lupdate **removes
  the message from the catalog**, with no error and no trace. Hoist the count into a
  local first (`n_faces = len(faces)`).

**A French literal in `src/` is now a defect, by construction.** As long as French
was the source language, a forgotten string was indistinguishable from a correctly
marked one: both displayed in French. The switch itself surfaced four holes that had
stayed invisible throughout the whole i18n campaign (the sidebar "Identify…" button,
the folder deletion confirmation button, the `<title>` of the duplicates report, and
the "Del" key concatenated into the context-menu labels of `thumbnail_grid`). An AST
audit of `src/` looking for French literals outside
`translate()`/`logger`/`journal`/`raise`/SQL has therefore become a check that means
something — it meant nothing before.

**Plurals** — a `%n` string is written neutrally in the code ("%n face(s)") since the
same literal serves both singular and plural. That is the only content of
`ppm_en.ts`: the source language has its own catalog **only** to carry the two real
forms ("%n face" / "%n faces"), the rest of it is empty (~1200 messages reported as
"untranslated" by `lrelease` on `ppm_en.ts`: that is normal and expected, unlike
`ppm_fr.ts`/`ppm_de.ts`, which must come out at 0 unfinished). Without it,
`QCoreApplication.translate` substitutes `%n` into the source and the user reads
"3 face(s)".

`update_translations.py::restore_numerus()` is the only place that knows a `%n` means
plural, and it runs after **every** lupdate: lupdate only marks `numerus="yes"` on a
whole literal (never the case in real code) and, worse, re-flattens the already
translated plurals at every pass, keeping only the first form. The forms are therefore
harvested before (`harvest_numerus`) and rewritten after. Two counts in the same
sentence: `%n` only agrees with one, the second goes through its own nested plural
string (cf. `main_window_faces.py`, "%n face annotation(s) in {photos}").

**A string that also serves as a key — the keys have stayed FRENCH.** Several places
used a label both as displayed text and as an internal identifier (dict key,
dispatch discriminant, operation name persisted in a database, `tab=` parameter).
Rule: **the key never changes, translate only at the display site** through a lookup
table and an accessor. These keys were frozen in French before the source-language
switch and have stayed that way — translating a key breaks the code silently, and
some of them are written into databases (`edits.db`) on users' machines. Instances in
place: `MainWindow._context_label` ("Toutes les photos", "Par notes", "Fichiers : "…),
`_MEDIA_SCOPE_VALUES`/`_media_scope_label`, `edit_panel._TOOL_LABELS`/`_tool_label`
and `_OP_LABELS`, `help_dialog._TABS`/`_TAB_LABELS` (and the file names under
`help_content/`, which stayed French in all three languages). A test that manipulates
one of these keys must therefore stay in French: that is intended, not a missed
migration. A corollary learned the hard way: comparing a widget's text with a literal
(`if combo.currentText() == "(all)"`) stops matching as soon as the interface changes
language — compare the index or a key, never the label.

**Message bodies: `translate(...).format(...)`, never an f-string.** An f-string is
evaluated before it reaches `translate` — lupdate only sees an expression, the string
does not enter the catalog, and the user reads an English message in a German
interface. That is the hole that let about thirty `QMessageBox` bodies through
(translated title, untranslated body). Write the substitutions as named `{name}`
placeholders in the translatable source, never positionally: word order changes from
one language to another.

A corollary of the same trap, for text **built at runtime**: progress messages
(`progress.emit`), status bar labels, thread journal badges. None of them appears in
a dialog, so none of them is spotted by re-reading the `QMessageBox` calls; all of
them were still f-strings after the first i18n pass. An AST audit of the
`ast.JoinedStr` nodes of `src/` (excluding `logger`/`journal`/`raise`/SQL) flushes
them all out at once — that is the only reliable way to check none are left.

**Units and date formats are translatable strings**, not constants: the source says
`B/kB/MB/GB`, French says "o/Ko/Mo/Go"; the source dates in `%m/%d/%Y`, which is
neither the French nor the German format. `fmt_size` (`ui_utils`, shared `"Units"`
context, reused by `face_backup_dialog` and `duplicate_detector`) and the display
`strftime` patterns (`exif_panel`, `main_window_duplicates`, `DuplicateReport`)
therefore go through `translate()`. The timestamps used in **file names**
(`%Y%m%d_%H%M%S`) and in logs stay fixed.

**Tests run without a catalog installed**: a `%n` message there falls back to its
neutral source ("Export 1 photo(s)"), never to what the user sees. A test asserting a
plural label must therefore install `ppm_en.qm` itself — see the `en_catalogue`
fixture in `tests/gui_widgets/test_small_dialogs.py`. Do not "fix" the assertion
towards the `(s)` form: that would freeze the test artefact as the expected
behaviour. Corollary: every label expected by the tests (unit **and** e2e) is in
English, apart from the internal keys above.

**`install()` before any `src.ui` import.** Many labels are **module constants** —
`frames.FRAME_TYPES`, `edit_panel._TREATMENTS`, `help_dialog._TAB_LABELS`, the tables
of `exif_panel`, the months of `thumbnail_grid`…: 312 `translate()` calls are
evaluated at import time, once. A module imported before `i18n.install()` therefore
freezes its English source for the whole life of the process, and the interface comes
out half translated without the slightest error. Hence the order in `main()`:
QApplication → `i18n.install()` → *only then* the `src.ui` imports (all deferred into
the body of `main()`, none at the top of `main.py`). Locked down by
`tests/test_i18n.py::TestInstallHappensBeforeUiImports`, whose detector is
**transitive**: `main_window` has no translated constant of its own but imports
`help_dialog` and `edit_panel`, which do.

**Built-in help** — `src/ui/help_content/<language>/*.html`, one subfolder per
language (`en/` `fr/` `de/`, 9 pages each; the file names stayed French in all three,
they are keys — cf. `_TABS`). `help_dialog._help_file()` resolves **file by file**
from `active_language()`, falling back to `en/`: a page not translated yet is shown
in English instead of losing the whole help for that language. Resolve from
`active_language()` and not from `ui.language` in the config — the latter may already
carry the language of the *next* start while the displayed interface is still in the
previous one. `_style.html` is pure CSS: it exists only in `en/` and lives off the
fallback. The PyInstaller `.spec` embeds `help_content` by folder, so a new language
subfolder needs nothing there.

**Deliberately untranslated**: the `logger`/`journal` messages, the exception texts,
the SQL DDL, and the body of the diagnostic report of `thread_journal_dialog.py`
(`_generate_problems_report`/`_THREAD_HINTS`) — meant to be pasted as-is into a bug
report, it stays in French whatever the interface. Only the chrome of that dialog is
translated.

### The MSI installer — the same three languages, picked by Windows itself

The installer speaks the same three languages as the application, and picks one
**without a bootstrapper and without a `TRANSFORMS` property**: `build_msi.ps1` links
the *same* wixobjs once per culture (`-cultures:<c> -loc installer\loc\<c>.wxl`),
turns the French and German MSIs into language transforms (`torch -p -t language`),
then embeds each transform into the English MSI as an MSI **substorage named exactly
by its LCID** (`1036`, `1031`) and lists the three LCIDs in the `Languages` summary
property (`SummaryInfo.Template` = `"x64;1033,1036,1031"`). Windows Installer looks
that list up on its own against the machine language; an LCID that is not listed
simply gets the base package. English is therefore the source language **and** the
fallback, exactly as in `src/core/i18n.py`.

Five things that break silently if changed:
- **The substorage name carries no extension.** `1036`, not `1036.mst` — msiexec
  looks up the bare LCID (`Looking for storage transform: 1036` in a `/L*v` log) and
  fails with error 1624 otherwise.
- **The `Languages` summary property is what triggers the selection**, not the
  presence of the substorages. It is written after the link, since each `light` pass
  only knows its own language.
- **`Product Id="*"` draws a new ProductCode at every link**, so the three passes
  come out with three different ones and `torch` would put that difference into the
  transform: the same build would install under another ProductCode on a French
  machine. The GUID must stay generated (a fixed one would break the `MajorUpgrade`
  of later versions), so the script realigns the language MSIs on the base
  ProductCode just before the comparison.
- **The licence is a file, not a string**: a `!(loc.*)` inside a `WixVariable` used
  as a file path is not resolved in time (`LGHT0103` on the literal `!(loc.…)`).
  `WixUILicenseRtf` is therefore defined per pass on the `light` command line
  (`-dWixUILicenseRtf=installer\license\<culture>.rtf`), not in the `.wxl`.
- **`-cc`/`-reusecab` shares one cabinet between the three passes** — the payload is
  identical, so compressing ~600 MB three times would be pure waste; `-sval` skips
  the ICE validation of the two intermediates, only the base package is validated.

Adding a language = one `installer/loc/<culture>.wxl`, one
`installer/license/<culture>.rtf`, one entry in `$LangTransforms` — nothing else. The
script ends on a self-check that fails the build if a substorage or an LCID of the
summary is missing (invisible otherwise until an installation comes out in English on
a French machine).

**The installer pre-positions the language of the application.** Windows picks the
transform matching the machine, so the installer is the only piece that knows, at that
moment, which language to start in — without it the application would come up in
English on a French machine until its user found the setting. `AppLanguageComp`
(`product.wxs`) writes `HKLM\SOFTWARE\PixelPhotoManager\InstallLanguage` = `en`/`fr`/`de`;
the value is the **localized string** `!(loc.AppLanguageCode)`, so it is carried by the
very same transforms as every label — no custom action, nothing computed at install
time. `i18n.installer_language()` reads it back (`None`, never `DEFAULT_LANGUAGE`, when
absent or unsupported: "no installer" must stay distinguishable from "installer in
English") and `Config._adopt_installer_language()` applies it **only when there is no
readable `config.json`**. Two consequences to keep:
- HKLM and not `%LOCALAPPDATA%\…\config.json`: the package is perMachine, installed
  once for every account of the machine, whereas the configuration is per user. A
  custom action writing the config would only serve the installing account, and would
  overwrite the choice of a user who already had one on update.
- `load()` writes nothing to disk — the language takes effect for that start and the
  first `save()` freezes it like any other setting. Tests:
  `tests/test_config.py::TestInstallerLanguage`,
  `tests/test_i18n.py::TestInstallerLanguage`.

**The final screen offers to start the application.** The tick box is the optional one
of the standard WixUI `ExitDialog` (`WIXUI_EXITDIALOGOPTIONALCHECKBOXTEXT` +
`WIXUI_EXITDIALOGOPTIONALCHECKBOX`, ticked by default), shown only on a fresh install
(its own `AND NOT Installed` condition) and labelled through `!(loc.LaunchApplication)`,
so the language transforms carry it like every other string. The launch itself is a
`WixShellExec` custom action (`WixUtilExtension`, `BinaryKey="WixCA_x64"` for an x64
package) published on the `Finish` button. Three points that are wrong by default:
- **`Impersonate="yes"`** — the action runs in the *client* process, so the application
  starts as the user, not with the elevated token of the perMachine installation. Started
  as administrator it would create its `%LOCALAPPDATA%` (`catalog.db`, `config.json`,
  `thumbnails.db`) in the administrator's profile, and the first real start would find an
  empty library.
- **`Return="ignore"`, not `asyncNoWait`** — `ShellExecute` returns as soon as the process
  is spawned, so the synchronous form costs nothing, while the asynchronous one races the
  end of the UI sequence (the same button closes the installer). `ignore` also keeps a
  failed launch from turning a successful installation into an error dialog.
- **`WixShellExecTarget` keeps an unresolved `[INSTALLFOLDER]` reference** — it is
  formatted when the action runs, i.e. after the user may have changed the folder on the
  InstallDir screen. Resolving it earlier (a `SetProperty` after `CostFinalize`, the usual
  way to silence candle's `CNDL1077`) would freeze the *default* folder; the warning is
  suppressed instead (`candle -sw1077`).

**The version displayed by the installer is painted into `dialog.bmp`.** The welcome
screen shows it in the left panel, in pixels — `installer/create_bitmaps.py` used to
draw the literal `"v 1.0"`, and `build_msi.ps1` only regenerated the bitmaps **when
they were missing**, so that constant survived every version bump and the 1.1.0
installer displayed 1.0. The script now regenerates them at **every** build and passes
`$ProductVersion` to `create_bitmaps.py` (which falls back to the `VERSION` file at the
root, the same single source as the MSI's `ProductVersion` and the exe). Everything
else the installer displays comes from `VERSION` through `candle -dProductVersion`.

---

## Plugin system

A plugin = a folder in `plugins/` with `plugin.json` + `plugin.py`.

Three base classes in `src/core/`:
- `BasePlugin` — every plugin
- `ProcessorPlugin(BasePlugin)` — image processing (implements `process(image, params) -> Image`)
- `ViewPlugin(BasePlugin)` — a new view in the sidebar (implements `create_widget(parent) -> QWidget`)

The `PluginManager` loads plugins dynamically through `importlib`. Plugins integrate without modifying existing code — only through the event bus and the menu/sidebar hooks.

---

## Database

Embedded SQLite, zero configuration. The catalog lives in `%LOCALAPPDATA%\PixelPhotoManager\catalog.db`. Thumbnails have their own database, `thumbnails.db`. The configuration is in `config.json` in the same folder. The base path is defined in `src/core/app_dirs.py` (`APP_DATA_DIR`). Use the standard `sqlite3`, no ORM.

**Connection pattern** (shared by `Catalog`, `FaceDatabase`, `ThumbnailCache`, `EditDatabase`): one SQLite connection **per (instance, thread)**, cached in a `threading.local` carried by the instance, with the PRAGMAs (`journal_mode=WAL`, `synchronous=NORMAL`, `cache_size`) set once at creation. Never go back to the "fresh connection per call" scheme. Corollary: the write methods do not close the connection — on an exception they call `conn.rollback()` (a `except BaseException: conn.rollback(); raise` guard) so as to **never** leave the cached connection inside an open transaction, which would make every subsequent write fail with `database is locked`. Every new write method must reuse that guard.

**Automatic migrations at startup** (the `try: ALTER TABLE ... except: pass` pattern):
- `_migrate_normalize_paths()` — normalises the Windows separators
- `_migrate_video_fields()` — adds `media_type` and `duration` if absent
- `_migrate_face_tables()` — adds the face and Picasa annotation tables

Trap: the `idx_faces_suggestion` index (faces.db) must be created **after** the migrations in `_init_db` — the `suggestion_person_id` column does not exist in `_CREATE_FACES`, only through `ALTER TABLE`.

**Column order of `photos`** (`catalog.py::_CREATE_PHOTOS`): `_photo_from_row()`
unpacks the row **positionally** (`*rest` for the columns added after
`media_type`/`duration`) — every new column must be added **at the end** of
`_CREATE_PHOTOS` (and of the matching `ALTER TABLE` migration), never in the
middle, on pain of silently shifting every following field on a database migrated
from an earlier version.

**No new user-editable column in the `DO UPDATE`** of `add_or_update_photo()`
(`ON CONFLICT(path) DO UPDATE SET ...`): `tags` and `rating` are deliberately
**absent** from that clause (like `is_favorite` before them) — a forced rescan
(`FolderManagerDialog` → `scan(force=True)`) must rebuild the EXIF/file fields but
never overwrite data entered by the user. Any future column of the same kind
(editable outside the scan) follows the same pattern: present in the `INSERT`,
absent from the `DO UPDATE`.

---

## Notable dependencies

| Package | Use |
|---------|-----|
| PySide6 | UI — use `QThread` + signals for threading |
| Pillow | Main image processing |
| opencv-python | Advanced processing (detection, filters, video thumbnails) |
| DeepFace + RetinaFace | Face recognition (optional, heavy) |
| scikit-learn | DBSCAN clustering for faces |
| imagehash | Perceptual duplicate detection |
| folium | OpenStreetMap map |
| reportlab | PDF export |
| send2trash | Deletion through the Windows recycle bin (`src/library/trash.py`) |
| pillow-heif | HEIC/HEIF decoding (`src/library/image_loader.py`) |
| rawpy | RAW decoding — CR2/NEF/ARW/DNG/ORF/RW2 (`src/library/image_loader.py`) |

The AI dependencies (PyTorch, DeepFace, Real-ESRGAN…) are **optional** and commented out in `requirements.txt`. Do not force them onto the core of the application — isolate them in plugins.

`scikit-learn` and `hdbscan` (face clustering, `src/faces/clusterer.py`) are, by contrast, **non-optional** dependencies of the application core: never add them to `excludes` in `pixelphotomanager.spec`, on pain of a `ModuleNotFoundError: sklearn` in the packaged executable only (dev Python mode is unaffected).

`insightface` must appear in `_with_data` (the `collect_all` list) of `pixelphotomanager.spec` **AND** its `data/objects/` folder must additionally be copied explicitly to the root of the bundle under the name `objects` (`datas += [(str(Path(insightface.__file__).parent / "data" / "objects"), "objects")]`). Reason: `insightface/data/pickle_object.py::get_object()` resolves the path differently depending on the mode:
- dev mode: `Path(__file__).parent / "objects"` → `insightface/data/objects/` (the normal package tree, which `collect_all()` alone reproduces in the frozen exe under `_internal/insightface/data/objects/`);
- frozen mode (`sys.frozen`): `sys._MEIPASS / "objects"` → an **`objects` folder at the root of the bundle** (`_internal/objects/`), completely different from the package tree.

`collect_all("insightface")` alone is therefore NOT enough: it does place `meanshape_68.pkl` in the frozen exe, but in the wrong location (`_internal/insightface/data/objects/`), never consulted by the code in frozen mode. Without the extra copy to `_internal/objects/`, `get_object('meanshape_68.pkl')` returns `None` silently (just a `print()`, invisible in `console=False` mode), and **every** detected face crashes `InsightFace.get()` with `AttributeError: 'NoneType' object has no attribute 'shape'` (inside `insightface/utils/transform.py::estimate_affine_matrix_3d23d`, called from `landmark.py::get()` for the `landmark_3d_68` model, pose estimation). A treacherous trap: detection succeeds (the bbox is found), only that landmark/pose post-processing fails, so it looks like a detection bug when it is really a data packaging problem — and a partial fix (just `collect_all`) changes nothing about the observed error, which can wrongly suggest the real problem lies elsewhere.

The `buffalo_l` model pack (SCRFD detection + ArcFace embedding, ~340 MB) is embedded in the bundle too, under `insightface_root/models/buffalo_l` (`pixelphotomanager.spec`, source = `~/.insightface/models/buffalo_l` on the build machine — so the application must have been run at least once in dev mode to have it cached locally before building). `src/faces/detector.py::_insightface_root()` points `FaceAnalysis(root=...)` at it in frozen mode. Without that, `insightface` tries to download the pack from GitHub on the first launch on every machine — silent and invisible as long as there is Internet access, but **completely blocking without access to github.com** (firewall, isolated machine): face recognition 100% inoperative (0 faces detected, whatever the number of photos), with a fresh full download attempt for *every photo* since the model is never cached.

`main.py` redirects `sys.stdout`/`sys.stderr` to `os.devnull` at the very beginning if they are `None` (the case of a `console=False` exe: any library writing to them, such as `tqdm` used by `insightface` during a download, crashes with `AttributeError: 'NoneType' object has no attribute 'write'`). That crash was particularly insidious with the `buffalo_l` download: the HTTP request did succeed (200 OK), but `tqdm` crashed while writing the progress bar, interrupting the stream **before** the file was written to disk — so the model was never cached, and the next run attempted a full download again, in a loop.

`pillow-heif` and `rawpy` (HEIC/RAW decoding, cf. `src/library/image_loader.py`)
also appear in `_with_data` (`collect_all`) of `pixelphotomanager.spec` — never in
`excludes` — so that their native libraries (libheif, libraw) are embedded. Unlike
the `buffalo_l` pack of insightface, no extra manual copy is needed: `collect_all()`
alone is enough for these two packages (verified with a dry-run `collect_all()`:
non-empty datas/binaries for both).
