"""End-to-end orchestration with a stubbed adapter."""

from __future__ import annotations

import contextlib
from pathlib import Path

import httpx
import respx
from httpx import Response

from config.settings import Settings
from core import selection
from core.database import DownloadDatabase
from core.interfaces import PlatformAdapter
from core.models import DownloadPlan, DownloadStatus, MediaStream, Platform, VideoInfo
from core.registry import PlatformRegistry
from core.service import DownloadService

MEDIA_URL = "https://stub.test/media.mp4"
PAGE_URL = "https://stub.test/video/1"
TITLE = "Stub / Title: *Sample*?"


class StubAdapter(PlatformAdapter):
    platform = Platform.YOUTUBE
    display_name = "Stub"
    requires_rights_confirmation = False

    def __init__(self, settings: Settings, client: httpx.AsyncClient, size: int) -> None:
        super().__init__(settings, client)
        self._size = size

    def matches(self, url: str) -> bool:
        return url.startswith("https://stub.test/")

    def normalize_url(self, url: str) -> str:
        return url

    async def fetch_info(self, url: str) -> VideoInfo:
        return VideoInfo(
            platform=self.platform,
            video_id="stub-1",
            url=url,
            title=TITLE,
            author="测试作者",
            streams=[
                MediaStream(
                    url=MEDIA_URL,
                    quality_label="480p",
                    height=480,
                    width=852,
                    size=self._size,
                    format_id="progressive",
                )
            ],
            extra={"engine": "stub"},
        )

    def select_streams(self, info: VideoInfo, quality: str) -> DownloadPlan:
        return selection.select_streams(info, quality)


@respx.mock
async def test_full_download_writes_file_metadata_and_record(
    settings: Settings, tmp_path: Path
) -> None:
    payload = b"\x00\x00\x00\x20ftypisom" + b"Z" * 5000
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        result = await service.download(PAGE_URL, quality="best")

    assert result.status is DownloadStatus.COMPLETED
    assert result.video_path is not None
    assert result.video_path.read_bytes() == payload
    assert result.metadata_path is not None and result.metadata_path.exists()

    # Platform / author / date layout, with the raw title preserved but sanitised.
    assert result.video_path.parent.parts[-3:] == ("youtube", "测试作者", _today(settings))
    assert result.video_path.parent.name == _today(settings)
    assert "/" not in result.video_path.stem
    assert result.video_path.stem.startswith("Stub")

    record = database.get(Platform.YOUTUBE, "stub-1")
    assert record is not None
    assert record.status is DownloadStatus.COMPLETED
    assert record.file_size == len(payload)
    assert record.resolution == "852x480"
    database.close()


@respx.mock
async def test_second_run_is_skipped(settings: Settings, tmp_path: Path) -> None:
    payload = b"\x00\x00\x00\x20ftypisom" + b"Y" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        calls_after_first = route.call_count
        second = await service.download(PAGE_URL, quality="best")

    assert first.status is DownloadStatus.COMPLETED
    assert second.status is DownloadStatus.SKIPPED
    assert second.skipped_reason is not None
    # No additional network traffic for the already-downloaded video.
    assert route.call_count == calls_after_first
    database.close()


@respx.mock
async def test_force_redownloads(settings: Settings, tmp_path: Path) -> None:
    payload = b"\x00\x00\x00\x20ftypisom" + b"W" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        await service.download(PAGE_URL, quality="best")
        route.reset()
        second = await service.download(PAGE_URL, quality="best", force=True)

    assert second.status is DownloadStatus.COMPLETED
    assert route.call_count == 1
    database.close()


@respx.mock
async def test_failure_is_recorded(settings: Settings, tmp_path: Path) -> None:
    payload = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MEDIA_URL).mock(return_value=Response(403, content=b"nope"))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        with contextlib.suppress(Exception):
            await service.download(PAGE_URL, quality="best")

    record = database.get(Platform.YOUTUBE, "stub-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    assert record.error_message
    database.close()


def _today(settings: Settings) -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")
