"""End-to-end orchestration with a stubbed adapter."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core import selection
from core.database import DownloadDatabase
from core.exceptions import (
    DownloadError,
    IntegrityError,
    MergeError,
    OutputLockError,
    TargetFileConflictError,
    TargetFileError,
    VideoDownloaderError,
)
from core.interfaces import PlatformAdapter
from core.models import (
    DownloadPlan,
    DownloadRecord,
    DownloadStatus,
    MediaStream,
    Platform,
    VideoInfo,
)
from core.naming import (
    LEAF_RESERVE,
    MAX_FULL_PATH,
    MIN_STEM,
    build_output_directory,
    claim_output_path,
    release_output_path,
)
from core.registry import PlatformRegistry
from core.service import DownloadService

MEDIA_URL = "https://stub.test/media.mp4"
PAGE_URL = "https://stub.test/video/1"
TITLE = "Stub / Title: *Sample*?"
MERGE_PAGE = "https://merge.test/video/1"
MERGE_VIDEO_URL = "https://merge.test/video.m4s"
MERGE_AUDIO_URL = "https://merge.test/audio.m4s"


class StubAdapter(PlatformAdapter):
    platform = Platform.YOUTUBE
    display_name = "Stub"
    requires_rights_confirmation = False

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        size: int,
        video_id: str = "stub-1",
        title: str = TITLE,
    ) -> None:
        super().__init__(settings, client)
        self._size = size
        self._video_id = video_id
        self._title = title

    def matches(self, url: str) -> bool:
        return url.startswith("https://stub.test/")

    def normalize_url(self, url: str) -> str:
        return url

    async def fetch_info(self, url: str) -> VideoInfo:
        return VideoInfo(
            platform=self.platform,
            video_id=self._video_id,
            url=url,
            title=self._title,
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


class MergeStubAdapter(StubAdapter):
    """Two separate streams, so the plan needs a merge step."""

    display_name = "MergeStub"

    def matches(self, url: str) -> bool:
        return url.startswith("https://merge.test/")

    async def fetch_info(self, url: str) -> VideoInfo:
        return VideoInfo(
            platform=self.platform,
            video_id="merge-1",
            url=url,
            title="Merge Title",
            author="测试作者",
            streams=[
                MediaStream(
                    url=MERGE_VIDEO_URL,
                    quality_label="1080P",
                    height=1080,
                    width=1920,
                    size=self._size,
                    format_id="dash-video",
                ),
                MediaStream(
                    url=MERGE_AUDIO_URL,
                    is_audio=True,
                    quality_label="audio",
                    bandwidth=128_000,
                    format_id="dash-audio",
                    ext="m4a",
                ),
            ],
        )


def test_the_publish_hook_cannot_be_injected_through_a_class_attribute() -> None:
    """Only a subclass may hook the publish step; a stray attribute must not."""

    assert not hasattr(DownloadService, "publish_hook")


class _HookedService(DownloadService):
    """Runs a hook at a fixed step - by overriding a method, not a class attribute."""

    def __init__(
        self,
        settings: Settings,
        registry: PlatformRegistry,
        client: httpx.AsyncClient,
        database: DownloadDatabase,
        hook: Callable[[Path], None] | None = None,
        recover_hook: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__(settings, registry, client, database)
        self._hook = hook
        self._recover_hook = recover_hook

    def _before_publish(self, target: Path) -> None:
        if self._hook is not None:
            self._hook(target)

    def _before_recover(self, target: Path) -> None:
        if self._recover_hook is not None:
            self._recover_hook(target)


@respx.mock
async def test_full_download_writes_file_metadata_and_record(
    settings: Settings, tmp_path: Path
) -> None:
    payload = b"\x00\x00\x00\x20ftypisom" + b"Z" * 5000
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()
    # The folder is named with the date *at download time*; a run that straddles
    # midnight would otherwise compare against tomorrow and fail for a reason
    # that has nothing to do with the code under test.
    before = _today(settings)
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        result = await service.download(PAGE_URL, quality="best")
    after = _today(settings)

    assert result.status is DownloadStatus.COMPLETED
    assert result.video_path is not None
    assert result.video_path.read_bytes() == payload
    assert result.metadata_path is not None and result.metadata_path.exists()

    # Platform / author / date layout, with the raw title preserved but sanitised.
    assert result.video_path.parent.parts[-3:-1] == ("youtube", "测试作者")
    assert result.video_path.parent.name in {before, after}
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
async def test_two_videos_with_the_same_title_do_not_overwrite_each_other(
    settings: Settings, tmp_path: Path
) -> None:
    """Same author + same title + same day is ordinary (Bilibili live replays).

    The destination used to be derived from the sanitised title alone, so the
    second download replaced the first one's bytes while both database rows kept
    pointing at the same file - silent data loss with a "completed" status.
    """

    first_payload = b"\x00\x00\x00\x20ftypisom" + b"A" * 3000
    second_payload = b"\x00\x00\x00\x20ftypisom" + b"B" * 3000
    respx.get(MEDIA_URL).mock(
        side_effect=[
            Response(200, content=first_payload),
            Response(200, content=second_payload),
        ]
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        first_adapter = StubAdapter(settings, client, len(first_payload), video_id="stub-1")
        first_service = DownloadService(
            settings, PlatformRegistry([first_adapter]), client, database
        )
        first = await first_service.download(PAGE_URL, quality="best")

        second_adapter = StubAdapter(settings, client, len(second_payload), video_id="stub-2")
        second_service = DownloadService(
            settings, PlatformRegistry([second_adapter]), client, database
        )
        second = await second_service.download(PAGE_URL, quality="best")

    assert first.video_path != second.video_path
    assert first.video_path.read_bytes() == first_payload
    assert second.video_path.read_bytes() == second_payload

    first_row = database.get(Platform.YOUTUBE, "stub-1")
    second_row = database.get(Platform.YOUTUBE, "stub-2")
    assert first_row is not None and second_row is not None
    assert first_row.file_path != second_row.file_path
    database.close()


@respx.mock
async def test_force_redownload_keeps_the_original_file_name(
    settings: Settings, tmp_path: Path
) -> None:
    """Re-downloading the *same* video must still overwrite its own file.

    Uniquifying blindly would leave "Title (1).mp4" duplicates on every retry.
    """

    payload = b"\x00\x00\x00\x20ftypisom" + b"Q" * 3000
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        adapter = StubAdapter(settings, client, len(payload))
        service = DownloadService(settings, PlatformRegistry([adapter]), client, database)
        first = await service.download(PAGE_URL, quality="best")
        second = await service.download(PAGE_URL, quality="best", force=True)

    assert second.status is DownloadStatus.COMPLETED
    assert first.video_path == second.video_path
    assert not list(first.video_path.parent.glob(f"{first.video_path.stem} (*).mp4"))
    database.close()


@respx.mock
async def test_a_long_title_in_a_deep_folder_still_downloads(
    settings: Settings, tmp_path: Path
) -> None:
    """The whole path has to fit on Windows, not just the file name.

    A 120-character title under a deep output folder used to exceed MAX_PATH
    and fail at write time with ``WinError 206``; the title is now shortened
    so the download still lands on disk.
    """

    payload = b"\x00\x00\x00\x20ftypisom" + b"L" * 3000
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    # Pad only while the folder still leaves room for a file name: CI's
    # tmp_path is far longer than a developer's, and padding blindly put the
    # directory past MAX_FULL_PATH before the download even started.
    limit = MAX_FULL_PATH - 1 - MIN_STEM - LEAF_RESERVE
    base = tmp_path
    for segment in ("a" * 40, "b" * 40):
        # Stop once the folder is deep enough to force a shortened title, and
        # never pad past the point where a file name no longer fits.
        if len(str(base)) + 30 >= 150 or len(str(base)) + 41 > limit:
            break
        base = base / segment
    base.mkdir(parents=True, exist_ok=True)
    settings.output_dir = base

    long_title = "很长的标题" * 30
    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        adapter = StubAdapter(settings, client, len(payload), title=long_title)
        service = DownloadService(settings, PlatformRegistry([adapter]), client, database)
        result = await service.download(PAGE_URL, quality="best")

    assert result.status is DownloadStatus.COMPLETED
    path = result.video_path
    assert path is not None and path.exists()
    assert len(str(path)) + LEAF_RESERVE <= MAX_FULL_PATH
    assert len(path.stem) < 120
    assert not path.with_name(path.name + ".part").exists()
    database.close()


@respx.mock
async def test_an_unusable_lock_directory_stops_before_any_request(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No lock means no safe write: the transfer must not even start."""

    blocker = tmp_path / "blocked-locks"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(blocker))

    payload = b"\x00\x00\x00\x20ftypisom" + b"Z" * 1000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        adapter = StubAdapter(settings, client, len(payload))
        service = DownloadService(settings, PlatformRegistry([adapter]), client, database)
        with pytest.raises(OutputLockError):
            await service.download(PAGE_URL, quality="best")

    assert route.call_count == 0, "锁不可用时不应发起下载请求"
    assert not list(Path(settings.output_dir).rglob("*.mp4"))
    assert not list(Path(settings.output_dir).rglob("*.part"))
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


