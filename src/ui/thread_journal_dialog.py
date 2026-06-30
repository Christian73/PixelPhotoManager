# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Dialogue d'analyse du journal d'activité des threads.

Accessible via Outils › Journal des threads…
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from src.core.thread_journal import journal


# ── seuils de performance ───────────────────────────────────────────────────
_SLOW_MS = 500      # lent (orange)
_WARN_MS = 2_000    # très lent / critique (rouge)

# ── seuils spécifiques FaceIndexThread (temps entre deux photos consécutives) ─
_FACE_STEP_NORMAL_MS = 10_000   # < 10 s/photo  → progression normale
_FACE_STEP_SLOW_MS   = 20_000   # < 20 s/photo  → lent mais progresse
# au-delà de _FACE_STEP_SLOW_MS → potentiellement bloqué

# ── couleurs par type d'événement ───────────────────────────────────────────
_EVENT_COLORS = {
    "START": "#1a3a1a",
    "STEP":  "#1a1a2e",
    "END":   "#1a3040",
    "ERROR": "#3a1a1a",
    "WARN":  "#3a2a00",
}
_EVENT_FG = {
    "START": "#7dbb7d",
    "STEP":  "#8888cc",
    "END":   "#7aabdb",
    "ERROR": "#dd6666",
    "WARN":  "#cc9900",
}

# ── pistes d'amélioration par thread ────────────────────────────────────────
_THREAD_HINTS: dict[str, list[str]] = {
    "FaceIndexThread": [
        "L'analyse de visages (DeepFace/RetinaFace) prend 10–30 s/photo sans accélération GPU.",
        "Vérifier si TensorFlow détecte un GPU (commande : nvidia-smi ; CUDA installé ?).",
        "Le modèle se ré-initialise à chaque lancement — envisager un warmup persistant au démarrage de l'app.",
        "Réduire la fréquence de clustering intermédiaire (paramètre _CLUSTER_EVERY dans face_indexer.py).",
    ],
    "ScanThread": [
        "Un scan lent peut indiquer un dossier réseau, un disque lent ou un grand nombre de fichiers.",
        "Le mode force=True relit tous les fichiers — l'utiliser uniquement si nécessaire.",
        "Vérifier si des dossiers temporaires ou système sont inclus accidentellement.",
    ],
    "ClusterThread": [
        "DBSCAN devient lent au-delà de ~10 000 embeddings — envisager un sous-échantillonnage.",
        "Mettre en cache les embeddings déjà clusterisés pour éviter un recalcul complet.",
        "Ajuster les paramètres eps/min_samples pour réduire la complexité du calcul.",
    ],
    "ThumbnailThread": [
        "Un grand nombre de vignettes manquantes peut saturer le thread de génération.",
        "Vérifier si certaines images sont corrompues (génération lente ou répétée en échec).",
    ],
}


def _face_index_step_stats(entries: list[dict]) -> tuple[int, float, float] | None:
    """
    Calcule les durées inter-photos pour FaceIndexThread à partir des STEPs.
    Retourne (nb_steps, avg_ms, max_ms) ou None si moins de 2 STEPs disponibles.
    """
    steps = sorted(
        (e for e in entries
         if e.get("thread") == "FaceIndexThread"
         and e.get("event") == "STEP"
         and e.get("elapsed_ms") is not None),
        key=lambda e: e["elapsed_ms"],
    )
    if len(steps) < 2:
        return None
    deltas = [
        steps[i]["elapsed_ms"] - steps[i - 1]["elapsed_ms"]
        for i in range(1, len(steps))
    ]
    return len(steps), statistics.mean(deltas), max(deltas)


# ═══════════════════════════════════════════════════════════════════════════
# Compte rendu visuel
# ═══════════════════════════════════════════════════════════════════════════

