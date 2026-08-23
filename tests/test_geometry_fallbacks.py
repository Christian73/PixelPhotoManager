# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Complement to test_geometry.py: the fallback branches of the quadrilateral
crop (cv2 unavailable -> PIL transformation, PIL failure -> bounding box) and
`_perspective_coeffs` (mapping of the corners, degenerate case)."""
import sys

import pytest
from PIL import Image

from src.processing.geometry import GeometryProcessor


def _img(w=100, h=100):
    return Image.new("RGB", (w, h), (200, 40, 40))


_QUAD = (0.1, 0.1, 0.9, 0.15, 0.85, 0.9, 0.05, 0.85)  # TL,TR,BR,BL not aligned


class TestQuadCropFallbacks:
    def test_pil_fallback_without_cv2(self, monkeypatch):
        """cv2 absent -> PIL PERSPECTIVE transformation."""
        monkeypatch.setitem(sys.modules, "cv2", None)  # import cv2 -> ImportError
        img = _img()
        result = GeometryProcessor.apply_crop(img, _QUAD)
        w, h = result.size
        assert w > 1 and h > 1

    def test_bounding_box_last_resort(self, monkeypatch):
        """cv2 absent AND PIL failure -> bounding box of the 4 corners."""
        monkeypatch.setitem(sys.modules, "cv2", None)
        monkeypatch.setattr(
            GeometryProcessor,
            "_perspective_coeffs",
            staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        img = _img(100, 100)
        result = GeometryProcessor.apply_crop(img, _QUAD)
        # bbox: x from 0.05*100=5 to 0.9*100=90, y from 0.1*100=10 to 0.9*100=90
        assert result.size == (85, 80)

    def test_pil_and_cv2_agree_on_size(self, monkeypatch):
        img = _img()
        with_cv2 = GeometryProcessor.apply_crop(img, _QUAD)
        monkeypatch.setitem(sys.modules, "cv2", None)
        without_cv2 = GeometryProcessor.apply_crop(img, _QUAD)
        assert with_cv2.size == without_cv2.size


class TestPerspectiveCoeffs:
    def test_axis_aligned_rectangle_maps_corners(self):
        """For an upright rectangle, the mapping must send each output corner onto
        the corresponding input corner."""
        tl, tr, br, bl = (10, 20), (90, 20), (90, 80), (10, 80)
        W, H = 80, 60
        a, b, c, d, e, f, g, h = GeometryProcessor._perspective_coeffs(
            tl, tr, br, bl, W, H
        )

        def _map(xo, yo):
            denom = g * xo + h * yo + 1.0
            return (
                (a * xo + b * yo + c) / denom,
                (d * xo + e * yo + f) / denom,
            )

        assert _map(0, 0) == pytest.approx(tl)
        assert _map(W, 0) == pytest.approx(tr)
        assert _map(W, H) == pytest.approx(br)
        assert _map(0, H) == pytest.approx(bl)

    def test_general_quadrilateral_maps_corners(self):
        tl, tr, br, bl = (5, 8), (95, 15), (88, 92), (12, 85)
        W, H = 90, 80
        a, b, c, d, e, f, g, h = GeometryProcessor._perspective_coeffs(
            tl, tr, br, bl, W, H
        )

        def _map(xo, yo):
            denom = g * xo + h * yo + 1.0
            return (
                (a * xo + b * yo + c) / denom,
                (d * xo + e * yo + f) / denom,
            )

        assert _map(0, 0) == pytest.approx(tl, abs=1e-6)
        assert _map(W, 0) == pytest.approx(tr, abs=1e-6)
        assert _map(W, H) == pytest.approx(br, abs=1e-6)
        assert _map(0, H) == pytest.approx(bl, abs=1e-6)

    def test_degenerate_points_do_not_crash(self):
        coeffs = GeometryProcessor._perspective_coeffs(
            (50, 50), (50, 50), (50, 50), (50, 50), 100, 100
        )
        assert len(coeffs) == 8
        assert all(isinstance(v, float) for v in coeffs)
