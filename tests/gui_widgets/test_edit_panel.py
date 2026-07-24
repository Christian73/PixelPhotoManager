# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour EditPanel/EditSlider.

`EditPanel.__init__` instancie `EditDatabase()` avec son chemin par défaut
(pas de `db_path=` injectable) — la redirection de `%LOCALAPPDATA%` par
`tests/conftest.py` (chargée avant tout import de test) est ce qui garantit
que ces tests n'écrivent jamais dans le vrai profil utilisateur."""
from PySide6.QtWidgets import QMessageBox

from src.core.models import PhotoInfo
from src.ui.edit_panel import EditPanel, EditSlider


def _photo(path: str) -> PhotoInfo:
    return PhotoInfo(path=path)


class TestEditSlider:
    def test_default_value(self, qtbot):
        slider = EditSlider("Luminosité", -1.0, 1.0, 0.25, 2)
        qtbot.addWidget(slider)
        assert slider.get_value() == 0.25

    def test_underlying_qslider_change_emits_value_changed(self, qtbot):
        slider = EditSlider("Luminosité", -1.0, 1.0, 0.0, 2)
        qtbot.addWidget(slider)

        with qtbot.waitSignal(slider.value_changed, timeout=1000) as blocker:
            slider._slider.setValue(50)  # scale=100 -> 0.50
        assert blocker.args == [0.5]
        assert slider.get_value() == 0.5

    def test_set_value_does_not_emit_signal(self, qtbot):
        """set_value() bloque les signaux (cf. edit_panel.py) : utilisé pour
        resynchroniser l'affichage sans redéclencher un cycle d'édition."""
        slider = EditSlider("Luminosité", -1.0, 1.0, 0.0, 2)
        qtbot.addWidget(slider)

        received = []
        slider.value_changed.connect(received.append)
        slider.set_value(0.8)
        assert received == []
        assert slider.get_value() == 0.8


