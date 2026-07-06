# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import collections
import hashlib
import io
import logging
import sqlite3
import threading
from pathlib import Path

from PySide6.QtGui import QPixmap
from PySide6.QtCore import QByteArray

from src.core.app_dirs import APP_DATA_DIR

logger = logging.getLogger(__name__)

_DB_PATH = APP_DATA_DIR / "thumbnails.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS thumbnails (
    photo_hash TEXT PRIMARY KEY,
    photo_path TEXT,
    file_mtime REAL,
    thumbnail_data BLOB,
    generated_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


class ThumbnailCache:
    THUMB_SIZE = (220, 220)
    RAM_MAX = 300  # QPixmaps en RAM (~43 Mo max)

    def __init__(self, db_path: str | Path = _DB_PATH):
        self._db_path = str(db_path)
        self._thread_local = threading.local()  # connexion SQLite par thread
        # OrderedDict : insertion-order = LRU order (oldest first).
        # Remplace l'ancien couple dict + deque qui accumulait des clés en double
        # et provoquait des évictions prématurées sur les photos visitées plusieurs fois.
        self._ram: collections.OrderedDict[str, QPixmap] = collections.OrderedDict()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        """Retourne la connexion SQLite du thread courant, créée une seule fois par thread."""
        conn = getattr(self._thread_local, 'db_conn', None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # 2 Mo de cache par connexion (vs 4 Mo avant) — 6 threads = 12 Mo total.
            conn.execute("PRAGMA cache_size=-2048")
            self._thread_local.db_conn = conn
        return conn

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Checkpoint toutes les 500 pages (~2 Mo de WAL) au lieu des 1000 par défaut.
            # Évite que le WAL grossisse indéfiniment avec 50 000+ vignettes.
            conn.execute("PRAGMA wal_autocheckpoint=500")
            conn.execute(_CREATE_TABLE)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _key(photo_path: str) -> str:
        return hashlib.md5(photo_path.encode()).hexdigest()

    def get_ram(self, photo_path: str) -> QPixmap | None:
        """Retourne la vignette depuis le cache RAM uniquement (thread UI, non bloquant)."""
        key = self._key(photo_path)
        if key in self._ram:
            self._ram.move_to_end(key)   # marquer MRU
            return self._ram[key]
        return None

    def get(self, photo_path: str) -> QPixmap | None:
        key = self._key(photo_path)
        if key in self._ram:
            self._ram.move_to_end(key)
            return self._ram[key]
        return self._get_from_db(photo_path, key)

    def get_bytes(self, photo_path: str) -> bytes | None:
        """Retourne les bytes JPEG depuis SQLite sans créer de QPixmap.
        Thread-safe — conçu pour être appelé depuis les workers."""
        key = self._key(photo_path)
        try:
            mtime = Path(photo_path).stat().st_mtime
            conn = self._conn()
            row = conn.execute(
                "SELECT thumbnail_data, file_mtime FROM thumbnails WHERE photo_hash=?",
                (key,),
            ).fetchone()
            if row and abs(row[1] - mtime) < 1.0:
                return bytes(row[0])
        except Exception as e:
            logger.debug("Erreur lecture vignette DB %s: %s", photo_path, e)
        return None

    def generate(self, photo_path: str, edit=None) -> bytes | None:
        """Génère la vignette JPEG de photo_path et la sauvegarde en base.
        Retourne les bytes JPEG bruts — NE crée pas de QPixmap (thread-safe).
        L'appelant doit créer le QPixmap dans le thread UI et appeler store_pixmap()."""
        from src.library.exif_reader import VIDEO_EXT
        if Path(photo_path).suffix.lower() in VIDEO_EXT:
            return self._generate_video_thumb(photo_path)
        try:
            from PIL import Image, ImageOps

            with Image.open(photo_path) as img:
                img = ImageOps.exif_transpose(img)
                if edit is not None and edit.is_modified():
                    # Downscale intermédiaire pour accélérer le traitement
                    w, h = img.size
                    if max(w, h) > 1024:
                        s = 1024 / max(w, h)
                        img = img.resize((round(w * s), round(h * s)), Image.LANCZOS)
                    from src.processing.adjustments import ImageAdjuster
                    img = ImageAdjuster.apply_all(img, edit)
                img.thumbnail(self.THUMB_SIZE, Image.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                data = buf.getvalue()

            key = self._key(photo_path)
            mtime = Path(photo_path).stat().st_mtime

            conn = self._conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO thumbnails
                    (photo_hash, photo_path, file_mtime, thumbnail_data)
                VALUES (?, ?, ?, ?)
                """,
                (key, photo_path, mtime, data),
            )
            conn.commit()

            return data
        except Exception as e:
            logger.warning("Erreur génération vignette %s: %s", photo_path, e)
            return None

    def store_pixmap(self, photo_path: str, pixmap: QPixmap) -> None:
        """Stocke un QPixmap dans le cache RAM. Doit être appelé depuis le thread UI."""
        self._store_ram(self._key(photo_path), pixmap)

    def _generate_video_thumb(self, video_path: str) -> bytes | None:
        """Extrait la première frame de la vidéo et en fait une vignette mise en cache.
        Retourne les bytes JPEG bruts — NE crée pas de QPixmap (thread-safe).

        On utilise CAP_FFMPEG pour éviter les appels COM sur Windows (DirectShow
        peut marshaler des appels sur le thread UI et provoquer des freezes).
        On lit la première frame sans aucun seek : cv2.CAP_PROP_FRAME_COUNT peut
        scanner tout le fichier pour les AVI sans index, et cap.set(POS_FRAMES, N)
        décode depuis le dernier keyframe — les deux bloquent plusieurs secondes."""
        try:
            import cv2
            from PIL import Image

            # CAP_FFMPEG évite DirectShow/Media Foundation sur Windows
            cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                cap = cv2.VideoCapture(video_path)   # fallback
                if not cap.isOpened():
                    return None
            ret, frame = cap.read()   # première frame — aucun seek
            cap.release()
            if not ret or frame is None:
                return None

            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame)
            img.thumbnail(self.THUMB_SIZE, Image.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()

            key   = self._key(video_path)
            mtime = Path(video_path).stat().st_mtime
            conn  = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO thumbnails"
                " (photo_hash, photo_path, file_mtime, thumbnail_data)"
                " VALUES (?, ?, ?, ?)",
                (key, video_path, mtime, data),
            )
            conn.commit()

            return data
        except Exception as e:
            logger.warning("Erreur génération vignette vidéo %s: %s", video_path, e)
            return None

    def move_photo(self, old_path: str, new_path: str) -> None:
        """Transfère l'entrée de vignette vers new_path sans la régénérer."""
        old_key = self._key(old_path)
        new_key = self._key(new_path)
        # RAM : transférer le pixmap
        pixmap = self._ram.pop(old_key, None)
        if pixmap is not None:
            self._store_ram(new_key, pixmap)
        # DB : copier la ligne sous la nouvelle clé puis supprimer l'ancienne
        conn = self._conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO thumbnails
                (photo_hash, photo_path, file_mtime, thumbnail_data)
            SELECT ?, ?, file_mtime, thumbnail_data
            FROM thumbnails WHERE photo_hash = ?
            """,
            (new_key, new_path, old_key),
        )
        conn.execute("DELETE FROM thumbnails WHERE photo_hash = ?", (old_key,))
        conn.commit()

    def invalidate(self, photo_path: str) -> None:
        key = self._key(photo_path)
        self._ram.pop(key, None)
        conn = self._conn()
        conn.execute("DELETE FROM thumbnails WHERE photo_hash=?", (key,))
        conn.commit()

    def invalidate_many(self, photo_paths: list[str]) -> None:
        """Supprime en une transaction les vignettes d'une liste de chemins."""
        keys = [self._key(p) for p in photo_paths]
        for key in keys:
            self._ram.pop(key, None)
        conn = self._conn()
        conn.executemany(
            "DELETE FROM thumbnails WHERE photo_hash=?", [(k,) for k in keys]
        )
        conn.commit()

    def _store_ram(self, key: str, pixmap: QPixmap) -> None:
        """LRU sur OrderedDict : O(1) insert/evict, sans doublons."""
        if key in self._ram:
            # Photo déjà en cache : mettre à jour et marquer MRU
            self._ram.move_to_end(key)
            self._ram[key] = pixmap
        else:
            if len(self._ram) >= self.RAM_MAX:
                self._ram.popitem(last=False)   # éviction LRU (le plus ancien)
            self._ram[key] = pixmap

    def _get_from_db(self, photo_path: str, key: str) -> QPixmap | None:
        try:
            mtime = Path(photo_path).stat().st_mtime
            conn = self._conn()
            row = conn.execute(
                "SELECT thumbnail_data, file_mtime FROM thumbnails WHERE photo_hash=?",
                (key,),
            ).fetchone()
            if row and abs(row[1] - mtime) < 1.0:
                pixmap = QPixmap()
                pixmap.loadFromData(QByteArray(row[0]))
                self._store_ram(key, pixmap)
                return pixmap
        except Exception as e:
            logger.debug(f"Erreur lecture vignette DB {photo_path}: {e}")
        return None
