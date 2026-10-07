"""The GUI's network-proxy settings: read, save, apply, and verify."""

from __future__ import annotations

import asyncio
import os
import socket

import httpx

from config import settings as settings_module
from config.constants import DEFAULT_PROXY_HOST, DEFAULT_PROXY_PORT, SUPPORTED_PLATFORM_IDS
from config.settings import ProxyConfig, Settings
from core import http as http_module
from core.http import build_client, check_proxy
from core.models import Platform
from core.registry import build_registry

# --- constants --------------------------------------------------------------


def test_supported_platform_ids_match_the_enum() -> None:
    assert tuple(platform.value for platform in Platform) == SUPPORTED_PLATFORM_IDS


# --- ProxyConfig ------------------------------------------------------------


def test_reads_the_configured_proxy_from_settings(settings: Settings) -> None:
    settings.http_proxy = "http://127.0.0.1:1080"
    settings.proxy_bypass_platforms = "bilibili"

    config = ProxyConfig.from_settings(settings)

    assert config.enabled is True
    assert config.host == "127.0.0.1"
    assert config.port == 1080
    assert config.url == "http://127.0.0.1:1080"
    assert config.proxied_platforms == ("youtube", "instagram")
    assert config.bypass_platforms == ("bilibili",)


def test_suggests_defaults_when_nothing_is_configured(settings: Settings) -> None:
    settings.http_proxy = None
    settings.https_proxy = None
    settings.proxy_bypass_platforms = "bilibili"

    config = ProxyConfig.from_settings(settings)

    assert config.enabled is False
    assert (config.host, config.port) == (DEFAULT_PROXY_HOST, DEFAULT_PROXY_PORT)
    # The per-platform switches still default to YouTube/Instagram on, B站 off.
    assert config.proxied_platforms == ("youtube", "instagram")


def test_disabled_config_has_no_url() -> None:
    assert ProxyConfig(enabled=False, host="127.0.0.1", port=8090).url is None


def test_env_payload_uses_the_existing_keys() -> None:
    config = ProxyConfig(
        enabled=True, host="10.0.0.5", port=8888, proxied_platforms=("youtube", "instagram")
    )
    assert config.as_env() == {
        "HTTP_PROXY": "http://10.0.0.5:8888",
        "HTTPS_PROXY": "http://10.0.0.5:8888",
        "PROXY_BYPASS_PLATFORMS": "bilibili",
    }


def test_disabled_env_payload_clears_both_urls() -> None:
    config = ProxyConfig(enabled=False, proxied_platforms=("youtube", "instagram"))
    payload = config.as_env()
    assert payload["HTTP_PROXY"] == ""
    assert payload["HTTPS_PROXY"] == ""
    assert payload["PROXY_BYPASS_PLATFORMS"] == "bilibili"


# --- saving -----------------------------------------------------------------


def test_save_writes_env_and_keeps_other_keys(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 我的配置\nYOUTUBE_API_KEY=keep-me\nHTTP_PROXY=\nPROXY_BYPASS_PLATFORMS=bilibili\n",
        encoding="utf-8",
    )
    config = ProxyConfig(
        enabled=True, host="127.0.0.1", port=8090, proxied_platforms=("youtube", "instagram")
    )

    written = settings_module.save_proxy_configuration(config, env_file)

    assert written == env_file
    text = env_file.read_text(encoding="utf-8")
    assert "YOUTUBE_API_KEY=keep-me" in text
    assert "# 我的配置" in text
    assert "HTTP_PROXY=http://127.0.0.1:8090" in text
    assert "HTTPS_PROXY=http://127.0.0.1:8090" in text
    assert "PROXY_BYPASS_PLATFORMS=bilibili" in text
    # No duplicate keys were appended.
    assert text.count("HTTP_PROXY=") == 1


