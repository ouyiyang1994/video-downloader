"""The queue scheduler: ordering, concurrency, pause/resume/cancel/retry, restore.

The download itself is replaced by a scriptable fake, because what is under test
is the state machine - which task runs when, what a pause leaves behind, and
what a restart rebuilds - not the HTTP engine, which has its own tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

import pytest

from config.settings import Settings
from core import queue as queue_module
from core.downloader import ProgressUpdate
from core.exceptions import DownloadCancelled, DownloadError
from core.models import DownloadResult, DownloadStatus, Platform
from core.queue import DownloadQueue, QueueRequest, QueueState, QueueTask, eta_seconds
from core.queue_store import QueueRow, QueueStore

URL = "https://www.bilibili.com/video/BV1GJ411x7h7"


def _wait_until(predicate: Callable[[], bool], timeout: float = 15.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message or "等待条件超时")


class FakeDownload:
    """A scriptable stand-in for ``execute_download``.

    ``gate`` blocks a call until the test releases it, which is how the
    concurrency and pause tests hold a task in the running state.
    """

    def __init__(self, *, behaviour: str = "complete", fail_times: int = 0) -> None:
        self.behaviour = behaviour
        self.fail_times = fail_times
        self.calls: list[str] = []
        self.gate: threading.Event | None = None
        self.running = 0
        self.max_running = 0
        #: Set when the fake actually saw the cancel event while transferring.
        #: This is the honest way to assert "the cancel reached the download":
        #: ``_finish`` clears the task's event once it settles, so inspecting
        #: the event afterwards races with the worker thread.
        self.saw_cancel_event = False
        self._lock = threading.Lock()

    def _cancelled(self, cancel_event: threading.Event) -> bool:
        if cancel_event.is_set():
            self.saw_cancel_event = True
            return True
        return False

    def __call__(
        self, task, progress: dict, cancel_event: threading.Event
    ) -> DownloadResult:
        with self._lock:
            self.calls.append(task.task_id)
            attempt = len(self.calls)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            progress["video"](
                ProgressUpdate(stream_kind="video", downloaded=50, total=100, speed_bps=25.0)
            )
            if self.gate is not None:
                # Poll rather than block outright: a real download notices the
                # cancel event at its next chunk, and the pause/stop tests rely
                # on the fake behaving the same way.
                deadline = time.monotonic() + 10
                while not self.gate.is_set():
                    if self._cancelled(cancel_event):
                        raise DownloadCancelled("已取消")
                    if time.monotonic() > deadline:
                        raise DownloadCancelled("测试等待超时")
                    time.sleep(0.02)
            if self._cancelled(cancel_event):
                raise DownloadCancelled("已取消")
            if self.behaviour == "cancel":
                raise DownloadCancelled("已取消")
            if self.behaviour == "fail" or attempt <= self.fail_times:
                raise DownloadError("模拟失败")
            return DownloadResult(
                status=DownloadStatus.COMPLETED,
                platform=Platform.BILIBILI,
                video_id="BV1GJ411x7h7",
                title="示例视频",
                bytes_downloaded=100,
                elapsed_seconds=1.0,
                quality_label="1080P",
                resolution="1920x1080",
            )
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture
def queues(settings: Settings) -> Iterator[Callable[..., DownloadQueue]]:
    """Builds queues and guarantees they are stopped afterwards."""

    created: list[DownloadQueue] = []

    def factory(executor: FakeDownload, **kwargs: object) -> DownloadQueue:
        queue = DownloadQueue(settings, executor=executor, **kwargs)  # type: ignore[arg-type]
        created.append(queue)
        return queue

    yield factory
    for queue in created:
        queue.stop(timeout=5)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries in tests must not wait the real five seconds."""

    monkeypatch.setattr(queue_module, "RETRY_BACKOFF_SECONDS", (0, 0, 0))


# -- basics ------------------------------------------------------------------


def test_a_submitted_task_waits_in_the_queue(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    queue = queues(FakeDownload())  # never started: nothing may run yet
    task = queue.submit(URL, "720p")

    assert task.state is QueueState.PENDING
    assert task.quality == "720p"
    assert queue.queue_position(task.task_id) == 1
    # It is already durable, before the queue ever starts.
    with QueueStore(settings) as store:
        assert [row.task_id for row in store.load_all()] == [task.task_id]


def test_a_task_runs_and_completes(
    queues: Callable[..., DownloadQueue]
) -> None:
    fake = FakeDownload()
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL)

    _wait_until(lambda: task.state.finished, message="任务未完成")

    assert task.state is QueueState.COMPLETED
    assert task.result is not None
    assert task.result.title == "示例视频"
    assert task.title == "示例视频"
    assert fake.calls == [task.task_id]


def test_progress_is_recorded_on_the_task(queues: Callable[..., DownloadQueue]) -> None:
    queue = queues(FakeDownload())
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state.finished)

    snapshot = task.snapshot()
    assert snapshot["streams"]["video"]["downloaded"] == 50
    assert snapshot["streams"]["video"]["total"] == 100
    assert snapshot["downloaded"] == 50
    assert snapshot["fraction"] == 0.5
    assert snapshot["speed_bps"] == 25.0


