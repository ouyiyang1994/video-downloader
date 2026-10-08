"""Native messaging host: Chrome extension -> this process -> managed session.

Chrome starts this program when the extension calls
``chrome.runtime.sendNativeMessage``. It speaks Chrome's framing (a 4-byte
little-endian length followed by that many bytes of UTF-8 JSON) on stdin/stdout
and does exactly one job: turn a list of cookies the *browser* decrypted into
the Netscape session file :mod:`core.session_store` already owns.

Why this exists
---------------
Chromium 127+ protects its cookie database with App-Bound Encryption. yt-dlp
cannot decrypt that, and this project will not try to: no key extraction, no
injection, no patched browser. The ``chrome.cookies`` API is the supported way
to obtain a cookie, and Chrome performs the decryption itself for an extension
the user installed. This host is the other half of that supported path.

Invariants
----------
* a cookie **value** is never logged, never put in a response, never echoed in
  an error - only names, counts and domains;
* stdout carries the protocol and nothing else (all logging goes to a file, or
  to stderr as a last resort), because a stray byte on stdout desynchronises
  Chrome;
* a malformed or hostile message produces an error reply, never a crash.

Run ``python -m core.native_host --selftest`` to check that the host can start
and locate the session directory; the GUI uses that before registering it.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger("video_downloader.native_host")

#: Must match ``core.chrome_bridge.HOST_NAME`` and the manifest the application
#: writes into the registry.
HOST_NAME = "com.videodownloader.cookies"
HOST_VERSION = "1.0"

#: Absolute directory the launcher passes in, so the host does not have to guess
#: where the application keeps its data. Used by the development ``.bat``.
SESSION_DIR_ENV = "VIDEO_DOWNLOADER_SESSION_DIR"

#: Chrome passes no arguments and no custom environment to a native messaging
#: host, so the compiled host cannot be told where the sessions live. The
#: installer leaves this file next to it instead; ``install_bridge`` writes it.
HOST_CONFIG_NAME = "host-config.json"

#: Chrome allows 64 MB from the extension; anything past a few kilobytes means
#: something is wrong, so cap it well below the limit.
MAX_INBOUND_BYTES = 8 * 1024 * 1024
#: Chrome refuses host->extension messages larger than 1 MB.
MAX_OUTBOUND_BYTES = 1024 * 1024
#: A session is a handful of cookies; a larger list is a bug or an attack.
MAX_COOKIES = 256

#: Platform -> the domain its cookies live on and the cookie that proves the
#: session is a real login. ``tests/test_chrome_bridge.py`` asserts this stays in
#: step with the adapters, so the two can never drift apart.
PLATFORMS: dict[str, dict[str, Any]] = {
    "bilibili": {"domain": "bilibili.com", "auth": ("SESSDATA",)},
    "instagram": {"domain": "instagram.com", "auth": ("sessionid",)},
}


class ProtocolError(Exception):
    """The byte stream on stdin is not valid Chrome native messaging."""


# --- framing -----------------------------------------------------------------


def _binary_stdio() -> None:
    """Stop Windows from translating newlines inside the binary protocol.

    Without this, a ``\\n`` byte inside a length prefix or a JSON string would be
    written as ``\\r\\n`` and Chrome would lose framing. The CRT keeps stdout in
    text mode by default; ``msvcrt.setmode`` switches it.
    """

    try:
        import msvcrt
    except ImportError:  # pragma: no cover - POSIX has no text mode
        return
    for stream in (sys.stdin, sys.stdout):
        try:
            msvcrt.setmode(stream.fileno(), os.O_BINARY)
        except (OSError, ValueError, AttributeError):  # pragma: no cover - defensive
            logger.debug("无法把标准流切换为二进制模式")


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one framed message, or ``None`` at a clean end of stream."""

    header = _read_exactly(stream, 4, eof_ok=True)
    if not header:
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0:
        raise ProtocolError("消息长度为 0")
    if length > MAX_INBOUND_BYTES:
        raise ProtocolError(f"消息过大（{length} 字节）")
    payload = _read_exactly(stream, length, eof_ok=False)
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"消息不是合法的 JSON：{type(exc).__name__}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("消息不是 JSON 对象")
    return message


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    """Write one framed message and flush it immediately."""

    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_OUTBOUND_BYTES:
        payload = json.dumps(
            {"ok": False, "error": "响应过大，已拒绝发送"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def _read_exactly(stream: BinaryIO, count: int, *, eof_ok: bool) -> bytes:
    """Read exactly ``count`` bytes.

    ``eof_ok`` marks the first read of a message, where an immediate end of
    stream simply means "Chrome closed the pipe". Anywhere else a short read is
    truncation - and must be reported as such rather than silently handed to the
    JSON parser, which would blame the content instead of the framing.
    """

    chunks = bytearray()
    while len(chunks) < count:
        chunk = stream.read(count - len(chunks))
        if not chunk:
            if eof_ok and not chunks:
                return b""
            raise ProtocolError("消息在读取过程中被截断")
        chunks.extend(chunk)
    return bytes(chunks)


# --- session location --------------------------------------------------------


def _host_config() -> dict[str, Any]:
    """The config file the installer left next to the compiled host."""

    if not getattr(sys, "frozen", False):
        # A development run is started by host.bat, which exports the paths.
        return {}
    path = Path(sys.executable).parent / HOST_CONFIG_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("无法读取宿主配置（%s）：%s", path.name, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _settings() -> Any:
    """A :class:`~config.settings.Settings` pointing at the real data root.

    Two sources, because the two ways of starting the host differ: the
    development ``.bat`` exports an environment variable, while Chrome gives the
    compiled host nothing at all - so that one reads a small JSON file placed
    beside it. An explicit keyword beats every other source in pydantic-settings,
    so either wins over a stale ``.env``.
    """

    from config.settings import Settings

    config = _host_config()
    raw_session = os.environ.get(SESSION_DIR_ENV, "").strip() or str(config.get("sessionDir") or "")
    raw_log = str(config.get("logDir") or "")

    session_dir = Path(raw_session) if raw_session else None
    log_dir = Path(raw_log) if raw_log else None
    if session_dir is not None and log_dir is not None:
        return Settings(session_dir=session_dir, log_dir=log_dir)
    if session_dir is not None:
        return Settings(session_dir=session_dir)
    if log_dir is not None:
        return Settings(log_dir=log_dir)
    return Settings()


def _session_dir() -> Path:
    from core.session_store import session_dir

    return session_dir(_settings())


def _log_dir() -> Path:
    try:
        settings = _settings()
        return settings.resolve_path(settings.log_dir)
    except Exception:  # noqa: BLE001 - logging must never be the reason we fail
        return Path.cwd() / "logs"


def setup_logging() -> None:
    """Log to a file so nothing can pollute the protocol on stdout."""

    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    try:
        directory = _log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            directory / "native_host.log",
            maxBytes=256 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
    except OSError:  # pragma: no cover - unwritable location
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


# --- request handling --------------------------------------------------------


def handle(message: dict[str, Any], *, settings: Any | None = None) -> dict[str, Any]:
    """Dispatch one message, never raising.

    ``settings`` lets a transport that already knows the data root hand it in -
    the loopback bridge does - instead of every route having to re-derive it
    from the environment.
    """

    try:
        action = message.get("action")
        if action == "ping":
            return _ping()
        if action == "store":
            return store_cookies(
                settings or _settings(), message.get("platform"), message.get("cookies")
            )
        if action == "status":
            return _status(settings)
        if action == "clear":
            return _clear(message, settings)
        return {"ok": False, "error": f"未知的操作：{action!r}"}
    except Exception as exc:  # noqa: BLE001 - the host must answer, always
        logger.exception("处理消息时异常")
        return {"ok": False, "error": f"本机程序处理失败：{type(exc).__name__}"}


def _ping() -> dict[str, Any]:
    directory = _session_dir()
    return {
        "ok": True,
        "host": HOST_NAME,
        "hostVersion": HOST_VERSION,
        # Only the folder *name*: the extension has no use for the full path and
        # the popup shows this to the user.
        "sessionDir": directory.name,
        "platforms": sorted(PLATFORMS),
    }


def _status(settings: Any | None = None) -> dict[str, Any]:
    from core.models import Platform
    from core.session_store import has_session

    active = settings or _settings()
    return {
        "ok": True,
        "sessions": {name: has_session(active, Platform(name)) for name in sorted(PLATFORMS)},
    }


def _clear(message: dict[str, Any], settings: Any | None = None) -> dict[str, Any]:
    from core.models import Platform
    from core.session_store import delete_session

    platform = str(message.get("platform") or "")
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"未知的平台：{platform!r}"}
    removed = delete_session(settings or _settings(), Platform(platform))
    return {"ok": True, "platform": platform, "removed": removed}


def store_cookies(settings: Any, platform: Any, cookies: Any) -> dict[str, Any]:
    """Validate a cookie payload and write it as the platform's session.

    Split out from the message handler so both acquisition paths - the extension
    (through :func:`handle`) and the DevTools fallback
    (:func:`core.chrome_cdp.store`) - pass through exactly the same gate before
    anything reaches disk. Returns a native-messaging shaped response, so the
    caller can hand it straight back to the extension.
    """

    from core.models import Platform
    from core.session_store import write_session

    name = str(platform or "")
    spec = PLATFORMS.get(name)
    if spec is None:
        return {"ok": False, "error": f"未知的平台：{name!r}"}

    if not isinstance(cookies, list):
        return {"ok": False, "error": "cookies 字段缺失或不是数组"}
    if len(cookies) > MAX_COOKIES:
        return {"ok": False, "error": f"Cookie 数量过多（{len(cookies)}）"}

    domain_suffix: str = spec["domain"]
    usable = [
        cookie for cookie in (_cookie_from(item, domain_suffix) for item in cookies) if cookie
    ]
    if not usable:
        return {"ok": False, "error": "没有可用的 Cookie（可能已过期或缺少 name/value）"}

    names = sorted({cookie.name for cookie in usable})
    required = set(spec["auth"])
    if not required & set(names):
        return {
            "ok": False,
            "error": f"缺少该平台的登录 Cookie（需要 {'/'.join(sorted(required))}）",
        }

    path = write_session(settings, Platform(name), usable, domain_suffix=domain_suffix)
    if path is None:
        return {"ok": False, "error": "没有属于该平台的 Cookie，未写入任何内容"}

    logger.info(
        "已接收 %s 的会话：%d 个 Cookie（%s）",
        name,
        len(usable),
        ", ".join(names),
    )
    return {
        "ok": True,
        "platform": name,
        "saved": len(usable),
        # Names only - the response can travel back through Chrome, so it must
        # never carry a value.
        "cookieNames": names,
        "file": path.name,
    }


def _cookie_from(item: Any, domain_suffix: str) -> Any:
    """Build a :class:`http.cookiejar.Cookie`, or ``None`` when unusable.

    A cookie must already belong to the platform. Rewriting a foreign domain
    into the platform's domain would be the dangerous direction - it would send
    another site's cookie to this platform - so a mismatched or missing domain
    is rejected rather than repaired.
    """

    import time

    from core.session_store import make_cookie, matches_domain

    if not isinstance(item, dict):
        return None
    name = item.get("name")
    value = item.get("value")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(value, str) or value == "":
        return None

    raw_domain = item.get("domain")
    if not isinstance(raw_domain, str) or not matches_domain(raw_domain, domain_suffix):
        return None
    domain = raw_domain

    path = item.get("path")
    path = path if isinstance(path, str) and path.startswith("/") else "/"

    expires: int | None = None
    raw_expiry = item.get("expirationDate")
    if isinstance(raw_expiry, (int, float)):
        expires = int(raw_expiry)
        if expires <= int(time.time()):
            return None

    return make_cookie(
        name.strip(),
        value,
        domain=domain,
        path=path,
        secure=bool(item.get("secure", True)),
        expires=expires,
    )


# --- entry points ------------------------------------------------------------


def serve(stdin: BinaryIO, stdout: BinaryIO) -> int:
    """Run the protocol loop until Chrome closes the pipe."""

    while True:
        try:
            message = read_message(stdin)
        except ProtocolError as exc:
            logger.warning("协议错误：%s", exc)
            write_message(stdout, {"ok": False, "error": str(exc)})
            return 2
        if message is None:
            logger.info("标准输入已关闭，宿主退出")
            return 0
        write_message(stdout, handle(message))


def selftest() -> int:
    """Report whether the host can start and find the session directory.

    Used by the GUI before it registers the host, so a broken launcher is caught
    in the application rather than in a silent Chrome failure.
    """

    report: dict[str, Any] = {"ok": False, "hostVersion": HOST_VERSION}
    try:
        directory = _session_dir()
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".native_host_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.update(ok=True, sessionDir=str(directory), platforms=sorted(PLATFORMS))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        report["error"] = f"{type(exc).__name__}: {exc}"
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Video Downloader native messaging host")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="检查宿主能否启动并定位会话目录，然后退出",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    setup_logging()
    logger.info("宿主启动（%s v%s）", HOST_NAME, HOST_VERSION)
    _binary_stdio()
    return serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
