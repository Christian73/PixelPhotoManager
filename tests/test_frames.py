# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of src/processing/frames.py (decorative frames): pure PIL/numpy,
no Qt dependency whatsoever.

Central invariant of the product, stated by the user request: "the outer
frames do not encroach on the image of the photo but position themselves
around it". Every test of this module revolves around that.
"""
import pytest
from PIL import Image

from src.core.models import EditInfo
from src.processing import frames
from src.processing.adjustments import ImageAdjuster


ALL_KINDS = [k for k, _ in frames.FRAME_TYPES if k != "none"]


def _photo(w=120, h=90, color=(20, 200, 40)):
    """Solid photo: any different colour in the content area = encroachment."""
    return Image.new("RGB", (w, h), color)


def _edit(kind, **kw):
    return EditInfo(frame_type=kind, **kw)


class TestGeometry:
    def test_no_frame_means_no_border(self):
        assert frames.border_fraction(EditInfo()) == 0.0
        assert frames.border_px(EditInfo(), 100, 100) == 0

    def test_double_border_sums_the_three_bands(self):
        e = _edit("double", frame_width=0.05, frame_gap=0.02, frame_inner_width=0.01)
        assert frames.border_fraction(e) == pytest.approx(0.08)
        # The simple frame ignores the gap and the inner frame.
        e.frame_type = "simple"
        assert frames.border_fraction(e) == pytest.approx(0.05)

    def test_border_uses_the_short_side(self):
        e = _edit("simple", frame_width=0.10)
        assert frames.border_px(e, 400, 200) == 20   # 10 % of 200, not of 400

    def test_total_fraction_is_capped(self):
        """An absurd setting must not drown the photo under the frame."""
        e = _edit("double", frame_width=5.0, frame_gap=5.0, frame_inner_width=5.0)
        assert frames.border_fraction(e) <= frames._MAX_FRACTION

    @pytest.mark.parametrize("size", [(120, 90), (90, 120), (100, 100), (400, 130)])
    def test_content_box_inverts_border_px(self, size):
        w, h = size
        e = _edit("double", frame_width=0.06, frame_gap=0.02, frame_inner_width=0.015)
        b = frames.border_px(e, w, h)
        x, y, cw, ch = frames.content_box(e, w + 2 * b, h + 2 * b)
        assert round(x) == b and round(y) == b
        assert round(cw) == w and round(ch) == h

    def test_content_box_inverts_border_px_over_many_sizes(self):
        """Wide sweep: the inverse must be exact everywhere, not only on
        a few well-chosen sizes (one pixel off shifts every tool)."""
        for fw in (0.005, 0.017, 0.05, 0.13, 0.25):
            e = _edit("simple", frame_width=fw)
            for w in range(40, 400, 37):
                for h in range(40, 400, 53):
                    b = frames.border_px(e, w, h)
                    x, y, cw, ch = frames.content_box(e, w + 2 * b, h + 2 * b)
                    assert (x, y, cw, ch) == (b, b, w, h), (fw, w, h, b)

    def test_content_box_without_frame_is_the_whole_image(self):
        assert frames.content_box(EditInfo(), 300, 200) == (0.0, 0.0, 300.0, 200.0)


class TestApplyFrame:
    @pytest.mark.parametrize("kind", [k for k in ALL_KINDS
                                      if k not in frames.SPILL_FRAMES])
    def test_photo_is_never_covered(self, kind):
        """The frame is added around: the content area stays pixel for pixel the photo.

        Two exceptions, both of display and with no effect on the geometry:
        the second frame of "plain" (frame_inner_enabled, cf. TestPlainInnerFrame),
        off by default, and the spills of the three foliage frames
        (`SPILL_FRAMES`, cf. TestSpill) - excluded from the parametrisation above."""
        photo = _photo()
        e = _edit(kind)
        out = frames.apply_frame(photo, e)
        b = frames.border_px(e, photo.width, photo.height)
        assert out.size == (photo.width + 2 * b, photo.height + 2 * b)
        content = out.crop((b, b, b + photo.width, b + photo.height))
        assert content.tobytes() == photo.convert(content.mode).tobytes()

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_border_is_actually_painted(self, kind):
        """A frame that left the band empty (transparent/solid black) would be
        a silently failed rendering - we check that there really is material there."""
        photo = _photo()
        out = frames.apply_frame(photo, _edit(kind)).convert("RGB")
        b = frames.border_px(_edit(kind), photo.width, photo.height)
        assert b >= 2
        band = [out.getpixel((x, y))
                for x in range(out.width) for y in range(b)]
        assert len(set(band)) > 1 or band[0] != (0, 0, 0)

    def test_none_returns_the_same_image(self):
        photo = _photo()
        assert frames.apply_frame(photo, EditInfo()) is photo

    def test_unknown_kind_is_ignored(self):
        """An unknown value in the database (future version, manual entry) must
        not make the display of the photo fail."""
        photo = _photo()
        out = frames.apply_frame(photo, _edit("licorne"))
        assert out.size == photo.size

    def test_parameters_change_the_thickness(self):
        photo = _photo()
        thin = frames.apply_frame(photo, _edit("simple", frame_width=0.02))
        thick = frames.apply_frame(photo, _edit("simple", frame_width=0.20))
        assert thick.width > thin.width > photo.width

    @pytest.mark.parametrize("style", [s for s, _ in frames.COLOR_STYLES])
    def test_color_styles_render(self, style):
        photo = _photo()
        out = frames.apply_frame(photo, _edit("simple", frame_style=style,
                                              frame_color="#ff0000",
                                              frame_color2="#0000ff"))
        assert out.size[0] > photo.width

    def test_solid_color_is_honoured(self):
        photo = _photo()
        e = _edit("simple", frame_style="solid", frame_color="#ff0000")
        out = frames.apply_frame(photo, e).convert("RGB")
        # Middle of the top moulding: red dominant despite the bevel.
        b = frames.border_px(e, photo.width, photo.height)
        r, g, bl = out.getpixel((out.width // 2, b // 2))
        assert r > g and r > bl

    def test_grayscale_photo_is_supported(self):
        """B&W photos (mode L) go through the same path as the RGB ones."""
        out = frames.apply_frame(Image.new("L", (60, 60), 128), _edit("simple"))
        assert out.width > 60

    def test_preview_is_bounded(self):
        preview = frames.frame_preview(_photo(600, 400), _edit("roses"), size=80)
        assert max(preview.size) <= 80 + 2 * frames.border_px(_edit("roses"), 80, 80)


class TestSpill:
    """Spills of the three foliage frames over the photo.

    The second exception to the invariant "the frame is added around", requested for
    realism: a carved trellis bites into the canvas. Like the second frame
    of "plain", it is purely a display matter - the geometry the interactive
    tools depend on (crop, red-eye, faces, annotations) stays that
    of the whole photo.
    """

    PHOTO = (400, 300)
    COLOR = (20, 200, 40)

    def _render(self, kind, **kw):
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = _edit(kind, frame_width=0.10, **kw)
        return frames.apply_frame(photo, e).convert("RGB"), e

    @staticmethod
    def _colors(img):
        """Histogram {colour: number of pixels} of the examined area."""
        return {color: n for n, color in img.getcolors(img.width * img.height)}

    def test_spill_frames_are_exactly_the_ones_with_a_spiller(self):
        assert frames.SPILL_FRAMES == set(frames._SPILLERS)
        assert frames.SPILL_FRAMES < set(ALL_KINDS)

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_motifs_reach_over_the_photo(self, kind):
        """Without that the feature does not exist: the solid photo must carry
        material from the frame."""
        out, e = self._render(kind)
        b = frames.border_px(e, *self.PHOTO)
        content = out.crop((b, b, b + self.PHOTO[0], b + self.PHOTO[1]))
        hist = self._colors(content)
        covered = sum(n for color, n in hist.items() if color != self.COLOR)
        assert covered > 200, f"{kind} : {covered} pixels débordés"

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_geometry_is_untouched(self, kind):
        """A spill does not enlarge the canvas and does not shift the content."""
        out, e = self._render(kind)
        b = frames.border_px(e, *self.PHOTO)
        assert out.size == (self.PHOTO[0] + 2 * b, self.PHOTO[1] + 2 * b)
        assert frames.content_box(e, *out.size) == (b, b, 400.0, 300.0)

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_the_photo_keeps_its_centre(self, kind):
        """The motifs stay attached to the band: they bite into the border
        of the image, they do not invade it. Margin of two band widths,
        drop shadow included."""
        out, e = self._render(kind)
        b = frames.border_px(e, *self.PHOTO)
        m = b * 2
        centre = out.crop((b + m, b + m,
                           b + self.PHOTO[0] - m, b + self.PHOTO[1] - m))
        assert set(self._colors(centre)) == {self.COLOR}

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_rendering_is_deterministic(self, kind):
        """Two renderings of the same photo must coincide: the preview of the
        gallery, the thumbnail of the grid and the export show the same frame."""
        first, _ = self._render(kind)
        second, _ = self._render(kind)
        assert first.tobytes() == second.tobytes()

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_failure_costs_the_spill_but_never_the_frame(self, kind, monkeypatch):
        def boom(*a, **kw):
            raise ValueError("débordement cassé")

        monkeypatch.setattr(frames, "_render_spill", boom)
        out, e = self._render(kind)
        b = frames.border_px(e, *self.PHOTO)
        assert out.size == (self.PHOTO[0] + 2 * b, self.PHOTO[1] + 2 * b)
        content = out.crop((b, b, b + self.PHOTO[0], b + self.PHOTO[1]))
        assert set(self._colors(content)) == {self.COLOR}          # photo untouched
        assert len(self._colors(out.crop((0, 0, out.width, b)))) > 1  # painted band

    def test_other_decorated_frames_never_spill(self):
        """The spill is reserved for the three foliage frames: elsewhere, it
        would make a geometric frieze (Greek key, art deco...) illegible."""
        for kind in ("baroque", "greek", "artdeco", "pearl"):
            out, e = self._render(kind)
            b = frames.border_px(e, *self.PHOTO)
            content = out.crop((b, b, b + self.PHOTO[0], b + self.PHOTO[1]))
            assert set(self._colors(content)) == {self.COLOR}, kind


DECOR_KINDS = [k for k in ALL_KINDS if k not in frames.PARAMETRIC_FRAMES]


def _luma(pixel) -> float:
    r, g, b = pixel[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _band_row(img, y: int, x0: int, x1: int) -> list:
    return [_luma(img.getpixel((x, y))) for x in range(x0, x1)]


def _stdev(values) -> float:
    n = len(values)
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / n) ** 0.5


class TestReliefEngine:
    """The relief engine: moulding profile, carved ornaments, lighting.

    What tells a carved frame from a flat colour fill is not the drawing
    but the LIGHT - a motif laid down in colour stays flat. These tests therefore check
    rendering properties (variation along the moulding, direction of
    the lighting), not the presence of this or that function.
    """

    @pytest.mark.parametrize("kind", DECOR_KINDS)
    def test_ornaments_break_the_uniformity_along_the_moulding(self, kind):
        """A decorative motif must vary ALONG the band.

        A moulding with no ornament only varies with the distance to the edge: every
        line parallel to a side is constant on it. A carved frieze breaks that
        constancy - it is the measurable signature of the ornament, and the test that
        would fail if a motif fell back to a simple border gradient.
        """
        photo = _photo(420, 320, (90, 90, 90))
        e = _edit(kind, frame_width=0.14)
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, photo.width, photo.height)
        x0, x1 = b * 2, out.width - b * 2
        best = max(_stdev(_band_row(out, int(b * f), x0, x1)) for f in (0.3, 0.5, 0.7))
        assert best > 3.0, f"{kind} : bandeau uniforme le long du côté ({best:.2f})"

    @pytest.mark.parametrize("kind", DECOR_KINDS)
    def test_rendering_is_deterministic(self, kind):
        """The grain and the wear are drawn from a seeded generator: two renderings
        of the same photo must be identical, otherwise the preview of the gallery would
        not match the export."""
        photo = _photo(200, 150)
        e = _edit(kind, frame_width=0.12)
        assert (frames.apply_frame(photo, e).tobytes()
                == frames.apply_frame(photo, e).tobytes())

    def test_light_comes_from_above(self):
        """The top moulding is lighter than the bottom moulding.

        That is the convention that makes a relief read as coming out of the
        surface rather than hollowed; reversing the light would flip the
        perception of every frame at once."""
        photo = _photo(400, 300, (60, 60, 60))
        e = _edit("simple", frame_color="#b0b0b0", frame_width=0.12)
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, photo.width, photo.height)
        x0, x1 = b * 3, out.width - b * 3
        top = sum(_band_row(out, int(b * 0.25), x0, x1))
        bottom = sum(_band_row(out, out.height - 1 - int(b * 0.25), x0, x1))
        assert top > bottom

    @pytest.mark.parametrize("kind", DECOR_KINDS)
    def test_every_decorative_kind_declares_a_profile_and_a_material(self, kind):
        """A motif added to FRAME_TYPES with no entry in _DECOR would fall back to
        a silent flat grey."""
        profile, material, amp = frames._DECOR[kind]
        assert profile in frames._PROFILE_LUTS
        assert material in frames._MATERIALS
        assert 0.0 < amp <= 1.0

    @pytest.mark.parametrize("name", sorted(frames._PROFILE_LUTS))
    def test_profiles_are_sampled_over_the_whole_band(self, name):
        ts, hs = frames._PROFILE_LUTS[name]
        assert ts[0] == pytest.approx(0.0) and ts[-1] == pytest.approx(1.0)
        assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))   # np.interp requires it
        assert all(0.0 <= h <= 1.0 for h in hs)

    def test_painted_motifs_bring_their_own_colours(self):
        """Roses and flowers are painted: their band cannot be
        monochrome like that of a gilded frame or of a wood."""
        photo = _photo(400, 300)
        e = _edit("flowers", frame_width=0.14)
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, photo.width, photo.height)
        hues = {out.getpixel((x, int(b * 0.55))) for x in range(b * 2, out.width - b * 2)}
        saturated = [c for c in hues if max(c) - min(c) > 60]
        assert len(saturated) > 20


class TestSuggestedWidth:
    """A carved frame does not exist below a certain thickness."""

    @pytest.mark.parametrize("kind", DECOR_KINDS)
    def test_decorative_kinds_get_a_floor(self, kind):
        assert frames.suggested_width(kind, 0.02) == frames.DECOR_MIN_WIDTH

    @pytest.mark.parametrize("kind", DECOR_KINDS)
    def test_a_wider_choice_is_never_reduced(self, kind):
        assert frames.suggested_width(kind, 0.25) == 0.25

    @pytest.mark.parametrize("kind", sorted(frames.PARAMETRIC_FRAMES) + ["none"])
    def test_adjustable_frames_keep_the_user_value(self, kind):
        assert frames.suggested_width(kind, 0.01) == 0.01


class TestPlainFrame:
    """"Plain surround": a strict flat fill of the requested colour, with no relief."""

    @pytest.mark.parametrize("hex_color,rgb", [
        ("#000000", (0, 0, 0)),
        ("#ffffff", (255, 255, 255)),
        ("#ff0000", (255, 0, 0)),
    ])
    def test_band_is_exactly_the_requested_color(self, hex_color, rgb):
        photo = _photo()
        e = _edit("plain", frame_color=hex_color, frame_width=0.10)
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, photo.width, photo.height)
        band = {out.getpixel((x, y))
                for x in range(out.width) for y in (0, b // 2, b - 1)}
        band |= {out.getpixel((x, out.height - 1 - y))
                 for x in range(out.width) for y in (0, b // 2, b - 1)}
        assert band == {rgb}, "aucun biseau ni liseré ne doit altérer la couleur"

    def test_style_and_second_color_are_ignored(self):
        """The motif exposes neither fill style nor second colour: the
        values inherited from another frame must not show through."""
        photo = _photo()
        e = _edit("plain", frame_color="#204080", frame_color2="#ff00ff",
                  frame_style="gradient", frame_width=0.08)
        out = frames.apply_frame(photo, e).convert("RGB")
        assert out.getpixel((0, 0)) == (0x20, 0x40, 0x80)

    def test_thickness_uses_frame_width_only(self):
        """Gap and inner frame belong to the double frame."""
        e = _edit("plain", frame_width=0.10, frame_gap=0.05, frame_inner_width=0.05)
        assert frames.border_fraction(e) == pytest.approx(0.10)

    def test_is_parametric_but_not_styled(self):
        assert "plain" in frames.PARAMETRIC_FRAMES
        assert "plain" not in frames.STYLED_FRAMES


class TestPlainInnerFrame:
    """Optional second frame: painted ON the photo, at a distance from the outer frame.

    User request: "The image is enlarged by the outer frame but not
    by the inner frame. Part of the image is therefore visible in
    the gap between the two frames."
    """

    PHOTO = (400, 300)
    COLOR = (20, 200, 40)

    def _edit_inner(self, **kw):
        params = dict(frame_color="#000000", frame_width=0.05,
                      frame_inner_enabled=True, frame_gap=0.04,
                      frame_inner_width=0.02)
        params.update(kw)
        return _edit("plain", **params)

    def test_disabled_by_default(self):
        """The motif stays a simple surround as long as the box is not ticked -
        it is the only frame that covers the image, it does not switch itself on."""
        e = _edit("plain", frame_gap=0.04, frame_inner_width=0.02)
        assert e.frame_inner_enabled is False
        assert frames.inner_overlay_px(e, *self.PHOTO) == (0, 0)
        photo = _photo(*self.PHOTO, color=self.COLOR)
        out = frames.apply_frame(photo, e)
        b = frames.border_px(e, *self.PHOTO)
        assert out.crop((b, b, b + 400, b + 300)).tobytes() == photo.tobytes()

    def test_canvas_grows_only_by_the_outer_frame(self):
        """The second frame does not enlarge the canvas: the geometry of the interactive
        tools (content_box) must stay that of the whole photo."""
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = self._edit_inner()
        out = frames.apply_frame(photo, e)
        b = frames.border_px(e, *self.PHOTO)
        assert out.size == (400 + 2 * b, 300 + 2 * b)
        assert frames.content_box(e, *out.size) == (b, b, 400.0, 300.0)

    def test_bands_alternate_frame_photo_frame(self):
        """Horizontal cut: outer frame, strip of image, second frame, image."""
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = self._edit_inner()
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, *self.PHOTO)
        gap, thick = frames.inner_overlay_px(e, *self.PHOTO)
        assert gap > 0 and thick > 0
        y = out.height // 2
        assert out.getpixel((b - 1, y)) == (0, 0, 0)            # outer frame
        assert out.getpixel((b + gap - 1, y)) == self.COLOR     # image in the gap
        assert out.getpixel((b + gap, y)) == (0, 0, 0)          # second frame
        assert out.getpixel((b + gap + thick, y)) == self.COLOR  # image

    def test_inner_frame_is_a_closed_ring(self):
        """All four sides are painted, including at the bottom and on the right."""
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = self._edit_inner()
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, *self.PHOTO)
        gap, _thick = frames.inner_overlay_px(e, *self.PHOTO)
        cx, cy = b + 400 // 2, b + 300 // 2
        assert out.getpixel((cx, b + gap)) == (0, 0, 0)                  # top
        assert out.getpixel((cx, b + 300 - 1 - gap)) == (0, 0, 0)        # bottom
        assert out.getpixel((b + gap, cy)) == (0, 0, 0)                  # left
        assert out.getpixel((b + 400 - 1 - gap, cy)) == (0, 0, 0)        # right

    def test_gap_and_thickness_are_independent(self):
        e_thin = self._edit_inner(frame_gap=0.02, frame_inner_width=0.01)
        e_wide = self._edit_inner(frame_gap=0.10, frame_inner_width=0.05)
        gap1, th1 = frames.inner_overlay_px(e_thin, *self.PHOTO)
        gap2, th2 = frames.inner_overlay_px(e_wide, *self.PHOTO)
        assert gap2 > gap1 and th2 > th1
        # ... and without changing the final size, which depends only on the outer frame
        photo = _photo(*self.PHOTO, color=self.COLOR)
        assert (frames.apply_frame(photo, e_thin).size
                == frames.apply_frame(photo, e_wide).size)

    def test_absurd_settings_never_close_over_the_photo(self):
        """Gap + cumulated thickness stay bounded: the photo does not disappear."""
        photo = _photo(120, 90)
        e = self._edit_inner(frame_gap=9.0, frame_inner_width=9.0)
        gap, thick = frames.inner_overlay_px(e, 120, 90)
        assert gap + thick <= 90 // 2
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, 120, 90)
        assert out.getpixel((b + 60, b + 45)) == (20, 200, 40)   # centre: still the photo

    def test_only_plain_gets_the_overlay(self):
        """The flag is ignored by the other motifs (their inner frame is
        already in the band, outside the photo).

        Witnesses taken outside `SPILL_FRAMES`: the foliage frames do lay
        material on the photo - for a completely different reason (cf. TestSpill)."""
        for kind in ("double", "simple", "baroque"):
            e = _edit(kind, frame_inner_enabled=True, frame_gap=0.04,
                      frame_inner_width=0.02)
            assert frames.inner_overlay_px(e, 400, 300) == (0, 0), kind
            photo = _photo(*self.PHOTO, color=self.COLOR)
            out = frames.apply_frame(photo, e)
            b = frames.border_px(e, *self.PHOTO)
            content = out.crop((b, b, b + 400, b + 300))
            assert content.tobytes() == photo.tobytes(), kind
        for kind in sorted(frames.SPILL_FRAMES):
            assert frames.inner_overlay_px(_edit(kind, frame_inner_enabled=True,
                                                frame_gap=0.04,
                                                frame_inner_width=0.02),
                                           400, 300) == (0, 0), kind

    def test_survives_a_thumbnail_sized_render(self):
        """Fractions of the short side: the second frame still exists on a preview."""
        preview = frames.frame_preview(_photo(1200, 900), self._edit_inner(), size=160)
        assert preview.width > 160 * 0.5
        assert (0, 0, 0) in {preview.getpixel((x, preview.height // 2))
                             for x in range(preview.width)}


class TestInnerIronwork:
    """Ironwork of the second frame: scrolls, running scrolls, twist, forged studs.

    The ornaments grow INWARDS from the line: they therefore stay
    inside the photo, touch neither the band of the outer frame nor the
    geometry (`border_px`/`content_box`) the interactive tools depend on.
    """

    PHOTO = (400, 300)
    COLOR = (20, 200, 40)

    def _edit_iron(self, motif, **kw):
        params = dict(frame_color="#000000", frame_width=0.05,
                      frame_inner_enabled=True, frame_gap=0.04,
                      frame_inner_width=0.015, frame_inner_motif=motif)
        params.update(kw)
        return _edit("plain", **params)

    def _render(self, motif, **kw):
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = self._edit_iron(motif, **kw)
        return frames.apply_frame(photo, e).convert("RGB"), e

    # --- Accessors ----------------------------------------------------------

    def test_default_is_the_historical_line(self):
        """The default motif stays the plain line: an existing database,
        migrated with no value, must not end up ornamented."""
        assert frames.inner_motif(EditInfo()) == "line"
        assert frames.inner_relief(EditInfo()) is True
        assert frames.inner_ornament_scale(EditInfo()) == 1.0

    def test_unknown_motif_falls_back_to_line(self):
        """A value coming from a future version or entered by hand in the database."""
        assert frames.inner_motif(_edit("plain", frame_inner_motif="licorne")) == "line"
        assert frames.inner_motif(_edit("plain", frame_inner_motif="")) == "line"
        unknown, _ = self._render("licorne")
        line, _ = self._render("line")
        assert unknown.tobytes() == line.tobytes()

    @pytest.mark.parametrize("value,expected", [
        (0.0, frames.INNER_ORNAMENT_MIN),
        (99.0, frames.INNER_ORNAMENT_MAX),
        (1.4, 1.4),
        ("abc", 1.0),
        (None, 1.0),
    ])
    def test_ornament_scale_is_clamped(self, value, expected):
        e = _edit("plain")
        e.frame_inner_ornament = value
        assert frames.inner_ornament_scale(e) == pytest.approx(expected)

    def test_ornamented_motifs_are_a_subset_of_the_motifs(self):
        assert frames.ORNAMENTED_MOTIFS < set(frames.INNER_MOTIF_LABELS)
        assert "line" not in frames.ORNAMENTED_MOTIFS   # no slider for the line

    # --- Rendering ----------------------------------------------------------

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_geometry_is_untouched(self, motif):
        """An ornament does not enlarge the canvas and does not shift the content."""
        out, e = self._render(motif)
        b = frames.border_px(e, *self.PHOTO)
        assert out.size == (400 + 2 * b, 300 + 2 * b)
        assert frames.content_box(e, *out.size) == (b, b, 400.0, 300.0)

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_ornaments_stay_inside_the_photo(self, motif):
        """The band of the outer frame stays a perfect flat black: in relief,
        the light (grey) fillet of an ornament that overflowed would show on it."""
        out, e = self._render(motif, frame_inner_ornament=frames.INNER_ORNAMENT_MAX)
        b = frames.border_px(e, *self.PHOTO)
        band = {out.getpixel((x, y))
                for x in range(out.width) for y in range(b)}
        band |= {out.getpixel((x, out.height - 1 - y))
                 for x in range(out.width) for y in range(b)}
        band |= {out.getpixel((x, y))
                 for y in range(out.height) for x in range(b)}
        assert band == {(0, 0, 0)}

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_photo_centre_is_left_alone(self, motif):
        """The ornaments border the line, they do not invade the photo."""
        out, e = self._render(motif, frame_inner_ornament=frames.INNER_ORNAMENT_MAX)
        b = frames.border_px(e, *self.PHOTO)
        assert out.getpixel((b + 200, b + 150)) == self.COLOR

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_ornaments_add_matter_to_the_line(self, motif):
        line, _ = self._render("line")
        iron, _ = self._render(motif)
        assert iron.tobytes() != line.tobytes(), motif

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_relief_and_flat_are_two_distinct_renderings(self, motif):
        """The two requested modes must indeed produce different images."""
        relief, _ = self._render(motif, frame_inner_relief=True)
        flat, _ = self._render(motif, frame_inner_relief=False)
        assert relief.tobytes() != flat.tobytes(), motif

    def test_the_line_ignores_the_relief_setting(self):
        """The historical line stays a strict flat fill: the relief is an ironwork
        setting. A migrated database (frame_inner_relief at 1 by default) must
        render exactly the same frame as before the feature."""
        relief, _ = self._render("line", frame_inner_relief=True)
        flat, _ = self._render("line", frame_inner_relief=False)
        assert relief.tobytes() == flat.tobytes()

    def test_ornament_slider_changes_the_scale(self):
        small, _ = self._render("scrolls", frame_inner_ornament=frames.INNER_ORNAMENT_MIN)
        big, _ = self._render("scrolls", frame_inner_ornament=frames.INNER_ORNAMENT_MAX)
        assert small.tobytes() != big.tobytes()

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_thumbnail_sized_render_does_not_crash(self, motif):
        """Previews of the gallery of the dialog: small sizes, very reduced layer."""
        preview = frames.frame_preview(_photo(1200, 900), self._edit_iron(motif), size=120)
        assert preview.width > 60

    def test_layer_failure_falls_back_to_the_plain_ring(self, monkeypatch):
        """A failed ornament must never make the second frame be lost."""
        def boom(*a, **kw):
            raise ValueError("ornement cassé")

        monkeypatch.setattr(frames, "_inner_motif_layer", boom)
        out, e = self._render("scrolls")
        b = frames.border_px(e, *self.PHOTO)
        gap, _thick = frames.inner_overlay_px(e, *self.PHOTO)
        assert out.getpixel((b + 200, b + gap)) == (0, 0, 0)      # top ring
        assert out.getpixel((b + gap, b + 150)) == (0, 0, 0)      # left ring


class TestApplyAllIntegration:
    def test_apply_all_adds_the_frame(self):
        photo = _photo()
        out = ImageAdjuster.apply_all(photo, _edit("simple"))
        assert out.size[0] > photo.width

    def test_with_frame_false_skips_it(self):
        """Export path: the annotations are composited in the space of the
        photo, hence BEFORE the frame is laid down."""
        photo = _photo()
        out = ImageAdjuster.apply_all(photo, _edit("simple"), with_frame=False)
        assert out.size == photo.size

    def test_frame_alone_marks_the_edit_as_modified(self):
        assert EditInfo(frame_type="wood").is_modified()
        assert not EditInfo(frame_type="none").is_modified()
