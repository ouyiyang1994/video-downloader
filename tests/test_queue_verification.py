"""Targeted verification of the download queue (Windows, v1.22).

``test_queue.py`` drives the scheduler with a scriptable fake. This file checks
the behaviours the queue exists *for*, and where it matters it uses a real
ranged HTTP transfer, so "resume does not corrupt the file" is proven against
actual bytes on disk rather than asserted about a stub agreeing with itself.

Section 6 answers the one question a fake cannot: whether five *real* engines,
each with its own event loop, connection and file handle, respect the
concurrency cap and leave each other's files alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import socket
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from config.constants import MAX_CONCURRENT_LIMIT
from config.settings import Settings
from core import queue as queue_module
from core.downloader import HttpDownloader, ProgressUpdate
from core.exceptions import DownloadCancelled, DownloadError
from core.models import DownloadResult, DownloadStatus, MediaStream, Platform
from core.queue import DownloadQueue, QueueState, QueueTask
from core.queue_store import QueueRow, QueueStore

URL = "https://www.bilibili.com/video/BV1GJ411x7h7"

#: 512 KiB, non-repeating, so a corrupted or shifted resume is visible.
PAYLOAD = bytes(range(256)) * 2048

#: A bounded 32 MiB transfer: large enough to expose whole-response buffering,
#: small enough to finish quickly on a local machine.
LARGE_PAYLOAD_SIZE = 32 * 1024 * 1024
LARGE_CANCEL_OFFSETS = (3 * 1024 * 1024, 11 * 1024 * 1024, 23 * 1024 * 1024)
LARGE_PATH = "/media/long-stream.bin"
LARGE_CHUNK_SIZE = 64 * 1024
LARGE_CHUNK_DELAY = 0.01

#: Safety valve for the opt-in mid-response gate: a handler parked by
#: ``_FleetHandler.hold_path`` gives up after this long so a test that dies
#: mid-way cannot leak a blocked thread into the rest of the session.
HOLD_TIMEOUT_SECONDS = 60.0

#: How far into its body a held response is parked.  Late enough that the
#: client - which reads in ``Settings.chunk_size`` buffers - has already
#: flushed bytes into ``.part``, early enough that a quarter of the response is
#: still behind it, which is what makes the interruption land mid-transfer.
HOLD_AFTER_FRACTION = 0.75

#: The stub's send buffer.  A held response is released and then raced by the
#: client's cancel: with a default buffer the kernel absorbs the whole
#: remainder, so the handler finishes writing a response the client has already
#: abandoned, and "the interrupted transfer delivered less than the payload"
#: becomes a race against socket buffering rather than a property of the
#: download (it failed exactly that way under load, with the client's ``.part``
#: still partial).  Bounded, ``send()`` blocks until the client reads or the
#: connection breaks, so the byte counters measure the download again.
STUB_SEND_BUFFER_BYTES = 8 * 1024

#: Range headers the test server has seen, cleared by the fixture.
_RANGES: list[str] = []


def _wait_until(predicate: Callable[[], bool], timeout: float = 25.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message or "等待条件超时")


def _bound_send_buffer(connection: socket.socket) -> None:
    """Keep the kernel from absorbing a response the client stopped reading.

    See :data:`STUB_SEND_BUFFER_BYTES` for why. A stub that cannot shrink its
    buffer is still usable, so a refusal is not fatal.
    """

    try:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, STUB_SEND_BUFFER_BYTES)
    except OSError:  # pragma: no cover - platform refusal
        return


class BlockingDownload:
    """A fake download that parks inside the executor until released."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.running = 0
        self.max_running = 0
        self.calls: list[str] = []
        self.started = threading.Semaphore(0)
        self._lock = threading.Lock()

    def __call__(self, task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        with self._lock:
            self.calls.append(task.task_id)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.started.release()
        try:
            deadline = time.monotonic() + 25
            while not self.gate.is_set():
                if cancel_event.is_set():
                    raise DownloadCancelled("已取消")
                if time.monotonic() > deadline:
                    raise DownloadCancelled("测试等待超时")
                time.sleep(0.01)
            return DownloadResult(
                status=DownloadStatus.COMPLETED,
                platform=Platform.BILIBILI,
                video_id="BV1GJ411x7h7",
                title="示例视频",
                bytes_downloaded=1,
            )
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """Smaller read chunks, so a pause or cancel lands *during* the transfer.

    ``HttpDownloader`` checks the cancel event once per yielded chunk, and its
    chunk floor is 64 KiB. With the default 1 MiB chunk a 512 KiB payload
    arrives in a single piece, so the transfer can never be interrupted - which
    would make the pause and cancel tests pass for the wrong reason.
    """

    settings.chunk_size = 64 * 1024
    return settings


@pytest.fixture
def queues(settings: Settings) -> Iterator[Callable[..., DownloadQueue]]:
    created: list[DownloadQueue] = []

    def factory(executor: Any, **kwargs: Any) -> DownloadQueue:
        queue = DownloadQueue(settings, executor=executor, **kwargs)
        created.append(queue)
        return queue

    yield factory
    for queue in created:
        queue.stop(timeout=5)


# --- 1. concurrency ---------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_the_running_count_never_exceeds_the_limit(
    queues: Callable[..., DownloadQueue], limit: int
) -> None:
    """A limit of N means at most N transfers, for every legal N."""

    fake = BlockingDownload()
    queue = queues(fake)
    queue.set_max_concurrent(limit)
    queue.start()

    tasks = [queue.submit(f"{URL}?p={n}") for n in range(1, 7)]

    for _ in range(limit):
        assert fake.started.acquire(timeout=10), f"并发 {limit} 时应有 {limit} 个任务在跑"
    time.sleep(0.4)
    assert fake.running == limit, f"并发上限 {limit}，实际在跑 {fake.running}"
    assert fake.max_running == limit, "并发上限被突破"

    fake.gate.set()
    _wait_until(lambda: all(task.state.finished for task in tasks), message="任务未全部结束")
    assert fake.max_running == limit, "运行过程中并发上限被突破"
    assert len(fake.calls) == 6


def test_lowering_the_limit_does_not_kill_running_transfers(
    queues: Callable[..., DownloadQueue]
) -> None:
    fake = BlockingDownload()
    queue = queues(fake)
    queue.set_max_concurrent(3)
    queue.start()
    tasks = [queue.submit(f"{URL}?p={n}") for n in range(1, 4)]
    for _ in range(3):
        assert fake.started.acquire(timeout=10)

    queue.set_max_concurrent(1)
    time.sleep(0.3)

    # The three already running are allowed to finish; nothing new starts.
    assert fake.running == 3
    fake.gate.set()
    _wait_until(lambda: all(task.state.finished for task in tasks))


# --- 2. pause / resume, against a real ranged transfer ----------------------


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves :data:`PAYLOAD` with Range support, slowly enough to interrupt."""

    protocol_version = "HTTP/1.1"
    chunk_size = 16 * 1024
    delay = 0.04

    def setup(self) -> None:
        super().setup()
        _bound_send_buffer(self.connection)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        return None

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        total = len(PAYLOAD)
        header = self.headers.get("Range") or ""
        _RANGES.append(header)

        start = 0
        status = 200
        if header.startswith("bytes="):
            first = header[len("bytes=") :].split(",")[0].partition("-")[0]
            if first.isdigit():
                start = int(first)
                status = 206

        body = PAYLOAD[start:]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        self.end_headers()

        for offset in range(0, len(body), self.chunk_size):
            try:
                self.wfile.write(body[offset : offset + self.chunk_size])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(self.delay)


@pytest.fixture
def ranged_server() -> Iterator[str]:
    """A local HTTP server that honours Range, throttled so it can be paused."""

    _RANGES.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/media.bin"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _transfer_executor(
    settings: Settings,
    *,
    url_for: Callable[[QueueTask], str],
    destination_for: Callable[[QueueTask], Path],
):
    """An executor running the *real* engine, so resume is genuinely exercised.

    Both the URL and the destination are derived from the task, which is what
    lets the concurrency test give every task its own file and its own bytes -
    so a transfer that wrote another task's payload would be visible as a
    mismatched file rather than hidden behind a shared fixture.
    """

    def executor(task: QueueTask, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        url = url_for(task)
        destination = destination_for(task)

        async def run() -> Any:
            # One event loop for the client's whole life. httpx binds its
            # connections to the loop that created them, so closing the client
            # from a second ``asyncio.run()`` raises "Event loop is closed".
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                downloader = HttpDownloader(settings, client, cancel_event)
                stream = MediaStream(
                    url=url, quality_label="test", size=len(PAYLOAD), format_id="test"
                )
                return await downloader.download_stream(
                    stream, destination, progress=progress["video"]
                )

        outcome = asyncio.run(run())
        return DownloadResult(
            status=DownloadStatus.COMPLETED,
            platform=Platform.BILIBILI,
            video_id="BV1GJ411x7h7",
            title="示例视频",
            video_path=outcome.path,
            bytes_downloaded=outcome.bytes_downloaded,
        )

    return executor


def _real_transfer_executor(settings: Settings, url: str, destination: Path):
    """A :func:`_transfer_executor` pinned to a single URL and destination."""

    return _transfer_executor(
        settings,
        url_for=lambda task: url,
        destination_for=lambda task: destination,
    )


def _part_of(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part")


def test_pause_keeps_the_partial_file_and_resume_finishes_it_correctly(
    settings: Settings, tmp_path: Path, ranged_server: str
) -> None:
    """The whole point of pause: stop, keep what arrived, then carry on."""

    destination = tmp_path / "media.bin"
    queue = DownloadQueue(
        settings, executor=_real_transfer_executor(settings, ranged_server, destination)
    )
    try:
        queue.set_max_concurrent(1)
        queue.start()
        task = queue.submit(URL, output_dir=str(tmp_path))
        part = _part_of(destination)

        _wait_until(lambda: part.exists() and part.stat().st_size > 0, message="没有产生分片文件")
        assert queue.pause(task.task_id) is True
        _wait_until(lambda: task.state is QueueState.PAUSED, message="任务未进入暂停")

        partial_size = part.stat().st_size
        assert 0 < partial_size < len(PAYLOAD), "暂停时应当只下载了一部分"
        assert not destination.exists(), "暂停时不应生成最终文件"

        # Progress must stop advancing while paused.
        frozen = task.snapshot()["downloaded"]
        time.sleep(0.6)
        assert task.snapshot()["downloaded"] == frozen, "暂停后进度仍在推进"

        assert queue.resume(task.task_id) is True
        _wait_until(lambda: task.state.finished, message="继续后未完成")
        assert task.state is QueueState.COMPLETED

        resumed = [item for item in _RANGES if item.startswith("bytes=")]
        assert resumed, "继续时没有发出 Range 请求，说明文件被从头重下"
        assert int(resumed[-1][len("bytes=") :].split("-")[0]) >= partial_size
        assert destination.read_bytes() == PAYLOAD, "最终文件与源文件不一致"
        assert not part.exists(), "完成后不应残留分片文件"
    finally:
        queue.stop(timeout=10)


def test_cancelling_stops_the_transfer_and_leaves_no_final_file(
    settings: Settings, tmp_path: Path, ranged_server: str
) -> None:
    destination = tmp_path / "media.bin"
    queue = DownloadQueue(
        settings, executor=_real_transfer_executor(settings, ranged_server, destination)
    )
    try:
        queue.set_max_concurrent(1)
        queue.start()
        task = queue.submit(URL, output_dir=str(tmp_path))
        part = _part_of(destination)
        _wait_until(lambda: part.exists() and part.stat().st_size > 0)

        assert queue.cancel(task.task_id) is True
        _wait_until(lambda: task.state is QueueState.CANCELLED, message="任务未被取消")
        # ``cancel()`` marks the task cancelled straight away, while the transfer
        # is still unwinding. Waiting for the worker to actually let go of it is
        # what makes the retry below deterministic: a fixed sleep was not enough
        # under load, and retrying mid-unwind let the two race.
        _wait_until(lambda: queue._active == 0, message="传输没有停下来")
        assert task.state is QueueState.CANCELLED, "取消后状态被改回"
        assert not destination.exists(), "取消不应产出最终文件"

        # Retry must be able to run it again, and this time to completion.
        assert queue.retry(task.task_id) is True
        _wait_until(lambda: task.state.finished, message="重试后未结束")
        assert task.state is QueueState.COMPLETED
        assert destination.read_bytes() == PAYLOAD
    finally:
        queue.stop(timeout=10)


def test_retry_re_executes_a_failed_task(queues: Callable[..., DownloadQueue]) -> None:
    attempts: list[int] = []

    def flaky(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        attempts.append(1)
        if len(attempts) == 1:
            raise DownloadError("第一次失败")
        return DownloadResult(
            status=DownloadStatus.COMPLETED,
            platform=Platform.BILIBILI,
            video_id="BV1",
            title="ok",
        )

    queue = queues(flaky)
    queue.start()
    task = queue.submit(URL, max_attempts=1)
    _wait_until(lambda: task.state is QueueState.FAILED, message="未按预期失败")

    assert queue.retry(task.task_id) is True
    _wait_until(lambda: task.state is QueueState.COMPLETED, message="重试未成功")
    assert len(attempts) == 2
    assert task.error is None


# --- 3. playlists -----------------------------------------------------------


class _StubYDL:
    """Stands in for ``yt_dlp.YoutubeDL``: records options, replays a canned result."""

    instances: list[_StubYDL] = []
    response: Any = None

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        type(self).instances.append(self)

    def __enter__(self) -> _StubYDL:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> Any:
        return type(self).response


@pytest.fixture
def stub_ytdlp(monkeypatch: pytest.MonkeyPatch) -> type[_StubYDL]:
    import yt_dlp

    _StubYDL.instances = []
    _StubYDL.response = None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _StubYDL)
    return _StubYDL


def test_a_playlist_is_enumerated_in_order(
    settings: Settings, stub_ytdlp: type[_StubYDL]
) -> None:
    """Entries keep the playlist's order and are numbered from 1."""

    from core.engine_ytdlp import YtDlpEngine

    stub_ytdlp.response = {
        "entries": [
            {"id": "one", "url": "https://example.test/1", "title": "第一"},
            {"id": "two", "url": "https://example.test/2", "title": "第二"},
            {"id": "three", "url": "https://example.test/3", "title": "第三"},
        ]
    }

    result = asyncio.run(YtDlpEngine(settings).extract_playlist("https://example.test/list"))

    assert [item.url for item in result] == [
        "https://example.test/1",
        "https://example.test/2",
        "https://example.test/3",
    ]
    assert [item.index for item in result] == [1, 2, 3]
    assert [item.title for item in result] == ["第一", "第二", "第三"]
    assert [item.video_id for item in result] == ["one", "two", "three"]


