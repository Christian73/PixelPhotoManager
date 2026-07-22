# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour PhotoViewer — pas de
catalogue réel : PhotoInfo est synthétique, instancié en process."""
import pytest

from src.core.models import PhotoInfo
from src.ui.photo_viewer import PhotoViewer


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=str(path), **kw)


@pytest.fixture
def viewer(qtbot):
    v = PhotoViewer()
    qtbot.addWidget(v)
    return v


class TestFavoriteToggle:
    def test_toggle_favorite_emits_with_flipped_state(self, viewer, qtbot):
        """Régression : _toggle_favorite ne faisait que muter l'état en
        mémoire (photo.is_favorite + texte du bouton) sans jamais persister
        ni notifier MainWindow — le favori revenait à son état d'origine à
        la moindre relecture de la photo depuis le catalogue. Ce test aurait
        échoué avant le correctif puisqu'aucun signal n'était émis."""
        photo = _photo("C:/lib/fav.jpg", is_favorite=False)
        viewer._photo = photo

        with qtbot.waitSignal(viewer.favorite_toggle_requested, timeout=1000) as blocker:
            viewer._toggle_favorite(True)
        assert blocker.args == [photo]
        assert photo.is_favorite is True

        with qtbot.waitSignal(viewer.favorite_toggle_requested, timeout=1000):
            viewer._toggle_favorite(False)
        assert photo.is_favorite is False

    def test_toggle_favorite_from_menu_flips_toolbar_button(self, viewer, qtbot):
        photo = _photo("C:/lib/fav2.jpg", is_favorite=False)
        viewer._photo = photo

        with qtbot.waitSignal(viewer.favorite_toggle_requested, timeout=1000):
            viewer._toggle_fav_from_menu()
        assert photo.is_favorite is True
        assert viewer._btn_fav.isChecked() is True


class _FakeConfig:
    def __init__(self, apps):
        self._apps = apps

    def get(self, key, default=None):
        assert key == "tools.external_apps"
        return self._apps


class TestExternalAppsMediaScope:
    """Régression : l'icône VLC apparaissait dans la barre de la visionneuse
    même en visionnant une photo fixe (signalé par l'utilisateur). Chaque
    application externe porte désormais une portée média ("image"/"video"/
    "both", absente = "both" pour les configs pré-existantes) comparée au
    media_type de la photo affichée dans refresh_external_apps()."""

    def _app(self, tmp_path, name="App", media=None):
        exe = tmp_path / f"{name}.exe"
        exe.write_bytes(b"")
        app = {"name": name, "path": str(exe)}
        if media is not None:
            app["media"] = media
        return app

    def test_video_scoped_app_hidden_for_still_photo(self, viewer, qtbot, tmp_path):
        viewer.show()
        qtbot.waitExposed(viewer)
        app = self._app(tmp_path, "VLC", media="video")
        viewer._config = _FakeConfig([app])
        viewer._photo = _photo("C:/lib/photo.jpg", media_type="image")

        viewer.refresh_external_apps()

        assert viewer._ext_apps_layout.count() == 0
        assert viewer._ext_apps_container.isVisible() is False

    def test_video_scoped_app_shown_for_video(self, viewer, qtbot, tmp_path):
        viewer.show()
        qtbot.waitExposed(viewer)
        app = self._app(tmp_path, "VLC", media="video")
        viewer._config = _FakeConfig([app])
        viewer._photo = _photo("C:/lib/clip.mp4", media_type="video")

        viewer.refresh_external_apps()

        assert viewer._ext_apps_layout.count() == 1
        assert viewer._ext_apps_container.isVisible() is True

    def test_image_scoped_app_hidden_for_video(self, viewer, qtbot, tmp_path):
        viewer.show()
        qtbot.waitExposed(viewer)
        app = self._app(tmp_path, "Editeur", media="image")
        viewer._config = _FakeConfig([app])
        viewer._photo = _photo("C:/lib/clip.mp4", media_type="video")

        viewer.refresh_external_apps()

        assert viewer._ext_apps_layout.count() == 0
        assert viewer._ext_apps_container.isVisible() is False

    def test_legacy_app_without_media_key_shown_for_both(self, viewer, tmp_path):
        """Rétrocompatibilité : une entrée de config antérieure à cette
        fonctionnalité n'a pas de clé "media" -> traitée comme "both"."""
        app = self._app(tmp_path, "Ancien")
        viewer._config = _FakeConfig([app])

        viewer._photo = _photo("C:/lib/photo.jpg", media_type="image")
        viewer.refresh_external_apps()
        assert viewer._ext_apps_layout.count() == 1

        viewer._photo = _photo("C:/lib/clip.mp4", media_type="video")
        viewer.refresh_external_apps()
        assert viewer._ext_apps_layout.count() == 1
