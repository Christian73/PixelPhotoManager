# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests (Layer 2) for Sidebar - live filter, lazy folder tree +
memorisation of expansion, special albums, list of people (icon cache,
merge, deletion), badges, drop of photos and folder operations with
monkeypatched dialogs. Complements test_sidebar_icon_cache /
test_sidebar_folder_counts / test_sidebar_dvd_badge."""
import os

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtWidgets import QAbstractItemView, QInputDialog, QMessageBox

from src.core.models import AlbumInfo, PersonInfo
from src.ui.people_panel import _face_bytes
from src.ui.sidebar import (
    Sidebar, _FaceIconLoader, _SingleFaceIconLoader, _MIME_PHOTOS,
    _SPECIAL_ALL, _SPECIAL_FILENAME, _SPECIAL_TAG, _SPECIAL_TAG_ITEM_PREFIX,
    _SPECIAL_RATED, _SPECIAL_RATED_ITEM_PREFIX,
)


@pytest.fixture
def sidebar(qtbot):
    sb = Sidebar()
    qtbot.addWidget(sb)
    return sb


def _person(pid, name, count=2, cover=None, bbox=None) -> PersonInfo:
    return PersonInfo(name=name, id=pid, photo_count=count,
                      cover_path=cover or "", cover_bbox=bbox)


def _make_tree(tmp_path, spec: dict) -> str:
    """Creates a tree {name: sub-dict} under tmp_path, returns the root."""
    root = tmp_path / "racine"
    root.mkdir()

    def _mk(base, d):
        for name, sub in d.items():
            p = base / name
            p.mkdir()
            _mk(p, sub)

    _mk(root, spec)
    return str(root)


# ---------------------------------------------------------------------------
# sorting and folder tree

class TestFolderTreeBasics:
    def test_sort_alpha_and_chrono(self, sidebar, tmp_path):
        a = tmp_path / "beta"; a.mkdir()
        b = tmp_path / "Alpha"; b.mkdir()
        paths = [str(a), str(b)]

        assert [os.path.basename(p) for p in sidebar._sort_folder_paths(paths)] \
            == ["Alpha", "beta"]

        sidebar.set_folder_order("alpha", "desc")
        assert [os.path.basename(p) for p in sidebar._sort_folder_paths(paths)] \
            == ["beta", "Alpha"]

        os.utime(a, (1_000_000_000, 1_000_000_000))
        os.utime(b, (2_000_000_000, 2_000_000_000))
        sidebar.set_folder_order("chrono", "asc")
        assert [os.path.basename(p) for p in sidebar._sort_folder_paths(paths)] \
            == ["beta", "Alpha"]

    def test_refresh_folders_builds_lazy_roots(self, sidebar, tmp_path):
        root = _make_tree(tmp_path, {"sub1": {}, "sub2": {"deep": {}}})

        sidebar.refresh_folders([root])

        tree = sidebar._folder_tree
        assert tree.topLevelItemCount() == 1
        item = tree.topLevelItem(0)
        assert item.data(0, Qt.UserRole) == root
        # Lazy placeholder: a child with no data
        assert item.childCount() == 1
        assert item.child(0).data(0, Qt.UserRole) is None

    def test_expand_populates_subfolders_and_memorizes(self, sidebar, tmp_path, qtbot):
        root = _make_tree(tmp_path, {"sub1": {}, "sub2": {}})
        sidebar.refresh_folders([root])
        item = sidebar._folder_tree.topLevelItem(0)

        with qtbot.waitSignal(sidebar.tree_state_changed, timeout=1000) as blocker:
            item.setExpanded(True)   # triggers _on_folder_expanded

        names = [item.child(i).text(0) for i in range(item.childCount())]
        assert names == ["sub1", "sub2"]
        assert root in sidebar._expanded_paths
        assert blocker.args == [[root]]

    def test_collapse_purges_descendants(self, sidebar, tmp_path, qtbot):
        root = _make_tree(tmp_path, {"sub1": {"deep": {}}})
        sidebar.refresh_folders([root])
        item = sidebar._folder_tree.topLevelItem(0)
        item.setExpanded(True)
        sub1 = item.child(0)
        sub1.setExpanded(True)
        assert sidebar._expanded_paths == {root, sub1.data(0, Qt.UserRole)}

        item.setExpanded(False)

        assert sidebar._expanded_paths == set()

    def test_restore_expand_from_config(self, sidebar, tmp_path):
        root = _make_tree(tmp_path, {"sub1": {"deep": {}}})
        deep = os.path.join(root, "sub1", "deep")
        sidebar.set_tree_expanded_paths([deep])

        sidebar.refresh_folders([root])

        item = sidebar._folder_tree.topLevelItem(0)
        assert item.isExpanded()
        sub1 = item.child(0)
        assert sub1.isExpanded()
        # _restoring: nothing was re-emitted nor added
        assert sidebar._expanded_paths == {deep}

    def test_folder_clicked_emits_path(self, sidebar, tmp_path, qtbot):
        root = _make_tree(tmp_path, {})
        sidebar.refresh_folders([root])
        item = sidebar._folder_tree.topLevelItem(0)

        with qtbot.waitSignal(sidebar.folder_selected, timeout=1000) as blocker:
            sidebar._on_folder_clicked(item, 0)

        assert blocker.args == [root]

    def test_select_folder_item_silently(self, sidebar, tmp_path):
        root = _make_tree(tmp_path, {})
        sidebar.refresh_folders([root])
        fired = []
        sidebar.folder_selected.connect(lambda p: fired.append(p))

        sidebar.select_folder_item(root)

        assert sidebar._folder_tree.currentItem().data(0, Qt.UserRole) == root
        assert fired == []

    def test_select_folder_item_centers_scroll(self, sidebar, tmp_path, qtbot, monkeypatch):
        root = _make_tree(tmp_path, {})
        sidebar.refresh_folders([root])
        item = sidebar._folder_tree.topLevelItem(0)
        calls = []
        monkeypatch.setattr(sidebar._folder_tree, "scrollToItem",
                             lambda it, hint: calls.append((it, hint)))

        sidebar.select_folder_item(root)
        qtbot.wait(20)   # singleShot(0) of the deferred centring

        assert calls == [(item, QAbstractItemView.PositionAtCenter)]


class TestFolderDrop:
    class _FakeDropEvent:
        def __init__(self, mime, point):
            self._mime = mime
            self._point = point
            self.result = None

        def mimeData(self):
            return self._mime

        def position(self):
            return QPointF(self._point)

        def acceptProposedAction(self):
            self.result = "accepted"

        def ignore(self):
            self.result = "ignored"

    def test_drop_on_folder_emits_files_dropped(self, sidebar, tmp_path, qtbot):
        root = _make_tree(tmp_path, {})
        sidebar.refresh_folders([root])
        sidebar.show()
        qtbot.waitExposed(sidebar)
        tree = sidebar._folder_tree
        item = tree.topLevelItem(0)
        point = tree.visualItemRect(item).center()

        mime = QMimeData()
        mime.setData(_MIME_PHOTOS, "C:/a.jpg\nC:/b.jpg".encode("utf-8"))
        event = self._FakeDropEvent(mime, point)

        with qtbot.waitSignal(sidebar.photos_dropped, timeout=1000) as blocker:
            tree.dropEvent(event)

        assert event.result == "accepted"
        assert blocker.args == [["C:/a.jpg", "C:/b.jpg"], root]

    def test_drop_without_mime_is_ignored(self, sidebar, tmp_path, qtbot):
        root = _make_tree(tmp_path, {})
        sidebar.refresh_folders([root])
        sidebar.show()
        qtbot.waitExposed(sidebar)
        event = self._FakeDropEvent(QMimeData(), sidebar._folder_tree.rect().center())

        sidebar._folder_tree.dropEvent(event)

        assert event.result == "ignored"


# ---------------------------------------------------------------------------
# filter

class TestFilter:
    def test_filter_hides_non_matching_folders_and_persons(self, sidebar, tmp_path):
        vac = tmp_path / "Vacances"; vac.mkdir()
        trav = tmp_path / "Travail"; trav.mkdir()
        sidebar.refresh_folders([str(vac), str(trav)])
        sidebar.refresh_persons([_person(1, "Alice"), _person(2, "Boris")])

        sidebar._filter_box.setText("vaca")

        tree = sidebar._folder_tree
        hidden = {tree.topLevelItem(i).text(0): tree.topLevelItem(i).isHidden()
                  for i in range(tree.topLevelItemCount())}
        assert hidden["Vacances"] is False
        assert hidden["Travail"] is True

        sidebar._filter_box.setText("bor")
        visible_persons = [
            sidebar._persons_list.item(i).data(Qt.UserRole).name
            for i in range(sidebar._persons_list.count())
            if not sidebar._persons_list.item(i).isHidden()
        ]
        assert visible_persons == ["Boris"]
        assert sidebar.filter_text == "bor"

        sidebar._filter_box.setText("")
        assert not sidebar._persons_list.item(0).isHidden()


# ---------------------------------------------------------------------------
# albums

class TestAlbums:
    # Default layout (nothing collapsed, no keyword/album): 0 Chronologie,
    # 1 Favoris, 2 Videos, 3 "By rating" header, 4-8 the 5 star levels
    # (5 down to 1), 9 "Par nom de fichier", 10 "By keyword" header - that is 11 special
    # items in total before the keywords/albums.

    def test_special_albums_present(self, sidebar):
        keys = [sidebar._albums_list.item(i).data(Qt.UserRole) for i in range(11)]
        assert keys[0] == _SPECIAL_ALL
        assert keys[3] == _SPECIAL_RATED
        assert keys[4:9] == [_SPECIAL_RATED_ITEM_PREFIX + str(n) for n in (5, 4, 3, 2, 1)]
        assert keys[9] == _SPECIAL_FILENAME
        assert keys[10] == _SPECIAL_TAG

    def test_refresh_albums_keeps_specials(self, sidebar):
        albums = [AlbumInfo(name="Été", id=1, photo_count=12)]
        sidebar.refresh_albums(albums)
        sidebar.refresh_albums(albums)   # idempotent: no duplicate

        assert sidebar._albums_list.count() == 12
        assert "Été (12)" in sidebar._albums_list.item(11).text()

    def test_album_clicked_emits_data(self, sidebar, qtbot):
        album = AlbumInfo(name="Été", id=1, photo_count=2)
        sidebar.refresh_albums([album])

        with qtbot.waitSignal(sidebar.album_selected, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(11))

        assert blocker.args[0] is album

    def test_refresh_tags_inserts_items_between_specials_and_albums(self, sidebar):
        sidebar.refresh_tags(["travail", "vacances"])
        assert sidebar._albums_list.item(11).data(Qt.UserRole) == _SPECIAL_TAG_ITEM_PREFIX + "travail"
        assert sidebar._albums_list.item(12).data(Qt.UserRole) == _SPECIAL_TAG_ITEM_PREFIX + "vacances"

        album = AlbumInfo(name="Été", id=1, photo_count=2)
        sidebar.refresh_albums([album])
        assert sidebar._albums_list.item(13).data(Qt.UserRole) is album

        # A second refresh_tags with a different list replaces the old
        # sub-items without leaving a duplicate nor moving the albums.
        sidebar.refresh_tags(["voyage"])
        assert sidebar._albums_list.item(11).data(Qt.UserRole) == _SPECIAL_TAG_ITEM_PREFIX + "voyage"
        assert sidebar._albums_list.item(12).data(Qt.UserRole) is album
        assert sidebar._albums_list.count() == 13

    def test_tag_item_clicked_emits_prefixed_data(self, sidebar, qtbot):
        sidebar.refresh_tags(["vacances"])

        with qtbot.waitSignal(sidebar.album_selected, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(11))

        assert blocker.args[0] == _SPECIAL_TAG_ITEM_PREFIX + "vacances"

    def test_tag_header_click_toggles_collapse(self, sidebar):
        sidebar.refresh_tags(["travail", "vacances"])
        assert sidebar._albums_list.count() == 13  # 11 special (including 5 ratings) + 2 keywords
        assert sidebar._albums_list.item(10).text().startswith("▾")

        sidebar._on_album_clicked(sidebar._albums_list.item(10))
        assert sidebar._albums_list.count() == 11  # sub-items hidden
        assert sidebar._albums_list.item(10).text().startswith("▸")

        sidebar._on_album_clicked(sidebar._albums_list.item(10))
        assert sidebar._albums_list.count() == 13  # expanded again
        assert sidebar._albums_list.item(10).text().startswith("▾")
        assert sidebar._albums_list.item(11).data(Qt.UserRole) == _SPECIAL_TAG_ITEM_PREFIX + "travail"

    def test_collapsed_tags_stay_hidden_across_refresh_tags(self, sidebar):
        sidebar.refresh_tags(["travail"])
        sidebar._on_album_clicked(sidebar._albums_list.item(10))  # collapses
        assert sidebar._albums_list.count() == 11

        sidebar.refresh_tags(["voyage", "été"])
        assert sidebar._albums_list.count() == 11  # still collapsed, no sub-item inserted

        sidebar._on_album_clicked(sidebar._albums_list.item(10))  # expands
        assert sidebar._albums_list.count() == 13
        assert sidebar._albums_list.item(11).data(Qt.UserRole) == _SPECIAL_TAG_ITEM_PREFIX + "voyage"

    def test_tag_header_click_still_emits_album_selected(self, sidebar, qtbot):
        with qtbot.waitSignal(sidebar.album_selected, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(10))
        assert blocker.args[0] == _SPECIAL_TAG

    def test_rated_item_clicked_emits_prefixed_data(self, sidebar, qtbot):
        with qtbot.waitSignal(sidebar.album_selected, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(6))  # 3rd level: "3 stars or more"

        assert blocker.args[0] == _SPECIAL_RATED_ITEM_PREFIX + "3"

    def test_rated_header_click_toggles_collapse(self, sidebar):
        assert sidebar._albums_list.count() == 11

        sidebar._on_album_clicked(sidebar._albums_list.item(3))
        assert sidebar._albums_list.count() == 6  # 5 levels hidden
        assert sidebar._albums_list.item(3).text().startswith("▸")
        assert sidebar._albums_list.item(4).data(Qt.UserRole) == _SPECIAL_FILENAME
        assert sidebar._albums_list.item(5).data(Qt.UserRole) == _SPECIAL_TAG

        sidebar._on_album_clicked(sidebar._albums_list.item(3))
        assert sidebar._albums_list.count() == 11  # expanded again
        assert sidebar._albums_list.item(3).text().startswith("▾")
        assert sidebar._albums_list.item(4).data(Qt.UserRole) == _SPECIAL_RATED_ITEM_PREFIX + "5"

    def test_rated_header_click_still_emits_album_selected(self, sidebar, qtbot):
        with qtbot.waitSignal(sidebar.album_selected, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(3))
        assert blocker.args[0] == _SPECIAL_RATED

    def test_tag_insertion_index_follows_collapsed_ratings(self, sidebar):
        sidebar._on_album_clicked(sidebar._albums_list.item(3))  # collapses the ratings
        sidebar.refresh_tags(["travail"])

        assert sidebar._albums_list.item(5).data(Qt.UserRole) == _SPECIAL_TAG
        assert sidebar._albums_list.item(6).data(Qt.UserRole) == _SPECIAL_TAG_ITEM_PREFIX + "travail"

    def test_tag_header_click_emits_section_collapse_changed(self, sidebar, qtbot):
        with qtbot.waitSignal(sidebar.section_collapse_changed, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(10))
        assert blocker.args == ["tags", True]

        with qtbot.waitSignal(sidebar.section_collapse_changed, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(10))
        assert blocker.args == ["tags", False]

    def test_rated_header_click_emits_section_collapse_changed(self, sidebar, qtbot):
        with qtbot.waitSignal(sidebar.section_collapse_changed, timeout=1000) as blocker:
            sidebar._on_album_clicked(sidebar._albums_list.item(3))
        assert blocker.args == ["ratings", True]

    def test_set_section_collapsed_state_restores_at_startup(self, sidebar):
        sidebar.set_section_collapsed_state(ratings_collapsed=True, tags_collapsed=True)
        sidebar.refresh_tags(["travail"])

        # ratings AND keywords collapsed from construction on: no more sub-items
        # (0 Chronologie, 1 Favoris, 2 Videos, 3 "By rating", 4 "Par nom de
        # fichier", 5 "Par mot-cle" - without the 5 levels nor the "travail" keyword)
        assert sidebar._albums_list.count() == 6
        assert sidebar._albums_list.item(3).text().startswith("▸")
        assert sidebar._albums_list.item(5).data(Qt.UserRole) == _SPECIAL_TAG
        assert sidebar._albums_list.item(5).text().startswith("▸")

    def test_rated_items_have_no_context_menu(self, sidebar, monkeypatch):
        import src.ui.sidebar as sidebar_module

        def _boom(*a, **k):
            raise AssertionError("aucun menu ne devrait s'ouvrir sur un niveau de notation")
        monkeypatch.setattr(sidebar_module, "QMenu", _boom)

        for i in range(3, 9):  # "By rating" header + its 5 sub-items
            pos = sidebar._albums_list.visualItemRect(sidebar._albums_list.item(i)).center()
            sidebar._album_context_menu(pos)  # must not raise

    def test_tag_context_menu_emits_delete_requested(self, sidebar, monkeypatch, qtbot):
        # QMenu.exec() opens a real blocking modal loop that PySide6
        # does not expose as a replaceable Python slot (unlike
        # QMessageBox.exec, reimplemented on the PySide6 side - cf. test_delete_folder_*):
        # monkeypatching QMenu.exec directly does not work (the original C++
        # call stays resolved). So we replace the QMenu class of the sidebar
        # module with a stand-in that captures the action without ever blocking.
        import src.ui.sidebar as sidebar_module
        captured: dict[str, object] = {}

        class _FakeMenu:
            def __init__(self, *a, **k):
                pass

            def addAction(self, text, callback):
                captured[text] = callback

            def exec(self, *a, **k):
                pass

        monkeypatch.setattr(sidebar_module, "QMenu", _FakeMenu)

        sidebar.refresh_tags(["vacances"])
        tag_item = sidebar._albums_list.item(11)
        pos = sidebar._albums_list.visualItemRect(tag_item).center()

        sidebar._album_context_menu(pos)
        assert len(captured) == 1
        (label, callback), = captured.items()
        assert "Delete" in label

        with qtbot.waitSignal(sidebar.tag_delete_requested, timeout=1000) as blocker:
            callback()

        assert blocker.args[0] == "vacances"

    def test_special_album_has_no_context_menu(self, sidebar, monkeypatch):
        import src.ui.sidebar as sidebar_module

        def _boom(*a, **k):
            raise AssertionError("aucun menu ne devrait s'ouvrir sur un album spécial")
        monkeypatch.setattr(sidebar_module, "QMenu", _boom)

        pos = sidebar._albums_list.visualItemRect(sidebar._albums_list.item(0)).center()
        sidebar._album_context_menu(pos)  # must not raise

    def test_select_album_item_silently(self, sidebar):
        album = AlbumInfo(name="Été", id=1)
        sidebar.refresh_albums([album])
        fired = []
        sidebar.album_selected.connect(lambda a: fired.append(a))

        sidebar.select_album_item(album)
        assert sidebar._albums_list.currentItem().data(Qt.UserRole).id == 1

        sidebar.select_album_item(_SPECIAL_ALL)
        assert sidebar._albums_list.currentRow() == 0
        assert fired == []


# ---------------------------------------------------------------------------
# people

class TestPersons:
    def test_refresh_persons_without_covers_emits_ready(self, sidebar, qtbot):
        with qtbot.waitSignal(sidebar.persons_thumbnails_ready, timeout=1000):
            sidebar.refresh_persons([_person(1, "Alice"), _person(2, "Boris")])

        assert sidebar._persons_list.count() == 2
        assert sidebar._persons_count_lbl.text() == "(2)"
        assert "Alice  (2)" == sidebar._persons_list.item(0).text()

    def test_person_clicked_emits(self, sidebar, qtbot):
        sidebar.refresh_persons([_person(1, "Alice")])

        with qtbot.waitSignal(sidebar.person_selected, timeout=1000) as blocker:
            sidebar._on_person_clicked(sidebar._persons_list.item(0))

        assert blocker.args[0].id == 1
        assert sidebar.get_selected_person_id() is None   # click != current selection

    def test_selection_restored_after_rebuild(self, sidebar, qtbot):
        persons = [_person(1, "Alice"), _person(2, "Boris")]
        sidebar.refresh_persons(persons)
        sidebar._persons_list.setCurrentRow(1)
        assert sidebar.get_selected_person_id() == 2

        sidebar.refresh_persons(persons)

        assert sidebar.get_selected_person_id() == 2

    def test_fallback_selection_when_person_removed(self, sidebar):
        sidebar.refresh_persons([_person(1, "Alice"), _person(2, "Boris")])
        sidebar._persons_list.setCurrentRow(1)   # Boris

        sidebar.refresh_persons([_person(1, "Alice")])   # Boris merged/deleted

        assert sidebar.get_selected_person_id() == 1     # closest neighbour

    def test_pending_person_selected_and_consumed(self, sidebar):
        sidebar.set_pending_person_id(2)

        sidebar.refresh_persons([_person(1, "Alice"), _person(2, "Boris")])

        assert sidebar.get_selected_person_id() == 2
        assert sidebar._pending_person_id is None

    def test_icon_cache_applied_and_purged(self, sidebar, qtbot, tmp_path):
        photo = str(tmp_path / "c.jpg")
        Image.new("RGB", (120, 120), color=(50, 60, 70)).save(photo)
        alice = _person(1, "Alice", cover=photo, bbox=(10, 10, 50, 50))
        key = Sidebar._icon_cache_key(alice)
        from src.core.models import FaceInfo
        face = FaceInfo(photo_path=photo, bbox_x=10, bbox_y=10, bbox_w=50, bbox_h=50)
        sidebar._icon_bytes_cache[key] = _face_bytes(face, 36)
        sidebar._icon_bytes_cache[("C:/orpheline.jpg", (1, 2, 3, 4))] = b"x"

        with qtbot.waitSignal(sidebar.persons_thumbnails_ready, timeout=1000):
            sidebar.refresh_persons([alice])

        # Icon laid down from the cache (no loader started) + orphan purge
        assert not sidebar._persons_list.item(0).icon().isNull()
        assert sidebar._face_loader is None
        assert list(sidebar._icon_bytes_cache.keys()) == [key]

    def test_face_icon_loader_run_sync(self, qtbot, tmp_path):
        photo = str(tmp_path / "c.jpg")
        Image.new("RGB", (120, 120), color=(50, 60, 70)).save(photo)
        alice = _person(1, "Alice", cover=photo, bbox=(10, 10, 50, 50))
        loader = _FaceIconLoader([(0, alice)])
        results = []
        loader.icon_ready.connect(lambda i, data: results.append((i, data[:4])))

        loader.run()

        assert results == [(0, b"\x89PNG")]

    def test_single_face_icon_loader_run_sync(self, qtbot, tmp_path):
        photo = str(tmp_path / "c.jpg")
        Image.new("RGB", (120, 120), color=(50, 60, 70)).save(photo)
        from src.core.models import FaceInfo
        face = FaceInfo(photo_path=photo, bbox_x=10, bbox_y=10, bbox_w=50, bbox_h=50)
        loader = _SingleFaceIconLoader(3, face)
        results = []
        loader.icon_ready.connect(lambda i, data: results.append(i))

        loader.run()

        assert results == [3]

    def test_on_face_icon_ready_sets_icon_and_cache(self, sidebar, qtbot, tmp_path):
        photo = str(tmp_path / "c.jpg")
        Image.new("RGB", (120, 120), color=(50, 60, 70)).save(photo)
        alice = _person(1, "Alice", cover=photo, bbox=(10, 10, 50, 50))
        # refresh_persons starts a real loader - wait for its end (real plumbing)
        with qtbot.waitSignal(sidebar.persons_thumbnails_ready, timeout=3000):
            sidebar.refresh_persons([alice])

        assert not sidebar._persons_list.item(0).icon().isNull()
        assert Sidebar._icon_cache_key(alice) in sidebar._icon_bytes_cache

    def test_update_persons_data_same_set_updates_labels(self, sidebar, qtbot):
        sidebar.refresh_persons([_person(1, "Alice", count=2)])

        sidebar.update_persons_data([_person(1, "Alice", count=9)])

        assert sidebar._persons_list.item(0).text() == "Alice  (9)"

    def test_update_persons_data_changed_set_rebuilds(self, sidebar, qtbot):
        sidebar.refresh_persons([_person(1, "Alice")])

        with qtbot.waitSignal(sidebar.persons_thumbnails_ready, timeout=1000):
            sidebar.update_persons_data([_person(1, "Alice"), _person(2, "Boris")])

        assert sidebar._persons_list.count() == 2

    def test_apply_person_merge(self, sidebar):
        sidebar.refresh_persons([_person(1, "Alice", 3), _person(2, "Boris", 2)])
        # Select the source to check that the selection is carried over to the target
        sidebar._persons_list.setCurrentRow(1)

        sidebar.apply_person_merge(source_id=2, target_id=1, new_count=5)

        assert sidebar._persons_list.count() == 1
        assert sidebar._persons_list.item(0).text() == "Alice  (5)"
        assert sidebar.get_selected_person_id() == 1
        assert [p.id for p in sidebar._persons] == [1]

    def test_remove_person(self, sidebar):
        sidebar.refresh_persons([_person(1, "Alice"), _person(2, "Boris")])

        sidebar.remove_person(1)

        assert sidebar._persons_list.count() == 1
        assert sidebar._persons_list.item(0).data(Qt.UserRole).id == 2

    def test_badges(self, sidebar):
        sidebar.update_cluster_badge(4)
        sidebar.update_duplicates_badge(7)

        assert sidebar._btn_identify._badge == 4
        assert sidebar._btn_duplicates._badge == 7


# ---------------------------------------------------------------------------
# splitter and folder operations

class TestSplitterAndFolderOps:
    def test_splitter_state_roundtrip(self, sidebar, qtbot):
        state = sidebar.save_splitter_state()
        assert state

        sidebar.restore_splitter_state(state)   # must not raise
        sidebar.restore_splitter_state("")      # empty state tolerated
        qtbot.wait(20)   # singleShot(0) of _update_section_arrows

    def test_create_subfolder(self, sidebar, tmp_path, qtbot, monkeypatch):
        parent = tmp_path / "parent"; parent.mkdir()
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("nouveau", True)))

        with qtbot.waitSignal(sidebar.folder_created, timeout=1000) as blocker:
            sidebar._create_subfolder(str(parent))

        assert os.path.isdir(blocker.args[0])
        assert os.path.basename(blocker.args[0]) == "nouveau"

    def test_create_subfolder_cancelled(self, sidebar, tmp_path, monkeypatch):
        parent = tmp_path / "parent"; parent.mkdir()
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("", False)))
        fired = []
        sidebar.folder_created.connect(fired.append)

        sidebar._create_subfolder(str(parent))

        assert fired == []

    def test_rename_folder(self, sidebar, tmp_path, qtbot, monkeypatch):
        old = tmp_path / "ancien"; old.mkdir()
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("nouveau", True)))

        with qtbot.waitSignal(sidebar.folder_moved, timeout=1000) as blocker:
            sidebar._rename_folder(str(old))

        assert blocker.args == [str(old), str(tmp_path / "nouveau")]
        assert (tmp_path / "nouveau").is_dir()

    def _fake_trash(self, monkeypatch):
        """Simulates the recycle bin (a real send2trash would pollute that of
        the user) and forbids any direct shutil.rmtree in sidebar."""
        import shutil
        import src.library.trash as trash_module
        calls = []
        real_rmtree = shutil.rmtree   # captured BEFORE the global patch of the module

        def _fake(path):
            calls.append(os.path.normpath(path))
            real_rmtree(path)

        monkeypatch.setattr(trash_module, "move_to_trash", _fake)
        # shutil is a shared module: this patch also covers sidebar.shutil
        monkeypatch.setattr(
            shutil, "rmtree",
            lambda *a, **k: pytest.fail("shutil.rmtree appelé au lieu de la corbeille"),
        )
        return calls

    def test_delete_folder_confirmed_goes_through_trash(
        self, sidebar, tmp_path, qtbot, monkeypatch
    ):
        doomed = tmp_path / "condamné"; doomed.mkdir()
        (doomed / "x.txt").write_text("x")
        calls = self._fake_trash(monkeypatch)
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)

        # Moving to the recycle bin goes into a real _FolderTrashThread
        with qtbot.waitSignal(sidebar.folder_deleted, timeout=3000) as blocker:
            sidebar._delete_folder(str(doomed))

        assert blocker.args == [str(doomed)]
        assert calls == [os.path.normpath(str(doomed))]
        assert not doomed.exists()

    def test_delete_folder_cancelled(self, sidebar, tmp_path, monkeypatch):
        doomed = tmp_path / "sauvé"; doomed.mkdir()
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Cancel)
        fired = []
        sidebar.folder_deleted.connect(fired.append)

        sidebar._delete_folder(str(doomed))

        assert fired == []
        assert doomed.exists()

    def test_delete_folder_trash_failure_keeps_folder(
        self, sidebar, tmp_path, qtbot, monkeypatch
    ):
        """Recycle bin unavailable -> explicit error message, folder untouched,
        folder_deleted never emitted."""
        import src.library.trash as trash_module
        doomed = tmp_path / "réseau"; doomed.mkdir()
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)

        def _boom(path):
            raise OSError("volume sans corbeille")

        monkeypatch.setattr(trash_module, "move_to_trash", _boom)
        errors = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            staticmethod(lambda *a, **k: errors.append(a[2])),
        )
        fired = []
        sidebar.folder_deleted.connect(fired.append)

        sidebar._delete_folder(str(doomed))
        qtbot.waitUntil(lambda: len(errors) == 1, timeout=3000)

        assert "n'a PAS été supprimé" in errors[0]
        assert fired == []
        assert doomed.exists()
