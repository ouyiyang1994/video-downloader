"""Sign-in: managed session storage, browser pickup, and per-platform checks.

The browser is never contacted here; ``yt_dlp.cookies`` is either monkeypatched
or left alone, so the suite stays offline. What is covered is everything the
project itself decides: where a session is written, which cookies may go into
it, how a browser failure is classified, and how each platform's own endpoint is
interpreted.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from config.settings import Settings
from core import browser_cookies, session_store
from core.browser_cookies import BrowserFailure, browser_from_progid, classify_failure
from core.exceptions import BrowserCookieError, SessionValueError
from core.http import load_cookie_header
from core.login import LoginSource, LoginState, header_has_cookie, parse_session_text
from core.models import Platform
from platforms.bilibili.adapter import BilibiliAdapter
from platforms.instagram.adapter import InstagramAdapter
from platforms.youtube.adapter import YouTubeAdapter

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
#: The "who am I" endpoint the status probe asks. The public-profile endpoint
#: (``users/web_profile_info``) is a scraping target that answers 429 to most
#: exit IPs, so it must never come back as the probe.
EDIT_FORM_URL = "https://www.instagram.com/api/v1/accounts/edit/web_form_data/"


def _edit_form(username: str = "someone") -> dict[str, object]:
    """A realistic account-settings answer for a signed-in session."""

    return {"status": "ok", "form_data": {"username": username, "email": "a@b.test"}}


def _cookie(name: str, value: str, domain: str) -> http.cookiejar.Cookie:
    return session_store.make_cookie(name, value, domain=domain)


def _write_env_cookie_file(path: Path) -> Path:
    path.write_text(
        "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tFROMFILE\n",
        encoding="utf-8",
    )
    return path


def _write_instagram_cookie_file(path: Path) -> Path:
    path.write_text(
        "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tFROMFILE\n",
        encoding="utf-8",
    )
    return path


# --- managed session storage -------------------------------------------------


def test_written_session_is_a_netscape_file_the_loader_can_read(
    settings: Settings,
) -> None:
    path = session_store.write_session(
        settings,
        Platform.BILIBILI,
        [_cookie("SESSDATA", "VALUE", ".bilibili.com")],
        domain_suffix="bilibili.com",
    )

    assert path == session_store.session_file(settings, Platform.BILIBILI)
    assert path is not None
    assert path.read_text(encoding="utf-8").startswith("# Netscape HTTP Cookie File")
    assert load_cookie_header(path, domain_suffix="bilibili.com") == "SESSDATA=VALUE"


def test_only_the_platform_domain_is_written(settings: Settings) -> None:
    path = session_store.write_session(
        settings,
        Platform.BILIBILI,
        [
            _cookie("SESSDATA", "bili", ".bilibili.com"),
            _cookie("sessionid", "ig", ".instagram.com"),
            _cookie("unrelated", "x", ".example.com"),
        ],
        domain_suffix="bilibili.com",
    )

    text = path.read_text(encoding="utf-8") if path else ""
    assert "SESSDATA" in text
    assert "sessionid" not in text
    assert "unrelated" not in text


def test_nothing_to_write_returns_none(settings: Settings) -> None:
    path = session_store.write_session(
        settings,
        Platform.BILIBILI,
        [_cookie("sessionid", "ig", ".instagram.com")],
        domain_suffix="bilibili.com",
    )
    assert path is None
    assert not session_store.has_session(settings, Platform.BILIBILI)


def test_sessions_are_isolated_between_platforms(settings: Settings) -> None:
    session_store.write_session(
        settings,
        Platform.BILIBILI,
        [_cookie("SESSDATA", "bili", ".bilibili.com")],
        domain_suffix="bilibili.com",
    )
    session_store.write_session(
        settings,
        Platform.INSTAGRAM,
        [_cookie("sessionid", "ig", ".instagram.com")],
        domain_suffix="instagram.com",
    )

    assert session_store.has_session(settings, Platform.BILIBILI)
    assert session_store.has_session(settings, Platform.INSTAGRAM)

    assert session_store.delete_session(settings, Platform.BILIBILI) is True
    assert not session_store.has_session(settings, Platform.BILIBILI)
    assert session_store.has_session(settings, Platform.INSTAGRAM), "另一个平台必须不受影响"


def test_deleting_a_missing_session_is_not_an_error(settings: Settings) -> None:
    assert session_store.delete_session(settings, Platform.BILIBILI) is False


def test_session_files_live_under_the_configured_directory(settings: Settings) -> None:
    path = session_store.session_file(settings, Platform.INSTAGRAM)
    assert path.parent == settings.session_dir
    assert path.name == "instagram_cookies.txt"


# --- manual session value ----------------------------------------------------


def test_a_full_cookie_header_is_parsed_pair_by_pair() -> None:
    cookies = parse_session_text(
        "SESSDATA=a%2Cb; bili_jct=xyz; DedeUserID=7",
        cookie_names=("SESSDATA",),
        domain=".bilibili.com",
    )
    assert {cookie.name for cookie in cookies} == {"SESSDATA", "bili_jct", "DedeUserID"}
    assert next(c for c in cookies if c.name == "SESSDATA").value == "a%2Cb"


def test_a_bare_value_becomes_the_primary_cookie() -> None:
    cookies = parse_session_text("abc123", cookie_names=("sessionid",), domain=".instagram.com")
    assert len(cookies) == 1
    assert cookies[0].name == "sessionid"
    assert cookies[0].value == "abc123"
    assert cookies[0].domain == ".instagram.com"


def test_a_leading_cookie_prefix_is_stripped() -> None:
    cookies = parse_session_text(
        "Cookie: SESSDATA=v", cookie_names=("SESSDATA",), domain=".bilibili.com"
    )
    assert [cookie.name for cookie in cookies] == ["SESSDATA"]


def test_newlines_are_treated_as_separators() -> None:
    cookies = parse_session_text(
        "SESSDATA=v\nbili_jct=w", cookie_names=("SESSDATA",), domain=".bilibili.com"
    )
    assert {cookie.name for cookie in cookies} == {"SESSDATA", "bili_jct"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "nosuchcookie=1",
        "this is not a cookie at all=maybe",
    ],
)
def test_unusable_text_is_rejected(text: str) -> None:
    with pytest.raises(SessionValueError):
        parse_session_text(text, cookie_names=("SESSDATA",), domain=".bilibili.com")


def test_oversized_text_is_rejected() -> None:
    with pytest.raises(SessionValueError):
        parse_session_text(
            "SESSDATA=" + "a" * 9000, cookie_names=("SESSDATA",), domain=".bilibili.com"
        )


def test_error_messages_never_contain_the_value() -> None:
    secret = "SUPERSECRETSESSIONVALUE"
    with pytest.raises(SessionValueError) as info:
        parse_session_text(
            f"wrongname={secret}", cookie_names=("SESSDATA",), domain=".bilibili.com"
        )
    assert secret not in str(info.value)
    assert secret not in (info.value.detail or "")


# --- browser detection and failure classification ----------------------------


@pytest.mark.parametrize(
    ("prog_id", "expected"),
    [
        ("ChromeHTML", "chrome"),
        ("MSEdgeHTM", "edge"),
        ("FirefoxURL-308046B0AF4A39CB", "firefox"),
        ("BraveHTML", "brave"),
        ("VivaldiHTM", "vivaldi"),
        ("OperaStable", "opera"),
        ("SomethingElse", None),
        ("", None),
    ],
)
def test_shell_progid_maps_to_a_yt_dlp_browser(prog_id: str, expected: str | None) -> None:
    assert browser_from_progid(prog_id) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Could not copy Chrome cookie database. See https://...", BrowserFailure.LOCKED),
        ("Failed to decrypt with DPAPI. See https://...", BrowserFailure.ENCRYPTED),
        ("could not find chrome cookies database in 'C:\\...'", BrowserFailure.NOT_INSTALLED),
        ("unsupported browser: netscape", BrowserFailure.NOT_INSTALLED),
        ("something nobody predicted", BrowserFailure.UNKNOWN),
    ],
)
def test_browser_failures_are_classified(message: str, expected: BrowserFailure) -> None:
    assert classify_failure(message) == expected


def test_app_bound_encryption_message_points_at_the_way_out() -> None:
    message, hint = browser_cookies.failure_message("chrome", BrowserFailure.ENCRYPTED)
    assert "App-Bound" in message
    assert "手动填写会话" in hint


def test_locked_database_message_asks_the_user_to_close_the_browser() -> None:
    message, hint = browser_cookies.failure_message("edge", BrowserFailure.LOCKED)
    assert "占用" in message
    assert "退出" in hint


def test_extract_session_returns_the_first_browser_that_works(monkeypatch) -> None:
    cookie = _cookie("sessionid", "v", ".instagram.com")
    monkeypatch.setattr(browser_cookies, "candidate_browsers", lambda: ["firefox", "chrome"])
    monkeypatch.setattr(
        browser_cookies, "extract_cookies", lambda browser, *, domain_suffix: [cookie]
    )

    session = browser_cookies.extract_session("instagram.com")

    assert session.browser == "firefox"
    assert session.cookie_names == ["sessionid"]


def test_extract_session_reports_every_browser_it_tried(monkeypatch) -> None:
    monkeypatch.setattr(browser_cookies, "candidate_browsers", lambda: ["chrome", "edge"])

    def _always_encrypted(browser: str, *, domain_suffix: str) -> list[object]:
        raise BrowserCookieError(
            "cannot decrypt",
            reason=BrowserFailure.ENCRYPTED.value,
            browser=browser,
        )

    monkeypatch.setattr(browser_cookies, "extract_cookies", _always_encrypted)

    with pytest.raises(BrowserCookieError) as info:
        browser_cookies.extract_session("bilibili.com")

    assert info.value.reason == BrowserFailure.ENCRYPTED.value
    detail = info.value.detail or ""
    assert "Chrome" in detail
    assert "Edge" in detail


def test_extract_session_without_any_browser(monkeypatch) -> None:
    monkeypatch.setattr(browser_cookies, "candidate_browsers", lambda: [])

    with pytest.raises(BrowserCookieError) as info:
        browser_cookies.extract_session("bilibili.com")

    assert info.value.reason == BrowserFailure.NOT_INSTALLED.value
    assert "手动填写会话" in (info.value.detail or "")


# --- Bilibili session check --------------------------------------------------


@respx.mock
async def test_bilibili_reports_a_live_session(settings: Settings) -> None:
    respx.get(url__regex=f"{NAV_URL}.*").mock(
        return_value=Response(
            200, json={"code": 0, "data": {"isLogin": True, "uname": "测试用户", "mid": 7}}
        )
    )

    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session("SESSDATA=live")

    assert status.state is LoginState.LOGGED_IN
    assert status.account == "测试用户"
    assert status.logged_in is True


@respx.mock
async def test_bilibili_reports_an_expired_session(settings: Settings) -> None:
    respx.get(url__regex=f"{NAV_URL}.*").mock(
        return_value=Response(200, json={"code": -101, "message": "账号未登录", "data": {}})
    )

    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session("SESSDATA=stale")

    assert status.state is LoginState.EXPIRED


@respx.mock
async def test_bilibili_anonymous_answer_is_expired_not_an_error(settings: Settings) -> None:
    respx.get(url__regex=f"{NAV_URL}.*").mock(
        return_value=Response(200, json={"code": 0, "data": {"isLogin": False}})
    )

    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session("SESSDATA=stale")

    assert status.state is LoginState.EXPIRED


async def test_bilibili_without_a_cookie_is_logged_out(settings: Settings) -> None:
    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session(None)

    assert status.state is LoginState.LOGGED_OUT


@respx.mock
async def test_bilibili_risk_control_is_not_a_verdict(settings: Settings) -> None:
    respx.get(url__regex=f"{NAV_URL}.*").mock(return_value=Response(412))

    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session("SESSDATA=live")

    assert status.state is LoginState.UNKNOWN
    assert "412" in status.detail


@respx.mock
async def test_bilibili_network_failure_is_not_a_verdict(settings: Settings) -> None:
    respx.get(url__regex=f"{NAV_URL}.*").mock(side_effect=httpx.ConnectError("boom"))

    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session("SESSDATA=live")

    assert status.state is LoginState.UNKNOWN


# --- Instagram session check -------------------------------------------------


@respx.mock
async def test_instagram_reports_a_live_session(settings: Settings) -> None:
    route = respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(
        return_value=Response(200, json=_edit_form("someone"))
    )

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=live")

    assert status.state is LoginState.LOGGED_IN
    assert status.account == "someone"
    assert route.called, "探针必须请求 accounts/edit/web_form_data"


def test_the_probe_is_the_edit_form_not_the_scraping_endpoint() -> None:
    """Guard the fix: the public-profile endpoint must not come back.

    It answers HTTP 429 to most exit IPs, which made a valid sign-in look
    broken. The probe has to be the endpoint the signed-in web app itself uses.
    """

    from platforms.instagram import adapter as instagram_adapter

    assert instagram_adapter.ACCOUNT_EDIT_API == EDIT_FORM_URL
    assert "web_profile_info" not in instagram_adapter.ACCOUNT_EDIT_API
    assert not hasattr(instagram_adapter, "PROFILE_INFO_API")


@respx.mock
async def test_instagram_rate_limit_is_not_a_verdict(settings: Settings) -> None:
    """HTTP 429 is rate limiting, not a rejected session."""

    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(return_value=Response(429))

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=live")

    assert status.state is LoginState.UNKNOWN
    assert status.logged_in is False
    assert "429" in status.detail
    assert "限流" in status.detail


@respx.mock
async def test_instagram_200_without_an_account_is_expired(settings: Settings) -> None:
    """A ``200`` with no account in it must not be read as a live session."""

    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(
        return_value=Response(200, json={"status": "ok", "form_data": {}})
    )

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=stale")

    assert status.state is LoginState.EXPIRED


@respx.mock
async def test_instagram_login_wall_means_expired(settings: Settings) -> None:
    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(
        return_value=Response(
            302, headers={"location": "https://www.instagram.com/accounts/login/"}
        )
    )

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=stale")

    assert status.state is LoginState.EXPIRED


@respx.mock
async def test_instagram_homepage_redirect_means_expired(settings: Settings) -> None:
    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(
        return_value=Response(302, headers={"location": "https://www.instagram.com/"})
    )

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=stale")

    assert status.state is LoginState.EXPIRED


@pytest.mark.parametrize("code", [401, 403])
@respx.mock
async def test_instagram_rejection_means_expired(settings: Settings, code: int) -> None:
    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(return_value=Response(code))

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=stale")

    assert status.state is LoginState.EXPIRED


@respx.mock
async def test_instagram_json_error_body_means_expired(settings: Settings) -> None:
    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(
        return_value=Response(200, json={"message": "login required", "status": "fail"})
    )

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=stale")

    assert status.state is LoginState.EXPIRED


async def test_instagram_without_a_cookie_is_logged_out(settings: Settings) -> None:
    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session(None)

    assert status.state is LoginState.LOGGED_OUT


@respx.mock
async def test_instagram_network_failure_is_not_a_verdict(settings: Settings) -> None:
    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(side_effect=httpx.ConnectError("boom"))

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("sessionid=live")

    assert status.state is LoginState.UNKNOWN


# --- how the download path picks up a session --------------------------------


async def test_instagram_engine_prefers_the_managed_session(settings: Settings) -> None:
    session_store.write_session(
        settings,
        Platform.INSTAGRAM,
        [_cookie("sessionid", "MANAGED", ".instagram.com")],
        domain_suffix="instagram.com",
    )

    async with httpx.AsyncClient() as client:
        adapter = InstagramAdapter(settings, client)

        assert adapter.engine.cookie_file == session_store.session_file(
            settings, Platform.INSTAGRAM
        )
        assert adapter.session_cookie_header() == "sessionid=MANAGED"


async def test_bilibili_managed_session_beats_the_env_cookie_file(
    settings: Settings, tmp_path: Path
) -> None:
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = str(_write_env_cookie_file(tmp_path / "env-cookies.txt"))
    session_store.write_session(
        settings,
        Platform.BILIBILI,
        [_cookie("SESSDATA", "MANAGED", ".bilibili.com")],
        domain_suffix="bilibili.com",
    )

    async with httpx.AsyncClient() as client:
        assert BilibiliAdapter(settings, client).session_cookie_header() == "SESSDATA=MANAGED"


async def test_bilibili_explicit_env_cookie_still_wins(settings: Settings, tmp_path: Path) -> None:
    settings.bilibili_cookie = "SESSDATA=FROMCONFIG"
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = str(_write_env_cookie_file(tmp_path / "env-cookies.txt"))
    session_store.write_session(
        settings,
        Platform.BILIBILI,
        [_cookie("SESSDATA", "MANAGED", ".bilibili.com")],
        domain_suffix="bilibili.com",
    )

    async with httpx.AsyncClient() as client:
        assert BilibiliAdapter(settings, client).session_cookie_header() == "SESSDATA=FROMCONFIG"


async def test_bilibili_env_cookie_file_still_works_without_a_managed_session(
    settings: Settings, tmp_path: Path
) -> None:
    """The pre-1.03 flow must keep working exactly as it did."""

    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = str(_write_env_cookie_file(tmp_path / "env-cookies.txt"))

    async with httpx.AsyncClient() as client:
        assert BilibiliAdapter(settings, client).session_cookie_header() == "SESSDATA=FROMFILE"


async def test_bilibili_session_cache_is_dropped_on_reload(settings: Settings) -> None:
    """Signing in or out must not leave the adapter reporting a stale session.

    The window keeps one registry for its whole lifetime, so the adapter that
    answers "am I logged in" is the same object the user just changed the
    session under.
    """

    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = ""

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        assert adapter.session_cookie_header() is None

        session_store.write_session(
            settings,
            Platform.BILIBILI,
            [_cookie("SESSDATA", "MANAGED", ".bilibili.com")],
            domain_suffix="bilibili.com",
        )
        assert adapter.session_cookie_header() is None, "缓存未失效前仍是旧答案"

        adapter.reload_session()
        assert adapter.session_cookie_header() == "SESSDATA=MANAGED"

        session_store.delete_session(settings, Platform.BILIBILI)
        adapter.reload_session()
        assert adapter.session_cookie_header() is None, "退出登录后不能再读到旧会话"


# --- which source the login state comes from ---------------------------------


async def test_instagram_reports_the_managed_session_as_its_own(
    settings: Settings, tmp_path: Path
) -> None:
    """A session this program stored is the one 「退出登录」 can delete."""

    settings.ytdlp_cookiefile = str(_write_instagram_cookie_file(tmp_path / "env-cookies.txt"))
    session_store.write_session(
        settings,
        Platform.INSTAGRAM,
        [_cookie("sessionid", "MANAGED", ".instagram.com")],
        domain_suffix="instagram.com",
    )

    async with httpx.AsyncClient() as client:
        adapter = InstagramAdapter(settings, client)
        origin = adapter.session_origin()

    assert origin.source is LoginSource.MANAGED
    assert origin.key is None
    assert origin.managed is True
    assert origin.configured is False


async def test_instagram_names_the_env_cookie_file_fallback(
    settings: Settings, tmp_path: Path
) -> None:
    """Without a managed session the verdict comes from ``YTDLP_COOKIEFILE``."""

    settings.ytdlp_cookiefile = str(_write_instagram_cookie_file(tmp_path / "env-cookies.txt"))

    async with httpx.AsyncClient() as client:
        adapter = InstagramAdapter(settings, client)
        origin = adapter.session_origin()

    assert origin.source is LoginSource.ENV_COOKIEFILE
    assert origin.key == "YTDLP_COOKIEFILE"
    assert origin.configured is True
    assert adapter.fallback_config_keys() == ("YTDLP_COOKIEFILE",)


async def test_instagram_reports_no_source_without_any_session(settings: Settings) -> None:
    settings.ytdlp_cookiefile = None

    async with httpx.AsyncClient() as client:
        adapter = InstagramAdapter(settings, client)
        origin = adapter.session_origin()

    assert origin.source is LoginSource.NONE
    assert adapter.fallback_config_keys() == ()


async def test_bilibili_names_the_env_cookie_file_fallback(
    settings: Settings, tmp_path: Path
) -> None:
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = str(_write_env_cookie_file(tmp_path / "env-cookies.txt"))

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        origin = adapter.session_origin()

    assert origin.source is LoginSource.ENV_COOKIEFILE
    assert origin.key == "BILIBILI_COOKIEFILE"
    assert adapter.fallback_config_keys() == ("BILIBILI_COOKIEFILE",)


async def test_bilibili_reports_the_managed_session_as_its_own(
    settings: Settings, tmp_path: Path
) -> None:
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = str(_write_env_cookie_file(tmp_path / "env-cookies.txt"))
    session_store.write_session(
        settings,
        Platform.BILIBILI,
        [_cookie("SESSDATA", "MANAGED", ".bilibili.com")],
        domain_suffix="bilibili.com",
    )

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        origin = adapter.session_origin()

    assert origin.source is LoginSource.MANAGED
    # The fallback still exists and must still be named in the warning.
    assert adapter.fallback_config_keys() == ("BILIBILI_COOKIEFILE",)


async def test_bilibili_names_a_pasted_env_value(settings: Settings) -> None:
    settings.bilibili_cookie = "SESSDATA=FROMCONFIG"
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = ""

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        origin = adapter.session_origin()

    assert origin.source is LoginSource.ENV_VALUE
    assert origin.key == "BILIBILI_COOKIE"
    assert adapter.fallback_config_keys() == ("BILIBILI_COOKIE",)


async def test_bilibili_reports_no_source_without_any_session(settings: Settings) -> None:
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = ""

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        origin = adapter.session_origin()
        assert adapter.session_cookie_header() is None

    assert origin.source is LoginSource.NONE
    assert adapter.fallback_config_keys() == ()


# --- which platforms offer sign-in -------------------------------------------


def test_only_bilibili_and_instagram_advertise_sign_in(settings: Settings) -> None:
    async def _run() -> None:
        async with httpx.AsyncClient() as client:
            bilibili = BilibiliAdapter(settings, client)
            instagram = InstagramAdapter(settings, client)
            youtube = YouTubeAdapter(settings, client)

            assert bilibili.login_supported is True
            assert instagram.login_supported is True
            assert youtube.login_supported is False

            assert bilibili.session_cookie_names == ("SESSDATA",)
            assert instagram.session_cookie_names == ("sessionid",)
            assert bilibili.session_domain == "bilibili.com"
            assert instagram.session_domain == "instagram.com"

    asyncio.run(_run())


def test_sign_in_urls_are_the_official_pages() -> None:
    assert BilibiliAdapter.login_url == "https://passport.bilibili.com/login"
    assert InstagramAdapter.login_url == "https://www.instagram.com/accounts/login/"


# --- which browsers can actually be read -------------------------------------


class FakeBrowsers:
    """A throw-away browser layout, so no real profile is ever inspected."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dirs: dict[str, Path] = {}
        self.default: str | None = None
        self.root.mkdir(parents=True, exist_ok=True)

    def add(self, browser: str, *, app_bound: bool = False) -> Path:
        """Register ``browser`` as installed, optionally with ABE enabled."""

        directory = self.root / browser
        directory.mkdir(parents=True, exist_ok=True)
        os_crypt: dict[str, str] = {"encrypted_key": "RFBBUEkB"}
        if app_bound:
            os_crypt["app_bound_encrypted_key"] = "AQAAA"
        (directory / "Local State").write_text(json.dumps({"os_crypt": os_crypt}), encoding="utf-8")
        self.dirs[browser] = directory
        return directory

    def use_as_default(self, browser: str | None) -> None:
        self.default = browser


