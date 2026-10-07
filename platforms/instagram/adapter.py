"""Instagram adapter.

Official path (preferred): with ``INSTAGRAM_ACCESS_TOKEN`` and
``INSTAGRAM_USER_ID`` configured, the Graph API returns the authenticated
account's own media together with a signed mp4 ``media_url``. That path needs
no rights confirmation because the media belongs to the authorised account.

Fallback path: public posts of other accounts have no official download API, so
they are resolved through yt-dlp and require ``--confirm-rights``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config.settings import Settings
from core import selection
from core.engine_ytdlp import YtDlpEngine
from core.exceptions import NotDownloadableError, UnsupportedUrlError, VideoDownloaderError
from core.http import load_cookie_header
from core.interfaces import PlatformAdapter
from core.login import LoginState, SessionStatus, header_has_cookie
from core.models import DownloadPlan, MediaStream, Platform, VideoInfo
from core.session_store import session_file
from platforms.instagram import graph, urls

logger = logging.getLogger(__name__)

PUBLIC_REFERER = "https://www.instagram.com/"

#: Official sign-in page, opened in the user's own browser.
LOGIN_URL = "https://www.instagram.com/accounts/login/"
#: Registrable domain the session cookies belong to.
SESSION_DOMAIN = "instagram.com"
#: ``sessionid`` is the cookie yt-dlp's own Instagram extractor treats as the
#: authentication token (``InstagramBaseIE._AUTH_COOKIE_NAME``).
SESSION_COOKIE_NAMES = ("sessionid",)

#: Endpoint and headers taken from yt-dlp's Instagram extractor, so the status
#: probe speaks to the site exactly the way the downloader does.
PROFILE_INFO_API = "https://www.instagram.com/api/v1/users/web_profile_info/"
#: A long-lived official account, used only as a "can this session read the
#: API at all" probe.
PROBE_USERNAME = "instagram"
APP_ID = "936619743392459"
#: A status probe should answer quickly; the download timeout (30 s by default)
#: would leave the settings row on "检测中…" for far too long.
SESSION_CHECK_TIMEOUT = 10.0
API_HEADERS = {
    "X-IG-App-ID": APP_ID,
    "X-ASBD-ID": "359341",
    "X-IG-WWW-Claim": "0",
    "Origin": "https://www.instagram.com",
    "Accept": "*/*",
}


class InstagramAdapter(PlatformAdapter):
    platform = Platform.INSTAGRAM
    display_name = "Instagram"
    requires_rights_confirmation = True
    login_url = LOGIN_URL
    session_domain = SESSION_DOMAIN
    session_cookie_names = SESSION_COOKIE_NAMES

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        # Public Instagram posts also answer only to a logged-in browser
        # session; the Graph API path above stays the preferred route.
        self.engine = YtDlpEngine(
            settings,
            use_browser_cookies=True,
            managed_session_file=session_file(settings, self.platform),
        )
        if settings.instagram_access_token and settings.instagram_user_id:
            # Graph API results are, by construction, the authorised account's
            # own media, so no additional rights confirmation is required.
            self.requires_rights_confirmation = False

    @property
    def graph_enabled(self) -> bool:
        return bool(self.settings.instagram_access_token and self.settings.instagram_user_id)

    # -- sign-in ------------------------------------------------------------
    def session_cookie_header(self) -> str | None:
        """Cookie header for the Instagram session, or ``None``.

        ``YtDlpEngine.cookie_file`` already resolves the managed ``secrets/``
        session ahead of ``YTDLP_COOKIEFILE``, so both the download path and
        this probe agree on which session is in use.
        """

        return load_cookie_header(self.engine.cookie_file, domain_suffix=SESSION_DOMAIN)

    async def check_session(self, cookie_header: str | None) -> SessionStatus:
        """Probe the session with the same API call yt-dlp relies on.

        Instagram has no "who am I" endpoint that does not need a username, so
        the probe asks for one well-known public account. A login wall (redirect
        to ``/accounts/login``), a 401/403, or a JSON error body all mean the
        session is no longer valid; a network failure means we simply cannot
        tell, which is reported as ``UNKNOWN`` rather than as a verdict.

        A header without ``sessionid`` is treated as "not signed in" before any
        request is made: if the profile endpoint ever answers an anonymous
        caller, that must not be mistaken for a signed-in session.
        """

        if not header_has_cookie(cookie_header, SESSION_COOKIE_NAMES):
            return SessionStatus(platform=self.platform, state=LoginState.LOGGED_OUT)

        headers = dict(API_HEADERS)
        headers["Cookie"] = cookie_header
        headers["User-Agent"] = self.settings.user_agent
        headers["Referer"] = PUBLIC_REFERER

        try:
            response = await self.client.get(
                PROFILE_INFO_API,
                params={"username": PROBE_USERNAME},
                headers=headers,
                follow_redirects=False,
                timeout=SESSION_CHECK_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            logger.info("检查 Instagram 登录状态失败：%s", type(exc).__name__)
            return SessionStatus(
                platform=self.platform,
                state=LoginState.UNKNOWN,
                detail="网络异常，暂时无法确认登录状态",
            )

        if response.is_redirect:
            location = response.headers.get("location", "")
            if "accounts/login" in location or location.rstrip("/") in {
                "https://www.instagram.com",
                "https://www.instagram.com/",
            }:
                return _expired()
            return SessionStatus(
                platform=self.platform,
                state=LoginState.UNKNOWN,
                detail="Instagram 返回了未预期的跳转，无法确认登录状态",
            )

        if response.status_code in (401, 403):
            return _expired()
        if response.status_code >= 400:
            return SessionStatus(
                platform=self.platform,
                state=LoginState.UNKNOWN,
                detail=f"Instagram 返回 HTTP {response.status_code}，无法确认登录状态",
            )

        try:
            payload: Any = response.json()
        except ValueError:
            return SessionStatus(
                platform=self.platform,
                state=LoginState.UNKNOWN,
                detail="Instagram 返回了非 JSON 内容，无法确认登录状态",
            )

        user = (payload.get("data") or {}).get("user") if isinstance(payload, dict) else None
        if isinstance(user, dict) and user.get("username"):
            return SessionStatus(platform=self.platform, state=LoginState.LOGGED_IN)
        return _expired()

    def matches(self, url: str) -> bool:
        return urls.matches(url)

    def normalize_url(self, url: str) -> str:
        cleaned = url.strip()
        if cleaned.startswith("//"):
            cleaned = f"https:{cleaned}"
        elif "://" not in cleaned:
            cleaned = f"https://{cleaned}"
        return cleaned

    async def fetch_info(self, url: str) -> VideoInfo:
        normalized = self.normalize_url(url)
        shortcode = urls.extract_shortcode(normalized)
        if shortcode is None:
            if urls.is_profile_url(normalized):
                raise UnsupportedUrlError(
                    "暂不支持 Instagram 用户主页链接，请提供单个帖子/Reel 链接",
                    detail=normalized,
                )
            raise UnsupportedUrlError("无法识别 Instagram 短码", detail=normalized)

        if self.graph_enabled:
            info = await self._fetch_own_media(normalized, shortcode)
            if info is not None:
                return info
            logger.info(
                "该链接不在已授权账号的作品列表中，回退到 yt-dlp 公开内容路径：%s", shortcode
            )

        info = await self.engine.extract(normalized, platform=self.platform, referer=PUBLIC_REFERER)
        info.url = url
        info.extra["metadata_source"] = "yt-dlp"
        return info

    async def _fetch_own_media(self, url: str, shortcode: str) -> VideoInfo | None:
        assert self.settings.instagram_access_token and self.settings.instagram_user_id
        try:
            items = await graph.fetch_owned_media(
                self.client,
                user_id=self.settings.instagram_user_id,
                access_token=self.settings.instagram_access_token,
            )
        except VideoDownloaderError as exc:
            logger.warning("Instagram Graph API 调用失败，回退到 yt-dlp：%s", exc)
            return None

        item = graph.find_by_shortcode(items, shortcode)
        if item is None:
            return None

        media_url = graph.media_stream_url(item)
        if not media_url:
            raise NotDownloadableError(
                "该 Instagram 作品不是视频（图片或纯图集）",
                detail=f"media_type={item.get('media_type')}",
            )

        caption = (item.get("caption") or "").strip()
        title = caption.splitlines()[0][:120] if caption else f"instagram-{shortcode}"
        return VideoInfo(
            platform=self.platform,
            video_id=shortcode,
            url=url,
            title=title,
            author=item.get("username"),
            author_id=item.get("username"),
            description=caption or None,
            publish_time=graph.parse_timestamp(item.get("timestamp")),
            thumbnail_url=item.get("thumbnail_url"),
            streams=[
                MediaStream(
                    url=media_url,
                    is_audio=False,
                    quality_label="source",
                    format_id="graph-media-url",
                    ext="mp4",
                )
            ],
            webpage_url=urls.canonical_watch_url(shortcode),
            extra={
                "media_type": item.get("media_type"),
                "permalink": item.get("permalink"),
                "graph_media_id": item.get("id"),
                "metadata_source": "instagram-graph-api",
            },
        )

    def select_streams(self, info: VideoInfo, quality: str) -> DownloadPlan:
        return selection.select_streams(info, quality)


def _expired() -> SessionStatus:
    """The single "this session no longer works" verdict."""

    return SessionStatus(
        platform=Platform.INSTAGRAM,
        state=LoginState.EXPIRED,
        detail="Instagram 判定该会话未登录，请重新登录",
    )
