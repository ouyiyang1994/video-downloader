"""Tests for the DevTools-protocol fallback.

The WebSocket client is exercised against byte streams rather than a server, so
the framing rules (masking, fragmentation, control frames) are checked exactly.
A real Chrome run is opt-in through ``VD_LIVE_CHROME=1`` - it launches a browser
and is therefore not something a normal test run should do.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any

import pytest

from config.settings import Settings
from core import chrome_cdp
from core.chrome_cdp import WebSocket, WebSocketError
from core.exceptions import ChromeBridgeError


class FakeSocket:
    """Feeds canned bytes to the client and records what it sent."""

    def __init__(self, incoming: bytes = b"") -> None:
        self._incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def recv(self, count: int) -> bytes:
        chunk = bytes(self._incoming[:count])
        del self._incoming[:count]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def settimeout(self, value: float) -> None:  # pragma: no cover - trivial
        return None

    def close(self) -> None:
        self.closed = True


def server_frame(opcode: int, payload: bytes, *, final: bool = True) -> bytes:
    """A frame as a server sends it: unmasked."""

    header = bytearray([(0x80 if final else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", length))
    return bytes(header) + payload


def connected(incoming: bytes = b"") -> tuple[WebSocket, FakeSocket]:
    socket_ = FakeSocket(incoming)
    client = WebSocket("ws://127.0.0.1:9222/devtools/page/x")
    client._socket = socket_  # noqa: SLF001 - the handshake needs a live Chrome
    return client, socket_


# --- framing -----------------------------------------------------------------


def test_a_text_frame_is_received() -> None:
    client, _socket = connected(server_frame(0x1, "你好".encode()))
    assert client.recv_text() == "你好"


def test_a_long_frame_is_received() -> None:
    payload = "x" * 70000
    client, _socket = connected(server_frame(0x1, payload.encode()))
    assert client.recv_text() == payload


def test_a_fragmented_message_is_reassembled() -> None:
    client, _socket = connected(
        server_frame(0x1, b'{"id":1,', final=False) + server_frame(0x0, b'"result":{}}', final=True)
    )
    assert client.recv_text() == '{"id":1,"result":{}}'


def test_a_ping_is_answered_with_a_pong() -> None:
    client, socket_ = connected(server_frame(0x9, b"hi") + server_frame(0x1, b"payload"))
    assert client.recv_text() == "payload"
    # 0x8A = FIN + pong opcode, and the client must mask what it sends.
    assert socket_.sent[0] == 0x8A
    assert socket_.sent[1] & 0x80, "客户端发送的帧必须带掩码"


def test_a_close_frame_ends_the_stream() -> None:
    client, _socket = connected(server_frame(0x8, b""))
    with pytest.raises(WebSocketError):
        client.recv_text()


def test_a_truncated_frame_is_reported() -> None:
    client, _socket = connected(b"\x81")
    with pytest.raises(WebSocketError):
        client.recv_text()


def test_an_unsupported_opcode_is_reported() -> None:
    client, _socket = connected(server_frame(0x3, b"data"))
    with pytest.raises(WebSocketError):
        client.recv_text()


def test_outgoing_frames_are_masked_and_sized_correctly() -> None:
    client, socket_ = connected()
    client.send_text("a")
    assert socket_.sent[0] == 0x81
    assert socket_.sent[1] == 0x80 | 1
    assert len(socket_.sent) == 2 + 4 + 1

    socket_.sent.clear()
    client.send_text("y" * 200)
    assert socket_.sent[1] == 0x80 | 126
    assert len(socket_.sent) == 4 + 4 + 200


def test_a_non_websocket_url_is_refused() -> None:
    client = WebSocket("http://127.0.0.1:9222/json")
    with pytest.raises(WebSocketError):
        client.connect()


def test_sending_on_a_closed_connection_is_an_error() -> None:
    client = WebSocket("ws://127.0.0.1:1/x")
    with pytest.raises(WebSocketError):
        client.send_text("nope")


def test_a_failed_handshake_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    socket_ = FakeSocket(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    monkeypatch.setattr(chrome_cdp.socket, "create_connection", lambda *a, **k: socket_)
    with pytest.raises(WebSocketError) as info:
        WebSocket("ws://127.0.0.1:9222/x").connect()
    assert "握手失败" in str(info.value)


def test_a_successful_handshake_leaves_the_buffer_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    socket_ = FakeSocket(b"HTTP/1.1 101 Switching Protocols\r\n\r\n" + server_frame(0x1, b"ok"))
    monkeypatch.setattr(chrome_cdp.socket, "create_connection", lambda *a, **k: socket_)
    with WebSocket("ws://127.0.0.1:9222/x") as client:
        assert client.recv_text() == "ok"


# --- cookie translation ------------------------------------------------------


def test_cdp_cookies_are_translated_to_the_hosts_payload() -> None:
    wire = chrome_cdp.to_wire_cookies(
        [
            {
                "name": "SESSDATA",
                "value": "V",
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
                "expires": 4102444800,
            }
        ]
    )
    assert wire == [
        {
            "name": "SESSDATA",
            "value": "V",
            "domain": ".bilibili.com",
            "path": "/",
            "secure": True,
            "expirationDate": 4102444800,
        }
    ]


@pytest.mark.parametrize("expires", [-1, 0, None, "soon"])
def test_a_session_cookie_has_no_expiry(expires: Any) -> None:
    wire = chrome_cdp.to_wire_cookies([{"name": "a", "value": "b", "expires": expires}])
    assert wire[0]["expirationDate"] is None
    assert wire[0]["path"] == "/"


def test_converting_twice_keeps_the_expiry() -> None:
    """``store`` converts, and a caller may have converted already."""

    once = chrome_cdp.to_wire_cookies([{"name": "a", "value": "b", "expires": 4102444800}])
    assert chrome_cdp.to_wire_cookies(once) == once


def test_store_translates_raw_cdp_cookies_before_the_gate(settings: Settings) -> None:
    """``expires`` is CDP's spelling; the validator only reads ``expirationDate``.

    Handing the gate raw cookies would silently turn every real expiry into "a
    session cookie that lasts a year" - including one that has already expired.
    """

    expired = [
        {"name": "SESSDATA", "value": "V", "domain": ".bilibili.com", "path": "/", "expires": 1}
    ]
    assert chrome_cdp.store(settings, "bilibili", expired)["ok"] is False

    fresh = [
        {
            "name": "SESSDATA",
            "value": "V",
            "domain": ".bilibili.com",
            "path": "/",
            "expires": 4102444800,
        }
    ]
    assert chrome_cdp.store(settings, "bilibili", fresh)["ok"] is True

    from core.models import Platform
    from core.session_store import session_file

    body = session_file(settings, Platform.BILIBILI).read_text(encoding="utf-8")
    assert "4102444800" in body, "真实过期时间必须写进会话文件"


# --- profile and launch ------------------------------------------------------


def test_the_debug_profile_lives_under_the_data_root(settings: Settings) -> None:
    assert chrome_cdp.profile_dir(settings) == settings.chrome_profile_dir


def test_launch_without_chrome_explains_the_alternatives(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome_cdp, "chrome_executable", lambda: None)
    with pytest.raises(ChromeBridgeError) as info:
        chrome_cdp.launch(settings)
    assert "Chrome" in info.value.message
    assert "扩展" in (info.value.detail or "")


def test_a_free_port_is_actually_free() -> None:
    import socket as socket_module

    port = chrome_cdp._free_port()  # noqa: SLF001 - small, pure helper
    with socket_module.socket() as probe:
        probe.bind(("127.0.0.1", port))  # would raise if still held


def test_cleanup_only_removes_the_dedicated_profile(settings: Settings) -> None:
    directory = chrome_cdp.profile_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "marker").write_text("x", encoding="utf-8")

    assert chrome_cdp.cleanup(settings) is True
    assert not directory.exists()
    assert chrome_cdp.cleanup(settings) is False


# --- the shared instance -----------------------------------------------------


class _FakeProcess:
    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.terminated = False

    def poll(self) -> int | None:
        return None if self._running else 0

    def terminate(self) -> None:
        self.terminated = True
        self._running = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_the_shared_session_forgets_a_dead_browser() -> None:
    session = chrome_cdp.DebugSession()
    session._instance = chrome_cdp.DebugChrome(  # noqa: SLF001 - white-box on purpose
        process=_FakeProcess(running=False),  # type: ignore[arg-type]
        port=1,
        profile_dir=Path("."),
        executable=Path("chrome.exe"),
    )
    assert session.instance is None


def test_the_shared_session_closes_what_it_started() -> None:
    session = chrome_cdp.DebugSession()
    process = _FakeProcess()
    session._instance = chrome_cdp.DebugChrome(  # noqa: SLF001
        process=process,  # type: ignore[arg-type]
        port=1,
        profile_dir=Path("."),
        executable=Path("chrome.exe"),
    )
    assert session.close() is True
    assert process.terminated is True
    assert session.close() is False


def test_ensure_reuses_a_live_instance(settings: Settings) -> None:
    session = chrome_cdp.DebugSession()
    existing = chrome_cdp.DebugChrome(
        process=_FakeProcess(),  # type: ignore[arg-type]
        port=7,
        profile_dir=Path("."),
        executable=Path("chrome.exe"),
    )
    session._instance = existing  # noqa: SLF001
    assert session.ensure(settings, url="about:blank") is existing


def test_the_endpoint_is_loopback_only() -> None:
    instance = chrome_cdp.DebugChrome(
        process=_FakeProcess(),  # type: ignore[arg-type]
        port=9222,
        profile_dir=Path("."),
        executable=Path("chrome.exe"),
    )
    assert instance.endpoint == "http://127.0.0.1:9222"


# --- live check (opt-in) -----------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("VD_LIVE_CHROME") != "1",
    reason="设置 VD_LIVE_CHROME=1 才会真的启动 Chrome",
)
def test_live_chrome_round_trip(settings: Settings) -> None:
    """Real Chrome: set a cookie, read it back through CDP, store the session."""

    from core.chrome_cdp import WebSocket as LiveWebSocket
    from core.chrome_cdp import _command, _page_socket_url

    instance = chrome_cdp.launch(settings, headless=True, url="https://example.com/")
    try:
        with LiveWebSocket(_page_socket_url(instance)) as socket_:
            _command(socket_, 1, "Network.enable")
            _command(
                socket_,
                2,
                "Network.setCookie",
                {
                    "name": "SESSDATA",
                    "value": "FAKE",
                    "domain": ".bilibili.com",
                    "path": "/",
                },
            )
        wire = chrome_cdp.to_wire_cookies(chrome_cdp.fetch_cookies(instance, "bilibili.com"))
        assert chrome_cdp.store(settings, "bilibili", wire)["ok"] is True
        assert (settings.session_dir / "bilibili_cookies.txt").is_file()
    finally:
        instance.stop()
