"""Writes the ``<title>.info.json`` sidecar next to each video."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.constants import DEFAULT_METADATA_SUFFIX
from core.models import DownloadPlan, MediaStream, VideoInfo

logger = logging.getLogger(__name__)

#: Header names that must never reach a metadata file.
_SECRET_HEADERS = {"cookie", "authorization", "proxy-authorization", "set-cookie"}


def _stream_dump(stream: MediaStream | None) -> dict[str, Any] | None:
    if stream is None:
        return None
    payload = stream.model_dump(mode="json")
    payload["headers"] = {
        name: value
        for name, value in (stream.headers or {}).items()
        if name.lower() not in _SECRET_HEADERS
    }
    return payload


def metadata_path_for(video_path: Path) -> Path:
    return video_path.with_suffix(DEFAULT_METADATA_SUFFIX)


def build_metadata_payload(
    info: VideoInfo,
    plan: DownloadPlan,
    video_path: Path,
    *,
    file_size: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "platform": info.platform.value,
        "video_id": info.video_id,
        "url": info.url,
        "webpage_url": info.webpage_url or info.url,
        "title": info.title,
        "author": info.author,
        "author_id": info.author_id,
        "description": info.description,
        "duration_seconds": info.duration,
        "publish_time": info.publish_time.isoformat() if info.publish_time else None,
        "thumbnail_url": info.thumbnail_url,
        "download": {
            "file": video_path.name,
            "file_size": file_size,
            "quality_label": plan.quality_label,
            "container": plan.container,
            "merged": plan.merge,
            # Request headers are deliberately dropped: when browser cookies are
            # in use they can carry Cookie/Authorization values, and a sidecar
            # file must never become a credential store.
            "video_stream": _stream_dump(plan.video),
            "audio_stream": _stream_dump(plan.audio),
        },
        "extra": dict(info.extra),
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    if extra:
        payload["extra"].update(extra)
    return payload


def write_metadata(payload: dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug("写入元数据 %s", destination)
    return destination