def test_the_same_url_is_not_queued_twice(queues: Callable[..., DownloadQueue]) -> None:
    queue = queues(FakeDownload())  # not started, so the first task stays pending
    first = queue.submit(URL)
    second = queue.submit(f"  {URL}  ")

    assert second.task_id == first.task_id
    assert len(queue.tasks()) == 1


def test_a_finished_url_can_be_queued_again(
    queues: Callable[..., DownloadQueue]
) -> None:
    queue = queues(FakeDownload())
    queue.start()
    first = queue.submit(URL)
    _wait_until(lambda: first.state.finished)

    second = queue.submit(URL)
    assert second.task_id != first.task_id


def test_submit_many_keeps_the_given_order(queues: Callable[..., DownloadQueue]) -> None:
    queue = queues(FakeDownload())
    tasks = queue.submit_many([f"{URL}?p={n}" for n in range(1, 4)])

    assert [task.quality for task in tasks] == ["best"] * 3
    assert [task.position for task in tasks] == [1, 2, 3]
    assert [task.task_id for task in queue.tasks()] == [task.task_id for task in tasks]


def test_an_empty_url_is_refused(queues: Callable[..., DownloadQueue]) -> None:
    queue = queues(FakeDownload())
    with pytest.raises(ValueError):
        queue.submit("   ")


def test_output_dir_travels_with_the_task(queues: Callable[..., DownloadQueue]) -> None:
    """The window's 保存位置 box must keep working through the queue."""

    queue = queues(FakeDownload())
    task = queue.submit(URL, output_dir="D:/videos")
    assert task.output_dir == "D:/videos"
    assert task.to_row().output_dir == "D:/videos"


# -- labels and identity from a playlist listing -----------------------------
#
# A playlist listing already knows each entry's title and identity, so it hands
# them over instead of letting the queue show a raw URL until the download
# finishes. A link the user pasted carries neither - and nothing is invented.


def test_a_request_carries_its_label_and_identity_into_the_task(
    queues: Callable[..., DownloadQueue]
) -> None:
    queue = queues(FakeDownload())
    tasks = queue.submit_many(
        [
            QueueRequest(url=f"{URL}?p=1", title="投稿 - P1", video_id="BV1_p1"),
            QueueRequest(url=f"{URL}?p=2", title="投稿 - P2", video_id="BV1_p2"),
        ]
    )

    assert [task.url for task in tasks] == [f"{URL}?p=1", f"{URL}?p=2"]
    assert [task.title for task in tasks] == ["投稿 - P1", "投稿 - P2"]
    assert [task.video_id for task in tasks] == ["BV1_p1", "BV1_p2"]


def test_a_bare_url_still_queues_without_a_label(
    queues: Callable[..., DownloadQueue]
) -> None:
    """The pasted-link path is unchanged: no title, no identity."""

    queue = queues(FakeDownload())
    task = queue.submit_many([URL])[0]

    assert task.title is None
    assert task.video_id is None


def test_the_label_and_identity_survive_a_restart(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    """Both are persisted, so a restored row is labelled as it was before."""

    first = queues(FakeDownload())
    kept = first.submit_many(
        [QueueRequest(url=URL, title="投稿 - P3", video_id="BV1_p3")]
    )[0]
    first.stop()

    fake = FakeDownload()
    second = queues(fake)
    second.start()
    # The restored task is a fresh object owned by the new queue; the old one is
    # only useful for its id.
    restored = second.get(kept.task_id)
    assert restored is not None, "重启后没有恢复未完成任务"
    _wait_until(lambda: fake.calls == [kept.task_id], message="恢复的任务未被执行")
    _wait_until(lambda: restored.state.finished)

    assert restored.title == "投稿 - P3"
    assert restored.video_id == "BV1_p3"


def test_the_finished_download_fills_in_a_missing_identity(
    queues: Callable[..., DownloadQueue]
) -> None:
    """A pasted link has no identity until the download reports one."""

    queue = queues(FakeDownload())
    queue.start()
    task = queue.submit(URL)

    _wait_until(lambda: task.state.finished)
    assert task.video_id == "BV1GJ411x7h7"


def test_a_supplied_identity_is_not_overwritten_by_the_download(
    queues: Callable[..., DownloadQueue]
) -> None:
    """The listing's identity is the part-aware one; the download must not undo it."""

    queue = queues(FakeDownload())
    queue.start()
    task = queue.submit(URL, title="投稿 - P3", video_id="BV1fkHD6wEXt_p3")

    _wait_until(lambda: task.state.finished)
    assert task.video_id == "BV1fkHD6wEXt_p3"
    assert task.title == "投稿 - P3"


# -- concurrency -------------------------------------------------------------


def test_concurrency_is_capped(queues: Callable[..., DownloadQueue]) -> None:
    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.set_max_concurrent(2)
    queue.start()

    tasks = [queue.submit(f"{URL}?p={n}") for n in range(1, 5)]
    _wait_until(lambda: fake.running == 2, message="两个任务未同时开始")

    # The cap holds: a third task is waiting, not running.
    _wait_until(
        lambda: sum(1 for t in tasks if t.state is QueueState.RUNNING) == 2,
        message="运行中的任务数不是 2",
    )
    assert fake.max_running == 2

    fake.gate.set()
    _wait_until(lambda: all(t.state.finished for t in tasks), message="任务未全部结束")
    assert fake.max_running == 2, "并发上限被突破"


def test_raising_the_limit_lets_more_run(queues: Callable[..., DownloadQueue]) -> None:
    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.set_max_concurrent(1)
    queue.start()

    tasks = [queue.submit(f"{URL}?p={n}") for n in range(1, 4)]
    _wait_until(lambda: fake.running == 1, message="第一个任务未开始")
    time.sleep(0.2)
    assert fake.max_running == 1

    queue.set_max_concurrent(3)
    _wait_until(lambda: fake.running == 3, message="提高并发后未启动更多任务")

    fake.gate.set()
    _wait_until(lambda: all(t.state.finished for t in tasks))


def test_set_max_concurrent_clamps_to_the_hard_limit(
    queues: Callable[..., DownloadQueue]
) -> None:
    queue = queues(FakeDownload())
    assert queue.set_max_concurrent(999) == queue_module.MAX_CONCURRENT_LIMIT
    assert queue.set_max_concurrent(0) == 1


# -- pause / resume / cancel -------------------------------------------------


def test_pause_stops_a_running_task_and_keeps_it_resumable(
    queues: Callable[..., DownloadQueue]
) -> None:
    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert queue.pause(task.task_id) is True
    _wait_until(lambda: task.state is QueueState.PAUSED, message="任务未进入暂停")

    # Paused is not finished: the partial file stays and resume is offered.
    assert task.state.unfinished is True
    assert task.error is None


def test_pausing_a_pending_task_holds_it_back(
    queues: Callable[..., DownloadQueue]
) -> None:
    fake = FakeDownload()
    queue = queues(fake)  # not started
    task = queue.submit(URL)

    assert queue.pause(task.task_id) is True
    assert task.state is QueueState.PAUSED

    queue.start()
    time.sleep(0.4)
    assert fake.calls == [], "暂停中的任务不应被执行"


def test_resume_puts_a_paused_task_back_in_the_queue(
    queues: Callable[..., DownloadQueue]
) -> None:
    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING)
    queue.pause(task.task_id)
    _wait_until(lambda: task.state is QueueState.PAUSED)

    assert queue.resume(task.task_id) is True
    fake.gate.set()
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="继续后未完成")
    assert len(fake.calls) == 2, "继续应当重新执行该任务"


