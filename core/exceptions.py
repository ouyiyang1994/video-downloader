"""Project exception hierarchy.

Every failure raised by an adapter is one of these so the CLI can render a
useful message instead of a traceback.
"""

from __future__ import annotations


class VideoDownloaderError(Exception):
    """Base class for all project errors."""

    exit_code: int = 1

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message} ({self.detail})" if self.detail else self.message


class UnsupportedUrlError(VideoDownloaderError):
    """The URL does not belong to any registered platform."""

    exit_code = 2


class MetadataError(VideoDownloaderError):
    """Video metadata could not be retrieved."""

    exit_code = 3


class NotDownloadableError(VideoDownloaderError):
    """The video exists but may not be downloaded.

    Raised for access-controlled, DRM-protected, paid or otherwise
    permissioned content, and when the caller has not confirmed they are
    allowed to download the media.
    """

    exit_code = 4


class AuthRequiredError(VideoDownloaderError):
    """Credentials are required for this request."""

    exit_code = 5


class RateLimitedError(VideoDownloaderError):
    """The platform throttled us."""

    exit_code = 6


class DownloadError(VideoDownloaderError):
    """Bytes could not be transferred."""

    exit_code = 7


class IntegrityError(VideoDownloaderError):
    """The downloaded file failed verification."""

    exit_code = 8


class MergeError(VideoDownloaderError):
    """Audio/video muxing failed."""

    exit_code = 9


class DatabaseError(VideoDownloaderError):
    """SQLite interaction failed."""

    exit_code = 10


class CookieAccessError(VideoDownloaderError):
    """Browser cookies could not be read (locked database, wrong profile, ...).

    Deliberately does not carry any cookie value: only the reason and the
    action the user has to take.
    """

    exit_code = 11


class BrowserCookieError(VideoDownloaderError):
    """The login flow could not obtain a session from the browser.

    ``reason`` is a :class:`core.browser_cookies.BrowserFailure` value so the
    caller can offer the right next step - retry after closing the browser, use
    a different browser, or ask the user for the session value directly.
    """

    exit_code = 12

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        browser: str = "",
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.reason = reason
        self.browser = browser


class ChromeBridgeError(VideoDownloaderError):
    """The Chrome extension bridge could not be installed or inspected.

    Raised for a missing extension folder, an unparsable manifest, or a failed
    registry write. Never carries a cookie value.
    """

    exit_code = 14


class SessionValueError(VideoDownloaderError):
    """A manually supplied session value could not be used.

    Raised when the pasted text is empty, malformed, or rejected by the
    platform. Never carries the value itself.
    """

    exit_code = 13


class DownloadCancelled(VideoDownloaderError):
    """The caller asked to stop an in-flight download.

    Only raised when a ``cancel_event`` was supplied; the CLI never passes one,
    so its behaviour is unchanged.
    """

    exit_code = 130
