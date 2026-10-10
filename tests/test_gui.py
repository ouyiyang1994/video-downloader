"""GUI layer tests.

Everything runs headless through the offscreen Qt platform (set in
``conftest.py``), so no window is ever shown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from PySide6.QtWidgets import QApplication

import gui
from config import settings as settings_module
from config.constants import QUALITY_AUDIO_ONLY, QUALITY_PRESETS
from config.settings import Settings
from core import browser_cookies, fallback_policy, session_store
from core.chrome_bridge import BridgeStatus
from core.database import DownloadDatabase
from core.downloader import ProgressUpdate
from core.exceptions import (
    AuthRequiredError,
    BrowserCookieError,
    ChromeBridgeError,
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
)
from core.login import LoginSource, LoginState, SessionOrigin, SessionStatus, parse_session_text
from core.models import DownloadRecord, DownloadResult, DownloadStatus, Platform
from core.registry import PlatformRegistry, build_registry


@pytest.fixture
def registry(settings: Settings) -> PlatformRegistry:
    client = httpx.AsyncClient()
    reg = build_registry(settings, client)
    try:
        yield reg
    finally:
        asyncio.run(client.aclose())


@pytest.fixture
def window(qtbot, settings: Settings, registry: PlatformRegistry) -> gui.MainWindow:
    main = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(main)
    return main


# --- platform recognition ---------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected_platform", "expected_label"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE, "YouTube"),
        ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE, "YouTube"),
        ("https://www.instagram.com/reel/CrN6uBSOINl/", Platform.INSTAGRAM, "Instagram"),
        ("https://www.bilibili.com/video/BV1GJ411x7h7", Platform.BILIBILI, "哔哩哔哩"),
        ("https://b23.tv/abcdefg", Platform.BILIBILI, "哔哩哔哩"),
    ],
)
def test_detect_supported_platforms(
    registry: PlatformRegistry, url: str, expected_platform: Platform, expected_label: str
) -> None:
    platform, label = gui.detect_platform(url, registry)
    assert platform is expected_platform
    assert label == expected_label


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/video/6961737553342991651",
        "https://v.douyin.com/iRNBho6/",
        "https://example.com/video/1",
    ],
)
def test_detect_unsupported_platforms(registry: PlatformRegistry, url: str) -> None:
    platform, label = gui.detect_platform(url, registry)
    assert platform is None
    assert label == gui.UNSUPPORTED_PLATFORM_TEXT
    assert label == "暂不支持该平台"


def test_detect_empty_url(registry: PlatformRegistry) -> None:
    platform, label = gui.detect_platform("   ", registry)
    assert platform is None
    assert label == gui.PLATFORM_IDLE_TEXT


# --- error translation ------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UnsupportedUrlError("nope"), "暂不支持该平台"),
        (MetadataError("gone"), "视频不存在或无法访问"),
        (NotDownloadableError("private"), "该视频不可下载"),
        (RateLimitedError("slow down"), "限流"),
        (MergeError("ffmpeg"), "音视频合并失败"),
        (IntegrityError("short"), "校验失败"),
        (DatabaseError("sqlite"), "写入下载记录失败"),
        (DownloadCancelled("stop"), "已取消"),
    ],
)
def test_friendly_error_messages(exc: BaseException, expected: str) -> None:
    message = gui.friendly_error(exc)
    assert expected in message
    assert "Traceback" not in message


def test_friendly_error_for_expired_instagram_login() -> None:
    message = gui.friendly_error(
        AuthRequiredError("login required"),
        platform=Platform.INSTAGRAM,
        session_present=True,
    )
    assert "登录已过期" in message
    assert "账号登录" in message


def test_friendly_error_for_missing_instagram_login() -> None:
    message = gui.friendly_error(
        AuthRequiredError("login required"),
        platform=Platform.INSTAGRAM,
        session_present=False,
    )
    assert "需要登录" in message
    assert "账号登录" in message


def test_friendly_error_mentions_proxy() -> None:
    wrapped = DownloadError("connect failed")
    wrapped.__cause__ = httpx.ConnectTimeout("timed out")
    message = gui.friendly_error(wrapped, proxy="http://127.0.0.1:8090")
    assert "代理连接失败" in message
    assert "127.0.0.1:8090" in message


def test_friendly_error_without_a_transport_cause_stays_generic() -> None:
    """With no evidence it was a connect failure we must not blame the proxy."""

    message = gui.friendly_error(DownloadError("download failed"), proxy="http://127.0.0.1:8090")
    assert message == "网络连接失败"


def test_friendly_error_sees_through_wrapped_transport_errors() -> None:
    """The core wraps httpx errors, so the proxy hint must follow the cause."""

    wrapped = DownloadError("下载失败")
    wrapped.__cause__ = httpx.ConnectError("proxy refused")
    message = gui.friendly_error(wrapped, proxy="http://127.0.0.1:8090")
    assert "代理连接失败" in message

    timeout = DownloadError("下载失败")
    timeout.__cause__ = httpx.ReadTimeout("slow")
    assert gui.friendly_error(timeout) == "网络超时，请稍后重试"


@pytest.mark.parametrize(
    "secret", ["sessionid=SUPERSECRETVALUE", "csrftoken=TOPSECRET", "SESSDATA=LEAKME"]
)
def test_friendly_error_never_leaks_credentials(secret: str) -> None:
    """Raw exception text must never reach the user, whatever it contains."""

    for exc in (
        DownloadError("download failed", detail=secret),
        CookieAccessError("cookie problem", detail=secret),
        MetadataError("metadata failed", detail=secret),
        RuntimeError(secret),
    ):
        message = gui.friendly_error(exc)
        assert "SECRET" not in message.upper()
        assert "LEAK" not in message.upper()


# --- quality options --------------------------------------------------------


def test_quality_choices_match_core_presets() -> None:
    assert (*QUALITY_PRESETS.keys(), QUALITY_AUDIO_ONLY) == gui.QUALITY_CHOICES
    assert len(gui.QUALITY_CHOICES) == 8
    for value in gui.QUALITY_CHOICES:
        assert value in gui.QUALITY_LABELS


def test_quality_combo_exposes_the_presets(window: gui.MainWindow) -> None:
    combo = window.quality_combo
    assert [combo.itemData(i) for i in range(combo.count())] == list(gui.QUALITY_CHOICES)
    assert combo.itemText(0) == "最佳"
    assert combo.itemData(combo.count() - 1) == "audio"


# --- window behaviour -------------------------------------------------------


def test_initial_state(window: gui.MainWindow) -> None:
    assert window.start_button.isEnabled()
    assert not window.cancel_button.isEnabled()
    assert window.progress_bar.value() == 0
    assert window.platform_label.text() == gui.PLATFORM_IDLE_TEXT
    assert window.error_label.isHidden()
    assert window.done_label.isHidden()


def test_typing_url_updates_platform_label(window: gui.MainWindow) -> None:
    window.url_edit.setText("https://youtu.be/dQw4w9WgXcQ")
    assert window.platform_label.text() == "平台：YouTube"
    window.url_edit.setText("https://www.douyin.com/video/1")
    assert window.platform_label.text() == f"平台：{gui.UNSUPPORTED_PLATFORM_TEXT}"


def test_empty_url_is_rejected(window: gui.MainWindow) -> None:
    window.url_edit.setText("")
    window.start_download()
    assert window.worker is None
    assert gui.EMPTY_URL_TEXT in window.error_label.text()


def test_unsupported_platform_blocks_start(window: gui.MainWindow) -> None:
    window.url_edit.setText("https://www.douyin.com/video/6961737553342991651")
    window.rights_check.setChecked(True)
    window.start_download()
    assert window.worker is None
    assert gui.UNSUPPORTED_PLATFORM_TEXT in window.error_label.text()
    assert "抖音" not in window.status_label.text()


def test_rights_checkbox_required_for_youtube(window: gui.MainWindow) -> None:
    window.url_edit.setText("https://youtu.be/dQw4w9WgXcQ")
    window.rights_check.setChecked(False)
    window.start_download()
    assert window.worker is None
    assert "权利" in window.error_label.text()


def test_bilibili_needs_no_rights_confirmation(window: gui.MainWindow) -> None:
    window.url_edit.setText("https://www.bilibili.com/video/BV1GJ411x7h7")
    window.rights_check.setChecked(False)
    window.start_download()
    assert window.worker is not None
    window.worker.cancel()
    window.worker.wait(5000)


def test_progress_updates_the_window(window: gui.MainWindow) -> None:
    window._on_progress(
        ProgressUpdate(
            stream_kind="video", downloaded=5_000_000, total=10_000_000, speed_bps=2_500_000
        )
    )
    assert window.progress_bar.value() == 500
    assert window.progress_bar.format() == "50%"
    assert "4.8 MB" in window.size_label.text()
    assert "MB/s" in window.speed_label.text()
    assert window.eta_label.text().startswith("剩余：00:0")
    assert "正在下载" in window.status_label.text()


def test_progress_without_total_is_not_faked(window: gui.MainWindow) -> None:
    window._on_progress(
        ProgressUpdate(stream_kind="video", downloaded=1234, total=None, speed_bps=0.0)
    )
    # Indeterminate range => a busy bar, never an invented percentage.
    assert window.progress_bar.minimum() == 0
    assert window.progress_bar.maximum() == 0
    assert window.progress_bar.format() == "正在下载…"
    assert window.eta_label.text() == "剩余：-"


def test_success_updates_state(window: gui.MainWindow) -> None:
    result = DownloadResult(
        status=DownloadStatus.COMPLETED,
        platform=Platform.BILIBILI,
        video_id="BV1",
        title="demo",
        video_path=Path("downloads/demo.mp4"),
        quality_label="480P",
    )
    window._on_success(result)
    assert "下载完成" in window.status_label.text()
    assert not window.done_label.isHidden()
    assert window.progress_bar.value() == window.progress_bar.maximum()


def test_skipped_result_is_reported(window: gui.MainWindow) -> None:
    result = DownloadResult(
        status=DownloadStatus.SKIPPED,
        platform=Platform.BILIBILI,
        video_id="BV1",
        title="demo",
        video_path=Path("downloads/demo.mp4"),
        skipped_reason="already downloaded",
    )
    window._on_success(result)
    assert "已跳过" in window.status_label.text()


def test_failure_shows_friendly_message(window: gui.MainWindow) -> None:
    window._on_failure("网络连接失败，请检查网络")
    assert "失败" in window.status_label.text()
    assert not window.error_label.isHidden()
    assert "网络连接失败" in window.error_label.text()
    assert "Traceback" not in window.error_label.text()


def test_cancel_updates_state(window: gui.MainWindow) -> None:
    window._on_cancelled()
    assert "已取消" in window.status_label.text()
    assert not window.done_label.isHidden()


def test_worker_finished_restores_buttons(window: gui.MainWindow) -> None:
    window._set_running_state()
    assert not window.start_button.isEnabled()
    window._on_worker_finished()
    assert window.start_button.isEnabled()
    assert not window.cancel_button.isEnabled()
    assert window.worker is None


def test_theme_toggle(window: gui.MainWindow) -> None:
    assert window.dark_mode is False
    window._toggle_theme()
    assert window.dark_mode is True
    assert window.styleSheet()
    window._toggle_theme()
    assert window.dark_mode is False


# --- history ----------------------------------------------------------------


def test_history_table_reads_the_existing_database(
    qtbot, settings: Settings, registry: PlatformRegistry
) -> None:
    database = DownloadDatabase(settings)
    try:
        database.upsert(
            DownloadRecord(
                platform=Platform.INSTAGRAM,
                video_id="ABC123",
                url="https://www.instagram.com/reel/ABC123/",
                title="历史上的一条视频",
                author="someone",
                file_path=str(settings.resolve_path(settings.output_dir) / "clip.mp4"),
                status=DownloadStatus.COMPLETED,
                resolution="1920x1080",
                quality_label="1080p",
                downloaded_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            )
        )
    finally:
        database.close()

    main = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(main)

    assert main.history_table.rowCount() == 1
    row = [
        main.history_table.item(0, column).text()
        for column in range(main.history_table.columnCount())
    ]
    assert row[0] == "Instagram"
    assert row[1] == "历史上的一条视频"
    assert row[2] == "someone"
    # The history shows the resolution that was actually downloaded.
    assert row[3] == "1920×1080 ｜ 1080p"
    assert row[5] == "已完成"
    assert "clip.mp4" in row[6]
    assert "completed=1" in main.summary_label.text()


def test_history_shows_cancelled_status(
    qtbot, settings: Settings, registry: PlatformRegistry
) -> None:
    database = DownloadDatabase(settings)
    try:
        database.upsert(
            DownloadRecord(
                platform=Platform.YOUTUBE,
                video_id="yt1",
                url="https://youtu.be/yt1",
                title="被取消的下载",
                status=DownloadStatus.CANCELLED,
            )
        )
    finally:
        database.close()

    main = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(main)
    assert main.history_table.item(0, 5).text() == "已取消"


# --- threading --------------------------------------------------------------


def test_download_runs_off_the_gui_thread(
    qtbot, monkeypatch, settings: Settings, window: gui.MainWindow
) -> None:
    """start() must return at once and the event loop must stay alive."""

    finished: list[DownloadResult] = []

    async def slow_download(self: gui.DownloadWorker) -> DownloadResult:
        await asyncio.sleep(0.6)
        return DownloadResult(
            status=DownloadStatus.COMPLETED,
            platform=Platform.BILIBILI,
            video_id="BV1",
            title="threaded",
        )

    monkeypatch.setattr(gui.DownloadWorker, "_download", slow_download)

    worker = gui.DownloadWorker(settings, "https://www.bilibili.com/video/BV1", "best", None)
    qtbot.addWidget(window)
    worker.succeeded.connect(finished.append)

    started = time.monotonic()
    worker.start()
    assert time.monotonic() - started < 0.3, "start() blocked the GUI thread"
    assert worker.isRunning()

    # Pump the GUI event loop while the download is in flight.
    ticks = 0
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        QApplication.processEvents()
        ticks += 1
        time.sleep(0.005)
    assert ticks > 5, "GUI event loop was starved by the worker"

    assert worker.wait(10000)
    QApplication.processEvents()
    assert finished
    assert finished[0].status is DownloadStatus.COMPLETED


def test_cancel_sets_the_event(
    qtbot, monkeypatch, settings: Settings, window: gui.MainWindow
) -> None:
    async def slow_download(self: gui.DownloadWorker) -> DownloadResult:
        await asyncio.sleep(5)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(gui.DownloadWorker, "_download", slow_download)

    window.url_edit.setText("https://www.bilibili.com/video/BV1GJ411x7h7")
    window._set_running_state()
    worker = gui.DownloadWorker(settings, window.url_edit.text(), "best", None, window)
    window.worker = worker
    worker.start()

    window.cancel_download()
    assert worker.cancel_event.is_set()
    assert not window.cancel_button.isEnabled()
    assert "取消" in window.status_label.text()

    worker.cancel()
    worker.wait(5000)


def test_worker_failure_emits_friendly_text(qtbot, monkeypatch, settings: Settings) -> None:
    messages: list[str] = []

    async def failing_download(self: gui.DownloadWorker) -> DownloadResult:
        raise MetadataError("video is gone")

    monkeypatch.setattr(gui.DownloadWorker, "_download", failing_download)

    worker = gui.DownloadWorker(settings, "https://youtu.be/dQw4w9WgXcQ", "best", None)
    worker.failed.connect(messages.append)
    worker.start()
    assert worker.wait(10000)
    QApplication.processEvents()

    assert messages == ["视频不存在或无法访问"]


# --- network proxy group ----------------------------------------------------


def test_proxy_group_shows_suggested_defaults(window: gui.MainWindow) -> None:
    """Nothing configured yet: the form suggests the local proxy endpoint."""

    assert window.proxy_enable_check.isChecked() is False
    assert window.proxy_host_edit.text() == "127.0.0.1"
    assert window.proxy_port_edit.text() == "8090"
    assert window.proxy_platform_checks["youtube"].isChecked()
    assert window.proxy_platform_checks["instagram"].isChecked()
    assert not window.proxy_platform_checks["bilibili"].isChecked()
    assert "未启用代理" in window.proxy_status_label.text()
    assert window.proxy_test_button.text() == "测试代理"
    assert window.proxy_save_button.text() == "保存"


def test_proxy_group_reads_an_existing_configuration(
    qtbot, settings: Settings, registry: PlatformRegistry
) -> None:
    settings.http_proxy = "http://10.1.2.3:7890"
    settings.https_proxy = "http://10.1.2.3:7890"
    settings.proxy_bypass_platforms = "bilibili"

    window = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(window)

    assert window.proxy_enable_check.isChecked()
    assert window.proxy_host_edit.text() == "10.1.2.3"
    assert window.proxy_port_edit.text() == "7890"
    assert "10.1.2.3:7890" in window.proxy_status_label.text()
    assert "哔哩哔哩" in window.proxy_status_label.text()


def test_disabling_the_switch_disables_the_inputs(window: gui.MainWindow) -> None:
    window.proxy_enable_check.setChecked(False)
    assert not window.proxy_host_edit.isEnabled()
    assert not window.proxy_port_edit.isEnabled()
    assert not window.proxy_test_button.isEnabled()

    window.proxy_enable_check.setChecked(True)
    assert window.proxy_host_edit.isEnabled()
    assert window.proxy_test_button.isEnabled()


def _use_temp_env(monkeypatch, env_file: Path) -> None:
    """Keep the window's save button away from the developer's real .env."""

    monkeypatch.setattr(
        gui,
        "save_proxy_configuration",
        lambda config: settings_module.save_proxy_configuration(config, env_file),
    )


def test_saving_the_proxy_writes_env_and_applies_it_live(
    qtbot, monkeypatch, settings: Settings, registry: PlatformRegistry, tmp_path, clean_proxy_env
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "YOUTUBE_API_KEY=keep-me\nHTTP_PROXY=\nPROXY_BYPASS_PLATFORMS=bilibili\n",
        encoding="utf-8",
    )
    _use_temp_env(monkeypatch, env_file)

    window = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(window)
    window.proxy_enable_check.setChecked(True)
    window.proxy_host_edit.setText("127.0.0.1")
    window.proxy_port_edit.setText("8090")

    assert window.save_proxy() is True

    text = env_file.read_text(encoding="utf-8")
    assert "HTTP_PROXY=http://127.0.0.1:8090" in text
    assert "HTTPS_PROXY=http://127.0.0.1:8090" in text
    assert "PROXY_BYPASS_PLATFORMS=bilibili" in text
    assert "YOUTUBE_API_KEY=keep-me" in text
    # Applied immediately, without restarting the GUI.
    assert settings.proxy == "http://127.0.0.1:8090"
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:8090"
    assert "已保存" in window.proxy_message_label.text()
    assert "http://127.0.0.1:8090" in window.proxy_status_label.text()


def test_saving_a_disabled_proxy_stops_using_it(
    qtbot, monkeypatch, settings: Settings, registry: PlatformRegistry, tmp_path, clean_proxy_env
) -> None:
    env_file = tmp_path / ".env"
    _use_temp_env(monkeypatch, env_file)
    settings.http_proxy = "http://127.0.0.1:8090"
    settings.https_proxy = "http://127.0.0.1:8090"
    settings_module.apply_proxy_environment(settings)
    assert os.environ.get("HTTP_PROXY") is not None

    window = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(window)
    window.proxy_enable_check.setChecked(False)

    assert window.save_proxy() is True
    assert settings.proxy is None
    assert os.environ.get("HTTP_PROXY") is None
    assert "HTTP_PROXY=\n" in env_file.read_text(encoding="utf-8")
    assert "未启用代理" in window.proxy_status_label.text()


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [("", "8090", "地址"), ("127.0.0.1", "70000", "端口")],
)
def test_saving_rejects_invalid_input(
    qtbot,
    monkeypatch,
    settings: Settings,
    registry: PlatformRegistry,
    tmp_path,
    host: str,
    port: str,
    expected: str,
) -> None:
    env_file = tmp_path / ".env"
    _use_temp_env(monkeypatch, env_file)
    window = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(window)
    window.proxy_enable_check.setChecked(True)
    window.proxy_host_edit.setText(host)
    window.proxy_port_edit.setText(port)

    assert window.save_proxy() is False
    assert expected in window.proxy_message_label.text()
    assert not env_file.exists()


def test_proxy_platform_switches_are_written_to_the_bypass_list(
    qtbot, monkeypatch, settings: Settings, registry: PlatformRegistry, tmp_path, clean_proxy_env
) -> None:
    env_file = tmp_path / ".env"
    _use_temp_env(monkeypatch, env_file)
    window = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(window)
    window.proxy_enable_check.setChecked(True)
    window.proxy_platform_checks["youtube"].setChecked(False)
    window.proxy_platform_checks["instagram"].setChecked(True)
    window.proxy_platform_checks["bilibili"].setChecked(False)

    assert window.save_proxy() is True

    assert "PROXY_BYPASS_PLATFORMS=youtube,bilibili" in env_file.read_text(encoding="utf-8")
    assert settings.proxy_bypass_set == {"youtube", "bilibili"}


def test_test_proxy_reports_success(
    qtbot, monkeypatch, settings: Settings, registry: PlatformRegistry
) -> None:
    async def fake_check(proxy: str, **kwargs: object) -> tuple[bool, str]:
        return True, "代理连接成功（HTTP 204）"

    monkeypatch.setattr(gui, "check_proxy", fake_check)
    window = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(window)
    window.proxy_enable_check.setChecked(True)

    window.test_proxy()

    assert window._proxy_test_worker is not None
    assert window._proxy_test_worker.wait(10000)
    QApplication.processEvents()
    assert "成功" in window.proxy_message_label.text()
    assert window.proxy_test_button.isEnabled()


def test_test_proxy_reports_failure(
    qtbot, monkeypatch, settings: Settings, registry: PlatformRegistry
) -> None:
    async def fake_check(proxy: str, **kwargs: object) -> tuple[bool, str]:
        return False, "无法连接到 127.0.0.1:9，请确认代理正在运行"

    monkeypatch.setattr(gui, "check_proxy", fake_check)
    window = gui.MainWindow(settings, registry=registry)
    qtbot.addWidget(window)
    window.proxy_enable_check.setChecked(True)

    window.test_proxy()

    assert window._proxy_test_worker is not None
    assert window._proxy_test_worker.wait(10000)
    QApplication.processEvents()
    assert "无法连接" in window.proxy_message_label.text()


def test_test_proxy_needs_the_switch(window: gui.MainWindow) -> None:
    window.proxy_enable_check.setChecked(False)
    window.test_proxy()
    assert window._proxy_test_worker is None
    assert "启用代理" in window.proxy_message_label.text()


# --- packaged-build self test ----------------------------------------------


def test_parse_self_test_ignores_normal_launches() -> None:
    assert gui.parse_self_test(["gui.py"]) is None
    assert gui.parse_self_test(["VideoDownloader.exe"]) is None
    assert gui.parse_self_test(None) is None


def test_parse_self_test_reads_url_quality_and_consent() -> None:
    parsed = gui.parse_self_test(
        [
            "VideoDownloader.exe",
            "--self-test",
            "https://youtu.be/x",
            "--quality",
            "720p",
            "--confirm-rights",
        ]
    )
    assert parsed == ("https://youtu.be/x", "720p", True)


def test_parse_self_test_defaults_to_best_and_no_consent() -> None:
    parsed = gui.parse_self_test(["VideoDownloader.exe", "--self-test", "https://youtu.be/x"])
    assert parsed == ("https://youtu.be/x", "best", False)


def test_parse_self_test_requires_a_url() -> None:
    with pytest.raises(ValueError, match="--self-test"):
        gui.parse_self_test(["VideoDownloader.exe", "--self-test"])
    with pytest.raises(ValueError):
        gui.parse_self_test(["VideoDownloader.exe", "--self-test", "--quality", "720p"])


def test_emit_survives_a_windowed_build_without_stdout(monkeypatch, capsys) -> None:
    """console=False leaves sys.stdout as None; printing must not crash."""

    gui.emit("普通输出")
    assert "普通输出" in capsys.readouterr().out

    monkeypatch.setattr(gui.sys, "stdout", None)
    gui.emit("无控制台输出")  # must not raise AttributeError


# --- sign-in panel ----------------------------------------------------------


def test_login_panel_lists_only_sign_in_platforms(window: gui.MainWindow) -> None:
    assert set(window.login_rows) == {Platform.BILIBILI, Platform.INSTAGRAM}
    for state_label, button in window.login_rows.values():
        assert state_label.text() == "检测中…"
        assert button.text() == "登录"
        assert not button.isEnabled()


def test_login_row_shows_a_logged_in_session(window: gui.MainWindow, settings: Settings) -> None:
    session_store.write_session(
        settings,
        Platform.BILIBILI,
        [session_store.make_cookie("SESSDATA", "v", domain=".bilibili.com")],
        domain_suffix="bilibili.com",
    )
    window.login_status[Platform.BILIBILI] = SessionStatus(
        platform=Platform.BILIBILI, state=LoginState.LOGGED_IN, account="某人"
    )
    window._refresh_login_row(Platform.BILIBILI)

    state_label, button = window.login_rows[Platform.BILIBILI]
    assert "已登录" in state_label.text()
    assert "某人" in state_label.text()
    assert button.text() == "退出登录"
    assert button.isEnabled()


def test_login_row_shows_an_expired_session(window: gui.MainWindow) -> None:
    window.login_status[Platform.INSTAGRAM] = SessionStatus(
        platform=Platform.INSTAGRAM, state=LoginState.EXPIRED
    )
    window._refresh_login_row(Platform.INSTAGRAM)

    state_label, button = window.login_rows[Platform.INSTAGRAM]
    assert "登录已过期" in state_label.text()
    assert button.text() == "重新登录"


def test_login_row_flags_a_session_that_came_from_env(
    window: gui.MainWindow, settings: Settings
) -> None:
    """A session in .env is not one this application stored, and says so."""

    window.login_status[Platform.BILIBILI] = SessionStatus(
        platform=Platform.BILIBILI, state=LoginState.LOGGED_IN
    )
    window._refresh_login_row(Platform.BILIBILI)

    state_label, _button = window.login_rows[Platform.BILIBILI]
    assert ".env" in state_label.text()


def _write_both_sessions(settings: Settings) -> None:
    """One managed session per platform, both inside the temporary root."""

    for platform, name, domain in (
        (Platform.BILIBILI, "SESSDATA", ".bilibili.com"),
        (Platform.INSTAGRAM, "sessionid", ".instagram.com"),
    ):
        session_store.write_session(
            settings,
            platform,
            [session_store.make_cookie(name, "v", domain=domain)],
            domain_suffix=domain.lstrip("."),
        )


@pytest.mark.parametrize("target", [Platform.BILIBILI, Platform.INSTAGRAM])
def test_logout_removes_only_this_application_session(
    window: gui.MainWindow, settings: Settings, monkeypatch, target: Platform
) -> None:
    """A confirmed logout deletes exactly one file, whatever Qt returns.

    ``QMessageBox.question`` is documented as returning ``StandardButton`` but
    PySide6 6.11 actually returns a plain ``int`` (``0x4000`` for Yes). The stub
    returns that same ``int`` so this test exercises the comparison the user's
    Qt build performs - returning the enum member is what hid the v1.07 bug.
    """

    _write_both_sessions(settings)

    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *a, **k: int(gui.QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._logout(window.login_adapters[target])

    other = Platform.INSTAGRAM if target is Platform.BILIBILI else Platform.BILIBILI
    assert not session_store.has_session(settings, target)
    assert session_store.has_session(settings, other), "另一个平台必须保留"
    assert window.login_status[target] is not None
    assert window.login_status[target].state is LoginState.LOGGED_OUT


def test_logout_can_be_cancelled(window: gui.MainWindow, settings: Settings, monkeypatch) -> None:
    """``No`` - an ``int`` again - must leave every session file alone."""

    _write_both_sessions(settings)
    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *a, **k: int(gui.QMessageBox.StandardButton.No),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._logout(window.login_adapters[Platform.INSTAGRAM])

    assert session_store.has_session(settings, Platform.INSTAGRAM)
    assert session_store.has_session(settings, Platform.BILIBILI)


def test_logout_only_ever_touches_the_temporary_session_directory(
    window: gui.MainWindow, settings: Settings, monkeypatch, tmp_path: Path
) -> None:
    """The sign-in flow must never reach the developer's real ``secrets/``.

    ``conftest.settings`` points ``session_dir`` at ``tmp_path``; a logout that
    escaped that folder would delete a real session file, so the boundary is
    asserted explicitly rather than assumed.
    """

    _write_both_sessions(settings)
    managed = session_store.session_dir(settings)
    assert managed == tmp_path / "secrets"
    assert managed.is_relative_to(tmp_path)

    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *a, **k: int(gui.QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._logout(window.login_adapters[Platform.INSTAGRAM])

    assert not (managed / "instagram_cookies.txt").exists()
    assert (managed / "bilibili_cookies.txt").is_file()


def _write_fallback_cookies(path: Path, *, domain: str, name: str) -> Path:
    """A simulated ``.env`` cookie file - temporary, with fake values only."""

    path.write_text(
        f"# Netscape HTTP Cookie File\n.{domain}\tTRUE\t/\tTRUE\t0\t{name}\tFROMFALLBACK\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("platform", "key"),
    [(Platform.INSTAGRAM, "YTDLP_COOKIEFILE"), (Platform.BILIBILI, "BILIBILI_COOKIEFILE")],
)
def test_login_row_names_the_env_source(
    window: gui.MainWindow, platform: Platform, key: str
) -> None:
    """The row must name the key, not just mumble about ``.env``."""

    window.login_status[platform] = SessionStatus(
        platform=platform,
        state=LoginState.LOGGED_IN,
        account="某人",
        origin=SessionOrigin(LoginSource.ENV_COOKIEFILE, key),
    )
    window._refresh_login_row(platform)

    state_label, _button = window.login_rows[platform]
    text = state_label.text()
    assert ".env" in text
    assert key in text
    assert "本程序未保存会话" in text


def test_login_row_stays_quiet_for_a_managed_session(window: gui.MainWindow) -> None:
    platform = Platform.INSTAGRAM
    window.login_status[platform] = SessionStatus(
        platform=platform,
        state=LoginState.LOGGED_IN,
        account="某人",
        origin=SessionOrigin(LoginSource.MANAGED),
    )
    window._refresh_login_row(platform)

    state_label, _button = window.login_rows[platform]
    assert ".env" not in state_label.text(), "本程序保存的会话不需要来源提示"


def test_login_summary_names_the_env_source(window: gui.MainWindow) -> None:
    window.login_status[Platform.INSTAGRAM] = SessionStatus(
        platform=Platform.INSTAGRAM,
        state=LoginState.LOGGED_IN,
        account="bosco.slash",
        origin=SessionOrigin(LoginSource.ENV_COOKIEFILE, "YTDLP_COOKIEFILE"),
    )

    summary = window._login_summary()

    assert "YTDLP_COOKIEFILE" in summary
    assert "来自 .env" in summary


def test_logout_with_nothing_to_do_only_explains(
    window: gui.MainWindow, settings: Settings, monkeypatch
) -> None:
    """No managed session and no fallback: there is genuinely nothing to do."""

    settings.ytdlp_cookiefile = None
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = ""

    explained: list[str] = []
    monkeypatch.setattr(
        gui.QMessageBox, "information", lambda *args, **kwargs: explained.append(args[2])
    )
    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *args, **kwargs: pytest.fail("没有可退出的内容时不应弹「确认退出」"),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._logout(window.login_adapters[Platform.INSTAGRAM])

    assert explained, "应弹出说明"
    assert ".env" in explained[0]
    assert not fallback_policy.is_disabled(settings, Platform.INSTAGRAM)


def test_logout_dialog_names_the_keys_it_will_disable(
    window: gui.MainWindow, settings: Settings, monkeypatch, tmp_path: Path
) -> None:
    """The confirmation says exactly what changes - and what does not."""

    settings.ytdlp_cookiefile = str(tmp_path / "cookies.txt")
    _store_session(settings, Platform.INSTAGRAM)

    prompts: list[str] = []

    def _ask(*args: object, **kwargs: object) -> int:
        prompts.append(str(args[2]))
        # Cancel, so nothing changes and the wording is the only assertion.
        return int(gui.QMessageBox.StandardButton.No)

    monkeypatch.setattr(gui.QMessageBox, "question", _ask)
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._logout(window.login_adapters[Platform.INSTAGRAM])

    assert "YTDLP_COOKIEFILE" in prompts[0]
    assert "禁用" in prompts[0]
    assert "未登录" in prompts[0]
    assert session_store.has_session(settings, Platform.INSTAGRAM), "取消时不得删除"
    assert not fallback_policy.is_disabled(settings, Platform.INSTAGRAM), "取消时不得禁用"


@pytest.mark.parametrize(
    ("platform", "key"),
    [(Platform.INSTAGRAM, "YTDLP_COOKIEFILE"), (Platform.BILIBILI, "BILIBILI_COOKIEFILE")],
)
def test_logout_deletes_the_session_and_disables_the_fallback(
    window: gui.MainWindow,
    settings: Settings,
    monkeypatch,
    tmp_path: Path,
    platform: Platform,
    key: str,
) -> None:
    """The two rows are switched independently, and only one file is deleted."""

    other = Platform.BILIBILI if platform is Platform.INSTAGRAM else Platform.INSTAGRAM
    _store_session(settings, platform, value="MINE")
    _store_session(settings, other, value="OTHER")
    other_path = session_store.session_file(settings, other)
    other_before = other_path.read_bytes()

    if platform is Platform.INSTAGRAM:
        settings.ytdlp_cookiefile = str(tmp_path / "cookies.txt")
    else:
        settings.bilibili_cookie = None
        settings.bilibili_sessdata = None
        settings.bilibili_cookiefile = str(tmp_path / "bili.txt")

    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *args, **kwargs: int(gui.QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._logout(window.login_adapters[platform])

    assert not session_store.has_session(settings, platform), "托管会话应被删除"
    assert fallback_policy.is_disabled(settings, platform) is True, "该平台的回退应被禁用"
    assert window.login_status[platform].state is LoginState.LOGGED_OUT
    # 另一个平台既没被禁用，文件也没被动过
    assert fallback_policy.is_disabled(settings, other) is False
    assert session_store.has_session(settings, other)
    assert other_path.read_bytes() == other_before


def test_logout_result_names_what_was_disabled(
    window: gui.MainWindow, settings: Settings, monkeypatch, tmp_path: Path
) -> None:
    settings.ytdlp_cookiefile = str(tmp_path / "cookies.txt")
    _store_session(settings, Platform.INSTAGRAM)
    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *args, **kwargs: int(gui.QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._logout(window.login_adapters[Platform.INSTAGRAM])

    message = window.login_message.text()
    assert "YTDLP_COOKIEFILE" in message
    assert "禁用" in message


def test_the_reenable_button_appears_only_while_disabled(
    window: gui.MainWindow, settings: Settings
) -> None:
    platform = Platform.INSTAGRAM
    button = window.login_reenable[platform]
    window.login_status[platform] = SessionStatus(platform=platform, state=LoginState.LOGGED_OUT)

    window._refresh_login_row(platform)
    assert button.isHidden() is True

    fallback_policy.set_disabled(settings, platform, disabled=True)
    window._refresh_login_row(platform)

    state_label, _action = window.login_rows[platform]
    assert button.isHidden() is False, "禁用后必须能看到恢复入口"
    assert gui.FALLBACK_DISABLED_HINT in state_label.text()

    fallback_policy.set_disabled(settings, platform, disabled=False)
    window._refresh_login_row(platform)
    assert button.isHidden() is True
    assert gui.FALLBACK_DISABLED_HINT not in state_label.text()


def test_reenable_action_restores_the_env_source(
    window: gui.MainWindow, settings: Settings, monkeypatch, tmp_path: Path
) -> None:
    platform = Platform.INSTAGRAM
    settings.ytdlp_cookiefile = str(tmp_path / "cookies.txt")
    fallback_policy.set_disabled(settings, platform, disabled=True)
    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *args, **kwargs: int(gui.QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._reenable_fallback(window.login_adapters[platform])

    assert fallback_policy.is_disabled(settings, platform) is False
    assert "重新启用" in window.login_message.text()


def test_reenable_action_can_be_cancelled(
    window: gui.MainWindow, settings: Settings, monkeypatch
) -> None:
    platform = Platform.INSTAGRAM
    fallback_policy.set_disabled(settings, platform, disabled=True)
    monkeypatch.setattr(
        gui.QMessageBox,
        "question",
        lambda *args, **kwargs: int(gui.QMessageBox.StandardButton.No),
    )
    monkeypatch.setattr(window, "refresh_login_status", lambda: None)

    window._reenable_fallback(window.login_adapters[platform])

    assert fallback_policy.is_disabled(settings, platform) is True


def test_status_refresh_stays_logged_out_after_the_switch(
    settings: Settings, registry: PlatformRegistry, tmp_path: Path
) -> None:
    """The bounce is gone: the fallback file is still there, but ignored.

    The real ``InstagramAdapter.check_session`` answers without any request when
    the header carries no ``sessionid``, so this stays offline.
    """

    settings.ytdlp_cookiefile = str(
        _write_fallback_cookies(tmp_path / "cookies.txt", domain="instagram.com", name="sessionid")
    )
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.STATUS)
    status = asyncio.run(worker._run())

    assert status.state is LoginState.LOGGED_OUT


def test_a_new_sign_in_still_works_while_the_fallback_is_disabled(
    settings: Settings, registry: PlatformRegistry, monkeypatch, tmp_path: Path
) -> None:
    """A fresh managed session - from the extension or the browser - wins."""

    settings.ytdlp_cookiefile = str(
        _write_fallback_cookies(tmp_path / "cookies.txt", domain="instagram.com", name="sessionid")
    )
    fallback_policy.set_disabled(settings, Platform.INSTAGRAM, disabled=True)
    _store_session(settings, Platform.INSTAGRAM, value="FRESH")

    async def _logged_in(self, header, *, origin=None):  # noqa: ANN001
        return SessionStatus(
            platform=self.adapter.platform,
            state=LoginState.LOGGED_IN,
            origin=origin or SessionOrigin(),
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _logged_in)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.STATUS)
    status = asyncio.run(worker._run())

    assert status.logged_in is True
    assert status.origin.managed is True


def test_status_refresh_labels_the_instagram_fallback_origin(
    settings: Settings, registry: PlatformRegistry, monkeypatch, tmp_path: Path
) -> None:
    """End-to-end: what the row shows after the managed session is gone."""

    settings.ytdlp_cookiefile = str(
        _write_fallback_cookies(tmp_path / "cookies.txt", domain="instagram.com", name="sessionid")
    )

    async def _logged_in(self, header, *, origin=None):  # noqa: ANN001
        return SessionStatus(
            platform=self.adapter.platform,
            state=LoginState.LOGGED_IN,
            account="bosco.slash",
            origin=origin or SessionOrigin(),
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _logged_in)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.STATUS)
    status = asyncio.run(worker._run())

    assert status.logged_in is True
    assert status.origin.source is LoginSource.ENV_COOKIEFILE
    assert status.origin.key == "YTDLP_COOKIEFILE"


def test_status_refresh_labels_the_bilibili_fallback_origin(
    settings: Settings, registry: PlatformRegistry, monkeypatch, tmp_path: Path
) -> None:
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = str(
        _write_fallback_cookies(tmp_path / "bili.txt", domain="bilibili.com", name="SESSDATA")
    )

    async def _logged_in(self, header, *, origin=None):  # noqa: ANN001
        return SessionStatus(
            platform=self.adapter.platform,
            state=LoginState.LOGGED_IN,
            account="某人",
            origin=origin or SessionOrigin(),
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _logged_in)

    worker = gui.LoginWorker(settings, registry.get(Platform.BILIBILI), gui.LoginAction.STATUS)
    status = asyncio.run(worker._run())

    assert status.logged_in is True
    assert status.origin.source is LoginSource.ENV_COOKIEFILE
    assert status.origin.key == "BILIBILI_COOKIEFILE"


def test_wait_for_thread_tolerates_a_destroyed_worker() -> None:
    """Closing the window after a worker was deleteLater'd must not raise."""

    gui.wait_for_thread(None)
    worker = gui.ProxyTestWorker("http://127.0.0.1:9")
    worker.finished.connect(worker.deleteLater)
    worker.start()
    assert worker.wait(10000)
    QApplication.processEvents()  # let deleteLater run

    gui.wait_for_thread(worker)  # must not raise RuntimeError


