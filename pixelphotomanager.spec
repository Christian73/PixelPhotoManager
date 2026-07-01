# -*- mode: python ; coding: utf-8 -*-
# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Spec PyInstaller — PixelPhotoManager
Build : .venv\Scripts\pyinstaller.exe pixelphotomanager.spec --clean
Sortie : dist\PixelPhotoManager\PixelPhotoManager.exe  (one-dir)
"""
from PyInstaller.utils.hooks import collect_all
from pathlib import Path
import insightface

ROOT = Path(SPECPATH)

# Packages avec ressources intégrées (templates, polices, plugins)
_with_data = ["PIL", "folium", "reportlab", "insightface"]

datas, binaries, hiddenimports = [], [], []
for pkg in _with_data:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# insightface.data.get_object() (pickle_object.py) résout ses ressources
# différemment en mode figé : en mode "frozen", il lit
# sys._MEIPASS/objects/<name>.pkl — un dossier "objects" À LA RACINE du
# bundle — et NON insightface/data/objects/ (l'arborescence normale du
# package, que collect_all() ci-dessus préserve). Sans cette copie
# supplémentaire, get_object('meanshape_68.pkl') renvoie None en silence
# (juste un print(), invisible en console=False), et le modèle
# landmark_3d_68 (estimation de pose) plante avec
# 'NoneType' object has no attribute 'shape' dans
# transform.estimate_affine_matrix_3d23d() — pour CHAQUE visage détecté.
_insightface_objects_dir = Path(insightface.__file__).parent / "data" / "objects"
datas += [(str(_insightface_objects_dir), "objects")]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas + [
        (str(ROOT / "assets"), "assets"),
    ],
    hiddenimports=hiddenimports + [
        # Pillow — plugins image chargés dynamiquement
        "PIL.Image", "PIL.ImageOps", "PIL.ImageFilter", "PIL.ImageDraw",
        "PIL.JpegImagePlugin", "PIL.Jpeg2KImagePlugin",
        "PIL.PngImagePlugin",  "PIL.TiffImagePlugin",
        "PIL.WebPImagePlugin", "PIL.BmpImagePlugin",
        "PIL.GifImagePlugin",  "PIL.IcoImagePlugin",
        # OpenCV (DLLs gérées par le hook hooks-contrib)
        "cv2",
        # EXIF
        "piexif",
        # Hashing perceptuel (détection doublons)
        "imagehash",
        # Stdlib (SQLite embarqué dans Python, mais on force l'inclusion)
        "sqlite3", "_sqlite3",
        # Encodages utilisés par le logging / les chemins Windows
        "encodings.utf_8", "encodings.cp1252",
    ],
    hookspath=[],
    hooksconfig={
        # PySide6 : inclure les plugins Qt nécessaires
        "PySide6": {
            "qt_plugins": [
                "platforms",        # QWindowsIntegration (obligatoire)
                "imageformats",     # JPEG, PNG, TIFF, WebP dans Qt
                "styles",           # QWindowsVistaStyle
                "iconengines",      # SVG icons
            ],
        },
    },
    runtime_hooks=[],
    excludes=[
        # Dépendances IA lourdes — non utilisées dans le build de base
        # (sklearn/scikit_learn NE PAS exclure : requis par hdbscan pour le
        # clustering des visages, cf. src/faces/clusterer.py)
        "deepface", "retinaface",
        "torch", "torchvision", "torchaudio",
        "tensorflow", "tensorboard", "keras",
        # Inutiles en production
        "tkinter",
        "matplotlib",
        "IPython", "jupyter", "notebook",
        "debugpy",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PixelPhotoManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX désactivé : évite les faux positifs antivirus
    console=False,      # Pas de fenêtre console (application graphique)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "app_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PixelPhotoManager",
)
