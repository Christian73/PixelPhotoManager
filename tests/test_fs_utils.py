# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Teste `src/library/fs_utils.py` : is_hidden_path (déjà couvert indirectement
via test_scanner.py::TestIsHidden, alias _is_hidden) et find_dvd_video_ts,
utilisé pour détecter les copies de DVD (VIDEO_TS/AUDIO_TS) qui apparaissent
sinon comme des dossiers vides (aucune extension .vob/.ifo/.bup cataloguée)."""
import os

from src.library.fs_utils import find_dvd_video_ts


class TestFindDvdVideoTs:
    def test_finds_video_ts_subfolder(self, tmp_path):
        (tmp_path / "VIDEO_TS").mkdir()
        result = find_dvd_video_ts(str(tmp_path))
        assert result == str(tmp_path / "VIDEO_TS")

    def test_case_insensitive(self, tmp_path):
        (tmp_path / "video_ts").mkdir()
        assert find_dvd_video_ts(str(tmp_path)) is not None

    def test_no_video_ts_returns_none(self, tmp_path):
        (tmp_path / "regular_subfolder").mkdir()
        assert find_dvd_video_ts(str(tmp_path)) is None

    def test_nonexistent_folder_returns_none(self, tmp_path):
        assert find_dvd_video_ts(str(tmp_path / "absent")) is None

    def test_video_ts_as_file_not_dir_ignored(self, tmp_path):
        (tmp_path / "VIDEO_TS").write_bytes(b"x")
        assert find_dvd_video_ts(str(tmp_path)) is None

    def test_nested_video_ts_not_detected(self, tmp_path):
        """VIDEO_TS doit être un enfant direct — un niveau plus profond ne
        qualifie pas folder lui-même comme copie de DVD."""
        (tmp_path / "sub" / "VIDEO_TS").mkdir(parents=True)
        assert find_dvd_video_ts(str(tmp_path)) is None
