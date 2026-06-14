"""
FacePanel — barre latérale affichant les visages identifiés d'une photo.

Apparaît à gauche du PhotoViewer quand le bouton "Visages" est activé.
"""

import logging

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel,
    QMenu, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from src.core.models import FaceInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import (
    _AssignDialog, _cosine_sim, _face_bytes, _SIM_WEAK,
)

logger = logging.getLogger(__name__)

_THUMB  = 72    # taille de la vignette de visage
_WIDTH  = 130   # largeur totale du panneau


# ------------------------------------------------------------------ async loader

class _FacePanelLoader(QThread):
    """Charge les vignettes de visage en arrière-plan.

    Ouvre chaque fichier image UNE SEULE FOIS et en extrait tous les visages,
    évitant de décoder N fois un même JPEG de 20 Mpx pour N visages.
    """
    ready = Signal(int, bytes)   # face_id, PNG bytes

    def __init__(self, items: list[tuple[int, object]], parent=None) -> None:
        super().__init__(parent)
        self._items     = items   # [(face_id, FaceInfo), ...]
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        if not self._items:
            return
        try:
            import io as _io
            from PIL import Image, ImageOps

            # Tous les visages du panneau sont issus de la même photo — une seule ouverture.
            photo_path = self._items[0][1].photo_path
            try:
                base_img = ImageOps.exif_transpose(Image.open(photo_path)).convert("RGB")
            except Exception as exc:
                logger.debug("_FacePanelLoader: impossible d'ouvrir %s: %s", photo_path, exc)
                return

            for face_id, face in self._items:
                if self._stop_flag:
                    break
                try:
                    img = base_img
                    if face.detected_rotation:
                        img = base_img.rotate(-face.detected_rotation, expand=True)
                    x, y, w, h = face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h
                    pad = int(max(w, h) * 0.18)
                    crop = img.crop((
                        max(0, x - pad), max(0, y - pad),
                        min(img.width, x + w + pad), min(img.height, y + h + pad),
                    )).resize((_THUMB, _THUMB))
                    buf = _io.BytesIO()
                    crop.save(buf, format="PNG")
                    data = buf.getvalue()
                    if data:
                        self.ready.emit(face_id, data)
                except Exception as exc:
                    logger.debug("_FacePanelLoader face %s: %s", face_id, exc)
        except Exception as exc:
            logger.debug("_FacePanelLoader: %s", exc)


# ------------------------------------------------------------------ faces data loader

class _FacesDataLoader(QThread):
    """Charge get_faces_for_photo + get_persons depuis un thread secondaire.

    Les dicts sont transmis comme list de tuples pour éviter la coercition
    des clés entières en str par PySide6 lors des connexions cross-thread.
    """
    # photo_path, faces, person_names_items [(int,str)], cluster_persons_items [(int,int)]
    data_ready = Signal(str, list, list, list)

    def __init__(self, face_db: "FaceDatabase", catalog, photo_path: str, parent=None) -> None:
        super().__init__(parent)
        self._face_db     = face_db
        self._catalog     = catalog
        self._photo_path  = photo_path

    def run(self) -> None:
        try:
            faces = [f for f in self._face_db.get_faces_for_photo(self._photo_path)
                     if not f.ignored]

            # Pour les faces sans person_id mais avec un cluster, vérifier si ce cluster
            # a déjà une personne assignée via d'autres faces (cas des faces ré-indexées
            # après qu'une personne a été assignée au cluster).
            unresolved = [
                f.cluster_id for f in faces
                if f.cluster_id is not None and f.cluster_id >= 0 and not f.person_id
            ]
            cluster_persons: dict[int, int] = (
                self._face_db.get_cluster_persons(unresolved) if unresolved else {}
            )

            persons = self._catalog.get_persons()
            person_names_items = [(p.id, p.name) for p in persons]

            self.data_ready.emit(
                self._photo_path, faces,
                person_names_items,
                list(cluster_persons.items()),
            )
        except Exception:
            logger.exception("[FacesDataLoader] exception during load")
            self.data_ready.emit(self._photo_path, [], [], [])


# ------------------------------------------------------------------ face item

_BTN_IGNORE_SZ = 20   # diamètre du bouton ✕

