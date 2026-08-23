# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Inhibiting the screensaver and the display sleep (Windows).

During a slideshow the user watches the screen without ever touching the
keyboard or the mouse: as far as Windows is concerned the session is
*inactive*, and the screensaver — then the monitor switching off — end up
covering the slideshow. Two complementary locks are set, neither of which is
enough on its own:

1. `ScreensaverGuard` — `SetThreadExecutionState(ES_CONTINUOUS |
   ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)`: continuously rearms the idle
   counter of the display, which prevents the monitor from switching off and
   the machine from going to sleep. This is the mechanism used by video
   players, and it is enough in the vast majority of cases.
2. `is_screensaver_command()` — filtering `WM_SYSCOMMAND/SC_SCREENSAVE` in
   `nativeEvent()`: the screensaver itself is driven by the *user* idle
   counter, distinct from that of the display. Windows warns the foreground
   window before launching it; answering "message handled" cancels the launch.

**Threading constraint**: `SetThreadExecutionState` applies to the calling
thread. `inhibit()` and `release()` must therefore be called from the same
thread — in practice the UI thread.
"""
import logging

logger = logging.getLogger(__name__)

# winbase.h — EXECUTION_STATE
ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# winuser.h — message announcing the screensaver / monitor sleep
WM_SYSCOMMAND   = 0x0112
SC_SCREENSAVE   = 0xF140
SC_MONITORPOWER = 0xF170

# Types of native messages forwarded by Qt on Windows
_WIN_MSG_TYPES = (b"windows_generic_MSG", b"windows_dispatcher_MSG")


def _set_execution_state(flags: int) -> bool:
    """Calls `SetThreadExecutionState`. Returns False if the call fails or if
    the platform is not Windows (silent no-op)."""
    try:
        import ctypes
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes = [ctypes.c_uint]
        fn.restype = ctypes.c_uint
        return bool(fn(flags))
    except Exception:
        return False


class ScreensaverGuard:
    """Prevents the screen from switching off until `release()` has been called.

    Idempotent in both directions: `inhibit()` on an already active guard or
    `release()` on an inactive one does nothing."""

    def __init__(self) -> None:
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def inhibit(self) -> bool:
        """Sets the inhibition. Returns True if the OS accepted it."""
        if self._active:
            return True
        ok = _set_execution_state(
            ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED
        )
        if ok:
            self._active = True
        else:
            logger.debug("Inhibition de l'économiseur d'écran indisponible")
        return ok

    def release(self) -> None:
        """Hands back control to the OS (the screensaver becomes possible again)."""
        if not self._active:
            return
        self._active = False
        _set_execution_state(ES_CONTINUOUS)


def is_screensaver_command(event_type, message) -> bool:
    """True if `message` (the pair received by `QWidget.nativeEvent`) is the
    Windows request to start the screensaver or to switch the monitor off.

    Any anomaly (non-Windows platform, unreadable pointer) returns False: the
    message then follows its normal processing."""
    try:
        kind = bytes(event_type)
    except Exception:
        return False
    if kind not in _WIN_MSG_TYPES:
        return False
    try:
        import ctypes.wintypes
        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message != WM_SYSCOMMAND:
            return False
        # The 4 low-order bits of wParam are reserved for the system.
        return (msg.wParam & 0xFFF0) in (SC_SCREENSAVE, SC_MONITORPOWER)
    except Exception:
        return False
