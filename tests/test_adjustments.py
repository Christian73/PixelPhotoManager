# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/processing/adjustments.py (ImageAdjuster) : pur PIL/numpy,
aucune dépendance Qt. Images synthétiques via PIL.Image.new()."""
from PIL import Image, ImageStat

from src.core.models import EditInfo
from src.processing.adjustments import ImageAdjuster


def _gray_img(w=20, h=20, level=128):
    return Image.new("RGB", (w, h), (level, level, level))


def _mean(img):
    return tuple(ImageStat.Stat(img.convert("RGB")).mean)


class TestApplyBrightness:
    def test_positive_value_brightens(self):
        img = _gray_img(level=100)
        result = ImageAdjuster.apply_brightness(img, 0.5)
        assert _mean(result)[0] > 100

    def test_negative_value_darkens(self):
        img = _gray_img(level=100)
        result = ImageAdjuster.apply_brightness(img, -0.5)
        assert _mean(result)[0] < 100

    def test_factor_never_goes_negative(self):
        img = _gray_img(level=100)
        result = ImageAdjuster.apply_brightness(img, -10.0)
        assert _mean(result)[0] == 0


class TestApplyContrast:
    def test_increases_contrast_pushes_away_from_mean(self):
        img = Image.new("RGB", (2, 1))
        img.putpixel((0, 0), (100, 100, 100))
        img.putpixel((1, 0), (150, 150, 150))
        result = ImageAdjuster.apply_contrast(img, 1.0)
        p0 = result.getpixel((0, 0))[0]
        p1 = result.getpixel((1, 0))[0]
        assert p1 - p0 > 50


class TestApplySaturation:
    def test_zero_saturation_desaturates_toward_gray(self):
        img = Image.new("RGB", (1, 1), (200, 50, 50))
        result = ImageAdjuster.apply_saturation(img, -1.0)
        r, g, b = result.getpixel((0, 0))
        assert abs(r - g) < 5 and abs(g - b) < 5


class TestApplyGamma:
    def test_gamma_one_is_close_to_identity(self):
        img = _gray_img(level=128)
        result = ImageAdjuster.apply_gamma(img, 1.0)
        assert abs(_mean(result)[0] - 128) <= 1

    def test_gamma_below_one_darkens_midtones(self):
        img = _gray_img(level=128)
        result = ImageAdjuster.apply_gamma(img, 0.5)
        assert _mean(result)[0] < 128

    def test_gamma_zero_or_negative_does_not_crash(self):
        img = _gray_img(level=128)
        ImageAdjuster.apply_gamma(img, 0.0)  # clampé à 0.01 en interne


class TestApplyGammaCurve:
    def test_identity_curve_is_close_to_original(self):
        img = _gray_img(level=128)
        points = [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
        result = ImageAdjuster.apply_gamma_curve(img, points)
        assert abs(_mean(result)[0] - 128) <= 2

    def test_inverted_curve_darkens_bright_pixel(self):
        img = _gray_img(level=200)
        points = [(0.0, 1.0), (1.0, 0.0)]
        result = ImageAdjuster.apply_gamma_curve(img, points)
        assert _mean(result)[0] < 200

    def test_single_point_returns_identity_lut(self):
        lut = ImageAdjuster._curve_lut([(0.5, 0.5)])
        assert lut == list(range(256))


class TestApplySharpness:
    def test_does_not_crash_and_preserves_size(self):
        img = _gray_img(20, 20)
        result = ImageAdjuster.apply_sharpness(img, 0.5)
        assert result.size == img.size


class TestApplyNoiseReduction:
    def test_zero_value_is_noop(self):
        img = _gray_img()
        assert ImageAdjuster.apply_noise_reduction(img, 0.0) is img

    def test_positive_value_blurs_without_crashing(self):
        img = _gray_img(20, 20)
        result = ImageAdjuster.apply_noise_reduction(img, 1.0)
        assert result.size == img.size


class TestApplyColorChannels:
    def test_boost_red_channel_only(self):
        img = _gray_img(level=100)
        result = ImageAdjuster.apply_color_channels(img, 0.5, 0.0, 0.0)
        r, g, b = _mean(result)
        assert r > 100
        assert abs(g - 100) < 2
        assert abs(b - 100) < 2

    def test_neutral_values_are_noop(self):
        img = _gray_img(level=100)
        result = ImageAdjuster.apply_color_channels(img, 0.0, 0.0, 0.0)
        assert _mean(result) == (100.0, 100.0, 100.0)


class TestApplyRedEyeCorrection:
    def test_no_regions_is_noop(self):
        img = _gray_img()
        assert ImageAdjuster.apply_red_eye_correction(img, []) is img

    def test_red_region_desaturated(self):
        img = Image.new("RGB", (10, 10), (200, 20, 20))
        result = ImageAdjuster.apply_red_eye_correction(img, [(0.5, 0.5, 0.5)])
        r, g, b = result.getpixel((5, 5))
        assert r < 200


class TestApplyVignette:
    def test_black_vignette_darkens_corners(self):
        img = Image.new("RGB", (40, 40), (200, 200, 200))
        result = ImageAdjuster.apply_vignette(
            img, strength=1.0, cx=0.5, cy=0.5,
            rx1=0.1, ry1=0.1, rx2=0.3, ry2=0.3,
            angle=0.0, color="black",
        )
        corner = result.getpixel((0, 0))
        center = result.getpixel((20, 20))
        assert sum(corner) < sum(center)

    def test_white_vignette_brightens_corners(self):
        img = Image.new("RGB", (40, 40), (50, 50, 50))
        result = ImageAdjuster.apply_vignette(
            img, strength=1.0, cx=0.5, cy=0.5,
            rx1=0.1, ry1=0.1, rx2=0.3, ry2=0.3,
            angle=0.0, color="white",
        )
        corner = result.getpixel((0, 0))
        center = result.getpixel((20, 20))
        assert sum(corner) > sum(center)


class TestApplyBw:
    def test_produces_neutral_gray_channels(self):
        img = Image.new("RGB", (4, 4), (200, 50, 50))
        result = ImageAdjuster.apply_bw(img, 0.0, 0.0, 0.0)
        r, g, b = result.getpixel((0, 0))
        assert r == g == b


class TestApplyAll:
    def test_default_edit_info_returns_unmodified_looking_image(self):
        img = _gray_img(level=128)
        edit = EditInfo()
        result = ImageAdjuster.apply_all(img, edit)
        assert result.size == img.size
        assert abs(_mean(result)[0] - 128) <= 1

    def test_rotation_applied_before_crop(self):
        img = _gray_img(100, 60)
        edit = EditInfo(rotation=90.0)
        result = ImageAdjuster.apply_all(img, edit)
        assert result.size == (60, 100)

    def test_bw_applied_before_color_adjustments(self):
        img = Image.new("RGB", (4, 4), (200, 50, 50))
        edit = EditInfo(bw=True, brightness=0.2)
        result = ImageAdjuster.apply_all(img, edit)
        r, g, b = result.getpixel((0, 0))
        assert r == g == b
