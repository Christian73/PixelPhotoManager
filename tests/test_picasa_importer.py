# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/faces/picasa_importer.py` en pur Python (sans Qt ni InsightFace) :
décodage rect64, transformation bbox brute → EXIF-corrigée (8 orientations),
parsing des chaînes de filtres et des picasa.ini (contacts locaux, faces,
retouches), conversion Picasa → EditInfo par étapes cumulées, découverte des
fichiers ini (exclusion Originals), scan rapide et import complet `_run_import`
sur de vraies bases Catalog/FaceDatabase/EditDatabase temporaires."""
import sqlite3

import pytest
from PIL import Image

from src.core.models import EditInfo
from src.faces import picasa_importer as pi
from src.faces.face_database import FaceDatabase
from src.library.catalog import Catalog
from src.processing.edit_database import EditDatabase


def _enc_rect64(left: float, top: float, right: float, bottom: float) -> str:
    """Encode 4 fractions [0,1] en hex rect64 (inverse de _decode_rect64)."""
    vals = [round(v * 65535) for v in (left, top, right, bottom)]
    packed = (vals[0] << 48) | (vals[1] << 32) | (vals[2] << 16) | vals[3]
    return f"{packed:016x}"


def _make_image(path, size=(200, 100), orientation=None) -> None:
    img = Image.new("RGB", size, color=(120, 120, 120))
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        img.save(str(path), exif=exif)
    else:
        img.save(str(path))


# ------------------------------------------------------------------ rect64


class TestDecodeRect64:
    def test_zero(self):
        assert pi._decode_rect64("0") == (0.0, 0.0, 0.0, 0.0)

    def test_full_frame(self):
        l, t, r, b = pi._decode_rect64("ffffffffffffffff")
        assert (l, t, r, b) == (1.0, 1.0, 1.0, 1.0)

    def test_roundtrip(self):
        hex_str = _enc_rect64(0.25, 0.10, 0.75, 0.90)
        l, t, r, b = pi._decode_rect64(hex_str)
        assert l == pytest.approx(0.25, abs=1e-4)
        assert t == pytest.approx(0.10, abs=1e-4)
        assert r == pytest.approx(0.75, abs=1e-4)
        assert b == pytest.approx(0.90, abs=1e-4)

    def test_known_quadrants(self):
        # left=top=0, right=bottom=0.5 → moitié haut-gauche
        val = (0 << 48) | (0 << 32) | (0x8000 << 16) | 0x8000
        l, t, r, b = pi._decode_rect64(f"{val:016x}")
        assert l == 0.0 and t == 0.0
        assert r == pytest.approx(0.5, abs=1e-3)
        assert b == pytest.approx(0.5, abs=1e-3)


class TestBboxRawToExif:
    """Vérifie chaque orientation EXIF sur une bbox asymétrique dans une image
    raw 100×60 (donc corrigée 60×100 pour les orientations 5-8)."""

    RAW_W, RAW_H = 100, 60
    BBOX = (10, 20, 30, 15)  # x, y, w, h

    def _t(self, ori):
        return pi._bbox_raw_to_exif(*self.BBOX, self.RAW_W, self.RAW_H, ori)

    def test_orientation_none_and_1_identity(self):
        assert self._t(None) == self.BBOX
        assert self._t(1) == self.BBOX

    def test_orientation_2_flip_h(self):
        # x' = W - x - w = 100-10-30 = 60
        assert self._t(2) == (60, 20, 30, 15)

    def test_orientation_3_rot180(self):
        # x' = 60, y' = 60-20-15 = 25
        assert self._t(3) == (60, 25, 30, 15)

    def test_orientation_4_flip_v(self):
        assert self._t(4) == (10, 25, 30, 15)

    def test_orientation_5_transpose(self):
        # swap x/y et w/h
        assert self._t(5) == (20, 10, 15, 30)

    def test_orientation_6_rot90cw(self):
        # x' = H - y - h = 60-20-15 = 25, y' = x = 10
        assert self._t(6) == (25, 10, 15, 30)

    def test_orientation_7_transverse(self):
        assert self._t(7) == (25, 60, 15, 30)

    def test_orientation_8_rot270cw(self):
        # x' = y = 20, y' = W - x - w = 60
        assert self._t(8) == (20, 60, 15, 30)

    def test_unknown_orientation_identity(self):
        assert self._t(9) == self.BBOX

    def test_result_stays_in_corrected_frame(self):
        """Pour chaque orientation, la bbox transformée doit tenir dans les
        dimensions EXIF-corrigées de l'image."""
        for ori in range(1, 9):
            x, y, w, h = self._t(ori)
            if ori in (5, 6, 7, 8):
                cw, ch = self.RAW_H, self.RAW_W
            else:
                cw, ch = self.RAW_W, self.RAW_H
            assert 0 <= x and x + w <= cw, f"orientation {ori}"
            assert 0 <= y and y + h <= ch, f"orientation {ori}"


