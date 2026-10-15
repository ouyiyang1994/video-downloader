"""Bilibili adapter against recorded API shapes."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core.database import DownloadDatabase
from core.exceptions import AuthRequiredError
from core.models import DownloadRecord, DownloadStatus, Platform
from core.registry import PlatformRegistry
from core.service import DownloadService
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


# -- multi-part identity -----------------------------------------------------
#
# A 分P video is one投稿 with several independently downloadable parts, and they
# all share the same bvid. The history table is keyed on
# ``(platform, video_id)``, so while every part reported the bare bvid, the
# first part to finish made the service answer "已下载" for all the others - and
# hand back the first part's file. Verified against a real 17-part video
# (BV1fkHD6wEXt): ?p=1/3/5 resolved to different titles and different cids, but
# identical video_ids.

MULTI_VIEW_PAYLOAD = {
    "code": 0,
    "data": {
        "bvid": "BV1fkHD6wEXt",
        "aid": 425668128,
        "cid": 111111111,
        "title": "多分P 测试投稿",
        "desc": "描述文本",
        "pic": "http://i1.hdslb.com/bfs/archive/multi.jpg",
        "pubdate": 1580000000,
        "duration": 330,
        "owner": {"mid": 999, "name": "测试UP"},
        "pages": [
            {"cid": 111111111, "page": 1, "part": "初入小镇", "duration": 100},
            {"cid": 222222222, "page": 2, "part": "警察局 上", "duration": 110},
            {"cid": 333333333, "page": 3, "part": "市政广场", "duration": 120},
        ],
        "stat": {"view": 10},
    },
}

MULTI_URL = "https://www.bilibili.com/video/BV1fkHD6wEXt"


def _mock_multi_api() -> None:
    respx.get(url__regex=rf"{NAV_URL}.*").mock(return_value=Response(200, json=NAV_PAYLOAD))
    respx.get(url__regex=rf"{VIEW_URL}.*").mock(
        return_value=Response(200, json=MULTI_VIEW_PAYLOAD)
    )
    respx.get(url__regex=rf"{PLAYURL_URL}.*").mock(
        return_value=Response(200, json=PLAYURL_PAYLOAD)
    )


@respx.mock
async def test_each_part_of_a_multi_part_video_gets_its_own_identity(
    settings: Settings,
) -> None:
    """The shared bvid cannot be the download's identity."""

    _mock_multi_api()
    adapter, client = await _adapter(settings)
    async with client:
        first = await adapter.fetch_info(f"{MULTI_URL}?p=1")
        second = await adapter.fetch_info(f"{MULTI_URL}?p=2")
        third = await adapter.fetch_info(f"{MULTI_URL}?p=3")

    assert first.video_id == "BV1fkHD6wEXt_p1"
    assert second.video_id == "BV1fkHD6wEXt_p2"
    assert third.video_id == "BV1fkHD6wEXt_p3"
    assert len({first.video_id, second.video_id, third.video_id}) == 3
    # Not cosmetic: each part resolves to its own cid, so the media differs too.
    assert [i.extra["cid"] for i in (first, second, third)] == [
        111111111,
        222222222,
        333333333,
    ]
    assert "P1 初入小镇" in first.title
    assert "P2 警察局 上" in second.title


@respx.mock
async def test_the_playurl_request_follows_the_requested_part(settings: Settings) -> None:
    """``?p=N`` has to drive the cid that is actually asked for."""

    _mock_multi_api()
    adapter, client = await _adapter(settings)
    async with client:
        await adapter.fetch_info(f"{MULTI_URL}?p=3")

    requested = {
        call.request.url.params.get("cid")
        for call in respx.calls
        if "playurl" in str(call.request.url)
    }
    assert requested == {"333333333"}


@respx.mock
async def test_a_single_part_video_keeps_the_plain_bvid(settings: Settings) -> None:
    """Existing history rows are keyed on the bare bvid and must keep matching."""

    _mock_api()
    adapter, client = await _adapter(settings)
    async with client:
        plain = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")
        explicit = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7?p=1")

    assert plain.video_id == "BV1GJ411x7h7"
    assert explicit.video_id == "BV1GJ411x7h7"


@respx.mock
async def test_two_parts_do_not_collapse_into_one_history_row(settings: Settings) -> None:
    """The end of the chain: two parts must occupy two rows."""

    _mock_multi_api()
    adapter, client = await _adapter(settings)
    database = DownloadDatabase(settings)
    database.connect()
    try:
        async with client:
            first = await adapter.fetch_info(f"{MULTI_URL}?p=1")
            second = await adapter.fetch_info(f"{MULTI_URL}?p=2")
        for info in (first, second):
            database.upsert(
                DownloadRecord(
                    platform=info.platform,
                    video_id=info.video_id,
                    url=info.url,
                    title=info.title,
                    status=DownloadStatus.COMPLETED,
                )
            )
        rows = [r for r in database.recent(limit=20) if "BV1fkHD6wEXt" in (r.video_id or "")]
    finally:
        database.close()

    assert len(rows) == 2, "两个分P 写进了同一行，后者覆盖了前者"
    assert {r.video_id for r in rows} == {"BV1fkHD6wEXt_p1", "BV1fkHD6wEXt_p2"}


