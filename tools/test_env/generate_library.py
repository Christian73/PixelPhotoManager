# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Generates a small synthetic and reproducible photo library,
used by the non-regression tests (Layer 1 unit, Layer 3 e2e).

Deliberately procedural (PIL + numpy, a fixed seed) rather than binary
files committed into the repository: `build_library()` regenerates the
library identically on every call with the same `seed`.

Contains:
- 3 "witness" photos with no duplicate (different EXIF dates, for the timeline).
- A pair of exact duplicates (a byte copy).
- A pair of resized duplicates (covers Tier 1 -- pHash).
- A pair of cropped duplicates (a pixel-exact crop, no resizing --
  covers Tier 2 -- ORB/RANSAC, cf. src/library/duplicate_detector.py).
- A corrupted (truncated) JPEG file, for src/library/file_repair.py.
- A best-effort synthetic video (which may be missing if the encoder is absent
  on the machine -- the scenarios must handle `video is None`).
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

_IMG_SIZE = (900, 700)  # (W, H) -- large enough for ~300 distinct ORB keypoints
_N_SHAPES = 40          # shapes drawn per image -- a rich texture over the whole canvas

# Crop for the Tier 2 pair: a central crop of ~70% x 70% (~49% of the area),
# area ratio ~= 2.0 -- well under the detector's _ORB_AREA_FACTOR=6.0.
# A pixel-exact crop (no resizing): the ORB keypoints of the
# common area stay identical between the two files, which guarantees a
# number of RANSAC inliers far above the _ORB_MIN_INLIERS=40 threshold.
_CROP_BOX_RATIO = (0.15, 0.15, 0.85, 0.85)

# A "burst" pair (the same shared textured background, a different
# foreground subject): a fixed opaque black/white rectangle covering ~30% of the
# area, painted at the same place in both variants. Reproduces the Tier 2 false
# positive (cf. src/library/duplicate_detector.py::_ORB_MAX_MEAN_DIFF):
# the background alone provides far more RANSAC inliers than
# _ORB_MIN_INLIERS, while the photos do not really look alike.
_BURST_BOX_RATIO = (0.28, 0.25, 0.73, 0.85)

_BASE_DATE = datetime(2026, 1, 1, 10, 0, 0)

# A moderate brightness/contrast edit: an anti-regression guard for the
# post-hash check of Tier 1 (cf. src/library/duplicate_detector.py::
# _HASH_PIXEL_MAX_DIFF) -- a genuinely legitimate edit must stay grouped.
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
        """Returns an equivalent manifest whose paths all point under
        `new_root` instead of `self.root` (after a `shutil.copytree`)."""
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
    """A textured image: smoothed noise + distinct random shapes over the whole
    canvas, so that any crop keeps enough ORB features
    (unlike an image with a flat background)."""
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
    """Writes a valid JPEG then truncates it after a partial header: the
    file keeps the JPEG magic bytes (0xFFD8) but fails a complete
    decoding -- cf. src/library/file_repair.py, which targets exactly that case."""
    buf = io.BytesIO()
    img = Image.fromarray(rng.integers(0, 256, size=(400, 400, 3), dtype=np.uint8), mode="RGB")
    img.save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    truncated = data[: max(64, len(data) // 4)]
    path.write_bytes(truncated)


def _make_video(path: Path, rng: np.random.Generator) -> Path | None:
    """Best-effort: some machines have no mp4v encoder available
    for OpenCV -- in that case None is returned rather than failing the
    generation of the whole library."""
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
    """Builds the synthetic library in `dest_dir` (created if absent,
    must be empty/dedicated -- the existing content is not cleaned up)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    dated_photos: dict[Path, datetime] = {}
    images: list[Path] = []

    def _dated(p: Path, offset_days: int) -> Path:
        dt = _BASE_DATE + timedelta(days=offset_days)
        dated_photos[p] = dt
        return p

    # -- Witness photos (no duplicate) ---------------------------------------
    control_photos: list[Path] = []
    for i in range(3):
        p = dest_dir / f"control_{i + 1}.jpg"
        _save_jpeg(_make_base_image(rng, index=100 + i), p)
        _dated(p, offset_days=i)
        control_photos.append(p)
        images.append(p)

    # -- Exact duplicate pair (Tier 1: a Hamming distance of ~0) -------------
    exact_a = dest_dir / "exact_a.jpg"
    exact_b = dest_dir / "exact_b.jpg"
    _save_jpeg(_make_base_image(rng, index=1), exact_a)
    _dated(exact_a, offset_days=10)
    shutil.copy2(exact_a, exact_b)
    _dated(exact_b, offset_days=10)
    images += [exact_a, exact_b]

    # -- Resized duplicate pair (Tier 1: pHash is robust to scale) -----------
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

    # -- Cropped duplicate pair (Tier 2: ORB + RANSAC) -----------------------
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

    # -- "Burst" pair (a shared background, a different subject -- must ------
    # -- NOT be grouped, cf. _ORB_MAX_MEAN_DIFF) -----------------------------
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

    # -- Edited pair (brightness + contrast -- must stay grouped -------------
    # -- despite the post-hash check of Tier 1, cf. _HASH_PIXEL_MAX_DIFF) ----
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

    # -- Corrupted file ------------------------------------------------------
    corrupted = dest_dir / "corrupted.jpg"
    _make_corrupted_jpeg(corrupted, rng)

    # -- Video (best-effort) -------------------------------------------------
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
