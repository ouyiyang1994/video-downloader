"""Download engine: resume, retry, integrity."""

from __future__ import annotations

import asyncio
import errno
import json
import threading
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core import downloader as downloader_module
from core.downloader import (
    HttpDownloader,
    completed_outcomes,
    resume_sidecar_path,
    write_resume_sidecar,
)
from core.exceptions import (
    DownloadCancelled,
    DownloadError,
    IntegrityError,
    TargetFileConflictError,
    TargetFileError,
)
from core.models import MediaStream

STREAM_URL = "https://cdn.example.test/media.m4s"
OTHER_URL = "https://cdn.example.test/other.m4s"
AUDIO_URL = "https://cdn.example.test/audio.m4s"


def _stream(
    size: int | None = None,
    *,
    url: str = STREAM_URL,
    quality: str = "1080P",
    format_id: str | None = None,
    is_audio: bool = False,
) -> MediaStream:
    return MediaStream(
        url=url,
        quality_label=quality,
        height=1080,
        size=size,
        format_id=format_id,
        is_audio=is_audio,
        ext="m4a" if is_audio else "mp4",
    )


def _seed_partial(part: Path, stream: MediaStream, content: bytes) -> None:
    """Recreate exactly what an interrupted attempt of ``stream`` leaves behind."""

    part.write_bytes(content)
    write_resume_sidecar(part, stream)