# -- history compatibility ---------------------------------------------------
#
# 1.25 keys each 分P as ``<bvid>_p<n>``. Rows written before that used the bare
# bvid for whichever part happened to be downloaded first, so P1 would be
# downloaded again for no reason. The bare bvid is therefore offered as a legacy
# key - but a row under it may hold *any* part, which is why the stored URL's
# page is checked before the row is trusted.

MULTI_BVID = "BV1fkHD6wEXt"


class _ReachedTransfer(Exception):
    """Raised by the stubbed transfer: proves the skip was NOT taken."""


def _store_completed_row(
    database: DownloadDatabase, *, stored_url: str, file_path: Path
) -> None:
    """Write a row the way an older version would have: keyed on the bare bvid."""

    database.upsert(
        DownloadRecord(
            platform=Platform.BILIBILI,
            video_id=MULTI_BVID,
            url=stored_url,
            title="旧版本记录",
            file_path=str(file_path),
            status=DownloadStatus.COMPLETED,
        )
    )


async def test_a_multi_part_entry_gets_an_identity_and_a_part_title(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp's flat 分P entries have neither, so the adapter supplies both.

    The raw entries here are exactly what the engine now produces for Bilibili:
    a URL carrying ``?p=``, the listing title, and no identity.
    """

    from core.engine_ytdlp import YtDlpEngine
    from core.models import PlaylistEntry

    async def fake_extract(self, url, *, limit=200, referer=None):  # noqa: ANN001, ANN202
        return [
            PlaylistEntry(url=f"{MULTI_URL}?p={n}", title="多分P 测试投稿", index=n)
            for n in (1, 2, 3)
        ]

    monkeypatch.setattr(YtDlpEngine, "extract_playlist", fake_extract)
    adapter, client = await _adapter(settings)
    async with client:
        entries = await adapter.expand_playlist(MULTI_URL)

    assert [e.video_id for e in entries] == [
        "BV1fkHD6wEXt_p1",
        "BV1fkHD6wEXt_p2",
        "BV1fkHD6wEXt_p3",
    ]
    assert [e.title for e in entries] == [
        "多分P 测试投稿 - P1",
        "多分P 测试投稿 - P2",
        "多分P 测试投稿 - P3",
    ]
    assert [e.index for e in entries] == [1, 2, 3]


@respx.mock
async def test_the_legacy_key_is_offered_only_for_multi_part_videos(
    settings: Settings,
) -> None:
    """A single-part video already uses the bare bvid, so it needs no fallback."""

    _mock_multi_api()
    adapter, client = await _adapter(settings)
    async with client:
        part = await adapter.fetch_info(f"{MULTI_URL}?p=2")

        assert adapter.legacy_video_ids(part) == (MULTI_BVID,)
        # Same投稿 and same 分P: the old row really is this download.
        assert adapter.is_same_download(part, f"{MULTI_URL}?p=2") is True
        # A URL without ?p= means page 1, so it is not part 2.
        assert adapter.is_same_download(part, MULTI_URL) is False
        assert adapter.is_same_download(part, f"{MULTI_URL}?p=1") is False
        # Another投稿 is another download, and an empty URL is no evidence.
        other = "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
        assert adapter.is_same_download(part, other) is False
        assert adapter.is_same_download(part, "") is False

        _mock_api()
        single = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    assert single.video_id == "BV1GJ411x7h7"
    assert adapter.legacy_video_ids(single) == ()


@respx.mock
async def test_an_old_row_for_the_same_part_is_not_downloaded_again(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of the whole scheme: upgrading must not re-download P1."""

    _mock_multi_api()
    adapter, client = await _adapter(settings)
    produced = tmp_path / "P1.mp4"
    produced.write_bytes(b"old file")

    database = DownloadDatabase(settings)
    database.connect()
    _store_completed_row(database, stored_url=f"{MULTI_URL}?p=1", file_path=produced)

    async def sentinel(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise _ReachedTransfer("没有命中旧记录，开始重新下载了")

    monkeypatch.setattr(DownloadService, "_fetch_streams", sentinel)
    try:
        async with client:
            registry = PlatformRegistry([adapter])
            service = DownloadService(settings, registry, client, database)
            result = await service.download(f"{MULTI_URL}?p=1", quality="best")
    finally:
        database.close()

    assert result.status is DownloadStatus.SKIPPED
    assert result.video_path == produced


@respx.mock
async def test_an_old_row_holding_another_part_does_not_skip_part_one(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bare-bvid row may hold any part, so the page decides - not the key.

    Trusting the key alone is what used to hand the user P3's file when they
    asked for P1.
    """

    _mock_multi_api()
    adapter, client = await _adapter(settings)
    produced = tmp_path / "P3.mp4"
    produced.write_bytes(b"part three")

    database = DownloadDatabase(settings)
    database.connect()
    _store_completed_row(database, stored_url=f"{MULTI_URL}?p=3", file_path=produced)

    async def sentinel(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise _ReachedTransfer("已进入传输阶段")

    monkeypatch.setattr(DownloadService, "_fetch_streams", sentinel)
    try:
        async with client:
            registry = PlatformRegistry([adapter])
            service = DownloadService(settings, registry, client, database)
            with pytest.raises(_ReachedTransfer):
                await service.download(f"{MULTI_URL}?p=1", quality="best")
    finally:
        database.close()
