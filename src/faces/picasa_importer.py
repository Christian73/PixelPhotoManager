# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Import face and person data from Picasa (picasa.ini + contacts.xml).

Picasa stores face data in picasa.ini files alongside photos:
  [filename.jpg]
  faces=rect64(hexvalue),personhash;rect64(hexvalue),personhash

Person names come from contacts.xml (global) and [Contacts2] sections
(per-folder). The rect64 format encodes normalized face rectangles as a
64-bit hex value split into four 16-bit normalized coordinates.
"""

import configparser
import copy
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.core.models import EditInfo

logger = logging.getLogger(__name__)

_CONTACTS_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Picasa2/contacts/contacts.xml",
    Path(os.environ.get("APPDATA", ""))       / "Google/Picasa2/contacts/contacts.xml",
]
_INI_NAMES = ("picasa.ini", ".picasa.ini", "Picasa.ini")

_CROP_RE = re.compile(r"rect64\(([0-9a-fA-F]+)\)", re.IGNORECASE)


# ------------------------------------------------------------------ contacts

def find_contacts_xml() -> Path | None:
    for p in _CONTACTS_PATHS:
        if p.exists():
            return p
    return None


def parse_contacts_xml(path: Path) -> dict[str, str]:
    """Returns {hash: name}. Robust against encoding issues."""
    contacts: dict[str, str] = {}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        root = ET.fromstring(raw)
        for c in root:
            h    = c.get("id", "").strip()
            name = c.get("name", "").strip()
            if h and name:
                contacts[h] = name
    except Exception as e:
        logger.warning("Picasa contacts.xml parse error: %s", e)
    return contacts


# ------------------------------------------------------------------ ini parsing

def _decode_rect64(hex_str: str) -> tuple[float, float, float, float]:
    """Returns (left, top, right, bottom) as fractions in [0, 1]."""
    val    = int(hex_str, 16)
    left   = ((val >> 48) & 0xFFFF) / 65535.0
    top    = ((val >> 32) & 0xFFFF) / 65535.0
    right  = ((val >> 16) & 0xFFFF) / 65535.0
    bottom = (val         & 0xFFFF) / 65535.0
    return left, top, right, bottom


def _bbox_raw_to_exif(rx: int, ry: int, rw: int, rh: int,
                      raw_W: int, raw_H: int,
                      orientation: int) -> tuple[int, int, int, int]:
    """
    Transforme une bbox du repère image brute (stockée) vers le repère
    EXIF-corrigé (affiché), selon l'orientation EXIF (tag 274).

    Picasa stocke les rect64 en coordonnées de l'image brute sur disque.
    Tout le reste du système travaille en coordonnées EXIF-corrigées.
    """
    if orientation in (None, 1):
        return rx, ry, rw, rh
    if orientation == 2:   # flip horizontal
        return raw_W - rx - rw, ry, rw, rh
    if orientation == 3:   # rotate 180°
        return raw_W - rx - rw, raw_H - ry - rh, rw, rh
    if orientation == 4:   # flip vertical
        return rx, raw_H - ry - rh, rw, rh
    if orientation == 5:   # transpose (flip along main diagonal)
        return ry, rx, rh, rw
    if orientation == 6:   # rotate 90° CW → PIL ROTATE_270 (90° CW), out = H×W
        return raw_H - ry - rh, rx, rh, rw
    if orientation == 7:   # transverse transpose
        return raw_H - ry - rh, raw_W - rx - rw, rh, rw
    if orientation == 8:   # rotate 270° CW (= 90° CCW) → PIL ROTATE_90, out = H×W
        return ry, raw_W - rx - rw, rh, rw
    return rx, ry, rw, rh


def _parse_filters(filters_str: str) -> dict[str, list[float]]:
    """
    Parse the Picasa filters chain.

    Format: "filtername=enabled,p1,p2,...;filtername2=...;"
    Returns {name: [float params including enabled flag]}.
    """
    result: dict[str, list[float]] = {}
    for entry in filters_str.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, params_str = entry.partition("=")
        name = name.strip()
        try:
            params = [float(p) for p in params_str.split(",") if p.strip()]
        except ValueError:
            continue
        if params:
            result[name] = params
    return result


def _parse_section_edits(cp: configparser.RawConfigParser, section: str) -> dict:
    """Extract edit metadata from a picasa.ini section. Returns raw dict (may be empty)."""
    raw: dict = {}

    # Rotation: rotate=1 (90° CW), 2 (180°), 3 (270° CW / 90° CCW)
    if cp.has_option(section, "rotate"):
        try:
            raw["rotate"] = int(cp.get(section, "rotate"))
        except ValueError:
            pass

    # Crop: crop=rect64(hex) ou crop64=hex (Picasa >= 3)
    for key in ("crop", "crop64"):
        if cp.has_option(section, key):
            val = cp.get(section, key).strip()
            m = _CROP_RE.search(val)
            hex_str = m.group(1) if m else (val if re.match(r"^[0-9a-fA-F]{16}$", val) else None)
            if hex_str:
                raw["crop"] = _decode_rect64(hex_str)
                break

    # Filters chain
    if cp.has_option(section, "filters"):
        filters = _parse_filters(cp.get(section, "filters"))
        if filters:
            raw["filters"] = filters

    return raw


def _picasa_to_edit_steps(raw: dict) -> list[tuple[str, EditInfo]]:
    """
    Convertit un dict de retouches Picasa en une liste de (label, EditInfo cumulé),
    une entrée par filtre Picasa dans l'ordre du pipeline.
    Chaque EditInfo est l'état *accumulé* après application du filtre courant.
    Enregistrer chaque étape dans edit_history permet un undo filtre par filtre.
    Retourne [] si aucune retouche n'est trouvée.
    """
    steps: list[tuple[str, EditInfo]] = []
    edit = EditInfo()

    rotate_map = {1: 90.0, 2: 180.0, 3: 270.0}
    if "rotate" in raw:
        deg = rotate_map.get(raw["rotate"])
        if deg is not None:
            edit = copy.copy(edit)
            edit.rotation = deg
            steps.append(("picasa_rotate", copy.copy(edit)))

    if "crop" in raw:
        left, top, right, bottom = raw["crop"]
        if right > left and bottom > top:
            # apply_crop attend (x, y, w, h) ; Picasa donne (left, top, right, bottom)
            edit = copy.copy(edit)
            edit.crop = (left, top, right - left, bottom - top)
            steps.append(("picasa_crop", copy.copy(edit)))

    filters = raw.get("filters", {})

    if "bw" in filters and filters["bw"] and filters["bw"][0] >= 1:
        edit = copy.copy(edit)
        edit.bw = True
        steps.append(("picasa_bw", copy.copy(edit)))

    # Tilt / straighten: tilt=enabled,value[,0]
    # Picasa stocke la valeur dans [-π/4, π/4] correspondant à [-2.5°, +2.5°] (empirique).
    # Signe inversé par rapport à notre convention de redressement.
    if "tilt" in filters:
        p = filters["tilt"]
        if p and p[0] >= 1 and len(p) >= 2 and p[1] != 0.0:
            angle_deg = -(p[1] * 2.5 / (math.pi / 4.0))
            if abs(angle_deg) > 0.05:
                edit = copy.copy(edit)
                edit.straighten = max(-2.5, min(2.5, angle_deg))
                steps.append(("picasa_tilt", copy.copy(edit)))

    # Fine tune: finetune2=enabled,fill_light,highlights,color_temp,saturation,0
    if "finetune2" in filters:
        p = filters["finetune2"]
        if p and p[0] >= 1:
            changed = False
            edit = copy.copy(edit)
            if len(p) >= 2 and p[1] != 0.0:
                edit.brightness = max(-1.0, min(1.0, p[1]))
                changed = True
            if len(p) >= 3 and p[2] != 0.0:
                edit.contrast = max(-1.0, min(1.0, p[2] * 0.5))
                changed = True
            if len(p) >= 4 and p[3] != 0.0:
                temp = max(-1.0, min(1.0, p[3]))
                edit.color_red  = max(-1.0, min(1.0, edit.color_red  + temp * 0.15))
                edit.color_blue = max(-1.0, min(1.0, edit.color_blue - temp * 0.15))
                changed = True
            if len(p) >= 5 and p[4] != 0.0:
                edit.saturation = max(-1.0, min(1.0, p[4]))
                changed = True
            if changed:
                steps.append(("picasa_finetune2", copy.copy(edit)))

    # Fill light: fill=enabled,amount (0..1) → brightness (filtre dédié Picasa)
    if "fill" in filters:
        p = filters["fill"]
        if p and p[0] >= 1 and len(p) >= 2 and p[1] != 0.0:
            edit = copy.copy(edit)
            edit.brightness = max(-1.0, min(1.0, edit.brightness + p[1] * 0.8))
            steps.append(("picasa_fill", copy.copy(edit)))

    # Warmth: warmth=enabled,amount (-1..1)
    if "warmth" in filters:
        p = filters["warmth"]
        if p and p[0] >= 1 and len(p) >= 2 and p[1] != 0.0:
            temp = max(-1.0, min(1.0, p[1]))
            edit = copy.copy(edit)
            edit.color_red  = max(-1.0, min(1.0, edit.color_red  + temp * 0.20))
            edit.color_blue = max(-1.0, min(1.0, edit.color_blue - temp * 0.20))
            steps.append(("picasa_warmth", copy.copy(edit)))

    # Luminosity: lumi=enabled,amount (-1..1)
    if "lumi" in filters:
        p = filters["lumi"]
        if p and p[0] >= 1 and len(p) >= 2 and p[1] != 0.0:
            edit = copy.copy(edit)
            edit.brightness = max(-1.0, min(1.0, edit.brightness + p[1]))
            steps.append(("picasa_lumi", copy.copy(edit)))

    # Auto-éclairage: autolight=enabled,amount (0..1)
    if "autolight" in filters:
        p = filters["autolight"]
        if p and p[0] >= 1 and len(p) >= 2 and p[1] != 0.0:
            amount = max(0.0, min(1.0, p[1]))
            edit = copy.copy(edit)
            edit.brightness = max(-1.0, min(1.0, edit.brightness + amount * 0.15))
            edit.contrast   = max(-1.0, min(1.0, edit.contrast   + amount * 0.20))
            steps.append(("picasa_autolight", copy.copy(edit)))

    # Saturation dédiée (Picasa < 3)
    if "sat" in filters:
        p = filters["sat"]
        if p and p[0] >= 1 and len(p) >= 2 and p[1] != 0.0:
            edit = copy.copy(edit)
            edit.saturation = max(-1.0, min(1.0, edit.saturation + p[1]))
            steps.append(("picasa_sat", copy.copy(edit)))

    # Sharpening
    for fname in ("anisotropic", "sharpen"):
        if fname in filters:
            p = filters[fname]
            if p and p[0] >= 1:
                strength = p[1] if len(p) >= 2 else 0.5
                edit = copy.copy(edit)
                edit.sharpness = max(0.0, min(1.0, strength))
                steps.append((f"picasa_{fname}", copy.copy(edit)))
                break

    # Soft focus: softfocus=enabled,amount (0..1) → noise_reduction
    if "softfocus" in filters:
        p = filters["softfocus"]
        if p and p[0] >= 1 and len(p) >= 2 and p[1] != 0.0:
            edit = copy.copy(edit)
            edit.noise_reduction = max(0.0, min(1.0, p[1] * 0.5))
            steps.append(("picasa_softfocus", copy.copy(edit)))

    return steps


def _picasa_to_edit_info(raw: dict) -> EditInfo | None:
    """Retourne l'état final fusionné (dernier step). Wrapper de _picasa_to_edit_steps."""
    steps = _picasa_to_edit_steps(raw)
    return steps[-1][1] if steps else None