@pytest.fixture
def browsers(monkeypatch, tmp_path: Path) -> FakeBrowsers:
    fake = FakeBrowsers(tmp_path / "browsers")
    monkeypatch.setattr(browser_cookies, "browser_data_dir", lambda browser: fake.dirs.get(browser))
    monkeypatch.setattr(browser_cookies, "default_browser", lambda: fake.default)
    return fake


@pytest.mark.parametrize(
    ("browser", "readable"),
    [
        ("firefox", True),
        ("chrome", False),
        ("edge", False),
        ("brave", False),
        ("vivaldi", False),
    ],
)
def test_only_firefox_is_readable(browser: str, readable: bool) -> None:
    assert browser_cookies.is_readable(browser) is readable
    assert browser_cookies.is_chromium_based(browser) is (not readable)


@pytest.mark.parametrize("browser", ["chrome", "edge", "brave", "vivaldi", "opera"])
def test_app_bound_encryption_is_detected_from_local_state(
    browsers: FakeBrowsers, browser: str
) -> None:
    browsers.add(browser, app_bound=True)
    assert browser_cookies.app_bound_encryption_enabled(browser) is True


@pytest.mark.parametrize("browser", ["chrome", "edge"])
def test_older_chromium_without_the_marker_is_not_flagged(
    browsers: FakeBrowsers, browser: str
) -> None:
    browsers.add(browser, app_bound=False)
    assert browser_cookies.app_bound_encryption_enabled(browser) is False


