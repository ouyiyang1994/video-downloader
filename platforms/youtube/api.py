"""YouTube Data API v3 (metadata only: the API has no download endpoint)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from core.exceptions import MetadataError, RateLimitedError

logger = logging.getLogger(__name__)

VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"
_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?T?"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
)


def parse_duration(value: str | None) -> float | None:
    """Parse an ISO-8601 duration such as ``PT1H2M10S`` into seconds."""

    if not value:
        return None
    match = _DURATION.fullmatch(value)
    if not match:
        return None
    parts = {name: int(raw) if raw else 0 for name, raw in match.groupdict().items()}
    return float(
        parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    )


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


async def fetch_video(client: httpx.AsyncClient, *, video_id: str, api_key: str) -> dict[str, Any]:
    params = {
        "part": "snippet,contentDetails,statistics",
        "id": video_id,
        "key": api_key,
    }
    response = await client.get(VIDEOS_API, params=params)
    if response.status_code == 403:
        raise RateLimitedError("YouTube API 配额用尽或被拒绝（HTTP 403）")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise MetadataError(f"YouTube API 失败：HTTP {response.status_code}") from exc

    payload = response.json()
    items = payload.get("items") or []
    if not items:
        raise MetadataError("YouTube API 未返回该视频", detail=video_id)
    return items[0]


def thumbnail_url(item: dict[str, Any]) -> str | None:
    thumbnails = (item.get("snippet") or {}).get("thumbnails") or {}
    for key in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(key)
        if entry and entry.get("url"):
            return str(entry["url"])
    return None
