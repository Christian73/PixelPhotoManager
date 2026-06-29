"""
PersonClusterView — vignettes des visages associés à une personne nommée.

Empilée dans MainWindow._stack à l'index 3.
Double-clic sur une vignette → photo_requested(photo_path).

La vue affiche toujours les visages individuels (dégroupés) :
  - Section confirmée  : visages déjà associés à la personne
    Clic : sélection  Ctrl+clic : multi-sélection  Shift+clic : plage
    Clic-droit : réassigner / dé-associer / définir comme vignette principale
    Double-clic : ouvrir la photo dans la visionneuse
  - Section en attente : suggestions non encore vérifiées (une vignette par groupe suggéré)
    Clic-droit : Accepter / Rejeter la suggestion de ce groupe
    Boutons « Accepter toutes » / « Rejeter toutes » dans l'en-tête
"""

import logging
import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.core.models import FaceInfo, PersonInfo
from src.faces.face_database import FaceDatabase
from src.ui.loading_label import LoadingLabel
from src.ui.people_panel import _AssignDialog, _face_bytes, _load_edit_rotations

logger = logging.getLogger(__name__)

_COLS_MIN  = 2
_THUMB_IMG = 80
_THUMB_W   = 90
_THUMB_GAP = 6
_BTN_OVL   = 22   # diamètre des boutons ✓/✗ overlay

_BTN_ACCEPT_STYLE = (
    "QPushButton { background: rgba(30,150,50,215); color: white;"
    " border-radius: 11px; font-weight: bold; font-size: 13px; border: none; padding: 0; }"
    "QPushButton:hover { background: rgba(50,200,70,255); }"
)
_BTN_REJECT_STYLE = (
    "QPushButton { background: rgba(170,30,30,215); color: white;"
    " border-radius: 11px; font-weight: bold; font-size: 13px; border: none; padding: 0; }"
    "QPushButton:hover { background: rgba(220,50,50,255); }"
)

_MENU_STYLE = (
    "QMenu { background: #2a2a2a; color: #eee; border: 1px solid #555; }"
    "QMenu::item { padding: 6px 20px; }"
    "QMenu::item:selected { background: #3a4a5a; }"
    "QMenu::separator { height: 1px; background: #444; margin: 3px 8px; }"
)


