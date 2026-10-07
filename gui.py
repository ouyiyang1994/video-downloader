"""PySide6 desktop GUI.

A thin presentation layer over the *same* download core the CLI uses:
``DownloadService`` / ``PlatformRegistry`` / ``DownloadDatabase`` /
``build_client``. No downloading, parsing, proxying or database logic lives
here - the GUI only collects input, runs the core on a worker thread and
renders what the core reports.

Run with::

    .\\.venv\\Scripts\\python.exe gui.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import traceback
import types
import webbrowser
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIntValidator, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.constants import (
    DEFAULT_PROXY_HOST,
    DEFAULT_PROXY_PORT,
    QUALITY_AUDIO_ONLY,
    QUALITY_PRESETS,
    SUPPORTED_PLATFORM_IDS,
)
from config.settings import (
    DATA_ROOT,
    ProxyConfig,
    Settings,
    apply_proxy_configuration,
    get_settings,
    save_proxy_configuration,
)
from core.browser_cookies import (
    automatic_read_outlook,
    browser_label,
    extract_session,
)
from core.database import DownloadDatabase
from core.downloader import ProgressUpdate
from core.exceptions import (
    AuthRequiredError,
    BrowserCookieError,
    CookieAccessError,
    DatabaseError,
    DownloadCancelled,
    DownloadError,
    IntegrityError,
    MergeError,
    MetadataError,
    NotDownloadableError,
    RateLimitedError,
    SessionValueError,
    UnsupportedUrlError,
    VideoDownloaderError,
)
from core.http import build_client, check_proxy
from core.interfaces import PlatformAdapter
from core.logging_setup import setup_logging
from core.login import (
    LoginState,
    SessionStatus,
    parse_session_text,
)
from core.merger import locate_ffmpeg
from core.models import DownloadResult, DownloadStatus, Platform
from core.registry import PlatformRegistry, build_registry
from core.service import DownloadService
from core.session_store import (
    delete_session,
    has_session,
    header_from_cookies,
    write_session,
)

logger = logging.getLogger(__name__)

# --- user-facing text -------------------------------------------------------

PLATFORM_IDLE_TEXT = "等待输入链接"
UNSUPPORTED_PLATFORM_TEXT = "暂不支持该平台"
RIGHTS_UNCHECKED_TEXT = "请先勾选「我确认拥有下载该内容的权利」"
EMPTY_URL_TEXT = "请先粘贴视频链接"

# --- sign-in button text ----------------------------------------------------
#
# Kept as constants so the "in progress" wording can never drift from the
# wording the tests assert on.

DETECT_BUTTON_TEXT = "我已登录，检测会话"
DETECT_BUSY_TEXT = "检测中…"
PASTE_BUTTON_TEXT = "保存并验证"
PASTE_BUSY_TEXT = "验证中…"
LOGIN_BUSY_TEXT = "检测中…"

#: Quality preset -> Chinese label shown in the combo box.
QUALITY_LABELS: dict[str, str] = {
    "best": "最佳",
    "2160p": "2160p (4K)",
    "1440p": "1440p (2K)",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    "360p": "360p",
    "audio": "仅音频 (m4a)",
}
QUALITY_CHOICES: tuple[str, ...] = (*QUALITY_PRESETS.keys(), QUALITY_AUDIO_ONLY)

#: DownloadStatus -> Chinese label for the history table.
STATUS_LABELS: dict[str, str] = {
    DownloadStatus.PENDING.value: "等待中",
    DownloadStatus.DOWNLOADING.value: "下载中",
    DownloadStatus.MERGING.value: "合并中",
    DownloadStatus.COMPLETED.value: "已完成",
    DownloadStatus.SKIPPED.value: "已跳过",
    DownloadStatus.FAILED.value: "失败",
    DownloadStatus.CANCELLED.value: "已取消",
}

HISTORY_COLUMNS = ("平台", "标题", "作者", "画质", "下载时间", "状态", "文件路径")


# --- pure helpers (no Qt objects, directly unit-testable) -------------------


def detect_platform(url: str, registry: PlatformRegistry | None) -> tuple[Platform | None, str]:
    """Return ``(platform, display_text)`` for a URL typed into the GUI.

    Uses the very same registry the CLI uses, so recognition can never drift
    between the two front ends.
    """

    cleaned = (url or "").strip()
    if not cleaned:
        return None, PLATFORM_IDLE_TEXT
    if registry is None:
        return None, PLATFORM_IDLE_TEXT
    try:
        adapter = registry.resolve(cleaned)
    except UnsupportedUrlError:
        return None, UNSUPPORTED_PLATFORM_TEXT
    return adapter.platform, adapter.platform.display_name


def format_size(num_bytes: float | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num_bytes)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.0f} {units[index]}" if index == 0 else f"{value:.1f} {units[index]}"


def format_speed(bps: float | None) -> str:
    if not bps or bps <= 0:
        return "-"
    return f"{format_size(bps)}/s"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "-"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def format_resolution(resolution: str | None) -> str:
    """``1920x1080`` -> ``1920×1080`` (portrait stays ``1080×1920``)."""

    if not resolution:
        return "-"
    return resolution.replace("x", "×").replace("X", "×")


def describe_quality(resolution: str | None, quality_label: str | None) -> str:
    """What was *actually* downloaded, e.g. ``1920×1080 ｜ 1080p``."""

    parts = []
    if resolution:
        parts.append(format_resolution(resolution))
    if quality_label and quality_label != "unknown":
        parts.append(quality_label)
    return " ｜ ".join(parts) if parts else "-"


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """Walk ``__cause__``/``__context__`` so wrapped errors stay classifiable."""

    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _network_hint(exc: BaseException, proxy: str | None) -> str:
    # The core wraps transport errors in DownloadError, so inspect the whole
    # cause chain rather than only the outermost exception.
    for candidate in _cause_chain(exc):
        if isinstance(candidate, (httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError)):
            if proxy:
                return f"代理连接失败，请确认 {proxy} 正在运行"
            return "网络连接失败，请检查网络"
        if isinstance(candidate, httpx.TimeoutException):
            return "网络超时，请稍后重试"
        if isinstance(candidate, OSError) and getattr(candidate, "errno", None) == 28:
            return "磁盘空间不足，请更换保存位置"
    return "网络连接失败"


def friendly_error(
    exc: BaseException,
    *,
    platform: Platform | None = None,
    proxy: str | None = None,
    session_present: bool | None = None,
) -> str:
    """Translate an internal exception into a short, safe user message.

    Never includes the raw exception text, a URL with query tokens, or anything
    read from a cookies file; the full detail stays in ``logs/downloader.log``.

    ``session_present`` lets the caller distinguish "you never signed in" from
    "the session you signed in with has expired", which need different advice.
    """

    if isinstance(exc, DownloadCancelled):
        return "已取消"
    if isinstance(exc, UnsupportedUrlError):
        return UNSUPPORTED_PLATFORM_TEXT
    if isinstance(exc, BrowserCookieError):
        return f"无法从浏览器获取登录会话：{exc.message}（可在「账号登录」里手动填写会话）"
    if isinstance(exc, SessionValueError):
        return f"登录会话不可用：{exc.message}"
    if isinstance(exc, CookieAccessError):
        text = str(exc)
        if "解密" in text or "App-Bound" in text:
            return "浏览器 Cookie 无法解密，请在「账号登录」里登录或手动填写会话"
        if "未找到" in text:
            return "未找到 Cookie 文件，请在「账号登录」里重新登录"
        return "Cookie 读取失败，请在「账号登录」里重新登录"
    if isinstance(exc, AuthRequiredError):
        name = platform.display_name if platform is not None else "该平台"
        if session_present:
            return f"{name} 登录已过期，请在「账号登录」里重新登录"
        return f"{name} 需要登录后才能访问该内容，请在「账号登录」里完成登录"
    if isinstance(exc, RateLimitedError):
        return "请求过于频繁，已被平台限流，请稍后重试"
    if isinstance(exc, NotDownloadableError):
        return "该视频不可下载（可能为私密、付费或受地区限制）"
    if isinstance(exc, MetadataError):
        return "视频不存在或无法访问"
    if isinstance(exc, MergeError):
        return "音视频合并失败（ffmpeg）"
    if isinstance(exc, IntegrityError):
        return "下载文件校验失败，请重试"
    if isinstance(exc, DatabaseError):
        return "写入下载记录失败"
    if isinstance(exc, DownloadError):
        return _network_hint(exc, proxy)
    if isinstance(exc, httpx.HTTPError):
        return _network_hint(exc, proxy)
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "磁盘空间不足，请更换保存位置"
    if isinstance(exc, VideoDownloaderError):
        return "下载失败，详情见 logs/downloader.log"
    return "下载失败，详情见 logs/downloader.log"


# --- background worker ------------------------------------------------------
#
# The download core is async, so the worker owns a private event loop inside its
# own QThread. Progress signals are emitted from that thread and delivered to
# the GUI thread by Qt, so the window never blocks and never touches the loop.


class DownloadWorker(QThread):
    """Runs :meth:`DownloadService.download` off the GUI thread."""

    progressed = Signal(object)  # ProgressUpdate
    succeeded = Signal(object)  # DownloadResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        settings: Settings,
        url: str,
        quality: str,
        output_dir: Path | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.url = url
        self.quality = quality
        self.output_dir = output_dir
        self.cancel_event = threading.Event()
        self.platform: Platform | None = None
        self.requires_rights = False

    def cancel(self) -> None:
        """Ask the running download to stop. Safe to call from the GUI thread."""

        self.cancel_event.set()

    def run(self) -> None:
        """QThread entry point: never let a traceback escape to the GUI."""

        try:
            result = asyncio.run(self._download())
        except DownloadCancelled:
            self.cancelled.emit()
        except VideoDownloaderError as exc:
            self.failed.emit(self._friendly(exc))
        except Exception as exc:  # noqa: BLE001 - last line of defence
            logger.exception("GUI 下载任务异常")
            self.failed.emit(self._friendly(exc))
        else:
            self.succeeded.emit(result)

    def _friendly(self, exc: BaseException) -> str:
        """Render ``exc``, telling "never signed in" apart from "expired"."""

        present = has_session(self.settings, self.platform) if self.platform is not None else None
        return friendly_error(
            exc,
            platform=self.platform,
            proxy=self.settings.proxy,
            session_present=present,
        )

    async def _download(self) -> DownloadResult:
        client = build_client(self.settings)
        registry: PlatformRegistry | None = None
        database = DownloadDatabase(self.settings)
        database.connect()
        try:
            registry = build_registry(self.settings, client)
            adapter = registry.resolve(self.url)
            self.platform = adapter.platform
            self.requires_rights = adapter.requires_rights_confirmation
            service = DownloadService(self.settings, registry, client, database)
            return await service.download(
                self.url,
                quality=self.quality,
                output_dir=self.output_dir,
                progress={"video": self._emit_progress, "audio": self._emit_progress},
                cancel_event=self.cancel_event,
            )
        finally:
            if registry is not None:
                for extra in registry.owned_clients:
                    await extra.aclose()
            await client.aclose()
            database.close()

    def _emit_progress(self, update: ProgressUpdate) -> None:
        self.progressed.emit(update)


class ProxyTestWorker(QThread):
    """Runs the proxy reachability check off the GUI thread."""

    tested = Signal(bool, str)

    def __init__(self, proxy_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.proxy_url = proxy_url

    def run(self) -> None:
        try:
            ok, message = asyncio.run(check_proxy(self.proxy_url))
        except Exception as exc:  # noqa: BLE001 - the GUI only needs the verdict
            logger.exception("代理测试异常")
            ok, message = False, f"代理测试失败（{type(exc).__name__}）"
        self.tested.emit(ok, message)


# --- sign-in ----------------------------------------------------------------


def wait_for_thread(thread: QThread | None, timeout_ms: int = 3000) -> None:
    """Wait for ``thread`` unless its C++ object has already been destroyed.

    Workers are usually paired with ``finished.connect(worker.deleteLater)``, so
    a worker that has already run can be gone by the time the window closes.
    The Python attribute then still exists but points at a deleted C++ object,
    and touching it raises ``RuntimeError`` - which, inside ``closeEvent``, used
    to stop the window from closing at all.
    """

    if thread is None:
        return
    try:
        if thread.isRunning():
            thread.wait(timeout_ms)
    except RuntimeError:
        # Already deleted; there is nothing left to wait for.
        logger.debug("线程对象已被销毁，跳过等待")


class LoginAction(StrEnum):
    """What a :class:`LoginWorker` was asked to do."""

    #: Only ask the platform about the session that is already stored.
    STATUS = "status"
    #: Read the session out of the browser the user just logged in with.
    ACQUIRE = "acquire"
    #: Validate a session value the user pasted, then store it.
    PASTE = "paste"


class LoginWorker(QThread):
    """Runs one sign-in step off the GUI thread.

    Both steps are slow enough to need their own thread: reading a browser's
    cookie store touches a locked SQLite database, and validating a session is a
    network round trip. Nothing here touches a widget - results go out as
    signals, exactly like :class:`DownloadWorker`.
    """

    status_ready = Signal(object)  # SessionStatus
    browser_unavailable = Signal(object)  # BrowserCookieError
    failed = Signal(str, str)  # (message, detail)

    def __init__(
        self,
        settings: Settings,
        adapter: PlatformAdapter,
        action: LoginAction,
        text: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.adapter = adapter
        self.action = action
        self.text = text

    def run(self) -> None:
        """QThread entry point: never let a traceback escape to the GUI."""

        try:
            status = asyncio.run(self._run())
        except BrowserCookieError as exc:
            self.browser_unavailable.emit(exc)
        except SessionValueError as exc:
            self.failed.emit(exc.message, exc.detail or "")
        except VideoDownloaderError as exc:
            self.failed.emit(exc.message, exc.detail or "")
        except Exception as exc:  # noqa: BLE001 - last line of defence
            logger.exception("登录任务异常")
            self.failed.emit("操作失败，详情见 logs/downloader.log", type(exc).__name__)
        else:
            self.status_ready.emit(status)

    async def _run(self) -> SessionStatus:
        # The window keeps one registry for its whole lifetime, so whatever the
        # adapter cached before the user signed in or out is stale by now.
        self.adapter.reload_session()
        if self.action is LoginAction.STATUS:
            return await self._check(self.adapter.session_cookie_header())
        if self.action is LoginAction.ACQUIRE:
            return await self._acquire()
        if self.action is LoginAction.PASTE:
            return await self._save_pasted()
        raise ValueError(f"未知的登录动作：{self.action}")

    async def _check(self, header: str | None) -> SessionStatus:
        """Ask the platform about ``header`` using a short-lived client."""

        client = build_client(self.settings)
        registry: PlatformRegistry | None = None
        try:
            registry = build_registry(self.settings, client)
            adapter = registry.get(self.adapter.platform)
            return await adapter.check_session(header)
        finally:
            if registry is not None:
                for extra in registry.owned_clients:
                    await extra.aclose()
            await client.aclose()

    async def _acquire(self) -> SessionStatus:
        """Read the session from the browser, prove it works, then store it."""

        session = extract_session(self.adapter.session_domain)
        status = await self._check(header_from_cookies(session.cookies))
        self._reject_only_when_the_platform_says_no(status, source=browser_label(session.browser))
        write_session(
            self.settings,
            self.adapter.platform,
            session.cookies,
            domain_suffix=self.adapter.session_domain,
        )
        logger.info("已从 %s 保存 %s 的登录会话", session.browser, self.adapter.platform.value)
        return status

    async def _save_pasted(self) -> SessionStatus:
        """Validate a pasted session value first, and only then store it."""

        cookies = parse_session_text(
            self.text,
            cookie_names=self.adapter.session_cookie_names,
            domain=f".{self.adapter.session_domain}",
        )
        status = await self._check(header_from_cookies(cookies))
        self._reject_only_when_the_platform_says_no(status, source="手动填写")
        write_session(
            self.settings,
            self.adapter.platform,
            cookies,
            domain_suffix=self.adapter.session_domain,
        )
        logger.info("已保存手动填写的 %s 登录会话", self.adapter.platform.value)
        return status

    @staticmethod
    def _reject_only_when_the_platform_says_no(status: SessionStatus, *, source: str) -> None:
        """Refuse a session the platform explicitly rejected - and only then.

        ``UNKNOWN`` means the platform could not be reached (offline, HTTP 412
        risk control, ...). Throwing the session away because of a transient
        network problem would be worse than storing it and saying it is
        unconfirmed, so that case is allowed through.
        """

        if status.state in (LoginState.EXPIRED, LoginState.LOGGED_OUT):
            raise SessionValueError(
                f"平台判定该会话未登录（来源：{source}）",
                detail=status.detail or "请确认已经登录，然后重试。",
            )


class LoginDialog(QDialog):
    """Sign in to one platform through the user's own browser.

    The browser does the signing in. This dialog only opens the platform's
    official login page, reads back the session the browser ended up with, and -
    when the browser's cookie store cannot be read at all - asks the user to
    paste the session value. No password, verification code or two-factor value
    is ever collected here.
    """

    def __init__(
        self,
        settings: Settings,
        adapter: PlatformAdapter,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.adapter = adapter
        self.platform = adapter.platform
        self.worker: LoginWorker | None = None
        #: The verdict when the dialog is accepted, so the caller can refresh.
        self.result_status: SessionStatus | None = None

        self.setWindowTitle(f"登录 {adapter.display_name}")
        self.setMinimumWidth(620)
        self._build_ui()
        self._open_login_page()

    # -- construction -------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        outlook = automatic_read_outlook()
        detected = outlook.default_browser
        browser_text = browser_label(detected) if detected else "系统默认浏览器"
        intro = QLabel(
            f"1. 已用 {browser_text} 打开 {self.adapter.display_name} 的官方登录页面\n"
            "2. 请在浏览器里自行完成登录（包括两步验证）\n"
            f"3. 登录完成后回到本窗口，点击「{DETECT_BUTTON_TEXT}」"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Say up front whether the automatic step can work at all, instead of
        # letting the user log in and only then discover that it cannot.
        self.advice_label = QLabel(outlook.advice())
        self.advice_label.setWordWrap(True)
        self.advice_label.setStyleSheet(
            f"color: {'#1a7f37' if outlook.default_readable else '#9a6700'};"
        )
        layout.addWidget(self.advice_label)

        privacy = QLabel(
            "本程序不会读取或保存你的密码、验证码、两步验证信息，"
            "也不会修改或删除浏览器里的任何 Cookie。"
        )
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #57606a;")
        layout.addWidget(privacy)

        actions = QHBoxLayout()
        self.reopen_button = QPushButton("重新打开登录页")
        self.reopen_button.clicked.connect(self._open_login_page)
        actions.addWidget(self.reopen_button)
        self.detect_button = QPushButton(DETECT_BUTTON_TEXT)
        self.detect_button.setObjectName("primary")
        self.detect_button.clicked.connect(self._detect)
        actions.addWidget(self.detect_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.manual_box = QGroupBox("手动填写会话")
        manual = QVBoxLayout(self.manual_box)
        self.failure_label = QLabel("")
        self.failure_label.setWordWrap(True)
        self.failure_label.setStyleSheet("color: #d1242f;")
        manual.addWidget(self.failure_label)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #57606a;")
        manual.addWidget(self.hint_label)

        cookie_name = self.adapter.session_cookie_names[0]
        howto = QLabel(
            "在已登录的浏览器中按 F12 打开开发者工具 → Application（应用）→ Cookies → "
            f"https://www.{self.adapter.session_domain} → 复制 {cookie_name} 的值，"
            "或直接复制整行 Cookie 粘贴到下面。"
        )
        howto.setWordWrap(True)
        manual.addWidget(howto)

        row = QHBoxLayout()
        self.paste_edit = QLineEdit()
        self.paste_edit.setPlaceholderText(f"{cookie_name}=… 或该 Cookie 的值")
        self.paste_edit.returnPressed.connect(self._save_pasted)
        row.addWidget(self.paste_edit, 1)
        self.save_button = QPushButton(PASTE_BUTTON_TEXT)
        self.save_button.clicked.connect(self._save_pasted)
        row.addWidget(self.save_button)
        manual.addLayout(row)

        self.manual_box.setVisible(not outlook.default_readable)
        layout.addWidget(self.manual_box)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = QPushButton("取消")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

    # -- browser ------------------------------------------------------------
    def _open_login_page(self) -> None:
        """Open the platform's official login page in the default browser."""

        url = self.adapter.login_url
        if not url:
            return
        try:
            opened = webbrowser.open(url, new=2)
        except Exception:  # noqa: BLE001 - webbrowser can raise anything
            logger.exception("打开登录页失败")
            opened = False
        if opened:
            self._set_status(f"已打开官方登录页面：{url}", ok=None)
        else:
            self._set_status(f"无法自动打开浏览器，请手动访问：{url}", ok=False)

    # -- actions ------------------------------------------------------------
    def _detect(self) -> None:
        self._start(LoginAction.ACQUIRE)

    def _save_pasted(self) -> None:
        text = self.paste_edit.text().strip()
        if not text:
            self._set_status("请先粘贴会话内容", ok=False)
            return
        self._start(LoginAction.PASTE, text)

    def _start(self, action: LoginAction, text: str = "") -> None:
        if self.worker is not None and self.worker.isRunning():
            # A run is already in flight. The buttons are disabled, so this is
            # only reachable through a stray signal - ignore it rather than
            # starting a second check.
            return
        self._set_busy(True, action=action)
        self._set_status(
            "正在从浏览器读取会话并向平台校验…"
            if action is LoginAction.ACQUIRE
            else "正在向平台校验你填写的会话…",
            ok=None,
        )
        worker = LoginWorker(self.settings, self.adapter, action, text, self)
        worker.status_ready.connect(self._on_status)
        worker.browser_unavailable.connect(self._on_browser_unavailable)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self.worker = worker
        worker.start()

    # -- worker callbacks (always run on the GUI thread) --------------------
    def _on_status(self, status: SessionStatus) -> None:
        self.result_status = status
        if status.logged_in:
            self._set_status(f"登录成功：{status.summary()}", ok=True)
            self.accept()
            return
        if status.state is LoginState.UNKNOWN:
            # The session was stored, but the platform could not be reached to
            # confirm it. Saying so beats claiming a success we did not verify.
            self._set_status(f"会话已保存，但暂时无法向平台确认：{status.detail}", ok=None)
            self.accept()
            return
        self._set_status(f"检测结果：{status.summary()}。{status.detail}", ok=False)
        self.manual_box.show()

    def _on_browser_unavailable(self, error: BrowserCookieError) -> None:
        self.failure_label.setText(error.message)
        self.hint_label.setText(error.detail or "")
        self.manual_box.show()
        self._set_status("自动获取会话失败，请使用下面的「手动填写会话」", ok=False)

    def _on_failed(self, message: str, detail: str) -> None:
        self.failure_label.setText(message)
        self.hint_label.setText(detail)
        self.manual_box.show()
        self._set_status(message, ok=False)

    def _on_finished(self) -> None:
        self._set_busy(False)
        self.worker = None

    # -- small helpers ------------------------------------------------------
    def _set_status(self, text: str, *, ok: bool | None) -> None:
        colour = {True: "#1a7f37", False: "#d1242f", None: "#57606a"}[ok]
        self.status_label.setStyleSheet(f"color: {colour};")
        self.status_label.setText(text)

    def _set_busy(self, busy: bool, *, action: LoginAction | None = None) -> None:
        """Disable every control and say what is happening.

        A second click must not be able to start a parallel check, and the
        button the user pressed has to show that it is working on something.
        """

        for widget in (
            self.detect_button,
            self.save_button,
            self.reopen_button,
            self.paste_edit,
        ):
            widget.setEnabled(not busy)
        self.detect_button.setText(
            DETECT_BUSY_TEXT if busy and action is LoginAction.ACQUIRE else DETECT_BUTTON_TEXT
        )
        self.save_button.setText(
            PASTE_BUSY_TEXT if busy and action is LoginAction.PASTE else PASTE_BUTTON_TEXT
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        wait_for_thread(self.worker, 5000)
        event.accept()


# --- themes -----------------------------------------------------------------

_LIGHT_QSS = """
QWidget { background: #f4f6f8; color: #1f2328; font-size: 13px; }
QGroupBox { border: 1px solid #d8dce2; border-radius: 8px; margin-top: 14px;
            padding: 12px 10px 10px 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: #57606a; }
QLineEdit, QComboBox { background: #ffffff; border: 1px solid #d0d7de;
                       border-radius: 6px; padding: 6px; }
QLineEdit:disabled, QComboBox:disabled { background: #eceff2; color: #8c959f; }
QPushButton { background: #ffffff; border: 1px solid #d0d7de; border-radius: 6px;
              padding: 7px 14px; }
QPushButton:hover { background: #eef1f4; }
QPushButton:disabled { color: #9aa4af; }
QPushButton#primary { background: #1f6feb; border-color: #1f6feb; color: #ffffff;
                      font-weight: 600; }
QPushButton#primary:hover { background: #1a60d0; }
QPushButton#primary:disabled { background: #a9c3ef; border-color: #a9c3ef; color: #ffffff; }
QProgressBar { border: 1px solid #d0d7de; border-radius: 6px; height: 20px;
               text-align: center; background: #ffffff; }
QProgressBar::chunk { background: #1f6feb; border-radius: 5px; }
QTableWidget { background: #ffffff; border: 1px solid #d8dce2; border-radius: 6px;
               gridline-color: #eceff2; }
QHeaderView::section { background: #f0f2f5; border: none; padding: 5px; }
"""

_DARK_QSS = """
QWidget { background: #1c1f24; color: #e6edf3; font-size: 13px; }
QGroupBox { border: 1px solid #30363d; border-radius: 8px; margin-top: 14px;
            padding: 12px 10px 10px 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: #9aa4af; }
QLineEdit, QComboBox { background: #22262c; border: 1px solid #3d444d;
                       border-radius: 6px; padding: 6px; color: #e6edf3; }
QLineEdit:disabled, QComboBox:disabled { background: #262a30; color: #6e7681; }
QPushButton { background: #262a30; border: 1px solid #3d444d; border-radius: 6px;
              padding: 7px 14px; color: #e6edf3; }
QPushButton:hover { background: #30363d; }
QPushButton:disabled { color: #6e7681; }
QPushButton#primary { background: #2f81f7; border-color: #2f81f7; color: #ffffff;
                      font-weight: 600; }
QPushButton#primary:hover { background: #1f6feb; }
QPushButton#primary:disabled { background: #21456f; border-color: #21456f; color: #8c959f; }
QProgressBar { border: 1px solid #3d444d; border-radius: 6px; height: 20px;
               text-align: center; background: #22262c; color: #e6edf3; }
QProgressBar::chunk { background: #2f81f7; border-radius: 5px; }
QTableWidget { background: #22262c; border: 1px solid #30363d; border-radius: 6px;
               gridline-color: #30363d; color: #e6edf3; }
QHeaderView::section { background: #262a30; border: none; padding: 5px; color: #9aa4af; }
"""


class MainWindow(QMainWindow):
    """The single application window."""

    def __init__(
        self,
        settings: Settings,
        *,
        registry: PlatformRegistry | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.registry = registry
        self.worker: DownloadWorker | None = None
        self.current_platform: Platform | None = None
        self.dark_mode = False

        #: Sign-in state, one entry per platform that supports it.
        self.login_adapters: dict[Platform, PlatformAdapter] = {}
        self.login_rows: dict[Platform, tuple[QLabel, QPushButton]] = {}
        self.login_status: dict[Platform, SessionStatus | None] = {}
        self.login_workers: dict[Platform, LoginWorker] = {}
        #: The open sign-in dialog, if any, so closing the window can wait for
        #: a check running inside it before Qt tears the thread down.
        self._login_dialog: LoginDialog | None = None
        self._login_checked = False

        self.setWindowTitle("Video Downloader")
        # Fits a 1080p screen; the content scrolls if it needs more room.
        self.resize(880, 940)
        self._build_ui()
        self._apply_theme()
        self._set_idle_state()
        self._load_history()

    # -- construction --------------------------------------------------------
    def _build_ui(self) -> None:
        # Everything lives inside a scroll area: with the sign-in panel added
        # the natural height exceeds a 1080p screen, and a window that cannot
        # fit is worse than one that scrolls.
        outer = QWidget(self)
        self.setCentralWidget(outer)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        outer_layout.addWidget(self.scroll_area)

        central = QWidget()
        self.scroll_area.setWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Video Downloader")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)
        self.theme_button = QPushButton("🌙 暗色")
        self.theme_button.clicked.connect(self._toggle_theme)
        header.addWidget(self.theme_button)
        root.addLayout(header)

        source = QGroupBox("视频来源")
        source_layout = QVBoxLayout(source)
        source_layout.addWidget(QLabel("视频链接"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("粘贴 YouTube / Instagram / B站链接")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.textChanged.connect(self._on_url_changed)
        source_layout.addWidget(self.url_edit)

        self.platform_label = QLabel(PLATFORM_IDLE_TEXT)
        self.platform_label.setStyleSheet("font-weight: 600;")
        source_layout.addWidget(self.platform_label)
        root.addWidget(source)

        options = QGroupBox("下载选项")
        form = QFormLayout(options)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.quality_combo = QComboBox()
        for value in QUALITY_CHOICES:
            self.quality_combo.addItem(QUALITY_LABELS.get(value, value), value)
        form.addRow("画质", self.quality_combo)

        dir_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.settings.resolve_path(self.settings.output_dir)))
        dir_row.addWidget(self.output_edit, 1)
        self.choose_dir_button = QPushButton("选择…")
        self.choose_dir_button.clicked.connect(self._choose_output_dir)
        dir_row.addWidget(self.choose_dir_button)
        form.addRow("保存位置", dir_row)

        self.rights_check = QCheckBox("我确认拥有下载该内容的权利")
        form.addRow("", self.rights_check)
        root.addWidget(options)

        proxy_box = QGroupBox("网络代理")
        proxy_grid = QGridLayout(proxy_box)
        proxy_grid.addWidget(QLabel("走代理"), 0, 0)
        self.proxy_platform_checks: dict[str, QCheckBox] = {}
        for column, (platform_id, label) in enumerate(self._proxy_platform_labels(), start=1):
            check = QCheckBox(label)
            self.proxy_platform_checks[platform_id] = check
            proxy_grid.addWidget(check, 0, column)

        self.proxy_enable_check = QCheckBox("启用代理")
        self.proxy_enable_check.toggled.connect(self._on_proxy_enabled_toggled)
        proxy_grid.addWidget(self.proxy_enable_check, 1, 0)
        proxy_grid.addWidget(QLabel("地址"), 1, 1)
        self.proxy_host_edit = QLineEdit()
        self.proxy_host_edit.setMaximumWidth(150)
        proxy_grid.addWidget(self.proxy_host_edit, 1, 2)
        proxy_grid.addWidget(QLabel("端口"), 1, 3)
        self.proxy_port_edit = QLineEdit()
        self.proxy_port_edit.setMaximumWidth(90)
        self.proxy_port_edit.setValidator(QIntValidator(1, 65535, self))
        proxy_grid.addWidget(self.proxy_port_edit, 1, 4)

        proxy_actions = QHBoxLayout()
        self.proxy_test_button = QPushButton("测试代理")
        self.proxy_test_button.clicked.connect(self.test_proxy)
        self.proxy_save_button = QPushButton("保存")
        self.proxy_save_button.clicked.connect(self.save_proxy)
        proxy_actions.addWidget(self.proxy_test_button)
        proxy_actions.addWidget(self.proxy_save_button)
        proxy_actions.addStretch(1)
        proxy_grid.addLayout(proxy_actions, 2, 0, 1, 3)
        self.proxy_status_label = QLabel("")
        self.proxy_status_label.setWordWrap(True)
        self.proxy_status_label.setStyleSheet("color: #57606a;")
        proxy_grid.addWidget(self.proxy_status_label, 2, 3, 1, 2)
        self.proxy_message_label = QLabel("")
        self.proxy_message_label.setWordWrap(True)
        proxy_grid.addWidget(self.proxy_message_label, 3, 0, 1, 5)
        root.addWidget(proxy_box)
        self._proxy_test_worker: ProxyTestWorker | None = None
        self._load_proxy_into_form()

        root.addWidget(self._build_login_box())

        actions = QHBoxLayout()
        self.start_button = QPushButton("开始下载")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_download)
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.clicked.connect(self.cancel_download)
        actions.addStretch(1)
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        root.addLayout(actions)

        progress_box = QGroupBox("下载进度")
        progress_layout = QVBoxLayout(progress_box)
        self.file_label = QLabel("当前文件：-")
        self.file_label.setWordWrap(True)
        progress_layout.addWidget(self.file_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        progress_layout.addWidget(self.progress_bar)

        metrics = QGridLayout()
        self.status_label = QLabel("状态：空闲")
        self.speed_label = QLabel("速度：-")
        self.size_label = QLabel("已下载：0 B")
        self.eta_label = QLabel("剩余：-")
        metrics.addWidget(self.status_label, 0, 0)
        metrics.addWidget(self.speed_label, 0, 1)
        metrics.addWidget(self.size_label, 1, 0)
        metrics.addWidget(self.eta_label, 1, 1)
        progress_layout.addLayout(metrics)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #d1242f;")
        self.error_label.hide()
        progress_layout.addWidget(self.error_label)

        self.done_label = QLabel("")
        self.done_label.setWordWrap(True)
        self.done_label.setStyleSheet("color: #1a7f37;")
        self.done_label.hide()
        progress_layout.addWidget(self.done_label)
        root.addWidget(progress_box)

        history_box = QGroupBox("下载历史")
        history_layout = QVBoxLayout(history_box)
        self.history_table = QTableWidget(0, len(HISTORY_COLUMNS))
        self.history_table.setHorizontalHeaderLabels(list(HISTORY_COLUMNS))
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header_view = self.history_table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header_view.setStretchLastSection(True)
        history_layout.addWidget(self.history_table)
        self.refresh_button = QPushButton("刷新历史")
        self.refresh_button.clicked.connect(self._load_history)
        history_layout.addWidget(self.refresh_button)
        root.addWidget(history_box, 1)

        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: #57606a;")
        root.addWidget(self.summary_label)

    # -- sign-in -------------------------------------------------------------
    def _build_login_box(self) -> QWidget:
        """The 账号登录 group: one status row per sign-in-capable platform."""

        box = QGroupBox("账号登录")
        layout = QVBoxLayout(box)

        note = QLabel(
            "使用系统默认浏览器完成官方网页登录。本程序不接收密码、验证码或两步验证信息，"
            "也不会修改浏览器里的任何 Cookie。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #57606a;")
        layout.addWidget(note)

        adapters = self._login_platforms()
        for adapter in adapters:
            row = QHBoxLayout()
            name = QLabel(adapter.display_name)
            name.setMinimumWidth(90)
            row.addWidget(name)

            state = QLabel("检测中…")
            state.setWordWrap(True)
            row.addWidget(state, 1)

            button = QPushButton("登录")
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, target=adapter: self._on_login_button(target)
            )
            row.addWidget(button)

            layout.addLayout(row)
            self.login_adapters[adapter.platform] = adapter
            self.login_rows[adapter.platform] = (state, button)
            self.login_status[adapter.platform] = None

        self.login_message = QLabel("")
        self.login_message.setWordWrap(True)
        self.login_message.setStyleSheet("color: #57606a;")
        layout.addWidget(self.login_message)

        self.login_refresh_button = QPushButton("刷新登录状态")
        self.login_refresh_button.clicked.connect(self.refresh_login_status)
        refresh_row = QHBoxLayout()
        refresh_row.addWidget(self.login_refresh_button)
        refresh_row.addStretch(1)
        layout.addLayout(refresh_row)

        if not adapters:
            box.hide()
        return box

    def _login_platforms(self) -> list[PlatformAdapter]:
        """Adapters that support signing in, in registry order."""

        if self.registry is None:
            return []
        return [adapter for adapter in self.registry.adapters if adapter.login_supported]

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        """Check the stored sessions once, the first time the window appears.

        Deliberately hooked to ``showEvent`` rather than ``__init__``: the check
        makes network calls, and the tests build windows without ever showing
        them.
        """

        super().showEvent(event)
        if not self._login_checked:
            self._login_checked = True
            QTimer.singleShot(0, self.refresh_login_status)

    def refresh_login_status(self) -> None:
        """Re-check every platform's session in the background."""

        adapters = self._login_platforms()
        if not adapters:
            return
        self._set_login_message("正在检测登录状态…", ok=None)
        for adapter in adapters:
            self._start_login_worker(adapter, LoginAction.STATUS)
            self._refresh_login_row(adapter.platform)
        self._refresh_login_controls()

    def _start_login_worker(
        self,
        adapter: PlatformAdapter,
        action: LoginAction,
        text: str = "",
    ) -> None:
        existing = self.login_workers.get(adapter.platform)
        if existing is not None and existing.isRunning():
            return
        worker = LoginWorker(self.settings, adapter, action, text, self)
        worker.status_ready.connect(
            lambda status, target=adapter: self._on_login_status(target, status)
        )
        worker.browser_unavailable.connect(
            lambda error, target=adapter: self._on_login_browser_unavailable(target, error)
        )
        worker.failed.connect(
            lambda message, detail, target=adapter: self._on_login_failed(target, message, detail)
        )
        worker.finished.connect(
            lambda target=adapter, finished=worker: self._on_login_worker_finished(target, finished)
        )
        self.login_workers[adapter.platform] = worker
        worker.start()
        self._refresh_login_row(adapter.platform)
        self._refresh_login_controls()

    def _on_login_button(self, adapter: PlatformAdapter) -> None:
        """The single button beside a platform: sign in, or sign out."""

        status = self.login_status.get(adapter.platform)
        if status is not None and status.logged_in:
            self._logout(adapter)
            return
        dialog = LoginDialog(self.settings, adapter, self)
        # Held so ``closeEvent`` can wait for a check that is still running
        # inside the dialog: a QThread destroyed while running is a crash.
        self._login_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._login_dialog = None
        if dialog.result_status is not None:
            self.login_status[adapter.platform] = dialog.result_status
            self._refresh_login_row(adapter.platform)
            self._set_login_message(
                f"{adapter.display_name}：{dialog.result_status.summary()}", ok=True
            )

    def _logout(self, adapter: PlatformAdapter) -> None:
        """Delete the session this application stored, and nothing else."""

        answer = QMessageBox.question(
            self,
            "退出登录",
            f"确定要退出 {adapter.display_name} 吗？\n\n"
            "只会删除本程序保存的该平台会话；浏览器里的登录状态完全不受影响。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return

        removed = delete_session(self.settings, adapter.platform)
        self.login_status[adapter.platform] = SessionStatus(
            platform=adapter.platform, state=LoginState.LOGGED_OUT
        )
        self._refresh_login_row(adapter.platform)
        if removed:
            self._set_login_message(
                f"已退出 {adapter.display_name}（仅删除了本程序保存的会话，浏览器不受影响）",
                ok=True,
            )
        else:
            self._set_login_message(f"本程序没有保存 {adapter.display_name} 的会话", ok=None)
        self.refresh_login_status()

    # -- sign-in worker callbacks (always run on the GUI thread) -------------
    def _on_login_status(self, adapter: PlatformAdapter, status: SessionStatus) -> None:
        self.login_status[adapter.platform] = status
        self._refresh_login_row(adapter.platform)
        if self._any_login_worker_running():
            return
        self._set_login_message(self._login_summary(), ok=None)

    def _on_login_browser_unavailable(
        self, adapter: PlatformAdapter, error: BrowserCookieError
    ) -> None:
        logger.info("自动获取 %s 会话失败：%s", adapter.platform.value, error.reason)
        self._set_login_message(f"{adapter.display_name}：{error.message}", ok=False)

    def _on_login_failed(self, adapter: PlatformAdapter, message: str, detail: str) -> None:
        logger.info("检查 %s 登录状态失败：%s", adapter.platform.value, message)
        self._set_login_message(f"{adapter.display_name}：{message}", ok=False)

    def _on_login_worker_finished(self, adapter: PlatformAdapter, worker: LoginWorker) -> None:
        # Only drop the entry when it still belongs to this worker. A newer check
        # can replace it while this one's ``finished`` is queued, and popping
        # unconditionally would leave the live worker untracked - so nothing
        # would wait for it on close.
        if self.login_workers.get(adapter.platform) is worker:
            self.login_workers.pop(adapter.platform, None)
        self._refresh_login_row(adapter.platform)
        self._refresh_login_controls()

    # -- sign-in rendering ---------------------------------------------------
    def _login_worker_running(self, platform: Platform) -> bool:
        worker = self.login_workers.get(platform)
        return worker is not None and worker.isRunning()

    def _any_login_worker_running(self) -> bool:
        return any(
            worker.isRunning() for worker in self.login_workers.values() if worker is not None
        )

    def _refresh_login_row(self, platform: Platform) -> None:
        row = self.login_rows.get(platform)
        if row is None:
            return
        state_label, button = row
        running = self._login_worker_running(platform)
        status = self.login_status.get(platform)
        if running:
            # Say that work is in progress rather than leaving the previous
            # verdict on screen as if nothing were happening.
            state_label.setText("检测中…" if status is None else status.summary())
            button.setText(LOGIN_BUSY_TEXT)
        elif status is None:
            state_label.setText("检测中…")
            button.setText("登录")
        else:
            state_label.setText(status.summary() + self._login_source_hint(platform))
            button.setText(status.action_label)
        button.setEnabled(not running)

    def _refresh_login_controls(self) -> None:
        """Keep the refresh button in step with the running checks."""

        self.login_refresh_button.setEnabled(not self._any_login_worker_running())

    def _login_source_hint(self, platform: Platform) -> str:
        """Say so when the session comes from ``.env`` rather than from here."""

        status = self.login_status.get(platform)
        if status is None or not status.logged_in:
            return ""
        if has_session(self.settings, platform):
            return ""
        return "（登录态来自 .env 配置，本程序未保存会话）"

    def _login_summary(self) -> str:
        """One line covering every platform, including why a check was unsure."""

        parts = []
        for platform, status in self.login_status.items():
            adapter = self.login_adapters.get(platform)
            if adapter is None:
                continue
            if status is None:
                parts.append(f"{adapter.display_name}：检测中…")
                continue
            text = f"{adapter.display_name}：{status.summary()}"
            if status.detail and not status.logged_in:
                text += f"（{status.detail}）"
            parts.append(text)
        return " ｜ ".join(parts)

    def _set_login_message(self, text: str, *, ok: bool | None) -> None:
        colour = {True: "#1a7f37", False: "#d1242f", None: "#57606a"}[ok]
        self.login_message.setStyleSheet(f"color: {colour};")
        self.login_message.setText(text)

    # -- theme ---------------------------------------------------------------
    def _apply_theme(self) -> None:
        self.setStyleSheet(_DARK_QSS if self.dark_mode else _LIGHT_QSS)
        self.theme_button.setText("☀️ 明亮" if self.dark_mode else "🌙 暗色")

    def _toggle_theme(self) -> None:
        self.dark_mode = not self.dark_mode
        self._apply_theme()

    # -- input ---------------------------------------------------------------
    def _on_url_changed(self, text: str) -> None:
        platform, label = detect_platform(text, self.registry)
        self.current_platform = platform
        self.platform_label.setText(f"平台：{label}")
        self._clear_messages()

    def _choose_output_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择保存目录", self.output_edit.text() or str(Path.home())
        )
        if chosen:
            self.output_edit.setText(chosen)

    # -- network proxy -------------------------------------------------------
    def _proxy_platform_labels(self) -> list[tuple[str, str]]:
        """(platform id, display name) for the proxy switches."""

        if self.registry is not None:
            return [
                (adapter.platform.value, adapter.platform.display_name)
                for adapter in self.registry.adapters
            ]
        return [(name, name) for name in SUPPORTED_PLATFORM_IDS]

    def _platform_label(self, platform_id: str) -> str:
        for name, label in self._proxy_platform_labels():
            if name == platform_id:
                return label
        return platform_id

    def _load_proxy_into_form(self) -> None:
        """Show the proxy configuration currently loaded from ``.env``."""

        config = ProxyConfig.from_settings(self.settings)
        self.proxy_enable_check.setChecked(config.enabled)
        self.proxy_host_edit.setText(config.host)
        self.proxy_port_edit.setText(str(config.port))
        proxied = set(config.proxied_platforms)
        for platform_id, check in self.proxy_platform_checks.items():
            check.setChecked(platform_id in proxied)
        self._on_proxy_enabled_toggled(config.enabled)
        self._update_proxy_status(config)

    def _on_proxy_enabled_toggled(self, enabled: bool) -> None:
        self.proxy_host_edit.setEnabled(enabled)
        self.proxy_port_edit.setEnabled(enabled)
        self.proxy_test_button.setEnabled(enabled)
        for check in self.proxy_platform_checks.values():
            check.setEnabled(enabled)

    def _proxy_config_from_form(self) -> ProxyConfig | None:
        """Build a :class:`ProxyConfig` from the widgets, or None when invalid."""

        enabled = self.proxy_enable_check.isChecked()
        host = self.proxy_host_edit.text().strip()
        raw_port = self.proxy_port_edit.text().strip()
        port = int(raw_port) if raw_port.isdigit() else 0
        if enabled and not host:
            self._set_proxy_message("请填写代理地址", ok=False)
            return None
        if enabled and not 0 < port <= 65535:
            self._set_proxy_message("端口必须是 1-65535 之间的数字", ok=False)
            return None
        proxied = tuple(
            platform_id
            for platform_id, check in self.proxy_platform_checks.items()
            if check.isChecked()
        )
        return ProxyConfig(
            enabled=enabled,
            host=host or DEFAULT_PROXY_HOST,
            port=port or DEFAULT_PROXY_PORT,
            proxied_platforms=proxied,
        )

    def _set_proxy_message(self, text: str, *, ok: bool | None) -> None:
        colour = {True: "#1a7f37", False: "#d1242f", None: "#57606a"}[ok]
        self.proxy_message_label.setStyleSheet(f"color: {colour};")
        self.proxy_message_label.setText(text)

    def _update_proxy_status(self, config: ProxyConfig) -> None:
        """Show the proxy state that is currently in effect."""

        if not config.enabled or not config.url:
            self.proxy_status_label.setText("当前：未启用代理（所有平台直连）")
            return
        parts = [f"当前：{config.url}"]
        proxied = [self._platform_label(name) for name in config.proxied_platforms]
        bypass = [self._platform_label(name) for name in config.bypass_platforms]
        if proxied:
            parts.append(f"走代理：{', '.join(proxied)}")
        if bypass:
            parts.append(f"直连：{', '.join(bypass)}")
        self.proxy_status_label.setText(" ｜ ".join(parts))

    def save_proxy(self) -> bool:
        """Persist the form into .env and apply it to this running process."""

        config = self._proxy_config_from_form()
        if config is None:
            return False
        try:
            path = save_proxy_configuration(config)
        except OSError as exc:
            logger.exception("保存代理配置失败")
            self._set_proxy_message(f"保存失败：{exc}", ok=False)
            return False

        apply_proxy_configuration(self.settings, config)
        logger.info("代理配置已保存：%s", config.url or "未启用")
        self._update_proxy_status(config)
        self._set_proxy_message(f"已保存到 {path}", ok=True)
        return True

    def test_proxy(self) -> None:
        """Check that the entered proxy is reachable and can relay a request."""

        config = self._proxy_config_from_form()
        if config is None:
            return
        url = config.url
        if url is None:
            self._set_proxy_message("请先勾选「启用代理」", ok=False)
            return
        self.proxy_test_button.setEnabled(False)
        self._set_proxy_message(f"正在测试 {url} …", ok=None)
        worker = ProxyTestWorker(url, self)
        worker.tested.connect(self._on_proxy_tested)
        worker.finished.connect(worker.deleteLater)
        self._proxy_test_worker = worker
        worker.start()

    def _on_proxy_tested(self, ok: bool, message: str) -> None:
        self.proxy_test_button.setEnabled(self.proxy_enable_check.isChecked())
        self._set_proxy_message(message, ok=ok)
        logger.info("代理测试结果：%s", message)

    # -- state helpers -------------------------------------------------------
    def _set_idle_state(self) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.url_edit.setEnabled(True)
        self.quality_combo.setEnabled(True)
        self.output_edit.setEnabled(True)
        self.choose_dir_button.setEnabled(True)

    def _set_running_state(self) -> None:
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.url_edit.setEnabled(False)
        self.quality_combo.setEnabled(False)
        self.output_edit.setEnabled(False)
        self.choose_dir_button.setEnabled(False)

    def _clear_messages(self) -> None:
        self.error_label.hide()
        self.error_label.setText("")
        self.done_label.hide()
        self.done_label.setText("")

    def _show_error(self, message: str) -> None:
        self.error_label.setText(f"⚠ {message}")
        self.error_label.show()
        self.done_label.hide()

    def _show_done(self, message: str) -> None:
        self.done_label.setText(f"✔ {message}")
        self.done_label.show()
        self.error_label.hide()

    # -- download control ----------------------------------------------------
    def start_download(self) -> None:
        """Validate the form and hand the job to a worker thread."""

        self._clear_messages()
        url = self.url_edit.text().strip()
        if not url:
            self._show_error(EMPTY_URL_TEXT)
            return

        platform, label = detect_platform(url, self.registry)
        self.current_platform = platform
        self.platform_label.setText(f"平台：{label}")
        if platform is None:
            self._show_error(UNSUPPORTED_PLATFORM_TEXT)
            self.status_label.setText("状态：无法识别平台")
            return

        adapter = self.registry.get(platform) if self.registry is not None else None
        if (
            adapter is not None
            and adapter.requires_rights_confirmation
            and not self.rights_check.isChecked()
        ):
            self._show_error(RIGHTS_UNCHECKED_TEXT)
            return

        quality = self.quality_combo.currentData() or "best"
        raw_dir = self.output_edit.text().strip()
        output_dir = Path(raw_dir) if raw_dir else None

        worker = DownloadWorker(self.settings, url, quality, output_dir, self)
        worker.progressed.connect(self._on_progress)
        worker.succeeded.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(self._on_worker_finished)
        self.worker = worker

        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        self.file_label.setText("当前文件：正在获取视频信息…")
        self.status_label.setText("状态：正在准备")
        self.speed_label.setText("速度：-")
        self.size_label.setText("已下载：0 B")
        self.eta_label.setText("剩余：-")
        self._set_running_state()
        worker.start()

    def cancel_download(self) -> None:
        if self.worker is None:
            return
        self.cancel_button.setEnabled(False)
        self.status_label.setText("状态：正在取消…")
        self.worker.cancel()

    # -- worker callbacks (always run on the GUI thread) ---------------------
    def _on_progress(self, update: ProgressUpdate) -> None:
        kind = "视频" if update.stream_kind == "video" else "音频"
        self.status_label.setText(f"状态：正在下载（{kind}）")
        self.speed_label.setText(f"速度：{format_speed(update.speed_bps)}")

        if update.total:
            fraction = update.fraction or 0.0
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(fraction * 1000))
            self.progress_bar.setFormat(f"{fraction * 100:.0f}%")
            self.size_label.setText(
                f"已下载：{format_size(update.downloaded)} / {format_size(update.total)}"
            )
            remaining = (
                (update.total - update.downloaded) / update.speed_bps
                if update.speed_bps > 0
                else None
            )
            self.eta_label.setText(f"剩余：{format_eta(remaining)}")
        else:
            # Size unknown: show activity instead of inventing a percentage.
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("正在下载…")
            self.size_label.setText(f"已下载：{format_size(update.downloaded)}")
            self.eta_label.setText("剩余：-")

        if update.stream_kind == "video":
            self.file_label.setText("当前文件：视频流")
        else:
            self.file_label.setText("当前文件：音频流")

    def _on_success(self, result: DownloadResult) -> None:
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100%")
        if result.status is DownloadStatus.SKIPPED:
            self.status_label.setText("状态：已跳过（数据库中已有记录）")
            self._show_done(f"已跳过：{result.video_path}")
        else:
            self.status_label.setText("状态：下载完成")
            name = result.video_path.name if result.video_path else "-"
            self.file_label.setText(f"当前文件：{name}")
            actual = describe_quality(result.resolution, result.quality_label)
            self._show_done(f"下载完成：{result.video_path}（实际画质 {actual}）")
        self._load_history()

    def _on_failure(self, message: str) -> None:
        self.status_label.setText("状态：下载失败")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("失败")
        self._show_error(message)
        self._load_history()

    def _on_cancelled(self) -> None:
        self.status_label.setText("状态：已取消")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setFormat("已取消")
        self._show_done("下载已取消（已下载的分片会保留，下次可继续）")
        self._load_history()

    def _on_worker_finished(self) -> None:
        self._set_idle_state()
        self.worker = None

    # -- history -------------------------------------------------------------
    def _load_history(self) -> None:
        database = DownloadDatabase(self.settings)
        try:
            records = database.recent(30)
            stats = database.stats()
        except VideoDownloaderError as exc:
            logger.warning("读取下载历史失败：%s", exc)
            self.summary_label.setText("下载历史读取失败，详情见日志")
            return
        finally:
            database.close()

        self.history_table.setRowCount(len(records))
        for row, record in enumerate(records):
            cells = (
                record.platform.display_name,
                record.title,
                record.author or "-",
                describe_quality(record.resolution, record.quality_label),
                format_timestamp(record.downloaded_at or record.updated_at),
                STATUS_LABELS.get(record.status.value, record.status.value),
                record.file_path or "-",
            )
            for column, value in enumerate(cells):
                self.history_table.setItem(row, column, QTableWidgetItem(str(value)))
        summary = ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "无记录"
        self.summary_label.setText(f"共 {len(records)} 条记录 | 状态统计：{summary}")

    # -- lifecycle -----------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.worker is not None:
            self.worker.cancel()
            wait_for_thread(self.worker)
        wait_for_thread(self._proxy_test_worker)
        for login_worker in list(self.login_workers.values()):
            wait_for_thread(login_worker)
        # The dialog is a child widget: destroying it would destroy its worker.
        if self._login_dialog is not None:
            wait_for_thread(self._login_dialog.worker, 5000)
        event.accept()


