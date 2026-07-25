# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de GeometryProcessor.transform_bboxes : vérifie que le recalage des
bboxes de visages (après enregistrement d'une photo retouchée, cf.
MainWindow._remap_face_bboxes_after_save) suit fidèlement la même géométrie
que les transformations réelles de pixels (apply_rotation/apply_flip/
apply_crop/apply_straighten_with_crop), en comparant à un marqueur peint dans
une image de synthèse et retrouvé par pixel-scanning après la transformation
réelle."""
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
    """La taille finale calculée par transform_bboxes doit correspondre à la
    taille réelle obtenue en appliquant la même séquence de transformations
    à une vraie image (rotation, redressement, flip, crop, dans cet ordre —
    même ordre que ImageAdjuster.apply_all)."""

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
        # Image 100x100, bbox de visage (30,40,10,8), crop = (0.2,0.2,0.5,0.5)
        # → fenêtre de crop en pixels (20,20)-(70,70).
        results, size = GeometryProcessor.transform_bboxes(
            [(30, 40, 10, 8)], (100, 100), crop=(0.2, 0.2, 0.5, 0.5))
        assert size == (50, 50)
        assert results[0] == (10, 20, 10, 8)  # (30-20, 40-20, 10, 8)

    def test_bbox_outside_crop_dropped(self):
        results, _size = GeometryProcessor.transform_bboxes(
            [(5, 5, 10, 10)], (100, 100), crop=(0.5, 0.5, 0.4, 0.4))
        assert results[0] is None

    def test_bbox_partially_inside_crop_clamped(self):
        # bbox chevauche le bord du crop : doit être clampée, pas droppée.
        results, size = GeometryProcessor.transform_bboxes(
            [(15, 15, 20, 20)], (100, 100), crop=(0.2, 0.2, 0.5, 0.5))
        assert size == (50, 50)
        x, y, w, h = results[0]
        assert x == 0 and y == 0
        assert w > 0 and h > 0


class Test90DegreeRotationExact:
    """apply_rotation utilise resample=NEAREST (par défaut) — pas de flou aux
    bords, donc le marqueur peint doit se retrouver pixel-exact dans l'image
    tournée."""

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
    """apply_straighten_with_crop utilise BICUBIC (flou aux bords du marqueur) :
    on ne peut pas exiger un match pixel-exact, mais le centre et la taille du
    marqueur retrouvé doivent rester proches (quelques px) de la prédiction."""

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

        # Comparaison tolérante (flou d'interpolation BICUBIC aux bords).
        pred_cx, pred_cy = pred[0] + pred[2] / 2.0, pred[1] + pred[3] / 2.0
        real_cx, real_cy = real_bbox[0] + real_bbox[2] / 2.0, real_bbox[1] + real_bbox[3] / 2.0
        assert abs(pred_cx - real_cx) <= 2.0
        assert abs(pred_cy - real_cy) <= 2.0
        assert abs(pred[2] - real_bbox[2]) <= 3
        assert abs(pred[3] - real_bbox[3]) <= 3


class TestPreRotationRoundTrip:
    """pre_rotation (detected_rotation) doit ramener une bbox exprimée dans le
    repère tourné vers le repère de base — sans aucune autre retouche, on doit
    exactement retrouver la bbox d'origine."""

    def test_undo_90_recovers_original_bbox(self):
        orig_size = (100, 60)
        orig_bbox = (20, 10, 15, 8)
        # Simule le repère de détection : image tournée de 90° CW.
        rotated_size = GeometryProcessor.apply_rotation(
            _canvas(*orig_size), 90.0).size
        detected_bbox_results, _ = GeometryProcessor.transform_bboxes(
            [orig_bbox], orig_size, rotation=90.0)
        detected_bbox = detected_bbox_results[0]

        # Repartir de detected_bbox dans rotated_size, annuler pre_rotation=90.
        results, size = GeometryProcessor.transform_bboxes(
            [detected_bbox], rotated_size, pre_rotation=90.0)
        assert size == orig_size
        assert results[0] == orig_bbox

    def test_no_op_when_nothing_changes(self):
        bbox = (10, 10, 20, 20)
        results, size = GeometryProcessor.transform_bboxes([bbox], (100, 100))
        assert size == (100, 100)
        assert results[0] == bbox