@pytest.mark.parametrize(
    ("state", "refused"),
    [
        (LoginState.EXPIRED, True),
        (LoginState.LOGGED_OUT, True),
        (LoginState.LOGGED_IN, False),
        # The platform could not be reached: storing the session the user just
        # produced is better than throwing it away over a transient failure.
        (LoginState.UNKNOWN, False),
    ],
)
def test_only_an_explicit_rejection_refuses_a_session(state: LoginState, refused: bool) -> None:
    status = SessionStatus(platform=Platform.BILIBILI, state=state)

    if refused:
        with pytest.raises(SessionValueError):
            gui.LoginWorker._reject_only_when_the_platform_says_no(status, source="测试")
    else:
        gui.LoginWorker._reject_only_when_the_platform_says_no(status, source="测试")


def test_login_worker_never_logs_the_pasted_value(caplog) -> None:
    """A session value must not reach the log, whatever the outcome."""

    secret = "SUPERSECRETSESSIONVALUE"
    with caplog.at_level("DEBUG"), pytest.raises(SessionValueError):
        parse_session_text(
            f"sessionid={secret}", cookie_names=("SESSDATA",), domain=".bilibili.com"
        )

    assert secret not in caplog.text


# --- sign-in progress, duplicate clicks, and browser advice ------------------