def test_firefox_has_no_local_state_to_inspect(browsers: FakeBrowsers) -> None:
    browsers.add("firefox")
    assert browser_cookies.local_state_path("firefox") is None
    assert browser_cookies.app_bound_encryption_enabled("firefox") is False


def test_a_missing_browser_is_never_reported_as_encrypted(browsers: FakeBrowsers) -> None:
    assert browser_cookies.app_bound_encryption_enabled("chrome") is False


def test_an_unreadable_local_state_is_treated_as_not_encrypted(
    browsers: FakeBrowsers,
) -> None:
    directory = browsers.add("chrome")
    (directory / "Local State").write_text("not json at all", encoding="utf-8")
    assert browser_cookies.app_bound_encryption_enabled("chrome") is False


def test_candidate_order_puts_firefox_before_the_rest(browsers: FakeBrowsers) -> None:
    """Chrome is tried first because the page was opened there, but the only
    browser that can actually succeed must not be last."""

    for browser in ("chrome", "edge", "firefox", "brave"):
        browsers.add(browser)
    browsers.use_as_default("chrome")

    assert browser_cookies.candidate_browsers() == ["chrome", "firefox", "edge", "brave"]


def test_candidate_order_keeps_a_readable_default_first(browsers: FakeBrowsers) -> None:
    for browser in ("firefox", "chrome"):
        browsers.add(browser)
    browsers.use_as_default("firefox")

    assert browser_cookies.candidate_browsers() == ["firefox", "chrome"]


