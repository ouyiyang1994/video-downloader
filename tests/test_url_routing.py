"""URL recognition and routing for every platform."""

from __future__ import annotations

import httpx
import pytest

from config.settings import Settings
from core.exceptions import UnsupportedUrlError
from core.models import Platform
from core.registry import build_registry


@pytest.fixture
def registry(settings: Settings):
    client = httpx.AsyncClient()
    try:
        yield build_registry(settings, client)
    finally:
        import asyncio

        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE),
        ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", Platform.YOUTUBE),
        ("https://www.instagram.com/p/CxYz123AbC/", Platform.INSTAGRAM),
        ("https://www.instagram.com/reel/CxYz123AbC/", Platform.INSTAGRAM),
        ("https://www.bilibili.com/video/BV1GJ411x7h7", Platform.BILIBILI),
        ("https://b23.tv/abcdefg", Platform.BILIBILI),
        ("https://www.bilibili.com/video/av80433022", Platform.BILIBILI),
    ],
)
def test_routing(registry, url: str, expected: Platform) -> None:
    assert registry.resolve(url).platform is expected


def test_unsupported_url(registry) -> None:
    with pytest.raises(UnsupportedUrlError):
        registry.resolve("https://example.com/video/123")


def test_empty_url(registry) -> None:
    with pytest.raises(UnsupportedUrlError):
        registry.resolve("   ")


def test_platform_lookup_by_enum(registry) -> None:
    assert registry.get(Platform.YOUTUBE).platform is Platform.YOUTUBE
    assert registry.get(Platform.BILIBILI).display_name == "哔哩哔哩"


def test_youtube_normalisation(registry) -> None:
    adapter = registry.get(Platform.YOUTUBE)
    assert adapter.normalize_url("youtu.be/dQw4w9WgXcQ") == (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )


def test_instagram_normalisation(registry) -> None:
    adapter = registry.get(Platform.INSTAGRAM)
    assert adapter.normalize_url("instagram.com/reel/CxYz123AbC/") == (
        "https://instagram.com/reel/CxYz123AbC/"
    )
