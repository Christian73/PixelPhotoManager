# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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
    duplicate_group_id: Optional[int] = None  # groupe de doublons (None = unique)

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
    vignette_strength: float = 0.0    # 0 = aucune, 1 = maximale
    vignette_color: str = "black"     # "black" ou "white"
    vignette_cx: float = 0.5          # centre X normalisé (0-1)
    vignette_cy: float = 0.5          # centre Y normalisé (0-1)
    vignette_rx1: float = 0.40        # rayon X interne (1.0 = demi-largeur image)
    vignette_ry1: float = 0.40        # rayon Y interne (1.0 = demi-hauteur image)
    vignette_rx2: float = 0.80        # rayon X externe (1.0 = demi-largeur image)
    vignette_ry2: float = 0.80        # rayon Y externe (1.0 = demi-hauteur image)
    vignette_angle: float = 0.0       # rotation en degrés
    annotations: list = field(default_factory=list)  # calque dessin/texte, cf. annotation_renderer.py

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
            or self.vignette_strength > 0.0
            or bool(self.annotations)
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
            "vignette_strength": self.vignette_strength,
            "vignette_color":    self.vignette_color,
            "vignette_cx":       self.vignette_cx,
            "vignette_cy":       self.vignette_cy,
            "vignette_rx1":      self.vignette_rx1,
            "vignette_ry1":      self.vignette_ry1,
            "vignette_rx2":      self.vignette_rx2,
            "vignette_ry2":      self.vignette_ry2,
            "vignette_angle":    self.vignette_angle,
            "annotations": [dict(a) for a in self.annotations],
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
            vignette_strength=float(data.get("vignette_strength", 0.0)),
            vignette_color=str(data.get("vignette_color", "black")),
            vignette_cx=float(data.get("vignette_cx", 0.5)),
            vignette_cy=float(data.get("vignette_cy", 0.5)),
            vignette_rx1=float(data.get("vignette_rx1", 0.40)),
            vignette_ry1=float(data.get("vignette_ry1", 0.40)),
            vignette_rx2=float(data.get("vignette_rx2", 0.80)),
            vignette_ry2=float(data.get("vignette_ry2", 0.80)),
            vignette_angle=float(data.get("vignette_angle", 0.0)),
            annotations=[dict(a) for a in data.get("annotations", [])],
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
    cover_bbox: Optional[tuple] = None            # (x, y, w, h) dans cover_path
    cover_detected_rotation: int = 0             # rotation CW lors de la détection
    pending_count: int = 0                        # groupes en attente de vérification


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
