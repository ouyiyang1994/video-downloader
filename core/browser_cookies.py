"""Read a platform session out of the browser the user actually logged in with.

This is the *only* acquisition mechanism the project uses, and it is the one
yt-dlp itself supports: ``--cookies-from-browser``. Nothing here decrypts a
browser's cookie store by hand, injects into a browser process, or touches a
browser profile; ``yt_dlp.cookies`` does the reading and we only keep the
cookies that belong to the platform being signed in to.

That comes with a real limitation which the code must not hide: Chromium 127+
protects cookies with App-Bound Encryption, and yt-dlp cannot decrypt them.
When that happens the caller is told precisely why, so it can fall back to
asking the user for the session value instead of pretending it worked.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from core.exceptions import BrowserCookieError

logger = logging.getLogger(__name__)

try:  # pragma: no cover - only absent off Windows
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


class BrowserFailure(StrEnum):
    """Why a browser's cookies could not be used."""

    NOT_INSTALLED = "not_installed"
    LOCKED = "locked"
    ENCRYPTED = "encrypted"
    NO_COOKIES = "no_cookies"
    UNKNOWN = "unknown"


#: Browsers whose cookie store yt-dlp can actually read on Windows. Firefox
#: keeps them in a plain SQLite file; every Chromium 127+ build protects them
#: with App-Bound Encryption, which yt-dlp cannot decrypt.
READABLE_BROWSERS: frozenset[str] = frozenset({"firefox"})

#: The Chromium family. Used to explain *why* a store cannot be read rather than
#: just that it could not.
CHROMIUM_BROWSERS: frozenset[str] = frozenset(
    {"chrome", "edge", "brave", "vivaldi", "opera", "chromium", "whale"}
)

#: Chromium writes this next to the profile and keeps it unlocked. Its
#: ``os_crypt.app_bound_encrypted_key`` entry is the definitive marker that the
#: browser has switched to App-Bound Encryption (v20 cookies).
LOCAL_STATE_NAME = "Local State"
APP_BOUND_KEY = "app_bound_encrypted_key"


#: ``ProgId`` fragment -> yt-dlp browser key. Checked case-insensitively against
#: the value Windows stores for the default https handler.
PROGID_HINTS: tuple[tuple[str, str], ...] = (
    ("chromehtml", "chrome"),
    ("msedgehtm", "edge"),
    ("firefoxurl", "firefox"),
    ("bravehtml", "brave"),
    ("vivaldihtm", "vivaldi"),
    ("operastable", "opera"),
    ("chromiumhtm", "chromium"),
    ("whalehtml", "whale"),
)

#: Browser key -> the user-data folder Windows puts it in. Used only to answer
#: "is this browser installed at all", never to read anything from it.
BROWSER_DATA_DIRS: dict[str, tuple[str, str]] = {
    "chrome": ("LOCALAPPDATA", "Google/Chrome/User Data"),
    "edge": ("LOCALAPPDATA", "Microsoft/Edge/User Data"),
    "brave": ("LOCALAPPDATA", "BraveSoftware/Brave-Browser/User Data"),
    "vivaldi": ("LOCALAPPDATA", "Vivaldi/User Data"),
    "chromium": ("LOCALAPPDATA", "Chromium/User Data"),
    "whale": ("LOCALAPPDATA", "Naver/Naver Whale/User Data"),
    "opera": ("APPDATA", "Opera Software/Opera Stable"),
    "firefox": ("APPDATA", "Mozilla/Firefox/Profiles"),
}

#: Human readable names for messages.
BROWSER_LABELS: dict[str, str] = {
    "chrome": "Chrome",
    "edge": "Edge",
    "firefox": "Firefox",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "chromium": "Chromium",
    "whale": "Whale",
}


def browser_label(browser: str) -> str:
    return BROWSER_LABELS.get(browser, browser)


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """The cookies of one platform, as read from one browser."""

    browser: str
    cookies: list[http.cookiejar.Cookie]

    @property
    def cookie_names(self) -> list[str]:
        """Sorted cookie *names* - safe to show, unlike the values."""

        return sorted({cookie.name for cookie in self.cookies})


@dataclass
class _Attempt:
    """One browser's outcome, kept so the failure message can list them all."""

    browser: str
    failure: BrowserFailure | None = None
    detail: str = ""
    cookies: list[http.cookiejar.Cookie] = field(default_factory=list)


# --- environment discovery ---------------------------------------------------


