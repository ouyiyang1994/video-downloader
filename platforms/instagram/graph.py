"""Instagram Graph API client.

The Graph API is the only official path that yields a downloadable media file,
and it is scoped to the authenticated Business/Creator account: ``media_url``
of a ``VIDEO`` item is a signed CDN link to the mp4. Those links expire, so they
are never persisted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from core.exceptions import AuthRequiredError, MetadataError, RateLimitedError

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_ROOT = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

MEDIA_FIELDS = ",".join(
    (
        "id",
        "caption",
        "media_type",
        "media_url",
        "permalink",
        "thumbnail_url",
        "timestamp",
        "username",
        "children{media_url,media_type}",
    )
)


async def fetch_owned_media(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    access_token: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the authenticated account's most recent media items."""

    params: dict[str, str | int] = {
        "fields": MEDIA_FIELDS,
        "limit": limit,
        "access_token": access_token,
    }
    response = await client.get(f"{GRAPH_ROOT}/{user_id}/media", params=params)
    _raise(response, context="Instagram Graph API")
    payload = response.json()
    items = payload.get("data")
    if not isinstance(items, list):
        raise MetadataError("Instagram Graph API 返回了非预期结构")
    return items


def find_by_shortcode(items: list[dict[str, Any]], shortcode: str) -> dict[str, Any] | None:
    for item in items:
        permalink = str(item.get("permalink") or "")
        if shortcode in permalink:
            return item
    return None


def media_stream_url(item: dict[str, Any]) -> str | None:
    """Return a direct mp4 URL for video items (or the first video child)."""

    if str(item.get("media_type", "")).upper() in {"VIDEO", "REELS"}:
        url = item.get("media_url")
        if url:
            return str(url)
    children = ((item.get("children") or {}).get("data")) or []
    for child in children:
        if str(child.get("media_type", "")).upper() == "VIDEO" and child.get("media_url"):
            return str(child["media_url"])
    return None


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _raise(response: httpx.Response, *, context: str) -> None:
    if response.status_code in (401, 403):
        raise AuthRequiredError(
            "Instagram 访问令牌无效或权限不足",
            detail=f"{context}: HTTP {response.status_code}",
        )
    if response.status_code == 429:
        raise RateLimitedError("Instagram 接口限流（HTTP 429）", detail=context)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text[:200] if response.text else str(exc)
        raise MetadataError(
            f"{context} 调用失败：HTTP {response.status_code}", detail=detail
        ) from exc
