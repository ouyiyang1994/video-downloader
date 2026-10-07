"""SQLite download history."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from config.settings import Settings
from core.exceptions import DatabaseError
from core.models import DownloadRecord, DownloadStatus, Platform

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT    NOT NULL,
    video_id      TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    author        TEXT,
    title         TEXT    NOT NULL DEFAULT '',
    publish_time  TEXT,
    file_path     TEXT,
    file_size     INTEGER,
    resolution    TEXT,
    quality_label TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending',
    error_message TEXT,
    downloaded_at TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE(platform, video_id)
);

CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_platform ON downloads(platform);
"""


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


class DownloadDatabase:
    """Thread-safe wrapper around a single SQLite file."""

    def __init__(self, settings: Settings) -> None:
        self.path = settings.resolve_path(settings.database_path)
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
            connection.executescript(SCHEMA)
            connection.commit()
        except sqlite3.Error as exc:  # pragma: no cover - filesystem dependent
            raise DatabaseError("无法初始化数据库", detail=str(exc)) from exc
        self._connection = connection
        logger.debug("SQLite 就绪：%s", self.path)
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> DownloadDatabase:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- queries ------------------------------------------------------------
    def get(self, platform: Platform, video_id: str) -> DownloadRecord | None:
        connection = self.connect()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM downloads WHERE platform = ? AND video_id = ?",
                (platform.value, video_id),
            ).fetchone()
        return self._to_record(row) if row else None

    def is_downloaded(self, platform: Platform, video_id: str) -> bool:
        """True only when a previous attempt completed and the file still exists."""

        record = self.get(platform, video_id)
        if record is None or record.status != DownloadStatus.COMPLETED:
            return False
        if not record.file_path:
            return False
        return Path(record.file_path).exists()

    def recent(self, limit: int = 20) -> list[DownloadRecord]:
        connection = self.connect()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM downloads ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def stats(self) -> dict[str, int]:
        connection = self.connect()
        with self._lock:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM downloads GROUP BY status"
            ).fetchall()
        return {row["status"]: row["total"] for row in rows}

    # -- writes -------------------------------------------------------------
    def upsert(self, record: DownloadRecord) -> None:
        connection = self.connect()
        payload = (
            record.platform.value,
            record.video_id,
            record.url,
            record.author,
            record.title,
            _iso(record.publish_time),
            record.file_path,
            record.file_size,
            record.resolution,
            record.quality_label,
            record.status.value,
            record.error_message,
            _iso(record.downloaded_at),
            _iso(record.created_at),
            _iso(record.updated_at or datetime.now(UTC)),
        )
        with self._lock:
            try:
                connection.execute(
                    """
                    INSERT INTO downloads (
                        platform, video_id, url, author, title, publish_time,
                        file_path, file_size, resolution, quality_label, status,
                        error_message, downloaded_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, video_id) DO UPDATE SET
                        url           = excluded.url,
                        author        = excluded.author,
                        title         = excluded.title,
                        publish_time  = excluded.publish_time,
                        file_path     = excluded.file_path,
                        file_size     = excluded.file_size,
                        resolution    = excluded.resolution,
                        quality_label = excluded.quality_label,
                        status        = excluded.status,
                        error_message = excluded.error_message,
                        downloaded_at = excluded.downloaded_at,
                        updated_at    = excluded.updated_at
                    """,
                    payload,
                )
                connection.commit()
            except sqlite3.Error as exc:  # pragma: no cover - filesystem dependent
                raise DatabaseError("写入下载记录失败", detail=str(exc)) from exc

    def mark_failed(self, platform: Platform, video_id: str, error: str) -> None:
        connection = self.connect()
        with self._lock:
            connection.execute(
                """
                UPDATE downloads
                   SET status = ?, error_message = ?, updated_at = ?
                 WHERE platform = ? AND video_id = ?
                """,
                (
                    DownloadStatus.FAILED.value,
                    error[:1000],
                    _iso(datetime.now(UTC)),
                    platform.value,
                    video_id,
                ),
            )
            connection.commit()

    def mark_cancelled(self, platform: Platform, video_id: str) -> None:
        """Record a user-initiated stop (kept distinct from a failure)."""

        connection = self.connect()
        with self._lock:
            connection.execute(
                """
                UPDATE downloads
                   SET status = ?, error_message = NULL, updated_at = ?
                 WHERE platform = ? AND video_id = ?
                """,
                (
                    DownloadStatus.CANCELLED.value,
                    _iso(datetime.now(UTC)),
                    platform.value,
                    video_id,
                ),
            )
            connection.commit()

    # -- mapping ------------------------------------------------------------
    @staticmethod
    def _to_record(row: sqlite3.Row) -> DownloadRecord:
        return DownloadRecord(
            platform=Platform(row["platform"]),
            video_id=row["video_id"],
            url=row["url"],
            author=row["author"],
            title=row["title"] or "",
            publish_time=_parse(row["publish_time"]),
            file_path=row["file_path"],
            file_size=row["file_size"],
            resolution=row["resolution"],
            quality_label=row["quality_label"],
            status=DownloadStatus(row["status"]),
            error_message=row["error_message"],
            downloaded_at=_parse(row["downloaded_at"]),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            updated_at=_parse(row["updated_at"]) or datetime.now(UTC),
        )
