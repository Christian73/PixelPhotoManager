# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour ThumbnailGrid — pas de
catalogue ni de bibliothèque réelle : ThumbnailCache et les PhotoInfo sont
entièrement synthétiques, instanciés en process."""
import io

from PIL import Image
from PySide6.QtCore import Qt

from src.core.models import EditInfo, PhotoInfo
from src.library.thumbnail_cache import ThumbnailCache, edit_signature
from src.ui.thumbnail_grid import ThumbnailCell, ThumbnailGrid


def _photo(path: str, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


def _jpeg_bytes(size=(32, 24)) -> bytes:
    """Octets JPEG valides — ce que le worker de vignette émet."""
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


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


class TestRatingChangeFromMenu:
    def test_emit_rating_change_forwards_photos_and_rating(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        p1, p2 = _photo("C:/lib/a.jpg"), _photo("C:/lib/b.jpg")
        grid.set_photos([p1, p2])

        with qtbot.waitSignal(grid.rating_change_requested, timeout=1000) as blocker:
            grid._emit_rating_change([p1, p2], 4)
        assert blocker.args == [[p1, p2], 4]


class TestRefreshRating:
    def test_refresh_rating_updates_photo_and_cell(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])
        cell = grid._make_cell(photo)
        grid._materialized[0] = cell
        qtbot.addWidget(cell)

        grid.refresh_rating({photo.path: 3})

        assert photo.rating == 3
        assert cell.photo.rating == 3

    def test_refresh_rating_ignores_unrelated_paths(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])

        grid.refresh_rating({"C:/lib/other.jpg": 5})

        assert photo.rating == 0


class TestRatingBadge:
    def test_set_rating_redraws_pixmap_when_already_loaded(self, qtbot, tmp_path):
        from PySide6.QtGui import QPixmap

        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)
        cell._set_pixmap(QPixmap(40, 40))

        cell.set_rating(4)

        assert cell.photo.rating == 4
        assert cell._pixmap is not None  # badge redessiné sans planter

    def test_set_pixmap_with_rating_does_not_crash(self, qtbot, tmp_path):
        """_add_rating_badge doit s'appliquer sans erreur pour chaque note 1-5."""
        from PySide6.QtGui import QPixmap

        grid = _make_grid(qtbot, tmp_path)
        for n in range(1, 6):
            photo = _photo(f"C:/lib/r{n}.jpg", rating=n)
            cell = grid._make_cell(photo)
            qtbot.addWidget(cell)
            cell._set_pixmap(QPixmap(40, 40))
            assert cell._pixmap is not None


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


