"""Runtime settings loaded from the environment and ``.env``."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.constants import (
    DEFAULT_PROXY_HOST,
    DEFAULT_PROXY_PORT,
    SUPPORTED_PLATFORM_IDS,
)

logger = logging.getLogger(__name__)


def _app_root() -> Path:
    """Directory the application lives in.

    Frozen (PyInstaller): the folder holding the executable, so ``.env``,
    ``secrets/`` and ``tools/`` stay external and editable. Unfrozen: the
    project root, exactly as before.
    """

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def is_writable(directory: Path) -> bool:
    """True when we can actually create files in ``directory``."""

    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_data_root(
    app_root: Path,
    *,
    frozen: bool,
    local_app_data: Path | None = None,
) -> Path:
    """Where downloads, logs and the database live.

    Next to the executable by default; when that folder is not writable (for
    example an install under ``C:\\Program Files``) fall back to
    ``%LOCALAPPDATA%\\VideoDownloader``.
    """

    if not frozen:
        return app_root
    if is_writable(app_root):
        return app_root
    base = local_app_data or Path(os.environ.get("LOCALAPPDATA") or Path.home())
    fallback = base / "VideoDownloader"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def env_file_candidates(app_root: Path, data_root: Path) -> tuple[Path, ...]:
    """Every location an ``.env`` may live in, in increasing precedence.

    Both are always read, so the configuration is found whether the app was
    started from a portable folder or from an install under ``C:\\Program
    Files`` - and, importantly, whichever mode it happens to run in this time.
    With UAC disabled an install directory *is* writable, which used to send the
    search to the executable folder and silently ignore the user's file in
    ``%LOCALAPPDATA%``. The writable data root is listed last because it is the
    one the application creates and manages.
    """

    candidates: list[Path] = []
    for directory in (app_root, data_root):
        candidate = directory / ".env"
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def ensure_env_file(
    target_dir: Path | None = None,
    *,
    template_dir: Path | None = None,
) -> Path | None:
    """First-run convenience: create ``.env`` from ``.env.example``.

    Only copies the template, which contains empty placeholders - never a real
    credential. Writes into the writable data root and **never raises**: if the
    location cannot be written the app simply runs with defaults and the reason
    is logged, instead of failing to start.
    """

    root = target_dir or DATA_ROOT
    template_root = template_dir or target_dir or APP_ROOT
    candidates = (target_dir / ".env",) if target_dir else ENV_FILES
    if any(candidate.is_file() for candidate in candidates):
        return None
    target = root / ".env"
    template = template_root / ".env.example"
    if not template.exists():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, target)
    except OSError as exc:
        logger.warning("无法在 %s 生成 .env：%s（将以默认配置运行）", target, exc)
        return None
    return target


#: Environment variable names that carry the proxy, in the casing different
#: libraries happen to look for.
PROXY_ENV_KEYS: tuple[str, ...] = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


#: Proxy variables this process has set itself, so switching the proxy off can
#: remove exactly those and never touch a variable the user exported.
_exported_proxy_keys: set[str] = set()


def apply_proxy_environment(settings: Settings) -> list[str]:
    """Make the process environment match the configured proxy.

    httpx and yt-dlp both accept an explicit proxy argument, but parts of those
    libraries (and anything else in the process) fall back to the environment.
    Keeping the two in sync is what makes an external ``.env`` behave the same
    as a shell-exported variable.

    When the proxy is switched off, only the variables this process set are
    removed - a variable the user exported themselves is left alone. Returns the
    names that changed.
    """

    proxy = settings.proxy
    changed: list[str] = []
    for key in PROXY_ENV_KEYS:
        if proxy:
            if os.environ.get(key) != proxy:
                os.environ[key] = proxy
                changed.append(key)
            _exported_proxy_keys.add(key)
        elif key in _exported_proxy_keys:
            os.environ.pop(key, None)
            _exported_proxy_keys.discard(key)
            changed.append(key)
    return changed


#: Location of the code and read-only resources (ffmpeg, .env.example).
APP_ROOT: Path = _app_root()
#: Backwards-compatible alias for APP_ROOT.
PROJECT_ROOT: Path = APP_ROOT
#: Writable root for downloads/, logs/ and downloads.db.
DATA_ROOT: Path = resolve_data_root(APP_ROOT, frozen=bool(getattr(sys, "frozen", False)))
#: Every .env location, lowest precedence first.
ENV_FILES: tuple[Path, ...] = env_file_candidates(APP_ROOT, DATA_ROOT)
#: The location the application creates .env in.
ENV_FILE: Path = ENV_FILES[-1]


class Settings(BaseSettings):
    """Application settings.

    Field names map to upper-cased environment variables, so ``youtube_api_key``
    is populated from ``YOUTUBE_API_KEY``.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Credentials (all optional) ---------------------------------------
    youtube_api_key: str | None = None
    instagram_access_token: str | None = None
    instagram_user_id: str | None = None
    bilibili_sessdata: str | None = None
    bilibili_cookie: str | None = None
    #: Netscape cookies.txt exported from a logged-in Bilibili session. Kept
    #: separate from YTDLP_COOKIEFILE, which belongs to Instagram.
    bilibili_cookiefile: str | None = "www.bilibili.com_cookies.txt"
    #: Browser to take the logged-in session from (yt-dlp ``cookiesfrombrowser``).
    #: Only Instagram reads it, and only when no cookies file is configured.
    ytdlp_cookies_from_browser: str | None = None
    #: Netscape-format cookies.txt exported from a browser. Takes precedence
    #: over the browser source. Only Instagram reads it.
    ytdlp_cookiefile: str | None = None

    # --- Storage -----------------------------------------------------------
    output_dir: Path = Field(default=Path("downloads"))
    database_path: Path = Field(default=Path("downloads/downloads.db"))
    log_dir: Path = Field(default=Path("logs"))
    #: Where the GUI's sign-in flow stores the per-platform sessions. Relative
    #: paths resolve against the data root, and the folder is git-ignored.
    session_dir: Path = Field(default=Path("secrets"))
    #: Generated launcher + native messaging manifest for the Chrome extension.
    #: Git-ignored; regenerated by 「安装登录助手」.
    native_host_dir: Path = Field(default=Path("native_host"))
    #: Chrome profile used by the DevTools fallback. Kept apart from the user's
    #: own profile on purpose - Chrome refuses a debugging port on that one.
    chrome_profile_dir: Path = Field(default=Path("chrome-profile"))
    dir_template: str = "{platform}/{author}/{date}"

    # --- Networking --------------------------------------------------------
    ffmpeg_path: str | None = None
    http_proxy: str | None = None
    https_proxy: str | None = None
    #: Comma separated platform ids that must NOT go through the proxy.
    #: Bilibili risk-controls proxy/datacenter exit IPs with HTTP 412, so it
    #: has to stay on the direct connection.
    proxy_bypass_platforms: str = "bilibili"
    request_timeout: float = 30.0
    max_retries: int = 3
    concurrent_fragments: int = 4
    chunk_size: int = 1024 * 1024
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # --- Behaviour ---------------------------------------------------------
    log_level: str = "INFO"
    confirm_rights: bool = False
    max_filename_length: int = 120
    skip_existing: bool = True

    @property
    def proxy(self) -> str | None:
        """Single proxy URL for httpx, if one was configured."""

        return self.https_proxy or self.http_proxy or None

    @property
    def proxy_bypass_set(self) -> set[str]:
        """Platform ids whose traffic bypasses the proxy."""

        return {
            part.strip().lower()
            for part in (self.proxy_bypass_platforms or "").split(",")
            if part.strip()
        }

    def bilibili_cookie_header(self) -> str | None:
        """Return a usable Cookie header for Bilibili requests."""

        if self.bilibili_cookie:
            return self.bilibili_cookie.strip()
        if self.bilibili_sessdata:
            return f"SESSDATA={self.bilibili_sessdata.strip()}"
        return None

    @property
    def bilibili_cookie_file(self) -> Path | None:
        """Resolved path of the Bilibili cookies.txt, if one is configured."""

        raw = (self.bilibili_cookiefile or "").strip()
        if not raw:
            return None
        return self.resolve_path(Path(raw))

    def resolve_path(self, value: Path) -> Path:
        """Resolve a possibly-relative path against the project root."""

        return value if value.is_absolute() else (DATA_ROOT / value)


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """The proxy settings the GUI edits and ``.env`` stores."""

    enabled: bool
    host: str = DEFAULT_PROXY_HOST
    port: int = DEFAULT_PROXY_PORT
    proxied_platforms: tuple[str, ...] = ()

    @property
    def url(self) -> str | None:
        """The proxy URL, or ``None`` when the proxy is switched off."""

        if not self.enabled or not self.host or not self.port:
            return None
        return f"http://{self.host}:{self.port}"

    @property
    def bypass_platforms(self) -> tuple[str, ...]:
        """Platforms that must keep a direct connection."""

        proxied = {name.strip().lower() for name in self.proxied_platforms}
        return tuple(name for name in SUPPORTED_PLATFORM_IDS if name not in proxied)

    def as_env(self) -> dict[str, str]:
        """The three ``.env`` entries this feature owns."""

        url = self.url or ""
        return {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "PROXY_BYPASS_PLATFORMS": ",".join(self.bypass_platforms),
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> ProxyConfig:
        """Read the current configuration, falling back to the suggested values."""

        host, port = DEFAULT_PROXY_HOST, DEFAULT_PROXY_PORT
        url = settings.proxy
        if url:
            parsed = urlsplit(url if "://" in url else f"http://{url}")
            host = parsed.hostname or host
            port = parsed.port or port
        bypass = settings.proxy_bypass_set
        proxied = tuple(name for name in SUPPORTED_PLATFORM_IDS if name not in bypass)
        return cls(enabled=bool(url), host=host, port=port, proxied_platforms=proxied)


def _env_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def update_env_values(path: Path, values: Mapping[str, str]) -> None:
    """Rewrite the given keys in ``.env``, leaving everything else untouched."""

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        key = _env_key(line)
        if key is not None and key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())

    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_proxy_configuration(config: ProxyConfig, env_file: Path | None = None) -> Path:
    """Persist ``config`` into ``.env`` and return the file that was written."""

    target = env_file or ENV_FILE
    if not target.is_file():
        template = APP_ROOT / ".env.example"
        if template.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, target)
    update_env_values(target, config.as_env())
    return target


def apply_proxy_configuration(settings: Settings, config: ProxyConfig) -> list[str]:
    """Push ``config`` into the live settings object and the process environment.

    Download workers hold a reference to the same ``settings`` instance, so a
    saved change takes effect on the next download without restarting the GUI.
    """

    settings.http_proxy = config.url
    settings.https_proxy = config.url
    settings.proxy_bypass_platforms = ",".join(config.bypass_platforms)
    return apply_proxy_environment(settings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance."""

    created = ensure_env_file()
    if created is not None:
        logger.info("首次运行：已从 .env.example 生成 %s", created)
    settings = Settings()
    applied = apply_proxy_environment(settings)
    if applied:
        logger.info("代理已从 .env 读取并写入进程环境：%s", ", ".join(applied))
    return settings
