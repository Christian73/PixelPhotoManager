# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/config.py.

Attention : Config est un singleton de classe (`Config._instance`). Sans
reset explicite, le premier test qui instancie Config() "gagne" pour toute
la session pytest et pollue silencieusement les tests suivants. Chaque test
ici réinitialise `Config._instance = None` ET redirige `_CONFIG_FILE` vers un
fichier isolé dans tmp_path, pour ne dépendre ni d'un état de classe partagé
ni d'un fichier config.json partagé entre tests.
"""
import json

import src.core.config as config_module
from src.core.config import Config


class BaseConfigTest:
    def setup_method(self):
        Config._instance = None

    def teardown_method(self):
        Config._instance = None

    def _make_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        monkeypatch.setattr(config_module, "_CONFIG_FILE", config_file)
        monkeypatch.setattr(config_module, "APP_DATA_DIR", tmp_path)
        return Config()


class TestSingleton(BaseConfigTest):
    def test_new_returns_same_instance(self, tmp_path, monkeypatch):
        cfg1 = self._make_config(tmp_path, monkeypatch)
        cfg2 = Config()
        assert cfg1 is cfg2

    def test_reset_instance_creates_fresh_object(self, tmp_path, monkeypatch):
        cfg1 = self._make_config(tmp_path, monkeypatch)
        Config._instance = None
        cfg2 = self._make_config(tmp_path, monkeypatch)
        assert cfg1 is not cfg2


class TestGetSet(BaseConfigTest):
    def test_get_dotted_key_from_defaults(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        assert cfg.get("ui.theme") == "dark"

    def test_get_missing_key_returns_default(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        assert cfg.get("ui.nonexistent", "fallback") == "fallback"

    def test_get_missing_top_level_key_returns_default(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        assert cfg.get("nope.nope", None) is None

    def test_set_dotted_key_creates_nested_dicts(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.set("ui.new_setting", 42)
        assert cfg.get("ui.new_setting") == 42

    def test_set_overwrites_existing_value(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.set("ui.theme", "light")
        assert cfg.get("ui.theme") == "light"

    def test_set_persists_to_disk(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.set("thumbnail_size", 240)
        with open(config_module._CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["thumbnail_size"] == 240


class TestMerge(BaseConfigTest):
    def test_load_merges_saved_values_over_defaults(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"thumbnail_size": 999}), encoding="utf-8")
        monkeypatch.setattr(config_module, "_CONFIG_FILE", config_file)
        monkeypatch.setattr(config_module, "APP_DATA_DIR", tmp_path)
        cfg = Config()
        assert cfg.get("thumbnail_size") == 999
        # les autres défauts restent présents
        assert cfg.get("sort_by") == "date_taken"

    def test_load_merges_nested_dict_recursively(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"ui": {"theme": "light"}}), encoding="utf-8"
        )
        monkeypatch.setattr(config_module, "_CONFIG_FILE", config_file)
        monkeypatch.setattr(config_module, "APP_DATA_DIR", tmp_path)
        cfg = Config()
        # valeur sauvegardée gagne...
        assert cfg.get("ui.theme") == "light"
        # ...mais les autres clés par défaut du même sous-dict survivent
        assert cfg.get("ui.sidebar_width") == 240

    def test_load_with_corrupt_json_falls_back_to_defaults(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(config_module, "_CONFIG_FILE", config_file)
        monkeypatch.setattr(config_module, "APP_DATA_DIR", tmp_path)
        cfg = Config()
        assert cfg.get("thumbnail_size") == 180

    def test_load_without_existing_file_uses_defaults(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        assert cfg.get("scan_folders") == []


class TestScanFolders(BaseConfigTest):
    def test_add_scan_folder_normalizes_path(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.add_scan_folder("C:/photos/2026")
        assert cfg.get_scan_folders() == ["C:\\photos\\2026"]

    def test_add_scan_folder_no_duplicate(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.add_scan_folder("C:/photos")
        cfg.add_scan_folder("C:\\photos")
        assert cfg.get_scan_folders() == ["C:\\photos"]

    def test_remove_scan_folder(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.add_scan_folder("C:/photos")
        cfg.remove_scan_folder("C:/photos")
        assert cfg.get_scan_folders() == []

    def test_remove_scan_folder_not_present_is_noop(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.remove_scan_folder("C:/nowhere")
        assert cfg.get_scan_folders() == []


class TestPluginSettings(BaseConfigTest):
    def test_get_plugin_settings_missing_plugin_returns_empty_dict(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        assert cfg.get_plugin_settings("unknown_plugin") == {}

    def test_set_then_get_plugin_settings(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.set_plugin_settings("map_plugin", {"zoom": 5})
        assert cfg.get_plugin_settings("map_plugin") == {"zoom": 5}

    def test_get_plugin_settings_returns_copy_not_live_reference(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.set_plugin_settings("map_plugin", {"zoom": 5})
        settings = cfg.get_plugin_settings("map_plugin")
        settings["zoom"] = 999
        assert cfg.get_plugin_settings("map_plugin") == {"zoom": 5}


class TestSaveLoadRoundTrip(BaseConfigTest):
    def test_save_then_reload_from_disk(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path, monkeypatch)
        cfg.set("ui.theme", "light")
        cfg.add_scan_folder("C:/photos")

        Config._instance = None
        cfg2 = Config()
        assert cfg2.get("ui.theme") == "light"
        assert cfg2.get_scan_folders() == ["C:\\photos"]
