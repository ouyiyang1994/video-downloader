"""Cross-process output claims, exercised with real child processes.

The in-process claim set in :mod:`core.naming` cannot see a second instance of
the app, so the real guarantee is an OS byte-range lock. These tests spawn
separate interpreters - threads would share the lock state and prove nothing.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.naming import claim_output_path, release_output_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("_mp_worker.py")
TIMEOUT = 60
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _spawn(tmp_path: Path, *args: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["VIDEO_DOWNLOADER_LOCK_DIR"] = str(tmp_path / "locks")
    for name in PROXY_VARS:
        env.pop(name, None)
    return subprocess.Popen(
        [sys.executable, str(WORKER), *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.stdin is not None and not proc.stdin.closed:
        with contextlib.suppress(OSError):  # already gone
            proc.stdin.close()
    try:
        # Closing stdin is the shutdown signal; give the child time to exit on
        # its own. A hard kill leaves the OS lock lingering for a few ms, which
        # is a teardown artefact rather than the behaviour under test.
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=10)


def _first_line(proc: subprocess.Popen[str]) -> Path:
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line, "子进程没有输出占用路径"
    return Path(line)


def test_two_processes_claiming_the_same_name_get_different_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(tmp_path / "locks"))
    output = tmp_path / "downloads"
    output.mkdir()

    first = _spawn(tmp_path, "claim", str(output), "clip")
    second = _spawn(tmp_path, "claim", str(output), "clip")
    try:
        one = _first_line(first)
        two = _first_line(second)
    finally:
        _stop(first)
        _stop(second)

    assert one != two
    assert {one.name, two.name} == {"clip.mp4", "clip (1).mp4"}


def test_a_lock_held_by_another_process_is_not_stolen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(tmp_path / "locks"))
    output = tmp_path / "downloads"
    output.mkdir()

    holder = _spawn(tmp_path, "claim", str(output), "clip")
    held: Path | None = None
    mine: Path | None = None
    try:
        held = _first_line(holder)
        mine = claim_output_path(output, "clip", "mp4")
        assert mine != held, "另一个进程正在写这个名字，不能抢"
        assert mine.name == "clip (1).mp4"
    finally:
        if mine is not None:
            release_output_path(mine)
        _stop(holder)

    again = claim_output_path(output, "clip", "mp4")
    try:
        assert again == held, "对方退出后原名必须能重新占用"
    finally:
        release_output_path(again)


def test_a_crashed_process_does_not_lock_the_name_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os._exit`` skips every cleanup path; the OS must still free the lock."""

    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(tmp_path / "locks"))
    output = tmp_path / "downloads"
    output.mkdir()

    victim = _spawn(tmp_path, "crash", str(output), "clip")
    try:
        claimed = _first_line(victim)
        assert victim.wait(timeout=TIMEOUT) == 9
    finally:
        _stop(victim)

    again = claim_output_path(output, "clip", "mp4")
    try:
        assert again == claimed
    finally:
        release_output_path(again)


def test_a_released_claim_can_be_taken_by_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(tmp_path / "locks"))
    output = tmp_path / "downloads"
    output.mkdir()

    child = _spawn(tmp_path, "claim-release", str(output), "clip")
    try:
        released = _first_line(child)
        assert child.wait(timeout=TIMEOUT) == 0
    finally:
        _stop(child)

    again = claim_output_path(output, "clip", "mp4")
    try:
        assert again == released
    finally:
        release_output_path(again)


@pytest.fixture
def media_server() -> Iterator[tuple[str, bytes]]:
    payload = bytes(range(256)) * 800

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            for offset in range(0, len(payload), 32 * 1024):
                try:
                    self.wfile.write(payload[offset : offset + 32 * 1024])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                    return
                time.sleep(0.02)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/media.bin", payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_two_processes_downloading_the_same_title_keep_their_own_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_server: tuple[str, bytes]
) -> None:
    """Neither the ``.part`` nor the sidecar may be shared between instances."""

    url, payload = media_server
    monkeypatch.setenv("VIDEO_DOWNLOADER_LOCK_DIR", str(tmp_path / "locks"))
    output = tmp_path / "downloads"
    output.mkdir()

    first = _spawn(
        tmp_path, "download", str(output), "clip", "mp4", "--url", url, "--size", str(len(payload))
    )
    second = _spawn(
        tmp_path, "download", str(output), "clip", "mp4", "--url", url, "--size", str(len(payload))
    )
    try:
        one = _first_line(first)
        two = _first_line(second)
        first_err = first.stderr.readline().strip() if first.stderr else ""
        second_err = second.stderr.readline().strip() if second.stderr else ""
        assert one != two, f"两个进程拿到了同一个最终路径：{one}（{first_err} | {second_err}）"
        assert first.wait(timeout=TIMEOUT) == 0, first.stderr.read() if first.stderr else ""
        assert second.wait(timeout=TIMEOUT) == 0, second.stderr.read() if second.stderr else ""
    finally:
        _stop(first)
        _stop(second)

    assert one.read_bytes() == payload
    assert two.read_bytes() == payload
    assert not list(output.glob("*.part"))
    assert not list(output.glob("*.part.json"))