def test_cancel_drops_a_pending_task(queues: Callable[..., DownloadQueue]) -> None:
    fake = FakeDownload()
    queue = queues(fake)
    task = queue.submit(URL)

    assert queue.cancel(task.task_id) is True
    assert task.state is QueueState.CANCELLED

    queue.start()
    time.sleep(0.4)
    assert fake.calls == [], "已取消的任务不应被执行"


def test_cancel_stops_a_running_task(queues: Callable[..., DownloadQueue]) -> None:
    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING)

    assert queue.cancel(task.task_id) is True
    # Wait for the *download* to observe the cancel, rather than reading the
    # task's own event: ``_finish`` clears that event once the task settles, so
    # inspecting it here races with the worker thread. The fake polls on a
    # 20 ms tick, so this is a real wait, not a formality.
    _wait_until(lambda: fake.saw_cancel_event, message="取消没有传达到正在运行的下载")
    _wait_until(lambda: task.state is QueueState.CANCELLED, message="任务未被取消")


def test_pausing_a_finished_task_is_refused(queues: Callable[..., DownloadQueue]) -> None:
    queue = queues(FakeDownload())
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state.finished)

    assert queue.pause(task.task_id) is False
    assert queue.cancel(task.task_id) is False


def test_resume_only_applies_to_a_paused_task(
    queues: Callable[..., DownloadQueue]
) -> None:
    queue = queues(FakeDownload())
    task = queue.submit(URL)
    assert queue.resume(task.task_id) is False  # it is pending, not paused


def test_remove_refuses_a_running_task(queues: Callable[..., DownloadQueue]) -> None:
    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING)

    assert queue.remove(task.task_id) is False
    fake.gate.set()
    _wait_until(lambda: task.state.finished)

    assert queue.remove(task.task_id) is True
    assert queue.tasks() == []


# -- retry -------------------------------------------------------------------


def test_a_failed_task_is_retried(queues: Callable[..., DownloadQueue], no_backoff: None) -> None:
    fake = FakeDownload(fail_times=1)
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL, max_attempts=2)

    _wait_until(lambda: task.state is QueueState.COMPLETED, message="重试后未成功")

    assert len(fake.calls) == 2
    assert task.attempts == 2
    assert task.error is None


def test_a_task_that_keeps_failing_ends_up_failed(
    queues: Callable[..., DownloadQueue], no_backoff: None
) -> None:
    fake = FakeDownload(behaviour="fail")
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL, max_attempts=2)

    _wait_until(lambda: task.state is QueueState.FAILED, message="任务未判定为失败")

    assert len(fake.calls) == 2, "应当恰好尝试 max_attempts 次"
    assert task.error == "模拟失败"


def test_retry_requeues_a_failed_task(
    queues: Callable[..., DownloadQueue], no_backoff: None
) -> None:
    fake = FakeDownload(behaviour="fail")
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL, max_attempts=1)
    _wait_until(lambda: task.state is QueueState.FAILED)

    fake.behaviour = "complete"
    assert queue.retry(task.task_id) is True
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="手动重试未成功")
    assert task.error is None


