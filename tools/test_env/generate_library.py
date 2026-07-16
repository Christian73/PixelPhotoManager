# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Génère une petite bibliothèque de photos synthétique et reproductible,
utilisée par les tests de non-régression (Layer 1 unitaire, Layer 3 e2e).

Volontairement procédural (PIL + numpy, seed fixe) plutôt que des fichiers
binaires committés dans le dépôt : `build_library()` régénère à l'identique
la bibliothèque à chaque appel avec le même `seed`.

Contient :
- 3 photos "témoin" sans doublon (dates EXIF différentes, pour la chronologie).
- Une paire de doublons exacts (copie d'octets).
- Une paire de doublons redimensionnés (couvre le Tier 1 — pHash).
- Une paire de doublons recadrés (crop pixel-exact, pas de redimensionnement —
  couvre le Tier 2 — ORB/RANSAC, cf. src/library/duplicate_detector.py).
- Un fichier JPEG corrompu (tronqué), pour src/library/file_repair.py.
- Une vidéo synthétique best-effort (peut être absente si l'encodeur manque
  sur la machine — les scénarios doivent gérer `video is None`).
"""
from __future__ import annotations

import argparse
import io
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import piexif
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

SEED = 20260715

_IMG_SIZE = (900, 700)  # (W, H) — assez grand pour ~300 keypoints ORB distincts
_N_SHAPES = 40          # formes dessinées par image — texture riche sur tout le canevas

# Crop pour la paire Tier 2 : recadrage central ~70% x 70% (~49% de l'aire),
# ratio d'aire ≈ 2.0 — largement sous _ORB_AREA_FACTOR=6.0 du détecteur.
# Recadrage pixel-exact (pas de redimensionnement) : les keypoints ORB de la
# zone commune restent identiques entre les deux fichiers, ce qui garantit un
# nombre d'inliers RANSAC très supérieur au seuil _ORB_MIN_INLIERS=40.
_CROP_BOX_RATIO = (0.15, 0.15, 0.85, 0.85)

# Paire "rafale" (même arrière-plan texturé partagé, sujet de premier plan
# différent) : rectangle opaque fixe noir/blanc couvrant ~30% de l'aire,
# peint au même endroit dans les deux variantes. Reproduit le faux positif
# du Tier 2 (cf. src/library/duplicate_detector.py::_ORB_MAX_MEAN_DIFF) :
# l'arrière-plan seul fournit largement plus d'inliers RANSAC que
# _ORB_MIN_INLIERS, alors que les photos ne se ressemblent pas réellement.
_BURST_BOX_RATIO = (0.28, 0.25, 0.73, 0.85)

_BASE_DATE = datetime(2026, 1, 1, 10, 0, 0)

# Retouche luminosité/contraste modérée : garde anti-régression pour la
# vérification post-hash du Tier 1 (cf. src/library/duplicate_detector.py::
# _HASH_PIXEL_MAX_DIFF) — une vraie retouche légitime doit rester groupée.
_EDIT_BRIGHTNESS = 1.25
_EDIT_CONTRAST = 1.15


@dataclass
class LibraryManifest:
    root: Path
    control_photos: list[Path]
    exact_duplicate_pair: tuple[Path, Path]
    resized_duplicate_pair: tuple[Path, Path]
    crop_duplicate_pair: tuple[Path, Path]
    burst_pair: tuple[Path, Path]
    edited_duplicate_pair: tuple[Path, Path]
    corrupted_file: Path
    dated_photos: dict[Path, datetime]
    video: Path | None = None
    images: list[Path] = field(default_factory=list)

    def rebased(self, new_root: Path) -> "LibraryManifest":
        """Retourne un manifeste équivalent dont tous les chemins pointent sous
        `new_root` au lieu de `self.root` (après un `shutil.copytree`)."""
        new_root = Path(new_root)

        def _r(p: Path) -> Path:
            return new_root / Path(p).relative_to(self.root)

        return replace(
            self,
            root=new_root,
            control_photos=[_r(p) for p in self.control_photos],
            exact_duplicate_pair=(_r(self.exact_duplicate_pair[0]), _r(self.exact_duplicate_pair[1])),
            resized_duplicate_pair=(_r(self.resized_duplicate_pair[0]), _r(self.resized_duplicate_pair[1])),
            crop_duplicate_pair=(_r(self.crop_duplicate_pair[0]), _r(self.crop_duplicate_pair[1])),
            burst_pair=(_r(self.burst_pair[0]), _r(self.burst_pair[1])),
            edited_duplicate_pair=(_r(self.edited_duplicate_pair[0]), _r(self.edited_duplicate_pair[1])),
            corrupted_file=_r(self.corrupted_file),
            dated_photos={_r(p): dt for p, dt in self.dated_photos.items()},
            video=_r(self.video) if self.video is not None else None,
            images=[_r(p) for p in self.images],
        )


def _make_base_image(rng: np.random.Generator, index: int) -> Image.Image:
    """Image texturée : bruit lissé + formes aléatoires distinctes sur tout le
    canevas, pour que n'importe quel recadrage conserve assez de features
    ORB (contrairement à une image à fond plat)."""
    w, h = _IMG_SIZE
    arr = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB").filter(ImageFilter.GaussianBlur(1.2))
    draw = ImageDraw.Draw(img)
    for _ in range(_N_SHAPES):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        x1 = x0 + int(rng.integers(20, 150))
        y1 = y0 + int(rng.integers(20, 150))
        color = tuple(int(c) for c in rng.integers(0, 256, size=3))
        kind = rng.integers(0, 3)
        if kind == 0:
            draw.ellipse([x0, y0, x1, y1], outline=color, width=3)
        elif kind == 1:
            draw.rectangle([x0, y0, x1, y1], outline=color, width=3)
        else:
            draw.line([x0, y0, x1, y1], fill=color, width=3)
    draw.text((10, 10), f"IMG-{index:03d}", fill=(255, 255, 255))
    return img


def _set_exif_date(jpeg_path: Path, dt: datetime) -> None:
    ts = dt.strftime("%Y:%m:%d %H:%M:%S")
    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = ts.encode()
    exif_dict["0th"][piexif.ImageIFD.DateTime] = ts.encode()
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, str(jpeg_path))


def _save_jpeg(img: Image.Image, path: Path, dt: datetime | None = None) -> None:
    img.convert("RGB").save(path, "JPEG", quality=92)
    if dt is not None:
        _set_exif_date(path, dt)


def _make_corrupted_jpeg(path: Path, rng: np.random.Generator) -> None:
    """Écrit un JPEG valide puis le tronque après un en-tête partiel : le
    fichier garde les octets magiques JPEG (0xFFD8) mais échoue à un décodage
    complet — cf. src/library/file_repair.py, qui cible exactement ce cas."""
    buf = io.BytesIO()
    img = Image.fromarray(rng.integers(0, 256, size=(400, 400, 3), dtype=np.uint8), mode="RGB")
    img.save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    truncated = data[: max(64, len(data) // 4)]
    path.write_bytes(truncated)


def _make_video(path: Path, rng: np.random.Generator) -> Path | None:
    """Best-effort : certaines machines n'ont pas d'encodeur mp4v disponible
    pour OpenCV — dans ce cas on renvoie None plutôt que d'échouer la
    génération de toute la bibliothèque."""
    try:
        import cv2
    except ImportError:
        return None
    try:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, 5.0, (320, 240))
        if not writer.isOpened():
            writer.release()
            return None
        for _ in range(15):
            frame = rng.integers(0, 256, size=(240, 320, 3), dtype=np.uint8)
            writer.write(frame)
        writer.release()
        return path if path.exists() and path.stat().st_size > 0 else None
    except Exception:
        return None


def build_library(dest_dir: Path, *, seed: int = SEED) -> LibraryManifest:
    """Construit la bibliothèque synthétique dans `dest_dir` (créé si absent,
    doit être vide/dédié — le contenu existant n'est pas nettoyé)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    dated_photos: dict[Path, datetime] = {}
    images: list[Path] = []

    def _dated(p: Path, offset_days: int) -> Path:
        dt = _BASE_DATE + timedelta(days=offset_days)
        dated_photos[p] = dt
        return p

    # -- Photos témoin (aucun doublon) --------------------------------------
    control_photos: list[Path] = []
    for i in range(3):
        p = dest_dir / f"control_{i + 1}.jpg"
        _save_jpeg(_make_base_image(rng, index=100 + i), p)
        _dated(p, offset_days=i)
        control_photos.append(p)
        images.append(p)

    # -- Paire doublons exacts (Tier 1 : distance de Hamming ≈ 0) -----------
    exact_a = dest_dir / "exact_a.jpg"
    exact_b = dest_dir / "exact_b.jpg"
    _save_jpeg(_make_base_image(rng, index=1), exact_a)
    _dated(exact_a, offset_days=10)
    shutil.copy2(exact_a, exact_b)
    _dated(exact_b, offset_days=10)
    images += [exact_a, exact_b]

    # -- Paire doublons redimensionnés (Tier 1 : pHash robuste à l'échelle) --
    resized_a = dest_dir / "resized_a.jpg"
    resized_b = dest_dir / "resized_b.jpg"
    base_resized = _make_base_image(rng, index=2)
    _save_jpeg(base_resized, resized_a)
    _dated(resized_a, offset_days=20)
    scaled = base_resized.resize(
        (int(_IMG_SIZE[0] * 0.6), int(_IMG_SIZE[1] * 0.6)), Image.LANCZOS
    )
    _save_jpeg(scaled, resized_b)
    _dated(resized_b, offset_days=20)
    images += [resized_a, resized_b]

    # -- Paire doublons recadrés (Tier 2 : ORB + RANSAC) --------------------
    crop_a = dest_dir / "crop_a.jpg"
    crop_b = dest_dir / "crop_b.jpg"
    base_crop = _make_base_image(rng, index=3)
    _save_jpeg(base_crop, crop_a)
    _dated(crop_a, offset_days=30)
    w, h = base_crop.size
    l, t, r, b = _CROP_BOX_RATIO
    cropped = base_crop.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
    _save_jpeg(cropped, crop_b)
    _dated(crop_b, offset_days=30)
    images += [crop_a, crop_b]

    # -- Paire "rafale" (arrière-plan partagé, sujet différent — ne doit --
    # -- PAS être groupée, cf. _ORB_MAX_MEAN_DIFF) ---------------------------
    burst_a_path = dest_dir / "burst_a.jpg"
    burst_b_path = dest_dir / "burst_b.jpg"
    base_burst = _make_base_image(rng, index=4)
    bw, bh = base_burst.size
    bl, bt, br, bb = _BURST_BOX_RATIO
    box = (int(bl * bw), int(bt * bh), int(br * bw), int(bb * bh))
    burst_a = base_burst.copy()
    ImageDraw.Draw(burst_a).rectangle(box, fill=(0, 0, 0))
    burst_b = base_burst.copy()
    ImageDraw.Draw(burst_b).rectangle(box, fill=(255, 255, 255))
    _save_jpeg(burst_a, burst_a_path)
    _dated(burst_a_path, offset_days=40)
    _save_jpeg(burst_b, burst_b_path)
    _dated(burst_b_path, offset_days=40)
    images += [burst_a_path, burst_b_path]

    # -- Paire retouchée (luminosité + contraste — doit rester groupée --
    # -- malgré la vérification post-hash du Tier 1, cf. _HASH_PIXEL_MAX_DIFF) -
    edited_a = dest_dir / "edited_a.jpg"
    edited_b = dest_dir / "edited_b.jpg"
    base_edited = _make_base_image(rng, index=5)
    _save_jpeg(base_edited, edited_a)
    _dated(edited_a, offset_days=50)
    retouched = ImageEnhance.Contrast(
        ImageEnhance.Brightness(base_edited).enhance(_EDIT_BRIGHTNESS)
    ).enhance(_EDIT_CONTRAST)
    _save_jpeg(retouched, edited_b)
    _dated(edited_b, offset_days=50)
    images += [edited_a, edited_b]

    # -- Fichier corrompu ----------------------------------------------------
    corrupted = dest_dir / "corrupted.jpg"
    _make_corrupted_jpeg(corrupted, rng)

    # -- Vidéo (best-effort) --------------------------------------------------
    video = _make_video(dest_dir / "clip.mp4", rng)

    return LibraryManifest(
        root=dest_dir,
        control_photos=control_photos,
        exact_duplicate_pair=(exact_a, exact_b),
        resized_duplicate_pair=(resized_a, resized_b),
        crop_duplicate_pair=(crop_a, crop_b),
        burst_pair=(burst_a_path, burst_b_path),
        edited_duplicate_pair=(edited_a, edited_b),
        corrupted_file=corrupted,
        dated_photos=dated_photos,
        video=video,
        images=images,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest", type=Path,
        default=Path(tempfile.gettempdir()) / "ppm_test_library",
        help="Dossier de destination (par défaut : %(default)s)",
    )
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    manifest = build_library(args.dest, seed=args.seed)
    print(f"Bibliothèque générée dans {manifest.root}")
    print(f"  {len(manifest.images)} photo(s), fichier corrompu : {manifest.corrupted_file.name}")
    print(f"  vidéo : {manifest.video.name if manifest.video else '(non générée sur cette machine)'}")


if __name__ == "__main__":
    _main()
