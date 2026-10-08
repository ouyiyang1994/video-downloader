"""Tests for the loopback fallback transport.

The server is real and is spoken to over a real socket, because the parts worth
testing here - the origin check, the size limit, the shared validation - only
exist at the HTTP layer.
"""

from __future__ import annotations

import json

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