def test_save_leaves_no_temp_file_behind(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("HTTP_PROXY=\n", encoding="utf-8")
    settings_module.save_proxy_configuration(ProxyConfig(enabled=False), env_file)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_save_can_proxy_bilibili_and_bypass_youtube(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    config = ProxyConfig(enabled=True, host="127.0.0.1", port=8090, proxied_platforms=("bilibili",))

    settings_module.save_proxy_configuration(config, env_file)

    text = env_file.read_text(encoding="utf-8")
    assert "PROXY_BYPASS_PLATFORMS=youtube,instagram" in text


def test_save_creates_env_from_the_template_when_missing(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "程序目录"
    app_root.mkdir()
    (app_root / ".env.example").write_text("YOUTUBE_API_KEY=\nHTTP_PROXY=\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "APP_ROOT", app_root)
    target = tmp_path / "数据目录" / ".env"
    target.parent.mkdir()

    settings_module.save_proxy_configuration(
        ProxyConfig(enabled=True, host="127.0.0.1", port=8090), target
    )

    text = target.read_text(encoding="utf-8")
    assert "YOUTUBE_API_KEY=" in text  # template keys survived
    assert "HTTP_PROXY=http://127.0.0.1:8090" in text


# --- applying to the live process -------------------------------------------


def test_apply_updates_settings_and_environment(settings: Settings, clean_proxy_env) -> None:
    config = ProxyConfig(
        enabled=True, host="127.0.0.1", port=8090, proxied_platforms=("youtube", "instagram")
    )

    settings_module.apply_proxy_configuration(settings, config)

    assert settings.proxy == "http://127.0.0.1:8090"
    assert settings.proxy_bypass_set == {"bilibili"}
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:8090"


def test_apply_takes_effect_on_clients_built_afterwards(
    settings: Settings, clean_proxy_env
) -> None:
    """A saved change must be visible without restarting the GUI."""

    config = ProxyConfig(
        enabled=True, host="127.0.0.1", port=8090, proxied_platforms=("youtube", "instagram")
    )
    settings_module.apply_proxy_configuration(settings, config)

    client = build_client(settings)
    try:
        mounts = client._mounts  # noqa: SLF001 - the only way to observe routing
        assert mounts, "expected the proxied client to install a transport"
    finally:
        asyncio.run(client.aclose())


def test_turning_the_proxy_off_stops_using_it(settings: Settings, clean_proxy_env) -> None:
    on = ProxyConfig(enabled=True, host="127.0.0.1", port=8090, proxied_platforms=("youtube",))
    settings_module.apply_proxy_configuration(settings, on)
    assert settings.proxy is not None
    assert os.environ.get("HTTP_PROXY") is not None

    settings_module.apply_proxy_configuration(settings, ProxyConfig(enabled=False))

    assert settings.proxy is None
    assert os.environ.get("HTTP_PROXY") is None
    # With nothing proxied, every platform is on the bypass list.
    assert settings.proxy_bypass_set == set(SUPPORTED_PLATFORM_IDS)


# --- routing ----------------------------------------------------------------


async def test_youtube_and_instagram_use_the_proxy_bilibili_does_not(
    settings: Settings, clean_proxy_env
) -> None:
    config = ProxyConfig(
        enabled=True, host="127.0.0.1", port=8090, proxied_platforms=("youtube", "instagram")
    )
    settings_module.apply_proxy_configuration(settings, config)

    shared = build_client(settings)
    registry = build_registry(settings, shared)
    try:
        assert registry.get(Platform.YOUTUBE).client is shared
        assert registry.get(Platform.INSTAGRAM).client is shared
        assert registry.get(Platform.BILIBILI).client is not shared
        assert registry.owned_clients, "Bilibili should get its own direct client"
    finally:
        for extra in registry.owned_clients:
            await extra.aclose()
        await shared.aclose()


# --- the "test proxy" check -------------------------------------------------


def _listening_port() -> tuple[socket.socket, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, int(server.getsockname()[1])


async def test_check_proxy_reports_a_closed_port() -> None:
    server, port = _listening_port()
    server.close()

    ok, message = await check_proxy(f"http://127.0.0.1:{port}", timeout=2.0)

    assert ok is False
    assert "无法连接到" in message
    assert str(port) in message


async def test_check_proxy_reports_a_listener_that_cannot_relay() -> None:
    server, port = _listening_port()
    try:
        ok, message = await check_proxy(f"http://127.0.0.1:{port}", timeout=2.0)
    finally:
        server.close()

    assert ok is False
    assert "代理" in message


async def test_check_proxy_reports_success(monkeypatch) -> None:
    server, port = _listening_port()

    class _FakeResponse:
        status_code = 204

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["proxy"] == f"http://127.0.0.1:{port}"

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str) -> _FakeResponse:
            assert url == http_module.PROXY_PROBE_URL
            return _FakeResponse()

    monkeypatch.setattr(http_module.httpx, "AsyncClient", _FakeClient)
    try:
        ok, message = await check_proxy(f"http://127.0.0.1:{port}", timeout=2.0)
    finally:
        server.close()

    assert ok is True
    assert "成功" in message


async def test_check_proxy_reports_an_http_error(monkeypatch) -> None:
    server, port = _listening_port()

    class _FailingClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            raise httpx.ConnectError("relay refused")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(http_module.httpx, "AsyncClient", _FailingClient)
    try:
        ok, message = await check_proxy(f"http://127.0.0.1:{port}", timeout=2.0)
    finally:
        server.close()

    assert ok is False
    assert "无法转发" in message