# ------------------------------------------------------------------ filters


class TestParseFilters:
    def test_simple_chain(self):
        out = pi._parse_filters("bw=1;tilt=1,0.5,0;")
        assert out == {"bw": [1.0], "tilt": [1.0, 0.5, 0.0]}

    def test_empty_string(self):
        assert pi._parse_filters("") == {}

    def test_malformed_entries_skipped(self):
        out = pi._parse_filters("noequal;bad=abc,def;ok=1,2")
        assert out == {"ok": [1.0, 2.0]}

    def test_entry_without_params_skipped(self):
        assert pi._parse_filters("name=;other=1") == {"other": [1.0]}


# ------------------------------------------------------------------ section edits


def _cp_from_text(tmp_path, text):
    import configparser
    p = tmp_path / "picasa.ini"
    p.write_text(text, encoding="utf-8")
    cp = configparser.RawConfigParser()
    cp.read(str(p), encoding="utf-8")
    return cp


class TestParseSectionEdits:
    def test_rotate(self, tmp_path):
        cp = _cp_from_text(tmp_path, "[a.jpg]\nrotate=2\n")
        assert pi._parse_section_edits(cp, "a.jpg") == {"rotate": 2}

    def test_rotate_invalid_ignored(self, tmp_path):
        cp = _cp_from_text(tmp_path, "[a.jpg]\nrotate=abc\n")
        assert pi._parse_section_edits(cp, "a.jpg") == {}

    def test_crop_rect64_format(self, tmp_path):
        hex_str = _enc_rect64(0.1, 0.2, 0.9, 0.8)
        cp = _cp_from_text(tmp_path, f"[a.jpg]\ncrop=rect64({hex_str})\n")
        raw = pi._parse_section_edits(cp, "a.jpg")
        l, t, r, b = raw["crop"]
        assert l == pytest.approx(0.1, abs=1e-3)
        assert b == pytest.approx(0.8, abs=1e-3)

    def test_crop64_bare_hex(self, tmp_path):
        hex_str = _enc_rect64(0.0, 0.0, 0.5, 0.5)
        cp = _cp_from_text(tmp_path, f"[a.jpg]\ncrop64={hex_str}\n")
        raw = pi._parse_section_edits(cp, "a.jpg")
        assert "crop" in raw

    def test_crop_invalid_value_ignored(self, tmp_path):
        cp = _cp_from_text(tmp_path, "[a.jpg]\ncrop=notahex\n")
        assert pi._parse_section_edits(cp, "a.jpg") == {}

    def test_filters(self, tmp_path):
        cp = _cp_from_text(tmp_path, "[a.jpg]\nfilters=bw=1;\n")
        raw = pi._parse_section_edits(cp, "a.jpg")
        assert raw == {"filters": {"bw": [1.0]}}

    def test_empty_section(self, tmp_path):
        cp = _cp_from_text(tmp_path, "[a.jpg]\nstar=yes\n")
        assert pi._parse_section_edits(cp, "a.jpg") == {}


# ------------------------------------------------------------------ edit steps


