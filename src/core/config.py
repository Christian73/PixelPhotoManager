import json
import logging
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
    },
    "faces": {
        "similarity_eps": 0.5,
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
                self._data = self._merge(_DEFAULTS.copy(), saved)
                return
            except Exception as e:
                logger.error(f"Erreur lecture config: {e}")
        self._data = json.loads(json.dumps(_DEFAULTS))

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
        keys = key.split(".")
        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value
        self.save()

    def get_scan_folders(self) -> list[str]:
        return list(self._data.get("scan_folders", []))

    def add_scan_folder(self, path: str) -> None:
        folders = self.get_scan_folders()
        if path not in folders:
            folders.append(path)
            self._data["scan_folders"] = folders
            self.save()

    def remove_scan_folder(self, path: str) -> None:
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