def test_retry_only_applies_to_a_finished_task(queues: Callable[..., DownloadQueue]) -> None:
    queue = queues(FakeDownload())
    task = queue.submit(URL)
    assert queue.retry(task.task_id) is False  # still pending


# -- a retry beats the run it interrupts -------------------------------------
#
# ``cancel()`` marks a task cancelled at once, without waiting for the transfer
# to stop, so a retry can arrive while the worker is still unwinding the old
# run. That run used to end with ``_finish(CANCELLED)`` and put the task back to
# cancelled, so the retry was silently lost.
#
# These use a scripted executor rather than FakeDownload: the point is the old
# run ending *after* the retry, which a gated fake cannot express (it raises as
# soon as it sees the cancel, i.e. before the retry). Every wait is on an event
# or a condition - never a fixed sleep.


def _completed_result() -> DownloadResult:
    """What a successful fake download reports."""

    return DownloadResult(
        status=DownloadStatus.COMPLETED,
        platform=Platform.BILIBILI,
        video_id="BV1GJ411x7h7",
        title="示例视频",
        bytes_downloaded=100,
        elapsed_seconds=1.0,
        quality_label="1080P",
        resolution="1920x1080",
    )


def _run_ends_late(
    calls: list[str], release: threading.Event, outcome: BaseException
):
    """An executor whose first run blocks, then ends however ``outcome`` says."""

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        calls.append(task.task_id)
        if len(calls) == 1:
            release.wait(10)
            raise outcome
        return _completed_result()

    return executor


def test_a_retry_is_not_undone_by_the_run_that_is_still_unwinding(
    queues: Callable[..., DownloadQueue]
) -> None:
    """Cancel, retry immediately, then let the old run end as a cancel."""

    calls: list[str] = []
    release = threading.Event()
    queue = queues(_run_ends_late(calls, release, DownloadCancelled("已取消")))
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert queue.cancel(task.task_id) is True
    assert task.state is QueueState.CANCELLED, "cancel() 应当立刻标记"

    # Retry while the first run is still unwinding - the exact race.
    assert queue.retry(task.task_id) is True
    assert task.state is QueueState.PENDING

    release.set()
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="重试被旧的一轮吃掉了")
    assert len(calls) == 2, "旧的一轮把重试吃掉了"


def test_a_stale_run_cannot_fail_the_task_over_the_new_run(
    queues: Callable[..., DownloadQueue]
) -> None:
    """A late *failure* must not settle the task either.

    With one attempt allowed, an unguarded conclusion would mark the task failed
    and the retry would never run.
    """

    calls: list[str] = []
    release = threading.Event()
    queue = queues(_run_ends_late(calls, release, DownloadError("模拟失败")))
    queue.start()
    task = queue.submit(URL, max_attempts=1)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert queue.cancel(task.task_id) is True
    assert queue.retry(task.task_id) is True
    release.set()

    _wait_until(lambda: task.state is QueueState.COMPLETED, message="旧一轮把任务判失败了")
    assert len(calls) == 2


def test_a_cancel_and_retry_cycle_can_be_repeated(
    queues: Callable[..., DownloadQueue]
) -> None:
    """Two rounds of the same race, so the guard is not a one-shot."""

    calls: list[str] = []
    first_release = threading.Event()
    second_release = threading.Event()
    releases = [first_release, second_release]

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        calls.append(task.task_id)
        if len(calls) <= 2:
            releases[len(calls) - 1].wait(10)
            raise DownloadCancelled("已取消")
        return _completed_result()

    queue = queues(executor)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert queue.cancel(task.task_id) is True
    assert queue.retry(task.task_id) is True
    first_release.set()
    _wait_until(lambda: len(calls) == 2, message="第一次重试没有生效")

    assert queue.cancel(task.task_id) is True
    assert queue.retry(task.task_id) is True
    second_release.set()
    _wait_until(lambda: len(calls) == 3, message="第二次重试没有生效")

    _wait_until(lambda: task.state is QueueState.COMPLETED, message="重复重试后未完成")


def test_a_pending_task_a_run_still_holds_is_not_handed_out(
    queues: Callable[..., DownloadQueue]
) -> None:
    """A retry is pending again, but the run that holds it keeps it unclaimable.

    Otherwise a free worker slot would start a second transfer of the same file
    while the first is still running. Asserted on the scheduler's own view,
    because with the bug the task is claimed again before a test could look.
    """

    queue = queues(FakeDownload())  # not started: no worker competes with us
    task = queue.submit(URL)

    task.claimed = True  # a run is still executing it
    with queue._condition:
        assert queue._next_task() is None, "旧一轮还没放手，任务就已经可以被领取了"

    task.claimed = False  # the run let go
    with queue._condition:
        assert queue._next_task() is task, "放手之后任务应当可以被领取"


def test_a_running_task_is_marked_as_held(queues: Callable[..., DownloadQueue]) -> None:
    """The worker has to say that it is holding the task."""

    release = threading.Event()

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        release.wait(10)
        return _completed_result()

    queue = queues(executor)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert task.claimed is True, "worker 没有标记任务已被占用"

    release.set()
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="任务未完成")
    assert task.claimed is False, "worker 结束后没有释放任务"


