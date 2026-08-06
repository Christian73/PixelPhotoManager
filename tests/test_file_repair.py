# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/library/file_repair.py : réparation de JPEG corrompus.

Deux scénarios de corruption synthétique :
- EOI manquant (transfert interrompu en toute fin de fichier) → doit être
  récupéré par le niveau 1 (_decode_strict_with_eoi_fix), sans perte.
- Troncature en plein milieu (comme _make_corrupted_jpeg de
  tools/test_env/generate_library.py) → le niveau 1 échoue, le niveau 2
  (meilleur des décodeurs tolérants) doit quand même produire un résultat.
"""
import io

import numpy as np
from PIL import Image

from src.library import file_repair


def _make_valid_jpeg_bytes(size=(64, 64), seed=0) -> bytes:
    rng = np.random.default_rng(seed)
    img = Image.fromarray(rng.integers(0, 256, size=(*size, 3), dtype=np.uint8), mode="RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class TestDecodeStrictWithEoiFix:
    def test_missing_eoi_is_recovered_losslessly(self, tmp_path):
        data = _make_valid_jpeg_bytes()
        assert data.endswith(file_repair._JPEG_EOI)
        truncated = data[: -len(file_repair._JPEG_EOI)]
        path = tmp_path / "missing_eoi.jpg"
        path.write_bytes(truncated)

        img = file_repair._decode_strict_with_eoi_fix(str(path))

        assert img is not None
        with Image.open(io.BytesIO(data)) as original:
            assert np.array_equal(np.asarray(img), np.asarray(original.convert("RGB")))

    def test_file_already_ending_with_eoi_returns_none(self, tmp_path):
        data = _make_valid_jpeg_bytes()
        path = tmp_path / "intact.jpg"
        path.write_bytes(data)

        assert file_repair._decode_strict_with_eoi_fix(str(path)) is None

    def test_mid_file_truncation_is_not_recovered_losslessly(self, tmp_path):
        """libjpeg peut « réussir » un décodage strict même sur un fichier
        tronqué en plein milieu une fois l'EOI ajouté (fin de flux entropique
        traitée comme récupérable) — dans ce cas le résultat doit comporter
        des lignes de filler détectables, donc ne pas être traité comme
        sans perte par _try_repair_file (cf. test_try_repair_file.py)."""
        data = _make_valid_jpeg_bytes()
        path = tmp_path / "truncated.jpg"
        path.write_bytes(data[: max(64, len(data) // 4)])

        img = file_repair._decode_strict_with_eoi_fix(str(path))
        if img is not None:
            assert file_repair._usable_height(img) < img.size[1]

    def test_non_jpeg_data_returns_none(self, tmp_path):
        path = tmp_path / "not_a_jpeg.jpg"
        path.write_bytes(b"not a jpeg at all")

        assert file_repair._decode_strict_with_eoi_fix(str(path)) is None


class TestUsableHeight:
    def test_full_content_scores_full_height(self):
        rng = np.random.default_rng(1)
        img = Image.fromarray(rng.integers(0, 256, size=(40, 20, 3), dtype=np.uint8), mode="RGB")

        assert file_repair._usable_height(img) == 40

    def test_flat_bottom_rows_reduce_score(self):
        rng = np.random.default_rng(2)
        arr = rng.integers(0, 256, size=(40, 20, 3), dtype=np.uint8)
        arr[25:] = 128  # moitié basse remplie d'une couleur unie (filler du décodeur tolérant)
        img = Image.fromarray(arr, mode="RGB")

        assert file_repair._usable_height(img) == 25

    def test_fully_flat_image_scores_zero(self):
        arr = np.full((10, 10, 3), 200, dtype=np.uint8)
        img = Image.fromarray(arr, mode="RGB")

        assert file_repair._usable_height(img) == 0


class TestTryRepairFile:
    def test_missing_eoi_repair_round_trip(self, tmp_path):
        data = _make_valid_jpeg_bytes()
        truncated = data[: -len(file_repair._JPEG_EOI)]
        path = tmp_path / "photo.jpg"
        path.write_bytes(truncated)

        assert file_repair._try_repair_file(str(path)) is True

        with Image.open(path) as repaired, Image.open(io.BytesIO(data)) as original:
            diff = np.abs(
                np.asarray(repaired.convert("RGB")).astype(int)
                - np.asarray(original.convert("RGB")).astype(int)
            )
            assert diff.mean() < 6.0  # ré-encodage JPEG qualité 95, quasi-identique

        backups = list((tmp_path / ".tmp_originals").glob("photo_*.jpg"))
        assert len(backups) == 1

    def test_mid_file_truncation_still_repairs_via_tolerant_decoder(self, tmp_path):
        data = _make_valid_jpeg_bytes(size=(200, 200))
        path = tmp_path / "photo.jpg"
        path.write_bytes(data[: max(64, len(data) // 4)])

        assert file_repair._try_repair_file(str(path)) is True

        with Image.open(path) as repaired:
            assert repaired.size[0] > 0 and repaired.size[1] > 0

    def test_mid_file_truncation_does_not_take_the_lossless_fast_path(self, tmp_path, monkeypatch):
        """Un décodage strict-après-EOI qui « réussit » avec des lignes de
        filler ne doit pas court-circuiter la comparaison du niveau 2 :
        _save_repaired ne doit être appelé qu'une seule fois, avec le
        meilleur candidat, jamais avec le résultat du niveau 1 seul."""
        data = _make_valid_jpeg_bytes(size=(200, 200))
        path = tmp_path / "photo.jpg"
        path.write_bytes(data[: max(64, len(data) // 4)])

        calls = []
        orig_save_repaired = file_repair._save_repaired

        def spy_save_repaired(p, img, orig_stat):
            calls.append(img)
            return orig_save_repaired(p, img, orig_stat)

        monkeypatch.setattr(file_repair, "_save_repaired", spy_save_repaired)

        assert file_repair._try_repair_file(str(path)) is True
        assert len(calls) == 1

    def test_unrecoverable_file_returns_false(self, tmp_path):
        path = tmp_path / "garbage.jpg"
        path.write_bytes(b"\xff\xd8" + b"not a real jpeg stream" * 5)

        assert file_repair._try_repair_file(str(path)) is False

    def test_missing_file_returns_false_instead_of_raising(self, tmp_path):
        path = tmp_path / "gone.jpg"  # jamais créé

        assert file_repair._try_repair_file(str(path)) is False

    def test_best_of_n_prefers_higher_scoring_decoder(self, tmp_path, monkeypatch):
        rng = np.random.default_rng(3)
        good = Image.fromarray(rng.integers(0, 256, size=(10, 10, 3), dtype=np.uint8), mode="RGB")
        bad = Image.fromarray(np.zeros((10, 10, 3), dtype=np.uint8), mode="RGB")

        monkeypatch.setattr(file_repair, "_decode_strict_with_eoi_fix", lambda p: None)
        monkeypatch.setattr(file_repair, "_decode_truncated_pil", lambda p: bad)
        monkeypatch.setattr(file_repair, "_decode_qimage", lambda p: good)
        monkeypatch.setattr(file_repair, "_decode_cv2_truncated", lambda p: None)

        saved = {}

        def fake_save_repaired(path, img, orig_stat):
            saved["img"] = img
            return True

        monkeypatch.setattr(file_repair, "_save_repaired", fake_save_repaired)

        path = tmp_path / "whatever.jpg"
        path.write_bytes(b"\xff\xd8\xff\xd9")

        assert file_repair._try_repair_file(str(path)) is True
        assert saved["img"] is good


class TestFileRepairThread:
    def test_unexpected_exception_on_one_file_does_not_abort_the_batch(self, tmp_path, monkeypatch, qtbot):
        """Régression : un fichier disparu entre la détection et la
        réparation (ou toute autre erreur imprévue) levait une exception non
        rattrapée dans run(), qui interrompait le thread avant d'émettre
        `finished` — la boîte de progression restait bloquée indéfiniment."""
        good_path = tmp_path / "good.jpg"
        good_path.write_bytes(_make_valid_jpeg_bytes()[: -len(file_repair._JPEG_EOI)])
        missing_path = str(tmp_path / "missing.jpg")  # jamais créé

        thread = file_repair.FileRepairThread([missing_path, str(good_path)])
        with qtbot.waitSignal(thread.finished, timeout=5000) as blocker:
            thread.start()

        repaired_count, still_failed = blocker.args
        assert repaired_count == 1
        assert still_failed == [missing_path]
