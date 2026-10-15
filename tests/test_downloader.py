"""Download engine: resume, retry, integrity."""

from __future__ import annotations

import errno
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core.downloader import HttpDownloader
from core.exceptions import DownloadError, IntegrityError, TargetFileError
from core.models import MediaStream

STREAM_URL = "https://cdn.example.test/media.m4s"


def _stream(size: int | None = None) -> MediaStream:
    return MediaStream(url=STREAM_URL, quality_label="1080P", height=1080, size=size)


@respx.mock
async def test_plain_download(settings: Settings, tmp_path: Path, fake_media_bytes: bytes) -> None:
    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == fake_media_bytes
    assert outcome.bytes_downloaded == len(fake_media_bytes)
    assert outcome.resumed_from == 0
    assert not destination.with_name(destination.name + ".part").exists()


@respx.mock
async def test_resumes_from_partial_file(settings: Settings, tmp_path: Path) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    head, tail = b"A" * 1000, b"B" * 500
    part.write_bytes(head)

    route = respx.get(STREAM_URL).mock(
        return_value=Response(206, content=tail, headers={"content-length": str(len(tail))})
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert route.calls[0].request.headers["range"] == "bytes=1000-"
    assert destination.read_bytes() == head + tail
    assert outcome.resumed_from == 1000
    assert outcome.bytes_downloaded == 1500


@respx.mock
async def test_server_ignoring_range_restarts_cleanly(settings: Settings, tmp_path: Path) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(b"stale")
    payload = b"C" * 2048

    respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_retries_after_transport_error(settings: Settings, tmp_path: Path) -> None:
    payload = b"D" * 4096
    respx.get(STREAM_URL).mock(
        side_effect=[httpx.ConnectTimeout("boom"), Response(200, content=payload)]
    )
    destination = tmp_path / "media.m4s"

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == payload
    assert outcome.bytes_downloaded == len(payload)


@respx.mock
async def test_exhausted_retries_raise_download_error(settings: Settings, tmp_path: Path) -> None:
    respx.get(STREAM_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    destination = tmp_path / "media.m4s"

    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadError):
            await HttpDownloader(settings, client).download_stream(_stream(), destination)


@respx.mock
async def test_a_locked_destination_is_reported_as_a_download_error(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A player holding the target must not surface as an internal error.

    Renaming ``.part`` onto the destination fails with ``WinError 32`` when
    another process has that file open. A bare ``OSError`` escapes every
    ``VideoDownloaderError`` handler above this layer, so the queue records
    "内部错误" and the user gets no idea which file is in the way.
    """

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    original_replace = Path.replace

    def _locked(self: Path, target: Path) -> Path:
        if Path(target) == destination:
            raise PermissionError(13, "文件正被其他程序使用")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _locked)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileError) as excinfo:
            await HttpDownloader(settings, client).download_stream(_stream(), destination)

    reported = f"{excinfo.value.message}{excinfo.value.detail or ''}"
    assert "media.m4s" in reported, "错误信息必须指出是哪个文件"
    assert "占用" in reported or "使用" in reported
    assert route.call_count == 1, "重命名失败不该重下正文（一次请求足够）"
    assert destination.with_name(destination.name + ".part").read_bytes() == fake_media_bytes, (
        "已经下载好的载荷必须留在 .part 里，供下次只重试重命名"
    )


@respx.mock
async def test_a_full_disk_is_not_described_as_a_locked_file(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different ``OSError``s need different advice; only the errno knows which."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    original_replace = Path.replace

    def _no_space(self: Path, target: Path) -> Path:
        if Path(target) == destination:
            raise OSError(errno.ENOSPC, "No space left on device")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _no_space)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileError) as excinfo:
            await HttpDownloader(settings, client).download_stream(_stream(), destination)

    detail = excinfo.value.detail or ""
    assert "磁盘空间不足" in detail
    assert "占用" not in detail, "磁盘满不该被说成文件被占用"


@respx.mock
async def test_a_complete_partial_file_only_retries_the_rename(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier attempt that only failed to rename must not download again."""

    route = respx.get(STREAM_URL).mock(side_effect=AssertionError("载荷已完整，不该再发请求"))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(fake_media_bytes)
    stream = _stream(size=len(fake_media_bytes))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 0, "完整 .part 的存在说明只需要重命名"
    assert destination.read_bytes() == fake_media_bytes
    assert not part.exists()
    assert outcome.resumed_from == len(fake_media_bytes)


@respx.mock
async def test_a_still_locked_destination_fails_the_rename_without_a_request(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.get(STREAM_URL).mock(side_effect=AssertionError("不该发请求"))
    destination = tmp_path / "media.m4s"
    destination.with_name(destination.name + ".part").write_bytes(fake_media_bytes)
    original_replace = Path.replace

    def _locked(self: Path, target: Path) -> Path:
        if Path(target) == destination:
            raise PermissionError(13, "文件正被其他程序使用")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _locked)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileError):
            await HttpDownloader(settings, client).download_stream(
                _stream(size=len(fake_media_bytes)), destination
            )

    assert route.call_count == 0


@respx.mock
async def test_truncated_body_raises_integrity_error(settings: Settings, tmp_path: Path) -> None:
    respx.get(STREAM_URL).mock(
        return_value=Response(200, content=b"E" * 100, headers={"content-length": "500"})
    )
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")

    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadError) as excinfo:
            await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert isinstance(excinfo.value.__cause__, IntegrityError)
    # The partial file survives so the next run can resume from it.
    assert part.exists()
    assert part.stat().st_size == 100


@respx.mock
async def test_http_error_status(settings: Settings, tmp_path: Path) -> None:
    respx.get(STREAM_URL).mock(return_value=Response(403, content=b"denied"))

    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadError):
            await HttpDownloader(settings, client).download_stream(
                _stream(), tmp_path / "media.m4s"
            )


@respx.mock
async def test_progress_callback_reports_totals(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes
) -> None:
    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    updates = []

    async with httpx.AsyncClient() as client:
        await HttpDownloader(settings, client).download_stream(
            _stream(), tmp_path / "media.m4s", progress=updates.append
        )

    assert updates
    assert updates[-1].downloaded == len(fake_media_bytes)
    assert updates[-1].total == len(fake_media_bytes)