def _parse_ini(ini_path: Path) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]], dict[str, dict]]:
    """
    Parse a single picasa.ini file.

    Returns
    -------
    contacts : {hash: name}  from the [Contacts2] section
    faces    : {filename: [(rect64_hex, person_hash), ...]}
    edits    : {filename: raw_edit_dict}
    """
    cp = configparser.RawConfigParser()
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            cp.read(str(ini_path), encoding=enc)
            break
        except Exception:
            cp = configparser.RawConfigParser()

    contacts: dict[str, str] = {}
    if cp.has_section("Contacts2"):
        for key, val in cp.items("Contacts2"):
            name = val.split(";")[0].strip()
            if name:
                contacts[key] = name

    rect_re = re.compile(r"rect64\(([0-9a-fA-F]+)\),([0-9a-fA-F]+)")
    faces: dict[str, list[tuple[str, str]]] = {}
    edits: dict[str, dict] = {}
    for section in cp.sections():
        if section in ("Contacts2", "Picasa") or section.startswith(".album:"):
            continue
        if cp.has_option(section, "faces"):
            entries = [
                (m.group(1), m.group(2))
                for m in rect_re.finditer(cp.get(section, "faces"))
            ]
            if entries:
                faces[section] = entries
        raw = _parse_section_edits(cp, section)
        if raw:
            edits[section] = raw

    return contacts, faces, edits


