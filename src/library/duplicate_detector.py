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
import bisect
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.core.cpu_throttle import (
    limit_cv2_threads,
    lower_current_thread_priority,
    throttle_tick,
    throttled_worker_count,
)
from src.library.dedup_cache import DedupCache
from src.library.image_loader import RAW_EXT

logger = logging.getLogger(__name__)

_PROGRESS_INTERVAL = 0.5  # secondes — throttle des logs/signaux dans les boucles O(N²)
_LIVE_SNAPSHOT_INTERVAL = 2.0  # secondes — throttle des instantanés partial_results
# (renumérotation + écriture catalogue + rafraîchissement UI), plus coûteux qu'un
# simple signal de progression, donc cadencé plus lentement que _PROGRESS_INTERVAL.

# ── Tier 1 ─────────────────────────────────────────────────────────────────────
_HASH_THRESHOLD  = 10    # distance de Hamming max (8 = exact/resize ; 10 couvre les éditions modérées)
_HASH_MICRO_SIZE     = 8    # miniature (px) pour la vérification post-hash
_HASH_PIXEL_MAX_DIFF = 0.34  # écart moyen (miniature 8x8 normalisée) au-delà
# duquel une paire pHash-positive est rejetée. Calibré empiriquement : pire
# retouche légitime plausible (rotation 5°, sous le seuil pHash) ~0.31 ;
# deux faux positifs réels observés (photos sans rapport, hash coïncidant
# tout juste au seuil, silhouette clair/sombre similaire — ex. ciel+côte vs
# ciel+falaise) ~0.375 et ~0.88. Le premier faux positif (0.375) est proche
# du cas légitime (0.31) : marge résiduelle étroite (~0.03 de chaque côté).
# Si un nouveau faux négatif apparaît (vraie retouche non détectée), il faut
# remonter ce seuil et se reposer sur le bouton ✕ (Catalog.ignore_duplicate_group,
# persistant) pour les faux positifs résiduels plutôt que sur ce seul seuil.

# ── Tier 2 ─────────────────────────────────────────────────────────────────────
_ORB_MIN_INLIERS = 40    # inliers RANSAC minimum pour valider un appariement
_ORB_AREA_FACTOR = 6.0   # ratio d'aire max entre deux photos pour être candidates
_ORB_MAX_KP      = 300   # keypoints ORB max par image (vitesse vs rappel)
_ORB_RATIO_TEST  = 0.75  # seuil du ratio test de Lowe
_ORB_LOAD_SIZE   = 800   # dimension max (px) pour charger une image en Tier 2
_ORB_GOOD_MIN    = 15    # matches après ratio test requis avant de lancer RANSAC
_ORB_MAX_MEAN_DIFF = 25.0  # écart de pixels (0-255) après recalage par
# homographie, sur la zone de recouvrement. Calibré empiriquement : un
# vrai recadrage (paire synthétique crop_duplicate_pair) donne ~14 ;
# deux faux positifs réels observés (rafale, arrière-plan statique très
# texturé mais sujet différent) donnaient 38 et 42 — c'est le seul des
# signaux testés (nombre d'inliers, ratio inliers/good) qui sépare
# nettement les deux cas.

_GRAY_CACHE_SIZE = 32  # images de travail Tier 2 gardées décodées (cf. _GrayImageCache)

_VIDEO_EXT = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.webm',
              '.m4v', '.3gp', '.flv', '.ts', '.mts', '.mpg', '.mpeg', '.vob'}


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_gray(path: str, max_dim: int) -> "np.ndarray | None":
    """Charge en niveaux de gris, réduit si > max_dim. Retourne None en cas d'erreur.

    cv2.imread rejette les chemins non-ASCII sur Windows (cf. detector.py::
    _exif_corrected) : on passe directement par PIL dans ce cas pour éviter
    une tentative cv2 vouée à l'échec (warning console + double décodage).

    Le TIFF est exclu de cv2.imread quel que soit le chemin : certains TIFF
    avec des tags de métadonnées exotiques (ex. tag 50341/0xc4a5, observé en
    usage réel) déclenchent un bug connu du décodeur libtiff d'OpenCV
    (assertion interne "original_ptr == real_mat.data" dans loadsave.cpp) qui
    peut aboutir à un abort() du process plutôt qu'à une cv2.error Python
    normalement rattrapable — un try/except ne protège pas contre ce cas.
    PIL décode ces mêmes fichiers sans problème (déjà utilisé sans incident
    par le Tier 1, qui ne passe jamais par cv2)."""
    try:
        import numpy as np
        import cv2
        img = None
        is_tiff = Path(path).suffix.lower() in (".tif", ".tiff")
        if not is_tiff:
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


class _GrayImageCache:
    """LRU thread-safe des images de travail du Tier 2 (niveaux de gris,
    réduites à `_ORB_LOAD_SIZE`), chargées **à la demande**.

    Ces images ne servent qu'à l'ultime vérification d'une paire (recalage par
    homographie puis comparaison pixel, cf. `_ORB_MAX_MEAN_DIFF`), atteinte
    seulement par les rares paires qui ont déjà passé le ratio test de Lowe et
    le seuil d'inliers RANSAC. Les garder toutes décodées dans `desc_list`
    coûtait, sur une bibliothèque de 65 000 photos, un décodage JPEG par photo
    à chaque démarrage (~80 s de CPU) et une empreinte mémoire de plusieurs
    dizaines de Go — pour un tableau dont on n'utilisait qu'une poignée
    d'entrées. Rechargées depuis le fichier d'origine via `_load_gray()`, qui
    est exactement la fonction ayant servi à calculer les keypoints : mêmes
    dimensions, donc homographie et masque de recouvrement restent valides."""

    def __init__(self, capacity: int = _GRAY_CACHE_SIZE) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, object]" = OrderedDict()

    def get(self, path: str):
        """Image en niveaux de gris, ou None si le fichier est illisible.
        Deux threads peuvent décoder le même chemin simultanément (le verrou
        n'est pas tenu pendant le décodage, qui est long) : sans conséquence,
        le résultat est identique et le second écrase le premier."""
        with self._lock:
            img = self._items.get(path)
            if img is not None:
                self._items.move_to_end(path)
                return img
        img = _load_gray(path, _ORB_LOAD_SIZE)
        if img is None:
            return None
        with self._lock:
            self._items[path] = img
            self._items.move_to_end(path)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return img


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


