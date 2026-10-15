"""Generic HTTP download engine: resume, retry, progress, verification."""

from __future__ import annotations

import asyncio
import errno
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from config.constants import PARTIAL_SUFFIX
from config.settings import Settings
from core.exceptions import (
    DownloadCancelled,
    DownloadError,
    IntegrityError,
    TargetFileError,
)
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


def _finalise(part_path: Path, destination: Path) -> None:
    """Move the finished file into place, explaining a locked destination.

    Another process holding the target - a player, a sync client, a virus
    scanner - makes the rename fail with ``WinError 32``/``PermissionError``. A
    bare ``OSError`` escapes every ``VideoDownloaderError`` handler above this
    layer, so the queue would record it as an internal error and the user would
    never learn which file is in the way. A :class:`TargetFileError` also tells
    the retry loop that the bytes are already on disk: it retries this rename
    instead of fetching the body again.
    """

    try:
        part_path.replace(destination)
    except OSError as exc:
        raise TargetFileError(
            f"无法写入目标文件：{destination.name}",
            detail=f"{_explain_os_error(exc)}（{type(exc).__name__}）",
        ) from exc


def _explain_os_error(exc: OSError) -> str:
    """Say what actually went wrong, instead of blaming every failure on locks.

    A full disk, a read-only folder and another process holding the file all
    arrive as ``OSError``; they need different answers from the user, so the
    errno decides the wording.
    """

    winerror = getattr(exc, "winerror", None)
    if exc.errno == errno.ENOSPC or winerror == 112:  # ERROR_DISK_FULL
        return "目标磁盘空间不足"
    if exc.errno == errno.EROFS:
        return "目标位置是只读的"
    if exc.errno in (errno.EACCES, errno.EPERM) or winerror in (5, 32):
        return "文件可能正被其他程序占用，或没有写入权限"
    return "无法替换目标文件"


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
            except TargetFileError:
                # The payload is complete next to the destination; the transfer
                # has nothing left to fetch, so retrying it would only re-
                # download the file. Report the rename failure as it is.
                raise
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
        expected_total = stream.size
        if resume and expected_total and part_path.exists():
            stored = part_path.stat().st_size
            if stored == expected_total:
                # A previous attempt fetched everything and only failed to move
                # the file into place (a locked destination, a denied rename).
                # Fetching it again would be pure waste - retry the rename.
                _finalise(part_path, destination)
                return DownloadOutcome(destination, expected_total, stored, 0.0)
        if resume and part_path.exists() and part_path.stat().st_size > 0:
            resumed_from = part_path.stat().st_size
            headers["Range"] = f"bytes={resumed_from}-"

        mode = "ab" if resumed_from else "wb"
        downloaded = resumed_from
        speed = 0.0

        async with self.client.stream("GET", stream.url, headers=headers) as response:
            if response.status_code == 416 and part_path.exists():
                # The partial file already holds the whole payload.
                size = part_path.stat().st_size
                _finalise(part_path, destination)
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

        _finalise(part_path, destination)
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
