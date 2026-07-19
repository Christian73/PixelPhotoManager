# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/ui/thread_journal_dialog.py` : fonctions pures
(_face_index_step_stats, _generate_problems_report), panneaux/tableaux peuplés
avec des entrées synthétiques, et le dialogue principal branché sur le vrai
journal global (redirigé en temp par conftest)."""
import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from src.core.thread_journal import journal
from src.ui import thread_journal_dialog as tjd


def _entry(thread, event, elapsed=None, msg="", wall="2026-07-19 10:00:00.000"):
    return {"wall": wall, "tid": 1, "thread": thread, "event": event,
            "msg": msg, "elapsed_ms": elapsed}


_OK_ENTRIES = [
    _entry("ScanThread", "START"),
    _entry("ScanThread", "END", elapsed=120.0, msg="scan fini"),
]
_SLOW_ENTRIES = [
    _entry("ScanThread", "END", elapsed=800.0, msg="lent"),
]
_CRITICAL_ENTRIES = [
    _entry("ClusterThread", "END", elapsed=3000.0, msg="très lent"),
]
_ERROR_ENTRIES = [
    _entry("ThumbnailThread", "ERROR", msg="boom"),
]
_FACE_OK_ENTRIES = [
    _entry("FaceIndexThread", "STEP", elapsed=1000.0, msg="[1/3] a.jpg"),
    _entry("FaceIndexThread", "STEP", elapsed=5000.0, msg="[2/3] b.jpg"),
    _entry("FaceIndexThread", "STEP", elapsed=9000.0, msg="[3/3] c.jpg"),
    _entry("FaceIndexThread", "END", elapsed=60000.0, msg="fini"),
]
_FACE_BLOCKED_ENTRIES = [
    _entry("FaceIndexThread", "STEP", elapsed=1000.0),
    _entry("FaceIndexThread", "STEP", elapsed=50000.0),
    _entry("FaceIndexThread", "END", elapsed=90000.0),
]


# ------------------------------------------------------------------ fonctions pures


class TestFaceIndexStepStats:
    def test_none_when_less_than_two_steps(self):
        assert tjd._face_index_step_stats([]) is None
        assert tjd._face_index_step_stats(
            [_entry("FaceIndexThread", "STEP", elapsed=100.0)]
        ) is None

    def test_stats_computed(self):
        nb, avg, mx = tjd._face_index_step_stats(_FACE_OK_ENTRIES)
        assert nb == 3
        assert avg == pytest.approx(4000.0)
        assert mx == pytest.approx(4000.0)

    def test_other_threads_ignored(self):
        entries = _FACE_OK_ENTRIES + [_entry("ScanThread", "STEP", elapsed=2.0)]
        nb, avg, mx = tjd._face_index_step_stats(entries)
        assert nb == 3


class TestGenerateProblemsReport:
    def test_empty_journal(self):
        report = tjd._generate_problems_report([])
        assert "(journal vide)" in report

    def test_all_ok(self):
        report = tjd._generate_problems_report(_OK_ENTRIES)
        assert "Aucun problème détecté" in report
        assert "✓  ScanThread" in report

    def test_slow_thread_flagged_with_hints(self):
        report = tjd._generate_problems_report(_SLOW_ENTRIES)
        assert "LENTEUR" in report
        assert "ScanThread" in report
        assert "Pistes d'amélioration" in report

    def test_critical_thread(self):
        report = tjd._generate_problems_report(_CRITICAL_ENTRIES)
        assert "CRITIQUE — durée excessive" in report

    def test_errors_listed(self):
        report = tjd._generate_problems_report(_ERROR_ENTRIES)
        assert "ERREUR(S) — 1 erreur(s)" in report
        assert "boom" in report

    def test_error_plus_slow_is_critical(self):
        entries = [
            _entry("ClusterThread", "ERROR", msg="crash"),
            _entry("ClusterThread", "END", elapsed=5000.0),
        ]
        report = tjd._generate_problems_report(entries)
        assert "CRITIQUE — erreur(s) ET durée excessive" in report

    def test_face_index_normal_progress_not_flagged(self):
        """Durée totale énorme mais temps/photo normal → thread OK."""
        report = tjd._generate_problems_report(_FACE_OK_ENTRIES)
        assert "FaceIndexThread" in report
        assert "progression normale" in report
        assert "PROBLÈMES DÉTECTÉS" not in report

    def test_face_index_blocked_flagged(self):
        report = tjd._generate_problems_report(_FACE_BLOCKED_ENTRIES)
        assert "PROBLÈMES DÉTECTÉS" in report
        assert "Temps/photo élevé" in report


# ------------------------------------------------------------------ panneaux


class TestCompteRenduPanel:
    def _texts(self, panel):
        from PySide6.QtWidgets import QLabel
        return [lbl.text() for lbl in panel.findChildren(QLabel)]

    def test_empty(self, qtbot):
        panel = tjd._CompteRenduPanel()
        qtbot.addWidget(panel)
        panel.populate([])
        assert "Aucune donnée" in panel._banner.text()

    def test_all_ok_banner(self, qtbot):
        panel = tjd._CompteRenduPanel()
        qtbot.addWidget(panel)
        panel.populate(_OK_ENTRIES)
        assert "Tout fonctionne normalement" in panel._banner.text()
        assert any("OK" in t for t in self._texts(panel))

    def test_critical_banner(self, qtbot):
        panel = tjd._CompteRenduPanel()
        qtbot.addWidget(panel)
        panel.populate(_CRITICAL_ENTRIES + _ERROR_ENTRIES)
        assert "en erreur ou trop lent" in panel._banner.text()

    def test_slow_banner(self, qtbot):
        panel = tjd._CompteRenduPanel()
        qtbot.addWidget(panel)
        panel.populate(_SLOW_ENTRIES)
        assert "légèrement lent" in panel._banner.text()

    def test_face_index_progress_badge(self, qtbot):
        panel = tjd._CompteRenduPanel()
        qtbot.addWidget(panel)
        panel.populate(_FACE_OK_ENTRIES)
        assert any("PROGRESSE" in t for t in self._texts(panel))


class TestSummaryTable:
    def test_rows_and_status(self, qtbot):
        table = tjd._SummaryTable()
        qtbot.addWidget(table)
        table.populate(_OK_ENTRIES + _ERROR_ENTRIES)
        assert table.rowCount() == 2
        col_status = table.columnCount() - 1
        statuses = {
            table.item(r, 0).text(): table.item(r, col_status).text()
            for r in range(table.rowCount())
        }
        assert statuses["ScanThread"] == "✓"
        assert "erreur" in statuses["ThumbnailThread"]

    def test_face_index_progress_status(self, qtbot):
        table = tjd._SummaryTable()
        qtbot.addWidget(table)
        table.populate(_FACE_OK_ENTRIES)
        assert "Progresse" in table.item(0, table.columnCount() - 1).text()


class TestEventTable:
    def test_populate_and_filters(self, qtbot):
        table = tjd._EventTable()
        qtbot.addWidget(table)
        entries = _OK_ENTRIES + _ERROR_ENTRIES
        table.populate(entries)
        assert table.rowCount() == 3

        table.populate(entries, thread_filter="ScanThread")
        assert table.rowCount() == 2

        table.populate(entries, text_filter="boom")
        assert table.rowCount() == 1
        assert table.item(0, 1).text() == "ThumbnailThread"

        table.populate(entries, text_filter="inexistant")
        assert table.rowCount() == 0


# ------------------------------------------------------------------ dialogues


class TestProblemsReportDialog:
    def test_report_shown_and_copy(self, qtbot, monkeypatch):
        dlg = tjd._ProblemsReportDialog(_SLOW_ENTRIES)
        qtbot.addWidget(dlg)
        assert "LENTEUR" in dlg._edit.toPlainText()
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

        # faux presse-papiers : le vrai peut être verrouillé par une autre appli
        class _FakeClipboard:
            def __init__(self):
                self.content = ""

            def setText(self, text):
                self.content = text

        fake = _FakeClipboard()
        from PySide6.QtWidgets import QApplication
        monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: fake))
        dlg._copy()
        assert "LENTEUR" in fake.content


class TestThreadJournalDialog:
    @pytest.fixture(autouse=True)
    def _fresh_journal(self):
        journal.clear()
        yield
        journal.clear()

    def test_load_from_real_journal(self, qtbot):
        t0 = journal.start("ScanThread", "démarrage test")
        journal.end("ScanThread", "fini", t0)

        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        assert len(dlg._entries) >= 2
        assert dlg._cmb_thread.count() >= 2   # "(tous)" + ScanThread
        assert "entrée(s)" in dlg._lbl_count.text()

    def test_filter_by_thread(self, qtbot):
        t0 = journal.start("ScanThread", "a")
        journal.end("ScanThread", "b", t0)
        t1 = journal.start("ClusterThread", "c")
        journal.end("ClusterThread", "d", t1)

        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        total = dlg._event_table.rowCount()
        dlg._cmb_thread.setCurrentText("ScanThread")
        assert 0 < dlg._event_table.rowCount() < total

    def test_text_filter(self, qtbot):
        t0 = journal.start("ScanThread", "message-unique-xyz")
        journal.end("ScanThread", "autre", t0)
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        dlg._txt_filter.setText("message-unique-xyz")
        assert dlg._event_table.rowCount() == 1

    def test_live_toggle(self, qtbot):
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        dlg._btn_live.setChecked(True)
        assert dlg._refresh_timer.isActive()
        assert "Arrêter" in dlg._btn_live.text()
        dlg._btn_live.setChecked(False)
        assert not dlg._refresh_timer.isActive()

    def test_clear_with_confirmation(self, qtbot, monkeypatch):
        t0 = journal.start("ScanThread", "x")
        journal.end("ScanThread", "y", t0)
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        assert dlg._entries
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.Yes
        )
        dlg._clear()
        assert dlg._entries == []

    def test_clear_cancelled(self, qtbot, monkeypatch):
        t0 = journal.start("ScanThread", "x")
        journal.end("ScanThread", "y", t0)
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        n = len(dlg._entries)
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.No
        )
        dlg._clear()
        assert len(dlg._entries) == n

    def test_export_csv(self, qtbot, tmp_path, monkeypatch):
        t0 = journal.start("ScanThread", "exporté")
        journal.end("ScanThread", "fin", t0)
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        out = tmp_path / "journal.csv"
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "CSV (*.csv)")
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        dlg._export_csv()
        content = out.read_text(encoding="utf-8")
        assert "wall" in content.splitlines()[0]
        assert "exporté" in content

    def test_export_csv_cancelled(self, qtbot, monkeypatch):
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", lambda *a, **k: ("", "")
        )
        dlg._export_csv()   # ne doit pas lever

    def test_open_problems_report(self, qtbot, monkeypatch):
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        opened: list = []
        monkeypatch.setattr(
            tjd._ProblemsReportDialog, "exec", lambda self: opened.append(True) or 0
        )
        dlg._open_problems_report()
        assert opened == [True]

    def test_journal_size_readable(self, qtbot):
        dlg = tjd.ThreadJournalDialog()
        qtbot.addWidget(dlg)
        size = dlg._journal_size()
        assert size == "?" or "Ko" in size or "Mo" in size