# --- application entry point -------------------------------------------------

SELF_TEST_FLAG = "--self-test"


def emit(text: str) -> None:
    """Write to stdout, falling back to the log file in a windowed build.

    ``console=False`` builds run with ``sys.stdout is None``, where ``print``
    raises ``AttributeError``. The log file is always available, so the message
    is never lost.
    """

    stream = sys.stdout
    if stream is None:
        logger.info("%s", text)
        return
    try:
        print(text)
    except (OSError, ValueError):  # pragma: no cover - depends on the console
        logger.info("%s", text)


async def _close_clients(client: httpx.AsyncClient, registry: PlatformRegistry) -> None:
    for extra in registry.owned_clients:
        await extra.aclose()
    await client.aclose()


def parse_self_test(argv: list[str] | None) -> tuple[str, str, bool] | None:
    """Parse ``--self-test <URL> [--quality Q] [--confirm-rights]``.

    A packaged build cannot be driven by clicking buttons from a script, so
    this flag runs exactly one real download through the same
    :class:`DownloadWorker` the start button uses, then exits. It is the
    supported way to verify a frozen build end to end.
    """

    args = list(argv if argv is not None else sys.argv)
    if args and not args[0].startswith("-"):
        args = args[1:]  # drop the program name
    if SELF_TEST_FLAG not in args:
        return None
    index = args.index(SELF_TEST_FLAG)
    if index + 1 >= len(args) or args[index + 1].startswith("-"):
        raise ValueError(
            "用法: VideoDownloader.exe --self-test <URL> "
            "[--quality best|1080p|...] [--confirm-rights]"
        )
    url = args[index + 1]
    quality = args[args.index("--quality") + 1] if "--quality" in args else "best"
    return url, quality, "--confirm-rights" in args


