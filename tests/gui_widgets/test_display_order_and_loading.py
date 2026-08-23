# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/ui/display_order_dialog.py` (independent sorting sections,
saving to the config) and `src/ui/loading_label.py` (shared spinner,
start/stop cycle, painting)."""
import pytest
from PySide6.QtGui import QPixmap

from src.core.config import Config
from src.ui.display_order_dialog import DisplayOrderDialog, _ChronoAlbumSection, _OrderSection
from src.ui.loading_label import LoadingLabel


@pytest.fixture
def config():
    cfg = Config()
    saved = {k: cfg.get(f"display_order.{k}") for k in
             ("folder_mode", "folder_dir", "grid_mode", "grid_dir", "chrono_album_dir")}
    yield cfg
    for k, v in saved.items():
        if v is not None:
            cfg.set(f"display_order.{k}", v)


# ------------------------------------------------------------------ sections


class TestOrderSection:
    def test_defaults_applied(self, qtbot):
        s = _OrderSection("Test", "chrono", "desc")
        qtbot.addWidget(s)
        assert s.mode() == "chrono"
        assert s.direction() == "desc"

    def test_mode_and_direction_are_independent(self, qtbot):
        """Two distinct QButtonGroups: ticking a direction must not untick the
        mode (the trap of automatic grouping by parent)."""
        s = _OrderSection("Test", "alpha", "asc")
        qtbot.addWidget(s)
        s._rb_desc.setChecked(True)
        assert s.mode() == "alpha"        # unchanged
        assert s.direction() == "desc"
        s._rb_chrono.setChecked(True)
        assert s.direction() == "desc"    # unchanged

    def test_chrono_album_section(self, qtbot):
        s = _ChronoAlbumSection("asc")
        qtbot.addWidget(s)
        assert s.direction() == "asc"
        s._rb_desc.setChecked(True)
        assert s.direction() == "desc"


class TestDisplayOrderDialog:
    def test_reads_config_defaults(self, qtbot, config):
        config.set("display_order.folder_mode", "chrono")
        config.set("display_order.folder_dir", "desc")
        config.set("display_order.grid_mode", "alpha")
        config.set("display_order.grid_dir", "asc")
        config.set("display_order.chrono_album_dir", "asc")
        dlg = DisplayOrderDialog(config)
        qtbot.addWidget(dlg)
        assert dlg._folders.mode() == "chrono"
        assert dlg._folders.direction() == "desc"
        assert dlg._grid.mode() == "alpha"
        assert dlg._chrono_album.direction() == "asc"

    def test_save_to_config(self, qtbot, config):
        dlg = DisplayOrderDialog(config)
        qtbot.addWidget(dlg)
        dlg._folders._rb_chrono.setChecked(True)
        dlg._folders._rb_desc.setChecked(True)
        dlg._grid._rb_alpha.setChecked(True)
        dlg._chrono_album._rb_asc.setChecked(True)
        dlg.save_to_config()
        assert config.get("display_order.folder_mode") == "chrono"
        assert config.get("display_order.folder_dir") == "desc"
        assert config.get("display_order.grid_mode") == "alpha"
        assert config.get("display_order.chrono_album_dir") == "asc"


# ------------------------------------------------------------------ LoadingLabel


class TestLoadingLabel:
    def test_start_loading_registers_and_starts_timer(self, qtbot):
        lbl = LoadingLabel()
        qtbot.addWidget(lbl)
        lbl.start_loading()
        assert lbl._loading is True
        assert lbl in LoadingLabel._active
        assert LoadingLabel._timer.isActive()
        lbl._stop()
        assert lbl not in LoadingLabel._active

    def test_start_twice_is_noop(self, qtbot):
        lbl = LoadingLabel()
        qtbot.addWidget(lbl)
        lbl.start_loading()
        lbl.start_loading()
        assert LoadingLabel._active.count(lbl) == 1
        lbl._stop()

    def test_set_pixmap_stops_spinner(self, qtbot):
        lbl = LoadingLabel()
        qtbot.addWidget(lbl)
        lbl.start_loading()
        pix = QPixmap(10, 10)
        lbl.setPixmap(pix)
        assert lbl._loading is False
        assert lbl not in LoadingLabel._active
        assert lbl.pixmap() is not None

    def test_timer_stops_when_no_active(self, qtbot):
        l1 = LoadingLabel()
        l2 = LoadingLabel()
        qtbot.addWidget(l1)
        qtbot.addWidget(l2)
        l1.start_loading()
        l2.start_loading()
        l1._stop()
        assert LoadingLabel._timer.isActive()   # l2 still active
        l2._stop()
        assert not LoadingLabel._timer.isActive()

    def test_tick_advances_frame(self, qtbot):
        lbl = LoadingLabel()
        qtbot.addWidget(lbl)
        lbl.start_loading()
        f0 = LoadingLabel._frame
        LoadingLabel._tick()
        assert LoadingLabel._frame == (f0 + 1) % LoadingLabel._N
        lbl._stop()

    def test_paint_spinner(self, qtbot):
        lbl = LoadingLabel("#000000")
        qtbot.addWidget(lbl)
        lbl.setFixedSize(60, 60)
        lbl.start_loading()
        img = lbl.grab().toImage()   # triggers paintEvent in spinner mode
        # at least one light grey pixel (one of the 8 dots)
        found = any(
            img.pixelColor(x, y).red() > 100
            for x in range(0, 60, 2) for y in range(0, 60, 2)
        )
        assert found
        lbl._stop()

    def test_hide_event_stops(self, qtbot):
        lbl = LoadingLabel()
        qtbot.addWidget(lbl)
        lbl.show()
        lbl.start_loading()
        lbl.hide()
        assert lbl._loading is False
