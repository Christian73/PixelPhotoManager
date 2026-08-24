# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Doubles shared by the tests of the MainWindow controllers (mixins).

These controllers are never instantiated on their own: their attributes
(self._catalog, self._grid, …) are created by MainWindow.__init__. The tests
therefore call their REAL methods unbound against a minimal host carrying only
what the tested path reads -- so what is checked is the wiring: which
collaborator is called, in which order, and above all which branch is taken.
"""
from types import SimpleNamespace

from PySide6.QtWidgets import QMessageBox


class RecordingSignal:
    """Signal stand-in: records what has been connected to it, and replays it."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot=None):
        self.slots.clear()

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class Recorder:
    """Records every call made on it -- `calls` holds (name, args, kwargs).

    Any unknown attribute becomes a recording method: a controller only ever
    calls its collaborators, so one class stands in for all of them. The return
    values are given at construction time (`Recorder(isVisible=True)`); a
    callable one is invoked with the real arguments."""

    def __init__(self, **returns):
        self.calls = []
        self._returns = dict(returns)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            value = self._returns.get(name)
            return value(*args, **kwargs) if callable(value) else value

        return _call

    # -- reading back -------------------------------------------------------
    def names(self) -> list:
        return [c[0] for c in self.calls]

    def called(self, name: str) -> list:
        return [c for c in self.calls if c[0] == name]

    def last(self, name: str) -> tuple:
        hits = self.called(name)
        assert hits, f"{name} jamais appele (appels: {self.names()})"
        return hits[-1][1]


def fake_thread(store: dict, key: str):
    """Builds a QThread stand-in that starts no OS thread and records itself."""

    class _T:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.started = False
            self.running = False
            self.stopped = False
            store.setdefault(key, []).append(self)

        def __getattr__(self, name):        # progress, finished, error…
            signal = RecordingSignal()
            setattr(self, name, signal)
            return signal

        def isRunning(self):
            return self.running

        def stop(self):
            self.stopped = True

        def cancel(self):
            self.stopped = True

        def wait(self, *args):
            return True

        def quit(self):
            pass

        def start(self):
            self.started = True

        def deleteLater(self):
            pass

    return _T


def make_message_box():
    """QMessageBox stand-in: records the static calls, and answers what the test
    decides. The real enums are kept -- the controllers compare against them."""

    class _Box:
        StandardButton = QMessageBox.StandardButton
        Icon = QMessageBox.Icon
        Yes = QMessageBox.StandardButton.Yes
        No = QMessageBox.StandardButton.No
        Ok = QMessageBox.StandardButton.Ok
        Cancel = QMessageBox.StandardButton.Cancel
        Warning = QMessageBox.Icon.Warning
        Information = QMessageBox.Icon.Information
        DestructiveRole = QMessageBox.ButtonRole.DestructiveRole
        ActionRole = QMessageBox.ButtonRole.ActionRole
        AcceptRole = QMessageBox.ButtonRole.AcceptRole
        RejectRole = QMessageBox.ButtonRole.RejectRole

        answer = QMessageBox.StandardButton.Yes   # question() / exec()
        clicked_role = None                       # which addButton() was pressed
        infos: list = []
        criticals: list = []
        warnings: list = []
        instances: list = []

        def __init__(self, *args):
            """QMessageBox(parent) as well as the full positional form
            QMessageBox(icon, title, text, buttons, parent) -- both are used."""
            self.buttons = []
            self.texts = [a for a in args if isinstance(a, str)][1:]  # [icon] title text
            self.title = next((a for a in args if isinstance(a, str)), "")
            self.checkbox = None
            _Box.instances.append(self)

        # -- instance API used by the controllers --------------------------
        def setIcon(self, *a):
            pass

        def setWindowTitle(self, title):
            self.title = title

        def setText(self, text):
            self.texts.append(text)

        def setInformativeText(self, text):
            self.texts.append(text)

        def setStandardButtons(self, *a):
            pass

        def setCheckBox(self, checkbox):
            self.checkbox = checkbox

        def setDefaultButton(self, *a):
            pass

        def button(self, *a):
            return Recorder()

        def addButton(self, text, role=None):
            """addButton(text, role) as well as addButton(StandardButton)."""
            marker = SimpleNamespace(text=text, role=role)
            self.buttons.append(marker)
            return marker

        def exec(self):
            return _Box.answer

        def clickedButton(self):
            return next((b for b in self.buttons if b.role == _Box.clicked_role), None)

        # -- static API ----------------------------------------------------
        @staticmethod
        def question(parent, title, body, *a, **k):
            _Box.infos.append((title, body))
            return _Box.answer

        @staticmethod
        def information(parent, title, body="", *a, **k):
            _Box.infos.append((title, body))

        @staticmethod
        def critical(parent, title, body="", *a, **k):
            _Box.criticals.append((title, body))

        @staticmethod
        def warning(parent, title, body="", *a, **k):
            _Box.warnings.append((title, body))
            # warning() called with buttons answers like question()
            return _Box.answer

    _Box.infos = []
    _Box.criticals = []
    _Box.warnings = []
    _Box.instances = []
    return _Box
