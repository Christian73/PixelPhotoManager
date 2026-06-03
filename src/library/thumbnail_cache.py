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
    RAM_MAX = 500

    def __init__(self, db_path: str | Path = _DB_PATH):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._ram: dict[str, QPixmap] = {}
        self._ram_order: list[str] = []
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(_CREATE_TABLE)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _key(photo_path: str) -> str:
        return hashlib.md5(photo_path.encode()).hexdigest()

    def get(self, photo_path: str) -> QPixmap | None:
        key = self._key(photo_path)
        if key in self._ram:
            return self._ram[key]
        return self._get_from_db(photo_path, key)

    def generate(self, photo_path: str) -> QPixmap | None:
        try:
            from PIL import Image, ImageOps

            with Image.open(photo_path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail(self.THUMB_SIZE, Image.LANCZOS)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                data = buf.getvalue()

            key = self._key(photo_path)
            mtime = Path(photo_path).stat().st_mtime

            with self._lock:
                conn = self._conn()
                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO thumbnails
                            (photo_hash, photo_path, file_mtime, thumbnail_data)
                        VALUES (?, ?, ?, ?)
                        """,
                        (key, photo_path, mtime, data),
                    )
                    conn.commit()
                finally:
                    conn.close()

            pixmap = QPixmap()
            pixmap.loadFromData(QByteArray(data))
            self._store_ram(key, pixmap)
            return pixmap
        except Exception as e:
            logger.debug(f"Erreur génération vignette {photo_path}: {e}")
            return None

    def invalidate(self, photo_path: str) -> None:
        key = self._key(photo_path)
        self._ram.pop(key, None)
        if key in self._ram_order:
            self._ram_order.remove(key)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM thumbnails WHERE photo_hash=?", (key,))
                conn.commit()
            finally:
                conn.close()

    def _store_ram(self, key: str, pixmap: QPixmap) -> None:
        if len(self._ram_order) >= self.RAM_MAX:
            oldest = self._ram_order.pop(0)
            self._ram.pop(oldest, None)
        self._ram[key] = pixmap
        self._ram_order.append(key)

    def _get_from_db(self, photo_path: str, key: str) -> QPixmap | None:
        try:
            mtime = Path(photo_path).stat().st_mtime
            with self._lock:
                conn = self._conn()
                try:
                    row = conn.execute(
                        "SELECT thumbnail_data, file_mtime FROM thumbnails WHERE photo_hash=?",
                        (key,),
                    ).fetchone()
                finally:
                    conn.close()
            if row and abs(row[1] - mtime) < 1.0:
                pixmap = QPixmap()
                pixmap.loadFromData(QByteArray(row[0]))
                self._store_ram(key, pixmap)
                return pixmap
        except Exception as e:
            logger.debug(f"Erreur lecture vignette DB {photo_path}: {e}")
        return None