def default_browser() -> str | None:
    """The yt-dlp key of the Windows default browser, or ``None``.

    Read from ``UrlAssociations\\https\\UserChoice``, which is the same value
    the shell uses to open a link - so it is by construction the browser that
    ``webbrowser.open`` will start.
    """

    if winreg is None:  # pragma: no cover - non-Windows
        return None
    key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            prog_id = str(winreg.QueryValueEx(handle, "ProgId")[0])
    except OSError:
        return None
    return browser_from_progid(prog_id)


def browser_from_progid(prog_id: str) -> str | None:
    """Map a shell ``ProgId`` (``ChromeHTML``, ``MSEdgeHTM``, ...) to a key."""

    lowered = (prog_id or "").lower()
    for hint, browser in PROGID_HINTS:
        if hint in lowered:
            return browser
    return None


def browser_data_dir(browser: str) -> Path | None:
    """Where ``browser`` keeps its profiles, or ``None`` when unknown."""

    entry = BROWSER_DATA_DIRS.get(browser)
    if entry is None:
        return None
    variable, relative = entry
    base = os.environ.get(variable)
    if not base:
        return None
    return Path(base) / relative


def is_installed(browser: str) -> bool:
    directory = browser_data_dir(browser)
    return bool(directory and directory.is_dir())


def installed_browsers() -> list[str]:
    return [browser for browser in BROWSER_DATA_DIRS if is_installed(browser)]


def is_chromium_based(browser: str) -> bool:
    """True for the Chromium family, which is where App-Bound Encryption lives."""

    return browser in CHROMIUM_BROWSERS


def is_readable(browser: str) -> bool:
    """True when yt-dlp can realistically read this browser's cookies."""

    return browser in READABLE_BROWSERS


def local_state_path(browser: str) -> Path | None:
    """``Local State`` for ``browser``, or ``None`` when it has no such file."""

    if not is_chromium_based(browser):
        return None
    directory = browser_data_dir(browser)
    return None if directory is None else directory / LOCAL_STATE_NAME


def app_bound_encryption_enabled(browser: str) -> bool:
    """True when ``browser`` protects its cookies with App-Bound Encryption.

    Chromium publishes the marker in ``Local State``, which sits next to the
    profile and is *not* locked while the browser runs. That makes the answer
    available without touching the cookie database at all, so the user can be
    told up front that automatic reading will not work - instead of after a
    failed attempt that also cannot explain itself.
    """

    path = local_state_path(browser)
    if path is None or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        logger.debug("无法读取 %s 的 Local State：%s", browser, type(exc).__name__)
        return False
    if not isinstance(data, dict):
        return False
    os_crypt = data.get("os_crypt")
    if not isinstance(os_crypt, dict):
        return False
    value = os_crypt.get(APP_BOUND_KEY)
    return isinstance(value, str) and bool(value)


def candidate_browsers() -> list[str]:
    """Browsers to try, in the order that gets the user signed in fastest.

    1. the default browser - that is where the login page was opened, so that
       is where the session is;
    2. any browser whose store yt-dlp can actually read (Firefox), because a
       Chromium store will fail and waiting for it first wastes the user's time;
    3. the remaining installed browsers, so the failure report can still say
       what each one did.
    """

    ordered: list[str] = []
    preferred = default_browser()
    if preferred and is_installed(preferred):
        ordered.append(preferred)
    for browser in installed_browsers():
        if browser not in ordered and is_readable(browser):
            ordered.append(browser)
    for browser in installed_browsers():
        if browser not in ordered:
            ordered.append(browser)
    return ordered


