"""Quality selection rules."""

from __future__ import annotations

import pytest

from core import selection
from core.exceptions import NotDownloadableError
from core.models import MediaStream, Platform, VideoInfo


def _info(*, include_audio: bool = True) -> VideoInfo:
    streams = [
        MediaStream(url="v360", quality_label="360p", height=360, bandwidth=300),
        MediaStream(url="v720", quality_label="720p", height=720, bandwidth=900),
        MediaStream(url="v1080", quality_label="1080p", height=1080, bandwidth=2000),
    ]
    if include_audio:
        streams.append(
            MediaStream(url="a128", is_audio=True, quality_label="audio", bandwidth=128_000)
        )
    return VideoInfo(
        platform=Platform.YOUTUBE,
        video_id="abc",
        url="https://youtu.be/abc",
        title="t",
        streams=streams,
    )


def test_best_picks_highest_video_and_audio() -> None:
    plan = selection.select_streams(_info(), "best")
    assert plan.video is not None and plan.video.height == 1080
    assert plan.audio is not None
    assert plan.merge is True


def test_exact_height_is_used() -> None:
    plan = selection.select_streams(_info(), "720p")
    assert plan.video is not None and plan.video.height == 720


def test_downgrades_to_nearest_lower_quality() -> None:
    plan = selection.select_streams(_info(), "480p")
    assert plan.video is not None and plan.video.height == 360


def test_falls_back_to_lowest_when_target_is_unreachable() -> None:
    plan = selection.select_streams(_info(), "2160p")
    assert plan.video is not None and plan.video.height == 1080


def test_audio_only_plan() -> None:
    plan = selection.select_streams(_info(), "audio")
    assert plan.video is None
    assert plan.audio is not None
    assert plan.merge is False
    assert plan.container == "m4a"


def test_audio_only_requires_audio_stream() -> None:
    with pytest.raises(NotDownloadableError):
        selection.select_streams(_info(include_audio=False), "audio")


def test_no_video_streams_raises() -> None:
    info = _info(include_audio=True)
    info.streams = [s for s in info.streams if s.is_audio]
    with pytest.raises(NotDownloadableError):
        selection.select_streams(info, "best")


def _mixed_container_info() -> VideoInfo:
    """720p exists as both webm (faster) and mp4; audio as webm and m4a."""

    return VideoInfo(
        platform=Platform.YOUTUBE,
        video_id="abc",
        url="https://youtu.be/abc",
        title="t",
        streams=[
            MediaStream(
                url="webm720",
                quality_label="720p",
                height=720,
                bandwidth=900_000,
                ext="webm",
            ),
            MediaStream(
                url="mp4-720",
                quality_label="720p",
                height=720,
                bandwidth=500_000,
                ext="mp4",
            ),
            MediaStream(
                url="audio-opus",
                is_audio=True,
                quality_label="audio",
                bandwidth=160_000,
                ext="webm",
            ),
            MediaStream(
                url="audio-aac",
                is_audio=True,
                quality_label="audio",
                bandwidth=128_000,
                ext="m4a",
            ),
        ],
    )


def test_prefers_mp4_video_at_the_same_resolution() -> None:
    plan = selection.select_streams(_mixed_container_info(), "720p")
    assert plan.video is not None
    # mp4 wins even though the webm variant reports a higher bitrate, because
    # it can be muxed into mp4 with -c copy.
    assert plan.video.url == "mp4-720"


def test_prefers_m4a_audio_for_mp4_container() -> None:
    plan = selection.select_streams(_mixed_container_info(), "720p")
    assert plan.audio is not None
    assert plan.audio.url == "audio-aac"


def test_higher_resolution_still_wins_over_container_preference() -> None:
    info = _mixed_container_info()
    info.streams.append(
        MediaStream(
            url="webm1080",
            quality_label="1080p",
            height=1080,
            bandwidth=1_500_000,
            ext="webm",
        )
    )
    plan = selection.select_streams(info, "best")
    assert plan.video is not None
    assert plan.video.url == "webm1080"


# --- effective (display) resolution -----------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1920, 1080, 1080),  # landscape 1080p
        (1080, 1920, 1080),  # portrait 1080p
        (3840, 2160, 2160),  # landscape 4K
        (2160, 3840, 2160),  # portrait 4K
        (720, 1280, 720),  # portrait 720p
        (852, 480, 480),  # landscape 480p
        (None, 1080, 1080),  # width unknown -> previous behaviour
        (1920, None, None),  # height unknown -> unknown
    ],
)
def test_display_height_is_the_short_side(width, height, expected) -> None:
    stream = MediaStream(url="u", width=width, height=height)
    assert stream.display_height == expected


def _portrait_info() -> VideoInfo:
    """A 720x1280 vertical clip, exactly like an Instagram Reel."""

    return VideoInfo(
        platform=Platform.INSTAGRAM,
        video_id="reel",
        url="https://www.instagram.com/reel/reel/",
        title="t",
        streams=[
            MediaStream(url="p720", height=1280, width=720, quality_label="720p"),
            MediaStream(url="p480", height=854, width=480, quality_label="480p"),
        ],
    )