class TestPicasaToEditSteps:
    def test_empty_raw_gives_no_steps(self):
        assert pi._picasa_to_edit_steps({}) == []
        assert pi._picasa_to_edit_info({}) is None

    def test_rotate_mapping(self):
        for code, deg in ((1, 90.0), (2, 180.0), (3, 270.0)):
            steps = pi._picasa_to_edit_steps({"rotate": code})
            assert len(steps) == 1
            label, edit = steps[0]
            assert label == "picasa_rotate"
            assert edit.rotation == deg

    def test_rotate_unknown_code_ignored(self):
        assert pi._picasa_to_edit_steps({"rotate": 7}) == []

    def test_crop_toplevel(self):
        steps = pi._picasa_to_edit_steps({"crop": (0.1, 0.2, 0.6, 0.9)})
        label, edit = steps[0]
        assert label == "picasa_crop"
        x, y, w, h = edit.crop
        assert (x, y) == (0.1, 0.2)
        assert w == pytest.approx(0.5)
        assert h == pytest.approx(0.7)

    def test_crop_degenerate_ignored(self):
        assert pi._picasa_to_edit_steps({"crop": (0.5, 0.5, 0.5, 0.5)}) == []

    def test_crop_from_filters_needs_corrected_size(self):
        raw = {"filters": {"crop": [1.0, 100.0, 50.0, 300.0, 250.0]}}
        assert pi._picasa_to_edit_steps(raw) == []  # sans corrected_size
        steps = pi._picasa_to_edit_steps(raw, corrected_size=(400, 500))
        label, edit = steps[0]
        assert label == "picasa_crop"
        x, y, w, h = edit.crop
        assert x == pytest.approx(100 / 400)
        assert y == pytest.approx(50 / 500)
        assert w == pytest.approx(200 / 400)
        assert h == pytest.approx(200 / 500)

    def test_crop_toplevel_wins_over_filters(self):
        raw = {
            "crop": (0.0, 0.0, 0.5, 0.5),
            "filters": {"crop": [1.0, 10.0, 10.0, 20.0, 20.0]},
        }
        steps = pi._picasa_to_edit_steps(raw, corrected_size=(100, 100))
        crops = [s for s in steps if s[0] == "picasa_crop"]
        assert len(crops) == 1
        assert crops[0][1].crop[2] == pytest.approx(0.5)

    def test_bw(self):
        steps = pi._picasa_to_edit_steps({"filters": {"bw": [1.0]}})
        assert steps[0][0] == "picasa_bw"
        assert steps[0][1].bw is True

    def test_bw_disabled(self):
        assert pi._picasa_to_edit_steps({"filters": {"bw": [0.0]}}) == []

    def test_tilt_sign_and_scale(self):
        import math
        # valeur pi/4 → -2.5° dans notre convention
        steps = pi._picasa_to_edit_steps(
            {"filters": {"tilt": [1.0, math.pi / 4.0, 0.0]}}
        )
        assert steps[0][0] == "picasa_tilt"
        assert steps[0][1].straighten == pytest.approx(-2.5)

    def test_tilt_near_zero_ignored(self):
        steps = pi._picasa_to_edit_steps({"filters": {"tilt": [1.0, 1e-5, 0.0]}})
        assert steps == []

    def test_finetune2_all_params(self):
        steps = pi._picasa_to_edit_steps(
            {"filters": {"finetune2": [1.0, 0.3, 0.4, 0.5, 0.6, 0.0]}}
        )
        assert len(steps) == 1
        edit = steps[0][1]
        assert edit.brightness == pytest.approx(0.3)
        assert edit.contrast == pytest.approx(0.2)   # 0.4 * 0.5
        assert edit.color_red == pytest.approx(0.075)  # 0.5 * 0.15
        assert edit.color_blue == pytest.approx(-0.075)
        assert edit.saturation == pytest.approx(0.6)

    def test_finetune2_all_zero_no_step(self):
        steps = pi._picasa_to_edit_steps(
            {"filters": {"finetune2": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]}}
        )
        assert steps == []

    def test_fill_light(self):
        steps = pi._picasa_to_edit_steps({"filters": {"fill": [1.0, 0.5]}})
        assert steps[0][0] == "picasa_fill"
        assert steps[0][1].brightness == pytest.approx(0.4)  # 0.5 * 0.8

    def test_warmth(self):
        steps = pi._picasa_to_edit_steps({"filters": {"warmth": [1.0, 0.5]}})
        edit = steps[0][1]
        assert edit.color_red == pytest.approx(0.10)
        assert edit.color_blue == pytest.approx(-0.10)

    def test_lumi(self):
        steps = pi._picasa_to_edit_steps({"filters": {"lumi": [1.0, -0.3]}})
        assert steps[0][1].brightness == pytest.approx(-0.3)

    def test_autolight(self):
        steps = pi._picasa_to_edit_steps({"filters": {"autolight": [1.0, 1.0]}})
        edit = steps[0][1]
        assert edit.brightness == pytest.approx(0.15)
        assert edit.contrast == pytest.approx(0.20)

    def test_sat(self):
        steps = pi._picasa_to_edit_steps({"filters": {"sat": [1.0, 0.7]}})
        assert steps[0][1].saturation == pytest.approx(0.7)

    def test_sharpen_and_anisotropic_exclusive(self):
        steps = pi._picasa_to_edit_steps(
            {"filters": {"anisotropic": [1.0, 0.6], "sharpen": [1.0, 0.9]}}
        )
        labels = [s[0] for s in steps]
        assert labels == ["picasa_anisotropic"]
        assert steps[0][1].sharpness == pytest.approx(0.6)

    def test_sharpen_default_strength(self):
        steps = pi._picasa_to_edit_steps({"filters": {"sharpen": [1.0]}})
        assert steps[0][1].sharpness == pytest.approx(0.5)

    def test_softfocus(self):
        steps = pi._picasa_to_edit_steps({"filters": {"softfocus": [1.0, 0.8]}})
        assert steps[0][1].noise_reduction == pytest.approx(0.4)

    def test_steps_are_cumulative(self):
        """Chaque étape doit contenir l'état accumulé, pas l'état isolé."""
        raw = {"rotate": 1, "filters": {"bw": [1.0], "lumi": [1.0, 0.5]}}
        steps = pi._picasa_to_edit_steps(raw)
        labels = [s[0] for s in steps]
        assert labels == ["picasa_rotate", "picasa_bw", "picasa_lumi"]
        final = steps[-1][1]
        assert final.rotation == 90.0
        assert final.bw is True
        assert final.brightness == pytest.approx(0.5)
        # les étapes intermédiaires ne sont pas mutées rétroactivement
        assert steps[0][1].bw is False
        assert steps[1][1].brightness == 0.0

    def test_picasa_to_edit_info_returns_final_state(self):
        raw = {"rotate": 2, "filters": {"bw": [1.0]}}
        info = pi._picasa_to_edit_info(raw)
        assert info.rotation == 180.0
        assert info.bw is True


