"""yt-dlp extraction engine.

Used as the stream resolver for platforms whose official APIs expose metadata
only (YouTube) or which have no official download API for public posts
(Instagram). It never downloads: we only borrow its URL extraction so that our
own downloader keeps control of resume, retries, integrity and layout.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import Settings
from core.exceptions import (
    AuthRequiredError,
    CookieAccessError,
    MetadataError,
    VideoDownloaderError,
)
from core.login import LoginSource, SessionOrigin
from core.models import MediaStream, Platform, VideoInfo

logger = logging.getLogger(__name__)

#: yt-dlp messages that mean "the browser cookie database could not be read".
_COOKIE_READ_HINTS = (
    "could not copy chrome cookie database",
    "could not find chrome cookies database",
    "could not find firefox cookies database",
    "could not find edge cookies database",
    "cookies database",
    "cookie database",
    "failed to decrypt with dpapi",
    "unable to decrypt",
    "failed to decrypt",
)

#: yt-dlp messages that mean "the platform wants a logged-in session".
_LOGIN_HINTS = (
    "fresh cookies",
    "sign in to confirm",
    "log in to",
    "login required",
    "empty media response",
    "use --cookies-from-browser",
    "use --cookies",
    "account is private",
)


def _first_hint(messages: tuple[str, ...], text: str) -> str | None:
    for hint in messages:
        if hint in text:
            return hint
    return None


def _filesize(fmt: dict[str, Any]) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _quality_label(fmt: dict[str, Any], *, audio: bool) -> str:
    if audio:
        abr = fmt.get("abr") or fmt.get("tbr")
        return f"{int(abr)}kbps" if abr else "audio"
    height = fmt.get("height")
    if not height:
        return fmt.get("format_note") or "unknown"
    width = fmt.get("width")
    # Name a portrait clip by its short side, the way every video site does
    # (1080x1920 is "1080p", not "1920p").
    short_side = min(width, height) if isinstance(width, int) and width else height
    return f"{short_side}p"


def _to_stream(fmt: dict[str, Any], *, audio: bool) -> MediaStream | None:
    url = fmt.get("url")
    if not url:
        return None
    headers = fmt.get("http_headers") or {}
    return MediaStream(
        url=url,
        is_audio=audio,
        quality_label=_quality_label(fmt, audio=audio),
        height=fmt.get("height"),
        width=fmt.get("width"),
        codec=fmt.get("acodec") if audio else fmt.get("vcodec"),
        bandwidth=int(fmt["tbr"] * 1000) if isinstance(fmt.get("tbr"), (int, float)) else None,
        size=_filesize(fmt),
        format_id=str(fmt.get("format_id")) if fmt.get("format_id") else None,
        ext=fmt.get("ext") or "mp4",
        headers={k: str(v) for k, v in headers.items()},
    )


#: Only these protocols hand back a real media file. Everything else
#: (``m3u8_native``, ``http_dash_segments``, ...) is a manifest that has to be
#: resolved segment by segment, so it must not be downloaded as a single file.
_DIRECT_PROTOCOLS = {"http", "https"}
_MANIFEST_HINTS = (".m3u8", ".mpd", "/manifest/")
_ALLOWED_EXTS = {"mp4", "m4a", "webm", "flv", "mov", "mkv", "mp3", "aac", "opus"}


def is_direct_download(fmt: dict[str, Any]) -> bool:
    """True when ``fmt`` points at a file rather than a playlist."""

    protocol = str(fmt.get("protocol") or "").lower()
    if protocol:
        return protocol in _DIRECT_PROTOCOLS
    url = str(fmt.get("url") or "").lower()
    return bool(url) and not any(hint in url for hint in _MANIFEST_HINTS)


def build_media_streams(formats: list[dict[str, Any]]) -> list[MediaStream]:
    """Convert yt-dlp formats into directly downloadable streams.

    Video-only and audio-only formats are kept apart so they can be merged.
    Manifest formats and storyboards are dropped. When a site only exposes
    muxed (video+audio) files, those are used as the video stream and no
    separate audio is requested.
    """

    video_only: list[MediaStream] = []
    audio_only: list[MediaStream] = []
    muxed: list[MediaStream] = []

    for fmt in formats:
        if not is_direct_download(fmt):
            continue
        ext = str(fmt.get("ext") or "").lower()
        if ext not in _ALLOWED_EXTS:
            continue
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        if vcodec == "none" and acodec == "none":
            continue  # storyboards and other non-media entries

        if vcodec != "none" and acodec == "none":
            stream = _to_stream(fmt, audio=False)
            if stream:
                video_only.append(stream)
        elif vcodec == "none" and acodec != "none":
            stream = _to_stream(fmt, audio=True)
            if stream:
                audio_only.append(stream)
        elif vcodec != "none":
            stream = _to_stream(fmt, audio=False)
            if stream:
                muxed.append(stream)

    if video_only:
        return [*video_only, *audio_only]
    # Muxed fallback: the file already carries audio, so no separate track.
    return muxed


class YtDlpEngine:
    """Async wrapper around :class:`yt_dlp.YoutubeDL` metadata extraction."""

    def __init__(
        self,
        settings: Settings,
        *,
        use_browser_cookies: bool = False,
        managed_session_file: Path | None = None,
    ) -> None:
        self.settings = settings
        #: Only platforms that genuinely need a logged-in session opt in, so
        #: YouTube and Bilibili keep working exactly as before.
        self.use_browser_cookies = use_browser_cookies
        #: Session the GUI's sign-in flow stored under ``secrets/``. When it
        #: exists it is the most specific source and wins over ``.env``.
        self.managed_session_file = managed_session_file

    @property
    def browser_cookie_source(self) -> str | None:
        """Configured browser name, or None when cookies are not in play."""

        if not self.use_browser_cookies:
            return None
        source = (self.settings.ytdlp_cookies_from_browser or "").strip().lower()
        return source or None

    @property
    def cookie_file(self) -> Path | None:
        """Resolved path of a Netscape cookies.txt, if one is configured.

        Precedence: the GUI-managed session under ``secrets/`` first (it is what
        the sign-in flow just wrote), then ``YTDLP_COOKIEFILE`` from ``.env``.
        """

        return self.cookie_file_source()[0]

    def cookie_file_source(self) -> tuple[Path | None, SessionOrigin]:
        """The cookies file in use *and* which configuration supplied it.

        One resolution, two answers: the download path only needs the path,
        while the sign-in status row has to know whether the file is *ours*
        (removable by 「退出登录」) or one named by ``.env`` (which the button
        must not pretend to delete).
        """

        if not self.use_browser_cookies:
            return None, SessionOrigin()
        if self.managed_session_file is not None and self.managed_session_file.is_file():
            return self.managed_session_file, SessionOrigin(LoginSource.MANAGED)
        raw = (self.settings.ytdlp_cookiefile or "").strip()
        if not raw:
            return None, SessionOrigin()
        return (
            self.settings.resolve_path(Path(raw)),
            SessionOrigin(LoginSource.ENV_COOKIEFILE, "YTDLP_COOKIEFILE"),
        )

    def _ydl_options(self, referer: str | None = None) -> dict[str, Any]:
        headers = {"User-Agent": self.settings.user_agent}
        if referer:
            headers["Referer"] = referer
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "nocheckcertificate": False,
            "http_headers": headers,
            "socket_timeout": self.settings.request_timeout,
        }
        if self.settings.proxy:
            options["proxy"] = self.settings.proxy
        cookie_file = self.cookie_file
        browser = self.browser_cookie_source
        if cookie_file is not None:
            # An explicit exported file wins over reading a live browser.
            options["cookiefile"] = str(cookie_file)
        elif browser:
            # yt-dlp accepts a 1-tuple: (browser, profile, keyring, container).
            options["cookiesfrombrowser"] = (browser,)
        cookie = self.settings.bilibili_cookie_header()
        if referer and "bilibili" in referer and cookie:
            options["http_headers"]["Cookie"] = cookie
        return options

    def _extract_sync(self, url: str, referer: str | None) -> dict[str, Any]:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise MetadataError("未安装 yt-dlp", detail=str(exc)) from exc

        with YoutubeDL(self._ydl_options(referer)) as ydl:
            data = ydl.extract_info(url, download=False)
        if data is None:
            raise MetadataError("yt-dlp 未返回任何数据", detail=url)
        if "entries" in data:
            entries = [entry for entry in (data.get("entries") or []) if entry]
            if not entries:
                raise MetadataError("播放列表为空", detail=url)
            data = entries[0]
        return data

    def _ensure_cookie_file(self) -> None:
        """Fail early and clearly when the configured cookies file is missing."""

        path = self.cookie_file
        if path is None or path.is_file():
            return
        raise CookieAccessError(
            "未找到 Cookie 文件",
            detail=(
                f"YTDLP_COOKIEFILE 指向的 {path} 不存在。"
                "请把浏览器导出的 Netscape 格式 cookies.txt 放到该路径，"
                "或在 .env 中修正 YTDLP_COOKIEFILE。"
            ),
        )

    async def extract(
        self,
        url: str,
        *,
        platform: Platform,
        referer: str | None = None,
    ) -> VideoInfo:
        """Return a :class:`VideoInfo` populated from yt-dlp's extractor."""

        logger.debug("yt-dlp 解析 %s", url)
        self._ensure_cookie_file()
        try:
            data = await asyncio.to_thread(self._extract_sync, url, referer)
        except MetadataError:
            raise
        except Exception as exc:  # yt-dlp raises a wide variety of errors
            raise self._translate(exc) from exc

        streams = build_media_streams(data.get("formats") or [])

        return VideoInfo(
            platform=platform,
            video_id=str(data.get("id") or ""),
            url=url,
            title=data.get("title") or "untitled",
            author=data.get("uploader") or data.get("channel") or data.get("creator"),
            author_id=data.get("uploader_id") or data.get("channel_id"),
            description=data.get("description"),
            duration=float(data["duration"]) if data.get("duration") else None,
            publish_time=_parse_timestamp(data),
            thumbnail_url=data.get("thumbnail"),
            streams=streams,
            webpage_url=data.get("webpage_url") or url,
            extra={
                "extractor": data.get("extractor_key") or data.get("extractor"),
                "view_count": data.get("view_count"),
                "like_count": data.get("like_count"),
                "is_live": data.get("is_live"),
                "availability": data.get("availability"),
                "engine": "yt-dlp",
            },
        )

    def _translate(self, exc: Exception) -> VideoDownloaderError:
        """Turn a yt-dlp failure into an actionable, credential-free error.

        Only the classified reason is exposed; the raw message is kept short
        and never contains cookie values.
        """

        text = str(exc).lower()
        browser = self.browser_cookie_source

        if "netscape" in text or "cookies file" in text or "cookie file" in text:
            return CookieAccessError(
                "Cookie 文件无法解析",
                detail=(
                    "yt-dlp 只接受 Netscape 格式的 cookies.txt。"
                    "请确认导出的是该格式（首行为 # Netscape HTTP Cookie File），"
                    "而不是 JSON 或 HTML。"
                ),
            )
        if _first_hint(_COOKIE_READ_HINTS, text):
            source = browser or "浏览器"
            if "decrypt" in text:
                # Chromium 127+ writes cookies with App-Bound Encryption (v20).
                # yt-dlp only understands v10 and plain DPAPI, so no amount of
                # retrying or re-login will help.
                return CookieAccessError(
                    f"无法解密 {source} 的 Cookie（App-Bound Encryption）",
                    detail=(
                        f"{source} 用 v20 加密了 Cookie，当前 yt-dlp 版本不支持解密，"
                        "与是否登录无关。可改用 Firefox 的登录态，或导出 cookies.txt 文件。"
                    ),
                )
            return CookieAccessError(
                f"无法读取 {source} 的 Cookie 数据库",
                detail=(
                    f"常见原因：{source} 正在运行并锁定了 Cookie 文件，或 profile 路径不正确。"
                    "请完全退出浏览器后重试；本程序不会修改或删除浏览器数据。"
                ),
            )
        if _first_hint(_LOGIN_HINTS, text):
            if browser:
                return AuthRequiredError(
                    f"平台要求登录，但读取到的 {browser} 登录态不可用",
                    detail="可能是浏览器中未登录该平台、Cookie 已过期，或登录的不是同一个 profile",
                )
            return AuthRequiredError(
                "该平台要求登录后才能获取内容",
                detail="可在 .env 中设置 YTDLP_COOKIES_FROM_BROWSER=chrome 使用浏览器登录态",
            )
        return MetadataError("yt-dlp 解析失败", detail=str(exc)[:300])


def _parse_timestamp(data: dict[str, Any]) -> datetime | None:
    timestamp = data.get("timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    upload_date = data.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        return datetime(
            int(upload_date[:4]), int(upload_date[4:6]), int(upload_date[6:8]), tzinfo=UTC
        )
    return None