def _dates_differ(dates: dict, path_a: str, path_b: str) -> bool:
    """True seulement si les deux photos ont une date EXIF connue et
    différente — une rafale (même seconde EXIF, sous-secondes différentes)
    doit être exclue des doublons même si le contenu visuel est
    quasi-identique, sur demande explicite de l'utilisateur. Une date
    manquante d'un côté ou des deux ne bloque jamais la fusion (repli sur le
    seul signal visuel, comme avant l'ajout de cette vérification)."""
    dt_a = dates.get(path_a)
    dt_b = dates.get(path_b)
    if dt_a is None or dt_b is None:
        return False
    return dt_a != dt_b


def _renumber(group_of: dict[str, int]) -> dict[int, list[str]]:
    """Renumérote les group_id bruts (1, 2, 3…) et exclut les singletons —
    utilisable aussi bien sur un `group_of` final que sur un instantané
    provisoire pris en cours de scan (les groupes ne font que croître)."""
    raw: dict[int, list[str]] = {}
    for path, gid in group_of.items():
        raw.setdefault(gid, []).append(path)

    groups: dict[int, list[str]] = {}
    new_id = 1
    for members in raw.values():
        if len(members) >= 2:
            groups[new_id] = members
            new_id += 1
    return groups


# ── thread principal ────────────────────────────────────────────────────────────

