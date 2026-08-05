# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/processing/frames.py (cadres décoratifs) : pur PIL/numpy,
aucune dépendance Qt.

Invariant central du produit, énoncé par la demande utilisateur : « les cadres
extérieurs n'empiètent pas sur l'image de la photo mais se positionnent
autour ». Tous les tests de ce module tournent autour de ça.
"""
import pytest
from PIL import Image

from src.core.models import EditInfo
from src.processing import frames
from src.processing.adjustments import ImageAdjuster


ALL_KINDS = [k for k, _ in frames.FRAME_TYPES if k != "none"]


def _photo(w=120, h=90, color=(20, 200, 40)):
    """Photo unie : toute couleur différente dans la zone contenu = empiètement."""
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
        # Le simple ignore l'intervalle et le cadre intérieur.
        e.frame_type = "simple"
        assert frames.border_fraction(e) == pytest.approx(0.05)

    def test_border_uses_the_short_side(self):
        e = _edit("simple", frame_width=0.10)
        assert frames.border_px(e, 400, 200) == 20   # 10 % de 200, pas de 400

    def test_total_fraction_is_capped(self):
        """Un réglage absurde ne doit pas noyer la photo sous le cadre."""
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
        """Balayage large : l'inverse doit être exact partout, pas seulement sur
        quelques tailles bien choisies (un pixel d'écart décale tous les outils)."""
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
        """Le cadre s'ajoute autour : la zone contenu reste pixel pour pixel la photo.

        Deux dérogations, toutes deux d'affichage et sans effet sur la géométrie :
        le second cadre de « plain » (frame_inner_enabled, cf. TestPlainInnerFrame),
        désactivé par défaut, et les débordements des trois cadres végétaux
        (`SPILL_FRAMES`, cf. TestSpill) — exclus du paramétrage ci-dessus."""
        photo = _photo()
        e = _edit(kind)
        out = frames.apply_frame(photo, e)
        b = frames.border_px(e, photo.width, photo.height)
        assert out.size == (photo.width + 2 * b, photo.height + 2 * b)
        content = out.crop((b, b, b + photo.width, b + photo.height))
        assert content.tobytes() == photo.convert(content.mode).tobytes()

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_border_is_actually_painted(self, kind):
        """Un cadre qui laisserait le bandeau vide (transparent/noir uni) serait
        un rendu raté silencieux — on vérifie qu'il y a bien de la matière."""
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
        """Une valeur inconnue en base (version future, saisie manuelle) ne doit
        pas faire échouer l'affichage de la photo."""
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
        # Milieu de la moulure haute : dominante rouge malgré le biseau.
        b = frames.border_px(e, photo.width, photo.height)
        r, g, bl = out.getpixel((out.width // 2, b // 2))
        assert r > g and r > bl

    def test_grayscale_photo_is_supported(self):
        """Les photos N&B (mode L) passent par le même chemin que les RGB."""
        out = frames.apply_frame(Image.new("L", (60, 60), 128), _edit("simple"))
        assert out.width > 60

    def test_preview_is_bounded(self):
        preview = frames.frame_preview(_photo(600, 400), _edit("roses"), size=80)
        assert max(preview.size) <= 80 + 2 * frames.border_px(_edit("roses"), 80, 80)


class TestSpill:
    """Débordements des trois cadres végétaux sur la photo.

    Seconde dérogation à l'invariant « le cadre s'ajoute autour », demandée pour
    le réalisme : une treille sculptée mord sur la toile. Comme le second cadre
    de « plain », c'est purement d'affichage — la géométrie dont dépendent les
    outils interactifs (recadrage, yeux rouges, visages, annotations) reste celle
    de la photo entière.
    """

    PHOTO = (400, 300)
    COLOR = (20, 200, 40)

    def _render(self, kind, **kw):
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = _edit(kind, frame_width=0.10, **kw)
        return frames.apply_frame(photo, e).convert("RGB"), e

    @staticmethod
    def _colors(img):
        """Histogramme {couleur: nombre de pixels} de la zone examinée."""
        return {color: n for n, color in img.getcolors(img.width * img.height)}

    def test_spill_frames_are_exactly_the_ones_with_a_spiller(self):
        assert frames.SPILL_FRAMES == set(frames._SPILLERS)
        assert frames.SPILL_FRAMES < set(ALL_KINDS)

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_motifs_reach_over_the_photo(self, kind):
        """Sans ça la fonctionnalité n'existe pas : la photo unie doit porter de
        la matière du cadre."""
        out, e = self._render(kind)
        b = frames.border_px(e, *self.PHOTO)
        content = out.crop((b, b, b + self.PHOTO[0], b + self.PHOTO[1]))
        hist = self._colors(content)
        covered = sum(n for color, n in hist.items() if color != self.COLOR)
        assert covered > 200, f"{kind} : {covered} pixels débordés"

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_geometry_is_untouched(self, kind):
        """Un débordement n'agrandit pas le canevas et ne décale pas le contenu."""
        out, e = self._render(kind)
        b = frames.border_px(e, *self.PHOTO)
        assert out.size == (self.PHOTO[0] + 2 * b, self.PHOTO[1] + 2 * b)
        assert frames.content_box(e, *out.size) == (b, b, 400.0, 300.0)

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_the_photo_keeps_its_centre(self, kind):
        """Les motifs restent accrochés au bandeau : ils mordent sur la bordure
        de l'image, ils ne l'envahissent pas. Marge de deux largeurs de bandeau,
        ombre portée comprise."""
        out, e = self._render(kind)
        b = frames.border_px(e, *self.PHOTO)
        m = b * 2
        centre = out.crop((b + m, b + m,
                           b + self.PHOTO[0] - m, b + self.PHOTO[1] - m))
        assert set(self._colors(centre)) == {self.COLOR}

    @pytest.mark.parametrize("kind", sorted(frames.SPILL_FRAMES))
    def test_rendering_is_deterministic(self, kind):
        """Deux rendus de la même photo doivent coïncider : l'aperçu de la
        galerie, la vignette de la grille et l'export montrent le même cadre."""
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
        assert set(self._colors(content)) == {self.COLOR}          # photo intacte
        assert len(self._colors(out.crop((0, 0, out.width, b)))) > 1  # bandeau peint

    def test_other_decorated_frames_never_spill(self):
        """Le débordement est réservé aux trois cadres végétaux : ailleurs, il
        rendrait illisible une frise géométrique (grecque, art déco…)."""
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
    """Le moteur de relief : profil de moulure, ornements gravés, éclairage.

    Ce qui distingue un cadre travaillé d'un aplat coloré n'est pas le dessin
    mais la LUMIÈRE — un motif plaqué en couleur reste plat. Ces tests vérifient
    donc des propriétés de rendu (variation le long de la moulure, sens de
    l'éclairage), pas la présence de telle ou telle fonction.
    """

    @pytest.mark.parametrize("kind", DECOR_KINDS)
    def test_ornaments_break_the_uniformity_along_the_moulding(self, kind):
        """Un motif décoratif doit varier LE LONG du bandeau.

        Une moulure sans ornement ne varie qu'avec la distance au bord : chaque
        ligne parallèle à un côté y est constante. Une frise sculptée casse cette
        constance — c'est la signature mesurable de l'ornement, et le test qui
        échouerait si un motif retombait sur un simple dégradé de bordure.
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
        """Le grain et l'usure sont tirés d'un générateur ensemencé : deux rendus
        de la même photo doivent être identiques, sinon l'aperçu de la galerie ne
        correspondrait pas à l'export."""
        photo = _photo(200, 150)
        e = _edit(kind, frame_width=0.12)
        assert (frames.apply_frame(photo, e).tobytes()
                == frames.apply_frame(photo, e).tobytes())

    def test_light_comes_from_above(self):
        """La moulure haute est plus claire que la moulure basse.

        C'est la convention qui fait qu'un relief se lit comme sortant de la
        surface plutôt que creusé ; inverser la lumière retournerait la
        perception de tous les cadres d'un coup."""
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
        """Un motif ajouté à FRAME_TYPES sans entrée dans _DECOR retomberait sur
        un aplat gris silencieux."""
        profile, material, amp = frames._DECOR[kind]
        assert profile in frames._PROFILE_LUTS
        assert material in frames._MATERIALS
        assert 0.0 < amp <= 1.0

    @pytest.mark.parametrize("name", sorted(frames._PROFILE_LUTS))
    def test_profiles_are_sampled_over_the_whole_band(self, name):
        ts, hs = frames._PROFILE_LUTS[name]
        assert ts[0] == pytest.approx(0.0) and ts[-1] == pytest.approx(1.0)
        assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))   # np.interp l'exige
        assert all(0.0 <= h <= 1.0 for h in hs)

    def test_painted_motifs_bring_their_own_colours(self):
        """Roses et fleurs sont peintes : leur bandeau ne peut pas être
        monochrome comme celui d'un cadre doré ou d'un bois."""
        photo = _photo(400, 300)
        e = _edit("flowers", frame_width=0.14)
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, photo.width, photo.height)
        hues = {out.getpixel((x, int(b * 0.55))) for x in range(b * 2, out.width - b * 2)}
        saturated = [c for c in hues if max(c) - min(c) > 60]
        assert len(saturated) > 20


