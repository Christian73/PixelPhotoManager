# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Faces & people controller (main_window_faces.py::FacesController).

As in test_similarity_search_triggers.py and test_main_window_face_rotation.py,
the REAL methods are called unbound against a minimal host carrying only what
the tested path reads: no MainWindow, no database, no OS thread. What is checked
here is the wiring -- which collaborator is called, in which order, and above
all which branch is taken -- since almost every method of this controller is a
chain of side effects whose only visible trace is a call on another object.
"""
import os
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

import src.faces.picasa_importer as picasa_importer
import src.ui.index_errors_dialog as index_errors_dialog
import src.ui.main_window_faces as mwf
from src.core.models import PersonInfo, PhotoInfo
from src.ui.main_window_faces import FacesController, _PERSON_CTX_PREFIX
from src.ui.reset_faces_dialog import _ResetFacesDialog
from tests.gui_widgets.controller_doubles import (
    Recorder as _Rec, RecordingSignal as _Signal, fake_thread as _fake_thread,
    make_message_box,
)


_THREADS = (
    "_PersonsRefreshThread", "_ResuggestThread", "_ResetWorkerThread",
    "RetryFaceIndexThread", "ForceRedetectThread", "SimilaritySearchThread",
    "ClusterThread", "FaceIndexThread",
)


@pytest.fixture
def threads(monkeypatch):
    """Every background thread of the controller, replaced by an inert double.

    Returns a dict name -> [instances], so a test can assert both that the right
    thread was created and that it was really started."""
    store: dict = {}
    for name in _THREADS:
        monkeypatch.setattr(mwf, name, _fake_thread(store, name))
    return store


@pytest.fixture
def box(monkeypatch):
    """The controller shows QMessageBox after QMessageBox: intercepting them is
    what makes these paths testable without a human in front of the screen."""
    fake = make_message_box()
    monkeypatch.setattr(mwf, "QMessageBox", fake)
    return fake


# ------------------------------------------------------------------- the host

_UNDER_TEST = (
    "_open_index_errors_dialog", "_on_index_errors_dialog_closed",
    "_on_index_error_dialog_retry", "_maybe_prompt_picasa_for_new_folder",
    "_on_folder_picasa_import_finished", "_reset_and_reindex_faces",
    "_on_reset_done", "_on_similarity_progress", "_on_similarity_finished",
    "_on_face_progress", "_on_face_index_error", "_on_picasa_edits_imported",
    "_on_face_restore_completed", "_run_clustering", "_on_clustering_finished",
    "_refresh_face_panel_if_visible", "_on_cluster_named", "_on_cluster_assigned",
    "_on_clusters_named", "_on_clusters_assigned", "_on_cluster_ignored",
    "_on_clusters_ignored", "_on_cluster_merged", "_on_cluster_photos_requested",
    "_show_merge_dialog", "_on_person_rename_requested", "_on_person_clear_requested",
    "_on_person_selected", "_on_cover_face_set", "_refresh_persons",
    "_update_persons_counts", "_on_persons_refreshed", "_on_persons_counts_updated",
    "_on_face_highlighted", "_on_all_faces_toggled", "_on_face_context_menu",
    "_on_add_face_mode_requested", "_on_face_panel_person_cluster_requested",
    "show_face_clusters", "show_person_clusters", "_on_person_cluster_photos_requested",
    "_on_person_cluster_photo_requested", "_on_person_cluster_back",
    "_on_pcv_cluster_unassigned", "_on_suggestion_accepted", "_on_suggestion_rejected",
    "_on_all_suggestions_accepted", "_on_all_suggestions_rejected",
    "_on_single_reindex_finished", "_on_retry_face_index_requested",
    "_on_retry_face_index_finished", "_on_force_redetect_requested",
    "_on_force_redetect_finished", "_start_clustering_with_confirm",
    "_import_from_picasa", "_backup_faces", "_manage_face_backups",
    "_show_face_counters", "_on_face_unavailable", "_open_people_dialog",
    "_on_person_merge_requested",
)


class _Host(QWidget):
    """The minimum a FacesController needs. A QWidget because the controller
    passes `self` as the parent of its dialogs and threads."""

    def __init__(self):
        super().__init__()
        self._face_db = _Rec(
            get_photos_for_cluster=lambda cid: [f"/p/{cid}.jpg"],
            get_photos_for_person=lambda pid: [f"/q/{pid}.jpg"],
            get_person_photo_count=lambda pid: 7,
            get_error_paths=lambda: ["/err.jpg"],
            count_embeddings=lambda: 100,
            count_identified_faces=lambda: 40,
        )
        self._face_db._db_path = "faces.db"    # an attribute, not a call
        self._catalog = _Rec(
            create_person=lambda name: PersonInfo(id=9, name=name),
            get_person=lambda pid: PersonInfo(id=pid, name="Alice"),
            get_photos_by_paths=lambda paths: [PhotoInfo(path=p) for p in paths],
            get_photo_by_path=lambda p: PhotoInfo(path=p),
        )
        self._catalog._db_path = "catalog.db"
        self._thumb_cache = _Rec()
        self._config = _Rec(get=lambda key, default=None: default)
        self._act_picasa = _Rec()
        self._edit_db = _Rec()
        self._face_cluster_grid = _Rec()
        self._face_panel = _Rec(isVisible=False)
        self._person_cluster_view = _Rec()
        self._person_cluster_view.current_person = None
        self._grid = _Rec()
        self._sidebar = _Rec()
        self._viewer = _Rec()
        self._stack = _Rec(currentWidget=None)
        self._left_stack = _Rec()
        self._lbl_action = _Rec()
        self._lbl_grid_nav = _Rec()
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
        self._act_cluster_faces = _Rec()
        self._grid_nav_bar = _Rec()
        self._sb_progress_bar = _Rec()
        self._similarity_debounce = _Rec()
        self._face_panel_refresh_timer = _Rec()

        self._index_errors_dialog = None
        self._current_photos: list = []
        self._current_photo_index = 0
        self._current_context = "Toutes les photos"
        self._current_album_id = 3
        self._pending_person_view_id = None
        self._from_person_cluster_view = False
        self._viewer_back_target = None
        self._face_indexer = None
        self._cluster_thread = None
        self._persons_refresh_thread = None
        self._retry_face_thread = None
        self._force_redetect_thread = None
        self._reset_worker = None
        self._cluster_start_time = None

        # what the tested paths call but that lives outside the controller
        self.log = _Rec()

    # -- collaborators of MainWindow, recorded ------------------------------
    def _start_photo_query(self, loader, context):
        self.log.start_photo_query(loader, context)

    def show_grid(self):
        self.log.show_grid()

    def show_viewer(self, photo):
        self.log.show_viewer(photo)

    def _update_status(self):
        self.log.update_status()

    def _on_delete_requested(self, photos):
        self.log.delete_requested(photos)

    def _drain_pending_reindex(self):
        self.log.drain_pending_reindex()

    def _start_face_indexing(self):
        self.log.start_face_indexing()

    def _schedule_similarity_search(self):
        self.log.schedule_similarity_search()

    def _start_similarity_search(self):
        self.log.start_similarity_search()

    def statusBar(self):
        return self._status_bar


for _name in _UNDER_TEST:
    setattr(_Host, _name, getattr(FacesController, _name))


@pytest.fixture
def host(qtbot):
    h = _Host()
    qtbot.addWidget(h)
    h._status_bar = _Rec()
    return h


# ------------------------------------------------------------ indexing errors

class TestIndexErrorsDialog:
    @pytest.fixture
    def dialogs(self, monkeypatch):
        built = []

        class _Dlg(_Rec):
            def __init__(self, *args):
                super().__init__()
                self.args = args
                self.retry_requested = _Signal()
                self.finished = _Signal()
                built.append(self)

        monkeypatch.setattr(index_errors_dialog, "IndexErrorsDialog", _Dlg)
        return built

    def test_the_dialog_is_opened_once_and_kept(self, host, dialogs):
        host._open_index_errors_dialog()
        assert len(dialogs) == 1
        assert host._index_errors_dialog is dialogs[0]
        assert "show" in dialogs[0].names()

    def test_a_second_call_raises_the_existing_one(self, host, dialogs):
        host._open_index_errors_dialog()
        host._open_index_errors_dialog()
        assert len(dialogs) == 1                     # never a second window
        assert "raise_" in dialogs[0].names()
        assert "activateWindow" in dialogs[0].names()

    def test_closing_frees_the_slot(self, host, dialogs):
        host._open_index_errors_dialog()
        host._on_index_errors_dialog_closed()
        assert host._index_errors_dialog is None
        host._open_index_errors_dialog()
        assert len(dialogs) == 2

    def test_the_retry_reuses_the_photo_of_the_grid(self, host, threads, box):
        photo = PhotoInfo(path="/lib/a.jpg")
        host._current_photos = [photo]
        host._on_index_error_dialog_retry("/lib/a.jpg")
        assert threads["RetryFaceIndexThread"][0].args[1] == photo.path

    def test_the_retry_rebuilds_an_unknown_photo(self, host, threads, box):
        """A file in error may no longer be in the displayed grid: the dialog
        must still be able to retry it."""
        host._on_index_error_dialog_retry(os.path.join("/lib", "ghost.jpg"))
        started = threads["RetryFaceIndexThread"][0]
        assert started.started
        assert started.args[1].endswith("ghost.jpg")


# -------------------------------------------------------------- picasa import

class TestPicasaPromptForNewFolder:
    @pytest.fixture
    def picasa(self, monkeypatch):
        store: dict = {}
        monkeypatch.setattr(picasa_importer, "PicasaImportThread",
                            _fake_thread(store, "thread"))
        return store

    def _scan(self, monkeypatch, contacts, photos, edits):
        monkeypatch.setattr(picasa_importer, "scan",
                            lambda folders: (contacts, photos, edits))

    def test_a_folder_without_picasa_data_asks_nothing(self, host, box, picasa,
                                                       monkeypatch):
        self._scan(monkeypatch, 0, 0, 0)
        host._maybe_prompt_picasa_for_new_folder("/photos")
        assert box.infos == []
        assert picasa == {}

    def test_a_refused_import_starts_nothing(self, host, box, picasa, monkeypatch):
        self._scan(monkeypatch, 3, 12, 0)
        box.answer = box.StandardButton.No
        host._maybe_prompt_picasa_for_new_folder("/photos")
        assert len(box.infos) == 1                   # the question was asked
        assert picasa == {}

    def test_an_accepted_import_starts_the_thread_for_that_folder_only(
            self, host, box, picasa, monkeypatch):
        self._scan(monkeypatch, 3, 12, 4)
        host._maybe_prompt_picasa_for_new_folder("/photos")
        thread = picasa["thread"][0]
        assert thread.started
        assert thread.args[2] == ["/photos"]
        assert host._lbl_action.last("setText")[0]   # a message in the status bar

    def test_the_question_details_both_counts(self, host, box, picasa, monkeypatch):
        self._scan(monkeypatch, 3, 12, 4)
        host._maybe_prompt_picasa_for_new_folder("/photos")
        _, body = box.infos[0]
        assert "12" in body and "4" in body


class TestPicasaImportFinished:
    def _result(self, **kw):
        base = dict(edited_map={}, persons_created=2, faces_imported=30,
                    photos_processed=11, edits_imported=0)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_the_report_counts_people_faces_and_photos(self, host, box):
        host._on_folder_picasa_import_finished(self._result())
        _, body = box.infos[0]
        assert "2" in body and "30" in body and "11" in body
        assert host._lbl_action.last("setText") == ("",)

    def test_the_edits_are_only_mentioned_when_there_are_some(self, host, box):
        host._on_folder_picasa_import_finished(self._result())
        assert "edit" not in box.infos[0][1]
        host._on_folder_picasa_import_finished(self._result(edits_imported=5))
        assert "5" in box.infos[1][1]

    def test_the_imported_edits_refresh_their_thumbnails(self, host, box):
        edits = {"/a.jpg": object(), "/b.jpg": object()}
        host._on_folder_picasa_import_finished(self._result(edited_map=edits))
        assert len(host._grid.called("refresh_photo")) == 2


# --------------------------------------------------------------------- resets

class TestResetAndReindex:
    @pytest.fixture
    def reset_dialog(self, monkeypatch):
        state = SimpleNamespace(accepted=True, choice=_ResetFacesDialog.RESET_FULL)

        class _Dlg:
            RESET_CLUSTERING = _ResetFacesDialog.RESET_CLUSTERING

            def __init__(self, parent=None):
                self.choice = state.choice

            def exec(self):
                return mwf.QDialog.Accepted if state.accepted else mwf.QDialog.Rejected

        monkeypatch.setattr(mwf, "_ResetFacesDialog", _Dlg)
        return state

    def test_a_cancelled_dialog_resets_nothing(self, host, threads, reset_dialog):
        reset_dialog.accepted = False
        host._reset_and_reindex_faces()
        assert threads == {}

    def test_the_worker_receives_the_running_threads_to_wait_for(
            self, host, threads, reset_dialog):
        indexer = _fake_thread({}, "x")()
        indexer.running = True
        indexer.cluster_requested = _Signal()
        host._face_indexer = indexer
        cluster = _fake_thread({}, "y")()
        cluster.running = True
        host._cluster_thread = cluster

        host._reset_and_reindex_faces()

        worker = threads["_ResetWorkerThread"][0]
        assert worker.started
        assert worker.args[2] == [indexer, cluster]
        # the indexer is asked to stop; the clustering, which has no stop(),
        # is only waited for
        assert indexer.stopped and not cluster.stopped

    def test_without_a_running_thread_the_worker_still_starts(
            self, host, threads, reset_dialog):
        host._reset_and_reindex_faces()
        worker = threads["_ResetWorkerThread"][0]
        assert worker.args[2] == []
        assert worker.started

    def test_a_clustering_reset_regroups_and_keeps_the_errors(self, host, box):
        """reset_clustering() does not empty face_index_errors: the badges of the
        photos in error must survive it."""
        host._on_reset_done(_ResetFacesDialog.RESET_CLUSTERING)
        assert host._grid.called("set_index_error_paths") == []
        assert "start_face_indexing" not in host.log.names()
        assert host._face_cluster_grid.called("refresh")

    def test_a_full_reset_clears_the_errors_and_reindexes(self, host, box):
        host._on_reset_done(_ResetFacesDialog.RESET_FULL)
        assert host._grid.last("set_index_error_paths") == ([],)
        assert "start_face_indexing" in host.log.names()


# ------------------------------------------------------------- status reports

class TestProgressReports:
    def test_the_similarity_progress_drives_the_bar(self, host):
        host._on_similarity_progress(3, 10)
        assert host._sb_progress_bar.last("setRange") == (0, 10)
        assert host._sb_progress_bar.last("setValue") == (3,)
        assert "3" in host._lbl_action.last("setText")[0]

    def test_a_zero_total_never_makes_an_empty_range(self, host):
        host._on_similarity_progress(0, 0)
        assert host._sb_progress_bar.last("setRange") == (0, 1)

    def test_the_end_of_the_search_reports_in_the_status_bar(self, host):
        host._on_similarity_finished(4, 20)
        assert host._sb_progress_bar.called("hide")
        message = host._status_bar.last("showMessage")[0]
        assert "4" in message and "20" in message

    def test_the_person_view_is_refreshed_only_if_something_was_found(self, host):
        host._stack = _Rec(currentWidget=lambda: host._person_cluster_view)
        host._on_similarity_finished(0, 20)
        assert host._person_cluster_view.called("refresh") == []
        host._on_similarity_finished(2, 20)
        assert host._person_cluster_view.called("refresh")

    def test_the_face_analysis_announces_its_start_then_its_progress(self, host):
        host._on_face_progress(0, 500)
        first = host._lbl_action.last("setText")[0]
        host._on_face_progress(120, 500)
        second = host._lbl_action.last("setText")[0]
        assert "500" not in first          # the start says nothing about a total
        assert "120" in second and "500" in second

    def test_an_indexing_error_updates_the_badges_and_the_dialog(self, host):
        host._index_errors_dialog = _Rec()
        host._on_face_index_error("/bad.jpg", "timeout")
        assert host._grid.last("set_index_error_paths") == (["/err.jpg"],)
        assert host._index_errors_dialog.called("refresh")

    def test_an_indexing_error_without_the_dialog_open(self, host):
        host._on_face_index_error("/bad.jpg", "timeout")
        assert host._grid.called("set_index_error_paths")


# ----------------------------------------------------------------- clustering

class TestClustering:
    def test_the_clustering_disables_its_menu_entry_while_running(self, host, threads):
        host._run_clustering()
        thread = threads["ClusterThread"][0]
        assert thread.started
        assert host._act_cluster_faces.last("setEnabled") == (False,)
        assert host._cluster_start_time is not None

    def test_a_second_clustering_is_refused_while_one_runs(self, host, threads):
        host._run_clustering()
        threads["ClusterThread"][0].running = True
        host._cluster_thread = threads["ClusterThread"][0]
        host._run_clustering()
        assert len(threads["ClusterThread"]) == 1

    def test_the_end_of_the_clustering_always_chains_the_similarity_search(self, host):
        """Even with 0 new groups: the existing groups still have to be compared
        with the people named in the meantime."""
        host._on_clustering_finished(0)
        assert "start_similarity_search" in host.log.names()
        assert host._act_cluster_faces.last("setEnabled") == (True,)
        assert host._cluster_start_time is None

    def test_the_face_panel_is_only_refreshed_when_visible(self, host):
        host._on_clustering_finished(3)
        assert host._face_panel_refresh_timer.called("start") == []
        host._face_panel = _Rec(isVisible=True)
        host._on_clustering_finished(3)
        assert host._face_panel_refresh_timer.called("start")


# ------------------------------------------------- groups named / assigned

class TestClusterIdentification:
    def test_naming_a_group_creates_the_person_and_removes_the_card(self, host, threads):
        host._on_cluster_named(42, "Alice")
        assert host._catalog.last("create_person") == ("Alice",)
        assert host._face_db.last("assign_person_to_cluster") == (42, 9)
        assert host._face_cluster_grid.last("remove_clusters") == ([42],)
        assert "schedule_similarity_search" in host.log.names()

    def test_naming_a_group_rebuilds_the_people_list(self, host, threads):
        """A NEW person appeared: the light refresh (counters only) is not enough."""
        host._on_cluster_named(42, "Alice")
        thread = threads["_PersonsRefreshThread"][0]
        assert thread.result_ready.slots == [host._on_persons_refreshed]

    def test_assigning_a_group_only_updates_the_counters(self, host, threads):
        host._on_cluster_assigned(42, 7)
        assert host._face_db.last("assign_person_to_cluster") == (42, 7)
        thread = threads["_PersonsRefreshThread"][0]
        assert thread.result_ready.slots == [host._on_persons_counts_updated]

    def test_naming_several_groups_assigns_them_all_to_one_person(self, host, threads):
        host._on_clusters_named([1, 2, 3], "Bob")
        assert len(host._face_db.called("assign_person_to_cluster")) == 3
        assert host._face_cluster_grid.last("remove_clusters") == ([1, 2, 3],)
        assert len(host._catalog.called("create_person")) == 1

    def test_assigning_several_groups(self, host, threads):
        host._on_clusters_assigned([4, 5], 7)
        assert [c[1] for c in host._face_db.called("assign_person_to_cluster")] == [
            (4, 7), (5, 7)]
        assert host._catalog.called("create_person") == []

    def test_ignoring_a_group_only_removes_its_card(self, host):
        host._on_cluster_ignored(11)
        assert host._face_cluster_grid.last("remove_clusters") == ([11],)
        assert host._face_db.calls == []

    def test_ignoring_several_groups(self, host):
        host._on_clusters_ignored([1, 2])
        assert host._face_cluster_grid.last("remove_clusters") == ([1, 2],)

    def test_a_merge_of_groups_rebuilds_the_whole_grid(self, host):
        host._on_cluster_merged(1, 2)
        assert host._face_cluster_grid.called("refresh")

    def test_the_face_panel_follows_only_when_visible(self, host, threads):
        host._on_cluster_assigned(1, 2)
        assert host._face_panel.called("refresh") == []
        host._face_panel = _Rec(isVisible=True)
        host._on_cluster_assigned(1, 2)
        assert host._face_panel.called("refresh")


class TestClusterPhotos:
    def test_a_group_shows_its_photos_in_the_grid(self, host):
        host._on_cluster_photos_requested(8, "Groupe 8")
        loader, context = host.log.last("start_photo_query")
        assert context == f"{_PERSON_CTX_PREFIX}cluster_8"
        assert host._lbl_grid_nav.last("setText") == ("Groupe 8",)
        assert host._grid_nav_bar.called("show")
        assert "show_grid" in host.log.names()

    def test_the_query_really_asks_for_the_photos_of_that_group(self, host):
        """The loader is a closure evaluated in a thread: calling it is the only
        way to check it does not query the wrong group."""
        host._on_cluster_photos_requested(8, "Groupe 8")
        loader, _ = host.log.last("start_photo_query")
        photos = loader()
        assert host._face_db.last("get_photos_for_cluster") == (8,)
        assert [p.path for p in photos] == [os.path.normpath("/p/8.jpg")]

    def test_the_ribbon_mode_is_left_behind(self, host):
        host._on_cluster_photos_requested(8, "x")
        assert host._grid.last("set_ribbon_mode") == (False,)
        assert host._grid.last("set_date_overlay_visible") == (False,)

    def test_coming_from_the_person_view_is_remembered(self, host):
        host._on_person_cluster_photos_requested(8, "x")
        assert host._from_person_cluster_view is True
        assert host.log.names().count("start_photo_query") == 1


# ------------------------------------------------------------------- people

class TestPersonMerge:
    @pytest.fixture
    def merge_dialog(self, monkeypatch):
        state = SimpleNamespace(accepted=True, target=7)

        class _Dlg:
            def __init__(self, source, persons, parent=None):
                self.source = source

            def exec(self):
                return mwf.QDialog.Accepted if state.accepted else mwf.QDialog.Rejected

            def target_person_id(self):
                return state.target

        monkeypatch.setattr(mwf, "MergePersonsDialog", _Dlg)
        return state

    def test_a_cancelled_merge_touches_nothing(self, host, merge_dialog):
        merge_dialog.accepted = False
        host._show_merge_dialog(PersonInfo(id=3, name="A"), [])
        assert host._face_db.calls == []
        assert host._catalog.calls == []

    def test_a_merge_without_a_target_touches_nothing(self, host, merge_dialog):
        merge_dialog.target = None
        host._show_merge_dialog(PersonInfo(id=3, name="A"), [])
        assert host._face_db.calls == []

    def test_the_merge_removes_the_source_and_updates_the_sidebar(self, host,
                                                                  merge_dialog):
        host._show_merge_dialog(PersonInfo(id=3, name="A"), [])
        assert host._face_db.last("merge_persons") == ()
        assert host._face_db.called("merge_persons")[0][2] == {
            "keep_id": 7, "remove_id": 3}
        assert host._catalog.last("delete_person") == (3,)
        assert host._sidebar.last("apply_person_merge") == (3, 7, 7)

    def test_the_displayed_grid_follows_the_merge(self, host, merge_dialog):
        """Merging the person being displayed must not leave the grid on an id
        that no longer exists."""
        host._current_context = f"{_PERSON_CTX_PREFIX}3"
        host._show_merge_dialog(PersonInfo(id=3, name="A"), [])
        assert host._current_context == f"{_PERSON_CTX_PREFIX}7"
        assert host._grid.called("set_photos")
        assert "update_status" in host.log.names()

    def test_another_context_is_left_alone(self, host, merge_dialog):
        host._show_merge_dialog(PersonInfo(id=3, name="A"), [])
        assert host._current_context == "Toutes les photos"
        assert host._grid.called("set_photos") == []


class TestPersonRenameAndClear:
    @pytest.fixture
    def ask(self, monkeypatch):
        state = SimpleNamespace(answer=("Bob", True))

        class _Input:
            @staticmethod
            def getText(*args, **kwargs):
                return state.answer

        monkeypatch.setattr(mwf, "QInputDialog", _Input)
        return state

    def test_a_rename_is_applied_and_rebuilds_the_list(self, host, ask, threads):
        host._on_person_rename_requested(PersonInfo(id=3, name="Alice"))
        assert host._catalog.last("rename_person") == (3, "Bob")
        assert threads["_PersonsRefreshThread"]

    def test_a_cancelled_rename_changes_nothing(self, host, ask, threads):
        ask.answer = ("Bob", False)
        host._on_person_rename_requested(PersonInfo(id=3, name="Alice"))
        assert host._catalog.calls == []

    @pytest.mark.parametrize("typed", ["", "   ", "Alice"])
    def test_an_empty_or_unchanged_name_changes_nothing(self, host, ask, typed):
        ask.answer = (typed, True)
        host._on_person_rename_requested(PersonInfo(id=3, name="Alice"))
        assert host._catalog.calls == []

    def test_clearing_a_name_requires_a_confirmation(self, host, box, threads):
        box.answer = box.Cancel
        host._on_person_clear_requested(PersonInfo(id=3, name="Alice"))
        assert host._face_db.calls == []
        assert len(box.infos) == 1

    def test_clearing_a_name_unlinks_the_faces_and_the_entry(self, host, box):
        host._on_person_clear_requested(PersonInfo(id=3, name="Alice"))
        assert host._face_db.last("unassign_person") == (3,)
        assert host._catalog.last("delete_person") == (3,)
        assert host._sidebar.last("remove_person") == (3,)

    def test_clearing_the_displayed_person_goes_back_to_the_grid(self, host, box):
        host._current_context = f"{_PERSON_CTX_PREFIX}3"
        host._on_person_clear_requested(PersonInfo(id=3, name="Alice"))
        assert "show_grid" in host.log.names()

    def test_clearing_another_person_keeps_the_view(self, host, box):
        host._on_person_clear_requested(PersonInfo(id=3, name="Alice"))
        assert "show_grid" not in host.log.names()


class TestPersonsRefresh:
    def test_a_refresh_is_never_stacked_on_a_running_one(self, host, threads):
        host._refresh_persons()
        threads["_PersonsRefreshThread"][0].running = True
        host._refresh_persons()
        host._update_persons_counts()
        assert len(threads["_PersonsRefreshThread"]) == 1

    def test_the_previous_thread_is_released(self, host, threads):
        host._refresh_persons()
        host._refresh_persons()          # the first is finished (running=False)
        assert len(threads["_PersonsRefreshThread"]) == 2

    def test_the_result_feeds_the_sidebar(self, host):
        persons = [PersonInfo(id=1, name="A")]
        host._on_persons_refreshed(persons, 12)
        assert host._sidebar.last("refresh_persons") == (persons,)
        assert host._sidebar.last("update_cluster_badge") == (12,)

    def test_a_light_update_never_rebuilds_the_list(self, host):
        persons = [PersonInfo(id=1, name="A")]
        host._on_persons_counts_updated(persons, 12)
        assert host._sidebar.last("update_persons_data") == (persons,)
        assert host._sidebar.called("refresh_persons") == []

    def test_a_pending_person_view_is_opened_once_the_list_is_known(self, host):
        """Opening the view of a person requested before the list was loaded."""
        host._pending_person_view_id = 1
        host._on_persons_refreshed([PersonInfo(id=1, name="A")], 0)
        assert host._pending_person_view_id is None
        assert host._person_cluster_view.called("set_person")

    def test_an_unknown_pending_person_opens_nothing(self, host):
        host._pending_person_view_id = 99
        host._on_persons_refreshed([PersonInfo(id=1, name="A")], 0)
        assert host._person_cluster_view.called("set_person") == []

    def test_a_pending_person_is_ignored_outside_the_main_context(self, host):
        """The user has navigated in the meantime: taking over the view would be
        a jump they did not ask for."""
        host._pending_person_view_id = 1
        host._current_context = "album_4"
        host._on_persons_refreshed([PersonInfo(id=1, name="A")], 0)
        assert host._pending_person_view_id == 1
        assert host._person_cluster_view.called("set_person") == []


# ----------------------------------------------------------------- the views

class TestClusterViews:
    def _hidden(self, host) -> list:
        return [w for w in (host._lbl_thumb_size, host._thumb_slider, host._lbl_zoom,
                            host._zoom_slider, host._zoom_pct_label,
                            host._btn_grid_status) if w.called("hide")]

    def test_the_group_view_hides_the_photo_controls(self, host):
        host.show_face_clusters()
        assert host._stack.last("setCurrentIndex") == (2,)
        assert host._left_stack.last("setCurrentIndex") == (0,)
        assert len(self._hidden(host)) == 6
        assert host._act_faces_toggle.last("setVisible") == (False,)
        assert host._face_cluster_grid.called("restore")

    def test_the_person_view_shows_that_person(self, host):
        person = PersonInfo(id=5, name="Alice")
        host.show_person_clusters(person)
        assert host._person_cluster_view.last("set_person") == (person,)
        assert host._stack.last("setCurrentIndex") == (3,)
        assert len(self._hidden(host)) == 6

    def test_selecting_a_person_hides_the_group_nav_bar(self, host):
        host._on_person_selected(PersonInfo(id=5, name="Alice"))
        assert host._grid_nav_bar.called("hide")
        assert host._stack.last("setCurrentIndex") == (3,)

    def test_a_named_face_of_the_panel_opens_the_person(self, host):
        host._on_face_panel_person_cluster_requested(5)
        assert host._face_db.called("enrich_persons")
        assert host._stack.last("setCurrentIndex") == (3,)

    def test_an_unknown_person_opens_nothing(self, host):
        host._catalog = _Rec(get_person=lambda pid: None)
        host._on_face_panel_person_cluster_requested(5)
        assert host._stack.calls == []

    def test_the_back_button_returns_to_the_grid(self, host):
        host._on_person_cluster_back()
        assert host._grid_nav_bar.called("hide")
        assert "show_grid" in host.log.names()

    def test_a_group_unlinked_from_the_person_view_refreshes_the_counters(
            self, host, threads):
        host._on_pcv_cluster_unassigned(3)
        assert threads["_PersonsRefreshThread"][0].result_ready.slots == [
            host._on_persons_counts_updated]


class TestPersonClusterPhoto:
    def test_an_unknown_photo_opens_nothing(self, host):
        host._catalog = _Rec(get_photo_by_path=lambda p: None)
        host._on_person_cluster_photo_requested("/gone.jpg")
        assert "show_viewer" not in host.log.names()

    def test_the_viewer_gets_every_photo_of_the_person_for_navigation(self, host):
        target = os.path.normpath("/lib/target.jpg")
        host._person_cluster_view.current_person = PersonInfo(id=4, name="A")
        host._catalog = _Rec(
            get_photo_by_path=lambda p: PhotoInfo(path=p),
            get_photos_by_paths=lambda paths: [PhotoInfo(path="/x.jpg"),
                                               PhotoInfo(path=target)],
        )
        host._on_person_cluster_photo_requested(target)
        assert len(host._current_photos) == 2
        assert host._current_photo_index == 1
        assert host._viewer_back_target == "person_cluster_view"
        assert host._current_album_id is None

    def test_without_a_person_the_photo_is_alone(self, host):
        host._on_person_cluster_photo_requested("/solo.jpg")
        assert len(host._current_photos) == 1
        assert host._current_photo_index == 0


# --------------------------------------------------------------- suggestions

class TestSuggestions:
    def test_accepting_moves_the_thumbnail_without_reloading(self, host, threads):
        host._on_suggestion_accepted(12)
        assert host._face_db.last("accept_cluster_suggestion") == (12,)
        assert host._person_cluster_view.last("accept_pending_cluster") == (12,)
        assert threads["_PersonsRefreshThread"][0].result_ready.slots == [
            host._on_persons_counts_updated]

    def test_rejecting_removes_it_at_once_and_recomputes_in_the_background(
            self, host, threads):
        host._person_cluster_view.current_person = PersonInfo(id=4, name="A")
        host._on_suggestion_rejected(12)
        assert host._person_cluster_view.last("remove_pending_cluster") == (12,)
        thread = threads["_ResuggestThread"][0]
        assert thread.started
        assert thread.args[1] == [12]
        assert thread.args[2] == 4          # the person refused is excluded

    def test_rejecting_outside_a_person_view_excludes_nobody(self, host, threads):
        host._on_suggestion_rejected(12)
        assert threads["_ResuggestThread"][0].args[2] is None

    def test_accepting_every_suggestion_at_once(self, host, threads):
        host._on_all_suggestions_accepted([1, 2, 3])
        assert len(host._face_db.called("accept_cluster_suggestion")) == 3
        assert len(host._person_cluster_view.called("accept_pending_cluster")) == 3

    def test_rejecting_every_suggestion_at_once(self, host, threads):
        host._person_cluster_view.current_person = PersonInfo(id=4, name="A")
        host._on_all_suggestions_rejected([1, 2])
        assert host._person_cluster_view.called("clear_all_pending")
        assert threads["_ResuggestThread"][0].args[1] == [1, 2]


# ------------------------------------------------------- retry / redetection

class TestRetryFaceIndex:
    def test_a_second_attempt_is_refused_while_one_runs(self, host, box, threads):
        host._retry_face_thread = _fake_thread({}, "x")()
        host._retry_face_thread.running = True
        host._on_retry_face_index_requested(PhotoInfo(path="/a.jpg"))
        assert threads.get("RetryFaceIndexThread") is None
        assert len(box.infos) == 1

    def test_the_attempt_starts_and_announces_the_file(self, host, box, threads):
        host._on_retry_face_index_requested(PhotoInfo(path="/lib/a.jpg"))
        thread = threads["RetryFaceIndexThread"][0]
        assert thread.started
        assert "a.jpg" in host._lbl_action.last("setText")[0]

    def test_a_success_reports_the_number_of_faces_and_clears_the_badge(
            self, host, box):
        host._index_errors_dialog = _Rec()
        host._on_retry_face_index_finished("/lib/a.jpg", True, 3)
        assert host._grid.last("set_index_error_paths") == (["/err.jpg"],)
        assert host._index_errors_dialog.called("refresh")
        assert "3" in box.infos[0][1] and "a.jpg" in box.infos[0][1]

    def test_a_failure_offers_deleting_the_file(self, host, box):
        box.clicked_role = box.DestructiveRole
        photo = PhotoInfo(path=os.path.normpath("/lib/a.jpg"))
        host._current_photos = [photo]
        host._on_retry_face_index_finished(photo.path, False, 0)
        assert host.log.last("delete_requested") == ([photo],)

    def test_a_failure_on_a_photo_outside_the_grid_still_deletes_it(self, host, box):
        box.clicked_role = box.DestructiveRole
        host._on_retry_face_index_finished("/lib/ghost.jpg", False, 0)
        deleted = host.log.last("delete_requested")[0]
        assert deleted[0].filename == "ghost.jpg"

    def test_a_failure_can_exclude_the_file_for_good(self, host, box):
        box.clicked_role = box.ActionRole
        host._on_retry_face_index_finished("/lib/a.jpg", False, 0)
        assert host._face_db.last("set_index_excluded") == ("/lib/a.jpg", True)
        assert host._grid.called("set_index_error_paths")

    def test_a_failure_left_alone_changes_nothing(self, host, box):
        box.clicked_role = box.RejectRole
        host._on_retry_face_index_finished("/lib/a.jpg", False, 0)
        assert host._face_db.called("set_index_excluded") == []
        assert "delete_requested" not in host.log.names()


class TestForceRedetect:
    @pytest.fixture
    def available(self, monkeypatch):
        import src.faces.detector as detector
        state = SimpleNamespace(value=True)
        monkeypatch.setattr(detector, "is_available", lambda: state.value)
        return state

    def test_nothing_happens_without_the_recognition_module(self, host, available,
                                                            threads):
        available.value = False
        host._on_force_redetect_requested(PhotoInfo(path="/a.jpg"))
        assert threads.get("ForceRedetectThread") is None

    def test_a_second_detection_is_refused_while_one_runs(self, host, available,
                                                          box, threads):
        host._force_redetect_thread = _fake_thread({}, "x")()
        host._force_redetect_thread.running = True
        host._on_force_redetect_requested(PhotoInfo(path="/a.jpg"))
        assert threads.get("ForceRedetectThread") is None
        assert len(box.infos) == 1

    def test_the_detection_starts_on_the_displayed_photo(self, host, available,
                                                         threads):
        host._on_force_redetect_requested(PhotoInfo(path="/lib/a.jpg"))
        thread = threads["ForceRedetectThread"][0]
        assert thread.started
        assert thread.args[1] == os.path.normpath("/lib/a.jpg")
        assert thread.kwargs["edit_db"] is host._edit_db

    def test_the_end_reports_that_nothing_was_ignored(self, host, box):
        host._on_force_redetect_finished("/lib/a.jpg", 5)
        assert "5" in box.infos[0][1]
        assert host._lbl_action.last("setText") == ("",)

    def test_the_face_panel_is_updated_only_when_visible(self, host, box):
        host._on_force_redetect_finished("/lib/a.jpg", 5)
        assert host._face_panel.called("set_photo") == []
        host._face_panel = _Rec(isVisible=True)
        host._on_force_redetect_finished("/lib/a.jpg", 5)
        assert host._face_panel.last("set_photo") == ("/lib/a.jpg",)


# ------------------------------------------------------------ small wirings

class TestDelegations:
    def test_a_highlighted_face_goes_to_the_viewer(self, host):
        face = object()
        host._on_face_highlighted(face)
        assert host._viewer.last("highlight_face") == (face,)

    def test_the_all_faces_toggle_goes_to_the_viewer(self, host):
        faces = [object()]
        host._on_all_faces_toggled(faces)
        assert host._viewer.last("set_all_highlighted_faces") == (faces,)

    def test_the_face_context_menu_is_the_panel_s(self, host):
        face, pos = object(), object()
        host._on_face_context_menu(face, pos)
        assert host._face_panel.last("show_face_context_menu") == (face, pos)

    @pytest.mark.parametrize("enter, expected", [
        (True, "enter_face_add_mode"), (False, "cancel_face_add_mode")])
    def test_the_add_face_mode_toggles_the_viewer(self, host, enter, expected):
        host._on_add_face_mode_requested(enter)
        assert host._viewer.names() == [expected]

    def test_a_new_cover_updates_the_sidebar_icon(self, host):
        face = object()
        host._on_cover_face_set(4, face)
        assert host._sidebar.last("update_person_icon") == (4, face)

    def test_the_imported_picasa_edits_refresh_the_thumbnails(self, host):
        edit = object()
        host._on_picasa_edits_imported({"/a.jpg": edit})
        assert host._grid.last("refresh_photo") == ("/a.jpg", edit)

    def test_a_restore_rebuilds_the_people_and_the_panel(self, host, threads):
        host._face_panel = _Rec(isVisible=True)
        host._on_face_restore_completed()
        assert threads["_PersonsRefreshThread"]
        assert host._face_panel.called("refresh")

    def test_a_single_reindex_updates_the_panel_then_drains_the_queue(self, host):
        host._face_panel = _Rec(isVisible=True)
        host._on_single_reindex_finished("/a.jpg", 2)
        assert host._face_panel.last("set_photo") == ("/a.jpg",)
        assert "drain_pending_reindex" in host.log.names()

    def test_an_invisible_face_panel_is_never_refreshed(self, host):
        host._refresh_face_panel_if_visible()
        assert host._face_panel.called("refresh") == []


# ----------------------------------------------- entry points of the menus

class TestSimilaritySearchStart:
    """`_start_similarity_search` is called unbound: the host keeps its recording
    stub, on which the tests of the callers rely."""

    def test_the_search_shows_an_indeterminate_bar_then_starts(self, host, threads):
        FacesController._start_similarity_search(host)
        thread = threads["SimilaritySearchThread"][0]
        assert thread.started
        assert host._sb_progress_bar.last("setRange") == (0, 0)   # indeterminate
        assert host._sb_progress_bar.called("show")

    def test_a_running_search_is_never_doubled(self, host, threads):
        FacesController._start_similarity_search(host)
        threads["SimilaritySearchThread"][0].running = True
        FacesController._start_similarity_search(host)
        assert len(threads["SimilaritySearchThread"]) == 1


class TestClusteringConfirmation:
    def test_a_running_clustering_only_says_so(self, host, box, threads):
        host._cluster_thread = _fake_thread({}, "x")()
        host._cluster_thread.running = True
        host._start_clustering_with_confirm()
        assert len(box.infos) == 1
        assert threads.get("ClusterThread") is None

    def test_the_explanation_states_the_counts_at_stake(self, host, box, threads):
        host._start_clustering_with_confirm()
        text = " ".join(box.instances[0].texts)
        assert "60" in text and "40" in text     # 100 embeddings - 40 identified

    def test_a_cancelled_confirmation_starts_nothing(self, host, box, threads):
        box.answer = box.Cancel
        host._start_clustering_with_confirm()
        assert threads.get("ClusterThread") is None

    def test_a_confirmed_one_starts_the_clustering(self, host, box, threads):
        box.answer = box.Ok
        host._start_clustering_with_confirm()
        assert threads["ClusterThread"][0].started


class TestPicasaMenu:
    @pytest.fixture
    def import_dialog(self, monkeypatch):
        import src.ui.picasa_import_dialog as pid
        built = []

        class _Dlg(_Rec):
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.args = args
                self.kwargs = kwargs
                built.append(self)

        monkeypatch.setattr(pid, "PicasaImportDialog", _Dlg)
        return built

    def test_a_refused_warning_opens_no_import(self, host, box, import_dialog):
        box.answer = box.Cancel
        host._import_from_picasa()
        assert import_dialog == []

    def test_an_accepted_warning_opens_the_import_dialog(self, host, box,
                                                         import_dialog):
        box.answer = box.Ok
        host._import_from_picasa()
        assert len(import_dialog) == 1
        assert import_dialog[0].kwargs["on_edits_imported"] == host._on_picasa_edits_imported

    def test_the_menu_entry_is_re_evaluated_afterwards(self, host, box,
                                                       import_dialog):
        """The import is meant to happen once: the entry is greyed out as soon as
        the configuration says it has been done."""
        box.answer = box.Ok
        host._config = _Rec(get=lambda key, default=None: True)
        host._import_from_picasa()
        assert host._act_picasa.last("setEnabled") == (False,)


class TestBackupsAndCounters:
    @pytest.fixture
    def backup(self, monkeypatch):
        import src.ui.face_backup_dialog as fbd

        store: dict = {}
        monkeypatch.setattr(fbd, "_BackupThread", _fake_thread(store, "backup"))
        monkeypatch.setattr(fbd, "_parse_ts", lambda path: "hier")
        return store

    def test_the_backup_starts_and_announces_itself(self, host, backup):
        host._backup_faces()
        assert backup["backup"][0].started
        assert host._lbl_action.last("setText")[0]

    def test_a_second_backup_is_ignored_while_one_runs(self, host, backup):
        host._backup_faces()
        backup["backup"][0].running = True
        host._backup_faces()
        assert len(backup["backup"]) == 1

    def test_the_success_names_the_archive(self, host, backup, box, monkeypatch,
                                           tmp_path):
        """QMessageBox is imported inside the method: it is the one of the
        QtWidgets module that has to be replaced, not the one of the controller."""
        from PySide6 import QtWidgets
        monkeypatch.setattr(QtWidgets, "QMessageBox", box)
        host._backup_faces()
        backup["backup"][0].succeeded.emit(tmp_path / "faces_20260101.zip")
        assert "faces_20260101.zip" in box.infos[0][1]
        assert host._lbl_action.last("setText") == ("",)

    def test_a_failure_is_reported_as_an_error(self, host, backup, box, monkeypatch):
        from PySide6 import QtWidgets
        monkeypatch.setattr(QtWidgets, "QMessageBox", box)
        host._backup_faces()
        backup["backup"][0].failed.emit("disque plein")
        assert box.criticals[0][1] == "disque plein"

    def test_the_backup_manager_wires_the_restore(self, host, monkeypatch):
        built = []

        class _Dlg(_Rec):
            def __init__(self, *args):
                super().__init__()
                self.restore_completed = _Signal()
                built.append(self)

        monkeypatch.setattr(mwf, "FaceBackupDialog", _Dlg)
        host._manage_face_backups()
        assert built[0].restore_completed.slots == [host._on_face_restore_completed]
        assert built[0].called("exec")

    def test_the_counters_dialog_is_opened(self, host, monkeypatch):
        import src.ui.face_counters_dialog as fcd
        built = []
        monkeypatch.setattr(fcd, "FaceCountersDialog",
                            lambda *a: built.append(a) or _Rec())
        host._show_face_counters()
        assert len(built) == 1


class TestSmallWirings:
    def test_an_unavailable_module_is_reported_once(self, host, box):
        host._on_face_unavailable()
        assert len(box.infos) == 1
        assert host._lbl_action.last("setText") == ("",)

    def test_the_people_dialog_wires_both_identification_signals(self, host,
                                                                 monkeypatch):
        built = []

        class _Dlg(_Rec):
            def __init__(self, *args):
                super().__init__()
                self.cluster_named = _Signal()
                self.cluster_assigned = _Signal()
                built.append(self)

        monkeypatch.setattr(mwf, "PeopleDialog", _Dlg)
        host._open_people_dialog()
        assert built[0].cluster_named.slots == [host._on_cluster_named]
        assert built[0].cluster_assigned.slots == [host._on_cluster_assigned]

    def test_a_merge_request_loads_the_people_in_a_thread_first(self, host, threads,
                                                                monkeypatch):
        """enrich_persons takes seconds on a large database: the dialog is only
        opened once the thread has answered."""
        opened = []
        monkeypatch.setattr(mwf, "MergePersonsDialog",
                            lambda source, persons, parent: opened.append(source)
                            or _Rec(exec=mwf.QDialog.Rejected))
        source = PersonInfo(id=3, name="A")
        host._on_person_merge_requested(source)
        thread = threads["_PersonsRefreshThread"][0]
        assert thread.started and opened == []
        thread.result_ready.emit([source], 0)
        assert opened == [source]


class TestThreadRecycling:
    """The controller keeps a reference to its one-shot threads and releases the
    previous one before creating the next: a branch only taken from the second
    call onwards."""

    def test_a_second_clustering_recycles_the_previous_thread(self, host, threads):
        host._run_clustering()
        host._cluster_thread = threads["ClusterThread"][0]   # finished
        host._run_clustering()
        assert len(threads["ClusterThread"]) == 2

    def test_a_second_retry_recycles_the_previous_thread(self, host, box, threads):
        host._on_retry_face_index_requested(PhotoInfo(path="/a.jpg"))
        host._on_retry_face_index_requested(PhotoInfo(path="/b.jpg"))
        assert len(threads["RetryFaceIndexThread"]) == 2

    def test_a_second_forced_detection_recycles_the_previous_thread(
            self, host, threads, monkeypatch):
        import src.faces.detector as detector
        monkeypatch.setattr(detector, "is_available", lambda: True)
        host._on_force_redetect_requested(PhotoInfo(path="/a.jpg"))
        host._on_force_redetect_requested(PhotoInfo(path="/b.jpg"))
        assert len(threads["ForceRedetectThread"]) == 2

    def test_a_successful_retry_refreshes_the_visible_face_panel(self, host, box):
        host._face_panel = _Rec(isVisible=True)
        host._on_retry_face_index_finished("/lib/a.jpg", True, 3)
        assert host._face_panel.last("set_photo") == ("/lib/a.jpg",)

    def test_a_failed_retry_refreshes_the_open_errors_dialog(self, host, box):
        host._index_errors_dialog = _Rec()
        box.clicked_role = box.RejectRole
        host._on_retry_face_index_finished("/lib/a.jpg", False, 0)
        assert host._index_errors_dialog.called("refresh")

    def test_an_already_dead_indexer_never_breaks_the_reset(self, host, threads,
                                                            monkeypatch):
        """The C++ object of the thread may already be gone: disconnecting it
        raises, and that must not abort the reset."""
        monkeypatch.setattr(mwf, "_ResetFacesDialog", type(
            "_Dlg", (), {"choice": _ResetFacesDialog.RESET_FULL,
                         "__init__": lambda self, parent=None: None,
                         "exec": lambda self: mwf.QDialog.Accepted}))
        indexer = _fake_thread({}, "x")()
        indexer.running = True

        class _Dead:
            def disconnect(self, slot=None):
                raise RuntimeError("wrapped C/C++ object already deleted")

        indexer.cluster_requested = _Dead()
        host._face_indexer = indexer
        host._reset_and_reindex_faces()
        assert threads["_ResetWorkerThread"][0].started
        assert indexer.stopped
