"""Project exception hierarchy.

Every failure raised by an adapter is one of these so the CLI can render a
useful message instead of a traceback.
"""

from __future__ import annotations


class VideoDownloaderError(Exception):
    """Base class for all project errors."""

    exit_code: int = 1

    #: Whether another attempt could plausibly succeed. Network timeouts, HTTP
    #: 5xx and truncated bodies are worth retrying; a path that is too long or
    #: a folder that has run out of names is not - retrying only delays the
    #: message the user needs to see.
    retryable: bool = True

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message} ({self.detail})" if self.detail else self.message


class UnsupportedUrlError(VideoDownloaderError):
    """The URL does not belong to any registered platform."""

    exit_code = 2
    retryable = False


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
    retryable = False


class AuthRequiredError(VideoDownloaderError):
    """Credentials are required for this request."""

    exit_code = 5
    retryable = False


class RateLimitedError(VideoDownloaderError):
    """The platform throttled us."""

    exit_code = 6


class DownloadError(VideoDownloaderError):
    """Bytes could not be transferred."""

    exit_code = 7


class TargetFileError(DownloadError):
    """The finished file could not be moved onto the target path.

    Raised when another process holds the destination open, or the folder
    refuses the write. The payload is already complete next to it, so the
    download loop deliberately does **not** retry this one - running the
    transfer again would only fetch bytes that are already on disk. Callers
    that want to wait the lock out retry the rename alone.

    A *busy* file is worth another attempt later, so this stays retryable at
    the queue level; the "somebody else took the name" case is
    :class:`TargetFileConflictError`.
    """

    exit_code = 15


class TargetFileConflictError(TargetFileError):
    """The destination is no longer the file this download may replace.

    Another program created the name, or swapped the file that a forced
    re-download was allowed to overwrite. Nothing about the download is wrong -
    the permission to write *there* is gone - so retrying the task would only
    re-fetch metadata and pick a different name, which is not what the user
    asked for. The task fails at once and shows this message.
    """

    exit_code = 19
    retryable = False


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


class PathTooLongError(VideoDownloaderError):
    """The output path would exceed the Windows path limit.

    Raised *before* anything is downloaded, when the fixed part of the path
    (output folder, platform, date) already leaves no room for a file name.
    Truncating the folder the user chose would write the file somewhere they
    did not ask for, so this is an error rather than a silent adjustment.
    """

    exit_code = 16
    retryable = False


class OutputLockError(VideoDownloaderError):
    """The cross-process output lock could not be established.

    The lock is what stops two instances of the app from choosing the same file
    name and writing the same ``.part``. When it cannot be created there is no
    safe way to continue, so the download stops instead of gambling on the
    in-process check alone.
    """

    exit_code = 17
    retryable = False


class OutputNameConflictError(VideoDownloaderError):
    """Every candidate file name for this download is already taken.

    Raised after the full range of ``"Title (n).ext"`` names has been tried, so
    the user learns the folder is saturated instead of seeing an internal error.
    """

    exit_code = 18
    retryable = False


class DownloadCancelled(VideoDownloaderError):
    """The caller asked to stop an in-flight download.

    Only raised when a ``cancel_event`` was supplied; the CLI never passes one,
    so its behaviour is unchanged.
    """

    exit_code = 130
