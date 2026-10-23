"""SQLite persistence for the download queue.

The queue keeps its own table rather than reusing ``downloads``. A queue row
exists *before* its URL has been resolved, so it has no platform or video id to
key on - and the two tables answer different questions: ``downloads`` records
what has been downloaded, ``queue_items`` records what is still to be done.

Rows survive a restart, which is the point: a queue that forgot its pending
work when the application closed would not be a queue. Finished rows are kept
only long enough for the window to show them, then pruned.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from config.settings import Settings
from core.exceptions import DatabaseError

logger = logging.getLogger(__name__)

#: Same reasoning as the download database: the desktop window may be writing
#: history while the queue writes its own table, so wait rather than fail.
SQLITE_BUSY_TIMEOUT_MS = 10_000

#: How long a finished row is kept so the window can still list it.
FINISHED_RETENTION_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_items (
    task_id      TEXT    PRIMARY KEY,
    url          TEXT    NOT NULL,
    quality      TEXT    NOT NULL DEFAULT 'best',
    title        TEXT,
    video_id     TEXT,
    state        TEXT    NOT NULL DEFAULT 'pending',
    -- Orders concurrent writes: a row is only overwritten by one whose revision
    -- is at least as new, so a slow snapshot cannot resurrect an older state.
    state_rev    INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    position     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    file_path    TEXT,
    file_size    INTEGER,
    output_dir   TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_items_state ON queue_items(state);
CREATE INDEX IF NOT EXISTS idx_queue_items_position ON queue_items(position);
"""

#: Columns added after the table first shipped, with the SQL type to add them
#: as. ``CREATE TABLE IF NOT EXISTS`` cannot introduce a column into a table
#: that already exists, so a database written by an earlier build is brought
#: forward here instead of failing on the first query.
_ADDED_COLUMNS: dict[str, str] = {
    "output_dir": "TEXT",
    "video_id": "TEXT",
    "state_rev": "INTEGER NOT NULL DEFAULT 0",
}

#: States that mean "this row still has work to do".
UNFINISHED_STATES = ("pending", "running", "paused")

