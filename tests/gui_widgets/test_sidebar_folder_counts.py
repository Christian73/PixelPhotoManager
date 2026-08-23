# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests the display of the number of photos/videos per folder in the sidebar
(Sidebar.set_folder_count_provider): roots (refresh_folders) and
subfolders loaded on demand (_populate_subfolders). Sidebar does not depend
on Catalog directly -- the provider is a plain injected function,
here a stub, so as not to depend on a real database in these tests."""
import os

from PySide6.QtWidgets import QTreeWidgetItem

from src.ui.sidebar import Sidebar


def _make_sidebar(qtbot) -> Sidebar:
    sb = Sidebar()
    qtbot.addWidget(sb)
    return sb


class TestFolderCountsRoots:
    def test_refresh_folders_shows_count_from_provider(self, qtbot):
        sb = _make_sidebar(qtbot)
        sb.set_folder_count_provider(lambda folders: {os.path.normpath(f): 3 for f in folders})

        sb.refresh_folders(["C:/lib"])

        root = sb._folder_tree.topLevelItem(0)
        assert root.text(0) == "lib (3)"

    def test_refresh_folders_without_provider_shows_plain_name(self, qtbot):
        sb = _make_sidebar(qtbot)

        sb.refresh_folders(["C:/lib"])

        root = sb._folder_tree.topLevelItem(0)
        assert root.text(0) == "lib"

    def test_refresh_folders_zero_count_still_shown(self, qtbot):
        sb = _make_sidebar(qtbot)
        sb.set_folder_count_provider(lambda folders: {os.path.normpath(f): 0 for f in folders})

        sb.refresh_folders(["C:/empty"])

        root = sb._folder_tree.topLevelItem(0)
        assert root.text(0) == "empty (0)"


class TestFolderCountsSubfolders:
    def test_populate_subfolders_shows_count_from_provider(self, qtbot, tmp_path):
        sb = _make_sidebar(qtbot)
        (tmp_path / "sub1").mkdir()
        (tmp_path / "sub2").mkdir()
        requested: list[str] = []

        def provider(folders):
            requested.extend(folders)
            return {os.path.normpath(f): 5 for f in folders}

        sb.set_folder_count_provider(provider)
        parent_item = QTreeWidgetItem(["root"])

        sb._populate_subfolders(parent_item, str(tmp_path))

        labels = sorted(parent_item.child(i).text(0) for i in range(parent_item.childCount()))
        assert labels == ["sub1 (5)", "sub2 (5)"]
        assert len(requested) == 2  # a single grouped call, not one per subfolder
