# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) pour SettingsDialog — pages Reconnaissance de visages et
Lecteur vidéo. Config est un singleton de classe : chaque test le réinitialise
et redirige _CONFIG_FILE vers tmp_path (même convention que test_config.py)."""
import pytest
from PySide6.QtWidgets import QFileDialog

import src.core.config as config_module
from src.core.config import Config
from src.ui.settings_dialog import (
    SettingsDialog, _FaceRecognitionPage, _VideoPlayerPage,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    Config._instance = None
    monkeypatch.setattr(config_module, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_module, "APP_DATA_DIR", tmp_path)
    yield Config()
    Config._instance = None


class TestFaceRecognitionPage:
    def test_default_threshold_60(self, qtbot, config):
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        assert page._slider.value() == 60
        assert page._lbl_value.text().startswith("0.60")
        assert "très larges" in page._lbl_value.text()

    def test_saved_threshold_restored_and_clamped(self, qtbot, config):
        config.set("faces.cluster_threshold", 0.9)   # hors plage → clampé à 70
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        assert page._slider.value() == 70

    @pytest.mark.parametrize("value, hint", [
        (28, "très stricts"),
        (38, "équilibrés"),
        (50, "plus larges"),
        (65, "très larges"),
    ])
    def test_value_label_hints(self, qtbot, config, value, hint):
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        page._slider.setValue(value)

        assert hint in page._lbl_value.text()

    def test_apply_returns_changed_flag_and_persists(self, qtbot, config):
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        assert page.apply() is False           # valeur inchangée

        page._slider.setValue(45)
        assert page.apply() is True
        assert config.get("faces.cluster_threshold") == pytest.approx(0.45)
        assert page.apply() is False           # plus de changement après apply


class TestVideoPlayerPage:
    def test_no_saved_path_defaults_to_system_player(self, qtbot, config):
        page = _VideoPlayerPage(config)
        qtbot.addWidget(page)

        assert page._rb_default.isChecked()
        assert not page._edit_path.isEnabled()

    def test_saved_path_selects_custom(self, qtbot, config):
        config.set("video.player_path", "C:/vlc/vlc.exe")
        page = _VideoPlayerPage(config)
        qtbot.addWidget(page)

        assert page._rb_custom.isChecked()
        assert page._edit_path.isEnabled()
        assert page._edit_path.text() == "C:/vlc/vlc.exe"

    def test_typing_path_checks_custom_radio(self, qtbot, config):
        page = _VideoPlayerPage(config)
        qtbot.addWidget(page)
        assert page._rb_default.isChecked()

        page._edit_path.setText("C:/mpc/mpc.exe")

        assert page._rb_custom.isChecked()

    def test_browse_sets_path(self, qtbot, config, monkeypatch):
        page = _VideoPlayerPage(config)
        qtbot.addWidget(page)
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: ("C:/vlc/vlc.exe", "Exécutables (*.exe)")),
        )

        page._browse()

        assert page._rb_custom.isChecked()
        assert page._edit_path.text() == "C:/vlc/vlc.exe"

    def test_browse_cancelled_keeps_state(self, qtbot, config, monkeypatch):
        page = _VideoPlayerPage(config)
        qtbot.addWidget(page)
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: ("", "")),
        )

        page._browse()

        assert page._rb_default.isChecked()

    def test_apply_custom_and_default(self, qtbot, config):
        page = _VideoPlayerPage(config)
        qtbot.addWidget(page)

        page._edit_path.setText("  C:/vlc/vlc.exe  ")
        page.apply()
        assert config.get("video.player_path") == "C:/vlc/vlc.exe"

        page._rb_default.setChecked(True)
        page.apply()
        assert config.get("video.player_path") == ""


class TestSettingsDialog:
    def test_category_list_switches_pages(self, qtbot, config):
        dlg = SettingsDialog(config)
        qtbot.addWidget(dlg)
        assert dlg._stack.currentWidget() is dlg._page_faces

        dlg._category_list.setCurrentRow(1)

        assert dlg._stack.currentWidget() is dlg._page_video

    def test_accept_without_change_does_not_emit_recluster(self, qtbot, config):
        dlg = SettingsDialog(config)
        qtbot.addWidget(dlg)
        fired = []
        dlg.recluster_needed.connect(lambda: fired.append(1))

        dlg._on_accept()

        assert fired == []
        assert dlg.result() == 1

    def test_accept_with_threshold_change_emits_recluster(self, qtbot, config):
        dlg = SettingsDialog(config)
        qtbot.addWidget(dlg)
        dlg._page_faces._slider.setValue(35)
        dlg._page_video._edit_path.setText("C:/vlc/vlc.exe")

        with qtbot.waitSignal(dlg.recluster_needed, timeout=1000):
            dlg._on_accept()

        assert config.get("faces.cluster_threshold") == pytest.approx(0.35)
        assert config.get("video.player_path") == "C:/vlc/vlc.exe"