def test_candidate_order_without_a_detectable_default(browsers: FakeBrowsers) -> None:
    for browser in ("chrome", "firefox"):
        browsers.add(browser)
    browsers.use_as_default(None)

    assert browser_cookies.candidate_browsers() == ["firefox", "chrome"]


# --- what to tell the user before they even try ------------------------------


def test_outlook_says_a_readable_default_just_works(browsers: FakeBrowsers) -> None:
    browsers.add("firefox")
    browsers.use_as_default("firefox")

    outlook = browser_cookies.automatic_read_outlook()

    assert outlook.possible is True
    assert outlook.default_readable is True
    assert "可以直接读取" in outlook.advice()


def test_outlook_prefers_firefox_when_the_default_is_blocked(
    browsers: FakeBrowsers,
) -> None:
    browsers.add("chrome", app_bound=True)
    browsers.add("firefox")
    browsers.use_as_default("chrome")

    outlook = browser_cookies.automatic_read_outlook()

    assert outlook.possible is True
    assert outlook.default_readable is False
    assert outlook.firefox_available is True
    advice = outlook.advice()
    assert "App-Bound Encryption" in advice
    assert "Firefox" in advice
    assert "手动填写会话" in advice


def test_outlook_offers_the_chrome_helper_when_only_chromium_exists(
    browsers: FakeBrowsers,
) -> None:
    """Requirement: App-Bound Encryption must not read as "cannot sign in".

    The extension is the route Chrome itself provides, so it leads - and manual
    entry stays available as the last resort.
    """

    browsers.add("chrome", app_bound=True)
    browsers.add("edge", app_bound=True)
    browsers.use_as_default("chrome")

    outlook = browser_cookies.automatic_read_outlook()

    assert outlook.possible is False
    advice = outlook.advice()
    assert "App-Bound Encryption" in advice
    assert "Chrome 登录助手" in advice
    assert "手动填写会话" in advice
    assert advice.index("Chrome 登录助手") < advice.index("手动填写会话")


