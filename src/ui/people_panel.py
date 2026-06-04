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

def _face_bytes(face: "FaceInfo", size: int) -> bytes:
    """Decode face crop as PNG bytes. Safe to call from any thread."""
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(face.photo_path)).convert("RGB")
        x, y, w, h = face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h
        pad = int(max(w, h) * 0.18)
        crop = img.crop((
            max(0, x - pad), max(0, y - pad),
            min(img.width, x + w + pad), min(img.height, y + h + pad),
        )).resize((size, size))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        logger.debug("_face_bytes: %s", exc)
        return b""


def load_face_pixmap(face: "FaceInfo", size: int = _AVATAR_SIZE) -> QPixmap:
    """Return a QPixmap of the face crop. Must be called from the UI thread."""
    data = _face_bytes(face, size)
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
_SIM_STRONG  = 0.82   # très probable  → libellé en bleu
_SIM_WEAK    = 0.68   # possible        → libellé en gris


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
        for cluster_id, face in self._items:
            data = _face_bytes(face, self._size)
            if data:
                self.avatar_ready.emit(cluster_id, data)


# ------------------------------------------------------------------ assign dialog

class _AssignDialog(QDialog):
    """
    Dialog shown when the user clicks 'Nommer…' on a cluster.
    Offers two choices:
      • Associer à une personne existante  (radio list)
      • Créer une nouvelle personne         (text input)
    """

    def __init__(
        self,
        cluster_id: int,
        existing_persons: list[PersonInfo],
        suggested_person_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Identifier le groupe {cluster_id}")
        self.setMinimumWidth(360)
        self.setStyleSheet(_RADIO_STYLE)
        self._selected_person_id: int | None = None
        self._new_name: str = ""
        self._ignored: bool = False
        self._setup_ui(cluster_id, existing_persons, suggested_person_id)

    def _setup_ui(
        self,
        cluster_id: int,
        persons: list[PersonInfo],
        suggested_person_id: int | None,
    ) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        layout.addWidget(QLabel(f"Qui est la personne du groupe {cluster_id} ?"))

        self._btn_group = QButtonGroup(self)
        preselect_rb = None

        if persons:
            lbl_existing = QLabel("Associer à une personne existante :")
            lbl_existing.setStyleSheet("color: #aaa; font-size: 11px;")
            layout.addWidget(lbl_existing)

            for p in persons:
                rb = QRadioButton(f"{p.name}  ({p.photo_count} photo{'s' if p.photo_count != 1 else ''})")
                rb.setProperty("person_id", p.id)
                self._btn_group.addButton(rb)
                layout.addWidget(rb)
                if p.id == suggested_person_id:
                    preselect_rb = rb

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #444;")
        layout.addWidget(sep)

        # New person option
        rb_new = QRadioButton("Créer une nouvelle personne :")
        self._btn_group.addButton(rb_new)
        self._rb_new = rb_new
        layout.addWidget(rb_new)

        # Pré-sélection : suggestion si disponible, sinon "Créer"
        if preselect_rb:
            preselect_rb.setChecked(True)
        else:
            rb_new.setChecked(True)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Nom de la personne…")
        self._name_input.textChanged.connect(lambda: rb_new.setChecked(True))
        layout.addWidget(self._name_input)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color: #444;")
        layout.addWidget(sep2)

        # Ignore option
        rb_ignore = QRadioButton("Ignorer ce groupe")
        self._btn_group.addButton(rb_ignore)
        self._rb_ignore = rb_ignore
        layout.addWidget(rb_ignore)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        checked = self._btn_group.checkedButton()
        if checked is self._rb_ignore:
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

    # Result accessors

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

        self._lbl_avatar = QLabel()
        self._lbl_avatar.setFixedSize(_AVATAR_SIZE, _AVATAR_SIZE)
        self._lbl_avatar.setAlignment(Qt.AlignCenter)
        self._lbl_avatar.setStyleSheet("border: none;")
        self._lbl_avatar.setPixmap(_placeholder_pixmap())
        row.addWidget(self._lbl_avatar)

        plural = "s" if face_count > 1 else ""
        info_text = f"Groupe {cluster_id}\n{face_count} visage{plural}"
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
                f"Groupe {cluster_id} — {face_count} visage{plural}\n"
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

        layout.addWidget(QLabel(
            f"Fusionner <b>{source.name}</b> avec :"
        ))

        if not others:
            layout.addWidget(QLabel("Aucune autre personne à fusionner."))
            btn = QDialogButtonBox(QDialogButtonBox.Cancel)
            btn.rejected.connect(self.reject)
            layout.addWidget(btn)
            return

        self._btn_group = QButtonGroup(self)
        for p in others:
            rb = QRadioButton(f"{p.name}  ({p.photo_count} photo{'s' if p.photo_count != 1 else ''})")
            rb.setProperty("person_id", p.id)
            self._btn_group.addButton(rb)
            layout.addWidget(rb)
        self._btn_group.buttons()[0].setChecked(True)

        note = QLabel(f"Les visages de <i>{source.name}</i> seront rattachés à la personne choisie.\n{source.name} sera ensuite supprimé.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
        # Arrêter un éventuel chargement d'avatars en cours
        if hasattr(self, "_avatar_loader") and self._avatar_loader.isRunning():
            self._avatar_loader.terminate()
            self._avatar_loader.wait()

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

        # Personnes existantes et leurs embeddings représentatifs
        persons = self._catalog.get_persons()
        self._face_db.enrich_persons(persons)

        person_embeddings: dict[int, list[float]] = {}
        for p in persons:
            emb = self._face_db.get_representative_embedding(person_id=p.id)
            if emb:
                person_embeddings[p.id] = emb

        avatar_items: list[tuple[int, FaceInfo]] = []
        for cluster_id, face_count in clusters:
            rep = self._face_db.get_representative_face(cluster_id=cluster_id)

            # Calculer la meilleure suggestion de personne
            suggestion: tuple[str, int, float] | None = None
            if persons and person_embeddings:
                cluster_emb = self._face_db.get_representative_embedding(
                    cluster_id=cluster_id
                )
                if cluster_emb:
                    best_sim, best_person = 0.0, None
                    for p in persons:
                        p_emb = person_embeddings.get(p.id)
                        if p_emb:
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
