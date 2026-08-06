# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/app_version.py : calcul de version (mode figé vs. dev),
en pur Python (subprocess et sys.frozen monkeypatchés, pas de PyInstaller réel).

`get_app_version()` mémoïse son résultat dans le global `_cached_version` :
chaque test le réinitialise à None pour ne pas dépendre de l'ordre d'exécution."""
import subprocess

import src.core.app_version as app_version_module
from src.core.app_version import _compute_app_version, get_app_version


class BaseAppVersionTest:
    def setup_method(self):
        app_version_module._cached_version = None

    def teardown_method(self):
        app_version_module._cached_version = None


class TestComputeAppVersionFrozen(BaseAppVersionTest):
    def test_reads_version_file_when_frozen(self, tmp_path, monkeypatch):
        (tmp_path / "VERSION").write_text("2.3.1\n", encoding="utf-8")
        monkeypatch.setattr(app_version_module.sys, "frozen", True, raising=False)
        monkeypatch.setattr(app_version_module.sys, "_MEIPASS", str(tmp_path), raising=False)

        assert _compute_app_version() == "2.3.1"

    def test_falls_back_when_version_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_version_module.sys, "frozen", True, raising=False)
        monkeypatch.setattr(app_version_module.sys, "_MEIPASS", str(tmp_path), raising=False)

        assert _compute_app_version() == app_version_module._FALLBACK_VERSION

    def test_falls_back_when_version_file_empty(self, tmp_path, monkeypatch):
        (tmp_path / "VERSION").write_text("", encoding="utf-8")
        monkeypatch.setattr(app_version_module.sys, "frozen", True, raising=False)
        monkeypatch.setattr(app_version_module.sys, "_MEIPASS", str(tmp_path), raising=False)

        assert _compute_app_version() == app_version_module._FALLBACK_VERSION


class TestComputeAppVersionDev(BaseAppVersionTest):
    def test_returns_git_describe_output_on_success(self, monkeypatch):
        monkeypatch.setattr(app_version_module.sys, "frozen", False, raising=False)

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=0, stdout="v1.4.0-2-gabcdef\n", stderr="")

        monkeypatch.setattr(app_version_module.subprocess, "run", fake_run)

        assert _compute_app_version() == "v1.4.0-2-gabcdef"

    def test_falls_back_when_git_returns_nonzero(self, monkeypatch):
        monkeypatch.setattr(app_version_module.sys, "frozen", False, raising=False)

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="not a git repo")

        monkeypatch.setattr(app_version_module.subprocess, "run", fake_run)

        assert _compute_app_version() == app_version_module._FALLBACK_VERSION

    def test_falls_back_when_git_raises(self, monkeypatch):
        monkeypatch.setattr(app_version_module.sys, "frozen", False, raising=False)

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("git introuvable")

        monkeypatch.setattr(app_version_module.subprocess, "run", fake_run)

        assert _compute_app_version() == app_version_module._FALLBACK_VERSION

    def test_falls_back_on_timeout(self, monkeypatch):
        monkeypatch.setattr(app_version_module.sys, "frozen", False, raising=False)

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=2)

        monkeypatch.setattr(app_version_module.subprocess, "run", fake_run)

        assert _compute_app_version() == app_version_module._FALLBACK_VERSION


class TestGetAppVersionMemoization(BaseAppVersionTest):
    def test_caches_result_across_calls(self, monkeypatch):
        calls = []

        def fake_compute():
            calls.append(1)
            return "9.9.9"

        monkeypatch.setattr(app_version_module, "_compute_app_version", fake_compute)

        assert get_app_version() == "9.9.9"
        assert get_app_version() == "9.9.9"
        assert len(calls) == 1
