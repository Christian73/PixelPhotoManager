import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

from src.core.app_dirs import APP_DATA_DIR
from src.core.models import EditInfo

logger = logging.getLogger(__name__)

_DB_PATH = APP_DATA_DIR / "edits.db"

_HISTORY_LIMIT = 50

_CREATE_EDITS = """
CREATE TABLE IF NOT EXISTS photo_edits (
    photo_path     TEXT PRIMARY KEY,
    brightness     REAL    DEFAULT 0.0,
    contrast       REAL    DEFAULT 0.0,
    saturation     REAL    DEFAULT 0.0,
    gamma          REAL    DEFAULT 1.0,
    sharpness      REAL    DEFAULT 0.0,
    noise_reduction REAL   DEFAULT 0.0,
    rotation       REAL    DEFAULT 0.0,
    straighten     REAL    DEFAULT 0.0,
    flip_h         INTEGER DEFAULT 0,
    flip_v         INTEGER DEFAULT 0,
    crop           TEXT    DEFAULT NULL,
    bw             INTEGER DEFAULT 0,
    bw_red         REAL    DEFAULT 0.0,
    bw_green       REAL    DEFAULT 0.0,
    bw_blue        REAL    DEFAULT 0.0,
    modified_at    TEXT    DEFAULT CURRENT_TIMESTAMP
)
"""

_MIGRATE_STRAIGHTEN = (
    "ALTER TABLE photo_edits ADD COLUMN straighten REAL DEFAULT 0.0"
)

_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path  TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    operation   TEXT NOT NULL DEFAULT 'edit',
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_history_path "
    "ON edit_history(photo_path, id DESC)"
)


