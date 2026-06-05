"""
FacePanel — barre latérale affichant les visages identifiés d'une photo.

Apparaît à gauche du PhotoViewer quand le bouton "Visages" est activé.
"""

import logging

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFrame, QLabel, QLineEdit,
    QMenu, QRadioButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from src.core.models import FaceInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import _face_bytes, _RADIO_STYLE

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


# ------------------------------------------------------------------ assign dialog

class _FaceAssignDialog(QDialog):
    """
    Dialogue d'affectation d'un visage à une personne.
    Propose les personnes existantes et la création d'une nouvelle.
    """

    def __init__(self, existing_persons, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ré-affecter à une autre personne")
        self.setMinimumWidth(320)
        self.setStyleSheet(_RADIO_STYLE)
        self._selected_person_id = None
        self._new_name = ""
        self._setup_ui(existing_persons)

    def _setup_ui(self, persons) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(QLabel("Ré-affecter ce visage à :"))

        self._btn_group = QButtonGroup(self)

        if persons:
            lbl = QLabel("Personne existante :")
            lbl.setStyleSheet("color: #aaa; font-size: 11px;")
            layout.addWidget(lbl)
            for p in persons:
                rb = QRadioButton(
                    f"{p.name}  ({p.photo_count} photo{'s' if p.photo_count != 1 else ''})"
                )
                rb.setProperty("person_id", p.id)
                self._btn_group.addButton(rb)
                layout.addWidget(rb)

            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet("color: #444;")
            layout.addWidget(sep)

        rb_new = QRadioButton("Créer une nouvelle personne :")
        self._btn_group.addButton(rb_new)
        self._rb_new = rb_new
        layout.addWidget(rb_new)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Nom de la personne…")
        self._name_input.textChanged.connect(lambda: rb_new.setChecked(True))
        _orig_focus = self._name_input.focusInEvent
        self._name_input.focusInEvent = lambda e: (rb_new.setChecked(True), _orig_focus(e))
        layout.addWidget(self._name_input)

        if not persons:
            rb_new.setChecked(True)
        else:
            self._btn_group.buttons()[0].setChecked(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        checked = self._btn_group.checkedButton()
        if checked is self._rb_new:
            name = self._name_input.text().strip()
            if not name:
                self._name_input.setFocus()
                return
            self._new_name = name
        else:
            self._selected_person_id = checked.property("person_id")
        self.accept()

    def is_new_person(self) -> bool:
        return self._selected_person_id is None

    def new_name(self) -> str:
        return self._new_name

    def existing_person_id(self) -> int | None:
        return self._selected_person_id


# ------------------------------------------------------------------ face item

class _FaceItem(QFrame):
    """Un visage dans le panneau : vignette + nom. Supporte le menu contextuel."""

    assign_requested   = Signal(int)   # face_id
    unassign_requested = Signal(int)   # face_id
    ignore_requested   = Signal(int)   # face_id
    isolate_requested  = Signal(int)   # face_id

    def __init__(self, face: FaceInfo, name: str, parent=None) -> None:
        super().__init__(parent)
        self._face    = face
        self._face_id = face.id
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        self._lbl_img = LoadingLabel("#1a1a1a")
        self._lbl_img.setFixedSize(_THUMB, _THUMB)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet("border-radius: 4px; border: none;")
        self._lbl_img.start_loading()
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

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        label_assign = "Ré-affecter à une autre personne…" if self._face.person_id else "Affecter à une personne…"
        act_assign   = menu.addAction(label_assign)
        act_unassign = menu.addAction("Désallouer")
        act_unassign.setEnabled(
            self._face.person_id is not None
            or self._face.cluster_id is not None
        )
        # "Séparer" : disponible si dans un groupe normal (cluster_id >= 0) et non déjà isolé
        in_normal_cluster = (
            self._face.cluster_id is not None
            and self._face.cluster_id >= 0
            and not self._face.pinned
        )
        act_isolate = menu.addAction("Séparer ce visage du groupe")
        act_isolate.setEnabled(in_normal_cluster)
        menu.addSeparator()
        act_ignore = menu.addAction("Ignorer ce visage")

        chosen = menu.exec(event.globalPos())
        if chosen == act_assign:
            self.assign_requested.emit(self._face_id)
        elif chosen == act_unassign:
            self.unassign_requested.emit(self._face_id)
        elif chosen == act_isolate:
            self.isolate_requested.emit(self._face_id)
        elif chosen == act_ignore:
            self.ignore_requested.emit(self._face_id)


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
        self._face_db  = face_db
        self._catalog  = catalog
        self._items:   dict[int, _FaceItem] = {}
        self._loader:  _FacePanelLoader | None = None
        self._current_photo: str = ""
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
        self._current_photo = photo_path
        self._stop_loader()
        self._clear()

        faces = [f for f in self._face_db.get_faces_for_photo(photo_path) if not f.ignored]
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
            elif face.pinned:
                name = "Séparé"
            elif face.cluster_id is not None:
                name = f"Groupe {face.cluster_id}"
            else:
                name = "Inconnu"

            item = _FaceItem(face, name, self._content)
            item.assign_requested.connect(self._on_assign_requested)
            item.unassign_requested.connect(self._on_unassign_requested)
            item.ignore_requested.connect(self._on_ignore_requested)
            item.isolate_requested.connect(self._on_isolate_requested)
            self._vbox.addWidget(item)
            self._items[face.id] = item
            loader_items.append((face.id, face))

        # Charger les vignettes en arrière-plan
        if loader_items:
            self._loader = _FacePanelLoader(loader_items, self)
            self._loader.ready.connect(self._on_face_ready)
            self._loader.start()

    # ------------------------------------------------------------------ context menu handlers

    def _on_assign_requested(self, face_id: int) -> None:
        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)

        dlg = _FaceAssignDialog(persons, self)
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_new_person():
            person = self._catalog.create_person(dlg.new_name())
            person_id = person.id
        else:
            person_id = dlg.existing_person_id()

        self._face_db.assign_person_to_face(face_id, person_id)
        self.set_photo(self._current_photo)

    def _on_unassign_requested(self, face_id: int) -> None:
        self._face_db.unassign_face(face_id)
        self.set_photo(self._current_photo)

    def _on_ignore_requested(self, face_id: int) -> None:
        self._face_db.ignore_face(face_id)
        self.set_photo(self._current_photo)

    def _on_isolate_requested(self, face_id: int) -> None:
        self._face_db.isolate_face(face_id)
        self.set_photo(self._current_photo)

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
