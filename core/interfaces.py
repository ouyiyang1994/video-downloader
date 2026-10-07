"""The unified adapter contract every platform must implement.

The application layer only ever talks to :class:`PlatformAdapter`; it never
imports anything platform specific.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

import httpx

from config.settings import Settings
from core.exceptions import NotDownloadableError
from core.models import DownloadPlan, Platform, VideoInfo


class PlatformAdapter(ABC):
    """Uniform interface for a video platform."""

    platform: ClassVar[Platform]
    #: Human readable name used in CLI output.
    display_name: ClassVar[str] = ""
    #: Set to True when the adapter fetches media through non-official
    #: endpoints and therefore requires ``--confirm-rights``.
    #: A plain attribute (not ClassVar) so an adapter can relax it per instance
    #: once official credentials are configured.
    requires_rights_confirmation: bool = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    # -- 1. URL recognition -------------------------------------------------
    @abstractmethod
    def matches(self, url: str) -> bool:
        """Return True when this adapter understands ``url``."""

    @abstractmethod
    def normalize_url(self, url: str) -> str:
        """Normalise a share/alias URL into a canonical form."""

    # -- 2. Metadata --------------------------------------------------------
    @abstractmethod
    async def fetch_info(self, url: str) -> VideoInfo:
        """Fetch metadata and the list of available streams."""

    # -- 3. Downloadability -------------------------------------------------
    def check_downloadable(self, info: VideoInfo) -> None:
        """Raise :class:`NotDownloadableError` when the media is off limits."""

        if not info.streams:
            raise NotDownloadableError(
                "该视频没有可用的下载流",
                detail=f"platform={self.platform.value} video={info.video_id}",
            )

    # -- 4. Stream selection ------------------------------------------------
    @abstractmethod
    def select_streams(self, info: VideoInfo, quality: str) -> DownloadPlan:
        """Pick the streams matching the requested quality preset."""

    # -- 5. Cover art / metadata sidecar ------------------------------------
    def cover_url(self, info: VideoInfo) -> str | None:
        return info.thumbnail_url

    # -- 6. Error translation ----------------------------------------------
    def describe(self) -> str:
        return self.display_name or self.platform.value
