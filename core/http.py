"""Shared httpx client construction."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import httpx

from config.settings import Settings

logger = logging.getLogger(__name__)

#: Small, cache-free endpoint used by the GUI's "test proxy" button. It is only
#: requested when the user asks for a test - never during a download.
PROXY_PROBE_URL: Final[str] = "https://www.gstatic.com/generate_204"


def default_headers(settings: Settings) -> dict[str, str]:
    return {
        "User-Agent": settings.user_agent,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def build_client(
    settings: Settings,
    *,
    headers: dict[str, str] | None = None,
    use_proxy: bool = True,
) -> httpx.AsyncClient:
    """Create an httpx client.

    ``use_proxy=False`` keeps the direct connection and is used for platforms
    listed in ``PROXY_BYPASS_PLATFORMS``.
    """

    merged = default_headers(settings)
    if headers:
        merged.update(headers)
    connect_timeout = min(settings.request_timeout, 15.0)
    return httpx.AsyncClient(
        headers=merged,
        timeout=httpx.Timeout(settings.request_timeout, connect=connect_timeout),
        follow_redirects=True,
        proxy=settings.proxy if use_proxy else None,
        # A direct client must stay direct even after the proxy has been
        # exported to the process environment: Bilibili answers HTTP 412 to
        # proxy exit IPs, so trust_env has to be off for it.
        trust_env=use_proxy,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    )


@asynccontextmanager
async def open_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    client = build_client(settings)
    try:
        yield client
    finally:
        await client.aclose()


def load_cookie_header(
    cookie_file: Path | None,
    *,
    domain_suffix: str | None = None,
) -> str | None:
    """Build a ``Cookie`` header from a Netscape ``cookies.txt``.

    Reuses yt-dlp's cookie jar so every platform shares one parser instead of
    growing its own. Only the assembled header string is returned; the values
    are never logged, stored or exposed anywhere else.

    Returns ``None`` when the file is missing, unreadable, empty, or holds no
    cookie for ``domain_suffix`` - callers then simply continue anonymously.
    """

    if cookie_file is None or not cookie_file.is_file():
        return None
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar

        jar = YoutubeDLCookieJar(str(cookie_file))
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:  # noqa: BLE001 - any parse failure means "no cookies"
        # Only the file name and the exception type: the parser message quotes
        # the offending line, which would put a cookie value in the log.
        logger.warning("读取 Cookie 文件失败（%s）：%s", cookie_file.name, type(exc).__name__)
        return None

    wanted = (domain_suffix or "").lstrip(".").lower()
    pairs: list[str] = []
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if wanted and not (domain == wanted or domain.endswith(f".{wanted}")):
            continue
        pairs.append(f"{cookie.name}={cookie.value}")
    return "; ".join(pairs) if pairs else None


# The timeout is applied to both stages explicitly (the TCP probe and the
# httpx request), which is why it is a parameter rather than inside a context
# manager. See the docstring.
async def check_proxy(proxy: str, *, timeout: float = 10.0) -> tuple[bool, str]:  # noqa: ASYNC109
    """Verify that ``proxy`` is reachable *and* can actually relay a request.

    Two stages, so the user gets a precise reason:

    1. a plain TCP connect, which separates "the proxy is not running" from
       "the proxy is up but cannot reach the internet";
    2. a real HTTPS request through the proxy.

    Returns ``(ok, message)`` with a message ready to show in the GUI.
    """

    parsed = httpx.URL(proxy)
    host = parsed.host
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=min(timeout, 5.0)
        )
    except (OSError, TimeoutError):
        return False, f"无法连接到 {host}:{port}，请确认代理正在运行"
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    try:
        async with httpx.AsyncClient(
            proxy=proxy, timeout=timeout, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.get(PROXY_PROBE_URL)
    except httpx.ProxyError:
        return False, "代理拒绝转发请求，地址或协议可能不正确"
    except httpx.ConnectError:
        return False, "已连上代理端口，但代理无法转发请求（可能未启动或配置有误）"
    except httpx.TimeoutException:
        return False, "代理转发超时，请检查代理和网络"
    except httpx.HTTPError as exc:
        return False, f"代理请求失败（{type(exc).__name__}）"

    if response.status_code >= 400:
        return False, f"代理已连通，但目标返回 HTTP {response.status_code}"
    return True, f"代理连接成功（HTTP {response.status_code}）"
