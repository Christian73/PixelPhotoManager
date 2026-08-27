# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
FacePanel — side bar showing the identified faces of a photo.

Appears to the left of the PhotoViewer when the "Faces" button is enabled.
"""

import logging

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QMenu, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from src.core.models import FaceInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.ui_utils import install_menu_width_fix
from src.ui.people_panel import (
    _AssignDialog, _cosine_sim, _face_bytes, _load_edit_rotations,
    _SIM_STRONG, _SIM_WEAK,
)
from src.core.i18n import translate

logger = logging.getLogger(__name__)

_THUMB  = 72    # size of the face thumbnail
_WIDTH  = 130   # total width of the panel


# ------------------------------------------------------------------ async loader

class _FacePanelLoader(QThread):
    """Loads the face thumbnails in the background.

    Opens each image file ONLY ONCE and extracts every face from it, avoiding
    decoding the same 20 Mpx JPEG N times for N faces.
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

            # Every face of the panel comes from the same photo — a single open.
            photo_path = self._items[0][1].photo_path
            from pathlib import Path as _Path
            from src.library.exif_reader import VIDEO_EXT as _VIDEO_EXT
            if _Path(photo_path).suffix.lower() in _VIDEO_EXT:
                return
            edit_rot = _load_edit_rotations([photo_path]).get(photo_path, 0)
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
                    ))
                    net = (face.detected_rotation - edit_rot) % 360
                    if net:
                        crop = crop.rotate(net, expand=True)
                    crop = crop.resize((_THUMB, _THUMB))
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
    """Loads get_faces_for_photo + get_persons from a secondary thread.

    The dicts are transmitted as lists of tuples to avoid the coercion of the
    integer keys to str by PySide6 during cross-thread connections.
    """
    # photo_path, faces, person_names_items [(int,str)], cluster_persons_items [(int,int)],
    # probable_items [(face_id, (person_id, score))], ignored_count, edit_rotation
    data_ready = Signal(str, list, list, list, list, int, int)

    def __init__(self, face_db: "FaceDatabase", catalog, photo_path: str, parent=None) -> None:
        super().__init__(parent)
        self._face_db     = face_db
        self._catalog     = catalog
        self._photo_path  = photo_path

    def run(self) -> None:
        try:
            faces = [f for f in self._face_db.get_faces_for_photo(self._photo_path)
                     if not f.ignored]

            # For the faces with no person_id but with a cluster, check whether that
            # cluster already has a person assigned through other faces (the case of
            # faces re-indexed after a person was assigned to the cluster).
            unresolved = [
                f.cluster_id for f in faces
                if f.cluster_id is not None and f.cluster_id >= 0 and not f.person_id
            ]
            cluster_persons: dict[int, int] = (
                self._face_db.get_cluster_persons(unresolved) if unresolved else {}
            )

            persons = self._catalog.get_persons()
            person_names_items = [(p.id, p.name) for p in persons]

            # Informative "≈ Probably/Maybe X" label (threshold _SIM_WEAK=0.45,
            # cf. people_panel.py/CLAUDE.md) for the faces that have neither an assigned
            # person (directly or through their cluster) nor a persisted suggestion
            # (>= _SIM_SUGGEST=0.55, handled separately by the ✓/✕ ticks) — purely
            # informative here, no automatic tick given the still low confidence.
            probable_items: list[tuple[int, tuple[int, float]]] = []
            candidates = [
                f for f in faces
                if not f.person_id
                and f.cluster_id is not None
                and f.cluster_id not in cluster_persons
                and f.suggestion_person_id is None
                and not f.pinned
            ]
            if candidates:
                person_ids = [p.id for p in persons if p.id is not None]
                person_centroids = (
                    self._face_db.get_all_person_centroids(person_ids) if person_ids else {}
                )
                if person_centroids:
                    for f in candidates:
                        c_emb = self._face_db.get_representative_embedding(cluster_id=f.cluster_id)
                        if not c_emb:
                            continue
                        best_sim, best_id = 0.0, None
                        for pid, p_emb in person_centroids.items():
                            sim = _cosine_sim(c_emb, p_emb)
                            if sim > best_sim:
                                best_sim, best_id = sim, pid
                        if best_id is not None and best_sim >= _SIM_WEAK:
                            probable_items.append((f.id, (best_id, best_sim)))

            ignored_count = len(
                self._face_db.get_ignored_faces_for_photo(self._photo_path)
            )

            # Pending (not baked) edit rotation: it influences the rendering of the
            # thumbnail (cf. _FacePanelLoader) without touching the stored bboxes — it is
            # part of the validity key of the thumbnail cache (cf. _thumb_cache).
            edit_rotation = _load_edit_rotations([self._photo_path]).get(self._photo_path, 0)

            self.data_ready.emit(
                self._photo_path, faces,
                person_names_items,
                list(cluster_persons.items()),
                probable_items,
                ignored_count,
                edit_rotation,
            )
        except Exception:
            logger.exception("[FacesDataLoader] exception during load")
            self.data_ready.emit(self._photo_path, [], [], [], [], 0, 0)


