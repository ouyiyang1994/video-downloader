"""Filesystem-safe naming and directory layout."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.exceptions import OutputLockError, OutputNameConflictError, PathTooLongError
from core.models import Platform

logger = logging.getLogger(__name__)

#: Conservative cap for a *whole* output path on Windows. The classic limit is
#: 260 characters, and only applications that declare ``longPathAware`` may go
#: beyond it; 240 leaves headroom for the suffixes the pipeline appends and for
#: tools that decorate names (ffmpeg temporaries, antivirus quarantine copies).
MAX_FULL_PATH = 240

#: Shortest file name worth generating. Below this the folder is unusable and
#: the user has to pick a shorter output directory instead.
MIN_STEM = 12

#: Longest decoration the pipeline adds to a stem: ``.video.`` + a 4-character
#: extension + ``.part`` = 16 characters, rounded up for safety.
LEAF_RESERVE = 18

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_TRAILING_DOTS_SPACES = re.compile(r"[ .]+$")

#: Names Windows refuses to use for a file or directory.
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(
    name: str,
    *,
    max_length: int = 120,
    replacement: str = "_",
    fallback: str = "untitled",
) -> str:
    """Return ``name`` as a safe single path component.

    Handles Windows illegal characters, control characters, reserved device
    names, trailing dots/spaces and length limits.
    """

    cleaned = _INVALID_CHARS.sub(replacement, name or "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if replacement:
        cleaned = re.sub(re.escape(replacement) + r"{2,}", replacement, cleaned)
    cleaned = _TRAILING_DOTS_SPACES.sub("", cleaned)

    if not cleaned:
        return fallback

    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_length:
        cleaned = _TRAILING_DOTS_SPACES.sub("", cleaned[:max_length]).strip()

    return cleaned or fallback


def sanitize_component(name: str, *, max_length: int = 60, fallback: str = "unknown") -> str:
    """Sanitize a directory component (author, platform folder, ...)."""

    return sanitize_filename(name, max_length=max_length, fallback=fallback)


def _render_directory(
    base: Path,
    platform: Platform,
    author: str,
    reference: datetime,
    template: str,
) -> Path:
    relative = template.format(
        platform=platform.value,
        author=author,
        date=reference.strftime("%Y-%m-%d"),
        year=reference.strftime("%Y"),
        month=reference.strftime("%m"),
        day=reference.strftime("%d"),
    )
    parts = [sanitize_component(part) for part in Path(relative).parts if part not in ("", ".")]
    return base.joinpath(*parts) if parts else base


def build_output_directory(
    base: Path,
    *,
    platform: Platform,
    author: str | None,
    publish_time: datetime | None,
    template: str = "{platform}/{author}/{date}",
    now: datetime | None = None,
    budget: int = MAX_FULL_PATH,
    reserve: int = LEAF_RESERVE,
) -> Path:
    """Render the configured directory template below ``base``.

    The whole path has to fit :data:`MAX_FULL_PATH`, not just the file name.
    When the rendered folder is too long the **author** segment is shortened
    first (the title is shortened later, by :func:`claim_output_path`);
    ``platform`` and ``date`` are structural and are never trimmed. If even a
    one-character author cannot make it fit, a
    :class:`~core.exceptions.PathTooLongError` is raised - writing to a
    truncated folder would put the file somewhere the user never chose.
    """

    reference = publish_time or now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    author_text = sanitize_component(author or "unknown_author")
    # Everything except the file name has to fit here, with MIN_STEM and the
    # leaf suffixes still to come.
    max_directory = budget - 1 - MIN_STEM - reserve
    base_length = len(str(base))
    if base_length >= max_directory:
        raise PathTooLongError(
            f"输出目录过长（{base_length} 字符），无法再容纳文件名",
            detail=str(base),
        )

    for _ in range(8):
        directory = _render_directory(base, platform, author_text, reference, template)
        overflow = len(str(directory)) - max_directory
        if overflow <= 0:
            return directory
        keep = len(author_text) - overflow
        if keep < 1:
            break
        shortened = sanitize_component(author_text[:keep])
        if shortened == author_text:
            break
        author_text = shortened

    raise PathTooLongError(
        f"输出路径超过 {budget} 字符上限，作者名已缩到最短仍放不下",
        detail=str(base),
    )


def unique_path(path: Path) -> Path:
    """Return ``path`` or ``path`` with a numeric suffix when it exists."""

    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(1, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法为 {path} 生成唯一文件名")


#: Destinations this process handed out but whose file does not exist yet.
#: ``unique_path`` alone is not enough: two queue workers can pick the same free
#: name before either has written a byte, and the second ``os.replace`` then
#: destroys the first download while both rows keep pointing at one file.
_CLAIMED_PATHS: dict[str, _DestinationLock] = {}
_CLAIM_LOCK = threading.Lock()

#: Where the cross-process lock files live. Kept out of the download folder so
#: a user never sees them, and out of ``%LOCALAPPDATA%\VideoDownloader`` so the
#: app never writes into its own user-data directory.
_LOCK_DIR_ENV = "VIDEO_DOWNLOADER_LOCK_DIR"
_LOCK_DIR_NAME = "VideoDownloader-locks"
#: How long to keep trying a contended name before moving on. A process that
#: died a moment ago still owns its lock until the kernel finishes reaping it,
#: and a scanner can hold the freshly created lock file for a few milliseconds.
_LOCK_RETRY_DELAYS: tuple[float, ...] = (0.0, 0.02, 0.05, 0.1)


def _claim_key(path: Path) -> str:
    # normcase folds separators and, on Windows, the case - the filesystem is
    # case-insensitive there, so "Song.mp4" and "song.mp4" are one file.
    return os.path.normcase(str(path))


def _lock_directory() -> Path:
    override = os.environ.get(_LOCK_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return Path(base) / _LOCK_DIR_NAME


def _lock_file(destination: Path) -> Path:
    digest = hashlib.sha256(_claim_key(destination).encode("utf-8")).hexdigest()
    return _lock_directory() / f"{digest[:32]}.lock"


def _lock_descriptor(fd: int) -> None:
    """Take a non-blocking exclusive lock on the first byte of ``fd``."""

    if os.name == "nt":
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - the product targets Windows
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]


def _unlock_descriptor(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - the product targets Windows
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:  # pragma: no cover - closing the handle releases it anyway
        pass
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class _DestinationLock:
    """One claimed destination and the OS handle keeping it claimed."""

    fd: int


def _acquire_destination_lock(destination: Path) -> _DestinationLock | None:
    """Claim ``destination`` across processes, or ``None`` when it is taken.

    The lock lives on an open file descriptor, so a crashed process releases it
    the moment the OS reaps its handles. Lock files are never deleted: removing
    one that another process holds would hand a third process a fresh inode for
    the same name, and both would believe they own it.

    A name that cannot be locked is reported as *taken*, which pushes the caller
    to the next candidate. There is deliberately no "in-process only" fallback:
    if the lock directory itself is unusable the caller gets
    :class:`~core.exceptions.OutputLockError` and the download stops, because
    without the lock two instances would happily write the same ``.part``.
    """

    try:
        lock_directory = _lock_directory()
        lock_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputLockError(
            f"无法创建输出锁目录（{lock_directory}），已停止下载以避免多个实例互相覆盖",
            detail=str(exc),
        ) from exc

    last_error: OSError | None = None
    for delay in _LOCK_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            fd = os.open(_lock_file(destination), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            # A scanner or indexer can hold the lock file exclusively for a few
            # milliseconds after it is created; keep trying before giving up.
            last_error = exc
            continue
        try:
            _lock_descriptor(fd)
        except OSError as exc:
            last_error = exc
            os.close(fd)
            continue
        return _DestinationLock(fd=fd)

    logger.debug("无法占用文件名 %s（%s）", destination.name, last_error)
    return None


def claim_output_path(
    directory: Path,
    stem: str,
    suffix: str,
    *,
    keep: Path | None = None,
    limit: int = 1000,
    budget: int = MAX_FULL_PATH,
    reserve: int = LEAF_RESERVE,
) -> Path:
    """Reserve a destination no other download - here or elsewhere - will take.

    ``keep`` is the one existing path the caller may reuse: re-downloading the
    same video keeps its historical file name instead of piling up ``(1)``,
    ``(2)`` copies. Any other existing file belongs to a different video and
    gets a numeric suffix. The claim must be released with
    :func:`release_output_path` when the download ends.

    The claim is an OS byte-range lock, so a second *process* is refused too:
    it moves on to the next free name instead of writing the same ``.part``
    and sidecar as this one. Nothing is ever overwritten to get a name.

    The returned path - including the ``(n)`` duplicate marker, the extension
    and the temporary suffixes counted by ``reserve`` - always fits
    ``budget``. The title is shortened as needed; when the directory alone
    leaves no room, :class:`~core.exceptions.PathTooLongError` is raised
    instead of writing an unusable one-character name.
    """

    directory_length = len(str(directory))
    keep_key = _claim_key(keep) if keep is not None else None
    with _CLAIM_LOCK:
        for index in range(limit):
            decoration = "" if index == 0 else f" ({index})"
            room = (
                budget
                - directory_length
                - 1
                - len(suffix)
                - 1
                - reserve
                - len(decoration)
            )
            if room < MIN_STEM:
                if index == 0:
                    raise PathTooLongError(
                        f"输出目录过长，只剩 {max(room, 0)} 个字符可用：{directory}",
                        detail=str(directory),
                    )
                break
            fitted = stem[:room].rstrip(" .") or "untitled"
            candidate = directory / f"{fitted}{decoration}.{suffix}"
            key = _claim_key(candidate)
            if key in _CLAIMED_PATHS:
                continue
            if candidate.exists() and key != keep_key:
                continue
            lock = _acquire_destination_lock(candidate)
            if lock is None:
                logger.info("另一个实例正在使用该文件名，换下一个：%s", candidate.name)
                continue
            # Re-check now that the name is ours: a non-app writer may have
            # created the file between the existence check and the lock.
            if candidate.exists() and key != keep_key:
                _release_destination_lock(lock)
                continue
            _CLAIMED_PATHS[key] = lock
            return candidate
    raise OutputNameConflictError(
        f"同名文件过多，无法为「{stem}」分配输出文件名",
        detail=f"已尝试 {limit} 个候选名，目录：{directory}",
    )


def _release_destination_lock(lock: _DestinationLock) -> None:
    _unlock_descriptor(lock.fd)


def release_output_path(path: Path) -> None:
    """Drop a claim once the download finished (succeeded or not)."""

    with _CLAIM_LOCK:
        lock = _CLAIMED_PATHS.pop(_claim_key(path), None)
    if lock is not None:
        _release_destination_lock(lock)