def test_a_single_video_yields_no_playlist_entries(
    settings: Settings, stub_ytdlp: type[_StubYDL]
) -> None:
    """No ``entries`` key means "not a playlist", so the queue keeps one task."""

    from core.engine_ytdlp import YtDlpEngine

    stub_ytdlp.response = {"id": "solo", "title": "单个视频"}

    assert asyncio.run(YtDlpEngine(settings).extract_playlist("https://example.test/v/1")) == []


def test_a_flat_entry_without_a_title_borrows_the_listing_title(
    settings: Settings, stub_ytdlp: type[_StubYDL]
) -> None:
    """Bilibili's 分P entries carry no title; a blank row helps nobody.

    The listing title arrived with the same response, so using it costs no
    extra request - and an entry that *does* have its own title keeps it, which
    is what keeps YouTube unchanged.
    """

    from core.engine_ytdlp import YtDlpEngine

    stub_ytdlp.response = {
        "title": "多分P 投稿",
        "entries": [
            {"id": None, "url": "https://www.bilibili.com/video/BV1x?p=1", "title": None},
            {"id": None, "url": "https://www.bilibili.com/video/BV1x?p=2", "title": None},
            {"id": "named", "url": "https://example.test/3", "title": "自带标题"},
        ],
    }

    result = asyncio.run(YtDlpEngine(settings).extract_playlist("https://example.test/list"))

    assert [item.title for item in result] == ["多分P 投稿", "多分P 投稿", "自带标题"]


def test_the_playlist_options_ask_yt_dlp_for_a_flat_listing(
    settings: Settings, stub_ytdlp: type[_StubYDL]
) -> None:
    """Enumeration must be flat - one request, no per-video metadata."""

    from core.engine_ytdlp import YtDlpEngine

    stub_ytdlp.response = {"entries": []}
    asyncio.run(
        YtDlpEngine(settings).extract_playlist("https://example.test/list", limit=50)
    )

    options = stub_ytdlp.instances[0].options
    assert options["noplaylist"] is False
    assert options["extract_flat"] == "in_playlist"
    assert options["playlistend"] == 50
    assert options["skip_download"] is True


def test_a_playlist_link_is_expanded_into_one_task_per_entry(
    queues: Callable[..., DownloadQueue]
) -> None:
    """The queue preserves the enumeration order, and each entry is its own task."""

    fake = BlockingDownload()
    queue = queues(fake)
    entries = [f"{URL}?p={n}" for n in range(1, 6)]

    tasks = queue.submit_many(entries)

    assert [task.url for task in queue.tasks()] == entries
    assert [task.position for task in tasks] == [1, 2, 3, 4, 5]


def test_every_playlist_entry_downloads_like_a_single_video(
    queues: Callable[..., DownloadQueue]
) -> None:
    """Each entry is submitted on its own, so it resolves exactly as it would
    have if the user had pasted that link directly."""

    seen: list[str] = []

    def recorder(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        seen.append(task.url)
        return DownloadResult(
            status=DownloadStatus.COMPLETED,
            platform=Platform.BILIBILI,
            video_id="BV1",
            title=task.url,
        )

    queue = queues(recorder)
    queue.set_max_concurrent(1)
    queue.start()
    entries = [f"{URL}?p={n}" for n in range(1, 4)]
    tasks = queue.submit_many(entries)

    _wait_until(lambda: all(task.state.finished for task in tasks), message="未全部完成")

    assert seen == entries, "下载顺序与播放列表顺序不一致"
    assert all(task.state is QueueState.COMPLETED for task in tasks)


# --- 4. restart -------------------------------------------------------------


def test_a_restart_restores_states_and_order(
    settings: Settings, queues: Callable[..., DownloadQueue]
) -> None:
    """Two unfinished, one finished, one paused: the order must survive."""

    with QueueStore(settings) as store:
        store.upsert(QueueRow(task_id="a", url=f"{URL}?p=1", position=1, state="pending"))
        store.upsert(QueueRow(task_id="b", url=f"{URL}?p=2", position=2, state="running"))
        store.upsert(QueueRow(task_id="c", url=f"{URL}?p=3", position=3, state="completed"))
        store.upsert(QueueRow(task_id="d", url=f"{URL}?p=4", position=4, state="paused"))

    fake = BlockingDownload()
    queue = queues(fake)
    # One slot, so 'a' occupies it and 'b' is observably still waiting - which
    # is what proves the row left as ``running`` came back as work-to-do rather
    # than being lost or left stuck.
    queue.set_max_concurrent(1)
    queue.start()

    assert [task.task_id for task in queue.tasks()] == ["a", "b", "c", "d"], "重启后顺序变了"
    assert queue.get("a").state is QueueState.RUNNING
    assert queue.get("b").state is QueueState.PENDING, "running 行应恢复为排队中"
    assert queue.get("d").state is QueueState.PAUSED, "用户暂停的任务不应被自动跑起来"
    assert queue.get("c").state is QueueState.COMPLETED

    fake.gate.set()
    _wait_until(
        lambda: queue.get("a").state.finished and queue.get("b").state.finished,
        message="恢复的两个任务未完成",
    )
    assert set(fake.calls) == {"a", "b"}, "只应执行未完成的任务"
    assert queue.get("c").state is QueueState.COMPLETED
    assert queue.get("d").state is QueueState.PAUSED, "已暂停的任务被执行了"


def test_closing_and_reopening_continues_an_interrupted_download(
    settings: Settings, tmp_path: Path, ranged_server: str
) -> None:
    """A download interrupted by closing the app must continue, not restart.

    Regression guard: the shutdown path used to persist these as ``paused``,
    which the restore path keeps paused - so a clean close never continued,
    while a crash did.
    """

    destination = tmp_path / "media.bin"
    executor = _real_transfer_executor(settings, ranged_server, destination)
    part = _part_of(destination)

    first = DownloadQueue(settings, executor=executor)
    first.set_max_concurrent(1)
    first.start()
    task = first.submit(URL, output_dir=str(tmp_path))
    _wait_until(lambda: part.exists() and part.stat().st_size > 0)
    first.stop(timeout=10)  # as if the window were closed mid-download

    assert task.state is QueueState.PENDING, "关闭窗口后任务应仍是待办，而不是被暂停"
    partial_size = part.stat().st_size
    assert 0 < partial_size < len(PAYLOAD)

    # Reopen the application: same database, brand-new queue.
    _RANGES.clear()
    second = DownloadQueue(settings, executor=executor)
    try:
        second.start()
        restored = second.get(task.task_id)
        assert restored is not None, "重启后没有恢复未完成任务"
        _wait_until(lambda: restored.state.finished, message="重启后未继续下载")

        assert restored.state is QueueState.COMPLETED
        assert destination.read_bytes() == PAYLOAD, "续传后的文件与源文件不一致"
        resumed = [item for item in _RANGES if item.startswith("bytes=")]
        assert resumed, "重启后没有续传，而是从头下载"
        assert int(resumed[-1][len("bytes=") :].split("-")[0]) >= partial_size
    finally:
        second.stop(timeout=10)


# --- 5. what the window renders ---------------------------------------------


def test_progress_snapshot_carries_everything_the_window_shows(
    queues: Callable[..., DownloadQueue]
) -> None:
    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        progress["video"](
            ProgressUpdate(stream_kind="video", downloaded=512, total=2048, speed_bps=1024.0)
        )
        progress["audio"](
            ProgressUpdate(stream_kind="audio", downloaded=512, total=2048, speed_bps=1024.0)
        )
        return DownloadResult(
            status=DownloadStatus.COMPLETED,
            platform=Platform.BILIBILI,
            video_id="BV1",
            title="示例",
        )

    queue = queues(executor)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state.finished)

    snapshot = task.snapshot()
    assert snapshot["downloaded"] == 1024
    assert snapshot["total"] == 4096
    assert snapshot["fraction"] == 0.25
    assert snapshot["speed_bps"] == 2048.0
    assert snapshot["title"] == "示例"
    assert snapshot["platform"] == "bilibili"
    assert queue_module.eta_seconds(snapshot) == pytest.approx(1.5)