def test_outlook_with_no_browser_at_all(browsers: FakeBrowsers) -> None:
    outlook = browser_cookies.automatic_read_outlook()

    assert outlook.any_installed is False
    assert outlook.possible is False
    assert "未检测到任何已安装的浏览器" in outlook.advice()


def test_app_bound_message_leads_with_the_extension_then_firefox() -> None:
    message, hint = browser_cookies.failure_message("chrome", BrowserFailure.ENCRYPTED)
    assert "App-Bound" in message
    assert hint.index("Chrome 登录助手") < hint.index("Firefox"), "扩展必须是首选方案"
    assert hint.index("Firefox") < hint.index("手动填写会话"), "Firefox 优先于手动填写"


def test_extraction_reports_encryption_when_the_marker_says_so(
    browsers: FakeBrowsers, monkeypatch
) -> None:
    """Even when yt-dlp blames a locked database, v20 encryption is the truth."""

    browsers.add("chrome", app_bound=True)

    def _locked(*args: object, **kwargs: object) -> object:
        raise RuntimeError("Could not copy Chrome cookie database")

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", _locked)

    with pytest.raises(BrowserCookieError) as info:
        browser_cookies.extract_cookies("chrome", domain_suffix="bilibili.com")

    assert info.value.reason == BrowserFailure.ENCRYPTED.value


