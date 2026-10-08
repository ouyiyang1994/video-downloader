"""Second way in: read cookies through the Chrome DevTools Protocol.

The extension (see :mod:`core.chrome_bridge`) is the preferred route. This
module is the fallback for a user who would rather not install anything: Chrome
is started with remote debugging on a **dedicated profile owned by this
application**, and ``Network.getAllCookies`` is asked for the platform's
cookies. Chrome decrypts them, exactly as it does for the extension - nothing
here touches a cookie database, a master key, or another browser's profile.

The trade-offs are real and the caller must surface them:

* Chrome 136 and later refuse ``--remote-debugging-port`` when the *default*
  profile is used, so a separate profile is mandatory - which means the user has
  to sign in once inside that profile. The session then persists there, so it is
  a one-time cost per platform.
* Chrome must be launched by this application (or with the same flags by hand).

Everything is loopback-only: the debugging port is bound to ``127.0.0.1`` and
the profile lives under the application's data root.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import secrets
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import Settings
from core.exceptions import ChromeBridgeError

logger = logging.getLogger(__name__)

#: Where the dedicated profile lives, under the data root.
PROFILE_DIRNAME = "chrome-profile"

#: How long to wait for Chrome to publish its debugging endpoint.
STARTUP_TIMEOUT = 30.0
#: Per-command timeout once connected.
COMMAND_TIMEOUT = 20.0

_LOOPBACK = "127.0.0.1"


# --- minimal WebSocket client ------------------------------------------------
#
# CDP only speaks WebSocket, and pulling in a dependency for one call on
# loopback is not worth it: this is the ~80 lines of RFC 6455 a client needs.
# Only text frames, fragmentation and the control frames are implemented.


class WebSocketError(Exception):
    """The WebSocket handshake or framing failed."""


class WebSocket:
    """A tiny text-only WebSocket client, good enough for CDP on loopback."""

    def __init__(self, url: str, *, timeout: float = COMMAND_TIMEOUT) -> None:
        self.url = url
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = bytearray()

    def __enter__(self) -> WebSocket:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> None:
        scheme, _, remainder = self.url.partition("://")
        if scheme != "ws":
            raise WebSocketError(f"只支持 ws:// 地址：{scheme}://")
        hostport, _, path = remainder.partition("/")
        host, _, port_text = hostport.partition(":")
        port = int(port_text or 80)
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as exc:
            raise WebSocketError(f"无法连接 {host}:{port}（{type(exc).__name__}）") from exc
        sock.settimeout(self.timeout)
        self._socket = sock

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        header = self._read_until(b"\r\n\r\n")
        if b" 101" not in header.split(b"\r\n", 1)[0]:
            first = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            raise WebSocketError(f"WebSocket 握手失败：{first}")

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is None:
            return
        with contextlib.suppress(OSError):
            sock.sendall(self._frame(0x8, b""))
        with contextlib.suppress(OSError):
            sock.close()

    # -- framing ------------------------------------------------------------
    def send_text(self, text: str) -> None:
        if self._socket is None:
            raise WebSocketError("连接已关闭")
        self._socket.sendall(self._frame(0x1, text.encode("utf-8")))

    def recv_text(self) -> str:
        """Return the next text message, answering pings on the way."""

        chunks: list[bytes] = []
        while True:
            final, opcode, payload = self._read_frame()
            if opcode == 0x9:  # ping
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise WebSocketError("对端关闭了连接")
            if opcode in (0x1, 0x2, 0x0):
                chunks.append(payload)
                if final:
                    return b"".join(chunks).decode("utf-8", "replace")
                continue
            raise WebSocketError(f"不支持的 WebSocket 帧类型：{opcode}")

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return bytes(header) + masked

    def _send_control(self, opcode: int, payload: bytes) -> None:
        if self._socket is None:
            return
        with contextlib.suppress(OSError):
            self._socket.sendall(self._frame(opcode, payload))

    def _read_frame(self) -> tuple[bool, int, bytes]:
        header = self._read_exactly(2)
        final = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exactly(8))[0]
        mask = self._read_exactly(4) if masked else b""
        payload = self._read_exactly(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return final, opcode, payload

    # -- raw io -------------------------------------------------------------
    def _read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            if self._socket is None:
                raise WebSocketError("连接已关闭")
            chunk = self._socket.recv(65536)
            if not chunk:
                raise WebSocketError("连接在读取过程中被关闭")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            if self._socket is None:
                raise WebSocketError("连接已关闭")
            chunk = self._socket.recv(65536)
            if not chunk:
                raise WebSocketError("握手响应不完整")
            self._buffer.extend(chunk)
        index = self._buffer.index(marker) + len(marker)
        data = bytes(self._buffer[:index])
        del self._buffer[:index]
        return data


# --- CDP ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DebugChrome:
    """A Chrome instance started by this application with debugging enabled."""

    process: subprocess.Popen[bytes]
    port: int
    profile_dir: Path
    executable: Path

    @property
    def endpoint(self) -> str:
        return f"http://{_LOOPBACK}:{self.port}"

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:  # pragma: no cover - already gone
                return
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn process
                self.process.kill()


def profile_dir(settings: Settings) -> Path:
    """The dedicated profile, under the data root (never the user's own).

    Configurable so the test suite never writes into a real checkout.
    """

    return settings.resolve_path(settings.chrome_profile_dir)


def chrome_executable() -> Path | None:
    """Chrome, wherever this machine keeps it."""

    from core.chrome_bridge import browser_executable

    return browser_executable("chrome")


def launch(
    settings: Settings,
    *,
    url: str = "about:blank",
    timeout: float = STARTUP_TIMEOUT,
    headless: bool = False,
) -> DebugChrome:
    """Start Chrome with debugging on, on the application's own profile.

    Deliberately *not* the user's default profile: Chrome 136+ refuses the
    debugging port there, and reading another profile would be exactly the kind
    of sideways access this project avoids. ``headless`` exists for the
    automated test suite, which has no one to sign in.
    """

    executable = chrome_executable()
    if executable is None:
        raise ChromeBridgeError(
            "未找到 Chrome",
            detail="备选方案需要本机安装 Google Chrome。也可以改用登录助手扩展或手动填写会话。",
        )

    directory = profile_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    command = [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={_LOOPBACK}",
        f"--user-data-dir={directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    if headless:
        command.append("--headless=new")
    command.append(url)
    logger.info("以独立配置启动 Chrome（端口 %d）", port)
    try:
        process = subprocess.Popen(  # noqa: S603 - a fixed path plus fixed flags
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise ChromeBridgeError("无法启动 Chrome", detail=f"{type(exc).__name__}：{exc}") from exc

    instance = DebugChrome(process=process, port=port, profile_dir=directory, executable=executable)
    if not _wait_for_endpoint(instance, timeout=timeout):
        instance.stop()
        raise ChromeBridgeError(
            "Chrome 没有在预期时间内开启调试端口",
            detail=(
                "请确认没有其他程序占用该端口，并且本机的 Chrome 允许以 "
                "--remote-debugging-port 启动。"
            ),
        )
    return instance


def fetch_cookies(
    instance: DebugChrome, domain_suffix: str, *, timeout: float = COMMAND_TIMEOUT
) -> list[dict[str, Any]]:
    """Ask Chrome for every cookie belonging to ``domain_suffix``.

    Chrome decrypts each value itself, so App-Bound Encryption is not an
    obstacle here - the browser is the one doing the reading.
    """

    from core.session_store import matches_domain

    with WebSocket(_page_socket_url(instance), timeout=timeout) as socket_:
        _command(socket_, 1, "Network.enable")
        result = _command(socket_, 2, "Network.getAllCookies")
        cookies = result.get("cookies")
        if not isinstance(cookies, list):
            raise ChromeBridgeError(
                "Chrome 没有返回 Cookie 列表",
                detail="Network.getAllCookies 的响应缺少 cookies 字段。",
            )
        return [
            cookie
            for cookie in cookies
            if isinstance(cookie, dict)
            and matches_domain(str(cookie.get("domain") or ""), domain_suffix)
        ]


def _number(value: Any) -> float | None:
    """``value`` as a number, ignoring booleans and anything else."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _expiry_of(cookie: dict[str, Any]) -> float | None:
    """The cookie's expiry in the wire spelling, or ``None`` for a session one.

    CDP writes ``expires`` and uses ``-1`` to mean "no expiry at all"; the host's
    validator reads ``expirationDate``. Accepting both spellings keeps the
    conversion idempotent, so a caller that converts twice cannot silently lose
    the real expiry.
    """

    expires = _number(cookie.get("expires"))
    if expires is None or expires <= 0:
        expires = _number(cookie.get("expirationDate"))
    return expires


def to_wire_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert CDP cookies into the payload :mod:`core.native_host` accepts.

    Reusing the host's validator means both acquisition paths go through exactly
    one gate before anything is written to disk. Already-converted input is
    passed through unchanged, so a caller cannot lose the expiry by converting
    twice.
    """

    wire: list[dict[str, Any]] = []
    for cookie in cookies:
        wire.append(
            {
                "name": cookie.get("name"),
                "value": cookie.get("value"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure", True)),
                "expirationDate": _expiry_of(cookie),
            }
        )
    return wire


def store(settings: Settings, platform: str, cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """Hand CDP cookies to the same validator the native host uses.

    Sharing the gate matters: both acquisition paths must apply the identical
    domain and authentication-cookie rules before anything is written. The
    translation happens here rather than at the call site so that no caller can
    accidentally hand the gate raw CDP cookies - which would silently turn a
    real expiry into "a session cookie that lasts a year".
    """

    from core.native_host import store_cookies

    return store_cookies(settings, platform, to_wire_cookies(cookies))


# --- internals ----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_LOOPBACK, 0))
        return int(probe.getsockname()[1])


def _http_json(instance: DebugChrome, path: str, *, timeout: float = 5.0) -> Any:
    import httpx

    url = f"{instance.endpoint}{path}"
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def _wait_for_endpoint(instance: DebugChrome, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if instance.process.poll() is not None:
            logger.warning("Chrome 在开启调试端口前退出（code=%s）", instance.process.returncode)
            return False
        try:
            _http_json(instance, "/json/version")
        except Exception:  # noqa: BLE001 - not up yet, or not answering
            time.sleep(0.4)
            continue
        return True
    return False


def _page_socket_url(instance: DebugChrome) -> str:
    """The WebSocket URL of a page target, opening one if Chrome has none."""

    targets = _http_json(instance, "/json/list")
    if isinstance(targets, list):
        for target in targets:
            if (
                isinstance(target, dict)
                and target.get("type") == "page"
                and target.get("webSocketDebuggerUrl")
            ):
                return str(target["webSocketDebuggerUrl"])
    # Chrome started with no page (for example about:blank was closed).
    created = _http_json(instance, "/json/new?about:blank", timeout=10.0)
    if isinstance(created, dict) and created.get("webSocketDebuggerUrl"):
        return str(created["webSocketDebuggerUrl"])
    raise ChromeBridgeError(
        "Chrome 没有可用的调试目标",
        detail="无法连接到任何标签页，请重试或改用登录助手扩展。",
    )


def _command(
    socket_: WebSocket,
    identifier: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one CDP command and return its ``result``."""

    payload: dict[str, Any] = {"id": identifier, "method": method}
    if params:
        payload["params"] = params
    socket_.send_text(json.dumps(payload))
    while True:
        message = json.loads(socket_.recv_text())
        if message.get("id") != identifier:
            continue  # an event, not our reply
        error = message.get("error")
        if error:
            raise ChromeBridgeError(
                f"Chrome 拒绝了 {method}",
                detail=str(error.get("message") or error)[:200],
            )
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def cleanup(settings: Settings) -> bool:
    """Delete the dedicated profile. Only ever called on explicit request."""

    import shutil

    directory = profile_dir(settings)
    if not directory.exists():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


class DebugSession:
    """Keeps at most one debugging Chrome alive for the whole application.

    Launching Chrome is slow and the user has to sign in inside it, so the
    instance has to survive across the several GUI actions that make up the
    fallback flow (open -> sign in -> fetch). Owning it in one small object also
    means there is exactly one place that can close it.
    """

    def __init__(self) -> None:
        self._instance: DebugChrome | None = None

    @property
    def instance(self) -> DebugChrome | None:
        if self._instance is not None and self._instance.process.poll() is not None:
            self._instance = None
        return self._instance

    def ensure(self, settings: Settings, *, url: str) -> DebugChrome:
        """Return the running instance, starting one pointed at ``url`` if needed."""

        current = self.instance
        if current is not None:
            return current
        self._instance = launch(settings, url=url)
        return self._instance

    def close(self) -> bool:
        current = self.instance
        if current is None:
            self._instance = None
            return False
        current.stop()
        self._instance = None
        return True


#: The instance the GUI drives.
_shared_session = DebugSession()


def shared_session() -> DebugSession:
    return _shared_session


def is_windows() -> bool:  # pragma: no cover - trivial
    return os.name == "nt"
