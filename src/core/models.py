from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import os


@dataclass
class PhotoInfo:
    path: str
    filename: str = ""
    directory: str = ""
    date_taken: Optional[datetime] = None
    width: int = 0
    height: int = 0
    file_size: int = 0
    file_mtime: float = 0.0
    camera_make: str = ""
    camera_model: str = ""
    lens_model: str = ""
    iso: Optional[int] = None
    exposure_time: str = ""
    aperture: Optional[float] = None
    focal_length: Optional[float] = None
    has_gps: bool = False
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    is_favorite: bool = False
    tags: list[str] = field(default_factory=list)
    id: Optional[int] = None
    media_type: str = "image"   # "image" or "video"
    duration: float = 0.0       # durée en secondes (vidéos uniquement)

    def __post_init__(self):
        if self.path:
            self.path = os.path.normpath(self.path)
        if not self.filename:
            self.filename = os.path.basename(self.path)
        if not self.directory:
            self.directory = os.path.dirname(self.path)


@dataclass
class EditInfo:
    brightness: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    gamma: float = 1.0
    gamma_use_curve: bool = False
    gamma_curve_points: list = field(
        default_factory=lambda: [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
    )
    sharpness: float = 0.0
    noise_reduction: float = 0.0
    rotation: float = 0.0
    straighten: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    crop: Optional[tuple] = None
    bw: bool = False
    bw_red: float = 0.0
    bw_green: float = 0.0
    bw_blue: float = 0.0
    color_red: float = 0.0
    color_green: float = 0.0
    color_blue: float = 0.0
    red_eye_regions: list = field(default_factory=list)  # [(cx, cy, r), ...] normalisés 0-1

    def is_modified(self) -> bool:
        return (
            self.brightness != 0.0
            or self.contrast != 0.0
            or self.saturation != 0.0
            or self.gamma != 1.0
            or self.gamma_use_curve
            or self.sharpness != 0.0
            or self.noise_reduction != 0.0
            or self.rotation != 0.0
            or self.straighten != 0.0
            or self.flip_h
            or self.flip_v
            or self.crop is not None
            or self.bw
            or self.color_red != 0.0
            or self.color_green != 0.0
            or self.color_blue != 0.0
            or bool(self.red_eye_regions)
        )

    def to_dict(self) -> dict:
        return {
            "brightness": self.brightness,
            "contrast": self.contrast,
            "saturation": self.saturation,
            "gamma": self.gamma,
            "gamma_use_curve": self.gamma_use_curve,
            "gamma_curve_points": self.gamma_curve_points,
            "sharpness": self.sharpness,
            "noise_reduction": self.noise_reduction,
            "rotation": self.rotation,
            "straighten": self.straighten,
            "flip_h": self.flip_h,
            "flip_v": self.flip_v,
            "crop": list(self.crop) if self.crop else None,
            "bw": self.bw,
            "bw_red": self.bw_red,
            "bw_green": self.bw_green,
            "bw_blue": self.bw_blue,
            "color_red": self.color_red,
            "color_green": self.color_green,
            "color_blue": self.color_blue,
            "red_eye_regions": [list(r) for r in self.red_eye_regions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EditInfo":
        crop = data.get("crop")
        return cls(
            brightness=float(data.get("brightness", 0.0)),
            contrast=float(data.get("contrast", 0.0)),
            saturation=float(data.get("saturation", 0.0)),
            gamma=float(data.get("gamma", 1.0)),
            gamma_use_curve=bool(data.get("gamma_use_curve", False)),
            gamma_curve_points=[
                (float(x), float(y))
                for x, y in data.get("gamma_curve_points", [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
            ],
            sharpness=float(data.get("sharpness", 0.0)),
            noise_reduction=float(data.get("noise_reduction", 0.0)),
            rotation=float(data.get("rotation", 0.0)),
            straighten=float(data.get("straighten", 0.0)),
            flip_h=bool(data.get("flip_h", False)),
            flip_v=bool(data.get("flip_v", False)),
            crop=tuple(crop) if crop else None,
            bw=bool(data.get("bw", False)),
            bw_red=float(data.get("bw_red", 0.0)),
            bw_green=float(data.get("bw_green", 0.0)),
            bw_blue=float(data.get("bw_blue", 0.0)),
            color_red=float(data.get("color_red", 0.0)),
            color_green=float(data.get("color_green", 0.0)),
            color_blue=float(data.get("color_blue", 0.0)),
            red_eye_regions=[
                tuple(r) for r in data.get("red_eye_regions", [])
            ],
        )


@dataclass
class AlbumInfo:
    name: str
    id: Optional[int] = None
    description: str = ""
    photo_count: int = 0


@dataclass
class PersonInfo:
    name: str
    id: Optional[int] = None
    photo_count: int = 0
    cover_path: str = ""
    cover_bbox: Optional[tuple] = None  # (x, y, w, h) dans cover_path


@dataclass
class FaceInfo:
    id: int = 0
    photo_path: str = ""
    bbox_x: int = 0
    bbox_y: int = 0
    bbox_w: int = 0
    bbox_h: int = 0
    cluster_id: Optional[int] = None
    person_id: Optional[int] = None
    ignored: bool = False
    pinned: bool = False   # True = face isolée manuellement, exclue du re-clustering
    detected_rotation: int = 0   # rotation CW (degrés) appliquée lors de la détection
