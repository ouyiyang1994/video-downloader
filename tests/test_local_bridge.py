"""Tests for the loopback fallback transport.

The server is real and is spoken to over a real socket, because the parts worth
testing here - the origin check, the size limit, the shared validation - only
exist at the HTTP layer.
"""

from __future__ import annotations

import contextlib
import json
import select
import socket
import threading
import time

import httpx
import pytest

from config.settings import Settings
from core import local_bridge
from core.chrome_bridge import extension_id
from core.local_bridge import LocalBridge
from core.models import Platform


def _origin() -> str:
    return f"chrome-extension://{extension_id()}"


@pytest.fixture
def bridge(settings: Settings):
    """A bridge on an ephemeral port, so parallel test runs cannot collide."""

    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    instance = LocalBridge(settings, port=port)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def _post(bridge: LocalBridge, payload: object, *, origin: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    return httpx.post(
        bridge.url, content=json.dumps(payload).encode("utf-8"), headers=headers, timeout=10
    )


def _bilibili_payload() -> dict[str, object]:
    return {
        "action": "store",
        "platform": "bilibili",
        "cookies": [
            {
                "name": "SESSDATA",
                "value": "SECRET-FROM-EXTENSION",
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
            }
        ],
    }


# --- lifecycle ---------------------------------------------------------------


def test_starting_twice_keeps_one_socket(bridge: LocalBridge) -> None:
    assert bridge.running is True
    assert bridge.start() == bridge.port


def test_stopping_releases_the_port(bridge: LocalBridge) -> None:
    bridge.stop()
    assert bridge.running is False
    assert local_bridge.probe(bridge.port) is False


def test_probe_sees_a_live_bridge(bridge: LocalBridge) -> None:
    assert local_bridge.probe(bridge.port) is True


def test_a_second_bridge_on_the_same_port_is_refused(
    settings: Settings, bridge: LocalBridge
) -> None:
    other = LocalBridge(settings, port=bridge.port)
    with pytest.raises(OSError):
        other.start()


def test_the_url_is_loopback_only(bridge: LocalBridge) -> None:
    assert bridge.url.startswith("http://127.0.0.1:")
    assert "0.0.0.0" not in bridge.url


# --- the one route -----------------------------------------------------------


def test_a_store_request_writes_the_session(bridge: LocalBridge, settings: Settings) -> None:
    response = _post(bridge, _bilibili_payload(), origin=_origin())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["cookieNames"] == ["SESSDATA"]

    from core.http import load_cookie_header
    from core.session_store import session_file

    path = session_file(settings, Platform.BILIBILI)
    assert path.is_file()
    header = load_cookie_header(path, domain_suffix="bilibili.com")
    assert header is not None and "SESSDATA=SECRET-FROM-EXTENSION" in header


def test_the_response_never_carries_a_value(bridge: LocalBridge) -> None:
    response = _post(bridge, _bilibili_payload(), origin=_origin())
    assert "SECRET-FROM-EXTENSION" not in response.text


def test_ping_answers(bridge: LocalBridge) -> None:
    response = _post(bridge, {"action": "ping"}, origin=_origin())
    assert response.json()["ok"] is True


# --- who may talk to it ------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "https://evil.example",
        "http://127.0.0.1:8765",
        "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_a_foreign_origin_is_refused(bridge: LocalBridge, origin: str | None) -> None:
    response = _post(bridge, _bilibili_payload(), origin=origin)
    assert response.status_code == 403


def test_a_refused_origin_writes_nothing(bridge: LocalBridge, settings: Settings) -> None:
    _post(bridge, _bilibili_payload(), origin="https://evil.example")
    from core.session_store import session_file

    assert not session_file(settings, Platform.BILIBILI).exists()


def test_the_extension_origin_is_accepted_in_both_spellings(bridge: LocalBridge) -> None:
    assert _post(bridge, {"action": "ping"}, origin=_origin()).status_code == 200
    assert _post(bridge, {"action": "ping"}, origin=_origin() + "/").status_code == 200


def test_a_preflight_from_a_foreign_origin_is_refused(bridge: LocalBridge) -> None:
    response = httpx.options(bridge.url, headers={"Origin": "https://evil.example"}, timeout=10)
    assert response.status_code == 403


def test_a_preflight_from_the_extension_is_allowed(bridge: LocalBridge) -> None:
    response = httpx.options(bridge.url, headers={"Origin": _origin()}, timeout=10)
    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == _origin()


def test_the_allowed_origin_is_our_extension() -> None:
    origins = LocalBridge.allowed_origins()
    assert f"chrome-extension://{extension_id()}" in origins
    assert all(origin.startswith("chrome-extension://") for origin in origins)


# --- malformed input ---------------------------------------------------------


def test_an_unknown_path_is_a_404(bridge: LocalBridge) -> None:
    response = httpx.post(
        f"http://127.0.0.1:{bridge.port}/nope",
        content=b"{}",
        headers={"Origin": _origin()},
        timeout=10,
    )
    assert response.status_code == 404


def test_a_body_that_is_not_json_is_a_400(bridge: LocalBridge) -> None:
    response = httpx.post(
        bridge.url,
        content=b"not json",
        headers={"Origin": _origin(), "Content-Type": "application/json"},
        timeout=10,
    )
    assert response.status_code == 400


def test_a_json_array_is_refused(bridge: LocalBridge) -> None:
    assert _post(bridge, [1, 2, 3], origin=_origin()).status_code == 400


def test_an_empty_body_is_refused(bridge: LocalBridge) -> None:
    response = httpx.post(bridge.url, content=b"", headers={"Origin": _origin()}, timeout=10)
    assert response.status_code == 413


def test_an_oversized_body_is_refused(bridge: LocalBridge) -> None:
    blob = b"x" * (local_bridge.MAX_BODY_BYTES + 1)
    response = httpx.post(bridge.url, content=blob, headers={"Origin": _origin()}, timeout=30)
    assert response.status_code == 413


def test_every_oversized_refusal_is_readable(bridge: LocalBridge) -> None:
    """The refusal used to arrive as a reset instead of a 413, about a third of
    the time.

    The server answered while the peer was still uploading, so the socket went
    away with the upload in flight and the client lost the response to a RST
    (``WinError 10054``). Reading the body first makes it deterministic - and a
    single request is not enough to catch a race, hence the burst.
    """

    blob = b"x" * (local_bridge.MAX_BODY_BYTES + 1)
    for attempt in range(20):
        response = httpx.post(
            bridge.url, content=blob, headers={"Origin": _origin()}, timeout=30
        )
        assert response.status_code == 413, f"第 {attempt + 1} 次超限请求没有拿到 413"
        assert response.json()["ok"] is False


def test_a_body_far_past_the_limit_is_also_refused_cleanly(bridge: LocalBridge) -> None:
    """The drain must not stop at the size limit.

    A body several times the limit is refused just as reliably; capping how much
    of it we read at the request limit left the rest on the wire and brought the
    reset back.
    """

    blob = b"x" * (4 * local_bridge.MAX_BODY_BYTES)
    for _ in range(3):
        response = httpx.post(
            bridge.url, content=blob, headers={"Origin": _origin()}, timeout=30
        )
        assert response.status_code == 413


def _port_of(bridge: LocalBridge) -> int:
    return int(bridge.url.rsplit(":", 1)[1].split("/")[0])


def _head(port: int, length: int) -> bytes:
    return (
        f"POST /session HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Origin: {_origin()}\r\n"
        f"Content-Length: {length}\r\n\r\n"
    ).encode("ascii")


def _status_line(sock: socket.socket, timeout: float = 20.0) -> str:
    sock.settimeout(timeout)
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except TimeoutError:
        # Not the same as a reset: nothing was cut off, we simply waited and
        # the server never spoke.
        return "<timeout>"
    except OSError:
        return "<reset>"
    return data.split(b"\r\n", 1)[0].decode("latin-1")


@pytest.mark.parametrize(
    "size",
    [
        local_bridge.MAX_BODY_BYTES + 1,
        2 * 1024 * 1024,
        local_bridge.DRAIN_MAX_BYTES,
    ],
    ids=["limit+1", "2MiB", "drain-budget"],
)
def test_every_size_up_to_the_drain_budget_gets_a_413(
    bridge: LocalBridge, size: int
) -> None:
    """Up to the drain budget the refusal is deterministic, not a race."""

    response = httpx.post(
        bridge.url, content=b"x" * size, headers={"Origin": _origin()}, timeout=30
    )
    assert response.status_code == 413
    assert response.json()["ok"] is False


def test_a_body_past_the_drain_budget_is_cut_off(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 32 MiB upload must not turn a refusal into a 32 MiB transfer."""

    read = {"bytes": 0}
    original = local_bridge._Handler._drain_request_body

    def spy(self) -> None:  # noqa: ANN001 - bound to the handler
        real_read = self.rfile.read1

        def counting(size: int = -1) -> bytes:
            data = real_read(size)
            read["bytes"] += len(data)
            return data

        self.rfile.read1 = counting  # type: ignore[method-assign]
        try:
            original(self)
        finally:
            self.rfile.read1 = real_read  # type: ignore[method-assign]

    monkeypatch.setattr(local_bridge._Handler, "_drain_request_body", spy)

    port = _port_of(bridge)
    body = b"x" * (32 * 1024 * 1024)
    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
        sock.sendall(_head(port, len(body)))
        # The server is allowed to hang up once its budget is spent.
        with contextlib.suppress(OSError):
            sock.sendall(body)
        _status_line(sock)

    assert read["bytes"] <= local_bridge.DRAIN_MAX_BYTES + 128 * 1024, (
        f"排空读了 {read['bytes']} 字节，超过 {local_bridge.DRAIN_MAX_BYTES} 的预算"
    )


def test_a_continuously_sending_peer_is_cut_off_by_the_deadline(
    bridge: LocalBridge,
) -> None:
    """A peer that never pauses must still hit the wall-clock deadline.

    ``settimeout`` alone only catches silence; a drip feed would otherwise be
    followed for as long as it likes.
    """

    port = _port_of(bridge)
    with socket.create_connection(("127.0.0.1", port), timeout=20) as sock:
        sock.sendall(_head(port, 1024**3))
        started = time.monotonic()
        while time.monotonic() - started < 8:
            ready, _, _ = select.select([sock], [], [], 0.05)
            if ready:
                break
            try:
                sock.send(b"x")
            except OSError:
                break
        elapsed = time.monotonic() - started
        status = _status_line(sock)

    assert elapsed < local_bridge.DRAIN_TIMEOUT_SECONDS + 2.0, (
        f"排空没有在墙钟截止时间内结束（{elapsed:.1f}s）"
    )
    if status == "HTTP/1.1 413 Request Entity Too Large":
        return
    # The deadline's promise is that the peer gets cut off, not that it wins the
    # race to read the refusal: a reset may still swallow the response. That is
    # only acceptable when the connection really is dead - anything else (a
    # timeout, a half-answer) means the server kept following the upload.
    assert status in {"<reset>", ""}, f"截止后既没收到 413 也没被切断：{status!r}"
    try:
        sock.settimeout(2.0)
        sock.send(b"x")
        # A clean FIN shows up as an empty read; a reset shows up as an
        # exception. Both mean the peer is gone, which is what the deadline
        # promises - anything readable here means the server kept following it.
        assert sock.recv(1) == b"", "服务端没有切断仍在发送的 peer"
    except TimeoutError:
        raise AssertionError("连接仍然活着：截止后 2 秒内既没有关闭也没有数据") from None
    except OSError:
        pass


def test_a_silent_peer_with_a_huge_content_length_is_answered_promptly(
    bridge: LocalBridge,
) -> None:
    """Declaring 1 GiB and then saying nothing must not hang the refusal."""

    port = _port_of(bridge)
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=20) as sock:
        sock.sendall(_head(port, 1024**3))
        status = _status_line(sock)
    elapsed = time.monotonic() - started

    assert status == "HTTP/1.1 413 Request Entity Too Large"
    assert elapsed < local_bridge.DRAIN_TIMEOUT_SECONDS + 2.0, f"等待了 {elapsed:.1f}s"


def test_a_fully_read_invalid_body_is_answered_without_waiting(
    bridge: LocalBridge,
) -> None:
    """The 400 paths must not drain a body they already consumed.

    Reading ``Content-Length`` bytes twice meant the second read waited for
    bytes that would never arrive, adding the full drain deadline to every
    malformed payload.
    """

    blob = b"not json at all"
    port = _port_of(bridge)
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=20) as sock:
        sock.sendall(_head(port, len(blob)) + blob)
        status = _status_line(sock)
    elapsed = time.monotonic() - started

    assert status == "HTTP/1.1 400 Bad Request"
    assert elapsed < 0.5, f"完整读取后的 400 仍等待了 {elapsed:.2f}s"


def _wait_for_connection_threads(baseline: int, timeout: float = 5.0) -> int:
    """Wait until the per-connection threads are gone again."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if threading.active_count() <= baseline:
            break
        time.sleep(0.02)
    return threading.active_count()


def _read_response(sock: socket.socket, timeout: float = 20.0) -> tuple[str, bytes]:
    """Read one whole response, body included.

    Closing a socket while its receive buffer still holds the body turns the
    close into a reset on Windows, and the server then logs it as an error.
    Tests that are done with a connection read it out first.
    """

    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            with contextlib.suppress(ValueError):
                length = int(value.strip())
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, body


def _wait_until_closed(sock: socket.socket, timeout: float) -> float | None:
    """Seconds until the peer closed the connection, or None if it stayed open."""

    started = time.monotonic()
    sock.settimeout(timeout)
    try:
        while True:
            if not sock.recv(4096):
                return time.monotonic() - started
    except TimeoutError:
        return None
    except OSError:
        # A reset counts as closed: the handler is finished either way.
        return time.monotonic() - started


def test_a_silent_connection_is_dropped(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connecting and saying nothing must not park a handler thread.

    ``_Handler`` inherited ``StreamRequestHandler.timeout = None``, so the
    request-line ``readline`` blocked until the peer went away - one thread per
    connection, and ``ThreadingHTTPServer`` has no cap on connections.
    """

    monkeypatch.setattr(local_bridge, "REQUEST_READ_TIMEOUT_SECONDS", 0.5)
    port = _port_of(bridge)
    baseline = threading.active_count()

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        elapsed = _wait_until_closed(sock, timeout=5)

    assert elapsed is not None, "静默连接没有被关闭"
    assert elapsed < 3.0, f"静默连接存活了 {elapsed:.1f}s"
    assert _wait_for_connection_threads(baseline) <= baseline, "处理线程没有释放"


def test_a_half_request_line_is_dropped(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_bridge, "REQUEST_READ_TIMEOUT_SECONDS", 0.5)
    port = _port_of(bridge)
    baseline = threading.active_count()

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(b"POST /ses")
        elapsed = _wait_until_closed(sock, timeout=5)

    assert elapsed is not None, "半截请求行没有被关闭"
    assert elapsed < 3.0, f"半截请求行存活了 {elapsed:.1f}s"
    assert _wait_for_connection_threads(baseline) <= baseline, "处理线程没有释放"


def test_unterminated_headers_are_dropped(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blank line never arrives, so ``parse_headers`` waits forever."""

    monkeypatch.setattr(local_bridge, "REQUEST_READ_TIMEOUT_SECONDS", 0.5)
    port = _port_of(bridge)
    baseline = threading.active_count()

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(
            b"POST /session HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode()
            + f"Origin: {_origin()}\r\n".encode()
        )
        elapsed = _wait_until_closed(sock, timeout=5)

    assert elapsed is not None, "不完整 headers 没有被关闭"
    assert elapsed < 3.0, f"不完整 headers 存活了 {elapsed:.1f}s"
    assert _wait_for_connection_threads(baseline) <= baseline, "处理线程没有释放"


def test_a_dripping_header_cannot_outlive_the_deadline(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One header every 200 ms must still hit the deadline.

    A per-read socket timeout is re-armed by every line, so this peer could
    hold the thread forever. Only a wall-clock deadline ends it.
    """

    monkeypatch.setattr(local_bridge, "REQUEST_READ_TIMEOUT_SECONDS", 0.5)
    port = _port_of(bridge)
    baseline = threading.active_count()

    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=15) as sock:
        sock.sendall(b"POST /session HTTP/1.1\r\n")
        while time.monotonic() - started < 6:
            ready, _, _ = select.select([sock], [], [], 0.01)
            if ready:
                break
            try:
                sock.send(b"X-Pad: 1\r\n")
            except OSError:
                break
            time.sleep(0.2)
        elapsed = time.monotonic() - started
        closed = _wait_until_closed(sock, timeout=2) is not None

    assert closed, "滴流 headers 没有被关闭"
    assert elapsed < local_bridge.REQUEST_READ_TIMEOUT_SECONDS + 2.0, (
        f"滴流 headers 没有被墙钟截止时间截断（{elapsed:.1f}s）"
    )
    assert _wait_for_connection_threads(baseline) <= baseline, "处理线程没有释放"


def test_a_normal_request_is_unaffected_by_the_request_deadline(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_bridge, "REQUEST_READ_TIMEOUT_SECONDS", 0.5)
    response = _post(bridge, {"action": "ping"}, origin=_origin())
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_the_body_does_not_inherit_the_request_head_deadline(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head budget has to end where the body begins.

    The headers arrive at 0.35 s of a 0.5 s budget and the body only at 0.7 s.
    If the body read inherited the fraction left over from the head it would be
    refused; the body has its own budget and must be accepted.
    """

    monkeypatch.setattr(local_bridge, "REQUEST_READ_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(local_bridge, "BODY_READ_TIMEOUT_SECONDS", 2.0)
    port = _port_of(bridge)
    body = json.dumps({"action": "ping"}).encode("utf-8")

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        time.sleep(0.35)
        sock.sendall(_head(port, len(body)))
        time.sleep(0.35)
        sock.sendall(body)
        status, payload = _read_response(sock, timeout=8)

    assert status == "HTTP/1.1 200 OK"
    assert json.loads(payload)["ok"] is True


def test_a_half_sent_body_is_abandoned_without_being_parsed(
    bridge: LocalBridge, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that stops mid-way must not pin the handler thread.

    ``rfile.read(length)`` had no deadline at all: a peer could declare 4 KiB,
    send twenty bytes and hold the worker thread for as long as it kept the
    socket open - one thread per connection, with no cap on connections. The
    deadline bounds that, and the half body must never be parsed.
    """

    monkeypatch.setattr(local_bridge, "BODY_READ_TIMEOUT_SECONDS", 0.5)
    port = _port_of(bridge)
    body = json.dumps(_bilibili_payload()).encode("utf-8")
    baseline = threading.active_count()

    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(_head(port, len(body)) + body[:20])
        status, _ = _read_response(sock, timeout=8)
    elapsed = time.monotonic() - started

    from core.session_store import session_file

    assert status == "HTTP/1.1 408 Request Timeout"
    assert elapsed < 5.0, f"超时后仍等待了 {elapsed:.1f}s"
    assert not session_file(settings, Platform.BILIBILI).exists(), "不完整的请求体被解析了"
    assert _wait_for_connection_threads(baseline) <= baseline, "处理线程没有释放"


def test_a_dripping_body_cannot_outlive_the_deadline(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One byte at a time must not reset the budget.

    A per-read timeout would be re-armed by every byte, so this peer could hold
    the thread forever. The deadline is wall clock, which is what this measures.
    """

    monkeypatch.setattr(local_bridge, "BODY_READ_TIMEOUT_SECONDS", 0.5)
    port = _port_of(bridge)
    baseline = threading.active_count()

    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=15) as sock:
        sock.sendall(_head(port, 100_000))
        while time.monotonic() - started < 6:
            ready, _, _ = select.select([sock], [], [], 0.01)
            if ready:
                break
            try:
                sock.send(b"x")
            except OSError:
                break
            time.sleep(0.02)
        elapsed = time.monotonic() - started
        # The answer itself may be lost to a reset while this peer is still
        # writing; that is TCP, not the deadline. The bound below is the point.
        with contextlib.suppress(OSError):
            _read_response(sock, timeout=8)

    assert elapsed < local_bridge.BODY_READ_TIMEOUT_SECONDS + 2.0, (
        f"滴流请求体没有被墙钟截止时间截断（{elapsed:.1f}s）"
    )
    assert _wait_for_connection_threads(baseline) <= baseline, "处理线程没有释放"


def test_a_body_split_across_two_writes_is_accepted(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline must not turn a slow-but-honest upload into a 408."""

    monkeypatch.setattr(local_bridge, "BODY_READ_TIMEOUT_SECONDS", 1.0)
    port = _port_of(bridge)
    body = json.dumps({"action": "ping"}).encode("utf-8")

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(_head(port, len(body)) + body[:5])
        time.sleep(0.2)
        sock.sendall(body[5:])
        status, _ = _read_response(sock, timeout=8)

    assert status == "HTTP/1.1 200 OK"


def test_a_normal_request_is_unaffected_by_the_body_deadline(
    bridge: LocalBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_bridge, "BODY_READ_TIMEOUT_SECONDS", 0.5)
    response = _post(bridge, {"action": "ping"}, origin=_origin())
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_get_is_refused(bridge: LocalBridge) -> None:
    response = httpx.get(bridge.url, headers={"Origin": _origin()}, timeout=10)
    assert response.status_code == 405
    assert response.headers.get("Connection") == "close"


def test_a_refusal_closes_the_connection(bridge: LocalBridge) -> None:
    """An unread body must not be parsed as the next request."""

    response = _post(bridge, {"action": "ping"}, origin="https://evil.example")
    assert response.status_code == 403
    assert response.headers.get("Connection") == "close"


# --- the shared gate ---------------------------------------------------------


def test_the_same_validation_as_the_native_host_applies(bridge: LocalBridge) -> None:
    """A cross-platform payload must be refused here too."""

    payload = _bilibili_payload()
    payload["platform"] = "instagram"
    response = _post(bridge, payload, origin=_origin())
    assert response.json()["ok"] is False


def test_a_foreign_domain_is_refused(bridge: LocalBridge, settings: Settings) -> None:
    payload = _bilibili_payload()
    payload["cookies"] = [{"name": "SESSDATA", "value": "X", "domain": ".example.com", "path": "/"}]
    response = _post(bridge, payload, origin=_origin())
    assert response.json()["ok"] is False

    from core.session_store import session_file

    assert not session_file(settings, Platform.BILIBILI).exists()


def test_an_unknown_action_is_answered_not_crashed(bridge: LocalBridge) -> None:
    response = _post(bridge, {"action": "wipe"}, origin=_origin())
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_the_bridge_keeps_serving_after_a_bad_request(bridge: LocalBridge) -> None:
    _post(bridge, {"action": "wipe"}, origin=_origin())
    assert _post(bridge, {"action": "ping"}, origin=_origin()).json()["ok"] is True


def test_sessions_stay_isolated(bridge: LocalBridge, settings: Settings) -> None:
    _post(bridge, _bilibili_payload(), origin=_origin())
    from core.session_store import session_file

    assert session_file(settings, Platform.BILIBILI).is_file()
    assert not session_file(settings, Platform.INSTAGRAM).exists()