class _StubWorker:
    """Stands in for a LoginWorker so UI state can be tested without any I/O."""

    def __init__(self, running: bool = True) -> None:
        self._running = running

    def isRunning(self) -> bool:  # noqa: N802 - Qt API
        return self._running

    def wait(self, _timeout: int = 0) -> bool:  # noqa: N802 - Qt API
        return True


def _outlook(*, default: str | None, readable: tuple[str, ...], installed: bool = True):
    blocked = ("chrome", "edge") if default in {"chrome", "edge"} else ()
    return browser_cookies.AutomaticReadOutlook(
        default_browser=default,
        readable_browsers=readable,
        blocked_browsers=blocked,
        any_installed=installed,
    )


@pytest.fixture
def login_dialog(qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch):
    """A dialog that never touches a real browser."""

    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)
    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)
    return dialog


def test_login_row_shows_progress_while_a_check_runs(window: gui.MainWindow) -> None:
    window.login_status[Platform.BILIBILI] = SessionStatus(
        platform=Platform.BILIBILI, state=LoginState.LOGGED_IN, account="某人"
    )
    window.login_workers[Platform.BILIBILI] = _StubWorker()  # type: ignore[assignment]

    window._refresh_login_row(Platform.BILIBILI)

    state_label, button = window.login_rows[Platform.BILIBILI]
    assert button.text() == gui.LOGIN_BUSY_TEXT
    assert not button.isEnabled(), "检测期间必须禁用，避免重复点击"
    assert "已登录" in state_label.text(), "检测中仍应显示上一次的结论"


