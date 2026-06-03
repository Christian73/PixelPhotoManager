import logging
import os
import sys
import traceback
from pathlib import Path

_LOG_PATH = Path(__file__).parent / "logs" / "photomanager.log"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

# Réduire les bibliothèques tierces trop verboses au niveau WARNING
for _noisy in ("PIL", "PIL.Image", "PIL.PngImagePlugin", "PIL.JpegImagePlugin",
               "PIL.TiffImagePlugin", "PIL.WebPImagePlugin"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


import threading

def _excepthook(exc_type, exc_value, exc_tb):
    logger.critical("Exception non gérée", exc_info=(exc_type, exc_value, exc_tb))

def _thread_excepthook(args):
    logger.critical(
        f"Exception non gérée dans thread {args.thread.name}",
        exc_info=(args.exc_type, args.exc_value, args.exc_tb),
    )

sys.excepthook = _excepthook
threading.excepthook = _thread_excepthook

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem,
    QFileDialog, QWidget,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


def _build_onboarding(config) -> QDialog:
    dlg = QDialog()
    dlg.setWindowTitle("Bienvenue dans PhotoManager")
    dlg.setMinimumWidth(480)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)
    layout.setContentsMargins(24, 24, 24, 24)

    title = QLabel("Bienvenue dans PhotoManager !")
    font = QFont()
    font.setPointSize(14)
    font.setBold(True)
    title.setFont(font)
    title.setAlignment(Qt.AlignCenter)
    layout.addWidget(title)

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

    btn_start = QPushButton("Commencer →")
    btn_start.setDefault(True)

    def _start():
        for i in range(folder_list.count()):
            config.add_scan_folder(folder_list.item(i).text())
        dlg.accept()

    btn_start.clicked.connect(_start)
    layout.addWidget(btn_start, alignment=Qt.AlignRight)

    return dlg


def main() -> None:
    logger.debug("Création QApplication")
    app = QApplication(sys.argv)
    app.setApplicationName("PhotoManager")
    app.setOrganizationName("PhotoManager")

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
        QSplitter::handle {
            background: #333;
        }
        QScrollBar:vertical {
            background: #252525;
            width: 8px;
        }
        QScrollBar::handle:vertical {
            background: #555;
            border-radius: 4px;
            min-height: 20px;
        }
    """)

    logger.debug("Import des modules internes")
    from src.core.config import Config
    from src.library.catalog import Catalog
    from src.library.thumbnail_cache import ThumbnailCache
    from src.library.scanner import LibraryScanner
    from src.ui.main_window import MainWindow

    logger.debug("Initialisation Config")
    config = Config()
    logger.debug("Initialisation Catalog")
    catalog = Catalog()
    logger.debug("Initialisation ThumbnailCache")
    thumb_cache = ThumbnailCache()
    logger.debug("Initialisation LibraryScanner")
    scanner = LibraryScanner(catalog, thumb_cache)

    if not config.get_scan_folders():
        logger.debug("Aucun dossier configuré — affichage onboarding")
        dlg = _build_onboarding(config)
        if dlg.exec() == QDialog.Rejected and not config.get_scan_folders():
            logger.info("Onboarding annulé sans dossier — fermeture")
            return  # propre, sans sys.exit()

    logger.debug("Création MainWindow")
    window = MainWindow(config, catalog, thumb_cache, scanner)
    window.show()
    logger.debug("Entrée dans la boucle Qt")
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        logger.info("Démarrage de PhotoManager")
        main()
    except SystemExit:
        pass  # sortie normale de app.exec()
    except BaseException:
        logger.critical("Crash au démarrage", exc_info=True)
        # Forcer le flush avant de quitter
        logging.shutdown()
        sys.exit(1)
