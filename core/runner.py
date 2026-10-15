"""One-shot execution of a single download.

Every entry point in this project needs the same objects assembled around
:class:`~core.service.DownloadService`: an HTTP client, a platform registry, a
database handle, and the service itself. The desktop GUI's single-download
worker and the download queue's worker threads both need exactly that, so the
assembly lives here once rather than being copied per caller.

What this module deliberately does *not* do is touch the download path: it
calls ``DownloadService.download`` unchanged, which is why the queue inherits
proxy handling, cookie sessions, ffmpeg merging, resume, integrity checks and
history bookkeeping for free.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from config.settings import Settings
from core.database import DownloadDatabase
from core.downloader import ProgressCallback
from core.http import build_client
from core.interfaces import PlatformAdapter
from core.models import DownloadResult
from core.registry import build_registry
from core.service import DownloadService

logger = logging.getLogger(__name__)


async def execute_download(
    settings: Settings,
    url: str,
    *,
    quality: str = "best",
    output_dir: Path | None = None,
    force: bool = False,
    progress: dict[str, ProgressCallback] | None = None,
    cancel_event: threading.Event | None = None,
    on_adapter: Callable[[PlatformAdapter], None] | None = None,
) -> DownloadResult:
    """Resolve ``url`` and run one download to completion.

    ``on_adapter`` is called with the resolved adapter before any bytes move, so
    a caller can learn the platform (and whether it demands a rights
    confirmation) without resolving the URL a second time.

    Everything opened here is closed here, even on failure, so a caller may run
    this concurrently from several threads without leaking sockets or leaving a
    SQLite handle open.
    """

    client = build_client(settings)
    database = DownloadDatabase(settings)
    registry = None
    try:
        database.connect()
        registry = build_registry(settings, client)
        adapter = registry.resolve(url)
        if on_adapter is not None:
            on_adapter(adapter)
        service = DownloadService(settings, registry, client, database)
        return await service.download(
            url,
            quality=quality,
            output_dir=output_dir,
            force=force,
            progress=progress,
            cancel_event=cancel_event,
        )
    finally:
        if registry is not None:
            for extra in registry.owned_clients:
                await extra.aclose()
        await client.aclose()
        database.close()
