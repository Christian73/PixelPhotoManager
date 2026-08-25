# User guide — PixelPhotoManager

> **PixelPhotoManager** is a non-destructive photo and video manager for Windows. Your original files are never modified: every edit is stored separately and applied on the fly for display.

---

## Table of contents

1. [Getting started](#1-getting-started)
2. [The interface](#2-the-interface)
3. [Managing your folders](#3-managing-your-folders)
4. [The photo grid](#4-the-photo-grid)
5. [Timeline mode (ribbon view)](#5-timeline-mode-ribbon-view)
6. [Viewing a photo or a video](#6-viewing-a-photo-or-a-video)
7. [Editing a photo](#7-editing-a-photo)
8. [Albums, favourites, ratings and keywords](#8-albums-favourites-ratings-and-keywords)
9. [Search and filtering](#9-search-and-filtering)
10. [Moving photos](#10-moving-photos)
11. [Saving and exporting your photos](#11-saving-and-exporting-your-photos)
12. [Slideshow](#12-slideshow)
13. [Face recognition](#13-face-recognition)
14. [Duplicate detection](#14-duplicate-detection)
15. [Syncing creation dates with EXIF](#15-syncing-creation-dates-with-exif)
16. [Other tools](#16-other-tools)
17. [Keyboard shortcuts](#17-keyboard-shortcuts)
18. [Where your data is stored](#18-where-your-data-is-stored)

---

## 1. Getting started

### Installation

The application is installed from the `PixelPhotoManager-<version>-x64.msi` file.
The installer **picks its own language** from the one Windows uses (French,
German, English for everything else) and **pre-positions the language of the
application** to match: at the first start, the interface is already in the
language of your machine, with nothing to set. An existing installation always
keeps the language its user chose.

The last screen offers a **Launch PixelPhotoManager** tick box, ticked by
default, which starts the application when the installer closes.

### First launch

At the first start, a welcome window invites you to choose at least one **photo folder** to watch.

1. Click **+ Add a folder** and select a folder holding your photos.
2. Repeat if you have several folders.
3. Click **Get started →**.

The application immediately indexes your photos and videos in the background. The status bar at the bottom shows the progress of the scan (`Scanning… 42%  —  file_name.jpg`).

### Adding a folder later

**File › Add a folder…** — select any folder; it is scanned automatically and added to the sidebar.

### Quitting the application

**File › Quit** (**Ctrl + Q**).

---

## 2. The interface

```
┌─────────────────────────────────────────────────────────┐
│ [File][View][Tools][Faces][Help]          [Export] [⚑]  │
├──────────────────┬──────────────────────────────────────┤
│                  │                                      │
│   SIDEBAR        │   MAIN AREA                          │
│                  │   (grid, ribbon, or viewer)          │
│   Folders        │                                      │
│   Albums         │                                      │
│                  │                                      │
├──────────────────┴──────────────────────────────────────┤
│  Status bar               [Size ──────────] [▦]         │
└─────────────────────────────────────────────────────────┘
```

| Area | Role |
|---|---|
| **Menu bar** | File, View, Tools, Faces, Help — see the dedicated sections |
| **⬆ Export button** | Exports the photo on display (viewer) or the selection (grid) |
| **Flag button** | Changes the language of the interface — see [Interface language](#interface-language) |
| **Sidebar** | Filtering, navigation through folders and albums |
| **Main area** | Thumbnail grid, timeline view (ribbon), People view, Duplicates view or full-screen viewer |
| **Status bar** | Information on the selection, scan progress, thumbnail size slider |

### The five menus

| Menu | Contents |
|---|---|
| **File** | Add a folder…, Advanced search… (Ctrl+F), Quit |
| **View** | Show/hide sidebar (F9), Full screen (F11), Slideshow (F5), Sort order… |
| **Tools** | Folders…, Duplicate status…, Sync creation dates with EXIF…, Thread journal…, Problem history…, External applications…, Settings |
| **Faces** | Import from Picasa…, Reset and reindex…, Group faces…, Error review…, Back up recognition data…, Manage backups…, Counters… |
| **Help** | Help… (F1), About |

### Hiding / showing the sidebar

Press **F9** or go to **View › Show/hide sidebar**.
The sidebar can also be resized by dragging the vertical separator.

### Full screen

**F11** switches the application to full screen.

### Sort order

**View › Sort order…** opens a window with two independent blocks: **Folders** (sidebar) and **Photo grid**. For each one, choose the sort mode — **Alphabetical** or **Chronological** — and the direction — **Ascending** or **Descending**. The two blocks are set separately: you can, for instance, sort folders alphabetically while keeping the grid in descending chronological order (most recent photos first). The setting is remembered from one session to the next.

### Interface language

The interface and the built-in help are available in **English**, **French** and
**German**. Two paths lead to the same setting:

- the **flag button**, at the far right of the top bar: one click drops down the
  list of languages, each written in its own language (English, Français,
  Deutsch);
- **Tools › Settings › Language**.

That duplication is deliberate: the language is the one setting you must be able
to reach **without being able to read the interface**. If the application starts
in a language you do not understand, the flag stays recognisable where a menu
named “Settings” does not.

The change takes effect **at the next start** of the application; a message
reminds you of it. The PDF documents (user guide, release note) are in English.

### Checking for updates

At start-up, the application queries the project's GitHub releases page in the background. If a newer version is available, a popup appears with the version number and an **Open the download page** button; it reminds you to read the release notes before installing, to learn about the new features and check compatibility with your existing library. If you are already up to date, or if a network error occurs, nothing is displayed.

The **About** tab (menu **Help › About**) performs the same check every time it is opened and shows one of three results: “✓ You have the latest version.”, the available-update warning with its link, or an error message if the check is impossible (no connection).

### Searching the help

The **Help…** window (F1) has a search bar at the top, above the tabs. Type a term: the tab on display is searched first, then the following tabs if the term is not there — the tab holding the first occurrence found is selected automatically and the matching passage highlighted. Press **Enter** to move to the next occurrence (the search wraps around the whole set of tabs). **Ctrl+F**, when the help window has the focus, puts the cursor in the search bar. If the term is nowhere to be found, the field turns red.

---

## 3. Managing your folders

### The sidebar — Folders section

The left sidebar shows the tree of your watched folders. Click the `▶` arrow of a folder to see its subfolders. Click a folder to display its contents in the grid.

### Context menu (right-click on a folder)

| Option | Effect |
|---|---|
| **Scan now** | Restarts the indexing of the folder (useful after adding files from outside the application) |
| **Stop watching this folder** | Removes the folder from the watched list (deletes nothing on disk) |
| **Create a subfolder…** | Creates a new subfolder directly from the application |
| **Rename…** | Renames the folder on disk and updates the catalog |
| **Move to…** | Moves the folder elsewhere on disk and updates every reference |
| **Open in File Explorer** | Opens the folder in Windows File Explorer |

> **Note:** renaming or moving a folder automatically updates the paths in the catalog and the edits attached to them. No data is lost.

### Folder manager (Tools › Folders…)

The **Tools › Folders…** menu opens a dialog for advanced management of the watched folders.

**For each folder, the dialog shows:**
- A ✓ indicator (folder found on disk) or ✗ (folder not found)
- The full path of the folder
- The number of indexed files
- The subfolders excluded from the scan (hidden folders, Picasa `Originals` folders, `.tmp_*` folders) with the reason for the exclusion

**Available actions:**
- **⟳ Rescan** — forces a full rescan of the folder, even for the files unchanged since the last scan. Useful to pick up changes made outside the application.
- **Remove** — removes the folder from the watched list (the photos already indexed stay in the catalog; only future scans are disabled).
- **＋ Add a folder…** — adds a new folder to watch.

---

## 4. The photo grid

### Navigating the grid

- **Click a folder** in the sidebar → shows the photos of that folder.
- **Scroll** with the mouse wheel to browse the thumbnails.

### Thumbnail size

The **Size** slider at the bottom right of the window offers four sizes: small, medium, large, very large.

### Selecting photos

| Action | Result |
|---|---|
| Single click | Selects the photo (deselects the others) |
| **Ctrl + Click** | Adds/removes the photo from the selection |
| **Shift + Click** | Selects a range of photos |
| **Ctrl + A** | Selects every photo of the folder |

The status bar shows the number of selected photos and the total.

### Video thumbnails

Videos are shown in the grid with a thumbnail extracted automatically (at roughly 10% of the video's duration), with a **▶** badge on top to tell them apart from photos.

### Duplicate badge

A photo belonging to a detected duplicate group (see [section 14](#14-duplicate-detection)) shows an orange **⧉** badge on its thumbnail. Clicking it opens a **“Duplicates of this photo”** popup listing the other copies (name + folder); double-clicking an entry of the list navigates straight to that file.

### Rating badge

A rated photo (see [Ratings](#ratings) below) shows a star badge (e.g. “★★★”) at the bottom left of its thumbnail.

### Opening a photo or a video

**Double-click** a thumbnail → opens the viewer.

### Thumbnail context menu (right-click)

| Option | Effect |
|---|---|
| **Open** | Opens the photo in the viewer |
| **Mark as favourite / Remove from favourites** | Manages the favourite state |
| **Rate** | ★ to ★★★★★ submenu to rate the selection, or **Clear the rating** |
| **Keywords…** | Opens the keyword editing dialog for the selection (see [Keywords](#keywords) below) |
| **Rename the image** | Renames the file on disk |
| **Move to…** | Moves the file to another watched folder |
| **Save the edited image to disk** | Opens the save dialog (see [section 11](#11-saving-and-exporting-your-photos)) |
| **Add {this photo\|the N selected photos} to an album…** | Adds the photo (or the whole selection) to an existing album |
| **Create a new album with {this photo\|the N selected photos}…** | Creates a new album on the fly with the photo (or the selection) |
| **Show in File Explorer** | Opens the folder holding the photo |
| **Retry face identification** | *(shown only if the photo is in face-detection error)* restarts the analysis for that single file |
| **Remove from the album / Remove the photos from the album** | *(album view only)* Removes the photo(s) from the album on display — files and catalog untouched |
| **Delete the file… / Delete the files…** | Sends the file(s) to the **Windows recycle bin** (recoverable) after confirmation |

> The right-click applies to **the whole current selection** (Ctrl+Click / Shift+Click) for Rename/Move/Add to an album/Delete, not only to the thumbnail you clicked.

### Deleting photos

Select one or more photos, then press the **Del** key (or right-click › Delete the file(s)…). A confirmation is asked. The files are sent to the **Windows recycle bin** (recoverable from there), never erased permanently. If the recycle bin is unavailable (network drive, volume without a recycle bin…), the file is **not** deleted and a message says so.

The deletion runs in the background: the interface stays usable during the operation, and the status bar shows the progress (“Deleting… n/total”) on large selections.

> **Inside an album**: the **Del** key (grid as well as viewer) **removes** the photo(s) from the album instead of deleting the files — see [Removing photos from an album](#8-albums-favourites-ratings-and-keywords).

### Renaming a photo

Right-click the thumbnail › **Rename the image** — type the new name without the extension. The file is renamed on disk and the catalog is updated.

---

## 5. Timeline mode (ribbon view)

Clicking **★ Timeline of every photo** in the sidebar (a special album, right at the top of the Albums list) shows your photos in a **ribbon view** rather than a classic grid.

### Principle

- The photos are laid out on **five rows** of decreasing size towards the top and the bottom; the **centre row** (clearly larger) holds the “current photo”.
- A small floating inset shows the **date** of the photo currently at the centre of the ribbon.
- The centre photo is the starting point if you launch a slideshow (**F5**) from this view, and it is the one deleted by **Del** when nothing else is selected.

### Navigation

| Action | Result |
|---|---|
| **Wheel** | Scrolling with inertia (glides gradually, like a physical ribbon) |
| **← / →** | Moves forward/back one photo |
| **↑ / ↓** | Moves forward/back three photos |
| **Vertical scrollbar** (to the right of the view) | Direct navigation, visible only in timeline mode |
| **Del** | Deletes the centre photo (or the current selection) |

> This mode is only active for the **Timeline of every photo** album; the other albums (Favourites, Videos, By file name, folders, custom albums) are shown in a classic grid.

---

## 6. Viewing a photo or a video

### Opening the viewer

Double-click a thumbnail in the grid.

### Navigating between photos

| Action | Result |
|---|---|
| **← / ↑** or **◀ Previous** | Previous photo (stops at the first one) |
| **→ / ↓** or **Next ▶** | Next photo (stops at the last one) |
| **Esc** or **✕** | Back to the grid |
| **▦** (status bar) | Back to the grid |

### Zoom

| Action | Result |
|---|---|
| **Zoom** slider (status bar) | Adjusts the zoom from 10% to 400% |
| **F** | Fit to window |
| **Z** | Zoom 100% (1 photo pixel = 1 screen pixel) |
| **⊡** (toolbar) | Fit to window |
| **1:1** (toolbar) | Zoom 100% |
| **Ctrl + Wheel** | Zoom in/out |
| Click and drag | Moves the image inside the window |

> In **crop mode**, the mouse wheel zooms. In **normal mode**, it moves to the next or previous photo.

> The **0** to **5** keys rate the photo on display (see [Ratings](#ratings)) — they no longer control the zoom.

### EXIF panel

Press **I** (or click the `[i]` button in the toolbar) to show/hide the EXIF panel. It is organised in sections:

- **File**: name, format, colour mode, dimensions, size, modification date.
- **Camera**: make, model, serial number, lens (make/model/specification/serial number), software.
- **Capture**: date, exposure, aperture (and maximum aperture), ISO (+ sensitivity type), focal length (+ 35 mm equivalent), digital zoom, exposure program, metering mode, exposure compensation, brightness, white balance, light source, flash (decoded as text, e.g. *“Fired, return detected”*), scene type, subject distance, contrast/saturation/sharpness, custom rendering.
- **Image**: dimensions in pixels, colour space, orientation, resolution, compression, bits per channel.
- **Author / rights**: artist, copyright, description, and the Windows fields (title, comment, author, keywords, subject).
- **GPS** *(if present)*: latitude/longitude, **altitude**, **GPS speed**, **shooting heading** (true or magnetic), **GPS date/time**, **accuracy (DOP)**.
- **Video** *(for videos)*: resolution, frames per second, duration, codec.
- **Miscellaneous**: every other EXIF tag present, not covered by the groups above.

#### Editing the EXIF metadata

The **✎ Edit metadata…** button at the bottom of the panel (disabled for videos) opens a dialog that edits the file directly:

- **Capture date** (calendar)
- **Description**
- **Artist**
- **Copyright**

An **“Also update the file date (mtime + creation date)”** tick box (ticked by default) additionally applies the new date to the file itself on disk.

> ⚠ Unlike image edits (which are non-destructive), this operation **writes directly into the file**.

### Watching a video

When you open a video in the viewer:
- The first extracted frame is displayed.
- A **▶ Open the video** button appears in the toolbar.
- Click it to play the video in the configured player (see **Settings**, [section 16](#16-other-tools) — the default system player or a custom one).
- The editing panel is **not available** for videos.

### Locating on the map

If a photo carries GPS coordinates, **right-click** in the viewer and choose **Locate on the map**. OpenStreetMap opens in the browser, centred on the place the photo was taken.

> The option is greyed out if the photo has no GPS data.

### Marking as favourite

Click the **♡** button in the viewer toolbar. The filled star **★** marks an active favourite.

### Rating a photo

Click one of the 5 stars in the viewer toolbar (next to the favourite button), or use the **1** to **5** keys on the keyboard. Clicking the rating already given (or pressing **0**) clears it. See [Ratings](#ratings) for the other ways to rate (context menu, thumbnail badge, “★ By rating” album).

### External applications

If you have configured third-party applications (see [section 16](#16-other-tools)), their icons appear in the viewer toolbar, next to the favourite button. One click starts the application with the current photo as an argument (tooltip: “Open with *name*”).

### Context menu in the viewer (right-click)

| Option | Effect |
|---|---|
| **Mark as favourite / Remove from favourites** | Manages the favourite state |
| **Rate** | ★ to ★★★★★ submenu to rate the photo, or **Clear the rating** |
| **Keywords…** | Opens the keyword editing dialog for the photo (see [Keywords](#keywords)) |
| **Rename…** | Renames the file on disk |
| **Move to…** | Moves the file to another folder |
| **Save the edited image to disk** | Opens the save dialog (see [section 11](#11-saving-and-exporting-your-photos)) |
| **Show in File Explorer** | Opens the folder holding the photo |
| **Show the folder in the grid** | Goes back to the photo grid, showing the folder that holds the current photo and selecting it |
| **Locate on the map** | Opens OpenStreetMap at the GPS position (greyed out if no GPS) |
| **Force a new detection with no size limit** | Restarts face detection on this photo ignoring the minimum-size filter, without losing the identifications already made |
| **Delete the file…** | Sends the file to the **Windows recycle bin** (recoverable) after confirmation |
| **Remove from the album** | *(replaces “Delete the file…” when the photo was opened from an album)* Removes the photo from the album — file and catalog untouched |

> As in the grid, the **Del** key in the viewer removes the photo from the album if it was opened from an album, and deletes the file otherwise.

---

## 7. Editing a photo

### Reaching the editing panel

Open a photo in the viewer. The **Editing** panel appears automatically on the left.

> **Non-destructive principle**: edits never modify the original file. They are stored in a separate database and applied on the fly for display and for export. You can always recover the original.

> The editing panel is not available for videos.

> **One tool active at a time**: selecting a new tool (Crop, Red eyes, Annotations, Frame, Brightness, Contrast, Colours, Vignette, Straighten…) automatically validates the work in progress in the previous tool then closes it — no need to validate by hand before switching tools.

---

### Tonal corrections

Click one of the six correction buttons to open its adjustment dialog.

| Correction | Range | Description |
|---|---|---|
| **Brightness** | -1.00 to +1.00 | Lightens or darkens the image |
| **Contrast** | -1.00 to +1.00 | Widens or narrows the gap between light and dark tones |
| **Saturation** | -1.00 to +1.00 | Intensifies or desaturates the colours (−1 = black and white) |
| **Gamma** | 0.10 to 3.00 | Adjusts the brightness curve (1.0 = neutral) |
| **Sharpness** | 0.00 to 1.00 | Accentuates the edges |
| **Denoise** | 0.00 to 1.00 | Smooths out digital noise |

**In every correction dialog:**
- The main **slider** sets the value.
- The **▲ ▼ arrows** on the right allow fine adjustment to a hundredth.
- **Double-clicking the slider** resets the value to zero.
- The **preview** updates in real time in the viewer.
- **Apply** applies the setting; **Cancel** restores the previous value.

---

### Colours (black & white with channel mixing)

The **Colours** treatment converts the photo to black and white with control over the contribution of each channel.

1. Click **Colours** in the editing panel.
2. Tick **Black & white** to enable the conversion.
3. Adjust the three **Red**, **Green**, **Blue** sliders (range −1.00 to +1.00) to balance the contribution of each channel in the grey tones.
4. Click **Apply**.

> Red at +1.00 with blue at −1.00 gives a dramatic result, with a dark sky and light skin tones — the equivalent of a red filter on film.

---

### Vignette

The **Vignette** button darkens (or lightens) the edges of the photo to
concentrate the eye on the subject. A dialog opens and handles appear on the
image.

- **Strength** (0.00 to 1.00): how strong the effect is. On a photo that does not
  have a vignette yet, the tool opens at **0.50**: the effect is therefore visible
  as soon as it opens, without having to hunt for the slider. Bringing the
  strength back to 0 leaves the photo untouched, and **Cancel** returns to the
  previous state.
- **Colour**: **Black** (darkens) or **White** (lightens, a “halo” effect).
- **Handles on the image**:
  - **inner circle** (dotted): where the fade starts;
  - **outer circle**: where the fade ends;
  - **round handle at the top**: rotates the ellipse;
  - **cross in the middle**: moves the vignette.
- **Apply** confirms the setting, **Cancel** restores the previous state.

---

### Geometry

#### Rotation

- **↺ -90°**: rotates by 90° anticlockwise.
- **↻ +90°**: rotates by 90° clockwise.

#### Straighten

Corrects the tilt of the horizon. Range: **-10° to +10°**.

1. Click **Straighten**.
2. A reference grid appears over the image.
3. Adjust the angle with the slider until the horizon lines up with the grid lines.
4. Click **Apply**.

#### Crop

1. Click **Crop** in the editing panel.
2. The viewer switches to **crop mode** (the complete original image is displayed).

**Choosing a format:**

| Button | Format | Ratio |
|---|---|---|
| Free | Any quadrilateral | None |
| 10×15 landscape | Standard photo print, landscape | 3:2 |
| 10×15 portrait | Standard photo print, portrait | 2:3 |
| 13×18 landscape | Large print, landscape | 18:13 |
| 13×18 portrait | Large print, portrait | 13:18 |

**Setting the crop area:**
- **Click and drag** on the image: draws the crop area.
- **Corner handles**: resize the area.
- **Edge handles** (middle of each side): move one edge.
- **Click and drag in the middle**: moves the area without resizing it.
- **Wheel**: zooms in the viewer for more precision.

**Confirming or cancelling:**
- **✓ Confirm the crop** (or **Enter**): applies the crop.
- **✕ Cancel** (or **Esc**): cancels without changing anything.

> To re-crop a photo already cropped, click **Crop** again: the complete original image is displayed with the previous area already in place, ready to be changed.

#### Mirror

- **Mirror H**: flips the image horizontally (left-right symmetry).
- **Mirror V**: flips the image vertically (top-bottom symmetry).

---

### Red eyes

1. Click **Red eyes** in the editing panel.
2. Click directly on each affected eye in the photo — a correction circle appears.
3. Adjust the **Size** slider if needed (0.5% to 8% of the smallest dimension of the image).
4. **Clear all** removes every correction placed; **Done** (or **Esc**) leaves the mode without losing anything.

---

### Decorative frame

The **Frame** button opens a gallery of previews of the current photo, one per pattern. Click a preview to choose the pattern; the preview in the viewer updates live.

| Pattern | Description |
|---|---|
| **Flat surround** | A flat fill of a single colour, with no relief (black, white or any colour) |
| **Simple** / **Double** | Moulding in relief, one or two bands separated by a gap |
| **Gilt baroque** | Acanthus and shells carved into a gilded ogee, with worn gold |
| **Egg-and-dart** | A classical frieze: eggs and darts, a row of beads and olives |
| **Greek key** | A continuous Greek meander, rosettes in the corners |
| **Art deco** | Silvered steps, chevrons and bars of the 1930s |
| **Carved wood** | Walnut hollowed with gadroons and fillets, with visible grain |
| **Vine leaves**, **Roses**, **Flowers** | Carved foliage surrounds (patinated bronze, carmine lacquer, painted porcelain) |
| **Metallic**, **Highlights** | Imitated materials (brushed metal and rivets, varnished lacquer) |

> The frame is added **around** the photo: it covers no pixel of the image, which simply comes out larger on export. Two exceptions, described below: the foliage surrounds, a few motifs of which bite into the edge of the image, and the second frame of the flat surround.

The patterns are carved, not drawn: each one is lit like a real moulding (light coming from the top left, shadows in the hollows, patina and wear on the gilding). They therefore need a little thickness to read — choosing one of them **automatically raises** the thickness if it is too small, and every thumbnail of the gallery shows exactly the frame you get by clicking it.

**Settings**:
- **Thickness** (named **Outer frame** for Simple and Double): available for every pattern. **Gap** and **Inner frame**: for Flat surround, Simple and Double. All of them are set as a percentage of the short side of the photo — the same setting therefore looks the same on a thumbnail and on a full-resolution export.
- **Colour**: black and white shortcuts, or a free colour picker (Flat surround, Simple, Double).
- **Fill style** (solid, gradient, glitter) and **second colour**: reserved for the Simple and Double patterns.

#### Foliage surrounds that spill over

On **Vine leaves**, **Roses** and **Flowers**, a few tendrils, roses or bunches go *over* the edge of the photo, with their drop shadow, as on a carved frame that bites into the canvas. They are deliberately irregular — a spill at regular intervals would be a frieze again — and stay attached to the band: the centre of the image is never touched. There is nothing to set, the pattern brings it; cropping, red eyes, annotations and faces keep working on the whole photo.

#### Second frame of the flat surround

For the **Flat surround** only, tick **Second frame over the photo**: a line is drawn *on* the photo, at a distance from the edge — the strip of image left visible between the two frames is exactly the intended effect. **Gap** sets that distance and **Second frame** its thickness.

That line can take the shape of **ironwork**:

| Ironwork | Effect |
|---|---|
| **Plain line** | A clean line (the default) |
| **Corner scrolls** | Forged scrolls in the four corners |
| **Running scrollwork** | Corner scrolls + motifs repeated along the sides |
| **Twisted bar** | A twisted bar, with finials in the corners and in the middle of the sides |
| **Forged studs** | A row of round-headed studs, larger in the corners |

- **Rendering**: **Light relief** (shade and light on the metal) or **Flat colour** (a solid silhouette, more graphic). No effect on the plain line, always rendered flat.
- **Ornaments**: the size of the motifs, from 40% to 250%. The ornaments grow towards the inside of the photo — they never spill onto the outer frame.

> Nothing is saved until you have clicked **Apply** in the gallery, then **Apply** in the editing panel.

---

### Annotations

A **non-destructive** drawing and text layer, laid over the photo — independent of the tonal corrections and of the geometry, and kept separately in the edits of the photo.

1. Click **Annotations** in the editing panel.

**Available tools:**

| Tool | Effect |
|---|---|
| Pen | Freehand stroke |
| Line | Straight line between two points |
| Curve | Click the successive waypoints, **double-click** to confirm the path |
| Rectangle | Rectangular shape (outline and/or fill) |
| Ellipse | Elliptical shape (outline and/or fill) |
| Text | Text box editable directly on the photo |
| Selection | Selects an existing item to move it, resize it or change its style |

**Style** (a panel that adapts to the selected tool):
- **Colour** of the stroke or of the text, stroke **thickness** (% of the image).
- For the shapes: **fill colour**, **opacity**, and **blur** of the photo under the shape.
- For the text: font, size (% of the image), **B** (bold), **I** (italic), colour.

**Changing or deleting:**
- With the **Selection** tool, click an item to select it (resize handles), or drag it to move it.
- **Double-click** an existing text: reopens the editor in place.
- **Delete the selection** (or the **Del** key): deletes the selected item. **Clear annotations**: removes the whole layer from the photo.

**Showing / hiding the layer:**
The **✏ Annotations** button at the top of the window (next to the EXIF button, visible as soon as a photo is open) shows or hides the layer without deleting anything. That setting is not saved: it only lasts for the current session.

> Annotations are included in the export and in the save of the edited image to disk, like the other edits — unless the layer is hidden through **✏ Annotations** at the moment of the export.

---

### Undo / Redo

| Button | Shortcut | Effect |
|---|---|---|
| **↩** | Ctrl + Z | Undoes the last edit |
| **↪** | Ctrl + Y | Redoes the undone edit |

The history keeps the **last 20 actions** in memory, and up to **50 states** are saved on disk — they are restored the next time the photo is opened.

### Resetting every edit

At the bottom of the editing panel, two complementary buttons:

- **Reset every edit** — removes at once every edit and the history of the current photo, without asking for confirmation. The original file on disk is never modified.
- **Restore every edit** — puts back the edits removed by the last **Reset**. It is disabled automatically as soon as a new edit is applied after the reset (the saved state is no longer restorable in that case).

---

## 8. Albums, favourites, ratings and keywords

### Favourites

- In the **grid**: right-click a thumbnail › **Mark as favourite**.
- In the **viewer**: click **♡**.
- To see all your favourites: click **♡ Favourites** in the Albums section of the sidebar.

### Ratings

- In the **grid**: right-click a selection › **Rate** › choose from ★ to ★★★★★, or **Clear the rating**.
- In the **viewer**: click one of the 5 stars in the toolbar, or use the **1** to **5** keys (**0** to clear the rating).
- A rated photo shows a star badge (e.g. “★★★”) at the bottom left of its thumbnail in the grid.
- To see the rated photos: in the Albums section of the sidebar, **★ By rating** can be collapsed and expanded — click the header to list every rated photo (≥ 1 star), or expand it and pick a level (5★ to 1★) to show only the photos rated at least that many stars. The [advanced search](#advanced-search) also lets you filter on a precise minimum rating.

### Keywords

- In the **grid** or the **viewer**: right-click a selection › **Keywords…** opens an editing dialog with completion on the keywords already used in the library.
- The keywords of a photo appear at the top of the EXIF panel (section 6).
- Keywords are **kept** across a forced rescan of the folder (like favourites and ratings) — never overwritten by the metadata of the file.
- To see every photo carrying a given keyword: type it in the sidebar filter then click **🏷 By keyword** in the Albums section (an exact keyword match, not a partial search).

### Special albums (always present, cannot be deleted)

| Album | Icon | Effect |
|---|---|---|
| **Timeline of every photo** | ★ | Shows the whole library in the [timeline/ribbon view](#5-timeline-mode-ribbon-view) |
| **Favourites** | ♡ | Photos marked as favourites |
| **Videos** | ▶ | Every video of the library |
| **By file name** | 🔍 | The result of the text typed in the sidebar filter (see [section 9](#9-search-and-filtering)) |
| **By rating** | ★ | Collapsible — header: photos rated ≥ 1; 5 sub-levels (5★ to 1★) for a precise minimum rating (see [Ratings](#ratings) above) |
| **By keyword** | 🏷 | Photos carrying the keyword typed in the sidebar filter (see [Keywords](#keywords) above) |

### Creating a custom album

**From the sidebar:**
1. Click the **+** button in the Albums header of the sidebar.
2. Type a name.

**From the grid (with photos already chosen):**
Right-click a thumbnail (or a selection) › **Create a new album with…** — type the name of the new album; it is created and filled straight away with the selected photo(s).

### Adding photos to an existing album

Right-click a thumbnail (in the grid **or** in the People view) › **Add to an album…** — a list of the existing albums (with their photo count) appears; double-click the target album or select it then confirm.

> If no custom album exists yet, a message invites you to create one first through the Albums panel.

### Removing photos from an album

In the view of a custom album:

- Select one or more photos, then press **Del**, or right-click › **Remove from the album** (**Remove the photos from the album** for a multiple selection).
- Only the link with the album is removed: **the file and the photo stay untouched** on disk, in the catalog and in the view of their folder.
- The same behaviour applies in the **viewer** opened from the album (**Del** key or right-click › **Remove from the album**).

> To really delete a file from the disk, do it from the view of its **folder** (or any other non-album view), where **Del** keeps its usual meaning of sending to the recycle bin.

### Deleting an album

Right-click a custom album in the sidebar › **Delete the album…** — a confirmation states the number of photos concerned and reminds you that **the photos stay untouched** in the catalog and on disk, only the album is deleted. *(The 6 special albums do not offer this option.)*

### Opening an album

Click its name in the Albums list of the sidebar.

---

## 9. Search and filtering

The filter field sits at the top of the **sidebar** (placeholder *“🔍 Filter folders, people and files…”*).

- Type a term: it filters the **folder** tree and the list of identified **people** in the sidebar **instantly** (on every keystroke, with no delay).
- The same text feeds the special album **🔍 By file name**, which searches the **whole catalog** by file name, camera make or camera model (a photo can therefore appear in that result even if the term you typed matches its camera rather than its file name).
- A **✕** button built into the field clears the filter.

### Advanced search

To combine several criteria at once, open the **Advanced search** dialog: menu **File › Advanced search…** (**Ctrl+F**), or the **🔎** button next to the sidebar filter field.

Available criteria, freely combinable:

| Criterion | Detail |
|---|---|
| **Date range** | Start date / end date |
| **Camera** | Make/model, list preloaded from the photos already catalogued |
| **Person** | An identified person (see [section 13](#13-face-recognition)) |
| **Folder** | A watched folder, searched recursively through its subfolders |
| **Min. rating** | ★ to ★★★★★ |
| **Keywords** | One or more keywords (completion on the existing keywords) |
| **Favourites only** | Tick box |
| **Media type** | Photos, videos, or both |

The results appear in the grid, as for any other context (folder, album, person…). The search runs in the background; the lists of the dialog (cameras, people, keywords) are preloaded before it opens so that it stays responsive.

---

## 10. Moving photos

### Drag and drop onto a folder

1. In the grid, **click a thumbnail** and hold the mouse button down.
2. Drag onto a **folder in the sidebar** (the folder is highlighted).
3. Release the mouse.

To move **several photos** at once:
1. Select them with Ctrl+Click or Shift+Click.
2. Drag one of the selected photos onto the destination folder.
   Every photo of the selection is moved.

You can also use right-click › **Move to…** (grid or viewer) to choose the destination without dragging and dropping.

After the move:
- The file is moved on disk.
- The catalog, the edits and the thumbnails are updated automatically.
- The grid switches to the **destination folder** to confirm the result.

> If a photo with that name already exists in the destination folder, the move is cancelled for that file and an error message is shown.

---

## 11. Saving and exporting your photos

There are two ways to get an edited image out of the application, depending on what you need.

### Saving the edited image (one file at a time)

Right-click a thumbnail or in the viewer › **Save the edited image to disk**. A dialog box offers two options:

- **Overwrite the original file** *(ticked by default)* — a warning reminds you that the action cannot be undone. A **“Copy the original into .tmp_originals before overwriting”** tick box (ticked by default) keeps a timestamped backup copy of the original file in a hidden `.tmp_originals` subfolder before it is replaced.
- **Save to another location…** — opens Windows File Explorer with a suggested name (`originalname_edited.jpg`), to keep the original untouched and create an edited copy next to it.

In both cases:
- The output file is saved at full resolution (JPEG quality 95, or PNG depending on the extension chosen).
- **The file date (creation + modification) is taken from the original**, not from the moment of saving — the output file therefore keeps the date the photo was taken.
- If you chose to overwrite the original, the edits stored in the application are removed (they are now “baked” into the file) and the thumbnail/preview are refreshed.

### Exporting several photos (the ⬆ Export button)

The **⬆ Export** button of the main toolbar exports either the photo shown in the viewer, or the whole selection of the grid. The **“Export N photo(s)”** dialog offers:

- **Destination folder** — an editable field + a **Browse…** button (`Documents\Pictures\PixelPhotoManager\Export` by default).
- **Export size** (one choice only):

| Option | Max. resolution | JPEG quality | Estimated size |
|---|---|---|---|
| Maximum size — original resolution | None | 95 | — |
| Large (~4 Mpx) | 4,000,000 px | 98 | 600–1,600 kB |
| Medium (~2 Mpx) | 2,000,000 px | 94 | 320–800 kB |
| Small (~500 kpx) | 500,000 px | 90 | 75–300 kB |

Every exported photo is converted to **JPEG**, with the edits applied; if a name clashes in the destination folder, a numeric suffix is added automatically. As for saving, **the date of the original file is carried over to each exported file**. Once the export is finished, the destination folder opens automatically in File Explorer.

---

## 12. Slideshow

Start a slideshow from **View › Slideshow** or with the **F5** key.

The slideshow opens **full screen** and runs through the photos of the current context.

### Starting point

| Situation when starting | Starting photo |
|---|---|
| Viewer open | The photo currently displayed |
| Timeline mode (ribbon) | The photo at the **centre** of the ribbon |
| Any other view | The oldest photo of the folder |

### Ken Burns effect

Each photo is animated with a **slight zoom and a slow pan** (the Ken Burns effect):
- The zoom varies from 0 to 8% over the whole display time.
- The direction of the movement is random, with a preference for **horizontal and diagonal** moves.
- Photos whose aspect ratio does not match the screen are shown with **black bars** (letterbox / pillarbox) — they are never cropped.

### Controls (bar at the bottom, shown when the mouse moves)

| Control | Effect |
|---|---|
| **◀ Previous** | Go to the older photo |
| **Next ▶** | Go to the newer photo |
| **−** / **+** | Decrease / increase the display interval (1 s to 60 s, in steps of 1 s) |
| **⏸ / ▶** | Pause / resume the automatic advance |
| **✕** | Leave the slideshow |

The bar disappears automatically after 5 seconds of inactivity and comes back at the slightest mouse movement.

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| **←** or **↑** | Older photo |
| **→** or **↓** | Newer photo |
| **Space** | Pause / Resume |
| **Esc** | Leave the slideshow |

### Screen saver

As long as the slideshow is open, **the screen saver and the display sleep are disabled** — watching photos go by without touching the keyboard or the mouse would otherwise look like inactivity to Windows. The usual setting resumes automatically when the slideshow closes (including if the application is closed).

---

## 13. Face recognition

> Face recognition needs optional dependencies (InsightFace/buffalo_l, scikit-learn, hdbscan). If they are not installed, this section is not available.

### The “Faces” panel of the viewer

Open a photo then show the **Faces** panel (next to the EXIF panel — the two are mutually exclusive).

- **“All” button**: frames every face detected on the photo.
- **Face thumbnails**: one card per detected face, named faces sorted first. The name shown under each thumbnail gives the person, `“Group N”` (an unnamed cluster), `“Separated”` (isolated by hand) or `“Unknown”`.
  - **Click**: selects/deselects the face (highlighted on the photo).
  - **Double-click** on a named face: opens the detailed view of that person.
  - **✕** on the thumbnail: ignores this face (hidden from the UI and from the grouping, recoverable).
  - **Right-click**: **Identify this person…**, **Identify this group…**, **Unassign the group**, **Ignore this face**.
- **➕ Add a person**: switches to a mode where you draw a rectangle by hand on the photo (for a face not detected automatically), confirmed with **✓ Confirm the position** (or Enter) / **✕ Cancel** (or Esc), then the choice of the name.
- **Ignored faces… (N)**: lists the ignored faces of the photo, with position/size, and a **Restore** button on each line — that dialog is how a face ignored by mistake is recovered.

### The “People” view — unidentified groups

Reachable through the dedicated icon/button that replaces the photo grid.

- **Group cards**: a thumbnail + the number of faces (or “Isolated” for a single face), with a **person suggestion** if the system finds one (e.g. *“≈ Name (82%)”*).
  - Click: cumulative multiple selection; Shift+click: extended selection.
  - Double-click: opens the photos of the group.
  - Right-click: **Identify this person…** / **Identify this group…**, **Ignore this face/this group**, and in multiple selection **Assign (N selected)**.
  - **✓ / ✗ buttons laid over each thumbnail** (on every card, not only those with a suggestion): **✓** accepts the suggestion directly in one click if one is offered, otherwise opens the identification dialog; **✗** ignores the face (an isolated card) or the whole group.
- **Suggestion sections** (“≈ Probably the same person” / “≈ Probably *Name*”) with the header buttons **Accept**, **Assign to…**, **Ignore** for the whole section at once.
- **“Isolated faces” section** at the bottom of the page.
- **Action bar** (as soon as a group is selected): **View the photos**, **Assign to…**, **Ignore**, **✕** (deselect everything).
- **Merge group N**: choose another group in an illustrated list then confirm.
- Pagination: **Load N more (N left)**.

### Detailed view of a person

Opened by double-clicking a named person.

- **“Awaiting verification” section** (unconfirmed suggestions): **✗ Reject all** / **✓ Accept all** buttons, or **✓**/**✗** on each thumbnail.
- **Confirmed section**: right-click one or more selected faces › **Reassign to another person…**, **Unassign from the person**, **Use this face as the main thumbnail**, **Add to an album…**, **Create a new album with…**.

### The “Faces” menu (menu bar)

| Option | Effect |
|---|---|
| **Import from Picasa…** | Walks the `.picasa.ini` files of your folders and imports the names/face regions defined in Picasa; offers an **“Also import the Picasa edits (rotation, cropping, brightness…)”** tick box. Never overwrites an assignment already made by hand. To be done once (the menu is greyed out afterwards). |
| **Reset and reindex…** | Two options: **“Reset the groups only — fast”** (keeps the detected faces, just redoes the grouping) or **“Full reset + reindexing — slow”** (erases everything and restarts detection, may take several hours) |
| **Group faces…** | Restarts the clustering of the unidentified faces (an estimated duration is shown before starting) |
| **Error review…** | Lists the photos whose detection failed (timeout/crash), with a **⟳ Retry** button on each line |
| **Back up recognition data…** | Immediately creates a backup (an archive) of the current state of the faces, groups and people |
| **Manage backups…** | Lists the existing backups (date, size) with **Restore** or **✕ Delete** on each line, and **＋ Create a backup** |
| **Counters…** | Statistics: identified people/faces, awaiting confirmation, unknown; Picasa import (imported/merged/pending); totals (detected, ignored by size, groups) |

### From a photo

- Viewer, right-click › **Force a new detection with no size limit** — restarts detection on this photo ignoring the minimum-size filter, without losing the identifications already made.
- Grid, right-click a photo in error › **Retry face identification**.

---

## 14. Duplicate detection

Duplicate detection runs **automatically in the background**, with nothing to do on your side: it starts after every scan of the library (videos are excluded) to spot duplicate photos, including resized, edited (colour/brightness) or cropped versions. There is no manual trigger and no end-of-analysis report any more — check the state of the detection through **Tools › Duplicate status…**.

### How it works

The analysis runs in two passes (no option to set):

1. **Perceptual hash (pHash)** — finds exact, resized or lightly edited duplicates.
2. **Keypoints (ORB + RANSAC)** — applied only to the photos not grouped at step 1, it additionally finds **crops** (up to ~60% of the area cropped away).

It is **incremental**: a pair of photos already compared during an earlier pass is never compared again, only the pairs involving a new or changed photo are. A new pass after a few photos have been added is therefore fast, even on a large library. The analysis runs in the background at reduced priority, so as not to slow down the rest of the application while you use it.

Duplicate photos are marked with an **orange ⧉ badge** in the grid and in the viewer (see [section 4](#4-the-photo-grid)) — the application never deletes or merges anything automatically, it is up to you to decide case by case.

### Duplicate status (Tools › Duplicate status…)

This window shows a snapshot of the current state:

- The number of duplicate groups and of photos concerned.
- Whether an analysis is running (“Analysis running…”) or the date of the last finished check.
- If corrupted files have been found (see below), their number with a **See the list…** button.
- The **View the groups** (opens the grid of duplicate groups), **Check now** (forces a new pass immediately — disabled if an analysis is already running) and **Close** buttons.

### Grid of duplicate groups (the “Duplicates” button of the sidebar)

The **Duplicates** button of the sidebar (with a badge giving the number of groups found) opens a dedicated grid listing **every** duplicate group at once — one card per group, with the thumbnail of the first copy and the number of copies.

- **Double-click** a card: opens the copies of the group in the viewer, for a quick comparison (previous/next navigation limited to the members of the group).
- **✕** on a card: **dissolves the whole group** (deletes no file) — the card disappears from the grid and the badge goes down. That dissolution is **persistent**: the group will never be formed again as long as none of its members changes (a new photo similar to one of them is, however, still detected normally).
- A **Check now** button at the top of the grid restarts an analysis without going back through the Tools menu.

### Corrupted files found during the analysis

A file that cannot be read during the analysis (damaged JPEG, interrupted copy…) is not silently ignored: it is counted and reported (a ⚠ counter in **Tools › Duplicate status…**, a **See the list…** button). That list offers two actions:

- **Repair…** — after confirmation, tries to save a clean copy of each file through a decoder more tolerant than the one used for the analysis (PIL in truncation-tolerant mode, then the JPEG codec of Qt). The original is backed up beforehand into a hidden `.tmp_originals` folder next to the file, and the Windows modification **and** creation dates are preserved identically on the repaired copy. A summary gives the number of repaired files; those that could not be repaired (corruption too severe for the available decoders) are listed in a timestamped text file (`fichiers_corrompus_YYYYMMDD_HHMMSS.txt`), whose location stays reachable through **Tools › Problem history…** (see [section 16](#16-other-tools)).
- **Delete…** — after confirmation, sends the selected files to the **Windows recycle bin** (recoverable) and removes them from the catalog, the thumbnails and the faces attached to them.

---

## 15. Syncing creation dates with EXIF

Menu **Tools › Sync creation dates with EXIF…**

**Why:** during a transfer or a copy of files, Windows sometimes sets today's date as the “creation date”, overwriting the real capture date held in the EXIF data.

**What the tool does:** it walks the whole catalog and, when the EXIF date differs from the Windows creation date (beyond a 2-second tolerance), it **replaces the creation date of the file** with the EXIF date. Photos with no valid EXIF data, or whose file cannot be found, are simply skipped and counted as such.

> ⚠ This operation modifies the system metadata of the original files (the dates), not the content of the image.

A single **Start** button runs the processing over the whole library, with a progress bar. At the end, a summary (“N file(s) updated · N skipped or in error”) is shown, with an **Open the CSV report** button detailing the action taken for each file.

---

## 16. Other tools

### Thread journal (Tools › Thread journal…)

A diagnostic tool to watch the background processing (scan, face indexing, clustering, thumbnails…) — useful if the application seems slow or stuck.

- **Execution summary**: overall status and per-thread status (✓ OK, ● SLOW, ● TOO LONG, ✗ ERROR, ● RUNNING).
- **Summary by thread**: number of runs, average/max duration, errors.
- **Raw events**: a detailed journal that can be filtered (by thread, by keyword), with a **▶ Live** button for automatic refreshing.
- **Problem report…** (a copyable textual diagnostic) and **Export CSV…** buttons.
- **🗑 Clear** empties the journal (a confirmation is asked).

### Problem history (Tools › Problem history…)

Keeps a trace of every duplicate analysis that ran into corrupted files (see [section 14](#14-duplicate-detection)).

- One line per analysis: date, number of corrupted files found, number repaired.
- An **Open the list…** button on each line: opens the text file listing the files that could not be repaired during that analysis (disabled if all of them were repaired, or if the file has since been deleted).

### External applications (Tools › External applications…)

Lets you add shortcuts to third-party software (an external editor, a RAW viewer, etc.), reachable afterwards as icons in the viewer toolbar.

- **Add…**: choose an executable (`*.exe`) then a display name (used as the tooltip).
- **Remove**: takes the selected application out of the list.

Clicking the matching icon in the viewer opens the application with the current photo as an argument.

### Settings (Tools › Settings)

A dialog with four categories:

- **Language**: the language of the interface and of the built-in help (English,
  Français, Deutsch), applied at the next start. The same setting as the flag
  button of the top bar, see [Interface language](#interface-language).
- **Face recognition**: a **“Similarity tolerance”** slider (25% to 70%) — it controls how alike two faces must be to be placed in the same group. A textual indicator goes with the slider (very strict → very loose groups). Changing this setting automatically restarts the grouping of the faces when the dialog closes.
- **Video player**: a choice between **“System default player”** (the Windows application associated with video files) or **“Custom player”** (the path to an executable, VLC or MPC-HC for instance, through **Browse…**). That choice decides what the **▶ Open the video** button of the viewer opens.
- **Performance**: the **CPU load of the background processing** (duplicate
  detection, face indexing) — **Frugal** (recommended), **Balanced** or
  **Maximum**. The limit is lifted automatically when the window is not in the
  foreground.

---

## 17. Keyboard shortcuts

### General

| Shortcut | Action |
|---|---|
| **Ctrl + Q** | Quit the application |
| **Ctrl + F** | Advanced search… |
| **F1** | Open the help |
| **F9** | Show/hide the sidebar |
| **F11** | Full screen |
| **F5** | Start the slideshow |

### Grid

| Shortcut | Action |
|---|---|
| **Ctrl + A** | Select every photo |
| **Del** | Delete the selected photos (with confirmation) — inside an album: remove from the album, files untouched |

### Timeline mode (ribbon)

| Shortcut | Action |
|---|---|
| **← / →** | Move by one photo |
| **↑ / ↓** | Move by three photos |
| **Wheel** | Scrolling with inertia |
| **Del** | Delete the centre photo (or the selection) |

### Viewer

| Shortcut | Action |
|---|---|
| **← / ↑** | Previous photo |
| **→ / ↓** | Next photo |
| **I** | Show/hide the EXIF panel |
| **F** | Fit to window |
| **Z** | Zoom 100% |
| **Ctrl + Wheel** | Zoom in / out |
| **Esc** | Back to the grid |
| **1** to **5** | Rate the photo (★ to ★★★★★) |
| **0** | Clear the rating |

### Slideshow

| Shortcut | Action |
|---|---|
| **← / ↑** | Older photo |
| **→ / ↓** | Newer photo |
| **Space** | Pause / Resume |
| **Esc** | Leave the slideshow |

### Crop mode

| Shortcut | Action |
|---|---|
| **Enter** | Confirm the crop |
| **Esc** | Cancel the crop |
| **Wheel** | Zoom in the viewer |

### Annotation mode

| Shortcut | Action |
|---|---|
| **Del** | Delete the selected annotation item |
| **Enter** | Confirm a curve being drawn |
| **Esc** | Cancel the path being drawn (annotation mode stays active) |

### Editing panel

| Shortcut | Action |
|---|---|
| **Ctrl + Z** | Undo the last edit |
| **Ctrl + Y** | Redo |

---

## 18. Where your data is stored

Every piece of application data lives in:

```
%LOCALAPPDATA%\PixelPhotoManager\
```

That is typically: `C:\Users\YourName\AppData\Local\PixelPhotoManager\`

| File / folder | Contents |
|---|---|
| `catalog.db` | The index of all your photos and videos (paths, EXIF, metadata, albums) |
| `thumbnails.db` | The cache of the generated thumbnails (images and first video frames) |
| `edits.db` | All your edits and their history |
| `faces.db` | Detected faces, groups/clusters, identified people |
| `config.json` | Watched folders and interface preferences (including the Settings) |
| `logs\pixelphotomanager.log` | The application journal (for troubleshooting) |
| `problems_history.jsonl` | The history of the corrupted files found/repaired (**Tools › Problem history…**, see [section 16](#16-other-tools)) |
| `fichiers_corrompus_YYYYMMDD_HHMMSS.txt` | The list of the corrupted files not repaired during a duplicate analysis |
| CSV report of the EXIF sync | Generated on every run of the date synchronisation tool |
| Face recognition backups | Archives created through **Faces › Back up recognition data…** |

> **Your original photos and videos are never modified** by the edits or by face recognition. You can delete `edits.db` to erase every edit and start again from scratch, or delete `catalog.db` to force a full reindexing.

In each photo folder, a hidden **`.tmp_originals`** subfolder may appear: it holds the backup copies of the files overwritten through **Save the edited image to disk** (only if you ticked the backup option), or those of the corrupted files before a repair was attempted (see [section 14](#14-duplicate-detection)).

### Supported formats

**Images:**
`.jpg` · `.jpeg` · `.png` · `.tiff` · `.tif` · `.webp` · `.bmp` · `.gif` · `.heic` · `.heif` · `.cr2` · `.nef` · `.arw` · `.dng` · `.orf` · `.rw2`

**Videos:**
`.mp4` · `.mov` · `.avi` · `.mkv` · `.wmv` · `.webm` · `.m4v` · `.3gp` · `.flv` · `.ts` · `.mts` · `.mpg` · `.mpeg` · `.vob`

> **RAW and HEIC/HEIF**: decoding is built into the application (the `rawpy` and `pillow-heif` libraries), no driver and no third-party software is needed. For a RAW file, the display and the export use the JPEG preview embedded by the camera (not a full demosaicing of the sensor) — the resolution available is therefore that of that preview.

---

## Appendix — Solving common problems

| Problem | Solution |
|---|---|
| Photos do not appear after being copied in | Right-click the folder in the sidebar › **Scan now**, or use **Tools › Folders…** › **⟳ Rescan** |
| The thumbnail of an edited photo is out of date | Open the photo, the edits are applied automatically |
| The application is slow at start-up | Normal during the first scan of a large library; the next scan will be far faster (only new photos are analysed) |
| A folder moved by hand (outside the application) is no longer found | Use **Stop watching this folder** then **File › Add a folder…** to register it again |
| Recovering the original of an edited photo | The original file was never modified — just clear the edits through **Undo** (↩) back to the initial state |
| Recovering an original overwritten by mistake | If it was backed up (the box ticked when saving), it is in the hidden `.tmp_originals` subfolder of the folder concerned |
| **Duplicate status…** reports corrupted files | Use the **Repair…** (or **Delete…**) button offered in the list (see [section 14](#14-duplicate-detection)); the original is always backed up into `.tmp_originals` before a repair |
| The thumbnail of a video is black | OpenCV could not read the video — check that the codec is installed on your system |
| An operation seems stuck or unusually slow | Open **Tools › Thread journal…** to see which background processing is running and its status |
| Duplicate detection never seems to run | The `imagehash` and `Pillow` modules are required; without OpenCV/numpy, only crop detection (Tier 2) is unavailable, the rest keeps working |
| Face recognition no longer detects anything | Check through **Faces › Error review…** whether the files concerned failed detection (timeout/crash), and use **⟳ Retry** |
