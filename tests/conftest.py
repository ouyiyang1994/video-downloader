"""Shared fixtures."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Qt must be told to run headless *before* PySide6 is imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config import settings as settings_module
from config.settings import Settings
from core.models import DownloadPlan, MediaStream, Platform, VideoInfo


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's real ``.env`` and directories."""

    return Settings(
        _env_file=None,
        output_dir=tmp_path / "downloads",
        database_path=tmp_path / "downloads" / "test.db",
        log_dir=tmp_path / "logs",
        # Keep the sign-in flow's session files inside tmp_path: a test must
        # never read or overwrite the developer's real secrets/ folder.
        session_dir=tmp_path / "secrets",
        max_retries=2,
        request_timeout=5.0,
    )


@pytest.fixture
def video_info() -> VideoInfo:
    return VideoInfo(
        platform=Platform.BILIBILI,
        video_id="BV1GJ411x7h7",
        url="https://www.bilibili.com/video/BV1GJ411x7h7",
        title="测试视频 / Test: Video? *",
        author="测试UP主",
        author_id="12345",
        duration=213.0,
        publish_time=datetime(2020, 1, 2, tzinfo=UTC),
        thumbnail_url="https://i0.hdslb.com/bfs/archive/demo.jpg",
        streams=[
            MediaStream(
                url="https://cdn.example.test/video-480.m4s",
                quality_label="480P",
                height=480,
                width=852,
                bandwidth=500_000,
                format_id="dash-video-32",
            ),
            MediaStream(
                url="https://cdn.example.test/video-1080.m4s",
                quality_label="1080P",
                height=1080,
                width=1920,
                bandwidth=2_000_000,
                format_id="dash-video-80",
            ),
            MediaStream(
                url="https://cdn.example.test/audio.m4s",
                is_audio=True,
                quality_label="audio",
                bandwidth=128_000,
                format_id="dash-audio-30280",
                ext="m4a",
            ),
        ],
    )


@pytest.fixture
def download_plan(video_info: VideoInfo) -> DownloadPlan:
    return DownloadPlan(
        video=video_info.streams[1],
        audio=video_info.streams[2],
        merge=True,
        container="mp4",
        quality_label="1080P",
    )


@pytest.fixture
def fake_media_bytes() -> bytes:
    """Bytes that pass the cheap "is this media?" sniff test."""

    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096


@pytest.fixture
def clean_proxy_env():
    """Isolate the process environment around ``apply_proxy_environment``.

    That function writes real proxy variables into ``os.environ`` by design, so
    a test exercising it must not leak them into the rest of the session.
    """

    original = {key: os.environ.get(key) for key in settings_module.PROXY_ENV_KEYS}
    settings_module._exported_proxy_keys.clear()
    for key in settings_module.PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    settings_module._exported_proxy_keys.clear()
    for key in settings_module.PROXY_ENV_KEYS:
        os.environ.pop(key, None)
        if original[key] is not None:
            os.environ[key] = original[key]
