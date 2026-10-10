"""The per-platform switch that hides ``.env`` fallback credentials.

Everything here runs against temporary directories and simulated cookie files:
the developer's real ``.env``, managed sessions and browser stores are never
read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from config.settings import Settings
from core import fallback_policy, session_store
from core.engine_ytdlp import YtDlpEngine
from core.login import LoginSource
from core.models import Platform
from platforms.bilibili.adapter import BilibiliAdapter
from platforms.instagram.adapter import InstagramAdapter


def _write_cookie_file(path: Path, *, domain: str, name: str, value: str = "FROMFALLBACK") -> Path:
    """A simulated ``.env`` cookie file - temporary, with fake values only."""

    path.write_text(
        f"# Netscape HTTP Cookie File\n.{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}\n",
        encoding="utf-8",
    )
    return path


def _store_managed(settings: Settings, platform: Platform, name: str, domain: str) -> Path:
    """Write a simulated managed session and return its path."""

    path = session_store.write_session(
        settings,
        platform,
        [session_store.make_cookie(name, "MANAGED", domain=f".{domain}")],
        domain_suffix=domain,
    )
    assert path is not None
    return path


def _instagram_settings(settings: Settings, tmp_path: Path) -> Settings:
    settings.ytdlp_cookiefile = str(
        _write_cookie_file(tmp_path / "ig-cookies.txt", domain="instagram.com", name="sessionid")
    )
    return settings


def _bilibili_settings(settings: Settings, tmp_path: Path) -> Settings:
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = str(
        _write_cookie_file(tmp_path / "bili-cookies.txt", domain="bilibili.com", name="SESSDATA")
    )
    return settings


# --- the flag itself ---------------------------------------------------------


def test_nothing_is_disabled_on_a_fresh_installation(settings: Settings) -> None:
    assert fallback_policy.disabled_platforms(settings) == frozenset()
    assert fallback_policy.is_disabled(settings, Platform.INSTAGRAM) is False
    assert fallback_policy.is_disabled(settings, Platform.BILIBILI) is False


def test_the_two_platforms_are_switched_independently(settings: Settings) -> None:
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    assert fallback_policy.is_disabled(settings, Platform.INSTAGRAM) is True
    assert fallback_policy.is_disabled(settings, Platform.BILIBILI) is False

    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=True)
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=False)

    assert fallback_policy.is_disabled(settings, Platform.INSTAGRAM) is False
    assert fallback_policy.is_disabled(settings, Platform.BILIBILI) is True


def test_the_flag_survives_a_restart(settings: Settings, tmp_path: Path) -> None:
    """A new ``Settings`` object - a restart - must see the same state."""

    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    restarted = Settings(_env_file=None, session_dir=settings.resolve_path(settings.session_dir))

    assert fallback_policy.is_disabled(restarted, Platform.INSTAGRAM) is True
    assert fallback_policy.is_disabled(restarted, Platform.BILIBILI) is False
    assert fallback_policy.state_file(restarted).is_file()


def test_the_state_file_records_platform_names_only(settings: Settings) -> None:
    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=True)

    raw = fallback_policy.state_file(settings).read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert payload["disabled"] == ["bilibili"]
    assert "cookie" not in raw.lower()
    assert "SESSDATA" not in raw


def test_a_corrupt_state_file_means_nothing_is_disabled(settings: Settings) -> None:
    """Failing closed would silently ignore the user's configured session."""

    path = fallback_policy.state_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert fallback_policy.disabled_platforms(settings) == frozenset()


def test_writing_the_flag_leaves_the_session_files_alone(
    settings: Settings, tmp_path: Path
) -> None:
    instagram = _store_managed(settings, Platform.INSTAGRAM, "sessionid", "instagram.com")
    bilibili = _store_managed(settings, Platform.BILIBILI, "SESSDATA", "bilibili.com")
    before = (instagram.read_bytes(), bilibili.read_bytes())

    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    assert (instagram.read_bytes(), bilibili.read_bytes()) == before


# --- enforcement: Instagram --------------------------------------------------


async def test_instagram_ignores_the_env_cookie_file_while_disabled(
    settings: Settings, tmp_path: Path
) -> None:
    _instagram_settings(settings, tmp_path)
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    async with httpx.AsyncClient() as client:
        adapter = InstagramAdapter(settings, client)

        assert adapter.session_cookie_header() is None
        assert adapter.session_origin().source is LoginSource.NONE
        assert adapter.engine.cookie_file is None
        assert adapter.engine.browser_cookie_source is None


async def test_instagram_uses_the_env_cookie_file_again_after_reenabling(
    settings: Settings, tmp_path: Path
) -> None:
    _instagram_settings(settings, tmp_path)
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    async with httpx.AsyncClient() as client:
        before = InstagramAdapter(settings, client).session_cookie_header()

    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=False)

    async with httpx.AsyncClient() as client:
        adapter = InstagramAdapter(settings, client)
        after = adapter.session_cookie_header()
        origin = adapter.session_origin()

    assert before is None
    assert after == "sessionid=FROMFALLBACK"
    assert origin.key == "YTDLP_COOKIEFILE"


async def test_instagram_honours_a_new_managed_session_while_disabled(
    settings: Settings, tmp_path: Path
) -> None:
    """Signing in again must keep working with the fallback switched off."""

    _instagram_settings(settings, tmp_path)
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)
    _store_managed(settings, Platform.INSTAGRAM, "sessionid", "instagram.com")

    async with httpx.AsyncClient() as client:
        adapter = InstagramAdapter(settings, client)
        header = adapter.session_cookie_header()
        origin = adapter.session_origin()

    assert header == "sessionid=MANAGED"
    assert origin.managed is True
    assert adapter.engine.cookie_file == session_store.session_file(settings, Platform.INSTAGRAM)


