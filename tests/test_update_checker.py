# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/update_checker.py : _parse_version (pur) et
UpdateCheckThread.run() (urllib.request.urlopen et get_app_version mockés,
run() appelé directement sans .start() ni QApplication, comme
tests/test_duplicate_detector.py pour d'autres QThread)."""
import json
from urllib.error import URLError

import src.core.update_checker as update_checker_module
from src.core.update_checker import (
    STATUS_ERROR,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    STATUS_VERSION_UNKNOWN,
    UpdateCheckThread,
    _parse_version,
)


class TestParseVersion:
    def test_parses_plain_dotted_version(self):
        assert _parse_version("1.2.0") == (1, 2, 0)

    def test_strips_leading_v(self):
        assert _parse_version("v1.2.0") == (1, 2, 0)

    def test_empty_string_returns_none(self):
        assert _parse_version("") is None

    def test_non_numeric_returns_none(self):
        assert _parse_version("17ab7a3-dirty") is None


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run_thread(monkeypatch, current_version, urlopen_fn):
    monkeypatch.setattr(update_checker_module, "get_app_version", lambda: current_version)
    monkeypatch.setattr(update_checker_module.urllib.request, "urlopen", urlopen_fn)

    thread = UpdateCheckThread()
    results = []
    thread.checked.connect(lambda status, version, url: results.append((status, version, url)))
    thread.run()
    assert len(results) == 1
    return results[0]


class TestUpdateCheckThreadRun:
    def test_update_available_when_latest_is_newer(self, monkeypatch):
        response = _FakeResponse({"tag_name": "v2.0.0", "html_url": "https://example.com/2.0.0"})
        status, version, url = _run_thread(monkeypatch, "1.0.0", lambda *a, **k: response)

        assert status == STATUS_UPDATE_AVAILABLE
        assert version == "2.0.0"
        assert url == "https://example.com/2.0.0"

    def test_up_to_date_when_versions_match(self, monkeypatch):
        response = _FakeResponse({"tag_name": "v1.0.0", "html_url": "https://example.com/1.0.0"})
        status, version, url = _run_thread(monkeypatch, "1.0.0", lambda *a, **k: response)

        assert status == STATUS_UP_TO_DATE

    def test_up_to_date_when_local_is_newer(self, monkeypatch):
        response = _FakeResponse({"tag_name": "v1.0.0", "html_url": "https://example.com/1.0.0"})
        status, _, _ = _run_thread(monkeypatch, "1.5.0", lambda *a, **k: response)

        assert status == STATUS_UP_TO_DATE

    def test_version_unknown_when_current_version_is_a_git_hash(self, monkeypatch):
        response = _FakeResponse({"tag_name": "v1.0.0", "html_url": "https://example.com/1.0.0"})
        status, version, url = _run_thread(monkeypatch, "17ab7a3-dirty", lambda *a, **k: response)

        assert status == STATUS_VERSION_UNKNOWN
        assert version == "1.0.0"
        assert url == "https://example.com/1.0.0"

    def test_error_on_url_error(self, monkeypatch):
        def raise_url_error(*args, **kwargs):
            raise URLError("pas de réseau")

        status, version, url = _run_thread(monkeypatch, "1.0.0", raise_url_error)

        assert status == STATUS_ERROR
        assert version == ""
        assert url == ""

    def test_error_on_unexpected_exception(self, monkeypatch):
        def raise_boom(*args, **kwargs):
            raise RuntimeError("boom")

        status, _, _ = _run_thread(monkeypatch, "1.0.0", raise_boom)

        assert status == STATUS_ERROR

    def test_error_when_response_missing_tag_name(self, monkeypatch):
        response = _FakeResponse({"html_url": "https://example.com/x"})
        status, _, _ = _run_thread(monkeypatch, "1.0.0", lambda *a, **k: response)

        assert status == STATUS_ERROR

    def test_error_when_response_missing_html_url(self, monkeypatch):
        response = _FakeResponse({"tag_name": "v1.0.0"})
        status, _, _ = _run_thread(monkeypatch, "1.0.0", lambda *a, **k: response)

        assert status == STATUS_ERROR