@respx.mock
async def test_a_destination_conflict_stops_after_one_download(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name stolen mid-download must not be retried into a ``(1)`` copy."""

    payload = b"\x00\x00\x00\x20ftypisom" + b"Y" * 4000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    def _steal(self: Path, target: Path) -> Path:
        # Another program creates the destination while we are renaming.
        Path(target).write_bytes(b"foreign file")
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(Path, "replace", _steal)

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        with pytest.raises(TargetFileConflictError) as excinfo:
            await service.download(PAGE_URL, quality="best")

    assert route.call_count == 1, "冲突后不该再请求媒体"
    assert "创建" in excinfo.value.message
    files = sorted(path.name for path in Path(settings.output_dir).rglob("*.mp4"))
    assert files == ["Stub _ Title_ _Sample_.mp4"], "不应产生 (1) 副本"
    leftovers = [path.name for path in Path(settings.output_dir).rglob("*.part*")]
    assert leftovers == [], "冲突后不应遗留 .part 或 sidecar"
    assert (Path(settings.output_dir) / "youtube").exists()
    foreign = next(Path(settings.output_dir).rglob("*.mp4"))
    assert foreign.read_bytes() == b"foreign file"

    record = database.get(Platform.YOUTUBE, "stub-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


@respx.mock
async def test_a_failed_merge_input_does_not_leave_its_sibling(
    settings: Settings, tmp_path: Path
) -> None:
    """A merge needs both streams; the finished one is dead weight on failure."""

    payload = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=payload))
    respx.get(MERGE_AUDIO_URL).mock(return_value=Response(500))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        with pytest.raises(DownloadError):
            await service.download(MERGE_PAGE, quality="best")

    finals = sorted(
        path.name
        for path in Path(settings.output_dir).rglob("*")
        if path.is_file() and path.suffix in {".mp4", ".m4a", ".webm", ".mp3"}
    )
    assert finals == [], "失败后不应留下已完成或半成品的目标文件"

    record = database.get(Platform.YOUTUBE, "merge-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


class _FakeMerger:
    """Stands in for ffmpeg: writes ``payload`` instead of running a subprocess."""

    available = False

    def __init__(self, payload: bytes | None = None) -> None:
        self.payload = payload

    async def merge(self, video: Path, audio: Path, output: Path, *, container: str) -> None:
        output.write_bytes(self.payload if self.payload is not None else video.read_bytes())


@respx.mock
async def test_a_rejected_merge_product_is_deleted(settings: Settings, tmp_path: Path) -> None:
    """A merged file that fails verification must not sit there looking done."""

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger(b"this is not a media file")
        with pytest.raises(IntegrityError) as excinfo:
            await service.download(MERGE_PAGE, quality="best")

    assert "媒体" in excinfo.value.message
    leftovers = sorted(
        path.name
        for path in Path(settings.output_dir).rglob("*")
        if path.is_file() and path.suffix in {".mp4", ".m4a", ".webm", ".mp3"}
    )
    assert leftovers == [], "无效产物不应留在下载目录"

    record = database.get(Platform.YOUTUBE, "merge-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


@respx.mock
async def test_a_verified_merge_keeps_the_product(settings: Settings, tmp_path: Path) -> None:
    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        result = await service.download(MERGE_PAGE, quality="best")

    assert result.status is DownloadStatus.COMPLETED
    assert result.video_path.exists()
    assert not list(Path(settings.output_dir).rglob("*.video.*"))
    assert not list(Path(settings.output_dir).rglob("*.audio.*"))
    database.close()


@respx.mock
async def test_a_rejected_merge_restores_the_old_file(
    settings: Settings, tmp_path: Path
) -> None:
    """The merge path keeps its backup as a copy, not a link - it must restore too."""

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        first = await service.download(MERGE_PAGE, quality="best")
        path = first.video_path
        old = path.read_bytes()

        service.merger = _FakeMerger(b"this is not a media file")
        with pytest.raises(IntegrityError):
            await service.download(MERGE_PAGE, quality="best", force=True)

    assert path.read_bytes() == old, "合并路径的备份是副本，也必须能恢复"
    assert not list(path.parent.glob("*.previous*"))
    assert not list(path.parent.glob("*.swap*"))
    assert not list(path.parent.glob("*.merge.*"))
    record = database.get(Platform.YOUTUBE, "merge-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


@respx.mock
async def test_a_rejected_redownload_restores_the_old_file(
    settings: Settings, tmp_path: Path
) -> None:
    """The user's previous copy must survive a failed replacement."""

    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        existing = first.video_path

        route.mock(return_value=Response(200, content=b"this is not a media file"))
        with pytest.raises(IntegrityError):
            await service.download(PAGE_URL, quality="best", force=True)

    assert existing.read_bytes() == old, "校验失败后必须恢复原文件内容"
    assert not list(existing.parent.glob("*.previous*"))
    database.close()


@respx.mock
async def test_a_forced_redownload_replaces_the_old_file(
    settings: Settings, tmp_path: Path
) -> None:
    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    new = b"\x00\x00\x00\x20ftypisom" + b"N" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        route.mock(return_value=Response(200, content=new))
        second = await service.download(PAGE_URL, quality="best", force=True)

    assert second.video_path == path, "强制重下应保留原文件名"
    assert path.read_bytes() == new
    assert not list(path.parent.glob("*.previous*")), "成功后备份必须删除"
    database.close()


@respx.mock
async def test_a_target_replaced_during_recovery_is_not_overwritten(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that changed hands while verifying belongs to somebody else."""

    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        route.mock(return_value=Response(200, content=b"this is not a media file"))

        async def _verify_and_lose_the_file(_path: Path, _info: VideoInfo) -> None:
            _path.write_bytes(b"third party took this name")
            raise IntegrityError("模拟校验失败")

        monkeypatch.setattr(service, "_verify", _verify_and_lose_the_file)
        with pytest.raises(IntegrityError):
            await service.download(PAGE_URL, quality="best", force=True)

    assert path.read_bytes() == b"third party took this name"
    backups = list(path.parent.glob("*.previous*"))
    assert backups, "恢复不安全时必须保留备份"
    assert backups[0].read_bytes() == old
    database.close()


@respx.mock
async def test_a_failed_backup_cleanup_is_reported(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    new = b"\x00\x00\x00\x20ftypisom" + b"N" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        original_unlink = Path.unlink

        def _locked_unlink(self: Path, missing_ok: bool = False) -> None:
            if ".previous" in self.name:
                raise PermissionError(32, "另一个程序正在使用此文件")
            original_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _locked_unlink)
        route.mock(return_value=Response(200, content=new))
        with caplog.at_level(logging.WARNING):
            second = await service.download(PAGE_URL, quality="best", force=True)

    assert second.status is DownloadStatus.COMPLETED
    assert path.read_bytes() == new
    assert any("备份" in record.message for record in caplog.records)
    database.close()


@respx.mock
async def test_a_download_stops_when_the_old_file_cannot_be_backed_up(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing without a recoverable copy would risk the user's data."""

    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        def _no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError("不支持硬链接")

        def _no_copy(*_args: object, **_kwargs: object) -> None:
            raise OSError("磁盘已满")

        monkeypatch.setattr(os, "link", _no_links)
        monkeypatch.setattr(shutil, "copyfileobj", _no_copy)
        route.mock(return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"N" * 10))
        with pytest.raises(TargetFileError) as excinfo:
            await service.download(PAGE_URL, quality="best", force=True)

    assert "备份" in excinfo.value.message
    assert path.read_bytes() == old, "无法备份时不得覆盖原文件"
    assert not list(path.parent.glob("*.previous*"))
    database.close()


@respx.mock
async def test_a_failed_cleanup_is_logged_and_the_error_survives(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=b"this is not a media file"))
    database = DownloadDatabase(settings)
    database.connect()
    original_unlink = Path.unlink

    def _locked_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".mp4":
            raise PermissionError(32, "另一个程序正在使用此文件")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _locked_unlink)

    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, 100)])
        service = DownloadService(settings, registry, client, database)
        with caplog.at_level(logging.WARNING), pytest.raises(IntegrityError):
            await service.download(PAGE_URL, quality="best")

    assert any("无法删除无效产物" in record.message for record in caplog.records)
    database.close()


