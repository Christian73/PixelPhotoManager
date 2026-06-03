from PIL import Image, ImageEnhance, ImageFilter
from src.core.models import EditInfo
from src.processing.geometry import GeometryProcessor


class ImageAdjuster:
    @staticmethod
    def apply_all(image: Image.Image, edit: EditInfo) -> Image.Image:
        image = GeometryProcessor.apply_rotation(image, edit.rotation + edit.straighten)
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
        if edit.gamma != 1.0:
            image = ImageAdjuster.apply_gamma(image, edit.gamma)
        if edit.sharpness > 0.0:
            image = ImageAdjuster.apply_sharpness(image, edit.sharpness)
        if edit.noise_reduction > 0.0:
            image = ImageAdjuster.apply_noise_reduction(image, edit.noise_reduction)

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
