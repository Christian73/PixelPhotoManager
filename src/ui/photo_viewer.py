# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import copy
import io
import logging
import math
import os
import uuid

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, Slot, QPoint, QRectF, QPointF, QSize, QFileInfo
from PySide6.QtGui import (
    QDesktopServices, QPixmap, QPainter, QKeyEvent, QWheelEvent,
    QMouseEvent, QPen, QBrush, QColor, QPainterPath, QPolygonF, QIcon, QFont, QTextCursor,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSizePolicy, QToolButton, QButtonGroup, QMenu, QFileIconProvider, QTextEdit,
)

from src.core.models import PhotoInfo, EditInfo
from src.processing.edit_database import EditDatabase
from src.processing.adjustments import ImageAdjuster
from src.processing.annotation_geometry import catmull_rom_to_bezier_segments
from src.ui.annotation_renderer import (
    render_annotations, hit_test_annotations, annotation_screen_bounds,
)

logger = logging.getLogger(__name__)

# Résolution maximale pour l'affichage à l'écran.
# Les retouches (rotation, recadrage, etc.) s'appliquent sur cette copie réduite.
# L'image originale pleine résolution n'est utilisée que pour l'export final.
# ------------------------------------------------------------------ modules extraits
# (2026-07) Pipeline pixmap et canvas déplacés dans leurs modules ; noms
# ré-exportés (le diaporama importe _build_pixmap depuis photo_viewer).
from src.ui.viewer_pixmaps import (  # noqa: E402,F401
    _PREVIEW_MAX_PX, _apply_edit_to_base, _build_base_image, _build_pixmap,
    _build_video_base_image, _build_video_pixmap, _to_rgb,
)
from src.ui.viewer_canvas import (  # noqa: E402,F401
    _ANNOTATION_CORNER_CURSORS, _CORNER_CURSORS, _CROP_FORMAT_DATA,
    _EDGE_INDICES, _HANDLE_HIT, _Canvas, _InlineTextEdit, _make_rect_quad,
)

_BTN_CROP_STYLE = """
QToolButton {
    background: rgba(50,50,50,180);
    border: 1px solid rgba(255,255,255,35);
    border-radius: 3px;
    padding: 1px 4px;
    color: #999;
    font-size: 9px;
}
QToolButton:checked {
    background: rgba(50,110,170,230);
    border: 1px solid rgba(110,170,230,200);
    color: white;
}
QToolButton:hover:!checked {
    background: rgba(70,70,70,200);
    border: 1px solid rgba(255,255,255,60);
    color: #ccc;
}
"""


