# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Détection des doublons perceptuels dans la bibliothèque de photos.

Deux niveaux de détection :
  Tier 1 — pHash (distance de Hamming) :
      Couvre les doublons exacts, redimensionnés et retouchés (couleur, luminosité).
      Rapide : O(N²) comparaisons de hashes 64-bit.

  Tier 2 — ORB + RANSAC (correspondance de points-clés) :
      Couvre les doublons recadrés (jusqu'à ~60 % de surface recadrée).
      S'exécute uniquement sur les photos non groupées par le Tier 1.
      Filtre préalable par ratio d'aire pour éviter les paires impossibles.
"""
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

_PROGRESS_INTERVAL = 0.5  # secondes — throttle des logs/signaux dans les boucles O(N²)

# ── Tier 1 ─────────────────────────────────────────────────────────────────────
_HASH_THRESHOLD  = 10    # distance de Hamming max (8 = exact/resize ; 10 couvre les éditions modérées)

# ── Tier 2 ─────────────────────────────────────────────────────────────────────
_ORB_MIN_INLIERS = 40    # inliers RANSAC minimum pour valider un appariement
_ORB_AREA_FACTOR = 6.0   # ratio d'aire max entre deux photos pour être candidates
_ORB_MAX_KP      = 300   # keypoints ORB max par image (vitesse vs rappel)
_ORB_RATIO_TEST  = 0.75  # seuil du ratio test de Lowe
_ORB_LOAD_SIZE   = 800   # dimension max (px) pour charger une image en Tier 2
_ORB_GOOD_MIN    = 15    # matches après ratio test requis avant de lancer RANSAC

_VIDEO_EXT = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.webm',
              '.m4v', '.3gp', '.flv', '.ts', '.mts', '.mpg', '.mpeg'}


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_gray(path: str, max_dim: int) -> "np.ndarray | None":
    """Charge en niveaux de gris, réduit si > max_dim. Retourne None en cas d'erreur.

    cv2.imread rejette les chemins non-ASCII sur Windows (cf. detector.py::
    _exif_corrected) : on passe directement par PIL dans ce cas pour éviter
    une tentative cv2 vouée à l'échec (warning console + double décodage)."""
    try:
        import numpy as np
        import cv2
        img = None
        try:
            path.encode("ascii")
        except UnicodeEncodeError:
            pass
        else:
            img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            from PIL import Image
            with Image.open(path) as pil:
                img = np.array(pil.convert("L"))
        if img is None:
            return None
        h, w = img.shape[:2]
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        return img
    except Exception:
        return None


def _merge(group_of: dict[str, int], path_a: str, path_b: str,
           next_group: list[int]) -> None:
    """Fusionne les groupes de path_a et path_b dans group_of (union-find naïf)."""
    ga = group_of.get(path_a)
    gb = group_of.get(path_b)
    if ga is None and gb is None:
        group_of[path_a] = next_group[0]
        group_of[path_b] = next_group[0]
        next_group[0] += 1
    elif ga is None:
        group_of[path_a] = gb
    elif gb is None:
        group_of[path_b] = ga
    elif ga != gb:
        old, new = max(ga, gb), min(ga, gb)
        for p in group_of:
            if group_of[p] == old:
                group_of[p] = new


# ── thread principal ────────────────────────────────────────────────────────────

class DuplicateDetectorThread(QThread):
    """Détecte les doublons en deux passes (pHash + ORB/RANSAC)."""

    progress  = Signal(int, int, str)  # (courant, total, message)
    # object (pas dict) : PySide6 mappe Signal(dict) sur QVariantMap, qui exige des
    # clés str côté C++ — avec des clés int (group_id), la conversion cross-thread
    # échoue silencieusement (Shiboken log une erreur en stderr, pas d'exception
    # Python) et le slot reçoit un dict vide, faisant croire à "aucun doublon".
    finished  = Signal(object)         # {group_id: [path, ...]}
    error     = Signal(str)
    cancelled = Signal()              # émis une fois le thread réellement arrêté

    def __init__(self, photo_paths: list, parent=None):
        super().__init__(parent)
        self._paths = photo_paths
        self._cancelled = False
        self._corrupted: set[str] = set()

    def cancel(self) -> None:
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        """Vérifie la demande d'annulation ; émet `cancelled` une seule fois
        au point d'arrêt effectif (les boucles O(N²) la testent à chaque
        itération, mais l'arrêt réel n'a lieu qu'une fois)."""
        if self._cancelled:
            self.cancelled.emit()
            return True
        return False

    @property
    def corrupted_paths(self) -> list[str]:
        """Chemins des fichiers dont le chargement a échoué pendant le scan
        (probablement corrompus). Stable une fois le signal `finished` émis."""
        return sorted(self._corrupted)

    def run(self) -> None:
        try:
            self._detect()
        except Exception as e:
            logger.exception("Erreur détection doublons")
            self.error.emit(str(e))

    # ── Tier 1 : pHash ─────────────────────────────────────────────────────────

    def _detect(self) -> None:
        try:
            import imagehash
            from PIL import Image
        except ImportError as e:
            self.error.emit(
                f"Module manquant : {e}\n"
                "Installez imagehash et Pillow (pip install imagehash Pillow)."
            )
            return

        paths = [
            p for p in self._paths
            if os.path.isfile(p) and Path(p).suffix.lower() not in _VIDEO_EXT
        ]
        total = len(paths)
        if total == 0:
            self.finished.emit({})
            return

        # Les deux phases comptent chacune pour la moitié de la barre de progression.
        grand_total = total * 2

        # Phase 1 : calcul des empreintes pHash + dimensions (utilisées par le Tier 2)
        hashes: list[tuple[str, object]] = []
        dims: dict[str, tuple[int, int]] = {}
        logger.info("Tier 1 : calcul des empreintes de %d photo(s)…", total)

        last_emit = time.monotonic()
        for i, path in enumerate(paths):
            if self._is_cancelled():
                return
            # Log systématique (pas throttlé) : si le traitement se bloque sur un
            # fichier précis (image corrompue, volume réseau lent…), cette ligne
            # reste la dernière du log — elle identifie le fichier en cause.
            logger.debug("Tier 1 empreinte %d/%d : %s", i + 1, total, path)

            now = time.monotonic()
            if now - last_emit >= _PROGRESS_INTERVAL:
                last_emit = now
                self.progress.emit(i, grand_total,
                                   f"Tier 1 — empreintes {i}/{total}…")
                logger.info("Tier 1 : %d/%d empreintes calculées", i, total)

            try:
                with Image.open(path) as img:
                    dims[path] = img.size
                    h = imagehash.phash(img)
                hashes.append((path, h))
            except Exception as exc:
                logger.warning("Fichier illisible (Tier 1) : %s (%s)", path, exc)
                self._corrupted.add(path)

        if self._is_cancelled():
            return

        # Phase 2 : groupement par distance de Hamming O(N²)
        n = len(hashes)
        group_of: dict[str, int] = {}
        next_group = [1]
        total_pairs = n * (n - 1) // 2
        logger.info("Tier 1 : comparaison de %d empreintes (%d paires à évaluer)…",
                    n, total_pairs)
        self.progress.emit(total, grand_total,
                           f"Tier 1 — comparaison des empreintes (0/{n})…")

        last_emit = time.monotonic()
        for i in range(n):
            if self._is_cancelled():
                return
            path_i, hash_i = hashes[i]
            for j in range(i + 1, n):
                path_j, hash_j = hashes[j]
                try:
                    dist = hash_i - hash_j
                except Exception:
                    continue
                if dist <= _HASH_THRESHOLD:
                    _merge(group_of, path_i, path_j, next_group)

            now = time.monotonic()
            if now - last_emit >= _PROGRESS_INTERVAL:
                last_emit = now
                n_groups = len({v for v in group_of.values()})
                self.progress.emit(
                    total + int((i + 1) * total / n), grand_total,
                    f"Tier 1 — comparaison des empreintes ({i + 1}/{n}, {n_groups} groupe(s))…",
                )
                logger.info("Tier 1 : %d/%d empreintes comparées (%d groupe(s) formés)",
                            i + 1, n, n_groups)

        # ── Tier 2 : ORB + RANSAC sur les photos non groupées ──────────────────
        unmatched = [p for p in paths if p not in group_of]
        logger.info("Tier 1 : %d groupe(s). Tier 2 sur %d photos non groupées.",
                    len({v for v in group_of.values()}), len(unmatched))

        self._detect_crops(unmatched, dims, group_of, next_group, total, grand_total)

        # Phase finale : renumérotation (1, 2, 3…)
        raw: dict[int, list[str]] = {}
        for path, gid in group_of.items():
            raw.setdefault(gid, []).append(path)

        groups: dict[int, list[str]] = {}
        new_id = 1
        for members in raw.values():
            if len(members) >= 2:
                groups[new_id] = members
                new_id += 1

        self.finished.emit(groups)

    # ── Tier 2 : ORB + RANSAC ──────────────────────────────────────────────────

    def _detect_crops(
        self,
        unmatched: list[str],
        dims: dict[str, tuple[int, int]],
        group_of: dict[str, int],
        next_group: list[int],
        phase1_total: int,
        grand_total: int,
    ) -> None:
        """Détecte les doublons recadrés par correspondance ORB + vérification RANSAC."""
        if len(unmatched) < 2:
            return

        try:
            import cv2
            import numpy as np
        except ImportError:
            logger.warning("Tier 2 ignoré : opencv-python ou numpy non disponible")
            return

        n = len(unmatched)
        self.progress.emit(phase1_total, grand_total,
                           f"Tier 2 — extraction ORB ({n} photos)…")

        orb = cv2.ORB_create(nfeatures=_ORB_MAX_KP)

        # Pré-calcul des descripteurs (réduit chaque image à _ORB_LOAD_SIZE)
        desc_list: list[tuple[str, object, object, int]] = []  # (path, kp, des, area)
        for i, path in enumerate(unmatched):
            if self._is_cancelled():
                return
            if i % 10 == 0:
                self.progress.emit(
                    phase1_total + i * (grand_total - phase1_total) // (n * 2),
                    grand_total,
                    f"Tier 2 — ORB descripteurs {i}/{n}…",
                )
            try:
                img = _load_gray(path, _ORB_LOAD_SIZE)
                if img is None:
                    logger.warning("Fichier illisible (Tier 2) : %s", path)
                    self._corrupted.add(path)
                    continue
                kp, des = orb.detectAndCompute(img, None)
                if des is None or len(kp) < 10:
                    continue
                if path in dims:
                    w, h = dims[path]
                else:
                    # Le Tier 1 n'a pas pu ouvrir ce fichier (Image.open a échoué) :
                    # retenter avec PIL pour obtenir la vraie résolution plutôt que
                    # de mélanger une aire réduite (_load_gray, max 800px) avec les
                    # aires réelles des autres photos — ça fausserait le préfiltre
                    # _ORB_AREA_FACTOR trié par aire (break prématuré possible).
                    try:
                        with Image.open(path) as pil_img:
                            w, h = pil_img.size
                    except Exception:
                        w, h = img.shape[1], img.shape[0]
                desc_list.append((path, kp, des, w * h))
            except Exception as exc:
                logger.debug("ORB descripteur échoué %s : %s", os.path.basename(path), exc)

        m = len(desc_list)
        if m < 2:
            return

        self.progress.emit(
            phase1_total + (grand_total - phase1_total) // 2,
            grand_total,
            f"Tier 2 — comparaison ORB ({m} photos)…",
        )

        # Tri par aire croissante : accélère le prefiltre (photos similaires proches)
        desc_list.sort(key=lambda x: x[3])

        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        pairs_checked = 0
        logger.info("Tier 2 : comparaison ORB de %d photos non groupées par le Tier 1.", m)

        comparison_start = phase1_total + (grand_total - phase1_total) // 2
        last_emit = time.monotonic()
        for i in range(m):
            if self._is_cancelled():
                return
            path_i, kp_i, des_i, area_i = desc_list[i]

            now = time.monotonic()
            if now - last_emit >= _PROGRESS_INTERVAL:
                last_emit = now
                current = comparison_start + int((i + 1) * (grand_total - comparison_start) / m)
                self.progress.emit(
                    current, grand_total,
                    f"Tier 2 — comparaison ORB ({i + 1}/{m}, {pairs_checked} paire(s) vérifiée(s))…",
                )
                logger.info("Tier 2 : %d/%d photos comparées (%d paire(s) vérifiée(s))",
                            i + 1, m, pairs_checked)

            for j in range(i + 1, m):
                path_j, kp_j, des_j, area_j = desc_list[j]

                # Prefiltre ratio d'aire : sortie anticipée (liste triée — area_j ≥ area_i)
                if area_i > 0 and area_j / area_i > _ORB_AREA_FACTOR:
                    break

                # Inutile de re-matcher deux photos déjà dans le même groupe
                gi, gj = group_of.get(path_i), group_of.get(path_j)
                if gi is not None and gi == gj:
                    continue

                pairs_checked += 1

                # Appariement BFMatcher + ratio test de Lowe
                try:
                    raw_matches = bf.knnMatch(des_i, des_j, k=2)
                except Exception:
                    continue

                good = [
                    m1 for pair in raw_matches if len(pair) == 2
                    for m1, m2 in (pair,)
                    if m1.distance < _ORB_RATIO_TEST * m2.distance
                ]
                if len(good) < _ORB_GOOD_MIN:
                    continue

                # Vérification géométrique par homographie RANSAC
                src_pts = np.float32(
                    [kp_i[m1.queryIdx].pt for m1 in good]
                ).reshape(-1, 1, 2)
                dst_pts = np.float32(
                    [kp_j[m1.trainIdx].pt for m1 in good]
                ).reshape(-1, 1, 2)

                _, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                if mask is None:
                    continue

                inliers = int(mask.sum())
                if inliers < _ORB_MIN_INLIERS:
                    continue

                logger.debug(
                    "Tier 2 crop détecté : %s ↔ %s (%d inliers)",
                    os.path.basename(path_i), os.path.basename(path_j), inliers,
                )
                _merge(group_of, path_i, path_j, next_group)

        logger.info("Tier 2 : %d paire(s) vérifiées par ORB/RANSAC.", pairs_checked)


# ── rapport HTML ────────────────────────────────────────────────────────────────

def generate_html_report(groups: dict, output_path: str) -> None:
    """Génère un rapport HTML listant tous les groupes de doublons."""
    n_groups = len(groups)
    n_files  = sum(len(v) for v in groups.values())
    now      = datetime.now().strftime("%d/%m/%Y à %H:%M")

    lines = [
        "<!DOCTYPE html>",
        "<html lang='fr'>",
        "<head>",
        "<meta charset='UTF-8'>",
        "<title>Doublons — PixelPhotoManager</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;background:#1a1a1a;color:#ccc;"
        "     margin:0;padding:24px}",
        "h1{color:#fff;font-size:1.3em;margin:0 0 6px}",
        ".summary{background:#2b2b2b;border-radius:8px;padding:12px 20px;"
        "         margin-bottom:24px;font-size:.9em}",
        ".group{background:#242424;border:1px solid #333;border-radius:8px;"
        "       padding:14px 18px;margin-bottom:18px}",
        ".gtitle{color:#ffa040;font-weight:bold;margin-bottom:10px}",
        ".file{display:grid;grid-template-columns:1fr auto;gap:8px;"
        "      padding:6px 0;border-bottom:1px solid #2f2f2f}",
        ".file:last-child{border-bottom:none}",
        ".fname{color:#e0e0e0;font-size:.92em}",
        ".fpath{color:#666;font-size:.80em;word-break:break-all}",
        ".fsize{color:#888;font-size:.85em;white-space:nowrap;align-self:center}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>Rapport de doublons — PixelPhotoManager</h1>",
        f"<div class='summary'>Généré le {now} &nbsp;·&nbsp; "
        f"<b>{n_groups}</b> groupe{'s' if n_groups != 1 else ''} de doublons"
        f" &nbsp;·&nbsp; <b>{n_files}</b> fichiers concernés</div>",
    ]

    for gid, members in groups.items():
        lines.append("<div class='group'>")
        lines.append(
            f"<div class='gtitle'>Groupe&nbsp;#{gid}"
            f" — {len(members)} fichier{'s' if len(members) != 1 else ''}</div>"
        )
        for path in members:
            fname = os.path.basename(path)
            fdir  = os.path.dirname(path)
            try:
                fsize = _fmt_size(os.path.getsize(path))
            except OSError:
                fsize = "—"
            lines.append(
                f"<div class='file'>"
                f"<div><div class='fname'>{fname}</div>"
                f"<div class='fpath'>{fdir}</div></div>"
                f"<div class='fsize'>{fsize}</div>"
                f"</div>"
            )
        lines.append("</div>")

    lines.append("</body></html>")
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")


def _fmt_size(n: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n}&nbsp;{unit}"
        n //= 1024
    return f"{n}&nbsp;To"
