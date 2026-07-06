# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Genere les bitmaps visuels requis par WiX pour l'installeur MSI.
  banner.bmp  : 493 x 58  px — bande superieure des ecrans interieurs
  dialog.bmp  : 493 x 312 px — fond de l'ecran de bienvenue

Layout de dialog.bmp :
  Gauche [0..SPLIT[   : panneau sombre avec icone + titre
  Droite [SPLIT..493] : fond blanc — le texte WiX (noir, transparent) y est lisible
  WiX WelcomeDlg place son texte a partir de X=135 DLU / 370 DLU total
  => 135/370 * 493 ~ 180 px => SPLIT = 170 px laisse une marge confortable.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT  = Path(__file__).parent
ROOT = OUT.parent

# Palette
BG      = (30, 40, 60)       # bleu ardoise fonce
ACCENT  = (80, 130, 200)     # bleu clair
WHITE   = (255, 255, 255)
LIGHT   = (200, 220, 245)
DIM     = (140, 165, 200)
RIGHT   = (248, 249, 252)    # blanc casse pour la zone de texte WiX

SPLIT   = 170                # limite gauche / droite en pixels

# ── Polices ──────────────────────────────────────────────────────────────────
def _load_fonts():
    fonts_dir = Path("C:/Windows/Fonts")
    try:
        f_big = ImageFont.truetype(str(fonts_dir / "segoeuib.ttf"), 26)
        f_med = ImageFont.truetype(str(fonts_dir / "segoeui.ttf"),  14)
        f_sub = ImageFont.truetype(str(fonts_dir / "segoeui.ttf"),  12)
        f_ban = ImageFont.truetype(str(fonts_dir / "segoeui.ttf"),  20)
        f_ban_sub = ImageFont.truetype(str(fonts_dir / "segoeui.ttf"), 13)
    except OSError:
        default = ImageFont.load_default()
        f_big = f_med = f_sub = f_ban = f_ban_sub = default
    return f_big, f_med, f_sub, f_ban, f_ban_sub


def _gradient_h(img: Image.Image, left: tuple, right: tuple,
                x0: int = 0, x1: int | None = None) -> None:
    """Degrade horizontal sur la plage [x0, x1[."""
    w, h = img.size
    if x1 is None:
        x1 = w
    draw = ImageDraw.Draw(img)
    span = x1 - x0
    for x in range(x0, x1):
        t = (x - x0) / max(span - 1, 1)
        r = int(left[0] + (right[0] - left[0]) * t)
        g = int(left[1] + (right[1] - left[1]) * t)
        b = int(left[2] + (right[2] - left[2]) * t)
        draw.line([(x, 0), (x, h)], fill=(r, g, b))


f_big, f_med, f_sub, f_ban, f_ban_sub = _load_fonts()

# ── banner.bmp (493 x 58) ────────────────────────────────────────────────────
# Contrainte WiX (WelcomeDlg / LicenseAgreementDlg / InstallDirDlg…) :
#   - Titre dialogue  : X=15..286 px (bitmap), Y=8..28 px  → texte noir
#   - Description     : X=33..406 px (bitmap), Y=30..50 px → texte noir
# Solution : dégradé clair à gauche (texte WiX noir lisible) → sombre à droite
#   (notre texte blanc + icône, hors zone WiX : X>286, Y<30)
BANNER_LEFT  = (185, 210, 240)   # bleu très clair  – texte WiX noir lisible
BANNER_RIGHT = (22, 42, 90)      # bleu nuit        – texte blanc + icône

banner = Image.new("RGB", (493, 58))
_gradient_h(banner, BANNER_LEFT, BANNER_RIGHT)
draw = ImageDraw.Draw(banner)

# Icône application – coin droit, Y=7 px, 44×44 px
_banner_icon_y = 7
_banner_icon_x = 441
if (ROOT / "assets" / "app_icon.ico").exists():
    try:
        _ico = Image.open(str(ROOT / "assets" / "app_icon.ico")).convert("RGBA")
        _ico = _ico.resize((44, 44), Image.LANCZOS)
        banner.paste(_ico, (_banner_icon_x, _banner_icon_y), _ico)
    except Exception as _e:
        print(f"  Avertissement icone banner : {_e}")

# "Pixel Photo Manager" – juste à gauche de l'icône, hors zone titre WiX (X>286)
# Y=14 : au-dessus de la zone description WiX (Y=30+), texte ~14 px de haut
try:
    f_name = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 13)
except OSError:
    f_name = ImageFont.load_default()
draw.text((297, 18), "Pixel Photo Manager", font=f_name, fill=WHITE)

# Ligne d'accent en bas
draw.line([(0, 56), (493, 56)], fill=(80, 130, 200), width=2)
banner.save(OUT / "banner.bmp")
print("banner.bmp cree")

# ── dialog.bmp (493 x 312) ───────────────────────────────────────────────────
dialog = Image.new("RGB", (493, 312), RIGHT)       # fond blanc pour la zone WiX

# Panneau gauche : degrade sombre
_gradient_h(dialog, BG, (20, 30, 55), x0=0, x1=SPLIT)

# Ligne de separation
draw = ImageDraw.Draw(dialog)
draw.line([(SPLIT, 0), (SPLIT, 312)], fill=ACCENT, width=2)

# ── Icone de l'application ──────────────────────────────────────────────────
icon_path = ROOT / "assets" / "app_icon.ico"
icon_bottom_y = 20
if icon_path.exists():
    try:
        ico = Image.open(str(icon_path)).convert("RGBA")
        ico = ico.resize((72, 72), Image.LANCZOS)
        ix = (SPLIT - 72) // 2
        iy = 20
        dialog.paste(ico, (ix, iy), ico)
        icon_bottom_y = iy + 72 + 10
    except Exception as exc:
        print(f"  Avertissement icone : {exc}")
        icon_bottom_y = 30

# ── Texte de presentation (panneau gauche) ───────────────────────────────────
ty = icon_bottom_y
draw.text((12, ty),      "Pixel Photo", font=f_big, fill=WHITE)
draw.text((12, ty + 32), "Manager",     font=f_big, fill=LIGHT)
draw.text((12, ty + 66), "v 1.0",       font=f_sub, fill=DIM)
draw.line([(10, 270), (SPLIT - 14, 270)], fill=ACCENT, width=2)

dialog.save(OUT / "dialog.bmp")
print("dialog.bmp cree")
