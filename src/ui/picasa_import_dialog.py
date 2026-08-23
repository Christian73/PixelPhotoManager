# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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
from src.core.i18n import translate

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

        self.setWindowTitle(translate("PicasaImportDialog", "Picasa data found"))
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
        title = QLabel(translate("PicasaImportDialog", "Picasa recognition data found"))
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        layout.addWidget(QLabel(
            translate("PicasaImportDialog", "PixelPhotoManager found Picasa face data on "
                                            "this\ncomputer. Would you like to import it?")
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

        lbl_contacts = QLabel("  " + translate(
            "PicasaImportDialog", "%n person(s) in the Picasa database", None, n_contacts))
        lbl_contacts.setStyleSheet("color: #ccc; border: none;")
        lbl_photos = QLabel("  " + translate(
            "PicasaImportDialog", "%n photo(s) with identified faces", None, n_photos))
        lbl_photos.setStyleSheet("color: #ccc; border: none;")
        lbl_edits = QLabel("  " + translate(
            "PicasaImportDialog",
            "%n photo(s) with edits (rotation, cropping, brightness…)",
            None, n_edits))
        lbl_edits.setStyleSheet("color: #ccc; border: none;")
        stats_layout.addWidget(lbl_contacts)
        stats_layout.addWidget(lbl_photos)
        stats_layout.addWidget(lbl_edits)
        layout.addWidget(stats_frame)

        layout.addWidget(QLabel(
            translate("PicasaImportDialog", "The import will create the missing people and "
                                            "record\nthe positions of the Picasa faces. They "
                                            "will be matched\nautomatically during the ArcFace "
                                            "analysis, even a later one.")
        ))

        # Checkbox retouches
        self._chk_edits = QCheckBox(translate("PicasaImportDialog", "Also import the Picasa "
                                                                    "edits (rotation, "
                                                                    "cropping, brightness…)"))
        self._chk_edits.setChecked(n_edits > 0 and self._edit_db is not None)
        self._chk_edits.setEnabled(n_edits > 0 and self._edit_db is not None)
        if self._edit_db is None:
            self._chk_edits.setToolTip(translate("PicasaImportDialog", "Edit database not "
                                                                       "available"))
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

        self._btn_skip = QPushButton(translate("PicasaImportDialog", "Later"))
        self._btn_skip.clicked.connect(self._on_skip)
        self._btn_row.addWidget(self._btn_skip)

        self._btn_row.addStretch()

        self._btn_import = QPushButton(translate("PicasaImportDialog", "Import →"))
        self._btn_import.setDefault(True)
        self._btn_import.clicked.connect(self._on_import)
        self._btn_import.setEnabled(n_photos > 0 or n_edits > 0)
        self._btn_row.addWidget(self._btn_import)

        layout.addLayout(self._btn_row)

    # ------------------------------------------------------------------ handlers

    def _on_skip(self) -> None:
        self.reject()

    def _on_import(self) -> None:
        from src.faces.picasa_importer import PicasaImportThread

        self._btn_import.hide()
        self._btn_skip.setEnabled(False)
        self._progress.show()
        self._lbl_status.setText(translate("PicasaImportDialog", "Scanning the folders…"))
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
        self._lbl_status.setText(translate(
            "PicasaImportDialog", "Processing folder {cur} / {total}…"
            ).format(cur=current, total=total))

    def _on_finished(self, result) -> None:
        self._progress.setValue(100)
        self._config.set("picasa.import_done", True)

        n_persons = result.persons_created
        n_faces   = result.faces_imported
        n_photos  = result.photos_processed
        n_edits   = result.edits_imported
        parts = [
            translate("PicasaImportDialog", "%n person(s) created",
                      None, n_persons),
            translate("PicasaImportDialog", "%n face annotation(s) in {photos}",
                      None, n_faces).format(
                photos=translate("PicasaImportDialog", "%n photo(s)",
                                 None, n_photos)),
        ]
        if result.edits_imported:
            parts.append(translate("PicasaImportDialog", "%n edit(s) imported",
                                   None, n_edits))
        summary = ", ".join(parts) + "."
        self._lbl_status.setText(summary)
        self._lbl_status.setStyleSheet("color: #7fba7f; font-size: 11px;")

        self._btn_skip.setText(translate("PicasaImportDialog", "Close"))
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
    if hasattr(parent, "_act_picasa"):
        parent._act_picasa.setEnabled(not config.get("picasa.import_done", False))
    return True
