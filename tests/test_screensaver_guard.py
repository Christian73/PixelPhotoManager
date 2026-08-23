# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests `src/core/screensaver_guard.py`: setting/releasing the inhibition
(SetThreadExecutionState replaced by a spy) and recognising the Windows message
announcing the screen saver."""
import ctypes.wintypes

import pytest

from src.core import screensaver_guard as sg


@pytest.fixture
def calls(monkeypatch):
    """Spies on _set_execution_state; returns the list of the requested flags."""
    seen: list[int] = []

    def fake(flags: int) -> bool:
        seen.append(flags)
        return True

    monkeypatch.setattr(sg, "_set_execution_state", fake)
    return seen


class TestScreensaverGuard:
    def test_inhibit_requests_display_and_system(self, calls):
        guard = sg.ScreensaverGuard()
        assert guard.active is False
        assert guard.inhibit() is True
        assert guard.active is True
        assert calls == [
            sg.ES_CONTINUOUS | sg.ES_DISPLAY_REQUIRED | sg.ES_SYSTEM_REQUIRED
        ]

    def test_release_restores_continuous_only(self, calls):
        guard = sg.ScreensaverGuard()
        guard.inhibit()
        guard.release()
        assert guard.active is False
        assert calls[-1] == sg.ES_CONTINUOUS

    def test_idempotent_both_ways(self, calls):
        guard = sg.ScreensaverGuard()
        guard.inhibit()
        guard.inhibit()          # already active: no extra call
        assert len(calls) == 1
        guard.release()
        guard.release()          # already released: same thing
        assert len(calls) == 2

    def test_failure_leaves_guard_inactive(self, monkeypatch):
        monkeypatch.setattr(sg, "_set_execution_state", lambda flags: False)
        guard = sg.ScreensaverGuard()
        assert guard.inhibit() is False
        assert guard.active is False
        guard.release()   # must not raise


def _msg(message: int, wparam: int) -> ctypes.wintypes.MSG:
    """MSG structure to be kept alive as long as its address is used."""
    m = ctypes.wintypes.MSG()
    m.message = message
    m.wParam = wparam
    return m


class TestIsScreensaverCommand:
    @pytest.mark.parametrize("wparam", [sg.SC_SCREENSAVE, sg.SC_MONITORPOWER])
    def test_screensaver_and_monitor_power(self, wparam):
        msg = _msg(sg.WM_SYSCOMMAND, wparam)
        assert sg.is_screensaver_command(
            b"windows_generic_MSG", ctypes.addressof(msg)) is True

    def test_low_bits_of_wparam_are_ignored(self):
        # Windows reserves the 4 low-order bits of wParam.
        msg = _msg(sg.WM_SYSCOMMAND, sg.SC_SCREENSAVE | 0x0002)
        assert sg.is_screensaver_command(
            b"windows_generic_MSG", ctypes.addressof(msg)) is True

    def test_other_syscommand_passes_through(self):
        msg = _msg(sg.WM_SYSCOMMAND, 0xF010)   # SC_MOVE
        assert sg.is_screensaver_command(
            b"windows_generic_MSG", ctypes.addressof(msg)) is False

    def test_other_message_passes_through(self):
        msg = _msg(0x0200, sg.SC_SCREENSAVE)   # WM_MOUSEMOVE
        assert sg.is_screensaver_command(
            b"windows_generic_MSG", ctypes.addressof(msg)) is False

    def test_unknown_event_type_passes_through(self):
        msg = _msg(sg.WM_SYSCOMMAND, sg.SC_SCREENSAVE)
        assert sg.is_screensaver_command(
            b"xcb_generic_event_t", ctypes.addressof(msg)) is False

    def test_unreadable_message_passes_through(self):
        assert sg.is_screensaver_command(b"windows_generic_MSG", None) is False