class _PersonsLoaderThread(QThread):
    """Charge les personnes existantes en arrière-plan avant d'ouvrir le dialogue de réallocation."""

    ready = Signal(list)   # list[PersonInfo]

    def __init__(self, catalog, face_db: FaceDatabase, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._face_db = face_db

    def run(self) -> None:
        try:
            persons = self._catalog.get_persons()
            self._face_db.enrich_persons(persons)
            self.ready.emit(persons)
        except Exception:
            logger.exception("_PersonsLoaderThread: erreur inattendue")
            self.ready.emit([])


class _UnassignThread(QThread):
    """Isole les visages dé-associés et calcule des suggestions pour d'autres personnes."""

    done = Signal()

    def __init__(
        self,
        face_db: FaceDatabase,
        face_ids: list[int],
        exclude_person_id: "int | None",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._face_ids = face_ids
        self._exclude_person_id = exclude_person_id

    def run(self) -> None:
        try:
            self._face_db.isolate_and_suggest(self._face_ids, self._exclude_person_id)
        except Exception:
            logger.exception("_UnassignThread error")
        finally:
            self.done.emit()


class _FlatFaceLoader(QThread):
    """Charge les crops de visages individuels en arrière-plan."""

    face_ready = Signal(int, bytes)   # face_id, PNG bytes

    def __init__(
        self,
        faces: list[FaceInfo],
        size: int = _THUMB_IMG,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._faces = faces
        self._size  = size

    def run(self) -> None:
        paths = list({f.photo_path for f in self._faces})
        edit_rots = _load_edit_rotations(paths)
        for face in self._faces:
            data = _face_bytes(face, self._size,
                               edit_rotation=edit_rots.get(face.photo_path, 0))
            if data:
                self.face_ready.emit(face.id, data)


class _FaceThumb(QFrame):
    """Vignette compacte d'un visage individuel."""

    clicked                = Signal(int, bool, bool)  # face_id, ctrl_held, shift_held
    double_clicked         = Signal(str)               # photo_path
    context_menu_requested = Signal(int, object)       # face_id, QPoint global
    accept_clicked         = Signal(int)               # face_id — bouton ✓ overlay
    reject_clicked         = Signal(int)               # face_id — bouton ✗ overlay

    _STYLE_NORMAL = (
        "QFrame { border: 1px solid #3a3a3a; border-radius: 4px; background: #252525; }"
        "QFrame:hover { border-color: #7aabdb; background: #2a3545; }"
    )
    _STYLE_SELECTED = (
        "QFrame { border: 2px solid #7aabdb; border-radius: 4px; background: #1e3a5a; }"
        "QFrame:hover { border-color: #9fcbf5; background: #243f5a; }"
    )
    _STYLE_PENDING = (
        "QFrame { border: 2px solid #7a5a10; border-radius: 4px; background: #231e0a; }"
        "QFrame:hover { border-color: #e8a040; background: #2a260a; }"
    )

    def __init__(self, face: FaceInfo, parent=None) -> None:
        super().__init__(parent)
        self._face_id    = face.id
        self._photo_path = face.photo_path
        self.setFixedSize(_THUMB_W, _THUMB_W)
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._STYLE_NORMAL)
        self.setToolTip(os.path.basename(face.photo_path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        self._lbl_img = LoadingLabel("#1a1a1a")
        self._lbl_img.setFixedSize(_THUMB_IMG, _THUMB_IMG)
        self._lbl_img.setAlignment(Qt.AlignCenter)
        self._lbl_img.setStyleSheet("border: none; border-radius: 3px;")
        self._lbl_img.start_loading()
        layout.addWidget(self._lbl_img, alignment=Qt.AlignCenter)

        # Boutons overlay ✓/✗ pour suggestions en attente (masqués par défaut)
        _y = _THUMB_W - _BTN_OVL - 3
        self._btn_accept = QPushButton("✓", self)
        self._btn_accept.setGeometry(_THUMB_W - _BTN_OVL - 3, _y, _BTN_OVL, _BTN_OVL)
        self._btn_accept.setStyleSheet(_BTN_ACCEPT_STYLE)
        self._btn_accept.setCursor(Qt.PointingHandCursor)
        self._btn_accept.setToolTip("Accepter cette suggestion")
        self._btn_accept.hide()
        self._btn_accept.clicked.connect(lambda: self.accept_clicked.emit(self._face_id))

        self._btn_reject = QPushButton("✗", self)
        self._btn_reject.setGeometry(3, _y, _BTN_OVL, _BTN_OVL)
        self._btn_reject.setStyleSheet(_BTN_REJECT_STYLE)
        self._btn_reject.setCursor(Qt.PointingHandCursor)
        self._btn_reject.setToolTip("Rejeter cette suggestion")
        self._btn_reject.hide()
        self._btn_reject.clicked.connect(lambda: self.reject_clicked.emit(self._face_id))

    def set_image(self, data: bytes) -> None:
        pix = QPixmap()
        pix.loadFromData(data)
        scaled = pix.scaled(
            _THUMB_IMG, _THUMB_IMG,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._lbl_img.setPixmap(scaled)

    def set_selected(self, selected: bool) -> None:
        self.setStyleSheet(
            self._STYLE_SELECTED if selected else self._STYLE_NORMAL
        )

    def set_pending(self, pending: bool) -> None:
        self.setStyleSheet(self._STYLE_PENDING if pending else self._STYLE_NORMAL)
        self._btn_accept.setVisible(pending)
        self._btn_reject.setVisible(pending)
        if pending:
            self._btn_accept.raise_()
            self._btn_reject.raise_()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            ctrl  = bool(event.modifiers() & Qt.ControlModifier)
            shift = bool(event.modifiers() & Qt.ShiftModifier)
            self.clicked.emit(self._face_id, ctrl, shift)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self._photo_path)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(self._face_id, event.globalPos())


class PersonClusterView(QWidget):
    """
    Vue des visages associés à une personne nommée.
    Empilée dans MainWindow._stack à l'index 3.
    """

    photos_requested    = Signal(int, str)   # cluster_id, label (conservé pour compatibilité)
    photo_requested     = Signal(str)        # photo_path — double-clic sur une vignette
    back_requested      = Signal()
    cluster_unassigned  = Signal(int)        # cluster_id — groupe dé-associé
    cluster_named       = Signal(int, str)   # cluster_id, name
    cluster_assigned    = Signal(int, int)   # cluster_id, person_id
    faces_reassigned    = Signal()           # visages réassignés
    cover_face_set      = Signal(int, object)  # person_id, FaceInfo
    suggestion_accepted      = Signal(int)   # cluster_id confirmé
    suggestion_rejected      = Signal(int)   # cluster_id refusé
    all_suggestions_accepted = Signal(list)  # tous les cluster_ids confirmés d'un coup
    all_suggestions_rejected = Signal(list)  # tous les cluster_ids refusés d'un coup
    add_to_album_requested    = Signal(list)  # list[PhotoInfo] — ajouter à album existant
    create_album_with_requested = Signal(list)  # list[PhotoInfo] — créer nouvel album

    def __init__(self, face_db: FaceDatabase, catalog, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._catalog = catalog
        self._person: PersonInfo | None = None

        # Vignettes confirmées
        self._flat_cards: dict[int, _FaceThumb] = {}
        self._flat_loader: _FlatFaceLoader | None = None
        self._selection: set[int] = set()
        self._flat_order: list[int] = []
        self._last_clicked: int | None = None

        # Chargement par lots (évite de bloquer l'UI sur ~5000 widgets)
        self._flat_batch_gen: int = 0
        self._flat_pending:  list = []
        self._flat_faces_all: list = []
        self._flat_cols: int = 1

        # Vignettes en attente de vérification (une par groupe suggéré)
        self._pending_flat_cards: dict[int, _FaceThumb] = {}
        self._pending_flat_loader: _FlatFaceLoader | None = None
        self._pending_thumb_clusters: dict[int, int] = {}  # face_id → cluster_id

        self._persons_loader: _PersonsLoaderThread | None = None
        self._build()

    @property
    def current_person(self) -> "PersonInfo | None":
        return self._person

    # ------------------------------------------------------------------ public

    def set_person(self, person: PersonInfo) -> None:
        if self._person is None or self._person.id != person.id:
            self._person = person
            self._refresh()
        else:
            self._lbl_title.setText(f"Visages de {person.name}")
            self._person = person

    def refresh(self) -> None:
        """Force un rafraîchissement complet de la vue (même personne)."""
        self._refresh()

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Barre titre
        header = QWidget()
        header.setStyleSheet("background: #1e1e1e; border-bottom: 1px solid #333;")
        header.setFixedHeight(44)
        h = QHBoxLayout(header)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(8)

        btn_back = QPushButton("← Retour")
        btn_back.setStyleSheet(
            "QPushButton { color: #7aabdb; border: none; font-size: 12px; background: transparent; }"
            "QPushButton:hover { color: #9fcbf5; }"
        )
        btn_back.setCursor(Qt.PointingHandCursor)
        btn_back.setFixedHeight(28)
        btn_back.clicked.connect(self.back_requested)
        h.addWidget(btn_back)

        self._lbl_title = QLabel()
        self._lbl_title.setStyleSheet(
            "color: #eee; font-size: 13px; font-weight: bold; background: transparent;"
        )
        self._lbl_title.setAlignment(Qt.AlignCenter)
        h.addWidget(self._lbl_title, stretch=1)

        root.addWidget(header)

        # Zone de scroll
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: #1a1a1a; }")

        self._content = QWidget()
        self._content.setStyleSheet("background: #1a1a1a;")
        content_vbox = QVBoxLayout(self._content)
        content_vbox.setContentsMargins(16, 16, 16, 16)
        content_vbox.setSpacing(16)
        content_vbox.setAlignment(Qt.AlignTop)

        # Section confirmée
        self._confirmed_area = QWidget()
        self._confirmed_area.setStyleSheet("background: transparent;")
        self._flow = QGridLayout(self._confirmed_area)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setSpacing(_THUMB_GAP)
        self._flow.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        content_vbox.addWidget(self._confirmed_area)

        # Section en attente de vérification
        self._pending_section = QWidget()
        self._pending_section.setStyleSheet("background: transparent;")
        self._pending_section.setVisible(False)
        pending_vbox = QVBoxLayout(self._pending_section)
        pending_vbox.setContentsMargins(0, 0, 0, 0)
        pending_vbox.setSpacing(8)

        pending_hdr = QHBoxLayout()
        pending_hdr.setSpacing(8)
        lbl_pending = QLabel("En attente de vérification")
        lbl_pending.setStyleSheet(
            "color: #e8a040; font-size: 11px; font-weight: bold; background: transparent;"
        )
        pending_hdr.addWidget(lbl_pending)
        pending_hdr.addStretch()

        self._btn_reject_all = QPushButton("✗ Rejeter toutes")
        self._btn_reject_all.setCursor(Qt.PointingHandCursor)
        self._btn_reject_all.setFixedHeight(24)
        self._btn_reject_all.setStyleSheet(
            "QPushButton { color: #cc5555; border: 1px solid #cc5555; border-radius: 3px;"
            " background: #221111; font-size: 11px; padding: 0 10px; }"
            "QPushButton:hover { background: #3a1a1a; border-color: #ee7777; }"
        )
        self._btn_reject_all.clicked.connect(self._on_reject_all)
        pending_hdr.addWidget(self._btn_reject_all)

        self._btn_accept_all = QPushButton("✓ Accepter toutes")
        self._btn_accept_all.setCursor(Qt.PointingHandCursor)
        self._btn_accept_all.setFixedHeight(24)
        self._btn_accept_all.setStyleSheet(
            "QPushButton { color: #4dbb5a; border: 1px solid #4dbb5a; border-radius: 3px;"
            " background: #112211; font-size: 11px; padding: 0 10px; }"
            "QPushButton:hover { background: #1a3a1a; border-color: #6ddb7a; }"
        )
        self._btn_accept_all.clicked.connect(self._on_accept_all)
        pending_hdr.addWidget(self._btn_accept_all)

        pending_vbox.addLayout(pending_hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: #e8a040; border: none;")
        sep.setFixedHeight(1)
        pending_vbox.addWidget(sep)

        self._pending_area = QWidget()
        self._pending_area.setStyleSheet("background: transparent;")
        self._pending_grid = QGridLayout(self._pending_area)
        self._pending_grid.setContentsMargins(0, 0, 0, 0)
        self._pending_grid.setSpacing(_THUMB_GAP)
        self._pending_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        pending_vbox.addWidget(self._pending_area)

        content_vbox.addWidget(self._pending_section)
        content_vbox.addStretch(1)

        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, stretch=1)

        self._lbl_empty = QLabel("Aucun visage associé à cette personne.")
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet("color: #555; font-size: 13px;")
        self._lbl_empty.hide()
        root.addWidget(self._lbl_empty)

    # ------------------------------------------------------------------ refresh

    def _stop_loaders(self) -> None:
        self._flat_batch_gen += 1
        self._flat_pending = []

        for loader, sig_name in (
            (self._flat_loader,         "face_ready"),
            (self._pending_flat_loader, "face_ready"),
        ):
            if loader is None:
                continue
            try:
                sig = getattr(loader, sig_name, None)
                if sig is not None:
                    sig.disconnect()
                if loader.isRunning():
                    loader.finished.connect(loader.deleteLater)
                else:
                    loader.deleteLater()
            except RuntimeError:
                pass
        self._flat_loader         = None
        self._pending_flat_loader = None

    def _clear_grid(self) -> None:
        for i in range(self._flow.count() - 1, -1, -1):
            item = self._flow.takeAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        for i in range(self._pending_grid.count() - 1, -1, -1):
            item = self._pending_grid.takeAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        self._flat_cards.clear()
        self._pending_flat_cards.clear()
        self._pending_thumb_clusters.clear()
        self._selection.clear()
        self._flat_order.clear()
        self._last_clicked = None
        self._pending_section.setVisible(False)

    def _refresh(self) -> None:
        if self._person is None:
            return
        self._lbl_title.setText(f"Visages de {self._person.name}")
        self._stop_loaders()
        self._clear_grid()
        self._refresh_flat()

    # Nombre de widgets _FaceThumb créés par tranche de QTimer.
    _FLAT_BATCH = 100

    def _refresh_flat(self) -> None:
        confirmed = self._face_db.get_faces_for_person(self._person.id)
        pending   = self._face_db.get_suggested_clusters_for_person(self._person.id)

        if not confirmed and not pending:
            self._scroll.hide()
            self._lbl_empty.setText("Aucun visage associé à cette personne.")
            self._lbl_empty.show()
            return

        self._lbl_empty.hide()
        self._scroll.show()

        cols = self._compute_cols(_THUMB_W, _THUMB_GAP)

        # ── Section en attente ─────────────────────────────────────────────
        if pending:
            pending_cluster_ids = [cid for cid, _, _ in pending]
            rep_faces = self._face_db.get_all_representative_faces(pending_cluster_ids)
            pending_faces: list[FaceInfo] = []

            for idx, (cluster_id, face_count, score) in enumerate(pending):
                face = rep_faces.get(cluster_id)
                if face is None:
                    continue
                thumb = _FaceThumb(face, self._content)
                thumb.set_pending(True)
                plural = "s" if face_count > 1 else ""
                thumb.setToolTip(
                    f"{os.path.basename(face.photo_path)}\n"
                    f"Suggestion {int(score * 100)} % — {face_count} visage{plural}"
                )
                thumb.double_clicked.connect(self.photo_requested)
                thumb.context_menu_requested.connect(self._on_pending_thumb_context_menu)
                thumb.accept_clicked.connect(self._on_pending_accept_by_face)
                thumb.reject_clicked.connect(self._on_pending_reject_by_face)
                self._pending_grid.addWidget(thumb, idx // cols, idx % cols)
                self._pending_flat_cards[face.id] = thumb
                self._pending_thumb_clusters[face.id] = cluster_id
                pending_faces.append(face)

            if pending_faces:
                self._pending_flat_loader = _FlatFaceLoader(pending_faces, _THUMB_IMG, self)
                self._pending_flat_loader.face_ready.connect(self._on_face_ready)
                self._pending_flat_loader.finished.connect(self._pending_flat_loader.deleteLater)
                self._pending_flat_loader.start()

            self._pending_section.setVisible(True)

        # ── Section confirmée (chargement par lots) ────────────────────────
        self._flat_order     = [face.id for face in confirmed]
        self._flat_faces_all = list(confirmed)
        self._flat_cols      = cols
        self._flat_pending   = list(enumerate(confirmed))
        gen = self._flat_batch_gen
        self._add_flat_batch(gen)

    def _add_flat_batch(self, gen: int) -> None:
        if gen != self._flat_batch_gen or not self._flat_pending:
            if gen == self._flat_batch_gen and not self._flat_pending:
                self._start_flat_loader()
            return

        batch, self._flat_pending = (
            self._flat_pending[: self._FLAT_BATCH],
            self._flat_pending[self._FLAT_BATCH :],
        )
        cols = self._flat_cols
        for idx, face in batch:
            thumb = _FaceThumb(face, self._content)
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.double_clicked.connect(self.photo_requested)
            thumb.context_menu_requested.connect(self._on_thumb_context_menu)
            self._flow.addWidget(thumb, idx // cols, idx % cols)
            self._flat_cards[face.id] = thumb

        if self._flat_pending:
            QTimer.singleShot(0, lambda g=gen: self._add_flat_batch(g))
        else:
            self._start_flat_loader()

    def _start_flat_loader(self) -> None:
        self._flat_loader = _FlatFaceLoader(self._flat_faces_all, _THUMB_IMG, self)
        self._flat_loader.face_ready.connect(self._on_face_ready)
        self._flat_loader.finished.connect(self._flat_loader.deleteLater)
        self._flat_loader.start()

    # ------------------------------------------------------------------ cols / reflow

    def _compute_cols(self, card_w: int, gap: int) -> int:
        w = self._scroll.width() or 600
        return max(_COLS_MIN, (w - 32) // (card_w + gap))

    def _reflow(self) -> None:
        cols = self._compute_cols(_THUMB_W, _THUMB_GAP)
        for i, thumb in enumerate(self._flat_cards.values()):
            self._flow.addWidget(thumb, i // cols, i % cols)
        for i, thumb in enumerate(self._pending_flat_cards.values()):
            self._pending_grid.addWidget(thumb, i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    # ------------------------------------------------------------------ slots loaders

    @Slot(int, bytes)
    def _on_face_ready(self, face_id: int, data: bytes) -> None:
        thumb = self._flat_cards.get(face_id) or self._pending_flat_cards.get(face_id)
        if thumb:
            thumb.set_image(data)

    # ------------------------------------------------------------------ pending accept/reject

    @Slot(int)
    def _on_pending_accepted(self, cluster_id: int) -> None:
        self.suggestion_accepted.emit(cluster_id)

    @Slot(int)
    def _on_pending_rejected(self, cluster_id: int) -> None:
        self.suggestion_rejected.emit(cluster_id)

    @Slot()
    def _on_reject_all(self) -> None:
        cluster_ids = list(set(self._pending_thumb_clusters.values()))
        if cluster_ids:
            self.all_suggestions_rejected.emit(cluster_ids)

    @Slot()
    def _on_accept_all(self) -> None:
        cluster_ids = list(set(self._pending_thumb_clusters.values()))
        if cluster_ids:
            self.all_suggestions_accepted.emit(cluster_ids)

    @Slot(int)
    def _on_pending_accept_by_face(self, face_id: int) -> None:
        cluster_id = self._pending_thumb_clusters.get(face_id)
        if cluster_id is not None:
            self.suggestion_accepted.emit(cluster_id)

    @Slot(int)
    def _on_pending_reject_by_face(self, face_id: int) -> None:
        cluster_id = self._pending_thumb_clusters.get(face_id)
        if cluster_id is not None:
            self.suggestion_rejected.emit(cluster_id)

    @Slot(int, object)
    def _on_pending_thumb_context_menu(self, face_id: int, pos) -> None:
        cluster_id = self._pending_thumb_clusters.get(face_id)
        if cluster_id is None:
            return
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLE)
        act_accept = menu.addAction("✓ Accepter cette suggestion")
        act_reject = menu.addAction("✗ Rejeter cette suggestion")
        chosen = menu.exec(pos)
        if chosen == act_accept:
            self.suggestion_accepted.emit(cluster_id)
        elif chosen == act_reject:
            self.suggestion_rejected.emit(cluster_id)

    # ------------------------------------------------------------------ suppression rapide de suggestions

    def remove_pending_cluster(self, cluster_id: int) -> None:
        """Retire la vignette de suggestion rejetée sans recharger toute la grille."""
        to_remove = [fid for fid, cid in self._pending_thumb_clusters.items()
                     if cid == cluster_id]
        if not to_remove:
            return
        for fid in to_remove:
            thumb = self._pending_flat_cards.pop(fid, None)
            if thumb:
                self._pending_grid.removeWidget(thumb)
                thumb.deleteLater()
            self._pending_thumb_clusters.pop(fid, None)

        if not self._pending_flat_cards:
            self._pending_section.setVisible(False)
            return

        # Re-layouter les vignettes restantes pour combler le trou
        cols = self._compute_cols(_THUMB_W, _THUMB_GAP)
        for i, thumb in enumerate(self._pending_flat_cards.values()):
            self._pending_grid.addWidget(thumb, i // cols, i % cols)

    def clear_all_pending(self) -> None:
        """Retire toutes les vignettes de suggestions sans recharger la grille."""
        for thumb in self._pending_flat_cards.values():
            self._pending_grid.removeWidget(thumb)
            thumb.deleteLater()
        self._pending_flat_cards.clear()
        self._pending_thumb_clusters.clear()
        self._pending_section.setVisible(False)

    def accept_pending_cluster(self, cluster_id: int) -> None:
        """Déplace les visages du cluster accepté de la section en attente vers
        la section confirmée, sans recharger toute la grille."""
        # 1. Supprimer la vignette en attente
        self.remove_pending_cluster(cluster_id)

        # 2. Récupérer les visages nouvellement confirmés
        new_faces = self._face_db.get_faces_by_cluster(cluster_id)
        if not new_faces:
            return

        self._lbl_empty.hide()
        self._scroll.show()

        # 3. Appendre les nouvelles vignettes à la grille confirmée
        cols = self._compute_cols(_THUMB_W, _THUMB_GAP)
        start_idx = len(self._flat_cards)
        for i, face in enumerate(new_faces):
            thumb = _FaceThumb(face, self._content)
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.double_clicked.connect(self.photo_requested)
            thumb.context_menu_requested.connect(self._on_thumb_context_menu)
            idx = start_idx + i
            self._flow.addWidget(thumb, idx // cols, idx % cols)
            self._flat_cards[face.id] = thumb
            self._flat_order.append(face.id)

        # 4. Charger les images uniquement pour les nouveaux visages
        loader = _FlatFaceLoader(new_faces, _THUMB_IMG, self)
        loader.face_ready.connect(self._on_face_ready)
        loader.finished.connect(loader.deleteLater)
        loader.start()

    # ------------------------------------------------------------------ sélection (visages confirmés)

    @Slot(int, bool, bool)
    def _on_thumb_clicked(self, face_id: int, ctrl_held: bool, shift_held: bool) -> None:
        if shift_held and self._last_clicked is not None and self._last_clicked in self._flat_cards:
            try:
                a = self._flat_order.index(self._last_clicked)
                b = self._flat_order.index(face_id)
                lo, hi = min(a, b), max(a, b)
                self._selection = set(self._flat_order[lo:hi + 1])
            except ValueError:
                self._selection = {face_id}
        elif ctrl_held:
            if face_id in self._selection:
                self._selection.discard(face_id)
            else:
                self._selection.add(face_id)
            self._last_clicked = face_id
        else:
            self._selection = {face_id}
            self._last_clicked = face_id
        self._apply_selection_style()

    def _apply_selection_style(self) -> None:
        for fid, thumb in self._flat_cards.items():
            thumb.set_selected(fid in self._selection)

    # ------------------------------------------------------------------ menu contextuel (visages confirmés)

    @Slot(int, object)
    def _on_thumb_context_menu(self, face_id: int, pos) -> None:
        if face_id not in self._selection:
            self._selection = {face_id}
            self._apply_selection_style()

        n = len(self._selection)
        s = "s" if n > 1 else ""

        # Photos uniques pour la sélection courante (dédoublonnage par chemin)
        selected_paths = {
            self._flat_cards[fid]._photo_path
            for fid in self._selection if fid in self._flat_cards
        }
        np = len(selected_paths)
        lbl_photos = f"les {np} photo(s)" if np > 1 else "cette photo"

        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLE)
        act_reassign = menu.addAction(f"Réassigner {n} visage{s} à une autre personne…")
        act_unassign = menu.addAction(f"Dé-associer {n} visage{s} de la personne")
        menu.addSeparator()
        act_cover = menu.addAction("Utiliser ce visage comme vignette principale")
        menu.addSeparator()
        act_add_album = menu.addAction(f"Ajouter {lbl_photos} à un album…")
        act_new_album = menu.addAction(f"Créer un nouvel album avec {lbl_photos}…")

        chosen = menu.exec(pos)
        if chosen == act_reassign:
            self._start_flat_reassign(list(self._selection))
        elif chosen == act_unassign:
            self._flat_unassign(list(self._selection))
        elif chosen == act_cover:
            self._set_cover_face(face_id)
        elif chosen in (act_add_album, act_new_album):
            photos = [self._catalog.get_photo_by_path(p) for p in selected_paths]
            photos = [p for p in photos if p is not None]
            if photos:
                if chosen == act_add_album:
                    self.add_to_album_requested.emit(photos)
                else:
                    self.create_album_with_requested.emit(photos)

    def _start_flat_reassign(self, face_ids: list[int]) -> None:
        if self._persons_loader is not None:
            try:
                if self._persons_loader.isRunning():
                    return
            except RuntimeError:
                pass
            self._persons_loader = None
        self._persons_loader = _PersonsLoaderThread(self._catalog, self._face_db, self)
        self._persons_loader.ready.connect(
            lambda persons, fids=face_ids: self._show_flat_reassign_dialog(fids, persons)
        )
        self._persons_loader.finished.connect(self._clear_persons_loader)
        self._persons_loader.start()

    def _show_flat_reassign_dialog(
        self, face_ids: list[int], persons: list[PersonInfo]
    ) -> None:
        current_pid   = self._person.id if self._person else None
        other_persons = [p for p in persons if p.id != current_pid]

        dlg = _AssignDialog(-1, other_persons, show_ignore=False, parent=self)
        n   = len(face_ids)
        dlg.setWindowTitle(
            f"Réassigner {n} visage{'s' if n > 1 else ''} à une autre personne"
        )
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.is_new_person():
            name = dlg.new_name()
            if not name:
                return
            new_person = self._catalog.create_person(name)
            person_id  = new_person.id
        else:
            person_id = dlg.existing_person_id()
            if person_id is None:
                return

        self._face_db.assign_person_to_faces(face_ids, person_id)
        self._remove_flat_thumbs(face_ids)
        self.faces_reassigned.emit()

    def _remove_flat_thumbs(self, face_ids: list[int]) -> None:
        for fid in face_ids:
            thumb = self._flat_cards.pop(fid, None)
            if thumb:
                self._flow.removeWidget(thumb)
                thumb.deleteLater()
        self._selection.clear()
        if not self._flat_cards:
            self._scroll.hide()
            self._lbl_empty.setText("Aucun visage associé à cette personne.")
            self._lbl_empty.show()
        else:
            self._reflow()

    def _flat_unassign(self, face_ids: list[int]) -> None:
        self._remove_flat_thumbs(face_ids)
        exclude_pid = self._person.id if self._person else None
        self._unassign_thread = _UnassignThread(
            self._face_db, face_ids, exclude_pid, self
        )
        self._unassign_thread.done.connect(self.faces_reassigned)
        self._unassign_thread.done.connect(self._unassign_thread.deleteLater)
        self._unassign_thread.start()

    def _set_cover_face(self, face_id: int) -> None:
        self._face_db.set_cover_face(face_id)
        if self._person:
            face = self._face_db.get_face_by_id(face_id)
            if face:
                self.cover_face_set.emit(self._person.id, face)
        self.faces_reassigned.emit()

    def _clear_persons_loader(self) -> None:
        if self._persons_loader is not None:
            self._persons_loader.deleteLater()
            self._persons_loader = None
