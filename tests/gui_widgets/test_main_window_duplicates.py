# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Duplicates & corrupted files controller (main_window_duplicates.py).

Same harness as test_main_window_faces.py: the REAL methods of the mixin are
called unbound against a minimal host (cf. controller_doubles). Two specifics
here:
- the dialogs are real QDialog built on the spot; `QDialog.exec()` is replaced
  by a subclass that records itself and returns at once, so the whole widget
  tree stays real and its buttons can genuinely be clicked;
- DedupCache and the problems history are always doubled: a test must never
  write into the real %LOCALAPPDATA% of the user running it.
"""
import os
from datetime import datetime

import pytest
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QListWidget, QProgressBar, QWidget,
)

import src.ui.main_window_duplicates as mwd
from src.core.models import PhotoInfo
from src.ui.main_window_duplicates import DuplicatesController
from tests.gui_widgets.controller_doubles import (
    Recorder as _Rec, RecordingSignal as _Signal, fake_thread as _fake_thread,
    make_message_box,
)

_P = os.path.normpath          # PhotoInfo normalises its path


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def box(monkeypatch):
    fake = make_message_box()
    monkeypatch.setattr(mwd, "QMessageBox", fake)
    return fake


@pytest.fixture
def dialogs(monkeypatch):
    """Every QDialog of the module, opened without blocking.

    A real QDialog subclass and not a stub: the code builds its layout, its
    QDialogButtonBox and its lambdas on it, and the tests click those buttons
    for real."""
    opened = []

    class _Dialog(QDialog):
        def exec(self):
            opened.append(self)
            return QDialog.Accepted

    monkeypatch.setattr(mwd, "QDialog", _Dialog)
    return opened


def _button(dialog, label: str):
    """The button of a QDialogButtonBox carrying that label (the dialogs are
    built on the fly: there is no attribute to reach them by)."""
    for group in dialog.findChildren(QDialogButtonBox):
        for btn in group.buttons():
            if btn.text().replace("&", "") == label:
                return btn
    raise AssertionError(f"bouton {label!r} absent de {dialog.windowTitle()!r}")


def _labels(dialog) -> str:
    return " | ".join(w.text() for w in dialog.findChildren(QLabel))


def _photo(path, group=None) -> PhotoInfo:
    return PhotoInfo(path=path, duplicate_group_id=group)


_UNDER_TEST = (
    "_on_persons_thumbnails_ready_start_duplicates", "_start_duplicate_detection",
    "_update_corrupted_indicator", "_load_persisted_corrupted_paths",
    "_remove_persisted_corrupted_paths", "_show_corrupted_status_dialog",
    "_show_corrupted_list_dialog", "_apply_duplicate_results",
    "_show_duplicate_status_dialog", "_record_corrupted_files",
    "_offer_corrupted_repair", "_show_repair_result_dialog",
    "_offer_corrupted_delete", "_on_corrupted_delete_finished",
    "_on_duplicate_badge_clicked", "_on_duplicate_popup_navigate",
    "_on_duplicate_group_view_requested", "_on_duplicate_group_ignored",
    "show_duplicate_grid",
)


class _Host(QWidget):
    """The minimum a DuplicatesController needs — a real QWidget, since it is
    the parent of every dialog and thread it creates."""

    def __init__(self):
        super().__init__()
        self._catalog = _Rec(
            get_all_photo_paths_for_dedup=lambda: [_P("/a.jpg"), _P("/b.jpg")],
            get_duplicate_group_assignments=lambda: {_P("/a.jpg"): 1, _P("/b.jpg"): 1},
            get_photo_dates_for_dedup=lambda: {},
            count_duplicate_groups=lambda: 3,
            get_duplicates_for_group=lambda gid: [_photo("/a.jpg", gid),
                                                  _photo("/b.jpg", gid)],
        )
        self._grid = _Rec()
        self._sidebar = _Rec()
        self._sidebar.persons_thumbnails_ready = _Signal()
        self._viewer = _Rec(current_photo=lambda: None)
        self._duplicate_grid = _Rec()
        self._thumb_cache = _Rec()
        self._face_db = _Rec()
        self._folder_watcher = _Rec()
        self._stack = _Rec(currentIndex=lambda: 0)
        self._left_stack = _Rec()
        self._lbl_corrupted = _Rec()
        self._lbl_action = _Rec()
        self._lbl_fileinfo = _Rec()
        self._lbl_thumb_size = _Rec()
        self._thumb_slider = _Rec()
        self._lbl_zoom = _Rec()
        self._zoom_slider = _Rec()
        self._zoom_pct_label = _Rec()
        self._btn_grid_status = _Rec()
        self._act_faces_toggle = _Rec()
        self._act_exif_toggle = _Rec()
        self._btn_annotations_toggle = _Rec()
        self._status_bar = _Rec()

        self._duplicate_thread = None
        self._dup_migration_thread = None
        self._delete_thread = None
        self._duplicates_popup = None
        self._duplicate_ignored_paths: set = set()
        self._live_corrupted_paths: list = []
        self._dup_progress = None
        self._last_duplicate_check = None
        self._current_photos: list = []
        self._current_paths: set = set()
        self._current_photo_index = 0
        self._current_album_id = 7
        self._viewer_back_target = None

        self.log = _Rec()

    # -- collaborators of MainWindow, recorded ------------------------------
    def statusBar(self):
        return self._status_bar

    def show_viewer(self, photo):
        self.log.show_viewer(photo)

    def _navigate_to_photo_path(self, path):
        self.log.navigate_to_photo_path(path)

    def _update_status(self):
        self.log.update_status()


for _name in _UNDER_TEST:
    setattr(_Host, _name, getattr(DuplicatesController, _name))


@pytest.fixture
def host(qtbot):
    h = _Host()
    qtbot.addWidget(h)
    return h


@pytest.fixture
def detector(monkeypatch):
    """DuplicateDetectorThread, inert."""
    store: dict = {}
    monkeypatch.setattr(mwd, "DuplicateDetectorThread", _fake_thread(store, "dup"))
    return store


@pytest.fixture
def cache(monkeypatch):
    """DedupCache, so that no test ever touches the real dedup_cache.db."""
    built = []

    def _factory(*args, **kwargs):
        rec = _Rec(get_corrupted_paths=lambda: ["/broken.jpg"])
        built.append(rec)
        return rec

    monkeypatch.setattr(mwd, "DedupCache", _factory)
    return built


@pytest.fixture
def history(monkeypatch, tmp_path):
    """problems_history + APP_DATA_DIR, both resolved inside the method."""
    import src.core.app_dirs as app_dirs
    import src.core.problems_history as ph

    monkeypatch.setattr(app_dirs, "APP_DATA_DIR", tmp_path)
    rec = _Rec()
    monkeypatch.setattr(ph, "problems_history", rec)
    return rec


# ---------------------------------------------------------------- the passes

class TestStartDetection:
    def test_an_empty_library_starts_nothing(self, host, detector):
        host._catalog = _Rec(get_all_photo_paths_for_dedup=lambda: [])
        host._start_duplicate_detection()
        assert detector == {}

    def test_a_running_pass_is_never_doubled(self, host, detector):
        host._start_duplicate_detection()
        detector["dup"][0].running = True
        host._start_duplicate_detection()
        assert len(detector["dup"]) == 1

    def test_the_pass_is_seeded_with_the_known_groups(self, host, detector):
        """The trap of the incremental detection: without seed_groups every pair
        looks "already compared" and no group is ever reformed."""
        host._start_duplicate_detection()
        thread = detector["dup"][0]
        assert thread.started
        assert thread.kwargs["seed_groups"] == {_P("/a.jpg"): 1, _P("/b.jpg"): 1}
        assert host._duplicate_grid.last("set_scanning") == (True,)

    def test_the_ignored_paths_of_the_previous_pass_are_forgotten(self, host,
                                                                  detector):
        host._duplicate_ignored_paths = {"/old.jpg"}
        host._start_duplicate_detection()
        assert host._duplicate_ignored_paths == set()

    def test_the_progress_starts_at_two_stages_per_file(self, host, detector):
        host._start_duplicate_detection()
        current, total, _ = host._dup_progress
        assert (current, total) == (0, 4)      # 2 files x 2 tiers

    def test_a_partial_snapshot_is_applied_live(self, host, detector):
        host._start_duplicate_detection()
        thread = detector["dup"][0]
        thread.partial_results.emit({5: [_P("/a.jpg"), _P("/b.jpg")]}, ["/broken.jpg"])
        assert host._catalog.last("set_duplicate_groups") == (
            {_P("/a.jpg"): 5, _P("/b.jpg"): 5},)
        assert host._lbl_corrupted.called("show")

    def test_the_progress_signal_only_stores_the_state(self, host, detector):
        host._start_duplicate_detection()
        detector["dup"][0].progress.emit(3, 8, "Tier 1")
        assert host._dup_progress == (3, 8, "Tier 1")

    def test_the_end_of_a_pass_dates_it_and_stops_the_animation(self, host, detector):
        host._start_duplicate_detection()
        thread = detector["dup"][0]
        thread.corrupted_paths = []
        thread.finished.emit({})
        assert host._duplicate_grid.last("set_scanning") == (False,)
        assert isinstance(host._last_duplicate_check, datetime)
        assert host._dup_progress is None

    @pytest.mark.parametrize("signal, args", [("error", ("boum",)), ("cancelled", ())])
    def test_a_failed_or_cancelled_pass_leaves_no_animation_running(
            self, host, detector, signal, args):
        host._start_duplicate_detection()
        getattr(detector["dup"][0], signal).emit(*args)
        assert host._duplicate_grid.last("set_scanning") == (False,)
        assert host._dup_progress is None
        assert host._last_duplicate_check is None


class TestDeferredStart:
    def test_the_start_waits_for_the_person_thumbnails(self, host, detector):
        host._sidebar.persons_thumbnails_ready.connect(
            host._on_persons_thumbnails_ready_start_duplicates)
        host._on_persons_thumbnails_ready_start_duplicates()
        assert detector["dup"][0].started
        assert host._sidebar.persons_thumbnails_ready.slots == []

    def test_a_running_migration_defers_the_start(self, host, detector):
        """The migration of the groups with conflicting dates must be over before
        seed_groups is read, otherwise the pass starts from a stale state."""
        migration = _fake_thread({}, "m")()
        migration.running = True
        host._dup_migration_thread = migration
        host._on_persons_thumbnails_ready_start_duplicates()
        assert detector == {}
        migration.finished.emit()
        assert detector["dup"][0].started

    def test_an_already_disconnected_signal_never_breaks_the_start(self, host,
                                                                   detector):
        class _Dead:
            def disconnect(self, slot=None):
                raise RuntimeError("already deleted")

        host._sidebar.persons_thumbnails_ready = _Dead()
        host._on_persons_thumbnails_ready_start_duplicates()
        assert detector["dup"][0].started


# ------------------------------------------------------- corrupted indicator

class TestCorruptedIndicator:
    def test_the_counter_appears_with_the_number_of_files(self, host):
        host._update_corrupted_indicator(["/a.jpg", "/b.jpg"])
        assert "2" in host._lbl_corrupted.last("setText")[0]
        assert host._lbl_corrupted.called("show")

    def test_no_corrupted_file_hides_the_counter(self, host):
        host._update_corrupted_indicator([])
        assert host._lbl_corrupted.called("hide")
        assert host._lbl_corrupted.called("setText") == []

    def test_the_list_is_copied_not_aliased(self, host):
        paths = ["/a.jpg"]
        host._update_corrupted_indicator(paths)
        paths.append("/b.jpg")
        assert host._live_corrupted_paths == ["/a.jpg"]


class TestPersistedCorrupted:
    def test_the_persisted_list_is_read_then_the_cache_closed(self, host, cache):
        assert host._load_persisted_corrupted_paths() == ["/broken.jpg"]
        assert cache[0].called("close")

    def test_removing_nothing_never_even_opens_the_cache(self, host, cache):
        host._remove_persisted_corrupted_paths([])
        assert cache == []

    def test_the_repaired_paths_are_removed_and_the_cache_closed(self, host, cache):
        host._remove_persisted_corrupted_paths(iter(["/a.jpg"]))
        assert cache[0].last("remove_corrupted_paths") == (["/a.jpg"],)
        assert cache[0].called("close")


# ------------------------------------------------------- applying a snapshot

class TestApplyDuplicateResults:
    def test_the_groups_are_persisted_and_the_badge_updated(self, host):
        host._apply_duplicate_results({7: ["/a.jpg", "/b.jpg"]})
        assert host._catalog.last("set_duplicate_groups") == (
            {"/a.jpg": 7, "/b.jpg": 7},)
        assert host._sidebar.last("update_duplicates_badge") == (1,)

    def test_a_group_that_has_disappeared_is_explicitly_cleared(self, host):
        """A dissolved group, or one reduced to a singleton: without this second
        write the photos keep a duplicate_group_id pointing nowhere."""
        host._apply_duplicate_results({}, seed_groups={"/a.jpg": 1})
        assert host._catalog.called("set_duplicate_groups")[1][1] == (
            {"/a.jpg": None},)

    def test_a_group_ignored_during_the_pass_is_never_rewritten(self, host):
        host._duplicate_ignored_paths = {"/a.jpg"}
        host._apply_duplicate_results({7: ["/a.jpg", "/b.jpg"]})
        assert host._catalog.last("set_duplicate_groups") == ({"/b.jpg": 7},)

    def test_the_photos_in_memory_follow_the_snapshot(self, host):
        kept, gone = _photo("/a.jpg"), _photo("/z.jpg", 1)
        host._current_photos = [kept, gone]
        host._apply_duplicate_results({7: [_P("/a.jpg")]},
                                      seed_groups={_P("/z.jpg"): 1})
        assert kept.duplicate_group_id == 7
        assert gone.duplicate_group_id is None

    def test_the_grid_is_told_about_the_cleared_paths_too(self, host):
        host._apply_duplicate_results({7: ["/a.jpg"]}, seed_groups={"/z.jpg": 1})
        assert host._grid.last("refresh_duplicate_status") == (
            {"/a.jpg": 7, "/z.jpg": None},)

    def test_the_displayed_photo_gets_its_badge_refreshed(self, host):
        current = _photo("/a.jpg")
        host._viewer = _Rec(current_photo=lambda: current)
        host._apply_duplicate_results({7: [_P("/a.jpg")]})
        assert current.duplicate_group_id == 7
        assert host._viewer.called("_update_dup_badge")

    def test_a_displayed_photo_outside_the_snapshot_is_left_alone(self, host):
        current = _photo("/other.jpg", 4)
        host._viewer = _Rec(current_photo=lambda: current)
        host._apply_duplicate_results({7: [_P("/a.jpg")]})
        assert current.duplicate_group_id == 4
        assert host._viewer.called("_update_dup_badge") == []

    def test_the_visible_duplicates_grid_is_refreshed_at_once(self, host):
        host._stack = _Rec(currentIndex=lambda: 4)
        host._apply_duplicate_results({7: ["/a.jpg"]})
        assert host._duplicate_grid.called("refresh")

    def test_an_invisible_duplicates_grid_is_only_invalidated(self, host):
        host._apply_duplicate_results({7: ["/a.jpg"]})
        assert host._duplicate_grid.called("invalidate")
        assert host._duplicate_grid.called("refresh") == []


# --------------------------------------------------------------- the dialogs

class TestCorruptedStatusDialog:
    def test_without_a_corrupted_file_the_actions_are_disabled(self, host, dialogs):
        host._show_corrupted_status_dialog()
        dlg = dialogs[0]
        assert not _button(dlg, "List…").isEnabled()
        assert not _button(dlg, "Repair…").isEnabled()

    def test_with_corrupted_files_the_count_is_shown(self, host, dialogs):
        host._live_corrupted_paths = ["/a.jpg", "/b.jpg"]
        host._show_corrupted_status_dialog()
        assert "2" in _labels(dialogs[0])
        assert _button(dialogs[0], "Repair…").isEnabled()

    def test_the_list_button_chains_to_the_detailed_dialog(self, host, dialogs):
        host._live_corrupted_paths = ["/a.jpg"]
        host._show_corrupted_status_dialog()
        _button(dialogs[0], "List…").click()
        assert len(dialogs) == 2                 # the detailed list opened

    def test_the_repair_button_hands_over_the_whole_list(self, host, dialogs):
        seen = []
        host._offer_corrupted_repair = lambda paths, on_done=None: seen.append(paths)
        host._live_corrupted_paths = ["/a.jpg", "/b.jpg"]
        host._show_corrupted_status_dialog()
        _button(dialogs[0], "Repair…").click()
        assert seen == [["/a.jpg", "/b.jpg"]]


class TestCorruptedListDialog:
    def test_an_empty_list_opens_nothing(self, host, dialogs):
        host._show_corrupted_list_dialog()
        assert dialogs == []

    def test_the_files_are_listed_with_their_count(self, host, dialogs):
        host._live_corrupted_paths = ["/a.jpg", "/b.jpg"]
        host._show_corrupted_list_dialog()
        widget = dialogs[0].findChild(QListWidget)
        assert [widget.item(i).text() for i in range(widget.count())] == [
            "/a.jpg", "/b.jpg"]
        assert "2" in _labels(dialogs[0])

    def test_without_a_selection_the_action_covers_the_whole_list(self, host,
                                                                  dialogs):
        seen = []
        host._offer_corrupted_delete = lambda paths: seen.append(paths)
        host._live_corrupted_paths = ["/a.jpg", "/b.jpg"]
        host._show_corrupted_list_dialog()
        _button(dialogs[0], "Delete…").click()
        assert seen == [["/a.jpg", "/b.jpg"]]

    def test_a_selection_targets_only_the_selected_files(self, host, dialogs):
        seen = []
        host._offer_corrupted_repair = lambda paths, on_done=None: seen.append(paths)
        host._live_corrupted_paths = ["/a.jpg", "/b.jpg"]
        host._show_corrupted_list_dialog()
        dialogs[0].findChild(QListWidget).item(0).setSelected(True)
        _button(dialogs[0], "Repair…").click()
        assert seen == [["/a.jpg"]]

    def test_the_list_is_refreshed_after_a_deletion(self, host, dialogs):
        """The dialog stays open: it must show what is left, not the list it was
        built with."""
        host._live_corrupted_paths = ["/a.jpg", "/b.jpg"]

        def _delete(paths):
            host._live_corrupted_paths = ["/b.jpg"]

        host._offer_corrupted_delete = _delete
        host._show_corrupted_list_dialog()
        _button(dialogs[0], "Delete…").click()
        widget = dialogs[0].findChild(QListWidget)
        assert [widget.item(i).text() for i in range(widget.count())] == ["/b.jpg"]

    def test_an_emptied_list_disables_the_two_actions(self, host, dialogs):
        host._live_corrupted_paths = ["/a.jpg"]
        host._offer_corrupted_delete = lambda paths: setattr(
            host, "_live_corrupted_paths", [])
        host._show_corrupted_list_dialog()
        _button(dialogs[0], "Delete…").click()
        assert not _button(dialogs[0], "Delete…").isEnabled()
        assert not _button(dialogs[0], "Repair…").isEnabled()


class TestDuplicateStatusDialog:
    def test_the_state_is_read_only_and_dated(self, host, dialogs):
        host._last_duplicate_check = datetime(2026, 8, 24, 10, 30)
        host._show_duplicate_status_dialog()
        text = _labels(dialogs[0])
        assert "3" in text                        # 3 groups
        assert "2026" in text

    def test_never_checked_is_said_as_such(self, host, dialogs):
        host._show_duplicate_status_dialog()
        assert "never" in _labels(dialogs[0]).lower()

    def test_check_now_starts_a_pass(self, host, dialogs, detector):
        host._show_duplicate_status_dialog()
        btn = _button(dialogs[0], "Check now")
        assert btn.isEnabled()
        btn.click()
        assert detector["dup"][0].started

    def test_view_the_groups_switches_to_the_duplicates_grid(self, host, dialogs):
        host._show_duplicate_status_dialog()
        _button(dialogs[0], "View the groups").click()
        assert host._stack.last("setCurrentIndex") == (4,)

    def test_a_running_pass_shows_a_live_progress_bar(self, host, dialogs):
        thread = _fake_thread({}, "t")()
        thread.running = True
        host._duplicate_thread = thread
        host._dup_progress = (2, 10, "Tier 1")
        host._show_duplicate_status_dialog()

        bar = dialogs[0].findChild(QProgressBar)
        assert (bar.value(), bar.maximum()) == (2, 10)
        assert not _button(dialogs[0], "Check now").isEnabled()

        thread.progress.emit(7, 10, "Tier 2")
        assert bar.value() == 7
        assert "Tier 2" in _labels(dialogs[0])

    def test_a_progress_beyond_the_total_is_clamped(self, host, dialogs):
        thread = _fake_thread({}, "t")()
        thread.running = True
        host._duplicate_thread = thread
        host._dup_progress = (0, 0, "")
        host._show_duplicate_status_dialog()
        thread.progress.emit(50, 10, "")
        bar = dialogs[0].findChild(QProgressBar)
        assert bar.value() == bar.maximum() == 10

    def test_a_pass_finishing_while_the_dialog_is_open_unfreezes_it(self, host,
                                                                    dialogs):
        thread = _fake_thread({}, "t")()
        thread.running = True
        host._duplicate_thread = thread
        host._show_duplicate_status_dialog()
        host._last_duplicate_check = datetime(2026, 8, 24, 11, 0)
        thread.finished.emit({})
        bar = dialogs[0].findChild(QProgressBar)
        assert bar.value() == bar.maximum()
        assert _button(dialogs[0], "Check now").isEnabled()
        assert "2026" in _labels(dialogs[0])

    def test_a_pass_cancelled_with_no_check_ever_done_says_never(self, host,
                                                                  dialogs):
        thread = _fake_thread({}, "t")()
        thread.running = True
        host._duplicate_thread = thread
        host._show_duplicate_status_dialog()
        thread.cancelled.emit()
        assert "never" in _labels(dialogs[0]).lower()

    def test_a_thread_already_gone_never_breaks_the_closing(self, host, dialogs):
        """The pass may have been destroyed while the dialog stayed open: the
        cleanup must go through its four signals whatever happens."""
        class _Fragile(_Signal):
            def disconnect(self, slot=None):
                raise TypeError("signal deja detruit")

        thread = _fake_thread({}, "t")()
        thread.running = True
        thread.progress = _Fragile()
        host._duplicate_thread = thread
        host._show_duplicate_status_dialog()
        dialogs[0].finished.emit(0)
        assert thread.finished.slots == []      # the three others were reached

    def test_closing_the_dialog_disconnects_it_from_the_thread(self, host, dialogs):
        thread = _fake_thread({}, "t")()
        thread.running = True
        host._duplicate_thread = thread
        host._show_duplicate_status_dialog()
        assert thread.progress.slots
        dialogs[0].finished.emit(0)
        assert thread.progress.slots == []


# --------------------------------------------------------- repair & deletion

class TestRecordCorruptedFiles:
    def test_everything_repaired_writes_no_list(self, host, history):
        assert host._record_corrupted_files(3, 3, []) is None
        assert history.last("add_entry") == (3, 3, None)

    def test_the_files_still_failing_are_written_down(self, host, history):
        path = host._record_corrupted_files(3, 1, ["/a.jpg", "/b.jpg"])
        assert path is not None
        with open(path, encoding="utf-8") as fh:
            assert fh.read().splitlines() == ["/a.jpg", "/b.jpg"]
        assert history.last("add_entry")[2] == path

    def test_an_unwritable_folder_still_records_the_entry(self, host, monkeypatch,
                                                          history, tmp_path):
        import src.core.app_dirs as app_dirs
        monkeypatch.setattr(app_dirs, "APP_DATA_DIR", tmp_path / "nope" / "deeper")
        assert host._record_corrupted_files(1, 0, ["/a.jpg"]) is None
        assert history.last("add_entry") == (1, 0, None)


@pytest.fixture
def repair(monkeypatch):
    """FileRepairThread + QProgressDialog, both imported inside the method."""
    import src.library.file_repair as fr
    from PySide6 import QtWidgets

    store: dict = {}
    monkeypatch.setattr(fr, "FileRepairThread", _fake_thread(store, "repair"))

    class _Progress(_Rec):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.canceled = _Signal()

    monkeypatch.setattr(QtWidgets, "QProgressDialog", _Progress)
    return store


class TestOfferRepair:
    def test_a_refusal_records_the_files_and_says_where_to_find_them(
            self, host, box, repair, history):
        box.answer = box.No
        host._offer_corrupted_repair(["/a.jpg"])
        assert repair == {}
        assert history.called("add_entry")
        assert len(box.infos) == 2           # the question, then the summary

    def test_an_acceptance_starts_the_repair_thread(self, host, box, repair,
                                                    history):
        host._offer_corrupted_repair(["/a.jpg", "/b.jpg"])
        thread = repair["repair"][0]
        assert thread.started
        assert thread.args[0] == ["/a.jpg", "/b.jpg"]

    def test_the_progress_never_climbs_out_of_the_thread(self, host, box, repair,
                                                          history):
        host._offer_corrupted_repair(["/a.jpg"])
        repair["repair"][0].progress.emit(0, 1, "/a.jpg")   # must not raise

    def test_the_repaired_files_lose_their_stale_thumbnail(self, host, box, repair,
                                                           history, cache, dialogs):
        """The content changed on disk: without the invalidation the grid keeps
        the truncated thumbnail until the next restart."""
        host._live_corrupted_paths = ["/a.jpg", "/b.jpg"]
        host._offer_corrupted_repair(["/a.jpg", "/b.jpg"])
        repair["repair"][0].finished.emit(1, ["/b.jpg"])
        assert host._thumb_cache.last("invalidate_many") == (["/a.jpg"],)
        assert host._grid.last("refresh_photo") == ("/a.jpg", None)
        assert host._live_corrupted_paths == ["/b.jpg"]
        assert cache[0].last("remove_corrupted_paths") == (["/a.jpg"],)

    def test_the_end_of_the_repair_calls_back_the_caller(self, host, box, repair,
                                                         history, cache, dialogs):
        done = []
        host._offer_corrupted_repair(["/a.jpg"], on_done=lambda: done.append(1))
        repair["repair"][0].finished.emit(1, [])
        assert done == [1]
        assert dialogs                        # the summary dialog was shown


class TestRepairResultDialog:
    def test_the_two_lists_are_shown_separately(self, host, dialogs):
        host._show_repair_result_dialog(["/ok.jpg"], ["/ko.jpg"])
        lists = dialogs[0].findChildren(QListWidget)
        assert [w.item(0).text() for w in lists] == ["/ok.jpg", "/ko.jpg"]

    def test_without_a_failure_no_delete_button_is_offered(self, host, dialogs):
        host._show_repair_result_dialog(["/ok.jpg"], [])
        with pytest.raises(AssertionError):
            _button(dialogs[0], "Delete these files…")

    def test_the_files_still_failing_can_be_deleted_from_there(self, host, dialogs):
        seen = []
        host._offer_corrupted_delete = lambda paths: seen.append(paths)
        host._show_repair_result_dialog([], ["/ko.jpg"])
        _button(dialogs[0], "Delete these files…").click()
        assert seen == [["/ko.jpg"]]


@pytest.fixture
def worker(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(mwd, "_DeleteWorkerThread", _fake_thread(store, "del"))
    return store


class TestOfferDelete:
    def test_a_refusal_deletes_nothing(self, host, box, worker):
        box.answer = box.Cancel
        host._offer_corrupted_delete(["/a.jpg"])
        assert worker == {}

    def test_a_running_deletion_is_never_doubled(self, host, box, worker):
        host._delete_thread = _fake_thread({}, "x")()
        host._delete_thread.running = True
        host._offer_corrupted_delete(["/a.jpg"])
        assert worker == {}
        assert host._status_bar.called("showMessage")

    def test_the_deletion_warns_the_watcher_before_starting(self, host, box, worker):
        """Without notify_self_deletions the folder watcher sees the files vanish
        and triggers a rescan of the folder."""
        host._offer_corrupted_delete(["/a.jpg"])
        assert host._folder_watcher.last("notify_self_deletions") == (["/a.jpg"],)
        assert worker["del"][0].started

    def test_the_progress_is_shown_in_the_status_bar(self, host, box, worker):
        host._offer_corrupted_delete(["/a.jpg"])
        worker["del"][0].progress.emit(1, 2)
        assert "1" in host._lbl_action.last("setText")[0]


@pytest.fixture
def deleted_registry(monkeypatch):
    import src.core.deleted_corrupted_files as dcf
    rec = _Rec()
    monkeypatch.setattr(dcf, "deleted_corrupted_files", rec)
    return rec


class TestDeleteFinished:
    def test_the_deleted_files_leave_the_grid_and_the_state(self, host, box, cache,
                                                            deleted_registry):
        host._current_photos = [_photo("/a.jpg"), _photo("/b.jpg")]
        host._current_paths = {_P("/a.jpg"), _P("/b.jpg")}
        host._live_corrupted_paths = [_P("/a.jpg"), _P("/b.jpg")]
        host._on_corrupted_delete_finished([_P("/a.jpg")], [])
        assert host._grid.last("remove_photos") == ([_P("/a.jpg")],)
        assert [p.path for p in host._current_photos] == [_P("/b.jpg")]
        assert host._current_paths == {_P("/b.jpg")}
        assert host._live_corrupted_paths == [_P("/b.jpg")]

    def test_the_deleted_files_stay_findable_afterwards(self, host, box, cache,
                                                        deleted_registry):
        host._on_corrupted_delete_finished(["/a.jpg"], [])
        assert deleted_registry.last("add_deleted") == (["/a.jpg"],)
        assert "1" in box.infos[0][1]

    def test_the_duplicates_badge_is_recomputed(self, host, box, cache,
                                                deleted_registry):
        host._on_corrupted_delete_finished(["/a.jpg"], [])
        assert host._sidebar.last("update_duplicates_badge") == (3,)
        assert host._duplicate_grid.called("invalidate")

    def test_the_errors_are_reported_instead_of_the_summary(self, host, box, cache,
                                                            deleted_registry):
        host._on_corrupted_delete_finished(["/a.jpg"], ["/b.jpg: acces refuse"])
        assert box.warnings and "/b.jpg" in box.warnings[0][1]
        assert box.infos == []

    def test_nothing_deleted_and_no_error_says_nothing(self, host, box, cache,
                                                       deleted_registry):
        host._on_corrupted_delete_finished([], [])
        assert box.infos == [] and box.warnings == []
        assert host._grid.called("remove_photos") == []


# -------------------------------------------------------- badge, popup, grid

@pytest.fixture
def popup(monkeypatch):
    built = []

    class _Popup(_Rec):
        def __init__(self, photo, others, parent):
            super().__init__(width=lambda: 100, height=lambda: 80)
            self.photo = photo
            self.others = others
            self.navigate_requested = _Signal()
            built.append(self)

    monkeypatch.setattr(mwd, "_DuplicatesPopup", _Popup)
    return built


class TestDuplicateBadge:
    def test_a_photo_without_a_group_opens_nothing(self, host, popup):
        host._on_duplicate_badge_clicked(_photo("/a.jpg"))
        assert popup == []

    def test_a_group_reduced_to_the_photo_itself_opens_nothing(self, host, popup):
        host._catalog = _Rec(
            get_duplicates_for_group=lambda gid: [_photo("/a.jpg", gid)])
        host._on_duplicate_badge_clicked(_photo("/a.jpg", 1))
        assert popup == []

    def test_the_popup_lists_the_other_copies_only(self, host, popup):
        host._on_duplicate_badge_clicked(_photo("/a.jpg", 1))
        assert [p.path for p in popup[0].others] == [_P("/b.jpg")]
        assert popup[0].called("show")

    def test_a_second_click_closes_the_previous_popup(self, host, popup):
        host._on_duplicate_badge_clicked(_photo("/a.jpg", 1))
        first = popup[0]
        host._on_duplicate_badge_clicked(_photo("/a.jpg", 1))
        assert first.called("close")
        assert host._duplicates_popup is popup[1]

    def test_the_popup_navigates_to_the_clicked_copy(self, host, popup):
        host._on_duplicate_badge_clicked(_photo("/a.jpg", 1))
        popup[0].navigate_requested.emit(_P("/b.jpg"))
        assert host.log.last("navigate_to_photo_path") == (_P("/b.jpg"),)


class TestPopupNavigation:
    def test_from_the_viewer_the_comparison_stays_in_the_viewer(self, host):
        host._stack = _Rec(currentIndex=lambda: 1)
        photos = [_photo("/a.jpg", 1), _photo("/b.jpg", 1)]
        host._on_duplicate_popup_navigate(photos[1].path, photos)
        assert host._current_photo_index == 1
        assert host._current_album_id is None
        assert host.log.last("show_viewer") == (photos[1],)

    def test_an_unknown_path_falls_back_on_the_first_copy(self, host):
        host._stack = _Rec(currentIndex=lambda: 1)
        photos = [_photo("/a.jpg", 1)]
        host._on_duplicate_popup_navigate("/gone.jpg", photos)
        assert host._current_photo_index == 0

    def test_from_the_grid_the_classic_navigation_is_used(self, host):
        photos = [_photo("/a.jpg", 1)]
        host._on_duplicate_popup_navigate("/a.jpg", photos)
        assert host.log.last("navigate_to_photo_path") == ("/a.jpg",)
        assert "show_viewer" not in host.log.names()


class TestGroupView:
    def test_an_empty_group_opens_nothing(self, host):
        host._catalog = _Rec(get_duplicates_for_group=lambda gid: [])
        host._on_duplicate_group_view_requested(1)
        assert host.log.calls == []

    def test_the_group_is_opened_in_the_viewer_with_a_way_back(self, host):
        host._on_duplicate_group_view_requested(1)
        assert len(host._current_photos) == 2
        assert host._current_photo_index == 0
        assert host._viewer_back_target == "duplicate_grid"
        assert host._current_album_id is None


class TestGroupIgnored:
    def test_the_group_is_dissolved_persistently(self, host):
        host._on_duplicate_group_ignored(1)
        assert host._catalog.last("ignore_duplicate_group") == (1,)
        assert host._duplicate_grid.last("remove_group") == (1,)
        assert host._sidebar.last("update_duplicates_badge") == (3,)

    def test_the_paths_are_shielded_from_the_pass_in_progress(self, host):
        """The detection thread may still carry them merged in its in-memory
        state: without this set, its next snapshot would recreate the group."""
        host._on_duplicate_group_ignored(1)
        assert host._duplicate_ignored_paths == {_P("/a.jpg"), _P("/b.jpg")}

    def test_the_photos_of_the_grid_lose_their_badge(self, host):
        photo = _photo("/a.jpg", 1)
        other = _photo("/c.jpg", 2)
        host._current_photos = [photo, other]
        host._on_duplicate_group_ignored(1)
        assert photo.duplicate_group_id is None
        assert other.duplicate_group_id == 2
        assert host._grid.last("refresh_duplicate_status") == ({photo.path: None},)

    def test_the_displayed_photo_loses_its_badge_too(self, host):
        current = _photo("/a.jpg", 1)
        host._viewer = _Rec(current_photo=lambda: current)
        host._on_duplicate_group_ignored(1)
        assert current.duplicate_group_id is None
        assert host._viewer.called("_update_dup_badge")

    def test_a_displayed_photo_of_another_group_is_left_alone(self, host):
        current = _photo("/c.jpg", 2)
        host._viewer = _Rec(current_photo=lambda: current)
        host._on_duplicate_group_ignored(1)
        assert current.duplicate_group_id == 2
        assert host._viewer.called("_update_dup_badge") == []


class TestShowDuplicateGrid:
    def test_the_view_is_loaded_and_the_photo_controls_hidden(self, host):
        host.show_duplicate_grid()
        assert host._duplicate_grid.called("ensure_loaded")
        assert host._stack.last("setCurrentIndex") == (4,)
        assert host._left_stack.last("setCurrentIndex") == (0,)
        hidden = [w for w in (host._lbl_thumb_size, host._thumb_slider,
                              host._lbl_zoom, host._zoom_slider,
                              host._zoom_pct_label, host._btn_grid_status)
                  if w.called("hide")]
        assert len(hidden) == 6
        assert host._act_faces_toggle.last("setVisible") == (False,)
        assert host._lbl_fileinfo.last("setText") == ("",)
