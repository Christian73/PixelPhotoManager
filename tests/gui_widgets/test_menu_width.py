# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Régression : dans les menus de la barre principale, un libellé long finissait
sous le texte de son raccourci.

Cause : la largeur d'un popup est calculée par le style. Le style natif Windows
(windows11) réserve la colonne du raccourci au plus juste — quelques pixels de
séparation seulement — si bien qu'« Exporter la sélection vers un dossier… » et
« Ctrl+Shift+E » se chevauchaient. `install_menu_width_fix()` recalcule la
largeur nécessaire à l'ouverture du menu et pose un `minimumWidth`.

Deux invariants sont vérifiés ici :
- le popup ouvert est toujours au moins aussi large que libellé + séparation +
  raccourci (sinon, chevauchement) ;
- un menu **sans** raccourci n'est pas élargi pour rien (le calcul maison doit
  rester calé sur celui du style, pas gonfler tous les menus)."""
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
    """Applique au widget le style qui sous-dimensionne les popups, s'il existe.

    Style posé sur le widget et non sur l'application : un `QApplication.setStyle`
    fuiterait sur toute la session de test. La référence est gardée sur le widget,
    Qt ne prenant pas possession de l'objet style."""
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
    """Le popup laisse-t-il de la place entre la fin du libellé et le raccourci ?"""
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
        """Convention des menus contextuels : « Libellé\\tTouche »."""
        w = QWidget()
        qtbot.addWidget(w)
        menu = QMenu(w)
        _tight_style(menu)
        menu.addAction(f"{_LONG}\tSuppr")
        fm = menu.fontMetrics()
        needed = fm.horizontalAdvance(_LONG) + fm.horizontalAdvance("Suppr")
        assert menu_required_width(menu) > needed

    def test_shortcut_column_is_aired(self, qtbot):
        """Demande utilisateur : le raccourci ne doit pas être collé au libellé.

        Le raccourci est aligné à droite du popup, donc la séparation se mesure
        sur ce que le raccourci ajoute à la largeur exigée — tout le reste (chrome,
        cadre) est identique entre les deux menus comparés."""
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
        """Le calcul maison reste calé sur celui du style : sans raccourci, il ne
        doit pas dépasser le `sizeHint` que Qt aurait choisi."""
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
        assert menu.minimumWidth() == 0        # rien tant qu'il n'est pas ouvert
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
        """Les sous-menus (Noter, applications externes…) sont branchés à
        l'ouverture du parent, y compris s'ils ont été construits après coup."""
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
    """Garde-fou PySide6 6.11 : `QAction.menu()` détruit le QMenu C++ quand le
    wrapper Python renvoyé est collecté. Énumérer les sous-menus par ce biais
    viderait les menus de l'application au premier survol — d'où `findChildren`
    dans `_submenus()`."""
    w = QWidget()
    qtbot.addWidget(w)
    menu = QMenu(w)
    sub = menu.addMenu("Sous-menu")
    sub.addAction("Truc")

    # Le piège lui-même, pour documenter qu'il est bien réel sur cette version.
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