@dataclass(frozen=True, slots=True)
class AutomaticReadOutlook:
    """Whether the browser login flow can realistically finish on its own.

    Computed from ``Local State`` alone - no cookie database is touched - so the
    sign-in dialog can warn the user *before* they spend time on a flow that is
    guaranteed to fail on this machine.
    """

    default_browser: str | None
    readable_browsers: tuple[str, ...]
    blocked_browsers: tuple[str, ...]
    any_installed: bool

    @property
    def possible(self) -> bool:
        """True when at least one installed browser can be read automatically."""

        return bool(self.readable_browsers)

    @property
    def default_readable(self) -> bool:
        return self.default_browser in self.readable_browsers

    @property
    def firefox_available(self) -> bool:
        return "firefox" in self.readable_browsers

    def advice(self) -> str:
        """One short paragraph the sign-in dialog shows up front."""

        if not self.any_installed:
            return (
                "未检测到任何已安装的浏览器。本程序通过浏览器自身的登录态获取会话，"
                "请用下面的「手动填写会话」提供会话值。"
            )
        default_label = (
            browser_label(self.default_browser) if self.default_browser else "系统默认浏览器"
        )
        if self.default_readable:
            return (
                f"{default_label} 的 Cookie 可以直接读取，"
                "登录后点击「我已登录，检测会话」即可自动获取。"
            )
        if self.default_browser and is_chromium_based(self.default_browser):
            # Chromium blocks outside access; the extension is the supported way
            # in, so it is offered first - not as a workaround but as the route
            # Chrome itself provides.
            return (
                f"{default_label} 用 App-Bound Encryption 保护 Cookie，外部程序无法读取。"
                "本程序提供「Chrome 登录助手」扩展：由 Chrome 自己解密并把会话交给本程序，"
                "一次性安装后即可自动获取。也可以改用 Firefox，或用「手动填写会话」。"
            )
        if self.firefox_available:
            return (
                f"{default_label} 的 Cookie 无法自动读取。检测到本机已安装 Firefox："
                "在 Firefox 中登录后可自动获取；"
                "也可以用「Chrome 登录助手」扩展，或下面的「手动填写会话」。"
            )
        return (
            f"{default_label} 的 Cookie 无法自动读取。"
            "请用「Chrome 登录助手」扩展（一次性安装），"
            "或安装 Firefox 后在 Firefox 中登录，也可以用下面的「手动填写会话」。"
        )


def automatic_read_outlook() -> AutomaticReadOutlook:
    """Summarise what automatic reading can achieve on this machine."""

    installed = installed_browsers()
    readable = tuple(browser for browser in installed if is_readable(browser))
    blocked = tuple(browser for browser in installed if not is_readable(browser))
    return AutomaticReadOutlook(
        default_browser=default_browser(),
        readable_browsers=readable,
        blocked_browsers=blocked,
        any_installed=bool(installed),
    )


# --- extraction --------------------------------------------------------------


class _QuietLogger:
    """Swallows yt-dlp's cookie chatter.

    Its messages are not guaranteed to be value-free, so none of them is
    forwarded; the classified :class:`BrowserCookieError` is what the user sees.
    """

    def debug(self, message: str) -> None:  # noqa: D102 - yt-dlp interface
        pass

    def info(self, message: str) -> None:  # noqa: D102
        pass

    def warning(self, message: str) -> None:  # noqa: D102
        logger.debug("yt-dlp 浏览器 Cookie 警告已抑制")

    def error(self, message: str) -> None:  # noqa: D102
        logger.debug("yt-dlp 浏览器 Cookie 错误已抑制")


def classify_failure(message: str) -> BrowserFailure:
    """Turn a yt-dlp failure message into an actionable reason."""

    text = (message or "").lower()
    if "could not copy" in text or "being used by another" in text:
        return BrowserFailure.LOCKED
    if "decrypt" in text or "dpapi" in text or "app-bound" in text:
        return BrowserFailure.ENCRYPTED
    if "could not find" in text or "not found" in text or "unsupported" in text:
        return BrowserFailure.NOT_INSTALLED
    return BrowserFailure.UNKNOWN


def failure_message(browser: str, failure: BrowserFailure) -> tuple[str, str]:
    """``(short message, what the user can do)`` for a failure."""

    label = browser_label(browser)
    if failure is BrowserFailure.LOCKED:
        return (
            f"无法读取 {label} 的 Cookie：数据库被浏览器占用",
            f"请完全退出 {label}（含后台常驻进程）后重试；本程序不会修改或删除浏览器数据。",
        )
    if failure is BrowserFailure.ENCRYPTED:
        return (
            f"无法直接解密 {label} 的 Cookie（App-Bound Encryption）",
            (
                f"{label} 用 v20 应用绑定加密保护 Cookie，外部程序无法解密，"
                "与是否登录、是否关闭浏览器都无关。\n"
                "首选方案：安装「Chrome 登录助手」扩展 —— 由 Chrome 自己解密并把会话"
                "交给本程序，扩展只申请读取哔哩哔哩 / Instagram 两个域名；\n"
                "其次：改用 Firefox 登录（Firefox 的 Cookie 不加密，可以自动读取）；\n"
                "最后：用下面的「手动填写会话」直接提供会话值。"
            ),
        )
    if failure is BrowserFailure.NOT_INSTALLED:
        return (
            f"未找到 {label} 的 Cookie 数据库",
            f"确认 {label} 已安装并至少启动过一次，或改用其他方式提供会话。",
        )
    if failure is BrowserFailure.NO_COOKIES:
        return (
            f"{label} 中没有该平台的登录 Cookie",
            "请先在浏览器里完成登录，然后回到本窗口点击「我已登录」。",
        )
    return (
        f"读取 {label} 的 Cookie 失败",
        "可在浏览器中确认已登录后重试，或使用下面的「手动填写会话」。",
    )


