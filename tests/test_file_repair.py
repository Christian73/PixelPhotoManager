# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of src/library/file_repair.py: repairing corrupted JPEGs.

Two synthetic corruption scenarios:
- missing EOI (transfer interrupted right at the end of the file) -> must be
  recovered by level 1 (_decode_strict_with_eoi_fix), without loss.
- truncation right in the middle (like _make_corrupted_jpeg in
  tools/test_env/generate_library.py) -> level 1 fails, level 2
  (best of the tolerant decoders) must still produce a result.
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
        """libjpeg may "succeed" at a strict decoding even on a file truncated
        right in the middle once the EOI has been added (the end of the entropy
        stream being treated as recoverable) -- in that case the result must
        contain detectable filler lines, hence must not be treated as lossless
        by _try_repair_file (cf. test_try_repair_file.py)."""
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
        arr[25:] = 128  # lower half filled with a solid colour (filler of the tolerant decoder)
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
            assert diff.mean() < 6.0  # JPEG re-encoding at quality 95, nearly identical

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
        """A strict-after-EOI decoding that "succeeds" with filler lines must not
        short-circuit the comparison of level 2: _save_repaired must be called
        only once, with the best candidate, never with the result of level 1
        alone."""
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
        path = tmp_path / "gone.jpg"  # never created

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
        """Regression: a file gone between the detection and the repair (or any
        other unforeseen error) raised an exception that was not caught in
        run(), which interrupted the thread before it emitted `finished` -- the
        progress box stayed stuck indefinitely."""
        good_path = tmp_path / "good.jpg"
        good_path.write_bytes(_make_valid_jpeg_bytes()[: -len(file_repair._JPEG_EOI)])
        missing_path = str(tmp_path / "missing.jpg")  # never created

        thread = file_repair.FileRepairThread([missing_path, str(good_path)])
        with qtbot.waitSignal(thread.finished, timeout=5000) as blocker:
            thread.start()

        repaired_count, still_failed = blocker.args
        assert repaired_count == 1
        assert still_failed == [missing_path]