# ------------------------------------------------------------------ contacts.xml


class TestParseContactsXml:
    def test_valid(self, tmp_path):
        p = tmp_path / "contacts.xml"
        p.write_bytes(
            b'<?xml version="1.0"?><contacts>'
            b'<contact id="abc123" name="Alice Dupont"/>'
            b'<contact id="def456" name="Bob Martin"/>'
            b'<contact id="" name="SansHash"/>'
            b'<contact id="ghi789" name=""/>'
            b"</contacts>"
        )
        contacts = pi.parse_contacts_xml(p)
        assert contacts == {"abc123": "Alice Dupont", "def456": "Bob Martin"}

    def test_invalid_xml_returns_empty(self, tmp_path):
        p = tmp_path / "contacts.xml"
        p.write_bytes(b"not xml at all <<<")
        assert pi.parse_contacts_xml(p) == {}

    def test_missing_file_returns_empty(self, tmp_path):
        assert pi.parse_contacts_xml(tmp_path / "absent.xml") == {}


class TestFindContactsXml:
    def test_found(self, tmp_path, monkeypatch):
        p = tmp_path / "contacts.xml"
        p.write_bytes(b"<contacts/>")
        monkeypatch.setattr(pi, "_CONTACTS_PATHS", [tmp_path / "absent.xml", p])
        assert pi.find_contacts_xml() == p

    def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pi, "_CONTACTS_PATHS", [tmp_path / "absent.xml"])
        assert pi.find_contacts_xml() is None


