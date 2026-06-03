# -*- mode: python ; coding: utf-8 -*-
"""
Spec PyInstaller — PixelPhotoManager
Build : .venv\Scripts\pyinstaller.exe pixelphotomanager.spec --clean
Sortie : dist\PixelPhotoManager\PixelPhotoManager.exe  (one-dir)
"""
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Packages avec ressources intégrées (templates, polices, plugins)
_with_data = ["PIL", "folium", "reportlab"]

datas, binaries, hiddenimports = [], [], []
for pkg in _with_data:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
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
        "deepface", "retinaface",
        "torch", "torchvision", "torchaudio",
        "tensorflow", "tensorboard", "keras",
        "sklearn", "scikit_learn",
        # Inutiles en production
        "tkinter",
        "matplotlib",
        "IPython", "jupyter", "notebook",
        "debugpy",
        "pytest",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    icon="assets/lutin_camera_icon_download.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PixelPhotoManager",
)
