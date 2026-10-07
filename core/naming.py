"""Filesystem-safe naming and directory layout."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from core.models import Platform

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


def build_output_directory(
    base: Path,
    *,
    platform: Platform,
    author: str | None,
    publish_time: datetime | None,
    template: str = "{platform}/{author}/{date}",
    now: datetime | None = None,
) -> Path:
    """Render the configured directory template below ``base``."""

    reference = publish_time or now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    relative = template.format(
        platform=platform.value,
        author=sanitize_component(author or "unknown_author"),
        date=reference.strftime("%Y-%m-%d"),
        year=reference.strftime("%Y"),
        month=reference.strftime("%m"),
        day=reference.strftime("%d"),
    )

    parts = [sanitize_component(part) for part in Path(relative).parts if part not in ("", ".")]
    return base.joinpath(*parts) if parts else base


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
