# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
PeopleDialog — identification and merging of the face groups.
"""

import io
import logging

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QPushButton, QRadioButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from src.core.models import FaceInfo, PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.core.i18n import translate

logger = logging.getLogger(__name__)

_AVATAR_SIZE = 60

# Stylesheet applied to the dialogs containing QRadioButtons.
# The global dark theme does not define QRadioButton::indicator,
# which makes the dots invisible on a dark background.
_RADIO_STYLE = """
QRadioButton {
    spacing: 6px;
}
QRadioButton::indicator {
    width: 14px;
    height: 14px;
    border-radius: 7px;
    border: 2px solid #777;
    background: #2a2a2a;
}
QRadioButton::indicator:hover {
    border-color: #aaa;
}
QRadioButton::indicator:checked {
    background: #7aabdb;
    border: 2px solid #7aabdb;
}
"""


# ------------------------------------------------------------------ helpers

def _load_edit_rotations(photo_paths: list[str]) -> dict[str, int]:
    """Batch-query edits.db for photo rotations. Returns {path: degrees_CW}."""
    if not photo_paths:
        return {}
    try:
        import sqlite3 as _sq
        from src.core.app_dirs import APP_DATA_DIR
        db = APP_DATA_DIR / "edits.db"
        if not db.exists():
            return {}
        ph = ",".join("?" * len(photo_paths))
        con = _sq.connect(str(db))
        rows = con.execute(
            f"SELECT photo_path, rotation FROM photo_edits WHERE photo_path IN ({ph})",
            list(photo_paths),
        ).fetchall()
        con.close()
        return {r[0]: int(round(r[1])) % 360 for r in rows if r[1] and int(round(r[1])) % 360}
    except Exception:
        return {}


def _face_bytes(face: "FaceInfo", size: int, edit_rotation: int = 0) -> bytes:
    """
    Decode face crop as PNG bytes. Safe to call from any thread.

    edit_rotation : CW rotation (degrees) applied to the photo for the display.
    The net rotation (detected_rotation − edit_rotation) is applied to the crop
    so that the thumbnail always matches the displayed orientation.
    """
    try:
        from pathlib import Path
        from src.library.exif_reader import VIDEO_EXT
        if Path(face.photo_path).suffix.lower() in VIDEO_EXT:
            return b""

        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(face.photo_path)).convert("RGB")
        if face.detected_rotation:
            img = img.rotate(-face.detected_rotation, expand=True)
        x, y, w, h = face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h
        pad = int(max(w, h) * 0.18)
        left   = max(0, x - pad)
        top    = max(0, y - pad)
        right  = min(img.width,  x + w + pad)
        bottom = min(img.height, y + h + pad)
        if right <= left or bottom <= top:
            return b""
        crop = img.crop((left, top, right, bottom))
        # Bring the crop back into the display space (edit_rotation).
        # PIL.rotate is CCW; detected_rotation/edit_rotation are CW.
        net = (face.detected_rotation - edit_rotation) % 360
        if net:
            crop = crop.rotate(net, expand=True)
        crop = crop.resize((size, size))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        logger.debug("_face_bytes: %s", exc)
        return b""


def load_face_pixmap(
    face: "FaceInfo", size: int = _AVATAR_SIZE, edit_rotation: int = 0
) -> QPixmap:
    """Return a QPixmap of the face crop. Must be called from the UI thread."""
    data = _face_bytes(face, size, edit_rotation=edit_rotation)
    if data:
        pix = QPixmap()
        pix.loadFromData(data)
        return pix
    pix = QPixmap(size, size)
    pix.fill(Qt.darkGray)
    return pix


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [−1, 1]. Returns 0 on zero vectors."""
    try:
        import numpy as np
        va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        return float(np.dot(va, vb) / denom) if denom > 1e-8 else 0.0
    except ImportError:
        dot  = sum(x * y for x, y in zip(a, b))
        na   = sum(x * x for x in a) ** 0.5
        nb   = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0


# Thresholds for displaying the suggestions (faces not yet "awaiting
# verification" — below _SIM_SUGGEST in face_database.py — shown here as a live
# preview while the user browses the unidentified groups).
_SIM_STRONG  = 0.50   # very likely → label in blue
_SIM_WEAK    = 0.45   # possible    → label in grey


def _placeholder_pixmap(size: int = _AVATAR_SIZE) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.darkGray)
    return pix


# ------------------------------------------------------------------ avatar loader

