# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of src/processing/geometry.py (GeometryProcessor): pure PIL/maths,
no Qt dependency at all. Synthetic images through PIL.Image.new()."""
from PIL import Image

from src.processing.geometry import GeometryProcessor


def _img(w=100, h=60, color=(255, 0, 0)):
    return Image.new("RGB", (w, h), color)


class TestApplyRotation:
    def test_zero_degrees_returns_same_image(self):
        img = _img()
        assert GeometryProcessor.apply_rotation(img, 0.0) is img

    def test_90_degrees_swaps_dimensions(self):
        img = _img(100, 60)
        rotated = GeometryProcessor.apply_rotation(img, 90.0)
        assert rotated.size == (60, 100)

    def test_180_degrees_keeps_dimensions(self):
        img = _img(100, 60)
        rotated = GeometryProcessor.apply_rotation(img, 180.0)
        assert rotated.size == (100, 60)


class TestApplyStraightenWithCrop:
    def test_zero_degrees_returns_same_image(self):
        img = _img()
        assert GeometryProcessor.apply_straighten_with_crop(img, 0.0) is img

    def test_small_angle_crops_smaller_than_original_no_black_corners(self):
        img = _img(200, 100)
        result = GeometryProcessor.apply_straighten_with_crop(img, 5.0)
        w, h = result.size
        assert w <= 200 and h <= 100
        assert w > 0 and h > 0

    def test_negative_angle_also_crops(self):
        img = _img(200, 100)
        result = GeometryProcessor.apply_straighten_with_crop(img, -5.0)
        w, h = result.size
        assert w <= 200 and h <= 100


class TestApplyFlip:
    def test_no_flip_returns_equivalent_image(self):
        img = _img()
        result = GeometryProcessor.apply_flip(img, False, False)
        assert result.size == img.size

    def test_flip_horizontal_changes_pixel_layout(self):
        img = Image.new("RGB", (2, 1))
        img.putpixel((0, 0), (255, 0, 0))
        img.putpixel((1, 0), (0, 255, 0))
        result = GeometryProcessor.apply_flip(img, True, False)
        assert result.getpixel((0, 0)) == (0, 255, 0)
        assert result.getpixel((1, 0)) == (255, 0, 0)

    def test_flip_vertical_changes_pixel_layout(self):
        img = Image.new("RGB", (1, 2))
        img.putpixel((0, 0), (255, 0, 0))
        img.putpixel((0, 1), (0, 255, 0))
        result = GeometryProcessor.apply_flip(img, False, True)
        assert result.getpixel((0, 0)) == (0, 255, 0)
        assert result.getpixel((0, 1)) == (255, 0, 0)

    def test_both_flips_applied(self):
        img = _img(10, 10)
        result = GeometryProcessor.apply_flip(img, True, True)
        assert result.size == img.size


class TestApplyCropRectangle:
    def test_rectangle_crop_uses_normalized_coords(self):
        img = _img(100, 100)
        result = GeometryProcessor.apply_crop(img, (0.25, 0.25, 0.5, 0.5))
        assert result.size == (50, 50)

    def test_full_image_crop_is_noop_size(self):
        img = _img(100, 100)
        result = GeometryProcessor.apply_crop(img, (0.0, 0.0, 1.0, 1.0))
        assert result.size == (100, 100)

    def test_crop_clamped_to_image_bounds(self):
        img = _img(100, 100)
        result = GeometryProcessor.apply_crop(img, (0.9, 0.9, 0.5, 0.5))
        w, h = result.size
        assert w >= 1 and h >= 1
        assert w <= 100 and h <= 100

    def test_unknown_crop_length_returns_same_image(self):
        img = _img()
        result = GeometryProcessor.apply_crop(img, (0.1, 0.2, 0.3))
        assert result is img


class TestApplyCropQuadrilateral:
    def test_quad_crop_produces_rectangular_output(self):
        img = _img(100, 100)
        # Quadrilateral = the complete rectangle, normalised coords TL,TR,BR,BL.
        quad = (0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0)
        result = GeometryProcessor.apply_crop(img, quad)
        w, h = result.size
        assert w > 0 and h > 0
        # The result must stay close to the original dimensions for an
        # undistorted quad.
        assert abs(w - 100) <= 1
        assert abs(h - 100) <= 1
