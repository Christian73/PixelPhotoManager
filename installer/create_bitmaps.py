# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Genere les bitmaps visuels requis par WiX pour l'installeur MSI.
  banner.bmp  : 493 x 58  px — bande superieure de chaque ecran
  dialog.bmp  : 493 x 312 px — fond de l'ecran de bienvenue
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent

# Palette
BG       = (30, 40, 60)      # bleu ardoise fonce
ACCENT   = (80, 130, 200)    # bleu clair
WHITE    = (255, 255, 255)
LIGHT    = (200, 220, 245)


def _gradient(img: Image.Image, left: tuple, right: tuple) -> None:
    w, h = img.size
    draw = ImageDraw.Draw(img)
    for x in range(w):
        r = int(left[0] + (right[0] - left[0]) * x / w)
        g = int(left[1] + (right[1] - left[1]) * x / w)
        b = int(left[2] + (right[2] - left[2]) * x / w)
        draw.line([(x, 0), (x, h)], fill=(r, g, b))


# ── banner.bmp (493 x 58) ────────────────────────────────────────────────────
banner = Image.new("RGB", (493, 58))
_gradient(banner, BG, ACCENT)
draw = ImageDraw.Draw(banner)
try:
    font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 20)
    font_sub = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 13)
except OSError:
    font = font_sub = ImageFont.load_default()

draw.text((16, 10), "Pixel Photo Manager", font=font, fill=WHITE)
draw.text((16, 36), "Gestionnaire de photos", font=font_sub, fill=LIGHT)
draw.line([(0, 56), (493, 56)], fill=ACCENT, width=2)
banner.save(OUT / "banner.bmp")
print("banner.bmp cree")

# ── dialog.bmp (493 x 312) ───────────────────────────────────────────────────
dialog = Image.new("RGB", (493, 312))
_gradient(dialog, BG, (20, 30, 50))
draw = ImageDraw.Draw(dialog)
try:
    font_big = ImageFont.truetype("C:/Windows/Fonts/segoeuil.ttf", 36)
    font_med = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 15)
except OSError:
    font_big = font_med = ImageFont.load_default()

draw.text((32, 60),  "Pixel Photo",  font=font_big, fill=WHITE)
draw.text((32, 102), "Manager",      font=font_big, fill=LIGHT)
draw.text((32, 155), "Gestionnaire de photos desktop Windows", font=font_med, fill=LIGHT)
draw.text((32, 178), "Version 1.0", font=font_med, fill=(150, 170, 200))
draw.line([(0, 270), (180, 270)], fill=ACCENT, width=2)
dialog.save(OUT / "dialog.bmp")
print("dialog.bmp cree")
