# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/library/fs_utils.py`: is_hidden_path (already covered indirectly
through test_scanner.py::TestIsHidden, aliased _is_hidden) and find_dvd_video_ts,
used to detect the DVD copies (VIDEO_TS/AUDIO_TS) that would otherwise appear
as empty folders (no .vob/.ifo/.bup extension catalogued)."""
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
        """VIDEO_TS must be a direct child -- one level deeper does not
        qualify folder itself as a DVD copy."""
        (tmp_path / "sub" / "VIDEO_TS").mkdir(parents=True)
        assert find_dvd_video_ts(str(tmp_path)) is None