def test_refresh_button_tracks_the_running_checks(window: gui.MainWindow) -> None:
    assert window.login_refresh_button.isEnabled()

    window.login_workers[Platform.BILIBILI] = _StubWorker()  # type: ignore[assignment]
    window._refresh_login_controls()
    assert not window.login_refresh_button.isEnabled()

    window.login_workers.clear()
    window._refresh_login_controls()
    assert window.login_refresh_button.isEnabled()


def test_login_summary_explains_an_unsure_verdict(window: gui.MainWindow) -> None:
    window.login_status[Platform.INSTAGRAM] = SessionStatus(
        platform=Platform.INSTAGRAM,
        state=LoginState.UNKNOWN,
        detail="Instagram 返回 HTTP 429，无法确认登录状态",
    )
    window.login_status[Platform.BILIBILI] = SessionStatus(
        platform=Platform.BILIBILI, state=LoginState.LOGGED_IN, account="某人"
    )

    text = window._login_summary()

    assert "状态未知" in text
    assert "429" in text, "必须说明为什么无法确认"
    assert "已登录（某人）" in text


def test_dialog_pre_shows_manual_entry_when_auto_read_is_impossible(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """The user must learn this before logging in, not after."""

    monkeypatch.setattr(
        gui, "automatic_read_outlook", lambda: _outlook(default="chrome", readable=())
    )
    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert not dialog.manual_box.isHidden()
    assert "App-Bound" in dialog.advice_label.text()
    assert "手动填写会话" in dialog.advice_label.text()


def test_dialog_keeps_manual_entry_hidden_when_the_default_is_readable(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    monkeypatch.setattr(
        gui, "automatic_read_outlook", lambda: _outlook(default="firefox", readable=("firefox",))
    )
    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert dialog.manual_box.isHidden()
    assert "可以直接读取" in dialog.advice_label.text()


def test_dialog_offers_firefox_when_it_is_installed(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    monkeypatch.setattr(
        gui,
        "automatic_read_outlook",
        lambda: _outlook(default="chrome", readable=("firefox",)),
    )
    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert not dialog.manual_box.isHidden()
    assert "Firefox" in dialog.advice_label.text()


def test_dialog_shows_progress_and_blocks_a_second_click(login_dialog) -> None:
    login_dialog._set_busy(True, action=gui.LoginAction.ACQUIRE)

    assert login_dialog.detect_button.text() == gui.DETECT_BUSY_TEXT
    for widget in (
        login_dialog.detect_button,
        login_dialog.save_button,
        login_dialog.reopen_button,
        login_dialog.paste_edit,
    ):
        assert not widget.isEnabled()

    login_dialog._set_busy(False)

    assert login_dialog.detect_button.text() == gui.DETECT_BUTTON_TEXT
    assert login_dialog.save_button.text() == gui.PASTE_BUTTON_TEXT
    assert login_dialog.detect_button.isEnabled()
    assert login_dialog.paste_edit.isEnabled()


def test_dialog_labels_the_button_that_is_actually_working(login_dialog) -> None:
    login_dialog._set_busy(True, action=gui.LoginAction.PASTE)

    assert login_dialog.save_button.text() == gui.PASTE_BUSY_TEXT
    assert login_dialog.detect_button.text() == gui.DETECT_BUTTON_TEXT


def test_dialog_refuses_to_start_a_second_check(login_dialog, monkeypatch) -> None:
    login_dialog.worker = _StubWorker()  # type: ignore[assignment]
    started: list[object] = []
    monkeypatch.setattr(gui.LoginWorker, "start", lambda self: started.append(self))

    login_dialog._start(gui.LoginAction.ACQUIRE)

    assert started == [], "已有任务在跑时不得再起一个"


def test_dialog_tells_the_user_when_no_browser_could_be_started(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: False)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert "无法自动打开浏览器" in dialog.status_label.text()
    assert "passport.bilibili.com" in dialog.status_label.text()


def test_dialog_survives_a_browser_that_raises(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> bool:
        raise OSError("no browser registered")

    monkeypatch.setattr(gui.webbrowser, "open", _boom)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert "无法自动打开浏览器" in dialog.status_label.text()


def test_dialog_points_at_the_extension_when_no_session_was_delivered(
    qtbot, settings: Settings, registry: PlatformRegistry
) -> None:
    """The extension is the route that works, so it must not be named last.

    The old wording sent the user straight to 「手动填写会话」 even though the
    session had simply not been handed over yet - the one case where sending
    from the extension again is the whole fix.
    """

    dialog = gui.LoginDialog(settings, registry.get(Platform.INSTAGRAM))
    qtbot.addWidget(dialog)

    dialog._on_browser_unavailable(  # noqa: SLF001 - the callback is the contract
        _unreadable_browser()
    )

    text = dialog.status_label.text()
    assert "登录助手" in text
    assert "发送到 Video Downloader" in text
    assert text.index("登录助手") < text.index("手动填写会话"), "先发送会话，再考虑手动填写"
    assert not dialog.manual_box.isHidden()


def test_login_worker_failure_never_logs_the_pasted_value(
    settings: Settings, registry: PlatformRegistry, caplog
) -> None:
    """A rejected session must not leak its value into the log or the message."""

    secret = "SUPERSECRETSESSIONVALUE"
    worker = gui.LoginWorker(
        settings,
        registry.get(Platform.BILIBILI),
        gui.LoginAction.PASTE,
        f"wrongcookie={secret}",
    )
    failures: list[tuple[str, str]] = []
    worker.failed.connect(lambda message, detail: failures.append((message, detail)))

    with caplog.at_level("DEBUG"):
        worker.run()  # runs in this thread; the value is rejected before any I/O

    assert failures, "无效的会话值应触发失败信号"
    assert secret not in caplog.text
    for message, detail in failures:
        assert secret not in message
        assert secret not in detail


def test_login_worker_reports_a_browser_it_cannot_read(
    settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    error = BrowserCookieError(
        "无法解密 Chrome 的 Cookie（App-Bound Encryption）",
        reason=browser_cookies.BrowserFailure.ENCRYPTED.value,
        browser="chrome",
        detail="首选方案：改用 Firefox 登录",
    )
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))
    # No stored session of any kind: the browser error must be the answer, and
    # nothing may be asked of the network.
    settings.bilibili_cookie = None
    settings.bilibili_sessdata = None
    settings.bilibili_cookiefile = None

    async def _never(header: str | None, *, origin: SessionOrigin | None = None) -> SessionStatus:
        raise AssertionError("没有已保存的会话时不应发起校验请求")

    monkeypatch.setattr(gui.LoginWorker, "_check", _never)

    worker = gui.LoginWorker(settings, registry.get(Platform.BILIBILI), gui.LoginAction.ACQUIRE)
    reported: list[object] = []
    worker.browser_unavailable.connect(reported.append)

    worker.run()

    assert reported == [error]


def _store_session(settings: Settings, platform: Platform, value: str = "STORED") -> None:
    """Write a managed session, so the acquire fallback has something to read."""

    names = {
        Platform.BILIBILI: ("SESSDATA", "bilibili.com"),
        Platform.INSTAGRAM: ("sessionid", "instagram.com"),
    }
    cookie_name, domain = names[platform]
    session_store.write_session(
        settings,
        platform,
        [session_store.make_cookie(cookie_name, value, domain=f".{domain}")],
        domain_suffix=domain,
    )


def _unreadable_browser() -> BrowserCookieError:
    return BrowserCookieError(
        "无法解密 Chrome 的 Cookie（App-Bound Encryption）",
        reason=browser_cookies.BrowserFailure.ENCRYPTED.value,
        browser="chrome",
        detail="首选方案：安装「Chrome 登录助手」扩展",
    )


def test_acquire_confirms_the_session_the_extension_already_saved(
    settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """App-Bound Encryption blocks Chrome - but the helper already delivered it.

    "我已登录，检测会话" must confirm what is already on disk instead of telling
    the user to type in a cookie the extension has already handed over.
    """

    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))
    _store_session(settings, Platform.INSTAGRAM)

    checked: list[str | None] = []

    async def _logged_in(  # noqa: ANN001 - stands in for LoginWorker._check
        self: gui.LoginWorker, header: str | None, *, origin: SessionOrigin | None = None
    ) -> SessionStatus:
        checked.append(header)
        return SessionStatus(platform=Platform.INSTAGRAM, state=LoginState.LOGGED_IN)

    monkeypatch.setattr(gui.LoginWorker, "_check", _logged_in)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)
    ready: list[SessionStatus] = []
    unavailable: list[object] = []
    worker.status_ready.connect(ready.append)
    worker.browser_unavailable.connect(unavailable.append)

    worker.run()

    assert unavailable == [], "扩展已保存的会话可用时不应再报「浏览器不可读」"
    assert [status.state for status in ready] == [LoginState.LOGGED_IN]
    assert checked and checked[0] is not None
    assert "sessionid=" in (checked[0] or "")


def test_acquire_never_asks_the_platform_when_nothing_is_stored(
    settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))

    async def _never(  # noqa: ANN001 - stands in for LoginWorker._check
        self: gui.LoginWorker, header: str | None, *, origin: SessionOrigin | None = None
    ) -> SessionStatus:
        raise AssertionError("没有已保存的会话时不应发起校验请求")

    monkeypatch.setattr(gui.LoginWorker, "_check", _never)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)
    unavailable: list[object] = []
    worker.browser_unavailable.connect(unavailable.append)

    worker.run()

    assert unavailable == [error]


