# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Decorative frames — a non-destructive edit applied AROUND the photo.

The frame never encroaches on the image: the photo is pasted as-is in the
centre of a canvas enlarged by ``border_px()`` pixels on each edge. Every width
is expressed as a fraction of the short side of the photo, so that one same
setting renders identically on a 220 px thumbnail and on a full-resolution
export.

One single exception, explicitly requested: the **optional second frame** of the
``plain`` pattern (``frame_inner_enabled``) is painted ON TOP OF the photo, at
``frame_gap`` from the edge — the strip of image left visible between the two
frames is the intended effect. It does not enlarge the canvas and therefore does
not enter ``border_px()``/``content_box()``: the geometry of the interactive
tools stays that of the whole photo. That second frame carries an **ironwork**
(``INNER_MOTIFS``: a plain line, corner scrolls, running scrolls, a twisted bar,
forged studs), rendered in light relief or as a strict flat fill
(``frame_inner_relief``) and sized by the "Ornaments" slider
(``frame_inner_ornament``). The ornaments grow INWARDS from the line: they stay
inside the photo and never touch the outer band.

Two families of patterns:

- **parametric** (``plain``, ``simple``, ``double``) — the user chooses the
  colours and widths. ``plain`` is a plain flat fill of a single colour (black,
  white or free) with no relief whatsoever; ``simple``/``double`` add a fill
  style (solid / gradient / glitter), a moulding and, for ``double``, a gap and
  an inner frame;
- **decorative** (baroque, egg-and-dart, Greek key, art deco, vine, roses,
  carved wood, metal, reflections, flowers) — drawn entirely by code (not a
  single embedded image resource, hence nothing added to the packaging and a
  crisp rendering at any resolution).

Rendering of the decorative frames — this is a RELIEF engine, not a drawing:

1. a **moulding cross-section** (``_PROFILE_SEGMENTS``: edge, torus, scotia,
   ogee, fillet, rebate) gives the height of the band as a function of the
   distance to the edge;
2. the **ornaments are carved** into that height map
   (``_Carver``/``_CARVERS``: acanthus, shells, eggs, beads, Greek key,
   gadroons, roses, grape bunches…) rather than painted as a flat fill;
3. ``_shade_relief()`` lights the whole thing — normals deduced from the
   gradient, Lambertian diffuse, Blinn-Phong highlight, approximate occlusion of
   the hollows and the patina that settles in them (the red bole under the
   gilding, the verdigris of bronze).

Stage 3 is what makes the difference between a motif that reads as a sticker and
a motif that reads as carved material: an ornament laid down in flat colour
stays flat whatever its drawing, while the same ornament in relief takes the
light of the frame and casts its shadows into its own hollows.

The band is produced at a working resolution bounded by ``_WORK_MAX`` then
enlarged to the final size (a lit relief is a soft motif — the enlargement does
not show, whereas a full-resolution rendering would cost several seconds and
hundreds of MB on a 6000 px export). The ornaments are carved with ``_SS``
supersampling then downscaled, for lack of antialiasing in ``ImageDraw``.
"""
import logging
import math

from PIL import Image, ImageDraw, ImageFilter

from src.core.i18n import translate

logger = logging.getLogger(__name__)

# (identifier, displayed label)
FRAME_TYPES: list[tuple[str, str]] = [
    ("none",    translate("Frames", "None")),
    ("plain",   translate("Frames", "Flat surround")),
    ("simple",  translate("Frames", "Simple")),
    ("double",  translate("Frames", "Double")),
    ("baroque", translate("Frames", "Gilt baroque")),
    ("pearl",   translate("Frames", "Egg-and-dart")),
    ("greek",   translate("Frames", "Greek key")),
    ("artdeco", translate("Frames", "Art deco")),
    ("wood",    translate("Frames", "Carved wood")),
    ("vine",    translate("Frames", "Vine leaves")),
    ("roses",   translate("Frames", "Roses")),
    ("flowers", translate("Frames", "Flowers")),
    ("metal",   translate("Frames", "Metallic")),
    ("gloss",   translate("Frames", "Highlights")),
]

# A carved frame only makes sense from a certain thickness on: below ~8 % of
# the short side, the acanthus leaves or the egg-and-dart hold in a handful of
# pixels and boil down to mush. The dialog raises the width to that floor when
# a decorative pattern is chosen — visibly, in the slider, never quietly at
# render time (the slider would lie about the result).
DECOR_MIN_WIDTH = 0.08

FRAME_LABELS: dict[str, str] = dict(FRAME_TYPES)

# Patterns whose colours / widths are adjustable by the user.
PARAMETRIC_FRAMES = {"plain", "simple", "double"}

# Subset of the parametric frames offering a fill style (solid / gradient /
# glitter) and hence a second colour. ``plain`` is deliberately excluded from
# it: it is a flat fill of a single colour, with no relief.
STYLED_FRAMES = {"simple", "double"}

# Foliage frames a few motifs of which spill over the photo (cf. the
# "spills" section further down): the only ones, along with the second frame
# of ``plain``, to lay material on top of the image.
SPILL_FRAMES = {"vine", "roses", "flowers"}

# Ready-made colours offered next to the picker (hex identifier, label).
QUICK_COLORS: list[tuple[str, str]] = [
    ("#000000", translate("Frames", "Black")),
    ("#ffffff", translate("Frames", "White")),
]

# Ironwork of the second frame of "plain" (identifier, label). "line" is the
# historical motif — a plain line — and remains the default.
INNER_MOTIFS: list[tuple[str, str]] = [
    ("line",    translate("Frames", "Plain line")),
    ("corners", translate("Frames", "Corner scrolls")),
    ("scrolls", translate("Frames", "Running scrollwork")),
    ("twist",   translate("Frames", "Twisted bar")),
    ("studs",   translate("Frames", "Forged studs")),
]

INNER_MOTIF_LABELS: dict[str, str] = dict(INNER_MOTIFS)

# Motifs whose ornament size depends on the "Ornaments" slider.
ORNAMENTED_MOTIFS = {"corners", "scrolls", "twist", "studs"}

# Bounds of the "Ornaments" slider (scale factor of the motifs).
INNER_ORNAMENT_MIN = 0.4
INNER_ORNAMENT_MAX = 2.5

# Rendering of the ironwork: light relief (a light/dark bevel) or a strict flat fill.
INNER_RELIEFS: list[tuple[bool, str]] = [
    (True,  translate("Frames", "Light relief")),
    (False, translate("Frames", "Flat colour")),
]

# Fill styles of the parametric frames
COLOR_STYLES: list[tuple[str, str]] = [
    ("solid",    translate("Frames", "Solid")),
    ("gradient", translate("Frames", "Gradient")),
    ("glitter",  translate("Frames", "Glitter")),
]

# Maximum rendering resolution of the band (cf. the module docstring).
_WORK_MAX = 2000
# Supersampling of the ornaments (polygons without antialiasing in PIL).
_SS = 2
# Maximum width of a frame, as a fraction of the shortest side (a safeguard:
# beyond it, the photo would vanish under its own frame).
_MAX_FRACTION = 0.30


# ------------------------------------------------------------------ utilities

def _hex_to_rgb(value, default=(255, 255, 255)) -> tuple[int, int, int]:
    """'#rrggbb' / '#rgb' → (r, g, b). Returns ``default`` if unreadable."""
    try:
        s = str(value).strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return default
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return default


def _shade(color, factor: float) -> tuple[int, int, int]:
    """Lightens (>1) or darkens (<1) an RGB colour."""
    return tuple(max(0, min(255, int(round(c * factor)))) for c in color)


def _mix(c1, c2, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def _attr_frac(edit, attr: str, default: float) -> float:
    try:
        v = float(getattr(edit, attr, default))
    except (TypeError, ValueError):
        v = default
    return max(0.0, min(_MAX_FRACTION, v))


def frame_type(edit) -> str:
    t = getattr(edit, "frame_type", "none") or "none"
    return t if t in FRAME_LABELS else "none"


def suggested_width(kind: str, current: float) -> float:
    """Width to offer when the user chooses ``kind``.

    A carved motif needs material: below ``DECOR_MIN_WIDTH``, the frieze no
    longer has the room to exist. The current width is therefore raised — never
    reduced, a wider choice remaining the user's own."""
    if kind in PARAMETRIC_FRAMES or kind == "none":
        return current
    return max(current, DECOR_MIN_WIDTH)


def _raw_total(edit) -> float:
    """Raw sum of the bands, before capping — serves as the denominator of the
    concentric split of the double frame, which must stay proportional even
    when the total is brought back down to _MAX_FRACTION."""
    kind = frame_type(edit)
    if kind == "none":
        return 0.0
    total = _attr_frac(edit, "frame_width", 0.05)
    if kind == "double":
        total += _attr_frac(edit, "frame_gap", 0.02)
        total += _attr_frac(edit, "frame_inner_width", 0.015)
    return total


def border_fraction(edit) -> float:
    """Total thickness of the frame, as a fraction of the short side of the photo,
    capped at ``_MAX_FRACTION`` (beyond it the photo would vanish under the
    frame — the per-band cap is not enough, three bands at the maximum would
    add up)."""
    return min(_MAX_FRACTION, _raw_total(edit))


def border_px(edit, width: int, height: int) -> int:
    """Thickness of the frame in pixels for a ``width`` × ``height`` photo."""
    frac = border_fraction(edit)
    if frac <= 0.0 or width <= 0 or height <= 0:
        return 0
    return max(2, int(round(frac * min(width, height))))


