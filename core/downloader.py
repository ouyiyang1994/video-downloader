"""Generic HTTP download engine: resume, retry, progress, verification."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from config.constants import PARTIAL_META_SUFFIX, PARTIAL_SUFFIX
from config.settings import Settings
from core.exceptions import (
    DownloadCancelled,
    DownloadError,
    IntegrityError,
    TargetFileConflictError,
    TargetFileError,
)
from core.models import MediaStream

logger = logging.getLogger(__name__)

#: Bumped when the sidecar layout changes; an unknown version is treated as
#: "no metadata" so an old file can never be resumed into a new build.
RESUME_META_VERSION = 1

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
#: What a 416 carries: ``Content-Range: bytes */1234`` - the current length.
_UNSATISFIED_RANGE = re.compile(r"bytes\s+\*/(\d+)", re.IGNORECASE)

#: A freshly written file can be held for a few milliseconds by a scanner, the
#: search indexer or a sync client. Retrying the rename is far cheaper than
#: discarding a complete download.
_RENAME_ATTEMPTS = 4
_RENAME_DELAY = 0.05

#: Where a failed ``download_many`` parks the outcomes that did finish.
_COMPLETED_OUTCOMES = "completed_outcomes"


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


@dataclass(frozen=True, slots=True)
class _DestinationState:
    """What the destination looked like when this download was authorised.

    ``claim_output_path`` promises one of two things: the name is free, or it
    holds the file of *this* video that a forced re-download may replace. The
    second case is only safe while the file is still that same file, so its
    identity is captured up front and checked again right before the rename.
    """

    existed: bool
    signature: tuple[int, int, int] | None


def _destination_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    # Size, modification time and file index: cheap to read, and together they
    # tell "the same file" from "somebody else's file at the same path".
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _capture_destination(path: Path) -> _DestinationState:
    signature = _destination_signature(path)
    return _DestinationState(existed=signature is not None, signature=signature)


def _replacement_blocker(state: _DestinationState, destination: Path) -> str | None:
    """Why the destination is no longer the file we may replace, if it is not.

    - It did not exist and now does: another program created it. Overwriting
      would destroy work that is not ours.
    - It existed (the authorised re-download case) but is now a different file:
      the original is gone, so the authorisation no longer applies.
    - It existed and is now gone: writing a fresh file there is exactly what
      the download wanted, so this is not a blocker.
    """

    current = _destination_signature(destination)
    if not state.existed:
        return "目标文件已被其他程序创建" if current is not None else None
    if current is None:
        return None
    if current != state.signature:
        return "目标文件已被其他程序替换"
    return None


def _finalise(
    part_path: Path,
    destination: Path,
    *,
    attempts: int = _RENAME_ATTEMPTS,
    expected: _DestinationState | None = None,
) -> None:
    """Move the finished file into place, explaining a locked destination.

    Another process holding the target - a player, a sync client, a virus
    scanner, the search indexer - makes the rename fail with
    ``WinError 32``/``PermissionError``. Those handles are usually short lived,
    so the rename is retried a few times before giving up: throwing away a
    complete download because a scanner glanced at it would be wasteful. A
    bare ``OSError`` escapes every ``VideoDownloaderError`` handler above this
    layer, so the final failure is a :class:`TargetFileError`, which also tells
    the retry loop that the bytes are already on disk.

    ``attempts`` lets the caller pace the retries itself (see
    :meth:`HttpDownloader._retry_move`) instead of stacking both loops.
    ``expected`` refuses the replacement when the destination stopped being the
    file this download was allowed to write - checked immediately before every
    attempt, so the window between the check and the rename is as small as the
    platform allows.
    """

    last_error: OSError | None = None
    for attempt in range(max(1, attempts)):
        if expected is not None:
            blocker = _replacement_blocker(expected, destination)
            if blocker is not None:
                # Not a lock: the authorisation to write here is gone, so no
                # amount of retrying can help and the queue must not re-run.
                raise TargetFileConflictError(
                    f"{blocker}，已停止写入：{destination.name}",
                    detail="目标文件与本次下载获准替换的文件不一致",
                )
        try:
            part_path.replace(destination)
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(_RENAME_DELAY * (attempt + 1))

    assert last_error is not None  # the loop only ends after a failure
    raise TargetFileError(
        f"无法写入目标文件：{destination.name}",
        detail=f"{_explain_os_error(last_error)}（{type(last_error).__name__}）",
    ) from last_error


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


def normalise_stream_url(url: str) -> str:
    """The part of a media URL that identifies the resource, not the session.

    CDN links carry rotating signatures in the query string - and that is also
    where signed credentials live - so the query and fragment are dropped
    before a URL is compared or written to disk. Nothing secret reaches the
    sidecar, and a refreshed link still resumes the same partial file.
    """

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url.split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


@dataclass(frozen=True, slots=True)
class ResumeIdentity:
    """What a ``.part`` file is allowed to be resumed for.

    Deliberately excludes every header: ``stream.headers`` carries cookies and
    referer values on some platforms, and none of that may be persisted.
    """

    url: str
    kind: str
    format_id: str | None
    quality_label: str
    size: int | None

    @classmethod
    def from_stream(cls, stream: MediaStream) -> ResumeIdentity:
        return cls(
            url=normalise_stream_url(stream.url),
            kind=stream.kind,
            format_id=stream.format_id,
            quality_label=stream.quality_label,
            size=stream.size,
        )

    def matches(self, other: ResumeIdentity) -> bool:
        if (self.url, self.kind, self.format_id, self.quality_label) != (
            other.url,
            other.kind,
            other.format_id,
            other.quality_label,
        ):
            return False
        # A size both sides know must agree; an unknown size is settled later
        # by the Content-Length check after the transfer.
        return self.size is None or other.size is None or self.size == other.size

    def to_payload(self) -> dict[str, object]:
        return {
            "version": RESUME_META_VERSION,
            "url": self.url,
            "kind": self.kind,
            "format_id": self.format_id,
            "quality_label": self.quality_label,
            "size": self.size,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ResumeIdentity | None:
        if not isinstance(payload, dict) or payload.get("version") != RESUME_META_VERSION:
            return None
        url = payload.get("url")
        kind = payload.get("kind")
        quality = payload.get("quality_label")
        format_id = payload.get("format_id")
        size = payload.get("size")
        if not isinstance(url, str) or not isinstance(kind, str) or not isinstance(quality, str):
            return None
        if format_id is not None and not isinstance(format_id, str):
            return None
        if size is not None and not isinstance(size, int):
            return None
        return cls(url=url, kind=kind, format_id=format_id, quality_label=quality, size=size)


def resume_sidecar_path(part_path: Path) -> Path:
    """Where the metadata describing ``part_path`` lives."""

    return part_path.with_name(part_path.name + PARTIAL_META_SUFFIX)


def write_resume_sidecar(part_path: Path, stream: MediaStream) -> None:
    """Record what ``part_path`` holds - atomically, and without credentials."""

    sidecar = resume_sidecar_path(part_path)
    temporary = sidecar.with_name(sidecar.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(ResumeIdentity.from_stream(stream).to_payload(), ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, sidecar)
    except OSError as exc:  # a missing sidecar only costs the next resume
        logger.debug("无法写入续传元数据 %s：%s", sidecar.name, exc)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def read_resume_sidecar(part_path: Path) -> ResumeIdentity | None:
    """The recorded identity, or ``None`` when absent, corrupt or outdated."""

    try:
        raw = resume_sidecar_path(part_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return ResumeIdentity.from_payload(payload)


def _remove_sidecar(part_path: Path) -> None:
    try:
        resume_sidecar_path(part_path).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("无法删除续传元数据 %s：%s", part_path.name, exc)


def _discard_partial(part_path: Path) -> None:
    """Remove a partial file whose origin cannot be trusted, plus its sidecar."""

    for target in (part_path, resume_sidecar_path(part_path)):
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("无法删除 %s：%s", target.name, exc)


def completed_outcomes(exc: BaseException) -> list[DownloadOutcome]:
    """Streams that finished before a :meth:`HttpDownloader.download_many` failed.

    The caller uses this as proof of ownership when cleaning up: only a file
    this run actually produced may be deleted.
    """

    value = getattr(exc, _COMPLETED_OUTCOMES, None)
    return list(value) if isinstance(value, list) else []


def _content_range_start(response: httpx.Response) -> int | None:
    """First byte offset the server says it is sending, if it says anything."""

    header = response.headers.get("content-range")
    if not header:
        return None
    match = _CONTENT_RANGE.match(header.strip())
    return int(match.group(1)) if match else None


def _unsatisfied_range_total(response: httpx.Response) -> int | None:
    """The resource length a 416 reports as ``Content-Range: bytes */N``."""

    header = response.headers.get("content-range")
    if not header:
        return None
    match = _UNSATISFIED_RANGE.match(header.strip())
    return int(match.group(1)) if match else None


def _describe_416(response: httpx.Response) -> str:
    """Why a 416 could not be trusted, in words the log can use."""

    header = response.headers.get("content-range")
    if not header:
        return "缺少 Content-Range"
    total = _unsatisfied_range_total(response)
    if total is None:
        return f"Content-Range 无效（{header}）"
    return f"服务器报告总长 {total}"


def _resume_is_complete(part_path: Path, stream: MediaStream, response: httpx.Response) -> bool:
    """Can we *prove* the partial file already holds the whole payload?

    A 416 only means "this range is not satisfiable". Servers answer that for a
    complete file, but also when the local file is larger than the resource or
    when they refuse ranges outright - renaming in those cases would publish
    bytes nobody verified. So completeness needs at least one independent
    statement of the full length, and every statement we do have must agree
    with what is on disk.
    """

    try:
        size = part_path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    if stream.size is not None and size != stream.size:
        return False
    total = _unsatisfied_range_total(response)
    if total is not None and size != total:
        return False
    return stream.size is not None or total is not None


def _resume_offset(part_path: Path, stream: MediaStream, *, resume: bool) -> int:
    """How many bytes may be reused, after checking where they came from."""

    if not part_path.exists():
        _remove_sidecar(part_path)  # left over from an attempt that wrote no bytes
        return 0
    if not resume:
        return 0  # the caller asked to start over; opening "wb" truncates
    recorded = read_resume_sidecar(part_path)
    if recorded is None:
        logger.info("临时文件缺少来源信息，重新下载：%s", part_path.name)
        _discard_partial(part_path)
        return 0
    if not recorded.matches(ResumeIdentity.from_stream(stream)):
        logger.info("临时文件与当前任务不是同一下载，重新下载：%s", part_path.name)
        _discard_partial(part_path)
        return 0
    stored = part_path.stat().st_size
    if stream.size is not None and stored > stream.size:
        # Larger than the stream can possibly be: the bytes are not this
        # download's, and resuming would only make the size check fail again.
        logger.info("临时文件比预期更大，重新下载：%s", part_path.name)
        _discard_partial(part_path)
        return 0
    return stored


class HttpDownloader:
    """Downloads individual :class:`MediaStream` objects to disk.

    Supports ranged resume through ``.part`` files, exponential-backoff
    retries, throttled progress callbacks and post-transfer size checks.
    """

    #: Delays between attempts to move an already verified file into place.
    #: ``_finalise`` covers the first few hundred milliseconds; these cover a
    #: player, sync client or scanner that holds the target for a few seconds.
    #: Tests shorten them to keep the suite fast.
    move_retry_delays: tuple[float, ...] = (0.5, 1.0, 2.0)

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
        # Captured before a single byte moves: if the destination is not the
        # file it was at this moment, it is not ours to replace.
        expected = _capture_destination(destination)

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
                    expected=expected,
                )
                outcome.elapsed_seconds = time.monotonic() - started
                return outcome
            except TargetFileConflictError:
                # The destination stopped being ours; waiting cannot help.
                # The bytes can never be used either - the task ends here and
                # the name belongs to somebody else - so drop what we wrote,
                # but only while the caller still holds the path claim.
                self._discard_conflicted_partial(stream, part_path)
                raise
            except TargetFileError:
                # The bytes are already on disk and verified; only the move
                # failed. Retry the move here instead of letting the queue
                # re-run - and possibly re-download - the whole task.
                moved = self._retry_move(stream, destination, part_path, expected)
                if moved is not None:
                    return moved
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
        expected: _DestinationState | None = None,
    ) -> DownloadOutcome:
        offset = _resume_offset(part_path, stream, resume=resume)
        expected_total = stream.size
        if offset and expected_total and offset == expected_total:
            # Everything is already on disk; the previous attempt only failed to
            # move the file into place (a locked destination, a denied rename).
            # Fetching it again would be pure waste - retry the rename.
            _finalise(part_path, destination, expected=expected)
            _remove_sidecar(part_path)
            return DownloadOutcome(destination, expected_total, offset, 0.0)

        resumed_from = offset
        downloaded = offset
        speed = 0.0

        # At most two passes: the second one only runs when the server refused
        # the range or answered from a different offset. The partial file is
        # then discarded and the whole body is fetched again.
        for pass_index in (0, 1):
            resumed_from = offset if pass_index == 0 else 0
            expected_total = stream.size
            headers = dict(stream.headers)
            if extra_headers:
                headers.update(extra_headers)
            if resumed_from:
                headers["Range"] = f"bytes={resumed_from}-"
            else:
                # Written before the first byte, so an interrupted transfer can
                # always be identified as belonging to this task.
                write_resume_sidecar(part_path, stream)

            restart = False
            downloaded = resumed_from
            speed = 0.0

            async with self.client.stream("GET", stream.url, headers=headers) as response:
                if response.status_code == 416:
                    if (
                        resumed_from
                        and part_path.exists()
                        and _resume_is_complete(part_path, stream, response)
                    ):
                        # Proven complete: the range really did start past the end.
                        size = part_path.stat().st_size
                        _finalise(part_path, destination, expected=expected)
                        _remove_sidecar(part_path)
                        return DownloadOutcome(destination, size, resumed_from, 0.0)
                    logger.warning(
                        "HTTP 416 但无法确认本地文件完整（%s），重新下载 %s",
                        _describe_416(response),
                        destination.name,
                    )
                    _discard_partial(part_path)
                    offset = 0
                    restart = True
                elif response.status_code not in (200, 206):
                    raise DownloadError(
                        f"HTTP {response.status_code}",
                        detail=f"stream={stream.quality_label}",
                    )

                if not restart and resumed_from and response.status_code == 200:
                    logger.debug("服务器不支持断点续传，重新下载 %s", destination.name)
                    restart = True
                elif (
                    not restart
                    and resumed_from
                    and _content_range_start(response) != resumed_from
                ):
                    logger.warning(
                        "Content-Range 与已下载的 %d 字节不一致，重新下载 %s",
                        resumed_from,
                        destination.name,
                    )
                    restart = True

                if not restart:
                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit():
                        expected_total = int(content_length)
                        if response.status_code == 206:
                            expected_total += resumed_from

                    chunk_size = max(self.settings.chunk_size, 65536)
                    window_bytes = 0
                    window_started = time.monotonic()

                    with part_path.open("ab" if resumed_from else "wb") as handle:
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

            if restart:
                # Never append a full response - or a body that starts at the
                # wrong offset - to bytes we already have.
                _discard_partial(part_path)
                offset = 0
                continue
            break

        if restart:
            # Both passes were refused; there is nothing trustworthy on disk.
            raise DownloadError(
                "HTTP 416",
                detail="服务器拒绝了请求范围，且无法确认本地文件完整",
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
            if resumed_from:
                # The bytes we appended to were not this stream's after all.
                # Keeping them would make every retry fail the same way, so the
                # next attempt starts clean instead.
                _discard_partial(part_path)
            raise IntegrityError(
                f"文件大小不符（期望 {expected_total}，实际 {actual_size}）",
                detail=part_path.name,
            )

        _finalise(part_path, destination, expected=expected)
        _remove_sidecar(part_path)
        return DownloadOutcome(
            path=destination,
            bytes_downloaded=actual_size,
            resumed_from=resumed_from,
            elapsed_seconds=0.0,
        )

    def _retry_move(
        self,
        stream: MediaStream,
        destination: Path,
        part_path: Path,
        expected: _DestinationState,
    ) -> DownloadOutcome | None:
        """Move an already verified file into place after a transient lock.

        Called only once the transfer finished and passed its size check, so
        there is nothing left to fetch. Returns ``None`` - leaving the original
        error to the caller - when completeness cannot be proven or when the
        partial file changed while waiting. A destination that stopped being
        the file this download was authorised to replace raises instead: that
        is not a lock, and retrying cannot fix it.
        """

        try:
            size = part_path.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None
        if stream.size is not None and size != stream.size:
            return None  # cannot prove the file is whole, so never promote it
        recorded = read_resume_sidecar(part_path)
        if recorded is None or not recorded.matches(ResumeIdentity.from_stream(stream)):
            return None  # unknown origin means unknown completeness

        for delay in self.move_retry_delays:
            if delay:
                time.sleep(delay)
            try:
                if part_path.stat().st_size != size:
                    logger.warning("重命名重试期间分片文件发生变化，放弃：%s", part_path.name)
                    return None
            except OSError:
                return None
            try:
                _finalise(part_path, destination, attempts=1, expected=expected)
            except TargetFileConflictError:
                raise
            except TargetFileError as exc:
                blocker = _replacement_blocker(expected, destination)
                if blocker is not None:
                    raise TargetFileConflictError(
                        f"{blocker}，已停止写入：{destination.name}",
                        detail="重试落盘期间目标文件发生变化",
                    ) from exc
                continue
            _remove_sidecar(part_path)
            return DownloadOutcome(destination, size, size, 0.0)
        return None

    def _discard_conflicted_partial(self, stream: MediaStream, part_path: Path) -> None:
        """Drop this stream's partial file after the name was taken from us.

        A conflict ends the task, so the bytes can never be used: the
        destination belongs to somebody else now and a retry would claim a
        different name. Ownership still has to be *proven* before anything is
        deleted - the sidecar records which stream wrote the file, and a
        ``.part`` we cannot attribute is left exactly where it is. The caller
        still holds the destination claim here, which is what rules out another
        instance of the app using the same file.
        """

        recorded = read_resume_sidecar(part_path)
        if recorded is None or not recorded.matches(ResumeIdentity.from_stream(stream)):
            logger.warning("无法确认临时文件属于本任务，保留：%s", part_path.name)
            return
        _discard_partial(part_path)
        logger.info("目标文件冲突，已清理本任务的临时文件：%s", part_path.name)

    async def download_many(
        self,
        streams: list[tuple[MediaStream, Path]],
        *,
        progress: dict[str, ProgressCallback] | None = None,
    ) -> list[DownloadOutcome]:
        """Download several streams concurrently (typically video + audio).

        If one of them fails, the others are cancelled and awaited before the
        error is re-raised: a sibling left running would keep writing into the
        output folder after the task was already reported as failed. Whatever
        *did* finish is attached to the raised error (see
        :func:`completed_outcomes`) so the caller can remove exactly those
        intermediates instead of guessing from file names.
        """

        callbacks = progress or {}
        tasks = [
            asyncio.create_task(
                self.download_stream(stream, path, progress=callbacks.get(stream.kind))
            )
            for stream, path in streams
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException as exc:
            for task in tasks:
                task.cancel()
            # Wait for the cancellations to land. Without this the sibling may
            # still be writing when the caller starts cleaning up.
            with contextlib.suppress(BaseException):
                await asyncio.gather(*tasks, return_exceptions=True)
            finished = [
                task.result()
                for task in tasks
                if task.done() and not task.cancelled() and task.exception() is None
            ]
            if finished:
                setattr(exc, _COMPLETED_OUTCOMES, finished)
            raise