def test_a_task_with_no_known_total_shows_no_invented_fraction(
    queues: Callable[..., DownloadQueue]
) -> None:
    """Unknown size stays unknown: the window shows bytes, not a made-up %."""

    def executor(task, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        progress["video"](
            ProgressUpdate(stream_kind="video", downloaded=4096, total=None, speed_bps=0.0)
        )
        raise DownloadCancelled("停止")

    queue = queues(executor)
    queue.start()
    task = queue.submit(URL)
    _wait_until(lambda: task.state.finished)

    snapshot = task.snapshot()
    assert snapshot["total"] is None
    assert snapshot["fraction"] is None
    assert snapshot["downloaded"] == 4096
    assert queue_module.eta_seconds(snapshot) is None


# --- 6. the concurrency cap, against several real transfers at once -------------
#
# Section 1 proves the cap with a fake that parks inside the executor, which
# measures the *scheduler*. What is left unproven there is that five real
# engines, each with its own event loop, its own HTTP connection and its own
# file handle, actually respect it - and that nothing they do to one another
# (a cancelled transfer closing its socket, a retry re-opening a ``.part``)
# leaks across the other four.


class _FleetHandler(http.server.BaseHTTPRequestHandler):
    """Serves a distinct payload per path and records how many overlapped.

    Every task gets its own path, so its own 512 KiB derived from that path.
    Two tasks that were handed each other's bytes would write different content
    to disk, which is what makes "the files are right" a real check instead of
    five copies of the same constant.

    ``inflight`` is bumped for the whole request - the failure responses too,
    because a rejected request still occupied a queue slot - so ``max_inflight``
    is an upper bound on how many transfers the server ever saw at once.  It is
    deliberately *not* a lower bound on the client's concurrency: ``do_GET``
    sleeps one more ``delay`` after the final chunk before releasing, and a
    client that has already read its Content-Length is finished during that
    sleep, so the reading can exceed the real cap by one.  Assert on the
    executor spans (``_max_concurrent_spans``) when the cap itself is the
    question, and use the per-path counters for "was this file fetched twice".
    The per-path counters answer a different question, and it is the one the
    cancel / pause / retry races live or die by: ``path_max_inflight`` is how
    many transfers of *this one file* were ever running at the same time. Two
    runs writing one ``.part`` file - the duplicate download the ``claimed``
    marker exists to prevent - shows up there as 2.
    """

    protocol_version = "HTTP/1.1"
    chunk_size = 16 * 1024
    delay = 0.05

    def setup(self) -> None:
        super().setup()
        _bound_send_buffer(self.connection)

    _lock = threading.Lock()
    _inflight = 0
    _max_inflight = 0
    _requests: dict[str, int] = {}
    _fail_first: dict[str, int] = {}
    _path_inflight: dict[str, int] = {}
    _path_max_inflight: dict[str, int] = {}
    _ranges: dict[str, list[str]] = {}
    #: Bytes actually delivered, one entry per request, in request order. A
    #: transfer that was cancelled mid-flight delivers fewer bytes than the
    #: payload; one that ran to completion delivers exactly the payload, which
    #: is what makes this a threshold-free way to ask "did the cancel land?".
    _delivered: dict[str, list[int]] = {}
    _path_config: dict[str, tuple[int, int, float]] = {}
    #: Opt-in, one-shot mid-response gate.  A test that has to interrupt a
    #: transfer *while it is running* registers a path with ``hold_path``; the
    #: first request for that path is parked once
    #: ``HOLD_AFTER_FRACTION`` of its body is on the wire and stays parked until
    #: ``release_path`` runs.  That turns "the transfer is still in flight" from
    #: a race against a fixed-duration response into a property of the fixture:
    #: a starved test thread can no longer miss the window and then wait out a
    #: timeout on a condition that can never become true again.
    _holds: dict[str, threading.Event] = {}
    _hold_pending: set[str] = set()
    #: Set when a held response reaches its parking point.
    _parked: dict[str, threading.Event] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        return None

    @classmethod
    def reset(cls) -> None:
        cls.release_all_holds()
        with cls._lock:
            cls._inflight = 0
            cls._max_inflight = 0
            cls._requests = {}
            cls._fail_first = {}
            cls._path_inflight = {}
            cls._path_max_inflight = {}
            cls._ranges = {}
            cls._delivered = {}
            cls._path_config = {}

    @classmethod
    def hold_path(cls, path: str) -> None:
        """Park the first response for ``path`` part-way through its body.

        Call it *before* the request is made.  The response is parked once
        ``HOLD_AFTER_FRACTION`` of the body has been written and cannot finish
        until :meth:`release_path`, so a test that interrupts the transfer is
        guaranteed to land inside it.

        Parking that late is deliberate: the client reads in large buffers
        (``Settings.chunk_size`` is 1 MiB), so parking early would leave it
        with nothing to flush into its ``.part`` file and the test would wait
        forever on bytes that only arrive at the end of an unparked response.
        """

        with cls._lock:
            cls._holds[path] = threading.Event()
            cls._hold_pending.add(path)
            cls._parked[path] = threading.Event()

    @classmethod
    def release_path(cls, path: str) -> None:
        """Let a held response continue; later requests are not held again."""

        with cls._lock:
            cls._hold_pending.discard(path)
            event = cls._holds.pop(path, None)
        if event is not None:
            event.set()

    @classmethod
    def release_all_holds(cls) -> None:
        """Unpark every held response.

        Teardown must never leave a handler parked: the thread would outlive
        the test and show up in the next one's thread count.
        """

        with cls._lock:
            cls._hold_pending = set()
            events = list(cls._holds.values())
            cls._holds = {}
            cls._parked = {}
        for event in events:
            event.set()

    @classmethod
    def wait_parked(cls, path: str, timeout: float = 30.0) -> bool:
        """Block until a held response reaches its parking point."""

        with cls._lock:
            event = cls._parked.get(path)
        return bool(event is not None and event.wait(timeout))

    @classmethod
    def configure_path(
        cls, path: str, *, size: int, chunk_size: int, delay: float
    ) -> None:
        """Configure a streaming, path-derived payload without materializing it."""

        with cls._lock:
            cls._path_config[path] = (size, chunk_size, delay)

    @classmethod
    def fail_first(cls, path: str, times: int) -> None:
        """Make the next ``times`` requests to ``path`` answer HTTP 500."""

        with cls._lock:
            cls._fail_first[path] = times

    @classmethod
    def path_inflight(cls, path: str) -> int:
        """How many transfers of ``path`` are running right now."""

        with cls._lock:
            return cls._path_inflight.get(path, 0)

    @classmethod
    def stats(cls) -> dict[str, Any]:
        """A consistent copy of what the server has seen."""

        with cls._lock:
            return {
                "inflight": cls._inflight,
                "max_inflight": cls._max_inflight,
                "requests": dict(cls._requests),
                "fail_first": dict(cls._fail_first),
                "path_max_inflight": dict(cls._path_max_inflight),
                "ranges": {key: list(value) for key, value in cls._ranges.items()},
                "delivered": {key: list(value) for key, value in cls._delivered.items()},
                "path_config": dict(cls._path_config),
            }

    def _claim(self, path: str) -> bool:
        """Record the request and say whether this one must fail."""

        with type(self)._lock:
            type(self)._requests[path] = type(self)._requests.get(path, 0) + 1
            remaining = type(self)._fail_first.get(path, 0)
            if remaining > 0:
                type(self)._fail_first[path] = remaining - 1
                fail_now = True
            else:
                fail_now = False
            type(self)._inflight += 1
            type(self)._max_inflight = max(type(self)._max_inflight, type(self)._inflight)
            current = type(self)._path_inflight.get(path, 0) + 1
            type(self)._path_inflight[path] = current
            type(self)._path_max_inflight[path] = max(
                type(self)._path_max_inflight.get(path, 0), current
            )
        return fail_now

    def _release(self, path: str) -> None:
        with type(self)._lock:
            type(self)._inflight -= 1
            type(self)._path_inflight[path] -= 1

    def _arrive_at_gate(self, sent: int, body_size: int) -> None:
        """Announce (and, if this path is held, reach) the parking point.

        Parking is what makes the gate useful: the client already has real bytes
        to put in its ``.part`` file, and the response cannot finish until the
        test releases it.
        """

        held = False
        if body_size > 0 and sent >= body_size * HOLD_AFTER_FRACTION:
            with type(self)._lock:
                held = self.path in type(self)._hold_pending
                if held:
                    type(self)._hold_pending.discard(self.path)
                signal = type(self)._parked.get(self.path)
                event = type(self)._holds.get(self.path)
            if signal is not None:
                signal.set()
            if held and event is not None:
                # Bounded so a test that dies mid-way cannot park a handler
                # thread for the rest of the session; the normal path releases
                # at once.
                event.wait(HOLD_TIMEOUT_SECONDS)

    def _note_range(self, path: str, header: str) -> None:
        with type(self)._lock:
            type(self)._ranges.setdefault(path, []).append(header)

    def _record_delivered(self, path: str, sent: int) -> None:
        with type(self)._lock:
            type(self)._delivered.setdefault(path, []).append(sent)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        # One stream request per short-lived httpx client; closing explicitly
        # avoids a keep-alive read after clients have abandoned a cancelled body.
        self.close_connection = True
        fail_now = self._claim(self.path)
        sent = 0
        try:
            if fail_now:
                body = b""
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            config = type(self)._path_config.get(self.path)
            if config is None:
                total, chunk_size, delay = len(PAYLOAD), self.chunk_size, self.delay
            else:
                total, chunk_size, delay = config
            header = self.headers.get("Range") or ""
            self._note_range(self.path, header)
            start = 0
            status = 200
            if header.startswith("bytes="):
                first = header[len("bytes=") :].split(",")[0].partition("-")[0]
                if first.isdigit():
                    start = int(first)
                    status = 206

            if start >= total:
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return

            body_size = total - start
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(body_size))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
            self.end_headers()

            for offset in range(0, body_size, chunk_size):
                piece_size = min(chunk_size, body_size - offset)
                piece = _payload_chunk_for(self.path, start + offset, piece_size)
                try:
                    self.wfile.write(piece)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                sent += len(piece)
                self._arrive_at_gate(sent, body_size)
                if delay:
                    time.sleep(delay)
        finally:
            self._record_delivered(self.path, sent)
            self._release(self.path)


