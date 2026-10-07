"""Browser-cookie wiring: opt-in per platform, with clear failures."""

from __future__ import annotations

import httpx
import pytest

from config.settings import PROJECT_ROOT, Settings
from core.engine_ytdlp import YtDlpEngine
from core.exceptions import AuthRequiredError, CookieAccessError, MetadataError
from core.models import Platform
from platforms.instagram.adapter import InstagramAdapter
from platforms.youtube.adapter import YouTubeAdapter


def test_youtube_engine_never_requests_browser_cookies(settings: Settings) -> None:
    settings.ytdlp_cookies_from_browser = "chrome"
    engine = YtDlpEngine(settings)  # default: not opted in
    assert engine.browser_cookie_source is None
    assert "cookiesfrombrowser" not in engine._ydl_options()


def test_opted_in_engine_passes_browser_to_ytdlp(settings: Settings) -> None:
    settings.ytdlp_cookies_from_browser = "chrome"
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    assert engine.browser_cookie_source == "chrome"
    assert engine._ydl_options()["cookiesfrombrowser"] == ("chrome",)


def test_opted_in_without_configuration_stays_cookie_less(settings: Settings) -> None:
    settings.ytdlp_cookies_from_browser = None
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    assert engine.browser_cookie_source is None
    assert "cookiesfrombrowser" not in engine._ydl_options()


def test_blank_configuration_stays_cookie_less(settings: Settings) -> None:
    settings.ytdlp_cookies_from_browser = "   "
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    assert engine.browser_cookie_source is None


def test_browser_name_is_normalised(settings: Settings) -> None:
    settings.ytdlp_cookies_from_browser = "  Chrome "
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    assert engine.browser_cookie_source == "chrome"


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: Could not copy Chrome cookie database. See "
        "https://github.com/yt-dlp/yt-dlp/issues/7271",
        "ERROR: could not find chrome cookies database in "
        '"C:\\Users\\x\\AppData\\Local\\Google\\Chrome\\User Data"',
    ],
)
def test_locked_cookie_database_is_explained(settings: Settings, message: str) -> None:
    settings.ytdlp_cookies_from_browser = "chrome"
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    error = engine._translate(Exception(message))
    assert isinstance(error, CookieAccessError)
    assert "chrome" in str(error)
    assert "退出" in (error.detail or "")


def test_app_bound_encryption_failure_is_explained(settings: Settings) -> None:
    """Chromium 127+ v20 cookies cannot be decrypted by yt-dlp at all."""

    settings.ytdlp_cookies_from_browser = "chrome"
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    error = engine._translate(
        Exception(
            "ERROR: Failed to decrypt with DPAPI. See https://github.com/yt-dlp/yt-dlp/issues/10927"
        )
    )
    assert isinstance(error, CookieAccessError)
    assert "App-Bound Encryption" in str(error)
    detail = error.detail or ""
    assert "Firefox" in detail
    # The actionable advice must not be "just close the browser again".
    assert "退出浏览器" not in detail


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [Instagram] abc: Instagram sent an empty media response. "
        "... use --cookies-from-browser",
        "ERROR: Sign in to confirm you're not a bot",
    ],
)
def test_login_requirement_with_browser_cookies_configured(
    settings: Settings, message: str
) -> None:
    settings.ytdlp_cookies_from_browser = "chrome"
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    error = engine._translate(Exception(message))
    assert isinstance(error, AuthRequiredError)
    assert "chrome" in str(error)


def test_login_requirement_without_configuration_points_at_env(
    settings: Settings,
) -> None:
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    error = engine._translate(Exception("Fresh cookies (not necessarily logged in) are needed"))
    assert isinstance(error, AuthRequiredError)
    assert "YTDLP_COOKIES_FROM_BROWSER" in (error.detail or "")


def test_unrelated_failure_stays_generic(settings: Settings) -> None:
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    error = engine._translate(Exception("Unable to download webpage: connection reset"))
    assert isinstance(error, MetadataError)
    assert not isinstance(error, (CookieAccessError, AuthRequiredError))


async def test_instagram_opts_in_youtube_does_not(settings: Settings) -> None:
    settings.ytdlp_cookies_from_browser = "chrome"
    async with httpx.AsyncClient() as client:
        assert InstagramAdapter(settings, client).engine.use_browser_cookies is True
        assert YouTubeAdapter(settings, client).engine.use_browser_cookies is False


# --- exported cookies.txt ---------------------------------------------------


def test_cookie_file_wins_over_browser_source(settings: Settings, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings.ytdlp_cookies_from_browser = "chrome"
    settings.ytdlp_cookiefile = str(cookie_file)

    options = YtDlpEngine(settings, use_browser_cookies=True)._ydl_options()
    assert options["cookiefile"] == str(cookie_file)
    assert "cookiesfrombrowser" not in options


def test_relative_cookie_file_resolves_against_project_root(settings: Settings) -> None:
    settings.ytdlp_cookiefile = "secrets/cookies.txt"
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    assert engine.cookie_file == PROJECT_ROOT / "secrets" / "cookies.txt"


def test_cookie_file_ignored_for_platforms_that_did_not_opt_in(
    settings: Settings, tmp_path
) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings.ytdlp_cookiefile = str(cookie_file)

    engine = YtDlpEngine(settings)  # YouTube-style engine
    assert engine.cookie_file is None
    assert "cookiefile" not in engine._ydl_options()


def test_blank_cookie_file_setting_is_ignored(settings: Settings) -> None:
    settings.ytdlp_cookiefile = "   "
    assert YtDlpEngine(settings, use_browser_cookies=True).cookie_file is None


async def test_missing_cookie_file_fails_before_any_request(settings: Settings, tmp_path) -> None:
    settings.ytdlp_cookiefile = str(tmp_path / "does-not-exist.txt")
    engine = YtDlpEngine(settings, use_browser_cookies=True)

    with pytest.raises(CookieAccessError) as excinfo:
        await engine.extract("https://www.instagram.com/reel/abc123/", platform=Platform.INSTAGRAM)

    assert "未找到 Cookie 文件" in str(excinfo.value)
    assert "YTDLP_COOKIEFILE" in (excinfo.value.detail or "")


def test_unparsable_cookie_file_is_explained(settings: Settings) -> None:
    engine = YtDlpEngine(settings, use_browser_cookies=True)
    error = engine._translate(Exception("does not look like a Netscape format cookies file"))
    assert isinstance(error, CookieAccessError)
    assert "Netscape" in (error.detail or "")
