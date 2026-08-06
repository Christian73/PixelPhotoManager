# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de l'absorption des changements auto-infligés par FolderWatcher
(notify_self_deletions / notify_self_additions) : quand l'application
supprime ou déplace elle-même des fichiers, l'événement watcher qui suit ne
doit pas déclencher files_changed (donc pas de rescan redondant), mais un
changement externe dans le même dossier doit toujours émettre.

Les tests pilotent _take_snapshot/_process directement (pas d'événements
QFileSystemWatcher réels ni de debounce) : c'est la logique de décision qui
est testée, pas la plomberie Qt."""
import os

from src.library.folder_watcher import FolderWatcher


def _make_files(directory, names):
    for name in names:
        (directory / name).write_bytes(b"x")


def _make_watcher(qtbot, tmp_path, names):
    """Watcher avec un snapshot initial de tmp_path contenant names.
    Retourne (watcher, liste des chemins émis par files_changed)."""
    _make_files(tmp_path, names)
    watcher = FolderWatcher()
    watcher._take_snapshot(str(tmp_path))
    emitted: list[str] = []
    watcher.files_changed.connect(emitted.append)
    return watcher, emitted


class TestSelfDeletionSuppression:
    def test_announced_deletions_do_not_emit(self, qtbot, tmp_path):
        watcher, emitted = _make_watcher(qtbot, tmp_path, ["a.jpg", "b.jpg", "keep.jpg"])
        watcher.notify_self_deletions([str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")])
        os.remove(tmp_path / "a.jpg")
        os.remove(tmp_path / "b.jpg")

        watcher._process(str(tmp_path))

        assert emitted == []
        # Les noms consommés ne restent pas en table
        assert watcher._self_deleted == {}

    def test_external_deletion_in_same_dir_still_emits(self, qtbot, tmp_path):
        watcher, emitted = _make_watcher(qtbot, tmp_path, ["a.jpg", "ext.jpg"])
        watcher.notify_self_deletions([str(tmp_path / "a.jpg")])
        os.remove(tmp_path / "a.jpg")
        os.remove(tmp_path / "ext.jpg")   # suppression externe non annoncée

        watcher._process(str(tmp_path))

        assert emitted == [str(tmp_path)]

    def test_external_addition_during_suppression_still_emits(self, qtbot, tmp_path):
        watcher, emitted = _make_watcher(qtbot, tmp_path, ["a.jpg"])
        watcher.notify_self_deletions([str(tmp_path / "a.jpg")])
        os.remove(tmp_path / "a.jpg")
        _make_files(tmp_path, ["new.jpg"])   # ajout externe simultané

        watcher._process(str(tmp_path))

        assert emitted == [str(tmp_path)]

    def test_expired_ttl_restores_emission(self, qtbot, tmp_path):
        watcher, emitted = _make_watcher(qtbot, tmp_path, ["a.jpg"])
        # Deadline déjà passée : la déclaration ne doit plus rien absorber
        watcher.notify_self_deletions([str(tmp_path / "a.jpg")], ttl_s=-1.0)
        os.remove(tmp_path / "a.jpg")

        watcher._process(str(tmp_path))

        assert emitted == [str(tmp_path)]
        assert watcher._self_deleted == {}   # entrée expirée purgée

    def test_unrelated_dir_unaffected(self, qtbot, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        _make_files(sub, ["s.jpg"])
        watcher, emitted = _make_watcher(qtbot, tmp_path, ["a.jpg"])
        watcher._take_snapshot(str(sub))
        # Déclaration pour tmp_path, suppression réelle dans sub
        watcher.notify_self_deletions([str(tmp_path / "a.jpg")])
        os.remove(sub / "s.jpg")

        watcher._process(str(sub))

        assert emitted == [str(sub)]


class TestSelfAdditionSuppression:
    def test_announced_additions_do_not_emit(self, qtbot, tmp_path):
        watcher, emitted = _make_watcher(qtbot, tmp_path, ["a.jpg"])
        watcher.notify_self_additions([str(tmp_path / "moved.jpg")])
        _make_files(tmp_path, ["moved.jpg"])

        watcher._process(str(tmp_path))

        assert emitted == []

    def test_mixed_announced_addition_and_external_change_emits(self, qtbot, tmp_path):
        watcher, emitted = _make_watcher(qtbot, tmp_path, ["a.jpg"])
        watcher.notify_self_additions([str(tmp_path / "moved.jpg")])
        _make_files(tmp_path, ["moved.jpg", "external.jpg"])

        watcher._process(str(tmp_path))

        assert emitted == [str(tmp_path)]