def test_extraction_keeps_the_locked_reason_when_encryption_is_not_in_play(
    browsers: FakeBrowsers, monkeypatch
) -> None:
    browsers.add("chrome", app_bound=False)

    def _locked(*args: object, **kwargs: object) -> object:
        raise RuntimeError("Could not copy Chrome cookie database")

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", _locked)

    with pytest.raises(BrowserCookieError) as info:
        browser_cookies.extract_cookies("chrome", domain_suffix="bilibili.com")

    assert info.value.reason == BrowserFailure.LOCKED.value


def test_aggregate_failure_mentions_installing_firefox(browsers: FakeBrowsers, monkeypatch) -> None:
    browsers.add("chrome", app_bound=True)
    browsers.use_as_default("chrome")

    def _blocked(browser: str, *, domain_suffix: str) -> list[object]:
        raise BrowserCookieError(
            "cannot decrypt", reason=BrowserFailure.ENCRYPTED.value, browser=browser
        )

    monkeypatch.setattr(browser_cookies, "extract_cookies", _blocked)

    with pytest.raises(BrowserCookieError) as info:
        browser_cookies.extract_session("bilibili.com")

    detail = info.value.detail or ""
    assert "未检测到 Firefox" in detail
    assert "Chrome" in detail


def test_aggregate_failure_stays_quiet_when_firefox_is_present(
    browsers: FakeBrowsers, monkeypatch
) -> None:
    browsers.add("chrome", app_bound=True)
    browsers.add("firefox")
    browsers.use_as_default("chrome")

    def _blocked(browser: str, *, domain_suffix: str) -> list[object]:
        raise BrowserCookieError(
            "cannot decrypt", reason=BrowserFailure.ENCRYPTED.value, browser=browser
        )

    monkeypatch.setattr(browser_cookies, "extract_cookies", _blocked)

    with pytest.raises(BrowserCookieError) as info:
        browser_cookies.extract_session("bilibili.com")

    assert "未检测到 Firefox" not in (info.value.detail or "")


