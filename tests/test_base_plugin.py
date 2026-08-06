# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/base_plugin.py (BasePlugin) et
src/core/processor_plugin.py (ProcessorPlugin) : valeurs par défaut des
méthodes non abstraites, via de petites sous-classes concrètes minimales."""
from PIL import Image

from src.core.base_plugin import BasePlugin
from src.core.processor_plugin import ProcessorPlugin


class _ConcretePlugin(BasePlugin):
    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass


class TestBasePluginDefaults:
    def _make(self):
        return _ConcretePlugin(bus=object(), config=object(), settings={"a": 1})

    def test_constructor_stores_bus_config_settings_and_starts_disabled(self):
        plugin = self._make()
        assert plugin.settings == {"a": 1}
        assert plugin.enabled is False

    def test_get_menu_items_default_empty(self):
        assert self._make().get_menu_items() == []

    def test_get_toolbar_items_default_empty(self):
        assert self._make().get_toolbar_items() == []

    def test_get_sidebar_widget_default_none(self):
        assert self._make().get_sidebar_widget() is None

    def test_get_context_menu_items_default_empty(self):
        assert self._make().get_context_menu_items(photos=[]) == []

    def test_on_settings_changed_replaces_settings(self):
        plugin = self._make()
        plugin.on_settings_changed({"b": 2})
        assert plugin.settings == {"b": 2}


class _ConcreteProcessor(ProcessorPlugin):
    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass

    def process(self, image, params):
        return image

    def get_default_params(self) -> dict:
        return {"strength": 0.5, "mode": "fast"}


class TestProcessorPluginDefaults:
    def _make(self):
        return _ConcreteProcessor(bus=object(), config=object(), settings={})

    def test_class_attribute_defaults(self):
        processor = self._make()
        assert processor.name == ""
        assert processor.category == "Général"
        assert processor.supports_preview is True
        assert processor.supports_batch is True

    def test_validate_params_fills_in_missing_defaults(self):
        processor = self._make()
        result = processor.validate_params({"strength": 0.9})
        assert result == {"strength": 0.9, "mode": "fast"}

    def test_validate_params_with_empty_input_returns_defaults(self):
        processor = self._make()
        assert processor.validate_params({}) == {"strength": 0.5, "mode": "fast"}

    def test_estimate_duration_default_is_one_second(self):
        processor = self._make()
        img = Image.new("RGB", (10, 10))
        assert processor.estimate_duration(img, {}) == 1.0