class _FaceItem(QFrame):
    """Un visage dans le panneau : vignette + nom. Supporte le menu contextuel."""

    clicked                = Signal(int)           # face_id  (clic gauche)
    context_menu_requested = Signal(int, object)   # (face_id, QPoint global)
    ignore_requested       = Signal(int)           # face_id  (bouton ✕)

    _STYLE_NORMAL   = "background: transparent; border: none;"
    _STYLE_SELECTED = "background: #1a2f45; border: 2px solid #4a9fd4; border-radius: 4px;"
    _BTN_STYLE = (
        "QPushButton {"
        "  background: rgba(180,30,30,210);"
        "  color: white; border-radius: 10px;"
        "  font-weight: bold; font-size: 12px;"
        "  border: none; padding: 0;"
        "}"
        "QPushButton:hover { background: rgba(220,50,50,240); }"
    )

    def __init__(self, face: FaceInfo, name: str, parent=None) -> None:
        super().__init__(parent)
        self._face    = face
        self._face_id = face.id
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(self._STYLE_NORMAL)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        # Container image : LoadingLabel + bouton ✕ superposé en bas
        img_container = QWidget()
        img_container.setFixedSize(_THUMB, _THUMB)
        img_container.setStyleSheet("background: transparent;")

        self._lbl_img = LoadingLabel("#1a1a1a", img_container)
        self._lbl_img.setGeometry(0, 0, _THUMB, _THUMB)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet("border-radius: 4px; border: none;")
        self._lbl_img.start_loading()

        self._btn_ignore = QPushButton("✕", img_container)
        self._btn_ignore.setGeometry(
            _THUMB - _BTN_IGNORE_SZ - 2,
            _THUMB - _BTN_IGNORE_SZ - 2,
            _BTN_IGNORE_SZ,
            _BTN_IGNORE_SZ,
        )
        self._btn_ignore.setStyleSheet(self._BTN_STYLE)
        self._btn_ignore.setCursor(Qt.PointingHandCursor)
        self._btn_ignore.setToolTip("Ignorer ce visage")
        self._btn_ignore.clicked.connect(lambda: self.ignore_requested.emit(self._face_id))
        self._btn_ignore.raise_()

        layout.addWidget(img_container, alignment=Qt.AlignHCenter)

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

    def set_selected(self, on: bool) -> None:
        self.setStyleSheet(self._STYLE_SELECTED if on else self._STYLE_NORMAL)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._face_id)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(self._face_id, event.globalPos())


# ------------------------------------------------------------------ panel

_TOUS_BTN_STYLE = (
    "QPushButton {"
    "  background: #2a2a2a; color: #aaa;"
    "  border: 2px solid #555; border-radius: 4px;"
    "  margin: 4px 6px; font-size: 12px; font-weight: bold;"
    "}"
    "QPushButton:hover { background: #333; color: #ddd; border-color: #888; }"
    "QPushButton:checked {"
    "  background: #1a3a5a; color: #7ab; border-color: #4a9fd4;"
    "}"
)


