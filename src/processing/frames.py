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
- **décoratifs** (baroque, oves et perles, grecque, art déco, vigne, roses, bois
  sculpté, métal, reflets, fleurs) — entièrement dessinés par code (aucune
  ressource image embarquée, donc aucun ajout au packaging et un rendu net à
  n'importe quelle résolution).

Rendu des cadres décoratifs — c'est un moteur de RELIEF, pas un dessin :

1. une **section de moulure** (``_PROFILE_SEGMENTS`` : chant, tore, scotie,
   doucine, filet, feuillure) donne la hauteur du bandeau en fonction de la
   distance au bord ;
2. les **ornements sont gravés** dans cette carte de hauteurs
   (``_Carver``/``_CARVERS`` : acanthes, coquilles, oves, perles, grecque,
   godrons, roses, grappes…) et non peints en aplat ;
3. ``_shade_relief()`` éclaire le tout — normales déduites du gradient, diffus
   de Lambert, reflet de Blinn-Phong, occlusion approchée des creux et patine
   qui s'y dépose (bol rouge sous la dorure, vert-de-gris du bronze).

C'est l'étape 3 qui fait la différence entre un motif qui se lit comme un
autocollant et un motif qui se lit comme de la matière sculptée : un ornement
plaqué en couleur reste plat quel que soit son dessin, le même ornement en
relief prend la lumière du cadre et projette ses ombres dans ses propres creux.

Le bandeau est produit à une résolution de travail bornée par ``_WORK_MAX`` puis
agrandi à la taille finale (un relief éclairé est un motif doux — l'agrandissement
ne se voit pas, alors qu'un rendu pleine résolution coûterait plusieurs secondes
et centaines de Mo sur un export 6000 px). Les ornements sont gravés en
suréchantillonnage ``_SS`` puis réduits, faute d'anticrénelage dans ``ImageDraw``.
"""
import logging
import math

from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

# (identifiant, libellé affiché)
FRAME_TYPES: list[tuple[str, str]] = [
    ("none",    "Aucun"),
    ("plain",   "Entourage uni"),
    ("simple",  "Simple"),
    ("double",  "Double"),
    ("baroque", "Baroque doré"),
    ("pearl",   "Oves et perles"),
    ("greek",   "Grecque"),
    ("artdeco", "Art déco"),
    ("wood",    "Sculpture bois"),
    ("vine",    "Feuilles de vigne"),
    ("roses",   "Roses"),
    ("flowers", "Fleurs"),
    ("metal",   "Métallique"),
    ("gloss",   "Reflets"),
]

# Un cadre sculpté n'a de sens qu'à partir d'une certaine épaisseur : sous ~8 %
# du petit côté, les acanthes ou les oves tiennent dans quelques pixels et se
# réduisent à une bouillie. Le dialogue relève la largeur à ce plancher quand on
# choisit un motif décoratif — visiblement, dans le curseur, jamais en douce au
# moment du rendu (le curseur mentirait sur le résultat).
DECOR_MIN_WIDTH = 0.08

FRAME_LABELS: dict[str, str] = dict(FRAME_TYPES)

# Motifs dont les couleurs / largeurs sont réglables par l'utilisateur.
PARAMETRIC_FRAMES = {"plain", "simple", "double"}

# Sous-ensemble des cadres paramétriques offrant un style de remplissage
# (uni / dégradé / pailleté) et donc une seconde couleur. ``plain`` en est
# volontairement exclu : c'est un aplat d'une seule couleur, sans relief.
STYLED_FRAMES = {"simple", "double"}

# Cadres végétaux dont quelques motifs débordent sur la photo (cf. la section
# « débordements » plus bas) : les seuls, avec le second cadre de ``plain``, à
# poser de la matière par-dessus l'image.
SPILL_FRAMES = {"vine", "roses", "flowers"}

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


def suggested_width(kind: str, current: float) -> float:
    """Largeur à proposer quand l'utilisateur choisit ``kind``.

    Un motif sculpté a besoin de matière : en dessous de ``DECOR_MIN_WIDTH``, la
    frise n'a plus la place d'exister. On relève donc la largeur courante — sans
    jamais la réduire, un choix plus large restant celui de l'utilisateur."""
    if kind in PARAMETRIC_FRAMES or kind == "none":
        return current
    return max(current, DECOR_MIN_WIDTH)


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


def _gauss(np, arr, radius: float, hi: float = 1.4):
    """Flou gaussien d'une carte flottante (0..``hi``), via PIL.

    La quantification sur 8 bits est sans conséquence ici : cette carte ne sert
    qu'à l'occlusion des creux, pas au relief lui-même."""
    if radius < 0.5:
        return arr
    img = Image.fromarray(np.clip(arr * (255.0 / hi), 0, 255).astype("uint8"), mode="L")
    img = img.filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(img, dtype="float32") * (hi / 255.0)


# ------------------------------------------------------------------ profils de moulure
#
# Une moulure réelle a une SECTION — chant extérieur, tore, scotie creuse,
# doucine, filet, feuillure — et c'est elle, bien plus que la couleur, qui fait
# qu'un cadre « tient » sous la lumière. Chaque profil est décrit par des
# segments (largeur relative, forme, hauteur de départ, hauteur d'arrivée,
# amplitude), échantillonnés une fois en table puis interpolés sur la carte de
# distance au bord : t = 0 sur l'arête extérieure, t = 1 contre la photo.

_PROFILE_N = 320

# Lumière du studio : haute, à gauche, légèrement en avant. Toutes les moulures
# du projet sont éclairées par elle — c'est ce qui rend les cadres cohérents
# entre eux (et avec l'ombre portée que l'œil attend en haut à gauche).
_LIGHT = (-0.4364, -0.5624, 0.7025)
_HALF = (-0.2564, -0.3305, 0.9083)      # normalisé (L + vue) — Blinn-Phong


def _profile_lut(segments) -> tuple[list, list]:
    """(ts, hs) — table hauteur(t) d'une section de moulure."""
    total = sum(s[0] for s in segments) or 1.0
    ts: list[float] = []
    hs: list[float] = []
    pos = 0.0
    for width, shape, h0, h1, amp in segments:
        count = max(2, int(round(_PROFILE_N * width / total)))
        for i in range(count):
            u = i / (count - 1)
            base = h0 + (h1 - h0) * u
            if shape == "round":            # tore / doucine bombée
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


# (largeur, forme, h_départ, h_arrivée, amplitude)
_PROFILE_SEGMENTS: dict[str, list] = {
    # Cadre doré classique : chant, tore, grande scotie, doucine à ornements,
    # filet, puis feuillure qui redescend contre la photo.
    "ogee": [
        (0.05, "step",  0.28, 0.66, 0.0),
        (0.11, "round", 0.66, 0.60, 0.15),
        (0.28, "cove",  0.60, 0.44, 0.30),
        (0.30, "round", 0.44, 0.66, 0.24),
        (0.10, "round", 0.66, 0.52, 0.11),
        (0.16, "step",  0.52, 0.14, 0.0),
    ],
    # Moulure creuse peinte (bois, roses) — large gorge, épaulement intérieur.
    "cove": [
        (0.07, "step",  0.32, 0.62, 0.0),
        (0.12, "round", 0.62, 0.58, 0.13),
        (0.44, "cove",  0.58, 0.50, 0.24),
        (0.22, "round", 0.50, 0.64, 0.15),
        (0.15, "step",  0.64, 0.18, 0.0),
    ],
    # Plate-bande à peine bombée : la section des cadres réglables, qui doivent
    # rendre la couleur choisie sans la noyer sous un relief.
    "flat": [
        (0.05, "step",  0.44, 0.74, 0.0),
        (0.10, "round", 0.74, 0.72, 0.06),
        (0.62, "round", 0.72, 0.72, 0.05),
        (0.08, "round", 0.72, 0.66, 0.05),
        (0.15, "step",  0.66, 0.26, 0.0),
    ],
    # Chanfreins vifs : métal, art déco.
    "bevel": [
        (0.04, "step",  0.38, 0.80, 0.0),
        (0.34, "line",  0.80, 0.56, 0.0),
        (0.28, "round", 0.56, 0.58, 0.08),
        (0.19, "line",  0.58, 0.76, 0.0),
        (0.15, "step",  0.76, 0.20, 0.0),
    ],
    # Demi-rond plein : laque, verre.
    "round": [
        (0.05, "step",  0.30, 0.54, 0.0),
        (0.76, "round", 0.54, 0.52, 0.34),
        (0.19, "step",  0.52, 0.16, 0.0),
    ],
    # Gradins : la section en escalier de l'Art déco, où le décor n'est pas
    # rapporté sur la moulure — c'est la moulure elle-même.
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
    # Gorge de porcelaine : creux doux et lèvre relevée.
    "scoop": [
        (0.07, "step",  0.40, 0.68, 0.0),
        (0.58, "cove",  0.68, 0.58, 0.26),
        (0.20, "round", 0.58, 0.68, 0.11),
        (0.15, "step",  0.68, 0.22, 0.0),
    ],
    # Champ plat entre deux baguettes : la section des cadres dont le décor
    # couvre TOUTE la largeur (vigne, roses, fleurs). Une moulure creuse y
    # ajouterait son propre relief, qui se superpose à celui des ornements et
    # noie le dessin — ici la sculpture est seule à porter la lumière.
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


# ------------------------------------------------------------------ matériaux
#
# Un matériau = albédo (couleur diffuse), part spéculaire, dureté du reflet,
# occlusion des creux et teinte qui s'y dépose (bol rouge sous la dorure,
# vert-de-gris du bronze, encrassement d'un bois ancien). C'est ce dernier
# point qui donne l'aspect « ancien » : un cadre neuf est propre au fond de
# ses creux, un cadre travaillé ne l'est jamais.

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
    # Le décor peint qui la recouvre doit rester velouté : un spéculaire de
    # laque sur des pétales leur donne un aspect de plastique moulé.
    "carmine": _material(albedo=(112, 26, 34), ambient=0.36, spec=0.24, shine=28,
                         ao=0.50, cavity_tint=(46, 12, 16), cavity_mix=0.45),
    "porcelain": _material(albedo=(244, 237, 224), ambient=0.50, spec=0.42, shine=44,
                           ao=0.42, relief=0.85, cavity_tint=(176, 162, 140),
                           cavity_mix=0.40),
    "paint": _material(ambient=0.38, spec=0.26, shine=30, ao=0.40, relief=0.85),
}


def _shade_relief(np, height, albedo, mat: dict, border: float):
    """Éclaire une carte de hauteurs — le cœur du rendu « sculpté ».

    Les normales sont déduites du gradient de la carte (l'amplitude du relief
    est proportionnelle à l'épaisseur du cadre, donc identique sur une vignette
    et sur un export), puis éclairées en Lambert + Blinn-Phong. L'occlusion
    approchée (différence entre la carte et sa version floutée) assombrit les
    creux et y dépose la patine : sans elle, un relief correct reste plat à
    l'œil."""
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


# ------------------------------------------------------------------ chemin du bandeau

def _ring_samples(width: float, height: float, border: float, step: float,
                  frac: float = 0.5) -> list:
    """Échantillonne une ligne continue du bandeau, à ``frac`` × ``border`` de
    l'arête extérieure (0,5 = ligne médiane).

    Retourne ``[(x, y, tx, ty, nx, ny, s)]`` — position, tangente unitaire,
    normale unitaire dirigée vers l'intérieur, abscisse curviligne. Parcours
    horaire : la normale intérieure vaut toujours la tangente tournée de +90°.

    Contrairement à ``_band_sides``, l'abscisse curviligne court sans rupture
    d'un côté à l'autre : c'est ce qui permet à une ondulation (sarment, ruban)
    de garder sa phase en passant les angles."""
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


def _band_sides(w: float, h: float, b: float, frac: float) -> list:
    """Les 4 côtés d'une ligne tracée à ``frac`` × ``b`` de l'arête extérieure.

    Chaque entrée vaut ``(départ, arrivée, tangente, normale vers l'intérieur)``
    — de quoi répartir un motif le long d'une moulure sans réécrire la
    trigonométrie à chaque fois."""
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
# Bissectrices vers l'intérieur, dans l'ordre de _band_corners.
_CORNER_DIRS = [(_DIAG, _DIAG), (-_DIAG, _DIAG), (-_DIAG, -_DIAG), (_DIAG, -_DIAG)]


def _distribute(length: float, spacing: float, margin: float) -> list:
    """Abscisses d'un motif répété, centrées sur le côté et à l'écart des angles.

    Centrer plutôt que partir d'un bout est ce qui distingue une frise dessinée
    d'une frise réelle : les deux extrémités d'un côté doivent se répondre."""
    spacing = max(spacing, 1.0)
    free = length - 2.0 * margin
    if free <= spacing:
        return []
    count = max(1, int(free // spacing))
    start = (length - count * spacing) / 2.0 + spacing / 2.0
    return [start + k * spacing for k in range(count)]


# ------------------------------------------------------------------ sculpture
#
# Les ornements ne sont pas peints : ils sont GRAVÉS dans la carte de hauteurs,
# puis éclairés par _shade_relief avec le reste de la moulure. Un motif peint en
# aplat se lit comme un autocollant ; le même motif en relief prend la lumière du
# cadre, projette ses ombres dans ses creux et devient de la matière.
#
# Le calque de couleur (facultatif) sert aux motifs réellement peints —
# porcelaine, roses — dont la teinte ne se déduit pas du matériau du bandeau.

class _Carver:
    """Ciseau du sculpteur : écrit dans la carte de hauteurs (et la couleur).

    La carte est une image « L » dont 128 est le niveau du bandeau : au-dessus
    on ajoute de la matière, en dessous on creuse. Les tracés s'écrasent dans
    l'ordre d'appel, comme des passes de gouge successives."""

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

    # Silhouette des ornements — utile au seul calque de débordement, qui doit
    # savoir où il couvre la photo. Seules les passes qui AJOUTENT de la matière
    # (dome/flat/ridge) y contribuent : une rainure ne fait jamais silhouette,
    # elle se creuse dans un motif déjà posé, et l'inscrire ici laisserait
    # traîner des griffures noires sur l'image là où elle dépasse du contour.
    def _cover(self, poly) -> None:
        if self._m is not None:
            self._m.polygon(poly, fill=255)

    def _cover_line(self, pts, width: float) -> None:
        if self._m is not None:
            self._m.line(pts, fill=255, width=max(1, int(round(width))),
                         joint="curve")

    def dome(self, poly, peak: float, color=None, layers: int = 4,
             base: float | None = None, edge: float = 0.0) -> None:
        """Bosse arrondie : contours emboîtés du plus large au plus haut.

        ``edge`` creuse en plus une rainure le long du contour — c'est le trait
        de séparation que le sculpteur donne autour de chaque motif, et sans lui
        deux ornements voisins fusionnent en une seule masse molle."""
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
        # Sous une dizaine de pixels, les contours emboîtés qui arrondissent la
        # bosse tombent tous dans le même pixel après réduction : on garde le
        # dessin, on abandonne le modelé qui ne se verrait pas de toute façon.
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
        """Filet en relief : passes de plus en plus fines et hautes."""
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
        """Rainure creusée (nervure de feuille, gorge d'une moulure)."""
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
    """Nombre de sommets d'un contour, proportionné à sa taille réelle.

    Un motif qui couvre 15 px n'a pas besoin des 54 sommets qu'il lui faut à
    150 px : au-delà d'un sommet tous les ~2,5 px la tessellation ne se voit
    plus, elle ne coûte que du temps Python. Les cadres végétaux tracent
    plusieurs centaines de contours par bandeau, et ce nombre ne dépend pas de
    la résolution (l'espacement des motifs est une fraction de la largeur du
    bandeau) — sans ce plafond, une vignette de galerie paye exactement le même
    calcul de sommets qu'un export 6000 × 4000, ce qui rend les treize aperçus
    du dialogue de cadre inutilisables."""
    return max(minimum, min(full, int(span / 2.5)))


def _vine_leaf_polygon(cx: float, cy: float, size: float, angle: float) -> list:
    """Feuille de vigne : cinq lobes, sinus profonds, base cordiforme, bord denté.

    Une feuille lobée générique (rayon en cosinus, deux lobes et demi) se lit
    comme une tache ronde dès qu'elle est petite — ce sont les sinus entre lobes
    et la denture du bord qui la font reconnaître comme de la vigne. Le rayon
    est la somme de cinq bosses gaussiennes plutôt qu'un cosinus : chaque lobe
    garde ainsi sa propre largeur, comme sur la feuille réelle où le lobe
    terminal domine."""
    lobes = ((0.00, 1.00, 0.40), (0.72, 0.80, 0.32), (-0.72, 0.80, 0.32),
             (1.50, 0.54, 0.28), (-1.50, 0.54, 0.28))
    pts = []
    steps = _detail_steps(2.0 * math.pi * size, 108, 24)
    for i in range(steps):
        th = -math.pi + 2.0 * math.pi * i / steps
        r = 0.28
        for centre, amp, wide in lobes:
            r += amp * math.exp(-(_wrap_pi(th - centre) / wide) ** 2)
        r += 0.022 * math.sin(11.0 * th)                     # denture du bord
        r *= 1.0 - 0.60 * math.exp(-(_wrap_pi(th - math.pi) / 0.40) ** 2)
        x, y = _rotate(size * r * math.cos(th), size * r * math.sin(th), angle)
        pts.append((cx + x, cy + y))
    return pts


def _petal_polygon(cx: float, cy: float, length: float, width: float,
                   angle: float, notch: float = 0.0) -> list:
    """Pétale en goutte, attaché en ``(cx, cy)`` et pointé vers ``angle``.

    Étroit à la base, large aux deux tiers, arrondi sur la pointe — un pétale
    en ellipse (le raccourci employé jusqu'ici) donne une marguerite de
    pictogramme, et un pétale qui s'effile en pointe donne une étoile. D'où le
    produit de deux termes : ``s ** 0.55`` resserre la base, ``sin(πs) ** 0.35``
    maintient la largeur presque jusqu'au bout avant de la refermer d'un coup.
    ``notch`` échancre l'extrémité, ce qui distingue une rose ou un pommier
    d'une fleur à pétales lancéolés."""
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
    """Pétale en coupe : croissant entre deux arcs concentriques.

    C'est la forme réelle d'un pétale de rose vu de face — il *enveloppe* le
    cœur. Des pétales rayonnants en gouttes (``_petal_polygon``) donnent une
    marguerite quel que soit leur nombre ; seul l'enveloppement produit une
    rose. Le bord extérieur est légèrement ondulé, sinon le croissant se lit
    comme une pièce de ferronnerie."""
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
    """Feuille d'acanthe : nervure incurvée, lobes décroissants, pointe recourbée.

    C'est LE motif du cadre sculpté européen. Une feuille lobée symétrique se
    lit comme une pastille ; l'acanthe doit s'effiler et se recourber, sans
    quoi la frise ressemble à une rangée de trèfles."""
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
    """Feuille d'acanthe complète : masse en relief, nervure creusée, lobes marqués."""
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
    """Coquille d'angle : éventail de côtes sous un bourrelet, façon cartouche."""
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
    """Rose sculptée : trois couronnes de pétales échancrés, cœur en spirale.

    Les pétales enveloppent le cœur (``_cup_polygon``) au lieu d'en rayonner, et
    chaque couronne est peinte dans sa propre valeur — claire à l'extérieur,
    profonde au cœur. C'est ce dégradé qui fait la fleur : à couleur constante,
    des pétales même bien sculptés se noient dans un aplat rose dès que le
    cadre est vu en vignette, parce que leur relief est trop fin pour survivre
    à la réduction. La valeur, elle, survit toujours."""
    # (rayon, hauteur, nombre, décalage angulaire, épaisseur, teinte)
    rings = ((1.00, 0.55, 5, 0.00, 0.44, 0.34),
             (0.70, 0.80, 4, 0.72, 0.50, 0.06),
             (0.44, 1.00, 3, 1.55, 0.60, -0.26))
    for scale, lift, count, spin, thick, tone in rings:
        pr = radius * scale
        shade = (_mix(color, (255, 255, 255), tone) if tone >= 0.0
                 else _mix(color, (58, 14, 26), -tone))
        # Chevauchement juste suffisant pour fermer la couronne : au-delà, les
        # croissants fusionnent en un disque et la rose redevient une pastille.
        span = 2.0 * math.pi / count * 1.06
        for k in range(count):
            a = 2.0 * math.pi * k / count + spin + angle
            petal = shade if k % 2 else _mix(shade, (255, 255, 255), 0.12)
            cv.dome(_cup_polygon(cx, cy, pr, a, span, thick), peak * lift,
                    color=petal, layers=4, base=peak * lift * 0.30,
                    edge=peak * 0.95)
    # Cœur roulé : deux virgules imbriquées et sombres, jamais un disque clair —
    # un rond pâle au milieu des pétales se lit comme un œil.
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
    """Corolle ouverte : pétales en goutte creusés en cuillère, cœur d'étamines.

    Chaque pétale est dômé depuis une base basse vers une crête proche de la
    pointe — c'est ce qui creuse la corolle autour du cœur au lieu d'en faire
    une pastille bombée. Le désordre léger (``rng``) est indispensable : quinze
    fleurs rigoureusement identiques le long d'une moulure se lisent comme un
    tampon répété, pas comme un semis."""
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
    for k in range(5):                                  # étamines
        a = 2.0 * math.pi * k / 5.0 + twist * 0.5
        cv.disc(cx + math.cos(a) * radius * 0.13, cy + math.sin(a) * radius * 0.13,
                max(1.0, radius * 0.075), peak * 1.25, color=heart, layers=2)


def _carve_bud(cv: _Carver, cx: float, cy: float, size: float, angle: float,
               peak: float, color=None, leaf=None) -> None:
    """Bouton : ovale fermé, deux pétales enroulés, sépales ouverts au calice."""
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
    """Touffe de feuilles lancéolées en éventail — le remplissage des vides.

    Sans elle, un massif de fleurs laisse voir le fond entre chaque corolle et
    le cadre retombe en frise ponctuelle. C'est le motif le plus répété des
    cadres végétaux (plusieurs centaines par bandeau, quelle que soit la
    taille de rendu) : y ajouter une passe de plus — une nervure, une couche de
    dôme — se paie sur chacune des treize vignettes de la galerie."""
    for k in range(count):
        a = angle + (k - (count - 1) / 2.0) * spread
        poly = _petal_polygon(cx, cy, size, size * 0.26, a)
        # Feuilles de valeurs différentes dans une même touffe : uniformes,
        # elles fusionnent en une tache verte dès la réduction en vignette.
        tint = None if color is None else _mix(
            color, (18, 34, 14) if k % 2 else (206, 226, 168), 0.20)
        cv.dome(poly, peak, color=tint, layers=3, base=peak * 0.25,
                edge=peak * 0.7)


def _carve_tendril(cv: _Carver, cx: float, cy: float, dx: float, dy: float,
                   size: float, peak: float, sign: float = 1.0) -> None:
    """Vrille : le filament enroulé qui accroche la treille — il boucle les
    vides que le feuillage ne couvre pas."""
    pts = _volute_points(cx, cy, dx, dy, size, sign, turns=1.55, steps=34)
    cv.ridge(pts, max(1.0, size * 0.20), peak, layers=3)


def _carve_rope(cv: _Carver, w: float, h: float, b: float, frac: float,
                radius: float, peak: float) -> None:
    """Cordon torsadé le long d'une ligne du bandeau : brins obliques serrés."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, frac):
        length = math.hypot(bx - ax, by - ay)
        ang = math.atan2(ty, tx) + 0.62
        for d in _distribute(length, radius * 1.55, 0.0):
            px, py = ax + tx * d, ay + ty * d
            cv.dome(_ellipse_polygon(px, py, radius * 0.52, radius * 1.05, ang),
                    peak, layers=3, edge=peak * 0.7)


def _carve_grapes(cv: _Carver, cx: float, cy: float, size: float, angle: float,
                  peak: float, color=None) -> None:
    """Grappe : trois rangs de grains décroissants, dans l'axe ``angle``."""
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
    """Rang de perles : perle, bobine, perle… le long d'une ligne du bandeau."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, frac):
        length = math.hypot(bx - ax, by - ay)
        for i, d in enumerate(_distribute(length, radius * 2.6, b * 0.5)):
            px, py = ax + tx * d, ay + ty * d
            if i % 3 == 2:                      # bobine : deux disques serrés
                for sgn in (-1.0, 1.0):
                    cv.dome(_ellipse_polygon(px + tx * radius * 0.34 * sgn,
                                             py + ty * radius * 0.34 * sgn,
                                             radius * 0.30, radius * 0.86,
                                             math.atan2(ty, tx)), peak * 0.8, layers=3)
            else:
                cv.disc(px, py, radius, peak, layers=4)


def _carve_egg_and_dart(cv: _Carver, w: float, h: float, b: float, frac: float,
                        size: float, peak: float) -> None:
    """Oves et fers de lance — la frise classique par excellence."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, frac):
        length = math.hypot(bx - ax, by - ay)
        ang = math.atan2(ny, nx)
        for d in _distribute(length, size * 1.7, b * 0.9):
            px, py = ax + tx * d, ay + ty * d
            cv.dome(_ellipse_polygon(px, py, size * 0.60, size * 0.44, ang),
                    peak * 0.55, layers=3)                    # coquille
            cv.dome(_ellipse_polygon(px, py, size * 0.42, size * 0.30, ang),
                    peak, layers=4)                           # œuf
            cv.groove(_arc_points(px, py, size * 0.52, size * 0.38, ang,
                                  -math.pi * 0.85, math.pi * 0.85),
                      max(1.0, size * 0.09), peak * 0.7)
            mx, my = px + tx * size * 0.85, py + ty * size * 0.85
            cv.dome(_fleuron_polygon(mx, my, size * 0.44, ang), peak * 0.8, layers=3)


def _carve_meander(cv: _Carver, w: float, h: float, b: float, peak: float) -> None:
    """Grecque (méandre courant) : rail continu + spirale par cellule."""
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
    """Godrons : lobes bombés en travers de la moulure, serrés comme des cannelures."""
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
    """Filets et gorges continus (``(fraction, largeur, hauteur signée)``)."""
    for frac, width, level in specs:
        for (a, bb, _t, _n) in _band_sides(w, h, b, frac):
            if level >= 0:
                cv.ridge([a, bb], max(1.0, b * width), level)
            else:
                cv.groove([a, bb], max(1.0, b * width), -level)


# --------------------------------------------------------------- motifs par cadre

def _carve_baroque(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Cadre doré : frise d'acanthes sur la doucine, coquilles d'angle, perles."""
    for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, 0.60):
        length = math.hypot(bx - ax, by - ay)
        ang = math.atan2(ty, tx)
        for i, d in enumerate(_distribute(length, b * 1.30, b * 1.9)):
            px, py = ax + tx * d, ay + ty * d
            # Rinceau : une grande feuille couchée, une petite qui la relève.
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
    """Oves et perles : la moulure néo-classique, sobre et très travaillée."""
    _carve_egg_and_dart(cv, w, h, b, 0.60, b * 0.42, 0.34)
    _carve_bead_reel(cv, w, h, b, 0.86, b * 0.070, 0.28)
    _carve_bead_reel(cv, w, h, b, 0.14, b * 0.055, 0.22)
    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 0.60), _CORNER_DIRS):
        _carve_rosette(cv, cx, cy, b * 0.42, 0.34)
    _carve_fillets(cv, w, h, b, ((0.28, 0.04, -0.20), (0.75, 0.035, 0.20)))


def _carve_greek(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Grecque dorée sur fond sombre — moulure Empire."""
    _carve_meander(cv, w, h, b, 0.34)
    _carve_bead_reel(cv, w, h, b, 0.90, b * 0.065, 0.26)
    _carve_fillets(cv, w, h, b, ((0.08, 0.05, 0.22), (0.80, 0.04, 0.18),
                                 (0.86, 0.03, -0.18)))
    for (cx, cy), _d in zip(_band_corners(w, h, b, 0.55), _CORNER_DIRS):
        cv.disc(cx, cy, b * 0.22, 0.34)


def _carve_artdeco(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Art déco : la moulure est déjà en gradins (profil ``steps``) — le décor se
    limite à des barrettes rythmées et à un éventail d'angle. La sobriété fait
    le style : un ornement continu détruirait la lecture des gradins."""
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
        for k, size in enumerate((0.86, 0.56, 0.28)):    # bloc d'angle à gradins
            r = b * size * 0.5
            cv.ridge([(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r),
                      (cx - r, cy + r), (cx - r, cy - r)],
                     max(1.0, b * 0.075), 0.24 + 0.06 * k)


def _carve_wood(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Noyer sculpté : godrons, rosaces d'angle, gorges de moulure."""
    _carve_gadroons(cv, w, h, b, 0.58, 0.30)
    for (cx, cy), _d in zip(_band_corners(w, h, b, 0.55), _CORNER_DIRS):
        _carve_rosette(cv, cx, cy, b * 0.44, 0.32, petals=6)
    _carve_fillets(cv, w, h, b, ((0.12, 0.05, 0.20), (0.24, 0.04, -0.22),
                                 (0.84, 0.045, 0.18), (0.92, 0.03, -0.18)))


def _carve_metal(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Acier : arêtes vives, rivets forgés, gorge centrale."""
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


# Palettes des trois cadres végétaux — au niveau module parce que le bandeau et
# les motifs qui débordent sur la photo (cf. « débordements » plus bas) doivent
# être taillés dans les mêmes teintes : un ornement qui passe par-dessus l'image
# dans une couleur voisine se lit comme un autocollant, pas comme la suite de la
# sculpture.
_VINE_LIT = (188, 156, 96)        # bronze frotté, arêtes exposées
_VINE_SHADE = (118, 106, 62)      # bronze verdi des dessous
_VINE_DEEP = (86, 76, 46)         # fond de patine

_ROSE_HUES = ((196, 82, 104), (170, 54, 80), (222, 130, 146), (206, 100, 118))
_ROSE_HEART = (244, 214, 168)
_ROSE_LEAVES = ((62, 98, 52), (88, 124, 66), (74, 110, 58))

# Teintes rompues de blanc : sur porcelaine, une couleur pure vire au
# décalcomanie. Le blanc de la pâte doit rester perceptible dans chaque ton.
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
    """Feuille de vigne complète : masse en relief, nervures palmées, sinus.

    Les cinq nervures partent du pétiole vers les cinq lobes de
    ``_vine_leaf_polygon`` — elles doivent viser les mêmes directions, sinon le
    nervuré traverse les sinus et la feuille redevient une tache."""
    base_x = cx - math.cos(angle) * size * 0.62
    base_y = cy - math.sin(angle) * size * 0.62
    # Bombé faible et large plutôt que haut : une feuille est une plaque
    # légèrement gondolée, pas un coussin. Avec cinq contours emboîtés elle
    # gonflait en ballon lisse et se lisait comme un poisson — c'est la nervure
    # creusée, pas le volume, qui la fait reconnaître.
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
    """Treille de bronze : deux sarments entrelacés, feuillage jointif, vrilles
    et grappes — la moulure est couverte d'une arête à l'autre.

    Un cep unique bordé de petites feuilles (la version précédente) laissait les
    deux tiers de la moulure nus. La couverture ne vient pas d'ajouter des
    motifs, mais de les mettre à l'échelle du bandeau : une feuille dont
    l'envergure vaut la moitié de la largeur du cadre, portée alternativement de
    part et d'autre du sarment, couvre la moulure à elle seule — la grappe et la
    vrille du côté opposé ne font que boucher l'entre-deux. Le sarment est tracé
    sur ``_ring_samples`` (abscisse curviligne continue) pour que l'ondulation
    ne se brise pas dans les angles.

    Les motifs sont **teintés**, alors que le bandeau est un bronze uni : ce ne
    sont pas des couleurs mais des valeurs de la même patine (bronze clair,
    bronze verdi, bronze sombre). Sans elles, une treille aussi couverte devient
    illisible dès la vignette — le relief seul se moyenne en une bosselure
    uniforme, alors qu'un écart de valeur survit à n'importe quelle réduction."""
    lit, shade, deep = _VINE_LIT, _VINE_SHADE, _VINE_DEEP
    # Tapis de petites feuilles d'abord, sur toute la largeur : les grandes
    # feuilles seules laissent le bronze nu contre les deux arêtes, où le
    # sarment ne passe jamais. Les touffes visent EN TRAVERS du bandeau (± la
    # normale) — un éventail couvre un secteur, pas un disque, et orienté au
    # hasard il ouvre un quadrillage de vides en diagonale.
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
        # De l'autre côté du sarment, dans le creux laissé entre deux feuilles.
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
    """Massif de roses sur laque carmin : le bandeau entier est fleuri.

    Deux rangs en quinconce — les fleurs de l'un bouchent les intervalles de
    l'autre, et leurs corolles se recouvrent en travers de la moulure — plus des
    boutons et du feuillage dans les interstices, entre deux cordons torsadés
    qui bordent le massif. La composition tient à ce recouvrement : un rang
    unique, si dense soit-il, laisse toujours voir la laque de part et
    d'autre."""
    rose_hues, heart, leaves = _ROSE_HUES, _ROSE_HEART, _ROSE_LEAVES

    _carve_rope(cv, w, h, b, 0.06, b * 0.075, 0.26)
    _carve_rope(cv, w, h, b, 0.94, b * 0.062, 0.24)

    # Tapis de feuillage d'abord, fleurs ensuite : c'est ce fond continu qui
    # fait disparaître la laque entre les corolles. Trois rangs se recouvrant
    # en travers du bandeau, avec des orientations qui tournent d'un motif à
    # l'autre pour ne pas donner une texture peignée.
    for row, depth in enumerate((0.16, 0.46, 0.78)):
        for (ax, ay), (bx, by), (tx, ty), (nx, ny) in _band_sides(w, h, b, depth):
            length = math.hypot(bx - ax, by - ay)
            for i, d in enumerate(_distribute(length, b * 0.40, 0.0)):
                px, py = ax + tx * d, ay + ty * d
                # Les touffes visent EN TRAVERS du bandeau (± la normale) : un
                # éventail orienté au hasard laisse des coins de laque nus,
                # parce qu'il couvre un secteur et non un disque.
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
                # Décalage d'un demi-pas entre les deux rangs, plus un flottement :
                # sans lui les fleurs des deux rangs s'alignent par endroits et le
                # massif se lit comme une rangée de « 8 ».
                d += (shift + 0.22 * (float(rng.random()) - 0.5)) * spacing
                px, py = ax + tx * d, ay + ty * d
                if (i + 2 * row) % 5 == 4:
                    # Bouton : il aère le rang et évite l'effet de pochoir.
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
    """Porcelaine peinte : semis mille-fleurs couvrant tout le bandeau.

    Le principe est celui d'un décor de faïence : un fond de feuillage clair,
    des corolles de plusieurs espèces (à cinq, six et huit pétales, échancrés
    ou non) semées en quinconce, des bouquets de myosotis dans les vides, et
    deux filets d'or qui bordent le semis. La variété est ici le sujet — un
    semis d'une seule fleur répétée n'est pas un mille-fleurs, c'est un
    papier peint."""
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

    # (nombre de pétales, échancrure) — trois espèces qui alternent.
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
                    # Myosotis : trois corolles minuscules serrées, le liant du
                    # semis — sans elles les grandes fleurs restent des îlots.
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
    """Laque noire : deux filets d'or, rien d'autre — le reflet fait le reste."""
    gold = (206, 168, 88)
    for frac, width in ((0.12, 0.05), (0.88, 0.038)):
        for (a, bb, _t, _n) in _band_sides(w, h, b, frac):
            cv.ridge([a, bb], max(1.0, b * width), 0.22, color=gold)


# ------------------------------------------------------------- débordements
#
# Les trois cadres végétaux laissent quelques motifs PASSER PAR-DESSUS la photo.
# C'est la seconde (et dernière) dérogation à l'invariant « le cadre ne recouvre
# jamais un pixel de l'image » — cf. le second cadre de `plain`. Comme lui, elle
# est purement d'affichage : elle n'entre ni dans `border_px()` ni dans
# `content_box()`, et la géométrie des outils interactifs (recadrage, yeux
# rouges, visages, annotations) reste celle de la photo entière.
#
# Deux choses font que le débordement se lit comme une sculpture qui surplombe
# l'image plutôt que comme un autocollant :
#  - l'OMBRE PORTÉE sur la photo (`_SPILL_SHADOW`), décalée dans l'axe de
#    `_LIGHT` — c'est elle, bien plus que le motif, qui crée la profondeur ;
#  - le fait que chaque motif reste ACCROCHÉ au bandeau par une tige qui part
#    de sous l'arête : un ornement qui flotte au milieu de l'image ne ressemble
#    à rien.
# « Parfois » est essentiel au réalisme : un débordement à intervalle régulier
# redevient une frise. D'où un tirage par site (`_SPILL_SKIP`) et un espacement
# de plusieurs largeurs de bandeau.

_SPILL_SHADOW = 0.5          # opacité de l'ombre portée sur la photo
_SPILL_SKIP = 0.34           # proportion de sites laissés vides
_SPILL_SPACING = 3.6         # espacement des sites, en largeurs de bandeau


def _spill_sites(w: float, h: float, b: float, rng, inset: float) -> list:
    """Points d'accroche à cheval sur l'arête intérieure du bandeau.

    Retourne ``[(x, y, tx, ty, nx, ny)]`` — le point est décalé de ``inset``
    vers l'intérieur de la photo, si bien que le motif qu'on y taille repose
    pour moitié sur le cadre et pour moitié sur l'image."""
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
    """Les 4 angles, décalés en diagonale vers l'intérieur de la photo.

    Un angle qui déborde est le geste le plus caractéristique d'un cadre
    sculpté réel — c'est là que le bois ou la porcelaine se permet de mordre
    sur la toile."""
    out = []
    for (cx, cy), (dx, dy) in zip(_band_corners(w, h, b, 1.0), _CORNER_DIRS):
        out.append((cx + dx * inset, cy + dy * inset, dx, dy))
    return out


def _spill_stem(cv: _Carver, px: float, py: float, nx: float, ny: float,
                tx: float, ty: float, length: float, width: float,
                color, peak: float) -> None:
    """Tige qui rattache un motif débordant au bandeau, en trois points.

    Elle part de SOUS l'arête et reste assez courte pour disparaître sous le
    feuillage : une tige droite et longue traverse les ornements du bandeau et
    se lit comme une épingle plantée dans le cadre."""
    cv.ridge([(px - nx * length - tx * length * 0.28,
               py - ny * length - ty * length * 0.28),
              (px - nx * length * 0.45, py - ny * length * 0.45),
              (px, py)], max(1.0, width), peak, color=color, layers=3)


def _spill_vine(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Sarments qui franchissent la feuillure : feuille, vrille, parfois grappe."""
    mid = _mix(_VINE_LIT, _VINE_SHADE, 0.55)
    for px, py, tx, ty, nx, ny in _spill_sites(w, h, b, rng, b * 0.16):
        ang = math.atan2(ny, nx) + (float(rng.random()) - 0.5) * 0.7
        _spill_stem(cv, px, py, nx, ny, tx, ty, b * 0.55, b * 0.055,
                    _VINE_SHADE, 0.20)
        # Même tapis de feuillage que sur le bandeau, mais étalé DANS la
        # photo : sans lui le sarment se réduit à une feuille posée seule sur
        # l'image, qui se lit comme une broche épinglée.
        for k, (side, out, size, turn) in enumerate(
                ((-0.46, 0.26, 0.46, 1.05), (0.46, 0.30, 0.46, -1.05))):
            _carve_foliage(cv, px - ny * b * side + nx * b * out,
                           py + nx * b * side + ny * b * out,
                           b * size, ang + turn, 0.18,
                           color=_VINE_DEEP if k else mid,
                           count=3, spread=0.82)
        # Feuille franchement plate : isolée sur la photo, une feuille de vigne
        # bombée se lit comme une étoile de mer en pâte à modeler. Sous ce
        # relief-là ce sont le contour et les nervures qui la dessinent, pas le
        # volume — d'où aussi une taille modeste, la masse revenant au feuillage.
        _carve_grape_leaf(cv, px, py, b * (0.42 + 0.06 * float(rng.random())),
                          ang, 0.15, color=_VINE_LIT)
        # La grappe est ce qui pend naturellement d'une treille : c'est elle,
        # plus que la feuille, qui justifie le débordement — donc la plus grosse
        # masse de la touffe.
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
        # La vrille reste cantonnée aux angles : répétée le long de l'arête,
        # cette boucle brillante et fermée se lit comme un anneau de porte-clés.
        _carve_tendril(cv, cx + dx * b * 0.46, cy + dy * b * 0.46, -dy, dx,
                       b * 0.16, 0.18, 1.0)


def _spill_roses(cv: _Carver, w: float, h: float, b: float, rng) -> None:
    """Roses qui retombent sur l'image, portées par leur feuillage."""
    for i, (px, py, tx, ty, nx, ny) in enumerate(_spill_sites(w, h, b, rng, b * 0.12)):
        ang = math.atan2(ny, nx)
        _spill_stem(cv, px, py, nx, ny, tx, ty, b * 0.45, b * 0.055,
                    _ROSE_LEAVES[1], 0.18)
        # Le feuillage déborde plus loin que la fleur : c'est lui qui rattache
        # la touffe au bandeau et lui évite l'allure de pendentif accroché.
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
    """Corolles de porcelaine qui mordent sur l'image, en bouquets."""
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
        # Un myosotis de chaque côté : c'est le second bouton, jamais la fleur
        # seule, qui fait lire un bouquet posé plutôt qu'un motif détouré.
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
    """(carte de hauteurs signée, calque de couleur) des ornements d'un motif.

    Le calque est sculpté en suréchantillonnage (les polygones de ``ImageDraw``
    n'ont pas d'anticrénelage), réduit, puis légèrement flouté : c'est ce flou
    qui arrondit les arêtes des passes successives et transforme un empilement
    de contours en volume."""
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


def _albedo_map(np, kind: str, mat: dict, width, height, dist, side, border, rng, height_map):
    """Couleur diffuse du bandeau, avant éclairage."""
    if kind == "wood":
        arr = _fill_wood(np, width, height, dist, border, rng, (78, 44, 20), (172, 116, 62))
    elif kind == "metal":
        arr = _fill_brushed(np, width, height, side, rng, mat["albedo"])
    elif kind == "artdeco":
        arr = _fill_brushed(np, width, height, side, rng, mat["albedo"])
    elif kind == "greek":
        # Fond laqué sombre, or sur les reliefs : la grecque doit se détacher.
        dark = np.array((36, 30, 26), dtype="float32")
        gold = np.array(mat["albedo"], dtype="float32")
        k = np.clip((height_map - 0.62) * 5.0, 0.0, 1.0)[:, :, None]
        arr = dark * (1.0 - k) + gold * k
    else:
        arr = _fill_solid(np, width, height, mat["albedo"])

    if mat.get("gilding"):
        # Feuille d'or posée à la main : irrégulière, et usée sur les crêtes où
        # le bol rouge d'assiette affleure. Sans cette usure, une dorure calculée
        # reste une surface jaune uniforme, jamais un cadre doré.
        n = _smooth_noise(np, width, height, max(4, width // 40), max(4, height // 40), rng)
        n_fine = _smooth_noise(np, width, height, max(6, width // 8), max(6, height // 8), rng)
        wear = (np.clip((height_map - 0.74) * 3.4, 0.0, 1.0)
                * np.clip((n - 0.48) * 2.6, 0.0, 1.0))[:, :, None]
        arr = arr * (1.0 - wear) + np.array((148, 62, 44), dtype="float32") * wear
        arr = arr * (0.93 + 0.14 * n_fine)[:, :, None]
    return arr


def _paint_over(np, arr, cmap):
    """Compose le calque de couleur des motifs peints sur l'albédo."""
    col = np.asarray(cmap, dtype="float32")
    a = (col[:, :, 3:4] / 255.0)
    return arr * (1.0 - a) + col[:, :, :3] * a


# ------------------------------------------------------------------ bandeaux

# Profil de moulure et matériau de chaque motif décoratif.
_DECOR: dict[str, tuple[str, str, float]] = {
    # motif : (profil, matériau, amplitude des ornements)
    "baroque": ("ogee", "gold", 0.62),
    "pearl": ("ogee", "gold", 0.55),
    "greek": ("bevel", "gold", 0.50),
    "artdeco": ("steps", "silver", 0.50),
    "wood": ("cove", "walnut", 0.58),
    "metal": ("bevel", "silver", 0.45),
    # Les trois cadres végétaux couvrent tout le bandeau : profil « field »
    # (champ plat) pour que la sculpture soit seule à porter le relief.
    "vine": ("field", "bronze", 0.85),
    "roses": ("field", "carmine", 0.80),
    "flowers": ("field", "porcelain", 0.70),
    "gloss": ("round", "lacquer", 0.35),
}


def _band_array(np, kind: str, width: int, height: int, border: float, edit, rng):
    """Fond du cadre — tableau float32 (h, w, 3) en 0-255."""
    dist, side = _edge_distance(np, width, height)
    t = np.clip(dist / max(border, 1.0), 0.0, 1.0)

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
        hmap = _profile_height(np, t, "flat")
        if kind == "double":
            # Bandes concentriques : cadre extérieur | intervalle | cadre intérieur.
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
            # Le passe-partout est en retrait entre les deux cadres : c'est ce
            # décroché, et non un simple changement de couleur, qui donne au
            # cadre double sa profondeur.
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
        # Laque : traînées de lumière en diagonale, par-dessus l'éclairage du
        # relief (un vernis réfléchit la pièce, pas seulement la source).
        ys = np.linspace(0.0, 1.0, height, dtype="float32")[:, None]
        xs = np.linspace(0.0, 1.0, width, dtype="float32")[None, :]
        u = xs * 0.72 + ys * 0.68
        streak = (np.exp(-((u - 0.34) ** 2) / 0.0040) * 0.55
                  + np.exp(-((u - 0.47) ** 2) / 0.0008) * 0.45
                  + np.exp(-((u - 0.78) ** 2) / 0.0025) * 0.25)
        arr = arr + 255.0 * streak[:, :, None]
    return arr


def _spill_array(np, kind: str, width: int, height: int, border: float, rng):
    """Calque RGBA des motifs qui débordent sur la photo, ou ``None``.

    Même sculpture, même matière et même lumière que le bandeau — c'est la
    condition pour que le morceau qui passe par-dessus l'image se lise comme la
    suite du cadre. La différence est qu'il n'y a pas de moulure sous les
    ornements (le plan de base est plat) et qu'une silhouette est suivie en
    parallèle, pour savoir où le calque couvre la photo et où il la laisse
    intacte."""
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

    # Ombre portée : la silhouette floutée, décalée dans l'axe de la lumière
    # (_LIGHT vient du haut-gauche, l'ombre tombe donc en bas à droite).
    off = max(1, int(round(border * 0.10)))
    drop = np.roll(np.roll(cover, off, axis=0), off, axis=1)
    shadow = np.clip(_gauss(np, drop, max(1.0, border * 0.10)) * _SPILL_SHADOW,
                     0.0, 1.0) * (1.0 - cover)

    alpha = np.clip(cover + shadow, 0.0, 1.0)
    # L'ombre est du noir pur : en composition « out = rgb·a + photo·(1-a) »,
    # une teinte nulle avec l'alpha de l'ombre assombrit l'image sans la colorer.
    rgb = arr * (cover / np.maximum(alpha, 1e-6))[:, :, None]
    out = np.empty((height, width, 4), dtype="uint8")
    out[:, :, :3] = np.clip(rgb, 0, 255).astype("uint8")
    out[:, :, 3] = np.clip(alpha * 255.0, 0, 255).astype("uint8")
    return out


def _render_spill(kind: str, width: int, height: int, border: float):
    """Calque de débordement à la résolution de travail (``None`` si sans objet)."""
    import numpy as np

    rng = np.random.default_rng(20260805)   # rendu déterministe, comme le bandeau
    arr = _spill_array(np, kind, width, height, border, rng)
    if arr is None:
        return None
    return Image.fromarray(arr, mode="RGBA")


def _render_band(kind: str, width: int, height: int, border: float, edit) -> Image.Image:
    """Bandeau complet à la résolution de travail."""
    import numpy as np

    rng = np.random.default_rng(20260802)   # rendu déterministe d'une session à l'autre
    arr = _band_array(np, kind, width, height, border, edit, rng)
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), mode="RGB")


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

    # Débordements : collés APRÈS la photo, sinon elle les recouvrirait. Un
    # échec ici ne coûte que le débordement, jamais le cadre.
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

    Préféré à une feuille lobée pour la ferronnerie — celle-ci se lit
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
