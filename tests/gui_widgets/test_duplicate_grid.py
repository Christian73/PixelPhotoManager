# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/ui/duplicate_grid.py` : cartes de groupe (compteur, signaux ✗ et
double-clic, vignette), grille (peuplement via _on_groups_ready, signature de
contenu anti-clignotement, retrait de groupe, états vide/analyse) et le thread
de chargement exécuté en synchrone sur un vrai catalogue."""
import os

import pytest
from PIL import Image
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QPushButton

from src.core.models import PhotoInfo
from src.library.catalog import Catalog
from src.library.thumbnail_cache import ThumbnailCache
from src.ui.duplicate_grid import DuplicateGrid, _DuplicateCard, _DuplicateGroupLoadThread


def _photo(path) -> PhotoInfo:
    return PhotoInfo(path=os.path.normpath(str(path)), file_size=1, file_mtime=1.0)


def _groups(tmp_path, spec: dict[int, int]) -> dict:
    """spec = {group_id: nb_photos} → {group_id: [PhotoInfo, ...]}"""
    out = {}
    for gid, n in spec.items():
        photos = []
        for i in range(n):
            p = tmp_path / f"g{gid}_p{i}.jpg"
            Image.new("RGB", (16, 16)).save(str(p))
            photos.append(_photo(p))
        out[gid] = photos
    return out


@pytest.fixture
def grid(qtbot, tmp_path):
    catalog = Catalog(db_path=tmp_path / "catalog.db")
    cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
    g = DuplicateGrid(catalog, cache)
    qtbot.addWidget(g)
    return g


# ------------------------------------------------------------------ load thread


class TestLoadThread:
    def test_loads_groups_from_catalog(self, qtbot, tmp_path):
        catalog = Catalog(db_path=tmp_path / "catalog.db")
        p1 = _photo(tmp_path / "a.jpg")
        p2 = _photo(tmp_path / "b.jpg")
        catalog.add_or_update_photo(p1)
        catalog.add_or_update_photo(p2)
        catalog.set_duplicate_groups({p1.path: 1, p2.path: 1})

        thread = _DuplicateGroupLoadThread(catalog)
        results: list = []
        thread.groups_ready.connect(results.append)
        thread.run()   # synchrone

        assert len(results) == 1
        groups = results[0]
        assert set(groups.keys()) == {1}
        assert {p.path for p in groups[1]} == {p1.path, p2.path}

    def test_catalog_error_yields_empty(self, qtbot):
        class _Boom:
            def get_duplicate_groups(self):
                raise RuntimeError("db morte")

        thread = _DuplicateGroupLoadThread(_Boom())
        results: list = []
        thread.groups_ready.connect(results.append)
        thread.run()
        assert results == [{}]


# ------------------------------------------------------------------ card


class TestDuplicateCard:
    def test_count_label(self, qtbot, tmp_path):
        photos = _groups(tmp_path, {1: 3})[1]
        card = _DuplicateCard(1, photos)
        qtbot.addWidget(card)
        from PySide6.QtWidgets import QLabel
        texts = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert any("3 exemplaires" in t for t in texts)

    def test_ignore_button_emits(self, qtbot, tmp_path):
        photos = _groups(tmp_path, {5: 2})[5]
        card = _DuplicateCard(5, photos)
        qtbot.addWidget(card)
        emitted: list[int] = []
        card.ignore_requested.connect(emitted.append)
        card.findChild(QPushButton).click()
        assert emitted == [5]

    def test_double_click_emits_view(self, qtbot, tmp_path):
        photos = _groups(tmp_path, {7: 2})[7]
        card = _DuplicateCard(7, photos)
        qtbot.addWidget(card)
        emitted: list[int] = []
        card.view_requested.connect(emitted.append)
        from PySide6.QtCore import QPointF
        event = QMouseEvent(
            QMouseEvent.MouseButtonDblClick, QPointF(10, 10), QPointF(10, 10),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        card.mouseDoubleClickEvent(event)
        assert emitted == [7]

    def test_set_thumbnail_none_shows_placeholder(self, qtbot, tmp_path):
        card = _DuplicateCard(1, [])
        qtbot.addWidget(card)
        card.set_thumbnail(None)
        assert card._lbl_img.text() == "?"

    def test_set_thumbnail_scales(self, qtbot, tmp_path):
        photos = _groups(tmp_path, {1: 1})[1]
        card = _DuplicateCard(1, photos)
        qtbot.addWidget(card)
        card.set_thumbnail(QPixmap(300, 200))
        assert card._lbl_img.pixmap() is not None

    def test_load_thumbnail_from_ram_cache(self, qtbot, tmp_path):
        photos = _groups(tmp_path, {1: 1})[1]
        cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
        cache.store_pixmap(photos[0].path, QPixmap(32, 32))
        card = _DuplicateCard(1, photos)
        qtbot.addWidget(card)
        card.load_thumbnail(cache)
        assert card._lbl_img.pixmap() is not None


# ------------------------------------------------------------------ grid


class TestDuplicateGrid:
    def test_empty_state_visible(self, grid, tmp_path):
        grid._on_groups_ready({})
        assert grid._empty_panel.isVisibleTo(grid)
        assert not grid._card_area.isVisibleTo(grid)
        assert grid._lbl_title.text() == ""

    def test_cards_created(self, grid, tmp_path):
        grid._on_groups_ready(_groups(tmp_path, {1: 2, 2: 3}))
        assert len(grid._cards) == 2
        assert "2 groupes de doublons" in grid._lbl_title.text()
        assert grid._card_area.isVisibleTo(grid)

    def test_empty_photo_lists_skipped(self, grid, tmp_path):
        groups = _groups(tmp_path, {1: 2})
        groups[9] = []
        grid._on_groups_ready(groups)
        assert set(grid._cards.keys()) == {1}

    def test_same_signature_no_rebuild(self, grid, tmp_path):
        groups = _groups(tmp_path, {1: 2})
        grid._on_groups_ready(groups)
        cards_before = dict(grid._cards)
        grid._on_groups_ready(groups)   # snapshot identique → pas de reconstruction
        assert grid._cards == cards_before

    def test_changed_signature_rebuilds(self, grid, tmp_path):
        grid._on_groups_ready(_groups(tmp_path, {1: 2}))
        first = grid._cards[1]
        grid._on_groups_ready(_groups(tmp_path, {1: 2, 2: 2}))
        assert len(grid._cards) == 2
        assert grid._cards[1] is not first

    def test_remove_group(self, grid, tmp_path):
        grid._on_groups_ready(_groups(tmp_path, {1: 2, 2: 2}))
        grid.remove_group(1)
        assert set(grid._cards.keys()) == {2}
        assert "1 groupe de doublons" in grid._lbl_title.text()
        grid.remove_group(2)
        assert grid._lbl_title.text() == ""
        assert grid._empty_panel.isVisibleTo(grid)
        grid.remove_group(99)   # inexistant : sans effet ni exception

    def test_card_signals_forwarded(self, grid, tmp_path):
        grid._on_groups_ready(_groups(tmp_path, {4: 2}))
        viewed: list[int] = []
        ignored: list[int] = []
        grid.view_requested.connect(viewed.append)
        grid.group_ignored.connect(ignored.append)
        card = grid._cards[4]
        card.view_requested.emit(4)
        card.ignore_requested.emit(4)
        assert viewed == [4]
        assert ignored == [4]

    def test_set_scanning_toggles_indicator(self, grid):
        grid._on_groups_ready({})
        grid.set_scanning(True)
        assert grid._lbl_scanning.isVisibleTo(grid)
        assert not grid._lbl_empty.isVisibleTo(grid)
        grid.set_scanning(False)
        assert grid._lbl_empty.isVisibleTo(grid)
        grid.set_scanning(False)   # idempotent

    def test_ensure_loaded_and_invalidate(self, grid, monkeypatch):
        calls: list = []
        monkeypatch.setattr(grid, "refresh", lambda: calls.append(True))
        grid.ensure_loaded()
        assert calls == [True]      # 1er affichage → recharge
        grid._loaded = True
        grid.ensure_loaded()
        assert calls == [True]      # déjà chargé → rien
        grid.invalidate()
        grid.ensure_loaded()
        assert calls == [True, True]

    def test_back_and_detect_buttons_emit(self, grid, qtbot):
        back: list = []
        detect: list = []
        grid.back_requested.connect(lambda: back.append(True))
        grid.detect_requested.connect(lambda: detect.append(True))
        buttons = {b.text(): b for b in grid.findChildren(QPushButton)}
        buttons["← Photos"].click()
        assert back == [True]
        buttons["Vérifier maintenant"].click()
        assert detect == [True]

    def test_reflow_places_cards(self, grid, tmp_path):
        grid._on_groups_ready(_groups(tmp_path, {i: 2 for i in range(1, 6)}))
        grid._current_cols = 2
        grid._reflow()
        assert grid._card_gl.count() == 5


class TestNoGhostWindows:
    def test_rapid_ignores_leave_no_toplevel_cards(self, qtbot, tmp_path):
        """Régression du bug 2026-07-19 : ✗ sur plusieurs groupes d'affilée
        faisait apparaître les cartes retirées comme de petites fenêtres
        flottantes au-dessus de l'application — setParent(None) sur un widget
        encore visible conserve son état « à montrer » et la carte détachée
        (top-level) s'affichait avant que deleteLater ne s'exécute. Le
        correctif : hide() systématique avant le détachement. Nécessite une
        grille réellement affichée pour se manifester."""
        from PySide6.QtWidgets import QApplication
        from src.ui.duplicate_grid import _DuplicateCard

        catalog = Catalog(db_path=tmp_path / "catalog.db")
        cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
        grid = DuplicateGrid(catalog, cache)
        qtbot.addWidget(grid)
        grid.group_ignored.connect(grid.remove_group)
        grid.resize(640, 420)
        grid.show()
        qtbot.waitExposed(grid)

        grid._on_groups_ready(_groups(tmp_path, {i: 2 for i in range(1, 6)}))
        qtbot.wait(150)   # laisse _force_reflow (singleShot) afficher les cartes

        # ✗ sur 4 cartes coup sur coup, sans retour à l'event loop entre deux
        for gid in (1, 2, 3, 4):
            grid._cards[gid].findChild(QPushButton).click()

        # processEvents (et non qtbot.wait) : dans l'application réelle le
        # deleteLater posé pendant le traitement du clic n'est exécuté qu'au
        # retour à la boucle externe — c'est dans cet intervalle que les cartes
        # détachées s'affichaient. qtbot.wait entre dans un exec() qui traite
        # les DeferredDelete immédiatement et masquerait la régression.
        import time
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            QApplication.processEvents()

        ghosts = [
            w for w in QApplication.topLevelWidgets()
            if isinstance(w, _DuplicateCard) and w.isVisible()
        ]
        assert ghosts == []
        assert set(grid._cards.keys()) == {5}
        qtbot.wait(50)   # laisse les DeferredDelete s'exécuter avant la fermeture
        grid.close()