class _FailingMerger:
    """Writes some bytes, then fails the way ffmpeg does."""

    available = False

    def __init__(self, partial: bytes = b"half merged") -> None:
        self.partial = partial

    async def merge(self, video: Path, audio: Path, output: Path, *, container: str) -> None:
        output.write_bytes(self.partial)
        raise MergeError("ffmpeg 合并失败")


@respx.mock
async def test_a_failed_merge_leaves_the_old_file_intact(
    settings: Settings, tmp_path: Path
) -> None:
    """ffmpeg must never write over the file a re-download is preserving."""

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        first = await service.download(MERGE_PAGE, quality="best")
        path = first.video_path
        old = path.read_bytes()

        service.merger = _FailingMerger()
        with pytest.raises(MergeError):
            await service.download(MERGE_PAGE, quality="best", force=True)

    assert path.read_bytes() == old, "合并失败不得损坏原有文件"
    assert not list(path.parent.glob("*.previous*")), "原文件未被动过，备份应清理"
    assert not list(path.parent.glob("*.merge.*")), "合并临时文件必须清理"
    record = database.get(Platform.YOUTUBE, "merge-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


@respx.mock
async def test_a_target_replaced_during_the_merge_is_not_overwritten(
    settings: Settings, tmp_path: Path
) -> None:
    """A third party that took the name during the merge keeps its file."""

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        first = await service.download(MERGE_PAGE, quality="best")
        path = first.video_path
        old = path.read_bytes()

        class _StealingMerger(_FakeMerger):
            async def merge(
                self, video: Path, audio: Path, output: Path, *, container: str
            ) -> None:
                output.write_bytes(b"merged bytes")
                path.write_bytes(b"third party took this name")

        service.merger = _StealingMerger()
        with pytest.raises(TargetFileConflictError):
            await service.download(MERGE_PAGE, quality="best", force=True)

    assert path.read_bytes() == b"third party took this name"
    backups = list(path.parent.glob("*.previous*"))
    assert backups and backups[0].read_bytes() == old
    assert not list(path.parent.glob("*.merge.*"))
    database.close()


@respx.mock
async def test_a_failed_restore_keeps_the_backup(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the old file cannot be put back, the copy must survive."""

    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        original_link = os.link

        def _refuse_restore(source: object, target: object) -> None:
            if ".previous" in str(source):
                raise PermissionError(32, "备份文件被占用")
            original_link(source, target)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "link", _refuse_restore)
        route.mock(return_value=Response(200, content=b"this is not a media file"))
        with caplog.at_level(logging.WARNING), pytest.raises(IntegrityError):
            await service.download(PAGE_URL, quality="best", force=True)

    backups = list(path.parent.glob("*.previous*"))
    assert backups and backups[0].read_bytes() == old, "无法恢复时必须保留备份"
    assert any("无法恢复" in record.message for record in caplog.records)
    database.close()


@respx.mock
async def test_a_target_created_before_publish_is_not_overwritten(
    settings: Settings, tmp_path: Path
) -> None:
    """The exact race window: the name is taken after inspection, before publish.

    Deterministic - the hook runs inside ``_publish_staging``, not on a timer.
    """

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = _HookedService(
            settings,
            registry,
            client,
            database,
            hook=lambda target: target.write_bytes(b"third party file"),
        )
        service.merger = _FakeMerger()
        with pytest.raises(TargetFileConflictError):
            await service.download(MERGE_PAGE, quality="best")

    target = next(Path(settings.output_dir).rglob("Merge Title.mp4"))
    assert target.read_bytes() == b"third party file"
    assert not list(target.parent.glob("*.merge.*"))
    record = database.get(Platform.YOUTUBE, "merge-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


@respx.mock
async def test_a_target_taken_while_publishing_a_replacement_keeps_both_files(
    settings: Settings, tmp_path: Path
) -> None:
    """A forced re-download that loses the name must not eat either file."""

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        first = await service.download(MERGE_PAGE, quality="best")
        path = first.video_path
        old = path.read_bytes()

        hooked = _HookedService(
            settings,
            registry,
            client,
            database,
            hook=lambda target: target.write_bytes(b"third party file"),
        )
        hooked.merger = _FakeMerger()
        with pytest.raises(TargetFileConflictError):
            await hooked.download(MERGE_PAGE, quality="best", force=True)

    assert path.read_bytes() == b"third party file", "第三方文件不得被覆盖"
    backups = list(path.parent.glob("*.previous*"))
    assert backups and backups[0].read_bytes() == old, "原文件必须留在备份里"
    assert not list(path.parent.glob("*.swap*"))
    assert not list(path.parent.glob("*.merge.*"))
    database.close()


@respx.mock
async def test_a_filesystem_without_hard_links_fails_safely(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No atomic publish primitive means no publish - never a racy replace."""

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    def _no_links(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EINVAL, "该文件系统不支持硬链接")

    monkeypatch.setattr(os, "link", _no_links)

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        with pytest.raises(TargetFileError) as excinfo:
            await service.download(MERGE_PAGE, quality="best")

    assert "不支持硬链接" in excinfo.value.message
    assert not list(Path(settings.output_dir).rglob("Merge Title.mp4")), "不得发布"
    assert list(Path(settings.output_dir).rglob("*.merge.*")), "合并结果必须保留"
    record = database.get(Platform.YOUTUBE, "merge-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


@respx.mock
async def test_a_permission_error_is_not_mistaken_for_a_missing_feature(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    def _denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "拒绝访问")

    monkeypatch.setattr(os, "link", _denied)

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        with pytest.raises(TargetFileError) as excinfo:
            await service.download(MERGE_PAGE, quality="best")

    assert "无法安全发布" in excinfo.value.message
    assert "不支持硬链接" not in excinfo.value.message
    assert list(Path(settings.output_dir).rglob("*.merge.*")), "合并结果必须保留"
    database.close()


async def test_the_old_file_returns_when_the_name_frees_up(
    settings: Settings, tmp_path: Path
) -> None:
    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, 100)])
        service = DownloadService(settings, registry, client, database)
        target = tmp_path / "video.mp4"
        backup = tmp_path / "video.mp4.previous"
        backup.write_bytes(b"old bytes")

        service._restore_backup_if_name_free(target, backup)

    assert target.read_bytes() == b"old bytes"
    assert not backup.exists(), "恢复成功后备份已用掉"
    database.close()


