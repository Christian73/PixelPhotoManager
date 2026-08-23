# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of GeometryProcessor.transform_bboxes: checks that the realignment
of the face bboxes (after saving an edited photo, cf.
MainWindow._remap_face_bboxes_after_save) faithfully follows the same geometry
as the real pixel transformations (apply_rotation/apply_flip/apply_crop/
apply_straighten_with_crop), by comparing with a marker painted into a
synthetic image and found again by pixel scanning after the real
transformation."""
import numpy as np
from PIL import Image, ImageDraw

from src.processing.geometry import GeometryProcessor


def _canvas(w, h, bg=(0, 0, 0)):
    return Image.new("RGB", (w, h), bg)


def _paint_marker(img, bbox, color=(0, 255, 0)):
    x, y, w, h = bbox
    ImageDraw.Draw(img).rectangle([x, y, x + w - 1, y + h - 1], fill=color)
    return img


def _marker_bbox(img, color=(0, 255, 0)):
    arr = np.array(img.convert("RGB"))
    mask = np.all(arr == color, axis=-1)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return (int(xs.min()), int(ys.min()),
            int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))


class TestFinalSizeMatchesRealPipeline:
    """The final size computed by transform_bboxes must match the real size
    obtained by applying the same sequence of transformations to a real image
    (rotation, straightening, flip, crop, in that order -- the same order as
    ImageAdjuster.apply_all)."""

    def _real_final_size(self, w, h, rotation, straighten, flip_h, flip_v, crop):
        img = _canvas(w, h)
        img = GeometryProcessor.apply_rotation(img, rotation)
        if straighten:
            img = GeometryProcessor.apply_straighten_with_crop(img, straighten)
        img = GeometryProcessor.apply_flip(img, flip_h, flip_v)
        if crop:
            img = GeometryProcessor.apply_crop(img, crop)
        return img.size

    def test_rotation_only(self):
        real = self._real_final_size(200, 100, 90.0, 0.0, False, False, None)
        _, size = GeometryProcessor.transform_bboxes(
            [], (200, 100), rotation=90.0)
        assert size == real

    def test_crop_only(self):
        real = self._real_final_size(200, 100, 0.0, 0.0, False, False,
                                      (0.25, 0.25, 0.5, 0.5))
        _, size = GeometryProcessor.transform_bboxes(
            [], (200, 100), crop=(0.25, 0.25, 0.5, 0.5))
        assert size == real

    def test_rotation_and_crop(self):
        real = self._real_final_size(200, 100, 180.0, 0.0, True, False,
                                      (0.1, 0.1, 0.6, 0.6))
        _, size = GeometryProcessor.transform_bboxes(
            [], (200, 100), rotation=180.0, flip_h=True,
            crop=(0.1, 0.1, 0.6, 0.6))
        assert size == real


class TestPureCropExact:
    def test_bbox_recomputed_exactly(self):
        # 100x100 image, face bbox (30,40,10,8), crop = (0.2,0.2,0.5,0.5)
        # -> crop window in pixels (20,20)-(70,70).
        results, size = GeometryProcessor.transform_bboxes(
            [(30, 40, 10, 8)], (100, 100), crop=(0.2, 0.2, 0.5, 0.5))
        assert size == (50, 50)
        assert results[0] == (10, 20, 10, 8)  # (30-20, 40-20, 10, 8)

    def test_bbox_outside_crop_dropped(self):
        results, _size = GeometryProcessor.transform_bboxes(
            [(5, 5, 10, 10)], (100, 100), crop=(0.5, 0.5, 0.4, 0.4))
        assert results[0] is None

    def test_bbox_partially_inside_crop_clamped(self):
        # the bbox overlaps the edge of the crop: it must be clamped, not dropped.
        results, size = GeometryProcessor.transform_bboxes(
            [(15, 15, 20, 20)], (100, 100), crop=(0.2, 0.2, 0.5, 0.5))
        assert size == (50, 50)
        x, y, w, h = results[0]
        assert x == 0 and y == 0
        assert w > 0 and h > 0


class Test90DegreeRotationExact:
    """apply_rotation uses resample=NEAREST (the default) -- no blur at the
    edges, so the painted marker must be found pixel-exact in the rotated
    image."""

    def test_marker_bbox_matches_after_90(self):
        bbox = (20, 10, 15, 8)
        img = _paint_marker(_canvas(100, 60), bbox)
        rotated = GeometryProcessor.apply_rotation(img, 90.0)
        real_bbox = _marker_bbox(rotated)

        results, size = GeometryProcessor.transform_bboxes(
            [bbox], (100, 60), rotation=90.0)
        assert size == rotated.size
        assert results[0] == real_bbox

    def test_marker_bbox_matches_after_270(self):
        bbox = (5, 5, 12, 20)
        img = _paint_marker(_canvas(100, 60), bbox)
        rotated = GeometryProcessor.apply_rotation(img, 270.0)
        real_bbox = _marker_bbox(rotated)

        results, size = GeometryProcessor.transform_bboxes(
            [bbox], (100, 60), rotation=270.0)
        assert size == rotated.size
        assert results[0] == real_bbox

    def test_marker_bbox_matches_after_180(self):
        bbox = (40, 15, 10, 10)
        img = _paint_marker(_canvas(100, 60), bbox)
        rotated = GeometryProcessor.apply_rotation(img, 180.0)
        real_bbox = _marker_bbox(rotated)

        results, size = GeometryProcessor.transform_bboxes(
            [bbox], (100, 60), rotation=180.0)
        assert size == rotated.size
        assert results[0] == real_bbox


class TestFlipExact:
    def test_marker_bbox_matches_after_flip_h(self):
        bbox = (10, 5, 20, 10)
        img = _paint_marker(_canvas(100, 60), bbox)
        flipped = GeometryProcessor.apply_flip(img, True, False)
        real_bbox = _marker_bbox(flipped)

        results, size = GeometryProcessor.transform_bboxes(
            [bbox], (100, 60), flip_h=True)
        assert size == (100, 60)
        assert results[0] == real_bbox

    def test_marker_bbox_matches_after_flip_v(self):
        bbox = (10, 5, 20, 10)
        img = _paint_marker(_canvas(100, 60), bbox)
        flipped = GeometryProcessor.apply_flip(img, False, True)
        real_bbox = _marker_bbox(flipped)

        results, size = GeometryProcessor.transform_bboxes(
            [bbox], (100, 60), flip_v=True)
        assert results[0] == real_bbox


class TestStraightenApprox:
    """apply_straighten_with_crop uses BICUBIC (blur at the edges of the
    marker): a pixel-exact match cannot be required, but the centre and the size
    of the marker found again must stay close (a few px) to the prediction."""

    def test_marker_bbox_close_after_straighten(self):
        bbox = (30, 20, 20, 15)
        img = _paint_marker(_canvas(120, 90), bbox)
        straightened = GeometryProcessor.apply_straighten_with_crop(img, 5.0)
        real_bbox = _marker_bbox(straightened)
        assert real_bbox is not None

        results, size = GeometryProcessor.transform_bboxes(
            [bbox], (120, 90), straighten=5.0)
        assert size == straightened.size
        pred = results[0]
        assert pred is not None

        # Tolerant comparison (BICUBIC interpolation blur at the edges).
        pred_cx, pred_cy = pred[0] + pred[2] / 2.0, pred[1] + pred[3] / 2.0
        real_cx, real_cy = real_bbox[0] + real_bbox[2] / 2.0, real_bbox[1] + real_bbox[3] / 2.0
        assert abs(pred_cx - real_cx) <= 2.0
        assert abs(pred_cy - real_cy) <= 2.0
        assert abs(pred[2] - real_bbox[2]) <= 3
        assert abs(pred[3] - real_bbox[3]) <= 3


class TestPreRotationRoundTrip:
    """pre_rotation (detected_rotation) must bring a bbox expressed in the
    rotated reference back to the base reference -- with no other edit, the
    original bbox must be found back exactly."""

    def test_undo_90_recovers_original_bbox(self):
        orig_size = (100, 60)
        orig_bbox = (20, 10, 15, 8)
        # Simulates the detection reference: image rotated by 90 degrees CW.
        rotated_size = GeometryProcessor.apply_rotation(
            _canvas(*orig_size), 90.0).size
        detected_bbox_results, _ = GeometryProcessor.transform_bboxes(
            [orig_bbox], orig_size, rotation=90.0)
        detected_bbox = detected_bbox_results[0]

        # Start again from detected_bbox in rotated_size, undo pre_rotation=90.
        results, size = GeometryProcessor.transform_bboxes(
            [detected_bbox], rotated_size, pre_rotation=90.0)
        assert size == orig_size
        assert results[0] == orig_bbox

    def test_no_op_when_nothing_changes(self):
        bbox = (10, 10, 20, 20)
        results, size = GeometryProcessor.transform_bboxes([bbox], (100, 100))
        assert size == (100, 100)
        assert results[0] == bbox