# --- a cookie header without the auth cookie is not a session ----------------


@pytest.mark.parametrize(
    ("header", "names", "expected"),
    [
        (None, ("SESSDATA",), False),
        ("", ("SESSDATA",), False),
        ("SESSDATA=value", ("SESSDATA",), True),
        ("buvid3=x; SESSDATA=value; bili_jct=y", ("SESSDATA",), True),
        ("buvid3=x; bili_jct=y", ("SESSDATA",), False),
        ("sessionid=value", ("SESSDATA",), False),
        ("SESSDATA=value", (), False),
    ],
)
def test_header_has_cookie(header: str | None, names: tuple[str, ...], expected: bool) -> None:
    assert header_has_cookie(header, names) is expected


@respx.mock
async def test_bilibili_without_sessdata_never_calls_the_api(settings: Settings) -> None:
    """Do not ask the platform about a header that cannot be a session."""

    route = respx.get(url__regex=f"{NAV_URL}.*").mock(
        return_value=Response(200, json={"code": 0, "data": {"isLogin": True, "uname": "x"}})
    )

    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session("buvid3=anonymous")

    assert status.state is LoginState.LOGGED_OUT
    assert route.call_count == 0, "没有 SESSDATA 时不应发起请求"


@respx.mock
async def test_instagram_without_sessionid_never_calls_the_api(settings: Settings) -> None:
    route = respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(
        return_value=Response(200, json=_edit_form())
    )

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("csrftoken=anonymous")

    assert status.state is LoginState.LOGGED_OUT
    assert route.call_count == 0, "没有 sessionid 时不应发起请求"