# ------------------------------------------------------------------ _parse_ini


class TestParseIni:
    def test_full_file(self, tmp_path):
        hex_face = _enc_rect64(0.1, 0.1, 0.5, 0.5)
        ini = tmp_path / "picasa.ini"
        ini.write_text(
            "[Contacts2]\n"
            "aa01=Alice Dupont;;\n"
            "bb02=Bob Martin;;\n"
            "emptyname=;;\n"
            "[Picasa]\n"
            "name=Mon dossier\n"
            "[.album:xyz]\n"
            "name=Album\n"
            "[photo1.jpg]\n"
            f"faces=rect64({hex_face}),aa01\n"
            "rotate=1\n"
            "[photo2.jpg]\n"
            f"faces=rect64({hex_face}),aa01;rect64({hex_face}),bb02\n"
            "[photo3.jpg]\n"
            "star=yes\n",
            encoding="utf-8",
        )
        contacts, faces, edits = pi._parse_ini(ini)
        assert contacts == {"aa01": "Alice Dupont", "bb02": "Bob Martin"}
        assert set(faces.keys()) == {"photo1.jpg", "photo2.jpg"}
        assert len(faces["photo2.jpg"]) == 2
        assert faces["photo1.jpg"][0][1] == "aa01"
        assert list(edits.keys()) == ["photo1.jpg"]
        assert edits["photo1.jpg"]["rotate"] == 1

    def test_latin1_fallback(self, tmp_path):
        ini = tmp_path / "picasa.ini"
        # é en latin-1 (0xE9) est invalide en UTF-8 → force le fallback
        ini.write_bytes(b"[Contacts2]\naa01=H\xe9l\xe8ne;;\n")
        contacts, faces, edits = pi._parse_ini(ini)
        assert contacts == {"aa01": "H\xe9l\xe8ne"}

    def test_empty_file(self, tmp_path):
        ini = tmp_path / "picasa.ini"
        ini.write_text("", encoding="utf-8")
        contacts, faces, edits = pi._parse_ini(ini)
        assert contacts == {} and faces == {} and edits == {}


# ------------------------------------------------------------------ discovery & scan


class TestFindIniFiles:
    def test_recursive_and_names(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "picasa.ini").write_text("")
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / ".picasa.ini").write_text("")
        (tmp_path / "b" / "sub").mkdir()
        (tmp_path / "b" / "sub" / "Picasa.ini").write_text("")
        found = pi.find_ini_files([str(tmp_path)])
        assert len(found) == 3

    def test_originals_excluded(self, tmp_path):
        (tmp_path / "Originals").mkdir()
        (tmp_path / "Originals" / "picasa.ini").write_text("")
        (tmp_path / "picasa.ini").write_text("")
        found = pi.find_ini_files([str(tmp_path)])
        assert len(found) == 1
        assert "Originals" not in str(found[0])

    def test_nonexistent_folder_skipped(self, tmp_path):
        assert pi.find_ini_files([str(tmp_path / "nope")]) == []


class TestScan:
    def test_counts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: None)
        hex_face = _enc_rect64(0.1, 0.1, 0.5, 0.5)
        d1 = tmp_path / "d1"
        d1.mkdir()
        (d1 / "picasa.ini").write_text(
            "[Contacts2]\nh1=Alice;;\nh2=Bob;;\n"
            f"[p1.jpg]\nfaces=rect64({hex_face}),aa01\n"
            "[p2.jpg]\nrotate=1\n",
            encoding="utf-8",
        )
        d2 = tmp_path / "d2"
        d2.mkdir()
        (d2 / "picasa.ini").write_text(
            # ee05 = même nom qu'Alice → dédupliqué par nom
            "[Contacts2]\nh3=Alice;;\n"
            f"[p3.jpg]\nfaces=rect64({hex_face}),ee05\nrotate=2\n",
            encoding="utf-8",
        )
        n_contacts, n_photos, n_edits = pi.scan([str(tmp_path)])
        assert n_contacts == 2   # Alice dédupliquée par nom
        assert n_photos == 2     # p1 et p3
        assert n_edits == 2      # p2 et p3

    def test_global_contacts_included(self, tmp_path, monkeypatch):
        cx = tmp_path / "contacts.xml"
        cx.write_bytes(
            b'<contacts><contact id="g1" name="Carol"/></contacts>'
        )
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: cx)
        n_contacts, n_photos, n_edits = pi.scan([str(tmp_path)])
        assert n_contacts == 1
        assert n_photos == 0 and n_edits == 0


