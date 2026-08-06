# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression/couverture : détection d'un dossier « copie de DVD » (VIDEO_TS)
sans photo cataloguée, et ouverture de son contenu dans une application
externe déjà configurée (MainWindow._open_dvd_folder/_external_apps_menu/
_launch_external_app, main_window.py).

Comme test_delete_queueing.py, les méthodes réelles de MainWindow sont
appelées en non lié (`MainWindow._methode(fake, ...)`) contre un objet minimal
ne portant que les attributs lus par le chemin testé — pas de QMainWindow
complet. Un QWidget minimal est nécessaire ici (pas un objet Python nu) car
QMessageBox(self)/QMenu(self)/self.cursor() exigent un vrai QWidget."""
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
    """Porte uniquement les attributs lus par _open_dvd_folder et consorts."""

    # Réutilise l'implémentation réelle (ne dépend que de self pour le
    # QMessageBox.warning en cas d'échec, sans autre attribut MainWindow).
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
    """Régression : une application externe taguée média "image" (ex. un
    éditeur photo) n'a pas de sens pour ouvrir un dossier VIDEO_TS — elle ne
    doit ni être lancée directement, ni apparaître dans le menu de choix."""

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
    """Régression exacte du bug signalé : QPushButton.clicked émet un bool
    (checked=False), qui écrasait silencieusement le paramètre par défaut
    `fp=folder_path` de la lambda câblée dans _on_photo_query_ready — Popen
    recevait alors ce bool en guise de chemin ("expected str, bytes or
    os.PathLike object, not bool"), échec invisible car seulement loggé.
    Doit passer par un vrai clic Qt (grid._empty_action_btn.click()), pas un
    appel direct de la lambda, sinon le bug ne se manifeste pas."""

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
        """Régression : l'échec de Popen (ex. chemin d'application invalide)
        ne doit ni lever d'exception, ni rester invisible pour l'utilisateur —
        un simple logger.warning() donnait l'impression que le bouton "Ouvrir
        avec un lecteur externe" ne faisait rien."""
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