def test_acquire_repairs_a_foreign_registration_before_relying_on_the_extension(
    settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """A stale registration sends the extension's delivery somewhere we never read.

    Chrome gives a native-messaging message to whichever host the ``HKCU`` key
    names. A development checkout - or an installation under a different data
    root - that registered last therefore swallows the session into *its*
    folder: the helper reports success, the file really is on disk, and
    「自动获取」 still answers "no session", because nothing ever landed here.
    Re-pointing the key before the user is asked to send is what closes that.
    """

    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))

    async def _never(  # noqa: ANN001 - stands in for LoginWorker._check
        self: gui.LoginWorker, header: str | None, *, origin: SessionOrigin | None = None
    ) -> SessionStatus:
        raise AssertionError("没有已保存的会话时不应发起校验请求")

    monkeypatch.setattr(gui.LoginWorker, "_check", _never)

    asked: list[Settings] = []

    def _repair(target: Settings) -> tuple[str, ...]:
        asked.append(target)
        return ("chrome",)

    monkeypatch.setattr(gui, "repair_registration", _repair)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)
    worker.run()

    assert asked == [settings], "获取会话前必须先确认注册指向本程序"


def test_a_registry_that_cannot_be_repaired_never_blocks_a_sign_in(
    settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """The re-point is best effort: a registry hiccup must not become an error."""

    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))

    def _boom(target: Settings) -> tuple[str, ...]:
        raise OSError("注册表不可用")

    monkeypatch.setattr(gui, "repair_registration", _boom)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)
    reported: list[object] = []
    worker.browser_unavailable.connect(reported.append)

    worker.run()

    assert reported == [error], "注册表故障不应改变对用户的答复"


