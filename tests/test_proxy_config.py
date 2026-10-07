"""Proxy configuration and per-platform bypass."""

from __future__ import annotations

import httpx

from config.settings import Settings
from core.http import build_client
from core.models import Platform
from core.registry import build_registry

LOCAL_PROXY = "http://127.0.0.1:8090"


def test_bypass_list_parsing(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        proxy_bypass_platforms=" bilibili , youtube ,",
    )
    assert settings.proxy_bypass_set == {"bilibili", "youtube"}


def test_empty_bypass_list_means_proxy_everything(tmp_path) -> None:
    settings = Settings(_env_file=None, proxy_bypass_platforms="")
    assert settings.proxy_bypass_set == set()


def test_proxy_property_prefers_https(settings: Settings) -> None:
    settings.http_proxy = "http://127.0.0.1:8090"
    settings.https_proxy = "socks5://127.0.0.1:1080"
    assert settings.proxy == "socks5://127.0.0.1:1080"


def test_use_proxy_flag_is_accepted(settings: Settings) -> None:
    settings.http_proxy = LOCAL_PROXY
    proxied = build_client(settings)
    direct = build_client(settings, use_proxy=False)
    # Both are usable clients; the difference is only which transport they hold.
    assert isinstance(proxied, httpx.AsyncClient)
    assert isinstance(direct, httpx.AsyncClient)


async def test_bypass_platform_gets_its_own_direct_client(settings: Settings) -> None:
    settings.http_proxy = LOCAL_PROXY
    async with httpx.AsyncClient() as shared:
        registry = build_registry(settings, shared)
        try:
            bilibili = registry.get(Platform.BILIBILI)
            youtube = registry.get(Platform.YOUTUBE)
            assert bilibili.client is not shared
            assert youtube.client is shared
            assert len(registry.owned_clients) == 1

            # Every platform still resolves; only the transport differs.
            assert registry.resolve("https://www.bilibili.com/video/BV1GJ411x7h7").platform is (
                Platform.BILIBILI
            )
        finally:
            for extra in registry.owned_clients:
                await extra.aclose()


async def test_no_proxy_configured_keeps_a_single_client(settings: Settings) -> None:
    assert settings.proxy is None
    async with httpx.AsyncClient() as shared:
        registry = build_registry(settings, shared)
        assert registry.owned_clients == []
        for adapter in registry.adapters:
            assert adapter.client is shared