class EditDatabase:
    """Stockage SQLite non-destructif des retouches.

    Deux tables :
    - ``photo_edits``  : état courant par photo (une ligne par photo modifiée).
    - ``edit_history`` : historique des états sauvegardés (undo/redo persistant).
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------ init

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(_CREATE_EDITS)
            conn.execute(_CREATE_HISTORY)
            conn.execute(_CREATE_INDEX)
            try:
                conn.execute(_MIGRATE_STRAIGHTEN)
            except sqlite3.OperationalError:
                pass  # colonne déjà présente
            self._migrate_normalize_paths(conn)
            conn.commit()

    def _migrate_normalize_paths(self, conn) -> None:
        """Normalise les séparateurs de chemin dans les données existantes."""
        for table, col in [("photo_edits", "photo_path"), ("edit_history", "photo_path")]:
            rows = conn.execute(f"SELECT rowid, {col} FROM {table}").fetchall()
            updates = [
                (os.path.normpath(p), rid)
                for rid, p in rows
                if p and os.path.normpath(p) != p
            ]
            if updates:
                conn.executemany(
                    f"UPDATE {table} SET {col}=? WHERE rowid=?", updates
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ API publique

    def load(self, photo_path: str) -> EditInfo:
        """Charge l'état courant des retouches. Retourne EditInfo() vierge si absent."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM photo_edits WHERE photo_path = ?",
                        (photo_path,),
                    ).fetchone()
                if row is None:
                    return EditInfo()
                return EditInfo(
                    brightness=row["brightness"],
                    contrast=row["contrast"],
                    saturation=row["saturation"],
                    gamma=row["gamma"],
                    sharpness=row["sharpness"],
                    noise_reduction=row["noise_reduction"],
                    rotation=row["rotation"],
                    straighten=row["straighten"] or 0.0,
                    flip_h=bool(row["flip_h"]),
                    flip_v=bool(row["flip_v"]),
                    crop=tuple(json.loads(row["crop"])) if row["crop"] else None,
                    bw=bool(row["bw"]),
                    bw_red=row["bw_red"],
                    bw_green=row["bw_green"],
                    bw_blue=row["bw_blue"],
                )
            except Exception as e:
                logger.error(f"Erreur lecture retouches {photo_path}: {e}")
                return EditInfo()

    def save(self, photo_path: str, edit: EditInfo, operation: str = "edit") -> None:
        """Sauvegarde l'état courant et l'enregistre dans l'historique."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            try:
                with self._connect() as conn:
                    if not edit.is_modified():
                        conn.execute(
                            "DELETE FROM photo_edits WHERE photo_path = ?",
                            (photo_path,),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO photo_edits
                                (photo_path, brightness, contrast, saturation, gamma,
                                 sharpness, noise_reduction, rotation, straighten,
                                 flip_h, flip_v, crop, bw, bw_red, bw_green, bw_blue,
                                 modified_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                    CURRENT_TIMESTAMP)
                            """,
                            (
                                photo_path,
                                edit.brightness, edit.contrast, edit.saturation,
                                edit.gamma, edit.sharpness, edit.noise_reduction,
                                edit.rotation, edit.straighten,
                                int(edit.flip_h), int(edit.flip_v),
                                json.dumps(list(edit.crop)) if edit.crop else None,
                                int(edit.bw),
                                edit.bw_red, edit.bw_green, edit.bw_blue,
                            ),
                        )
                    conn.execute(
                        "INSERT INTO edit_history (photo_path, state_json, operation)"
                        " VALUES (?, ?, ?)",
                        (photo_path, json.dumps(edit.to_dict()), operation),
                    )
                    # Limite le nombre d'entrées d'historique par photo
                    conn.execute(
                        """
                        DELETE FROM edit_history
                        WHERE photo_path = ?
                          AND id NOT IN (
                              SELECT id FROM edit_history
                              WHERE photo_path = ?
                              ORDER BY id DESC
                              LIMIT ?
                          )
                        """,
                        (photo_path, photo_path, _HISTORY_LIMIT),
                    )
                    conn.commit()
            except Exception as e:
                logger.error(f"Erreur sauvegarde retouches {photo_path}: {e}")

    def has_edits(self, photo_path: str) -> bool:
        with self._lock:
            try:
                with self._connect() as conn:
                    return conn.execute(
                        "SELECT 1 FROM photo_edits WHERE photo_path = ?",
                        (photo_path,),
                    ).fetchone() is not None
            except Exception as e:
                logger.error(f"Erreur vérification retouches {photo_path}: {e}")
                return False

    def delete(self, photo_path: str) -> None:
        """Supprime l'état courant et tout l'historique pour cette photo."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "DELETE FROM photo_edits  WHERE photo_path = ?", (photo_path,)
                    )
                    conn.execute(
                        "DELETE FROM edit_history WHERE photo_path = ?", (photo_path,)
                    )
                    conn.commit()
            except Exception as e:
                logger.error(f"Erreur suppression retouches {photo_path}: {e}")

    def rename_photo(self, old_path: str, new_path: str) -> None:
        """Propage un renommage de fichier dans les deux tables."""
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE photo_edits  SET photo_path=? WHERE photo_path=?",
                        (new_path, old_path),
                    )
                    conn.execute(
                        "UPDATE edit_history SET photo_path=? WHERE photo_path=?",
                        (new_path, old_path),
                    )
                    conn.commit()
            except Exception as e:
                logger.error(f"Erreur renommage retouches {old_path} → {new_path}: {e}")

    def get_history(self, photo_path: str, limit: int = 20) -> list[EditInfo]:
        """Retourne les états précédents du plus ancien au plus récent (pour undo persistant)."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            try:
                with self._connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT state_json FROM edit_history
                        WHERE photo_path = ?
                        ORDER BY id DESC LIMIT ?
                        """,
                        (photo_path, limit),
                    ).fetchall()
                result = []
                for row in reversed(rows):
                    try:
                        result.append(EditInfo.from_dict(json.loads(row["state_json"])))
                    except Exception:
                        pass
                return result
            except Exception as e:
                logger.error(f"Erreur lecture historique {photo_path}: {e}")
                return []
