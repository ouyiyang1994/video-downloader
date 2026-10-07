"""YouTube URL recognition."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_ID = r"[A-Za-z0-9_-]{11}"
_PATH_PATTERNS = (re.compile(rf"^/(?:shorts|live|embed|v)/(?P<id>{_ID})"),)
_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


def matches(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _HOSTS or host.endswith(".youtube.com")


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if re.fullmatch(_ID, candidate) else None
    query = parse_qs(parsed.query)
    if "v" in query and re.fullmatch(_ID, query["v"][0]):
        return query["v"][0]
    for pattern in _PATH_PATTERNS:
        match = pattern.match(parsed.path)
        if match:
            return match.group("id")
    return None


def canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
