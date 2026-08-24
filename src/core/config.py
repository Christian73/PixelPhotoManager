# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import json
import logging
import os
from pathlib import Path

from .app_dirs import APP_DATA_DIR

logger = logging.getLogger(__name__)

_CONFIG_FILE = APP_DATA_DIR / "config.json"

_DEFAULTS = {
    "scan_folders": [],
    "thumbnail_size": 180,
    "sort_by": "date_taken",
    "sort_desc": True,
    "plugins": {},
    "ui": {
        "sidebar_width": 240,
        "theme": "dark",
        # Interface language — "en" | "fr" | "de" (cf. src/core/i18n.py).
        # Read at startup only: a change takes effect on restart.
        # English by default, like `i18n.DEFAULT_LANGUAGE`: a fresh install therefore
        # starts in the language of the source strings, the only one where no message
        # can be missing. This only concerns configs without the key — `save()` writes
        # the complete merged dictionary, so an already installed machine carries its
        # explicit choice and is not switched over. On a machine installed through
        # the MSI, `_adopt_installer_language()` replaces this default at the very
        # first start with the language the installer itself displayed.
        "language": "en",
        "splitters": {
            "viewer": "",
            "sidebar_panels": "",
        },
    },
    "faces": {
        "similarity_eps": 0.5,
    },
    "picasa": {
        "import_done": False,
    },
}


class Config:
    _instance: "Config | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data = {}
            cls._instance.load()
        return cls._instance

    def load(self) -> None:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if _CONFIG_FILE.exists():
            try:
                with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._data = self._merge(json.loads(json.dumps(_DEFAULTS)), saved)
                return
            except Exception as e:
                logger.error(f"Erreur lecture config: {e}")
        self._data = json.loads(json.dumps(_DEFAULTS))
        self._adopt_installer_language()

    def _adopt_installer_language(self) -> None:
        """First start: adopt the language the installer displayed.

        Only reached when there is no readable `config.json` — an installation
        that already has one keeps the language its user chose, which is the
        whole point of not letting the installer write into `%LOCALAPPDATA%`
        directly (cf. the comment on `AppLanguageComp` in
        `installer/product.wxs`).

        Nothing is written to disk here: `load()` must stay free of side
        effects. The value takes effect straight away for this start, and the
        first `save()` freezes it like any other setting.
        """
        # Local import: `i18n` pulls in PySide6, which `config` must not require
        # merely to be imported (it is loaded very early, and by the tests).
        from .i18n import CONFIG_KEY, installer_language

        code = installer_language()
        if code:
            self.set_in_memory(CONFIG_KEY, code)

    def set_in_memory(self, key: str, value) -> None:
        """`set()` without writing to disk (cf. `_adopt_installer_language`)."""
        keys = key.split(".")
        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    def save(self) -> None:
        try:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Erreur sauvegarde config: {e}")

    def _merge(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._merge(result[k], v)
            else:
                result[k] = v
        return result

    def get(self, key: str, default=None):
        keys = key.split(".")
        val = self._data
        for k in keys:
            if not isinstance(val, dict) or k not in val:
                return default
            val = val[k]
        return val

    def set(self, key: str, value) -> None:
        self.set_in_memory(key, value)
        self.save()

    def get_scan_folders(self) -> list[str]:
        # normpath(): folders may have been recorded with "/" separators
        # (QFileDialog) — we normalise on read so that path comparisons
        # (e.g. remove_scan_folder) stay reliable.
        return [os.path.normpath(p) for p in self._data.get("scan_folders", [])]

    def add_scan_folder(self, path: str) -> None:
        path = os.path.normpath(path)
        folders = self.get_scan_folders()
        if path not in folders:
            folders.append(path)
            self._data["scan_folders"] = folders
            self.save()

    def remove_scan_folder(self, path: str) -> None:
        path = os.path.normpath(path)
        folders = self.get_scan_folders()
        if path in folders:
            folders.remove(path)
            self._data["scan_folders"] = folders
            self.save()

    def get_plugin_settings(self, plugin_id: str) -> dict:
        return dict(self._data.get("plugins", {}).get(plugin_id, {}))

    def set_plugin_settings(self, plugin_id: str, settings: dict) -> None:
        if "plugins" not in self._data:
            self._data["plugins"] = {}
        self._data["plugins"][plugin_id] = settings
        self.save()