class TestScrollToPhoto:
    """scroll_to_photo en mode normal (hors ruban) : retour de la visionneuse
    vers la grille, la vignette de la dernière photo affichée doit redevenir
    visible sans défilement inutile si elle l'est déjà."""

    def test_offscreen_photo_scrolls_into_view(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.resize(300, 200)
        grid.show()
        qtbot.waitExposed(grid)
        grid.set_thumbnail_size(80)
        photos = [_photo(f"C:/lib/p{i}.jpg") for i in range(60)]
        grid.set_photos(photos)
        last = photos[-1]

        grid.scroll_to_photo(last.path)

        idx = len(photos) - 1
        rect = grid._container.cell_rect(idx)
        vbar = grid.verticalScrollBar()
        assert vbar.value() > 0
        assert rect.top() >= vbar.value()
        assert rect.bottom() <= vbar.value() + grid.viewport().height()

    def test_already_visible_photo_does_not_scroll(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.resize(300, 400)
        grid.show()
        qtbot.waitExposed(grid)
        photos = [_photo(f"C:/lib/p{i}.jpg") for i in range(3)]
        grid.set_photos(photos)

        grid.scroll_to_photo(photos[0].path)

        assert grid.verticalScrollBar().value() == 0

    def test_unknown_path_is_noop(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.set_photos([_photo("C:/lib/a.jpg")])

        grid.scroll_to_photo("C:/lib/missing.jpg")   # ne doit pas lever


class TestLoadingIndicator:
    """set_loading — retour visuel immédiat quand une requête photo démarre
    (clic dossier/album dans la sidebar) : l'indicateur "Chargement…" n'apparaît
    qu'après 150 ms (pas de clignotement sur les requêtes rapides) et est masqué
    automatiquement dès que set_photos() livre le résultat."""

    def test_indicator_appears_after_delay(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)

        grid.set_loading(True)
        assert not grid._loading_label.isVisible()   # différé de 150 ms

        qtbot.waitUntil(lambda: grid._loading_label.isVisible(), timeout=2000)

    def test_fast_query_never_shows_indicator(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)

        grid.set_loading(True)
        grid.set_photos([_photo("C:/lib/a.jpg")])    # réponse avant les 150 ms

        assert not grid._loading_label.isVisible()
        assert not grid._loading_delay_timer.isActive()

    def test_set_photos_hides_visible_indicator(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.show()
        qtbot.waitExposed(grid)
        grid.set_loading(True)
        qtbot.waitUntil(lambda: grid._loading_label.isVisible(), timeout=2000)

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert not grid._loading_label.isVisible()


class TestEditedThumbnails:
    """Une photo tournée/recadrée dans la visionneuse doit apparaître retouchée
    dans la grille. Difficulté : la grille est virtualisée — au moment de la
    retouche, la cellule de la photo n'existe le plus souvent pas, il n'y a donc
    rien à rafraîchir. L'état est mémorisé dans grid._edits et transmis à la
    cellule au moment où elle est (re)construite."""

    def test_refresh_photo_records_edit_without_materialized_cell(self, qtbot, tmp_path):
        grid = _make_grid(qtbot, tmp_path)
        grid.set_photos([_photo("C:/lib/a.jpg")])
        grid._dematerialize_all()
        edit = EditInfo(rotation=90)

        grid.refresh_photo("C:/lib/a.jpg", edit)

        assert grid._edit_for("C:/lib/a.jpg") is edit

    def test_cell_built_later_receives_the_recorded_edit(self, qtbot, tmp_path):
        """Le cœur du correctif : la cellule créée après coup connaît la retouche
        et demandera donc la vignette retouchée, pas celle d'origine."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])
        edit = EditInfo(rotation=90)
        grid.refresh_photo(photo.path, edit)

        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)

        assert cell._edit is edit

    def test_reset_removes_the_recorded_edit(self, qtbot, tmp_path):
        """Annulation des retouches : la cellule suivante doit repartir de zéro."""
        grid = _make_grid(qtbot, tmp_path)
        photo = _photo("C:/lib/a.jpg")
        grid.set_photos([photo])
        grid.refresh_photo(photo.path, EditInfo(rotation=90))

        grid.refresh_photo(photo.path, EditInfo())

        assert grid._edit_for(photo.path) is None
        cell = grid._make_cell(photo)
        qtbot.addWidget(cell)
        assert cell._edit is None

    def test_edits_are_keyed_on_normalized_paths(self, qtbot, tmp_path):
        """La visionneuse et le catalogue ne livrent pas toujours le chemin avec
        les mêmes séparateurs — sans normalisation, la retouche serait enregistrée
        sous une clé que _edit_for() ne retrouve jamais."""
        grid = _make_grid(qtbot, tmp_path)
        edit = EditInfo(rotation=90)

        grid.refresh_photo("C:/lib/sub/a.jpg", edit)

        assert grid._edit_for("C:\\lib\\sub\\a.jpg") is edit

    def test_set_photos_reloads_edits_from_provider(self, qtbot, tmp_path):
        """Au démarrage comme à chaque changement de dossier, les retouches déjà
        enregistrées en base doivent être reprises — sans quoi une photo retouchée
        lors d'une session précédente réapparaîtrait non retouchée."""
        grid = _make_grid(qtbot, tmp_path)
        edit = EditInfo(rotation=90)
        grid.set_edit_provider(lambda: {"C:\\lib\\a.jpg": edit})

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert grid._edit_for("C:/lib/a.jpg") is edit

    def test_provider_failure_does_not_break_the_grid(self, qtbot, tmp_path):
        """Une base de retouches illisible ne doit pas empêcher d'afficher les
        photos (dégradation : vignettes non retouchées)."""
        grid = _make_grid(qtbot, tmp_path)
        grid._edit_provider = lambda: (_ for _ in ()).throw(RuntimeError("db down"))

        grid.set_photos([_photo("C:/lib/a.jpg")])

        assert len(grid._photos) == 1
        assert grid._edit_for("C:/lib/a.jpg") is None


class TestCellEditSignature:
    """ThumbnailCell — l'empreinte de retouches accompagne la vignette de bout en
    bout (demande au cache, génération, mise en cache, affichage)."""

    def _cell(self, qtbot, tmp_path, edit):
        cache = ThumbnailCache(db_path=tmp_path / "thumbs.db")
        cell = ThumbnailCell(_photo("C:/lib/a.jpg"), cache, 128, edit=edit)
        qtbot.addWidget(cell)
        return cell

    def test_ready_result_from_a_superseded_edit_is_not_displayed(self, qtbot, tmp_path):
        """L'utilisateur enchaîne deux rotations : le résultat de la première ne
        doit pas écraser l'affichage de la seconde (elles arrivent dans un ordre
        non garanti, deux workers distincts)."""
        cell = self._cell(qtbot, tmp_path, EditInfo(rotation=180))
        data = _jpeg_bytes()
        # Le chemin émis est celui du worker, donc celui de PhotoInfo — normalisé
        # par __post_init__ (séparateurs Windows). Passer le littéral en «/» ferait
        # échouer la garde `path == self._photo.path` et le test passerait pour de
        # mauvaises raisons (rien de stocké, rien d'affiché).
        path = cell._photo.path

        cell._on_thumb_ready(path, data, edit_signature(EditInfo(rotation=90)))

        assert cell._pixmap is None
        # …mais le résultat périmé reste mis en cache pour son empreinte
        assert cell._cache.get_ram(path, edit_signature(EditInfo(rotation=90)))

    def test_ready_result_for_the_current_edit_is_displayed(self, qtbot, tmp_path):
        cell = self._cell(qtbot, tmp_path, EditInfo(rotation=90))

        cell._on_thumb_ready(cell._photo.path, _jpeg_bytes(),
                             edit_signature(EditInfo(rotation=90)))

        assert cell._pixmap is not None

    def test_reload_with_edit_updates_the_cell_state(self, qtbot, tmp_path):
        cell = self._cell(qtbot, tmp_path, None)
        edit = EditInfo(rotation=90)

        cell.reload_with_edit(edit)

        assert cell._edit is edit
