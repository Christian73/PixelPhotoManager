# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations  # lazy annotations -> QDialog etc. not evaluated in the subprocess

import logging
import os
import sys
import traceback
from pathlib import Path
import multiprocessing

# In a "windowed" exe (console=False), sys.stdout/sys.stderr are None: any
# library writing to them (e.g. tqdm, used by insightface during the
# download of the buffalo_l model pack) crashes with
# AttributeError: 'NoneType' object has no attribute 'write'. That crash
# interrupted the download before the model was written to disk,
# so the model was never cached and every photo attempted a full
# download again. Extends to the worker subprocesses (spawn) as well.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# ProcessPoolExecutor on Windows uses spawn: the worker subprocess imports
# this module BEFORE running the task.  We keep to a strict minimum what runs
# in the subprocess to avoid side effects (duplicate logging, Qt DLLs...).
if multiprocessing.current_process().name == 'MainProcess':
    import threading

    # In EXE mode (PyInstaller), store the logs in %LOCALAPPDATA%
    if getattr(sys, "frozen", False):
        _LOG_PATH = (
            Path(os.environ.get("LOCALAPPDATA", Path.home()))
            / "PixelPhotoManager" / "logs" / "pixelphotomanager.log"
        )
    else:
        _LOG_PATH = Path(__file__).parent / "logs" / "pixelphotomanager.log"
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(_LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    for _noisy in ("PIL", "PIL.Image", "PIL.PngImagePlugin", "PIL.JpegImagePlugin",
                   "PIL.TiffImagePlugin", "PIL.WebPImagePlugin"):
        logging.getLogger(_noisy).setLevel(logging.ERROR)

    import warnings
    from PIL import Image as _PilImage
    warnings.filterwarnings("ignore", category=_PilImage.DecompressionBombWarning)

    def _show_error_dialog(exc_type, exc_value, exc_tb) -> None:
        """Displays a dialog with the full traceback, selectable and copyable."""
        try:
            app = QApplication.instance()
            if app is None:
                return
            text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))

            dlg = QDialog()
            dlg.setWindowTitle(translate("Main", "Error — PixelPhotoManager"))
            dlg.setMinimumSize(720, 420)
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel(translate("Main", "<b>An unhandled error occurred:</b>")))

            te = QTextEdit(dlg)
            te.setReadOnly(True)
            te.setFont(QFont("Consolas", 9))
            te.setPlainText(text)
            layout.addWidget(te)

            btn_row = QHBoxLayout()
            btn_copy = QPushButton(translate("Main", "Copy the text"))
            btn_copy.clicked.connect(lambda: app.clipboard().setText(text))
            btn_close = QPushButton(translate("Main", "Close"))
            btn_close.clicked.connect(dlg.accept)
            btn_row.addWidget(btn_copy)
            btn_row.addStretch()
            btn_row.addWidget(btn_close)
            layout.addLayout(btn_row)

            dlg.exec()
        except Exception:
            pass

    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger(__name__).critical(
            "Exception non gérée", exc_info=(exc_type, exc_value, exc_tb)
        )
        _show_error_dialog(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args):
        logging.getLogger(__name__).critical(
            f"Exception non gérée dans thread {args.thread.name}",
            exc_info=(args.exc_type, args.exc_value, args.exc_tb),
        )
        _show_error_dialog(args.exc_type, args.exc_value, args.exc_tb)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QDialog, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QListWidget, QListWidgetItem,
        QFileDialog, QFrame, QWidget, QSplashScreen, QTextEdit,
    )
    from PySide6.QtCore import Qt, QPoint, QTimer
    from src.core.i18n import translate
    from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QLinearGradient

logger = logging.getLogger(__name__)


