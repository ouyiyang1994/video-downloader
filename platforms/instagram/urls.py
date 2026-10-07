"""Instagram URL recognition."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_PATTERN = re.compile(r"/(?:p|reel|reels|tv)/(?P<code>[A-Za-z0-9_-]+)")

_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}


def matches(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return host in _HOSTS or host.endswith(".instagram.com")


def extract_shortcode(url: str) -> str | None:
    match = _PATTERN.search(urlparse(url).path)
    return match.group("code") if match else None


def is_profile_url(url: str) -> bool:
    path = urlparse(url).path.strip("/")
    return bool(path) and "/" not in path and path not in {"p", "reel", "reels", "tv", "explore"}


def canonical_watch_url(shortcode: str, *, kind: str = "p") -> str:
    return f"https://www.instagram.com/{kind}/{shortcode}/"
