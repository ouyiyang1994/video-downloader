"""End-to-end download orchestration.

The service knows nothing about individual platforms: it asks the registry for
an adapter, uses the unified interface, and drives the shared download engine.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from config.settings import Settings
from core.database import DownloadDatabase
from core.downloader import HttpDownloader, ProgressCallback
from core.exceptions import (
    DownloadCancelled,
    IntegrityError,
    NotDownloadableError,
    VideoDownloaderError,
)
from core.integrity import looks_like_media
from core.merger import Merger
from core.models import (
    DownloadPlan,
    DownloadRecord,
    DownloadResult,
    DownloadStatus,
    VideoInfo,
)
from core.naming import build_output_directory, sanitize_filename
from core.registry import PlatformRegistry
from storage import metadata as metadata_store

logger = logging.getLogger(__name__)

_DURATION_TOLERANCE = 0.05


class DownloadService:
    """Coordinates metadata lookup, stream download, merge and bookkeeping."""

    def __init__(
        self,
        settings: Settings,
        registry: PlatformRegistry,
        client: httpx.AsyncClient,
        database: DownloadDatabase,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.client = client
        self.database = database
        self.merger = Merger(settings)

    async def download(
        self,
        url: str,
        *,
        quality: str = "best",
        output_dir: Path | None = None,
        force: bool = False,
        progress: dict[str, ProgressCallback] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DownloadResult:
        started = time.monotonic()
        adapter = self.registry.resolve(url)
        logger.info("识别平台：%s", adapter.describe())
        # Use the adapter's own client so that streams follow the same proxy
        # decision as that platform's metadata calls.
        downloader = HttpDownloader(self.settings, adapter.client, cancel_event)

        info = await adapter.fetch_info(url)
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("下载已取消")
        adapter.check_downloadable(info)
        logger.info("获取到视频信息：%s（%s）", info.title, info.author or "未知作者")

        base_dir = output_dir or self.settings.output_dir
        base_dir = self.settings.resolve_path(base_dir)

        if self.settings.skip_existing and not force:
            record = self.database.get(info.platform, info.video_id)
            if record and record.status is DownloadStatus.COMPLETED and record.file_path:
                if Path(record.file_path).exists():
                    logger.info("数据库中已存在且文件仍在，跳过：%s", record.file_path)
                    return DownloadResult(
                        status=DownloadStatus.SKIPPED,
                        platform=info.platform,
                        video_id=info.video_id,
                        title=info.title,
                        video_path=Path(record.file_path),
                        quality_label=record.quality_label or "unknown",
                        resolution=record.resolution,
                        skipped_reason="已下载（数据库记录命中）",
                    )
                logger.warning("数据库记录存在但文件缺失，将重新下载：%s", record.file_path)

        plan = adapter.select_streams(info, quality)
        target_dir = build_output_directory(
            base_dir,
            platform=info.platform,
            author=info.author,
            publish_time=info.publish_time,
            template=self.settings.dir_template,
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        self.database.upsert(
            DownloadRecord(
                platform=info.platform,
                video_id=info.video_id,
                url=info.url,
                author=info.author,
                title=info.title,
                publish_time=info.publish_time,
                status=DownloadStatus.DOWNLOADING,
                quality_label=plan.quality_label,
            )
        )

        try:
            video_path, stream_bytes = await self._fetch_streams(
                info, plan, target_dir, progress, downloader
            )
            final_path = await self._finalize(info, plan, video_path, target_dir)
            await self._verify(final_path, info)
            size = final_path.stat().st_size

            payload = metadata_store.build_metadata_payload(info, plan, final_path, file_size=size)
            meta_path = metadata_store.write_metadata(
                payload, metadata_store.metadata_path_for(final_path)
            )

            self.database.upsert(
                DownloadRecord(
                    platform=info.platform,
                    video_id=info.video_id,
                    url=info.url,
                    author=info.author,
                    title=info.title,
                    publish_time=info.publish_time,
                    file_path=str(final_path),
                    file_size=size,
                    resolution=_resolution(plan),
                    quality_label=plan.quality_label,
                    status=DownloadStatus.COMPLETED,
                    downloaded_at=datetime.now(UTC),
                )
            )
            logger.info("完成：%s（%.1f MB）", final_path, size / 1024 / 1024)
            return DownloadResult(
                status=DownloadStatus.COMPLETED,
                platform=info.platform,
                video_id=info.video_id,
                title=info.title,
                video_path=final_path,
                metadata_path=meta_path,
                bytes_downloaded=stream_bytes,
                elapsed_seconds=time.monotonic() - started,
                quality_label=plan.quality_label,
                resolution=_resolution(plan),
            )
        except DownloadCancelled:
            logger.info("下载已取消：%s", info.title)
            self.database.mark_cancelled(info.platform, info.video_id)
            raise
        except VideoDownloaderError as exc:
            logger.error("下载失败：%s（%s）", info.title, exc)
            self.database.mark_failed(info.platform, info.video_id, str(exc))
            raise

    # -- internals ----------------------------------------------------------
    async def _fetch_streams(
        self,
        info: VideoInfo,
        plan: DownloadPlan,
        target_dir: Path,
        progress: dict[str, ProgressCallback] | None,
        downloader: HttpDownloader,
    ) -> tuple[Path, int]:
        stem = sanitize_filename(info.title, max_length=self.settings.max_filename_length)
        jobs: list[tuple[str, Path]] = []
        if plan.video is not None:
            extension = plan.video.ext or "mp4"
            name = f"{stem}.video.{extension}" if plan.merge else f"{stem}.{extension}"
            jobs.append(("video", target_dir / name))
        if plan.audio is not None:
            extension = plan.audio.ext or "m4a"
            name = f"{stem}.audio.{extension}" if plan.merge else f"{stem}.{extension}"
            jobs.append(("audio", target_dir / name))

        streams = [s for s in (plan.video, plan.audio) if s is not None]
        if not streams:
            raise NotDownloadableError("没有可下载的媒体流")

        pairs = [(stream, path) for stream, (_, path) in zip(streams, jobs, strict=True)]
        outcomes = await downloader.download_many(pairs, progress=progress)
        total_bytes = sum(outcome.bytes_downloaded for outcome in outcomes)
        return outcomes[0].path, total_bytes

    async def _finalize(
        self,
        info: VideoInfo,
        plan: DownloadPlan,
        first_path: Path,
        target_dir: Path,
    ) -> Path:
        if not plan.merge:
            return first_path

        if plan.video is None or plan.audio is None:
            raise NotDownloadableError("合并计划缺少视频或音频流")

        stem = sanitize_filename(info.title, max_length=self.settings.max_filename_length)
        video_path = target_dir / f"{stem}.video.{plan.video.ext or 'mp4'}"
        audio_path = target_dir / f"{stem}.audio.{plan.audio.ext or 'm4a'}"
        merged = target_dir / f"{stem}.{plan.container or 'mp4'}"

        logger.info("使用 ffmpeg 合并音视频…")
        await self.merger.merge(video_path, audio_path, merged, container=plan.container)
        for stale in (video_path, audio_path):
            try:
                stale.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - filesystem dependent
                logger.debug("无法删除临时流文件 %s", stale)
        return merged

    async def _verify(self, path: Path, info: VideoInfo) -> None:
        if not path.exists() or path.stat().st_size == 0:
            raise IntegrityError("输出文件不存在或为空", detail=str(path))
        if not looks_like_media(path):
            raise IntegrityError("输出文件不像是有效的媒体文件", detail=str(path))

        if info.duration and self.merger.available:
            actual = await self.merger.duration(path)
            if actual and info.duration > 0:
                drift = abs(actual - info.duration) / info.duration
                if drift > _DURATION_TOLERANCE:
                    logger.warning(
                        "时长校验偏差 %.1f%%（期望 %.1fs，实际 %.1fs）",
                        drift * 100,
                        info.duration,
                        actual,
                    )


def _resolution(plan: DownloadPlan) -> str | None:
    video = plan.video
    if video and video.width and video.height:
        return f"{video.width}x{video.height}"
    if video and video.height:
        return f"{video.height}p"
    return None