def _build_splash() -> QSplashScreen:
    """Builds the splash screen: black background, centred icon, status area at the bottom."""
    W, H = 480, 320
    STATUS_H = 48          # height reserved for the status line

    base = QPixmap(W, H)
    base.fill(QColor("#000000"))

    p = QPainter(base)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    # Icon
    icon_path = Path(__file__).resolve().parent / "assets" / "cubic.png"
    if icon_path.exists():
        icon_px = QPixmap(str(icon_path)).scaled(
            120, 120,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        ix = (W - icon_px.width()) // 2
        iy = 36
        p.drawPixmap(ix, iy, icon_px)

    # Application name
    font_title = QFont("Segoe UI", 17, QFont.Weight.Bold)
    p.setFont(font_title)
    p.setPen(QColor("#ffffff"))
    p.drawText(0, 170, W, 36, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
               "PixelPhotoManager")

    # Separator above the status area
    sep_y = H - STATUS_H - 1
    grad = QLinearGradient(0, sep_y, W, sep_y)
    grad.setColorAt(0.0,  QColor(0, 0, 0, 0))
    grad.setColorAt(0.2,  QColor("#333333"))
    grad.setColorAt(0.8,  QColor("#333333"))
    grad.setColorAt(1.0,  QColor(0, 0, 0, 0))
    p.setPen(Qt.PenStyle.NoPen)
    from PySide6.QtGui import QBrush
    p.fillRect(0, sep_y, W, 1, QBrush(grad))

    p.end()

    splash = QSplashScreen(base, Qt.WindowType.WindowStaysOnTopHint)
    splash.setFont(QFont("Segoe UI", 10))
    return splash


def _splash_status(splash: QSplashScreen, app: QApplication, msg: str) -> None:
    """Updates the status message and forces the repaint."""
    splash.showMessage(
        msg,
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        QColor("#888888"),
    )
    app.processEvents()


def _build_onboarding(config) -> QDialog:
    dlg = QDialog()
    dlg.setWindowTitle(translate("Main", "Welcome to PixelPhotoManager"))
    dlg.setMinimumWidth(500)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(14)
    layout.setContentsMargins(24, 24, 24, 24)

    title = QLabel(translate("Main", "Welcome to PixelPhotoManager!"))
    font = QFont()
    font.setPointSize(14)
    font.setBold(True)
    title.setFont(font)
    title.setAlignment(Qt.AlignCenter)
    layout.addWidget(title)

    # --- Folders ---
    layout.addWidget(QLabel(translate(
        "Main", "Where are your photos? Choose at least one folder to watch.")))

    folder_list = QListWidget()
    for f in config.get_scan_folders():
        folder_list.addItem(QListWidgetItem(f))
    layout.addWidget(folder_list)

    btn_row = QHBoxLayout()
    btn_add = QPushButton(translate("Main", "+ Add a folder"))

    def _add_folder():
        folder = QFileDialog.getExistingDirectory(
            dlg, translate("Main", "Choose a folder"),
            os.path.expanduser("~"))
        if folder and folder not in [
            folder_list.item(i).text() for i in range(folder_list.count())
        ]:
            folder_list.addItem(QListWidgetItem(folder))

    btn_add.clicked.connect(_add_folder)
    btn_row.addWidget(btn_add)
    btn_row.addStretch()
    layout.addLayout(btn_row)

    # --- Separator ---
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setStyleSheet("color: #444;")
    layout.addWidget(sep)

    # --- Face recognition ---
    lbl_faces = QLabel(translate("Main", "People recognition"))
    font2 = QFont()
    font2.setBold(True)
    lbl_faces.setFont(font2)
    layout.addWidget(lbl_faces)

    chk_faces = QCheckBox(translate(
        "Main",
        "Analyse faces automatically after each scan\n(runs in the background — recommended)",
    ))
    chk_faces.setChecked(True)
    layout.addWidget(chk_faces)

    lbl_note = QLabel(translate(
        "Main",
        "The first analysis can take several minutes depending on the size\nof your library. "
        "It runs only once per photo.",
    ))
    lbl_note.setStyleSheet("color: #888; font-size: 11px;")
    layout.addWidget(lbl_note)

    # --- Start button ---
    btn_start = QPushButton(translate("Main", "Get started →"))
    btn_start.setDefault(True)

    def _start():
        for i in range(folder_list.count()):
            config.add_scan_folder(folder_list.item(i).text())
        config.set("faces.auto_index", chk_faces.isChecked())
        dlg.accept()

    btn_start.clicked.connect(_start)
    layout.addWidget(btn_start, alignment=Qt.AlignRight)

    return dlg


def main() -> None:
    logger.debug("Création QApplication")
    app = QApplication(sys.argv)
    app.setApplicationName("PixelPhotoManager")
    app.setOrganizationName("PixelPhotoManager")

    # Language - BEFORE the construction of the least widget (including the splash):
    # the labels are translated at construction time, not re-evaluated afterwards.
    # Before the src.ui imports further down too, whose module constants
    # (treatment labels, frame labels...) are translated at import time.
    from src.core import i18n
    from src.core.config import Config
    i18n.install(app, i18n.current_language(Config()))

    # Generates the check icon for the global style of the QCheckBox
    import tempfile
    _chk_px = QPixmap(13, 13)
    _chk_px.fill(QColor(0, 0, 0, 0))
    _p = QPainter(_chk_px)
    _p.setRenderHint(QPainter.Antialiasing)
    _p.setPen(QPen(QColor(255, 255, 255), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    _p.drawLine(QPoint(2, 7), QPoint(5, 10))
    _p.drawLine(QPoint(5, 10), QPoint(11, 3))
    _p.end()
    _check_icon = os.path.join(tempfile.gettempdir(), "ppm_check.png").replace("\\", "/")
    _chk_px.save(_check_icon, "PNG")

    # Dark theme: in src/ui/theme.py rather than here, so as to be verifiable
    # without importing this entry point (which reconfigures the logging at import).
    from src.ui.theme import app_stylesheet
    app.setStyleSheet(app_stylesheet(_check_icon))

    # --- Splash screen ---
    splash = _build_splash()
    splash.show()
    app.processEvents()

    logger.debug("Import des modules internes")
    _splash_status(splash, app,
                   translate("Main", "Loading modules…"))
    from src.core.config import Config
    from src.library.catalog import Catalog
    from src.library.thumbnail_cache import ThumbnailCache
    from src.library.scanner import LibraryScanner
    from src.faces.face_database import FaceDatabase
    from src.ui.main_window import MainWindow
    from src.core.app_version import warm_app_version_async

    # Precomputes get_app_version() in the background (git describe in dev mode,
    # up to 2s) during the following initialisations, so that the result
    # is already cached when the UI needs it (startup, help).
    warm_app_version_async()

    logger.debug("Initialisation Config")
    _splash_status(splash, app,
                   translate("Main", "Reading the configuration…"))
    config = Config()

    logger.debug("Initialisation Catalog")
    _splash_status(splash, app,
                   translate("Main", "Opening the catalogue…"))
    catalog = Catalog()

    logger.debug("Initialisation ThumbnailCache")
    _splash_status(splash, app,
                   translate("Main", "Setting up the thumbnail cache…"))
    thumb_cache = ThumbnailCache()
    _removed = catalog.cleanup_asset_dirs()
    if _removed:
        thumb_cache.invalidate_many(_removed)

    logger.debug("Initialisation LibraryScanner")
    _splash_status(splash, app,
                   translate("Main", "Preparing the library scanner…"))
    scanner = LibraryScanner(catalog, thumb_cache)

    logger.debug("Initialisation FaceDatabase")
    _splash_status(splash, app,
                   translate("Main", "Setting up the face database…"))
    face_db = FaceDatabase()

    if not config.get_scan_folders():
        splash.hide()
        logger.debug("Aucun dossier configuré — affichage onboarding")
        dlg = _build_onboarding(config)
        if dlg.exec() == QDialog.Rejected and not config.get_scan_folders():
            logger.info("Onboarding annulé sans dossier — fermeture")
            return  # clean, without sys.exit()
        splash.show()
        app.processEvents()

    logger.debug("Création MainWindow")
    _splash_status(splash, app,
                   translate("Main", "Building the main window…"))
    window = MainWindow(config, catalog, thumb_cache, scanner, face_db)
    window.show()
    splash.finish(window)

    # Offer the Picasa import after the first display of the window
    def _check_picasa():
        from src.ui.picasa_import_dialog import check_and_prompt

        def _on_edits(edited_map):
            for path, edit_info in edited_map.items():
                window._grid.refresh_photo(path, edit_info)

        check_and_prompt(config, catalog, face_db, window._edit_db, window,
                         on_edits_imported=_on_edits)

    QTimer.singleShot(800, _check_picasa)

    logger.debug("Entrée dans la boucle Qt")
    sys.exit(app.exec())


if __name__ == "__main__":
    # CRITICAL: must be the first call in __main__ for the PyInstaller EXEs.
    # Without it, every worker subprocess relaunches the whole application on Windows
    # (spawn), causing an infinite loop that saturates the CPU.
    multiprocessing.freeze_support()
    try:
        logger.info("Démarrage de PixelPhotoManager")
        main()
    except SystemExit:
        pass  # normal exit of app.exec()
    except BaseException:
        logger.critical("Crash au démarrage", exc_info=True)
        _show_error_dialog(*sys.exc_info())
        logging.shutdown()
        sys.exit(1)
