# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
PeopleDialog — identification et fusion des groupes de visages.
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

logger = logging.getLogger(__name__)

_AVATAR_SIZE = 60

# Stylesheet appliqué aux dialogues contenant des QRadioButton.
# Le thème sombre global ne définit pas QRadioButton::indicator,
# ce qui rend les pastilles invisibles sur fond foncé.
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

    edit_rotation : rotation CW (degrés) appliquée à la photo pour l'affichage.
    La rotation nette (detected_rotation − edit_rotation) est appliquée au crop
    pour que la vignette corresponde toujours à l'orientation affichée.
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
        # Ramener le crop dans l'espace d'affichage (edit_rotation).
        # PIL.rotate est CCW ; detected_rotation/edit_rotation sont CW.
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


# Seuils pour l'affichage des suggestions
_SIM_STRONG  = 0.60   # très probable  → libellé en bleu
_SIM_WEAK    = 0.50   # possible        → libellé en gris


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
    Dialogue unifié pour identifier un groupe ou un visage.

    Affiche en tête la personne suggérée (si disponible), puis les autres
    personnes dans une liste filtrabe, puis les options de création / d'ignorance.
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
        self.setWindowTitle("Identifier cette personne")
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
            lbl_sugg = QLabel("Personne probable :")
            lbl_sugg.setStyleSheet("color: #7aabdb; font-size: 11px; font-weight: bold;")
            layout.addWidget(lbl_sugg)

            rb_sugg = QRadioButton(
                f"{suggested_person.name}"
                f"  ({suggested_person.photo_count}"
                f" photo{'s' if suggested_person.photo_count != 1 else ''})"
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
                "Autres personnes :" if suggested_person else "Personnes existantes :"
            )
            lbl_others.setStyleSheet("color: #aaa; font-size: 11px;")
            layout.addWidget(lbl_others)

            self._search_input = QLineEdit()
            self._search_input.setPlaceholderText("🔍  Rechercher un nom…")
            self._search_input.setClearButtonEnabled(True)
            self._search_input.textChanged.connect(self._filter_persons)
            layout.addWidget(self._search_input)

            scroll_content = QWidget()
            scroll_content.setStyleSheet("background: transparent;")
            sc_layout = QVBoxLayout(scroll_content)
            sc_layout.setContentsMargins(4, 4, 4, 4)
            sc_layout.setSpacing(2)

            for p in other_persons:
                rb = QRadioButton(
                    f"{p.name}  ({p.photo_count} photo{'s' if p.photo_count != 1 else ''})"
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

        # --- Ignorer ---
        if show_ignore:
            sep_ign = QFrame()
            sep_ign.setFrameShape(QFrame.HLine)
            sep_ign.setStyleSheet("color: #444;")
            layout.addWidget(sep_ign)

            rb_ignore = QRadioButton("Ignorer ce groupe")
            self._btn_group.addButton(rb_ignore)
            self._rb_ignore = rb_ignore
            layout.addWidget(rb_ignore)

        # Pré-sélection : suggestion ou premier de liste, sinon "Créer"
        if preselect_rb is not None:
            preselect_rb.setChecked(True)
        else:
            rb_new.setChecked(True)

        # Boutons
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

    named    = Signal(int, str)   # cluster_id, person_name  → créer nouvelle personne
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

        plural = "s" if face_count > 1 else ""
        group_label = "Isolé" if face_count == 1 else f"Groupe {cluster_id}"
        info_text = f"{group_label}\n{face_count} visage{plural}"
        lbl_info = QLabel(info_text)
        lbl_info.setStyleSheet("border: none;")
        lbl_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        if suggestion:
            name, _, sim = suggestion
            pct = int(sim * 100)
            if sim >= _SIM_STRONG:
                color, label = "#7aabdb", f"→ Probablement {name} ({pct} %)"
            else:
                color, label = "#888", f"→ Peut-être {name} ({pct} %)"
            lbl_info.setText(
                f"{group_label} — {face_count} visage{plural}\n"
                f"<span style='color:{color}; font-size:11px'>{label}</span>"
            )
            lbl_info.setTextFormat(Qt.RichText)

        row.addWidget(lbl_info)

        btn = QPushButton("Nommer…")
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
        self.setWindowTitle("Fusionner avec…")
        self.setMinimumWidth(340)
        self.setStyleSheet(_RADIO_STYLE)
        self._target_id: int | None = None
        self._setup_ui(source, [p for p in all_persons if p.id != source.id])

    def _setup_ui(self, source: PersonInfo, others: list[PersonInfo]) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(QLabel(f"Fusionner <b>{source.name}</b> avec :"))

        if not others:
            layout.addWidget(QLabel("Aucune autre personne à fusionner."))
            btn = QDialogButtonBox(QDialogButtonBox.Cancel)
            btn.rejected.connect(self.reject)
            layout.addWidget(btn)
            return

        self._btn_group = QButtonGroup(self)
        self._person_rbs: list[tuple[QRadioButton, str]] = []

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍  Rechercher un nom…")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._filter_persons)
        layout.addWidget(self._search_input)

        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        sc_layout = QVBoxLayout(scroll_content)
        sc_layout.setContentsMargins(4, 4, 4, 4)
        sc_layout.setSpacing(2)

        for p in others:
            rb = QRadioButton(
                f"{p.name}  ({p.photo_count} photo{'s' if p.photo_count != 1 else ''})"
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
            f"Les visages de <i>{source.name}</i> seront rattachés à la personne choisie."
            f"\n{source.name} sera ensuite supprimé."
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
    Dialogue d'identification des groupes de visages.

    Signals
    -------
    cluster_named(cluster_id, person_name)
        L'utilisateur crée une nouvelle personne pour ce cluster.
    cluster_assigned(cluster_id, person_id)
        L'utilisateur associe ce cluster à une personne existante.
    """

    cluster_named    = Signal(int, str)
    cluster_assigned = Signal(int, int)

    def __init__(
        self,
        face_db: FaceDatabase,
        catalog,                     # Catalog — import tardif pour éviter la circularité
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self.setWindowTitle("Identifier les personnes")
        self.setMinimumSize(460, 540)
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        intro = QLabel(
            "PixelPhotoManager a regroupé automatiquement les visages similaires.\n"
            "Nommez chaque groupe pour créer un album par personne."
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

        btn_close = QPushButton("Fermer")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

    def refresh(self) -> None:
        # Arrêter un éventuel chargement d'avatars en cours et libérer le thread Qt enfant
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
                "Tous les groupes ont été nommés.\n\n"
                "Ajoutez de nouvelles photos et relancez\n"
                "l'analyse pour détecter de nouveaux visages."
            )
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #777;")
            self._content_layout.addWidget(lbl)
            return

        # Personnes existantes et leurs centroïdes par groupe
        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)

        person_ids = [p.id for p in persons if p.id is not None]
        person_cluster_embs = self._face_db.get_all_person_cluster_centroids(person_ids)

        avatar_items: list[tuple[int, FaceInfo]] = []
        for cluster_id, face_count in clusters:
            rep = self._face_db.get_representative_face(cluster_id=cluster_id)

            # Suggestion : meilleur score contre chaque centroïde de groupe connu
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

        # Lancer le chargement des avatars en arrière-plan
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
