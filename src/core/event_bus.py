from typing import Callable
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._once_handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, handler: Callable) -> None:
        self._handlers[event].append(handler)

    def once(self, event: str, handler: Callable) -> None:
        self._once_handlers[event].append(handler)

    def off(self, event: str, handler: Callable) -> None:
        if handler in self._handlers[event]:
            self._handlers[event].remove(handler)

    def emit(self, event: str, **kwargs) -> None:
        for handler in list(self._handlers.get(event, [])):
            try:
                handler(**kwargs)
            except Exception as e:
                logger.error(f"Erreur handler '{event}': {e}", exc_info=True)

        for handler in self._once_handlers.pop(event, []):
            try:
                handler(**kwargs)
            except Exception as e:
                logger.error(f"Erreur handler once '{event}': {e}", exc_info=True)


bus = EventBus()