class DuplicateDetectorThread(QThread):
    """Détecte les doublons en deux passes (pHash + ORB/RANSAC)."""

    progress  = Signal(int, int, str)  # (courant, total, message)
    # object (pas dict) : PySide6 mappe Signal(dict) sur QVariantMap, qui exige des
    # clés str côté C++ — avec des clés int (group_id), la conversion cross-thread
    # échoue silencieusement (Shiboken log une erreur en stderr, pas d'exception
    # Python) et le slot reçoit un dict vide, faisant croire à "aucun doublon".
    finished  = Signal(object)         # {group_id: [path, ...]}
    # Instantané provisoire pendant le scan, même contrainte de clés que ci-dessus.
    partial_results = Signal(object, object)  # ({group_id: [path,...]}, [chemin_corrompu, ...])
    error     = Signal(str)
    cancelled = Signal()              # émis une fois le thread réellement arrêté

    def __init__(self, photo_paths: list, seed_groups: dict[str, int] | None = None,
                 cache_db_path: str | None = None,
                 full_catalog_scan: bool = True, parent=None,
                 dates: dict | None = None):
        """seed_groups : {path: group_id} connu au moment du déclenchement
        (typiquement Catalog.get_duplicate_group_assignments()) — amorce
        group_of pour que la comparaison reste vraiment incrémentale (cf.
        compared_tier1/compared_tier2 dans dedup_cache.py). Attention :
        omettre seed_groups lors d'un 2e run sur un même cache_db_path déjà
        peuplé par un run précédent ne redéclenche PAS une comparaison
        complète — toutes les paires apparaîtront comme « déjà comparées »
        et aucun groupe ne sera (re)formé. Toujours passer le seed_groups
        courant, y compris pour un nouveau scan complet volontaire (auquel
        cas passer {} explicitement n'aide pas non plus : purger
        compared_tier1/compared_tier2 via un nouveau cache_db_path ou une
        purge de cache serait nécessaire).

        dates : {path: datetime|None} — date de prise de vue (EXIF, précision
        sous-seconde si disponible ; typiquement
        Catalog.get_photo_dates_for_dedup()). Deux photos dont les dates sont
        toutes deux connues et différentes ne sont jamais fusionnées en
        doublons, même si les signaux visuels (pHash, ORB) concordent — cf.
        _dates_differ(). Une date manquante ne bloque rien (repli sur le seul
        signal visuel)."""
        super().__init__(parent)
        self._paths = photo_paths
        self._seed_groups = dict(seed_groups) if seed_groups else {}
        self._cache_db_path = cache_db_path
        self._full_catalog_scan = full_catalog_scan
        self._cancelled = False
        self._corrupted: set[str] = set()
        self._dates = dates or {}

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
        self.setPriority(QThread.LowestPriority)
        # setPriority() ci-dessus ne descend qu'à THREAD_PRIORITY_LOWEST (-2) :
        # insuffisant pour la boucle O(N²) du Tier 1, qui est la partie
        # mono-thread la plus lourde de toute la détection et tourne
        # précisément sur ce thread-ci (les ThreadPoolExecutor, eux, passent
        # déjà lower_current_thread_priority en initializer).
        lower_current_thread_priority()
        # Réglage global au process (cf. docstring) : sans lui, chacun de nos
        # workers « throttlés » peut à lui seul occuper les 16 cœurs via le
        # pool interne d'OpenCV.
        limit_cv2_threads(1)
        try:
            self._detect()
        except Exception as e:
            logger.exception("Erreur détection doublons")
            self.error.emit(str(e))

    # ── Tier 1 : pHash ─────────────────────────────────────────────────────────

    def _detect(self) -> None:
        try:
            import imagehash
            import numpy as np
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
            and Path(p).suffix.lower() not in RAW_EXT
        ]
        total = len(paths)
        if total == 0:
            self.finished.emit({})
            return

        # Les deux phases comptent chacune pour la moitié de la barre de progression.
        grand_total = total * 2

        cache = DedupCache(self._cache_db_path) if self._cache_db_path else DedupCache()
        cache.open()
        try:
            if self._full_catalog_scan:
                removed = cache.purge_missing(set(paths))
                if removed:
                    logger.info("dedup_cache : %d entrée(s) obsolète(s) purgée(s).", removed)

            # Phase 1 : calcul des empreintes pHash + dimensions (utilisées par le Tier 2)
            hashes: list[tuple[str, object]] = []
            dims: dict[str, tuple[int, int]] = {}
            micro: dict[str, "np.ndarray"] = {}
            mtimes: dict[str, float] = {}
            n_workers = throttled_worker_count()

            # Réutilisation du cache : une photo dont le mtime n'a pas changé
            # depuis le dernier scan n'a pas besoin d'être rouverte/rehashée.
            cached_fp = cache.get_fingerprints(paths)
            to_compute: list[str] = []
            for path in paths:
                row = cached_fp.get(path)
                if row is not None:
                    mtime_cached, phash_hex, w, h, micro_blob = row
                    try:
                        current_mtime = os.path.getmtime(path)
                    except OSError:
                        current_mtime = None
                    if current_mtime is not None and abs(mtime_cached - current_mtime) < 1.0:
                        dims[path] = (w, h)
                        micro[path] = np.frombuffer(
                            micro_blob, dtype=np.float64
                        ).reshape(_HASH_MICRO_SIZE, _HASH_MICRO_SIZE)
                        mtimes[path] = current_mtime
                        hashes.append((path, imagehash.hex_to_hash(phash_hex)))
                        continue
                to_compute.append(path)

            cache_hits = total - len(to_compute)
            logger.info(
                "Tier 1 : %d/%d empreintes réutilisées du cache, %d à calculer sur %d cœur(s)…",
                cache_hits, total, len(to_compute), n_workers,
            )

            def _compute_fingerprint(path: str):
                # Fonction pure (aucun état partagé écrit) : chaque appel décode son
                # propre fichier et retourne son résultat, fusionné ensuite sur ce
                # thread au fur et à mesure des complétions — décodage JPEG/PNG (PIL)
                # et calcul du DCT (imagehash, numpy) libèrent tous deux le GIL le
                # temps du calcul C, donc un pool de threads exploite réellement
                # plusieurs cœurs ici (même raisonnement que pour ORB/RANSAC au Tier 2
                # plus bas dans ce fichier).
                try:
                    with Image.open(path) as img:
                        d = img.size
                        h = imagehash.phash(img)
                        arr = np.asarray(
                            img.convert("L").resize(
                                (_HASH_MICRO_SIZE, _HASH_MICRO_SIZE), Image.LANCZOS
                            ),
                            dtype=np.float64,
                        )
                    # Lu après l'ouverture réussie (pas avant) pour que le mtime
                    # persisté corresponde au contenu réellement empreinté.
                    mtime = os.path.getmtime(path)
                    arr -= arr.mean()
                    std = arr.std()
                    if std > 1e-6:
                        arr /= std
                    result = (path, h, d, arr, mtime, None)
                except Exception as exc:
                    result = (path, None, None, None, None, exc)
                # Cycle de service pris ici, dans le worker qui vient de
                # consommer le CPU, et non côté consommateur (`as_completed`)
                # : toutes les futures sont soumises d'avance, ralentir la
                # boucle de collecte ne ralentirait donc pas le pool d'un iota.
                throttle_tick(lambda: self._cancelled)
                return result

            done = cache_hits
            if cache_hits:
                # Signal immédiat : évite que la barre semble figée pendant
                # qu'on saute la majorité d'une grosse bibliothèque déjà en cache.
                self.progress.emit(done, grand_total,
                                   f"Tier 1 — empreintes {done}/{total} (cache)…")

            last_emit = time.monotonic()
            last_persist = last_emit
            pending_fp: list[tuple] = []
            try:
                executor = ThreadPoolExecutor(
                    max_workers=n_workers, initializer=lower_current_thread_priority
                )
                cancelled_mid_flight = False
                try:
                    futures = {}
                    for path in to_compute:
                        # Log systématique (pas throttlé) à la soumission : si le
                        # traitement se bloque sur un fichier précis (image corrompue,
                        # volume réseau lent…), cette ligne reste la dernière du log —
                        # elle identifie le fichier en cause même en exécution parallèle.
                        logger.debug("Tier 1 empreinte (soumission) : %s", path)
                        futures[executor.submit(_compute_fingerprint, path)] = path

                    for future in as_completed(futures):
                        if self._is_cancelled():
                            cancelled_mid_flight = True
                            break

                        path = futures[future]
                        try:
                            path, h, d, arr, mtime, exc = future.result()
                        except Exception as e:
                            # _compute_fingerprint capture déjà toute exception en interne
                            # (retournée dans le tuple plutôt que levée) : ce garde-fou ne
                            # devrait normalement jamais se déclencher, mais une panne
                            # inattendue d'un worker ne doit pas faire échouer tout le scan.
                            logger.warning("Worker Tier 1 en échec pour %s : %s", path, e)
                            h = d = arr = mtime = None
                            exc = e
                        done += 1
                        if exc is not None:
                            # Un fichier supprimé (par l'utilisateur, pendant que ce
                            # scan tourne) échoue à l'ouverture exactement comme un
                            # fichier corrompu (FileNotFoundError capturée dans le
                            # même except Exception ci-dessus) — vérifier qu'il existe
                            # encore avant de le classer corrompu, sinon il resterait
                            # signalé (et proposé à la suppression/réparation) pour un
                            # fichier qui n'existe déjà plus.
                            if os.path.exists(path):
                                logger.warning("Fichier illisible (Tier 1) : %s (%s)", path, exc)
                                self._corrupted.add(path)
                            else:
                                logger.debug(
                                    "Fichier disparu avant le calcul d'empreinte (Tier 1) : %s",
                                    path,
                                )
                        else:
                            dims[path] = d
                            micro[path] = arr
                            mtimes[path] = mtime
                            hashes.append((path, h))
                            pending_fp.append((path, mtime, str(h), d[0], d[1], arr.tobytes()))

                        now = time.monotonic()
                        if now - last_emit >= _PROGRESS_INTERVAL:
                            last_emit = now
                            self.progress.emit(done, grand_total,
                                               f"Tier 1 — empreintes {done}/{total}…")
                            logger.info("Tier 1 : %d/%d empreintes calculées", done, total)
                        if pending_fp and now - last_persist >= _PROGRESS_INTERVAL:
                            last_persist = now
                            cache.store_fingerprints(pending_fp)
                            pending_fp = []
                finally:
                    # Un thread Python ne peut pas être tué proprement de l'extérieur
                    # (contrairement à un ProcessPoolExecutor, cf. FaceIndexThread), mais
                    # shutdown(wait=False, cancel_futures=True) évite au moins d'attendre
                    # ici la fin des ~n_workers tâches déjà en cours (chacune : un seul
                    # décodage d'image, borné) — sans ça, le `with ThreadPoolExecutor`
                    # implicite bloquait sur shutdown(wait=True) jusqu'à la fin de TOUT
                    # le lot restant à chaque annulation, un des principaux contributeurs
                    # à la fermeture lente de l'application quand une détection de
                    # doublons est en cours (cf. MainWindow.closeEvent).
                    executor.shutdown(
                        wait=not cancelled_mid_flight, cancel_futures=cancelled_mid_flight
                    )
                if cancelled_mid_flight:
                    return
            finally:
                # Persiste tout ce qui a été calculé jusqu'ici, y compris en cas
                # d'annulation (return ci-dessus) — c'est ce qui rend le scan reprenable.
                if pending_fp:
                    cache.store_fingerprints(pending_fp)

            if self._is_cancelled():
                return

            # Phase 2 : groupement incrémental par distance de Hamming — amorcé
            # avec les groupes déjà connus (self._seed_groups), puis seules les
            # paires impliquant au moins un fichier « nouveau » (jamais comparé
            # lors d'une passe complète antérieure, ou modifié depuis) sont
            # évaluées. Les paires ancien×ancien ne sont même pas itérées : déjà
            # vérifiées lors d'une passe précédente et ne peuvent pas changer
            # tant qu'aucun des deux fichiers n'est modifié.
            paths_set = set(paths)
            group_of: dict[str, int] = {
                p: gid for p, gid in self._seed_groups.items() if p in paths_set
            }
            next_group = [max(group_of.values(), default=0) + 1]

            compared_tier1 = cache.get_compared_tier1([p for p, _ in hashes])
            new_list: list[tuple[str, object]] = []
            old_list: list[tuple[str, object]] = []
            for path, h in hashes:
                cached_mtime = compared_tier1.get(path)
                if cached_mtime is not None and abs(cached_mtime - mtimes[path]) < 1.0:
                    old_list.append((path, h))
                else:
                    new_list.append((path, h))

            n = len(new_list)
            total_pairs = n * (n - 1) // 2 + n * len(old_list)
            logger.info(
                "Tier 1 : comparaison de %d empreinte(s) nouvelle(s)/modifiée(s) "
                "contre %d ancienne(s) (%d paire(s) à évaluer)…",
                n, len(old_list), total_pairs,
            )
            self.progress.emit(total, grand_total,
                               f"Tier 1 — comparaison des empreintes (0/{n})…")

            def _compare_pair(path_i, hash_i, path_j, hash_j) -> None:
                # Discriminant le plus simple et le moins cher évalué en premier
                # (lookup dict + comparaison, pas d'accès image) : deux photos de
                # dates EXIF connues et différentes ne sont jamais des doublons,
                # inutile de calculer quoi que ce soit d'autre pour la paire.
                if _dates_differ(self._dates, path_i, path_j):
                    return
                try:
                    dist = hash_i - hash_j
                except Exception:
                    return
                if dist <= _HASH_THRESHOLD:
                    # Un pHash peut coïncider par hasard entre deux photos sans
                    # rapport (répartition de luminosité globale similaire),
                    # surtout quand la distance tombe tout juste au seuil —
                    # vérification de secours sur une miniature 8x8 normalisée
                    # (cf. _HASH_PIXEL_MAX_DIFF).
                    arr_i = micro.get(path_i)
                    arr_j = micro.get(path_j)
                    if arr_i is not None and arr_j is not None:
                        pixel_diff = float(np.abs(arr_i - arr_j).mean())
                        if pixel_diff > _HASH_PIXEL_MAX_DIFF:
                            return
                    _merge(group_of, path_i, path_j, next_group)

            # Persistance incrémentale de la complétude des comparaisons : les
            # lignes 0..i étant traitées dans l'ordre, une fois la ligne i
            # terminée, `new_list[0..i]` a été comparé à *tout* le reste (les
            # lignes précédentes ont chacune couvert leurs paires avec i).
            # Sans ce jalonnement, une passe interrompue (fermeture de
            # l'application) ne persistait rien du tout et repartait de zéro au
            # démarrage suivant — soit, sur une grosse bibliothèque, une heure
            # de CPU rejouée à chaque session, indéfiniment.
            #
            # Le jalon reste volontairement en retard d'un instantané sur la
            # progression réelle : les groupes formés ne sont persistés dans le
            # catalogue que via `partial_results`, traité en asynchrone sur le
            # thread UI. Marquer une ligne « comparée » avant que ses fusions
            # n'aient été diffusées risquerait de perdre définitivement un
            # groupe (la paire ne serait plus jamais réévaluée). En ne jalonnant
            # que jusqu'à l'indice du snapshot *précédent*, on laisse au moins
            # un intervalle complet (_LIVE_SNAPSHOT_INTERVAL) à l'UI pour
            # l'avoir traité.
            stored_upto = -1     # dernier indice de new_list persisté
            snapshot_idx = -1    # indice atteint lors du dernier partial_results

            def _checkpoint_tier1(upto: int) -> None:
                nonlocal stored_upto
                if upto <= stored_upto:
                    return
                cache.store_compared_tier1(
                    [(p, mtimes[p]) for p, _ in new_list[stored_upto + 1:upto + 1]]
                )
                stored_upto = upto

            last_emit = time.monotonic()
            last_snapshot = last_emit
            for i in range(n):
                if self._is_cancelled():
                    return
                path_i, hash_i = new_list[i]
                for j in range(i + 1, n):
                    path_j, hash_j = new_list[j]
                    _compare_pair(path_i, hash_i, path_j, hash_j)
                for path_j, hash_j in old_list:
                    _compare_pair(path_i, hash_i, path_j, hash_j)
                throttle_tick(lambda: self._cancelled)

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
                if now - last_snapshot >= _LIVE_SNAPSHOT_INTERVAL:
                    last_snapshot = now
                    _checkpoint_tier1(snapshot_idx)
                    snapshot_idx = i
                    self.partial_results.emit(_renumber(group_of), sorted(self._corrupted))

            _checkpoint_tier1(n - 1)

            # ── Tier 2 : ORB + RANSAC sur les photos non groupées ──────────────────
            unmatched = [p for p in paths if p not in group_of]
            logger.info("Tier 1 : %d groupe(s). Tier 2 sur %d photos non groupées.",
                        len({v for v in group_of.values()}), len(unmatched))

            # Émission inconditionnelle : rend visibles les résultats du Tier 1
            # (souvent déjà la majorité des vrais doublons) avant que la phase
            # ORB, plus lente, ne démarre.
            self.partial_results.emit(_renumber(group_of), sorted(self._corrupted))

            self._detect_crops(unmatched, dims, group_of, next_group, total, grand_total, cache)

            # Phase finale : renumérotation (1, 2, 3…)
            self.finished.emit(_renumber(group_of))
        finally:
            # self._corrupted est, à ce stade (quelle que soit la sortie —
            # normale ou annulation), l'état complet et à jour : tout fichier
            # corrompu est systématiquement retenté à chaque passage (jamais
            # mis en cache dans fingerprints/orb_features), donc un fichier
            # réparé ou supprimé depuis le dernier passage n'y figure plus.
            # Persisté ici (et pas seulement gardé en mémoire côté UI) pour
            # survivre à un redémarrage de l'application.
            cache.replace_corrupted_paths(self._corrupted)
            cache.close()

    # ── Tier 2 : ORB + RANSAC ──────────────────────────────────────────────────

    def _detect_crops(
        self,
        unmatched: list[str],
        dims: dict[str, tuple[int, int]],
        group_of: dict[str, int],
        next_group: list[int],
        phase1_total: int,
        grand_total: int,
        cache: "DedupCache",
    ) -> None:
        """Détecte les doublons recadrés par correspondance ORB + vérification RANSAC."""
        if len(unmatched) < 2:
            return

        try:
            import cv2
            import numpy as np
            from PIL import Image
        except ImportError:
            logger.warning("Tier 2 ignoré : opencv-python ou numpy non disponible")
            return

        n = len(unmatched)
        self.progress.emit(phase1_total, grand_total,
                           f"Tier 2 — extraction ORB ({n} photos)…")

        orb = cv2.ORB_create(nfeatures=_ORB_MAX_KP)
        gray_cache = _GrayImageCache()

        # ── Sélection paresseuse : déterminer quoi charger, avant de charger ───
        # Une paire n'est évaluée que si au moins un de ses deux membres est
        # nouveau ou modifié depuis la dernière passe complète (les paires
        # ancien×ancien sont sautées plus bas) et que le préfiltre par ratio
        # d'aire ne l'écarte pas d'emblée. Ces deux critères ne dépendent que
        # de métadonnées (mtime, dimensions) : les évaluer *avant* de toucher
        # aux features évite de reconstruire toute la bibliothèque à chaque
        # démarrage pour finalement ne comparer aucune paire — sur 65 000
        # photos, plusieurs Go lus en base, un décodage JPEG et ~300 objets
        # cv2.KeyPoint par photo, systématiquement, dans le cas nominal où
        # rien n'a changé.
        orb_meta = cache.get_orb_meta(unmatched)
        compared_tier2 = cache.get_compared_tier2(unmatched)

        mtimes2: dict[str, float] = {}
        cached_paths: list[str] = []
        to_compute: list[str] = []
        old_paths_set: set[str] = set()
        for path in unmatched:
            try:
                current_mtime = os.path.getmtime(path)
            except OSError:
                continue  # disparu depuis le Tier 1
            mtimes2[path] = current_mtime
            meta = orb_meta.get(path)
            if meta is not None and abs(meta[0] - current_mtime) < 1.0:
                cached_paths.append(path)
            else:
                to_compute.append(path)
            cached_cmp = compared_tier2.get(path)
            if cached_cmp is not None and abs(cached_cmp - current_mtime) < 1.0:
                old_paths_set.add(path)

        new_paths_set = {p for p in mtimes2 if p not in old_paths_set}
        if not new_paths_set:
            logger.info(
                "Tier 2 : aucune photo nouvelle ou modifiée parmi les %d non groupées, "
                "aucune paire à évaluer.", n,
            )
            return

        # Aire de chaque photo, connue sans rien charger : dimensions réelles
        # relevées par le Tier 1, ou celles mémorisées avec les features.
        def _area_of(path: str) -> int:
            d = dims.get(path)
            if d is not None:
                return d[0] * d[1]
            meta = orb_meta.get(path)
            return meta[1] * meta[2] if meta is not None else 0

        areas = {path: _area_of(path) for path in mtimes2}

        # Une photo ancienne n'est utile que si son aire tombe dans la fenêtre
        # [a/F, a·F] d'au moins une photo nouvelle — sinon le préfiltre par
        # ratio d'aire de la boucle de comparaison écarterait de toute façon
        # chacune de ses paires. Aire inconnue (0) : la boucle désactive alors
        # le préfiltre, donc tout devient candidat, on ne peut rien exclure.
        new_areas = sorted(areas[p] for p in new_paths_set)
        keep_everything = new_areas and new_areas[0] <= 0

        def _is_needed(path: str) -> bool:
            if keep_everything or path in new_paths_set:
                return True
            a = areas[path]
            if a <= 0:
                return True
            lo = bisect.bisect_left(new_areas, a / _ORB_AREA_FACTOR)
            hi = bisect.bisect_right(new_areas, a * _ORB_AREA_FACTOR)
            return lo < hi

        cached_paths = [p for p in cached_paths if _is_needed(p)]
        to_compute = [p for p in to_compute if _is_needed(p)]

        # ── Chargement des features des seules photos retenues ────────────────
        desc_list: list[tuple[str, object, object, int]] = []  # (path, pts, des, area)
        cached_orb = cache.get_orb_descriptors(cached_paths)
        for path in cached_paths:
            row = cached_orb.get(path)
            if row is not None:
                _mtime, w, h, kp_blob, des_blob = row
                try:
                    des = np.frombuffer(des_blob, dtype=np.uint8).reshape(-1, 32)
                    pts = np.frombuffer(kp_blob, dtype=np.float32).reshape(-1, 2)
                except Exception as exc:
                    logger.debug("dedup_cache : ligne ORB corrompue pour %s (%s), recalcul.",
                                 path, exc)
                else:
                    if des.shape[0] > 0 and len(pts) == des.shape[0]:
                        desc_list.append((path, pts, des, w * h))
                        continue
            to_compute.append(path)

        cache_hits = len(desc_list)
        total_needed = cache_hits + len(to_compute)
        logger.info(
            "Tier 2 : %d/%d photos concernées par au moins une paire à évaluer "
            "(%d features réutilisées du cache, %d à calculer)…",
            total_needed, n, cache_hits, len(to_compute),
        )

        pending_orb: list[tuple] = []
        try:
            for i, path in enumerate(to_compute):
                if self._is_cancelled():
                    return
                idx = cache_hits + i
                if idx % 10 == 0:
                    self.progress.emit(
                        phase1_total
                        + idx * (grand_total - phase1_total) // (max(1, total_needed) * 2),
                        grand_total,
                        f"Tier 2 — ORB descripteurs {idx}/{total_needed}…",
                    )
                    if pending_orb:
                        cache.store_orb_features(pending_orb)
                        pending_orb = []
                try:
                    img = _load_gray(path, _ORB_LOAD_SIZE)
                    if img is None:
                        # Cf. commentaire équivalent au Tier 1 : un fichier supprimé
                        # entre-temps échoue à l'ouverture comme un fichier corrompu.
                        if os.path.exists(path):
                            logger.warning("Fichier illisible (Tier 2) : %s", path)
                            self._corrupted.add(path)
                        else:
                            logger.debug(
                                "Fichier disparu avant le calcul ORB (Tier 2) : %s", path
                            )
                        continue
                    mtime = os.path.getmtime(path)
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
                    mtimes2[path] = mtime
                    # Seules les coordonnées des keypoints sont conservées : le
                    # reste de l'algorithme ne lit que `.pt` (cf. _compare_chunk),
                    # et un tableau (N,2) float32 remplace avantageusement ~300
                    # objets cv2.KeyPoint par photo — que le cache devait de
                    # toute façon reconstruire un à un à chaque démarrage.
                    pts = np.array([k.pt for k in kp], dtype=np.float32)
                    desc_list.append((path, pts, des, w * h))
                    pending_orb.append((path, mtime, w, h, pts.tobytes(), des.tobytes()))
                except Exception as exc:
                    logger.debug("ORB descripteur échoué %s : %s", os.path.basename(path), exc)
                throttle_tick(lambda: self._cancelled)
        finally:
            # Persiste tout ce qui a été calculé jusqu'ici, y compris en cas
            # d'annulation (return ci-dessus) — c'est ce qui rend le scan reprenable.
            if pending_orb:
                cache.store_orb_features(pending_orb)

        m = len(desc_list)
        if m < 2:
            return

        self.progress.emit(
            phase1_total + (grand_total - phase1_total) // 2,
            grand_total,
            f"Tier 2 — comparaison ORB ({m} photos)…",
        )

        # `old_paths_set` / `new_paths_set` ont été établis plus haut, sur
        # l'ensemble des photos non groupées et non plus sur `desc_list` — ce
        # dernier ne contient désormais que les photos réellement impliquées
        # dans une paire à évaluer. Seuls les chemins effectivement présents
        # dans desc_list peuvent être marqués « comparés » en fin de passe.
        new_paths_set &= {path for path, *_ in desc_list}

        # Tri par aire croissante : accélère le prefiltre (photos similaires proches)
        desc_list.sort(key=lambda x: x[3])

        pairs_checked = 0
        logger.info("Tier 2 : comparaison ORB de %d photos non groupées par le Tier 1.", m)

        # Comparaison des paires d'une même ligne `i` en parallèle : les appels
        # cv2 (knnMatch/findHomography) libèrent le GIL, donc un pool de threads
        # apporte un vrai gain sans le coût de sérialisation d'un pool de process.
        # Chaque tâche traite un *lot* (chunk) de candidats plutôt qu'une seule
        # paire : une paire se compare en général en bien moins d'une milliseconde,
        # et soumettre une tâche par paire fait dominer le coût de dispatch du
        # ThreadPoolExecutor sur le gain du parallélisme (mesuré : ~10 % de gain
        # seulement au lieu d'un gain proche du nombre de coeurs). Découper en
        # `n_workers` lots amortit ce coût tout en gardant tous les coeurs actifs.
        # Les fusions (_merge) restent appliquées séquentiellement sur ce thread
        # une fois les résultats de la ligne connus — _merge est order-independent
        # pour une même ligne (tous les appels partagent path_i), donc paralléliser
        # ne change jamais le résultat final (juste, potentiellement, les ids de
        # groupe intermédiaires, renumérotés de toute façon en fin de _detect()).
        def _compare_chunk(items, des_i=None, pts_i=None, path_i=""):
            # BFMatcher local au worker plutôt qu'une instance partagée entre
            # threads du pool : le thread-safety de cv2.BFMatcher.knnMatch()
            # n'est pas documenté par OpenCV, et un objet natif partagé appelé
            # concurremment est un candidat plausible à un crash dur (segfault),
            # donc non rattrapable par un try/except Python. Le coût de création
            # est négligeable face au travail du lot (plusieurs comparaisons ORB).
            local_bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
            results = []
            for path_j, pts_j, des_j in items:
                # Lecture directe du flag (pas _is_cancelled(), qui émet le
                # signal `cancelled` à chaque appel) : permet d'interrompre un
                # lot en cours d'item en item plutôt que d'attendre qu'il aille
                # au bout — un lot peut contenir des dizaines de candidats,
                # chacun coûtant potentiellement plusieurs dizaines de ms
                # (knnMatch + RANSAC + warpPerspective + absdiff), et le
                # `with ThreadPoolExecutor(...)` englobant attend (wait=True à
                # la sortie du bloc) que les tâches déjà en cours se terminent
                # avant de rendre la main à closeEvent — sans ce garde-fou,
                # fermer l'application pendant le Tier 2 pouvait rester bloqué
                # plusieurs secondes de plus que nécessaire.
                if self._cancelled:
                    break
                throttle_tick(lambda: self._cancelled)
                # Discriminant le plus simple et le moins cher d'abord (lookup
                # dict), avant tout le pipeline ORB (knnMatch + RANSAC +
                # warpPerspective + absdiff) qui est de loin le plus coûteux de
                # toute la détection — deux photos de dates EXIF connues et
                # différentes ne sont jamais des doublons.
                if _dates_differ(self._dates, path_i, path_j):
                    results.append((path_j, False))
                    continue
                # Tout le corps de la comparaison (pas seulement knnMatch) est
                # protégé : une géométrie dégénérée (ex. homographie quasi
                # singulière) peut faire échouer n'importe quel appel cv2 en
                # aval (findHomography, warpPerspective, absdiff) — une paire
                # en erreur ne doit ni faire planter tout le lot, ni annuler
                # le scan entier (cf. run() qui, sinon, transformerait ça en
                # message d'erreur pour l'utilisateur au lieu d'un simple
                # "pas de correspondance" pour cette paire).
                try:
                    raw_matches = local_bf.knnMatch(des_i, des_j, k=2)

                    good = [
                        m1 for pair in raw_matches if len(pair) == 2
                        for m1, m2 in (pair,)
                        if m1.distance < _ORB_RATIO_TEST * m2.distance
                    ]
                    if len(good) < _ORB_GOOD_MIN:
                        results.append((path_j, False))
                        continue

                    src_pts = pts_i[[m1.queryIdx for m1 in good]].reshape(-1, 1, 2)
                    dst_pts = pts_j[[m1.trainIdx for m1 in good]].reshape(-1, 1, 2)

                    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                    if mask is None:
                        results.append((path_j, False))
                        continue

                    inliers = int(mask.sum())
                    if inliers < _ORB_MIN_INLIERS:
                        results.append((path_j, False))
                        continue

                    # Un arrière-plan riche et statique (ex: rafale de photos)
                    # peut fournir à lui seul assez d'inliers cohérents même si
                    # le sujet réel diffère complètement — le nombre d'inliers
                    # ne suffit pas à garantir que les photos se ressemblent
                    # réellement. Vérification supplémentaire : recaler j sur i
                    # via l'homographie trouvée et exiger que les pixels
                    # concordent sur la zone de recouvrement (cf. _ORB_MAX_MEAN_DIFF).
                    #
                    # Les deux images de travail ne sont décodées qu'ici, au
                    # seul endroit qui en a besoin : y parvenir suppose d'avoir
                    # déjà passé le ratio test de Lowe *et* le seuil d'inliers
                    # RANSAC, ce que ne font que de vrais quasi-doublons — une
                    # infime minorité des paires évaluées (cf. _GrayImageCache).
                    img_i = gray_cache.get(path_i)
                    img_j = gray_cache.get(path_j)
                    if img_i is None or img_j is None:
                        results.append((path_j, False))
                        continue

                    h_i, w_i = img_i.shape[:2]
                    warped = cv2.warpPerspective(img_j, H, (w_i, h_i))
                    valid = cv2.warpPerspective(
                        np.full(img_j.shape, 255, dtype=np.uint8), H, (w_i, h_i)
                    ) > 0
                    if not valid.any():
                        results.append((path_j, False))
                        continue

                    mean_diff = float(cv2.absdiff(warped, img_i)[valid].mean())
                    results.append((path_j, mean_diff <= _ORB_MAX_MEAN_DIFF))
                except Exception as exc:
                    logger.debug("Tier 2 comparaison échouée %s ↔ %s : %s",
                                os.path.basename(path_i), os.path.basename(path_j), exc)
                    results.append((path_j, False))
            return results

        n_workers = throttled_worker_count()
        comparison_start = phase1_total + (grand_total - phase1_total) // 2
        last_emit = time.monotonic()
        last_snapshot = last_emit
        executor = ThreadPoolExecutor(
            max_workers=n_workers, initializer=lower_current_thread_priority
        )
        cancelled_mid_flight = False
        # Jalonnement de la complétude des comparaisons, même raisonnement
        # qu'au Tier 1 (`_checkpoint_tier1`) : la ligne i traitée, les lignes
        # 0..i ont couvert toutes leurs paires ; on ne persiste toutefois que
        # jusqu'à l'indice du snapshot précédent, pour laisser à l'UI le temps
        # d'avoir écrit les groupes correspondants dans le catalogue.
        stored_upto2 = -1
        snapshot_idx2 = -1

        def _checkpoint_tier2(upto: int) -> None:
            nonlocal stored_upto2
            if upto <= stored_upto2:
                return
            rows = [
                (path, mtimes2[path])
                for path, *_ in desc_list[stored_upto2 + 1:upto + 1]
                if path in new_paths_set
            ]
            cache.store_compared_tier2(rows)
            stored_upto2 = upto

        try:
            for i in range(m):
                if self._is_cancelled():
                    cancelled_mid_flight = True
                    break
                path_i, pts_i, des_i, area_i = desc_list[i]

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
                if now - last_snapshot >= _LIVE_SNAPSHOT_INTERVAL:
                    last_snapshot = now
                    _checkpoint_tier2(snapshot_idx2)
                    snapshot_idx2 = i
                    self.partial_results.emit(_renumber(group_of), sorted(self._corrupted))

                candidates = []
                for j in range(i + 1, m):
                    path_j, pts_j, des_j, area_j = desc_list[j]

                    # Prefiltre ratio d'aire : sortie anticipée (liste triée — area_j ≥ area_i)
                    if area_i > 0 and area_j / area_i > _ORB_AREA_FACTOR:
                        break

                    # Paire ancien×ancien : déjà entièrement vérifiée lors d'une
                    # passe complète antérieure, aucun des deux n'a changé depuis.
                    if path_i in old_paths_set and path_j in old_paths_set:
                        continue

                    # Inutile de re-matcher deux photos déjà dans le même groupe
                    gi, gj = group_of.get(path_i), group_of.get(path_j)
                    if gi is not None and gi == gj:
                        continue

                    candidates.append((path_j, pts_j, des_j))

                if not candidates:
                    continue

                pairs_checked += len(candidates)
                chunk_size = max(1, -(-len(candidates) // n_workers))  # ceil division
                chunks = [candidates[k:k + chunk_size] for k in range(0, len(candidates), chunk_size)]
                worker = partial(_compare_chunk, des_i=des_i, pts_i=pts_i, path_i=path_i)
                futures = [executor.submit(worker, chunk) for chunk in chunks]
                for future in futures:
                    if self._is_cancelled():
                        for f in futures:
                            f.cancel()
                        cancelled_mid_flight = True
                        break
                    try:
                        chunk_result = future.result()
                    except Exception as exc:
                        # _compare_chunk protège déjà chaque paire individuellement ;
                        # ce garde-fou supplémentaire évite qu'une panne inattendue
                        # d'un lot entier (ex. MemoryError) n'interrompe tout le
                        # scan pour les photos restantes.
                        logger.warning("Tier 2 : lot de comparaison échoué pour %s : %s",
                                      os.path.basename(path_i), exc)
                        continue
                    for path_j, matched in chunk_result:
                        if matched:
                            logger.debug(
                                "Tier 2 crop détecté : %s ↔ %s",
                                os.path.basename(path_i), os.path.basename(path_j),
                            )
                            _merge(group_of, path_i, path_j, next_group)
                if cancelled_mid_flight:
                    break
        finally:
            # Cf. le commentaire équivalent au Tier 1 : un thread Python ne peut pas
            # être tué de l'extérieur, mais shutdown(wait=False, cancel_futures=True)
            # évite d'attendre ici la fin des tâches déjà en cours — chacune sort déjà
            # vite via `if self._cancelled: break` dans _compare_chunk (item par item),
            # donc ce non-blocage est surtout une garantie supplémentaire (ex. si un
            # seul appel cv2 est en cours et prend plus longtemps que prévu) plutôt
            # que le principal gain, contrairement au Tier 1.
            executor.shutdown(wait=not cancelled_mid_flight, cancel_futures=cancelled_mid_flight)
        if cancelled_mid_flight:
            return

        logger.info("Tier 2 : %d paire(s) vérifiées par ORB/RANSAC.", pairs_checked)
        _checkpoint_tier2(m - 1)


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
