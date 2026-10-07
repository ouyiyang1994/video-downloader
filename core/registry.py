"""Maps a URL to the adapter that understands it."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from config.settings import Settings
from core.exceptions import UnsupportedUrlError
from core.http import build_client
from core.interfaces import PlatformAdapter
from core.models import Platform

logger = logging.getLogger(__name__)


class PlatformRegistry:
    """Holds every registered adapter and resolves URLs against them."""

    def __init__(self, adapters: Sequence[PlatformAdapter]) -> None:
        if not adapters:
            raise ValueError("至少需要注册一个平台适配器")
        self._adapters = list(adapters)
        #: Extra clients created during registration; the caller closes them.
        self.owned_clients: list[httpx.AsyncClient] = []

    @property
    def adapters(self) -> list[PlatformAdapter]:
        return list(self._adapters)

    def resolve(self, url: str) -> PlatformAdapter:
        """Return the adapter responsible for ``url``."""

        cleaned = (url or "").strip()
        if not cleaned:
            raise UnsupportedUrlError("URL 为空")
        for adapter in self._adapters:
            if adapter.matches(cleaned):
                return adapter
        raise UnsupportedUrlError(
            "不支持的视频链接",
            detail=f"已支持：{', '.join(self.supported_platforms())}",
        )

    def supported_platforms(self) -> list[str]:
        return [adapter.platform.display_name for adapter in self._adapters]

    def get(self, platform: Platform) -> PlatformAdapter:
        for adapter in self._adapters:
            if adapter.platform is platform:
                return adapter
        raise UnsupportedUrlError(f"未注册平台：{platform.value}")


def build_registry(settings: Settings, client: httpx.AsyncClient) -> PlatformRegistry:
    """Instantiate every adapter. Imports live here to keep the core generic."""

    from platforms.bilibili.adapter import BilibiliAdapter
    from platforms.instagram.adapter import InstagramAdapter
    from platforms.youtube.adapter import YouTubeAdapter

    bypass = settings.proxy_bypass_set
    direct_client: httpx.AsyncClient | None = None
    if settings.proxy and bypass:
        # Platforms in PROXY_BYPASS_PLATFORMS keep the direct connection.
        direct_client = build_client(settings, use_proxy=False)

    builders = (YouTubeAdapter, InstagramAdapter, BilibiliAdapter)
    adapters: list[PlatformAdapter] = []
    for builder in builders:
        platform = builder.platform
        if direct_client is not None and platform.value in bypass:
            logger.debug("%s 绕过代理，使用直连", platform.value)
            adapters.append(builder(settings, direct_client))
        else:
            adapters.append(builder(settings, client))

    registry = PlatformRegistry(adapters)
    if direct_client is not None:
        registry.owned_clients.append(direct_client)
    logger.debug("已注册平台：%s", ", ".join(a.platform.value for a in adapters))
    return registry