class _AssignPrepLoader(QThread):
    """Prepares the name assignment popup off the UI thread.

    get_persons + enrich_persons_photo_count + computing the suggested person
    (a comparison of centroids) involve queries over ~60k faces; running them
    on the UI thread made the popup appear several seconds late (or even froze
    the window for the duration of the computation)."""

    ready = Signal(list, object)   # persons: list[PersonInfo], suggested_person_id | None

    def __init__(
        self, catalog, face_db: "FaceDatabase", face: "FaceInfo | None", parent=None
    ) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._face_db = face_db
        self._face    = face

    def run(self) -> None:
        try:
            persons = self._catalog.get_persons()
            self._face_db.enrich_persons_photo_count(persons)
            suggested_id = None
            face = self._face
            if face is not None and face.cluster_id is not None and face.cluster_id >= 0:
                c_emb = self._face_db.get_representative_embedding(cluster_id=face.cluster_id)
                person_ids = [p.id for p in persons if p.id is not None]
                if c_emb and person_ids:
                    person_centroids = self._face_db.get_all_person_centroids(person_ids)
                    best_sim, best_id = 0.0, None
                    for p in persons:
                        p_emb = person_centroids.get(p.id)
                        if p_emb:
                            sim = _cosine_sim(c_emb, p_emb)
                            if sim > best_sim:
                                best_sim, best_id = sim, p.id
                    suggested_id = best_id if best_sim >= _SIM_WEAK else None
            self.ready.emit(persons, suggested_id)
        except Exception:
            logger.exception("[AssignPrepLoader] exception during load")
            self.ready.emit([], None)


