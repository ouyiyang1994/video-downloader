"""File integrity helpers."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

_READ_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


async def sha256_file_async(path: Path) -> str:
    return await asyncio.to_thread(sha256_file, path)


def size_matches(path: Path, expected: int | None, *, tolerance: int = 0) -> bool:
    """Return True when ``path`` has the expected size within ``tolerance``."""

    if expected is None:
        return True
    if not path.exists():
        return False
    return abs(path.stat().st_size - expected) <= tolerance


def looks_like_media(path: Path, *, minimum_bytes: int = 1024) -> bool:
    """Cheap sanity check that a finished file is not an error page."""

    if not path.exists() or path.stat().st_size < minimum_bytes:
        return False
    with path.open("rb") as handle:
        head = handle.read(64)
    return not head.lstrip().startswith(b"<")