def extract_cookies(browser: str, *, domain_suffix: str) -> list[http.cookiejar.Cookie]:
    """Return the cookies ``browser`` holds for ``domain_suffix``.

    Raises :class:`BrowserCookieError` with a classified ``reason`` when the
    store cannot be read, and with ``NO_COOKIES`` when it can but holds nothing
    for this platform.
    """

    from yt_dlp.cookies import extract_cookies_from_browser

    from core.session_store import matches_domain

    try:
        jar = extract_cookies_from_browser(browser, logger=_QuietLogger())
    except Exception as exc:  # yt-dlp raises DownloadError and friends
        failure = classify_failure(str(exc))
        # App-Bound Encryption is a hard stop: closing the browser, retrying, or
        # logging in again cannot change it, so it is reported as the reason
        # whenever the marker says the browser has switched to v20.
        if failure is not BrowserFailure.NOT_INSTALLED and app_bound_encryption_enabled(browser):
            failure = BrowserFailure.ENCRYPTED
        message, hint = failure_message(browser, failure)
        raise BrowserCookieError(
            message, reason=failure.value, browser=browser, detail=hint
        ) from exc

    cookies = [cookie for cookie in jar if matches_domain(cookie.domain, domain_suffix)]
    if not cookies:
        message, hint = failure_message(browser, BrowserFailure.NO_COOKIES)
        raise BrowserCookieError(
            message, reason=BrowserFailure.NO_COOKIES.value, browser=browser, detail=hint
        )
    return cookies


def extract_session(domain_suffix: str) -> BrowserSession:
    """Try every candidate browser and return the first usable session.

    When nothing works the raised error names every browser that was tried and
    the reason each one failed, so the user is never left guessing.
    """

    candidates = candidate_browsers()
    if not candidates:
        raise BrowserCookieError(
            "未检测到任何已安装的浏览器",
            reason=BrowserFailure.NOT_INSTALLED.value,
            browser="",
            detail=(
                "本程序通过浏览器自身的登录态获取会话，因此需要至少安装一个浏览器。"
                "也可以用下面的「手动填写会话」直接提供会话值。"
            ),
        )

    attempts: list[_Attempt] = []
    for browser in candidates:
        attempt = _Attempt(browser=browser)
        try:
            cookies = extract_cookies(browser, domain_suffix=domain_suffix)
        except BrowserCookieError as exc:
            attempt.failure = BrowserFailure(exc.reason)
            attempt.detail = exc.detail or ""
            attempts.append(attempt)
            logger.info("从 %s 读取会话失败：%s", browser, attempt.failure.value)
            continue
        logger.info("已从 %s 读取到该平台的登录 Cookie（%d 个）", browser, len(cookies))
        return BrowserSession(browser=browser, cookies=cookies)

    raise _aggregate_failure(attempts)


def _aggregate_failure(attempts: list[_Attempt]) -> BrowserCookieError:
    """Build one error that explains every browser that was tried."""

    # Prefer a reason that tells the user something actionable.
    priority = (
        BrowserFailure.ENCRYPTED,
        BrowserFailure.LOCKED,
        BrowserFailure.NO_COOKIES,
        BrowserFailure.NOT_INSTALLED,
        BrowserFailure.UNKNOWN,
    )
    chosen = next(
        (attempt for reason in priority for attempt in attempts if attempt.failure is reason),
        attempts[0],
    )
    failure = chosen.failure or BrowserFailure.UNKNOWN
    message, hint = failure_message(chosen.browser, failure)

    lines = []
    for attempt in attempts:
        reason = attempt.failure or BrowserFailure.UNKNOWN
        short, _ = failure_message(attempt.browser, reason)
        lines.append(f"- {short}")

    notes: list[str] = []
    if failure is BrowserFailure.ENCRYPTED and not any(
        is_readable(b) for b in installed_browsers()
    ):
        notes.append(
            "本机未检测到 Firefox；可以在 Video Downloader 里安装「Chrome 登录助手」扩展"
            "（由 Chrome 自己解密，一次性安装），或在 Firefox 中登录后自动获取会话。"
        )

    detail = "\n".join([hint, *notes, "", "各浏览器结果：", *lines])
    return BrowserCookieError(message, reason=failure.value, browser=chosen.browser, detail=detail)
