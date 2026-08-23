# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import collections
import hashlib
import io
import json
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

# Fingerprint of the edits applied to the stored thumbnail. The rows
# predating this column get '' — the value of thumbnails with no edit,
# which is what they actually are.
_MIGRATE_EDIT_SIG = "ALTER TABLE thumbnails ADD COLUMN edit_sig TEXT DEFAULT ''"


def edit_signature(edit) -> str:
    """Fingerprint of the edit state of a photo, '' if there is none.

    Edits do not modify the file: `file_mtime` alone therefore cannot tell that
    a cached thumbnail is stale after a rotation or a crop. By storing this
    fingerprint next to the thumbnail, any read of the cache for an edit state
    other than the one used to produce it misses and regenerates — without
    depending on the cell being on screen at the time of the edit (the grid is
    virtualised: a photo off screen has no cell to invalidate)."""
    if edit is None or not edit.is_modified():
        return ""
    payload = json.dumps(edit.to_dict(), sort_keys=True, default=str)
    return hashlib.md5(payload.encode()).hexdigest()[:16]


class ThumbnailCache:
    THUMB_SIZE = (220, 220)
    RAM_MAX = 300  # QPixmaps en RAM (~43 Mo max)

    def __init__(self, db_path: str | Path = _DB_PATH):
        self._db_path = str(db_path)
        self._thread_local = threading.local()  # one SQLite connection per thread
        # Serialises the SQLite writes between threads (the Catalog/FaceDatabase
        # pattern): without it, the 4+2 thumbnail generation threads (thumbnail_grid.py)
        # and the UI thread (invalidate() on a deletion) fight over SQLite's write lock
        # through its busy_timeout — a plain Python Lock, held only for the duration of
        # the INSERT/DELETE+commit (never during the image decoding), is faster and more
        # deterministic than waiting on SQLite's back-off/retry.
        self._db_lock = threading.Lock()
        # OrderedDict: insertion-order = LRU order (oldest first).
        # Replaces the former dict + deque pair, which accumulated duplicate keys
        # and caused premature evictions on photos visited several times.
        # Value = (fingerprint of the edits, pixmap) — cf. edit_signature().
        self._ram: collections.OrderedDict[str, tuple[str, QPixmap]] = collections.OrderedDict()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        """Returns the SQLite connection of the current thread, created once per thread."""
        conn = getattr(self._thread_local, 'db_conn', None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # 2 MB of cache per connection (vs 4 MB before) — 6 threads = 12 MB in total.
            conn.execute("PRAGMA cache_size=-2048")
            self._thread_local.db_conn = conn
        return conn

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Checkpoint every 500 pages (~2 MB of WAL) instead of the default 1000.
            # Keeps the WAL from growing indefinitely with 50,000+ thumbnails.
            conn.execute("PRAGMA wal_autocheckpoint=500")
            conn.execute(_CREATE_TABLE)
            try:
                conn.execute(_MIGRATE_EDIT_SIG)
            except sqlite3.OperationalError:
                pass  # column already present
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _key(photo_path: str) -> str:
        return hashlib.md5(photo_path.encode()).hexdigest()

    def get_ram(self, photo_path: str, edit_sig: str | None = None) -> QPixmap | None:
        """Returns the thumbnail from the RAM cache only (UI thread, non blocking).

        `edit_sig` = the expected edit fingerprint; None (the default) accepts
        the entry whatever its own — for the uses where a slightly stale
        thumbnail is of no consequence (the viewer's placeholder, the cover of
        a duplicate group)."""
        key = self._key(photo_path)
        # try/except rather than a membership test: invalidate()/
        # invalidate_many() can now run in a background thread
        # (_DeleteWorkerThread) and remove the key between the test and the access.
        # Each individual operation on the OrderedDict stays atomic (GIL).
        try:
            self._ram.move_to_end(key)   # marquer MRU
            sig, pixmap = self._ram[key]
        except KeyError:
            return None
        if edit_sig is not None and sig != edit_sig:
            return None
        return pixmap

    def get(self, photo_path: str, edit_sig: str | None = None) -> QPixmap | None:
        pixmap = self.get_ram(photo_path, edit_sig)
        if pixmap is not None:
            return pixmap
        return self._get_from_db(photo_path, self._key(photo_path), edit_sig)

    def get_bytes(self, photo_path: str, edit_sig: str = "") -> bytes | None:
        """Returns the JPEG bytes from SQLite without creating a QPixmap.
        Thread-safe — designed to be called from the workers.

        A miss if the stored thumbnail was produced with an edit state other
        than `edit_sig` (cf. edit_signature)."""
        key = self._key(photo_path)
        try:
            mtime = Path(photo_path).stat().st_mtime
            conn = self._conn()
            row = conn.execute(
                "SELECT thumbnail_data, file_mtime, edit_sig FROM thumbnails"
                " WHERE photo_hash=?",
                (key,),
            ).fetchone()
            if row and abs(row[1] - mtime) < 1.0 and (row[2] or "") == edit_sig:
                return bytes(row[0])
        except Exception as e:
            logger.debug("Erreur lecture vignette DB %s: %s", photo_path, e)
        return None

    def generate(self, photo_path: str, edit=None) -> bytes | None:
        """Generates the JPEG thumbnail of photo_path and saves it in the database.
        Returns the raw JPEG bytes — does NOT create a QPixmap (thread-safe).
        The caller must create the QPixmap in the UI thread and call store_pixmap()."""
        from src.library.exif_reader import VIDEO_EXT
        if Path(photo_path).suffix.lower() in VIDEO_EXT:
            return self._generate_video_thumb(photo_path)
        try:
            from PIL import Image, ImageOps

            from src.library.image_loader import open_image

            with open_image(photo_path) as img:
                img = ImageOps.exif_transpose(img)
                if edit is not None and edit.is_modified():
                    # Intermediate downscale to speed the processing up
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

            with self._db_lock:
                conn = self._conn()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO thumbnails
                        (photo_hash, photo_path, file_mtime, thumbnail_data, edit_sig)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, photo_path, mtime, data, edit_signature(edit)),
                )
                conn.commit()

            return data
        except Exception as e:
            logger.warning("Erreur génération vignette %s: %s", photo_path, e)
            return None

    def store_pixmap(self, photo_path: str, pixmap: QPixmap, edit_sig: str = "") -> None:
        """Stores a QPixmap in the RAM cache. Must be called from the UI thread."""
        self._store_ram(self._key(photo_path), (edit_sig, pixmap))

    def _generate_video_thumb(self, video_path: str) -> bytes | None:
        """Extracts the first frame of the video and makes a cached thumbnail of it.
        Returns the raw JPEG bytes — does NOT create a QPixmap (thread-safe).

        CAP_FFMPEG is used to avoid the COM calls on Windows (DirectShow can
        marshal calls onto the UI thread and cause freezes).
        The first frame is read with no seek at all: cv2.CAP_PROP_FRAME_COUNT
        may scan the whole file for AVIs without an index, and
        cap.set(POS_FRAMES, N) decodes from the last keyframe — both block for
        several seconds."""
        try:
            import cv2
            from PIL import Image
            from src.library.exif_reader import ascii_safe_path

            with ascii_safe_path(video_path) as safe_path:
                # CAP_FFMPEG avoids DirectShow/Media Foundation on Windows
                cap = cv2.VideoCapture(safe_path, cv2.CAP_FFMPEG)
                if not cap.isOpened():
                    cap = cv2.VideoCapture(safe_path)   # fallback
                    if not cap.isOpened():
                        return None
                ret, frame = cap.read()   # first frame — no seek at all
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
            with self._db_lock:
                conn = self._conn()
                conn.execute(
                    "INSERT OR REPLACE INTO thumbnails"
                    " (photo_hash, photo_path, file_mtime, thumbnail_data, edit_sig)"
                    " VALUES (?, ?, ?, ?, '')",
                    (key, video_path, mtime, data),
                )
                conn.commit()

            return data
        except Exception as e:
            logger.warning("Erreur génération vignette vidéo %s: %s", video_path, e)
            return None

    def move_photo(self, old_path: str, new_path: str) -> None:
        """Transfers the thumbnail entry to new_path without regenerating it."""
        old_key = self._key(old_path)
        new_key = self._key(new_path)
        # RAM: transfer the entry (the edit fingerprint included)
        entry = self._ram.pop(old_key, None)
        if entry is not None:
            self._store_ram(new_key, entry)
        # DB: copy the row under the new key, then delete the old one
        with self._db_lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO thumbnails
                    (photo_hash, photo_path, file_mtime, thumbnail_data, edit_sig)
                SELECT ?, ?, file_mtime, thumbnail_data, edit_sig
                FROM thumbnails WHERE photo_hash = ?
                """,
                (new_key, new_path, old_key),
            )
            conn.execute("DELETE FROM thumbnails WHERE photo_hash = ?", (old_key,))
            conn.commit()

    def invalidate(self, photo_path: str) -> None:
        key = self._key(photo_path)
        self._ram.pop(key, None)
        with self._db_lock:
            conn = self._conn()
            conn.execute("DELETE FROM thumbnails WHERE photo_hash=?", (key,))
            conn.commit()

    def invalidate_many(self, photo_paths: list[str]) -> None:
        """Deletes the thumbnails of a list of paths in a single transaction."""
        keys = [self._key(p) for p in photo_paths]
        for key in keys:
            self._ram.pop(key, None)
        with self._db_lock:
            conn = self._conn()
            conn.executemany(
                "DELETE FROM thumbnails WHERE photo_hash=?", [(k,) for k in keys]
            )
            conn.commit()

    def _store_ram(self, key: str, entry: tuple[str, QPixmap]) -> None:
        """LRU on an OrderedDict: O(1) insert/evict, with no duplicates."""
        if key in self._ram:
            # Photo already cached: update it and mark it MRU
            self._ram.move_to_end(key)
            self._ram[key] = entry
        else:
            if len(self._ram) >= self.RAM_MAX:
                self._ram.popitem(last=False)   # LRU eviction (the oldest)
            self._ram[key] = entry

    def _get_from_db(self, photo_path: str, key: str,
                     edit_sig: str | None = None) -> QPixmap | None:
        try:
            mtime = Path(photo_path).stat().st_mtime
            conn = self._conn()
            row = conn.execute(
                "SELECT thumbnail_data, file_mtime, edit_sig FROM thumbnails"
                " WHERE photo_hash=?",
                (key,),
            ).fetchone()
            if (row and abs(row[1] - mtime) < 1.0
                    and (edit_sig is None or (row[2] or "") == edit_sig)):
                pixmap = QPixmap()
                pixmap.loadFromData(QByteArray(row[0]))
                self._store_ram(key, (row[2] or "", pixmap))
                return pixmap
        except Exception as e:
            logger.debug(f"Erreur lecture vignette DB {photo_path}: {e}")
        return None
