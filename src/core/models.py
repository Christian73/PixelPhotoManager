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
    rating: int = 0             # rating 0-5 stars (0 = unrated)
    tags: list[str] = field(default_factory=list)
    id: Optional[int] = None
    media_type: str = "image"   # "image" or "video"
    duration: float = 0.0       # duration in seconds (videos only)
    duplicate_group_id: Optional[int] = None  # duplicate group (None = unique)

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
    red_eye_regions: list = field(default_factory=list)  # [(cx, cy, r), ...] normalised 0-1
    vignette_strength: float = 0.0    # 0 = none, 1 = maximum
    vignette_color: str = "black"     # "black" or "white"
    vignette_cx: float = 0.5          # normalised X centre (0-1)
    vignette_cy: float = 0.5          # normalised Y centre (0-1)
    vignette_rx1: float = 0.40        # inner X radius (1.0 = half the image width)
    vignette_ry1: float = 0.40        # inner Y radius (1.0 = half the image height)
    vignette_rx2: float = 0.80        # outer X radius (1.0 = half the image width)
    vignette_ry2: float = 0.80        # outer Y radius (1.0 = half the image height)
    vignette_angle: float = 0.0       # rotation in degrees
    annotations: list = field(default_factory=list)  # drawing/text layer, cf. annotation_renderer.py
    # Decorative frame — cf. src/processing/frames.py. The widths are fractions
    # of the short side of the photo (independent of the resolution); the frame is
    # added AROUND the image, it never encroaches on it — the only exception being
    # the optional second frame of "plain" (frame_inner_enabled), drawn ON the
    # photo (cf. frames.inner_overlay_px).
    frame_type: str = "none"           # "none", "plain", "simple", "double", "vine", …
    frame_width: float = 0.05          # width of the outer frame
    frame_inner_width: float = 0.015   # width of the inner frame (double, plain)
    frame_gap: float = 0.02            # gap between the two frames (double, plain)
    frame_style: str = "solid"         # "solid", "gradient", "glitter"
    frame_color: str = "#f2f2f2"       # main colour of the outer frame
    frame_color2: str = "#8c8c8c"      # 2nd colour (gradient / glitter flecks)
    frame_inner_color: str = "#303030"  # colour of the inner frame (double)
    frame_gap_color: str = "#ffffff"   # colour of the gap (double)
    frame_inner_enabled: bool = False  # second frame of "plain" (on the photo)
    frame_inner_motif: str = "line"    # ironwork of the second frame, cf. frames.INNER_MOTIFS
    frame_inner_relief: bool = True    # light relief (False = strict flat fill)
    frame_inner_ornament: float = 1.0  # scale of the ornaments ("Ornaments" slider)

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
            or (self.frame_type or "none") != "none"
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
            "frame_type":        self.frame_type,
            "frame_width":       self.frame_width,
            "frame_inner_width": self.frame_inner_width,
            "frame_gap":         self.frame_gap,
            "frame_style":       self.frame_style,
            "frame_color":       self.frame_color,
            "frame_color2":      self.frame_color2,
            "frame_inner_color": self.frame_inner_color,
            "frame_gap_color":   self.frame_gap_color,
            "frame_inner_enabled": self.frame_inner_enabled,
            "frame_inner_motif":     self.frame_inner_motif,
            "frame_inner_relief":    self.frame_inner_relief,
            "frame_inner_ornament":  self.frame_inner_ornament,
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
            frame_type=str(data.get("frame_type", "none") or "none"),
            frame_width=float(data.get("frame_width", 0.05)),
            frame_inner_width=float(data.get("frame_inner_width", 0.015)),
            frame_gap=float(data.get("frame_gap", 0.02)),
            frame_style=str(data.get("frame_style", "solid") or "solid"),
            frame_color=str(data.get("frame_color", "#f2f2f2") or "#f2f2f2"),
            frame_color2=str(data.get("frame_color2", "#8c8c8c") or "#8c8c8c"),
            frame_inner_color=str(data.get("frame_inner_color", "#303030") or "#303030"),
            frame_gap_color=str(data.get("frame_gap_color", "#ffffff") or "#ffffff"),
            frame_inner_enabled=bool(data.get("frame_inner_enabled", False)),
            frame_inner_motif=str(data.get("frame_inner_motif", "line") or "line"),
            frame_inner_relief=bool(data.get("frame_inner_relief", True)),
            frame_inner_ornament=float(data.get("frame_inner_ornament", 1.0)),
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
    cover_bbox: Optional[tuple] = None            # (x, y, w, h) in cover_path
    cover_detected_rotation: int = 0             # CW rotation at detection time
    pending_count: int = 0                        # groups awaiting verification


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
    pinned: bool = False   # True = face isolated manually, excluded from re-clustering
    detected_rotation: int = 0   # CW rotation (degrees) applied at detection time
    suggestion_person_id: Optional[int] = None   # suggested person (awaiting verification)
    suggestion_score: float = 0.0                # cosine similarity of the suggestion above
