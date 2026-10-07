"""Metadata sidecar generation."""

from __future__ import annotations

import json
from pathlib import Path

from core.models import DownloadPlan, MediaStream, VideoInfo
from storage import metadata as store


def test_metadata_path_replaces_suffix(tmp_path: Path) -> None:
    assert store.metadata_path_for(tmp_path / "clip.mp4") == tmp_path / "clip.info.json"


def test_payload_contains_expected_fields(
    video_info: VideoInfo, download_plan: DownloadPlan, tmp_path: Path
) -> None:
    video_path = tmp_path / "clip.mp4"
    payload = store.build_metadata_payload(video_info, download_plan, video_path, file_size=2048)

    assert payload["platform"] == "bilibili"
    assert payload["video_id"] == video_info.video_id
    assert payload["title"] == video_info.title
    assert payload["publish_time"].startswith("2020-01-02")
    assert payload["download"]["quality_label"] == "1080P"
    assert payload["download"]["merged"] is True
    assert payload["download"]["video_stream"]["height"] == 1080
    assert payload["download"]["audio_stream"]["is_audio"] is True
    assert payload["download"]["file_size"] == 2048


def test_write_metadata_roundtrip(
    video_info: VideoInfo, download_plan: DownloadPlan, tmp_path: Path
) -> None:
    video_path = tmp_path / "clip.mp4"
    payload = store.build_metadata_payload(video_info, download_plan, video_path)
    destination = store.write_metadata(payload, store.metadata_path_for(video_path))

    assert destination.exists()
    reloaded = json.loads(destination.read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == 1
    assert reloaded["title"] == video_info.title


def test_credentials_in_stream_headers_are_redacted(
    video_info: VideoInfo, download_plan: DownloadPlan, tmp_path: Path
) -> None:
    """A sidecar must never become a cookie store."""

    poisoned = MediaStream(
        url="https://cdn.example.test/v.mp4",
        quality_label="1080p",
        height=1080,
        headers={
            "Cookie": "SESSDATA=super-secret-value",
            "Authorization": "Bearer super-secret-token",
            "Referer": "https://www.bilibili.com/",
        },
    )
    plan = download_plan.model_copy(update={"video": poisoned})
    payload = store.build_metadata_payload(video_info, plan, tmp_path / "clip.mp4")

    dumped = json.dumps(payload, ensure_ascii=False)
    assert "super-secret-value" not in dumped
    assert "super-secret-token" not in dumped
    assert payload["download"]["video_stream"]["headers"] == {
        "Referer": "https://www.bilibili.com/"
    }