class TestSuggestedWidth:
    """Un cadre sculpté n'existe pas sous une certaine épaisseur."""

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
    """« Entourage uni » : un aplat strict de la couleur demandée, sans relief."""

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
        """Le motif n'expose ni style de remplissage ni seconde couleur : les
        valeurs héritées d'un autre cadre ne doivent pas transparaître."""
        photo = _photo()
        e = _edit("plain", frame_color="#204080", frame_color2="#ff00ff",
                  frame_style="gradient", frame_width=0.08)
        out = frames.apply_frame(photo, e).convert("RGB")
        assert out.getpixel((0, 0)) == (0x20, 0x40, 0x80)

    def test_thickness_uses_frame_width_only(self):
        """Intervalle et cadre intérieur appartiennent au cadre double."""
        e = _edit("plain", frame_width=0.10, frame_gap=0.05, frame_inner_width=0.05)
        assert frames.border_fraction(e) == pytest.approx(0.10)

    def test_is_parametric_but_not_styled(self):
        assert "plain" in frames.PARAMETRIC_FRAMES
        assert "plain" not in frames.STYLED_FRAMES


class TestPlainInnerFrame:
    """Second cadre facultatif : peint SUR la photo, à distance du cadre extérieur.

    Demande utilisateur : « L'image est étendue par le cadre extérieur mais pas
    par dessus le cadre intérieur. Une partie de l'image est donc visible dans
    l'intervalle entre les deux cadres. »
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
        """Le motif reste un simple entourage tant que la case n'est pas cochée —
        c'est le seul cadre qui recouvre l'image, il ne s'active pas tout seul."""
        e = _edit("plain", frame_gap=0.04, frame_inner_width=0.02)
        assert e.frame_inner_enabled is False
        assert frames.inner_overlay_px(e, *self.PHOTO) == (0, 0)
        photo = _photo(*self.PHOTO, color=self.COLOR)
        out = frames.apply_frame(photo, e)
        b = frames.border_px(e, *self.PHOTO)
        assert out.crop((b, b, b + 400, b + 300)).tobytes() == photo.tobytes()

    def test_canvas_grows_only_by_the_outer_frame(self):
        """Le second cadre n'agrandit pas le canevas : la géométrie des outils
        interactifs (content_box) doit rester celle de la photo entière."""
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = self._edit_inner()
        out = frames.apply_frame(photo, e)
        b = frames.border_px(e, *self.PHOTO)
        assert out.size == (400 + 2 * b, 300 + 2 * b)
        assert frames.content_box(e, *out.size) == (b, b, 400.0, 300.0)

    def test_bands_alternate_frame_photo_frame(self):
        """Coupe horizontale : cadre extérieur, bande d'image, second cadre, image."""
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = self._edit_inner()
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, *self.PHOTO)
        gap, thick = frames.inner_overlay_px(e, *self.PHOTO)
        assert gap > 0 and thick > 0
        y = out.height // 2
        assert out.getpixel((b - 1, y)) == (0, 0, 0)            # cadre extérieur
        assert out.getpixel((b + gap - 1, y)) == self.COLOR     # image dans l'intervalle
        assert out.getpixel((b + gap, y)) == (0, 0, 0)          # second cadre
        assert out.getpixel((b + gap + thick, y)) == self.COLOR  # image

    def test_inner_frame_is_a_closed_ring(self):
        """Les quatre côtés sont peints, y compris en bas et à droite."""
        photo = _photo(*self.PHOTO, color=self.COLOR)
        e = self._edit_inner()
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, *self.PHOTO)
        gap, _thick = frames.inner_overlay_px(e, *self.PHOTO)
        cx, cy = b + 400 // 2, b + 300 // 2
        assert out.getpixel((cx, b + gap)) == (0, 0, 0)                  # haut
        assert out.getpixel((cx, b + 300 - 1 - gap)) == (0, 0, 0)        # bas
        assert out.getpixel((b + gap, cy)) == (0, 0, 0)                  # gauche
        assert out.getpixel((b + 400 - 1 - gap, cy)) == (0, 0, 0)        # droite

    def test_gap_and_thickness_are_independent(self):
        e_thin = self._edit_inner(frame_gap=0.02, frame_inner_width=0.01)
        e_wide = self._edit_inner(frame_gap=0.10, frame_inner_width=0.05)
        gap1, th1 = frames.inner_overlay_px(e_thin, *self.PHOTO)
        gap2, th2 = frames.inner_overlay_px(e_wide, *self.PHOTO)
        assert gap2 > gap1 and th2 > th1
        # ... et sans changer la taille finale, qui ne dépend que du cadre extérieur
        photo = _photo(*self.PHOTO, color=self.COLOR)
        assert (frames.apply_frame(photo, e_thin).size
                == frames.apply_frame(photo, e_wide).size)

    def test_absurd_settings_never_close_over_the_photo(self):
        """Intervalle + épaisseur cumulés restent bornés : la photo ne disparaît pas."""
        photo = _photo(120, 90)
        e = self._edit_inner(frame_gap=9.0, frame_inner_width=9.0)
        gap, thick = frames.inner_overlay_px(e, 120, 90)
        assert gap + thick <= 90 // 2
        out = frames.apply_frame(photo, e).convert("RGB")
        b = frames.border_px(e, 120, 90)
        assert out.getpixel((b + 60, b + 45)) == (20, 200, 40)   # centre : encore la photo

    def test_only_plain_gets_the_overlay(self):
        """Le drapeau est ignoré par les autres motifs (leur cadre intérieur est
        déjà dans le bandeau, hors de la photo).

        Témoins pris hors de `SPILL_FRAMES` : les cadres végétaux posent, eux,
        de la matière sur la photo — pour une tout autre raison (cf. TestSpill)."""
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
        """Fractions du petit côté : le second cadre existe encore sur un aperçu."""
        preview = frames.frame_preview(_photo(1200, 900), self._edit_inner(), size=160)
        assert preview.width > 160 * 0.5
        assert (0, 0, 0) in {preview.getpixel((x, preview.height // 2))
                             for x in range(preview.width)}


class TestInnerIronwork:
    """Ferronnerie du second cadre : volutes, rinceaux, torsade, clous forgés.

    Les ornements se développent VERS L'INTÉRIEUR depuis la ligne : ils restent
    donc dans la photo, ne touchent ni le bandeau du cadre extérieur ni la
    géométrie (`border_px`/`content_box`) dont dépendent les outils interactifs.
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

    # --- Accesseurs ---------------------------------------------------------

    def test_default_is_the_historical_line(self):
        """Le motif par défaut reste le simple trait : une base existante,
        migrée sans valeur, ne doit pas se retrouver ornementée."""
        assert frames.inner_motif(EditInfo()) == "line"
        assert frames.inner_relief(EditInfo()) is True
        assert frames.inner_ornament_scale(EditInfo()) == 1.0

    def test_unknown_motif_falls_back_to_line(self):
        """Valeur venue d'une version future ou saisie à la main en base."""
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
        assert "line" not in frames.ORNAMENTED_MOTIFS   # pas de curseur pour le trait

    # --- Rendu --------------------------------------------------------------

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_geometry_is_untouched(self, motif):
        """Un ornement n'agrandit pas le canevas et ne décale pas le contenu."""
        out, e = self._render(motif)
        b = frames.border_px(e, *self.PHOTO)
        assert out.size == (400 + 2 * b, 300 + 2 * b)
        assert frames.content_box(e, *out.size) == (b, b, 400.0, 300.0)

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_ornaments_stay_inside_the_photo(self, motif):
        """Le bandeau du cadre extérieur reste un aplat noir parfait : en relief,
        le liseré clair (gris) d'un ornement qui déborderait s'y verrait."""
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
        """Les ornements bordent la ligne, ils n'envahissent pas la photo."""
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
        """Les deux modes demandés doivent bien produire des images différentes."""
        relief, _ = self._render(motif, frame_inner_relief=True)
        flat, _ = self._render(motif, frame_inner_relief=False)
        assert relief.tobytes() != flat.tobytes(), motif

    def test_the_line_ignores_the_relief_setting(self):
        """Le trait historique reste un aplat strict : le relief est un réglage
        de ferronnerie. Une base migrée (frame_inner_relief à 1 par défaut) doit
        rendre exactement le même cadre qu'avant la fonctionnalité."""
        relief, _ = self._render("line", frame_inner_relief=True)
        flat, _ = self._render("line", frame_inner_relief=False)
        assert relief.tobytes() == flat.tobytes()

    def test_ornament_slider_changes_the_scale(self):
        small, _ = self._render("scrolls", frame_inner_ornament=frames.INNER_ORNAMENT_MIN)
        big, _ = self._render("scrolls", frame_inner_ornament=frames.INNER_ORNAMENT_MAX)
        assert small.tobytes() != big.tobytes()

    @pytest.mark.parametrize("motif", sorted(frames.ORNAMENTED_MOTIFS))
    def test_thumbnail_sized_render_does_not_crash(self, motif):
        """Aperçus de la galerie du dialogue : petites tailles, calque très réduit."""
        preview = frames.frame_preview(_photo(1200, 900), self._edit_iron(motif), size=120)
        assert preview.width > 60

    def test_layer_failure_falls_back_to_the_plain_ring(self, monkeypatch):
        """Un ornement raté ne doit jamais faire perdre le second cadre."""
        def boom(*a, **kw):
            raise ValueError("ornement cassé")

        monkeypatch.setattr(frames, "_inner_motif_layer", boom)
        out, e = self._render("scrolls")
        b = frames.border_px(e, *self.PHOTO)
        gap, _thick = frames.inner_overlay_px(e, *self.PHOTO)
        assert out.getpixel((b + 200, b + gap)) == (0, 0, 0)      # anneau haut
        assert out.getpixel((b + gap, b + 150)) == (0, 0, 0)      # anneau gauche


class TestApplyAllIntegration:
    def test_apply_all_adds_the_frame(self):
        photo = _photo()
        out = ImageAdjuster.apply_all(photo, _edit("simple"))
        assert out.size[0] > photo.width

    def test_with_frame_false_skips_it(self):
        """Chemin d'export : les annotations sont composées dans l'espace de la
        photo, donc AVANT la pose du cadre."""
        photo = _photo()
        out = ImageAdjuster.apply_all(photo, _edit("simple"), with_frame=False)
        assert out.size == photo.size

    def test_frame_alone_marks_the_edit_as_modified(self):
        assert EditInfo(frame_type="wood").is_modified()
        assert not EditInfo(frame_type="none").is_modified()
