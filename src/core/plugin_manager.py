# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Type

from .app_dirs import APP_DATA_DIR
from .base_plugin import BasePlugin
from .event_bus import bus

logger = logging.getLogger(__name__)


def _app_root() -> Path:
    """Racine de l'application : dossier de l'exe en mode figé, racine du dépôt en mode dev.
    Ne jamais dériver ceci du cwd — un lancement depuis un autre dossier de travail
    ne doit pas faire chercher les plugins au mauvais endroit."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


class PluginManager:
    PLUGIN_DIRS = [
        _app_root() / "plugins",
        _app_root() / "src" / "plugins",
        APP_DATA_DIR / "plugins",
    ]

    def __init__(self, config):
        self.config = config
        self._available: dict[str, dict] = {}
        self._loaded: dict[str, BasePlugin] = {}

    def discover(self) -> list[dict]:
        self._available.clear()
        for plugin_dir in self.PLUGIN_DIRS:
            if not plugin_dir.exists():
                continue
            for item in plugin_dir.iterdir():
                if not item.is_dir():
                    continue
                manifest_path = item / "plugin.json"
                if not manifest_path.exists():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["_path"] = str(item)
                    self._available[manifest["id"]] = manifest
                    logger.info(f"Plugin découvert : {manifest['name']}")
                except Exception as e:
                    logger.error(f"Erreur lecture plugin {item}: {e}")
        return list(self._available.values())

    def activate(self, plugin_id: str) -> bool:
        if plugin_id in self._loaded:
            return True
        if plugin_id not in self._available:
            logger.error(f"Plugin inconnu : {plugin_id}")
            return False
        manifest = self._available[plugin_id]
        plugin_path = Path(manifest["_path"]) / "plugin.py"
        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{plugin_id}", plugin_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            plugin_class = self._find_plugin_class(module)
            if not plugin_class:
                raise ValueError("Aucune classe BasePlugin trouvée")
            settings = self.config.get_plugin_settings(plugin_id)
            instance = plugin_class(bus, self.config, settings)
            instance.activate()
            instance.enabled = True
            self._loaded[plugin_id] = instance
            bus.emit("plugin.activated", plugin_id=plugin_id)
            logger.info(f"Plugin activé : {manifest['name']}")
            return True
        except Exception as e:
            logger.error(f"Erreur activation plugin {plugin_id}: {e}", exc_info=True)
            return False

    def deactivate(self, plugin_id: str) -> bool:
        if plugin_id not in self._loaded:
            return True
        try:
            instance = self._loaded[plugin_id]
            instance.deactivate()
            instance.enabled = False
            del self._loaded[plugin_id]
            bus.emit("plugin.deactivated", plugin_id=plugin_id)
            return True
        except Exception as e:
            logger.error(f"Erreur désactivation {plugin_id}: {e}", exc_info=True)
            return False

    def get_active_plugins(self) -> list[BasePlugin]:
        return list(self._loaded.values())

    def list_available(self) -> list[dict]:
        return [
            {**manifest, "active": manifest["id"] in self._loaded}
            for manifest in self._available.values()
        ]

    def _find_plugin_class(self, module) -> Type[BasePlugin] | None:
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, BasePlugin) and obj is not BasePlugin:
                return obj
        return None