def test_a_missing_session_is_recorded_in_the_log(
    settings: Settings, registry: PlatformRegistry, monkeypatch, caplog
) -> None:
    """The two failure modes must be distinguishable after the fact.

    "the extension never delivered" and "the delivery went to a folder this
    application does not read" used to look identical *and* leave no trace at
    all, which is what made this report so hard to diagnose.
    """

    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))
    monkeypatch.setattr(gui, "repair_registration", lambda target: ())

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)

    with caplog.at_level(logging.INFO):
        worker.run()

    assert "没有已保存的 instagram 会话" in caplog.text


def test_a_received_session_the_platform_rejects_is_logged_apart(
    settings: Settings, registry: PlatformRegistry, monkeypatch, caplog
) -> None:
    """"Delivered, but the platform says no" must not read like "nothing arrived"."""

    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))
    monkeypatch.setattr(gui, "repair_registration", lambda target: ())
    _store_session(settings, Platform.INSTAGRAM)

    async def _expired(  # noqa: ANN001 - stands in for LoginWorker._check
        self: gui.LoginWorker, header: str | None, *, origin: SessionOrigin | None = None
    ) -> SessionStatus:
        return SessionStatus(
            platform=Platform.INSTAGRAM, state=LoginState.EXPIRED, detail="登录已过期"
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _expired)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)

    with caplog.at_level(logging.INFO):
        worker.run()

    assert "已收到 instagram 的会话，但平台判定未登录" in caplog.text
    assert "没有已保存的 instagram 会话" not in caplog.text


def test_a_received_session_that_cannot_be_confirmed_is_logged_apart(
    settings: Settings, registry: PlatformRegistry, monkeypatch, caplog
) -> None:
    """"Delivered, but we could not ask the platform" is its own sentence too."""

    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))
    monkeypatch.setattr(gui, "repair_registration", lambda target: ())
    _store_session(settings, Platform.INSTAGRAM)

    async def _unknown(  # noqa: ANN001 - stands in for LoginWorker._check
        self: gui.LoginWorker, header: str | None, *, origin: SessionOrigin | None = None
    ) -> SessionStatus:
        return SessionStatus(
            platform=Platform.INSTAGRAM,
            state=LoginState.UNKNOWN,
            detail="Instagram 触发限流（HTTP 429）",
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _unknown)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)

    with caplog.at_level(logging.INFO):
        worker.run()

    assert "已收到 instagram 的会话，但暂时无法向平台确认" in caplog.text
    assert "平台判定未登录" not in caplog.text
    assert "没有已保存的 instagram 会话" not in caplog.text


def test_the_login_dialog_repairs_the_registration_before_inviting_a_send(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """The dialog is where the user is told to send, so it must be reachable.

    Sending from the extension only delivers to *this* installation while the
    HKCU key names our host, and this box is what tells the user to go and send.
    """

    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)
    asked: list[Settings] = []

    def _repair(target: Settings) -> tuple[str, ...]:
        asked.append(target)
        return ("chrome",)

    monkeypatch.setattr(gui, "repair_registration", _repair)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert asked == [settings], "登录对话框出现前必须先把注册指向本程序"


