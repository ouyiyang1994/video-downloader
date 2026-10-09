"""The unified adapter contract every platform must implement.

The application layer only ever talks to :class:`PlatformAdapter`; it never
imports anything platform specific.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

import httpx

from config.settings import Settings
from core.exceptions import NotDownloadableError
from core.http import load_cookie_header
from core.login import LoginSource, LoginState, SessionOrigin, SessionStatus
from core.models import DownloadPlan, Platform, VideoInfo
from core.session_store import session_file


class PlatformAdapter(ABC):
    """Uniform interface for a video platform."""

    platform: ClassVar[Platform]
    #: Human readable name used in CLI output.
    display_name: ClassVar[str] = ""
    #: Set to True when the adapter fetches media through non-official
    #: endpoints and therefore requires ``--confirm-rights``.
    #: A plain attribute (not ClassVar) so an adapter can relax it per instance
    #: once official credentials are configured.
    requires_rights_confirmation: bool = True

    #: Official sign-in page, opened in the user's own browser. ``None`` means
    #: the platform has no sign-in concept in this application.
    login_url: ClassVar[str | None] = None
    #: Registrable domain the session cookies belong to (``bilibili.com``).
    session_domain: ClassVar[str] = ""
    #: Cookies that must be present for a session to be usable. The first entry
    #: is the value the user is asked for when a manual paste is needed.
    session_cookie_names: ClassVar[tuple[str, ...]] = ()

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    # -- 0. Sign-in ---------------------------------------------------------
    @property
    def login_supported(self) -> bool:
        """True when this platform can be signed in to from the GUI."""

        return self.login_url is not None and bool(self.session_domain)

    @property
    def managed_session_file(self) -> Path:
        """Where the GUI-managed session for this platform lives."""

        return session_file(self.settings, self.platform)

    def reload_session(self) -> None:  # noqa: B027 - a no-op default is correct here
        """Drop any cached session so the next lookup re-reads its sources.

        Adapters may cache the resolved cookie header because they are rebuilt
        per download; the sign-in flow, however, changes the session behind a
        long-lived adapter's back, so it has to say so explicitly. Most adapters
        cache nothing and inherit this no-op.
        """

    def session_cookie_header(self) -> str | None:
        """Cookie header taken from the managed session file, if any.

        Adapters with additional ``.env``-configured sources override this to
        put them in the right precedence order.
        """

        if not self.session_domain:
            return None
        return load_cookie_header(self.managed_session_file, domain_suffix=self.session_domain)

    def session_origin(self) -> SessionOrigin:
        """Where :meth:`session_cookie_header` takes its header from.

        The GUI needs this to tell a session *this application stored* from one
        supplied by ``.env``: only the former can be removed by 「退出登录」, and
        saying otherwise makes the row bounce back to "已登录" with no
        explanation. Adapters with ``.env`` fallbacks override this together
        with :meth:`session_cookie_header`, so both answers come from the same
        resolution.
        """

        if not self.session_domain:
            return SessionOrigin()
        if load_cookie_header(self.managed_session_file, domain_suffix=self.session_domain):
            return SessionOrigin(LoginSource.MANAGED)
        return SessionOrigin()

    def fallback_config_keys(self) -> tuple[str, ...]:
        """``.env`` keys that could still supply a session after a logout.

        Only platforms with a configured fallback answer here. The GUI uses the
        names - never the values - to warn that 「退出登录」 cannot remove them.
        """

        return ()

    async def check_session(self, cookie_header: str | None) -> SessionStatus:
        """Ask the platform whether ``cookie_header`` is a live session.

        The default answer is "this platform cannot be checked", which keeps
        YouTube - and any future adapter that has not opted in - working exactly
        as before.
        """

        return SessionStatus(
            platform=self.platform,
            state=LoginState.UNKNOWN,
            detail="该平台暂不支持登录状态检查",
        )

    # -- 1. URL recognition -------------------------------------------------
    @abstractmethod
    def matches(self, url: str) -> bool:
        """Return True when this adapter understands ``url``."""

    @abstractmethod
    def normalize_url(self, url: str) -> str:
        """Normalise a share/alias URL into a canonical form."""

    # -- 2. Metadata --------------------------------------------------------
    @abstractmethod
    async def fetch_info(self, url: str) -> VideoInfo:
        """Fetch metadata and the list of available streams."""

    # -- 3. Downloadability -------------------------------------------------
    def check_downloadable(self, info: VideoInfo) -> None:
        """Raise :class:`NotDownloadableError` when the media is off limits."""

        if not info.streams:
            raise NotDownloadableError(
                "该视频没有可用的下载流",
                detail=f"platform={self.platform.value} video={info.video_id}",
            )

    # -- 4. Stream selection ------------------------------------------------
    @abstractmethod
    def select_streams(self, info: VideoInfo, quality: str) -> DownloadPlan:
        """Pick the streams matching the requested quality preset."""

    # -- 5. Cover art / metadata sidecar ------------------------------------
    def cover_url(self, info: VideoInfo) -> str | None:
        return info.thumbnail_url

    # -- 6. Error translation ----------------------------------------------
    def describe(self) -> str:
        return self.display_name or self.platform.value