def inner_overlay_px(edit, width: int, height: int) -> tuple[int, int]:
    """Second frame of "plain", laid ON the photo: ``(gap, thickness)`` in px.

    Returns ``(0, 0)`` if the pattern has none. Unlike the outer frame, that one
    does not enlarge the canvas: it is painted on top of the image, at ``gap``
    px from the edge of the photo — the strip of image left between the two
    frames is precisely the intended effect. ``width``/``height`` are the
    dimensions of the PHOTO (outer frame excluded)."""
    if frame_type(edit) != "plain" or not getattr(edit, "frame_inner_enabled", False):
        return (0, 0)
    short = min(width, height)
    if short <= 0:
        return (0, 0)
    thick = int(round(_attr_frac(edit, "frame_inner_width", 0.015) * short))
    if thick <= 0:
        return (0, 0)
    gap = int(round(_attr_frac(edit, "frame_gap", 0.02) * short))
    # Safeguard: the second frame must never close in on itself at the centre
    # of the photo (the two settings add up, each capped on its own).
    limit = max(1, short // 2 - 1)
    gap = max(0, min(gap, limit - 1))
    thick = max(1, min(thick, limit - gap))
    return (gap, thick)


def inner_motif(edit) -> str:
    """Ironwork motif of the second frame ("line" if absent or unknown)."""
    motif = getattr(edit, "frame_inner_motif", "line") or "line"
    return motif if motif in INNER_MOTIF_LABELS else "line"


def inner_ornament_scale(edit) -> float:
    """Scale factor of the ornaments ("Ornaments" slider), clamped."""
    try:
        value = float(getattr(edit, "frame_inner_ornament", 1.0))
    except (TypeError, ValueError):
        value = 1.0
    return max(INNER_ORNAMENT_MIN, min(INNER_ORNAMENT_MAX, value))


def inner_relief(edit) -> bool:
    """True if the ironwork is rendered in light relief, false as a strict flat fill."""
    return bool(getattr(edit, "frame_inner_relief", True))


def content_box(edit, framed_w: float, framed_h: float) -> tuple[float, float, float, float]:
    """Inverse of ``border_px``: the area taken by the photo in a framed image.

    Returns ``(x, y, w, h)``. Of use to the viewer, which receives the already
    framed pixmap but must express the coordinates of the interactive tools
    (crop, red eyes, vignette, face boxes) in the space of the photo, not of the
    frame."""
    frac = border_fraction(edit)
    if frac <= 0.0 or framed_w <= 0 or framed_h <= 0:
        return (0.0, 0.0, float(framed_w), float(framed_h))
    # border_px rounds to the integer: it is solved continuously first (b = frac ×
    # short side of the content, framed = content + 2b), then the neighbouring
    # integer that gives exactly border_px back is looked for — without that the
    # inverse drifts by a pixel, and everything aligned on content_box (crop, face
    # bbox) slides by as much.
    framed_s = min(framed_w, framed_h)
    b = int(round(frac * framed_s / (1.0 + 2.0 * frac)))
    for cand in (b, b - 1, b + 1, b - 2, b + 2):
        if cand < 0 or 2 * cand >= framed_s:
            continue
        s = framed_s - 2 * cand   # short side of the content for this candidate
        if border_px(edit, s, s) == cand:
            b = cand
            break
    b = max(0, min(b, int((framed_s - 1) // 2)))
    return (float(b), float(b),
            max(1.0, float(framed_w) - 2 * b), max(1.0, float(framed_h) - 2 * b))


# ------------------------------------------------------------------ numpy maps

def _edge_distance(np, width: int, height: int):
    """(distance to the nearest edge, index of that edge) — float32 (h, w).

    The index of the edge (0 top, 1 left, 2 right, 3 bottom) cuts the band into
    four trapezoids joined at 45° in the corners, exactly like the mouldings of
    a real frame."""
    ys = np.arange(height, dtype=np.float32)[:, None]
    xs = np.arange(width, dtype=np.float32)[None, :]
    d_top = np.broadcast_to(ys, (height, width))
    d_bot = np.broadcast_to((height - 1) - ys, (height, width))
    d_left = np.broadcast_to(xs, (height, width))
    d_right = np.broadcast_to((width - 1) - xs, (height, width))
    dist = np.minimum(np.minimum(d_top, d_bot), np.minimum(d_left, d_right))
    side = np.where(
        d_top <= dist, 0,
        np.where(d_left <= dist, 1, np.where(d_right <= dist, 2, 3)),
    ).astype(np.int8)
    return dist, side


def _smooth_noise(np, width: int, height: int, cells_x: int, cells_y: int, rng):
    """Smooth noise (0-1) obtained by bicubic upscaling of a small grid."""
    cells_x = max(2, cells_x)
    cells_y = max(2, cells_y)
    small = (rng.random((cells_y, cells_x)) * 255).astype("uint8")
    img = Image.fromarray(small, mode="L").resize((width, height), Image.BICUBIC)
    return np.asarray(img, dtype="float32") / 255.0


def _gauss(np, arr, radius: float, hi: float = 1.4):
    """Gaussian blur of a float map (0..``hi``), through PIL.

    The quantisation to 8 bits is of no consequence here: this map only serves
    the occlusion of the hollows, not the relief itself."""
    if radius < 0.5:
        return arr
    img = Image.fromarray(np.clip(arr * (255.0 / hi), 0, 255).astype("uint8"), mode="L")
    img = img.filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(img, dtype="float32") * (hi / 255.0)


# ------------------------------------------------------------------ moulding profiles
#
# A real moulding has a CROSS-SECTION — outer edge, torus, hollow scotia,
# ogee, fillet, rebate — and it is that, far more than the colour, which makes
# a frame "hold" under the light. Each profile is described by segments
# (relative width, shape, start height, end height, amplitude), sampled once
# into a table then interpolated over the distance-to-edge map: t = 0 on the
# outer edge, t = 1 against the photo.

_PROFILE_N = 320

# Studio light: high, on the left, slightly to the front. Every moulding of
# the project is lit by it — that is what makes the frames consistent with one
# another (and with the drop shadow the eye expects at the top left).
_LIGHT = (-0.4364, -0.5624, 0.7025)
_HALF = (-0.2564, -0.3305, 0.9083)      # normalised (L + view) — Blinn-Phong


def _profile_lut(segments) -> tuple[list, list]:
    """(ts, hs) — height(t) table of a moulding cross-section."""
    total = sum(s[0] for s in segments) or 1.0
    ts: list[float] = []
    hs: list[float] = []
    pos = 0.0
    for width, shape, h0, h1, amp in segments:
        count = max(2, int(round(_PROFILE_N * width / total)))
        for i in range(count):
            u = i / (count - 1)
            base = h0 + (h1 - h0) * u
            if shape == "round":            # torus / domed ogee
                v = base + amp * math.sin(math.pi * u)
            elif shape == "cove":           # scotie creuse
                v = base - amp * math.sin(math.pi * u)
            elif shape == "step":           # ressaut adouci (feuillure, gradin)
                v = h0 + (h1 - h0) * (u * u * (3.0 - 2.0 * u))
            else:                           # "line" — chanfrein droit
                v = base
            ts.append((pos + width * u) / total)
            hs.append(v)
        pos += width
    return ts, hs


# (width, shape, start height, end height, amplitude)
_PROFILE_SEGMENTS: dict[str, list] = {
    # Classic gilded frame: edge, torus, large scotia, ogee with ornaments,
    # fillet, then a rebate coming back down against the photo.
    "ogee": [
        (0.05, "step",  0.28, 0.66, 0.0),
        (0.11, "round", 0.66, 0.60, 0.15),
        (0.28, "cove",  0.60, 0.44, 0.30),
        (0.30, "round", 0.44, 0.66, 0.24),
        (0.10, "round", 0.66, 0.52, 0.11),
        (0.16, "step",  0.52, 0.14, 0.0),
    ],
    # Painted hollow moulding (wood, roses) — a wide throat, an inner shoulder.
    "cove": [
        (0.07, "step",  0.32, 0.62, 0.0),
        (0.12, "round", 0.62, 0.58, 0.13),
        (0.44, "cove",  0.58, 0.50, 0.24),
        (0.22, "round", 0.50, 0.64, 0.15),
        (0.15, "step",  0.64, 0.18, 0.0),
    ],
    # A barely domed flat band: the section of the adjustable frames, which must
    # render the chosen colour without drowning it under relief.
    "flat": [
        (0.05, "step",  0.44, 0.74, 0.0),
        (0.10, "round", 0.74, 0.72, 0.06),
        (0.62, "round", 0.72, 0.72, 0.05),
        (0.08, "round", 0.72, 0.66, 0.05),
        (0.15, "step",  0.66, 0.26, 0.0),
    ],
    # Sharp chamfers: metal, art deco.
    "bevel": [
        (0.04, "step",  0.38, 0.80, 0.0),
        (0.34, "line",  0.80, 0.56, 0.0),
        (0.28, "round", 0.56, 0.58, 0.08),
        (0.19, "line",  0.58, 0.76, 0.0),
        (0.15, "step",  0.76, 0.20, 0.0),
    ],
    # Solid half-round: lacquer, glass.
    "round": [
        (0.05, "step",  0.30, 0.54, 0.0),
        (0.76, "round", 0.54, 0.52, 0.34),
        (0.19, "step",  0.52, 0.16, 0.0),
    ],
    # Steps: the stepped section of Art Deco, where the decoration is not
    # applied onto the moulding — it is the moulding itself.
    "steps": [
        (0.04, "step",  0.34, 0.60, 0.0),
        (0.10, "line",  0.60, 0.60, 0.0),
        (0.05, "step",  0.60, 0.74, 0.0),
        (0.13, "line",  0.74, 0.74, 0.0),
        (0.05, "step",  0.74, 0.88, 0.0),
        (0.18, "line",  0.88, 0.88, 0.0),
        (0.06, "step",  0.88, 0.62, 0.0),
        (0.11, "line",  0.62, 0.62, 0.0),
        (0.06, "step",  0.62, 0.42, 0.0),
        (0.08, "line",  0.42, 0.42, 0.0),
        (0.14, "step",  0.42, 0.16, 0.0),
    ],
    # Porcelain throat: a soft hollow and a raised lip.
    "scoop": [
        (0.07, "step",  0.40, 0.68, 0.0),
        (0.58, "cove",  0.68, 0.58, 0.26),
        (0.20, "round", 0.58, 0.68, 0.11),
        (0.15, "step",  0.68, 0.22, 0.0),
    ],
    # A flat field between two beads: the section of the frames whose
    # decoration covers the WHOLE width (vine, roses, flowers). A hollow
    # moulding would add its own relief there, superimposed on that of the
    # ornaments and drowning the drawing — here the carving alone carries the light.
    "field": [
        (0.05, "step",  0.30, 0.72, 0.0),
        (0.07, "round", 0.72, 0.60, 0.09),
        (0.66, "line",  0.60, 0.60, 0.0),
        (0.08, "round", 0.60, 0.70, 0.09),
        (0.14, "step",  0.70, 0.20, 0.0),
    ],
}

_PROFILE_LUTS = {name: _profile_lut(seg) for name, seg in _PROFILE_SEGMENTS.items()}


def _profile_height(np, t, name: str):
    ts, hs = _PROFILE_LUTS.get(name, _PROFILE_LUTS["flat"])
    return np.interp(t, ts, hs).astype("float32")


# ------------------------------------------------------------------ materials
#
# A material = albedo (diffuse colour), specular share, hardness of the
# highlight, occlusion of the hollows and the tint that settles in them (the red
# bole under the gilding, the verdigris of bronze, the grime of an old wood).
# That last point is what gives the "aged" look: a new frame is clean at the
# bottom of its hollows, a worked frame never is.

_MATERIAL_DEFAULTS = dict(
    albedo=(200, 200, 200), ambient=0.34, spec=0.35, shine=26,
    spec_color=(255, 255, 255), ao=0.55, relief=1.0,
    cavity_tint=None, cavity_mix=0.5, gilding=False,
)


def _material(**over) -> dict:
    mat = dict(_MATERIAL_DEFAULTS)
    mat.update(over)
    return mat


_MATERIALS: dict[str, dict] = {
    "gold": _material(albedo=(184, 143, 60), ambient=0.30, spec=0.62, shine=20,
                      spec_color=(255, 238, 190), ao=0.68, gilding=True,
                      cavity_tint=(78, 50, 20), cavity_mix=0.62),
    "silver": _material(albedo=(172, 177, 188), ambient=0.31, spec=0.72, shine=34,
                        ao=0.60, cavity_tint=(52, 56, 66), cavity_mix=0.50),
    "bronze": _material(albedo=(146, 114, 66), ambient=0.36, spec=0.46, shine=24,
                        spec_color=(255, 236, 196), ao=0.58,
                        cavity_tint=(66, 104, 84), cavity_mix=0.60),
    "walnut": _material(albedo=(140, 92, 48), ambient=0.34, spec=0.20, shine=12,
                        spec_color=(255, 240, 214), ao=0.62, relief=0.95,
                        cavity_tint=(44, 24, 10), cavity_mix=0.55),
    "lacquer": _material(albedo=(24, 26, 31), ambient=0.24, spec=1.00, shine=110,
                         ao=0.38, relief=0.75),
    # The painted decoration covering it must stay velvety: a lacquer
    # specular on petals gives them the look of moulded plastic.
    "carmine": _material(albedo=(112, 26, 34), ambient=0.36, spec=0.24, shine=28,
                         ao=0.50, cavity_tint=(46, 12, 16), cavity_mix=0.45),
    "porcelain": _material(albedo=(244, 237, 224), ambient=0.50, spec=0.42, shine=44,
                           ao=0.42, relief=0.85, cavity_tint=(176, 162, 140),
                           cavity_mix=0.40),
    "paint": _material(ambient=0.38, spec=0.26, shine=30, ao=0.40, relief=0.85),
}


def _shade_relief(np, height, albedo, mat: dict, border: float):
    """Lights a height map — the heart of the "carved" rendering.

    The normals are deduced from the gradient of the map (the amplitude of the
    relief is proportional to the thickness of the frame, hence identical on a
    thumbnail and on an export), then lit in Lambert + Blinn-Phong. The
    approximate occlusion (the difference between the map and its blurred
    version) darkens the hollows and deposits the patina in them: without it, a
    correct relief still looks flat to the eye."""
    amp = float(mat["relief"]) * max(border, 1.0) * 0.55
    gy, gx = np.gradient(height * np.float32(amp))
    inv = 1.0 / np.sqrt(gx * gx + gy * gy + 1.0)
    nx, ny, nz = -gx * inv, -gy * inv, inv

    diff = np.clip(nx * _LIGHT[0] + ny * _LIGHT[1] + nz * _LIGHT[2], 0.0, 1.0)
    spec = np.clip(nx * _HALF[0] + ny * _HALF[1] + nz * _HALF[2], 0.0, 1.0) ** mat["shine"]

    blurred = _gauss(np, height, max(1.0, border * 0.09))
    cav = np.clip(1.0 - 3.0 * (blurred - height), 0.0, 1.0)

    tint = mat.get("cavity_tint")
    if tint is not None:
        k = (float(mat["cavity_mix"]) * (1.0 - cav))[:, :, None]
        albedo = albedo * (1.0 - k) + np.array(tint, dtype="float32") * k

    ao = 1.0 - float(mat["ao"]) * (1.0 - cav)
    lit = (mat["ambient"] + (1.0 - mat["ambient"]) * diff) * ao
    arr = albedo * lit[:, :, None]
    arr += (np.array(mat["spec_color"], dtype="float32")
            * (float(mat["spec"]) * spec * ao)[:, :, None])
    return arr


# ------------------------------------------------------------------ band path

def _ring_samples(width: float, height: float, border: float, step: float,
                  frac: float = 0.5) -> list:
    """Samples a continuous line of the band, at ``frac`` × ``border`` from the
    outer edge (0.5 = the median line).

    Returns ``[(x, y, tx, ty, nx, ny, s)]`` — position, unit tangent, unit
    normal pointing inwards, curvilinear abscissa. Clockwise walk: the inner
    normal is always the tangent rotated by +90°.

    Unlike ``_band_sides``, the curvilinear abscissa runs without a break from
    one side to the next: that is what lets an undulation (a vine stem, a
    ribbon) keep its phase around the corners."""
    half = border * frac
    x0, y0 = half, half
    x1, y1 = width - half, height - half
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    samples = []
    s = 0.0
    step = max(1.0, step)
    for i in range(4):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % 4]
        length = math.hypot(bx - ax, by - ay)
        if length <= 0:
            continue
        count = max(1, int(round(length / step)))
        tx, ty = (bx - ax) / length, (by - ay) / length
        nx, ny = -ty, tx          # a +90° rotation → inwards (clockwise walk)
        for k in range(count):
            f = (k + 0.5) / count
            samples.append((ax + (bx - ax) * f, ay + (by - ay) * f, tx, ty, nx, ny, s + f * length))
        s += length
    return samples


def _corner_points(width: float, height: float, border: float) -> list:
    half = border / 2.0
    return [
        (half, half), (width - half, half),
        (width - half, height - half), (half, height - half),
    ]


def _band_sides(w: float, h: float, b: float, frac: float) -> list:
    """The 4 sides of a line drawn at ``frac`` × ``b`` from the outer edge.

    Each entry is ``(start, end, tangent, inner normal)`` — enough to spread a
    motif along a moulding without rewriting the trigonometry every time."""
    d = b * frac
    x0, y0 = d, d
    x1, y1 = w - 1.0 - d, h - 1.0 - d
    return [
        ((x0, y0), (x1, y0), (1.0, 0.0), (0.0, 1.0)),
        ((x1, y0), (x1, y1), (0.0, 1.0), (-1.0, 0.0)),
        ((x1, y1), (x0, y1), (-1.0, 0.0), (0.0, -1.0)),
        ((x0, y1), (x0, y0), (0.0, -1.0), (1.0, 0.0)),
    ]


def _band_corners(w: float, h: float, b: float, frac: float) -> list:
    d = b * frac
    return [(d, d), (w - 1.0 - d, d), (w - 1.0 - d, h - 1.0 - d), (d, h - 1.0 - d)]


_DIAG = math.sqrt(0.5)
# Bisectors pointing inwards, in the order of _band_corners.
_CORNER_DIRS = [(_DIAG, _DIAG), (-_DIAG, _DIAG), (-_DIAG, -_DIAG), (_DIAG, -_DIAG)]


def _distribute(length: float, spacing: float, margin: float) -> list:
    """Abscissas of a repeated motif, centred on the side and clear of the corners.

    Centring rather than starting from one end is what distinguishes a drawn
    frieze from a real one: the two ends of a side must answer each other."""
    spacing = max(spacing, 1.0)
    free = length - 2.0 * margin
    if free <= spacing:
        return []
    count = max(1, int(free // spacing))
    start = (length - count * spacing) / 2.0 + spacing / 2.0
    return [start + k * spacing for k in range(count)]


# ------------------------------------------------------------------ carving
#
# The ornaments are not painted: they are CARVED into the height map, then lit
# by _shade_relief along with the rest of the moulding. A motif painted as a flat
# fill reads as a sticker; the same motif in relief takes the light of the frame,
# casts its shadows into its hollows and becomes material.
#
# The colour layer (optional) serves the motifs that are genuinely painted —
# porcelain, roses — whose tint cannot be deduced from the material of the band.

class _Carver:
    """The sculptor's chisel: writes into the height map (and the colour).

    The map is an "L" image in which 128 is the level of the band: above it,
    material is added, below it, material is hollowed out. The strokes overwrite
    one another in call order, like successive passes of a gouge."""

    def __init__(self, hdraw, cdraw, unit: float, mdraw=None) -> None:
        self._h = hdraw
        self._c = cdraw
        self._m = mdraw
        self._unit = max(2.0, unit)
        self.painted = False

    @staticmethod
    def _lvl(value: float) -> int:
        return max(0, min(255, int(round(128.0 + 127.0 * value))))

    def _paint(self, poly, color) -> None:
        if color is not None:
            self._c.polygon(poly, fill=color)
            self.painted = True

    # Silhouette of the ornaments — of use only to the spill layer, which must
    # know where it covers the photo. Only the passes that ADD material
    # (dome/flat/ridge) contribute to it: a groove never makes a silhouette, it is
    # hollowed into a motif already laid down, and writing it here would leave
    # black scratches trailing on the image wherever it overflows the outline.
    def _cover(self, poly) -> None:
        if self._m is not None:
            self._m.polygon(poly, fill=255)

    def _cover_line(self, pts, width: float) -> None:
        if self._m is not None:
            self._m.line(pts, fill=255, width=max(1, int(round(width))),
                         joint="curve")

    def dome(self, poly, peak: float, color=None, layers: int = 4,
             base: float | None = None, edge: float = 0.0) -> None:
        """Rounded bump: nested outlines from the widest to the highest.

        ``edge`` additionally carves a groove along the outline — that is the
        separating stroke the sculptor puts around each motif, and without it
        two neighbouring ornaments merge into a single soft mass."""
        if len(poly) < 3:
            return
        sx = sy = 0.0
        x0 = y0 = 1e18
        x1 = y1 = -1e18
        for x, y in poly:
            sx += x
            sy += y
            x0 = x if x < x0 else x0
            x1 = x if x > x1 else x1
            y0 = y if y < y0 else y0
            y1 = y if y > y1 else y1
        cx, cy = sx / len(poly), sy / len(poly)
        # Below about ten pixels, the nested outlines that round the bump all fall
        # into the same pixel after the downscale: the drawing is kept, the
        # modelling that would not be visible anyway is dropped.
        if max(x1 - x0, y1 - y0) < 12.0:
            layers = min(layers, 2)
        low = peak * 0.30 if base is None else base
        for i in range(layers):
            f = 1.0 - 0.72 * (i / layers)
            v = low + (peak - low) * (i / (layers - 1) if layers > 1 else 1.0)
            self._h.polygon([(cx + (x - cx) * f, cy + (y - cy) * f) for x, y in poly],
                            fill=self._lvl(v))
        if edge > 0.0:
            self._h.line(list(poly) + [poly[0]], fill=self._lvl(-edge),
                         width=max(1, int(round(self._unit * 0.035))), joint="curve")
        self._cover(poly)
        self._paint(poly, color)

    def flat(self, poly, level: float, color=None) -> None:
        if len(poly) < 3:
            return
        self._h.polygon(poly, fill=self._lvl(level))
        self._cover(poly)
        self._paint(poly, color)

    def disc(self, cx: float, cy: float, radius: float, peak: float,
             color=None, layers: int = 4, edge: float = 0.0) -> None:
        self.dome(_circle_polygon(cx, cy, radius, steps=28), peak, color, layers,
                  edge=edge)

    def ridge(self, pts, width: float, peak: float, color=None, layers: int = 3) -> None:
        """Raised fillet: passes that grow finer and higher."""
        if len(pts) < 2:
            return
        low = peak * 0.35
        for i in range(layers):
            w = max(1, int(round(width * (1.0 - 0.55 * i / max(1, layers)))))
            v = low + (peak - low) * (i / (layers - 1) if layers > 1 else 1.0)
            self._h.line(pts, fill=self._lvl(v), width=w, joint="curve")
        self._cover_line(pts, width)
        if color is not None:
            self._c.line(pts, fill=color, width=max(1, int(round(width))), joint="curve")
            self.painted = True

    def groove(self, pts, width: float, depth: float) -> None:
        """Hollowed groove (a leaf vein, the throat of a moulding)."""
        if len(pts) < 2:
            return
        self._h.line(pts, fill=self._lvl(-abs(depth)), width=max(1, int(round(width))),
                     joint="curve")


def _rotate(px: float, py: float, angle: float) -> tuple[float, float]:
    ca, sa = math.cos(angle), math.sin(angle)
    return px * ca - py * sa, px * sa + py * ca


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _detail_steps(span: float, full: int, minimum: int = 6) -> int:
    """Number of vertices of an outline, proportioned to its real size.

    A motif covering 15 px does not need the 54 vertices it takes at 150 px:
    beyond one vertex every ~2.5 px the tessellation no longer shows, it only
    costs Python time. The foliage frames draw several hundred outlines per
    band, and that number does not depend on the resolution (the spacing of the
    motifs is a fraction of the width of the band) — without this cap, a gallery
    thumbnail pays exactly the same vertex cost as a 6000 × 4000 export, which
    makes the thirteen previews of the frame dialog unusable."""
    return max(minimum, min(full, int(span / 2.5)))


def _vine_leaf_polygon(cx: float, cy: float, size: float, angle: float) -> list:
    """Vine leaf: five lobes, deep sinuses, a cordate base, a toothed edge.

    A generic lobed leaf (cosine radius, two and a half lobes) reads as a round
    blotch as soon as it is small — it is the sinuses between the lobes and the
    toothing of the edge that make it recognisable as a vine. The radius is the
    sum of five Gaussian bumps rather than a cosine: each lobe thus keeps its
    own width, as on the real leaf where the terminal lobe dominates."""
    lobes = ((0.00, 1.00, 0.40), (0.72, 0.80, 0.32), (-0.72, 0.80, 0.32),
             (1.50, 0.54, 0.28), (-1.50, 0.54, 0.28))
    pts = []
    steps = _detail_steps(2.0 * math.pi * size, 108, 24)
    for i in range(steps):
        th = -math.pi + 2.0 * math.pi * i / steps
        r = 0.28
        for centre, amp, wide in lobes:
            r += amp * math.exp(-(_wrap_pi(th - centre) / wide) ** 2)
        r += 0.022 * math.sin(11.0 * th)                     # toothing of the edge
        r *= 1.0 - 0.60 * math.exp(-(_wrap_pi(th - math.pi) / 0.40) ** 2)
        x, y = _rotate(size * r * math.cos(th), size * r * math.sin(th), angle)
        pts.append((cx + x, cy + y))
    return pts


def _petal_polygon(cx: float, cy: float, length: float, width: float,
                   angle: float, notch: float = 0.0) -> list:
    """Teardrop petal, attached at ``(cx, cy)`` and pointing towards ``angle``.

    Narrow at the base, wide at two thirds, rounded at the tip — an elliptical
    petal (the shortcut used until now) gives a pictogram daisy, and a petal
    tapering to a point gives a star. Hence the product of two terms:
    ``s ** 0.55`` tightens the base, ``sin(πs) ** 0.35`` keeps the width almost
    to the end before closing it up abruptly. ``notch`` indents the tip, which
    distinguishes a rose or an apple blossom from a flower with lanceolate
    petals."""
    steps = _detail_steps(length, 26, 5)
    left, right = [], []
    for i in range(steps + 1):
        s = i / steps
        half = width * (s ** 0.55) * (math.sin(math.pi * s) ** 0.35)
        left.append((length * s, half))
        right.append((length * s, -half))
    poly = left
    if notch > 0.0:
        poly = poly + [(length * (1.0 - notch), 0.0)]
    poly = poly + right[::-1]
    out = []
    for px, py in poly:
        rx, ry = _rotate(px, py, angle)
        out.append((cx + rx, cy + ry))
    return out


def _cup_polygon(cx: float, cy: float, radius: float, angle: float, span: float,
                 thickness: float, steps: int = 0) -> list:
    """Cupped petal: a crescent between two concentric arcs.

    That is the real shape of a rose petal seen from the front — it *wraps* the
    heart. Radiating teardrop petals (``_petal_polygon``) give a daisy whatever
    their number; only the wrapping produces a rose. The outer edge is slightly
    wavy, failing which the crescent reads as a piece of ironwork."""
    inner = radius * max(0.05, 1.0 - thickness)
    a0, a1 = angle - span / 2.0, angle + span / 2.0
    steps = steps or _detail_steps(abs(span) * radius, 20, 5)
    pts = []
    for i in range(steps + 1):
        a = a0 + (a1 - a0) * i / steps
        r = radius * (1.0 + 0.055 * math.sin(3.0 * math.pi * i / steps))
        pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    for i in range(steps + 1):
        a = a1 + (a0 - a1) * i / steps
        pts.append((cx + math.cos(a) * inner, cy + math.sin(a) * inner))
    return pts


def _circle_polygon(cx: float, cy: float, radius: float, steps: int = 40) -> list:
    steps = _detail_steps(2.0 * math.pi * radius, steps, 8)
    return [(cx + math.cos(2 * math.pi * i / steps) * radius,
             cy + math.sin(2 * math.pi * i / steps) * radius) for i in range(steps)]


def _ellipse_polygon(cx: float, cy: float, rx: float, ry: float,
                     angle: float, steps: int = 26) -> list:
    steps = _detail_steps(math.pi * (rx + ry), steps, 8)
    pts = []
    for i in range(steps):
        a = 2.0 * math.pi * i / steps
        x, y = _rotate(math.cos(a) * rx, math.sin(a) * ry, angle)
        pts.append((cx + x, cy + y))
    return pts


def _arc_points(cx: float, cy: float, rx: float, ry: float, angle: float,
                a0: float, a1: float, steps: int = 20) -> list:
    pts = []
    for i in range(steps + 1):
        a = a0 + (a1 - a0) * i / steps
        x, y = _rotate(math.cos(a) * rx, math.sin(a) * ry, angle)
        pts.append((cx + x, cy + y))
    return pts


def _acanthus_polygon(cx: float, cy: float, length: float, angle: float,
                      lobes: int = 5, width: float = 0.40, sweep: float = 0.34) -> list:
    """Acanthus leaf: a curved midrib, decreasing lobes, a curled-back tip.

    THE motif of the European carved frame. A symmetrical lobed leaf reads as a
    pastille; the acanthus must taper and curl back, failing which the frieze
    looks like a row of clover leaves."""
    steps = 26
    spine = [(length * (i / steps), length * sweep * (i / steps) ** 1.8)
             for i in range(steps + 1)]
    left, right = [], []
    for i, (px, py) in enumerate(spine):
        s = i / steps
        j, k = min(i + 1, steps), max(i - 1, 0)
        tx, ty = spine[j][0] - spine[k][0], spine[j][1] - spine[k][1]
        n = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / n, tx / n
        lobe = 0.58 + 0.42 * abs(math.sin(math.pi * lobes * s))
        wd = length * width * ((1.0 - s) ** 0.55) * lobe
        left.append((px + nx * wd, py + ny * wd))
        right.append((px - nx * wd, py - ny * wd))
    out = []
    for x, y in left + right[::-1]:
        rx, ry = _rotate(x, y, angle)
        out.append((cx + rx, cy + ry))
    return out


def _carve_acanthus(cv: _Carver, cx: float, cy: float, length: float, angle: float,
                    peak: float, lobes: int = 5) -> None:
    """Complete acanthus leaf: mass in relief, hollowed midrib, marked lobes."""
    cv.dome(_acanthus_polygon(cx, cy, length, angle, lobes), peak, layers=5,
            edge=peak * 0.9)
    spine = [(cx, cy)]
    for i in range(1, 7):
        s = i / 6.0
        x, y = _rotate(length * s, length * 0.34 * s ** 1.8, angle)
        spine.append((cx + x, cy + y))
    cv.groove(spine, max(1.0, length * 0.055), peak * 0.75)
    for k in range(1, lobes):
        s = k / float(lobes)
        bx, by = _rotate(length * s, length * 0.34 * s ** 1.8, angle)
        ex, ey = _rotate(length * (s + 0.20), length * 0.34 * s ** 1.8
                         + length * 0.30 * (1.0 - s), angle)
        cv.groove([(cx + bx, cy + by), (cx + ex, cy + ey)],
                  max(1.0, length * 0.035), peak * 0.5)


def _carve_shell(cv: _Carver, cx: float, cy: float, radius: float, angle: float,
                 peak: float) -> None:
    """Corner shell: a fan of ribs under a bead, cartouche style."""
    ribs = 9
    span = math.pi * 0.86
    step = span / (ribs - 1)
    for k in range(ribs):
        a = angle - span / 2.0 + step * k
        half = step * 0.40
        inner = radius * 0.24
        poly = [
            (cx + math.cos(a - half) * inner, cy + math.sin(a - half) * inner),
            (cx + math.cos(a - half * 0.8) * radius, cy + math.sin(a - half * 0.8) * radius),
            (cx + math.cos(a) * radius * 1.10, cy + math.sin(a) * radius * 1.10),
            (cx + math.cos(a + half * 0.8) * radius, cy + math.sin(a + half * 0.8) * radius),
            (cx + math.cos(a + half) * inner, cy + math.sin(a + half) * inner),
        ]
        cv.dome(poly, peak if k % 2 == 0 else peak * 0.62, layers=3, edge=peak * 0.8)
    cv.disc(cx, cy, radius * 0.26, peak * 1.15)
    cv.groove(_arc_points(cx, cy, radius * 0.34, radius * 0.34, 0.0,
                          angle - span / 2.0, angle + span / 2.0),
              max(1.0, radius * 0.07), peak * 0.6)


def _carve_rosette(cv: _Carver, cx: float, cy: float, radius: float, peak: float,
                   petals: int = 8, color=None, heart=None) -> None:
    for k in range(petals):
        a = 2.0 * math.pi * k / petals
        poly = _ellipse_polygon(cx + math.cos(a) * radius * 0.52,
                                cy + math.sin(a) * radius * 0.52,
                                radius * 0.44, radius * 0.26, a)
        cv.dome(poly, peak, color=color)
        cv.groove([(cx + math.cos(a) * radius * 0.22, cy + math.sin(a) * radius * 0.22),
                   (cx + math.cos(a) * radius * 0.94, cy + math.sin(a) * radius * 0.94)],
                  max(1.0, radius * 0.05), peak * 0.55)
    cv.disc(cx, cy, radius * 0.26, peak * 1.2, color=heart)


def _carve_rose(cv: _Carver, cx: float, cy: float, radius: float, peak: float,
                color=None, heart=None, angle: float = 0.0) -> None:
    """Carved rose: three rings of notched petals, a spiral heart.

    The petals wrap the heart (``_cup_polygon``) instead of radiating from it,
    and each ring is painted in its own value — light on the outside, deep at
    the heart. It is that gradation which makes the flower: at a constant
    colour, even well carved petals drown in a flat pink as soon as the frame is
    seen as a thumbnail, because their relief is too fine to survive the
    downscale. Value, on the other hand, always survives."""
    # (radius, height, count, angular offset, thickness, tint)
    rings = ((1.00, 0.55, 5, 0.00, 0.44, 0.34),
             (0.70, 0.80, 4, 0.72, 0.50, 0.06),
             (0.44, 1.00, 3, 1.55, 0.60, -0.26))
    for scale, lift, count, spin, thick, tone in rings:
        pr = radius * scale
        shade = (_mix(color, (255, 255, 255), tone) if tone >= 0.0
                 else _mix(color, (58, 14, 26), -tone))
        # Just enough overlap to close the ring: beyond it, the crescents merge
        # into a disc and the rose becomes a pastille again.
        span = 2.0 * math.pi / count * 1.06
        for k in range(count):
            a = 2.0 * math.pi * k / count + spin + angle
            petal = shade if k % 2 else _mix(shade, (255, 255, 255), 0.12)
            cv.dome(_cup_polygon(cx, cy, pr, a, span, thick), peak * lift,
                    color=petal, layers=4, base=peak * lift * 0.30,
                    edge=peak * 0.95)
    # Rolled heart: two nested, dark commas, never a light disc — a pale round
    # in the middle of the petals reads as an eye.
    core = _mix(color, (52, 12, 24), 0.45)
    for sgn in (1.0, -1.0):
        cv.dome(_cup_polygon(cx, cy, radius * 0.30,
                             angle + (0.0 if sgn > 0 else math.pi),
                             math.pi * 1.20, 0.86), peak * 1.05, color=core,
                layers=3, base=peak * 0.45, edge=peak * 0.85)
    if heart is not None:
        cv.disc(cx, cy, max(1.0, radius * 0.085), peak * 1.15,
                color=_mix(core, heart, 0.55), layers=2)


def _carve_blossom(cv: _Carver, cx: float, cy: float, radius: float, peak: float,
                   petals: int = 6, color=None, heart=None, notch: float = 0.0,
                   twist: float = 0.0, rng=None) -> None:
    """Open corolla: teardrop petals hollowed like spoons, a heart of stamens.

    Each petal is domed from a low base towards a ridge near the tip — that is
    what hollows the corolla around the heart instead of making it a domed
    pastille. The slight disorder (``rng``) is essential: fifteen strictly
    identical flowers along a moulding read as a repeated stamp, not as a
    sowing."""
    for k in range(petals):
        a = 2.0 * math.pi * k / petals + twist
        span = radius
        if rng is not None:
            a += (float(rng.random()) - 0.5) * 0.22
            span = radius * (0.86 + 0.26 * float(rng.random()))
        poly = _petal_polygon(cx + math.cos(a) * radius * 0.15,
                              cy + math.sin(a) * radius * 0.15,
                              span, radius * 0.40, a, notch)
        cv.dome(poly, peak, color=color, layers=4, base=peak * 0.16,
                edge=peak * 0.75)
        cv.groove([(cx + math.cos(a) * radius * 0.20,
                    cy + math.sin(a) * radius * 0.20),
                   (cx + math.cos(a) * span * 0.82,
                    cy + math.sin(a) * span * 0.82)],
                  max(1.0, radius * 0.055), peak * 0.45)
    cv.disc(cx, cy, radius * 0.24, peak * 0.60, color=heart, layers=3)
    for k in range(5):                                  # stamens
        a = 2.0 * math.pi * k / 5.0 + twist * 0.5
        cv.disc(cx + math.cos(a) * radius * 0.13, cy + math.sin(a) * radius * 0.13,
                max(1.0, radius * 0.075), peak * 1.25, color=heart, layers=2)


def _carve_bud(cv: _Carver, cx: float, cy: float, size: float, angle: float,
               peak: float, color=None, leaf=None) -> None:
    """Bud: a closed oval, two rolled petals, sepals opening at the calyx."""
    cv.dome(_ellipse_polygon(cx, cy, size * 0.62, size * 0.40, angle),
            peak * 0.80, color=color, layers=4, edge=peak * 0.6)
    for sgn in (1.0, -1.0):
        cv.dome(_petal_polygon(cx - math.cos(angle) * size * 0.28,
                               cy - math.sin(angle) * size * 0.28,
                               size * 0.86, size * 0.24, angle + sgn * 0.26,
                               notch=0.12),
                peak, color=color, layers=3, base=peak * 0.3, edge=peak * 0.6)
    for sgn in (1.0, -1.0):
        cv.dome(_petal_polygon(cx - math.cos(angle) * size * 0.46,
                               cy - math.sin(angle) * size * 0.46,
                               size * 0.66, size * 0.13,
                               angle + math.pi * 0.5 * sgn + sgn * 0.35),
                peak * 0.55, color=leaf, layers=3, edge=peak * 0.5)


def _carve_foliage(cv: _Carver, cx: float, cy: float, size: float, angle: float,
                   peak: float, color=None, count: int = 3,
                   spread: float = 0.72) -> None:
    """Tuft of lanceolate leaves in a fan — the filler of the gaps.

    Without it, a clump of flowers lets the ground show between each corolla and
    the frame falls back to a punctuated frieze. It is the most repeated motif
    of the foliage frames (several hundred per band, whatever the rendering
    size): adding one more pass to it — a vein, a layer of dome — is paid for on
    each of the thirteen thumbnails of the gallery."""
    for k in range(count):
        a = angle + (k - (count - 1) / 2.0) * spread
        poly = _petal_polygon(cx, cy, size, size * 0.26, a)
        # Leaves of different values within one same tuft: uniform, they merge
        # into a green blotch as soon as the thumbnail is downscaled.
        tint = None if color is None else _mix(
            color, (18, 34, 14) if k % 2 else (206, 226, 168), 0.20)
        cv.dome(poly, peak, color=tint, layers=3, base=peak * 0.25,
                edge=peak * 0.7)


def _carve_tendril(cv: _Carver, cx: float, cy: float, dx: float, dy: float,
                   size: float, peak: float, sign: float = 1.0) -> None:
    """Tendril: the coiled filament that grips the trellis — it closes the gaps
    the foliage does not cover."""
    pts = _volute_points(cx, cy, dx, dy, size, sign, turns=1.55, steps=34)
    cv.ridge(pts, max(1.0, size * 0.20), peak, layers=3)


def _carve_rope(cv: _Carver, w: float, h: float, b: float, frac: float,
                radius: float, peak: float) -> None:
    """Twisted cord along a line of the band: tight oblique strands."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, frac):
        length = math.hypot(bx - ax, by - ay)
        ang = math.atan2(ty, tx) + 0.62
        for d in _distribute(length, radius * 1.55, 0.0):
            px, py = ax + tx * d, ay + ty * d
            cv.dome(_ellipse_polygon(px, py, radius * 0.52, radius * 1.05, ang),
                    peak, layers=3, edge=peak * 0.7)


def _carve_grapes(cv: _Carver, cx: float, cy: float, size: float, angle: float,
                  peak: float, color=None) -> None:
    """Bunch: three rows of decreasing grapes, along the ``angle`` axis."""
    rows = ((3, 0.0, 1.0), (2, 0.60, 0.88), (1, 1.15, 0.74))
    r = size * 0.26
    for count, depth, scale in rows:
        for k in range(count):
            off = (k - (count - 1) / 2.0) * r * 1.8
            x, y = _rotate(depth * size * 0.55, off, angle)
            cv.disc(cx + x, cy + y, r * scale, peak, color=color, layers=3,
                    edge=peak * 0.75)


def _carve_bead_reel(cv: _Carver, w: float, h: float, b: float, frac: float,
                     radius: float, peak: float) -> None:
    """Row of beads: bead, spool, bead… along a line of the band."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, frac):
        length = math.hypot(bx - ax, by - ay)
        for i, d in enumerate(_distribute(length, radius * 2.6, b * 0.5)):
            px, py = ax + tx * d, ay + ty * d
            if i % 3 == 2:                      # spool: two tight discs
                for sgn in (-1.0, 1.0):
                    cv.dome(_ellipse_polygon(px + tx * radius * 0.34 * sgn,
                                             py + ty * radius * 0.34 * sgn,
                                             radius * 0.30, radius * 0.86,
                                             math.atan2(ty, tx)), peak * 0.8, layers=3)
            else:
                cv.disc(px, py, radius, peak, layers=4)


def _carve_egg_and_dart(cv: _Carver, w: float, h: float, b: float, frac: float,
                        size: float, peak: float) -> None:
    """Eggs and darts — the classical frieze par excellence."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, frac):
        length = math.hypot(bx - ax, by - ay)
        ang = math.atan2(ny, nx)
        for d in _distribute(length, size * 1.7, b * 0.9):
            px, py = ax + tx * d, ay + ty * d
            cv.dome(_ellipse_polygon(px, py, size * 0.60, size * 0.44, ang),
                    peak * 0.55, layers=3)                    # coquille
            cv.dome(_ellipse_polygon(px, py, size * 0.42, size * 0.30, ang),
                    peak, layers=4)                           # egg
            cv.groove(_arc_points(px, py, size * 0.52, size * 0.38, ang,
                                  -math.pi * 0.85, math.pi * 0.85),
                      max(1.0, size * 0.09), peak * 0.7)
            mx, my = px + tx * size * 0.85, py + ty * size * 0.85
            cv.dome(_fleuron_polygon(mx, my, size * 0.44, ang), peak * 0.8, layers=3)


def _carve_meander(cv: _Carver, w: float, h: float, b: float, peak: float) -> None:
    """Greek key (running meander): a continuous rail + one spiral per cell."""
    cell_pts = [(0.10, 0.00), (0.10, 0.82), (0.82, 0.82), (0.82, 0.26),
                (0.36, 0.26), (0.36, 0.58), (0.60, 0.58)]
    lw = max(1.0, b * 0.085)
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, 0.30):
        length = math.hypot(bx - ax, by - ay)
        count = max(1, int(length // (b * 0.92)))
        cell = length / count
        depth = b * 0.46
        cv.ridge([(ax, ay), (bx, by)], lw, peak)
        for k in range(count):
            base = k * cell
            pts = [(ax + tx * (base + u * cell) + nx * v * depth,
                    ay + ty * (base + u * cell) + ny * v * depth)
                   for u, v in cell_pts]
            cv.ridge(pts, lw, peak)


def _carve_gadroons(cv: _Carver, w: float, h: float, b: float, frac: float,
                    peak: float) -> None:
    """Gadroons: domed lobes across the moulding, tight like flutes."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, frac):
        length = math.hypot(bx - ax, by - ay)
        ang = math.atan2(ny, nx)
        spacing = b * 0.46
        for d in _distribute(length, spacing, b * 1.25):
            px, py = ax + tx * d, ay + ty * d
            cv.dome(_ellipse_polygon(px, py, b * 0.17, b * 0.34, ang), peak, layers=4)
            cv.groove([(px + tx * spacing * 0.5 - nx * b * 0.34,
                        py + ty * spacing * 0.5 - ny * b * 0.34),
                       (px + tx * spacing * 0.5 + nx * b * 0.34,
                        py + ty * spacing * 0.5 + ny * b * 0.34)],
                      max(1.0, b * 0.045), peak * 0.6)


def _carve_fillets(cv: _Carver, w: float, h: float, b: float, specs) -> None:
    """Continuous fillets and grooves (``(fraction, width, signed height)``)."""
    for frac, width, level in specs:
        for (a, bb, _t, _n) in _band_sides(w, h, b, frac):
            if level >= 0:
                cv.ridge([a, bb], max(1.0, b * width), level)
            else:
                cv.groove([a, bb], max(1.0, b * width), -level)


# --------------------------------------------------------------- motifs per frame

def _carve_baroque(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Gilded frame: an acanthus frieze on the ogee, corner shells, beads."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, 0.60):
        length = math.hypot(bx - ax, by - ay)
        ang = math.atan2(ty, tx)
        for i, d in enumerate(_distribute(length, b * 1.30, b * 1.9)):
            px, py = ax + tx * d, ay + ty * d
            # Scroll: a large reclining leaf, a small one lifting it back up.
            _carve_acanthus(cv, px - tx * b * 0.62 - nx * b * 0.16,
                            py - ty * b * 0.62 - ny * b * 0.16,
                            b * 1.22, ang + 0.30, 0.36)
            _carve_acanthus(cv, px + tx * b * 0.30 + nx * b * 0.20,
                            py + ty * b * 0.30 + ny * b * 0.20,
                            b * 0.58, ang - 0.85, 0.30, lobes=3)
    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 0.52), _CORNER_DIRS):
        _carve_shell(cv, cx + dx * b * 0.06, cy + dy * b * 0.06, b * 0.72,
                     math.atan2(dy, dx), 0.42)
    _carve_bead_reel(cv, w, h, b, 0.88, b * 0.075, 0.26)
    _carve_fillets(cv, w, h, b, ((0.05, 0.05, 0.18), (0.79, 0.035, -0.22)))


def _carve_pearl(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Eggs and beads: the neo-classical moulding, sober and highly worked."""
    _carve_egg_and_dart(cv, w, h, b, 0.60, b * 0.42, 0.34)
    _carve_bead_reel(cv, w, h, b, 0.86, b * 0.070, 0.28)
    _carve_bead_reel(cv, w, h, b, 0.14, b * 0.055, 0.22)
    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 0.60), _CORNER_DIRS):
        _carve_rosette(cv, cx, cy, b * 0.42, 0.34)
    _carve_fillets(cv, w, h, b, ((0.28, 0.04, -0.20), (0.75, 0.035, 0.20)))


def _carve_greek(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Gilded Greek key on a dark ground — an Empire moulding."""
    _carve_meander(cv, w, h, b, 0.34)
    _carve_bead_reel(cv, w, h, b, 0.90, b * 0.065, 0.26)
    _carve_fillets(cv, w, h, b, ((0.08, 0.05, 0.22), (0.80, 0.04, 0.18),
                                 (0.86, 0.03, -0.18)))
    for (cx, cy), _d in zip(_band_corners(w, h, b, 0.55), _CORNER_DIRS):
        cv.disc(cx, cy, b * 0.22, 0.34)


def _carve_artdeco(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Art deco: the moulding is already stepped (the ``steps`` profile) — the
    decoration is limited to rhythmic bars and a corner fan. Sobriety makes the
    style: a continuous ornament would destroy the reading of the steps."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, 0.44):
        length = math.hypot(bx - ax, by - ay)
        for d in _distribute(length, b * 0.90, b * 1.7):
            px, py = ax + tx * d, ay + ty * d
            for k in range(3):
                off = (k - 1) * b * 0.13
                cv.ridge([(px + tx * off - nx * b * 0.16, py + ty * off - ny * b * 0.16),
                          (px + tx * off + nx * b * 0.16, py + ty * off + ny * b * 0.16)],
                         max(1.0, b * 0.070), 0.30)
    for cx, cy in _band_corners(w, h, b, 0.5):
        for k, size in enumerate((0.86, 0.56, 0.28)):    # stepped corner block
            r = b * size * 0.5
            cv.ridge([(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r),
                      (cx - r, cy + r), (cx - r, cy - r)],
                     max(1.0, b * 0.075), 0.24 + 0.06 * k)


def _carve_wood(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Carved walnut: gadroons, corner rosettes, moulding grooves."""
    _carve_gadroons(cv, w, h, b, 0.58, 0.30)
    for (cx, cy), _d in zip(_band_corners(w, h, b, 0.55), _CORNER_DIRS):
        _carve_rosette(cv, cx, cy, b * 0.44, 0.32, petals=6)
    _carve_fillets(cv, w, h, b, ((0.12, 0.05, 0.20), (0.24, 0.04, -0.22),
                                 (0.84, 0.045, 0.18), (0.92, 0.03, -0.18)))


def _carve_metal(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Steel: sharp edges, forged rivets, a central groove."""
    _carve_fillets(cv, w, h, b, ((0.18, 0.05, 0.22), (0.50, 0.10, -0.16),
                                 (0.84, 0.05, 0.22)))
    spots = list(_band_corners(w, h, b, 0.5))
    for (ax, ay), (bx, by), (tx, ty), _n in _band_sides(w, h, b, 0.5):
        length = math.hypot(bx - ax, by - ay)
        spots += [(ax + tx * d, ay + ty * d)
                  for d in _distribute(length, b * 2.6, b * 1.6)]
    for cx, cy in spots:
        ring = _circle_polygon(cx, cy, b * 0.235, steps=24)
        cv.disc(cx, cy, b * 0.20, 0.34)
        cv.groove(ring + [ring[0]], max(1.0, b * 0.035), 0.20)


# Palettes of the three foliage frames — at module level because the band and
# the motifs that spill over the photo (cf. "spills" further down) must be cut
# from the same tints: an ornament passing over the image in a neighbouring
# colour reads as a sticker, not as the continuation of the carving.
_VINE_LIT = (188, 156, 96)        # rubbed bronze, exposed edges
_VINE_SHADE = (118, 106, 62)      # greened bronze of the undersides
_VINE_DEEP = (86, 76, 46)         # bottom of the patina

_ROSE_HUES = ((196, 82, 104), (170, 54, 80), (222, 130, 146), (206, 100, 118))
_ROSE_HEART = (244, 214, 168)
_ROSE_LEAVES = ((62, 98, 52), (88, 124, 66), (74, 110, 58))

# Broken tints of white: on porcelain, a pure colour turns into a decal.
# The white of the paste must stay perceptible in every tone.
_PORCELAIN = (250, 244, 234)
_FLOWER_PALETTE = tuple(_mix(c, _PORCELAIN, 0.30) for c in
                        ((206, 84, 116), (228, 162, 70), (104, 138, 208),
                         (172, 106, 190), (224, 120, 96), (236, 220, 232)))
_FLOWER_HEARTS = ((238, 196, 92), (246, 226, 152), (230, 178, 68))
_FLOWER_LEAVES = tuple(_mix(c, _PORCELAIN, 0.18) for c in
                       ((104, 146, 84), (78, 122, 68), (134, 168, 106)))
_FLOWER_GOLD = (198, 158, 76)


def _carve_grape_leaf(cv: _Carver, cx: float, cy: float, size: float,
                      angle: float, peak: float, color=None) -> None:
    """Complete vine leaf: mass in relief, palmate veins, sinuses.

    The five veins run from the petiole towards the five lobes of
    ``_vine_leaf_polygon`` — they must aim in the same directions, failing which
    the veining crosses the sinuses and the leaf becomes a blotch again."""
    base_x = cx - math.cos(angle) * size * 0.62
    base_y = cy - math.sin(angle) * size * 0.62
    # A weak, wide dome rather than a tall one: a leaf is a slightly buckled
    # plate, not a cushion. With five nested outlines it swelled into a smooth
    # balloon and read as a fish — it is the hollowed vein, not the volume, that
    # makes it recognisable.
    cv.dome(_vine_leaf_polygon(cx, cy, size, angle), peak, color=color, layers=3,
            base=peak * 0.62, edge=peak * 0.95)
    for centre, reach in ((0.00, 1.15), (0.72, 0.92), (-0.72, 0.92),
                          (1.50, 0.66), (-1.50, 0.66)):
        a = angle + centre
        cv.groove([(base_x, base_y),
                   (base_x + math.cos(a) * size * reach,
                    base_y + math.sin(a) * size * reach)],
                  max(1.0, size * 0.10), peak * 0.95)


def _carve_vine(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Bronze trellis: two interlaced vine stems, contiguous foliage, tendrils
    and bunches — the moulding is covered from one edge to the other.

    A single stem lined with small leaves (the previous version) left two thirds
    of the moulding bare. The covering does not come from adding motifs, but
    from putting them at the scale of the band: a leaf whose span is half the
    width of the frame, carried alternately on either side of the stem, covers
    the moulding on its own — the bunch and the tendril on the opposite side
    merely plug the gap between them. The stem is drawn on ``_ring_samples`` (a
    continuous curvilinear abscissa) so that the undulation does not break in
    the corners.

    The motifs are **tinted**, whereas the band is a plain bronze: these are not
    colours but values of the same patina (light bronze, greened bronze, dark
    bronze). Without them, a trellis this covered becomes illegible from the
    thumbnail on — relief alone averages out into a uniform bumpiness, whereas a
    difference in value survives any downscaling."""
    lit, shade, deep = _VINE_LIT, _VINE_SHADE, _VINE_DEEP
    # A carpet of small leaves first, across the whole width: the large leaves
    # alone leave the bronze bare against both edges, where the vine stem never
    # passes. The tufts aim ACROSS the band (± the normal) — a fan covers a
    # sector, not a disc, and oriented at random it opens a diagonal grid of gaps.
    for row, depth in enumerate((0.18, 0.82)):
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, depth):
            length = math.hypot(bx - ax, by - ay)
            for i, d in enumerate(_distribute(length, b * 0.44, 0.0)):
                px, py = ax + tx * d, ay + ty * d
                sgn = 1.0 if (i + row) % 2 else -1.0
                ang = math.atan2(ny * sgn, nx * sgn) + (float(rng.random()) - 0.5) * 0.8
                _carve_foliage(cv, px, py, b * (0.30 + 0.08 * float(rng.random())),
                               ang, 0.18, color=deep, count=3, spread=0.86)

    sstep = max(2.0, b * 0.06)
    period = max(b * 2.60, 1.0)
    samples = _ring_samples(w, h, b, sstep, frac=0.50)
    if len(samples) < 8:
        return
    path = []
    for x, y, _tx, _ty, nx, ny, s in samples:
        off = math.sin(s / period * 2.0 * math.pi) * b * 0.17
        path.append((x + nx * off, y + ny * off))
    cv.ridge(path + [path[0]], max(2.0, b * 0.070), 0.24, color=lit, layers=4)
    every = max(2, int(round(b * 0.66 / sstep)))
    for n, idx in enumerate(range(0, len(samples), every)):
        x, y, tx, ty, nx, ny, s = samples[idx]
        off = math.sin(s / period * 2.0 * math.pi) * b * 0.17
        sx, sy = x + nx * off, y + ny * off
        sign = 1.0 if n % 2 == 0 else -1.0
        jit = float(rng.random()) - 0.5
        ang = math.atan2(ty, tx) + sign * (1.25 + jit * 0.22)
        lx = sx + nx * sign * b * 0.30 + tx * b * 0.04
        ly = sy + ny * sign * b * 0.30 + ty * b * 0.04
        cv.groove([(sx, sy), (lx, ly)], max(1.0, b * 0.035), 0.18)
        _carve_grape_leaf(cv, lx, ly, b * (0.36 + 0.04 * jit), ang, 0.34,
                          color=lit if n % 2 else _mix(lit, shade, 0.55))
        # On the other side of the stem, in the hollow left between two leaves.
        ox = sx - nx * sign * b * 0.26 + tx * b * 0.33
        oy = sy - ny * sign * b * 0.26 + ty * b * 0.33
        if n % 2:
            _carve_grapes(cv, ox, oy, b * 0.42, ang + math.pi, 0.30, color=shade)
        else:
            _carve_tendril(cv, ox, oy, tx, ty, b * 0.17, 0.22, -sign)
    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 0.50), _CORNER_DIRS):
        _carve_grape_leaf(cv, cx + dx * b * 0.06, cy + dy * b * 0.06, b * 0.40,
                          math.atan2(dy, dx), 0.36, color=lit)


def _carve_roses(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Clump of roses on carmine lacquer: the whole band is in flower.

    Two staggered rows — the flowers of one plug the gaps of the other, and
    their corollas overlap across the moulding — plus buds and foliage in the
    interstices, between two twisted cords bordering the clump. The composition
    hangs on that overlap: a single row, however dense, always lets the lacquer
    show on either side."""
    rose_hues, heart, leaves = _ROSE_HUES, _ROSE_HEART, _ROSE_LEAVES

    _carve_rope(cv, w, h, b, 0.06, b * 0.075, 0.26)
    _carve_rope(cv, w, h, b, 0.94, b * 0.062, 0.24)

    # A carpet of foliage first, flowers afterwards: it is that continuous
    # background which makes the lacquer disappear between the corollas. Three
    # rows overlapping across the band, with orientations turning from one motif
    # to the next so as not to give a combed texture.
    for row, depth in enumerate((0.16, 0.46, 0.78)):
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, depth):
            length = math.hypot(bx - ax, by - ay)
            for i, d in enumerate(_distribute(length, b * 0.40, 0.0)):
                px, py = ax + tx * d, ay + ty * d
                # The tufts aim ACROSS the band (± the normal): a fan oriented at
                # random leaves bare corners of lacquer, because it covers a sector
                # and not a disc.
                sgn = 1.0 if (i + row) % 2 else -1.0
                ang = math.atan2(ny * sgn, nx * sgn) + (float(rng.random()) - 0.5) * 0.9
                _carve_foliage(cv, px, py, b * (0.42 + 0.09 * float(rng.random())),
                               ang, 0.24, color=leaves[(i + row) % 3], count=3,
                               spread=0.86)

    for row, (depth, radius, shift) in enumerate(((0.32, 0.245, 0.0),
                                                  (0.70, 0.225, 0.5))):
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, depth):
            length = math.hypot(bx - ax, by - ay)
            spacing = b * 0.62
            for i, d in enumerate(_distribute(length, spacing, b * 0.50)):
                # A half-step offset between the two rows, plus a jitter: without it the
                # flowers of the two rows line up in places and the clump reads as a row
                # of "8"s.
                d += (shift + 0.22 * (float(rng.random()) - 0.5)) * spacing
                px, py = ax + tx * d, ay + ty * d
                if (i + 2 * row) % 5 == 4:
                    # A bud: it airs the row out and avoids the stencil effect.
                    _carve_bud(cv, px, py, b * 0.26,
                               math.atan2(ty, tx) + (1.0 if row else -1.0) * 1.4,
                               0.34, color=rose_hues[(i + row) % 4],
                               leaf=leaves[i % 3])
                else:
                    _carve_rose(cv, px, py, b * radius, 0.36,
                                color=rose_hues[(i + 2 * row) % 4], heart=heart,
                                angle=float(rng.random()) * 1.2)

    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 0.50), _CORNER_DIRS):
        ang = math.atan2(dy, dx)
        for sgn in (1.0, -1.0):
            _carve_foliage(cv, cx - dy * b * 0.30 * sgn, cy + dx * b * 0.30 * sgn,
                           b * 0.34, ang + sgn * 1.15, 0.26, color=leaves[0],
                           count=3, spread=0.62)
        _carve_rose(cv, cx, cy, b * 0.34, 0.40, color=rose_hues[0], heart=heart,
                    angle=ang)


def _carve_flowers(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Painted porcelain: a mille-fleurs sowing covering the whole band.

    The principle is that of a faience decoration: a ground of light foliage,
    corollas of several species (with five, six and eight petals, notched or
    not) sown in a staggered layout, posies of forget-me-nots in the gaps, and
    two gold fillets bordering the sowing. Variety is the subject here — a
    sowing of one single repeated flower is not a mille-fleurs, it is wallpaper."""
    palette, hearts, leaves = _FLOWER_PALETTE, _FLOWER_HEARTS, _FLOWER_LEAVES
    gold = _FLOWER_GOLD

    for row, depth in enumerate((0.16, 0.46, 0.78)):
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, depth):
            length = math.hypot(bx - ax, by - ay)
            for i, d in enumerate(_distribute(length, b * 0.40, 0.0)):
                px, py = ax + tx * d, ay + ty * d
                sgn = 1.0 if (i + row) % 2 else -1.0
                ang = math.atan2(ny * sgn, nx * sgn) + (float(rng.random()) - 0.5) * 1.0
                _carve_foliage(cv, px, py, b * (0.40 + 0.10 * float(rng.random())),
                               ang, 0.20, color=leaves[(i + row) % 3], count=3,
                               spread=0.88)

    # (number of petals, notch) — three species alternating.
    species = ((5, 0.20), (6, 0.0), (8, 0.10))
    for row, (depth, radius, shift) in enumerate(((0.31, 0.235, 0.0),
                                                  (0.69, 0.215, 0.5))):
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, depth):
            length = math.hypot(bx - ax, by - ay)
            spacing = b * 0.60
            for i, d in enumerate(_distribute(length, spacing, b * 0.50)):
                d += (shift + 0.24 * (float(rng.random()) - 0.5)) * spacing
                px, py = ax + tx * d, ay + ty * d
                n = i + 2 * row
                if n % 5 == 4:
                    # Forget-me-nots: three tiny, tight corollas, the binder of the
                    # sowing — without them the large flowers stay islands.
                    for k in range(3):
                        a = 2.0 * math.pi * k / 3.0 + 0.5
                        _carve_blossom(cv, px + math.cos(a) * b * 0.13,
                                       py + math.sin(a) * b * 0.13, b * 0.115, 0.22,
                                       petals=5, color=palette[(n + k) % 6],
                                       heart=hearts[k % 3], twist=float(rng.random()),
                                       rng=rng)
                    continue
                petals, notch = species[n % 3]
                _carve_blossom(cv, px, py, b * radius, 0.26, petals=petals,
                               color=palette[n % 6], heart=hearts[n % 3],
                               notch=notch, twist=float(rng.random()) * 1.3, rng=rng)

    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 0.50), _CORNER_DIRS):
        _carve_rose(cv, cx, cy, b * 0.32, 0.30, color=palette[0], heart=hearts[0],
                    angle=math.atan2(dy, dx))

    for frac, width in ((0.055, 0.042), (0.945, 0.034)):
        for (a, bb, _t, _n) in _band_sides(w, h, b, frac):
            cv.ridge([a, bb], max(1.0, b * width), 0.22, color=gold)


def _carve_gloss(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Black lacquer: two gold fillets, nothing else — the reflection does the rest."""
    gold = (206, 168, 88)
    for frac, width in ((0.12, 0.05), (0.88, 0.038)):
        for (a, bb, _t, _n) in _band_sides(w, h, b, frac):
            cv.ridge([a, bb], max(1.0, b * width), 0.22, color=gold)


# ------------------------------------------------------------- spills
#
# The three foliage frames let a few motifs PASS OVER the photo. This is the
# second (and last) exception to the invariant "the frame never covers a single
# pixel of the image" — cf. the second frame of `plain`. Like it, it is purely a
# display matter: it enters neither `border_px()` nor `content_box()`, and the
# geometry of the interactive tools (crop, red eyes, faces, annotations) stays
# that of the whole photo.
#
# Two things make a spill read as a sculpture overhanging the image rather
# than as a sticker:
#  - the DROP SHADOW on the photo (`_SPILL_SHADOW`), offset along the axis of
#    `_LIGHT` — it is that, far more than the motif, which creates the depth;
#  - the fact that every motif stays ATTACHED to the band by a stem starting
#    from under the edge: an ornament floating in the middle of the image
#    looks like nothing at all.
# "Sometimes" is essential to the realism: a spill at regular intervals is a
# frieze again. Hence a per-site draw (`_SPILL_SKIP`) and a spacing of several
# band widths.

_SPILL_SHADOW = 0.5          # opacity of the drop shadow on the photo
_SPILL_SKIP = 0.34           # proportion of sites left empty
_SPILL_SPACING = 3.6         # spacing of the sites, in band widths


def _spill_sites(w: float, h: float, b: float, rng, inset: float) -> list:
    """Anchor points astride the inner edge of the band.

    Returns ``[(x, y, tx, ty, nx, ny)]`` — the point is offset by ``inset``
    towards the inside of the photo, so that the motif carved there rests half
    on the frame and half on the image."""
    sites = []
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, 1.0):
        length = math.hypot(bx - ax, by - ay)
        count = int(length // max(b * _SPILL_SPACING, 1.0))
        if count < 1:
            continue
        for k in range(count):
            if float(rng.random()) < _SPILL_SKIP:
                continue
            f = (k + 0.5 + (float(rng.random()) - 0.5) * 0.7) / count
            d = length * min(max(f, 0.08), 0.92)
            sites.append((ax + tx * d + nx * inset, ay + ty * d + ny * inset,
                          tx, ty, nx, ny))
    return sites


def _spill_corners(w: float, h: float, b: float, inset: float) -> list:
    """The 4 corners, offset diagonally towards the inside of the photo.

    A corner that spills over is the most characteristic gesture of a real
    carved frame — that is where the wood or the porcelain allows itself to bite
    into the canvas."""
    out = []
    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 1.0), _CORNER_DIRS):
        out.append((cx + dx * inset, cy + dy * inset, dx, dy))
    return out


def _spill_stem(cv: _Carver, px: float, py: float, nx: float, ny: float,
                tx: float, ty: float, length: float, width: float,
                color, peak: float) -> None:
    """Stem attaching a spilling motif to the band, at three points.

    It starts from UNDER the edge and stays short enough to disappear under the
    foliage: a straight, long stem crosses the ornaments of the band and reads
    as a pin stuck into the frame."""
    cv.ridge([(px - nx * length - tx * length * 0.28,
               py - ny * length - ty * length * 0.28),
              (px - nx * length * 0.45, py - ny * length * 0.45),
              (px, py)], max(1.0, width), peak, color=color, layers=3)


def _spill_vine(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Vine stems crossing the rebate: a leaf, a tendril, sometimes a bunch."""
    mid = _mix(_VINE_LIT, _VINE_SHADE, 0.55)
    for px, py, tx, ty, nx, ny in _spill_sites(w, h, b, rng, b * 0.16):
        ang = math.atan2(ny, nx) + (float(rng.random()) - 0.5) * 0.7
        _spill_stem(cv, px, py, nx, ny, tx, ty, b * 0.55, b * 0.055,
                    _VINE_SHADE, 0.20)
        # The same carpet of foliage as on the band, but spread INTO the photo:
        # without it the vine stem boils down to a leaf sitting alone on the
        # image, which reads as a brooch pinned on.
        for k, (side, out, size, turn) in enumerate(
                ((-0.46, 0.26, 0.46, 1.05), (0.46, 0.30, 0.46, -1.05))):
            _carve_foliage(cv, px - ny * b * side + nx * b * out,
                           py + nx * b * side + ny * b * out,
                           b * size, ang + turn, 0.18,
                           color=_VINE_DEEP if k else mid,
                           count=3, spread=0.82)
        # A distinctly flat leaf: isolated on the photo, a domed vine leaf reads
        # as a starfish made of modelling clay. Under that relief it is the
        # outline and the veins that draw it, not the volume — hence a modest
        # size too, the mass going to the foliage.
        _carve_grape_leaf(cv, px, py, b * (0.42 + 0.06 * float(rng.random())),
                          ang, 0.15, color=_VINE_LIT)
        # A bunch of grapes is what naturally hangs from a trellis: it is that,
        # more than the leaf, which justifies the spill — hence the largest mass
        # of the tuft.
        _carve_grapes(cv, px + tx * b * 0.44 + nx * b * 0.42,
                      py + ty * b * 0.44 + ny * b * 0.42,
                      b * 0.54, ang, 0.30, color=_VINE_SHADE)
    for cx, cy, dx, dy in _spill_corners(w, h, b, b * 0.42):
        ang = math.atan2(dy, dx)
        _spill_stem(cv, cx, cy, dx, dy, -dy, dx, b * 0.75, b * 0.06,
                    _VINE_SHADE, 0.20)
        for sgn in (1.0, -1.0):
            _carve_foliage(cv, cx - dy * b * 0.46 * sgn + dx * b * 0.22,
                           cy + dx * b * 0.46 * sgn + dy * b * 0.22,
                           b * 0.48, ang + sgn * 1.15, 0.20, color=mid,
                           count=3, spread=0.80)
        _carve_grape_leaf(cv, cx, cy, b * 0.46, ang, 0.16, color=_VINE_LIT)
        _carve_grapes(cv, cx - dy * b * 0.48 + dx * b * 0.30,
                      cy + dx * b * 0.48 + dy * b * 0.30, b * 0.52,
                      ang, 0.30, color=_VINE_SHADE)
        # The tendril stays confined to the corners: repeated along the edge,
        # that shiny, closed loop reads as a keyring ring.
        _carve_tendril(cv, cx + dx * b * 0.46, cy + dy * b * 0.46, -dy, dx,
                       b * 0.16, 0.18, 1.0)


def _spill_roses(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Roses falling back onto the image, carried by their foliage."""
    for i, (px, py, tx, ty, nx, ny) in enumerate(_spill_sites(w, h, b, rng, b * 0.12)):
        ang = math.atan2(ny, nx)
        _spill_stem(cv, px, py, nx, ny, tx, ty, b * 0.45, b * 0.055,
                    _ROSE_LEAVES[1], 0.18)
        # The foliage spills further than the flower: it is what attaches the
        # tuft to the band and spares it the look of a hanging pendant.
        for k, (off, size, turn) in enumerate(((-0.34, 0.44, 1.15),
                                               (0.34, 0.44, -1.15),
                                               (0.0, 0.38, 0.0))):
            _carve_foliage(cv, px - ny * b * off, py + nx * b * off,
                           b * size, ang + turn, 0.24,
                           color=_ROSE_LEAVES[(i + k) % 3], count=3, spread=0.74)
        _carve_bud(cv, px - ny * b * 0.44, py + nx * b * 0.44, b * 0.26,
                   ang + 0.9, 0.32, color=_ROSE_HUES[(i + 1) % 4],
                   leaf=_ROSE_LEAVES[0])
        _carve_rose(cv, px, py, b * 0.36, 0.40, color=_ROSE_HUES[i % 4],
                    heart=_ROSE_HEART, angle=float(rng.random()) * 1.2)
    for cx, cy, dx, dy in _spill_corners(w, h, b, b * 0.40):
        ang = math.atan2(dy, dx)
        for sgn in (1.0, -1.0):
            _carve_foliage(cv, cx - dy * b * 0.42 * sgn, cy + dx * b * 0.42 * sgn,
                           b * 0.46, ang + sgn * 1.15, 0.26, color=_ROSE_LEAVES[0],
                           count=3, spread=0.70)
        _carve_rose(cv, cx, cy, b * 0.40, 0.42, color=_ROSE_HUES[0],
                    heart=_ROSE_HEART, angle=ang)


def _spill_flowers(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Porcelain corollas biting into the image, in posies."""
    species = ((5, 0.20), (6, 0.0), (8, 0.10))
    for i, (px, py, tx, ty, nx, ny) in enumerate(_spill_sites(w, h, b, rng, b * 0.12)):
        ang = math.atan2(ny, nx)
        _spill_stem(cv, px, py, nx, ny, tx, ty, b * 0.42, b * 0.05,
                    _FLOWER_LEAVES[1], 0.16)
        for k, (off, size, turn) in enumerate(((-0.32, 0.42, 1.10),
                                               (0.32, 0.42, -1.10),
                                               (0.0, 0.36, 0.0))):
            _carve_foliage(cv, px - ny * b * off, py + nx * b * off,
                           b * size, ang + turn, 0.20,
                           color=_FLOWER_LEAVES[(i + k) % 3], count=3, spread=0.76)
        petals, notch = species[i % 3]
        _carve_blossom(cv, px, py, b * 0.32, 0.28, petals=petals,
                       color=_FLOWER_PALETTE[i % 6], heart=_FLOWER_HEARTS[i % 3],
                       notch=notch, twist=float(rng.random()) * 1.3, rng=rng)
        # One forget-me-not on each side: it is the second bud, never the flower
        # alone, that makes it read as a posy laid down rather than a cut-out motif.
        for k in range(2):
            a = ang + (1.25 if k else -1.25)
            _carve_blossom(cv, px + math.cos(a) * b * 0.40,
                           py + math.sin(a) * b * 0.40, b * 0.15, 0.22,
                           petals=5, color=_FLOWER_PALETTE[(i + 3 + k) % 6],
                           heart=_FLOWER_HEARTS[(i + k) % 3],
                           twist=float(rng.random()), rng=rng)
    for cx, cy, dx, dy in _spill_corners(w, h, b, b * 0.38):
        ang = math.atan2(dy, dx)
        for sgn in (1.0, -1.0):
            _carve_foliage(cv, cx - dy * b * 0.40 * sgn, cy + dx * b * 0.40 * sgn,
                           b * 0.44, ang + sgn * 1.15, 0.22,
                           color=_FLOWER_LEAVES[0], count=3, spread=0.72)
        _carve_rose(cv, cx, cy, b * 0.38, 0.32, color=_FLOWER_PALETTE[0],
                    heart=_FLOWER_HEARTS[0], angle=ang)


_SPILLERS = {
    "vine": _spill_vine,
    "roses": _spill_roses,
    "flowers": _spill_flowers,
}


_CARVERS = {
    "baroque": _carve_baroque,
    "pearl": _carve_pearl,
    "greek": _carve_greek,
    "artdeco": _carve_artdeco,
    "wood": _carve_wood,
    "metal": _carve_metal,
    "vine": _carve_vine,
    "roses": _carve_roses,
    "flowers": _carve_flowers,
    "gloss": _carve_gloss,
}


def _carve_layers(np, kind: str, width: int, height: int, border: float, rng):
    """(signed height map, colour layer) of the ornaments of a pattern.

    The layer is carved with supersampling (the polygons of ``ImageDraw`` have
    no antialiasing), downscaled, then blurred slightly: it is that blur which
    rounds the edges of the successive passes and turns a stack of outlines into
    volume."""
    carver = _CARVERS.get(kind)
    if carver is None:
        return None, None
    ss = _SS if max(width, height) * _SS <= 4200 else 1
    lw, lh = width * ss, height * ss
    hmap = Image.new("L", (lw, lh), 128)
    cmap = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    cv = _Carver(ImageDraw.Draw(hmap), ImageDraw.Draw(cmap, "RGBA"), border * ss)
    carver(cv, lw, lh, border * ss, rng)
    if ss > 1:
        hmap = hmap.resize((width, height), Image.LANCZOS)
        cmap = cmap.resize((width, height), Image.LANCZOS)
    hmap = hmap.filter(ImageFilter.GaussianBlur(max(1.0, border * 0.035)))
    harr = (np.asarray(hmap, dtype="float32") - 128.0) / 127.0
    return harr, (cmap if cv.painted else None)


# ------------------------------------------------------------------ fills

def _fill_solid(np, width, height, color):
    arr = np.empty((height, width, 3), dtype="float32")
    arr[:, :] = color
    return arr


def _fill_gradient(np, width, height, c1, c2):
    ys = np.linspace(0.0, 1.0, height, dtype="float32")[:, None]
    xs = np.linspace(0.0, 1.0, width, dtype="float32")[None, :]
    t = ((xs + ys) * 0.5)[:, :, None]
    a = np.array(c1, dtype="float32")
    b = np.array(c2, dtype="float32")
    return a + (b - a) * t


def _fill_glitter(np, width, height, c1, c2, rng):
    """A solid base + fine grain + bright flecks — a glitter look."""
    arr = _fill_solid(np, width, height, c1)
    grain = (rng.random((height, width, 1)).astype("float32") - 0.5) * 26.0
    arr = arr + grain
    spark = rng.random((height, width))
    mask = (spark > 0.988)[:, :, None]
    bright = np.array(_mix(c2, (255, 255, 255), 0.45), dtype="float32")
    arr = np.where(mask, bright, arr)
    mask2 = ((spark > 0.965) & (spark <= 0.988))[:, :, None]
    arr = np.where(mask2, np.array(c2, dtype="float32"), arr)
    return arr


def _fill_wood(np, width, height, dist, border, rng, c_dark, c_light):
    """Graining parallel to the edges (like a moulding cut lengthwise)."""
    n_low = _smooth_noise(np, width, height, max(3, width // 90), max(3, height // 90), rng)
    n_fine = _smooth_noise(np, width, height, max(4, width // 12), max(4, height // 12), rng)
    rings = np.sin((dist / max(border, 1.0)) * 21.0 + n_low * 7.0)
    mix = np.clip(0.5 + 0.5 * rings, 0.0, 1.0) * 0.62 + n_fine * 0.38
    a = np.array(c_dark, dtype="float32")
    b = np.array(c_light, dtype="float32")
    return a + (b - a) * mix[:, :, None]


def _fill_brushed(np, width, height, side, rng, c_base):
    """Brushed metal: fine striations following the LENGTH of each moulding.

    A horizontal moulding (top/bottom) is brushed horizontally: the striations
    are therefore horizontal lines, hence a noise varying fast in y."""
    n_fast_y = _smooth_noise(np, width, height, 6, max(4, height // 2), rng)
    n_fast_x = _smooth_noise(np, width, height, max(4, width // 2), 6, rng)
    vertical = (side == 1) | (side == 2)
    n = np.where(vertical, n_fast_x, n_fast_y)
    base = np.array(c_base, dtype="float32")
    return base[None, None, :] * (0.90 + 0.18 * n[:, :, None])


def _albedo_map(np, kind: str, mat: dict, width, height, dist, side, border, rng, height_map):
    """Diffuse colour of the band, before lighting."""
    if kind == "wood":
        arr = _fill_wood(np, width, height, dist, border, rng, (78, 44, 20), (172, 116, 62))
    elif kind == "metal":
        arr = _fill_brushed(np, width, height, side, rng, mat["albedo"])
    elif kind == "artdeco":
        arr = _fill_brushed(np, width, height, side, rng, mat["albedo"])
    elif kind == "greek":
        # A dark lacquered ground, gold on the reliefs: the Greek key must stand out.
        dark = np.array((36, 30, 26), dtype="float32")
        gold = np.array(mat["albedo"], dtype="float32")
        k = np.clip((height_map - 0.62) * 5.0, 0.0, 1.0)[:, :, None]
        arr = dark * (1.0 - k) + gold * k
    else:
        arr = _fill_solid(np, width, height, mat["albedo"])

    if mat.get("gilding"):
        # Gold leaf laid by hand: irregular, and worn on the ridges where the red
        # bole of the ground shows through. Without that wear, a computed gilding
        # stays a uniform yellow surface, never a gilded frame.
        n = _smooth_noise(np, width, height, max(4, width // 40), max(4, height // 40), rng)
        n_fine = _smooth_noise(np, width, height, max(6, width // 8), max(6, height // 8), rng)
        wear = (np.clip((height_map - 0.74) * 3.4, 0.0, 1.0)
                * np.clip((n - 0.48) * 2.6, 0.0, 1.0))[:, :, None]
        arr = arr * (1.0 - wear) + np.array((148, 62, 44), dtype="float32") * wear
        arr = arr * (0.93 + 0.14 * n_fine)[:, :, None]
    return arr


def _paint_over(np, arr, cmap):
    """Composites the colour layer of the painted motifs onto the albedo."""
    col = np.asarray(cmap, dtype="float32")
    a = (col[:, :, 3:4] / 255.0)
    return arr * (1.0 - a) + col[:, :, :3] * a


# ------------------------------------------------------------------ bands

# Moulding profile and material of each decorative motif.
_DECOR: dict[str, tuple[str, str, float]] = {
    # motif: (profile, material, amplitude of the ornaments)
    "baroque": ("ogee", "gold", 0.62),
    "pearl": ("ogee", "gold", 0.55),
    "greek": ("bevel", "gold", 0.50),
    "artdeco": ("steps", "silver", 0.50),
    "wood": ("cove", "walnut", 0.58),
    "metal": ("bevel", "silver", 0.45),
    # The three foliage frames cover the whole band: the "field" profile (a
    # flat field) so that the carving alone carries the relief.
    "vine": ("field", "bronze", 0.85),
    "roses": ("field", "carmine", 0.80),
    "flowers": ("field", "porcelain", 0.70),
    "gloss": ("round", "lacquer", 0.35),
}


def _band_array(np, kind: str, width: int, height: int, border: float, edit, rng):
    """Ground of the frame — a float32 (h, w, 3) array in 0-255."""
    dist, side = _edge_distance(np, width, height)
    t = np.clip(dist / max(border, 1.0), 0.0, 1.0)

    if kind == "plain":
        # A strict flat fill: no moulding, no fillet, no gradient — the surround
        # must stay exactly the chosen colour (a requested black is a true black, a
        # white a true white), failing which it reads as a frame in relief.
        color = _hex_to_rgb(getattr(edit, "frame_color", "#ffffff"), (255, 255, 255))
        return _fill_solid(np, width, height, color)

    if kind in ("simple", "double"):
        style = str(getattr(edit, "frame_style", "solid") or "solid")
        c1 = _hex_to_rgb(getattr(edit, "frame_color", "#f2f2f2"), (242, 242, 242))
        c2 = _hex_to_rgb(getattr(edit, "frame_color2", "#8c8c8c"), (140, 140, 140))
        if style == "gradient":
            arr = _fill_gradient(np, width, height, c1, c2)
        elif style == "glitter":
            arr = _fill_glitter(np, width, height, c1, c2, rng)
        else:
            arr = _fill_solid(np, width, height, c1)
        hmap = _profile_height(np, t, "flat")
        if kind == "double":
            # Concentric bands: outer frame | gap | inner frame.
            total = _raw_total(edit) or 1.0
            outer_t = _attr_frac(edit, "frame_width", 0.05) / total
            gap_t = _attr_frac(edit, "frame_gap", 0.02) / total
            gap_col = np.array(_hex_to_rgb(getattr(edit, "frame_gap_color", "#ffffff"),
                                           (255, 255, 255)), dtype="float32")
            inner_col = np.array(_hex_to_rgb(getattr(edit, "frame_inner_color", "#303030"),
                                             (48, 48, 48)), dtype="float32")
            in_gap = ((t > outer_t) & (t <= outer_t + gap_t))[:, :, None]
            in_inner = (t > outer_t + gap_t)[:, :, None]
            arr = np.where(in_gap, gap_col, arr)
            arr = np.where(in_inner, inner_col, arr)
            # The mount is set back between the two frames: it is that step, and not
            # a mere change of colour, which gives the double frame its depth.
            hmap = np.where(in_gap[:, :, 0], hmap - 0.16, hmap)
            hmap = np.where(in_inner[:, :, 0], hmap + 0.06, hmap).astype("float32")
        return _shade_relief(np, hmap, arr, _MATERIALS["paint"], border)

    spec = _DECOR.get(kind)
    if spec is None:
        return _fill_solid(np, width, height, (240, 240, 240))
    profile, material, orn_amp = spec
    mat = _MATERIALS[material]

    hmap = _profile_height(np, t, profile)
    orn, cmap = _carve_layers(np, kind, width, height, border, rng)
    if orn is not None:
        hmap = hmap + orn * np.float32(orn_amp)
    arr = _albedo_map(np, kind, mat, width, height, dist, side, border, rng, hmap)
    if cmap is not None:
        arr = _paint_over(np, arr, cmap)
    arr = _shade_relief(np, hmap, arr, mat, border)

    if kind == "gloss":
        # Lacquer: diagonal streaks of light, on top of the lighting of the relief
        # (a varnish reflects the room, not only the source).
        ys = np.linspace(0.0, 1.0, height, dtype="float32")[:, None]
        xs = np.linspace(0.0, 1.0, width, dtype="float32")[None, :]
        u = xs * 0.72 + ys * 0.68
        streak = (np.exp(-((u - 0.34) ** 2) / 0.0040) * 0.55
                  + np.exp(-((u - 0.47) ** 2) / 0.0008) * 0.45
                  + np.exp(-((u - 0.78) ** 2) / 0.0025) * 0.25)
        arr = arr + 255.0 * streak[:, :, None]
    return arr


def _spill_array(np, kind: str, width: int, height: int, border: float, rng):
    """RGBA layer of the motifs spilling over the photo, or ``None``.

    The same carving, the same material and the same light as the band — that is
    the condition for the piece passing over the image to read as the
    continuation of the frame. The difference is that there is no moulding under
    the ornaments (the base plane is flat) and that a silhouette is tracked in
    parallel, to know where the layer covers the photo and where it leaves it
    untouched."""
    spiller = _SPILLERS.get(kind)
    spec = _DECOR.get(kind)
    if spiller is None or spec is None:
        return None
    _profile, material, orn_amp = spec
    mat = _MATERIALS[material]

    ss = _SS if max(width, height) * _SS <= 4200 else 1
    lw, lh = width * ss, height * ss
    hmap = Image.new("L", (lw, lh), 128)
    cmap = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    mmap = Image.new("L", (lw, lh), 0)
    cv = _Carver(ImageDraw.Draw(hmap), ImageDraw.Draw(cmap, "RGBA"), border * ss,
                 ImageDraw.Draw(mmap))
    spiller(cv, lw, lh, border * ss, rng)
    if ss > 1:
        hmap = hmap.resize((width, height), Image.LANCZOS)
        cmap = cmap.resize((width, height), Image.LANCZOS)
        mmap = mmap.resize((width, height), Image.LANCZOS)
    cover = np.asarray(mmap, dtype="float32") / 255.0
    if float(cover.max()) < 0.01:
        return None
    hmap = hmap.filter(ImageFilter.GaussianBlur(max(1.0, border * 0.035)))
    harr = ((np.asarray(hmap, dtype="float32") - 128.0) / 127.0) * np.float32(orn_amp)

    arr = _fill_solid(np, width, height, mat["albedo"])
    if cv.painted:
        arr = _paint_over(np, arr, cmap)
    arr = np.clip(_shade_relief(np, harr, arr, mat, border), 0.0, 255.0)

    # Drop shadow: the blurred silhouette, offset along the axis of the light
    # (_LIGHT comes from the top left, so the shadow falls to the bottom right).
    off = max(1, int(round(border * 0.10)))
    drop = np.roll(np.roll(cover, off, axis=0), off, axis=1)
    shadow = np.clip(_gauss(np, drop, max(1.0, border * 0.10)) * _SPILL_SHADOW,
                     0.0, 1.0) * (1.0 - cover)

    alpha = np.clip(cover + shadow, 0.0, 1.0)
    # The shadow is pure black: in an "out = rgb·a + photo·(1-a)" composition,
    # a null tint with the alpha of the shadow darkens the image without colouring it.
    rgb = arr * (cover / np.maximum(alpha, 1e-6))[:, :, None]
    out = np.empty((height, width, 4), dtype="uint8")
    out[:, :, :3] = np.clip(rgb, 0, 255).astype("uint8")
    out[:, :, 3] = np.clip(alpha * 255.0, 0, 255).astype("uint8")
    return out


def _render_spill(kind: str, width: int, height: int, border: float):
    """Spill layer at the working resolution (``None`` if not applicable)."""
    import numpy as np

    rng = np.random.default_rng(20260805)   # deterministic rendering, like the band
    arr = _spill_array(np, kind, width, height, border, rng)
    if arr is None:
        return None
    return Image.fromarray(arr, mode="RGBA")


def _render_band(kind: str, width: int, height: int, border: float, edit) -> Image.Image:
    """Complete band at the working resolution."""
    import numpy as np

    rng = np.random.default_rng(20260802)   # deterministic rendering from one session to the next
    arr = _band_array(np, kind, width, height, border, edit, rng)
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), mode="RGB")


# ------------------------------------------------------------------ public API

def apply_frame(image: Image.Image, edit) -> Image.Image:
    """Returns a new image: ``image`` centred inside its frame.

    The original image is never covered — it is pasted last, on top of the band."""
    kind = frame_type(edit)
    if kind == "none":
        return image
    w, h = image.size
    border = border_px(edit, w, h)
    if border <= 0:
        return image

    full_w, full_h = w + 2 * border, h + 2 * border
    scale = min(1.0, _WORK_MAX / float(max(full_w, full_h)))
    work_w = max(8, int(round(full_w * scale)))
    work_h = max(8, int(round(full_h * scale)))
    work_b = max(2.0, border * scale)

    try:
        band = _render_band(kind, work_w, work_h, work_b, edit)
    except Exception as e:                       # no edit lost if the rendering fails
        logger.error("Erreur rendu du cadre %s : %s", kind, e)
        return image

    if (work_w, work_h) != (full_w, full_h):
        band = band.resize((full_w, full_h), Image.LANCZOS)

    photo = image if image.mode == "RGB" else image.convert("RGB")
    band.paste(photo, (border, border))

    # Spills: pasted AFTER the photo, otherwise it would cover them. A failure
    # here only costs the spill, never the frame.
    try:
        spill = _render_spill(kind, work_w, work_h, work_b)
    except Exception as e:
        logger.error("Erreur rendu du débordement %s : %s", kind, e)
        spill = None
    if spill is not None:
        if spill.size != (full_w, full_h):
            spill = spill.resize((full_w, full_h), Image.LANCZOS)
        band.paste(spill, (0, 0), spill)

    _draw_inner_overlay(band, edit, border, w, h)
    return band


# ------------------------------------------------------------- ironwork
#
# Ornaments of the second frame of "plain". They grow INWARDS into the photo:
# the strip of image left between the two frames (`frame_gap`) must stay clean,
# which is the whole point of the second frame. None of these strokes enters
# `border_px()`/`content_box()` — this is display, not geometry (cf. the module
# docstring).

def _iron_shades(color) -> tuple[tuple, tuple]:
    """Bevel colours (light, dark) of the light relief.

    A blend towards white / black rather than a multiplication: a black frame
    (the most common one) would have no relief at all with a plain factor."""
    return _mix(color, (255, 255, 255), 0.55), _mix(color, (0, 0, 0), 0.45)


def _iron_stroke(draw, pts: list, width: float, color, relief: bool) -> None:
    """Iron stroke: the core, plus a light/dark edging if ``relief``."""
    w = max(1, int(round(width)))
    if len(pts) < 2:
        return
    if relief and w >= 3:
        light, dark = _iron_shades(color)
        d = max(1.0, w * 0.30)
        draw.line([(x + d, y + d) for x, y in pts], fill=dark, width=w, joint="curve")
        draw.line([(x - d, y - d) for x, y in pts], fill=light, width=w, joint="curve")
    draw.line(pts, fill=color, width=w, joint="curve")


def _iron_poly(draw, poly: list, color, relief: bool) -> None:
    """Solid piece (finial, stud head) with the same relief as the strokes."""
    if len(poly) < 3:
        return
    if relief:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        d = max(1.0, 0.08 * max(max(xs) - min(xs), max(ys) - min(ys)))
        light, dark = _iron_shades(color)
        draw.polygon([(x + d, y + d) for x, y in poly], fill=dark)
        draw.polygon([(x - d, y - d) for x, y in poly], fill=light)
    draw.polygon(poly, fill=color)


def _fleuron_polygon(cx: float, cy: float, size: float, angle: float) -> list:
    """Finial: a dart pointing in the ``angle`` direction.

    Preferred to a lobed leaf for the ironwork — the latter reads as a round
    pastille at a small size, while a point stays identifiable."""
    shape = [(1.0, 0.0), (0.30, 0.32), (-0.10, 0.17),
             (-0.36, 0.0), (-0.10, -0.17), (0.30, -0.32)]
    pts = []
    for px, py in shape:
        rx, ry = _rotate(px * size, py * size, angle)
        pts.append((cx + rx, cy + ry))
    return pts


def _volute_points(x: float, y: float, dx: float, dy: float, radius: float,
                   sign: float, turns: float = 1.05, steps: int = 44) -> list:
    """Ironwork volute: starts at (x, y) in the (dx, dy) direction and coils
    towards the ``sign`` side (± 1) while tightening its radius."""
    ang = math.atan2(dy, dx)
    dang = sign * (turns * 2.0 * math.pi) / steps
    pts = [(x, y)]
    for i in range(steps):
        t = (i + 1) / steps
        adv = abs(dang) * radius * (1.0 - 0.62 * t)
        ang += dang
        x += math.cos(ang) * adv
        y += math.sin(ang) * adv
        pts.append((x, y))
    return pts


def _curl_sign(dx: float, dy: float, nx: float, ny: float) -> float:
    """Direction of rotation bringing the (dx, dy) direction back to the inner normal."""
    return 1.0 if (dx * ny - dy * nx) > 0 else -1.0


def _draw_corner_iron(draw, corner, e1, e2, unit, lw, color, relief) -> None:
    """A pair of symmetrical volutes in the corner, plus a finial on the bisector."""
    cx, cy = corner
    nx, ny = e1[0] + e2[0], e1[1] + e2[1]      # bisector, inwards
    norm = math.hypot(nx, ny) or 1.0
    nx, ny = nx / norm, ny / norm
    for ex, ey in (e1, e2):
        # The scroll starts from the line, comes back towards the corner then coils inwards.
        sx = cx + ex * unit * 1.25
        sy = cy + ey * unit * 1.25
        sign = _curl_sign(-ex, -ey, nx, ny)
        _iron_stroke(draw, _volute_points(sx, sy, -ex, -ey, unit * 0.46, sign),
                     lw, color, relief)
    _iron_poly(draw, _fleuron_polygon(cx + nx * unit * 0.66, cy + ny * unit * 0.66,
                                      unit * 0.30, math.atan2(ny, nx)),
               color, relief)


def _draw_iron_motif(draw, motif: str, box: tuple, thick: float, unit: float,
                     color, relief: bool) -> None:
    """Draws the ironwork motif on the ``box`` median line of the second frame."""
    x0, y0, x1, y1 = box
    iw, ih = x1 - x0, y1 - y0
    if iw <= 0 or ih <= 0:
        return
    lw = max(1.0, thick * 0.85)
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    # Unit vectors of the two sides starting from each corner.
    edges = [((1.0, 0.0), (0.0, 1.0)), ((-1.0, 0.0), (0.0, 1.0)),
             ((-1.0, 0.0), (0.0, -1.0)), ((1.0, 0.0), (0.0, -1.0))]
    # Sides: (start, end, tangent, inner normal).
    sides = [
        ((x0, y0), (x1, y0), (1.0, 0.0), (0.0, 1.0)),
        ((x1, y0), (x1, y1), (0.0, 1.0), (-1.0, 0.0)),
        ((x1, y1), (x0, y1), (-1.0, 0.0), (0.0, -1.0)),
        ((x0, y1), (x0, y0), (0.0, -1.0), (1.0, 0.0)),
    ]

    # Every motif adorns a continuous line, apart from the twisted bar as a flat
    # fill: with no relief, only the separated oblique strands give the twist (a
    # solid band behind them would make it disappear).
    if motif != "twist" or relief:
        _iron_stroke(draw, corners + [corners[0]], thick, color, relief)

    if motif == "corners":
        for corner, (e1, e2) in zip(corners, edges):
            _draw_corner_iron(draw, corner, e1, e2, unit, lw, color, relief)

    elif motif == "scrolls":
        for corner, (e1, e2) in zip(corners, edges):
            _draw_corner_iron(draw, corner, e1, e2, unit, lw, color, relief)
        spacing = max(unit * 1.9, 4.0)
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in sides:
            length = math.hypot(bx - ax, by - ay)
            free = length - 2.0 * unit * 2.2          # room taken by the corners
            if free <= spacing:
                continue
            count = max(1, int(free // spacing))
            span = count * spacing
            start = (length - span) / 2.0 + spacing / 2.0
            sign = _curl_sign(tx, ty, nx, ny)
            for k in range(count):
                d = start + k * spacing
                px, py = ax + tx * d, ay + ty * d
                # Scroll: two opposed volutes attached to the line, opening
                # inwards — the motif runs from one corner to the next.
                _iron_stroke(draw, _volute_points(px, py, tx, ty, unit * 0.40, sign),
                             lw, color, relief)
                _iron_stroke(draw, _volute_points(px, py, -tx, -ty, unit * 0.40, -sign),
                             lw, color, relief)
                _iron_poly(draw, _fleuron_polygon(px + nx * unit * 0.52,
                                                  py + ny * unit * 0.52,
                                                  unit * 0.24, math.atan2(ny, nx)),
                           color, relief)

    elif motif == "twist":
        # Twist: oblique strands repeated along the bar. The "Ornaments"
        # slider sets the pitch of the twist (tight ↔ loose).
        period = max(3.0, unit * 0.38)
        half = thick * 0.75
        light, dark = _iron_shades(color)
        bar = max(1, int(round(thick * (0.70 if relief else 1.0))))
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in sides:
            length = math.hypot(bx - ax, by - ay)
            count = max(2, int(length / period))
            for k in range(count + 1):
                d = length * k / count
                px, py = ax + tx * d, ay + ty * d
                # A slanted stick: the diagonal of the bar gives the twist.
                sx = px - (tx + nx) * half
                sy = py - (ty + ny) * half
                ex = px + (tx + nx) * half
                ey = py + (ty + ny) * half
                col = (light if k % 2 == 0 else dark) if relief else color
                draw.line([(sx, sy), (ex, ey)], fill=col, width=bar)
        # Finials at the corners and in the middle of each side, pointing inwards.
        for corner, (e1, e2) in zip(corners, edges):
            nx, ny = e1[0] + e2[0], e1[1] + e2[1]
            norm = math.hypot(nx, ny) or 1.0
            nx, ny = nx / norm, ny / norm
            _iron_poly(draw, _fleuron_polygon(corner[0] + nx * unit * 0.34,
                                              corner[1] + ny * unit * 0.34,
                                              unit * 0.46, math.atan2(ny, nx)),
                       color, relief)
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in sides:
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            # A finial laid on the bar (not floating beside it): the offset
            # stays below its half-length.
            _iron_poly(draw, _fleuron_polygon(mx + nx * unit * 0.26, my + ny * unit * 0.26,
                                              unit * 0.42, math.atan2(ny, nx)),
                       color, relief)

    elif motif == "studs":
        radius = max(1.5, unit * 0.30)
        spacing = max(radius * 3.4, 4.0)
        light, dark = _iron_shades(color)

        def _stud(cx: float, cy: float, r: float) -> None:
            poly = _circle_polygon(cx, cy, r, steps=28)
            if relief:
                d = max(1.0, r * 0.22)
                draw.polygon([(x + d, y + d) for x, y in poly], fill=dark)
                draw.polygon([(x - d, y - d) for x, y in poly], fill=light)
            draw.polygon(poly, fill=color)
            if relief:
                # A hammered facet: the head of a forged stud is not smooth.
                hr = r * 0.42
                draw.polygon(_circle_polygon(cx - r * 0.26, cy - r * 0.26, hr, steps=20),
                             fill=light)

        for cx, cy in corners:
            _stud(cx, cy, radius * 1.35)
        for (ax, ay), (bx, by), (tx, ty), _n in sides:
            length = math.hypot(bx - ax, by - ay)
            free = length - 2.0 * radius * 3.0
            if free <= spacing:
                continue
            count = max(1, int(free // spacing))
            span = count * spacing
            start = (length - span) / 2.0 + spacing / 2.0
            for k in range(count):
                d = start + k * spacing
                _stud(ax + tx * d, ay + ty * d, radius)


def _inner_motif_layer(motif: str, w: int, h: int, gap: int, thick: int,
                       ornament: float, color, relief: bool) -> "Image.Image | None":
    """RGBA layer (w × h) of the ironwork, ready to be pasted onto the photo.

    Rendered at a bounded working resolution then supersampled like the
    ornaments of the band (cf. ``_ornament_layer``): the motifs are smooth
    curves, the final enlargement does not show and the cost stays constant
    whatever the size of the export."""
    scale = min(1.0, _WORK_MAX / float(max(w, h)))
    ww = max(16, int(round(w * scale)))
    wh = max(16, int(round(h * scale)))
    ss = _SS if max(ww, wh) * _SS <= 4200 else 1
    lw_px, lh_px = ww * ss, wh * ss
    k = (lw_px + lh_px) / float(w + h)          # photo → layer

    t = max(1.0, thick * k)
    g = gap * k
    x0 = g + t / 2.0
    y0 = g + t / 2.0
    x1 = lw_px - 1 - g - t / 2.0
    y1 = lh_px - 1 - g - t / 2.0
    if x1 - x0 < 4 * t or y1 - y0 < 4 * t:
        return None

    # Reference size of the ornaments: a fraction of the short side (like every
    # width of this module, for an identical rendering at any resolution), never
    # thinner than the line carrying them nor large enough to invade the photo.
    short = min(lw_px, lh_px)
    unit = max(t * 2.0, min(short * 0.09 * ornament, short * 0.22))

    layer = Image.new("RGBA", (lw_px, lh_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    _draw_iron_motif(draw, motif, (x0, y0, x1, y1), t, unit, color, relief)
    if (lw_px, lh_px) != (w, h):
        layer = layer.resize((w, h), Image.LANCZOS)
    return layer


def _draw_inner_overlay(canvas: Image.Image, edit, border: int, w: int, h: int) -> None:
    """Paints the second frame of "plain" on top of the already pasted photo."""
    gap, thick = inner_overlay_px(edit, w, h)
    if thick <= 0:
        return
    color = _hex_to_rgb(getattr(edit, "frame_color", "#ffffff"), (255, 255, 255))
    relief = inner_relief(edit)
    motif = inner_motif(edit)
    x0 = border + gap
    y0 = border + gap
    x1 = border + w - 1 - gap
    y1 = border + h - 1 - gap
    if x1 - x0 < 2 * thick or y1 - y0 < 2 * thick:
        return

    if motif == "line":
        # The historical stroke: a strict flat fill, with no relief and no ornament,
        # whatever `frame_inner_relief` (an ironwork setting, cf. ORNAMENTED_MOTIFS) —
        # a migrated database must render exactly the same frame as before.
        # `width` draws the outline INWARDS from the rectangle: the outer edge of the
        # stroke therefore stays exactly `gap` px from the edge of the photo.
        ImageDraw.Draw(canvas).rectangle([x0, y0, x1, y1], outline=color, width=thick)
        return

    try:
        layer = _inner_motif_layer(motif, w, h, gap, thick,
                                   inner_ornament_scale(edit), color, relief)
    except Exception as e:                      # a failed ornament does not lose the frame
        logger.error("Erreur rendu de la ferronnerie %s : %s", motif, e)
        layer = None
    if layer is None:
        ImageDraw.Draw(canvas).rectangle([x0, y0, x1, y1], outline=color, width=thick)
        return
    # The alpha channel serves as the mask: no need to convert the canvas to RGBA.
    canvas.paste(layer, (border, border), layer)


def frame_preview(image: Image.Image, edit, size: int = 160) -> Image.Image:
    """Squarish preview of ``image`` framed, intended for the dialog gallery.

    Downscaled BEFORE framing: since the widths are relative, the rendering
    stays faithful to what the full resolution will give, for a fraction of the
    cost."""
    thumb = image.copy()
    thumb.thumbnail((size, size), Image.LANCZOS)
    return apply_frame(thumb, edit)