async def test_the_old_file_stays_in_the_backup_while_the_name_is_taken(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, 100)])
        service = DownloadService(settings, registry, client, database)
        target = tmp_path / "video.mp4"
        target.write_bytes(b"third party file")
        backup = tmp_path / "video.mp4.previous"
        backup.write_bytes(b"old bytes")

        with caplog.at_level(logging.WARNING):
            service._restore_backup_if_name_free(target, backup)

    assert target.read_bytes() == b"third party file", "绝不覆盖第三方文件"
    assert backup.read_bytes() == b"old bytes"
    assert any("原文件保留在" in record.message for record in caplog.records)
    database.close()


@respx.mock
async def test_a_database_failure_still_releases_the_claimed_name(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure before the download starts must not keep the name claimed."""

    payload = b"\x00\x00\x00\x20ftypisom" + b"D" * 2000
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()

    def _boom(_record: DownloadRecord) -> None:
        raise sqlite3.OperationalError("database is locked")

    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        monkeypatch.setattr(database, "upsert", _boom)
        with pytest.raises(sqlite3.OperationalError):
            await service.download(PAGE_URL, quality="best")

    target_dir = build_output_directory(
        settings.resolve_path(settings.output_dir),
        platform=Platform.YOUTUBE,
        author="测试作者",
        publish_time=None,
        template=settings.dir_template,
    )
    again = claim_output_path(target_dir, "Stub _ Title_ _Sample_", "mp4")
    try:
        assert again.name == "Stub _ Title_ _Sample_.mp4", "锁未释放，重试会拿到 (1)"
    finally:
        release_output_path(again)
    database.close()


@respx.mock
async def test_a_metadata_write_failure_is_recorded(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain OSError must not leave the row at DOWNLOADING for ever."""

    payload = b"\x00\x00\x00\x20ftypisom" + b"M" * 2000
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()

    def _full_disk(_payload: object, _destination: object) -> None:
        raise OSError(errno.ENOSPC, "磁盘已满")

    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        monkeypatch.setattr("core.service.metadata_store.write_metadata", _full_disk)
        with pytest.raises(VideoDownloaderError) as excinfo:
            await service.download(PAGE_URL, quality="best")

    assert "写入输出文件失败" in excinfo.value.message
    assert excinfo.value.retryable is False, "磁盘满不该被反复重试"
    record = database.get(Platform.YOUTUBE, "stub-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED, "不得把失败写成成功或停在 DOWNLOADING"
    assert record.error_message and "磁盘已满" in record.error_message
    # The verified product stays: the download itself succeeded.
    assert list(Path(settings.output_dir).rglob("*.mp4"))

    target_dir = build_output_directory(
        settings.resolve_path(settings.output_dir),
        platform=Platform.YOUTUBE,
        author="测试作者",
        publish_time=None,
        template=settings.dir_template,
    )
    again = claim_output_path(target_dir, "Stub _ Title_ _Sample_", "mp4")
    try:
        assert again.name == "Stub _ Title_ _Sample_ (1).mp4", "产物占着名字，下一个名字才对"
    finally:
        release_output_path(again)
    database.close()


@respx.mock
async def test_a_verify_io_failure_restores_the_old_file(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read error while verifying is a failure, not a silent success."""

    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        route.mock(return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"N" * 64))

        async def _broken_verify(_path: Path, _info: VideoInfo) -> None:
            raise OSError(errno.EIO, "读取失败")

        monkeypatch.setattr(service, "_verify", _broken_verify)
        with pytest.raises(VideoDownloaderError) as excinfo:
            await service.download(PAGE_URL, quality="best", force=True)

    assert "写入输出文件失败" in excinfo.value.message
    assert path.read_bytes() == old, "校验未通过时旧文件必须恢复"
    assert not list(path.parent.glob("*.previous*"))
    record = database.get(Platform.YOUTUBE, "stub-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


@respx.mock
async def test_a_restore_blocked_by_a_newcomer_keeps_the_backup(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If somebody takes the name during recovery, their file stays."""

    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        route.mock(return_value=Response(200, content=b"this is not a media file"))
        original_link = os.link

        def _steal_the_name(source: object, target: object) -> None:
            if ".previous" in str(source):
                Path(str(target)).write_bytes(b"a newcomer arrived")
                raise FileExistsError(17, "目标已存在")
            original_link(source, target)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "link", _steal_the_name)
        with pytest.raises(IntegrityError):
            await service.download(PAGE_URL, quality="best", force=True)

    assert path.read_bytes() == b"a newcomer arrived", "绝不覆盖第三方文件"
    backups = list(path.parent.glob("*.previous*"))
    assert backups and backups[0].read_bytes() == old, "备份必须保留"
    database.close()


@respx.mock
async def test_a_displaced_file_blocked_by_a_newcomer_is_kept(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish-time restore must not clobber a file that took the name."""

    media = b"\x00\x00\x00\x20ftypisom" + b"V" * 3000
    respx.get(MERGE_VIDEO_URL).mock(return_value=Response(200, content=media))
    respx.get(MERGE_AUDIO_URL).mock(
        return_value=Response(200, content=b"\x00\x00\x00\x20ftypisom" + b"A" * 1000)
    )

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([MergeStubAdapter(settings, client, len(media))])
        service = DownloadService(settings, registry, client, database)
        service.merger = _FakeMerger()
        first = await service.download(MERGE_PAGE, quality="best")
        path = first.video_path

        original_replace = os.replace

        def _swap_then_steal(source: object, target: object) -> None:
            if str(target).endswith(".swap"):
                # The file we displace is not ours any more, and by the time we
                # try to put it back the name has been taken again.
                Path(str(source)).write_bytes(b"third party took over")
                original_replace(source, target)  # type: ignore[arg-type]
                Path(str(source)).write_bytes(b"a newcomer arrived")
                return
            original_replace(source, target)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", _swap_then_steal)
        with pytest.raises(TargetFileConflictError):
            await service.download(MERGE_PAGE, quality="best", force=True)

    assert path.read_bytes() == b"a newcomer arrived", "绝不覆盖第三方文件"
    swaps = list(path.parent.glob("*.swap*"))
    assert swaps and swaps[0].read_bytes() == b"third party took over", "被移开的文件必须保留"
    database.close()


@respx.mock
async def test_a_transient_io_error_stays_retryable(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the errors that cannot change are made permanent."""

    payload = b"\x00\x00\x00\x20ftypisom" + b"T" * 2000
    respx.get(MEDIA_URL).mock(return_value=Response(200, content=payload))

    database = DownloadDatabase(settings)
    database.connect()

    def _io_error(_payload: object, _destination: object) -> None:
        raise OSError(errno.EIO, "读取失败")

    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(payload))])
        service = DownloadService(settings, registry, client, database)
        monkeypatch.setattr("core.service.metadata_store.write_metadata", _io_error)
        with pytest.raises(VideoDownloaderError) as excinfo:
            await service.download(PAGE_URL, quality="best")

    assert excinfo.value.retryable is True, "可能瞬态的 I/O 错误保留有限重试"
    database.close()