class _DbWriteWorker(QThread):
    """Runs a FaceDatabase write off the UI thread.

    The person assignments (a whole group) trigger, in a transaction, the
    deduplication of the overlapping faces and the consumption of the Picasa
    annotations on every photo of the group — potentially long on a large
    group. The panel refreshes through finished (cf. FacePanel._run_db_write)."""

    def __init__(self, fn, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self._fn()
        except Exception:
            logger.exception("[FacePanel] écriture DB en arrière-plan échouée")


# ------------------------------------------------------------------ face item

_BTN_IGNORE_SZ = 20   # diameter of the ✕ button

class _FaceItem(QFrame):
    """A face in the panel: thumbnail + name. Supports the context menu."""

    clicked                     = Signal(int)           # face_id  (left click)
    double_clicked              = Signal(int)           # face_id  (left double-click)
    context_menu_requested      = Signal(int, object)   # (face_id, global QPoint)
    ignore_requested            = Signal(int)           # face_id  (✕ button, bottom
                                                        # right, absent when confirmed)
    suggestion_accept_requested = Signal(int)           # face_id  (✓ suggestion button)
    suggestion_reject_requested = Signal(int)           # face_id  (✕ suggestion button)

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
    _BTN_ACCEPT_STYLE = (
        "QPushButton {"
        "  background: rgba(30,150,30,210);"
        "  color: white; border-radius: 10px;"
        "  font-weight: bold; font-size: 12px;"
        "  border: none; padding: 0;"
        "}"
        "QPushButton:hover { background: rgba(50,190,50,240); }"
    )
    _BTN_REJECT_STYLE = (
        "QPushButton {"
        "  background: rgba(180,30,30,210);"
        "  color: white; border-radius: 10px;"
        "  font-weight: bold; font-size: 12px;"
        "  border: none; padding: 0;"
        "}"
        "QPushButton:hover { background: rgba(220,50,50,240); }"
    )

    def __init__(
        self, face: FaceInfo, name: str, parent=None,
        *, suggestion: bool = False, name_color: "str | None" = None,
        confirmed: bool = False,
    ) -> None:
        super().__init__(parent)
        self._face    = face
        self._face_id = face.id
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(self._STYLE_NORMAL)
        self.setAccessibleName(f"faceitem::{face.id}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        # Image container: LoadingLabel + ✕ button overlaid at the bottom
        img_container = QWidget()
        img_container.setFixedSize(_THUMB, _THUMB)
        img_container.setStyleSheet("background: transparent;")

        self._lbl_img = LoadingLabel("#1a1a1a", img_container)
        self._lbl_img.setGeometry(0, 0, _THUMB, _THUMB)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet("border-radius: 4px; border: none;")
        self._lbl_img.start_loading()

        # The quick-ignore cross is a shortcut for triaging what is NOT settled yet
        # (anonymous face, group, suggestion awaiting verification). On a confirmed
        # identification it offers nothing but the risk of a misclick on a face the
        # user has just validated: ignoring it stays available through the context
        # menu ("Ignore this face"), which is deliberate and unconditional there.
        if not confirmed:
            self._btn_ignore = QPushButton("✕", img_container)
            self._btn_ignore.setGeometry(
                _THUMB - _BTN_IGNORE_SZ - 2,
                _THUMB - _BTN_IGNORE_SZ - 2,
                _BTN_IGNORE_SZ,
                _BTN_IGNORE_SZ,
            )
            self._btn_ignore.setStyleSheet(self._BTN_STYLE)
            self._btn_ignore.setCursor(Qt.PointingHandCursor)
            self._btn_ignore.setToolTip(translate("FaceItem", "Ignore this face"))
            self._btn_ignore.clicked.connect(lambda: self.ignore_requested.emit(self._face_id))
            self._btn_ignore.raise_()

        if suggestion:
            self._btn_accept = QPushButton("✓", img_container)
            self._btn_accept.setGeometry(2, 2, _BTN_IGNORE_SZ, _BTN_IGNORE_SZ)
            self._btn_accept.setStyleSheet(self._BTN_ACCEPT_STYLE)
            self._btn_accept.setCursor(Qt.PointingHandCursor)
            self._btn_accept.setToolTip(translate("FaceItem", "Confirm this person (the whole "
                                                              "group)"))
            self._btn_accept.clicked.connect(
                lambda: self.suggestion_accept_requested.emit(self._face_id)
            )
            self._btn_accept.raise_()

            self._btn_reject = QPushButton("✕", img_container)
            self._btn_reject.setGeometry(
                _THUMB - _BTN_IGNORE_SZ - 2, 2, _BTN_IGNORE_SZ, _BTN_IGNORE_SZ
            )
            self._btn_reject.setStyleSheet(self._BTN_REJECT_STYLE)
            self._btn_reject.setCursor(Qt.PointingHandCursor)
            self._btn_reject.setToolTip(translate("FaceItem", "Reject this suggestion"))
            self._btn_reject.clicked.connect(
                lambda: self.suggestion_reject_requested.emit(self._face_id)
            )
            self._btn_reject.raise_()

        layout.addWidget(img_container, alignment=Qt.AlignHCenter)

        lbl_name = QLabel(name)
        lbl_name.setAlignment(Qt.AlignCenter)
        lbl_name.setWordWrap(True)
        lbl_name.setMaximumWidth(_WIDTH - 8)
        lbl_name.setStyleSheet(
            f"font-size: 11px; color: {name_color or '#ccc'}; border: none;"
        )
        layout.addWidget(lbl_name)
        self._name_label = lbl_name

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

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self._face_id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(self._face_id, event.globalPos())


# ------------------------------------------------------------------ ignored faces dialog

class _IgnoredFacesDialog(QDialog):
    """Dialog listing the ignored faces of this photo, with a Restore button."""

    def __init__(self, faces: list[FaceInfo], photo_path: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("IgnoredFacesDialog", "Ignored faces"))
        self.setMinimumWidth(300)
        self._restored: list[int] = []   # face_ids restored
        self._photo_path = photo_path
        self._rows: dict[int, QWidget] = {}

        layout = QVBoxLayout(self)

        n_faces = len(faces)
        lbl = QLabel(translate("FacePanel",
                               "%n ignored face(s) on this photo:",
                               None, n_faces))
        lbl.setStyleSheet("color: #aaa; font-size: 11px; margin-bottom: 4px;")
        layout.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: 1px solid #444;")
        scroll.setMaximumHeight(400)
        container = QWidget()
        self._vbox = QVBoxLayout(container)
        self._vbox.setContentsMargins(4, 4, 4, 4)
        self._vbox.setSpacing(6)
        scroll.setWidget(container)
        layout.addWidget(scroll)

        for face in faces:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 4, 4, 4)
            row_layout.setSpacing(8)

            thumb_lbl = LoadingLabel("#1a1a1a", row)
            thumb_lbl.setFixedSize(48, 48)
            thumb_lbl.setAlignment(Qt.AlignCenter)
            thumb_lbl.setStyleSheet("border-radius: 3px; border: none;")
            thumb_lbl.start_loading()
            row_layout.addWidget(thumb_lbl)

            info = QLabel(f"x={face.bbox_x}, y={face.bbox_y}\n{face.bbox_w}×{face.bbox_h}px")
            info.setStyleSheet("font-size: 10px; color: #aaa;")
            row_layout.addWidget(info, stretch=1)

            btn = QPushButton(translate("IgnoredFacesDialog", "Restore"))
            btn.setFixedWidth(75)
            btn.setStyleSheet(
                "QPushButton { background:#2a5a3a; color:#9d9; border:none;"
                " border-radius:3px; padding:4px 8px; }"
                "QPushButton:hover { background:#3a7a4a; }"
                "QPushButton:disabled { background:#333; color:#666; }"
            )
            face_id = face.id
            btn.clicked.connect(lambda checked=False, fid=face_id, b=btn, r=row: self._restore(fid, b, r))
            row_layout.addWidget(btn)

            self._vbox.addWidget(row)
            self._rows[face.id] = (thumb_lbl, face)

        self._vbox.addStretch()

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.accept)
        layout.addWidget(bb)

        # Load the thumbnails
        items = [(face.id, face) for face in faces]
        if items:
            self._loader = _FacePanelLoader(items, self)
            self._loader.ready.connect(self._on_thumb_ready)
            self._loader.start()
        else:
            self._loader = None

    @Slot(int, bytes)
    def _on_thumb_ready(self, face_id: int, data: bytes) -> None:
        row_data = self._rows.get(face_id)
        if row_data:
            thumb_lbl, _ = row_data
            pix = QPixmap()
            pix.loadFromData(data)
            scaled = pix.scaled(48, 48, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            thumb_lbl.setPixmap(scaled)

    def _restore(self, face_id: int, btn: QPushButton, row: QWidget) -> None:
        self._restored.append(face_id)
        btn.setEnabled(False)
        btn.setText(translate("IgnoredFacesDialog", "Restored"))
        row.setStyleSheet("background: #1a2a1a; border-radius: 3px;")

    def restored_ids(self) -> list[int]:
        return self._restored


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

_BOTTOM_BTN_STYLE = (
    "QPushButton {"
    "  background: #252525; color: #888;"
    "  border-top: 1px solid #444; border-radius: 0;"
    "  font-size: 11px; padding: 5px 4px;"
    "}"
    "QPushButton:hover { background: #2e2e2e; color: #bbb; }"
    "QPushButton:disabled { color: #444; }"
)


class FacePanel(QWidget):
    """
    Side panel showing the faces detected in the current photo.
    """

    face_highlighted         = Signal(object)  # the selected FaceInfo, or None on deselection
    all_faces_toggled        = Signal(list)    # list[FaceInfo] when "All" is active, [] otherwise
    person_assigned          = Signal()        # after an identification (group or individual face)
    cover_face_set           = Signal(int, object)  # person_id, FaceInfo — main thumbnail changed
    person_cluster_requested = Signal(int)     # person_id — double-click on a named face
    undo_stack_changed       = Signal(bool)    # True = can undo
    add_face_mode_requested  = Signal(bool)    # True = enter add mode, False = cancel

    def __init__(
        self,
        face_db: FaceDatabase,
        catalog,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db  = face_db
        self._catalog  = catalog
        self._items:           dict[int, _FaceItem] = {}
        self._faces:           dict[int, FaceInfo]  = {}
        self._cluster_persons: dict[int, int]       = {}   # cluster_id → person_id
        self._person_names:    dict[int, str]       = {}   # person_id → name (last load)
        self._loader:          _FacePanelLoader | None = None
        self._data_loader:     _FacesDataLoader | None = None
        self._dying_threads:   list = []   # threads being stopped — keep ref until finished
        self._current_photo:   str = ""
        self._selected_face_id: int | None = None
        self._undo_stack:      list[tuple[str, object]] = []
        # face_id → ((bbox_x, bbox_y, bbox_w, bbox_h, detected_rotation, edit_rotation), PNG)
        # a complete geometry key: a re-indexed face (rotated 90° before being saved,
        # cf. SingleFaceReindexThread) can change bbox/rotation under the same face_id;
        # without comparing it, an obsolete old framing would be shown again.
        self._thumb_cache:      dict[int, tuple[tuple, bytes]] = {}
        self._last_edit_rotation: int = 0
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
        lbl_title = QLabel(translate("FacePanel", "Faces"))
        lbl_title.setStyleSheet("color: #ccc; font-weight: bold; background: transparent;")
        hbox.addWidget(lbl_title)
        root.addWidget(header_bar)

        self._btn_tous = QPushButton(translate("FacePanel", "All"))
        self._btn_tous.setCheckable(True)
        self._btn_tous.setFixedHeight(34)
        self._btn_tous.setStyleSheet(_TOUS_BTN_STYLE)
        self._btn_tous.setToolTip(translate("FacePanel", "Show every face in the picture"))
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

        self._btn_add_face = QPushButton(translate("FacePanel", "➕  Add a person"))
        self._btn_add_face.setCheckable(True)
        self._btn_add_face.setFixedHeight(28)
        self._btn_add_face.setStyleSheet(
            _BOTTOM_BTN_STYLE + "QPushButton:checked { background: #2a6a2a; color: white; }"
        )
        self._btn_add_face.setToolTip(
            translate("FacePanel", "Draw a box for a person that was not detected automatically")
        )
        self._btn_add_face.toggled.connect(self._on_add_face_toggled)
        self._btn_add_face.setEnabled(False)
        root.addWidget(self._btn_add_face)

        self._btn_ignored = QPushButton(translate("FacePanel", "Ignored faces…"))
        self._btn_ignored.setFixedHeight(28)
        self._btn_ignored.setStyleSheet(_BOTTOM_BTN_STYLE)
        self._btn_ignored.setToolTip(translate("FacePanel", "Restore an ignored face on this "
                                                            "photo"))
        self._btn_ignored.clicked.connect(self._on_show_ignored)
        self._btn_ignored.setEnabled(False)
        root.addWidget(self._btn_ignored)

    # ------------------------------------------------------------------ public

    def refresh(self) -> None:
        """Reload the faces of the current photo (after an external change)."""
        if self._current_photo:
            self.set_photo(self._current_photo)

    def set_photo(self, photo_path: str) -> None:
        """Load and show the faces of the photo (asynchronously).

        A refresh of the same photo (after an identification, an ignore, etc.)
        reuses the thumbnails already decoded (cf. _thumb_cache): only the new
        faces (a manual addition) do not have an unchanged bbox in cache yet
        and are therefore the only ones to go through _FacePanelLoader again."""
        same_photo = photo_path == self._current_photo
        if not same_photo:
            self._undo_stack.clear()
            self.undo_stack_changed.emit(False)
            self._thumb_cache.clear()
        self._current_photo = photo_path
        self._btn_add_face.setEnabled(bool(photo_path))
        self._stop_loader()
        if not same_photo:
            # Emptying right now is only correct when navigating: the faces on
            # screen are those of ANOTHER photo. On a refresh of the SAME photo
            # (confirmed suggestion, ignore, unassign, undo...) they are still the
            # right ones, and _on_faces_data_ready clears and rebuilds in a single
            # slot -- so the swap costs no repaint. Clearing here instead left the
            # panel blank for the whole duration of _FacesDataLoader: every face
            # vanishing then coming back, for no reason the user can see.
            self._clear()

        # Keep the old loader before replacing it, so that reassigning
        # self._data_loader does not drop the Python refcount to 0 while the C++
        # thread is running (a QThread destroyed while running crash).
        old_dl = self._data_loader
        self._data_loader = _FacesDataLoader(self._face_db, self._catalog, photo_path, self)
        self._data_loader.data_ready.connect(self._on_faces_data_ready)
        self._data_loader.start()

        if old_dl is not None:
            try:
                old_dl.data_ready.disconnect()
            except RuntimeError:
                pass
            if old_dl.isRunning():
                self._dying_threads.append(old_dl)
                old_dl.finished.connect(old_dl.deleteLater)
                old_dl.finished.connect(self._reap_dying_threads)
            else:
                old_dl.deleteLater()

    # ------------------------------------------------------------------ undo

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def undo(self) -> None:
        if not self._undo_stack:
            return
        _desc, undo_fn = self._undo_stack.pop()
        try:
            undo_fn()
        except Exception as exc:
            logger.error("[FacePanel] undo failed: %s", exc)
        finally:
            self.undo_stack_changed.emit(bool(self._undo_stack))

    def _push_undo(self, description: str, fn) -> None:
        self._undo_stack.append((description, fn))
        self.undo_stack_changed.emit(True)

    # ------------------------------------------------------------------ data loading

    @Slot(str, list, list, list, list, int, int)
    def _on_faces_data_ready(
        self,
        photo_path: str,
        faces: list,
        person_names_items: list,
        cluster_persons_items: list,
        probable_items: list,
        ignored_count: int,
        edit_rotation: int,
    ) -> None:
        if photo_path != self._current_photo:
            return  # navigation in the meantime

        # _clear() FIRST: it empties self._cluster_persons in place, so assigning
        # the new dict before it wiped that very dict — and the local variable
        # with it, since it is the same object. A face whose person comes from its
        # cluster (re-indexed after the assignment) then fell through to the
        # "Group {id}" branch instead of showing the name.
        self._clear()

        # Rebuild the dicts with explicit int keys — avoids the coercion of the
        # keys to str by PySide6 during the cross-thread transmission via Signal.
        person_names: dict[int, str] = {int(k): v for k, v in person_names_items}
        cluster_persons: dict[int, int] = {int(k): v for k, v in cluster_persons_items}
        probable: dict[int, tuple[int, float]] = {int(k): v for k, v in probable_items}
        self._cluster_persons = cluster_persons
        self._person_names = person_names
        self._last_edit_rotation = edit_rotation

        # Update the "Ignored faces" button (the count is computed in the
        # loading thread — no DB query on the UI thread here)
        self._btn_ignored.setEnabled(ignored_count > 0)
        self._btn_ignored.setText(
            translate("FacePanel", "Ignored faces… ({n})").format(n=ignored_count)
            if ignored_count else translate("FacePanel", "Ignored faces…")
        )

        if not faces:
            lbl = QLabel(translate("FacePanel", "No face\ndetected"))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #555; font-size: 11px; border: none;")
            self._vbox.addWidget(lbl)
            if self._btn_tous.isChecked():
                self.all_faces_toggled.emit([])
            return

        # Sort: named faces first, then anonymous ones; within each group, left→right.
        def _sort_key(f):
            named = bool(
                (f.person_id and f.person_id in person_names)
                or (f.cluster_id is not None and f.cluster_id in cluster_persons)
            )
            return (0 if named else 1, f.bbox_x)

        faces_sorted = sorted(faces, key=_sort_key)

        loader_items = []
        for face in faces_sorted:
            suggestion = False
            confirmed  = False   # identification settled: no quick-ignore cross
            name_color = None
            if face.person_id and face.person_id in person_names:
                name = person_names[face.person_id]
                confirmed = True
            elif face.cluster_id is not None and face.cluster_id in cluster_persons:
                # Face re-indexed after an assignment: the cluster has a person,
                # but this individual face does not have its person_id updated yet.
                pid = cluster_persons[face.cluster_id]
                name = person_names.get(
                    pid,
                    translate("FacePanel", "Group {id}").format(id=face.cluster_id))
                confirmed = pid in person_names
            elif face.pinned:
                name = translate("FacePanel", "Separated")
            elif (
                face.suggestion_person_id is not None
                and face.suggestion_person_id in person_names
                and face.cluster_id is not None
            ):
                # Suggestion awaiting verification (_SIM_SUGGEST <= score < _SIM_AUTO_ASSIGN,
                # cf. CLAUDE.md): a green tick/red cross on the thumbnail to confirm/reject
                # without going through the full assignment dialog.
                sugg_name = person_names[face.suggestion_person_id]
                pct = round(face.suggestion_score * 100)
                name = f"{sugg_name} ? ({pct} %)"
                suggestion = True
                name_color = "#7aabdb"
            elif face.id in probable and probable[face.id][0] in person_names:
                # Match computed on the fly (not persisted, below the _SIM_SUGGEST=0.55
                # threshold that triggers the ✓/✕ ticks): purely informative,
                # confirmation through the "Identify this person…" context menu.
                prob_pid, prob_sim = probable[face.id]
                prob_name = person_names[prob_pid]
                pct = round(prob_sim * 100)
                qualifier = (translate("FacePanel", "Probably")
                             if prob_sim >= _SIM_STRONG
                             else translate("FacePanel", "Maybe"))
                name = translate("FacePanel", "≈ {qualifier} {name} ({pct} %)").format(
                    qualifier=qualifier, name=prob_name, pct=pct)
                name_color = "#7aabdb" if prob_sim >= _SIM_STRONG else "#888"
            elif face.cluster_id is not None:
                name = translate("FacePanel", "Group {id}").format(id=face.cluster_id)
            else:
                name = "Inconnu"

            item = _FaceItem(
                face, name, self._content, suggestion=suggestion, name_color=name_color,
                confirmed=confirmed,
            )
            item.clicked.connect(self._on_item_clicked)
            item.double_clicked.connect(self._on_item_double_clicked)
            item.context_menu_requested.connect(self._on_item_context_menu)
            item.ignore_requested.connect(self._on_ignore_requested)
            if suggestion:
                item.suggestion_accept_requested.connect(self._on_suggestion_accept_requested)
                item.suggestion_reject_requested.connect(self._on_suggestion_reject_requested)
            self._vbox.addWidget(item)
            self._items[face.id] = item
            self._faces[face.id] = face
            geom_key = (
                face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h,
                face.detected_rotation, edit_rotation,
            )
            cached = self._thumb_cache.get(face.id)
            if cached is not None and cached[0] == geom_key:
                item.set_image(cached[1])
            else:
                loader_items.append((face.id, face))

        # The bbox/rotation of an existing face never changes after an
        # identification — only the entries that disappeared (dedup, ignore) need purging.
        live_ids = {f.id for f in faces_sorted}
        for stale_id in list(self._thumb_cache):
            if stale_id not in live_ids:
                del self._thumb_cache[stale_id]

        # Load in the background only the thumbnails not already in cache
        # (a new face: a manual addition, or the 1st display of this photo).
        if loader_items:
            self._loader = _FacePanelLoader(loader_items, self)
            self._loader.ready.connect(self._on_face_ready)
            self._loader.start()

        # If the "All" mode was active, update the list in the viewer
        if self._btn_tous.isChecked():
            self.all_faces_toggled.emit(list(self._faces.values()))

    def show_face_context_menu(self, face: FaceInfo, gpos) -> None:
        """Builds and shows the context menu of a face.
        Called from the panel (through _on_item_context_menu) and from the viewer."""
        menu = QMenu(self)
        install_menu_width_fix(menu)
        act_identify = menu.addAction(translate("FacePanel", "Identify this person…"))
        act_identify.setToolTip(
            translate("FacePanel", "Detaches this face from its group and attaches it to a "
                                   "named person")
        )
        act_assign = menu.addAction(translate("FacePanel", "Identify this group…"))
        act_assign.setToolTip(translate("FacePanel", "Assigns the whole group to a named person"))
        menu.addSeparator()
        act_unassign = menu.addAction(translate("FacePanel", "Unassign the group"))
        act_unassign.setEnabled(
            face.person_id is not None or face.cluster_id is not None
        )
        menu.addSeparator()
        act_ignore = menu.addAction(translate("FacePanel", "Ignore this face"))

        chosen = menu.exec(gpos)
        if chosen == act_identify:
            self._on_identify_face_requested(face.id)
        elif chosen == act_assign:
            self._on_assign_requested(face.id)
        elif chosen == act_unassign:
            self._on_unassign_requested(face.id)
        elif chosen == act_ignore:
            self._on_ignore_requested(face.id)

    def _on_item_double_clicked(self, face_id: int) -> None:
        face = self._faces.get(face_id)
        if face is None:
            return
        person_id = face.person_id
        if person_id is None:
            # Unidentified face: check whether its cluster has a person
            if face.cluster_id is not None and face.cluster_id in self._cluster_persons:
                person_id = self._cluster_persons[face.cluster_id]
        if person_id is not None:
            self.person_cluster_requested.emit(person_id)

    def _on_item_context_menu(self, face_id: int, gpos) -> None:
        face = self._faces.get(face_id)
        if face is not None:
            self.show_face_context_menu(face, gpos)

    def _on_tous_toggled(self, checked: bool) -> None:
        if checked:
            # Deselect the individual face if it is active
            if self._selected_face_id is not None and self._selected_face_id in self._items:
                self._items[self._selected_face_id].set_selected(False)
                self._selected_face_id = None
                self.face_highlighted.emit(None)
            self.all_faces_toggled.emit(list(self._faces.values()))
        else:
            self.all_faces_toggled.emit([])

    # ------------------------------------------------------------------ selection

    def _on_item_clicked(self, face_id: int) -> None:
        # Leave the "All" mode without re-emitting (the simple selection takes over)
        if self._btn_tous.isChecked():
            self._btn_tous.blockSignals(True)
            self._btn_tous.setChecked(False)
            self._btn_tous.blockSignals(False)

        if face_id == self._selected_face_id:
            # Deselection (toggle)
            self._items[face_id].set_selected(False)
            self._selected_face_id = None
            self.face_highlighted.emit(None)
        else:
            # Deselection of the old one
            if self._selected_face_id is not None and self._selected_face_id in self._items:
                self._items[self._selected_face_id].set_selected(False)
            # Selection of the new one
            self._selected_face_id = face_id
            self._items[face_id].set_selected(True)
            self.face_highlighted.emit(self._faces.get(face_id))

    # ------------------------------------------------------------------ manual addition

    def _on_add_face_toggled(self, checked: bool) -> None:
        self.add_face_mode_requested.emit(checked)

    def reset_add_face_button(self) -> None:
        """Unchecks the button without re-emitting add_face_mode_requested (called by
        main_window when the mode ends on the viewer side: validation or Esc)."""
        self._btn_add_face.blockSignals(True)
        self._btn_add_face.setChecked(False)
        self._btn_add_face.blockSignals(False)

    def on_face_bbox_ready(self, bbox: tuple) -> None:
        """Receives the bbox positioned manually in the viewer and asks for the
        name of the person before creating the face in the database."""
        if not self._current_photo:
            return
        QApplication.setOverrideCursor(Qt.BusyCursor)
        t = _AssignPrepLoader(self._catalog, self._face_db, None, self)
        t.ready.connect(
            lambda persons, _sugg, bbox=bbox: self._continue_bbox_ready(bbox, persons)
        )
        t.finished.connect(t.deleteLater)
        t.start()

    def _continue_bbox_ready(self, bbox: tuple, persons: list) -> None:
        QApplication.restoreOverrideCursor()
        dlg = _AssignDialog(0, persons, suggested_person_id=None, show_ignore=False, parent=self)
        dlg.setWindowTitle(translate("FacePanel", "Name this face"))
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_new_person():
            name = dlg.new_name()
            if not name:
                return
            person = self._catalog.create_person(name)
            person_id = person.id
        else:
            person_id = dlg.existing_person_id()
            if person_id is None:
                return

        face_id = self._face_db.add_manual_face(self._current_photo, bbox, person_id)

        def _undo(fid=face_id):
            self._face_db.delete_face(fid)
            self.person_assigned.emit()
            self.set_photo(self._current_photo)
        self._push_undo("Ajouter un visage", _undo)

        self.person_assigned.emit()
        self.set_photo(self._current_photo)

    # ------------------------------------------------------------------ context menu handlers

    def _run_db_write(self, fn, refresh: bool = True) -> None:
        """Runs a FaceDatabase write in a _DbWriteWorker then refreshes the panel
        (person_assigned + set_photo) at the end. The UI thread stays smooth
        during the dedup/Picasa consumption of a large group; the immediate
        visual feedback is provided by _set_item_name_immediate on the caller
        side."""
        w = _DbWriteWorker(fn, self)
        if refresh:
            w.finished.connect(self._on_db_write_done)
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._reap_dying_threads)
        self._dying_threads.append(w)
        w.start()

    @Slot()
    def _on_db_write_done(self) -> None:
        self.person_assigned.emit()
        if self._current_photo:
            self.set_photo(self._current_photo)

    def _set_item_name_immediate(self, face_id: int, text: str) -> None:
        """Updates the label of a face without waiting for the DB write nor the
        reload of the panel — immediate visual feedback after the dialog."""
        item = self._items.get(face_id)
        if item is not None:
            item._name_label.setText(text)
            item._name_label.setStyleSheet(
                "font-size: 11px; color: #ccc; border: none;"
            )

    def _person_display_name(self, person_id: int, persons: list) -> str:
        name = self._person_names.get(person_id)
        if name:
            return name
        p = next((p for p in persons if p.id == person_id), None)
        return p.name if p is not None else "…"

    def _on_identify_face_requested(self, face_id: int) -> None:
        """Separates this face from its group and attaches it to a named person."""
        face = self._faces.get(face_id)
        if face is None:
            return
        QApplication.setOverrideCursor(Qt.BusyCursor)
        t = _AssignPrepLoader(self._catalog, self._face_db, face, self)
        t.ready.connect(
            lambda persons, suggested_id, face_id=face_id:
                self._continue_identify_face(face_id, persons, suggested_id)
        )
        t.finished.connect(t.deleteLater)
        t.start()

    def _continue_identify_face(
        self, face_id: int, persons: list, suggested_id: "int | None"
    ) -> None:
        QApplication.restoreOverrideCursor()
        dlg = _AssignDialog(
            face_id, persons,
            suggested_person_id=suggested_id,
            show_ignore=False,
            parent=self,
        )
        dlg.setWindowTitle(translate("FacePanel", "Identify this person"))
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_new_person():
            name = dlg.new_name()
            if not name:
                return
            person = self._catalog.create_person(name)
            person_id = person.id
            display = name
        else:
            person_id = dlg.existing_person_id()
            if person_id is None:
                return
            display = self._person_display_name(person_id, persons)

        logger.debug(
            "[FacePanel] isolate_and_assign face=%s person=%s", face_id, person_id
        )
        # Immediate visual feedback, the write (dedup + Picasa) in the background
        self._set_item_name_immediate(face_id, display)

        def _undo(fid=face_id):
            self._face_db.unassign_face(fid)
            self.person_assigned.emit()
            self.set_photo(self._current_photo)
        self._push_undo("Identifier visage", _undo)

        self._run_db_write(
            lambda fid=face_id, pid=person_id:
                self._face_db.isolate_and_assign_face(fid, pid)
        )

    def _on_assign_requested(self, face_id: int) -> None:
        face = self._faces.get(face_id)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        t = _AssignPrepLoader(self._catalog, self._face_db, face, self)
        t.ready.connect(
            lambda persons, suggested_id, face_id=face_id, face=face:
                self._continue_assign_requested(face_id, face, persons, suggested_id)
        )
        t.finished.connect(t.deleteLater)
        t.start()

    def _continue_assign_requested(
        self, face_id: int, face: "FaceInfo | None", persons: list, suggested_id: "int | None"
    ) -> None:
        QApplication.restoreOverrideCursor()
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
            display = person.name
        else:
            person_id = dlg.existing_person_id()
            if person_id is None:
                return
            display = self._person_display_name(person_id, persons)

        cluster_id_assigned: int | None = None
        if (
            face is not None
            and face.cluster_id is not None
            and face.cluster_id >= 0
            and not face.pinned
        ):
            cluster_id_assigned = face.cluster_id
        logger.debug(
            "[FacePanel] assign cluster=%s face=%s person=%s",
            cluster_id_assigned, face_id, person_id,
        )

        # Immediate visual feedback: every face of the group takes the name
        # without waiting for the write (dedup + Picasa on every photo of the group)
        for fid, f in self._faces.items():
            if fid == face_id or (
                cluster_id_assigned is not None and f.cluster_id == cluster_id_assigned
            ):
                self._set_item_name_immediate(fid, display)

        def _undo(fid=face_id, pid=person_id, cid=cluster_id_assigned):
            if cid is not None:
                self._face_db.unassign_person_from_cluster(pid, cid)
            else:
                self._face_db.unassign_person_from_face(fid)
            self.person_assigned.emit()
            self.set_photo(self._current_photo)
        self._push_undo("Identifier groupe", _undo)

        def _write(fid=face_id, pid=person_id, cid=cluster_id_assigned):
            if cid is not None:
                self._face_db.assign_person_to_cluster(cid, pid)
            self._face_db.assign_person_to_face(fid, pid)
        self._run_db_write(_write)

    def _on_unassign_requested(self, face_id: int) -> None:
        self._face_db.unassign_face(face_id)
        self.set_photo(self._current_photo)

    def _on_ignore_requested(self, face_id: int) -> None:
        self._face_db.ignore_face(face_id)

        def _undo(fid=face_id):
            self._face_db.unignore_face(fid)
            self.set_photo(self._current_photo)
        self._push_undo("Ignorer visage", _undo)

        self.set_photo(self._current_photo)

    def _on_suggestion_accept_requested(self, face_id: int) -> None:
        """Confirms the pending suggestion: assigns the person to the whole group
        (the same effect as the "Confirm" button of the per-person verification list)."""
        face = self._faces.get(face_id)
        if face is None or face.cluster_id is None or face.suggestion_person_id is None:
            return
        cluster_id = face.cluster_id
        person_id = face.suggestion_person_id

        # Immediate visual feedback on every face of the group present in the
        # panel; the write (dedup + Picasa) goes to the background.
        display = self._person_names.get(person_id, "…")
        for fid, f in self._faces.items():
            if f.cluster_id == cluster_id:
                self._set_item_name_immediate(fid, display)

        def _undo(cid=cluster_id, pid=person_id):
            self._face_db.unassign_person_from_cluster(pid, cid)
            self.person_assigned.emit()
            self.set_photo(self._current_photo)
        self._push_undo("Confirmer suggestion", _undo)

        self._run_db_write(
            lambda cid=cluster_id: self._face_db.accept_cluster_suggestion(cid)
        )

    def _on_suggestion_reject_requested(self, face_id: int) -> None:
        """Rejects the pending suggestion (clears suggestion_person_id/score of the
        group, without immediately recomputing another person — unlike a
        rejection from the per-person verification view)."""
        face = self._faces.get(face_id)
        if face is None or face.cluster_id is None:
            return
        cluster_id = face.cluster_id
        person_id = face.suggestion_person_id
        score = face.suggestion_score
        self._face_db.clear_cluster_suggestion(cluster_id)

        def _undo(cid=cluster_id, pid=person_id, sc=score):
            if pid is not None:
                self._face_db.set_cluster_suggestions({cid: (pid, sc)})
            self.set_photo(self._current_photo)
        self._push_undo("Rejeter suggestion", _undo)

        self.set_photo(self._current_photo)

    def _on_isolate_requested(self, face_id: int) -> None:
        self._face_db.isolate_face(face_id)
        self.set_photo(self._current_photo)

    def _on_set_cover_requested(self, face_id: int) -> None:
        self._face_db.set_cover_face(face_id)
        face = self._faces.get(face_id)
        if face and face.person_id:
            self.cover_face_set.emit(face.person_id, face)
            self.person_assigned.emit()

    # ------------------------------------------------------------------ ignored faces

    def _on_show_ignored(self) -> None:
        ignored = self._face_db.get_ignored_faces_for_photo(self._current_photo)
        if not ignored:
            return
        dlg = _IgnoredFacesDialog(ignored, self._current_photo, self)
        dlg.exec()
        restored = dlg.restored_ids()
        if restored:
            for face_id in restored:
                self._face_db.unignore_face(face_id)
                fid = face_id

                def _undo(fid=fid):
                    self._face_db.ignore_face(fid)
                    self.set_photo(self._current_photo)
                self._push_undo("Restaurer visage ignoré", _undo)

            self.set_photo(self._current_photo)

    # ------------------------------------------------------------------ internal

    def _on_face_ready(self, face_id: int, data: bytes) -> None:
        face = self._faces.get(face_id)
        if face is not None:
            geom_key = (
                face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h,
                face.detected_rotation, self._last_edit_rotation,
            )
            self._thumb_cache[face_id] = (geom_key, data)
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
        self._cluster_persons.clear()
        self._selected_face_id = None

    def _stop_loader(self) -> None:
        if self._loader is not None:
            old = self._loader
            self._loader = None
            old.stop()
            if old.isRunning():
                try:
                    old.ready.disconnect(self._on_face_ready)
                except RuntimeError:
                    pass
                # Keep a Python reference until the end of the thread so that Shiboken
                # does not destroy the C++ QThread while it is still running.
                self._dying_threads.append(old)
                old.finished.connect(old.deleteLater)
                old.finished.connect(self._reap_dying_threads)
            else:
                old.deleteLater()

    @Slot()
    def _reap_dying_threads(self) -> None:
        """Removes the threads that have finished from the list (releases their Python references)."""
        still_running = []
        for t in self._dying_threads:
            try:
                if t.isRunning():
                    still_running.append(t)
            except RuntimeError:
                # deleteLater() has already destroyed the underlying C++ object (another
                # dying thread may have finished and emptied the event queue before
                # this one could be removed from the list): it is simply
                # considered already cleaned up.
                pass
        self._dying_threads = still_running
