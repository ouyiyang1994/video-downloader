"""Domain models shared by every layer."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Platform(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    BILIBILI = "bilibili"

    @property
    def display_name(self) -> str:
        return {
            Platform.YOUTUBE: "YouTube",
            Platform.INSTAGRAM: "Instagram",
            Platform.BILIBILI: "哔哩哔哩",
        }[self]


class DownloadStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MediaStream(BaseModel):
    """One directly downloadable stream (video-only or audio-only)."""

    model_config = ConfigDict(frozen=True)

    url: str
    is_audio: bool = False
    quality_label: str = "unknown"
    height: int | None = None
    width: int | None = None
    codec: str | None = None
    bandwidth: int | None = None
    size: int | None = None
    format_id: str | None = None
    ext: str = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "audio" if self.is_audio else "video"

    @property
    def display_height(self) -> int | None:
        """The resolution a user would name: the *short* side of the frame.

        ``1920x1080 -> 1080``, ``1080x1920 -> 1080``, ``3840x2160 -> 2160``,
        ``2160x3840 -> 2160``.  yt-dlp reports ``height`` as the real pixel
        height, so a portrait clip reports its long side and would otherwise be
        compared against the wrong preset.

        When the width is unknown the plain ``height`` is returned, which keeps
        the previous behaviour for streams without dimensions.
        """

        if self.height is None:
            return None
        if self.width is None:
            return self.height
        return min(self.width, self.height)


class DownloadPlan(BaseModel):
    """What a platform adapter decided should be fetched."""

    video: MediaStream | None = None
    audio: MediaStream | None = None
    merge: bool = False
    container: str = "mp4"
    quality_label: str = "unknown"

    @property
    def streams(self) -> list[MediaStream]:
        return [s for s in (self.video, self.audio) if s is not None]

    @property
    def audio_only(self) -> bool:
        return self.video is None and self.audio is not None


class VideoInfo(BaseModel):
    """Platform-independent video metadata."""

    platform: Platform
    video_id: str
    url: str
    title: str
    author: str | None = None
    author_id: str | None = None
    description: str | None = None
    duration: float | None = None
    publish_time: datetime | None = None
    thumbnail_url: str | None = None
    streams: list[MediaStream] = Field(default_factory=list)
    webpage_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def best_video(self) -> MediaStream | None:
        videos = [s for s in self.streams if not s.is_audio]
        if not videos:
            return None
        return max(videos, key=lambda s: (s.height or 0, s.bandwidth or 0))

    def best_audio(self) -> MediaStream | None:
        audios = [s for s in self.streams if s.is_audio]
        if not audios:
            return None
        return max(audios, key=lambda s: s.bandwidth or 0)


class DownloadResult(BaseModel):
    """Outcome of a download attempt."""

    status: DownloadStatus
    platform: Platform
    video_id: str
    title: str
    video_path: Path | None = None
    metadata_path: Path | None = None
    bytes_downloaded: int = 0
    elapsed_seconds: float = 0.0
    quality_label: str = "unknown"
    resolution: str | None = None
    skipped_reason: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in (DownloadStatus.COMPLETED, DownloadStatus.SKIPPED)


class DownloadRecord(BaseModel):
    """A row of the download history database."""

    platform: Platform
    video_id: str
    url: str
    author: str | None = None
    title: str = ""
    publish_time: datetime | None = None
    file_path: str | None = None
    file_size: int | None = None
    resolution: str | None = None
    quality_label: str | None = None
    status: DownloadStatus = DownloadStatus.PENDING
    error_message: str | None = None
    downloaded_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
