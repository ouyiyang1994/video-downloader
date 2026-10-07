"""Bilibili URL recognition and normalisation."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_BV_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_PATTERN = re.compile(r"av(\d+)", re.IGNORECASE)

_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv"}


def matches(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in _HOSTS or host.endswith(".bilibili.com"):
        return True
    return host.endswith("b23.tv")


def is_short_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("b23.tv")


def extract_video_id(url: str) -> tuple[str, str] | None:
    """Return ``("bvid", value)`` or ``("aid", value)`` when detectable."""

    match = _BV_PATTERN.search(url)
    if match:
        return "bvid", match.group(1)
    match = _AV_PATTERN.search(url)
    if match:
        return "aid", match.group(1)
    return None


def extract_page(url: str) -> int:
    """Return the 1-based page index requested in the URL."""

    query = parse_qs(urlparse(url).query)
    raw = query.get("p", ["1"])[0]
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(page, 1)


def canonical_watch_url(kind: str, value: str) -> str:
    if kind == "bvid":
        return f"https://www.bilibili.com/video/{value}"
    return f"https://www.bilibili.com/video/av{value}"