def test_portrait_best_picks_the_tallest_rendition() -> None:
    plan = selection.select_streams(_portrait_info(), "best")
    assert plan.video is not None
    assert plan.video.url == "p720"
    assert plan.video.display_height == 720


def test_portrait_1080p_request_keeps_the_720p_rendition() -> None:
    """720x1280 is a 720p clip; asking for 1080p must not drop below it."""

    plan = selection.select_streams(_portrait_info(), "1080p")
    assert plan.video is not None
    assert (plan.video.width, plan.video.height) == (720, 1280)


def test_portrait_720p_request_picks_the_720p_rendition() -> None:
    plan = selection.select_streams(_portrait_info(), "720p")
    assert plan.video is not None
    assert plan.video.url == "p720"


def test_landscape_1080p_request_picks_1080p() -> None:
    info = VideoInfo(
        platform=Platform.YOUTUBE,
        video_id="v",
        url="u",
        title="t",
        streams=[
            MediaStream(url="l1080", width=1920, height=1080, quality_label="1080p"),
            MediaStream(url="l720", width=1280, height=720, quality_label="720p"),
        ],
    )
    plan = selection.select_streams(info, "1080p")
    assert plan.video is not None
    assert plan.video.url == "l1080"


# --- the "nothing at or below the target" fallback ---------------------------


def test_falls_back_to_the_closest_stream_above_the_target() -> None:
    """Nothing at or below 720p -> take the lowest rendition above it."""

    info = VideoInfo(
        platform=Platform.INSTAGRAM,
        video_id="v",
        url="u",
        title="t",
        streams=[
            MediaStream(url="2160", width=2160, height=3840),
            MediaStream(url="1440", width=1440, height=2560),
            MediaStream(url="1080", width=1080, height=1920),
        ],
    )
    plan = selection.select_streams(info, "720p")
    assert plan.video is not None
    assert plan.video.url == "1080"
    assert plan.video.display_height == 1080


def test_portrait_720p_rendition_satisfies_a_720p_request() -> None:
    """720x1280 *is* 720p, so it must win over the smaller rendition."""

    info = VideoInfo(
        platform=Platform.INSTAGRAM,
        video_id="v",
        url="u",
        title="t",
        streams=[
            MediaStream(url="1920", width=1080, height=1920),
            MediaStream(url="1280", width=720, height=1280),
            MediaStream(url="854", width=480, height=854),
        ],
    )
    assert selection.select_streams(info, "1080p").video.url == "1920"
    assert selection.select_streams(info, "720p").video.url == "1280"
    assert selection.select_streams(info, "480p").video.url == "854"


def test_single_stream_above_the_target_is_used() -> None:
    info = VideoInfo(
        platform=Platform.YOUTUBE,
        video_id="v",
        url="u",
        title="t",
        streams=[MediaStream(url="only1080", width=1920, height=1080)],
    )
    plan = selection.select_streams(info, "720p")
    assert plan.video is not None
    assert plan.video.url == "only1080"


def test_target_between_two_streams_prefers_the_one_below() -> None:
    """1080 and 480 available, 720 requested -> 480 (never above the target)."""

    info = VideoInfo(
        platform=Platform.YOUTUBE,
        video_id="v",
        url="u",
        title="t",
        streams=[
            MediaStream(url="l1080", width=1920, height=1080),
            MediaStream(url="l480", width=852, height=480),
        ],
    )
    plan = selection.select_streams(info, "720p")
    assert plan.video is not None
    assert plan.video.url == "l480"


def test_fallback_above_target_keeps_the_better_stream_of_that_rendition() -> None:
    """Two renditions at the same (above-target) resolution, different codecs."""

    info = VideoInfo(
        platform=Platform.YOUTUBE,
        video_id="v",
        url="u",
        title="t",
        streams=[
            MediaStream(url="mp4-1280", width=720, height=1280, ext="mp4", bandwidth=1_500_000),
            MediaStream(url="webm-1280", width=720, height=1280, ext="webm", bandwidth=800_000),
        ],
    )
    plan = selection.select_streams(info, "480p")
    assert plan.video is not None
    assert plan.video.url == "mp4-1280"


def test_separate_video_and_audio_are_still_merged() -> None:
    info = VideoInfo(
        platform=Platform.YOUTUBE,
        video_id="v",
        url="u",
        title="t",
        streams=[
            MediaStream(url="v1080", width=1920, height=1080),
            MediaStream(url="a", is_audio=True, ext="m4a", bandwidth=128_000),
        ],
    )
    plan = selection.select_streams(info, "best")
    assert plan.video is not None
    assert plan.audio is not None
    assert plan.merge is True
    assert plan.container == "mp4"


def test_muxed_only_plan_still_downloads_a_single_file() -> None:
    info = VideoInfo(
        platform=Platform.INSTAGRAM,
        video_id="v",
        url="u",
        title="t",
        streams=[MediaStream(url="progressive", width=720, height=1280, ext="mp4")],
    )
    plan = selection.select_streams(info, "best")
    assert plan.audio is None
    assert plan.merge is False