# ------------------------------------------------------------------ discovery

def find_ini_files(folders: list[str]) -> list[Path]:
    """Recursively find all picasa.ini files under the given folder roots."""
    found: list[Path] = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for dirpath, dirs, filenames in os.walk(folder):
            dirs[:] = [d for d in dirs if d != "Originals"]
            for name in _INI_NAMES:
                if name in filenames:
                    found.append(Path(dirpath) / name)
    return found


def scan(folders: list[str]) -> tuple[int, int, int]:
    """
    Quick scan without full import.

    Returns
    -------
    (n_contacts, n_photos_with_faces, n_photos_with_edits)
    """
    # Doit refléter exactement le comptage de _run_import() (contacts globaux
    # + contacts locaux par picasa.ini, dédupliqués par nom puisque
    # _run_import() crée une personne par nom distinct, pas par hash).
    all_contacts: dict[str, str] = {}
    cx = find_contacts_xml()
    if cx:
        all_contacts.update(parse_contacts_xml(cx))

    n_photos = 0
    n_edits = 0
    for ini_path in find_ini_files(folders):
        local_contacts, faces, edits = _parse_ini(ini_path)
        all_contacts.update(local_contacts)
        n_photos += len(faces)
        n_edits += len(edits)

    n_contacts = len(set(all_contacts.values()))

    return n_contacts, n_photos, n_edits


