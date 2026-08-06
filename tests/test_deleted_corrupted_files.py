# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/deleted_corrupted_files.py (_DeletedCorruptedFiles) :
journal JSONL pur Python, même pattern que test_problems_history.py.
`_REGISTRY_PATH` est monkeypatché vers tmp_path avant instanciation."""
import src.core.deleted_corrupted_files as deleted_corrupted_files_module
from src.core.deleted_corrupted_files import _DeletedCorruptedFiles


def _make_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deleted_corrupted_files_module, "_REGISTRY_PATH",
        tmp_path / "deleted_corrupted_files.jsonl",
    )
    return _DeletedCorruptedFiles()


class TestAddDeleted:
    def test_add_deleted_then_get_entries_round_trip(self, tmp_path, monkeypatch):
        registry = _make_registry(tmp_path, monkeypatch)

        registry.add_deleted(["a.jpg", "b.jpg"])

        entries = registry.get_entries()
        assert [e["path"] for e in entries] == ["a.jpg", "b.jpg"]
        assert all("wall" in e and "ts" in e for e in entries)

    def test_add_deleted_empty_list_is_noop(self, tmp_path, monkeypatch):
        registry = _make_registry(tmp_path, monkeypatch)
        registry.add_deleted([])
        assert registry.get_entries() == []

    def test_entries_accumulate_across_calls(self, tmp_path, monkeypatch):
        registry = _make_registry(tmp_path, monkeypatch)
        registry.add_deleted(["a.jpg"])
        registry.add_deleted(["b.jpg", "c.jpg"])

        entries = registry.get_entries()
        assert [e["path"] for e in entries] == ["a.jpg", "b.jpg", "c.jpg"]


class TestGetEntries:
    def test_get_entries_on_missing_file_returns_empty_list(self, tmp_path, monkeypatch):
        registry = _make_registry(tmp_path, monkeypatch)
        assert registry.get_entries() == []

    def test_get_entries_skips_corrupt_lines(self, tmp_path, monkeypatch):
        registry = _make_registry(tmp_path, monkeypatch)
        registry.add_deleted(["a.jpg"])
        with open(registry._path, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")
        registry.add_deleted(["b.jpg"])

        entries = registry.get_entries()
        assert [e["path"] for e in entries] == ["a.jpg", "b.jpg"]

    def test_get_entries_skips_blank_lines(self, tmp_path, monkeypatch):
        registry = _make_registry(tmp_path, monkeypatch)
        registry.add_deleted(["a.jpg"])
        with open(registry._path, "a", encoding="utf-8") as f:
            f.write("\n")

        assert len(registry.get_entries()) == 1
