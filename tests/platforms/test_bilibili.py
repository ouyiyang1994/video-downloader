"""Bilibili adapter against recorded API shapes."""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core.exceptions import AuthRequiredError
from core.models import Platform
from platforms.bilibili.adapter import BilibiliAdapter

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
PLAYURL_URL = "https://api.bilibili.com/x/player/wbi/playurl"
UNSIGNED_PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"

NAV_PAYLOAD = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        }
    },
}

VIEW_PAYLOAD = {
    "code": 0,
    "data": {
        "bvid": "BV1GJ411x7h7",
        "aid": 80433022,
        "cid": 137649199,
        "title": "【官方 MV】Never Gonna Give You Up",
        "desc": "描述文本",
        "pic": "http://i1.hdslb.com/bfs/archive/demo.jpg",
        "pubdate": 1580000000,
        "duration": 213,
        "owner": {"mid": 12345, "name": "RickAstleyVEVO"},
        "pages": [{"cid": 137649199, "page": 1, "part": "正片", "duration": 213}],
        "stat": {"view": 1000, "like": 100},
    },
}

PLAYURL_PAYLOAD = {
    "code": 0,
    "data": {
        "quality": 80,
        "accept_quality": [80, 64, 32],
        "support_formats": [
            {"quality": 80, "new_description": "1080P 高清"},
            {"quality": 64, "new_description": "720P 高清"},
            {"quality": 32, "new_description": "480P 清晰"},
        ],
        "dash": {
            "duration": 213,
            "video": [
                {
                    "id": 80,
                    "baseUrl": "https://cdn.test/v1080.m4s",
                    "bandwidth": 2_000_000,
                    "codecs": "avc1.640028",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "id": 64,
                    "baseUrl": "https://cdn.test/v720.m4s",
                    "bandwidth": 900_000,
                    "codecs": "avc1.64001F",
                    "width": 1280,
                    "height": 720,
                },
            ],
            "audio": [
                {
                    "id": 30280,
                    "baseUrl": "https://cdn.test/audio.m4s",
                    "bandwidth": 128_000,
                    "codecs": "mp4a.40.2",
                }
            ],
        },
    },
}


def _mock_api() -> None:
    respx.get(url__regex=rf"{NAV_URL}.*").mock(return_value=Response(200, json=NAV_PAYLOAD))
    respx.get(url__regex=rf"{VIEW_URL}.*").mock(return_value=Response(200, json=VIEW_PAYLOAD))
    respx.get(url__regex=rf"{PLAYURL_URL}.*").mock(return_value=Response(200, json=PLAYURL_PAYLOAD))


async def _adapter(settings: Settings) -> tuple[BilibiliAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient()
    return BilibiliAdapter(settings, client), client


def test_url_matching(settings: Settings) -> None:
    adapter = BilibiliAdapter(settings, httpx.AsyncClient())
    assert adapter.platform is Platform.BILIBILI
    assert adapter.matches("https://www.bilibili.com/video/BV1GJ411x7h7")
    assert adapter.matches("https://b23.tv/abcdefg")
    assert not adapter.matches("https://example.com/video/BV1GJ411x7h7")


@respx.mock
async def test_fetch_info_builds_streams(settings: Settings) -> None:
    _mock_api()
    adapter, client = await _adapter(settings)
    async with client:
        info = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    assert info.platform is Platform.BILIBILI
    assert info.video_id == "BV1GJ411x7h7"
    assert info.title == "【官方 MV】Never Gonna Give You Up"
    assert info.author == "RickAstleyVEVO"
    assert info.duration == 213.0
    assert info.thumbnail_url == "https://i1.hdslb.com/bfs/archive/demo.jpg"
    assert len([s for s in info.streams if not s.is_audio]) == 2
    assert len([s for s in info.streams if s.is_audio]) == 1
    assert info.extra["cid"] == 137649199
    # CDN requests must carry a Referer or Bilibili blocks them.
    assert info.streams[0].headers["Referer"].startswith("https://www.bilibili.com")


@respx.mock
async def test_wbi_signature_is_sent(settings: Settings) -> None:
    _mock_api()
    adapter, client = await _adapter(settings)
    async with client:
        await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    playurl_calls = [call for call in respx.calls if "/wbi/playurl" in str(call.request.url)]
    assert playurl_calls
    params = playurl_calls[0].request.url.params
    assert "w_rid" in params
    assert "wts" in params
    assert len(params["w_rid"]) == 32


@respx.mock
async def test_select_streams(settings: Settings) -> None:
    _mock_api()
    adapter, client = await _adapter(settings)
    async with client:
        info = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    best = adapter.select_streams(info, "best")
    assert best.video is not None and best.video.height == 1080
    assert best.audio is not None
    assert best.merge is True
    assert best.quality_label == "1080P 高清"

    mid = adapter.select_streams(info, "720p")
    assert mid.video is not None and mid.video.height == 720

    low = adapter.select_streams(info, "360p")
    assert low.video is not None and low.video.height == 720

    audio = adapter.select_streams(info, "audio")
    assert audio.video is None and audio.audio is not None
    assert audio.container == "m4a"


@respx.mock
async def test_progressive_fallback(settings: Settings) -> None:
    respx.get(url__regex=rf"{NAV_URL}.*").mock(return_value=Response(200, json=NAV_PAYLOAD))
    respx.get(url__regex=rf"{VIEW_URL}.*").mock(return_value=Response(200, json=VIEW_PAYLOAD))
    respx.get(url__regex=rf"{PLAYURL_URL}.*").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "data": {
                    "quality": 32,
                    "support_formats": [{"quality": 32, "new_description": "480P 清晰"}],
                    "durl": [{"url": "https://cdn.test/progressive.flv", "size": 12345}],
                },
            },
        )
    )
    adapter, client = await _adapter(settings)
    async with client:
        info = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    plan = adapter.select_streams(info, "best")
    assert plan.video is not None
    assert plan.video.size == 12345
    assert plan.video.ext == "flv"
    assert plan.merge is False


