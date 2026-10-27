"""Loopback HTTP transport, for machines where Chrome cannot start a host.

Chrome's native messaging hands the host process a pair of *inheritable* pipes.
On a locked-down Windows install that can be refused outright: the extension then
gets ``Error when communicating with the native messaging host`` and no host
process ever runs - not ours, not ``cmd.exe``, not ``powershell.exe``. This
module is the fallback for exactly that case, and it speaks the same payload as
:mod:`core.native_host`.

It is deliberately narrow:

* bound to ``127.0.0.1`` only - never a routable interface;
* one route, ``POST`` only, and the request must carry our own extension's
  ``Origin``, which a web page cannot forge (the browser sets it, and the CORS
  preflight for any other origin is answered with a refusal);
* it only listens while the sign-in dialog is open, so there is no long-lived
  socket;
* the payload goes through :func:`core.native_host.store_cookies`, the same gate
  the native host uses, so both transports enforce identical rules.

What it deliberately is not: a way to accept a session from anywhere but the
user's own browser on this machine.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import logging
import socket
import threading
import time
from functools import partial
from typing import Any

from config.settings import Settings

logger = logging.getLogger(__name__)

#: How long to keep reading after our last byte when we are the side hanging up.
#: Closing a socket with unread input in the receive buffer makes Windows answer
#: with RST, and a peer that is still uploading then loses the response we just
#: wrote - a 413 that never arrives looks exactly like a dropped connection.
#: Half-closing and draining for this long lets the refusal land first.
LINGER_CLOSE_SECONDS = 0.5

#: Fixed, because the extension cannot be told an ephemeral one. If something
#: else already holds it, :meth:`LocalBridge.start` says so instead of silently
#: choosing another.
DEFAULT_PORT = 8765
ROUTE = "/session"

#: A session is a few kilobytes; anything larger is a mistake or an attack.
MAX_BODY_BYTES = 512 * 1024

#: How much of a *refused* request's body we are willing to read. The refusal
#: is written after the body is read (see ``_drain_request_body``), so the read
#: has to be capped twice over: a body larger than this is refused without being
#: followed to the end, and a peer that keeps sending is cut off by the deadline
#: below. 8 MiB is 16x the request limit - far past any real payload, small
#: enough that a hostile one cannot turn a refusal into a transfer.
DRAIN_MAX_BYTES = 8 * 1024 * 1024

#: Wall-clock budget for draining one refused body. This is a deadline, not a
#: per-read timeout: ``settimeout`` alone only catches a peer that goes silent,
#: and a peer that keeps sending would otherwise be followed for as long as it
#: likes.
DRAIN_TIMEOUT_SECONDS = 2.0

#: Wall-clock budget for reading one request's line and headers. Also a
#: deadline, and for a sharper reason than the body one: the head is read line
#: by line (``rfile.readline``), and every line is a separate socket operation,
#: so a peer that sends one header every few seconds re-arms a per-read timeout
#: indefinitely. Ten seconds is far past what a browser needs over loopback.
REQUEST_READ_TIMEOUT_SECONDS = 10.0

#: Wall-clock budget for reading one *accepted* request's body. Also a deadline
#: rather than a per-read timeout, for the same reason: a peer that dribbles one
#: byte at a time resets an idle timeout on every byte, and the handler thread
#: would stay parked for as long as that peer keeps going. Ten seconds is far
#: past what a browser needs to deliver a few kilobytes over loopback; it exists
#: so a stalled or hostile upload cannot pin a thread indefinitely.
BODY_READ_TIMEOUT_SECONDS = 10.0


def bridge_url(port: int = DEFAULT_PORT) -> str:
    """The URL the extension posts to."""

    return f"http://127.0.0.1:{port}{ROUTE}"


class _RequestDeadlineReader:
    """``rfile`` wrapper that bounds a whole request head by wall clock.

    ``BaseHTTPRequestHandler`` reads the request line with ``rfile.readline``
    and then the headers one ``readline`` per line. Each call is its own socket
    operation, so ``settimeout`` only catches a peer that goes completely
    silent - a peer sending one header every few seconds resets it every time
    and keeps the worker thread parked for as long as it likes. This wrapper
    re-arms the socket with whatever is *left* of the request budget before
    every read, which makes the limit a deadline instead of an idle timeout.

    The handler clears ``_read_deadline`` as soon as the head has been parsed
    and the wrapper then steps aside completely: the body read and the refusal
    drain have their own budgets and must not inherit the leftover fraction of
    this one.
    """

    def __init__(self, raw: Any, handler: _Handler) -> None:
        self._raw = raw
        self._handler = handler

    def _arm(self) -> None:
        deadline = self._handler._read_deadline
        if not deadline:
            return
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("请求读取超出总截止时间")
        self._handler.connection.settimeout(budget)

    def readline(self, *args: Any) -> Any:
        self._arm()
        return self._raw.readline(*args)

    def read1(self, *args: Any) -> Any:
        self._arm()
        return self._raw.read1(*args)

    def read(self, *args: Any) -> Any:
        self._arm()
        return self._raw.read(*args)

    def __getattr__(self, name: str) -> Any:
        # close(), closed, name - everything else belongs to the real reader.
        return getattr(self._raw, name)


class _Handler(http.server.BaseHTTPRequestHandler):
    """One route, one verb, one accepted origin."""

    protocol_version = "HTTP/1.1"
    server_version = "VideoDownloaderBridge"
    sys_version = ""

    def __init__(
        self,
        *args: Any,
        settings: Settings,
        allowed_origins: frozenset[str],
        **kwargs: Any,
    ) -> None:
        self._settings = settings
        self._allowed_origins = allowed_origins
        #: Bytes of the current request's body already taken off the socket. A
        #: refusal must not read them twice: they will never arrive again, and
        #: the drain would sit there until its deadline.
        self._body_consumed = 0
        #: Wall-clock deadline for the request line + headers currently being
        #: read; 0 means "not reading a request head" (see the reader wrapper).
        self._read_deadline = 0.0
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        super().setup()
        # Wrapping here, not in __init__: rfile only exists after setup().
        # The wrapper only forwards the handful of read methods http.server
        # uses, so it is not a BufferedIOBase - the ignore is about the declared
        # attribute type, not about behaviour.
        self.rfile = _RequestDeadlineReader(self.rfile, self)  # type: ignore[assignment]

    def handle_one_request(self) -> None:
        # A fresh budget per request: keep-alive must not carry the previous
        # request's leftovers, and an idle connection gets closed after it.
        self._read_deadline = time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS
        super().handle_one_request()

    def finish(self) -> None:
        """Close politely when we are the side hanging up.

        A refusal is written while the peer may still be uploading. Closing the
        socket with that input unread turns the close into a reset, and the peer
        loses the response it never read. Half-closing and draining for at most
        :data:`LINGER_CLOSE_SECONDS` gives the response a chance to arrive while
        still bounding how long the worker thread can be held.
        """

        if self.close_connection:
            with contextlib.suppress(OSError):
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(LINGER_CLOSE_SECONDS)
                deadline = time.monotonic() + LINGER_CLOSE_SECONDS
                while time.monotonic() < deadline:
                    if not self.rfile.read1(64 * 1024):
                        break
        super().finish()

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:
            # Hand the socket back exactly as it was before the head was read.
            # The body and the response have their own budgets; inheriting the
            # few hundred milliseconds left here would turn a slow-but-honest
            # upload into a 408.
            self._read_deadline = 0.0
            with contextlib.suppress(OSError):
                self.connection.settimeout(None)

    # -- helpers ------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        logger.debug("本地桥接 %s：%s", self.address_string(), format % args)

    def _drain_request_body(self) -> None:
        """Read a refused request's body before the refusal is written.

        Order matters more than the bytes. Answering while the peer is still
        uploading is a race the client usually loses: the socket goes away with
        its upload in flight, the peer sees a reset (``WinError 10054``) and the
        413 we wrote is never read. Reading the declared body first means the
        refusal is the last thing on a quiet connection, which is what makes it
        reliably visible.

        Two hard bounds keep that from turning a refusal into a transfer: at
        most :data:`DRAIN_MAX_BYTES` bytes, and at most
        :data:`DRAIN_TIMEOUT_SECONDS` of wall clock. Whichever runs out first
        ends the read and the refusal is written anyway - the peer may then lose
        it to a reset, which is the price of not being followable forever.
        """

        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(max(declared - self._body_consumed, 0), DRAIN_MAX_BYTES)
        if remaining <= 0:
            return

        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        previous = self.connection.gettimeout()
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    logger.debug("本地桥接：排空请求体超时，按预算截断")
                    break
                # Re-arm each round: the timeout has to shrink with the budget,
                # or a slow drip could stretch the total past the deadline.
                self.connection.settimeout(budget)
                # read1, not read: read(n) blocks until it has n bytes, so a
                # peer sending one byte at a time would never come back to the
                # deadline check above. read1 returns as soon as anything
                # arrives, which keeps the loop - and the budget - in charge.
                chunk = self.rfile.read1(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # The peer stalled or vanished; the refusal is still owed to it.
            logger.debug("本地桥接：拒绝请求时未能读完请求体")
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(previous)

    def _read_request_body(self, length: int) -> bytes | None:
        """Read exactly ``length`` bytes of body, or give up at the deadline.

        The budget is wall clock (:data:`BODY_READ_TIMEOUT_SECONDS`), not a
        per-read timeout: ``settimeout`` alone is re-armed by every byte a peer
        sends, so a drip feed would keep the handler thread parked forever.
        ``read1`` is what makes the deadline checkable - it returns as soon as
        anything arrives, instead of blocking until a whole chunk has.

        ``None`` means the body did not arrive in full. A partial body is never
        returned, because parsing it would mean acting on a request we did not
        receive; the caller answers 408 and closes, and closing is what releases
        the worker thread.
        """

        deadline = time.monotonic() + BODY_READ_TIMEOUT_SECONDS
        previous = self.connection.gettimeout()
        buffer = bytearray()
        try:
            while len(buffer) < length:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    logger.warning(
                        "本地桥接：请求体在 %.1f 秒内未收全（%d/%d 字节），放弃该连接",
                        BODY_READ_TIMEOUT_SECONDS,
                        len(buffer),
                        length,
                    )
                    return None
                # Re-arm with what is left of the budget, so the socket timeout
                # can never outlive the deadline.
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(min(length - len(buffer), 64 * 1024))
                if not chunk:
                    logger.debug(
                        "本地桥接：请求体提前结束（%d/%d 字节）", len(buffer), length
                    )
                    return None
                buffer.extend(chunk)
                # Counted as it arrives: a refusal must not read these bytes
                # again, and they will never arrive twice.
                self._body_consumed += len(chunk)
        except OSError:
            logger.debug("本地桥接：读取请求体失败（%d/%d 字节）", len(buffer), length)
            return None
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(previous)
        return bytes(buffer)

    def _reply(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        cors: bool = False,
        drain: bool = True,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if status != 200 and drain:
            # Before, not after: see _drain_request_body. Closing a refused
            # request also stops the next read from parsing the unread body as
            # a fresh request.
            self._drain_request_body()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status != 200:
            # Closing stops the next read from parsing an unread body as a
            # fresh request and logging a traceback.
            self.close_connection = True
            self.send_header("Connection", "close")
        if cors:
            # Only ever echo back to the extension that asked, and only after
            # the origin check below has already passed.
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            # The peer hung up before the answer could be written - the usual
            # reason is an upload that stopped mid-body and timed out. There is
            # nothing left to send; closing releases the thread.
            logger.debug("本地桥接：写响应失败，对端可能已断开")
            self.close_connection = True

    def _origin_allowed(self) -> bool:
        return self.headers.get("Origin", "") in self._allowed_origins

    # -- routes -------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
        """Answer the preflight, but only for our own extension."""

        if not self._origin_allowed():
            self._reply(403, {"ok": False, "error": "来源不被允许"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "60")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        """Refuse plainly, and close: nothing here answers a GET."""

        self._reply(405, {"ok": False, "error": "只接受 POST"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self._body_consumed = 0
        if self.path.split("?")[0].rstrip("/") != ROUTE:
            self._reply(404, {"ok": False, "error": "未知的路径"})
            return
        if not self._origin_allowed():
            self._reply(403, {"ok": False, "error": "来源不被允许"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"ok": False, "error": "请求体大小不合法"})
            return

        raw = self._read_request_body(length)
        if raw is None:
            # The body never finished arriving, so there is nothing complete to
            # parse. No drain either: these are exactly the bytes we stopped
            # waiting for, and reading them again would just burn the budget.
            self._reply(408, {"ok": False, "error": "读取请求体超时"}, drain=False)
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._reply(400, {"ok": False, "error": "请求体不是合法的 JSON"})
            return
        if not isinstance(message, dict):
            self._reply(400, {"ok": False, "error": "请求体不是 JSON 对象"})
            return

        # Same dispatcher the native host uses, so both routes share one gate.
        # The settings are handed in explicitly rather than re-derived, so the
        # bridge can never write to a different directory than the app reads.
        from core.native_host import handle

        self._reply(200, handle(message, settings=self._settings), cors=True)


class _Server(http.server.ThreadingHTTPServer):
    """A server that refuses to share its port.

    ``HTTPServer`` sets ``SO_REUSEADDR``, which on Windows lets a *second*
    process bind the same port and silently steal requests. The bridge must fail
    loudly instead, so the port really belongs to one application at a time.
    """

    allow_reuse_address = False
    daemon_threads = True


class LocalBridge:
    """Runs the loopback server for as long as the sign-in dialog is open."""

    def __init__(self, settings: Settings, *, port: int = DEFAULT_PORT) -> None:
        self.settings = settings
        self.port = port
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return bridge_url(self.port)

    @staticmethod
    def allowed_origins() -> frozenset[str]:
        """The extension origins Chrome will present, in both spellings."""

        from core.chrome_bridge import extension_id

        try:
            identifier = extension_id()
        except Exception:  # noqa: BLE001 - a missing extension means no bridge
            return frozenset()
        return frozenset({f"chrome-extension://{identifier}", f"chrome-extension://{identifier}/"})

    def start(self) -> int:
        """Start listening and return the port. Raises ``OSError`` when taken."""

        if self._server is not None:
            return self.port

        origins = self.allowed_origins()
        if not origins:
            raise OSError("找不到 Chrome 扩展，无法启用本地桥接")

        handler = partial(_Handler, settings=self.settings, allowed_origins=origins)
        server = _Server(("127.0.0.1", self.port), handler)
        thread = threading.Thread(target=server.serve_forever, name="vd-local-bridge", daemon=True)
        thread.start()

        self._server = server
        self._thread = thread
        logger.info("本地桥接已启动：127.0.0.1:%d", self.port)
        return self.port

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        logger.info("本地桥接已停止")


def probe(port: int = DEFAULT_PORT, *, timeout: float = 0.5) -> bool:
    """True when something is already listening on the bridge port."""

    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.settimeout(timeout)
        return probe_socket.connect_ex(("127.0.0.1", port)) == 0
