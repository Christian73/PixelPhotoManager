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
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

_CONTACTS_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Picasa2/contacts/contacts.xml",
    Path(os.environ.get("APPDATA", ""))       / "Google/Picasa2/contacts/contacts.xml",
]
_INI_NAMES = ("picasa.ini", ".picasa.ini", "Picasa.ini")


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


def _parse_ini(ini_path: Path) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """
    Parse a single picasa.ini file.

    Returns
    -------
    contacts : {hash: name}  from the [Contacts2] section
    faces    : {filename: [(rect64_hex, person_hash), ...]}
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

    return contacts, faces


# ------------------------------------------------------------------ discovery

def find_ini_files(folders: list[str]) -> list[Path]:
    """Recursively find all picasa.ini files under the given folder roots."""
    found: list[Path] = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for dirpath, _dirs, filenames in os.walk(folder):
            for name in _INI_NAMES:
                if name in filenames:
                    found.append(Path(dirpath) / name)
    return found


def scan(folders: list[str]) -> tuple[int, int]:
    """
    Quick scan without full import.

    Returns
    -------
    (n_global_contacts, n_photos_with_faces)
    """
    n_contacts = 0
    cx = find_contacts_xml()
    if cx:
        n_contacts = len(parse_contacts_xml(cx))

    n_photos = 0
    for ini_path in find_ini_files(folders):
        _, faces = _parse_ini(ini_path)
        n_photos += len(faces)

    return n_contacts, n_photos


# ------------------------------------------------------------------ result

class PicasaImportResult:
    def __init__(self) -> None:
        self.persons_created:  int       = 0
        self.faces_imported:   int       = 0
        self.photos_processed: int       = 0
        self.errors:           list[str] = []


# ------------------------------------------------------------------ import logic

def _run_import(
    catalog,
    face_db,
    folders: list[str],
    progress_cb=None,   # callable(current: int, total: int) → None
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
    ini_data: list[tuple[Path, dict[str, list]]] = []
    for ini_path in ini_files:
        local_contacts, faces = _parse_ini(ini_path)
        all_contacts.update(local_contacts)
        ini_data.append((ini_path, faces))

    # Create missing persons in catalog
    existing_persons = {p.name: p for p in catalog.get_persons()}
    hash_to_person_id: dict[str, int] = {}
    for h, name in all_contacts.items():
        if name not in existing_persons:
            person = catalog.create_person(name)
            existing_persons[name] = person
            result.persons_created += 1
        hash_to_person_id[h] = existing_persons[name].id

    # Second pass: import faces
    for i, (ini_path, faces) in enumerate(ini_data):
        if progress_cb:
            progress_cb(i + 1, total)
        folder = ini_path.parent

        for filename, entries in faces.items():
            photo_path = folder / filename
            if not photo_path.exists():
                continue

            try:
                from PIL import Image
                with Image.open(str(photo_path)) as img:
                    img_w, img_h = img.size
            except Exception:
                continue

            detections: list[dict] = []
            person_hashes: list[str] = []
            for rect_hex, person_hash in entries:
                try:
                    lf, tf, rf, bf = _decode_rect64(rect_hex)
                    x = int(lf * img_w)
                    y = int(tf * img_h)
                    w = int((rf - lf) * img_w)
                    h = int((bf - tf) * img_h)
                    if w < 10 or h < 10:
                        continue
                    detections.append({"bbox": (x, y, w, h), "embedding": None})
                    person_hashes.append(person_hash)
                except Exception as exc:
                    result.errors.append(f"{photo_path.name}: {exc}")

            if not detections:
                continue

            face_db.save_faces(str(photo_path), detections)

            # Assign persons in insertion order (matches save_faces insertion)
            saved_faces = face_db.get_faces_for_photo(str(photo_path))
            for face, ph in zip(saved_faces, person_hashes):
                pid = hash_to_person_id.get(ph)
                if pid:
                    face_db.assign_person_to_face(face.id, pid)

            result.faces_imported  += len(detections)
            result.photos_processed += 1

    logger.info(
        "Picasa import terminé : %d personnes créées, %d visages, %d photos",
        result.persons_created, result.faces_imported, result.photos_processed,
    )
    return result


# ------------------------------------------------------------------ QThread wrapper

class PicasaImportThread(QThread):
    """Run the Picasa import in a background thread."""

    progress = Signal(int, int)   # current_ini, total_ini
    finished = Signal(object)     # PicasaImportResult

    def __init__(self, catalog, face_db, folders: list[str], parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._face_db = face_db
        self._folders = folders

    def run(self) -> None:
        result = _run_import(
            self._catalog,
            self._face_db,
            self._folders,
            progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
        )
        self.finished.emit(result)
