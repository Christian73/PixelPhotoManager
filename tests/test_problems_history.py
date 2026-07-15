# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/problems_history.py (_ProblemsHistory) : journal JSONL
pur Python. `_HISTORY_PATH` est monkeypatché vers tmp_path avant instanciation
(même pattern que l'isolation de `_CONFIG_FILE` pour Config)."""
import src.core.problems_history as problems_history_module
from src.core.problems_history import _ProblemsHistory


def _make_history(tmp_path, monkeypatch):
    monkeypatch.setattr(problems_history_module, "_HISTORY_PATH", tmp_path / "problems_history.jsonl")
    return _ProblemsHistory()


class TestAddEntry:
    def test_add_entry_then_get_entries_round_trip(self, tmp_path, monkeypatch):
        history = _make_history(tmp_path, monkeypatch)

        history.add_entry(corrupted_count=5, repaired_count=3, list_path="C:/still_failed.txt")

        entries = history.get_entries()
        assert len(entries) == 1
        assert entries[0]["corrupted_count"] == 5
        assert entries[0]["repaired_count"] == 3
        assert entries[0]["still_failed_count"] == 2
        assert entries[0]["list_path"] == "C:/still_failed.txt"

    def test_add_entry_with_none_list_path(self, tmp_path, monkeypatch):
        history = _make_history(tmp_path, monkeypatch)
        history.add_entry(corrupted_count=0, repaired_count=0, list_path=None)

        entries = history.get_entries()
        assert entries[0]["list_path"] is None

    def test_entries_appear_in_insertion_order(self, tmp_path, monkeypatch):
        history = _make_history(tmp_path, monkeypatch)
        history.add_entry(1, 1, None)
        history.add_entry(2, 0, "x.txt")

        entries = history.get_entries()
        assert [e["corrupted_count"] for e in entries] == [1, 2]


class TestGetEntries:
    def test_get_entries_on_missing_file_returns_empty_list(self, tmp_path, monkeypatch):
        history = _make_history(tmp_path, monkeypatch)
        assert history.get_entries() == []

    def test_get_entries_skips_corrupt_lines(self, tmp_path, monkeypatch):
        history = _make_history(tmp_path, monkeypatch)
        history.add_entry(1, 1, None)
        with open(history._path, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")
        history.add_entry(2, 2, None)

        entries = history.get_entries()
        assert [e["corrupted_count"] for e in entries] == [1, 2]

    def test_get_entries_skips_blank_lines(self, tmp_path, monkeypatch):
        history = _make_history(tmp_path, monkeypatch)
        history.add_entry(1, 1, None)
        with open(history._path, "a", encoding="utf-8") as f:
            f.write("\n")

        assert len(history.get_entries()) == 1
