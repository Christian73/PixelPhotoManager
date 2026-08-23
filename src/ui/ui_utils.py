# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Small display helpers shared between widgets."""

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMenu, QMenuBar, QStyle, QStyleOptionMenuItem

from src.core.i18n import translate

# A mark set on a QMenu already wired, so as not to connect it twice.
_FIT_PROPERTY = "ppm_menu_width_fitted"

# Separation between the end of a label and the beginning of its shortcut, in
# widths of an "M". Two were enough to avoid the overlap, but the shortcut still
# looked glued to the text: this is THE constant to touch to loosen (or tighten)
# the shortcut column, it has no other effect — a menu without a shortcut is
# never widened.
_SHORTCUT_GAP_EM = 4


def fmt_size(size_bytes: int) -> str:
    """Formats a file size for the UI: "512 kB", "3.2 MB", "" if unknown."""
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024 * 1024:
        return translate("Units", "{n} kB").format(n=f"{size_bytes / 1024:.0f}")
    return translate("Units", "{n} MB").format(n=f"{size_bytes / (1024 * 1024):.1f}")


def _action_label(action) -> str:
    """Text actually painted: the "&" mnemonics are not displayed."""
    text = action.text()
    if "&" in text:
        text = text.replace("&&", "\x00").replace("&", "").replace("\x00", "&")
    return text


def _action_shortcut(action, label: str) -> str:
    """Shortcut shown on the right: QAction.shortcut() or the part after "\\t"."""
    seq = action.shortcut()
    if not seq.isEmpty():
        return seq.toString(QKeySequence.NativeText)
    if "\t" in label:
        return label.split("\t", 1)[1]
    return ""


def _submenus(widget) -> list:
    """Direct submenus of `widget` (a QMenu or a QMenuBar).

    Goes through `findChildren` and **never** through `QAction.menu()`: in
    PySide6 6.11, the object returned by `QAction.menu()` destroys the C++
    QMenu when its Python wrapper is collected (an emptied submenu, then a
    RuntimeError "already deleted").
    """
    return widget.findChildren(QMenu, options=Qt.FindDirectChildrenOnly)


def _item_chrome_width(menu: QMenu) -> int:
    """Width of an item apart from its text, measured by the style itself.

    Icon/tick column, margins, stylesheet padding…: the style is asked for the
    size of an item with a text of known width, and the width of that text is
    subtracted. More reliable than a home-made sum of `pixelMetric`, which
    would ignore the padding set by QStyleSheetStyle.
    """
    fm = menu.fontMetrics()
    style = menu.style()
    icon_px = style.pixelMetric(QStyle.PM_SmallIconSize, None, menu)
    # Icon/tick column: reserved by Qt only if the menu contains one.
    has_icon = any(not a.icon().isNull() for a in menu.actions())
    has_checkable = any(a.isCheckable() for a in menu.actions())
    probe = "M" * 24
    probe_w = fm.horizontalAdvance(probe)
    opt = QStyleOptionMenuItem()
    opt.initFrom(menu)
    opt.menuItemType = QStyleOptionMenuItem.Normal
    opt.checkType = QStyleOptionMenuItem.NotCheckable
    opt.menuHasCheckableItems = has_checkable
    opt.maxIconWidth = icon_px if has_icon else 0
    opt.font = menu.font()
    opt.text = probe
    total = style.sizeFromContents(
        QStyle.CT_MenuItem, opt, QSize(probe_w, fm.height()), menu
    ).width()
    return max(total - probe_w, 0)


def menu_required_width(menu: QMenu) -> int:
    """Minimum width for no label to overlap its shortcut column.

    Some Qt styles (windows11 with the application stylesheet in particular)
    underestimate the space needed when a long label sits next to a shortcut:
    the shortcut, right-aligned, ends up on top of the end of the label. So the
    item chrome + label + separation + shortcut is recomputed here.
    Returns 0 if the menu is empty (nothing to impose).
    """
    fm = menu.fontMetrics()
    style = menu.style()
    # Separation between the end of the label and the beginning of the shortcut
    # (cf. _SHORTCUT_GAP_EM): since the shortcut is right-aligned, any width
    # gained here ends up entirely in that space.
    gap = max(fm.horizontalAdvance("M" * _SHORTCUT_GAP_EM),
              _SHORTCUT_GAP_EM * fm.height() // 2)
    # Submenu arrow column: Qt reserves it for every item as soon as a submenu
    # exists.
    arrow = 0
    if _submenus(menu):
        arrow = style.pixelMetric(QStyle.PM_MenuButtonIndicator, None, menu)
    frame = 2 * (
        style.pixelMetric(QStyle.PM_MenuPanelWidth, None, menu)
        + style.pixelMetric(QStyle.PM_MenuHMargin, None, menu)
    )

    content = 0
    for action in menu.actions():
        if action.isSeparator() or not action.isVisible():
            continue
        label = _action_label(action)
        shortcut = _action_shortcut(action, label)
        label = label.split("\t", 1)[0]
        width = fm.horizontalAdvance(label)
        if shortcut:
            width += gap + fm.horizontalAdvance(shortcut)
        content = max(content, width)
    if content <= 0:
        return 0
    return content + arrow + _item_chrome_width(menu) + frame


def fit_menu_width(menu: QMenu) -> None:
    """Widens the popup if the style has not reserved the room for the shortcuts.

    `setMinimumWidth` has no visible effect when the style already computes a
    sufficient width: the popup then keeps its `sizeHint`.
    """
    menu.setMinimumWidth(menu_required_width(menu))


def install_menu_width_fix(target) -> None:
    """Wires `fit_menu_width` onto the opening of `target` (a QMenu or a QMenuBar).

    The submenus are wired on the fly at every opening of the parent: the
    dynamically built menus (Rate, plugins…) are thus covered without having to
    call this function again at every rebuild.
    """
    if isinstance(target, QMenuBar):
        menus = _submenus(target)
    elif isinstance(target, QMenu):
        menus = [target]
    else:
        return
    for menu in menus:
        if menu.property(_FIT_PROPERTY):
            continue
        menu.setProperty(_FIT_PROPERTY, True)
        menu.aboutToShow.connect(lambda m=menu: _on_menu_about_to_show(m))


def _on_menu_about_to_show(menu: QMenu) -> None:
    for sub in _submenus(menu):
        install_menu_width_fix(sub)
    fit_menu_width(menu)