def test_removing_a_task_a_run_still_holds_is_refused(
    queues: Callable[..., DownloadQueue]
) -> None:
    """A retried task whose previous run is still unwinding is still live.

    Removing it would orphan that run, which would then write a row for a task
    the window has already forgotten.
    """

    calls: list[str] = []
    release = threading.Event()
    queue = queues(_run_ends_late(calls, release, DownloadCancelled("已取消")))
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert queue.cancel(task.task_id) is True
    assert queue.retry(task.task_id) is True
    assert queue.remove(task.task_id) is False, "旧一轮还没放手，不该被移除"

    release.set()
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="重试没有生效")
    # Once the run has let go, the task is removable again.
    assert queue.remove(task.task_id) is True


def test_a_normal_cancel_still_settles_as_cancelled(
    queues: Callable[..., DownloadQueue]
) -> None:
    """The guard must not stop an ordinary cancel from settling."""

    observed = threading.Event()

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        cancel_event.wait(10)
        observed.set()
        raise DownloadCancelled("已取消")

    queue = queues(executor)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert queue.cancel(task.task_id) is True
    _wait_until(lambda: observed.is_set(), message="下载没有观察到取消")
    # ``_finish`` clears the event, so this proves the run's own conclusion
    # landed rather than being mistaken for a stale one.
    _wait_until(lambda: not task.cancel_event.is_set(), message="worker 的结论没有落地")
    assert task.state is QueueState.CANCELLED


def test_a_cancelled_task_is_not_retried_by_itself(
    queues: Callable[..., DownloadQueue], no_backoff: None
) -> None:
    """Cancelling must not be undone by the automatic retry."""

    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL, max_attempts=3)
    _wait_until(lambda: task.state is QueueState.RUNNING)

    queue.cancel(task.task_id)
    fake.gate.set()
    _wait_until(lambda: task.state is QueueState.CANCELLED)

    time.sleep(0.4)
    assert len(fake.calls) == 1, "已取消的任务被自动重试了"


# -- persistence across a restart --------------------------------------------


