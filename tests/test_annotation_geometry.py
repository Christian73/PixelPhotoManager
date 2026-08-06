# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import math

from src.processing.annotation_geometry import (
    catmull_rom_to_bezier_segments,
    distance_point_to_segment,
)


class TestDistancePointToSegment:
    def test_point_on_segment(self):
        assert distance_point_to_segment(5, 0, 0, 0, 10, 0) == 0.0

    def test_point_off_segment_perpendicular(self):
        assert distance_point_to_segment(5, 5, 0, 0, 10, 0) == 5.0

    def test_point_beyond_endpoint_clamps(self):
        # Le point projeté tomberait hors [0,10] -> distance à l'extrémité la plus proche
        assert distance_point_to_segment(20, 0, 0, 0, 10, 0) == 10.0
        assert distance_point_to_segment(-5, 0, 0, 0, 10, 0) == 5.0

    def test_degenerate_segment_is_point_distance(self):
        # x1,y1 == x2,y2 : pas de division par zéro, distance = distance au point
        d = distance_point_to_segment(3, 4, 0, 0, 0, 0)
        assert math.isclose(d, 5.0)


class TestCatmullRomToBezier:
    def test_empty_and_single_point(self):
        assert catmull_rom_to_bezier_segments([]) == []
        assert catmull_rom_to_bezier_segments([(0, 0)]) == []

    def test_two_points_returns_single_linear_segment(self):
        segments = catmull_rom_to_bezier_segments([(0, 0), (10, 0)])
        assert len(segments) == 1
        p0, cp1, cp2, p3 = segments[0]
        assert p0 == (0, 0)
        assert p3 == (10, 0)

    def test_passes_through_all_waypoints(self):
        points = [(0, 0), (5, 5), (10, 0), (15, 5)]
        segments = catmull_rom_to_bezier_segments(points)
        # Une spline à N points de passage produit N-1 segments, chacun
        # démarrant/finissant exactement sur un point de passage (propriété
        # d'interpolation de Catmull-Rom, contrairement à une simple courbe
        # d'approximation).
        assert len(segments) == len(points) - 1
        for i, (p0, _cp1, _cp2, p3) in enumerate(segments):
            assert p0 == points[i]
            assert p3 == points[i + 1]

    def test_collinear_points_stay_reasonably_straight(self):
        # Points régulièrement espacés sur une droite : les points de contrôle
        # ne doivent pas s'écarter de la droite (pas de boucle/rebroussement).
        points = [(0, 0), (10, 0), (20, 0), (30, 0)]
        segments = catmull_rom_to_bezier_segments(points)
        for p0, cp1, cp2, p3 in segments:
            assert math.isclose(cp1[1], 0.0, abs_tol=1e-6)
            assert math.isclose(cp2[1], 0.0, abs_tol=1e-6)
