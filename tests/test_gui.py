"""GUI layer tests.

Everything runs headless through the offscreen Qt platform (set in
``conftest.py``), so no window is ever shown.
"""

from __future__ import annotations

import asyncio
import os
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
)
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
    message = gui.friendly_error(AuthRequiredError("login required"), platform=Platform.INSTAGRAM)
    assert message == "Instagram 登录状态已失效，请重新更新 Cookie。"


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
