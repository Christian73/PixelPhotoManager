# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import contextlib
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v", ".3gp", ".flv", ".ts", ".mts", ".mpg", ".mpeg"}


@contextlib.contextmanager
def ascii_safe_path(path: str):
    """Chemin garanti encodable en ASCII pour cv2 (imread/VideoCapture), qui
    rejette les chemins non-ASCII sur Windows. Si `path` est déjà ASCII, le
    retourne tel quel (aucune I/O). Sinon, crée un hardlink temporaire vers un
    chemin ASCII (repli sur une copie si hardlink impossible, ex. volume
    différent) et le supprime en sortie de bloc."""
    try:
        path.encode("ascii")
        yield path
        return
    except UnicodeEncodeError:
        pass

    suffix = os.path.splitext(path)[1]
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.remove(temp_path)  # os.link exige que la destination n'existe pas
    try:
        try:
            os.link(path, temp_path)
        except OSError:
            shutil.copy2(path, temp_path)
        yield temp_path
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def preserve_file_dates(src_stat: os.stat_result, dst_path: str) -> None:
    """Copie atime, mtime et date de création (Windows) de src_stat vers dst_path."""
    os.utime(dst_path, (src_stat.st_atime, src_stat.st_mtime))
    try:
        import ctypes
        import ctypes.wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime",  ctypes.wintypes.DWORD),
                         ("dwHighDateTime", ctypes.wintypes.DWORD)]

        # Convertir timestamp Unix → FILETIME (100 ns depuis le 1er janvier 1601)
        val = int((src_stat.st_ctime + 11644473600) * 10_000_000)
        ft = FILETIME(dwLowDateTime=val & 0xFFFFFFFF,
                      dwHighDateTime=(val >> 32) & 0xFFFFFFFF)

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            dst_path, 0x40000000, 1, None, 3, 0x02000000, None
        )
        if handle not in (-1, 0):
            kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
            kernel32.CloseHandle(handle)
    except Exception:
        pass   # non-Windows ou droits insuffisants : mtime suffit


_SUBSEC_TAG_FOR = {
    "DateTimeOriginal": "SubsecTimeOriginal",
    "DateTimeDigitized": "SubsecTimeDigitized",
    "DateTime": "SubsecTime",
}


def _parse_subsec(value: str) -> int:
    """Convertit un tag EXIF SubsecTime* (ex. '05', '563') en microsecondes.
    Le tag représente les décimales de la seconde, pas des microsecondes
    brutes : '05' = 0.05s = 50000µs (et non 5µs), d'où le padding à droite."""
    value = value.strip()
    if not value or not value.isdigit():
        return 0
    return int((value + "000000")[:6])


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

                exif_obj = img.getexif()
                if not exif_obj:
                    return result

                exif = {
                    ExifTags.TAGS.get(k, k): v
                    for k, v in exif_obj.items()
                    if k in ExifTags.TAGS
                }

                # ExifIFD sub-tags (DateTimeOriginal, ISO, ExposureTime, etc.)
                try:
                    exif_ifd = exif_obj.get_ifd(0x8769)
                    if exif_ifd:
                        for k, v in exif_ifd.items():
                            tag_name = ExifTags.TAGS.get(k, k)
                            if isinstance(tag_name, str):
                                exif.setdefault(tag_name, v)
                except Exception:
                    pass

                for tag in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                    if tag in exif and exif[tag]:
                        try:
                            dt = datetime.strptime(
                                str(exif[tag]), "%Y:%m:%d %H:%M:%S"
                            )
                            subsec = exif.get(_SUBSEC_TAG_FOR[tag])
                            if subsec:
                                dt = dt.replace(microsecond=_parse_subsec(str(subsec)))
                            result["date_taken"] = dt
                            break
                        except ValueError:
                            pass

                result["camera_make"] = str(exif.get("Make", "")).strip()
                result["camera_model"] = str(exif.get("Model", "")).strip()
                result["lens_model"] = str(exif.get("LensModel", "")).strip()

                iso_val = exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity")
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

                try:
                    gps_ifd = exif_obj.get_ifd(0x8825)
                    if gps_ifd:
                        coords = ExifReader._parse_gps(gps_ifd)
                        if coords:
                            result["has_gps"] = True
                            result["gps_lat"], result["gps_lon"] = coords
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"Erreur lecture EXIF {path}: {e}")

        return result

    @staticmethod
    def _parse_gps(gps_info) -> "tuple[float, float] | None":  # noqa: F811
        try:
            from PIL import ExifTags

            gps_tags = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_info.items()}

            def _to_float(val) -> float:
                # IFDRational supports float(); raw (num, denom) tuple does not
                if isinstance(val, tuple) and len(val) == 2:
                    return val[0] / val[1]
                return float(val)

            def dms_to_dd(dms, ref: str) -> float:
                d = _to_float(dms[0])
                m = _to_float(dms[1])
                s = _to_float(dms[2])
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


class VideoMetadataReader:
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
            "duration": 0.0,
        }
        try:
            import cv2
            with ascii_safe_path(path) as safe_path:
                cap = cv2.VideoCapture(safe_path)
                if cap.isOpened():
                    result["width"]  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    result["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    fps         = cap.get(cv2.CAP_PROP_FPS)
                    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                    if fps > 0 and frame_count > 0:
                        result["duration"] = frame_count / fps
                    cap.release()
        except Exception as e:
            logger.debug("Erreur lecture métadonnées vidéo %s: %s", path, e)
        try:
            result["date_taken"] = datetime.fromtimestamp(os.stat(path).st_mtime)
        except OSError:
            pass
        return result
