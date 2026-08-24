# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Generates the visual bitmaps required by WiX for the MSI installer.
  banner.bmp  : 493 x 58  px — top band of the inner screens
  dialog.bmp  : 493 x 312 px — background of the welcome screen

Layout of dialog.bmp:
  Left  [0..SPLIT[    : dark panel with the icon + title
  Right [SPLIT..493]  : white background — the WiX text (black, transparent) is readable there
  WiX WelcomeDlg places its text from X=135 DLU / 370 DLU total
  => 135/370 * 493 ~ 180 px => SPLIT = 170 px leaves a comfortable margin.
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT  = Path(__file__).parent
ROOT = OUT.parent


def _version() -> str:
    """Version painted into dialog.bmp.

    It used to be the literal "v 1.0", which the installer kept displaying long
    after the product had moved on — build_msi.ps1 only regenerated the bitmaps
    when they were missing, so the constant survived every version bump. It is
    now taken from the build (argv) or, failing that, from VERSION at the root of
    the repository, the single source shared with the MSI itself.
    """
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    version_file = ROOT / "VERSION"
    if version_file.exists():
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "dev"


VERSION = _version()

# Palette
BG      = (30, 40, 60)       # dark slate blue
ACCENT  = (80, 130, 200)     # light blue
WHITE   = (255, 255, 255)
LIGHT   = (200, 220, 245)
DIM     = (140, 165, 200)
RIGHT   = (248, 249, 252)    # off-white for the WiX text area

SPLIT   = 170                # left / right boundary in pixels

# ── Fonts ────────────────────────────────────────────────────────────────────
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
    """Horizontal gradient over the range [x0, x1[."""
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
# WiX constraint (WelcomeDlg / LicenseAgreementDlg / InstallDirDlg…):
#   - Dialog title    : X=15..286 px (bitmap), Y=8..28 px  → black text
#   - Description     : X=33..406 px (bitmap), Y=30..50 px → black text
# Solution: light gradient on the left (black WiX text readable) → dark on the right
#   (our white text + icon, outside the WiX area: X>286, Y<30)
BANNER_LEFT  = (185, 210, 240)   # very light blue – black WiX text readable
BANNER_RIGHT = (22, 42, 90)      # midnight blue   – white text + icon

banner = Image.new("RGB", (493, 58))
_gradient_h(banner, BANNER_LEFT, BANNER_RIGHT)
draw = ImageDraw.Draw(banner)

# Application icon – right corner, Y=7 px, 44×44 px
_banner_icon_y = 7
_banner_icon_x = 441
if (ROOT / "assets" / "app_icon.ico").exists():
    try:
        _ico = Image.open(str(ROOT / "assets" / "app_icon.ico")).convert("RGBA")
        _ico = _ico.resize((44, 44), Image.LANCZOS)
        banner.paste(_ico, (_banner_icon_x, _banner_icon_y), _ico)
    except Exception as _e:
        print(f"  Avertissement icone banner : {_e}")

# "Pixel Photo Manager" – just left of the icon, outside the WiX title area (X>286)
# Y=14: above the WiX description area (Y=30+), text ~14 px high
try:
    f_name = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 13)
except OSError:
    f_name = ImageFont.load_default()
draw.text((297, 18), "Pixel Photo Manager", font=f_name, fill=WHITE)

# Accent line at the bottom
draw.line([(0, 56), (493, 56)], fill=(80, 130, 200), width=2)
banner.save(OUT / "banner.bmp")
print("banner.bmp cree")

# ── dialog.bmp (493 x 312) ───────────────────────────────────────────────────
dialog = Image.new("RGB", (493, 312), RIGHT)       # white background for the WiX area

# Left panel: dark gradient
_gradient_h(dialog, BG, (20, 30, 55), x0=0, x1=SPLIT)

# Separating line
draw = ImageDraw.Draw(dialog)
draw.line([(SPLIT, 0), (SPLIT, 312)], fill=ACCENT, width=2)

# ── Application icon ─────────────────────────────────────────────────────────
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

# ── Presentation text (left panel) ───────────────────────────────────────────
ty = icon_bottom_y
draw.text((12, ty),      "Pixel Photo", font=f_big, fill=WHITE)
draw.text((12, ty + 32), "Manager",     font=f_big, fill=LIGHT)
draw.text((12, ty + 66), f"v {VERSION}", font=f_sub, fill=DIM)
draw.line([(10, 270), (SPLIT - 14, 270)], fill=ACCENT, width=2)

dialog.save(OUT / "dialog.bmp")
print(f"dialog.bmp cree (version affichee : {VERSION})")
