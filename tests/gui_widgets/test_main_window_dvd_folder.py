# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Regression/coverage: detecting a "DVD copy" folder (VIDEO_TS)
with no catalogued photo, and opening its content in an already configured
external application (MainWindow._open_dvd_folder/_external_apps_menu/
_launch_external_app, main_window.py).

Like test_delete_queueing.py, the real methods of MainWindow are called
unbound (`MainWindow._method(fake, ...)`) against a minimal object carrying
only the attributes read by the tested path -- no complete QMainWindow. A
minimal QWidget is needed here (not a bare Python object) because
QMessageBox(self)/QMenu(self)/self.cursor() require a real QWidget."""
from PySide6.QtWidgets import QMessageBox, QWidget

from src.library.thumbnail_cache import ThumbnailCache
from src.ui.main_window import MainWindow
from src.ui.thumbnail_grid import ThumbnailGrid


class _FakeConfig:
    def __init__(self, apps):
        self._apps = apps

    def get(self, key, default=None):
        assert key == "tools.external_apps"
        return self._apps


class _FakeMainWindow(QWidget):
    """Carries only the attributes read by _open_dvd_folder and friends."""

    # Reuses the real implementation (it only depends on self for the
    # QMessageBox.warning on failure, with no other MainWindow attribute).
    _launch_external_app = MainWindow._launch_external_app

    def __init__(self, apps):
        super().__init__()
        self._config = _FakeConfig(apps)
        self.opened_dialog = False

    def _open_external_apps_dialog(self) -> None:
        self.opened_dialog = True


class TestOpenDvdFolderNoAppsConfigured:
    def test_shows_message_and_launches_nothing(self, qtbot, monkeypatch):
        fake = _FakeMainWindow(apps=[])
        qtbot.addWidget(fake)
        exec_calls: list = []
        monkeypatch.setattr(QMessageBox, "exec", lambda self: exec_calls.append(self) or 0)
        popen_calls: list = []
        monkeypatch.setattr("subprocess.Popen", lambda args: popen_calls.append(args))

        MainWindow._open_dvd_folder(fake, "D:/Photos/MonDVD")

        assert len(exec_calls) == 1
        assert popen_calls == []
        assert fake.opened_dialog is False


class TestOpenDvdFolderSingleApp:
    def test_launches_directly_without_menu(self, qtbot, monkeypatch):
        fake = _FakeMainWindow(apps=[{"name": "VLC", "path": "C:/VLC/vlc.exe"}])
        qtbot.addWidget(fake)
        popen_calls: list = []
        monkeypatch.setattr("subprocess.Popen", lambda args: popen_calls.append(args))

        MainWindow._open_dvd_folder(fake, "D:/Photos/MonDVD")

        assert popen_calls == [["C:/VLC/vlc.exe", "D:/Photos/MonDVD"]]


class TestOpenDvdFolderImageOnlyAppExcluded:
    """Regression: an external application tagged with the "image" media
    scope (e.g. a photo editor) makes no sense for opening a VIDEO_TS folder --
    it must neither be launched directly, nor appear in the choice menu."""

    def test_single_image_scoped_app_shows_information_message(self, qtbot, monkeypatch):
        fake = _FakeMainWindow(apps=[{"name": "Editeur", "path": "C:/Editeur.exe", "media": "image"}])
        qtbot.addWidget(fake)
        exec_calls: list = []
        monkeypatch.setattr(QMessageBox, "exec", lambda self: exec_calls.append(self) or 0)
        popen_calls: list = []
        monkeypatch.setattr("subprocess.Popen", lambda args: popen_calls.append(args))

        MainWindow._open_dvd_folder(fake, "D:/Photos/MonDVD")

        assert len(exec_calls) == 1
        assert popen_calls == []

    def test_video_scoped_app_launched_among_mixed_apps(self, qtbot, monkeypatch):
        fake = _FakeMainWindow(apps=[
            {"name": "Editeur", "path": "C:/Editeur.exe", "media": "image"},
            {"name": "VLC", "path": "C:/VLC/vlc.exe", "media": "video"},
        ])
        qtbot.addWidget(fake)
        popen_calls: list = []
        monkeypatch.setattr("subprocess.Popen", lambda args: popen_calls.append(args))

        MainWindow._open_dvd_folder(fake, "D:/Photos/MonDVD")

        assert popen_calls == [["C:/VLC/vlc.exe", "D:/Photos/MonDVD"]]


class TestExternalAppsMenu:
    def test_menu_has_one_action_per_app(self, qtbot):
        fake = _FakeMainWindow(apps=[])
        qtbot.addWidget(fake)
        apps = [
            {"name": "VLC", "path": "C:/VLC/vlc.exe"},
            {"name": "PotPlayer", "path": "C:/Pot/PotPlayer.exe"},
        ]

        menu = MainWindow._external_apps_menu(fake, apps, "D:/Photos/MonDVD")

        assert [a.text() for a in menu.actions()] == ["VLC", "PotPlayer"]

    def test_triggering_action_launches_matching_app(self, qtbot, monkeypatch):
        fake = _FakeMainWindow(apps=[])
        qtbot.addWidget(fake)
        popen_calls: list = []
        monkeypatch.setattr("subprocess.Popen", lambda args: popen_calls.append(args))
        apps = [
            {"name": "VLC", "path": "C:/VLC/vlc.exe"},
            {"name": "PotPlayer", "path": "C:/Pot/PotPlayer.exe"},
        ]
        menu = MainWindow._external_apps_menu(fake, apps, "D:/Photos/MonDVD")

        menu.actions()[1].trigger()

        assert popen_calls == [["C:/Pot/PotPlayer.exe", "D:/Photos/MonDVD"]]


class TestEmptyMessageButtonRealClick:
    """The exact regression of the reported bug: QPushButton.clicked emits a
    bool (checked=False), which silently clobbered the default parameter
    `fp=folder_path` of the lambda wired in _on_photo_query_ready -- Popen
    then received that bool as the path ("expected str, bytes or
    os.PathLike object, not bool"), an invisible failure since it was only
    logged. Must go through a real Qt click (grid._empty_action_btn.click()),
    not a direct call of the lambda, otherwise the bug does not show up."""

    def test_real_click_passes_folder_path_not_bool(self, qtbot, tmp_path):
        (tmp_path / "VIDEO_TS").mkdir()
        cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
        grid = ThumbnailGrid(cache)
        qtbot.addWidget(grid)
        calls: list = []

        class _Fake(QWidget):
            def __init__(self):
                super().__init__()
                self._grid = grid
                self._config = _FakeConfig([])
                self._current_photos = None
                self._current_paths = None
                self._current_context = None
                self._current_album_id = None

            def _update_status(self):
                pass

            def _open_dvd_folder(self, folder_path):
                calls.append(folder_path)

        fake = _Fake()
        qtbot.addWidget(fake)

        MainWindow._on_photo_query_ready(fake, [], "ctx", None, str(tmp_path))
        grid._empty_action_btn.click()

        assert calls == [str(tmp_path)]


class TestLaunchExternalApp:
    def test_popen_failure_is_logged_not_raised(self, qtbot, monkeypatch):
        """Regression: a failure of Popen (e.g. an invalid application path)
        must neither raise an exception, nor stay invisible to the user --
        a plain logger.warning() gave the impression that the "Open with an
        external player" button did nothing."""
        fake = _FakeMainWindow(apps=[])
        qtbot.addWidget(fake)
        warning_calls: list = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *a, **kw: warning_calls.append((a, kw)) or QMessageBox.Ok,
        )

        def _boom(args):
            raise OSError("introuvable")
        monkeypatch.setattr("subprocess.Popen", _boom)

        MainWindow._launch_external_app(fake, "C:/nope.exe", "D:/Photos/MonDVD")

        assert len(warning_calls) == 1
