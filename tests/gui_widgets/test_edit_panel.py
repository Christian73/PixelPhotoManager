# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de widget Qt isolés (Layer 2, pytest-qt) pour EditPanel/EditSlider.

`EditPanel.__init__` instancie `EditDatabase()` avec son chemin par défaut
(pas de `db_path=` injectable) — la redirection de `%LOCALAPPDATA%` par
`tests/conftest.py` (chargée avant tout import de test) est ce qui garantit
que ces tests n'écrivent jamais dans le vrai profil utilisateur."""
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
