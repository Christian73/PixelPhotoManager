# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Petits helpers d'affichage partagés entre widgets."""

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMenu, QMenuBar, QStyle, QStyleOptionMenuItem

# Marque posée sur un QMenu déjà branché, pour ne pas connecter deux fois.
_FIT_PROPERTY = "ppm_menu_width_fitted"

# Séparation entre la fin d'un libellé et le début de son raccourci, en largeurs
# de « M ». Deux suffisaient à éviter le chevauchement, mais le raccourci restait
# visuellement collé au texte : c'est LA constante à toucher pour aérer (ou
# resserrer) la colonne des raccourcis, elle n'a aucun autre effet — un menu sans
# raccourci n'est jamais élargi.
_SHORTCUT_GAP_EM = 4


def fmt_size(size_bytes: int) -> str:
    """Formate une taille fichier pour l'UI : « 512 Ko », « 3.2 Mo », "" si inconnue."""
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} Ko"
    return f"{size_bytes / (1024 * 1024):.1f} Mo"


def _action_label(action) -> str:
    """Texte réellement peint : les mnémoniques « & » ne sont pas affichés."""
    text = action.text()
    if "&" in text:
        text = text.replace("&&", "\x00").replace("&", "").replace("\x00", "&")
    return text


def _action_shortcut(action, label: str) -> str:
    """Raccourci affiché à droite : QAction.shortcut() ou la partie après « \\t »."""
    seq = action.shortcut()
    if not seq.isEmpty():
        return seq.toString(QKeySequence.NativeText)
    if "\t" in label:
        return label.split("\t", 1)[1]
    return ""


def _submenus(widget) -> list:
    """Sous-menus directs de `widget` (QMenu ou QMenuBar).

    Passe par `findChildren` et **jamais** par `QAction.menu()` : en PySide6 6.11,
    l'objet renvoyé par `QAction.menu()` détruit le QMenu C++ quand son wrapper
    Python est collecté (sous-menu vidé, puis RuntimeError « already deleted »).
    """
    return widget.findChildren(QMenu, options=Qt.FindDirectChildrenOnly)


def _item_chrome_width(menu: QMenu) -> int:
    """Largeur d'un item hors texte, mesurée par le style lui-même.

    Colonne icône/coche, marges, padding de la feuille de style… : on demande au
    style la taille d'un item au texte connu, et on retranche la largeur de ce
    texte. Plus fiable qu'une addition de `pixelMetric` maison, qui ignorerait le
    padding posé par QStyleSheetStyle.
    """
    fm = menu.fontMetrics()
    style = menu.style()
    icon_px = style.pixelMetric(QStyle.PM_SmallIconSize, None, menu)
    # Colonne icône/coche : réservée par Qt seulement si le menu en contient.
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
    """Largeur minimale pour qu'aucun libellé ne chevauche sa colonne de raccourci.

    Certains styles Qt (windows11 avec la feuille de style applicative, notamment)
    sous-estiment la place nécessaire quand un libellé long côtoie un raccourci :
    le raccourci, aligné à droite, vient se superposer à la fin du libellé. On
    recalcule donc soi-même chrome de l'item + libellé + séparation + raccourci.
    Renvoie 0 si le menu est vide (rien à imposer).
    """
    fm = menu.fontMetrics()
    style = menu.style()
    # Séparation entre la fin du libellé et le début du raccourci (cf.
    # _SHORTCUT_GAP_EM) : le raccourci étant aligné à droite, toute largeur
    # gagnée ici se retrouve intégralement dans cet espace.
    gap = max(fm.horizontalAdvance("M" * _SHORTCUT_GAP_EM),
              _SHORTCUT_GAP_EM * fm.height() // 2)
    # Colonne de flèche des sous-menus : Qt la réserve pour tous les items dès
    # qu'un sous-menu existe.
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
    """Élargit le popup si le style n'a pas réservé la place des raccourcis.

    `setMinimumWidth` n'a aucun effet visible quand le style calcule déjà une
    largeur suffisante : le popup garde alors son `sizeHint`.
    """
    menu.setMinimumWidth(menu_required_width(menu))


def install_menu_width_fix(target) -> None:
    """Branche `fit_menu_width` sur l'ouverture de `target` (QMenu ou QMenuBar).

    Les sous-menus sont branchés à la volée à chaque ouverture du parent : les
    menus construits dynamiquement (Noter, plugins…) sont ainsi couverts sans
    avoir à rappeler cette fonction à chaque reconstruction.
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
