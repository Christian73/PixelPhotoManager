# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Cadres décoratifs — retouche non destructive appliquée AUTOUR de la photo.

Le cadre n'empiète jamais sur l'image : la photo est collée telle quelle au
centre d'un canevas agrandi de ``border_px()`` pixels sur chaque bord. Toutes
les largeurs sont exprimées en fraction du plus petit côté de la photo, pour
qu'un même réglage rende identiquement sur une vignette de 220 px et sur un
export pleine résolution.

Unique exception, explicitement demandée : le **second cadre facultatif** du
motif ``plain`` (``frame_inner_enabled``) est peint PAR-DESSUS la photo, à
``frame_gap`` du bord — la bande d'image laissée visible entre les deux cadres
est l'effet recherché. Il n'agrandit pas le canevas et n'entre donc pas dans
``border_px()``/``content_box()`` : la géométrie des outils interactifs reste
celle de la photo entière. Ce second cadre porte une **ferronnerie**
(``INNER_MOTIFS`` : ligne simple, volutes d'angle, rinceaux courants, barreau
torsadé, clous forgés), rendue en relief léger ou en aplat strict
(``frame_inner_relief``) et dimensionnée par le curseur « Ornements »
(``frame_inner_ornament``). Les ornements se développent vers l'INTÉRIEUR depuis
la ligne : ils restent dans la photo et ne touchent jamais le bandeau extérieur.

Deux familles de motifs :

- **paramétriques** (``plain``, ``simple``, ``double``) — l'utilisateur choisit
  couleurs et largeurs. ``plain`` est un simple aplat d'une couleur (noir, blanc
  ou libre) sans aucun relief ; ``simple``/``double`` ajoutent un style de
  remplissage (uni / dégradé / pailleté), une moulure et, pour ``double``, un
  intervalle et un cadre intérieur ;
