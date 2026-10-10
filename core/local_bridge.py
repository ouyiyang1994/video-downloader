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
import threading
from functools import partial
from typing import Any

from config.settings import Settings

logger = logging.getLogger(__name__)

#: Fixed, because the extension cannot be told an ephemeral one. If something
#: else already holds it, :meth:`LocalBridge.start` says so instead of silently
#: choosing another.
DEFAULT_PORT = 8765
ROUTE = "/session"

#: A session is a few kilobytes; anything larger is a mistake or an attack.
MAX_BODY_BYTES = 512 * 1024

#: How much of a *refused* request's body we are still willing to read. Bounded,
#: so a hostile ``Content-Length`` cannot pin a worker thread on a body we are
#: about to reject anyway.
DRAIN_LIMIT_BYTES = MAX_BODY_BYTES + 1

#: A peer that stops mid-body must not hold the worker thread open forever.
DRAIN_TIMEOUT_SECONDS = 2.0


def bridge_url(port: int = DEFAULT_PORT) -> str:
    """The URL the extension posts to."""

    return f"http://127.0.0.1:{port}{ROUTE}"


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
        super().__init__(*args, **kwargs)

    # -- helpers ------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        logger.debug("本地桥接 %s：%s", self.address_string(), format % args)

    def _drain_request_body(self) -> None:
        """Swallow the rest of a refused request's body before we close.

        Closing a socket while unread bytes are still queued makes Windows send
        RST instead of FIN, and the client then loses the refusal we just wrote
        - it reports ``WinError 10053`` rather than reading the 403/413. The
        oversized case is exactly that: we answer while the peer is still
        sending. Reading the body first keeps the close orderly.
        """

        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(remaining, DRAIN_LIMIT_BYTES)
        if remaining <= 0:
            return

        previous = self.connection.gettimeout()
        self.connection.settimeout(DRAIN_TIMEOUT_SECONDS)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # The peer stalled or vanished; there is nothing left to drain and
            # the refusal has already been written.
            logger.debug("本地桥接：拒绝请求时未能读完请求体")
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(previous)

    def _reply(self, status: int, payload: dict[str, Any], *, cors: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status != 200:
            # A refused request may still have an unread body on the wire - the
            # oversized case certainly does. Closing stops the next read from
            # parsing those bytes as a fresh request and logging a traceback.
            self.close_connection = True
            self.send_header("Connection", "close")
        if cors:
            # Only ever echo back to the extension that asked, and only after
            # the origin check below has already passed.
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.end_headers()
        self.wfile.write(body)
        if status != 200:
            self._drain_request_body()

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

        raw = self.rfile.read(length)
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
