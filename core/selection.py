"""Shared "pick the best stream" logic used by engine-backed adapters."""

from __future__ import annotations

import logging

from config.constants import QUALITY_AUDIO_ONLY, QUALITY_PRESETS
from core.exceptions import NotDownloadableError
from core.models import DownloadPlan, MediaStream, VideoInfo

logger = logging.getLogger(__name__)

#: Within the same resolution, prefer streams whose container can be muxed with
#: ``-c copy`` into the target container (H.264/AAC into mp4, and so on).
_PREFERRED_VIDEO_EXTS: dict[str, set[str]] = {
    "mp4": {"mp4"},
    "m4a": {"mp4"},
    "mkv": {"mkv", "webm", "mp4"},
}
_PREFERRED_AUDIO_EXTS: dict[str, set[str]] = {
    "mp4": {"m4a"},
    "m4a": {"m4a"},
    "mkv": {"m4a", "webm"},
}


def _ext_score(stream: MediaStream, preferred: set[str]) -> int:
    return 1 if (stream.ext or "").lower() in preferred else 0


def _quality_rank(stream: MediaStream, preferred: set[str]) -> tuple[int, int, int]:
    """Order streams by the resolution a user would name, then by preference."""

    return (
        stream.display_height or 0,
        _ext_score(stream, preferred),
        stream.bandwidth or 0,
    )


def _closest_above(videos: list[MediaStream], target: int, preferred: set[str]) -> MediaStream:
    """The lowest quality above ``target`` (best of the tied ones).

    Only used when nothing is at or below the target: taking the closest stream
    above it is what the user asked for, and it can never be worse than asking
    for the minimum.
    """

    above = [stream for stream in videos if (stream.display_height or 0) > target]
    if not above:
        return videos[0]
    lowest = min(stream.display_height or 0 for stream in above)
    tied = [stream for stream in above if (stream.display_height or 0) == lowest]
    return max(tied, key=lambda stream: (_ext_score(stream, preferred), stream.bandwidth or 0))


def _pick_audio(info: VideoInfo, preferred: set[str]) -> MediaStream | None:
    audios = [stream for stream in info.streams if stream.is_audio]
    if not audios:
        return None
    return max(audios, key=lambda stream: (_ext_score(stream, preferred), stream.bandwidth or 0))


def select_streams(info: VideoInfo, quality: str, *, container: str = "mp4") -> DownloadPlan:
    """Choose a video stream at or below ``quality`` plus the best audio."""

    preferred_audio = _PREFERRED_AUDIO_EXTS.get(container, {"m4a"})

    if quality == QUALITY_AUDIO_ONLY:
        audio = _pick_audio(info, preferred_audio)
        if audio is None:
            raise NotDownloadableError("该视频没有独立的音频流")
        return DownloadPlan(audio=audio, merge=False, container="m4a", quality_label="audio")

    target = QUALITY_PRESETS.get(quality)
    preferred_video = _PREFERRED_VIDEO_EXTS.get(container, {"mp4"})
    videos = sorted(
        (stream for stream in info.streams if not stream.is_audio),
        key=lambda stream: _quality_rank(stream, preferred_video),
        reverse=True,
    )
    if not videos:
        raise NotDownloadableError(
            "该视频没有可用的视频流",
            detail=f"platform={info.platform.value} video={info.video_id}",
        )

    if target is None:
        chosen = videos[0]
    else:
        affordable = [stream for stream in videos if (stream.display_height or 0) <= target]
        if affordable:
            chosen = affordable[0]
        else:
            # Everything is above the target: take the closest one instead of
            # dropping to the lowest available quality.
            chosen = _closest_above(videos, target, preferred_video)
            logger.warning(
                "该视频没有 %dp 及以下画质，改用最接近的 %dp（%s）",
                target,
                chosen.display_height or 0,
                chosen.quality_label,
            )

    audio = _pick_audio(info, preferred_audio)
    return DownloadPlan(
        video=chosen,
        audio=audio,
        merge=audio is not None,
        container=container,
        quality_label=chosen.quality_label,
    )
