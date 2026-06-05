"""
LoadingLabel — QLabel avec animation spinner pendant le chargement.

Toutes les instances actives partagent un seul QTimer de classe (~10 fps)
pour ne pas surcharger le CPU quand des dizaines de vignettes chargent
en parallèle.
"""

import math

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QLabel


class LoadingLabel(QLabel):
    """
    QLabel qui affiche 8 points tournants pendant le chargement,
    puis l'image dès que setPixmap() est appelé.

    Usage :
        lbl = LoadingLabel("#1a1a1a")
        lbl.setFixedSize(130, 130)
        lbl.start_loading()          # démarre le spinner
        ...
        lbl.setPixmap(pixmap)        # arrête le spinner, affiche l'image
    """

    _timer: "QTimer | None" = None
    _active: "list[LoadingLabel]" = []
    _frame: int = 0
    _N = 8       # nombre de points

    def __init__(self, bg_color: str = "#1a1a1a", parent=None) -> None:
        super().__init__(parent)
        self._loading = False
        self._bg = QColor(bg_color)

    # ------------------------------------------------------------------ timer partagé

    @classmethod
    def _ensure_timer(cls) -> None:
        if cls._timer is None:
            cls._timer = QTimer()
            cls._timer.setInterval(100)   # 10 fps — suffisant pour un spinner
            cls._timer.timeout.connect(cls._tick)

    @classmethod
    def _tick(cls) -> None:
        cls._frame = (cls._frame + 1) % cls._N
        alive = []
        for lbl in cls._active:
            try:
                lbl.update()
                alive.append(lbl)
            except RuntimeError:
                pass   # objet C++ déjà détruit, on le retire silencieusement
        cls._active = alive
        if not cls._active:
            cls._timer.stop()

    # ------------------------------------------------------------------ API publique

    def start_loading(self) -> None:
        """Affiche le spinner. Sans effet si déjà en cours de chargement."""
        if self._loading:
            return
        self._loading = True
        LoadingLabel._ensure_timer()
        LoadingLabel._active.append(self)
        LoadingLabel._timer.start()
        self.update()

    def setPixmap(self, pix: QPixmap) -> None:  # noqa: N802
        """Arrête le spinner et affiche le pixmap."""
        super().setPixmap(pix)
        self._stop()

    # ------------------------------------------------------------------ interne

    def _stop(self) -> None:
        if not self._loading:
            return
        self._loading = False
        try:
            LoadingLabel._active.remove(self)
        except ValueError:
            pass
        if not LoadingLabel._active and LoadingLabel._timer:
            LoadingLabel._timer.stop()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._stop()

    # ------------------------------------------------------------------ dessin

    def paintEvent(self, event) -> None:
        if not self._loading:
            super().paintEvent(event)
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self._bg)

        cx, cy = self.width() // 2, self.height() // 2
        r  = max(6, min(self.width(), self.height()) // 6)   # rayon de la ronde
        dr = max(2, r // 4)                                   # rayon de chaque point

        for i in range(self._N):
            angle = 2 * math.pi * i / self._N - math.pi / 2
            age   = (self._N - 1 - (i - self._frame) % self._N)
            alpha = 40 + int(215 * age / (self._N - 1))
            x = cx + int(r * math.cos(angle))
            y = cy + int(r * math.sin(angle))
            p.setBrush(QColor(190, 190, 190, alpha))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPoint(x, y), dr, dr)

        p.end()