def run_self_test(
    app: QApplication,
    settings: Settings,
    url: str,
    quality: str,
    confirmed: bool,
    *,
    timeout_ms: int = 1_800_000,
) -> int:
    """Run one real download through the GUI worker and report the outcome."""

    client = build_client(settings)
    registry = build_registry(settings, client)
    try:
        platform, label = detect_platform(url, registry)
        if platform is None:
            emit(f"[self-test] 平台识别失败: {label}")
            return 2
        adapter = registry.get(platform)
        if adapter.requires_rights_confirmation and not confirmed:
            emit(f"[self-test] {label} 需要 --confirm-rights")
            return 2
    finally:
        asyncio.run(_close_clients(client, registry))

    emit(f"[self-test] 平台={label} 画质={quality}")
    emit(f"[self-test] 数据目录={DATA_ROOT}")
    emit(f"[self-test] URL={url}")

    outcome: dict[str, object] = {}
    worker = DownloadWorker(settings, url, quality, None)
    worker.succeeded.connect(lambda result: outcome.update(result=result))
    worker.failed.connect(lambda message: outcome.update(error=message))
    worker.cancelled.connect(lambda: outcome.update(error="已取消"))
    worker.finished.connect(app.quit)
    worker.start()
    QTimer.singleShot(timeout_ms, app.quit)
    app.exec()
    worker.wait(5000)

    result = outcome.get("result")
    if not isinstance(result, DownloadResult):
        emit(f"[self-test] 失败: {outcome.get('error', '未收到结果')}")
        return 1
    emit(f"[self-test] 状态={result.status.value}")
    emit(f"[self-test] 分辨率={result.resolution} 画质={result.quality_label}")
    emit(f"[self-test] 文件={result.video_path}")
    emit(f"[self-test] 元数据={result.metadata_path}")
    return 0