def test_acquire_keeps_the_stored_session_when_it_cannot_be_confirmed(
    settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """A rate-limited check must not throw the stored session away."""

    error = _unreadable_browser()
    monkeypatch.setattr(gui, "extract_session", lambda domain: (_ for _ in ()).throw(error))
    _store_session(settings, Platform.INSTAGRAM)
    session_path = session_store.session_file(settings, Platform.INSTAGRAM)

    async def _unknown(  # noqa: ANN001 - stands in for LoginWorker._check
        self: gui.LoginWorker, header: str | None, *, origin: SessionOrigin | None = None
    ) -> SessionStatus:
        return SessionStatus(
            platform=Platform.INSTAGRAM,
            state=LoginState.UNKNOWN,
            detail="Instagram 触发限流（HTTP 429）",
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _unknown)

    worker = gui.LoginWorker(settings, registry.get(Platform.INSTAGRAM), gui.LoginAction.ACQUIRE)
    unavailable: list[object] = []
    ready: list[SessionStatus] = []
    worker.browser_unavailable.connect(unavailable.append)
    worker.status_ready.connect(ready.append)

    worker.run()

    assert unavailable == [error], "无法确认时应保留原有的浏览器错误提示"
    assert ready == []
    assert session_path.is_file(), "无法确认不是删除会话的理由"


def test_a_stale_worker_finish_does_not_untrack_the_live_one(
    window: gui.MainWindow,
) -> None:
    """A queued ``finished`` from an older worker must not evict its successor.

    ``refresh_login_status`` can replace the tracked worker before the previous
    one's ``finished`` slot runs; popping unconditionally would leave the live
    worker untracked, so nothing would wait for it when the window closes.
    """

    stale = _StubWorker(running=False)
    live = _StubWorker()
    window.login_workers[Platform.BILIBILI] = live  # type: ignore[assignment]

    window._on_login_worker_finished(window.login_adapters[Platform.BILIBILI], stale)  # type: ignore[arg-type]

    assert window.login_workers.get(Platform.BILIBILI) is live


def test_a_matching_worker_finish_clears_the_tracking(window: gui.MainWindow) -> None:
    live = _StubWorker(running=False)
    window.login_workers[Platform.BILIBILI] = live  # type: ignore[assignment]

    window._on_login_worker_finished(window.login_adapters[Platform.BILIBILI], live)  # type: ignore[arg-type]

    assert Platform.BILIBILI not in window.login_workers


def test_the_open_dialog_is_tracked_and_released(window: gui.MainWindow, monkeypatch) -> None:
    """``closeEvent`` needs a handle on the dialog to wait for its worker."""

    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)
    seen: dict[str, object] = {}

    def _exec(self: gui.LoginDialog) -> int:
        seen["during"] = window._login_dialog
        return 0

    monkeypatch.setattr(gui.LoginDialog, "exec", _exec)

    window._on_login_button(window.login_adapters[Platform.BILIBILI])

    assert seen["during"] is not None
    assert window._login_dialog is None, "对话框关闭后必须释放引用"


def test_close_waits_for_a_worker_running_inside_the_dialog(
    window: gui.MainWindow, monkeypatch
) -> None:
    dialog = gui.LoginDialog(window.settings, window.login_adapters[Platform.BILIBILI], window)
    dialog.worker = _StubWorker()  # type: ignore[assignment]
    window._login_dialog = dialog

    waited: list[object] = []
    monkeypatch.setattr(gui, "wait_for_thread", lambda thread, *a, **k: waited.append(thread))

    window.close()

    assert dialog.worker in waited, "关闭窗口必须等待对话框里仍在运行的检测线程"
    window._login_dialog = None


def test_cancelling_the_login_dialog_waits_for_its_worker(
    window: gui.MainWindow, monkeypatch
) -> None:
    """``reject`` never reaches ``closeEvent`` - it goes through ``done``.

    The 取消 button calls ``reject()``, which hides the dialog and lets
    ``exec()`` return; the caller then drops its reference. Without a wait the
    dialog is destroyed with its QThread still running, and a QThread destroyed
    while running aborts the process.
    """

    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)
    dialog = gui.LoginDialog(window.settings, window.login_adapters[Platform.BILIBILI], window)
    dialog.worker = _StubWorker()  # type: ignore[assignment]

    waited: list[object] = []
    monkeypatch.setattr(gui, "wait_for_thread", lambda thread, *a, **k: waited.append(thread))

    dialog.reject()

    assert dialog.worker in waited


def test_closing_the_helper_dialog_waits_for_its_worker(
    qtbot, settings: Settings, monkeypatch
) -> None:
    """The same trap, in the dialog that owns the bridge worker."""

    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status())
    dialog = gui.ChromeHelperDialog(settings)
    qtbot.addWidget(dialog)
    dialog.worker = _StubWorker()  # type: ignore[assignment]

    waited: list[object] = []
    monkeypatch.setattr(gui, "wait_for_thread", lambda thread, *a, **k: waited.append(thread))

    dialog.accept()

    assert dialog.worker in waited


# --- Chrome login helper -----------------------------------------------------


def _bridge_status(
    *,
    present: bool = True,
    registered: bool = True,
    delivered: bool = False,
    host_present: bool = True,
) -> BridgeStatus:
    return BridgeStatus(
        extension_present=present,
        extension_id="deegmfcjldojkppoahflbkikhppnfepd" if present else None,
        extension_version="1.0.0" if present else None,
        extension_dir=Path("chrome-extension") if present else None,
        registered=registered,
        registered_browsers=("chrome", "edge") if registered else (),
        expected_manifest=Path("native_host") / "com.videodownloader.cookies.json",
        session_dir=Path("secrets"),
        last_delivery=datetime(2026, 1, 2, tzinfo=UTC) if delivered else None,
        host_present=host_present,
    )


@pytest.fixture
def chrome_helper(qtbot, settings: Settings, monkeypatch):
    """A helper dialog that never starts Chrome or the file manager."""

    monkeypatch.setattr(gui, "bridge_status", lambda _settings: _bridge_status())
    monkeypatch.setattr(gui, "open_extensions_page", lambda browser: True)
    monkeypatch.setattr(gui, "open_directory", lambda path: True)
    dialog = gui.ChromeHelperDialog(settings)
    qtbot.addWidget(dialog)
    return dialog


def test_chrome_installed_tracks_the_executable(monkeypatch) -> None:
    monkeypatch.setattr(gui, "_chrome_installed", lambda: True)
    assert gui._chrome_installed() is True


def test_safe_bridge_status_swallows_a_bridge_error(settings: Settings, monkeypatch) -> None:
    def _boom(_settings: Settings) -> BridgeStatus:
        raise ChromeBridgeError("坏了")

    monkeypatch.setattr(gui, "bridge_status", _boom)
    assert gui._safe_bridge_status(settings) is None


def test_the_helper_dialog_repairs_the_registration(qtbot, settings: Settings, monkeypatch) -> None:
    """An upgrade's uninstaller deletes our keys; opening the dialog puts them back."""

    calls: list[Settings] = []
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status())
    monkeypatch.setattr(gui, "repair_registration", lambda target: calls.append(target) or ())

    dialog = gui.ChromeHelperDialog(settings)
    qtbot.addWidget(dialog)

    assert calls == [settings]


def test_a_failed_repair_never_breaks_the_dialog(qtbot, settings: Settings, monkeypatch) -> None:
    def _boom(_settings: Settings) -> tuple[str, ...]:
        raise ChromeBridgeError("注册表坏了")

    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status())
    monkeypatch.setattr(gui, "repair_registration", _boom)

    dialog = gui.ChromeHelperDialog(settings)
    qtbot.addWidget(dialog)

    assert dialog.status_label.text() == _bridge_status().summary()


def test_the_dialog_never_touches_the_registry_without_a_manifest(
    qtbot, settings: Settings, monkeypatch
) -> None:
    """A machine that never installed the helper must not be touched at all.

    ``settings`` points its data root at ``tmp_path``, so there is no manifest -
    and the repair path must stop before it reads anything from ``HKCU``.
    """

    from core import chrome_bridge

    class _Forbidden:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"不应访问注册表：{name}")

    monkeypatch.setattr(chrome_bridge, "winreg", _Forbidden())
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status())

    dialog = gui.ChromeHelperDialog(settings)
    qtbot.addWidget(dialog)

    assert dialog.status_label.text() == _bridge_status().summary()


def test_friendly_error_surfaces_a_bridge_message() -> None:
    message = gui.friendly_error(ChromeBridgeError("找不到 Chrome 扩展文件"))
    assert message == "找不到 Chrome 扩展文件"


def test_helper_dialog_describes_the_bridge(chrome_helper) -> None:
    text = chrome_helper.detail_label.text()
    assert "deegmfcjldojkppoahflbkikhppnfepd" in text
    assert "1.0.0" in text
    assert "chrome, edge" in text
    assert "尚未收到" in text
    assert chrome_helper.status_label.text() == _bridge_status().summary()


def test_helper_dialog_says_when_it_is_ready(qtbot, settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status(delivered=True))
    dialog = gui.ChromeHelperDialog(settings)
    qtbot.addWidget(dialog)
    assert "已就绪" in dialog.status_label.text()
    assert "最近一次收到会话" in dialog.detail_label.text()


def test_helper_dialog_shows_progress_and_blocks_a_second_click(chrome_helper) -> None:
    chrome_helper._set_busy(True)

    assert chrome_helper.install_button.text() == gui.CHROME_HELPER_BUSY_TEXT
    for widget in (
        chrome_helper.install_button,
        chrome_helper.check_button,
        chrome_helper.uninstall_button,
        chrome_helper.page_button,
        chrome_helper.folder_button,
    ):
        assert not widget.isEnabled()

    chrome_helper._set_busy(False)
    assert chrome_helper.install_button.text() == gui.CHROME_HELPER_BUTTON_TEXT
    assert chrome_helper.install_button.isEnabled()


def test_helper_dialog_renders_a_failure_without_a_status(chrome_helper) -> None:
    chrome_helper._on_done(
        {"ok": False, "message": "写注册表失败", "detail": "denied", "status": None}
    )
    assert chrome_helper.status_label.text() == "写注册表失败"
    assert "denied" in chrome_helper.detail_label.text()


def test_helper_dialog_opens_the_extension_page(chrome_helper) -> None:
    chrome_helper._open_page()
    assert "开发者模式" in chrome_helper.detail_label.text()


def test_helper_dialog_reports_an_unopenable_folder(chrome_helper, monkeypatch) -> None:
    monkeypatch.setattr(gui, "open_directory", lambda path: False)
    chrome_helper._open_folder()
    assert "请手动打开" in chrome_helper.detail_label.text()


def test_helper_dialog_survives_a_missing_extension(qtbot, settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status(present=False))
    dialog = gui.ChromeHelperDialog(settings)
    qtbot.addWidget(dialog)
    assert "缺失" in dialog.status_label.text()
    assert "（不可用）" in dialog.detail_label.text()


def test_main_window_offers_the_chrome_helper(window: gui.MainWindow) -> None:
    assert window.chrome_helper_button.isEnabled()
    assert gui.CHROME_HELPER_TITLE in window.chrome_helper_button.text()