# ------------------------------------------------------------------ result

class PicasaImportResult:
    def __init__(self) -> None:
        self.persons_created:  int             = 0
        self.faces_imported:   int             = 0
        self.photos_processed: int             = 0
        self.edits_imported:   int             = 0
        self.errors:           list[str]       = []
        self.edited_map:       dict           = {}  # {path_str: EditInfo}


# ------------------------------------------------------------------ import logic

def _run_import(
    catalog,
    face_db,
    folders: list[str],
    edit_db=None,           # EditDatabase optionnel pour importer les retouches
    progress_cb=None,       # callable(current: int, total: int) → None
) -> PicasaImportResult:
    result = PicasaImportResult()

    # Global contacts
    all_contacts: dict[str, str] = {}
    cx = find_contacts_xml()
    if cx:
        all_contacts.update(parse_contacts_xml(cx))
        logger.info("Picasa: %d contacts chargés depuis %s", len(all_contacts), cx)

    # Collect ini files
    ini_files = find_ini_files(folders)
    total = len(ini_files)
    logger.info("Picasa: %d fichiers picasa.ini trouvés", total)
    if total == 0:
        return result

    # First pass: collect all local contacts to build a complete hash→name map
    ini_data: list[tuple[Path, dict, dict]] = []
    for ini_path in ini_files:
        local_contacts, faces, edits = _parse_ini(ini_path)
        all_contacts.update(local_contacts)
        ini_data.append((ini_path, faces, edits))

    # Create missing persons in catalog
    existing_persons = {p.name: p for p in catalog.get_persons()}
    hash_to_person_id: dict[str, int] = {}
    for h, name in all_contacts.items():
        if name not in existing_persons:
            person = catalog.create_person(name)
            existing_persons[name] = person
            result.persons_created += 1
        hash_to_person_id[h] = existing_persons[name].id

    # Nettoyage des person_ids orphelins : faces et annotations qui référencent
    # d'anciens IDs devenus invalides (après réinitialisation de catalog.db).
    # Sans ce nettoyage, les faces portant l'ancien ID restent « invisibles »
    # même si la personne existe à nouveau avec un nouvel ID.
    all_valid_ids = {p.id for p in existing_persons.values()}
    face_db.cleanup_orphan_person_ids(all_valid_ids)

    # Second pass: import faces and edits
    for i, (ini_path, faces, edits_map) in enumerate(ini_data):
        if progress_cb:
            progress_cb(i + 1, total)
        folder = ini_path.parent

        for filename, entries in faces.items():
            photo_path = folder / filename
            if not photo_path.exists():
                continue

            try:
                from PIL import Image, ImageOps
                with Image.open(str(photo_path)) as img:
                    raw_w, raw_h = img.size
                    exif_ori = img.getexif().get(274, 1)
                    # Dimensions après correction EXIF (repère de travail du système)
                    corr = ImageOps.exif_transpose(img)
                    img_w, img_h = corr.size
            except Exception:
                continue

            annotations: list[dict] = []
            for rect_hex, person_hash in entries:
                try:
                    lf, tf, rf, bf = _decode_rect64(rect_hex)
                    # Picasa stocke les rect64 en coordonnées de l'image brute
                    rx = int(lf * raw_w)
                    ry = int(tf * raw_h)
                    rw = int((rf - lf) * raw_w)
                    rh = int((bf - tf) * raw_h)
                    # Transformer vers l'espace EXIF-corrigé
                    x, y, w, h = _bbox_raw_to_exif(rx, ry, rw, rh, raw_w, raw_h, exif_ori)
                    if w < 10 or h < 10:
                        continue
                    pid = hash_to_person_id.get(person_hash)
                    if pid:
                        annotations.append({"bbox": (x, y, w, h), "person_id": pid})
                except Exception as exc:
                    result.errors.append(f"{photo_path.name}: {exc}")

            if not annotations:
                continue

            # Stocker en table dédiée ; sera appliqué lors de la détection ArcFace
            # (ou immédiatement si la photo est déjà détectée)
            face_db.save_picasa_annotations(str(photo_path), annotations)
            result.faces_imported  += len(annotations)
            result.photos_processed += 1

        # Import des retouches Picasa (seulement si demandé et photo non encore retouchée)
        if edit_db is not None:
            for filename, raw in edits_map.items():
                photo_path = folder / filename
                if not photo_path.exists():
                    continue
                path_str = os.path.normpath(str(photo_path))
                if edit_db.has_edits(path_str):
                    continue  # ne pas écraser les retouches existantes
                steps = _picasa_to_edit_steps(raw)
                if steps:
                    # État vierge = point de départ pour undo complet
                    edit_db.save(path_str, EditInfo(), operation="picasa_before")
                    for op_name, step_edit in steps:
                        edit_db.save(path_str, step_edit, operation=op_name)
                    result.edits_imported += 1
                    result.edited_map[path_str] = steps[-1][1]

    # Nettoyage 1 : placeholders qui chevauchent une face ArcFace (même région,
    # person_id différents — contacts Picasa ≠ cluster ArcFace).
    n_dupes = face_db.cleanup_overlapping_placeholders()
    if n_dupes:
        result.faces_imported = max(0, result.faces_imported - n_dupes)
    # Nettoyage 2 : placeholders orphelins dont le person_id n'apparaît plus dans
    # aucune annotation Picasa courante (résidus d'anciens imports avec IDs différents).
    face_db.cleanup_stale_placeholder_faces()

    logger.info(
        "Picasa import terminé : %d personnes créées, %d visages, %d photos, %d retouches",
        result.persons_created, result.faces_imported, result.photos_processed, result.edits_imported,
    )
    return result


# ------------------------------------------------------------------ QThread wrapper

class PicasaImportThread(QThread):
    """Run the Picasa import in a background thread."""

    progress = Signal(int, int)   # current_ini, total_ini
    finished = Signal(object)     # PicasaImportResult

    def __init__(self, catalog, face_db, folders: list[str], edit_db=None, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._face_db = face_db
        self._folders = folders
        self._edit_db = edit_db

    def run(self) -> None:
        result = _run_import(
            self._catalog,
            self._face_db,
            self._folders,
            edit_db=self._edit_db,
            progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
        )
        self.finished.emit(result)
