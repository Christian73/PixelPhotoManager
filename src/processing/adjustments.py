# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import math

from PIL import Image, ImageEnhance, ImageFilter
from src.core.models import EditInfo
from src.processing.geometry import GeometryProcessor


class ImageAdjuster:
    @staticmethod
    def apply_all(image: Image.Image, edit: EditInfo) -> Image.Image:
        image = GeometryProcessor.apply_rotation(image, edit.rotation)
        if edit.straighten != 0.0:
            image = GeometryProcessor.apply_straighten_with_crop(image, edit.straighten)
        image = GeometryProcessor.apply_flip(image, edit.flip_h, edit.flip_v)
        if edit.crop:
            image = GeometryProcessor.apply_crop(image, edit.crop)

        if edit.bw:
            image = ImageAdjuster.apply_bw(image, edit.bw_red, edit.bw_green, edit.bw_blue)

        if edit.brightness != 0.0:
            image = ImageAdjuster.apply_brightness(image, edit.brightness)
        if edit.contrast != 0.0:
            image = ImageAdjuster.apply_contrast(image, edit.contrast)
        if edit.saturation != 0.0:
            image = ImageAdjuster.apply_saturation(image, edit.saturation)
        if edit.color_red != 0.0 or edit.color_green != 0.0 or edit.color_blue != 0.0:
            image = ImageAdjuster.apply_color_channels(image, edit.color_red, edit.color_green, edit.color_blue)
        if edit.gamma_use_curve:
            image = ImageAdjuster.apply_gamma_curve(image, edit.gamma_curve_points)
        elif edit.gamma != 1.0:
            image = ImageAdjuster.apply_gamma(image, edit.gamma)
        if edit.sharpness > 0.0:
            image = ImageAdjuster.apply_sharpness(image, edit.sharpness)
        if edit.noise_reduction > 0.0:
            image = ImageAdjuster.apply_noise_reduction(image, edit.noise_reduction)
        if edit.red_eye_regions:
            image = ImageAdjuster.apply_red_eye_correction(image, edit.red_eye_regions)
        if edit.vignette_strength > 0.0:
            image = ImageAdjuster.apply_vignette(
                image,
                edit.vignette_strength,
                edit.vignette_cx,
                edit.vignette_cy,
                edit.vignette_rx1,
                edit.vignette_ry1,
                edit.vignette_rx2,
                edit.vignette_ry2,
                edit.vignette_angle,
                edit.vignette_color,
            )

        return image

    @staticmethod
    def apply_brightness(image: Image.Image, value: float) -> Image.Image:
        factor = 1.0 + value
        return ImageEnhance.Brightness(image).enhance(max(0.0, factor))

    @staticmethod
    def apply_contrast(image: Image.Image, value: float) -> Image.Image:
        factor = 1.0 + value
        return ImageEnhance.Contrast(image).enhance(max(0.0, factor))

    @staticmethod
    def apply_saturation(image: Image.Image, value: float) -> Image.Image:
        factor = 1.0 + value
        return ImageEnhance.Color(image).enhance(max(0.0, factor))

    @staticmethod
    def apply_gamma(image: Image.Image, gamma: float) -> Image.Image:
        if gamma <= 0:
            gamma = 0.01
        import array
        lut = array.array("B", [
            int(min(255, (i / 255.0) ** (1.0 / gamma) * 255))
            for i in range(256)
        ])
        if image.mode == "RGB":
            lut_full = list(lut) * 3
        elif image.mode == "RGBA":
            lut_full = list(lut) * 3 + list(range(256))
        elif image.mode == "L":
            lut_full = list(lut)
        else:
            image = image.convert("RGB")
            lut_full = list(lut) * 3
        return image.point(lut_full)

    @staticmethod
    def _curve_lut(points) -> list:
        """Compute 256-entry LUT from control points (monotone cubic spline)."""
        seen: dict[float, float] = {}
        for pt in points:
            x, y = max(0.0, min(1.0, float(pt[0]))), max(0.0, min(1.0, float(pt[1])))
            if x not in seen:
                seen[x] = y
        pts = sorted(seen.items())

        if len(pts) < 2:
            return list(range(256))

        if pts[0][0] > 0.0:
            pts.insert(0, (0.0, pts[0][1]))
        if pts[-1][0] < 1.0:
            pts.append((1.0, pts[-1][1]))

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        n = len(xs)
        h = [xs[i + 1] - xs[i] for i in range(n - 1)]
        delta = [(ys[i + 1] - ys[i]) / h[i] if h[i] > 1e-10 else 0.0 for i in range(n - 1)]

        m = [0.0] * n
        m[0] = delta[0]
        m[n - 1] = delta[-1]
        for i in range(1, n - 1):
            m[i] = 0.0 if delta[i - 1] * delta[i] <= 0 else (delta[i - 1] + delta[i]) / 2.0

        for i in range(n - 1):
            if abs(delta[i]) < 1e-10:
                m[i] = m[i + 1] = 0.0
                continue
            alpha, beta = m[i] / delta[i], m[i + 1] / delta[i]
            s = alpha * alpha + beta * beta
            if s > 9.0:
                tau = 3.0 / math.sqrt(s)
                m[i] = tau * alpha * delta[i]
                m[i + 1] = tau * beta * delta[i]

        lut = []
        for k in range(256):
            t = k / 255.0
            seg = n - 2
            for i in range(n - 1):
                if t <= xs[i + 1]:
                    seg = i
                    break
            dx = xs[seg + 1] - xs[seg]
            if dx < 1e-10:
                y = ys[seg]
            else:
                u = (t - xs[seg]) / dx
                u2, u3 = u * u, u * u * u
                y = (ys[seg] * (2 * u3 - 3 * u2 + 1)
                     + m[seg] * dx * (u3 - 2 * u2 + u)
                     + ys[seg + 1] * (-2 * u3 + 3 * u2)
                     + m[seg + 1] * dx * (u3 - u2))
            lut.append(int(max(0, min(255, round(y * 255)))))
        return lut

    @staticmethod
    def apply_gamma_curve(image: Image.Image, points) -> Image.Image:
        lut = ImageAdjuster._curve_lut(points)
        if image.mode == "RGB":
            lut_full = lut * 3
        elif image.mode == "RGBA":
            lut_full = lut * 3 + list(range(256))
        elif image.mode == "L":
            lut_full = lut
        else:
            image = image.convert("RGB")
            lut_full = lut * 3
        return image.point(lut_full)

    @staticmethod
    def apply_sharpness(image: Image.Image, value: float) -> Image.Image:
        # value 0..1 → factor 1..2
        factor = 1.0 + value
        return ImageEnhance.Sharpness(image).enhance(factor)

    @staticmethod
    def apply_noise_reduction(image: Image.Image, value: float) -> Image.Image:
        if value <= 0:
            return image
        radius = value * 2.0
        return image.filter(ImageFilter.GaussianBlur(radius=radius))

    @staticmethod
    def apply_color_channels(image: Image.Image, red: float, green: float, blue: float) -> Image.Image:
        """Ajuste chaque canal indépendamment. Valeurs en [-1, 1] : 0 = neutre."""
        import array
        def _lut(v):
            return array.array("B", [int(min(255, max(0, i * (1.0 + v)))) for i in range(256)])
        lut_full = list(_lut(red)) + list(_lut(green)) + list(_lut(blue))
        img = image.convert("RGB") if image.mode != "RGB" else image
        return img.point(lut_full)

    @staticmethod
    def apply_red_eye_correction(image: Image.Image, regions: list) -> Image.Image:
        """Corrige les yeux rouges dans les zones circulaires définies (coords normalisées 0-1)."""
        import numpy as np
        if not regions:
            return image
        img = image.convert("RGB")
        arr = np.array(img, dtype=np.int16)
        h, w = arr.shape[:2]
        ys, xs = np.mgrid[0:h, 0:w]
        for region in regions:
            cx, cy, r = region[0], region[1], region[2]
            px, py = cx * w, cy * h
            pr = max(1.0, r * min(w, h))
            circle = ((xs - px) ** 2 + (ys - py) ** 2) <= pr ** 2
            r_ch = arr[:, :, 0].astype(np.float32)
            g_ch = arr[:, :, 1].astype(np.float32)
            b_ch = arr[:, :, 2].astype(np.float32)
            red_mask = circle & (r_ch > 80) & (r_ch > g_ch * 1.3) & (r_ch > b_ch * 1.3)
            lum = ((g_ch + b_ch) / 2.0).clip(0, 255)
            arr[:, :, 0] = np.where(red_mask, lum.astype(np.int16), arr[:, :, 0])
        return Image.fromarray(arr.clip(0, 255).astype(np.uint8))

    @staticmethod
    def apply_vignette(
        image: Image.Image,
        strength: float,
        cx: float,
        cy: float,
        rx1: float,
        ry1: float,
        rx2: float,
        ry2: float,
        angle: float,
        color: str,
    ) -> Image.Image:
        """Vignette à double ellipse interactive avec dégradé entre ellipse interne et externe.

        strength : 0–1 — intensité maximale de la vignette
        cx, cy   : 0–1 — centre normalisé (0-1 des dimensions de l'image)
        rx1, ry1 : rayons de l'ellipse interne (1.0 = demi-dimension de l'image)
        rx2, ry2 : rayons de l'ellipse externe
        angle    : rotation en degrés
        color    : "black" ou "white"
        """
        import numpy as np

        img = image.convert("RGB")
        arr = np.array(img, dtype=np.float32)
        h, w = arr.shape[:2]

        px_cx  = cx * w
        px_cy  = cy * h
        px_rx1 = rx1 * (w / 2.0)
        px_ry1 = ry1 * (h / 2.0)
        px_rx2 = rx2 * (w / 2.0)
        px_ry2 = ry2 * (h / 2.0)

        Y, X = np.ogrid[:h, :w]
        dx = (X - px_cx).astype(np.float32)
        dy = (Y - px_cy).astype(np.float32)

        # Rotation inverse pour aligner avec les axes de l'ellipse
        rad = math.radians(angle)
        cos_a = math.cos(-rad)
        sin_a = math.sin(-rad)
        dx_rot = dx * cos_a - dy * sin_a
        dy_rot = dx * sin_a + dy * cos_a

        eps = 1e-6
        theta = np.arctan2(dy_rot, dx_rot)
        cos_t = np.cos(theta)
        sin_t = np.sin(theta)

        # Distance du pixel au centre (dans l'espace tourné)
        r_pix = np.sqrt(dx_rot ** 2 + dy_rot ** 2)

        # Rayon de l'ellipse dans la direction de chaque pixel (intersection rayon-ellipse)
        r_inner = (px_rx1 * px_ry1) / np.sqrt(
            (px_ry1 * cos_t) ** 2 + (px_rx1 * sin_t) ** 2 + eps)
        r_outer = (px_rx2 * px_ry2) / np.sqrt(
            (px_ry2 * cos_t) ** 2 + (px_rx2 * sin_t) ** 2 + eps)

        gap = np.maximum(r_outer - r_inner, eps)
        t = np.clip((r_pix - r_inner) / gap, 0.0, 1.0)

        # Smoothstep pour une courbe en S naturelle
        alpha = t * t * (3.0 - 2.0 * t)
        alpha = (alpha * strength)[:, :, np.newaxis]

        target = np.array([255.0, 255.0, 255.0]) if color == "white" else np.array([0.0, 0.0, 0.0])
        result = arr * (1.0 - alpha) + target * alpha

        return Image.fromarray(np.clip(result, 0, 255).astype(np.uint8))

    @staticmethod
    def apply_bw(image: Image.Image, red: float, green: float, blue: float) -> Image.Image:
        import numpy as np

        rgb = image.convert("RGB")
        arr = np.array(rgb, dtype=np.float32)

        base_r, base_g, base_b = 0.299, 0.587, 0.114
        wr = max(0.0, base_r + red * 0.3)
        wg = max(0.0, base_g + green * 0.3)
        wb = max(0.0, base_b + blue * 0.3)
        total = wr + wg + wb
        if total == 0:
            total = 1.0
        wr, wg, wb = wr / total, wg / total, wb / total

        gray = arr[:, :, 0] * wr + arr[:, :, 1] * wg + arr[:, :, 2] * wb
        gray = np.clip(gray, 0, 255).astype(np.uint8)
        gray_img = Image.fromarray(gray, mode="L")
        return gray_img.convert("RGB")