#: States that mean "this row is finished" - the only ones :meth:`QueueStore.prune`
#: may drop. Deliberately a whitelist rather than "not in UNFINISHED_STATES": a
#: state written by a newer version (or edited by hand) is something this build
#: cannot interpret, and silently deleting it after the retention window would
#: throw away work the user still has. Kept in step with ``QueueState.finished``
#: by tests/test_queue_store.py.
FINISHED_STATES = ("completed", "skipped", "failed", "cancelled")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class QueueRow:
    """One persisted queue entry."""

    task_id: str
    url: str
    quality: str = "best"
    title: str | None = None
    #: The identity the platform will report for this download, when the caller
    #: already knows it (a playlist listing does). It is metadata for the
    #: listing - the queue still deduplicates on the URL, because the parts of a
    #: Bilibili video share one bvid.
    video_id: str | None = None
    state: str = "pending"
    #: Monotonic revision of ``state``. A write whose revision is older than the
    #: stored one is ignored, which is what stops two threads from landing their
    #: snapshots out of order.
    state_rev: int = 0
    attempts: int = 0
    max_attempts: int = 2
    position: int = 0
    error: str | None = None
    file_path: str | None = None
    file_size: int | None = None
    #: Where this task should be saved, when the user picked a folder for it.
    #: ``None`` means the configured default, exactly as before the queue.
    output_dir: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class QueueStore:
    """Thread-safe wrapper around the ``queue_items`` table."""

    def __init__(self, settings: Settings) -> None:
        # Deliberately the same file as the download history: one database for
        # the application, not two to keep in sync.
        self.path: Path = settings.resolve_path(settings.database_path)
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            connection.executescript(SCHEMA)
            self._add_missing_columns(connection)
            connection.commit()
        except sqlite3.Error as exc:  # pragma: no cover - filesystem dependent
            raise DatabaseError("无法初始化队列表", detail=str(exc)) from exc
        self._connection = connection
        logger.debug("队列表就绪：%s", self.path)
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> QueueStore:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        """Bring an older ``queue_items`` table up to the current shape."""

        present = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(queue_items)")
        }
        for name, sql_type in _ADDED_COLUMNS.items():
            if name not in present:
                connection.execute(f"ALTER TABLE queue_items ADD COLUMN {name} {sql_type}")
                logger.info("队列表已补充列：%s", name)

    # -- reads --------------------------------------------------------------
    def load_all(self) -> list[QueueRow]:
        """Every row, oldest position first."""

        connection = self.connect()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM queue_items ORDER BY position ASC, created_at ASC"
            ).fetchall()
        return [self._to_row(row) for row in rows]

    def load_unfinished(self) -> list[QueueRow]:
        """Rows that still have work to do, in queue order."""

        connection = self.connect()
        placeholders = ", ".join("?" for _ in UNFINISHED_STATES)
        with self._lock:
            rows = connection.execute(
                f"SELECT * FROM queue_items WHERE state IN ({placeholders}) "
                "ORDER BY position ASC, created_at ASC",
                UNFINISHED_STATES,
            ).fetchall()
        return [self._to_row(row) for row in rows]

    def has_unfinished_url(self, url: str, *, exclude: str | None = None) -> bool:
        """True when ``url`` is already waiting somewhere in the queue."""

        connection = self.connect()
        placeholders = ", ".join("?" for _ in UNFINISHED_STATES)
        sql = f"SELECT 1 FROM queue_items WHERE url = ? AND state IN ({placeholders})"
        params: list[object] = [url, *UNFINISHED_STATES]
        if exclude is not None:
            sql += " AND task_id != ?"
            params.append(exclude)
        with self._lock:
            return connection.execute(sql, params).fetchone() is not None

    def next_position(self) -> int:
        """One past the highest position in use."""

        connection = self.connect()
        with self._lock:
            row = connection.execute("SELECT MAX(position) AS top FROM queue_items").fetchone()
        top = row["top"] if row is not None else None
        return int(top) + 1 if top is not None else 1

    # -- writes -------------------------------------------------------------
    def upsert(self, row: QueueRow) -> None:
        connection = self.connect()
        now = _iso(datetime.now(UTC))
        payload = (
            row.task_id,
            row.url,
            row.quality,
            row.title,
            row.video_id,
            row.state,
            row.state_rev,
            row.attempts,
            row.max_attempts,
            row.position,
            row.error,
            row.file_path,
            row.file_size,
            row.output_dir,
            _iso(row.created_at) or now,
            _iso(row.updated_at) or now,
        )
        with self._lock:
            try:
                connection.execute(
                    """
                    INSERT INTO queue_items (
                        task_id, url, quality, title, video_id, state, state_rev, attempts,
                        max_attempts, position, error, file_path, file_size,
                        output_dir, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        url          = excluded.url,
                        quality      = excluded.quality,
                        title        = excluded.title,
                        video_id     = excluded.video_id,
                        state        = excluded.state,
                        state_rev    = excluded.state_rev,
                        attempts     = excluded.attempts,
                        max_attempts = excluded.max_attempts,
                        position     = excluded.position,
                        error        = excluded.error,
                        file_path    = excluded.file_path,
                        file_size    = excluded.file_size,
                        output_dir   = excluded.output_dir,
                        updated_at   = excluded.updated_at
                    WHERE excluded.state_rev >= queue_items.state_rev
                    """,
                    payload,
                )
                connection.commit()
            except sqlite3.Error as exc:  # pragma: no cover - filesystem dependent
                raise DatabaseError("写入队列记录失败", detail=str(exc)) from exc

    def delete(self, task_id: str) -> None:
        connection = self.connect()
        with self._lock:
            connection.execute("DELETE FROM queue_items WHERE task_id = ?", (task_id,))
            connection.commit()

    def delete_finished(self) -> int:
        """Drop every row in a *known* terminal state. Returns how many went.

        Same whitelist as :meth:`prune`: "clear finished" must not reach into
        rows this build cannot interpret, which never appeared in the listing
        the user was looking at when they asked to clear it.
        """

        connection = self.connect()
        placeholders = ", ".join("?" for _ in FINISHED_STATES)
        with self._lock:
            cursor = connection.execute(
                f"DELETE FROM queue_items WHERE state IN ({placeholders})",
                FINISHED_STATES,
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    def prune(self, *, days: int = FINISHED_RETENTION_DAYS) -> int:
        """Forget finished rows older than ``days``.

        Only known terminal states are eligible. A row whose state this build
        does not recognise is kept: it may be real pending work written by a
        newer version, and dropping it on a timer would be silent data loss.
        """

        connection = self.connect()
        cutoff = _iso(datetime.now(UTC) - timedelta(days=days))
        placeholders = ", ".join("?" for _ in FINISHED_STATES)
        with self._lock:
            cursor = connection.execute(
                f"DELETE FROM queue_items WHERE state IN ({placeholders}) "
                "AND updated_at < ?",
                (*FINISHED_STATES, cutoff),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    # -- mapping ------------------------------------------------------------
    @staticmethod
    def _to_row(row: sqlite3.Row) -> QueueRow:
        return QueueRow(
            task_id=row["task_id"],
            url=row["url"],
            quality=row["quality"],
            title=row["title"],
            video_id=row["video_id"],
            state=row["state"],
            state_rev=int(row["state_rev"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            position=int(row["position"]),
            error=row["error"],
            file_path=row["file_path"],
            file_size=row["file_size"],
            output_dir=row["output_dir"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
        )