def _payload_chunk_for(path: str, offset: int, size: int) -> bytes:
    """Generate any slice of a path-specific payload without holding the file."""

    seed = hashlib.sha256(path.encode("utf-8")).digest()
    seed_offset = offset % len(seed)
    needed = seed_offset + size
    repeats = (needed + len(seed) - 1) // len(seed)
    return (seed * repeats)[seed_offset : seed_offset + size]


def _payload_for(path: str) -> bytes:
    """512 KiB that depends only on ``path``, so no two tasks share bytes."""

    return _payload_chunk_for(path, 0, len(PAYLOAD))


def _payload_digest(path: str, size: int) -> str:
    """Expected SHA-256 for a generated payload, using bounded memory."""

    digest = hashlib.sha256()
    chunk_size = 256 * 1024
    for offset in range(0, size, chunk_size):
        digest.update(_payload_chunk_for(path, offset, min(chunk_size, size - offset)))
    return digest.hexdigest()


def _start_real_stream(
    settings: Settings,
    url: str,
    destination: Path,
    *,
    size: int,
    cancel_event: threading.Event,
) -> tuple[threading.Thread, threading.Event, list[Any], list[BaseException]]:
    """Run one real HttpDownloader stream on its own loop/thread."""

    finished = threading.Event()
    outcomes: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        async def transfer() -> Any:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                downloader = HttpDownloader(settings, client, cancel_event)
                stream = MediaStream(
                    url=url, quality_label="large-test", size=size, format_id="large-test"
                )
                return await downloader.download_stream(stream, destination)

        try:
            outcomes.append(asyncio.run(transfer()))
        except BaseException as exc:  # captured for assertion in the test thread
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name="vd-large-http-test", daemon=True)
    thread.start()
    return thread, finished, outcomes, errors