@respx.mock
async def test_login_required_surfaces_auth_error(settings: Settings) -> None:
    respx.get(url__regex=rf"{NAV_URL}.*").mock(return_value=Response(200, json=NAV_PAYLOAD))
    respx.get(url__regex=rf"{VIEW_URL}.*").mock(
        return_value=Response(200, json={"code": -403, "message": "访问权限不足"})
    )
    adapter, client = await _adapter(settings)
    async with client:
        with pytest.raises(AuthRequiredError):
            await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")


@respx.mock
async def test_anonymous_session_keeps_the_480p_cap(settings: Settings) -> None:
    """An anonymous session only receives 480P/360P; best must not invent more."""

    respx.get(url__regex=rf"{NAV_URL}.*").mock(return_value=Response(200, json=NAV_PAYLOAD))
    respx.get(url__regex=rf"{VIEW_URL}.*").mock(return_value=Response(200, json=VIEW_PAYLOAD))
    respx.get(url__regex=rf"{PLAYURL_URL}.*").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "data": {
                    "quality": 32,
                    "accept_quality": [80, 64, 32, 16],
                    "support_formats": [
                        {"quality": 80, "new_description": "1080P 高清"},
                        {"quality": 32, "new_description": "480P 标清"},
                        {"quality": 16, "new_description": "360P 流畅"},
                    ],
                    "dash": {
                        "duration": 213,
                        "video": [
                            {
                                "id": 32,
                                "baseUrl": "https://cdn.test/v480.m4s",
                                "bandwidth": 218_550,
                                "codecs": "avc1.64001F",
                                "width": 852,
                                "height": 480,
                            },
                            {
                                "id": 16,
                                "baseUrl": "https://cdn.test/v360.m4s",
                                "bandwidth": 153_220,
                                "codecs": "avc1.64001E",
                                "width": 640,
                                "height": 360,
                            },
                        ],
                        "audio": [
                            {
                                "id": 30280,
                                "baseUrl": "https://cdn.test/a.m4s",
                                "bandwidth": 203_786,
                                "codecs": "mp4a.40.2",
                            }
                        ],
                    },
                },
            },
        )
    )
    adapter, client = await _adapter(settings)
    async with client:
        info = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    # The API advertises 1080P but the session is only entitled to 480P.
    assert info.extra["accept_quality"] == [80, 64, 32, 16]
    heights = sorted({s.height for s in info.streams if not s.is_audio}, reverse=True)
    assert heights == [480, 360]

    for quality in ("best", "1080p", "720p", "480p"):
        plan = adapter.select_streams(info, quality)
        assert plan.video is not None
        assert plan.video.height == 480
        assert plan.quality_label == "480P 标清"
        assert plan.audio is not None
        assert plan.merge is True