class FacePanel(QWidget):
    """
    Panneau latéral affichant les visages détectés dans la photo courante.
    """

    face_highlighted  = Signal(object)  # FaceInfo sélectionné, ou None si désélection
    all_faces_toggled = Signal(list)    # list[FaceInfo] quand "Tous" actif, [] sinon

    def __init__(
        self,
        face_db: FaceDatabase,
        catalog,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db  = face_db
        self._catalog  = catalog
        self._items:        dict[int, _FaceItem] = {}
        self._faces:        dict[int, FaceInfo]  = {}
        self._loader:       _FacePanelLoader | None = None
        self._data_loader:  _FacesDataLoader | None = None
        self._current_photo: str = ""
        self._selected_face_id: int | None = None
        self._setup_ui()

    # ------------------------------------------------------------------ setup

    def _setup_ui(self) -> None:
        self.setFixedWidth(_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header_bar = QWidget()
        header_bar.setStyleSheet("background: #2a2a2a; border-bottom: 1px solid #444;")
        hbox = QHBoxLayout(header_bar)
        hbox.setContentsMargins(6, 4, 6, 4)
        lbl_title = QLabel("Visages")
        lbl_title.setStyleSheet("color: #ccc; font-weight: bold; background: transparent;")
        hbox.addWidget(lbl_title)
        root.addWidget(header_bar)

        self._btn_tous = QPushButton("Tous")
        self._btn_tous.setCheckable(True)
        self._btn_tous.setFixedHeight(34)
        self._btn_tous.setStyleSheet(_TOUS_BTN_STYLE)
        self._btn_tous.setToolTip("Afficher tous les visages dans l'image")
        self._btn_tous.toggled.connect(self._on_tous_toggled)
        root.addWidget(self._btn_tous)

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

    def refresh(self) -> None:
        """Recharger les visages de la photo courante (après modification externe)."""
        if self._current_photo:
            self.set_photo(self._current_photo)

    def set_photo(self, photo_path: str) -> None:
        """Charger et afficher les visages de la photo (asynchrone)."""
        self._current_photo = photo_path
        self._stop_loader()
        self._clear()

        # Toujours déconnecter et libérer l'ancien loader pour éviter l'accumulation
        # de threads orphelins en tant qu'enfants Qt (zombies C++).
        if self._data_loader is not None:
            try:
                self._data_loader.data_ready.disconnect()
            except RuntimeError:
                pass
            if self._data_loader.isRunning():
                self._data_loader.finished.connect(self._data_loader.deleteLater)
            else:
                self._data_loader.deleteLater()
        self._data_loader = _FacesDataLoader(self._face_db, self._catalog, photo_path, self)
        self._data_loader.data_ready.connect(self._on_faces_data_ready)
        self._data_loader.start()

    @Slot(str, list, list, list)
    def _on_faces_data_ready(
        self,
        photo_path: str,
        faces: list,
        person_names_items: list,
        cluster_persons_items: list,
    ) -> None:
        if photo_path != self._current_photo:
            return  # navigation entre-temps

        # Reconstruire les dicts avec des clés int explicites — évite la coercition
        # des clés en str par PySide6 lors de la transmission cross-thread via Signal.
        person_names: dict[int, str] = {int(k): v for k, v in person_names_items}
        cluster_persons: dict[int, int] = {int(k): v for k, v in cluster_persons_items}

        self._clear()
        if not faces:
            lbl = QLabel("Aucun visage\ndétecté")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #555; font-size: 11px; border: none;")
            self._vbox.addWidget(lbl)
            if self._btn_tous.isChecked():
                self.all_faces_toggled.emit([])
            return

        # Trier : visages nommés en premier, puis anonymes ; dans chaque groupe, gauche→droite.
        def _sort_key(f):
            named = bool(
                (f.person_id and f.person_id in person_names)
                or (f.cluster_id is not None and f.cluster_id in cluster_persons)
            )
            return (0 if named else 1, f.bbox_x)

        faces_sorted = sorted(faces, key=_sort_key)

        loader_items = []
        for face in faces_sorted:
            if face.person_id and face.person_id in person_names:
                name = person_names[face.person_id]
            elif face.cluster_id is not None and face.cluster_id in cluster_persons:
                # Face ré-indexée après assignation : le cluster a une personne,
                # mais cette face individuelle n'a pas encore son person_id mis à jour.
                pid = cluster_persons[face.cluster_id]
                name = person_names.get(pid, f"Groupe {face.cluster_id}")
            elif face.pinned:
                name = "Séparé"
            elif face.cluster_id is not None:
                name = f"Groupe {face.cluster_id}"
            else:
                name = "Inconnu"

            item = _FaceItem(face, name, self._content)
            item.clicked.connect(self._on_item_clicked)
            item.context_menu_requested.connect(self._on_item_context_menu)
            item.ignore_requested.connect(self._on_ignore_requested)
            self._vbox.addWidget(item)
            self._items[face.id] = item
            self._faces[face.id] = face
            loader_items.append((face.id, face))

        # Charger les vignettes en arrière-plan
        if loader_items:
            self._loader = _FacePanelLoader(loader_items, self)
            self._loader.ready.connect(self._on_face_ready)
            self._loader.start()

        # Si le mode "Tous" était actif, mettre à jour la liste dans la visionneuse
        if self._btn_tous.isChecked():
            self.all_faces_toggled.emit(list(self._faces.values()))

    def show_face_context_menu(self, face: FaceInfo, gpos) -> None:
        """Construit et affiche le menu contextuel d'un visage.
        Appelé depuis le panneau (via _on_item_context_menu) et depuis la visionneuse."""
        menu = QMenu(self)
        act_assign = menu.addAction("Identifier ce groupe…")
        act_unassign = menu.addAction("Désallouer le groupe")
        act_unassign.setEnabled(
            face.person_id is not None or face.cluster_id is not None
        )
        menu.addSeparator()
        act_ignore = menu.addAction("Ignorer ce visage")

        chosen = menu.exec(gpos)
        if chosen == act_assign:
            self._on_assign_requested(face.id)
        elif chosen == act_unassign:
            self._on_unassign_requested(face.id)
        elif chosen == act_ignore:
            self._on_ignore_requested(face.id)

    def _on_item_context_menu(self, face_id: int, gpos) -> None:
        face = self._faces.get(face_id)
        if face is not None:
            self.show_face_context_menu(face, gpos)

    def _on_tous_toggled(self, checked: bool) -> None:
        if checked:
            # Désélectionner le visage individuel si actif
            if self._selected_face_id is not None and self._selected_face_id in self._items:
                self._items[self._selected_face_id].set_selected(False)
                self._selected_face_id = None
                self.face_highlighted.emit(None)
            self.all_faces_toggled.emit(list(self._faces.values()))
        else:
            self.all_faces_toggled.emit([])

    # ------------------------------------------------------------------ selection

    def _on_item_clicked(self, face_id: int) -> None:
        # Quitter le mode "Tous" sans réémettre (la sélection simple prend le dessus)
        if self._btn_tous.isChecked():
            self._btn_tous.blockSignals(True)
            self._btn_tous.setChecked(False)
            self._btn_tous.blockSignals(False)

        if face_id == self._selected_face_id:
            # Désélection (toggle)
            self._items[face_id].set_selected(False)
            self._selected_face_id = None
            self.face_highlighted.emit(None)
        else:
            # Désélection de l'ancien
            if self._selected_face_id is not None and self._selected_face_id in self._items:
                self._items[self._selected_face_id].set_selected(False)
            # Sélection du nouveau
            self._selected_face_id = face_id
            self._items[face_id].set_selected(True)
            self.face_highlighted.emit(self._faces.get(face_id))

    # ------------------------------------------------------------------ context menu handlers

    def _on_assign_requested(self, face_id: int) -> None:
        face = self._faces.get(face_id)
        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)

        # Calcul de la suggestion à partir de l'embedding du cluster du visage
        suggested_id = None
        if face and face.cluster_id is not None and face.cluster_id >= 0:
            c_emb = self._face_db.get_representative_embedding(cluster_id=face.cluster_id)
            if c_emb:
                best_sim, best_id = 0.0, None
                for p in persons:
                    p_emb = self._face_db.get_representative_embedding(person_id=p.id)
                    if p_emb:
                        sim = _cosine_sim(c_emb, p_emb)
                        if sim > best_sim:
                            best_sim, best_id = sim, p.id
                if best_sim >= _SIM_WEAK:
                    suggested_id = best_id

        dlg = _AssignDialog(
            face_id, persons,
            suggested_person_id=suggested_id,
            show_ignore=False,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_new_person():
            person = self._catalog.create_person(dlg.new_name())
            person_id = person.id
        else:
            person_id = dlg.existing_person_id()

        if (
            face is not None
            and face.cluster_id is not None
            and face.cluster_id >= 0
            and not face.pinned
        ):
            logger.debug(
                "[FacePanel] assign_person_to_cluster cluster=%s person=%s",
                face.cluster_id, person_id,
            )
            self._face_db.assign_person_to_cluster(face.cluster_id, person_id)
        logger.debug(
            "[FacePanel] assign_person_to_face face=%s person=%s",
            face_id, person_id,
        )
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

    def _on_set_cover_requested(self, face_id: int) -> None:
        self._face_db.set_cover_face(face_id)

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
        self._faces.clear()
        self._selected_face_id = None

    def _stop_loader(self) -> None:
        if self._loader is not None:
            self._loader.stop()
            if self._loader.isRunning():
                try:
                    self._loader.ready.disconnect(self._on_face_ready)
                except RuntimeError:
                    pass
                self._loader.finished.connect(self._loader.deleteLater)
            else:
                self._loader.deleteLater()
            self._loader = None
