"""The download queue: several tasks, a bounded number at a time.

The queue does not know how to download anything. Each task is executed by
:func:`core.runner.execute_download`, which calls the same
``DownloadService.download`` the CLI and the single-download GUI worker use - so
proxy routing, cookie sessions, ffmpeg merging, ranged resume, integrity checks
and history bookkeeping all behave exactly as before.

What the queue adds is scheduling: an ordered list of tasks, a cap on how many
run at once, pause / resume / cancel / retry, and a SQLite row per task so the
list survives a restart.

Four behaviours are worth stating explicitly because they are the ones a
caller will otherwise guess wrong:

* **Pause keeps the partial file.** Pausing stops the transfer through the same
  ``threading.Event`` the desktop GUI already uses, which leaves the ``.part``
  file on disk; resuming re-runs the task and the HTTP engine continues from
  where it stopped via a ``Range`` request. Nothing is thrown away.
* **A task that dies with the process comes back as pending.** A row found in
  ``running`` at startup means the application was closed mid-download, not that
  the download failed, so it is re-queued rather than marked failed.
* **Retries are whole-task.** A failed task is retried from scratch after a
  backoff, up to ``queue_max_attempts``; the per-stream retries inside
  ``HttpDownloader`` are unchanged and still happen first.
* **A retry beats the run it interrupts.** ``cancel()`` marks a task cancelled
  without waiting for the transfer to stop, so the worker may still be
  unwinding when the user retries. Each run carries a token and only the run
  that still holds the task may write its conclusion, so the old run cannot
  overwrite the retry.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from config.constants import MAX_CONCURRENT_LIMIT
from config.settings import Settings
from core.downloader import ProgressCallback, ProgressUpdate
from core.exceptions import DownloadCancelled, VideoDownloaderError
from core.models import DownloadResult, DownloadStatus
from core.queue_store import QueueRow, QueueStore
from core.runner import execute_download

logger = logging.getLogger(__name__)

#: Retry backoff for a whole failed task, in seconds, indexed by attempt.
RETRY_BACKOFF_SECONDS = (5, 15, 30, 60)

#: How often an idle worker re-checks for work (and for a concurrency change).
IDLE_POLL_SECONDS = 0.4


class QueueState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        """True when the task will not run again on its own."""

        return self in (
            QueueState.COMPLETED,
            QueueState.SKIPPED,
            QueueState.FAILED,
            QueueState.CANCELLED,
        )

    @property
    def unfinished(self) -> bool:
        return not self.finished

    @property
    def countable(self) -> bool:
        """True when the task occupies a slot in the queue listing."""

        return self in (QueueState.PENDING, QueueState.RUNNING, QueueState.PAUSED)


#: How a :class:`~core.models.DownloadStatus` maps onto a queue state.
_RESULT_STATES = {
    DownloadStatus.COMPLETED: QueueState.COMPLETED,
    DownloadStatus.SKIPPED: QueueState.SKIPPED,
}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class QueueRequest:
    """One thing to queue, plus whatever the caller already knows about it.

    A bare URL is enough for a link the user pasted. A playlist listing knows
    more: the entry's own title, and the identity the platform will report for
    it. Carrying them here means the queue can show a real title straight away
    instead of a raw URL, and that the identity survives a restart - without the
    queue ever having to ask the network what a task is.
    """

    url: str
    title: str | None = None
    video_id: str | None = None


@dataclass(slots=True)
class QueueTask:
    """One queued download and everything known about it."""

    task_id: str
    url: str
    quality: str = "best"
    title: str | None = None
    #: The platform's identity for this download, once known. A playlist listing
    #: supplies it up front; otherwise it is filled in from the finished
    #: download. It is *metadata* - the queue still deduplicates on the URL,
    #: because the parts of one Bilibili video share a single bvid.
    video_id: str | None = None
    platform: str | None = None
    #: Per-task destination, when the user chose a folder for it. ``None``
    #: means "use the configured default", which is what the window's empty
    #: 保存位置 field has always meant.
    output_dir: str | None = None
    state: QueueState = QueueState.PENDING
    attempts: int = 0
    max_attempts: int = 2
    position: int = 0
    error: str | None = None
    result: DownloadResult | None = None
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Set when the transfer should stop; shared with ``DownloadService``.
    cancel_event: threading.Event = field(default_factory=threading.Event)
    #: True when the stop was a pause rather than a cancellation, so the worker
    #: can tell the two apart when the transfer unwinds.
    pause_requested: bool = False
    #: True when the stop came from the queue shutting down rather than from the
    #: user. A shutdown is not a decision to stop: such a task goes back to
    #: *pending* so the next start carries on, exactly as a crash would. Without
    #: this, closing the window cleanly was worse than crashing - the crash left
    #: a ``running`` row that got resumed, while the clean close left a
    #: ``paused`` row that never ran again.
    interrupted: bool = False
    #: A retry is not eligible before this instant.
    not_before: float = 0.0
    #: Which run of this task is allowed to write its conclusion. The worker
    #: bumps it when it claims the task, and ``retry()`` bumps it again, so a
    #: run that is still unwinding can tell that the task has moved on and stay
    #: quiet instead of overwriting the retry the user just asked for.
    run_id: int = 0
    #: True while a worker is executing this task. ``retry()`` puts the task
    #: back to *pending* while the previous run is still unwinding, so the state
    #: alone cannot say whether a run is in flight - and without this a second
    #: worker would start a second transfer of the same file on top of the first.
    claimed: bool = False
    _streams: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- progress -----------------------------------------------------------
    @property
    def file_path(self) -> str | None:
        """Where the finished file landed, once there is one."""

        if self.result is not None and self.result.video_path is not None:
            return str(self.result.video_path)
        return None

    def update_progress(self, update: ProgressUpdate) -> None:
        """Called by the download core on the worker thread, about twice a second."""

        entry: dict[str, Any] = {
            "kind": update.stream_kind,
            "downloaded": update.downloaded,
            "total": update.total,
            "speed_bps": round(update.speed_bps, 1),
            "resumed_from": update.resumed_from,
            "fraction": update.fraction,
        }
        with self._lock:
            self._streams[update.stream_kind] = entry

    def snapshot(self) -> dict[str, Any]:
        """A consistent copy for the window to render."""

        with self._lock:
            streams = {kind: dict(value) for kind, value in self._streams.items()}
            downloaded = sum(int(value.get("downloaded") or 0) for value in streams.values())
            totals = [value.get("total") for value in streams.values()]
            known = [int(total) for total in totals if isinstance(total, int) and total > 0]
            total = sum(known) if known and len(known) == len(streams) else None
            speed = sum(float(value.get("speed_bps") or 0.0) for value in streams.values())
            return {
                "task_id": self.task_id,
                "url": self.url,
                "quality": self.quality,
                "title": self.title,
                "video_id": self.video_id,
                "platform": self.platform,
                "state": self.state,
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
                "position": self.position,
                "error": self.error,
                "downloaded": downloaded,
                "total": total,
                "fraction": round(min(downloaded / total, 1.0), 4) if total else None,
                "speed_bps": speed,
                "streams": streams,
                "file_path": self.file_path,
                "resolution": self.result.resolution if self.result else None,
                "quality_label": self.result.quality_label if self.result else None,
            }

    def to_row(self) -> QueueRow:
        return QueueRow(
            task_id=self.task_id,
            url=self.url,
            quality=self.quality,
            title=self.title,
            video_id=self.video_id,
            state=self.state.value,
            attempts=self.attempts,
            max_attempts=self.max_attempts,
            position=self.position,
            error=self.error,
            file_path=self.file_path,
            file_size=self.result.bytes_downloaded if self.result else None,
            output_dir=self.output_dir,
            created_at=self.created_at,
            updated_at=_now(),
        )

    @classmethod
    def from_row(cls, row: QueueRow) -> QueueTask:
        return cls(
            task_id=row.task_id,
            url=row.url,
            quality=row.quality,
            title=row.title,
            video_id=row.video_id,
            output_dir=row.output_dir,
            state=QueueState(row.state),
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            position=row.position,
            error=row.error,
            created_at=row.created_at or _now(),
        )


#: The pluggable "do the download" step, so tests can drive the state machine
#: without touching the network.
Executor = Callable[[QueueTask, "dict[str, ProgressCallback]", threading.Event], DownloadResult]


class DownloadQueue:
    """An ordered, bounded, persistent queue of downloads."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: QueueStore | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or QueueStore(settings)
        self._executor: Executor = executor or self._default_executor

        self._tasks: dict[str, QueueTask] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._max_concurrent = settings.concurrent_downloads
        self._active = 0
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._running = False

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Load persisted work and begin scheduling."""

        if self._running:
            return
        self._stopping.clear()
        self._restore()
        # One thread per possible slot; ``max_concurrent`` decides how many may
        # hold a task at once, so changing the limit needs no thread churn.
        self._threads = [
            threading.Thread(target=self._worker_loop, name=f"vd-queue-{index}", daemon=True)
            for index in range(MAX_CONCURRENT_LIMIT)
        ]
        self._running = True
        for thread in self._threads:
            thread.start()
        logger.info("下载队列已启动（最大并发 %d）", self._max_concurrent)

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop scheduling and interrupt whatever is running.

        An interrupted transfer is put back to *pending*, not paused: the queue
        is shutting down, which is not the user deciding to stop. Its partial
        file stays on disk, so the next start resumes it.
        """

        if self._running:
            self._stopping.set()
            with self._lock:
                # ``claimed`` matters here too: a retry puts a task back to
                # pending while its previous run is still unwinding, and that
                # run has to be told the shutdown is not a user decision.
                running = [
                    task
                    for task in self._tasks.values()
                    if task.state is QueueState.RUNNING or task.claimed
                ]
            for task in running:
                task.interrupted = True
                task.pause_requested = True
                task.cancel_event.set()
            with self._condition:
                self._condition.notify_all()
            for thread in self._threads:
                thread.join(timeout=timeout / max(len(self._threads), 1))
            self._threads = []
            self._running = False
            logger.info("下载队列已停止")
        # Closed even when the queue was never started, so a caller that only
        # submitted tasks still releases the database handle.
        self.store.close()

    @property
    def running(self) -> bool:
        return self._running

    # -- configuration ------------------------------------------------------
    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def set_max_concurrent(self, value: int) -> int:
        """Change the concurrency limit. Takes effect on the next scheduling pass."""

        clamped = max(1, min(int(value), MAX_CONCURRENT_LIMIT))
        with self._condition:
            self._max_concurrent = clamped
            self._condition.notify_all()
        logger.info("最大并发下载数已设为 %d", clamped)
        return clamped

    # -- submission ---------------------------------------------------------
    def submit(
        self,
        url: str,
        quality: str = "best",
        *,
        title: str | None = None,
        video_id: str | None = None,
        output_dir: str | None = None,
        max_attempts: int | None = None,
    ) -> QueueTask:
        """Queue one download.

        A URL already waiting in the queue is not queued twice - the existing
        task is returned, so a double click cannot start two downloads of the
        same video that would then fight over one output file.
        """

        cleaned = (url or "").strip()
        if not cleaned:
            raise ValueError("url 不能为空")

        with self._lock:
            existing = self._find_unfinished_by_url(cleaned)
            if existing is not None:
                logger.info("该链接已在队列中，复用任务 %s", existing.task_id)
                return existing

            position = max((task.position for task in self._tasks.values()), default=0) + 1
            task = QueueTask(
                task_id=uuid.uuid4().hex[:12],
                url=cleaned,
                quality=quality,
                title=title or None,
                video_id=video_id or None,
                output_dir=output_dir or None,
                max_attempts=max_attempts or self.settings.queue_attempts,
                position=position,
            )
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)
        self._persist(task)
        with self._condition:
            self._condition.notify_all()
        logger.info("已加入队列：%s（%s）", task.task_id, cleaned)
        return task

    def submit_many(
        self,
        requests: Sequence[str | QueueRequest],
        quality: str = "best",
        *,
        output_dir: str | None = None,
    ) -> list[QueueTask]:
        """Queue several downloads in order, skipping duplicates and blanks.

        A bare string is the ordinary case - the user pasted a link. A
        :class:`QueueRequest` additionally carries the title and identity a
        playlist listing already knows, so those tasks are labelled before
        anything is fetched.
        """

        tasks: list[QueueTask] = []
        for item in requests:
            request = item if isinstance(item, QueueRequest) else QueueRequest(url=item)
            if not (request.url or "").strip():
                continue
            tasks.append(
                self.submit(
                    request.url,
                    quality,
                    title=request.title,
                    video_id=request.video_id,
                    output_dir=output_dir,
                )
            )
        return tasks

    # -- queries ------------------------------------------------------------
    def tasks(self) -> list[QueueTask]:
        """Every task, in queue order."""

        with self._lock:
            return [self._tasks[task_id] for task_id in self._order if task_id in self._tasks]

    def get(self, task_id: str) -> QueueTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def queue_position(self, task_id: str) -> int | None:
        """1-based place among the tasks that are still waiting or running."""

        with self._lock:
            waiting = [
                task.task_id
                for task in self._ordered_locked()
                if task.state.countable
            ]
        try:
            return waiting.index(task_id) + 1
        except ValueError:
            return None

    def counts(self) -> dict[str, int]:
        """Task totals per state, for the window's summary line."""

        counts = {state.value: 0 for state in QueueState}
        with self._lock:
            for task in self._tasks.values():
                counts[task.state.value] += 1
        return counts

    # -- control ------------------------------------------------------------
    def pause(self, task_id: str) -> bool:
        """Stop a waiting or running task, keeping its partial file.

        The state is read, decided on and written inside one critical section.
        That matters because a worker claims a task by flipping it to
        ``RUNNING`` while holding the same lock: if the decision were taken
        outside it, a worker could claim the task between the check and the
        write, and the pause would be silently overwritten by the start (or,
        worse, a task the worker had just finished would be dragged back to
        ``paused`` and re-downloaded on the next resume).

        Persisting happens *after* the lock is released: it touches the disk,
        and a slow write must not stall the workers waiting to schedule.
        """

        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or task.state.finished:
                return False
            task.pause_requested = True
            if task.state is QueueState.RUNNING:
                # The worker notices the event, the transfer unwinds, and the
                # task is marked paused instead of cancelled. Nothing to persist
                # yet - the worker writes the paused row when it unwinds.
                task.cancel_event.set()
                persist = False
            else:
                task.state = QueueState.PAUSED
                persist = True
            self._condition.notify_all()
        if persist:
            self._persist(task)
        logger.info("已暂停队列任务 %s", task_id)
        return True

    def resume(self, task_id: str) -> bool:
        """Put a paused task back in the queue; the partial file makes it continue."""

        task = self.get(task_id)
        if task is None or task.state is not QueueState.PAUSED:
            return False
        task.pause_requested = False
        task.cancel_event.clear()
        task.not_before = 0.0
        task.state = QueueState.PENDING
        task.error = None
        self._persist(task)
        with self._condition:
            self._condition.notify_all()
        logger.info("已继续队列任务 %s", task_id)
        return True

    def cancel(self, task_id: str) -> bool:
        """Drop a task. Whatever it already downloaded is kept on disk."""

        task = self.get(task_id)
        if task is None or task.state.finished:
            return False
        task.pause_requested = False
        task.cancel_event.set()
        if task.state is QueueState.RUNNING:
            # Marked now; the worker will confirm when the transfer unwinds.
            task.state = QueueState.CANCELLED
            task.finished_at = _now()
        else:
            task.state = QueueState.CANCELLED
            task.finished_at = _now()
        self._persist(task)
        with self._condition:
            self._condition.notify_all()
        logger.info("已取消队列任务 %s", task_id)
        return True

    def retry(self, task_id: str) -> bool:
        """Put a failed or cancelled task back in the queue immediately.

        A cancelled task may still have a worker unwinding it - ``cancel()``
        marks it cancelled at once, without waiting for the transfer to stop.
        The run id is therefore bumped here, under the same lock the worker
        claims with, so that run's conclusion is ignored rather than written on
        top of this retry. Doing it under the lock also means the worker cannot
        claim the task in the gap between the state change and the bump.

        ``claimed`` is deliberately left alone: the previous run is still
        holding the task, and it stays unclaimable until that run lets go.
        """

        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or task.state not in (QueueState.FAILED, QueueState.CANCELLED):
                return False
            task.run_id += 1
            task.error = None
            # Keep the cancellation armed for the run still unwinding. The
            # worker clears it when it claims the retried run.
            task.pause_requested = False
            task.not_before = 0.0
            task.state = QueueState.PENDING
            task.finished_at = None
            self._condition.notify_all()
        self._persist(task)
        logger.info("已重试队列任务 %s", task_id)
        return True

    def remove(self, task_id: str) -> bool:
        """Forget a task entirely, without touching any file it produced."""

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            # Stop it first. A run that has not settled yet would still write
            # its conclusion, and would then write a row for a task the window
            # has already forgotten. A run that has settled may still be
            # unwinding, but it has nothing left to write.
            if task.state is QueueState.RUNNING or (task.claimed and not task.state.finished):
                return False
            self._tasks.pop(task_id, None)
            if task_id in self._order:
                self._order.remove(task_id)
        self.store.delete(task_id)
        return True

    def clear_finished(self) -> int:
        """Drop every finished task from the listing and the table."""

        with self._lock:
            gone = [task_id for task_id, task in self._tasks.items() if task.state.finished]
            for task_id in gone:
                self._tasks.pop(task_id, None)
                if task_id in self._order:
                    self._order.remove(task_id)
        if gone:
            self.store.delete_finished()
        return len(gone)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for task in self._tasks.values() if task.state is QueueState.PENDING)

    # -- internals ----------------------------------------------------------
    def _ordered_locked(self) -> list[QueueTask]:
        return [self._tasks[task_id] for task_id in self._order if task_id in self._tasks]

    def _find_unfinished_by_url(self, url: str) -> QueueTask | None:
        for task in self._ordered_locked():
            if task.url == url and task.state.unfinished:
                return task
        return None

    def _persist(self, task: QueueTask) -> None:
        try:
            self.store.upsert(task.to_row())
        except Exception:  # noqa: BLE001 - persistence must not kill a download
            logger.warning("写入队列记录失败（%s）", task.task_id, exc_info=True)

    def _restore(self) -> None:
        """Rebuild the queue from SQLite."""

        try:
            rows = self.store.load_all()
            self.store.prune()
        except Exception:  # noqa: BLE001 - a broken table must not stop the app
            logger.warning("读取队列记录失败，本次从空队列开始", exc_info=True)
            return

        with self._lock:
            for row in rows:
                if row.task_id in self._tasks:
                    continue  # already submitted in this session; leave it alone
                task = QueueTask.from_row(row)
                if task.state is QueueState.RUNNING:
                    # The process died mid-download. The .part file is still
                    # there, so re-queue rather than call it a failure.
                    task.state = QueueState.PENDING
                # PAUSED is left paused: the user asked for it, and a restart is
                # not a reason to overrule them.
                if task.state is QueueState.PENDING:
                    task.not_before = 0.0
                self._tasks[task.task_id] = task
                self._order.append(task.task_id)
        restored = sum(1 for task in self.tasks() if task.state is QueueState.PENDING)
        if restored:
            logger.info("已从数据库恢复 %d 个未完成任务", restored)

    def _next_task(self) -> QueueTask | None:
        """The oldest pending task that is ready to run, or ``None``.

        A task a worker is still executing is never handed out again, even when
        a retry has already put it back to *pending*: the previous run has to
        release it first, or two transfers would write the same file.
        """

        now = time.monotonic()
        for task in self._ordered_locked():
            if task.claimed:
                continue
            if task.state is QueueState.PENDING and task.not_before <= now:
                return task
        return None

    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            with self._condition:
                while (
                    not self._stopping.is_set()
                    and (self._active >= self._max_concurrent or self._next_task() is None)
                ):
                    self._condition.wait(timeout=IDLE_POLL_SECONDS)
                if self._stopping.is_set():
                    return
                task = self._next_task()
                if task is None:
                    continue
                self._active += 1
                task.state = QueueState.RUNNING
                task.attempts += 1
                # Claiming the task starts a new run, and only that run may
                # write the task's conclusion. ``claimed`` keeps the task out of
                # circulation until this run lets go of it.
                task.run_id += 1
                token = task.run_id
                task.claimed = True
                task.started_at = task.started_at or _now()
                task.error = None
                # Both belong to the run that just ended, and a shutdown leaves
                # them set. Carrying them into the next run would make its very
                # first cancel look like another shutdown, which puts the task
                # straight back to pending - and the run after that does the
                # same, so the task would never settle.
                task.interrupted = False
                task.cancel_event.clear()
            self._persist(task)

            try:
                self._run(task, token)
            except Exception:  # noqa: BLE001 - one bad task must not kill the worker
                logger.exception("队列任务执行时出现未预期的异常：%s", task.task_id)
                self._finish(task, QueueState.FAILED, error="内部错误", token=token)
            finally:
                with self._condition:
                    task.claimed = False
                    self._active -= 1
                    self._condition.notify_all()

    def _run(self, task: QueueTask, token: int) -> None:
        """Execute one task and translate the outcome into a queue state.

        ``token`` identifies the run. A retry moves the task on to a new run
        while this one is still unwinding, so everything computed here is only
        written while this run still owns the task.
        """

        progress: dict[str, ProgressCallback] = {
            "video": task.update_progress,
            "audio": task.update_progress,
        }
        try:
            result = self._executor(task, progress, task.cancel_event)
        except DownloadCancelled:
            if task.interrupted:
                # The queue is shutting down. Not a user decision, so the task
                # stays work-to-do and the next start resumes its partial file.
                if self._settle(
                    task, token, state=QueueState.PENDING, error=None, not_before=0.0
                ):
                    logger.info("队列停止，任务已重新排队：%s", task.task_id)
            elif task.pause_requested:
                if self._settle(task, token, state=QueueState.PAUSED, error=None):
                    logger.info("队列任务已暂停（可继续）：%s", task.task_id)
            else:
                self._finish(task, QueueState.CANCELLED, token=token)
            return
        except VideoDownloaderError as exc:
            self._retry_or_fail(task, exc.message, token)
            return

        with self._condition:
            if not self._owns_run(task, token):
                logger.info("任务已开始新一轮，忽略上一轮的结果：%s", task.task_id)
                return
            task.result = result
            # The title is what the window shows, so take it from the result even
            # when the download produced no file (a skip, or a failure carrying
            # partial information).
            task.title = task.title or result.title or None
            task.platform = result.platform.value
            # A task queued from a plain link had no identity until now; the
            # listing supplies its own up front, so never overwrite that.
            task.video_id = task.video_id or result.video_id or None
        state = _RESULT_STATES.get(result.status, QueueState.FAILED)
        if state is QueueState.FAILED:
            self._retry_or_fail(task, result.error or "下载未完成", token)
            return
        self._finish(task, state, token=token)

    def _retry_or_fail(self, task: QueueTask, message: str, token: int) -> None:
        """Schedule another attempt when attempts remain, else mark failed."""

        if task.cancel_event.is_set() and not task.pause_requested:
            self._finish(task, QueueState.CANCELLED, token=token)
            return
        if task.attempts < task.max_attempts:
            delay = RETRY_BACKOFF_SECONDS[min(task.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            if not self._settle(
                task,
                token,
                state=QueueState.PENDING,
                error=message,
                not_before=time.monotonic() + delay,
            ):
                return
            logger.warning(
                "队列任务失败，%d 秒后重试（第 %d/%d 次）：%s",
                delay,
                task.attempts,
                task.max_attempts,
                task.task_id,
            )
            return
        self._finish(task, QueueState.FAILED, error=message, token=token)

    def _finish(
        self,
        task: QueueTask,
        state: QueueState,
        *,
        error: str | None = None,
        token: int,
    ) -> None:
        if self._settle(task, token, state=state, error=error, final=True):
            logger.info("队列任务结束：%s → %s", task.task_id, state.value)

    def _settle(
        self,
        task: QueueTask,
        token: int,
        *,
        state: QueueState,
        error: str | None = None,
        not_before: float | None = None,
        final: bool = False,
    ) -> bool:
        """Write a run's outcome, unless a retry has taken the task over.

        The run token is re-checked *inside* the lock that ``retry()`` also
        takes, so a retry cannot slip between the check and the write - which is
        the whole point: the previous run's ``cancelled`` must not land on top
        of the retry the user just asked for.
        """

        with self._condition:
            stale = not self._owns_run(task, token)
            if not stale:
                task.state = state
                task.error = error
                if not_before is not None:
                    task.not_before = not_before
                if final:
                    task.finished_at = _now()
                    task.cancel_event.clear()
        if stale:
            logger.info("任务已开始新一轮，忽略上一轮的结果：%s", task.task_id)
            return False
        self._persist(task)
        return True

    @staticmethod
    def _owns_run(task: QueueTask, token: int) -> bool:
        """Whether the run identified by ``token`` still owns the task."""

        return task.run_id == token

    def _default_executor(
        self,
        task: QueueTask,
        progress: dict[str, ProgressCallback],
        cancel_event: threading.Event,
    ) -> DownloadResult:
        """Run the real download, on this worker thread's own event loop."""

        def _remember(adapter: Any) -> None:
            task.platform = adapter.platform.value
            if not task.title:
                task.title = None  # filled in from the result once it exists

        return asyncio.run(
            execute_download(
                self.settings,
                task.url,
                quality=task.quality,
                output_dir=Path(task.output_dir) if task.output_dir else None,
                progress=progress,
                cancel_event=cancel_event,
                on_adapter=_remember,
            )
        )


def eta_seconds(snapshot: dict[str, Any]) -> float | None:
    """Estimated seconds left for a task snapshot, or ``None``.

    Returns ``None`` while the total size is unknown or the transfer has
    stalled, so the window shows ``-`` instead of inventing a number.
    """

    total = snapshot.get("total")
    downloaded = snapshot.get("downloaded") or 0
    speed = snapshot.get("speed_bps") or 0.0
    if not total or speed <= 0 or downloaded >= total:
        return None
    return float(total - downloaded) / float(speed)
