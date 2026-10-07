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
from datetime import UTC, datetime
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIntValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
from core.database import DownloadDatabase
from core.downloader import ProgressUpdate
from core.exceptions import (
    AuthRequiredError,
    CookieAccessError,
    DatabaseError,
    DownloadCancelled,
    DownloadError,
    IntegrityError,
    MergeError,
    MetadataError,
    NotDownloadableError,
    RateLimitedError,
    UnsupportedUrlError,
    VideoDownloaderError,
)
from core.http import build_client, check_proxy
from core.logging_setup import setup_logging
from core.merger import locate_ffmpeg
from core.models import DownloadResult, DownloadStatus, Platform
from core.registry import PlatformRegistry, build_registry
from core.service import DownloadService

logger = logging.getLogger(__name__)

# --- user-facing text -------------------------------------------------------

PLATFORM_IDLE_TEXT = "等待输入链接"
UNSUPPORTED_PLATFORM_TEXT = "暂不支持该平台"
RIGHTS_UNCHECKED_TEXT = "请先勾选「我确认拥有下载该内容的权利」"
EMPTY_URL_TEXT = "请先粘贴视频链接"

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
) -> str:
    """Translate an internal exception into a short, safe user message.

    Never includes the raw exception text, a URL with query tokens, or anything
    read from a cookies file; the full detail stays in ``logs/downloader.log``.
    """

    if isinstance(exc, DownloadCancelled):
        return "已取消"
    if isinstance(exc, UnsupportedUrlError):
        return UNSUPPORTED_PLATFORM_TEXT
    if isinstance(exc, CookieAccessError):
        text = str(exc)
        if "解密" in text or "App-Bound" in text:
            return "浏览器 Cookie 无法解密，请改用 secrets/cookies.txt"
        if "未找到" in text:
            return "未找到 Cookie 文件，请检查 secrets/cookies.txt"
        return "Cookie 读取失败，请重新导出登录状态"
    if isinstance(exc, AuthRequiredError):
        if platform is Platform.INSTAGRAM:
            return "Instagram 登录状态已失效，请重新更新 Cookie。"
        return "该内容需要登录后才能访问"
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
            self.failed.emit(friendly_error(exc, platform=self.platform, proxy=self.settings.proxy))
        except Exception as exc:  # noqa: BLE001 - last line of defence
            logger.exception("GUI 下载任务异常")
            self.failed.emit(friendly_error(exc, platform=self.platform, proxy=self.settings.proxy))
        else:
            self.succeeded.emit(result)

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

        self.setWindowTitle("Video Downloader")
        self.resize(820, 720)
        self._build_ui()
        self._apply_theme()
        self._set_idle_state()
        self._load_history()

    # -- construction --------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
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
            self.worker.wait(3000)
        if self._proxy_test_worker is not None:
            self._proxy_test_worker.wait(3000)
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