# ------------------------------------------------------------------ _run_import


@pytest.fixture
def import_env(tmp_path, monkeypatch):
    """Environnement complet : catalog + face_db + edit_db + dossier photos."""
    monkeypatch.setattr(pi, "find_contacts_xml", lambda: None)
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    face_db = FaceDatabase(db_path=tmp_path / "faces.db")
    edit_db = EditDatabase(db_path=tmp_path / "edits.db")
    photos = tmp_path / "photos"
    photos.mkdir()
    return catalog, face_db, edit_db, photos


def _write_faces_ini(folder, filename, entries, extra=""):
    """entries = [(left, top, right, bottom, hash)]"""
    parts = ";".join(
        f"rect64({_enc_rect64(l, t, r, b)}),{h}" for (l, t, r, b, h) in entries
    )
    (folder / "picasa.ini").write_text(
        f"[Contacts2]\naa01=Alice;;\nbb02=Bob;;\n"
        f"[{filename}]\nfaces={parts}\n{extra}",
        encoding="utf-8",
    )


class TestRunImport:
    def test_import_creates_persons_and_annotations(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg", size=(200, 100))
        _write_faces_ini(
            photos, "p1.jpg",
            [(0.1, 0.1, 0.5, 0.9, "aa01"), (0.6, 0.1, 0.9, 0.9, "bb02")],
        )

        result = pi._run_import(catalog, face_db, [str(photos)])

        assert result.persons_created == 2
        assert result.faces_imported == 2
        assert result.photos_processed == 1
        assert result.errors == []

        names = {p.name for p in catalog.get_persons()}
        assert names == {"Alice", "Bob"}

        conn = sqlite3.connect(face_db._db_path)
        try:
            rows = conn.execute(
                "SELECT photo_path, bbox_x, bbox_y, bbox_w, bbox_h"
                " FROM picasa_annotations"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 2
        # bbox d'Alice : left 0.1*200=20, top 0.1*100=10, w 0.4*200=80, h 0.8*100=80
        bboxes = {tuple(r[1:]) for r in rows}
        assert (20, 10, 80, 80) in bboxes

    def test_reimport_does_not_duplicate_persons(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg")
        _write_faces_ini(photos, "p1.jpg", [(0.1, 0.1, 0.5, 0.9, "aa01")])

        r1 = pi._run_import(catalog, face_db, [str(photos)])
        r2 = pi._run_import(catalog, face_db, [str(photos)])

        assert r1.persons_created == 2  # Alice + Bob (Contacts2 complet)
        assert r2.persons_created == 0
        assert len(catalog.get_persons()) == 2

    def test_missing_photo_skipped(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _write_faces_ini(photos, "absente.jpg", [(0.1, 0.1, 0.5, 0.9, "aa01")])
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.photos_processed == 0
        assert result.faces_imported == 0

    def test_tiny_face_skipped(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg", size=(200, 100))
        # 0.02 × 200 = 4 px de large → < 10 px, ignoré
        _write_faces_ini(photos, "p1.jpg", [(0.10, 0.10, 0.12, 0.14, "aa01")])
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.faces_imported == 0

    def test_unknown_hash_skipped(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg")
        _write_faces_ini(photos, "p1.jpg", [(0.1, 0.1, 0.5, 0.9, "dd04")])
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.faces_imported == 0

    def test_exif_orientation_6_bbox_transformed(self, import_env):
        """Photo raw 200×100 avec orientation 6 (90° CW) → repère corrigé 100×200."""
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg", size=(200, 100), orientation=6)
        # bbox raw : left 0.1*200=20, top 0.2*100=20, w 60, h 40
        _write_faces_ini(photos, "p1.jpg", [(0.1, 0.2, 0.4, 0.6, "aa01")])
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.faces_imported == 1
        conn = sqlite3.connect(face_db._db_path)
        try:
            x, y, w, h = conn.execute(
                "SELECT bbox_x, bbox_y, bbox_w, bbox_h FROM picasa_annotations"
            ).fetchone()
        finally:
            conn.close()
        # orientation 6 : x' = H - y - h = 100-20-40 = 40, y' = x = 20, swap w/h
        # (±1 px : l'encodage rect64 sur 16 bits + int() tronque)
        assert x == pytest.approx(40, abs=1)
        assert y == pytest.approx(20, abs=1)
        assert w == pytest.approx(40, abs=1)
        assert h == pytest.approx(60, abs=1)

    def test_progress_callback(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg")
        _write_faces_ini(photos, "p1.jpg", [(0.1, 0.1, 0.5, 0.9, "aa01")])
        calls = []
        pi._run_import(
            catalog, face_db, [str(photos)],
            progress_cb=lambda cur, tot: calls.append((cur, tot)),
        )
        assert calls == [(1, 1)]

    def test_no_ini_files(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.persons_created == 0
        assert result.photos_processed == 0

    def test_edits_imported_with_history_steps(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg", size=(200, 100))
        (photos / "picasa.ini").write_text(
            "[p1.jpg]\nrotate=1\nfilters=bw=1;\n", encoding="utf-8"
        )
        result = pi._run_import(
            catalog, face_db, [str(photos)], edit_db=edit_db
        )
        assert result.edits_imported == 1
        path_str = list(result.edited_map.keys())[0]
        final = result.edited_map[path_str]
        assert final.rotation == 90.0
        assert final.bw is True
        assert edit_db.has_edits(path_str)

    def test_existing_edits_not_overwritten(self, import_env):
        import os as _os
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg")
        path_str = _os.path.normpath(str(photos / "p1.jpg"))
        manual = EditInfo()
        manual.brightness = 0.9
        edit_db.save(path_str, manual, operation="manual")
        (photos / "picasa.ini").write_text(
            "[p1.jpg]\nrotate=1\n", encoding="utf-8"
        )
        result = pi._run_import(
            catalog, face_db, [str(photos)], edit_db=edit_db
        )
        assert result.edits_imported == 0
        assert edit_db.load(path_str).brightness == pytest.approx(0.9)

    def test_edits_skipped_without_edit_db(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        _make_image(photos / "p1.jpg")
        (photos / "picasa.ini").write_text(
            "[p1.jpg]\nrotate=1\n", encoding="utf-8"
        )
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.edits_imported == 0

    def test_global_contacts_used(self, import_env, tmp_path, monkeypatch):
        catalog, face_db, edit_db, photos = import_env
        cx = tmp_path / "contacts.xml"
        cx.write_bytes(
            b'<contacts><contact id="cc03" name="Carol"/></contacts>'
        )
        monkeypatch.setattr(pi, "find_contacts_xml", lambda: cx)
        _make_image(photos / "p1.jpg")
        hex_face = _enc_rect64(0.1, 0.1, 0.5, 0.9)
        (photos / "picasa.ini").write_text(
            f"[p1.jpg]\nfaces=rect64({hex_face}),cc03\n", encoding="utf-8"
        )
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.persons_created == 1
        assert result.faces_imported == 1
        assert {p.name for p in catalog.get_persons()} == {"Carol"}

    def test_corrupt_image_skipped(self, import_env):
        catalog, face_db, edit_db, photos = import_env
        (photos / "p1.jpg").write_bytes(b"pas une image")
        _write_faces_ini(photos, "p1.jpg", [(0.1, 0.1, 0.5, 0.9, "aa01")])
        result = pi._run_import(catalog, face_db, [str(photos)])
        assert result.photos_processed == 0
        assert result.faces_imported == 0
