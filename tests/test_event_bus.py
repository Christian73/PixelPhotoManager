# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests de src/core/event_bus.py — le seul canal de communication autorisé
entre composants (cf. CLAUDE.md). EventBus ne dépend d'aucun singleton et
s'instancie librement : chaque test crée sa propre instance isolée.
"""
from src.core.event_bus import EventBus


class TestOnEmit:
    def test_emit_calls_registered_handler_with_kwargs(self):
        bus = EventBus()
        received = {}
        bus.on("photo.selected", lambda photo: received.update(photo=photo))
        bus.emit("photo.selected", photo="a.jpg")
        assert received == {"photo": "a.jpg"}

    def test_emit_calls_all_handlers_in_order(self):
        bus = EventBus()
        calls = []
        bus.on("evt", lambda: calls.append(1))
        bus.on("evt", lambda: calls.append(2))
        bus.emit("evt")
        assert calls == [1, 2]

    def test_emit_unknown_event_does_nothing(self):
        bus = EventBus()
        bus.emit("nobody.listens")  # ne doit pas lever


class TestOff:
    def test_off_unsubscribes_handler(self):
        bus = EventBus()
        calls = []
        handler = lambda: calls.append(1)
        bus.on("evt", handler)
        bus.off("evt", handler)
        bus.emit("evt")
        assert calls == []

    def test_off_unknown_handler_does_not_raise(self):
        bus = EventBus()
        bus.off("evt", lambda: None)

    def test_off_leaves_other_handlers_intact(self):
        bus = EventBus()
        calls = []
        handler_a = lambda: calls.append("a")
        handler_b = lambda: calls.append("b")
        bus.on("evt", handler_a)
        bus.on("evt", handler_b)
        bus.off("evt", handler_a)
        bus.emit("evt")
        assert calls == ["b"]


class TestOnce:
    def test_once_fires_exactly_once(self):
        bus = EventBus()
        calls = []
        bus.once("evt", lambda: calls.append(1))
        bus.emit("evt")
        bus.emit("evt")
        assert calls == [1]

    def test_once_removed_after_first_emit(self):
        bus = EventBus()
        bus.once("evt", lambda: None)
        bus.emit("evt")
        # emit() dépile via pop(event, []) : la clé disparaît complètement,
        # elle n'est pas juste vidée.
        assert "evt" not in bus._once_handlers

    def test_once_and_on_both_fire_on_same_emit(self):
        bus = EventBus()
        calls = []
        bus.on("evt", lambda: calls.append("on"))
        bus.once("evt", lambda: calls.append("once"))
        bus.emit("evt")
        assert calls == ["on", "once"]


class TestHandlerExceptionIsolation:
    def test_exception_in_handler_does_not_block_next_handler(self):
        bus = EventBus()
        calls = []

        def bad_handler():
            raise ValueError("boom")

        bus.on("evt", bad_handler)
        bus.on("evt", lambda: calls.append("ok"))
        bus.emit("evt")  # ne doit pas propager l'exception
        assert calls == ["ok"]

    def test_exception_in_once_handler_does_not_block_next_once_handler(self):
        bus = EventBus()
        calls = []

        def bad_handler():
            raise RuntimeError("boom")

        bus.once("evt", bad_handler)
        bus.once("evt", lambda: calls.append("ok"))
        bus.emit("evt")
        assert calls == ["ok"]