def _fmt_icon(ratio: float | None, iw: int = 24, ih: int = 18) -> QPixmap:
    """Icône représentant le ratio w/h par un rectangle aux bonnes proportions."""
    px = QPixmap(iw, ih)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    mg = 2
    mw, mh = iw - 2 * mg, ih - 2 * mg
    if ratio is None:
        rw, rh = mw, mh
        p.setPen(QPen(QColor(170, 170, 170), 1.2, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
    elif ratio >= 1:
        rw = mw
        rh = max(4, round(mw / ratio))
        p.setPen(QPen(QColor(190, 190, 190), 1.2))
        p.setBrush(QColor(130, 130, 130, 55))
    else:
        rh = mh
        rw = max(4, round(mh * ratio))
        p.setPen(QPen(QColor(190, 190, 190), 1.2))
        p.setBrush(QColor(130, 130, 130, 55))
    rx = (iw - rw) // 2
    ry = (ih - rh) // 2
    p.drawRect(rx, ry, rw - 1, rh - 1)
    p.end()
    return px

# 4 poignées de coin : TL(0), TR(1), BR(2), BL(3)


class _BaseLoader(QThread):
    """Charge _build_base_image dans un thread secondaire.
    Nécessaire pour les vidéos : cv2.VideoCapture peut marshaler des appels COM
    sur le thread UI (STA Windows) et provoquer des freezes si appelé directement."""

    base_ready = Signal(str, object)   # (photo_path, tuple[bytes,int,int] | None)

    def __init__(self, photo: "PhotoInfo", parent=None) -> None:
        super().__init__(parent)
        self._photo = photo

    def run(self) -> None:
        result = _build_base_image(self._photo)
        self.base_ready.emit(self._photo.path, result)


class PhotoViewer(QWidget):
    closed               = Signal()
    navigate             = Signal(int)
    zoom_changed         = Signal(float)
    crop_ready           = Signal(object)  # tuple 8 coords relatives (x0,y0,…,x3,y3)
    crop_mode_ended      = Signal()        # mode recadrage terminé (validé ou annulé)
    save_requested       = Signal(object)  # PhotoInfo
    rename_requested     = Signal(object)  # PhotoInfo
    move_requested       = Signal(object)  # PhotoInfo
    delete_requested            = Signal(list)    # list[PhotoInfo]
    remove_from_album_requested = Signal(list)    # list[PhotoInfo] — retrait d'album (non destructif)
    dup_badge_clicked    = Signal(object)  # PhotoInfo — badge de doublon cliqué
    red_eye_point_added         = Signal(float, float)  # cx_norm, cy_norm (0-1)
    pixel_sampled               = Signal(int, int, int)  # R, G, B — pipette balance des blancs
    face_context_menu_requested = Signal(object, object)  # (FaceInfo, QPoint global)
    vignette_changed            = Signal(object)  # EditInfo (géométrie après drag)
    face_bbox_ready             = Signal(object)  # tuple (bbox_x,bbox_y,bbox_w,bbox_h) int
    face_add_mode_ended         = Signal()  # mode ajout de visage terminé (validé ou annulé)
    force_redetect_requested    = Signal(object)  # PhotoInfo — menu contextuel
    folder_grid_requested       = Signal(object)  # PhotoInfo — menu contextuel : grille du dossier
    favorite_toggle_requested   = Signal(object)  # PhotoInfo — bascule favori demandée
    annotation_added             = Signal(object)  # dict annotation ajoutée
    annotation_deleted           = Signal(str)     # id de l'annotation supprimée
    annotation_deleted_multi     = Signal(object)  # list[str] ids supprimés (suppression groupée)
    annotation_selection_changed = Signal(object)  # list[str] ids sélectionnés (peut être vide)
    annotation_moved              = Signal(str, object)  # (id, dict annotation à jour)
    annotation_moved_multi        = Signal(object)  # dict[id, annotation à jour] (déplacement groupé)
    annotation_resized            = Signal(str, object)  # (id, dict annotation à jour)
    annotation_grouped            = Signal(object)  # dict[id, annotation à jour] (groupe/dégroupe)

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self._config = config
        self._photo: PhotoInfo | None = None
        self._edit: EditInfo | None = None
        self._db = EditDatabase()
        # Cache de l'image de base (1024px, sans retouche) pour la photo courante.
        # Évite de relire le fichier complet à chaque preview de slider.
        self._base_cache: "tuple[bytes, int, int] | None" = None
        # Thread de chargement de l'image de base (images et vidéos).
        # Le chargement est asynchrone pour éviter de bloquer le thread UI,
        # particulièrement critique pour les vidéos (cv2.VideoCapture + COM).
        self._base_loader: "_BaseLoader | None" = None
        # Album affiché lorsque la visionneuse a été ouverte depuis sa grille
        # (ou None) : détermine si le menu contextuel propose "Retirer de
        # l'album" et si la touche Del retire de l'album plutôt que d'effacer
        # le fichier — cf. ThumbnailGrid.set_album_context.
        self._album_id: int | None = None
        # Debounce pour les previews de retouche : on ne recharge que 60 ms
        # après le dernier événement slider (évite les surcharges mémoire).
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(60)
        self._preview_timer.timeout.connect(self._reload_pixmap)
        self._setup_ui()
        self.refresh_external_apps()
        self.setFocusPolicy(Qt.StrongFocus)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- Toolbar permanente ----
        self._toolbar = QWidget()
        self._toolbar.setStyleSheet("background: rgba(0,0,0,200);")
        tb_layout = QHBoxLayout(self._toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4)

        self._btn_back = QPushButton("←")
        self._btn_back.setToolTip("Retour à la grille  (Echap)")
        self._btn_back.setFixedWidth(32)
        self._btn_back.clicked.connect(self.closed.emit)
        tb_layout.addWidget(self._btn_back)

        self._lbl_name = QLabel("")
        self._lbl_name.setStyleSheet("color: #ccc;")
        self._lbl_name.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self._lbl_name.setCursor(Qt.IBeamCursor)
        tb_layout.addWidget(self._lbl_name, stretch=1)

        self._btn_fav = QPushButton("♡")
        self._btn_fav.setToolTip("Marquer comme favori  (F)")
        self._btn_fav.setFixedWidth(32)
        self._btn_fav.setCheckable(True)
        self._btn_fav.clicked.connect(self._toggle_favorite)
        tb_layout.addWidget(self._btn_fav)

        # Conteneur des boutons d'applications externes (reconstruit par refresh_external_apps)
        self._ext_apps_container = QWidget()
        self._ext_apps_container.setStyleSheet("background: transparent;")
        self._ext_apps_layout = QHBoxLayout(self._ext_apps_container)
        self._ext_apps_layout.setContentsMargins(4, 0, 4, 0)
        self._ext_apps_layout.setSpacing(4)
        tb_layout.addWidget(self._ext_apps_container)

        self._btn_fit = QPushButton("⊡")
        self._btn_fit.setToolTip("Ajuster à la fenêtre  (0)")
        self._btn_fit.setFixedWidth(32)
        self._btn_fit.clicked.connect(self.zoom_fit)
        tb_layout.addWidget(self._btn_fit)

        self._btn_100 = QPushButton("1:1")
        self._btn_100.setToolTip("Zoom 100%  (1)")
        self._btn_100.setFixedWidth(36)
        self._btn_100.clicked.connect(self.zoom_100)
        tb_layout.addWidget(self._btn_100)

        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip("Fermer  (Echap)")
        self._btn_close.setFixedWidth(32)
        self._btn_close.clicked.connect(self.closed.emit)
        tb_layout.addWidget(self._btn_close)

        layout.addWidget(self._toolbar)

        # ---- Canvas ----
        self._canvas = _Canvas()
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._canvas.zoom_changed.connect(self.zoom_changed)
        self._canvas.wheel_navigate.connect(self.navigate)
        self._canvas.crop_confirmed.connect(self._on_crop_confirmed)
        self._canvas.context_menu_requested.connect(self._show_context_menu)
        self._canvas.red_eye_point_added.connect(self.red_eye_point_added)
        self._canvas.pixel_sampled.connect(self.pixel_sampled)
        self._canvas.face_context_menu_requested.connect(self.face_context_menu_requested)
        self._canvas.vignette_changed.connect(self.vignette_changed)
        self._canvas.face_add_confirmed.connect(self._on_face_add_confirmed)
        self._canvas.annotation_added.connect(self.annotation_added)
        self._canvas.annotation_deleted.connect(self.annotation_deleted)
        self._canvas.annotation_deleted_multi.connect(self.annotation_deleted_multi)
        self._canvas.annotation_selection_changed.connect(self.annotation_selection_changed)
        self._canvas.annotation_moved.connect(self.annotation_moved)
        self._canvas.annotation_moved_multi.connect(self.annotation_moved_multi)
        self._canvas.annotation_resized.connect(self.annotation_resized)
        self._canvas.annotation_grouped.connect(self.annotation_grouped)
        layout.addWidget(self._canvas, stretch=1)

        # ---- Pied de page ----
        self._navbar = QWidget()
        self._navbar.setStyleSheet("background: rgba(0,0,0,200);")
        self._navbar.setFixedHeight(52)
        nav_layout = QHBoxLayout(self._navbar)
        nav_layout.setContentsMargins(16, 6, 16, 6)

        self._btn_prev = QPushButton("◀  Plus ancienne")
        self._btn_prev.setFixedHeight(36)
        self._btn_prev.clicked.connect(lambda: self.navigate.emit(1))
        nav_layout.addWidget(self._btn_prev)

        nav_layout.addStretch()

        # Boutons de format de recadrage (masqués hors mode crop)
        self._btn_play_video = QPushButton("▶  Ouvrir la vidéo")
        self._btn_play_video.setToolTip("Ouvrir dans le lecteur vidéo par défaut")
        self._btn_play_video.setFixedHeight(36)
        self._btn_play_video.setStyleSheet(
            "QPushButton { background:#2a6a2a; color:white; border:none;"
            " border-radius:3px; padding:4px 16px; font-size: 13px; }"
            "QPushButton:hover { background:#3a7a3a; }"
        )
        self._btn_play_video.clicked.connect(self._open_in_player)
        self._btn_play_video.hide()
        nav_layout.addWidget(self._btn_play_video)

        self._crop_format_widget = QWidget()
        self._crop_format_widget.setStyleSheet("background: transparent;")
        fmt_layout = QHBoxLayout(self._crop_format_widget)
        fmt_layout.setContentsMargins(0, 0, 8, 0)
        fmt_layout.setSpacing(4)
        self._crop_format_group = QButtonGroup(self)
        self._crop_format_group.setExclusive(True)
        self._crop_format_btns: list[QToolButton] = []
        for i, (label, tip, ratio) in enumerate(_CROP_FORMAT_DATA):
            btn = QToolButton()
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIcon(QIcon(_fmt_icon(ratio)))
            btn.setIconSize(QSize(24, 18))
            btn.setText(label)
            btn.setToolTip(tip)
            btn.setCheckable(True)
            btn.setFixedSize(54, 40)
            btn.setStyleSheet(_BTN_CROP_STYLE)
            fmt_layout.addWidget(btn)
            self._crop_format_group.addButton(btn, i)
            self._crop_format_btns.append(btn)
        self._crop_format_btns[0].setChecked(True)  # "Libre" par défaut
        self._crop_format_group.idClicked.connect(self._on_crop_format_changed)
        self._crop_format_widget.hide()
        nav_layout.addWidget(self._crop_format_widget)

        self._btn_crop_confirm = QPushButton("✓  Confirmer le recadrage")
        self._btn_crop_confirm.setToolTip("Valider le recadrage  (Entrée)")
        self._btn_crop_confirm.setFixedHeight(36)
        self._btn_crop_confirm.setStyleSheet("background: #2a6a2a; color: white;")
        self._btn_crop_confirm.clicked.connect(self.confirm_crop)
        self._btn_crop_confirm.hide()
        nav_layout.addWidget(self._btn_crop_confirm)

        self._btn_crop_cancel = QPushButton("✕  Annuler")
        self._btn_crop_cancel.setToolTip("Annuler le recadrage  (Echap)")
        self._btn_crop_cancel.setFixedHeight(36)
        self._btn_crop_cancel.setStyleSheet("background: #6a2a2a; color: white;")
        self._btn_crop_cancel.clicked.connect(self.cancel_crop)
        self._btn_crop_cancel.hide()
        nav_layout.addWidget(self._btn_crop_cancel)

        self._btn_face_confirm = QPushButton("✓  Valider la position")
        self._btn_face_confirm.setToolTip("Valider la position du visage  (Entrée)")
        self._btn_face_confirm.setFixedHeight(36)
        self._btn_face_confirm.setStyleSheet("background: #2a6a2a; color: white;")
        self._btn_face_confirm.clicked.connect(self.confirm_face_add)
        self._btn_face_confirm.hide()
        nav_layout.addWidget(self._btn_face_confirm)

        self._btn_face_cancel = QPushButton("✕  Annuler")
        self._btn_face_cancel.setToolTip("Annuler l'ajout du visage  (Echap)")
        self._btn_face_cancel.setFixedHeight(36)
        self._btn_face_cancel.setStyleSheet("background: #6a2a2a; color: white;")
        self._btn_face_cancel.clicked.connect(self.cancel_face_add_mode)
        self._btn_face_cancel.hide()
        nav_layout.addWidget(self._btn_face_cancel)

        nav_layout.addStretch()

        self._btn_next = QPushButton("Plus récente  ▶")
        self._btn_next.setFixedHeight(36)
        self._btn_next.clicked.connect(lambda: self.navigate.emit(-1))
        nav_layout.addWidget(self._btn_next)

        layout.addWidget(self._navbar)

        # ---- Badge doublons (flottant sur le canvas) ----
        self._dup_badge = QPushButton("⧉ Doublons", self)
        self._dup_badge.setToolTip("Cette photo a des doublons — cliquer pour voir")
        self._dup_badge.setStyleSheet(
            "QPushButton{"
            "  background:rgba(255,140,0,210);color:white;border:none;"
            "  border-radius:5px;padding:3px 10px;"
            "  font-size:11px;font-weight:bold"
            "}"
            "QPushButton:hover{background:rgba(255,160,40,240)}"
        )
        self._dup_badge.setCursor(Qt.PointingHandCursor)
        self._dup_badge.setFixedHeight(24)
        self._dup_badge.hide()
        self._dup_badge.clicked.connect(
            lambda: self.dup_badge_clicked.emit(self._photo)
        )

    # ------------------------------------------------------------------ photo

    def current_photo(self) -> "PhotoInfo | None":
        return self._photo

    def set_album_context(self, album_id: int | None) -> None:
        """Indique si la photo affichée provient d'un album (et lequel), pour
        proposer "Retirer de l'album" et faire pointer la touche Del dessus
        plutôt que sur l'effacement définitif du fichier."""
        self._album_id = album_id

    def set_photo(self, photo: PhotoInfo, edit: EditInfo | None = None) -> None:
        self._preview_timer.stop()
        # Annuler un chargement de base image en cours (navigation rapide)
        if self._base_loader and self._base_loader.isRunning():
            self._base_loader.base_ready.disconnect()
            self._base_loader.quit()
            self._base_loader = None
        self._base_cache = None  # invalide le cache pour la nouvelle photo
        self._photo = photo
        is_video = photo.media_type == "video"
        self._edit = None if is_video else (edit or self._db.load(photo.path))
        self._lbl_name.setText(photo.path)
        self._btn_fav.setChecked(photo.is_favorite)
        self._btn_play_video.setVisible(is_video)
        self._canvas.set_highlighted_face(None)
        self._update_dup_badge()
        self._reload_pixmap()

    def _update_dup_badge(self) -> None:
        if self._photo and self._photo.duplicate_group_id is not None:
            self._dup_badge.show()
            self._dup_badge.raise_()
            self._reposition_dup_badge()
            # La géométrie du viewer peut ne pas être définitive à cet instant
            # (ex. bascule depuis la grille : le stack/splitter ne se redimensionne
            # qu'après cet appel) — un repositionnement différé rattrape le calcul
            # une fois la géométrie finale connue, pour éviter le badge mal placé.
            QTimer.singleShot(0, self._reposition_dup_badge)
        else:
            self._dup_badge.hide()

    def _reposition_dup_badge(self) -> None:
        if not self._dup_badge.isVisible():
            return
        self._dup_badge.adjustSize()
        margin = 12
        toolbar_h = self._toolbar.height()
        x = self.width() - self._dup_badge.width() - margin
        y = toolbar_h + margin
        self._dup_badge.move(x, y)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._dup_badge.isVisible():
            self._reposition_dup_badge()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dup_badge.isVisible():
            QTimer.singleShot(0, self._reposition_dup_badge)

    def highlight_face(self, face) -> None:
        """Encadre un visage unique sur la photo (appelé depuis main_window)."""
        self._canvas.set_highlighted_face(face)

    def set_all_highlighted_faces(self, faces: list) -> None:
        """Encadre tous les visages (mode 'Tous' du FacePanel)."""
        self._canvas.set_highlighted_faces(faces)

    def _reload_pixmap(self) -> None:
        if not self._photo:
            return
        self._canvas.set_edit(self._edit)
        self._canvas.set_annotations(self._edit.annotations if self._edit else [])

        if self._base_cache is not None:
            # Cache chaud : appliquer les retouches et afficher immédiatement
            base_bytes, orig_w, orig_h = self._base_cache
            pixmap = _apply_edit_to_base(base_bytes, self._edit)
            self._canvas.set_orig_size(orig_w, orig_h)
            self._canvas.set_pixmap(pixmap)
            return

        # Cache froid : lancer le chargement en arrière-plan (image ou vidéo)
        # Afficher un état vide pendant le chargement
        self._canvas.set_orig_size(0, 0)
        self._canvas.set_pixmap(None)
        if self._base_loader and self._base_loader.isRunning():
            return  # déjà en cours pour cette photo
        self._base_loader = _BaseLoader(self._photo, self)
        self._base_loader.base_ready.connect(self._on_base_ready)
        self._base_loader.start()

    @Slot(str, object)
    def _on_base_ready(self, path: str, result: object) -> None:
        """Reçoit le résultat du chargement de base image/vidéo depuis _BaseLoader."""
        if self._photo is None or self._photo.path != path:
            return  # navigation entre-temps — résultat obsolète
        self._base_cache = result
        if result is None:
            return
        base_bytes, orig_w, orig_h = result
        pixmap = _apply_edit_to_base(base_bytes, self._edit)
        self._canvas.set_edit(self._edit)
        self._canvas.set_orig_size(orig_w, orig_h)
        self._canvas.set_pixmap(pixmap)

    def refresh_name(self) -> None:
        if self._photo:
            self._lbl_name.setText(self._photo.path)

    def update_edit(self, edit: EditInfo) -> None:
        self._edit = edit
        # Les annotations sont des données vectorielles légères, rendues en
        # calque séparé — pas besoin d'attendre le debounce du pixmap raster.
        self._canvas.set_annotations(edit.annotations)
        # Debounce : reporte le rendu de 60 ms pour absorber les rafales de
        # sliders. Évite d'accumuler des images PIL de 72 Mo en mémoire.
        self._preview_timer.stop()
        self._preview_timer.start()

    def set_grid_visible(self, visible: bool) -> None:
        self._canvas.set_grid_visible(visible)

    # ------------------------------------------------------------------ zoom

    def zoom_fit(self) -> None:
        self._canvas.zoom_fit()

    def zoom_100(self) -> None:
        self._canvas.zoom_100()

    def set_zoom(self, factor: float) -> None:
        self._canvas.set_zoom(factor)

    @property
    def zoom(self) -> float:
        return self._canvas.zoom

    # ------------------------------------------------------------------ crop

    def enter_crop_mode(self) -> None:
        existing = self._edit.crop if self._edit else None
        # Si un crop est déjà appliqué, afficher l'image sans ce crop pour que
        # l'utilisateur puisse repositionner la zone sur l'image complète.
        if existing and self._photo and self._edit and self._base_cache:
            edit_no_crop = EditInfo.from_dict({**self._edit.to_dict(), 'crop': None})
            base_bytes, _, _ = self._base_cache
            pixmap = _apply_edit_to_base(base_bytes, edit_no_crop)
            if pixmap:
                self._canvas.set_pixmap(pixmap)
        self._canvas.enter_crop(existing)
        idx = self._crop_format_group.checkedId()
        self._canvas.set_aspect_ratio(_CROP_FORMAT_DATA[idx][2] if idx >= 0 else None)
        self._btn_prev.hide()
        self._btn_next.hide()
        self._crop_format_widget.show()
        self._btn_crop_confirm.show()
        self._btn_crop_cancel.show()

    def _on_crop_format_changed(self, idx: int) -> None:
        self._canvas.set_aspect_ratio(_CROP_FORMAT_DATA[idx][2])

    def confirm_crop(self) -> None:
        self._canvas.confirm_crop()    # émet crop_confirmed → _on_crop_confirmed

    def update_nav_arrows(self, has_prev: bool, has_next: bool) -> None:
        self._btn_prev.setVisible(has_prev)
        self._btn_next.setVisible(has_next)

    def cancel_crop(self) -> None:
        self._canvas.cancel_crop()
        self._crop_format_widget.hide()
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        # Restaurer l'image avec le crop appliqué (on avait affiché l'image sans crop)
        self._reload_pixmap()
        self.crop_mode_ended.emit()

    def _on_crop_confirmed(self, quad: tuple) -> None:
        self._crop_format_widget.hide()
        self._btn_crop_confirm.hide()
        self._btn_crop_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self.crop_ready.emit(quad)
        self.crop_mode_ended.emit()

    # ------------------------------------------------------------------ ajout manuel de visage

    def enter_face_add_mode(self) -> None:
        self._canvas.enter_face_add_mode()
        self._btn_prev.hide()
        self._btn_next.hide()
        self._btn_face_confirm.show()
        self._btn_face_cancel.show()

    def confirm_face_add(self) -> None:
        self._canvas.confirm_face_add()   # émet face_add_confirmed → _on_face_add_confirmed si valide
        self._btn_face_confirm.hide()
        self._btn_face_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self.face_add_mode_ended.emit()

    def cancel_face_add_mode(self) -> None:
        self._canvas.cancel_face_add_mode()
        self._btn_face_confirm.hide()
        self._btn_face_cancel.hide()
        self._btn_prev.show()
        self._btn_next.show()
        self.face_add_mode_ended.emit()

    def _on_face_add_confirmed(self, bbox: tuple) -> None:
        self.face_bbox_ready.emit(bbox)

    # ------------------------------------------------------------------ red-eye mode

    def enter_red_eye_mode(self, radius: float = 0.03) -> None:
        self._canvas.enter_red_eye_mode(radius)

    def exit_red_eye_mode(self) -> None:
        self._canvas.exit_red_eye_mode()

    def set_red_eye_radius(self, radius: float) -> None:
        self._canvas.set_red_eye_radius(radius)

    # ------------------------------------------------------------------ vignette interactive

    def enter_vignette_mode(self, edit) -> None:
        self._canvas.enter_vignette_mode(edit)

    def exit_vignette_mode(self) -> None:
        self._canvas.exit_vignette_mode()

    def update_vignette(self, edit) -> None:
        self._canvas.update_vignette(edit)

    # ------------------------------------------------------------------ pipette couleur (balance des blancs)

    def start_color_pick(self) -> None:
        """Active le mode pipette : prochain clic gauche sur l'image → pixel_sampled(r, g, b)."""
        self._canvas.start_color_pick()

    def stop_color_pick(self) -> None:
        self._canvas.stop_color_pick()

    # ------------------------------------------------------------------ annotations (dessin/texte)

    def enter_annotation_mode(self, tool: str = "pen") -> None:
        self._canvas.enter_annotation_mode(tool)

    def exit_annotation_mode(self) -> None:
        self._canvas.exit_annotation_mode()

    def set_annotation_tool(self, tool: str) -> None:
        self._canvas.set_annotation_tool(tool)

    def set_annotation_style(self, color: str, width: float, font_family: str,
                              font_size: float, bold: bool, italic: bool,
                              fill_color: str = "#40ff0000", opacity: float = 1.0,
                              blur: float = 0.0) -> None:
        self._canvas.set_annotation_style(color, width, font_family, font_size, bold, italic,
                                           fill_color, opacity, blur)

    def delete_selected_annotation(self) -> None:
        self._canvas.delete_selected_annotation()

    def clear_all_annotations(self) -> None:
        self._canvas.clear_all_annotations()

    def group_selected_annotations(self) -> None:
        self._canvas.group_selected_annotations()

    def ungroup_selected_annotations(self) -> None:
        self._canvas.ungroup_selected_annotations()

    def set_annotations_visible(self, visible: bool) -> None:
        self._canvas.set_annotations_visible(visible)

    # ------------------------------------------------------------------ misc

    def refresh_external_apps(self) -> None:
        """Reconstruit les boutons d'applications externes dans la toolbar depuis la config."""
        while self._ext_apps_layout.count():
            item = self._ext_apps_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._config:
            return

        _icon_provider = QFileIconProvider()
        for app in self._config.get("tools.external_apps", []):
            name = app.get("name", "")
            path = app.get("path", "")
            if not path or not os.path.isfile(path):
                continue
            btn = QToolButton()
            btn.setToolTip(f"Ouvrir avec {name}")
            # Nom accessible pour l'automatisation pywinauto (e2e) — même
            # convention que ThumbnailCell/_DuplicateCard : ce bouton n'a pas
            # de texte propre (icône seule), donc pas de window_text() unique.
            btn.setAccessibleName(f"extapp::{name}")
            btn.setFixedSize(32, 32)
            btn.setIcon(_icon_provider.icon(QFileInfo(path)))
            btn.setIconSize(QSize(22, 22))
            btn.setStyleSheet(
                "QToolButton { background: white; border: none; border-radius: 4px; }"
                "QToolButton:hover { background: #e0e8ff; }"
                "QToolButton:pressed { background: #c0d0ff; }"
            )
            btn.clicked.connect(lambda _checked=False, p=path: self._open_with(p))
            self._ext_apps_layout.addWidget(btn)

    def _open_with(self, app_path: str) -> None:
        if not self._photo:
            return
        import subprocess
        try:
            subprocess.Popen([app_path, self._photo.path])
        except Exception as exc:
            logger.warning("Impossible de lancer '%s' : %s", app_path, exc)

    def _open_in_player(self) -> None:
        if not self._photo:
            return
        player = self._config.get("video.player_path", "") if self._config else ""
        if player:
            import subprocess
            try:
                subprocess.Popen([player, self._photo.path])
                return
            except Exception as e:
                logger.warning("Lecteur vidéo introuvable (%s) : %s", player, e)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._photo.path))

    def _show_context_menu(self, pos) -> None:
        if not self._photo:
            return
        photo = self._photo
        menu = QMenu(self)

        fav_label = "Retirer des favoris" if photo.is_favorite else "Marquer comme favori"
        menu.addAction(fav_label, self._toggle_fav_from_menu)
        menu.addAction("Renommer…", lambda: self.rename_requested.emit(photo))
        menu.addAction("Déplacer vers…", lambda: self.move_requested.emit(photo))
        menu.addAction("Enregistrer l'image traitée sur le disque",
                       lambda: self.save_requested.emit(photo))
        menu.addSeparator()
        menu.addAction("Révéler dans l'Explorateur",
                       lambda: os.startfile(os.path.dirname(photo.path)))
        menu.addAction("Afficher le dossier dans la grille",
                       lambda: self.folder_grid_requested.emit(photo))
        menu.addSeparator()

        gps_coords = self._resolve_gps(photo)
        act_map = menu.addAction("Localiser sur la carte")
        act_map.setEnabled(gps_coords is not None)
        if gps_coords is not None:
            lat, lon = gps_coords
            act_map.triggered.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl(f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=15#map=15/{lat}/{lon}")
                )
            )

        menu.addSeparator()
        menu.addAction("Forcer une nouvelle détection sans limite de taille",
                       lambda: self.force_redetect_requested.emit(photo))
        menu.addSeparator()
        if self._album_id is not None:
            menu.addAction("Retirer de l'album",
                           lambda: self.remove_from_album_requested.emit([photo]))
        else:
            menu.addAction("Effacer le fichier…", lambda: self.delete_requested.emit([photo]))

        menu.exec(pos)

    def _resolve_gps(self, photo) -> "tuple[float, float] | None":
        """Return (lat, lon) from catalog cache or by reading EXIF directly."""
        if photo.has_gps and photo.gps_lat is not None and photo.gps_lon is not None:
            return photo.gps_lat, photo.gps_lon
        # Catalog field missing (indexed before GPS support or parse failure) — read live
        try:
            from PIL import Image
            from src.library.exif_reader import ExifReader
            with Image.open(photo.path) as img:
                gps_ifd = img.getexif().get_ifd(0x8825)
                if gps_ifd:
                    return ExifReader._parse_gps(gps_ifd)
        except Exception:
            pass
        return None

    def _toggle_fav_from_menu(self) -> None:
        if not self._photo:
            return
        new_state = not self._photo.is_favorite
        self._btn_fav.setChecked(new_state)
        self._toggle_favorite(new_state)

    def _toggle_favorite(self, checked: bool) -> None:
        if self._photo:
            self._photo.is_favorite = checked
            self._btn_fav.setText("★" if checked else "♡")
            self.favorite_toggle_requested.emit(self._photo)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            if self._canvas._crop_mode:
                self.cancel_crop()
            elif self._canvas._face_add_mode:
                self.cancel_face_add_mode()
            elif self._canvas._red_eye_mode:
                self.exit_red_eye_mode()
            elif self._canvas._annotation_mode:
                self._canvas.cancel_annotation_draft()
            else:
                self.closed.emit()
        elif key == Qt.Key_Return or key == Qt.Key_Enter:
            if self._canvas._crop_mode:
                self.confirm_crop()
            elif self._canvas._face_add_mode:
                self.confirm_face_add()
            elif self._canvas._annotation_mode and self._canvas._annotation_tool == "curve":
                self._canvas.confirm_annotation_draft()
        elif key in (Qt.Key_Right, Qt.Key_Up):
            if not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                    and not self._canvas._face_add_mode and not self._canvas._annotation_mode:
                self.navigate.emit(-1)   # plus récente (droite/haut = vers le haut de la liste)
        elif key in (Qt.Key_Left, Qt.Key_Down):
            if not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                    and not self._canvas._face_add_mode and not self._canvas._annotation_mode:
                self.navigate.emit(1)    # plus ancienne (gauche/bas = vers le bas de la liste)
        elif key == Qt.Key_Delete:
            if self._canvas._annotation_mode:
                self.delete_selected_annotation()
            else:
                photo = self.current_photo()
                if photo and not self._canvas._crop_mode and not self._canvas._red_eye_mode \
                        and not self._canvas._face_add_mode:
                    if self._album_id is not None:
                        self.remove_from_album_requested.emit([photo])
                    else:
                        self.delete_requested.emit([photo])
        elif key == Qt.Key_0:
            self.zoom_fit()
        elif key == Qt.Key_1:
            self.zoom_100()
        else:
            super().keyPressEvent(event)