@respx.mock
async def test_a_target_replaced_before_recovery_is_put_back(
    settings: Settings, tmp_path: Path
) -> None:
    """A file that slips in before the rollback must end up where it was."""

    old = b"\x00\x00\x00\x20ftypisom" + b"O" * 3000
    route = respx.get(MEDIA_URL).mock(return_value=Response(200, content=old))

    database = DownloadDatabase(settings)
    database.connect()
    async with httpx.AsyncClient() as client:
        registry = PlatformRegistry([StubAdapter(settings, client, len(old))])
        service = DownloadService(settings, registry, client, database)
        first = await service.download(PAGE_URL, quality="best")
        path = first.video_path

        route.mock(return_value=Response(200, content=b"this is not a media file"))
        hooked = _HookedService(
            settings,
            registry,
            client,
            database,
            recover_hook=lambda target: target.write_bytes(b"third party took over"),
        )
        with pytest.raises(IntegrityError):
            await hooked.download(PAGE_URL, quality="best", force=True)

    assert path.read_bytes() == b"third party took over", "第三方文件必须回到原名"
    backups = list(path.parent.glob("*.previous*"))
    assert backups and backups[0].read_bytes() == old, "备份必须保留"
    assert not list(path.parent.glob("*.swap*")), "第三方文件不得留在 .swap"
    record = database.get(Platform.YOUTUBE, "stub-1")
    assert record is not None
    assert record.status is DownloadStatus.FAILED
    database.close()


def _today(settings: Settings) -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")