# --- enforcement: Bilibili ---------------------------------------------------


async def test_bilibili_ignores_both_env_sources_while_disabled(
    settings: Settings, tmp_path: Path
) -> None:
    _bilibili_settings(settings, tmp_path)
    settings.bilibili_cookie = "SESSDATA=FROMCONFIG"
    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=True)

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)

        assert adapter.session_cookie_header() is None
        assert adapter.session_origin().source is LoginSource.NONE


async def test_bilibili_honours_a_new_managed_session_while_disabled(
    settings: Settings, tmp_path: Path
) -> None:
    _bilibili_settings(settings, tmp_path)
    settings.bilibili_cookie = "SESSDATA=FROMCONFIG"
    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=True)
    _store_managed(settings, Platform.BILIBILI, "SESSDATA", "bilibili.com")

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)

        assert adapter.session_cookie_header() == "SESSDATA=MANAGED"
        assert adapter.session_origin().managed is True


async def test_bilibili_uses_the_env_sources_again_after_reenabling(
    settings: Settings, tmp_path: Path
) -> None:
    _bilibili_settings(settings, tmp_path)
    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=True)
    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=False)

    async with httpx.AsyncClient() as client:
        adapter = BilibiliAdapter(settings, client)
        header = adapter.session_cookie_header()
        origin = adapter.session_origin()

    assert header == "SESSDATA=FROMFALLBACK"
    assert origin.key == "BILIBILI_COOKIEFILE"


# --- the platforms stay independent ------------------------------------------


async def test_disabling_instagram_leaves_bilibili_untouched(
    settings: Settings, tmp_path: Path
) -> None:
    _instagram_settings(settings, tmp_path)
    _bilibili_settings(settings, tmp_path)
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    async with httpx.AsyncClient() as client:
        bilibili = BilibiliAdapter(settings, client).session_cookie_header()

    assert bilibili == "SESSDATA=FROMFALLBACK"
    assert fallback_policy.is_disabled(settings, Platform.BILIBILI) is False


# --- the download path (yt-dlp options) --------------------------------------


def _download_options(settings: Settings, *, referer: str | None = None) -> dict[str, object]:
    """The options a download task would hand to yt-dlp."""

    adapter_engine = YtDlpEngine(
        settings,
        use_browser_cookies=True,
        managed_session_file=session_store.session_file(settings, Platform.INSTAGRAM),
        platform=Platform.INSTAGRAM,
    )
    return adapter_engine._ydl_options(referer)  # noqa: SLF001 - the options are the contract


def test_instagram_download_options_drop_the_env_cookie_file_while_disabled(
    settings: Settings, tmp_path: Path
) -> None:
    """The path that actually feeds yt-dlp must obey the switch too."""

    _instagram_settings(settings, tmp_path)

    enabled = _download_options(settings)
    assert enabled["cookiefile"] == settings.ytdlp_cookiefile

    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)
    disabled = _download_options(settings)

    assert "cookiefile" not in disabled
    assert "cookiesfrombrowser" not in disabled

    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=False)
    assert _download_options(settings)["cookiefile"] == settings.ytdlp_cookiefile


def test_instagram_download_options_drop_the_browser_source_while_disabled(
    settings: Settings,
) -> None:
    """``YTDLP_COOKIES_FROM_BROWSER`` is an ``.env`` fallback as well."""

    settings.ytdlp_cookiefile = None
    settings.ytdlp_cookies_from_browser = "chrome"
    assert _download_options(settings)["cookiesfrombrowser"] == ("chrome",)

    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)
    assert "cookiesfrombrowser" not in _download_options(settings)

    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=False)
    assert _download_options(settings)["cookiesfrombrowser"] == ("chrome",)


def test_a_download_task_reads_the_switch_from_disk(settings: Settings, tmp_path: Path) -> None:
    """A task started later - a restart, or another worker - sees the flag."""

    _instagram_settings(settings, tmp_path)
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    # A separate Settings object, exactly like a worker that starts later.
    later = Settings(
        _env_file=None,
        session_dir=settings.resolve_path(settings.session_dir),
        ytdlp_cookiefile=settings.ytdlp_cookiefile,
    )

    assert "cookiefile" not in _download_options(later)


def test_bilibili_credentials_stay_out_of_ytdlp_options_while_disabled(
    settings: Settings,
) -> None:
    """Bilibili's ``.env`` value must not be injected either."""

    settings.bilibili_cookie = None
    settings.bilibili_sessdata = "FAKE-SESSDATA"

    engine = YtDlpEngine(settings, use_browser_cookies=True)
    referer = "https://www.bilibili.com/video/BV1"
    assert engine._ydl_options(referer)["http_headers"]["Cookie"] == "SESSDATA=FAKE-SESSDATA"

    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=True)
    assert "Cookie" not in engine._ydl_options(referer)["http_headers"]

    fallback_policy.set_disabled(settings, Platform.BILIBILI, disabled=False)
    assert engine._ydl_options(referer)["http_headers"]["Cookie"] == "SESSDATA=FAKE-SESSDATA"


def test_a_missing_fallback_file_does_not_fail_a_disabled_download(
    settings: Settings, tmp_path: Path
) -> None:
    """Disabling means "anonymous", not "error out about a file we ignore"."""

    settings.ytdlp_cookiefile = str(tmp_path / "gone.txt")
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    engine = YtDlpEngine(
        settings,
        use_browser_cookies=True,
        managed_session_file=session_store.session_file(settings, Platform.INSTAGRAM),
        platform=Platform.INSTAGRAM,
    )

    engine._ensure_cookie_file()  # noqa: SLF001 - must not raise while disabled
    assert engine.cookie_file is None
