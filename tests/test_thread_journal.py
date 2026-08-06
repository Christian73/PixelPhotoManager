# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/thread_journal.py (_ThreadJournal) : journal JSONL pur
Python, y compris la rotation (jamais exercée jusqu'ici). `_JOURNAL_PATH` est
monkeypatché vers tmp_path avant instanciation."""
import src.core.thread_journal as thread_journal_module
from src.core.thread_journal import _ThreadJournal, rss_mb


def _make_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(thread_journal_module, "_JOURNAL_PATH", tmp_path / "thread_journal.jsonl")
    return _ThreadJournal()


class TestStartStepEnd:
    def test_start_returns_perf_counter_token(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        t0 = journal.start("scanner", "début scan")
        assert isinstance(t0, float)
        entries = journal.get_entries()
        assert entries[0]["event"] == "START"
        assert entries[0]["thread"] == "scanner"
        assert entries[0]["elapsed_ms"] is None

    def test_step_without_t0_has_no_elapsed(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        journal.step("scanner", "étape 1")
        entries = journal.get_entries()
        assert entries[0]["event"] == "STEP"
        assert entries[0]["elapsed_ms"] is None

    def test_step_with_t0_computes_elapsed(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        t0 = journal.start("scanner", "début")
        journal.step("scanner", "étape", t0=t0)
        entries = journal.get_entries()
        assert entries[1]["elapsed_ms"] >= 0

    def test_end_computes_elapsed_ms(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        t0 = journal.start("scanner", "début")
        journal.end("scanner", "fin", t0)
        entries = journal.get_entries()
        assert entries[1]["event"] == "END"
        assert entries[1]["elapsed_ms"] >= 0

    def test_error_without_t0(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        journal.error("scanner", "boom")
        entries = journal.get_entries()
        assert entries[0]["event"] == "ERROR"
        assert entries[0]["elapsed_ms"] is None

    def test_extra_kwargs_are_stored(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        journal.start("scanner", "début", photo_count=42)
        entries = journal.get_entries()
        assert entries[0]["photo_count"] == 42


class TestGetEntries:
    def test_get_entries_on_missing_file_returns_empty(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        assert journal.get_entries() == []

    def test_get_entries_respects_limit_keeping_most_recent(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        for i in range(5):
            journal.step("t", f"msg{i}")

        entries = journal.get_entries(limit=2)

        assert [e["msg"] for e in entries] == ["msg3", "msg4"]


class TestClear:
    def test_clear_empties_file_and_resets_line_count(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        journal.step("t", "msg")

        journal.clear()

        assert journal.get_entries() == []
        assert journal._line_count == 0


class TestRotation:
    def test_rotate_keeps_only_last_keep_lines(self, tmp_path, monkeypatch):
        journal = _make_journal(tmp_path, monkeypatch)
        monkeypatch.setattr(thread_journal_module, "_MAX_LINES", 5)
        monkeypatch.setattr(thread_journal_module, "_KEEP_LINES", 2)

        for i in range(6):
            journal.step("t", f"msg{i}")

        entries = journal.get_entries()
        assert [e["msg"] for e in entries] == ["msg4", "msg5"]
        assert journal._line_count == 2

    def test_count_lines_on_existing_file_reflects_prior_content(self, tmp_path, monkeypatch):
        path = tmp_path / "thread_journal.jsonl"
        path.write_text("a\nb\nc\n", encoding="utf-8")
        monkeypatch.setattr(thread_journal_module, "_JOURNAL_PATH", path)

        journal = _ThreadJournal()

        assert journal._line_count == 3


class TestRssMb:
    def test_returns_a_float(self):
        assert isinstance(rss_mb(), float)