def test_unfinished_tasks_are_restored_on_start(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    first = queues(FakeDownload())
    kept = first.submit(URL, "720p")  # never started, so it stays pending
    first.stop()

    fake = FakeDownload()
    second = queues(fake)
    second.start()

    # The restored task is a fresh object owned by the new queue; the old one
    # is only useful for its id.
    restored = second.get(kept.task_id)
    assert restored is not None, "重启后没有恢复未完成任务"
    _wait_until(lambda: fake.calls == [kept.task_id], message="恢复的任务未被执行")
    _wait_until(lambda: restored.state.finished)
    assert restored.quality == "720p"


def test_a_row_left_running_comes_back_as_pending(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    """A 'running' row means the process died mid-download, not that it failed."""

    with QueueStore(settings) as store:
        store.upsert(
            QueueRow(
                task_id="interrupted",
                url=URL,
                state="running",
                attempts=1,
                max_attempts=3,
                position=1,
            )
        )

    fake = FakeDownload()
    queue = queues(fake)
    queue.start()

    task = queue.get("interrupted")
    assert task is not None
    _wait_until(lambda: task.state.finished, message="中断的任务未被重新执行")
    assert task.attempts == 2, "重试次数应当延续，而不是从零开始"


def test_finished_rows_are_loaded_for_display_only(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    with QueueStore(settings) as store:
        store.upsert(QueueRow(task_id="done", url=URL, state="completed", position=1))

    fake = FakeDownload()
    queue = queues(fake)
    queue.start()
    time.sleep(0.3)

    assert fake.calls == [], "已完成的任务不应重新下载"
    assert queue.get("done") is not None


def test_state_changes_are_persisted(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    queue = queues(FakeDownload())
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state.finished)

    with QueueStore(settings) as store:
        rows = {row.task_id: row for row in store.load_all()}
    assert rows[task.task_id].state == "completed"
    assert rows[task.task_id].title == "示例视频"


# -- listing helpers ---------------------------------------------------------


def test_queue_position_counts_only_unfinished_tasks(
    queues: Callable[..., DownloadQueue]
) -> None:
    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.start()

    first = queue.submit(URL)
    _wait_until(lambda: first.state is QueueState.RUNNING)
    second = queue.submit(f"{URL}?p=2")
    third = queue.submit(f"{URL}?p=3")

    assert queue.queue_position(first.task_id) == 1
    assert queue.queue_position(second.task_id) == 2
    assert queue.queue_position(third.task_id) == 3

    queue.cancel(second.task_id)
    assert queue.queue_position(third.task_id) == 2

    fake.gate.set()
    _wait_until(lambda: first.state.finished)
    assert queue.queue_position(first.task_id) is None


def test_counts_summarise_every_state(queues: Callable[..., DownloadQueue]) -> None:
    queue = queues(FakeDownload())
    queue.submit(URL)
    queue.submit(f"{URL}?p=2")

    counts = queue.counts()
    assert counts[QueueState.PENDING.value] == 2
    assert counts[QueueState.RUNNING.value] == 0


def test_clear_finished_keeps_the_live_queue(
    queues: Callable[..., DownloadQueue]
) -> None:
    fake = FakeDownload()
    queue = queues(fake)
    queue.start()
    done = queue.submit(URL)
    _wait_until(lambda: done.state.finished)
    waiting = queue.submit(f"{URL}?p=2")

    removed = queue.clear_finished()

    assert removed == 1
    assert [task.task_id for task in queue.tasks()] == [waiting.task_id]


def test_stopping_requeues_a_running_task_for_the_next_start(
    queues: Callable[..., DownloadQueue]
) -> None:
    """Closing the window must leave work to do, not silently drop it.

    A shutdown is not the user pausing, so the task goes back to *pending* and
    the next start continues it from its partial file. It used to be marked
    paused - which the restore path deliberately keeps paused - making a clean
    close worse than a crash, since a crash's ``running`` row was resumed.
    """

    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING)

    queue.stop(timeout=5)

    assert task.state is QueueState.PENDING
    assert task.error is None
    assert queue.running is False


# -- eta ---------------------------------------------------------------------


def test_eta_uses_size_and_speed() -> None:
    assert eta_seconds({"total": 1000, "downloaded": 400, "speed_bps": 100.0}) == 6.0


@pytest.mark.parametrize(
    "snapshot",
    [
        {"total": None, "downloaded": 100, "speed_bps": 50.0},
        {"total": 1000, "downloaded": 100, "speed_bps": 0.0},
        {"total": 1000, "downloaded": 1000, "speed_bps": 50.0},
    ],
)
def test_eta_is_none_when_it_cannot_be_known(snapshot: dict) -> None:
    assert eta_seconds(snapshot) is None


# -- the pause() race --------------------------------------------------------
#
# ``pause()`` used to read the task's state, decide, and write the result
# without holding the queue lock. A worker claims a task by flipping it to
# RUNNING while holding that same lock, so a start could slip between the check
# and the write: the pause was then overwritten by the start, or a task the
# worker had just finished was dragged back to ``paused`` and downloaded again
# on the next resume.
#
# Starting threads and hoping the scheduler lines them up is a coin flip, so
# these tests pin the moment down instead: the task announces the exact instant
# ``pause()`` marks it, which makes everything the method does afterwards
# observable. The verdict never depends on timing.


class _PauseWindowTask(QueueTask):
    """A task that reports the instant ``pause()`` marks it for stopping."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "pause_requested" and value is True:
            hook = self.__dict__.get("_pause_window")
            if hook is not None:
                hook(self)
        super().__setattr__(name, value)


def _observe_pause_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``submit`` build tasks that report their pause window."""

    monkeypatch.setattr(queue_module, "QueueTask", _PauseWindowTask)


def _another_thread_can_enter(lock: threading.RLock, timeout: float = 0.5) -> bool:
    """Whether a second thread can take ``lock`` while the caller holds it."""

    entered: list[bool] = []

    def probe() -> None:
        if lock.acquire(timeout=timeout):
            lock.release()
            entered.append(True)

    thread = threading.Thread(target=probe, name="vd-test-lock-probe")
    thread.start()
    thread.join(timeout + 5)
    return bool(entered)


def test_pause_decides_while_holding_the_queue_lock(
    monkeypatch: pytest.MonkeyPatch, queues: Callable[..., DownloadQueue]
) -> None:
    """The state check and the state write sit in one critical section.

    A second thread tries to enter the queue's critical section at exactly the
    moment between pause()'s check and its write. Holding the lock there is
    what stops a worker from claiming the task in between.
    """

    _observe_pause_window(monkeypatch)
    queue = queues(FakeDownload())  # not started: only pause() runs
    task = queue.submit(URL)
    assert isinstance(task, _PauseWindowTask), "测试没有拦到 pause() 的决策点"

    reached = threading.Event()
    intruder_entered = threading.Event()

    def hook(_task: QueueTask) -> None:
        reached.set()
        if _another_thread_can_enter(queue._lock):
            intruder_entered.set()

    task._pause_window = hook
    assert queue.pause(task.task_id) is True

    assert reached.is_set(), "pause() 没有走到被观测的位置，测试需要更新"
    assert not intruder_entered.is_set(), "pause() 在决定任务状态时没有持有队列锁"
    assert task.state is QueueState.PAUSED


def test_pause_persists_after_releasing_the_queue_lock(
    monkeypatch: pytest.MonkeyPatch, queues: Callable[..., DownloadQueue]
) -> None:
    """A disk write must not happen under the queue lock.

    Persisting inside it would stall every worker behind a slow disk, so the
    state is decided first and the row saved once the lock is back down.
    """

    queue = queues(FakeDownload())
    task = queue.submit(URL)

    observed: list[bool] = []
    persist = queue._persist

    def spy(record: QueueTask) -> None:
        observed.append(_another_thread_can_enter(queue._lock))
        persist(record)

    monkeypatch.setattr(queue, "_persist", spy)
    assert queue.pause(task.task_id) is True

    assert observed == [True], "持久化时仍持有队列锁（或者压根没有持久化）"


def test_a_pause_is_not_undone_by_a_worker_ready_to_claim_the_task(
    monkeypatch: pytest.MonkeyPatch, queues: Callable[..., DownloadQueue]
) -> None:
    """The outcome, not just the lock.

    One task holds the only slot, so the worker is busy and the target stays
    pending. The instant pause() marks the target, the test frees the slot and
    the worker goes straight for it. Deciding outside the lock let the worker
    claim the target in that window and download it, so the pause the user
    asked for was silently ignored.
    """

    _observe_pause_window(monkeypatch)

    claimed = threading.Event()
    watched: list[str] = [""]

    class WatchingDownload(FakeDownload):
        def __call__(self, task, progress: dict, cancel_event: threading.Event):
            if task.task_id == watched[0]:
                claimed.set()
            return super().__call__(task, progress, cancel_event)

    fake = WatchingDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.set_max_concurrent(1)
    queue.start()

    blocker = queue.submit("https://www.bilibili.com/video/BV1GJ411x7h8")
    _wait_until(lambda: blocker.state is QueueState.RUNNING, message="占用槽位的任务未开始")
    target = queue.submit(URL)
    watched[0] = target.task_id
    assert target.state is QueueState.PENDING, "目标任务应当还在排队"

    def hook(_task: QueueTask) -> None:
        # Inside pause(), between its check and its write: let the worker loose.
        # On the fixed code it cannot claim the target until this critical
        # section ends, and by then the task is already paused - so the wait
        # simply runs out. It is a bound, not a race: the claim is impossible,
        # not merely unlikely.
        fake.gate.set()
        claimed.wait(timeout=1.0)

    target._pause_window = hook
    assert queue.pause(target.task_id) is True

    assert target.state is QueueState.PAUSED
    assert not claimed.is_set(), "worker 在 pause() 的决策窗口内抢走了任务"
    assert target.task_id not in fake.calls, "被暂停的任务仍然被下载了"


# -- stability under concurrency ---------------------------------------------
#
# The run token and the ``claimed`` marker are what keep a worker's conclusion
# from landing on a task the user has already moved on. These tests drive them
# through the five ways a task can be interrupted *while other work is in
# flight*, because that is where the scheduler's own bookkeeping (``_active``,
# the queue order, the per-task claim) can go wrong in ways a single-task test
# cannot see.
#
# Every wait is on an Event or on ``_wait_until`` (a condition poll) - never a
# fixed sleep. FakeDownload is reused wherever its single shared gate can
# express the sequence; the one case it cannot is called out inline.


def test_cancelling_one_task_does_not_disturb_the_others(
    queues: Callable[..., DownloadQueue]
) -> None:
    """Cancel + retry one running task while two others are downloading."""

    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.set_max_concurrent(2)
    queue.start()

    first = queue.submit(f"{URL}?p=1")
    second = queue.submit(f"{URL}?p=2")
    third = queue.submit(f"{URL}?p=3")
    _wait_until(lambda: fake.running == 2, message="两个任务没有同时开始")

    assert queue.cancel(first.task_id) is True
    assert queue.retry(first.task_id) is True
    # The run that still holds it keeps it out of circulation, so no third
    # transfer of the same file can start behind the first one's back.
    with queue._condition:
        handed_out = queue._next_task()
    assert handed_out is not first, "旧一轮还没放手，任务就已经被派发了"

    fake.gate.set()
    _wait_until(
        lambda: all(task.state is QueueState.COMPLETED for task in (first, second, third)),
        message="并发下取消后重试没有全部完成",
    )

    # The retried task ran twice; the other two were untouched by the cancel.
    assert fake.calls.count(first.task_id) == 2
    assert fake.calls.count(second.task_id) == 1
    assert fake.calls.count(third.task_id) == 1
    assert queue.counts()["completed"] == 3


def test_pausing_one_task_leaves_the_others_running(
    queues: Callable[..., DownloadQueue]
) -> None:
    """A pause/resume round trip while another task keeps downloading."""

    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.set_max_concurrent(2)
    queue.start()

    paused = queue.submit(f"{URL}?p=1")
    other = queue.submit(f"{URL}?p=2")
    _wait_until(lambda: fake.running == 2, message="两个任务没有同时开始")

    assert queue.pause(paused.task_id) is True
    _wait_until(lambda: paused.state is QueueState.PAUSED, message="任务未进入暂停")
    # The pause is the paused task's business only.
    assert other.state is QueueState.RUNNING, "暂停一个任务影响了另一个"

    assert queue.resume(paused.task_id) is True
    _wait_until(lambda: paused.state is QueueState.RUNNING, message="继续后没有重新开跑")

    fake.gate.set()
    _wait_until(
        lambda: paused.state is QueueState.COMPLETED and other.state is QueueState.COMPLETED,
        message="暂停恢复后没有全部完成",
    )
    assert fake.calls.count(paused.task_id) == 2, "继续应当重新执行该任务"
    assert fake.calls.count(other.task_id) == 1


def test_a_network_failure_retry_and_a_manual_retry_coexist(
    queues: Callable[..., DownloadQueue], no_backoff: None
) -> None:
    """One task retries itself after a failure while another is retried by hand."""

    calls: list[str] = []
    lock = threading.Lock()
    gate = threading.Event()
    failing: list[str] = [""]

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        with lock:
            calls.append(task.task_id)
            attempt = calls.count(task.task_id)
        gate.wait(10)
        if task.task_id == failing[0] and attempt == 1:
            raise DownloadError("模拟网络失败")
        return _completed_result()

    queue = queues(executor)
    queue.set_max_concurrent(2)
    queue.start()

    flaky = queue.submit(f"{URL}?p=1")
    failing[0] = flaky.task_id
    manual = queue.submit(f"{URL}?p=2")
    _wait_until(lambda: len(calls) == 2, message="两个任务没有同时开始")

    assert queue.cancel(manual.task_id) is True
    assert queue.retry(manual.task_id) is True

    gate.set()
    _wait_until(
        lambda: flaky.state is QueueState.COMPLETED and manual.state is QueueState.COMPLETED,
        message="失败自动重试与手动重试没能各自完成",
    )

    assert calls.count(flaky.task_id) == 2, "网络失败应当自动重试一次"
    assert calls.count(manual.task_id) == 2, "手动重试应当重新执行一次"
    assert flaky.error is None
    assert manual.error is None


def test_stopping_while_a_retried_task_is_still_held_keeps_it_pending(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    """A retried task must survive a shutdown as pending, not as cancelled.

    Its previous run is still unwinding when the window closes, so the shutdown
    has to reach that run too - otherwise the task would be recorded as a
    cancellation the user never asked for.
    """

    fake = FakeDownload()
    fake.gate = threading.Event()
    first = queues(fake)
    first.set_max_concurrent(1)
    first.start()

    task = first.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert first.cancel(task.task_id) is True
    assert first.retry(task.task_id) is True

    first.stop(timeout=10)

    assert first.running is False
    assert task.state is QueueState.PENDING, "关机把重试写成了取消"

    # And the next start picks it up again.
    resumed = queues(FakeDownload())
    resumed.start()
    restored = resumed.get(task.task_id)
    assert restored is not None, "重启后没有恢复任务"
    _wait_until(lambda: restored.state is QueueState.COMPLETED, message="重启后没有继续下载")


def test_cancelling_exactly_as_the_run_completes_keeps_the_completion(
    queues: Callable[..., DownloadQueue]
) -> None:
    """Cancelling a transfer that has already finished does not undo it.

    ``cancel()`` marks the task at once, but the transfer it was interrupting
    has already produced a complete file - and the history row says so. Writing
    ``cancelled`` over that would leave the window disagreeing with the file on
    disk, so the completion stands. What matters is that the run still owns the
    task, and it does: ``cancel()`` deliberately does not bump the run id.
    """

    ready = threading.Event()
    go = threading.Event()

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        ready.set()
        go.wait(10)
        return _completed_result()

    queue = queues(executor)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: ready.is_set(), message="传输没有开始")

    assert queue.cancel(task.task_id) is True
    assert task.state is QueueState.CANCELLED, "cancel() 应当立刻标记"

    go.set()
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="已完成的结果被取消覆盖了")

    assert task.result is not None
    # A settled run clears the event, so nothing is left armed for a later run.
    assert task.cancel_event.is_set() is False


def test_restarting_the_queue_does_not_inherit_the_shutdown_flags(
    queues: Callable[..., DownloadQueue]
) -> None:
    """A shutdown's flags belong to the run it interrupted, not to the next one.

    ``stop()`` marks the running task ``interrupted`` and arms its cancel event.
    A later ``start()`` on the same queue used to hand both straight to the new
    run, so that run's first cancel looked like another shutdown: the task went
    back to pending, was claimed again, and span like that without ever settling
    - one executor call per poll interval, for ever.
    """

    fake = FakeDownload()
    fake.gate = threading.Event()
    queue = queues(fake)
    queue.set_max_concurrent(1)
    queue.start()

    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    queue.stop(timeout=10)
    assert task.state is QueueState.PENDING, "关机应当把任务放回排队"

    queue.start()
    _wait_until(lambda: task.state is QueueState.RUNNING, message="重启后没有重新开跑")

    # The new run has to be a clean one.
    assert task.interrupted is False, "关机标记泄漏到了新一轮"
    assert task.cancel_event.is_set() is False, "关机留下的取消事件没有清掉"

    fake.gate.set()
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="新一轮没能正常完成")


