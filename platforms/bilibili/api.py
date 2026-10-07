"""Public Bilibili web endpoints used by the player itself."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.exceptions import AuthRequiredError, MetadataError, RateLimitedError
from platforms.bilibili import wbi

logger = logging.getLogger(__name__)

API_ROOT = "https://api.bilibili.com"
VIEW_API = f"{API_ROOT}/x/web-interface/view"
NAV_API = f"{API_ROOT}/x/web-interface/nav"
PLAYURL_API = f"{API_ROOT}/x/player/playurl"
WBI_PLAYURL_API = f"{API_ROOT}/x/player/wbi/playurl"

# fnval bit mask: DASH + HDR + 4K + Dolby vision + Dolby audio + AV1.
FNVAL_DASH = 4048


class WbiKeyCache:
    """Caches the WBI keys for the lifetime of a process."""

    def __init__(self) -> None:
        self._keys: tuple[str, str] | None = None

    async def get(self, client: httpx.AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
        if self._keys is None:
            response = await client.get(NAV_API, headers=headers)
            _raise_for_code(response, context="获取 WBI 密钥")
            payload = response.json()
            wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
            img_url = wbi_img.get("img_url") or ""
            sub_url = wbi_img.get("sub_url") or ""
            if not img_url or not sub_url:
                raise MetadataError("Bilibili 未返回 WBI 密钥")
            self._keys = (wbi.extract_key(img_url), wbi.extract_key(sub_url))
        return self._keys


def _raise_for_code(response: httpx.Response, *, context: str) -> None:
    if response.status_code == 412:
        raise RateLimitedError("Bilibili 触发了风控（HTTP 412）", detail=context)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise MetadataError(f"{context}失败：HTTP {response.status_code}") from exc


def _check_payload(payload: dict[str, Any], *, context: str) -> dict[str, Any]:
    code = payload.get("code")
    if code == 0:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    message = payload.get("message") or "unknown error"
    if code in (-101, -400, -403):
        raise AuthRequiredError(
            "Bilibili 需要登录后才能获取该资源",
            detail=f"{context}: [{code}] {message}",
        )
    raise MetadataError(f"{context}失败：[{code}] {message}")


async def fetch_view(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    bvid: str | None = None,
    aid: str | None = None,
) -> dict[str, Any]:
    params = {"bvid": bvid} if bvid else {"aid": aid}
    response = await client.get(VIEW_API, params=params, headers=headers)
    _raise_for_code(response, context="获取视频信息")
    return _check_payload(response.json(), context="获取视频信息")


async def fetch_nav(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    timeout: float | None = None,  # noqa: ASYNC109 - a probe, not a long download
) -> dict[str, Any]:
    """``x/web-interface/nav`` - who, if anyone, is signed in on this session.

    This is the same call the web player makes on every page load. Unlike the
    other endpoints it answers ``code = -101`` ("账号未登录") for an anonymous
    caller, which is a normal answer here rather than a failure, so the payload
    is handed back untouched for the caller to interpret.
    """

    response = await client.get(NAV_API, headers=headers, timeout=timeout)
    _raise_for_code(response, context="检查登录状态")
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def fetch_playurl(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    bvid: str,
    cid: int,
    keys: tuple[str, str] | None = None,
    qn: int = 127,
    fnval: int = FNVAL_DASH,
) -> dict[str, Any]:
    """Ask for the best playable streams the current session is entitled to."""

    params: dict[str, Any] = {
        "bvid": bvid,
        "cid": cid,
        "qn": qn,
        "fnval": fnval,
        "fourk": 1,
        "fnver": 0,
    }
    if keys is not None:
        params = wbi.sign(params, keys[0], keys[1])
        url = WBI_PLAYURL_API
    else:
        url = PLAYURL_API

    response = await client.get(url, params=params, headers=headers)
    _raise_for_code(response, context="获取播放地址")
    return _check_payload(response.json(), context="获取播放地址")
