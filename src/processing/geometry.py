# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import math

from PIL import Image, ImageOps


class GeometryProcessor:
    @staticmethod
    def apply_rotation(image: Image.Image, degrees: float) -> Image.Image:
        if degrees == 0.0:
            return image
        return image.rotate(-degrees, expand=True)

    @staticmethod
    def apply_straighten_with_crop(image: Image.Image, degrees: float) -> Image.Image:
        """Fine rotation + automatic crop to the largest inscribed rectangle of
        the same aspect ratio as the original (no black corners)."""
        if degrees == 0.0:
            return image
        W, H = image.size
        theta = math.radians(abs(degrees))
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        # s = ratio of the inscribed diagonal to the original rectangle
        s = min(W / (W * cos_t + H * sin_t),
                H / (W * sin_t + H * cos_t))
        W_crop = max(1, round(W * s))
        H_crop = max(1, round(H * s))
        rotated = image.rotate(-degrees, expand=True, resample=Image.BICUBIC)
        rW, rH = rotated.size
        left = (rW - W_crop) // 2
        top  = (rH - H_crop) // 2
        return rotated.crop((left, top, left + W_crop, top + H_crop))

    @staticmethod
    def apply_flip(image: Image.Image, flip_h: bool, flip_v: bool) -> Image.Image:
        if flip_h:
            image = ImageOps.mirror(image)
        if flip_v:
            image = ImageOps.flip(image)
        return image

    @staticmethod
    def apply_crop(image: Image.Image, crop: tuple) -> Image.Image:
        if len(crop) == 4:
            # Format rectangulaire classique : x, y, w, h (coords relatives 0-1)
            x, y, w, h = crop
            iw, ih = image.size
            left   = max(0, min(int(x * iw), iw))
            top    = max(0, min(int(y * ih), ih))
            right  = max(left + 1, min(int((x + w) * iw), iw))
            bottom = max(top  + 1, min(int((y + h) * ih), ih))
            return image.crop((left, top, right, bottom))

        if len(crop) == 8:
            # Quadrilateral format: x0,y0,x1,y1,x2,y2,x3,y3 (TL, TR, BR, BL)
            iw, ih = image.size
            pts = [(crop[i] * iw, crop[i + 1] * ih) for i in range(0, 8, 2)]
            tl, tr, br, bl = pts
            top_w  = math.hypot(tr[0] - tl[0], tr[1] - tl[1])
            bot_w  = math.hypot(br[0] - bl[0], br[1] - bl[1])
            left_h = math.hypot(bl[0] - tl[0], bl[1] - tl[1])
            rgt_h  = math.hypot(br[0] - tr[0], br[1] - tr[1])
            out_w  = max(1, int((top_w + bot_w) / 2))
            out_h  = max(1, int((left_h + rgt_h) / 2))
            # Perspective transform through OpenCV (preferred)
            try:
                import cv2
                import numpy as np
                src = np.float32([tl, tr, br, bl])
                dst = np.float32([(0, 0), (out_w, 0), (out_w, out_h), (0, out_h)])
                M   = cv2.getPerspectiveTransform(src, dst)
                arr = np.array(image.convert('RGB'))
                warped = cv2.warpPerspective(arr, M, (out_w, out_h))
                return Image.fromarray(warped)
            except ImportError:
                pass
            # PIL fallback with an analytical computation of the coefficients
            try:
                coeffs = GeometryProcessor._perspective_coeffs(tl, tr, br, bl, out_w, out_h)
                return image.convert('RGB').transform(
                    (out_w, out_h), Image.PERSPECTIVE, coeffs, Image.BICUBIC,
                )
            except Exception:
                pass
            # Dernier recours : bounding box
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return image.crop((int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))))

        return image

    @staticmethod
    def transform_bboxes(
        bboxes: list,
        size: tuple,
        rotation: float = 0.0,
        straighten: float = 0.0,
        flip_h: bool = False,
        flip_v: bool = False,
        crop=None,
        pre_rotation: float = 0.0,
    ) -> "tuple[list, tuple]":
        """Carries a list of bboxes (x, y, w, h in pixels) through the same
        geometric sequence as apply_rotation/apply_straighten_with_crop/
        apply_flip/apply_crop (in that order), without touching a single pixel —
        used to realign the face bboxes after an edited photo is saved
        (crop/rotation/straightening baked into the file).

        `size` is the size of the frame of reference the bboxes are currently
        expressed in. `pre_rotation` (CW, typically `FaceInfo.detected_rotation`)
        is applied in reverse first of all, to bring the bboxes back to the base
        frame (the EXIF-oriented photo, before any edit) before applying the
        normal editing sequence.

        Returns (a list of integer (x, y, w, h), or None if the bbox fell out of
        frame; the final size (w, h)). The quadrilateral crop case (8 values) is
        only approximated by the bounding box of the source quadrilateral, like
        the last resort of apply_crop itself.
        """
        W, H = float(size[0]), float(size[1])
        frame = [(0.0, 0.0), (W, 0.0), (W, H), (0.0, H)]
        polys = [frame] + [
            [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            for (x, y, w, h) in bboxes
        ]

        def _rotate(deg: float) -> None:
            nonlocal W, H, polys
            deg = deg % 360
            if deg == 0.0:
                return
            theta = math.radians(-deg)
            cos_a, sin_a = math.cos(theta), math.sin(theta)
            cx, cy = W / 2.0, H / 2.0
            rotated = [
                [
                    (
                        (x - cx) * cos_a + (y - cy) * sin_a,
                        -(x - cx) * sin_a + (y - cy) * cos_a,
                    )
                    for (x, y) in poly
                ]
                for poly in polys
            ]
            fxs = [p[0] for p in rotated[0]]
            fys = [p[1] for p in rotated[0]]
            new_w = round(max(fxs) - min(fxs))
            new_h = round(max(fys) - min(fys))
            ncx, ncy = new_w / 2.0, new_h / 2.0
            polys = [[(x + ncx, y + ncy) for (x, y) in poly] for poly in rotated]
            W, H = float(new_w), float(new_h)

        if pre_rotation % 360 != 0:
            _rotate(-pre_rotation)

        _rotate(rotation)

        if straighten != 0.0:
            theta = math.radians(abs(straighten))
            cos_t, sin_t = math.cos(theta), math.sin(theta)
            s = min(W / (W * cos_t + H * sin_t), H / (W * sin_t + H * cos_t))
            w_crop = max(1, round(W * s))
            h_crop = max(1, round(H * s))
            _rotate(straighten)
            left = (W - w_crop) / 2.0
            top  = (H - h_crop) / 2.0
            polys = [[(x - left, y - top) for (x, y) in poly] for poly in polys]
            W, H = float(w_crop), float(h_crop)

        if flip_h:
            polys = [[(W - x, y) for (x, y) in poly] for poly in polys]
        if flip_v:
            polys = [[(x, H - y) for (x, y) in poly] for poly in polys]

        if crop:
            iw, ih = int(round(W)), int(round(H))
            if len(crop) == 4:
                cx0, cy0, cw, ch = crop
                left   = max(0, min(int(cx0 * iw), iw))
                top    = max(0, min(int(cy0 * ih), ih))
                right  = max(left + 1, min(int((cx0 + cw) * iw), iw))
                bottom = max(top  + 1, min(int((cy0 + ch) * ih), ih))
            else:
                pts = [(crop[i] * iw, crop[i + 1] * ih) for i in range(0, 8, 2)]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                left, top = min(xs), min(ys)
                right, bottom = max(xs), max(ys)
            polys = [[(x - left, y - top) for (x, y) in poly] for poly in polys]
            W, H = float(right - left), float(bottom - top)

        final_w, final_h = max(1, int(round(W))), max(1, int(round(H)))
        result = []
        for poly in polys[1:]:
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            x0, x1 = max(0.0, min(xs)), min(float(final_w), max(xs))
            y0, y1 = max(0.0, min(ys)), min(float(final_h), max(ys))
            if x1 - x0 < 2.0 or y1 - y0 < 2.0:
                result.append(None)
            else:
                result.append((
                    int(round(x0)), int(round(y0)),
                    int(round(x1 - x0)), int(round(y1 - y0)),
                ))
        return result, (final_w, final_h)

    @staticmethod
    def _perspective_coeffs(tl, tr, br, bl, out_w, out_h):
        """PIL PERSPECTIVE coefficients for the inverse output→input mapping.

        PIL applies: x_in = (a*xo + b*yo + c) / (g*xo + h*yo + 1)
                     y_in = (d*xo + e*yo + f) / (g*xo + h*yo + 1)
        The output corners (0,0),(W,0),(W,H),(0,H) must map to tl,tr,br,bl.
        """
        x0, y0 = float(tl[0]), float(tl[1])
        x1, y1 = float(tr[0]), float(tr[1])
        x2, y2 = float(br[0]), float(br[1])
        x3, y3 = float(bl[0]), float(bl[1])
        W, H = float(out_w), float(out_h)
        # Solving the 2×2 system for g and h
        det = W * (x1 - x2) * H * (y3 - y2) - H * (x3 - x2) * W * (y1 - y2)
        if abs(det) < 1e-6:
            g, h = 0.0, 0.0
        else:
            rhs_x = x2 - x1 - x3 + x0
            rhs_y = y2 - y1 - y3 + y0
            g = (rhs_x * H * (y3 - y2) - H * (x3 - x2) * rhs_y) / det
            h = (W * (x1 - x2) * rhs_y - rhs_x * W * (y1 - y2)) / det
        a = x1 * g + (x1 - x0) / W
        b = x3 * h + (x3 - x0) / H
        c = x0
        d = y1 * g + (y1 - y0) / W
        e = y3 * h + (y3 - y0) / H
        f = y0
        return (a, b, c, d, e, f, g, h)
