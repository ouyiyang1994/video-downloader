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

import httpx

from config.settings import Settings
from core import selection
from core.engine_ytdlp import YtDlpEngine
from core.exceptions import NotDownloadableError, UnsupportedUrlError, VideoDownloaderError
from core.interfaces import PlatformAdapter
from core.models import DownloadPlan, MediaStream, Platform, VideoInfo
from platforms.instagram import graph, urls

logger = logging.getLogger(__name__)

PUBLIC_REFERER = "https://www.instagram.com/"


class InstagramAdapter(PlatformAdapter):
    platform = Platform.INSTAGRAM
    display_name = "Instagram"
    requires_rights_confirmation = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        # Public Instagram posts also answer only to a logged-in browser
        # session; the Graph API path above stays the preferred route.
        self.engine = YtDlpEngine(settings, use_browser_cookies=True)
        if settings.instagram_access_token and settings.instagram_user_id:
            # Graph API results are, by construction, the authorised account's
            # own media, so no additional rights confirmation is required.
            self.requires_rights_confirmation = False

    @property
    def graph_enabled(self) -> bool:
        return bool(self.settings.instagram_access_token and self.settings.instagram_user_id)

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