class TestEditPanelUndoRedo:
    def _make_panel(self, qtbot) -> EditPanel:
        panel = EditPanel()
        qtbot.addWidget(panel)
        return panel

    def test_set_photo_starts_with_empty_undo_stack(self, qtbot):
        panel = self._make_panel(qtbot)
        panel.set_photo(_photo("C:/lib/photo1.jpg"))
        assert panel._undo_stack == []
        assert panel._redo_stack == []

    def test_push_undo_then_undo_restores_previous_edit_and_emits_signal(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        original = panel.get_edit()
        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4

        assert panel._undo_stack, "l'état pré-modification doit être empilé"

        with qtbot.waitSignal(panel.edits_changed, timeout=1000) as blocker:
            panel.undo()
        assert blocker.args == [original]
        assert panel._edit.brightness == original.brightness
        assert panel._redo_stack, "redo doit être disponible après un undo"

    def test_redo_after_undo_restores_modified_edit(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        modified = panel.get_edit()

        panel.undo()
        with qtbot.waitSignal(panel.edits_changed, timeout=1000) as blocker:
            panel.redo()
        assert blocker.args == [modified]
        assert panel._edit.brightness == 0.4

    def test_undo_persists_across_new_edit_panel_instance(self, qtbot, tmp_path):
        """Undo/redo persistant entre sessions : l'historique est rechargé
        depuis la DB (`EditDatabase`) à l'ouverture d'une photo, cf. CLAUDE.md."""
        photo = _photo(str(tmp_path / "photo1.jpg"))

        panel1 = self._make_panel(qtbot)
        panel1.set_photo(photo)
        panel1._checkpoint("Luminosité")
        panel1._push_undo("Luminosité")
        panel1._edit.brightness = 0.6
        panel1._save("Luminosité")

        panel2 = self._make_panel(qtbot)
        panel2.set_photo(photo)
        assert panel2._undo_stack, "l'historique doit être rechargé depuis la DB"


class TestEditPanelContentMinWidth:
    """Régression : la grille de boutons de traitement à 2 colonnes (Contraste,
    Vignette… en colonne 2) ne doit jamais être coupée par la QScrollArea qui
    l'héberge. Bug réel observé (pas un artefact de l'automation e2e) :
    `QScrollArea` ne propage pas le `minimumSizeHint()` de son contenu vers le
    sien (cf. commentaire sur `scroll.setMinimumWidth` dans
    `edit_panel.py::_setup_ui`) — sans plancher explicite, un panneau aussi
    étroit que la sidebar laissait la colonne 2 partiellement hors du viewport
    visible : invisible et inatteignable au clic pour un utilisateur réel, pas
    seulement pour un test automatisé. `content_min_width()` est le contrat
    que `main_window.py::_ensure_left_pane_min_width()` s'appuie dessus pour
    dimensionner le splitter ; ce test vérifie directement ce contrat, sans
    dépendre du splitter ni de l'automation OS (contrairement au scénario e2e
    `test_edit_treatments_extended.py`, qui clique via UIA Invoke — donc
    aveugle à un défaut de géométrie visuelle)."""

    def _make_panel(self, qtbot) -> EditPanel:
        panel = EditPanel()
        qtbot.addWidget(panel)
        panel.show()
        qtbot.waitExposed(panel)
        return panel

    def test_content_min_width_avoids_horizontal_clipping(self, qtbot):
        panel = self._make_panel(qtbot)
        panel.resize(panel.content_min_width(), 600)
        qtbot.wait(50)

        viewport_width = panel._scroll.viewport().width()
        inner_min_width = panel._scroll_inner.minimumSizeHint().width()
        assert viewport_width >= inner_min_width, (
            f"content_min_width() ({panel.content_min_width()}) est insuffisant : "
            f"le viewport ({viewport_width}px) reste plus étroit que le contenu "
            f"({inner_min_width}px) — la colonne 2 de boutons serait coupée"
        )

    def test_content_min_width_keeps_second_column_buttons_in_viewport(self, qtbot):
        panel = self._make_panel(qtbot)
        panel.resize(panel.content_min_width(), 600)
        qtbot.wait(50)

        viewport_width = panel._scroll.viewport().width()
        # Colonne 2 de la grille (idx impair dans _TREATMENTS, cf. _setup_ui :
        # grid.addWidget(btn, idx // 2, idx % 2)) — Contraste/Vignette dans
        # l'ordre actuel de _TREATMENTS.
        for name, btn in panel._treatment_buttons.items():
            right_edge = btn.geometry().right()
            assert right_edge <= viewport_width, (
                f"le bouton {name!r} déborde du viewport de la QScrollArea "
                f"({right_edge}px > {viewport_width}px) — colonne clippée"
            )


class TestEditPanelResetRestore:
    def _make_panel(self, qtbot) -> EditPanel:
        panel = EditPanel()
        qtbot.addWidget(panel)
        return panel

    def test_reset_all_does_not_prompt_for_confirmation(self, qtbot, tmp_path, monkeypatch):
        """Régression : reset_all() est réversible via restore_all(), donc ne doit
        plus afficher de popup de confirmation (cf. QMessageBox.question retiré)."""
        def _fail_if_called(*a, **k):
            raise AssertionError("reset_all() ne doit plus afficher de QMessageBox de confirmation")
        monkeypatch.setattr(QMessageBox, "question", _fail_if_called)
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._edit.brightness = 0.4
        panel.reset_all()
        assert not panel._edit.is_modified()

    def test_reset_all_then_restore_all_brings_back_the_edit(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        panel._save("brightness")
        modified = panel.get_edit()

        assert not panel._btn_restore.isEnabled()
        panel.reset_all()
        assert not panel._edit.is_modified()
        assert panel._btn_restore.isEnabled(), "reset_all() doit rendre le bouton restore disponible"

        with qtbot.waitSignal(panel.edits_changed, timeout=1000) as blocker:
            panel.restore_all()
        assert blocker.args == [modified]
        assert panel._edit.brightness == 0.4
        assert not panel._btn_restore.isEnabled(), "l'instantané est consommé après restauration"

        panel2 = self._make_panel(qtbot)
        panel2.set_photo(photo)
        assert panel2.get_edit().brightness == 0.4, "la restauration doit être persistée en DB"

    def test_restore_all_without_prior_reset_is_a_noop(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._edit.brightness = 0.3
        panel.restore_all()
        assert panel._edit.brightness == 0.3

    def test_new_edit_after_reset_invalidates_restore_snapshot(self, qtbot, tmp_path):
        panel = self._make_panel(qtbot)
        photo = _photo(str(tmp_path / "photo1.jpg"))
        panel.set_photo(photo)

        panel._checkpoint("Luminosité")
        panel._push_undo("Luminosité")
        panel._edit.brightness = 0.4
        panel._save("brightness")

        panel.reset_all()
        assert panel._btn_restore.isEnabled()

        panel._checkpoint("Rotation +90°")
        panel._push_undo("Rotation +90°")
        panel._edit.rotation = 90

        assert not panel._btn_restore.isEnabled(), (
            "une nouvelle retouche après reset_all() doit invalider l'instantané de restauration"
        )
