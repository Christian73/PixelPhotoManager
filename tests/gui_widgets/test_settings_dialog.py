# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) for SettingsDialog -- the Face recognition, Video player
and Performance pages. Config is a class singleton: every test resets it and
redirects _CONFIG_FILE to tmp_path (same convention as test_config.py)."""
import pytest
from PySide6.QtWidgets import QFileDialog

import src.core.config as config_module
from src.core import cpu_throttle
from src.core.config import Config
from src.ui.settings_dialog import (
    SettingsDialog, _FaceRecognitionPage, _PerformancePage, _VideoPlayerPage,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    Config._instance = None
    monkeypatch.setattr(config_module, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_module, "APP_DATA_DIR", tmp_path)
    yield Config()
    Config._instance = None


@pytest.fixture(autouse=True)
def _restore_cpu_ratio():
    """_PerformancePage.apply() writes the `_ratio` global of cpu_throttle
    (applied immediately to the threads already started) -- restore it so as not
    to contaminate the other tests of the suite."""
    saved = cpu_throttle._ratio
    yield
    cpu_throttle._ratio = saved


class TestFaceRecognitionPage:
    def test_default_threshold_60(self, qtbot, config):
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        assert page._slider.value() == 60
        assert page._lbl_value.text().startswith("0.60")
        assert "very broad" in page._lbl_value.text()

    def test_saved_threshold_restored_and_clamped(self, qtbot, config):
        config.set("faces.cluster_threshold", 0.9)   # out of range -> clamped to 70
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        assert page._slider.value() == 70

    @pytest.mark.parametrize("value, hint", [
        (28, "very strict"),
        (38, "balanced"),
        (50, "broader"),
        (65, "very broad"),
    ])
    def test_value_label_hints(self, qtbot, config, value, hint):
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        page._slider.setValue(value)

        assert hint in page._lbl_value.text()

    def test_apply_returns_changed_flag_and_persists(self, qtbot, config):
        page = _FaceRecognitionPage(config)
        qtbot.addWidget(page)

        assert page.apply() is False           # value unchanged

        page._slider.setValue(45)
        assert page.apply() is True
        assert config.get("faces.cluster_threshold") == pytest.approx(0.45)
        assert page.apply() is False           # no more change after apply


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


class TestPerformancePage:
    def test_default_level_is_the_economical_one(self, qtbot, config):
        """Without an explicit setting, the application favours responsiveness:
        the background analyses are permanent and have no deadline, whereas a
        sluggish interface is noticed straight away."""
        page = _PerformancePage(config)
        qtbot.addWidget(page)

        assert cpu_throttle.DEFAULT_BACKGROUND_CPU == "low"
        assert page.selected_level() == "low"

    def test_recommended_label_marks_the_default_choice(self, qtbot, config):
        """The "(recommended)" label must designate the level actually applied by
        default -- otherwise the settings contradict the behaviour."""
        page = _PerformancePage(config)
        qtbot.addWidget(page)

        recommended = [key for key, label, _ in page._CHOICES if "recommandé" in label]
        assert recommended == [cpu_throttle.DEFAULT_BACKGROUND_CPU]

    def test_saved_level_restored(self, qtbot, config):
        config.set("performance.background_cpu", "max")
        page = _PerformancePage(config)
        qtbot.addWidget(page)

        assert page.selected_level() == "max"

    def test_unknown_saved_level_falls_back_to_default(self, qtbot, config):
        """A value written by hand into config.json, or a key from a later
        version: must neither crash nor leave the page without a selection."""
        config.set("performance.background_cpu", "turbo")
        page = _PerformancePage(config)
        qtbot.addWidget(page)

        assert page.selected_level() == cpu_throttle.DEFAULT_BACKGROUND_CPU
        assert page._grp.checkedButton() is not None

    def test_every_level_has_a_radio(self, qtbot, config):
        page = _PerformancePage(config)
        qtbot.addWidget(page)

        keys = {key for key, _, _ in page._CHOICES}
        assert keys == set(cpu_throttle.BACKGROUND_CPU_LEVELS)

    def test_apply_persists_and_takes_effect_immediately(self, qtbot, config):
        """The background threads re-read the ratio on every throttle_tick(): the
        change must apply without restarting them."""
        page = _PerformancePage(config)
        qtbot.addWidget(page)
        page._grp.button(1).setChecked(True)      # "medium" (!= default)

        page.apply()

        assert config.get("performance.background_cpu") == "medium"
        assert cpu_throttle.background_cpu_ratio() == pytest.approx(0.60)

    def test_apply_max_level_disables_throttling(self, qtbot, config):
        page = _PerformancePage(config)
        qtbot.addWidget(page)
        page._grp.button(2).setChecked(True)      # "max"

        page.apply()

        assert config.get("performance.background_cpu") == "max"
        assert cpu_throttle.background_cpu_ratio() == pytest.approx(1.0)

    def test_no_selection_falls_back_to_default(self, qtbot, config):
        page = _PerformancePage(config)
        qtbot.addWidget(page)
        page._grp.setExclusive(False)
        page._grp.checkedButton().setChecked(False)

        assert page.selected_level() == cpu_throttle.DEFAULT_BACKGROUND_CPU


class TestSettingsDialog:
    def test_category_list_switches_pages(self, qtbot, config):
        dlg = SettingsDialog(config)
        qtbot.addWidget(dlg)
        assert dlg._stack.currentWidget() is dlg._page_faces

        dlg._category_list.setCurrentRow(1)
        assert dlg._stack.currentWidget() is dlg._page_video

        dlg._category_list.setCurrentRow(2)
        assert dlg._stack.currentWidget() is dlg._page_perf

    def test_accept_applies_performance_level(self, qtbot, config):
        dlg = SettingsDialog(config)
        qtbot.addWidget(dlg)
        dlg._page_perf._grp.button(2).setChecked(True)    # "max" (!= default)

        dlg._on_accept()

        assert config.get("performance.background_cpu") == "max"
        assert cpu_throttle.background_cpu_ratio() == pytest.approx(1.0)

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
