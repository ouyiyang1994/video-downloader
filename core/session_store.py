"""Managed login sessions: one Netscape cookie file per platform.

The GUI's login feature is the only writer. Whatever the browser handed over is
copied into ``secrets/<platform>_cookies.txt``; the browser's own profile is
never modified, so "退出登录" is exactly "delete this file" and the user's
browsing session is untouched.

Two invariants hold everywhere in this module:

* a cookie *value* is never logged, formatted into a message, or included in an
  exception - only file names, cookie names, counts and domains;
* a missing or unreadable file means "not logged in", never an error, so the
  existing anonymous download paths keep working exactly as before.
"""

from __future__ import annotations

import http.cookiejar
import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path

from config.settings import Settings
from core.models import Platform

logger = logging.getLogger(__name__)

#: Suffix of the managed file for a platform. The folder itself is
#: ``Settings.session_dir`` (``secrets`` by default); it is listed in
#: ``.gitignore`` and flagged by ``packaging/ci_guard.py``, so a session can
#: never be committed.
SESSION_SUFFIX = "_cookies.txt"

#: Netscape files written by yt-dlp start with this header.
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"


def session_dir(settings: Settings) -> Path:
    """Directory holding every managed session.

    Configurable through ``SESSION_DIR`` so an installed build can keep the
    sessions next to its other data, and so tests never touch the real folder.
    """

    return settings.resolve_path(settings.session_dir)


def session_file(settings: Settings, platform: Platform) -> Path:
    """Path of the managed session for ``platform`` (may not exist yet)."""

    return session_dir(settings) / f"{platform.value}{SESSION_SUFFIX}"


def has_session(settings: Settings, platform: Platform) -> bool:
    """True when a managed session file exists and is not empty."""

    path = session_file(settings, platform)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def delete_session(settings: Settings, platform: Platform) -> bool:
    """Remove the managed session for ``platform``.

    Returns True when a file was actually removed. Only this file is touched -
    the browser's cookies, the ``.env`` configuration and the other platform's
    session are all left alone.
    """

    path = session_file(settings, platform)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("删除会话文件失败（%s）：%s", path.name, type(exc).__name__)
        return False
    logger.info("已删除 %s 的本地登录会话（%s）", platform.value, path.name)
    return True


def make_cookie(
    name: str,
    value: str,
    *,
    domain: str,
    path: str = "/",
    secure: bool = True,
) -> http.cookiejar.Cookie:
    """Build a :class:`~http.cookiejar.Cookie` for the Netscape writer."""

    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=int(time.time()) + 365 * 24 * 3600,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def matches_domain(domain: str, domain_suffix: str) -> bool:
    """True when ``domain`` belongs to ``domain_suffix`` (``bilibili.com``)."""

    wanted = domain_suffix.lstrip(".").lower()
    actual = (domain or "").lstrip(".").lower()
    return actual == wanted or actual.endswith(f".{wanted}")


def header_from_cookies(cookies: Iterable[http.cookiejar.Cookie]) -> str:
    """Assemble a ``Cookie`` header from cookies, mirroring the read path.

    The counterpart of :func:`core.http.load_cookie_header`, so a session can be
    validated in memory before it is written to disk.
    """

    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies)


def write_session(
    settings: Settings,
    platform: Platform,
    cookies: Iterable[http.cookiejar.Cookie],
    *,
    domain_suffix: str,
) -> Path | None:
    """Persist ``cookies`` as the managed session for ``platform``.

    Only cookies belonging to ``domain_suffix`` are written, so a browser dump
    can never leak unrelated sites into the file. Returns the path, or ``None``
    when there was nothing to write.
    """

    selected = [cookie for cookie in cookies if matches_domain(cookie.domain, domain_suffix)]
    if not selected:
        return None

    from yt_dlp.cookies import YoutubeDLCookieJar

    jar = YoutubeDLCookieJar()
    for cookie in selected:
        jar.set_cookie(cookie)

    path = session_file(settings, platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(str(path), ignore_discard=True, ignore_expires=True)
    _restrict(path)

    names = sorted({cookie.name for cookie in selected})
    logger.info(
        "已保存 %s 的本地登录会话：%s（%d 个 Cookie：%s）",
        platform.value,
        path.name,
        len(selected),
        ", ".join(names),
    )
    return path


def _restrict(path: Path) -> None:
    """Best-effort owner-only permissions (a no-op where unsupported)."""

    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("无法设置 %s 的权限（平台不支持）", path.name)
