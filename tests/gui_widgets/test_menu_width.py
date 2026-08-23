# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Regression: in the menus of the main bar, a long label ended up
underneath the text of its shortcut.

Cause: the width of a popup is computed by the style. The native Windows style
(windows11) reserves the shortcut column as tightly as possible -- only a few
pixels of separation -- so that "Export the selection to a folder…" and
"Ctrl+Shift+E" overlapped. `install_menu_width_fix()` recomputes the required
width when the menu opens and sets a `minimumWidth`.

Two invariants are checked here:
- the open popup is always at least as wide as label + separation + shortcut
  (otherwise, overlap);
- a menu **without** a shortcut is not widened for nothing (the home-made
  computation must stay aligned with the style's own, not inflate every menu)."""
import gc

import pytest
import shiboken6
from PySide6.QtCore import QPoint
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu, QMenuBar, QStyleFactory, QWidget

from src.ui.ui_utils import (
    _SHORTCUT_GAP_EM, _submenus, fit_menu_width, install_menu_width_fix,
    menu_required_width,
)

_LONG = "Exporter la sélection vers un dossier…"
_SHORTCUT = "Ctrl+Shift+E"


def _tight_style(widget) -> None:
    """Applies to the widget the style that under-sizes the popups, if it
    exists.

    The style is set on the widget and not on the application: a
    `QApplication.setStyle` would leak over the whole test session. The
    reference is kept on the widget, Qt not taking ownership of the style
    object."""
    style = QStyleFactory.create("windows11") or QStyleFactory.create("windowsvista")
    if style is not None:
        widget._ppm_test_style = style
        widget.setStyle(style)


def _menu_with_shortcut(parent) -> QMenu:
    menu = QMenu(parent)
    _tight_style(menu)
    act = QAction(_LONG, menu)
    act.setShortcut(QKeySequence(_SHORTCUT))
    menu.addAction(act)
    return menu


def _overlap_free(menu: QMenu, label: str, shortcut: str) -> bool:
    """Does the popup leave room between the end of the label and the shortcut?"""
    fm = menu.fontMetrics()
    return menu.width() >= fm.horizontalAdvance(label) + fm.horizontalAdvance(shortcut)


class TestMenuRequiredWidth:
    def test_covers_label_plus_shortcut(self, qtbot):
        w = QWidget()
        qtbot.addWidget(w)
        menu = _menu_with_shortcut(w)
        fm = menu.fontMetrics()
        needed = fm.horizontalAdvance(_LONG) + fm.horizontalAdvance(_SHORTCUT)
        assert menu_required_width(menu) > needed

    def test_shortcut_written_with_a_tab_counts_too(self, qtbot):
        """Convention of the context menus: "Label\\tKey"."""
        w = QWidget()
        qtbot.addWidget(w)
        menu = QMenu(w)
        _tight_style(menu)
        menu.addAction(f"{_LONG}\tSuppr")
        fm = menu.fontMetrics()
        needed = fm.horizontalAdvance(_LONG) + fm.horizontalAdvance("Suppr")
        assert menu_required_width(menu) > needed

    def test_shortcut_column_is_aired(self, qtbot):
        """User request: the shortcut must not be stuck to the label.

        The shortcut is right-aligned in the popup, so the separation is
        measured on what the shortcut adds to the required width -- everything
        else (chrome, frame) is identical between the two menus compared."""
        w = QWidget()
        qtbot.addWidget(w)
        with_shortcut = _menu_with_shortcut(w)
        plain = QMenu(w)
        _tight_style(plain)
        plain.addAction(_LONG)

        fm = with_shortcut.fontMetrics()
        assert _SHORTCUT_GAP_EM >= 3, "séparation trop maigre pour être lisible"
        delta = menu_required_width(with_shortcut) - menu_required_width(plain)
        assert delta >= (fm.horizontalAdvance(_SHORTCUT)
                         + fm.horizontalAdvance("M" * _SHORTCUT_GAP_EM))

    def test_menu_without_shortcut_is_not_widened(self, qtbot):
        """The home-made computation stays aligned with the style's own: without a
        shortcut, it must not exceed the `sizeHint` Qt would have chosen."""
        w = QWidget()
        qtbot.addWidget(w)
        menu = QMenu(w)
        _tight_style(menu)
        menu.addAction("Open in File Explorer")
        menu.addAction("Rename…")
        menu.ensurePolished()
        hint = menu.sizeHint().width()
        assert menu_required_width(menu) <= hint + max(8, hint // 20)

    def test_empty_menu_imposes_nothing(self, qtbot):
        w = QWidget()
        qtbot.addWidget(w)
        menu = QMenu(w)
        assert menu_required_width(menu) == 0
        fit_menu_width(menu)
        assert menu.minimumWidth() == 0


class TestInstallMenuWidthFix:
    def test_popup_applies_the_minimum_width(self, qtbot):
        w = QWidget()
        qtbot.addWidget(w)
        menu = _menu_with_shortcut(w)
        install_menu_width_fix(menu)
        assert menu.minimumWidth() == 0        # nothing as long as it is not open
        menu.popup(QPoint(0, 0))
        try:
            assert menu.minimumWidth() == menu_required_width(menu)
            assert _overlap_free(menu, _LONG, _SHORTCUT)
        finally:
            menu.hide()

    def test_menubar_wires_every_menu(self, qtbot):
        w = QWidget()
        qtbot.addWidget(w)
        bar = QMenuBar(w)
        _tight_style(bar)
        for title in ("File", "Tools"):
            m = bar.addMenu(title)
            _tight_style(m)
            act = QAction(_LONG, m)
            act.setShortcut(QKeySequence(_SHORTCUT))
            m.addAction(act)
        install_menu_width_fix(bar)
        for menu in _submenus(bar):
            menu.popup(QPoint(0, 0))
            try:
                assert menu.minimumWidth() > 0
                assert _overlap_free(menu, _LONG, _SHORTCUT)
            finally:
                menu.hide()

    def test_submenu_is_wired_when_the_parent_opens(self, qtbot):
        """The submenus (Rate, external applications…) are wired when the parent
        opens, including when they were built afterwards."""
        w = QWidget()
        qtbot.addWidget(w)
        menu = QMenu(w)
        _tight_style(menu)
        menu.addAction("Open")
        install_menu_width_fix(menu)
        sub = menu.addMenu("Rate")
        _tight_style(sub)
        act = QAction("Trois étoiles et un libellé volontairement long", sub)
        act.setShortcut(QKeySequence("Ctrl+3"))
        sub.addAction(act)

        menu.popup(QPoint(0, 0))
        menu.hide()
        sub.popup(QPoint(0, 0))
        try:
            assert sub.minimumWidth() > 0
            assert _overlap_free(sub, act.text(), "Ctrl+3")
        finally:
            sub.hide()

    def test_connection_is_installed_only_once(self, qtbot):
        w = QWidget()
        qtbot.addWidget(w)
        menu = _menu_with_shortcut(w)
        install_menu_width_fix(menu)
        install_menu_width_fix(menu)
        install_menu_width_fix(menu)
        assert menu.receivers("2aboutToShow()") == 1


def test_submenus_never_uses_qaction_menu(qtbot):
    """PySide6 6.11 safety net: `QAction.menu()` destroys the C++ QMenu when
    the returned Python wrapper is collected. Enumerating the submenus that way
    would empty the menus of the application on the first hover -- hence
    `findChildren` in `_submenus()`."""
    w = QWidget()
    qtbot.addWidget(w)
    menu = QMenu(w)
    sub = menu.addMenu("Sous-menu")
    sub.addAction("Truc")

    # The trap itself, to document that it is indeed real on this version.
    doomed = QMenu(w)
    doomed_sub = doomed.addMenu("Sous-menu")
    for action in doomed.actions():
        _ = action.menu()
    del action, _
    gc.collect()
    if shiboken6.isValid(doomed_sub):
        pytest.skip("PySide6 ne détruit plus le sous-menu via QAction.menu()")

    assert [m.title() for m in _submenus(menu)] == ["Sous-menu"]
    gc.collect()
    assert shiboken6.isValid(sub)
    assert [a.text() for a in sub.actions()] == ["Truc"]
