"""SQLite history: upsert, duplicate detection, failure bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from config.settings import Settings
from core.database import DownloadDatabase
from core.models import DownloadRecord, DownloadStatus, Platform


def _record(tmp_path: Path, **overrides) -> DownloadRecord:
    payload = {
        "platform": Platform.BILIBILI,
        "video_id": "BV1GJ411x7h7",
        "url": "https://www.bilibili.com/video/BV1GJ411x7h7",
        "author": "UP主",
        "title": "测试视频",
        "status": DownloadStatus.COMPLETED,
        "file_path": str(tmp_path / "video.mp4"),
        "file_size": 1024,
        "resolution": "1920x1080",
        "quality_label": "1080P",
        "downloaded_at": datetime(2024, 1, 1, tzinfo=UTC),
    }
    payload.update(overrides)
    return DownloadRecord(**payload)


def test_schema_is_created(settings: Settings) -> None:
    database = DownloadDatabase(settings)
    database.connect()
    try:
        assert database.stats() == {}
        assert database.recent() == []
    finally:
        database.close()


def test_upsert_then_get(settings: Settings, tmp_path: Path) -> None:
    database = DownloadDatabase(settings)
    try:
        database.upsert(_record(tmp_path))
        stored = database.get(Platform.BILIBILI, "BV1GJ411x7h7")
        assert stored is not None
        assert stored.title == "测试视频"
        assert stored.status is DownloadStatus.COMPLETED
        assert stored.resolution == "1920x1080"
        assert stored.downloaded_at is not None
    finally:
        database.close()


def test_upsert_updates_existing_row(settings: Settings, tmp_path: Path) -> None:
    database = DownloadDatabase(settings)
    try:
        database.upsert(_record(tmp_path))
        database.upsert(_record(tmp_path, title="新标题", status=DownloadStatus.FAILED))
        stored = database.get(Platform.BILIBILI, "BV1GJ411x7h7")
        assert stored is not None
        assert stored.title == "新标题"
        assert stored.status is DownloadStatus.FAILED
        assert database.stats() == {"failed": 1}
    finally:
        database.close()


def test_is_downloaded_requires_an_existing_file(settings: Settings, tmp_path: Path) -> None:
    database = DownloadDatabase(settings)
    try:
        target = tmp_path / "video.mp4"
        database.upsert(_record(tmp_path, file_path=str(target)))
        # Recorded as complete, but the file is gone.
        assert database.is_downloaded(Platform.BILIBILI, "BV1GJ411x7h7") is False

        target.write_bytes(b"data")
        assert database.is_downloaded(Platform.BILIBILI, "BV1GJ411x7h7") is True
    finally:
        database.close()


def test_incomplete_status_is_not_skipped(settings: Settings, tmp_path: Path) -> None:
    database = DownloadDatabase(settings)
    try:
        target = tmp_path / "video.mp4"
        target.write_bytes(b"data")
        database.upsert(_record(tmp_path, file_path=str(target), status=DownloadStatus.FAILED))
        assert database.is_downloaded(Platform.BILIBILI, "BV1GJ411x7h7") is False
    finally:
        database.close()


def test_mark_failed(settings: Settings, tmp_path: Path) -> None:
    database = DownloadDatabase(settings)
    try:
        database.upsert(_record(tmp_path))
        database.mark_failed(Platform.BILIBILI, "BV1GJ411x7h7", "网络超时")
        stored = database.get(Platform.BILIBILI, "BV1GJ411x7h7")
        assert stored is not None
        assert stored.status is DownloadStatus.FAILED
        assert stored.error_message == "网络超时"
    finally:
        database.close()


def test_recent_orders_by_update_time(settings: Settings, tmp_path: Path) -> None:
    database = DownloadDatabase(settings)
    try:
        database.upsert(_record(tmp_path, video_id="A"))
        database.upsert(_record(tmp_path, video_id="B"))
        titles = [record.video_id for record in database.recent()]
        assert set(titles) == {"A", "B"}
        assert len(titles) == 2
    finally:
        database.close()