@respx.mock
async def test_instagram_does_not_trust_a_positive_answer_without_sessionid(
    settings: Settings,
) -> None:
    """Even a 200 must not become "已登录" when the auth cookie is missing."""

    respx.get(url__regex=f"{EDIT_FORM_URL}.*").mock(return_value=Response(200, json=_edit_form()))

    async with httpx.AsyncClient() as client:
        status = await InstagramAdapter(settings, client).check_session("csrftoken=only")

    assert status.logged_in is False


@respx.mock
async def test_bilibili_unexpected_code_is_not_a_verdict(settings: Settings) -> None:
    """Only the two documented answers may be read as a verdict."""

    respx.get(url__regex=f"{NAV_URL}.*").mock(
        return_value=Response(200, json={"code": -400, "message": "请求错误", "data": {}})
    )

    async with httpx.AsyncClient() as client:
        status = await BilibiliAdapter(settings, client).check_session("SESSDATA=live")

    assert status.state is LoginState.UNKNOWN
    assert "-400" in status.detail
    assert status.logged_in is False


# --- yt-dlp chatter must never reach the log ---------------------------------


def test_quiet_logger_drops_every_message(caplog) -> None:
    """yt-dlp's cookie messages are not guaranteed value-free, so none is kept."""

    secret = "SUPERSECRETSESSIONVALUE"
    quiet = browser_cookies._QuietLogger()

    with caplog.at_level("DEBUG"):
        quiet.debug(f"cookie {secret}")
        quiet.info(f"cookie {secret}")
        quiet.warning(f"cookie {secret}")
        quiet.error(f"cookie {secret}")

    assert secret not in caplog.text
