"""Generic HTTP download engine: resume, retry, progress, verification."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from config.constants import PARTIAL_SUFFIX
from config.settings import Settings
from core.exceptions import DownloadCancelled, DownloadError, IntegrityError
from core.models import MediaStream

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProgressUpdate:
    """Emitted while bytes are being transferred."""

    stream_kind: str
    downloaded: int
    total: int | None
    speed_bps: float
    resumed_from: int = 0

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(self.downloaded / self.total, 1.0)


ProgressCallback = Callable[[ProgressUpdate], None]


def _noop(_: ProgressUpdate) -> None:  # pragma: no cover - trivial
    return None


@dataclass(slots=True)
class DownloadOutcome:
    path: Path
    bytes_downloaded: int
    resumed_from: int
    elapsed_seconds: float


class HttpDownloader:
    """Downloads individual :class:`MediaStream` objects to disk.

    Supports ranged resume through ``.part`` files, exponential-backoff
    retries, throttled progress callbacks and post-transfer size checks.
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        #: Optional cooperative cancellation. ``None`` keeps the previous
        #: behaviour exactly as it was (the CLI never passes one).
        self.cancel_event = cancel_event

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise DownloadCancelled("下载已取消")

    async def download_stream(
        self,
        stream: MediaStream,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
        resume: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> DownloadOutcome:
        callback = progress or _noop
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = destination.with_name(destination.name + PARTIAL_SUFFIX)

        attempts = max(1, self.settings.max_retries)
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(1, attempts + 1):
            self._raise_if_cancelled()
            try:
                outcome = await self._attempt(
                    stream,
                    destination,
                    part_path,
                    callback,
                    resume=resume,
                    extra_headers=extra_headers,
                )
                outcome.elapsed_seconds = time.monotonic() - started
                return outcome
            except (httpx.HTTPError, DownloadError, IntegrityError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                backoff = min(2 ** (attempt - 1), 15)
                logger.warning(
                    "下载 %s 第 %d/%d 次尝试失败：%s，%d 秒后重试",
                    destination.name,
                    attempt,
                    attempts,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)

        raise DownloadError(
            f"下载失败：{destination.name}",
            detail=str(last_error) if last_error else "unknown error",
        ) from last_error

    async def _attempt(
        self,
        stream: MediaStream,
        destination: Path,
        part_path: Path,
        callback: ProgressCallback,
        *,
        resume: bool,
        extra_headers: dict[str, str] | None,
    ) -> DownloadOutcome:
        headers = dict(stream.headers)
        if extra_headers:
            headers.update(extra_headers)

        resumed_from = 0
        if resume and part_path.exists() and part_path.stat().st_size > 0:
            resumed_from = part_path.stat().st_size
            headers["Range"] = f"bytes={resumed_from}-"

        mode = "ab" if resumed_from else "wb"
        downloaded = resumed_from
        expected_total = stream.size
        speed = 0.0

        async with self.client.stream("GET", stream.url, headers=headers) as response:
            if response.status_code == 416 and part_path.exists():
                # The partial file already holds the whole payload.
                size = part_path.stat().st_size
                part_path.replace(destination)
                return DownloadOutcome(destination, size, resumed_from, 0.0)

            if response.status_code not in (200, 206):
                raise DownloadError(
                    f"HTTP {response.status_code}",
                    detail=f"stream={stream.quality_label}",
                )

            if resumed_from and response.status_code == 200:
                logger.debug("服务器不支持断点续传，重新下载 %s", destination.name)
                resumed_from = 0
                downloaded = 0
                mode = "wb"

            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit():
                expected_total = int(content_length)
                if response.status_code == 206:
                    expected_total += resumed_from

            chunk_size = max(self.settings.chunk_size, 65536)
            window_bytes = 0
            window_started = time.monotonic()

            with part_path.open(mode) as handle:
                async for chunk in response.aiter_bytes(chunk_size):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    self._raise_if_cancelled()
                    window_bytes += len(chunk)
                    elapsed_window = time.monotonic() - window_started
                    if elapsed_window >= 0.5:
                        speed = window_bytes / elapsed_window
                        window_bytes = 0
                        window_started = time.monotonic()
                        callback(
                            ProgressUpdate(
                                stream_kind=stream.kind,
                                downloaded=downloaded,
                                total=expected_total,
                                speed_bps=speed,
                                resumed_from=resumed_from,
                            )
                        )

        callback(
            ProgressUpdate(
                stream_kind=stream.kind,
                downloaded=downloaded,
                total=expected_total,
                speed_bps=speed,
                resumed_from=resumed_from,
            )
        )

        actual_size = part_path.stat().st_size
        if expected_total and actual_size != expected_total:
            # Keep the partial file so the next attempt can resume from it.
            raise IntegrityError(
                f"文件大小不符（期望 {expected_total}，实际 {actual_size}）",
                detail=part_path.name,
            )

        part_path.replace(destination)
        return DownloadOutcome(
            path=destination,
            bytes_downloaded=actual_size,
            resumed_from=resumed_from,
            elapsed_seconds=0.0,
        )

    async def download_many(
        self,
        streams: list[tuple[MediaStream, Path]],
        *,
        progress: dict[str, ProgressCallback] | None = None,
    ) -> list[DownloadOutcome]:
        """Download several streams concurrently (typically video + audio)."""

        callbacks = progress or {}
        tasks = [
            self.download_stream(stream, path, progress=callbacks.get(stream.kind))
            for stream, path in streams
        ]
        return list(await asyncio.gather(*tasks))
