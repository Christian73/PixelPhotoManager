# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

from src.core.models import EditInfo
from src.processing.edit_database import EditDatabase

_SAMPLE_ANNOTATIONS = [
    {
        "id": "ann-1",
        "type": "pen",
        "color": "#ffff0000",
        "width": 0.006,
        "points": [[0.1, 0.1], [0.2, 0.3], [0.25, 0.4]],
    },
    {
        "id": "ann-2",
        "type": "text",
        "color": "#ffffffff",
        "text": "Bonjour",
        "font_family": "Arial",
        "font_size": 0.04,
        "bold": True,
        "italic": False,
        "pos": [0.5, 0.5],
    },
]


class TestEditInfoAnnotationsRoundTrip:
    def test_to_dict_from_dict_round_trip(self):
        edit = EditInfo(annotations=[dict(a) for a in _SAMPLE_ANNOTATIONS])
        restored = EditInfo.from_dict(edit.to_dict())
        assert restored.annotations == _SAMPLE_ANNOTATIONS

    def test_is_modified_true_with_annotations_only(self):
        edit = EditInfo(annotations=[dict(_SAMPLE_ANNOTATIONS[0])])
        assert edit.is_modified() is True

    def test_is_modified_false_when_empty(self):
        assert EditInfo().is_modified() is False

    def test_from_dict_defaults_to_empty_list(self):
        assert EditInfo.from_dict({}).annotations == []


class TestEditDatabaseAnnotationsPersistence:
    def _make_db(self, tmp_path: Path) -> EditDatabase:
        return EditDatabase(db_path=tmp_path / "edits.db")

    def test_save_then_load_round_trip(self, tmp_path):
        db = self._make_db(tmp_path)
        edit = EditInfo(annotations=[dict(a) for a in _SAMPLE_ANNOTATIONS])
        db.save("C:/photos/test.jpg", edit)

        loaded = db.load("C:/photos/test.jpg")
        assert loaded.annotations == _SAMPLE_ANNOTATIONS

    def test_load_missing_photo_returns_empty_annotations(self, tmp_path):
        db = self._make_db(tmp_path)
        loaded = db.load("C:/photos/never_saved.jpg")
        assert loaded.annotations == []

    def test_save_without_annotations_stores_null(self, tmp_path):
        db = self._make_db(tmp_path)
        # is_modified() doit rester True via un autre champ pour que la ligne
        # soit conservée (sinon save() la supprime).
        edit = EditInfo(brightness=0.2, annotations=[])
        db.save("C:/photos/test2.jpg", edit)
        loaded = db.load("C:/photos/test2.jpg")
        assert loaded.annotations == []

    def test_annotations_only_edit_survives_delete_of_other_fields(self, tmp_path):
        """Une photo avec seulement des annotations (aucune autre retouche) doit
        être conservée en base, pas supprimée par le chemin 'not is_modified()'."""
        db = self._make_db(tmp_path)
        edit = EditInfo(annotations=[dict(_SAMPLE_ANNOTATIONS[0])])
        db.save("C:/photos/test3.jpg", edit)
        loaded = db.load("C:/photos/test3.jpg")
        assert len(loaded.annotations) == 1

    def test_init_db_is_idempotent(self, tmp_path):
        """_init_db() (migrations ALTER TABLE incluses) doit pouvoir être
        appelé plusieurs fois sans erreur — c'est le pattern utilisé pour
        toutes les migrations automatiques au démarrage de l'appli."""
        db_path = tmp_path / "edits.db"
        db1 = EditDatabase(db_path=db_path)
        db1.save("C:/photos/test.jpg", EditInfo(annotations=[dict(_SAMPLE_ANNOTATIONS[0])]))

        db2 = EditDatabase(db_path=db_path)  # ré-appelle _init_db() sur la même DB
        loaded = db2.load("C:/photos/test.jpg")
        assert len(loaded.annotations) == 1