- **décoratifs** (vigne, roses, bois sculpté, métal, reflets, fleurs) — dessinés
  par code (aucune ressource image embarquée, donc aucun ajout au packaging et
  un rendu net à n'importe quelle résolution).

Rendu : le bandeau est produit à une résolution de travail bornée par
``_WORK_MAX`` puis agrandi à la taille finale (les dégradés et les ornements
sont des motifs doux — l'agrandissement ne se voit pas, alors qu'un rendu
pleine résolution coûterait plusieurs secondes et centaines de Mo sur un export
6000 px). Les ornements sont dessinés en suréchantillonnage ``_SS`` puis
réduits, faute d'anticrénelage dans ``ImageDraw``.
"""
import logging
import math

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# (identifiant, libellé affiché)
FRAME_TYPES: list[tuple[str, str]] = [
    ("none",    "Aucun"),
    ("plain",   "Entourage uni"),
    ("simple",  "Simple"),
    ("double",  "Double"),
    ("vine",    "Feuilles de vigne"),
    ("roses",   "Roses"),
    ("wood",    "Sculpture bois"),
    ("metal",   "Métallique"),
    ("gloss",   "Reflets"),
    ("flowers", "Fleurs"),
]

FRAME_LABELS: dict[str, str] = dict(FRAME_TYPES)

# Motifs dont les couleurs / largeurs sont réglables par l'utilisateur.
PARAMETRIC_FRAMES = {"plain", "simple", "double"}

# Sous-ensemble des cadres paramétriques offrant un style de remplissage
# (uni / dégradé / pailleté) et donc une seconde couleur. ``plain`` en est
# volontairement exclu : c'est un aplat d'une seule couleur, sans relief.
STYLED_FRAMES = {"simple", "double"}

# Couleurs prêtes à l'emploi proposées à côté du sélecteur (identifiant hexa, libellé).
QUICK_COLORS: list[tuple[str, str]] = [
    ("#000000", "Noir"),
    ("#ffffff", "Blanc"),
]

# Ferronnerie du second cadre de « plain » (identifiant, libellé). « line » est
# le motif historique — un simple trait — et reste le défaut.
INNER_MOTIFS: list[tuple[str, str]] = [
    ("line",    "Ligne simple"),
    ("corners", "Volutes d'angle"),
    ("scrolls", "Rinceaux courants"),
    ("twist",   "Barreau torsadé"),
    ("studs",   "Clous forgés"),
]

INNER_MOTIF_LABELS: dict[str, str] = dict(INNER_MOTIFS)

# Motifs dont la taille des ornements dépend du curseur « Ornements ».
ORNAMENTED_MOTIFS = {"corners", "scrolls", "twist", "studs"}

# Bornes du curseur « Ornements » (facteur d'échelle des motifs).
INNER_ORNAMENT_MIN = 0.4
INNER_ORNAMENT_MAX = 2.5

# Rendu de la ferronnerie : relief léger (biseau clair/sombre) ou aplat strict.
INNER_RELIEFS: list[tuple[bool, str]] = [
    (True,  "Relief léger"),
    (False, "Aplat strict"),
]

# Styles de remplissage des cadres paramétriques
COLOR_STYLES: list[tuple[str, str]] = [
    ("solid",    "Uni"),
    ("gradient", "Dégradé"),
    ("glitter",  "Pailleté"),
]

# Résolution maximale de rendu du bandeau (cf. docstring du module).
_WORK_MAX = 2000
# Suréchantillonnage des ornements (polygones sans anticrénelage dans PIL).
_SS = 2
# Largeur maximale d'un cadre, en fraction du plus petit côté (garde-fou : au
# delà, la photo disparaîtrait sous son propre cadre).
_MAX_FRACTION = 0.30


# ------------------------------------------------------------------ utilitaires

def _hex_to_rgb(value, default=(255, 255, 255)) -> tuple[int, int, int]:
    """'#rrggbb' / '#rgb' → (r, g, b). Retourne ``default`` si illisible."""
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
    """Éclaircit (>1) ou assombrit (<1) une couleur RGB."""
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


def _raw_total(edit) -> float:
    """Somme brute des bandes, avant plafonnement — sert de dénominateur au
    découpage concentric du cadre double, qui doit rester proportionnel même
    quand le total est ramené à _MAX_FRACTION."""
    kind = frame_type(edit)
    if kind == "none":
        return 0.0
    total = _attr_frac(edit, "frame_width", 0.05)
    if kind == "double":
        total += _attr_frac(edit, "frame_gap", 0.02)
        total += _attr_frac(edit, "frame_inner_width", 0.015)
    return total


def border_fraction(edit) -> float:
    """Épaisseur totale du cadre, en fraction du plus petit côté de la photo,
    plafonnée à ``_MAX_FRACTION`` (au delà, la photo disparaîtrait sous le cadre —
    le plafond par bande ne suffit pas, trois bandes au maximum les cumuleraient)."""
    return min(_MAX_FRACTION, _raw_total(edit))


def border_px(edit, width: int, height: int) -> int:
    """Épaisseur du cadre en pixels pour une photo ``width`` × ``height``."""
    frac = border_fraction(edit)
    if frac <= 0.0 or width <= 0 or height <= 0:
        return 0
    return max(2, int(round(frac * min(width, height))))


def inner_overlay_px(edit, width: int, height: int) -> tuple[int, int]:
    """Second cadre de « plain », posé SUR la photo : ``(intervalle, épaisseur)`` en px.

    Retourne ``(0, 0)`` si le motif n'en a pas. Contrairement au cadre extérieur,
    ce cadre-là n'agrandit pas le canevas : il est peint par-dessus l'image, à
    ``intervalle`` px du bord de la photo — la bande d'image restée entre les deux
    cadres est justement l'effet recherché. ``width``/``height`` sont les
    dimensions de la PHOTO (hors cadre extérieur)."""
    if frame_type(edit) != "plain" or not getattr(edit, "frame_inner_enabled", False):
        return (0, 0)
    short = min(width, height)
    if short <= 0:
        return (0, 0)
    thick = int(round(_attr_frac(edit, "frame_inner_width", 0.015) * short))
    if thick <= 0:
        return (0, 0)
    gap = int(round(_attr_frac(edit, "frame_gap", 0.02) * short))
    # Garde-fou : le second cadre ne doit jamais se refermer sur lui-même au
    # centre de la photo (les deux réglages se cumulent, chacun plafonné isolément).
    limit = max(1, short // 2 - 1)
    gap = max(0, min(gap, limit - 1))
    thick = max(1, min(thick, limit - gap))
    return (gap, thick)


def inner_motif(edit) -> str:
    """Motif de ferronnerie du second cadre (« line » si absent ou inconnu)."""
    motif = getattr(edit, "frame_inner_motif", "line") or "line"
    return motif if motif in INNER_MOTIF_LABELS else "line"


def inner_ornament_scale(edit) -> float:
    """Facteur d'échelle des ornements (curseur « Ornements »), borné."""
    try:
        value = float(getattr(edit, "frame_inner_ornament", 1.0))
    except (TypeError, ValueError):
        value = 1.0
    return max(INNER_ORNAMENT_MIN, min(INNER_ORNAMENT_MAX, value))


def inner_relief(edit) -> bool:
    """Vrai si la ferronnerie est rendue en relief léger, faux en aplat strict."""
    return bool(getattr(edit, "frame_inner_relief", True))


def content_box(edit, framed_w: float, framed_h: float) -> tuple[float, float, float, float]:
    """Inverse de ``border_px`` : zone occupée par la photo dans une image encadrée.

    Retourne ``(x, y, w, h)``. Sert à la visionneuse, qui reçoit le pixmap déjà
    encadré mais doit exprimer les coordonnées des outils interactifs
    (recadrage, yeux rouges, vignette, cadres de visages) dans l'espace de la
    photo, pas du cadre."""
    frac = border_fraction(edit)
    if frac <= 0.0 or framed_w <= 0 or framed_h <= 0:
        return (0.0, 0.0, float(framed_w), float(framed_h))
    # border_px arrondit à l'entier : on résout d'abord en continu (b = frac × petit
    # côté du contenu, framed = contenu + 2b), puis on cherche l'entier voisin qui
    # redonne exactement border_px — sans ça l'inverse dérive d'un pixel, et tout
    # ce qui est calé sur content_box (recadrage, bbox de visage) glisse d'autant.
    framed_s = min(framed_w, framed_h)
    b = int(round(frac * framed_s / (1.0 + 2.0 * frac)))
    for cand in (b, b - 1, b + 1, b - 2, b + 2):
        if cand < 0 or 2 * cand >= framed_s:
            continue
        s = framed_s - 2 * cand   # petit côté du contenu pour ce candidat
        if border_px(edit, s, s) == cand:
            b = cand
            break
    b = max(0, min(b, int((framed_s - 1) // 2)))
    return (float(b), float(b),
            max(1.0, float(framed_w) - 2 * b), max(1.0, float(framed_h) - 2 * b))


# ------------------------------------------------------------------ cartes numpy

def _edge_distance(np, width: int, height: int):
    """(distance au bord le plus proche, index du bord) — float32 (h, w).

    L'index du bord (0 haut, 1 gauche, 2 droite, 3 bas) découpe le bandeau en
    quatre trapèzes joints à 45° dans les coins, exactement comme les
    moulures d'un cadre réel."""
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
    """Bruit lisse (0-1) obtenu par agrandissement bicubique d'une petite grille."""
    cells_x = max(2, cells_x)
    cells_y = max(2, cells_y)
    small = (rng.random((cells_y, cells_x)) * 255).astype("uint8")
    img = Image.fromarray(small, mode="L").resize((width, height), Image.BICUBIC)
    return np.asarray(img, dtype="float32") / 255.0


def _bevel(np, dist, side, border: float, profile: str = "round",
           lights=(1.30, 1.14, 0.84, 0.72)):
    """Facteur d'éclairage (h, w) : moulure éclairée en haut/à gauche.

    ``lights`` = (haut, gauche, droite, bas)."""
    fac = np.where(side == 0, lights[0],
                   np.where(side == 1, lights[1],
                            np.where(side == 2, lights[2], lights[3]))).astype("float32")
    t = np.clip(dist / max(border, 1.0), 0.0, 1.0)
    if profile == "round":
        prof = 0.72 + 0.46 * np.sin(np.pi * t)
    elif profile == "flat":
        prof = 0.92 + 0.14 * np.sin(np.pi * t)
    else:  # "step" — gorge marquée près de la photo
        prof = 0.80 + 0.40 * np.sin(np.pi * t) - 0.25 * np.clip((t - 0.82) / 0.18, 0.0, 1.0)
    # Liseré sombre contre la photo : décolle visuellement le cadre de l'image.
    prof = prof * (1.0 - 0.35 * np.clip((t - 0.94) / 0.06, 0.0, 1.0))
    return (fac * prof).astype("float32")


# ------------------------------------------------------------------ remplissages

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
    """Base unie + grain fin + éclats brillants — aspect pailleté."""
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
    """Veinage parallèle aux bords (comme une moulure débitée dans la longueur)."""
    n_low = _smooth_noise(np, width, height, max(3, width // 90), max(3, height // 90), rng)
    n_fine = _smooth_noise(np, width, height, max(4, width // 12), max(4, height // 12), rng)
    rings = np.sin((dist / max(border, 1.0)) * 21.0 + n_low * 7.0)
    mix = np.clip(0.5 + 0.5 * rings, 0.0, 1.0) * 0.62 + n_fine * 0.38
    a = np.array(c_dark, dtype="float32")
    b = np.array(c_light, dtype="float32")
    return a + (b - a) * mix[:, :, None]


def _fill_brushed(np, width, height, side, rng, c_base):
    """Métal brossé : stries fines suivant la LONGUEUR de chaque moulure.

    Une moulure horizontale (haut/bas) est brossée horizontalement : les stries
    sont donc des lignes horizontales, donc un bruit qui varie vite en y."""
    n_fast_y = _smooth_noise(np, width, height, 6, max(4, height // 2), rng)
    n_fast_x = _smooth_noise(np, width, height, max(4, width // 2), 6, rng)
    vertical = (side == 1) | (side == 2)
    n = np.where(vertical, n_fast_x, n_fast_y)
    base = np.array(c_base, dtype="float32")
    return base[None, None, :] * (0.90 + 0.18 * n[:, :, None])


# ------------------------------------------------------------------ chemin du bandeau

def _ring_samples(width: float, height: float, border: float, step: float) -> list:
    """Échantillonne la ligne médiane du bandeau.

    Retourne ``[(x, y, tx, ty, nx, ny, s)]`` — position, tangente unitaire,
    normale unitaire dirigée vers l'intérieur, abscisse curviligne. Parcours
    horaire : la normale intérieure vaut toujours la tangente tournée de +90°."""
    half = border / 2.0
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
        nx, ny = -ty, tx          # rotation +90° → vers l'intérieur (parcours horaire)
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


# ------------------------------------------------------------------ ornements

def _rotate(px: float, py: float, angle: float) -> tuple[float, float]:
    ca, sa = math.cos(angle), math.sin(angle)
    return px * ca - py * sa, px * sa + py * ca


def _leaf_polygon(cx: float, cy: float, size: float, angle: float, lobes: float = 2.5) -> list:
    """Feuille lobée (vigne si ``lobes`` = 2.5, ovale simple si 0.5)."""
    pts = []
    steps = 46
    for i in range(steps + 1):
        th = -math.pi + 2.0 * math.pi * i / steps
        r = size * (0.42 + 0.58 * abs(math.cos(lobes * th)) ** 0.75)
        x, y = _rotate(r * math.cos(th), r * math.sin(th) * 0.86, angle)
        pts.append((cx + x, cy + y))
    return pts


def _draw_leaf(draw, cx, cy, size, angle, color, lobes=2.5) -> None:
    poly = _leaf_polygon(cx, cy, size, angle, lobes)
    draw.polygon(poly, fill=color, outline=_shade(color, 0.62))
    # nervure centrale
    ex, ey = _rotate(size * 0.92, 0.0, angle)
    draw.line([(cx - ex * 0.6, cy - ey * 0.6), (cx + ex, cy + ey)],
              fill=_shade(color, 0.66), width=max(1, int(size * 0.07)))


def _draw_rose(draw, cx, cy, radius, color) -> None:
    """Rose vue de dessus : couronnes de pétales décroissantes + cœur enroulé."""
    for ring, (scale, shade) in enumerate(((1.00, 0.72), (0.74, 0.88), (0.52, 1.06))):
        petals = 7 - ring
        pr = radius * scale
        col = _shade(color, shade)
        for k in range(petals):
            a = 2.0 * math.pi * k / petals + ring * 0.5
            px = cx + math.cos(a) * pr * 0.46
            py = cy + math.sin(a) * pr * 0.46
            rr = pr * 0.58
            draw.ellipse([px - rr, py - rr * 0.86, px + rr, py + rr * 0.86],
                         fill=col, outline=_shade(color, 0.55))
    heart = radius * 0.24
    draw.ellipse([cx - heart, cy - heart, cx + heart, cy + heart],
                 fill=_shade(color, 1.20), outline=_shade(color, 0.6))
    draw.arc([cx - heart * 1.9, cy - heart * 1.9, cx + heart * 1.9, cy + heart * 1.9],
             20, 300, fill=_shade(color, 0.6), width=max(1, int(radius * 0.06)))


def _draw_daisy(draw, cx, cy, radius, petal, heart) -> None:
    petals = 8
    for k in range(petals):
        a = 2.0 * math.pi * k / petals
        px = cx + math.cos(a) * radius * 0.52
        py = cy + math.sin(a) * radius * 0.52
        poly = _leaf_polygon(px, py, radius * 0.56, a, lobes=0.5)
        draw.polygon(poly, fill=petal, outline=_shade(petal, 0.78))
    hr = radius * 0.28
    draw.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=heart,
                 outline=_shade(heart, 0.7))


def _circle_polygon(cx: float, cy: float, radius: float, steps: int = 40) -> list:
    return [(cx + math.cos(2 * math.pi * i / steps) * radius,
             cy + math.sin(2 * math.pi * i / steps) * radius) for i in range(steps)]


def _draw_carved(draw, poly: list, depth: float, hollow: bool = False) -> None:
    """Grave ``poly`` en relief sur le fond, sans le recouvrir.

    Le motif n'est pas peint en aplat : on superpose deux copies décalées du
    contour, l'une claire l'autre sombre, en semi-transparence — le veinage du
    bois reste visible au travers, ce qui fait la différence entre une gravure
    et une gommette collée. ``hollow`` inverse l'éclairage (creux au lieu de
    bosse) : c'est l'alternance des deux qui donne le rythme d'une moulure."""
    d = max(1.0, depth)
    light = (255, 246, 226, 132)
    dark = (26, 12, 4, 132)
    top, bottom = (dark, light) if hollow else (light, dark)
    draw.polygon([(x + d, y + d) for x, y in poly], fill=bottom)
    draw.polygon([(x - d, y - d) for x, y in poly], fill=top)
    draw.polygon(poly, fill=(150, 96, 44, 46))


def _draw_tendril(draw, cx, cy, size, angle, color, width) -> None:
    """Vrille de vigne (spirale logarithmique courte)."""
    pts = []
    for i in range(22):
        t = i / 21.0
        r = size * (0.15 + 0.85 * t)
        a = angle + t * 3.4 * math.pi
        pts.append((cx + math.cos(a) * r * 0.55, cy + math.sin(a) * r * 0.55))
    draw.line(pts, fill=color, width=width, joint="curve")


def _ornament_layer(kind: str, width: int, height: int, border: float, rng,
                    colors: dict) -> "Image.Image | None":
    """Calque RGBA des ornements, à la taille (width, height) du bandeau."""
    if kind not in ("vine", "roses", "flowers", "wood", "metal"):
        return None
    ss = _SS if max(width, height) * _SS <= 4200 else 1
    w, h = width * ss, height * ss
    b = border * ss
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    # mode "RGBA" : les tracés semi-transparents se COMPOSENT entre eux au lieu
    # de s'écraser (défaut de ImageDraw) — indispensable aux motifs gravés.
    draw = ImageDraw.Draw(layer, "RGBA")
    lw = max(1, int(b * 0.06))

    if kind == "vine":
        stem = colors["stem"]
        samples = _ring_samples(w, h, b, max(2.0, b * 0.10))
        path = []
        for x, y, _tx, _ty, nx, ny, s in samples:
            off = math.sin(s / max(b * 0.85, 1.0)) * b * 0.20
            path.append((x + nx * off, y + ny * off))
        if len(path) > 2:
            draw.line(path + [path[0]], fill=stem, width=max(2, int(b * 0.075)), joint="curve")
        leaf_step = max(3, int(len(samples) / max(6, int((2 * (w + h)) / (b * 1.9)))))
        for idx in range(0, len(samples), leaf_step):
            x, y, tx, ty, nx, ny, s = samples[idx]
            sign = 1.0 if (idx // leaf_step) % 2 == 0 else -1.0
            base_a = math.atan2(ty, tx)
            lx = x + nx * sign * b * 0.20
            ly = y + ny * sign * b * 0.20
            _draw_leaf(draw, lx, ly, b * 0.40, base_a + sign * 1.15,
                       colors["leaf_a"] if sign > 0 else colors["leaf_b"])
            if (idx // leaf_step) % 3 == 1:
                _draw_tendril(draw, x - nx * sign * b * 0.18, y - ny * sign * b * 0.18,
                              b * 0.34, base_a, stem, max(1, int(b * 0.05)))
        for cx, cy in _corner_points(w, h, b):
            _draw_leaf(draw, cx, cy, b * 0.50, math.pi / 4, colors["leaf_a"])
            for k in range(5):     # petite grappe
                a = 2.0 * math.pi * k / 5
                gr = b * 0.11
                gx, gy = cx + math.cos(a) * gr, cy + math.sin(a) * gr
                draw.ellipse([gx - gr, gy - gr, gx + gr, gy + gr],
                             fill=colors["berry"], outline=_shade(colors["berry"], 0.6))

    elif kind == "roses":
        samples = _ring_samples(w, h, b, max(2.0, b * 0.10))
        span = max(6, int((2 * (w + h)) / (b * 2.1)))
        step = max(3, int(len(samples) / span))
        for idx in range(0, len(samples), step):
            x, y, tx, ty, nx, ny, _s = samples[idx]
            base_a = math.atan2(ty, tx)
            for sign in (1.0, -1.0):
                _draw_leaf(draw, x + nx * sign * b * 0.30, y + ny * sign * b * 0.30,
                           b * 0.30, base_a + sign * 1.0, colors["leaf"], lobes=0.5)
            _draw_rose(draw, x, y, b * 0.30,
                       colors["rose_a"] if (idx // step) % 2 == 0 else colors["rose_b"])
        for cx, cy in _corner_points(w, h, b):
            _draw_rose(draw, cx, cy, b * 0.40, colors["rose_a"])

    elif kind == "flowers":
        samples = _ring_samples(w, h, b, max(2.0, b * 0.10))
        span = max(6, int((2 * (w + h)) / (b * 1.6)))
        step = max(3, int(len(samples) / span))
        palette = colors["palette"]
        for idx in range(0, len(samples), step):
            x, y, tx, ty, nx, ny, _s = samples[idx]
            base_a = math.atan2(ty, tx)
            k = (idx // step) % len(palette)
            _draw_leaf(draw, x + nx * b * 0.28, y + ny * b * 0.28, b * 0.26,
                       base_a + 1.0, colors["leaf"], lobes=0.5)
            _draw_leaf(draw, x - nx * b * 0.28, y - ny * b * 0.28, b * 0.26,
                       base_a - 1.0, colors["leaf"], lobes=0.5)
            _draw_daisy(draw, x, y, b * 0.34, palette[k], colors["heart"])
        for cx, cy in _corner_points(w, h, b):
            _draw_daisy(draw, cx, cy, b * 0.42, palette[0], colors["heart"])

    elif kind == "wood":
        # Godrons sculptés le long de la moulure + rosace d'angle. Les motifs ne
        # sont pas peints en aplat mais gravés en relief (cf. _draw_carved) :
        # c'est le bois du fond qu'on doit continuer à voir dessous.
        samples = _ring_samples(w, h, b, max(2.0, b * 0.95))
        depth = max(1.0, b * 0.055)
        for idx, (x, y, tx, ty, nx, ny, _s) in enumerate(samples):
            a = math.atan2(ty, tx)
            for sign in (-1.0, 1.0):
                gx = x + nx * sign * b * 0.17
                gy = y + ny * sign * b * 0.17
                poly = _leaf_polygon(gx, gy, b * 0.24, a + math.pi / 2, lobes=0.5)
                _draw_carved(draw, poly, depth, hollow=(sign > 0))
        for cx, cy in _corner_points(w, h, b):
            r = b * 0.42
            _draw_carved(draw, _circle_polygon(cx, cy, r), depth * 1.4, hollow=False)
            for k in range(8):
                a = 2.0 * math.pi * k / 8
                poly = _leaf_polygon(cx + math.cos(a) * r * 0.46, cy + math.sin(a) * r * 0.46,
                                     r * 0.40, a, lobes=0.5)
                _draw_carved(draw, poly, depth, hollow=(k % 2 == 1))
            _draw_carved(draw, _circle_polygon(cx, cy, r * 0.30), depth, hollow=True)

    elif kind == "metal":
        # Rivets aux angles et au milieu de chaque moulure.
        spots = list(_corner_points(w, h, b))
        spots += [(w / 2, b / 2), (w / 2, h - b / 2), (b / 2, h / 2), (w - b / 2, h / 2)]
        for cx, cy in spots:
            r = b * 0.26
            draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                         fill=colors["rivet"], outline=colors["rivet_dark"], width=max(1, lw))
            hr = r * 0.44
            draw.ellipse([cx - r * 0.30 - hr, cy - r * 0.30 - hr,
                          cx - r * 0.30 + hr, cy - r * 0.30 + hr], fill=colors["rivet_light"])
        # Liserés brillants sur les arêtes intérieure et extérieure
        inset = b * 0.14
        draw.rectangle([inset, inset, w - inset, h - inset],
                       outline=colors["rivet_light"], width=max(1, int(b * 0.045)))

    if ss > 1:
        layer = layer.resize((width, height), Image.LANCZOS)
    return layer


# ------------------------------------------------------------------ bandeaux

def _band_array(np, kind: str, width: int, height: int, border: float, edit, rng):
    """Fond du cadre (hors ornements) — tableau float32 (h, w, 3) en 0-255."""
    dist, side = _edge_distance(np, width, height)

    if kind == "plain":
        # Aplat strict : ni moulure, ni liseré, ni dégradé — l'entourage doit
        # rester exactement la couleur choisie (un noir demandé est un vrai noir,
        # un blanc un vrai blanc), sans quoi il se lit comme un cadre en relief.
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
        if kind == "double":
            # Bandes concentriques : cadre extérieur | intervalle | cadre intérieur.
            total = _raw_total(edit) or 1.0
            outer_t = _attr_frac(edit, "frame_width", 0.05) / total
            gap_t = _attr_frac(edit, "frame_gap", 0.02) / total
            t = np.clip(dist / max(border, 1.0), 0.0, 1.0)
            gap_col = np.array(_hex_to_rgb(getattr(edit, "frame_gap_color", "#ffffff"),
                                           (255, 255, 255)), dtype="float32")
            inner_col = np.array(_hex_to_rgb(getattr(edit, "frame_inner_color", "#303030"),
                                             (48, 48, 48)), dtype="float32")
            in_gap = ((t > outer_t) & (t <= outer_t + gap_t))[:, :, None]
            in_inner = (t > outer_t + gap_t)[:, :, None]
            arr = np.where(in_gap, gap_col, arr)
            arr = np.where(in_inner, inner_col, arr)
            # Ombre douce de chaque côté de l'intervalle (relief du passe-partout)
            edge = np.exp(-(((t - outer_t) * max(border, 1.0)) ** 2) / (2 * (border * 0.05 + 1) ** 2))
            arr = arr * (1.0 - 0.22 * edge)[:, :, None]
        arr = arr * _bevel(np, dist, side, border, "flat")[:, :, None]
        return arr

    if kind == "wood":
        arr = _fill_wood(np, width, height, dist, border, rng, (86, 50, 22), (168, 112, 58))
        return arr * _bevel(np, dist, side, border, "step")[:, :, None]

    if kind == "metal":
        arr = _fill_brushed(np, width, height, side, rng, (150, 155, 165))
        return arr * _bevel(np, dist, side, border, "round", (1.26, 1.10, 0.80, 0.64))[:, :, None]

    if kind == "gloss":
        # Verre laqué sombre + traînées de lumière en diagonale.
        base = _fill_gradient(np, width, height, (26, 28, 34), (72, 78, 92))
        ys = np.linspace(0.0, 1.0, height, dtype="float32")[:, None]
        xs = np.linspace(0.0, 1.0, width, dtype="float32")[None, :]
        u = (xs * 0.72 + ys * 0.68)
        streak = (np.exp(-((u - 0.34) ** 2) / 0.0040) * 0.85
                  + np.exp(-((u - 0.47) ** 2) / 0.0008) * 0.65
                  + np.exp(-((u - 0.78) ** 2) / 0.0025) * 0.35)
        arr = base + (np.array([255.0, 255.0, 255.0]) * streak[:, :, None] * 0.55)
        return arr * _bevel(np, dist, side, border, "round", (1.22, 1.10, 0.88, 0.78))[:, :, None]

    if kind == "vine":
        arr = _fill_gradient(np, width, height, (32, 60, 34), (74, 108, 56))
        return arr * _bevel(np, dist, side, border, "flat")[:, :, None]

    if kind == "roses":
        arr = _fill_gradient(np, width, height, (68, 20, 32), (128, 52, 62))
        return arr * _bevel(np, dist, side, border, "round")[:, :, None]

    if kind == "flowers":
        arr = _fill_gradient(np, width, height, (250, 244, 232), (222, 206, 182))
        return arr * _bevel(np, dist, side, border, "flat")[:, :, None]

    return _fill_solid(np, width, height, (240, 240, 240))


_ORNAMENT_COLORS: dict[str, dict] = {
    "vine": {
        "stem": (96, 70, 36),
        "leaf_a": (108, 156, 62),
        "leaf_b": (78, 126, 50),
        "berry": (92, 58, 116),
    },
    "roses": {
        "rose_a": (214, 84, 96),
        "rose_b": (238, 158, 168),
        "leaf": (86, 128, 66),
    },
    "flowers": {
        "palette": [(232, 122, 148), (246, 198, 96), (150, 176, 232), (238, 146, 96)],
        "heart": (250, 214, 96),
        "leaf": (122, 164, 96),
    },
    "wood": {
        "carve_light": (196, 148, 92),
        "carve_dark": (92, 56, 26),
        "boss": (156, 104, 52),
    },
    "metal": {
        "rivet": (196, 200, 208),
        "rivet_light": (246, 248, 252),
        "rivet_dark": (96, 100, 108),
    },
}


def _render_band(kind: str, width: int, height: int, border: float, edit) -> Image.Image:
    """Bandeau complet (fond + ornements) à la résolution de travail."""
    import numpy as np

    rng = np.random.default_rng(20260802)   # rendu déterministe d'une session à l'autre
    arr = _band_array(np, kind, width, height, border, edit, rng)
    band = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), mode="RGB")
    colors = _ORNAMENT_COLORS.get(kind)
    if colors is not None:
        layer = _ornament_layer(kind, width, height, border, rng, colors)
        if layer is not None:
            band = band.convert("RGBA")
            band.alpha_composite(layer)
            band = band.convert("RGB")
    return band


# ------------------------------------------------------------------ API publique

def apply_frame(image: Image.Image, edit) -> Image.Image:
    """Retourne une nouvelle image : ``image`` centrée dans son cadre.

    L'image d'origine n'est jamais recouverte — elle est collée en dernier,
    par-dessus le bandeau."""
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
    except Exception as e:                       # pas de retouche perdue si le rendu échoue
        logger.error("Erreur rendu du cadre %s : %s", kind, e)
        return image

    if (work_w, work_h) != (full_w, full_h):
        band = band.resize((full_w, full_h), Image.LANCZOS)

    photo = image if image.mode == "RGB" else image.convert("RGB")
    band.paste(photo, (border, border))
    _draw_inner_overlay(band, edit, border, w, h)
    return band


# ------------------------------------------------------------- ferronnerie
#
# Ornements du second cadre de « plain ». Ils se développent VERS L'INTÉRIEUR
# de la photo : la bande d'image laissée entre les deux cadres (`frame_gap`)
# doit rester nette, c'est tout l'intérêt du second cadre. Aucun de ces tracés
# n'entre dans `border_px()`/`content_box()` — c'est de l'affichage, pas de la
# géométrie (cf. docstring du module).

def _iron_shades(color) -> tuple[tuple, tuple]:
    """Couleurs de biseau (clair, sombre) du relief léger.

    Mélange vers le blanc / le noir plutôt que multiplication : un cadre noir
    (le plus courant) n'aurait aucun relief avec un simple facteur."""
    return _mix(color, (255, 255, 255), 0.55), _mix(color, (0, 0, 0), 0.45)


def _iron_stroke(draw, pts: list, width: float, color, relief: bool) -> None:
    """Trait de fer : le cœur, plus un liseré clair/sombre si ``relief``."""
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
    """Pièce pleine (fleuron, tête de clou) avec le même relief que les traits."""
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
    """Fleuron : fer de lance pointé dans la direction ``angle``.

    Préféré à ``_leaf_polygon`` pour la ferronnerie — une feuille lobée se lit
    comme une pastille ronde à petite taille, une pointe reste identifiable."""
    shape = [(1.0, 0.0), (0.30, 0.32), (-0.10, 0.17),
             (-0.36, 0.0), (-0.10, -0.17), (0.30, -0.32)]
    pts = []
    for px, py in shape:
        rx, ry = _rotate(px * size, py * size, angle)
        pts.append((cx + rx, cy + ry))
    return pts


def _volute_points(x: float, y: float, dx: float, dy: float, radius: float,
                   sign: float, turns: float = 1.05, steps: int = 44) -> list:
    """Volute de ferronnerie : part de (x, y) dans la direction (dx, dy) et
    s'enroule du côté ``sign`` (± 1) en resserrant son rayon."""
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
    """Sens de rotation qui ramène la direction (dx, dy) vers la normale intérieure."""
    return 1.0 if (dx * ny - dy * nx) > 0 else -1.0


def _draw_corner_iron(draw, corner, e1, e2, unit, lw, color, relief) -> None:
    """Paire de volutes symétriques dans l'angle, plus un fleuron sur la bissectrice."""
    cx, cy = corner
    nx, ny = e1[0] + e2[0], e1[1] + e2[1]      # bissectrice, vers l'intérieur
    norm = math.hypot(nx, ny) or 1.0
    nx, ny = nx / norm, ny / norm
    for ex, ey in (e1, e2):
        # La volute part de la ligne, revient vers l'angle puis s'enroule à l'intérieur.
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
    """Trace le motif de ferronnerie sur la ligne médiane ``box`` du second cadre."""
    x0, y0, x1, y1 = box
    iw, ih = x1 - x0, y1 - y0
    if iw <= 0 or ih <= 0:
        return
    lw = max(1.0, thick * 0.85)
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    # Vecteurs unitaires des deux côtés partant de chaque angle.
    edges = [((1.0, 0.0), (0.0, 1.0)), ((-1.0, 0.0), (0.0, 1.0)),
             ((-1.0, 0.0), (0.0, -1.0)), ((1.0, 0.0), (0.0, -1.0))]
    # Côtés : (départ, arrivée, tangente, normale intérieure).
    sides = [
        ((x0, y0), (x1, y0), (1.0, 0.0), (0.0, 1.0)),
        ((x1, y0), (x1, y1), (0.0, 1.0), (-1.0, 0.0)),
        ((x1, y1), (x0, y1), (-1.0, 0.0), (0.0, -1.0)),
        ((x0, y1), (x0, y0), (0.0, -1.0), (1.0, 0.0)),
    ]

    # Tous les motifs ornent une ligne continue, sauf le barreau torsadé en
    # aplat : sans relief, seuls les brins obliques séparés donnent la torsade
    # (une bande pleine derrière eux la ferait disparaître).
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
            free = length - 2.0 * unit * 2.2          # place tenue par les angles
            if free <= spacing:
                continue
            count = max(1, int(free // spacing))
            span = count * spacing
            start = (length - span) / 2.0 + spacing / 2.0
            sign = _curl_sign(tx, ty, nx, ny)
            for k in range(count):
                d = start + k * spacing
                px, py = ax + tx * d, ay + ty * d
                # Rinceau : deux volutes opposées attachées à la ligne, ouvertes
                # vers l'intérieur — le motif court d'un angle à l'autre.
                _iron_stroke(draw, _volute_points(px, py, tx, ty, unit * 0.40, sign),
                             lw, color, relief)
                _iron_stroke(draw, _volute_points(px, py, -tx, -ty, unit * 0.40, -sign),
                             lw, color, relief)
                _iron_poly(draw, _fleuron_polygon(px + nx * unit * 0.52,
                                                  py + ny * unit * 0.52,
                                                  unit * 0.24, math.atan2(ny, nx)),
                           color, relief)

    elif motif == "twist":
        # Torsade : brins obliques répétés le long du barreau. Le curseur
        # « Ornements » règle le pas de la torsion (serrée ↔ lâche).
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
                # Bâtonnet en biais : la diagonale du barreau donne la torsion.
                sx = px - (tx + nx) * half
                sy = py - (ty + ny) * half
                ex = px + (tx + nx) * half
                ey = py + (ty + ny) * half
                col = (light if k % 2 == 0 else dark) if relief else color
                draw.line([(sx, sy), (ex, ey)], fill=col, width=bar)
        # Fleurons aux angles et au milieu de chaque côté, pointés vers l'intérieur.
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
            # Fleuron posé sur le barreau (pas flottant à côté) : le décalage
            # reste inférieur à sa demi-longueur.
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
                # Facette martelée : la tête d'un clou forgé n'est pas lisse.
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
    """Calque RGBA (w × h) de la ferronnerie, prêt à être collé sur la photo.

    Rendu à résolution de travail bornée puis suréchantillonné comme les
    ornements du bandeau (cf. ``_ornament_layer``) : les motifs sont des courbes
    douces, l'agrandissement final ne se voit pas et le coût reste constant quelle
    que soit la taille de l'export."""
    scale = min(1.0, _WORK_MAX / float(max(w, h)))
    ww = max(16, int(round(w * scale)))
    wh = max(16, int(round(h * scale)))
    ss = _SS if max(ww, wh) * _SS <= 4200 else 1
    lw_px, lh_px = ww * ss, wh * ss
    k = (lw_px + lh_px) / float(w + h)          # photo → calque

    t = max(1.0, thick * k)
    g = gap * k
    x0 = g + t / 2.0
    y0 = g + t / 2.0
    x1 = lw_px - 1 - g - t / 2.0
    y1 = lh_px - 1 - g - t / 2.0
    if x1 - x0 < 4 * t or y1 - y0 < 4 * t:
        return None

    # Taille de référence des ornements : une fraction du petit côté (comme
    # toutes les largeurs du module, pour un rendu identique à toute résolution),
    # jamais plus fine que le trait qui les porte ni assez grosse pour envahir
    # la photo.
    short = min(lw_px, lh_px)
    unit = max(t * 2.0, min(short * 0.09 * ornament, short * 0.22))

    layer = Image.new("RGBA", (lw_px, lh_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    _draw_iron_motif(draw, motif, (x0, y0, x1, y1), t, unit, color, relief)
    if (lw_px, lh_px) != (w, h):
        layer = layer.resize((w, h), Image.LANCZOS)
    return layer


def _draw_inner_overlay(canvas: Image.Image, edit, border: int, w: int, h: int) -> None:
    """Peint le second cadre de « plain » par-dessus la photo déjà collée."""
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
        # Trait historique : aplat strict, sans relief ni ornement, quel que soit
        # `frame_inner_relief` (réglage de ferronnerie, cf. ORNAMENTED_MOTIFS) —
        # une base migrée doit rendre exactement le même cadre qu'avant.
        # `width` dessine le contour VERS L'INTÉRIEUR du rectangle : le bord externe
        # du trait reste donc exactement à `gap` px du bord de la photo.
        ImageDraw.Draw(canvas).rectangle([x0, y0, x1, y1], outline=color, width=thick)
        return

    try:
        layer = _inner_motif_layer(motif, w, h, gap, thick,
                                   inner_ornament_scale(edit), color, relief)
    except Exception as e:                      # un ornement raté ne perd pas le cadre
        logger.error("Erreur rendu de la ferronnerie %s : %s", motif, e)
        layer = None
    if layer is None:
        ImageDraw.Draw(canvas).rectangle([x0, y0, x1, y1], outline=color, width=thick)
        return
    # Le canal alpha sert de masque : inutile de convertir le canevas en RGBA.
    canvas.paste(layer, (border, border), layer)


def frame_preview(image: Image.Image, edit, size: int = 160) -> Image.Image:
    """Aperçu carré-ish d'``image`` encadrée, destiné à la galerie du dialogue.

    Réduit AVANT d'encadrer : les largeurs étant relatives, le rendu reste
    fidèle à ce que donnera la pleine résolution, pour une fraction du coût."""
    thumb = image.copy()
    thumb.thumbnail((size, size), Image.LANCZOS)
    return apply_frame(thumb, edit)
