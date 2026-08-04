# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Inhibition de l'économiseur d'écran et de la veille de l'écran (Windows).

Pendant un diaporama, l'utilisateur regarde l'écran sans jamais toucher au
clavier ni à la souris : pour Windows, la session est *inactive*, et
l'économiseur d'écran puis l'extinction du moniteur finissent par recouvrir le
diaporama. Deux verrous complémentaires sont posés, aucun des deux ne
suffisant seul :

1. `ScreensaverGuard` — `SetThreadExecutionState(ES_CONTINUOUS |
   ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)` : réarme en continu le compteur
   d'inactivité de l'affichage, ce qui empêche l'extinction du moniteur et la
   mise en veille de la machine. C'est le mécanisme utilisé par les lecteurs
   vidéo, et il suffit dans la grande majorité des cas.
2. `is_screensaver_command()` — filtrage de `WM_SYSCOMMAND/SC_SCREENSAVE` dans
   `nativeEvent()` : l'économiseur d'écran, lui, est piloté par le compteur
   d'inactivité *utilisateur*, distinct de celui de l'affichage. Windows
   prévient la fenêtre au premier plan avant de le lancer ; répondre « message
   traité » annule le lancement.

**Contrainte de thread** : `SetThreadExecutionState` s'applique au thread
appelant. `inhibit()` et `release()` doivent donc être appelés depuis le même
thread — en pratique le thread UI.
"""
import logging

logger = logging.getLogger(__name__)

# winbase.h — EXECUTION_STATE
ES_CONTINUOUS       = 0x80000000
ES_SYSTEM_REQUIRED  = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# winuser.h — message d'annonce de l'économiseur d'écran / veille moniteur
WM_SYSCOMMAND   = 0x0112
SC_SCREENSAVE   = 0xF140
SC_MONITORPOWER = 0xF170

# Types de messages natifs remontés par Qt sous Windows
_WIN_MSG_TYPES = (b"windows_generic_MSG", b"windows_dispatcher_MSG")


def _set_execution_state(flags: int) -> bool:
    """Appelle `SetThreadExecutionState`. Renvoie False si l'appel échoue ou
    si la plateforme n'est pas Windows (no-op silencieux)."""
    try:
        import ctypes
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes = [ctypes.c_uint]
        fn.restype = ctypes.c_uint
        return bool(fn(flags))
    except Exception:
        return False


class ScreensaverGuard:
    """Empêche l'écran de s'éteindre tant que `release()` n'a pas été appelé.

    Idempotent dans les deux sens : `inhibit()` sur un garde déjà actif ou
    `release()` sur un garde inactif ne fait rien."""

    def __init__(self) -> None:
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def inhibit(self) -> bool:
        """Pose l'inhibition. Renvoie True si l'OS l'a acceptée."""
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
        """Rend la main à l'OS (l'économiseur d'écran redevient possible)."""
        if not self._active:
            return
        self._active = False
        _set_execution_state(ES_CONTINUOUS)


def is_screensaver_command(event_type, message) -> bool:
    """True si `message` (couple reçu par `QWidget.nativeEvent`) est la demande
    Windows de lancer l'économiseur d'écran ou d'éteindre le moniteur.

    Toute anomalie (plateforme non Windows, pointeur illisible) renvoie False :
    le message suit alors son traitement normal."""
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
        # Les 4 bits de poids faible de wParam sont réservés au système.
        return (msg.wParam & 0xFFF0) in (SC_SCREENSAVE, SC_MONITORPOWER)
    except Exception:
        return False