def _fast_rename_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the rename back-off so the tests do not sleep for seconds."""

    monkeypatch.setattr(HttpDownloader, "move_retry_delays", (0.0, 0.0, 0.0))
    monkeypatch.setattr(downloader_module, "_RENAME_DELAY", 0.0)


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
    stream = _stream()
    _seed_partial(part, stream, head)
    total = len(head) + len(tail)

    route = respx.get(STREAM_URL).mock(
        return_value=Response(
            206,
            content=tail,
            headers={
                "content-length": str(len(tail)),
                "content-range": f"bytes {len(head)}-{total - 1}/{total}",
            },
        )
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.calls[0].request.headers["range"] == "bytes=1000-"
    assert destination.read_bytes() == head + tail
    assert outcome.resumed_from == 1000
    assert outcome.bytes_downloaded == 1500
    assert not resume_sidecar_path(part).exists()


@respx.mock
async def test_a_partial_file_from_another_url_is_not_appended(
    settings: Settings, tmp_path: Path
) -> None:
    """A stale ``.part`` for a different resource must never be continued."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stale = b"S" * 1000
    payload = b"F" * 900
    _seed_partial(part, _stream(url=OTHER_URL), stale)

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert "range" not in route.calls[0].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_a_partial_file_from_another_format_is_not_appended(
    settings: Settings, tmp_path: Path
) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    payload = b"G" * 800
    _seed_partial(part, _stream(quality="720P", format_id="30064"), b"T" * 700)

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(
            _stream(quality="1080P", format_id="30080"), destination
        )

    assert "range" not in route.calls[0].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_a_partial_file_without_metadata_is_downloaded_again(
    settings: Settings, tmp_path: Path
) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(b"U" * 1200)  # no sidecar: origin unknown
    payload = b"V" * 600

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert "range" not in route.calls[0].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_corrupt_resume_metadata_restarts_the_download(
    settings: Settings, tmp_path: Path
) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(b"W" * 1200)
    resume_sidecar_path(part).write_text("{not json", encoding="utf-8")
    payload = b"X" * 700

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert "range" not in route.calls[0].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_server_ignoring_range_restarts_cleanly(settings: Settings, tmp_path: Path) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream()
    _seed_partial(part, stream, b"stale")
    payload = b"C" * 2048

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 2, "第一次带 Range 被忽略，必须重新请求完整内容"
    assert route.calls[0].request.headers["range"] == "bytes=5-"
    assert "range" not in route.calls[1].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_a_wrong_content_range_is_not_appended(settings: Settings, tmp_path: Path) -> None:
    """A 206 that starts at the wrong offset must not be appended to the part."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream()
    head = b"H" * 1000
    _seed_partial(part, stream, head)
    payload = b"I" * 900

    route = respx.get(STREAM_URL).mock(
        side_effect=[
            Response(
                206,
                content=b"J" * 400,
                headers={"content-range": "bytes 0-399/1300"},
            ),
            Response(200, content=payload),
        ]
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 2
    assert route.calls[0].request.headers["range"] == "bytes=1000-"
    assert "range" not in route.calls[1].request.headers
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
    _fast_rename_retries(monkeypatch)

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
    stream = _stream(size=len(fake_media_bytes))
    _seed_partial(part, stream, fake_media_bytes)

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 0, "完整 .part 的存在说明只需要重命名"
    assert destination.read_bytes() == fake_media_bytes
    assert not part.exists()
    assert not resume_sidecar_path(part).exists()
    assert outcome.resumed_from == len(fake_media_bytes)


@respx.mock
async def test_a_still_locked_destination_fails_the_rename_without_a_request(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.get(STREAM_URL).mock(side_effect=AssertionError("不该发请求"))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream(size=len(fake_media_bytes))
    _seed_partial(part, stream, fake_media_bytes)
    _fast_rename_retries(monkeypatch)
    original_replace = Path.replace

    def _locked(self: Path, target: Path) -> Path:
        if Path(target) == destination:
            raise PermissionError(13, "文件正被其他程序使用")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _locked)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileError):
            await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 0
    # The payload is still complete on disk, so a later retry can just rename it.
    assert part.exists()
    assert resume_sidecar_path(part).exists()


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


@respx.mock
async def test_a_cancelled_transfer_keeps_a_resumable_partial_file(
    settings: Settings, tmp_path: Path
) -> None:
    """Cancelling must not throw away what the next run could reuse."""

    payload = b"K" * 4000
    head = payload[:1500]
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream()
    _seed_partial(part, stream, head)

    cancel = threading.Event()
    cancel.set()
    respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadCancelled):
            await HttpDownloader(settings, client, cancel).download_stream(stream, destination)

        # Nothing was appended, nothing was deleted, the origin is still known.
        assert part.read_bytes() == head
        assert resume_sidecar_path(part).exists()

        # A later run resumes exactly where the cancelled one stopped.
        respx.get(STREAM_URL).mock(
            return_value=Response(
                206,
                content=payload[len(head):],
                headers={"content-range": f"bytes 1500-{len(payload) - 1}/{len(payload)}"},
            )
        )
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert outcome.resumed_from == len(head)
    assert destination.read_bytes() == payload


@respx.mock
async def test_a_partial_file_larger_than_the_stream_is_discarded(
    settings: Settings, tmp_path: Path
) -> None:
    """More bytes than the stream can hold means the file is not ours."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream(size=500)
    _seed_partial(part, stream, b"P" * 900)
    payload = b"O" * 500

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=payload))

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert "range" not in route.calls[0].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_a_complete_partial_file_answered_with_416_is_renamed(
    settings: Settings, tmp_path: Path
) -> None:
    """416 + ``bytes */N`` matching the file on disk proves completeness."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    payload = b"Q" * 500
    stream = _stream()  # total size unknown to the caller
    _seed_partial(part, stream, payload)

    route = respx.get(STREAM_URL).mock(
        return_value=Response(416, headers={"content-range": f"bytes */{len(payload)}"})
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 1, "文件已完整，不该重新下载"
    assert route.calls[0].request.headers["range"] == f"bytes={len(payload)}-"
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == len(payload)
    assert not part.exists()
    assert not resume_sidecar_path(part).exists()


@respx.mock
async def test_a_partial_file_answered_with_416_is_downloaded_again(
    settings: Settings, tmp_path: Path
) -> None:
    """The server says the resource is longer than the file we hold."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream()
    _seed_partial(part, stream, b"R" * 500)
    payload = b"S" * 1500

    route = respx.get(STREAM_URL).mock(
        side_effect=[
            Response(416, headers={"content-range": "bytes */1500"}),
            Response(200, content=payload),
        ]
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 2
    assert "range" not in route.calls[1].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_a_416_that_disagrees_with_the_known_size_is_not_trusted(
    settings: Settings, tmp_path: Path
) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream(size=1000)
    _seed_partial(part, stream, b"T" * 800)  # 800 != 1000
    payload = b"U" * 1000

    route = respx.get(STREAM_URL).mock(
        side_effect=[
            Response(416, headers={"content-range": "bytes */1000"}),
            Response(200, content=payload),
        ]
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 2
    assert "range" not in route.calls[1].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_a_416_without_content_range_is_not_trusted(
    settings: Settings, tmp_path: Path
) -> None:
    """No statement about the length means no proof of completeness."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream()
    _seed_partial(part, stream, b"V" * 500)
    payload = b"W" * 500

    route = respx.get(STREAM_URL).mock(
        side_effect=[Response(416), Response(200, content=payload)]
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 2
    assert "range" not in route.calls[1].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_a_416_with_an_invalid_content_range_is_not_trusted(
    settings: Settings, tmp_path: Path
) -> None:
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream()
    _seed_partial(part, stream, b"X" * 500)
    payload = b"Y" * 500

    route = respx.get(STREAM_URL).mock(
        side_effect=[
            # A 206-style header has no meaning on a 416 response.
            Response(416, headers={"content-range": "bytes 0-99/100"}),
            Response(200, content=payload),
        ]
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count == 2
    assert "range" not in route.calls[1].request.headers
    assert destination.read_bytes() == payload
    assert outcome.resumed_from == 0


@respx.mock
async def test_repeated_416_without_proof_fails_instead_of_publishing(
    settings: Settings, tmp_path: Path
) -> None:
    """Never rename bytes we cannot vouch for - report the refusal instead."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    stream = _stream()
    _seed_partial(part, stream, b"Z" * 500)

    route = respx.get(STREAM_URL).mock(return_value=Response(416))

    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadError):
            await HttpDownloader(settings, client).download_stream(stream, destination)

    assert route.call_count >= 2
    assert not destination.exists()
    assert not part.exists()
    assert not resume_sidecar_path(part).exists()


@respx.mock
async def test_a_transient_lock_on_the_destination_is_retried(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scanner holding the file for a moment must not lose the download."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    original_replace = Path.replace
    calls = {"n": 0}
    _fast_rename_retries(monkeypatch)

    def _flaky_replace(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace)

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == fake_media_bytes
    assert calls["n"] == 3
    assert outcome.bytes_downloaded == len(fake_media_bytes)


@respx.mock
async def test_a_locked_destination_is_retried_without_another_request(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bytes are already on disk; retrying the move must not refetch them."""

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    original_replace = Path.replace
    failures = {"left": 5}
    _fast_rename_retries(monkeypatch)

    def _flaky_replace(self: Path, target: Path) -> Path:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(32, "另一个程序正在使用此文件")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace)

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert route.call_count == 1, "只重命名，不应重新请求媒体"
    assert destination.read_bytes() == fake_media_bytes
    assert outcome.bytes_downloaded == len(fake_media_bytes)
    assert not part.exists()
    assert not resume_sidecar_path(part).exists()


@respx.mock
async def test_a_destination_created_while_retrying_is_not_overwritten(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Somebody else's file appearing mid-retry must survive untouched."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    calls = {"n": 0}
    original_replace = Path.replace
    _fast_rename_retries(monkeypatch)

    def _blocked_replace(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == 5:
            # Another program wins the race while we are waiting.
            Path(target).write_bytes(b"someone else's file")
        if calls["n"] <= 5:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _blocked_replace)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileConflictError) as excinfo:
            await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == b"someone else's file"
    assert excinfo.value.retryable is False


@respx.mock
async def test_a_part_truncated_while_retrying_is_never_promoted(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial file that changes underneath us is no longer provably whole."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    calls = {"n": 0}
    original_replace = Path.replace
    _fast_rename_retries(monkeypatch)

    def _shrinking_replace(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == 5:
            Path(self).write_bytes(b"short")
        if calls["n"] <= 5:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _shrinking_replace)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileError):
            await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert not destination.exists()
    assert part.read_bytes() == b"short"


@respx.mock
async def test_a_persistently_locked_destination_gives_up_after_bounded_retries(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file held forever must fail - never loop, never overwrite."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    calls = {"n": 0}
    _fast_rename_retries(monkeypatch)

    def _always_locked(self: Path, target: Path) -> Path:
        calls["n"] += 1
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(Path, "replace", _always_locked)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileError) as excinfo:
            await HttpDownloader(settings, client).download_stream(_stream(), destination)

    # A busy file is a lock, not a lost authorisation: still retryable.
    assert not isinstance(excinfo.value, TargetFileConflictError)
    assert excinfo.value.retryable is True
    assert calls["n"] == 7, "4 次内层 + 3 次外层，重试必须有界"
    assert not destination.exists()
    # The verified payload stays on disk so the next run only has to rename it.
    assert part.read_bytes() == fake_media_bytes
    assert resume_sidecar_path(part).exists()


@respx.mock
async def test_a_file_created_during_the_download_is_not_overwritten(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name was free when we started; somebody else took it meanwhile."""

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    _fast_rename_retries(monkeypatch)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileConflictError) as excinfo:
            await HttpDownloader(settings, client).download_stream(
                _stream(),
                destination,
                progress=lambda _update: destination.write_bytes(b"someone else's file"),
            )

    assert destination.read_bytes() == b"someone else's file"
    assert "创建" in excinfo.value.message
    assert excinfo.value.retryable is False
    assert route.call_count == 1, "冲突不该再请求媒体"


@respx.mock
async def test_an_authorised_target_replaced_while_retrying_is_not_overwritten(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced re-download may replace *its* file - not a different one."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    destination.write_bytes(b"the old copy we may replace")
    calls = {"n": 0}
    original_replace = Path.replace
    _fast_rename_retries(monkeypatch)

    def _swapped_replace(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == 5:
            # Another program swaps in a file that is not ours to touch.
            Path(target).write_bytes(b"a different file entirely")
        if calls["n"] <= 5:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _swapped_replace)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileConflictError) as excinfo:
            await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == b"a different file entirely"
    assert "替换" in excinfo.value.message
    assert excinfo.value.retryable is False


@respx.mock
async def test_an_authorised_target_is_still_replaced(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``keep`` semantics are unchanged: the same video's file is overwritten."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    destination.write_bytes(b"old bytes of the same video")
    _fast_rename_retries(monkeypatch)

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == fake_media_bytes
    assert outcome.bytes_downloaded == len(fake_media_bytes)


@respx.mock
async def test_an_authorised_target_that_disappears_is_written_again(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user deleted the old file mid-download; writing it again is fine."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    destination.write_bytes(b"old bytes")
    calls = {"n": 0}
    original_replace = Path.replace
    _fast_rename_retries(monkeypatch)

    def _vanishing_replace(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == 5:
            Path(target).unlink()
        if calls["n"] <= 5:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _vanishing_replace)

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.read_bytes() == fake_media_bytes
    assert outcome.bytes_downloaded == len(fake_media_bytes)


@respx.mock
async def test_a_conflicted_download_removes_its_own_partial(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task ends here, so its ``.part`` and sidecar must not be orphaned."""

    route = respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    _fast_rename_retries(monkeypatch)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileConflictError):
            await HttpDownloader(settings, client).download_stream(
                _stream(),
                destination,
                progress=lambda _update: destination.write_bytes(b"someone else's file"),
            )

    assert destination.read_bytes() == b"someone else's file"
    assert not part.exists()
    assert not resume_sidecar_path(part).exists()
    assert route.call_count == 1


@respx.mock
async def test_a_partial_of_unknown_origin_survives_a_conflict(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting requires proof of ownership; without it the file stays put."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    _fast_rename_retries(monkeypatch)
    monkeypatch.setattr(downloader_module, "read_resume_sidecar", lambda _path: None)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileConflictError):
            await HttpDownloader(settings, client).download_stream(
                _stream(),
                destination,
                progress=lambda _update: destination.write_bytes(b"someone else's file"),
            )

    assert part.exists(), "无法证明归属时不得删除"
    assert resume_sidecar_path(part).exists()


@respx.mock
async def test_a_conflict_leaves_another_tasks_partial_alone(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only this destination's files are candidates for cleanup."""

    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    neighbour = tmp_path / "other.m4s"
    neighbour_part = neighbour.with_name(neighbour.name + ".part")
    _seed_partial(neighbour_part, _stream(url=OTHER_URL), b"other task bytes")
    _fast_rename_retries(monkeypatch)

    async with httpx.AsyncClient() as client:
        with pytest.raises(TargetFileConflictError):
            await HttpDownloader(settings, client).download_stream(
                _stream(),
                destination,
                progress=lambda _update: destination.write_bytes(b"someone else's file"),
            )

    assert neighbour_part.read_bytes() == b"other task bytes"
    assert resume_sidecar_path(neighbour_part).exists()


@respx.mock
async def test_success_removes_the_resume_sidecar(
    settings: Settings, tmp_path: Path, fake_media_bytes: bytes
) -> None:
    respx.get(STREAM_URL).mock(return_value=Response(200, content=fake_media_bytes))
    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")

    async with httpx.AsyncClient() as client:
        await HttpDownloader(settings, client).download_stream(_stream(), destination)

    assert destination.exists()
    assert not part.exists()
    assert not resume_sidecar_path(part).exists()


def test_the_sidecar_never_records_credentials(tmp_path: Path) -> None:
    """Cookies, auth headers and signed query strings must not reach the disk."""

    part = tmp_path / "media.m4s.part"
    stream = MediaStream(
        url=STREAM_URL + "?sign=SUPERSECRET&expires=1",
        quality_label="1080P",
        height=1080,
        headers={"Cookie": "SESSDATA=SUPERSECRET", "Authorization": "Bearer SUPERSECRET"},
    )

    write_resume_sidecar(part, stream)
    text = resume_sidecar_path(part).read_text(encoding="utf-8")

    assert "SUPERSECRET" not in text
    assert "cookie" not in text.lower()
    assert "authorization" not in text.lower()

    payload = json.loads(text)
    assert set(payload) == {"version", "url", "kind", "format_id", "quality_label", "size"}
    assert payload["url"] == STREAM_URL


@respx.mock
async def test_a_refreshed_signed_url_still_resumes(settings: Settings, tmp_path: Path) -> None:
    """CDN links rotate their signature; the partial file stays valid."""

    destination = tmp_path / "media.m4s"
    part = destination.with_name(destination.name + ".part")
    head, tail = b"L" * 1000, b"M" * 400
    _seed_partial(part, _stream(url=STREAM_URL + "?sign=old"), head)
    fresh = _stream(url=STREAM_URL + "?sign=new")

    route = respx.get(STREAM_URL).mock(
        return_value=Response(
            206,
            content=tail,
            headers={"content-range": f"bytes 1000-{1000 + len(tail) - 1}/{1000 + len(tail)}"},
        )
    )

    async with httpx.AsyncClient() as client:
        outcome = await HttpDownloader(settings, client).download_stream(fresh, destination)

    assert route.calls[0].request.headers["range"] == "bytes=1000-"
    assert destination.read_bytes() == head + tail
    assert outcome.resumed_from == 1000


# -- video + audio pairs -----------------------------------------------------


@respx.mock
async def test_a_successful_pair_keeps_both_files(settings: Settings, tmp_path: Path) -> None:
    respx.get(STREAM_URL).mock(return_value=Response(200, content=b"V" * 200))
    respx.get(AUDIO_URL).mock(return_value=Response(200, content=b"A" * 120))
    video = tmp_path / "media.mp4"
    audio = tmp_path / "media.m4a"

    async with httpx.AsyncClient() as client:
        outcomes = await HttpDownloader(settings, client).download_many(
            [(_stream(), video), (_stream(url=AUDIO_URL, is_audio=True), audio)]
        )

    assert len(outcomes) == 2
    assert video.read_bytes() == b"V" * 200
    assert audio.read_bytes() == b"A" * 120


@respx.mock
async def test_a_failing_stream_cancels_its_slow_sibling(
    settings: Settings, tmp_path: Path
) -> None:
    """A sibling left running would keep writing after the task was reported."""

    started = asyncio.Event()
    finished = asyncio.Event()

    async def _slow(_request: httpx.Request) -> Response:
        started.set()
        try:
            await asyncio.sleep(30)
            return Response(200, content=b"A" * 100)  # pragma: no cover - cancelled
        finally:
            finished.set()

    respx.get(STREAM_URL).mock(return_value=Response(500))
    respx.get(AUDIO_URL).mock(side_effect=_slow)
    video = tmp_path / "media.mp4"
    audio = tmp_path / "media.m4a"

    started_at = asyncio.get_running_loop().time()
    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadError):
            await HttpDownloader(settings, client).download_many(
                [(_stream(), video), (_stream(url=AUDIO_URL, is_audio=True), audio)]
            )
    elapsed = asyncio.get_running_loop().time() - started_at

    assert started.is_set(), "慢的兄弟流应当已经开始"
    assert finished.is_set(), "返回前必须等兄弟流真正停下"
    assert elapsed < 10, "不应等待 30 秒的兄弟流结束"
    assert not video.exists()
    assert not audio.exists()


@respx.mock
async def test_a_finished_sibling_is_reported_when_the_other_fails(
    settings: Settings, tmp_path: Path
) -> None:
    """The caller needs proof of which files this run produced."""

    respx.get(STREAM_URL).mock(return_value=Response(500))
    respx.get(AUDIO_URL).mock(return_value=Response(200, content=b"A" * 300))
    video = tmp_path / "media.mp4"
    audio = tmp_path / "media.m4a"

    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadError) as excinfo:
            await HttpDownloader(settings, client).download_many(
                [(_stream(), video), (_stream(url=AUDIO_URL, is_audio=True), audio)]
            )

    finished = completed_outcomes(excinfo.value)
    assert [outcome.path for outcome in finished] == [audio]
    assert audio.read_bytes() == b"A" * 300
    assert not video.exists()


@respx.mock
async def test_cancelling_a_pair_keeps_the_resume_data(settings: Settings, tmp_path: Path) -> None:
    """Cancelling must not delete what a later run could resume from."""

    async def _slow(_request: httpx.Request) -> Response:  # pragma: no cover - cancelled
        await asyncio.sleep(30)
        return Response(200, content=b"x" * 10)

    respx.get(STREAM_URL).mock(side_effect=_slow)
    respx.get(AUDIO_URL).mock(side_effect=_slow)
    audio = tmp_path / "media.m4a"
    part = audio.with_name(audio.name + ".part")
    audio_stream = _stream(url=AUDIO_URL, is_audio=True)
    _seed_partial(part, audio_stream, b"resume me")

    async with httpx.AsyncClient() as client:
        downloader = HttpDownloader(settings, client)
        task = asyncio.create_task(
            downloader.download_many([(_stream(), tmp_path / "media.mp4"), (audio_stream, audio)])
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert part.read_bytes() == b"resume me"
    assert resume_sidecar_path(part).exists()
    assert not audio.exists()
