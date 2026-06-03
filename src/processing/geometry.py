import math

from PIL import Image, ImageOps


class GeometryProcessor:
    @staticmethod
    def apply_rotation(image: Image.Image, degrees: float) -> Image.Image:
        if degrees == 0.0:
            return image
        return image.rotate(-degrees, expand=True)

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
            # Format quadrilatère : x0,y0,x1,y1,x2,y2,x3,y3 (TL, TR, BR, BL)
            iw, ih = image.size
            pts = [(crop[i] * iw, crop[i + 1] * ih) for i in range(0, 8, 2)]
            tl, tr, br, bl = pts
            top_w  = math.hypot(tr[0] - tl[0], tr[1] - tl[1])
            bot_w  = math.hypot(br[0] - bl[0], br[1] - bl[1])
            left_h = math.hypot(bl[0] - tl[0], bl[1] - tl[1])
            rgt_h  = math.hypot(br[0] - tr[0], br[1] - tr[1])
            out_w  = max(1, int((top_w + bot_w) / 2))
            out_h  = max(1, int((left_h + rgt_h) / 2))
            # Transformation de perspective via OpenCV (privilégié)
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
            # Fallback PIL avec calcul analytique des coefficients
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
    def _perspective_coeffs(tl, tr, br, bl, out_w, out_h):
        """Coefficients PIL PERSPECTIVE pour le mapping inverse output→input.

        PIL applique : x_in = (a*xo + b*yo + c) / (g*xo + h*yo + 1)
                       y_in = (d*xo + e*yo + f) / (g*xo + h*yo + 1)
        Les coins de sortie (0,0),(W,0),(W,H),(0,H) doivent correspondre à tl,tr,br,bl.
        """
        x0, y0 = float(tl[0]), float(tl[1])
        x1, y1 = float(tr[0]), float(tr[1])
        x2, y2 = float(br[0]), float(br[1])
        x3, y3 = float(bl[0]), float(bl[1])
        W, H = float(out_w), float(out_h)
        # Résolution du système 2×2 pour g et h
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
