# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.event_bus import EventBus
    from src.core.config import Config


class BasePlugin(ABC):
    def __init__(self, bus: "EventBus", config: "Config", settings: dict):
        self.bus = bus
        self.config = config
        self.settings = settings
        self.enabled = False

    @abstractmethod
    def activate(self) -> None: ...

    @abstractmethod
    def deactivate(self) -> None: ...

    def get_menu_items(self) -> list[dict]:
        return []

    def get_toolbar_items(self) -> list[dict]:
        return []

    def get_sidebar_widget(self):
        return None

    def get_context_menu_items(self, photos: list) -> list[dict]:
        return []

    def on_settings_changed(self, settings: dict) -> None:
        self.settings = settings
