"""Per-platform cookie sources.

Instagram keeps using ``YTDLP_COOKIEFILE`` (``secrets/cookies.txt``).
Bilibili uses its own ``BILIBILI_COOKIEFILE`` (``www.bilibili.com_cookies.txt``).
The two must never be mixed, and a missing file must fall back to anonymous.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core.engine_ytdlp import YtDlpEngine
from core.http import load_cookie_header
from platforms.bilibili.adapter import BilibiliAdapter

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
PLAYURL_URL = "https://api.bilibili.com/x/player/wbi/playurl"

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
        "title": "cookie test",
        "pic": "http://i1.hdslb.com/bfs/archive/demo.jpg",
        "pubdate": 1580000000,
        "duration": 10,
        "owner": {"mid": 1, "name": "up"},
        "pages": [{"cid": 137649199, "page": 1, "part": "P1", "duration": 10}],
    },
}

PLAYURL_PAYLOAD = {
    "code": 0,
    "data": {
        "quality": 32,
        "support_formats": [{"quality": 32, "new_description": "480P 标清"}],
        "durl": [{"url": "https://cdn.test/progressive.flv", "size": 1234}],
    },
}


def _write_cookies(path: Path, *, bilibili: str, instagram: str) -> Path:
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        f".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\t{bilibili}\n"
        f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{instagram}\n",
        encoding="utf-8",
    )
    return path


def _mock_bilibili_api() -> None:
    respx.get(url__regex=rf"{NAV_URL}.*").mock(return_value=Response(200, json=NAV_PAYLOAD))
    respx.get(url__regex=rf"{VIEW_URL}.*").mock(return_value=Response(200, json=VIEW_PAYLOAD))
    respx.get(url__regex=rf"{PLAYURL_URL}.*").mock(return_value=Response(200, json=PLAYURL_PAYLOAD))


# --- the shared loader ------------------------------------------------------


def test_loads_a_cookie_header_from_a_netscape_file(tmp_path: Path) -> None:
    path = _write_cookies(tmp_path / "cookies.txt", bilibili="BILIVALUE", instagram="IGVALUE")

    header = load_cookie_header(path)

    assert header is not None
    assert "SESSDATA=BILIVALUE" in header
    assert "sessionid=IGVALUE" in header


def test_domain_filter_keeps_only_the_requested_site(tmp_path: Path) -> None:
    path = _write_cookies(tmp_path / "cookies.txt", bilibili="BILIVALUE", instagram="IGVALUE")

    header = load_cookie_header(path, domain_suffix="bilibili.com")

    assert header == "SESSDATA=BILIVALUE"
    assert "IGVALUE" not in header


def test_domain_filter_also_matches_subdomains(tmp_path: Path) -> None:
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\nwww.bilibili.com\tFALSE\t/\tTRUE\t0\tbili_jct\tTOKENVALUE\n",
        encoding="utf-8",
    )
    assert load_cookie_header(path, domain_suffix="bilibili.com") == "bili_jct=TOKENVALUE"


def test_missing_cookie_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_cookie_header(tmp_path / "nope.txt") is None
    assert load_cookie_header(None) is None


def test_unparsable_cookie_file_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.txt"
    path.write_text("this is not a netscape cookie file at all\n", encoding="utf-8")
    assert load_cookie_header(path) is None


def test_parse_failure_never_logs_a_cookie_value(tmp_path: Path, caplog) -> None:
    """The parser quotes the offending line, so its message must not be logged."""

    path = tmp_path / "broken.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\nwww.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tLEAKEDVALUE\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        assert load_cookie_header(path) is None

    assert "LEAKEDVALUE" not in caplog.text
    assert "broken.txt" in caplog.text


def test_cookie_file_without_a_matching_domain_returns_nothing(tmp_path: Path) -> None:
    path = _write_cookies(tmp_path / "cookies.txt", bilibili="B", instagram="I")
    assert load_cookie_header(path, domain_suffix="youtube.com") is None


# --- the two platforms stay separate ----------------------------------------


def test_bilibili_and_instagram_read_different_files(settings: Settings) -> None:
    settings.bilibili_cookiefile = "www.bilibili.com_cookies.txt"
    settings.ytdlp_cookiefile = "secrets/cookies.txt"

    assert settings.bilibili_cookie_file == settings.resolve_path(
        Path("www.bilibili.com_cookies.txt")
    )
    assert YtDlpEngine(settings, use_browser_cookies=True).cookie_file == settings.resolve_path(
        Path("secrets/cookies.txt")
    )


def test_changing_the_bilibili_file_does_not_affect_instagram(settings: Settings) -> None:
    settings.ytdlp_cookiefile = "secrets/cookies.txt"
    settings.bilibili_cookiefile = "www.bilibili.com_cookies.txt"
    instagram_engine = YtDlpEngine(settings, use_browser_cookies=True)
    before = instagram_engine.cookie_file

    settings.bilibili_cookiefile = "somewhere/else.txt"

    assert instagram_engine.cookie_file == before
    assert settings.bilibili_cookie_file == settings.resolve_path(Path("somewhere/else.txt"))


def test_instagram_engine_never_uses_the_bilibili_file(settings: Settings) -> None:
    settings.bilibili_cookiefile = "www.bilibili.com_cookies.txt"
    settings.ytdlp_cookiefile = None

    # YouTube-style engine (no opt-in) and Instagram engine both ignore it.
    assert YtDlpEngine(settings).cookie_file is None
    assert YtDlpEngine(settings, use_browser_cookies=True).cookie_file is None


# --- the adapter actually sends it ------------------------------------------


@respx.mock
async def test_bilibili_requests_carry_its_cookie(settings: Settings, tmp_path: Path) -> None:
    cookie_file = _write_cookies(
        tmp_path / "www.bilibili.com_cookies.txt", bilibili="BILIVALUE", instagram="IGVALUE"
    )
    settings.bilibili_cookiefile = str(cookie_file)
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    _mock_bilibili_api()

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        info = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    view_calls = [call for call in respx.calls if "/view" in str(call.request.url)]
    assert view_calls
    sent = view_calls[0].request.headers.get("cookie", "")
    assert "SESSDATA=BILIVALUE" in sent
    # The Instagram cookie must never travel to Bilibili.
    assert "IGVALUE" not in sent
    assert info.extra["requires_login_for_hd"] is False


@respx.mock
async def test_missing_cookie_file_falls_back_to_anonymous(
    settings: Settings, tmp_path: Path
) -> None:
    settings.bilibili_cookiefile = str(tmp_path / "does-not-exist.txt")
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    _mock_bilibili_api()

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        info = await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    view_calls = [call for call in respx.calls if "/view" in str(call.request.url)]
    assert view_calls
    assert "cookie" not in view_calls[0].request.headers
    # The request still succeeded, anonymously.
    assert info.video_id == "BV1GJ411x7h7"
    assert info.extra["requires_login_for_hd"] is True


@respx.mock
async def test_explicit_cookie_setting_wins_over_the_file(
    settings: Settings, tmp_path: Path
) -> None:
    cookie_file = _write_cookies(tmp_path / "cookies.txt", bilibili="FROMFILE", instagram="X")
    settings.bilibili_cookiefile = str(cookie_file)
    settings.bilibili_cookie = "SESSDATA=FROMCONFIG"
    _mock_bilibili_api()

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        await adapter.fetch_info("https://www.bilibili.com/video/BV1GJ411x7h7")

    view_calls = [call for call in respx.calls if "/view" in str(call.request.url)]
    sent = view_calls[0].request.headers.get("cookie", "")
    assert "SESSDATA=FROMCONFIG" in sent
    assert "FROMFILE" not in sent


@pytest.mark.parametrize("missing", [True, False])
def test_adapter_cookie_header_is_never_logged(
    settings: Settings, tmp_path: Path, caplog, missing: bool
) -> None:
    """Whatever the source, the cookie value must stay out of the logs."""

    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    if missing:
        settings.bilibili_cookiefile = str(tmp_path / "nope.txt")
    else:
        settings.bilibili_cookiefile = str(
            _write_cookies(tmp_path / "cookies.txt", bilibili="TOPSECRETVALUE", instagram="X")
        )

    with caplog.at_level("DEBUG"):
        client = httpx.AsyncClient()
        try:
            BilibiliAdapter(settings, client).session_cookie_header()
        finally:
            import asyncio

            asyncio.run(client.aclose())

    assert "TOPSECRETVALUE" not in caplog.text
