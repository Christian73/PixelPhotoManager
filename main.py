from __future__ import annotations  # annotations lazy → QDialog etc. non évalués dans le subprocess

import logging
import os
import sys
import traceback
from pathlib import Path
import multiprocessing

# ProcessPoolExecutor sur Windows utilise spawn : le sous-processus worker importe
# ce module AVANT d'exécuter la tâche.  On limite au strict minimum ce qui s'exécute
# dans le sous-processus pour éviter les effets de bord (logging doublon, Qt DLLs…).
if multiprocessing.current_process().name == 'MainProcess':
    import threading

    # En mode EXE (PyInstaller), stocker les logs dans %LOCALAPPDATA%
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
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger(__name__).critical(
            "Exception non gérée", exc_info=(exc_type, exc_value, exc_tb)
        )

    def _thread_excepthook(args):
        logging.getLogger(__name__).critical(
            f"Exception non gérée dans thread {args.thread.name}",
            exc_info=(args.exc_type, args.exc_value, args.exc_tb),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QDialog, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QListWidget, QListWidgetItem,
        QFileDialog, QFrame, QWidget,
    )
    from PySide6.QtCore import Qt, QPoint, QTimer
    from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap

logger = logging.getLogger(__name__)


def _build_onboarding(config) -> QDialog:
    dlg = QDialog()
    dlg.setWindowTitle("Bienvenue dans PixelPhotoManager")
    dlg.setMinimumWidth(500)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(14)
    layout.setContentsMargins(24, 24, 24, 24)

    title = QLabel("Bienvenue dans PixelPhotoManager !")
    font = QFont()
    font.setPointSize(14)
    font.setBold(True)
    title.setFont(font)
    title.setAlignment(Qt.AlignCenter)
    layout.addWidget(title)

    # --- Dossiers ---
    layout.addWidget(QLabel("Où sont vos photos ? Choisissez au moins un dossier à surveiller."))

    folder_list = QListWidget()
    for f in config.get_scan_folders():
        folder_list.addItem(QListWidgetItem(f))
    layout.addWidget(folder_list)

    btn_row = QHBoxLayout()
    btn_add = QPushButton("+ Ajouter un dossier")

    def _add_folder():
        folder = QFileDialog.getExistingDirectory(dlg, "Choisir un dossier", os.path.expanduser("~"))
        if folder and folder not in [
            folder_list.item(i).text() for i in range(folder_list.count())
        ]:
            folder_list.addItem(QListWidgetItem(folder))

    btn_add.clicked.connect(_add_folder)
    btn_row.addWidget(btn_add)
    btn_row.addStretch()
    layout.addLayout(btn_row)

    # --- Séparateur ---
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setStyleSheet("color: #444;")
    layout.addWidget(sep)

    # --- Reconnaissance faciale ---
    lbl_faces = QLabel("Reconnaissance des personnes")
    font2 = QFont()
    font2.setBold(True)
    lbl_faces.setFont(font2)
    layout.addWidget(lbl_faces)

    chk_faces = QCheckBox(
        "Analyser automatiquement les visages après chaque scan\n"
        "(traitement en arrière-plan — recommandé)"
    )
    chk_faces.setChecked(True)
    layout.addWidget(chk_faces)

    lbl_note = QLabel(
        "La première analyse peut prendre plusieurs minutes selon la taille\n"
        "de votre bibliothèque. Elle n'est faite qu'une seule fois par photo."
    )
    lbl_note.setStyleSheet("color: #888; font-size: 11px;")
    layout.addWidget(lbl_note)

    # --- Bouton démarrer ---
    btn_start = QPushButton("Commencer →")
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

    # Génère l'icône coche pour le style global des QCheckBox
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

    app.setStyleSheet("""
        QToolTip {
            background-color: #2d2d2d;
            color: #eeeeee;
            border: 1px solid #666;
            padding: 4px 6px;
            border-radius: 3px;
        }
        QMainWindow, QDialog, QWidget {
            background-color: #1e1e1e;
            color: #ddd;
        }
        QMenuBar {
            background: #252525;
            color: #ddd;
        }
        QMenuBar::item:selected {
            background: #3a3a3a;
        }
        QMenu {
            background: #252525;
            color: #ddd;
        }
        QMenu::item:selected {
            background: #3a5a8a;
        }
        QToolBar {
            background: #252525;
            border: none;
            spacing: 4px;
            padding: 2px;
        }
        QTreeWidget, QListWidget {
            background: #252525;
            color: #ccc;
            border: none;
        }
        QTreeWidget::item:selected, QListWidget::item:selected {
            background: #3a5a8a;
        }
        QScrollArea {
            background: #1e1e1e;
            border: none;
        }
        QPushButton {
            background: #3a3a3a;
            color: #ddd;
            border: 1px solid #555;
            border-radius: 3px;
            padding: 4px 10px;
        }
        QPushButton:hover {
            background: #4a4a4a;
        }
        QPushButton:pressed {
            background: #2a2a2a;
        }
        QPushButton:checked {
            background: #3a5a8a;
        }
        QLineEdit {
            background: #2a2a2a;
            color: #ddd;
            border: 1px solid #555;
            border-radius: 3px;
            padding: 3px 6px;
        }
        QSlider::groove:horizontal {
            height: 4px;
            background: #555;
            border-radius: 2px;
        }
        QSlider::handle:horizontal {
            width: 12px;
            height: 12px;
            margin: -4px 0;
            background: #7aabdb;
            border-radius: 6px;
        }
        QGroupBox {
            color: #aaa;
            border: 1px solid #444;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 4px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 4px;
        }
        QStatusBar {
            background: #252525;
            color: #aaa;
        }
        QCheckBox {
            color: #ccc;
            spacing: 6px;
        }
        QCheckBox::indicator {
            width: 14px;
            height: 14px;
            border-radius: 2px;
        }
        QCheckBox::indicator:unchecked {
            border: 1px solid #777;
            background: #222232;
        }
        QCheckBox::indicator:unchecked:hover {
            border-color: #bbb;
            background: #2a2a3e;
        }
        QCheckBox::indicator:unchecked:disabled {
            border: 1px solid #444;
            background: #1a1a1a;
        }
        QCheckBox::indicator:checked:disabled {
            border: 1px solid #444;
            background: #1a3060;
        }
        QSplitter::handle {
            background: #333;
        }
        QScrollBar:vertical {
            background: #1e1e1e;
            width: 14px;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            background: #666;
            border-radius: 6px;
            min-height: 30px;
            margin: 2px 2px;
        }
        QScrollBar::handle:vertical:hover {
            background: #888;
        }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {
            height: 0;
        }
        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {
            background: none;
        }
    """ + f"""
        QCheckBox::indicator:checked {{
            border: 1px solid #5577ff;
            background: #2244bb;
            image: url({_check_icon});
        }}
        QCheckBox::indicator:checked:hover {{
            background: #3355cc;
        }}
    """)

    logger.debug("Import des modules internes")
    from src.core.config import Config
    from src.library.catalog import Catalog
    from src.library.thumbnail_cache import ThumbnailCache
    from src.library.scanner import LibraryScanner
    from src.faces.face_database import FaceDatabase
    from src.ui.main_window import MainWindow

    logger.debug("Initialisation Config")
    config = Config()
    logger.debug("Initialisation Catalog")
    catalog = Catalog()
    logger.debug("Initialisation ThumbnailCache")
    thumb_cache = ThumbnailCache()
    _removed = catalog.cleanup_asset_dirs()
    if _removed:
        thumb_cache.invalidate_many(_removed)
    logger.debug("Initialisation LibraryScanner")
    scanner = LibraryScanner(catalog, thumb_cache)
    logger.debug("Initialisation FaceDatabase")
    face_db = FaceDatabase()

    if not config.get_scan_folders():
        logger.debug("Aucun dossier configuré — affichage onboarding")
        dlg = _build_onboarding(config)
        if dlg.exec() == QDialog.Rejected and not config.get_scan_folders():
            logger.info("Onboarding annulé sans dossier — fermeture")
            return  # propre, sans sys.exit()

    logger.debug("Création MainWindow")
    window = MainWindow(config, catalog, thumb_cache, scanner, face_db)
    window.show()

    # Proposer l'import Picasa après le premier affichage de la fenêtre
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
    try:
        logger.info("Démarrage de PixelPhotoManager")
        main()
    except SystemExit:
        pass  # sortie normale de app.exec()
    except BaseException:
        logger.critical("Crash au démarrage", exc_info=True)
        # Forcer le flush avant de quitter
        logging.shutdown()
        sys.exit(1)
