"""Static mappings and defaults shared across the project."""

from __future__ import annotations

from typing import Final

PROJECT_ROOT_MARKER: Final[str] = "pyproject.toml"

DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Output directory template. Rendered with {platform}/{author}/{date}.
DEFAULT_DIR_TEMPLATE: Final[str] = "{platform}/{author}/{date}"
DEFAULT_METADATA_SUFFIX: Final[str] = ".info.json"

# Quality presets accepted on the CLI, mapped to a target height in pixels.
# "best" resolves per platform; "audio" keeps audio only.
QUALITY_PRESETS: Final[dict[str, int | None]] = {
    "best": None,
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}
QUALITY_AUDIO_ONLY: Final[str] = "audio"

#: Platform ids the application supports. Used to turn the GUI's per-platform
#: proxy switches into ``PROXY_BYPASS_PLATFORMS``.
SUPPORTED_PLATFORM_IDS: Final[tuple[str, ...]] = ("youtube", "instagram", "bilibili")

#: Suggested proxy endpoint shown in the GUI when .env has no proxy configured.
#: It is only a starting value for the form - the real value always comes from
#: .env, and nothing in the download path hard-codes it.
DEFAULT_PROXY_HOST: Final[str] = "127.0.0.1"
DEFAULT_PROXY_PORT: Final[int] = 8090

# Download tuning
DEFAULT_CHUNK_SIZE: Final[int] = 1024 * 1024
PARTIAL_SUFFIX: Final[str] = ".part"
FILE_EXTENSIONS_VIDEO: Final[tuple[str, ...]] = (".mp4", ".mkv", ".flv", ".webm", ".mov")
