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
