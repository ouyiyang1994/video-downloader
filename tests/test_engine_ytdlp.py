"""yt-dlp format filtering.

Regression guard: YouTube returns HLS manifests whose ``ext`` is still ``mp4``,
which previously slipped through and got "downloaded" as a playlist file.
"""

from __future__ import annotations

from core.engine_ytdlp import build_media_streams, is_direct_download

HLS_MANIFEST = {
    "format_id": "230",
    "ext": "mp4",
    "protocol": "m3u8_native",
    "vcodec": "avc1.4D401E",
    "acodec": "none",
    "height": 360,
    "tbr": 610.6,
    "url": "https://manifest.googlevideo.com/api/manifest/hls_playlist/expire/x/id/y",
}
DIRECT_VIDEO = {
    "format_id": "134",
    "ext": "mp4",
    "protocol": "https",
    "vcodec": "avc1.4D401E",
    "acodec": "none",
    "height": 360,
    "tbr": 315.0,
    "url": "https://rr2---sn-a5m7lnld.googlevideo.com/videoplayback?expire=x",
}
DIRECT_AUDIO = {
    "format_id": "140",
    "ext": "m4a",
    "protocol": "https",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "tbr": 129.5,
    "url": "https://rr2---sn-a5m7lnld.googlevideo.com/videoplayback?expire=y",
}
STORYBOARD = {
    "format_id": "sb0",
    "ext": "mhtml",
    "protocol": "mhtml",
    "vcodec": "none",
    "acodec": "none",
    "height": 180,
    "url": "https://i.ytimg.com/sb/dQw4w9WgXcQ/storyboard3_L3/M$M.jpg",
}
MUXED = {
    "format_id": "18",
    "ext": "mp4",
    "protocol": "https",
    "vcodec": "avc1.42001E",
    "acodec": "mp4a.40.2",
    "height": 360,
    "tbr": 500.0,
    "url": "https://rr2---sn-a5m7lnld.googlevideo.com/videoplayback?expire=z",
}


def test_manifest_protocol_is_not_direct() -> None:
    assert is_direct_download(HLS_MANIFEST) is False
    assert is_direct_download(DIRECT_VIDEO) is True


def test_manifest_url_without_protocol_is_rejected() -> None:
    assert is_direct_download({"url": "https://x.example/stream.m3u8"}) is False
    assert is_direct_download({"url": "https://x.example/clip.mp4"}) is True


def test_manifests_and_storyboards_are_dropped() -> None:
    streams = build_media_streams([HLS_MANIFEST, DIRECT_VIDEO, DIRECT_AUDIO, STORYBOARD])
    assert len(streams) == 2
    assert [s.is_audio for s in streams] == [False, True]
    assert streams[0].url == DIRECT_VIDEO["url"]


def test_muxed_is_used_when_no_separate_video_exists() -> None:
    streams = build_media_streams([MUXED, DIRECT_AUDIO])
    assert len(streams) == 1
    assert streams[0].is_audio is False
    assert streams[0].url == MUXED["url"]


def test_muxed_is_ignored_when_separate_streams_exist() -> None:
    streams = build_media_streams([MUXED, DIRECT_VIDEO, DIRECT_AUDIO])
    assert len(streams) == 2
    assert all(s.url != MUXED["url"] for s in streams)


def test_empty_formats() -> None:
    assert build_media_streams([]) == []
