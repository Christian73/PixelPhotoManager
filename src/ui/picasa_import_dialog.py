"""
Dialogue d'import des données Picasa (visages + personnes).
Peut être déclenché au démarrage ou manuellement depuis le menu Visages.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFrame, QLabel,
    QProgressBar, QPushButton, QVBoxLayout, QHBoxLayout,
)

logger = logging.getLogger(__name__)


class PicasaImportDialog(QDialog):
    """
    Propose l'import des données de reconnaissance faciale Picasa.

    Paramètres
    ----------
    config   : Config — pour lire les dossiers et écrire le flag d'import
    catalog  : Catalog
    face_db  : FaceDatabase
    parent   : widget parent optionnel
    """

    def __init__(self, config, catalog, face_db, edit_db=None, parent=None,
                 on_edits_imported=None) -> None:
        super().__init__(parent)
        self._config             = config
        self._catalog            = catalog
        self._face_db            = face_db
        self._edit_db            = edit_db
        self._thread             = None
        self._on_edits_imported  = on_edits_imported  # callable({path: EditInfo}) | None

        self.setWindowTitle("Données Picasa détectées")
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self._setup_ui()

    # ------------------------------------------------------------------ ui

    def _setup_ui(self) -> None:
        from src.faces.picasa_importer import scan

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 20)

        # Title
        title = QLabel("Données de reconnaissance Picasa détectées")
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        layout.addWidget(QLabel(
            "PixelPhotoManager a trouvé des données de visages Picasa\n"
            "sur cet ordinateur. Souhaitez-vous les importer ?"
        ))

        # Stats box
        folders = self._config.get_scan_folders()
        n_contacts, n_photos, n_edits = scan(folders)

        stats_frame = QFrame()
        stats_frame.setFrameShape(QFrame.StyledPanel)
        stats_frame.setStyleSheet(
            "QFrame { background: #2a2a2a; border: 1px solid #444; border-radius: 4px; }"
        )
        stats_layout = QVBoxLayout(stats_frame)
        stats_layout.setContentsMargins(14, 10, 14, 10)
        stats_layout.setSpacing(4)

        lbl_contacts = QLabel(f"  {n_contacts} personne(s) dans la base Picasa")
        lbl_contacts.setStyleSheet("color: #ccc; border: none;")
        lbl_photos = QLabel(f"  {n_photos} photo(s) avec des visages identifiés")
        lbl_photos.setStyleSheet("color: #ccc; border: none;")
        lbl_edits = QLabel(f"  {n_edits} photo(s) avec des retouches (rotation, recadrage, luminosité…)")
        lbl_edits.setStyleSheet("color: #ccc; border: none;")
        stats_layout.addWidget(lbl_contacts)
        stats_layout.addWidget(lbl_photos)
        stats_layout.addWidget(lbl_edits)
        layout.addWidget(stats_frame)

        layout.addWidget(QLabel(
            "L'import créera les personnes manquantes et enregistrera\n"
            "les positions des visages Picasa. Elles seront associées\n"
            "automatiquement lors de l'analyse ArcFace, même ultérieure."
        ))

        # Checkbox retouches
        self._chk_edits = QCheckBox("Importer aussi les retouches Picasa (rotation, recadrage, luminosité…)")
        self._chk_edits.setChecked(n_edits > 0 and self._edit_db is not None)
        self._chk_edits.setEnabled(n_edits > 0 and self._edit_db is not None)
        if self._edit_db is None:
            self._chk_edits.setToolTip("Base de retouches non disponible")
        layout.addWidget(self._chk_edits)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #444;")
        layout.addWidget(sep)

        # Progress bar (hidden until import starts)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.hide()
        layout.addWidget(self._progress)

        self._lbl_status = QLabel("")
        self._lbl_status.setAlignment(Qt.AlignCenter)
        self._lbl_status.setWordWrap(True)
        self._lbl_status.setStyleSheet("color: #888; font-size: 11px;")
        self._lbl_status.hide()
        layout.addWidget(self._lbl_status)

        # Buttons
        self._btn_row = QHBoxLayout()
        self._btn_row.setSpacing(10)

        self._btn_skip = QPushButton("Plus tard")
        self._btn_skip.clicked.connect(self._on_skip)
        self._btn_row.addWidget(self._btn_skip)

        self._btn_row.addStretch()

        self._btn_import = QPushButton("Importer →")
        self._btn_import.setDefault(True)
        self._btn_import.clicked.connect(self._on_import)
        self._btn_import.setEnabled(n_photos > 0 or n_edits > 0)
        self._btn_row.addWidget(self._btn_import)

        layout.addLayout(self._btn_row)

    # ------------------------------------------------------------------ handlers

    def _on_skip(self) -> None:
        self._config.set("picasa.import_done", True)
        self.reject()

    def _on_import(self) -> None:
        from src.faces.picasa_importer import PicasaImportThread

        self._btn_import.hide()
        self._btn_skip.setEnabled(False)
        self._progress.show()
        self._lbl_status.setText("Analyse des dossiers en cours…")
        self._lbl_status.show()

        folders  = self._config.get_scan_folders()
        edit_db  = self._edit_db if self._chk_edits.isChecked() else None
        self._thread = PicasaImportThread(self._catalog, self._face_db, folders, edit_db, self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_finished)
        self._thread.start()

    def _on_progress(self, current: int, total: int) -> None:
        if total > 0:
            self._progress.setValue(int(current / total * 100))
        self._lbl_status.setText(f"Traitement du dossier {current} / {total}…")

    def _on_finished(self, result) -> None:
        self._progress.setValue(100)
        self._config.set("picasa.import_done", True)

        parts = [
            f"{result.persons_created} personne(s) créée(s)",
            f"{result.faces_imported} annotation(s) de visage dans {result.photos_processed} photo(s)",
        ]
        if result.edits_imported:
            parts.append(f"{result.edits_imported} retouche(s) importée(s)")
        summary = ", ".join(parts) + "."
        self._lbl_status.setText(summary)
        self._lbl_status.setStyleSheet("color: #7fba7f; font-size: 11px;")

        self._btn_skip.setText("Fermer")
        self._btn_skip.setEnabled(True)
        self._btn_skip.clicked.disconnect()
        self._btn_skip.clicked.connect(self.accept)
        self._btn_row.insertStretch(0)  # centre le bouton Fermer

        if self._on_edits_imported and result.edited_map:
            self._on_edits_imported(result.edited_map)

        logger.info("Import Picasa terminé : %s", summary)


# ------------------------------------------------------------------ helper

def check_and_prompt(config, catalog, face_db, edit_db=None, parent=None,
                     on_edits_imported=None) -> bool:
    """
    Show the Picasa import dialog if data is available and hasn't been imported yet.

    Returns True if the dialog was shown (regardless of outcome).
    """
    if config.get("picasa.import_done", False):
        return False

    from src.faces.picasa_importer import scan
    folders = config.get_scan_folders()
    if not folders:
        return False

    n_contacts, n_photos, n_edits = scan(folders)
    if n_contacts == 0 and n_photos == 0 and n_edits == 0:
        return False

    dlg = PicasaImportDialog(config, catalog, face_db, edit_db, parent,
                             on_edits_imported=on_edits_imported)
    dlg.exec()
    return True