def test_main_window_opens_the_helper_dialog(window: gui.MainWindow, monkeypatch) -> None:
    opened: list[object] = []

    def _exec(self) -> int:  # noqa: ANN001 - Qt signature
        opened.append(self)
        return 0

    monkeypatch.setattr(gui.ChromeHelperDialog, "exec", _exec)
    window._open_chrome_helper()

    assert len(opened) == 1
    assert "登录助手" in window.login_message.text()


def test_login_dialog_offers_the_extension_when_chromium_blocks(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """Requirement: a blocked browser must show the new route, not a refusal."""

    monkeypatch.setattr(
        gui, "automatic_read_outlook", lambda: _outlook(default="chrome", readable=())
    )
    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status())

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert not dialog.chrome_box.isHidden()
    assert "Chrome 登录助手" in dialog.advice_label.text()
    assert dialog.chrome_status_label.text()


def test_login_dialog_hides_the_extension_when_the_default_is_readable(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    monkeypatch.setattr(
        gui, "automatic_read_outlook", lambda: _outlook(default="firefox", readable=("firefox",))
    )
    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert dialog.chrome_box.isHidden()


def test_login_dialog_prompts_to_send_from_the_extension(login_dialog, monkeypatch) -> None:
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status())
    login_dialog._refresh_chrome_box()
    assert "发送到 Video Downloader" in login_dialog.chrome_send_hint.text()


def test_login_dialog_prompts_to_install_when_not_ready(login_dialog, monkeypatch) -> None:
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status(registered=False))
    login_dialog._refresh_chrome_box()
    assert "安装" in login_dialog.chrome_send_hint.text()


def test_login_dialog_handles_an_unknown_bridge_state(login_dialog, monkeypatch) -> None:
    monkeypatch.setattr(gui, "_safe_bridge_status", lambda _s: None)
    login_dialog._refresh_chrome_box()
    assert "状态未知" in login_dialog.chrome_status_label.text()


def test_login_dialog_busy_state_covers_the_new_controls(login_dialog) -> None:
    login_dialog._set_busy(True, action=gui.LoginAction.CDP_LAUNCH)
    for widget in (
        login_dialog.chrome_button,
        login_dialog.cdp_launch_button,
        login_dialog.cdp_fetch_button,
        login_dialog.cdp_close_button,
    ):
        assert not widget.isEnabled()
    assert login_dialog.cdp_launch_button.text() == gui.CHROME_HELPER_BUSY_TEXT

    login_dialog._set_busy(True, action=gui.LoginAction.CDP_FETCH)
    assert login_dialog.cdp_fetch_button.text() == gui.PASTE_BUSY_TEXT

    login_dialog._set_busy(False)
    assert login_dialog.cdp_launch_button.text() == gui.CDP_LAUNCH_TEXT
    assert login_dialog.cdp_fetch_button.text() == gui.CDP_FETCH_TEXT
    assert login_dialog.cdp_close_button.isEnabled()


def test_launching_chrome_does_not_close_the_dialog(login_dialog) -> None:
    """The fallback is a two-step flow; step one must not dismiss the window."""

    accepted: list[bool] = []
    login_dialog.accepted.connect(lambda: accepted.append(True))

    login_dialog._last_action = gui.LoginAction.CDP_LAUNCH
    login_dialog._on_status(
        SessionStatus(
            platform=Platform.BILIBILI,
            state=LoginState.UNKNOWN,
            detail="已打开独立 Chrome（配置目录：chrome-profile）。",
        )
    )

    assert accepted == [], "第一步只是启动 Chrome，不能关闭对话框"
    assert "独立 Chrome" in login_dialog.status_label.text()


def test_a_stored_but_unconfirmed_session_still_closes_the_dialog(login_dialog) -> None:
    accepted: list[bool] = []
    login_dialog.accepted.connect(lambda: accepted.append(True))

    login_dialog._last_action = gui.LoginAction.CDP_FETCH
    login_dialog._on_status(
        SessionStatus(
            platform=Platform.BILIBILI,
            state=LoginState.UNKNOWN,
            detail="Instagram 返回 HTTP 429，无法确认登录状态",
        )
    )

    assert accepted == [True]


def test_fetching_without_a_running_chrome_explains_the_order(login_dialog, monkeypatch) -> None:
    class _NoSession:
        instance = None

    monkeypatch.setattr(gui, "cdp_session", lambda: _NoSession())
    worker = gui.LoginWorker(login_dialog.settings, login_dialog.adapter, gui.LoginAction.CDP_FETCH)

    with pytest.raises(ChromeBridgeError) as info:
        asyncio.run(worker._run())
    assert "打开独立 Chrome" in (info.value.detail or "")


def test_fetching_from_the_debug_chrome_stores_the_session(login_dialog, monkeypatch) -> None:
    """The fallback must end with the same kind of session file the extension writes."""

    class _Instance:
        port = 9222

    class _Session:
        instance = _Instance()

    monkeypatch.setattr(gui, "cdp_session", lambda: _Session())
    monkeypatch.setattr(
        gui,
        "cdp_fetch_cookies",
        lambda instance, domain: [
            {
                "name": "SESSDATA",
                "value": "SECRET-FROM-CHROME",
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
            }
        ],
    )

    async def _logged_in(  # noqa: ANN001 - stands in for LoginWorker._check
        self, header, *, origin: SessionOrigin | None = None
    ):
        assert header is not None and "SESSDATA" in header
        return SessionStatus(
            platform=self.adapter.platform, state=LoginState.LOGGED_IN, account="某人"
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _logged_in)

    worker = gui.LoginWorker(login_dialog.settings, login_dialog.adapter, gui.LoginAction.CDP_FETCH)
    status = asyncio.run(worker._run())

    assert status.logged_in is True
    body = session_store.session_file(login_dialog.settings, Platform.BILIBILI).read_text(
        encoding="utf-8"
    )
    assert "SECRET-FROM-CHROME" in body


def test_a_session_the_platform_rejects_is_not_left_on_disk(login_dialog, monkeypatch) -> None:
    class _Instance:
        port = 9222

    class _Session:
        instance = _Instance()

    monkeypatch.setattr(gui, "cdp_session", lambda: _Session())
    monkeypatch.setattr(
        gui,
        "cdp_fetch_cookies",
        lambda instance, domain: [
            {"name": "SESSDATA", "value": "STALE", "domain": ".bilibili.com", "path": "/"}
        ],
    )

    async def _rejected(self, header, *, origin: SessionOrigin | None = None):  # noqa: ANN001
        return SessionStatus(
            platform=self.adapter.platform, state=LoginState.EXPIRED, detail="未登录"
        )

    monkeypatch.setattr(gui.LoginWorker, "_check", _rejected)

    worker = gui.LoginWorker(login_dialog.settings, login_dialog.adapter, gui.LoginAction.CDP_FETCH)
    with pytest.raises(SessionValueError):
        asyncio.run(worker._run())

    assert not session_store.session_file(login_dialog.settings, Platform.BILIBILI).exists(), (
        "被平台否定的会话不能留在磁盘上"
    )


def test_closing_the_debug_browser_is_reported(login_dialog, monkeypatch) -> None:
    class _Closable:
        def close(self) -> bool:
            return True

    monkeypatch.setattr(gui, "cdp_session", lambda: _Closable())
    login_dialog._cdp_close()
    assert "已关闭" in login_dialog.status_label.text()

    class _Nothing:
        def close(self) -> bool:
            return False

    monkeypatch.setattr(gui, "cdp_session", lambda: _Nothing())
    login_dialog._cdp_close()
    assert "没有在运行" in login_dialog.status_label.text()


def _free_port() -> int:
    """An ephemeral port nothing is listening on.

    Same trick as ``test_local_bridge``'s fixture: production pins the bridge to
    8765 because the extension has to know it, so a test has to be handed a port
    of its own - otherwise a Video Downloader already running on this machine
    holds 8765 and fails a test that has nothing to do with it.
    """

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_login_dialog_starts_the_loopback_fallback_when_shown(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """The fallback only listens while the window is open - no idle socket."""

    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)
    dialog = gui.LoginDialog(
        settings, registry.get(Platform.BILIBILI), bridge_port=_free_port()
    )
    qtbot.addWidget(dialog)

    assert dialog.bridge.running is False, "构造对话框不应开监听端口"

    dialog.show()
    qtbot.waitExposed(dialog)
    assert dialog.bridge.running is True

    dialog.close()
    assert dialog.bridge.running is False, "关闭后必须释放端口"


def test_the_production_bridge_port_is_still_the_fixed_one(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """The override above must not become a way to move the real port."""

    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)

    assert dialog.bridge.port == gui.BRIDGE_DEFAULT_PORT == 8765


def test_a_busy_bridge_port_does_not_break_the_dialog(
    qtbot, settings: Settings, registry: PlatformRegistry, monkeypatch
) -> None:
    """Native messaging is still the primary route, so this is not fatal."""

    monkeypatch.setattr(gui.webbrowser, "open", lambda *a, **k: True)

    def _busy(self) -> int:  # noqa: ANN001 - stands in for LocalBridge.start
        raise OSError("port in use")

    monkeypatch.setattr(gui.LocalBridge, "start", _busy)

    dialog = gui.LoginDialog(settings, registry.get(Platform.BILIBILI))
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.bridge.running is False
    assert dialog._bridge_error == "OSError"
    dialog.close()


def test_the_login_dialog_reports_the_bridge_in_its_hint(login_dialog, monkeypatch) -> None:
    monkeypatch.setattr(gui, "bridge_status", lambda _s: _bridge_status())

    class _Running:
        running = True
        port = 8765

        def stop(self) -> None:
            self.running = False

    login_dialog.bridge = _Running()
    login_dialog._refresh_chrome_box()

    assert "127.0.0.1:8765" in login_dialog.chrome_send_hint.text()
