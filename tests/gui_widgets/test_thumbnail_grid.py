# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour ThumbnailGrid — pas de
catalogue ni de bibliothèque réelle : ThumbnailCache et les PhotoInfo sont
entièrement synthétiques, instanciés en process."""
from PySide6.QtCore import Qt

from src.core.models import PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache
from src.ui.thumbnail_grid import ThumbnailGrid


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


def _make_grid(qtbot, tmp_path) -> ThumbnailGrid:
    cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
    grid = ThumbnailGrid(cache)
    qtbot.addWidget(grid)
    return grid


class TestSetPhotos:
    def test_set_photos_stores_all_photos(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        photos = [_photo(f"C:/lib/p{i}.jpg") for i in range(5)]
        grid.set_photos(photos)
        assert len(grid._photos) == 5
        assert grid.get_selected() == []

    def test_set_photos_clears_previous_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])
        grid._on_cell_clicked(p1, Qt.NoModifier)
        assert grid.get_selected() == [p1]

        grid.set_photos([p1, p2])
        assert grid.get_selected() == []


class TestEmptyMessage:
    """show_empty_message/clear_empty_message — utilisé par MainWindow pour
    signaler un dossier vide de photos cataloguées mais contenant en réalité
    une copie de DVD (VIDEO_TS), avec une action pour l'ouvrir en externe."""

    def test_show_empty_message_displays_text_and_action(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        calls: list = []

        grid.show_empty_message("Copie de DVD détectée", "Ouvrir", lambda: calls.append(1))

        assert grid._empty_overlay.isVisible()
        assert grid._empty_label.text() == "Copie de DVD détectée"
        assert grid._empty_action_btn.isVisible()
        grid._empty_action_btn.click()
        assert calls == [1]

    def test_show_empty_message_without_action_hides_button(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)

        grid.show_empty_message("Dossier vide")

        assert grid._empty_overlay.isVisible()
        assert not grid._empty_action_btn.isVisible()

    def test_clear_empty_message_hides_overlay(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        grid.show_empty_message("Copie de DVD détectée", "Ouvrir", lambda: None)

        grid.clear_empty_message()

        assert not grid._empty_overlay.isVisible()

    def test_set_photos_clears_previous_empty_message(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        grid.show_empty_message("Copie de DVD détectée", "Ouvrir", lambda: None)

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert not grid._empty_overlay.isVisible()

    def test_second_action_replaces_first_connection(self, qtbot, tmp_path):
        """Un second show_empty_message ne doit pas empiler les connexions du
        signal clicked (sinon un clic déclenche N callbacks après N appels)."""
        grid = _make_grid(qtbot, tmp_path)
        calls: list = []
        grid.show_empty_message("Message 1", "Ouvrir", lambda: calls.append("first"))
        grid.show_empty_message("Message 2", "Ouvrir", lambda: calls.append("second"))

        grid._empty_action_btn.click()

        assert calls == ["second"]


class TestCellClickSelection:
    def test_plain_click_selects_single_photo_and_emits_signal(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        with qtbot.waitSignal(grid.selection_changed, timeout=1000) as blocker:
            grid._on_cell_clicked(p1, Qt.NoModifier)
        assert blocker.args == [[p1]]
        assert grid.get_selected() == [p1]

    def test_ctrl_click_adds_to_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p2, Qt.ControlModifier)
        assert {p.path for p in grid.get_selected()} == {p1.path, p2.path}

    def test_ctrl_click_on_selected_photo_deselects_it(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1 = _photo("C:/lib/a.jpg")
        grid.set_photos([p1])

        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p1, Qt.ControlModifier)
        assert grid.get_selected() == []

    def test_plain_click_replaces_multi_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p2, Qt.ControlModifier)
        assert len(grid.get_selected()) == 2

        grid._on_cell_clicked(p1, Qt.NoModifier)
        assert grid.get_selected() == [p1]


class TestDuplicateBadgeForwarding:
    def test_cell_duplicate_clicked_forwards_to_grid_signal(self, qtbot, tmp_path):
        """Régression de câblage : le badge de doublon d'une cellule doit
        remonter jusqu'au signal `duplicate_clicked` de la grille elle-même
        (cf. _make_cell dans thumbnail_grid.py)."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/dup.jpg", duplicate_group_id=3)
        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)

        with qtbot.waitSignal(grid.duplicate_clicked, timeout=1000) as blocker:
            cell.duplicate_clicked.emit(photo)
        assert blocker.args == [photo]


class TestNoGhostCellWindows:
    def test_dematerialize_leaves_no_visible_toplevel_cells(self, qtbot, tmp_path):
        """Garde préventive — même racine que le bug des cartes de
        DuplicateGrid (2026-07-19) : setParent(None) sur un widget encore
        visible en fait une fenêtre top-level affichable. Contrairement aux
        cartes (référencées jusqu'au deleteLater), les cellules détachées
        sont détruites par le GC dès _materialized.clear(), donc le fantôme
        n'est pas reproductible ici sans le correctif — ce test verrouille
        l'invariant « cellule détachée ⇒ cachée » sans prouver la régression."""
        from PySide6.QtWidgets import QApplication
        from src.ui.thumbnail_grid import ThumbnailCell

        grid = _make_grid(qtbot, tmp_path)
        grid.resize(640, 420)
        grid.show()
        qtbot.waitExposed(grid)
        grid.set_photos([_photo(f"C:/lib/p{i}.jpg") for i in range(12)])
        qtbot.wait(150)   # matérialisation de la zone visible
        assert grid._materialized   # précondition : des cellules existent

        grid.set_ribbon_mode(True)  # _dematerialize_all()

        # processEvents et non qtbot.wait : cf. TestNoGhostWindows dans
        # test_duplicate_grid.py — un exec() traiterait les DeferredDelete
        # et masquerait la fenêtre fantôme.
        import time
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            QApplication.processEvents()

        ghosts = [
            w for w in QApplication.topLevelWidgets()
            if isinstance(w, ThumbnailCell) and w.isVisible()
        ]
        assert ghosts == []
        qtbot.wait(50)
        grid.close()


class TestFavoriteToggleFromMenu:
    def test_toggle_favorite_flips_state_and_emits_signal(self, qtbot, tmp_path):
        """Régression : le menu contextuel « Marquer comme favori » n'était
        câblé à aucun callback (fav_label ajouté sans action) — l'action ne
        faisait donc strictement rien. Ce test aurait échoué avant le
        correctif puisqu'aucun signal n'était jamais émis."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/fav.jpg", is_favorite=False)
        grid.set_photos([photo])

        with qtbot.waitSignal(grid.favorite_toggle_requested, timeout=1000) as blocker:
            grid._toggle_favorite_from_menu(photo)
        assert blocker.args == [photo]
        assert photo.is_favorite is True

        with qtbot.waitSignal(grid.favorite_toggle_requested, timeout=1000):
            grid._toggle_favorite_from_menu(photo)
        assert photo.is_favorite is False


class TestRemovePhotos:
    def test_remove_photos_updates_list_and_selection(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2, p3 = (_photo(f"C:/lib/{n}.jpg") for n in "abc")
        grid.set_photos([p1, p2, p3])
        grid._on_cell_clicked(p1, Qt.NoModifier)
        grid._on_cell_clicked(p2, Qt.ControlModifier)

        with qtbot.waitSignal(grid.selection_changed, timeout=1000):
            grid.remove_photos([p1.path])

        assert p1 not in grid._photos
        assert grid.get_selected() == [p2]
