# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Slots of MainWindow (main_window.py) — without ever building the window.

MainWindow.__init__ builds the whole interface (two databases, seven QThreads,
the folder watcher): unusable in a unit test. The host below therefore
INHERITS from MainWindow but never calls its __init__ — it only sets the
attributes the tested slots read, all of them recording doubles (cf.
controller_doubles).

Inheriting, rather than binding the methods one by one onto a bare QWidget (the
harness of test_main_window_faces.py), is what makes closeEvent and
keyPressEvent testable: both call super(), which demands a real MainWindow.

The construction methods (_setup_central, _setup_menu…, 554 statements) stay
deliberately out of scope: only the e2e scenarios, which drive the real window,
can cover them — and they already do.
"""
import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QMainWindow

import src.ui.main_window as mw
from src.core.models import AlbumInfo, EditInfo, PhotoInfo
from src.ui.main_window import MainWindow
from tests.gui_widgets.controller_doubles import (
    RecordingSignal, Recorder as _Rec, fake_thread as _fake_thread, make_message_box,
)

_P = os.path.normpath          # PhotoInfo normalises its path


def _photo(path, **kw) -> PhotoInfo:
    return PhotoInfo(path=path, **kw)


def _raise(exc):
    def _fn(*args, **kwargs):
        raise exc
    return _fn


# ------------------------------------------------------------------ doubles

class _Config:
    """Config stand-in: a real dict, so a set() is read back by a get()."""

    def __init__(self, **values):
        self.values = dict(values)
        self.sets: list = []
        self.folders: list = []
        self.added: list = []
        self.removed: list = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        self.sets.append((key, value))

    def get_scan_folders(self):
        return list(self.folders)

    def add_scan_folder(self, folder):
        self.added.append(folder)
        if folder not in self.folders:
            self.folders.append(folder)

    def remove_scan_folder(self, folder):
        self.removed.append(folder)
        self.folders = [f for f in self.folders if f != folder]


class _Stack:
    """QStackedWidget stand-in carrying its real state (index, current page)."""

    def __init__(self, index=0):
        self.index = index
        self.current = None
        self.visible = True

    def setCurrentIndex(self, i):
        self.index = i

    def currentIndex(self):
        return self.index

    def currentWidget(self):
        return self.current

    def isVisible(self):
        return self.visible

    def setVisible(self, value):
        self.visible = value

    def width(self):
        return 260

    def minimumSizeHint(self):
        return QSize(120, 400)


class _Panel(_Rec):
    """A doubled panel whose visibility is really tracked (show/hide/isVisible)."""

    def __init__(self, visible=False, **returns):
        super().__init__(**returns)
        self.visible = visible

    def show(self):
        self.calls.append(("show", (), {}))
        self.visible = True

    def hide(self):
        self.calls.append(("hide", (), {}))
        self.visible = False

    def isVisible(self):
        return self.visible


class _Host(MainWindow):
    """MainWindow reduced to its state: no widget, no thread, no database."""

    def __init__(self):
        QMainWindow.__init__(self)          # NEVER MainWindow.__init__
        self.log: list = []                 # calls to the neutralised collaborators
        self.queries: list = []             # (context, album_id, folder_path, result)
        self.scans: list = []               # (folders, force)

        self._config = _Config()
        self._catalog = _Rec(
            get_albums=lambda: [],
            get_all_tags=lambda: [],
            get_photos_in_folder=lambda folder: [],
        )
        self._edit_db = _Rec(load=lambda path: EditInfo())
        self._thumb_cache = _Rec()
        self._face_db = _Rec(get_faces_for_photo=lambda path: [])
        self._folder_watcher = _Rec()
        self._scanner = _Rec()

        self._sidebar = _Rec(get_selected_person_id=lambda: None)
        self._sidebar.filter_text = ""          # a real attribute, not a method
        self._sidebar.persons_thumbnails_ready = RecordingSignal()
        self._grid = _Rec(get_selected=lambda: [])
        self._grid_nav_bar = _Rec()
        self._viewer = _Panel(visible=False, current_photo=lambda: None)
        self._viewer._canvas = _Rec()
        self._viewer._canvas._crop_mode = False
        self._edit_panel = _Rec(content_min_width=lambda: 250)
        self._face_panel = _Panel()
        self._exif_panel = _Panel()
        self._right_panel = _Panel()
        self._duplicate_grid = _Rec()
        self._person_cluster_view = _Rec()
        self._person_cluster_view.current_person = None

        self._stack = _Stack()
        self._left_stack = _Stack()
        self._splitter = _Rec(sizes=lambda: [100, 400])
        self._viewer_splitter = _Rec(saveState=lambda: _Rec(data=lambda: b"state"))

        for name in ("_lbl_fileinfo", "_lbl_action", "_lbl_corrupted", "_lbl_thumb_size",
                     "_lbl_zoom", "_zoom_slider", "_zoom_pct_label", "_thumb_slider",
                     "_btn_grid_status", "_act_faces_toggle", "_act_exif_toggle",
                     "_btn_annotations_toggle", "_btn_faces_toggle", "_btn_exif_toggle"):
            setattr(self, name, _Rec())

        self._current_photos: list = []
        self._current_paths: set = set()
        self._current_context = "Toutes les photos"
        self._current_album_id = None
        self._current_photo_index = 0
        self._viewer_back_target = "grid"
        self._annotations_globally_visible = True
        self._pending_person_view_id = None
        self._cluster_start_time = None
        self._pending_deletes: list = []
        self._duplicate_ignored_paths: set = set()
        self._live_corrupted_paths: list = []
        self._scan_had_removals = False
        self._from_person_cluster_view = False
        self._pending_reindex = None
        self._face_index_pending = False
        self._persons_loaded = False
        self._dup_gate_connected = False
        for name in ("_warmup_thread", "_update_check_thread", "_slideshow_win",
                     "_delete_thread", "_duplicate_thread", "_cluster_thread",
                     "_face_indexer", "_catalog_loader", "_photo_query_thread",
                     "_persons_refresh_thread", "_dup_migration_thread",
                     "_reindex_thread"):
            setattr(self, name, None)

    # -- neutralised collaborators: each one starts a QThread ---------------
    def _start_photo_query(self, fn, context_key, album_id=None, folder_path=None):
        """Records the query AND runs its lambda: that is what tells which
        catalog call the branch really wired."""
        self.queries.append((context_key, album_id, folder_path, fn()))

    def _show_all_photos(self):
        self.log.append("show_all_photos")

    def _start_scan(self, folders, force=False):
        self.scans.append((list(folders), force))

    def show_person_clusters(self, person):
        self.log.append(("person_clusters", person))

    def show_duplicate_grid(self):
        self.log.append("duplicate_grid")

    def show_face_clusters(self):
        self.log.append("face_clusters")

    def _run_clustering(self):
        self.log.append("clustering")

    def _refresh_persons(self):
        self.log.append("refresh_persons")

    def _update_persons_counts(self):
        self.log.append("update_persons_counts")

    def _start_face_indexing(self):
        self.log.append("face_indexing")

    def _remove_persisted_corrupted_paths(self, paths):
        """Opens dedup_cache.db for real — never in a unit test."""
        self.log.append(("forget_corrupted", list(paths)))


@pytest.fixture
def host(qtbot):
    win = _Host()
    qtbot.addWidget(win)
    return win


@pytest.fixture
def box(monkeypatch):
    fake = make_message_box()
    monkeypatch.setattr(mw, "QMessageBox", fake)
    return fake


@pytest.fixture
def inputs(monkeypatch):
    """QInputDialog: the answer is decided by the test."""

    class _Input:
        text = ("", False)
        item = ("", False)

        @staticmethod
        def getText(*a, **k):
            return _Input.text

        @staticmethod
        def getItem(parent, title, label, items, current=0, editable=False):
            _Input.offered = list(items)
            chosen, ok = _Input.item
            return (chosen or items[current], ok)

    _Input.offered = []
    monkeypatch.setattr(mw, "QInputDialog", _Input)
    return _Input


@pytest.fixture
def files(monkeypatch):
    """QFileDialog: the chosen folder is decided by the test."""

    class _Files:
        directory = ""
        opened = ""

        @staticmethod
        def getExistingDirectory(*a, **k):
            return _Files.directory

        @staticmethod
        def getOpenFileName(*a, **k):
            return _Files.opened, ""

    monkeypatch.setattr(mw, "QFileDialog", _Files)
    return _Files


# ------------------------------------------------------------------ tests

class TestEncodeViewState:
    """What closeEvent persists into ui.last_view. The context is an internal
    key, which stays French whatever the language (cf. CLAUDE.md)."""

    @pytest.mark.parametrize("ctx, expected", [
        ("Toutes les photos", {"type": "all"}),
        ("Favoris",           {"type": "favorites"}),
        ("Vidéos",            {"type": "videos"}),
        ("Par notes",         {"type": "rated"}),
    ])
    def test_the_special_views(self, host, ctx, expected):
        host._current_context = ctx
        assert host._encode_view_state() == expected

    @pytest.mark.parametrize("ctx", [
        "Par notes : 3★ et plus", "Fichiers : dsc", "Mot-clé : été",
        "Recherche avancée", "",
    ])
    def test_an_ephemeral_filter_is_not_restored(self, host, ctx):
        host._current_context = ctx
        assert host._encode_view_state() == {"type": "all"}

    def test_a_person_is_restored_by_their_id(self, host):
        host._current_context = f"{mw._PERSON_CTX_PREFIX}42"
        assert host._encode_view_state() == {"type": "person", "value": 42}

    def test_a_cluster_view_is_transient(self, host):
        host._current_context = f"{mw._PERSON_CTX_PREFIX}cluster_7"
        assert host._encode_view_state() == {"type": "all"}

    def test_an_unparsable_person_falls_back_on_all(self, host):
        host._current_context = f"{mw._PERSON_CTX_PREFIX}abc"
        assert host._encode_view_state() == {"type": "all"}

    def test_an_existing_folder(self, host, tmp_path):
        host._current_context = str(tmp_path)
        assert host._encode_view_state() == {"type": "folder", "value": str(tmp_path)}

    def test_an_album_is_found_back_by_its_name(self, host):
        host._current_context = "Vacances"
        host._catalog = _Rec(get_albums=lambda: [AlbumInfo(id=3, name="Vacances")])
        assert host._encode_view_state() == {"type": "album", "value": 3}

    def test_an_unknown_name_falls_back_on_all(self, host):
        host._current_context = "Album disparu"
        assert host._encode_view_state() == {"type": "all"}

    def test_a_catalog_failure_never_blocks_the_closing(self, host):
        host._current_context = "Vacances"
        host._catalog = _Rec(get_albums=_raise(RuntimeError("base fermée")))
        assert host._encode_view_state() == {"type": "all"}


class TestContextLabel:
    """The displayed label of an internal key — the only translation site."""

    @pytest.mark.parametrize("ctx", ["Toutes les photos", "Favoris", "Vidéos",
                                     "Par notes", "Recherche avancée"])
    def test_a_known_key_is_never_shown_raw(self, host, ctx):
        assert host._context_label(ctx)

    def test_the_rating_key_keeps_its_number(self, host):
        assert "4" in host._context_label("Par notes : 4★ et plus")

    def test_the_filter_keys_keep_their_query(self, host):
        assert "dsc" in host._context_label("Fichiers : dsc")
        assert "été" in host._context_label("Mot-clé : été")

    def test_a_folder_path_is_shown_as_is(self, host):
        assert host._context_label(r"D:\Photos\2026") == r"D:\Photos\2026"


class TestUpdateStatus:
    """The status bar label, driven by the selection."""

    def test_one_selected_photo_shows_its_name_and_size(self, host):
        host._update_status([_photo("/a.jpg", file_size=2048)])
        text = host._lbl_fileinfo.last("setText")[0]
        assert text.startswith("a.jpg") and "2" in text

    def test_several_selected_photos_are_counted(self, host):
        host._current_photos = [_photo(f"/{i}.jpg") for i in range(5)]
        host._update_status(host._current_photos[:3])
        assert "3" in host._lbl_fileinfo.last("setText")[0]

    def test_with_no_selection_the_context_is_shown(self, host):
        host._current_context = "Favoris"
        host._current_photos = [_photo("/a.jpg")]
        host._update_status([])
        assert host._context_label("Favoris") in host._lbl_fileinfo.last("setText")[0]

    def test_with_no_context_only_the_count_is_shown(self, host):
        host._current_context = ""
        host._update_status([])
        assert "—" not in host._lbl_fileinfo.last("setText")[0]

    def test_the_selection_is_read_from_the_grid_by_default(self, host):
        photo = _photo("/a.jpg")
        host._grid = _Rec(get_selected=lambda: [photo])
        host._update_status()
        assert host._lbl_fileinfo.last("setText")[0].startswith("a.jpg")


class TestEnsureLeftPaneMinWidth:
    """Without this floor, the 2nd column of the edit panel becomes
    unreachable by click (cf. CLAUDE.md)."""

    def test_a_wide_enough_pane_is_left_alone(self, host):
        host._splitter = _Rec(sizes=lambda: [400, 600])
        host._ensure_left_pane_min_width()
        assert host._splitter.called("setSizes") == []

    def test_a_squeezed_pane_is_widened_at_the_expense_of_the_other(self, host):
        host._ensure_left_pane_min_width()          # sizes [100, 400], needed 250
        assert host._splitter.last("setSizes") == ([250, 250],)

    def test_the_other_pane_never_goes_down_to_zero(self, host):
        host._splitter = _Rec(sizes=lambda: [10, 20])
        host._ensure_left_pane_min_width()
        assert host._splitter.last("setSizes") == ([250, 1],)

    def test_a_splitter_with_another_number_of_panes_is_left_alone(self, host):
        host._splitter = _Rec(sizes=lambda: [10, 20, 30])
        host._ensure_left_pane_min_width()
        assert host._splitter.called("setSizes") == []


class TestAlbumSelected:
    """The sidebar dispatch: which catalog query for which entry."""

    def test_all_photos(self, host):
        host._on_album_selected(mw._SPECIAL_ALL)
        assert host.log == ["show_all_photos"]

    def test_favorites(self, host):
        host._catalog = _Rec(get_favorites=lambda: ["fav"])
        host._on_album_selected(mw._SPECIAL_FAV)
        assert host.queries == [("Favoris", None, None, ["fav"])]
        assert host._grid.last("set_ribbon_mode") == (False,)
        assert host._stack.currentIndex() == 0          # show_grid()

    def test_videos(self, host):
        host._catalog = _Rec(get_videos=lambda: ["vid"])
        host._on_album_selected(mw._SPECIAL_VIDEOS)
        assert host.queries == [("Vidéos", None, None, ["vid"])]

    def test_rated(self, host):
        host._catalog = _Rec(get_photos_min_rating=lambda n: [f"min{n}"])
        host._on_album_selected(mw._SPECIAL_RATED)
        assert host.queries == [("Par notes", None, None, ["min1"])]

    def test_one_rating_level(self, host):
        host._catalog = _Rec(get_photos_min_rating=lambda n: [f"min{n}"])
        host._on_album_selected(f"{mw._SPECIAL_RATED_ITEM_PREFIX}4")
        ctx, _, _, result = host.queries[0]
        assert result == ["min4"] and ctx == "Par notes : 4★ et plus"

    def test_a_filename_search(self, host):
        host._sidebar.filter_text = "dsc"
        host._catalog = _Rec(search=lambda q: [f"found:{q}"])
        host._on_album_selected(mw._SPECIAL_FILENAME)
        assert host.queries == [("Fichiers : dsc", None, None, ["found:dsc"])]

    def test_an_empty_filename_search_does_nothing(self, host):
        host._on_album_selected(mw._SPECIAL_FILENAME)
        assert host.queries == [] and host._grid.called("set_ribbon_mode") == []

    def test_a_tag_search(self, host):
        host._sidebar.filter_text = "été"
        host._catalog = _Rec(get_photos_by_tag=lambda t: [f"tag:{t}"])
        host._on_album_selected(mw._SPECIAL_TAG)
        assert host.queries == [("Mot-clé : été", None, None, ["tag:été"])]

    def test_an_empty_tag_search_does_nothing(self, host):
        host._on_album_selected(mw._SPECIAL_TAG)
        assert host.queries == []

    def test_one_tag_of_the_list(self, host):
        host._catalog = _Rec(get_photos_by_tag=lambda t: [f"tag:{t}"])
        host._on_album_selected(f"{mw._SPECIAL_TAG_ITEM_PREFIX}noël")
        assert host.queries == [("Mot-clé : noël", None, None, ["tag:noël"])]

    def test_an_album_carries_its_id(self, host):
        host._catalog = _Rec(get_photos_in_album=lambda aid: [f"album{aid}"])
        host._on_album_selected(AlbumInfo(id=7, name="Vacances"))
        assert host.queries == [("Vacances", 7, None, ["album7"])]

    def test_an_album_with_no_id_is_ignored(self, host):
        host._on_album_selected(AlbumInfo(id=None, name="Sans id"))
        assert host.queries == []

    def test_a_folder_carries_its_path(self, host):
        host._catalog = _Rec(get_photos_in_folder=lambda f: [f"in:{f}"])
        host._on_folder_selected(r"D:\Photos")
        assert host.queries == [(r"D:\Photos", None, r"D:\Photos", [r"in:D:\Photos"])]
        assert host._current_album_id is None


class TestRestoreLastView:
    """The view reopened at startup, from ui.last_view."""

    def test_no_saved_view_falls_back_on_all_photos(self, host):
        host._restore_last_view([], [])
        assert host.log == ["show_all_photos"]
        assert host._sidebar.last("select_album_item") == (mw._SPECIAL_ALL,)

    def test_a_watched_folder_is_reopened(self, host, tmp_path):
        host._config.values["ui.last_view"] = {"type": "folder", "value": str(tmp_path)}
        host._restore_last_view([], [str(tmp_path)])
        assert host.queries[0][0] == str(tmp_path)
        assert host._sidebar.last("select_folder_item") == (str(tmp_path),)
        assert host.log == []

    def test_a_subfolder_of_a_watched_folder_is_reopened(self, host, tmp_path):
        sub = tmp_path / "2026"
        sub.mkdir()
        host._config.values["ui.last_view"] = {"type": "folder", "value": str(sub)}
        host._restore_last_view([], [str(tmp_path)])
        assert host.queries[0][0] == str(sub)

    def test_a_folder_outside_the_watched_ones_is_refused(self, host, tmp_path):
        host._config.values["ui.last_view"] = {"type": "folder", "value": str(tmp_path)}
        host._restore_last_view([], [str(tmp_path / "ailleurs")])
        assert host.log == ["show_all_photos"]

    def test_a_folder_gone_from_the_disk_is_refused(self, host, tmp_path):
        host._config.values["ui.last_view"] = {
            "type": "folder", "value": str(tmp_path / "disparu")}
        host._restore_last_view([], [str(tmp_path)])
        assert host.log == ["show_all_photos"]

    @pytest.mark.parametrize("vtype, special, context", [
        ("favorites", mw._SPECIAL_FAV,    "Favoris"),
        ("videos",    mw._SPECIAL_VIDEOS, "Vidéos"),
        ("rated",     mw._SPECIAL_RATED,  "Par notes"),
    ])
    def test_the_special_views(self, host, vtype, special, context):
        host._config.values["ui.last_view"] = {"type": vtype}
        host._restore_last_view([], [])
        assert host.queries[0][0] == context
        assert host._sidebar.last("select_album_item") == (special,)
        assert host.log == []

    def test_a_still_existing_album(self, host):
        album = AlbumInfo(id=5, name="Vacances")
        host._config.values["ui.last_view"] = {"type": "album", "value": 5}
        host._restore_last_view([album], [])
        assert host.queries[0][:2] == ("Vacances", 5)
        assert host._sidebar.last("select_album_item") == (album,)

    def test_a_deleted_album_falls_back_on_all_photos(self, host):
        host._config.values["ui.last_view"] = {"type": "album", "value": 99}
        host._restore_last_view([AlbumInfo(id=5, name="Vacances")], [])
        assert host.log == ["show_all_photos"]

    def test_a_person_view_is_deferred(self, host):
        """The people are not loaded yet: the id is memorised and the grid
        shows all the photos while waiting."""
        host._config.values["ui.last_view"] = {"type": "person", "value": 12}
        host._restore_last_view([], [])
        assert host._pending_person_view_id == 12
        assert host.log == ["show_all_photos"]


class TestNavigatePhoto:
    """Previous/next in the viewer."""

    def test_no_photo_does_nothing(self, host):
        host._navigate_photo(1)
        assert host._viewer.called("set_photo") == []

    def test_hitting_the_end_only_refreshes_the_arrows(self, host):
        host._current_photos = [_photo("/a.jpg")]
        host._navigate_photo(-1)
        assert host._viewer.called("set_photo") == []
        assert host._viewer.called("update_nav_arrows")

    def test_an_image_feeds_the_edit_panel(self, host):
        host._current_photos = [_photo("/a.jpg"), _photo("/b.jpg")]
        host._navigate_photo(1)
        assert host._current_photo_index == 1
        assert host._viewer.last("set_photo")[0].path == _P("/b.jpg")
        assert host._edit_panel.called("set_photo")
        assert host._left_stack.currentIndex() == 1

    def test_a_video_keeps_the_sidebar(self, host):
        host._current_photos = [_photo("/a.jpg"),
                                _photo("/b.mp4", media_type="video")]
        host._navigate_photo(1)
        assert host._edit_panel.called("set_photo") == []
        assert host._left_stack.currentIndex() == 0

    def test_the_open_panels_follow_the_photo(self, host):
        host._face_panel.visible = True
        host._exif_panel.visible = True
        host._current_photos = [_photo("/a.jpg"), _photo("/b.jpg", tags=["x"])]
        host._navigate_photo(1)
        assert host._face_panel.last("set_photo") == (_P("/b.jpg"),)
        assert host._exif_panel.last("set_photo") == (_P("/b.jpg"),)
        assert host._exif_panel.last("set_tags") == (["x"],)

    def test_the_closed_panels_are_not_refreshed(self, host):
        host._current_photos = [_photo("/a.jpg"), _photo("/b.jpg")]
        host._navigate_photo(1)
        assert host._face_panel.called("set_photo") == []
        assert host._exif_panel.called("set_photo") == []

    def test_the_nearest_neighbours_are_prefetched_first(self, host):
        host._current_photos = [_photo(f"/{i}.jpg") for i in range(5)]
        host._navigate_photo(2)
        prefetched = [p.path for p in host._viewer.last("prefetch")[0]]
        assert prefetched == [_P("/1.jpg"), _P("/3.jpg"), _P("/0.jpg"), _P("/4.jpg")]


class TestPhotosDropped:
    """Drag & drop of files onto a folder of the tree."""

    def _make(self, tmp_path, name="a.jpg"):
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        f = src / name
        f.write_bytes(b"data")
        return f

    def test_the_file_is_moved_and_every_reference_updated(self, host, tmp_path):
        f = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        moved = _photo(str(dest / "a.jpg"))
        host._catalog = _Rec(get_photos_in_folder=lambda folder: [moved])

        host._on_photos_dropped([str(f)], str(dest))

        assert (dest / "a.jpg").exists() and not f.exists()
        assert host._catalog.last("move_photo") == (str(f), _P(str(dest / "a.jpg")))
        assert host._edit_db.called("rename_photo")
        assert host._thumb_cache.called("move_photo")
        assert host._face_db.called("update_path")
        assert host._current_photos == [moved]
        assert host._current_context == str(dest)

    def test_the_watcher_is_warned_before_the_disk_is_touched(self, host, tmp_path):
        f = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        host._on_photos_dropped([str(f)], str(dest))
        assert host._folder_watcher.last("notify_self_deletions") == ([str(f)],)
        assert host._folder_watcher.last("notify_self_additions")[0] == \
            [os.path.join(str(dest), "a.jpg")]

    def test_a_drop_onto_its_own_folder_is_a_no_op(self, host, tmp_path):
        f = self._make(tmp_path)
        host._on_photos_dropped([str(f)], str(f.parent))
        assert f.exists()
        assert host._catalog.called("move_photo") == []
        assert host._grid.called("set_photos") == []

    def test_an_existing_destination_is_reported_not_overwritten(self, host, tmp_path, box):
        f = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.jpg").write_bytes(b"deja la")
        host._on_photos_dropped([str(f)], str(dest))
        assert (dest / "a.jpg").read_bytes() == b"deja la"
        assert f.exists()
        assert "a.jpg" in box.warnings[0][1]

    def test_a_move_failure_is_reported(self, host, tmp_path, box, monkeypatch):
        f = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(mw.shutil, "move", _raise(OSError("disque plein")))
        host._on_photos_dropped([str(f)], str(dest))
        assert "disque plein" in box.warnings[0][1]
        assert host._catalog.called("move_photo") == []

    def test_a_reference_update_failure_never_loses_the_move(self, host, tmp_path):
        f = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        host._catalog = _Rec(move_photo=_raise(RuntimeError("base verrouillée")),
                             get_photos_in_folder=lambda folder: [])
        host._on_photos_dropped([str(f)], str(dest))
        assert (dest / "a.jpg").exists()
        assert host._grid.called("set_photos")


class TestRenameRequested:
    """Renaming from the grid/viewer context menu."""

    def _make(self, tmp_path, name="a.jpg"):
        f = tmp_path / name
        f.write_bytes(b"data")
        return _photo(str(f))

    def test_the_file_and_every_reference_are_renamed(self, host, tmp_path, inputs, box):
        photo = self._make(tmp_path)
        host._current_photos = [photo]
        inputs.text = ("b", True)

        host._on_rename_requested(photo)

        assert (tmp_path / "b.jpg").exists()
        assert host._catalog.last("rename_photo") == (
            _P(str(tmp_path / "a.jpg")), _P(str(tmp_path / "b.jpg")))
        assert host._edit_db.called("rename_photo")
        assert host._face_db.called("update_path")
        assert host._grid.called("update_photo_path")
        assert photo.filename == "b.jpg"
        assert host._viewer.called("refresh_name")

    def test_a_cancelled_dialog_changes_nothing(self, host, tmp_path, inputs):
        photo = self._make(tmp_path)
        inputs.text = ("b", False)
        host._on_rename_requested(photo)
        assert (tmp_path / "a.jpg").exists()
        assert host._catalog.called("rename_photo") == []

    @pytest.mark.parametrize("name", ["", "   "])
    def test_an_empty_name_changes_nothing(self, host, tmp_path, inputs, name):
        photo = self._make(tmp_path)
        inputs.text = (name, True)
        host._on_rename_requested(photo)
        assert host._catalog.called("rename_photo") == []

    def test_the_same_name_changes_nothing(self, host, tmp_path, inputs):
        photo = self._make(tmp_path)
        inputs.text = ("a", True)
        host._on_rename_requested(photo)
        assert host._catalog.called("rename_photo") == []

    @pytest.mark.parametrize("bad", ["a/b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a|b"])
    def test_a_forbidden_character_is_refused(self, host, tmp_path, inputs, box, bad):
        photo = self._make(tmp_path)
        inputs.text = (bad, True)
        host._on_rename_requested(photo)
        assert box.warnings
        assert host._catalog.called("rename_photo") == []

    def test_an_already_taken_name_is_refused(self, host, tmp_path, inputs, box):
        photo = self._make(tmp_path)
        (tmp_path / "b.jpg").write_bytes(b"autre")
        inputs.text = ("b", True)
        host._on_rename_requested(photo)
        assert "b.jpg" in box.warnings[0][1]
        assert (tmp_path / "a.jpg").exists()

    def test_an_os_error_is_reported(self, host, tmp_path, inputs, box, monkeypatch):
        photo = self._make(tmp_path)
        inputs.text = ("b", True)
        monkeypatch.setattr(mw.Path, "rename", _raise(OSError("occupé")))
        host._on_rename_requested(photo)
        assert "occupé" in box.criticals[0][1]
        assert host._catalog.called("rename_photo") == []


class TestMoveRequested:
    """Moving a single photo (viewer/grid context menu)."""

    def _make(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "a.jpg"
        f.write_bytes(b"data")
        return _photo(str(f))

    def test_the_photo_leaves_the_current_view(self, host, tmp_path, files, box):
        photo = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        files.directory = str(dest)
        host._current_photos = [photo]
        host._current_paths = {photo.path}

        host._on_move_requested(photo)

        assert (dest / "a.jpg").exists()
        assert host._current_photos == []
        assert host._current_paths == set()
        assert host._thumb_cache.last("invalidate") == (photo.path,)
        assert host._grid.last("remove_photos") == ([photo.path],)

    def test_a_cancelled_dialog_changes_nothing(self, host, tmp_path, files):
        photo = self._make(tmp_path)
        host._on_move_requested(photo)
        assert host._catalog.called("rename_photo") == []

    def test_the_same_folder_changes_nothing(self, host, tmp_path, files):
        photo = self._make(tmp_path)
        files.directory = str(Path(photo.path).parent)
        host._on_move_requested(photo)
        assert host._catalog.called("rename_photo") == []

    def test_an_existing_destination_is_refused(self, host, tmp_path, files, box):
        photo = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.jpg").write_bytes(b"deja la")
        files.directory = str(dest)
        host._on_move_requested(photo)
        assert "a.jpg" in box.warnings[0][1]
        assert Path(photo.path).exists()

    def test_a_move_failure_is_reported(self, host, tmp_path, files, box, monkeypatch):
        photo = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        files.directory = str(dest)
        monkeypatch.setattr(mw.shutil, "move", _raise(OSError("refusé")))
        host._on_move_requested(photo)
        assert "refusé" in box.criticals[0][1]
        assert host._catalog.called("rename_photo") == []

    def test_the_viewer_navigates_to_the_neighbour(self, host, tmp_path, files, box):
        photo = self._make(tmp_path)
        other = _photo(str(tmp_path / "src" / "b.jpg"))
        dest = tmp_path / "dest"
        dest.mkdir()
        files.directory = str(dest)
        host._current_photos = [photo, other]
        host._current_paths = {photo.path, other.path}
        host._stack.index = 1
        host._viewer = _Panel(visible=True, current_photo=lambda: photo)
        host._viewer._canvas = _Rec()

        host._on_move_requested(photo)

        assert host._viewer.last("set_photo")[0] is other
        assert host._current_photo_index == 0

    def test_the_last_photo_moved_goes_back_to_the_grid(self, host, tmp_path, files, box):
        photo = self._make(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        files.directory = str(dest)
        host._current_photos = [photo]
        host._stack.index = 1
        host._viewer = _Panel(visible=True, current_photo=lambda: photo)
        host._viewer._canvas = _Rec()

        host._on_move_requested(photo)

        assert host._stack.currentIndex() == 0


class TestRemoveFromAlbum:
    """Del in an album context: the photo leaves the album, never the disk."""

    def test_nothing_without_a_selection(self, host):
        host._current_album_id = 3
        host._on_remove_from_album_requested([])
        assert host._catalog.called("remove_photos_from_album") == []

    def test_nothing_outside_an_album(self, host):
        host._on_remove_from_album_requested([_photo("/a.jpg", id=1)])
        assert host._catalog.called("remove_photos_from_album") == []

    def test_the_photos_leave_the_grid_and_the_album(self, host):
        a, b = _photo("/a.jpg", id=1), _photo("/b.jpg", id=2)
        host._current_album_id = 3
        host._current_photos = [a, b]
        host._current_paths = {a.path, b.path}
        host._on_remove_from_album_requested([a])
        assert host._catalog.last("remove_photos_from_album") == (3, [1])
        assert host._current_photos == [b]
        assert host._current_paths == {b.path}
        assert host._sidebar.called("refresh_albums")

    def test_a_photo_absent_from_the_catalog_is_skipped(self, host):
        a = _photo("/a.jpg", id=None)
        host._current_album_id = 3
        host._current_photos = [a]
        host._on_remove_from_album_requested([a])
        assert host._catalog.last("remove_photos_from_album") == (3, [])

    def test_the_grid_selects_the_neighbour(self, host):
        a, b = _photo("/a.jpg", id=1), _photo("/b.jpg", id=2)
        host._current_album_id = 3
        host._current_photos = [a, b]
        host._current_paths = {a.path, b.path}
        host._on_remove_from_album_requested([a])
        assert host._grid.last("select_photo") == (b.path,)
        assert host._grid.called("scroll_to_photo")

    def test_the_viewer_follows_onto_the_neighbour(self, host):
        a, b = _photo("/a.jpg", id=1), _photo("/b.jpg", id=2)
        host._current_album_id = 3
        host._current_photos = [a, b]
        host._current_paths = {a.path, b.path}
        host._stack.index = 1
        host._viewer = _Panel(visible=True, current_photo=lambda: a)
        host._viewer._canvas = _Rec()
        host._on_remove_from_album_requested([a])
        assert host._viewer.last("set_photo")[0] is b

    def test_an_emptied_album_goes_back_to_the_grid(self, host):
        a = _photo("/a.jpg", id=1)
        host._current_album_id = 3
        host._current_photos = [a]
        host._current_paths = {a.path}
        host._stack.index = 1
        host._viewer = _Panel(visible=True, current_photo=lambda: a)
        host._viewer._canvas = _Rec()
        host._on_remove_from_album_requested([a])
        assert host._stack.currentIndex() == 0


class TestCloseEvent:
    """Closing: persist the state, then stop the threads in parallel."""

    def test_the_geometry_and_the_view_are_persisted(self, host):
        host._current_context = "Favoris"
        host.closeEvent(QCloseEvent())
        saved = dict(host._config.sets)
        assert saved["ui.last_view"] == {"type": "favorites"}
        assert saved["ui.window_width"] == host.width()
        assert saved["ui.sidebar_width"] == host._left_stack.width()
        assert saved["ui.splitters.viewer"]        # base64 of the saved state

    def test_the_databases_are_closed(self, host):
        host.closeEvent(QCloseEvent())
        assert host._catalog.called("close") and host._face_db.called("close")

    def test_a_failure_when_closing_never_blocks_the_exit(self, host):
        host._catalog = _Rec(get_albums=lambda: [], close=_raise(RuntimeError("WAL")))
        host.closeEvent(QCloseEvent())          # no exception

    def test_the_window_is_hidden_before_the_waits(self, host):
        host.closeEvent(QCloseEvent())
        assert host.isHidden()

    def test_the_running_threads_are_stopped_then_waited_for(self, host):
        store: dict = {}
        for attr in ("_face_indexer", "_photo_query_thread", "_persons_refresh_thread",
                     "_dup_migration_thread", "_delete_thread"):
            thread = _fake_thread(store, attr)()
            thread.running = True
            setattr(host, attr, thread)

        host.closeEvent(QCloseEvent())

        assert host._scanner.called("request_stop")
        assert host._scanner.last("wait_stopped") == (3000,)
        assert host._face_indexer.stopped
        assert host._folder_watcher.last("set_folders") == ([],)

    def test_an_idle_thread_is_not_stopped(self, host):
        store: dict = {}
        host._face_indexer = _fake_thread(store, "idx")()   # running = False
        host.closeEvent(QCloseEvent())
        assert not host._face_indexer.stopped

    def test_a_grouping_in_progress_asks_before_closing(self, host, box):
        store: dict = {}
        host._cluster_thread = _fake_thread(store, "cluster")()
        host._cluster_thread.running = True
        host._cluster_start_time = time.monotonic() - 42
        box.answer = box.StandardButton.No

        event = QCloseEvent()
        host.closeEvent(event)

        assert not event.isAccepted()
        assert host._scanner.called("request_stop") == []   # nothing interrupted

    @pytest.mark.parametrize("elapsed, expected", [
        (42, "42s"), (125, "2min05s"), (3725, "1h02min05s"),
    ])
    def test_the_elapsed_time_is_shown_in_readable_form(self, host, box, elapsed, expected):
        store: dict = {}
        host._cluster_thread = _fake_thread(store, "cluster")()
        host._cluster_thread.running = True
        host._cluster_start_time = time.monotonic() - elapsed
        box.answer = box.StandardButton.Yes

        host.closeEvent(QCloseEvent())

        assert any(expected in t for t in box.instances[0].texts)

    def test_confirming_really_closes(self, host, box):
        store: dict = {}
        host._cluster_thread = _fake_thread(store, "cluster")()
        host._cluster_thread.running = True
        box.answer = box.StandardButton.Yes
        host.closeEvent(QCloseEvent())
        assert host._scanner.called("request_stop")

    def test_a_duplicate_thread_that_never_stops_kills_the_process(self, host, monkeypatch):
        """Documented last resort: a cv2/ORB call cannot be interrupted, and
        the whole useful state is already saved at that point."""
        exits: list = []
        monkeypatch.setattr(mw.os, "_exit", lambda code: exits.append(code))
        store: dict = {}
        host._duplicate_thread = _fake_thread(store, "dup")()
        host._duplicate_thread.running = True        # still running after wait()

        host.closeEvent(QCloseEvent())

        assert exits == [0]
        assert host._duplicate_thread.stopped        # cancel() requested first


class TestKeyPress:
    """The window shortcuts."""

    def _key(self, key, modifier=Qt.NoModifier):
        return QKeyEvent(QEvent.KeyPress, key, modifier)

    def test_f9_toggles_the_sidebar(self, host):
        host.keyPressEvent(self._key(Qt.Key_F9))
        assert host._left_stack.isVisible() is False
        host.keyPressEvent(self._key(Qt.Key_F9))
        assert host._left_stack.isVisible() is True

    def test_ctrl_a_selects_the_whole_grid(self, host):
        host.keyPressEvent(self._key(Qt.Key_A, Qt.ControlModifier))
        assert host._grid.called("select_all")

    def test_ctrl_a_selects_the_whole_cluster_view(self, host):
        host._stack.index = 2
        host._stack.current = host._person_cluster_view
        host.keyPressEvent(self._key(Qt.Key_A, Qt.ControlModifier))
        assert host._person_cluster_view.called("select_all")

    def test_ctrl_a_does_nothing_in_the_viewer(self, host):
        host._stack.index = 1
        host.keyPressEvent(self._key(Qt.Key_A, Qt.ControlModifier))
        assert host._grid.called("select_all") == []

    def test_the_arrows_navigate_in_the_viewer(self, host):
        host._current_photos = [_photo(f"/{i}.jpg") for i in range(3)]
        host._current_photo_index = 1
        host._stack.index = 1
        host.keyPressEvent(self._key(Qt.Key_Right))     # newer
        assert host._current_photo_index == 0
        host.keyPressEvent(self._key(Qt.Key_Left))      # older
        assert host._current_photo_index == 1

    def test_the_arrows_are_left_to_the_crop_tool(self, host):
        host._current_photos = [_photo(f"/{i}.jpg") for i in range(3)]
        host._current_photo_index = 1
        host._stack.index = 1
        host._viewer._canvas._crop_mode = True
        host.keyPressEvent(self._key(Qt.Key_Right))
        assert host._current_photo_index == 1

    def test_the_arrows_do_nothing_in_the_grid(self, host):
        host._current_photos = [_photo(f"/{i}.jpg") for i in range(3)]
        host._current_photo_index = 1
        host.keyPressEvent(self._key(Qt.Key_Right))
        assert host._current_photo_index == 1

    def test_an_unknown_key_is_passed_on(self, host):
        host.keyPressEvent(self._key(Qt.Key_Z))     # super(), no exception


class TestDeleteRequested:
    """Confirmation, then a single worker at a time (cf. _pending_deletes)."""

    @pytest.fixture
    def worker(self, monkeypatch):
        store: dict = {}
        monkeypatch.setattr(mw, "_DeleteWorkerThread", _fake_thread(store, "del"))
        return store

    def test_nothing_without_a_photo(self, host, worker):
        host._on_delete_requested([])
        assert worker == {}

    def test_the_confirmation_names_the_single_file(self, host, box, worker):
        host._on_delete_requested([_photo("/a.jpg")])
        assert "a.jpg" in box.instances[0].texts[-1]
        assert worker["del"][0].started

    def test_the_confirmation_counts_a_multiple_selection(self, host, box, worker):
        host._on_delete_requested([_photo(f"/{i}.jpg") for i in range(3)])
        assert "3" in box.instances[0].texts[-1]

    def test_a_refusal_deletes_nothing(self, host, box, worker):
        box.answer = box.StandardButton.Cancel
        host._on_delete_requested([_photo("/a.jpg")])
        assert worker == {}

    def test_do_not_ask_again_is_persisted(self, host, box, worker, monkeypatch):
        monkeypatch.setattr(mw, "QCheckBox", lambda label: _Rec(isChecked=lambda: True))
        host._on_delete_requested([_photo("/a.jpg")])
        assert host._config.get("ui.delete_no_confirm") is True

    def test_once_persisted_nothing_is_asked_any_more(self, host, box, worker):
        host._config.values["ui.delete_no_confirm"] = True
        host._on_delete_requested([_photo("/a.jpg")])
        assert box.instances == []
        assert worker["del"][0].started

    def test_a_second_deletion_is_queued_never_dropped(self, host, box, worker):
        host._config.values["ui.delete_no_confirm"] = True
        host._on_delete_requested([_photo("/a.jpg")])
        worker["del"][0].running = True
        host._on_delete_requested([_photo("/b.jpg")])
        assert len(worker["del"]) == 1                      # a single worker
        assert [p.path for p in host._pending_deletes[0]] == [_P("/b.jpg")]

    def test_the_watcher_is_warned_before_the_disk_is_touched(self, host, worker):
        host._config.values["ui.delete_no_confirm"] = True
        host._on_delete_requested([_photo("/a.jpg")])
        assert host._folder_watcher.last("notify_self_deletions") == ([_P("/a.jpg")],)

    def test_the_progress_is_shown_in_the_status_bar(self, host, worker):
        host._config.values["ui.delete_no_confirm"] = True
        host._on_delete_requested([_photo("/a.jpg")])
        worker["del"][0].progress.emit(1, 3)
        assert "1" in host._lbl_action.last("setText")[0]

    def test_the_epilogue_receives_the_state_from_before_the_deletion(self, host, worker):
        host._config.values["ui.delete_no_confirm"] = True
        a, b = _photo("/a.jpg"), _photo("/b.jpg")
        host._current_photos = [a, b]
        host._current_paths = {a.path, b.path}
        host._on_delete_requested([b])
        worker["del"][0].finished_delete.emit([b.path], [])
        assert host._current_photos == [a]


class TestDeleteFinished:
    """UI epilogue of a deletion: grid, duplicate groups, navigation."""

    def _run(self, host, deleted, errors=(), in_viewer=False, viewed_index=0,
             first_idx=None, groups=frozenset()):
        host._on_delete_finished(list(deleted), list(errors), in_viewer,
                                 viewed_index, first_idx, set(groups))

    def test_the_photos_leave_the_grid_and_the_view(self, host):
        a, b = _photo("/a.jpg"), _photo("/b.jpg")
        host._current_photos = [a, b]
        host._current_paths = {a.path, b.path}
        self._run(host, [a.path])
        assert host._current_photos == [b]
        assert host._current_paths == {b.path}
        assert host._grid.last("remove_photos") == ([a.path],)
        assert host._sidebar.called("refresh_albums")
        assert host._sidebar.called("refresh_tags")

    def test_nothing_deleted_leaves_the_grid_alone(self, host):
        self._run(host, [])
        assert host._grid.called("remove_photos") == []

    def test_the_errors_are_reported(self, host, box):
        self._run(host, [], ["a.jpg : accès refusé"])
        assert "accès refusé" in box.warnings[0][1]

    def test_a_group_reduced_to_one_copy_is_dissolved(self, host):
        a = _photo("/a.jpg", duplicate_group_id=7)
        b = _photo("/b.jpg", duplicate_group_id=7)
        host._current_photos = [a, b]
        host._catalog = _Rec(get_albums=lambda: [], get_all_tags=lambda: [],
                             get_duplicates_for_group=lambda gid: [b],
                             count_duplicate_groups=lambda: 0)
        self._run(host, [a.path], groups={7})
        assert host._catalog.last("ignore_duplicate_group") == (7,)
        assert host._duplicate_grid.last("remove_group") == (7,)
        assert b.duplicate_group_id is None
        assert host._duplicate_grid.called("invalidate")
        assert host._duplicate_ignored_paths == {b.path}

    def test_a_group_still_holding_two_copies_survives(self, host):
        a = _photo("/a.jpg", duplicate_group_id=7)
        host._current_photos = [a]
        host._catalog = _Rec(get_albums=lambda: [], get_all_tags=lambda: [],
                             get_duplicates_for_group=lambda gid: [1, 2])
        self._run(host, [a.path], groups={7})
        assert host._catalog.called("ignore_duplicate_group") == []
        assert host._duplicate_grid.called("invalidate") == []

    def test_the_grid_selects_the_neighbour(self, host):
        a, b = _photo("/a.jpg"), _photo("/b.jpg")
        host._current_photos = [a, b]
        self._run(host, [a.path], first_idx=0)
        assert host._grid.last("select_photo") == (b.path,)

    def test_the_viewer_follows_onto_the_neighbour(self, host):
        a, b = _photo("/a.jpg"), _photo("/b.jpg")
        host._current_photos = [a, b]
        host._viewer = _Panel(visible=True, current_photo=lambda: a)
        host._viewer._canvas = _Rec()
        self._run(host, [a.path], in_viewer=True)
        assert host._viewer.last("set_photo")[0] is b

    def test_the_last_photo_deleted_goes_back_to_the_grid(self, host):
        a = _photo("/a.jpg")
        host._current_photos = [a]
        host._stack.index = 1
        host._viewer = _Panel(visible=True, current_photo=lambda: a)
        host._viewer._canvas = _Rec()
        self._run(host, [a.path], in_viewer=True)
        assert host._stack.currentIndex() == 0

    def test_a_comparison_reduced_to_one_copy_returns_to_the_duplicates(self, host):
        """No point going on displaying the single surviving copy."""
        a, b = _photo("/a.jpg"), _photo("/b.jpg")
        host._current_photos = [a, b]
        host._viewer_back_target = "duplicate_grid"
        host._viewer = _Panel(visible=True, current_photo=lambda: a)
        host._viewer._canvas = _Rec()
        self._run(host, [a.path], in_viewer=True)
        assert host.log == ["duplicate_grid"]
        assert host._viewer_back_target == "grid"

    def test_the_queued_deletion_starts_right_after(self, host, monkeypatch):
        store: dict = {}
        monkeypatch.setattr(mw, "_DeleteWorkerThread", _fake_thread(store, "del"))
        host._pending_deletes = [[_photo("/b.jpg")]]
        self._run(host, [])
        assert store["del"][0].started
        assert host._pending_deletes == []


@pytest.fixture
def timers(monkeypatch):
    """QTimer.singleShot: the callbacks are collected instead of being armed —
    a real timer would fire long after the end of the test."""

    class _Timer:
        calls: list = []

        @staticmethod
        def singleShot(delay, callback):
            _Timer.calls.append((delay, callback))

    _Timer.calls = []
    monkeypatch.setattr(mw, "QTimer", _Timer)
    return _Timer


class TestRunExport:
    """Export of a selection to a folder (resizing + quality)."""

    def _jpeg(self, path, size=(40, 30)):
        from PIL import Image
        Image.new("RGB", size, (200, 30, 30)).save(str(path), format="JPEG")
        return _photo(str(path))

    @pytest.fixture(autouse=True)
    def no_explorer(self, monkeypatch):
        monkeypatch.setenv("PPM_SUPPRESS_EXPLORER", "1")

    def test_the_photo_is_written_as_jpeg_into_the_folder(self, host, tmp_path, timers):
        photo = self._jpeg(tmp_path / "a.jpg")
        out = tmp_path / "export"
        host._run_export([photo], out, None, 90)
        assert (out / "a.jpg").exists()
        assert "a.jpg" in host._lbl_action.called("setText")[0][1][0]

    def test_the_source_extension_is_always_replaced(self, host, tmp_path, timers):
        photo = self._jpeg(tmp_path / "a.png")
        out = tmp_path / "export"
        host._run_export([photo], out, None, 90)
        assert (out / "a.jpg").exists()

    def test_a_name_collision_never_overwrites(self, host, tmp_path, timers):
        photo = self._jpeg(tmp_path / "a.jpg")
        out = tmp_path / "export"
        out.mkdir()
        (out / "a.jpg").write_bytes(b"deja la")
        host._run_export([photo], out, None, 90)
        assert (out / "a.jpg").read_bytes() == b"deja la"
        assert (out / "a_1.jpg").exists()

    def test_the_image_is_reduced_to_the_requested_budget(self, host, tmp_path, timers):
        from PIL import Image
        photo = self._jpeg(tmp_path / "a.jpg", size=(400, 300))
        out = tmp_path / "export"
        host._run_export([photo], out, 4000, 90)
        with Image.open(out / "a.jpg") as img:
            # The scale factor is applied then rounded per side: the budget is
            # a target, not a strict ceiling (here 73x55 = 4015).
            assert img.size[0] * img.size[1] == pytest.approx(4000, rel=0.02)
            assert img.size[0] / img.size[1] == pytest.approx(400 / 300, rel=0.02)

    def test_a_small_enough_image_is_not_enlarged(self, host, tmp_path, timers):
        from PIL import Image
        photo = self._jpeg(tmp_path / "a.jpg", size=(40, 30))
        out = tmp_path / "export"
        host._run_export([photo], out, 10_000_000, 90)
        with Image.open(out / "a.jpg") as img:
            assert img.size == (40, 30)

    def test_an_impossible_folder_is_reported(self, host, tmp_path, box, timers):
        photo = self._jpeg(tmp_path / "a.jpg")
        blocker = tmp_path / "blocker"
        blocker.write_bytes(b"a file, not a folder")
        host._run_export([photo], blocker / "sub", None, 90)
        assert box.criticals

    def test_an_unreadable_photo_is_listed_not_fatal(self, host, tmp_path, box, timers):
        ok = self._jpeg(tmp_path / "a.jpg")
        broken = _photo(str(tmp_path / "b.jpg"))
        out = tmp_path / "export"
        host._run_export([ok, broken], out, None, 90)
        assert (out / "a.jpg").exists()
        assert "b.jpg" in box.warnings[0][1]

    def test_the_success_message_names_the_folder(self, host, tmp_path, timers):
        photo = self._jpeg(tmp_path / "a.jpg")
        out = tmp_path / "export"
        host._run_export([photo], out, None, 90)
        assert str(out) in host._lbl_action.last("setText")[0]
        timers.calls[0][1]()                       # the message clears itself
        assert host._lbl_action.last("setText") == ("",)

    def test_the_explorer_is_suppressed_under_the_e2e_harness(self, host, tmp_path,
                                                              timers, monkeypatch):
        opened: list = []
        monkeypatch.setattr(mw.os, "startfile", lambda p: opened.append(p))
        photo = self._jpeg(tmp_path / "a.jpg")
        host._run_export([photo], tmp_path / "export", None, 90)
        assert opened == []
        monkeypatch.delenv("PPM_SUPPRESS_EXPLORER")
        host._run_export([photo], tmp_path / "export2", None, 90)
        assert opened == [str(tmp_path / "export2")]


class TestExportClicked:
    """What the Export button exports: the viewer photo, or the selection."""

    @pytest.fixture
    def accepted(self, monkeypatch):
        monkeypatch.setattr(mw.QDialog, "exec", lambda self: mw.QDialog.Accepted)

    @pytest.fixture
    def exports(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(_Host, "_run_export",
                            lambda self, *a: calls.append(a))
        return calls

    def test_an_empty_selection_is_reported(self, host, box, exports):
        host._on_export_clicked()
        assert box.infos
        assert exports == []

    def test_the_grid_exports_its_selection(self, host, accepted, exports):
        photos = [_photo("/a.jpg")]
        host._grid = _Rec(get_selected=lambda: photos)
        host._on_export_clicked()
        assert exports[0][0] == photos

    def test_the_viewer_exports_the_displayed_photo(self, host, accepted, exports):
        photo = _photo("/a.jpg")
        host._stack.index = 1
        host._viewer._photo = photo
        host._on_export_clicked()
        assert exports[0][0] == [photo]

    def test_the_viewer_with_no_photo_exports_nothing(self, host, accepted, exports):
        host._stack.index = 1
        host._viewer._photo = None
        host._on_export_clicked()
        assert exports == []

    def test_a_cancelled_dialog_exports_nothing(self, host, monkeypatch, exports):
        monkeypatch.setattr(mw.QDialog, "exec", lambda self: mw.QDialog.Rejected)
        host._grid = _Rec(get_selected=lambda: [_photo("/a.jpg")])
        host._on_export_clicked()
        assert exports == []


class TestFolderRemoved:
    """Tools › Folders… — stop watching a folder (the files stay on disk)."""

    def test_an_empty_folder_is_removed_with_no_question(self, host, box):
        host._config.add_scan_folder(r"D:\Photos")
        host._catalog = _Rec(count_photos_in_folder=lambda f: 0)
        host._on_folder_removed(r"D:\Photos")
        assert box.infos == []
        assert host._config.get_scan_folders() == []
        assert host._folder_watcher.last("set_folders") == ([],)
        assert host._sidebar.called("refresh_folders")

    def test_the_confirmation_counts_the_affected_photos(self, host, box):
        host._catalog = _Rec(count_photos_in_folder=lambda f: 1234,
                             get_photos_in_folder=lambda f: [],
                             count_duplicate_groups=lambda: 0)
        host._on_folder_removed(r"D:\Photos")
        assert "1,234" in box.infos[0][1]

    def test_a_refusal_keeps_the_folder(self, host, box):
        host._config.add_scan_folder(r"D:\Photos")
        host._catalog = _Rec(count_photos_in_folder=lambda f: 5)
        box.answer = box.StandardButton.No
        host._on_folder_removed(r"D:\Photos")
        assert host._config.get_scan_folders() == [r"D:\Photos"]

    def test_the_catalog_is_purged_and_the_duplicates_invalidated(self, host, box):
        photo = _photo(r"D:\Photos\a.jpg")
        host._catalog = _Rec(count_photos_in_folder=lambda f: 1,
                             get_photos_in_folder=lambda f: [photo],
                             count_duplicate_groups=lambda: 0)
        host._current_photos = [photo]
        host._current_paths = {photo.path}
        host._on_folder_removed(r"D:\Photos")
        assert host._catalog.last("delete_photos") == ([photo.path],)
        assert host._thumb_cache.called("invalidate")
        assert host._face_db.called("delete_for_path")
        assert host._current_photos == []
        assert host._duplicate_grid.called("invalidate")


class TestPurgeCatalogForFolder:
    """Purging a folder covers its subfolders, and only them."""

    def test_the_subfolders_are_purged_too(self, host):
        inside = _photo(r"D:\Photos\2026\a.jpg")
        host._catalog = _Rec(get_photos_in_folder=lambda f: [inside])
        assert host._purge_catalog_for_folder(r"D:\Photos") == [inside.path]

    def test_a_photo_of_another_folder_is_left_alone(self, host):
        outside = _photo(r"D:\Autre\a.jpg")
        host._catalog = _Rec(get_photos_in_folder=lambda f: [outside])
        assert host._purge_catalog_for_folder(r"D:\Photos") == []
        assert host._catalog.called("delete_photos") == []

    def test_the_corrupted_files_of_the_folder_are_forgotten(self, host):
        inside = _photo(r"D:\Photos\a.jpg")
        host._catalog = _Rec(get_photos_in_folder=lambda f: [inside])
        host._live_corrupted_paths = [inside.path, r"D:\Autre\b.jpg"]
        host._purge_catalog_for_folder(r"D:\Photos")
        assert host.log == [("forget_corrupted", [inside.path])]
        assert host._live_corrupted_paths == [r"D:\Autre\b.jpg"]


class TestFolderDeleted:
    """Folder gone from the disk (watcher): clean up everything."""

    def test_the_photos_and_the_watched_folder_disappear(self, host):
        photo = _photo(r"D:\Photos\a.jpg")
        host._config.add_scan_folder(r"D:\Photos")
        host._catalog = _Rec(get_photos_in_folder=lambda f: [photo],
                             count_duplicate_groups=lambda: 0)
        host._on_folder_deleted(r"D:\Photos")
        assert host._config.get_scan_folders() == []
        assert host._folder_watcher.last("set_folders") == ([],)
        assert host._duplicate_grid.called("invalidate")

    def test_the_displayed_view_falls_back_on_an_empty_grid(self, host):
        photo = _photo(r"D:\Photos\a.jpg")
        host._catalog = _Rec(get_photos_in_folder=lambda f: [photo],
                             count_duplicate_groups=lambda: 0)
        host._current_context = r"D:\Photos\2026"
        host._on_folder_deleted(r"D:\Photos")
        assert host._current_context == ""
        assert host._grid.last("set_photos") == ([],)

    def test_another_displayed_view_only_loses_the_deleted_photos(self, host):
        photo = _photo(r"D:\Photos\a.jpg")
        host._catalog = _Rec(get_photos_in_folder=lambda f: [photo],
                             count_duplicate_groups=lambda: 0)
        host._current_context = "Favoris"
        host._on_folder_deleted(r"D:\Photos")
        assert host._current_context == "Favoris"
        assert host._grid.last("remove_photos") == ([photo.path],)


class TestFolderMoved:
    """Folder renamed on disk: every path follows, with no rescan."""

    def test_the_databases_are_updated_by_prefix(self, host):
        host._on_folder_moved(r"D:\Photos", r"D:\Images")
        assert host._catalog.last("update_paths_prefix") == (r"D:\Photos", r"D:\Images")
        assert host._face_db.last("update_paths_prefix") == (r"D:\Photos", r"D:\Images")

    def test_the_watched_folder_follows(self, host):
        host._config.add_scan_folder(r"D:\Photos")
        host._on_folder_moved(r"D:\Photos", r"D:\Images")
        assert host._config.get_scan_folders() == [r"D:\Images"]
        assert host._folder_watcher.last("set_folders") == ([r"D:\Images"],)

    def test_a_watched_subfolder_follows_too(self, host):
        host._config.add_scan_folder(r"D:\Photos\2026")
        host._on_folder_moved(r"D:\Photos", r"D:\Images")
        assert host._config.get_scan_folders() == [r"D:\Images\2026"]

    def test_an_unrelated_watched_folder_is_left_alone(self, host):
        host._config.add_scan_folder(r"D:\Autre")
        host._on_folder_moved(r"D:\Photos", r"D:\Images")
        assert host._config.get_scan_folders() == [r"D:\Autre"]

    def test_the_photos_in_memory_follow(self, host):
        photo = _photo(r"D:\Photos\2026\a.jpg")
        host._current_photos = [photo]
        host._on_folder_moved(r"D:\Photos", r"D:\Images")
        assert photo.path == r"D:\Images\2026\a.jpg"
        assert photo.directory == r"D:\Images\2026"
        assert host._grid.called("set_photos")

    def test_the_displayed_context_follows(self, host):
        host._current_context = r"D:\Photos\2026"
        host._on_folder_moved(r"D:\Photos", r"D:\Images")
        assert host._current_context == r"D:\Images\2026"

    def test_another_context_is_left_alone(self, host):
        host._current_context = "Favoris"
        host._on_folder_moved(r"D:\Photos", r"D:\Images")
        assert host._current_context == "Favoris"


class TestAlbumsAndTags:
    """Creating/deleting an album, deleting a keyword."""

    def test_an_album_is_created_then_the_sidebar_refreshed(self, host):
        host._on_album_create("Vacances")
        assert host._catalog.last("create_album") == ("Vacances",)
        assert host._sidebar.called("refresh_albums")

    def test_deleting_an_album_is_confirmed(self, host, box):
        box.answer = box.StandardButton.No
        host._on_album_delete_requested(AlbumInfo(id=3, name="Vacances", photo_count=12))
        assert "Vacances" in box.infos[0][1] and "12" in box.infos[0][1]
        assert host._catalog.called("delete_album") == []

    def test_a_confirmed_album_is_deleted(self, host, box):
        host._on_album_delete_requested(AlbumInfo(id=3, name="Vacances"))
        assert host._catalog.last("delete_album") == (3,)
        assert host.log == []          # another album was displayed: no fallback

    def test_deleting_the_displayed_album_falls_back_on_all_photos(self, host, box):
        host._current_context = "Vacances"
        host._on_album_delete_requested(AlbumInfo(id=3, name="Vacances"))
        assert host.log == ["show_all_photos"]
        assert host._sidebar.called("select_album_item")

    def test_deleting_a_keyword_is_confirmed(self, host, box):
        host._catalog = _Rec(get_photos_by_tag=lambda t: [_photo("/a.jpg", id=1)])
        box.answer = box.StandardButton.No
        host._on_tag_delete_requested("plage")
        assert "plage" in box.infos[0][1]
        assert host._catalog.called("remove_tag_from_photos") == []

    def test_a_confirmed_keyword_leaves_every_photo(self, host, box):
        photo = _photo("/a.jpg", id=1, tags=["plage", "mer"])
        host._catalog = _Rec(get_photos_by_tag=lambda t: [photo],
                             get_all_tags=lambda: ["mer"])
        host._on_tag_delete_requested("plage")
        assert host._catalog.last("remove_tag_from_photos") == ([1], "plage")
        assert photo.tags == ["mer"]                 # the in-memory copy follows
        assert host._sidebar.last("refresh_tags") == (["mer"],)
        assert host._viewer.last("set_available_tags") == (["mer"],)

    def test_deleting_the_displayed_keyword_falls_back_on_all_photos(self, host, box):
        host._current_context = "Mot-clé : plage"
        host._catalog = _Rec(get_photos_by_tag=lambda t: [], get_all_tags=lambda: [])
        host._on_tag_delete_requested("plage")
        assert host.log == ["show_all_photos"]

    def test_the_displayed_photo_loses_the_keyword_live(self, host, box):
        photo = _photo("/a.jpg", id=1, tags=["plage", "mer"])
        host._catalog = _Rec(get_photos_by_tag=lambda t: [], get_all_tags=lambda: [])
        host._viewer = _Panel(visible=True, current_photo=lambda: photo)
        host._exif_panel.show()
        host._on_tag_delete_requested("plage")
        assert photo.tags == ["mer"]
        assert host._viewer.called("refresh_tags")
        assert host._exif_panel.last("set_tags") == (["mer"],)


class TestAddToAlbum:
    """Adding the selection to an album (grid context menu).

    The real QDialog is built -- only exec() is short-circuited: that is what
    checks the list really carries the albums and the preselected row."""

    @pytest.fixture
    def accepted(self, monkeypatch):
        monkeypatch.setattr(mw.QDialog, "exec", lambda self: mw.QDialog.Accepted)

    def test_with_no_album_the_user_is_told(self, host, box):
        host._on_add_to_album([_photo("/a.jpg", id=1)])
        assert box.infos
        assert host._catalog.called("add_photos_to_album") == []

    def test_the_first_album_is_preselected(self, host, accepted):
        albums = [AlbumInfo(id=3, name="Vacances"), AlbumInfo(id=4, name="Noel")]
        host._catalog = _Rec(get_albums=lambda: albums,
                             add_photos_to_album=lambda aid, ids: len(ids))
        host._on_add_to_album([_photo("/a.jpg", id=1), _photo("/b.jpg", id=None)])
        assert host._catalog.last("add_photos_to_album") == (3, [1])
        assert host._sidebar.called("refresh_albums")

    def test_a_cancelled_dialog_adds_nothing(self, host, monkeypatch):
        monkeypatch.setattr(mw.QDialog, "exec", lambda self: mw.QDialog.Rejected)
        host._catalog = _Rec(get_albums=lambda: [AlbumInfo(id=3, name="Vacances")])
        host._on_add_to_album([_photo("/a.jpg", id=1)])
        assert host._catalog.called("add_photos_to_album") == []

    def test_a_new_album_is_created_with_the_selection(self, host, inputs):
        inputs.text = ("  Vacances  ", True)
        host._catalog = _Rec(get_albums=lambda: [],
                             create_album=lambda name: AlbumInfo(id=9, name=name),
                             add_photos_to_album=lambda aid, ids: len(ids))
        host._on_create_album_with([_photo("/a.jpg", id=1)])
        assert host._catalog.last("create_album") == ("Vacances",)   # trimmed
        assert host._catalog.last("add_photos_to_album") == (9, [1])

    @pytest.mark.parametrize("answer", [("", True), ("   ", True), ("Vacances", False)])
    def test_no_name_creates_no_album(self, host, inputs, answer):
        inputs.text = answer
        host._on_create_album_with([_photo("/a.jpg", id=1)])
        assert host._catalog.called("create_album") == []


class TestSmallSlots:
    """The one-line slots -- each of them is a wire that can be crossed."""

    def test_a_favourite_is_persisted(self, host):
        host._on_favorite_toggle_requested(_photo("/a.jpg", id=4, is_favorite=True))
        assert host._catalog.last("set_favorite") == (4, True)

    def test_a_photo_absent_from_the_catalogue_is_not_persisted(self, host):
        host._on_favorite_toggle_requested(_photo("/a.jpg", id=None))
        assert host._catalog.called("set_favorite") == []

    def test_a_rating_covers_the_whole_selection(self, host):
        photos = [_photo("/a.jpg", id=1), _photo("/b.jpg", id=2)]
        host._on_rating_change_requested(photos, 4)
        assert host._catalog.last("set_rating_for_ids") == ([1, 2], 4)
        assert host._grid.last("refresh_rating") == (
            {_P("/a.jpg"): 4, _P("/b.jpg"): 4},)

    def test_a_rating_on_photos_outside_the_catalogue_still_refreshes_the_badges(self, host):
        host._on_rating_change_requested([_photo("/a.jpg", id=None)], 2)
        assert host._catalog.called("set_rating_for_ids") == []
        assert host._grid.last("refresh_rating") == ({_P("/a.jpg"): 2},)

    def test_the_thumbnail_size_is_persisted(self, host):
        host._on_thumb_size_changed(2)
        assert host._grid.last("set_thumbnail_size") == (mw._THUMB_SIZES[2],)
        assert host._config.get("thumbnail_size") == mw._THUMB_SIZES[2]

    def test_a_scan_removal_is_reflected_in_the_grid(self, host):
        a, b = _photo("/a.jpg"), _photo("/b.jpg")
        host._current_photos = [a, b]
        host._current_paths = {a.path, b.path}
        host._on_photos_removed([a.path])
        assert host._current_photos == [b]
        assert host._current_paths == {b.path}
        assert host._scan_had_removals is True
        assert host._grid.last("remove_photos") == ([a.path],)
        assert host._face_db.last("delete_for_paths") == ([a.path],)

    def test_a_scan_batch_only_shows_the_photos_of_the_displayed_folder(self, host):
        host._current_context = r"D:\Photos"
        inside, outside = _photo(r"D:\Photos\a.jpg"), _photo(r"D:\Autre\b.jpg")
        host._on_photos_batch([inside, outside])
        assert host._current_photos == [inside]
        assert host._grid.called("set_photos")

    def test_a_scan_batch_never_duplicates_a_known_photo(self, host):
        known = _photo(r"D:\Photos\a.jpg")
        host._current_context = r"D:\Photos"
        host._current_photos, host._current_paths = [known], {known.path}
        host._on_photos_batch([_photo(r"D:\Photos\a.jpg")])
        assert host._current_photos == [known]
        assert host._grid.called("set_photos") == []      # nothing new: no redraw

    def test_the_chronological_view_takes_every_batch(self, host):
        host._on_photos_batch([_photo(r"D:\Autre\b.jpg")])
        assert len(host._current_photos) == 1

    def test_a_catalogue_batch_out_of_context_is_dropped(self, host):
        host._current_context = "Favoris"
        host._on_catalog_batch([_photo("/a.jpg")])
        assert host._current_photos == []

    def test_a_catalogue_batch_feeds_the_grid_incrementally(self, host):
        host._on_catalog_batch([_photo("/a.jpg")])
        assert host._grid.called("add_photos_batch")
        assert host._current_paths == {_P("/a.jpg")}

    def test_a_new_folder_on_disk_is_scanned(self, host):
        host._on_folder_created(r"D:\Photos\2026")
        assert host.scans == [([r"D:\Photos\2026"], False)]
        assert host._sidebar.called("refresh_folders")

    def test_a_watcher_change_triggers_a_scan(self, host):
        host._on_watcher_files_changed(r"D:\Photos")
        assert host.scans == [([r"D:\Photos"], False)]

    def test_the_menu_entry_scans_one_folder(self, host):
        host._on_scan_requested(r"D:\Photos")
        assert host.scans == [([r"D:\Photos"], False)]


class TestShowGridAndViewer:
    """Switching between the grid and the viewer: two toolbars in one."""

    def test_the_grid_shows_the_thumbnail_slider_not_the_zoom(self, host):
        host.show_grid()
        assert host._stack.index == 0 and host._left_stack.index == 0
        assert host._lbl_thumb_size.called("show") and host._zoom_slider.called("hide")
        assert host._act_faces_toggle.last("setVisible") == (False,)
        # cleared, then rewritten right after by the _update_status() of show_grid
        assert host._lbl_fileinfo.called("setText")[0][1] == ("",)

    def test_the_viewer_shows_the_zoom_and_the_panel_toggles(self, host):
        host.show_viewer(_photo("/a.jpg"))
        assert host._stack.index == 1
        assert host._thumb_slider.called("hide") and host._zoom_slider.called("show")
        assert host._act_exif_toggle.last("setVisible") == (True,)
        assert host._viewer.called("set_photo")

    def test_a_photo_opens_the_edit_panel(self, host):
        host.show_viewer(_photo("/a.jpg"))
        assert host._left_stack.index == 1
        assert host._edit_panel.called("set_photo")

    def test_a_video_keeps_the_sidebar_and_skips_the_edit_panel(self, host):
        host.show_viewer(_photo("/a.mp4", media_type="video"))
        assert host._left_stack.index == 0
        assert host._edit_panel.called("set_photo") == []

    def test_the_open_panels_follow_the_displayed_photo(self, host):
        host._face_panel.show()
        host._exif_panel.show()
        photo = _photo("/a.jpg", tags=["mer"])
        host.show_viewer(photo)
        assert host._face_panel.last("set_photo") == (photo.path,)
        assert host._exif_panel.last("set_tags") == (["mer"],)

    def test_the_annotation_state_of_the_session_is_reapplied(self, host):
        host._annotations_globally_visible = False
        host.show_viewer(_photo("/a.jpg"))
        assert host._viewer.last("set_annotations_visible") == (False,)

    def test_the_sidebar_is_toggled(self, host):
        host.toggle_sidebar()
        assert host._left_stack.visible is False
        host.toggle_sidebar()
        assert host._left_stack.visible is True


class TestBackNavigation:
    """Where the back arrow of the viewer, and the one of the grid, land."""

    def test_the_viewer_goes_back_to_the_grid_and_locates_the_photo(self, host):
        photo = _photo("/a.jpg")
        host._viewer = _Panel(visible=True, current_photo=lambda: photo)
        host._on_viewer_closed()
        assert host._stack.index == 0
        assert host._grid.last("scroll_to_photo") == (photo.path,)
        assert host._grid.last("select_photo") == (photo.path,)

    def test_the_viewer_goes_back_to_the_group_of_the_person(self, host):
        person = object()
        host._viewer_back_target = "person_cluster_view"
        host._person_cluster_view.current_person = person
        host._on_viewer_closed()
        assert host.log == [("person_clusters", person)]

    def test_without_a_person_it_falls_back_on_the_grid(self, host):
        host._viewer_back_target = "person_cluster_view"
        host._on_viewer_closed()
        assert host.log == [] and host._stack.index == 0

    def test_the_viewer_goes_back_to_the_duplicates_grid(self, host):
        host._viewer_back_target = "duplicate_grid"
        host._on_viewer_closed()
        assert host.log == ["duplicate_grid"]

    def test_the_target_is_consumed_by_the_first_return(self, host):
        host._viewer_back_target = "duplicate_grid"
        host._on_viewer_closed()
        host._on_viewer_closed()
        assert host.log == ["duplicate_grid"]      # the 2nd went back to the grid

    def test_the_grid_arrow_goes_back_to_the_group_of_the_person(self, host):
        person = object()
        host._from_person_cluster_view = True
        host._person_cluster_view.current_person = person
        host._on_back_nav_clicked()
        assert host.log == [("person_clusters", person)]
        assert host._from_person_cluster_view is False

    def test_the_photos_of_a_group_go_back_to_the_groups(self, host):
        host._current_context = "__person__cluster_7"
        host._on_back_nav_clicked()
        assert host.log == ["face_clusters"]

    def test_anywhere_else_the_arrow_goes_back_to_the_grid(self, host):
        host._current_context = "Favoris"
        host._on_back_nav_clicked()
        assert host.log == [] and host._stack.index == 0
        assert host._grid_nav_bar.called("hide")


class TestShowAllPhotos:
    """The "All the photos" view: an incremental load in the background."""

    @pytest.fixture
    def loader(self, monkeypatch):
        threads: dict = {}
        monkeypatch.setattr(mw, "_CatalogLoadThread", _fake_thread(threads, "load"))
        return threads

    def test_the_view_is_emptied_then_the_load_started(self, host, loader):
        host._current_photos = [_photo("/old.jpg")]
        host._current_paths = {_P("/old.jpg")}
        MainWindow._show_all_photos(host)          # the host neutralises the method
        assert host._current_photos == [] and host._current_paths == set()
        assert host._current_context == "Toutes les photos"
        assert host._current_album_id is None
        assert loader["load"][0].started is True
        assert host._catalog_loader is loader["load"][0]

    def test_the_ribbon_and_the_dates_are_turned_on(self, host, loader):
        MainWindow._show_all_photos(host)
        assert host._grid.last("set_ribbon_mode") == (True,)
        assert host._grid.last("set_date_overlay_visible") == (True,)
        assert host._grid_nav_bar.called("hide")
        assert host._stack.index == 0

    @pytest.mark.parametrize("direction,reverse", [("asc", True), ("desc", False)])
    def test_the_chronological_direction_is_the_one_of_the_setting(
            self, host, loader, direction, reverse):
        host._config.set("display_order.chrono_album_dir", direction)
        MainWindow._show_all_photos(host)
        assert loader["load"][0].kwargs["reverse"] is reverse

    def test_a_load_still_in_flight_is_cancelled_first(self, host, loader):
        previous = _fake_thread({}, "x")()
        previous.running = True
        host._catalog_loader = previous
        MainWindow._show_all_photos(host)
        assert previous.stopped is True
        assert host._catalog_loader is loader["load"][0]


class TestCancelGridDisplayOps:
    """A query that answers late must never overwrite the current view."""

    def test_a_finished_loader_is_destroyed_straight_away(self, host):
        loader = _fake_thread({}, "x")()
        host._catalog_loader = loader
        host._cancel_grid_display_ops()
        assert loader.stopped is True
        assert host._catalog_loader is None

    def test_a_running_loader_is_disconnected_before_being_destroyed(self, host):
        loader = _fake_thread({}, "x")()
        loader.running = True
        loader.batch_ready.connect(lambda batch: None)
        host._catalog_loader = loader
        host._cancel_grid_display_ops()
        assert loader.batch_ready.slots == []       # its results are ignored
        assert loader.finished.slots                # destroyed when it ends

    def test_a_running_query_is_disconnected_too(self, host):
        query = _fake_thread({}, "x")()
        query.running = True
        query.photos_ready.connect(lambda photos: None)
        host._photo_query_thread = query
        host._cancel_grid_display_ops()
        assert query.photos_ready.slots == []
        assert host._photo_query_thread is None

    def test_with_nothing_in_flight_it_does_nothing(self, host):
        host._cancel_grid_display_ops()
        assert host._catalog_loader is None and host._photo_query_thread is None


class TestDisplayOrderAndSplitters:
    """The settings reapplied to an already displayed interface."""

    def test_the_display_order_reaches_the_tree_and_the_grid(self, host):
        host._config.set("display_order.folder_mode", "date")
        host._config.set("display_order.folder_dir", "desc")
        host._current_photos = [_photo("/a.jpg")]
        host._apply_display_order()
        assert host._sidebar.last("set_folder_order") == ("date", "desc")
        assert host._grid.called("set_photos")

    def test_an_empty_grid_is_not_redrawn(self, host):
        host._apply_display_order()
        assert host._grid.called("set_photos") == []

    def test_the_alphabetical_order_of_a_folder_is_applied(self, host):
        host._config.set("display_order.grid_mode", "name")
        host._config.set("display_order.grid_dir", "asc")
        host._current_context = r"D:\Photos"
        host._current_photos = [_photo(r"D:\Photos\b.jpg"), _photo(r"D:\Photos\a.jpg")]
        host._apply_display_order()
        assert [p.filename for p in host._current_photos] == ["a.jpg", "b.jpg"]

    def test_a_saved_splitter_is_restored(self, host):
        import base64
        host._config.set("ui.splitters.viewer", base64.b64encode(b"state").decode())
        host._config.set("ui.splitters.sidebar_panels", "abcd")
        host._config.set("ui.persons_list_selected_id", "12")
        host._restore_splitter_states()
        assert host._viewer_splitter.called("restoreState")
        assert host._sidebar.last("restore_splitter_state") == ("abcd",)
        assert host._sidebar.last("set_pending_person_id") == (12,)

    def test_nothing_saved_restores_nothing(self, host):
        host._restore_splitter_states()
        assert host._viewer_splitter.called("restoreState") == []
        assert host._sidebar.called("set_pending_person_id") == []

    def test_a_corrupted_state_is_ignored_rather_than_crashing(self, host):
        host._config.set("ui.splitters.viewer", "not-base64!!")
        host._restore_splitter_states()      # must not raise
        assert host._viewer_splitter.called("restoreState") == []


class TestOpenFolderDialog:
    """Adding a folder to the library."""

    def test_the_chosen_folder_is_watched_then_scanned(self, host, files):
        files.directory = r"D:\Photos"
        host.open_folder_dialog()
        assert host._config.added == [r"D:\Photos"]
        assert host.scans == [([r"D:\Photos"], False)]
        assert host._sidebar.called("refresh_folders")
        assert host._folder_watcher.last("set_folders") == (host._config.folders,)

    def test_a_cancelled_dialog_changes_nothing(self, host, files):
        host.open_folder_dialog()
        assert host._config.added == [] and host.scans == []


class TestNavigateToPhotoPath:
    """Jumping to a photo from elsewhere (map, duplicates, faces)."""

    def test_the_folder_of_the_photo_is_displayed_then_the_photo_located(
            self, host, timers):
        photo = _photo(r"D:\Photos\a.jpg")
        host._catalog = _Rec(get_photos_by_paths=lambda paths: [photo],
                             get_photos_in_folder=lambda folder: [photo])
        host._navigate_to_photo_path(photo.path)
        assert host.queries == [(r"D:\Photos", None, r"D:\Photos", [photo])]
        delay, callback = timers.calls[0]
        assert delay > 0                      # after the grid has been filled
        callback()
        assert host._grid.last("scroll_to_photo") == (photo.path,)
        assert host._grid.last("select_photo") == (photo.path,)

    def test_a_photo_gone_from_the_library_warns_the_user(self, host, box, timers):
        host._catalog = _Rec(get_photos_by_paths=lambda paths: [])
        host._navigate_to_photo_path(r"D:\Photos\a.jpg")
        assert host._catalog.called("get_photos_in_folder") == []
        assert timers.calls == []
        assert r"D:\Photos\a.jpg" in box.warnings[0][1]


class TestSaveRequested:
    """Apply on the edit panel: overwrite, or save under another name."""

    @pytest.fixture
    def save_dlg(self, monkeypatch):
        """_SaveOptionsDialog stand-in — the test decides what the user chose."""

        class _Dlg:
            accepted = True
            overwrite = True
            backup_before_overwrite = False

            def __init__(self, path, parent=None):
                _Dlg.path = path

            def exec(self):
                return mw.QDialog.Accepted if _Dlg.accepted else mw.QDialog.Rejected

        _Dlg.accepted, _Dlg.overwrite, _Dlg.backup_before_overwrite = True, True, False
        monkeypatch.setattr(mw, "_SaveOptionsDialog", _Dlg)
        return _Dlg

    @pytest.fixture
    def exported(self, host, monkeypatch):
        """_export_image is covered by its own tests: only the destination matters here."""
        calls: list = []
        monkeypatch.setattr(type(host), "_export_image",
                            lambda self, photo, dest: calls.append((photo, dest)))
        return calls

    def test_a_cancelled_dialog_saves_nothing(self, host, save_dlg, exported):
        save_dlg.accepted = False
        host._on_save_requested(_photo("/a.jpg"))
        assert exported == []

    def test_overwriting_targets_the_original_file(self, host, save_dlg, exported):
        photo = _photo("/a.jpg")
        host._on_save_requested(photo)
        assert exported == [(photo, photo.path)]

    def test_the_original_is_backed_up_before_being_overwritten(
            self, host, save_dlg, exported, tmp_path, monkeypatch):
        save_dlg.backup_before_overwrite = True
        src = tmp_path / "a.jpg"
        src.write_bytes(b"original")
        host._on_save_requested(_photo(str(src)))
        backups = list((tmp_path / ".tmp_originals").glob("a_*.jpg"))
        assert len(backups) == 1 and backups[0].read_bytes() == b"original"
        assert exported[0][1] == str(src)

    def test_a_failed_backup_asks_before_overwriting(
            self, host, save_dlg, exported, box, monkeypatch):
        save_dlg.backup_before_overwrite = True
        monkeypatch.setattr(type(host), "_backup_original", _raise(OSError("disque plein")))
        box.answer = box.StandardButton.Cancel
        host._on_save_requested(_photo("/a.jpg"))
        assert exported == []                      # the original is intact
        assert "disque plein" in box.warnings[0][1]

    def test_a_failed_backup_confirmed_still_overwrites(
            self, host, save_dlg, exported, box, monkeypatch):
        save_dlg.backup_before_overwrite = True
        monkeypatch.setattr(type(host), "_backup_original", _raise(OSError("disque plein")))
        box.answer = box.StandardButton.Yes
        host._on_save_requested(_photo("/a.jpg"))
        assert exported[0][1] == _P("/a.jpg")

    def test_a_copy_suggests_the_edited_name(self, host, save_dlg, exported, monkeypatch):
        save_dlg.overwrite = False
        seen: list = []

        class _Files:
            @staticmethod
            def getSaveFileName(parent, title, suggested, filters):
                seen.append(suggested)
                return r"D:\out.jpg", ""

        monkeypatch.setattr(mw, "QFileDialog", _Files)
        host._on_save_requested(_photo(r"D:\Photos\a.jpg"))
        assert "a_edited.jpg" in seen[0]
        assert exported[0][1] == r"D:\out.jpg"

    def test_a_cancelled_save_as_exports_nothing(
            self, host, save_dlg, exported, monkeypatch):
        save_dlg.overwrite = False
        monkeypatch.setattr(mw, "QFileDialog",
                            _Rec(getSaveFileName=lambda *a: ("", "")))
        host._on_save_requested(_photo("/a.jpg"))
        assert exported == []


class TestExportImage:
    """Writing the processed image — real files, so the pixels are checked."""

    @pytest.fixture
    def jpeg(self, tmp_path):
        from PIL import Image
        path = tmp_path / "a.jpg"
        Image.new("RGB", (40, 30), (200, 100, 50)).save(path)
        return path

    def test_an_unedited_photo_is_copied_as_is(self, host, jpeg, tmp_path, timers):
        from PIL import Image
        dest = tmp_path / "out.jpg"
        host._export_image(_photo(str(jpeg)), str(dest))
        with Image.open(dest) as img:
            assert img.size == (40, 30)
        assert "out.jpg" in host._lbl_action.last("setText")[0]

    def test_the_png_extension_decides_the_format(self, host, jpeg, tmp_path, timers):
        from PIL import Image
        dest = tmp_path / "out.png"
        host._export_image(_photo(str(jpeg)), str(dest))
        with Image.open(dest) as img:
            assert img.format == "PNG"

    def test_the_edits_are_baked_into_the_exported_file(
            self, host, jpeg, tmp_path, timers):
        from PIL import Image
        host._edit_db = _Rec(load=lambda p: EditInfo(brightness=0.5))
        dest = tmp_path / "out.jpg"
        host._export_image(_photo(str(jpeg)), str(dest))
        with Image.open(jpeg) as before, Image.open(dest) as after:
            assert after.getpixel((0, 0)) != before.getpixel((0, 0))

    def test_the_frame_enlarges_the_exported_image(self, host, jpeg, tmp_path, timers):
        from PIL import Image
        host._edit_db = _Rec(load=lambda p: EditInfo(frame_type="plain"))
        dest = tmp_path / "out.jpg"
        host._export_image(_photo(str(jpeg)), str(dest))
        with Image.open(dest) as img:
            assert img.size[0] > 40 and img.size[1] > 30

    def test_the_dates_of_the_original_are_preserved(self, host, jpeg, tmp_path, timers):
        dest = tmp_path / "out.jpg"
        host._export_image(_photo(str(jpeg)), str(dest))
        assert os.stat(dest).st_mtime == pytest.approx(os.stat(jpeg).st_mtime, abs=2)

    def test_overwriting_bakes_the_edits_and_forgets_them(
            self, host, jpeg, timers):
        host._edit_db = _Rec(load=lambda p: EditInfo(brightness=0.5))
        photo = _photo(str(jpeg))
        host._export_image(photo, str(jpeg))
        assert host._edit_db.last("delete") == (photo.path,)
        assert host._thumb_cache.last("invalidate") == (photo.path,)
        assert host._viewer.last("invalidate_base_cache") == (photo.path,)
        assert host._viewer.last("update_edit") == (EditInfo(),)
        assert host._edit_panel.called("set_photo")

    def test_a_copy_keeps_the_edits_of_the_original(self, host, jpeg, tmp_path, timers):
        host._edit_db = _Rec(load=lambda p: EditInfo(brightness=0.5))
        host._export_image(_photo(str(jpeg)), str(tmp_path / "out.jpg"))
        assert host._edit_db.called("delete") == []

    def test_an_unreadable_file_reports_the_error(self, host, tmp_path, box, timers):
        host._export_image(_photo(str(tmp_path / "ghost.jpg")), str(tmp_path / "o.jpg"))
        assert box.criticals
        assert host._lbl_action.called("setText") == []

    def test_the_message_is_cleared_by_a_timer(self, host, jpeg, tmp_path, timers):
        host._export_image(_photo(str(jpeg)), str(tmp_path / "out.jpg"))
        delay, callback = timers.calls[-1]
        callback()
        assert host._lbl_action.last("setText") == ("",)


class TestRemapFaceBboxes:
    """After an overwriting save, the geometry is baked into the pixels: the
    stored face bboxes no longer point at the right place."""

    def _face(self, **kw):
        from src.core.models import FaceInfo
        base = dict(id=1, bbox_x=10, bbox_y=10, bbox_w=20, bbox_h=20)
        base.update(kw)
        return FaceInfo(**base)

    def test_an_edit_without_geometry_changes_nothing(self, host):
        host._face_db = _Rec(get_faces_for_photo=lambda p: [self._face()])
        host._remap_face_bboxes_after_save("/a.jpg", EditInfo(brightness=0.5), 100, 80)
        assert host._face_db.called("get_faces_for_photo") == []

    def test_a_photo_without_a_face_writes_nothing(self, host):
        host._face_db = _Rec(get_faces_for_photo=lambda p: [])
        host._remap_face_bboxes_after_save("/a.jpg", EditInfo(rotation=90), 100, 80)
        assert host._face_db.called("remap_bboxes_after_save") == []

    def test_a_rotation_moves_the_boxes(self, host):
        host._face_db = _Rec(get_faces_for_photo=lambda p: [self._face()])
        host._remap_face_bboxes_after_save("/a.jpg", EditInfo(rotation=90), 100, 80)
        path, updates, deletions = host._face_db.last("remap_bboxes_after_save")
        assert deletions == [] and updates[1] != (10, 10, 20, 20)

    def test_a_crop_excluding_the_face_purges_it(self, host):
        host._face_db = _Rec(get_faces_for_photo=lambda p: [self._face()])
        host._remap_face_bboxes_after_save(
            "/a.jpg", EditInfo(crop=(0.8, 0.8, 0.2, 0.2)), 100, 80)
        _path, updates, deletions = host._face_db.last("remap_bboxes_after_save")
        assert deletions == [1] and updates == {}

    def test_the_faces_are_grouped_by_detection_orientation(self, host):
        faces = [self._face(id=1, detected_rotation=0),
                 self._face(id=2, detected_rotation=90)]
        host._face_db = _Rec(get_faces_for_photo=lambda p: faces)
        host._remap_face_bboxes_after_save("/a.jpg", EditInfo(flip_h=True), 100, 80)
        _path, updates, _deletions = host._face_db.last("remap_bboxes_after_save")
        # a mirror in a 100x80 frame and in the 80x100 frame of the rotated
        # detection do not give the same abscissa
        assert updates[1] != updates[2]


class TestExifSaved:
    """An EXIF rewrite may have changed the shooting date."""

    @pytest.fixture
    def dated_jpeg(self, tmp_path):
        from PIL import Image
        path = tmp_path / "a.jpg"
        img = Image.new("RGB", (8, 8))
        exif = img.getexif()
        exif.get_ifd(0x8769)[0x9003] = "2019:07:14 10:30:00"
        img.save(path, exif=exif)
        return path

    def test_the_cached_base_image_is_forgotten(self, host):
        host._on_exif_photo_saved("/a.jpg")
        assert host._viewer.last("invalidate_base_cache") == ("/a.jpg",)

    def test_the_new_date_reaches_the_catalogue(self, host, dated_jpeg):
        photo = _photo(str(dated_jpeg))
        host._current_photos = [photo]
        host._on_exif_photo_saved(photo.path)
        assert photo.date_taken == datetime(2019, 7, 14, 10, 30)
        assert host._catalog.last("add_or_update_photo") == (photo,)

    def test_a_photo_outside_the_view_is_not_updated(self, host, dated_jpeg):
        host._current_photos = [_photo("/other.jpg")]
        host._on_exif_photo_saved(str(dated_jpeg))
        assert host._catalog.called("add_or_update_photo") == []

    def test_an_unreadable_file_still_updates_the_catalogue(self, host, tmp_path):
        photo = _photo(str(tmp_path / "ghost.jpg"))
        host._current_photos = [photo]
        host._on_exif_photo_saved(photo.path)
        assert host._catalog.last("add_or_update_photo") == (photo,)


class TestRotationReindex:
    """A 90 degree rotation invalidates the detected faces: they are looked for
    again, and the last rotation requested must never be lost."""

    @pytest.fixture
    def reindex(self, monkeypatch):
        import src.faces.detector as detector
        monkeypatch.setattr(detector, "is_available", lambda: True)
        threads: dict = {}
        monkeypatch.setattr(mw, "SingleFaceReindexThread", _fake_thread(threads, "r"))
        return threads

    def test_a_rotation_restarts_the_detection(self, host, reindex):
        host._on_rotation_stepped("/a.jpg", 90)
        thread = reindex["r"][0]
        assert thread.args[1:3] == ("/a.jpg", 90) and thread.started is True
        assert host._reindex_thread is thread

    def test_without_detection_available_nothing_is_started(self, host, reindex,
                                                            monkeypatch):
        import src.faces.detector as detector
        monkeypatch.setattr(detector, "is_available", lambda: False)
        host._on_rotation_stepped("/a.jpg", 90)
        assert reindex == {}

    def test_a_rotation_during_a_detection_is_kept_for_later(self, host, reindex):
        running = _fake_thread({}, "x")()
        running.running = True
        host._reindex_thread = running
        host._on_rotation_stepped("/a.jpg", 180)
        assert host._pending_reindex == ("/a.jpg", 180)
        assert reindex == {}                    # nothing started meanwhile

    def test_only_the_last_rotation_survives(self, host, reindex):
        running = _fake_thread({}, "x")()
        running.running = True
        host._reindex_thread = running
        host._on_rotation_stepped("/a.jpg", 90)
        host._on_rotation_stepped("/a.jpg", 180)
        assert host._pending_reindex == ("/a.jpg", 180)

    def test_the_kept_rotation_is_started_at_the_end(self, host, reindex):
        finished = _fake_thread({}, "x")()
        host._reindex_thread = finished
        host._pending_reindex = ("/a.jpg", 270)
        host._drain_pending_reindex()
        assert host._pending_reindex is None
        assert reindex["r"][0].args[1:3] == ("/a.jpg", 270)

    def test_a_still_running_detection_is_waited_for(self, host, reindex, timers):
        running = _fake_thread({}, "x")()
        running.running = True
        host._reindex_thread = running
        host._pending_reindex = ("/a.jpg", 270)
        host._drain_pending_reindex()
        assert reindex == {} and timers.calls[0][1] == host._drain_pending_reindex

    def test_nothing_pending_starts_nothing(self, host, reindex):
        host._drain_pending_reindex()
        assert reindex == {}

    def test_the_end_of_the_detection_refreshes_the_open_panel(self, host, reindex):
        host._face_panel.show()
        host._on_single_reindex_finished("/a.jpg", 3)
        assert host._face_panel.last("set_photo") == ("/a.jpg",)


class TestViewerModes:
    """The interactive tools of the viewer: an on/off wire each."""

    @pytest.mark.parametrize("slot,on,off,arg", [
        ("_on_red_eye_mode_requested", "enter_red_eye_mode", "exit_red_eye_mode", 12.0),
        ("_on_vignette_edit_mode", "enter_vignette_mode", "exit_vignette_mode", EditInfo()),
        ("_on_annotation_mode_requested", "enter_annotation_mode", "exit_annotation_mode",
         "arrow"),
    ])
    def test_a_mode_is_entered_then_left(self, host, slot, on, off, arg):
        getattr(host, slot)(True, arg)
        assert host._viewer.last(on) == (arg,)
        getattr(host, slot)(False, arg)
        assert host._viewer.called(off)

    def test_the_colour_picker_is_started_then_stopped(self, host):
        host._on_wb_pick_requested(True)
        assert host._viewer.called("start_color_pick")
        host._on_wb_pick_requested(False)
        assert host._viewer.called("stop_color_pick")

    def test_the_annotations_toggle_is_remembered_for_the_session(self, host):
        host._on_annotations_toggle(False)
        assert host._annotations_globally_visible is False
        assert host._viewer.last("set_annotations_visible") == (False,)

    def test_the_zoom_of_the_viewer_moves_the_slider_without_a_loop(self, host):
        host._on_viewer_zoom_changed(2.5)
        assert host._zoom_slider.last("setValue") == (250,)
        assert host._zoom_slider.called("blockSignals")[0][1] == (True,)
        assert host._zoom_slider.called("blockSignals")[-1][1] == (False,)
        assert host._zoom_pct_label.last("setText") == ("250%",)

    @pytest.mark.parametrize("zoom,expected", [(0.01, 10), (12.0, 400)])
    def test_the_slider_stays_within_its_bounds(self, host, zoom, expected):
        host._on_viewer_zoom_changed(zoom)
        assert host._zoom_slider.last("setValue") == (expected,)

    def test_the_slider_drives_the_viewer(self, host):
        host._on_zoom_slider_changed(150)
        assert host._viewer.last("set_zoom") == (1.5,)
        assert host._zoom_pct_label.last("setText") == ("150%",)

    def test_the_selection_of_the_grid_is_reflected_in_the_status_bar(self, host):
        host._on_selection_changed([_photo("/a.jpg"), _photo("/b.jpg")])
        assert "2" in host._lbl_fileinfo.last("setText")[0]

    def test_a_saved_photo_refreshes_its_thumbnail(self, host):
        edit = EditInfo(brightness=0.2)
        host._on_photo_saved("/a.jpg", edit)
        assert host._grid.last("refresh_photo") == ("/a.jpg", edit)


class TestSidePanels:
    """Faces and EXIF share the right-hand panel: they exclude each other."""

    def test_the_faces_panel_takes_the_place_of_the_exif_panel(self, host):
        host._exif_panel.show()
        host._viewer = _Panel(visible=True, current_photo=lambda: _photo("/a.jpg"))
        host._on_faces_toggle(True)
        assert host._exif_panel.isVisible() is False
        assert host._face_panel.isVisible() is True
        assert host._right_panel.isVisible() is True
        assert host._btn_exif_toggle.last("setChecked") == (False,)
        assert host._face_panel.last("set_photo") == (_P("/a.jpg"),)

    def test_closing_the_faces_panel_closes_the_right_hand_panel(self, host):
        host._on_faces_toggle(True)
        host._on_faces_toggle(False)
        assert host._right_panel.isVisible() is False
        assert host._viewer.last("highlight_face") == (None,)

    def test_the_right_hand_panel_stays_open_for_the_exif(self, host):
        host._exif_panel.show()
        host._right_panel.show()
        host._on_faces_toggle(False)
        assert host._right_panel.isVisible() is True

    def test_the_exif_panel_takes_the_place_of_the_faces_panel(self, host):
        host._face_panel.show()
        photo = _photo("/a.jpg", tags=["mer"])
        host._viewer = _Panel(visible=True, current_photo=lambda: photo)
        host._on_exif_toggle(True)
        assert host._face_panel.isVisible() is False
        assert host._exif_panel.last("set_tags") == (["mer"],)
        assert host._btn_faces_toggle.last("setChecked") == (False,)

    def test_without_a_displayed_photo_the_panel_opens_empty(self, host):
        host._on_exif_toggle(True)
        assert host._exif_panel.isVisible() is True
        assert host._exif_panel.called("set_photo") == []


class TestExternalAppsDialog:
    """Tools › External applications…  The real dialog is built; only its
    modal exec() is replaced by a scenario driving its buttons."""

    @pytest.fixture
    def driver(self, monkeypatch):
        from types import SimpleNamespace
        state = SimpleNamespace(scenario=lambda dlg: None, accept=True)

        def _exec(dlg):
            state.scenario(dlg)
            return mw.QDialog.Accepted if state.accept else mw.QDialog.Rejected

        monkeypatch.setattr(mw.QDialog, "exec", _exec)
        return state

    @staticmethod
    def _click(dlg, label):
        from PySide6.QtWidgets import QPushButton
        button = next(b for b in dlg.findChildren(QPushButton) if b.text() == label)
        button.click()

    @staticmethod
    def _rows(dlg):
        from PySide6.QtWidgets import QListWidget
        lst = dlg.findChild(QListWidget)
        return [lst.item(i).text() for i in range(lst.count())]

    def test_the_configured_applications_are_listed_with_their_scope(self, host, driver):
        host._config.set("tools.external_apps", [
            {"name": "VLC", "path": r"C:\vlc.exe", "media": "video"}])
        seen: list = []
        driver.scenario = lambda dlg: seen.extend(self._rows(dlg))
        host._open_external_apps_dialog()
        assert "VLC" in seen[0] and r"C:\vlc.exe" in seen[0] and "Video" in seen[0]

    def test_a_cancelled_dialog_saves_nothing(self, host, driver):
        driver.accept = False
        host._open_external_apps_dialog()
        assert host._config.sets == []
        assert host._viewer.called("refresh_external_apps") == []

    def test_an_added_application_is_persisted(self, host, driver, files, inputs):
        files.opened = r"C:\Program Files\VLC\vlc.exe"
        inputs.text = ("VLC", True)
        inputs.item = ("Video", True)
        driver.scenario = lambda dlg: self._click(dlg, "Add…")
        host._open_external_apps_dialog()
        assert host._config.get("tools.external_apps") == [
            {"name": "VLC", "path": r"C:\Program Files\VLC\vlc.exe", "media": "video"}]
        assert host._viewer.called("refresh_external_apps")

    def test_the_name_of_the_executable_is_suggested(self, host, driver, files, inputs):
        files.opened = r"C:\Program Files\VLC\vlc.exe"
        inputs.text = ("", False)          # the user cancels: only the default matters
        driver.scenario = lambda dlg: self._click(dlg, "Add…")
        host._open_external_apps_dialog()
        assert host._config.get("tools.external_apps") == []

    def test_the_three_scopes_are_offered(self, host, driver, files, inputs):
        files.opened = r"C:\vlc.exe"
        inputs.text = ("VLC", True)
        inputs.item = ("", True)           # the default: the first entry
        driver.scenario = lambda dlg: self._click(dlg, "Add…")
        host._open_external_apps_dialog()
        assert inputs.offered == ["Both", "Photo", "Video"]
        assert host._config.get("tools.external_apps")[0]["media"] == "both"

    @pytest.mark.parametrize("cancel_at", ["file", "name", "media"])
    def test_cancelling_adds_nothing(self, host, driver, files, inputs, cancel_at):
        files.opened = "" if cancel_at == "file" else r"C:\vlc.exe"
        inputs.text = ("VLC", cancel_at != "name")
        inputs.item = ("Video", cancel_at != "media")
        driver.scenario = lambda dlg: self._click(dlg, "Add…")
        host._open_external_apps_dialog()
        assert host._config.get("tools.external_apps") == []

    def test_the_selected_application_is_removed(self, host, driver):
        host._config.set("tools.external_apps", [
            {"name": "VLC", "path": r"C:\vlc.exe", "media": "video"},
            {"name": "GIMP", "path": r"C:\gimp.exe", "media": "image"}])
        host._config.sets.clear()

        def _remove(dlg):
            from PySide6.QtWidgets import QListWidget
            dlg.findChild(QListWidget).setCurrentRow(0)
            self._click(dlg, "Remove")

        driver.scenario = _remove
        host._open_external_apps_dialog()
        assert [a["name"] for a in host._config.get("tools.external_apps")] == ["GIMP"]

    def test_removing_with_nothing_selected_does_nothing(self, host, driver):
        host._config.set("tools.external_apps", [
            {"name": "VLC", "path": r"C:\vlc.exe", "media": "video"}])
        driver.scenario = lambda dlg: self._click(dlg, "Remove")
        host._open_external_apps_dialog()
        assert len(host._config.get("tools.external_apps")) == 1


class TestOpenDvdFolder:
    """A DVD copy has no catalogued photo: the grid offers an external player."""

    @pytest.fixture
    def popen(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(mw.subprocess, "Popen", lambda argv: calls.append(argv))
        return calls

    def test_the_only_video_application_is_started_on_the_folder(self, host, popen):
        host._config.set("tools.external_apps", [
            {"name": "VLC", "path": r"C:\vlc.exe", "media": "video"}])
        host._open_dvd_folder(r"D:\DVD")
        assert popen == [[r"C:\vlc.exe", r"D:\DVD"]]

    def test_an_entry_without_a_scope_still_counts_as_a_player(self, host, popen):
        host._config.set("tools.external_apps", [{"name": "VLC", "path": r"C:\vlc.exe"}])
        host._open_dvd_folder(r"D:\DVD")
        assert popen == [[r"C:\vlc.exe", r"D:\DVD"]]

    def test_a_photo_application_is_never_offered(self, host, popen, box):
        host._config.set("tools.external_apps", [
            {"name": "GIMP", "path": r"C:\gimp.exe", "media": "image"}])
        host._open_dvd_folder(r"D:\DVD")
        assert popen == []
        assert "video" in box.instances[0].texts[-1]

    def test_with_no_application_at_all_the_user_is_told_how(self, host, popen, box):
        host._open_dvd_folder(r"D:\DVD")
        assert popen == []
        assert box.instances[0].texts[-1]

    def test_the_configure_button_opens_the_dialog(self, host, box, monkeypatch):
        opened: list = []
        monkeypatch.setattr(type(host), "_open_external_apps_dialog",
                            lambda self: opened.append(True))
        box.clicked_role = box.AcceptRole
        host._open_dvd_folder(r"D:\DVD")
        assert opened == [True]

    def test_several_players_open_a_choice_menu(self, host, popen, monkeypatch):
        host._config.set("tools.external_apps", [
            {"name": "VLC", "path": r"C:\vlc.exe", "media": "video"},
            {"name": "MPC", "path": r"C:\mpc.exe", "media": "both"}])
        menus: list = []
        monkeypatch.setattr(type(host), "_external_apps_menu",
                            lambda self, apps, target: menus.append((apps, target)) or _Rec())
        host._open_dvd_folder(r"D:\DVD")
        assert popen == []
        assert [a["name"] for a in menus[0][0]] == ["VLC", "MPC"]
        assert menus[0][1] == r"D:\DVD"

    def test_each_menu_entry_starts_its_own_application(self, host, popen):
        apps = [{"name": "VLC", "path": r"C:\vlc.exe"},
                {"name": "MPC", "path": r"C:\mpc.exe"}]
        menu = host._external_apps_menu(apps, r"D:\DVD")
        assert [a.text() for a in menu.actions()] == ["VLC", "MPC"]
        menu.actions()[1].trigger()
        assert popen == [[r"C:\mpc.exe", r"D:\DVD"]]

    def test_an_application_that_will_not_start_warns_the_user(self, host, box, monkeypatch):
        monkeypatch.setattr(mw.subprocess, "Popen", _raise(OSError("introuvable")))
        host._launch_external_app(r"C:\vlc.exe", r"D:\DVD")
        assert "introuvable" in box.warnings[0][1]


class TestEditTags:
    """Editing the keywords of a selection — the catalogue is preloaded in a
    thread, the dialog only opens afterwards."""

    @pytest.fixture
    def prep(self, monkeypatch):
        threads: dict = {}
        monkeypatch.setattr(mw, "TagsPrepLoader", _fake_thread(threads, "prep"))
        return threads

    @pytest.fixture
    def dlg(self, monkeypatch):
        class _Dlg:
            accepted = True
            add_remove = ([], [])

            def __init__(self, photos, all_tags, parent=None):
                _Dlg.all_tags = all_tags

            def exec(self):
                return mw.QDialog.Accepted if _Dlg.accepted else mw.QDialog.Rejected

            def result_add_remove(self):
                return _Dlg.add_remove

        _Dlg.accepted, _Dlg.add_remove, _Dlg.all_tags = True, ([], []), None
        monkeypatch.setattr(mw, "TagEditDialog", _Dlg)
        return _Dlg

    def test_an_empty_selection_opens_nothing(self, host, prep):
        host._on_edit_tags_requested([])
        assert prep == {}

    def test_the_known_keywords_are_loaded_before_the_dialog(self, host, prep, dlg):
        photo = _photo("/a.jpg", id=1)
        host._on_edit_tags_requested([photo])
        thread = prep["prep"][0]
        assert thread.started is True
        thread.ready.emit(["mer", "montagne"])
        assert dlg.all_tags == ["mer", "montagne"]

    def test_a_cancelled_dialog_writes_nothing(self, host, dlg):
        dlg.accepted = False
        host._continue_edit_tags([_photo("/a.jpg", id=1)], [])
        assert host._catalog.called("add_tags_to_photos") == []

    def test_the_added_keywords_reach_the_catalogue_and_the_photos(self, host, dlg):
        dlg.add_remove = (["mer"], [])
        photo = _photo("/a.jpg", id=1, tags=["ete"])
        host._continue_edit_tags([photo], [])
        assert host._catalog.last("add_tags_to_photos") == ([1], ["mer"])
        assert photo.tags == ["ete", "mer"]
        assert host._sidebar.called("refresh_tags")

    def test_the_removed_keywords_leave_one_by_one(self, host, dlg):
        dlg.add_remove = ([], ["ete", "mer"])
        photo = _photo("/a.jpg", id=1, tags=["ete", "mer", "plage"])
        host._continue_edit_tags([photo], [])
        assert [c[1][1] for c in host._catalog.called("remove_tag_from_photos")] == \
            ["ete", "mer"]
        assert photo.tags == ["plage"]

    def test_an_already_carried_keyword_is_not_duplicated(self, host, dlg):
        dlg.add_remove = (["mer"], [])
        photo = _photo("/a.jpg", id=1, tags=["mer"])
        host._continue_edit_tags([photo], [])
        assert photo.tags == ["mer"]

    def test_photos_outside_the_catalogue_write_nothing(self, host, dlg):
        dlg.add_remove = (["mer"], [])
        host._continue_edit_tags([_photo("/a.jpg", id=None)], [])
        assert host._catalog.called("add_tags_to_photos") == []

    def test_the_displayed_photo_is_refreshed(self, host, dlg):
        dlg.add_remove = (["mer"], [])
        photo = _photo("/a.jpg", id=1)
        current = _photo("/a.jpg", id=1)          # another instance, same path
        host._viewer = _Panel(visible=True, current_photo=lambda: current)
        host._exif_panel.show()
        host._continue_edit_tags([photo], [])
        assert current.tags == ["mer"]
        assert host._viewer.called("refresh_tags")
        assert host._exif_panel.last("set_tags") == (["mer"],)

    def test_a_photo_outside_the_selection_is_left_alone(self, host, dlg):
        dlg.add_remove = (["mer"], [])
        current = _photo("/other.jpg", id=2)
        host._viewer = _Panel(visible=True, current_photo=lambda: current)
        host._continue_edit_tags([_photo("/a.jpg", id=1)], [])
        assert current.tags == []
        assert host._viewer.called("refresh_tags") == []

    @pytest.mark.parametrize("added,method", [
        (True, "add_tags_to_photos"), (False, "remove_tag_from_photos")])
    def test_the_dropdown_of_the_viewer_writes_straight_away(self, host, added, method):
        host._on_viewer_tag_toggle_requested(_photo("/a.jpg", id=1), "mer", added)
        assert host._catalog.called(method)
        assert host._sidebar.called("refresh_tags")

    def test_a_photo_absent_from_the_catalogue_writes_nothing(self, host):
        host._on_viewer_tag_toggle_requested(_photo("/a.jpg", id=None), "mer", True)
        assert host._catalog.called("add_tags_to_photos") == []


class TestAdvancedSearch:
    """Advanced search: the same preloading pattern, then a query in a thread."""

    @pytest.fixture
    def prep(self, monkeypatch):
        threads: dict = {}
        monkeypatch.setattr(mw, "AdvancedSearchPrepLoader", _fake_thread(threads, "prep"))
        return threads

    @pytest.fixture
    def dlg(self, monkeypatch):
        class _Dlg:
            accepted = True
            criteria = {"camera": "Nikon"}
            person_id = None

            def __init__(self, cameras, persons, all_tags, folders, parent=None):
                _Dlg.args = (cameras, persons, all_tags, folders)

            def exec(self):
                return mw.QDialog.Accepted if _Dlg.accepted else mw.QDialog.Rejected

            def get_criteria(self):
                return _Dlg.criteria

            def get_person_id(self):
                return _Dlg.person_id

        _Dlg.accepted, _Dlg.criteria, _Dlg.person_id, _Dlg.args = \
            True, {"camera": "Nikon"}, None, None
        monkeypatch.setattr(mw, "AdvancedSearchDialog", _Dlg)
        return _Dlg

    def test_the_criteria_are_preloaded_before_the_dialog(self, host, prep, dlg):
        host._config.folders.append(r"D:\Photos")
        host._open_advanced_search()
        assert prep["prep"][0].started is True
        prep["prep"][0].ready.emit(["Nikon"], [], ["mer"])
        assert dlg.args == (["Nikon"], [], ["mer"], [r"D:\Photos"])

    def test_a_cancelled_dialog_searches_nothing(self, host, dlg):
        dlg.accepted = False
        host._continue_advanced_search([], [], [])
        assert host.queries == []

    def test_the_search_is_displayed_in_its_own_context(self, host, dlg):
        host._catalog = _Rec(search_advanced=lambda c: [_photo("/a.jpg")])
        host._continue_advanced_search([], [], [])
        context, album_id, folder, photos = host.queries[0]
        assert context == "Recherche avancée" and album_id is None and folder is None
        assert [p.filename for p in photos] == ["a.jpg"]
        assert host._grid.last("set_ribbon_mode") == (False,)
        assert host._grid_nav_bar.called("hide")

    def test_a_person_narrows_the_result_of_the_sql_query(self, host, dlg):
        dlg.person_id = 7
        host._catalog = _Rec(search_advanced=lambda c: [_photo("/a.jpg"), _photo("/b.jpg")])
        host._face_db = _Rec(get_photos_for_person=lambda pid: [_P("/b.jpg")])
        host._continue_advanced_search([], [], [])
        assert [p.filename for p in host.queries[0][3]] == ["b.jpg"]

    def test_without_a_person_the_sql_result_is_kept_as_is(self, host, dlg):
        host._catalog = _Rec(search_advanced=lambda c: [_photo("/a.jpg")])
        host._continue_advanced_search([], [], [])
        assert host._face_db.called("get_photos_for_person") == []


class TestLoadLibrary:
    """Start-up: the tree, the albums, the keywords, then the scan."""

    @pytest.fixture
    def warmup(self, monkeypatch):
        threads: dict = {}
        monkeypatch.setattr(mw, "TFWarmUpThread", _fake_thread(threads, "warm"))
        return threads

    def test_an_empty_library_lands_on_all_photos(self, host, warmup):
        host._load_library()
        assert host.log == ["show_all_photos"]
        assert host.scans == [] and warmup == {}

    def test_the_watched_folders_are_scanned_and_watched(self, host, warmup):
        host._config.folders.append(r"D:\Photos")
        host._load_library()
        assert host.scans == [([r"D:\Photos"], False)]
        assert host._folder_watcher.last("set_folders") == ([r"D:\Photos"],)
        assert warmup["warm"][0].started is True     # loaded in parallel

    def test_the_albums_and_keywords_reach_the_sidebar(self, host, warmup):
        host._catalog = _Rec(get_albums=lambda: [AlbumInfo(id=1, name="Vacances")],
                             get_all_tags=lambda: ["mer"])
        host._load_library()
        assert host._sidebar.last("refresh_tags") == (["mer"],)
        assert host._viewer.last("set_available_tags") == (["mer"],)

    def test_the_files_in_error_are_flagged_in_the_grid(self, host, warmup):
        host._config.folders.append(r"D:\Photos")
        host._face_db = _Rec(get_error_paths=lambda: [r"D:\Photos\ko.jpg"])
        host._load_library()
        assert host._grid.last("set_index_error_paths") == ([r"D:\Photos\ko.jpg"],)

    def test_the_state_of_the_collapsed_sections_is_restored(self, host, warmup):
        host._config.set("ui.ratings_collapsed", True)
        host._load_library()
        assert host._sidebar.last("set_section_collapsed_state") == (True, False)


class TestScanFinished:
    """End of scan: what is refreshed depends on what the scan really changed."""

    def test_a_scan_without_change_costs_nothing(self, host):
        host._on_scan_finished(0)
        assert host._catalog.called("get_albums") == []
        assert host.log == ["update_persons_counts", "face_indexing"]

    def test_new_photos_rebuild_the_people(self, host):
        host._on_scan_finished(5)
        assert host._sidebar.called("refresh_albums")
        assert "refresh_persons" in host.log

    def test_a_deletion_only_updates_the_counters(self, host):
        host._persons_loaded = True
        host._scan_had_removals = True
        host._on_scan_finished(0)
        assert host._sidebar.called("refresh_albums")
        assert "update_persons_counts" in host.log and "refresh_persons" not in host.log

    def test_the_second_pass_without_change_refreshes_nothing(self, host):
        host._persons_loaded = True
        host._on_scan_finished(0)
        assert "update_persons_counts" not in host.log

    def test_the_new_photos_are_sorted_again(self, host):
        host._current_context = r"D:\Photos"
        host._current_photos = [_photo(r"D:\Photos\b.jpg"), _photo(r"D:\Photos\a.jpg")]
        host._config.set("display_order.grid_mode", "name")
        host._config.set("display_order.grid_dir", "asc")
        host._on_scan_finished(2)
        assert [p.filename for p in host._current_photos] == ["a.jpg", "b.jpg"]
        assert host._grid.called("set_photos")

    def test_a_person_view_keeps_its_own_order(self, host):
        host._current_context = "__person__3"
        host._current_photos = [_photo("/b.jpg"), _photo("/a.jpg")]
        host._on_scan_finished(2)
        assert [p.filename for p in host._current_photos] == ["b.jpg", "a.jpg"]

    def test_the_face_indexing_waits_for_the_warm_up(self, host):
        warm = _fake_thread({}, "x")()
        warm.running = True
        host._warmup_thread = warm
        host._on_scan_finished(1)
        assert host._face_index_pending is True
        assert "face_indexing" not in host.log

    def test_the_end_of_the_warm_up_launches_the_deferred_indexing(self, host):
        host._warmup_thread = _fake_thread({}, "x")()
        host._face_index_pending = True
        host._on_warmup_done()
        assert host._warmup_thread is None
        assert host.log == ["face_indexing"]
        assert host._face_index_pending is False

    def test_a_warm_up_with_nothing_pending_launches_nothing(self, host):
        host._on_warmup_done()
        assert host.log == []

    def test_the_duplicates_gate_is_only_connected_once(self, host):
        host._on_scan_finished(0)
        host._on_scan_finished(0)
        assert len(host._sidebar.persons_thumbnails_ready.slots) == 1


class TestUpdateCheck:
    """The version check: silent when up to date, a dialog otherwise."""

    @pytest.fixture
    def check(self, monkeypatch):
        threads: dict = {}
        monkeypatch.setattr(mw, "UpdateCheckThread", _fake_thread(threads, "check"))
        return threads

    @pytest.fixture
    def opened(self, monkeypatch):
        urls: list = []
        monkeypatch.setattr(mw.QDesktopServices, "openUrl", lambda url: urls.append(url))
        return urls

    def test_the_check_runs_in_the_background(self, host, check):
        host._start_update_check()
        assert check["check"][0].started is True

    def test_an_up_to_date_version_says_nothing(self, host, box, opened):
        host._on_update_checked("up_to_date", "1.0.0", "https://x")
        assert box.instances == []

    def test_a_new_version_offers_the_download_page(self, host, box, opened):
        box.clicked_role = box.AcceptRole
        host._on_update_checked(mw.STATUS_UPDATE_AVAILABLE, "2.0.0", "https://x/rel")
        assert "2.0.0" in box.instances[0].texts[-1]
        assert [u.toString() for u in opened] == ["https://x/rel"]

    def test_later_opens_nothing(self, host, box, opened):
        box.clicked_role = box.RejectRole
        host._on_update_checked(mw.STATUS_UPDATE_AVAILABLE, "2.0.0", "https://x/rel")
        assert opened == []


class TestPhotoQuery:
    """The query feeding the grid: in a thread, and cancellable."""

    @pytest.fixture
    def query(self, monkeypatch):
        threads: dict = {}
        monkeypatch.setattr(mw, "_PhotoQueryThread", _fake_thread(threads, "q"))
        return threads

    def test_the_query_is_started_with_its_sort_parameters(self, host, query):
        host._config.set("display_order.grid_dir", "asc")
        MainWindow._start_photo_query(host, lambda: [], r"D:\Photos",
                                      folder_path=r"D:\Photos")
        thread = query["q"][0]
        assert thread.started is True and thread.args[1] == r"D:\Photos"
        assert thread.args[3] is False                   # reverse = (dir == "desc")
        assert host._grid.last("set_loading") == (True,)

    def test_a_query_in_flight_is_cancelled_by_the_next_one(self, host, query):
        previous = _fake_thread({}, "x")()
        previous.running = True
        host._photo_query_thread = previous
        MainWindow._start_photo_query(host, lambda: [], "Favoris")
        assert host._photo_query_thread is query["q"][0]

    def test_the_result_replaces_the_displayed_view(self, host):
        photos = [_photo("/a.jpg"), _photo("/b.jpg")]
        host._on_photo_query_ready(photos, "Favoris", 7)
        assert host._current_photos == photos
        assert host._current_paths == {p.path for p in photos}
        assert host._current_context == "Favoris"
        assert host._current_album_id == 7
        assert host._grid.last("set_album_context") == (7,)
        assert host._grid.last("set_photos") == (photos,)

    def test_an_empty_dvd_copy_offers_an_external_player(self, host, tmp_path,
                                                         monkeypatch):
        (tmp_path / "VIDEO_TS").mkdir()
        opened: list = []
        monkeypatch.setattr(type(host), "_open_dvd_folder",
                            lambda self, folder: opened.append(folder))
        host._on_photo_query_ready([], str(tmp_path), None, str(tmp_path))
        text, button, callback = host._grid.last("show_empty_message")
        assert "VIDEO_TS" in text and button
        callback()
        assert opened == [str(tmp_path)]

    def test_an_ordinary_empty_folder_says_nothing(self, host, tmp_path):
        host._on_photo_query_ready([], str(tmp_path), None, str(tmp_path))
        assert host._grid.called("show_empty_message") == []

    def test_an_album_that_happens_to_be_a_path_is_not_a_dvd(self, host, tmp_path):
        (tmp_path / "VIDEO_TS").mkdir()
        host._on_photo_query_ready([], str(tmp_path), 7)     # no folder_path
        assert host._grid.called("show_empty_message") == []


class TestSlideshow:
    """Where the slideshow starts from depends on what is displayed."""

    @pytest.fixture
    def window(self, monkeypatch):
        import src.ui.slideshow as slideshow
        built: list = []
        monkeypatch.setattr(slideshow, "SlideshowWindow",
                            lambda **kw: built.append(kw) or _Rec())
        return built

    def test_an_empty_view_starts_nothing(self, host, window):
        host._start_slideshow()
        assert window == []

    def test_the_open_viewer_decides_the_starting_photo(self, host, window):
        host._current_photos = [_photo("/a.jpg"), _photo("/b.jpg")]
        host._current_photo_index = 1
        host._viewer = _Panel(visible=True, current_photo=lambda: None)
        host._start_slideshow()
        assert window[0]["start_index"] == 1

    def test_the_chronological_ribbon_starts_at_its_centre(self, host, window):
        host._current_photos = [_photo("/a.jpg"), _photo("/b.jpg"), _photo("/c.jpg")]
        host._grid = _Rec(center_photo_index=lambda: 2)
        host._start_slideshow()
        assert window[0]["start_index"] == 2

    def test_otherwise_it_starts_at_the_oldest_photo(self, host, window):
        host._current_photos = [_photo("/a.jpg"), _photo("/b.jpg")]
        host._grid = _Rec(center_photo_index=lambda: None)
        host._start_slideshow()
        assert window[0]["start_index"] == 1        # the list is antichronological


class TestFolderManager:
    """Tools › Folders… — the dialog only emits, the window acts."""

    def test_a_forced_rescan_bypasses_the_mtime_cache(self, host):
        host._on_folder_rescan_requested(r"D:\Photos")
        assert host.scans == [([r"D:\Photos"], True)]

    def test_an_added_folder_is_watched_scanned_and_probed_for_picasa(
            self, host, monkeypatch):
        prompted: list = []
        monkeypatch.setattr(type(host), "_maybe_prompt_picasa_for_new_folder",
                            lambda self, folder: prompted.append(folder))
        host._on_folder_added_from_manager(r"D:\Photos")
        assert host._config.added == [r"D:\Photos"]
        assert host.scans == [([r"D:\Photos"], False)]
        assert host._folder_watcher.last("set_folders") == (host._config.folders,)
        assert prompted == [r"D:\Photos"]


class TestWindowChrome:
    """Full screen and the settings dialog."""

    def test_full_screen_is_a_toggle(self, host):
        host._toggle_fullscreen()
        assert host.isFullScreen() is True
        host._toggle_fullscreen()
        assert host.isFullScreen() is False

    def test_the_settings_dialog_realigns_the_language_flag(self, host, monkeypatch):
        dlg = _Rec(exec=lambda: 0)
        dlg.recluster_needed = RecordingSignal()      # a signal, not a method
        monkeypatch.setattr(mw, "SettingsDialog", lambda config, parent: dlg)
        host._btn_language = _Rec()
        host._open_settings()
        assert dlg.called("exec")
        assert dlg.recluster_needed.slots == [host._run_clustering]
        assert host._btn_language.called("refresh")


class TestScanWiring:
    """_start_scan wires the scanner thread to the four slots of the window."""

    def test_the_scanner_thread_is_wired_to_the_four_slots(self, host):
        thread = _fake_thread({}, "s")()
        host._scanner = _Rec(scan=lambda folders, force: thread)
        host._scan_had_removals = True
        MainWindow._start_scan(host, [r"D:\Photos"])       # the host neutralises it
        assert host._scan_had_removals is False            # reset for this pass
        assert thread.photos_batch.slots == [host._on_photos_batch]
        assert thread.photos_removed.slots == [host._on_photos_removed]
        assert thread.finished.slots == [host._on_scan_finished]
        assert thread.progress.slots == [host._on_scan_progress]

    def test_a_forced_scan_is_passed_on_to_the_scanner(self, host):
        host._scanner = _Rec(scan=lambda folders, force: _fake_thread({}, "s")())
        MainWindow._start_scan(host, [r"D:\Photos"], force=True)
        assert host._scanner.last("scan") == ([r"D:\Photos"],)
        assert host._scanner.called("scan")[0][2] == {"force": True}

    def test_the_progress_is_shown_in_the_status_bar(self, host):
        host._on_scan_progress(42, r"D:\Photos\a.jpg")
        text = host._lbl_action.last("setText")[0]
        assert "42" in text and "a.jpg" in text


class TestSortKeyAndStatus:
    """The chronological key, and the file line of the viewer."""

    def test_the_shooting_date_wins(self, host):
        photo = _photo("/a.jpg", date_taken=datetime(2020, 1, 1), file_mtime=1.0)
        assert mw._photo_sort_key(photo) == datetime(2020, 1, 1)

    def test_without_a_shooting_date_the_file_date_is_used(self, host):
        photo = _photo("/a.jpg", file_mtime=time.mktime((2021, 6, 1, 12, 0, 0, 0, 0, -1)))
        assert mw._photo_sort_key(photo).year == 2021

    def test_without_any_date_the_photo_goes_last(self, host):
        assert mw._photo_sort_key(_photo("/a.jpg")) == datetime.min

    def test_the_viewer_shows_the_name_and_the_size(self, host):
        host._update_viewer_status(_photo("/a.jpg", file_size=2048))
        text = host._lbl_fileinfo.last("setText")[0]
        assert "a.jpg" in text and "2" in text

    def test_a_file_of_unknown_size_shows_its_name_alone(self, host):
        host._update_viewer_status(_photo("/a.jpg", file_size=0))
        assert host._lbl_fileinfo.last("setText") == ("a.jpg",)

    def test_a_double_click_opens_the_photo_at_its_place_in_the_view(self, host):
        photos = [_photo("/a.jpg"), _photo("/b.jpg"), _photo("/c.jpg")]
        host._current_photos = photos
        host._on_photo_activated(photos[2])
        assert host._current_photo_index == 2
        assert host._viewer.last("set_photo") == (photos[2],)

    def test_a_photo_outside_the_view_opens_at_the_start(self, host):
        host._current_photos = [_photo("/a.jpg")]
        host._on_photo_activated(_photo("/z.jpg"))
        assert host._current_photo_index == 0


class TestRunExportPipeline:
    """The three optional stages of the batch export: edits, annotations, frame."""

    @pytest.fixture(autouse=True)
    def no_explorer(self, monkeypatch):
        monkeypatch.setenv("PPM_SUPPRESS_EXPLORER", "1")

    @pytest.fixture
    def jpeg(self, tmp_path):
        from PIL import Image
        path = tmp_path / "a.jpg"
        Image.new("RGB", (40, 30), (200, 100, 50)).save(path)
        return path

    def test_the_edits_are_baked_into_the_exported_copy(
            self, host, jpeg, tmp_path, timers):
        from PIL import Image
        host._edit_db = _Rec(load=lambda p: EditInfo(brightness=0.5))
        out = tmp_path / "export"
        host._run_export([_photo(str(jpeg))], out, None, 90)
        with Image.open(jpeg) as before, Image.open(out / "a.jpg") as after:
            assert after.getpixel((0, 0)) != before.getpixel((0, 0))

    def test_the_frame_enlarges_the_exported_copy(self, host, jpeg, tmp_path, timers):
        from PIL import Image
        host._edit_db = _Rec(load=lambda p: EditInfo(frame_type="plain"))
        out = tmp_path / "export"
        host._run_export([_photo(str(jpeg))], out, None, 90)
        with Image.open(out / "a.jpg") as img:
            assert img.size[0] > 40 and img.size[1] > 30

    def test_the_annotations_are_composited_when_they_are_shown(
            self, host, jpeg, tmp_path, timers, monkeypatch):
        import src.ui.annotation_renderer as renderer
        seen: list = []
        monkeypatch.setattr(renderer, "composite_annotations_pil",
                            lambda img, ann: seen.append(ann) or img)
        host._edit_db = _Rec(load=lambda p: EditInfo(annotations=[{"type": "text"}]))
        host._run_export([_photo(str(jpeg))], tmp_path / "export", None, 90)
        assert seen == [[{"type": "text"}]]

    def test_the_annotations_are_left_out_when_they_are_hidden(
            self, host, jpeg, tmp_path, timers, monkeypatch):
        import src.ui.annotation_renderer as renderer
        seen: list = []
        monkeypatch.setattr(renderer, "composite_annotations_pil",
                            lambda img, ann: seen.append(ann) or img)
        host._annotations_globally_visible = False
        host._edit_db = _Rec(load=lambda p: EditInfo(annotations=[{"type": "text"}]))
        host._run_export([_photo(str(jpeg))], tmp_path / "export", None, 90)
        assert seen == []

    def test_a_photo_with_an_alpha_channel_is_flattened_to_jpeg(
            self, host, tmp_path, timers):
        from PIL import Image
        src = tmp_path / "a.png"
        Image.new("RGBA", (20, 10), (10, 200, 10, 128)).save(src)
        out = tmp_path / "export"
        host._run_export([_photo(str(src))], out, None, 90)
        with Image.open(out / "a.jpg") as img:
            assert img.mode == "RGB"


class TestExifToggleClosing:
    """Closing the EXIF panel: the right-hand panel only goes away with it."""

    def test_closing_the_exif_panel_closes_the_right_hand_panel(self, host):
        host._on_exif_toggle(True)
        host._on_exif_toggle(False)
        assert host._exif_panel.isVisible() is False
        assert host._right_panel.isVisible() is False

    def test_the_right_hand_panel_stays_open_for_the_faces(self, host):
        host._on_exif_toggle(True)
        host._face_panel.show()
        host._on_exif_toggle(False)
        assert host._right_panel.isVisible() is True


class TestDialogOpeners:
    """The menu entries that only open a dialog — what is checked is which
    dialog, with which arguments, and how it is wired back to the window."""

    @pytest.fixture
    def opened(self):
        return []

    @staticmethod
    def _fake(opened, module, name, monkeypatch):
        """Replaces a dialog class by a recorder; the imports are local to the
        slot, so patching the source module is enough."""
        import importlib
        mod = importlib.import_module(module)

        def _factory(*args, **kwargs):
            dlg = _Rec(exec=lambda: mw.QDialog.Accepted)
            dlg.args, dlg.kwargs = args, kwargs
            opened.append(dlg)
            return dlg

        monkeypatch.setattr(mod, name, _factory)
        return opened

    def test_the_thread_journal_is_shown(self, host, opened, monkeypatch):
        self._fake(opened, "src.ui.thread_journal_dialog", "ThreadJournalDialog",
                   monkeypatch)
        host._open_thread_journal()
        assert opened[0].called("exec")
        assert opened[0].args == (host,)

    def test_the_problems_history_is_shown(self, host, opened, monkeypatch):
        self._fake(opened, "src.ui.problems_history_dialog", "ProblemsHistoryDialog",
                   monkeypatch)
        host._open_problems_history()
        assert opened[0].called("exec")

    def test_the_deleted_corrupted_files_are_shown(self, host, opened, monkeypatch):
        self._fake(opened, "src.ui.deleted_corrupted_files_dialog",
                   "DeletedCorruptedFilesDialog", monkeypatch)
        host._open_deleted_corrupted_files_dialog()
        assert opened[0].called("exec")

    def test_the_exif_date_sync_receives_the_catalog(self, host, opened, monkeypatch):
        self._fake(opened, "src.ui.exif_date_sync_dialog", "ExifDateSyncDialog",
                   monkeypatch)
        host._open_exif_date_sync()
        assert opened[0].args == (host._catalog, host)

    def test_the_help_opens_on_its_first_page(self, host, opened, monkeypatch):
        self._fake(opened, "src.ui.help_dialog", "HelpDialog", monkeypatch)
        host._show_help()
        assert opened[0].args == (host,) and opened[0].kwargs == {}

    def test_the_about_box_is_a_tab_of_the_help(self, host, opened, monkeypatch):
        self._fake(opened, "src.ui.help_dialog", "HelpDialog", monkeypatch)
        host._show_about()
        # The tab name doubles as a key (cf. help_dialog._TABS): it stays French.
        assert opened[0].kwargs == {"tab": "À propos"}

    def test_the_folder_manager_is_wired_to_the_three_slots(
            self, host, opened, monkeypatch):
        import src.ui.folder_manager_dialog as fmd

        def _factory(config, catalog, parent):
            dlg = _Rec(exec=lambda: 0)
            dlg.args = (config, catalog, parent)
            for signal in ("rescan_requested", "folder_removed", "folder_added"):
                setattr(dlg, signal, RecordingSignal())
            opened.append(dlg)
            return dlg

        monkeypatch.setattr(fmd, "FolderManagerDialog", _factory)
        host._open_folder_manager()
        dlg = opened[0]
        assert dlg.args == (host._config, host._catalog, host)
        assert dlg.rescan_requested.slots == [host._on_folder_rescan_requested]
        assert dlg.folder_removed.slots == [host._on_folder_removed]
        assert dlg.folder_added.slots == [host._on_folder_added_from_manager]
        assert dlg.called("exec")

    def test_an_accepted_display_order_is_saved_then_applied(
            self, host, monkeypatch):
        applied: list = []
        dlg = _Rec(exec=lambda: mw.QDialog.Accepted)
        monkeypatch.setattr(mw, "DisplayOrderDialog", lambda config, parent: dlg)
        monkeypatch.setattr(type(host), "_apply_display_order",
                            lambda self: applied.append(True))
        host._open_display_order_dialog()
        assert dlg.called("save_to_config")
        assert applied == [True]

    def test_a_cancelled_display_order_changes_nothing(self, host, monkeypatch):
        applied: list = []
        dlg = _Rec(exec=lambda: mw.QDialog.Rejected)
        monkeypatch.setattr(mw, "DisplayOrderDialog", lambda config, parent: dlg)
        monkeypatch.setattr(type(host), "_apply_display_order",
                            lambda self: applied.append(True))
        host._open_display_order_dialog()
        assert dlg.called("save_to_config") == []
        assert applied == []


class TestExportModeConversions:
    """Every export ends in RGB — a palette or an alpha channel is flattened."""

    @pytest.fixture(autouse=True)
    def no_explorer(self, monkeypatch):
        monkeypatch.setenv("PPM_SUPPRESS_EXPLORER", "1")

    def test_a_palette_image_is_converted_for_the_single_export(
            self, host, tmp_path, timers):
        from PIL import Image
        src = tmp_path / "a.png"
        Image.new("P", (20, 10)).save(src)
        dest = tmp_path / "out.jpg"
        host._export_image(_photo(str(src)), str(dest))
        with Image.open(dest) as img:
            assert img.mode == "RGB"

    def test_a_palette_image_is_converted_for_the_batch_export(
            self, host, tmp_path, timers):
        from PIL import Image
        src = tmp_path / "a.png"
        Image.new("P", (20, 10)).save(src)
        out = tmp_path / "export"
        host._run_export([_photo(str(src))], out, None, 90)
        with Image.open(out / "a.jpg") as img:
            assert img.mode == "RGB"

    def test_an_alpha_channel_is_flattened_when_the_target_is_jpeg(
            self, host, tmp_path, timers):
        from PIL import Image
        src = tmp_path / "a.png"
        Image.new("RGBA", (20, 10), (10, 200, 10, 128)).save(src)
        dest = tmp_path / "out.jpg"
        host._export_image(_photo(str(src)), str(dest))
        with Image.open(dest) as img:
            assert img.mode == "RGB"

    def test_an_alpha_channel_survives_a_png_target(self, host, tmp_path, timers):
        from PIL import Image
        src = tmp_path / "a.png"
        Image.new("RGBA", (20, 10), (10, 200, 10, 128)).save(src)
        dest = tmp_path / "out.png"
        host._export_image(_photo(str(src)), str(dest))
        with Image.open(dest) as img:
            assert img.mode == "RGBA"

    def test_the_annotations_are_composited_by_the_single_export(
            self, host, tmp_path, timers, monkeypatch):
        from PIL import Image
        import src.ui.annotation_renderer as renderer
        seen: list = []
        monkeypatch.setattr(renderer, "composite_annotations_pil",
                            lambda img, ann: seen.append(ann) or img)
        src = tmp_path / "a.jpg"
        Image.new("RGB", (20, 10), (1, 2, 3)).save(src)
        host._edit_db = _Rec(load=lambda p: EditInfo(annotations=[{"type": "arrow"}]))
        host._export_image(_photo(str(src)), str(tmp_path / "out.jpg"))
        assert seen == [[{"type": "arrow"}]]


class TestLastSmallSlots:
    """The residual branches: a finished query, and the EXIF panel kept in step."""

    def test_a_finished_query_thread_is_simply_dropped(self, host):
        thread = _fake_thread({}, "q")()
        thread.running = False
        host._photo_query_thread = thread
        host._cancel_grid_display_ops()
        assert host._photo_query_thread is None

    def test_a_tag_toggled_from_the_viewer_refreshes_the_open_exif_panel(self, host):
        host._exif_panel.show()
        photo = _photo("/a.jpg", id=7, tags=["mer"])
        host._on_viewer_tag_toggle_requested(photo, "mer", True)
        assert host._catalog.last("add_tags_to_photos") == ([7], ["mer"])
        assert host._exif_panel.last("set_tags") == (["mer"],)

    def test_the_bus_selection_slot_is_a_documented_no_op(self, host):
        assert host._on_bus_photo_selected(_photo("/a.jpg")) is None


class TestBackupOriginal:
    """The safety copy before an overwrite — .tmp_originals, hidden on Windows."""

    def test_the_original_is_copied_into_a_timestamped_file(self, host, tmp_path):
        src = tmp_path / "a.jpg"
        src.write_bytes(b"les pixels d'origine")
        host._backup_original(str(src))
        copies = list((tmp_path / ".tmp_originals").glob("a_*.jpg"))
        assert len(copies) == 1
        assert copies[0].read_bytes() == b"les pixels d'origine"

    def test_a_folder_that_cannot_be_hidden_does_not_stop_the_copy(
            self, host, tmp_path, monkeypatch):
        class _NoWinApi:
            @property
            def windll(self):
                raise AttributeError("no windll here")   # a non-Windows system

        monkeypatch.setattr(mw, "ctypes", _NoWinApi())
        src = tmp_path / "a.jpg"
        src.write_bytes(b"les pixels d'origine")
        host._backup_original(str(src))
        assert list((tmp_path / ".tmp_originals").glob("a_*.jpg"))
