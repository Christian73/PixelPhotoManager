# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Threads d'arrière-plan de MainWindow (règle CLAUDE.md : l'UI ne bloque
jamais). Extraits de main_window.py — les noms préfixés d'un underscore sont
conservés pour l'historique et les tests existants ; ils restent des détails
d'implémentation de MainWindow, pas une API de plugin."""

import logging
import os
from PySide6.QtCore import QThread, Signal

from src.core.i18n import translate
from src.faces.clusterer import reset_clustering_cache
from src.library.dedup_cache import DedupCache

logger = logging.getLogger(__name__)


class _CatalogLoadThread(QThread):
    """Charge get_all_photos() hors du thread UI et émet les résultats par lots."""

    batch_ready = Signal(list)  # list[PhotoInfo]

    def __init__(self, catalog, batch_size: int = 300, reverse: bool = False, parent=None):
        super().__init__(parent)
        self._catalog = catalog
        self._batch_size = batch_size
        self._reverse = reverse
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        # get_all_photos() est trié chronologique descendant (SQL) ; "reverse"
        # inverse en ascendant pour suivre le réglage "Ordre d'affichage" —
        # la vue "Toutes les photos" reste toujours chronologique, seule la
        # direction est configurable (cf. MainWindow._sort_photos_for_display).
        photos = self._catalog.get_all_photos()
        if self._reverse:
            photos = list(reversed(photos))
        for i in range(0, len(photos), self._batch_size):
            if self._stop:
                break
            self.batch_ready.emit(photos[i : i + self._batch_size])