@pytest.fixture
def fleet_server() -> Iterator[str]:
    """A local server that counts overlapping transfers and can fail on demand."""

    _FleetHandler.reset()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FleetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        # Unpark anything the test left on a gate before the server goes away,
        # so a failed test cannot leak a blocked handler thread into the next.
        _FleetHandler.release_all_holds()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries in tests must not wait the real five seconds."""

    monkeypatch.setattr(queue_module, "RETRY_BACKOFF_SECONDS", (0, 0, 0))


def test_five_real_transfers_stay_within_the_cap_and_keep_their_files_apart(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """The hard cap of five, with five genuinely overlapping engines.

    Six tasks and five slots, so "at most five" is measured rather than
    assumed, and the sixth wave proves the queue keeps working after a full
    slot cycle. The measurement is the executor's own spans; the server-side
    ``max_inflight`` is deliberately not used for the cap, because ``do_GET``
    holds its slot through one more ``delay`` after the final chunk, so a client
    that has already finished still counts as in flight and the reading can
    exceed the real cap by one (``logs/_probe-inflight-tail-sleep.py.txt``
    reproduces that by enlarging the trailing sleep).

    Every task writes its own path-derived bytes to its own file, so a task
    that ran twice, or that wrote another task's payload, shows up as a
    mismatched file instead of passing.
    """

    calls: list[str] = []
    spans: list[tuple[str, float, float]] = []
    lock = threading.Lock()

    def url_for(task: QueueTask) -> str:
        return f"{fleet_server}/media/{task.task_id}.bin"

    def destination_for(task: QueueTask) -> Path:
        return tmp_path / f"{task.task_id}.bin"

    inner = _transfer_executor(settings, url_for=url_for, destination_for=destination_for)

    def executor(task: QueueTask, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        started = time.monotonic()
        with lock:
            calls.append(task.task_id)
        try:
            return inner(task, progress, cancel_event)
        finally:
            with lock:
                spans.append((task.task_id, started, time.monotonic()))

    queue = DownloadQueue(settings, executor=executor)
    try:
        queue.set_max_concurrent(5)
        queue.start()
        tasks = [queue.submit(f"{URL}?p={index}") for index in range(1, 7)]

        _wait_until(
            lambda: all(task.state.finished for task in tasks),
            timeout=90,
            message="六个任务没有全部结束",
        )

        assert all(task.state is QueueState.COMPLETED for task in tasks)
        # Measured, not assumed: never a sixth transfer at once, and the cap was
        # really saturated - otherwise the first assertion would hold vacuously.
        overlap = _max_concurrent_spans(spans)
        assert overlap <= 5, f"实测最多 {overlap} 个传输同时进行，超过上限 5"
        assert overlap == 5, f"只观测到 {overlap} 个传输同时进行，上限没有被压满"
        # One executor call per task: no task ran twice behind another's back.
        assert sorted(calls) == sorted(task.task_id for task in tasks), "有任务被重复执行"
        assert len(calls) == len(set(calls)) == len(tasks)
        # Server-side cross-check: every file was fetched exactly once, and no
        # file ever had two requests running against it at the same time.
        stats = _FleetHandler.stats()
        assert set(stats["requests"].values()) == {1}, f"请求数异常：{stats['requests']}"
        assert set(stats["path_max_inflight"].values()) == {1}, (
            f"有文件被同时下载了两次：{stats['path_max_inflight']}"
        )
        # Each file holds exactly the bytes that were meant for it.
        for task in tasks:
            path = destination_for(task)
            assert path.stat().st_size == len(PAYLOAD), f"{path.name} 大小不对"
            assert path.read_bytes() == _payload_for(f"/media/{task.task_id}.bin"), (
                f"{path.name} 的内容与它自己的请求不符"
            )
        # Nothing left behind in the partial state.
        assert list(tmp_path.glob("*.part")) == [], "尚有残留的分片文件"
    finally:
        queue.stop(timeout=15)


def test_a_transient_http_error_is_retried_within_the_same_run(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """A one-off 500 is the engine's problem, and the engine retries it.

    ``max_retries`` leaves two attempts, so the queue must not hear about it at
    all: one task, one executor call, two HTTP requests, and a file that is
    byte-exact because the retry started from an empty ``.part``.
    """

    settings.max_retries = 2
    path = "/media/transient.bin"
    _FleetHandler.fail_first(path, 1)
    destination = tmp_path / "transient.bin"

    queue = DownloadQueue(
        settings,
        executor=_transfer_executor(
            settings,
            url_for=lambda task: f"{fleet_server}{path}",
            destination_for=lambda task: destination,
        ),
    )
    try:
        queue.set_max_concurrent(1)
        queue.start()
        task = queue.submit(URL)

        _wait_until(lambda: task.state.finished, timeout=60, message="任务没有结束")

        assert task.state is QueueState.COMPLETED
        assert task.attempts == 1, "一次成功的重试不应该变成两轮任务"
        assert _FleetHandler.stats()["requests"][path] == 2, "500 之后没有重新发起请求"
        assert destination.read_bytes() == _payload_for(path)
        assert not destination.with_name(destination.name + ".part").exists()
    finally:
        queue.stop(timeout=10)


def test_the_queue_retries_a_whole_task_when_the_engine_gives_up(
    settings: Settings, tmp_path: Path, fleet_server: str, no_backoff: None
) -> None:
    """When every in-engine attempt fails, the queue runs the task again.

    ``max_retries=1`` stops the engine from hiding the failure, so the 500
    reaches the queue as a real ``DownloadError``. The whole-task retry then
    re-runs the executor - a second connection, a fresh transfer - and the
    second request succeeds against the same server.
    """

    settings.max_retries = 1
    path = "/media/giveup.bin"
    _FleetHandler.fail_first(path, 1)
    destination = tmp_path / "giveup.bin"

    queue = DownloadQueue(
        settings,
        executor=_transfer_executor(
            settings,
            url_for=lambda task: f"{fleet_server}{path}",
            destination_for=lambda task: destination,
        ),
    )
    try:
        queue.set_max_concurrent(1)
        queue.start()
        task = queue.submit(URL, max_attempts=2)

        _wait_until(lambda: task.state.finished, timeout=60, message="任务没有结束")

        assert task.state is QueueState.COMPLETED
        assert task.attempts == 2, "失败的任务应当由队列整体重跑一次"
        assert _FleetHandler.stats()["requests"][path] == 2, "队列重试没有真正再访问服务器"
        assert destination.read_bytes() == _payload_for(path)
        assert task.error is None
    finally:
        queue.stop(timeout=10)


# --- 7. cancelling and pausing while five transfers are in flight -------------
#
# Sections 1-6 either drive one real transfer or drive the scheduler with a
# fake. These three tests put the *real* engine into the situations where a
# scheduler bug turns into a wrong file: one task of a running fleet is told to
# stop, while four others keep writing. The synchronisation is entirely
# Event/Condition based - the fleet is entered when the server reports five
# in-flight requests, and every state is waited for with ``_wait_until`` - so no
# test depends on a sleep landing inside a transfer.


def _fleet(
    settings: Settings, tmp_path: Path, fleet_server: str, *, count: int
) -> tuple[
    DownloadQueue, list[QueueTask], threading.Lock, list[str], list[tuple[str, float, float]]
]:
    """A running fleet of ``count`` real transfers, already sharing the server.

    Returns the queue, the tasks, a lock plus the list of executed task ids, and
    the list of ``(task_id, started, ended)`` spans - one per executor call.
    The spans are how "never downloading the same file twice at once" is
    checked: the server cannot answer that question, because a transfer the
    client abandoned still looks in-flight until its next write fails.
    """

    calls: list[str] = []
    spans: list[tuple[str, float, float]] = []
    lock = threading.Lock()

    def url_for(task: QueueTask) -> str:
        return f"{fleet_server}/media/{task.task_id}.bin"

    def destination_for(task: QueueTask) -> Path:
        return tmp_path / f"{task.task_id}.bin"

    inner = _transfer_executor(settings, url_for=url_for, destination_for=destination_for)

    def executor(task: QueueTask, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        started = time.monotonic()
        with lock:
            calls.append(task.task_id)
        try:
            return inner(task, progress, cancel_event)
        finally:
            with lock:
                spans.append((task.task_id, started, time.monotonic()))

    queue = DownloadQueue(settings, executor=executor)
    queue.set_max_concurrent(MAX_CONCURRENT_LIMIT)
    queue.start()
    tasks = [queue.submit(f"{URL}?p={index}") for index in range(1, count + 1)]
    _wait_until(
        lambda: _FleetHandler.stats()["inflight"] == count,
        timeout=30,
        message=f"{count} 个任务没有同时在传输",
    )
    return queue, tasks, lock, calls, spans


def _assert_one_transfer_at_a_time(
    spans: list[tuple[str, float, float]], task_id: str
) -> None:
    """Two runs of one task must never be transferring at the same time.

    This is what the ``claimed`` marker exists to prevent, and it is the honest
    way to ask "was this file downloaded twice at once": two executor calls
    whose spans overlap would be two writers on one ``.part`` file.
    """

    mine = sorted((started, ended) for (other, started, ended) in spans if other == task_id)
    for (_, first_ended), (second_started, _) in zip(mine, mine[1:], strict=False):
        assert first_ended <= second_started, "同一任务的两个传输在时间上重叠了"


def _max_concurrent_spans(spans: list[tuple[str, float, float]]) -> int:
    """The most executor calls that were ever open at the same time.

    This is the honest measure of the concurrency cap: a span brackets one
    executor call exactly, so the count cannot drift the way the server-side
    ``max_inflight`` counter does when a client finishes before its handler
    returns.
    """

    events: list[tuple[float, int]] = []
    for _, started, ended in spans:
        events.append((started, 1))
        events.append((ended, -1))
    # At equal timestamps -1 sorts before 1, so a call that ends exactly when
    # the next one starts is not counted as overlapping.
    events.sort()
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _assert_files_match_the_paths(
    tasks: list[QueueTask], destination_of: Callable[[QueueTask], Path]
) -> None:
    """Every finished file holds exactly the bytes its own request served."""

    for task in tasks:
        path = destination_of(task)
        assert path.read_bytes() == _payload_for(f"/media/{task.task_id}.bin"), (
            f"{path.name} 的内容与它自己的请求不符"
        )


def _await_server_idle(timeout: float = 30.0) -> None:
    """Wait until the server has finished recording every request.

    A request's delivered-byte count is written just after the client has read
    its last byte, so reading the counters the instant the queue reports
    "finished" can miss the very last request. ``inflight`` reaching zero means
    every request has been recorded, because the recording happens before the
    release for the same request.
    """

    _wait_until(
        lambda: _FleetHandler.stats()["inflight"] == 0,
        timeout=timeout,
        message="服务端没有空闲下来",
    )


def _assert_queue_drained(
    queue: DownloadQueue, *, message: str = "队列没有释放全部槽位"
) -> None:
    """Every slot must be handed back once the tasks have settled.

    ``_run`` writes the task's terminal state and the worker's ``finally``
    releases the slot immediately afterwards, so the two are ordered but not
    simultaneous: asserting ``_active == 0`` the instant the states look done
    reads a count the worker has not decremented yet.  Waiting turns that flake
    into a real check - a slot that is never released now fails instead of
    passing or failing depending on scheduling.
    """

    _wait_until(lambda: queue._active == 0, message=message)


def test_cancelling_one_of_five_transfers_leaves_the_others_intact(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """Cancel one of five real transfers; the other four must be untouched.

    The cancel is issued at the moment all five transfers are genuinely in
    flight - the only point where one task stopping could plausibly disturb
    another's socket, slot or bookkeeping. What has to hold afterwards is that
    the survivors are all complete with their own bytes, and that the cancelled
    task got exactly one request: a second one would mean the queue quietly
    re-downloaded something the user had just told it to stop.
    """

    queue, tasks, lock, calls, spans = _fleet(settings, tmp_path, fleet_server, count=5)
    try:
        doomed = tasks[2]
        path = f"/media/{doomed.task_id}.bin"

        assert queue.cancel(doomed.task_id) is True
        _wait_until(lambda: doomed.state is QueueState.CANCELLED, message="取消未被确认")

        survivors = [task for task in tasks if task is not doomed]
        _wait_until(
            lambda: all(task.state.finished for task in survivors),
            timeout=60,
            message="其他任务没有全部完成",
        )
        _await_server_idle()

        assert all(task.state is QueueState.COMPLETED for task in survivors), (
            "取消一个任务影响了其他任务"
        )
        assert queue.counts()["cancelled"] == 1
        # The others' bytes are their own: nothing was redirected to them.
        _assert_files_match_the_paths(survivors, lambda task: tmp_path / f"{task.task_id}.bin")

        stats = _FleetHandler.stats()
        assert stats["requests"][path] == 1, "被取消的任务被重新下载了"
        _assert_one_transfer_at_a_time(spans, doomed.task_id)
        with lock:
            assert calls.count(doomed.task_id) == 1, "被取消的任务被执行了不止一次"
        assert not (tmp_path / f"{doomed.task_id}.bin").exists(), "取消后仍产出了最终文件"
        # The four survivors each fetched exactly once.
        for task in survivors:
            survivor_path = f"/media/{task.task_id}.bin"
            assert stats["requests"][survivor_path] == 1
            _assert_one_transfer_at_a_time(spans, task.task_id)
    finally:
        queue.stop(timeout=15)


def test_pausing_one_of_five_transfers_freezes_it_and_resumes_it_correctly(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """Pause one of five: it stops, keeps what arrived, and finishes on resume.

    "It stopped" is proven structurally rather than with a sleep: the pause is
    only written *after* the transfer has unwound, so the byte counter frozen at
    that point must still be frozen long afterwards - and the wait for that is
    the other four tasks finishing, which is a condition, not a timer.

    "It resumed correctly" is proven by the second request being a Range that
    starts at least where the first one left off, and by the finished file
    matching its payload exactly.
    """

    queue, tasks, lock, calls, spans = _fleet(settings, tmp_path, fleet_server, count=5)
    try:
        paused = tasks[1]
        path = f"/media/{paused.task_id}.bin"
        destination = tmp_path / f"{paused.task_id}.bin"
        part = _part_of(destination)

        # Wait for real bytes on disk before pausing, so the resume has
        # something to resume from and the pause lands mid-transfer.
        _wait_until(
            lambda: part.exists() and part.stat().st_size > 0,
            timeout=30,
            message="没有产生分片文件",
        )

        assert queue.pause(paused.task_id) is True
        _wait_until(lambda: paused.state is QueueState.PAUSED, message="任务未进入暂停")

        partial_size = part.stat().st_size
        assert 0 < partial_size < len(PAYLOAD), "暂停时应当只下载了一部分"
        # Once PAUSED is written the transfer has already unwound, so this
        # reading is final.
        frozen_bytes = paused.snapshot()["downloaded"]

        survivors = [task for task in tasks if task is not paused]
        _wait_until(
            lambda: all(task.state.finished for task in survivors),
            timeout=60,
            message="其他任务没有全部完成",
        )
        _await_server_idle()

        # Still frozen after the whole rest of the fleet finished.
        assert paused.snapshot()["downloaded"] == frozen_bytes, "暂停后进度仍在推进"
        assert part.stat().st_size == partial_size, "暂停后文件仍在增长"
        assert paused.state is QueueState.PAUSED
        assert not (tmp_path / f"{paused.task_id}.bin").exists(), "暂停时不应生成最终文件"

        # The pause was the paused task's business only.
        assert all(task.state is QueueState.COMPLETED for task in survivors)
        _assert_files_match_the_paths(survivors, lambda task: tmp_path / f"{task.task_id}.bin")

        assert queue.resume(paused.task_id) is True
        _wait_until(lambda: paused.state.finished, timeout=60, message="继续后未完成")
        _await_server_idle()

        assert paused.state is QueueState.COMPLETED
        stats = _FleetHandler.stats()
        ranges = stats["ranges"][path]
        assert len(ranges) == 2, "续传应当发出第二个请求"
        assert ranges[1].startswith("bytes="), "续传没有使用 Range，而是从头重下"
        assert int(ranges[1][len("bytes=") :].split("-")[0]) >= partial_size, (
            "续传的起点比分片已有字节还少"
        )
        assert destination.read_bytes() == _payload_for(path), "续传后文件与源不一致"
        assert not part.exists(), "完成后不应残留分片文件"
    finally:
        queue.stop(timeout=15)


def test_cancelling_then_immediately_retrying_keeps_one_transfer_per_task(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """Cancel + retry inside a running fleet: never two transfers of one file.

    The retry is issued the instant ``cancel()`` returns, which is while the run
    it interrupted is still unwinding. Three things have to hold in that window:

    * the task is never *downloading* twice at once - the server counts requests
      per path, so two overlapping runs would show up as 2 and would corrupt one
      ``.part`` file between them;
    * the cancelled run actually stops. ``cancel()`` marks the task at once but
      the transfer unwinds on its own, and a retry that clears the cancel event
      out from under that run would let it - and only it - finish the file,
      delivering the whole payload and renaming the partial file away;
    * what is left on disk afterwards is a complete, correct file.

    The middle point is what makes this a threshold-free test: the payload is
    512 KiB and the cancel lands with 64 KiB of it on disk, so a run that
    stopped delivered strictly less than the payload - one that did not, sent
    all of it.
    """

    queue, tasks, lock, calls, spans = _fleet(settings, tmp_path, fleet_server, count=5)
    try:
        target = tasks[3]
        path = f"/media/{target.task_id}.bin"
        destination = tmp_path / f"{target.task_id}.bin"
        part = _part_of(destination)

        # Real bytes on disk first, so the cancel lands mid-transfer. Without
        # this the cancel can beat the first chunk of the payload, and then a
        # retry legitimately restarts from zero.
        _wait_until(
            lambda: part.exists() and part.stat().st_size > 0,
            timeout=30,
            message="没有产生分片文件",
        )
        partial_size = part.stat().st_size
        assert partial_size < len(PAYLOAD), "取消前已把整个文件下载完，无法构造取消场景"

        assert queue.cancel(target.task_id) is True
        assert queue.retry(target.task_id) is True

        _wait_until(
            lambda: all(task.state.finished for task in tasks),
            timeout=90,
            message="取消后重试的任务没有完成",
        )
        _await_server_idle()

        assert target.state is QueueState.COMPLETED, "取消后立即重试没有成功"
        assert all(task.state is QueueState.COMPLETED for task in tasks)

        stats = _FleetHandler.stats()
        # Never two transfers of that file overlapping, whatever else happens.
        _assert_one_transfer_at_a_time(spans, target.task_id)
        # The cancelled run's request plus the retried run's, and nothing else.
        assert stats["requests"][path] == 2, "取消后重试没有产生第二次请求"
        with lock:
            assert calls.count(target.task_id) == 2, "重试后任务没有重新执行"

        delivered = stats["delivered"][path]
        assert len(delivered) == 2, "取消后重试应当产生两个请求"
        assert delivered[0] < len(PAYLOAD), (
            f"被取消的传输没有停下来：它交付了 {delivered[0]} 字节（整个文件 "
            f"{len(PAYLOAD)} 字节），取消被重试清掉了"
        )

        # A run that stopped leaves its partial file behind, so the next run
        # resumes from it instead of starting over.
        ranges = stats["ranges"][path]
        assert ranges[1].startswith("bytes="), "重试后没有续传，而是从头下载"
        assert int(ranges[1][len("bytes=") :].split("-")[0]) >= partial_size, (
            "续传的起点比分片已有字节还少"
        )

        # Everyone's file is its own bytes - including the retried one, whose
        # ``.part`` was written by a run that was cancelled and then re-run.
        _assert_files_match_the_paths(tasks, lambda task: tmp_path / f"{task.task_id}.bin")
        assert not part.exists()
        # The other four were fetched exactly once each.
        for task in tasks:
            if task is target:
                continue
            other = f"/media/{task.task_id}.bin"
            assert stats["requests"][other] == 1
            _assert_one_transfer_at_a_time(spans, task.task_id)
    finally:
        queue.stop(timeout=15)


def _task_path(task: QueueTask) -> str:
    """The path used by ``_fleet`` for one task."""

    return f"/media/{task.task_id}.bin"


def _task_destination(tmp_path: Path, task: QueueTask) -> Path:
    """The destination used by ``_fleet`` for one task."""

    return tmp_path / f"{task.task_id}.bin"


def _wait_for_partial_files(tmp_path: Path, tasks: list[QueueTask]) -> dict[str, int]:
    """Wait until every task has real bytes, returning stable partial sizes."""

    sizes: dict[str, int] = {}

    def all_present() -> bool:
        for task in tasks:
            part = _task_destination(tmp_path, task).with_name(f"{task.task_id}.bin.part")
            size = part.stat().st_size if part.exists() else 0
            if size <= 0:
                return False
            sizes[task.task_id] = size
        return True

    _wait_until(all_present, timeout=30, message="并发任务没有全部产生分片字节")
    return dict(sizes)


def test_cancelling_multiple_real_transfers_does_not_disturb_the_survivors(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """Cancel three of five real transfers at the same transfer boundary."""

    queue, tasks, lock, calls, spans = _fleet(settings, tmp_path, fleet_server, count=5)
    try:
        doomed = tasks[:3]
        _wait_for_partial_files(tmp_path, doomed)

        for task in doomed:
            assert queue.cancel(task.task_id) is True
        _wait_until(
            lambda: all(task.state is QueueState.CANCELLED for task in doomed),
            message="多个取消任务没有全部进入 CANCELLED",
        )
        survivors = tasks[3:]
        _wait_until(
            lambda: all(task.state is QueueState.COMPLETED for task in survivors),
            timeout=60,
            message="未取消任务没有全部完成",
        )
        _await_server_idle()

        stats = _FleetHandler.stats()
        assert all(task.state is QueueState.CANCELLED for task in doomed)
        assert all(task.state is QueueState.COMPLETED for task in survivors)
        for task in doomed:
            path = _task_path(task)
            assert stats["requests"][path] == 1
            assert stats["delivered"][path][0] < len(PAYLOAD)
            assert calls.count(task.task_id) == 1
            _assert_one_transfer_at_a_time(spans, task.task_id)
            assert not _task_destination(tmp_path, task).exists()
        for task in survivors:
            path = _task_path(task)
            assert stats["requests"][path] == 1
            assert calls.count(task.task_id) == 1
            _assert_one_transfer_at_a_time(spans, task.task_id)
            assert _task_destination(tmp_path, task).read_bytes() == _payload_for(path)
        _assert_queue_drained(queue, message="取消后队列没有释放全部槽位")
    finally:
        queue.stop(timeout=15)


def test_pausing_multiple_real_transfers_then_resuming_preserves_each_file(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """Pause three of five, prove all three freeze, then resume them together."""

    queue, tasks, lock, calls, spans = _fleet(settings, tmp_path, fleet_server, count=5)
    try:
        paused = tasks[:3]
        _wait_for_partial_files(tmp_path, paused)
        for task in paused:
            assert queue.pause(task.task_id) is True
        _wait_until(
            lambda: all(task.state is QueueState.PAUSED for task in paused),
            message="多个暂停任务没有全部进入 PAUSED",
        )

        paused_sizes = {
            task.task_id: _task_destination(tmp_path, task)
            .with_name(f"{task.task_id}.bin.part")
            .stat()
            .st_size
            for task in paused
        }
        assert all(0 < size < len(PAYLOAD) for size in paused_sizes.values())
        frozen_progress = {task.task_id: task.snapshot()["downloaded"] for task in paused}
        survivors = tasks[3:]
        _wait_until(
            lambda: all(task.state is QueueState.COMPLETED for task in survivors),
            timeout=60,
            message="未暂停任务没有全部完成",
        )
        _await_server_idle()

        for task in paused:
            part = _task_destination(tmp_path, task).with_name(f"{task.task_id}.bin.part")
            assert task.state is QueueState.PAUSED
            assert part.stat().st_size == paused_sizes[task.task_id]
            assert task.snapshot()["downloaded"] == frozen_progress[task.task_id]
            assert not _task_destination(tmp_path, task).exists()
        for task in survivors:
            path = _task_path(task)
            assert task.state is QueueState.COMPLETED
            assert calls.count(task.task_id) == 1
            assert _task_destination(tmp_path, task).read_bytes() == _payload_for(path)

        for task in paused:
            assert queue.resume(task.task_id) is True
        _wait_until(
            lambda: all(task.state is QueueState.COMPLETED for task in tasks),
            timeout=90,
            message="多个暂停任务恢复后没有全部完成",
        )
        _await_server_idle()

        stats = _FleetHandler.stats()
        for task in paused:
            path = _task_path(task)
            ranges = stats["ranges"][path]
            assert len(ranges) == 2
            assert ranges[1].startswith("bytes=")
            assert int(ranges[1][len("bytes=") :].split("-")[0]) >= paused_sizes[task.task_id]
            assert stats["requests"][path] == 2
            assert calls.count(task.task_id) == 2
            _assert_one_transfer_at_a_time(spans, task.task_id)
            assert _task_destination(tmp_path, task).read_bytes() == _payload_for(path)
            assert not _task_destination(tmp_path, task).with_name(
                f"{task.task_id}.bin.part"
            ).exists()
        assert all(task.state is QueueState.COMPLETED for task in tasks)
        _assert_queue_drained(queue, message="恢复后队列没有释放全部槽位")
    finally:
        queue.stop(timeout=15)


def test_mixed_cancel_pause_resume_and_retry_does_not_deadlock_or_cross_files(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """Exercise cancellation, pause, resume and retry in one five-way fleet."""

    queue, tasks, lock, calls, spans = _fleet(settings, tmp_path, fleet_server, count=5)
    try:
        paused = tasks[:2]
        retried = tasks[2]
        cancelled = tasks[3]
        untouched = tasks[4]
        partial_sizes = _wait_for_partial_files(tmp_path, [*paused, retried, cancelled])

        for task in paused:
            assert queue.pause(task.task_id) is True
        assert queue.cancel(retried.task_id) is True
        assert queue.retry(retried.task_id) is True
        assert queue.cancel(cancelled.task_id) is True

        _wait_until(
            lambda: all(task.state is QueueState.PAUSED for task in paused)
            and retried.state is QueueState.COMPLETED
            and cancelled.state is QueueState.CANCELLED
            and untouched.state is QueueState.COMPLETED,
            timeout=90,
            message="混合操作没有达到预期中间状态",
        )
        _await_server_idle()
        paused_sizes = {
            task.task_id: _task_destination(tmp_path, task)
            .with_name(f"{task.task_id}.bin.part")
            .stat()
            .st_size
            for task in paused
        }
        assert all(0 < size < len(PAYLOAD) for size in paused_sizes.values())

        for task in paused:
            assert _task_destination(tmp_path, task).with_name(
                f"{task.task_id}.bin.part"
            ).stat().st_size == paused_sizes[task.task_id]
        assert not _task_destination(tmp_path, cancelled).exists()
        assert retried.state is QueueState.COMPLETED
        assert untouched.state is QueueState.COMPLETED

        for task in paused:
            assert queue.resume(task.task_id) is True
        _wait_until(
            lambda: all(task.state.finished for task in tasks),
            timeout=90,
            message="混合操作恢复后队列没有全部结束",
        )
        _await_server_idle()

        stats = _FleetHandler.stats()
        for task in paused:
            path = _task_path(task)
            ranges = stats["ranges"][path]
            assert len(ranges) == 2
            assert ranges[1].startswith("bytes=")
            assert int(ranges[1][len("bytes=") :].split("-")[0]) >= paused_sizes[task.task_id]
            assert stats["requests"][path] == 2
            assert _task_destination(tmp_path, task).read_bytes() == _payload_for(path)
            _assert_one_transfer_at_a_time(spans, task.task_id)
        retry_path = _task_path(retried)
        retry_ranges = stats["ranges"][retry_path]
        assert stats["requests"][retry_path] == 2
        assert retry_ranges[1].startswith("bytes=")
        assert int(retry_ranges[1][len("bytes=") :].split("-")[0]) >= partial_sizes[retried.task_id]
        assert stats["delivered"][retry_path][0] < len(PAYLOAD)
        assert _task_destination(tmp_path, retried).read_bytes() == _payload_for(retry_path)
        _assert_one_transfer_at_a_time(spans, retried.task_id)

        cancelled_path = _task_path(cancelled)
        assert stats["requests"][cancelled_path] == 1
        assert stats["delivered"][cancelled_path][0] < len(PAYLOAD)
        assert not _task_destination(tmp_path, cancelled).exists()
        _assert_one_transfer_at_a_time(spans, cancelled.task_id)

        untouched_path = _task_path(untouched)
        assert stats["requests"][untouched_path] == 1
        assert _task_destination(tmp_path, untouched).read_bytes() == _payload_for(untouched_path)
        _assert_one_transfer_at_a_time(spans, untouched.task_id)
        with lock:
            assert calls.count(retried.task_id) == 2
            assert calls.count(cancelled.task_id) == 1
            assert calls.count(untouched.task_id) == 1
            assert calls.count(paused[0].task_id) == 2
            assert calls.count(paused[1].task_id) == 2
        assert all(task.state is QueueState.COMPLETED for task in paused + [retried, untouched])
        assert cancelled.state is QueueState.CANCELLED
        _assert_queue_drained(queue, message="混合操作后队列没有释放全部槽位")
    finally:
        queue.stop(timeout=15)


# --- 8. large streaming and repeated interruption/resume ----------------------
#
# The fleet normally serves 512 KiB per path. These two paths opt into a
# generated 32 MiB payload that is emitted a chunk at a time by the same local
# server. The response body is never assembled in memory, and transfer tests
# wait for real file-size thresholds rather than a timer, so they remain
# representative without turning a normal test run into a long soak test.


def test_large_stream_is_integral_streamed_to_disk_and_leaves_no_part(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """A 32 MiB real transfer stays bounded in memory and completes cleanly."""

    path = LARGE_PATH
    destination = tmp_path / "large.bin"
    part = _part_of(destination)
    _FleetHandler.configure_path(
        path,
        size=LARGE_PAYLOAD_SIZE,
        chunk_size=LARGE_CHUNK_SIZE,
        delay=LARGE_CHUNK_DELAY,
    )
    settings.chunk_size = LARGE_CHUNK_SIZE
    cancel_event = threading.Event()

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    thread, finished, outcomes, errors = _start_real_stream(
        settings,
        f"{fleet_server}{path}",
        destination,
        size=LARGE_PAYLOAD_SIZE,
        cancel_event=cancel_event,
    )
    try:
        assert finished.wait(60), "32 MiB 本地下载未在 60 秒内结束"
        thread.join(timeout=5)
        assert not thread.is_alive(), "下载线程结束事件已置位但线程仍存活"
        assert errors == [], f"大文件传输失败：{errors!r}"
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.bytes_downloaded == LARGE_PAYLOAD_SIZE
        assert outcome.resumed_from == 0
        assert destination.stat().st_size == LARGE_PAYLOAD_SIZE
        assert not part.exists(), "下载完成后遗留 .part 文件"

        current, peak = tracemalloc.get_traced_memory()
        peak_delta = peak - baseline
        assert peak_delta < 8 * 1024 * 1024, (
            f"32 MiB 下载期间 Python 峰值分配 {peak_delta} 字节，疑似缓冲了完整响应"
        )
    finally:
        if not was_tracing:
            tracemalloc.stop()

    _await_server_idle(timeout=30)
    stats = _FleetHandler.stats()
    assert stats["requests"][path] == 1
    assert stats["ranges"][path] == [""]
    assert stats["delivered"][path] == [LARGE_PAYLOAD_SIZE]
    with destination.open("rb") as handle:
        actual_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    assert actual_digest == _payload_digest(path, LARGE_PAYLOAD_SIZE)
    assert {item.name for item in tmp_path.iterdir()} == {destination.name}


def test_repeated_large_transfer_interruptions_resume_at_exact_file_offsets(
    settings: Settings, tmp_path: Path, fleet_server: str
) -> None:
    """Three real mid-transfer cancellations resume without losing or replaying bytes."""

    path = LARGE_PATH
    destination = tmp_path / "repeated-resume.bin"
    part = _part_of(destination)
    _FleetHandler.configure_path(
        path,
        size=LARGE_PAYLOAD_SIZE,
        chunk_size=LARGE_CHUNK_SIZE,
        delay=LARGE_CHUNK_DELAY,
    )
    settings.chunk_size = LARGE_CHUNK_SIZE

    interrupted_sizes: list[int] = []
    request_offsets = [0]
    for stop_at in LARGE_CANCEL_OFFSETS:
        cancel_event = threading.Event()
        thread, finished, outcomes, errors = _start_real_stream(
            settings,
            f"{fleet_server}{path}",
            destination,
            size=LARGE_PAYLOAD_SIZE,
            cancel_event=cancel_event,
        )
        _wait_until(
            lambda stop_at=stop_at: part.exists() and part.stat().st_size >= stop_at,
            timeout=45,
            message=f"传输没有到达中断阈值 {stop_at} 字节",
        )
        cancel_event.set()
        assert finished.wait(30), "中断请求后下载线程没有退出"
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcomes == []
        assert len(errors) == 1 and isinstance(errors[0], DownloadCancelled), (
            f"期望协作取消，实际错误：{errors!r}"
        )
        _await_server_idle(timeout=30)

        size_after_cancel = part.stat().st_size
        assert stop_at <= size_after_cancel < LARGE_PAYLOAD_SIZE
        assert not destination.exists(), "部分传输不应创建最终文件"
        interrupted_sizes.append(size_after_cancel)
        request_offsets.append(size_after_cancel)

        stats = _FleetHandler.stats()
        request_index = len(interrupted_sizes) - 1
        assert stats["requests"][path] == request_index + 1
        assert stats["delivered"][path][request_index] < (
            LARGE_PAYLOAD_SIZE - request_offsets[request_index]
        ), "取消后服务端仍交付了本次请求的全部剩余内容"

    # The final run starts at the exact stable .part size left by the third
    # interruption. Therefore no already-written byte is requested again.
    final_offset = interrupted_sizes[-1]
    cancel_event = threading.Event()
    thread, finished, outcomes, errors = _start_real_stream(
        settings,
        f"{fleet_server}{path}",
        destination,
        size=LARGE_PAYLOAD_SIZE,
        cancel_event=cancel_event,
    )
    assert finished.wait(90), "最终续传未在 90 秒内结束"
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == [] and len(outcomes) == 1, f"最终续传失败：{errors!r}"
    outcome = outcomes[0]
    assert outcome.resumed_from == final_offset
    assert outcome.bytes_downloaded == LARGE_PAYLOAD_SIZE

    _await_server_idle(timeout=30)
    stats = _FleetHandler.stats()
    ranges = stats["ranges"][path]
    assert len(ranges) == len(interrupted_sizes) + 1
    assert ranges[0] == ""
    for index, expected_offset in enumerate(interrupted_sizes, start=1):
        assert ranges[index] == f"bytes={expected_offset}-"
    assert stats["requests"][path] == len(interrupted_sizes) + 1
    assert stats["delivered"][path][-1] == LARGE_PAYLOAD_SIZE - interrupted_sizes[-1]
    assert destination.stat().st_size == LARGE_PAYLOAD_SIZE
    assert not part.exists(), "多轮续传后遗留 .part 文件"
    with destination.open("rb") as handle:
        actual_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    assert actual_digest == _payload_digest(path, LARGE_PAYLOAD_SIZE)
    assert {item.name for item in tmp_path.iterdir()} == {destination.name}


# --- 9. bounded multi-round queue soak ---------------------------------------
#
# This is intentionally a bounded soak rather than a wall-clock loop: eight
# rounds, five tasks per round, and a 90-second hard deadline. Each round has
# one normal transfer, one cancellation, one pause/resume, one cancel+immediate
# retry, and one injected HTTP failure that triggers a queue retry. Data is
# served only by the local Fleet handler; every wait is an event/state/file
# condition, never a delay used to make the test pass.

SOAK_ROUNDS = 8
SOAK_TASKS_PER_ROUND = 5
SOAK_DEADLINE_SECONDS = 90.0
SOAK_PAYLOAD_SIZE = len(PAYLOAD)
SOAK_CHUNK_SIZE = 16 * 1024
SOAK_CHUNK_DELAY = 0.02


def test_bounded_http_queue_soak_keeps_tasks_threads_and_memory_stable(
    settings: Settings,
    tmp_path: Path,
    fleet_server: str,
    no_backoff: None,
) -> None:
    """Repeatedly download, cancel, pause, resume and retry without queue drift."""

    settings.max_retries = 1
    settings.queue_max_attempts = 2
    baseline_threads = threading.active_count()
    tracing_was_active = tracemalloc.is_tracing()
    if not tracing_was_active:
        tracemalloc.start()
    baseline_memory, _ = tracemalloc.get_traced_memory()

    calls: dict[str, int] = {}
    spans: list[tuple[str, float, float]] = []
    errors_by_round: dict[int, list[tuple[str, str]]] = {}
    lock = threading.Lock()
    active_round = [0]

    def url_for(task: QueueTask) -> str:
        return f"{fleet_server}/{task.url.removeprefix('soak://')}"

    def destination_for(task: QueueTask) -> Path:
        return tmp_path / f"{task.task_id}.bin"

    inner = _transfer_executor(settings, url_for=url_for, destination_for=destination_for)

    def executor(task: QueueTask, progress: dict, cancel_event: threading.Event) -> DownloadResult:
        started = time.monotonic()
        with lock:
            calls[task.url] = calls.get(task.url, 0) + 1
        try:
            return inner(task, progress, cancel_event)
        except Exception as exc:
            with lock:
                errors_by_round.setdefault(active_round[0], []).append(
                    (task.url, type(exc).__name__)
                )
            raise
        finally:
            with lock:
                spans.append((task.url, started, time.monotonic()))

    queue = DownloadQueue(settings, executor=executor)
    queue.set_max_concurrent(MAX_CONCURRENT_LIMIT)
    queue.start()
    all_tasks: list[QueueTask] = []
    round_metrics: list[dict[str, Any]] = []
    peak_threads = baseline_threads
    peak_memory_delta = 0
    soak_started = time.monotonic()
    deadline = soak_started + SOAK_DEADLINE_SECONDS

    def wait_for(predicate: Callable[[], bool], *, timeout: float = 30.0, message: str) -> None:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "soak test exceeded its hard deadline"
        _wait_until(predicate, timeout=min(timeout, remaining), message=message)

    try:
        for round_number in range(1, SOAK_ROUNDS + 1):
            active_round[0] = round_number
            round_tasks: dict[str, QueueTask] = {}
            round_urls: dict[str, str] = {}
            for role in ("normal", "cancel", "pause", "retry", "network-retry"):
                task_url = f"soak://r{round_number:02d}/{role}"
                request_path = f"/r{round_number:02d}/{role}"
                round_urls[role] = request_path
                _FleetHandler.configure_path(
                    request_path,
                    size=SOAK_PAYLOAD_SIZE,
                    chunk_size=SOAK_CHUNK_SIZE,
                    delay=SOAK_CHUNK_DELAY,
                )
                if role == "network-retry":
                    _FleetHandler.fail_first(request_path, 1)
                if role in ("cancel", "pause", "retry"):
                    # These three get interrupted below.  Holding their first
                    # response part-way through its body makes "the transfer is
                    # still running" a property of the fixture rather than a
                    # race against a response that may already be over.
                    _FleetHandler.hold_path(request_path)
                task = queue.submit(task_url, max_attempts=2, output_dir=str(tmp_path))
                round_tasks[role] = task
                all_tasks.append(task)

            current_before, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            wait_for(
                lambda round_tasks=round_tasks: all(
                    calls.get(task.url, 0) >= 1 for task in round_tasks.values()
                ),
                message=f"第 {round_number} 轮没有启动全部五个任务",
            )
            peak_threads = max(peak_threads, threading.active_count())

            controlled = [round_tasks[role] for role in ("cancel", "pause", "retry")]
            # The gate is the synchronisation point: once a held response has
            # reached its parking point it is stuck there, so the transfer
            # cannot finish - and its ``.part`` cannot be renamed away - before
            # the interruption below lands.  Waiting on the file alone used to
            # be a race: a transfer that completed first made the condition
            # permanently false and the wait burned its whole timeout.
            remaining = deadline - time.monotonic()
            assert remaining > 0, "soak test exceeded its hard deadline"
            for role in ("cancel", "pause", "retry"):
                assert _FleetHandler.wait_parked(
                    round_urls[role], timeout=min(30.0, max(0.1, remaining))
                ), f"第 {round_number} 轮 {role} 没有走到响应闸门"
            wait_for(
                lambda controlled=controlled: all(
                    _part_of(destination_for(task)).exists()
                    and _part_of(destination_for(task)).stat().st_size > 0
                    for task in controlled
                ),
                message=f"第 {round_number} 轮受控任务没有产生真实分片",
            )

            cancel_task = round_tasks["cancel"]
            paused_task = round_tasks["pause"]
            retry_task = round_tasks["retry"]
            retry_initial_size = _part_of(destination_for(retry_task)).stat().st_size
            assert queue.cancel(cancel_task.task_id) is True
            assert queue.pause(paused_task.task_id) is True
            assert queue.cancel(retry_task.task_id) is True
            assert queue.retry(retry_task.task_id) is True

            # The interruption has landed.  Let the held responses continue:
            # the aborted ones will fail their next write, the retried task's
            # new request must not be parked, and nothing may stay on a gate
            # by the time the round checks its thread and in-flight counts.
            for role in ("cancel", "pause", "retry"):
                _FleetHandler.release_path(round_urls[role])

            normal_task = round_tasks["normal"]
            network_task = round_tasks["network-retry"]
            wait_for(
                lambda cancel_task=cancel_task,
                paused_task=paused_task,
                retry_task=retry_task,
                normal_task=normal_task,
                network_task=network_task: cancel_task.state is QueueState.CANCELLED
                and paused_task.state is QueueState.PAUSED
                and retry_task.state is QueueState.COMPLETED
                and normal_task.state is QueueState.COMPLETED
                and network_task.state is QueueState.COMPLETED,
                message=f"第 {round_number} 轮混合操作未落定",
            )
            _await_server_idle(timeout=min(30.0, max(0.1, deadline - time.monotonic())))

            paused_part = _part_of(destination_for(paused_task))
            paused_size = paused_part.stat().st_size
            paused_progress = paused_task.snapshot()["downloaded"]
            assert 0 < paused_size < SOAK_PAYLOAD_SIZE
            assert paused_progress <= paused_size
            assert not destination_for(paused_task).exists()
            assert not destination_for(cancel_task).exists()
            cancel_part = _part_of(destination_for(cancel_task))
            assert 0 < cancel_part.stat().st_size < SOAK_PAYLOAD_SIZE
            assert 0 < retry_initial_size < SOAK_PAYLOAD_SIZE
            assert destination_for(retry_task).exists()
            assert not _part_of(destination_for(retry_task)).exists()

            assert queue.resume(paused_task.task_id) is True
            wait_for(
                lambda paused_task=paused_task: paused_task.state is QueueState.COMPLETED,
                message=f"第 {round_number} 轮暂停任务恢复后未完成",
            )
            wait_for(
                lambda round_tasks=round_tasks: all(
                    task.state.finished for task in round_tasks.values()
                ),
                message=f"第 {round_number} 轮仍有未结束任务",
            )
            _await_server_idle(timeout=min(30.0, max(0.1, deadline - time.monotonic())))

            stats = _FleetHandler.stats()
            for role, task in round_tasks.items():
                request_path = round_urls[role]
                destination = destination_for(task)
                expected_calls = 2 if role in ("pause", "retry", "network-retry") else 1
                assert calls[task.url] == expected_calls, (
                    f"第 {round_number} 轮 {role} executor 调用数错误：{calls[task.url]}"
                )
                assert stats["requests"][request_path] == expected_calls, (
                    f"第 {round_number} 轮 {role} HTTP 请求数错误"
                )
                _assert_one_transfer_at_a_time(spans, task.url)

                if role == "cancel":
                    assert task.state is QueueState.CANCELLED
                    # "The cancel landed mid-transfer" needs both halves, each
                    # measured where it is deterministic: the stub had already
                    # written the parked fraction when the interruption was
                    # issued, and the client's own ``.part`` then stopped short
                    # of the payload. The stub's *final* byte count is
                    # deliberately not used: the kernel can absorb the rest of a
                    # response the client abandoned (its send buffer is bounded
                    # above, the peer's receive window is not ours to set).
                    assert stats["delivered"][request_path][0] >= int(
                        SOAK_PAYLOAD_SIZE * HOLD_AFTER_FRACTION
                    ), "响应没有走到停靠点，取消就没有中断任何东西"
                    assert 0 < cancel_part.stat().st_size < SOAK_PAYLOAD_SIZE
                    assert _part_of(destination).exists()
                    continue

                assert task.state is QueueState.COMPLETED
                assert destination.stat().st_size == SOAK_PAYLOAD_SIZE
                assert not _part_of(destination).exists()
                with destination.open("rb") as handle:
                    actual_digest = hashlib.file_digest(handle, "sha256").hexdigest()
                assert actual_digest == _payload_digest(request_path, SOAK_PAYLOAD_SIZE)

                ranges = stats["ranges"].get(request_path, [])
                if role == "pause":
                    assert ranges[1] == f"bytes={paused_size}-"
                    assert stats["delivered"][request_path][-1] == SOAK_PAYLOAD_SIZE - paused_size
                elif role == "retry":
                    assert ranges[1].startswith("bytes=")
                    retry_offset = int(ranges[1][len("bytes=") :].split("-")[0])
                    assert retry_offset > 0
                    # Same two-sided evidence as the cancel branch: the first
                    # attempt reached the parked fraction, and the second had to
                    # resume from part-way through the payload.
                    assert stats["delivered"][request_path][0] >= int(
                        SOAK_PAYLOAD_SIZE * HOLD_AFTER_FRACTION
                    ), "第一次请求没有走到停靠点"
                    assert retry_initial_size < SOAK_PAYLOAD_SIZE
                elif role == "network-retry":
                    assert stats["delivered"][request_path] == [0, SOAK_PAYLOAD_SIZE]
                    assert ranges == [""]
                else:
                    assert ranges == [""]

            expected_exceptions = [
                (round_tasks[role].url, "DownloadCancelled")
                for role in ("cancel", "pause", "retry")
            ] + [(network_task.url, "DownloadError")]
            assert sorted(errors_by_round.get(round_number, [])) == sorted(expected_exceptions)

            expected_names = {
                f"{task.task_id}.bin.part" if task is cancel_task else f"{task.task_id}.bin"
                for task in round_tasks.values()
            }
            actual_round_names = {
                path.name for path in tmp_path.iterdir() if path.name in expected_names
            }
            assert actual_round_names == expected_names

            wait_for(
                lambda: threading.active_count() <= baseline_threads + MAX_CONCURRENT_LIMIT + 2,
                timeout=10,
                message=f"第 {round_number} 轮后线程数未回落到稳定范围",
            )
            current_memory, peak_memory = tracemalloc.get_traced_memory()
            round_peak_delta = peak_memory - current_before
            peak_memory_delta = max(peak_memory_delta, round_peak_delta)
            thread_count = threading.active_count()
            round_metrics.append(
                {
                    "round": round_number,
                    "tasks": len(round_tasks),
                    "completed": sum(t.state is QueueState.COMPLETED for t in round_tasks.values()),
                    "cancelled": sum(t.state is QueueState.CANCELLED for t in round_tasks.values()),
                    "failed": sum(t.state is QueueState.FAILED for t in round_tasks.values()),
                    "thread_count": thread_count,
                    "memory_live_delta_bytes": current_memory - baseline_memory,
                    "peak_delta_bytes": round_peak_delta,
                    "executor_errors": list(errors_by_round.get(round_number, [])),
                }
            )
            print(f"[soak] {round_metrics[-1]}", flush=True)

        queue.stop(timeout=15)
        wait_for(
            lambda: threading.active_count() <= baseline_threads + 2,
            timeout=10,
            message="停止队列后工作线程仍未退出",
        )
        final_memory, total_peak = tracemalloc.get_traced_memory()
        elapsed = time.monotonic() - soak_started
        assert elapsed <= SOAK_DEADLINE_SECONDS
        assert len(all_tasks) == SOAK_ROUNDS * SOAK_TASKS_PER_ROUND
        assert sum(task.state is QueueState.COMPLETED for task in all_tasks) == SOAK_ROUNDS * 4
        assert sum(task.state is QueueState.CANCELLED for task in all_tasks) == SOAK_ROUNDS
        assert sum(task.state is QueueState.FAILED for task in all_tasks) == 0
        _assert_queue_drained(queue, message="soak 结束后队列没有释放全部槽位")
        max_live_growth = max(item["memory_live_delta_bytes"] for item in round_metrics)
        assert max_live_growth < 16 * 1024 * 1024, (
            f"soak 期间 Python 活内存增长峰值 {max_live_growth} 字节"
        )
        assert final_memory - baseline_memory < 16 * 1024 * 1024, (
            f"soak 后 Python 活内存增长 {final_memory - baseline_memory} 字节"
        )
        assert max(peak_threads, *(item["thread_count"] for item in round_metrics)) <= (
            baseline_threads + MAX_CONCURRENT_LIMIT * 2 + 5
        )
        assert peak_memory_delta < 24 * 1024 * 1024
        print(
            "[soak-summary] "
            f"rounds={SOAK_ROUNDS} tasks={len(all_tasks)} "
            f"completed={sum(t.state is QueueState.COMPLETED for t in all_tasks)} "
            f"cancelled={sum(t.state is QueueState.CANCELLED for t in all_tasks)} failed=0 "
            f"elapsed={elapsed:.2f}s baseline_threads={baseline_threads} "
            f"peak_threads={peak_threads} final_threads={threading.active_count()} "
            f"memory_live_delta={final_memory - baseline_memory} "
            f"peak_round_delta={peak_memory_delta} total_peak={total_peak - baseline_memory}"
        )
    finally:
        # Defence in depth: a failed round must not leave a response parked on a
        # gate, or the handler thread outlives the test and skews the next one.
        _FleetHandler.release_all_holds()
        queue.stop(timeout=15)
        if not tracing_was_active:
            tracemalloc.stop()
