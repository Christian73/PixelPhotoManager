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


class _FakeThumbCache:
    """Simule ThumbnailCache.get_ram : retourne toujours le même pixmap."""

    def __init__(self, pixmap):
        self._px = pixmap

    def get_ram(self, path):
        return self._px


class TestBaseImageCache:
    """Cache LRU des images de base + placeholder vignette (réactivité perçue) :
    la navigation prev/next affiche instantanément une photo préchargée, et une
    photo froide montre la vignette de la grille plutôt qu'un écran noir."""

    def _make_jpg(self, tmp_path, name):
        from PIL import Image
        p = tmp_path / name
        Image.new("RGB", (64, 48), color=(200, 40, 40)).save(p)
        return str(p)

    def test_placeholder_from_thumb_cache_is_immediate(self, qtbot):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap
        px = QPixmap(32, 32)
        px.fill(Qt.darkGray)
        v = PhotoViewer(thumb_cache=_FakeThumbCache(px))
        qtbot.addWidget(v)

        # Chemin inexistant : le chargement de base échouera (résultat None) —
        # seule la vignette placeholder s'affiche, et ce dès le retour de set_photo.
        v.set_photo(_photo("C:/nulle/part/photo.jpg", width=640, height=480))

        assert v._canvas._pixmap is not None
        assert not v._canvas._pixmap.isNull()

    def test_base_load_populates_lru(self, qtbot, tmp_path):
        v = PhotoViewer()
        qtbot.addWidget(v)
        p1 = self._make_jpg(tmp_path, "a.jpg")

        v.set_photo(_photo(p1))

        qtbot.waitUntil(lambda: p1 in v._base_lru, timeout=3000)
        assert p1 not in v._loading_paths

    def test_prefetch_neighbors_makes_navigation_instant(self, qtbot, tmp_path):
        v = PhotoViewer()
        qtbot.addWidget(v)
        p1 = self._make_jpg(tmp_path, "a.jpg")
        p2 = self._make_jpg(tmp_path, "b.jpg")

        v.prefetch([_photo(p1), _photo(p2)])
        qtbot.waitUntil(
            lambda: p1 in v._base_lru and p2 in v._base_lru, timeout=3000
        )

        # Cache chaud : l'affichage est synchrone (aucun thread, pas d'attente)
        v.set_photo(_photo(p2))
        assert v._canvas._pixmap is not None
        assert not v._canvas._pixmap.isNull()

    def test_lru_stays_bounded_and_evicts_oldest(self, qtbot):
        from src.ui.photo_viewer import _BASE_LRU_MAX
        v = PhotoViewer()
        qtbot.addWidget(v)

        for i in range(_BASE_LRU_MAX + 3):
            v._on_base_ready(f"C:/lib/p{i}.jpg", (b"jpeg", 10, 10))

        assert len(v._base_lru) == _BASE_LRU_MAX
        assert "C:/lib/p0.jpg" not in v._base_lru
        assert f"C:/lib/p{_BASE_LRU_MAX + 2}.jpg" in v._base_lru

    def test_invalidate_base_cache_forgets_path(self, qtbot):
        v = PhotoViewer()
        qtbot.addWidget(v)
        v._on_base_ready("C:/lib/x.jpg", (b"jpeg", 10, 10))

        v.invalidate_base_cache("C:/lib/x.jpg")

        assert "C:/lib/x.jpg" not in v._base_lru
