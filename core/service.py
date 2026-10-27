"""End-to-end download orchestration.

The service knows nothing about individual platforms: it asks the registry for
an adapter, uses the unified interface, and drives the shared download engine.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from config.settings import Settings
from core.database import DownloadDatabase
from core.downloader import (
    DownloadOutcome,
    HttpDownloader,
    ProgressCallback,
    completed_outcomes,
)
from core.exceptions import (
    DownloadCancelled,
    IntegrityError,
    NotDownloadableError,
    TargetFileConflictError,
    TargetFileError,
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
from core.naming import (
    build_output_directory,
    claim_output_path,
    release_output_path,
    sanitize_filename,
)
from core.registry import PlatformRegistry
from storage import metadata as metadata_store

if TYPE_CHECKING:
    from core.interfaces import PlatformAdapter

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
            record = self._completed_record(adapter, info)
            if record is not None and record.file_path:
                file_path = Path(record.file_path)
                logger.info("数据库中已存在且文件仍在，跳过：%s", file_path)
                return DownloadResult(
                    status=DownloadStatus.SKIPPED,
                    platform=info.platform,
                    video_id=info.video_id,
                    title=info.title,
                    video_path=file_path,
                    quality_label=record.quality_label or "unknown",
                    resolution=record.resolution,
                    skipped_reason="已下载（数据库记录命中）",
                )

        plan = adapter.select_streams(info, quality)
        target_dir = build_output_directory(
            base_dir,
            platform=info.platform,
            author=info.author,
            publish_time=info.publish_time,
            template=self.settings.dir_template,
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        # Claim the destination *before* any bytes are fetched. Two different
        # videos can sanitise to the same file name (same author, same title,
        # same day), and two queue workers can run at the same time - without
        # this the second ``os.replace`` silently replaced the first download
        # while both database rows kept pointing at that one file.
        stem = sanitize_filename(info.title, max_length=self.settings.max_filename_length)
        previous = self.database.get(info.platform, info.video_id)
        keep = Path(previous.file_path) if previous is not None and previous.file_path else None
        claimed = claim_output_path(target_dir, stem, _output_suffix(plan), keep=keep)
        # Whether this name already held a file decides who may delete it, and
        # the old file has to survive a failed replacement: both the fact and
        # its identity are captured before a single byte is fetched.
        claimed_existed = claimed.exists()
        original_signature = _signature(claimed) if claimed_existed else None
        backup: Path | None = None
        produced: Path | None = None
        produced_signature: tuple[int, int, int] | None = None
        verified = False

        try:
            # Inside the try on purpose: a database failure here must still
            # release the name claimed above.
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
            if claimed_existed:
                backup = self._protect_previous(claimed, in_place=plan.merge)
            video_path, stream_bytes = await self._fetch_streams(
                plan, target_dir, progress, downloader, stem=claimed.stem
            )
            produced = await self._finalize(
                plan,
                video_path,
                target_dir,
                stem=claimed.stem,
                expected=(claimed_existed, original_signature),
                backup=backup,
            )
            produced_signature = _signature(produced)
            await self._verify(produced, info)
            verified = True
            self._discard_backup(backup)
            final_path = produced
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
            # A conflict means the name belongs to somebody else now; nothing of
            # ours may be removed from it (the downloader already dropped its
            # own ``.part``). The old file is only put back when the name is
            # free again - never over the newcomer.
            if not verified:
                if isinstance(exc, TargetFileConflictError):
                    self._restore_backup_if_name_free(produced or claimed, backup)
                elif produced is not None or backup is not None:
                    self._recover_replacement(
                        target=produced or claimed,
                        original_signature=original_signature,
                        product_signature=produced_signature,
                        backup=backup,
                        existed_before=claimed_existed,
                        reason=exc.message,
                    )
            raise
        except OSError as exc:
            # A plain filesystem error after the product was written - a full
            # disk while saving metadata, a file that vanished before it could
            # be measured. Record it like any other failure so the row does not
            # sit at DOWNLOADING forever, and never report it as a success.
            message = f"写入输出文件失败：{exc}"
            logger.error("下载失败：%s（%s）", info.title, message)
            self.database.mark_failed(info.platform, info.video_id, message)
            if not verified and (produced is not None or backup is not None):
                self._recover_replacement(
                    target=produced or claimed,
                    original_signature=original_signature,
                    product_signature=produced_signature,
                    backup=backup,
                    existed_before=claimed_existed,
                    reason=message,
                )
            failure = VideoDownloaderError(message, detail=type(exc).__name__)
            if exc.errno in PERMANENT_IO_ERRNOS:
                # A full disk or a read-only folder will still be full and
                # read-only on the next attempt; re-downloading the file to
                # find that out again is pure waste.
                failure.retryable = False
            raise failure from exc
        finally:
            # Once the file exists the claim is redundant; on failure the name
            # must become available again so a retry reuses it.
            release_output_path(claimed)

    # -- internals ----------------------------------------------------------
    def _discard_rejected_product(self, produced: Path, *, existed_before: bool) -> None:
        """Remove a product that failed verification, unless the name was taken.

        The claim promises the name was free - or that it held this video's own
        previous file, which a forced re-download is allowed to replace. Only
        the first case may be cleaned up: deleting a path that existed before
        the task would destroy something the task did not create. Either way the
        caller still raises the verification error, so the reason stays visible.
        """

        if existed_before:
            logger.warning(
                "验证未通过，但目标文件在任务开始前已存在，保留不删：%s", produced.name
            )
            return
        try:
            produced.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("验证未通过，且无法删除无效产物 %s：%s", produced.name, exc)
            return
        logger.info("验证未通过，已删除无效产物：%s", produced.name)

    def _protect_previous(self, destination: Path, *, in_place: bool) -> Path:
        """Keep a recoverable copy of the file a forced re-download replaces.

        The original stays where it is; only a copy is parked beside it, so a
        failed download can never leave the user without their file. A hard link
        is enough when the replacement arrives as a rename (the new bytes land
        in a ``.part`` file first), but the merge step lets ffmpeg write into
        the target directly - truncating an inode a link shares - so that case
        gets a real copy.
        """

        backup = _backup_path(destination)
        if not in_place:
            try:
                os.link(destination, backup)
                return backup
            except OSError:
                logger.debug("无法为 %s 建立硬链接，改为复制备份", destination.name)
        try:
            with destination.open("rb") as source, backup.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
        except OSError as exc:
            with contextlib.suppress(OSError):
                backup.unlink(missing_ok=True)
            raise TargetFileError(
                f"无法备份原有文件，已停止下载以免覆盖：{destination.name}",
                detail=str(exc),
            ) from exc
        return backup

    def _before_recover(self, target: Path) -> None:
        """Overridable pause before a failed download is rolled back.

        Production does nothing here. Tests subclass the service and override
        this method to replace the target at the exact moment the recovery
        starts, which is otherwise only reachable by winning a microsecond race.
        """

    def _put_back(self, source: Path, target: Path, *, label: str) -> bool:
        """Restore ``source`` at ``target`` only if that name is still free.

        Restoring uses the same atomic create-if-absent as publishing: a file
        that appeared at the name belongs to somebody else and is never
        replaced. When it fails the source stays where it is and the log says
        exactly where that is.
        """

        try:
            os.link(source, target)
        except FileExistsError:
            logger.warning(
                "目标名仍被其他程序占用，未恢复%s；原文件保留在 %s（可手动改回 %s）",
                label,
                source,
                target.name,
            )
            return False
        except OSError as exc:
            logger.warning("无法恢复%s，原文件保留在 %s：%s", label, source, exc)
            return False
        try:
            source.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("已恢复%s，但无法删除临时副本 %s：%s", label, source, exc)
            return True
        logger.warning("%s已恢复：%s", label, target.name)
        return True

    def _restore_backup_if_name_free(self, target: Path, backup: Path | None) -> None:
        """Put the old file back only when its name is free again."""

        if backup is None or not backup.exists():
            return
        self._put_back(backup, target, label="旧文件")

    def _discard_backup(self, backup: Path | None) -> None:
        if backup is None:
            return
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("无法删除替换前的备份 %s：%s", backup, exc)

    def _recover_replacement(
        self,
        *,
        target: Path,
        original_signature: tuple[int, int, int] | None,
        product_signature: tuple[int, int, int] | None,
        backup: Path | None,
        existed_before: bool,
        reason: str,
    ) -> None:
        """Put the original back after a failed replacement, when that is safe.

        Nothing is overwritten unless it is provably either untouched or our own
        rejected product: a file that changed identity while the task ran
        belongs to somebody else, and the backup is kept next to it instead.
        The task stays FAILED either way - the old file is *not* a download this
        run completed, and the log says so.
        """

        if backup is None:
            self._discard_rejected_product(target, existed_before=existed_before)
            return

        current = _signature(target)
        if current == original_signature:
            # The original never moved; the backup is redundant.
            self._discard_backup(backup)
            return
        if product_signature is not None and current != product_signature:
            logger.warning(
                "目标文件在校验期间被其他程序替换，未恢复旧文件；备份保留在 %s", backup
            )
            return
        if product_signature is None and current is not None:
            logger.warning(
                "无法确认目标文件 %s 的归属，未恢复旧文件；备份保留在 %s", target.name, backup
            )
            return
        self._before_recover(target)

        # Windows has no "remove only if unchanged", so the rejected product
        # has to be taken off the name before the backup can be linked in. The
        # move is re-checked immediately afterwards: if what came off the name
        # is not our product, a third party's file slipped in between the check
        # and the move, and it is put straight back where it was.
        discarded: Path | None = None
        if current is not None:
            discarded = _swap_path(target)
            try:
                os.replace(target, discarded)
            except OSError as exc:
                logger.warning(
                    "无法移开无效产物 %s，未恢复旧文件；备份保留在 %s：%s",
                    target.name,
                    backup,
                    exc,
                )
                return
            if _signature(discarded) != product_signature:
                self._put_back(discarded, target, label="第三方文件")
                logger.warning(
                    "恢复期间目标被第三方替换，未恢复旧文件；备份保留在 %s", backup
                )
                return
        if not self._put_back(backup, target, label="替换前的文件"):
            if discarded is not None:
                logger.warning("无效产物保留在 %s，备份保留在 %s", discarded, backup)
            return
        if discarded is not None:
            try:
                discarded.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem dependent
                logger.warning("无法删除无效产物 %s：%s", discarded, exc)
        logger.warning(
            "下载失败（%s），已恢复替换前的文件：%s；任务保持失败状态", reason, target.name
        )

    def _completed_record(
        self, adapter: PlatformAdapter, info: VideoInfo
    ) -> DownloadRecord | None:
        """The completed row for this download, if there is one.

        The current key is tried first and behaves exactly as it always has.
        Only when it has no row are the identities earlier versions used
        consulted, and then only when the adapter confirms the stored URL is the
        same download - so the compatibility path can spare a redundant
        re-download but can never hide one that has not happened.
        """

        record = self.database.get(info.platform, info.video_id)
        if record is None:
            record = self._legacy_record(adapter, info)
        if record is None or record.status is not DownloadStatus.COMPLETED:
            return None
        if not record.file_path:
            return None
        if not Path(record.file_path).exists():
            logger.warning("数据库记录存在但文件缺失，将重新下载：%s", record.file_path)
            return None
        return record

    def _legacy_record(
        self, adapter: PlatformAdapter, info: VideoInfo
    ) -> DownloadRecord | None:
        """A row written under an identity an older version used for this download."""

        for key in adapter.legacy_video_ids(info):
            record = self.database.get(info.platform, key)
            if record is None:
                continue
            if not adapter.is_same_download(info, record.url or ""):
                logger.debug("忽略旧键 %s 的记录：URL 指向的不是同一次下载", key)
                continue
            logger.info("命中旧版本记录（键 %s），无需重复下载", key)
            return record
        return None

    async def _fetch_streams(
        self,
        plan: DownloadPlan,
        target_dir: Path,
        progress: dict[str, ProgressCallback] | None,
        downloader: HttpDownloader,
        *,
        stem: str,
    ) -> tuple[Path, int]:
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
        try:
            outcomes = await downloader.download_many(pairs, progress=progress)
        except DownloadCancelled:
            # A cancelled task can still be resumed, so its files stay.
            raise
        except Exception as exc:
            self._discard_unused_intermediates(plan, jobs, completed_outcomes(exc))
            raise
        total_bytes = sum(outcome.bytes_downloaded for outcome in outcomes)
        return outcomes[0].path, total_bytes

    def _discard_unused_intermediates(
        self,
        plan: DownloadPlan,
        jobs: list[tuple[str, Path]],
        completed: list[DownloadOutcome],
    ) -> None:
        """Remove the merge inputs this failed run had already produced.

        A merge needs both streams; when one of them fails the other is dead
        weight, and leaving it behind litters the output folder with a file
        nothing will ever pick up. Only paths with a :class:`DownloadOutcome`
        are candidates - that is the proof this run wrote them - and ``.part``
        files are never touched, because the next attempt resumes from them.
        """

        if not plan.merge:
            return
        produced = {os.path.normcase(str(outcome.path)) for outcome in completed}
        for _, path in jobs:
            if os.path.normcase(str(path)) not in produced:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem dependent
                logger.warning("无法清理未使用的中间文件 %s：%s", path.name, exc)
            else:
                logger.info("已清理未使用的中间文件：%s", path.name)

    async def _finalize(
        self,
        plan: DownloadPlan,
        first_path: Path,
        target_dir: Path,
        *,
        stem: str,
        expected: tuple[bool, tuple[int, int, int] | None],
        backup: Path | None = None,
    ) -> Path:
        if not plan.merge:
            return first_path

        if plan.video is None or plan.audio is None:
            raise NotDownloadableError("合并计划缺少视频或音频流")

        video_path = target_dir / f"{stem}.video.{plan.video.ext or 'mp4'}"
        audio_path = target_dir / f"{stem}.audio.{plan.audio.ext or 'm4a'}"
        merged = target_dir / f"{stem}.{plan.container or 'mp4'}"
        # ffmpeg writes into whatever path it is handed. Merging straight onto
        # the destination would truncate the file a forced re-download is meant
        # to preserve, and a failed merge would leave that half-written file in
        # place with nothing to tell it apart from a third party's work. So the
        # merge lands beside it and only a guarded rename publishes it.
        staging = target_dir / f"{stem}.merge.{plan.container or 'mp4'}"

        logger.info("使用 ffmpeg 合并音视频…")
        try:
            await self.merger.merge(video_path, audio_path, staging, container=plan.container)
        except BaseException:
            self._discard_staging(staging)
            raise

        # There is deliberately no "publish without a destination check" path:
        # every merge goes through the compare-and-swap in ``_publish_staging``.
        try:
            self._publish_staging(staging, merged, expected=expected, backup=backup)
        except TargetFileConflictError:
            # The name belongs to somebody else now; our merged file is of no
            # use, so it goes.
            self._discard_staging(staging)
            raise
        except BaseException:
            # Publishing failed for a reason that may not repeat (no hard links,
            # a permission problem). Keep the merged file: rebuilding it means
            # downloading both streams again.
            logger.warning("合并结果未能发布，保留在 %s", staging)
            raise

        for stale in (video_path, audio_path):
            try:
                stale.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - filesystem dependent
                logger.debug("无法删除临时流文件 %s", stale)
        return merged

    def _before_publish(self, target: Path) -> None:
        """Overridable pause before the new file takes the name.

        Production does nothing here. Tests subclass the service and override
        this method to drop a third party's file into the exact race window -
        there is no attribute to set, so no accidental class attribute can make
        a real run call test code.
        """

    def _publish_staging(
        self,
        staging: Path,
        target: Path,
        *,
        expected: tuple[bool, tuple[int, int, int] | None],
        backup: Path | None,
    ) -> None:
        """Publish the merged file without ever replacing a stranger's file.

        Windows has no "replace only if unchanged" primitive, so the swap is
        assembled from the atomic ones it does have: a file being replaced is
        first renamed aside and its identity checked, and the new file is then
        *created* at the name - ``os.link`` refuses to overwrite anything that
        appeared in the meantime. Whatever we did not expect is put back or left
        beside the target; nothing of somebody else's is deleted.
        """

        existed, signature = expected

        if not existed:
            self._before_publish(target)
            try:
                self._create_from(staging, target)
            except FileExistsError as exc:
                raise TargetFileConflictError(
                    f"目标文件已被其他程序创建，已停止写入：{target.name}",
                    detail="发布前目标文件出现",
                ) from exc
            return

        displaced = _swap_path(target)
        try:
            os.replace(target, displaced)
        except OSError as exc:
            raise TargetFileError(
                f"无法写入目标文件：{target.name}",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        if _signature(displaced) != signature:
            # The file we were authorised to replace is gone; put theirs back
            # the same safe way: only into a name that is still free.
            self._put_back(displaced, target, label="被移开的文件")
            raise TargetFileConflictError(
                f"目标文件已被其他程序替换，已停止写入：{target.name}",
                detail="发布前目标文件发生变化",
            )

        self._before_publish(target)
        try:
            self._create_from(staging, target)
        except FileExistsError as exc:
            # The name was taken again. The displaced copy is only dropped when
            # the pre-download backup really exists to fall back on.
            if backup is not None and backup.exists():
                with contextlib.suppress(OSError):
                    displaced.unlink(missing_ok=True)
                kept = f"原文件保留在 {backup}"
            else:  # pragma: no cover - a backup always exists on this path
                kept = f"原文件保留在 {displaced}"
            logger.warning("目标名被其他程序占用，%s", kept)
            raise TargetFileConflictError(
                f"目标文件已被其他程序创建，已停止写入：{target.name}",
                detail=kept,
            ) from exc

        try:
            displaced.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("无法删除发布过程中的临时文件 %s：%s", displaced, exc)

    @staticmethod
    def _create_from(staging: Path, target: Path) -> None:
        """Create ``target`` from ``staging``; never replaces an existing file.

        ``os.link`` is the atomic "create if absent" Windows offers, so it is
        the only publishing primitive used. When it fails the publish fails:
        falling back to ``os.replace`` would reintroduce the very race this
        guards against. The error is classified so the user learns whether the
        filesystem simply cannot do it or something else went wrong.
        """

        try:
            os.link(staging, target)
        except FileExistsError:
            raise
        except OSError as exc:
            if _looks_like_no_hardlinks(exc):
                raise TargetFileError(
                    f"当前文件系统不支持硬链接，无法安全发布：{target.name}",
                    detail=(
                        f"{type(exc).__name__}: {exc}；"
                        f"合并结果保留在 {staging.name}，可用其他磁盘重试"
                    ),
                ) from exc
            raise TargetFileError(
                f"无法安全发布到目标文件：{target.name}",
                detail=f"{type(exc).__name__}: {exc}；合并结果保留在 {staging.name}",
            ) from exc
        try:
            staging.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("无法删除合并临时文件 %s：%s", staging, exc)

    @staticmethod
    def _discard_staging(staging: Path) -> None:
        """Drop the merge scratch file - it is ours and never user data."""

        try:
            staging.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("无法删除合并临时文件 %s：%s", staging, exc)

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


#: Suffix of the copy parked beside a file a forced re-download replaces.
_BACKUP_SUFFIX = ".previous"
#: Suffix of the file moved aside for the split second a publish takes.
_SWAP_SUFFIX = ".swap"

#: I/O errors where another attempt cannot plausibly succeed: the disk is full,
#: the folder is read-only, or the process may not write there at all. Retrying
#: those only downloads the same file again into the same wall. Everything else
#: (a sharing violation, a transient read error) keeps the bounded retry.
PERMANENT_IO_ERRNOS = frozenset(
    code
    for code in (errno.ENOSPC, errno.EACCES, errno.EPERM, getattr(errno, "EROFS", None))
    if code is not None
)

#: ``os.link`` failures that mean "this filesystem has no hard links". Anything
#: else - a permission problem, a full disk, a vanished directory - is a real
#: error and must not be mistaken for a missing capability.
_NO_HARDLINK_ERRNOS = {
    code
    for code in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        errno.ENOSYS,
    )
    if code is not None
}
#: Windows reports "no hard links here" as ``ERROR_INVALID_FUNCTION`` or
#: ``ERROR_NOT_SUPPORTED`` rather than as an errno the POSIX set covers.
_NO_HARDLINK_WINERRORS = {1, 50}


def _looks_like_no_hardlinks(exc: OSError) -> bool:
    """Whether ``exc`` really means the filesystem cannot make hard links."""

    if exc.errno in _NO_HARDLINK_ERRNOS:
        return True
    return getattr(exc, "winerror", None) in _NO_HARDLINK_WINERRORS


def _free_sibling(destination: Path, suffix: str) -> Path:
    """A free name beside ``destination`` ending in ``suffix``."""

    for index in range(100):
        tail = suffix if index == 0 else f"{suffix}.{index}"
        candidate = destination.with_name(destination.name + tail)
        if not candidate.exists():
            return candidate
    raise TargetFileError(
        f"临时文件名已被占满，无法安全替换：{destination.name}",
        detail=f"尝试了 100 个 {destination.name}{suffix}* 名称",
    )


def _backup_path(destination: Path) -> Path:
    """Where the copy of a file about to be replaced is kept."""

    return _free_sibling(destination, _BACKUP_SUFFIX)


def _swap_path(destination: Path) -> Path:
    """Where the current target is parked while the new file takes its name."""

    return _free_sibling(destination, _SWAP_SUFFIX)


def _signature(path: Path) -> tuple[int, int, int] | None:
    """Size, modification time and file index - enough to spot a different file."""

    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _output_suffix(plan: DownloadPlan) -> str:
    """The extension the finished file will carry.

    Mirrors the names ``_fetch_streams``/``_finalize`` build, so the claimed
    destination is the file that actually gets written - claiming "mp4" while
    the payload ends up as "webm" would let two videos collide again.
    """

    if plan.merge:
        return plan.container or "mp4"
    if plan.video is not None:
        return plan.video.ext or "mp4"
    if plan.audio is not None:
        return plan.audio.ext or "m4a"
    return "mp4"


def _resolution(plan: DownloadPlan) -> str | None:
    video = plan.video
    if video and video.width and video.height:
        return f"{video.width}x{video.height}"
    if video and video.height:
        return f"{video.height}p"
    return None