def test_a_retry_leaves_the_cancel_event_armed_for_the_run_it_interrupts(
    queues: Callable[..., DownloadQueue]
) -> None:
    """``retry()`` must not clear the cancel event out from under its own run.

    ``cancel()`` marks the task at once but lets the transfer unwind on its own,
    so ``retry()`` is normally called while that run is still going. The run id
    safely discards whatever that run concludes - but clearing the cancel event
    here tells the run the user changed their mind, so the transfer the user
    just cancelled keeps going and finishes the whole file. Only the claim can
    clear the event, because that is the first moment a *new* run could use it.

    The executor parks on an ``Event`` and records what the cancel event said
    when it finally woke up, which makes the ordering deterministic: the release
    happens after ``retry()``, so the reading is from the interrupted run.
    """

    saw_cancel: list[bool] = []
    release = threading.Event()

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        release.wait(10)
        saw_cancel.append(cancel_event.is_set())
        if len(saw_cancel) == 1:
            raise DownloadCancelled("已取消")
        return _completed_result()

    queue = queues(executor)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state is QueueState.RUNNING, message="任务未开始")

    assert queue.cancel(task.task_id) is True
    assert queue.retry(task.task_id) is True
    release.set()

    _wait_until(lambda: saw_cancel, message="被中断的 run 没有退出")
    assert saw_cancel[0] is True, "retry() 把还在收尾的 run 的取消信号清掉了"

    # And the retried run still gets to start, which is the point of the retry.
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="重试后未能完成")
    assert task.attempts == 2