class _CompteRenduPanel(QGroupBox):
    """Bilan d'exécution avec badges colorés par thread."""

    def __init__(self, parent=None) -> None:
        super().__init__("Bilan d'exécution", parent)
        outer = QVBoxLayout(self)
        outer.setSpacing(4)
        outer.setContentsMargins(10, 6, 10, 8)

        self._banner = QLabel()
        self._banner.setFont(QFont("Segoe UI", 10, QFont.Bold))
        outer.addWidget(self._banner)

        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(2)
        self._grid.setContentsMargins(4, 0, 4, 0)
        outer.addWidget(self._grid_widget)

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _lbl(text: str, style: str) -> QLabel:
        w = QLabel(text)
        w.setStyleSheet(style + " font-family: Consolas; font-size: 10px;")
        return w

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ── public ──────────────────────────────────────────────────────────────

    def populate(self, entries: list[dict]) -> None:
        self._clear_grid()

        if not entries:
            self._banner.setText("  Aucune donnée dans le journal.")
            self._banner.setStyleSheet("color: #888;")
            return

        # ── stats par thread
        durations:  dict[str, list[float]] = defaultdict(list)
        err_counts: dict[str, int]         = defaultdict(int)
        last_event: dict[str, str]         = {}

        for e in entries:
            th = e.get("thread", "?")
            ev = e.get("event", "")
            last_event[th] = ev
            if ev == "ERROR":
                err_counts[th] += 1
            el = e.get("elapsed_ms")
            if ev == "END" and el is not None:
                durations[th].append(el)

        threads = sorted(set(e.get("thread", "?") for e in entries))
        face_step_stats = _face_index_step_stats(entries)
        _face_ok = face_step_stats is not None and face_step_stats[2] < _FACE_STEP_SLOW_MS

        n_critical = sum(
            1 for th in threads
            if err_counts[th] or (
                durations[th] and max(durations[th]) >= _WARN_MS
                and not (th == "FaceIndexThread" and _face_ok)
            )
        )
        n_slow = sum(
            1 for th in threads
            if not err_counts[th]
            and durations[th] and _SLOW_MS <= max(durations[th]) < _WARN_MS
            and not (th == "FaceIndexThread" and _face_ok)
        )

        # ── bannière globale
        if n_critical:
            self._banner.setText(
                f"  ⚠  {n_critical} thread(s) en erreur ou trop lent(s)"
            )
            self._banner.setStyleSheet("color: #dd6666; padding: 2px 0 4px 0;")
        elif n_slow:
            self._banner.setText(
                f"  ⚠  {n_slow} thread(s) légèrement lent(s)"
            )
            self._banner.setStyleSheet("color: #cc8844; padding: 2px 0 4px 0;")
        else:
            self._banner.setText(
                "  ✓  Tout fonctionne normalement — aucune anomalie détectée"
            )
            self._banner.setStyleSheet("color: #5dbb5d; padding: 2px 0 4px 0;")

        # ── lignes par thread
        for row, th in enumerate(threads):
            durs  = durations[th]
            errs  = err_counts[th]
            runs  = len(durs)
            avg   = statistics.mean(durs) if durs else None
            mx    = max(durs)             if durs else None
            last  = last_event.get(th, "")

            # badge
            if errs and mx is not None and mx >= _WARN_MS:
                badge, bstyle = "  ✗  ERREUR + TROP LONG", "color:#dd6666;font-weight:bold;"
            elif errs:
                badge, bstyle = f"  ✗  ERREUR ({errs})", "color:#dd6666;font-weight:bold;"
            elif mx is not None and mx >= _WARN_MS:
                badge, bstyle = "  ●  TROP LONG", "color:#dd6666;font-weight:bold;"
            elif mx is not None and mx >= _SLOW_MS:
                badge, bstyle = "  ●  LENT",      "color:#cc8844;font-weight:bold;"
            elif last == "START":
                badge, bstyle = "  ●  EN COURS",  "color:#8888cc;font-weight:bold;"
            elif runs:
                badge, bstyle = "  ✓  OK",        "color:#5dbb5d;font-weight:bold;"
            else:
                badge, bstyle = "  –",            "color:#666;"
            # FaceIndexThread : réévaluer d'après le temps par photo, pas la durée totale
            if th == "FaceIndexThread" and not errs and face_step_stats is not None:
                _nb, _avg_s, _max_s = face_step_stats
                if _max_s < _FACE_STEP_NORMAL_MS:
                    badge  = f"  ✓  PROGRESSE  ({_nb} photos)"
                    bstyle = "color:#5dbb5d;font-weight:bold;"
                elif _max_s < _FACE_STEP_SLOW_MS:
                    badge  = f"  ●  LENT/PHOTO  ({_nb} photos)"
                    bstyle = "color:#cc8844;font-weight:bold;"

            # stats texte
            if th == "FaceIndexThread" and face_step_stats is not None:
                _nb, _avg_s, _max_s = face_step_stats
                parts = [f"{_nb} photo(s) traitée(s)", f"moy {_avg_s / 1000:.0f} s/photo"]
                _c = (
                    "#dd6666" if _max_s >= _FACE_STEP_SLOW_MS * 2 else
                    "#cc8844" if _max_s >= _FACE_STEP_SLOW_MS else "#777"
                )
                max_label = f"<span style='color:{_c};'>max {_max_s / 1000:.0f} s/photo</span>"
            else:
                parts = []
                if runs:
                    parts.append(f"{runs} run{'s' if runs > 1 else ''}")
                if avg is not None:
                    parts.append(f"moy {avg:,.0f} ms")
                if mx is not None:
                    color_mx = (
                        "#dd6666" if mx >= _WARN_MS else
                        "#cc8844" if mx >= _SLOW_MS else "#777"
                    )
                    max_label = f"<span style='color:{color_mx};'>max {mx:,.0f} ms</span>"
                else:
                    max_label = ""

            lbl_name  = self._lbl(f"  {th}", "color:#ccc;")
            lbl_badge = self._lbl(badge, bstyle)
            lbl_badge.setMinimumWidth(150)

            lbl_stats = QLabel("   ".join(parts) + ("   " + max_label if max_label else ""))
            lbl_stats.setStyleSheet("font-family: Consolas; font-size: 10px; color: #777;")

            self._grid.addWidget(lbl_name,  row, 0)
            self._grid.addWidget(lbl_badge, row, 1)
            self._grid.addWidget(lbl_stats, row, 2)

        self._grid.setColumnStretch(2, 1)


