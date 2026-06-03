import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ExifReader:
    SUPPORTED = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp", ".bmp", ".gif"}

    @staticmethod
    def read(path: str) -> dict:
        result = {
            "date_taken": None,
            "width": 0,
            "height": 0,
            "camera_make": "",
            "camera_model": "",
            "lens_model": "",
            "iso": None,
            "exposure_time": "",
            "aperture": None,
            "focal_length": None,
            "has_gps": False,
            "gps_lat": None,
            "gps_lon": None,
        }
        try:
            from PIL import Image, ImageOps, ExifTags

            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img)
                result["width"], result["height"] = img.size

                raw_exif = img._getexif() if hasattr(img, "_getexif") else None
                if not raw_exif:
                    return result

                exif = {
                    ExifTags.TAGS.get(k, k): v
                    for k, v in raw_exif.items()
                    if k in ExifTags.TAGS
                }

                for tag in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                    if tag in exif and exif[tag]:
                        try:
                            result["date_taken"] = datetime.strptime(
                                str(exif[tag]), "%Y:%m:%d %H:%M:%S"
                            )
                            break
                        except ValueError:
                            pass

                result["camera_make"] = str(exif.get("Make", "")).strip()
                result["camera_model"] = str(exif.get("Model", "")).strip()
                result["lens_model"] = str(exif.get("LensModel", "")).strip()

                iso_val = exif.get("ISOSpeedRatings")
                if iso_val is not None:
                    result["iso"] = int(iso_val) if not isinstance(iso_val, tuple) else int(iso_val[0])

                exp = exif.get("ExposureTime")
                if exp is not None:
                    if hasattr(exp, "numerator"):
                        result["exposure_time"] = f"{exp.numerator}/{exp.denominator}s"
                    else:
                        result["exposure_time"] = str(exp)

                fnumber = exif.get("FNumber")
                if fnumber is not None:
                    try:
                        result["aperture"] = float(fnumber)
                    except (TypeError, ValueError):
                        pass

                fl = exif.get("FocalLength")
                if fl is not None:
                    try:
                        result["focal_length"] = float(fl)
                    except (TypeError, ValueError):
                        pass

                gps_info = exif.get("GPSInfo")
                if gps_info:
                    coords = ExifReader._parse_gps(gps_info)
                    if coords:
                        result["has_gps"] = True
                        result["gps_lat"], result["gps_lon"] = coords

        except Exception as e:
            logger.debug(f"Erreur lecture EXIF {path}: {e}")

        return result

    @staticmethod
    def _parse_gps(gps_info) -> "tuple[float, float] | None":
        try:
            from PIL import ExifTags

            gps_tags = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_info.items()}

            def dms_to_dd(dms, ref: str) -> float:
                d = float(dms[0])
                m = float(dms[1])
                s = float(dms[2])
                dd = d + m / 60.0 + s / 3600.0
                if ref in ("S", "W"):
                    dd = -dd
                return dd

            lat_dms = gps_tags.get("GPSLatitude")
            lat_ref = gps_tags.get("GPSLatitudeRef", "N")
            lon_dms = gps_tags.get("GPSLongitude")
            lon_ref = gps_tags.get("GPSLongitudeRef", "E")

            if lat_dms and lon_dms:
                return dms_to_dd(lat_dms, lat_ref), dms_to_dd(lon_dms, lon_ref)
        except Exception as e:
            logger.debug(f"Erreur parse GPS: {e}")
        return None
