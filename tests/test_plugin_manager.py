# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/plugin_manager.py (PluginManager) : découverte/activation
de plugins via importlib, sans dépendance Qt. `PLUGIN_DIRS` est monkeypatché
vers un dossier temporaire contenant un faux plugin (plugin.json + plugin.py)."""
import json

import src.core.plugin_manager as plugin_manager_module
from src.core.plugin_manager import PluginManager


class _StubConfig:
    def get_plugin_settings(self, plugin_id):
        return {}


def _write_fake_plugin(plugins_root, plugin_id="demo", activate_ok=True):
    plugin_dir = plugins_root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"id": plugin_id, "name": f"Demo {plugin_id}"}),
        encoding="utf-8",
    )
    activate_body = "pass" if activate_ok else "raise RuntimeError('boom')"
    (plugin_dir / "plugin.py").write_text(
        "from src.core.base_plugin import BasePlugin\n"
        "class DemoPlugin(BasePlugin):\n"
        "    def activate(self):\n"
        f"        {activate_body}\n"
        "    def deactivate(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    return plugin_dir


def _make_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_manager_module.PluginManager, "PLUGIN_DIRS", [tmp_path])
    return PluginManager(config=_StubConfig())


class TestDiscover:
    def test_discover_finds_manifest_and_sets_path(self, tmp_path, monkeypatch):
        _write_fake_plugin(tmp_path)
        manager = _make_manager(tmp_path, monkeypatch)

        manifests = manager.discover()

        assert len(manifests) == 1
        assert manifests[0]["id"] == "demo"
        assert manifests[0]["_path"] == str(tmp_path / "demo")

    def test_discover_ignores_dir_without_manifest(self, tmp_path, monkeypatch):
        (tmp_path / "not_a_plugin").mkdir()
        manager = _make_manager(tmp_path, monkeypatch)

        assert manager.discover() == []

    def test_discover_ignores_dir_with_corrupt_manifest(self, tmp_path, monkeypatch):
        bad = tmp_path / "broken"
        bad.mkdir()
        (bad / "plugin.json").write_text("{not json", encoding="utf-8")
        manager = _make_manager(tmp_path, monkeypatch)

        assert manager.discover() == []

    def test_discover_skips_missing_plugin_dir(self, tmp_path, monkeypatch):
        missing = tmp_path / "does_not_exist"
        monkeypatch.setattr(plugin_manager_module.PluginManager, "PLUGIN_DIRS", [missing])
        manager = PluginManager(config=_StubConfig())

        assert manager.discover() == []


class TestActivateDeactivate:
    def test_activate_loads_and_calls_activate(self, tmp_path, monkeypatch):
        _write_fake_plugin(tmp_path)
        manager = _make_manager(tmp_path, monkeypatch)
        manager.discover()

        result = manager.activate("demo")

        assert result is True
        loaded = manager.get_active_plugins()
        assert len(loaded) == 1
        assert loaded[0].enabled is True

    def test_activate_unknown_plugin_returns_false(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        manager.discover()

        assert manager.activate("nope") is False

    def test_activate_twice_is_idempotent(self, tmp_path, monkeypatch):
        _write_fake_plugin(tmp_path)
        manager = _make_manager(tmp_path, monkeypatch)
        manager.discover()

        assert manager.activate("demo") is True
        assert manager.activate("demo") is True
        assert len(manager.get_active_plugins()) == 1

    def test_activate_plugin_whose_activate_raises_returns_false(self, tmp_path, monkeypatch):
        _write_fake_plugin(tmp_path, activate_ok=False)
        manager = _make_manager(tmp_path, monkeypatch)
        manager.discover()

        assert manager.activate("demo") is False
        assert manager.get_active_plugins() == []

    def test_deactivate_unloaded_plugin_returns_true(self, tmp_path, monkeypatch):
        manager = _make_manager(tmp_path, monkeypatch)
        assert manager.deactivate("demo") is True

    def test_deactivate_loaded_plugin_removes_it(self, tmp_path, monkeypatch):
        _write_fake_plugin(tmp_path)
        manager = _make_manager(tmp_path, monkeypatch)
        manager.discover()
        manager.activate("demo")

        result = manager.deactivate("demo")

        assert result is True
        assert manager.get_active_plugins() == []

    def test_list_available_marks_active_flag(self, tmp_path, monkeypatch):
        _write_fake_plugin(tmp_path)
        manager = _make_manager(tmp_path, monkeypatch)
        manager.discover()
        manager.activate("demo")

        listed = manager.list_available()

        assert listed[0]["active"] is True
