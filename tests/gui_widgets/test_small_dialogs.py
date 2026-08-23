# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) for the small dialogs extracted from main_window /
face_cluster_grid: export, saving, face reset, duplicates popup,
group merging -- plus the fmt_size helper. No exec() at all: the widgets are
driven directly."""
import sqlite3
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTranslator
from PySide6.QtWidgets import QFileDialog, QLabel

from src.core.models import PhotoInfo
from src.faces.face_database import FaceDatabase
from src.ui.duplicates_popup import _DuplicatesPopup
from src.ui.export_dialogs import _ExportDialog, _SaveOptionsDialog
from src.ui.face_merge_dialog import _MergePickerDialog, _MergeRow
from src.ui.reset_faces_dialog import _ResetFacesDialog
from src.ui.ui_utils import fmt_size


@pytest.fixture
def en_catalogue(qapp):
    """Installs ppm_en.qm for the duration of one test.

    Without a catalog installed, a `%n` message falls back on its neutral
    source ("Export 1 photo(s)"): that is a test artefact, never what the user
    sees -- `main()` always installs a catalog at startup, and
    ppm_en.qm exists only to carry the two real plural forms
    (cf. src/core/i18n.py and tools/update_translations.py)."""
    qm = Path(__file__).resolve().parents[2] / "translations" / "ppm_en.qm"
    tr = QTranslator()
    assert tr.load(str(qm)), f"{qm} absent — lancer tools/update_translations.py"
    qapp.installTranslator(tr)
    yield
    qapp.removeTranslator(tr)


class TestFmtSize:
    def test_zero_and_negative_empty(self):
        assert fmt_size(0) == ""
        assert fmt_size(-5) == ""

    def test_kilobytes_and_megabytes(self):
        assert fmt_size(512 * 1024) == "512 kB"
        assert fmt_size(int(3.2 * 1024 * 1024)) == "3.2 MB"


class TestExportDialog:
    def test_title_singular_plural(self, qtbot, en_catalogue):
        dlg1 = _ExportDialog(1)
        qtbot.addWidget(dlg1)
        assert dlg1.windowTitle() == "Export 1 photo"

        dlg2 = _ExportDialog(4)
        qtbot.addWidget(dlg2)
        assert dlg2.windowTitle() == "Export 4 photos"

    def test_default_preset_is_original_size(self, qtbot):
        dlg = _ExportDialog(2)
        qtbot.addWidget(dlg)

        assert dlg.size_preset == (None, 95)

    def test_selecting_medium_preset(self, qtbot):
        dlg = _ExportDialog(2)
        qtbot.addWidget(dlg)

        dlg._size_radios[2][0].setChecked(True)   # Medium (~2 Mpx)

        assert dlg.size_preset == (2_000_000, 94)

    def test_browse_updates_dir(self, qtbot, monkeypatch, tmp_path):
        dlg = _ExportDialog(1)
        qtbot.addWidget(dlg)
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: str(tmp_path)),
        )

        dlg._browse()

        assert dlg.export_dir == tmp_path

    def test_browse_cancelled_keeps_default(self, qtbot, monkeypatch):
        dlg = _ExportDialog(1)
        qtbot.addWidget(dlg)
        before = dlg.export_dir
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: ""),
        )

        dlg._browse()

        assert dlg.export_dir == before


class TestSaveOptionsDialog:
    def test_defaults_overwrite_with_backup(self, qtbot):
        dlg = _SaveOptionsDialog("C:/lib/photo.jpg")
        qtbot.addWidget(dlg)

        assert dlg.overwrite is True
        assert dlg.backup_before_overwrite is True

    def test_elsewhere_disables_overwrite_details(self, qtbot):
        dlg = _SaveOptionsDialog("C:/lib/photo.jpg")
        qtbot.addWidget(dlg)

        dlg._rb_elsewhere.setChecked(True)

        assert dlg.overwrite is False
        assert not dlg._overwrite_details.isEnabled()

    def test_backup_checkbox_toggle(self, qtbot):
        dlg = _SaveOptionsDialog("C:/lib/photo.jpg")
        qtbot.addWidget(dlg)

        dlg._cb_backup.setChecked(False)

        assert dlg.backup_before_overwrite is False


class TestResetFacesDialog:
    def test_default_choice_is_clustering(self, qtbot):
        dlg = _ResetFacesDialog()
        qtbot.addWidget(dlg)

        assert dlg.choice == _ResetFacesDialog.RESET_CLUSTERING
        assert dlg._frame_cluster.styleSheet() == dlg._FRAME_SEL

    def test_selecting_full_reset_updates_choice_and_frames(self, qtbot):
        dlg = _ResetFacesDialog()
        qtbot.addWidget(dlg)

        dlg._rb_full.setChecked(True)

        assert dlg.choice == _ResetFacesDialog.RESET_FULL
        assert dlg._frame_full.styleSheet() == dlg._FRAME_SEL
        assert dlg._frame_cluster.styleSheet() == dlg._FRAME_BASE

        dlg._rb_cluster.setChecked(True)
        assert dlg.choice == _ResetFacesDialog.RESET_CLUSTERING


class TestDuplicatesPopup:
    def _photos(self):
        original = PhotoInfo(path="C:/lib/orig.jpg", file_size=2 * 1024 * 1024)
        others = [
            PhotoInfo(path="C:/lib/copie1.jpg", file_size=512 * 1024),
            PhotoInfo(path="C:/lib/copie2.jpg", file_size=0),   # unknown size
        ]
        return original, others

    def test_lists_original_first_with_star(self, qtbot):
        original, others = self._photos()
        popup = _DuplicatesPopup(original, others)
        qtbot.addWidget(popup)

        assert popup._list.count() == 3
        first = popup._list.item(0)
        assert first.text().startswith("★ Original — orig.jpg")
        assert "2.0 MB" in first.text()
        assert first.font().bold()

    def test_unknown_size_shows_dash(self, qtbot):
        original, others = self._photos()
        popup = _DuplicatesPopup(original, others)
        qtbot.addWidget(popup)

        assert popup._list.item(2).text().endswith("—")

    def test_click_entry_emits_navigate(self, qtbot):
        original, others = self._photos()
        popup = _DuplicatesPopup(original, others)
        qtbot.addWidget(popup)

        with qtbot.waitSignal(popup.navigate_requested, timeout=1000) as blocker:
            popup._on_navigate(popup._list.item(1))

        assert blocker.args == [others[0].path]


class TestMergeRow:
    def test_labels_isolated_vs_group(self, qtbot, en_catalogue):
        solo = _MergeRow(9, 1)
        qtbot.addWidget(solo)
        grp = _MergeRow(4, 3)
        qtbot.addWidget(grp)

        assert any("Isolated" in lbl.text() for lbl in solo.findChildren(QLabel))
        assert any("Group 4  —  3 faces" in lbl.text()
                   for lbl in grp.findChildren(QLabel))

    def test_click_emits_selected(self, qtbot):
        row = _MergeRow(4, 3)
        qtbot.addWidget(row)
        row.show()
        qtbot.waitExposed(row)

        with qtbot.waitSignal(row.selected, timeout=1000) as blocker:
            qtbot.mouseClick(row, Qt.LeftButton)

        assert blocker.args == [4]

    def test_set_selected_switches_style(self, qtbot):
        row = _MergeRow(4, 3)
        qtbot.addWidget(row)

        row.set_selected(True)
        assert "#1e3a5f" in row.styleSheet()

        row.set_selected(False)
        assert "transparent" in row.styleSheet()


class TestMergePickerDialog:
    def _seed_clusters(self, tmp_path, cluster_counts: dict[int, int]):
        db = FaceDatabase(db_path=tmp_path / "faces.db")
        conn = sqlite3.connect(db._db_path)
        try:
            for cid, count in cluster_counts.items():
                for k in range(count):
                    conn.execute(
                        "INSERT INTO faces (photo_path, bbox_x, bbox_y, bbox_w,"
                        " bbox_h, cluster_id) VALUES (?, 1, 1, 40, 40, ?)",
                        (f"C:/lib/c{cid}_{k}.jpg", cid),
                    )
            conn.commit()
        finally:
            conn.close()
        return db

    def _settle(self, qtbot, dlg):
        # _start_loader is deferred (QTimer.singleShot(0)): let the event
        # loop trigger it, then wait for the real _AvatarLoader to finish
        # (polling -- waitSignal(finished) would miss an emission already gone by).
        qtbot.wait(50)
        loader = dlg._loader
        if loader is not None:
            def _done():
                try:
                    return not loader.isRunning()
                except RuntimeError:
                    return True
            qtbot.waitUntil(_done, timeout=3000)

    def test_rows_exclude_source_cluster(self, qtbot, tmp_path):
        db = self._seed_clusters(tmp_path, {1: 2, 2: 3, 3: 1})
        dlg = _MergePickerDialog(source_cluster_id=1, face_db=db)
        qtbot.addWidget(dlg)
        self._settle(qtbot, dlg)

        assert sorted(dlg._rows.keys()) == [2, 3]
        assert not dlg._btn_ok.isEnabled()
        assert dlg.selected_cluster_id() is None

    def test_row_selection_enables_merge(self, qtbot, tmp_path):
        db = self._seed_clusters(tmp_path, {1: 2, 2: 3})
        dlg = _MergePickerDialog(source_cluster_id=1, face_db=db)
        qtbot.addWidget(dlg)
        self._settle(qtbot, dlg)

        dlg._on_row_selected(2)

        assert dlg._btn_ok.isEnabled()
        assert dlg.selected_cluster_id() == 2
        assert dlg._rows[2]._is_selected

    def test_no_other_cluster_shows_empty_label(self, qtbot, tmp_path):
        db = self._seed_clusters(tmp_path, {1: 2})
        dlg = _MergePickerDialog(source_cluster_id=1, face_db=db)
        qtbot.addWidget(dlg)
        self._settle(qtbot, dlg)

        assert dlg._rows == {}
        assert any("Aucun autre groupe" in lbl.text()
                   for lbl in dlg.findChildren(QLabel))
