"""Per-platform switch that hides the ``.env`` fallback credentials.

「退出登录」 deletes the session this application stored - but Instagram and
Bilibili also read credentials configured in ``.env`` (``YTDLP_COOKIEFILE``,
``BILIBILI_COOKIEFILE``, ``BILIBILI_COOKIE`` / ``BILIBILI_SESSDATA``). Those
would log the platform straight back in on the next refresh, which made the
button look broken.

This module owns the per-platform flag that turns those fallbacks *off*. Three
invariants hold here:

* the flag only ever hides ``.env`` credentials from this application - it
  never edits ``.env``, never deletes a cookie file, and never records a value;
* the managed session under ``secrets/`` is always honoured, so signing in
  again through the browser or the Chrome helper keeps working;
* the state lives in one small JSON file inside the session directory, written
  atomically, so it survives a restart and the platforms stay independent.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from config.settings import Settings
from core.models import Platform
from core.session_store import session_dir

logger = logging.getLogger(__name__)

#: File inside the session directory. It holds platform names only - no cookie,
#: no token, nothing that could authenticate anything.
STATE_NAME = "fallback_state.json"
STATE_VERSION = 1


def state_file(settings: Settings) -> Path:
    """Where the per-platform flags are kept."""

    return session_dir(settings) / STATE_NAME


def disabled_platforms(settings: Settings) -> frozenset[Platform]:
    """Platforms whose ``.env`` fallback must be ignored right now.

    A missing file means "nothing is disabled". A corrupt one means the same:
    failing closed would silently ignore a user's configured session, and the
    sign-in screen must never break because of a half-written state file.
    """

    path = state_file(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError) as exc:
        logger.warning("读取配置会话开关失败（%s）：%s", path.name, type(exc).__name__)
        return frozenset()

    names = raw.get("disabled") if isinstance(raw, dict) else None
    if not isinstance(names, list):
        return frozenset()
    known = {platform.value: platform for platform in Platform}
    return frozenset(known[str(name)] for name in names if str(name) in known)


def is_disabled(settings: Settings, platform: Platform) -> bool:
    """True when ``platform`` must not read its ``.env`` fallback."""

    return platform in disabled_platforms(settings)


def set_disabled(settings: Settings, platform: Platform, *, disabled: bool) -> frozenset[Platform]:
    """Switch ``platform``'s fallback off (or back on) and persist the change.

    Returns the flags that are on disk afterwards - the requested set on
    success, and whatever could still be read when the write fails.
    """

    current = set(disabled_platforms(settings))
    if disabled:
        current.add(platform)
    else:
        current.discard(platform)
    return _write(settings, frozenset(current))


def _write(settings: Settings, platforms: frozenset[Platform]) -> frozenset[Platform]:
    path = state_file(settings)
    payload = {
        "version": STATE_VERSION,
        "disabled": sorted(platform.value for platform in platforms),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory, then a rename: an interrupted write can never leave a
        # half-parsed file behind for the next start to choke on.
        staging = path.with_name(path.name + ".tmp")
        staging.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staging, path)
    except OSError as exc:
        logger.warning("保存配置会话开关失败（%s）：%s", path.name, type(exc).__name__)
        return disabled_platforms(settings)
    _restrict(path)
    return platforms


def _restrict(path: Path) -> None:
    """Best-effort owner-only permissions (a no-op where unsupported)."""

    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("无法设置 %s 的权限（平台不支持）", path.name)
