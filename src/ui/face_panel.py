"""
FacePanel — barre latérale affichant les visages identifiés d'une photo.

Apparaît à gauche du PhotoViewer quand le bouton "Visages" est activé.
"""

import logging

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QLabel, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from src.faces.face_database import FaceDatabase
from src.ui.people_panel import _face_bytes

logger = logging.getLogger(__name__)

_THUMB  = 72    # taille de la vignette de visage
_WIDTH  = 130   # largeur totale du panneau


# ------------------------------------------------------------------ async loader

class _FacePanelLoader(QThread):
    """Charge les vignettes de visage en arrière-plan."""
    ready = Signal(int, bytes)   # face_id, PNG bytes

    def __init__(self, items: list[tuple[int, object]], parent=None) -> None:
        super().__init__(parent)
        self._items = items   # [(face_id, FaceInfo), ...]

    def run(self) -> None:
        for face_id, face in self._items:
            data = _face_bytes(face, _THUMB)
            if data:
                self.ready.emit(face_id, data)


# ------------------------------------------------------------------ face item

class _FaceItem(QFrame):
    """Un visage dans le panneau : vignette + nom."""

    def __init__(self, face_id: int, name: str, parent=None) -> None:
        super().__init__(parent)
        self._face_id = face_id
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        self._lbl_img = QLabel()
        self._lbl_img.setFixedSize(_THUMB, _THUMB)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet(
            "background: #1a1a1a; border-radius: 4px; border: none;"
        )
        placeholder = QPixmap(_THUMB, _THUMB)
        placeholder.fill(Qt.darkGray)
        self._lbl_img.setPixmap(placeholder)
        layout.addWidget(self._lbl_img, alignment=Qt.AlignHCenter)

        lbl_name = QLabel(name)
        lbl_name.setAlignment(Qt.AlignCenter)
        lbl_name.setWordWrap(True)
        lbl_name.setMaximumWidth(_WIDTH - 8)
        lbl_name.setStyleSheet("font-size: 11px; color: #ccc; border: none;")
        layout.addWidget(lbl_name)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333; border: none; background: #333;")
        sep.setFixedHeight(1)
        layout.addWidget(sep)

    def set_image(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(_THUMB, _THUMB,
                            Qt.KeepAspectRatioByExpanding,
                            Qt.SmoothTransformation)
        self._lbl_img.setPixmap(scaled)


# ------------------------------------------------------------------ panel

class FacePanel(QWidget):
    """
    Panneau latéral affichant les visages détectés dans la photo courante.
    """

    def __init__(
        self,
        face_db: FaceDatabase,
        catalog,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._items:  dict[int, _FaceItem] = {}
        self._loader: _FacePanelLoader | None = None
        self._setup_ui()

    # ------------------------------------------------------------------ setup

    def _setup_ui(self) -> None:
        self.setFixedWidth(_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QLabel("Visages")
        header.setAlignment(Qt.AlignCenter)
        header.setStyleSheet(
            "background: #2a2a2a; color: #ccc; font-weight: bold;"
            " padding: 5px; border-bottom: 1px solid #444;"
        )
        root.addWidget(header)

        self._content = QWidget()
        self._content.setStyleSheet("background: #1e1e1e;")
        self._vbox = QVBoxLayout(self._content)
        self._vbox.setContentsMargins(0, 4, 0, 4)
        self._vbox.setSpacing(0)
        self._vbox.setAlignment(Qt.AlignTop)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._content)
        scroll.setStyleSheet("border: none;")
        root.addWidget(scroll)

    # ------------------------------------------------------------------ public

    def set_photo(self, photo_path: str) -> None:
        """Charger et afficher les visages de la photo."""
        self._stop_loader()
        self._clear()

        faces = self._face_db.get_faces_for_photo(photo_path)
        if not faces:
            lbl = QLabel("Aucun visage\ndétecté")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #555; font-size: 11px; border: none;")
            self._vbox.addWidget(lbl)
            return

        # Résoudre les noms une fois
        person_names: dict[int, str] = {}
        if any(f.person_id for f in faces):
            persons = self._catalog.get_persons()
            person_names = {p.id: p.name for p in persons}

        # Trier de gauche à droite (bbox_x)
        faces_sorted = sorted(faces, key=lambda f: f.bbox_x)

        loader_items = []
        for face in faces_sorted:
            if face.person_id and face.person_id in person_names:
                name = person_names[face.person_id]
            elif face.cluster_id is not None:
                name = f"Groupe {face.cluster_id}"
            else:
                name = "Inconnu"

            item = _FaceItem(face.id, name, self._content)
            self._vbox.addWidget(item)
            self._items[face.id] = item
            loader_items.append((face.id, face))

        # Charger les vignettes en arrière-plan
        if loader_items:
            self._loader = _FacePanelLoader(loader_items, self)
            self._loader.ready.connect(self._on_face_ready)
            self._loader.start()

    # ------------------------------------------------------------------ internal

    def _on_face_ready(self, face_id: int, data: bytes) -> None:
        item = self._items.get(face_id)
        if item:
            item.set_image(data)

    def _clear(self) -> None:
        while self._vbox.count():
            child = self._vbox.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._items.clear()

    def _stop_loader(self) -> None:
        if self._loader and self._loader.isRunning():
            try:
                self._loader.ready.disconnect(self._on_face_ready)
            except RuntimeError:
                pass
            self._loader = None
