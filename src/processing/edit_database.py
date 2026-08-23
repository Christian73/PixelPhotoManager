# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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
    color_red      REAL    DEFAULT 0.0,
    color_green    REAL    DEFAULT 0.0,
    color_blue     REAL    DEFAULT 0.0,
    modified_at    TEXT    DEFAULT CURRENT_TIMESTAMP
)
"""

_MIGRATE_STRAIGHTEN = (
    "ALTER TABLE photo_edits ADD COLUMN straighten REAL DEFAULT 0.0"
)
_MIGRATE_GAMMA_CURVE = [
    "ALTER TABLE photo_edits ADD COLUMN gamma_use_curve   INTEGER DEFAULT 0",
    "ALTER TABLE photo_edits ADD COLUMN gamma_curve_points TEXT    DEFAULT NULL",
]
_MIGRATE_COLOR_CHANNELS = [
    "ALTER TABLE photo_edits ADD COLUMN color_red   REAL DEFAULT 0.0",
    "ALTER TABLE photo_edits ADD COLUMN color_green REAL DEFAULT 0.0",
    "ALTER TABLE photo_edits ADD COLUMN color_blue  REAL DEFAULT 0.0",
]
_MIGRATE_RED_EYE = (
    "ALTER TABLE photo_edits ADD COLUMN red_eye_regions TEXT DEFAULT NULL"
)
_MIGRATE_VIGNETTE = [
    "ALTER TABLE photo_edits ADD COLUMN vignette_strength REAL DEFAULT 0.0",
    "ALTER TABLE photo_edits ADD COLUMN vignette_radius   REAL DEFAULT 0.75",
    "ALTER TABLE photo_edits ADD COLUMN vignette_feather  REAL DEFAULT 0.5",
    "ALTER TABLE photo_edits ADD COLUMN vignette_color    TEXT DEFAULT 'black'",
    "ALTER TABLE photo_edits ADD COLUMN vignette_shape    TEXT DEFAULT 'ellipse'",
]
_MIGRATE_VIGNETTE_V2 = [
    "ALTER TABLE photo_edits ADD COLUMN vignette_cx    REAL DEFAULT 0.5",
    "ALTER TABLE photo_edits ADD COLUMN vignette_cy    REAL DEFAULT 0.5",
    "ALTER TABLE photo_edits ADD COLUMN vignette_rx1   REAL DEFAULT 0.4",
    "ALTER TABLE photo_edits ADD COLUMN vignette_ry1   REAL DEFAULT 0.4",
    "ALTER TABLE photo_edits ADD COLUMN vignette_rx2   REAL DEFAULT 0.8",
    "ALTER TABLE photo_edits ADD COLUMN vignette_ry2   REAL DEFAULT 0.8",
    "ALTER TABLE photo_edits ADD COLUMN vignette_angle REAL DEFAULT 0.0",
]
_MIGRATE_ANNOTATIONS = (
    "ALTER TABLE photo_edits ADD COLUMN annotations TEXT DEFAULT NULL"
)
_MIGRATE_FRAME = [
    "ALTER TABLE photo_edits ADD COLUMN frame_type        TEXT DEFAULT 'none'",
    "ALTER TABLE photo_edits ADD COLUMN frame_width       REAL DEFAULT 0.05",
    "ALTER TABLE photo_edits ADD COLUMN frame_inner_width REAL DEFAULT 0.015",
    "ALTER TABLE photo_edits ADD COLUMN frame_gap         REAL DEFAULT 0.02",
    "ALTER TABLE photo_edits ADD COLUMN frame_style       TEXT DEFAULT 'solid'",
    "ALTER TABLE photo_edits ADD COLUMN frame_color       TEXT DEFAULT '#f2f2f2'",
    "ALTER TABLE photo_edits ADD COLUMN frame_color2      TEXT DEFAULT '#8c8c8c'",
    "ALTER TABLE photo_edits ADD COLUMN frame_inner_color TEXT DEFAULT '#303030'",
    "ALTER TABLE photo_edits ADD COLUMN frame_gap_color   TEXT DEFAULT '#ffffff'",
    "ALTER TABLE photo_edits ADD COLUMN frame_inner_enabled INTEGER DEFAULT 0",
    "ALTER TABLE photo_edits ADD COLUMN frame_inner_motif    TEXT DEFAULT 'line'",
    "ALTER TABLE photo_edits ADD COLUMN frame_inner_relief   INTEGER DEFAULT 1",
    "ALTER TABLE photo_edits ADD COLUMN frame_inner_ornament REAL DEFAULT 1.0",
]

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
    """Non-destructive SQLite storage of the edits.

    Two tables:
    - ``photo_edits``  : the current state per photo (one row per edited photo).
    - ``edit_history`` : history of the saved states (persistent undo/redo).

    A singleton per database path: main_window.py, photo_viewer.py and
    edit_panel.py each create their own EditDatabase(); without the singleton,
    each has its own threading.Lock() (so the 3 do not exclude one another) and
    reruns the full migration at startup (3x the same cost).
    """

    _instances: "dict[str, EditDatabase]" = {}
    _instances_lock = threading.Lock()

    def __new__(cls, db_path: Path = _DB_PATH):
        key = str(db_path)
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = super().__new__(cls)
                inst._initialized = False
                cls._instances[key] = inst
            return inst

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        if self._initialized:
            return
        self._db_path = db_path
        self._lock = threading.Lock()
        # One SQLite connection per thread, created once then reused (the
        # ThumbnailCache pattern): load() is called on every navigation in the
        # viewer — opening a fresh connection on every arrow key cost more than
        # the query itself.
        self._tls = threading.local()
        # Snapshot of all_edits(), invalidated by any write (cf. _invalidate_cache).
        # Every write goes through this singleton: the snapshot cannot fall out of
        # sync behind the application's back.
        self._all_edits_cache: "dict[str, EditInfo] | None" = None
        self._init_db()
        self._initialized = True

    def _invalidate_cache(self) -> None:
        """To be called in every method that writes into photo_edits, under _lock."""
        self._all_edits_cache = None

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
                pass  # column already present
            for _sql in _MIGRATE_GAMMA_CURVE:
                try:
                    conn.execute(_sql)
                except sqlite3.OperationalError:
                    pass
            for _sql in _MIGRATE_COLOR_CHANNELS:
                try:
                    conn.execute(_sql)
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute(_MIGRATE_RED_EYE)
            except sqlite3.OperationalError:
                pass
            for _sql in _MIGRATE_VIGNETTE:
                try:
                    conn.execute(_sql)
                except sqlite3.OperationalError:
                    pass
            for _sql in _MIGRATE_VIGNETTE_V2:
                try:
                    conn.execute(_sql)
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute(_MIGRATE_ANNOTATIONS)
            except sqlite3.OperationalError:
                pass
            for _sql in _MIGRATE_FRAME:
                try:
                    conn.execute(_sql)
                except sqlite3.OperationalError:
                    pass
            self._migrate_normalize_paths(conn)
            conn.commit()

    def _migrate_normalize_paths(self, conn) -> None:
        """Normalises the path separators in the existing data."""
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
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._tls.conn = conn
        return conn

    # ------------------------------------------------------------------ API publique

    def load(self, photo_path: str) -> EditInfo:
        """Loads the current edit state. Returns a blank EditInfo() if absent."""
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
                return self._edit_from_row(row)
            except Exception as e:
                logger.error(f"Erreur lecture retouches {photo_path}: {e}")
                return EditInfo()

    def all_edits(self) -> dict[str, EditInfo]:
        """Every edit in progress, indexed by normalised path.

        The table only holds the photos actually edited (save() removes the row
        when nothing is modified any more): the dictionary therefore stays
        small, and a single query is enough to feed a whole grid — instead of
        one SELECT per displayed thumbnail.

        The result is memoised and invalidated by the writes: the grid asks for
        it again on every folder/album change, on the UI thread."""
        with self._lock:
            if self._all_edits_cache is not None:
                return dict(self._all_edits_cache)
            try:
                with self._connect() as conn:
                    rows = conn.execute("SELECT * FROM photo_edits").fetchall()
                edits = {row["photo_path"]: self._edit_from_row(row) for row in rows}
            except Exception as e:
                logger.error("Erreur lecture de l'ensemble des retouches : %s", e)
                return {}
            self._all_edits_cache = edits
            return dict(edits)

    @staticmethod
    def _edit_from_row(row) -> EditInfo:
        _curve_pts = row["gamma_curve_points"]
        return EditInfo(
            brightness=row["brightness"],
            contrast=row["contrast"],
            saturation=row["saturation"],
            gamma=row["gamma"],
            gamma_use_curve=bool(row["gamma_use_curve"]) if row["gamma_use_curve"] is not None else False,
            gamma_curve_points=(
                [(float(x), float(y)) for x, y in json.loads(_curve_pts)]
                if _curve_pts else [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
            ),
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
            color_red=row["color_red"] or 0.0,
            color_green=row["color_green"] or 0.0,
            color_blue=row["color_blue"] or 0.0,
            red_eye_regions=(
                [tuple(r) for r in json.loads(row["red_eye_regions"])]
                if row["red_eye_regions"] else []
            ),
            vignette_strength=float(row["vignette_strength"] or 0.0),
            vignette_color=str(row["vignette_color"] or "black"),
            vignette_cx=float(row["vignette_cx"] if row["vignette_cx"] is not None else 0.5),
            vignette_cy=float(row["vignette_cy"] if row["vignette_cy"] is not None else 0.5),
            vignette_rx1=float(row["vignette_rx1"] if row["vignette_rx1"] is not None else 0.4),
            vignette_ry1=float(row["vignette_ry1"] if row["vignette_ry1"] is not None else 0.4),
            vignette_rx2=float(row["vignette_rx2"] if row["vignette_rx2"] is not None else 0.8),
            vignette_ry2=float(row["vignette_ry2"] if row["vignette_ry2"] is not None else 0.8),
            vignette_angle=float(row["vignette_angle"] if row["vignette_angle"] is not None else 0.0),
            annotations=(
                json.loads(row["annotations"]) if row["annotations"] else []
            ),
            frame_type=str(row["frame_type"] or "none"),
            frame_width=float(row["frame_width"] if row["frame_width"] is not None else 0.05),
            frame_inner_width=float(
                row["frame_inner_width"] if row["frame_inner_width"] is not None else 0.015
            ),
            frame_gap=float(row["frame_gap"] if row["frame_gap"] is not None else 0.02),
            frame_style=str(row["frame_style"] or "solid"),
            frame_color=str(row["frame_color"] or "#f2f2f2"),
            frame_color2=str(row["frame_color2"] or "#8c8c8c"),
            frame_inner_color=str(row["frame_inner_color"] or "#303030"),
            frame_gap_color=str(row["frame_gap_color"] or "#ffffff"),
            frame_inner_enabled=bool(row["frame_inner_enabled"]),
            frame_inner_motif=str(row["frame_inner_motif"] or "line"),
            frame_inner_relief=bool(
                row["frame_inner_relief"] if row["frame_inner_relief"] is not None else 1
            ),
            frame_inner_ornament=float(
                row["frame_inner_ornament"]
                if row["frame_inner_ornament"] is not None else 1.0
            ),
        )

    def save(self, photo_path: str, edit: EditInfo, operation: str = "edit") -> bool:
        """Saves the current state and records it in the history.

        Returns False on failure (a locked DB, a full disk, etc.) — the callers
        must check the return value before considering the save as done (e.g.
        before emitting a photo_saved signal)."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            # Invalidated before the write, not after: a failure along the way then
            # leaves the stale snapshot discarded rather than kept.
            self._invalidate_cache()
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
                                 gamma_use_curve, gamma_curve_points,
                                 sharpness, noise_reduction, rotation, straighten,
                                 flip_h, flip_v, crop, bw, bw_red, bw_green, bw_blue,
                                 color_red, color_green, color_blue, red_eye_regions,
                                 vignette_strength, vignette_color,
                                 vignette_cx, vignette_cy,
                                 vignette_rx1, vignette_ry1,
                                 vignette_rx2, vignette_ry2,
                                 vignette_angle, annotations,
                                 frame_type, frame_width, frame_inner_width,
                                 frame_gap, frame_style, frame_color, frame_color2,
                                 frame_inner_color, frame_gap_color,
                                 frame_inner_enabled, frame_inner_motif,
                                 frame_inner_relief, frame_inner_ornament,
                                 modified_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                    CURRENT_TIMESTAMP)
                            """,
                            (
                                photo_path,
                                edit.brightness, edit.contrast, edit.saturation,
                                edit.gamma,
                                int(edit.gamma_use_curve),
                                json.dumps(edit.gamma_curve_points) if edit.gamma_curve_points else None,
                                edit.sharpness, edit.noise_reduction,
                                edit.rotation, edit.straighten,
                                int(edit.flip_h), int(edit.flip_v),
                                json.dumps(list(edit.crop)) if edit.crop else None,
                                int(edit.bw),
                                edit.bw_red, edit.bw_green, edit.bw_blue,
                                edit.color_red, edit.color_green, edit.color_blue,
                                json.dumps([list(r) for r in edit.red_eye_regions]) if edit.red_eye_regions else None,
                                edit.vignette_strength, edit.vignette_color,
                                edit.vignette_cx, edit.vignette_cy,
                                edit.vignette_rx1, edit.vignette_ry1,
                                edit.vignette_rx2, edit.vignette_ry2,
                                edit.vignette_angle,
                                json.dumps(edit.annotations) if edit.annotations else None,
                                edit.frame_type, edit.frame_width, edit.frame_inner_width,
                                edit.frame_gap, edit.frame_style,
                                edit.frame_color, edit.frame_color2,
                                edit.frame_inner_color, edit.frame_gap_color,
                                int(edit.frame_inner_enabled),
                                edit.frame_inner_motif,
                                int(edit.frame_inner_relief),
                                edit.frame_inner_ornament,
                            ),
                        )
                    conn.execute(
                        "INSERT INTO edit_history (photo_path, state_json, operation)"
                        " VALUES (?, ?, ?)",
                        (photo_path, json.dumps(edit.to_dict()), operation),
                    )
                    # Caps the number of history entries per photo
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
                    return True
            except Exception as e:
                logger.error(f"Erreur sauvegarde retouches {photo_path}: {e}")
                return False

    def has_edits(self, photo_path: str) -> bool:
        photo_path = os.path.normpath(photo_path)
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
        """Deletes the current state and the whole history for this photo."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            self._invalidate_cache()
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
        """Propagates a file rename through both tables."""
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        with self._lock:
            self._invalidate_cache()
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

    def push_history(self, photo_path: str, edit: EditInfo, operation: str = "checkpoint") -> None:
        """Inserts a state into edit_history without modifying photo_edits.

        Used to save the PRE-operation state, so that the undo stays possible
        after a restart of the application (cross-session undo).
        """
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO edit_history (photo_path, state_json, operation) VALUES (?, ?, ?)",
                        (photo_path, json.dumps(edit.to_dict()), operation),
                    )
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
                logger.error("Erreur push_history %s: %s", photo_path, e)

    def get_history(self, photo_path: str, limit: int = 20) -> list[tuple]:
        """Returns the previous states from the oldest to the most recent (for a persistent undo).

        Each entry is a ``(EditInfo, operation_name)`` tuple."""
        photo_path = os.path.normpath(photo_path)
        with self._lock:
            try:
                with self._connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT state_json, operation FROM edit_history
                        WHERE photo_path = ?
                        ORDER BY id DESC LIMIT ?
                        """,
                        (photo_path, limit),
                    ).fetchall()
                result = []
                for row in reversed(rows):
                    try:
                        result.append((
                            EditInfo.from_dict(json.loads(row["state_json"])),
                            row["operation"],
                        ))
                    except Exception:
                        pass
                return result
            except Exception as e:
                logger.error(f"Erreur lecture historique {photo_path}: {e}")
                return []
