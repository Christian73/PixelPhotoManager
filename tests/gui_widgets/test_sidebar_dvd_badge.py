# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests the "DVD copy" badge (a disc icon) put by Sidebar on the
nodes of the folder tree whose path contains a VIDEO_TS subfolder
(Sidebar._mark_if_dvd_copy, called from refresh_folders and
_populate_subfolders). Restricted to folders with no catalogued photo (an
empty/zero count) -- cf. the comment of _mark_if_dvd_copy on the cost of one
extra scandir per displayed folder."""
import os

from PySide6.QtWidgets import QTreeWidgetItem

from src.ui.sidebar import Sidebar


def _make_sidebar(qtbot) -> Sidebar:
    sb = Sidebar()
    qtbot.addWidget(sb)
    return sb


class TestDvdBadgeRoots:
    def test_dvd_root_folder_gets_icon(self, qtbot, tmp_path):
        (tmp_path / "VIDEO_TS").mkdir()
        sb = _make_sidebar(qtbot)

        sb.refresh_folders([str(tmp_path)])

        root = sb._folder_tree.topLevelItem(0)
        assert not root.icon(0).isNull()

    def test_normal_root_folder_gets_no_icon(self, qtbot, tmp_path):
        (tmp_path / "regular").mkdir()
        sb = _make_sidebar(qtbot)

        sb.refresh_folders([str(tmp_path)])

        root = sb._folder_tree.topLevelItem(0)
        assert root.icon(0).isNull()

    def test_dvd_folder_with_photos_gets_no_icon(self, qtbot, tmp_path):
        """count > 0: the folder does not look empty, so the extra scandir is
        not paid for -- an edge case deliberately out of scope."""
        (tmp_path / "VIDEO_TS").mkdir()
        sb = _make_sidebar(qtbot)
        sb.set_folder_count_provider(lambda folders: {os.path.normpath(f): 3 for f in folders})

        sb.refresh_folders([str(tmp_path)])

        root = sb._folder_tree.topLevelItem(0)
        assert root.icon(0).isNull()


class TestDvdBadgeSubfolders:
    def test_dvd_subfolder_gets_icon(self, qtbot, tmp_path):
        dvd = tmp_path / "MonDVD"
        (dvd / "VIDEO_TS").mkdir(parents=True)
        (tmp_path / "regular").mkdir()
        sb = _make_sidebar(qtbot)
        parent_item = QTreeWidgetItem(["root"])

        sb._populate_subfolders(parent_item, str(tmp_path))

        by_label = {parent_item.child(i).text(0): parent_item.child(i) for i in range(parent_item.childCount())}
        assert not by_label["MonDVD"].icon(0).isNull()
        assert by_label["regular"].icon(0).isNull()