# ═══════════════════════════════════════════════════════════════════════════
# Résumé tabulaire
# ═══════════════════════════════════════════════════════════════════════════

class _SummaryTable(QTableWidget):
    _HEADERS = ["Thread", "Runs", "Durée moy.", "Durée max.", "Erreurs", "Statut"]

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self._HEADERS), parent)
        self.setHorizontalHeaderLabels(self._HEADERS)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(self._HEADERS)):
            self.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setStyleSheet("alternate-background-color: #252525;")

    def populate(self, entries: list[dict]) -> None:
        durations:  dict[str, list[float]] = defaultdict(list)
        errors:     dict[str, int]         = defaultdict(int)
        last_event: dict[str, str]         = {}
        face_step_stats = _face_index_step_stats(entries)

        for e in entries:
            th = e.get("thread", "?")
            ev = e.get("event", "")
            last_event[th] = ev
            if ev == "ERROR":
                errors[th] += 1
            el = e.get("elapsed_ms")
            if ev == "END" and el is not None:
                durations[th].append(el)

        threads = sorted(set(e.get("thread", "?") for e in entries))
        self.setRowCount(len(threads))

        for row, th in enumerate(threads):
            durs  = durations[th]
            errs  = errors[th]
            runs  = len(durs)
            avg   = statistics.mean(durs) if durs else None
            mx    = max(durs)             if durs else None
            last  = last_event.get(th, "")

            status = "✓" if last == "END" else ("⚠ En cours" if last == "START" else "–")
            if errs:
                status = f"✗ {errs} erreur(s)"

            if th == "FaceIndexThread" and face_step_stats is not None:
                _nb, _avg_s, _max_s = face_step_stats
                if not errs and _max_s < _FACE_STEP_SLOW_MS:
                    status = f"✓ Progresse ({_nb} photos)"
                cells = [
                    th,
                    f"{_nb} photos",
                    f"{_avg_s / 1000:.0f} s/photo",
                    f"{_max_s / 1000:.0f} s/photo",
                    str(errs) if errs else "0",
                    status,
                ]
            else:
                cells = [
                    th,
                    str(runs) if runs else "–",
                    f"{avg:,.0f} ms" if avg is not None else "–",
                    f"{mx:,.0f} ms"  if mx  is not None else "–",
                    str(errs) if errs else "0",
                    status,
                ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignCenter if col > 0 else Qt.AlignVCenter | Qt.AlignLeft
                )
                if errs and col == len(cells) - 1:
                    item.setForeground(QColor(_EVENT_FG["ERROR"]))
                elif mx is not None and mx >= _WARN_MS and col == 3:
                    item.setForeground(QColor(_EVENT_FG["WARN"]))
                elif mx is not None and mx >= _SLOW_MS and col == 3:
                    item.setForeground(QColor("#cc8844"))
                self.setItem(row, col, item)


