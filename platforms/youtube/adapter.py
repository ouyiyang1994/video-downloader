"""YouTube adapter.

The official Data API v3 exposes metadata only, so metadata comes from Google
and the actual media URLs are resolved through the shared yt-dlp engine. This
adapter therefore requires ``--confirm-rights``.
"""

from __future__ import annotations

import logging

import httpx

from config.settings import Settings
from core import selection
from core.engine_ytdlp import YtDlpEngine
from core.exceptions import NotDownloadableError, VideoDownloaderError
from core.interfaces import PlatformAdapter
from core.models import DownloadPlan, Platform, VideoInfo
from platforms.youtube import api, urls

logger = logging.getLogger(__name__)


class YouTubeAdapter(PlatformAdapter):
    platform = Platform.YOUTUBE
    display_name = "YouTube"
    requires_rights_confirmation = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        self.engine = YtDlpEngine(settings)

    def matches(self, url: str) -> bool:
        return urls.matches(url)

    def normalize_url(self, url: str) -> str:
        cleaned = url.strip()
        if cleaned.startswith("//"):
            cleaned = f"https:{cleaned}"
        elif "://" not in cleaned:
            cleaned = f"https://{cleaned}"
        video_id = urls.extract_video_id(cleaned)
        return urls.canonical_watch_url(video_id) if video_id else cleaned

    async def fetch_info(self, url: str) -> VideoInfo:
        video_id = urls.extract_video_id(url)
        canonical = urls.canonical_watch_url(video_id) if video_id else url

        official: dict | None = None
        api_key = self.settings.youtube_api_key
        if api_key and video_id:
            try:
                official = await api.fetch_video(self.client, video_id=video_id, api_key=api_key)
            except VideoDownloaderError as exc:
                logger.warning("YouTube 官方 API 获取失败，回退到 yt-dlp 元数据：%s", exc)
        elif not api_key:
            logger.debug("未配置 YOUTUBE_API_KEY，元数据由 yt-dlp 提供")

        info = await self.engine.extract(canonical, platform=self.platform)
        info.url = url
        info.webpage_url = canonical

        if official:
            snippet = official.get("snippet") or {}
            details = official.get("contentDetails") or {}
            info.title = snippet.get("title") or info.title
            info.author = snippet.get("channelTitle") or info.author
            info.author_id = snippet.get("channelId") or info.author_id
            info.description = snippet.get("description") or info.description
            info.publish_time = api.parse_timestamp(snippet.get("publishedAt")) or info.publish_time
            info.duration = api.parse_duration(details.get("duration")) or info.duration
            info.thumbnail_url = api.thumbnail_url(official) or info.thumbnail_url
            info.extra.update(
                {
                    "metadata_source": "youtube-data-api-v3",
                    "statistics": official.get("statistics"),
                    "definition": details.get("definition"),
                    "licensed_content": details.get("licensedContent"),
                }
            )

        if info.extra.get("is_live"):
            raise NotDownloadableError("直播流不在本工具支持范围内", detail=video_id or url)
        if info.extra.get("availability") in {"private", "subscriber_only", "needs_auth"}:
            raise NotDownloadableError(
                "该视频不可公开访问",
                detail=str(info.extra.get("availability")),
            )

        return info

    def select_streams(self, info: VideoInfo, quality: str) -> DownloadPlan:
        return selection.select_streams(info, quality)