def crash_log_path() -> Path:
    """Where a startup failure is written when no console is attached."""

    try:
        settings = get_settings()
        base = settings.resolve_path(settings.log_dir)
    except Exception:  # noqa: BLE001 - settings themselves may be the failure
        base = DATA_ROOT / "logs"
    return base / "startup-error.log"


def report_fatal(error: BaseException) -> Path | None:
    """Append a full traceback to ``logs/startup-error.log``; never raises."""

    path = crash_log_path()
    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {datetime.now(UTC).isoformat()} =====\n{text}\n")
        return path
    except OSError:
        return None


def _show_fatal_message(error: BaseException, log_path: Path | None) -> None:
    """Best-effort dialog so a windowed build is never silently dead."""

    try:
        if QApplication.instance() is None:
            return
        detail = f"\n\n详细日志：{log_path}" if log_path else ""
        QMessageBox.critical(
            None,
            "Video Downloader 启动失败",
            f"程序启动时发生错误：\n{type(error).__name__}: {error}{detail}",
        )
    except Exception:  # noqa: BLE001 - dialog is best effort only
        logger.debug("无法显示启动失败对话框", exc_info=True)


def _install_excepthook() -> None:
    """Route otherwise-unhandled exceptions to the crash log."""

    def _hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: types.TracebackType | None,
    ) -> None:
        path = report_fatal(exc_value)
        logger.error("未捕获异常，已写入 %s", path)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def _run_app(argv: list[str] | None) -> int:
    try:
        self_test = parse_self_test(argv)
    except ValueError as error:
        emit(str(error))
        return 2

    settings = get_settings()
    setup_logging(settings)
    _install_excepthook()

    # Strip our own flags before Qt sees them.
    qt_argv = [sys.argv[0]] if self_test else (argv if argv is not None else sys.argv)
    app = QApplication(qt_argv if isinstance(qt_argv, list) else sys.argv)
    app.setApplicationName("Video Downloader")

    if self_test is not None:
        url, quality, confirmed = self_test
        return run_self_test(app, settings, url, quality, confirmed)

    logger.info(
        "GUI 启动：数据目录 %s | ffmpeg %s | 代理 %s",
        DATA_ROOT,
        "已就绪" if locate_ffmpeg(settings) else "缺失",
        settings.proxy or "直连",
    )

    # One registry for live URL recognition; each download job builds its own
    # client, exactly like the CLI does.
    client = build_client(settings)
    registry = build_registry(settings, client)
    window = MainWindow(settings, registry=registry)
    window.show()
    try:
        return int(app.exec())
    finally:
        asyncio.run(_close_clients(client, registry))


def main(argv: list[str] | None = None) -> int:
    """Start the desktop application.

    Fully wrapped: with ``console=False`` there is no terminal, so anything
    raised before or during startup must land in ``logs/startup-error.log``.
    """

    try:
        return _run_app(argv)
    except BaseException as error:  # noqa: BLE001 - report, then re-raise
        path = report_fatal(error)
        logger.exception("GUI 启动失败")
        _show_fatal_message(error, path)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
