"""Download engine: resume, retry, integrity."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core.downloader import HttpDownloader
from core.exceptions import DownloadError, IntegrityError
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