class _AvatarLoader(QThread):
    """
    Background thread: decodes face crops as PNG bytes, emits them one by one.
    QPixmap must be created in the UI thread — we only send raw bytes here.
    """
    avatar_ready = Signal(int, bytes)   # cluster_id, PNG bytes

    def __init__(
        self,
        items: list[tuple[int, "FaceInfo"]],
        size: int = _AVATAR_SIZE,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items = items
        self._size  = size

    def run(self) -> None:
        paths = list({face.photo_path for _, face in self._items})
        edit_rots = _load_edit_rotations(paths)
        for cluster_id, face in self._items:
            data = _face_bytes(face, self._size,
                               edit_rotation=edit_rots.get(face.photo_path, 0))
            if data:
                self.avatar_ready.emit(cluster_id, data)


# ------------------------------------------------------------------ assign dialog

class _AssignDialog(QDialog):
    """
    Unified dialog to identify a group or a face.

    Shows the suggested person at the top (if available), then the other
    people in a filterable list, then the create / ignore options.
    """

    def __init__(
        self,
        cluster_id: int,
        existing_persons: list[PersonInfo],
        suggested_person_id: int | None = None,
        show_ignore: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("AssignDialog", "Identify this person"))
        self.setMinimumWidth(360)
        self.setStyleSheet(_RADIO_STYLE)
        self._selected_person_id: int | None = None
        self._new_name: str = ""
        self._ignored: bool = False
        self._rb_ignore: QRadioButton | None = None
        self._person_rbs: list[tuple[QRadioButton, str]] = []
        self._setup_ui(existing_persons, suggested_person_id, show_ignore)

    def _setup_ui(
        self,
        persons: list[PersonInfo],
        suggested_person_id: int | None,
        show_ignore: bool,
    ) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        self._btn_group = QButtonGroup(self)
        preselect_rb: QRadioButton | None = None

        # --- Personne probable (suggestion) ---
        suggested_person = (
            next((p for p in persons if p.id == suggested_person_id), None)
            if suggested_person_id is not None else None
        )

        if suggested_person:
            lbl_sugg = QLabel(translate("AssignDialog", "Likely person:"))
            lbl_sugg.setStyleSheet("color: #7aabdb; font-size: 11px; font-weight: bold;")
            layout.addWidget(lbl_sugg)

            n_photos = suggested_person.photo_count
            rb_sugg = QRadioButton(
                suggested_person.name + "  ("
                + translate("AssignDialog", "%n photo(s)", None, n_photos) + ")"
            )
            rb_sugg.setProperty("person_id", suggested_person.id)
            self._btn_group.addButton(rb_sugg)
            layout.addWidget(rb_sugg)
            preselect_rb = rb_sugg

            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet("color: #444;")
            layout.addWidget(sep)

        # --- Autres personnes existantes ---
        other_persons = [p for p in persons if p.id != suggested_person_id]

        if other_persons:
            lbl_others = QLabel(
                translate("AssignDialog", "Other people:") if suggested_person
                else translate("AssignDialog", "Existing people:")
            )
            lbl_others.setStyleSheet("color: #aaa; font-size: 11px;")
            layout.addWidget(lbl_others)

            self._search_input = QLineEdit()
            self._search_input.setPlaceholderText(translate("AssignDialog", "🔍  Search for a "
                                                                            "name…"))
            self._search_input.setClearButtonEnabled(True)
            self._search_input.textChanged.connect(self._filter_persons)
            layout.addWidget(self._search_input)

            scroll_content = QWidget()
            scroll_content.setStyleSheet("background: transparent;")
            sc_layout = QVBoxLayout(scroll_content)
            sc_layout.setContentsMargins(4, 4, 4, 4)
            sc_layout.setSpacing(2)

            for p in other_persons:
                n_photos = p.photo_count
                rb = QRadioButton(
                    p.name + "  ("
                    + translate("AssignDialog", "%n photo(s)", None, n_photos) + ")"
                )
                rb.setProperty("person_id", p.id)
                self._btn_group.addButton(rb)
                sc_layout.addWidget(rb)
                self._person_rbs.append((rb, p.name))
                if preselect_rb is None:
                    preselect_rb = rb

            scroll_area = QScrollArea()
            scroll_area.setWidget(scroll_content)
            scroll_area.setWidgetResizable(True)
            scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            scroll_area.setMaximumHeight(200)
            scroll_area.setStyleSheet(
                "QScrollArea { border: 1px solid #444; border-radius: 3px;"
                " background: transparent; }"
            )
            layout.addWidget(scroll_area)

        # --- Nouvelle personne ---
        sep_new = QFrame()
        sep_new.setFrameShape(QFrame.HLine)
        sep_new.setStyleSheet("color: #444;")
        layout.addWidget(sep_new)

        rb_new = QRadioButton(translate("AssignDialog", "Create a new person:"))
        self._btn_group.addButton(rb_new)
        self._rb_new = rb_new
        layout.addWidget(rb_new)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText(translate("AssignDialog", "Name of the person…"))
        self._name_input.textChanged.connect(lambda: rb_new.setChecked(True))
        _orig_focus = self._name_input.focusInEvent
        self._name_input.focusInEvent = lambda e: (rb_new.setChecked(True), _orig_focus(e))
        layout.addWidget(self._name_input)

        # --- Ignorer ---
        if show_ignore:
            sep_ign = QFrame()
            sep_ign.setFrameShape(QFrame.HLine)
            sep_ign.setStyleSheet("color: #444;")
            layout.addWidget(sep_ign)

            rb_ignore = QRadioButton(translate("AssignDialog", "Ignore this group"))
            self._btn_group.addButton(rb_ignore)
            self._rb_ignore = rb_ignore
            layout.addWidget(rb_ignore)

        # Pre-selection: the suggestion or the first of the list, otherwise "Create"
        if preselect_rb is not None:
            preselect_rb.setChecked(True)
        else:
            rb_new.setChecked(True)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter_persons(self, text: str) -> None:
        query = text.lower().strip()
        first_visible: QRadioButton | None = None
        for rb, name in self._person_rbs:
            visible = not query or query in name.lower()
            rb.setVisible(visible)
            if visible and first_visible is None:
                first_visible = rb
        checked = self._btn_group.checkedButton()
        if checked is not None and checked is not self._rb_new and not checked.isVisible():
            if first_visible is not None:
                first_visible.setChecked(True)
            else:
                self._rb_new.setChecked(True)

    def _on_accept(self) -> None:
        checked = self._btn_group.checkedButton()
        if self._rb_ignore is not None and checked is self._rb_ignore:
            self._ignored = True
            self.accept()
        elif checked is self._rb_new:
            name = self._name_input.text().strip()
            if not name:
                self._name_input.setFocus()
                return
            self._new_name = name
            self._selected_person_id = None
            self.accept()
        else:
            self._selected_person_id = checked.property("person_id")
            self.accept()

    def is_ignored(self) -> bool:
        return self._ignored

    def is_new_person(self) -> bool:
        return not self._ignored and self._selected_person_id is None

    def new_name(self) -> str:
        return self._new_name

    def existing_person_id(self) -> int:
        return self._selected_person_id


# ------------------------------------------------------------------ cluster row

class _ClusterRow(QFrame):
    """One row: face thumbnail + cluster info + 'Nommer' button."""

    named    = Signal(int, str)   # cluster_id, person_name  → create a new person
    assigned = Signal(int, int)   # cluster_id, person_id   → assigner existante
    ignored  = Signal(int)        # cluster_id              → ignorer

    def __init__(
        self,
        cluster_id: int,
        face_count: int,
        rep_face: FaceInfo | None,
        existing_persons: list[PersonInfo],
        suggestion: tuple[str, int, float] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cluster_id = cluster_id
        self._existing_persons = existing_persons
        self._suggested_person_id = suggestion[1] if suggestion else None
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("QFrame { border: 1px solid #3a3a3a; border-radius: 4px; }")

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(12)

        self._lbl_avatar = LoadingLabel("#2a2a2a")
        self._lbl_avatar.setFixedSize(_AVATAR_SIZE, _AVATAR_SIZE)
        self._lbl_avatar.setAlignment(Qt.AlignCenter)
        self._lbl_avatar.setStyleSheet("border: none;")
        self._lbl_avatar.start_loading()
        row.addWidget(self._lbl_avatar)

        group_label = (translate("PeoplePanel", "Isolated") if face_count == 1
                       else translate("PeoplePanel", "Group {id}").format(id=cluster_id))
        n_faces = translate("PeoplePanel", "%n face(s)", None, face_count)
        lbl_info = QLabel(group_label + "\n" + n_faces)
        lbl_info.setStyleSheet("border: none;")
        lbl_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        if suggestion:
            name, _, sim = suggestion
            pct = int(sim * 100)
            if sim >= _SIM_STRONG:
                color = "#7aabdb"
                label = translate("PeoplePanel", "→ Probably {name} ({pct} %)"
                                  ).format(name=name, pct=pct)
            else:
                color = "#888"
                label = translate("PeoplePanel", "→ Maybe {name} ({pct} %)"
                                  ).format(name=name, pct=pct)
            lbl_info.setText(
                f"{group_label} — {n_faces}\n"
                f"<span style='color:{color}; font-size:11px'>{label}</span>"
            )
            lbl_info.setTextFormat(Qt.RichText)

        row.addWidget(lbl_info)

        btn = QPushButton(translate("ClusterRow", "Name…"))
        btn.setFixedWidth(90)
        btn.setStyleSheet("border: 1px solid #555;")
        btn.clicked.connect(self._ask_name)
        row.addWidget(btn)

    def set_avatar(self, data: bytes) -> None:
        """Receive PNG bytes from _AvatarLoader and update the avatar label."""
        pix = QPixmap()
        pix.loadFromData(data)
        self._lbl_avatar.setPixmap(pix)

    def _ask_name(self) -> None:
        dlg = _AssignDialog(
            self._cluster_id,
            self._existing_persons,
            suggested_person_id=self._suggested_person_id,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.is_ignored():
            self.ignored.emit(self._cluster_id)
        elif dlg.is_new_person():
            self.named.emit(self._cluster_id, dlg.new_name())
        else:
            self.assigned.emit(self._cluster_id, dlg.existing_person_id())


# ------------------------------------------------------------------ merge dialog

class MergePersonsDialog(QDialog):
    """
    Dialog to merge two named persons.
    Shows all persons except `source` and lets the user pick the one to keep.
    """

    def __init__(
        self,
        source: PersonInfo,
        all_persons: list[PersonInfo],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate("MergePersonsDialog", "Merge with…"))
        self.setMinimumWidth(340)
        self.setStyleSheet(_RADIO_STYLE)
        self._target_id: int | None = None
        self._setup_ui(source, [p for p in all_persons if p.id != source.id])

    def _setup_ui(self, source: PersonInfo, others: list[PersonInfo]) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            translate("MergePersonsDialog", "Merge <b>{name}</b> with:"
                      ).format(name=source.name)))

        if not others:
            layout.addWidget(QLabel(translate("MergePersonsDialog", "No other person to merge "
                                                                    "with.")))
            btn = QDialogButtonBox(QDialogButtonBox.Cancel)
            btn.rejected.connect(self.reject)
            layout.addWidget(btn)
            return

        self._btn_group = QButtonGroup(self)
        self._person_rbs: list[tuple[QRadioButton, str]] = []

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(translate("MergePersonsDialog", "🔍  Search for a "
                                                                              "name…"))
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._filter_persons)
        layout.addWidget(self._search_input)

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        sc_layout = QVBoxLayout(scroll_content)
        sc_layout.setContentsMargins(4, 4, 4, 4)
        sc_layout.setSpacing(2)

        for p in others:
            n_photos = p.photo_count
            rb = QRadioButton(
                p.name + "  ("
                + translate("MergePersonsDialog", "%n photo(s)", None, n_photos) + ")"
            )
            rb.setProperty("person_id", p.id)
            self._btn_group.addButton(rb)
            sc_layout.addWidget(rb)
            self._person_rbs.append((rb, p.name))
        self._btn_group.buttons()[0].setChecked(True)

        scroll_area = QScrollArea()
        scroll_area.setWidget(scroll_content)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setMaximumHeight(200)
        scroll_area.setStyleSheet(
            "QScrollArea { border: 1px solid #444; border-radius: 3px;"
            " background: transparent; }"
        )
        layout.addWidget(scroll_area)

        note = QLabel(
            translate("MergePersonsDialog",
                      "The faces of <i>{name}</i> will be attached to the person you "
                      "pick.\n{name} will then be deleted.").format(name=source.name)
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter_persons(self, text: str) -> None:
        query = text.lower().strip()
        first_visible: QRadioButton | None = None
        for rb, name in self._person_rbs:
            visible = not query or query in name.lower()
            rb.setVisible(visible)
            if visible and first_visible is None:
                first_visible = rb
        checked = self._btn_group.checkedButton()
        if checked is not None and not checked.isVisible():
            if first_visible is not None:
                first_visible.setChecked(True)

    def _on_accept(self) -> None:
        checked = self._btn_group.checkedButton()
        if checked:
            self._target_id = checked.property("person_id")
            self.accept()

    def target_person_id(self) -> int | None:
        return self._target_id


# ------------------------------------------------------------------ main dialog

class PeopleDialog(QDialog):
    """
    Dialog for identifying the face groups.

    Signals
    -------
    cluster_named(cluster_id, person_name)
        The user creates a new person for this cluster.
    cluster_assigned(cluster_id, person_id)
        The user associates this cluster with an existing person.
    """

    cluster_named    = Signal(int, str)
    cluster_assigned = Signal(int, int)

    def __init__(
        self,
        face_db: FaceDatabase,
        catalog,                     # Catalog — a late import to avoid the circularity
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self.setWindowTitle(translate("PeopleDialog", "Identify the people"))
        self.setMinimumSize(460, 540)
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        intro = QLabel(
            translate("PeopleDialog", "PixelPhotoManager has automatically gathered the "
                                      "similar faces.\nName each group to create one album per "
                                      "person.")
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #aaa;")
        layout.addWidget(intro)

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._content_layout.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._content)
        layout.addWidget(scroll)

        btn_close = QPushButton(translate("PeopleDialog", "Close"))
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

    def refresh(self) -> None:
        # Stop any avatar loading in progress and release the child Qt thread
        if hasattr(self, "_avatar_loader") and self._avatar_loader is not None:
            try:
                self._avatar_loader.avatar_ready.disconnect(self._on_avatar_ready)
            except RuntimeError:
                pass
            if self._avatar_loader.isRunning():
                self._avatar_loader.finished.connect(self._avatar_loader.deleteLater)
            else:
                self._avatar_loader.deleteLater()
            self._avatar_loader = None

        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._rows: dict[int, _ClusterRow] = {}

        clusters = self._face_db.get_unnamed_clusters()
        if not clusters:
            lbl = QLabel(
                translate("PeopleDialog", "Every group has been named.\n\nAdd new photos and "
                                          "run the analysis\nagain to find new faces.")
            )
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #777;")
            self._content_layout.addWidget(lbl)
            return

        # Existing people and their centroids by group
        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)

        person_ids = [p.id for p in persons if p.id is not None]
        person_cluster_embs = self._face_db.get_all_person_cluster_centroids(person_ids)
        reps = self._face_db.get_all_representative_faces(
            [cid for cid, _ in clusters]
        )

        avatar_items: list[tuple[int, FaceInfo]] = []
        for cluster_id, face_count in clusters:
            rep = reps.get(cluster_id)

            # Suggestion: the best score against each known group centroid
            suggestion: tuple[str, int, float] | None = None
            if persons and person_cluster_embs:
                cluster_emb = self._face_db.get_representative_embedding(
                    cluster_id=cluster_id
                )
                if cluster_emb:
                    best_sim, best_person = 0.0, None
                    for p in persons:
                        for p_emb in person_cluster_embs.get(p.id, {}).values():
                            sim = _cosine_sim(cluster_emb, p_emb)
                            if sim > best_sim:
                                best_sim, best_person = sim, p
                    if best_person and best_sim >= _SIM_WEAK:
                        suggestion = (best_person.name, best_person.id, best_sim)

            row = _ClusterRow(cluster_id, face_count, rep, persons, suggestion, self)
            row.named.connect(self._on_named)
            row.assigned.connect(self._on_assigned)
            row.ignored.connect(self._on_ignored)
            self._content_layout.addWidget(row)
            self._rows[cluster_id] = row
            if rep:
                avatar_items.append((cluster_id, rep))

        # Start loading the avatars in the background
        if avatar_items:
            self._avatar_loader = _AvatarLoader(avatar_items, _AVATAR_SIZE, self)
            self._avatar_loader.avatar_ready.connect(self._on_avatar_ready)
            self._avatar_loader.start()

    def _on_avatar_ready(self, cluster_id: int, data: bytes) -> None:
        row = self._rows.get(cluster_id)
        if row:
            row.set_avatar(data)

    def _on_named(self, cluster_id: int, name: str) -> None:
        self.cluster_named.emit(cluster_id, name)
        self.refresh()

    def _on_assigned(self, cluster_id: int, person_id: int) -> None:
        self.cluster_assigned.emit(cluster_id, person_id)
        self.refresh()

    def _on_ignored(self, cluster_id: int) -> None:
        self._face_db.ignore_cluster(cluster_id)
        self.refresh()
