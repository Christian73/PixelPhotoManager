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
import PySide6

ROOT = Path(SPECPATH)

# Packages avec ressources intégrées (templates, polices, plugins)
# pillow_heif/rawpy : DLLs natives (libheif, libraw) embarquées via
# collect_all — jamais dans excludes (cf. src/library/image_loader.py,
# point de décodage unique RAW/HEIC).
_with_data = ["PIL", "folium", "reportlab", "insightface", "pillow_heif", "rawpy"]

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

# Pack de modèles buffalo_l (détection + embedding) embarqué dans l'exe pour
# éviter le téléchargement (~340 Mo) depuis GitHub au 1er lancement — sur un
# poste sans accès Internet (ou pare-feu bloquant github.com), la détection
# de visages était sinon totalement inopérante. Placé à la racine du bundle
# sous "insightface_root/models/buffalo_l" ; src/faces/detector.py pointe
# explicitement FaceAnalysis(root=...) dessus en mode figé (sys._MEIPASS).
# Nécessite d'avoir lancé l'appli au moins une fois en mode dev pour que le
# pack soit présent dans le cache utilisateur (~/.insightface/models/buffalo_l).
_buffalo_l_dir = Path.home() / ".insightface" / "models" / "buffalo_l"
if not _buffalo_l_dir.is_dir():
    raise FileNotFoundError(
        f"Pack de modèles buffalo_l introuvable : {_buffalo_l_dir}\n"
        "Lancez l'appli une fois en mode dev (avec accès Internet) pour "
        "déclencher le téléchargement automatique, puis relancez le build."
    )
datas += [(str(_buffalo_l_dir), "insightface_root/models/buffalo_l")]

# Contenu des onglets d'aide (src/ui/help_content/<langue>/*.html) — résolu en
# mode figé via sys._MEIPASS/help_content (cf. src/ui/help_dialog.py::_content_dir).
# Une entrée « dossier » de datas est copiée récursivement : les sous-dossiers
# de langue (fr/en/de) suivent sans entrée dédiée.
datas += [("src/ui/help_content", "help_content")]

# Numéro de version embarqué à la racine du bundle (sys._MEIPASS/VERSION), lu
# par src/core/app_version.py::get_app_version() en mode figé (le dossier .git
# n'est pas disponible dans l'exe pour faire un "git describe"). Le fichier
# VERSION à la racine du dépôt est mis à jour par build.ps1 avant chaque build,
# à partir du numéro de version demandé au constructeur.
_version_file = ROOT / "VERSION"
if not _version_file.is_file():
    raise FileNotFoundError(
        f"Fichier VERSION introuvable : {_version_file}\n"
        "Lancez le build via build.ps1 (qui le génère) plutôt que pyinstaller directement."
    )
datas += [(str(_version_file), ".")]

# Catalogues de traduction compilés (translations/ppm_*.qm) + ceux de Qt lui-même
# (qtbase_*.qm : boutons OK/Annuler, sélecteur de fichiers…), les deux résolus par
# src/core/i18n.py sous sys._MEIPASS/translations en mode figé. Les .qm sont
# générés par tools/update_translations.py — un .ts sans .qm ne sert à rien à
# l'exécution, d'où l'échec explicite plutôt qu'une interface muettement française.
_ts_dir = ROOT / "translations"
_qm_files = sorted(_ts_dir.glob("ppm_*.qm")) if _ts_dir.is_dir() else []
if not _qm_files:
    raise FileNotFoundError(
        f"Aucun catalogue compilé dans {_ts_dir}\n"
        "Lancez : .venv\\Scripts\\python.exe tools\\update_translations.py"
    )
datas += [(str(p), "translations") for p in _qm_files]

_qt_ts_dir = Path(PySide6.__file__).parent / "translations"
for _code in ("fr", "en", "de"):
    _qtbase = _qt_ts_dir / f"qtbase_{_code}.qm"
    if _qtbase.is_file():
        datas += [(str(_qtbase), "translations")]

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
        # Corbeille Windows (src/library/trash.py)
        "send2trash",
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