class _PhotoQueryThread(QThread):
    """Exécute une requête catalog/face_db dans un thread secondaire.

    Le tri d'affichage (O(n log n) sur toute la bibliothèque pour "Toutes les
    photos") est fait ici aussi : les paramètres de tri sont résolus par
    l'appelant sur le thread UI (lectures de Config) et passés au thread."""

    photos_ready = Signal(list, str)   # list[PhotoInfo], context_key

    def __init__(self, fn, context_key: str, sort_key_fn=None,
                 sort_reverse: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._fn           = fn
        self._context_key  = context_key
        self._sort_key_fn  = sort_key_fn
        self._sort_reverse = sort_reverse

    def run(self) -> None:
        try:
            photos = self._fn()
            if self._sort_key_fn is not None:
                photos = sorted(photos, key=self._sort_key_fn,
                                reverse=self._sort_reverse)
            self.photos_ready.emit(photos, self._context_key)
        except Exception:
            self.photos_ready.emit([], self._context_key)


class _DeleteWorkerThread(QThread):
    """Envoie les fichiers à la corbeille Windows puis purge catalogue/
    vignettes/visages en lot, hors du thread UI (règle CLAUDE.md : l'UI ne
    bloque jamais). Jamais d'unlink définitif : si la corbeille est
    indisponible (lecteur réseau…), le fichier est laissé intact, son chemin
    part dans errors et le catalogue n'est PAS purgé pour lui."""

    progress        = Signal(int, int)     # fait, total (libellé barre d'état)
    finished_delete = Signal(list, list)   # deleted_paths: list[str], errors: list[str]

    def __init__(self, paths: list[str], catalog, thumb_cache, face_db,
                 parent=None) -> None:
        super().__init__(parent)
        self._paths       = list(paths)
        self._catalog     = catalog
        self._thumb_cache = thumb_cache
        self._face_db     = face_db

    def run(self) -> None:
        from src.library.trash import move_to_trash
        deleted: list[str] = []
        errors:  list[str] = []
        for i, path in enumerate(self._paths):
            try:
                move_to_trash(path)
                deleted.append(path)
            except FileNotFoundError:
                # Déjà absent du disque : purger quand même le catalogue
                # (équivalent de l'ancien missing_ok=True).
                deleted.append(path)
            except Exception as e:
                errors.append(
                    translate("DeleteWorker",
                              "{name} : mise à la corbeille impossible ({err}) — "
                              "le fichier n'a PAS été supprimé."
                              ).format(name=os.path.basename(path), err=e)
                )
            self.progress.emit(i + 1, len(self._paths))
        if deleted:
            try:
                # En lot : delete_photos dissout aussi les groupes de doublons
                # devenus singletons, dans la même transaction.
                self._catalog.delete_photos(deleted)
                self._thumb_cache.invalidate_many(deleted)
                self._face_db.delete_for_paths(deleted)
            except Exception:
                logger.exception("Purge catalogue/vignettes/visages après suppression")
        self.finished_delete.emit(deleted, errors)


class _DupMigrationThread(QThread):
    """Exécute la migration des groupes de doublons à dates EXIF conflictuelles
    puis compte les groupes restants (badge sidebar), hors du thread UI : au
    premier lancement après upgrade, la migration charge TOUS les groupes avec
    leurs photos — exécutée avant dans MainWindow.__init__, elle retardait
    d'autant le premier affichage de la fenêtre.

    Migration : dissout les groupes existants qui contiennent au moins deux
    membres dont la date EXIF est connue et différente (cf.
    duplicate_detector.py::_dates_differ). Un tel groupe ne peut plus être
    *créé* aujourd'hui, mais l'incrémentalité de la détection
    (compared_tier1/compared_tier2, dedup_cache.py) ne recompare et ne dissout
    jamais spontanément un groupe déjà formé avant l'ajout de cette règle —
    cf. dedup_exif_date_exclusion_2026-07 en mémoire.

    En plus de dissoudre le groupe en base (duplicate_group_id=NULL, comme
    Catalog.ignore_duplicate_group), retire aussi ses membres de
    compared_tier1/tier2 pour qu'ils soient recomparés intégralement au
    prochain passage plutôt que de rester des « paires ancien×ancien » jamais
    réévaluées — sans ça, la dissolution ne durerait pas : le prochain
    seed_groups() les retrouverait simplement fusionnés à l'identique puisque
    plus rien ne les aurait jamais reconfrontés.

    Naturellement idempotente : une fois ces groupes dissous, la règle de date
    empêche définitivement leur recréation, donc ce balayage ne trouve plus
    rien aux démarrages suivants — pas de flag « déjà exécuté » nécessaire.

    Séquencement : _start_duplicate_detection ne doit jamais démarrer avant la
    fin de cette migration (cf. _on_persons_thumbnails_ready_start_duplicates),
    sinon seed_groups serait amorcé avec les groupes non encore dissous."""

    done = Signal(int)   # nombre de groupes de doublons restants (badge)

    def __init__(self, catalog, parent=None) -> None:
        super().__init__(parent)
        self._catalog = catalog

    def run(self) -> None:
        try:
            self._migrate()
        except Exception:
            logger.exception("Migration des groupes de doublons à dates conflictuelles")
        try:
            self.done.emit(self._catalog.count_duplicate_groups())
        except Exception:
            self.done.emit(0)

    def _migrate(self) -> None:
        if self._catalog.count_duplicate_groups() == 0:
            return
        groups = self._catalog.get_duplicate_groups()
        conflicted_group_ids: list[int] = []
        conflicted_paths: list[str] = []
        for gid, photos in groups.items():
            known_dates = {p.date_taken for p in photos if p.date_taken is not None}
            if len(known_dates) > 1:
                conflicted_group_ids.append(gid)
                conflicted_paths.extend(p.path for p in photos)
        if not conflicted_group_ids:
            return
        for gid in conflicted_group_ids:
            self._catalog.ignore_duplicate_group(gid)
        cache = DedupCache()
        cache.open()
        try:
            cache.remove_compared(conflicted_paths)
        finally:
            cache.close()
        logger.info(
            "Migration doublons : %d groupe(s) à dates EXIF conflictuelles dissous "
            "(%d photo(s) remise(s) en file pour recomparaison complète).",
            len(conflicted_group_ids), len(conflicted_paths),
        )


class _PersonsRefreshThread(QThread):
    """Charge get_persons + enrich_persons + get_unnamed_clusters hors du thread UI."""

    result_ready = Signal(list, int)   # persons, unnamed_cluster_count

    def __init__(self, catalog, face_db, parent=None) -> None:
        super().__init__(parent)
        self._catalog  = catalog
        self._face_db  = face_db

    def run(self) -> None:
        try:
            persons = self._catalog.get_persons()
            self._face_db.enrich_persons(persons)
            count = len(self._face_db.get_unnamed_clusters())
            self.result_ready.emit(persons, count)
        except Exception:
            self.result_ready.emit([], 0)


class _ResuggestThread(QThread):
    """Recalcule les suggestions après le rejet d'un cluster, dans un thread secondaire."""

    def __init__(self, face_db, cluster_ids: list, exclude_pid, parent=None) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._cluster_ids = cluster_ids
        self._exclude_pid = exclude_pid

    def run(self) -> None:
        self._face_db.resuggest_clusters(self._cluster_ids, self._exclude_pid)


class _ResetWorkerThread(QThread):
    """
    Attend l'arrêt des threads d'indexation/clustering en cours,
    effectue le reset DB demandé, puis émet done(choice).
    """

    done = Signal(int)   # choice : RESET_CLUSTERING ou RESET_FULL

    def __init__(
        self,
        face_db,
        choice: int,
        threads_to_wait: list,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._face_db = face_db
        self._choice  = choice
        self._threads = threads_to_wait   # refs Python fortes → gardés en vie

    def run(self) -> None:
        for t in self._threads:
            try:
                if t.isRunning():
                    t.wait(10_000)   # 10 s max par thread
            except RuntimeError:
                pass   # objet C++ déjà supprimé
        if self._choice == 1:   # RESET_CLUSTERING
            self._face_db.reset_clustering()
        else:                    # RESET_FULL
            self._face_db.reset_index()
        # Les deux resets vident cluster_id en masse sans changer le nombre
        # de visages non identifiés si la bibliothèque n'a pas bougé entre
        # temps : sans invalider ce cache, le clustering qui suit (déclenché
        # par _on_reset_done) sauterait silencieusement (cf. reset_clustering_cache).
        reset_clustering_cache()
        self.done.emit(self._choice)
