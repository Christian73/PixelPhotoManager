# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
LoadingLabel — a QLabel with a spinner animation during loading.

Every active instance shares a single class QTimer (~10 fps) so as not to
overload the CPU when dozens of thumbnails load in parallel.
"""

import math

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QLabel


class LoadingLabel(QLabel):
    """
    A QLabel showing 8 rotating dots during loading, then the image as soon
    as setPixmap() is called.

    Usage:
        lbl = LoadingLabel("#1a1a1a")
        lbl.setFixedSize(130, 130)
        lbl.start_loading()          # starts the spinner
        ...
        lbl.setPixmap(pixmap)        # stops the spinner, shows the image
    """

    _timer: "QTimer | None" = None
    _active: "list[LoadingLabel]" = []
    _frame: int = 0
    _N = 8       # nombre de points

    def __init__(self, bg_color: str = "#1a1a1a", parent=None) -> None:
        super().__init__(parent)
        self._loading = False
        self._bg = QColor(bg_color)

    # ------------------------------------------------------------------ shared timer

    @classmethod
    def _ensure_timer(cls) -> None:
        if cls._timer is None:
            cls._timer = QTimer()
            cls._timer.setInterval(100)   # 10 fps — enough for a spinner
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
                pass   # C++ object already destroyed, remove it silently
        cls._active = alive
        if not cls._active:
            cls._timer.stop()

    # ------------------------------------------------------------------ API publique

    def start_loading(self) -> None:
        """Shows the spinner. No effect if already loading."""
        if self._loading:
            return
        self._loading = True
        LoadingLabel._ensure_timer()
        LoadingLabel._active.append(self)
        LoadingLabel._timer.start()
        self.update()

    def setPixmap(self, pix: QPixmap) -> None:  # noqa: N802
        """Stops the spinner and shows the pixmap."""
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

    # ------------------------------------------------------------------ drawing

    def paintEvent(self, event) -> None:
        if not self._loading:
            super().paintEvent(event)
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self._bg)

        cx, cy = self.width() // 2, self.height() // 2
        r  = max(6, min(self.width(), self.height()) // 6)   # radius of the ring
        dr = max(2, r // 4)                                   # radius of each dot

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
