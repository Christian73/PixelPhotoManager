"""
Genere assets/app_icon.ico depuis l'image source PNG.
- Separation fond/sujet par GrabCut (OpenCV)
- Produit un .ico multi-resolution : 16, 32, 48, 64, 128, 256 px (PNG embarques)
"""

import sys
import io
import struct
from pathlib import Path
import numpy as np
import cv2
from PIL import Image

SRC = Path("assets/ChatGPT Image Jun 6, 2026, 04_29_56 PM.png")
DST = Path("assets/app_icon.ico")
SIZES = [256, 128, 64, 48, 32, 16]

# Travaille sur une image reduite pour la vitesse
WORK_SIZE = 640


def grabcut_extract(src_path: Path) -> Image.Image:
    """
    Extrait le sujet (cube) via GrabCut, retourne une image RGBA.
    """
    # Charger en BGR (OpenCV)
    img_bgr = cv2.imread(str(src_path))
    orig_h, orig_w = img_bgr.shape[:2]

    # Reduire pour la vitesse
    scale = WORK_SIZE / max(orig_h, orig_w)
    w = int(orig_w * scale)
    h = int(orig_h * scale)
    small = cv2.resize(img_bgr, (w, h), interpolation=cv2.INTER_AREA)

    print(f"  Resolution de travail : {w}x{h}")

    # Rectangle d'initialisation : on exclut 5 % de marge tout autour
    margin_x = int(w * 0.05)
    margin_y = int(h * 0.05)
    rect = (margin_x, margin_y, w - 2 * margin_x, h - 2 * margin_y)

    mask = np.zeros((h, w), dtype=np.uint8)
    bgd_model = np.zeros((1, 65), dtype=np.float64)
    fgd_model = np.zeros((1, 65), dtype=np.float64)

    print("  GrabCut en cours (5 iterations) ...")
    cv2.grabCut(small, mask, rect, bgd_model, fgd_model, 5, cv2.GC_INIT_WITH_RECT)

    # Affiner : les coins sont du fond pur, le centre est premier plan
    # Marquer manuellement les coins comme fond definitif
    corner = 20
    mask[:corner, :corner] = cv2.GC_BGD
    mask[:corner, -corner:] = cv2.GC_BGD
    mask[-corner:, :corner] = cv2.GC_BGD
    mask[-corner:, -corner:] = cv2.GC_BGD
    # Le centre de l'image est premier plan definitif
    cx, cy = w // 2, h // 2
    center_r = int(min(w, h) * 0.25)
    mask[cy - center_r:cy + center_r, cx - center_r:cx + center_r] = cv2.GC_FGD

    print("  GrabCut affinement (3 iterations) ...")
    cv2.grabCut(small, mask, rect, bgd_model, fgd_model, 3, cv2.GC_INIT_WITH_MASK)

    # mask : 0=BGD, 1=FGD, 2=PR_BGD, 3=PR_FGD
    fg_mask_small = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)

    # Affiner le masque : fermeture morphologique pour boucher les trous
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    fg_mask_small = cv2.morphologyEx(fg_mask_small, cv2.MORPH_CLOSE, kernel, iterations=2)
    fg_mask_small = cv2.morphologyEx(fg_mask_small, cv2.MORPH_OPEN, kernel, iterations=1)

    # Flouter legerement les bords (anti-aliasing)
    fg_mask_small = cv2.GaussianBlur(fg_mask_small, (5, 5), 1.5)

    # Remonter au format original
    fg_mask = cv2.resize(fg_mask_small, (orig_w, orig_h), interpolation=cv2.INTER_LINEAR)

    # Construire l'image RGBA
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    rgba = np.dstack([img_rgb, fg_mask])
    return Image.fromarray(rgba.astype(np.uint8), "RGBA")


def crop_to_content(img: Image.Image, padding: int = 12) -> Image.Image:
    """Recadre sur le contenu non-transparent (carre centre)."""
    bbox = img.getbbox()
    if bbox is None:
        return img
    l, t, r, b = bbox
    w, h = img.size
    l = max(0, l - padding)
    t = max(0, t - padding)
    r = min(w, r + padding)
    b = min(h, b + padding)
    side = max(r - l, b - t)
    cx = (l + r) // 2
    cy = (t + b) // 2
    half = side // 2
    l2 = max(0, cx - half)
    t2 = max(0, cy - half)
    r2 = min(w, l2 + side)
    b2 = min(h, t2 + side)
    return img.crop((l2, t2, r2, b2))


def build_ico(images: list, path: Path) -> None:
    """Construit un .ico multi-taille (format Vista+, PNG embarques, RGBA)."""
    count = len(images)
    png_chunks = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_chunks.append(buf.getvalue())

    header_size = 6 + count * 16
    header = struct.pack("<HHH", 0, 1, count)
    directory = b""
    offset = header_size
    for img, data in zip(images, png_chunks):
        w, h = img.size
        wb = w if w < 256 else 0
        hb = h if h < 256 else 0
        directory += struct.pack("<BBBBHHII", wb, hb, 0, 0, 1, 32, len(data), offset)
        offset += len(data)

    with open(path, "wb") as f:
        f.write(header)
        f.write(directory)
        for data in png_chunks:
            f.write(data)


def main():
    if not SRC.exists():
        sys.exit(f"Source introuvable : {SRC}")

    print(f"Chargement de {SRC} ...")
    img_orig = Image.open(SRC)
    print(f"  {img_orig.size}, mode : {img_orig.mode}")

    print("Extraction du sujet par GrabCut ...")
    transparent = grabcut_extract(SRC)

    print("Recadrage sur le contenu ...")
    cropped = crop_to_content(transparent, padding=14)
    print(f"  Taille recadree : {cropped.size}")

    print("Generation des tailles :")
    frames = []
    for size in SIZES:
        frame = cropped.resize((size, size), Image.LANCZOS)
        frames.append(frame)
        print(f"  {size}x{size}")

    DST.parent.mkdir(parents=True, exist_ok=True)
    build_ico(frames, DST)
    print(f"\nIcone creee : {DST.resolve()}")
    print(f"Taille fichier : {DST.stat().st_size:,} octets")


if __name__ == "__main__":
    main()