# ═══════════════════════════════════════════════════════════════════════════
# Tableau d'événements bruts
# ═══════════════════════════════════════════════════════════════════════════

class _EventTable(QTableWidget):
    _HEADERS = ["Heure", "Thread", "Événement", "Message", "Durée (ms)"]

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self._HEADERS), parent)
        self.setHorizontalHeaderLabels(self._HEADERS)
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for i in [0, 1, 2, 4]:
            self.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.verticalHeader().setVisible(False)
        self.setFont(QFont("Consolas", 9))
        self.setWordWrap(False)

    def populate(self, entries: list[dict],
                 thread_filter: str = "", text_filter: str = "") -> None:
        filtered = [
            e for e in entries
            if (not thread_filter or e.get("thread", "") == thread_filter)
            and (not text_filter
                 or text_filter.lower() in e.get("msg", "").lower()
                 or text_filter.lower() in e.get("thread", "").lower())
        ]
        self.setRowCount(len(filtered))

        for row, e in enumerate(filtered):
            event   = e.get("event", "")
            elapsed = e.get("elapsed_ms")
            bg = QColor(_EVENT_COLORS.get(event, "#1e1e1e"))
            fg = QColor(_EVENT_FG.get(event, "#ccc"))

            wall  = e.get("wall", "")[-12:]   # HH:MM:SS.mmm
            cells = [
                wall,
                e.get("thread", ""),
                event,
                e.get("msg", ""),
                f"{elapsed:,.1f}" if elapsed is not None else "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setBackground(bg)
                item.setForeground(fg)
                if col == 4 and elapsed is not None:
                    if elapsed >= _WARN_MS:
                        item.setForeground(QColor(_EVENT_FG["WARN"]))
                    elif elapsed >= _SLOW_MS:
                        item.setForeground(QColor("#cc8844"))
                self.setItem(row, col, item)

        self.scrollToBottom()


# ═══════════════════════════════════════════════════════════════════════════
# Rapport de problèmes — génération du texte
# ═══════════════════════════════════════════════════════════════════════════

def _generate_problems_report(entries: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d à %H:%M:%S")
    sep = "━" * 62

    durations:   dict[str, list[float]] = defaultdict(list)
    err_list:    dict[str, list[dict]]  = defaultdict(list)
    end_list:    dict[str, list[dict]]  = defaultdict(list)
    last_event:  dict[str, str]         = {}

    for e in entries:
        th = e.get("thread", "?")
        ev = e.get("event", "")
        last_event[th] = ev
        el = e.get("elapsed_ms")
        if ev == "END" and el is not None:
            durations[th].append(el)
            end_list[th].append(e)
        if ev == "ERROR":
            err_list[th].append(e)

    threads = sorted(set(e.get("thread", "?") for e in entries))
    face_step_stats = _face_index_step_stats(entries)
    _face_ok = face_step_stats is not None and face_step_stats[2] < _FACE_STEP_SLOW_MS

    problem_threads = [
        th for th in threads
        if err_list[th] or (
            durations[th] and max(durations[th]) >= _SLOW_MS
            and not (th == "FaceIndexThread" and _face_ok)
        )
    ]
    ok_threads = [th for th in threads if th not in problem_threads]

    lines: list[str] = []
    lines.append("RAPPORT DE PERFORMANCE — PixelPhotoManager")
    lines.append(f"Généré le {now}")
    lines.append(f"Journal : {len(entries)} entrée(s) analysée(s)")
    lines.append("")

    if not entries:
        lines.append("(journal vide)")
        return "\n".join(lines)

    # ── section problèmes
    if problem_threads:
        lines.append(sep)
        lines.append("PROBLÈMES DÉTECTÉS")
        lines.append(sep)
        lines.append("")

        for th in problem_threads:
            durs = durations[th]
            errs = err_list[th]
            runs = len(durs)
            avg  = statistics.mean(durs) if durs else None
            mx   = max(durs)             if durs else None

            if errs and mx is not None and mx >= _WARN_MS:
                severity = "CRITIQUE — erreur(s) ET durée excessive"
            elif mx is not None and mx >= _WARN_MS:
                severity = "CRITIQUE — durée excessive"
            elif errs:
                severity = f"ERREUR(S) — {len(errs)} erreur(s)"
            else:
                severity = "LENTEUR — durée au-dessus du seuil"

            lines.append(f"[{severity}]  {th}")

            if durs:
                lines.append(f"  • {runs} exécution(s) enregistrée(s)")
                lines.append(
                    f"  • Durée moyenne : {avg:,.0f} ms"
                    f"   (seuil lent : {_SLOW_MS} ms / critique : {_WARN_MS} ms)"
                )
                lines.append(f"  • Durée maximale : {mx:,.0f} ms")

                slow_ends = sorted(
                    end_list[th], key=lambda e: e.get("elapsed_ms", 0), reverse=True
                )[:5]
                if slow_ends:
                    lines.append("  • Opérations les plus lentes :")
                    for e in slow_ends:
                        wall = e.get("wall", "")
                        el   = e.get("elapsed_ms", 0)
                        msg  = e.get("msg", "")
                        lines.append(f"      {wall}   {el:>10,.0f} ms   {msg}")

            if errs:
                lines.append(f"  • {len(errs)} erreur(s) :")
                for e in errs[:5]:
                    wall = e.get("wall", "")
                    msg  = e.get("msg", "")
                    lines.append(f"      {wall}   {msg}")
                if len(errs) > 5:
                    lines.append(f"      … et {len(errs) - 5} autre(s)")

            # Pour FaceIndexThread : préciser si c'est le temps/photo ou la durée totale qui pose problème
            if th == "FaceIndexThread" and face_step_stats is not None:
                _nb, _avg_s, _max_s = face_step_stats
                lines.append(f"  • Progression par photo ({_nb} photo(s) traitée(s)) :")
                lines.append(
                    f"      Moy. {_avg_s / 1000:.0f} s/photo   "
                    f"Max {_max_s / 1000:.0f} s/photo   "
                    f"(seuil bloqué : {_FACE_STEP_SLOW_MS // 1000} s/photo)"
                )
                if _max_s >= _FACE_STEP_SLOW_MS:
                    lines.append("      → Temps/photo élevé : le thread progresse lentement ou est bloqué")
                else:
                    lines.append("      → Temps/photo normal : c'est la durée totale qui est longue (attendu pour ce thread)")

            hints = _THREAD_HINTS.get(th, [])
            if hints:
                lines.append("  Pistes d'amélioration :")
                for h in hints:
                    lines.append(f"    →  {h}")

            lines.append("")

    # ── section OK
    if ok_threads:
        lines.append(sep)
        lines.append("THREADS OK")
        lines.append(sep)
        for th in ok_threads:
            durs = durations[th]
            runs = len(durs)
            avg  = statistics.mean(durs) if durs else None
            mx   = max(durs)             if durs else None
            if th == "FaceIndexThread" and face_step_stats is not None:
                _nb, _avg_s, _max_s = face_step_stats
                stats = (
                    f"   {_nb} photo(s) traitée(s)"
                    f"   moy {_avg_s / 1000:.0f} s/photo"
                    f"   max {_max_s / 1000:.0f} s/photo"
                    f"   (durée totale longue mais progression normale)"
                )
            elif runs:
                stats = (
                    f"   {runs} run{'s' if runs > 1 else ''}"
                    f"   moy {avg:,.0f} ms"
                    f"   max {mx:,.0f} ms"
                )
            else:
                stats = ""
            lines.append(f"  ✓  {th}{stats}")
        lines.append("")

    if not problem_threads:
        lines.append(sep)
        lines.append("✓  Aucun problème détecté — tous les threads fonctionnent normalement.")
        lines.append(sep)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Dialogue rapport de problèmes
# ═══════════════════════════════════════════════════════════════════════════

class _ProblemsReportDialog(QDialog):
    def __init__(self, entries: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rapport détaillé de performance")
        self.setMinimumSize(840, 580)
        self._report_text = _generate_problems_report(entries)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 10, 12, 10)

        lbl = QLabel(
            "Ce rapport décrit en détail les problèmes de performance détectés. "
            "Copiez-le et transmettez-le pour obtenir des améliorations ciblées."
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        root.addWidget(lbl)

        self._edit = QTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setFont(QFont("Consolas", 9))
        self._edit.setPlainText(self._report_text)
        self._edit.setStyleSheet(
            "background: #141414; color: #ddd; border: 1px solid #333;"
        )
        root.addWidget(self._edit, stretch=1)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        btn_copy = btns.addButton("Copier tout", QDialogButtonBox.ActionRole)
        btn_copy.clicked.connect(self._copy)
        root.addWidget(btns)

    @Slot()
    def _copy(self) -> None:
        QApplication.clipboard().setText(self._report_text)
        QMessageBox.information(self, "Copié", "Rapport copié dans le presse-papiers.")


# ═══════════════════════════════════════════════════════════════════════════
# Dialogue principal
# ═══════════════════════════════════════════════════════════════════════════

class ThreadJournalDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Journal d'activité des threads")
        self.setMinimumSize(1000, 720)
        self._entries: list[dict] = []
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._load)
        self._setup_ui()
        self._load()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 10, 12, 10)

        # ── barre de contrôles
        ctrl = QHBoxLayout()

        self._lbl_count = QLabel()
        self._lbl_count.setStyleSheet("color: #888; font-size: 11px;")
        ctrl.addWidget(self._lbl_count)
        ctrl.addStretch()

        ctrl.addWidget(QLabel("Thread :"))
        self._cmb_thread = QComboBox()
        self._cmb_thread.setMinimumWidth(180)
        self._cmb_thread.currentIndexChanged.connect(self._apply_filter)
        ctrl.addWidget(self._cmb_thread)

        ctrl.addWidget(QLabel("Filtre :"))
        self._txt_filter = QLineEdit()
        self._txt_filter.setPlaceholderText("Texte dans le message…")
        self._txt_filter.setMinimumWidth(160)
        self._txt_filter.textChanged.connect(self._apply_filter)
        ctrl.addWidget(self._txt_filter)

        self._btn_refresh = QPushButton("↻ Rafraîchir")
        self._btn_refresh.clicked.connect(self._load)
        ctrl.addWidget(self._btn_refresh)

        self._btn_live = QPushButton("▶ Temps réel")
        self._btn_live.setCheckable(True)
        self._btn_live.toggled.connect(self._on_live_toggled)
        ctrl.addWidget(self._btn_live)

        self._btn_clear = QPushButton("🗑 Vider")
        self._btn_clear.clicked.connect(self._clear)
        ctrl.addWidget(self._btn_clear)

        root.addLayout(ctrl)

        # ── compte rendu visuel
        self._compte_rendu = _CompteRenduPanel()
        root.addWidget(self._compte_rendu)

        # ── splitter résumé / événements
        splitter = QSplitter(Qt.Vertical)

        grp_summary = QGroupBox("Résumé par thread")
        vbox_s = QVBoxLayout(grp_summary)
        vbox_s.setContentsMargins(4, 4, 4, 4)
        self._summary_table = _SummaryTable()
        vbox_s.addWidget(self._summary_table)
        splitter.addWidget(grp_summary)

        grp_events = QGroupBox("Événements bruts")
        vbox_e = QVBoxLayout(grp_events)
        vbox_e.setContentsMargins(4, 4, 4, 4)
        self._event_table = _EventTable()
        vbox_e.addWidget(self._event_table)
        splitter.addWidget(grp_events)

        splitter.setSizes([180, 400])
        root.addWidget(splitter, stretch=1)

        # ── boutons du bas
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)

        btn_report = btns.addButton("Rapport de problèmes…", QDialogButtonBox.ActionRole)
        btn_report.clicked.connect(self._open_problems_report)
        btn_report.setToolTip(
            "Ouvre un rapport détaillé des problèmes de performance, "
            "prêt à copier pour demander des améliorations."
        )

        btn_export = btns.addButton("Exporter CSV…", QDialogButtonBox.ActionRole)
        btn_export.clicked.connect(self._export_csv)

        root.addWidget(btns)

    # ── slots ────────────────────────────────────────────────────────────────

    @Slot()
    def _load(self) -> None:
        self._entries = journal.get_entries(limit=3000)
        threads = sorted({e.get("thread", "") for e in self._entries})

        current = self._cmb_thread.currentText()
        self._cmb_thread.blockSignals(True)
        self._cmb_thread.clear()
        self._cmb_thread.addItem("(tous)")
        for t in threads:
            self._cmb_thread.addItem(t)
        idx = self._cmb_thread.findText(current)
        self._cmb_thread.setCurrentIndex(max(0, idx))
        self._cmb_thread.blockSignals(False)

        self._lbl_count.setText(
            f"{len(self._entries)} entrée(s) — journal : {self._journal_size()}"
        )
        self._compte_rendu.populate(self._entries)
        self._summary_table.populate(self._entries)
        self._apply_filter()

    @Slot()
    def _apply_filter(self) -> None:
        th = self._cmb_thread.currentText()
        if th == "(tous)":
            th = ""
        self._event_table.populate(self._entries, th, self._txt_filter.text().strip())

    @Slot(bool)
    def _on_live_toggled(self, checked: bool) -> None:
        if checked:
            self._btn_live.setText("⏹ Arrêter")
            self._refresh_timer.start(1500)
        else:
            self._btn_live.setText("▶ Temps réel")
            self._refresh_timer.stop()

    @Slot()
    def _clear(self) -> None:
        if QMessageBox.question(
            self, "Vider le journal",
            "Supprimer toutes les entrées du journal ?",
            QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes:
            journal.clear()
            self._load()

    @Slot()
    def _open_problems_report(self) -> None:
        dlg = _ProblemsReportDialog(self._entries, self)
        dlg.exec()

    @Slot()
    def _export_csv(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Exporter le journal", "thread_journal.csv", "CSV (*.csv)"
        )
        if not path:
            return
        import csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["wall", "tid", "thread", "event", "msg", "elapsed_ms"]
            )
            writer.writeheader()
            for e in self._entries:
                writer.writerow({k: e.get(k, "") for k in writer.fieldnames})
        QMessageBox.information(self, "Export", f"Exporté : {path}")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _journal_size(self) -> str:
        try:
            from src.core.thread_journal import _JOURNAL_PATH
            sz = _JOURNAL_PATH.stat().st_size
            return (
                f"{sz / 1024:.0f} Ko" if sz < 1024 * 1024
                else f"{sz / (1024 * 1024):.1f} Mo"
            )
        except Exception:
            return "?"
