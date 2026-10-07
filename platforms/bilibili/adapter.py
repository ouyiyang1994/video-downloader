"""Bilibili adapter.

Metadata comes from ``x/web-interface/view`` and streams from
``x/player/wbi/playurl``: the same public endpoints the web player calls. No
CAPTCHA, DRM, access control or paywall is bypassed. Anonymous sessions are
limited to 480p; supplying ``BILIBILI_SESSDATA`` raises the ceiling to whatever
that account is entitled to (1080p for a free account, higher for VIP).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from core import selection
from core.exceptions import MetadataError, NotDownloadableError
from core.http import load_cookie_header
from core.interfaces import PlatformAdapter
from core.models import DownloadPlan, MediaStream, Platform, VideoInfo
from platforms.bilibili import api, urls

logger = logging.getLogger(__name__)

WEB_ROOT = "https://www.bilibili.com"


class BilibiliAdapter(PlatformAdapter):
    platform = Platform.BILIBILI
    display_name = "哔哩哔哩"
    requires_rights_confirmation = False

    def __init__(self, settings: Any, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        self._wbi = api.WbiKeyCache()
        self._cookie_header: str | None = None
        self._cookie_loaded = False

    # -- URL -----------------------------------------------------------------
    def matches(self, url: str) -> bool:
        return urls.matches(url)

    def normalize_url(self, url: str) -> str:
        cleaned = url.strip()
        if cleaned.startswith("//"):
            cleaned = f"https:{cleaned}"
        elif "://" not in cleaned:
            cleaned = f"https://{cleaned}"
        return cleaned

    # -- helpers -------------------------------------------------------------
    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "Referer": referer or f"{WEB_ROOT}/",
            "Origin": WEB_ROOT,
            "User-Agent": self.settings.user_agent,
        }
        cookie = self._request_cookie()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _request_cookie(self) -> str | None:
        """The Bilibili cookie to send, or ``None`` for an anonymous session.

        An explicit ``BILIBILI_COOKIE`` / ``BILIBILI_SESSDATA`` wins; otherwise
        the exported ``www.bilibili.com_cookies.txt`` is used. A missing or
        unreadable file simply means "stay anonymous" - never an error. The
        value is cached per adapter (one adapter is built per download) and is
        never logged.
        """

        if not self._cookie_loaded:
            self._cookie_loaded = True
            explicit = self.settings.bilibili_cookie_header()
            self._cookie_header = explicit or load_cookie_header(
                self.settings.bilibili_cookie_file,
                domain_suffix="bilibili.com",
            )
            if self._cookie_header:
                logger.debug(
                    "Bilibili 请求将携带登录 Cookie（来源：%s）",
                    "配置项" if explicit else "cookies.txt",
                )
        return self._cookie_header

    async def _resolve_identity(self, url: str) -> tuple[str, str, str]:
        """Return ``(kind, value, resolved_url)`` handling b23.tv short links."""

        normalized = self.normalize_url(url)
        identity = urls.extract_video_id(normalized)
        if identity:
            return identity[0], identity[1], normalized

        if not urls.is_short_link(normalized):
            raise MetadataError("无法从链接中解析出 BV 号", detail=normalized)

        logger.debug("解析 b23.tv 短链 %s", normalized)
        response = await self.client.get(normalized, headers=self._headers())
        resolved = str(response.url)
        identity = urls.extract_video_id(resolved)
        if not identity:
            raise MetadataError("短链跳转后仍无法解析 BV 号", detail=resolved)
        return identity[0], identity[1], resolved

    @staticmethod
    def _stream_url(node: dict[str, Any]) -> str | None:
        return node.get("baseUrl") or node.get("base_url") or None

    @staticmethod
    def _quality_descriptions(data: dict[str, Any]) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for entry in data.get("support_formats") or []:
            quality = entry.get("quality")
            if isinstance(quality, int):
                mapping[quality] = entry.get("new_description") or entry.get("display_desc") or ""
        return mapping

    # -- metadata ------------------------------------------------------------
    async def fetch_info(self, url: str) -> VideoInfo:
        kind, value, resolved = await self._resolve_identity(url)
        headers = self._headers(resolved)

        view_kwargs = {"bvid": value} if kind == "bvid" else {"aid": value}
        view = await api.fetch_view(self.client, headers=headers, **view_kwargs)
        bvid = view.get("bvid")
        aid = view.get("aid")
        if not bvid:
            raise MetadataError("Bilibili 未返回 BV 号", detail=resolved)

        pages = view.get("pages") or []
        page_index = min(urls.extract_page(resolved), max(len(pages), 1))
        page = pages[page_index - 1] if pages else {}
        cid = page.get("cid") or view.get("cid")
        if not cid:
            raise MetadataError("Bilibili 未返回 cid", detail=resolved)

        # page_list marks content that cannot be played as-is.
        page_list = view.get("is_season") or {}
        if isinstance(page_list, dict):
            pass

        keys: tuple[str, str] | None
        try:
            keys = await self._wbi.get(self.client, headers)
        except Exception as exc:  # signature is optional; fall back to unsigned
            logger.debug("获取 WBI 密钥失败，改用未签名接口：%s", exc)
            keys = None

        play = await api.fetch_playurl(
            self.client,
            headers=headers,
            bvid=bvid,
            cid=int(cid),
            keys=keys,
        )

        quality_desc = self._quality_descriptions(play)
        streams = self._build_streams(play, headers, quality_desc)
        if not streams:
            raise NotDownloadableError(
                "该投稿没有可用的播放流（可能是付费、番剧受限或地区限制内容）",
                detail=f"bvid={bvid} cid={cid}",
            )

        title = view.get("title") or page.get("part") or "untitled"
        if len(pages) > 1 and page.get("part"):
            title = f"{title} - P{page_index} {page['part']}"

        owner = view.get("owner") or {}
        return VideoInfo(
            platform=self.platform,
            video_id=bvid,
            url=resolved,
            title=title,
            author=owner.get("name"),
            author_id=str(owner.get("mid")) if owner.get("mid") else None,
            description=_clean_text(view.get("desc")),
            duration=float(page.get("duration") or view.get("duration") or 0) or None,
            publish_time=_to_datetime(view.get("pubdate")),
            thumbnail_url=_https(view.get("pic")),
            streams=streams,
            webpage_url=urls.canonical_watch_url("bvid", bvid),
            extra={
                "aid": aid,
                "cid": cid,
                "page": page_index,
                "page_count": len(pages),
                "page_title": page.get("part"),
                "quality_map": quality_desc,
                "accept_quality": play.get("accept_quality"),
                "requires_login_for_hd": self._request_cookie() is None,
                "dynamic": view.get("dynamic"),
                "stat": view.get("stat"),
                "source": "bilibili-web-api",
            },
        )

    def _build_streams(
        self,
        play: dict[str, Any],
        headers: dict[str, str],
        quality_desc: dict[int, str],
    ) -> list[MediaStream]:
        streams: list[MediaStream] = []
        dash = play.get("dash")

        if isinstance(dash, dict):
            for node in dash.get("video") or []:
                stream_url = self._stream_url(node)
                if not stream_url:
                    continue
                raw_qn = node.get("id")
                qn = raw_qn if isinstance(raw_qn, int) else -1
                streams.append(
                    MediaStream(
                        url=stream_url,
                        is_audio=False,
                        quality_label=quality_desc.get(qn, f"{node.get('height', '?')}p"),
                        height=node.get("height"),
                        width=node.get("width"),
                        codec=node.get("codecs"),
                        bandwidth=node.get("bandwidth"),
                        format_id=f"dash-video-{qn}",
                        ext="mp4",
                        headers=dict(headers),
                    )
                )
            for node in dash.get("audio") or []:
                stream_url = self._stream_url(node)
                if not stream_url:
                    continue
                streams.append(
                    MediaStream(
                        url=stream_url,
                        is_audio=True,
                        quality_label="audio",
                        codec=node.get("codecs"),
                        bandwidth=node.get("bandwidth"),
                        format_id=f"dash-audio-{node.get('id')}",
                        ext="m4a",
                        headers=dict(headers),
                    )
                )

        if not any(not s.is_audio for s in streams):
            raw_quality = play.get("quality")
            progressive_quality = raw_quality if isinstance(raw_quality, int) else -1
            for index, node in enumerate(play.get("durl") or []):
                stream_url = node.get("url")
                if not stream_url:
                    continue
                streams.append(
                    MediaStream(
                        url=stream_url,
                        is_audio=False,
                        quality_label=quality_desc.get(progressive_quality, "progressive"),
                        size=node.get("size"),
                        format_id=f"durl-{index}",
                        ext="flv" if "flv" in (node.get("url") or "") else "mp4",
                        headers=dict(headers),
                    )
                )

        return streams

    # -- download plan -------------------------------------------------------
    def select_streams(self, info: VideoInfo, quality: str) -> DownloadPlan:
        # Deliberately the shared rule: Bilibili keeps the same quality
        # semantics as every other platform, including the portrait handling.
        # The server-side cap (480P anonymous, more with BILIBILI_SESSDATA) is
        # untouched - it simply decides which streams appear in ``info``.
        return selection.select_streams(info, quality)

    def check_downloadable(self, info: VideoInfo) -> None:
        super().check_downloadable(info)
        if info.extra.get("is_upower_exclusive"):
            raise NotDownloadableError("该内容为充电专属，跳过", detail=info.video_id)


def _https(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("http://", "https://", 1) if url.startswith("http://") else url


def _clean_text(value: Any) -> str | None:
    """Bilibili uses ``"-"`` as a placeholder for an empty description."""

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return None if stripped in {"", "-"} else stripped


def _to_datetime(timestamp: Any) -> datetime | None:
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return None
