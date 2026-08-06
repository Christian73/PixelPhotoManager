# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/library/trash.py — point unique de mise à la corbeille.
send2trash est monkeypatché : un vrai appel enverrait les fichiers de test
dans la corbeille Windows de l'utilisateur."""
import os

import pytest

from src.library import trash


class TestMoveToTrash:
    def test_normalizes_path_and_calls_send2trash(self, tmp_path, monkeypatch):
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"x")
        calls = []
        import send2trash as s2t_module
        monkeypatch.setattr(s2t_module, "send2trash", calls.append)

        trash.move_to_trash(str(tmp_path) + "/photo.jpg")   # séparateur mixte

        assert calls == [os.path.normpath(str(f))]

    def test_missing_path_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            trash.move_to_trash(str(tmp_path / "absent.jpg"))

    def test_send2trash_error_propagates(self, tmp_path, monkeypatch):
        """Jamais de repli unlink : l'erreur corbeille remonte à l'appelant."""
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"x")
        import send2trash as s2t_module

        def _boom(path):
            raise OSError("volume sans corbeille")

        monkeypatch.setattr(s2t_module, "send2trash", _boom)

        with pytest.raises(OSError):
            trash.move_to_trash(str(f))
        assert f.exists()   # fichier intact

    def test_is_trash_available(self):
        assert trash.is_trash_available() is True
